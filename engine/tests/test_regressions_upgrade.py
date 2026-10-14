"""Behaviour tests on small throwaway workspaces: what an upgrade refreshes and renders, and the engine standard."""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from regressions_support import *  # noqa: E402,F401,F403

class UpgradeRefreshesBlocks(Base):
    """An upgrade regenerates every generated block with the engine it pins, so it never turns a
    project red: a doc under a nested `build/` that 0.4.1 governed (its excludes matched only at
    the top) is left out now, and the doc registry 0.4.1 wrote would otherwise read as stale."""

    @staticmethod
    def _top_level_only(rel: str):
        """0.4.1's rule: an always-excluded directory matched only at the top."""
        from fnmatch import fnmatch
        from govern.context import ALWAYS_EXCLUDED_DIRS
        return next((f"{d}/**" for d in ALWAYS_EXCLUDED_DIRS if fnmatch(rel, f"{d}/**")), None)

    def test_a_doc_under_a_nested_build_dir_upgrades_green(self):
        from unittest import mock
        w = self.ws()
        cfg_text = (w.root / CFG).read_text(encoding="utf-8").replace(
            'project = [{ file = "DECISIONS.md", id = "decision-index" }]',
            'project = [{ file = "DECISIONS.md", id = "decision-index" },\n'
            '           { file = "INDEX.md", id = "doc-registry" }]').replace(
            f'engine = "{__version__}"', f'engine = "{SERIES}"')
        cfg = self.tmp / "config.toml"
        wtext(cfg, cfg_text)
        (w.root / CFG).unlink()
        (w.root / layout.GOV_DIR).rmdir()
        w.write("projects/alpha/INDEX.md", fm() + "# Index\n\n"
                "<!-- t:generated:start id=doc-registry -->\n"
                "<!-- t:generated:end id=doc-registry -->\n")
        w.write("projects/alpha/sub/build/notes.md", fm("reference") + "# Notes\n")
        with mock.patch("govern.context.default_exclude", self._top_level_only), \
                contextlib.redirect_stdout(io.StringIO()):
            installer.main(["install", "--root", str(w.root), "--config", str(cfg),
                            "--no-report"])
            w.run("index")
            code, out, _ = w.run("check")
        self.assertEqual(code, 0, out)                          # green on the old rule
        self.assertIn("sub/build/notes.md", (w.root / "projects/alpha/INDEX.md")
                      .read_text(encoding="utf-8"))
        code, out, _ = w.run("check")
        self.assertIn("generated block 'doc-registry' is stale", out)   # what upgrade must fix
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = installer.main(["upgrade", "--root", str(w.root), "--no-report"])
        self.assertEqual(code, 0, buf.getvalue())
        self.assertIn("  index        1 block(s) refreshed", buf.getvalue())
        code, out, _ = w.run("check")
        self.assertEqual(code, 0, out)
        self.assertNotIn("sub/build/notes.md", (w.root / "projects/alpha/INDEX.md")
                         .read_text(encoding="utf-8"))

    def _installed(self) -> Workspace:
        w = self.ws()
        cfg = self.tmp / "config.toml"
        wtext(cfg, (w.root / CFG).read_text(encoding="utf-8").replace(
            f'engine = "{__version__}"', f'engine = "{SERIES}"'))
        (w.root / CFG).unlink()
        (w.root / layout.GOV_DIR).rmdir()
        with contextlib.redirect_stdout(io.StringIO()):
            installer.main(["install", "--root", str(w.root), "--config", str(cfg),
                            "--no-report"])
        return w

    def test_a_tool_file_that_cannot_be_written_rolls_everything_back(self):
        # The first tool file is written, the next fails: the written one is put back too.
        from unittest import mock
        w = self._installed()
        gov = w.root / layout.GOV_DIR
        before = {p: p.read_bytes() for p in sorted(w.root.rglob("*")) if p.is_file()
                  and ".git" not in p.parts}
        def partial(dest_dir):
            (dest_dir / installer.TOOL_FILES[0]).write_bytes(b"half an upgrade\n")
            raise PermissionError(13, "Permission denied",
                                  str(dest_dir / installer.TOOL_FILES[-1]))
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(installer, "_write_tool_files", side_effect=partial), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = installer.main(["upgrade", "--root", str(w.root), "--no-report"])
        self.assertEqual(code, 2)
        self.assertIn("upgrade rolled back:", err.getvalue())
        self.assertIn("Permission denied — nothing was changed", err.getvalue())
        after = {p: p.read_bytes() for p in sorted(w.root.rglob("*")) if p.is_file()
                 and ".git" not in p.parts}
        self.assertEqual(after, before)
        self.assertIn(f'engine = "{SERIES}"', (w.root / CFG).read_text(encoding="utf-8"))
        self.assertTrue((gov / installer.TOOL_FILES[0]).is_file())

    def test_a_tool_file_that_cannot_be_put_back_is_named(self):
        from unittest import mock
        w = self._installed()
        def failing(dest_dir):
            (dest_dir / installer.TOOL_FILES[0]).write_bytes(b"half an upgrade\n")
            raise PermissionError(13, "Permission denied")
        real = Path.write_bytes
        def no_restore(self, data):
            if self.parent.name == "bin" and data != b"half an upgrade\n":
                raise PermissionError(13, "Permission denied")
            return real(self, data)
        err = io.StringIO()
        with mock.patch.object(installer, "_write_tool_files", side_effect=failing), \
                mock.patch.object(Path, "write_bytes", no_restore), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = installer.main(["upgrade", "--root", str(w.root), "--no-report"])
        self.assertEqual(code, 2)
        self.assertNotIn("nothing was changed", err.getvalue())
        self.assertIn(f"but {layout.GOV_DIR}/{installer.TOOL_FILES[0]} could not be put back",
                      err.getvalue())
        self.assertIn(f'engine = "{SERIES}"', (w.root / CFG).read_text(encoding="utf-8"))

    def test_the_pin_rewrite_changes_only_the_pin_line(self):
        # A CRLF config: the upgrade rewrites the pin and nothing else, byte for byte.
        w = self.ws()
        cfg = self.tmp / "config.toml"
        wtext(cfg, (w.root / CFG).read_text(encoding="utf-8").replace(
            f'engine = "{__version__}"', f'engine = "{SERIES}"'))
        (w.root / CFG).unlink()
        (w.root / layout.GOV_DIR).rmdir()
        with contextlib.redirect_stdout(io.StringIO()):
            installer.main(["install", "--root", str(w.root), "--config", str(cfg),
                            "--no-report"])
        text = (w.root / CFG).read_text(encoding="utf-8")
        before = text.replace("\n", "\r\n").encode("utf-8")
        (w.root / CFG).write_bytes(before)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = installer.main(["upgrade", "--root", str(w.root), "--no-report"])
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual((w.root / CFG).read_bytes(), before.replace(
            f'engine = "{SERIES}"\r\n'.encode(), f'engine = "{__version__}"\r\n'.encode()))

    def test_a_config_that_does_not_load_is_restored_byte_for_byte(self):
        # CRLF line endings survive the rollback: the original bytes return, not a re-encoded
        # copy of the text.
        w = self.ws()
        cfg = self.tmp / "config.toml"
        wtext(cfg, (w.root / CFG).read_text(encoding="utf-8").replace(
            f'engine = "{__version__}"', f'engine = "{SERIES}"'))
        (w.root / CFG).unlink()
        (w.root / layout.GOV_DIR).rmdir()
        with contextlib.redirect_stdout(io.StringIO()):
            installer.main(["install", "--root", str(w.root), "--config", str(cfg),
                            "--no-report"])
        text = (w.root / CFG).read_text(encoding="utf-8").replace(
            "[blocks]\n", '[blocks]\nplaceholder = "x"\n', 1)
        original = text.replace("\n", "\r\n").encode("utf-8")
        (w.root / CFG).write_bytes(original)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = installer.main(["upgrade", "--root", str(w.root), "--no-report"])
        self.assertNotEqual(code, 0, out.getvalue())
        self.assertIn("upgrade rolled back", err.getvalue())
        self.assertIn("[blocks] placeholder is no longer used", err.getvalue())
        self.assertEqual((w.root / CFG).read_bytes(), original)

    def test_an_unreadable_doc_rolls_the_upgrade_back_and_changes_nothing(self):
        w = self.ws()
        cfg = self.tmp / "config.toml"
        wtext(cfg, (w.root / CFG).read_text(encoding="utf-8").replace(
            f'engine = "{__version__}"', f'engine = "{SERIES}"'))
        (w.root / CFG).unlink()
        (w.root / layout.GOV_DIR).rmdir()
        with contextlib.redirect_stdout(io.StringIO()):
            installer.main(["install", "--root", str(w.root), "--config", str(cfg),
                            "--no-report"])
        log = w.root / "projects/alpha/DECISIONS.md"
        log.write_bytes(log.read_bytes() + b"\xff\xfe not UTF-8\n")   # the block's own file
        gov = w.root / layout.GOV_DIR
        before = {p: p.read_bytes() for p in sorted(gov.rglob("*")) if p.is_file()}
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = installer.main(["upgrade", "--root", str(w.root), "--no-report"])
        self.assertNotEqual(code, 0, out.getvalue())
        self.assertIn("projects/alpha/DECISIONS.md: not valid UTF-8", err.getvalue())
        self.assertIn("nothing was changed", err.getvalue())
        after = {p: p.read_bytes() for p in sorted(gov.rglob("*")) if p.is_file()}
        self.assertEqual(after, before)             # config.toml and installed.toml included


