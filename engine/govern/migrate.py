"""Move a project's decision logs and trap files onto the standard mechanically, for a project
whose logs use another common shape:

- Sections grouping entries (`## Topic (D-500-D-599)` over `### D-500 — Title`) flatten to
  `## D-500 — Title` at the log's own entry level, with a `**Topic:**` line carrying the
  section's title.
- Hand-written bullet traps (`- **1. Title.** Body...`) become `## T-1 — Title` headings, and
  the hand-written `## Index` list becomes the engine's generated `trap-index` block.
- A superseded decision that names its successor becomes a one-line pointer; one that
  names none is left for a person to decide.

Wired as `python3 -m govern.installer migrate --root DIR [--apply]`. Default is a dry run:
nothing changes, and `.context-gate/migration-report.md` describes every edit an apply
would make, what could not be placed mechanically, and the findings before and after. `--apply`
makes the edits and refuses (exit 2, nothing changed) if a file it would touch has uncommitted
changes in its own git repo, so the migration lands as one purely mechanical commit — it never
commits itself, the report ends with the commands to. A second run on an already-migrated
project reports nothing to do.

Every file comes from the project's config and registry (decision logs and trap files per
scope), never a hard-coded project path.
"""
from __future__ import annotations

import os
import re
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path

import govern.checks  # noqa: F401  (registers the built-in checks the before/after run needs)
from govern import __version__, cli, config, layout, registry
from govern.context import Context, git, repo_of
from govern.decisions import GRAMMARS, id_key, mask_lines, POINTER_RE, STATUS_RE, TOPIC_RE
from govern.findings import Findings
from govern.text import Unreadable, eol, has_bom, overlay, read

RANGE_RE = re.compile(r"\s*\(\s*[A-Za-z]*-?\d+\s*[–—-]\s*[A-Za-z]*-?\d+\s*\)\s*$")
# A successor id, plain (`D-40`), bold (`**D-40**`) or a markdown link (`[D-40](...)`): the
# wrapping markup is stripped, never captured, so callers only ever see the bare id.
SUCCESSOR_INTRO_RE = re.compile(r"(?:Superseded|Replaced)\s+by:?", re.I)
SUCCESSOR_ID_RE = re.compile(r"\s*[\[\*]*\s*([A-Za-z]+-\d+)\s*[\]\*]*(?:\([^)]*\))?")
SUCCESSOR_SEP_RE = re.compile(r"\s*(?:,\s*|\band\b\s*)", re.I)
STATUS_LINE_RE = re.compile(r"^(?:[-*][ \t]+)?\*\*Status:\*\*.*$", re.M)
BULLET_RE = re.compile(r"^- \*\*(\d+)\.\s*(.+?)\*\*(.*)$")
CONTINUES_RE = re.compile(r"^[,;:—–-]|^[a-z]")
INDEX_ITEM_RE = re.compile(r"^(\d+)\.\s+\S")
INDEX_HEADING_RE = re.compile(r"^(#{1,6})\s+Index\s*$", re.I)
LINE_NO_RE = re.compile(r":\d+")


def fail(msg: str, code: int = 2) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return code


def _rel_or_none(path: Path, base: Path) -> str | None:
    """`path`, relative to `base`, as a POSIX string — or `None` when a misconfigured layout
    (an absolute or `..`-escaping `working_dir`, say) puts `path` outside `base` entirely, so a
    caller can report that instead of letting `relative_to` raise."""
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return None


# ---------------------------------------------------------------------------- the plan

@dataclass
class Change:
    path: Path
    detail: str


@dataclass
class Problem:
    path: Path
    line: int          # 0 when the problem is file-wide, not one line
    reason: str


@dataclass
class Plan:
    edits: dict[Path, str] = field(default_factory=dict)         # path -> full new text
    changes: list[Change] = field(default_factory=list)
    notes: list[Change] = field(default_factory=list)            # informational, not an edit
    problems: list[Problem] = field(default_factory=list)
    config_notes: list[str] = field(default_factory=list)
    expected: list[str] = field(default_factory=list)            # findings the plan predicts
    # Suggested trap-index entries, glob -> sources. One per glob: every `[[blocks.project]]`
    # entry applies to every scope, so two entries with the same glob would render the same
    # file two ways and leave one of them stale forever. `sources` is kept if any scope needs it.
    trap_notes: dict[str, str | None] = field(default_factory=dict)
    # Every id a section migration bumped onto its own heading level: a finding
    # about one of these that the old, malformed heading hid from the gate is "newly visible",
    # not "should not happen".
    touched_ids: set[str] = field(default_factory=set)


