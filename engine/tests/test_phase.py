"""`govern phase close`: push the branch, check the remote holds it, then remove what it contains.
Against real repositories in temporary directories, with a local bare repository as the remote
and a HOME of their own (the fixtures `base_support` builds).

    python3 -m unittest discover -s engine/tests -k test_phase
"""
from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from govern import phase  # noqa: E402
from base_support import Case, Tree  # noqa: E402


class Phase(Case):
    """A work repository whose `origin` is a bare repository, default branch `main` unless a
    case says otherwise. The work branch `work` holds two commits past the default."""

    def setUp(self) -> None:
        super().setUp()
        self.t = None

    def repo(self, default: str = "main") -> Tree:
        remote = self.tmp / "remote.git"
        remote.mkdir()
        t = Tree(self.tmp / "work", self.tmp / "home")
        t.root.mkdir()
        t.git("init", "-q", "--bare", at=remote)
        t.git("symbolic-ref", "HEAD", f"refs/heads/{default}", at=remote)
        t.init(branch=default)
        t.write("a.txt", "one\n")
        t.commit("one", index=False)
        t.git("remote", "add", "origin", str(remote))
        t.git("push", "-q", "origin", default)
        t.git("remote", "set-head", "origin", default)
        self.remote = remote
        self.t = t
        return t

    def commit(self, name: str, text: str, at: Path | None = None) -> None:
        self.t.write(name, text) if at is None else (at / name).write_text(text, encoding="utf-8")
        self.t.commit(name, at=at, index=False)

    def work(self) -> Tree:
        """`work` two commits past the default, with `old` at the first of them."""
        t = self.repo()
        t.branch("work")
        self.commit("b.txt", "b\n")
        t.git("branch", "old")
        self.commit("c.txt", "c\n")
        return t

    def close(self, *flags: str) -> tuple[int, str, str]:
        return self.t.run("phase", "close", *flags, "--path", str(self.t.root))

    def state(self) -> tuple[str, str, str]:
        return (self.t.git("for-each-ref", "--format=%(refname) %(objectname) %(upstream)",
                           "refs/heads", "refs/remotes"),
                self.t.git("for-each-ref", "--format=%(refname) %(objectname)", "refs/heads",
                           at=self.remote),
                self.t.git("worktree", "list"))

    def branches(self) -> list[str]:
        return self.t.git("for-each-ref", "--format=%(refname:short)", "refs/heads").split()

    def refused(self, message: str, *flags: str) -> None:
        before = self.state()
        code, out, err = self.close(*flags)
        self.assertEqual(code, 1, err)
        self.assertIn(message, err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(self.state(), before)


class Refusals(Phase):
    def test_a_detached_head_is_refused_and_nothing_changes(self):
        t = self.work()
        t.git("checkout", "-q", "--detach")
        self.refused("detached")

    def test_the_remotes_default_branch_is_refused(self):
        self.work()
        self.t.switch("main")
        self.refused("default branch of origin")

    def test_a_branch_whose_upstream_is_the_default_branch_is_refused_and_not_pushed(self):
        t = self.repo(default="development")
        t.git("checkout", "-q", "-b", "phase-1", "origin/development")
        self.commit("b.txt", "b\n")
        before = t.git("rev-parse", "development", at=self.remote)
        self.refused("is the default branch of origin (origin/development)", "--dry-run")
        self.refused("git push -u origin phase-1")
        self.assertEqual(t.git("rev-parse", "development", at=self.remote), before)

    def test_uncommitted_changes_are_refused(self):
        t = self.work()
        t.write("a.txt", "edited\n")
        self.refused("uncommitted changes")

    def test_no_remote_is_refused(self):
        t = self.work()
        t.git("remote", "remove", "origin")
        self.refused("no remote")

    def test_several_remotes_without_an_upstream_are_listed(self):
        t = self.work()
        t.git("remote", "add", "second", str(self.remote))
        self.refused("origin, second")

    def test_several_remotes_are_fine_with_remote_named(self):
        t = self.work()
        t.git("remote", "add", "second", str(self.remote))
        t.git("fetch", "-q", "second")
        t.git("remote", "set-head", "second", "main")
        code, out, err = self.close("--remote", "second")
        self.assertEqual(code, 0, err)
        self.assertIn("pushed work to second/work", out)

    def test_an_unknown_remote_name_is_refused(self):
        self.work()
        self.refused("no remote named nope", "--remote", "nope")

    def test_a_default_branch_that_cannot_be_found_deletes_nothing(self):
        t = self.work()
        t.git("symbolic-ref", "HEAD", "refs/heads/nowhere", at=self.remote)
        t.git("remote", "set-head", "origin", "--delete")
        self.refused("cannot tell which branch is the default")
        self.assertIn("old", self.branches())

    def test_git_that_cannot_run_is_an_error_not_a_traceback(self):
        self.work()
        with mock.patch.object(phase, "_git", return_value=None):
            code, out, err = self.close()
        self.assertEqual(code, 1)
        self.assertIn("git could not run", err)
        self.assertNotIn("Traceback", err)

    def test_a_path_that_is_no_repository_is_an_error(self):
        t = self.work()
        plain = self.tmp / "plain"
        plain.mkdir()
        code, out, err = t.run("phase", "close", "--path", str(plain))
        self.assertEqual(code, 1)
        self.assertIn("not inside a git repository", err)


class Default(Phase):
    def test_the_default_is_found_from_the_remote_when_the_local_head_is_missing(self):
        t = self.work()
        t.git("remote", "set-head", "origin", "--delete")
        code, out, err = self.close()
        self.assertEqual(code, 0, err)
        self.assertIn("removed old", out)


class Pushing(Phase):
    def test_a_branch_without_an_upstream_is_pushed_with_it_set(self):
        t = self.work()
        code, out, err = self.close()
        self.assertEqual(code, 0, err)
        self.assertIn("pushed work to origin/work", out)
        self.assertIn("upstream set", out)
        self.assertEqual(t.git("rev-parse", "work@{upstream}").strip(),
                         t.git("rev-parse", "work").strip())
        self.assertEqual(t.git("rev-parse", "refs/heads/work", at=self.remote).strip(),
                         t.git("rev-parse", "work").strip())

    def test_a_branch_with_an_upstream_is_pushed_there(self):
        t = self.work()
        t.git("push", "-q", "origin", "work:elsewhere")
        t.git("branch", "--set-upstream-to=origin/elsewhere", "work")
        code, out, err = self.close()
        self.assertEqual(code, 0, err)
        self.assertIn("pushed work to origin/elsewhere", out)
        self.assertNotIn("upstream set", out)
        self.assertEqual(t.git("rev-parse", "refs/heads/elsewhere", at=self.remote).strip(),
                         t.git("rev-parse", "work").strip())
        self.assertEqual(t.git("branch", "--list", "work", at=self.remote).strip(), "")

    def test_a_remote_that_does_not_show_the_commit_deletes_nothing(self):
        self.work()
        real = phase._git

        def disagree(repo, *args, **kw):
            res = real(repo, *args, **kw)
            if args[0] == "ls-remote" and "--symref" not in args:
                return subprocess.CompletedProcess(args, 0, b"0" * 40 + b"\trefs/heads/work\n", b"")
            return res

        with mock.patch.object(phase, "_git", disagree):
            code, out, err = self.close()
        self.assertEqual(code, 1)
        self.assertIn("nothing was deleted", err)
        self.assertIn("old", self.branches())

    def test_a_rejected_push_deletes_nothing(self):
        t = self.work()
        other = self.tmp / "other"
        t.git("clone", "-q", str(self.remote), str(other), at=self.tmp)
        t.git("checkout", "-qb", "work", at=other)
        self.commit("z.txt", "z\n", at=other)
        t.git("push", "-q", "origin", "work", at=other)
        code, out, err = self.close()
        self.assertEqual(code, 1)
        self.assertIn("the push failed", err)
        self.assertIn("nothing was deleted", err)
        self.assertIn("old", self.branches())


class Removing(Phase):
    def test_a_contained_branch_goes_and_one_with_a_commit_of_its_own_stays(self):
        t = self.work()
        t.git("checkout", "-qb", "ahead", "old")
        self.commit("d.txt", "d\n")
        t.switch("work")
        code, out, err = self.close()
        self.assertEqual(code, 0, err)
        self.assertIn("removed old", out)
        self.assertIn("kept ahead: has commits the pushed branch does not", out)
        self.assertEqual(sorted(self.branches()), ["ahead", "main", "work"])

    def test_the_remotes_default_branch_is_kept_with_its_reason(self):
        self.work()
        code, out, err = self.close()
        self.assertIn("kept main: the default branch of origin", out)
        self.assertIn("main", self.branches())

    def test_a_branch_reachable_from_the_default_is_kept_under_another_default(self):
        t = self.repo(default="development")
        t.git("branch", "main")
        t.branch("work")
        self.commit("b.txt", "b\n")
        code, out, err = self.close()
        self.assertEqual(code, 0, err)
        self.assertIn("kept main: already in origin/development", out)
        self.assertIn("kept development: the default branch of origin", out)
        self.assertIn("main", self.branches())

    def test_a_branch_with_an_upstream_of_its_own_is_kept(self):
        t = self.work()
        t.git("push", "-q", "origin", "old")
        t.git("branch", "--set-upstream-to=origin/old", "old")
        code, out, err = self.close()
        self.assertEqual(code, 0, err)
        self.assertIn("kept old: has an upstream of its own (remotes/origin/old)", out)
        self.assertIn("old", self.branches())

    def test_a_branch_named_with_keep_is_kept(self):
        self.work()
        code, out, err = self.close("--keep", "old")
        self.assertEqual(code, 0, err)
        self.assertIn("kept old: named with --keep", out)
        self.assertIn("old", self.branches())


class Worktrees(Phase):
    def test_a_clean_worktree_is_removed_before_its_branch(self):
        t = self.work()
        where = self.tmp / "wt"
        t.git("worktree", "add", "-q", str(where), "old")
        code, out, err = self.close()
        self.assertEqual(code, 0, err)
        self.assertIn("removed old and its worktree", out)
        self.assertFalse(where.exists())
        self.assertNotIn("old", self.branches())

    def test_a_worktree_with_changes_keeps_both_and_says_why(self):
        t = self.work()
        where = self.tmp / "wt"
        t.git("worktree", "add", "-q", str(where), "old")
        (where / "a.txt").write_text("edited\n", encoding="utf-8")
        code, out, err = self.close()
        self.assertEqual(code, 0, err)
        self.assertRegex(out, r"kept old: its worktree stays: .*(modified|untracked|changes)")
        self.assertTrue(where.exists())
        self.assertIn("old", self.branches())

    def test_the_kept_line_names_the_first_three_paths_in_the_way(self):
        t = self.work()
        where = self.tmp / "wt"
        t.git("worktree", "add", "-q", str(where), "old")
        (where / "a.txt").write_text("edited\n", encoding="utf-8")
        for name in ("b.txt", "c.txt", "d.txt"):
            (where / name).write_text("new\n", encoding="utf-8")
        code, out, err = self.close()
        self.assertEqual(code, 0, err)
        kept = next(line for line in out.splitlines() if line.startswith("kept old:"))
        self.assertTrue(kept.endswith(" (in the way: a.txt, b.txt, c.txt)"), kept)


class DryRun(Phase):
    def test_it_prints_the_plan_and_changes_nothing_not_even_a_push(self):
        t = self.work()
        before = self.state()
        code, out, err = self.close("--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("would push work to origin/work, setting the upstream", out)
        self.assertIn("would remove old", out)
        self.assertIn("would keep main: the default branch of origin", out)
        self.assertEqual(self.state(), before)
        self.assertEqual(t.git("branch", "--list", "work", at=self.remote).strip(), "")

    def test_it_names_the_same_branches_the_real_run_then_removes(self):
        self.work()
        _, planned, _ = self.close("--dry-run")
        _, done, _ = self.close()
        for name in re.findall(r"would remove (\S+)", planned):
            self.assertIn(f"removed {name}", done)


class OpenBuildsKept(Phase):
    """A phase built on a phase that is still open: the lower build's branches stay."""

    def stacked(self) -> Tree:
        """`phase/lower` with agent branches `low-a` and `low-b` merged in, and `phase/upper` cut
        from it with `up-a` merged in; `phase/upper` is checked out."""
        t = self.repo()
        t.branch("phase/lower")
        for name in ("low-a", "low-b"):
            t.git("checkout", "-qb", name, "phase/lower")
            self.commit(f"{name}.txt", name)
            t.switch("phase/lower")
            t.git("merge", "-q", "--no-ff", "-m", f"merge {name}", name)
        t.branch("phase/upper")
        t.git("checkout", "-qb", "up-a", "phase/upper")
        self.commit("up-a.txt", "up-a")
        t.switch("phase/upper")
        t.git("merge", "-q", "--no-ff", "-m", "merge up-a", "up-a")
        return t

    def ledger(self, slug: str, branch: str, text: str | None = None,
               at: Path | None = None) -> None:
        """A tracked (committed) ledger in the project at `at`, else at the repository's top."""
        folder = (at or self.t.root) / ".context-gate" / "build" / slug
        folder.mkdir(parents=True)
        (folder / "ledger.toml").write_text(text if text is not None else (
            f'phase = "{slug}"\nbranch = "{branch}"\nbase = "{"0" * 40}"\nstage = "build"\n'),
            encoding="utf-8")
        self.t.git("add", "-f", str(folder / "ledger.toml"))
        self.t.git("commit", "-qm", f"ledger {slug}")

    def kept_lines(self, out: str) -> None:
        for name in ("phase/lower", "low-a", "low-b"):
            self.assertIn(f"would keep {name}:", out)
            self.assertNotIn(f"would remove {name}", out)

    def test_from_a_subdirectory_they_are_kept(self):
        t = self.stacked()
        self.ledger("lower", "phase/lower")
        sub = t.root / "sub"
        sub.mkdir()
        code, out, err = t.run("phase", "close", "--dry-run", "--path", str(sub))
        self.assertEqual(code, 0, err)
        self.kept_lines(out)

    def test_project_below_the_repository_top(self):
        t = self.stacked()
        proj = t.root / "proj"
        proj.mkdir()
        self.ledger("lower", "phase/lower", at=proj)
        code, out, err = t.run("phase", "close", "--dry-run", "--path", str(t.root))
        self.assertEqual(code, 0, err)
        self.kept_lines(out)

    def test_git_failing_on_an_open_build_stops_the_plan(self):
        t = self.stacked()
        real = phase._run

        def fail_merge_base(repo, *args, **kw):
            if args[0] == "merge-base":
                return subprocess.CompletedProcess(args, 128, b"", b"fatal: unable to lock")
            return real(repo, *args, **kw)

        with mock.patch.object(phase, "_run", fail_merge_base):
            with self.assertRaises(phase.PhaseError) as cm:
                phase.plan(t.root, open_builds=[("lower", "phase/lower")])
        self.assertIn("merge-base failed", str(cm.exception))

    def test_lower_phase_branches_are_kept(self):
        t = self.stacked()
        p = phase.plan(t.root, open_builds=[("lower", "phase/lower")])
        self.assertEqual([b for b, _ in p.removals], ["up-a"])
        kept = dict(p.keeps)
        self.assertEqual(kept["phase/lower"], "the branch of the build lower, which is not closed")
        for name in ("low-a", "low-b"):
            self.assertEqual(kept[name], "merged into phase/lower, whose build lower is not closed")

    def test_without_the_ledger_they_are_removed_as_before(self):
        t = self.stacked()
        p = phase.plan(t.root)
        self.assertEqual(sorted(b for b, _ in p.removals),
                         ["low-a", "low-b", "phase/lower", "up-a"])

    def test_the_closing_branch_own_build_keeps_nothing(self):
        t = self.stacked()
        before = phase.plan(t.root)
        p = phase.plan(t.root, open_builds=[("upper", "phase/upper")])
        self.assertEqual(p.removals, before.removals)
        self.assertEqual(p.keeps, before.keeps)

    def test_open_build_with_no_local_branch_is_skipped(self):
        t = self.stacked()
        before = phase.plan(t.root)
        p = phase.plan(t.root, open_builds=[("gone", "phase/gone")])
        self.assertEqual(p.removals, before.removals)
        self.assertEqual(p.keeps, before.keeps)

    def test_unreadable_ledger_stops_the_command(self):
        self.stacked()
        self.ledger("broken", "phase/broken", text="this is [not toml\n")
        before = self.state()
        code, out, err = self.close()
        self.assertEqual(code, 1, err)
        self.assertIn("cannot tell which builds are open", err)
        self.assertEqual(err.count("nothing was changed"), 1, err)
        self.assertEqual(self.state(), before)

    def test_command_reads_the_ledgers(self):
        self.stacked()
        self.ledger("lower", "phase/lower")
        code, out, err = self.close("--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("would keep phase/lower: the branch of the build lower, which is not closed",
                      out)
        self.assertIn("would keep low-a: merged into phase/lower, whose build lower is not closed",
                      out)
        self.assertIn("would keep low-b: merged into phase/lower, whose build lower is not closed",
                      out)
        self.assertIn("would remove up-a", out)


class NeverForced(Phase):
    FORCED = {"-D", "-f", "--force", "--force-with-lease", "--delete"}

    def test_no_command_the_module_runs_carries_a_forcing_flag(self):
        t = self.work()
        where = self.tmp / "wt"
        t.git("worktree", "add", "-q", str(where), "old")
        real, seen = phase._git, []

        def record(repo, *args, **kw):
            seen.append(args)
            return real(repo, *args, **kw)

        with mock.patch.object(phase, "_git", record):
            code, out, err = self.close()
        self.assertEqual(code, 0, err)
        self.assertTrue(any(a[:2] == ("branch", "-d") for a in seen))
        self.assertTrue(any(a[:2] == ("worktree", "remove") for a in seen))
        for args in seen:
            self.assertFalse(self.FORCED & set(args), args)

    def test_the_source_never_spells_one(self):
        text = (ENGINE / "govern" / "phase.py").read_text(encoding="utf-8")
        for flag in self.FORCED:
            self.assertNotRegex(text, rf'"{re.escape(flag)}"')
        self.assertNotRegex(text, r'"[+]refs')


if __name__ == "__main__":
    unittest.main()
