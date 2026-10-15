"""merged-branches: local branches already merged into the default branch are named.

    python3 -m unittest discover -s engine/tests -k branches
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))

from govern.checks.branches import merged_branches  # noqa: E402

KEEP = ["main", "master", "development", "beta-*", "release-*"]


def git(cwd: Path, *args: str) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t",
                    "-c", "commit.gpgsign=false", *args],
                   check=True, capture_output=True, env=env)


class Branches(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "repo"
        self.root.mkdir()
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "commit", "-q", "--allow-empty", "-m", "first")

    def merged(self, *names: str) -> None:
        for n in names:
            git(self.root, "branch", n)

    def run_check(self, root: Path | None = None, keep=KEEP):
        ctx = SimpleNamespace(root=root or self.root)
        return merged_branches(ctx, {"keep": keep})

    def message(self, **kw) -> str:
        f = self.run_check(**kw)
        self.assertEqual(len(f.warnings), 1)
        return f.warnings[0]

    def test_merged_named_and_unmerged_not(self):
        self.merged("old-a", "old-b")
        git(self.root, "switch", "-q", "-c", "live")
        git(self.root, "commit", "-q", "--allow-empty", "-m", "work")
        git(self.root, "switch", "-q", "main")
        self.assertEqual(self.message(),
                         "2 local branches are merged into main and can be deleted: "
                         "old-a, old-b")

    def test_checked_out_linked_worktree_and_keep_are_not_named(self):
        self.merged("beta-1", "linked", "gone")
        git(self.root, "worktree", "add", "-q", str(self.tmp / "wt"), "linked")
        git(self.root, "switch", "-q", "-c", "here")
        self.assertEqual(self.message(),
                         "1 local branch is merged into main and can be deleted: gone")

    def test_twelve_merged_names_ten_and_counts_the_rest(self):
        self.merged(*[f"b{i:02d}" for i in range(12)])
        want = ", ".join(f"b{i:02d}" for i in range(10))
        self.assertEqual(self.message(),
                         f"12 local branches are merged into main and can be deleted: "
                         f"{want} and 2 more")

    def test_no_merged_branch_no_finding(self):
        self.assertEqual(self.run_check().warnings, [])

    def test_not_a_repository_is_one_not_checked_warning(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        msg = self.message(root=plain)
        self.assertTrue(msg.startswith("merged branches not checked: "), msg)

    def test_custom_keep_hides_its_match_and_not_development(self):
        self.merged("wip-x", "development")
        self.assertEqual(self.message(keep=["wip-*"]),
                         "1 local branch is merged into main and can be deleted: development")

    def test_default_branch_is_never_named_even_without_keep(self):
        self.merged("old")
        git(self.root, "switch", "-q", "-c", "other")
        self.assertEqual(self.message(keep=[]),
                         "1 local branch is merged into main and can be deleted: old")

    def test_detached_head_is_not_named_as_a_branch(self):
        self.merged("old")
        git(self.root, "checkout", "-q", "--detach")
        self.assertEqual(self.message(),
                         "1 local branch is merged into main and can be deleted: old")

    def clone(self) -> Path:
        git(self.tmp, "clone", "-q", str(self.root), "cl")
        return self.tmp / "cl"

    def test_no_local_default_branch_measures_against_the_remote(self):
        cl = self.clone()
        git(cl, "branch", "old", "origin/main")
        git(cl, "switch", "-q", "-c", "feat")
        git(cl, "branch", "-D", "main")
        self.assertEqual(self.message(root=cl),
                         "1 local branch is merged into main and can be deleted: old")

    def test_a_stale_local_default_branch_is_not_the_measure(self):
        cl = self.clone()
        git(self.root, "commit", "-q", "--allow-empty", "-m", "second")
        git(cl, "fetch", "-q")
        git(cl, "branch", "caught-up", "origin/main")
        git(cl, "switch", "-q", "-c", "feat")
        self.assertEqual(self.message(root=cl),
                         "1 local branch is merged into main and can be deleted: caught-up")

    def test_a_default_branch_with_no_ref_here_is_one_warning(self):
        cl = self.clone()
        git(cl, "switch", "-q", "-c", "feat")
        git(cl, "branch", "-D", "main")
        git(cl, "update-ref", "-d", "refs/remotes/origin/main")
        msg = self.message(root=cl)
        self.assertTrue(msg.startswith("merged branches not checked: "), msg)


if __name__ == "__main__":
    unittest.main()