def _load_context(root: Path) -> Context:
    cfg = config.load(root, layout.home())
    return Context(root=root, home=layout.home(), cfg=cfg, registry=registry.load(cfg),
                   prog="govern")


def plan(ctx: Context) -> Plan:
    p = Plan()
    planned_logs: set[Path] = set()

    def plan_log(log: Path) -> None:
        # Two scopes (or the workspace and a scope) can share the same decision log; plan it
        # once, so its changes and problems are not reported twice.
        if log in planned_logs:
            return
        planned_logs.add(log)
        _plan_decisions(ctx, log, p)

    if ctx.workspace_log is not None:
        plan_log(ctx.workspace_log)
    for scope in ctx.registry.scopes:
        if not scope.governed:
            continue
        plan_log(ctx.scope_log(scope))
        _plan_traps(ctx, scope, p)
    for glob, sources in p.trap_notes.items():
        note = f'[[blocks.project]]\nid = "trap-index"\nglob = "{glob}"'
        p.config_notes.append(note + (f'\nsources = "{sources}"' if sources else ""))
    return p


# ---------------------------------------------------------------------------- 1: sections -> Topic

def _would_be_pointer(body: str, known: set) -> bool:
    """Whether `_migrate_pointers` will reduce this entry to a bodyless pointer in this same
    run: superseded, with a successor `_successor` resolves and that exists in this log
    (`known`, as `id_key`s). The same test `_migrate_pointers` makes, so an entry whose pointer
    will be refused keeps its **Topic:** line."""
    status = STATUS_RE.search(body)
    if not status or status.group(1).lower() != "superseded":
        return False
    succ, _ = _successor(body)
    return succ is not None and id_key(succ) in known


def _migrate_sections(ctx: Context, text: str) -> tuple[str | None, list[str], list[tuple[int, str]],
                                                        set[str]]:
    """Old shape: a section heading (`## Topic (D-500-D-599)`) groups entries one heading level
    deeper. New: the entries flatten to the log's own entry level, each carrying a `**Topic:**`
    line (the section title, its trailing id-range parenthetical stripped) as the first body
    line — unless it already has one, is already a pointer (a pointer carries no body), or
    is about to become one in this same run. A section whose heading has prose before its first
    entry cannot be placed mechanically, so it, and its entries, are left exactly as they were."""
    level = ctx.grammar.level
    sep = GRAMMARS[ctx.cfg.dialect["decision_heading"]]["sep"]
    entry_re = re.compile(rf"^#{{{level}}} [A-Za-z]+-\d+{sep}.+$")
    old_entry_re = re.compile(rf"^#{{{level + 1}}} ([A-Za-z]+-\d+){sep}(.+)$")
    section_re = re.compile(rf"^#{{{level}}} (.+)$")
    child_stop_re = re.compile(rf"^#{{1,{level + 1}}}\s")
    section_stop_re = re.compile(rf"^#{{1,{level}}}\s")

    lines = text.split("\n")
    live = mask_lines(lines, ctx.markers)
    n = len(lines)
    any_entry_re = re.compile(rf"^#{{{level},{level + 1}}} ([A-Za-z]+-\d+){sep}")
    known = {id_key(m.group(1)) for k, ln in enumerate(lines)
             if live[k] and (m := any_entry_re.match(ln))}

    remove: set[int] = set()
    bump: set[int] = set()
    blank_strip: set[int] = set()
    insert_before: dict[int, list[str]] = {}
    changes: list[str] = []
    problems: list[tuple[int, str]] = []
    touched: set[str] = set()

    i = 0
    while i < n:
        if not (live[i] and section_re.match(lines[i]) and not entry_re.match(lines[i])):
            i += 1
            continue
        title = section_re.match(lines[i]).group(1).strip()
        j = i + 1
        while j < n and not (live[j] and section_stop_re.match(lines[j])):
            j += 1
        children = [k for k in range(i + 1, j) if live[k] and old_entry_re.match(lines[k])]
        if not children:
            i += 1     # a plain heading, not a section grouping entries — none of our business
            continue
        if any(lines[k].strip() for k in range(i + 1, children[0])):
            problems.append((i + 1, f"prose between '{title}' and its first entry cannot be "
                                    f"placed mechanically — left as a plain heading"))
            i = j
            continue
        topic = RANGE_RE.sub("", title).strip()
        remove.add(i)
        if i + 1 < n and not lines[i + 1].strip():
            remove.add(i + 1)
        inserted = 0
        for k in children:
            bump.add(k)
            hm = old_entry_re.match(lines[k])
            ident, child_title = hm.group(1), hm.group(2).strip()
            touched.add(ident)
            m2 = k + 1
            while m2 < n and not (live[m2] and child_stop_re.match(lines[m2])):
                m2 += 1
            live_body = "\n".join(lines[m] for m in range(k + 1, m2) if live[m])
            if TOPIC_RE.search(live_body):
                continue        # already has one: keep it
            if POINTER_RE.match(child_title) or _would_be_pointer(live_body, known):
                continue        # a pointer carries no body: never give it one
            fc = k + 1
            while fc < m2 and not lines[fc].strip():
                fc += 1
            blank_strip.update(range(k + 1, fc))
            insert_before[fc] = ["", f"**Topic:** {topic}", ""]
            inserted += 1
        if inserted:
            changes.append(f"'{title}' -> **Topic:** {topic} on {inserted} "
                           f"entr{'y' if inserted == 1 else 'ies'}")
        i = j

    if not remove and not bump:
        return None, changes, problems, touched

    out: list[str] = []
    for idx, line in enumerate(lines):
        if idx in insert_before:
            out.extend(insert_before[idx])
        if idx in remove or idx in blank_strip:
            continue
        out.append(line[1:] if idx in bump else line)
    if n in insert_before:      # a bodyless child at the very end of the file: nothing to
        out.extend(insert_before[n])   # append to, so the loop above never reaches it
    return "\n".join(out), changes, problems, touched


