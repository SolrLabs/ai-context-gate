"""Agent gates: the produces, checklist, held-brief, red, launch and push gates, driven through the hook."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from pathlib import PurePosixPath
from pathlib import PureWindowsPath
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent_runs_support import *  # noqa: E402,F401,F403
import types  # noqa: E402
import test_agent_runs_measure  # noqa: E402

# Resolve lives in test_agent_runs_measure; a plain namespace keeps the test runner from collecting it twice.
Resolve = types.SimpleNamespace(Cfg=test_agent_runs_measure.Resolve.Cfg, Chk=test_agent_runs_measure.Resolve.Chk)

class ProducesGate(HookCase):
    def test_missing_file_blocks_twice_then_lets_go_and_records_it(self):
        report = self.work / "dev-report.md"
        self.launch(f"Do it.\nProduces: {report}")
        for _ in range(2):
            out = self.stop()
            self.assertEqual(out["decision"], "block")
            self.assertIn(str(report), out["reason"])
        self.assertIsNone(self.stop())
        row = self.rows()[-1]
        self.assertEqual(row["gate_blocks"], 2)
        self.assertIn("does not exist", row["gate_unmet"][0])

    def test_file_written_after_a_refusal_passes(self):
        report = self.work / "dev-report.md"
        self.launch(f"Do it.\nProduces: {report}")
        self.assertEqual(self.stop()["decision"], "block")
        report.write_text("the report")
        self.assertIsNone(self.stop())
        self.assertEqual((self.rows()[-1]["gate_blocks"], self.rows()[-1]["gate_unmet"]), (1, []))

    def test_empty_file_and_stale_file_block(self):
        empty, stale = self.work / "a.md", self.work / "b.md"
        empty.write_text("")
        stale.write_text("from an earlier run")
        old = time.time() - 3600
        os.utime(stale, (old, old))
        self.launch(f"Produces: {empty}, {stale}")
        reason = self.stop()["reason"]
        self.assertIn("is empty", reason)
        self.assertIn("older than this run", reason)

    def test_no_contract_never_blocks(self):
        self.launch("Run date.")
        self.assertIsNone(self.stop())
        self.assertEqual(self.rows()[-1]["gate_blocks"], 0)

    def test_contract_is_read_from_the_brief_file(self):
        brief, report = self.work / "brief.md", self.work / "dev-report.md"
        brief.write_text(f"# Unit\n\nProduces: {report}\n")
        self.launch(f"Brief: {brief} Read it and do the work.")
        self.assertIn(str(report), self.stop()["reason"])

    def test_final_message_naming_a_missing_report_blocks(self):
        self.launch("Run date.")
        ghost = self.work / "unit" / "report.md"
        self.assertIn("names", self.stop(final=f"All done.\nFull report: {ghost}")["reason"])
        self.assertIsNone(self.stop(final="All done. I wrote no report file."))

    def test_ordinary_sentences_are_not_claims(self):
        self.launch("Run date.")
        for final in ("Wrote engine/tests/test_roles.py and ran it.",
                      "I wrote the handler for /api/login",
                      "saved to ./out/red.txt",
                      "The report is ready.",
                      "Findings: /no/such/file.md"):
            with self.subTest(final=final):
                self.assertIsNone(self.stop(final=final))

    def test_a_claim_starts_at_a_boundary_and_is_absolute(self):
        for text in ("Wrote engine/tests/test_roles.py", "saved to ./out/red.txt",
                     "wrote the handler for /api/login", "report: x/abs/missing.md",
                     "Findings: /abs/missing.md"):
            with self.subTest(text=text):
                self.assertEqual(agent_runs.CLAIM.findall(text), [])
        for text in ("Full report: /abs/missing.md", "Wrote `/abs/missing.md`.",
                     "REPORT \"/abs/missing.md\"", "saved to:/abs/missing.md"):
            with self.subTest(text=text):
                self.assertEqual(agent_runs.CLAIM.findall(text), ["/abs/missing.md"])

    def test_a_claim_is_not_checked_with_claims_off(self):
        self.resolve({**GATES, "claims": False}, MEASURE)
        self.launch("Run date.")
        self.assertIsNone(self.stop(final=f"Full report: {self.work / 'ghost.md'}"))

    def test_max_refusals_is_a_param(self):
        self.resolve({**GATES, "max_refusals": 1}, MEASURE)
        self.launch(f"Produces: {self.work / 'r.md'}")
        self.assertEqual(self.stop()["decision"], "block")
        self.assertIsNone(self.stop())
        self.assertEqual(self.rows()[-1]["gate_blocks"], 1)

    def test_relative_produces_in_a_brief_is_looked_for_from_the_root_then_the_briefs_folder(self):
        brief = self.work / "brief.md"
        brief.write_text("Produces: out/report.md\n")
        self.launch(f"Brief: {brief}", cwd=str(self.tmp))
        self.assertIn(str(self.tmp / "out" / "report.md"), self.stop()["reason"])
        (self.work / "out").mkdir()
        (self.work / "out" / "report.md").write_text("done\n")
        self.assertIsNone(self.stop())

    def test_relative_produces_in_the_prompt_resolves_against_the_hook_cwd(self):
        self.launch("Produces: out/report.md", cwd=str(self.work))
        self.assertIn(str(self.work / "out" / "report.md"), self.stop()["reason"])

    def test_the_contract_is_read_from_the_transcript_when_no_launch_was_seen(self):
        report = self.work / "r.md"
        path = Path(self.transcript([("m1", USAGE, "done")]))
        first = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        first["message"]["content"] = f"Do it.\nProduces: {report}"
        path.write_text(json.dumps(first) + "\n" + path.read_text(encoding="utf-8").partition("\n")[2],
                        encoding="utf-8")
        self.assertIn(str(report), self.stop(agent="never-launched", transcript=str(path))["reason"])

    def test_a_run_blocked_and_never_ended_is_said_at_the_notice(self):
        report = self.work / "r.md"
        self.launch(f"Produces: {report}")
        self.assertEqual(self.stop()["decision"], "block")
        line = self.notice()
        self.assertTrue(line.startswith(
            "[agent run] general-purpose: was stopped on an unmet contract and has not "
            "finished: "), line)
        self.assertIn(str(report), line)
        self.assertTrue(line.endswith(" Treat its report as not filed."))

    def test_a_blocked_run_that_then_ends_met_keeps_no_blocked_on(self):
        report = self.work / "r.md"
        self.launch(f"Produces: {report}")
        self.assertEqual(self.stop()["decision"], "block")
        self.assertIn("blocked_on", agent_runs.read_json(self.state / "launch-a1.json"))
        report.write_text("done\n", encoding="utf-8")
        self.assertIsNone(self.stop())
        record = agent_runs.read_json(self.state / "launch-a1.json")
        self.assertIn("ended", record)
        self.assertNotIn("blocked_on", record)
        line = self.notice()
        self.assertTrue(line.startswith("[agent run] general-purpose"), line)
        self.assertIn("Report:", line)
        self.assertNotIn("not finished", line)

    def test_a_blocked_run_is_said_with_measurement_off(self):
        self.resolve(GATES, None)
        report = self.work / "r.md"
        self.launch(f"Produces: {report}")
        self.assertEqual(self.stop()["decision"], "block")
        line = self.notice()
        self.assertTrue(line.startswith(
            "[agent run] general-purpose: was stopped on an unmet contract and has not "
            "finished: "), line)
        self.assertIn(str(report), line)

    def test_a_resumed_run_blocked_and_never_ended_is_said_not_its_earlier_row(self):
        report, second = self.work / "r.md", self.work / "two.md"
        self.launch(f"Produces: {report}")
        report.write_text("done\n", encoding="utf-8")
        self.assertIsNone(self.stop())
        self.fire("PreToolUse", tool_name="SendMessage",
                  tool_input={"to": "a1", "message": f"more\nProduces: {second}"})
        self.fire("SubagentStart", agent_id="a1", agent_type="general-purpose")
        self.assertEqual(self.stop()["decision"], "block")
        line = self.notice()
        self.assertTrue(line.startswith(
            "[agent run] general-purpose: was stopped on an unmet contract and has not "
            "finished: "), line)
        self.assertIn(str(second), line)

    def test_a_resume_leaves_no_blocked_on_on_the_record(self):
        report = self.work / "r.md"
        self.launch(f"Produces: {report}")
        self.assertEqual(self.stop()["decision"], "block")
        self.fire("SubagentStart", agent_id="a1", agent_type="general-purpose")
        self.assertNotIn("blocked_on", agent_runs.read_json(self.state / "launch-a1.json"))

    def test_an_unseen_agent_with_no_type_contract_or_calls_leaves_no_row(self):
        out = self.fire("SubagentStop", agent_id="helper", agent_type="",
                        last_assistant_message="done", agent_transcript_path="")
        self.assertIsNone(out)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.durable(), [])
        self.assertFalse((self.state / "launch-helper.json").exists())

    def test_an_unseen_agent_with_a_contract_is_still_gated_and_logged(self):
        report = self.work / "r.md"
        path = Path(self.transcript([("m1", USAGE, "done")]))
        first = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        first["message"]["content"] = f"Do it.\nProduces: {report}"
        path.write_text(json.dumps(first) + "\n" + path.read_text(encoding="utf-8").partition("\n")[2],
                        encoding="utf-8")

        def stop():
            return self.fire("SubagentStop", agent_id="helper", agent_type="",
                             last_assistant_message="done", agent_transcript_path=str(path))
        self.assertEqual(stop()["decision"], "block")
        report.write_text("done\n", encoding="utf-8")
        self.assertIsNone(stop())
        self.assertEqual(len(self.rows()), 1)
        durable = self.durable()
        self.assertEqual(len(durable), 2)
        self.assertTrue(durable[0]["unfinished"])
        self.assertNotIn("unfinished", durable[1])
        self.assertEqual(len(agent_runs.load_runs(self.home, self.root, True)[0]), 1)


class ChecklistGate(HookCase):
    """With `checklist` on, a report must answer each line of the role's hand-back checklist."""
    gates = {**GATES, "checklist": True,
             "checklist_keys": {"developer": list(TEXTS)}, "checklist_texts": {"developer": TEXTS}}

    def start(self, report, agent_type):
        brief = self.work / "brief.md"
        brief.write_text(f"# Unit\n\nProduces: {report}\n", encoding="utf-8")
        self.launch(f"Brief: {brief} Read it and do the work.", agent_type=agent_type)

    def run_report(self, text, agent_type="developer"):
        report = self.work / "dev-report.md"
        report.write_text(text, encoding="utf-8")
        self.start(report, agent_type)
        out = self.fire("SubagentStop", agent_id="a1", agent_type=agent_type,
                        last_assistant_message="done", agent_transcript_path="")
        return report, out

    def test_a_report_missing_a_key_blocks_and_names_it(self):
        report, out = self.run_report("Scope: a.py\nNot verified: nothing\n")
        self.assertEqual(out["decision"], "block")
        self.assertIn(f"{report} does not answer the checklist line `Gate`.", out["reason"])
        self.assertNotIn("`Scope`", out["reason"])

    def test_every_key_answered_in_each_shape_passes(self):
        _, out = self.run_report("Scope: a.py\n1. `Gate:` green\n- **Not verified:** none\n")
        self.assertIsNone(out)

    def test_off_by_default_does_not_block(self):
        self.resolve({**GATES, "checklist": False}, MEASURE)
        _, out = self.run_report("nothing answered\n")
        self.assertIsNone(out)

    def test_an_agent_the_engine_does_not_render_is_not_checked(self):
        _, out = self.run_report("nothing answered\n", agent_type="Explore")
        self.assertIsNone(out)

    def test_a_missing_file_reports_only_the_file(self):
        report = self.work / "dev-report.md"
        self.start(report, "developer")
        out = self.fire("SubagentStop", agent_id="a1", agent_type="developer",
                        last_assistant_message="done", agent_transcript_path="")
        self.assertIn(f"{report} does not exist.", out["reason"])
        self.assertNotIn("checklist", out["reason"].split("Each line above")[0])

    def test_answers_matches_the_worked_examples(self):
        for text, expect in (("Scope: a.py", True), ("1. `Scope:` a.py", True),
                             ("- **Scope:** a.py", True), ("Scope:", False),
                             ("Scope:   \n", False), ("The scope: a.py", False),
                             ("Scope a.py", False), ("**Scope**: x", True),
                             ("`Scope`: x", True), ("1) Scope: x", True),
                             ("- [x] Scope: x", True), ("- [ ] Scope: x", True),
                             ("scope: x", True), ("Scope:\nx.py", True),
                             ("Scope:\n\nx.py", False), ("Scope:\nGate: ok", False),
                             ("Scope:\nGate:\n", False)):
            with self.subTest(text=text):
                self.assertEqual(agent_runs.answers(text, "Scope", TEXTS), expect)

    def test_a_pasted_checklist_answers_no_key(self):
        from govern import roles
        pasted = roles.handback("developer", {"handback_checks": [], "extra_roles": []})
        texts = roles.handback_texts("developer", {"handback_checks": [], "extra_roles": []})
        for key in texts:
            with self.subTest(key=key):
                self.assertFalse(agent_runs.answers(pasted, key, texts))

    def test_a_bom_file_and_a_crlf_file_with_every_key_answered_pass(self):
        body = "Scope: a.py\nGate: green\nNot verified: none\n"
        for raw in (b"\xef\xbb\xbf" + body.encode(), body.replace("\n", "\r\n").encode()):
            with self.subTest(raw=raw[:6]):
                report = self.work / "dev-report.md"
                report.write_bytes(raw)
                self.start(report, "developer")
                out = self.fire("SubagentStop", agent_id="a1", agent_type="developer",
                                last_assistant_message="done", agent_transcript_path="")
                self.assertIsNone(out)

    def test_a_non_utf8_report_is_read_not_a_traceback(self):
        report = self.work / "dev-report.md"
        report.write_bytes(b"Scope: caf\xe9\nGate: ok\nNot verified: none\n")
        self.start(report, "developer")
        out = self.fire("SubagentStop", agent_id="a1", agent_type="developer",
                        last_assistant_message="done", agent_transcript_path="")
        self.assertIsNone(out)

    def two_files(self, text_r, text_data):
        r, data = self.work / "r.md", self.work / "data.json"
        r.write_text(text_r, encoding="utf-8")
        data.write_text(text_data, encoding="utf-8")
        brief = self.work / "brief.md"
        brief.write_text(f"# Unit\n\nProduces: {r}, {data}\n", encoding="utf-8")
        self.launch(f"Brief: {brief} Read it and do the work.", agent_type="developer")
        return r, self.fire("SubagentStop", agent_id="a1", agent_type="developer",
                            last_assistant_message="done", agent_transcript_path="")

    def test_any_produces_file_may_hold_the_checklist(self):
        _, out = self.two_files("Scope: a\nGate: g\nNot verified: none\n", "{}\n")
        self.assertIsNone(out)

    def test_a_key_in_no_produces_file_gives_one_problem_naming_the_first(self):
        r, out = self.two_files("Scope: a\n", "{}\n")
        self.assertEqual(out["decision"], "block")
        for key in ("Gate", "Not verified"):
            self.assertEqual(out["reason"].count(f"{r} does not answer the checklist line `{key}`."), 1)
        self.assertNotIn("data.json does not answer", out["reason"])

    def test_an_unreadable_produces_file_is_a_problem(self):
        r = self.work / "dev-report.md"
        r.write_text("Scope: a\nGate: g\nNot verified: n\n", encoding="utf-8")
        self.start(r, "developer")
        with mock.patch.object(Path, "read_text", side_effect=OSError("denied")):
            problems = agent_runs.missing(
                {"t": 0, "produces": [str(r)]}, [], "", False, ["Scope"], TEXTS)
        self.assertEqual(problems, [f"{r} could not be read for the checklist."])

    def test_resolve_carries_the_keys_of_every_role(self):
        Chk, Cfg = Resolve.Chk, Resolve.Cfg
        roles_chk = Chk("warn", {"handback_checks": ["Licences: each new dependency's licence"]})
        for on in (True, False):
            cfg = Cfg({"agent-gates": Chk("warn", {**GATES, "checklist": on}),
                       "agent-roles": roles_chk})
            keys = agent_runs.resolve(cfg, Path("/p"))["gates"]["checklist_keys"]
            with self.subTest(on=on):
                if on:
                    self.assertEqual(keys["code-reviewer"],
                                     ["Licences", "Not verified", "Report"])
                    self.assertIn("Licences", agent_runs.resolve(
                        cfg, Path("/p"))["gates"]["checklist_texts"]["code-reviewer"])
                else:
                    self.assertEqual(keys, {})

    def resolved_keys(self, roles_chk, **gates):
        Chk, Cfg = Resolve.Chk, Resolve.Cfg
        cfg = Cfg({"agent-gates": Chk("warn", {**GATES, "checklist": True}),
                   **({"agent-roles": roles_chk} if roles_chk else {})})
        return agent_runs.resolve(cfg, Path("/p"))["gates"]["checklist_keys"]

    def test_no_keys_when_agent_roles_is_missing_or_off(self):
        self.assertEqual(self.resolved_keys(None), {})
        self.assertEqual(self.resolved_keys(Resolve.Chk("off", {})), {})

    def test_only_rendered_roles_are_checked(self):
        self.assertNotIn("architect", self.resolved_keys(Resolve.Chk("warn", {})))
        self.assertIn("architect", self.resolved_keys(
            Resolve.Chk("warn", {"extra_roles": ["architect"]})))

    def test_a_bad_handback_entry_is_not_demanded(self):
        keys = self.resolved_keys(Resolve.Chk("warn", {"handback_checks": [
            "Scope: mine", ": empty", "Trail : y", "Licences: l"]}))
        self.assertEqual(keys["code-reviewer"], ["Licences", "Not verified", "Report"])


