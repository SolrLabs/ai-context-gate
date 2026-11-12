"""Where docs, agent definitions, build briefs and code cite a decision or trap id."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from govern import decisions
from govern.text import Unreadable, read


# `decision-citations: allow T-3, D-42`: ids written as examples. The ids run to the end of the
# line, or to the end of the comment the marker sits in; the marker covers its own line and the
# line below it.
ALLOW_RE = re.compile(r"decision-citations:\s*allow\s+(.*?)\s*(?:-->|\*/|--\}\}|$)",
                      re.IGNORECASE)


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


def _allowed(lines: list[str]) -> dict[int, set]:
    """The ids each line may cite without a finding: those a marker names on that line or the
    line above, as `decisions.id_key` keys (so `T-3` and `t-003` are one id). Read from the raw
    lines, so a marker counts inside an HTML comment, which is where one sits."""
    out: dict[int, set] = {}
    for i, line in enumerate(lines):
        ids = {decisions.id_key(t.strip()) for m in ALLOW_RE.finditer(line)
               for t in m.group(1).split(",")}
        ids.discard(None)
        if ids:
            for j in (i, i + 1):
                out.setdefault(j, set()).update(ids)
    return out


def scan(ctx, code_globs=(), unreadable: list | None = None,
         example_ids=()) -> list[Citation]:
    """Every citation of an id in those files, sorted by path then line, minus the ids a marker
    allows there (`ALLOW_RE`) and `example_ids` everywhere. A file that cannot be read raises
    `Unreadable`; with `unreadable` given, it is appended there and skipped."""
    names = prefixes(ctx)
    examples = {decisions.id_key(i) for i in example_ids}
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
        allowed = _allowed(lines)
        for i, line in enumerate(lines):
            if not live[i] or ctx.grammar.entry_re.match(line.rstrip("\r")):
                continue
            for m in cite_re.finditer(line):
                key = decisions.id_key(m.group(0))
                if key in examples or key in allowed.get(i, ()):
                    continue
                found.append(Citation(path, i + 1, m.group(0)))
    return sorted(found, key=lambda c: (str(c.path), c.line))
