"""The `govern` command line. A project may keep its own entry-point name (the one its skills
and scripts already call, installed from `engine/govern/templates/entrypoint.py`): the program name in
usage and in messages is whatever the engine was invoked as.

Exit codes: 0 clean, 1 a finding or an unresolved problem, 2 usage or configuration error.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import sys
from pathlib import Path

import govern.checks  # noqa: F401  (registers the built-in checks)
from govern import __version__, blocks, config, layout, manifest, notice, ratchet, registry
from govern.context import Context
from govern.findings import REPORTERS, Findings, report_per_scope
from govern.text import Unreadable, eol, read, write


def fail(msg: str, code: int = 2) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return code


def load_context(root: Path | None, prog: str) -> Context:
    if root is None:
        root = config.find_root(Path.cwd())
        if root is None:
            raise config.ConfigError(f"no {config.CONFIG_NAME} here or in any parent directory")
    if not (root / config.CONFIG_NAME).is_file():
        raise config.ConfigError(f"{root / config.CONFIG_NAME} not found")
    home = layout.home()
    cfg = config.load(root, home)
    reg = registry.load(cfg)
    return Context(root=root, home=home, cfg=cfg, registry=reg, prog=prog)


# ---------------------------------------------------------------------------- check

def _order(ctx: Context, phase: str) -> list[str]:
    listed = ctx.cfg.raw.get("checks", {}).get(f"{phase}_order")
    if phase == "workspace":
        default = [c for c in manifest.registration_order("workspace")]
        default += [cid for cid, c in manifest.CHECKS.items() if c.also_workspace]
    else:
        default = manifest.registration_order("project")
    order = list(listed) if listed is not None else default
    return order + [cid for cid in default if cid not in order]


def _applies(chk: manifest.Check, scope) -> bool:
    if chk.applies == "doc-set":
        return scope.doc_set
    if chk.applies == "governed":
        return scope.governed
    return True


def _run(ctx: Context, cid: str, scope=None) -> Findings:
    chk, settings = manifest.CHECKS[cid], ctx.cfg.checks[cid]
    if settings.level == "off":
        return Findings()
    try:
        found = chk.fn(ctx, settings.params) if scope is None \
            else chk.fn(ctx, settings.params, scope)
    except Unreadable as exc:
        found = Findings()
        found.error(f"{_rel(ctx, exc.path)}: {exc.reason} — {cid} could not check it")
    return found.capped(settings.level)


def _rel(ctx: Context, path: Path) -> str:
    try:
        return ctx.rel(path)
    except ValueError:
        return str(path)


def collect_scoped(ctx: Context, targets: list, workspace: bool
                   ) -> list[tuple[str, str, Findings]]:
    """Every check's findings, in run order: (scope label, check id, findings). Without
    `workspace` (a `check --project X` run), a workspace check marked `also_project` still
    runs, restricted to each of `targets` — the workspace checks that are really about one
    project's files (doc-links, generated-blocks, ratchet), so a pre-commit hook checking one
    project alone still sees them. Every other workspace check sits out."""
    out = []
    if workspace:
        label = ctx.workspace_label
        for cid in _order(ctx, "workspace"):
            chk = manifest.CHECKS[cid]
            if chk.scope == "workspace":
                out.append((label, cid, _run(ctx, cid)))
            elif chk.also_workspace:
                out.append((label, cid, _run(ctx, cid, ctx.registry.workspace)))
    else:
        for scope in targets:
            for cid in _order(ctx, "workspace"):
                chk = manifest.CHECKS[cid]
                if chk.scope == "workspace" and chk.also_project:
                    out.append((scope.name, cid, _run(ctx, cid, scope)))
    for scope in targets:
        for cid in _order(ctx, "project"):
            chk = manifest.CHECKS[cid]
            if chk.scope == "project" and _applies(chk, scope):
                out.append((scope.name, cid, _run(ctx, cid, scope)))
    return out


def collect(ctx: Context, targets: list, workspace: bool) -> list[tuple[str, Findings]]:
    """Every check's findings, in run order, each tagged with the check that raised it."""
    return [(cid, found) for _, cid, found in collect_scoped(ctx, targets, workspace)]


