"""`govern sweep --apply --only`: removes exactly the named MERGED or PR-MERGED items after a
verified backup bundle, never forcing. Against real repositories, as `test_sweep` builds them.

    python3 -m unittest discover -s engine/tests -k test_sweep_apply
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_build_workspace import Space  # noqa: E402
from test_sweep import Sweep, pr  # noqa: E402
from govern import sweep  # noqa: E402


class Apply(Sweep):
    def apply(self, *names: str) -> tuple[int, str]:
        code, out, err = self.sweep("--apply", "--only", *names)
        self.assertEqual(err, "", out)
        return code, out

    def branches(self) -> list[str]:
        return self.w.git("for-each-ref", "--format=%(refname:short)", "refs/heads/").split()

    def test_only_the_named_removable_items_go(self):
        self.branch_at("old")
        self.branch_at("spare")
        self.branch_at("tree")
        folder = self.worktree("tree")
        code, out = self.apply("old", "tree")
        self.assertEqual(code, 0, out)
        got = out.splitlines()
        self.assertIn(f"removed   worktree {folder.resolve()}", got)
        self.assertIn("removed   branch old", got)
        self.assertIn("removed   branch tree", got)
        self.assertEqual(got[-1], "[OK] sweep --apply  (3 removed, 0 kept)")
        self.assertEqual(self.branches(), ["main", "spare"])
        self.assertFalse(folder.exists())

    def test_one_bundle_per_repository_under_the_git_directory_and_it_verifies(self):
        self.branch_at("old")
        self.branch_at("older")
        _, out = self.apply("old", "older")
        bundles = [ln.split(None, 1)[1] for ln in out.splitlines() if ln.startswith("bundle    ")]
        self.assertEqual(len(bundles), 1)
        path = Path(bundles[0])
        self.assertEqual(path.name, "sweep.bundle")
        self.assertEqual(path.parent.parent, (self.w.root / ".git" / "context-gate" /
                                                      "sweep").resolve())
        self.w.git("bundle", "verify", str(path))
        self.branch_at("third")
        _, again = self.apply("third")
        self.assertIn("sweep-2.bundle", again)

    def test_review_dirty_and_in_use_names_are_each_skipped(self):
        self.branch_at("feat", 1)
        self.branch_at("busy")
        self.branch_at("dirty")
        busy = self.worktree("busy")
        dirty = self.worktree("dirty")
        (dirty / "scratch.txt").write_text("x\n", encoding="utf-8")
        self.cwds = {os.path.realpath(busy)}
        code, out = self.apply("feat", "busy", "dirty", "main", "nope")
        got = out.splitlines()
        self.assertEqual(code, 0, out)
        self.assertIn("skipped  feat  (REVIEW: not in main and no PR)", got)
        self.assertIn("skipped  busy  (IN-USE: a process is working in it)", got)
        self.assertIn("skipped  dirty  (DIRTY: 1 uncommitted change(s))", got)
        self.assertIn("skipped  main  (IN-USE: the default branch)", got)
        self.assertIn("skipped  nope  (not found)", got)
        self.assertEqual(got[-1], "[OK] sweep --apply  (0 removed, 0 kept)")
        self.assertFalse([ln for ln in got if ln.startswith(("bundle", "removed"))])
        self.assertEqual(self.branches(), ["busy", "dirty", "feat", "main"])
        self.assertTrue(busy.is_dir() and dirty.is_dir())

    def test_a_squash_merged_branch_goes_and_its_backup_restores_it(self):
        tip = self.branch_at("feat", 1)
        self.prs["feat"] = [pr(5, "MERGED", tip)]
        _, out = self.apply("feat")
        self.assertIn("removed   branch feat", out)
        self.assertNotIn("feat", self.branches())
        self.run_restore_line(out)
        self.assertEqual(self.head("refs/heads/feat"), tip)

    def run_restore_line(self, out):
        line = next(ln for ln in out.splitlines() if ln.startswith("restore with: "))
        text = line[len("restore with: "):]
        argv = text if os.name == "nt" else shlex.split(text)
        done = subprocess.run(argv, capture_output=True, text=True, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_the_restore_line_quotes_a_path_the_way_windows_reads_it(self):
        with mock.patch.object(sweep.os, "name", "nt"):
            self.assertEqual(sweep._quote("C:\\a b\\x.bundle"), '"C:\\a b\\x.bundle"')
            self.assertEqual(sweep._quote("C:\\ab\\x.bundle"), "C:\\ab\\x.bundle")

    def test_a_name_given_twice_for_one_item_picks_it_once(self):
        self.branch_at("tree")
        folder = self.worktree("tree")
        name = self.w.top.name
        code, out = self.apply("tree", f"{name}:tree")
        self.assertEqual(code, 0, out)
        self.assertFalse([ln for ln in out.splitlines() if ln.startswith("kept ")], out)
        self.assertIn("removed   branch tree", out)
        self.assertFalse(folder.exists())
        self.assertEqual(self.branches(), ["main"])
        self.assertEqual(out.splitlines()[-1], "[OK] sweep --apply  (2 removed, 0 kept)")

    def test_a_restore_line_survives_a_path_with_a_space(self):
        self.project(branch="", below="with space")
        self.branch_at("feat", 1)
        tip = self.head("refs/heads/feat")
        self.prs["feat"] = [pr(5, "MERGED", tip)]
        _, out = self.apply("feat")
        self.assertNotIn("feat", self.branches())
        self.run_restore_line(out)
        self.assertEqual(self.head("refs/heads/feat"), tip)

    def test_a_bundle_folder_that_cannot_be_made_is_reported_not_raised(self):
        self.branch_at("old")
        with mock.patch.object(Path, "mkdir", side_effect=OSError("read-only")):
            code, out = self.apply("old")
        self.assertEqual(code, 1, out)
        self.assertIn("kept      old  (bundle failed, nothing removed: ", out)
        self.assertIn("read-only", out)
        self.assertIn("old", self.branches())

    def test_a_tip_that_moved_between_the_judgment_and_the_delete_is_kept(self):
        self.branch_at("old")
        with mock.patch.object(sweep, "_tip", lambda repo, branch: "0" * 40):
            code, out = self.apply("old")
        self.assertIn("kept      old  (its tip moved since it was judged)", out)
        self.assertEqual(out.splitlines()[-1], "[OK] sweep --apply  (0 removed, 1 kept)")
        self.assertIn("old", self.branches())

    def test_a_bundle_that_fails_removes_nothing_and_leaves_no_file(self):
        self.branch_at("old")
        folder = self.tmp / "in-the-way"
        folder.write_text("a file, not a folder\n", encoding="utf-8")
        with mock.patch.object(sweep, "_bundle_path", lambda repo: folder / "sweep.bundle"):
            code, out = self.apply("old")
        self.assertEqual(code, 1, out)
        self.assertIn("kept      old  (bundle failed, nothing removed: ", out)
        self.assertEqual(out.splitlines()[-1], "[FAIL] sweep --apply  (0 removed, 1 kept)")
        self.assertIn("old", self.branches())

    def test_a_bundle_that_does_not_verify_is_deleted_and_removes_nothing(self):
        self.branch_at("old")
        real = sweep.phase._git

        def fake(repo, *args, **kw):
            if args[:2] == ("bundle", "verify"):
                return mock.Mock(returncode=1, stdout=b"", stderr=b"bad bundle")
            return real(repo, *args, **kw)

        with mock.patch.object(sweep.phase, "_git", fake):
            code, out = self.apply("old")
        self.assertEqual(code, 1, out)
        self.assertIn("did not verify: bad bundle", out)
        self.assertIn("old", self.branches())
        leftovers = list((self.w.root / ".git" / "context-gate").rglob("*.bundle"))
        self.assertEqual(leftovers, [])

    def test_a_worktree_git_refuses_to_remove_keeps_its_branch(self):
        self.branch_at("tree")
        folder = self.worktree("tree")
        real = sweep.phase._git

        def fake(repo, *args, **kw):
            if args[:2] == ("worktree", "remove"):
                return mock.Mock(returncode=1, stdout=b"", stderr=b"refused")
            return real(repo, *args, **kw)

        with mock.patch.object(sweep.phase, "_git", fake):
            _, out = self.apply("tree")
        self.assertIn("kept      tree  (git worktree remove refused: refused)", out)
        self.assertIn("tree", self.branches())
        self.assertTrue(folder.is_dir())

    def test_nothing_is_forced(self):
        self.branch_at("old")
        seen = []
        real = sweep.phase._git

        def spy(repo, *args, **kw):
            seen.append(args)
            return real(repo, *args, **kw)

        with mock.patch.object(sweep.phase, "_git", spy):
            self.apply("old")
        self.assertFalse([a for a in seen if "--force" in a or "-f" in a])

    def test_only_without_apply_and_apply_without_only_are_refused(self):
        self.branch_at("old")
        code, out, err = self.sweep("--only", "old")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("--only needs --apply", err)
        code, out, err = self.sweep("--apply")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("--apply needs --only", err)
        self.assertIn("old", self.branches())


class InAWorkspace(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        env = mock.patch.dict(os.environ, {"CONTEXT_GATE_GH": str(Path(self._tmp.name) / "none")})
        env.start()
        self.addCleanup(env.stop)
        patch = mock.patch.object(sweep, "process_cwds", lambda: set())
        patch.start()
        self.addCleanup(patch.stop)
        self.w = Space(Path(self._tmp.name))
        for name in ("alpha", "beta"):
            self.w.git("branch", "old", at=self.w.member(name))
        self.w.git("branch", "only-beta", at=self.w.member("beta"))

    def branches(self, name: str) -> list[str]:
        return self.w.git("for-each-ref", "--format=%(refname:short)", "refs/heads/",
                          at=self.w.member(name)).split()

    def test_project_branch_names_one_repository(self):
        code, out, err = self.w.run("sweep", "--apply", "--only", "alpha:old")
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("removed   branch old", out)
        self.assertEqual(self.branches("alpha"), ["main"])
        self.assertEqual(self.branches("beta"), ["main", "old", "only-beta"])

    def test_a_bare_name_in_two_repositories_is_ambiguous_and_removes_neither(self):
        code, out, err = self.w.run("sweep", "--apply", "--only", "old", "only-beta")
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("skipped  old  (ambiguous: alpha:old, beta:old; name one as project:branch)",
                      out)
        self.assertIn("removed   branch only-beta", out)
        self.assertEqual(self.branches("alpha"), ["main", "old"])
        self.assertEqual(self.branches("beta"), ["main", "old"])


if __name__ == "__main__":
    unittest.main()
