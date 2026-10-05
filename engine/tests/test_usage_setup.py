"""govern usage install / uninstall / resolve, and the SessionStart re-wrap."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))

import govern.checks  # noqa: E402,F401  (registers the built-in checks)
from govern import cli, manifest, options, usage_setup  # noqa: E402
from test_options import repo  # noqa: E402

THEIRS = {"type": "command", "command": "bash /home/x/.claude/statusline-command.sh", "padding": 0}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "a home"
        (self.home / ".claude").mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def settings(self, data=None):
        p = self.home / ".claude" / "settings.json"
        if data is not None:
            p.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return json.loads(p.read_text(encoding="utf-8"))


class Install(Base):
    def test_wraps_and_keeps_other_fields(self):
        self.settings({"model": "opus", "statusLine": THEIRS})
        usage_setup.install(self.home, python="/usr/bin/python3")
        s = self.settings()
        self.assertEqual(s["model"], "opus")
        self.assertEqual(s["statusLine"]["padding"], 0)
        cmd = s["statusLine"]["command"]
        self.assertEqual(cmd, f'"/usr/bin/python3" "{usage_setup.capture_path(self.home)}"')
        self.assertTrue(usage_setup.capture_path(self.home).is_file())
        chain = json.loads(usage_setup.chain_path(self.home).read_text(encoding="utf-8"))
        self.assertEqual(chain, {"statusLine": THEIRS})

    def test_reinstall_does_not_double_wrap(self):
        self.settings({"statusLine": THEIRS})
        usage_setup.install(self.home)
        usage_setup.install(self.home)
        chain = json.loads(usage_setup.chain_path(self.home).read_text(encoding="utf-8"))
        self.assertEqual(chain, {"statusLine": THEIRS})

    def test_reinstall_refreshes_the_interpreter_path(self):
        self.settings({"statusLine": THEIRS})
        usage_setup.install(self.home, python="/usr/bin/python3")
        usage_setup.install(self.home, python="/usr/bin/python3.12")
        cmd = self.settings()["statusLine"]["command"]
        self.assertEqual(cmd, f'"/usr/bin/python3.12" "{usage_setup.capture_path(self.home)}"')
        chain = json.loads(usage_setup.chain_path(self.home).read_text(encoding="utf-8"))
        self.assertEqual(chain, {"statusLine": THEIRS})

    def test_no_statusline_before(self):
        self.settings({})
        usage_setup.install(self.home)
        self.assertTrue(usage_setup.is_ours(self.settings()["statusLine"]))
        self.assertEqual(json.loads(usage_setup.chain_path(self.home).read_text()), {"statusLine": None})

    def test_uninstall_restores_exactly(self):
        self.settings({"model": "opus", "statusLine": THEIRS})
        usage_setup.install(self.home)
        usage_setup.uninstall(self.home)
        self.assertEqual(self.settings(), {"model": "opus", "statusLine": THEIRS})
        self.assertFalse(usage_setup.chain_path(self.home).exists())

    def test_uninstall_with_nothing_before_removes_the_key(self):
        self.settings({"model": "opus"})
        usage_setup.install(self.home)
        usage_setup.uninstall(self.home)
        self.assertEqual(self.settings(), {"model": "opus"})

    def test_uninstall_leaves_another_tools_statusline(self):
        self.settings({"statusLine": THEIRS})
        usage_setup.install(self.home)
        other = {"type": "command", "command": "other-tool"}
        self.settings({"statusLine": other})
        usage_setup.uninstall(self.home)
        self.assertEqual(self.settings()["statusLine"], other)
        self.assertFalse(usage_setup.chain_path(self.home).exists())

    def test_uninstall_with_missing_chain_clears_the_stuck_statusline(self):
        self.settings({"model": "opus", "statusLine": THEIRS})
        usage_setup.install(self.home)
        usage_setup.chain_path(self.home).unlink()          # chain gone: ours is now stuck
        msg = usage_setup.uninstall(self.home)
        self.assertNotIn("statusLine", self.settings())
        self.assertIn("could not be restored", msg)
        self.assertIn("chain file missing", msg)

    def test_install_backs_up_settings_bytes_exactly(self):
        before = json.dumps({"model": "opus", "statusLine": THEIRS}, indent=2)
        self.settings(json.loads(before))
        raw = (self.home / ".claude" / "settings.json").read_bytes()
        usage_setup.install(self.home)
        backup = usage_setup.share_dir(self.home) / "settings.json.bak"
        self.assertEqual(backup.read_bytes(), raw)

    def test_uninstall_backs_up_the_wrapped_settings_first(self):
        self.settings({"statusLine": THEIRS})
        usage_setup.install(self.home)
        wrapped = (self.home / ".claude" / "settings.json").read_bytes()
        usage_setup.uninstall(self.home)
        backup = usage_setup.share_dir(self.home) / "settings.json.bak"
        self.assertEqual(backup.read_bytes(), wrapped)


class Rewrap(Base):
    def test_not_installed_is_none(self):
        self.settings({"statusLine": THEIRS})
        self.assertIsNone(usage_setup.rewrap(self.home))

    def test_still_ours_is_none(self):
        self.settings({"statusLine": THEIRS})
        usage_setup.install(self.home)
        self.assertIsNone(usage_setup.rewrap(self.home))

    def test_replaced_as_claude_usage_writes_it(self):
        self.settings({"statusLine": THEIRS})
        usage_setup.install(self.home)
        new = {"command": "bash /home/x/.claude/statusline-command.sh", "type": "command"}
        self.settings({"zeta": 1, "statusLine": new, "alpha": 2})       # keys reordered
        msg = usage_setup.rewrap(self.home)
        self.assertIn("re-wrapped", msg)
        self.assertTrue(usage_setup.is_ours(self.settings()["statusLine"]))
        chain = json.loads(usage_setup.chain_path(self.home).read_text(encoding="utf-8"))
        self.assertEqual(chain, {"statusLine": new})

    def test_removed_is_not_recreated(self):
        self.settings({"statusLine": THEIRS})
        usage_setup.install(self.home)
        self.settings({})
        msg = usage_setup.rewrap(self.home)
        self.assertIn("usage data off", msg)
        self.assertNotIn("statusLine", self.settings())

    def test_removed_message_says_once_until_reinstalled(self):
        self.settings({"statusLine": THEIRS})
        usage_setup.install(self.home)
        self.settings({})
        self.assertIsNotNone(usage_setup.rewrap(self.home))     # first removal: speaks
        self.assertIsNone(usage_setup.rewrap(self.home))         # second session: silent
        self.assertIsNone(usage_setup.rewrap(self.home))
        usage_setup.install(self.home)                          # install clears the marker
        self.settings({})
        self.assertIsNotNone(usage_setup.rewrap(self.home))      # removed again: speaks once more


class Resolve(Base):
    def cfg(self, level="error", profile=None):
        return SimpleNamespace(checks={"usage": SimpleNamespace(level=level)},
                               profile=SimpleNamespace(path=profile) if profile else None)

    def root(self):
        r = Path(self._tmp.name) / "proj"
        (r / ".context-gate").mkdir(parents=True, exist_ok=True)
        return r

    ALERT = '[[alert]]\nsignal = "seven_day"\nat = 93\nsay = "{where}"\n'

    def test_off(self):
        self.assertEqual(usage_setup.resolve(self.cfg("off"), self.root())["enabled"], False)

    def test_project_file_wins_outright(self):
        root, prof = self.root(), Path(self._tmp.name) / "prof"
        prof.mkdir()
        (prof / "usage-alerts.toml").write_text(self.ALERT.replace("{where}", "profile"), encoding="utf-8")
        (root / ".context-gate" / "usage-alerts.toml").write_text(
            self.ALERT.replace("{where}", "project"), encoding="utf-8")
        r = usage_setup.resolve(self.cfg(profile=prof), root)
        self.assertEqual(r["alerts_file"], ".context-gate/usage-alerts.toml")
        self.assertEqual([a["say"] for a in r["alerts"]], ["project"])

    def test_profile_file_when_project_has_none(self):
        root, prof = self.root(), Path(self._tmp.name) / "prof"
        prof.mkdir()
        (prof / "usage-alerts.toml").write_text(self.ALERT.replace("{where}", "profile"), encoding="utf-8")
        r = usage_setup.resolve(self.cfg(profile=prof), root)
        self.assertEqual(r["alerts_file"], "profile usage-alerts.toml")
        self.assertEqual([a["say"] for a in r["alerts"]], ["profile"])

    def test_neither_is_data_only(self):
        r = usage_setup.resolve(self.cfg(), self.root())
        self.assertEqual((r["enabled"], r["alerts"], r["alerts_file"], r["error"]), (True, [], None, None))
        self.assertEqual(r["context_step"], 5)

    def test_context_step_carried_through(self):
        root = self.root()
        (root / ".context-gate" / "usage-alerts.toml").write_text(
            "context_step = 2\n" + self.ALERT.replace("{where}", "x"), encoding="utf-8")
        r = usage_setup.resolve(self.cfg(), root)
        self.assertEqual((r["context_step"], len(r["alerts"]), r["error"]), (2, 1, None))

    def test_bad_context_step_is_data_only_with_error(self):
        root = self.root()
        (root / ".context-gate" / "usage-alerts.toml").write_text(
            "context_step = 11\n" + self.ALERT.replace("{where}", "x"), encoding="utf-8")
        r = usage_setup.resolve(self.cfg(), root)
        self.assertEqual((r["context_step"], r["alerts"]), (5, []))
        self.assertIn("context_step", r["error"])

    def test_invalid_file_is_data_only_with_error(self):
        root = self.root()
        (root / ".context-gate" / "usage-alerts.toml").write_text(
            '[[alert]]\nsignal = "context"\nat = 0\nsay = "x"\n', encoding="utf-8")
        r = usage_setup.resolve(self.cfg(), root)
        self.assertEqual(r["alerts"], [])
        self.assertIn("at must be", r["error"])

    def test_records_the_absolute_path_and_mtime_for_mid_session_reloads(self):
        root, prof = self.root(), Path(self._tmp.name) / "prof"
        prof.mkdir()
        (prof / "usage-alerts.toml").write_text(self.ALERT.replace("{where}", "x"), encoding="utf-8")
        os.utime(prof / "usage-alerts.toml", (1_000_000, 1_000_000))
        r = usage_setup.resolve(self.cfg(profile=prof), root)
        path = (prof / "usage-alerts.toml").resolve()
        self.assertEqual(r["alerts_file"], "profile usage-alerts.toml")      # the shown name stays
        self.assertEqual(r["alerts_path"], str(path))
        self.assertTrue(Path(r["alerts_path"]).is_absolute())
        self.assertEqual((r["alerts_mtime"], r["alerts_size"]),
                         (path.stat().st_mtime_ns, path.stat().st_size))

    def test_file_modified_under_two_seconds_ago_is_loaded_with_mtime_zero(self):
        root = self.root()
        f = root / ".context-gate" / "usage-alerts.toml"
        f.write_text(self.ALERT.replace("{where}", "x"), encoding="utf-8")       # just now
        r = usage_setup.resolve(self.cfg(), root)
        self.assertEqual(len(r["alerts"]), 1)
        self.assertEqual((r["alerts_mtime"], r["alerts_size"]), (0, f.stat().st_size))

    def test_invalid_file_still_records_its_path_so_a_fix_reloads(self):
        root = self.root()
        f = root / ".context-gate" / "usage-alerts.toml"
        f.write_text("context_step = 0\n", encoding="utf-8")
        os.utime(f, (1_000_000, 1_000_000))
        r = usage_setup.resolve(self.cfg(), root)
        self.assertIsNotNone(r["error"])
        self.assertEqual((r["alerts_path"], r["alerts_mtime"]), (str(f.resolve()), f.stat().st_mtime_ns))

    def test_no_file_records_no_path(self):
        r = usage_setup.resolve(self.cfg(), self.root())
        self.assertEqual((r["alerts_path"], r["alerts_mtime"]), (None, None))

    def test_alert_not_a_list_is_data_only_with_error(self):
        root = self.root()
        (root / ".context-gate" / "usage-alerts.toml").write_text("alert = 5\n", encoding="utf-8")
        r = usage_setup.resolve(self.cfg(), root)
        self.assertEqual(r["alerts"], [])
        self.assertIn("alert must be a list", r["error"])


class Command(Base):
    def test_usage_is_an_option(self):
        c = manifest.CHECKS["usage"]
        self.assertEqual((c.default, c.scope), ("off", "workspace"))
        self.assertIn("usage", options.ids())

    def test_resolve_json(self):
        r = repo(Path(self._tmp.name), extra='\n[checks.usage]\nlevel = "error"\n')
        code, out, _ = r.run("usage", "resolve", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"enabled": True, "alerts_file": None, "alerts": [],
                                           "context_step": 5, "error": None, "alerts_path": None,
                                           "alerts_mtime": None, "alerts_size": None})

    def test_resolve_json_reports_a_bad_context_step(self):
        r = repo(Path(self._tmp.name), extra='\n[checks.usage]\nlevel = "error"\n')
        (r.root / ".context-gate" / "usage-alerts.toml").write_text("context_step = 0\n", encoding="utf-8")
        code, out, _ = r.run("usage", "resolve", "--json")
        self.assertEqual(code, 0)
        got = json.loads(out)
        self.assertEqual((got["context_step"], got["alerts"]), (5, []))
        self.assertIn("context_step must be a whole number 1-10", got["error"])

    def test_install_works_outside_a_governed_project(self):
        import contextlib
        import io
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}), \
             contextlib.redirect_stdout(io.StringIO()):
            code = cli.main(["usage", "install"], root=Path(self._tmp.name))
        self.assertEqual(code, 0)
        self.assertTrue(usage_setup.is_ours(self.settings()["statusLine"]))

    def test_install_with_a_malformed_settings_json_fails_closed(self):
        import contextlib
        import io
        import os
        from unittest import mock
        (self.home / ".claude" / "settings.json").write_text("{not json", encoding="utf-8")
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}), \
             contextlib.redirect_stderr(err):
            code = cli.main(["usage", "install"], root=Path(self._tmp.name))
        self.assertEqual(code, 2)
        self.assertIn("settings.json", err.getvalue())
        self.assertIn("settings.json was not changed", err.getvalue())


if __name__ == "__main__":
    unittest.main()