def cmd_check(ctx: Context, project: str | None, path: str | None = None,
             history_from: str | None = None) -> int:
    """`--path` checks a scope's files from a directory swapped in for the one the registry
    names — a git pre-commit hook's staged-tree snapshot, say — instead of the checkout on disk.
    `--history-from` then answers every git question a project-scope check asks about those
    files (last touched, committed) from that checkout instead, since the snapshot carries no
    history of its own."""
    if path is not None and not project:
        return fail("--path needs --project")
    if history_from is not None and path is None:
        return fail("--history-from needs --path")
    if project:
        scope = ctx.registry.find(project)
        if scope is None:
            return fail(f"no project '{project}' in {ctx.registry.path.name}")
        if path is not None:
            # Resolved the way the scope's own dir is: joined onto the registry root when
            # relative, used as-is when already absolute.
            snapshot = ctx.root / path
            if not snapshot.is_dir():
                return fail(f"--path {path} does not exist")
            ctx.real_scope_dir = scope.gov   # before it is swapped, below
            scope = dataclasses.replace(scope, gov=snapshot)
            ctx.snapshot = snapshot
            if history_from is not None:
                ctx.history_from = ctx.root / history_from
        targets = [scope]
    else:
        targets = ctx.registry.scopes
    results = collect_scoped(ctx, targets, workspace=not project)
    if ctx.cfg.dialect["report"] == "per-scope":
        groups: dict[str, Findings] = {}
        for label, _, found in results:
            groups.setdefault(label, Findings()).extend(found)
        return 0 if report_per_scope(list(groups.items())) else 1
    total = Findings()
    for _, _, found in results:
        total.extend(found)
    label = f"project:{project}" if project else ctx.workspace_label
    return 0 if REPORTERS["aggregate"](label, total) else 1


# ---------------------------------------------------------------------------- explain

from govern.report import value as _value


def cmd_explain(ctx: Context, only: str | None) -> int:
    """Every effective setting and where it came from: the engine default, the profile, or the
    project's config.toml."""
    if only and only not in manifest.CHECKS:
        return fail(f"no check '{only}' — `{ctx.prog} explain` lists them all")
    if not only:
        prof = ctx.cfg.profile
        print(f"engine {__version__} · policy {config.CONFIG_NAME}"
              + (f" · profile {prof.source}" if prof else ""))
        raw = ctx.cfg.raw.get("dialect", {})
        pd = prof.settings.get("dialect", {}) if prof else {}
        print("dialect")
        for key, value in ctx.cfg.dialect.items():
            src = "project" if key in raw else "profile" if key in pd else "engine default"
            print(f"  {key} = {_value(value)}  ({src})")
    ids = [only] if only else _order(ctx, "workspace") + [
        c for c in _order(ctx, "project") if c not in _order(ctx, "workspace")]
    for cid in ids:
        chk, s = manifest.CHECKS[cid], ctx.cfg.checks[cid]
        lsrc = s.source.get("level", "engine")
        flags = [f"{s.level} ({'engine default' if lsrc == 'engine' else lsrc})"]
        if chk.ratchets:
            flags.append("ratchet " + ("on" if s.ratchet else f"off ({s.source.get('ratchet')})"))
        if chk.core:
            flags.append("fixed core")
        if chk.origin != "engine":
            flags.append(f"extension: {chk.origin}")
        print(f"\n{cid}  {' · '.join(flags)}")
        print(f"  {chk.summary}")
        for key, p in chk.params.items():
            src = s.source.get(key, "engine")
            note = "" if src == "engine" else f"; engine default {_value(p.default)}"
            print(f"  {key} = {_value(s.params[key])}  ({src if src != 'engine' else 'engine default'}{note})")
        if s.reason:
            print(f"  reason: {s.reason}")
    return 0


def cmd_principles(ctx: Context) -> int:
    """The doctrine this project works by: its own PRINCIPLES.md if it keeps one, else its
    profile's."""
    prof = ctx.cfg.profile
    doc = prof.document(ctx.root, "PRINCIPLES.md") if prof else None
    if doc is None:
        local = ctx.root / layout.GOV_DIR / "PRINCIPLES.md"
        doc = local if local.is_file() else None
    if doc is None:
        print("no principles: this project names no profile and keeps no PRINCIPLES.md",
              file=sys.stderr)
        return 1
    print(read(doc).rstrip())
    return 0


# ---------------------------------------------------------------------------- index

