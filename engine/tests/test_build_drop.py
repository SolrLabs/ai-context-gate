"""`govern build unit drop`, the far-behind base warning and `build start` again before any unit.

    python3 -m unittest discover -s engine/tests -k test_build_drop
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_support import *  # noqa: E402,F401,F403


class Drop(Build):
    def test_dropping_the_last_pending_unit_lets_the_review_run_and_status_names_it(self):
        w = self.started()
        self.code()
        self.unit("u1", "done")
        self.unit("u2")
        w.commit("briefs", at=w.top, index=False)
        out = self.ok("unit", "drop", "u2")
        self.assertIn("dropped u2", out)
        self.assertEqual([u["id"] for u in self.ledger()["unit"]], ["u1"])
        self.assertIn(f"Every unit is done: run `{GOVERN} build review`", self.ok("status"))
        self.ok("review")
        self.assertTrue((self.dir() / "review" / "package.diff").is_file())

    def test_a_done_or_running_unit_is_refused_and_the_ledger_is_unchanged(self):
        self.started()
        self.unit("u1", "done")
        self.unit("u2", "running")
        before = (self.dir() / build.LEDGER).read_bytes()
        for uid, status in (("u1", "done"), ("u2", "running")):
            self.refused(f"{uid} is {status}: only a pending, blocked or failed unit can be "
                         f"dropped", "unit", "drop", uid)
            self.assertEqual((self.dir() / build.LEDGER).read_bytes(), before)

    def test_a_blocked_and_a_failed_unit_can_be_dropped(self):
        self.started()
        self.unit("u1", "blocked")
        self.unit("u2", "failed")
        self.ok("unit", "drop", "u1")
        self.ok("unit", "drop", "u2")
        self.assertNotIn("unit", self.ledger())

    def test_an_unknown_unit_is_refused_as_set_refuses_it(self):
        self.started()
        self.unit("u1")
        self.refused("has no unit u9 (u1)", "unit", "drop", "u9")

    def test_a_committed_brief_folder_is_deleted(self):
        w = self.started()
        self.unit("u1")
        w.commit("briefs", at=w.top, index=False)
        self.ok("unit", "drop", "u1")
        self.assertFalse((self.dir() / "u1").exists())

    def test_a_folder_holding_a_file_git_does_not_have_is_kept_and_named(self):
        w = self.started()
        self.unit("u1")
        w.commit("briefs", at=w.top, index=False)
        (self.dir() / "u1" / "brief-2.md").write_text("mine\n", encoding="utf-8")
        out = self.ok("unit", "drop", "u1")
        self.assertIn("dropped u1", out)
        self.assertIn("it holds files git does not have", out)
        self.assertIn("kept ", out)
        self.assertTrue((self.dir() / "u1" / "brief-2.md").is_file())
        self.assertNotIn("unit", self.ledger())

    def test_a_staged_brief_never_committed_keeps_the_folder(self):
        w = self.started()
        self.unit("u1")
        w.git("add", "-f", "--", str(self.dir() / "u1" / "brief.md"))
        out = self.ok("unit", "drop", "u1")
        self.assertIn("kept ", out)
        self.assertTrue((self.dir() / "u1" / "brief.md").is_file())

    def test_a_committed_brief_edited_since_keeps_the_folder(self):
        w = self.started()
        self.unit("u1")
        w.commit("briefs", at=w.top, index=False)
        brief = self.dir() / "u1" / "brief.md"
        brief.write_text(brief.read_text(encoding="utf-8") + "more\n", encoding="utf-8")
        out = self.ok("unit", "drop", "u1")
        self.assertIn("kept ", out)
        self.assertTrue(brief.is_file())

    def test_a_folder_holding_only_ignored_files_is_deleted(self):
        w = self.started()
        self.unit("u1")
        w.commit("briefs", at=w.top, index=False)
        (self.dir() / "u1" / "developer-report.md").write_text("r\n", encoding="utf-8")
        self.ok("unit", "drop", "u1")
        self.assertFalse((self.dir() / "u1").exists())

    def test_a_failing_git_keeps_the_folder_and_still_drops_the_unit(self):
        self.started()
        self.unit("u1")
        real = phase._run

        def fail(repo, *args, **kw):
            if args and args[0] == "status":
                raise phase.PhaseError("git could not run")
            return real(repo, *args, **kw)

        with mock.patch.object(phase, "_run", fail):
            code, out, err = self.go("unit", "drop", "u1")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("kept ", out)
        self.assertTrue((self.dir() / "u1" / "brief.md").is_file())
        self.assertNotIn("unit", self.ledger())


class FarBehind(Build):
    def commits(self, n: int) -> None:
        for i in range(n):
            self.w.git("commit", "-q", "--allow-empty", "-m", f"c{i}")

    def test_a_base_51_commits_behind_warns_with_the_count(self):
        self.project()
        self.commits(51)
        out = self.ok("start", "p1")
        base = self.head("main")[:12]
        self.assertIn(f"the review will cover 51 commits since {base}: if the phase branch was "
                      f"cut from another branch, start again with --from <branch>", out)
        self.assertIn("started p1", out)

    def test_a_base_50_commits_behind_says_nothing(self):
        self.project()
        self.commits(50)
        out = self.ok("start", "p1")
        self.assertNotIn("the review will cover", out)


class StartAgain(Build):
    def test_start_again_with_no_unit_replaces_the_base_and_origin(self):
        w = self.project()
        w.git("checkout", "-q", "main")
        w.git("checkout", "-qb", "mid")
        w.git("commit", "-q", "--allow-empty", "-m", "mid work")
        w.git("checkout", "-q", "phase/p1")
        w.git("reset", "-q", "--hard", "mid")
        self.ok("start", "p1")
        self.assertEqual(self.ledger()["base"], self.head("main"))
        out = self.ok("start", "p1", "--from", "mid")
        self.assertIn("restarted p1 on phase/p1, from mid at " + self.head("mid")[:12], out)
        data = self.ledger()
        self.assertEqual((data["base"], data["from"]), (self.head("mid"), "mid"))

    def test_start_again_with_a_unit_is_refused(self):
        self.started()
        self.unit("u1")
        self.refused("a build is already in flight on phase/p1: p1", "start", "p1")

    def test_start_again_for_another_phase_name_is_refused(self):
        self.started()
        self.refused("a build is already in flight on phase/p1: p1", "start", "p2")


if __name__ == "__main__":
    unittest.main()
