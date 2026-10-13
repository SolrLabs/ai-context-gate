"""Behaviour tests on small throwaway workspaces: the local layer, the installer, its baselines, notices and public beta tags."""
from __future__ import annotations

import contextlib
import io
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

class LocalLayer(Base):
    """`.context-gate/local.toml` is the top config layer for the beta engine it names, on this
    machine only: that beta accepts the committed pin it does not match, and every other engine
    never reads the file."""

    def setUp(self) -> None:
        super().setUp()
        from unittest import mock
        self.mock = mock
        self.w = self.ws(extra='\n[checks.writing-rules]\nlevel = "warn"\n')

    def local(self, text: str | bytes = "") -> None:
        if isinstance(text, str):
            text = f'[governance]\nengine = "{BETA}"\n' + text
        self.w.write(layout.LOCAL, text)

    def load(self, engine: str | None = BETA) -> config.Config:
        if engine is None:
            return config.load(self.w.root, self.w.home)
        with self.mock.patch.object(config, "__version__", engine):
            return config.load(self.w.root, self.w.home)

    def refused(self, engine: str | None = BETA) -> str:
        with self.assertRaises(config.ConfigError) as ctx:
            self.load(engine)
        return str(ctx.exception)

    def test_the_beta_it_names_runs_the_committed_pin_and_local_wins(self):
        self.local('\n[checks.writing-rules]\nlevel = "error"\n')
        s = self.load().checks["writing-rules"]
        self.assertEqual((s.level, s.source["level"]), ("error", "local"))

    def test_the_settings_beta_on_writes_load(self):
        self.local('plugins_before = { "context-gate@context-gate" = true }\n')
        self.assertEqual(self.load().checks["writing-rules"].source["level"], "project")

    def test_overriding_a_principle_from_it_is_named(self):
        prof = self.tmp / "prof"
        prof.mkdir()
        wtext(prof / "principles.toml", '[checks.agents]\nmax_turns = 100\n'
              '\n[checks.working-file-count]\nlevel = "error"\n')
        cfg = self.w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            "schema = 1", f'schema = 1\nprofile = "{prof.as_posix()}"', 1))
        self.local('\n[checks.agents]\nmax_turns = 50\n'
                   '\n[checks.working-file-count]\nlevel = "warn"\n')
        self.assertEqual(sorted(self.load().profile_overrides),
                         [("agents", "max_turns"), ("working-file-count", "level")])

    def test_a_stable_engine_ignores_it(self):
        self.local('\n[checks.no-such-check]\nlevel = "error"\n')
        s = self.load(None).checks["writing-rules"]
        self.assertEqual((s.level, s.source["level"]), ("warn", "project"))
        code, _, err = self.w.run("check")
        self.assertNotEqual(code, 2, err)
        self.assertNotIn("no-such-check", err)

    def test_a_stable_engine_ignores_it_even_when_it_names_that_engine(self):
        self.w.write(layout.LOCAL, f'[governance]\nengine = "{__version__}"\n'
                     '\n[checks.no-such-check]\nlevel = "error"\n')
        self.assertEqual(self.load(None).checks["writing-rules"].level, "warn")

    def test_another_beta_ignores_it_and_refuses_the_committed_pin(self):
        self.local('\n[checks.no-such-check]\nlevel = "error"\n')
        # A beta of another release: a later beta of the named one reads the file too.
        err = self.refused(f"{NEXT_RELEASE}-beta.1")
        self.assertIn(f"pins engine {__version__}", err)
        self.assertNotIn("no-such-check", err)

    def test_no_local_file_and_a_beta_engine_is_refused(self):
        # A beta never runs a project silently: only local.toml lets it past the pin.
        self.assertIn(f"pins engine {__version__}, but this is engine {BETA}", self.refused())

    def test_an_unreadable_file_is_ignored_not_a_traceback(self):
        self.local(b'[governance]\nengine = "\xff"\n')
        self.assertIn(f"pins engine {__version__}", self.refused())

    def test_another_governance_key_is_refused(self):
        self.local('profile = "x"\n')
        err = self.refused()
        self.assertIn("local.toml", err)
        self.assertIn("profile", err)

    def test_a_layout_table_is_refused(self):
        self.local('\n[projects]\nrequired_docs = ["X.md"]\n')
        self.assertIn(f"{layout.LOCAL}: sets projects", self.refused())

    def test_a_check_order_is_refused(self):
        self.local('\n[checks]\nworkspace_order = ["writing-rules"]\n')
        self.assertIn(f"{layout.LOCAL}: [checks] sets workspace_order", self.refused())

    def test_plugins_before_must_map_ids_to_booleans(self):
        self.local('plugins_before = { "context-gate@context-gate" = "yes" }\n')
        self.assertIn("plugins_before must be a table", self.refused())

    def test_widening_a_list_needs_its_reason(self):
        self.local('\n[checks.decision-log]\n'
                   'statuses = ["locked", "provisional", "superseded", "dropped"]\n')
        self.assertTrue(self.refused().startswith(f"{layout.LOCAL}: [checks."))

    def test_a_beta_pin_in_the_committed_config_is_refused(self):
        cfg = self.w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            f'engine = "{__version__}"', f'engine = "{BETA}"', 1))
        self.assertIn(f"{CFG} pins beta {BETA}; a beta runs only from {layout.LOCAL} on one "
                      f"machine (govern beta on {BETA}) — pin a release", self.refused(None))

    def test_engine_alone_activates_it(self):
        # What `govern beta on` writes: only the engine it names.
        self.w.write(layout.LOCAL, f'[governance]\nengine = "{BETA}"\n')
        self.assertEqual(self.load().checks["writing-rules"].level, "warn")

    def pin(self, new: str) -> None:
        cfg = self.w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(f'engine = "{__version__}"', new, 1))

    def test_the_layer_does_not_excuse_a_missing_committed_pin(self):
        self.pin("")
        self.local()
        self.assertIn("[governance] engine is required", self.refused())

    def test_the_layer_does_not_excuse_a_beta_committed_pin(self):
        self.pin(f'engine = "{BETA}"')
        self.local()
        self.assertIn(f"pins beta {BETA}", self.refused())

    def test_an_override_set_there_is_reported_under_its_file(self):
        prof = self.tmp / "prof"
        prof.mkdir()
        wtext(prof / "principles.toml", '[checks.working-file-count]\nlevel = "error"\n')
        cfg = self.w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            "schema = 1", f'schema = 1\nprofile = "{prof.as_posix()}"', 1))
        self.local('\n[checks.working-file-count]\nlevel = "warn"\n')
        warns = self.overrides_warnings()
        self.assertTrue(any(w.startswith(f"{layout.LOCAL}: [checks.working-file-count]")
                            for w in warns), warns)

    def test_a_widening_set_there_is_reported_under_its_file(self):
        wtext(self.w.root / CFG, (self.w.root / CFG).read_text(encoding="utf-8").replace(
            "schema = 1", "schema = 1\nrequire_reasons = false", 1))
        self.local('\n[checks.decision-log]\n'
                   'statuses = ["locked", "provisional", "superseded", "dropped"]\n')
        warns = self.overrides_warnings()
        self.assertTrue(any(w.startswith(f"{layout.LOCAL}: [checks.decision-log] statuses")
                            for w in warns), warns)

    def overrides_warnings(self) -> list[str]:
        from govern.checks.overrides import standard_overrides
        cfg = self.load()
        return [str(x) for x in standard_overrides(self.mock.Mock(cfg=cfg), {}).warnings]

    def test_loosening_there_without_a_reason_names_the_file(self):
        self.local('\n[checks.agent-worktrees]\nlevel = "off"\n')
        err = self.refused()
        self.assertIn(layout.LOCAL, err)
        self.assertIn("agent-worktrees", err)