def cmd_index(ctx: Context) -> int:
    """Every block is rendered before any file is written, so a failure part-way leaves the
    tree as it was rather than half-regenerated."""
    changed, missing = 0, 0
    pending: dict[Path, str] = {}
    lines = []
    for path, bid, build in blocks.targets(ctx):
        rel = ctx.rel(path)
        if not path.exists():
            lines.append(f"  missing     {rel} :: {bid} (file)")
            missing += 1
            continue
        text = pending.get(path) or read(path)
        if blocks.extract(ctx, text, bid) is None:
            lines.append(f"  missing     {rel} :: {bid} (markers)")
            missing += 1
            continue
        new, did = blocks.replace(ctx, text, bid, build())
        if did:
            pending[path] = new
            lines.append(f"  regenerated {rel} :: {bid}")
            changed += 1
    for path, text in pending.items():
        write(path, text)
    for line in lines:
        print(line)
    status = "FAIL" if missing else "OK"
    extra = f", {missing} missing" if missing else ""
    print(f"[{status}] index  ({changed} block(s) updated{extra})")
    return 1 if missing else 0


# ---------------------------------------------------------------------------- baseline

def cmd_baseline(ctx: Context, allow_raise: bool) -> int:
    try:
        old = ratchet.load_baseline(ctx)
    except ratchet.BaselineUnreadable as exc:
        print(f"[FAIL] baseline  ({exc}; nothing written — restore it from git)")
        return 1
    current = ratchet.compute(ctx)
    new = dict(old)
    added, raised, lowered, removed, refused = [], [], [], [], []
    for key, val in current.items():
        if key not in old:
            if allow_raise:
                new[key] = val
                added.append(f"{key}={val}")
            else:
                refused.append(f"new entry '{key}'={val} (needs --allow-raise)")
        elif val > old[key]:
            if allow_raise:
                new[key] = val
                raised.append(f"{key}: {old[key]} -> {val}")
            else:
                refused.append(f"'{key}' grew {old[key]} -> {val} (needs --allow-raise)")
        elif val < old[key]:
            new[key] = val
            lowered.append(f"{key}: {old[key]} -> {val}")
    for key, base in old.items():
        if key not in current:
            del new[key]
            removed.append(f"{key} (was {base}, no longer breaches)")
    ratchet.write_baseline(ctx, new)
    for label, rows in (("added", added), ("raised", raised),
                        ("lowered", lowered), ("removed", removed)):
        for row in rows:
            print(f"  {label:8}{row}")
    for row in refused:
        print(f"  refused {row}")
    status = "FAIL" if refused else "OK"
    n = len(new)
    print(f"[{status}] baseline  ({n} entr{'y' if n == 1 else 'ies'} in "
          f"{ctx.rel(ctx.baseline_path)})")
    return 1 if refused else 0


# ---------------------------------------------------------------------------- ids

def _log_scope(ctx: Context, name: str):
    if name == ctx.workspace_label:
        return ctx.registry.workspace, ctx.workspace_log
    scope = ctx.registry.find(name)
    if scope is None or not scope.id_range or scope.gov is None:
        return None, None
    return scope, ctx.scope_log(scope)


def cmd_next_id(ctx: Context, name: str) -> int:
    scope, log = _log_scope(ctx, name)
    if scope is None or not scope.id_range:
        return fail(f"no id range for '{name}' — a project name, or "
                    f"'{ctx.workspace_label}' for the workspace log")
    prefix, (lo, hi) = scope.id_prefix or "W", scope.id_range
    used = ctx.grammar.parse_file(log).used_numbers(prefix) if log.exists() else set()
    if ctx.cfg.dialect["next_id"] == "first-free":
        free = next((n for n in range(lo, hi + 1) if n not in used), None)
    else:
        top = max((n for n in used if lo <= n <= hi), default=lo - 1)
        free = top + 1 if top < hi else None
    if free is None:
        return fail(f"range {lo}-{hi} is exhausted")
    print(f"{prefix}-{free}")
    return 0


def _doc_for(ctx: Context, name: str, prefix: str, num: str | None) -> Path | None:
    """Which governed file owns an id of this prefix. A trap id can live in any of a project's
    trap files, so for traps this looks for the number, falling back to `traps.md`."""
    if prefix == (ctx.registry.workspace_prefix or "W"):
        return ctx.workspace_log
    scope = ctx.registry.find(name)
    if scope is None or scope.gov is None:
        return None
    if prefix == ctx.trap_prefix:
        if num is not None:
            for doc in ctx.trap_files(scope):
                if any(e.num == int(num) for e in ctx.grammar.traps(doc, ctx.trap_prefix)):
                    return doc
        return ctx.working_dir(scope) / "traps.md"
    if prefix == scope.id_prefix:
        return ctx.scope_log(scope)
    return None