# ---------------------------------------------------------------------------- 2: superseded -> pointer

def _successor_groups(text: str) -> list[list[str]]:
    """Every `Superseded by`/`Replaced by` phrase in `text`, each as the list of ids it names, in
    order. An id may be bare (`D-40`), bold (`**D-40**`) or a markdown link (`[D-40](...)`); a
    phrase naming several, joined by `,` or `and` (`by D-40 and D-41`, `by D-40, D-41`), yields
    all of them — ambiguous on its own, exactly like naming them in separate phrases."""
    groups: list[list[str]] = []
    for intro in SUCCESSOR_INTRO_RE.finditer(text):
        pos = intro.end()
        ids: list[str] = []
        while True:
            m = SUCCESSOR_ID_RE.match(text, pos)
            if not m:
                break
            ids.append(m.group(1))
            pos = m.end()
            sep = SUCCESSOR_SEP_RE.match(text, pos)
            if not sep:
                break
            pos = sep.end()
        if ids:
            groups.append(ids)
    return groups


def _successor(body: str) -> tuple[str | None, list[str]]:
    """(chosen successor, every distinct id found). The `**Status:**` line wins outright: it is
    the one line an entry's status is read from, so a successor named there is unambiguous even
    when the rest of the body mentions other ids in passing (an earlier supersession, a typo
    fixed later) — unless the status line itself names several, which is ambiguous right there.
    Failing that, the body names a successor only when it names exactly one — several distinct
    ids, whether from separate phrases or one phrase naming more than one, is ambiguous, not a
    pointer to whichever came first."""
    status_line = STATUS_LINE_RE.search(body)
    if status_line:
        groups = _successor_groups(status_line.group(0))
        if groups:
            ids = groups[0]
            return (ids[0], ids) if len(ids) == 1 else (None, ids)
    found: list[str] = []
    for ids in _successor_groups(body):
        for i in ids:
            if i not in found:
                found.append(i)
    return (found[0], found) if len(found) == 1 else (None, found)


def _migrate_pointers(ctx: Context, text: str) -> tuple[str | None, list[str], list[tuple[int, str]]]:
    """A `**Status:** superseded` entry that names its successor (`Superseded by D-NNN` or
    `Replaced by X-N`, in the status line or body) becomes a one-line pointer, only when
    the successor exists in this same log. One with no successor, or with several possible
    successors named and none of them on the `**Status:**` line, is left alone — it needs a
    person: rewrite in place, retire it, or say which successor is the real one."""
    parsed = ctx.grammar.parse(text)
    known = {(e.prefix.upper(), e.num) for e in parsed.entries}
    lines = text.split("\n")
    replacements: list[tuple[int, int, list[str]]] = []
    changes: list[str] = []
    problems: list[tuple[int, str]] = []

    for e in parsed.entries:
        if e.replaced_by or e.status != "superseded":
            continue
        succ, found = _successor(e.body)
        if succ is None:
            if not found:
                problems.append((e.line, f"{e.ident} is superseded with no successor named — "
                                         f"rewrite it in place, or retire it"))
            else:
                problems.append((e.line, f"{e.ident} names several possible successors "
                                         f"({', '.join(found)}) and none is on the "
                                         f"**Status:** line — a person needs to say which one"))
            continue
        if id_key(succ) not in known:
            problems.append((e.line, f"{e.ident} names successor {succ}, which is not in this "
                                     f"log — it cannot become a pointer until {succ} exists here"))
            continue
        heading = f"{'#' * ctx.grammar.level} {e.ident} — Replaced by {succ}"
        replacements.append((e.line - 1, e.last_line, [heading, ""]))
        changes.append(f"{e.ident} -> pointer to {succ}")

    if not replacements:
        return None, changes, problems
    out, pos = [], 0
    for start, end, new in sorted(replacements):
        out += lines[pos:start] + new
        pos = end
    out += lines[pos:]
    return "\n".join(out), changes, problems


