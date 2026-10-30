"""`build launch`: the commit an agent's worktree must start from. A launch refuses when the
default branch has commits the phase branch lacks (agent worktrees are cut from it), and a unit
retried after a failed attempt starts from its own branch with the phase merged in.

    python3 -m unittest discover -s engine/tests -k test_build_launch_base
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import profile_guard  # noqa: E402,F401

import test_build_workspace as ws  # noqa: E402
import test_build_worktree as wt  # noqa: E402
from build_support import BUILD, Build  # noqa: E402


class Launching(Build):
    def ready(self, uid: str = "u1") -> None:
        wt.fill(self.unit(uid))
        self.w.commit("briefs", at=self.w.top, index=False)


class DefaultBranch(Launching):
    def move_default(self, name: str = "main") -> None:
        """A commit on the default branch that the phase branch does not have."""
        self.w.switch(name)
        self.w.write("src/default_only.py", "ahead\n")
        self.w.commit("default moved on", at=self.w.top, index=False)
        self.w.switch("phase/p1")

    def test_a_launch_is_refused_while_the_default_branch_has_commits_the_phase_lacks(self):
        self.started()
        self.ready()
        self.move_default()
        before = self.files()
        err = self.refused("main has commits this phase branch does not", "launch", "u1")
        self.assertIn("git merge --no-edit main", err)
        self.assertEqual(self.files(), before)
        self.assertEqual(self.ledger()["unit"][0]["status"], "pending")
        self.w.git("merge", "-q", "--no-edit", "main")
        out = self.ok("launch", "u1")
        self.assertEqual(out.splitlines()[-1], f"Base: {self.head()}")

    def test_the_remote_copy_of_the_default_branch_is_named_with_its_remote(self):
        self.started(remote=True)
        self.ready()
        self.w.switch("main")
        self.w.write("src/default_only.py", "ahead\n")
        self.w.commit("default moved on", at=self.w.top, index=False)
        self.w.git("push", "-q", "origin", "main")
        self.w.switch("phase/p1")
        self.refused("origin/main has commits this phase branch does not", "launch", "u1")

    def test_a_repository_that_cannot_tell_its_default_branch_is_not_checked(self):
        w = self.started()
        self.ready()
        w.git("branch", "-m", "main", "trunk")
        self.assertEqual(self.ok("launch", "u1").splitlines()[-1], f"Base: {self.head()}")


class Retried(Launching):
    def attempt(self, *, worktree: bool) -> tuple[str, str]:
        """A unit whose first attempt left `u1-try` one commit ahead of an older phase commit,
        with the phase since moved on. Returns the attempt's tip and the phase HEAD."""
        self.started()
        # The engine commits and merges here itself; a CI runner has no global identity.
        self.w.git("config", "user.email", "t@t")
        self.w.git("config", "user.name", "t")
        self.ready()
        self.ok("launch", "u1")
        tree =self.tmp / "attempt"
        self.w.git("worktree", "add", "-q", str(tree), "-b", "u1-try", "HEAD")
        (tree / "attempt.txt").write_text("first try\n", encoding="utf-8")
        self.w.git("add", "-A", at=tree)
        self.w.git("commit", "-qm", "attempt", at=tree)
        tip = self.w.git("rev-parse", "HEAD", at=tree).strip()
        if not worktree:
            self.w.git("worktree", "remove", "--force", str(tree))
        self.ok("unit", "set", "u1", "failed", "--branch", "u1-try")
        self.code("src/later.py", "phase moved on\n")
        return tip, self.head()

    def merged(self, tip: str, head: str) -> str:
        out = self.ok("launch", "u1")
        new = self.head("u1-try")
        self.assertEqual(out.splitlines()[-1], f"Base: {new}")
        self.assertNotEqual(new, tip)
        self.assertEqual(self.w.git("rev-parse", f"{new}^1", f"{new}^2").split(), [tip, head])
        for old in (tip, head):
            self.w.git("merge-base", "--is-ancestor", old, new)
        self.assertEqual(self.ledger()["unit"][0]["attempts"], 2)
        return new

    def test_a_branch_with_no_worktree_gets_the_phase_merged_in_by_the_engine(self):
        tip, head = self.attempt(worktree=False)
        self.merged(tip, head)
        self.assertIn("for attempt 2", self.w.git("log", "-1", "--format=%s", "u1-try"))

    def test_a_branch_in_a_clean_worktree_is_merged_there(self):
        tip, head = self.attempt(worktree=True)
        new = self.merged(tip, head)
        tree = self.tmp / "attempt"
        self.assertEqual(self.w.git("rev-parse", "HEAD", at=tree).strip(), new)
        self.assertTrue((tree / "src" / "later.py").is_file())

    def test_a_dirty_worktree_is_refused_and_the_branch_stays(self):
        tip, head = self.attempt(worktree=True)
        (self.tmp / "attempt" / "attempt.txt").write_text("edited\n", encoding="utf-8")
        before = self.files()
        self.refused("u1-try does not merge cleanly with HEAD", "launch", "u1")
        self.assertEqual(self.head("u1-try"), tip)
        self.assertEqual(self.files(), before)
        self.assertEqual(self.ledger()["unit"][0]["attempts"], 1)

    def test_a_branch_that_conflicts_is_refused_and_the_ref_is_unmoved(self):
        self.started()
        self.ready()
        self.ok("launch", "u1")
        self.w.git("branch", "u1-try")
        self.w.switch("u1-try")
        self.w.write("src/clash.py", "theirs\n")
        self.w.commit("attempt", at=self.w.top, index=False)
        tip = self.head()
        self.w.switch("phase/p1")
        self.ok("unit", "set", "u1", "failed", "--branch", "u1-try")
        self.code("src/clash.py", "ours\n")
        self.refused("u1-try does not merge cleanly with HEAD", "launch", "u1")
        self.assertEqual(self.head("u1-try"), tip)

    def test_a_branch_that_has_head_is_left_alone(self):
        self.started()
        self.ready()
        self.ok("launch", "u1")
        self.w.git("branch", "u1-try")
        self.ok("unit", "set", "u1", "failed", "--branch", "u1-try")
        out = self.ok("launch", "u1")
        self.assertEqual(out.splitlines()[-1], f"Base: {self.head()}")
        self.assertEqual(self.head("u1-try"), self.head())


class Workspace(ws.Workspace):
    def test_a_workspace_launch_is_not_checked_against_the_default_branch(self):
        self.started("alpha")
        member = self.w.member("alpha")
        self.ok("--project", "alpha", "unit", "add", "u1", "--title", "One")
        wt.fill(member / BUILD / "p1" / "u1" / "brief.md")
        self.w.commit("briefs", at=member, index=False)
        self.w.git("checkout", "-q", "main", at=member)
        self.w.write("alpha/ahead.txt", "ahead\n")
        self.w.commit("main moved on", at=member, index=False)
        self.w.git("checkout", "-q", "phase/p1", at=member)
        out = self.ok("--project", "alpha", "launch", "u1")
        self.assertIn("Worktree:", out)


if __name__ == "__main__":
    unittest.main()
