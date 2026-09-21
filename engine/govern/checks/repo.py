"""The governance root is inside a git repository, which the history-based checks rely on."""
from __future__ import annotations

from govern.context import repo_of
from govern.findings import Findings
from govern.manifest import check


@check("git-repo", scope="workspace", since="0.4.1", default="warn",
       summary="The governance root is inside a git repository.",
       question="Is this project a git repository? Several checks read its history.",
       rationale="Outside git, the checks that read history or ignore rules (when a file was "
                 "last touched, whether it was ever committed, what git ignores) quietly check "
                 "nothing; the gate would look green while not looking.")
def git_repo(ctx, params) -> Findings:
    f = Findings()
    if not (ctx.root / ".git").exists() and repo_of(ctx.root) is None:
        f.warn(f"{ctx.root.name}: not inside a git repository, so the checks that read git "
               f"history or ignore rules check nothing — run `git init` and commit")
    return f
