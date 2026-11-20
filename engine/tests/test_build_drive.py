"""`govern build drive`: the loop that starts one headless coordinator session per build step.
A fake `claude` (a Python file) prints scripted stream lines and may change the ledger.

    python3 -m unittest engine.tests.test_build_drive
"""
from __future__ import annotations

import contextlib
import io
import shutil
import signal
import subprocess
import sys
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import profile_guard  # noqa: E402,F401

from build_support import *  # noqa: E402,F401,F403
from govern import drive, drive_log, usage_week  # noqa: E402
from fixture_cache import copy_of  # noqa: E402

FAKE = '''\
import glob, json, os, shutil, subprocess, sys, time
script = json.load(open(os.environ["FAKE_SCRIPT"], encoding="utf-8"))
log = os.environ["FAKE_LOG"]
with open(log, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"argv": sys.argv[1:], "pid": os.getpid(),
                         "pct": os.environ.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"),
                         "ceiling": os.environ.get("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS")}) + "\\n")
n = sum(1 for _ in open(log, encoding="utf-8")) - 1
entry = script[min(n, len(script) - 1)]
if entry.get("ruling"):
    subprocess.run([sys.executable, os.path.join(".context-gate", "bin", "govern"), "build",
                    "ruling", entry["ruling"]], check=True, stdout=subprocess.DEVNULL)
if entry.get("end"):
    subprocess.run(["git", "checkout", "--detach"], check=True, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)
if entry.get("grandchild"):
    child = subprocess.Popen(["sh", "-c", "trap '' TERM; sleep %d" % entry["grandchild"]])
    with open(os.environ["FAKE_PID"], "a", encoding="utf-8") as fh:
        fh.write("%d\\n" % child.pid)
if entry.get("snapshot"):
    found = glob.glob(os.path.join(os.environ["FAKE_RUNS"], "*", "status.json"))
    shutil.copy(found[0], entry["snapshot"])
for line in entry.get("lines", []):
    print(json.dumps(line), flush=True)
if entry.get("snapshot_after"):
    time.sleep(entry["snapshot_after"])
    found = glob.glob(os.path.join(os.environ["FAKE_RUNS"], "*", "status.json"))
    shutil.copy(found[0], entry["snapshot_after_to"])
time.sleep(entry.get("sleep", 0))
'''

SHIM = '''\
import pathlib, sys
sys.path.insert(0, {engine!r})
from govern import cli
sys.exit(cli.main(sys.argv[1:], root=pathlib.Path.cwd()))
'''


def rl(pct: float) -> dict:
    return {"type": "rate_limit_event", "session_id": "s",
            "rate_limit_info": {"unifiedWindows": {"seven_day": {"utilization": pct / 100}}}}


def res(text: str, **more) -> dict:
    return {"type": "result", "session_id": "sess", "result": text, "is_error": False, **more}


def entry(text=None, pct=40.0, **more) -> dict:
    lines = [] if pct is None else [rl(pct)]
    if text is not None:
        lines.append(res(text))
    return {"lines": lines, **more}


