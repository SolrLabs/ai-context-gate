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

    def test_plan_skill_scopes_senior_units(self):
        text = (PLUGIN / "skills/plan/SKILL.md").read_text(encoding="utf-8")
        self.assertIn("holds only the work that sends it there", text)

    def test_options_skill_says_roles_need_a_new_session(self):
        text = (PLUGIN / "skills/options/SKILL.md").read_text(encoding="utf-8")
        self.assertIn("can be launched only from a new session", " ".join(text.split()))

    def test_plan_skill_closes_planning_as_a_phase(self):
        text = (PLUGIN / "skills/plan/SKILL.md").read_text(encoding="utf-8")
        self.assertIn("Planning is a phase of its own", text)
        self.assertIn("\n\nPlanning is a phase of its own", text)
        self.assertLess(text.index("Planning is a phase of its own"),
                        text.index("Never start it yourself"))

    def build_text(self) -> str:
        text = (PLUGIN / "skills/build/SKILL.md").read_text(encoding="utf-8")
        return " ".join(text.split())     # a re-wrapped line must not fail a phrase

    def test_plan_skill_marks_owner_checkpoints(self):
        text = " ".join((PLUGIN / "skills/plan/SKILL.md").read_text(encoding="utf-8").split())
        self.assertIn("Mark the owner checkpoints", text)
        self.assertIn("checkpoint: yes|no", text)
        self.assertIn("(a checkpoint phase only)", text)
        self.assertNotIn("The first phase of anything new", text)

    def test_build_skill_carries_on_by_default(self):
        text = self.build_text()
        self.assertIn("## Carrying on", text)
        self.assertIn("`--one` stops when this phase is closed", text)
        self.assertIn("do not stop for the owner", text)
        self.assertNotIn("A long run", text)

    def test_build_skill_checks_roles_are_launchable_before_the_branch(self):
        text = self.build_text()
        self.assertIn("among the agent types this session can launch", text)
        self.assertLess(text.index("among the agent types this session can launch"),
                        text.index("Cut the phase branch"))

    def test_build_skill_never_briefs_a_beta_only_command(self):
        self.assertIn("runs the project's committed pin", self.build_text())

    def test_build_skill_runs_the_gate_without_moving_the_shell(self):
        text = self.build_text()
        self.assertIn("without moving your shell", text)
        self.assertIn("Never `cd <worktree> && <gate>`", text)

    def test_build_skill_reports_before_phase_close(self):
        text = self.build_text()
        close = text[text.index("## 5. Close"):]
        self.assertLess(close.index("run report"), close.index("govern phase close`. It pushes"))

    def test_adopt_skill_runs_usage_install_after_apply(self):
        """`govern usage install` writes into `.claude/settings.json` and needs
        `.context-gate/bin/`, which only exists once `## 4. Apply` has run: the instruction
        must come after it, not in `## 3a. Options`, which runs first."""
        text = (PLUGIN / "skills" / "adopt" / "SKILL.md").read_text(encoding="utf-8")
        apply_at = text.index("## 4. Apply")
        install_at = text.index("govern usage install")
        self.assertGreater(install_at, apply_at)

    def test_adopt_skill_renders_the_roles_after_apply_and_checks_again(self):
        """`govern agents render` needs `.context-gate/bin/`, which only exists once
        `## 4. Apply` has run, and the gate that apply ran saw no rendered role: the skill
        renders, then runs the gate again, in that order."""
        text = (PLUGIN / "skills" / "adopt" / "SKILL.md").read_text(encoding="utf-8")
        step = text.index("if the answer for `agent-roles` was On")
        render_at = text.index("python3 .context-gate/bin/govern agents render")
        check_at = text.index("python3 .context-gate/bin/govern check", render_at)
        self.assertGreater(step, text.index("## 5. Hand back"))
        self.assertGreater(render_at, step)
        self.assertLess(check_at, text.index("\n- ", render_at))    # in the same step

    def test_options_skill_renders_the_roles_once_the_option_is_written(self):
        """`govern agents render` writes nothing while the option is off, so the instruction
        sits in `## 4. Write`, after the table is written and before `## 5. Prove` runs the
        gate, which would otherwise report every role as not rendered."""
        text = (PLUGIN / "skills" / "options" / "SKILL.md").read_text(encoding="utf-8")
        render_at = text.index("python3 .context-gate/bin/govern agents render")
        self.assertIn("When `agent-roles` is turned on", text)
        self.assertGreater(render_at, text.index("- On: `[checks.<id>]`"))
        self.assertLess(render_at, text.index("## 5. Prove"))

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
        manifest = (self.home / ".claude" / "skills" / "context-gate-beta" / ".claude-plugin"
                    / "plugin.json")
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


