"""Ids used on other developers' branches, as far as this clone has fetched them.

Two developers on two branches each see the same next free id. Counting the ids on every
remote-tracking branch too makes the second one skip what the first already pushed. Nothing here
fetches unless asked (`fetch`); a branch this clone never fetched is invisible, and a ref that
cannot be read is reported, never read as empty.
"""
from __future__ import annotations

import fnmatch
import subprocess
from pathlib import Path

from govern import base, decisions
from govern.context import Context, git, git_env


def remote_branches(repo: Path) -> list[str]:
    """Every remote-tracking ref of `repo`, sorted, without a remote's `HEAD` pointer. Empty
    when git fails."""
    out = git(repo, "for-each-ref", "--format=%(refname)", "refs/remotes")
    if not out:
        return []
    return sorted(r for r in out.splitlines() if r and not r.endswith("/HEAD"))


def _ref_numbers(ctx: Context, repo: Path, ref: str, rels: set[str], prefix: str,
                 listing: tuple[str, str] | None) -> set[int]:
    """The numbers of `prefix` in the files `rels` hold at `ref`, and in every file directly
    under `listing[0]` there whose name matches `listing[1]`. Raises `base.CannotCompare`."""
    names = set(rels)
    if listing is not None:
        directory, glob = listing
        listed = base.ls(repo, ref, "" if directory == "." else directory)
        if listed is None:
            raise base.CannotCompare("git could not list the files")
        top = "" if directory == "." else directory
        for name in listed:
            parent, _, leaf = name.rpartition("/")
            if parent == top and fnmatch.fnmatchcase(leaf, glob):
                names.add(name)
    used: set[int] = set()
    for rel in sorted(names):
        held = base.exists(repo, ref, rel)
        if held is None:
            raise base.CannotCompare(f"git could not say whether it holds {rel}")
        if held:
            used |= ctx.grammar.parse(base.read(repo, ref, rel)).used_numbers(prefix)
    return used


def used_numbers(ctx: Context, paths: list[Path], prefix: str,
                 also: tuple[Path, str] | None = None) -> tuple[set[int], list[str]]:
    """`(numbers of prefix used, warnings)`: in each of `paths` that exists on disk, and in the
    copy each remote branch of the repository holding them has. A file a ref does not hold adds
    nothing. `also=(directory, glob)` adds every file directly under `directory` on each ref
    whose name matches `glob`, so a file that exists only on a fetched branch counts. A ref git
    cannot read is skipped with a warning line."""
    used: set[int] = set()
    for path in paths:
        if path.exists():
            used |= ctx.grammar.parse_file(path).used_numbers(prefix)
    wanted: dict[Path, set[str]] = {}
    for path in paths:
        found = ctx._repo_rel(path)
        if found is not None:
            wanted.setdefault(found[0], set()).add(found[1])
    listings: dict[Path, tuple[str, str]] = {}
    if also is not None:
        found = ctx._repo_rel(also[0])
        if found is not None:
            listings[found[0]] = (found[1], also[1])
            wanted.setdefault(found[0], set())
    warnings: list[str] = []
    for repo, rels in wanted.items():
        for ref in remote_branches(repo):
            try:
                used |= _ref_numbers(ctx, repo, ref, rels, prefix, listings.get(repo))
            except base.CannotCompare as exc:
                warnings.append(f"could not read {ref}: {exc.reason}")
    return used, warnings


def fetch(repo: Path, timeout: int = 60) -> str | None:
    """`git fetch --quiet --all` in `repo`. None on success, else why it did not."""
    try:
        res = subprocess.run(["git", "-C", str(repo), "fetch", "--quiet", "--all"],
                             capture_output=True, timeout=timeout, env=git_env())
    except subprocess.TimeoutExpired:
        return f"timed out after {timeout} s"
    except (OSError, subprocess.SubprocessError) as exc:
        return str(exc) or type(exc).__name__
    if res.returncode == 0:
        return None
    lines = [ln.strip() for ln in res.stderr.decode("utf-8", errors="replace").splitlines()
             if ln.strip()]
    for line in lines:
        for prefix in ("fatal:", "error:"):
            if line.startswith(prefix):
                return line[len(prefix):].strip()
    return lines[-1] if lines else f"git exited {res.returncode}"


