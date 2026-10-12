"""Agent gates and measurement: synthetic hook input through the plugin's hook, no model involved.

    python3 -m unittest discover -s engine/tests -k agent_runs
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path, PurePosixPath, PureWindowsPath
from unittest import mock

ENGINE = Path(__file__).resolve().parent.parent
PLUGIN = ENGINE.parent / "plugin"
sys.path.insert(0, str(ENGINE))

from govern import agent_runs, layout  # noqa: E402
from test_options import repo  # noqa: E402

BUILT = None            # the plugin as install-plugin assembles it: plugin/ plus the engine
_BUILD = None
GATES = {"briefed": ["developer", "senior-developer"], "claims": True, "deny_push": True,
         "max_refusals": 2}
MEASURE = {"notice": True}
USAGE = {"input_tokens": 2, "cache_creation_input_tokens": 100,
         "cache_read_input_tokens": 1000, "output_tokens": 10}


def setUpModule():
    global BUILT, _BUILD
    _BUILD = tempfile.TemporaryDirectory()
    BUILT = Path(_BUILD.name) / "built"
    shutil.copytree(PLUGIN, BUILT, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(ENGINE / "govern", BUILT / "govern",
                    ignore=shutil.ignore_patterns("__pycache__"))


def tearDownModule():
    _BUILD.cleanup()


class HookCase(unittest.TestCase):
    """One session ("s1") with both options on, driven through hooks/agents.py."""
    gates = GATES
    measure = MEASURE

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.root = self.tmp / "project"
        self.root.mkdir()
        self.state = agent_runs.agents_dir(self.home) / "s1"
        self.env = {**os.environ, "HOME": str(self.home)}
        self.resolve(self.gates, self.measure)

    def tearDown(self):
        self._tmp.cleanup()

    def resolve(self, gates, measure):
        agent_runs.write_json(agent_runs.resolved_path(self.home, "s1"),
                              {"gates": gates, "measure": measure, "root": str(self.root)})

    def run_hook(self, stdin: bytes, env=None):
        return subprocess.run([sys.executable, str(BUILT / "hooks" / "agents.py")], input=stdin,
                              capture_output=True, env=env or self.env, timeout=20)

    def fire(self, event, **fields):
        data = {"session_id": "s1", "hook_event_name": event, **fields}
        done = self.run_hook(json.dumps(data).encode())
        self.assertEqual(done.returncode, 0)
        self.assertEqual(done.stderr, b"")
        return json.loads(done.stdout) if done.stdout.strip() else None

    def launch(self, prompt, agent="a1", agent_type="general-purpose", start_cwd=None, **extra):
        """`extra` goes to PreToolUse (its `cwd` is the launching session's); `start_cwd` is the
        folder SubagentStart reports the agent started in."""
        out = self.fire("PreToolUse", tool_name="Agent", tool_use_id="t1",
                        tool_input={"prompt": prompt, "subagent_type": agent_type}, **extra)
        if out is None:
            self.fire("SubagentStart", agent_id=agent, agent_type=agent_type,
                      **({"cwd": start_cwd} if start_cwd else {}))
        return out

    def notice(self, agent="a1"):
        out = self.fire("UserPromptSubmit", prompt="<task-notification>\n<task-id>"
                        f"{agent}</task-id></task-notification>")
        return out["hookSpecificOutput"]["additionalContext"] if out else None

    def stop(self, agent="a1", final="done", transcript=""):
        return self.fire("SubagentStop", agent_id=agent, agent_type="general-purpose",
                         last_assistant_message=final, agent_transcript_path=transcript)

    def tool(self, name, agent="a1", **inp):
        self.fire("PostToolUse", agent_id=agent, agent_type="general-purpose", tool_name=name,
                  tool_input=inp, tool_response={"ok": True}, duration_ms=5)

    def rows(self):
        return agent_runs.read_json_lines(self.state / "runs.jsonl")

    def durable(self):
        return agent_runs.read_json_lines(agent_runs.durable_path(self.home, self.root))

    def transcript(self, requests):
        """A transcript with one assistant request per (id, usage, text) given."""
        path = self.tmp / "agent.jsonl"
        lines = [{"type": "user", "message": {"content": "the brief"}}]
        for mid, usage, text in requests:
            lines.append({"type": "assistant", "message": {
                "id": mid, "usage": usage, "content": [{"type": "text", "text": text}]}})
        path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
        return str(path)


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
        self.assertEqual(len(self.durable()), 1)


TEXTS = {"Scope": "the files you changed. Each is a file the brief names.",
         "Gate": "the full gate's result line.", "Not verified": "each claim that rests on reading."}


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


class Rows(HookCase):
    def test_tokens_are_summed_over_requests_not_taken_from_the_last(self):
        self.launch("Run date.")
        path = self.transcript([("m1", USAGE, "one"), ("m2", USAGE, "done")])
        self.stop(transcript=path)
        tok = self.rows()[-1]["tokens"]
        self.assertEqual((tok["requests"], tok["cache_read_input_tokens"]), (2, 2000))
        self.assertEqual(tok["context_at_end"], 1112)

    def test_a_request_written_as_several_lines_counts_once(self):
        self.launch("Run date.")
        path = self.transcript([("m1", USAGE, "thinking"), ("m1", USAGE, "done")])
        self.stop(transcript=path)
        self.assertEqual(self.rows()[-1]["tokens"]["requests"], 1)

    def test_a_resumed_run_reports_only_its_own_tokens_and_calls(self):
        self.launch("Run date.")
        self.tool("Bash", command="date")
        self.stop(transcript=self.transcript([("m1", USAGE, "done")]))
        self.fire("PreToolUse", tool_name="SendMessage", tool_input={"to": "a1", "message": "more"})
        self.fire("SubagentStart", agent_id="a1", agent_type="general-purpose")
        self.tool("Bash", command="whoami")
        self.stop(transcript=self.transcript([("m1", USAGE, "one"), ("m2", USAGE, "done")]))
        first, second = self.rows()
        self.assertEqual((first["resumed"], second["resumed"]), (False, True))
        self.assertEqual((second["tokens"]["requests"], second["tool_calls"]), (1, 1))
        self.assertEqual(second["tokens"]["cache_read_input_tokens"], 1000)

    def test_a_resume_is_gated_on_its_own_message_not_the_first_brief(self):
        first, second = self.work / "one.md", self.work / "two.md"
        self.launch(f"Produces: {first}")
        first.write_text("x")
        self.assertIsNone(self.stop())
        self.fire("PreToolUse", tool_name="SendMessage",
                  tool_input={"to": "a1", "message": f"Fix pass.\nProduces: {second}"})
        self.fire("SubagentStart", agent_id="a1", agent_type="general-purpose")
        reason = self.stop()["reason"]
        self.assertIn(str(second), reason)
        self.assertNotIn(str(first), reason)

    def test_an_unreadable_transcript_gives_no_figure_and_still_a_row(self):
        self.launch("Run date.")
        bad = self.tmp / "bad.jsonl"
        bad.write_text("not json\n")
        self.stop(transcript=str(bad))
        self.assertIsNone(self.rows()[-1]["tokens"])

    def test_the_coordinator_gets_one_line_with_the_completion_notice(self):
        self.launch(f"Produces: {self.work / 'r.md'}")
        for _ in range(3):
            self.stop(transcript=self.transcript([("m1", USAGE, "done")]))
        out = self.fire("UserPromptSubmit",
                        prompt="<task-notification>\n<task-id>a1</task-id></task-notification>")
        line = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("1,000 cached re-read", line)
        self.assertIn("CONTRACT NOT MET after 2 refusals", line)

    def test_garbage_on_stdin_is_silent(self):
        done = self.run_hook(b"{not json")
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, b"", b""))

    def test_per_tool_totals_the_three_largest_results_and_repeated_commands(self):
        self.launch("Run date.")
        for size, name in ((10, "a"), (3000, "b"), (200, "c"), (90000, "d"), (50, "e")):
            self.fire("PostToolUse", agent_id="a1", agent_type="general-purpose",
                      tool_name="Read", tool_input={"file_path": f"/w/{name}"},
                      tool_response="x" * size, duration_ms=7)
        for _ in range(3):
            self.tool("Bash", command="make test")
        self.tool("Bash", command="date")
        self.fire("PostToolUseFailure", agent_id="a1", agent_type="general-purpose",
                  tool_name="Bash", tool_input={"command": "false"}, error="exit 1", duration_ms=1)
        self.stop()
        row = self.rows()[-1]
        self.assertEqual(row["by_tool"]["Read"], {"calls": 5, "ms": 35, "bytes": row["by_tool"]["Read"]["bytes"], "failed": 0})
        self.assertEqual((row["by_tool"]["Bash"]["calls"], row["by_tool"]["Bash"]["failed"]), (5, 1))
        self.assertEqual([x["label"] for x in row["largest"]], ["/w/d", "/w/b", "/w/c"])
        self.assertEqual(row["repeated"], [{"command": "make test", "n": 3}])

    def test_status_and_findings_come_from_the_final_message(self):
        self.launch("Review it.")
        self.stop(final="Status: DONE_WITH_CONCERNS\nSummary.\nFindings: critical=1 important=2 minor=3")
        row = self.rows()[-1]
        self.assertEqual(row["status"], "DONE_WITH_CONCERNS")
        self.assertEqual(row["findings"], {"critical": 1, "important": 2, "minor": 3})

    def test_a_final_message_without_them_has_neither(self):
        self.launch("Review it.")
        self.stop(final="All good. Status: DONE is only counted on the first line.")
        row = self.rows()[-1]
        self.assertEqual((row["status"], row["findings"]), (None, None))

    def test_the_row_also_goes_to_the_durable_log_with_session_and_phase(self):
        agent_runs.mark(self.home, self.root, "phase 1")
        self.launch("Run date.")
        self.stop()
        (row,) = [r for r in self.durable() if "agent" in r]
        self.assertEqual((row["session"], row["phase"], row["agent"]), ("s1", "phase 1", "a1"))

    def test_a_foreground_runs_model_is_filled_in_after_the_fact(self):
        self.launch("Run date.")
        self.stop()
        out = self.fire("PostToolUse", tool_name="Agent", tool_input={},
                        tool_response={"agentId": "a1", "resolvedModel": "haiku",
                                       "status": "completed"})
        self.assertEqual(self.rows()[-1]["model"], "haiku")
        self.assertIn("(haiku)", out["hookSpecificOutput"]["additionalContext"])
        runs, _ = agent_runs.load_runs(self.home, self.root, True)
        self.assertEqual(runs[-1]["model"], "haiku")


class Quiet(HookCase):
    """Every hook path stays silent, exit 0, and writes nothing to stdout or stderr when its
    input or its state is wrong."""
    EVENTS = [
        ("PreToolUse", {"tool_name": "Agent", "tool_input": {"prompt": "x"}}),
        ("PreToolUse", {"tool_name": "SendMessage", "tool_input": {"to": "a1", "message": "m"}}),
        ("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "ls"}, "agent_id": "a1"}),
        ("PostToolUse", {"tool_name": "Agent", "tool_response": {"agentId": "a1",
                                                                 "status": "completed"}}),
        ("PostToolUse", {"tool_name": "Bash", "agent_id": "a1", "tool_input": {}}),
        ("PostToolUseFailure", {"tool_name": "Bash", "agent_id": "a1", "error": "x"}),
        ("SubagentStart", {"agent_id": "a1", "agent_type": "developer"}),
        ("SubagentStop", {"agent_id": "a1", "agent_type": "developer",
                          "agent_transcript_path": "/no/such/transcript.jsonl"}),
        ("UserPromptSubmit", {"prompt": "<task-notification><task-id>a1</task-id>"}),
    ]

    def assert_silent(self, stdin: bytes, env=None):
        done = self.run_hook(stdin, env)
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, b"", b""), stdin)

    def test_garbage_or_empty_stdin(self):
        for stdin in (b"", b"   ", b"{not json", b"[]", b"null", b'"text"', b"7", b"\xff\xfe",
                      b'{"session_id": 7}', b'{"session_id": "../x", "hook_event_name": "Stop"}',
                      b'{"session_id": "s1"}', b'{"session_id": "s1", "hook_event_name": 5}'):
            with self.subTest(stdin=stdin):
                self.assert_silent(stdin)

    def test_garbage_fields_in_every_event(self):
        for event, fields in self.EVENTS:
            for bad in (None, 5, [], "x", {"a": 1}):
                with self.subTest(event=event, bad=bad):
                    data = {"session_id": "s1", "hook_event_name": event,
                            **{k: bad for k in fields}}
                    self.assert_silent(json.dumps(data).encode())

    def test_a_missing_resolved_file_does_nothing_at_all(self):
        agent_runs.resolved_path(self.home, "s1").unlink()
        for event, fields in self.EVENTS:
            with self.subTest(event=event):
                self.assert_silent(json.dumps(
                    {"session_id": "s1", "hook_event_name": event, **fields}).encode())
        self.assertEqual(list(agent_runs.agents_dir(self.home).iterdir()), [])

    def test_an_unreadable_resolved_file(self):
        agent_runs.resolved_path(self.home, "s1").write_text("{not json", encoding="utf-8")
        self.assert_silent(json.dumps({"session_id": "s1", "hook_event_name": "SubagentStop",
                                       "agent_id": "a1"}).encode())

    def test_an_unreadable_transcript(self):
        binary = self.tmp / "binary.jsonl"
        binary.write_bytes(b"\xff\xfe\x00{")
        for n, path in enumerate((str(self.tmp), str(self.tmp / "missing.jsonl"), "", None, 7,
                                  str(binary))):
            with self.subTest(path=path):
                self.launch(f"Produces: {self.work / 'r.md'}", agent=f"a{n}")
                self.assertEqual(self.stop(agent=f"a{n}", transcript=path)["decision"], "block")
                self.assertEqual(self.stop(agent=f"a{n}", transcript=path)["decision"], "block")
                self.assertIsNone(self.stop(agent=f"a{n}", transcript=path))

    def test_an_unwritable_state_dir(self):
        self.state.parent.mkdir(parents=True, exist_ok=True)
        self.state.write_text("a file where the session's folder should be", encoding="utf-8")
        for event, fields in self.EVENTS[3:]:
            with self.subTest(event=event):
                self.assert_silent(json.dumps(
                    {"session_id": "s1", "hook_event_name": event, **fields}).encode())
        self.assert_silent(json.dumps({"session_id": "s1", "hook_event_name": "PreToolUse",
                                       "tool_name": "Agent", "tool_input": {
                                           "prompt": "x"}}).encode())

    def test_an_unwritable_home(self):
        self.assert_silent(json.dumps({"session_id": "s1", "hook_event_name": "SubagentStart",
                                       "agent_id": "a1"}).encode(),
                           {**self.env, "HOME": str(self.tmp / "no" / "such" / "home")})


class SubagentsGetNoMeasurement(HookCase):
    """The only text a subagent can get from this code is a gate's refusal."""
    NOTICE = "<task-notification>\n<task-id>a1</task-id></task-notification>"

    def finished_run(self):
        self.launch("Run date.")
        self.stop(transcript=self.transcript([("m1", USAGE, "done")]))

    def test_the_coordinator_is_told_and_a_subagent_is_not(self):
        self.finished_run()
        done = {"tool_name": "Agent", "tool_input": {}, "tool_response": {
            "agentId": "a1", "resolvedModel": "haiku", "status": "completed"}}
        for event, fields in (("UserPromptSubmit", {"prompt": self.NOTICE}),
                              ("PostToolUse", done)):
            with self.subTest(event=event, who="coordinator"):
                out = agent_runs.handle(self.home, self.resolved(),
                                        {"session_id": "s1", "hook_event_name": event, **fields})
                self.assertIn("[agent run]", out["hookSpecificOutput"]["additionalContext"])
            with self.subTest(event=event, who="subagent"):
                self.assertIsNone(agent_runs.handle(
                    self.home, self.resolved(), {"session_id": "s1", "hook_event_name": event,
                                                 "agent_id": "a9", "agent_type": "developer",
                                                 **fields}))

    def resolved(self):
        return agent_runs.read_json(agent_runs.resolved_path(self.home, "s1"))

    def test_nothing_a_subagent_triggers_prints_anything_but_a_refusal(self):
        brief = self.work / "brief.md"
        brief.write_text(f"Produces: {self.work / 'r.md'}")
        outputs = [
            self.launch(f"Brief: {brief}", agent_type="developer"),
            self.fire("PostToolUse", agent_id="a1", agent_type="developer", tool_name="Read",
                      tool_input={"file_path": "/w/x"}, tool_response="x", duration_ms=1),
            self.fire("PostToolUseFailure", agent_id="a1", agent_type="developer",
                      tool_name="Bash", tool_input={}, error="x"),
            self.fire("PreToolUse", agent_id="a1", tool_name="Bash", tool_input={"command": "ls"}),
            self.fire("UserPromptSubmit", agent_id="a1", prompt=self.NOTICE),
        ]
        self.assertEqual(outputs, [None] * 5)
        stops = [self.stop(), self.stop(), self.stop()]
        self.assertEqual([s and s["decision"] for s in stops], ["block", "block", None])


class GatesOnly(HookCase):
    measure = None

    def test_nothing_is_measured_but_launch_records_and_the_tool_log_are_kept(self):
        self.launch("Run date.")
        self.tool("Bash", command="date")
        self.stop(transcript=self.transcript([("m1", USAGE, "done")]))
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.durable(), [])
        self.assertTrue((self.state / "launch-a1.json").is_file())
        self.assertEqual(len(agent_runs.agent_tools(self.state, "a1")), 1)

    def test_the_gates_still_refuse(self):
        self.launch(f"Produces: {self.work / 'r.md'}")
        self.assertEqual(self.stop()["decision"], "block")

    def test_the_coordinator_is_told_only_of_a_contract_that_ended_unmet(self):
        notice = "<task-notification><task-id>a1</task-id></task-notification>"
        self.launch("Run date.")
        self.stop()
        self.assertIsNone(self.fire("UserPromptSubmit", prompt=notice))
        self.launch(f"Produces: {self.work / 'r.md'}", agent="a2")
        for _ in range(3):
            self.stop(agent="a2")
        line = self.fire("UserPromptSubmit", prompt=notice.replace("a1", "a2"))
        self.assertIn("CONTRACT NOT MET after 2 refusals",
                      line["hookSpecificOutput"]["additionalContext"])
        self.assertNotIn("tokens", line["hookSpecificOutput"]["additionalContext"])

    def test_the_block_says_the_run_is_not_finished_and_asks_for_the_report_line(self):
        report = self.work / "r.md"
        self.launch(f"Produces: {report}")
        reason = self.stop()["reason"]
        self.assertTrue(reason.startswith(
            "Your run is not finished and nothing you sent has been accepted."))
        self.assertIn("Your brief's contract is not met:", reason)
        self.assertIn(str(report), reason)
        flat = " ".join(reason.split())
        self.assertIn("even if you have already sent a summary", flat)
        self.assertIn("write a missing file at exactly that path", flat)
        self.assertIn("with a Bash heredoc", flat)
        self.assertIn("`Report: <full path>`", flat)

    def test_a_foreground_agents_unmet_contract_rides_on_the_agent_call(self):
        self.launch(f"Produces: {self.work / 'r.md'}")
        for _ in range(3):
            self.stop()
        out = self.fire("PostToolUse", tool_name="Agent", tool_input={},
                        tool_response={"agentId": "a1", "status": "completed"})
        self.assertIn("CONTRACT NOT MET", out["hookSpecificOutput"]["additionalContext"])


