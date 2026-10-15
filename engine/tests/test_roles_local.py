"""Rendered roles on one machine only: `agent-roles` on in `local.toml` alone.

    python3 -m unittest discover -s engine/tests -k roles
"""
from __future__ import annotations

import copy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))

import govern.checks  # noqa: E402,F401  (registers the built-in checks)
from govern import config, layout, roles  # noqa: E402
from test_roles import AGENTS, ON, RULES, raw, snapshot, record_install  # noqa: E402
from test_single_repo import CFG, Base, SingleRepo, wtext  # noqa: E402


# A beta's local.toml names the beta it is for, or the engine never reads it.
BETA = "0.7.0-beta.9"
LOCAL_HEAD = f'[governance]\nengine = "{BETA}"\n\n'
LOCAL_ON = LOCAL_HEAD + '[checks.agent-roles]\nlevel = "error"\n'
LOCAL_OFF = LOCAL_HEAD + '[checks.agent-roles]\nlevel = "off"\n'
BLOCK_START = "# context-gate: rendered on this machine"


def git(root: Path, *args: str) -> str:
    """Run git in `root`, failing the test when git does."""
    res = subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@e.x",
                          "-c", "commit.gpgsign=false", *args],
                         capture_output=True, text=True, encoding="utf-8")
    if res.returncode:
        raise AssertionError(f"git {' '.join(args)}: {res.stderr}")
    return res.stdout