class AgentHooks(Plugin):
    """The agent options through the plugin: one SessionStart child resolves them with usage, and
    the hooks register for the events they act on."""
    GATES = '\n[checks.agent-gates]\nlevel = "error"\n'
    MEASURE = '\n[checks.agent-measure]\nlevel = "warn"\n'

    def configure(self, extra: str) -> None:
        cfg = self.project / layout.CONFIG
        cfg.write_text(cfg.read_text(encoding="utf-8") + extra, encoding="utf-8")

    def run_hook(self, script: str, payload: dict) -> str:
        env = {**os.environ, "HOME": str(self.home), "CLAUDE_PROJECT_DIR": str(self.project),
               "CONTEXT_GATE_NO_UPDATE_CHECK": "1"}
        res = subprocess.run([sys.executable, str(self.built / "hooks" / script)],
                             input=json.dumps(payload), env=env, capture_output=True, text=True)
        self.assertEqual((res.returncode, res.stderr), (0, ""))
        return res.stdout

    def start(self) -> str:
        return self.run_hook("session_start.py", {"session_id": "S", "hook_event_name": "SessionStart"})

    def resolved(self):
        from govern import agent_runs
        return agent_runs.read_json(agent_runs.resolved_path(self.home, "S"))

    def test_hooks_json_registers_every_event_the_agent_hooks_act_on(self):
        hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
        wanted = {"PreToolUse": "Agent|SendMessage|Bash", "PostToolUse": "*",
                  "PostToolUseFailure": "*", "SubagentStart": None, "SubagentStop": None,
                  "UserPromptSubmit": None}
        for event, matcher in wanted.items():
            ours = [e for e in hooks[event] if "hooks/agents.py" in e["hooks"][0]["command"]]
            self.assertEqual(len(ours), 1, event)
            self.assertEqual(ours[0].get("matcher"), matcher, event)
            self.assertTrue((PLUGIN / "hooks" / "agents.py").is_file())

    def test_the_existing_hooks_are_kept(self):
        hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
        self.assertIn("hooks/session_start.py", hooks["SessionStart"][0]["hooks"][0]["command"])
        for event in ("PostToolUse", "UserPromptSubmit", "SessionEnd"):
            self.assertIn("hooks/usage.py", hooks[event][0]["hooks"][0]["command"])

    def test_gates_on_resolves_for_the_session(self):
        self.configure(self.GATES)
        self.start()
        got = self.resolved()
        self.assertEqual(got["gates"], {"briefed": ["developer", "senior-developer"],
                                        "claims": True, "deny_push": True, "max_refusals": 2,
                                        "checklist": False, "checklist_keys": {},
                                    "checklist_texts": {}})
        self.assertIsNone(got["measure"])
        self.assertEqual(got["root"], str(self.project.resolve()))

    def test_measure_on_resolves_with_its_params(self):
        self.configure(self.MEASURE)
        self.start()
        got = self.resolved()
        self.assertEqual((got["gates"], got["measure"]), (None, {"notice": True}))

    def test_runs_resolve_prints_what_the_session_resolved(self):
        import contextlib
        import io
        from govern import cli
        self.configure(self.GATES + self.MEASURE)
        self.start()
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"HOME": str(self.home),
                                          "CONTEXT_GATE_NO_UPDATE_CHECK": "1"}), \
                contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["runs", "resolve", "--json"], root=self.project), 0)
        self.assertEqual(json.loads(out.getvalue()), self.resolved())

    def test_usage_and_the_agent_options_are_resolved_by_the_one_child(self):
        from govern import usage
        self.configure(self.GATES + '\n[checks.usage]\nlevel = "error"\n')
        self.start()
        self.assertIsNotNone(self.resolved())
        self.assertTrue(usage.read_json(usage.resolved_path(self.home, "S"))["enabled"])
        self.assertFalse((PLUGIN / "hooks" / "agents_resolve.py").exists())

    def test_options_set_only_in_local_toml_are_resolved(self):
        """A beta's options live in local.toml: the pre-check reads it as well as config.toml."""
        self.beta_plugin()
        self.pin(BETA)
        self.local(f'[governance]\nengine = "{BETA}"\n[checks.agent-gates]\nlevel = "error"\n')
        self.start()
        self.assertTrue(self.resolved()["gates"])

    def test_usage_set_only_in_local_toml_is_resolved(self):
        from govern import usage
        self.beta_plugin()
        self.pin(BETA)
        self.local(f'[governance]\nengine = "{BETA}"\n[checks.usage]\nlevel = "error"\n')
        self.start()
        self.assertTrue(usage.read_json(usage.resolved_path(self.home, "S"))["enabled"])

    def test_naming_an_option_as_off_writes_no_file_and_clears_a_stale_one(self):
        from govern import agent_runs
        self.configure(self.GATES.replace("error", "off"))
        agent_runs.write_json(agent_runs.resolved_path(self.home, "S"), {"gates": {}, "root": "x"})
        self.start()
        self.assertIsNone(self.resolved())

    def test_a_config_that_names_neither_option_nor_a_profile_gets_no_child(self):
        """The same proof as for usage: an extension that would fail loudly if config were loaded
        leaves no log line and no file."""
        self.configure("")
        cfg = self.project / layout.CONFIG
        cfg.write_text(cfg.read_text(encoding="utf-8").replace(
            "schema = 1", 'schema = 1\nextensions = ["ext"]'), encoding="utf-8")
        (self.project / "ext").mkdir()
        (self.project / "ext" / "broken.py").write_text("raise RuntimeError('boom')\n",
                                                        encoding="utf-8")
        self.start()
        from govern import usage
        self.assertIsNone(self.resolved())
        log = usage.log_path(self.home)
        self.assertFalse(log.exists() and "resolution failed" in log.read_text(encoding="utf-8"))

    def test_either_option_name_alone_passes_the_pre_check(self):
        """A broken extension makes the child fail, which logs: proof the child was spawned for a
        config that names only one of the new options."""
        from govern import usage
        for extra in (self.GATES, self.MEASURE):
            with self.subTest(extra=extra.strip()):
                self.pin(__version__)
                self.configure(extra)
                cfg = self.project / layout.CONFIG
                cfg.write_text(cfg.read_text(encoding="utf-8").replace(
                    "schema = 1", 'schema = 1\nextensions = ["ext"]'), encoding="utf-8")
                (self.project / "ext").mkdir(exist_ok=True)
                (self.project / "ext" / "broken.py").write_text("raise RuntimeError('boom')\n",
                                                                encoding="utf-8")
                usage.log_path(self.home).unlink(missing_ok=True)
                self.start()
                self.assertIn("resolution failed", usage.log_path(self.home).read_text(encoding="utf-8"))

    def test_session_start_sweeps_state_older_than_thirty_days(self):
        import time
        from govern import agent_runs
        self.configure(self.MEASURE)
        old = agent_runs.agents_dir(self.home) / "old-session"
        old.mkdir(parents=True)
        (old / "runs.jsonl").write_text("{}\n", encoding="utf-8")
        then = time.time() - 40 * 86400
        for p in (old, old / "runs.jsonl"):
            os.utime(p, (then, then))
        recent = agent_runs.agents_dir(self.home) / "recent-session"
        recent.mkdir()
        (recent / "runs.jsonl").write_text("{}\n", encoding="utf-8")
        self.start()
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())

    def test_the_installed_hook_denies_an_unbriefed_launch_and_logs_nothing_with_both_off(self):
        launch = {"session_id": "S", "hook_event_name": "PreToolUse", "tool_name": "Agent",
                  "tool_input": {"subagent_type": "developer", "prompt": "Fix it."}}
        self.assertEqual(self.run_hook("agents.py", launch), "")
        self.configure(self.GATES)
        self.start()
        out = json.loads(self.run_hook("agents.py", launch))
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")


