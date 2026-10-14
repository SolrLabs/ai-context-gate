"""`govern phase close`: push the current branch, check the remote holds it, and only then remove
the local branches and worktrees the pushed commit already contains.

It never opens a pull request and never merges, and it needs no project: any git repository.
`plan` only reads; `apply` does the push and the removals. The only deleting forms it can build
are `git branch -d` and a plain `git worktree remove`: git itself refuses to drop unmerged work
or a worktree with changes, and that refusal is reported, never forced.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from govern.context import GIT_TIMEOUT, git_env

PUSH_TIMEOUT = 120   # a push moves objects; the seconds a local question gets are too few


class PhaseError(Exception):
    """Something that stops the command before it changes anything."""


@dataclass
class Plan:
    repo: Path
    branch: str = ""
    commit: str = ""
    remote: str = ""
    target: str = ""                  # the branch on the remote the push goes to
    has_upstream: bool = False
    default: str = ""                 # the remote's default branch
    removals: list[tuple[str, str | None]] = field(default_factory=list)   # (branch, worktree)
    keeps: list[tuple[str, str]] = field(default_factory=list)             # (branch, reason)


@dataclass
class Result:
    code: int = 0
    lines: list[str] = field(default_factory=list)
    error: str = ""


def _git(repo: Path, *args: str, timeout: int = GIT_TIMEOUT) -> subprocess.CompletedProcess | None:
    """One git call, or None when git could not run (missing, or timed out). The same
    `git_env()` as every other git call the engine makes, so a hook's repository variables never
    redirect it."""
    try:
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                              timeout=timeout, env=git_env())
    except (OSError, subprocess.SubprocessError):
        return None


def _out(res: subprocess.CompletedProcess) -> str:
    return res.stdout.decode("utf-8", errors="replace").strip()


def _reason(res: subprocess.CompletedProcess) -> str:
    text = res.stderr.decode("utf-8", errors="replace").strip()
    return " ".join(text.split()) or f"git exited {res.returncode}"


def _run(repo: Path, *args: str, timeout: int = GIT_TIMEOUT) -> subprocess.CompletedProcess:
    """A git call that must run; git missing or timing out is an error, never an empty answer."""
    res = _git(repo, *args, timeout=timeout)
    if res is None:
        raise PhaseError("git could not run")
    return res


def _ask(repo: Path, *args: str) -> str:
    """What git prints for a question that must have an answer."""
    res = _run(repo, *args)
    if res.returncode != 0:
        raise PhaseError(f"git {args[0]} failed: {_reason(res)}")
    return _out(res)


def _test(repo: Path, *args: str) -> bool:
    """A yes/no question git answers with its exit code."""
    return _run(repo, *args).returncode == 0


def _default_branch(repo: Path, remote: str) -> str:
    """The remote's default branch: its remote HEAD here, else what the remote itself says."""
    prefix = f"refs/remotes/{remote}/"
    res = _run(repo, "symbolic-ref", "--quiet", prefix + "HEAD")
    if res.returncode == 0 and _out(res).startswith(prefix):
        return _out(res)[len(prefix):]
    res = _run(repo, "ls-remote", "--symref", remote, "HEAD", timeout=PUSH_TIMEOUT)
    if res.returncode == 0:
        for line in res.stdout.decode("utf-8", errors="replace").splitlines():
            if line.startswith("ref: refs/heads/"):
                return line[len("ref: refs/heads/"):].split("\t")[0]
    raise PhaseError(f"cannot tell which branch is the default of {remote}; "
                     f"nothing was changed")


