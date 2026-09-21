"""The Claude Code plugin: its session-start hook, the notice that points at its skill, and the
rule that plugin and engine release as one version."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
REPO = ENGINE.parent
PLUGIN = REPO / "plugin"
sys.path.insert(0, str(ENGINE))

from govern import __version__, layout, notice  # noqa: E402


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

    def test_series_pin_names_the_engine_it_actually_runs(self):
        self.pin("0.1")
        self.install_engine("0.1.2")
        msg = notice.for_project(self.project, self.home)
        self.assertIn("to pin 0.1.2 exactly", msg)


if __name__ == "__main__":
    unittest.main()
