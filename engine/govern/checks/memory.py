"""The operator's Claude Code memory index for this project."""
from __future__ import annotations

from govern.findings import Findings
from govern.manifest import Param, check
from govern.text import claude_project_slug, read


def memory_index_path(ctx):
    return (ctx.home / ".claude" / "projects" / claude_project_slug(ctx.root) / "memory" /
            "MEMORY.md")


@check("memory-index", scope="workspace", since="0.4.0",
       summary="Bounds the Claude Code memory index for this project.",
       question="How many lines may the memory index hold before it needs pruning?",
       rationale="The index is loaded into every session; measure what you want bounded.",
       params={"max_lines": Param("int", 40, "Non-blank lines in MEMORY.md", looser="higher")})
def memory_index(ctx, params) -> Findings:
    f = Findings()
    path = memory_index_path(ctx)
    if not path.exists():
        return f
    n = len([ln for ln in read(path).splitlines() if ln.strip()])
    if n > params["max_lines"]:
        f.warn(f"memory index: {n} non-blank lines exceeds "
               f"memory-index max_lines={params['max_lines']} ({path})")
    return f
