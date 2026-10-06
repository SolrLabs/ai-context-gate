"""The local plugin as the engine assembles it (`govern.local_plugin`), from one release's tree:
the same files `tools/release/install-plugin.py` installs, swapped in whole.

    python3 -m unittest discover -s engine/tests -k local_plugin
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parent.parent
REPO = ENGINE.parent
sys.path.insert(0, str(ENGINE))

from govern import __version__, layout, local_plugin, profile  # noqa: E402

VERSION = "9.8.7-beta.3"
TOOL = REPO / "tools" / "release" / "install-plugin.py"


def wtext(path: Path, content: str) -> None:
    """Exactly these characters, on every OS: UTF-8, no newline translation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(content)


def listing(top: Path) -> dict:
    """Every path under top: a file's bytes, None for a directory."""
    return {p.relative_to(top).as_posix(): p.read_bytes() if p.is_file() else None
            for p in sorted(top.rglob("*"))}


def release_tree(where: Path, version: str = VERSION, plugin_version: str | None = None,
                 name: str | None = None) -> Path:
    """`plugin/` and `engine/govern/` as one release tag holds them: this repository's own, each
    carrying `version` (the plugin `plugin_version`, when the two are to disagree), the plugin
    under its own name or `name`."""
    ignore = shutil.ignore_patterns("__pycache__")
    shutil.copytree(REPO / "plugin", where / "plugin", ignore=ignore)
    shutil.copytree(ENGINE / "govern", where / "engine" / "govern", ignore=ignore)
    init = where / "engine" / "govern" / "__init__.py"
    wtext(init, init.read_text(encoding="utf-8").replace(
        f'__version__ = "{__version__}"', f'__version__ = "{version}"'))
    manifest = where / "plugin" / ".claude-plugin" / "plugin.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["version"] = plugin_version or version
    data["name"] = name or data["name"]
    wtext(manifest, json.dumps(data, indent=2) + "\n")
    return where


