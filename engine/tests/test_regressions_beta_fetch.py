"""Behaviour tests on small throwaway workspaces: fetching a beta."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from regressions_support import *  # noqa: E402,F401,F403

class BetaFetch(Base):
    """`govern beta on` installs what is missing from the project's `[governance] source`: the
    beta's engine, as a pinned release is fetched, and the local plugin, assembled by that beta's
    own engine from the same clone. Everything is fetched before the project is switched, and
    nothing but `beta on` ever fetches a beta. The sources are repositories in the temp
    directory: no test reaches the network."""

    TWO = f"{SERIES}.99-beta.2"

    def setUp(self) -> None:
        super().setUp()
        w = self.ws()
        self.w = w
        self.entry, self.env = self._installed(w)
        self.root, self.home = w.root, w.home
        self.env = {**self.env, "XDG_CONFIG_HOME": str(self.home)}
        subprocess.run(["git", "init", "-q", str(self.root)], env=self.env, check=True,
                       capture_output=True)
        (self.root / ".claude").mkdir()
        self.settings = self.root / ".claude" / "settings.local.json"
        self.engines = layout.engines_dir(self.home)
        self.skills = self.home / ".claude" / "skills"
        self.plugin = self.skills / layout.BETA_PLUGIN
        self.marker = self.plugin / "govern" / layout.RELEASE_MARKER

    def source(self, *tags: str, without_module: tuple = (), mismatched: tuple = (),
               plugin_name: str | None = None) -> Path:
        """A release source the project's config names: this repository's engine and plugin,
        one commit and tag per version, each carrying its own version as a release does. A
        version in `without_module` is a beta from before the engine could assemble its plugin;
        one in `mismatched` has a plugin.json that disagrees with its engine. With
        `plugin_name`, every tag's plugin.json gives that name instead of this plugin's."""
        src = self.tmp / "source"
        ignore = shutil.ignore_patterns("__pycache__")
        shutil.copytree(ENGINE / "govern", src / "engine" / "govern", ignore=ignore)
        shutil.copytree(REPO / "plugin", src / "plugin", ignore=ignore)
        git = lambda *a: self.source_git(src, *a)
        git("init", "-q")
        init = src / "engine" / "govern" / "__init__.py"
        module = src / "engine" / "govern" / "local_plugin.py"
        manifest = src / "plugin" / ".claude-plugin" / "plugin.json"
        engine, assembler = init.read_text(encoding="utf-8"), module.read_text(encoding="utf-8")
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["name"] = plugin_name or data["name"]
        for version in tags:
            wtext(init, engine.replace(f'__version__ = "{__version__}"',
                                       f'__version__ = "{version}"'))
            data["version"] = "0.0.1" if version in mismatched else version
            wtext(manifest, json.dumps(data, indent=2) + "\n")
            module.unlink(missing_ok=True)
            if version not in without_module:
                wtext(module, assembler)
            git("add", "-A")
            git("commit", "-qm", version, "--allow-empty")
            git("tag", f"v{version}")
        self.name_source(src.as_posix())
        return src

    def source_git(self, src: Path, *args: str) -> None:
        subprocess.run(["git", "-C", str(src), "-c", "user.email=t@t", "-c", "user.name=t",
                        "-c", "tag.gpgSign=false", "-c", "commit.gpgSign=false", *args],
                       check=True, capture_output=True)

    def name_source(self, source: str) -> None:
        cfg = self.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            "schema = 1", f'schema = 1\nsource = "{source}"', 1))

    def installed(self, version: str, plugin: bool = True) -> None:
        """A beta installed by hand: its engine and, unless told otherwise, the local plugin."""
        self._engines(self.w, (version,))
        if plugin:
            self.marker.parent.mkdir(parents=True, exist_ok=True)
            wtext(self.marker, f"v{version}\n")

    def run_beta(self, *args: str, env=None) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(self.entry), "beta", *args],
                              env=env or self.env, capture_output=True, text=True)

    def state(self) -> dict:
        """Every file in the project, its bytes, git's exclude included."""
        files = [p for p in self.root.rglob("*") if p.is_file() and ".git" not in p.parts]
        files.append(self.root / ".git" / "info" / "exclude")
        return {p: p.read_bytes() for p in files if p.is_file()}

    def engines_installed(self) -> list[str]:
        return sorted(p.name for p in self.engines.iterdir()) if self.engines.is_dir() else []

    def assert_installed(self, version: str) -> None:
        """Engine and local plugin are that beta's, whole: the engine read-only, the plugin
        stamped and off by default, and nothing left beside either."""
        engine = self.engines / version / "govern"
        self.assertIn(f'__version__ = "{version}"',
                      (engine / "__init__.py").read_text(encoding="utf-8"))
        self.assertEqual((engine / "cli.py").stat().st_mode & 0o222, 0)      # read-only
        self.assertEqual(self.marker.read_text(encoding="utf-8"), f"v{version}\n")
        manifest = json.loads((self.plugin / ".claude-plugin" / "plugin.json")
                              .read_text(encoding="utf-8"))
        self.assertEqual((manifest["name"], manifest["version"], manifest["defaultEnabled"]),
                         (layout.BETA_PLUGIN, version, False))
        self.assertTrue((self.plugin / "hooks" / "session_start.py").is_file())
        self.assertIn(f'__version__ = "{version}"',
                      (self.plugin / "govern" / "__init__.py").read_text(encoding="utf-8"))
        self.assertEqual(sorted(p.name for p in self.skills.iterdir()),
                         sorted([layout.PLUGIN, layout.BETA_PLUGIN]))
        self.assertEqual([p.name for p in self.engines.iterdir()
                          if p.name.startswith(".") or p.name.endswith(".tmp")], [])
        for top in (self.engines, self.plugin):
            self.assertEqual(list(top.rglob("__pycache__")) + list(top.rglob(".git")), [])

    def assert_nothing_fetched(self, res: subprocess.CompletedProcess) -> None:
        self.assertNotIn("installing", res.stderr)

    # A named beta, nothing installed.
    def test_on_a_named_beta_fetches_its_engine_and_plugin_then_switches(self):
        src = self.source(__version__, BETA, self.TWO)
        res = self.run_beta("on", BETA)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stderr.splitlines(),
                         [f"context-gate: installing engine {BETA} from {src.as_posix()}",
                          f"context-gate: installing the local plugin {BETA} from "
                          f"{src.as_posix()}"])
        self.assertEqual(res.stdout.splitlines()[0],
                         f"beta {BETA} on for this project, on this machine:")
        self.assertIn("Restart the Claude Code session", res.stdout)
        self.assertEqual(self.engines_installed(), [BETA])     # the one asked for, no other
        self.assert_installed(BETA)
        # The project now runs it: the gate loads that engine, and says so.
        self.assertTrue(self._which(self.entry, self.env).endswith(BETA))
        for stamp in (self.home / ".local" / "state" / "context-gate").glob("beta-notice-*"):
            stamp.unlink()              # the probe above said the line already
        gate = subprocess.run([sys.executable, str(self.entry), "check"], env=self.env,
                              capture_output=True, text=True)
        self.assertEqual(gate.returncode, 0, gate.stdout + gate.stderr)
        self.assertIn(f"context-gate: running beta {BETA} on this machine", gate.stderr)
        self.assertNotIn("installing", gate.stderr)
        status = self.run_beta().stdout
        self.assertIn(f"beta {BETA}\n  engine: installed\n  plugin: installed\n", status)
        self.assertIn("settings.local.json: beta on, stable off", status)

    def test_a_second_run_fetches_nothing(self):
        src = self.source(BETA)
        self.assertEqual(self.run_beta("on", BETA).returncode, 0)
        once = self.state(), tree_of(self.engines), tree_of(self.skills)
        profile_rmtree(src)                 # a fetch now could only fail
        res = self.run_beta("on", BETA)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stderr, "")
        self.assertEqual((self.state(), tree_of(self.engines), tree_of(self.skills)), once)

    def test_without_a_source_a_missing_beta_is_refused_as_before(self):
        before = self.state()
        res = self.run_beta("on", BETA)
        self.assertEqual(res.returncode, 2, res.stdout)
        self.assertEqual(res.stderr.strip(),
                         f"error: engine {BETA} is not installed — from a clone of the "
                         f"repository, run: python3 tools/release/install-engine.py v{BETA}")
        self.installed(BETA, plugin=False)
        res = self.run_beta("on", BETA)
        self.assertEqual(res.returncode, 2, res.stdout)
        self.assertEqual(res.stderr.strip(),
                         f"error: the local plugin is not {BETA} — from a clone of the "
                         f"repository, run: python3 tools/release/install-plugin.py v{BETA}")
        self.assertEqual(self.state(), before)

    def test_a_source_without_the_tag_changes_nothing(self):
        src = self.source(__version__, self.TWO)
        self.installed(self.TWO)
        wtext(self.plugin / "govern" / "kept.py", "# the plugin that is installed\n")
        before = self.state(), tree_of(self.engines), tree_of(self.skills)
        res = self.run_beta("on", BETA)
        self.assertEqual(res.returncode, 2, res.stdout)
        self.assertNotIn("Traceback", res.stderr)
        said = res.stderr.splitlines()
        self.assertEqual(said[0], f"context-gate: installing engine {BETA} from {src.as_posix()}")
        self.assertEqual(len(said), 2, res.stderr)
        self.assertTrue(said[1].startswith(
            f"error: could not fetch engine v{BETA} from {src.as_posix()}: "), said[1])
        self.assertEqual((self.state(), tree_of(self.engines), tree_of(self.skills)), before)

    def test_a_fetch_with_no_git_to_run_is_refused_in_one_line(self):
        self.source(BETA)
        nowhere = self.tmp / "empty-path"
        nowhere.mkdir()
        before = self.state()
        for args in (("on", BETA), ("on",)):
            with self.subTest(args=args):
                res = self.run_beta(*args, env={**self.env, "PATH": str(nowhere)})
                self.assertEqual(res.returncode, 2, res.stdout)
                self.assertNotIn("Traceback", res.stderr)
                self.assertTrue(res.stderr.splitlines()[-1].startswith("error: "), res.stderr)
                self.assertEqual((self.state(), self.engines_installed(), tree_of(self.skills)),
                                 (before, [], {}))

    def test_a_tag_carrying_another_engine_version_changes_nothing(self):
        src = self.source(__version__)
        self.source_git(src, "tag", f"v{BETA}")
        before = self.state()
        res = self.run_beta("on", BETA)
        self.assertEqual(res.returncode, 2, res.stdout)
        self.assertIn(f"tag v{BETA} does not carry engine {BETA}", res.stderr)
        self.assertEqual((self.state(), self.engines_installed(), tree_of(self.skills)),
                         (before, [], {}))

    # The plugin comes from the same fetched tree, assembled by that beta's own engine.
    def test_a_plugin_that_cannot_be_assembled_leaves_the_project_unswitched(self):
        """The fetch stops between engine and plugin: the engine stays, the plugin that was
        installed is whole, and neither local.toml nor the settings were touched."""
        self.source(BETA, mismatched=(BETA,))
        self.marker.parent.mkdir(parents=True)
        wtext(self.marker, f"v{self.TWO}\n")
        before = self.state(), tree_of(self.skills)
        res = self.run_beta("on", BETA)
        self.assertEqual(res.returncode, 2, res.stdout)
        self.assertNotIn("Traceback", res.stderr)
        said = res.stderr.splitlines()
        self.assertEqual(len(said), 3, res.stderr)
        self.assertTrue(said[2].startswith(
            f"error: could not install the local plugin {BETA} from "), said[2])
        self.assertIn("plugin and engine release together", said[2])
        self.assertEqual((self.state(), tree_of(self.skills)), before)
        self.assertEqual(self.engines_installed(), [BETA])
        self.assertFalse((self.root / layout.LOCAL).exists())
        self.assertFalse(self.settings.exists())
        self.assertEqual(self.run_beta().stdout, "no beta in this project\n")

    def test_a_beta_older_than_the_plugin_module_keeps_its_engine_and_names_the_tool(self):
        src = self.source(BETA, without_module=(BETA,))
        before = self.state()
        res = self.run_beta("on", BETA)
        self.assertEqual(res.returncode, 2, res.stdout)
        self.assertEqual(res.stderr.splitlines(),
                         [f"context-gate: installing engine {BETA} from {src.as_posix()}",
                          f"error: the local plugin is not {BETA} — from a clone of the "
                          f"repository, run: python3 tools/release/install-plugin.py v{BETA}"])
        self.assertEqual(self.engines_installed(), [BETA])
        self.assertEqual((self.state(), tree_of(self.skills)), (before, {}))

    def test_the_plugin_is_assembled_by_the_fetched_engine_not_the_entry_point(self):
        """A later beta may lay its plugin out differently: what lands is what that beta's own
        `local_plugin` module writes, run from the fetched tree."""
        src = self.source(BETA)
        module = src / "engine" / "govern" / "local_plugin.py"
        ends = "    return name, version\n"
        own = module.read_text(encoding="utf-8")
        self.assertEqual(own.count(ends), 1)
        wtext(module, own.replace(
            ends, "    (stage / 'laid-out-by-the-beta').write_text('', encoding='utf-8')\n" + ends))
        self.source_git(src, "commit", "-qam", "a new layout")
        self.source_git(src, "tag", "-f", f"v{BETA}")
        res = self.run_beta("on", BETA)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue((self.plugin / "laid-out-by-the-beta").is_file())
        self.assertEqual(self.marker.read_text(encoding="utf-8"), f"v{BETA}\n")

    def test_engine_installed_and_plugin_of_another_beta_fetches_the_plugin_only(self):
        """A machine holds one local plugin. Replacing another beta's says, in one line, what
        that means for the projects still on it; a first install has nothing to say."""
        src = self.source(BETA)
        self.installed(BETA, plugin=False)
        engine = tree_of(self.engines)
        shared = (f"The local plugin was {self.TWO}: other projects on this machine still on "
                  f"{self.TWO} now load this plugin, until they run govern beta on themselves.")
        for plugin in ("another beta's", "missing"):
            with self.subTest(plugin=plugin):
                (self.root / layout.LOCAL).unlink(missing_ok=True)
                profile_rmtree(self.skills)
                if plugin != "missing":
                    self.marker.parent.mkdir(parents=True)
                    wtext(self.marker, f"v{self.TWO}\n")
                    stale = self.plugin / "hooks" / "gone.py"
                    stale.parent.mkdir()
                    wtext(stale, "# only the other beta had this\n")
                    stale.chmod(0o444)
                res = self.run_beta("on", BETA)
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertEqual(res.stderr.splitlines(),
                                 [f"context-gate: installing the local plugin {BETA} from "
                                  f"{src.as_posix()}"])
                self.assertEqual(tree_of(self.engines), engine)        # not fetched again
                self.assertEqual(res.stdout.splitlines().count(shared),
                                 0 if plugin == "missing" else 1, res.stdout)
                self.assertEqual(res.stdout.count("other projects on this machine"),
                                 0 if plugin == "missing" else 1, res.stdout)
                self.assertEqual(self.marker.read_text(encoding="utf-8"), f"v{BETA}\n")
                self.assertFalse((self.plugin / "hooks" / "gone.py").exists())
                self.assertTrue((self.plugin / "hooks" / "session_start.py").is_file())
                self.assertEqual(sorted(p.name for p in self.skills.iterdir()),
                         sorted([layout.PLUGIN, layout.BETA_PLUGIN]))

    def test_what_is_not_the_plugin_under_the_users_claude_directory_is_untouched(self):
        self.source(BETA)
        (self.skills / "another").mkdir(parents=True)
        wtext(self.skills / "another" / "SKILL.md", "# someone else's\n")
        wtext(self.home / ".claude" / "settings.json", '{"enabledPlugins": {}}\n')
        before = {k: v for k, v in tree_of(self.home / ".claude").items()
                  if not k.startswith(f"skills/{layout.PLUGIN}")}
        self.assertEqual(self.run_beta("on", BETA).returncode, 0)
        after = tree_of(self.home / ".claude")
        ours = (layout.PLUGIN, layout.BETA_PLUGIN)       # the beta's folder, and the marker
        self.assertEqual({k: v for k, v in after.items()
                          if not any(k == f"skills/{n}" or k.startswith(f"skills/{n}/")
                                     for n in ours)}, before)
        self.assertIn(f"skills/{layout.BETA_PLUGIN}/govern/{layout.RELEASE_MARKER}", after)
        self.assertIn(f"skills/{layout.PLUGIN}/govern/{layout.RELEASE_MARKER}", after)

    # No version named: the newest of what is installed and what the source has.
    def test_on_without_a_version_installs_a_newer_beta_the_source_has(self):
        nine, ten = f"{SERIES}.99-beta.9", f"{SERIES}.99-beta.10"
        src = self.source(__version__, BETA, nine, ten, f"{SERIES}.98-beta.11")
        self.installed(BETA)
        res = self.run_beta("on")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stderr.splitlines(),
                         [f"context-gate: installing engine {ten} from {src.as_posix()}",
                          f"context-gate: installing the local plugin {ten} from "
                          f"{src.as_posix()}"])
        self.assertEqual(res.stdout.splitlines()[0],
                         f"beta {ten} on for this project, on this machine:")
        self.assertEqual(self.engines_installed(), sorted([BETA, ten]))
        self.assert_installed(ten)
        self.assertTrue(self._which(self.entry, self.env).endswith(ten))
        once = self.state()
        res = self.run_beta("on")                       # and again: nothing to fetch or change
        self.assertEqual((res.returncode, res.stderr), (0, ""))
        self.assertEqual(self.state(), once)

    def test_on_without_a_version_keeps_an_installed_beta_newer_than_the_sources(self):
        self.source(__version__, BETA)
        self.installed(self.TWO)
        res = self.run_beta("on")
        self.assertEqual((res.returncode, res.stderr), (0, ""))
        self.assertEqual(res.stdout.splitlines()[0],
                         f"beta {self.TWO} on for this project, on this machine:")
        self.assertEqual(self.engines_installed(), [self.TWO])

    def test_on_without_a_version_and_a_source_that_cannot_be_asked_takes_the_installed(self):
        self.installed(BETA)
        gone = (self.tmp / "no-such-source").as_posix()
        for source, why in ((None, "config.toml names no [governance] source"), (gone, gone)):
            with self.subTest(source=source):
                (self.root / layout.LOCAL).unlink(missing_ok=True)
                if source:
                    self.name_source(source)
                res = self.run_beta("on")
                self.assertEqual(res.returncode, 0, res.stderr)
                said = res.stderr.splitlines()
                self.assertEqual(len(said), 1, res.stderr)
                self.assertTrue(said[0].startswith(
                    "context-gate: the source could not be asked for a newer beta ("), said[0])
                self.assertIn(why, said[0])
                self.assertTrue(said[0].endswith("taking the newest installed"), said[0])
                self.assertEqual(res.stdout.splitlines()[0],
                                 f"beta {BETA} on for this project, on this machine:")

    def test_on_without_a_version_nothing_installed_and_no_source_to_ask_is_one_error(self):
        """Nothing is installed, so there is no "newest installed" to take: the one line is the
        error, and it carries why the source could not be asked."""
        gone = (self.tmp / "no-such-source").as_posix()
        for source, why in ((None, "config.toml names no [governance] source"),
                            (gone, f"{gone}: fatal: '{gone}' does not appear to be a git")):
            with self.subTest(source=source):
                if source:
                    self.name_source(source)
                before = self.state()
                res = self.run_beta("on")
                self.assertEqual(res.returncode, 2, res.stdout)
                said = res.stderr.splitlines()
                self.assertEqual(len(said), 1, res.stderr)
                self.assertTrue(said[0].startswith("error: no beta engine is installed, and the "
                                                   "source could not be asked for one ("), said[0])
                self.assertIn(why, said[0])
                self.assertNotIn("taking the newest installed", res.stderr)
                self.assertIn("python3 tools/release/install-engine.py vX.Y.Z-beta.N", said[0])
                self.assertEqual((self.state(), self.engines_installed()), (before, []))

    def test_on_without_a_version_refuses_a_source_beta_of_a_release_already_run(self):
        old = f"{__version__}-beta.4"
        self.source(f"{PREV_SERIES}.9-beta.1", old, __version__)
        before = self.state()
        res = self.run_beta("on")
        self.assertEqual(res.returncode, 2, res.stdout)
        self.assertEqual(res.stderr.strip(),
                         f"error: the newest beta, {old}, is not newer than the release this "
                         f"project runs ({__version__}); to run it anyway: govern beta on {old}")
        self.assertEqual((self.state(), self.engines_installed(), tree_of(self.skills)),
                         (before, [], {}))
        res = self.run_beta("on", old)                  # named, it is fetched and run
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assert_installed(old)

    def test_on_without_a_version_and_no_beta_installed_or_at_the_source(self):
        src = self.source(__version__, NEXT_RELEASE)
        before = self.state()
        res = self.run_beta("on")
        self.assertEqual(res.returncode, 2, res.stdout)
        self.assertEqual(res.stderr.strip(),
                         f"error: no beta engine is installed, and {src.as_posix()} has no "
                         "beta tag")
        self.assertEqual((self.state(), self.engines_installed()), (before, []))

    # Check everything, then switch.
    def test_a_project_that_would_be_refused_fetches_nothing(self):
        self.source(BETA)
        cases = [
            ("no .claude/ directory", lambda: (self.root / ".claude").rmdir()),
            ("not valid settings JSON", lambda: wtext(self.settings, "{not json")),
            ("is unreadable", lambda: (self.root / layout.LOCAL).write_bytes(b"\xff")),
            ("names no beta", lambda: wtext(self.root / layout.LOCAL,
                                            f'[governance]\nengine = "{__version__}"\n')),
        ]
        for words, arrange in cases:
            with self.subTest(words=words):
                for path in (self.root / layout.LOCAL, self.settings):
                    path.unlink(missing_ok=True)
                (self.root / ".claude").mkdir(exist_ok=True)
                arrange()
                before = self.state()
                res = self.run_beta("on", BETA)
                self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
                self.assertIn(words, res.stderr)
                self.assert_nothing_fetched(res)
                self.assertEqual((self.state(), self.engines_installed(), tree_of(self.skills)),
                                 (before, [], {}))

    def test_an_engine_copy_that_fails_leaves_no_engine_directory(self):
        """The engine is copied beside its place and moved in whole: a copy that stops part-way
        leaves nothing a later run could take for an installed engine."""
        self.source(BETA)
        probe = ("import runpy, shutil, sys\n"
                 "real, depth = shutil.copytree, []\n"
                 "def fails(*args, **kw):\n"         # copytree calls itself for each directory
                 "    depth.append(1)\n"
                 "    try:\n"
                 "        real(*args, **kw)\n"
                 "    finally:\n"
                 "        depth.pop()\n"
                 "    if not depth:\n"
                 "        raise OSError(28, 'No space left on device')\n"
                 "shutil.copytree = fails\n"
                 f"sys.argv = [{str(self.entry)!r}, 'beta', 'on', {BETA!r}]\n"
                 f"runpy.run_path({str(self.entry)!r}, run_name='__main__')\n")
        before = self.state()
        res = subprocess.run([sys.executable, "-c", probe], env=self.env, capture_output=True,
                             text=True)
        self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
        self.assertNotIn("Traceback", res.stderr)
        self.assertIn(f"error: could not install engine {BETA}", res.stderr.splitlines()[-1])
        self.assertIn("No space left on device", res.stderr.splitlines()[-1])
        self.assertEqual((self.state(), self.engines_installed(), tree_of(self.skills)),
                         (before, [], {}))
        res = self.run_beta("on", BETA)                 # and the next run installs it whole
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assert_installed(BETA)

    def test_off_and_status_never_ask_the_source(self):
        self.source(BETA, self.TWO)
        self.assertEqual(self.run_beta("on", BETA).returncode, 0)
        installed = tree_of(self.engines), tree_of(self.skills)
        # git is here and the source has a newer beta: status does not go and look.
        res = self.run_beta()
        self.assertEqual((res.returncode, res.stderr), (0, ""))
        self.assertIn(f"beta {BETA}\n  engine: installed\n  plugin: installed\n", res.stdout)
        self.assertNotIn(self.TWO, res.stdout)
        # Nor does either run git at all: the only git on the path records every call.
        only = self.tmp / "only-git"
        only.mkdir()
        called = only / "called"
        wtext(only / "git", f'#!/bin/sh\necho "$@" >> "{called.as_posix()}"\nexit 1\n')
        (only / "git").chmod(0o755)
        env = {**self.env, "PATH": str(only)}
        res = self.run_beta(env=env)
        self.assertEqual((res.returncode, res.stderr), (0, ""))
        res = self.run_beta("off", env=env)
        self.assertEqual((res.returncode, res.stderr), (0, ""))
        self.assertFalse((self.root / layout.LOCAL).exists())
        self.assertFalse(called.exists())
        self.assertEqual((tree_of(self.engines), tree_of(self.skills)), installed)
        if os.name != "nt":             # the recorder does record: `beta on` asks the source
            self.run_beta("on", env=env)
            self.assertIn("ls-remote", called.read_text(encoding="utf-8"))

    # Never without being asked.
    def test_a_gate_run_and_a_session_start_never_fetch_the_beta_local_toml_names(self):
        """local.toml names a beta that is not installed, and the source has it: the gate falls
        back to the pin, the session start says nothing of it, and `bin/upgrade` stays on the
        release. None of them downloads it."""
        self.source(__version__, BETA)
        self._engines(self.w, (__version__,))
        wtext(self.root / layout.LOCAL, f'[governance]\nengine = "{BETA}"\n')
        env = {k: v for k, v in self.env.items() if k != notice.NO_UPDATE_CHECK}
        gate = subprocess.run([sys.executable, str(self.entry), "check"], env=env,
                              capture_output=True, text=True)
        self.assertEqual(gate.returncode, 0, gate.stdout + gate.stderr)
        self.assertIn(f"context-gate: beta {BETA} is not installed, running the committed pin "
                      f"{__version__}", gate.stderr)
        self.assertNotIn("installing", gate.stderr)
        self.assertTrue(self._which(self.entry, env).endswith(__version__))
        built = self.tmp / "built"                      # the plugin as a release assembles it
        shutil.copytree(REPO / "plugin", built)
        shutil.copytree(ENGINE / "govern", built / "govern",
                        ignore=shutil.ignore_patterns("__pycache__"))
        hook = subprocess.run([sys.executable, str(built / "hooks" / "session_start.py")],
                              input="{}", env={**env, "CLAUDE_PROJECT_DIR": str(self.root)},
                              capture_output=True, text=True)
        self.assertEqual((hook.returncode, hook.stderr), (0, ""))
        self.assertNotIn(BETA, hook.stdout)
        upgrade = subprocess.run([sys.executable, str(self.root / layout.GOV_DIR / "bin" /
                                                      "upgrade")], env=env, capture_output=True,
                                 text=True)
        self.assertEqual(upgrade.stdout.strip(), f"already on context-gate {__version__}")
        self.assertEqual(self.engines_installed(), [__version__])
        self.assertFalse(self.skills.exists())

    def test_a_project_with_no_local_toml_never_fetches_a_beta(self):
        self.source(__version__, BETA)
        gate = subprocess.run([sys.executable, str(self.entry), "check"], env=self.env,
                              capture_output=True, text=True)
        self.assertEqual(gate.returncode, 0, gate.stdout + gate.stderr)
        self.assertNotIn("beta", gate.stderr)
        self.assertEqual(self.run_beta().stdout, "no beta in this project\n")
        self.assertEqual(self.engines_installed(), [__version__])      # the pin, bootstrapped
        self.assertFalse(self.skills.exists())

    def test_a_project_git_cannot_ignore_the_files_in_fetches_nothing(self):
        """Whether git can be told to ignore local.toml is asked with the other checks, before
        the fetch: a project with `.claude/` but no repository is refused with the machine as
        it was, not after the engine and the local plugin have been replaced."""
        self.source(BETA)
        self.marker.parent.mkdir(parents=True)
        wtext(self.marker, f"v{self.TWO}\n")              # the plugin another project runs
        profile_rmtree(self.root / ".git")
        before = self.state(), tree_of(self.skills)
        res = self.run_beta("on", BETA)
        self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
        self.assertIn("git cannot say whether", res.stderr)
        self.assert_nothing_fetched(res)
        self.assertEqual((self.state(), tree_of(self.skills)), before)
        self.assertEqual(self.engines_installed(), [])
        self.assertFalse((self.root / layout.LOCAL).exists())

    # The engine install: whole or not at all, whoever else is installing the same one.
    def gate(self, *args: str, env=None, script: str = "govern") -> subprocess.CompletedProcess:
        """The gate (or `bin/upgrade`), run from the project as a user runs it."""
        return subprocess.run([sys.executable, str(self.entry.with_name(script)), *args],
                              env=env or self.env, cwd=self.root, capture_output=True, text=True)

    def patched(self, patch: str, *args: str) -> subprocess.CompletedProcess:
        """The gate run with `patch` applied first: Python replacing a function the entry point
        calls, to stand for what another process or a dead link does part-way through a run."""
        probe = ("import os, runpy, shutil, subprocess, sys\nfrom pathlib import Path\n" + patch
                 + f"sys.argv = [{str(self.entry)!r}, *{list(args)!r}]\n"
                 f"runpy.run_path({str(self.entry)!r}, run_name='__main__')\n")
        return subprocess.run([sys.executable, "-c", probe], env=self.env, cwd=self.root,
                              capture_output=True, text=True)

    def theirs(self, version: str) -> str:
        """Python defining `theirs()`: another process's install of that engine landing, with
        one file only it has."""
        dest = self.engines / version
        return ("def theirs():\n"
                f"    shutil.copytree({str(ENGINE / 'govern')!r}, {str(dest / 'govern')!r},\n"
                "                    ignore=shutil.ignore_patterns('__pycache__'))\n"
                f"    Path({str(dest / 'theirs')!r}).write_text('', encoding='utf-8')\n")

    def stage(self, name: str, age: float = 0) -> Path:
        """What an install killed before its move left in the engines directory: a whole,
        read-only copy under a dot name, last written `age` seconds ago."""
        stage = self.engines / name
        shutil.copytree(ENGINE / "govern", stage / "govern",
                        ignore=shutil.ignore_patterns("__pycache__"))
        os.utime(stage, (time.time() - age,) * 2)
        for path in sorted(stage.rglob("*"), reverse=True) + [stage]:
            path.chmod(path.stat().st_mode & ~0o222)
        return stage

    def test_an_engine_installed_while_this_run_was_fetching_is_kept(self):
        self.source(__version__)
        res = self.patched(self.theirs(__version__)
                           + "real = subprocess.Popen.communicate\n"
                           "def communicate(self, *args, **kw):\n"
                           "    res = real(self, *args, **kw)\n"
                           "    if list(self.args[:2]) == ['git', 'clone']:\n"
                           "        theirs()\n"
                           "    return res\n"
                           "subprocess.Popen.communicate = communicate\n", "check")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertTrue((self.engines / __version__ / "theirs").is_file())
        self.assertEqual(self.engines_installed(), [__version__])       # and no stage left

    def test_an_engine_that_lands_at_the_moment_of_the_move_is_kept(self):
        self.source(__version__)
        dest = self.engines / __version__
        res = self.patched(self.theirs(__version__)
                           + "real = os.rename\n"
                           "def rename(src, dst, *args, **kw):\n"
                           f"    if Path(dst) == Path({str(dest)!r}) and not Path(dst).exists():\n"
                           "        theirs()\n"
                           "    return real(src, dst, *args, **kw)\n"
                           "os.rename = rename\n", "check")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertTrue((dest / "theirs").is_file())
        self.assertEqual(self.engines_installed(), [__version__])

    def test_two_runs_at_once_both_succeed_and_leave_one_engine(self):
        """Two gates started together with the pinned engine missing, as two sessions or two
        hooks do: both run, and one whole read-only engine is left with nothing beside it."""
        self.source(__version__)
        dest = self.engines / __version__
        for attempt in range(20):
            profile_rmtree(self.engines)
            runs = [subprocess.Popen([sys.executable, str(self.entry), "check"], env=self.env,
                                     cwd=self.root, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True) for _ in range(2)]
            said = [run.communicate(timeout=120) for run in runs]
            self.assertEqual([run.returncode for run in runs], [0, 0], (attempt, said))
            self.assertEqual(self.engines_installed(), [__version__], attempt)
            self.assertIn(f'__version__ = "{__version__}"',
                          (dest / "govern" / "__init__.py").read_text(encoding="utf-8"))
            self.assertTrue((dest / "govern" / "cli.py").is_file())
            # Bytecode aside: run as root (the Linux CI container), Python writes its cache
            # into the read-only tree, and those files are its own, not the engine's.
            self.assertEqual([p for p in dest.rglob("*")
                              if p.is_file() and p.stat().st_mode & 0o222
                              and "__pycache__" not in p.parts], [], attempt)

    def test_a_stage_a_killed_run_left_is_cleared_and_a_live_one_is_not(self):
        self.source(__version__)
        killed = self.stage(f".{__version__}.killed00", age=3600)
        live = self.stage(f".{__version__}.sibling0")
        other = self.stage(f".{BETA}.killed00", age=3600)   # another version's: not this run's
        res = self.gate("check")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertFalse(killed.exists())
        self.assertEqual(self.engines_installed(), sorted([other.name, live.name, __version__]))
        self.assertEqual((self.engines / __version__ / "govern" / "cli.py").stat().st_mode
                         & 0o222, 0)

    def test_a_stage_is_never_taken_for_an_engine(self):
        """A stage holds a whole engine under a dot name. Nothing that lists the engines
        directory takes it for one: not a series pin, the notice, `bin/upgrade` or `beta`."""
        self._engines(self.w, (__version__,))
        names = (f".{NEXT_RELEASE}.killed00", f".{NEXT_RELEASE}.12345678", f".{SERIES}.98",
                 f".{BETA}.killed00", f".{BETA}")
        for name in names:
            self.stage(name)
        cfg = self.root / CFG
        exact = cfg.read_text(encoding="utf-8")
        for pin in (__version__, SERIES):
            with self.subTest(pin=pin):
                wtext(cfg, exact.replace(f'engine = "{__version__}"', f'engine = "{pin}"'))
                self.assertTrue(self._which(self.entry, self.env).endswith(__version__))
                res = self.gate("check")
                self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
                self.assertNotIn("available", res.stderr)
        wtext(cfg, exact)
        self.assertEqual(notice.installed_versions(self.home), [__version__])
        self.assertEqual(notice.installed_betas(self.home), [])
        res = self.gate(script="upgrade")
        self.assertEqual(res.stdout.strip(), f"already on context-gate {__version__}", res.stderr)
        res = self.gate("beta", "on")
        self.assertEqual(res.returncode, 2, res.stdout)
        self.assertIn("no beta engine is installed", res.stderr)
        self.assertEqual(self.engines_installed(), sorted([*names, __version__]))

    def test_a_directory_with_no_engine_in_it_is_still_replaced(self):
        self.source(__version__)
        dest = self.engines / __version__
        init = dest / "govern" / "__init__.py"
        init.parent.mkdir(parents=True)
        wtext(init, "# what a copy that stopped part-way left\n")
        for path in (init, init.parent, dest):
            path.chmod(path.stat().st_mode & ~0o222)
        res = self.gate("check")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn(f'__version__ = "{__version__}"', init.read_text(encoding="utf-8"))
        self.assertEqual((dest / "govern" / "cli.py").stat().st_mode & 0o222, 0)
        self.assertEqual(self.engines_installed(), [__version__])

    def test_an_engine_that_lands_while_a_broken_directory_is_set_aside_is_kept(self):
        """A directory with no engine in it is moved aside, never deleted in place: when
        another run has replaced it with a whole engine by the time it moves, that engine is
        moved back."""
        self.source(__version__)
        dest = self.engines / __version__
        (dest / "govern").mkdir(parents=True)
        wtext(dest / "govern" / "__init__.py", "# what a copy that stopped part-way left\n")
        res = self.patched(self.theirs(__version__)
                           + "real = os.rename\n"
                           "def rename(src, dst, *args, **kw):\n"
                           f"    if Path(src) == Path({str(dest)!r}) \\\n"
                           "            and not Path(src, 'theirs').exists():\n"
                           "        shutil.rmtree(src)\n"
                           "        theirs()\n"
                           "    return real(src, dst, *args, **kw)\n"
                           "os.rename = rename\n", "check")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertTrue((dest / "theirs").is_file())
        self.assertEqual(self.engines_installed(), [__version__])

    def slow_git(self, body: str = "") -> Path:
        """A directory to put first on PATH, holding a `git` that stands in for the real one on
        `clone` alone: it writes what it was run with to `clone.log`, runs `body`, and (with no
        `exit` in it) starts a child that sleeps for minutes, then sleeps itself."""
        if os.name == "nt":
            self.skipTest("the stand-in git is a shell script")
        real = shutil.which("git")
        shim = self.tmp / "shim"
        shim.mkdir()
        log = self.tmp / "clone.log"
        wtext(shim / "git", "#!/bin/sh\n"
              'if [ "$1" != clone ]; then exec ' + real + ' "$@"; fi\n'
              f'echo "prompt=${{GIT_TERMINAL_PROMPT-unset}}" > {log}\n'
              + body + f"sleep 300 &\necho $! >> {log}\nsleep 300\n")
        (shim / "git").chmod(0o755)
        return shim

    def with_shim(self, shim: Path, **extra: str) -> dict:
        return {**self.env, "PATH": f"{shim}{os.pathsep}{os.environ['PATH']}", **extra}

    def reap(self, log: Path) -> None:
        """Kill the sleeping child the stand-in git started, if the run did not."""
        try:
            pid = int(log.read_text(encoding="utf-8").splitlines()[1])
            os.kill(pid, 9)
        except (OSError, ValueError, IndexError):
            pass

    def test_a_clone_that_does_not_finish_is_one_line_not_a_hang(self):
        """The clone has a time limit, at a session start too, where a pinned release is
        fetched: a dead link ends as one line, and nothing git started is left running."""
        src = self.source(__version__, BETA)
        env = self.with_shim(self.slow_git(), CONTEXT_GATE_FETCH_TIMEOUT="2")
        self.addCleanup(self.reap, self.tmp / "clone.log")
        before = self.state()
        for args, version in ((("check",), __version__), (("beta", "on", BETA), BETA)):
            with self.subTest(args=args):
                began = time.monotonic()
                res = self.gate(*args, env=env)
                self.assertLess(time.monotonic() - began, 30)
                self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
                self.assertEqual(res.stderr.splitlines(),
                                 [f"context-gate: installing engine {version} from "
                                  f"{src.as_posix()}",
                                  f"error: could not fetch engine v{version} from "
                                  f"{src.as_posix()}: git clone did not finish in 2 seconds; "
                                  "set CONTEXT_GATE_FETCH_TIMEOUT to allow longer"])
                self.assertEqual((self.state(), self.engines_installed(), tree_of(self.skills)),
                                 (before, [], {}))
                pid = int((self.tmp / "clone.log").read_text(encoding="utf-8").splitlines()[1])
                for _ in range(50):         # the child is killed with git, not left to finish
                    try:
                        os.kill(pid, 0)
                    except OSError:
                        break
                    time.sleep(0.1)
                else:
                    self.fail("the child git started is still running")

    def clone_limit(self, **env: str) -> int:
        """What the entry point makes of the environment: its limit, in seconds."""
        from unittest import mock
        text_ = self.entry.read_text(encoding="utf-8").split("\nif Path(__file__).name == \"upgrade\":")[0]
        scope = {"__file__": str(self.entry), "__name__": "entry"}
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}):
            exec(compile(text_, str(self.entry), "exec"), scope)
        with mock.patch.dict(os.environ, env):
            return scope["_clone_timeout"]()

    def test_the_clone_limit_is_read_from_the_environment_or_is_120(self):
        self.assertEqual(self.clone_limit(CONTEXT_GATE_FETCH_TIMEOUT="45"), 45)
        for bad in ("", "abc", "0", "-5", "1.5", "2s", "\u0663"):
            with self.subTest(value=bad):
                self.assertEqual(self.clone_limit(CONTEXT_GATE_FETCH_TIMEOUT=bad), 120)
        self.assertEqual(self.clone_limit(), 120)

    def test_git_is_told_not_to_prompt_only_where_there_is_no_terminal(self):
        self.source(__version__)
        shim = self.slow_git("exit 1\n")
        env = self.with_shim(shim)
        log = self.tmp / "clone.log"
        # Run from a test there is no terminal on stdin: git must not wait for a credential.
        res = subprocess.run([sys.executable, str(self.entry), "check"], env=env, cwd=self.root,
                             stdin=subprocess.DEVNULL, capture_output=True, text=True)
        self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
        self.assertEqual(log.read_text(encoding="utf-8").splitlines(), ["prompt=0"])
        # A terminal keeps the prompt git has by default: stdin is a pseudo-terminal here.
        try:
            import pty
        except ImportError:
            return
        leader, follower = pty.openpty()
        try:
            res = subprocess.run([sys.executable, str(self.entry), "check"], env=env,
                                 cwd=self.root, stdin=follower, capture_output=True, text=True)
        finally:
            os.close(leader)
            os.close(follower)
        self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
        self.assertEqual(log.read_text(encoding="utf-8").splitlines(), ["prompt=unset"])

    def test_the_installed_engine_is_readable_by_others_and_read_only(self):
        """The engine directory is `r-xr-xr-x`, as it was: an engine fetched by one user is run
        by another."""
        if os.name == "nt":
            self.skipTest("Windows has no mode bits to read")
        self.source(__version__)
        res = self.gate("check")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        dest = self.engines / __version__
        self.assertEqual(dest.stat().st_mode & 0o777, 0o555)
        for path in (dest / "govern", dest / "govern" / "cli.py"):
            mode = path.stat().st_mode & 0o777
            self.assertEqual((mode & 0o444, mode & 0o222), (0o444, 0), path)

    def test_a_broken_directory_a_sibling_deleted_first_is_no_error(self):
        """The set-aside directory is made fresh, so a sibling does not take it for a stage a
        killed run left, and one a sibling deleted anyway is not an error."""
        self.source(__version__)
        dest = self.engines / __version__
        (dest / "govern").mkdir(parents=True)
        wtext(dest / "govern" / "__init__.py", "# what a copy that stopped part-way left\n")
        os.utime(dest, (time.time() - 3600,) * 2)       # the leftover is old, and a rename keeps it
        seen = self.tmp / "seen"
        res = self.patched("real = shutil.rmtree\n"
                           "def rmtree(path, *args, **kw):\n"
                           "    if str(path).endswith('.broken'):\n"
                           "        import time\n"
                           f"        Path({str(seen)!r}).write_text(\n"
                           "            str(time.time() - os.lstat(path).st_mtime))\n"
                           "        real(path, *args, **kw)\n"
                           "        raise FileNotFoundError(path)\n"
                           "    return real(path, *args, **kw)\n"
                           "shutil.rmtree = rmtree\n", "check")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertLess(float(seen.read_text(encoding="utf-8")), 60)
        self.assertEqual(self.engines_installed(), [__version__])
        self.assertTrue((dest / "govern" / "cli.py").is_file())

    def test_a_rename_refused_for_a_moment_is_retried(self):
        self.source(__version__)
        dest = self.engines / __version__
        res = self.patched("real = os.rename\n"
                           "refused = []\n"
                           "def rename(src, dst, *args, **kw):\n"
                           f"    if Path(dst) == Path({str(dest)!r}) and len(refused) < 2:\n"
                           "        refused.append(src)\n"
                           "        raise PermissionError(13, 'Permission denied', str(dst))\n"
                           "    return real(src, dst, *args, **kw)\n"
                           "os.rename = rename\n", "check")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertTrue((dest / "govern" / "cli.py").is_file())
        self.assertEqual(self.engines_installed(), [__version__])

    # The plugin a beta brings can only replace this plugin.
    def test_a_tag_whose_plugin_has_another_name_replaces_nothing(self):
        self.source(BETA, plugin_name="another")
        (self.skills / "another").mkdir(parents=True)
        wtext(self.skills / "another" / "SKILL.md", "# someone else's\n")
        before = self.state(), tree_of(self.skills)
        res = self.run_beta("on", BETA)
        self.assertEqual(res.returncode, 2, res.stdout)
        self.assertNotIn("Traceback", res.stderr)
        said = res.stderr.splitlines()
        self.assertEqual(len(said), 3, res.stderr)
        self.assertTrue(said[2].startswith(
            f"error: could not install the local plugin {BETA} from "), said[2])
        self.assertIn("another", said[2])
        self.assertEqual((self.state(), tree_of(self.skills)), before)
        self.assertFalse((self.root / layout.LOCAL).exists())

    # A source is a place to fetch from, never an option to git.
    def as_an_option(self) -> Path:
        """Name a source git would read as an option that runs a command, and return the file
        that command would make. The project gets an `origin`, a repository in the temp
        directory: it is what `ls-remote` asks when it is handed no repository."""
        made = self.tmp / "made-by-the-source"
        origin = self.tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", str(self.root), "remote", "add", "origin",
                        origin.as_posix()], check=True, capture_output=True)
        self.name_source(f"--upload-pack=touch {made.as_posix()}")
        return made

    def test_a_source_starting_with_a_dash_never_reaches_git(self):
        made = self.as_an_option()
        refusal = ('[governance] source \'--upload-pack=touch ' + made.as_posix()
                   + '\' starts with "-"')
        before = self.state()
        for script, args in (("govern", ("check",)),              # the pinned engine: a clone
                             ("govern", ("beta", "on")),          # the betas there: ls-remote
                             ("govern", ("beta", "on", BETA)),    # a named beta: a clone
                             ("upgrade", ())):                    # the releases there: ls-remote
            with self.subTest(script=script, args=args):
                made.unlink(missing_ok=True)
                res = self.gate(*args, script=script)
                self.assertFalse(made.exists(), res.stderr)         # its command never ran
                self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
                self.assertEqual(len(res.stderr.splitlines()), 1, res.stderr)
                self.assertTrue(res.stderr.startswith("error: "), res.stderr)
                self.assertIn(refusal, res.stderr)
                self.assertEqual((self.state(), self.engines_installed(), tree_of(self.skills)),
                                 (before, [], {}))

    def test_a_source_starting_with_a_dash_gets_no_notice_and_no_call(self):
        """The upgrade notice asks the source at a session start and after a gate run: for such
        a source it says nothing and runs nothing."""
        from unittest import mock
        made = self.as_an_option()
        source = f"--upload-pack=touch {made.as_posix()}"
        self._engines(self.w, (__version__,))
        env = {k: v for k, v in self.env.items() if k != notice.NO_UPDATE_CHECK}
        res = self.gate("check", env=env)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertNotIn("available", res.stderr)
        self.assertFalse(made.exists())
        built = self.tmp / "built"                      # the plugin as a release assembles it
        shutil.copytree(REPO / "plugin", built)
        shutil.copytree(ENGINE / "govern", built / "govern",
                        ignore=shutil.ignore_patterns("__pycache__"))
        hook = subprocess.run([sys.executable, str(built / "hooks" / "session_start.py")],
                              input="{}", env={**env, "CLAUDE_PROJECT_DIR": str(self.root)},
                              cwd=self.root, capture_output=True, text=True)
        self.assertEqual((hook.returncode, hook.stderr), (0, ""))
        self.assertFalse(made.exists())
        with mock.patch.dict(os.environ), mock.patch.object(notice.subprocess, "run") as run:
            os.environ.pop(notice.NO_UPDATE_CHECK, None)
            self.assertEqual(notice.released_versions(source, self.home, fresh=True), [])
            self.assertIsNone(notice.for_project(self.root, self.home, __version__))
        run.assert_not_called()
        self.assertFalse((self.home / ".cache" / layout.TOOL / "releases.json").exists())

    # A tag is read from the start of its name.
    def test_a_tag_with_a_slash_in_its_name_is_no_beta(self):
        """`vzz/refs/tags/vX` ends like a version tag and is not one: taken for the newest
        beta, it could not be fetched and stood in the way of the real one."""
        src = self.source(__version__, BETA)
        self.source_git(src, "tag", f"vzz/refs/tags/v{NEXT_RELEASE}-beta.4")
        res = self.run_beta("on")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.splitlines()[0],
                         f"beta {BETA} on for this project, on this machine:")
        self.assert_installed(BETA)

    def test_a_tag_with_a_slash_in_its_name_is_no_release(self):
        from unittest import mock
        src = self.source(__version__)
        self.source_git(src, "tag", f"vzz/refs/tags/v{NEXT_RELEASE}")
        res = self.gate(script="upgrade")
        self.assertEqual(res.stdout.strip(), f"already on context-gate {__version__}", res.stderr)
        self.assertEqual(self.engines_installed(), [])
        with mock.patch.dict(os.environ):
            os.environ.pop(notice.NO_UPDATE_CHECK, None)
            self.assertEqual(notice.released_versions(src.as_posix(), self.home, True),
                             [__version__])


if __name__ == "__main__":
    unittest.main()