class HeldBrief(HookCase):
    def pending(self):
        return sorted(self.state.glob("pending-*.json"))

    def age(self, path, seconds):
        then = time.time() - seconds
        os.utime(path, (then, then))

    def test_a_stale_pending_is_not_claimed_and_is_removed(self):
        report = self.work / "r.md"
        self.fire("PreToolUse", tool_name="Agent", tool_use_id="t1",
                  tool_input={"prompt": f"Produces: {report}", "subagent_type": "general-purpose"})
        (held,) = self.pending()
        self.age(held, 120)
        self.fire("SubagentStart", agent_id="a1", agent_type="general-purpose")
        self.assertEqual(self.pending(), [])
        self.assertIsNone(self.stop())          # a1 did not inherit the stale contract

    def test_a_stale_pending_of_another_type_is_removed_too(self):
        self.fire("PreToolUse", tool_name="Agent", tool_use_id="t1",
                  tool_input={"prompt": "x", "subagent_type": "reviewer"})
        self.age(self.pending()[0], 120)
        self.fire("PreToolUse", tool_name="Agent", tool_use_id="t2",
                  tool_input={"prompt": "y", "subagent_type": "general-purpose"})
        self.fire("SubagentStart", agent_id="a1", agent_type="general-purpose")
        self.assertEqual(self.pending(), [])

    def test_a_fresh_pending_is_still_claimed(self):
        self.fire("PreToolUse", tool_name="Agent", tool_use_id="t1",
                  tool_input={"prompt": "x", "subagent_type": "general-purpose"})
        self.fire("SubagentStart", agent_id="a1", agent_type="general-purpose")
        self.assertEqual(self.pending(), [])
        self.assertTrue(agent_runs.read_json(self.state / "launch-a1.json")["paired"])

    def test_a_failed_launchs_pending_is_removed(self):
        for tid in ("t1", "t2"):
            self.fire("PreToolUse", tool_name="Agent", tool_use_id=tid,
                      tool_input={"prompt": "x", "subagent_type": "general-purpose"})
            time.sleep(0.01)
        self.assertEqual(len(self.pending()), 2)
        self.fire("PostToolUseFailure", tool_name="Agent", tool_use_id="t1",
                  tool_input={"prompt": "x"}, error="rejected")
        (left,) = self.pending()
        self.assertEqual(agent_runs.read_json(left)["tool_use_id"], "t2")


