"""`govern renumber OLD NEW`: move a decision or trap entry to another id, and everything that
cites it, when two branches added the same id.

Every file is computed in memory first and written in one all-or-none step, so a file that cannot
be read or written stops the command with nothing changed.
"""
from __future__ import annotations

import re
from pathlib import Path

from govern import build, citations, decisions, refs
from govern.context import Context
from govern.text import has_bom, overlay, read

_TOKEN_RE = re.compile(r"\b([A-Za-z]+)-(\d+)\b")
_POINTER_TAIL_RE = re.compile(r"(Replaced by )[A-Za-z]+-\d+(\s*)$")


def _files(ctx: Context, project: str | None):
    """`[(path, scope, is_trap)]` for the logs and trap files in scope; None for an unknown
    project."""
    from govern import cli
    out = []
    if project is not None:
        scope, log = cli._log_scope(ctx, project)
        if scope is None:
            return None
        out.append((log, scope, False))
        if not scope.is_workspace:
            out.extend((p, scope, True) for p in ctx.trap_files(scope))
        return [t for t in out if t[0].is_file()]
    if ctx.workspace_log is not None:
        out.append((ctx.workspace_log, ctx.registry.workspace, False))
    for scope in ctx.registry.scopes:
        if scope.gov is None:
            continue
        out.append((ctx.scope_log(scope), scope, False))
        out.extend((p, scope, True) for p in ctx.trap_files(scope))
    return [t for t in out if t[0].is_file()]


def _block_end(lines: list[str], entry, markers_prefix: str) -> int:
    """Where an entry's text ends: its last line, or the line a generated block starts on when
    one sits inside the entry's body (the decision index at the end of a log)."""
    start = entry.line - 1
    end = min(entry.last_line, len(lines))
    mark = f"<!-- {markers_prefix}:generated:start"
    for i in range(start + 1, end):
        if lines[i].lstrip().startswith(mark):
            return i
    return end


def _moved(ctx: Context, text: str, key: tuple[str, int]) -> str:
    """`text` with the entry `key` at its sorted place (`text` is the file after the rename, and
    its entries ascended before it); the text as it is when the entry already sits there."""
    entries = ctx.grammar.parse(text).entries
    at = next((i for i, e in enumerate(entries) if e.key == key), None)
    if at is None:
        return text
    mine = entries[at]
    others = entries[:at] + entries[at + 1:]
    target = next((i for i, e in enumerate(others) if e.num > mine.num), len(others))
    if target == at:
        return text
    lines = text.split("\n")
    cr = "\r" if "\r\n" in text else ""
    start, end = mine.line - 1, _block_end(lines, mine, ctx.markers.prefix)
    block = lines[start:end]
    while block and block[-1].strip() == "":
        block.pop()
    rest = lines[:start] + lines[end:]
    left = ctx.grammar.parse("\n".join(rest)).entries
    if target < len(left):
        where = left[target].line - 1
    else:
        where = _block_end(rest, left[-1], ctx.markers.prefix)
    head, tail = rest[:where], rest[where:]
    if head and head[-1].strip() != "":
        head.append(cr)
    if tail and tail[0].strip() != "":
        block = block + [cr]
    return "\n".join(head + block + tail)


