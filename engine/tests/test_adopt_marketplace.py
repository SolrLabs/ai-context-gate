"""The plugin's marketplace, the public plugin id and the release source that adopt writes, and
the engine it installs.

Adopt opts a project in to `context-gate@context-gate` and registers its marketplace in
`extraKnownMarketplaces`, both recorded in `installed.toml`. Rollback and uninstall bring the
settings file back byte for byte (or remove it, if there was none) while it is still exactly
what the tool wrote; an edit since is a conflict, and `--force` undoes only the tool's own keys.
`[governance] source` defaults to the public repository, or a `--marketplace`
fork's. Adopt installs the engine it runs when that engine is a stamped release, so a project
adopted from an empty HOME runs its gate with no fetch; a development tree is never installed.

    python3 -m unittest discover -s engine/tests -k marketplace
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parent.parent
REPO = ENGINE.parent
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from govern import __version__, installer, layout, profile  # noqa: E402
from test_measure import BODY, FM, commit, put  # noqa: E402

SETTINGS = ".claude/settings.json"
PUBLIC = {"source": {"source": "github", "repo": layout.PUBLIC_REPO}}
FORK = {"source": {"source": "github", "repo": "fork-owner/fork-repo"}}
CONFIG = f'[governance]\nengine = "{__version__}"\nschema = 1\n'


def run(*args: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = installer.main(list(args))
    return code, out.getvalue(), err.getvalue()


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(profile.rmtree, self.tmp)
        self.root = self.tmp / "root"
        self.home = self.tmp / "home"
        put(self.root / "README.md", "# Small\n")
        put(self.root / "docs/DECISIONS.md", FM + "# Decisions\n\n## D-1 — One\n" + BODY)
        env = {"HOME": str(self.home), "CONTEXT_GATE_NO_UPDATE_CHECK": "1"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def settings(self, text: str | bytes | None = None) -> dict | None:
        """Write the project's settings when given; return them as JSON (None if absent)."""
        path = self.root / SETTINGS
        if text is not None:
            put(path, text)
        return json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else None

    def raw(self) -> bytes | None:
        path = self.root / SETTINGS
        return path.read_bytes() if path.exists() else None

    def manifest(self) -> dict:
        with (self.root / layout.MANIFEST).open("rb") as fh:
            return tomllib.load(fh)

    def install(self, *extra: str, config: str = CONFIG) -> tuple[int, str, str]:
        cfg = self.tmp / "config.toml"
        put(cfg, config)
        return run("install", "--root", str(self.root), "--config", str(cfg), "--no-report",
                   *extra)

    def uninstall(self, *extra: str) -> None:
        code, out, err = run("uninstall", "--root", str(self.root), *extra)
        self.assertEqual(code, 0, out + err)

    def stamped(self, tag: str = f"v{__version__}") -> Path:
        """A copy of this engine's `govern` package, stamped as the release tools stamp one."""
        dest = self.tmp / "engine-copy" / "govern"
        profile.rmtree(dest.parent)
        shutil.copytree(ENGINE / "govern", dest, ignore=shutil.ignore_patterns("__pycache__"))
        put(dest / layout.RELEASE_MARKER, f"{tag}\n")
        return dest


# ---------------------------------------------------------------------------- the record