def _plan_decisions(ctx: Context, log: Path, p: Plan) -> None:
    if not log.exists():
        return
    raw = read(log)
    nl = eol(raw)
    lf = raw.replace("\r\n", "\n")

    sections_text, section_changes, section_problems, touched = _migrate_sections(ctx, lf)
    working = sections_text if sections_text is not None else lf
    pointers_text, pointer_changes, pointer_problems = _migrate_pointers(ctx, working)
    final = pointers_text if pointers_text is not None else working

    p.touched_ids |= touched
    if final != lf:
        p.edits[log] = final.replace("\n", nl) if nl == "\r\n" else final
    for detail in section_changes + pointer_changes:
        p.changes.append(Change(log, detail))
    for line, reason in section_problems + pointer_problems:
        p.problems.append(Problem(log, line, reason))


# ---------------------------------------------------------------------------- 3: bullet traps -> headings

def _plan_traps(ctx: Context, scope, p: Plan) -> None:
    files = ctx.trap_files(scope)
    if not files:
        return
    prefix, level = ctx.trap_prefix, ctx.grammar.level

    file_lines: dict[Path, list[str]] = {}
    file_live: dict[Path, list[bool]] = {}
    file_nl: dict[Path, str] = {}
    for path in files:
        raw = read(path)
        file_nl[path] = eol(raw)
        lines = raw.replace("\r\n", "\n").split("\n")
        file_lines[path] = lines
        file_live[path] = mask_lines(lines, ctx.markers)

    heading_loc: dict[int, tuple[Path, int]] = {}
    for path in files:
        for e in ctx.grammar.traps(path, prefix):
            heading_loc.setdefault(e.num, (path, e.line))
    existing = set(heading_loc)
    bullet_hits: dict[Path, list[tuple[int, int, re.Match]]] = {path: [] for path in files}
    bullet_nums: set[int] = set()
    num_paths: dict[int, list[Path]] = {}
    for path in files:
        lines, live = file_lines[path], file_live[path]
        starts = [idx for idx, line in enumerate(lines) if live[idx] and BULLET_RE.match(line)]
        for pos, start in enumerate(starts):
            end = starts[pos + 1] if pos + 1 < len(starts) else len(lines)
            while end > start + 1 and not lines[end - 1].strip():
                end -= 1
            m = BULLET_RE.match(lines[start])
            num = int(m.group(1))
            bullet_hits[path].append((start, end, m))
            bullet_nums.add(num)
            num_paths.setdefault(num, []).append(path)

    known_after = existing | bullet_nums
    if not known_after:
        return   # nothing trap-shaped in here at all

    # T-N with a bullet body in more than one file: neither copy is
    # migrated — a person has to pick one — though the number still counts as "has a body" for
    # the index-drop check below.
    dup_nums = {num for num, paths in num_paths.items() if len(set(paths)) > 1}
    for num in sorted(dup_nums):
        locs = [(path, start) for path in files for start, end, m in bullet_hits[path]
               if int(m.group(1)) == num]
        where = ", ".join(f"{ctx.rel(loc_path)}:{start + 1}" for loc_path, start in locs)
        for loc_path, start in locs:
            p.problems.append(Problem(loc_path, start + 1, f"{prefix}-{num} has a body in more "
                                      f"than one file ({where}) — needs a person to pick one"))

    # A bullet whose number is already spent by a `## T-N` heading somewhere in the scope
    # (elsewhere in this same run, or from an earlier, partial migration): migrating it too
    # would mint a second entry under the same id, so it is left as a bullet — a person has to
    # merge the two or renumber one.
    heading_conflicts = bullet_nums & existing
    for num in sorted(heading_conflicts):
        hpath, hline = heading_loc[num]
        for path in files:
            for start, end, m in bullet_hits[path]:
                if int(m.group(1)) == num:
                    p.problems.append(Problem(path, start + 1,
                        f"{prefix}-{num} is already a heading ({ctx.rel(hpath)}:{hline}) — this "
                        f"bullet was left as a bullet, needs a person to merge them"))

    num_location: dict[int, Path] = {}
    for path in files:
        for e in ctx.grammar.traps(path, prefix):
            num_location.setdefault(e.num, path)
    for path, hits in bullet_hits.items():
        for start, end, m in hits:
            num_location.setdefault(int(m.group(1)), path)

    index_loc = None   # (path, heading idx, body-end idx, [(line idx, num)])
    index_bad = False
    for path in files:
        lines, live = file_lines[path], file_live[path]
        if ctx.markers.open("trap-index") in "\n".join(lines):
            continue    # already migrated: the generated block is there, nothing hand-written left
        for idx, line in enumerate(lines):
            hm = live[idx] and INDEX_HEADING_RE.match(line.strip())
            if not hm:
                continue
            stop_re = re.compile(rf"^#{{1,{len(hm.group(1))}}}\s")
            j, items, bad, seen_blank = idx + 1, [], False, False
            while j < len(lines):
                if not live[j]:
                    j += 1
                    continue
                raw_line = lines[j]
                if stop_re.match(raw_line):
                    break
                if not raw_line.strip():
                    seen_blank = True
                    j += 1
                    continue
                m = INDEX_ITEM_RE.match(raw_line)
                if not m:
                    # A clean list ends at a blank, once it has actually collected an item —
                    # anything else right after an item (no blank) is a continuation, not an
                    # end. Content that never matched a numbered item at all (a table, or a
                    # bulleted list) is not a plain numbered list no matter how it is spaced.
                    bad = not (seen_blank and items)
                    break
                items.append((j, int(m.group(1))))
                seen_blank = False
                j += 1
            if bad:
                p.problems.append(Problem(path, idx + 1,
                    "the '## Index' list is not a plain numbered list (a table, bullets, or "
                    "wrapped/indented lines) — left unchanged, needs a person"))
                index_bad = True
            else:
                index_loc = (path, idx, j, items)
            break
        if index_loc or index_bad:
            break

    declared = any(t.get("render", t.get("id")) == "trap-index"
                   for t in ctx.cfg.get("blocks", "project", []))
    overall_max = max(known_after | {n for _, n in (index_loc[3] if index_loc else [])})

    for path in files:
        lines = file_lines[path]
        replacements: list[tuple[int, int, list[str]]] = []

        if index_loc and index_loc[0] == path:
            _, hidx, body_end, items = index_loc
            replacements.append((hidx + 1, body_end,
                                 ["", ctx.markers.open("trap-index"),
                                  ctx.markers.close("trap-index"), ""]))
            for line_idx, num in items:
                if num in known_after:
                    continue
                risk = (f" — it is the highest number ever mentioned, so next-id could reissue "
                        f"it once nothing else names it" if num == overall_max else "")
                p.problems.append(Problem(path, line_idx + 1,
                    f"{prefix}-{num} has no body anywhere and was dropped from the index{risk}"))
            p.changes.append(Change(path, "hand-written `## Index` list replaced by the "
                                         "generated `trap-index` block"))
            if not declared:
                rel_file = _rel_or_none(path, scope.gov)
                if rel_file is None:
                    p.problems.append(Problem(path, 0,
                        f"[projects] working_dir places this outside {scope.name}'s own "
                        f"governance directory — no [[blocks.project]] entry can be suggested "
                        f"for it; fix working_dir, then rerun"))
                else:
                    sources = None
                    if any(loc != path for loc in num_location.values()):
                        sources = (f"{ctx.projects('working_dir', 'working-files')}/"
                                  f"{ctx.projects('trap_glob', 'traps*.md')}")
                    # `glob`, never `file`: a `file` target missing from another scope is an
                    # error there; a `glob` matching nothing in a scope is simply empty.
                    p.trap_notes[rel_file] = p.trap_notes.get(rel_file) or sources

        for start, end, m in bullet_hits[path]:
            num = int(m.group(1))
            if num in dup_nums:
                continue    # a body in more than one file: reported above, neither is touched
            if num in heading_conflicts:
                continue    # already a heading elsewhere: reported above, left as a bullet
            raw_title, rest = m.group(2).strip(), m.group(3)
            title = raw_title[:-1] if raw_title.endswith(".") else raw_title
            heading = f"{'#' * level} {prefix}-{num} — {title}"
            sentence_done = raw_title.endswith((".", "!", "?"))
            if rest and not sentence_done and CONTINUES_RE.match(rest.lstrip()):
                # The text right after `**` continues the sentence: the heading is still just
                # the bold text, but the body keeps the whole sentence, joined, so it never
                # opens on a fragment.
                body_first = (raw_title + rest).strip()
                p.problems.append(Problem(path, start + 1,
                    f"{prefix}-{num}: the bold text does not end its sentence — the heading may "
                    f"need a better title (the body keeps the whole sentence as written)"))
            else:
                body_first = rest.strip()
            block = [heading, ""] + ([body_first] if body_first else []) + lines[start + 1:end]
            replacements.append((start, end, block))
            p.changes.append(Change(path, f"{prefix}-{num} — {title}: bullet -> heading"))
            if "**Bites when:**" not in "\n".join(block):
                p.notes.append(Change(path, f"{prefix}-{num} has no **Bites when:** line — the "
                                            f"trap index will show a dash"))
                p.expected.append(f"{scope.name}: {ctx.rel(path)} {prefix}-{num} has no "
                                  f"**Bites when:** — the trap index shows it as a dash")

        if not replacements:
            continue
        out, pos = [], 0
        for start, end, new in sorted(replacements):
            out += lines[pos:start] + new
            pos = end
        out += lines[pos:]
        heading_prefix = f"{'#' * level} {prefix}-"
        fixed: list[str] = []
        for line in out:
            if line.startswith(heading_prefix) and fixed and fixed[-1].strip():
                fixed.append("")
            fixed.append(line)
        new_text = "\n".join(fixed)
        nl = file_nl[path]
        p.edits[path] = new_text.replace("\n", nl) if nl == "\r\n" else new_text


