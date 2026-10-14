"""The beta plugin: the switch an old launcher writes."""
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
from plugin_support import *  # noqa: E402,F401,F403


class BetaSwitch(unittest.TestCase):
    """The switch an old launcher writes (`context-gate@skills-dir`) is the name the beta plugin
    had before it had one of its own: the beta engine corrects it, and the beta plugin's
    session start removes the new key an old launcher's `beta off` leaves behind."""

    OLD, NEW = layout.OLD_SKILLS_DIR_PLUGIN_ID, layout.SKILLS_DIR_PLUGIN_ID

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.project = self.tmp / "project"
        (self.project / layout.GOV_DIR).mkdir(parents=True)
        (self.project / layout.CONFIG).write_text(
            f'[governance]\nengine = "{__version__}"\nschema = 1\n', encoding="utf-8")
        (self.project / ".claude").mkdir()
        self.settings = self.project / ".claude" / "settings.local.json"
        # The plugin as a beta's install assembles it: named `context-gate-beta`.
        self.built = self.tmp / "built"
        shutil.copytree(PLUGIN, self.built)
        shutil.copytree(ENGINE / "govern", self.built / "govern",
                        ignore=shutil.ignore_patterns("__pycache__"))
        manifest = self.built / ".claude-plugin" / "plugin.json"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        manifest.write_text(json.dumps({**data, "name": layout.BETA_PLUGIN}), encoding="utf-8")

    def put(self, data) -> bytes:
        text = data if isinstance(data, str) else json.dumps(data, indent=2) + "\n"
        self.settings.write_text(text, encoding="utf-8", newline="")
        return self.settings.read_bytes()

    def got(self) -> dict:
        return json.loads(self.settings.read_text(encoding="utf-8"))

    OTHERS = {"permissions": {"allow": ["Bash(ls)"]},
              "enabledPlugins": {"a@b": True, "context-gate@skills-dir": True,
                                 "context-gate@context-gate": False, "z@y": False},
              "model": "opus"}

    def test_the_old_ids_switch_becomes_the_new_ones_and_nothing_else_moves(self):
        self.put(self.OTHERS)
        line = notice.correct_old_launcher_switch(self.project)
        self.assertEqual(len((line or "").splitlines()), 1)
        self.assertIn("restart Claude Code to load it", line)
        self.assertIn(self.NEW, line)
        got = self.got()
        self.assertEqual(list(got), ["permissions", "enabledPlugins", "model"])
        self.assertEqual(got["permissions"], self.OTHERS["permissions"])
        self.assertEqual(list(got["enabledPlugins"]),
                         ["a@b", self.NEW, "context-gate@context-gate", "z@y"])
        self.assertEqual(got["enabledPlugins"], {"a@b": True, self.NEW: True,
                                                 "context-gate@context-gate": False, "z@y": False})
        self.assertEqual(list(self.settings.parent.glob("*.tmp")), [])

    def test_the_correction_is_idempotent(self):
        self.put(self.OTHERS)
        self.assertIsNotNone(notice.correct_old_launcher_switch(self.project))
        once = self.settings.read_bytes()
        self.assertIsNone(notice.correct_old_launcher_switch(self.project))
        self.assertEqual(self.settings.read_bytes(), once)
        self.assertNotIn(self.OLD, self.got()["enabledPlugins"])

    def test_the_new_id_set_to_false_is_set_to_true(self):
        self.put({"enabledPlugins": {self.OLD: True, self.NEW: False}})
        self.assertIsNotNone(notice.correct_old_launcher_switch(self.project))
        self.assertEqual(self.got(), {"enabledPlugins": {self.NEW: True}})

    def test_every_other_state_changes_nothing(self):
        cases = {
            "old id off": {"enabledPlugins": {self.OLD: False}},
            "old id absent": {"enabledPlugins": {"a@b": True}},
            "new id on already": {"enabledPlugins": {self.NEW: True}},
            "both on": {"enabledPlugins": {self.OLD: True, self.NEW: True}},
            "no enabledPlugins": {"permissions": {}},
            "enabledPlugins is a list": {"enabledPlugins": [self.OLD]},
            "not an object": [self.OLD],
            "old id is a string": {"enabledPlugins": {self.OLD: "true"}},
            "malformed": "{not json",
            "empty": "",
        }
        for name, data in cases.items():
            with self.subTest(state=name):
                before = self.put(data)
                self.assertIsNone(notice.correct_old_launcher_switch(self.project))
                self.assertEqual(self.settings.read_bytes(), before)
        self.settings.write_bytes(b'{"enabledPlugins": {"x": \xff}}')
        self.assertIsNone(notice.correct_old_launcher_switch(self.project))
        self.settings.unlink()
        self.assertIsNone(notice.correct_old_launcher_switch(self.project))
        self.assertFalse(self.settings.exists())
        shutil.rmtree(self.project / ".claude")
        self.assertIsNone(notice.correct_old_launcher_switch(self.project))
        self.assertFalse((self.project / ".claude").exists())

    def test_a_file_that_cannot_be_written_is_left_as_it_was(self):
        before = self.put(self.OTHERS)
        with mock.patch.object(os, "replace", side_effect=PermissionError(13, "in use")):
            self.assertIsNone(notice.correct_old_launcher_switch(self.project))
        self.assertEqual(self.settings.read_bytes(), before)
        self.assertEqual(list(self.settings.parent.iterdir()), [self.settings])

    def test_the_files_own_bom_and_line_endings_are_kept(self):
        text = json.dumps(self.OTHERS, indent=2).replace("\n", "\r\n") + "\r\n"
        self.settings.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
        self.assertIsNotNone(notice.correct_old_launcher_switch(self.project))
        raw = self.settings.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(raw.count(b"\r\n"), raw.count(b"\n"))
        self.assertIn(self.NEW, self.got_bom(raw))

    @staticmethod
    def got_bom(raw: bytes) -> dict:
        return json.loads(raw.decode("utf-8-sig"))["enabledPlugins"]

    # The beta plugin's session start, after an old launcher's `beta off`.
    def hook(self) -> str:
        env = {**os.environ, "HOME": str(self.home), "CLAUDE_PROJECT_DIR": str(self.project),
               "CONTEXT_GATE_NO_UPDATE_CHECK": "1"}
        res = subprocess.run([sys.executable, str(self.built / "hooks" / "session_start.py")],
                             input="{}", env=env, capture_output=True, text=True)
        self.assertEqual((res.returncode, res.stderr), (0, ""))
        return res.stdout

    LEFT = {"permissions": {"deny": ["Bash(rm:*)"]},
            "enabledPlugins": {"a@b": True, "context-gate-beta@skills-dir": True,
                               "context-gate@context-gate": True},
            "model": "opus"}
    SAID = ("the beta is off in this project; removed context-gate-beta@skills-dir from "
            ".claude/settings.local.json. Restart Claude Code to unload the beta plugin.")

    def test_the_beta_plugin_removes_its_key_where_no_beta_is_on(self):
        self.put(self.LEFT)
        out = json.loads(self.hook())
        self.assertIn(self.SAID, out["systemMessage"].splitlines())
        self.assertIn(self.SAID, out["hookSpecificOutput"]["additionalContext"])
        got = self.got()
        self.assertEqual(list(got), ["permissions", "enabledPlugins", "model"])
        self.assertEqual(got["enabledPlugins"], {"a@b": True, "context-gate@context-gate": True})
        self.assertEqual(got["permissions"], self.LEFT["permissions"])
        once = self.settings.read_bytes()
        self.assertNotIn("the beta is off", self.hook())    # idempotent: nothing left to remove
        self.assertEqual(self.settings.read_bytes(), once)

    def test_the_beta_plugin_leaves_a_project_on_a_beta_alone(self):
        before = self.put(self.LEFT)
        (self.project / layout.LOCAL).write_text(
            f'[governance]\nengine = "{BETA}"\n', encoding="utf-8")
        self.assertNotIn("the beta is off", self.hook())
        self.assertEqual(self.settings.read_bytes(), before)
        # A local.toml that cannot be read says nothing about the beta: also left alone.
        (self.project / layout.LOCAL).write_bytes(b"\xff[")
        self.assertNotIn("the beta is off", self.hook())
        self.assertEqual(self.settings.read_bytes(), before)

    def test_the_stable_plugin_and_an_ungoverned_project_are_left_alone(self):
        before = self.put(self.LEFT)
        manifest = self.built / ".claude-plugin" / "plugin.json"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        manifest.write_text(json.dumps({**data, "name": layout.PLUGIN}), encoding="utf-8")
        self.assertNotIn("the beta is off", self.hook())
        self.assertEqual(self.settings.read_bytes(), before)
        manifest.write_text(json.dumps({**data, "name": layout.BETA_PLUGIN}), encoding="utf-8")
        (self.project / layout.CONFIG).unlink()
        self.assertEqual(self.hook(), "")
        self.assertEqual(self.settings.read_bytes(), before)

    def test_a_beta_plugin_session_without_the_key_or_the_file_says_nothing(self):
        self.assertEqual(self.hook(), "")
        self.put({"enabledPlugins": {"a@b": True}})
        self.assertEqual(self.hook(), "")
        self.put({"enabledPlugins": {self.NEW: False}})
        self.assertEqual(self.hook(), "")
        self.put("{not json")
        self.assertEqual(self.hook(), "")
        shutil.rmtree(self.project / ".claude")
        self.assertEqual(self.hook(), "")
        self.assertFalse((self.project / ".claude").exists())