class LocalPlugin(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(profile.rmtree, self.tmp)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.skills = self.home / ".claude" / "skills"
        self.out = self.skills / layout.PLUGIN

    def module(self, tree: Path, engine: Path | None = None,
               *more: str) -> subprocess.CompletedProcess:
        """Run as the entry point runs it: the tree's own engine (or `engine`, for a tree with
        none that loads), nothing written beside it."""
        env = {k: v for k, v in os.environ.items() if k != "GOVERN_ENGINE"}
        env["PYTHONPATH"] = str(engine or tree / "engine")
        return subprocess.run([sys.executable, "-P", "-B", "-m", "govern.local_plugin",
                               "--tree", str(tree), "--home", str(self.home), *more],
                              env=env, capture_output=True, text=True, encoding="utf-8",
                              timeout=60)

    def tagged(self, tree: Path) -> Path:
        """The tree committed and tagged in a repository of its own that also holds the
        release tool: what the tool is run from."""
        git = lambda *a: subprocess.run(
            ["git", "-C", str(tree), "-c", "user.email=t@t", "-c", "user.name=t",
             "-c", "tag.gpgSign=false", "-c", "commit.gpgSign=false", *a],
            check=True, capture_output=True)
        (tree / "tools" / "release").mkdir(parents=True)
        shutil.copy(TOOL, tree / "tools" / "release" / TOOL.name)
        git("init", "-q")
        git("add", "-A")
        git("commit", "-qm", VERSION)
        git("tag", f"v{VERSION}")
        return tree

    def tool(self, repo: Path, home: Path, *more: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(repo / "tools" / "release" / TOOL.name),
                               f"v{VERSION}", *more], capture_output=True, text=True,
                              encoding="utf-8", env={**os.environ, "HOME": str(home)},
                              timeout=60)

    def earlier_install(self, home: Path) -> None:
        """An older local install holding a read-only file, beside things that are not this
        plugin's: another skill, and the user's settings."""
        out = home / ".claude" / "skills" / layout.PLUGIN
        wtext(out / "govern" / layout.RELEASE_MARKER, "v0.0.1-beta.1\n")
        wtext(out / "hooks" / "gone.py", "# only the older plugin had this\n")
        (out / "hooks" / "gone.py").chmod(0o444)
        wtext(home / ".claude" / "skills" / "another" / "SKILL.md", "# someone else's\n")
        wtext(home / ".claude" / "settings.json", '{"enabledPlugins": {}}\n')

    def test_installs_plugin_and_engine_with_the_marker_and_off_by_default(self):
        tree = release_tree(self.tmp / "tree")
        before = listing(tree)
        res = self.module(tree)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.strip(),
                         f"assembled plugin {layout.PLUGIN} {VERSION} -> {self.out}")
        self.assertEqual((self.out / "govern" / layout.RELEASE_MARKER)
                         .read_text(encoding="utf-8"), f"v{VERSION}\n")
        manifest = json.loads((self.out / ".claude-plugin" / "plugin.json")
                              .read_text(encoding="utf-8"))
        self.assertEqual((manifest["name"], manifest["version"], manifest["defaultEnabled"]),
                         (layout.PLUGIN, VERSION, False))
        self.assertTrue((self.out / "hooks" / "session_start.py").is_file())
        self.assertIn(f'__version__ = "{VERSION}"',
                      (self.out / "govern" / "__init__.py").read_text(encoding="utf-8"))
        # The marker and the switch are in the installed copy only: the tree is as it was.
        self.assertEqual(listing(tree), before)
        self.assertEqual([p.name for p in self.skills.iterdir()], [layout.PLUGIN])
        self.assertEqual(list(self.out.rglob("__pycache__")), [])

    def test_leaves_what_the_release_tool_leaves(self):
        """For one tag, the tool and the module leave the same directory tree: names, bytes,
        the marker, `defaultEnabled`. Each replaces an older install and nothing beside it."""
        repo = self.tagged(release_tree(self.tmp / "repo"))
        # The tree the tool reads: the tag's plugin/ and engine/govern, as it exports them.
        tree = self.tmp / "tree"
        archive = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar",
                                  f"v{VERSION}", "plugin", "engine/govern"], check=True,
                                 capture_output=True).stdout
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(tree, filter="data")
        by_tool, by_module = self.tmp / "tool-home", self.home
        by_tool.mkdir()
        for home in (by_tool, by_module):
            self.earlier_install(home)
        res = self.tool(repo, by_tool)
        self.assertEqual(res.returncode, 0, res.stderr)
        res = self.module(tree)
        self.assertEqual(res.returncode, 0, res.stderr)
        tool, module = listing(by_tool), listing(by_module)
        self.assertEqual(sorted(module), sorted(tool))
        self.assertEqual(module, tool)
        marker = f".claude/skills/{layout.PLUGIN}/govern/{layout.RELEASE_MARKER}"
        self.assertEqual(module[marker].split(), [f"v{VERSION}".encode()])
        manifest = json.loads(module[f".claude/skills/{layout.PLUGIN}/.claude-plugin/plugin.json"])
        self.assertIs(manifest["defaultEnabled"], False)
        self.assertNotIn(f".claude/skills/{layout.PLUGIN}/hooks/gone.py", module)
        self.assertEqual(module[".claude/skills/another/SKILL.md"], b"# someone else's\n")
        self.assertEqual(module[".claude/settings.json"], b'{"enabledPlugins": {}}\n')

    def test_a_second_run_leaves_the_same_tree(self):
        tree = release_tree(self.tmp / "tree")
        self.assertEqual(self.module(tree).returncode, 0)
        once = listing(self.home)
        res = self.module(tree)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(listing(self.home), once)

    def test_plugin_and_engine_versions_that_disagree_are_refused(self):
        self.earlier_install(self.home)
        before = listing(self.home)
        for name, tree in (
                ("plugin", release_tree(self.tmp / "a", plugin_version="9.8.7-beta.2")),
                ("engine", release_tree(self.tmp / "b", version=__version__,
                                        plugin_version=VERSION))):
            with self.subTest(differs=name):
                res = self.module(tree)
                self.assertEqual(res.returncode, 1, res.stdout)
                self.assertEqual(len(res.stderr.strip().splitlines()), 1, res.stderr)
                self.assertIn("the engine says otherwise; plugin and engine release together",
                              res.stderr)
                self.assertEqual(listing(self.home), before)

    def test_a_tree_that_is_no_release_is_refused(self):
        cases = {}
        cases["no plugin"] = release_tree(self.tmp / "a")
        shutil.rmtree(cases["no plugin"] / "plugin")
        cases["no engine"] = release_tree(self.tmp / "b")
        shutil.rmtree(cases["no engine"] / "engine")
        cases["plugin.json is not JSON"] = release_tree(self.tmp / "c")
        (cases["plugin.json is not JSON"] / "plugin" / ".claude-plugin" / "plugin.json") \
            .write_bytes(b"\xff{")
        cases["a version that is no release"] = release_tree(self.tmp / "d", version="1.2")
        cases["no usable name"] = release_tree(self.tmp / "e")
        manifest = cases["no usable name"] / "plugin" / ".claude-plugin" / "plugin.json"
        wtext(manifest, json.dumps({"name": "../elsewhere", "version": VERSION}))
        self.earlier_install(self.home)
        before = listing(self.home)
        for name, tree in cases.items():
            with self.subTest(tree=name):
                res = self.module(tree, engine=ENGINE)
                self.assertEqual(res.returncode, 1, res.stdout)
                self.assertNotIn("Traceback", res.stderr)
                self.assertEqual(len(res.stderr.strip().splitlines()), 1, res.stderr)
                self.assertTrue(res.stderr.startswith("error: "), res.stderr)
                self.assertEqual(listing(self.home), before)
        self.assertFalse((self.home / ".claude" / "elsewhere").exists())

    def test_a_copy_that_fails_leaves_the_earlier_install_whole(self):
        """Assembled beside the destination and swapped in: a failure part-way through leaves
        the earlier install as it was and nothing half-built beside it."""
        tree = release_tree(self.tmp / "tree")
        self.earlier_install(self.home)
        before = listing(self.home)
        real, copied = shutil.copytree, []

        def engine_copy_fails(src, dst, *args, **kw):   # copytree calls itself for each directory
            if Path(src) == tree / "engine" / "govern":
                raise OSError(28, "No space left on device")
            copied.append(Path(src))
            return real(src, dst, *args, **kw)

        with mock.patch.object(shutil, "copytree", engine_copy_fails):
            with self.assertRaises(local_plugin.PluginError) as caught:
                local_plugin.install(tree, self.home)
        self.assertIn("No space left on device", str(caught.exception))
        self.assertIn(tree / "plugin", copied)          # it failed part-way, not at the start
        self.assertEqual(listing(self.home), before)

    def test_a_swap_that_fails_puts_the_earlier_install_back(self):
        tree = release_tree(self.tmp / "tree")
        self.earlier_install(self.home)
        before = listing(self.home)
        real, calls = os.rename, []

        def second_rename_fails(src, dst):
            calls.append(src)
            if len(calls) == 2:
                raise PermissionError(13, "a file in it is in use")
            return real(src, dst)

        with mock.patch.object(os, "rename", second_rename_fails):
            with self.assertRaises(local_plugin.PluginError):
                local_plugin.install(tree, self.home)
        self.assertEqual(listing(self.home), before)

    def test_what_an_interrupted_run_left_is_cleared(self):
        """Killed between the two moves, a run leaves the earlier install under its set-aside
        name and the new one half in place: the next run ends with one install and no more."""
        tree = release_tree(self.tmp / "tree")
        self.earlier_install(self.home)
        real, calls = os.rename, []

        def killed_at_the_second_rename(src, dst):
            calls.append(src)
            if len(calls) == 2:
                raise KeyboardInterrupt
            return real(src, dst)

        with mock.patch.object(os, "rename", killed_at_the_second_rename):
            with self.assertRaises(KeyboardInterrupt):
                local_plugin.install(tree, self.home)
        self.assertFalse(self.out.exists())
        self.assertEqual(len(list(self.skills.iterdir())), 3)     # set aside, staged, another
        res = self.module(tree)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(sorted(p.name for p in self.skills.iterdir()),
                         sorted(["another", layout.PLUGIN]))
        self.assertEqual((self.out / "govern" / layout.RELEASE_MARKER)
                         .read_text(encoding="utf-8"), f"v{VERSION}\n")

    def test_a_destination_that_is_no_install_is_refused(self):
        tree = release_tree(self.tmp / "tree")
        mine = self.tmp / "working-copy"
        wtext(mine / "SKILL.md", "# a checkout the user linked in\n")
        self.skills.mkdir(parents=True)
        try:
            self.out.symlink_to(mine, target_is_directory=True)
        except OSError:
            self.skipTest("symlinks are not available here")
        res = self.module(tree)
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertIn("is a symbolic link", res.stderr)
        self.assertEqual(len(res.stderr.strip().splitlines()), 1, res.stderr)
        self.assertTrue(self.out.is_symlink())
        self.assertEqual(listing(mine), {"SKILL.md": b"# a checkout the user linked in\n"})
        self.out.unlink()
        wtext(self.out, "a file where the plugin goes\n")
        res = self.module(tree)
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertIn("is not a directory", res.stderr)
        self.assertEqual(self.out.read_text(encoding="utf-8"), "a file where the plugin goes\n")

    def test_a_swap_that_cannot_be_undone_says_where_the_earlier_install_is(self):
        """Both moves fail: the earlier install is under its set-aside name. The error names
        that directory and says the next run puts it back, which it does."""
        tree = release_tree(self.tmp / "tree")
        self.earlier_install(self.home)
        before = listing(self.out)
        aside = self.skills / f".{layout.PLUGIN}.old"
        real, calls = os.rename, []

        def only_the_first_rename_works(src, dst):
            calls.append(src)
            if len(calls) > 1:
                raise PermissionError(13, "a file in it is in use")
            return real(src, dst)

        with mock.patch.object(os, "rename", only_the_first_rename_works):
            with self.assertRaises(local_plugin.PluginError) as caught:
                local_plugin.install(tree, self.home)
        said = str(caught.exception)
        self.assertEqual(len(said.splitlines()), 1, said)
        self.assertIn("a file in it is in use", said)
        self.assertIn(f"the earlier install is in {aside}", said)
        self.assertIn("the next run puts it back", said)
        self.assertFalse(self.out.exists())
        self.assertEqual(listing(aside), before)
        res = self.module(tree)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(sorted(p.name for p in self.skills.iterdir()),
                         sorted(["another", layout.PLUGIN]))
        self.assertEqual((self.out / "govern" / layout.RELEASE_MARKER)
                         .read_text(encoding="utf-8"), f"v{VERSION}\n")

    def test_a_directory_this_tool_did_not_install_is_refused(self):
        """Only a local install made by this tool is replaced: a directory in the plugin's
        place that carries no release stamp is someone's own, whatever it holds."""
        tree = release_tree(self.tmp / "tree")
        cases = {"a skill of the user's": {"SKILL.md": "# mine\n", "govern/notes.md": "x\n"},
                 "an empty directory": {}}
        for name, files in cases.items():
            with self.subTest(holds=name):
                profile.rmtree(self.skills)
                self.out.mkdir(parents=True)
                for rel, text in files.items():
                    wtext(self.out / rel, text)
                wtext(self.home / ".claude" / "settings.json", '{"enabledPlugins": {}}\n')
                before = listing(self.home)
                res = self.module(tree)
                self.assertEqual(res.returncode, 1, res.stdout)
                self.assertNotIn("Traceback", res.stderr)
                self.assertEqual(len(res.stderr.strip().splitlines()), 1, res.stderr)
                self.assertIn(f"error: {self.out} is not a local install of the plugin",
                              res.stderr)
                self.assertEqual(listing(self.home), before)

    def test_a_tree_under_another_name_than_the_one_expected_is_refused(self):
        """The caller says which plugin it came for. A tree whose plugin.json gives another
        name installs nothing, not even over a directory this tool stamped."""
        tree = release_tree(self.tmp / "tree", name="another")
        wtext(self.skills / "another" / "SKILL.md", "# someone else's\n")
        cases = {"no stamp": None, "stamped": "v0.0.1-beta.1\n"}
        for name, stamp in cases.items():
            with self.subTest(theirs=name):
                if stamp:
                    wtext(self.skills / "another" / "govern" / layout.RELEASE_MARKER, stamp)
                before = listing(self.home)
                res = self.module(tree, None, "--name", layout.PLUGIN)
                self.assertEqual(res.returncode, 1, res.stdout)
                self.assertEqual(len(res.stderr.strip().splitlines()), 1, res.stderr)
                self.assertIn(f"is the plugin 'another', not '{layout.PLUGIN}'", res.stderr)
                self.assertEqual(listing(self.home), before)
        # Its own name is the one expected: installed as ever.
        own = release_tree(self.tmp / "own")
        res = self.module(own, None, "--name", layout.PLUGIN)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue((self.out / "govern" / layout.RELEASE_MARKER).is_file())

    def test_the_release_tool_refuses_a_directory_it_did_not_install(self):
        """The same rule in `tools/release/install-plugin.py`, for the plugin's place only: a
        directory with no release stamp is not deleted. `--out` names where to assemble and
        replaces what is there, as it always did."""
        repo = self.tagged(release_tree(self.tmp / "repo", name="another"))
        wtext(self.skills / "another" / "SKILL.md", "# someone else's\n")
        elsewhere = self.tmp / "elsewhere"
        before = listing(self.home)
        res = self.tool(repo, self.home)
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertNotIn("Traceback", res.stderr)
        self.assertEqual(len(res.stderr.strip().splitlines()), 1, res.stderr)
        self.assertIn("is not a local install of the plugin", res.stderr)
        self.assertEqual(listing(self.home), before)
        # What the tool itself assembled there before is replaced, as it always was.
        wtext(self.skills / "another" / "govern" / "RELEASE", "v0.0.1-beta.1\n")
        res = self.tool(repo, self.home)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual((self.skills / "another" / "govern" / "RELEASE")
                         .read_text(encoding="utf-8"), f"v{VERSION}\n")

    def test_out_replaces_a_directory_whatever_it_holds(self):
        """`--out DIR` on an existing directory, empty or not, stamped or not, replaces it."""
        repo = self.tagged(release_tree(self.tmp / "repo", name="another"))
        for name, files in {"empty": {}, "unstamped": {"kept.txt": "not a plugin\n"},
                            "stamped": {"govern/RELEASE": "v0.0.1-beta.1\n"}}.items():
            with self.subTest(holds=name):
                elsewhere = self.tmp / f"elsewhere-{name}"
                elsewhere.mkdir()
                for rel, text in files.items():
                    wtext(elsewhere / rel, text)
                res = self.tool(repo, self.home, "--out", str(elsewhere))
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertEqual((elsewhere / "govern" / "RELEASE").read_text(encoding="utf-8"),
                                 f"v{VERSION}\n")
                self.assertFalse((elsewhere / "kept.txt").exists())

    def test_home_defaults_to_the_users(self):
        tree = release_tree(self.tmp / "tree")
        env = {k: v for k, v in os.environ.items() if k != "GOVERN_ENGINE"}
        env.update(PYTHONPATH=str(tree / "engine"), HOME=str(self.home))
        res = subprocess.run([sys.executable, "-P", "-B", "-m", "govern.local_plugin",
                              "--tree", str(tree)], env=env, capture_output=True, text=True,
                             encoding="utf-8", timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue((self.out / "govern" / layout.RELEASE_MARKER).is_file())


if __name__ == "__main__":
    unittest.main()