class LineEndings(Base):
    """A CRLF checkout (git on Windows with autocrlf) behaves exactly like an LF one."""

    def test_crlf_files_index_and_check_clean(self):
        w = self.ws()
        for rel in ("AGENTS.md", "governance/DECISIONS.md", "projects/alpha/DECISIONS.md"):
            path = w.root / rel
            path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        code, out, _ = w.run("check")
        self.assertEqual(code, 0, out)
        before = (w.root / "projects/alpha/DECISIONS.md").read_bytes()
        code, out, _ = w.run("index")
        self.assertIn("(0 block(s) updated)", out)
        log = w.root / "projects/alpha/DECISIONS.md"
        with open(log, encoding="utf-8", newline="") as fh:
            wtext(log, fh.read() + entry("A", 101).replace("\n", "\r\n"))
        w.run("index")
        after = log.read_bytes()
        self.assertNotIn(b"\n", after.replace(b"\r\n", b""), "an edit mixed line endings")
        self.assertTrue(before.startswith(b"---\r\n"))

    def test_messages_use_forward_slashes(self):
        w = self.ws()
        w.write("projects/alpha/sub/bad.md", fm("manual"))
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertIn("alpha/sub/bad.md: doc_type 'manual'", out)


class Installer(Base):
    """Everything the tool owns lives in one directory; uninstall reverses the rest."""

    def project(self, extra=""):
        w = self.ws(extra=extra)
        cfg = self.tmp / "config.toml"
        shutil.move(str(w.root / CFG), str(cfg))
        (w.root / layout.GOV_DIR).rmdir()
        w.write("governance/gate.py", "print('old gate')\n")
        w.write("governance/old-baseline.json", '{"governed_docs:alpha": 3}\n')
        return w.root, cfg

    def install(self, root, cfg, *extra):
        return installer.main(["install", "--root", str(root), "--config", str(cfg), *extra])

    def snapshot(self, root):
        return {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}

    def test_install_then_uninstall_leaves_the_project_as_it_was(self):
        root, cfg = self.project()
        before = self.snapshot(root)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.install(root, cfg, "--entrypoint", "governance/gate.py",
                                          "--migrate-baseline", "governance/old-baseline.json"), 0)
        self.assertTrue((root / layout.CONFIG).is_file())
        self.assertTrue((root / layout.BASELINE).is_file())
        self.assertFalse((root / "governance/old-baseline.json").exists())
        self.assertIn("runpy", (root / "governance/gate.py").read_text(encoding="utf-8"))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(installer.main(["uninstall", "--root", str(root)]), 0)
        self.assertEqual(self.snapshot(root), before)
        self.assertFalse((root / layout.GOV_DIR).exists())

    def test_uninstall_refuses_over_an_edited_stub_and_changes_nothing(self):
        root, cfg = self.project()
        with contextlib.redirect_stdout(io.StringIO()):
            self.install(root, cfg, "--entrypoint", "governance/gate.py")
        wtext(root / "governance/gate.py", "edited\n")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(installer.main(["uninstall", "--root", str(root)]), 1)
        self.assertTrue((root / layout.GOV_DIR).is_dir())
        self.assertEqual((root / "governance/gate.py").read_text(encoding="utf-8"), "edited\n")

    def test_bad_config_rolls_back(self):
        root, cfg = self.project()
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(f'engine = "{__version__}"', 'engine = "9.9.9"'))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.install(root, cfg, "--entrypoint", "governance/gate.py"), 2)
        self.assertFalse((root / layout.GOV_DIR).exists())
        self.assertEqual((root / "governance/gate.py").read_text(encoding="utf-8"),
                         "print('old gate')\n")

    def test_refuses_over_an_existing_install(self):
        root, cfg = self.project()
        with contextlib.redirect_stdout(io.StringIO()):
            self.install(root, cfg)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.install(root, cfg), 2)

    # ---------------------------------------------------------- rendered agent roles

    def rendered(self, extra=""):
        """Installed with the agent-roles option on and its roles rendered: (root, a snapshot
        from before the install)."""
        root, cfg = self.project(extra=ROLES_ON + extra)
        before = self.snapshot(root)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.install(root, cfg), 0)
            self.assertEqual(cli.main(["agents", "render"], root=root), 0)
        self.assertEqual(len(installer.read_manifest(root)["rendered"]), 6)
        return root, before

    def uninstall(self, root, *extra):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = installer.main(["uninstall", "--root", str(root), *extra])
        return code, out.getvalue(), err.getvalue()

    def test_uninstall_removes_the_rendered_roles_and_leaves_the_project_as_it_was(self):
        root, before = self.rendered()
        self.assertTrue((root / ".claude/agents/developer.md").is_file())
        self.assertTrue((root / ROLES_RULES).is_file())
        code, out, err = self.uninstall(root)
        self.assertEqual(code, 0, err)
        self.assertIn("  removed      .claude/agents/developer.md\n", out)
        self.assertIn(f"  removed      {ROLES_RULES}\n", out)
        self.assertEqual(self.snapshot(root), before)
        self.assertFalse((root / ".claude").exists())       # the directories it left empty

    def test_uninstall_reads_a_crlf_checkout_of_a_rendered_role_as_unchanged(self):
        root, before = self.rendered()
        for rel in (".claude/agents/developer.md", ROLES_RULES):
            path = root / rel
            wtext(path, path.read_bytes().decode("utf-8").replace("\n", "\r\n"))
        code, _, err = self.uninstall(root)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.snapshot(root), before)

    def test_uninstall_refuses_over_an_edited_rendered_role_and_force_removes_it(self):
        root, before = self.rendered()
        path = root / ".claude/agents/developer.md"
        wtext(path, path.read_text(encoding="utf-8") + "\nAn edit.\n")
        installed = self.snapshot(root)
        code, _, err = self.uninstall(root)
        self.assertEqual(code, 1)
        self.assertIn("conflict     .claude/agents/developer.md was edited after it was "
                      "rendered (--force removes it)", err)
        self.assertEqual(self.snapshot(root), installed)             # nothing was changed
        code, _, err = self.uninstall(root, "--force")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.snapshot(root), before)

    def test_uninstall_never_removes_a_file_that_replaced_a_rendered_role(self):
        root, before = self.rendered()
        mine = "---\nname: developer\n---\nOur own developer, written by hand.\n"
        wtext(root / ".claude/agents/developer.md", mine)
        code, _, err = self.uninstall(root)
        self.assertEqual(code, 1)
        self.assertIn("conflict     .claude/agents/developer.md is no longer the file "
                      "context-gate rendered (--force leaves it in place)", err)
        self.assertTrue((root / layout.GOV_DIR).is_dir())
        code, out, err = self.uninstall(root, "--force")
        self.assertEqual(code, 0, err)
        self.assertIn("  left         .claude/agents/developer.md", out)
        self.assertEqual((root / ".claude/agents/developer.md").read_text(encoding="utf-8"), mine)
        self.assertFalse((root / ".claude/agents/qa-engineer.md").exists())
        self.assertFalse((root / layout.GOV_DIR).exists())

    def test_uninstall_removes_a_rendered_role_that_no_record_names(self):
        # An older engine rewriting installed.toml drops the table it does not know.
        root, before = self.rendered(extra='rules_file = ".claude/rules/build.md"\n')
        record = installer.read_manifest(root)
        record["rendered"] = []
        installer.write_manifest(root / layout.MANIFEST, record)
        self.assertNotIn("rendered", (root / layout.MANIFEST).read_text(encoding="utf-8"))
        mine = "---\nname: helper\n---\nWritten by hand.\n"
        wtext(root / ".claude/agents/helper.md", mine)
        code, _, err = self.uninstall(root)
        self.assertEqual(code, 0, err)
        left = self.snapshot(root)
        self.assertEqual(left.pop(Path(".claude/agents/helper.md")), mine.encode())
        self.assertEqual(left, before)                     # the moved rules file went too

    def test_uninstall_leaves_a_copy_of_a_role_under_another_name(self):
        root, before = self.rendered()
        record = installer.read_manifest(root)
        record["rendered"] = []
        installer.write_manifest(root / layout.MANIFEST, record)
        copy = root / ".claude/agents/my-dev.md"
        copy.write_bytes((root / ".claude/agents/developer.md").read_bytes())
        code, _, err = self.uninstall(root)
        self.assertEqual(code, 0, err)
        self.assertTrue(copy.is_file())
        self.assertFalse((root / ".claude/agents/developer.md").exists())

    def test_uninstall_removes_recorded_roles_after_the_option_is_turned_off(self):
        root, before = self.rendered()
        cfg = root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(ROLES_ON, ""))
        self.assertNotIn("agent-roles", cfg.read_text(encoding="utf-8"))
        code, _, err = self.uninstall(root)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.snapshot(root), before)


