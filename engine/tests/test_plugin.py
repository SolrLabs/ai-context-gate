"""The Claude Code plugin: its session-start hook, the notice that points at its skill, and the
rule that plugin and engine release as one version."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parent.parent
REPO = ENGINE.parent
PLUGIN = REPO / "plugin"
sys.path.insert(0, str(ENGINE))

from govern import __version__, layout, notice  # noqa: E402

BETA = "9.9.0-beta.1"
BOTH = ("context-gate: both the beta and the stable plugin are enabled here, so every hook "
        f"runs twice; govern beta on {BETA} re-applies the switch")


class Plugin(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.project = self.tmp / "project"
        self.home.mkdir()
        (self.project / layout.GOV_DIR).mkdir(parents=True)
        self.pin(__version__)
        # The plugin as install-plugin assembles it: plugin/ plus the engine.
        self.built = self.tmp / "built"
        shutil.copytree(PLUGIN, self.built)
        shutil.copytree(ENGINE / "govern", self.built / "govern",
                        ignore=shutil.ignore_patterns("__pycache__"))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def pin(self, version: str) -> None:
        (self.project / layout.CONFIG).write_text(
            f'[governance]\nengine = "{version}"\nschema = 1\n', encoding="utf-8")

    def hook(self, project: Path) -> str:
        env = {**os.environ, "HOME": str(self.home), "CLAUDE_PROJECT_DIR": str(project),
               "CONTEXT_GATE_NO_UPDATE_CHECK": "1"}
        res = subprocess.run([sys.executable, str(self.built / "hooks" / "session_start.py")],
                             input="{}", env=env, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        return res.stdout

    def install_engine(self, version: str) -> None:
        shutil.copytree(ENGINE / "govern", layout.engines_dir(self.home) / version / "govern",
                        ignore=shutil.ignore_patterns("__pycache__"))

    def test_plugin_and_engine_are_one_version(self):
        manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
        self.assertEqual(manifest["version"], __version__)

    @unittest.skipUnless((REPO / ".claude-plugin" / "marketplace.json").is_file(),
                         "no .claude-plugin/marketplace.json in this checkout")
    def test_marketplace_lists_this_version(self):
        market = json.loads((REPO / ".claude-plugin" / "marketplace.json").read_text())
        self.assertEqual(market["plugins"][0]["version"], __version__)

    def test_upgrade_skill_is_visible_to_the_agent(self):
        """The notice tells the agent to use the skill, so the agent must be able to see it:
        a user-only skill (disable-model-invocation) is hidden from the model entirely."""
        head = (PLUGIN / "skills" / "upgrade" / "SKILL.md").read_text().split("---")[1]
        self.assertNotIn("disable-model-invocation", head)
        self.assertIn("Never start it unprompted", head)

    def test_options_skill_is_visible_to_the_agent(self):
        text = (PLUGIN / "skills/options/SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\nname: options\n"))
        self.assertIn("govern options --json", text)
        self.assertIn("Chat about this", text)
        # Windows has no shebang: every call runs through python3, and the grant is no wider
        # than the commands the skill actually needs.
        self.assertIsNone(re.search(r"(?<!python3 )\.context-gate/bin/govern", text))
        self.assertNotIn("git -C:*", text)

    def test_adopt_skill_runs_usage_install_after_apply(self):
        """`govern usage install` writes into `.claude/settings.json` and needs
        `.context-gate/bin/`, which only exists once `## 4. Apply` has run: the instruction
        must come after it, not in `## 3a. Options`, which runs first."""
        text = (PLUGIN / "skills" / "adopt" / "SKILL.md").read_text(encoding="utf-8")
        apply_at = text.index("## 4. Apply")
        install_at = text.index("govern usage install")
        self.assertGreater(install_at, apply_at)

    def test_hook_is_silent_outside_a_governed_project(self):
        self.assertEqual(self.hook(self.tmp), "")

    def test_hook_is_silent_when_current(self):
        self.assertEqual(self.hook(self.project), "")

    def test_hook_announces_a_newer_engine_to_session_and_person(self):
        self.install_engine("9.9.9")
        out = json.loads(self.hook(self.project))
        self.assertIn("context-gate 9.9.9 available", out["systemMessage"])
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertEqual(out["hookSpecificOutput"]["additionalContext"], out["systemMessage"])

    def test_notice_names_the_skill_when_the_plugin_is_present(self):
        self.install_engine("9.9.9")
        self.assertIn("Use python3 .context-gate/bin/upgrade to upgrade",
                      notice.for_project(self.project, self.home))
        (self.home / ".claude" / "skills" / "context-gate" / ".claude-plugin").mkdir(
            parents=True)
        (self.home / ".claude" / "skills" / "context-gate" / ".claude-plugin"
         / "plugin.json").write_text("{}")
        self.assertIn("Use /context-gate:upgrade to upgrade",
                      notice.for_project(self.project, self.home))

    def settings(self, scope: str, plugins) -> None:
        """Write enabledPlugins at one scope: user, project or local."""
        path = {"user": self.home / ".claude" / "settings.json",
                "project": self.project / ".claude" / "settings.json",
                "local": self.project / ".claude" / "settings.local.json"}[scope]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(plugins if isinstance(plugins, str)
                        else json.dumps({"enabledPlugins": plugins}), encoding="utf-8")

    def test_enabled_plugins_merge_user_then_project_then_local(self):
        self.settings("user", {"a": True})
        self.settings("project", {"a": True, "b": True})
        self.settings("local", {"a": False})
        self.assertEqual(notice.enabled_plugins(self.project, self.home), {"a": False, "b": True})

    def test_enabled_plugins_skips_an_unreadable_file(self):
        self.settings("user", {"a": True})
        self.settings("project", "{not json")
        (self.project / ".claude" / "settings.local.json").write_bytes(b'{"enabledPlugins": \xff}')
        self.assertEqual(notice.enabled_plugins(self.project, self.home), {"a": True})
        self.settings("local", '\ufeff{"enabledPlugins": {"a": false, "c": "yes"}}')
        self.assertEqual(notice.enabled_plugins(self.project, self.home), {"a": False})

    def test_both_plugins_warning_only_from_a_beta_with_both_enabled(self):
        self.settings("user", {layout.PLUGIN_ID: True})
        self.settings("local", {layout.SKILLS_DIR_PLUGIN_ID: True})
        with mock.patch.object(notice, "__version__", BETA):
            self.assertEqual(notice.both_plugins_warning(self.project, self.home), BOTH)
            # Local settings win: the stable plugin switched off here is off.
            self.settings("local", {layout.SKILLS_DIR_PLUGIN_ID: True, layout.PLUGIN_ID: False})
            self.assertIsNone(notice.both_plugins_warning(self.project, self.home))
            self.settings("local", {layout.PLUGIN_ID: True})
            self.assertIsNone(notice.both_plugins_warning(self.project, self.home))
        self.settings("local", {layout.SKILLS_DIR_PLUGIN_ID: True})
        with mock.patch.object(notice, "__version__", "9.9.0"):
            self.assertIsNone(notice.both_plugins_warning(self.project, self.home))

    def beta_plugin(self, version: str = BETA) -> None:
        """The built plugin as a beta's: its bundled engine says it is that beta."""
        (self.built / "govern" / "__init__.py").write_text(
            (ENGINE / "govern" / "__init__.py").read_text(encoding="utf-8").replace(
                f'__version__ = "{__version__}"', f'__version__ = "{version}"'), encoding="utf-8")

    def local(self, text: str) -> None:
        (self.project / layout.LOCAL).write_text(text, encoding="utf-8")

    def test_newer_beta_plugin_tells_a_project_still_on_an_older_beta(self):
        """One local beta plugin per machine: with N+1 installed, a project whose local.toml
        still names N runs N's engine at the gate under N+1's hooks, and the session says so."""
        newer = "9.9.0-beta.10"
        self.beta_plugin(newer)
        for version in (BETA, "9.9.0-beta.9", newer):
            self.install_engine(version)
        self.local(f'[governance]\nengine = "{BETA}"\n')
        said = (f"context-gate: beta {newer} is installed (this project runs beta {BETA}): "
                f"govern beta on {newer}")
        out = json.loads(self.hook(self.project))
        self.assertEqual(out["systemMessage"], said)
        self.assertEqual(out["hookSpecificOutput"]["additionalContext"], said)
        self.local(f'[governance]\nengine = "{newer}"\n')        # switched: nothing to say
        self.assertEqual(self.hook(self.project), "")

    def test_a_release_above_the_beta_is_said_before_a_newer_beta(self):
        self.beta_plugin("9.9.0-beta.2")
        for version in (BETA, "9.9.0-beta.2", "9.9.0"):
            self.install_engine(version)
        self.local(f'[governance]\nengine = "{BETA}"\n')
        out = json.loads(self.hook(self.project))
        self.assertIn(f"context-gate 9.9.0 is out (this machine runs beta {BETA})",
                      out["systemMessage"])
        self.assertNotIn("is installed", out["systemMessage"])

    def test_a_stable_project_is_never_told_of_an_installed_beta(self):
        """No local.toml: silent from the stable plugin and from a beta's alike."""
        for version in (BETA, "9.9.0-beta.2"):
            self.install_engine(version)
        self.assertEqual(self.hook(self.project), "")
        self.beta_plugin("9.9.0-beta.2")
        self.assertEqual(self.hook(self.project), "")

    def test_beta_hook_says_when_both_plugins_load(self):
        """Review focus 3: the stable plugin re-enabled by hand beside the beta."""
        self.beta_plugin()
        self.settings("local", {layout.SKILLS_DIR_PLUGIN_ID: True, layout.PLUGIN_ID: False})
        self.assertEqual(self.hook(self.project), "")
        self.settings("project", {layout.PLUGIN_ID: True})
        self.settings("local", {layout.SKILLS_DIR_PLUGIN_ID: True})
        out = json.loads(self.hook(self.project))
        self.assertEqual(out["systemMessage"], BOTH)
        self.assertEqual(out["hookSpecificOutput"]["additionalContext"], BOTH)

    def test_a_local_install_off_by_default_is_not_available(self):
        manifest = self.home / ".claude" / "skills" / "context-gate" / ".claude-plugin" / "plugin.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text('{"defaultEnabled": false}', encoding="utf-8")
        self.assertFalse(notice.plugin_available(self.project, self.home))
        self.settings("local", {layout.SKILLS_DIR_PLUGIN_ID: True})
        self.assertTrue(notice.plugin_available(self.project, self.home))
        manifest.write_text("{}", encoding="utf-8")
        self.settings("local", {layout.SKILLS_DIR_PLUGIN_ID: False})
        self.assertFalse(notice.plugin_available(self.project, self.home))

    def test_a_plugin_switched_off_below_the_user_level_is_not_available(self):
        """User-level on, project- or local-level off: the merge says off, so the notice names
        `bin/upgrade`, not the plugin's skill."""
        for scope in ("project", "local"):
            self.settings("user", {layout.PLUGIN_ID: True})
            self.settings("project", {})
            self.settings("local", {})
            self.assertEqual(notice.upgrade_command(self.project, self.home),
                             f"/{notice.PLUGIN}:upgrade")
            self.settings(scope, {layout.PLUGIN_ID: False})
            self.assertEqual(notice.upgrade_command(self.project, self.home),
                             f"python3 {layout.GOV_DIR}/bin/upgrade", scope)

    def test_the_repository_plugin_is_on_by_default(self):
        """Only the installed copy is off by default (install-plugin.py writes it): the
        marketplace plugin, built from this file, loads where it is installed."""
        manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertNotIn("defaultEnabled", manifest)

    def test_series_pin_names_the_engine_it_actually_runs(self):
        self.pin("0.1")
        self.install_engine("0.1.2")
        msg = notice.for_project(self.project, self.home)
        self.assertIn("to pin 0.1.2 exactly", msg)

    def test_hook_survives_a_python_without_tomllib(self):
        """`_mentions_usage`'s `tomllib` import must be lazy, inside its own try, not a
        top-level `import`: on an interpreter that predates tomllib (3.10 or earlier — the
        hook is not held to the engine's 3.11+ floor), it must fail silently, not print a
        traceback to stderr."""
        fake = self.tmp / "fake_stdlib"
        fake.mkdir()
        (fake / "tomllib.py").write_text(
            "raise ImportError('no tomllib on this interpreter (simulated)')\n",
            encoding="utf-8")
        env = {**os.environ, "HOME": str(self.home), "CLAUDE_PROJECT_DIR": str(self.project),
               "CONTEXT_GATE_NO_UPDATE_CHECK": "1", "PYTHONPATH": str(fake)}
        res = subprocess.run(
            [sys.executable, str(self.built / "hooks" / "session_start.py")],
            input=json.dumps({"session_id": "S", "hook_event_name": "SessionStart"}),
            env=env, capture_output=True, text=True)
        self.assertEqual((res.returncode, res.stderr), (0, ""))