class UpgradeRendersRoles(Base):
    """Where the agent-roles option is on, an upgrade renders the roles again with the engine
    it pins, all or none, and never touches a file the engine did not render."""

    def installed(self, extra=ROLES_ON):
        w = self.ws(extra=extra)
        cfg = self.tmp / "config.toml"
        wtext(cfg, (w.root / CFG).read_text(encoding="utf-8").replace(
            f'engine = "{__version__}"', f'engine = "{SERIES}"'))
        (w.root / CFG).unlink()
        (w.root / layout.GOV_DIR).rmdir()
        with contextlib.redirect_stdout(io.StringIO()):
            installer.main(["install", "--root", str(w.root), "--config", str(cfg),
                            "--no-report"])
        return w

    def upgrade(self, w, *extra):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = installer.main(["upgrade", "--root", str(w.root), *extra])
        return code, out.getvalue(), err.getvalue()

    def tree(self, w):
        return {p.relative_to(w.root).as_posix(): p.read_bytes()
                for p in sorted(w.root.rglob("*")) if p.is_file()}

    def fresh(self, w, role):
        from govern import roles
        ctx = installer._load_context(w.root)
        return roles.render(roles.shipped(role), role, ctx.cfg.checks["agent-roles"].params)

    def test_upgrade_renders_a_stale_role_again_and_records_it(self):
        from govern import roles
        w = self.installed()
        w.run("agents", "render")
        dev = w.root / ".claude/agents/developer.md"
        # What an earlier engine rendered: the note, and a body this engine words differently.
        wtext(dev, dev.read_text(encoding="utf-8").replace("You implement", "You build"))
        self.assertEqual(installer_check(w.root)[0], 1)
        record = installer.read_manifest(w.root)       # and that engine knew no such table
        record["rendered"] = []
        installer.write_manifest(w.root / layout.MANIFEST, record)
        code, out, err = self.upgrade(w, "--no-report")
        self.assertEqual(code, 0, err)
        self.assertIn("  roles        1 file(s) rendered, 0 removed\n", out)
        with open(dev, encoding="utf-8", newline="") as fh:
            self.assertEqual(fh.read(), self.fresh(w, "developer"))
        rows = {r["path"]: r["sha256"] for r in installer.read_manifest(w.root)["rendered"]}
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[".claude/agents/developer.md"],
                         roles.digest(self.fresh(w, "developer")))
        self.assertEqual(installer_check(w.root)[0], 0)

    def test_upgrade_does_not_render_with_the_option_off(self):
        w = self.installed(extra="")
        code, out, err = self.upgrade(w, "--no-report")
        self.assertEqual(code, 0, err)
        self.assertNotIn("roles", out)
        self.assertFalse((w.root / ".claude").exists())
        self.assertEqual(installer.read_manifest(w.root)["rendered"], [])

    def test_upgrade_leaves_a_file_it_did_not_render_alone_names_it_and_succeeds(self):
        w = self.installed()
        mine = "---\nname: developer\n---\nOur own developer, written by hand.\n"
        w.write(".claude/agents/developer.md", mine)
        shutil.copytree(ENGINE / "govern", layout.engines_dir(w.home) / __version__ / "govern",
                        ignore=shutil.ignore_patterns("__pycache__"))
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(w.home)
        self.addCleanup(lambda: os.environ.__setitem__("HOME", old_home) if old_home
                        else os.environ.pop("HOME", None))
        code, out, err = self.upgrade(w)
        self.assertEqual(code, 0, err)
        self.assertEqual((w.root / ".claude/agents/developer.md").read_text(encoding="utf-8"),
                         mine)
        said = (".claude/agents/developer.md: was not rendered by context-gate, so it is left "
                "alone")
        self.assertIn(f"  roles        left alone: {said}", out)
        self.assertIn("  roles        5 file(s) rendered, 0 removed\n", out)
        self.assertTrue((w.root / ".claude/agents/qa-engineer.md").is_file())
        rep = (w.root / layout.GOV_DIR / "upgrade-report.md").read_text(encoding="utf-8")
        self.assertIn("## Agent roles left for a person\n", rep)
        self.assertIn(f"\n- {said}", rep)
        self.assertNotIn(".claude/agents/developer.md",
                         [r["path"] for r in installer.read_manifest(w.root)["rendered"]])

    def test_upgrade_goes_on_past_a_setting_that_cannot_be_rendered(self):
        w = self.installed(extra=ROLES_ON + 'extra_roles = ["dba"]\n')
        code, out, err = self.upgrade(w, "--no-report")
        self.assertEqual(code, 0, err)
        self.assertIn("  roles        not rendered: [checks.agent-roles] extra_roles names 'dba'",
                      out)
        self.assertFalse((w.root / ".claude").exists())

    def test_an_unreadable_insert_rolls_the_upgrade_back_and_changes_nothing(self):
        w = self.installed()
        w.write(".context-gate/agents/all.md", b"- caf\xe9\n")
        before = self.tree(w)
        code, out, err = self.upgrade(w, "--no-report")
        self.assertNotEqual(code, 0, out)
        self.assertIn("upgrade rolled back: .context-gate/agents/all.md: not valid UTF-8 "
                      "(byte 5), so the agent roles cannot be rendered — nothing was changed",
                      err)
        self.assertEqual(self.tree(w), before)

    def test_a_role_that_cannot_be_written_rolls_the_upgrade_back(self):
        from unittest import mock
        from govern import roles
        w = self.installed()
        w.run("agents", "render")
        dev = w.root / ".claude/agents/developer.md"
        wtext(dev, dev.read_text(encoding="utf-8").replace("You implement", "You build"))
        (w.root / ".claude/agents/qa-engineer.md").unlink()
        before = self.tree(w)
        real, calls = roles.write, []

        def failing(path, text):
            calls.append(path)
            if len(calls) == 2:
                raise PermissionError(13, "Permission denied", str(path))
            real(path, text)
        with mock.patch.object(roles, "write", failing):
            code, out, err = self.upgrade(w, "--no-report")
        self.assertNotEqual(code, 0, out)
        self.assertEqual(len(calls), 2)
        self.assertIn("upgrade rolled back:", err)
        self.assertIn("so the agent roles cannot be rendered — nothing was changed", err)
        self.assertEqual(self.tree(w), before)

    def test_rendered_roles_are_put_back_when_the_tool_files_cannot_be_refreshed(self):
        from unittest import mock
        w = self.installed()
        w.run("agents", "render")
        dev = w.root / ".claude/agents/developer.md"
        wtext(dev, dev.read_text(encoding="utf-8").replace("You implement", "You build"))
        (w.root / ROLES_RULES).unlink()
        (w.root / ROLES_RULES).parent.rmdir()
        before = self.tree(w)
        stuck = (PermissionError(13, "Permission denied", "bin/govern"), [])
        with mock.patch.object(installer, "_refresh_tool_files", return_value=stuck):
            code, out, err = self.upgrade(w, "--no-report")
        self.assertNotEqual(code, 0, out)
        self.assertIn("nothing was changed", err)
        self.assertEqual(self.tree(w), before)
        self.assertFalse((w.root / ".claude/rules").exists())


