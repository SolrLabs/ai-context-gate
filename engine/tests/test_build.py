"""`govern build`: the ledger of the phase a branch has in flight, its briefs and its next action.
Against real repositories in temporary directories with a HOME of their own (the fixtures
`test_base` builds).

`build status --json` is a contract: its keys are held byte for byte in `expected/`. After a
deliberate change, regenerate with

    GOVERN_UPDATE_EXPECTED=1 python3 -m unittest discover -s engine/tests -k ExpectedStatus

and review the difference like any other change.

    python3 -m unittest discover -s engine/tests -k test_build
"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from govern import agent_runs, build, layout, phase, roles  # noqa: E402
from test_base import Case, Solo  # noqa: E402

EXPECTED = Path(__file__).resolve().parent / "expected"
BUILD = Path(layout.GOV_DIR) / "build"
GATES = {"briefed": ["developer", "senior-developer"], "claims": True, "deny_push": True,
         "max_refusals": 2}
RELATIVE = "Paths on the lines below are from the repository root, in your own working tree."


def at(folder: str, name: str) -> str:
    """A contract path as a brief writes it: from the repository root."""
    return f"{BUILD.as_posix()}/p1/{folder}/{name}"
GOVERN = roles.GOVERN
MEASURE_ON = '\n[checks.agent-measure]\nlevel = "warn"\n'


def then(what: str, brief: str = "the brief") -> str:
    """The last line of every command that writes a brief."""
    return f"fill in {brief}, commit it, then run `{GOVERN} build launch {what}`"


def ledger(folder: Path, *units: tuple, stage: str = "build", **more) -> build.Ledger:
    """A ledger in memory: each unit is (id, status) or (id, status, note)."""
    rows = [{"id": u[0], "title": f"Unit {u[0]}", "role": "developer", "status": u[1],
             "attempts": 0, "branch": "", "commit": "", "note": u[2] if len(u) > 2 else ""}
            for u in units]
    return build.Ledger(path=folder / "p1" / build.LEDGER, phase="p1", branch="phase/p1",
                        base="0" * 40, stage=stage, units=rows, **more)


class NextAction(unittest.TestCase):
    """The sentence `status` and the session's first line both say, from the ledger alone."""
    HERE = Path("build")

    def say(self, *units, stage="build") -> str:
        return build.next_action(ledger(self.HERE, *units, stage=stage))

    def test_a_phase_with_no_units_adds_the_first(self):
        self.assertIn(f"`{roles.GOVERN} build unit add <id> --title <title>`", self.say())

    def test_a_blocked_unit_comes_first_with_its_note(self):
        said = self.say(("u1", "pending"), ("u2", "blocked", "needs the schema settled"),
                        ("u3", "running"))
        self.assertTrue(said.startswith("Unit u2 is blocked (needs the schema settled)"), said)
        self.assertIn(f"`{roles.GOVERN} build launch u2`", said)
        self.assertNotIn("Brief:", said)
        self.assertNotIn("u1", said)

    def test_a_failed_unit_comes_first_too(self):
        said = self.say(("u1", "done"), ("u2", "failed", "tests red three times"))
        self.assertTrue(said.startswith("Unit u2 is failed (tests red three times)"), said)

    def test_a_stuck_unit_without_a_note_says_so(self):
        self.assertIn("Unit u1 is blocked (no note)", self.say(("u1", "blocked")))

    def test_the_first_pending_unit_is_named_with_its_launch_line(self):
        said = self.say(("u1", "done"), ("u2", "pending"), ("u3", "pending"))
        self.assertIn(f"Fill in the brief of unit u2, commit it, then run "
                      f"`{roles.GOVERN} build launch u2`", said)
        self.assertNotIn("Brief:", said)
        self.assertNotIn("unit set", said)
        self.assertNotIn("u3", said)
        self.assertNotIn("awaited", said)

    def test_running_units_are_listed_as_awaited_beside_a_pending_one(self):
        said = self.say(("u1", "running"), ("u2", "pending"), ("u3", "running"))
        self.assertIn("build launch u2", said)
        self.assertTrue(said.endswith("; units u1 and u3 are running and awaited."), said)

    def test_only_running_units_are_awaited(self):
        said = self.say(("u1", "done"), ("u2", "running"))
        self.assertTrue(said.startswith("Unit u2 is running and awaited"), said)
        self.assertIn(f"`{roles.GOVERN} build unit set <id> done --commit <hash>`", said)

    def test_every_unit_done_in_the_build_stage_is_reviewed(self):
        said = self.say(("u1", "done"), ("u2", "done"))
        self.assertEqual(said, f"Every unit is done: run `{roles.GOVERN} build review`.")

    def test_the_review_stage_goes_to_a_fix_or_straight_to_the_close(self):
        said = self.say(("u1", "done"), stage="review")
        self.assertIn(f"`{roles.GOVERN} build fix`", said)
        self.assertIn(f"`{roles.GOVERN} build close` if they found nothing", said)

    def test_the_fix_stage_goes_to_the_close(self):
        said = self.say(("u1", "done"), stage="fix")
        self.assertIn(f"`{roles.GOVERN} build close`", said)
        self.assertNotIn("build fix", said)

    def test_the_closing_stage_goes_to_phase_close(self):
        said = self.say(("u1", "done"), stage="closing")
        self.assertIn(f"`{roles.GOVERN} phase close`", said)

    def test_a_unit_that_is_not_done_outranks_the_stage(self):
        self.assertIn("build launch u2", self.say(("u1", "done"), ("u2", "pending"),
                                                      stage="review"))

    def test_it_reads_nothing_but_the_ledger(self):
        """No way to open a file or look one up, and no git call, is left open to it."""
        gone = AssertionError("touched the file system or git")
        with mock.patch("builtins.open", side_effect=gone), \
                mock.patch("io.open", side_effect=gone), \
                mock.patch.object(Path, "stat", side_effect=gone), \
                mock.patch.object(Path, "read_bytes", side_effect=gone), \
                mock.patch.object(Path, "read_text", side_effect=gone), \
                mock.patch.object(os, "stat", side_effect=gone), \
                mock.patch.object(os, "scandir", side_effect=gone), \
                mock.patch("subprocess.Popen", side_effect=gone), \
                mock.patch.object(phase, "_git", side_effect=gone):
            for stage in build.STAGES:
                self.say(("u1", "done"), ("u2", "pending"), stage=stage)
                self.say(("u1", "done"), stage=stage)


class LaunchLine(unittest.TestCase):
    def test_a_plain_path_is_not_quoted(self):
        path = Path("build") / "p1" / "u1" / "brief.md"
        self.assertEqual(build.launch_line(path), f"Brief: {path}")

    def test_a_path_holding_a_space_is_quoted(self):
        path = Path("my project") / "u1" / "brief.md"
        self.assertEqual(build.launch_line(path), f'Brief: "{path}"')

    def test_the_quote_is_one_the_path_does_not_hold(self):
        path = Path('my "own" project') / "brief.md"
        self.assertEqual(build.launch_line(path), f"Brief: '{path}'")

    def test_the_gate_reads_the_quoted_line_back_to_the_same_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            brief = Path(tmp) / "my project" / "brief.md"
            brief.parent.mkdir()
            brief.write_text("x\n", encoding="utf-8")
            self.assertEqual(agent_runs.brief_path(build.launch_line(brief)), brief)