class UsageHooks(Plugin):
    ON = '\n[checks.usage]\nlevel = "error"\n'
    ALERT = '[[alert]]\nsignal = "context"\nat = 5\nsay = "ctx at {pct}"\n'

    def setUp(self) -> None:
        super().setUp()
        cfg = self.project / layout.CONFIG
        cfg.write_text(cfg.read_text(encoding="utf-8") + self.ON, encoding="utf-8")
        (self.project / layout.GOV_DIR / "usage-alerts.toml").write_text(self.ALERT, encoding="utf-8")

    def run_hook(self, script: str, payload: dict) -> str:
        env = {**os.environ, "HOME": str(self.home), "CLAUDE_PROJECT_DIR": str(self.project),
               "CONTEXT_GATE_NO_UPDATE_CHECK": "1"}
        res = subprocess.run([sys.executable, str(self.built / "hooks" / script)],
                             input=json.dumps(payload), env=env, capture_output=True, text=True)
        self.assertEqual((res.returncode, res.stderr), (0, ""))
        return res.stdout

    def snapshot(self, ctx: float) -> None:
        from govern import usage
        import time
        usage.write_json(usage.snapshot_path(self.home, "S"),
                         {"context_window": {"used_percentage": ctx}, "rate_limits": {},
                          "transcript_path": "t", "captured_at": int(time.time())})

    def start(self) -> str:
        return self.run_hook("session_start.py", {"session_id": "S", "hook_event_name": "SessionStart"})

    def call(self, **extra) -> str:
        return self.run_hook("usage.py", {"session_id": "S", "hook_event_name": "PostToolUse", **extra})

    def test_orchestrator_gets_data_and_alert(self):
        self.start()
        self.snapshot(7)
        out = json.loads(self.call())["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "PostToolUse")
        self.assertIn("usage: context 7%", out["additionalContext"])
        self.assertTrue(out["additionalContext"].startswith(
            "⚠ usage alert (owner's prompt, .context-gate/usage-alerts.toml):\nctx at 7\n\nusage: context 7%"),
            out["additionalContext"])
        self.assertEqual(self.call(), "")

    def test_ub12_alerts_edited_mid_session_load_without_a_restart(self):
        self.start()
        self.snapshot(7)
        self.call()
        alerts = self.project / layout.GOV_DIR / "usage-alerts.toml"
        alerts.write_text(self.ALERT.replace("at = 5", "at = 8").replace("ctx at", "now at"),
                          encoding="utf-8")
        later = alerts.stat().st_mtime_ns - 10_000_000_000    # settled, and not the first one's mtime
        os.utime(alerts, ns=(later, later))
        self.snapshot(9)
        out = json.loads(self.call())["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(out.startswith("⚠ usage alert (owner's prompt, .context-gate/usage-alerts.toml):"
                                       "\nnow at 9\n\nusage: context 9%"), out)

    def test_subagent_gets_nothing(self):
        self.start()
        self.snapshot(7)
        self.assertEqual(self.call(agent_id="a1", agent_type="general-purpose"), "")

    def test_pin_mismatch_still_resolves_usage(self):
        """Resolving the option must not depend on the project's own engine pin: most projects
        sit pinned to an older version between upgrades, and usage must not go silently off for
        all of them. Only `config.load`'s pin check is skipped for the resolver — the level
        cascade (profile, then project) still runs as normal."""
        cfg = self.project / layout.CONFIG
        cfg.write_text(cfg.read_text(encoding="utf-8").replace(
            f'engine = "{__version__}"', 'engine = "0.0.1"'), encoding="utf-8")
        self.start()
        self.snapshot(7)
        out = json.loads(self.call())["hookSpecificOutput"]
        self.assertIn("usage: context 7%", out["additionalContext"])

    def test_option_off_gets_nothing(self):
        (self.project / layout.CONFIG).write_text(
            (self.project / layout.CONFIG).read_text(encoding="utf-8").replace(self.ON, ""),
            encoding="utf-8")
        self.start()
        self.snapshot(7)
        self.assertEqual(self.call(), "")

    def test_option_off_writes_no_resolved_file_and_the_hook_stays_silent(self):
        """A project that still mentions `usage` but sets it off: SessionStart writes no
        `<sid>.resolved.json` (and clears a stale one), so the per-call hook can exit on a file
        check alone, before it ever imports `govern`."""
        cfg = self.project / layout.CONFIG
        cfg.write_text(cfg.read_text(encoding="utf-8").replace('level = "error"', 'level = "off"'),
                       encoding="utf-8")
        self.start()
        from govern import usage
        self.assertIsNone(usage.read_json(usage.resolved_path(self.home, "S")))
        log = usage.log_path(self.home)
        self.assertFalse(log.exists() and "resolution failed" in log.read_text(encoding="utf-8"))
        self.snapshot(7)
        self.assertEqual(self.call(), "")

    def test_session_end_cleans_up(self):
        from govern import usage
        self.start()
        self.snapshot(7)
        self.call()
        self.run_hook("usage.py", {"session_id": "S", "hook_event_name": "SessionEnd"})
        self.assertEqual(list(usage.state_dir(self.home).glob("S.*")), [])

    def test_bad_stdin_is_silent(self):
        env = {**os.environ, "HOME": str(self.home), "CLAUDE_PROJECT_DIR": str(self.project)}
        res = subprocess.run([sys.executable, str(self.built / "hooks" / "usage.py")],
                             input="not json", env=env, capture_output=True, text=True)
        self.assertEqual((res.returncode, res.stdout, res.stderr), (0, "", ""))

    def test_session_start_rewraps_a_replaced_statusline(self):
        from govern import usage_setup
        settings = self.home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text('{"statusLine": {"type": "command", "command": "mine"}}', encoding="utf-8")
        usage_setup.install(self.home)
        settings.write_text('{"statusLine": {"type": "command", "command": "other"}}', encoding="utf-8")
        out = json.loads(self.start())
        self.assertIn("re-wrapped", out["systemMessage"])

    def test_hooks_json_registers_the_events(self):
        hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
        for event in ("PostToolUse", "UserPromptSubmit", "SessionEnd"):
            cmd = hooks[event][0]["hooks"][0]["command"]
            self.assertIn("hooks/usage.py", cmd)

    def test_usage_hook_reads_non_ascii_stdin_as_utf8(self):
        """sys.stdin.read() decodes with the locale codec (cp1252 on Windows); a payload with
        non-ASCII text must still be read correctly. PYTHONIOENCODING=cp1252 makes this host
        behave like that even though its own locale is UTF-8."""
        self.start()
        self.snapshot(7)
        payload = json.dumps(
            {"session_id": "S", "hook_event_name": "PostToolUse",
             "tool_input": {"text": "тест 测试"}, "cwd": "/tmp/тест"},
            ensure_ascii=False).encode("utf-8")
        env = {**os.environ, "HOME": str(self.home), "CLAUDE_PROJECT_DIR": str(self.project),
               "CONTEXT_GATE_NO_UPDATE_CHECK": "1", "PYTHONIOENCODING": "cp1252"}
        res = subprocess.run([sys.executable, str(self.built / "hooks" / "usage.py")],
                             input=payload, env=env, capture_output=True)
        self.assertEqual((res.returncode, res.stderr), (0, b""))
        out = json.loads(res.stdout)["hookSpecificOutput"]
        self.assertIn("usage: context 7%", out["additionalContext"])

    def test_session_start_reads_non_ascii_stdin_as_utf8(self):
        payload = json.dumps(
            {"session_id": "S", "hook_event_name": "SessionStart", "cwd": "/tmp/тест"},
            ensure_ascii=False).encode("utf-8")
        env = {**os.environ, "HOME": str(self.home), "CLAUDE_PROJECT_DIR": str(self.project),
               "CONTEXT_GATE_NO_UPDATE_CHECK": "1", "PYTHONIOENCODING": "cp1252"}
        res = subprocess.run([sys.executable, str(self.built / "hooks" / "session_start.py")],
                             input=payload, env=env, capture_output=True)
        self.assertEqual((res.returncode, res.stderr), (0, b""))
        from govern import usage
        self.assertIsNotNone(usage.read_json(usage.resolved_path(self.home, "S")))

    def test_a_broken_extension_does_not_cost_the_upgrade_notice(self):
        """A project's own extension can print to stdout or exit, and a profile fetch has no
        timeout of its own: resolving the usage option must not run any of that in the process
        that still has to print the upgrade notice."""
        cfg = self.project / layout.CONFIG
        cfg.write_text(
            cfg.read_text(encoding="utf-8").replace("schema = 1", 'schema = 1\nextensions = ["ext"]'),
            encoding="utf-8")
        (self.project / "ext").mkdir()
        (self.project / "ext" / "broken.py").write_text(
            "import sys\nprint('side effect on stdout')\nsys.exit(1)\n", encoding="utf-8")
        self.install_engine("9.9.9")
        out = json.loads(self.start())
        self.assertIn("9.9.9 available", out["systemMessage"])

    def add_extension(self, code: str) -> None:
        cfg = self.project / layout.CONFIG
        cfg.write_text(
            cfg.read_text(encoding="utf-8").replace("schema = 1", 'schema = 1\nextensions = ["ext"]'),
            encoding="utf-8")
        (self.project / "ext").mkdir()
        (self.project / "ext" / "broken.py").write_text(code, encoding="utf-8")

    def test_extension_exception_is_a_logged_failure_not_off(self):
        """An extension that raises during `config.load` must not read as the option being
        off: the resolve child must exit non-zero, so the missing resolved file is logged as
        a resolution failure — the upgrade notice still survives it."""
        self.add_extension("raise RuntimeError('boom')\n")
        self.install_engine("9.9.9")
        out = json.loads(self.start())
        self.assertIn("9.9.9 available", out["systemMessage"])
        from govern import usage
        log = usage.log_path(self.home).read_text(encoding="utf-8")
        self.assertEqual(log.count("resolution failed"), 1)

    def test_extension_sys_exit_zero_is_a_logged_failure_not_off(self):
        """`sys.exit(0)` raised by a project's own extension must not be mistaken for the
        resolve child's own success: `SystemExit` (any code) during resolution is a failure
        like any other, not the option being off."""
        self.add_extension("import sys\nsys.exit(0)\n")
        self.install_engine("9.9.9")
        out = json.loads(self.start())
        self.assertIn("9.9.9 available", out["systemMessage"])
        from govern import usage
        log = usage.log_path(self.home).read_text(encoding="utf-8")
        self.assertEqual(log.count("resolution failed"), 1)


class UsageResolveSkipped(Plugin):
    """A project's config that never mentions `usage` and names no profile cannot turn the
    option on, so `SessionStart` must not pay for the resolve child at all."""

    def start_with_session(self, sid: str = "S") -> str:
        env = {**os.environ, "HOME": str(self.home), "CLAUDE_PROJECT_DIR": str(self.project),
               "CONTEXT_GATE_NO_UPDATE_CHECK": "1"}
        res = subprocess.run([sys.executable, str(self.built / "hooks" / "session_start.py")],
                             input=json.dumps({"session_id": sid, "hook_event_name": "SessionStart"}),
                             env=env, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        return res.stdout

    def test_project_without_usage_mention_or_profile_gets_no_child(self):
        """A config that would make the resolve child fail loudly if it ran (a broken
        extension, which `config.load` would import) still logs nothing: proof the child was
        never spawned, not that it happened to run and find nothing wrong."""
        cfg = self.project / layout.CONFIG
        cfg.write_text(
            cfg.read_text(encoding="utf-8").replace("schema = 1", 'schema = 1\nextensions = ["ext"]'),
            encoding="utf-8")
        (self.project / "ext").mkdir()
        (self.project / "ext" / "broken.py").write_text("raise RuntimeError('boom')\n",
                                                         encoding="utf-8")
        self.start_with_session()
        from govern import usage
        self.assertIsNone(usage.read_json(usage.resolved_path(self.home, "S")))
        log = usage.log_path(self.home)
        self.assertFalse(log.exists() and "resolution failed" in log.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
