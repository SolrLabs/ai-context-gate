"""`govern build`: the next action, starting, units and their ledger, and the status line."""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_support import *  # noqa: E402,F401,F403

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


class Start(Build):
    def test_it_writes_the_ledger_for_the_checked_out_branch(self):
        w = self.project()
        w.write("docs/plan-login.md", "the plan\n")
        w.commit("plan", at=w.top, index=False)
        out = self.ok("start", "p1", "--plan", "docs/plan-login.md", "--high-risk")
        self.assertEqual(self.ledger(), {
            "phase": "p1", "branch": "phase/p1", "base": self.head("main"),
            "from": "main", "plan": "docs/plan-login.md", "high_risk": True, "visual": False, "stage": "build",
            "rulings": []})
        self.assertIn(f"started p1 on phase/p1, from main at {self.head('main')[:12]}", out)
        self.assertIn(f"wrote {BUILD / 'p1' / build.LEDGER}", out)
        self.assertIn("Next: Add the first unit", out)

    def test_without_a_plan_the_ledger_names_none(self):
        self.started()
        self.assertNotIn("plan", self.ledger())
        self.assertIs(self.ledger()["high_risk"], False)

    def test_visual_is_stored_and_a_ledger_without_the_key_loads_as_not_visual(self):
        self.started("--visual", extra=VISUAL_ON)
        self.assertIs(self.ledger()["visual"], True)
        self.assertIs(build.load(self.dir() / build.LEDGER).visual, True)
        self.assertIn("visual", self.ok("status").splitlines()[0])
        self.assertIs(json.loads(self.ok("status", "--json"))["visual"], True)
        path = self.dir() / build.LEDGER
        path.write_text(path.read_text(encoding="utf-8").replace("visual = true\n", ""),
                        encoding="utf-8")
        self.assertIs(build.load(path).visual, False)

    def test_visual_without_a_rendered_ux_reviewer_is_refused_and_writes_nothing(self):
        self.project()
        err = self.refused("this phase is visual and `ux-reviewer` is not rendered: set "
                           "`visual_paths` on `agent-roles`, run `" + GOVERN + " agents render`, "
                           "then try again", "start", "p1", "--visual")
        self.assertFalse(self.dir().exists(), err)

    def test_visual_with_visual_paths_set_starts(self):
        self.started("--visual", extra=VISUAL_ON)
        self.assertIs(self.ledger()["visual"], True)

    def test_review_of_a_visual_ledger_without_the_role_is_refused(self):
        self.started()
        self.unit("u1", "done")
        path = self.dir() / build.LEDGER
        path.write_text(path.read_text(encoding="utf-8").replace("visual = false", "visual = true"),
                        encoding="utf-8")
        self.refused("`ux-reviewer` is not rendered", "review")
        self.assertFalse((self.dir() / "review").exists())

    def test_a_visual_that_is_not_true_or_false_is_refused(self):
        self.started()
        path = self.dir() / build.LEDGER
        path.write_text(path.read_text(encoding="utf-8").replace("visual = false", 'visual = "yes"'),
                        encoding="utf-8")
        self.assertIn("`visual` is not true or false", self.refused("not a build ledger", "status"))

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
        self.assertIn("name no whole-suite run", text)
        self.assertEqual([line for line in lines if line.startswith("## ")],
                         ["## Context", "## Task", "## Files", "## Tests", "## Follow",
                          "## Out of scope"])
        self.assertEqual(sum(line.startswith(agent_runs.FILL) for line in lines), 6)
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
        keys = ["phase", "branch", "base", "plan", "high_risk", "visual", "stage", "ledger", "rulings",
                "units", "done", "total", "next"]
        self.assertEqual((list(some), list(none)), (keys, keys))
        self.assertEqual(list(some["units"][0]), ["id", "title", "role", "status", "attempts",
                                                  "branch", "commit", "note", "brief", "report"])
        self.assertEqual((none["phase"], none["units"]), (None, []))
        self.assertEqual(some["units"][0]["brief"], ".context-gate/build/p1-login/u1/brief.md")


if __name__ == "__main__":
    unittest.main()
