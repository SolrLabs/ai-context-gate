"""Adopt the engine in a project that has never had it: measure it, propose
its config, and, once every question is answered, install it green in one step.

    python3 -P -m govern.installer adopt --root DIR [--source S|none] [--marketplace OWNER/REPO]
            [--profile P] [--answers FILE] [--apply] [--json]

`[governance] source`, where `bin/govern` fetches a release it does not have, is the public
repository unless `--source` names another; with `--marketplace OWNER/REPO` (a fork) and no
`--source`, it is that fork's GitHub URL, and `--source none` leaves it unset. The plugin is
opted in as the public plugin id, with its marketplace (the public repository, or the
`--marketplace` fork) beside it in `.claude/settings.json`, so a teammate's Claude Code offers it.

Without `--apply` it prints the proposal (or, with `--json`, the proposal and the measurement
behind it) and exits 3 while questions remain, else 0. `--answers` is a TOML file of `key =
"option"`, keyed by `Question.key`.

With `--apply`, once no question is open and no file it would touch has uncommitted changes:
validate the config, write the registry file a workspace without one needs, install (with the
plugin opt-in), install the engine adopt is
running as its version under the user's engines directory when it is a release (stamped
`govern/RELEASE`, as the plugin's bundled copy is) and that version is not there, so the gate
runs with no fetch; a development tree is never installed, and the gate fetches from
`[governance] source` instead. Then create the decision logs
the proposal lists, run `migrate --apply` when measure found old formats, regenerate every
block, baseline what migrate made newly visible (adding keys only, never raising one), run the
gate, and write
`.context-gate/adopt-report.md`. It exits 0 when the gate is green. It never commits: the
report ends with the files to review and commit. When existing content outside the ratchet
keeps the gate red (an entry's missing field, an agent's frontmatter), adopt exits 1 and the
report lists those findings first, under "Needs a person before this is green".

A failure before the gate runs (install refused, migrate refused, or the engine not installable
with no `[governance] source` to fetch it from) undoes everything adopt wrote: the install is
reversed, and the logs and registry file it created are removed.
"""
from __future__ import annotations

import contextlib
import io
import json
import re
import sys
import tempfile
import tomllib
from dataclasses import asdict
from datetime import date
from pathlib import Path

from govern import __version__, cli, config, installer, layout, measure, migrate, propose, ratchet
from govern import registry, tomlw
from govern.text import Unreadable, write

PLUGIN = layout.PLUGIN_ID
REPORT = f"{layout.GOV_DIR}/adopt-report.md"
OPEN = 3                      # exit code: the proposal still has questions
REGENERATED_RE = re.compile(r"^\s*regenerated (.+?) :: ", re.M)   # `cmd_index`'s own lines


class AdoptError(Exception):
    pass


def fail(msg: str, code: int = 2) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return code


def load_answers(path: Path) -> dict[str, str]:
    try:
        data = tomllib.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise AdoptError(f"--answers {path}: not readable TOML ({exc})") from exc
    bad = [k for k, v in data.items() if not isinstance(v, str)]
    if bad:
        raise AdoptError(f"--answers {path}: {', '.join(bad)} must be a string (key = \"option\")")
    return data


def plan(root: Path, source: str | None, profile: str | None, answers: dict[str, str]
         ) -> tuple[measure.Measurement, propose.Proposal]:
    """Measure and propose. A workspace with no registry is proposed twice: once to choose the
    repos, then again with them measured as members of the registry adopt will write."""
    m = measure.measure(root)
    p = propose.propose(m, source, profile, answers)
    if p.registry_file is not None and m.registry is None \
            and not (root / propose.REGISTRY_FILE).exists():
        m = measure.measure(root, planned=(propose.REGISTRY_FILE, p.registry_file))
        p = propose.propose(m, source, profile, answers)
    return m, p


# ---------------------------------------------------------------------------- the proposal

def source_for(source: str | None, marketplace: str | None) -> str | None:
    """`[governance] source`: as given, none for `--source none`, else the `--marketplace`
    fork's GitHub URL, else the public repository's."""
    if source == installer.SOURCE_NONE:
        return None
    if source:
        return source
    if marketplace:
        return f"https://github.com/{marketplace}.git"
    return layout.PUBLIC_SOURCE


