"""`govern sweep`: the PUSHED verdict, a branch whose exact tip is on its remote and has sat idle,
and its removal. Against real repositories with a bare `origin`, as `test_sweep` builds them.

    python3 -m unittest discover -s engine/tests -k test_sweep_pushed
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import profile_guard  # noqa: E402,F401

from test_sweep import Sweep, pr  # noqa: E402
from govern import config  # noqa: E402

DAY = 86400


class Pushed(Sweep):
    def commit_aged(self, name: str, days: int, push: bool = True) -> str:
        """A branch `name` cut from `main`, one commit `days` old, pushed unless told not to;
        `main` stays checked out. Returns the tip."""
        stamp = f"{int(time.time()) - days * DAY} +0000"
        self.w.git("checkout", "-qb", name)
        self.w.write(f"{name}.txt", f"{name}\n")
        self.w.git("add", "-A")
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(HOME=str(self.w.home), GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_DATE=stamp,
                   GIT_COMMITTER_DATE=stamp)
        subprocess.run(["git", "-C", str(self.w.root), "-c", "user.email=t@t", "-c", "user.name=t",
                        "-c", "commit.gpgSign=false", "commit", "-qm", name],
                       check=True, capture_output=True, env=env, cwd=str(self.tmp))
        tip = self.head()
        if push:
            self.w.git("push", "-q", "origin", name)
        self.w.git("checkout", "-q", "main")
        return tip

    def set_config(self, text: str) -> None:
        cfg = Path(self.w.root) / ".context-gate" / "config.toml"
        cfg.write_text(cfg.read_text(encoding="utf-8") + "\n" + text, encoding="utf-8")

    def apply(self, *names: str) -> tuple[int, str]:
        code, out, err = self.sweep("--apply", "--only", *names)
        self.assertEqual(err, "", out)
        return code, out


class Verdict(Pushed):
    def test_pushed_and_idle_twenty_days_is_pushed_with_the_note(self):
        self.commit_aged("spike", 20)
        got = self.lines()
        self.assertIn("branch    PUSHED  spike  (on origin/spike, idle 20 days)", got)
        self.assertNotIn("unpushed  spike  no upstream", got)
        self.assertEqual(got[-1], "[OK] sweep  (PUSHED 1, IN-USE 1)")

    def test_three_days_idle_needs_review(self):
        self.commit_aged("spike", 3)
        self.assertIn("branch    REVIEW  spike  (not in main and no PR)", self.lines())

    def test_a_remote_tip_that_differs_needs_review(self):
        self.commit_aged("spike", 20)
        self.w.git("checkout", "-q", "spike")
        self.w.write("more.txt", "more\n")
        self.w.commit("more", index=False)
        self.w.git("checkout", "-q", "main")
        got = self.lines()
        self.assertIn("branch    REVIEW  spike  (not in main and no PR)", got)
        self.assertIn("unpushed  spike  no upstream", got)

    def test_an_upstream_one_commit_behind_is_review_and_counted_unpushed(self):
        self.commit_aged("spike", 20)
        self.w.git("branch", "-u", "origin/spike", "spike")
        self.w.git("checkout", "-q", "spike")
        self.w.write("more.txt", "more\n")
        self.w.commit("more", index=False)
        self.w.git("checkout", "-q", "main")
        got = self.lines()
        self.assertIn("branch    REVIEW  spike  (not in main and no PR)", got)
        self.assertIn("unpushed  spike  1 commit(s) not on origin/spike", got)

    def test_an_open_pr_needs_review(self):
        self.commit_aged("spike", 20)
        self.prs["spike"] = [pr(4, "OPEN")]
        self.assertIn("branch    REVIEW  spike  (open PR #4)", self.lines())

    def test_a_pr_closed_unmerged_at_this_tip_is_pushed(self):
        tip = self.commit_aged("spike", 20)
        self.prs["spike"] = [pr(5, "CLOSED", tip)]
        self.assertIn("branch    PUSHED  spike  (on origin/spike, idle 20 days)", self.lines())

    def test_stale_days_zero_means_any_age(self):
        self.commit_aged("spike", 0)
        self.set_config("[sweep]\nstale_days = 0\n")
        self.assertIn("branch    PUSHED  spike  (on origin/spike, idle 0 days)", self.lines())

    def test_a_clean_worktree_on_a_pushed_branch_is_pushed(self):
        self.commit_aged("spike", 20)
        folder = self.worktree("spike")
        got = self.lines()
        self.assertIn(f"worktree  PUSHED  spike  {os.path.relpath(folder.resolve(), self.w.top.resolve())}"
                      "  (on origin/spike, idle 20 days)", got)


class Removal(Pushed):
    def test_apply_removes_it_and_the_bundle_restores_it_at_the_same_tip(self):
        tip = self.commit_aged("spike", 20)
        code, out = self.apply("spike")
        self.assertEqual(code, 0, out)
        self.assertIn("removed   branch spike", out.splitlines())
        self.assertEqual(self.w.git("branch", "--list", "spike").strip(), "")
        bundle = next(line.split(None, 1)[1] for line in out.splitlines()
                      if line.startswith("bundle "))
        self.w.git("fetch", "-q", bundle, "spike:spike")
        self.assertEqual(self.head("spike"), tip)

    def test_a_remote_that_lost_the_branch_after_the_survey_keeps_it(self):
        self.commit_aged("spike", 20)
        self.w.git("branch", "-D", "spike", at=self.tmp / "remote.git")
        code, out = self.apply("spike")
        self.assertEqual(code, 0, out)
        self.assertIn("kept      spike  (origin no longer holds this tip)", out.splitlines())
        self.assertEqual(out.splitlines()[-1], "[OK] sweep --apply  (0 removed, 1 kept)")
        self.assertIn("spike", self.w.git("branch", "--list", "spike"))
        self.assertNotIn("bundle ", out)

    def test_a_remote_that_moved_on_keeps_it(self):
        self.commit_aged("spike", 20)
        other = self.tmp / "other"
        self.w.git("clone", "-q", str(self.tmp / "remote.git"), str(other), at=self.tmp)
        self.w.git("checkout", "-q", "spike", at=other)
        (other / "x.txt").write_text("x\n", encoding="utf-8")
        self.w.git("add", "-A", at=other)
        self.w.git("commit", "-qm", "x", at=other)
        self.w.git("push", "-q", "origin", "spike", at=other)
        code, out = self.apply("spike")
        self.assertIn("kept      spike  (origin no longer holds this tip)", out.splitlines())


class Configured(Pushed):
    def test_no_auto_remove_key_and_an_empty_list_both_mean_none(self):
        self.assertEqual(config.SWEEP_AUTO_REMOVE_DEFAULT, ())
        self.assertIn("auto-remove  none: ask before every removal", self.lines())
        self.set_config("[sweep]\nauto_remove = []\n")
        self.assertIn("auto-remove  none: ask before every removal", self.lines())

    def test_a_set_list_is_printed_without_default(self):
        self.set_config('[sweep]\nauto_remove = ["pushed"]\n')
        self.assertIn("auto-remove  pushed", self.lines())

    def test_a_bogus_verdict_and_a_bad_stale_days_are_refused(self):
        self.set_config('[sweep]\nauto_remove = ["bogus"]\n')
        code, out, err = self.sweep()
        self.assertNotEqual(code, 0)
        self.assertIn("'bogus' is not one of merged, pr-merged, pushed, stale", err)

    def test_a_negative_stale_days_is_refused(self):
        self.set_config("[sweep]\nstale_days = -1\n")
        code, out, err = self.sweep()
        self.assertNotEqual(code, 0)
        self.assertIn("[sweep] stale_days", err)

    def test_a_text_stale_days_is_refused(self):
        self.set_config('[sweep]\nstale_days = "soon"\n')
        code, out, err = self.sweep()
        self.assertNotEqual(code, 0)
        self.assertIn("[sweep] stale_days", err)


if __name__ == "__main__":
    unittest.main()
