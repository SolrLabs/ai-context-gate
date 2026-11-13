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
from base_support import BLOCK, decision, fm  # noqa: E402
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
        self.refused_done(f"the last full gate is red (rc 3 at {head[:7]})")

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
        self.refused_done("no full gate has run on this phase")

    def test_a_commit_made_while_the_gate_runs_is_not_recorded_as_tested(self):
        self.started_with("git -c user.name=t -c user.email=t@t commit -q --allow-empty -m x")
        self.code()
        before = self.head()
        self.ok("gate")
        self.assertNotEqual(self.head(), before)
        rec = self.record()
        self.assertEqual((rec["head"], rec["dirty"]), (before, True))
        self.refused_done("the last full gate is stale")

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
        self.refused_done(f"the last full gate is stale: 1 files changed since {self.head('HEAD~1')[:7]}")

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
        for part in ("mark u1 done", "the last full gate is red", "the flaky leg"):
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

    def test_review_refuses_on_red_and_accept_red_passes(self):
        self.started_with("exit 3", unit_gate="full")
        self.ok("unit", "set", "u1", "done")
        self.code()
        self.go("gate")
        code, out, err = self.go("review")
        self.assertEqual(code, 1, out)
        self.assertIn("the last full gate is red", err)
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
        self.refused_done("the last full gate is red (rc 1 at")

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