class BuildSkillGuidance(unittest.TestCase):
    """Two lines the build skill needs, each found by a real build."""

    def build_text(self) -> str:
        text = (PLUGIN / "skills/build/SKILL.md").read_text(encoding="utf-8")
        return " ".join(text.split())     # a re-wrapped line must not fail a phrase

    def test_red_gate_outside_the_diff_has_a_line(self):
        self.assertIn("red for a reason outside the unit's diff", self.build_text())

    def test_data_removing_command_has_a_line(self):
        self.assertIn("confirm which copy it points at", self.build_text())


class ReportSkill(unittest.TestCase):
    """The `report` skill, and the lines that point a coordinator at it."""

    def squash(self, path: Path) -> str:
        return " ".join(path.read_text(encoding="utf-8").split())

    def skill(self) -> str:
        return self.squash(PLUGIN / "skills/report/SKILL.md")

    def test_frontmatter_parses_and_names_the_commands(self):
        raw = (PLUGIN / "skills/report/SKILL.md").read_text(encoding="utf-8")
        head = raw.split("---")[1]
        fields = dict(line.split(": ", 1) for line in head.strip().splitlines())
        self.assertEqual(fields["name"], "report")
        self.assertIn("AskUserQuestion", fields["allowed-tools"])
        self.assertIn("argument-hint", fields)
        for command in ("govern report draft", "govern report check", "govern report send"):
            self.assertIn(command, self.skill())

    def test_send_comes_only_after_the_question(self):
        text = self.skill()
        self.assertLess(text.index("AskUserQuestion", text.index("# Report")),
                        text.index("govern report send"))
        self.assertIn("Only on \"open\"", text)

    def test_no_other_route_and_no_subagent_send(self):
        text = self.skill()
        self.assertIn("Never submit it for them by another route", text)
        self.assertIn("A subagent never runs `send`", text)

    def test_build_rules_and_build_skill_hold_the_new_phrases(self):
        rules = self.squash(ENGINE / "govern/templates/build-rules.md")
        self.assertIn("written up with the plugin's `report` skill when the phase closes", rules)
        build = self.squash(PLUGIN / "skills/build/SKILL.md")
        self.assertIn("any fault in context-gate itself, with the draft the `report` skill made", build)
        self.assertIn("start it with `--high-risk` all the same", build)

    def test_adopt_and_options_point_at_each_other(self):
        self.assertIn("use this plugin's `options` skill instead",
                      self.squash(PLUGIN / "skills/adopt/SKILL.md"))
        self.assertIn("is set up with the `adopt` skill",
                      self.squash(PLUGIN / "skills/options/SKILL.md"))


if __name__ == "__main__":
    unittest.main()
