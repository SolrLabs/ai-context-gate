"""Decision logs and trap files: entries under `## P-N - Title` headings.

The heading grammar is a dialect setting: `any-dash`, the standard, reads a hyphen, an en dash
or an em dash there. Whatever the grammar, three things hold:

- a heading-shaped line the grammar rejects is reported, never skipped;
- headings inside code fences and HTML comments are examples, not entries;
- an entry ends at the next heading of its own level or higher, so a trailing `## Notes` section
  is not counted as part of the last decision.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from itertools import chain
from pathlib import Path

from govern.text import Markers, read, word_count

# Named grammars. `level` is the heading depth of an entry.
GRAMMARS = {
    # `## O-107 — Title`, an em dash with whitespace on both sides.
    "em-dash": {"level": 2, "sep": r"\s+—\s+"},
    # Canonical: `## O-107 - Title`, any dash (hyphen, en or em), spacing optional.
    "any-dash": {"level": 2, "sep": r"\s*[—–-]\s*"},
}

# What sits between an id and its title in a heading the engine writes when no entry in the
# project says otherwise: plain ASCII, and read by every grammar but `em-dash`.
DEFAULT_SEP = " - "
# The dash an entry heading was written with: the first one after its id.
HEADING_DASH_RE = re.compile(r"^#+ [A-Za-z]+-\d+\s*([—–-])")


def heading_dash(heading: str) -> str:
    """The separator one entry heading was written with (` — `, ` – ` or ` - `), a hyphen where
    it has no dash."""
    m = HEADING_DASH_RE.match(heading)
    return f" {m.group(1)} " if m else DEFAULT_SEP


def heading_sep(grammar: "Grammar", text: str, others: Iterable[str] = ()) -> str:
    """Which separator a new entry heading gets in the file whose text is `text`: one of
    ` — `, ` – ` or ` - `, so the engine never writes a heading the project's own writing
    rules would fail.

    Under the `em-dash` grammar, ` — `: it reads nothing else. Otherwise the dash of the last
    entry heading already in the file; failing that, of the last entry in the first of `others`
    that has one (the texts of the scope's other log and trap files, read only when needed);
    failing that, `DEFAULT_SEP`. A heading inside a code fence or an HTML comment is an example,
    not an entry, and decides nothing."""
    if grammar.name == "em-dash":
        return " — "
    for candidate in chain([text], others):
        entries = grammar.parse(candidate).entries
        if entries:
            m = HEADING_DASH_RE.match(entries[-1].heading)
            if m:
                return f" {m.group(1)} "
    return DEFAULT_SEP

STATUS_RE = re.compile(r"\*\*Status:\*\*\s*(\w+)")
TOPIC_RE = re.compile(r"\*\*Topic:\*\*[ \t]*(.+)")
POINTER_RE = re.compile(r"^Replaced by ([A-Za-z]+-\d+)\s*$")


def id_key(ident: str) -> tuple[str, int] | None:
    """An id as (PREFIX, number): `D-019` and `D-19` are the same id."""
    m = ID_RE.fullmatch(ident.strip())
    return (m.group(1).upper(), int(m.group(2))) if m else None
BITES_RE = re.compile(r"\*\*Bites when:\*\*[ \t]*(.*)")
FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
INLINE_CODE_RE = re.compile(r"(`+).*?\1")
ID_RE = re.compile(r"([A-Za-z]+)-(\d+)")


def _bites(body: str) -> str:
    """The **Bites when:** value, through the end of its paragraph: a value that wraps onto the
    next lines is read whole, never cut at the first line break."""
    m = BITES_RE.search(body)
    if not m:
        return ""
    parts = [m.group(1).strip()]
    for line in body[m.end():].split("\n")[1:]:
        if not line.strip() or line.lstrip().startswith(("**", "#", "- ", "* ", "|", "<!--")):
            break
        parts.append(line.strip())
    return " ".join(p for p in parts if p)


def _topic_words(lines: list[str], live: list[bool], start: int, end: int) -> int:
    """Words on the entry's own **Topic:** line: metadata, not text a reader pays for, the same
    treatment word_count gives frontmatter and generated blocks. A Topic-shaped line inside a
    masked block (a code fence, say) is an example, not metadata, and counts as ordinary body
    text."""
    for j in range(start, end):
        if live[j] and TOPIC_RE.match(lines[j]):
            return len(lines[j].split())
    return 0


def mask_lines(lines: list[str], markers: Markers) -> list[bool]:
    """Which lines are live markdown: not inside a fenced code block, an HTML comment, or a
    generated block. Lines are masked, never removed, so reported line numbers are real ones.

    Fences close only on the same character at least as long as the opening (CommonMark), and
    a comment or marker mentioned inside inline code is text, not markup.
    """
    return [kind == LIVE for kind in line_kinds(lines, markers)]


# What `line_kinds` says a line is: live markdown, or masked as a fenced code block (its fence
# lines included), an HTML comment, or a generated block (its marker lines included).
LIVE, FENCE, COMMENT, BLOCK = "live", "fence", "comment", "block"


def line_kinds(lines: list[str], markers: Markers) -> list[str]:
    """`mask_lines`, saying why a line is masked: for a scan that treats code differently
    from other masked text (writing-rules)."""
    bid = r"[\w-]+"
    open_marker = re.escape(markers.open("X")).replace("X", bid)
    close_marker = re.escape(markers.close("X")).replace("X", bid)
    open_re = re.compile(rf"^\s*{open_marker}\s*$")
    close_re = re.compile(rf"^\s*{close_marker}\s*$")
    kinds: list[str] = []
    fence: str | None = None
    in_comment = in_block = False
    for line in lines:
        if fence is not None:
            kinds.append(FENCE)
            m = FENCE_RE.match(line)
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) \
                    and not line.strip().strip(fence[0]):
                fence = None
            continue
        if in_block:
            kinds.append(BLOCK)
            in_block = not close_re.match(line)
            continue
        starts_in_comment = in_comment
        scan = INLINE_CODE_RE.sub("", line)
        pos = 0
        while True:
            if in_comment:
                j = scan.find("-->", pos)
                if j < 0:
                    break
                in_comment, pos = False, j + 3
            else:
                j = scan.find("<!--", pos)
                if j < 0:
                    break
                in_comment, pos = True, j + 4
        if starts_in_comment:
            kinds.append(COMMENT)
            continue
        if open_re.match(line):
            kinds.append(BLOCK)
            in_block, in_comment = True, False
            continue
        m = FENCE_RE.match(line)
        if m:
            kinds.append(FENCE)
            fence = m.group(1)
            continue
        kinds.append(COMMENT if line.lstrip().startswith("<!--") else LIVE)
    return kinds


@dataclass
class Entry:
    prefix: str
    num: int
    title: str
    body: str
    line: int
    last_line: int
    status: str | None
    words: int
    fields: dict[str, bool] = field(default_factory=dict)
    bites: str = ""
    raw_num: str = ""                 # the number as written, padding and all
    topic: str | None = None          # `**Topic:**`, which the index groups by
    replaced_by: str | None = None    # a one-line pointer, `## D-3 - Replaced by D-40`
    heading: str = ""                 # the heading line as written, its own dash and spacing

    @property
    def ident(self) -> str:
        """As written in the log, so a reader can grep for exactly what they see."""
        return f"{self.prefix}-{self.raw_num or self.num}"

    @property
    def key(self) -> tuple[str, int]:
        return (self.prefix, self.num)


@dataclass
class Parsed:
    entries: list[Entry]
    malformed: list[tuple[int, str]]

    def used_numbers(self, prefix: str) -> set[int]:
        """Every number this file has spent under a prefix, malformed headings included: an id
        a typo hides is still an id somebody may cite."""
        used = {e.num for e in self.entries if e.prefix == prefix}
        for _, heading in self.malformed:
            m = ID_RE.search(heading)
            if m and m.group(1).upper() == prefix:
                used.add(int(m.group(2)))
        return used


class Grammar:
    def __init__(self, name: str, required_fields: list[str], markers: Markers) -> None:
        g = GRAMMARS[name]
        self.name = name
        self.level = g["level"]
        hashes = "#" * self.level
        self.entry_re = re.compile(rf"^{hashes} ([A-Z]+)-(\d+){g['sep']}(.+)$")
        # Shaped like an entry at any heading depth: an id, then a separator or nothing. A
        # section titled `## UTF-8 and BOMs` is not one.
        self.entry_like_re = re.compile(r"^#{1,6}\s+[A-Za-z]+-\d+\s*(?:[—–:-]|$)")
        self.stop_re = re.compile(rf"^#{{1,{self.level}}}\s")
        self.required_fields = required_fields
        self.markers = markers

    def parse(self, text: str) -> Parsed:
        lines = text.split("\n")
        starts, offset = [], 0
        for line in lines:
            starts.append(offset)
            offset += len(line) + 1

        live = mask_lines(lines, self.markers)

        heads, malformed = [], []
        for i, line in enumerate(lines):
            if not live[i]:
                continue
            m = self.entry_re.match(line.rstrip("\r"))
            if m:
                heads.append((i, m))
            elif self.entry_like_re.match(line):
                malformed.append((i + 1, line.strip()))
        lines_total = len(text.splitlines())

        entries = []
        for i, m in heads:
            end, last = len(text), lines_total
            for j in range(i + 1, len(lines)):
                if live[j] and self.stop_re.match(lines[j]):
                    end, last = starts[j], j
                    break
            body = self.markers.strip_blocks(text[starts[i] + len(lines[i]):end])
            status = STATUS_RE.search(body)
            bites = _bites(body)
            topic = TOPIC_RE.search(body)
            title = m.group(3).strip()
            pointer = POINTER_RE.match(title)
            entries.append(Entry(
                prefix=m.group(1), num=int(m.group(2)), title=title, body=body,
                raw_num=m.group(2), topic=topic.group(1).strip() if topic else None,
                replaced_by=pointer.group(1) if pointer else None,
                line=i + 1, last_line=last, status=status.group(1).lower() if status else None,
                heading=lines[i].rstrip(),
                words=word_count(body, self.markers) - _topic_words(lines, live, i + 1, last),
                fields={f: f"**{f}:**" in body for f in self.required_fields},
                bites=bites))
        return Parsed(entries, malformed)

    def parse_file(self, path: Path) -> Parsed:
        return self.parse(read(path))

    def traps(self, path: Path, prefix: str = "T") -> list[Entry]:
        return [e for e in self.parse_file(path).entries if e.prefix == prefix]