class InstallBaselinesTodaysBreaches(Base):
    """Installing must not leave the gate red over a breach that predates the install, and must
    never raise an entry already in the baseline."""

    project = Installer.project
    install = Installer.install

    def oversized(self):
        root, cfg = self.project(extra="\n[checks.doc-frontmatter]\nmax_working_words = 3\n")
        notes = root / "projects/alpha/working-files/notes.md"
        notes.parent.mkdir(parents=True, exist_ok=True)
        wtext(notes, fm("working", status="active") + "word " * 10)
        return root, cfg

    def test_check_exits_0_after_install(self):
        root, cfg = self.oversized()
        with contextlib.redirect_stdout(io.StringIO()):
            code = self.install(root, cfg, "--entrypoint", "governance/gate.py")
        self.assertEqual(code, 0)
        report_text = (root / layout.GOV_DIR / "install-report.md").read_text(encoding="utf-8")
        self.assertIn("working_file_words:projects/alpha/working-files/notes.md=10", report_text)
        code, out, _ = installer_check(root)
        self.assertEqual(code, 0, out)

    def test_no_report_still_baselines(self):
        root, cfg = self.oversized()
        with contextlib.redirect_stdout(io.StringIO()):
            code = self.install(root, cfg, "--no-report")
        self.assertEqual(code, 0)
        self.assertFalse((root / layout.GOV_DIR / "install-report.md").exists())
        baseline = json.loads((root / layout.BASELINE).read_text(encoding="utf-8"))
        self.assertIn("working_file_words:projects/alpha/working-files/notes.md", baseline)

    def test_an_existing_migrated_entry_that_has_since_grown_stays_an_error(self):
        root, cfg = self.oversized()
        wtext(root / "governance/old-baseline.json",
             json.dumps({"working_file_words:projects/alpha/working-files/notes.md": 5}))
        with contextlib.redirect_stdout(io.StringIO()):
            code = self.install(root, cfg, "--migrate-baseline", "governance/old-baseline.json",
                                "--no-report")
        self.assertEqual(code, 0)
        baseline = json.loads((root / layout.BASELINE).read_text(encoding="utf-8"))
        # never raised: the migrated value (5) stands even though the file is now 10 words
        self.assertEqual(baseline["working_file_words:projects/alpha/working-files/notes.md"], 5)
        code, out, _ = installer_check(root)
        self.assertEqual(code, 1, out)
        self.assertIn("grew to 10 words (doc-frontmatter max_working_words=3, baseline 5)",
                      out)