class Record(Base):
    def test_absent_is_written_recorded_and_removed(self):
        before = self.settings('{\n  "hooks": {}\n}\n')
        code, out, err = self.install("--enable-plugin", layout.PLUGIN_ID)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.settings(), {"hooks": {},
                                           "extraKnownMarketplaces": {layout.MARKETPLACE: PUBLIC},
                                           "enabledPlugins": {layout.PLUGIN_ID: True}})
        self.assertEqual(self.manifest()["marketplace"],
                         [{"settings": SETTINGS, "name": layout.MARKETPLACE,
                           "repo": layout.PUBLIC_REPO, "previous": "absent"}])
        self.uninstall()
        self.assertEqual(self.settings(), before)

    def test_a_different_previous_entry_is_restored(self):
        before = self.settings(json.dumps({"extraKnownMarketplaces": {
            layout.MARKETPLACE: FORK, "other": {"source": {"source": "github", "repo": "o/r"}}}}))
        self.assertEqual(self.install("--enable-plugin", layout.PLUGIN_ID)[0], 0)
        self.assertEqual(self.settings()["extraKnownMarketplaces"][layout.MARKETPLACE], PUBLIC)
        self.assertEqual(json.loads(self.manifest()["marketplace"][0]["previous"]), FORK)
        self.uninstall()
        self.assertEqual(self.settings(), before)

    def test_the_same_entry_is_recorded_as_same_and_left(self):
        before = self.settings(json.dumps({"extraKnownMarketplaces": {layout.MARKETPLACE: PUBLIC}}))
        self.assertEqual(self.install("--enable-plugin", layout.PLUGIN_ID)[0], 0)
        self.assertEqual(self.manifest()["marketplace"][0]["previous"], installer.SAME)
        self.uninstall()
        self.assertEqual(self.settings(), before)

    def test_a_failed_install_rolls_the_marketplace_back(self):
        text = '{\r\n    "extraKnownMarketplaces": {"context-gate": {}},\r\n    "x": {}\r\n}\r\n'
        self.settings(text)
        code, _, err = self.install("--enable-plugin", layout.PLUGIN_ID,
                                    config=CONFIG + "unknown_key = 1\n")
        self.assertEqual(code, 2)
        self.assertIn("install rolled back", err)
        self.assertEqual(self.raw(), text.encode())
        self.assertFalse((self.root / layout.GOV_DIR).exists())

    def test_a_failed_install_removes_a_settings_file_it_created(self):
        code, _, _ = self.install("--enable-plugin", layout.PLUGIN_ID,
                                  config=CONFIG + "unknown_key = 1\n")
        self.assertEqual(code, 2)
        self.assertFalse((self.root / ".claude").exists())

    def test_skills_dir_id_writes_no_marketplace(self):
        self.assertEqual(self.install("--enable-plugin", layout.SKILLS_DIR_PLUGIN_ID)[0], 0)
        self.assertEqual(self.settings(), {"enabledPlugins": {layout.SKILLS_DIR_PLUGIN_ID: True}})
        self.assertNotIn("marketplace", self.manifest())


