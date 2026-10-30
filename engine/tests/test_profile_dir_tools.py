"""The launcher and the release installer read Claude Code's profile folder: CLAUDE_CONFIG_DIR
when set, else ~/.claude."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import profile_guard  # noqa: E402,F401

from regressions_support import *  # noqa: E402,F401,F403

REPO = Path(__file__).resolve().parents[2]
INSTALL = REPO / "tools" / "release" / "install-plugin.py"
BOTH = "both the beta and the stable plugin"


class LauncherProfileDir(Base):
    def setUp(self) -> None:
        super().setUp()
        w = self.ws()
        self.entry, env = self._installed(w)
        self._engines(w, (__version__, BETA))
        self.root, self.home = w.root, w.home
        self.profile = self.tmp / "profile"
        env = {k: v for k, v in env.items() if k != "CLAUDE_CONFIG_DIR"}
        self.env = {**env, "XDG_CONFIG_HOME": str(self.home)}
        subprocess.run(["git", "init", "-q", str(self.root)], env=self.env, check=True)
        (self.root / ".claude").mkdir()
        self.run_dir = self.tmp / "run"
        self.run_dir.mkdir()
        wtext(self.root / ".context-gate" / "local.toml",
              f'[governance]\nengine = "{BETA}"\n')

    def plugin_in(self, base: Path) -> None:
        release = base / "skills" / "context-gate-beta" / "govern" / "RELEASE"
        release.parent.mkdir(parents=True)
        wtext(release, f"v{BETA}\n")

    def beta(self, **extra: str) -> str:
        res = subprocess.run([sys.executable, str(self.entry), "beta"],
                             env={**self.env, **extra}, capture_output=True, text=True,
                             cwd=str(self.run_dir))
        self.assertEqual(res.returncode, 0, res.stderr)
        return res.stdout

    def test_variable_set_finds_the_plugin_in_the_profile(self):
        self.plugin_in(self.profile)
        self.assertIn("plugin: installed", self.beta(CLAUDE_CONFIG_DIR=str(self.profile)))

    def test_variable_set_ignores_the_plugin_under_home(self):
        self.plugin_in(self.home / ".claude")
        self.assertIn("plugin: missing", self.beta(CLAUDE_CONFIG_DIR=str(self.profile)))

    def test_variable_unset_finds_the_plugin_under_home(self):
        self.plugin_in(self.home / ".claude")
        self.assertIn("plugin: installed", self.beta())

    def test_blank_variable_counts_as_unset(self):
        self.plugin_in(self.home / ".claude")
        self.assertIn("plugin: installed", self.beta(CLAUDE_CONFIG_DIR="  "))

    def test_a_tilde_in_the_variable_is_the_home(self):
        self.plugin_in(self.home / "prof")
        self.assertIn("plugin: installed", self.beta(
            CLAUDE_CONFIG_DIR="~/prof", HOME=str(self.home), USERPROFILE=str(self.home)))

    def test_a_blank_variable_reads_the_default_folder_under_home(self):
        self.plugin_in(self.home / ".claude")
        self.assertIn("plugin: installed", self.beta(
            CLAUDE_CONFIG_DIR="  ", HOME=str(self.home), USERPROFILE=str(self.home)))

    def test_both_plugins_warning_reads_the_profile_settings(self):
        both = {"enabledPlugins": {"context-gate-beta@skills-dir": True,
                                   "context-gate@context-gate": True}}
        self.profile.mkdir()
        wtext(self.profile / "settings.json", json.dumps(both))
        self.assertIn(BOTH, self.beta(CLAUDE_CONFIG_DIR=str(self.profile)))
        self.assertNotIn(BOTH, self.beta(CLAUDE_CONFIG_DIR=str(self.tmp / "other")))
        # Unset, the user layer is <home>/.claude/settings.json, where nothing is.
        self.assertNotIn(BOTH, self.beta())


class InstallPluginProfileDir(Base):
    def tag(self) -> str:
        res = subprocess.run(["git", "-C", str(REPO), "tag", "-l", "v*"], capture_output=True,
                             text=True)
        tags = [t for t in res.stdout.split() if re.fullmatch(r"v\d+\.\d+\.\d+(-beta\.\d+)?", t)]
        if not tags:
            self.skipTest("no v* tag in the local repository")
        return tags[-1]

    def run_tool(self, *args: str, config: str | None) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_CONFIG_DIR", "HOME")}
        env["HOME"] = str(self.tmp / "home")
        env["USERPROFILE"] = env["HOME"]
        if config is not None:
            env["CLAUDE_CONFIG_DIR"] = config
        run_dir = self.tmp / "run"
        run_dir.mkdir(exist_ok=True)
        return subprocess.run([sys.executable, str(INSTALL), *args], env=env,
                              capture_output=True, text=True, cwd=str(run_dir))

    def test_out_is_unchanged(self):
        out = self.tmp / "out"
        res = self.run_tool(self.tag(), "--out", str(out), config=str(self.tmp / "profile"))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue((out / "govern" / "RELEASE").is_file())
        self.assertIn(str(out), res.stdout)
        self.assertFalse((self.tmp / "profile").exists())
        self.assertFalse((self.tmp / "home" / ".claude").exists())

    def test_variable_set_installs_into_the_profile(self):
        profile = self.tmp / "profile"
        res = self.run_tool(self.tag(), config=str(profile))
        self.assertEqual(res.returncode, 0, res.stderr)
        installed = [p for p in (profile / "skills").iterdir()
                     if (p / ".claude-plugin" / "plugin.json").is_file()]
        self.assertEqual(len(installed), 1)
        self.assertIn(str(installed[0]), res.stdout)
        self.assertFalse((self.tmp / "home" / ".claude").exists())

    def test_a_tilde_in_the_variable_is_the_home(self):
        res = self.run_tool(self.tag(), config="~/prof")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue((self.tmp / "home" / "prof" / "skills").is_dir())
        self.assertFalse((self.tmp / "home" / ".claude").exists())

    def test_a_blank_variable_installs_under_home(self):
        res = self.run_tool(self.tag(), config="  ")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue((self.tmp / "home" / ".claude" / "skills").is_dir())

    def test_variable_unset_installs_under_home(self):
        res = self.run_tool(self.tag(), config=None)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue((self.tmp / "home" / ".claude" / "skills").is_dir())


if __name__ == "__main__":
    unittest.main()