class UpgradeReportAndPluginOptIn(Base):
    """The upgrade report when both runs used one engine, the report's settings format, and the
    plugin opt-in recorded on install and reversed on uninstall."""

    def project(self):
        w = self.ws()
        cfg = self.tmp / "config.toml"
        shutil.move(str(w.root / CFG), str(cfg))
        (w.root / layout.GOV_DIR).rmdir()
        return w, cfg

    def test_upgrade_report_says_when_both_runs_used_one_engine(self):
        w, cfg = self.project()
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(f'engine = "{__version__}"',
                                                            f'engine = "{SERIES}"'))
        shutil.copytree(ENGINE / "govern", layout.engines_dir(w.home) / __version__ / "govern",
                        ignore=shutil.ignore_patterns("__pycache__"))
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(w.home)
        self.addCleanup(lambda: os.environ.__setitem__("HOME", old_home) if old_home
                        else os.environ.pop("HOME", None))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            installer.main(["install", "--root", str(w.root), "--config", str(cfg), "--no-report"])
            installer.main(["upgrade", "--root", str(w.root)])
        self.assertIn(f"upgraded context-gate {SERIES} (ran {__version__}) -> {__version__}",
                      out.getvalue())
        rep = (w.root / layout.GOV_DIR / "upgrade-report.md").read_text(encoding="utf-8")
        self.assertIn(f"Both runs used engine {__version__}", rep)
        self.assertNotIn("## What changed in the engine", rep)   # same engine: nothing new
        self.assertIn(f"| Engine | {__version__} | {__version__} |", rep)

    def test_upgrade_lists_new_options_and_keeps_the_answered_ones(self):
        # The merged upgrade: blocks refreshed, then the report with the options not yet
        # answered; the install record's answers survive the manifest rewrite.
        from govern import options
        w, cfg = self.project()
        with contextlib.redirect_stdout(io.StringIO()):
            installer.main(["install", "--root", str(w.root), "--config", str(cfg), "--no-report"])
        first, *rest = options.ids()
        options.record(w.root, [first])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(installer.main(["upgrade", "--root", str(w.root)]), 0)
        self.assertEqual(options.answered(w.root), {first})
        self.assertIn("  index        ", out.getvalue())
        self.assertIn(f"  options      {len(rest)} to review: {', '.join(rest)}", out.getvalue())
        rep = (w.root / layout.GOV_DIR / "upgrade-report.md").read_text(encoding="utf-8")
        section = rep[rep.index("## New options"):rep.index("## New findings")]
        self.assertNotIn(f"`{first}`", section)
        for cid in rest:
            self.assertIn(f"`{cid}`", section)

    def test_a_list_valued_registry_license_never_breaks_options_or_upgrade(self):
        w, cfg = self.project()
        reg = w.root / "projects.toml"
        wtext(reg, reg.read_text(encoding="utf-8").replace(
            'license = "MIT"', 'license = ["MIT", "Apache-2.0"]')
            + '\n[[project]]\nname = "beta"\ndir = "beta"\ntier = "registered"\n'
              'license = "GPL-3.0"\n')
        with contextlib.redirect_stdout(io.StringIO()):
            installer.main(["install", "--root", str(w.root), "--config", str(cfg), "--no-report"])
        code, out, err = w.run("options", "--json")
        self.assertEqual(code, 0, err)
        lic = next(o for o in json.loads(out) if o["id"] == "licenses")
        self.assertEqual(lic["suggestion"],
                         "2 registry entries; licenses: GPL-3.0, MIT + Apache-2.0")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(installer.main(["upgrade", "--root", str(w.root)]), 0)
        self.assertIn("## New options", (w.root / layout.GOV_DIR / "upgrade-report.md")
                      .read_text(encoding="utf-8"))

    def test_an_upgrade_from_the_old_check_name_stays_green_and_reports_the_rename(self):
        # A project written for 0.4.1, when the check was `licences`: the old gate (a stand-in
        # engine that reports nothing) passed, the upgrade passes, and the report names the
        # rename as a new finding, so the project sees what to rename.
        w = self.ws(extra='\n[checks.licences]\nlevel = "error"\n')
        cfg = self.tmp / "config.toml"
        shutil.move(str(w.root / CFG), str(cfg))
        (w.root / layout.GOV_DIR).rmdir()
        old = layout.engines_dir(w.home) / "0.4.1" / "govern"
        old.mkdir(parents=True)
        wtext(old / "__init__.py", '__version__ = "0.4.1"\n')
        wtext(old / "cli.py", "def main(root=None, prog=None):\n    return 0\n")
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(w.home)
        self.addCleanup(lambda: os.environ.__setitem__("HOME", old_home) if old_home
                        else os.environ.pop("HOME", None))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(installer.main(["install", "--root", str(w.root), "--config",
                                             str(cfg), "--no-report"]), 0)
        pinned = w.root / CFG
        wtext(pinned, pinned.read_text(encoding="utf-8").replace(
            f'engine = "{__version__}"', 'engine = "0.4.1"'))
        said = ("'licences' is now 'licenses' (the old name still works; rename it)")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(installer.main(["upgrade", "--root", str(w.root)]), 0,
                             err.getvalue())
        self.assertIn(said, err.getvalue())
        rep = (w.root / layout.GOV_DIR / "upgrade-report.md").read_text(encoding="utf-8")
        self.assertIn(f"- **warn** {CFG} [checks]: {said}", rep[rep.index("## New findings"):])
        code, out, _ = w.run("check")
        self.assertEqual(code, 0, out)

    def test_load_warnings_are_said_and_read_as_unchanged_by_the_upgrade_report(self):
        # The old gate prints `warning: ...` for a docs entry it can never govern, and the
        # report reads that line as a finding: the new side counts the same warning, so it is
        # unchanged, never "no longer reported". Install and upgrade say it too.
        w, cfg = self.project()
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            f'engine = "{__version__}"', f'engine = "{SERIES}"').replace(
            "[projects]\n", '[projects]\ndocs = ["**/*.md", ".claude/x.md"]\n', 1))
        shutil.copytree(ENGINE / "govern", layout.engines_dir(w.home) / __version__ / "govern",
                        ignore=shutil.ignore_patterns("__pycache__"))
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(w.home)
        self.addCleanup(lambda: os.environ.__setitem__("HOME", old_home) if old_home
                        else os.environ.pop("HOME", None))
        said = "warning: [projects] docs entry '.claude/x.md' is never governed"
        for argv in (["install", "--root", str(w.root), "--config", str(cfg), "--no-report"],
                     ["upgrade", "--root", str(w.root)]):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(installer.main(argv), 0, err.getvalue())
            self.assertEqual(err.getvalue().count(said), 1, argv[0])
        self.assertIn(" 0 no longer reported", out.getvalue())
        rep = (w.root / layout.GOV_DIR / "upgrade-report.md").read_text(encoding="utf-8")
        self.assertNotIn(".claude/x.md", rep[rep.index("## New findings"):])

    def test_report_settings_use_explain_format(self):
        w, cfg = self.project()
        with contextlib.redirect_stdout(io.StringIO()):
            installer.main(["install", "--root", str(w.root), "--config", str(cfg)])
        rep = (w.root / layout.GOV_DIR / "install-report.md").read_text(encoding="utf-8")
        self.assertIn('statuses = ["locked", "provisional", "superseded"]', rep)
        self.assertNotIn("['locked'", rep)

    def test_plugin_opt_in_is_recorded_and_reversed(self):
        w, cfg = self.project()
        w.write(".claude/settings.json", '{\n  "hooks": {}\n}\n')
        before = (w.root / ".claude/settings.json").read_text(encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            installer.main(["install", "--root", str(w.root), "--config", str(cfg),
                            "--no-report", "--enable-plugin", "context-gate@skills-dir"])
        settings = json.loads((w.root / ".claude/settings.json").read_text(encoding="utf-8"))
        self.assertIs(settings["enabledPlugins"]["context-gate@skills-dir"], True)
        self.assertIn("[[plugin]]", (w.root / layout.MANIFEST).read_text(encoding="utf-8"))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(installer.main(["uninstall", "--root", str(w.root)]), 0)
        self.assertEqual(json.loads((w.root / ".claude/settings.json").read_text(encoding="utf-8")),
                         json.loads(before))

    def test_enable_plugin_on_an_existing_install_keeps_a_previous_value(self):
        w, cfg = self.project()
        w.write(".claude/settings.json",
                '{"enabledPlugins": {"context-gate@skills-dir": false}}')
        with contextlib.redirect_stdout(io.StringIO()):
            installer.main(["install", "--root", str(w.root), "--config", str(cfg), "--no-report"])
            installer.main(["enable-plugin", "--root", str(w.root),
                            "--id", "context-gate@skills-dir"])
            installer.main(["uninstall", "--root", str(w.root)])
        settings = json.loads((w.root / ".claude/settings.json").read_text(encoding="utf-8"))
        self.assertIs(settings["enabledPlugins"]["context-gate@skills-dir"], False)


class Standard(Base):
    """The standard: pointers for replaced decisions, topics, links, and overrides that carry
    their reason."""

    def log(self, w):
        return w.root / "projects/alpha/DECISIONS.md"

    def test_pointer_has_no_body_and_resolves(self):
        w = self.ws()
        log = self.log(w)
        wtext(log, log.read_text(encoding="utf-8") + entry("A", 101)
              + "\n## A-102 — Replaced by A-101\n"
              + "\n## A-103 — Replaced by A-199\n"
              + "\n## A-104 — Replaced by A-101\n\nOld text that misleads.\n")
        w.index()
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertNotIn("A-102", out)
        self.assertIn("A-103 points to A-199, which is not in this log", out)
        self.assertIn("A-104 is a pointer to A-101, so it carries no body", out)
        index = log.read_text(encoding="utf-8")
        self.assertIn("| A-102 | *replaced* | → A-101 |", index)

    def test_superseded_entry_is_told_to_rewrite_or_point(self):
        w = self.ws(extra="")
        cfg = w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            'statuses = ["locked", "provisional", "superseded"]', ""))
        log = self.log(w)
        wtext(log, log.read_text(encoding="utf-8") + entry("A", 101, status="superseded"))
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertIn("A-101 is superseded — rewrite it in place", out)

    def superseded_advice(self, sep: str, em_dash: bool) -> str:
        root = self.tmp / ("em" if em_dash else "hyphen")
        root.mkdir()
        w = Workspace(root, extra="")
        cfg = w.root / CFG
        text_ = cfg.read_text(encoding="utf-8").replace(
            'statuses = ["locked", "provisional", "superseded"]', "")
        if not em_dash:
            text_ = text_.replace('decision_heading = "em-dash"\n', "")
        wtext(cfg, text_)
        log = self.log(w)
        wtext(log, log.read_text(encoding="utf-8")
              + entry("A", 101, status="superseded", sep=sep))
        return w.run("check", "--project", "alpha")[1]

    def test_superseded_advice_shows_a_hyphen_in_a_hyphen_form_log(self):
        self.assertIn("`## A-101 - Replaced by <id>`", self.superseded_advice(" - ", False))

    def test_superseded_advice_shows_an_em_dash_in_an_em_dash_form_log(self):
        self.assertIn("`## A-101 — Replaced by <id>`", self.superseded_advice(" — ", True))

    def test_index_groups_by_topic(self):
        w = self.ws()
        log = self.log(w)
        wtext(log, log.read_text(encoding="utf-8")
              + entry("A", 101, extra="\n**Topic:** Sync\n")
              + entry("A", 102, extra="\n**Topic:** Sync\n"))
        w.index()
        text = log.read_text(encoding="utf-8")
        self.assertIn("**Sync**", text)
        self.assertLess(text.index("| A-100 |"), text.index("**Sync**"))

    def test_padded_and_unpadded_ids_are_one_id(self):
        w = self.ws()
        log = self.log(w)
        wtext(log, log.read_text(encoding="utf-8").replace("## A-100 —", "## A-0100 —"))
        code, out, _ = w.run("show", "--project", "alpha", "A-100")
        self.assertEqual(code, 0, out)
        self.assertIn("## A-0100 — Title 100", out)

    def test_doc_registry_is_grouped_and_links_from_the_index(self):
        w = self.ws()
        cfg = w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            'project = [{ file = "DECISIONS.md", id = "decision-index" }]',
            'project = [{ file = "DECISIONS.md", id = "decision-index" }, '
            '{ file = "INDEX.md", id = "doc-registry" }]'))
        w.write("projects/alpha/INDEX.md", fm() + "<!-- t:generated:start id=doc-registry -->\n"
                "<!-- t:generated:end id=doc-registry -->\n")
        w.write("projects/alpha/working-files/plan.md", fm("working", status="active") + "x\n")
        w.index()
        text = (w.root / "projects/alpha/INDEX.md").read_text(encoding="utf-8")
        self.assertIn("**Control**", text)
        self.assertIn("**Working**", text)
        self.assertIn("[`working-files/plan.md`](working-files/plan.md) | testing — _active_ |", text)
        code, out, _ = w.run("check")
        self.assertEqual(code, 0, out)
        w.write("projects/alpha/orphan.md", fm())
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertIn("alpha/orphan.md: not reachable from INDEX.md's doc registry", out)

    def test_broken_body_link(self):
        w = self.ws()
        w.write("projects/alpha/note.md", fm() + "See [the plan](plan.md) and `[x](nope.md)`.\n"
                "```\n[fenced](gone.md)\n```\n")
        code, out, _ = w.run("check")
        self.assertIn("alpha/note.md: link target does not exist: plan.md", out)
        self.assertNotIn("nope.md", out)
        self.assertNotIn("gone.md", out)

    def _git(self, root: Path, *args: str) -> None:
        subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t",
                        "-c", "tag.gpgSign=false", "-c", "commit.gpgSign=false", *args],
                       check=True, capture_output=True)

    def test_broken_link_to_a_deleted_file_names_the_commit(self):
        w = self.ws()
        self._git(w.root, "init", "-q")
        self._git(w.root, "add", "-A")
        self._git(w.root, "commit", "-qm", "initial")
        w.write("projects/alpha/plan.md", fm() + "# Plan\n")
        self._git(w.root, "add", "-A")
        self._git(w.root, "commit", "-qm", "add plan")
        (w.root / "projects/alpha/plan.md").unlink()
        self._git(w.root, "add", "-A")
        self._git(w.root, "commit", "-qm", "remove plan")
        res = subprocess.run(["git", "-C", str(w.root), "log", "-1", "--diff-filter=D",
                              "--format=%h", "--", "projects/alpha/plan.md"],
                             capture_output=True, text=True)
        sha = res.stdout.strip()
        self.assertTrue(sha)
        w.write("projects/alpha/note.md", fm() + "See [the plan](plan.md).\n")
        code, out, _ = w.run("check")
        self.assertIn(f"alpha/note.md: link target does not exist: plan.md — deleted in {sha}; "
                      f"link the last commit that had it ({sha}^:projects/alpha/plan.md) instead "
                      f"of removing the link", out)

    def test_broken_link_that_never_existed_has_no_hint(self):
        w = self.ws()
        self._git(w.root, "init", "-q")
        self._git(w.root, "add", "-A")
        self._git(w.root, "commit", "-qm", "initial")
        w.write("projects/alpha/note.md", fm() + "See [ghost](ghost.md).\n")
        code, out, _ = w.run("check")
        self.assertIn("alpha/note.md: link target does not exist: ghost.md", out)
        self.assertNotIn("deleted in", out)

    def test_broken_link_hint_is_cached_per_run(self):
        from unittest import mock
        w = self.ws()
        self._git(w.root, "init", "-q")
        self._git(w.root, "add", "-A")
        self._git(w.root, "commit", "-qm", "initial")
        w.write("projects/alpha/plan.md", fm() + "# Plan\n")
        self._git(w.root, "add", "-A")
        self._git(w.root, "commit", "-qm", "add plan")
        (w.root / "projects/alpha/plan.md").unlink()
        self._git(w.root, "add", "-A")
        self._git(w.root, "commit", "-qm", "remove plan")
        w.write("projects/alpha/note.md",
               fm() + "See [the plan](plan.md) and [it again](plan.md).\n")
        from govern.checks import links as links_mod
        with mock.patch.object(links_mod, "git", wraps=links_mod.git) as spy:
            code, out, _ = w.run("check")
        self.assertEqual(spy.call_count, 1)

    def test_skill_without_description(self):
        w = self.ws()
        w.write(".claude/skills/thing/SKILL.md", "---\nname: thing\n---\nbody\n")
        code, out, _ = w.run("check")
        self.assertIn("SKILL.md: skill is missing 'description'", out)

    def test_format_override_needs_a_reason(self):
        w = self.ws()
        code, out, _ = w.run("check")
        self.assertIn('[dialect] decision_heading = "em-dash" differs from the standard', out)
        cfg = w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            'markers = "t"', 'markers = "t"\n\n[dialect.reasons]\ndecision_heading = "old logs"'))
        code, out, _ = w.run("check")
        self.assertNotIn("differs from the standard", out)

    def unreasoned(self, w: Workspace, extra: str = "") -> None:
        """Take away the fixture's `reasons.statuses`, so its re-allowed 'superseded' is a
        widening with no reason; `extra` goes in the same [checks.decision-log] table."""
        cfg = w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(REASON_LINE, extra))

    def test_widened_list_names_only_the_loosening_side(self):
        # The fixture's own [checks.decision-log] re-allows 'superseded':
        # statuses = ["locked", "provisional", "superseded"]. It also drops 'deferred', which
        # tightens — a tightening must never be named as loosening.
        w = self.ws()
        self.unreasoned(w)
        code, _, err = w.run("check")
        self.assertEqual(code, 2)
        self.assertIn("project: [checks.decision-log] statuses adds 'superseded' — loosens past "
                      "what the project inherits without a reason: say why in "
                      "[checks.decision-log.reasons] statuses, or keep the inherited list", err)
        self.assertNotIn("deferred", err)
        wtext(w.root / CFG, (w.root / CFG).read_text(encoding="utf-8").replace(
            'statuses = ["locked", "provisional", "superseded"]',
            'statuses = ["locked", "provisional", "superseded"]\n\n[checks.decision-log.reasons]'
            '\nstatuses = "our older entries predate the standard"'))
        code, out, err = w.run("check")
        self.assertNotEqual(code, 2, err)
        self.assertNotIn("statuses adds", out + err)

    def test_table_reason_does_not_silence_a_list_loosening(self):
        # A reason written for one setting (max_words) must never silently excuse another
        # (statuses re-allowing 'superseded') — only a per-setting reasons.statuses does.
        w = self.ws()
        self.unreasoned(w, 'max_words = 400\nreason = "measured elsewhere"\n')
        code, _, err = w.run("check")
        self.assertEqual(code, 2)
        self.assertIn("[checks.decision-log] statuses adds 'superseded'", err)
        wtext(w.root / CFG, (w.root / CFG).read_text(encoding="utf-8").replace(
            'reason = "measured elsewhere"',
            'reason = "measured elsewhere"\n\n[checks.decision-log.reasons]\n'
            'statuses = "our older entries predate the standard"'))
        code, out, err = w.run("check")
        self.assertNotEqual(code, 2, err)
        self.assertNotIn("statuses adds", out + err)

    def test_emptied_required_list_needs_a_reason(self):
        w = self.ws(extra='required_fields = ["Rule"]\n')
        code, _, err = w.run("check")
        self.assertEqual(code, 2)
        self.assertIn("project: [checks.decision-log] required_fields drops 'Why' — loosens "
                      "past what the project inherits without a reason: say why in "
                      "[checks.decision-log.reasons] required_fields", err)
        wtext(w.root / CFG, (w.root / CFG).read_text(encoding="utf-8").replace(
            'required_fields = ["Rule"]',
            'required_fields = ["Rule"]\nreason = "traps carry no Why field"'))
        code, _, err = w.run("check")
        self.assertIn("required_fields drops", err)   # a table-level reason does not cover it
        wtext(w.root / CFG, (w.root / CFG).read_text(encoding="utf-8").replace(
            REASON_LINE, 'reasons = { statuses = "older entries predate the standard", '
                         'required_fields = "traps carry no Why field" }\n'))
        code, out, err = w.run("check")
        self.assertNotEqual(code, 2, err)
        self.assertNotIn("required_fields drops", out + err)

    def test_emptying_an_anything_goes_list_needs_a_reason(self):
        w = self.ws(extra='\n[checks.doc-frontmatter]\nworking_statuses = []\n')
        code, _, err = w.run("check")
        self.assertEqual(code, 2)
        self.assertIn("project: [checks.doc-frontmatter] working_statuses drops 'active', "
                      "'held', 'planned', 'complete', 'superseded' — loosens past what the "
                      "project inherits without a reason", err)
        wtext(w.root / CFG, (w.root / CFG).read_text(encoding="utf-8").replace(
            "working_statuses = []",
            'working_statuses = []\n\n[checks.doc-frontmatter.reasons]\n'
            'working_statuses = "this project writes free-text status notes"'))
        code, out, err = w.run("check")
        self.assertNotEqual(code, 2, err)
        self.assertNotIn("working_statuses drops", out + err)

    def test_every_widened_list_of_a_check_is_named_at_once(self):
        w = self.ws()
        self.unreasoned(w, 'required_fields = ["Rule"]\n')
        code, _, err = w.run("check")
        self.assertEqual(code, 2)
        self.assertIn("statuses adds 'superseded'", err)
        self.assertIn("required_fields drops 'Why'", err)

    def test_without_require_reasons_a_widened_list_is_a_warning(self):
        w = self.ws()
        self.unreasoned(w)
        cfg = w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            "schema = 1", "schema = 1\nrequire_reasons = false", 1))
        code, out, err = w.run("check")
        self.assertNotEqual(code, 2, err)
        self.assertIn("config: [checks.decision-log] statuses adds 'superseded' — loosens past "
                      "what the project inherits with no reason (say why in reasons.statuses)",
                      out)

    def test_repo_alias_and_default_subcommand(self):
        w = self.ws()
        code, out, _ = w.run("--repo", "alpha")
        self.assertEqual(code, 0, out)
        self.assertIn("[OK] alpha", out)
        code, out, _ = w.run("next-id", "--repo", "alpha")
        self.assertEqual(out.strip(), "A-101")

    def test_registry_well_formed(self):
        w = self.ws(alpha_extra='', ws_range="1-99")
        reg = w.root / "projects.toml"
        wtext(reg, reg.read_text(encoding="utf-8").replace('tier = "full"', 'tier = "gold"'))
        code, out, _ = w.run("check")
        self.assertIn("registry: 'alpha' tier 'gold' is not one of", out)