class LocalOnlyRender(Base):
    """`agent-roles` on in `local.toml` alone: the files stay on this machine."""

    def setUp(self) -> None:
        super().setUp()
        patch = mock.patch.object(config, "__version__", BETA)   # only a beta reads local.toml
        patch.start()
        self.addCleanup(patch.stop)

    def project(self, repo: bool = True, local: str = LOCAL_ON, **kw) -> SingleRepo:
        r = self.repo(**kw)
        record_install(r.root)
        r.write(layout.LOCAL, local)
        if repo:
            git(r.root, "init", "-q")
            with open(r.root / ".git/info/exclude", "a", encoding="utf-8", newline="") as fh:
                fh.write(f"{layout.LOCAL}\n")
            git(r.root, "add", "-A")
            git(r.root, "commit", "-q", "-m", "start")
        return r

    def exclude(self, r: SingleRepo) -> Path:
        return r.root / ".git/info/exclude"

    def test_files_stay_on_this_machine_and_a_second_render_changes_no_byte(self):
        r = self.project()
        code, out, err = r.run("agents", "render")
        self.assertEqual(code, 0, out + err)
        self.assertIn("  recorded    6 file(s) in .context-gate/rendered.local.toml\n", out)
        self.assertIn("  excluded    7 path(s) in .git/info/exclude\n", out)
        self.assertNotIn("tracked", out)
        self.assertEqual(git(r.root, "status", "--short"), "")
        block = raw(self.exclude(r))
        self.assertIn(f"{BLOCK_START}\n/{AGENTS}/code-reviewer.md\n/{AGENTS}/developer.md\n",
                      block)
        self.assertIn(f"/{RULES}\n", block)
        self.assertIn(f"/{layout.GOV_DIR}/rendered.local.toml\n# context-gate: end\n", block)
        self.assertEqual(roles._rows(r.root / layout.MANIFEST), [])
        self.assertEqual(len(roles._rows(r.root / roles.LOCAL_RECORD)), 6)
        code, out, _ = r.run("agents", "render", "--check")
        self.assertEqual(code, 0, out)
        self.assertIn("(0 file(s) would change)", out)
        files = [self.exclude(r), r.root / layout.MANIFEST, r.root / roles.LOCAL_RECORD,
                 r.root / AGENTS / "developer.md"]
        before = [p.read_bytes() for p in files]
        code, out, _ = r.run("agents", "render")
        self.assertEqual(code, 0, out)
        self.assertNotIn("recorded", out)
        self.assertNotIn("excluded", out)
        self.assertEqual([p.read_bytes() for p in files], before)

    def test_check_counts_a_block_that_would_change(self):
        r = self.project()
        r.run("agents", "render")
        wtext(self.exclude(r), "")
        code, out, _ = r.run("agents", "render", "--check")
        self.assertEqual(code, 1, out)
        self.assertIn("  exclude     .git/info/exclude\n", out)
        self.assertIn("(1 file(s) would change)", out)
        self.assertEqual(raw(self.exclude(r)), "")

    def test_other_lines_of_the_exclude_file_survive_byte_for_byte(self):
        r = self.project()
        old = "﻿*.log\r\n# note"
        wtext(self.exclude(r), old)
        code, out, _ = r.run("agents", "render")
        self.assertEqual(code, 0, out)
        got = raw(self.exclude(r))
        self.assertTrue(got.startswith(old + "\r\n" + BLOCK_START + "\r\n"), repr(got))
        self.assertEqual(got.count("\n"), got.count("\r\n"))
        before = self.exclude(r).read_bytes()
        r.run("agents", "render")
        self.assertEqual(self.exclude(r).read_bytes(), before)
        roles.set_block(self.exclude(r), [])
        self.assertEqual(raw(self.exclude(r)), old + "\r\n")

    def test_a_linked_worktree_writes_the_main_repositorys_exclude_file(self):
        r = self.project()
        wt = self.tmp / "linked"
        git(r.root, "worktree", "add", "-q", str(wt))
        w = copy.copy(r)
        w.root = wt
        w.write(layout.LOCAL, LOCAL_ON)
        code, out, err = w.run("agents", "render")
        self.assertEqual(code, 0, out + err)
        self.assertTrue((wt / ".git").is_file())
        self.assertIn(f"/{AGENTS}/developer.md\n", raw(self.exclude(r)))
        self.assertEqual(git(wt, "status", "--short"), "")

    def test_files_git_already_tracks_are_named_and_untrack_stops_it(self):
        r = self.project()
        r.run("agents", "render")
        git(r.root, "add", "-f", AGENTS, RULES)
        git(r.root, "commit", "-q", "-m", "committed")
        code, out, _ = r.run("agents", "render")
        self.assertEqual(code, 0, out)
        self.assertEqual(out.count("  tracked     "), 1)
        self.assertIn(f"{AGENTS}/developer.md", out)
        self.assertIn("run govern agents render --untrack to stop committing them", out)
        code, out, _ = r.run("agents", "render", "--check")
        self.assertEqual(code, 0, out)
        self.assertIn("  tracked     ", out)
        code, out, err = r.run("agents", "render", "--untrack")
        self.assertEqual(code, 0, out + err)
        self.assertIn(f"  untracked   {AGENTS}/", out)
        listed = git(r.root, "ls-files", AGENTS, RULES)
        self.assertNotIn("developer.md", listed)
        self.assertNotIn("build.md", listed)
        self.assertIn("reviewer.md", listed)
        self.assertTrue((r.root / AGENTS / "developer.md").is_file())
        self.assertTrue((r.root / RULES).is_file())
        code, out, _ = r.run("agents", "render", "--untrack")
        self.assertEqual(code, 0, out)
        self.assertNotIn("untracked", out)
        self.assertNotIn("  tracked     ", out)

    def test_a_param_set_only_locally_is_refused_when_committed_config_has_it_on(self):
        r = self.project(extra=ON, local=LOCAL_HEAD + '[checks.agent-roles]\nfull_gate = "make all"\n')
        before = snapshot(r.root)
        code, out, _ = r.run("agents", "render")
        self.assertEqual(code, 1, out)
        self.assertIn(f"  refused     full_gate is set in {layout.LOCAL} and would be rendered "
                      f"into committed files: move it to {layout.CONFIG}\n", out)
        self.assertIn("[FAIL] agents render  (nothing written: fix the settings above)", out)
        self.assertNotIn("level", out)
        code, out, _ = r.run("agents", "render", "--check")
        self.assertEqual(code, 1, out)
        self.assertIn("  refused     full_gate", out)
        self.assertEqual(snapshot(r.root), before)

    def test_committed_on_records_in_the_manifest_alone(self):
        r = self.project(extra=ON, local=LOCAL_HEAD)
        code, out, err = r.run("agents", "render")
        self.assertEqual(code, 0, out + err)
        self.assertIn(f"file(s) in {layout.MANIFEST}\n", out)
        self.assertEqual(len(roles._rows(r.root / layout.MANIFEST)), 6)
        self.assertFalse((r.root / roles.LOCAL_RECORD).exists())
        self.assertNotIn(BLOCK_START, raw(self.exclude(r)))
        self.assertNotIn("excluded", out)

    def test_moving_the_setting_to_committed_config_drops_the_local_record_and_block(self):
        r = self.project()
        r.run("agents", "render")
        r.write(layout.LOCAL, LOCAL_HEAD)
        cfg = r.root / CFG
        wtext(cfg, raw(cfg) + ON)
        code, out, err = r.run("agents", "render")
        self.assertEqual(code, 0, out + err)
        self.assertFalse((r.root / roles.LOCAL_RECORD).exists())
        self.assertNotIn(BLOCK_START, raw(self.exclude(r)))
        self.assertEqual(len(roles._rows(r.root / layout.MANIFEST)), 6)

    def test_turning_it_off_removes_the_files_the_record_and_the_block(self):
        r = self.project()
        r.run("agents", "render")
        edited = r.root / AGENTS / "qa-engineer.md"
        wtext(edited, raw(edited) + "\nOurs.\n")
        r.write(layout.LOCAL, LOCAL_OFF)
        code, out, err = r.run("agents", "render")
        self.assertEqual(code, 0, out + err)
        self.assertIn(f"  removed     {AGENTS}/developer.md\n", out)
        self.assertIn(f"  kept        {AGENTS}/qa-engineer.md (changed since it was rendered)\n",
                      out)
        self.assertIn("agent-roles is off: nothing rendered.", out)
        self.assertFalse((r.root / AGENTS / "developer.md").exists())
        self.assertFalse((r.root / RULES).exists())
        self.assertTrue(edited.is_file())
        self.assertFalse((r.root / roles.LOCAL_RECORD).exists())
        self.assertNotIn(BLOCK_START, raw(self.exclude(r)))

    def test_outside_a_git_repository_it_records_locally_and_says_so(self):
        r = self.project(repo=False)
        code, out, err = r.run("agents", "render")
        self.assertEqual(code, 0, out + err)
        self.assertIn("  note        not a git repository: rendered files recorded locally, "
                      "nothing excluded\n", out)
        self.assertEqual(len(roles._rows(r.root / roles.LOCAL_RECORD)), 6)
        self.assertIsNone(roles.exclude_file(r.root))
        code, out, _ = r.run("agents", "render", "--check")
        self.assertEqual(code, 0, out)

    def test_untrack_needs_the_setting_to_be_local_only(self):
        want = f"error: --untrack applies only when agent-roles is on in {layout.LOCAL} alone"
        r = self.project(extra=ON, local=LOCAL_HEAD)
        before = snapshot(r.root)
        code, out, err = r.run("agents", "render", "--untrack")
        self.assertEqual(code, 1, out)
        self.assertIn(want, err)
        self.assertEqual(snapshot(r.root), before)
        r = self.project_again()
        before = snapshot(r.root)
        code, out, err = r.run("agents", "render", "--check", "--untrack")
        self.assertEqual(code, 1, out)
        self.assertIn(want, err)
        self.assertEqual(snapshot(r.root), before)

    def project_again(self) -> SingleRepo:
        """A second local-only project beside the first, in its own folder."""
        self._tmp2 = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp2.cleanup)
        r = SingleRepo(Path(self._tmp2.name))
        r.write(layout.LOCAL, LOCAL_ON)
        return r

    def test_an_exclude_block_with_no_end_is_reported_and_nothing_is_changed(self):
        r = self.project()
        wtext(self.exclude(r), f"{BLOCK_START}\n/x\n")
        code, out, err = r.run("agents", "render")
        self.assertEqual(code, 1, out)
        self.assertIn("never ends", err)
        self.assertEqual(raw(self.exclude(r)), f"{BLOCK_START}\n/x\n")

    # ------------------------------------------------------------ the fix pass

    def test_level_in_both_layers_behaves_as_committed_on(self):
        r = self.project(extra=ON, local=LOCAL_ON)
        code, out, err = r.run("agents", "render")
        self.assertEqual(code, 0, out + err)
        self.assertIn(f"file(s) in {layout.MANIFEST}\n", out)
        self.assertEqual(len(roles._rows(r.root / layout.MANIFEST)), 6)
        self.assertFalse((r.root / roles.LOCAL_RECORD).exists())
        self.assertNotIn(BLOCK_START, raw(self.exclude(r)))
        self.assertNotIn("tracked", out)
        self.assertNotIn("excluded", out)
        code, out, err = r.run("agents", "render", "--untrack")
        self.assertEqual(code, 1, out)
        self.assertIn("--untrack applies only when agent-roles is on", err)

    def test_git_that_cannot_run_in_a_repository_stops_the_render(self):
        r = self.project()
        before = snapshot(r.root)
        with mock.patch.object(roles, "_git", return_value=None):
            code, out, err = r.run("agents", "render")
        self.assertEqual(code, 1, out)
        self.assertIn("error: git: ", err)
        self.assertNotIn("not a git repository", out)
        self.assertEqual(snapshot(r.root), before)
        self.assertFalse((r.root / roles.LOCAL_RECORD).exists())
        self.assertFalse((r.root / AGENTS / "developer.md").exists())

    def test_git_that_cannot_run_fails_check_too(self):
        r = self.project()
        with mock.patch.object(roles, "_git", return_value=None):
            code, out, err = r.run("agents", "render", "--check")
        self.assertEqual(code, 1, out)
        self.assertIn("error: git: ", err)

    def test_tracked_failing_is_an_error_not_an_empty_answer(self):
        r = self.project()
        with mock.patch.object(roles, "tracked", side_effect=roles.GitFailed("lock")):
            code, out, err = r.run("agents", "render")
        self.assertEqual(code, 1, out)
        self.assertIn("error: git: lock", err)

    def test_untrack_failing_is_an_error(self):
        r = self.project()
        r.run("agents", "render")
        git(r.root, "add", "-f", AGENTS, RULES)
        git(r.root, "commit", "-q", "-m", "committed")
        with mock.patch.object(roles, "untrack_files", side_effect=roles.GitFailed("lock")):
            code, out, err = r.run("agents", "render", "--untrack")
        self.assertEqual(code, 1, out)
        self.assertIn("error: git: lock", err)

    def test_a_plain_folder_still_renders_with_the_note(self):
        r = self.project(repo=False)
        code, out, err = r.run("agents", "render")
        self.assertEqual(code, 0, out + err)
        self.assertIn("note        not a git repository", out)

    def test_turning_it_off_leaves_files_git_still_tracks(self):
        r = self.project()
        r.run("agents", "render")
        git(r.root, "add", "-f", AGENTS, RULES)
        git(r.root, "commit", "-q", "-m", "committed")
        r.write(layout.LOCAL, LOCAL_OFF)
        code, out, err = r.run("agents", "render")
        self.assertEqual(code, 0, out + err)
        self.assertIn(f"  kept        {AGENTS}/developer.md (git tracks it)\n", out)
        self.assertTrue((r.root / AGENTS / "developer.md").is_file())
        self.assertNotIn(" D", git(r.root, "status", "--short"))

    def test_a_failed_block_removal_keeps_the_record_for_the_next_run(self):
        r = self.project()
        r.run("agents", "render")
        r.write(layout.LOCAL, LOCAL_OFF)
        with mock.patch.object(roles, "set_block", side_effect=OSError("disk")):
            code, out, err = r.run("agents", "render")
        self.assertEqual(code, 1, out)
        self.assertTrue((r.root / roles.LOCAL_RECORD).is_file())
        code, out, err = r.run("agents", "render")
        self.assertEqual(code, 0, out + err)
        self.assertFalse((r.root / roles.LOCAL_RECORD).exists())
        self.assertNotIn(BLOCK_START, raw(self.exclude(r)))


