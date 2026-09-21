"""The user docs under `docs/` say only what the engine does: `docs/checks.md` is exactly what
`tools/render-checks.py` renders from the check manifest, and every config example in
`docs/configuration.md` loads with the real loader.

    python3 -m unittest discover -s engine/tests -k docs
"""
from __future__ import annotations

import importlib.util
import re
import sys
import tempfile
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
REPO = ENGINE.parent
sys.path.insert(0, str(ENGINE))

from govern import __version__, config, layout  # noqa: E402

DOCS = REPO / "docs"
RENDER = REPO / "tools" / "render-checks.py"
USER_DOCS = ("how-it-works.md", "configuration.md", "checks.md")

# A ```toml block whose first line starts with this is not a whole config.toml (a registry
# file, a profile, or a config that needs files the test does not create): it is not loaded.
FRAGMENT = "# fragment"
TOML_BLOCK = re.compile(r"^```toml\n(.*?)^```", re.M | re.S)
PIN = re.compile(r'^(engine\s*=\s*)"[^"]*"', re.M)


def read(path: Path) -> str:
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read().replace("\r\n", "\n")


def load_renderer():
    spec = importlib.util.spec_from_file_location("render_checks", RENDER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ChecksReference(unittest.TestCase):
    def test_committed_checks_md_matches_the_manifest(self):
        rendered = load_renderer().render()
        self.assertEqual(read(DOCS / "checks.md"), rendered,
                         "docs/checks.md is out of date: run python3 tools/render-checks.py")

    def test_every_built_in_check_is_on_the_page(self):
        from govern import manifest
        text = read(DOCS / "checks.md")
        for cid, chk in manifest.CHECKS.items():
            if chk.origin == "engine":
                self.assertIn(f"### `{cid}`", text)

    def test_a_table_parameters_keys_follow_its_meaning_as_a_sentence(self):
        page = load_renderer().render()
        self.assertIn("Pairings that need a recorded decision. Keys: `a`, `b`, `decision`.", page)
        self.assertIsNone(re.search(r"[a-z]Keys:", page))


class ConfigurationExamples(unittest.TestCase):
    def blocks(self) -> list[str]:
        return TOML_BLOCK.findall(read(DOCS / "configuration.md"))

    def test_every_whole_config_example_loads(self):
        whole = [b for b in self.blocks() if not b.startswith(FRAGMENT)]
        self.assertGreaterEqual(len(whole), 10)
        for block in whole:
            with self.subTest(block=block.splitlines()[:3]), \
                    tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / layout.GOV_DIR).mkdir()
                self.assertTrue(PIN.search(block), "an example config pins the engine")
                text = PIN.sub(lambda m: f'{m.group(1)}"{__version__}"', block, count=1)
                (root / config.CONFIG_NAME).write_text(text, encoding="utf-8")
                config.load(root, root)

    def test_every_fragment_is_still_valid_toml(self):
        import tomllib
        fragments = [b for b in self.blocks() if b.startswith(FRAGMENT)]
        self.assertTrue(fragments)
        for block in fragments:
            with self.subTest(block=block.splitlines()[0]):
                tomllib.loads(block)

    def test_every_table_and_key_the_loader_accepts_is_documented(self):
        text = read(DOCS / "configuration.md")
        for section, keys in config.LAYOUT.items():
            self.assertIn(f"`[{section}]`", text)
            for key in keys:
                self.assertIn(f"`{key}`", text, f"[{section}] {key}")
        for key, choices in config.DIALECT_CHOICES.items():
            for choice in choices:
                self.assertIn(f'`"{choice}"`', text, f"[dialect] {key} = {choice}")
        for key in config.REGISTRY_KEYS:
            self.assertIn(f"`{key}`", text, f"[repo] {key}")
        for key in ("level", "reason", "reasons", "ratchet", "extend_<param>"):
            self.assertIn(f"`{key}`", text)


class UserDocs(unittest.TestCase):
    def test_the_docs_link_to_each_other(self):
        for name in USER_DOCS:
            text = read(DOCS / name)
            for other in USER_DOCS:
                if other != name:
                    self.assertIn(f"]({other}", text, f"{name} links to {other}")

    def test_frontmatter_is_valid_yaml(self):
        # GitHub renders frontmatter as YAML: an unquoted value holding ": " or " #", or starting
        # with a YAML indicator, breaks the page's frontmatter table.
        docs = [REPO / "README.md", REPO / "CONTRIBUTING.md", ENGINE / "README.md",
                REPO / "plugin" / "README.md", *(DOCS / n for n in USER_DOCS)]
        for doc in docs:
            if not doc.is_file():
                continue
            text = read(doc)
            if not text.startswith("---\n"):
                continue
            for line in text[4:text.index("\n---", 4)].splitlines():
                m = re.match(r"[\w-]+:\s*(.*)", line)
                value = m.group(1) if m else ""
                with self.subTest(doc=doc.name, line=line):
                    self.assertFalse(value and value[0] not in "\"'[{" and (
                        ": " in value or " #" in value or value[0] in "&*!|>%@`-?"),
                        "quote this value or reword it")



if __name__ == "__main__":
    unittest.main()