class Build(Case):
    """A single repo on `phase/p1`, cut from `main`. With `remote`, `origin` is a bare
    repository holding the default branch."""

    def project(self, *, remote: bool = False, default: str = "main", branch: str = "phase/p1",
                extra: str = "", below: str | None = None) -> Solo:
        w = Solo(self.tmp, extra=extra, below=below)
        if default != "main":
            w.git("branch", "-m", "main", default)
        if remote:
            bare = self.tmp / "remote.git"
            bare.mkdir()
            w.git("init", "-q", "--bare", at=bare)
            w.git("symbolic-ref", "HEAD", f"refs/heads/{default}", at=bare)
            w.git("remote", "add", "origin", str(bare))
            w.git("push", "-q", "origin", default)
            w.git("remote", "set-head", "origin", default)
        if branch:
            w.branch(branch)
        self.w = w
        return w

    def go(self, *argv: str) -> tuple[int, str, str]:
        return self.w.run("build", *argv)

    def ok(self, *argv: str) -> str:
        code, out, err = self.go(*argv)
        self.assertEqual((code, err), (0, ""), out)
        return out

    def files(self) -> dict[str, bytes]:
        top = self.w.root / BUILD
        return {p.relative_to(top).as_posix(): p.read_bytes()
                for p in sorted(top.rglob("*")) if p.is_file()}

    def refused(self, message: str, *argv: str) -> str:
        before = self.files()
        code, out, err = self.go(*argv)
        self.assertEqual(code, 1, out + err)
        self.assertTrue(err.startswith("error: "), err)
        self.assertIn(message, err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(self.files(), before)
        return err

    def dir(self, name: str = "p1") -> Path:
        return self.w.root / BUILD / name

    def ledger(self, name: str = "p1") -> dict:
        return tomllib.loads((self.dir(name) / build.LEDGER).read_text(encoding="utf-8"))

    def head(self, ref: str = "HEAD") -> str:
        return self.w.git("rev-parse", ref).strip()

    def started(self, *flags: str, **kw) -> Solo:
        w = self.project(**kw)
        self.ok("start", "p1", *flags)
        return w

    def unit(self, uid: str = "u1", status: str | None = None, *flags: str) -> Path:
        self.ok("unit", "add", uid, "--title", f"Unit {uid}", *flags)
        if status:
            self.ok("unit", "set", uid, status)
        return self.dir() / uid / "brief.md"

    def code(self, name: str = "src/a.py", text: str = "print(1)\n") -> None:
        """A commit on the phase branch, the ledger and the briefs with it."""
        self.w.write(name, text)
        self.w.commit(name, at=self.w.top, index=False)


class Start(Build):
    def test_it_writes_the_ledger_for_the_checked_out_branch(self):
        w = self.project()
        w.write("docs/plan-login.md", "the plan\n")
        w.commit("plan", at=w.top, index=False)
        out = self.ok("start", "p1", "--plan", "docs/plan-login.md", "--high-risk")
        self.assertEqual(self.ledger(), {
            "phase": "p1", "branch": "phase/p1", "base": self.head("main"),
            "from": "main", "plan": "docs/plan-login.md", "high_risk": True, "stage": "build", "rulings": []})
        self.assertIn(f"started p1 on phase/p1, from main at {self.head('main')[:12]}", out)
        self.assertIn(f"wrote {BUILD / 'p1' / build.LEDGER}", out)
        self.assertIn("Next: Add the first unit", out)

    def test_without_a_plan_the_ledger_names_none(self):
        self.started()
        self.assertNotIn("plan", self.ledger())
        self.assertIs(self.ledger()["high_risk"], False)

    def test_the_base_is_where_the_branch_left_the_default_branch_not_its_head(self):
        self.project()
        self.code()
        self.ok("start", "p1")
        self.assertEqual(self.ledger()["base"], self.head("main"))
        self.assertNotEqual(self.ledger()["base"], self.head())

    def test_the_default_branch_with_no_remote_is_refused(self):
        for name in ("main", "master"):
            with self.subTest(branch=name), self.fresh():
                w = self.project(branch="")
                if name != "main":
                    w.git("branch", "-m", "main", name)
                self.assertIn("git checkout -b phase/p1",
                              self.refused(f"{name} is the default branch", "start", "p1"))
                self.assertFalse((w.root / BUILD).exists())

    def test_the_remotes_default_branch_is_refused(self):
        self.project(remote=True, default="development", branch="")
        self.refused("development is the default branch", "start", "p1")

    def test_main_is_an_ordinary_branch_when_the_remotes_default_is_another(self):
        self.project(remote=True, default="development", branch="main")
        self.ok("start", "p1")
        self.assertEqual(self.ledger()["branch"], "main")
        self.assertEqual(self.ledger()["base"], self.head("origin/development"))

    def test_a_local_default_branch_ahead_of_the_remote_gives_the_later_base(self):
        w = self.project(remote=True, branch="")
        w.write("docs/plan.md", "the plan\n")
        w.commit("plan, not pushed", at=w.top, index=False)
        w.branch("phase/p1")
        self.ok("start", "p1")
        self.assertEqual(self.ledger()["base"], self.head("main"))
        self.assertNotEqual(self.head("main"), self.head("origin/main"))

    def test_a_local_default_branch_behind_the_remote_gives_the_later_base(self):
        w = self.project(remote=True, branch="")
        w.write("docs/plan.md", "the plan\n")
        w.commit("plan", at=w.top, index=False)
        w.git("push", "-q", "origin", "main")
        w.branch("phase/p1")
        w.git("branch", "-f", "main", "HEAD~1")
        self.ok("start", "p1")
        self.assertEqual(self.ledger()["base"], self.head("origin/main"))

    def test_no_remote_and_no_main_or_master_is_refused(self):
        w = self.project(branch="")
        w.git("branch", "-m", "main", "trunk")
        w.branch("phase/p1")
        self.refused("cannot tell which branch is the default", "start", "p1")

    def test_a_dirty_tree_is_refused(self):
        w = self.project()
        w.write("docs/DECISIONS.md", w.read("docs/DECISIONS.md") + "\nedited\n")
        self.refused("uncommitted changes", "start", "p1")

    def test_a_detached_head_is_refused(self):
        w = self.project()
        w.git("checkout", "-q", "--detach")
        self.refused("HEAD is detached", "start", "p1")

    def test_a_second_ledger_for_the_branch_is_refused(self):
        self.started()
        err = self.refused("a build is already in flight on phase/p1: p1", "start", "p2")
        self.assertIn(str(BUILD / "p1" / build.LEDGER), err)
        self.assertFalse(self.dir("p2").exists())

    def test_a_phase_name_another_branch_holds_is_refused(self):
        w = self.started()
        w.branch("phase/other")
        self.refused("the phase p1 already has a ledger, for the branch phase/p1", "start", "p1")

    def test_a_name_that_is_not_a_slug_is_refused(self):
        self.project()
        for name in ("P1", ".p1", "p1/login", "p 1", "p_1"):
            with self.subTest(name=name):
                self.refused("is not a phase name", "start", name)

    def test_a_plan_that_is_not_there_is_refused(self):
        self.project()
        self.refused("--plan docs/nope.md: no such file", "start", "p1", "--plan",
                     "docs/nope.md")
        self.assertFalse(self.dir().exists())

    def test_outside_a_git_repository_it_is_refused_not_read_as_no_build(self):
        self.w = Solo(self.tmp, git=None)
        for argv in (("start", "p1"), ("status",), ("status", "--json"), ("review",)):
            with self.subTest(argv=argv):
                code, out, err = self.go(*argv)
                self.assertEqual((code, out), (1, ""))
                self.assertIn("error: not inside a git repository", err)

    def test_a_git_that_cannot_run_is_refused_not_read_as_no_build(self):
        self.started()
        with mock.patch.object(phase, "_git", return_value=None):
            for argv in (("status",), ("unit", "add", "u1", "--title", "T"), ("close",)):
                with self.subTest(argv=argv):
                    self.refused("git could not run", *argv)

    def test_it_works_with_agent_roles_off_and_on(self):
        for extra in ("", '\n[checks.agent-roles]\nlevel = "warn"\n'):
            with self.subTest(extra=extra), self.fresh():
                self.started(extra=extra)
                self.unit("u1")
                self.assertEqual(self.ledger()["unit"][0]["role"], "developer")


class Measure(Build):
    def marks(self) -> list:
        log = agent_runs.durable_path(self.w.home, self.w.root)
        return [row["mark"] for row in agent_runs.read_json_lines(log) if "mark" in row]

    def test_with_agent_measure_on_start_marks_the_phase_in_the_run_log(self):
        self.project(extra=MEASURE_ON)
        out = self.ok("start", "p1")
        self.assertEqual(self.marks(), ["p1"])
        self.assertIn("phase 'p1' started in", out)

    def test_with_agent_measure_off_nothing_is_marked(self):
        self.started()
        self.assertEqual(self.marks(), [])

    def test_a_refused_start_marks_nothing(self):
        self.project(extra=MEASURE_ON, branch="")
        self.refused("is the default branch", "start", "p1")
        self.assertEqual(self.marks(), [])


class Ignored(Build):
    def ignored(self, rel: str) -> bool:
        """What git itself says: `check-ignore` prints a path it ignores, and nothing otherwise."""
        return self.w.git("check-ignore", (BUILD / rel).as_posix(), check=False).strip() != ""

    def test_git_keeps_the_ledger_and_the_briefs_and_ignores_the_rest(self):
        self.started()
        for rel in ("p1/ledger.toml", "p1/u1/brief.md", "p1/fix/brief.md", "p1/close/brief.md",
                    "p1/review/code-reviewer-brief.md", "p1/review/qa-engineer-brief.md",
                    ".gitignore"):
            with self.subTest(kept=rel):
                self.assertFalse(self.ignored(rel))
        for rel in ("p1/u1/developer-report.md", "p1/review/package.diff", "p1/u1/red/out.txt",
                    "p1/review/code-reviewer-report.md", "p1/close/technical-writer-report.md",
                    "p1/review/scratch/probe.py", "p1/.ledger.toml.1.ab.tmp"):
            with self.subTest(ignored=rel):
                self.assertTrue(self.ignored(rel))

    def test_what_git_adds_is_the_plan_of_record_only(self):
        w = self.started()
        brief = self.unit("u1", "done")
        (brief.parent / "developer-report.md").write_text("done\n", encoding="utf-8")
        (brief.parent / "red").mkdir()
        (brief.parent / "red" / "out.txt").write_text("FAILED\n", encoding="utf-8")
        self.ok("review")
        w.git("add", "-A")
        staged = w.git("diff", "--cached", "--name-only").split()
        self.assertEqual(staged, [(BUILD / rel).as_posix() for rel in (
            ".gitignore", "p1/ledger.toml", "p1/review/code-reviewer-brief.md",
            "p1/u1/brief.md")])

    def test_a_root_ignore_of_build_swallows_the_ledger_and_start_refuses_and_undoes_itself(self):
        w = self.project()
        w.write(".gitignore", "build/\n")
        w.commit(".gitignore", at=w.top, index=False)
        err = self.refused("git ignores the ledger", "start", "p1")
        self.assertIn(".gitignore:1:build/", err)
        self.assertIn("!.context-gate/build/", err)
        self.assertFalse((w.root / BUILD).exists())
        self.assertEqual(w.git("status", "--porcelain").strip(), "")

    def test_the_line_start_names_is_the_one_that_makes_git_keep_the_ledger(self):
        w = self.project()
        w.write(".gitignore", "build/\n!.context-gate/build/\n")
        w.commit(".gitignore", at=w.top, index=False)
        self.ok("start", "p1")
        self.assertFalse(self.ignored("p1/ledger.toml"))

    def test_the_ignore_file_is_written_once_and_never_again(self):
        w = self.started()
        ignore = w.root / BUILD / ".gitignore"
        self.assertEqual(ignore.read_text(encoding="utf-8"), build.GITIGNORE)
        ignore.write_text("# ours now\n", encoding="utf-8")
        w.branch("phase/p2")
        self.ok("start", "p2")
        self.assertEqual(ignore.read_text(encoding="utf-8"), "# ours now\n")


class Units(Build):
    def test_add_writes_the_unit_and_its_brief_and_says_to_fill_commit_and_launch(self):
        w = self.started()
        self.code()
        out = self.ok("unit", "add", "u1", "--title", "Session token store")
        brief = self.dir() / "u1" / "brief.md"
        self.assertEqual(self.ledger()["unit"], [{
            "id": "u1", "title": "Session token store", "role": "developer",
            "status": "pending", "attempts": 0, "branch": "", "commit": "", "note": ""}])
        text = w.read((BUILD / "p1" / "u1" / "brief.md").as_posix())
        lines = text.splitlines()
        self.assertEqual(lines[0], "# p1 / u1 — Session token store")
        self.assertNotIn("Base commit", text)
        self.assertEqual([line for line in lines if line.startswith("## ")],
                         ["## Task", "## Files", "## Tests", "## Follow", "## Out of scope"])
        self.assertEqual(sum(line.startswith(agent_runs.FILL) for line in lines), 5)
        self.assertNotIn("Red:", text)
        self.assertEqual(lines[-2:], [RELATIVE, f"Produces: {at('u1', 'developer-report.md')}"])
        self.assertEqual(out.splitlines()[-1], then("u1"))
        self.assertNotIn("Brief:", out)
        self.assertIn(f"wrote {BUILD / 'p1' / 'u1' / 'brief.md'}", out)

    def test_red_adds_the_folder_for_the_failing_output_before_the_report(self):
        self.started()
        brief = self.unit("u1", None, "--red")
        lines = brief.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[-3:], [RELATIVE, f"Red: {at('u1', 'red')}",
                                   f"Produces: {at('u1', 'developer-report.md')}"])
        got = agent_runs.contract(build.launch_line(brief), str(self.w.root))
        launch = {**got, "cwd": str(self.w.root)}
        self.assertEqual(agent_runs.where(launch, "red", got["red"][0])[0],
                         self.w.root / at("u1", "red"))

    def test_the_brief_is_written_with_lf_and_utf8(self):
        self.started()
        raw = self.unit("u1").read_bytes()
        self.assertNotIn(b"\r", raw)
        self.assertIn("—".encode("utf-8"), raw)

    def test_a_role_that_writes_code_is_accepted_and_names_the_report(self):
        self.started()
        self.ok("unit", "add", "u1", "--title", "T", "--role", "senior-developer")
        self.assertEqual(self.ledger()["unit"][0]["role"], "senior-developer")
        last = (self.dir() / "u1" / "brief.md").read_text(encoding="utf-8").splitlines()[-1]
        self.assertEqual(last, f"Produces: {at('u1', 'senior-developer-report.md')}")

    def test_a_role_that_does_not_write_code_or_is_not_rendered_is_refused(self):
        self.started(extra='\n[checks.agent-roles]\nextra_roles = ["architect"]\n')
        for role in ("technical-writer", "code-reviewer", "qa-engineer", "architect", "nobody"):
            with self.subTest(role=role):
                self.refused(f"{role} is not a rendered role that writes code: a unit goes to "
                             f"developer or senior-developer",
                             "unit", "add", "u1", "--title", "T", "--role", role)

    def test_an_id_already_taken_reserved_or_not_a_slug_is_refused(self):
        self.started()
        self.unit("u1")
        self.refused("already has a unit u1", "unit", "add", "u1", "--title", "again")
        for uid in build.RESERVED:
            self.refused("the phase's own folders", "unit", "add", uid, "--title", "T")
        for uid in ("U2", "u/2", ".u", "u_2"):
            self.refused("is not a unit id", "unit", "add", uid, "--title", "T")
        self.refused("--title is empty", "unit", "add", "u2", "--title", "  ")

    def test_a_brief_no_unit_owns_is_never_overwritten(self):
        self.started()
        stray = self.dir() / "u1" / "brief.md"
        stray.parent.mkdir()
        stray.write_text("somebody's work\n", encoding="utf-8")
        self.refused("is already there and no unit owns it", "unit", "add", "u1", "--title", "T")
        self.assertEqual(stray.read_text(encoding="utf-8"), "somebody's work\n")

    def test_a_title_is_kept_as_one_line(self):
        self.started()
        self.ok("unit", "add", "u1", "--title", "Two\nlines  here")
        self.assertEqual(self.ledger()["unit"][0]["title"], "Two lines here")

    def test_set_updates_the_unit_and_only_what_was_given(self):
        self.started()
        self.unit("u1")
        self.unit("u2")
        self.assertIn("unit u1 is done", self.ok(
            "unit", "set", "u1", "done", "--branch", "unit/u1", "--commit", "abc1234"))
        self.ok("unit", "set", "u1", "done", "--note", "merged")
        one, two = self.ledger()["unit"]
        self.assertEqual((one["status"], one["branch"], one["commit"], one["note"]),
                         ("done", "unit/u1", "abc1234", "merged"))
        self.assertEqual((two["status"], two["attempts"]), ("pending", 0))

    def test_running_adds_one_to_the_attempts_each_time(self):
        self.started()
        self.unit("u1")
        self.assertIn("unit u1 is running (attempt 1)", self.ok("unit", "set", "u1", "running"))
        self.ok("unit", "set", "u1", "failed", "--note", "tests red")
        self.assertEqual(self.ledger()["unit"][0]["attempts"], 1)
        self.assertIn("(attempt 2)", self.ok("unit", "set", "u1", "running"))
        self.assertEqual(self.ledger()["unit"][0]["attempts"], 2)

    def test_set_with_a_role_changes_the_unit_and_names_a_report_of_its_own(self):
        self.started()
        brief = self.unit("u1")
        before = brief.read_bytes()
        self.ok("unit", "set", "u1", "blocked", "--role", "senior-developer")
        self.assertEqual(self.ledger()["unit"][0]["role"], "senior-developer")
        self.assertEqual(brief.read_bytes(),
                         before.replace(b"/u1/developer-report.md",
                                        b"/u1/senior-developer-report.md"))
        self.assertIn("senior-developer", self.ok("status"))
        self.assertIn(f"launch a fresh senior-developer with `{GOVERN} build launch u1`",
                      self.ok("status"))

    def test_a_role_that_writes_no_code_is_refused_by_set_and_changes_nothing(self):
        self.started()
        self.unit("u1")
        self.refused("is not a rendered role that writes code",
                     "unit", "set", "u1", "running", "--role", "code-reviewer")

    def test_running_leaves_the_brief_as_committed(self):
        self.started()
        brief = self.unit("u1")
        before = brief.read_bytes()
        self.code()
        self.ok("unit", "set", "u1", "running")
        self.assertEqual(brief.read_bytes(), before)

    def test_an_unknown_unit_or_status_is_refused(self):
        self.started()
        self.refused("has no unit u1 (it has none yet)", "unit", "set", "u1", "done")
        self.unit("u1")
        self.refused("has no unit u9 (u1)", "unit", "set", "u9", "done")
        self.refused("'finished' is not a status: pending, running, done, blocked, failed",
                     "unit", "set", "u1", "finished")

    def test_a_ruling_is_appended_as_one_line_and_status_and_close_print_them_all(self):
        self.started()
        self.unit("u1", "done")
        self.assertIn("ruling 1 recorded", self.ok(
            "ruling", "Chose a table over a file:\nfewer moving parts;", "a rewrite if wrong"))
        self.ok("ruling", "Kept the old flag; removing it breaks callers")
        both = ["Chose a table over a file: fewer moving parts; a rewrite if wrong",
                "Kept the old flag; removing it breaks callers"]
        self.assertEqual(self.ledger()["rulings"], both)
        self.ok("review")
        for argv in (("status",), ("close",)):
            out = self.ok(*argv)
            self.assertIn(f"Rulings:\n  1. {both[0]}\n  2. {both[1]}\n", out)
        self.refused("a ruling is one line", "ruling", "  ")


