"""`govern build --project`: in a workspace every build command works in one member's repository,
which is its own git repository in a folder the root's `.gitignore` lists.

    python3 -m unittest discover -s engine/tests -k test_build_workspace
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_support import *  # noqa: E402,F401,F403
from base_support import PROJECT, TEAM_CONFIG, TEAM_REGISTRY, Tree  # noqa: E402
from govern import __version__  # noqa: E402

MEMBERS = (("alpha", "A", 100), ("beta", "B", 200))


class Space(Tree):
    """A root repository with a `[registry]` config and the members `alpha` and `beta`, each its
    own repository on `main` in a git-ignored folder. `plain` adds `gamma`, a folder of the root's
    own repository: a member that is not a repository's top."""

    def __init__(self, tmp: Path, plain: bool = False, repos: bool = True, extra: str = "",
                 dirs: dict | None = None) -> None:
        """`repos` False makes every member a folder of the root's own repository; `dirs` maps a
        member's name to a folder name that is not the name itself."""
        super().__init__(tmp / "space", tmp / "home")
        self.dirs = dirs or {}
        rows = [(n, self.dirs.get(n, n), p, lo) for n, p, lo in MEMBERS] \
            + ([("gamma", "gamma", "G", 300)] if plain else [])
        self.write(layout.CONFIG, TEAM_CONFIG.format(version=__version__, extra=extra))
        self.write("projects.toml", TEAM_REGISTRY + "".join(
            PROJECT.format(name=n, dir=d, prefix=p, lo=lo, hi=lo + 99) for n, d, p, lo in rows))
        if repos:
            self.write(".gitignore", "".join(f"{self.dirs.get(n, n)}/\n" for n, _, _ in MEMBERS))
        if plain:
            self.write("gamma/readme.txt", "gamma\n")
        if not repos:
            for name, _, _ in MEMBERS:
                self.write(f"{self.dirs.get(name, name)}/readme.txt", name + "\n")
        self.init()
        self.commit("start", index=False)
        for name, _, _ in MEMBERS if repos else ():
            self.write(f"{self.member(name).name}/readme.txt", name + "\n")
            self.init(at=self.member(name))
            self.commit("start", at=self.member(name), index=False)

    def member(self, name: str) -> Path:
        return self.root / self.dirs.get(name, name)

    def phase_branch(self, name: str, branch: str = "phase/p1") -> None:
        self.git("checkout", "-qb", branch, at=self.member(name))


class Workspace(Case):
    def space(self, **kw) -> Space:
        self.w = Space(self.tmp, **kw)
        return self.w

    def go(self, *argv: str) -> tuple[int, str, str]:
        return self.w.run("build", *argv)

    def ok(self, *argv: str) -> str:
        code, out, err = self.go(*argv)
        self.assertEqual((code, err), (0, ""), out)
        return out

    def refused(self, message: str, *argv: str) -> str:
        code, out, err = self.go(*argv)
        self.assertEqual(code, 1, out + err)
        self.assertTrue(err.startswith("error: "), err)
        self.assertIn(message, err)
        self.assertNotIn("Traceback", err)
        return err

    def ledger(self, name: str, phase: str = "p1") -> dict:
        return tomllib.loads((self.w.member(name) / BUILD / phase / build.LEDGER)
                             .read_text(encoding="utf-8"))

    def started(self, *names: str) -> None:
        self.space()
        for name in names:
            self.w.phase_branch(name)
            self.ok("--project", name, "start", "p1")


