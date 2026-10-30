"""Recording the coordinator's rows: at session end, caught up by the report, shown in it."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import profile_guard  # noqa: E402,F401

from test_options import repo  # noqa: E402

from govern import agent_runs, coordinator_runs, layout, usage  # noqa: E402
from test_coordinator_runs import T0, assistant, tool_result, tool_use, user  # noqa: E402
import test_coordinator_runs  # noqa: E402
import test_phase  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / "plugin"
ENGINE = REPO / "engine"


def session(extra_at=0.0) -> list[dict]:
    """A session with a request before any mark, and two later ones."""
    return [assistant(1, "m1"),
            user(2, {"kind": "human"}),
            assistant(20 + extra_at, "m2"),
            tool_use(21 + extra_at, "m3", "u1", "Read"),
            tool_result(22 + extra_at, "u1", "x" * 2048)]


class Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.root = self.tmp / "project"
        self.home.mkdir()
        self.root.mkdir()
        self.log = agent_runs.durable_path(self.home, self.root)

    def transcript(self, entries, name="S1.jsonl", mtime=None) -> Path:
        folder = coordinator_runs.transcript_folder(self.home, self.root)
        folder.mkdir(parents=True, exist_ok=True)
        p = folder / name
        p.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
        if mtime is not None:
            os.utime(p, (mtime, mtime))
        return p

    def rows(self) -> list[dict]:
        return [r for r in agent_runs.read_json_lines(self.log) if r.get("coordinator")]


class Record(Case):
    def test_record_writes_one_row_per_phase(self):
        agent_runs.mark(self.home, self.root, "p1", T0 + 10)
        p = self.transcript(session())
        self.assertEqual(coordinator_runs.record(self.home, self.root, p), 2)
        self.assertEqual(sorted(r["phase"] for r in self.rows()), ["", "p1"])

    def test_record_of_a_rejected_transcript_writes_nothing(self):
        p = self.tmp / "bad.jsonl"
        p.write_text("not json\n", encoding="utf-8")
        self.assertEqual(coordinator_runs.record(self.home, self.root, p), 0)
        self.assertEqual(self.rows(), [])

    def test_recording_twice_keeps_one_row_per_session_and_phase(self):
        agent_runs.mark(self.home, self.root, "p1", T0 + 10)
        p = self.transcript(session())
        coordinator_runs.record(self.home, self.root, p)
        coordinator_runs.record(self.home, self.root, p)
        self.assertEqual(len(self.rows()), 4)
        runs, _ = agent_runs.load_runs(self.home, self.root, everything=True)
        self.assertEqual(sorted(r["phase"] for r in runs if r.get("coordinator")), ["", "p1"])

    def test_a_phase_written_after_a_later_mark_reports_under_its_own_phase(self):
        agent_runs.mark(self.home, self.root, "p1", T0 + 10)
        p = self.transcript(session())
        coordinator_runs.record(self.home, self.root, p)
        agent_runs.mark(self.home, self.root, "p2", T0 + 100)
        runs, name = agent_runs.load_runs(self.home, self.root)
        self.assertEqual((name, [r for r in runs if r.get("coordinator")]), ("p2", []))
        # The session's later rows are written only now, after p2 began.
        coordinator_runs.record(self.home, self.root, self.transcript(session(), "S2.jsonl"))
        agent_runs.append(self.log, {"mark": "p1", "t": T0 + 200})
        runs, name = agent_runs.load_runs(self.home, self.root)
        self.assertEqual(name, "p1")
        self.assertEqual({(r["session"], r["phase"]) for r in runs if r.get("coordinator")},
                         {("S1", "p1"), ("S2", "p1")})


# A session's file time must not be in the future of its recorded row, and the transcripts' own
# stamps (T0) are, so the catch-up cases keep their marks and file times in the past.
MARK, PAST = T0 - 300_000_000, T0 - 200_000_000


class CatchUp(Case):
    def test_without_a_mark_nothing_is_done(self):
        self.transcript(session())
        self.assertEqual(coordinator_runs.catch_up(self.home, self.root), 0)
        self.assertEqual(self.rows(), [])

    def test_a_session_that_never_ended_is_recorded(self):
        agent_runs.mark(self.home, self.root, "p1", MARK)
        self.transcript(session(), mtime=PAST)
        self.assertEqual(coordinator_runs.catch_up(self.home, self.root), 1)
        self.assertEqual(len(self.rows()), 1)

    def test_a_session_older_than_the_oldest_mark_is_skipped(self):
        agent_runs.mark(self.home, self.root, "p1", MARK)
        self.transcript(session(), mtime=MARK - 500)
        self.assertEqual(coordinator_runs.catch_up(self.home, self.root), 0)

    def test_a_session_with_nothing_new_writes_nothing(self):
        agent_runs.mark(self.home, self.root, "p1", MARK)
        self.transcript(session(), mtime=PAST)
        coordinator_runs.catch_up(self.home, self.root)
        before = self.log.read_text(encoding="utf-8")
        self.assertEqual(coordinator_runs.catch_up(self.home, self.root), 0)
        self.assertEqual(self.log.read_text(encoding="utf-8"), before)

    def test_a_session_that_grew_is_recorded_again(self):
        agent_runs.mark(self.home, self.root, "p1", MARK)
        p = self.transcript(session(), mtime=PAST)
        coordinator_runs.catch_up(self.home, self.root)
        p.write_text(p.read_text(encoding="utf-8")
                     + json.dumps(assistant(40, "m9")) + "\n", encoding="utf-8")
        later = time.time() + 5
        os.utime(p, (later, later))
        self.assertEqual(coordinator_runs.catch_up(self.home, self.root), 1)


class Report(Case):
    def build(self):
        agent_runs.mark(self.home, self.root, "p1", T0 + 10)
        coordinator_runs.record(self.home, self.root, self.transcript(session()))
        runs, name = agent_runs.load_runs(self.home, self.root, everything=True)
        return agent_runs.build_report(runs), name

    def test_json_has_a_coordinator_key_per_phase(self):
        report, _ = self.build()
        self.assertEqual(sorted(report["coordinator"]), ["", "p1"])
        p1 = report["coordinator"]["p1"]
        self.assertEqual((p1["requests"], p1["owner_turns"], p1["handbacks"]), (2, 0, 0))
        self.assertEqual(p1["largest"], [{"tool": "Read", "kb": 2.0}])
        self.assertEqual(report["coordinator"][""]["owner_turns"], 1)

    def test_text_section_and_role_line(self):
        report, name = self.build()
        text = agent_runs.render_report(report, name, True)
        self.assertIn("0 agent runs (all runs).", text)
        self.assertIn("Coordinator (requests, tokens: new input, cached re-read, output; peak "
                      "context; compactions; owner turns; hand-backs):", text)
        self.assertIn("- planning: 1, 3, 3, 4; 10; 0; 1; 0", text)
        self.assertIn("- p1: 2, 6, 6, 8; 10; 0; 0; 0", text)
        self.assertIn("- 2.0 KB Read", text)
        self.assertIn("- coordinator (model-a): 2, 30, 9, 9, 12", text)

    def test_per_run_lines_skip_coordinator_rows(self):
        report, name = self.build()
        text = agent_runs.render_report(report, name, True)
        self.assertNotIn("None", text.split("By role")[0])

    def test_no_section_without_coordinator_rows(self):
        text = agent_runs.render_report(agent_runs.build_report([]), None, True)
        self.assertNotIn("Coordinator", text)


class ReportCatchUp(unittest.TestCase):
    """`runs report` records the main sessions not yet in the log, only with `agent-measure` on."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = repo(Path(self._tmp.name))
        self.home, self.project = self.repo.home, self.repo.root
        self.log = agent_runs.durable_path(self.home, self.project)
        agent_runs.mark(self.home, self.project, "p1", MARK)
        folder = coordinator_runs.transcript_folder(self.home, self.project)
        folder.mkdir(parents=True)
        p = folder / "S1.jsonl"
        p.write_text("".join(json.dumps(e) + "\n" for e in session()), encoding="utf-8")
        os.utime(p, (PAST, PAST))

    def measure(self, level):
        cfg = self.project / layout.CONFIG
        cfg.write_text(cfg.read_text(encoding="utf-8")
                       + f'\n[checks.agent-measure]\nlevel = "{level}"\n', encoding="utf-8")

    def test_measure_on_records_a_session_that_never_ended(self):
        self.measure("warn")
        code, out, _ = self.repo.run("runs", "report", "--all")
        self.assertEqual(code, 0)
        self.assertIn("Coordinator (", out)
        self.assertIn("- p1: 3, 9, 9, 12; 10; 0; 1; 0", out)
        self.assertTrue(any(r.get("coordinator") for r in agent_runs.read_json_lines(self.log)))

    def test_measure_off_leaves_the_log_unchanged(self):
        before = self.log.read_text(encoding="utf-8")
        code, out, _ = self.repo.run("runs", "report", "--all")
        self.assertEqual(code, 0)
        self.assertNotIn("Coordinator (", out)
        self.assertEqual(self.log.read_text(encoding="utf-8"), before)