class Collect(Build):
    """What a developer's worktree holds that git ignores, brought to the checkout the
    coordinator runs in: `unit set done --branch`, and `build collect` for any other agent."""
    UNIT = BUILD / "p1" / "u1"

    def tree(self, branch: str = "unit/u1") -> Path:
        """A worktree of its own on `branch`, cut from the phase branch's head."""
        self.started()
        self.unit("u1")
        self.w.commit("brief", at=self.w.top, index=False)
        where = self.tmp / "wt"
        self.w.git("worktree", "add", "-q", "-b", branch, str(where))
        return where

    def write(self, where: Path, rel: Path | str, text: str) -> Path:
        path = where / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def untracked(self, where: Path) -> list[str]:
        return [p for p in self.w.git("ls-files", "--others", "--exclude-standard", "-z",
                                      at=where).split("\0") if p]

    def test_done_with_a_branch_copies_the_units_files_but_not_its_brief(self):
        where = self.tree()
        self.write(where, self.UNIT / "developer-report.md", "the report\n")
        self.write(where, self.UNIT / "red" / "out.txt", "FAILED\n")
        self.write(where, self.UNIT / "brief.md", "the developer's edit of the brief\n")
        before = self.untracked(where)
        brief = (self.dir() / "u1" / "brief.md").read_bytes()
        out = self.ok("unit", "set", "u1", "done", "--branch", "unit/u1")
        here = self.w.root / self.UNIT
        self.assertEqual((here / "developer-report.md").read_text(encoding="utf-8"), "the report\n")
        self.assertEqual((here / "red" / "out.txt").read_text(encoding="utf-8"), "FAILED\n")
        self.assertEqual((here / "brief.md").read_bytes(), brief)
        self.assertIn(f"copied {(self.UNIT / 'developer-report.md').as_posix()}", out)
        self.assertIn(f"copied {(self.UNIT / 'red' / 'out.txt').as_posix()}", out)
        self.assertEqual(sum(line.startswith("copied ") for line in out.splitlines()), 2)
        self.assertEqual(self.untracked(where), before)           # read, never deleted or moved
        self.assertTrue((where / self.UNIT / "developer-report.md").is_file())
        ignored = self.w.git("ls-files", "--others", "--ignored", "--exclude-standard", "-z",
                             self.UNIT.as_posix()).split("\0")
        self.assertIn((self.UNIT / "developer-report.md").as_posix(), ignored)
        self.assertEqual(self.untracked(self.w.top), [])

    def test_an_older_copy_is_overwritten(self):
        where = self.tree()
        self.write(self.w.root, self.UNIT / "developer-report.md", "older\n")
        self.write(where, self.UNIT / "developer-report.md", "newer\n")
        self.ok("unit", "set", "u1", "done", "--branch", "unit/u1")
        self.assertEqual((self.w.root / self.UNIT / "developer-report.md").read_text(
            encoding="utf-8"), "newer\n")

    def test_a_branch_with_no_worktree_is_said_in_one_line_and_the_unit_is_still_set(self):
        self.tree()
        out = self.ok("unit", "set", "u1", "done", "--branch", "unit/elsewhere")
        self.assertIn("no worktree has unit/elsewhere checked out; nothing copied", out)
        self.assertEqual(self.ledger()["unit"][0]["status"], "done")

    def test_a_worktree_with_nothing_to_copy_is_said_in_one_line(self):
        self.tree()
        out = self.ok("unit", "set", "u1", "done", "--branch", "unit/u1")
        self.assertIn("nothing to copy from the worktree of unit/u1", out)
        self.assertNotIn("copied", out)

    def test_collect_copies_what_git_ignores_under_the_phase_and_nothing_tracked(self):
        where = self.tree()
        self.write(where, BUILD / "p1" / "close" / "technical-writer-report.md", "notes\n")
        self.write(where, BUILD / "p1" / "fix" / "developer-report.md", "fixed\n")
        self.write(where, BUILD / "p1" / "u1" / "brief.md", "edited\n")      # tracked: not copied
        self.write(where, "src/scratch.txt", "outside the build folder\n")
        out = self.ok("collect", "unit/u1")
        top = self.w.root / BUILD / "p1"
        self.assertEqual((top / "close" / "technical-writer-report.md").read_text(
            encoding="utf-8"), "notes\n")
        self.assertEqual((top / "fix" / "developer-report.md").read_text(encoding="utf-8"),
                         "fixed\n")
        self.assertNotEqual((top / "u1" / "brief.md").read_text(encoding="utf-8"), "edited\n")
        self.assertFalse((self.w.root / "src" / "scratch.txt").exists())
        self.assertEqual(sum(line.startswith("copied ") for line in out.splitlines()), 2)

    def test_collect_for_a_branch_with_no_worktree_says_so(self):
        self.tree()
        out = self.ok("collect", "unit/elsewhere")
        self.assertIn("no worktree has unit/elsewhere checked out; nothing copied", out)


