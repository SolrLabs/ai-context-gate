"""`govern build gate`, and the refusals that follow a red, missing or stale full gate.

    python3 -m unittest discover -s engine/tests -k build_gate
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import profile_guard  # noqa: E402,F401

from build_support import *  # noqa: E402,F401,F403
from unittest import mock  # noqa: E402
from base_support import BLOCK, Tree, decision, fm  # noqa: E402
from govern import roles  # noqa: E402
from test_build_workspace import Workspace  # noqa: E402


def gated(cmd: str, unit_gate: str = "scoped") -> str:
    return (f'\n[checks.agent-roles]\nlevel = "warn"\nfull_gate = "{cmd}"\n'
            f'unit_gate = "{unit_gate}"\n')


class BuildGate(Build):
    def record(self) -> dict:
        return json.loads((self.dir() / "gate" / "last.json").read_text(encoding="utf-8"))

    def started_with(self, cmd: str, **kw) -> None:
        self.started(extra=gated(cmd, **kw))
        self.unit("u1")

    def refused_done(self, message: str, *extra: str) -> str:
        code, out, err = self.go("unit", "set", "u1", "done", *extra)
        self.assertEqual(code, 1, out)
        self.assertIn(message, err)
        self.assertEqual(self.ledger()["unit"][0]["status"], "pending")
        return err

    def test_a_red_gate_is_recorded_and_unit_set_done_refuses(self):
        self.started_with("echo boom && exit 3")
        code, out, err = self.go("gate")
        self.assertEqual((code, err), (1, ""))
        head = self.head()
        self.assertIn(f"gate red (rc 3, ", out)
        self.assertIn(f"head {head[:7]})", out)
        self.assertIn("boom", out)
        rec = self.record()
        self.assertEqual((rec["status"], rec["rc"], rec["head"]), ("done", 3, head))
        self.assertEqual(rec["cmd"], "echo boom && exit 3")
        self.assertTrue((self.dir() / "gate" / "last.log").is_file())
        self.refused_done(f"the last local suite run is red (rc 3 at {head[:7]})")

    def test_a_green_gate_lets_unit_set_done_through(self):
        self.started_with("exit 0")
        self.code()
        code, out, err = self.go("gate")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("gate green (rc 0, ", out)
        self.assertFalse(self.record()["dirty"])
        self.ok("unit", "set", "u1", "done")
        self.assertEqual(self.ledger()["unit"][0]["status"], "done")

    def test_no_record_refuses(self):
        self.started_with("exit 0")
        self.refused_done("no local suite run on this phase")

    def test_a_commit_made_while_the_gate_runs_is_not_recorded_as_tested(self):
        self.started_with("git -c user.name=t -c user.email=t@t commit -q --allow-empty -m x")
        self.code()
        before = self.head()
        self.ok("gate")
        self.assertNotEqual(self.head(), before)
        rec = self.record()
        self.assertEqual((rec["head"], rec["dirty"]), (before, True))
        self.refused_done("the last local suite run is stale")

    def test_a_commit_under_the_build_folder_keeps_the_record_fresh(self):
        self.started_with("exit 0")
        self.code()
        self.ok("gate")
        self.ok("ruling", "a note")
        self.w.git("add", "-A")
        self.w.git("commit", "-qm", "ledger")
        self.ok("unit", "set", "u1", "done")

    def test_a_commit_on_any_other_file_makes_it_stale_with_the_count(self):
        self.started_with("exit 0")
        self.code()
        self.ok("gate")
        self.code("src/b.py")
        self.refused_done(f"the last local suite run is stale: 1 files changed since {self.head('HEAD~1')[:7]}")

    def test_an_uncommitted_change_when_the_gate_ran_is_recorded_as_dirty(self):
        self.started_with("exit 0")
        self.w.write("src/loose.py", "x = 1\n")
        self.ok("gate")
        self.assertTrue(self.record()["dirty"])
        self.refused_done("stale")

    def test_accept_red_lets_it_through_and_records_a_ruling(self):
        self.started_with("exit 3")
        self.go("gate")
        self.ok("unit", "set", "u1", "done", "--accept-red", "the flaky leg")
        led = self.ledger()
        self.assertEqual(led["unit"][0]["status"], "done")
        self.assertEqual(len(led["rulings"]), 1)
        text = led["rulings"][0]
        for part in ("mark u1 done", "the last local suite run is red", "the flaky leg"):
            self.assertIn(part, text)

    def test_without_a_full_gate_nothing_refuses_and_gate_says_so(self):
        self.started(extra='\n[checks.agent-roles]\nlevel = "warn"\n')
        self.unit("u1")
        code, out, err = self.go("gate")
        self.assertEqual((code, err), (1, ""))
        self.assertIn("no full_gate set under [checks.agent-roles]; nothing was run", out)
        self.ok("unit", "set", "u1", "done")
        self.code()
        self.ok("review")

    def test_a_full_unit_gate_does_not_refuse_unit_set(self):
        self.started_with("exit 3", unit_gate="full")
        self.ok("unit", "set", "u1", "done")

    def started_default(self, cmd: str) -> None:
        self.started(extra=f'\n[checks.agent-roles]\nlevel = "warn"\nfull_gate = "{cmd}"\n')
        self.unit("u1")

    def test_by_default_unit_set_done_needs_no_gate_record(self):
        self.started_default("exit 3")
        self.assertFalse((self.dir() / "gate" / "last.json").exists())
        self.ok("unit", "set", "u1", "done")
        self.assertEqual(self.ledger()["unit"][0]["status"], "done")

    def test_by_default_review_refuses_without_a_record_and_takes_a_green_one(self):
        self.started_default("exit 0")
        self.ok("unit", "set", "u1", "done")
        self.code()
        code, out, err = self.go("review")
        self.assertEqual(code, 1, out)
        self.assertIn("gate", err)
        self.assertFalse((self.dir() / "review").exists())
        self.go("gate")
        self.ok("review")

    def test_review_refuses_on_red_and_accept_red_passes(self):
        self.started_with("exit 3", unit_gate="full")
        self.ok("unit", "set", "u1", "done")
        self.code()
        self.go("gate")
        code, out, err = self.go("review")
        self.assertEqual(code, 1, out)
        self.assertIn("the last local suite run is red", err)
        self.assertFalse((self.dir() / "review").exists())
        self.ok("review", "--accept-red", "known")
        self.assertEqual(len(self.ledger()["rulings"]), 1)

    def test_cmd_runs_that_command_and_records_nothing(self):
        self.started_with("exit 3")
        code, out, err = self.go("gate", "--cmd", "exit 0")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("gate green", out)
        self.assertFalse((self.dir() / "gate" / "last.json").exists())

    def test_a_running_record_is_written_before_the_command_ends(self):
        self.started_with("exit 0")
        seen = []
        real = build._write_record

        def spy(path, data):
            seen.append(data["status"])
            real(path, data)

        from unittest import mock
        with mock.patch.object(build, "_write_record", spy):
            self.go("gate")
        self.assertEqual(seen, ["running", "done"])

class BuildGateDocs(Build):
    """Documentation never makes the last local suite run stale; a unit's own record counts."""

    record = BuildGate.record
    started_with = BuildGate.started_with
    refused_done = BuildGate.refused_done

    def green_then(self, name: str, text: str | None = None) -> None:
        self.started_with("exit 0")
        self.code()
        self.ok("gate")
        self.code(name, fm() + "words\n" if text is None else text)

    def test_a_docs_commit_keeps_the_record_fresh(self):
        self.green_then("docs/x.md")
        self.ok("unit", "set", "u1", "done")

    def test_a_root_readme_keeps_the_record_fresh(self):
        self.green_then("README.md")
        self.ok("unit", "set", "u1", "done")

    def test_a_nested_readme_makes_it_stale(self):
        self.started(extra=gated("exit 0") + 'docs_paths = ["docs/**", "*.md"]\n')
        self.unit("u1")
        self.code()
        self.ok("gate")
        self.code("engine/x.md", fm() + "words\n")
        self.refused_done("the last local suite run is stale: 1 files changed since")

    def test_docs_only_gate_skips_the_suite(self):
        self.started_with("exit 0")
        self.code()
        self.ok("gate")
        self.assertEqual(self.record()["scope"], "full")
        self.code("docs/x.md", fm() + "words\n")
        with mock.patch.object(build, "_full_gate", return_value="exit 7"):
            code, out, err = self.go("gate")
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("gate green (docs only: govern check, ", out)
        rec = self.record()
        self.assertEqual((rec["scope"], rec["cmd"], rec["head"], rec["dirty"]),
                         ("docs", "govern check", self.head(), False))

    def test_a_code_change_beside_the_docs_runs_the_suite(self):
        self.started_with("exit 0")
        self.code()
        self.ok("gate")
        self.code("docs/x.md")
        self.code("src/b.py")
        with mock.patch.object(build, "_full_gate", return_value="exit 7"):
            code, out, err = self.go("gate")
        self.assertEqual(code, 1, out)
        self.assertEqual(self.record()["scope"], "full")

    def test_nothing_changed_runs_the_suite(self):
        self.started_with("exit 0")
        self.code()
        self.ok("gate")
        with mock.patch.object(build, "_full_gate", return_value="exit 7"):
            code, out, err = self.go("gate")
        self.assertEqual(code, 1, out)
        self.assertEqual(self.record()["scope"], "full")

    def test_the_default_docs_paths_worked_examples(self):
        for path, want in (("engine/govern/CHANGELOG.md", True), ("docs/plan.md", True),
                           ("README.md", True), ("engine/README.md", True),
                           ("plugin/skills/build/SKILL.md", False), ("skills/a.md", False),
                           ("engine/govern/templates/build-rules.md", False),
                           (".claude/agents/developer.md", False), ("docs/notes.txt", False),
                           ("src/app.py", False)):
            with self.subTest(path=path):
                self.assertIs(build._is_docs(path, list(roles.DOCS_PATHS)), want)

    def test_a_later_docs_pattern_decides_and_bang_excludes(self):
        patterns = ["**/*.md", "!x/**", "x/keep.md"]
        self.assertTrue(build._is_docs("x/keep.md", patterns))
        self.assertFalse(build._is_docs("x/a.md", patterns))
        self.assertTrue(build._is_docs("y/a.md", patterns))

    def test_a_changelog_only_commit_runs_govern_check_only(self):
        self.started_with("exit 0")
        self.code()
        self.ok("gate")
        self.code("engine/govern/CHANGELOG.md", fm() + "words\n")
        with mock.patch.object(build, "_full_gate", return_value="exit 7"):
            code, out, err = self.go("gate")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.record()["scope"], "docs")

    def test_the_docs_paths_matcher(self):
        patterns = ["docs/**", "*.md"]
        for path, want in (("docs/plan.md", True), ("docs/a/b.md", True), ("README.md", True),
                           ("engine/README.md", False),
                           ("engine/govern/templates/build-rules.md", False),
                           ("plugin/skills/build/SKILL.md", False), ("docsx/a.md", False)):
            with self.subTest(path=path):
                self.assertIs(build._is_docs(path, patterns), want)
        self.assertTrue(build._is_docs("a/b", ["a/**/b"]))
        self.assertTrue(build._is_docs("a/x/y/b", ["a/**/b"]))
        self.assertFalse(build._is_docs("a/b/c", ["a/?"]))

    def unit_record(self, **over) -> None:
        rec = {"status": "done", "rc": 0, "check_rc": 0, "head": self.head(), "dirty": False,
               "seconds": 1, "at": "now", "cmd": "exit 0", "scope": "full", **over}
        (self.dir() / "gate").mkdir(exist_ok=True)
        (self.dir() / "gate" / "u1.json").write_text(json.dumps(rec), encoding="utf-8")

    def test_a_green_unit_record_is_enough_without_last_json(self):
        self.started_with("exit 0")
        self.code()
        self.unit_record()
        self.assertFalse((self.dir() / "gate" / "last.json").exists())
        self.ok("unit", "set", "u1", "done")

    def test_a_code_commit_after_the_unit_record_refuses(self):
        self.started_with("exit 0")
        self.code()
        self.unit_record()
        self.code("src/b.py")
        self.refused_done("no local suite run on this phase")

    def test_a_red_or_dirty_unit_record_refuses(self):
        self.started_with("exit 0")
        self.code()
        for over in ({"rc": 1}, {"dirty": True}):
            with self.subTest(over=over):
                self.unit_record(**over)
                self.refused_done("no local suite run on this phase")

    def test_docs_gate_runs_beside_govern_check_and_can_turn_it_red(self):
        self.started(extra=gated("exit 0") + 'docs_gate = "exit 4"\n')
        self.unit("u1")
        self.code()
        self.ok("gate")
        self.code("docs/x.md", fm() + "words\n")
        code, out, err = self.go("gate")
        self.assertEqual(code, 1, out)
        rec = self.record()
        self.assertEqual((rec["scope"], rec["rc"], rec["check_rc"]), ("docs", 4, 0))
        self.assertIn("exit 4", rec["cmd"])
        self.assertIn("govern check", rec["cmd"])

    def test_an_uncommitted_change_beside_a_docs_commit_runs_the_suite(self):
        self.started_with("exit 0")
        self.code()
        self.ok("gate")
        self.code("docs/x.md", fm() + "words\n")
        self.w.write("src/x.py", "x = 1\n")
        with mock.patch.object(build, "_full_gate", return_value="exit 7"):
            code, out, err = self.go("gate")
        self.assertEqual(code, 1, out)
        self.assertEqual(self.record()["scope"], "full")

    def test_a_stale_unit_record_is_named_in_the_refusal(self):
        self.started_with("exit 0")
        self.code()
        self.unit_record()
        was = self.head()[:7]
        self.code("src/b.py")
        err = self.refused_done("no local suite run on this phase")
        self.assertIn(f"; gate/u1.json is stale: 1 files changed since {was}", err)

    def test_a_running_unit_record_is_named_in_the_refusal(self):
        self.started_with("exit 0")
        self.code()
        self.unit_record(status="running")
        err = self.refused_done("no local suite run on this phase")
        self.assertIn("; gate/u1.json is still running", err)

    def test_gate_in_a_linked_worktree_and_the_hand_over(self):
        self.started_with("exit 0")
        self.code()
        tree = self.tmp / "unit-tree"
        self.w.git("worktree", "add", "-q", "-b", "unit/u1", str(tree), "HEAD", at=self.w.top)
        other = Tree(tree, self.w.home)
        code, out, err = other.run("build", "gate")
        self.assertEqual((code, err), (0, ""), out)
        made = tree / build.BUILD_DIR / "p1" / "gate" / "last.json"
        self.assertTrue(made.is_file())
        self.assertFalse((self.dir() / "gate" / "last.json").exists())
        code, out, err = other.run("build", "unit", "set", "u1", "running")
        self.assertEqual(code, 1, out)
        self.assertIn("no build is in flight", err.lower())
        out = self.ok("unit", "set", "u1", "done", "--branch", "unit/u1")
        self.assertIn("copied the gate record of unit/u1", out)
        self.assertEqual(json.loads((self.dir() / "gate" / "u1.json").read_text(encoding="utf-8")),
                         json.loads(made.read_text(encoding="utf-8")))

    def test_no_worktree_copies_nothing_and_says_so(self):
        self.started_with("exit 0")
        self.code()
        self.w.git("branch", "unit/u1")
        ctx = self.w.context()
        said = build._take_unit_gate(ctx, build.current(ctx.root, "phase/p1"), "u1", "unit/u1")
        self.assertEqual(said, ["no worktree has unit/u1 checked out; no gate record copied"])
        self.assertFalse((self.dir() / "gate" / "u1.json").exists())
        tree = self.tmp / "unit-tree"
        self.w.git("worktree", "add", "-q", str(tree), "unit/u1", at=self.w.top)
        said = build._take_unit_gate(ctx, build.current(ctx.root, "phase/p1"), "u1", "unit/u1")
        self.assertEqual(said, ["the worktree of unit/u1 has no gate record; none copied"])
        self.assertFalse((self.dir() / "gate" / "u1.json").exists())


