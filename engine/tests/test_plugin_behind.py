"""notice.plugin_behind: the plugin Claude Code loads is older than the engine the project pins."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import profile_guard  # noqa: E402,F401

from plugin_support import ENGINE, PLUGIN, layout, notice  # noqa: E402

KEY = layout.PLUGIN_ID


class PluginBehind(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.project = self.tmp / "project"
        self.other = self.tmp / "other"
        self.home.mkdir()
        (self.project / layout.GOV_DIR).mkdir(parents=True)
        self.pin("0.7.0")
        self.profile = layout.claude_dir(self.home)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def pin(self, version: str) -> None:
        (self.project / layout.CONFIG).write_text(
            f'[governance]\nengine = "{version}"\nschema = 1\n', encoding="utf-8")

    def record(self, entries, profile: Path | None = None) -> None:
        path = (profile or self.profile) / "plugins" / "installed_plugins.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 2, "plugins": {KEY: entries}}), encoding="utf-8")

    def behind(self):
        return notice.plugin_behind(self.project, self.home)

    def test_user_entry_behind(self):
        self.record([{"scope": "user", "version": "0.6.1"}])
        out = self.behind()
        self.assertEqual(len(out.splitlines()), 1)
        self.assertIn("(user) is 0.6.1; this project runs 0.7.0", out)
        self.assertIn("update context-gate@context-gate --scope user", out)
        self.assertIn("marketplace update context-gate", out)

    def test_project_entry_for_another_path_ignored(self):
        self.record([{"scope": "project", "projectPath": str(self.other), "version": "0.6.1"}])
        self.assertIsNone(self.behind())

    def test_project_and_user_entries_both_behind(self):
        self.record([{"scope": "user", "version": "0.6.1"},
                     {"scope": "project", "projectPath": str(self.project), "version": "0.6.0"}])
        lines = self.behind().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("--scope user", lines[0])
        self.assertIn("--scope project", lines[1])

    def test_local_entry_for_root(self):
        self.record([{"scope": "local", "projectPath": str(self.project), "version": "0.6.0"}])
        self.assertIn("--scope local", self.behind())

    def test_equal_or_newer_is_silent(self):
        self.record([{"scope": "user", "version": "0.7.0"},
                     {"scope": "project", "projectPath": str(self.project), "version": "0.8.0"}])
        self.assertIsNone(self.behind())

    def test_unparsable_version_skipped(self):
        self.record([{"scope": "user", "version": "latest"}])
        self.assertIsNone(self.behind())

    def test_beta_pin_is_silent(self):
        self.pin("0.7.0-beta.1")
        self.record([{"scope": "user", "version": "0.6.1"}])
        self.assertIsNone(self.behind())

    def test_series_pin_is_silent(self):
        self.pin("0.7")
        self.record([{"scope": "user", "version": "0.6.1"}])
        self.assertIsNone(self.behind())

    def test_bad_json_and_missing_file(self):
        self.assertIsNone(self.behind())
        path = self.profile / "plugins" / "installed_plugins.json"
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(self.behind())
        path.write_text(json.dumps({"plugins": {"other@x": []}}), encoding="utf-8")
        self.assertIsNone(self.behind())

    def test_reads_claude_config_dir(self):
        profile = self.tmp / "profile"
        self.record([{"scope": "user", "version": "0.6.1"}], profile)
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(profile)}), \
                mock.patch.object(layout, "home", return_value=self.home):
            self.assertIn("(user)", notice.plugin_behind(self.project, layout.home()))

    def test_session_start_hook_holds_the_line(self):
        self.record([{"scope": "user", "version": "0.6.1"}])
        built = self.tmp / "built"
        shutil.copytree(PLUGIN, built)
        shutil.copytree(ENGINE / "govern", built / "govern",
                        ignore=shutil.ignore_patterns("__pycache__"))
        env = {**os.environ, "HOME": str(self.home), "USERPROFILE": str(self.home),
               "CLAUDE_PROJECT_DIR": str(self.project), "CONTEXT_GATE_NO_UPDATE_CHECK": "1"}
        env.pop("CLAUDE_CONFIG_DIR", None)
        res = subprocess.run([sys.executable, str(built / "hooks" / "session_start.py")],
                             input="{}", env=env, capture_output=True, text=True, cwd=self.tmp)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("plugin (user) is 0.6.1", json.loads(res.stdout)["systemMessage"])


class BetaChannel(unittest.TestCase):
    """On `channel = "beta"` the plugin Claude Code loads is the beta channel's."""
    BETA_KEY = layout.BETA_CHANNEL_PLUGIN_ID

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.project = self.tmp / "project"
        self.home.mkdir()
        (self.project / layout.GOV_DIR).mkdir(parents=True)
        self.profile = layout.claude_dir(self.home)
        self.channel("beta")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def channel(self, value: str) -> None:
        (self.project / layout.CONFIG).write_text(
            f'[governance]\nengine = "0.9.4"\nschema = 1\nchannel = "{value}"\n',
            encoding="utf-8")

    def behind(self):
        return notice.plugin_behind(self.project, self.home)

    def record2(self, **by_id) -> None:
        path = self.profile / "plugins" / "installed_plugins.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 2, "plugins": by_id}), encoding="utf-8")

    def test_beta_entry_behind_names_the_beta_channel(self):
        self.record2(**{self.BETA_KEY: [{"scope": "user", "version": "0.9.3"}]})
        self.assertEqual(self.behind(), (
            "context-gate's plugin (user) is 0.9.3; this project runs 0.9.4. Run claude plugin "
            "marketplace update context-gate-beta, then claude plugin update "
            "context-gate@context-gate-beta --scope user, then /exit and start claude again "
            "(claude -c keeps the old plugin)."))

    def test_stable_entry_behind_is_not_reported_on_the_beta_channel(self):
        self.record2(**{KEY: [{"scope": "user", "version": "0.9.3"}]})
        self.assertIsNone(self.behind())

    def test_with_both_entries_only_the_beta_one_is_reported(self):
        self.record2(**{KEY: [{"scope": "user", "version": "0.9.2"}],
                        self.BETA_KEY: [{"scope": "user", "version": "0.9.3"}]})
        out = self.behind()
        self.assertEqual(len(out.splitlines()), 1)
        self.assertIn("is 0.9.3;", out)
        self.assertIn("context-gate@context-gate-beta", out)

    def test_another_channel_value_keeps_the_stable_ids(self):
        self.channel("stable")
        self.record2(**{KEY: [{"scope": "user", "version": "0.9.3"}]})
        self.assertIn("update context-gate@context-gate --scope user", self.behind())