class NoBuild(Build):
    def test_every_command_but_start_says_so_and_exits_1(self):
        self.project()
        for argv in (("unit", "add", "u1", "--title", "T"), ("unit", "set", "u1", "done"),
                     ("ruling", "x"), ("review",), ("fix",), ("close",)):
            with self.subTest(argv=argv):
                code, out, err = self.go(*argv)
                self.assertEqual((code, out), (1, ""))
                self.assertIn("error: no build is in flight on phase/p1", err)
        self.assertFalse((self.w.root / BUILD).exists())

    def test_status_says_so_and_exits_0(self):
        self.project()
        self.assertEqual(self.ok("status"), (
            "No build is in flight on phase/p1: start one from a phase branch with "
            f"`{roles.GOVERN} build start <phase>`.\n"))

    def test_a_detached_head_has_no_build(self):
        w = self.started()
        w.git("checkout", "-q", "--detach")
        self.assertIn("No build is in flight on a detached HEAD", self.ok("status"))
        self.refused("no build is in flight on a detached HEAD", "fix")


class TwoBranches(Build):
    def both(self) -> Solo:
        """`p1` for `phase/p1` and `p2` for `phase/p2`, side by side in one tree."""
        w = self.started()
        self.unit("u1")
        w.branch("phase/p2")
        self.ok("start", "p2", "--high-risk")
        return w

    def test_two_ledgers_coexist_and_a_command_acts_on_the_checked_out_branchs(self):
        w = self.both()
        one = (self.dir("p1") / build.LEDGER).read_bytes()
        self.ok("unit", "add", "a1", "--title", "On p2")
        self.ok("ruling", "decided on p2")
        self.assertIn("Phase p2 on phase/p2", self.ok("status"))
        self.assertEqual((self.dir("p1") / build.LEDGER).read_bytes(), one)
        self.assertEqual([u["id"] for u in self.ledger("p2")["unit"]], ["a1"])
        w.switch("phase/p1")
        two = (self.dir("p2") / build.LEDGER).read_bytes()
        self.assertIn("Phase p1 on phase/p1", self.ok("status"))
        self.ok("unit", "set", "u1", "running")
        self.assertEqual((self.dir("p2") / build.LEDGER).read_bytes(), two)
        self.assertEqual(self.ledger("p1")["unit"][0]["status"], "running")

    def test_a_branch_with_no_ledger_of_its_own_has_no_build(self):
        w = self.both()
        w.branch("unit/u1")
        self.assertIn("No build is in flight on unit/u1", self.ok("status"))

    def test_two_ledgers_naming_one_branch_are_refused_by_name(self):
        self.both()
        path = self.dir("p1") / build.LEDGER
        path.write_text(path.read_text(encoding="utf-8").replace("phase/p1", "phase/p2"),
                        encoding="utf-8")
        err = self.refused("2 ledgers name the branch phase/p2", "status")
        self.assertIn(str(path), err)
        self.assertIn(str(self.dir("p2") / build.LEDGER), err)

    def test_another_branchs_broken_ledger_does_not_stop_this_one(self):
        self.both()
        (self.dir("p1") / build.LEDGER).write_text("not [ toml\n", encoding="utf-8")
        self.assertIn("Phase p2 on phase/p2", self.ok("status"))


