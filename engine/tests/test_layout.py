"""The tool's names come from one place, `govern.layout`. What cannot import it (the entry-point
template a project runs before any engine is on the path, the plugin's manifests, the skills'
prose, the release tools' docstrings) carries its own copy, and these tests hold each copy to
layout's."""
from __future__ import annotations

import ast
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
REPO = ENGINE.parent
PLUGIN = REPO / "plugin"
sys.path.insert(0, str(ENGINE))

from govern import layout, notice  # noqa: E402

TEMPLATE = ENGINE / "govern" / "templates" / "entrypoint.py"
# A marketplace, checked when .claude-plugin/marketplace.json exists.
MARKET = REPO / ".claude-plugin" / "marketplace.json"
RELEASE_TOOLS = (REPO / "tools" / "release" / "install-engine.py",
                 REPO / "tools" / "release" / "install-plugin.py")
# A path inside the tool's own directory in a project: `.x/bin/...`, `.x/config.toml`, ...
GOV_PATH_RE = re.compile(
    r"(\.[\w-]+)/(?:bin|config\.toml|adopt-report|upgrade-report|installed\.toml)")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class EntrypointTemplate(unittest.TestCase):
    def setUp(self) -> None:
        self.text = read(TEMPLATE)

    def test_engines_dir_matches_layout(self):
        line = re.search(r"^ENGINES = HOME / (.+)$", self.text, re.M)
        self.assertIsNotNone(line)
        home = Path("home")
        self.assertEqual(home.joinpath(*re.findall(r'"([^"]+)"', line.group(1))),
                         layout.engines_dir(home))

    def test_gov_dir_matches_layout(self):
        self.assertEqual(set(re.findall(r"([\w.-]+)/bin/govern", self.text)), {layout.GOV_DIR})
        self.assertIn(f"~/.local/share/{layout.TOOL}/engines/", self.text)

    def test_messages_name_the_tool(self):
        named = (re.findall(r'"([\w-]+): installing engine', self.text)
                 + re.findall(r'"(?:no )?([\w-]+) engine \{pin\}', self.text)
                 + re.findall(r'already on ([\w-]+) \{', self.text))
        self.assertEqual(len(named), 4, named)
        self.assertEqual(set(named), {layout.DISPLAY_NAME})


class PluginNames(unittest.TestCase):
    def test_manifests_carry_the_layout_names(self):
        manifest = json.loads(read(PLUGIN / ".claude-plugin" / "plugin.json"))
        self.assertEqual(manifest["name"], layout.PLUGIN)
        self.assertEqual(manifest["displayName"], layout.DISPLAY_NAME)
        self.assertEqual(manifest["repository"], f"https://github.com/{layout.PUBLIC_REPO}")
        self.assertEqual(manifest["homepage"], f"https://github.com/{layout.PUBLIC_REPO}")

    @unittest.skipUnless(MARKET.is_file(), "no marketplace in this checkout")
    def test_a_marketplace_lists_this_plugin(self):
        market = json.loads(read(MARKET))
        self.assertEqual([p["name"] for p in market["plugins"]], [layout.PLUGIN])
        if market["name"] != layout.MARKETPLACE:
            # Any other marketplace takes the `-dev` name, so installing from a checkout cannot
            # collide with `context-gate@context-gate`.
            self.assertEqual(market["name"], f"{layout.MARKETPLACE}-dev")
            return
        # The public marketplace: the plugin pinned to its release zip on GitHub by URL and
        # sha256.
        entry = market["plugins"][0]
        source, version = entry["source"], re.escape(entry["version"])
        self.assertEqual(source["source"], "archive")
        self.assertRegex(source["url"], rf"^https://github\.com/{re.escape(layout.PUBLIC_REPO)}"
                                        rf"/releases/download/v{version}/"
                                        rf"{re.escape(layout.PLUGIN)}-{version}\.zip$")
        self.assertRegex(source["sha256"], r"^[0-9a-f]{64}$")


def prose() -> dict[str, str]:
    """Every text that names the tool without importing layout: the plugin's docs, its hook,
    the engine README, and the release tools' docstrings."""
    docs = [*sorted(PLUGIN.rglob("*.md")), PLUGIN / "hooks" / "session_start.py",
            ENGINE / "README.md"]
    out = {d.relative_to(REPO).as_posix(): read(d) for d in docs}
    for tool in RELEASE_TOOLS:
        out[tool.relative_to(REPO).as_posix()] = ast.get_docstring(ast.parse(read(tool))) or ""
    return out


def notice_words() -> str:
    """What the upgrade notice says, with the version as `X`: taken from notice.py itself."""
    with tempfile.TemporaryDirectory() as tmp:
        root, home = Path(tmp) / "project", Path(tmp) / "home"
        (root / layout.GOV_DIR).mkdir(parents=True)
        (root / layout.CONFIG).write_text('[governance]\nengine = "0.0.1"\n', encoding="utf-8")
        (layout.engines_dir(home) / "9.9.9" / "govern").mkdir(parents=True)
        msg = notice.for_project(root, home)
    return msg.split(" (", 1)[0].replace("9.9.9", "X")


class Prose(unittest.TestCase):
    def test_each_names_the_tool(self):
        for name, text in prose().items():
            with self.subTest(doc=name):
                self.assertIn(layout.TOOL, text)

    def test_gov_dir_paths_match_layout(self):
        for name, text in prose().items():
            with self.subTest(doc=name):
                self.assertLessEqual(set(GOV_PATH_RE.findall(text)), {layout.GOV_DIR})

    def test_skills_dirs_and_commands_match_layout(self):
        for name, text in prose().items():
            with self.subTest(doc=name):
                self.assertLessEqual(set(re.findall(r"/([\w-]+):(?:adopt|upgrade)\b", text)),
                                     {layout.PLUGIN})
                self.assertLessEqual(set(re.findall(r"skills/([\w-]+)/", text)), {layout.PLUGIN})
                self.assertLessEqual(set(re.findall(r"([\w-]+)@skills-dir", text)),
                                     {layout.PLUGIN})
                self.assertLessEqual(set(re.findall(r"share/([\w-]+)/engines", text)),
                                     {layout.TOOL})

    def test_upgrade_skill_quotes_the_notice(self):
        """The skill tells the agent it applies after the notice, so it must quote the notice
        as notice.py prints it."""
        head = read(PLUGIN / "skills" / "upgrade" / "SKILL.md").split("---")[1]
        words = notice_words()
        self.assertTrue(words.startswith(layout.DISPLAY_NAME + " X "), words)
        self.assertIn(f'"{words}"', head)


if __name__ == "__main__":
    unittest.main()