class Newest(Case):
    def row(self, t, requests, session_id="S1", phase="p1"):
        return {"coordinator": True, "session": session_id, "phase": phase, "t": t,
                "requests": requests, "model": "model-a"}

    def test_load_runs_keeps_the_row_with_the_larger_t_in_either_order(self):
        old, new = self.row(10.0, 1), self.row(20.0, 5)
        for rows in ([old, new], [new, old]):
            self.log.unlink(missing_ok=True)
            agent_runs.append(self.log, {"mark": "p1", "t": 1.0})
            for r in rows:
                agent_runs.append(self.log, r)
            runs, _ = agent_runs.load_runs(self.home, self.root)
            self.assertEqual([(r["t"], r["requests"]) for r in runs], [(20.0, 5)])


class EmptyPhase(Case):
    def test_a_phase_with_only_an_owner_turn_has_no_role_line(self):
        agent_runs.mark(self.home, self.root, "p1", T0 + 10)
        coordinator_runs.record(self.home, self.root, self.transcript(
            [assistant(1, "m1"), user(2, {"kind": "human"}), user(20, {"kind": "human"})]))
        runs, _ = agent_runs.load_runs(self.home, self.root, everything=True)
        report = agent_runs.build_report(runs)
        self.assertEqual(report["coordinator"]["p1"]["requests"], 0)
        self.assertEqual(list(report["by_role"]), ["coordinator (model-a)"])
        self.assertNotIn("coordinator ()", agent_runs.render_report(report, None, True))