class ExplainTrapsAndHints(Base):
    """`explain`, the ratchet opt-out, a trap's **Bites when:** line, stale references and the
    unknown-project hint."""

    def test_explain_shows_value_and_source(self):
        w = self.ws(extra="\n[checks.agents]\nmax_turns = 100\n")
        code, out, _ = w.run("explain", "agents")
        self.assertEqual(code, 0)
        self.assertIn("max_turns = 100  (project; engine default 0)", out)
        self.assertIn('efforts = ["low", "medium", "high", "xhigh", "max"]  (engine default)', out)

    def test_ratchet_opt_out_needs_a_reason_and_is_honoured(self):
        w = self.ws(extra="\n[checks.governed-doc-count]\nmax_docs = 0\nratchet = false\n")
        code, _, err = w.run("check")
        self.assertEqual(code, 2)
        self.assertIn("loosens ratchet", err)
        (self.tmp / "b").mkdir()
        w = Workspace(self.tmp / "b", extra="\n[checks.governed-doc-count]\nmax_docs = 0\n"
                      "ratchet = false\nreason = \"warn-only by policy\"\n")
        code, out, _ = w.run("check")
        self.assertEqual(code, 0, out)
        self.assertIn("governed docs exceeds", out)

    def test_trap_bites_when(self):
        w = self.ws()
        w.write("projects/alpha/working-files/traps.md", fm("working", status="active")
                + "\n## T-1 — Wraps\n\n**Bites when:** first line\ncontinues here.\n\nBody.\n"
                + "\n## T-2 — Silent\n\nBody only.\n")
        from govern.decisions import Grammar
        traps = Grammar("em-dash", [], text.Markers("t")).traps(
            w.root / "projects/alpha/working-files/traps.md")
        self.assertEqual(traps[0].bites, "first line continues here.")
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertIn("T-2 has no **Bites when:**", out)
        self.assertNotIn("T-1 has no", out)

    def test_stale_references(self):
        w = self.ws(extra='\n[checks.stale-references]\nnames = [{ text = "OldRules", now = "config" }]\n')
        w.write("governance/notes.md", fm() + "Limits are in OldRules.\n")
        code, out, _ = w.run("check")
        self.assertIn("governance/notes.md: mentions 'OldRules' 1× — now config", out)

    def test_unknown_project_hint(self):
        code, _, err = self.ws().run("show", "--project", "nope", "W-1")
        self.assertIn("'workspace' for the workspace log", err)