class RedGate(HookCase):
    def test_no_failing_output_blocks(self):
        self.launch(f"Fix it.\nRed: {self.work / 'red'}")
        self.assertIn("no failing-test output", self.stop()["reason"])

    def test_output_saved_after_the_last_edit_blocks(self):
        red = self.work / "red"
        self.launch(f"Fix it.\nRed: {red}")
        self.tool("Edit", file_path="calc.py")
        time.sleep(0.05)
        red.mkdir()
        (red / "t.txt").write_text("FAILED")
        self.assertIn("after your last edit", self.stop()["reason"])

    def test_output_saved_before_the_edit_passes(self):
        red = self.work / "red"
        self.launch(f"Fix it.\nRed: {red}")
        red.mkdir()
        (red / "t.txt").write_text("FAILED")
        time.sleep(0.05)
        self.tool("Edit", file_path="calc.py")
        self.assertIsNone(self.stop())


class LaunchGate(HookCase):
    def deny(self, prompt, agent_type="developer", **extra):
        out = self.launch(prompt, agent_type=agent_type, **extra)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        return out["hookSpecificOutput"]["permissionDecisionReason"]

    def test_briefed_type_without_a_brief_is_denied(self):
        self.assertIn("has no `Brief:` line", self.deny("Fix the bug."))

    def test_briefed_type_naming_a_missing_brief_is_denied(self):
        self.assertIn("does not exist", self.deny(f"Brief: {self.work / 'nope.md'}"))

    def test_a_relative_brief_that_is_not_there_is_denied_and_says_so(self):
        self.assertIn("`Brief: briefs/one.md`, which does not exist",
                      self.deny("Brief: briefs/one.md", cwd=str(self.work)))

    def _briefed_launch(self, prompt, cwd):
        out = self.launch(prompt, agent_type="developer", cwd=str(cwd))
        self.assertIsNone(out, "the launch was allowed")
        return [agent_runs.read_json(self.state / "launch-a1.json")]

    def test_a_relative_brief_is_found_under_the_session_root_from_another_cwd(self):
        (self.root / "briefs").mkdir()
        (self.root / "briefs" / "one.md").write_text("Do it.", encoding="utf-8")
        rows = self._briefed_launch("Brief: briefs/one.md", self.work)
        self.assertEqual([Path(r["brief"]) for r in rows], [self.root / "briefs" / "one.md"])

    def test_a_relative_brief_only_under_cwd_is_still_found(self):
        (self.work / "briefs").mkdir()
        (self.work / "briefs" / "one.md").write_text("Do it.", encoding="utf-8")
        rows = self._briefed_launch("Brief: briefs/one.md", self.work)
        self.assertEqual([Path(r["brief"]) for r in rows], [self.work / "briefs" / "one.md"])

    def test_a_relative_brief_in_neither_place_is_refused_as_before(self):
        self.assertIn("which does not exist", self.deny("Brief: briefs/one.md", cwd=str(self.work)))

    def test_an_absolute_brief_ignores_the_root(self):
        (self.root / "brief.md").write_text("root", encoding="utf-8")
        mine = self.work / "brief.md"
        mine.write_text("mine", encoding="utf-8")
        rows = self._briefed_launch(f"Brief: {mine}", self.work)
        self.assertEqual([Path(r["brief"]) for r in rows], [mine])

    def test_the_reason_says_exactly_how_to_fix_it(self):
        reason = self.deny("Fix the bug.")
        self.assertIn("`Brief: <path to that file>`", reason)
        self.assertIn("`Produces: <report path>`", reason)

    def test_other_types_launch_freely(self):
        self.assertIsNone(self.launch("Find the file."))

    def test_a_brief_that_exists_launches(self):
        brief = self.work / "brief.md"
        brief.write_text("Do it.")
        self.assertIsNone(self.launch(f"Brief: {brief}", agent_type="developer"))

    def test_briefed_is_a_param(self):
        self.resolve({**GATES, "briefed": ["qa-engineer"]}, MEASURE)
        self.assertIsNone(self.launch("Fix the bug.", agent_type="developer"))
        self.assertIn("has no", self.deny("Test it.", agent_type="qa-engineer"))

    def test_each_denied_launch_is_judged_alone_with_no_counter(self):
        for _ in range(5):
            self.deny("Fix the bug.")

    def test_a_denied_launch_leaves_no_state(self):
        self.deny("Fix the bug.")
        self.assertFalse(self.state.exists())

    def test_with_gates_off_nothing_is_denied(self):
        self.resolve(None, MEASURE)
        self.assertIsNone(self.launch("Fix the bug.", agent_type="developer"))