class LocalOnlyRoles(Base):
    """`agent-roles` on in `local.toml` alone: an upgrade keeps the rendered roles on this
    machine, as `govern agents render` does, and the project stays clean in git."""

    LOCAL = ('[governance]\nengine = "0.7.0-beta.9"\n\n'
             '[checks.agent-roles]\nlevel = "error"\n')
    START = "# context-gate: rendered on this machine"

    def git(self, root, *args):
        res = subprocess.run(["git", "-C", str(root), "-c", "user.name=t",
                              "-c", "user.email=t@e.x", "-c", "commit.gpgsign=false", *args],
                             capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(res.returncode, 0, res.stderr)
        return res.stdout

    def project(self, local=True):
        """A committed git repository, installed at this engine; `local.toml` turns the roles on
        for this machine only, and the engine is a beta, the only kind that reads it."""
        w = self.ws(extra="" if local else ROLES_ON)
        cfg = self.tmp / "config.toml"
        shutil.move(str(w.root / CFG), str(cfg))
        (w.root / layout.GOV_DIR).rmdir()
        with contextlib.redirect_stdout(io.StringIO()):
            installer.main(["install", "--root", str(w.root), "--config", str(cfg),
                            "--no-report"])
        self.git(w.root, "init", "-q")
        if local:
            w.write(layout.LOCAL, self.LOCAL)
            with open(w.root / ".git/info/exclude", "a", encoding="utf-8", newline="") as fh:
                fh.write(f"{layout.LOCAL}\n")
        self.git(w.root, "add", "-A")
        self.git(w.root, "commit", "-q", "-m", "start")
        if local:
            patch = mock.patch.object(config, "__version__", "0.7.0-beta.9")
            patch.start()
            self.addCleanup(patch.stop)
        return w

    def upgrade(self, w):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = installer.main(["upgrade", "--root", str(w.root), "--no-report"])
        return code, out.getvalue(), err.getvalue()

    def files(self, w):
        return {p: p.read_bytes() for p in sorted(w.root.rglob("*"))
                if p.is_file() and ".git/objects" not in p.as_posix()}

    def test_upgrade_keeps_local_roles_local(self):
        from govern import roles
        w = self.project()
        code, out, err = self.upgrade(w)
        self.assertEqual(code, 0, out + err)
        self.assertIn("  roles        6 file(s) rendered, 0 removed\n", out)
        self.assertEqual(installer.read_manifest(w.root)["rendered"], [])
        self.assertEqual(len(roles._rows(w.root / roles.LOCAL_RECORD)), 6)
        block = (w.root / ".git/info/exclude").read_text(encoding="utf-8")
        self.assertIn(f"{self.START}\n/.claude/agents/code-reviewer.md\n", block)
        self.assertIn(f"/{roles.LOCAL_RECORD}\n# context-gate: end\n", block)
        self.assertEqual(self.git(w.root, "status", "--short"), "")

    def test_second_upgrade_changes_no_byte(self):
        w = self.project()
        self.assertEqual(self.upgrade(w)[0], 0)
        before = self.files(w)
        code, out, err = self.upgrade(w)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.files(w), before)
        self.assertEqual(self.git(w.root, "status", "--short"), "")

    def test_committed_roles_are_recorded_as_before(self):
        from govern import roles
        w = self.project(local=False)
        code, out, err = self.upgrade(w)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(len(installer.read_manifest(w.root)["rendered"]), 6)
        self.assertFalse((w.root / roles.LOCAL_RECORD).exists())
        self.assertNotIn(self.START, (w.root / ".git/info/exclude").read_text(encoding="utf-8"))

    def test_failing_git_is_said_and_the_upgrade_stands(self):
        from govern import roles
        w = self.project()
        with mock.patch.object(roles, "_git", return_value=None):
            code, out, err = self.upgrade(w)
        self.assertEqual(code, 0, out + err)
        self.assertIn("  roles        not kept out of git (", out)
        self.assertIn(f"): run {roles.RENDER}\n", out)
        self.assertTrue((w.root / ".claude/agents/developer.md").is_file())


if __name__ == "__main__":
    unittest.main()