def summary(p: propose.Proposal, marketplace: str | None = None) -> str:
    out = ["# config.toml", "", tomlw.dumps(p.config).rstrip(), ""]
    if p.registry_file is not None:
        out += [f"# {propose.REGISTRY_FILE} (written by adopt)", "",
                tomlw.dumps(p.registry_file).rstrip(), ""]
    out.append(f"plugin: {PLUGIN} (marketplace {marketplace or layout.PUBLIC_REPO})")
    out.append(f"create: {', '.join(p.create) or 'nothing'}")
    out.append(f"migrate: {'yes' if p.migrate else 'no'}")
    for n in p.notes:
        out.append(f"note: {n}")
    if p.questions:
        out += ["", f"{len(p.questions)} question(s) open (answer with --answers FILE):"]
        for q in p.questions:
            out.append(f"  {q.key}: {q.text}")
            out.append(f"    options: {' | '.join(q.options)} (the first is recommended)")
            out.append(f"    why: {q.why}")
    return "\n".join(out)


# ---------------------------------------------------------------------------- what adopt touches

def _scope_dirs(m: measure.Measurement, cfg: dict) -> list[str]:
    if "registry" not in cfg:
        return ["."]
    skip = cfg["registry"].get("skip", [])
    return [s.dir for s in m.scopes if s.name not in skip]


def touched(root: Path, m: measure.Measurement, p: propose.Proposal) -> list[Path]:
    """Every existing file adopt, migrate or index would write: the install's own, each decision
    log, trap file and block target the config names, and the plugin setting."""
    cfg = p.config
    projects, blocks = cfg.get("projects", {}), cfg.get("blocks", {})
    rels: list[str] = [installer.SETTINGS]
    if "decision_log" in cfg.get("workspace", {}):
        rels.append(cfg["workspace"]["decision_log"])
    rels += [b["file"] for b in blocks.get("workspace", [])]
    paths = [root / r for r in rels]
    dirs = _scope_dirs(m, cfg)
    for s in [m.workspace, *m.scopes]:
        if s.dir in dirs:
            paths += [root / f for ts in s.trap_sets for f in ts.files]
    for d in dirs:
        base = root / d
        paths.append(base / projects.get("decision_log", propose.DEFAULT_LOG))
        for b in blocks.get("project", []):
            if "file" in b:
                paths.append(base / b["file"])
            for pattern in (b.get("glob"), b.get("sources")):
                if pattern:
                    paths += sorted(base.glob(pattern))
    seen: dict[str, Path] = {}
    for path in paths:
        if path.is_file():
            seen.setdefault(path.resolve().as_posix(), path)
    return list(seen.values())


# ---------------------------------------------------------------------------- apply

