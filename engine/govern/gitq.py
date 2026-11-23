"""Read-only git lookups whose answer cannot change during one command, asked once per command.

The cache is live only inside `with scope():` (`cli.main` opens one around a command) and empty
outside it, so a caller that changes git between two direct calls never sees a stale answer.
Nothing that names HEAD, a ref, the index or the work tree is cached here, and neither is a
failure to run git or an exception: those are asked again.
"""
from __future__ import annotations

import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from govern import phase

_cache: dict[tuple, object] = {}
_depth = 0


@contextmanager
def scope() -> Iterator[None]:
    """Open the cache. Cleared on entry and on exit; nesting is allowed and only the outermost
    scope clears."""
    global _depth
    if _depth == 0:
        _cache.clear()
    _depth += 1
    try:
        yield
    finally:
        _depth -= 1
        if _depth == 0:
            _cache.clear()


def _key(repo: Path, *args: str) -> tuple:
    return (str(Path(repo).resolve()), *args)


def toplevel(repo: Path) -> subprocess.CompletedProcess | None:
    """`git rev-parse --show-toplevel`; None, when git could not run, is never cached."""
    key = _key(repo, "rev-parse", "--show-toplevel")
    if _depth and key in _cache:
        return _cache[key]  # type: ignore[return-value]
    res = phase._git(repo, "rev-parse", "--show-toplevel")
    if res is not None and _depth:
        _cache[key] = res
    return res


def toplevel_text(repo: Path) -> str:
    """The top-level folder as `phase._ask` would answer it: raises `PhaseError` on failure."""
    res = toplevel(repo)
    if res is None:
        raise phase.PhaseError("git could not run")
    if res.returncode != 0:
        raise phase.PhaseError(f"git rev-parse failed: {phase._reason(res)}")
    return phase._out(res)


def git_dir_ok(repo: Path) -> bool:
    """Whether `git rev-parse --git-dir` succeeds; raises as `phase._test` does."""
    key = _key(repo, "rev-parse", "--git-dir")
    if _depth and key in _cache:
        return _cache[key]  # type: ignore[return-value]
    ok = phase._test(repo, "rev-parse", "--git-dir")
    if _depth:
        _cache[key] = ok
    return ok


def remotes(repo: Path) -> list[str]:
    """The remote names, from `git remote`; raises as `phase._ask` does (an exception is never cached)."""
    key = _key(repo, "remote")
    if _depth and key in _cache:
        return list(_cache[key])  # type: ignore[call-overload]
    names = phase._ask(repo, "remote").split()
    if _depth:
        _cache[key] = names
    return list(names)


def config_get(repo: Path, key: str) -> subprocess.CompletedProcess:
    """`git config --get <key>`, whatever its exit code; raises when git could not run."""
    ckey = _key(repo, "config", "--get", key)
    if _depth and ckey in _cache:
        return _cache[ckey]  # type: ignore[return-value]
    res = phase._run(repo, "config", "--get", key)
    if _depth:
        _cache[ckey] = res
    return res


def config_set(repo: Path, key: str, value: str) -> subprocess.CompletedProcess:
    """`git config <key> <value>`, never cached; drops every cached `config_get` of that key."""
    res = phase._run(repo, "config", key, value)
    drop = _key(repo, "config", "--get", key)
    _cache.pop(drop, None)
    return res


def default_branch(repo: Path, remote: str) -> str:
    """`phase._default_branch`, cached; a `PhaseError` is not cached."""
    key = _key(repo, "default-branch", remote)
    if _depth and key in _cache:
        return _cache[key]  # type: ignore[return-value]
    name = phase._default_branch(repo, remote)
    if _depth:
        _cache[key] = name
    return name