class UnlimitedDeclaration(Base):
    """"0 means no limit" is declared per param (`unlimited=`), not guessed from any int param
    that happens to default to 0 — `agents.max_turns` declares it; an extension's own ceiling
    that defaults to 0 for an unrelated reason still owes a reason once raised."""

    def test_max_turns_default_zero_is_still_exempt(self):
        w = self.ws(extra="\n[checks.agents]\nmax_turns = 100\n")
        code, out, _ = w.run("check")
        self.assertEqual(code, 0, out)

    def test_an_unrelated_zero_default_param_still_needs_a_reason_once_raised(self):
        from govern.findings import Findings

        @manifest.check("ext-ceiling", scope="workspace", since="0.2.1", origin="test-ext",
                        summary="An extension ceiling that happens to default to 0.",
                        question="q", rationale="r",
                        params={"cap": manifest.Param(
                            "int", 0, "Not 'no limit' — just an extension that starts at 0",
                            looser="higher")})
        def ext_ceiling(ctx, params):
            return Findings()

        self.addCleanup(manifest.forget, "test-ext")
        w = self.ws(extra='\n[checks.ext-ceiling]\ncap = 5\n')
        code, _, err = w.run("check")
        self.assertEqual(code, 2)
        self.assertIn("[checks.ext-ceiling] loosens cap past the engine default without a "
                      "'reason'", err)
        (self.tmp / "b").mkdir()
        w2 = Workspace(self.tmp / "b", extra='\n[checks.ext-ceiling]\ncap = 5\n'
                       'reason = "raised for this test"\n')
        code, out, _ = w2.run("check")
        self.assertEqual(code, 0, out)