class LocalBetaWithEngineDir(unittest.TestCase):
    """notice.local_beta follows the launcher: a set engine_dir runs the tree, not local.toml."""
    BETA = "9.9.0-beta.8"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.project = self.tmp / "project"
        (self.project / layout.GOV_DIR).mkdir(parents=True)
        engine = layout.engines_dir(self.home) / self.BETA / "govern"
        engine.mkdir(parents=True)
        (engine / "cli.py").write_text("", encoding="utf-8")
        (self.project / layout.LOCAL).write_text(
            f'[governance]\nengine = "{self.BETA}"\n', encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def config(self, extra: str) -> None:
        (self.project / layout.CONFIG).write_text(
            f'[governance]\nengine = "9.9.0"\nschema = 1\n{extra}', encoding="utf-8")

    def test_the_beta_is_named_without_engine_dir(self):
        self.config("")
        self.assertEqual(notice.local_beta(self.project, self.home), self.BETA)

    def test_an_empty_engine_dir_is_unset(self):
        self.config('engine_dir = ""\n')
        self.assertEqual(notice.local_beta(self.project, self.home), self.BETA)

    def test_engine_dir_set_means_no_local_beta(self):
        self.config('engine_dir = "engine"\n')
        self.assertIsNone(notice.local_beta(self.project, self.home))


class ChannelPlugin(unittest.TestCase):
    """notice.channel_plugin: a beta-channel project whose session loads the wrong plugin."""
    BETA = layout.BETA_CHANNEL_PLUGIN_ID
    STABLE = layout.PLUGIN_ID

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.project = self.tmp / "project"
        self.home.mkdir()
        (self.project / layout.GOV_DIR).mkdir(parents=True)
        self.config('channel = "beta"\n')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def config(self, extra: str) -> None:
        (self.project / layout.CONFIG).write_text(
            f'[governance]\nengine = "0.9.4"\nschema = 1\n{extra}', encoding="utf-8")

    def settings(self, *ids: str) -> None:
        path = layout.claude_dir(self.home) / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"enabledPlugins": {i: True for i in ids}}), encoding="utf-8")

    def say(self):
        return notice.channel_plugin(self.project, self.home)

    def test_only_the_stable_plugin_enabled_names_the_beta_and_the_commands(self):
        self.settings(self.STABLE)
        out = self.say()
        self.assertEqual(len(out.splitlines()), 1)
        self.assertIn(self.BETA, out)
        self.assertIn("claude plugin marketplace update context-gate-beta", out)
        self.assertIn(f"claude plugin install {self.BETA} --scope local", out)
        self.assertIn(f"claude plugin disable {self.STABLE} --scope local", out)
        self.assertIn("/exit and start claude again", out)

    def test_no_plugin_enabled_has_no_disable_command(self):
        self.settings()
        out = self.say()
        self.assertIn(f"claude plugin install {self.BETA} --scope local", out)
        self.assertNotIn("disable", out)

    def test_both_enabled_says_every_hook_runs_twice(self):
        self.settings(self.STABLE, self.BETA)
        out = self.say()
        self.assertIn("every hook runs twice", out)
        self.assertIn(f"claude plugin disable {self.STABLE} --scope local", out)
        self.assertNotIn("install", out)

    def test_only_the_beta_plugin_enabled_is_silent(self):
        self.settings(self.BETA)
        self.assertIsNone(self.say())

    def test_no_user_settings_file_is_silent(self):
        self.assertIsNone(self.say())

    def test_the_stable_channel_is_silent(self):
        self.config("")
        self.settings(self.STABLE)
        self.assertIsNone(self.say())

    def test_an_unreadable_config_is_silent(self):
        self.settings(self.STABLE)
        (self.project / layout.CONFIG).write_bytes(b"\xff\xfe[governance")
        self.assertIsNone(self.say())


if __name__ == "__main__":
    unittest.main()