class BuildInFlight(Plugin):
    """The line a session starts with when the checked-out branch has a build ledger. The child
    that resolves the options writes it; a project with no build folder pays nothing for it."""

    def git(self, *args: str) -> None:
        env = {**os.environ, "HOME": str(self.home), "GIT_CONFIG_NOSYSTEM": "1"}
        subprocess.run(["git", "-C", str(self.project), *args], check=True, capture_output=True,
                       env=env)

    def on_branch(self, name: str = "phase/p1") -> None:
        self.git("init", "-q")
        self.git("symbolic-ref", "HEAD", f"refs/heads/{name}")

    def ledger(self, *statuses: str, branch: str = "phase/p1", phase: str = "p1"):
        from govern import build
        units = [{"id": f"u{n}", "title": f"Unit {n}", "role": "developer", "status": status,
                  "attempts": 0, "branch": "", "commit": "", "note": ""}
                 for n, status in enumerate(statuses, 1)]
        made = build.Ledger(
            path=self.project / layout.GOV_DIR / "build" / phase / build.LEDGER, phase=phase,
            branch=branch, base="0" * 40, units=units)
        build.save(made)
        return made

    def break_the_config(self) -> None:
        """An extension that fails when the config is loaded: the resolve child then fails, and
        the hook logs it. The log line is the proof the child was spawned."""
        cfg = self.project / layout.CONFIG
        cfg.write_text(cfg.read_text(encoding="utf-8").replace(
            "schema = 1", 'schema = 1\nextensions = ["ext"]'), encoding="utf-8")
        (self.project / "ext").mkdir()
        (self.project / "ext" / "broken.py").write_text("raise RuntimeError('boom')\n",
                                                        encoding="utf-8")

    def child_failed(self) -> bool:
        from govern import usage
        log = usage.log_path(self.home)
        return log.exists() and "resolution failed" in log.read_text(encoding="utf-8")

    def start(self, sid: str = "S") -> str:
        env = {**os.environ, "HOME": str(self.home), "CLAUDE_PROJECT_DIR": str(self.project),
               "CONTEXT_GATE_NO_UPDATE_CHECK": "1"}
        res = subprocess.run(
            [sys.executable, str(self.built / "hooks" / "session_start.py")],
            input=json.dumps({"session_id": sid, "hook_event_name": "SessionStart"}),
            env=env, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual((res.returncode, res.stderr), (0, ""))
        return res.stdout

    def test_the_checked_out_branchs_build_is_said_in_one_line(self):
        from govern import build
        self.on_branch()
        made = self.ledger("done", "pending", "pending")
        out = json.loads(self.start())
        line = ("A build is in flight: p1, 1 of 3 units done. Next: "
                + build.next_action(made))
        self.assertEqual(out["systemMessage"], line)
        self.assertEqual(out["hookSpecificOutput"],
                         {"hookEventName": "SessionStart", "additionalContext": line})
        self.assertIn(f"{build.roles.GOVERN} build launch u2", line)
        self.assertNotIn("\n", line)

    def test_with_no_build_folder_nothing_more_is_spawned_and_nothing_is_said(self):
        from govern import agent_runs
        self.on_branch()
        self.break_the_config()
        self.assertEqual(self.start(), "")
        self.assertFalse(self.child_failed())
        self.assertFalse(agent_runs.agents_dir(self.home).exists())

    def test_the_build_folder_alone_spawns_the_child_and_a_broken_config_keeps_the_line(self):
        self.on_branch()
        self.ledger("running")
        self.break_the_config()
        out = json.loads(self.start())
        self.assertTrue(out["systemMessage"].startswith(
            "A build is in flight: p1, 0 of 1 units done. Next: Unit u1 is running"))
        self.assertTrue(self.child_failed())

    def test_a_ledger_for_another_branch_says_nothing(self):
        self.on_branch("unit/u1")
        self.ledger("pending")
        self.assertEqual(self.start(), "")

    def test_two_ledgers_say_only_the_checked_out_branchs(self):
        self.on_branch("phase/p2")
        self.ledger("pending")
        self.ledger("done", branch="phase/p2", phase="p2")
        self.assertTrue(json.loads(self.start())["systemMessage"].startswith(
            "A build is in flight: p2, 1 of 1 units done. Next: Every unit is done"))

    def test_a_build_folder_with_no_ledger_says_nothing(self):
        self.on_branch()
        (self.project / layout.GOV_DIR / "build").mkdir()
        self.assertEqual(self.start(), "")
        self.assertFalse(self.child_failed())

    def test_a_build_folder_holding_only_its_gitignore_spawns_nothing(self):
        self.on_branch()
        build = self.project / layout.GOV_DIR / "build"
        build.mkdir()
        (build / ".gitignore").write_text("*\n", encoding="utf-8")
        self.break_the_config()
        self.assertEqual(self.start(), "")
        self.assertFalse(self.child_failed())

    def test_a_ledger_that_cannot_be_read_says_nothing_and_fails_nothing(self):
        self.on_branch()
        made = self.ledger("pending")
        made.path.write_bytes(b'phase = "p\xff1"\n')
        self.assertEqual(self.start(), "")
        self.assertFalse(self.child_failed())

    def test_outside_a_git_repository_it_says_nothing(self):
        self.ledger("pending")
        self.assertEqual(self.start(), "")
        self.assertFalse(self.child_failed())

    def test_the_line_is_handed_over_once_and_not_said_after_the_phase_is_gone(self):
        from govern import agent_runs
        self.on_branch()
        made = self.ledger("pending")
        self.assertIn("A build is in flight", self.start())
        self.assertEqual(list(agent_runs.agents_dir(self.home).glob("*build*")), [])
        shutil.rmtree(made.dir)
        self.assertEqual(self.start(), "")

    def test_the_options_are_resolved_beside_it(self):
        from govern import agent_runs
        self.on_branch()
        self.ledger("pending")
        cfg = self.project / layout.CONFIG
        cfg.write_text(cfg.read_text(encoding="utf-8")
                       + '\n[checks.agent-gates]\nlevel = "error"\n', encoding="utf-8")
        self.assertIn("A build is in flight", self.start())
        resolved = agent_runs.read_json(agent_runs.resolved_path(self.home, "S"))
        self.assertEqual(resolved["gates"]["briefed"], ["developer", "senior-developer"])


class BetaSwitch(unittest.TestCase):
    """The switch an old launcher writes (`context-gate@skills-dir`) is the name the beta plugin
    had before it had one of its own: the beta engine corrects it, and the beta plugin's
    session start removes the new key an old launcher's `beta off` leaves behind."""

    OLD, NEW = layout.OLD_SKILLS_DIR_PLUGIN_ID, layout.SKILLS_DIR_PLUGIN_ID

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.project = self.tmp / "project"
        (self.project / layout.GOV_DIR).mkdir(parents=True)
        (self.project / layout.CONFIG).write_text(
            f'[governance]\nengine = "{__version__}"\nschema = 1\n', encoding="utf-8")
        (self.project / ".claude").mkdir()
        self.settings = self.project / ".claude" / "settings.local.json"
        # The plugin as a beta's install assembles it: named `context-gate-beta`.
        self.built = self.tmp / "built"
        shutil.copytree(PLUGIN, self.built)
        shutil.copytree(ENGINE / "govern", self.built / "govern",
                        ignore=shutil.ignore_patterns("__pycache__"))
        manifest = self.built / ".claude-plugin" / "plugin.json"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        manifest.write_text(json.dumps({**data, "name": layout.BETA_PLUGIN}), encoding="utf-8")

    def put(self, data) -> bytes:
        text = data if isinstance(data, str) else json.dumps(data, indent=2) + "\n"
        self.settings.write_text(text, encoding="utf-8", newline="")
        return self.settings.read_bytes()

    def got(self) -> dict:
        return json.loads(self.settings.read_text(encoding="utf-8"))

    OTHERS = {"permissions": {"allow": ["Bash(ls)"]},
              "enabledPlugins": {"a@b": True, "context-gate@skills-dir": True,
                                 "context-gate@context-gate": False, "z@y": False},
              "model": "opus"}

    def test_the_old_ids_switch_becomes_the_new_ones_and_nothing_else_moves(self):
        self.put(self.OTHERS)
        line = notice.correct_old_launcher_switch(self.project)
        self.assertEqual(len((line or "").splitlines()), 1)
        self.assertIn("restart Claude Code to load it", line)
        self.assertIn(self.NEW, line)
        got = self.got()
        self.assertEqual(list(got), ["permissions", "enabledPlugins", "model"])
        self.assertEqual(got["permissions"], self.OTHERS["permissions"])
        self.assertEqual(list(got["enabledPlugins"]),
                         ["a@b", self.NEW, "context-gate@context-gate", "z@y"])
        self.assertEqual(got["enabledPlugins"], {"a@b": True, self.NEW: True,
                                                 "context-gate@context-gate": False, "z@y": False})
        self.assertEqual(list(self.settings.parent.glob("*.tmp")), [])

    def test_the_correction_is_idempotent(self):
        self.put(self.OTHERS)
        self.assertIsNotNone(notice.correct_old_launcher_switch(self.project))
        once = self.settings.read_bytes()
        self.assertIsNone(notice.correct_old_launcher_switch(self.project))
        self.assertEqual(self.settings.read_bytes(), once)
        self.assertNotIn(self.OLD, self.got()["enabledPlugins"])

    def test_the_new_id_set_to_false_is_set_to_true(self):
        self.put({"enabledPlugins": {self.OLD: True, self.NEW: False}})
        self.assertIsNotNone(notice.correct_old_launcher_switch(self.project))
        self.assertEqual(self.got(), {"enabledPlugins": {self.NEW: True}})

    def test_every_other_state_changes_nothing(self):
        cases = {
            "old id off": {"enabledPlugins": {self.OLD: False}},
            "old id absent": {"enabledPlugins": {"a@b": True}},
            "new id on already": {"enabledPlugins": {self.NEW: True}},
            "both on": {"enabledPlugins": {self.OLD: True, self.NEW: True}},
            "no enabledPlugins": {"permissions": {}},
            "enabledPlugins is a list": {"enabledPlugins": [self.OLD]},
            "not an object": [self.OLD],
            "old id is a string": {"enabledPlugins": {self.OLD: "true"}},
            "malformed": "{not json",
            "empty": "",
        }
        for name, data in cases.items():
            with self.subTest(state=name):
                before = self.put(data)
                self.assertIsNone(notice.correct_old_launcher_switch(self.project))
                self.assertEqual(self.settings.read_bytes(), before)
        self.settings.write_bytes(b'{"enabledPlugins": {"x": \xff}}')
        self.assertIsNone(notice.correct_old_launcher_switch(self.project))
        self.settings.unlink()
        self.assertIsNone(notice.correct_old_launcher_switch(self.project))
        self.assertFalse(self.settings.exists())
        shutil.rmtree(self.project / ".claude")
        self.assertIsNone(notice.correct_old_launcher_switch(self.project))
        self.assertFalse((self.project / ".claude").exists())

    def test_a_file_that_cannot_be_written_is_left_as_it_was(self):
        before = self.put(self.OTHERS)
        with mock.patch.object(os, "replace", side_effect=PermissionError(13, "in use")):
            self.assertIsNone(notice.correct_old_launcher_switch(self.project))
        self.assertEqual(self.settings.read_bytes(), before)
        self.assertEqual(list(self.settings.parent.iterdir()), [self.settings])

    def test_the_files_own_bom_and_line_endings_are_kept(self):
        text = json.dumps(self.OTHERS, indent=2).replace("\n", "\r\n") + "\r\n"
        self.settings.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
        self.assertIsNotNone(notice.correct_old_launcher_switch(self.project))
        raw = self.settings.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(raw.count(b"\r\n"), raw.count(b"\n"))
        self.assertIn(self.NEW, self.got_bom(raw))

    @staticmethod
    def got_bom(raw: bytes) -> dict:
        return json.loads(raw.decode("utf-8-sig"))["enabledPlugins"]

    # The beta plugin's session start, after an old launcher's `beta off`.
    def hook(self) -> str:
        env = {**os.environ, "HOME": str(self.home), "CLAUDE_PROJECT_DIR": str(self.project),
               "CONTEXT_GATE_NO_UPDATE_CHECK": "1"}
        res = subprocess.run([sys.executable, str(self.built / "hooks" / "session_start.py")],
                             input="{}", env=env, capture_output=True, text=True)
        self.assertEqual((res.returncode, res.stderr), (0, ""))
        return res.stdout

    LEFT = {"permissions": {"deny": ["Bash(rm:*)"]},
            "enabledPlugins": {"a@b": True, "context-gate-beta@skills-dir": True,
                               "context-gate@context-gate": True},
            "model": "opus"}
    SAID = ("the beta is off in this project; removed context-gate-beta@skills-dir from "
            ".claude/settings.local.json. Restart Claude Code to unload the beta plugin.")

    def test_the_beta_plugin_removes_its_key_where_no_beta_is_on(self):
        self.put(self.LEFT)
        out = json.loads(self.hook())
        self.assertIn(self.SAID, out["systemMessage"].splitlines())
        self.assertIn(self.SAID, out["hookSpecificOutput"]["additionalContext"])
        got = self.got()
        self.assertEqual(list(got), ["permissions", "enabledPlugins", "model"])
        self.assertEqual(got["enabledPlugins"], {"a@b": True, "context-gate@context-gate": True})
        self.assertEqual(got["permissions"], self.LEFT["permissions"])
        once = self.settings.read_bytes()
        self.assertNotIn("the beta is off", self.hook())    # idempotent: nothing left to remove
        self.assertEqual(self.settings.read_bytes(), once)

    def test_the_beta_plugin_leaves_a_project_on_a_beta_alone(self):
        before = self.put(self.LEFT)
        (self.project / layout.LOCAL).write_text(
            f'[governance]\nengine = "{BETA}"\n', encoding="utf-8")
        self.assertNotIn("the beta is off", self.hook())
        self.assertEqual(self.settings.read_bytes(), before)
        # A local.toml that cannot be read says nothing about the beta: also left alone.
        (self.project / layout.LOCAL).write_bytes(b"\xff[")
        self.assertNotIn("the beta is off", self.hook())
        self.assertEqual(self.settings.read_bytes(), before)

    def test_the_stable_plugin_and_an_ungoverned_project_are_left_alone(self):
        before = self.put(self.LEFT)
        manifest = self.built / ".claude-plugin" / "plugin.json"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        manifest.write_text(json.dumps({**data, "name": layout.PLUGIN}), encoding="utf-8")
        self.assertNotIn("the beta is off", self.hook())
        self.assertEqual(self.settings.read_bytes(), before)
        manifest.write_text(json.dumps({**data, "name": layout.BETA_PLUGIN}), encoding="utf-8")
        (self.project / layout.CONFIG).unlink()
        self.assertEqual(self.hook(), "")
        self.assertEqual(self.settings.read_bytes(), before)

    def test_a_beta_plugin_session_without_the_key_or_the_file_says_nothing(self):
        self.assertEqual(self.hook(), "")
        self.put({"enabledPlugins": {"a@b": True}})
        self.assertEqual(self.hook(), "")
        self.put({"enabledPlugins": {self.NEW: False}})
        self.assertEqual(self.hook(), "")
        self.put("{not json")
        self.assertEqual(self.hook(), "")
        shutil.rmtree(self.project / ".claude")
        self.assertEqual(self.hook(), "")
        self.assertFalse((self.project / ".claude").exists())


if __name__ == "__main__":
    unittest.main()


class BetaStandsDown(unittest.TestCase):
    """The beta copy of the plugin acts only in a project that is on a beta, so it and the
    released plugin never both act: `beta off` deletes local.toml."""
    GATES = AgentHooks.GATES
    _plugin_set_up = Plugin.setUp
    tearDown = Plugin.tearDown
    pin = Plugin.pin
    configure = AgentHooks.configure
    run_hook = AgentHooks.run_hook
    start = AgentHooks.start
    resolved = AgentHooks.resolved

    def setUp(self):
        self._plugin_set_up()
        beta = self.built.with_name("context-gate-beta")
        os.rename(self.built, beta)
        self.built = beta
        self.configure(self.GATES)

    def launch(self) -> str:
        return self.run_hook("agents.py", {
            "session_id": "S", "hook_event_name": "PreToolUse", "tool_name": "Agent",
            "tool_use_id": "t1", "cwd": str(self.project),
            "tool_input": {"subagent_type": "developer", "prompt": "Fix it."}})

    def on_a_beta(self) -> None:
        (self.project / layout.LOCAL).write_text(
            f'[governance]\nengine = "{self.beta_version()}"\n', encoding="utf-8")

    def beta_version(self) -> str:
        return "9.9.9-beta.1"

    def test_with_the_beta_on_its_gates_act(self):
        self.on_a_beta()
        (self.home / ".local" / "state" / "context-gate" / "agents").mkdir(parents=True)
        from govern import agent_runs
        agent_runs.write_json(agent_runs.resolved_path(self.home, "S"),
                              {"gates": {"briefed": ["developer"], "max_refusals": 2,
                                         "claims": True, "deny_push": True},
                               "measure": None, "project": str(self.project)})
        self.assertIn("deny", self.launch())

    def test_with_the_beta_off_the_same_launch_is_let_through_untouched(self):
        (self.home / ".local" / "state" / "context-gate" / "agents").mkdir(parents=True)
        from govern import agent_runs
        agent_runs.write_json(agent_runs.resolved_path(self.home, "S"),
                              {"gates": {"briefed": ["developer"], "max_refusals": 2,
                                         "claims": True, "deny_push": True},
                               "measure": None, "project": str(self.project)})
        self.assertEqual(self.launch(), "")

    def test_with_the_beta_off_session_start_resolves_nothing_and_says_nothing(self):
        self.assertEqual(self.start(), "")
        self.assertIsNone(self.resolved())

    def test_with_the_beta_off_the_usage_hook_says_nothing(self):
        out = self.run_hook("usage.py", {"session_id": "S", "hook_event_name": "SessionEnd"})
        self.assertEqual(out, "")
