"""A git merge driver for decision logs, trap files and generated blocks: entries merge by id,
so two branches that each add one never conflict, and a generated block is never merged at all
(it takes ours; `govern index` refills it).

    git merge-driver contract: govern merge-driver %O %A %B %P
"""
from __future__ import annotations

import fnmatch
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from govern.context import Context
from govern import blocks, ratchet
from govern.decisions import FENCE, Grammar, id_key, line_kinds, mask_lines

BOTH_ADDED = ("<!-- context-gate: {ident} was added on both branches; keep one and give the "
              "other a new id with govern renumber -->")


class MergeError(Exception):
    """git could not merge: the driver fails closed and leaves ours as it was."""


def _read(path: Path) -> str:
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def _write(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _nl(*texts: str) -> str:
    return "\r\n" if any("\r\n" in t for t in texts) else "\n"


# ---------------------------------------------------------------------------- git merge-file

def merge_file(base: str, ours: str, theirs: str) -> tuple[str, int]:
    """`git merge-file -p` on three texts: the merged text and the number of conflicts."""
    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for name, text in (("ours", ours), ("base", base), ("theirs", theirs)):
            p = Path(tmp) / name
            _write(p, text)
            paths.append(p)
        try:
            res = subprocess.run(
                ["git", "merge-file", "-p", "-L", "ours", "-L", "base", "-L", "theirs",
                 *map(str, paths)], capture_output=True)
        except OSError as exc:
            raise MergeError(f"git merge-file did not run: {exc}") from exc
    if res.returncode < 0 or res.returncode > 127:
        raise MergeError(f"git merge-file failed: {res.stderr.decode('utf-8', 'replace').strip()}")
    return res.stdout.decode("utf-8"), res.returncode


# ---------------------------------------------------------------------------- generated blocks

def _block_re(ctx: Context) -> re.Pattern:
    p = re.escape(ctx.markers.prefix)
    start = rf"<!-- {p}:generated:start id=([\w-]+) -->"
    end = rf"<!-- {p}:generated:end id=\2 -->"
    return re.compile(rf"({start}\r?\n?)(.*?)({end})", re.S)


def _unfenced(ctx: Context, text: str) -> str:
    """`text` with every line inside a code fence blanked to spaces (same length, line ends
    kept), so a marker quoted in a fenced example is not found as a block."""
    lines = text.splitlines(keepends=True)
    kinds = line_kinds([line.rstrip("\r\n") for line in lines], ctx.markers)
    return "".join(re.sub(r"[^\r\n]", " ", line) if kind == FENCE else line
                   for line, kind in zip(lines, kinds))


def _blocks(ctx: Context, text: str) -> list[re.Match]:
    """The generated blocks of `text`, found on the lines outside code fences; the spans are
    those of `text` itself."""
    return list(_block_re(ctx).finditer(_unfenced(ctx, text)))


def empty_blocks(ctx: Context, text: str) -> tuple[str, dict[str, str]]:
    """The text with every generated block's contents dropped, and what each block held (the
    first of an id, should one repeat)."""
    held: dict[str, str] = {}
    out: list[str] = []
    last = 0
    for m in _blocks(ctx, text):
        held.setdefault(m.group(2), text[m.start(3):m.end(3)])
        out.append(text[last:m.end(1)])
        last = m.start(4)
    out.append(text[last:])
    return "".join(out), held


def refill_blocks(ctx: Context, text: str, held: dict[str, str]) -> str:
    out: list[str] = []
    last = 0
    for m in _blocks(ctx, text):
        out.append(text[last:m.end(1)] + held.get(m.group(2), ""))
        last = m.start(4)
    out.append(text[last:])
    return "".join(out)


# ---------------------------------------------------------------------------- entries

def split(text: str, grammar: Grammar) -> tuple[str, dict[str, str], str]:
    """`(head, entries, tail)`: head + the entries in order + tail is the text again."""
    lines = text.splitlines(keepends=True)
    bare = [line.rstrip("\r\n") for line in lines]
    live = mask_lines(bare, grammar.markers)
    starts: list[tuple[int, str]] = []
    for i, line in enumerate(bare):
        m = grammar.entry_re.match(line) if live[i] else None
        if m:
            starts.append((i, f"{m.group(1)}-{m.group(2)}"))
    if not starts:
        return text, {}, ""
    stop = len(lines)
    for j in range(starts[-1][0] + 1, len(lines)):
        if live[j] and grammar.stop_re.match(bare[j]):
            stop = j
            break
    entries: dict[str, str] = {}
    bounds = [i for i, _ in starts] + [stop]
    for (i, ident), end in zip(starts, bounds[1:]):
        entries[ident] = entries.get(ident, "") + "".join(lines[i:end])
    return "".join(lines[:starts[0][0]]), entries, "".join(lines[stop:])


def _bare(entry: str) -> str:
    """An entry without its trailing blank lines and final line end."""
    return re.sub(r"(\r?\n[ \t]*)+\Z", "", entry)


def _line(text: str, nl: str) -> str:
    return text if text.endswith("\n") or not text else text + nl


def _marked(ours: str, theirs: str) -> str:
    nl = _nl(ours, theirs)
    return (f"<<<<<<< ours{nl}{_line(ours, nl)}======={nl}"
            f"{_line(theirs, nl)}>>>>>>> theirs{nl}")


def merge_entries(base: dict[str, str], ours: dict[str, str],
                  theirs: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    merged: dict[str, str] = {}
    conflicts: list[str] = []
    for ident in set(base) | set(ours) | set(theirs):
        o, a, b = base.get(ident), ours.get(ident), theirs.get(ident)
        if a == b:
            result = a
        elif a == o:
            result = b
        elif b == o:
            result = a
        elif o is None:
            nl = _nl(a, b)
            result = BOTH_ADDED.format(ident=ident) + nl + _marked(a, b)
            conflicts.append(ident)
        elif a is None or b is None:
            kept = b if a is None else a
            result = _marked("", kept) if a is None else _marked(kept, "")
            conflicts.append(ident)
        else:
            nl = _nl(o, a, b)
            result, count = merge_file(o + nl, a + nl, b + nl)
            if count:
                conflicts.append(ident)
        if result is not None:
            merged[ident] = result
    return _in_order(merged, list(ours)), sorted(conflicts, key=lambda i: id_key(i) or ("", 0))


def _in_order(merged: dict[str, str], ours: list[str]) -> dict[str, str]:
    """Ours' entries in ours' order; each other entry in its place by id when ours' ids run
    ascending or descending, else at the end."""
    order = [i for i in ours if i in merged]
    keys = [id_key(i) for i in order]
    ascending = descending = False
    if all(k is not None for k in keys):
        ascending = keys == sorted(keys)
        descending = not ascending and keys == sorted(keys, reverse=True)
    for ident in sorted((i for i in merged if i not in order), key=lambda i: id_key(i) or ("", 0)):
        key = id_key(ident)
        at = len(order)
        if key is not None and (ascending or descending):
            for n, other in enumerate(order):
                if (key < id_key(other)) if ascending else (key > id_key(other)):
                    at = n
                    break
        order.insert(at, ident)
    return {i: merged[i] for i in order}


# ---------------------------------------------------------------------------- the baseline

def _merge_keys(o: dict, a: dict, b: dict, join) -> dict:
    """Per key: a side that left it as it was yields to the other; removed against changed keeps
    the changed value; two different values are `join`ed."""
    merged = {}
    for key in set(o) | set(a) | set(b):
        base, ours, theirs = o.get(key), a.get(key), b.get(key)
        if ours == theirs or theirs == base:
            result = ours
        elif ours == base:
            result = theirs
        elif ours is None or theirs is None:
            result = theirs if ours is None else ours
        else:
            result = join(ours, theirs)
        if result is not None:
            merged[key] = result
    return dict(sorted(merged.items()))


def merge_baseline(o: dict[str, int], a: dict[str, int], b: dict[str, int]) -> dict[str, int]:
    """Never a conflict: a number both sides changed takes the larger."""
    return _merge_keys(o, a, b, max)


def merge_reasons(o: dict[str, str], a: dict[str, str], b: dict[str, str]) -> dict[str, str]:
    """Never a conflict: a reason both sides changed keeps both, `ours / theirs`."""
    return _merge_keys(o, a, b, lambda x, y: f"{x} / {y}")


def _parse_reasons(text: str) -> dict[str, str]:
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    return {str(k): str(v) for k, v in data.items()}


def _merge_json_map(ctx: Context, o: Path, a: Path, b: Path, rel: str) -> int | None:
    """The driver's exit code when `rel` is the baseline or its reasons file; None when it is
    neither. A side that does not parse leaves the merge to git's own."""
    real = (Path.cwd() / rel).resolve()
    if real == ctx.baseline_path.resolve():
        parse, merge = ratchet.parse_baseline, merge_baseline
    elif real == ratchet.reasons_path(ctx).resolve():
        parse, merge = _parse_reasons, merge_reasons
    else:
        return None
    try:
        sides = []
        for path in (o, a, b):
            text = _read(path)
            sides.append(parse(text) if text.strip() else {})
    except (OSError, ValueError, TypeError):    # JSON and Unicode decode errors are ValueErrors
        return _plain(o, a, b)
    _write(a, json.dumps(merge(*sides), indent=2, sort_keys=True) + "\n")
    return 0


# ---------------------------------------------------------------------------- the driver

def _is_entry_file(ctx: Context, rel: str) -> bool:
    real = (Path.cwd() / rel).resolve()
    logs = [ctx.workspace_log] if ctx.workspace_log is not None else []
    logs += [ctx.scope_log(s) for s in ctx.registry.scopes]
    if any(real == p.resolve() for p in logs):
        return True
    glob = ctx.projects("trap_glob", "traps*.md")
    return any(real.parent == ctx.working_dir(s).resolve() and fnmatch.fnmatch(real.name, glob)
               for s in ctx.registry.scopes)


def _plain(o: Path, a: Path, b: Path) -> int:
    try:
        res = subprocess.run(["git", "merge-file", str(a), str(o), str(b)], capture_output=True)
    except OSError:
        return 1
    return 0 if res.returncode == 0 else 1


def merge_text(ctx: Context, base: str, ours: str, theirs: str, entry_file: bool) -> tuple[str, bool]:
    base, _ = empty_blocks(ctx, base)
    ours_empty, held = empty_blocks(ctx, ours)
    theirs, _ = empty_blocks(ctx, theirs)
    if not entry_file:
        text, count = merge_file(base, ours_empty, theirs)
        return refill_blocks(ctx, text, held), bool(count)
    parts = [split(t, ctx.grammar) for t in (base, ours_empty, theirs)]
    (bh, be, bt), (oh, oe, ot), (th, te, tt) = parts
    entries, conflicts = merge_entries(*({k: _bare(v) for k, v in e.items()} for e in (be, oe, te)))
    head, hc = merge_file(bh, oh, th)
    tail, tc = merge_file(bt, ot, tt)
    nl = _nl(base, ours, theirs)
    body = (nl * 2).join(_bare(e) for e in entries.values())
    if body:
        body += nl * 2 if tail else (nl if ours.endswith("\n") else "")
    text = head + body + tail
    return refill_blocks(ctx, text, held), bool(conflicts or hc or tc)


def drive(ctx: Context | None, o: Path, a: Path, b: Path, rel: str) -> int:
    """Git's driver contract: merged text into `a`; 0 clean, 1 with conflicts."""
    if ctx is None:
        return _plain(o, a, b)
    done = _merge_json_map(ctx, o, a, b, rel)
    if done is not None:
        return done
    try:
        base, ours, theirs = _read(o), _read(a), _read(b)
    except UnicodeDecodeError:
        return _plain(o, a, b)    # not text we can read by entry: git's own merge decides
    try:
        text, conflicted = merge_text(ctx, base, ours, theirs, _is_entry_file(ctx, rel))
    except MergeError:
        return 1                  # `a` is untouched: the merge stops, git reports a conflict
    _write(a, text)
    return 1 if conflicted else 0


# ---------------------------------------------------------------------------- .gitattributes

ATTR_START = "# context-gate:start"
ATTR_END = "# context-gate:end"
ATTR_DRIVER = "merge=context-gate"


def attributes_paths(ctx: Context) -> list[str]:
    """Every `.gitattributes` pattern, anchored at the governance root, the driver must merge:
    decision logs, trap files (as a pattern, so one added later is covered), generated-block
    files, the baseline and its reasons."""
    found: set[str] = set()
    if ctx.workspace_log is not None:
        found.add(attribute_pattern(ctx.rel(ctx.workspace_log)))
    glob = ctx.projects("trap_glob", "traps*.md")
    for scope in ctx.registry.scopes:
        found.add(attribute_pattern(ctx.rel(ctx.scope_log(scope))))
        found.add(attribute_pattern(ctx.rel(ctx.working_dir(scope)), glob))
    for path, _bid, _build in blocks.targets(ctx):
        found.add(attribute_pattern(ctx.rel(path)))
    found.add(attribute_pattern(ctx.rel(ctx.baseline_path)))
    found.add(attribute_pattern(ctx.rel(ratchet.reasons_path(ctx))))
    return sorted(found)


def attribute_pattern(rel: str, glob: str = "") -> str:
    """The `.gitattributes` pattern for `rel` (and a file-name `glob` inside it, when given):
    anchored with a leading `/`, no `./`, the characters of a path that globbing reads
    escaped, and the whole in git's C-style quoted form when it holds a space or another
    character that would split or garble the line."""
    parts = [p for p in rel.split("/") if p and p != "."]
    path = "/".join(re.sub(r"([\\*?\[])", r"\\\1", p) for p in parts)
    if glob:
        path = f"{path}/{glob}" if path else glob
    pattern = "/" + path
    if not any(c in pattern for c in ' \t\n"\\') and pattern.isprintable():
        return pattern
    escaped = pattern.replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("\t", "\\t").replace("\n", "\\n")
    return f'"{escaped}"'


def attributes_lines(ctx: Context) -> list[str]:
    return [ATTR_START, *(f"{p} {ATTR_DRIVER}" for p in attributes_paths(ctx)), ATTR_END]


def attributes_region(ctx: Context) -> str:
    """The `.gitattributes` region that assigns the driver, one line each, ending in a newline."""
    return "\n".join(attributes_lines(ctx)) + "\n"


def find_region(lines: list[str]) -> tuple[int, int] | None:
    """The index of the start line and of the end line (inclusive) of the region, or None."""
    for i, line in enumerate(lines):
        if line.strip() == ATTR_START:
            for j in range(i + 1, len(lines)):
                if lines[j].strip() == ATTR_END:
                    return i, j
            return None
    return None


def attributes_current(text: str) -> list[str] | None:
    """The region's lines in `text`, or None when it holds none."""
    lines = text.splitlines()
    found = find_region(lines)
    return None if found is None else lines[found[0]:found[1] + 1]


def attributes_with(ctx: Context, text: str) -> str:
    """`text` with the region in place: replaced where it is, else appended. Every other line,
    and the line ending the file uses, stay."""
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    found = find_region(lines)
    new = attributes_lines(ctx)
    if found is None:
        lines = lines + new
    else:
        lines[found[0]:found[1] + 1] = new
    return nl.join(lines) + nl


def attributes_without(text: str) -> str | None:
    """`text` without the region, or None when it holds none."""
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    found = find_region(lines)
    if found is None:
        return None
    del lines[found[0]:found[1] + 1]
    return nl.join(lines) + nl if lines else ""


# ---------------------------------------------------------------------------- wiring a clone

HOOK_MARK = "# context-gate: refresh generated blocks"
HOOKS = ("post-merge", "post-rewrite")


def _git(ctx: Context, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(ctx.root), *args], capture_output=True, text=True)


def _python() -> str:
    """The interpreter running this install, quoted, so git runs the same Python later."""
    return f'"{Path(sys.executable).as_posix()}"'


def _hook_text(bin_path: str) -> str:
    return (f"#!/bin/sh\n{HOOK_MARK}\n"
            f"out=$({_python()} {bin_path} index 2>&1)\n"
            'case "$out" in\n'
            "  *regenerated*)\n"
            '    echo "context-gate: generated blocks refreshed after the merge; commit them" ;;\n'
            "esac\n"
            "exit 0\n")


def install_clone(ctx: Context) -> int:
    """Wire this clone: git's merge driver in its config, and the hooks that refresh generated
    blocks after a merge or a rebase. Each thing done is printed."""
    from govern import layout
    try:
        top = _git(ctx, "rev-parse", "--show-toplevel")
        hooks = _git(ctx, "rev-parse", "--git-path", "hooks")
        path_set = _git(ctx, "config", "--get", "core.hooksPath")
    except OSError as exc:
        print(f"merge-driver install: git did not run ({exc})", file=sys.stderr)
        return 1
    if top.returncode or hooks.returncode:
        print(f"merge-driver install: git cannot say where this clone is: "
              f"{(top.stderr or hooks.stderr).strip()}", file=sys.stderr)
        return 1
    try:
        rel = ctx.root.resolve().relative_to(Path(top.stdout.strip()).resolve())
    except ValueError:
        print("merge-driver install: the governance root is not inside this git repository",
              file=sys.stderr)
        return 1
    bin_path = (rel / layout.ENTRYPOINT).as_posix()
    if " " in bin_path:
        bin_path = f'"{bin_path}"'
    driver = f"{_python()} {bin_path} merge-driver %O %A %B %P"
    for key, value in (("merge.context-gate.name", "context-gate"),
                       ("merge.context-gate.driver", driver)):
        res = _git(ctx, "config", key, value)
        if res.returncode:
            print(f"merge-driver install: git config {key} failed: {res.stderr.strip()}",
                  file=sys.stderr)
            return 1
        print(f"  set          git config {key}")
    hooks_dir = ctx.root / hooks.stdout.strip()
    for name in HOOKS:
        target = hooks_dir / name
        ours = False
        if target.is_file():
            try:
                ours = HOOK_MARK in _read(target).splitlines()
            except (OSError, UnicodeDecodeError):
                ours = False
        if path_set.returncode == 0 and path_set.stdout.strip() or (target.exists() and not ours):
            print(f"  kept         {name}: not context-gate's; add this line to it: "
                  f"python3 {bin_path} index")
            continue
        hooks_dir.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(_hook_text(bin_path))
        target.chmod(0o755)
        print(f"  wrote        {name} hook")
    return 0