class Hook(Case):
    def setUp(self):
        super().setUp()
        self.built = self.tmp / "built"
        shutil.copytree(PLUGIN, self.built)
        shutil.copytree(ENGINE / "govern", self.built / "govern",
                        ignore=shutil.ignore_patterns("__pycache__"))
        usage.write_json(usage.snapshot_path(self.home, "S"), {"captured_at": 1})
        agent_runs.mark(self.home, self.root, "p1", T0 + 10)

    def resolve(self, measure=None, root=None):
        agent_runs.write_json(agent_runs.resolved_path(self.home, "S"), {
            "gates": None, "measure": measure, "root": str(root or self.root)})

    def end(self, payload=None, raw=None):
        env = {**os.environ, "HOME": str(self.home), "CLAUDE_PROJECT_DIR": str(self.root),
               "CONTEXT_GATE_NO_UPDATE_CHECK": "1"}
        body = raw if raw is not None else json.dumps(
            {"session_id": "S", "hook_event_name": "SessionEnd", **(payload or {})})
        res = subprocess.run([sys.executable, str(self.built / "hooks" / "usage.py")],
                             input=body, env=env, capture_output=True, text=True,
                             cwd=str(self.tmp))
        self.assertEqual((res.returncode, res.stdout, res.stderr), (0, "", ""))

    def ended(self) -> bool:
        return list(usage.state_dir(self.home).glob("S.*")) == []

    def test_session_end_records_the_coordinator(self):
        self.resolve({"notice": True})
        p = self.transcript(session())
        self.end({"transcript_path": str(p)})
        self.assertEqual(sorted(r["phase"] for r in self.rows()), ["", "p1"])
        self.assertTrue(self.ended())

    def test_a_row_holds_numbers_only(self):
        self.resolve({"notice": True})
        self.end({"transcript_path": str(self.transcript(session()))})
        text = self.log.read_text(encoding="utf-8")
        self.assertNotIn("made-up", text)

    def test_measure_off_writes_nothing(self):
        p = self.transcript(session())
        self.end({"transcript_path": str(p)})
        self.resolve(None)
        self.end({"transcript_path": str(p)})
        self.assertEqual(self.rows(), [])

    def test_malformed_payloads_are_silent_and_end_the_session(self):
        self.resolve({"notice": True})
        bad = self.tmp / "bad.jsonl"
        bad.write_text("not json\n", encoding="utf-8")
        for payload in ({"transcript_path": 7}, {"transcript_path": str(bad)},
                        {"transcript_path": str(self.tmp / "missing.jsonl")}, {}):
            usage.write_json(usage.snapshot_path(self.home, "S"), {"captured_at": 1})
            self.end(payload)
            self.assertTrue(self.ended(), payload)
        self.assertEqual(self.rows(), [])

    def test_a_failing_record_is_silent_and_still_ends_the_session(self):
        self.resolve({"notice": True})
        agent_runs.durable_path(self.home, self.root).unlink()
        agent_runs.durable_path(self.home, self.root).mkdir()      # appending to a folder fails
        self.end({"transcript_path": str(self.transcript(session()))})
        self.assertTrue(self.ended())

    def test_not_json_is_silent(self):
        self.resolve({"notice": True})
        self.end(raw="not json")