def cmd_show(ctx: Context, name: str, ids: list[str], as_list: bool, as_lines: bool) -> int:
    if as_list:
        scope = ctx.registry.find(name)
        if scope is None or scope.gov is None:
            return fail(f"unknown project '{name}'")
        for doc in [ctx.scope_log(scope), *ctx.trap_files(scope)]:
            if not doc.exists():
                continue
            print(f"# {ctx.rel(doc)}")
            for e in ctx.grammar.parse_file(doc).entries:
                print(f"  {e.ident}  {e.title}")
        return 0
    if not ids:
        return fail("give one or more ids, or --list")
    if name != ctx.workspace_label and ctx.registry.find(name) is None:
        return fail(f"unknown project '{name}' — a project name, or "
                    f"'{ctx.workspace_label}' for the workspace log")
    missing = 0
    for raw in ids:
        wanted = raw.strip().upper()
        if "-" not in wanted:
            print(f"error: '{raw}' is not an id like B-219 or T-23", file=sys.stderr)
            missing += 1
            continue
        prefix, num = wanted.split("-", 1)
        if not num.isdigit():
            print(f"error: '{raw}' is not an id like B-219 or T-23", file=sys.stderr)
            missing += 1
            continue
        doc = _doc_for(ctx, name, prefix, num)
        if doc is None or not doc.exists():
            print(f"error: no file owns '{wanted}' for project '{name}'", file=sys.stderr)
            missing += 1
            continue
        hit = next((e for e in ctx.grammar.parse_file(doc).entries
                    if e.prefix == prefix and e.num == int(num)), None)
        if hit is None:
            print(f"error: {wanted} not found in {ctx.rel(doc)}", file=sys.stderr)
            missing += 1
            continue
        if as_lines:
            print(f"{ctx.rel(doc)}:{hit.line}-{hit.last_line}  {wanted}  {hit.title}")
            continue
        print(f"{'#' * ctx.grammar.level} {hit.ident} — {hit.title}")
        print(hit.body.rstrip())
        print()
    return 2 if missing else 0


def cmd_find(ctx: Context, name: str, pattern: str, ids_only: bool, context: int) -> int:
    scope = ctx.registry.find(name)
    if scope is None and name != ctx.workspace_label:
        return fail(f"unknown project '{name}' — a project name, or "
                    f"'{ctx.workspace_label}' for the workspace log")
    try:
        rx = re.compile(pattern, re.I)
    except re.error as exc:
        return fail(f"bad pattern: {exc}")
    docs = [ctx.workspace_log] if ctx.workspace_log is not None else []
    if scope is not None and scope.gov is not None:
        docs += [ctx.scope_log(scope), *ctx.trap_files(scope)]
    hits = 0
    for doc in [d for d in docs if d.exists()]:
        for e in ctx.grammar.parse_file(doc).entries:
            blob = e.title + "\n" + e.body
            if not rx.search(blob):
                continue
            hits += 1
            if ids_only:
                print(e.ident)
                continue
            print(f"{e.ident}  {e.title}")
            shown = 0
            for line in blob.splitlines() if context else []:
                if rx.search(line) and line.strip():
                    print(f"    {line.strip()[:160]}")
                    shown += 1
                    if shown >= context:
                        break
    if not hits:
        print(f"no entry matches /{pattern}/", file=sys.stderr)
        return 1
    return 0


def cmd_trap_add(ctx: Context, name: str, title: str, bites: str, body: str,
                 filename: str) -> int:
    """Append a new trap; `index` writes its summary row. Numbers are monotonic across every
    trap file of the project and never recycled: other docs still cite retired numbers."""
    scope = ctx.registry.find(name)
    if scope is None or scope.gov is None:
        return fail(f"unknown project {name!r}")
    doc = ctx.working_dir(scope) / filename
    if not doc.exists():
        return fail(f"no traps file at {ctx.rel(doc)}")
    prefix = ctx.trap_prefix
    used = set().union(*(ctx.grammar.parse_file(tf).used_numbers(prefix)
                         for tf in ctx.trap_files(scope)))
    for _, _, build in blocks.targets(ctx):
        build()   # raises before anything is written if any index input is unreadable
    ident = f"{prefix}-{(max(used) + 1) if used else 1}"
    original = read(doc)
    text = original.rstrip("\r\n")
    added = f"\n\n{'#' * ctx.grammar.level} {ident} — {title}\n\n**Bites when:** {bites}\n"
    if body.strip():
        added += "\n" + body.strip() + "\n"
    write(doc, text + (added + "\n").replace("\n", eol(original)))
    print(f"{ident} appended to {ctx.rel(doc)}")
    return cmd_index(ctx)


