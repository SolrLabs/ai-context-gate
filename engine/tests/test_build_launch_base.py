"""`build launch`: the commit an agent's worktree must start from. A launch refuses when the
default branch has commits the phase branch lacks (agent worktrees are cut from it), and a unit
retried after a failed attempt starts on HEAD like any other, and its earlier branch is only named.

    python3 -m unittest discover -s engine/tests -k test_build_launch_base
"""
from __future__ import annotations

import os
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


class OutsideTheDriver(Launching):
    """A session the driver did not start is sent to the driver, unless it asks with `--here`."""

    def test_launch_is_refused_and_nothing_changes(self):
        self.started()
        self.ready()
        os.environ.pop("GOVERN_DRIVE", None)
        before = self.files()
        for what in ("u1", "review", "fix", "close"):
            err = self.refused("Builds run under the driver", "launch", what)
            self.assertIn("build drive", err)
            self.assertIn(f"launch {what} --here", err)
        self.assertEqual(self.files(), before)
        unit = self.ledger()["unit"][0]
        self.assertEqual((unit["status"], unit["attempts"]), ("pending", 0))

    def test_here_or_a_driver_session_launches(self):
        self.started()
        self.ready()
        os.environ.pop("GOVERN_DRIVE", None)
        out = self.ok("launch", "u1", "--here")
        self.assertIn("Brief:", out)
        self.assertIn("Base:", out)
        self.assertEqual(self.ledger()["unit"][0]["status"], "running")
        self.ok("unit", "set", "u1", "pending")
        os.environ["GOVERN_DRIVE"] = "1"
        out = self.ok("launch", "u1")
        self.assertIn("Brief:", out)
        self.assertIn("Base:", out)


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
        self.assertEqual(out.splitlines()[-2], f"Base: {self.head()}")

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
        self.assertEqual(self.ok("launch", "u1").splitlines()[-2], f"Base: {self.head()}")


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

    def test_a_relaunch_behind_head_starts_on_head_and_names_the_earlier_attempt(self):
        tip, head = self.attempt(worktree=False)
        lines = self.ok("launch", "u1").splitlines()
        i = lines.index(f"Base: {head}")
        self.assertEqual(lines[i + 1], "Earlier attempt: u1-try")
        self.assertEqual(lines[i + 2], "Phase branch: phase/p1")
        self.assertEqual(self.head("u1-try"), tip)
        unit = self.ledger()["unit"][0]
        self.assertEqual((unit["attempts"], unit["base"]), (2, head))

    def test_a_relaunch_ahead_of_head_starts_on_head(self):
        self.started()
        self.ready()
        self.ok("launch", "u1")
        self.w.git("branch", "u1-try")
        self.w.switch("u1-try")
        self.w.write("src/more.py", "attempt\n")
        self.w.commit("attempt", at=self.w.top, index=False)
        tip = self.head()
        self.w.switch("phase/p1")
        self.ok("unit", "set", "u1", "failed", "--branch", "u1-try")
        lines = self.ok("launch", "u1").splitlines()
        self.assertIn(f"Base: {self.head()}", lines)
        self.assertNotIn(f"Base: {tip}", lines)
        self.assertIn("Earlier attempt: u1-try", lines)
        self.assertEqual(self.head("u1-try"), tip)

    def test_a_first_launch_has_no_earlier_attempt_line(self):
        self.started()
        self.ready()
        self.assertNotIn("Earlier attempt", self.ok("launch", "u1"))

    def test_a_conflicting_or_dirty_earlier_attempt_does_not_stop_a_launch(self):
        tip, head = self.attempt(worktree=True)
        (self.tmp / "attempt" / "attempt.txt").write_text("edited\n", encoding="utf-8")
        before = (self.tmp / "attempt" / "attempt.txt").read_text(encoding="utf-8")
        out = self.ok("launch", "u1")
        self.assertIn(f"Base: {head}", out)
        self.assertEqual(self.head("u1-try"), tip)
        self.assertEqual((self.tmp / "attempt" / "attempt.txt").read_text(encoding="utf-8"), before)
        self.assertEqual(self.ledger()["unit"][0]["attempts"], 2)


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
