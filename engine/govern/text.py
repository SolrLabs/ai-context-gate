"""Reading governed files: text, frontmatter, word counts, generated-block markers."""
from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Text:
    """A file's text, or why it could not be read. A decode failure is reported as one, never
    silently read as an empty file that then has "no frontmatter"."""
    text: str
    error: str | None = None


_overlay: dict[Path, str] = {}


@contextlib.contextmanager
def overlay(mapping: dict[Path, str]):
    """Serve `mapping`'s text for these paths instead of reading them from disk, for every
    engine read that goes through `read`/`read_text` — so a before/after comparison (`govern
    migrate`'s report) sees what a project would look like once edits land, without writing
    anything ("never read the wrong thing silently" applies here too: a check that reads a
    project file some other way would see stale disk contents instead)."""
    global _overlay
    previous = _overlay
    _overlay = {**previous, **mapping}
    try:
        yield
    finally:
        _overlay = previous


def has_bom(path: Path) -> bool:
    """Whether the file on disk currently opens with a UTF-8 BOM — checked directly on disk,
    never through the overlay, since it is about what is really there before a write, not about
    a planned edit's text."""
    try:
        return path.read_bytes()[:3] == b"\xef\xbb\xbf"
    except OSError:
        return False


def read_text(path: Path) -> Text:
    if path in _overlay:
        return Text(_overlay[path])
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return Text("", f"unreadable ({exc.strerror})")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return Text("", f"not valid UTF-8 (byte {exc.start})")
    return Text(text.removeprefix("﻿"))


class Unreadable(Exception):
    """A governed file that cannot be read. Never treated as empty: an empty decision log reads
    as "no breaches" to the ratchet and as "no ids used" to next-id."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


def read(path: Path) -> str:
    got = read_text(path)
    if got.error:
        raise Unreadable(path, got.error)
    return got.text


def write(path: Path, text: str) -> None:
    """Exactly these characters: no newline translation, so a file written on Windows reads
    back byte for byte."""
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def eol(text: str) -> str:
    """The line ending a file uses, so an edit keeps it (git may check files out with CRLF)."""
    return "\r\n" if "\r\n" in text else "\n"


# ---------------------------------------------------------------------------- frontmatter

class Frontmatter:
    """The YAML subset governed docs use: scalars, inline lists and block lists.

    Nested mappings are skipped, never merged over a top-level key, and only a matching pair of
    quotes is removed. Keys keep their case: `maxTurns` is not `maxturns`.
    """

    def __init__(self, scalars: dict[str, str], lists: dict[str, list[str]]) -> None:
        self.scalars = scalars
        self.lists = lists

    def __bool__(self) -> bool:
        return bool(self.scalars or self.lists)

    def get(self, key: str, default=None):
        if key in self.scalars:
            return self.scalars[key]
        if key in self.lists:
            return self.lists[key]
        return default

    def get_list(self, key: str) -> list[str]:
        """A list, with a lone scalar read as a one-item list rather than as characters."""
        if key in self.lists:
            return self.lists[key]
        value = self.scalars.get(key)
        return [value] if value else []


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_frontmatter(text: str) -> tuple[Frontmatter, str]:
    """(frontmatter, body). Empty frontmatter when the file has no closed `---` block.

    The body starts right after the closing `---` line's text, so it begins with that line's
    newline, as every consumer (word counts, `show`) expects.
    """
    lines = text.split("\n")
    if not lines or lines[0].rstrip("\r").strip() != "---":
        return Frontmatter({}, {}), text
    closing = next((i for i in range(1, len(lines)) if lines[i].rstrip("\r").strip() == "---"), -1)
    if closing < 0:
        return Frontmatter({}, {}), text

    scalars: dict[str, str] = {}
    lists: dict[str, list[str]] = {}
    pending_key: str | None = None
    pending: list[str] | None = None
    block_key: str | None = None       # a `|` or `>` block scalar being collected
    block: list[str] = []
    folded = False

    def flush() -> None:
        nonlocal block_key
        if pending_key is not None and pending:
            lists[pending_key] = pending
        if block_key is not None:
            scalars[block_key] = (" " if folded else "\n").join(block).strip()
            block_key = None

    for raw in lines[1:closing]:
        raw = raw.rstrip("\r")
        trimmed = raw.lstrip()
        indented = len(raw) != len(trimmed)
        if block_key is not None and (indented or not raw.strip()):
            block.append(trimmed)
            continue
        if not raw.strip() or trimmed.startswith("#"):
            continue
        # A block list item, indented or not (PyYAML writes them flush with the key).
        if trimmed.startswith("- ") and pending is not None:
            pending.append(_unquote(trimmed[2:].strip()))
            continue
        sep = trimmed.find(":")
        if sep <= 0 or indented:
            continue
        flush()
        pending_key, pending = None, None
        key, value = trimmed[:sep].strip(), trimmed[sep + 1:].strip()
        if not value:
            pending_key, pending = key, []
            continue
        if value[0] in "|>" and value.rstrip("+-") in ("|", ">"):
            block_key, block, folded = key, [], value[0] == ">"
            continue
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            items = [_unquote(v.strip()) for v in inner.split(",") if v.strip()] if inner else []
            lists[key] = [v for v in items if v]
            continue
        scalars[key] = _unquote(value)
    flush()
    body_start = sum(len(line) + 1 for line in lines[:closing]) + len(lines[closing])
    return Frontmatter(scalars, lists), text[body_start:]


# ---------------------------------------------------------------------------- markers

class Markers:
    """Generated-block markers. The prefix is policy; this is its only spelling, so no check
    repeats it as a regex literal of its own."""

    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        p = re.escape(prefix)
        self._block_re = re.compile(
            rf"<!-- {p}:generated:start id=[\w-]+ -->.*?<!-- {p}:generated:end id=[\w-]+ -->",
            re.S)

    def open(self, bid: str) -> str:
        return f"<!-- {self.prefix}:generated:start id={bid} -->"

    def close(self, bid: str) -> str:
        return f"<!-- {self.prefix}:generated:end id={bid} -->"

    def strip_blocks(self, text: str) -> str:
        return self._block_re.sub("", text)


COMMENT_RE = re.compile(r"<!--.*?-->", re.S)


def word_count(text: str, markers: Markers) -> int:
    """Words a reader actually reads: HTML comments and generated blocks excluded."""
    return len(markers.strip_blocks(COMMENT_RE.sub("", text)).split())


def claude_project_slug(root: Path) -> str:
    """The directory name Claude Code keeps a project's memory under: the absolute path with
    every character other than a letter or digit replaced by `-`. Derived, never written as a
    literal."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(root))