class Drive(Build):
    def setUp(self) -> None:
        super().setUp()

        def make(template: Path) -> None:
            """The project with `build start p1` run, made once per process."""
            self.tmp, outer = template, self.tmp
            try:
                self.started()
            finally:
                self.tmp = outer

        copy_of(("drive.started",), self.tmp, make)
        self.w = Solo.attach(self.tmp)
        shim = self.w.root / ".context-gate" / "bin" / "govern"
        shim.parent.mkdir(parents=True, exist_ok=True)
        shim.write_text(SHIM.format(engine=str(ENGINE)), encoding="utf-8")
        self.fake = self.tmp / "fake_claude.py"
        self.fake.write_text(FAKE, encoding="utf-8")
        self.log = self.tmp / "fake.log"

    def drive(self, script: list[dict], **kw) -> tuple[int, str, list[dict]]:
        path = self.tmp / "script.json"
        path.write_text(json.dumps(script), encoding="utf-8")
        env = {"HOME": str(self.w.home), "FAKE_SCRIPT": str(path), "FAKE_LOG": str(self.log),
               "FAKE_PID": str(self.tmp / "fake.pids"), "CONTEXT_GATE_NO_UPDATE_CHECK": "1",
               "FAKE_RUNS": str(drive_log.log_path(self.w.home).parent / "runs")}
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out):
            ctx = self.w.context()
            code = drive.run(ctx, "plan.md", **{"claude": str(self.fake), "cap": 90.0, **kw})
        calls = [json.loads(x) for x in self.log.read_text(encoding="utf-8").splitlines()] \
            if self.log.exists() else []
        return code, out.getvalue(), calls

    def rows(self) -> list[dict]:
        return drive_log.rows(self.w.home)

    def test_session_env_lifts_background_ceiling(self):
        code, out, calls = self.drive([entry(pct=40), entry("x\nLOOP: plan-done", end=True)])
        self.assertEqual(code, 0, out)
        sessions = calls[1:]
        self.assertTrue(sessions)
        self.assertEqual({c["ceiling"] for c in sessions}, {"0"})

    def test_skill_name_follows_the_engine(self):
        both = ["context-gate-beta:build", "context-gate:build"]
        cases = [
            ("0.9.5-beta.2", ["context-gate:build"], "/context-gate:build p.md"),
            ("0.9.5-beta.2", both, "/context-gate-beta:build p.md"),
            ("0.9.5", ["context-gate-beta:build"], "/context-gate-beta:build p.md"),
            ("0.9.5", both, "/context-gate:build p.md"),
            ("0.9.5-beta.2", None, "/context-gate-beta:build p.md"),
            ("0.9.5", None, "/context-gate:build p.md"),
            ("0.9.5-beta.2", ["/context-gate:build"], "/context-gate:build p.md"),
        ]
        for version, commands, want in cases:
            with self.subTest(version=version, commands=commands):
                with mock.patch("govern.__version__", version):
                    self.assertEqual(drive.skill_command("p.md", commands), want)
        code, out, calls = self.drive([entry(pct=40), entry("x\nLOOP: plan-done", end=True)])
        argv = calls[1]["argv"]
        self.assertEqual(argv[argv.index("-p") + 1], "/context-gate:build plan.md")

    def test_stale_roles_exit_1_before_any_session(self):
        err = io.StringIO()
        real = subprocess.run

        def fail_check(argv, *a, **kw):
            if "render" in argv:
                return subprocess.CompletedProcess(
                    argv, 1, "", "1 file(s) would change\nroles differ\n")
            return real(argv, *a, **kw)

        with mock.patch.object(drive.subprocess, "run", fail_check), \
                contextlib.redirect_stderr(err):
            code, out, calls = self.drive([entry(pct=40)])
        self.assertEqual(code, 1)
        self.assertIn("error: agent roles are stale", err.getvalue())
        self.assertIn("roles differ", err.getvalue())
        self.assertEqual(calls, [])

    def test_prompt_says_the_driver_owns_the_build(self):
        text = drive.PROMPT.read_text(encoding="utf-8")
        self.assertIn("not rival coordinators", text)
        self.assertIn("installed.toml", text)

    def test_plan_done_after_two_steps(self):
        code, out, calls = self.drive([
            entry(pct=40),
            entry("did a step\nLOOP: continue", ruling="step one"),
            entry("all closed\nLOOP: plan-done", ruling="step two", end=True),
        ])
        self.assertEqual(code, 0, out)
        self.assertEqual(len(calls), 3)
        argv = calls[1]["argv"]
        for word in ("-p", "stream-json", "--append-system-prompt"):
            self.assertIn(word, argv)
        self.assertEqual(argv[argv.index("-p") + 1], "/context-gate:build plan.md")
        self.assertIn("Unattended run", argv[argv.index("--append-system-prompt") + 1])
        self.assertEqual(calls[1]["pct"], "50")
        self.assertEqual([r["exit"] for r in self.rows()], ["continue", "plan-done"])
        self.assertIn("session 2: plan-done", out)
        runs = list((drive_log.log_path(self.w.home).parent / "runs").iterdir())
        self.assertEqual(len(runs), 1)
        self.assertEqual(sorted(p.name for p in runs[0].iterdir()),
                         ["1.err", "1.jsonl", "2.err", "2.jsonl", "status.json"])

    def test_stop_reason_is_printed(self):
        code, out, calls = self.drive([entry(pct=40), entry("x\nLOOP: stop need the owner",
                                                             ruling="r")])
        self.assertEqual(code, 0, out)
        self.assertIn("need the owner", out)
        self.assertEqual([r["exit"] for r in self.rows()], ["stop"])

    def test_missing_exit_line_runs_again(self):
        code, out, calls = self.drive([
            entry(pct=40),
            entry("no exit line here", ruling="one"),
            entry("done\nLOOP: plan-done", ruling="two", end=True),
        ])
        self.assertEqual(code, 0, out)
        self.assertEqual(len(calls), 3)
        self.assertEqual([r["exit"] for r in self.rows()], ["unfinished", "plan-done"])

    def test_no_progress_twice_stops(self):
        code, out, calls = self.drive([entry(pct=40), entry("a\nLOOP: continue"),
                                       entry("b\nLOOP: continue"),
                                       entry("c\nLOOP: continue")])
        self.assertEqual(code, 1, out)
        self.assertIn("no progress", out)
        self.assertEqual(len(calls), 3)

    def test_cap(self):
        code, out, calls = self.drive([entry(pct=95)], cap=90.0)
        self.assertEqual(code, 0, out)
        self.assertIn("cap reached", out)
        self.assertEqual(len(calls), 1)
        self.log.unlink()
        code, out, calls = self.drive([{"lines": []}])
        self.assertEqual(code, 0, out)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.rows(), [])
        self.assertTrue(drive.over_cap([40, 50, 60], 70))
        self.assertFalse(drive.over_cap([40, 50, 60], 71))
        self.assertTrue(drive.over_cap([], 90))
        self.assertFalse(drive.over_cap([60, 50], 90))

    def test_stream_reading(self):
        self.assertEqual(drive.stream_reading([json.dumps(rl(44))]), 44.0)
        self.assertIsNone(drive.stream_reading(["not json", json.dumps(res("x"))]))

    def test_hours_kills_a_session(self):
        started = time.time()
        code, out, calls = self.drive([entry(pct=40), {"lines": [rl(41)], "sleep": 60}],
                                      hours=0.001)
        self.assertEqual(code, 0, out)
        self.assertLess(time.time() - started, 20)
        self.assertEqual([r["exit"] for r in self.rows()], ["killed"])
        self.assertIn("time limit", out)

    def test_session_limit(self):
        code, out, calls = self.drive([entry(pct=40), entry("a\nLOOP: continue", ruling="r")],
                                      max_sessions=1)
        self.assertEqual(code, 0, out)
        self.assertIn("session limit", out)
        self.assertEqual(len(calls), 2)

    def test_compact_pct_range(self):
        for bad in ("10", "95", "x"):
            self.log.unlink(missing_ok=True)
            code, out, calls = self.drive([entry(pct=40)], compact_pct=bad)
            self.assertEqual((code, calls), (1, []), bad)
        code, out, calls = self.drive([entry(pct=40), entry("a\nLOOP: plan-done", end=True)],
                                      compact_pct="auto")
        self.assertEqual(calls[1]["pct"], "50")

    def test_cap_uses_the_largest_rise_across_the_run(self):
        code, out, calls = self.drive([
            entry(pct=40), entry("a\nLOOP: continue", pct=55, ruling="r1"),
            entry("b\nLOOP: continue", pct=60, ruling="r2"),
            entry("c\nLOOP: continue", pct=60, ruling="r3")], cap=75.0)
        self.assertEqual(code, 0, out)
        self.assertEqual(len(calls), 3)
        self.assertIn("cap reached: 7d at 60%, cap 75% (--cap), last step", out)

    def test_no_event_in_a_session_triggers_a_probe(self):
        code, out, calls = self.drive([entry(pct=40),
                                       entry("x\nLOOP: stop r", pct=None, ruling="r")])
        self.assertEqual(code, 0, out)
        self.assertEqual(len(calls), 3)
        self.assertIn("7d 40%", out)

    def test_auto_reads_only_this_projects_rows(self):
        for project, peak in (("other", 95.0), (self.w.root.name, 5.0)):
            for _ in range(20):
                drive_log.append(self.w.home, {"project": project, "peak_pct": peak})
        code, out, calls = self.drive([entry(pct=40), entry("a\nLOOP: stop r", ruling="r")],
                                      compact_pct="auto")
        self.assertEqual(calls[1]["pct"], "20")

    def test_compact_pct_refuses_inf_and_nan(self):
        for bad in ("inf", "nan", "-inf"):
            self.log.unlink(missing_ok=True)
            code, out, calls = self.drive([entry(pct=40)], compact_pct=bad)
            self.assertEqual((code, calls), (1, []), bad)

    def test_stop_must_be_the_word(self):
        code, out, calls = self.drive([entry(pct=40), entry("x\nLOOP: stopx", ruling="r"),
                                       entry("y\nLOOP: stop", ruling="s")])
        self.assertEqual([r["exit"] for r in self.rows()], ["unfinished", "stop"])

    def test_plan_done_not_confirmed_runs_another_session(self):
        code, out, calls = self.drive([
            entry(pct=40), entry("x\nLOOP: plan-done", ruling="r"),
            entry("y\nLOOP: plan-done", ruling="s", end=True)])
        self.assertEqual(code, 0, out)
        self.assertEqual(len(calls), 3)
        self.assertIn("plan-done not confirmed:", out)
        self.assertEqual([r["exit"] for r in self.rows()], ["continue", "plan-done"])

    def test_missing_claude_exits_1(self):
        code, out, calls = self.drive([entry(pct=40)], claude=str(self.tmp / "no" / "such"))
        self.assertEqual((code, calls), (1, []))
        self.assertFalse((drive_log.log_path(self.w.home).parent / "runs").exists())

    def test_probe_failure_exits_1(self):
        failing = self.tmp / "failing.py"
        failing.write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"HOME": str(self.w.home)}), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = drive.run(self.w.context(), "plan.md", claude=str(failing))
        self.assertEqual(code, 1)
        self.assertIn("error: usage probe failed: exit status 3", err.getvalue())
        self.assertFalse((drive_log.log_path(self.w.home).parent / "runs").exists())

    def test_no_reading_says_cap_unknown(self):
        code, out, calls = self.drive([{"lines": []}])
        self.assertEqual(code, 0, out)
        self.assertIn("cap unknown: no usage reading", out)
        self.assertEqual(len(calls), 1)
        self.log.unlink()
        code, out, calls = self.drive([entry(pct=95)], cap=90.0)
        self.assertIn("cap reached: 7d at 95%, cap 90% (--cap), before the first session", out)

    @unittest.skipIf(os.name == "nt", "process groups and signals are POSIX")
    def test_hours_kills_a_grandchild_that_ignores_term(self):
        started = time.time()
        code, out, calls = self.drive([entry(pct=40), {"lines": [rl(41)], "sleep": 60,
                                                       "grandchild": 100}], hours=0.002)
        self.assertEqual(code, 0, out)
        self.assertLess(time.time() - started, 30)
        pids = [int(x) for x in (self.tmp / "fake.pids").read_text(encoding="utf-8").split()]
        self.addCleanup(lambda: [self.reap(p) for p in pids])
        self.assertTrue(self.gone(pids[0]))

    @staticmethod
    def alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def gone(self, pid: int, within: float = 5.0) -> bool:
        end = time.time() + within
        while time.time() < end:
            if not self.alive(pid):
                return True
            time.sleep(0.1)
        return not self.alive(pid)

    def reap(self, pid: int) -> None:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    @unittest.skipIf(os.name == "nt", "process groups and signals are POSIX")
    def test_sigterm_to_the_driver_ends_the_session(self):
        path = self.tmp / "script.json"
        path.write_text(json.dumps([entry(pct=40), {"lines": [rl(41)], "sleep": 120}]),
                        encoding="utf-8")
        env = {**os.environ, "HOME": str(self.w.home), "FAKE_SCRIPT": str(path),
               "FAKE_LOG": str(self.log), "FAKE_PID": str(self.tmp / "fake.pids"),
               "CONTEXT_GATE_NO_UPDATE_CHECK": "1"}
        shim = self.w.root / ".context-gate" / "bin" / "govern"
        proc = subprocess.Popen([sys.executable, str(shim), "build", "drive", "plan.md",
                                 "--cap", "90", "--claude", str(self.fake)], cwd=str(self.w.root), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(proc.kill)
        end = time.time() + 30
        while time.time() < end and (not self.log.exists() or
                                     len(self.log.read_text(encoding="utf-8").splitlines()) < 2):
            time.sleep(0.2)
        pid = json.loads(self.log.read_text(encoding="utf-8").splitlines()[1])["pid"]
        self.addCleanup(self.reap, pid)
        time.sleep(1)
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 130, err)
        self.assertIn("interrupted", out)
        self.assertTrue(self.gone(pid, 15))

    def status(self) -> dict:
        found = list((drive_log.log_path(self.w.home).parent / "runs").glob("*/status.json"))
        self.assertEqual(len(found), 1)
        return json.loads(found[0].read_text(encoding="utf-8"))

    KEYS = {"pid", "project", "cwd", "plan", "cap", "cap_source", "hours", "started", "state", "session",
            "session_started", "seven_day", "last", "next", "units", "stop_reason", "updated",
            "skill", "doing", "doing_since", "owner_session"}

    def test_status_file_after_plan_done(self):
        code, out, calls = self.drive([
            entry(pct=40),
            entry("did a step\nLOOP: continue", ruling="step one"),
            entry("all closed\nLOOP: plan-done", ruling="step two", end=True)])
        self.assertEqual(code, 0, out)
        runs = drive_log.log_path(self.w.home).parent / "runs"
        self.assertIn(f"run folder: {next(runs.iterdir())}\n", out)
        doc = self.status()
        self.assertEqual(set(doc), self.KEYS)
        self.assertEqual((doc["state"], doc["stop_reason"], doc["session"]),
                         ("stopped", "plan done", 2))
        self.assertEqual(doc["last"]["kind"], "plan-done")
        self.assertEqual(doc["project"], self.w.root.name)
        self.assertEqual(doc["cwd"], str(self.w.root))
        self.assertEqual(doc["plan"], "plan.md")
        self.assertEqual(doc["cap"], 90.0)
        self.assertIsNone(doc["hours"])
        self.assertEqual(doc["seven_day"], 40.0)
        self.assertIsNotNone(doc["session_started"])
        self.assertTrue(doc["updated"])
        self.assertFalse(next(runs.iterdir()).joinpath("status.json.tmp").exists())

    def test_status_stop_reasons(self):
        cases = [
            ("cap reached", [entry(pct=95)], {}),
            ("session limit", [entry(pct=40), entry("a\nLOOP: continue", ruling="r")],
             {"max_sessions": 1}),
            ("time limit", [entry(pct=40), {"lines": [rl(41)], "sleep": 60}], {"hours": 0.001}),
            ("stop: need the owner", [entry(pct=40), entry("x\nLOOP: stop need the owner",
                                                           ruling="r")], {}),
            ("no progress", [entry(pct=40), entry("a\nLOOP: continue"),
                             entry("b\nLOOP: continue"), entry("c\nLOOP: continue")], {}),
            ("cap unknown: no usage reading", [{"lines": []}], {}),
        ]
        for reason, script, kw in cases:
            with self.subTest(reason):
                shutil.rmtree(drive_log.log_path(self.w.home).parent / "runs", ignore_errors=True)
                self.log.unlink(missing_ok=True)
                code, out, calls = self.drive(script, **kw)
                doc = self.status()
                self.assertEqual(doc["state"], "stopped")
                self.assertTrue(doc["stop_reason"].startswith(reason), doc["stop_reason"])
                self.assertIn(doc["stop_reason"], out)

    def test_status_says_in_session_while_the_session_runs(self):
        seen = self.tmp / "seen.json"
        code, out, calls = self.drive([
            entry(pct=40),
            entry("x\nLOOP: stop r", ruling="r", snapshot=str(seen))])
        self.assertEqual(code, 0, out)
        doc = json.loads(seen.read_text(encoding="utf-8"))
        self.assertEqual((doc["state"], doc["session"]), ("in_session", 1))
        self.assertIsNotNone(doc["session_started"])
        self.assertIsNone(doc["last"])

    def test_status_says_interrupted(self):
        with mock.patch.object(drive, "_session", side_effect=drive._Interrupted):
            code, out, calls = self.drive([entry(pct=40)])
        self.assertEqual(code, 130)
        doc = self.status()
        self.assertEqual((doc["state"], doc["stop_reason"]), ("stopped", "interrupted"))
        self.assertEqual(doc["session"], 1)

    def test_a_stop_before_the_run_folder_writes_nothing(self):
        code, out, calls = self.drive([entry(pct=40)], compact_pct="5")
        self.assertEqual(code, 1)
        self.assertFalse((drive_log.log_path(self.w.home).parent / "runs").exists())

    def test_roles_check_render_check_that_cannot_start_exits_1(self):
        err = io.StringIO()
        real = subprocess.run

        def cannot_start(argv, *a, **kw):
            if "render" in argv:
                raise OSError("no such interpreter")
            return real(argv, *a, **kw)

        with mock.patch.object(drive.subprocess, "run", cannot_start), \
                contextlib.redirect_stderr(err):
            code, out, calls = self.drive([entry(pct=40)])
        self.assertEqual((code, calls), (1, []))
        self.assertIn("agents render --check failed", err.getvalue())
        self.assertIn("no such interpreter", err.getvalue())
        self.assertNotIn("are stale", err.getvalue())

    def test_roles_check_a_failing_check_without_changes_is_not_called_stale(self):
        err = io.StringIO()
        real = subprocess.run

        def broken(argv, *a, **kw):
            if "render" in argv:
                return subprocess.CompletedProcess(argv, 2, "", "Traceback\nboom\n")
            return real(argv, *a, **kw)

        with mock.patch.object(drive.subprocess, "run", broken), \
                contextlib.redirect_stderr(err):
            code, out, calls = self.drive([entry(pct=40)])
        self.assertEqual((code, calls), (1, []))
        self.assertIn("error: agent roles: python3 .context-gate/bin/govern agents render "
                      "--check failed\nboom\n", err.getvalue())
        self.assertNotIn("are stale", err.getvalue())

    def test_roles_check_rendered_roles_let_the_driver_start(self):
        config = self.w.root / layout.CONFIG
        config.write_text(config.read_text(encoding="utf-8")
                          + '\n[checks.agent-roles]\nlevel = "warn"\n', encoding="utf-8")
        code, out, err = self.w.run("agents", "render")
        self.assertEqual(code, 0, out + err)
        code, out, calls = self.drive([entry(pct=40), entry("x\nLOOP: stop r", ruling="r")])
        self.assertEqual(code, 0, out)
        self.assertEqual(len(calls), 2)

    NOON = datetime(2026, 7, 15, 12, 0, 0)

    def seed_week(self, start=44.0, weight_today=None, reset=True):
        """A home whose window resets at midnight three days on (three days of weight 1 are left, so
        the allowance is (100 - start) / 3), with `start` as the figure when today began."""
        cap = usage_week.load_capture(self.w.home)
        today = self.NOON.date()
        r = int(cap.midnight(today + timedelta(days=3)))
        state = self.w.home / ".local" / "state" / "context-gate" / "usage"
        state.mkdir(parents=True, exist_ok=True)
        (state / "days.json").write_text(json.dumps(
            {"": {today.isoformat(): [[r, start, start]]}}), encoding="utf-8")
        if reset:
            (state / "sess.json").write_text(json.dumps(
                {"rate_limits": {"seven_day": {"resets_at": r}}}), encoding="utf-8")
        if weight_today is not None:
            share = self.w.home / ".local" / "share" / "context-gate"
            share.mkdir(parents=True, exist_ok=True)
            day = cap.DAY_NAMES[today.weekday()]
            (share / "usage-week.toml").write_text(
                f"[weekly]\nweights = {{ {day} = {weight_today} }}\n", encoding="utf-8")
        return r

    def drive_today(self, script, clock=None, **kw):
        """`drive` with no cap; the clock reads each of `clock`, then stays on the last."""
        times = list(clock or [self.NOON.timestamp()])
        with mock.patch("govern.drive._now", side_effect=lambda: times.pop(0) if len(times) > 1
                        else times[0]):
            return self.drive(script, cap=None, **kw)

    def test_default_cap_is_todays_line(self):
        self.seed_week(start=40.0)
        code, out, calls = self.drive_today([
            entry(pct=50), entry("a\nLOOP: continue", pct=50, ruling="r1"),
            entry("b\nLOOP: continue", pct=60, ruling="r2")])
        self.assertEqual(code, 0, out)
        self.assertEqual(len(calls), 3)
        self.assertIn("session 1: continue", out)
        self.assertIn("7d 50%, cap 60% (today)", out)
        self.assertIn("cap reached: 7d at 60%, cap 60% (today), last step", out)
        doc = self.status()
        self.assertEqual((doc["cap"], doc["cap_source"]), (60.0, "today"))

    def test_explicit_cap_wins_and_ignores_the_line(self):
        self.seed_week(reset=False)
        code, out, calls = self.drive([entry(pct=50), entry("a\nLOOP: plan-done", pct=51,
                                                            end=True)], cap=75.0)
        self.assertEqual(code, 0, out)
        self.assertIn("7d 51%, cap 75% (--cap)", out)
        doc = self.status()
        self.assertEqual((doc["cap"], doc["cap_source"]), (75.0, "--cap"))

    def test_a_run_past_midnight_takes_the_new_days_line(self):
        self.seed_week(start=40.0)
        noon = self.NOON.timestamp()
        code, out, calls = self.drive_today([
            entry(pct=50), entry("a\nLOOP: continue", pct=50, ruling="r1"),
            entry("b\nLOOP: plan-done", pct=70, ruling="r2", end=True)],
            clock=[noon, noon + 86400, noon + 86400])
        self.assertEqual(code, 0, out)
        self.assertIn("7d 50%, cap 60% (today)", out)
        self.assertIn("7d 70%, cap 75% (today)", out)

    def test_an_off_day_stops_as_cap_unknown(self):
        self.seed_week(weight_today=0)
        code, out, calls = self.drive_today([entry(pct=50)])
        self.assertEqual((code, len(calls)), (0, 1))
        self.assertIn("cap unknown: off day", out)
        doc = self.status()
        self.assertEqual((doc["cap"], doc["cap_source"], doc["stop_reason"]),
                         (None, "today", "cap unknown: off day"))

    def test_no_reset_time_stops_as_cap_unknown(self):
        self.seed_week(reset=False)
        code, out, calls = self.drive_today([entry(pct=50)])
        self.assertEqual((code, len(calls)), (0, 1))
        self.assertIn("cap unknown: no reset time", out)

    def test_no_reading_stops_as_cap_unknown(self):
        self.seed_week()
        code, out, calls = self.drive_today([{"lines": []}])
        self.assertIn("cap unknown: no usage reading", out)

    def test_stream_reset_reads_the_window_next_to_the_utilization(self):
        def event(**week):
            return json.dumps({"type": "rate_limit_event", "rate_limit_info": {
                "unifiedWindows": {"seven_day": {"utilization": 0.4, **week}}}})
        self.assertEqual(drive.stream_reset([event(resets_at=1800000000)]), 1800000000.0)
        self.assertEqual(drive.stream_reset([event(resetsAt=1800000000000)]), 1800000000.0)
        self.assertIsNone(drive.stream_reset([event()]))
        self.assertIsNone(drive.stream_reset(["not json"]))

    def init(self, commands) -> dict:
        return {"type": "system", "subtype": "init", "slash_commands": commands}

    def test_adopter_beta_engine_with_only_the_stable_plugin_loaded(self):
        """The probe's init lists only context-gate:build: the session asks for that one."""
        with mock.patch("govern.__version__", "0.9.5-beta.2"):
            code, out, calls = self.drive([
                {"lines": [self.init(["compact", "context-gate:build"]), rl(40)]},
                entry("x\nLOOP: plan-done", end=True)])
        self.assertEqual(code, 0, out)
        argv = calls[1]["argv"]
        self.assertEqual(argv[argv.index("-p") + 1], "/context-gate:build plan.md")
        self.assertEqual(self.status()["skill"], "context-gate")

    def test_no_build_skill_loaded_exits_1_before_a_session(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code, out, calls = self.drive([{"lines": [self.init(["compact"]), rl(40)]}])
        self.assertEqual(code, 1)
        self.assertEqual(err.getvalue(),
                         "error: no context-gate build skill is loaded in this project (looked "
                         "for context-gate:build, context-gate-beta:build)\n")
        self.assertEqual(len(calls), 1)
        self.assertFalse(drive_log.rows(self.w.home))

    def test_session_is_named_for_the_project(self):
        code, out, calls = self.drive([entry(pct=40), entry("x\nLOOP: plan-done", end=True)])
        argv = calls[1]["argv"]
        self.assertEqual(argv[argv.index("--name") + 1], f"drive-{self.w.root.name}-s1")

    def tools(self, *descriptions) -> list[dict]:
        def block(name, **inp):
            return {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "hi"}, {"type": "tool_use", "name": name, "input": inp}]}}
        return [block("Bash", description=descriptions[0]),
                block("Agent", description=descriptions[1]), block("Skill", skill=descriptions[2])]

    def test_status_shows_what_the_session_is_doing(self):
        seen = self.tmp / "seen.json"
        lines = self.tools("run tests", "x" * 200, "context-gate:build")
        with mock.patch.object(drive, "DOING_EVERY", 0):
            code, out, calls = self.drive([entry(pct=40), {
                "lines": lines, "snapshot_after": 0.3, "snapshot_after_to": str(seen)},
                entry("x\nLOOP: stop r", ruling="r")])
        doc = json.loads(seen.read_text(encoding="utf-8"))
        self.assertEqual(doc["doing"], "context-gate:build")
        self.assertTrue(doc["doing_since"])
        self.assertEqual(self.status()["doing"], None)
        self.assertEqual(self.status()["doing_since"], None)
        with mock.patch.object(drive, "DOING_EVERY", 0):
            self.log.unlink()
            shutil.rmtree(drive_log.log_path(self.w.home).parent / "runs")
            code, out, calls = self.drive([entry(pct=40), {
                "lines": lines[1:2], "snapshot_after": 0.3, "snapshot_after_to": str(seen)},
                entry("x\nLOOP: stop r", ruling="r")])
        self.assertEqual(json.loads(seen.read_text(encoding="utf-8"))["doing"], "x" * 120)

    def test_doing_is_throttled(self):
        seen = self.tmp / "seen.json"
        with mock.patch.object(drive, "DOING_EVERY", 3600):
            code, out, calls = self.drive([entry(pct=40), {
                "lines": self.tools("first", "second", "third"), "snapshot_after": 0.3,
                "snapshot_after_to": str(seen)}, entry("x\nLOOP: stop r", ruling="r")])
        self.assertEqual(json.loads(seen.read_text(encoding="utf-8"))["doing"], "first")

    def test_a_held_back_step_is_written_by_time(self):
        seen = self.tmp / "seen.json"
        with mock.patch.object(drive, "DOING_EVERY", 1.0):
            code, out, calls = self.drive([entry(pct=40), {
                "lines": self.tools("first", "second", "third"), "snapshot_after": 1.5,
                "snapshot_after_to": str(seen)}, entry("x\nLOOP: stop r", ruling="r")])
        self.assertEqual(json.loads(seen.read_text(encoding="utf-8"))["doing"], "third")

    def test_doing_since_is_the_time_the_step_was_seen(self):
        status = drive._Status(mock.Mock(root=self.tmp), "plan.md", None, None)
        status.begin(self.tmp, {})
        status.doing("x", since=1_700_000_000)
        doc = json.loads((self.tmp / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(doc["doing_since"], "2023-11-14T22:13:20Z")
        status.doing(None)
        doc = json.loads((self.tmp / "status.json").read_text(encoding="utf-8"))
        self.assertIsNone(doc["doing_since"])

    def test_status_records_the_owner_session(self):
        for given, want in (("abc", "abc"), ("", None)):
            with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": given}):
                self.assertEqual(drive._Status(mock.Mock(root=self.tmp), "p", None, None).doc["owner_session"], want)
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertIsNone(drive._Status(mock.Mock(root=self.tmp), "p", None, None).doc["owner_session"])

    def test_a_late_step_never_follows_the_return_of_the_session(self):
        script = ("import json,sys,time\n"
                  "print(json.dumps({'type':'assistant','message':{'content':["
                  "{'type':'tool_use','name':'Bash','input':{'description':'one'}}]}}),flush=True)\n"
                  "print(json.dumps({'type':'assistant','message':{'content':["
                  "{'type':'tool_use','name':'Bash','input':{'description':'two'}}]}}),flush=True)\n")
        got = []
        with mock.patch.object(drive, "DOING_EVERY", 0.1):
            drive._session([sys.executable, "-c", script], dict(os.environ), self.tmp,
                           self.tmp / "o.jsonl", self.tmp / "o.err", None,
                           lambda text, since: got.append((text, time.monotonic())))
            returned = time.monotonic()
            time.sleep(0.4)
        self.assertTrue(got)
        self.assertEqual([t for t, when in got if when > returned], [])
        self.assertEqual([t.name for t in threading.enumerate() if t.name == "drive-doing"], [])

    def test_progress_names_the_phase(self):
        self.assertEqual(drive._Status.progress(
            {"phase": "p1", "done": 2, "total": 5, "next": "x"}),
            {"next": "x", "units": "p1 \u00b7 2/5 units"})
        self.assertIsNone(drive._Status.progress(
            {"phase": None, "done": 2, "total": 5, "next": "x"})["units"])

    def test_timer_keeps_units_live_and_stops_with_the_session(self):
        seen = self.tmp / "seen.json"
        real = drive._status
        calls_made = []

        def fake_status(ctx, project):
            if not calls_made:
                calls_made.append(real(ctx, project))
                return calls_made[0]
            got = calls_made[0]
            if got is None:
                return got
            return got[0], {**got[1], "phase": "ph", "done": 7, "total": 9}

        with mock.patch.object(drive, "STATUS_EVERY", 0.1), \
                mock.patch.object(drive, "_status", fake_status):
            code, out, calls = self.drive([entry(pct=40), {
                "lines": [], "snapshot_after": 1.5, "snapshot_after_to": str(seen),
                "sleep": 0.2}, entry("x\nLOOP: stop r", ruling="r")])
        self.assertEqual(json.loads(seen.read_text(encoding="utf-8"))["units"],
                         "ph \u00b7 7/9 units")
        self.assertEqual([t.name for t in threading.enumerate() if t.name == "drive-status"], [])

    def test_cli_drive_report(self):
        code, out, err = self.w.run("build", "drive-report")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("all projects", out)
        code, out, err = self.w.run("build", "drive", "--compact-pct", "5", "plan.md")
        self.assertEqual(code, 1)
        self.assertIn("--compact-pct", err)


if __name__ == "__main__":
    unittest.main()
