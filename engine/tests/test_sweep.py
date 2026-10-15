"""`govern sweep`: a read-only report of every worktree and local branch with a verdict. Against
real repositories (a bare remote, linked worktrees) and a stand-in `gh` named by CONTEXT_GATE_GH.

    python3 -m unittest discover -s engine/tests -k test_sweep
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_support import Build  # noqa: E402
from base_support import fm, wtext  # noqa: E402
from test_build_workspace import Space  # noqa: E402
from govern import build, profile, sweep  # noqa: E402
from govern.checks import working  # noqa: E402

GH = """import json, pathlib, sys
here = pathlib.Path(__file__).resolve().parent
if (here / "fail").exists():
    sys.exit(1)
args = sys.argv[1:]
head = args[args.index("--head") + 1]
print(json.dumps(json.loads((here / "prs.json").read_text(encoding="utf-8")).get(head, [])))
"""


def pr(number: int, state: str, head: str = "", base: str = "main") -> dict:
    return {"number": number, "state": state, "headRefOid": head, "baseRefName": base}


class Sweep(Build):
    """A single repository on `main` with a bare `origin`, and a stand-in `gh` that answers from
    `prs`: branch name -> the pull requests whose head it is."""

    def setUp(self) -> None:
        super().setUp()
        self.prs: dict[str, list] = {}
        self.cwds: set[str] | None = set()
        self.gh_dir = self.tmp / "gh"
        self.gh_dir.mkdir()
        body = f"#!{sys.executable}\n{GH}"
        if os.name == "nt":
            (self.gh_dir / "gh.py").write_text(GH, encoding="utf-8")
            body = f'@"{sys.executable}" "%~dp0gh.py" %*\r\n'
            self.gh = self.gh_dir / "gh.cmd"
        else:
            self.gh = self.gh_dir / "gh"
        self.gh.write_text(body, encoding="utf-8", newline="")
        self.gh.chmod(self.gh.stat().st_mode | stat.S_IEXEC)
        self.w = self.project(remote=True, branch="")
        patch = mock.patch.object(sweep, "process_cwds", lambda: self.cwds)
        patch.start()
        self.addCleanup(patch.stop)

    def branch_at(self, name: str, commits: int = 0) -> str:
        """A branch cut from `main` with that many commits of its own; `main` stays checked out.
        Returns its tip."""
        self.w.git("checkout", "-qb", name)
        for i in range(commits):
            self.w.write(f"{name}-{i}.txt", f"{name} {i}\n")
            self.w.commit(f"{name} {i}", at=self.w.top, index=False)
        tip = self.head()
        self.w.git("checkout", "-q", "main")
        return tip

    def sweep(self, *argv: str, gh: bool = True) -> tuple[int, str, str]:
        (self.gh_dir / "prs.json").write_text(json.dumps(self.prs), encoding="utf-8")
        env = {"CONTEXT_GATE_GH": str(self.gh) if gh else str(self.gh_dir / "absent")}
        return self.w.run("sweep", *argv, env=env)

    def lines(self, *argv: str, gh: bool = True) -> list[str]:
        code, out, err = self.sweep(*argv, gh=gh)
        self.assertEqual((code, err), (0, ""), out)
        return out.splitlines()

    def worktree(self, branch: str, name: str | None = None) -> Path:
        folder = self.tmp / (name or "wt-" + branch.replace("/", "-"))
        self.w.git("worktree", "add", "-q", str(folder), branch)
        return folder


class Verdicts(Sweep):
    def test_a_branch_in_the_default_branch_is_merged(self):
        self.branch_at("old")
        self.assertIn("branch    MERGED  old", self.lines())

    def test_the_default_branch_and_the_primary_checkout_are_in_use_with_the_reason(self):
        self.branch_at("other")
        self.w.git("checkout", "-q", "other")
        got = self.lines()
        self.assertIn("branch    IN-USE  main  (the default branch)", got)
        self.assertIn("branch    IN-USE  other  (checked out in the primary checkout)", got)

    def test_a_branch_with_no_pr_that_is_not_merged_needs_review(self):
        self.branch_at("feat", 1)
        got = self.lines()
        self.assertIn("branch    REVIEW  feat  (not in main and no PR)", got)
        self.assertIn("unpushed  feat  no upstream", got)

    def test_an_open_pr_needs_review_even_when_the_branch_is_merged(self):
        self.branch_at("feat")
        self.prs["feat"] = [pr(7, "OPEN")]
        self.assertIn("branch    REVIEW  feat  (open PR #7)", self.lines())

    def test_a_squash_merged_pr_at_this_tip_is_pr_merged(self):
        tip = self.branch_at("feat", 1)
        self.prs["feat"] = [pr(3, "MERGED", tip)]
        self.assertIn("branch    PR-MERGED  feat  (PR #3)", self.lines())

    def test_a_merged_pr_at_another_tip_needs_review(self):
        self.branch_at("feat", 2)
        self.prs["feat"] = [pr(3, "MERGED", "0" * 40)]
        self.assertIn("branch    REVIEW  feat  (PR #3 merged, but the branch has moved since)",
                      self.lines())

    def test_a_pr_closed_unmerged_needs_review(self):
        tip = self.branch_at("feat", 1)
        self.prs["feat"] = [pr(4, "CLOSED", tip)]
        self.assertIn("branch    REVIEW  feat  (PR #4 closed unmerged at this tip)", self.lines())

    def test_a_pr_merged_into_another_base_needs_review(self):
        tip = self.branch_at("feat", 1)
        self.prs["feat"] = [pr(5, "MERGED", tip, base="release")]
        self.assertIn("branch    REVIEW  feat  (PR #5 merged into release, not main)",
                      self.lines())

    def test_gh_missing_makes_what_is_not_merged_review_and_keeps_merged(self):
        self.branch_at("old")
        self.branch_at("feat", 1)
        got = self.lines(gh=False)
        self.assertIn("branch    MERGED  old", got)
        self.assertIn("branch    REVIEW  feat  (not in main, and gh could not be asked)", got)

    def test_gh_failing_is_the_same_as_gh_missing(self):
        self.branch_at("feat", 1)
        (self.gh_dir / "fail").write_text("", encoding="utf-8")
        self.assertIn("branch    REVIEW  feat  (not in main, and gh could not be asked)",
                      self.lines())

    def test_a_pushed_branch_with_commits_beyond_its_upstream_is_counted(self):
        self.branch_at("feat", 1)
        self.w.git("push", "-q", "-u", "origin", "feat")
        self.w.git("checkout", "-q", "feat")
        self.w.write("more.txt", "more\n")
        self.w.commit("more", at=self.w.top, index=False)
        self.w.git("checkout", "-q", "main")
        self.assertIn("unpushed  feat  1 commit(s) not on origin/feat", self.lines())

    def test_a_branch_whose_upstream_is_gone_says_so(self):
        self.branch_at("feat", 1)
        self.w.git("push", "-q", "-u", "origin", "feat")
        self.w.git("push", "-q", "origin", "--delete", "feat")
        self.w.git("fetch", "-q", "--prune")
        got = self.lines()
        self.assertIn("unpushed  feat  upstream origin/feat is gone", got)
        self.assertNotIn("unpushed  feat  no upstream", got)

    def test_a_merged_branch_is_not_listed_as_unpushed(self):
        self.branch_at("old")
        self.assertFalse([ln for ln in self.lines() if ln.startswith("unpushed  old")])

    def test_the_branch_of_a_build_in_flight_is_in_use_and_named(self):
        self.branch_at("phase/p9", 1)
        with mock.patch.object(build, "open_builds", return_value=([("p9", "phase/p9")], [])):
            got = self.lines()
        self.assertIn("branch    IN-USE  phase/p9  (the branch of the build p9)", got)
        self.assertIn("build     p9 in flight on phase/p9", got)

    def test_a_started_phase_is_a_build_in_flight(self):
        self.w.git("checkout", "-qb", "phase/p1")
        self.ok("start", "p1")
        self.w.commit("ledger", at=self.w.top, index=False)
        self.assertIn("build     p1 in flight on phase/p1", self.lines())


class Worktrees(Sweep):
    def test_a_merged_worktree_is_merged_and_shown_from_the_repository(self):
        self.branch_at("old")
        folder = self.worktree("old", "wt-old")
        got = self.lines()
        where = os.path.relpath(os.path.realpath(folder), os.path.realpath(self.w.top))
        self.assertIn(f"worktree  MERGED  old  {where}", got)
        self.assertFalse([ln for ln in got if ln.startswith("branch    ") and "  old" in ln])

    def test_uncommitted_changes_are_dirty(self):
        self.branch_at("old")
        folder = self.worktree("old")
        (folder / "loose.txt").write_text("x\n", encoding="utf-8")
        line = [ln for ln in self.lines() if ln.startswith("worktree  ")][0]
        self.assertIn("worktree  DIRTY  old  ", line)
        self.assertTrue(line.endswith("(1 uncommitted change(s))"), line)

    def test_a_process_working_in_it_is_in_use_and_so_is_this_one(self):
        self.branch_at("old")
        folder = self.worktree("old")
        self.cwds = {os.path.realpath(folder / "sub")}
        self.assertIn("IN-USE  old", self.lines()[1])
        self.cwds = set()
        with mock.patch.object(os, "getcwd", return_value=str(folder)):
            self.assertIn("IN-USE  old", self.lines()[1])

    def test_a_missing_folder_needs_review(self):
        self.branch_at("old")
        folder = self.worktree("old")
        shutil.rmtree(folder)
        line = self.lines()[1]
        self.assertIn("worktree  REVIEW  old  ", line)
        self.assertIn("`git worktree prune` clears it", line)

    def test_a_locked_worktree_needs_review(self):
        self.branch_at("old")
        folder = self.worktree("old")
        self.w.git("worktree", "lock", str(folder))
        self.assertTrue(self.lines()[1].endswith("(locked)"), self.lines()[1])

    def test_a_detached_worktree_needs_review(self):
        folder = self.tmp / "wt-detached"
        self.w.git("worktree", "add", "-q", "--detach", str(folder))
        line = self.lines()[1]
        self.assertIn("worktree  REVIEW  (detached)  ", line)
        self.assertTrue(line.endswith("(detached HEAD)"), line)

    def test_a_worktree_on_an_unmerged_branch_takes_the_branch_verdict(self):
        self.branch_at("feat", 1)
        self.worktree("feat")
        self.assertTrue(self.lines()[1].endswith("(not in main and no PR)"), self.lines()[1])

    def test_without_lsof_a_removable_worktree_needs_review_but_a_branch_does_not(self):
        self.branch_at("old")
        self.branch_at("older")
        self.worktree("old")
        self.cwds = None
        got = self.lines()
        self.assertTrue(got[1].endswith("(cannot tell whether a process works in it)"), got[1])
        self.assertIn("worktree  REVIEW  old", got[1])
        self.assertIn("branch    MERGED  older", got)

    def test_status_that_fails_needs_review(self):
        self.branch_at("old")
        self.worktree("old")
        real = sweep.phase._git

        def flaky(repo, *args, **kw):
            if args[:1] == ("status",):
                return None
            return real(repo, *args, **kw)
        with mock.patch.object(sweep.phase, "_git", flaky):
            self.assertTrue(self.lines()[1].endswith("(git status failed)"), self.lines()[1])


class OutsidePrograms(unittest.TestCase):
    def test_lsof_output_is_read_as_the_directories_processes_stand_in(self):
        out = subprocess.CompletedProcess([], 0, b"p1\nfcwd\nn/\np2\nfcwd\nn/tmp\n", b"")
        with mock.patch.object(sweep, "_run", return_value=out), \
                mock.patch.object(os, "name", "posix"), \
                mock.patch.object(os, "getuid", create=True, return_value=501):
            got = sweep.process_cwds()
        self.assertEqual(got, {os.path.realpath("/"), os.path.realpath("/tmp")})

    def test_lsof_missing_failing_or_a_system_without_it_is_unknown(self):
        with mock.patch.object(os, "name", "posix"), \
                mock.patch.object(os, "getuid", create=True, return_value=501):
            with mock.patch.object(sweep, "_run", return_value=None):
                self.assertIsNone(sweep.process_cwds())
            with mock.patch.object(sweep, "_run",
                                   return_value=subprocess.CompletedProcess([], 1, b"", b"")):
                self.assertIsNone(sweep.process_cwds())
        with mock.patch.object(os, "name", "nt"), mock.patch.object(sweep, "_run") as ran:
            self.assertIsNone(sweep.process_cwds())
        ran.assert_not_called()

    def test_a_posix_name_without_getuid_is_unknown(self):
        saved = getattr(os, "getuid", None)
        if saved is not None:
            del os.getuid
            self.addCleanup(setattr, os, "getuid", saved)
        with mock.patch.object(os, "name", "posix"), mock.patch.object(sweep, "_run") as ran:
            self.assertIsNone(sweep.process_cwds())
        ran.assert_not_called()


class Whole(Sweep):
    def test_the_report_ends_with_the_counts_and_the_auto_remove_line(self):
        self.branch_at("old")
        self.branch_at("feat", 1)
        got = self.lines()
        self.assertEqual(got[0], f"== {self.w.top.name} (.) ==")
        self.assertEqual(got[-2], "auto-remove  none: ask before every removal")
        self.assertEqual(got[-1], "[OK] sweep  (MERGED 1, IN-USE 1, REVIEW 1)")

    def test_auto_remove_is_read_from_the_config_and_an_unknown_value_is_refused(self):
        cfg = Path(self.w.root) / ".context-gate" / "config.toml"
        original = cfg.read_text(encoding="utf-8")
        cfg.write_text(original + '\n[sweep]\nauto_remove = ["merged", "pr-merged"]\n',
                       encoding="utf-8")
        self.assertIn("auto-remove  merged, pr-merged", self.lines())
        cfg.write_text(original + '\n[sweep]\nauto_remove = ["merged", "anything"]\n',
                       encoding="utf-8")
        code, out, err = self.sweep()
        self.assertNotEqual(code, 0)
        self.assertIn("'anything' is not one of merged, pr-merged", err)
        cfg.write_text(original + '\n[sweep]\nauto_remove = "merged"\n', encoding="utf-8")
        self.assertIn("expected list", self.sweep()[2])
        cfg.write_text(original + '\n[sweep]\nother = []\n', encoding="utf-8")
        self.assertIn("unknown key 'other' in [sweep]", self.sweep()[2])

    def test_the_memory_index_is_counted_against_its_limit(self):
        from govern.checks import memory
        path = memory.memory_index_path(self.w.context())
        wtext(path, "- [a](a.md) one\n\n- [b](b.md) two\n")
        got = self.lines()
        self.assertIn(f"memory    2 of 40 lines  {path}", got)

    def test_a_finished_file_untouched_for_long_is_listed_with_its_age(self):
        self.w.write("docs/working/done.md", fm("working", status="complete") + "# Done\n")
        self.w.commit("done", at=self.w.top, index=False)
        line = [ln for ln in self.lines() if ln.startswith("finished  ")][0]
        self.assertTrue(line.startswith("finished  docs/working/done.md  status complete, "
                                        "last touched "), line)
        self.assertTrue(line.endswith("d ago"), line)

    def test_a_repository_git_cannot_read_is_reported_and_the_exit_is_1(self):
        profile.rmtree(self.w.top / ".git")
        code, out, err = self.sweep()
        self.assertEqual(code, 1, out + err)
        lines = out.splitlines()
        self.assertEqual(lines[0], f"== {self.w.top.name} (.) ==")
        self.assertTrue(lines[1].startswith("error     "), lines[1])
        self.assertEqual(lines[-1], "[FAIL] sweep  (none)")

    def test_project_is_refused_in_one_repository(self):
        code, out, err = self.sweep("--project", "x")
        self.assertEqual(code, 1)
        self.assertIn("--project applies only in a workspace", err)

    def test_collect_gives_items_with_the_fields_other_commands_read(self):
        self.branch_at("old")
        items = sweep.collect(self.w.context())
        old = [i for i in items if i.branch == "old"][0]
        self.assertEqual((old.kind, old.verdict, old.path, old.name, old.repo.resolve()),
                         ("branch", "MERGED", None, self.w.top.name, self.w.top.resolve()))
        self.assertEqual(old.tip, self.head("old"))


class FinishedFiles(Sweep):
    """`finished-files` reports what it always did, now through `finished_candidates`."""

    def findings(self):
        w = self.w
        ctx = w.context()
        scope = ctx.registry.scopes[0]
        params = ctx.cfg.checks["finished-files"].params
        return ctx, scope, params, working.finished_files(ctx, params, scope)

    def test_findings_and_candidates_agree(self):
        w = self.w
        w.write("docs/working/old.md", fm("working", status="complete") + "# Old\n")
        w.write("docs/working/ticked.md", fm("working", status="active") + "- [x] one\n")
        w.commit("old", at=w.top, index=False)
        w.write("docs/working/new.md", fm("working", status="complete") + "# New\n")
        ctx, scope, params, f = self.findings()
        self.assertEqual(len(f.warnings), 1)
        self.assertTrue(f.warnings[0].endswith("docs/working/ticked.md: every box is ticked: "
                                               "delete it (git keeps it), or set its status "
                                               "to complete"), f.warnings)
        self.assertEqual(len(f.errors), 2)
        self.assertTrue(f.errors[0].endswith("is finished but was never committed — commit it "
                                             "once so git keeps it, then delete it"))
        self.assertIn("docs/working/new.md", f.errors[0])
        self.assertRegex(f.errors[1], r"docs/working/old\.md: status 'complete', last touched "
                                      r"\d+d ago \(finished-files max_days=7\) — delete it; "
                                      r"git keeps history$")
        got = working.finished_candidates(ctx, params, scope)
        self.assertEqual([(p.name, s) for p, s, _ in got], [("old.md", "complete")])
        self.assertGreater(got[0][2], 7)


class InAWorkspace(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        env = mock.patch.dict(os.environ, {"CONTEXT_GATE_GH": str(Path(self._tmp.name) / "none"),
                                           "GIT_CONFIG_NOSYSTEM": "1"})
        env.start()
        self.addCleanup(env.stop)
        patch = mock.patch.object(sweep, "process_cwds", lambda: set())
        patch.start()
        self.addCleanup(patch.stop)
        self.w = Space(Path(self._tmp.name))
        self.w.git("branch", "old", at=self.w.member("alpha"))
        self.w.git("branch", "spare", at=self.w.member("beta"))

    def test_all_repositories_are_swept_when_no_project_is_named(self):
        code, out, err = self.w.run("sweep")
        self.assertEqual((code, err), (0, ""), out)
        heads = [ln for ln in out.splitlines() if ln.startswith("== ")]
        self.assertEqual(heads, ["== space (.) ==", "== alpha (alpha) ==", "== beta (beta) =="])
        self.assertIn("branch    MERGED  old", out)
        self.assertIn("branch    MERGED  spare", out)

    def test_project_sweeps_only_that_members_repository(self):
        code, out, err = self.w.run("sweep", "--project", "alpha")
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual([ln for ln in out.splitlines() if ln.startswith("== ")],
                         ["== alpha (alpha) =="])
        self.assertIn("branch    MERGED  old", out)
        self.assertNotIn("spare", out)

    def test_an_unknown_project_is_refused(self):
        code, out, err = self.w.run("sweep", "--project", "nope")
        self.assertEqual(code, 1)
        self.assertIn("no project 'nope'", err)


if __name__ == "__main__":
    unittest.main()