BAD_DOC = "no frontmatter\n"


class BuildGateCheck(Build):
    """The gate also runs the project's own `govern check`."""

    record = BuildGate.record
    started_with = BuildGate.started_with
    refused_done = BuildGate.refused_done

    def test_a_document_error_turns_a_green_full_gate_red(self):
        self.started_with("exit 0")
        self.code("docs/bad.md", BAD_DOC)
        code, out, err = self.go("gate")
        self.assertEqual((code, err), (1, ""))
        self.assertIn("gate red (rc 1, ", out)
        self.assertIn("check: red (govern check)", out)
        self.assertIn("docs/bad.md", out)
        rec = self.record()
        self.assertEqual((rec["rc"], rec["check_rc"]), (1, 1))
        self.refused_done("the last local suite run is red (rc 1 at")

    def test_accept_red_still_passes_a_red_check(self):
        self.started_with("exit 0")
        self.code("docs/bad.md", BAD_DOC)
        self.go("gate")
        self.ok("unit", "set", "u1", "done", "--accept-red", "the doc is a fixture")
        self.assertEqual(self.ledger()["unit"][0]["status"], "done")

    def test_a_clean_tree_stays_green(self):
        self.started_with("exit 0")
        self.code()
        self.ok("gate")
        rec = self.record()
        self.assertEqual((rec["rc"], rec["check_rc"]), (0, 0))

    def test_a_red_full_gate_is_not_pushed_out_of_view_by_the_check(self):
        self.started_with("echo boom && exit 3")
        self.code("docs/bad.md", BAD_DOC)
        code, out, err = self.go("gate")
        self.assertEqual(code, 1, out)
        self.assertIn("boom", out)
        self.assertLess(out.index("boom"), out.index("check: red"))
        self.assertIn("docs/bad.md", out)

    def test_a_red_check_alone_names_the_offending_file(self):
        self.started_with("exit 0")
        self.code("docs/bad.md", BAD_DOC)
        code, out, err = self.go("gate")
        self.assertEqual(code, 1, out)
        after = out.split("check: red", 1)[1]
        self.assertIn("docs/bad.md", after)

    def test_a_long_full_gate_output_does_not_hide_the_check_failure(self):
        self.started_with("exit 0")
        self.code("docs/bad.md", BAD_DOC)
        for n in range(30):
            self.code(f"docs/ok{n}.md", BAD_DOC)
        code, out, err = self.go("gate")
        self.assertEqual(code, 1, out)
        self.assertLessEqual(len(out.splitlines()), 3 + 2 * build.GATE_TAIL)

    def test_a_red_full_gate_keeps_its_own_rc(self):
        self.started_with("exit 3")
        self.code("docs/bad.md", BAD_DOC)
        self.go("gate")
        rec = self.record()
        self.assertEqual((rec["rc"], rec["check_rc"]), (3, 1))

    def test_gate_cmd_runs_no_check(self):
        self.started_with("exit 0")
        self.code("docs/bad.md", BAD_DOC)
        code, out, err = self.go("gate", "--cmd", "exit 0")
        self.assertEqual((code, err), (0, ""))
        self.assertNotIn("check:", out)
        self.assertFalse((self.dir() / "gate" / "last.json").exists())


class BuildGateWorkspace(Workspace):
    def test_in_a_workspace_only_the_member_is_checked(self):
        w = self.space(extra=gated("exit 0"))
        for n, num in (("alpha", 100), ("beta", 200)):
            w.write(f"{n}/DECISIONS.md",
                    fm() + "# Decisions\n" + decision(n[0].upper(), num) + "\n" + BLOCK)
        w.run("index")
        for n in ("alpha", "beta"):
            w.commit("decisions", at=w.member(n), index=False)
        for n in ("alpha", "beta"):
            w.phase_branch(n)
            self.ok("--project", n, "start", "p1")
        w.write("beta/docs/bad.md", BAD_DOC)
        w.commit("bad", at=w.member("beta"), index=False)
        code, out, err = self.go("--project", "alpha", "gate")
        self.assertEqual((code, err), (0, ""), out)
        self.assertNotIn("check:", out)
        w.write("alpha/docs/bad.md", BAD_DOC)
        w.commit("bad", at=w.member("alpha"), index=False)
        code, out, err = self.go("--project", "alpha", "gate")
        self.assertEqual((code, err), (1, ""))
        self.assertIn("check: red (govern check --project alpha)", out)


if __name__ == "__main__":
    unittest.main()