# ---------------------------------------------------------------------------- before/after findings

def _flatten(results: list[tuple[str, Findings]]) -> list[tuple[str, str, str]]:
    return [(cid, lvl, msg) for cid, f in results
            for lvl, msgs in (("error", f.errors), ("warn", f.warnings)) for msg in msgs]


def _norm(msg: str) -> str:
    """A finding's message, whitespace-collapsed and with any `:N` line number blanked, so a
    finding that only moved to a different line is neither "fixed" nor "new" in the diff."""
    return " ".join(LINE_NO_RE.sub(":", msg).split())


def _findings(ctx: Context, override: dict[Path, str] | None = None) -> list[tuple[str, str, str]]:
    """Every check's findings. When `override` is given, every engine read of a project file
    (`text.read`/`text.read_text`) is served from it instead of disk — so the "after" run sees
    the migrated text without anything being written.

    `plan` and `run` call this twice against the same `ctx` (before, then after): a check that
    dedupes a workspace and a project scope sharing one file decides ownership from the registry
    itself (`Context.owner`), not from which of the two runs first, so the second pass reads
    exactly like the first would on its own."""
    if not override:
        return _flatten(cli.collect(ctx, ctx.registry.scopes, workspace=True))
    with overlay(override):
        return _flatten(cli.collect(ctx, ctx.registry.scopes, workspace=True))


