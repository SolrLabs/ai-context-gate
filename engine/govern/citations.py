"""Where docs, agent definitions, build briefs and code cite a decision or trap id."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from govern import decisions
from govern.text import Unreadable, read


@dataclass(frozen=True)
class Citation:
    path: Path
    line: int
    ident: str


def prefixes(ctx) -> list[str]:
    """The id prefixes this project numbers entries with: each scope's, the workspace's, the
    trap prefix. Anything else shaped like an id (`UTF-8`) is not one."""
    found = [s.id_prefix for s in ctx.registry.scopes if s.id_prefix]
    if ctx.registry.workspace_prefix:
        found.append(ctx.registry.workspace_prefix)
    found.append(ctx.trap_prefix)
    return sorted(set(found))


def files(ctx, code_globs) -> list[Path]:
    """Every file that may cite an id, each once."""
    out: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        if path.is_file() and path not in seen:
            seen.add(path)
            out.append(path)

    for s in ctx.project_scopes:
        if s.governed and s.gov:
            for _, p in ctx.governed_docs(s.gov):
                add(p)
    for p in ctx.workspace_docs():
        add(p)
    for p in sorted((ctx.root / ".claude" / "agents").glob("*.md")):
        add(p)
    for p in sorted((ctx.root / ".context-gate" / "build").rglob("*.md")):
        add(p)
    for pattern in code_globs:
        for p in sorted(ctx.root.glob(pattern)):
            add(p)
    return out


def scan(ctx, code_globs=(), unreadable: list | None = None) -> list[Citation]:
    """Every citation of an id in those files, sorted by path then line. A file that cannot be
    read raises `Unreadable`; with `unreadable` given, it is appended there and skipped."""
    names = prefixes(ctx)
    if not names:
        return []
    cite_re = re.compile(r"\b(" + "|".join(re.escape(n) for n in names) + r")-(\d+)\b")
    found: list[Citation] = []
    for path in files(ctx, code_globs):
        try:
            lines = read(path).split("\n")
        except Unreadable as exc:
            if unreadable is None:
                raise
            unreadable.append(exc)
            continue
        live = decisions.mask_lines(lines, ctx.markers)
        for i, line in enumerate(lines):
            if not live[i] or ctx.grammar.entry_re.match(line.rstrip("\r")):
                continue
            for m in cite_re.finditer(line):
                found.append(Citation(path, i + 1, m.group(0)))
    return sorted(found, key=lambda c: (str(c.path), c.line))