class ExactRestore(Base):
    """Uninstall brings back the settings file's own bytes: its indent, line endings, escapes,
    BOM and empty objects, or its absence."""

    def round_trip(self, original: bytes) -> None:
        self.settings(original)
        code, out, err = self.install("--enable-plugin", layout.PLUGIN_ID)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.settings()["enabledPlugins"], {layout.PLUGIN_ID: True})
        self.uninstall()
        self.assertEqual(self.raw(), original)

    def test_crlf(self):
        self.round_trip(b'{\r\n  "hooks": {},\r\n  "enabledPlugins": {}\r\n}\r\n')

    def test_crlf_survives_the_write_too(self):
        self.settings(b'{\r\n  "hooks": {}\r\n}\r\n')
        self.install("--enable-plugin", layout.PLUGIN_ID)
        raw = self.raw()
        self.assertEqual(raw.count(b"\r\n"), raw.count(b"\n"))

    def test_four_spaces_escapes_and_empty_objects(self):
        self.round_trip(b'{\n    "name": "caf\\u00e9",\n    "extraKnownMarketplaces": {},\n'
                        b'    "enabledPlugins": {}\n}\n')

    def test_bom(self):
        self.round_trip(b'\xef\xbb\xbf{"hooks": {}}\n')

    def test_absent_file_and_directory(self):
        self.assertEqual(self.install("--enable-plugin", layout.PLUGIN_ID)[0], 0)
        self.assertTrue((self.root / SETTINGS).is_file())
        self.uninstall()
        self.assertFalse((self.root / ".claude").exists())

    def test_absent_file_in_an_existing_directory(self):
        put(self.root / ".claude" / "agents" / "a.md", "x\n")
        self.assertEqual(self.install("--enable-plugin", layout.PLUGIN_ID)[0], 0)
        self.uninstall()
        self.assertFalse((self.root / SETTINGS).exists())
        self.assertTrue((self.root / ".claude" / "agents" / "a.md").is_file())

    def test_an_edit_since_install_is_a_conflict_and_force_keeps_it(self):
        self.settings('{\n    "hooks": {}\n}\n')
        self.assertEqual(self.install("--enable-plugin", layout.PLUGIN_ID)[0], 0)
        data = self.settings()
        data["permissions"] = {"allow": ["Bash(ls:*)"]}
        self.settings(json.dumps(data))
        edited = self.raw()
        code, _, err = run("uninstall", "--root", str(self.root))
        self.assertEqual(code, 1)
        self.assertIn(f"{SETTINGS} was edited after install", err)
        self.assertEqual(self.raw(), edited)                     # nothing changed
        self.uninstall("--force")
        self.assertEqual(self.settings(), {"hooks": {}, "permissions": {"allow": ["Bash(ls:*)"]}})

    def test_an_edit_to_a_file_install_created_keeps_the_file_under_force(self):
        self.assertEqual(self.install("--enable-plugin", layout.PLUGIN_ID)[0], 0)
        data = self.settings()
        data["model"] = "x"
        self.settings(json.dumps(data))
        self.uninstall("--force")
        self.assertEqual(self.settings(), {"model": "x"})

    def test_a_removed_file_is_a_conflict_and_force_restores_it(self):
        original = b'{"hooks": {}}'
        self.settings(original)
        self.assertEqual(self.install("--enable-plugin", layout.PLUGIN_ID)[0], 0)
        (self.root / SETTINGS).unlink()
        code, _, err = run("uninstall", "--root", str(self.root))
        self.assertEqual(code, 1)
        self.assertIn("was removed after install", err)
        self.uninstall("--force")
        self.assertEqual(self.raw(), original)


class EnablePlugin(Base):
    def setUp(self) -> None:
        super().setUp()
        self.assertEqual(self.install()[0], 0)

    def enable(self, *extra: str) -> tuple[int, str, str]:
        return run("enable-plugin", "--root", str(self.root), *extra)

    def test_fork_marketplace_is_written_and_reversed(self):
        code, out, err = self.enable("--id", layout.PLUGIN_ID,
                                     "--marketplace", "fork-owner/fork-repo")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.settings(), {"extraKnownMarketplaces": {layout.MARKETPLACE: FORK},
                                           "enabledPlugins": {layout.PLUGIN_ID: True}})
        before = self.raw()
        self.assertEqual(self.enable("--id", layout.PLUGIN_ID)[0], 0)   # a second run: no change
        self.assertEqual(self.raw(), before)
        self.assertEqual(len(self.manifest()["marketplace"]), 1)
        self.uninstall()
        self.assertFalse((self.root / ".claude").exists())

    def test_a_different_fork_is_refused(self):
        self.assertEqual(self.enable("--id", layout.PLUGIN_ID,
                                     "--marketplace", "fork-owner/fork-repo")[0], 0)
        before = self.raw()
        code, _, err = self.enable("--id", layout.PLUGIN_ID, "--marketplace", "other/repo")
        self.assertEqual(code, 2)
        self.assertIn("already recorded as fork-owner/fork-repo; uninstall or edit it", err)
        self.assertEqual(self.raw(), before)

    def test_public_id_defaults_to_the_public_marketplace(self):
        self.assertEqual(self.enable("--id", layout.PLUGIN_ID)[0], 0)
        self.assertEqual(self.settings()["extraKnownMarketplaces"], {layout.MARKETPLACE: PUBLIC})

    def test_a_second_id_keeps_the_byte_restore(self):
        original = b'{\r\n    "hooks": {}\r\n}\r\n'
        self.settings(original)
        self.assertEqual(self.enable("--id", layout.SKILLS_DIR_PLUGIN_ID)[0], 0)
        self.assertEqual(self.enable("--id", layout.PLUGIN_ID)[0], 0)
        self.uninstall()
        self.assertEqual(self.raw(), original)

    def test_skills_dir_still_works_and_writes_no_marketplace(self):
        self.assertEqual(self.enable("--id", layout.SKILLS_DIR_PLUGIN_ID)[0], 0)
        self.assertEqual(self.settings(), {"enabledPlugins": {layout.SKILLS_DIR_PLUGIN_ID: True}})

    def test_a_bad_marketplace_changes_nothing(self):
        for extra in (["--id", layout.SKILLS_DIR_PLUGIN_ID, "--marketplace", "o/r"],
                      ["--id", layout.PLUGIN_ID, "--marketplace", "not a repo"],
                      ["--id", "no-marketplace"]):
            with self.subTest(extra=extra):
                code, _, err = self.enable(*extra)
                self.assertEqual(code, 2)
                self.assertIn("nothing was changed", err)
                self.assertIsNone(self.settings())

    def test_unreadable_settings_change_nothing(self):
        put(self.root / SETTINGS, "not json")
        code, _, err = self.enable("--id", layout.PLUGIN_ID)
        self.assertEqual(code, 2)
        self.assertIn("nothing was changed", err)
        self.assertEqual(self.raw(), b"not json")
        self.assertFalse((self.root / layout.BACKUP).exists())
        self.assertNotIn("settings", self.manifest())