def team_size(root: Path, days: int = 90) -> int:
    """Distinct author emails (case-insensitive) of the commits on `HEAD` in the last `days`
    days. 0 when git fails or there are none."""
    out = git(root, "log", f"--since={days}.days.ago", "--format=%ae", "HEAD")
    if not out:
        return 0
    return len({e.strip().lower() for e in out.splitlines() if e.strip()})


def _titles(ctx: Context, repo: Path, ref: str, rel: str) -> dict[tuple[str, int], str] | None:
    """`{id key: title}` of the entries `rel` holds at `ref`; empty when it holds no such file;
    None when git cannot say or the file cannot be read."""
    held = base.exists(repo, ref, rel)
    if held is None:
        return None
    if not held:
        return {}
    try:
        parsed = ctx.grammar.parse(base.read(repo, ref, rel))
    except base.CannotCompare:
        return None
    out: dict[tuple[str, int], str] = {}
    for e in parsed.entries:
        key = decisions.id_key(f"{e.prefix}-{e.num}")
        if key is not None:
            out.setdefault(key, e.title.strip())
    return out


def _logs(ctx: Context) -> list[Path]:
    paths = [ctx.workspace_log] if ctx.workspace_log is not None else []
    for scope in ctx.registry.scopes:
        paths.append(ctx.scope_log(scope))
        paths.extend(ctx.trap_files(scope))
    return paths


def _default_tip(repo: Path) -> str | None:
    """The ref holding the default branch, from refs this clone already has: a remote's
    `HEAD` pointer (`origin` first), else a local `main` or `master`. None when neither is known.
    Never asks a remote."""
    remotes = (git(repo, "remote") or "").split()
    for remote in sorted(remotes, key=lambda r: r != "origin"):
        got = git(repo, "symbolic-ref", "--quiet", f"refs/remotes/{remote}/HEAD")
        if got and git(repo, "rev-parse", "--verify", "--quiet", got) is not None:
            return got
    for name in ("main", "master"):
        ref = f"refs/heads/{name}"
        if git(repo, "rev-parse", "--verify", "--quiet", ref) is not None:
            return ref
    return None


def sibling_collisions(ctx: Context) -> list[str]:
    """Lines naming an id this branch added to a decision log or a trap file that another
    fetched branch also added, with a different title. Sorted, unique; `[]` when the default
    branch cannot be told. Reads only refs this clone already has: nothing is fetched.

    Ruling: a solo developer pays one `git log` call (`team_size`) per session start and
    nothing else; only a team runs this."""
    tips: dict[Path, tuple[str, set[str]] | None] = {}
    lines: set[str] = set()
    for path in _logs(ctx):
        if not path.is_file():
            continue
        found = ctx._repo_rel(path)
        if found is None:
            continue
        repo, rel = found
        if repo not in tips:
            tip_ref = _default_tip(repo)
            if tip_ref is None:
                return []
            up = git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
            skip = {f"refs/remotes/{up}"} if up else set()
            tips[repo] = (tip_ref, skip)
        tip = tips[repo]
        if tip is None:
            continue
        tip_ref, skip = tip
        try:
            mine = ctx.grammar.parse_file(path).entries
        except Exception:
            continue
        before = _titles(ctx, repo, tip_ref, rel)
        if before is None:
            continue
        added: dict[tuple[str, int], tuple[str, int, str]] = {}
        for e in mine:
            key = decisions.id_key(f"{e.prefix}-{e.num}")
            if key is not None and key not in before:
                added.setdefault(key, (e.prefix, e.num, e.title.strip()))
        if not added:
            continue
        for ref in remote_branches(repo):
            if ref in skip or ref == tip_ref:
                continue
            theirs = _titles(ctx, repo, ref, rel)
            if not theirs:
                continue
            for key, (prefix, num, title) in added.items():
                other = theirs.get(key)
                if other is not None and other != title:
                    name = ref.removeprefix("refs/remotes/")
                    lines.add(f"{ctx.rel(path)}: {prefix}-{num} is also added on {name} "
                              f'("{other}"); renumber one with govern renumber')
    return sorted(lines)
