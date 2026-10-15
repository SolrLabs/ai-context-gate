"""Local branches that hold nothing git lacks: named, never deleted."""
from __future__ import annotations

from fnmatch import fnmatchcase

from govern import build, phase
from govern.findings import Findings
from govern.manifest import Param, check

# At most this many names are written out; the rest are counted.
_NAMED = 10
# What stands for the branch when HEAD is detached: no branch has this name.
_DETACHED = "(detached HEAD)"


def _checked_out(root) -> set[str]:
    """Every branch some worktree of this repository has checked out, this one included."""
    out = phase._ask(root, "worktree", "list", "--porcelain")
    return {line[len("branch refs/heads/"):] for line in out.splitlines()
            if line.startswith("branch refs/heads/")}


@check("merged-branches", scope="workspace", since="0.7.0", default="warn",
       summary="Local branches already merged into the default branch are named",
       question="Is a branch still here after its work was merged?",
       rationale="A merged branch holds nothing git lacks, and a pile of them hides the ones "
                 "that matter.",
       params={
           "keep": Param("list", ["main", "master", "development", "beta-*", "release-*"],
                         "Branch names never reported", looser="more"),
       })
def merged_branches(ctx, params) -> Findings:
    """A branch whose tip is an ancestor of the default branch's tip, other than the default
    branch, any branch a worktree has checked out, and a name `keep` matches. It names them; it
    deletes nothing. A folder git cannot answer for, or a default branch that cannot be told,
    is one warning, never an empty answer."""
    f = Findings()
    try:
        current = phase._ask(ctx.root, "branch", "--show-current") or _DETACHED
        default, refs = build.default_branch(ctx.root, current)
        if not refs:
            f.warn(f"merged branches not checked: {default} is not known here")
            return f
        merged = phase._ask(ctx.root, "for-each-ref", "--merged", refs[0],
                            "--format=%(refname:short)", "refs/heads/").splitlines()
        skip = _checked_out(ctx.root)
    except (phase.PhaseError, build.BuildError) as exc:
        f.warn(f"merged branches not checked: {exc}")
        return f
    skip |= {default, current}
    named = sorted({b.strip() for b in merged if b.strip()} - skip)
    named = [b for b in named if not any(fnmatchcase(b, p) for p in params["keep"])]
    if not named:
        return f
    shown = ", ".join(named[:_NAMED])
    more = f" and {len(named) - _NAMED} more" if len(named) > _NAMED else ""
    plural = "branch is" if len(named) == 1 else "branches are"
    f.warn(f"{len(named)} local {plural} merged into {default} and can be deleted: "
           f"{shown}{more}")
    return f
