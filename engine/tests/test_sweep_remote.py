"""`govern sweep`: remote branches (merged, STALE, REVIEW), their removal on the remote, and
`--home`, which ends the checkout on the default branch. Against real repositories with a bare
`origin`, as `test_sweep_pushed` builds them.

    python3 -m unittest discover -s engine/tests -k test_sweep_remote
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import profile_guard  # noqa: E402,F401

from test_sweep import pr  # noqa: E402
from test_sweep_pushed import Pushed  # noqa: E402
from govern import build, config  # noqa: E402


class Remote(Pushed):
    def remote_only(self, name: str, days: int) -> str:
        """`origin/<name>`, `days` old and unmerged, with no local branch of that name."""
        tip = self.commit_aged(name, days)
        self.w.git("branch", "-D", name)
        return tip

    def merged_remote(self, name: str) -> str:
        """`origin/<name>` at the tip of `main`."""
        self.w.git("push", "-q", "origin", f"main:refs/heads/{name}")
        return self.head("main")

    def on_origin(self, name: str) -> str:
        return self.w.git("ls-remote", "origin", f"refs/heads/{name}").strip()

    def advance_origin(self, branch: str) -> None:
        """Another clone pushes one more commit to `branch` on origin; this one does not fetch."""
        other = self.tmp / "other"
        self.w.git("clone", "-q", str(self.tmp / "remote.git"), str(other), at=self.tmp)
        self.w.git("checkout", "-q", branch, at=other)
        (other / "advance.txt").write_text("advance\n", encoding="utf-8")
        self.w.git("add", "-A", at=other)
        self.w.git("commit", "-qm", "x", at=other)
        self.w.git("push", "-q", "origin", branch, at=other)


class Verdict(Remote):
    def test_unmerged_forty_days_old_with_no_pr_is_stale(self):
        self.remote_only("old-spike", 40)
        got = self.lines()
        self.assertIn("remote    STALE  origin/old-spike  (idle 40 days)", got)
        self.assertEqual(got[-1], "[OK] sweep  (STALE 1, IN-USE 1)")

    def test_ten_days_old_needs_review(self):
        self.remote_only("old-spike", 10)
        self.assertIn("remote    REVIEW  origin/old-spike  (not in main and no PR)", self.lines())

    def test_merged_into_the_remote_default_is_merged(self):
        self.merged_remote("done")
        self.assertIn("remote    MERGED  origin/done", self.lines())

    def test_a_squash_merged_pr_is_pr_merged(self):
        tip = self.remote_only("old-spike", 3)
        self.prs["old-spike"] = [pr(8, "MERGED", tip)]
        self.assertIn("remote    PR-MERGED  origin/old-spike  (PR #8)", self.lines())

    def test_an_open_pr_needs_review_however_old(self):
        self.remote_only("old-spike", 90)
        self.prs["old-spike"] = [pr(9, "OPEN")]
        self.assertIn("remote    REVIEW  origin/old-spike  (open PR #9)", self.lines())

    def test_gh_missing_needs_review_however_old(self):
        self.remote_only("old-spike", 90)
        got = self.lines(gh=False)
        self.assertIn("remote    REVIEW  origin/old-spike  (not in main, and gh could not be "
                      "asked)", got)

    def test_stale_remote_days_zero_means_never_stale(self):
        self.remote_only("old-spike", 400)
        self.set_config("[sweep]\nstale_remote_days = 0\n")
        self.assertIn("remote    REVIEW  origin/old-spike  (not in main and no PR)", self.lines())

    def test_stale_remote_days_is_read_from_the_config(self):
        self.remote_only("old-spike", 10)
        self.set_config("[sweep]\nstale_remote_days = 5\n")
        self.assertIn("remote    STALE  origin/old-spike  (idle 10 days)", self.lines())

    def test_the_default_branch_and_head_are_not_listed(self):
        got = self.lines()
        self.assertFalse([ln for ln in got if ln.startswith("remote ")], got)

    def test_a_keep_glob_and_an_open_builds_branch_are_not_listed(self):
        self.merged_remote("release-1")
        self.merged_remote("phase/p9")
        self.merged_remote("plain")
        cfg = Path(self.w.root) / ".context-gate" / "config.toml"
        cfg.write_text(cfg.read_text(encoding="utf-8")
                       + '\n[checks.merged-branches]\nkeep = ["release-*"]\n'
                       + '[checks.merged-branches.reasons]\nkeep = "a test"\n', encoding="utf-8")
        with mock.patch.object(build, "open_builds", return_value=([("p9", "phase/p9")], [])):
            got = self.lines()
        self.assertIn("remote    MERGED  origin/plain", got)
        self.assertFalse([ln for ln in got if "origin/release-1" in ln or "origin/phase/p9" in ln],
                         got)

    def test_a_namesake_that_is_not_removable_is_judged_locally_first(self):
        self.commit_aged("spike", 3)
        self.w.git("checkout", "-q", "spike")
        self.w.write("more.txt", "more\n")
        self.w.commit("more", index=False)
        self.w.git("checkout", "-q", "main")
        got = self.lines()
        self.assertFalse([ln for ln in got if ln.startswith("remote ")], got)

    def test_a_merged_namesake_leaves_both_listed(self):
        self.w.git("branch", "done")
        self.merged_remote("done")
        got = self.lines()
        self.assertIn("branch    MERGED  done", got)
        self.assertIn("remote    MERGED  origin/done", got)


class Removal(Remote):
    def test_a_merged_remote_branch_is_deleted_on_origin_and_bundled(self):
        tip = self.merged_remote("done")
        code, out = self.apply("origin/done")
        self.assertEqual(code, 0, out)
        got = out.splitlines()
        self.assertIn("removed   remote origin/done", got)
        self.assertEqual(self.on_origin("done"), "")
        self.assertEqual(self.w.git("branch", "-r", "--list", "origin/done").strip(), "")
        self.assertEqual(got[-1], "[OK] sweep --apply  (1 removed, 0 kept)")
        bundle = next(line.split(None, 1)[1] for line in got if line.startswith("bundle "))
        heads = self.w.git("bundle", "list-heads", bundle)
        self.assertIn(f"{tip} refs/remotes/origin/done", heads)
        restore = next(line for line in got if line.startswith("restore remote:"))
        self.assertIn("refs/restored/origin/done:refs/heads/done", restore)
        self.assertIn("push origin", restore)

    def test_a_stale_branch_is_deleted_and_the_plain_name_does_not_pick_it(self):
        self.remote_only("old-spike", 40)
        code, out = self.apply("old-spike")
        self.assertIn("skipped  old-spike  (not found)", out.splitlines())
        self.assertNotEqual(self.on_origin("old-spike"), "")
        code, out = self.apply("origin/old-spike")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.on_origin("old-spike"), "")

    def test_a_review_remote_branch_is_skipped(self):
        self.remote_only("old-spike", 10)
        code, out = self.apply("origin/old-spike")
        self.assertIn("skipped  origin/old-spike  (REVIEW: not in main and no PR)",
                      out.splitlines())
        self.assertNotEqual(self.on_origin("old-spike"), "")

    def test_a_remote_that_moved_after_the_survey_is_skipped_with_the_moved_line(self):
        self.remote_only("old-spike", 40)
        self.advance_origin("old-spike")
        before = self.on_origin("old-spike")
        code, out = self.apply("origin/old-spike")
        self.assertEqual(code, 0, out)
        self.assertIn("kept      origin/old-spike  (origin/old-spike moved since it was judged)",
                      out.splitlines())
        self.assertEqual(out.splitlines()[-1], "[OK] sweep --apply  (0 removed, 1 kept)")
        self.assertEqual(self.on_origin("old-spike"), before)

    def test_a_second_run_finds_nothing_to_remove(self):
        self.merged_remote("done")
        self.apply("origin/done")
        code, out = self.apply("origin/done")
        self.assertIn("skipped  origin/done  (not found)", out.splitlines())


class Home(Remote):
    def phase_branch(self) -> None:
        """`phase/x` merged into `main` and pushed, and checked out; origin's `main` then moves
        one commit on, in another clone."""
        self.w.git("checkout", "-qb", "phase/x")
        self.w.write("x.txt", "x\n")
        self.w.commit("x", index=False)
        self.w.git("checkout", "-q", "main")
        self.w.git("merge", "-q", "--ff-only", "phase/x")
        self.w.git("push", "-q", "origin", "main")
        self.w.git("checkout", "-q", "phase/x")

    def test_home_from_a_merged_phase_branch_ends_on_the_default_level_with_origin(self):
        self.phase_branch()
        self.advance_origin("main")
        code, out, err = self.sweep("--apply", "--home", "--only", "phase/x")
        self.assertEqual((code, err), (0, ""), out)
        got = out.splitlines()
        self.assertIn("home      on main, level with origin/main (left phase/x)", got)
        self.assertIn("removed   branch phase/x", got)
        self.assertEqual(self.w.git("symbolic-ref", "--short", "HEAD").strip(), "main")
        self.assertEqual(self.head("main"), self.head("origin/main"))
        self.assertEqual(self.head("main"), self.w.git("rev-parse", "origin/main").strip())
        self.assertEqual(self.w.git("branch", "--list", "phase/x").strip(), "")
        self.assertEqual(self.w.git("status", "--porcelain").strip(), "")

    def test_home_fetches_first_and_prunes_what_origin_lost(self):
        self.merged_remote("gone")
        self.w.git("branch", "-D", "gone", at=self.tmp / "remote.git")
        self.assertIn("remote    MERGED  origin/gone", self.lines())
        code, out, err = self.sweep("--apply", "--home", "--only", "origin/gone")
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("skipped  origin/gone  (not found)", out.splitlines())

    def test_a_dirty_tree_stays_put_and_removes_nothing_on_the_current_branch(self):
        self.phase_branch()
        self.w.write("x.txt", "changed\n")
        code, out, err = self.sweep("--apply", "--home", "--only", "phase/x")
        self.assertEqual((code, err), (0, ""), out)
        got = out.splitlines()
        self.assertIn("home      stays on phase/x: the work tree has uncommitted changes", got)
        self.assertIn("skipped  phase/x  (IN-USE: checked out in the primary checkout)", got)
        self.assertEqual(self.w.git("symbolic-ref", "--short", "HEAD").strip(), "phase/x")
        self.assertIn("phase/x", self.w.git("branch", "--list", "phase/x"))

    def test_a_branch_that_cannot_go_stays_put(self):
        self.w.git("checkout", "-qb", "work")
        self.w.write("w.txt", "w\n")
        self.w.commit("w", index=False)
        code, out, err = self.sweep("--apply", "--home", "--only", "work")
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("home      stays on work: REVIEW: not in main and no PR", out.splitlines())
        self.assertEqual(self.w.git("symbolic-ref", "--short", "HEAD").strip(), "work")

    def test_a_default_branch_with_commits_origin_lacks_stops_with_nothing_removed(self):
        self.phase_branch()
        self.w.git("checkout", "-q", "main")
        self.w.write("local.txt", "local\n")
        self.w.commit("local", index=False)
        self.w.git("checkout", "-q", "phase/x")
        self.merged_remote("done")
        code, out, err = self.sweep("--apply", "--home", "--only", "phase/x", "origin/done")
        self.assertEqual((code, err), (1, ""), out)
        self.assertIn("error     the default branch has commits origin does not: push or "
                      "reconcile it first", out.splitlines())
        self.assertNotIn("removed", out)
        self.assertEqual(self.w.git("symbolic-ref", "--short", "HEAD").strip(), "phase/x")
        self.assertNotEqual(self.on_origin("done"), "")
        self.assertIn("phase/x", self.w.git("branch", "--list", "phase/x"))

    def test_a_failed_fetch_stops_with_nothing_removed(self):
        self.phase_branch()
        self.merged_remote("done")
        self.w.git("remote", "set-url", "origin", str(self.tmp / "no-such-remote.git"))
        code, out, err = self.sweep("--apply", "--home", "--only", "phase/x", "origin/done")
        self.assertEqual((code, err), (1, ""), out)
        self.assertIn("error     git fetch --prune origin failed, nothing removed", out)
        self.assertNotIn("removed   ", out)
        self.assertEqual(self.w.git("symbolic-ref", "--short", "HEAD").strip(), "phase/x")

    def test_home_needs_apply(self):
        code, out, err = self.sweep("--home")
        self.assertEqual(code, 1)
        self.assertIn("--home needs --apply", err)


class Configured(Remote):
    def test_a_negative_stale_remote_days_is_refused(self):
        self.set_config("[sweep]\nstale_remote_days = -1\n")
        code, out, err = self.sweep()
        self.assertNotEqual(code, 0)
        self.assertIn("[sweep] stale_remote_days", err)

    def test_the_default_list_is_empty_and_the_key_accepts_stale(self):
        self.assertEqual(config.SWEEP_AUTO_REMOVE_DEFAULT, ())
        self.assertIn("auto-remove  none: ask before every removal", self.lines())
        self.set_config('[sweep]\nauto_remove = ["stale"]\n')
        self.assertIn("auto-remove  stale", self.lines())


if __name__ == "__main__":
    unittest.main()