# ---------------------------------------------------------------------------- git

def _apply_blockers(paths: list[Path]) -> list[str]:
    """Every reason `--apply` must refuse: a file outside any git repo, one `git status` could
    not be checked (git missing, `safe.directory`, a lock — `context.git` returns `None`,
    never confused with a clean file's empty output), one with uncommitted changes, or one git
    ignores. `--porcelain --ignored` is what surfaces the last case: plain `--porcelain` omits
    an ignored file entirely, which would otherwise read as clean and let the edit through —
    into a file the migration's own commit could never actually include. The migration has to
    land as one reviewable commit, so any of these fails closed."""
    blockers = []
    for path in paths:
        repo = repo_of(path)
        if repo is None:
            blockers.append(f"{path}: not inside a git repo — the migration must land as a "
                            f"reviewable commit")
            continue
        rel = path.relative_to(repo).as_posix()
        status = git(repo, "status", "--porcelain", "--ignored", "--", rel)
        if status is None:
            blockers.append(f"{path}: `git status` failed in {repo} (git missing, "
                            f"safe.directory, or a lock) — resolve it and rerun")
        elif status:
            first = status.splitlines()[0].strip()
            if first.startswith("!!"):
                blockers.append(f"{path}: ignored by git ({first}) — a .gitignore rule excludes "
                                f"it, so the migration's own commit could never include it")
            else:
                blockers.append(f"{path}: uncommitted changes ({first})")
    return blockers