def _create_log(ctx, path: Path) -> list[str]:
    """A new decision log: frontmatter the doc check accepts and an empty decision-index pair
    in the configured markers, for `index` to fill. Returns the frontmatter keys the resolved
    config requires that adopt cannot fill in."""
    params = ctx.cfg.checks["doc-frontmatter"].params
    pick = lambda want, allowed: want if want in allowed or not allowed else allowed[0]  # noqa: E731
    fm = {"doc_type": pick("reference", params["doc_types"]),
          "purpose": "The decision log: one entry per decision.",
          "audience": pick("both", params["audiences"]),
          "load_when": "making or revisiting a decision",
          "last_reviewed": date.today().isoformat()}
    missing = [k for k in params["required_keys"] if k not in fm]
    lines = ["---", *(f"{k}: {v}" for k, v in fm.items()), "---", "", "# Decisions", "",
             "## Index", "", ctx.markers.open("decision-index"),
             ctx.markers.close("decision-index"), ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    write(path, "\n".join(lines))
    return missing


def _captured(fn, *args) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        try:
            code = fn(*args)
        except Unreadable as exc:
            print(f"error: {exc.path}: {exc.reason}")
            code = 2
    return code, out.getvalue()


def _undo(root: Path, created: list[Path]) -> None:
    if (root / layout.MANIFEST).is_file():
        with contextlib.redirect_stdout(io.StringIO()):
            installer.uninstall(root, force=True)
    for path in created:
        path.unlink(missing_ok=True)


def apply(root: Path, m: measure.Measurement, p: propose.Proposal,
          marketplace: str | None = None) -> int:
    if p.questions:
        return fail(f"{len(p.questions)} question(s) still open "
                    f"({', '.join(q.key for q in p.questions)}); answer them with --answers")
    reg_path = None
    if p.registry_file is not None:
        reg_path = root / p.config["registry"]["file"]
        if reg_path.exists():
            return fail(f"{reg_path.name} exists and is not a registry measure recognised; "
                        f"adopt will not overwrite it")
    blockers = migrate._apply_blockers(touched(root, m, p))
    if blockers:
        for msg in blockers:
            print(f"error: {msg}", file=sys.stderr)
        return fail("nothing was changed: commit or stash these files first, so adopt's "
                    "changes can be reviewed on their own")
    try:
        propose.validate(p.config, m, layout.home(), p.registry_file)
    except (config.ConfigError, registry.RegistryMissing) as exc:
        return fail(f"the proposed config does not load ({exc}); nothing was changed")

    created: list[Path] = []
    if reg_path is not None:
        write(reg_path, tomlw.dumps(p.registry_file, header="The projects this workspace "
                                    f"governs, written by {layout.DISPLAY_NAME} adopt."))
        created.append(reg_path)
    with tempfile.TemporaryDirectory() as tmp:
        cfg_file = Path(tmp) / "config.toml"
        write(cfg_file, tomlw.dumps(p.config))
        code = installer.install(root, cfg_file, [], None, [], True, PLUGIN, marketplace)
    if code:
        _undo(root, created)
        return code
    try:
        note = installer.install_running_engine()
    except installer.EngineNotInstalled as exc:
        if p.config["governance"].get("source") is None:
            _undo(root, created)
            return fail(f"could not install engine {__version__} in {layout.engines_dir()} "
                        f"({exc}), and no [governance] source says where the gate could fetch "
                        f"it; adopt was undone")
        note = (f"{__version__} not installed ({exc}); the gate fetches it from "
                f"[governance] source")
    if note:
        print(f"  engine       {note}")

    ctx = installer._load_context(root)
    unfilled: dict[str, list[str]] = {}
    for rel in p.create:
        path = root / rel
        if not path.exists():
            unfilled[rel] = _create_log(ctx, path)
            created.append(path)
            print(f"  created      {rel}")

    migrated = None
    if p.migrate:
        mp = migrate.plan(ctx)
        code, out = _captured(migrate.run, root, True)
        print(out, end="")
        if code:
            _undo(root, created)
            return fail("migrate refused, so adopt was undone; nothing was changed")
        migrated = mp

    ctx = installer._load_context(root)
    _, index_out = _captured(cli.cmd_index, ctx)
    print(index_out, end="")
    ctx = installer._load_context(root)
    installer._baseline_new(ctx)
    try:
        now = ratchet.load_baseline(ctx)
    except ratchet.BaselineUnreadable:
        now = {}
    added = [f"{k}={v}" for k, v in sorted(now.items())]
    check_code, check_out = _captured(cli.cmd_check, ctx, None)
    print(check_out, end="")
    try:
        red = needs_a_person(ctx) if check_code else []
    except Unreadable:
        red = []                  # the gate's own output above already names the file

    report = root / REPORT
    write(report, _report(ctx, m, p, migrated, unfilled, added, index_out, check_code,
                          check_out, created, red))
    if check_code:
        print(f"  report       {REPORT}: gate red — {len(red)} finding(s) in content adopt "
              f"cannot baseline or fix need a person first; they are listed at the top")
        return 1
    print(f"  report       {REPORT}: gate green")
    return 0


FIX_RE = re.compile(r"\s+[—–]\s+(.*)$")


def needs_a_person(ctx) -> list[tuple[str, str, str, str]]:
    """The errors keeping the gate red once adopt has done all it can: the ratchet's are
    baselined, so each of these is existing content outside it (an entry's missing field, an
    agent's frontmatter, a registry value). Each is (check, file, finding, fix): the file is
    the finding's own leading path, and the fix the clause it ends with after a dash, if any."""
    out = []
    for _, cid, found in cli.collect_scoped(ctx, ctx.registry.scopes, workspace=True):
        for msg in found.errors:
            where, _, rest = msg.partition(": ")
            if not rest:
                where, rest = "", msg
            fix = FIX_RE.search(rest)
            out.append((cid, where, rest[:fix.start()] if fix else rest,
                        fix.group(1) if fix else ""))
    return out


# ---------------------------------------------------------------------------- report

def _report(ctx, m, p, migrated, unfilled, added, index_out, check_code, check_out,
            created, red) -> str:
    L = ["# Adopt report", "",
         f"Adopted with {layout.DISPLAY_NAME} {ctx.cfg.raw['governance']['engine']} on "
         f"{date.today().isoformat()}. The gate is **{'green' if check_code == 0 else 'red'}**.",
         "", "Nothing was committed. Review the files below, then commit them.", ""]

    if red:
        L += ["## Needs a person before this is green", "",
              "Existing content the ratchet does not cover, so adopt neither baselines nor "
              f"fixes it. Fix each, then run `python3 {layout.ENTRYPOINT} check`.",
              ""]
        for cid, where, finding, fix in red:
            L.append(f"- {f'`{where}`: ' if where else ''}{finding} (`{cid}`)"
                     + (f". Fix: {fix}" if fix else ""))
        L.append("")

    L += ["## Measured", ""]
    L.append(f"- Registry: {m.registry or 'none'}"
             + (f" (`[[{m.registry_entries}]]`)" if m.registry_entries else ""))
    L.append(f"- Scopes: {', '.join(f'{s.name} (`{s.dir}`)' for s in m.scopes) or 'the root'}")
    if m.subrepos:
        L.append(f"- Nested checkouts: {', '.join(f'`{s}`' for s in m.subrepos)}")
    L.append(f"- Markers: `{m.markers or 'none'}`")
    L.append("")

    L += ["## Chosen", "", "```toml", tomlw.dumps(p.config).rstrip(), "```", ""]
    if p.registry_file is not None:
        L += [f"`{propose.REGISTRY_FILE}`, written by adopt:", "", "```toml",
              tomlw.dumps(p.registry_file).rstrip(), "```", ""]
    if p.notes:
        L += ["Notes:", ""] + [f"- {n}" for n in p.notes] + [""]

    if p.create:
        L += ["## Created", ""]
        for rel in p.create:
            gaps = unfilled.get(rel, [])
            L.append(f"- `{rel}`: a decision log with an empty index"
                     + (f"; fill in its frontmatter {', '.join(gaps)}" if gaps else ""))
        L.append("")

    if migrated is not None:
        L += ["## Migrated", "", f"Details in `{layout.GOV_DIR}/migration-report.md`.", ""]
        L += [f"- `{ctx.rel(c.path)}`: {c.detail}" for c in migrated.changes] or ["- nothing"]
        L.append("")
        if migrated.problems:
            L += ["Left for a person:", ""]
            L += [f"- `{ctx.rel(q.path)}{f':{q.line}' if q.line else ''}`: {q.reason}"
                  for q in migrated.problems]
            L.append("")

    L += ["## Baseline", ""]
    L += ([f"- `{k}`" for k in added] if added else ["No breach needed recording."]) + [""]

    L += ["## Gate", "", "```", (index_out + check_out).rstrip(), "```", ""]

    files = sorted({*(ctx.rel(c) for c in created), layout.GOV_DIR + "/",
                    installer.SETTINGS,
                    *(ctx.rel(c.path) for c in (migrated.changes if migrated else [])),
                    *REGENERATED_RE.findall(index_out)})
    L += ["## To review and commit", "",
          "Each file below is new, changed or removed by adopt. A file in a nested checkout is "
          "committed in that checkout.", ""]
    L += [f"- `{f}`" for f in files] + [""]
    return "\n".join(L)


# ---------------------------------------------------------------------------- entry point

def run(root: Path, source: str | None, profile: str | None, answers_file: Path | None,
        apply_: bool, as_json: bool, marketplace: str | None = None) -> int:
    if not root.is_dir():
        return fail(f"{root} is not a directory")
    try:
        installer.marketplace_for(PLUGIN, marketplace)
    except ValueError as exc:
        return fail(f"--marketplace: {exc}")
    source = source_for(source, marketplace)
    if (root / layout.GOV_DIR).exists():
        return fail(f"{root / layout.GOV_DIR} already exists: this project has adopted; "
                    f"use upgrade, or uninstall first")
    try:
        answers = load_answers(answers_file) if answers_file else {}
        m, p = plan(root, source, profile, answers)
    except (AdoptError, measure.MeasureError) as exc:
        return fail(str(exc))
    if apply_:
        return apply(root, m, p, marketplace)
    if as_json:
        data = asdict(p)
        data["measurement"] = asdict(m)
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print(summary(p, marketplace))
    return OPEN if p.questions else 0
