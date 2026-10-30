"""govern usage row on / off / status, and how install and uninstall treat the row file."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))

sys.path.insert(0, str(Path(__file__).resolve().parent))

import profile_guard  # noqa: E402,F401

import govern.checks  # noqa: E402,F401  (registers the built-in checks)
from govern import cli, usage_setup  # noqa: E402
from test_usage_setup import THEIRS, Base  # noqa: E402


class Row(Base):
    def row_file(self):
        return usage_setup.row_path(self.home)

    def test_on_off_status_after_install(self):
        self.settings({"statusLine": THEIRS})
        usage_setup.install(self.home)
        code, msg = usage_setup.row(self.home, "on")
        self.assertEqual(code, 0)
        self.assertIn("status row on", msg)
        self.assertEqual(self.row_file().read_text(encoding="utf-8"), '{"row": true}')
        self.assertEqual(usage_setup.row(self.home, "status"), (0, "row on"))
        code, msg = usage_setup.row(self.home, "off")
        self.assertEqual(code, 0)
        self.assertIn("status row off", msg)
        self.assertFalse(self.row_file().exists())
        self.assertEqual(usage_setup.row(self.home, "status"), (0, "row off"))
        self.assertEqual(usage_setup.row(self.home, "off")[0], 0)   # missing is fine

    def test_on_before_install_writes_nothing(self):
        self.settings({"statusLine": THEIRS})
        code, msg = usage_setup.row(self.home, "on")
        self.assertEqual(code, 1)
        self.assertIn("govern usage install", msg)
        self.assertFalse(self.row_file().exists())
        code, msg = usage_setup.row(self.home, "status")
        self.assertEqual(code, 0)
        self.assertIn("capture not installed", msg.split("\n"))

    def test_install_keeps_the_row_file_byte_for_byte(self):
        self.settings({"statusLine": THEIRS})
        usage_setup.install(self.home)
        usage_setup.row(self.home, "on")
        before = self.row_file().read_bytes()
        usage_setup.install(self.home)
        self.assertEqual(self.row_file().read_bytes(), before)

    def test_uninstall_removes_the_row_file(self):
        self.settings({"statusLine": THEIRS})
        usage_setup.install(self.home)
        usage_setup.row(self.home, "on")
        usage_setup.uninstall(self.home)
        self.assertFalse(self.row_file().exists())

    def test_uninstall_removes_it_when_the_capture_was_never_installed(self):
        self.settings({"statusLine": THEIRS})
        self.row_file().parent.mkdir(parents=True)
        self.row_file().write_text('{"row": true}', encoding="utf-8")
        self.assertEqual(usage_setup.uninstall(self.home), "usage capture is not installed")
        self.assertFalse(self.row_file().exists())

    def history_file(self):
        return usage_setup.history_path(self.home)

    def test_uninstall_removes_the_shared_history(self):
        self.settings({"statusLine": THEIRS})
        usage_setup.install(self.home)
        self.history_file().parent.mkdir(parents=True, exist_ok=True)
        self.history_file().write_text('{"t": 1}\n', encoding="utf-8")
        snap = self.history_file().parent / "abc.json"
        snap.write_text("{}", encoding="utf-8")
        usage_setup.uninstall(self.home)
        self.assertFalse(self.history_file().exists())
        self.assertTrue(snap.exists())

    def test_uninstall_without_a_history_file_is_fine(self):
        self.settings({"statusLine": THEIRS})
        usage_setup.install(self.home)
        self.assertFalse(self.history_file().exists())
        usage_setup.uninstall(self.home)

    def test_uninstall_removes_history_when_the_capture_was_never_installed(self):
        self.settings({"statusLine": THEIRS})
        self.history_file().parent.mkdir(parents=True, exist_ok=True)
        self.history_file().write_text('{"t": 1}\n', encoding="utf-8")
        self.assertEqual(usage_setup.uninstall(self.home), "usage capture is not installed")
        self.assertFalse(self.history_file().exists())

    def test_row_never_touches_settings_or_its_backup(self):
        self.settings({"model": "opus", "statusLine": THEIRS})
        sp = self.home / ".claude" / "settings.json"
        bak = usage_setup.share_dir(self.home) / "settings.json.bak"
        before = sp.read_bytes()
        for state in ("on", "status", "off"):
            usage_setup.row(self.home, state)
        self.assertEqual(sp.read_bytes(), before)
        self.assertFalse(bak.exists())
        usage_setup.install(self.home)
        before, before_bak = sp.read_bytes(), bak.read_bytes()
        for state in ("on", "status", "off"):
            usage_setup.row(self.home, state)
        self.assertEqual(sp.read_bytes(), before)
        self.assertEqual(bak.read_bytes(), before_bak)

    def test_a_truthy_number_is_not_on(self):
        self.settings({"statusLine": THEIRS})
        usage_setup.install(self.home)
        for text in ('{"row": 1}', '{"row": 1.0}', '{"row": true, "x": 1}'):
            with self.subTest(text=text):
                self.row_file().write_text(text, encoding="utf-8")
                self.assertEqual(usage_setup.row(self.home, "status"), (0, "row off"))

    def test_broken_settings_do_not_stop_row_off_or_status(self):
        self.settings({"statusLine": THEIRS})
        usage_setup.install(self.home)
        usage_setup.row(self.home, "on")
        (self.home / ".claude" / "settings.json").write_text("{not json", encoding="utf-8")
        code, msg = usage_setup.row(self.home, "status")
        self.assertEqual(code, 0)
        self.assertEqual(msg.split("\n"), ["row on", "settings.json unreadable: capture state unknown"])
        self.assertEqual(usage_setup.row(self.home, "off")[0], 0)
        self.assertFalse(self.row_file().exists())

    def test_broken_settings_make_row_on_fail_and_write_nothing(self):
        (self.home / ".claude" / "settings.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            usage_setup.row(self.home, "on")
        self.assertFalse(self.row_file().exists())

    def test_cli_row_status(self):
        self.settings({"statusLine": THEIRS})
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home)}), \
             contextlib.redirect_stdout(out):
            code = cli.main(["usage", "row", "status"], root=Path(self._tmp.name))
        self.assertEqual(code, 0)
        self.assertIn("row off", out.getvalue())
        self.assertIn("capture not installed", out.getvalue())

    def test_cli_row_on_without_capture_exits_1(self):
        self.settings({"statusLine": THEIRS})
        with mock.patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home)}), \
             contextlib.redirect_stdout(io.StringIO()):
            code = cli.main(["usage", "row", "on"], root=Path(self._tmp.name))
        self.assertEqual(code, 1)


class RefreshTimer(Base):
    def bak(self):
        return usage_setup.share_dir(self.home) / "settings.json.bak"

    def test_fresh_install_sets_the_timer_and_chain_keeps_the_old_line(self):
        self.settings({"statusLine": THEIRS})
        usage_setup.install(self.home)
        self.assertEqual(self.settings()["statusLine"]["refreshInterval"], 60)
        chain = json.loads(usage_setup.chain_path(self.home).read_text(encoding="utf-8"))
        self.assertEqual(chain, {"statusLine": THEIRS})
        self.assertNotIn("refreshInterval", chain["statusLine"])

    def test_fresh_install_keeps_their_own_interval(self):
        theirs = {**THEIRS, "refreshInterval": 5}
        self.settings({"statusLine": theirs})
        usage_setup.install(self.home)
        self.assertEqual(self.settings()["statusLine"]["refreshInterval"], 5)

    def test_install_again_adds_a_missing_timer_with_one_backup(self):
        self.settings({"statusLine": THEIRS})
        usage_setup.install(self.home, python="/usr/bin/python3")
        data = self.settings()
        del data["statusLine"]["refreshInterval"]
        self.settings(data)
        before = (self.home / ".claude" / "settings.json").read_bytes()
        msg = usage_setup.install(self.home, python="/usr/bin/python3")
        self.assertIn("refresh timer set to 60 s", msg)
        self.assertEqual(self.settings()["statusLine"]["refreshInterval"], 60)
        self.assertEqual(self.bak().read_bytes(), before)

    def test_install_with_moved_interpreter_and_no_timer_is_one_write(self):
        self.settings({"statusLine": THEIRS})
        usage_setup.install(self.home, python="/usr/bin/python3")
        data = self.settings()
        del data["statusLine"]["refreshInterval"]
        self.settings(data)
        msg = usage_setup.install(self.home, python="/opt/py/python3")
        self.assertIn("interpreter path", msg)
        self.assertIn("refresh timer set to 60 s", msg)
        line = self.settings()["statusLine"]
        self.assertEqual(line["refreshInterval"], 60)
        self.assertIn("/opt/py/python3", line["command"])

    def test_install_again_with_the_timer_changes_nothing(self):
        self.settings({"statusLine": THEIRS})
        usage_setup.install(self.home)
        path = self.home / ".claude" / "settings.json"
        before = path.read_bytes()
        msg = usage_setup.install(self.home)
        self.assertNotIn("timer", msg)
        self.assertEqual(path.read_bytes(), before)

    def test_install_again_keeps_a_changed_interval(self):
        self.settings({"statusLine": THEIRS})
        usage_setup.install(self.home)
        data = self.settings()
        data["statusLine"]["refreshInterval"] = 30
        self.settings(data)
        usage_setup.install(self.home)
        self.assertEqual(self.settings()["statusLine"]["refreshInterval"], 30)

    def test_uninstall_restores_the_saved_line_without_the_timer(self):
        self.settings({"statusLine": THEIRS})
        usage_setup.install(self.home)
        usage_setup.uninstall(self.home)
        self.assertEqual(self.settings()["statusLine"], THEIRS)


if __name__ == "__main__":
    unittest.main()
