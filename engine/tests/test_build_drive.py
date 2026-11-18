"""`govern build drive`: the loop that starts one headless coordinator session per build step.
A fake `claude` (a Python file) prints scripted stream lines and may change the ledger.

    python3 -m unittest engine.tests.test_build_drive
"""
from __future__ import annotations

import contextlib
import io
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import profile_guard  # noqa: E402,F401

from build_support import *  # noqa: E402,F401,F403
from govern import drive, drive_log  # noqa: E402

FAKE = '''\
import json, os, subprocess, sys, time
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
for line in entry.get("lines", []):
    print(json.dumps(line), flush=True)
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
        self.w = self.started()
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
               "FAKE_PID": str(self.tmp / "fake.pids"), "CONTEXT_GATE_NO_UPDATE_CHECK": "1"}
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out):
            ctx = self.w.context()
            code = drive.run(ctx, "plan.md", **{"claude": str(self.fake), **kw})
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
        with mock.patch("govern.__version__", "0.9.5"):
            self.assertEqual(drive.skill_command("p.md"), "/context-gate:build p.md")
        with mock.patch("govern.__version__", "0.9.5-beta.2"):
            self.assertEqual(drive.skill_command("p.md"), "/context-gate-beta:build p.md")
        code, out, calls = self.drive([entry(pct=40), entry("x\nLOOP: plan-done", end=True)])
        argv = calls[1]["argv"]
        self.assertEqual(argv[argv.index("-p") + 1], "/context-gate:build plan.md")

    def test_stale_roles_exit_1_before_any_session(self):
        err = io.StringIO()
        real = subprocess.run

        def fail_check(argv, *a, **kw):
            if "render" in argv:
                return subprocess.CompletedProcess(argv, 1, "", "roles differ\n")
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
                         ["1.err", "1.jsonl", "2.err", "2.jsonl"])

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
        self.assertIn("cap reached: 7d at 60%, last step", out)

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
        self.assertIn("cap reached: 7d at 95%, before the first session", out)

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
                                 "--claude", str(self.fake)], cwd=str(self.w.root), env=env,
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

    def test_cli_drive_report(self):
        code, out, err = self.w.run("build", "drive-report")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("all projects", out)
        code, out, err = self.w.run("build", "drive", "--compact-pct", "5", "plan.md")
        self.assertEqual(code, 1)
        self.assertIn("--compact-pct", err)


if __name__ == "__main__":
    unittest.main()
