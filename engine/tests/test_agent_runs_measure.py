"""Agent measurement: rows, quiet runs, the options, concurrency, the report command and unfinished runs."""
from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent_runs_support import *  # noqa: E402,F401,F403

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


class UnfinishedRuns(HookCase):
    """A run blocked on its contract and never stopped again is still measured."""
    LINE = "blocked on its contract and never ended"

    def report(self):
        runs, name = agent_runs.load_runs(self.home, self.root, True)
        return agent_runs.render_report(agent_runs.build_report(runs), name, True), runs

    def test_a_blocked_run_never_stopped_again_is_in_the_report(self):
        report = self.work / "r.md"
        self.launch(f"Produces: {report}")
        tr = self.transcript([("m1", USAGE, "working")])
        self.assertEqual(self.stop(transcript=tr)["decision"], "block")
        (dur,) = [r for r in self.durable() if "agent" in r]
        self.assertTrue(dur["unfinished"])
        self.assertEqual((dur["session"], dur["gate_blocks"], dur["produced"]), ("s1", 1, []))
        self.assertEqual(self.rows(), [])
        text, runs = self.report()
        self.assertEqual(len(runs), 1)
        self.assertIn("1 agent runs", text)
        self.assertIn(f"general-purpose: {self.LINE}", text)

    def test_the_same_run_ending_met_counts_once_with_the_whole_runs_tokens(self):
        report = self.work / "r.md"
        self.launch(f"Produces: {report}")
        tr = self.transcript([("m1", USAGE, "working")])
        self.assertEqual(self.stop(transcript=tr)["decision"], "block")
        report.write_text("done\n", encoding="utf-8")
        tr = self.transcript([("m1", USAGE, "working"), ("m2", USAGE, "more")])
        self.assertIsNone(self.stop(transcript=tr))
        text, runs = self.report()
        self.assertEqual(len(runs), 1)
        self.assertNotIn("unfinished", runs[0])
        self.assertEqual(runs[0]["tokens"]["output_tokens"], 20)
        self.assertEqual(runs[0]["tokens"]["requests"], 2)
        self.assertIn("1 agent runs", text)
        self.assertNotIn(self.LINE, text)

    def test_with_measurement_off_a_blocked_run_writes_no_row(self):
        self.resolve(GATES, None)
        self.launch(f"Produces: {self.work / 'r.md'}")
        self.assertEqual(self.stop()["decision"], "block")
        self.assertEqual(self.rows(), [])
        self.assertEqual([r for r in self.durable() if "agent" in r], [])

    def test_unfinished_rows_are_dropped_when_a_later_row_has_the_same_agent(self):
        path = agent_runs.durable_path(self.home, self.root)

        def runs_for(rows):
            path.unlink(missing_ok=True)
            for r in rows:
                agent_runs.append(path, r)
            return agent_runs.load_runs(self.home, self.root, True)[0]

        def un(t, agent="a1"):
            return dict(row(agent), session="s1", unfinished=True, t=t)

        def fin(t):
            return dict(row("a1"), session="s1", t=t)

        got = runs_for([un(1), un(2), fin(3)])
        self.assertEqual([(r["t"], "unfinished" in r) for r in got], [(3, False)])
        got = runs_for([un(1), un(2)])
        self.assertEqual([r["t"] for r in got], [2])
        got = runs_for([fin(1), fin(2)])
        self.assertEqual([r["t"] for r in got], [1, 2])
        got = runs_for([un(1, "a2"), fin(2)])
        self.assertEqual(len(got), 2)
        got = runs_for([un(1), dict(fin(2), session="s2")])
        self.assertEqual([r["t"] for r in got], [1, 2])


class ChecklistVisualPaths(unittest.TestCase):
    def test_visual_paths_gives_ux_reviewer_its_checklist(self):
        Chk, Cfg = Resolve.Chk, Resolve.Cfg
        cfg = Cfg({"agent-gates": Chk("warn", {**GATES, "checklist": True}),
                   "agent-roles": Chk("warn", {"visual_paths": ["web/**"]})})
        texts = agent_runs.resolve(cfg, Path("/p"))["gates"]["checklist_texts"]
        self.assertTrue(texts.get("ux-reviewer"))


class ChecklistUnitGate(unittest.TestCase):
    def texts(self, **params):
        Chk, Cfg = Resolve.Chk, Resolve.Cfg
        cfg = Cfg({"agent-gates": Chk("warn", {**GATES, "checklist": True}),
                   "agent-roles": Chk("warn", params)})
        return agent_runs.resolve(cfg, Path("/p"))["gates"]["checklist_texts"]

    def test_a_developer_answering_the_scoped_gate_line_passes(self):
        texts = self.texts()["developer"]
        self.assertIn("The coordinator runs the full gate.", texts["Gate"])
        report = "Gate: python3 -m unittest -k roles: Ran 9 tests, OK\n"
        self.assertTrue(agent_runs.answers(report, "Gate", texts))

    def test_full_keeps_the_old_gate_line(self):
        self.assertIn("the full gate's result line", self.texts(unit_gate="full")["developer"]["Gate"])


if __name__ == "__main__":
    unittest.main()