class WhyNotRendered(Base):
    """A missing or stale role file says why, by where the roles are kept."""

    project = LocalOnlyRender.project

    def committed(self) -> SingleRepo:
        (self.tmp / "committed").mkdir()
        r = SingleRepo(self.tmp / "committed", extra=ON)
        record_install(r.root)
        code, out, err = r.run("agents", "render")
        self.assertEqual(code, 0, out + err)
        return r

    def local(self) -> SingleRepo:
        patch = mock.patch.object(config, "__version__", BETA)   # only a beta reads local.toml
        patch.start()
        self.addCleanup(patch.stop)
        r = self.project()
        code, out, err = r.run("agents", "render")
        self.assertEqual(code, 0, out + err)
        return r

    def test_lost_local_file_says_a_checkout_removes_it(self):
        r = self.local()
        (r.root / AGENTS / "developer.md").unlink()
        code, out, _ = r.run("check")
        self.assertEqual(code, 1, out)
        self.assertIn(f"({roles.LOST_NOTE})", out)
        code, out, _ = r.run("agents", "render", "--check")
        self.assertEqual(code, 1, out)
        self.assertIn(f"  note        {roles.LOST_NOTE}\n  note        run {roles.RENDER}\n"
                      f"[FAIL]", out)
        self.assertNotIn(roles.BRANCH_NOTE, out)

    def test_stale_committed_file_names_the_branch_fix(self):
        r = self.committed()
        path = r.root / AGENTS / "developer.md"
        wtext(path, raw(path) + "\nan edit\n")
        code, out, _ = r.run("check")
        self.assertEqual(code, 1, out)
        self.assertIn(f"({roles.BRANCH_NOTE})", out)
        code, out, _ = r.run("agents", "render", "--check")
        self.assertEqual(code, 1, out)
        self.assertIn(f"  note        {roles.BRANCH_NOTE}\n  note        run {roles.RENDER}\n"
                      f"[FAIL]", out)
        self.assertNotIn(roles.LOST_NOTE, out)

    def test_passing_check_prints_no_note(self):
        r = self.committed()
        code, out, _ = r.run("agents", "render", "--check")
        self.assertEqual(code, 0, out)
        self.assertNotIn("note", out)
        r = self.local()
        code, out, _ = r.run("agents", "render", "--check")
        self.assertEqual(code, 0, out)
        self.assertNotIn("note", out)

    def test_other_cases_get_the_command_only(self):
        r = self.committed()      # built first: `local` patches the version for the rest
        (r.root / AGENTS / "developer.md").unlink()
        code, out, _ = r.run("agents", "render", "--check")
        self.assertEqual(code, 1, out)
        self.assertIn(f"  note        run {roles.RENDER}\n", out)
        code, out2, _ = r.run("check")
        for text in (out, out2):
            self.assertNotIn(roles.LOST_NOTE, text)
            self.assertNotIn(roles.BRANCH_NOTE, text)
        r = self.local()
        path = r.root / AGENTS / "developer.md"
        wtext(path, raw(path) + "\nan edit\n")
        code, out, _ = r.run("agents", "render", "--check")
        self.assertEqual(code, 1, out)
        self.assertIn(f"  note        run {roles.RENDER}\n", out)
        code, out2, _ = r.run("check")
        for text in (out, out2):
            self.assertNotIn(roles.LOST_NOTE, text)
            self.assertNotIn(roles.BRANCH_NOTE, text)

    def test_conflict_only_failure_gets_no_render_note(self):
        r = self.committed()
        wtext(r.root / AGENTS / "developer.md", "written by hand\n")
        code, out, _ = r.run("agents", "render", "--check")
        self.assertEqual(code, 1, out)
        self.assertIn("  conflict    ", out)
        self.assertIn("(0 file(s) would change, 1 conflict(s))", out)
        self.assertNotIn("  note        ", out)