class Notice(Base):
    def test_release_at_the_source_is_announced_once_checked(self):
        from unittest import mock
        src = make_source(self.tmp, (__version__, "0.9.0"))
        w = self.ws()
        cfg = w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            "schema = 1", f'schema = 1\nsource = "{src.as_posix()}"', 1))
        os.environ.pop(notice.NO_UPDATE_CHECK, None)
        with mock.patch.object(notice, "__version__", "0.5.0"):   # a stable engine, whatever this is
            code, _, err = w.run("check")
        self.assertIn("context-gate 0.9.0 available", err)
        self.assertTrue((w.home / ".cache" / layout.TOOL / "releases.json").is_file())
        os.environ["CONTEXT_GATE_NO_UPDATE_CHECK"] = "1"
        self.addCleanup(os.environ.pop, "CONTEXT_GATE_NO_UPDATE_CHECK", None)
        (w.home / ".cache" / layout.TOOL / "releases.json").unlink()
        code, _, err = w.run("check")
        self.assertNotIn("available", err)
        self.assertFalse((w.home / ".cache" / layout.TOOL / "releases.json").exists())

    def _engines(self, home: Path, *versions: str) -> None:
        for v in versions:
            (layout.engines_dir(home) / v / "govern").mkdir(parents=True)
            (layout.engines_dir(home) / v / "govern" / "cli.py").write_text("", encoding="utf-8")

    def test_newest_available_is_never_a_beta(self):
        """Stable code paths never return a beta: an installed beta, even one numbered above
        every release, is not what a project is told to upgrade to."""
        home = self.tmp / "home"
        self._engines(home, "99.0.0-beta.1", "0.5.0")
        self.assertEqual(notice.newest_available(home, None), "0.5.0")
        self.assertEqual(notice.installed_versions(home), ["0.5.0"])

    def test_only_betas_installed_and_none_released_is_no_newest(self):
        home = self.tmp / "home"
        self._engines(home, "99.0.0-beta.1")
        self.assertIsNone(notice.newest_available(home, None))
        # Nor does a beta that reached the release cache count as a release.
        cache = home / ".cache" / layout.TOOL / "releases.json"
        cache.parent.mkdir(parents=True)
        cache.write_text(json.dumps({"src": {"checked": time.time(), "ok": True,
                                             "versions": ["99.0.0-beta.1"]}}), encoding="utf-8")
        self.assertIsNone(notice.newest_available(home, "src"))

    def test_a_beta_is_installed_when_the_entry_point_could_run_it(self):
        """The entry point needs govern/cli.py; a half-installed beta is not announced."""
        home = self.tmp / "home"
        self._engines(home, "99.0.0-beta.2")
        (layout.engines_dir(home) / "99.0.0-beta.1" / "govern").mkdir(parents=True)   # no cli.py
        self.assertEqual(notice.installed_betas(home), ["99.0.0-beta.2"])

    def test_a_beta_tag_at_the_source_is_not_a_release(self):
        src = make_source(self.tmp, ("0.5.0", "99.0.0-beta.1", "0.9.0"))
        self.assertEqual(sorted(notice.released_versions(src.as_posix(), self.tmp / "home", True)),
                         ["0.5.0", "0.9.0"])

    def test_a_beta_hears_only_of_its_own_release(self):
        """A machine running a beta is told when a release above it is out, and how to leave
        the beta for it; older releases say nothing."""
        w = self.ws()
        self._engines(w.home, "0.5.0", "0.5.1")
        self.assertIsNone(notice.for_project(w.root, w.home, "0.6.0-beta.1"))
        self._engines(w.home, "0.6.0")
        msg = notice.for_project(w.root, w.home, "0.6.0-beta.1")
        self.assertIn("0.6.0 is out (this machine runs beta 0.6.0-beta.1)", msg)
        self.assertIn("govern beta off, then", msg)


    # A newer beta installed beside the one a project runs. Only one local beta plugin exists
    # per machine, so the project still on the older beta is the one to tell.
    def _newer(self, newer: str, running: str) -> str:
        return (f"context-gate: beta {newer} is installed (this project runs beta {running}): "
                f"govern beta on {newer}")

    def test_a_beta_hears_of_a_newer_installed_beta(self):
        w = self.ws()
        self._engines(w.home, "0.5.0", "0.7.0-beta.1", "0.7.0-beta.2", "0.6.9-beta.30")
        self.assertIsNone(notice.for_project(w.root, w.home, "0.7.0-beta.2"))
        self.assertEqual(notice.for_project(w.root, w.home, "0.7.0-beta.1"),
                         self._newer("0.7.0-beta.2", "0.7.0-beta.1"))
        # Newest wins, by number: beta.10 is above beta.9.
        self._engines(w.home, "0.7.0-beta.9", "0.7.0-beta.10")
        self.assertEqual(notice.for_project(w.root, w.home, "0.7.0-beta.1"),
                         self._newer("0.7.0-beta.10", "0.7.0-beta.1"))
        # A directory without an engine in it is not an installed beta.
        (layout.engines_dir(w.home) / "0.7.0-beta.11").mkdir()
        self.assertEqual(notice.for_project(w.root, w.home, "0.7.0-beta.9"),
                         self._newer("0.7.0-beta.10", "0.7.0-beta.9"))

    def test_a_release_above_the_beta_is_said_before_a_newer_beta(self):
        w = self.ws()
        self._engines(w.home, "0.7.0-beta.1", "0.7.0-beta.2", "0.7.0")
        msg = notice.for_project(w.root, w.home, "0.7.0-beta.1")
        self.assertIn("0.7.0 is out (this machine runs beta 0.7.0-beta.1)", msg)
        self.assertNotIn("is installed", msg)

    def test_the_gate_of_an_older_beta_says_a_newer_one_is_installed(self):
        """The gate path: local.toml names N, so engine N runs the command and passes its own
        version as what is running."""
        from unittest import mock
        w = self.ws()
        self._engines(w.home, "99.0.0-beta.1", "99.0.0-beta.2")
        with mock.patch.object(notice, "__version__", "99.0.0-beta.1"):
            code, _, err = w.run("check")
        self.assertEqual(code, 0, err)
        self.assertEqual(err.strip().splitlines()[-1],
                         self._newer("99.0.0-beta.2", "99.0.0-beta.1"))

    def test_a_session_of_an_older_beta_says_a_newer_one_is_installed(self):
        """The session-start path: the hook runs the engine bundled with the one local plugin
        (N+1) and passes no version, so what the project runs is read from its local.toml."""
        w = self.ws()
        self._engines(w.home, "99.0.0-beta.1", "99.0.0-beta.2")
        said = self._newer("99.0.0-beta.2", "99.0.0-beta.1")
        w.write(layout.LOCAL, '[governance]\nengine = "99.0.0-beta.1"\n')
        self.assertEqual(notice.for_project(w.root, w.home), said)
        # local.toml naming the newest beta, or one that is not installed (the gate then runs the
        # committed pin), or nothing readable: no beta is behind.
        for text in ('[governance]\nengine = "99.0.0-beta.2"\n',
                     '[governance]\nengine = "98.0.0-beta.1"\n',
                     f'[governance]\nengine = "{__version__}"\n', "[governance\n", b"\xff"):
            with self.subTest(local=text):
                w.write(layout.LOCAL, text)
                self.assertIsNone(notice.for_project(w.root, w.home))

    def test_a_stable_project_never_hears_of_an_installed_beta(self):
        """No local.toml: whatever betas are installed, and whichever engine asks."""
        from unittest import mock
        w = self.ws()
        self._engines(w.home, "99.0.0-beta.1", "99.0.0-beta.2", f"{__version__}-beta.7")
        self.assertIsNone(notice.for_project(w.root, w.home))
        self.assertIsNone(notice.for_project(w.root, w.home, __version__))
        code, _, err = w.run("check")
        self.assertNotIn("beta", err)
        with mock.patch.object(notice, "__version__", "99.0.0-beta.2"):   # the beta plugin's hook
            self.assertIsNone(notice.for_project(w.root, w.home))


