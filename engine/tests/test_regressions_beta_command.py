"""Behaviour tests on small throwaway workspaces: the beta command."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import profile_guard  # noqa: E402,F401

from regressions_support import *  # noqa: E402,F401,F403

class BetaCommand(Base):
    """`govern beta`, handled by the entry point before any engine loads: on checks everything
    before writing anything, off needs no engine, and the user's own settings survive both."""

    def setUp(self) -> None:
        super().setUp()
        w = self.ws()
        self.entry, self.env = self._installed(w)
        self._engines(w, (__version__, BETA))
        self.root, self.home = w.root, w.home
        self.env = {**self.env, "XDG_CONFIG_HOME": str(self.home)}
        self.git("init", "-q", str(self.root))
        (self.root / ".claude").mkdir()
        self.settings = self.root / ".claude" / "settings.local.json"
        self.plugin_release = self.home / ".claude/skills/context-gate-beta/govern/RELEASE"
        self.plugin_release.parent.mkdir(parents=True)
        wtext(self.plugin_release, f"v{BETA}\n")

    def git(self, *args: str, env=None) -> subprocess.CompletedProcess:
        """git under the temp HOME: the developer's own git config never reaches a test."""
        return subprocess.run(["git", *args], env=env or self.env, capture_output=True,
                              text=True)

    def run_beta(self, *args: str, env=None) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(self.entry), "beta", *args],
                              env=env or self.env, capture_output=True, text=True,
                              cwd=self.root)

    def check_ignored(self) -> int:
        return self.git("-C", str(self.root), "check-ignore", "-q", layout.LOCAL).returncode

    def state(self) -> dict:
        """Every file in the project, its bytes, git's exclude included."""
        files = [p for p in self.root.rglob("*") if p.is_file() and ".git" not in p.parts]
        files.append(self.root / ".git" / "info" / "exclude")
        return {p: p.read_bytes() for p in files if p.is_file()}

    def test_on_then_off_restores_settings(self):
        own = {"permissions": {"allow": ["Bash(ls)"]}, "enabledPlugins": {"x@y": True},
               "note": "caf\u00e9"}
        wtext(self.settings, json.dumps(own, indent=2, ensure_ascii=False) + "\n")
        before = self.settings.read_bytes()
        res = self.run_beta("on", BETA)
        self.assertEqual(res.returncode, 0, res.stderr)
        got = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(got["enabledPlugins"], {"x@y": True, "context-gate-beta@skills-dir": True,
                                                 "context-gate@context-gate": False})
        self.assertEqual(got["permissions"], own["permissions"])
        self.assertIn(f'engine = "{BETA}"', (self.root / layout.LOCAL).read_text(encoding="utf-8"))
        self.assertEqual(self.check_ignored(), 0)
        self.assertIn("Restart the Claude Code session", res.stdout)
        res = self.run_beta("off")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.settings.read_bytes(), before)
        self.assertFalse((self.root / layout.LOCAL).exists())
        self.assertIn("Back on the committed pin", res.stdout)

    def test_on_and_off_with_no_settings_file(self):
        self.assertEqual(self.run_beta("on", BETA).returncode, 0)
        self.assertTrue(self.settings.is_file())
        self.assertEqual(self.run_beta("off").returncode, 0)
        self.assertFalse(self.settings.exists())

    def test_settings_bom_and_crlf_survive(self):
        raw = b'\xef\xbb\xbf{\r\n  "permissions": {}\r\n}\r\n'
        self.settings.write_bytes(raw)
        self.assertEqual(self.run_beta("on", BETA).returncode, 0)
        on = self.settings.read_bytes()
        self.assertTrue(on.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn(b"\n", on.replace(b"\r\n", b""))
        self.assertEqual(self.run_beta("off").returncode, 0)
        self.assertEqual(self.settings.read_bytes(), raw)

    def test_on_twice_changes_nothing(self):
        self.assertEqual(self.run_beta("on", BETA).returncode, 0)
        once = self.state()
        self.assertEqual(self.run_beta("on", f"v{BETA}").returncode, 0)
        self.assertEqual(self.state(), once)
        exclude = (self.root / ".git" / "info" / "exclude").read_text(encoding="utf-8")
        self.assertEqual(exclude.count(layout.LOCAL), 1)

    def test_on_keeps_other_tables_for_the_same_beta(self):
        text = f'[governance]\nengine = "{BETA}"\n\n[checks.usage]\nenabled = true\n'
        wtext(self.root / layout.LOCAL, text)
        self.assertEqual(self.run_beta("on", BETA).returncode, 0)
        self.assertEqual((self.root / layout.LOCAL).read_text(encoding="utf-8"), text)

    # UB-10: `beta on B` while beta A is on moves the engine line and keeps the rest of local.toml.
    STABLE_TRUE = '{\n  "enabledPlugins": {\n    "context-gate@context-gate": true\n  }\n}\n'
    OWN = "\n# my alerts, kept across betas\n[checks.usage]\nenabled = true\n"

    def _second_beta(self) -> str:
        """Another beta, its engine and the local plugin installed; the first stays installed."""
        other = f"{SERIES}.99-beta.2"
        if not (layout.engines_dir(self.home) / other).is_dir():
            self._engines(SimpleNamespace(home=self.home), (other,))
        wtext(self.plugin_release, f"v{other}\n")
        return other

    def _first_beta_on(self, local: str | None = None) -> Path:
        """BETA on over a settings file that had the stable plugin on, then the user's own
        lines added to local.toml (or local.toml replaced by `local`). Starts over each call."""
        path = self.root / layout.LOCAL
        path.unlink(missing_ok=True)
        wtext(self.plugin_release, f"v{BETA}\n")
        wtext(self.settings, self.STABLE_TRUE)
        res = self.run_beta("on", BETA)
        self.assertEqual(res.returncode, 0, res.stderr)
        with open(path, "rb") as fh:
            written = fh.read().decode("utf-8")
        wtext(path, written + self.OWN if local is None else local)
        return path

    def test_ub10_on_another_beta_switches_and_keeps_the_rest_of_local_toml(self):
        local = self._first_beta_on()
        before = local.read_bytes()
        self.assertEqual(before.count(BETA.encode()), 1)
        self.assertIn(b'plugins_before = { "context-gate@context-gate" = true }\n', before)
        other = self._second_beta()
        res = self.run_beta("on", other)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.splitlines()[0],
                         f"beta {other} on for this project, on this machine (was {BETA}; the "
                         "rest of local.toml is kept):")
        self.assertIn(f'engine = "{other}"', res.stdout)
        self.assertIn("Restart the Claude Code session", res.stdout)
        self.assertEqual(local.read_bytes(), before.replace(BETA.encode(), other.encode()))
        out = self.run_beta().stdout
        self.assertIn(f"beta {other}\n", out)
        self.assertIn("engine: installed", out)
        self.assertIn("plugin: installed", out)
        self.assertIn("settings.local.json: beta on, stable off", out)
        # The same beta again: nothing changes, and it is no longer a switch.
        once = self.state()
        res = self.run_beta("on", other)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.splitlines()[0],
                         f"beta {other} on for this project, on this machine:")
        self.assertEqual(self.state(), once)

    def test_ub10_switch_then_off_restores_the_settings_from_before_the_first_beta(self):
        local = self._first_beta_on()
        other = self._second_beta()
        # Someone flipped the two keys by hand in between: the switch sets them again.
        wtext(self.settings, json.dumps({"enabledPlugins": {
            "context-gate-beta@skills-dir": False, "context-gate@context-gate": True}}, indent=2)
            + "\n")
        self.assertEqual(self.run_beta("on", other).returncode, 0)
        self.assertEqual(json.loads(self.settings.read_text(encoding="utf-8"))["enabledPlugins"],
                         {"context-gate-beta@skills-dir": True, "context-gate@context-gate": False})
        res = self.run_beta("off")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(f"beta {other} off", res.stdout)
        self.assertEqual(self.settings.read_text(encoding="utf-8"), self.STABLE_TRUE)
        self.assertEqual(local.read_bytes(), self.OWN.lstrip("\n").encode())

    def test_ub10_switch_keeps_crlf(self):
        text = (f'[governance]\r\nengine = "{BETA}"\r\n'
                'plugins_before = { "context-gate@context-gate" = true }\r\n'
                "\r\n# mine\r\n[checks.usage]\r\nenabled = true\r\n")
        local = self._first_beta_on(text)
        other = self._second_beta()
        self.assertEqual(self.run_beta("on", other).returncode, 0)
        after = local.read_bytes()
        self.assertEqual(after, text.replace(BETA, other).encode())
        self.assertNotIn(b"\n", after.replace(b"\r\n", b""))

    def test_ub10_switch_reads_the_engine_line_in_its_usual_spellings(self):
        for text in (f'[governance]\nengine="{BETA}"\n',
                     f"[ governance ]  # this machine\n\tengine = '{BETA}'   # trying it\n",
                     f'# top\n\n[governance]\nplugins_before = {{}}\nengine = "{BETA}"'):
            with self.subTest(text=text):
                local = self._first_beta_on(text)
                other = self._second_beta()
                res = self.run_beta("on", other)
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertEqual(local.read_bytes(), text.replace(BETA, other).encode())

    def test_ub10_engine_line_in_another_table_is_not_touched(self):
        text = (f'[checks.usage]\nengine = "{BETA}"\n\n'
                f'[governance]\nengine = "{BETA}"\n\n'
                f'[governance.notes]\nengine = "{BETA}"\n\n'
                f'[[checks.usage.alerts]]\nengine = "{BETA}"\n')
        local = self._first_beta_on(text)
        other = self._second_beta()
        self.assertEqual(self.run_beta("on", other).returncode, 0)
        self.assertEqual(local.read_text(encoding="utf-8"), text.replace(
            f'[governance]\nengine = "{BETA}"', f'[governance]\nengine = "{other}"'))

    def test_ub10_engine_line_the_edit_cannot_place_changes_nothing(self):
        forms = {
            "a multi-line string": f'[governance]\nengine = """{BETA}"""\n',
            "an inline table": f'governance = {{ engine = "{BETA}" }}\n',
            "a dotted key": f'governance.engine = "{BETA}"\n',
            "a quoted key": f'[governance]\n"engine" = "{BETA}"\n',
            "a quoted table name": f'["governance"]\nengine = "{BETA}"\n',
            "a second line that looks the same": (
                f'[governance]\nengine = "{BETA}"\nnote = """\nengine = "{BETA}"\n"""\n'),
            # The only line the edit finds is inside a string: the re-read catches it.
            "a look-alike inside a string": (
                f'governance.engine = "{BETA}"\n\n[checks.usage]\nnote = """\n'
                f'[governance]\nengine = "{BETA}"\n"""\n'),
        }
        for name, text in forms.items():
            with self.subTest(form=name):
                self._first_beta_on(text)
                other = self._second_beta()
                before = self.state()
                res = self.run_beta("on", other)
                self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
                self.assertIn("local.toml", res.stderr)
                self.assertIn(f'engine = "{other}"', res.stderr)
                self.assertIn("by hand", res.stderr)
                self.assertNotIn("Traceback", res.stderr)
                self.assertEqual(self.state(), before)
                self.assertIn(f"beta {BETA}\n", self.run_beta().stdout)

    def test_ub10_switch_to_a_beta_not_installed_changes_nothing(self):
        self._first_beta_on()
        other = f"{SERIES}.99-beta.2"
        before = self.state()
        res = self.run_beta("on", other)                 # no engine
        self.assertEqual(res.returncode, 2, res.stdout)
        self.assertIn("install-engine.py", res.stderr)
        self.assertEqual(self.state(), before)
        self._engines(SimpleNamespace(home=self.home), (other,))
        res = self.run_beta("on", other)                 # the local plugin is still BETA's
        self.assertEqual(res.returncode, 2, res.stdout)
        self.assertIn("install-plugin.py", res.stderr)
        self.assertEqual(self.state(), before)
        self.assertIn(f"beta {BETA}\n", self.run_beta().stdout)

    def test_ub10_failed_switch_leaves_local_toml(self):
        # The settings path cannot be written (a directory): local.toml, edited first, is put back.
        local = self._first_beta_on()
        before = local.read_bytes()
        other = self._second_beta()
        self.settings.unlink()
        self.settings.mkdir()
        res = self.run_beta("on", other)
        self.assertEqual(res.returncode, 2, res.stdout)
        self.assertIn("local.toml and .claude/settings.local.json are as they were", res.stderr)
        self.assertNotIn("nothing changed", res.stderr)
        self.assertEqual(local.read_bytes(), before)

    # Review of UB-10: the switch replaces local.toml whole, and a write it cannot make or undo
    # ends in one line, never a traceback.
    def test_ub10_switch_replaces_local_toml_rather_than_writing_in_place(self):
        """Written beside the file and moved over it, so a full disk or a kill mid-write leaves
        the old file whole: the file after the switch is a new one, and no temp file stays."""
        local = self._first_beta_on()
        other = self._second_beta()
        inode = local.stat().st_ino
        self.assertEqual(self.run_beta("on", other).returncode, 0)
        self.assertNotEqual(local.stat().st_ino, inode)
        self.assertEqual([p.name for p in local.parent.iterdir() if p.name.endswith(".tmp")], [])

    def test_ub10_switch_keeps_a_symlinked_local_toml_a_symlink(self):
        local = self._first_beta_on()
        other = self._second_beta()
        target = self.root / "elsewhere.toml"
        target.write_bytes(local.read_bytes())
        local.unlink()
        try:
            local.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("this system cannot make symlinks")
        self.assertEqual(self.run_beta("on", other).returncode, 0)
        self.assertTrue(local.is_symlink())
        self.assertIn(f'engine = "{other}"', target.read_text(encoding="utf-8"))

    @unittest.skipIf(os.name == "nt", "Windows has no mode bits to keep")
    def test_ub10_switch_keeps_the_mode_of_local_toml(self):
        local = self._first_beta_on()
        other = self._second_beta()
        local.chmod(0o600)
        self.assertEqual(self.run_beta("on", other).returncode, 0)
        self.assertEqual(local.stat().st_mode & 0o777, 0o600)

    def test_ub10_read_only_local_toml_is_refused_without_a_traceback(self):
        local = self._first_beta_on()
        before = local.read_bytes()
        other = self._second_beta()
        state = self.state()
        local.chmod(0o444)
        try:
            if os.access(local, os.W_OK):
                self.skipTest("this user writes a read-only file (root)")
            res = self.run_beta("on", other)
        finally:
            local.chmod(0o644)       # a read-only file would also stop Windows clearing the tmp
        self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
        self.assertNotIn("Traceback", res.stderr)
        self.assertEqual(len(res.stderr.strip().splitlines()), 1, res.stderr)
        self.assertIn(f"could not switch to beta {other}", res.stderr)
        self.assertIn("local.toml and .claude/settings.local.json are as they were", res.stderr)
        self.assertEqual(local.read_bytes(), before)
        self.assertEqual(self.state(), state)
        self.assertIn(f"beta {BETA}\n", self.run_beta().stdout)

    def test_ub10_local_toml_that_cannot_be_read_is_reported_as_that(self):
        local = self._first_beta_on()
        other = self._second_beta()
        state = self.state()
        local.chmod(0)
        try:
            if os.access(local, os.R_OK):
                self.skipTest("this user reads a file with no read bit (root, Windows)")
            res = self.run_beta("on", other)
        finally:
            local.chmod(0o644)
        self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
        self.assertNotIn("Traceback", res.stderr)
        self.assertIn("local.toml could not be read (", res.stderr)
        self.assertIn("nothing changed", res.stderr)
        self.assertNotIn("does not edit", res.stderr)
        self.assertNotIn("names no beta", res.stderr)
        self.assertEqual(self.state(), state)

    def test_off_that_cannot_remove_local_toml_puts_the_settings_back(self):
        """`beta off` rewrites the settings, then cannot remove local.toml (its directory is
        read-only), and cannot rewrite it either (so is the file): the settings still go back,
        and it ends in one line."""
        local = self._first_beta_on()
        gov = local.parent
        state = self.state()
        local.chmod(0o444)
        gov.chmod(0o555)
        try:
            if os.access(gov, os.W_OK):
                self.skipTest("a directory cannot be made read-only here (root, Windows)")
            res = self.run_beta("off")
        finally:
            gov.chmod(0o755)
            local.chmod(0o644)
        self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
        self.assertNotIn("Traceback", res.stderr)
        self.assertEqual(len(res.stderr.strip().splitlines()), 1, res.stderr)
        self.assertIn("could not switch the beta off (", res.stderr)
        self.assertIn("local.toml and .claude/settings.local.json are as they were", res.stderr)
        self.assertNotIn("nothing changed", res.stderr)
        self.assertEqual(self.state(), state)

    def test_a_file_that_cannot_be_put_back_is_named(self):
        """The failure after the failure: the message says which file is not as it was. No
        filesystem state reaches this from outside, so the entry point's functions are loaded
        without its dispatch and `_restore` is made to fail for one file."""
        probe = textwrap.dedent("""
            import sys
            from pathlib import Path
            entry, kept, lost = (Path(a) for a in sys.argv[1:])
            source = entry.read_text(encoding="utf-8")
            body, mark, _ = source.partition('\\nif Path(__file__).name == "upgrade":')
            assert mark, "the entry point's dispatch moved"
            ns = {"__file__": str(entry), "__name__": "entry"}
            exec(compile(body, str(entry), "exec"), ns)
            restore = ns["_restore"]
            def failing(saved):
                if lost in saved:
                    raise PermissionError(13, "Permission denied", str(lost))
                restore(saved)
            ns["_restore"] = failing
            ns["_put_back"]({kept: b"kept before", lost: b"lost before"}, "could not do it (why)")
        """)
        kept, lost = self.tmp / "kept.toml", self.tmp / "lost.json"
        kept.write_bytes(b"kept after")
        lost.write_bytes(b"lost after")
        res = subprocess.run([sys.executable, "-c", probe, str(self.entry), str(kept), str(lost)],
                             env=self.env, capture_output=True, text=True)
        self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
        self.assertNotIn("Traceback", res.stderr)
        self.assertEqual(res.stderr.strip(),
                         "error: could not do it (why); could not put lost.json back as before "
                         f"([Errno 13] Permission denied: {str(lost)!r}), so check by hand")
        self.assertEqual(kept.read_bytes(), b"kept before")      # the other file was still tried
        self.assertEqual(lost.read_bytes(), b"lost after")

    # `beta off` takes the beta out of local.toml and keeps the rest of the file.
    def _off_output(self, version: str = BETA, put_back: str = "", local: str = "removed") -> str:
        return (f"beta {version} off for this project, on this machine:\n"
                f"  .context-gate/local.toml          {local}\n" + put_back +
                "Back on the committed pin. Anything the beta installed outside this project "
                "(see its release\nnotes) is still installed. Restart the Claude Code session to "
                "load the stable plugin.\n")

    KEPT = "engine and plugins_before removed, the rest kept"

    def test_off_keeps_the_rest_of_local_toml_byte_for_byte(self):
        local = self._first_beta_on(f'[governance]\nengine = "{BETA}"\n\n[checks.usage]\n'
                                    'level = "warn"\n')
        res = self.run_beta("off")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(local.read_bytes(), b'[checks.usage]\nlevel = "warn"\n')
        self.assertEqual(res.stdout, self._off_output(
            put_back="  .claude/settings.local.json       context-gate-beta@skills-dir and "
                     "context-gate@context-gate put back\n", local=self.KEPT))
        self.assertNotIn("held settings", res.stdout)

    def test_off_keeps_a_table_before_and_after_the_governance_table(self):
        for eol in ("\n", "\r\n"):
            with self.subTest(eol=eol):
                head = '# mine\n[checks.usage]\nenabled = true\n\n'
                tail = '# about writing\n[checks.writing-rules]\nlevel = "error"\n'
                gov = f"[ governance ]  # this machine\nengine = '{BETA}'\nplugins_before = {{}}\n"
                local = self._first_beta_on((head + gov + tail).replace("\n", eol))
                res = self.run_beta("off")
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertEqual(local.read_bytes(), (head + tail).replace("\n", eol).encode())

    def test_off_keeps_a_governance_key_of_the_users_own(self):
        local = self._first_beta_on(f'[governance]\nsource = "x"\nengine = "{BETA}"\n'
                                    'plugins_before = {}\n[checks.usage]\nenabled = true\n')
        self.assertEqual(self.run_beta("off").returncode, 0)
        self.assertEqual(local.read_bytes(),
                         b'[governance]\nsource = "x"\n[checks.usage]\nenabled = true\n')

    def test_off_drops_a_governance_table_that_is_last(self):
        local = self._first_beta_on(f'[checks.usage]\nenabled = true\n\n[governance]\n'
                                    f'engine = "{BETA}"\n')
        self.assertEqual(self.run_beta("off").returncode, 0)
        self.assertEqual(local.read_bytes(), b"[checks.usage]\nenabled = true\n")

    def test_off_deletes_local_toml_holding_only_the_governance_table(self):
        for text in (f'[governance]\nengine = "{BETA}"\n',
                     f'# this machine only\n\n[governance]\nengine = "{BETA}"\n'
                     'plugins_before = {}\n\n# nothing else\n'):
            with self.subTest(text=text):
                local = self._first_beta_on(text)
                res = self.run_beta("off")
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertFalse(local.exists())
                self.assertEqual(res.stdout, self._off_output(
                    put_back="  .claude/settings.local.json       context-gate-beta@skills-dir "
                             "and context-gate@context-gate put back\n"))

    def test_off_leaves_a_file_it_cannot_parse_whole_and_exits_1(self):
        for name, raw in (("a broken table header",
                           f'[governance\nengine = "{BETA}"\n\n[checks.usage]\n'.encode()),
                          ("a byte-order mark",
                           b"\xef\xbb\xbf" + f'[governance]\nengine = "{BETA}"\n'.encode()),
                          ("not UTF-8", b'[governance]\nengine = "\xff"\n\n[checks.usage]\n'),
                          ("a header look-alike in a string",
                           f'[governance]\nengine = "{BETA}"\n\n[checks.usage]\nnote = \"\"\"\n'
                           '[governance]\nengine = "x"\n\"\"\"\n'.encode())):
            with self.subTest(form=name):
                local = self._first_beta_on("")
                local.write_bytes(raw)
                state = self.state()
                res = self.run_beta("off")
                self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
                self.assertNotIn("Traceback", res.stderr)
                self.assertIn("local.toml could not be edited", res.stderr)
                self.assertEqual(self.state(), state)

    def test_off_twice_leaves_the_settings_the_user_changed_since(self):
        local = self._first_beta_on()
        self.assertEqual(self.run_beta("off").returncode, 0)
        kept = local.read_bytes()
        wtext(self.settings, self.STABLE_TRUE)
        state = self.state()
        res = self.run_beta("off")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout, "no beta is on in this project\n")
        self.assertEqual(self.state(), state)
        self.assertEqual(local.read_bytes(), kept)

    # A newer beta installed beside the one that is on.
    def _newer_line(self, newer: str, running: str = BETA) -> str:
        return (f"context-gate: beta {newer} is installed (this project runs beta {running}): "
                f"govern beta on {newer}")

    def test_status_says_when_a_newer_beta_is_installed(self):
        self.assertEqual(self.run_beta("on", BETA).returncode, 0)
        self.assertNotIn("is installed (this project runs", self.run_beta().stdout)
        home = SimpleNamespace(home=self.home)
        self._engines(home, (f"{SERIES}.98-beta.5", f"{SERIES}.99-beta.9", NEXT_RELEASE))
        self.assertEqual(self.run_beta().stdout.splitlines()[-1],
                         self._newer_line(f"{SERIES}.99-beta.9"))
        self._engines(home, (f"{SERIES}.99-beta.10",))        # by number, not as a string
        self.assertEqual(self.run_beta().stdout.splitlines()[-1],
                         self._newer_line(f"{SERIES}.99-beta.10"))
        # A directory that only looks like an engine is not one.
        (layout.engines_dir(self.home) / f"{SERIES}.99-beta.11").mkdir()
        self.assertEqual(self.run_beta().stdout.splitlines()[-1],
                         self._newer_line(f"{SERIES}.99-beta.10"))

    def test_status_without_a_beta_never_mentions_an_installed_one(self):
        self._engines(SimpleNamespace(home=self.home), (f"{SERIES}.99-beta.2",))
        self.assertEqual(self.run_beta().stdout, "no beta in this project\n")

    # `beta on` with no version: the newest installed beta.
    def test_on_without_a_version_takes_the_newest_installed_beta(self):
        res = self.run_beta("on")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.splitlines()[0],
                         f"beta {BETA} on for this project, on this machine:")
        self.assertIn(f'engine = "{BETA}"', (self.root / layout.LOCAL).read_text(encoding="utf-8"))
        once = self.state()
        self.assertEqual(self.run_beta("on").returncode, 0)      # and again changes nothing
        self.assertEqual(self.state(), once)

    def test_on_without_a_version_orders_betas_by_number(self):
        nine, ten = f"{SERIES}.99-beta.9", f"{SERIES}.99-beta.10"
        self._engines(SimpleNamespace(home=self.home), (nine, ten, f"{SERIES}.98-beta.11"))
        (layout.engines_dir(self.home) / f"{SERIES}.99-beta.11").mkdir()    # no engine in it
        before = self.state()
        res = self.run_beta("on")            # the same checks as `beta on <ten>`: plugin is BETA's
        self.assertEqual(res.returncode, 2, res.stdout)
        self.assertIn(f"install-plugin.py v{ten}", res.stderr)
        self.assertEqual(self.state(), before)
        wtext(self.plugin_release, f"v{ten}\n")
        res = self.run_beta("on")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.splitlines()[0],
                         f"beta {ten} on for this project, on this machine:")

    def test_on_without_a_version_switches_from_an_older_beta(self):
        local = self._first_beta_on()
        other = self._second_beta()
        res = self.run_beta("on")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.splitlines()[0],
                         f"beta {other} on for this project, on this machine (was {BETA}; the "
                         "rest of local.toml is kept):")
        self.assertIn(self.OWN.encode(), local.read_bytes())

    def test_on_without_a_version_and_no_beta_installed(self):
        shutil.rmtree(layout.engines_dir(self.home) / BETA)
        for engines in (True, False):
            with self.subTest(engines_dir=engines):
                if not engines:
                    shutil.rmtree(layout.engines_dir(self.home))
                before = self.state()
                res = self.run_beta("on")
                self.assertEqual(res.returncode, 2, res.stdout)
                self.assertNotIn("Traceback", res.stderr)
                self.assertIn("no beta engine is installed", res.stderr)
                self.assertIn("from a clone that has the beta's tag, run: "
                              "python3 tools/release/install-engine.py vX.Y.Z-beta.N",
                              res.stderr)
                self.assertEqual(self.state(), before)

    def test_on_without_a_version_refuses_a_beta_of_a_release_already_run(self):
        """The newest installed beta is of the release the pin already runs (or an older one):
        nothing to move to. Naming it is still allowed."""
        shutil.rmtree(layout.engines_dir(self.home) / BETA)
        old = f"{__version__}-beta.4"
        self._engines(SimpleNamespace(home=self.home), (old, f"{PREV_SERIES}.9-beta.1"))
        wtext(self.plugin_release, f"v{old}\n")
        cfg = self.root / CFG
        exact = cfg.read_text(encoding="utf-8")
        for pin in (__version__, SERIES):
            with self.subTest(pin=pin):
                wtext(cfg, exact.replace(f'engine = "{__version__}"', f'engine = "{pin}"'))
                before = self.state()
                res = self.run_beta("on")
                self.assertEqual(res.returncode, 2, res.stdout)
                self.assertIn(f"the newest installed beta, {old}, is not newer than the release "
                              f"this project runs ({__version__})", res.stderr)
                self.assertIn(f"govern beta on {old}", res.stderr)
                self.assertEqual(self.state(), before)
        res = self.run_beta("on", old)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(f"beta {old} on for this project", res.stdout)

    def test_on_without_a_version_needs_a_pin_it_can_read(self):
        cfg = self.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace("[governance]", "[governance", 1))
        before = self.state()
        res = self.run_beta("on")
        self.assertEqual(res.returncode, 2, res.stdout)
        self.assertNotIn("Traceback", res.stderr)
        self.assertIn("cannot read the engine pin", res.stderr)
        self.assertEqual(self.state(), before)

    def test_on_without_a_version_says_a_beta_pin_is_not_a_release(self):
        cfg = self.root / CFG
        pin = f"{NEXT_RELEASE}-beta.1"
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            f'engine = "{__version__}"', f'engine = "{pin}"'))
        before = self.state()
        res = self.run_beta("on")
        self.assertEqual(res.returncode, 2, res.stdout)
        self.assertIn(f"engine pin {pin} is not a release", res.stderr)
        self.assertNotIn("has no [governance] engine pin", res.stderr)
        self.assertEqual(self.state(), before)

    def test_usage_line_names_the_optional_version(self):
        for args in (("on", BETA, "more"), ("off", "now"), ("nope",)):
            with self.subTest(args=args):
                res = self.run_beta(*args)
                self.assertEqual(res.returncode, 2)
                self.assertEqual(res.stderr.strip(),
                                 "error: usage: govern beta [on [X.Y.Z-beta.N] | off]")

    def test_off_twice_and_with_engine_gone(self):
        self.assertEqual(self.run_beta("on", BETA).returncode, 0)
        shutil.rmtree(layout.engines_dir(self.home))
        res = self.run_beta("off")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertFalse((self.root / layout.LOCAL).exists())
        self.assertFalse(self.settings.exists())
        res = self.run_beta("off")
        self.assertEqual(res.returncode, 0)
        self.assertIn("no beta is on", res.stdout)

    def test_refusals_change_nothing(self):
        other = f"{SERIES}.99-beta.2"
        self._engines(SimpleNamespace(home=self.home), (other,))
        cases = [
            (("on", NEXT_RELEASE), "not a beta", None),
            (("on", f"{SERIES}.98-beta.1"), "install-engine.py", None),
            (("on", other), "install-plugin.py", None),
            (("on", BETA, "more"), "usage", None),
            (("on", BETA), "names no beta (None): govern beta off first",
             lambda: wtext(self.root / layout.LOCAL, "[governance\n")),
            (("on", BETA), "is unreadable", lambda: (self.root / layout.LOCAL).write_bytes(b"\xff")),
            (("on", BETA), "not valid settings JSON",
             lambda: wtext(self.settings, '{"enabledPlugins": {"context-gate@context-gate": "x"}}')),
            (("on", BETA), "no .claude/ directory",
             lambda: (self.root / ".claude").rmdir()),
            (("on", BETA), "not valid settings JSON", lambda: wtext(self.settings, "[]")),
            (("on", BETA), "not valid settings JSON",
             lambda: wtext(self.settings, '{"enabledPlugins": []}')),
            (("on", BETA), "not valid settings JSON",
             lambda: self.settings.write_bytes(b'{"a": "\xff"}')),
            (("off",), "not valid settings JSON", lambda: (
                wtext(self.settings, "{not json"),
                wtext(self.root / layout.LOCAL, f'[governance]\nengine = "{BETA}"\n'))),
            (("on", BETA), "git cannot say", lambda: shutil.rmtree(self.root / ".git")),
        ]
        for args, words, arrange in cases:
            with self.subTest(args=args, words=words):
                for path in (self.root / layout.LOCAL, self.settings):
                    if path.is_file():
                        path.unlink()
                (self.root / ".claude").mkdir(exist_ok=True)
                if arrange:
                    arrange()
                before = self.state()
                res = self.run_beta(*args)
                self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
                self.assertIn(words, res.stderr)
                self.assertNotIn("Traceback", res.stderr)
                self.assertEqual(self.state(), before)

    def test_plugin_must_match(self):
        wtext(self.plugin_release, "v0.0.1-beta.1\n")
        res = self.run_beta("on", BETA)
        self.assertEqual(res.returncode, 2)
        self.assertIn("install-plugin.py", res.stderr)
        self.plugin_release.unlink()
        res = self.run_beta("on", BETA)
        self.assertEqual(res.returncode, 2)
        self.assertIn("install-plugin.py", res.stderr)

    def test_invalid_settings_is_never_rewritten(self):
        wtext(self.settings, "{not json")
        self.assertEqual(self.run_beta("on", BETA).returncode, 2)
        self.assertEqual(self.settings.read_text(encoding="utf-8"), "{not json")
        self.assertFalse((self.root / layout.LOCAL).exists())

    def test_failed_write_leaves_the_originals(self):
        # A settings path that cannot be written (here, a directory): local.toml, written
        # first, is taken back.
        self.settings.mkdir()
        res = self.run_beta("on", BETA)
        self.assertEqual(res.returncode, 2, res.stderr)
        self.assertIn("local.toml and .claude/settings.local.json are as they were", res.stderr)
        self.assertNotIn("nothing changed", res.stderr)
        self.assertFalse((self.root / layout.LOCAL).exists())

    def test_exclude_lands_in_the_enclosing_repo(self):
        # Review Focus 5: the project root is a subdirectory of the git repository.
        repo = self.root.parent
        shutil.rmtree(self.root / ".git")
        self.git("init", "-q", str(repo))
        res = self.run_beta("on", BETA)
        self.assertEqual(res.returncode, 0, res.stderr)
        rel = f"{self.root.name}/{layout.LOCAL}"
        res = self.git("-C", str(repo), "check-ignore", "-q", rel)
        self.assertEqual(res.returncode, 0)
        self.assertNotIn(".gitignore", {p.name for p in repo.iterdir()})
        self.assertNotIn(".gitignore", {p.name for p in self.root.iterdir()})

    def test_status(self):
        self.assertIn("no beta", self.run_beta().stdout)
        self.run_beta("on", BETA)
        out = self.run_beta().stdout
        self.assertIn(f"beta {BETA}", out)
        self.assertIn("engine: installed", out)
        self.assertIn("plugin: installed", out)
        self.assertIn("settings.local.json: beta on, stable off", out)
        wtext(self.plugin_release, f"v{SERIES}.99-beta.2\n")
        shutil.rmtree(layout.engines_dir(self.home) / BETA)
        out = self.run_beta().stdout
        self.assertIn("engine: missing", out)
        self.assertIn(f"plugin: is {SERIES}.99-beta.2", out)

    def test_status_says_when_both_plugins_load(self):
        """enabledPlugins merged as Claude Code merges it: user, then project, then local."""
        both = ("context-gate: both the beta and the stable plugin are enabled here, so every "
                f"hook runs twice; govern beta on {BETA} re-applies the switch")
        user, project = self.home / ".claude" / "settings.json", self.root / ".claude" / "settings.json"
        user.parent.mkdir(parents=True, exist_ok=True)
        wtext(user, '{"enabledPlugins": {"context-gate@context-gate": true}}\n')
        self.assertEqual(self.run_beta("on", BETA).returncode, 0)
        self.assertNotIn(both, self.run_beta().stdout)    # local's false wins over user's true
        data = json.loads(self.settings.read_text(encoding="utf-8"))
        del data["enabledPlugins"]["context-gate@context-gate"]
        wtext(self.settings, json.dumps(data))
        self.assertIn(both, self.run_beta().stdout)       # nothing above the user's true now
        wtext(project, '{"enabledPlugins": {"context-gate@context-gate": false}}\n')
        self.assertNotIn(both, self.run_beta().stdout)    # project's false over user's true
        wtext(project, "{not json")
        self.assertIn(both, self.run_beta().stdout)       # an unreadable file is skipped

    def test_prior_plugin_values_survive(self):
        own = {"enabledPlugins": {"context-gate@context-gate": True, "x@y": True}}
        wtext(self.settings, json.dumps(own, indent=2) + "\n")
        before = self.settings.read_bytes()
        self.assertEqual(self.run_beta("on", BETA).returncode, 0)
        self.assertIn('plugins_before = { "context-gate@context-gate" = true }',
                      (self.root / layout.LOCAL).read_text(encoding="utf-8"))
        self.assertEqual(self.run_beta("off").returncode, 0)
        self.assertEqual(self.settings.read_bytes(), before)

    def test_on_removes_the_id_an_older_launcher_wrote_and_off_puts_it_back(self):
        old = layout.OLD_SKILLS_DIR_PLUGIN_ID
        own = {"permissions": {"allow": ["Bash(ls)"]},
               "enabledPlugins": {"x@y": True, old: False, "context-gate@context-gate": True}}
        wtext(self.settings, json.dumps(own, indent=2) + "\n")
        before = self.settings.read_bytes()
        self.assertEqual(self.run_beta("on", BETA).returncode, 0)
        plugins = json.loads(self.settings.read_text(encoding="utf-8"))["enabledPlugins"]
        self.assertEqual(plugins, {"x@y": True, layout.SKILLS_DIR_PLUGIN_ID: True,
                                   "context-gate@context-gate": False})
        recorded = (self.root / layout.LOCAL).read_text(encoding="utf-8")
        self.assertIn(f'"{old}" = false', recorded)
        self.assertEqual(self.run_beta("off").returncode, 0)
        self.assertEqual(self.settings.read_bytes(), before)

    def test_the_marker_an_older_launcher_reads_is_not_the_plugin(self):
        """The folder a launcher older than the beta's own name reads holds a stamp only: this
        launcher looks for the plugin in the beta's folder."""
        self.plugin_release.unlink()
        marker = self.home / ".claude/skills/context-gate/govern/RELEASE"
        marker.parent.mkdir(parents=True)
        wtext(marker, f"v{BETA}\n")
        res = self.run_beta("on", BETA)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn(f"the local plugin is not {BETA}", res.stderr)
        self.assertFalse((self.root / layout.LOCAL).exists())

    def test_no_plugins_before_line_when_neither_key_existed(self):
        self.assertEqual(self.run_beta("on", BETA).returncode, 0)
        self.assertNotIn("plugins_before", (self.root / layout.LOCAL).read_text(encoding="utf-8"))

    def test_off_without_local_toml_touches_nothing(self):
        wtext(self.settings, '{"enabledPlugins": {"context-gate@context-gate": true}}\n')
        before = self.state()
        res = self.run_beta("off")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("no beta is on in this project", res.stdout)
        self.assertEqual(self.state(), before)

    def test_on_makes_git_ignore_the_settings_file(self):
        self.assertEqual(self.run_beta("on", BETA).returncode, 0)
        status = self.git("-C", str(self.root), "status", "--porcelain", "-uall").stdout
        self.assertNotIn(".claude/", status)

    def _fake_git(self, body: str) -> dict:
        real = shutil.which("git")
        bin_dir = self.tmp / "fakebin"
        bin_dir.mkdir()
        script = bin_dir / "git"
        wtext(script, f"#!{sys.executable}\nimport os, sys\nREAL = {real!r}\n{body}\n")
        script.chmod(0o755)
        return {**self.env, "PATH": f"{bin_dir}{os.pathsep}{self.env['PATH']}"}

    @unittest.skipIf(os.name == "nt", "the fake git is a shebang script, which Windows does not run from PATH")
    def test_exclude_survives_a_git_that_echoes_unknown_flags(self):
        # git < 2.31 prints an unknown rev-parse flag back and exits 0.
        env = self._fake_git(
            "args = sys.argv[1:]\n"
            "if '--path-format=absolute' in args:\n"
            "    print('--path-format=absolute')\n"
            "    args.remove('--path-format=absolute')\n"
            "os.execv(REAL, [REAL, *args])")
        res = self.run_beta("on", BETA, env=env)
        self.assertEqual(res.returncode, 0, res.stderr)
        exclude = (self.root / ".git" / "info" / "exclude").read_text(encoding="utf-8")
        self.assertIn(f"/{layout.LOCAL}", exclude)
        self.assertEqual(self.check_ignored(), 0)

    @unittest.skipIf(os.name == "nt", "the fake git is a shebang script, which Windows does not run from PATH")
    def test_exclude_outside_the_git_dir_is_refused(self):
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        env = self._fake_git(
            "args = sys.argv[1:]\n"
            "if '--git-path' in args:\n"
            f"    print({str(elsewhere / 'exclude')!r})\n"
            "    sys.exit(0)\n"
            "os.execv(REAL, [REAL, *args])")
        before = self.state()
        res = self.run_beta("on", BETA, env=env)
        self.assertEqual(res.returncode, 2, res.stdout)
        self.assertIn("which is not inside", res.stderr)
        self.assertEqual(self.state(), before)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_uninstall_does_not_take_beta(self):
        # Only the gate dispatches `beta`: bin/uninstall hands its argv to the installer.
        uninstall = self.root / layout.GOV_DIR / "bin" / "uninstall"
        res = subprocess.run([sys.executable, str(uninstall), "beta"], env=self.env,
                             capture_output=True, text=True, cwd=self.root)
        self.assertNotIn("no beta", res.stdout + res.stderr)


if __name__ == "__main__":
    unittest.main()
