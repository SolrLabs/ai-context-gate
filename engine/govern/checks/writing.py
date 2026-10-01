"""Writing rules: strings a set of files must not contain, with the replacement to use.

A project cannot rename the engine's own names, so a match inside one, used as a name, is never
a finding: anywhere in a file whose format the engine owns (the config, the profile's
principles, the registry file), and in Markdown code (an inline code span or a fenced block).
Prose and every other file are checked in full.

A file under an always-excluded directory (this tool's own and `.claude/` among them) is left out
however a `files` glob or path reaches it, and so is one an `exclude` glob matches. So is a file
git ignores, unless an `include_ignored` glob names it (drafts kept out of git). Every glob is
matched with `Path.glob` semantics, case-sensitive on every OS.

A line that carries `writing-rules: allow <text>[, <text>...]` keeps a spelling on purpose (an
old name kept for back-compat, say): a rule whose `text` it names is not counted on that line."""
from __future__ import annotations

import os
import re
import string
import unicodedata
from pathlib import Path

from govern import config, layout, manifest
from govern.decisions import FENCE, INLINE_CODE_RE, LIVE, line_kinds
from govern.context import default_exclude, glob_matches
from govern.findings import Findings
from govern.manifest import Param, check
from govern.profile import PRINCIPLES
from govern.text import read

OUTWARD = ("README.md", "CONTRIBUTING.md", "docs")

# The characters of a name: the run of them around a match is the name the match is part of
# (`licenses` in `[checks.licenses]`, `checks.licenses.conflicts` or `` `licenses` ``).
NAME_CHARS = frozenset(string.ascii_letters + string.digits + "_-")

# `writing-rules: allow licence, licences`, keywords in any case: the texts run to the end of the
# line, or to the end of the comment the marker sits in (`-->`, `*/`).
ALLOW_RE = re.compile(r"writing-rules:\s*allow\s+(.*?)\s*(?:-->|\*/|$)", re.IGNORECASE)


def vocabulary() -> set[str]:
    """The engine's own names, as registered now (a project's extensions included): every
    check id and parameter, the config's tables and keys, the registry facts, and every old
    name that still loads (`config.RENAMED`)."""
    words = set(manifest.CHECKS) | set(config.REGISTRY_KEYS) | set(config.RENAMED)
    words |= set(config.LAYOUT) | {key for keys in config.LAYOUT.values() for key in keys}
    words |= {"level", "reason", "reasons", "ratchet"}      # a `[checks.<id>]` table's own keys
    for chk in manifest.CHECKS.values():
        words |= set(chk.params) | {f"extend_{k}" for k in chk.params}
    return words


def _name_at(text: str, start: int, end: int) -> str:
    """The run of name characters around `text[start:end]`."""
    while start > 0 and text[start - 1] in NAME_CHARS:
        start -= 1
    while end < len(text) and text[end] in NAME_CHARS:
        end += 1
    return text[start:end]


def _code(text: str, markers) -> list[tuple[int, int]]:
    """Where a Markdown file's code is, as (start, end) offsets: each line of a fenced block,
    and each inline code span on a live line (never one inside an HTML comment)."""
    spans, pos = [], 0
    lines = text.split("\n")
    for line, kind in zip(lines, line_kinds(lines, markers)):
        if kind == FENCE:
            spans.append((pos, pos + len(line)))
        elif kind == LIVE:
            spans += [(pos + m.start(), pos + m.end()) for m in INLINE_CODE_RE.finditer(line)]
        pos += len(line) + 1
    return spans


def _allow_keys(text: str) -> set[str]:
    """The ways a marker's text matches a rule's, casefolded: as written, and without the quotes
    or backticks around it and a trailing `.`, `;` or `,` (`allow "<text>".`)."""
    bare, prev = text.strip(), None
    while bare != prev:
        prev, bare = bare, bare.rstrip(".;,").strip().strip("\"'`").strip()
    return {k.casefold() for k in (text.strip(), bare) if k}


