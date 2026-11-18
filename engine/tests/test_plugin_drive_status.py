"""plugin/hooks/drive_status.py: the drive band's reading of a run's status.json.

    python3 -m unittest discover -s engine/tests -p 'test_plugin_drive_status.py'
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import profile_guard  # noqa: E402,F401

SCRIPT = Path(__file__).resolve().parent.parent.parent / "plugin" / "hooks" / "drive_status.py"


def utc(ago: float = 0) -> str:
    return datetime.fromtimestamp(time.time() - ago, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class DriveStatus(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name).resolve()
        self.addCleanup(self._tmp.cleanup)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.root = self.tmp / "demo"
        (self.root / ".context-gate").mkdir(parents=True)
        self.runs = self.home / ".local" / "state" / "context-gate" / "drive" / "runs"

    def status(self, stamp="20261118T183000Z", name="demo", cwd=None, **over) -> Path:
        doc = {"pid": os.getpid(), "project": name, "cwd": str(cwd or self.root), "plan": "p",
               "cap": 50.4, "hours": 5, "started": utc(900), "state": "between", "session": 2,
               "session_started": None, "seven_day": 31.6, "last": None, "next": "u3",
               "units": "3/9 units done", "stop_reason": None, "updated": utc(1)}
        doc.update(over)
        folder = self.runs / f"{name}-{stamp}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "status.json").write_text(json.dumps(doc), encoding="utf-8")
        return folder

    def run_script(self, *args: str) -> dict:
        env = {**os.environ, "HOME": str(self.home), "USERPROFILE": str(self.home)}
        res = subprocess.run([sys.executable, str(SCRIPT), *args], env=env, cwd=self.tmp,
                             capture_output=True, text=True)
        self.assertEqual((res.returncode, res.stderr), (0, ""))
        return json.loads(res.stdout)

    def read(self) -> dict:
        return self.run_script(str(self.root))

    def dead_pid(self) -> int:
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        return proc.pid

    def test_running_in_session(self):
        self.status(state="in_session", session_started=utc(65), last={"kind": "continue", "seconds": 720, "peak_pct": 40})
        got = self.read()
        self.assertTrue(got["running"] and got["in_session"] and not got["stopped"])
        self.assertAlmostEqual(got["elapsed_s"], 65, delta=2)
        self.assertEqual((got["project"], got["session"], got["seven_day"], got["cap"], got["units"]),
                         ("demo", 2, 32, 50, "3/9 units done"))
        self.assertEqual(got["last"], "continue 12m")

    def test_between_sessions(self):
        self.status()
        got = self.read()
        self.assertTrue(got["running"])
        self.assertFalse(got["in_session"])
        self.assertIsNone(got["elapsed_s"])
        self.assertEqual(got["next"], "u3")

    def test_walks_up_from_a_subfolder(self):
        self.status()
        sub = self.root / "a" / "b"
        sub.mkdir(parents=True)
        self.assertEqual(self.run_script(str(sub))["project"], "demo")

    def test_stopped_recently_is_shown_not_running(self):
        self.status(state="stopped", updated=utc(60), stop_reason="done")
        got = self.read()
        self.assertFalse(got["running"])
        self.assertTrue(got["stopped"])

    def test_stopped_long_ago_is_hidden(self):
        self.status(state="stopped", updated=utc(660))
        self.assertEqual(self.read(), {})

    def test_dead_pid_in_session_is_not_running(self):
        self.status(state="in_session", session_started=utc(30), updated=utc(5), pid=self.dead_pid(),
                    last={"kind": "fresh", "seconds": 30, "peak_pct": 1})
        got = self.read()
        self.assertFalse(got["running"])
        self.assertFalse(got["stopped"])
        self.assertEqual(got["last"], "fresh 30s")

    def test_dead_pid_long_ago_is_hidden(self):
        self.status(state="in_session", updated=utc(660), pid=self.dead_pid())
        self.assertEqual(self.read(), {})

    def test_no_run_folder(self):
        self.assertEqual(self.read(), {})

    def test_no_governed_project(self):
        bare = self.tmp / "bare"
        bare.mkdir()
        self.assertEqual(self.run_script(str(bare)), {})

    def test_other_project_with_same_prefix_is_ignored(self):
        other = self.tmp / "elsewhere"
        other.mkdir()
        self.status(stamp="20261119T000000Z", cwd=other)
        self.status(stamp="20261118T000000Z", session=7)
        self.assertEqual(self.read()["session"], 7)

    def test_half_written_status_gives_empty(self):
        folder = self.status()
        (folder / "status.json").write_text('{"pid": 1, "proj', encoding="utf-8")
        self.assertEqual(self.read(), {})

    def test_newest_of_two_runs_is_read(self):
        self.status(stamp="20261118T100000Z", session=1)
        self.status(stamp="20261118T200000Z", session=9)
        self.assertEqual(self.read()["session"], 9)

    def test_last_formatting(self):
        self.status(last={"kind": "continue", "seconds": 30, "peak_pct": 1})
        self.assertEqual(self.read()["last"], "continue 30s")
        self.status(last={"kind": "continue", "seconds": 720, "peak_pct": 1})
        self.assertEqual(self.read()["last"], "continue 12m")

    def test_cap_source_passes_through(self):
        self.status(cap_source="--cap")
        self.assertEqual(self.read()["cap_source"], "--cap")
        self.status()
        self.assertNotIn("cap_source", self.read())

    def test_dead_pid_without_updated_is_hidden(self):
        self.status(state="in_session", pid=self.dead_pid(), updated=None)
        self.assertEqual(self.read(), {})
        self.status(state="in_session", pid=self.dead_pid(), updated="not a time")
        self.assertEqual(self.read(), {})

    def test_stopped_without_updated_is_hidden(self):
        self.status(state="stopped", updated=None)
        self.assertEqual(self.read(), {})

    def test_home_is_read_before_userprofile(self):
        other = self.tmp / "profile"
        other.mkdir()
        env = {**os.environ, "HOME": str(self.home), "USERPROFILE": str(other)}
        res = subprocess.run(
            [sys.executable, "-c",
             "import importlib.util as u,sys;"
             f"s=u.spec_from_file_location('ds',{str(SCRIPT)!r});m=u.module_from_spec(s);s.loader.exec_module(m);"
             "print(m.home_dir())"],
            env=env, cwd=self.tmp, capture_output=True, text=True)
        self.assertEqual((res.returncode, res.stdout.strip()), (0, str(self.home)))
        self.status()
        env2 = {**env, "USERPROFILE": str(other)}
        res = subprocess.run([sys.executable, str(SCRIPT), str(self.root)], env=env2, cwd=self.tmp,
                             capture_output=True, text=True)
        self.assertEqual(json.loads(res.stdout)["project"], "demo")


if __name__ == "__main__":
    unittest.main()