class LedgerFile(Build):
    def broken(self, content: bytes) -> Path:
        self.started()
        self.unit("u1")
        path = self.dir() / build.LEDGER
        path.write_bytes(content)
        return path

    def test_what_is_written_reads_back_as_the_same_ledger(self):
        made = ledger(self.tmp, ("u1", "done"), ("u2", "blocked", 'a "quoted" note'),
                      stage="fix", plan="docs/plan.md", high_risk=True,
                      rulings=["one; because; cost", "two"])
        build.save(made)
        text = made.path.read_text(encoding="utf-8")
        self.assertEqual(text.count("[[unit]]"), 2)
        self.assertEqual(build.load(made.path), made)

    def test_a_ledger_that_is_not_toml_is_an_error_naming_the_file(self):
        path = self.broken(b"phase = \n")
        for argv in (("status",), ("status", "--json"), ("unit", "set", "u1", "done"),
                     ("start", "p9")):
            with self.subTest(argv=argv):
                err = self.refused("not valid TOML", *argv)
                self.assertIn(str(path), err)
                self.assertNotIn("No build is in flight", err)

    def test_a_ledger_of_the_wrong_shape_is_an_error_naming_the_file(self):
        good = build.render(ledger(self.tmp, ("u1", "pending")))
        for change, why in ((('stage = "build"', 'stage = "shipping"'), "`stage` is 'shipping'"),
                            (('status = "pending"', 'status = "paused"'), "`status` 'paused'"),
                            (("attempts = 0", 'attempts = "many"'), "`attempts`"),
                            (('branch = "phase/p1"\n', ""), "`branch` is missing"),
                            (('phase = "p1"', 'phase = "p2"'), "in a folder named 'p1'"),
                            (("rulings = []", "rulings = [1]"), "`rulings`")):
            with self.subTest(why=why), self.fresh():
                self.assertIn(change[0], good)
                path = self.broken(good.replace(*change, 1).encode("utf-8"))
                err = self.refused("not a build ledger: ", "status")
                self.assertIn(why, err)
                self.assertIn(str(path), err)

    def test_a_ledger_that_is_not_utf8_is_an_error_naming_the_file(self):
        path = self.broken(b'phase = "p\xff1"\n')
        self.assertIn(str(path), self.refused("not valid UTF-8", "status"))

    def test_crlf_stays_crlf_and_a_byte_order_mark_survives_a_rewrite(self):
        self.started()
        self.unit("u1")
        path = self.dir() / build.LEDGER
        lf = path.read_bytes()
        path.write_bytes(b"\xef\xbb\xbf" + lf.replace(b"\n", b"\r\n"))
        self.ok("unit", "set", "u1", "running")
        raw = path.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(raw.count(b"\r\n"), raw.count(b"\n"))
        self.assertIn(b'status = "running"', raw)
        self.assertIn("Phase p1 on phase/p1", self.ok("status"))

    def test_an_lf_ledger_stays_lf_and_gains_no_byte_order_mark(self):
        self.started()
        self.unit("u1", "running")
        raw = (self.dir() / build.LEDGER).read_bytes()
        self.assertNotIn(b"\r", raw)
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))

    def test_a_second_run_of_the_same_change_writes_the_same_bytes(self):
        self.started()
        self.unit("u1")
        self.ok("unit", "set", "u1", "done", "--commit", "abc1234")
        before = self.files()
        self.ok("unit", "set", "u1", "done", "--commit", "abc1234")
        self.assertEqual(self.files(), before)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root writes to read-only files")
    def test_a_read_only_ledger_is_still_rewritten(self):
        """Windows refuses a replace onto a read-only file; POSIX does not, so the refusal is
        stood in for here, and only making the file writable first gets past it."""
        self.started()
        self.unit("u1")
        path = self.dir() / build.LEDGER
        os.chmod(path, stat.S_IREAD)
        self.addCleanup(lambda: path.exists() and os.chmod(path, stat.S_IWRITE | stat.S_IREAD))
        real = os.replace

        def replace(src, dst):
            if not os.stat(dst).st_mode & stat.S_IWRITE:
                raise PermissionError(13, "denied", str(dst))
            real(src, dst)
        with mock.patch.object(os, "replace", replace):
            self.ok("unit", "set", "u1", "running")
        self.assertEqual(self.ledger()["unit"][0]["status"], "running")

    def test_several_files_are_written_all_or_none(self):
        kept, new, blocked = self.tmp / "kept.txt", self.tmp / "sub" / "new.txt", self.tmp / "d"
        kept.write_bytes(b"old")
        blocked.mkdir()
        (blocked / "inside").write_bytes(b"x")       # a folder cannot be replaced by a file
        with mock.patch.object(build, "REPLACE_TRIES", 1), self.assertRaises(OSError):
            build.write_all([(kept, b"changed"), (new, b"made"), (blocked, b"never")])
        self.assertEqual(kept.read_bytes(), b"old")
        self.assertFalse(new.exists())
        self.assertEqual([p.name for p in self.tmp.rglob("*.tmp")], [])

    def test_a_write_that_fails_leaves_the_ledger_and_writes_no_brief(self):
        self.started()
        self.unit("u1")
        with mock.patch.object(build, "_replace", side_effect=PermissionError(13, "denied")):
            err = self.refused("nothing was written", "unit", "add", "u2", "--title", "T")
        self.assertIn("denied", err)
        self.assertFalse((self.dir() / "u2" / "brief.md").exists())
        self.assertEqual([u["id"] for u in self.ledger()["unit"]], ["u1"])


class Status(Build):
    def test_it_prints_the_phase_a_line_per_unit_and_the_next_action(self):
        w = self.project()
        w.write("docs/plan.md", "plan\n")
        w.commit("plan", at=w.top, index=False)
        self.ok("start", "p1", "--plan", "docs/plan.md", "--high-risk")
        self.unit("u1", "done")
        self.ok("unit", "add", "u22", "--title", "Refresh", "--role", "senior-developer")
        self.ok("unit", "set", "u22", "blocked", "--note", "needs the schema")
        out = self.ok("status").splitlines()
        self.assertEqual(out[0], f"Phase p1 on phase/p1: stage build, base "
                                 f"{self.head('main')[:12]}, plan docs/plan.md, high-risk")
        self.assertEqual(out[1], "  u1   done     developer         Unit u1")
        self.assertEqual(out[2], "  u22  blocked  senior-developer  Refresh  "
                                 "(note: needs the schema)")
        self.assertEqual(len(out), 4)
        self.assertTrue(out[3].startswith("Next: Unit u22 is blocked (needs the schema)"))

    def test_the_next_line_is_what_next_action_says(self):
        self.started()
        self.unit("u1")
        said = build.next_action(build.load(self.dir() / build.LEDGER))
        self.assertEqual(self.ok("status").splitlines()[-1], f"Next: {said}")
        self.assertEqual(json.loads(self.ok("status", "--json"))["next"], said)
        self.assertIn(f"`{roles.GOVERN} build launch u1`", said)

    def test_a_note_the_console_cannot_encode_is_escaped_not_a_traceback(self):
        self.started()
        self.unit("u1")
        self.ok("unit", "set", "u1", "blocked", "--note", "waiting on 日本")
        ascii_out = mock.Mock(encoding="ascii")
        with mock.patch.object(build.sys, "stdout", ascii_out):
            build._say(build.status_lines(build.load(self.dir() / build.LEDGER)))
        written = ascii_out.write.call_args[0][0]
        self.assertIn("waiting on \\u65e5\\u672c", written)
        written.encode("ascii")


class ExpectedStatus(Build):
    """The keys of `build status --json`, byte for byte (the files are stored with LF endings:
    `expected/.gitattributes`). The base commit is the fixture's own and is written `<base>`."""

    def _compare(self, name: str) -> None:
        out = self.ok("status", "--json")
        if json.loads(out)["base"]:
            self.assertEqual(json.loads(out)["base"], self.head("main"))
            out = out.replace(self.head("main"), "<base>")
        got, path = out.encode("utf-8"), EXPECTED / name
        if os.environ.get("GOVERN_UPDATE_EXPECTED"):
            path.write_bytes(got)
        self.assertEqual(got.decode("utf-8"), path.read_bytes().decode("utf-8"))
        self.assertEqual(got, path.read_bytes())

    def test_a_build_in_flight(self):
        w = self.project()
        w.write("docs/plan-login.md", "the plan\n")
        w.commit("plan", at=w.top, index=False)
        self.ok("start", "p1-login", "--plan", "docs/plan-login.md", "--high-risk")
        self.ok("unit", "add", "u1", "--title", "Session token store")
        self.ok("unit", "set", "u1", "running")
        self.ok("unit", "set", "u1", "done", "--branch", "unit/u1", "--commit", "3f2a9c1",
                "--note", "merged")
        self.ok("unit", "add", "u2", "--title", "Token refresh", "--role", "senior-developer")
        self.ok("unit", "set", "u2", "running")
        self.ok("ruling", "Tokens live in a table, not a file: one writer; a migration if wrong")
        self._compare("build-status.json")

    def test_no_build_in_flight(self):
        self.project()
        self._compare("build-status-none.json")

    def test_both_shapes_hold_the_same_keys(self):
        some = json.loads((EXPECTED / "build-status.json").read_text(encoding="utf-8"))
        none = json.loads((EXPECTED / "build-status-none.json").read_text(encoding="utf-8"))
        keys = ["phase", "branch", "base", "plan", "high_risk", "stage", "ledger", "rulings",
                "units", "done", "total", "next"]
        self.assertEqual((list(some), list(none)), (keys, keys))
        self.assertEqual(list(some["units"][0]), ["id", "title", "role", "status", "attempts",
                                                  "branch", "commit", "note", "brief", "report"])
        self.assertEqual((none["phase"], none["units"]), (None, []))
        self.assertEqual(some["units"][0]["brief"], ".context-gate/build/p1-login/u1/brief.md")


