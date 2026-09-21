"""Measure a project before it adopts the engine: where its registry, decision
logs, trap files, working files and docs are, read from the tree itself.

    python3 -P -m govern.installer measure --root DIR [--json]

Read-only, and needs no config: nothing is installed yet. `propose` turns a `Measurement` into a
config and the questions measurement cannot settle. Every path is repo-relative `as_posix()`.

Never descended into: `.git/`, `backup/`, the trees in `DEFAULT_DOC_EXCLUDES` (which include
`.context-gate/` and `.claude/`, so an agent's worktree there is never a nested checkout), and
anything git ignores, asked of the repo that contains it (a registry member's checkout is its own
repo, so a scope dir is never skipped because the workspace root ignores it). Of a registry
member's checkout only its scope dir is measured, never its code.

A git call that fails (git missing, `safe.directory`, a lock) raises `MeasureError` rather than
reading every file as not ignored.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import sys
import tomllib
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

from govern import layout
from govern.config import REGISTRY_KEYS
from govern.context import DEFAULT_DOC_EXCLUDES, git, repo_of
from govern.decisions import mask_lines
from govern.migrate import BULLET_RE, INDEX_HEADING_RE, INDEX_ITEM_RE
from govern.registry import dig
from govern.text import Markers, parse_frontmatter, read_text


@dataclass
class DecisionLog:
    path: str
    entries: int              # headings matching the entry rule
    level: int                # 2 or 3; 3 means migrate will flatten sections
    prefixes: list[str]       # most common first
    named: bool               # file is DECISIONS.md (any case) or holds a decision-index pair
    lowest: int | None = None  # lowest entry number under the most common prefix


@dataclass
class TrapSet:
    files: list[str]          # the index file first: the one with `## Index` or a trap-index pair
    index_spans_files: bool   # the index lists traps whose bodies are in other files
    bullets: bool             # old `- **N. Title.**` format; migrate converts
    glob: str                 # narrowest glob, relative to the working dir, matching every file
    has_index: bool = False   # files[0] holds `## Index` or a trap-index pair


@dataclass
class ScopeMeasure:
    name: str
    dir: str                  # registry: entry's `governance` if set, else `dir`; single: "."
    decision_logs: list[DecisionLog]  # candidates, ranked: named first, then by entries
    trap_sets: list[TrapSet]
    working_dir: str | None   # relative to dir: the dir holding HANDOFF.md, else the shallowest `working-files` under dir
    doc_dirs: dict[str, tuple[int, int]]  # top dir under `dir` -> (markdown files, with frontmatter holding `doc_type`); dot-dirs never listed
    doc_files: dict[str, list[str]] = field(default_factory=dict)  # a mixed doc_dirs dir -> its files whose frontmatter holds `doc_type`


@dataclass
class Measurement:
    root: str
    registry: str | None      # e.g. "projects.toml"
    registry_entries: str | None      # e.g. "project", "repo"
    registry_keys: dict[str, str]     # engine key -> where entries hold it, e.g. "handoff": "profile.handoff"
    governance_key_missing: bool      # no entry has a `governance` key
    workspace: ScopeMeasure           # the root as the workspace scope
    scopes: list[ScopeMeasure]        # governed registry entries; [] for a single repo
    markers: str | None               # prefix of existing `<!-- X:generated:start id=… -->` pairs, most common
    blocks: list[tuple[str, str]]     # (file, id) marker pairs found
    max_ids: dict[str, int]           # prefix -> highest number seen in any heading anywhere
    notes: list[str]                  # e.g. traps kept as a section of another file
    subrepos: list[str] = field(default_factory=list)  # nested git checkouts under the root, not excluded, named by a registry or not


class MeasureError(Exception):
    """Measurement cannot be trusted: a git call failed, so what git ignores is unknown."""


# A decision entry: a hyphenated id at level 2 or 3, then a dash.
ENTRY_RE = re.compile(r"^(#{2,3}) ([A-Z]+)-(\d+)\s*[—–-] ")
# Any heading that opens with an id, hyphenated or not (`### D7` counts toward `D`).
HEADING_ID_RE = re.compile(r"^#{1,6}\s+([A-Z]+)-?(\d+)\b")
# A trap body under a heading (`## T-4 — Title`); bullet bodies are migrate's BULLET_RE.
TRAP_HEADING_RE = re.compile(r"^#{2,3}\s+[A-Za-z]+-(\d+)\b")
# A row of a generated trap-index table: `| T-4 | ...`.
TRAP_ROW_RE = re.compile(r"^\|\s*[A-Za-z]+-(\d+)\s*\|")
MARKER_RE = re.compile(r"^\s*<!-- ([\w-]+):generated:(start|end) id=([\w-]+) -->\s*$")
TRAPS_SECTION_RE = re.compile(r"^#{2,6}\s+Traps\s*$", re.I)

NEVER = frozenset({layout.GOV_DIR, ".git", "backup"}
                  | {p.split("/", 1)[0] for p in DEFAULT_DOC_EXCLUDES})
# A marker set that matches nothing: mask_lines is asked only about fences and comments here,
# since the real generated-block markers are found (and blanked) before it runs.
_NO_MARKERS = Markers("\x00")


# ---------------------------------------------------------------------------- one file

@dataclass
class _Pair:
    prefix: str
    bid: str
    start: int                # line index of the start marker
    end: int                  # line index of the end marker


@dataclass
class _Doc:
    path: Path
    rel: str                  # root-relative, posix
    lines: list[str]
    live: list[bool]          # not frontmatter, fenced, commented or generated
    pairs: list[_Pair]
    doc_type: bool            # frontmatter holds `doc_type`

    def live_lines(self):
        return ((i, line) for i, line in enumerate(self.lines) if self.live[i])

    def has_pair(self, bid: str) -> bool:
        return any(p.bid == bid for p in self.pairs)


def _scan(lines: list[str]) -> tuple[list[bool], list[_Pair]]:
    """Which lines are live markdown, and the generated-block pairs, whatever prefix they use.

    Marker lines are blanked before `mask_lines` runs, so a marker inside a fence or an HTML
    comment is seen as masked and never counts; a live start marker then masks every line up
    to its own end marker."""
    blanked = ["" if MARKER_RE.match(line) else line for line in lines]
    live = mask_lines(blanked, _NO_MARKERS)
    if lines and lines[0].strip() == "---":
        close = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), -1)
        for i in range(close + 1):
            live[i] = False
    pairs: list[_Pair] = []
    open_: tuple[str, str, int] | None = None
    for i, line in enumerate(lines):
        m = MARKER_RE.match(line)
        if m and live[i]:
            live[i] = False
            prefix, kind, bid = m.groups()
            if kind == "start" and open_ is None:
                open_ = (prefix, bid, i)
            elif kind == "end" and open_ is not None and open_[:2] == (prefix, bid):
                pairs.append(_Pair(prefix, bid, open_[2], i))
                open_ = None
        elif open_ is not None:
            live[i] = False
    return live, pairs


def _load(path: Path, root: Path, notes: list[str]) -> _Doc | None:
    rel = path.relative_to(root).as_posix()
    got = read_text(path)
    if got.error:
        notes.append(f"{rel}: {got.error}; not measured")
        return None
    lines = got.text.replace("\r\n", "\n").split("\n")
    live, pairs = _scan(lines)
    fm, _ = parse_frontmatter(got.text)
    return _Doc(path, rel, lines, live, pairs, bool(fm.get("doc_type")))


# ---------------------------------------------------------------------------- walking

class _Ignores:
    """What git ignores, asked of the repo that contains each path (a nested checkout is its
    own repo, so its files are asked of it, and the checkout itself of the repo around it).
    One `git ls-files` per repo, cached."""

    def __init__(self) -> None:
        self._cache: dict[Path, frozenset[str]] = {}

    def _ignored(self, repo: Path) -> frozenset[str]:
        if repo not in self._cache:
            out = git(repo, "ls-files", "-z", "--others", "--ignored", "--exclude-standard",
                      "--directory")
            if out is None:
                raise MeasureError(f"`git ls-files` failed in {repo} (git missing, "
                                   f"safe.directory, or a lock): cannot tell what it ignores")
            self._cache[repo] = frozenset(p for p in out.split("\0") if p)
        return self._cache[repo]

    def __call__(self, path: Path, is_dir: bool) -> bool:
        repo = repo_of(path)
        if repo is None:
            return False
        rel = path.relative_to(repo).as_posix()
        return (rel + "/" if is_dir else rel) in self._ignored(repo)


@dataclass
class _Tree:
    files: list[Path] = field(default_factory=list)
    dirs: list[Path] = field(default_factory=list)


def _walk(base: Path, skip: set[Path], ignored: _Ignores) -> _Tree:
    """Every file and dir under `base`, pruning the never-descend trees, `skip` (other scopes'
    and members' dirs) and whatever git ignores. `base` itself is never pruned."""
    tree = _Tree()
    for current, dirnames, filenames in os.walk(base):
        here = Path(current)
        keep = []
        for name in sorted(dirnames):
            sub = here / name
            if name in NEVER or sub in skip or ignored(sub, True):
                continue
            keep.append(name)
            tree.dirs.append(sub)
        dirnames[:] = keep
        for name in sorted(filenames):
            f = here / name
            if not ignored(f, False):
                tree.files.append(f)
    return tree


# ---------------------------------------------------------------------------- registry

@dataclass
class _Registry:
    file: str
    entries_key: str
    entries: list[dict]
    keys: dict[str, str]      # only the keys held somewhere other than the top level


def _find_registry(root: Path, notes: list[str]) -> _Registry | None:
    """A root `*.toml` holding one array of tables whose entries carry `dir` or `governance`,
    at least one of which names a dir that exists. Two such files, or two such arrays in one
    file, are ambiguous: none is chosen and a note says so."""
    found: list[tuple[str, str, list[dict]]] = []
    for path in sorted(root.glob("*.toml")):
        try:
            data = tomllib.loads(path.read_bytes().decode("utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            continue
        arrays = []
        for key, value in data.items():
            if not (isinstance(value, list) and value and all(isinstance(e, dict) for e in value)):
                continue
            if not any("dir" in e or "governance" in e for e in value):
                continue
            named = [e.get(k) for e in value for k in ("dir", "governance")]
            if any(isinstance(d, str) and d and (root / d).is_dir() for d in named):
                arrays.append((key, value))
        if len(arrays) == 1:
            found.append((path.name, *arrays[0]))
        elif len(arrays) > 1:
            notes.append(f"{path.name}: several arrays of tables look like a registry "
                         f"({', '.join(k for k, _ in arrays)}); none chosen")
    if len(found) > 1:
        notes.append(f"several files look like a registry ({', '.join(f for f, _, _ in found)});"
                     f" none chosen")
        return None
    if not found:
        return None
    return _registry(*found[0])


def _registry(name: str, key: str, entries: list[dict]) -> _Registry:
    keys: dict[str, str] = {}
    for k in REGISTRY_KEYS:
        if any(k in e for e in entries):
            continue
        subs = sorted({sub for e in entries for sub, table in e.items()
                       if isinstance(table, dict) and k in table})
        if len(subs) == 1:
            keys[k] = f"{subs[0]}.{k}"
    return _Registry(name, key, entries, keys)


def _norm(root: Path, rel: str) -> Path:
    return Path(os.path.normpath(root / rel))


# ---------------------------------------------------------------------------- one scope

def _is_trap_file(path: Path) -> bool:
    return fnmatch.fnmatchcase(path.name.lower(), "*traps*.md")


def _under(path: Path, d: Path) -> bool:
    return path == d or d in path.parents


def _working_dir(base: Path, docs: list[_Doc], tree: _Tree) -> Path | None:
    handoffs = sorted((d.path.parent for d in docs if d.path.name.lower() == "handoff.md"),
                      key=lambda p: (len(p.parts), p.as_posix()))
    if handoffs:
        return handoffs[0]
    working = sorted((d for d in tree.dirs if d.name == "working-files"),
                     key=lambda p: (len(p.parts), p.as_posix()))
    return working[0] if working else None


def _decision_log(doc: _Doc) -> DecisionLog | None:
    levels: Counter[int] = Counter()
    prefixes: Counter[str] = Counter()
    lows: dict[str, int] = {}
    for _, line in doc.live_lines():
        m = ENTRY_RE.match(line)
        if m:
            levels[len(m.group(1))] += 1
            prefixes[m.group(2)] += 1
            lows[m.group(2)] = min(lows.get(m.group(2), int(m.group(3))), int(m.group(3)))
    named = doc.path.name.lower() == "decisions.md" or doc.has_pair("decision-index")
    entries = sum(levels.values())
    if not entries and not named:
        return None
    return DecisionLog(path=doc.rel, entries=entries, level=3 if levels[3] > levels[2] else 2,
                       prefixes=[p for p, _ in prefixes.most_common()], named=named,
                       lowest=lows[prefixes.most_common(1)[0][0]] if prefixes else None)


def _index_numbers(doc: _Doc) -> set[int] | None:
    """The trap numbers a file's index names, or `None` when it has no index: a hand-written
    `## Index` list, or the rows of a generated trap-index block."""
    nums: set[int] | None = None
    for i, line in doc.live_lines():
        hm = INDEX_HEADING_RE.match(line.strip())
        if not hm:
            continue
        nums = set()
        stop = re.compile(rf"^#{{1,{len(hm.group(1))}}}\s")
        for j in range(i + 1, len(doc.lines)):
            if not doc.live[j]:
                continue
            if stop.match(doc.lines[j]):
                break
            m = INDEX_ITEM_RE.match(doc.lines[j])
            if m:
                nums.add(int(m.group(1)))
        break
    for pair in doc.pairs:
        if pair.bid == "trap-index":
            nums = nums or set()
            for line in doc.lines[pair.start + 1:pair.end]:
                m = TRAP_ROW_RE.match(line.strip())
                if m:
                    nums.add(int(m.group(1)))
    return nums


def _bodies(doc: _Doc) -> set[int]:
    nums = set()
    for _, line in doc.live_lines():
        m = TRAP_HEADING_RE.match(line) or BULLET_RE.match(line)
        if m:
            nums.add(int(m.group(1)))
    return nums


def narrowest_glob(names: list[str]) -> str:
    """The narrowest `prefix*suffix` glob matching every name: the name itself when there is
    one, else the names' common prefix and common suffix around one `*`."""
    uniq = sorted(set(names))
    if len(uniq) == 1:
        return uniq[0]
    prefix = os.path.commonprefix(uniq)
    suffix = os.path.commonprefix([n[::-1] for n in uniq])[::-1]
    room = min(len(n) for n in uniq) - len(prefix)
    suffix = suffix[max(0, len(suffix) - room):] if room > 0 else ""
    return f"{prefix}*{suffix}"


def _trap_sets(base: Path, working: Path | None, docs: list[_Doc], notes: list[str]
               ) -> list[TrapSet]:
    groups: dict[Path, list[_Doc]] = {}
    for d in docs:
        if _is_trap_file(d.path):
            groups.setdefault(d.path.parent, []).append(d)
    sets = []
    anchor = working or base
    for folder in sorted(groups, key=lambda p: (len(p.parts), p.as_posix())):
        group = groups[folder]
        indexes = {id(d): _index_numbers(d) for d in group}
        with_index = sorted((d for d in group if indexes[id(d)] is not None),
                            key=lambda d: (len(d.path.name), d.path.name))
        rest = sorted((d for d in group if indexes[id(d)] is None), key=lambda d: d.path.name)
        bodies = {id(d): _bodies(d) for d in group}
        spans = False
        for idx in with_index:
            elsewhere = set().union(*(bodies[id(d)] for d in group if d is not idx))
            if (indexes[id(idx)] - bodies[id(idx)]) & elsewhere:
                spans = True
        bullets = any(BULLET_RE.match(line) for d in group for _, line in d.live_lines())
        pattern = narrowest_glob([d.path.name for d in group])
        if _under(folder, anchor):
            rel = folder.relative_to(anchor).as_posix()
            glob = pattern if rel == "." else f"{rel}/{pattern}"
        else:
            rel = folder.relative_to(base).as_posix()
            glob = pattern if rel == "." else f"{rel}/{pattern}"
            notes.append(f"{group[0].rel}: trap files outside the working dir; their glob "
                         f"'{glob}' is relative to the scope dir")
        sets.append(TrapSet(files=[d.rel for d in with_index + rest], index_spans_files=spans,
                            bullets=bullets, glob=glob, has_index=bool(with_index)))
    return sets


def _scope(name: str, base: Path, root: Path, tree: _Tree, docs: list[_Doc],
           notes: list[str]) -> ScopeMeasure:
    working = _working_dir(base, docs, tree)
    logs = []
    for d in docs:
        if _is_trap_file(d.path):
            continue
        log = _decision_log(d)
        if log is None:
            continue
        if not log.named and working is not None and working != base \
                and _under(d.path.parent, working):
            continue
        logs.append(log)
    logs.sort(key=lambda log: (not log.named, -log.entries, log.path))

    for d in docs:
        if not _is_trap_file(d.path) and any(TRAPS_SECTION_RE.match(line)
                                             for _, line in d.live_lines()):
            notes.append(f"{d.rel}: traps kept as a '## Traps' section of this file, not in a "
                         f"trap file")

    doc_dirs: dict[str, tuple[int, int]] = {}
    typed: dict[str, list[str]] = {}
    for d in docs:
        parts = d.path.relative_to(base).parts
        if len(parts) < 2 or parts[0].startswith("."):
            continue
        total, with_fm = doc_dirs.get(parts[0], (0, 0))
        doc_dirs[parts[0]] = (total + 1, with_fm + d.doc_type)
        if d.doc_type:
            typed.setdefault(parts[0], []).append(d.rel)
    # Only a mixed dir lists its files: `propose` offers them in place of the whole dir.
    doc_files = {k: sorted(typed[k]) for k, (total, with_fm) in sorted(doc_dirs.items())
                 if 0 < with_fm < total}

    return ScopeMeasure(
        name=name, dir=base.relative_to(root).as_posix(), decision_logs=logs,
        trap_sets=_trap_sets(base, working, docs, notes),
        working_dir=working.relative_to(base).as_posix() if working is not None else None,
        doc_dirs=dict(sorted(doc_dirs.items())), doc_files=doc_files)


# ---------------------------------------------------------------------------- the whole root

def _subrepos(root: Path) -> list[str]:
    """Nested git checkouts (a `.git` dir, or a submodule's `.git` file) under the root, outside
    the never-descend trees. Git-ignored ones count: a workspace root usually ignores its
    members' checkouts. A checkout's own nested checkouts are its business, not the root's."""
    found = []
    for current, dirnames, _ in os.walk(root):
        here = Path(current)
        keep = []
        for name in sorted(dirnames):
            if name in NEVER:
                continue
            if (here / name / ".git").exists():
                found.append((here / name).relative_to(root).as_posix())
            else:
                keep.append(name)
        dirnames[:] = keep
    return found


def measure(root: Path, planned: tuple[str, dict] | None = None) -> Measurement:
    """`planned` is a registry file adopt will write, `(file name, its TOML data)`: measured as
    if it were on disk, in place of looking for one."""
    root = Path(os.path.normpath(root.resolve()))
    notes: list[str] = []
    ignored = _Ignores()
    if planned is not None:
        file, data = planned
        key, entries = next((k, v) for k, v in data.items() if isinstance(v, list))
        reg = _registry(file, key, entries)
    else:
        reg = _find_registry(root, notes)

    governed: list[tuple[str, Path]] = []
    members: set[Path] = set()
    if reg is not None:
        key = lambda k: reg.keys.get(k, k)   # noqa: E731
        for e in reg.entries:
            for k in ("dir", "governance"):
                v = dig(e, key(k))
                if isinstance(v, str) and v and _norm(root, v) != root:
                    members.add(_norm(root, v))
        for e in reg.entries:
            name = str(dig(e, key("name")) or "?")
            gov, d = dig(e, key("governance")), dig(e, key("dir"))
            if gov == "" or dig(e, key("tier")) != "full":
                continue
            where = gov or d
            if not isinstance(where, str) or not where:
                notes.append(f"{reg.file}: '{name}' names no dir; not measured")
                continue
            base = _norm(root, where)
            if not base.is_dir():
                notes.append(f"{reg.file}: '{name}' dir '{where}' does not exist; not measured")
                continue
            governed.append((name, base))

    docs: dict[Path, _Doc] = {}

    def load(tree: _Tree) -> list[_Doc]:
        out = []
        for f in tree.files:
            if f.suffix.lower() != ".md":
                continue
            if f not in docs:
                d = _load(f, root, notes)
                if d is None:
                    continue
                docs[f] = d
            out.append(docs[f])
        return out

    ws_tree = _walk(root, members, ignored)
    workspace = _scope("workspace" if reg is not None else root.name, root, root, ws_tree,
                       load(ws_tree), notes)
    scopes = []
    for name, base in governed:
        tree = _walk(base, {m for m in members if m != base and base in m.parents}, ignored)
        scopes.append(_scope(name, base, root, tree, load(tree), notes))

    prefixes: Counter[str] = Counter()
    blocks: set[tuple[str, str]] = set()
    max_ids: dict[str, int] = {}
    for d in docs.values():
        for p in d.pairs:
            prefixes[p.prefix] += 1
            blocks.add((d.rel, p.bid))
        for _, line in d.live_lines():
            m = HEADING_ID_RE.match(line)
            if m:
                max_ids[m.group(1)] = max(max_ids.get(m.group(1), 0), int(m.group(2)))

    return Measurement(
        root=str(root),
        registry=reg.file if reg else None,
        registry_entries=reg.entries_key if reg else None,
        registry_keys=reg.keys if reg else {},
        governance_key_missing=bool(reg) and not any(
            dig(e, reg.keys.get("governance", "governance")) is not None for e in reg.entries),
        workspace=workspace, scopes=scopes,
        markers=prefixes.most_common(1)[0][0] if prefixes else None,
        blocks=sorted(blocks), max_ids=dict(sorted(max_ids.items())),
        notes=notes,
        subrepos=_subrepos(root))


# ---------------------------------------------------------------------------- CLI

def _summary(m: Measurement) -> str:
    out = [f"root: {m.root}"]
    if m.registry:
        keys = ", ".join(f"{k} = {v}" for k, v in m.registry_keys.items()) or "all top-level"
        out.append(f"registry: {m.registry} [[{m.registry_entries}]] (keys: {keys})"
                   + ("; no entry has a governance key" if m.governance_key_missing else ""))
    else:
        out.append("registry: none (a single repo)")
    out.append(f"markers: {m.markers or 'none'} ({len(m.blocks)} block pairs)")
    for s in [m.workspace, *m.scopes]:
        out.append(f"scope {s.name} ({s.dir}):")
        out.append(f"  working dir: {s.working_dir or 'none'}")
        for log in s.decision_logs:
            out.append(f"  decision log: {log.path} ({log.entries} entries, level {log.level}, "
                       f"prefixes {', '.join(log.prefixes) or 'none'}"
                       f"{', named' if log.named else ''})")
        for ts in s.trap_sets:
            out.append(f"  traps: {', '.join(ts.files)} (glob {ts.glob}"
                       f"{', index spans files' if ts.index_spans_files else ''}"
                       f"{', bullets' if ts.bullets else ''})")
        for d, (total, fm) in s.doc_dirs.items():
            out.append(f"  docs: {d}/ {fm}/{total} with doc_type")
    ids = ", ".join(f"{k}-{v}" for k, v in m.max_ids.items())
    out.append(f"highest ids: {ids or 'none'}")
    if m.subrepos:
        out.append(f"nested checkouts: {', '.join(m.subrepos)}")
    for n in m.notes:
        out.append(f"note: {n}")
    return "\n".join(out)


def run(root: Path, as_json: bool) -> int:
    if not root.is_dir():
        print(f"measure: {root} is not a directory", file=sys.stderr)
        return 2
    try:
        m = measure(root)
    except MeasureError as exc:
        print(f"measure: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(asdict(m), indent=2, ensure_ascii=False) if as_json else _summary(m))
    return 0
