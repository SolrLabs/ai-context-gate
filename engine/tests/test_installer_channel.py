"""`installer channel stable|beta`: the beta marketplace in the project's settings, recorded and undone."""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import tomllib
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import profile_guard  # noqa: E402,F401

from regressions_support import *  # noqa: E402,F401,F403

BETA_ID = layout.BETA_CHANNEL_PLUGIN_ID
STABLE_ID = layout.PLUGIN_ID


class Channel(Base):
    def project(self):
        w = self.ws()
        cfg = self.tmp / "config.toml"
        shutil.move(str(w.root / CFG), str(cfg))
        (w.root / layout.GOV_DIR).rmdir()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(installer.main(["install", "--root", str(w.root), "--config",
                                             str(cfg), "--no-report"]), 0)
        return w.root

    def run_cmd(self, root, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = installer.main(["channel", "--root", str(root), *args])
        return code, out.getvalue(), err.getvalue()

    def settings(self, root):
        return json.loads((root / installer.SETTINGS).read_text(encoding="utf-8"))

    def channel_key(self, root):
        with (root / layout.CONFIG).open("rb") as fh:
            return tomllib.load(fh)["governance"].get("channel")

    def test_beta_from_a_fresh_install(self):
        root = self.project()
        cfg_before = (root / layout.CONFIG).read_text(encoding="utf-8")
        code, _, _ = self.run_cmd(root, "beta")
        self.assertEqual(code, 0)
        self.assertEqual(self.channel_key(root), "beta")
        lines = (root / layout.CONFIG).read_text(encoding="utf-8").splitlines()
        i = next(n for n, ln in enumerate(lines) if ln.startswith("engine = "))
        self.assertEqual(lines[i + 1], 'channel = "beta"')
        self.assertEqual("\n".join(ln for ln in lines if not ln.startswith("channel = ")) + "\n",
                         cfg_before)
        self.assertEqual(self.settings(root), {
            "extraKnownMarketplaces": {layout.BETA_MARKETPLACE: {"source": {
                "source": "github", "repo": layout.PUBLIC_REPO, "ref": "beta"}}},
            "enabledPlugins": {BETA_ID: True, STABLE_ID: False}})
        record = installer.read_manifest(root)
        self.assertEqual({r["id"]: r["previous"] for r in record["plugin"]},
                         {BETA_ID: "absent", STABLE_ID: "absent"})
        self.assertEqual([m["previous"] for m in record["marketplace"]], ["absent"])

    def test_stable_after_beta_puts_everything_back(self):
        root = self.project()
        cfg_before = (root / layout.CONFIG).read_bytes()
        self.run_cmd(root, "beta")
        code, _, _ = self.run_cmd(root, "stable")
        self.assertEqual(code, 0)
        self.assertEqual((root / layout.CONFIG).read_bytes(), cfg_before)
        self.assertFalse((root / installer.SETTINGS).exists())
        record = installer.read_manifest(root)
        self.assertEqual((record["plugin"], record["marketplace"], record["settings"]),
                         ([], [], []))

    def test_stable_restores_a_file_the_project_already_had(self):
        root = self.project()
        path = root / installer.SETTINGS
        path.parent.mkdir(exist_ok=True)
        wtext(path, json.dumps({"permissions": {"allow": ["x"]}}, indent=2) + "\n")
        original = path.read_bytes()
        self.run_cmd(root, "beta")
        self.assertIn("permissions", self.settings(root))
        self.run_cmd(root, "stable")
        self.assertEqual(path.read_bytes(), original)

    def test_stable_puts_back_a_stable_plugin_the_user_had_enabled(self):
        root = self.project()
        path = root / installer.SETTINGS
        path.parent.mkdir(exist_ok=True)
        wtext(path, json.dumps({"enabledPlugins": {STABLE_ID: True}}, indent=2) + "\n")
        self.run_cmd(root, "beta")
        self.assertIs(self.settings(root)["enabledPlugins"][STABLE_ID], False)
        self.run_cmd(root, "stable")
        self.assertEqual(self.settings(root), {"enabledPlugins": {STABLE_ID: True}})

    def test_stable_keeps_edits_made_since(self):
        root = self.project()
        self.run_cmd(root, "beta")
        data = self.settings(root)
        data["model"] = "opus"
        wtext(root / installer.SETTINGS, json.dumps(data, indent=2) + "\n")
        self.run_cmd(root, "stable")
        self.assertEqual(self.settings(root), {"model": "opus"})

    def test_beta_twice_changes_nothing_and_says_so(self):
        root = self.project()
        self.run_cmd(root, "beta")
        snap = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
        code, out, _ = self.run_cmd(root, "beta")
        self.assertEqual(code, 0)
        self.assertIn("already on the beta channel", out)
        self.assertEqual({p: p.read_bytes() for p in root.rglob("*") if p.is_file()}, snap)

    def test_stable_when_stable_changes_nothing_and_says_so(self):
        root = self.project()
        code, out, _ = self.run_cmd(root, "stable")
        self.assertEqual(code, 0)
        self.assertIn("already on the stable channel", out)
        self.assertFalse((root / installer.SETTINGS).exists())

    def test_uninstall_after_beta_restores_the_settings(self):
        root = self.project()
        path = root / installer.SETTINGS
        path.parent.mkdir(exist_ok=True)
        wtext(path, json.dumps({"enabledPlugins": {STABLE_ID: True}}, indent=2) + "\n")
        self.run_cmd(root, "beta")
        data = self.settings(root)
        data["model"] = "opus"                      # edited since: restored key by key
        wtext(path, json.dumps(data, indent=2) + "\n")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            installer.main(["uninstall", "--root", str(root), "--force"])
        self.assertEqual(self.settings(root), {"enabledPlugins": {STABLE_ID: True},
                                               "model": "opus"})

    def test_uninstall_undoes_a_key_the_install_and_the_channel_both_recorded(self):
        root = self.project()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(installer.main(["enable-plugin", "--root", str(root), "--id",
                                             STABLE_ID]), 0)
        self.run_cmd(root, "beta")
        data = self.settings(root)
        data["model"] = "opus"
        wtext(root / installer.SETTINGS, json.dumps(data, indent=2) + "\n")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            installer.main(["uninstall", "--root", str(root), "--force"])
        self.assertEqual(self.settings(root), {"model": "opus"})

    def test_uninstall_after_beta_untouched_removes_the_file_it_made(self):
        root = self.project()
        self.run_cmd(root, "beta")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(installer.main(["uninstall", "--root", str(root)]), 0)
        self.assertFalse((root / installer.SETTINGS).exists())

    def test_bad_value_is_refused_and_changes_nothing(self):
        root = self.project()
        snap = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
        code, _, err = self.run_cmd(root, "nightly")
        self.assertEqual(code, 2)
        self.assertIn("nothing was changed", err)
        self.assertEqual({p: p.read_bytes() for p in root.rglob("*") if p.is_file()}, snap)

    def test_not_installed_is_refused(self):
        w = self.ws()
        code, _, err = self.run_cmd(w.root, "beta")
        self.assertEqual(code, 2)
        self.assertIn("install first", err)

    def test_crlf_and_bom_in_the_config_survive(self):
        root = self.project()
        cfg = root / layout.CONFIG
        raw = cfg.read_bytes().replace(b"\n", b"\r\n")
        cfg.write_bytes(b"\xef\xbb\xbf" + raw)
        self.run_cmd(root, "beta")
        after = cfg.read_bytes()
        self.assertTrue(after.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn(b"\n", after.replace(b"\r\n", b""))
        self.run_cmd(root, "stable")
        self.assertEqual(cfg.read_bytes(), b"\xef\xbb\xbf" + raw)

    def test_unreadable_config_is_reported_not_a_traceback(self):
        root = self.project()
        (root / layout.CONFIG).write_bytes(b"\xff\xfe\x00bad")
        code, _, err = self.run_cmd(root, "beta")
        self.assertEqual(code, 2)
        self.assertFalse((root / installer.SETTINGS).exists())
        self.assertIn("cannot be edited", err)

    def tree(self, root):
        return {p: (p.read_bytes() if p.is_file() else None) for p in root.rglob("*")}

    def test_a_failed_config_write_puts_everything_back(self):
        root = self.project()
        cfg = root / layout.CONFIG
        before = self.tree(root)
        cfg.chmod(0o444)                                # read-only: the last write fails
        try:
            code, _, err = self.run_cmd(root, "beta")
        finally:
            cfg.chmod(0o644)
        self.assertEqual(code, 2, err)
        self.assertEqual(self.tree(root), before)
        code, _, _ = self.run_cmd(root, "beta")         # no duplicate records afterwards
        self.assertEqual(code, 0)
        record = installer.read_manifest(root)
        self.assertEqual(len(record["plugin"]), 2)
        self.assertEqual(len(record["marketplace"]), 1)

    def _old_launchers(self, root):
        for rel in layout.TOOL_FILES:
            (root / layout.GOV_DIR / rel).write_bytes(b"old launcher\n")

    def _assert_launchers_current(self, root):
        want = (Path(installer.__file__).resolve().parent / "templates" / "entrypoint.py"
                ).read_bytes()
        for rel in layout.TOOL_FILES:
            dest = root / layout.GOV_DIR / rel
            self.assertEqual(dest.read_bytes(), want, rel)
            if sys.platform != "win32":
                self.assertTrue(dest.stat().st_mode & 0o100, rel)

    def test_beta_refreshes_an_old_launcher(self):
        root = self.project()
        self._old_launchers(root)
        code, _, err = self.run_cmd(root, "beta")
        self.assertEqual(code, 0, err)
        self._assert_launchers_current(root)

    def test_stable_refreshes_an_old_launcher(self):
        root = self.project()
        self.assertEqual(self.run_cmd(root, "beta")[0], 0)
        self._old_launchers(root)
        code, _, err = self.run_cmd(root, "stable")
        self.assertEqual(code, 0, err)
        self._assert_launchers_current(root)

    def _nothing_changed_refreshes(self, which, first=None):
        root = self.project()
        if first:
            self.assertEqual(self.run_cmd(root, first)[0], 0)
        self.assertEqual(self.run_cmd(root, which)[0], 0)
        self._old_launchers(root)
        code, out, err = self.run_cmd(root, which)
        self.assertEqual(code, 0, err)
        self.assertIn(f"already on the {which} channel; launchers refreshed", out)
        self._assert_launchers_current(root)

    def test_a_nothing_changed_run_refreshes_old_launchers(self):
        self._nothing_changed_refreshes("beta")

    def test_a_nothing_changed_stable_run_refreshes_old_launchers(self):
        self._nothing_changed_refreshes("stable", "beta")

    def test_a_failed_launcher_write_puts_everything_back(self):
        root = self.project()
        self._old_launchers(root)
        blocked = root / layout.GOV_DIR / "bin" / "govern"
        blocked.unlink()
        blocked.mkdir()                                 # a directory where the launcher goes
        before = self.tree(root)
        code, _, err = self.run_cmd(root, "beta")
        self.assertEqual(code, 2, err)
        self.assertIn("settings and manifest put back", err)
        self.assertNotIn("could not be put back", err)
        self.assertEqual(self.tree(root), before)
        self.assertFalse((root / installer.SETTINGS).exists())

    def test_a_failed_config_write_keeps_a_settings_file_the_project_had(self):
        root = self.project()
        path = root / installer.SETTINGS
        path.parent.mkdir(exist_ok=True)
        wtext(path, json.dumps({"model": "opus"}, indent=2) + "\n")
        cfg = root / layout.CONFIG
        before = self.tree(root)
        cfg.chmod(0o444)
        try:
            code, _, _ = self.run_cmd(root, "beta")
        finally:
            cfg.chmod(0o644)
        self.assertEqual(code, 2)
        self.assertEqual(self.tree(root), before)

    def test_uninstall_restores_a_value_after_an_earlier_undo_emptied_the_table(self):
        root = self.project()
        path = root / installer.SETTINGS
        path.parent.mkdir(exist_ok=True)
        wtext(path, json.dumps({"enabledPlugins": {STABLE_ID: True}}, indent=2) + "\n")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(installer.main(["enable-plugin", "--root", str(root), "--id",
                                             STABLE_ID]), 0)
        self.run_cmd(root, "beta")
        wtext(path, json.dumps({"model": "opus"}, indent=2) + "\n")   # edited: key by key
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            installer.main(["uninstall", "--root", str(root), "--force"])
        self.assertEqual(self.settings(root), {"model": "opus",
                                               "enabledPlugins": {STABLE_ID: True}})

    def test_restore_keys_undoes_the_last_record_first(self):
        root = self.project()
        path = root / installer.SETTINGS
        path.parent.mkdir(exist_ok=True)
        wtext(path, json.dumps({"enabledPlugins": {STABLE_ID: False}}, indent=2) + "\n")
        record = {"settings": [], "marketplace": [], "plugin": [
            {"settings": installer.SETTINGS, "id": STABLE_ID, "previous": "false"},
            {"settings": installer.SETTINGS, "id": STABLE_ID, "previous": "true"}]}
        installer._restore_keys(root, record)
        self.assertEqual(self.settings(root), {"enabledPlugins": {STABLE_ID: False}})

    def test_marketplace_entry_with_a_ref(self):
        self.assertEqual(installer.marketplace_entry("a/b", "beta"),
                         {"source": {"source": "github", "repo": "a/b", "ref": "beta"}})
        self.assertEqual(installer.marketplace_entry("a/b"),
                         {"source": {"source": "github", "repo": "a/b"}})
        self.assertEqual(installer.marketplace_for(BETA_ID, None),
                         (layout.BETA_MARKETPLACE, layout.PUBLIC_REPO))


if __name__ == "__main__":
    unittest.main()