class MeasureOnly(HookCase):
    gates = None

    def test_nothing_is_refused_and_the_row_is_written(self):
        self.launch("Fix the bug.", agent_type="developer")
        report = self.work / "r.md"
        self.launch(f"Produces: {report}", agent="a2")
        self.assertIsNone(self.stop(agent="a2"))
        row = self.rows()[-1]
        self.assertEqual((row["gate_blocks"], row["gate_unmet"]), (0, []))

    def test_the_routine_line_reaches_the_coordinator(self):
        self.launch("Run date.")
        self.stop(transcript=self.transcript([("m1", USAGE, "done")]))
        out = self.fire("UserPromptSubmit",
                        prompt="<task-notification><task-id>a1</task-id></task-notification>")
        self.assertIn("1,000 cached re-read", out["hookSpecificOutput"]["additionalContext"])

    def test_notice_off_sends_no_line(self):
        self.resolve(None, {**MEASURE, "notice": False})
        self.launch("Run date.")
        self.stop()
        self.assertEqual(len(self.rows()), 1)
        self.assertIsNone(self.fire(
            "UserPromptSubmit",
            prompt="<task-notification><task-id>a1</task-id></task-notification>"))

    def test_a_push_is_not_denied(self):
        self.assertIsNone(self.fire("PreToolUse", tool_name="Bash", agent_id="a1",
                                    tool_input={"command": "git push"}))


