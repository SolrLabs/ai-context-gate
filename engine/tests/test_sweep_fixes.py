"""`govern sweep`: the fix pass. One remote judged, long-lived remote branches left alone, the
checked-out phase branch's own remote listed for `--home`, exact remote-ref matching for PUSHED,
and an atomic lease on remote deletion. Against real repositories with a bare `origin`.

    python3 -m unittest discover -s engine/tests -k test_sweep_fixes
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import profile_guard  # noqa: E402,F401

import test_sweep_remote as base  # noqa: E402
from govern import sweep  # noqa: E402


class OneRemote(base.Remote):
    def second_remote(self) -> None:
        """A bare `upstream` holding `main` and `x`, fetched here."""
        bare = self.tmp / "upstream.git"
        self.w.git("init", "-q", "--bare", str(bare), at=self.tmp)
        self.w.git("remote", "add", "upstream", str(bare))
        self.w.git("push", "-q", "upstream", "main")
        self.w.git("push", "-q", "upstream", "main:refs/heads/x")
        self.w.git("fetch", "-q", "upstream")

    def test_a_second_remotes_branch_is_not_listed(self):
        self.second_remote()
        self.merged_remote("done")
        got = self.lines()
        self.assertIn("remote    MERGED  origin/done", got)
        self.assertFalse([ln for ln in got if "upstream/" in ln], got)

    def test_long_lived_branches_at_or_behind_the_default_are_not_listed(self):
        for name in sweep.LONG_LIVED:
            self.merged_remote(name)
        self.merged_remote("done")
        got = self.lines()
        self.assertEqual([ln for ln in got if ln.startswith("remote ")],
                         ["remote    MERGED  origin/done"], got)

    def test_the_long_lived_names(self):
        self.assertEqual(set(sweep.LONG_LIVED),
                         {"develop", "dev", "staging", "stage", "production", "prod", "trunk"})


class PhaseRemote(base.Home):
    def push_phase(self) -> None:
        self.phase_branch()
        self.w.git("push", "-q", "origin", "phase/x")

    def test_the_checked_out_merged_branchs_remote_is_listed_and_goes_with_home(self):
        self.push_phase()
        self.assertIn("remote    MERGED  origin/phase/x", self.lines())
        code, out, err = self.sweep("--apply", "--home", "--only", "phase/x", "origin/phase/x")
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(self.w.git("symbolic-ref", "--short", "HEAD").strip(), "main")
        self.assertEqual(self.on_origin("phase/x"), "")
        self.assertEqual([ln for ln in self.lines() if "MERGED" in ln or "STALE" in ln], [])

    def test_the_checked_out_branchs_remote_is_not_listed_when_the_branch_is_not_merged(self):
        self.w.git("checkout", "-qb", "work")
        self.w.write("w.txt", "w\n")
        self.w.commit("w", index=False)
        self.w.git("push", "-q", "origin", "work")
        got = self.lines()
        self.assertFalse([ln for ln in got if ln.startswith("remote ")], got)

    def test_home_on_the_default_branch_still_fast_forwards(self):
        self.advance_origin("main")
        self.w.git("fetch", "-q", "origin")
        self.assertNotEqual(self.head("main"), self.head("origin/main"))
        code, out, err = self.sweep("--apply", "--home", "--only", "nothing")
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(self.head("main"), self.head("origin/main"))


class ExactRef(base.Remote):
    def test_a_remote_ref_that_only_ends_in_the_branch_name_is_not_pushed(self):
        tip = self.commit_aged("fix", 20, push=False)
        self.w.git("push", "-q", "origin", "fix:refs/heads/qa/fix")
        got = self.lines()
        self.assertIn("branch    REVIEW  fix  (not in main and no PR)", got)
        self.assertEqual(self.head("fix"), tip)

    def test_the_exact_ref_still_counts(self):
        self.commit_aged("fix", 20)
        self.assertIn("branch    PUSHED  fix  (on origin/fix, idle 20 days)", self.lines())


class Lease(base.Remote):
    def test_a_branch_that_moves_after_the_tip_check_is_not_deleted(self):
        tip = self.remote_only("old-spike", 40)
        self.advance_origin("old-spike")
        moved = self.on_origin("old-spike")
        with mock.patch.object(sweep, "_remote_tip", return_value=tip):
            code, out = self.apply("origin/old-spike")
        self.assertEqual(code, 0, out)
        self.assertIn("kept      origin/old-spike  (origin/old-spike moved since it was judged)",
                      out.splitlines())
        self.assertEqual(self.on_origin("old-spike"), moved)
        self.assertEqual(out.splitlines()[-1], "[OK] sweep --apply  (0 removed, 1 kept)")

    def test_an_unmoved_branch_is_still_deleted(self):
        self.remote_only("old-spike", 40)
        code, out = self.apply("origin/old-spike")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.on_origin("old-spike"), "")


if __name__ == "__main__":
    unittest.main()