def _allowed(text: str) -> list[tuple[int, int, set[str]]]:
    """Each line that carries an allow marker, as (start, end) offsets and the texts it allows,
    casefolded. Read from the raw line, so a marker counts inside an HTML comment or a code
    comment, which is where one sits."""
    marks, pos = [], 0
    for line in text.split("\n"):
        texts = {k for m in ALLOW_RE.finditer(line) for t in m.group(1).split(",")
                 for k in _allow_keys(t)}
        if texts:
            marks.append((pos, pos + len(line), texts))
        pos += len(line) + 1
    return marks


def _engine_owned(ctx) -> set[Path]:
    """The files whose format the engine owns: every name in them is the engine's to choose."""
    # The config is always left out now (it sits in this tool's own directory); kept here in
    # case that ever changes, so its names are still the engine's.
    owned = [ctx.root / layout.CONFIG]
    if ctx.cfg.profile is not None:
        owned.append(ctx.cfg.profile.path / PRINCIPLES)
    if not ctx.registry.single:
        owned.append(ctx.registry.path)
    return {p.resolve() for p in owned}


def _pattern(glob: str) -> str:
    """A configured glob as it is matched: `config.normalize_glob`, in Unicode's composed form
    (NFC), as every path it is compared with is."""
    return unicodedata.normalize("NFC", config.normalize_glob(glob))


def _spelled(root: Path, rel: str, listed: dict) -> str | None:
    """`rel` spelled as on disk: each component as the directory above it lists it, found by
    Unicode's composed form (NFC), else by case — or None when a directory lists no such name.
    A file system may store a name decomposed (NFD) that a config spells composed, and a
    case-insensitive one answers a literal component of a glob in the glob's own case
    (`Notes/*.md` finds `notes/a.md` as `Notes/a.md`). `listed` caches each directory's names
    across the run, by NFC form and by NFC casefolded."""
    d, out = root, []
    for part in rel.split("/"):
        name = part
        if part not in (".", ".."):
            if d not in listed:
                try:
                    names = os.listdir(d)
                except OSError:
                    names = []
                nfc = {unicodedata.normalize("NFC", n): n for n in names}
                listed[d] = (nfc, {k.casefold(): n for k, n in nfc.items()})
            key = unicodedata.normalize("NFC", part)
            name = listed[d][0].get(key) or listed[d][1].get(key.casefold())
            if name is None:
                return None
        out.append(name)
        d = d / name
    return "/".join(out)


def _on_disk(root: Path, rel: str, listed: dict) -> bool:
    """Whether each component of `rel` is spelled as the directory above it lists it, in either
    Unicode form: what keeps `files` case-sensitive on every OS, as `exclude` is."""
    spelled = _spelled(root, rel, listed)
    return spelled is not None and \
        unicodedata.normalize("NFC", spelled) == unicodedata.normalize("NFC", rel)


def _selected(ctx, params) -> tuple[list[Path], list[str]]:
    """The files `files` names, sorted, and a warning for each entry that matched files but kept
    none. A match is a file `Path.glob` finds whose path `glob_matches` too, spelled as on disk,
    so case counts on every OS. Every match under an always-excluded directory (this tool's own
    and `.claude/` among them, `default_exclude`) is left out, and so is one `exclude` names.
    So is one git ignores, asked of the repo that holds each file (`_ignored`, as for governed
    docs), unless `include_ignored` names it: drafts kept out of git are still checked there.
    An entry whose every file `Path.glob` found is spelled otherwise on disk warns, naming one."""
    exclude = [_pattern(x) for x in params["exclude"]]
    include = [_pattern(x) for x in params["include_ignored"]]
    listed: dict = {}
    other: dict[str, list[str]] = {}
    found: dict[str, list[tuple[Path, bool]]] = {}
    why: dict[str, set[str]] = {}
    matched: dict[str, int] = {}
    for entry in params["files"]:
        pattern = _pattern(entry)
        found[entry], why[entry], matched[entry], other[entry] = [], set(), 0, []
        for p in ctx.root.glob(pattern):
            if not p.is_file():
                continue
            rel = unicodedata.normalize("NFC", p.relative_to(ctx.root).as_posix())
            if not (glob_matches(pattern, rel) and _on_disk(ctx.root, rel, listed)):
                other[entry].append(_spelled(ctx.root, rel, listed) or rel)
                continue
            matched[entry] += 1
            if default_exclude(rel) is not None:
                why[entry].add("always excluded")
            elif any(glob_matches(x, rel) for x in exclude):
                why[entry].add("in exclude")
            else:
                found[entry].append((p, any(glob_matches(x, rel) for x in include)))
    ignored = ctx._ignored([p for paths in found.values() for p, forced in paths if not forced])
    kept: set[Path] = set()
    warnings = []
    for entry, paths in found.items():
        if not matched[entry] and other[entry]:
            warnings.append(f"writing-rules: '{entry}' matches '{min(other[entry])}' only in "
                            f"another spelling")
        mine = [p for p, forced in paths if forced or p not in ignored]
        kept.update(mine)
        if len(mine) < len(paths):
            why[entry].add("gitignored")
        if matched[entry] and not mine:
            msg = (f"writing-rules: '{entry}' matched {matched[entry]} file(s), all left out "
                   f"({', '.join(sorted(why[entry]))})")
            if "gitignored" in why[entry]:
                msg += " — list it in include_ignored to check it anyway"
            warnings.append(msg)
    return sorted(kept), warnings