class BothOff(HookCase):
    def test_a_resolved_file_with_neither_option_does_nothing(self):
        self.resolve(None, None)
        self.assertIsNone(self.launch("Fix the bug.", agent_type="developer"))
        self.tool("Bash", command="date")
        self.assertFalse(self.state.exists())


class CoordinatorGuard(unittest.TestCase):
    """The coordinator's own tool calls are not logged: judged on the logic, where a handler that
    raised on a missing key would hide the guard's absence."""

    def test_a_call_without_an_agent_id_writes_nothing_not_even_the_folder(self):
        with tempfile.TemporaryDirectory() as home:
            home = Path(home)
            data = {"session_id": "s1", "hook_event_name": "PostToolUse", "tool_name": "Bash",
                    "tool_input": {"command": "ls"}, "tool_response": {}, "duration_ms": 3}
            self.assertIsNone(agent_runs.handle(
                home, {"gates": None, "measure": MEASURE, "root": str(home)}, data))
            self.assertFalse(agent_runs.agents_dir(home).exists())

    def test_a_call_with_an_agent_id_is_logged_under_that_agent(self):
        with tempfile.TemporaryDirectory() as home:
            home = Path(home)
            data = {"session_id": "s1", "hook_event_name": "PostToolUse", "tool_name": "Bash",
                    "tool_input": {"command": "ls"}, "tool_response": {}, "duration_ms": 3,
                    "agent_id": "a1", "agent_type": "developer"}
            agent_runs.handle(home, {"gates": None, "measure": MEASURE, "root": str(home)}, data)
            (row,) = agent_runs.read_json_lines(agent_runs.agents_dir(home) / "s1" / "tools.jsonl")
            self.assertEqual((row["agent"], row["tool"], row["label"]), ("a1", "Bash", "ls"))