class UnfilledBrief(HookCase):
    """A brief that still holds a skeleton's instruction line (`<!-- fill: ... -->`) is not
    launched from, whatever the agent's type."""
    SKELETON = ("# p1 / u1 — A unit\n\nBase commit: abc\n\n"
                "## Task\n\n<!-- fill: what this unit must do -->\n\n"
                "## Files\n\nengine/a.py\n\n"
                "## Tests\n\n<!-- fill: the tests that prove it -->\n\n"
                "Produces: developer-report.md\n")

    def brief(self, text: str | bytes, name: str = "brief.md") -> Path:
        path = self.work / name
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(text)
        return path

    def reason(self, text, agent_type="developer") -> str:
        out = self.launch(f"Brief: {self.brief(text)}", agent_type=agent_type)
        self.assertIsNotNone(out, "the launch was allowed")
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        return out["hookSpecificOutput"]["permissionDecisionReason"]

    def allowed(self, text, agent_type="developer") -> None:
        self.assertIsNone(self.launch(f"Brief: {self.brief(text)}", agent_type=agent_type))

    def test_a_brief_with_an_instruction_line_left_is_denied_naming_its_headings(self):
        reason = self.reason(self.SKELETON)
        self.assertIn("the brief is not filled in: Task, Tests.", reason)
        self.assertIn(str(self.work / "brief.md"), reason)
        self.assertIn("`<!-- fill:`", reason)

    def test_a_filled_brief_launches(self):
        self.allowed(self.SKELETON.replace("<!-- fill: what this unit must do -->", "Do it.")
                     .replace("<!-- fill: the tests that prove it -->", "test_a"))

    def test_a_heading_is_named_once_however_many_lines_it_holds(self):
        text = "## Task\n\n<!-- fill: one -->\n<!-- fill: two -->\n"
        self.assertIn("not filled in: Task.", self.reason(text))

    def test_a_line_above_the_first_heading_is_named_by_its_number(self):
        self.assertIn("not filled in: line 3.", self.reason("Intro.\n\n<!-- fill: x -->\n"))

    def test_a_quotation_of_the_marker_is_not_an_instruction(self):
        self.allowed("## Task\n\nWrite the skeleton:\n\n```md\n  <!-- fill: x -->\n```\n"
                     "\n    <!-- fill: indented code -->\n\n     ```\n    <!-- fill: y -->\n"
                     "\n  <!-- fill: x --> and more\n<!-- fill: unclosed\n")

    def test_a_skeleton_line_after_an_unclosed_fence_is_refused(self):
        self.assertIn("not filled in: Files.",
                      self.reason("## Task\n\n```\ncode\n\n## Files\n\n<!-- fill: x -->\n"))

    def test_a_brief_over_a_megabyte_or_not_utf8_is_not_refused_by_the_gate(self):
        self.allowed(b"## Task\n\n<!-- fill: x -->\n" + b"a" * (1024 * 1024))
        self.allowed(b"## Task\n\n<!-- fill: x -->\n\xff\xfe")

    def test_a_line_after_a_closed_fence_still_counts(self):
        text = "## Task\n\n```\ncode\n```\n\n## Files\n\n<!-- fill: x -->\n"
        self.assertIn("not filled in: Files.", self.reason(text))

    def test_a_mention_inside_a_sentence_is_not_an_instruction(self):
        self.allowed("## Task\n\nReplace each `<!-- fill:` line the skeleton holds.\n")

    def test_crlf_and_a_byte_order_mark_change_nothing(self):
        raw = b"\xef\xbb\xbf" + "<!-- fill: x -->\n## Task\n\n<!-- fill: y -->\n".replace(
            "\n", "\r\n").encode("utf-8")
        self.assertIn("not filled in: line 1, Task.", self.reason(raw))

    def test_any_agent_launched_from_a_brief_is_held_to_it(self):
        for agent_type in ("qa-engineer", "code-reviewer", "general-purpose"):
            with self.subTest(agent_type=agent_type):
                self.assertIn("not filled in: Task, Tests.",
                              self.reason(self.SKELETON, agent_type=agent_type))

    def test_a_launch_that_names_no_brief_is_not_held_to_one(self):
        self.brief(self.SKELETON)
        self.assertIsNone(self.launch("Find the file. <!-- fill: x -->"))

    def test_a_brief_that_is_not_utf8_is_let_through_without_a_traceback(self):
        self.allowed(b"## Task\n\n<!-- fill: \xff\xfe -->\n")

    def test_a_denied_launch_leaves_no_state_and_is_judged_alone(self):
        for _ in range(3):
            self.reason(self.SKELETON)
        self.assertFalse(self.state.exists())

    def test_with_gates_off_nothing_is_denied(self):
        self.resolve(None, MEASURE)
        self.allowed(self.SKELETON)


