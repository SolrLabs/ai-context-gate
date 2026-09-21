"""The release installers under `tools/release/`, run against a throwaway tagged repository.

    python3 -m unittest discover -s engine/tests -k tools
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
RELEASE = REPO / "tools" / "release"


def remove(path: Path) -> None:
    """Delete a tree the installers made read-only (directories too, which POSIX needs writable
    to unlink from and Windows needs writable to delete)."""
    for p in [path, *path.rglob("*")]:
        if not p.is_symlink():
            p.chmod(p.stat().st_mode | 0o700 if p.is_dir() else p.stat().st_mode | 0o600)
    shutil.rmtree(path)


class ReleaseInstallers(unittest.TestCase):
    """install-engine.py and install-plugin.py take every name from the tag they install, never
    from the working tree, and refuse a tag that is not a release of this tool."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(remove, self.dir)
        self.repo, self.home = self.dir / "repo", self.dir / "home"
        self.home.mkdir()
        for tool in ("install-engine.py", "install-plugin.py"):
            (self.repo / "tools" / "release").mkdir(parents=True, exist_ok=True)
            shutil.copy(RELEASE / tool, self.repo / "tools" / "release" / tool)
        self.git("init", "-q")

    def git(self, *args: str) -> None:
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.email=t@t", "-c", "user.name=t",
                        "-c", "tag.gpgSign=false", "-c", "commit.gpgSign=false", *args],
                       check=True, capture_output=True)

    def release(self, version: str, tool: str | None, plugin: str) -> None:
        """Commit a release shaped like the real one, with its own names, and tag it. With no
        `tool`, the tag's layout.py names none."""
        govern = self.repo / "engine" / "govern"
        govern.mkdir(parents=True, exist_ok=True)
        (govern / "__init__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
        (govern / "layout.py").write_text(
            f'TOOL = "{tool}"\nGOV_DIR = f".{{TOOL}}"\n' if tool else 'GOV_DIR = ".gate"\n',
            encoding="utf-8")
        manifest = self.repo / "plugin" / ".claude-plugin" / "plugin.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({"name": plugin, "version": version}), encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-qm", version)
        self.git("tag", f"v{version}")

    def run_tool(self, tool: str, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(self.repo / "tools" / "release" / tool), *args],
                              capture_output=True, text=True, encoding="utf-8",
                              env={**os.environ, "HOME": str(self.home)}, timeout=60)

    def test_a_tag_whose_layout_names_no_tool_is_refused(self):
        self.release("1.2.3", None, "tag-plugin")
        res = self.run_tool("install-engine.py", "v1.2.3")
        self.assertEqual(res.returncode, 1)
        self.assertIn("v1.2.3: not a release tag of this tool", res.stderr)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_names_come_from_the_tag_not_the_working_tree(self):
        self.release("1.2.3", "tag-tool", "tag-plugin")
        # The working tree has since moved on to other names: they must not be used.
        (self.repo / "engine" / "govern" / "layout.py").write_text('TOOL = "tree-tool"\n',
                                                                     encoding="utf-8")
        (self.repo / "plugin" / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "tree-plugin", "version": "1.2.3"}), encoding="utf-8")

        res = self.run_tool("install-engine.py", "v1.2.3")
        self.assertEqual(res.returncode, 0, res.stderr)
        engine = self.home / ".local" / "share" / "tag-tool" / "engines" / "1.2.3"
        self.assertTrue((engine / "govern" / "layout.py").is_file())
        self.assertFalse((self.home / ".local" / "share" / "tree-tool").exists())

        res = self.run_tool("install-plugin.py", "v1.2.3")
        self.assertEqual(res.returncode, 0, res.stderr)
        plugin = self.home / ".claude" / "skills" / "tag-plugin"
        self.assertTrue((plugin / ".claude-plugin" / "plugin.json").is_file())
        self.assertTrue((plugin / "govern" / "__init__.py").is_file())
        # The bundled engine carries the release stamp adopt looks for; the repo never does.
        self.assertEqual((plugin / "govern" / "RELEASE").read_text(encoding="utf-8"), "v1.2.3\n")
        self.assertFalse((self.repo / "engine" / "govern" / "RELEASE").exists())
        self.assertFalse((self.home / ".claude" / "skills" / "tree-plugin").exists())
        self.assertIn("assembled plugin tag-plugin 1.2.3", res.stdout)


if __name__ == "__main__":
    unittest.main()
