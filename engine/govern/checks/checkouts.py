"""Checkouts of other repositories that a governance root tracks (forks it contributes to, repos
it publishes): none of the root's own files inside them, and the upstream remote where the
registry says; and, wherever agents run, that a checkout's own repo ignores the worktrees they
create."""
from __future__ import annotations

import subprocess
from pathlib import Path

from govern.context import git, git_env
from govern.findings import Findings
from govern.manifest import Param, check


def _hint(params) -> str:
    return f" — {params['setup_hint']}" if params["setup_hint"] else ""


@check("checkout-hygiene", scope="project", since="0.4.0", default="off",
       summary="Checkouts in the configured registry roles carry none of the governance root's "
               "own files, and their upstream remote matches the registry.",
       question="Do you govern checkouts of repositories you don't want your governance files "
                "to land in (a fork you send pull requests from, a repo you publish)? Which "
                "registry roles are they, and which files must never appear in them?",
       rationale="A governance file inside a fork lands in somebody else's pull request; inside "
                 "a published repo it lands in a release. Both failures are silent.",
       params={
           "roles": Param("list", [], "Registry roles whose checkouts are checked"),
           "forbidden": Param("list", [], "Paths, relative to a checkout, that never belong "
                                          "in it", looser="fewer"),
           "rules": Param("table", {}, "Decision id to cite per role, e.g. "
                                       "{ contribute = \"W-1\" }"),
           "upstream_remote": Param("str", "upstream", "Remote whose URL must equal the "
                                                       "registry's upstream"),
           "setup_hint": Param("str", "", "What to run when a checkout or remote is missing"),
       })
def checkout_hygiene(ctx, params, scope) -> Findings:
    f = Findings()
    role = scope.get("role")
    if role not in params["roles"]:
        return f
    name, d = scope.name, scope.get("dir", "")
    checkout = ctx.root / d
    if not checkout.is_dir():
        f.warn(f"{name}: checkout '{scope.get('dir')}' not present{_hint(params)}")
        return f
    if not (checkout / ".git").exists():
        f.error(f"{name}: '{scope.get('dir')}' exists but is not a git repo")
        return f
    rule = params["rules"].get(role, "")
    cite = f" ({rule})" if rule else ""
    for rel in params["forbidden"]:
        if (checkout / rel).exists():
            if git(checkout, "ls-files", "--error-unmatch", rel):
                f.error(f"{name}: '{rel}' is tracked inside the checkout — "
                        f"our files never go there{cite}")
            else:
                f.warn(f"{name}: '{rel}' is present but untracked inside the checkout{cite}")
    want = scope.get("upstream", "")
    remote = params["upstream_remote"]
    if want:
        have = git(checkout, "remote", "get-url", remote)
        if have is None:
            f.error(f"{name}: no '{remote}' remote{_hint(params)}")
        elif have.rstrip("/") != want.rstrip("/"):
            f.error(f"{name}: {remote} remote is '{have}', registry says '{want}'")
    return f


# Claude Code creates an agent's worktree under `.claude/worktrees/` at the top of the repo it
# works in; this probe path stands for any of them.
_WORKTREE_PROBE = ".claude/worktrees/x"


def _checkout(ctx, scope) -> Path:
    return ctx.root if scope.is_workspace else ctx.root / scope.get("dir", "")


def _has_agents(path: Path) -> bool:
    return (path / ".claude" / "agents").is_dir()


def _own_repo(ctx, checkout: Path) -> bool:
    """Whether `checkout` answers for itself: the root, or a directory with a `.git` of its own.
    Anything else lives in the root's repo, whose worktrees land at the root's top."""
    return checkout.resolve() == ctx.root.resolve() or (checkout / ".git").exists()


def _check_ignore(checkout: Path) -> tuple[int | None, str]:
    """Exit code of `git check-ignore -q` for the probe, asked of `checkout`'s own repo, and
    git's own words when it failed. Not `context.git()`, which folds exit 1 (not ignored) into
    the same None as a real failure. The user's global excludes file is switched off, so a
    machine-local rule never hides what every other clone lacks. A git that could not run at
    all is `(None, reason)`."""
    try:
        res = subprocess.run(["git", "-C", str(checkout), "-c", "core.excludesFile=",
                              "check-ignore", "-q", _WORKTREE_PROBE],
                             capture_output=True, timeout=20, env=git_env())
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)
    return res.returncode, res.stderr.decode("utf-8", errors="replace").strip()


@check("agent-worktrees", scope="project", since="0.4.0", default="warn", also_workspace=True,
       summary="Where agents run, each checkout's own repo ignores `.claude/worktrees/`, so an "
               "agent's worktree is never committed into it.",
       question="Do agents in this workspace work in their own worktrees (Claude Code's "
                "`isolation: \"worktree\"`), and should each checkout's repo keep those "
                "worktrees out of its commits?",
       rationale="An agent worktree is a nested checkout under `.claude/worktrees/`; a repo that "
                 "does not ignore it sweeps it into the next `git add -A` as an embedded repo, "
                 "silently. A user's global excludes file does not count, since other clones "
                 "lack it; a repo's own `.git/info/exclude` still does, but only `.gitignore` "
                 "protects every clone, so that is the fix.")
def agent_worktrees(ctx, params, scope) -> Findings:
    """Fires when the workspace root or this scope's checkout has `.claude/agents/`. Each
    checkout is asked about in its own repo, never the root's: a member's `.gitignore` is what
    decides what its own `git add -A` takes. A member directory with no repo of its own lives
    inside the root's repo, where its agents' worktrees land at the root's top, so the root's
    pass fires for it and a member pass never runs. A root that is not a git repo at all (a plain
    folder of repos) has nothing to ignore."""
    f = Findings()
    checkout = _checkout(ctx, scope)
    root = ctx.root.resolve()
    at_root = checkout.resolve() == root
    if scope.is_workspace and any(_checkout(ctx, s).resolve() == root
                                  for s in ctx.registry.scopes):
        return f           # a project scope is this same checkout (a single repo): asked once
    if not checkout.is_dir() or not _own_repo(ctx, checkout):
        return f
    if at_root:
        inside = [c for c in (_checkout(ctx, s) for s in ctx.registry.scopes)
                  if c.is_dir() and not _own_repo(ctx, c)]
        if not _has_agents(ctx.root) and not any(_has_agents(c) for c in inside):
            return f
    elif not _has_agents(ctx.root) and not _has_agents(checkout):
        return f
    where = "." if at_root else scope.get("dir", "")
    code, detail = _check_ignore(checkout)
    if code == 0:
        return f
    if code == 1:
        f.warn(f"{scope.name}: '{where}' does not ignore .claude/worktrees/; an agent worktree "
               f"there is swept into `git add -A`")
    elif at_root and "not a git repository" in detail:
        return f
    else:
        why = detail.splitlines()[0] if detail else f"exit {code}"
        f.warn(f"{scope.name}: could not ask git whether '{where}' ignores .claude/worktrees/ "
               f"({why})")
    return f
