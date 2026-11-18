"""The Claude profile folder: $CLAUDE_CONFIG_DIR when set, else `<home>/.claude`, and every
engine read or write of it going through `layout.claude_dir`."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ENGINE = Path(__file__).resolve().parent.parent
REPO = ENGINE.parent
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(REPO / "tools" / "ci"))

import profile_guard  # noqa: E402,F401

from govern import cli, installer, layout, local_plugin, notice, profile, usage_setup  # noqa: E402
from govern.checks import memory  # noqa: E402
from test_local_plugin import release_tree  # noqa: E402
import parallel_tests  # noqa: E402


class Profile(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(profile.rmtree, self.tmp)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.profile = self.tmp / "profile"

    def env(self, value: str | None = None) -> None:
        """HOME in the temporary folder; CLAUDE_CONFIG_DIR as given, or absent."""
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        if value is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = value


class ClaudeDir(Profile):
    def test_unset_is_the_homes_dot_claude(self):
        self.env()
        self.assertEqual(layout.claude_dir(), self.home / ".claude")

    def test_set_is_the_variables_folder(self):
        self.env(str(self.profile))
        self.assertEqual(layout.claude_dir(), self.profile)
        self.assertEqual(layout.claude_dir(self.home), self.profile)

    def test_whitespace_is_unset(self):
        self.env("  ")
        self.assertEqual(layout.claude_dir(), self.home / ".claude")

    def test_another_home_gets_its_own_default(self):
        self.env(str(self.profile))
        other = self.tmp / "other"
        self.assertEqual(layout.claude_dir(other), other / ".claude")

    def test_a_tilde_is_expanded(self):
        self.env("~/work-profile")
        self.assertEqual(layout.claude_dir(), Path("~/work-profile").expanduser())
        self.assertNotIn("~", str(layout.claude_dir()))


class UsageSetup(Profile):
    def test_install_writes_the_profiles_settings_and_backs_up_the_old_one(self):
        self.env(str(self.profile))
        self.profile.mkdir()
        (self.profile / "settings.json").write_text('{"model": "opus"}', encoding="utf-8")
        usage_setup.install(self.home)
        data = json.loads((self.profile / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(data["model"], "opus")
        self.assertIn("statusLine", data)
        backup = usage_setup.share_dir(self.home) / "settings.json.bak"
        self.assertEqual(json.loads(backup.read_text(encoding="utf-8")), {"model": "opus"})
        self.assertFalse((self.home / ".claude").exists())

    def test_user_settings_follows_the_profile(self):
        self.env(str(self.profile))
        self.assertEqual(installer.user_settings(self.home), self.profile / "settings.json")


class Notice(Profile):
    def test_enabled_plugins_reads_the_profiles_settings(self):
        self.env(str(self.profile))
        self.profile.mkdir()
        (self.profile / "settings.json").write_text(
            json.dumps({"enabledPlugins": {"x@y": True}}), encoding="utf-8")
        root = self.tmp / "proj"
        root.mkdir()
        self.assertEqual(notice.enabled_plugins(root, self.home), {"x@y": True})

    def test_plugin_available_finds_a_plugin_under_the_profiles_skills(self):
        self.env(str(self.profile))
        root = self.tmp / "proj"
        root.mkdir()
        self.assertFalse(notice.plugin_available(root, self.home))
        manifest = self.profile / "skills" / layout.BETA_PLUGIN / ".claude-plugin" / "plugin.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text("{}", encoding="utf-8")
        self.assertTrue(notice.plugin_available(root, self.home))


class LocalPlugin(Profile):
    def test_installs_into_the_profiles_skills(self):
        self.env(str(self.profile))
        tree = release_tree(self.tmp / "tree")
        local_plugin.install(tree, self.home)
        self.assertTrue((self.profile / "skills" / layout.PLUGIN / ".claude-plugin"
                         / "plugin.json").is_file())
        self.assertFalse((self.home / ".claude").exists())


class UsageError(Profile):
    def test_a_settings_file_that_is_not_an_object_is_named_once_each_way(self):
        self.env(str(self.profile))
        self.profile.mkdir()
        settings = self.profile / "settings.json"
        settings.write_text("[]", encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli.main(["usage", "install"])
        self.assertEqual(code, 2)
        self.assertEqual(
            err.getvalue(),
            f"error: {settings}: settings.json is not a JSON object; it was not changed\n")
        self.assertEqual(settings.read_text(encoding="utf-8"), "[]")


class Memory(Profile):
    def test_memory_index_is_under_the_profiles_projects(self):
        self.env(str(self.profile))
        path = memory.memory_index_path(SimpleNamespace(home=self.home, root=self.tmp / "proj"))
        self.assertEqual(path.relative_to(self.profile).parts[0], "projects")


class ParallelRunner(Profile):
    def test_a_grouped_test_does_not_see_the_variable(self):
        mod = self.tmp / "test_sees_profile.py"
        mod.write_text(
            "import os, unittest\n"
            "class T(unittest.TestCase):\n"
            "    def test_unset(self):\n"
            "        self.assertNotIn('CLAUDE_CONFIG_DIR', os.environ)\n",
            encoding="utf-8")
        self.env(str(self.profile))
        result = parallel_tests.run_group(self.tmp, "test_sees_profile")
        self.assertTrue(result["ok"], result["output"])


if __name__ == "__main__":
    unittest.main()