class InstallReportAndUpgrade(Base):
    def project(self):
        w = self.ws()
        cfg = self.tmp / "config.toml"
        shutil.move(str(w.root / CFG), str(cfg))
        (w.root / layout.GOV_DIR).rmdir()
        # An old gate that reports one finding the engine also reports, and one it does not.
        w.write("governance/gate.py", "print('  warn   alpha: something only the old gate says')\n"
                                      "raise SystemExit(0)\n")
        w.write("governance/test_gate.py", "# tests of the old gate\n")
        return w, cfg

    def test_install_writes_report_and_retires(self):
        w, cfg = self.project()
        w.write("projects/alpha/working-files/traps.md", fm("working", status="active")
                + "\n## T-1 — Silent\n\nBody.\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = installer.main(["install", "--root", str(w.root), "--config", str(cfg),
                                   "--entrypoint", "governance/gate.py",
                                   "--retire", "governance/test_gate.py"])
        self.assertEqual(code, 0, out.getvalue())
        self.assertFalse((w.root / "governance/test_gate.py").exists())
        rep = (w.root / layout.GOV_DIR / "install-report.md").read_text(encoding="utf-8")
        self.assertIn("### `trap-entries`", rep)
        self.assertIn("something only the old gate says", rep.split("## No longer reported")[1])
        self.assertIn("decision-log: error", rep)
        with contextlib.redirect_stdout(io.StringIO()):
            installer.main(["uninstall", "--root", str(w.root)])
        self.assertTrue((w.root / "governance/test_gate.py").is_file())

    def test_upgrade_pins_this_engine(self):
        w, cfg = self.project()
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(f'engine = "{__version__}"',
                                                            f'engine = "{SERIES}"'))
        with contextlib.redirect_stdout(io.StringIO()):
            installer.main(["install", "--root", str(w.root), "--config", str(cfg), "--no-report"])
            code = installer.main(["upgrade", "--root", str(w.root), "--no-report"])
        self.assertEqual(code, 0)
        self.assertIn(f'engine = "{__version__}"',
                      (w.root / CFG).read_text(encoding="utf-8"))
        self.assertIn(f'engine = "{__version__}"',
                      (w.root / layout.MANIFEST).read_text(encoding="utf-8"))

    def test_bin_upgrade_fetches_the_newest_release_and_upgrades(self):
        newer = f"{SERIES}.99"
        src = make_source(self.tmp, (__version__, newer))
        w, cfg = self.project()
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            "schema = 1", f'schema = 1\nsource = "{src.as_posix()}"', 1))
        with contextlib.redirect_stdout(io.StringIO()):
            installer.main(["install", "--root", str(w.root), "--config", str(cfg), "--no-report"])
        env = {k: v for k, v in os.environ.items() if k != "GOVERN_ENGINE"}
        env["HOME"] = str(w.home)
        res = subprocess.run([sys.executable, str(w.root / layout.GOV_DIR / "bin" / "upgrade")],
                             env=env, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        self.assertIn(f"upgraded context-gate {__version__} -> {newer}", res.stdout)
        self.assertIn(f'engine = "{newer}"', (w.root / CFG).read_text(encoding="utf-8"))
        self.assertTrue((w.root / layout.GOV_DIR / "upgrade-report.md").is_file())

    def test_bin_upgrade_runs_the_fetched_engine_from_a_directory_holding_another(self):
        # `python3 -m` puts the working directory ahead of PYTHONPATH: run from a directory that
        # holds a `govern/` package (this engine's own source tree), the upgrade loaded that one
        # and reported `X -> X` instead of running the fetched release.
        newer = f"{SERIES}.99"
        src = make_source(self.tmp, (__version__, newer))
        w, cfg = self.project()
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            "schema = 1", f'schema = 1\nsource = "{src.as_posix()}"', 1))
        with contextlib.redirect_stdout(io.StringIO()):
            installer.main(["install", "--root", str(w.root), "--config", str(cfg), "--no-report"])
        env = {k: v for k, v in os.environ.items() if k != "GOVERN_ENGINE"}
        env["HOME"] = str(w.home)
        res = subprocess.run([sys.executable, str(w.root / layout.GOV_DIR / "bin" / "upgrade")],
                             env=env, capture_output=True, text=True,
                             cwd=Path(installer.__file__).resolve().parent.parent)
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        self.assertIn(f"upgraded context-gate {__version__} -> {newer}", res.stdout)


