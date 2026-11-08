"""`build_root`: a registry key that moves a member's build state into the workspace, and
`build.state_root`, which answers which root holds a repository's build state.

    python3 -m unittest discover -s engine/tests -p test_build_root.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import profile_guard  # noqa: E402,F401

from base_support import Case, Solo  # noqa: E402
from test_build_workspace import Space  # noqa: E402
from govern import __version__, agent_runs, build, layout, manifest, phase, profile  # noqa: E402

PARAMS = {"required_keys": ["name", "dir", "tier"], "tiers": ["full", "registered"]}


def set_build_root(space: Space, value: str) -> None:
    """Add `build_root` to alpha's table in projects.toml."""
    path = space.root / "projects.toml"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace('dir = "alpha"\n', f'dir = "alpha"\nbuild_root = "{value}"\n'),
                    encoding="utf-8", newline="\n")


class Base(Case):
    def setUp(self) -> None:
        super().setUp()
        build.state_root.cache_clear()
        self.addCleanup(build.state_root.cache_clear)

    def space(self, build_root: str | None = None) -> Space:
        w = Space(self.tmp)
        if build_root is not None:
            set_build_root(w, build_root)
        return w


class StateRoot(Base):
    def test_a_plain_repository_is_its_own_state_root(self):
        repo = Solo(self.tmp).root
        self.assertEqual(build.state_root(repo), repo)

    def test_a_member_without_build_root_is_its_own_state_root(self):
        w = self.space()
        self.assertEqual(build.state_root(w.member("alpha")), w.member("alpha"))

    def test_a_member_with_build_root_keeps_its_state_in_the_workspace(self):
        w = self.space("planning/alpha")
        self.assertEqual(build.state_root(w.member("alpha")),
                         w.root.resolve() / "planning" / "alpha")
        self.assertEqual(build.state_root(w.member("beta")), w.member("beta"))

    def test_a_symlink_to_the_member_gives_the_same_answer(self):
        w = self.space("planning/alpha")
        link = self.tmp / "link"
        try:
            os.symlink(w.member("alpha"), link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks cannot be made here")
        self.assertEqual(build.state_root(link), w.root.resolve() / "planning" / "alpha")

    def test_a_workspace_pinning_another_engine_does_not_stop_a_member_without_build_root(self):
        w = self.space()
        path = w.root / layout.CONFIG
        path.write_text(path.read_text(encoding="utf-8").replace(
            f'engine = "{__version__}"', 'engine = "0.8.0"'), encoding="utf-8", newline="\n")
        self.assertIn('engine = "0.8.0"', path.read_text(encoding="utf-8"))
        self.assertEqual(build.state_root(w.member("alpha")), w.member("alpha"))
        self.assertEqual(build.open_builds(w.member("alpha"), remote=False), ([], []))

    def test_an_unreadable_registry_is_a_build_error_naming_the_file(self):
        w = self.space()
        (w.root / "projects.toml").write_text("[[project\n", encoding="utf-8")
        with self.assertRaises(build.BuildError) as got:
            build.state_root(w.member("alpha"))
        self.assertIn("projects.toml", str(got.exception))
        self.assertIn("cannot read the workspace registry", str(got.exception))


class RegistryCheck(Base):
    def found(self, w: Space):
        return manifest.CHECKS["registry"].fn(w.context(), PARAMS)

    def test_a_valid_build_root_is_clean(self):
        w = self.space("planning/alpha")
        self.assertEqual(self.found(w).errors, [])

    def test_outside_the_workspace_is_an_error(self):
        w = self.space("../elsewhere")
        self.assertEqual(self.found(w).errors,
                         ["registry: 'alpha' build_root '../elsewhere' is outside the workspace"])

    def test_inside_the_projects_own_folder_is_an_error(self):
        w = self.space("alpha/state")
        self.assertEqual(self.found(w).errors,
                         ["registry: 'alpha' build_root 'alpha/state' is inside the "
                          "project's own folder"])

    def test_a_folder_not_in_a_git_repository_is_an_error(self):
        w = self.space("planning/alpha")
        profile.rmtree(w.root / ".git")
        self.assertEqual(self.found(w).errors,
                         ["registry: 'alpha' build_root 'planning/alpha' is not in a git "
                          "repository"])


class Started(Base):
    """`start` and the readers, with the state in the workspace."""

    def setUp(self) -> None:
        super().setUp()
        self.w = self.space("planning/state")
        self.w.git("add", "-A")
        self.w.git("commit", "-qm", "build_root")
        self.w.phase_branch("alpha")
        self.alpha = self.w.member("alpha")
        self.ledger = self.w.root.resolve() / "planning" / "state" / build.BUILD_DIR / "p1" \
            / build.LEDGER

    def go(self, *argv: str) -> tuple[int, str, str]:
        return self.w.run("build", *argv)

    def start(self) -> str:
        code, out, err = self.go("--project", "alpha", "start", "p1")
        self.assertEqual((code, err), (0, ""), out)
        return out

    def test_start_writes_the_ledger_in_the_workspace_and_prints_it_from_there(self):
        out = self.start()
        self.assertTrue(self.ledger.is_file())
        self.assertFalse((self.alpha / build.BUILD_DIR).exists())
        shown = str(Path("planning") / "state" / build.BUILD_DIR / "p1" / build.LEDGER)
        self.assertIn(f"wrote {shown}", out)
        self.assertEqual(self.w.git("status", "--porcelain", at=self.alpha), "")

    def test_the_member_excludes_its_build_folder_once_even_after_a_restart(self):
        self.start()
        exclude = self.alpha / ".git" / "info" / "exclude"
        line = f"/{build.BUILD_DIR}/\n"
        self.assertEqual(exclude.read_text(encoding="utf-8").count(line), 1)
        self.w.git("add", "-A")
        self.w.git("commit", "-qm", "ledger")
        self.start()
        self.assertEqual(exclude.read_text(encoding="utf-8").count(line), 1)

    def test_a_member_below_its_repository_top_excludes_its_own_folder_once(self):
        w = Space(self.tmp / "sub", repos=False)
        set_build_root(w, "planning/alpha")
        member = w.member("alpha")
        prefix = w.git("rev-parse", "--show-prefix", at=member).strip()
        self.assertEqual(prefix, "alpha/")
        build._exclude_state(member)
        build._exclude_state(member)
        exclude = w.root / ".git" / "info" / "exclude"
        line = f"/{prefix}{build.BUILD_DIR}/"
        self.assertEqual(exclude.read_text(encoding="utf-8").splitlines().count(line), 1)
        probe = f"{build.BUILD_DIR}/x/{build.LEDGER}"
        env = {**os.environ, "HOME": str(w.home), "GIT_CONFIG_NOSYSTEM": "1"}
        codes = [subprocess.run(["git", "-C", str(at), "check-ignore", "-q", probe],
                                capture_output=True, env=env, cwd=self.tmp).returncode
                 for at in (member, w.root)]
        self.assertEqual(codes, [0, 1])

    def test_an_exclude_file_without_a_final_newline_gets_one_first(self):
        exclude = self.alpha / ".git" / "info" / "exclude"
        exclude.parent.mkdir(exist_ok=True)
        exclude.write_bytes(b"*.tmp")
        self.start()
        self.assertEqual(exclude.read_bytes(), f"*.tmp\n/{build.BUILD_DIR}/\n".encode("utf-8"))

    def test_uncommitted_changes_under_the_state_folder_refuse_and_write_nothing(self):
        stray = self.ledger.parent.parent / "stray.txt"
        stray.parent.mkdir(parents=True)
        stray.write_text("x\n", encoding="utf-8")
        code, out, err = self.go("--project", "alpha", "start", "p1")
        self.assertEqual(code, 1, out)
        shown = str(Path("planning") / "state" / build.BUILD_DIR)
        self.assertIn(f"uncommitted changes under {shown}: commit them first", err)
        self.assertFalse(self.ledger.exists())
        exclude = self.alpha / ".git" / "info" / "exclude"
        self.assertNotIn(build.BUILD_DIR, exclude.read_text(encoding="utf-8")
                         if exclude.exists() else "")

    def test_open_builds_reads_the_ledger_from_the_workspace_repository(self):
        self.start()
        self.assertEqual(build.open_builds(self.alpha, remote=False), ([], []))
        self.w.git("add", "-A")
        self.w.git("commit", "-qm", "ledger")
        got = build.open_builds(self.alpha, remote=False)
        self.assertEqual(got, ([("p1", "phase/p1")], []))
        remote = self.tmp / "remote.git"
        remote.mkdir()
        self.w.git("init", "-q", "--bare", at=remote)
        self.w.git("symbolic-ref", "HEAD", "refs/heads/main", at=remote)
        self.w.git("remote", "add", "origin", str(remote), at=self.alpha)
        self.w.git("push", "-q", "origin", "main", at=self.alpha)
        self.w.git("remote", "set-head", "origin", "main", at=self.alpha)
        self.w.git("checkout", "-qb", "phase/p2", at=self.alpha)
        kept = dict(phase.plan(self.alpha, open_builds=got[0]).keeps)
        self.assertIn("phase/p1", kept)

    def test_phase_close_and_sweep_keep_the_branch_of_the_open_build(self):
        self.start()
        self.w.git("add", "-A")
        self.w.git("commit", "-qm", "ledger")
        remote = self.tmp / "remote.git"
        remote.mkdir()
        self.w.git("init", "-q", "--bare", at=remote)
        self.w.git("symbolic-ref", "HEAD", "refs/heads/main", at=remote)
        self.w.git("remote", "add", "origin", str(remote), at=self.alpha)
        self.w.git("push", "-q", "origin", "main", at=self.alpha)
        self.w.git("remote", "set-head", "origin", "main", at=self.alpha)
        self.w.git("checkout", "-qb", "phase/p2", at=self.alpha)
        code, out, err = self.w.run("phase", "close", "--path", str(self.alpha))
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("the branch of the build p1, which is not closed", out)
        heads = self.w.git("branch", "--format=%(refname:short)", at=self.alpha).split()
        self.assertIn("phase/p1", heads)
        code, out, err = self.w.run("sweep", "--project", "alpha")
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("IN-USE  phase/p1  (the branch of the build p1)", out)
        heads = self.w.git("branch", "--format=%(refname:short)", at=self.alpha).split()
        self.assertIn("phase/p1", heads)

    def test_status_finds_the_build_with_and_without_project(self):
        self.start()
        for argv in (("status", "--project", "alpha"), ("status",)):
            code, out, err = self.go(*argv)
            self.assertEqual((code, err), (0, ""), out)
            self.assertIn("p1", out)
        code, out, _ = self.go("status", "--json", "--project", "alpha")
        self.assertEqual(json.loads(out)["ledger"], "planning/state/.context-gate/build/p1/ledger.toml")

    def test_a_plan_is_read_from_the_state_folder(self):
        plan = self.w.root / "planning" / "state" / "plan.md"
        plan.parent.mkdir(parents=True)
        plan.write_text("# Plan\n", encoding="utf-8")
        self.w.git("add", "-A")
        self.w.git("commit", "-qm", "plan")
        code, out, err = self.go("--project", "alpha", "start", "p1", "--plan", "plan.md")
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(build.load(self.ledger).plan, "plan.md")


class Cycle(Started):
    """Launching, collecting and closing with the state in the workspace."""

    def state(self) -> Path:
        return self.w.root.resolve() / "planning" / "state"

    def fill(self, folder: Path) -> None:
        for brief in folder.rglob("*brief*.md"):
            text = brief.read_text(encoding="utf-8")
            brief.write_text("".join("done\n" if line.startswith("<!-- fill") else line
                                     for line in text.splitlines(keepends=True)),
                             encoding="utf-8", newline="\n")

    def commit(self, message: str) -> None:
        self.w.git("add", "-A")
        self.w.git("commit", "-qm", message)

    def unit(self) -> Path:
        self.start()
        code, out, err = self.go("--project", "alpha", "unit", "add", "u1", "--title", "First", "--role",
                                 "developer")
        self.assertEqual((code, err), (0, ""), out)
        brief = self.ledger.parent / "u1" / "brief.md"
        self.fill(brief.parent)
        return brief

    def test_the_whole_cycle_leaves_no_state_in_the_members_history(self):
        brief = self.unit()
        self.commit("brief")
        code, out, err = self.go("--project", "alpha", "launch", "u1")
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn(build.launch_line(brief.resolve()), out)
        tree = Path(next(l for l in out.splitlines() if l.startswith("Worktree: "))[10:])
        report = tree / build.BUILD_DIR / "p1" / "u1" / "developer-report.md"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("done\n", encoding="utf-8")
        (tree / "code.txt").write_text("x\n", encoding="utf-8")
        self.w.git("add", "code.txt", at=tree)
        self.w.git("commit", "-qm", "work", at=tree)
        code, out, err = self.go("--project", "alpha", "unit", "set", "u1", "done", "--branch",
                                 "build/p1/u1")
        self.assertEqual((code, err), (0, ""), out)
        copied = self.state() / build.BUILD_DIR / "p1" / "u1" / "developer-report.md"
        self.assertTrue(copied.is_file())
        shown = Path("planning") / "state" / build.BUILD_DIR / "p1" / "u1" / "developer-report.md"
        self.assertIn(f"copied {shown.as_posix()}", out)
        self.commit("unit")
        self.w.git("merge", "-q", "build/p1/u1", at=self.alpha)
        for step in ("review", "fix", "close"):
            code, out, err = self.go("--project", "alpha", step)
            self.assertEqual((code, err), (0, ""), out)
            self.assertNotIn(str(self.w.root), out.replace(str(self.w.root / ".claude"), ""))
            if step == "review":
                self.fill(self.ledger.parent / "review")
                self.commit("review")
            if step == "fix":
                self.fill(self.ledger.parent / "fix")
                self.commit("fix")
        self.assertIn(f"git -C planning/state rm -r -q .context-gate/build/p1", out)
        log = self.w.git("log", "--all", "--name-only", "--format=", at=self.alpha)
        self.assertFalse([n for n in log.splitlines() if n.startswith(".context-gate/")], log)

    def test_a_committed_unit_is_dropped_with_its_folder_and_can_be_added_again(self):
        brief = self.unit()
        self.commit("brief")
        code, out, err = self.go("--project", "alpha", "unit", "drop", "u1")
        self.assertEqual((code, err), (0, ""), out)
        self.assertNotIn("kept", out)
        self.assertFalse(brief.parent.exists())
        code, out, err = self.go("--project", "alpha", "unit", "add", "u1", "--title", "First",
                                 "--role", "developer")
        self.assertEqual((code, err), (0, ""), out)

    def test_launch_refuses_a_brief_not_committed_in_the_workspace_repository(self):
        self.unit()
        code, out, err = self.go("--project", "alpha", "launch", "u1")
        self.assertEqual(code, 1, out)
        self.assertIn("is not committed at HEAD", err)
        self.assertIn(str(Path("planning") / "state"), err)

    def test_collect_copies_an_ignored_file_into_the_state_folder(self):
        self.start()
        self.commit("ledger")
        tree = self.tmp / "agent"
        self.w.git("worktree", "add", "-q", str(tree), "-b", "agent/x", at=self.alpha)
        stray = tree / build.BUILD_DIR / "p1" / "red" / "out.txt"
        stray.parent.mkdir(parents=True)
        stray.write_text("red\n", encoding="utf-8")
        code, out, err = self.go("--project", "alpha", "collect", "agent/x")
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual((self.state() / build.BUILD_DIR / "p1" / "red" / "out.txt")
                         .read_text(encoding="utf-8"), "red\n")
        self.assertFalse((self.alpha / build.BUILD_DIR).exists())

    def test_agent_runs_reads_an_absolute_brief_and_its_relative_produces(self):
        brief = self.unit().resolve()
        tree = self.tmp / "agent"
        tree.mkdir()
        got = agent_runs.contract(f"{build.launch_line(brief)}\nWorktree: {tree}", str(self.alpha))
        self.assertEqual(got["brief"], str(brief))
        rel = f"{build.BUILD_DIR}/p1/u1/developer-report.md"
        self.assertEqual(got["in_brief"]["produces"], [[str(brief.parent / rel), rel]])
        self.assertEqual(agent_runs.where(got, "produces", str(brief.parent / rel))[0],
                         tree / rel)


class HygieneRefusal(Base):
    """`build start` in a member that `checkout-hygiene` keeps free of `.context-gate/build`."""

    def started(self, forbidden: str, build_root: str | None) -> tuple[int, str, str]:
        extra = ('\n[checks.checkout-hygiene]\nlevel = "error"\nroles = ["public"]\n'
                 f"forbidden = {forbidden}\n")
        self.runs = getattr(self, "runs", 0) + 1
        here = self.tmp / f"run{self.runs}"
        here.mkdir()
        w = Space(here, extra=extra)
        path = w.root / "projects.toml"
        text = path.read_text(encoding="utf-8")
        more = 'role = "public"\n' + (f'build_root = "{build_root}"\n' if build_root else "")
        path.write_text(text.replace('dir = "alpha"\n', f'dir = "alpha"\n{more}'),
                        encoding="utf-8", newline="\n")
        w.git("add", "-A")
        w.git("commit", "-qm", "config")
        w.phase_branch("alpha")
        return w.run("build", "--project", "alpha", "start", "p1")

    def test_a_forbidden_parent_of_the_build_folder_refuses_without_build_root(self):
        for entry in ('".context-gate/"', '".context-gate"', '".context-gate/build"'):
            with self.subTest(entry=entry):
                code, out, err = self.started(f"[{entry}]", None)
                self.assertEqual(code, 1, out)
                self.assertIn(f"alpha's checkout must hold none of {entry.strip(chr(34))!r} "
                              "(checkout-hygiene), so its build state cannot live in it: set "
                              "build_root on its registry entry - see docs/configuration.md",
                              err)

    def test_the_same_member_with_build_root_starts(self):
        code, out, err = self.started('[".context-gate/"]', "planning/state")
        self.assertEqual((code, err), (0, ""), out)

    def test_a_forbidden_path_that_does_not_cover_the_build_folder_starts(self):
        for entry in ('["AGENTS.md"]', '[".context-gate/build/p1"]'):
            with self.subTest(entry=entry):
                code, out, err = self.started(entry, None)
                self.assertEqual((code, err), (0, ""), out)


if __name__ == "__main__":
    unittest.main()