class Review(Build):
    def ready(self, *flags: str, **kw) -> Solo:
        """Two units done, and a change on the phase branch committed with the ledger."""
        w = self.started(*flags, **kw)
        self.unit("u1", "done")
        self.ok("unit", "add", "u2", "--title", "Unit u2", "--role", "senior-developer")
        self.ok("unit", "set", "u2", "done")
        self.code("src/a.py", "".join(f"line {n}\n" for n in range(1, 41)))
        return w

    def package(self) -> str:
        return (self.dir() / "review" / "package.diff").read_text(encoding="utf-8")

    def test_it_is_refused_while_a_unit_is_not_done(self):
        self.started()
        self.refused("has no units", "review")
        self.unit("u1", "done")
        self.unit("u2", "running")
        self.unit("u3")
        self.refused("not every unit is done: u2 is running, u3 is pending", "review")
        self.assertEqual(self.ledger()["stage"], "build")
        self.assertFalse((self.dir() / "review").exists())

    def test_the_package_holds_the_commit_list_the_stat_and_the_diff_from_the_base(self):
        w = self.ready()
        w.write("src/a.py", w.read("src/a.py").replace("line 20\n", "line twenty\n"))
        w.commit("the twentieth line", at=w.top, index=False)
        self.ok("review")
        text = self.package()
        self.assertEqual(text.splitlines()[0], f"# Review package: p1 on phase/p1, "
                                               f"{self.head('main')[:12]}..{self.head()[:12]}")
        commits = text.split("## Commits\n\n")[1].split("\n\n## Stat")[0].splitlines()
        self.assertEqual([c.split(" ", 1)[1] for c in commits],
                         ["the twentieth line", "src/a.py"])
        self.assertIn(" src/a.py | 40 +", text.split("## Stat\n\n")[1].split("## Diff")[0])
        self.assertIn("+line twenty\n", text.split("## Diff\n\n")[1])
        self.assertTrue(text.endswith("\n"))

    def test_the_default_branch_merged_into_the_phase_is_not_the_phases_work(self):
        w = self.ready()
        w.switch("main")
        w.write("src/main_only.py", "moved on\n")
        w.commit("main moved on", at=w.top, index=False)
        w.switch("phase/p1")
        w.git("merge", "-q", "--no-edit", "main")
        self.ok("review")
        text = self.package()
        self.assertIn("src/a.py", text)
        self.assertNotIn("main_only", text)
        self.assertNotIn("main moved on", text)
        self.assertIn(f"{self.head('main')[:12]}..{self.head()[:12]}", text.splitlines()[0])

    def test_a_phase_cut_from_another_phase_measures_from_it_when_started_with_from(self):
        w = self.ready()
        self.assertEqual(self.ledger()["from"], "main")
        w.branch("phase/p2")
        self.ok("start", "p2", "--from", "phase/p1")
        self.assertEqual(self.ledger("p2")["from"], "phase/p1")
        self.assertEqual(self.ledger("p2")["base"], self.head("phase/p1"))
        self.ok("unit", "add", "u1", "--title", "T")
        self.ok("unit", "set", "u1", "done")
        self.code("src/b.py", "phase two\n")
        self.ok("review")
        text = (self.dir("p2") / "review" / "package.diff").read_text(encoding="utf-8")
        self.assertIn("src/b.py", text)
        self.assertNotIn("src/a.py", text)

    def test_a_from_that_is_not_a_branch_here_is_refused_at_start(self):
        self.project()
        self.refused("nope is not a branch here", "start", "p1", "--from", "nope")
        self.assertFalse(self.dir().exists())

    def test_the_diff_carries_ten_lines_of_context(self):
        w = self.ready()
        w.git("checkout", "-q", "main")
        w.git("merge", "-q", "--ff-only", "phase/p1")
        w.branch("phase/next")
        self.ok("start", "p2")
        self.ok("unit", "add", "u1", "--title", "T")
        self.ok("unit", "set", "u1", "done")
        w.write("src/a.py", w.read("src/a.py").replace("line 20\n", "line twenty\n"))
        w.commit("the twentieth line", at=w.top, index=False)
        self.ok("review")
        text = (self.dir("p2") / "review" / "package.diff").read_text(encoding="utf-8")
        self.assertIn("@@ -10,21 +10,21 @@", text)

    def test_the_package_never_holds_the_build_folder_itself(self):
        w = self.ready()
        tracked = w.git("ls-files", (BUILD / "p1").as_posix()).split()
        self.assertIn((BUILD / "p1" / build.LEDGER).as_posix(), tracked)
        self.ok("ruling", "a ledger change, committed with the next commit")
        w.commit("the ledger alone", at=w.top, index=False)
        self.ok("review")
        text = self.package()
        self.assertNotIn(BUILD.as_posix(), text)
        self.assertNotIn("ledger.toml", text)
        self.assertNotIn("the ledger alone", text)
        self.assertIn("src/a.py", text)

    def test_the_package_covers_the_repository_when_the_project_sits_below_its_top(self):
        w = self.ready(below="repo")
        (w.top / "outside.txt").write_text("outside the project's folder\n", encoding="utf-8")
        w.commit("outside", at=w.top, index=False)
        self.ok("review")
        text = self.package()
        self.assertIn("+outside the project's folder", text)
        self.assertIn("gov/src/a.py", text)
        self.assertNotIn("ledger.toml", text)

    def test_the_package_is_three_plain_git_calls_each_leaving_the_build_folder_out(self):
        self.ready()
        seen, real = [], phase._run

        def spy(repo, *args, **kw):
            seen.append(args)
            return real(repo, *args, **kw)
        with mock.patch.object(phase, "_run", spy):
            build.package(self.w.root, build.load(self.dir() / build.LEDGER))
        span = f"{self.head('main')}..HEAD"
        made = [a for a in seen if a[0] in ("log", "diff")]
        self.assertEqual([a[:a.index("--")] for a in made], [
            ("log", "--oneline", "--no-decorate", "--no-color", span),
            ("diff", "--stat", "--no-color", "--no-ext-diff", span),
            ("diff", "-U10", "--no-color", "--no-ext-diff", span)])
        for args in made:
            self.assertEqual(args[args.index("--") + 1:],
                             (":(top)", f":(top,exclude,literal){BUILD.as_posix()}"))

    def test_the_code_reviewers_brief_names_the_plan_each_unit_and_the_package(self):
        w = self.project()
        w.write("docs/plan.md", "plan\n")
        w.commit("plan", at=w.top, index=False)
        self.ok("start", "p1", "--plan", "docs/plan.md")
        self.unit("u1", "done")
        self.ok("unit", "add", "u2", "--title", "Unit u2", "--role", "senior-developer")
        self.ok("unit", "set", "u2", "done")
        out = self.ok("review")
        d, brief = self.dir(), self.dir() / "review" / "code-reviewer-brief.md"
        text = brief.read_text(encoding="utf-8")
        self.assertIn("Review the phase `p1` as one diff;", text)
        self.assertIn("- Plan: ../../../../docs/plan.md\n", text)
        self.assertIn("- Unit u1, Unit u1: brief ../u1/brief.md, report "
                      "../u1/developer-report.md\n", text)
        self.assertIn("- Unit u2, Unit u2: brief ../u2/brief.md, report "
                      "../u2/senior-developer-report.md\n", text)
        self.assertIn("full diff from the phase's base): package.diff\n", text)
        self.assertNotIn(agent_runs.FILL, text)
        self.assertEqual(text.splitlines()[-2:],
                         [RELATIVE, f"Produces: {at('review', 'code-reviewer-report.md')}"])
        self.assertEqual(out.splitlines()[-1], then("review", "the briefs that need it"))
        self.assertNotIn("Brief:", out)
        self.assertFalse((d / "review" / "qa-engineer-brief.md").exists())
        self.assertEqual(self.ledger()["stage"], "review")

    def test_the_brief_repeats_nothing_the_role_definition_says(self):
        self.ready()
        self.ok("review")
        text = (self.dir() / "review" / "code-reviewer-brief.md").read_text(encoding="utf-8")
        for said_by_the_role in ("Critical", "Findings:", "Status:", "heredoc", "severity"):
            self.assertNotIn(said_by_the_role, text)
        self.assertLessEqual(len(text.splitlines()), 11)

    def test_without_a_plan_the_brief_names_none(self):
        self.ready()
        self.ok("review")
        text = (self.dir() / "review" / "code-reviewer-brief.md").read_text(encoding="utf-8")
        self.assertNotIn("Plan:", text)

    def test_a_high_risk_phase_also_gets_a_qa_brief_with_a_definition_of_done_to_fill(self):
        self.ready("--high-risk")
        out = self.ok("review")
        d = self.dir() / "review"
        text = (d / "qa-engineer-brief.md").read_text(encoding="utf-8")
        self.assertEqual(agent_runs.unfilled(text), ["Definition of done"])
        for rel in ("../u1/brief.md", "../u1/developer-report.md",
                    "../u2/senior-developer-report.md", "package.diff"):
            self.assertIn(rel, text)
        self.assertIn("Scratch files go only in scratch.\n", text)
        self.assertEqual(text.splitlines()[-2:], [RELATIVE, f"Produces: {at('review', 'qa-engineer-report.md')}"])
        lines = out.splitlines()
        self.assertEqual(lines[-1], then("review", "the briefs that need it"))
        self.assertIn(f"fill in the definition of done in "
                      f"{BUILD / 'p1' / 'review' / 'qa-engineer-brief.md'}", lines)
        self.assertNotIn("Brief:", out)

    def test_a_filled_qa_brief_is_kept_on_a_second_review(self):
        self.ready("--high-risk")
        self.ok("review")
        qa = self.dir() / "review" / "qa-engineer-brief.md"
        qa.write_text("the coordinator's checklist\n", encoding="utf-8")
        out = self.ok("review")
        self.assertEqual(qa.read_text(encoding="utf-8"), "the coordinator's checklist\n")
        self.assertIn(f"kept {BUILD / 'p1' / 'review' / 'qa-engineer-brief.md'} as it is", out)

    def test_a_second_review_changes_nothing(self):
        self.ready("--high-risk")
        self.ok("review")
        before = self.files()
        self.ok("review")
        self.assertEqual(self.files(), before)

    def test_a_source_branch_that_is_gone_writes_nothing_and_leaves_the_stage(self):
        self.ready()
        path = self.dir() / build.LEDGER
        path.write_text(path.read_text(encoding="utf-8").replace('from = "main"',
                                                                 'from = "gone"'),
                        encoding="utf-8")
        self.refused("gone is not a branch here", "review")
        self.assertEqual(self.ledger()["stage"], "build")


