"""A locked worktree whose locking process is gone, or is this session, no longer blocks `govern
phase close` or `govern sweep --apply`; one a live process holds, or one with no pid, is kept and
said so. Against real repositories, as `test_phase` and `test_sweep` build them.

    python3 -m unittest discover -s engine/tests -k test_phase_locks
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import profile_guard  # noqa: E402,F401

from govern import phase  # noqa: E402
from test_phase import Phase  # noqa: E402
from test_sweep import Sweep  # noqa: E402

POSIX = unittest.skipIf(os.name == "nt", "process ids and ps are POSIX only")


class Pids:
    """Processes a case starts: a finished one for a dead pid, a sleeping one for a live pid."""

    def dead_pid(self) -> int:
        proc = subprocess.Popen([sys.executable, "-c", "pass"], cwd=str(self.tmp))
        proc.wait()
        return proc.pid

    def live_pid(self) -> int:
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                cwd=str(self.tmp))
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        return proc.pid


class Helpers(unittest.TestCase):
    def test_lock_pid_reads_the_number_after_pid(self):
        self.assertEqual(phase.lock_pid("claude agent agent-a1b2 (pid 4242)"), 4242)
        self.assertIsNone(phase.lock_pid(""))
        self.assertIsNone(phase.lock_pid("held by the owner"))

    def test_pid_state_is_unknown_on_windows(self):
        with mock.patch.object(os, "name", "nt"):
            self.assertEqual(phase.pid_state(os.getpid()), "unknown")


@POSIX
class States(Pids, unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def test_the_four_states(self):
        self.assertEqual(phase.pid_state(self.dead_pid()), "gone")
        self.assertEqual(phase.pid_state(os.getpid()), "session")
        self.assertEqual(phase.pid_state(self.live_pid()), "running")

    def test_an_ancestor_is_the_session(self):
        self.assertEqual(phase.pid_state(os.getppid()), "session")

    def test_ps_that_fails_is_unknown(self):
        with mock.patch.object(subprocess, "run", side_effect=OSError("no ps")):
            self.assertEqual(phase.pid_state(self.live_pid()), "unknown")


class Locked(Pids, Phase):
    def locked(self, reason: str) -> Path:
        t = self.work()
        where = self.tmp / "wt"
        t.git("worktree", "add", "-q", str(where), "old")
        t.git("worktree", "lock", "--reason", reason, str(where))
        self.where = where
        return where

    def test_the_locks_are_read_by_path(self):
        where = self.locked("claude agent a (pid 7)")
        found = phase._locks(self.t.root)
        self.assertEqual(list(found.values()), ["claude agent a (pid 7)"])
        self.assertEqual(Path(next(iter(found))).resolve(), where.resolve())

    @POSIX
    def test_a_dead_pid_is_unlocked_and_removed(self):
        self.locked(f"claude agent a (pid {self.dead_pid()})")
        code, out, err = self.close()
        self.assertEqual(code, 0, err)
        self.assertIn("removed old and its worktree", out)
        self.assertFalse(self.where.exists())
        self.assertNotIn("old", self.branches())

    @POSIX
    def test_this_session_is_unlocked_and_removed(self):
        self.locked(f"claude agent a (pid {os.getpid()})")
        code, out, err = self.close()
        self.assertEqual(code, 0, err)
        self.assertIn("removed old and its worktree", out)
        self.assertFalse(self.where.exists())

    @POSIX
    def test_a_running_process_keeps_it(self):
        pid = self.live_pid()
        self.locked(f"claude agent a (pid {pid})")
        code, out, err = self.close()
        self.assertEqual(code, 0, err)
        self.assertIn(f"kept old: its worktree is locked by process {pid}, which is running and "
                      f"is not this session; when it ends, run phase close again", out)
        self.assertTrue(self.where.exists())
        self.assertIn("old", self.branches())

    def test_no_pid_keeps_it_and_names_the_unlock(self):
        self.locked("held by hand")
        code, out, err = self.close()
        self.assertEqual(code, 0, err)
        self.assertIn(f"kept old: its worktree is locked (held by hand); if no agent works in "
                      f"it, run git worktree unlock {phase._locks(self.t.root).popitem()[0]} "
                      f"and phase close again", out)
        self.assertTrue(self.where.exists())

    def test_an_empty_reason_reads_no_reason_given(self):
        t = self.work()
        where = self.tmp / "wt"
        t.git("worktree", "add", "-q", str(where), "old")
        t.git("worktree", "lock", str(where))
        _, out, _ = self.close()
        self.assertIn("is locked (no reason given); if no agent", out)

    @POSIX
    def test_the_dry_run_says_it_would_unlock(self):
        self.locked(f"claude agent a (pid {self.dead_pid()})")
        code, out, err = self.close("--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("would unlock and remove old and its worktree", out)
        self.assertTrue(self.where.exists())

    @POSIX
    def test_a_dirty_unlocked_tree_is_still_kept(self):
        self.locked(f"claude agent a (pid {self.dead_pid()})")
        (self.where / "a.txt").write_text("edited\n", encoding="utf-8")
        code, out, err = self.close()
        self.assertEqual(code, 0, err)
        self.assertRegex(out, r"kept old: its worktree stays: ")
        self.assertTrue(self.where.exists())


class Swept(Pids, Sweep):
    def locked(self, reason: str) -> Path:
        self.branch_at("old")
        folder = self.worktree("old")
        self.w.git("worktree", "lock", "--reason", reason, str(folder))
        return folder

    def worktree_line(self) -> str:
        return [ln for ln in self.lines() if ln.startswith("worktree  ")][0]

    @POSIX
    def test_a_dead_pid_takes_the_branch_verdict_and_apply_removes_it(self):
        folder = self.locked(f"claude agent a (pid {self.dead_pid()})")
        self.assertIn("worktree  MERGED  old", self.worktree_line())
        code, out, err = self.sweep("--apply", "--only", "old")
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("removed   branch old", out)
        self.assertFalse(folder.exists())

    @POSIX
    def test_a_live_process_needs_review(self):
        pid = self.live_pid()
        self.locked(f"claude agent a (pid {pid})")
        line = self.worktree_line()
        self.assertIn("worktree  REVIEW  old", line)
        self.assertTrue(line.endswith(f"(locked by pid {pid})"), line)

    def test_no_pid_needs_review(self):
        self.locked("held by hand")
        self.assertTrue(self.worktree_line().endswith("(locked)"), self.worktree_line())


if __name__ == "__main__":
    unittest.main()