def _worktrees(repo: Path) -> dict[str, str]:
    """Branch name -> the path of the worktree that has it checked out."""
    found, path = {}, None
    for line in _ask(repo, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line.startswith("branch refs/heads/") and path:
            found[line[len("branch refs/heads/"):]] = path
    return found


def _in_the_way(tree: str) -> str:
    """` (in the way: a, b, c)`: the first three untracked or modified paths in a worktree git
    would not remove; empty when git cannot say."""
    res = _git(Path(tree), "status", "--porcelain")
    if res is None or res.returncode != 0:
        return ""
    lines = res.stdout.decode("utf-8", errors="replace").splitlines()
    paths = [line[3:] for line in lines if len(line) > 3][:3]
    return f" (in the way: {', '.join(paths)})" if paths else ""


def _yes(repo: Path, *args: str) -> bool:
    """A question git answers 0 for yes and 1 for no; any other exit stops the command."""
    res = _run(repo, *args)
    if res.returncode not in (0, 1):
        raise PhaseError(f"git {args[0]} failed: {_reason(res)}")
    return res.returncode == 0


def _open_build(repo: Path, name: str, closing: str, open_builds) -> str:
    """Why `name` stays because another build is still open, or empty. An open build keeps its own
    branch and what is merged into it; a build whose branch is the one closing, or that does not
    exist here, keeps nothing."""
    for slug, branch in open_builds:
        if branch == closing or not _yes(repo, "rev-parse", "--verify", "--quiet",
                                         f"refs/heads/{branch}"):
            continue
        if name == branch:
            return f"the branch of the build {slug}, which is not closed"
        if _yes(repo, "merge-base", "--is-ancestor", f"refs/heads/{name}", f"refs/heads/{branch}"):
            return f"merged into {branch}, whose build {slug} is not closed"
    return ""


def plan(repo: Path, remote: str | None = None, keep=(), open_builds=()) -> Plan:
    """What `apply` would do. Only reads. `open_builds` is `(slug, branch)` for each build still
    in flight: its branch and what is merged into it are kept. Raises `PhaseError` with the
    reason when the command must refuse."""
    if not _test(repo, "rev-parse", "--git-dir"):
        raise PhaseError("not inside a git repository")
    p = Plan(repo=repo)
    res = _run(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    if res.returncode != 0:
        raise PhaseError("HEAD is detached: check out the branch to close")
    p.branch = _out(res)
    p.commit = _ask(repo, "rev-parse", "HEAD")

    rows = [line.split("\t") for line in _ask(
        repo, "for-each-ref", "--format=%(refname:short)%09%(upstream)%09%(upstream:remotename)"
        "%09%(upstream:remoteref)%09.", "refs/heads").splitlines()]   # the dot survives strip()
    mine = next((r for r in rows if r[0] == p.branch), None)
    remotes = _ask(repo, "remote").split()
    if mine and mine[2] and mine[2] != "." and mine[3].startswith("refs/heads/"):
        p.remote, p.target, p.has_upstream = mine[2], mine[3][len("refs/heads/"):], True
    elif remote:
        if remote not in remotes:
            raise PhaseError(f"no remote named {remote}")
        p.remote, p.target = remote, p.branch
    elif len(remotes) == 1:
        p.remote, p.target = remotes[0], p.branch
    elif not remotes:
        raise PhaseError("this repository has no remote to push to")
    else:
        raise PhaseError("several remotes and no upstream: name one with --remote ("
                         + ", ".join(remotes) + ")")

    p.default = _default_branch(repo, p.remote)
    if p.branch == p.default:
        raise PhaseError(f"{p.branch} is the default branch of {p.remote}: "
                         f"close a work branch, not that one")
    if p.target == p.default:
        raise PhaseError(f"the upstream of {p.branch} is the default branch of {p.remote} "
                         f"({p.remote}/{p.default}), and a push would land there: push it under "
                         f"its own name with git push -u {p.remote} {p.branch}, or run "
                         f"git branch --unset-upstream first")
    if _ask(repo, "status", "--porcelain", "--untracked-files=no"):
        raise PhaseError("uncommitted changes: commit or set them aside first")
    tip = f"refs/remotes/{p.remote}/{p.default}"
    if not _test(repo, "rev-parse", "--verify", "--quiet", tip + "^{commit}"):
        raise PhaseError(f"{p.remote}/{p.default} is not known here: run git fetch {p.remote}")

    trees = _worktrees(repo)
    named = set(keep)
    for name, upstream, *_ in rows:
        if name == p.branch:
            continue
        if name == p.default:
            p.keeps.append((name, f"the default branch of {p.remote}"))
        elif name in named:
            p.keeps.append((name, "named with --keep"))
        elif held := _open_build(repo, name, p.branch, open_builds):
            p.keeps.append((name, held))
        elif upstream:
            p.keeps.append((name, f"has an upstream of its own ({upstream.removeprefix('refs/')})"))
        elif _test(repo, "merge-base", "--is-ancestor", f"refs/heads/{name}", tip):
            p.keeps.append((name, f"already in {p.remote}/{p.default}"))
        elif not _test(repo, "merge-base", "--is-ancestor", f"refs/heads/{name}", p.commit):
            p.keeps.append((name, "has commits the pushed branch does not"))
        else:
            p.removals.append((name, trees.get(name)))
    return p


def describe(p: Plan) -> list[str]:
    """The plan as lines, what `--dry-run` prints."""
    how = f"to {p.remote}/{p.target}" + ("" if p.has_upstream else ", setting the upstream")
    lines = [f"would push {p.branch} {how}"]
    lines += [f"would remove {b}" + (" and its worktree" if w else "") for b, w in p.removals]
    lines += [f"would keep {b}: {why}" for b, why in p.keeps]
    return lines


def apply(p: Plan) -> Result:
    """Push, verify, then remove. Stops with code 1 and nothing deleted when the push fails or
    the remote does not show the pushed commit."""
    r = Result()
    refspec = f"refs/heads/{p.branch}:refs/heads/{p.target}"
    args = ["push", p.remote, refspec] if p.has_upstream else \
        ["push", "--set-upstream", p.remote, refspec]
    res = _git(p.repo, *args, timeout=PUSH_TIMEOUT)
    if res is None:
        r.code, r.error = 1, "git could not run the push; nothing was deleted"
        return r
    if res.returncode != 0:
        r.code, r.error = 1, f"the push failed ({_reason(res)}); nothing was deleted"
        return r
    seen = _git(p.repo, "ls-remote", p.remote, f"refs/heads/{p.target}", timeout=PUSH_TIMEOUT)
    shown = _out(seen).split()[:1] if seen is not None and seen.returncode == 0 else []
    if shown != [p.commit]:
        r.code, r.error = 1, (f"{p.remote} does not show {p.target} at {p.commit[:12]} after the "
                              f"push; nothing was deleted")
        return r
    r.lines.append(f"pushed {p.branch} to {p.remote}/{p.target} at {p.commit[:12]}"
                   + ("" if p.has_upstream else " (upstream set)"))
    kept = list(p.keeps)
    for name, tree in p.removals:
        if tree:
            res = _git(p.repo, "worktree", "remove", tree)
            if res is None or res.returncode != 0:
                kept.append((name, "its worktree stays: "
                             + (_reason(res) if res else "git could not run")
                             + _in_the_way(tree)))
                continue
        res = _git(p.repo, "branch", "-d", name)
        if res is None or res.returncode != 0:
            kept.append((name, "git kept it: " + (_reason(res) if res else "git could not run")))
        else:
            r.lines.append(f"removed {name}" + (" and its worktree" if tree else ""))
    r.lines += [f"kept {b}: {why}" for b, why in kept]
    return r
