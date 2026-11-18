"""Two developers, one remote: a bare repository and two clones of it, for tests of what git
does when both have committed.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import profile_guard  # noqa: E402,F401
from base_support import REPO_ENV, Case, Solo, Tree  # noqa: E402,F401


class TwoClones(Case):
    """`remote.git` seeded from a `Solo` tree on `main`, cloned to `self.tmp / "a"` and
    `self.tmp / "b"`. `self.seed` is the tree the remote was seeded from."""

    def setUp(self) -> None:
        super().setUp()
        self.seed = w = self.solo()
        self.remote = self.tmp / "remote.git"
        self.remote.mkdir()
        w.git("init", "-q", "--bare", at=self.remote)
        w.git("symbolic-ref", "HEAD", "refs/heads/main", at=self.remote)
        w.git("remote", "add", "origin", str(self.remote))
        w.git("push", "-q", "origin", "main")
        self.a, self.b = self.tmp / "a", self.tmp / "b"
        for path, who in ((self.a, "A"), (self.b, "B")):
            w.git("clone", "-q", str(self.remote), str(path), at=self.tmp)
            w.git("config", "user.email", f"{who.lower()}@example.com", at=path)
            w.git("config", "user.name", who, at=path)

    def git_env(self, **extra: str) -> dict:
        env = {k: v for k, v in os.environ.items() if k not in REPO_ENV}
        env.update(HOME=str(self.seed.home), GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0",
                   CONTEXT_GATE_NO_UPDATE_CHECK="1", **extra)
        return env

    def commit(self, clone: Path, msg: str) -> None:
        self.seed.git("add", "-A", at=clone)
        self.seed.git("commit", "-qm", msg, at=clone)

    def push(self, clone: Path) -> None:
        self.seed.git("push", "-q", "origin", "HEAD", at=clone)

    def fetch(self, clone: Path) -> None:
        self.seed.git("fetch", "-q", "origin", at=clone)

    def merge(self, clone: Path, ref: str, **env: str) -> subprocess.CompletedProcess:
        """`git merge --no-edit REF`: a conflict is a result, not an error. `env` is added to
        the environment git runs in (a merge driver's `PYTHONPATH`, say)."""
        return subprocess.run(
            ["git", "-c", "tag.gpgSign=false", "-c", "commit.gpgSign=false", "merge",
             "--no-edit", ref],
            cwd=clone, capture_output=True, text=True, env=self.git_env(**env))
