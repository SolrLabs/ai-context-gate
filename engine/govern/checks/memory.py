"""The operator's Claude Code memory index for this project."""
from __future__ import annotations

import re

from govern.findings import Findings
from govern.manifest import Param, check
from govern.text import claude_project_slug, read, read_text

LINK = re.compile(r"\[[^\]]*\]\(\s*<?([^)\s>]+)")
KIND = re.compile(r"^[ \t]*type[ \t]*:[ \t]*(.*?)[ \t]*$", re.MULTILINE)


def memory_index_path(ctx):
    return (ctx.home / ".claude" / "projects" / claude_project_slug(ctx.root) / "memory" /
            "MEMORY.md")


def memory_links(line):
    """The targets of the markdown links on one index line, in order."""
    return [m.group(1) for m in LINK.finditer(line)]


def is_memory_file(target):
    return "://" not in target and target.split("#", 1)[0].lower().endswith(".md")


def memory_kind(index, line):
    """The `type` in the frontmatter of the memory file the line's first link names, or None
    when there is no link, the file is missing, unreadable or outside the index's folder, or it
    has no kind. A regex over the frontmatter text, not a YAML parser."""
    links = memory_links(line)
    if not links:
        return None
    folder = index.parent.resolve()
    try:
        target = (folder / links[0].split("#", 1)[0]).resolve()
        target.relative_to(folder)
        got = read_text(target)
    except (OSError, ValueError):
        return None
    if got.error:
        return None
    lines = got.text.splitlines()
    marks = [i for i, ln in enumerate(lines) if ln.strip() == "---"]
    if len(marks) < 2:
        return None
    found = KIND.search("\n".join(lines[marks[0] + 1:marks[1]]))
    kind = found.group(1).strip().strip("\"'").strip() if found else ""
    return kind or None


@check("memory-index", scope="workspace", since="0.4.0",
       summary="Bounds the Claude Code memory index for this project, one memory per line.",
       question="How many lines may the memory index hold before it needs pruning?",
       rationale="The index is loaded into every session, so it counts memories, not lines, "
                 "and leaves out the kinds that never go stale.",
       params={"max_lines": Param("int", 40, "Counted non-blank lines in MEMORY.md",
                                  looser="higher"),
               "exempt_types": Param("list", ["feedback"],
                                     "Memory kinds whose lines are not counted", looser="more"),
               "one_link_per_line": Param("bool", True,
                                          "Warn when a line links more than one memory")})
def memory_index(ctx, params) -> Findings:
    f = Findings()
    path = memory_index_path(ctx)
    if not path.exists():
        return f
    exempt_types = list(params["exempt_types"])
    n = exempt = 0
    for i, line in enumerate(read(path).splitlines(), 1):
        if not line.strip():
            continue
        if params["one_link_per_line"]:
            k = len([t for t in memory_links(line) if is_memory_file(t)])
            if k > 1:
                f.warn(f"memory index: line {i} links {k} memories — one memory per line, "
                       f"so the limit counts memories ({path}); an engine before 0.7.0 counts "
                       f"lines instead, so splitting a line can put it over max_lines there")
        if exempt_types and memory_kind(path, line) in exempt_types:
            exempt += 1
        else:
            n += 1
    if n > params["max_lines"]:
        if exempt:
            f.warn(f"memory index: {n} lines exceeds memory-index max_lines="
                   f"{params['max_lines']} ({exempt} more are exempt: "
                   f"{', '.join(exempt_types)}) ({path})")
        else:
            f.warn(f"memory index: {n} non-blank lines exceeds "
                   f"memory-index max_lines={params['max_lines']} ({path})")
    return f