class FixAndClose(Build):
    def reviewed(self, *flags: str) -> Solo:
        w = self.started(*flags)
        self.unit("u1", "done")
        self.code()
        self.ok("review")
        return w

    def test_every_brief_says_the_report_is_part_of_the_task(self):
        self.reviewed("--high-risk")
        self.ok("fix")
        self.ok("close")
        d = self.dir()
        for brief in (d / "u1" / "brief.md", d / "review" / "code-reviewer-brief.md",
                      d / "review" / "qa-engineer-brief.md", d / "fix" / "brief.md",
                      d / "close" / "brief.md"):
            with self.subTest(brief=brief.name):
                lines = brief.read_text(encoding="utf-8").splitlines()
                self.assertEqual(lines.count(build.REPORT_NOTE), 1)
                self.assertEqual(build.REPORT_NOTE, "Write the report below before your final "
                                 "message, and answer your role's hand-back checklist in it. "
                                 "Every line of it is part of this task.")
                at_ = lines.index(build.REPORT_NOTE)
                self.assertEqual(lines[at_ + 1], build.PATHS_NOTE)
                self.assertTrue(lines[-1].startswith("Produces: "))

    def test_fix_writes_a_list_for_the_coordinator_to_fill_and_sets_the_stage(self):
        self.reviewed()
        out = self.ok("fix")
        brief = self.dir() / "fix" / "brief.md"
        text = brief.read_text(encoding="utf-8")
        self.assertEqual(agent_runs.unfilled(text), ["Fixes", "Out of scope"])
        self.assertIn("ranked, five at most", text)
        self.assertIn("the test to write first and see fail", text)
        self.assertIn(f"Base commit: {self.head()}", text.splitlines())
        self.assertEqual(text.splitlines()[-2:],
                         [RELATIVE, f"Produces: {at('fix', 'developer-report.md')}"])
        self.assertEqual(out.splitlines()[-1], then("fix", "its ranked list"))
        self.assertNotIn("Brief:", out)
        self.assertEqual(self.ledger()["stage"], "fix")
        self.assertIn("build close", self.ok("status").splitlines()[-1])

    def test_a_fix_brief_still_to_fill_is_kept_on_a_second_run(self):
        self.reviewed()
        self.ok("fix")
        brief = self.dir() / "fix" / "brief.md"
        before = self.files()
        out = self.ok("fix")
        self.assertEqual(self.files(), before)
        self.assertIn(f"kept {BUILD / 'p1' / 'fix' / 'brief.md'} as it is", out)
        self.assertEqual(out.splitlines()[-1], then("fix", "its ranked list"))

    def test_a_filled_fix_brief_is_kept_and_the_next_pass_gets_a_brief_of_its_own(self):
        self.reviewed()
        self.ok("fix")
        folder = self.dir() / "fix"
        first = folder / "brief.md"
        first.write_text("1. the ranked list\n", encoding="utf-8")
        out = self.ok("fix")
        second = folder / "brief-2.md"
        self.assertEqual(first.read_text(encoding="utf-8"), "1. the ranked list\n")
        text = second.read_text(encoding="utf-8")
        self.assertEqual(agent_runs.unfilled(text), ["Fixes", "Out of scope"])
        self.assertEqual(text.splitlines()[-2:], [RELATIVE, f"Produces: {at('fix', 'developer-report-2.md')}"])
        self.assertIn(f"wrote {BUILD / 'p1' / 'fix' / 'brief-2.md'}", out)
        self.assertEqual(out.splitlines()[-1], then("fix", "its ranked list"))
        again = self.ok("fix")
        self.assertIn(f"kept {BUILD / 'p1' / 'fix' / 'brief-2.md'} as it is", again)
        self.assertEqual(again.splitlines()[-1], then("fix", "its ranked list"))
        self.assertFalse((folder / "brief-3.md").exists())
        second.write_text("1. the second list\n", encoding="utf-8")
        self.ok("fix")
        third = (folder / "brief-3.md").read_text(encoding="utf-8")
        self.assertEqual(third.splitlines()[-1],
                         f"Produces: {at('fix', 'developer-report-3.md')}")

    def test_fix_can_go_to_the_senior_developer_and_a_role_that_writes_no_code_is_refused(self):
        self.reviewed()
        self.refused("is not a rendered role that writes code", "fix", "--role", "code-reviewer")
        self.assertFalse((self.dir() / "fix").exists())
        out = self.ok("fix", "--role", "senior-developer")
        text = (self.dir() / "fix" / "brief.md").read_text(encoding="utf-8")
        self.assertEqual(text.splitlines()[-1],
                         f"Produces: {at('fix', 'senior-developer-report.md')}")
        self.assertEqual(out.splitlines()[-1], then("fix", "its ranked list"))

    def test_fix_is_refused_before_the_review_and_after_the_close_began(self):
        self.started()
        self.unit("u1", "done")
        self.refused("is in stage build", "fix")
        self.assertFalse((self.dir() / "fix").exists())
        self.assertEqual(self.ledger()["stage"], "build")
        self.ok("review")
        self.ok("close")
        self.refused("is in stage closing", "fix")
        self.assertEqual(self.ledger()["stage"], "closing")

    def test_close_is_refused_before_the_review(self):
        self.started()
        self.unit("u1", "done")
        self.refused("is in stage build", "close")
        self.assertFalse((self.dir() / "close").exists())

    def test_close_after_the_review_or_the_fix_writes_the_writers_brief(self):
        for fix_first in (False, True):
            with self.subTest(fix_first=fix_first), self.fresh():
                self.reviewed()
                if fix_first:
                    self.ok("fix")
                self.ok("unit", "set", "u1", "done", "--commit", "3f2a9c1")
                self.ok("ruling", "kept the old flag; callers break if wrong")
                out = self.ok("close").splitlines()
                brief = self.dir() / "close" / "brief.md"
                text = brief.read_text(encoding="utf-8")
                self.assertIn("The phase `p1` on the branch `phase/p1` is closing.", text)
                self.assertIn("- Unit u1, Unit u1 (commit 3f2a9c1): report "
                              "../u1/developer-report.md\n", text)
                self.assertIn("- kept the old flag; callers break if wrong\n", text)
                self.assertNotIn("Delete", text)
                self.assertEqual(agent_runs.unfilled(text), ["What landed and what was decided"])
                self.assertEqual(self.ledger()["stage"], "closing")
                self.assertEqual(out[-5:], [
                    then("close"), "once its branch is merged, run:",
                    "git rm -r -q .context-gate/build/p1",
                    'git commit -m "Close phase p1: remove its build folder"',
                    f"{roles.GOVERN} phase close"])
                self.assertIn("  1. kept the old flag; callers break if wrong", out)
                self.assertIn("phase close", self.ok("status").splitlines()[-1])

    def test_the_writer_is_not_told_to_delete_the_folder_and_its_report_sits_in_it(self):
        self.reviewed()
        self.ok("close")
        text = (self.dir() / "close" / "brief.md").read_text(encoding="utf-8")
        self.assertNotIn("elete", text)
        self.assertEqual(text.splitlines()[-2:],
                         [RELATIVE, f"Produces: {at('close', 'technical-writer-report.md')}"])

    def test_a_close_brief_the_coordinator_filled_is_kept_on_a_second_close(self):
        self.reviewed()
        self.ok("close")
        brief = self.dir() / "close" / "brief.md"
        brief.write_text("the coordinator's account\n", encoding="utf-8")
        before = self.files()
        out = self.ok("close")
        self.assertEqual(self.files(), before)
        self.assertIn(f"kept {BUILD / 'p1' / 'close' / 'brief.md'} as it is", out)
        self.assertIn(then("close"), out.splitlines())

    def test_a_second_close_changes_nothing(self):
        self.reviewed()
        self.ok("close")
        before = self.files()
        self.ok("close")
        self.assertEqual(self.files(), before)