# ---------------------------------------------------------------------------- main

def build_parser(prog: str) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog=prog, description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    c = sub.add_parser("check", help="run the gate")
    c.add_argument("--project", "--repo", dest="project", default=None)
    c.add_argument("--path", default=None, metavar="DIR",
                   help="check this checkout of --project instead of the one the registry "
                        "names, e.g. a git pre-commit hook's staged-tree snapshot")
    c.add_argument("--history-from", dest="history_from", default=None, metavar="DIR",
                   help="date --path's files from this checkout's git history instead, for a "
                        "snapshot that carries none of its own")
    sub.add_parser("index", help="regenerate generated blocks")
    bl = sub.add_parser("baseline", help="rewrite the ratchet baseline (lower/remove only)")
    bl.add_argument("--allow-raise", action="store_true",
                    help="also record new or grown breaches, instead of refusing them")
    n = sub.add_parser("next-id", help="print the next free decision id")
    n.add_argument("--project", "--repo", dest="project", required=True)
    sh = sub.add_parser("show", help="print one decision or trap entry, not the whole file")
    sh.add_argument("--project", "--repo", dest="project", required=True)
    sh.add_argument("ids", nargs="*", help="e.g. B-219 T-23 W-11")
    sh.add_argument("--list", action="store_true", dest="as_list",
                    help="id and title only, for every entry")
    sh.add_argument("--lines", action="store_true", dest="as_lines",
                    help="print file:start-end instead of the body, for a surgical edit")
    ta = sub.add_parser("trap-add", help="append a new trap; its summary row is generated")
    ta.add_argument("--project", "--repo", dest="project", required=True)
    ta.add_argument("--title", required=True)
    ta.add_argument("--bites", required=True, help="the Bites when column")
    ta.add_argument("--body", default="", help="markdown body, e.g. **Tell:** and **Avoid:** lines")
    ta.add_argument("--file", default="traps.md",
                    help="which working-files/traps*.md to append to (default traps.md)")
    sub.add_parser("principles", help="the doctrine this project works by")
    ex = sub.add_parser("explain", help="every effective setting and where it came from")
    ex.add_argument("check", nargs="?", help="one check id; all of them if omitted")
    fd = sub.add_parser("find", help="which entries mention this, without reading the files")
    fd.add_argument("--project", "--repo", dest="project", required=True)
    fd.add_argument("pattern", help="regex, case-insensitive")
    fd.add_argument("--ids-only", action="store_true", dest="ids_only")
    fd.add_argument("--context", type=int, default=2,
                    help="matching lines to show per entry (default 2, 0 for none)")
    return ap


def main(argv: list[str] | None = None, root: Path | None = None,
         prog: str | None = None) -> int:
    if prog is None:
        prog = os.path.basename(sys.argv[0]) or "govern"
        if prog == "__main__.py":
            prog = "govern"
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0].startswith("-") and argv[0] not in ("-h", "--help"):
        argv.insert(0, "check")   # `govern --project X` is a check of X
    args = build_parser(prog).parse_args(argv)
    try:
        ctx = load_context(root, prog)
    except config.ConfigError as exc:
        return fail(str(exc))
    except registry.RegistryMissing as exc:
        return fail(f"{exc} not found")
    try:
        return dispatch(ctx, args)
    except Unreadable as exc:
        return fail(f"{_rel(ctx, exc.path)}: {exc.reason} — nothing was written", 1)
    finally:
        notice.emit(ctx)


def dispatch(ctx: Context, args) -> int:
    if args.cmd == "index":
        return cmd_index(ctx)
    if args.cmd == "baseline":
        return cmd_baseline(ctx, args.allow_raise)
    if args.cmd == "next-id":
        return cmd_next_id(ctx, args.project)
    if args.cmd == "show":
        return cmd_show(ctx, args.project, args.ids, args.as_list, args.as_lines)
    if args.cmd == "trap-add":
        return cmd_trap_add(ctx, args.project, args.title, args.bites, args.body, args.file)
    if args.cmd == "find":
        return cmd_find(ctx, args.project, args.pattern, args.ids_only, args.context)
    if args.cmd == "explain":
        return cmd_explain(ctx, args.check)
    if args.cmd == "principles":
        return cmd_principles(ctx)
    return cmd_check(ctx, getattr(args, "project", None), getattr(args, "path", None),
                     getattr(args, "history_from", None))