class WorktreeContracts(HookCase):
    """An agent launched in a worktree of its own writes only inside it: a relative `Brief:` is
    read where the session launches, and the contract's paths resolve where the agent starts."""
    BRIEF = ".context-gate/build/p/u1/brief.md"
    REPORT = ".context-gate/build/p/u1/developer-report.md"

    def setUp(self):
        super().setUp()
        self.proj = self.tmp / "proj"
        (self.proj / ".context-gate/build/p/u1").mkdir(parents=True)
        (self.proj / self.BRIEF).write_text(f"Do it.\nProduces: {self.REPORT}\n",
                                            encoding="utf-8")
        self.tree = self.proj / ".claude/worktrees/a1"
        (self.tree / ".context-gate/build/p/u1").mkdir(parents=True)

    def go(self, prompt=None):
        return self.launch(prompt or f"Brief: {self.BRIEF}", agent_type="developer",
                           cwd=str(self.proj), start_cwd=str(self.tree))

    def test_a_relative_brief_is_read_where_the_session_launches(self):
        self.assertIsNone(self.go())
        launch = agent_runs.read_json(self.state / "launch-a1.json")
        self.assertEqual(launch["cwd"], str(self.tree))
        self.assertTrue(launch["paired"])

    def test_a_relative_brief_still_to_fill_is_refused_by_name(self):
        (self.proj / self.BRIEF).write_text("## Task\n\n<!-- fill: what -->\n", encoding="utf-8")
        out = self.go()
        self.assertIn("not filled in: Task", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_a_report_only_in_the_main_checkout_does_not_satisfy_the_gate(self):
        self.go()
        (self.proj / self.REPORT).write_text("done\n", encoding="utf-8")
        out = self.stop()
        self.assertEqual(out["decision"], "block")
        self.assertIn(f"{self.tree / self.REPORT} does not exist", out["reason"])

    def test_a_report_in_the_agents_own_tree_satisfies_the_gate(self):
        self.go()
        (self.tree / self.REPORT).write_text("done\n", encoding="utf-8")
        self.assertIsNone(self.stop())

    def test_an_absolute_brief_names_the_root_based_path_in_the_refusal(self):
        brief = self.proj / self.BRIEF
        self.go(f"Brief: {brief}")
        (self.tree / self.REPORT).unlink(missing_ok=True)
        self.assertIn(f"{self.tree / self.REPORT} does not exist", self.stop()["reason"])

    def test_a_path_written_beside_the_brief_is_still_met_in_the_briefs_folder(self):
        (self.proj / self.BRIEF).write_text("Do it.\nProduces: developer-report.md\n",
                                            encoding="utf-8")
        self.go()
        (self.tree / self.REPORT).write_text("done\n", encoding="utf-8")
        self.assertIsNone(self.stop())

    def test_an_agent_with_no_worktree_is_looked_for_from_the_launching_sessions_root(self):
        self.launch(f"Brief: {self.BRIEF}", agent_type="developer", cwd=str(self.proj))
        out = self.stop()
        self.assertIn(f"{self.proj / self.REPORT} does not exist", out["reason"])
        (self.proj / self.REPORT).write_text("done\n", encoding="utf-8")
        self.assertIsNone(self.stop())

    def test_a_path_with_dotdot_is_not_looked_for_outside_the_tree(self):
        (self.proj / self.BRIEF).write_text("Do it.\nProduces: ../outside.md\n",
                                            encoding="utf-8")
        self.go()
        (self.tree.parent / "outside.md").write_text("done\n", encoding="utf-8")
        self.assertEqual(self.stop()["decision"], "block")

    def test_the_refusal_names_the_full_path_and_stays_inside_the_agents_tree(self):
        self.go()
        reason = self.stop()["reason"]
        self.assertIn("inside your own working tree", reason)
        self.assertNotIn("absolute", reason)


class ReportLine(HookCase):
    """The coordinator's line for a run whose contract was met names each file produced."""

    def finish(self):
        report = self.work / "dev-report.md"
        self.launch(f"Produces: {report}")
        report.write_text("done\n", encoding="utf-8")
        self.assertIsNone(self.stop(transcript=self.transcript([("m1", USAGE, "done")])))
        return report

    def test_the_measured_line_ends_with_each_report(self):
        report = self.finish()
        self.assertTrue(self.notice().endswith(f" Report: {report}"), self.notice())

    def test_with_measurement_off_a_met_contract_gets_a_short_line(self):
        self.resolve(GATES, None)
        report = self.finish()
        self.assertEqual(self.notice(), f"[agent run] general-purpose: Report: {report}")

    def test_with_measurement_off_and_no_contract_there_is_no_line(self):
        self.resolve(GATES, None)
        self.launch("Run date.")
        self.stop()
        self.assertIsNone(self.notice())

    def test_an_unmet_contract_names_no_report(self):
        self.launch(f"Produces: {self.work / 'r.md'}")
        for _ in range(3):
            self.stop(transcript=self.transcript([("m1", USAGE, "done")]))
        self.assertNotIn("Report:", self.notice())


class BriefPaths(unittest.TestCase):
    """`Brief:` must be absolute on the platform that runs the hook: judged here with both
    platforms' path rules, on whatever host runs the tests."""

    def test_a_drive_path_is_absolute_on_windows_only(self):
        prompt = "Brief: C:\\work\\brief.md"
        self.assertEqual(str(agent_runs.brief_path(prompt, PureWindowsPath)), "C:\\work\\brief.md")
        self.assertIsNone(agent_runs.brief_path(prompt, PurePosixPath))

    def test_a_leading_slash_is_absolute_on_posix_only(self):
        prompt = "Brief: /work/brief.md"
        self.assertEqual(agent_runs.brief_path(prompt, PurePosixPath).as_posix(),
                         "/work/brief.md")
        self.assertIsNone(agent_runs.brief_path(prompt, PureWindowsPath))

    def test_a_relative_path_is_never_absolute(self):
        for flavor in (PurePosixPath, PureWindowsPath):
            self.assertIsNone(agent_runs.brief_path("Brief: briefs\\one.md", flavor))
            self.assertIsNone(agent_runs.brief_path("Brief: ./one.md", flavor))

    def test_quotes_and_trailing_punctuation_are_not_part_of_the_path(self):
        self.assertEqual(agent_runs.brief_path("see Brief: `/w/b.md`.", PurePosixPath).as_posix(),
                         "/w/b.md")

    def test_a_quoted_path_may_hold_spaces(self):
        for prompt in ('Brief: "/a b/brief.md" Read it.', "Brief: `/a b/brief.md` Read it."):
            self.assertEqual(agent_runs.brief_ref(prompt), "/a b/brief.md")

    def test_an_unquoted_path_with_a_space_is_found_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            brief = Path(tmp) / "a b" / "brief.md"
            brief.parent.mkdir()
            brief.write_text("Do it.")
            for tail in ("", " Read it and do the work.", "."):
                with self.subTest(tail=tail):
                    self.assertEqual(agent_runs.brief_ref(f"Do it.\nBrief: {brief}{tail}"),
                                     str(brief))

    def test_a_missing_brief_is_reported_as_the_rest_of_the_line(self):
        self.assertEqual(agent_runs.brief_ref("Brief: /a b/brief.md Read it and do the work."),
                         "/a b/brief.md Read it and do the work")
        self.assertEqual(agent_runs.brief_ref("Brief: C:\\Users\\John Smith\\b.md\nNext line"),
                         "C:\\Users\\John Smith\\b.md")
        self.assertEqual(str(agent_runs.brief_path("Brief: C:\\Users\\John Smith\\b.md",
                                                   PureWindowsPath)),
                         "C:\\Users\\John Smith\\b.md")

    def test_a_claim_may_name_a_drive_path(self):
        found = agent_runs.CLAIM.findall("Done.\nFull report: C:\\work\\unit\\report.md")
        self.assertEqual(found, ["C:\\work\\unit\\report.md"])


class PushGate(unittest.TestCase):
    DENIED = [
        "git push",
        "git push origin main",
        "git -C x push",
        "git -C /some/dir -c user.name=x push --force",
        "git --git-dir=.git --work-tree=. push",
        "git --git-dir .git push",
        "cd x && git push origin b",
        "git add . ; git commit -m done; git push",
        "make test || git push",
        "echo done | git push",
        "git status\ngit push",
        "git add .\\\n&& git push",
        "FOO=1 git push",
        "(cd x && git push)",
        "cat <<EOF\nnotes\nEOF\ngit push",
        "git commit -m 'x' && git push -u origin b # publish",
        "if x; then git push; fi",
        "if git push; then echo ok; fi",
        "if x; then y; else git push; fi",
        "while true; do git push; done",
        "until git push; do sleep 1; done",
        "{ git push; }",
        "! git push",
        "time git push",
        "exec git push",
        "command git push",
        "nohup git push",
        "env git push",
        "env FOO=1 BAR=2 git push",
        "time ! FOO=1 git push",
    ]
    ALLOWED = [
        "git status",
        'git commit -m "push later"',
        "git commit -m 'git push now'",
        "echo git push",
        'echo "cd x && git push"',
        "git log --grep push",
        "git stash push",
        "git stash push -m wip",
        "git -C x stash push",
        "cat > notes.md <<'EOF'\ngit push\nEOF",
        "cat > notes.md <<EOF\nfirst\ngit push origin b\nEOF\ngit status",
        "cat <<-EOF\n\tgit push\n\tEOF\nls",
        'git commit -m "$(cat <<\'EOF\'\nwhy we push\nEOF\n)"',
        "git push-all",
        "git pull && make push",
        "ls  # git push",
        "if x; then echo git push; fi",
        "time git status",
        "env FOO=1 git stash push",
        "{ git commit -m push; }",
        "",
    ]

    def test_pushes_are_denied(self):
        for command in self.DENIED:
            with self.subTest(command=command):
                self.assertTrue(agent_runs.pushes(command))

    def test_everything_else_is_allowed(self):
        for command in self.ALLOWED:
            with self.subTest(command=command):
                self.assertFalse(agent_runs.pushes(command))

    def test_an_unparseable_command_is_allowed(self):
        for command in ('git push "origin', "git push 'a", 'echo "unterminated; git push'):
            with self.subTest(command=command):
                self.assertFalse(agent_runs.pushes(command))

    def test_text_that_is_not_a_command_is_allowed(self):
        for value in (None, 7):
            self.assertFalse(agent_runs.pushes(value))


class PushGateHook(HookCase):
    def bash(self, command, **extra):
        return self.fire("PreToolUse", tool_name="Bash", tool_input={"command": command}, **extra)

    def test_a_subagents_push_is_denied_with_what_to_do(self):
        out = self.bash("git -C x push", agent_id="a1", agent_type="developer")
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("ready to push", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_the_coordinators_push_is_never_denied(self):
        self.assertIsNone(self.bash("git push"))

    def test_a_subagents_other_commands_pass(self):
        self.assertIsNone(self.bash('git commit -m "push later"', agent_id="a1"))

    def test_deny_push_off_allows_it(self):
        self.resolve({**GATES, "deny_push": False}, MEASURE)
        self.assertIsNone(self.bash("git push", agent_id="a1"))

    def test_with_gates_off_a_push_is_allowed(self):
        self.resolve(None, MEASURE)
        self.assertIsNone(self.bash("git push", agent_id="a1"))


if __name__ == "__main__":
    unittest.main()