class Launch(Build):
    """`build launch`: the one place a brief is checked, a unit marked running and a launch
    prompt printed."""

    def fill(self, brief: Path) -> None:
        text = "".join("Filled.\n" if line.startswith(agent_runs.FILL) else line + "\n"
                       for line in brief.read_text(encoding="utf-8").splitlines())
        brief.write_text(text, encoding="utf-8")

    def commit(self) -> None:
        self.w.commit("briefs", at=self.w.top, index=False)

    def ready(self, uid: str = "u1", *flags: str) -> Path:
        brief = self.unit(uid, None, *flags)
        self.fill(brief)
        self.commit()
        return brief

    def prompt(self, role: str, path: str) -> list[str]:
        return [f"launch {role} with the prompt:", f"Brief: {path}", f"Base: {self.head()}"]

    def reviewed(self, *flags: str) -> None:
        self.started(*flags)
        self.unit("u1", "done")
        self.code()
        self.ok("review")

    def test_a_unit_is_marked_running_and_gets_its_role_its_brief_and_the_base(self):
        self.started()
        self.ready()
        out = self.ok("launch", "u1")
        self.assertEqual(out.splitlines(),
                         self.prompt("developer", ".context-gate/build/p1/u1/brief.md"))
        unit = self.ledger()["unit"][0]
        self.assertEqual((unit["status"], unit["attempts"]), ("running", 1))
        self.assertEqual(len(self.head()), 40)

    def test_a_senior_developer_works_in_a_worktree_too(self):
        self.started()
        self.ok("unit", "add", "u1", "--title", "T", "--role", "senior-developer")
        self.fill(self.dir() / "u1" / "brief.md")
        self.commit()
        out = self.ok("launch", "u1")
        self.assertEqual(out.splitlines()[0], "launch senior-developer with the prompt:")
        self.assertEqual(out.splitlines()[-1], f"Base: {self.head()}")

    def test_a_relaunch_is_one_more_attempt_from_the_brief_as_committed(self):
        self.started()
        brief = self.ready()
        before = brief.read_bytes()
        self.ok("launch", "u1")
        self.ok("unit", "set", "u1", "failed", "--note", "red")
        self.code()
        out = self.ok("launch", "u1")
        self.assertEqual(self.ledger()["unit"][0]["attempts"], 2)
        self.assertEqual(out.splitlines()[-1], f"Base: {self.head()}")
        self.assertEqual(brief.read_bytes(), before)

    def test_a_brief_still_to_fill_is_refused_by_name_and_nothing_changes(self):
        self.started()
        self.unit("u1")
        self.commit()
        err = self.refused("the brief is not filled in", "launch", "u1")
        self.assertIn(str(BUILD / "p1" / "u1" / "brief.md"), err)
        self.assertEqual(self.ledger()["unit"][0]["status"], "pending")

    def test_a_brief_not_committed_is_refused_by_name(self):
        self.started()
        self.unit("u1")
        self.fill(self.dir() / "u1" / "brief.md")
        err = self.refused("is not committed at HEAD", "launch", "u1")
        self.assertIn(str(BUILD / "p1" / "u1" / "brief.md"), err)

    def test_a_brief_changed_since_it_was_committed_is_refused(self):
        self.started()
        brief = self.ready()
        brief.write_text(brief.read_text(encoding="utf-8") + "More.\n", encoding="utf-8")
        self.refused("is not committed at HEAD", "launch", "u1")
        self.w.git("add", "-A", at=self.w.top)
        self.refused("is not committed at HEAD", "launch", "u1")

    def test_an_unknown_unit_is_refused(self):
        self.started()
        self.refused("has no unit u9", "launch", "u9")

    def test_the_path_is_from_the_repository_root_in_posix_form(self):
        self.started(below="sub")
        self.ready()
        out = self.ok("launch", "u1")
        rel = (self.w.root / BUILD / "p1" / "u1" / "brief.md").relative_to(self.w.top)
        self.assertEqual(out.splitlines()[1], f"Brief: {rel.as_posix()}")
        self.assertNotIn("\\", out)

    def test_review_prints_a_prompt_per_reviewer_brief_and_no_base(self):
        self.reviewed("--high-risk")
        d = self.dir() / "review"
        self.fill(d / "qa-engineer-brief.md")
        self.commit()
        out = self.ok("launch", "review")
        self.assertEqual(out.splitlines(), [
            "launch code-reviewer with the prompt:",
            "Brief: .context-gate/build/p1/review/code-reviewer-brief.md",
            "launch qa-engineer with the prompt:",
            "Brief: .context-gate/build/p1/review/qa-engineer-brief.md"])

    def test_review_without_high_risk_prints_the_code_reviewers_alone(self):
        self.reviewed()
        self.commit()
        self.assertEqual(len(self.ok("launch", "review").splitlines()), 2)

    def test_review_is_refused_while_the_qa_brief_is_unfilled(self):
        self.reviewed("--high-risk")
        self.commit()
        err = self.refused("the brief is not filled in", "launch", "review")
        self.assertIn("qa-engineer-brief.md", err)

    def test_fix_prints_the_brief_not_yet_launched_for_its_role(self):
        self.reviewed()
        self.ok("fix", "--role", "senior-developer")
        first = self.dir() / "fix" / "brief.md"
        self.fill(first)
        self.commit()
        out = self.ok("launch", "fix")
        self.assertEqual(out.splitlines(), self.prompt(
            "senior-developer", ".context-gate/build/p1/fix/brief.md"))
        self.ok("fix", "--role", "senior-developer")
        self.fill(self.dir() / "fix" / "brief-2.md")
        # The ledger folder's .gitignore does not keep `brief-2.md` (`!*brief.md`): add it by force.
        self.w.git("add", str(self.dir() / "fix" / "brief-2.md"), at=self.w.top)
        self.commit()
        self.assertEqual(self.ok("launch", "fix").splitlines(), self.prompt(
            "senior-developer", ".context-gate/build/p1/fix/brief-2.md"))

    def test_close_prints_the_writers_prompt_with_a_base(self):
        self.reviewed()
        self.ok("close")
        self.fill(self.dir() / "close" / "brief.md")
        self.commit()
        self.assertEqual(self.ok("launch", "close").splitlines(), self.prompt(
            "technical-writer", ".context-gate/build/p1/close/brief.md"))

    def test_a_brief_the_command_has_not_written_yet_is_refused(self):
        self.started()
        self.refused("run `" + GOVERN + " build review` first", "launch", "review")

    def test_with_no_build_it_says_so(self):
        self.project()
        code, _, err = self.go("launch", "u1")
        self.assertEqual(code, 1)
        self.assertIn("error:", err)


class Briefs(Build):
    """What every brief the engine writes must hold, so `agent-gates` can hold its agent to it."""

    def every(self) -> dict[str, Path]:
        self.started("--high-risk")
        self.unit("u1", "done", "--red")
        self.code()
        self.ok("review")
        self.ok("fix")
        self.ok("close")
        d = self.dir()
        return {"unit": d / "u1" / "brief.md", "fix": d / "fix" / "brief.md",
                "code-reviewer": d / "review" / "code-reviewer-brief.md",
                "qa-engineer": d / "review" / "qa-engineer-brief.md",
                "close": d / "close" / "brief.md"}

    def test_each_ends_with_a_produces_line_from_the_repository_root(self):
        for name, brief in self.every().items():
            with self.subTest(brief=name):
                lines = brief.read_text(encoding="utf-8").splitlines()
                contract = [n for n, line in enumerate(lines) if line.startswith(("Red: ",
                                                                                 "Produces: "))]
                self.assertEqual(lines[contract[0] - 1], RELATIVE)
                self.assertEqual(contract[-1], len(lines) - 1)
                last = lines[-1]
                self.assertTrue(last.startswith("Produces: "), last)
                relative = last.removeprefix("Produces: ")
                self.assertFalse(Path(relative).is_absolute(), relative)
                report = self.w.root / relative
                self.assertEqual(report.parent, brief.parent)
                self.assertNotIn(report.name, build.REFUSED_NAMES)
                self.assertTrue(report.name.endswith("-report.md"), report.name)
                got = agent_runs.contract(build.launch_line(brief), str(self.w.root))
                launch = {**got, "cwd": str(self.w.root)}
                self.assertEqual(agent_runs.where(launch, "produces", got["produces"][0])[0],
                                 report)
                self.assertEqual(Path(got["brief"]), brief)

    def test_no_committed_file_holds_this_machines_path_to_the_repository(self):
        self.every()
        self.ok("unit", "add", "u2", "--title", "T")
        forms = {str(self.w.root), self.w.root.as_posix(), str(self.w.root.resolve()),
                 self.w.root.resolve().as_posix()}
        kept = [p for p in self.w.git("ls-files", "--others", "--cached", "--exclude-standard",
                                      "-z", BUILD.as_posix()).split("\0") if p]
        self.assertGreaterEqual(len(kept), 7)
        for rel in kept:
            text = (self.w.top / rel).read_text(encoding="utf-8", errors="replace")
            for form in forms:
                self.assertNotIn(form, text, rel)

    def test_a_brief_moved_to_another_checkout_names_its_files_from_that_checkouts_root(self):
        brief = self.every()["unit"]
        other = self.tmp / "elsewhere"
        moved = other / BUILD / "p1" / brief.parent.name
        moved.mkdir(parents=True)
        (moved / "brief.md").write_bytes(brief.read_bytes())
        got = agent_runs.contract(build.launch_line(moved / "brief.md"), str(other))
        launch = {**got, "cwd": str(other)}
        self.assertEqual(agent_runs.where(launch, "produces", got["produces"][0])[0],
                         moved / "developer-report.md")

    def test_no_report_shares_a_name_the_write_tool_refuses(self):
        self.assertEqual(build.REFUSED_NAMES,
                         ("report.md", "findings.md", "summary.md", "analysis.md"))

    def test_a_unit_brief_is_refused_at_launch_until_every_section_is_filled(self):
        brief = self.every()["unit"]
        home = self.tmp / "gate-home"

        def launch():
            return agent_runs.on_pre_launch(home, "s1", GATES, {"tool_input": {
                "subagent_type": "developer", "prompt": build.launch_line(brief)}})
        reason = launch()["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("the brief is not filled in: Task, Files, Tests, Follow, Out of scope",
                      reason)
        text = brief.read_text(encoding="utf-8")
        for _, what in build.UNIT_SECTIONS[:-1]:
            text = text.replace(build._fill(what), "Filled.")
        brief.write_text(text, encoding="utf-8")
        self.assertIn("the brief is not filled in: Out of scope.",
                      launch()["hookSpecificOutput"]["permissionDecisionReason"])
        brief.write_text(text.replace(build._fill(build.UNIT_SECTIONS[-1][1]), "Nothing."),
                         encoding="utf-8")
        self.assertIsNone(launch())


if __name__ == "__main__":
    unittest.main()