def _repo_groups(paths: list[Path]) -> dict[Path, list[Path]]:
    groups: dict[Path, list[Path]] = {}
    for path in paths:
        repo = repo_of(path)
        if repo is not None:
            groups.setdefault(repo, []).append(path)
    return groups


# ---------------------------------------------------------------------------- atomic apply

def _apply_edits(edits: dict[Path, str]) -> str | None:
    """Write every edit, or none of them. Each file is written to a temp sibling first (a BOM
    the original had is restored here, at the last moment, never carried in `p.edits` itself —
    the before/after run reads that text through the overlay, and a stray BOM there would make
    frontmatter parsing fail on text that will read cleanly once really written, since `read`
    strips a real file's BOM on the way in). Every temp file is then swapped in with `os.replace`
    (atomic, and works the same on Windows); if any swap fails, the ones already swapped are
    restored from the bytes captured just before, so a partial apply is never left on disk."""
    temps: dict[Path, Path] = {}
    try:
        for path, new_text in edits.items():
            out_text = ("﻿" + new_text) if has_bom(path) else new_text
            tmp = path.with_name(path.name + ".govmigrate.tmp")
            with open(tmp, "w", encoding="utf-8", newline="") as fh:
                fh.write(out_text)
            temps[path] = tmp
    except OSError as exc:
        for tmp in temps.values():
            tmp.unlink(missing_ok=True)
        return str(exc)

    backups: dict[Path, bytes] = {}
    replaced: list[Path] = []
    for path, tmp in temps.items():
        try:
            backups[path] = path.read_bytes()
            os.replace(tmp, path)
            replaced.append(path)
        except OSError as exc:
            for done in replaced:
                done.write_bytes(backups[done])
            for path2, tmp2 in temps.items():
                if path2 not in replaced:
                    tmp2.unlink(missing_ok=True)
            return str(exc)
    return None


# ---------------------------------------------------------------------------- report

def _mentions(msg: str, idents: set[str]) -> bool:
    return any(re.search(rf"\b{re.escape(i)}\b", msg) for i in idents)