def renumber(ctx: Context, old: str, new: str,
             project: str | None) -> tuple[int, list[str], list[str]]:
    """`(exit code, stdout lines, stderr lines)`. Exit 1, nothing written, when `old` is not an
    entry, `new` has another prefix, is outside the range, or is already used, or when a file
    cannot be written. Trap ids are numbered per project but share one prefix, so a citation is
    rewritten only in a file the entry's own project owns; a file no project owns is rewritten
    only when no other project holds an entry with the same id."""
    from govern import cli
    okey, nkey = decisions.id_key(old), decisions.id_key(new)
    files = _files(ctx, project)
    if files is None:
        return 1, [], [f"error: unknown project {project!r}"]
    holders = []
    pointers: dict[Path, list] = {}
    for path, scope, is_trap in files:
        entries = ctx.grammar.parse_file(path).entries
        for e in entries:
            if okey is not None and decisions.id_key(e.ident) == okey:
                holders.append((path, scope, is_trap))
            if e.replaced_by and okey is not None and decisions.id_key(e.replaced_by) == okey:
                pointers.setdefault(path, []).append(e)
    if not holders:
        return 1, [], [f"error: {old} is not an entry"]
    if len(holders) > 1:
        return 1, [], [f"error: {old} is an entry in more than one file; "
                       "name the project with --project"]
    if nkey is None or nkey[0] != okey[0]:
        return 1, [], [f"error: {new} has another prefix than {old}"]
    path, scope, is_trap = holders[0]
    if not is_trap and scope.id_range and not scope.id_range[0] <= nkey[1] <= scope.id_range[1]:
        lo, hi = scope.id_range
        return 1, [], [f"error: {new} is outside {lo}-{hi}"]
    if is_trap:
        used, warns = refs.used_numbers(
            ctx, ctx.trap_files(scope), ctx.trap_prefix,
            also=(ctx.working_dir(scope), ctx.projects("trap_glob", "traps*.md")))
    else:
        used, warns = refs.used_numbers(ctx, [path], okey[0])
    warnings = [f"warning: {w}" for w in warns]
    if nkey[1] in used:
        return 1, [], warnings + [f"error: {new} is already used"]
    others = any(s.name != scope.name and decisions.id_key(e.ident) == okey
                 for other, s, _ in _files(ctx, None)
                 for e in ctx.grammar.parse_file(other).entries)

    def swap_token(line: str) -> str:
        def one(m: re.Match) -> str:
            return new if decisions.id_key(m.group(0)) == okey else m.group(0)
        return _TOKEN_RE.sub(one, line)

    texts: dict[Path, str] = {}

    def text_of(p: Path) -> str:
        if p not in texts:
            texts[p] = read(p)
        return texts[p]

    new_lines: dict[Path, list[str]] = {}

    def lines_of(p: Path) -> list[str]:
        if p not in new_lines:
            new_lines[p] = text_of(p).split("\n")
        return new_lines[p]

    globs = ctx.cfg.checks["decision-citations"].params["code_globs"]
    original = ctx.grammar.parse_file(path).entries
    nums = [e.num for e in original]
    if is_trap:
        ascending = nums == sorted(nums)
    else:
        ascending = bool(ctx.cfg.checks["decision-log"].params["ascending"])
    for e in original:
        if decisions.id_key(e.ident) == okey:
            ls = lines_of(path)
            ls[e.line - 1] = swap_token(ls[e.line - 1])
    for p, entries in pointers.items():
        ls = lines_of(p)
        for e in entries:
            i = e.line - 1
            ls[i] = _POINTER_TAIL_RE.sub(lambda m: m.group(1) + new + m.group(2), ls[i].rstrip("\r")) \
                + ("\r" if ls[i].endswith("\r") else "")
    left: list[str] = []
    for c in citations.scan(ctx, globs):
        if decisions.id_key(c.ident) != okey:
            continue
        if others:
            owner = ctx.owner(c.path)
            if owner.name != scope.name:
                if owner.is_workspace:
                    left.append(f"  left         {ctx.rel(c.path)}:{c.line}: {c.ident} "
                                "may be another project's")
                continue
        ls = lines_of(c.path)
        ls[c.line - 1] = swap_token(ls[c.line - 1])
    if path not in new_lines:
        lines_of(path)
    final = {p: "\n".join(ls) for p, ls in new_lines.items()}
    if ascending:
        final[path] = _moved(ctx, final[path], nkey)
    changed = sorted((p for p in final if final[p] != texts[p]), key=ctx.rel)
    # Every index builder runs on the new texts before anything is written, so an unreadable
    # input stops the command first, and the regenerated blocks go out in the same write.
    with overlay({p: final[p] for p in changed}):
        pending, _, _, _ = cli.render_index(ctx)
    texts_out = {p: final[p] for p in changed} | pending
    files_out = [(p, (("\ufeff" if has_bom(p) else "") + texts_out[p]).encode("utf-8"))
                 for p in sorted(texts_out, key=ctx.rel)]
    try:
        build.write_all(files_out)
    except OSError as exc:
        where = ctx.rel(Path(str(exc.filename))) if exc.filename else "a file"
        return 1, [], warnings + [f"error: {where}: {exc.strerror or exc}; nothing was written"]
    out = [f"  renumbered {ctx.rel(p)}" for p in changed] + left + [f"{old} is now {new}"]
    return cli.cmd_index(ctx), out, warnings