class Target(Workspace):
    def test_start_with_project_writes_the_ledger_in_the_member_on_its_branch(self):
        self.space()
        self.w.phase_branch("alpha")
        out = self.ok("--project", "alpha", "start", "p1")
        data = self.ledger("alpha")
        self.assertEqual((data["project"], data["branch"], data["phase"]),
                         ("alpha", "phase/p1", "p1"))
        self.assertFalse((self.w.root / BUILD).exists())
        self.assertFalse((self.w.member("beta") / BUILD).exists())
        self.assertIn("started p1 on phase/p1, from main at", out)
        self.assertIn(f"`{GOVERN} build --project alpha unit add <id> --title <title>`", out)

    def test_project_is_accepted_after_the_subcommand_and_after_a_nested_one(self):
        self.space()
        self.w.phase_branch("alpha")
        self.ok("start", "p1", "--project", "alpha")
        self.ok("unit", "add", "u1", "--title", "One", "--project", "alpha")
        self.assertIn("u1", self.ok("status", "--project", "alpha"))
        self.assertEqual([u["id"] for u in self.ledger("alpha")["unit"]], ["u1"])

    def test_start_without_project_is_refused_and_names_both(self):
        self.space()
        self.w.phase_branch("alpha")
        self.refused("in a workspace, name the project to build in: --project <one of: "
                     "alpha, beta>", "start", "p1")
        self.assertFalse((self.w.member("alpha") / BUILD).exists())

    def test_an_unknown_project_is_refused_with_the_names(self):
        self.space()
        self.refused("no project 'nope' in projects.toml: one of alpha, beta",
                     "--project", "nope", "start", "p1")

    def test_a_member_folder_that_is_not_a_repository_top_is_refused(self):
        self.space(plain=True)
        self.refused("project 'gamma' is a folder of this repository, not a repository of its "
                     "own: build from the root without --project", "--project", "gamma", "start", "p1")

    def test_one_repository_workspace_starts_without_project_as_before(self):
        self.space(repos=False)
        self.w.git("checkout", "-qb", "phase/p1")
        out = self.ok("start", "p1")
        self.assertIn("started p1 on phase/p1, from main at", out)
        self.assertIn(f"`{GOVERN} build unit add <id> --title <title>`", out)
        self.assertNotIn("--project", out)
        data = tomllib.loads((self.w.root / BUILD / "p1" / build.LEDGER).read_text(encoding="utf-8"))
        self.assertNotIn("project", data)
        self.assertIn("Phase p1 on phase/p1", self.ok("status"))

    def test_project_naming_a_folder_of_a_one_repository_workspace_is_refused(self):
        self.space(repos=False)
        self.w.git("checkout", "-qb", "phase/p1")
        self.refused("project 'alpha' is a folder of this repository, not a repository of its "
                     "own: build from the root without --project", "--project", "alpha", "start", "p1")
        self.assertFalse((self.w.root / BUILD).exists())

    def test_a_workspace_with_a_member_repository_still_refuses_start_without_project(self):
        self.space(plain=True)
        self.refused("in a workspace, name the project to build in", "start", "p1")

    def test_the_mark_goes_to_the_governance_roots_run_log(self):
        self.space(extra=MEASURE_ON)
        self.w.phase_branch("alpha")
        out = self.ok("--project", "alpha", "start", "p1")
        marks = lambda root: [r["mark"] for r in agent_runs.read_json_lines(
            agent_runs.durable_path(self.w.home, root)) if "mark" in r]
        self.assertEqual(marks(self.w.root), ["p1"])
        self.assertEqual(marks(self.w.member("alpha")), [])
        self.assertIn(str(agent_runs.durable_path(self.w.home, self.w.root)), out)


class Find(Workspace):
    def test_status_from_the_root_finds_the_one_build_in_flight(self):
        self.started("alpha")
        self.w.phase_branch("beta", "phase/other")
        out = self.ok("status")
        self.assertIn("Phase p1 on phase/p1: stage build", out)
        self.assertIn(f"`{GOVERN} build --project alpha unit add", out)

    def test_status_json_from_the_root_finds_it_too(self):
        self.started("beta")
        data = json.loads(self.ok("status", "--json"))
        self.assertEqual((data["phase"], data["ledger"]), ("p1", f"{BUILD.as_posix()}/p1/ledger.toml"))

    def test_status_refuses_when_both_members_have_a_build(self):
        self.started("alpha", "beta")
        self.refused("builds are in flight in several repositories: alpha (phase p1), "
                     "beta (phase p1) - name one with --project", "status")
        self.assertIn("Phase p1", self.ok("--project", "beta", "status"))

    def test_a_member_on_a_branch_with_no_ledger_is_not_a_build_in_flight(self):
        self.started("alpha")
        self.w.git("checkout", "-q", "main", at=self.w.member("alpha"))
        out = self.ok("status")
        self.assertIn("No build is in flight in this workspace", out)
        self.assertIn(f"{GOVERN} build --project <one of: alpha, beta> start <phase>", out)
        self.refused("no build is in flight in this workspace", "review")

    def test_a_git_call_that_fails_for_a_member_is_an_error_not_no_build(self):
        self.started("alpha")
        beta = self.w.member("beta")
        real = phase._git
        fail = lambda repo, *a, **k: None if Path(repo) == beta else real(repo, *a, **k)
        with mock.patch.object(phase, "_git", fail):
            self.refused(f"cannot tell whether {beta} is a repository: git could not run", "status")

    def test_a_git_error_other_than_not_a_repository_is_an_error_too(self):
        self.started("alpha")
        beta = self.w.member("beta")
        real = phase._git

        def fail(repo, *a, **k):
            if Path(repo) != beta:
                return real(repo, *a, **k)
            return subprocess.CompletedProcess(a, 128, b"", b"fatal: detected dubious ownership")
        with mock.patch.object(phase, "_git", fail):
            self.refused(f"cannot tell whether {beta} is a repository: fatal: detected dubious "
                         f"ownership", "status")

    def test_status_json_with_no_build_in_flight_names_project(self):
        self.space()
        data = json.loads(self.ok("status", "--json"))
        self.assertIsNone(data["phase"])
        self.assertIn("--project <one of: alpha, beta> start <phase>", data["next"])

    def test_a_ledger_in_the_root_itself_is_found_without_a_project(self):
        self.space()
        self.w.git("checkout", "-qb", "phase/root")
        # `start` is refused at the root; a ledger already there still reads.
        (self.w.root / BUILD / "p1").mkdir(parents=True)
        base = self.w.git("rev-parse", "main").strip()
        (self.w.root / BUILD / "p1" / build.LEDGER).write_text(
            f'phase = "p1"\nbranch = "phase/root"\nbase = "{base}"\nstage = "build"\n', encoding="utf-8")
        out = self.ok("status")
        self.assertIn("Phase p1 on phase/root", out)
        self.assertNotIn("--project", out)


