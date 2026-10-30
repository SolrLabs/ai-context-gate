"""The usage writer is never downgraded by an older engine, and the history is kept per Claude
profile folder (CLAUDE_CONFIG_DIR), so two accounts on one machine do not mix."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import profile_guard  # noqa: E402,F401

from govern import __version__, usage_setup  # noqa: E402
from test_usage_capture import load_capture  # noqa: E402

R = 1790499600
NOW = R - 7200


def stamp_line(version: str) -> str:
    return f"# context-gate capture {version}"


class Install(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.profile = Path(self._tmp.name) / "profile"
        self.profile.mkdir()
        env = mock.patch.dict(os.environ, {"HOME": str(self.home), "CLAUDE_CONFIG_DIR": str(self.profile)})
        env.start()
        self.addCleanup(env.stop)
        self.path = usage_setup.capture_path(self.home)

    def installed(self, text):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(text, encoding="utf-8")

    def statusline(self):
        data = json.loads((self.profile / "settings.json").read_text(encoding="utf-8"))
        return data["statusLine"]

    def test_a_newer_installed_writer_is_kept(self):
        self.installed(f'"""old"""\n{stamp_line("9.9.9")}\nmarker = 1\n')
        before = self.path.read_bytes()
        msg = usage_setup.install(self.home, python="/usr/bin/python3")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(msg.startswith(
            f"capture.py is from 9.9.9, newer than this engine ({__version__}): kept; "), msg)
        self.assertIn(usage_setup.MARK, self.statusline()["command"].replace("\\", "/"))

    def test_an_older_stamp_is_replaced_and_carries_this_engine_stamp(self):
        self.installed(f'"""old"""\n{stamp_line("0.0.1")}\nmarker = 1\n')
        msg = usage_setup.install(self.home, python="/usr/bin/python3")
        text = self.path.read_text(encoding="utf-8")
        self.assertNotIn("marker = 1", text)
        self.assertEqual(usage_setup.CAPTURE_STAMP_RE.findall(text), [__version__])
        self.assertNotIn("kept", msg)

    def test_no_stamp_is_replaced(self):
        self.installed('"""old"""\nmarker = 1\n')
        usage_setup.install(self.home, python="/usr/bin/python3")
        text = self.path.read_text(encoding="utf-8")
        self.assertNotIn("marker = 1", text)
        self.assertEqual(usage_setup.CAPTURE_STAMP_RE.findall(text), [__version__])

    def test_the_stamp_follows_the_docstring_and_the_rest_is_the_template(self):
        usage_setup.install(self.home, python="/usr/bin/python3")
        text = self.path.read_text(encoding="utf-8")
        template = usage_setup.TEMPLATE.read_text(encoding="utf-8")
        self.assertEqual(text.replace(stamp_line(__version__) + "\n", "", 1), template)
        lines = text.splitlines()
        i = lines.index(stamp_line(__version__))
        self.assertTrue(lines[i - 1].endswith('"""'))

    def test_an_equal_stamp_is_replaced(self):
        self.installed(f'"""old"""\n{stamp_line(__version__)}\nmarker = 1\n')
        usage_setup.install(self.home, python="/usr/bin/python3")
        self.assertNotIn("marker = 1", self.path.read_text(encoding="utf-8"))


class Profiles(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.top = Path(self._tmp.name)
        self.state = self.top / "state"
        self.profile_a = str(self.top / "profile-a")
        self.profile_b = str(self.top / "profile-b")

    def cap(self, profile):
        """capture.py loaded the way Claude Code runs it for that profile, sharing one history file."""
        env = {"HOME": str(self.top / "home")}
        if profile is None:
            patch = mock.patch.dict(os.environ, env)
            patch.start()
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            patch = mock.patch.dict(os.environ, {**env, "CLAUDE_CONFIG_DIR": profile})
            patch.start()
        try:
            mod = load_capture()
        finally:
            patch.stop()
        mod.STATE = self.state
        mod.HISTORY = self.state / "history.jsonl"
        return mod

    def lines(self):
        text = (self.state / "history.jsonl").read_text(encoding="utf-8")
        return [json.loads(x) for x in text.splitlines()]

    @staticmethod
    def payload5(u):
        return {"rate_limits": {"five_hour": {"used_percentage": u, "resets_at": R}}}

    def test_another_profile_is_not_behind_the_first(self):
        a, b = self.cap(self.profile_a), self.cap(self.profile_b)
        a.record(self.payload5(40), NOW)
        b.record(self.payload5(10), NOW + 5)
        self.assertEqual([(x.get("p"), x["u"]) for x in self.lines()],
                         [(a.PROFILE, 40), (b.PROFILE, 10)])
        self.assertEqual(b.history("5h", R, NOW + 10), [(NOW + 5, 10)])
        self.assertEqual(a.history("5h", R, NOW + 10), [(NOW, 40)])

    def test_the_profile_is_an_absolute_path(self):
        a = self.cap(self.profile_a)
        self.assertEqual(a.PROFILE, os.path.abspath(self.profile_a))

    def test_a_line_with_no_p_is_the_default_profile(self):
        default = self.cap(None)
        self.assertEqual(default.PROFILE, "")
        self.state.mkdir(parents=True)
        (self.state / "history.jsonl").write_text(
            json.dumps({"t": NOW, "w": "5h", "r": R, "u": 30}) + "\n", encoding="utf-8")
        self.assertEqual(default.history("5h", R, NOW + 1), [(NOW, 30)])
        self.assertEqual(self.cap(self.profile_a).history("5h", R, NOW + 1), [])
        default.record(self.payload5(35), NOW + 5)
        self.assertNotIn("p", self.lines()[-1])

    def test_the_default_profile_records_no_p(self):
        default = self.cap(None)
        default.record(self.payload5(40), NOW)
        self.assertEqual(self.lines(), [{"t": NOW, "w": "5h", "r": R, "u": 40}])

    def test_the_compaction_keeps_p(self):
        a = self.cap(self.profile_a)
        self.state.mkdir(parents=True)
        (self.state / "history.jsonl").write_text(
            "".join(json.dumps({"t": NOW - 100 + i % 50, "w": "5h", "r": R, "u": 10, "p": a.PROFILE}) + "\n"
                    for i in range(510)), encoding="utf-8")
        a.record(self.payload5(50), NOW)
        rows = self.lines()
        self.assertEqual(len(rows), 511)
        self.assertTrue(all(x.get("p") == a.PROFILE for x in rows))


if __name__ == "__main__":
    unittest.main()