class Concurrency(unittest.TestCase):
    def test_two_writers_of_one_file_never_leave_it_torn(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state" / "launch-a1.json"
            stop, torn = threading.Event(), []

            def writer(tag):
                for n in range(150):
                    agent_runs.write_json(path, {"tag": tag, "n": n, "pad": "x" * 5000})

            def reader():
                while not stop.is_set():
                    if path.exists():
                        try:
                            json.loads(path.read_text(encoding="utf-8"))
                        except ValueError:
                            torn.append(1)
                        except OSError:     # gone, or held by a writer for an instant
                            pass

            threads = [threading.Thread(target=writer, args=(t,)) for t in "ab"]
            watcher = threading.Thread(target=reader)
            watcher.start()
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            stop.set()
            watcher.join()
            self.assertEqual(torn, [])
            self.assertEqual(len(list(path.parent.iterdir())), 1)        # no temp file left over

    def test_two_sessions_appending_to_the_durable_log_keep_every_line_whole(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runs" / "k.jsonl"

            def writer(tag):
                for n in range(200):
                    agent_runs.append(path, {"session": tag, "n": n, "pad": "y" * 3000})

            threads = [threading.Thread(target=writer, args=(t,)) for t in ("s1", "s2")]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 400)
            self.assertEqual(len([json.loads(line) for line in lines]), 400)
            self.assertEqual([f.name for f in path.parent.iterdir()], ["k.jsonl"])  # lock released

    def test_a_lock_nobody_released_does_not_stop_an_append(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runs" / "k.jsonl"
            path.parent.mkdir()
            lock = path.with_name(".k.jsonl.lock")
            lock.write_text("")
            old = time.time() - 60
            os.utime(lock, (old, old))
            agent_runs.append(path, {"n": 1})
            self.assertEqual(path.read_text(encoding="utf-8"), '{"n": 1}\n')
            self.assertFalse(lock.exists())

    def test_a_lock_held_too_long_is_written_past(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runs" / "k.jsonl"
            path.parent.mkdir()
            lock = path.with_name(".k.jsonl.lock")
            lock.write_text("")                       # fresh, and never released
            with mock.patch.object(agent_runs, "LOCK_TRIES", 3):
                agent_runs.append(path, {"n": 1})
            self.assertEqual(path.read_text(encoding="utf-8"), '{"n": 1}\n')
            self.assertTrue(lock.exists())            # not ours to remove


class Sweep(unittest.TestCase):
    def test_state_older_than_the_fixed_period_goes_and_the_durable_log_stays(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            base = agent_runs.agents_dir(home)
            (base / "old").mkdir(parents=True)
            (base / "old" / "runs.jsonl").write_text("{}\n")
            (base / "new").mkdir()
            (base / "new" / "runs.jsonl").write_text("{}\n")
            (base / "runs").mkdir()
            (base / "runs" / "k.jsonl").write_text("{}\n")
            (base / "old.resolved.json").write_text("{}")
            (base / "new.resolved.json").write_text("{}")
            then = time.time() - 40 * 86400
            for p in (base / "old", base / "old" / "runs.jsonl", base / "old.resolved.json",
                      base / "runs" / "k.jsonl"):
                os.utime(p, (then, then))
            agent_runs.sweep(home, time.time())
            self.assertEqual(sorted(p.name for p in base.iterdir()),
                             ["new", "new.resolved.json", "runs"])
            self.assertTrue((base / "runs" / "k.jsonl").exists())

    def test_a_session_still_being_written_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            d = agent_runs.agents_dir(home) / "live"
            d.mkdir(parents=True)
            then = time.time() - 40 * 86400
            os.utime(d, (then, then))
            (d / "tools.jsonl").write_text("{}\n")           # written just now
            agent_runs.sweep(home, time.time())
            self.assertTrue(d.exists())


class HookLatency(unittest.TestCase):
    """What the hook costs with both options off, on whatever runs this: the number a release
    reads from the CI log. The bound is loose on purpose, so a slow runner passes."""

    def test_both_options_off_hook_latency(self):
        payload = json.dumps({"session_id": "S", "hook_event_name": "PostToolUse",
                              "tool_name": "Bash"}).encode()
        took = []
        with tempfile.TemporaryDirectory() as home:      # no resolved file: both options off
            env = {**os.environ, "HOME": home}
            for _ in range(5):
                start = time.perf_counter()
                res = subprocess.run([sys.executable, str(PLUGIN / "hooks" / "agents.py")],
                                     input=payload, env=env, capture_output=True)
                took.append(time.perf_counter() - start)
                self.assertEqual((res.returncode, res.stdout, res.stderr), (0, b"", b""))
            self.assertEqual(os.listdir(home), [])
        median = statistics.median(took)
        # On its own line, also under `unittest -v`, which leaves the cursor after the test name.
        sys.stderr.write(f"\nagent-hook latency: {median * 1000:.0f} ms median of 5 runs, "
                         f"both options off ({sys.platform}, Python {sys.version.split()[0]})\n")
        sys.stderr.flush()
        self.assertLess(median, 1.5)


class Resolve(unittest.TestCase):
    class Chk:
        def __init__(self, level, params):
            self.level, self.params = level, params

    class Cfg:
        def __init__(self, checks):
            self.checks = checks

    def test_both_off_resolves_to_none_for_each(self):
        cfg = self.Cfg({"agent-gates": self.Chk("off", {}), "agent-measure": self.Chk("off", {})})
        out = agent_runs.resolve(cfg, Path("/p"))
        self.assertEqual((out["gates"], out["measure"]), (None, None))

    def test_on_carries_the_params(self):
        cfg = self.Cfg({"agent-gates": self.Chk("warn", {**GATES, "briefed": ["qa-engineer"]}),
                        "agent-measure": self.Chk("off", {})})
        out = agent_runs.resolve(cfg, Path("/p"))
        self.assertEqual(out["gates"]["briefed"], ["qa-engineer"])
        self.assertIsNone(out["measure"])


def row(agent, type_="developer", tokens=None, **extra):
    tokens = tokens if tokens is not None else {
        "requests": 1, "input_tokens": 0, "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0, "output_tokens": 100, "context_at_end": 100}
    return {"t": 1.0, "agent": agent, "type": type_, "model": "sonnet", "resumed": False,
            "wall_s": 1.0, "tokens": tokens, "tool_calls": 0, "tool_ms": 0, "failed_calls": 0,
            "result_bytes": 0, "by_tool": {}, "largest": [], "repeated": [], "status": None,
            "findings": None, "gate_blocks": 0, "gate_unmet": [], **extra}


class Suggestions(unittest.TestCase):
    """Each rule prints when its pattern is present and not otherwise."""

    def suggest(self, runs):
        return agent_runs.suggestions(runs)

    def assert_one(self, runs, needle):
        found = self.suggest(runs)
        self.assertEqual(len([s for s in found if needle in s]), 1, found)
        self.assertTrue(all(s.startswith("Suggestion: ") for s in found))

    def assert_none(self, runs, needle):
        self.assertEqual([s for s in self.suggest(runs) if needle in s], [])

    def senior(self, n_senior, n_other, big=100, small=10):
        return ([row(f"s{i}", "senior-developer", tokens=self.tok(big)) for i in range(n_senior)]
                + [row(f"d{i}", "developer", tokens=self.tok(small)) for i in range(n_other)])

    @staticmethod
    def tok(n):
        return {"requests": 1, "input_tokens": n, "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0, "output_tokens": 0, "context_at_end": n}

    def test_no_runs_no_suggestions(self):
        self.assertEqual(self.suggest([]), [])

    def test_senior_developer_above_40_percent_over_five_runs(self):
        self.assert_one(self.senior(2, 3), "senior_triggers")

    def test_senior_developer_below_40_percent_is_quiet(self):
        self.assert_none(self.senior(1, 4, big=10, small=100), "senior_triggers")

    def test_senior_developer_share_over_fewer_than_five_runs_is_quiet(self):
        self.assert_none(self.senior(2, 2), "senior_triggers")

    def test_two_resumed_runs(self):
        tok = {**self.tok(5), "cache_read_input_tokens": 1234}
        self.assert_one([row("a", resumed=True, tokens=tok), row("b", resumed=True, tokens=tok)],
                        "2 runs were resumed and re-read 2,468 cached tokens")

    def test_one_resumed_run_is_quiet(self):
        self.assert_none([row("a", resumed=True), row("b")], "resumed")

    def test_one_command_repeated_in_two_runs(self):
        rep = [{"command": "make test", "n": 3}]
        self.assert_one([row("a", repeated=rep), row("b", repeated=rep)], "`make test` ran")

    def test_a_command_repeated_in_only_one_run_is_quiet(self):
        rep = [{"command": "make test", "n": 9}]
        self.assert_none([row("a", repeated=rep), row("b")], "make test")

    def test_two_different_commands_each_in_one_run_are_quiet(self):
        self.assert_none([row("a", repeated=[{"command": "x", "n": 3}]),
                          row("b", repeated=[{"command": "y", "n": 3}])], "ran three times")

    def test_a_result_over_50_kb(self):
        big = [{"tool": "Read", "bytes": 60 * 1024, "label": "/w/big.log"}]
        self.assert_one([row("a", largest=big)], "read by search or line range")

    def test_a_result_of_50_kb_or_less_is_quiet(self):
        small = [{"tool": "Read", "bytes": 50 * 1024, "label": "/w/ok.log"}]
        self.assert_none([row("a", largest=small)], "read by search")

    def test_contracts_refused_in_three_runs(self):
        self.assert_one([row(str(i), gate_blocks=1) for i in range(3)], "contracts were refused")

    def test_contracts_refused_in_two_runs_is_quiet(self):
        self.assert_none([row("a", gate_blocks=2), row("b", gate_blocks=1), row("c")],
                         "contracts were refused")

    def test_three_blocked_for_one_role(self):
        self.assert_one([row(str(i), status="BLOCKED") for i in range(3)], "finished BLOCKED 3")

    def test_blocked_spread_over_roles_is_quiet(self):
        runs = [row("a", "developer", status="BLOCKED"), row("b", "developer", status="BLOCKED"),
                row("c", "qa-engineer", status="BLOCKED"), row("d", "qa-engineer", status="DONE")]
        self.assert_none(runs, "BLOCKED")


class ReportCommand(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = repo(Path(self._tmp.name))
        self.home, self.project = self.repo.home, self.repo.root

    def tearDown(self):
        self._tmp.cleanup()

    def govern(self, *argv):
        return self.repo.run(*argv)

    def log(self, *rows):
        for r in rows:
            agent_runs.append(agent_runs.durable_path(self.home, self.project), r)

    def test_mark_starts_a_phase_and_report_covers_only_since_the_last_mark(self):
        self.log(row("old"))
        self.assertEqual(self.govern("runs", "mark", "phase 2")[0], 0)
        self.log(row("new", "qa-engineer"))
        code, out, _ = self.govern("runs", "report")
        self.assertEqual(code, 0)
        self.assertIn("1 agent runs (since mark 'phase 2')", out)
        self.assertIn("qa-engineer", out)
        self.assertNotIn("developer", out)

    def test_report_all_covers_every_run(self):
        self.log(row("old"))
        self.govern("runs", "mark", "phase 2")
        self.log(row("new", "qa-engineer"))
        _, out, _ = self.govern("runs", "report", "--all")
        self.assertIn("2 agent runs (all runs)", out)

    def test_report_json(self):
        self.log(row("a", by_tool={"Bash": {"calls": 2, "ms": 10, "bytes": 5, "failed": 0}}),
                 row("b", resumed=True), row("c", resumed=True))
        code, out, _ = self.govern("runs", "report", "--json")
        data = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(len(data["runs"]), 3)
        self.assertEqual(data["by_tool"]["Bash"]["calls"], 2)
        self.assertTrue(data["suggestions"][0].startswith("Suggestion: 2 runs were resumed"))

    def test_report_prints_a_suggestion_when_its_pattern_is_present_and_not_otherwise(self):
        self.log(row("a"))
        self.assertNotIn("Suggestion:", self.govern("runs", "report")[1])
        self.log(*[row(str(i), status="BLOCKED") for i in range(3)])
        self.assertIn("finished BLOCKED 3", self.govern("runs", "report")[1])

    def test_an_empty_report(self):
        code, out, _ = self.govern("runs", "report")
        self.assertEqual(code, 0)
        self.assertIn("0 agent runs", out)

    def test_a_late_model_row_is_applied(self):
        self.log({**row("a"), "model": None, "session": "s1"},
                 {"model_for": "a", "session": "s1", "model": "haiku"})
        self.assertEqual(json.loads(self.govern("runs", "report", "--json")[1])["runs"][0]["model"],
                         "haiku")

    def test_resolve_json_prints_the_resolved_settings(self):
        cfg = self.project / layout.CONFIG
        cfg.write_text(cfg.read_text(encoding="utf-8") +
                       '\n[checks.agent-gates]\nlevel = "error"\nbriefed = ["developer"]\n'
                       '[checks.agent-gates.reasons]\nbriefed = "only the developer needs a brief here"\n', encoding="utf-8")
        code, out, _ = self.govern("runs", "resolve", "--json")
        data = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(data["gates"]["briefed"], ["developer"])
        self.assertEqual(data["gates"]["max_refusals"], 2)
        self.assertIsNone(data["measure"])
        self.assertEqual(data["root"], str(self.project.resolve()))

    def test_resolve_both_off_by_default(self):
        data = json.loads(self.govern("runs", "resolve", "--json")[1])
        self.assertEqual((data["gates"], data["measure"]), (None, None))
        self.assertIn("agent-gates: off", self.govern("runs", "resolve")[1])


class HeldLaunchPairing(HookCase):
    """Two starts never take one held launch; a launch paired with the wrong brief is put right
    at stop from the brief the agent's own first prompt names."""
    U1 = ".context-gate/build/p/u1/brief.md"
    U2 = ".context-gate/build/p/u2/brief.md"

    def setUp(self):
        super().setUp()
        self.proj = self.tmp / "proj"
        self.tree = self.proj / ".claude/worktrees/a1"
        for unit in ("u1", "u2"):
            (self.proj / f".context-gate/build/p/{unit}").mkdir(parents=True)
            (self.tree / f".context-gate/build/p/{unit}").mkdir(parents=True)
            (self.proj / f".context-gate/build/p/{unit}/brief.md").write_text(
                f"Do it.\nProduces: .context-gate/build/p/{unit}/developer-report.md\n",
                encoding="utf-8")

    def held(self, name, ref):
        agent_runs.write_json(self.state / name, {
            "agent_type": "developer",
            **agent_runs.contract(f"Brief: {ref}", str(self.proj))})

    def test_two_starts_never_take_the_same_held_launch(self):
        self.held("pending-1.json", self.U1)
        self.held("pending-2.json", self.U2)
        for agent in ("x", "y"):
            agent_runs.on_start(self.home, "s1", {"agent_id": agent, "agent_type": "developer"})
        refs = {agent_runs.read_json(self.state / f"launch-{a}.json")["brief_ref"]
                for a in ("x", "y")}
        self.assertEqual(refs, {self.U1, self.U2})
        self.assertEqual(list(self.state.glob("pending-*.json")), [])
        self.assertEqual(list(self.state.glob("claimed-*.json")), [])

    def test_a_held_launch_taken_by_another_start_is_skipped(self):
        self.held("pending-1.json", self.U1)
        self.held("pending-2.json", self.U2)
        real = os.replace

        def taken(src, dst):
            if Path(src).name == "pending-1.json":
                Path(src).unlink()      # the other hook process got there first
            return real(src, dst)

        with mock.patch.object(agent_runs.os, "replace", taken):
            agent_runs.on_start(self.home, "s1", {"agent_id": "x", "agent_type": "developer"})
        record = agent_runs.read_json(self.state / "launch-x.json")
        self.assertEqual(record["brief_ref"], self.U2)
        self.assertTrue(record["paired"])

    def paired_with_u1(self, first_prompt=None):
        self.launch(f"Brief: {self.U1}", agent_type="developer", cwd=str(self.proj),
                    start_cwd=str(self.tree))
        path = self.tmp / "agent.jsonl"
        lines = [{"type": "user", "message": {"content": first_prompt or f"Brief: {self.U2}"}},
                 {"type": "assistant", "message": {
                     "id": "m1", "usage": USAGE, "content": [{"type": "text", "text": "done"}]}}]
        path.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
        return str(path)

    def record(self):
        return agent_runs.read_json(self.state / "launch-a1.json")

    def test_a_contract_paired_with_the_wrong_brief_is_corrected_at_stop(self):
        transcript = self.paired_with_u1()
        (self.tree / ".context-gate/build/p/u2/developer-report.md").write_text(
            "done\n", encoding="utf-8")
        self.assertIsNone(self.stop(transcript=transcript))
        record = self.record()
        self.assertEqual(record["brief_ref"], self.U2)
        self.assertTrue(record["repaired"])

    def test_a_corrected_contract_still_blocks_on_its_own_missing_file(self):
        transcript = self.paired_with_u1()
        out = self.stop(transcript=transcript)
        self.assertEqual(out["decision"], "block")
        self.assertIn(str(self.tree / ".context-gate/build/p/u2/developer-report.md"),
                      out["reason"])
        self.assertNotIn("u1", out["reason"])

    def test_a_rightly_paired_contract_is_left_alone(self):
        transcript = self.paired_with_u1(f"Brief: {self.U1}")
        (self.tree / ".context-gate/build/p/u1/developer-report.md").write_text(
            "done\n", encoding="utf-8")
        self.assertIsNone(self.stop(transcript=transcript))
        record = self.record()
        self.assertNotIn("repaired", record)
        self.assertEqual(record["brief_ref"], self.U1)

    def test_an_unreadable_transcript_leaves_the_pairing(self):
        self.paired_with_u1()
        out = self.stop(transcript=str(self.tmp / "missing.jsonl"))
        self.assertEqual(out["decision"], "block")
        self.assertIn("u1", out["reason"])
        self.assertNotIn("repaired", self.record())


    def test_a_resumed_agent_keeps_its_resume_contract_at_stop(self):
        transcript = self.paired_with_u1(f"Brief: {self.U1}")
        (self.tree / ".context-gate/build/p/u1/developer-report.md").write_text(
            "done\n", encoding="utf-8")
        self.assertIsNone(self.stop(transcript=transcript))
        self.fire("PreToolUse", tool_name="SendMessage", cwd=str(self.proj),
                  tool_input={"to": "a1", "message": f"Brief: {self.U2}\nFix pass."})
        self.fire("SubagentStart", agent_id="a1", agent_type="developer",
                  cwd=str(self.tree))
        self.stop(transcript=transcript)
        record = self.record()
        self.assertNotIn("repaired", record)
        self.assertEqual(record["brief_ref"], self.U2)

    def test_a_named_brief_that_is_not_a_file_leaves_the_contract(self):
        transcript = self.paired_with_u1(
            "Brief: .context-gate/build/p/gone/brief.md and then some words")
        out = self.stop(transcript=transcript)
        record = self.record()
        self.assertNotIn("repaired", record)
        self.assertEqual(record["brief_ref"], self.U1)
        self.assertEqual(out["decision"], "block")
        self.assertIn("u1", out["reason"])

    def test_a_stale_claimed_file_is_swept_at_start(self):
        stale = self.state / "claimed-x.json"
        fresh = self.state / "claimed-y.json"
        agent_runs.write_json(stale, {"agent_type": "developer"})
        agent_runs.write_json(fresh, {"agent_type": "developer"})
        old = time.time() - agent_runs.HELD_BRIEF_SECONDS - 5
        os.utime(stale, (old, old))
        agent_runs.on_start(self.home, "s1", {"agent_id": "z", "agent_type": "developer"})
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())


if __name__ == "__main__":
    unittest.main()