class Lines(Workspace):
    def test_the_command_lines_carry_project_folder_and_path(self):
        self.started("alpha")
        member = self.w.member("alpha")
        out = self.ok("unit", "add", "u1", "--title", "One")
        self.assertIn(f"then run `{GOVERN} build --project alpha launch u1`", out)
        self.ok("unit", "set", "u1", "done")
        self.w.write("alpha/src/a.py", "print(1)\n")
        self.w.commit("code", at=member, index=False)
        out = self.ok("review")
        self.assertIn(f"`{GOVERN} build --project alpha launch review`", out)
        out = self.ok("close")
        self.assertIn(f"`{GOVERN} build --project alpha launch close`", out)
        self.assertIn(f"git -C alpha rm -r -q {BUILD.as_posix()}/p1", out)
        self.assertIn(f'git -C alpha commit -m "Close phase p1: remove its build folder"', out)
        self.assertIn(f"{GOVERN} phase close --path alpha", out.splitlines()[-1])
        self.assertIn("phase close --path alpha`", self.ok("status"))

    def test_a_member_folder_with_a_space_is_quoted_in_the_command_lines(self):
        self.space(dirs={"alpha": "my alpha"})
        self.w.phase_branch("alpha")
        self.ok("--project", "alpha", "start", "p1")
        self.ok("unit", "add", "u1", "--title", "One")
        self.ok("unit", "set", "u1", "done")
        self.w.write("my alpha/src/a.py", "print(1)\n")
        self.w.commit("code", at=self.w.member("alpha"), index=False)
        self.ok("review")
        out = self.ok("close")
        self.assertIn(f"git -C 'my alpha' rm -r -q {BUILD.as_posix()}/p1", out)
        self.assertIn("git -C 'my alpha' commit -m", out)
        self.assertIn(f"{GOVERN} phase close --path 'my alpha'", out.splitlines()[-1])
        self.assertIn("phase close --path 'my alpha'`", self.ok("status"))

    def test_launch_and_fix_lines_carry_the_project_too(self):
        self.started("alpha")
        self.refused(f"run `{GOVERN} build --project alpha review` first", "fix")
        self.refused(f"run `{GOVERN} build --project alpha review` first", "close")
        self.ok("unit", "add", "u1", "--title", "One")
        self.refused(f"{Path(BUILD, 'p1', 'review', 'code-reviewer-brief.md')} is not there: run "
                     f"`{GOVERN} build --project alpha review` first",
                     "launch", "review")


class Single(Build):
    def test_start_status_and_close_print_no_project_flag(self):
        self.project()
        out = self.ok("start", "p1")
        self.assertIn(f"`{GOVERN} build unit add <id> --title <title>`", out)
        self.assertNotIn("--project", out)
        self.assertNotIn("project", self.ledger())
        self.ok("unit", "add", "u1", "--title", "One")
        self.ok("unit", "set", "u1", "done")
        self.code()
        self.ok("review")
        out = self.ok("close")
        self.assertEqual(out.splitlines()[-3:], [
            f"git rm -r -q {BUILD.as_posix()}/p1",
            'git commit -m "Close phase p1: remove its build folder"',
            f"{GOVERN} phase close"])
        for text in (out, self.ok("status")):
            for flag in ("--project", "git -C", "--path"):
                self.assertNotIn(flag, text)

    def test_project_in_a_single_repository_is_refused_and_writes_nothing(self):
        self.project()
        self.refused("--project applies only in a workspace: this project has no [registry]",
                     "--project", "alpha", "start", "p1")
        self.assertFalse(self.dir().exists())


if __name__ == "__main__":
    unittest.main()
