"""Release notes: the CHANGELOG.md that ships inside the engine."""
from __future__ import annotations

import re
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parent / "CHANGELOG.md"
HEADING = re.compile(r"^## (\d+\.\d+\.\d+)\b.*$", re.M)


def _key(version: str) -> tuple:
    return tuple(int(p) for p in version.split("."))


def entries(text: str | None = None) -> list[tuple[str, str]]:
    """(version, the entry's markdown, heading included), newest first as written."""
    text = CHANGELOG.read_text(encoding="utf-8") if text is None else text
    heads = list(HEADING.finditer(text))
    out = []
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        out.append((m.group(1), text[m.start():end].strip()))
    return out


def between(old: str | None, new: str, text: str | None = None) -> list[tuple[str, str]]:
    """Every entry after `old` up to and including `new`: what an upgrade from old to new
    brings. With no known old version, just `new`'s own entry."""
    lo = _key(old) if old and re.fullmatch(r"\d+\.\d+\.\d+", old) else _key(new)
    inclusive_lo = not (old and re.fullmatch(r"\d+\.\d+\.\d+", old))
    return [(v, body) for v, body in entries(text)
            if (lo <= _key(v) if inclusive_lo else lo < _key(v)) and _key(v) <= _key(new)]