def _write_report(ctx: Context, path: Path, p: Plan, before, after, applied: bool,
                  apply_error: str | None = None) -> None:
    before_set = {_norm(m) for _, _, m in before}
    after_set = {_norm(m) for _, _, m in after}
    expected_set = {_norm(m) for m in p.expected}
    fixed = [(cid, lvl, msg) for cid, lvl, msg in before if _norm(msg) not in after_set]
    stayed_new = [(cid, lvl, msg) for cid, lvl, msg in after if _norm(msg) not in before_set]
    expected = [(cid, lvl, msg) for cid, lvl, msg in stayed_new if _norm(msg) in expected_set]
    # A finding about an id the section migration bumped onto its own heading level was hidden
    # from the gate by the old, malformed heading — it is newly visible, not a new problem this
    # migration created, so it gets its own bucket, separate from both "expected" (the plan
    # already predicted it, e.g. a missing **Bites when:**) and "should not happen" (everything
    # else — a real regression worth checking by hand).
    newly_visible = [(cid, lvl, msg) for cid, lvl, msg in stayed_new
                     if _norm(msg) not in expected_set and _mentions(msg, p.touched_ids)]
    should_not = [(cid, lvl, msg) for cid, lvl, msg in stayed_new
                  if _norm(msg) not in expected_set and not _mentions(msg, p.touched_ids)]

    lines = ["# Migration report", "", f"Engine {__version__}.", ""]
    if apply_error:
        lines += [f"**Apply failed and was rolled back:** {apply_error} Nothing was changed.", ""]
    lines.append("**Applied.**" if applied else
                 "**Dry run — nothing was changed.** Rerun with `--apply` to make these edits.")
    lines.append("")

    if not p.edits and not p.problems:
        lines += ["Nothing to migrate: every decision log and trap file already matches the "
                  "standard.", ""]

    if p.edits:
        lines += ["## Changes", ""]
        by_file: dict[Path, list[Change]] = {}
        for c in p.changes:
            by_file.setdefault(c.path, []).append(c)
        for file in sorted(p.edits, key=lambda x: x.as_posix()):
            lines += [f"### `{ctx.rel(file)}`", ""]
            lines += [f"- {c.detail}" for c in by_file.get(file, [])] + [""]

    if p.notes:
        lines += ["## Notes", ""]
        by_file = {}
        for c in p.notes:
            by_file.setdefault(c.path, []).append(c)
        for file in sorted(by_file, key=lambda x: x.as_posix()):
            lines += [f"### `{ctx.rel(file)}`", ""]
            lines += [f"- {c.detail}" for c in by_file[file]] + [""]

    if p.problems:
        lines += ["## Could not be migrated — needs a person", ""]
        for prob in sorted(p.problems, key=lambda x: (x.path.as_posix(), x.line)):
            where = f"{ctx.rel(prob.path)}:{prob.line}" if prob.line else ctx.rel(prob.path)
            lines.append(f"- `{where}`: {prob.reason}")
        lines.append("")

    if p.config_notes:
        lines += ["## Config", "",
                  "So `govern index` fills the block this migration left empty, add:", "",
                  "```toml"]
        for note in p.config_notes:
            lines += [note, ""]
        lines += ["```", ""]

    lines += ["## Findings before and after", "",
              f"Before: {len(before)} finding(s). After: {len(after)} finding(s).", ""]
    if fixed:
        lines += ["Fixed by this migration:", ""]
        lines += [f"- **{lvl}** {msg}" for _, lvl, msg in fixed] + [""]
    if expected:
        lines += ["Expected after this migration (the plan above already says why):", ""]
        lines += [f"- **{lvl}** {msg}" for _, lvl, msg in expected] + [""]
    if newly_visible:
        lines += ["Newly visible: existing content the old format hid from the gate:", ""]
        lines += [f"- **{lvl}** {msg}" for _, lvl, msg in newly_visible] + [""]
    if should_not:
        lines += ["New after this migration (should not happen — check by hand):", ""]
        lines += [f"- **{lvl}** {msg}" for _, lvl, msg in should_not] + [""]

    if p.edits:
        groups = _repo_groups(list(p.edits))
        lines += ["## Commit", ""]
        if not groups:
            lines += ["No git repo was found for the touched files.", ""]
        for repo, paths in groups.items():
            rel_paths = sorted(os.path.relpath(fp, repo).replace(os.sep, "/") for fp in paths)
            rels = " ".join(shlex.quote(rp) for rp in rel_paths)
            verb = "" if applied else " (once you rerun with --apply)"
            lines += [f"In `{repo}`{verb}:", "", "```sh", f"git add {rels}",
                      'git commit -m "Migrate decision and trap docs onto the standard"',
                      "```", ""]

    if p.edits or newly_visible:
        lines += ["## Next steps", ""]
        if p.edits:
            lines.append("- Run `govern index` to regenerate every generated block against the "
                         "migrated text.")
        ratchet = [msg for cid, _, msg in newly_visible if cid == "ratchet"]
        others = [msg for cid, _, msg in newly_visible if cid != "ratchet"]
        if ratchet:
            lines.append(f"- {len(ratchet)} newly visible size breach(es) above can be recorded "
                         "in the ratchet baseline with `govern baseline --allow-raise`. It "
                         "records every new or grown breach in the project, not only these, "
                         "so run `govern check` first and make sure the ratchet findings it "
                         "lists are only the ones above.")
        if others:
            lines.append(f"- {len(others)} other newly visible finding(s) above (a missing "
                         "field, an entry out of order) are fixed in the entries themselves; "
                         "the baseline does not cover them.")
        lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------- entry point

def run(root: Path, apply_: bool) -> int:
    try:
        ctx = _load_context(root)
    except (config.ConfigError, registry.RegistryMissing) as exc:
        return fail(str(exc))
    try:
        p = plan(ctx)
    except Unreadable as exc:
        return fail(f"{exc.path}: {exc.reason} — nothing changed")

    if apply_ and p.edits:
        blockers = _apply_blockers(list(p.edits))
        if blockers:
            for msg in blockers:
                print(f"error: {msg}", file=sys.stderr)
            return fail("nothing was changed — commit or stash these files first, so the "
                       "migration lands as one purely mechanical commit")

    before = _findings(ctx)
    after = _findings(ctx, p.edits)
    report_path = root / layout.GOV_DIR / "migration-report.md"

    if apply_ and p.edits:
        apply_error = _apply_edits(p.edits)
        if apply_error:
            _write_report(ctx, report_path, p, before, after, applied=False,
                          apply_error=apply_error)
            return fail(f"apply failed and was rolled back ({apply_error}) — nothing was changed")

    _write_report(ctx, report_path, p, before, after, applied=apply_)

    rel = report_path.relative_to(root).as_posix()
    if not p.edits and not p.problems:
        print(f"nothing to migrate: every decision log and trap file already matches the "
             f"standard (see {rel})")
        return 0
    verb = "migrated" if apply_ else "would migrate"
    print(f"{verb} {len(p.edits)} file(s), {len(p.problems)} needing a person — see {rel}")
    return 0