def _suggest(ctx, s) -> str | None:
    if s.params["rules"] and not s.params["files"]:
        return (f"{len(s.params['rules'])} rule(s) set ({s.source.get('rules', 'engine')}) "
                f"but no files named for them")
    found = [n for n in OUTWARD if (ctx.root / n).exists()]
    if found and s.level == "off":
        return f"outward-facing copy is unchecked: {', '.join(found)}"
    return None


@check("writing-rules", scope="workspace", since="0.4.0", default="off", suggest=_suggest,
       summary="Files matching the configured globs contain none of the configured strings.",
       question="Is there copy with a house style you must enforce (drafts that go to another "
                "project, public docs)? Which files, and which words or characters are out?",
       rationale="A style guide is only a rule if something enforces it. The engine's own "
                 "names (check ids, settings, registry facts) are never flagged where they are "
                 "used as names: in the config, the profile and the registry file, and in "
                 "Markdown code. A project cannot rename them, whichever spelling it enforces. "
                 "Files under this tool's own directory, `.claude/` or an always-excluded "
                 "directory are never checked, nor is a file git ignores unless "
                 "`include_ignored` names it (drafts kept out of git). A spelling "
                 "that must stay (an old name kept for back-compat) is kept by a marker on its "
                 "line: `writing-rules: allow <text>`, in a comment.",
       params={
           "files": Param("list", [], "Globs, relative to the governance root", globs=True),
           "exclude": Param("list", [], "Globs, relative to the governance root, of files "
                                        "`files` matches that are not checked",
                             looser="more", globs=True),
           "include_ignored": Param("list", [], "Globs, relative to the governance root, of "
                                                "files `files` matches that are checked even "
                                                "though git ignores them", globs=True),
           "rules": Param("tables", [], "One per forbidden string",
                          fields={"text": "str", "use": "str", "why": "str", "level": "str",
                                  "ignore_case": "bool"},
                          required=("text",)),
       },
       needs=("files", "rules"))
def writing_rules(ctx, params) -> Findings:
    f = Findings()
    names, owned = vocabulary(), _engine_owned(ctx)
    paths, warnings = _selected(ctx, params)
    for msg in warnings:
        f.warn(msg)
    for path in paths:
        text = read(path)
        rel = ctx.rel(path)
        if path.resolve() in owned:
            code = [(0, len(text))]
        elif path.suffix.lower() == ".md":
            code = _code(text, ctx.markers)
        else:
            code = []
        marks = _allowed(text)
        for rule in params["rules"]:
            found = re.finditer(re.escape(rule["text"]),
                                text, re.IGNORECASE if rule.get("ignore_case") else 0)
            keys = _allow_keys(rule["text"])
            n = sum(1 for m in found
                    if not (any(a <= m.start() and m.end() <= b for a, b in code)
                            and _name_at(text, m.start(), m.end()) in names)
                    and not any(a <= m.start() and m.end() <= b and keys & allowed
                                for a, b, allowed in marks))
            if not n:
                continue
            msg = f"{rel}: {n} × '{rule['text']}'"
            if rule.get("use"):
                msg += f" (use '{rule['use']}')"
            if rule.get("why"):
                msg += f" — {rule['why']}"
            (f.warn if rule.get("level") == "warn" else f.error)(msg)
    return f