class PublicBetaTagsAtTheSource(Base):
    """Betas are published as prereleases, so a release source's tags hold `vX.Y.Z-beta.N` beside
    its releases. Nothing a stable project runs takes one for a release: not the notice, not
    `bin/upgrade`, not a bootstrap, not a series pin."""

    OWN = f"{__version__}-beta.1"         # a beta of the release this engine is
    AHEAD = f"{NEXT_RELEASE}-beta.3"      # a beta numbered above every release there is
    NEWER = f"{SERIES}.99"                # a release newer than this engine

    def _project(self, tags: tuple, pin: str = __version__):
        """An installed project whose source carries `tags`, pinned to `pin`; nothing is in the
        engines directory yet."""
        src = make_source(self.tmp, tags)
        w = self.ws()
        entry, env = self._installed(w)
        cfg = w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            "schema = 1", f'schema = 1\nsource = "{src.as_posix()}"', 1).replace(
            f'engine = "{__version__}"', f'engine = "{pin}"'))
        return w, entry, env, src

    def _upgrade(self, w, env, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(w.root / layout.GOV_DIR / "bin" / "upgrade"),
                               *args], env=env, capture_output=True, text=True)

    def _engines_installed(self, w) -> list[str]:
        d = layout.engines_dir(w.home)
        return sorted(p.name for p in d.iterdir()) if d.is_dir() else []

    def test_the_notice_offers_only_a_release(self):
        from unittest import mock
        w, _, _, src = self._project((self.OWN, __version__, self.AHEAD))
        with mock.patch.dict(os.environ):
            os.environ.pop(notice.NO_UPDATE_CHECK, None)
            self.assertEqual(notice.released_versions(src.as_posix(), w.home, True),
                             [__version__])
            self.assertEqual(notice.newest_available(w.home, src.as_posix(), fresh=True),
                             __version__)
            # Nothing newer than what the project runs, so nothing is said: no beta is offered.
            self.assertIsNone(notice.for_project(w.root, w.home))
            self.assertIsNone(notice.for_project(w.root, w.home, __version__))
            code, _, err = w.run("check")
            self.assertEqual(code, 0, err)
            self.assertNotIn("beta", err)
            self.assertNotIn("available", err)

    def test_the_notice_names_the_newest_release_past_newer_betas(self):
        from unittest import mock
        w, _, _, src = self._project((__version__, self.NEWER, f"{self.NEWER}-beta.2",
                                      self.AHEAD))
        with mock.patch.dict(os.environ):
            os.environ.pop(notice.NO_UPDATE_CHECK, None)
            self.assertEqual(notice.newest_available(w.home, src.as_posix(), fresh=True),
                             self.NEWER)
            said = notice.for_project(w.root, w.home, __version__)
        self.assertIn(f"{self.NEWER} available (this project runs {__version__})", said)
        self.assertNotIn("beta", said)

    def test_bin_upgrade_takes_the_newest_release_past_newer_betas(self):
        w, _, env, _ = self._project((self.OWN, __version__, self.NEWER, self.AHEAD))
        res = self._upgrade(w, env)
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        self.assertIn(f"upgraded context-gate {__version__} -> {self.NEWER}", res.stdout)
        self.assertIn(f'engine = "{self.NEWER}"', (w.root / CFG).read_text(encoding="utf-8"))
        self.assertEqual([v for v in self._engines_installed(w) if "beta" in v], [])

    def test_bin_upgrade_with_only_betas_above_the_pin_stays_where_it_is(self):
        w, _, env, _ = self._project((self.OWN, __version__, self.AHEAD))
        before = (w.root / CFG).read_bytes()
        res = self._upgrade(w, env)
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        self.assertEqual(res.stdout.strip(), f"already on context-gate {__version__}")
        self.assertEqual((w.root / CFG).read_bytes(), before)
        self.assertEqual(self._engines_installed(w), [])

    def test_bin_upgrade_to_a_named_beta_never_pins_it(self):
        """Even asked for by name, a beta does not become the committed pin: the project stays
        on its release."""
        w, _, env, _ = self._project((__version__, self.AHEAD))
        before = (w.root / CFG).read_bytes()
        res = self._upgrade(w, env, "--to", self.AHEAD)
        self.assertEqual(res.returncode, 2, res.stderr + res.stdout)
        self.assertNotIn("Traceback", res.stderr)
        self.assertEqual(res.stderr.strip().splitlines(),
                         [f"error: an upgrade takes a release, and {self.AHEAD} is a beta; "
                          f"run a beta with govern beta on {self.AHEAD}"])
        self.assertEqual((w.root / CFG).read_bytes(), before)
        self.assertEqual(self._engines_installed(w), [])      # nothing was downloaded

    def test_a_missing_engine_bootstraps_the_pinned_release_beside_beta_tags(self):
        w, entry, env, _ = self._project((self.OWN, __version__, self.AHEAD))
        res = subprocess.run([sys.executable, str(entry), "check"], env=env,
                             capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        self.assertIn(f"installing engine {__version__} from", res.stderr)
        self.assertNotIn("beta", res.stderr)
        self.assertEqual(self._engines_installed(w), [__version__])

    def test_a_series_pin_runs_and_upgrades_to_a_release_only(self):
        w, entry, env, _ = self._project((self.OWN, __version__, self.AHEAD), pin=SERIES)
        # Betas of the series, and one above it, installed beside its releases.
        self._engines(w, (f"{SERIES}.0", __version__, BETA, self.OWN, self.AHEAD))
        self.assertTrue(self._which(entry, env).endswith(__version__))
        self.assertEqual(notice.resolve(SERIES, w.home), __version__)
        res = self._upgrade(w, env)
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        self.assertIn(f'engine = "{__version__}"', (w.root / CFG).read_text(encoding="utf-8"))
        self.assertNotIn("beta", res.stdout)


if __name__ == "__main__":
    unittest.main()