class Repo(unittest.TestCase):
    def test_owner_repo_shapes(self):
        for good in ("o/r", "Org.Name/repo-name_2", "o/.github"):
            with self.subTest(repo=good):
                self.assertEqual(installer.marketplace_for(layout.PLUGIN_ID, good)[1], good)
        for bad in ("./x", "../x", "o/.", "o/..", "o", "o/r/x", "/r", "o/"):
            with self.subTest(repo=bad):
                with self.assertRaises(ValueError):
                    installer.marketplace_for(layout.PLUGIN_ID, bad)


# ---------------------------------------------------------------------------- adopt

class Adopt(Base):
    ANSWERS = 'shape = "single"\n'

    def setUp(self) -> None:
        super().setUp()
        commit(self.root)
        self.answers = self.tmp / "answers.toml"
        put(self.answers, self.ANSWERS)

    def adopt(self, *extra: str) -> tuple[int, str, str]:
        return run("adopt", "--root", str(self.root), "--answers", str(self.answers), *extra)

    def proposed_source(self, *extra: str) -> str | None:
        code, out, err = self.adopt("--json", *extra)
        self.assertEqual(code, 0, out + err)
        return json.loads(out)["config"]["governance"].get("source")

    def test_source_defaults(self):
        self.assertEqual(self.proposed_source(), layout.PUBLIC_SOURCE)
        self.assertEqual(self.proposed_source("--marketplace", "fork-owner/fork-repo"),
                         "https://github.com/fork-owner/fork-repo.git")
        self.assertEqual(self.proposed_source("--source", "git@host:x.git",
                                              "--marketplace", "fork-owner/fork-repo"),
                         "git@host:x.git")
        self.assertIsNone(self.proposed_source("--source", installer.SOURCE_NONE))

    def test_a_bad_marketplace_is_refused(self):
        code, _, err = self.adopt("--apply", "--marketplace", "nope")
        self.assertEqual(code, 2)
        self.assertIn("--marketplace", err)
        self.assertFalse((self.root / layout.GOV_DIR).exists())

    def test_apply_writes_the_public_marketplace_and_id(self):
        code, out, err = self.adopt("--apply")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.settings(), {"extraKnownMarketplaces": {layout.MARKETPLACE: PUBLIC},
                                           "enabledPlugins": {layout.PLUGIN_ID: True}})
        cfg = tomllib.loads((self.root / layout.CONFIG).read_text(encoding="utf-8"))
        self.assertEqual(cfg["governance"]["source"], layout.PUBLIC_SOURCE)
        self.uninstall()
        self.assertFalse((self.root / ".claude").exists())

    def test_apply_with_a_fork(self):
        code, out, err = self.adopt("--apply", "--marketplace", "fork-owner/fork-repo")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.settings()["extraKnownMarketplaces"], {layout.MARKETPLACE: FORK})
        cfg = tomllib.loads((self.root / layout.CONFIG).read_text(encoding="utf-8"))
        self.assertEqual(cfg["governance"]["source"], "https://github.com/fork-owner/fork-repo.git")

    def test_a_development_tree_is_not_installed(self):
        """This working tree carries no release stamp: adopt still succeeds, and says so."""
        self.assertFalse((ENGINE / "govern" / layout.RELEASE_MARKER).exists())
        code, out, err = self.adopt("--apply")
        self.assertEqual(code, 0, out + err)
        self.assertIn(f"engine {__version__} is a development tree; not installed", out)
        self.assertFalse(layout.engines_dir().exists())

    def test_a_development_tree_says_nothing_when_its_version_is_installed(self):
        """Nothing would have been installed either way: no note, not the development one."""
        self.assertFalse((ENGINE / "govern" / layout.RELEASE_MARKER).exists())
        dest = layout.engines_dir() / __version__
        put(dest / "govern" / "cli.py", "# installed\n")
        self.assertIsNone(installer.install_running_engine())
        code, out, err = self.adopt("--apply")
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("development tree", out)
        self.assertNotIn("  engine  ", out)
        self.assertEqual((dest / "govern" / "cli.py").read_text(encoding="utf-8"),
                         "# installed\n")

    def test_a_stamp_for_another_version_is_not_installed(self):
        note = installer.install_running_engine(engine=self.stamped("v9.9.9"))
        self.assertIn("development tree; not installed", note)
        self.assertFalse(layout.engines_dir().exists())

    def test_an_installed_engine_is_never_overwritten(self):
        dest = layout.engines_dir() / __version__
        put(dest / "govern" / "cli.py", "# mine\n")
        self.assertIsNone(installer.install_running_engine(engine=self.stamped()))
        self.assertEqual((dest / "govern" / "cli.py").read_text(encoding="utf-8"), "# mine\n")

    def test_a_broken_engine_dir_is_reported_not_replaced(self):
        dest = layout.engines_dir() / __version__
        put(dest / "marker", "x\n")
        note = installer.install_running_engine(engine=self.stamped())
        self.assertIn("broken (no govern/cli.py); not replaced", note)
        self.assertEqual(sorted(p.name for p in dest.iterdir()), ["marker"])

    def test_a_failed_rename_is_retried_once(self):
        real, calls = Path.rename, []

        def flaky(path, target):
            calls.append(target)
            if len(calls) == 1:
                raise PermissionError("locked")
            return real(path, target)
        with mock.patch.object(Path, "rename", flaky):
            note = installer.install_running_engine(engine=self.stamped())
        self.assertEqual(len(calls), 2)
        self.assertIn("installed in", note)
        self.assertEqual([p.name for p in layout.engines_dir().iterdir()], [__version__])

    def test_an_engine_install_failure_warns_when_a_source_can_fetch(self):
        with mock.patch.object(installer, "install_running_engine",
                               side_effect=installer.EngineNotInstalled("locked")):
            code, out, err = self.adopt("--apply")
        self.assertEqual(code, 0, out + err)
        self.assertIn("not installed (locked); the gate fetches it", out)
        self.assertTrue((self.root / layout.GOV_DIR).exists())

    def test_an_engine_install_failure_undoes_adopt_with_no_source(self):
        with mock.patch.object(installer, "install_running_engine",
                               side_effect=installer.EngineNotInstalled("locked")):
            code, _, err = self.adopt("--apply", "--source", installer.SOURCE_NONE)
        self.assertEqual(code, 2)
        self.assertIn("adopt was undone", err)
        self.assertFalse((self.root / layout.GOV_DIR).exists())
        self.assertFalse((self.root / ".claude").exists())

    def test_clean_home_gate_runs_with_no_fetch(self):
        """From an empty HOME, adopt run from a released copy installs it, so the gate runs in a
        fresh process with a source that cannot be fetched from."""
        self.assertFalse(self.home.exists())
        copy = self.stamped()
        nowhere = (self.tmp / "no-such-remote").as_uri()
        env = {k: v for k, v in os.environ.items() if k not in ("GOVERN_ENGINE", "PYTHONPATH")}
        env["USERPROFILE"] = str(self.home)
        res = subprocess.run([sys.executable, "-P", "-m", "govern.installer", "adopt", "--root",
                              str(self.root), "--answers", str(self.answers), "--apply",
                              "--source", nowhere], cwd=self.tmp,
                             env={**env, "PYTHONPATH": str(copy.parent)},
                             capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn(f"{__version__} installed in", res.stdout)
        engine = layout.engines_dir() / __version__ / "govern"
        self.assertTrue((engine / "cli.py").is_file())
        self.assertEqual(list(engine.rglob("__pycache__")), [])
        self.assertEqual((engine / "cli.py").stat().st_mode & 0o222, 0)      # read-only
        res = subprocess.run([sys.executable, str(Path(layout.ENTRYPOINT)), "check"],
                             cwd=self.root, env=env, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertNotIn("installing engine", res.stderr)


class ReleaseStamp(unittest.TestCase):
    def test_the_plugin_assembler_stamps_the_marker_layout_names(self):
        text = (REPO / "tools" / "release" / "install-plugin.py").read_text(encoding="utf-8")
        self.assertIn(f'"govern" / "{layout.RELEASE_MARKER}"', text)


# ---------------------------------------------------------------------------- profile.rmtree

class Rmtree(unittest.TestCase):
    """Unlocking a read-only tree never reaches outside it, through a symlink or its parent."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(profile.rmtree, self.tmp)
        self.outside = self.tmp / "outside"
        self.outside.mkdir()
        (self.outside / "keep").write_text("x", encoding="utf-8")
        self.outside.chmod(0o555)
        self.addCleanup(self.outside.chmod, 0o755)
        self.mode = stat.S_IMODE(self.outside.stat().st_mode)

    def link(self, at: Path, to: Path) -> None:
        try:
            os.symlink(to, at, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available here")

    def test_a_symlink_inside_a_read_only_dir(self):
        tree = self.tmp / "tree"
        (tree / "ro").mkdir(parents=True)
        self.link(tree / "ro" / "link", self.outside)
        (tree / "ro").chmod(0o555)
        profile.rmtree(tree)
        self.assertFalse(tree.exists())
        self.assertEqual(stat.S_IMODE(self.outside.stat().st_mode), self.mode)
        self.assertTrue((self.outside / "keep").is_file())

    def test_a_symlink_as_the_tree(self):
        self.link(self.tmp / "link", self.outside)
        with contextlib.suppress(OSError):
            profile.rmtree(self.tmp / "link")
        self.assertEqual(stat.S_IMODE(self.outside.stat().st_mode), self.mode)
        self.assertEqual(stat.S_IMODE(self.tmp.stat().st_mode) & 0o700, 0o700)
        self.assertTrue((self.outside / "keep").is_file())

    def test_the_parent_of_the_tree_is_left_alone(self):
        parent = self.tmp / "parent"
        (parent / "tree" / "sub").mkdir(parents=True)
        (parent / "tree" / "sub").chmod(0o555)
        parent.chmod(0o555)
        self.addCleanup(parent.chmod, 0o755)
        with contextlib.suppress(OSError):
            profile.rmtree(parent / "tree")
        self.assertEqual(stat.S_IMODE(parent.stat().st_mode), stat.S_IMODE(0o555))


if __name__ == "__main__":
    unittest.main()
