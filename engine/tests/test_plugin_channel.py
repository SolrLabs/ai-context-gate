"""The SessionStart hook on a beta-channel project: which plugin this session loaded."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import profile_guard  # noqa: E402,F401

from plugin_support import ENGINE, PLUGIN, layout, __version__  # noqa: E402

LOADED = "this session loaded the stable plugin"


class Channel(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.project = self.tmp / "project"
        (self.project / layout.GOV_DIR).mkdir(parents=True)
        (self.project / layout.CONFIG).write_text(
            f'[governance]\nengine = "{__version__}"\nschema = 1\nchannel = "beta"\n',
            encoding="utf-8")
        self.cache = layout.claude_dir(self.home) / "plugins" / "cache"
        self.settings = layout.claude_dir(self.home) / "settings.json"
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text(
            '{"enabledPlugins": {"%s": true}}' % layout.PLUGIN_ID, encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def build(self, built: Path) -> Path:
        shutil.copytree(PLUGIN, built)
        shutil.copytree(ENGINE / "govern", built / "govern",
                        ignore=shutil.ignore_patterns("__pycache__"))
        return built

    def hook(self, built: Path) -> str:
        env = {**os.environ, "HOME": str(self.home), "CLAUDE_PROJECT_DIR": str(self.project),
               "CONTEXT_GATE_NO_UPDATE_CHECK": "1"}
        env.pop("CLAUDE_CONFIG_DIR", None)
        res = subprocess.run([sys.executable, str(built / "hooks" / "session_start.py")],
                             input="{}", env=env, capture_output=True, text=True,
                             cwd=str(self.tmp))
        self.assertEqual(res.returncode, 0, res.stderr)
        return res.stdout

    def test_the_stable_plugin_on_a_beta_channel_project_says_so_and_names_the_beta(self):
        out = self.hook(self.build(self.cache / layout.MARKETPLACE / "context-gate" / "0.9.4"))
        self.assertIn(LOADED, out)
        self.assertIn(f"{layout.BETA_CHANNEL_PLUGIN_ID} is not enabled", out)

    def test_the_beta_marketplace_plugin_does_not_say_it_loaded_the_stable_one(self):
        self.settings.write_text(
            '{"enabledPlugins": {"%s": true}}' % layout.BETA_CHANNEL_PLUGIN_ID, encoding="utf-8")
        out = self.hook(self.build(
            self.cache / layout.BETA_MARKETPLACE / "context-gate" / "0.9.5-beta.7"))
        self.assertNotIn(LOADED, out)
        self.assertNotIn("beta channel", out)

    def test_a_plugin_outside_the_cache_does_not_say_it_loaded_the_stable_one(self):
        out = self.hook(self.build(self.tmp / "dev-plugin"))
        self.assertNotIn(LOADED, out)


if __name__ == "__main__":
    unittest.main()