class PhaseClosePlanning(test_phase.Phase):
    """`phase close` starts planning in the run log, when there is one."""

    def log_for(self, t) -> Path:
        return agent_runs.durable_path(t.home, t.root)

    def marks(self, t) -> list[dict]:
        return [r for r in agent_runs.read_json_lines(self.log_for(t)) if "mark" in r]

    def test_a_close_with_a_run_log_appends_a_planning_mark(self):
        t = self.work()
        agent_runs.mark(t.home, t.root, "p1", now=1.0)
        before = time.time()
        code, out, err = self.close()
        self.assertEqual(code, 0, err)
        marks = self.marks(t)
        self.assertEqual([m["mark"] for m in marks], ["p1", ""])
        self.assertGreaterEqual(marks[1]["t"], before)
        self.assertEqual(out.splitlines()[-1], "run log: planning from here")

    def test_a_close_without_a_run_log_creates_none(self):
        t = self.work()
        code, out, err = self.close()
        self.assertEqual(code, 0, err)
        self.assertFalse(self.log_for(t).exists())
        self.assertNotIn("run log", out)

    def test_dry_run_writes_nothing(self):
        t = self.work()
        agent_runs.mark(t.home, t.root, "p1", now=T0)
        code, out, err = self.close("--dry-run")
        self.assertEqual(code, 0, err)
        self.assertEqual([m["mark"] for m in self.marks(t)], ["p1"])

    def test_a_failed_close_writes_nothing(self):
        t = self.work()
        agent_runs.mark(t.home, t.root, "p1", now=T0)
        t.git("remote", "remove", "origin")
        code, out, err = self.close()
        self.assertEqual(code, 1, err)
        self.assertEqual([m["mark"] for m in self.marks(t)], ["p1"])

    def test_a_failing_mark_is_swallowed(self):
        t = self.work()
        agent_runs.mark(t.home, t.root, "p1", now=T0)
        with mock.patch.object(agent_runs, "mark", side_effect=OSError("disk")):
            code, out, err = self.close()
        self.assertEqual(code, 0, err)
        self.assertNotIn("run log", out)


class PlanningReport(Case):
    def test_the_report_names_planning_and_shows_only_its_rows(self):
        agent_runs.mark(self.home, self.root, "p1", now=T0)
        agent_runs.append(self.log, {"type": "developer", "role": "developer", "tokens": 5})
        agent_runs.append(self.log, {"coordinator": True, "session": "S", "phase": "p1",
                                     "t": T0 + 1, "requests": 3})
        agent_runs.mark(self.home, self.root, "", now=T0 + 10)
        agent_runs.append(self.log, {"coordinator": True, "session": "S", "phase": "",
                                     "t": T0 + 11, "requests": 7})
        runs, name = agent_runs.load_runs(self.home, self.root)
        text = agent_runs.render_report(agent_runs.build_report(runs), name, False)
        self.assertIn("(planning, since the last phase closed)", text)
        self.assertNotIn("no mark yet", text)
        self.assertIn("- planning: 7,", text)
        self.assertNotIn("- p1:", text)
        runs, name = agent_runs.load_runs(self.home, self.root, True)
        text = agent_runs.render_report(agent_runs.build_report(runs), name, True)
        self.assertIn("- planning: 7,", text)
        self.assertIn("- p1: 3,", text)


class SpanningClose(test_coordinator_runs.Case):
    def test_a_session_splits_at_the_planning_mark(self):
        rows = self.rows([assistant(-50, "m0"), assistant(50, "m1"), assistant(150, "m2")],
                         [(T0, "p1"), (T0 + 100, "")])
        self.assertEqual([(r["phase"], r["requests"]) for r in rows], [("", 2), ("p1", 1)])


if __name__ == "__main__":
    unittest.main()
