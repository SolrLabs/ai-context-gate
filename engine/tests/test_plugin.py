"""The Claude Code plugin: its session-start hook, the notice that points at its skill, and the
rule that plugin and engine release as one version."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parent.parent
REPO = ENGINE.parent
PLUGIN = REPO / "plugin"
sys.path.insert(0, str(ENGINE))

from govern import __version__, layout, notice  # noqa: E402

BETA = "9.9.0-beta.1"
BOTH = ("context-gate: both the beta and the stable plugin are enabled here, so every hook "
        f"runs twice; govern beta on {BETA} re-applies the switch")


class Plugin(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.project = self.tmp / "project"
        self.home.mkdir()
        (self.project / layout.GOV_DIR).mkdir(parents=True)
        self.pin(__version__)
        # The plugin as install-plugin assembles it: plugin/ plus the engine.
        self.built = self.tmp / "built"
        shutil.copytree(PLUGIN, self.built)
        shutil.copytree(ENGINE / "govern", self.built / "govern",
                        ignore=shutil.ignore_patterns("__pycache__"))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def pin(self, version: str) -> None:
        (self.project / layout.CONFIG).write_text(
            f'[governance]\nengine = "{version}"\nschema = 1\n', encoding="utf-8")

    def hook(self, project: Path) -> str:
        env = {**os.environ, "HOME": str(self.home), "CLAUDE_PROJECT_DIR": str(project),
               "CONTEXT_GATE_NO_UPDATE_CHECK": "1"}
        res = subprocess.run([sys.executable, str(self.built / "hooks" / "session_start.py")],
                             env=env, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        return res.stdout

    def install_engine(self, version: str) -> None:
        shutil.copytree(ENGINE / "govern", layout.engines_dir(self.home) / version / "govern",
                        ignore=shutil.ignore_patterns("__pycache__"))

    def test_plugin_and_engine_are_one_version(self):
        manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
        self.assertEqual(manifest["version"], __version__)

    @unittest.skipUnless((REPO / ".claude-plugin" / "marketplace.json").is_file(),
                         "no .claude-plugin/marketplace.json in this checkout")
    def test_marketplace_lists_this_version(self):
        market = json.loads((REPO / ".claude-plugin" / "marketplace.json").read_text())
        self.assertEqual(market["plugins"][0]["version"], __version__)

    def test_upgrade_skill_is_visible_to_the_agent(self):
        """The notice tells the agent to use the skill, so the agent must be able to see it:
        a user-only skill (disable-model-invocation) is hidden from the model entirely."""
        head = (PLUGIN / "skills" / "upgrade" / "SKILL.md").read_text().split("---")[1]
        self.assertNotIn("disable-model-invocation", head)
        self.assertIn("Never start it unprompted", head)

    def test_options_skill_is_visible_to_the_agent(self):
        text = (PLUGIN / "skills/options/SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\nname: options\n"))
        self.assertIn("govern options --json", text)
        self.assertIn("Chat about this", text)
        # Windows has no shebang: every call runs through python3, and the grant is no wider
        # than the commands the skill actually needs.
        self.assertIsNone(re.search(r"(?<!python3 )\.context-gate/bin/govern", text))
        self.assertNotIn("git -C:*", text)

    def test_hook_is_silent_outside_a_governed_project(self):
        self.assertEqual(self.hook(self.tmp), "")

    def test_hook_is_silent_when_current(self):
        self.assertEqual(self.hook(self.project), "")

    def test_hook_announces_a_newer_engine_to_session_and_person(self):
        self.install_engine("9.9.9")
        out = json.loads(self.hook(self.project))
        self.assertIn("context-gate 9.9.9 available", out["systemMessage"])
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertEqual(out["hookSpecificOutput"]["additionalContext"], out["systemMessage"])

    def test_notice_names_the_skill_when_the_plugin_is_present(self):
        self.install_engine("9.9.9")
        self.assertIn("Use python3 .context-gate/bin/upgrade to upgrade",
                      notice.for_project(self.project, self.home))
        (self.home / ".claude" / "skills" / "context-gate" / ".claude-plugin").mkdir(
            parents=True)
        (self.home / ".claude" / "skills" / "context-gate" / ".claude-plugin"
         / "plugin.json").write_text("{}")
        self.assertIn("Use /context-gate:upgrade to upgrade",
                      notice.for_project(self.project, self.home))

    def settings(self, scope: str, plugins) -> None:
        """Write enabledPlugins at one scope: user, project or local."""
        path = {"user": self.home / ".claude" / "settings.json",
                "project": self.project / ".claude" / "settings.json",
                "local": self.project / ".claude" / "settings.local.json"}[scope]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(plugins if isinstance(plugins, str)
                        else json.dumps({"enabledPlugins": plugins}), encoding="utf-8")

    def test_enabled_plugins_merge_user_then_project_then_local(self):
        self.settings("user", {"a": True})
        self.settings("project", {"a": True, "b": True})
        self.settings("local", {"a": False})
        self.assertEqual(notice.enabled_plugins(self.project, self.home), {"a": False, "b": True})

    def test_enabled_plugins_skips_an_unreadable_file(self):
        self.settings("user", {"a": True})
        self.settings("project", "{not json")
        (self.project / ".claude" / "settings.local.json").write_bytes(b'{"enabledPlugins": \xff}')
        self.assertEqual(notice.enabled_plugins(self.project, self.home), {"a": True})
        self.settings("local", '\ufeff{"enabledPlugins": {"a": false, "c": "yes"}}')
        self.assertEqual(notice.enabled_plugins(self.project, self.home), {"a": False})

    def test_both_plugins_warning_only_from_a_beta_with_both_enabled(self):
        self.settings("user", {layout.PLUGIN_ID: True})
        self.settings("local", {layout.SKILLS_DIR_PLUGIN_ID: True})
        with mock.patch.object(notice, "__version__", BETA):
            self.assertEqual(notice.both_plugins_warning(self.project, self.home), BOTH)
            # Local settings win: the stable plugin switched off here is off.
            self.settings("local", {layout.SKILLS_DIR_PLUGIN_ID: True, layout.PLUGIN_ID: False})
            self.assertIsNone(notice.both_plugins_warning(self.project, self.home))
            self.settings("local", {layout.PLUGIN_ID: True})
            self.assertIsNone(notice.both_plugins_warning(self.project, self.home))
        self.settings("local", {layout.SKILLS_DIR_PLUGIN_ID: True})
        with mock.patch.object(notice, "__version__", "9.9.0"):
            self.assertIsNone(notice.both_plugins_warning(self.project, self.home))

    def test_beta_hook_says_when_both_plugins_load(self):
        """Review focus 3: the stable plugin re-enabled by hand beside the beta."""
        (self.built / "govern" / "__init__.py").write_text(
            (ENGINE / "govern" / "__init__.py").read_text(encoding="utf-8").replace(
                f'__version__ = "{__version__}"', f'__version__ = "{BETA}"'), encoding="utf-8")
        self.settings("local", {layout.SKILLS_DIR_PLUGIN_ID: True, layout.PLUGIN_ID: False})
        self.assertEqual(self.hook(self.project), "")
        self.settings("project", {layout.PLUGIN_ID: True})
        self.settings("local", {layout.SKILLS_DIR_PLUGIN_ID: True})
        out = json.loads(self.hook(self.project))
        self.assertEqual(out["systemMessage"], BOTH)
        self.assertEqual(out["hookSpecificOutput"]["additionalContext"], BOTH)

    def test_a_local_install_off_by_default_is_not_available(self):
        manifest = self.home / ".claude" / "skills" / "context-gate" / ".claude-plugin" / "plugin.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text('{"defaultEnabled": false}', encoding="utf-8")
        self.assertFalse(notice.plugin_available(self.project, self.home))
        self.settings("local", {layout.SKILLS_DIR_PLUGIN_ID: True})
        self.assertTrue(notice.plugin_available(self.project, self.home))
        manifest.write_text("{}", encoding="utf-8")
        self.settings("local", {layout.SKILLS_DIR_PLUGIN_ID: False})
        self.assertFalse(notice.plugin_available(self.project, self.home))

    def test_a_plugin_switched_off_below_the_user_level_is_not_available(self):
        """User-level on, project- or local-level off: the merge says off, so the notice names
        `bin/upgrade`, not the plugin's skill."""
        for scope in ("project", "local"):
            self.settings("user", {layout.PLUGIN_ID: True})
            self.settings("project", {})
            self.settings("local", {})
            self.assertEqual(notice.upgrade_command(self.project, self.home),
                             f"/{notice.PLUGIN}:upgrade")
            self.settings(scope, {layout.PLUGIN_ID: False})
            self.assertEqual(notice.upgrade_command(self.project, self.home),
                             f"python3 {layout.GOV_DIR}/bin/upgrade", scope)

    def test_the_repository_plugin_is_on_by_default(self):
        """Only the installed copy is off by default (install-plugin.py writes it): the
        marketplace plugin, built from this file, loads where it is installed."""
        manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertNotIn("defaultEnabled", manifest)

    def test_series_pin_names_the_engine_it_actually_runs(self):
        self.pin("0.1")
        self.install_engine("0.1.2")
        msg = notice.for_project(self.project, self.home)
        self.assertIn("to pin 0.1.2 exactly", msg)


if __name__ == "__main__":
    unittest.main()
