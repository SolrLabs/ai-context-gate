"""Behaviour tests on small throwaway workspaces: defaults, agent worktrees, decision history, profiles and project-scoped checks."""
from __future__ import annotations

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

class Defaults(Base):
    """The engine's neutral defaults for agents and working files: stricter house rules belong
    in a principles profile, not in the engine."""

    def commit_all(self, w: "Workspace", when: str | None = None) -> None:
        env = {**os.environ}
        if when:
            env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = when
        for cmd in (["init", "-q"], ["add", "-A"],
                    ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"]):
            subprocess.run(["git", "-C", str(w.root), *cmd], check=True, env=env)

    # ---------------------------------------------------------------- agents

    def test_missing_omit_claude_md_is_a_warning_by_default(self):
        w = self.ws()
        w.write(".claude/agents/helper.md", AGENT.format(extra="", body="body")
                .replace("omitClaudeMd: true\n", ""))
        code, out, _ = w.run("check")
        self.assertEqual(code, 0, out)
        self.assertIn("warn   agents/helper.md: frontmatter missing 'omitClaudeMd'", out)

    def test_warn_keys_empty_makes_it_an_error(self):
        w = self.ws(extra="\n[checks.agents]\nwarn_keys = []\n")
        w.write(".claude/agents/helper.md", AGENT.format(extra="", body="body")
                .replace("omitClaudeMd: true\n", ""))
        code, out, _ = w.run("check")
        self.assertEqual(code, 1)
        self.assertIn("ERROR  agents/helper.md: frontmatter missing 'omitClaudeMd'", out)

    def test_max_turns_optional_by_default(self):
        w = self.ws()
        w.write(".claude/agents/helper.md", AGENT.format(extra="", body="body"))
        code, out, _ = w.run("check")
        self.assertEqual(code, 0, out)

    def test_require_max_turns_flags_a_missing_cap(self):
        w = self.ws(extra="\n[checks.agents]\nrequire_max_turns = true\n")
        w.write(".claude/agents/helper.md", AGENT.format(extra="", body="body"))
        code, out, _ = w.run("check")
        self.assertIn("agents/helper.md: no maxTurns — set a cap high enough that a properly "
                      "performing agent never hits it, but a runaway agent is stopped for "
                      "review", out)

    def test_max_turns_zero_means_no_ceiling(self):
        w = self.ws()
        w.write(".claude/agents/helper.md",
                AGENT.format(extra="maxTurns: 5000\n", body="body"))
        code, out, _ = w.run("check")
        self.assertEqual(code, 0, out)

    def test_positive_ceiling_still_enforced(self):
        w = self.ws(extra="\n[checks.agents]\nmax_turns = 50\n")
        w.write(".claude/agents/helper.md",
                AGENT.format(extra="maxTurns: 100\n", body="body"))
        code, out, _ = w.run("check")
        self.assertIn("agents/helper.md: maxTurns 100 exceeds agents max_turns=50", out)

    def test_max_turns_must_be_a_positive_integer(self):
        w = self.ws()
        w.write(".claude/agents/helper.md", AGENT.format(extra="maxTurns: 0\n", body="body"))
        code, out, _ = w.run("check")
        self.assertIn("agents/helper.md: maxTurns '0' is not a positive integer", out)

    def test_malformed_max_turns_warns_not_errors(self):
        # A warning, not an error: an existing project carrying one must not turn red on
        # upgrade.
        for i, extra in enumerate(("maxTurns: 0\n", "maxTurns: -5\n", "maxTurns: soon\n")):
            (self.tmp / f"case{i}").mkdir()
            w = Workspace(self.tmp / f"case{i}")
            w.write(".claude/agents/helper.md", AGENT.format(extra=extra, body="body"))
            code, out, _ = w.run("check")
            self.assertEqual(code, 0, out)
            self.assertIn("warn   agents/helper.md: maxTurns", out)
            self.assertNotIn("ERROR  agents/helper.md: maxTurns", out)

    def test_malformed_max_turns_with_prose_turn_count_reports_once(self):
        w = self.ws()
        w.write(".claude/agents/helper.md",
                AGENT.format(extra="maxTurns: 0\n", body="Stop after 10 turns."))
        code, out, _ = w.run("check")
        self.assertIn("maxTurns '0' is not a positive integer", out)
        self.assertNotIn("no maxTurns to match", out)

    def test_agent_prose_must_match_by_default(self):
        w = self.ws()
        w.write(".claude/agents/helper.md",
                AGENT.format(extra="maxTurns: 10\n", body="Stop well before 10 turns."))
        code, out, _ = w.run("check")
        self.assertEqual(code, 0, out)
        w.write(".claude/agents/helper.md",
                AGENT.format(extra="maxTurns: 10\n", body="Stop well before 20 turns."))
        code, out, _ = w.run("check")
        self.assertIn("agents/helper.md: prose says 20 turns, frontmatter says 10", out)

    def test_max_turns_false_is_a_stated_no_cap(self):
        # It meets require_max_turns, has no ceiling to exceed and is not a malformed number.
        w = self.ws(extra="\n[checks.agents]\nrequire_max_turns = true\nmax_turns = 50\n")
        for value in ("false", "False", '"false"'):
            w.write(".claude/agents/helper.md",
                    AGENT.format(extra=f"maxTurns: {value}\n", body="body"))
            code, out, _ = w.run("check")
            self.assertEqual(code, 0, out)
            self.assertNotIn("maxTurns", out)

    def test_max_turns_false_still_has_to_be_written(self):
        w = self.ws(extra="\n[checks.agents]\nrequire_max_turns = true\n")
        w.write(".claude/agents/helper.md", AGENT.format(extra="", body="body"))
        code, out, _ = w.run("check")
        self.assertEqual(code, 1, out)
        self.assertIn("ERROR  agents/helper.md: no maxTurns", out)

    def test_any_other_non_integer_max_turns_is_still_a_warning(self):
        w = self.ws(extra="\n[checks.agents]\nrequire_max_turns = true\n")
        for value in ("true", "no", "none"):
            w.write(".claude/agents/helper.md",
                    AGENT.format(extra=f"maxTurns: {value}\n", body="body"))
            code, out, _ = w.run("check")
            self.assertEqual(code, 0, out)
            self.assertIn(f"warn   agents/helper.md: maxTurns '{value}' is not an integer", out)

    def test_a_prose_turn_count_beside_max_turns_false_contradicts_it(self):
        w = self.ws()
        w.write(".claude/agents/helper.md",
                AGENT.format(extra="maxTurns: false\n", body="Stop after 10 turns."))
        code, out, _ = w.run("check")
        self.assertEqual(code, 0, out)       # a warning before maxTurns: false was understood
        self.assertIn("warn   agents/helper.md: prose says 10 turns, frontmatter says no cap "
                      "(maxTurns: false)", out)
        self.assertNotIn("no maxTurns to match", out)

    # ---------------------------------------------------------- working files

    def test_finished_files_warn_by_default(self):
        w = self.ws()
        w.write("projects/alpha/working-files/plan.md",
                fm("working", status="complete") + "body\n")
        self.commit_all(w, "2020-01-01T00:00:00")
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertEqual(code, 0, out)
        self.assertIn("warn   ", out)
        self.assertIn("plan.md: status 'complete'", out)

    def test_never_committed_finished_file_is_flagged_instead_of_aged(self):
        w = self.ws()
        self.commit_all(w)
        w.write("projects/alpha/working-files/plan.md",
                fm("working", status="complete") + "body\n")
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertIn("plan.md is finished but was never committed — commit it once so git "
                      "keeps it, then delete it", out)
        self.assertNotIn("last touched", out)

    def test_max_days_zero_reports_a_committed_file_at_once(self):
        w = self.ws(extra="\n[checks.finished-files]\nmax_days = 0\n")
        w.write("projects/alpha/working-files/plan.md",
                fm("working", status="complete") + "body\n")
        self.commit_all(w)
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertIn("plan.md: status 'complete', last touched 0d ago "
                      "(finished-files max_days=0)", out)

    def test_working_file_count_warns_above_twelve(self):
        w = self.ws()
        for i in range(13):
            w.write(f"projects/alpha/working-files/f{i}.md",
                    fm("working", status="active") + "x\n")
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertEqual(code, 0, out)
        self.assertIn("warn   alpha: 13 files in working-files/ exceeds "
                      "working-file-count max_files=12", out)


class AgentWorktrees(Base):
    """An agent worktree under `.claude/worktrees/` that a checkout does not ignore is swept into
    its next `git add -A`. Each checkout's own repo is asked, never the workspace root's."""

    NOT_IGNORED = "does not ignore .claude/worktrees/; an agent worktree there is swept into"

    def git_init(self, where: Path, ignore: str | None = None) -> None:
        where.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-C", str(where), "init", "-q"], check=True)
        if ignore is not None:
            wtext(where / ".gitignore", ignore)

    def member_ws(self, *, root_ignore: str, alpha_ignore: str | None) -> "Workspace":
        # The root ignores the member's whole directory, the way a workspace root keeps its
        # members out of its own commits: asking the root about a member path would answer
        # "ignored" whatever the member's own .gitignore says.
        w = self.ws()
        self.git_init(w.root, root_ignore)
        self.git_init(w.root / "alpha", alpha_ignore)
        w.write(".claude/agents/helper.md", AGENT.format(extra="", body="body"))
        return w

    def test_member_that_does_not_ignore_worktrees_warns(self):
        w = self.member_ws(root_ignore="alpha/\n.claude/worktrees/\n", alpha_ignore=None)
        code, out, _ = w.run("check")
        self.assertEqual(code, 0, out)
        self.assertIn(f"alpha: 'alpha' {self.NOT_IGNORED}", out)
        self.assertEqual(out.count(self.NOT_IGNORED), 1, out)

    def test_member_that_ignores_worktrees_is_clean(self):
        w = self.member_ws(root_ignore="alpha/\n.claude/worktrees/\n",
                           alpha_ignore=".claude/worktrees/\n")
        code, out, _ = w.run("check")
        self.assertEqual(code, 0, out)
        self.assertNotIn(self.NOT_IGNORED, out)
        self.assertNotIn("could not ask git", out)

    def test_workspace_root_that_does_not_ignore_worktrees_warns(self):
        w = self.member_ws(root_ignore="alpha/\n", alpha_ignore=".claude/worktrees/\n")
        code, out, _ = w.run("check")
        self.assertIn(f"workspace: '.' {self.NOT_IGNORED}", out)
        self.assertEqual(out.count(self.NOT_IGNORED), 1, out)

    def test_member_with_its_own_agents_fires_without_root_agents(self):
        w = self.ws()
        self.git_init(w.root, "alpha/\n")
        self.git_init(w.root / "alpha")
        w.write("alpha/.claude/agents/helper.md", AGENT.format(extra="", body="body"))
        code, out, _ = w.run("check")
        self.assertIn(f"alpha: 'alpha' {self.NOT_IGNORED}", out)
        self.assertNotIn("workspace: '.'", out)

    def test_no_agents_dir_no_finding(self):
        w = self.ws()
        self.git_init(w.root)
        self.git_init(w.root / "alpha")
        code, out, _ = w.run("check")
        self.assertNotIn(self.NOT_IGNORED, out)
        self.assertNotIn("could not ask git", out)

    def test_git_failure_is_a_warning_naming_it_never_clean(self):
        w = self.member_ws(root_ignore="alpha/\n", alpha_ignore=None)
        wtext(w.home / ".gitconfig", "[bad\n")    # every git call now fails: exit 128
        code, out, _ = w.run("check")
        self.assertEqual(code, 0, out)
        self.assertIn("alpha: could not ask git whether 'alpha' ignores .claude/worktrees/ "
                      "(fatal: bad config", out)
        self.assertNotIn(self.NOT_IGNORED, out)

    def test_a_root_that_is_not_a_repo_is_skipped_its_members_are_not(self):
        w = self.ws()                  # a plain folder of repos, with agents at the top
        self.git_init(w.root / "alpha")
        w.write(".claude/agents/helper.md", AGENT.format(extra="", body="body"))
        code, out, _ = w.run("check")
        self.assertEqual(code, 0, out)
        self.assertNotIn("could not ask git", out)
        self.assertNotIn("workspace: '.'", out)
        self.assertIn(f"alpha: 'alpha' {self.NOT_IGNORED}", out)

    def test_repo_env_from_a_hook_never_redirects_the_question(self):
        # A pre-commit hook in a linked worktree exports GIT_DIR; `git -C alpha` would then ask
        # that repo, which here ignores the worktrees, instead of alpha's own.
        w = self.member_ws(root_ignore="alpha/\n.claude/worktrees/\n", alpha_ignore=None)
        other = self.tmp / "other"
        self.git_init(other)
        wtext(other / ".git" / "info" / "exclude", ".claude/worktrees/\n")
        old = os.environ.get("GIT_DIR")
        os.environ["GIT_DIR"] = str(other / ".git")
        try:
            code, out, _ = w.run("check")
        finally:
            if old is None:
                del os.environ["GIT_DIR"]
            else:
                os.environ["GIT_DIR"] = old
        self.assertIn(f"alpha: 'alpha' {self.NOT_IGNORED}", out)

    def test_a_global_excludes_file_does_not_hide_it(self):
        w = self.member_ws(root_ignore="alpha/\n.claude/worktrees/\n", alpha_ignore=None)
        excludes = w.home / "excludes"
        wtext(excludes, ".claude/worktrees/\n")
        wtext(w.home / ".gitconfig", f"[core]\n\texcludesFile = {excludes.as_posix()}\n")
        code, out, _ = w.run("check")
        self.assertIn(f"alpha: 'alpha' {self.NOT_IGNORED}", out)

    # A monorepo member: a directory with no repo of its own. Its agents' worktrees land at the
    # root repo's top, so the root's pass answers for it.

    def monorepo(self, root_ignore: str) -> "Workspace":
        w = self.ws()
        self.git_init(w.root, root_ignore)
        w.write("alpha/.claude/agents/helper.md", AGENT.format(extra="", body="body"))
        return w

    def test_monorepo_member_with_agents_fires_the_roots_pass_once(self):
        w = self.monorepo("")
        code, out, _ = w.run("check")
        self.assertIn(f"workspace: '.' {self.NOT_IGNORED}", out)
        self.assertEqual(out.count(self.NOT_IGNORED), 1, out)

    def test_monorepo_member_is_clean_when_the_root_ignores_worktrees(self):
        w = self.monorepo(".claude/worktrees/\n")
        code, out, _ = w.run("check")
        self.assertNotIn(self.NOT_IGNORED, out)
        self.assertNotIn("could not ask git", out)

    def test_monorepo_member_is_never_asked_about_its_own_path(self):
        # The root ignores only its own top-level worktrees, which is where they land; asking
        # about alpha/.claude/worktrees/ would warn about a directory nothing creates.
        w = self.monorepo("/.claude/worktrees/\n")
        code, out, _ = w.run("check")
        self.assertNotIn(self.NOT_IGNORED, out)

    def test_single_repo_is_asked_once(self):
        root, home = self.tmp / "single", self.tmp / "singlehome"
        home.mkdir()
        self.git_init(root)
        (root / CFG).parent.mkdir(parents=True, exist_ok=True)
        wtext(root / CFG, f"[governance]\nengine = \"{__version__}\"\nschema = 1\n")
        agent = root / ".claude" / "agents" / "helper.md"
        agent.parent.mkdir(parents=True)
        wtext(agent, AGENT.format(extra="", body="body"))
        run = lambda: Workspace.run(SimpleNamespace(root=root, home=home), "check")  # noqa: E731
        code, out, _ = run()       # red for its missing log; only this check matters here
        self.assertEqual(out.count(self.NOT_IGNORED), 1, out)
        self.assertIn(f"single: '.' {self.NOT_IGNORED}", out)
        wtext(root / ".gitignore", ".claude/worktrees/\n")
        code, out, _ = run()
        self.assertNotIn(self.NOT_IGNORED, out)
        self.assertNotIn("could not ask git", out)


class DecisionHistory(Base):
    """A rewritten decision entry keeps no history line."""

    def log(self, w):
        return w.root / "projects/alpha/DECISIONS.md"

    def test_bold_history_label_is_flagged(self):
        w = self.ws()
        log = self.log(w)
        wtext(log, log.read_text(encoding="utf-8")
              + entry("A", 101, extra="\n**Earlier:** the old rule required a Y field.\n"))
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertIn('alpha/DECISIONS.md: A-101 keeps history ("Earlier:") — a rewritten '
                      'entry states only today\'s rule; git keeps the old one', out)

    def test_plain_history_label_is_flagged(self):
        w = self.ws()
        log = self.log(w)
        wtext(log, log.read_text(encoding="utf-8")
              + entry("A", 101, extra="\nPreviously: the old rule.\n"))
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertIn('A-101 keeps history ("Previously:")', out)

    def test_history_label_is_case_insensitive(self):
        w = self.ws()
        log = self.log(w)
        wtext(log, log.read_text(encoding="utf-8")
              + entry("A", 101, extra="\nearlier: the old rule.\n"))
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertIn('A-101 keeps history ("Earlier:")', out)   # configured spelling, not the file's

    def test_history_label_inside_a_fence_is_not_flagged(self):
        w = self.ws()
        log = self.log(w)
        wtext(log, log.read_text(encoding="utf-8")
              + entry("A", 101, extra="\n```\nEarlier: example text, not history.\n```\n"))
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertNotIn("keeps history", out)

    def test_pointer_entries_are_unaffected(self):
        w = self.ws()
        log = self.log(w)
        wtext(log, log.read_text(encoding="utf-8") + entry("A", 101)
              + "\n## A-102 — Replaced by A-101\n")
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertNotIn("keeps history", out)

    def test_no_history_label_is_silent(self):
        w = self.ws()
        log = self.log(w)
        wtext(log, log.read_text(encoding="utf-8") + entry("A", 101))
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertNotIn("keeps history", out)

    def test_dropping_a_label_needs_a_reason(self):
        # Fewer labels flagged means more history text passes unnoticed — dropping one is
        # loosening.
        w = self.ws(extra='\n[checks.decision-history]\n'
                          'labels = ["Earlier", "Previously", "Formerly", "Was"]\n')
        code, _, err = w.run("check")
        self.assertEqual(code, 2)
        self.assertIn("project: [checks.decision-history] labels drops 'Superseded' — loosens "
                      "past what the project inherits without a reason: say why in "
                      "[checks.decision-history.reasons] labels", err)
        wtext(w.root / CFG, (w.root / CFG).read_text(encoding="utf-8").replace(
            'labels = ["Earlier", "Previously", "Formerly", "Was"]',
            'labels = ["Earlier", "Previously", "Formerly", "Was"]\n\n'
            '[checks.decision-history.reasons]\n'
            'labels = "\'Superseded\' collides with our status of the same name"'))
        code, out, err = w.run("check")
        self.assertNotEqual(code, 2, err)
        self.assertNotIn("labels drops", out + err)


class ProfileLayer(Base):
    """The shared profile between the standard and each project."""

    RULES = '''
[checks.writing-rules]
rules = [{ text = "colour", use = "color", ignore_case = true, why = "American English" }]
'''

    def make_profile(self, where: Path, settings: str = RULES) -> Path:
        where.mkdir(parents=True, exist_ok=True)
        wtext(where / "principles.toml", settings)
        wtext(where / "PRINCIPLES.md", "# Principles\n\n1. Measure, then claim.\n")
        return where

    def with_profile(self, source: str, extra: str = "") -> Workspace:
        w = self.ws(extra=extra)
        cfg = w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            "schema = 1", f'schema = 1\nprofile = "{source}"', 1))
        return w

    def test_profile_rules_apply_to_the_project_files(self):
        prof = self.make_profile(self.tmp / "prof")
        w = self.with_profile(prof.as_posix(), extra='''
[checks.writing-rules]
level = "error"
files = ["drafts/*.md"]
extend_rules = [{ text = "monster", use = "mob", level = "warn" }]
''')
        w.write("drafts/a.md", "The colour of the monster.\n")
        code, out, _ = w.run("check")
        self.assertIn("drafts/a.md: 1 × 'colour' (use 'color') — American English", out)
        self.assertIn("drafts/a.md: 1 × 'monster' (use 'mob')", out)
        code, out, _ = w.run("explain", "writing-rules")
        self.assertIn("(profile + project", out)

    def test_overriding_a_principle_needs_a_reason(self):
        prof = self.make_profile(self.tmp / "prof", "[checks.agents]\nmax_turns = 100\n")
        w = self.with_profile(prof.as_posix(), extra="\n[checks.agents]\nmax_turns = 50\n")
        code, out, _ = w.run("check")
        self.assertIn("[checks.agents] max_turns overrides the profile with no reason", out)
        cfg = w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            "max_turns = 50", 'max_turns = 50\nreason = "short tasks here"'))
        code, out, _ = w.run("check")
        self.assertNotIn("overrides the profile", out)

    def test_list_widening_is_relative_to_the_profile(self):
        # The fixture's project table always sets statuses = ["locked", "provisional",
        # "superseded"]; a profile narrower than that is what the project widens past.
        prof = self.make_profile(self.tmp / "prof", '[checks.decision-log]\nstatuses = '
                                 '["locked"]\nreason = "profile default"\n')
        w = self.with_profile(prof.as_posix())
        cfg = w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(REASON_LINE, ""))
        code, _, err = w.run("check")
        self.assertEqual(code, 2)
        self.assertIn("project: [checks.decision-log] statuses adds 'provisional', 'superseded'",
                      err)

    def test_list_widening_is_silent_when_the_project_matches_the_profile(self):
        prof = self.make_profile(self.tmp / "prof", '[checks.decision-log]\nstatuses = '
                                 '["locked", "provisional", "superseded"]\n'
                                 'reasons = { statuses = "profile default" }\n')
        w = self.with_profile(prof.as_posix())
        cfg = w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(REASON_LINE, ""))
        code, out, err = w.run("check")
        self.assertNotEqual(code, 2, err)
        self.assertNotIn("statuses adds", out + err)

    def test_list_widening_reports_once_not_also_as_a_profile_override(self):
        # Under require_reasons = false, a project loosening a list its profile set gets the
        # list message naming the values — not also the generic "overrides the profile"
        # warning (one warning per override).
        prof = self.make_profile(self.tmp / "prof", '[checks.decision-log]\nstatuses = '
                                 '["locked"]\nreason = "profile default"\n')
        w = self.with_profile(prof.as_posix())
        cfg = w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(REASON_LINE, "").replace(
            "schema = 1", "schema = 1\nrequire_reasons = false", 1))
        code, out, _ = w.run("check")
        self.assertIn("config: [checks.decision-log] statuses adds", out)
        self.assertNotIn("statuses overrides the profile with no reason", out)

    def test_a_profile_widening_a_list_needs_a_reason_in_either_direction(self):
        # Each is a loosening past the engine standard, refused in the profile as in a project:
        # a value added (looser "more"), a value removed (looser "fewer"), a list emptied where
        # empty means anything goes.
        cases = {
            "adding": ('[checks.decision-log]\nstatuses = ["locked", "provisional", "deferred", '
                       '"superseded"]\n', "[checks.decision-log] statuses adds 'superseded'",
                       '[checks.decision-log.reasons]\nstatuses = "old logs"\n'),
            "removing": ('[checks.decision-history]\nlabels = ["Earlier"]\n',
                         "[checks.decision-history] labels drops 'Previously', 'Formerly', "
                         "'Superseded', 'Was'",
                         '[checks.decision-history.reasons]\nlabels = "one label here"\n'),
            "emptying": ('[checks.doc-frontmatter]\nworking_statuses = []\n',
                         "[checks.doc-frontmatter] working_statuses drops 'active', 'held', "
                         "'planned', 'complete', 'superseded'",
                         '[checks.doc-frontmatter.reasons]\nworking_statuses = "free text"\n'),
        }
        for name, (table, finding, reasons) in cases.items():
            with self.subTest(name):
                prof = self.make_profile(self.tmp / f"prof-{name}", table)
                (self.tmp / name).mkdir()
                w = Workspace(self.tmp / name)
                cfg = w.root / CFG
                wtext(cfg, cfg.read_text(encoding="utf-8").replace(
                    "schema = 1", f'schema = 1\nprofile = "{prof.as_posix()}"', 1))
                code, _, err = w.run("check")
                self.assertEqual(code, 2)
                self.assertIn(f"profile {prof.as_posix()} (principles.toml): {finding} — "
                              f"loosens past the engine standard without a reason: say why "
                              f"under [checks.", err)
                self.assertIn("in the profile's principles.toml (a pinned profile: then tag "
                              "it and move the project's pin)", err)
                wtext(prof / "principles.toml", table + reasons)
                code, _, err = w.run("check")
                self.assertNotEqual(code, 2, err)

    def test_profile_may_not_set_layout(self):
        prof = self.make_profile(self.tmp / "prof", "[projects]\ndocs = []\n")
        code, _, err = self.with_profile(prof.as_posix()).run("check")
        self.assertEqual(code, 2)
        self.assertIn("a profile holds [checks.*] and [dialect] only", err)

    def test_a_profile_dialect_value_is_checked_against_its_choices(self):
        prof = self.make_profile(self.tmp / "prof", '[dialect]\nagent_turns_prose = "never"\n')
        code, _, err = self.with_profile(prof.as_posix()).run("check")
        self.assertEqual(code, 2)
        self.assertIn(f"profile {prof.as_posix()} (principles.toml): [dialect] agent_turns_prose "
                      f"= 'never' is not one of ('forbid', 'must-match')", err)

    def test_an_unknown_profile_dialect_key_names_the_profile(self):
        prof = self.make_profile(self.tmp / "prof", '[dialect]\nheading_style = "x"\n')
        code, _, err = self.with_profile(prof.as_posix()).run("check")
        self.assertEqual(code, 2)
        self.assertIn(f"profile {prof.as_posix()} (principles.toml): unknown [dialect] key "
                      f"'heading_style'", err)

    def test_a_profile_dialect_value_of_the_wrong_type_is_named(self):
        prof = self.make_profile(self.tmp / "prof", "[dialect]\nmarkers = 3\n")
        code, _, err = self.with_profile(prof.as_posix()).run("check")
        self.assertEqual(code, 2)
        self.assertIn(f"profile {prof.as_posix()} (principles.toml): [dialect] markers: "
                      f"expected str, got int", err)

    def test_a_valid_profile_dialect_value_loads(self):
        prof = self.make_profile(self.tmp / "prof", '[dialect]\nagent_turns_prose = "forbid"\n'
                                 '\n[dialect.reasons]\nagent_turns_prose = "principle"\n')
        code, _, err = self.with_profile(prof.as_posix()).run("check")
        self.assertNotEqual(code, 2, err)

    def test_principles_document_and_local_override(self):
        prof = self.make_profile(self.tmp / "prof")
        w = self.with_profile(prof.as_posix())
        code, out, _ = w.run("principles")
        self.assertIn("Measure, then claim.", out)
        w.write(".context-gate/PRINCIPLES.md", "# Ours\n")
        code, out, _ = w.run("principles")
        self.assertEqual(out.strip(), "# Ours")

    def test_git_profile_is_pinned_fetched_once_and_cached(self):
        src = self.make_profile(self.tmp / "profrepo")
        git = lambda *a: subprocess.run(
            ["git", "-C", str(src), "-c", "user.email=t@t", "-c", "user.name=t",
             "-c", "tag.gpgSign=false", "-c", "commit.gpgSign=false", *a],
            check=True, capture_output=True)
        git("init", "-q")
        git("add", "-A")
        git("commit", "-qm", "principles")
        git("tag", "v1")
        url = f"file://{src.as_posix()}"
        code, _, err = self.with_profile(url).run("check")
        self.assertEqual(code, 2)
        self.assertIn("no pinned ref", err)
        shutil.rmtree(self.tmp / "ws")
        shutil.rmtree(self.tmp / "home")
        w = self.with_profile(f"{url}#v1")
        code, out, _ = w.run("principles")
        self.assertEqual(code, 0, out)
        from govern.profile import rmtree
        rmtree(src)                             # the source is gone; the cache is enough
        code, out, _ = w.run("principles")
        self.assertIn("Measure, then claim.", out)

    def test_a_git_profile_starting_with_a_dash_never_reaches_git(self):
        """A profile source git would read as an option is refused before git runs."""
        from unittest import mock
        from govern import profile
        made = self.tmp / "made-by-the-profile"
        url = f"--upload-pack=touch {made.as_posix()} x.git"
        self.assertTrue(profile.is_git(url))
        w = self.with_profile(f"{url}#v1")
        with mock.patch.object(profile.subprocess, "run") as run:
            run.return_value = SimpleNamespace(returncode=1, stderr="no")
            with self.assertRaises(profile.ProfileError):
                profile.resolve(f"{url}#v1", w.root, w.home)
        run.assert_not_called()
        code, _, err = w.run("check")
        self.assertEqual(code, 2, err)
        self.assertIn('starts with "-"', err)
        self.assertNotIn("Traceback", err)
        self.assertFalse(made.exists())
        self.assertEqual(list((w.home / ".cache").rglob("*.tmp")), [])
        # And a source that is one never reads as an option: `--` comes before it.
        with mock.patch.object(profile.subprocess, "run") as run:
            run.return_value = SimpleNamespace(returncode=1, stderr="no")
            with self.assertRaises(profile.ProfileError):
                profile.fetch("https://example.invalid/p.git", "v1", self.tmp / "cache" / "v1")
        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index("https://example.invalid/p.git") - 1], "--")


class PreCommitPath(Base):
    """`check --project X --path DIR --history-from DIR`: a git pre-commit hook's shape (a hook
    that exports the staged tree to a temporary snapshot with no history of its own, then checks
    that snapshot with the checkout named for git history)."""

    def _git(self, root: Path, *args: str, env: dict | None = None) -> None:
        subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t",
                        "-c", "tag.gpgSign=false", "-c", "commit.gpgSign=false", *args],
                       check=True, capture_output=True, env=env)

    def _snapshot(self, w: "Workspace") -> Path:
        """A copy of alpha's governed checkout, the shape `git checkout-index` would leave: no
        `.git` of its own — a staged-tree snapshot carries no history."""
        snap = self.tmp / "snapshot"
        shutil.copytree(w.root / "projects/alpha", snap)
        return snap

    def test_finding_only_in_the_snapshot_is_reported(self):
        w = self.ws()
        snap = self._snapshot(w)
        wtext(snap / "orphan.md", fm(doc_type="manual"))
        code, out, _ = w.run("check", "--project", "alpha", "--path", str(snap))
        self.assertIn("alpha/orphan.md: doc_type 'manual' not in", out)

    def test_finding_only_in_the_working_tree_is_not_reported(self):
        w = self.ws()
        snap = self._snapshot(w)
        w.write("projects/alpha/orphan.md", fm(doc_type="manual"))
        code, out, _ = w.run("check", "--project", "alpha", "--path", str(snap))
        self.assertNotIn("orphan.md", out)

    def test_git_history_answers_come_from_history_from(self):
        w = self.ws()
        w.write("projects/alpha/working-files/plan.md",
               fm("working", status="complete") + "body\n")
        env = {**os.environ, "GIT_AUTHOR_DATE": "2020-01-01T00:00:00",
               "GIT_COMMITTER_DATE": "2020-01-01T00:00:00"}
        self._git(w.root, "init", "-q", env=env)
        self._git(w.root, "add", "-A", env=env)
        self._git(w.root, "commit", "-qm", "x", env=env)
        snap = self._snapshot(w)
        history = w.root / "projects/alpha"
        code, out, _ = w.run("check", "--project", "alpha", "--path", str(snap),
                             "--history-from", str(history))
        self.assertIn("plan.md: status 'complete'", out)   # unchanged: dated from the old commit
        plan = snap / "working-files" / "plan.md"
        wtext(plan, plan.read_text(encoding="utf-8") + "edit\n")
        code, out, _ = w.run("check", "--project", "alpha", "--path", str(snap),
                             "--history-from", str(history))
        self.assertNotIn("plan.md: status 'complete'", out)  # a staged-style edit: touched now

    def test_working_file_words_breach_stays_baselined_under_path(self):
        # A ratchet key for a project's own file must name the real, repo-relative path (never
        # the snapshot's), and `owns` must match it against the scope's real directory — so a
        # breach baselined from an ordinary run still reads as baselined, not new, under
        # `--path`. Built from the snapshot's own absolute path instead, and compared against
        # the snapshot, the key would make every pre-commit run report it as a new breach.
        w = self.ws(extra="\n[checks.doc-frontmatter]\nmax_working_words = 3\n")
        w.write("projects/alpha/working-files/notes.md",
               fm("working", status="active") + "word " * 10)
        code, out, _ = w.run("baseline", "--allow-raise")
        self.assertEqual(code, 0, out)
        baseline = json.loads((w.root / layout.BASELINE).read_text(encoding="utf-8"))
        key = "working_file_words:projects/alpha/working-files/notes.md"
        self.assertIn(key, baseline, baseline)
        snap = self._snapshot(w)
        history = w.root / "projects/alpha"
        code, out, _ = w.run("check", "--project", "alpha", "--path", str(snap),
                             "--history-from", str(history))
        self.assertEqual(code, 0, out)
        self.assertNotIn("new breach", out)

    def test_missing_path_is_exit_2(self):
        w = self.ws()
        missing = self.tmp / "nope"
        code, out, err = w.run("check", "--project", "alpha", "--path", str(missing))
        self.assertEqual(code, 2)
        self.assertIn(f"--path {missing} does not exist", err)

    def test_path_without_project_is_exit_2(self):
        w = self.ws()
        code, out, err = w.run("check", "--path", str(self.tmp / "snapshot"))
        self.assertEqual(code, 2)
        self.assertIn("--path needs --project", err)


class ProjectScopedWorkspaceChecks(Base):
    """`check --project X` also runs the workspace checks that are really about one project's
    files (doc-links, generated-blocks, ratchet), restricted to X — the shape a pre-commit hook
    needs."""

    def _git(self, root: Path, *args: str) -> None:
        subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t",
                        "-c", "tag.gpgSign=false", "-c", "commit.gpgSign=false", *args],
                       check=True, capture_output=True)

    def _two(self, **kw) -> Workspace:
        """A second governed project, `beta`, beside `alpha`."""
        w = self.ws(**kw)
        wtext(w.root / "projects.toml",
             (w.root / "projects.toml").read_text(encoding="utf-8") + BETA_PROJECT)
        w.write("projects/beta/DECISIONS.md",
               fm() + "# Decisions\n\n<!-- t:generated:start id=decision-index -->\n"
               "<!-- t:generated:end id=decision-index -->\n" + entry("B", 700))
        w.index()
        return w

    def _snapshot(self, w: Workspace, name: str) -> Path:
        """A copy of one project's own checkout, the shape `git checkout-index` would leave: no
        `.git` of its own — a staged-tree snapshot carries no history."""
        snap = self.tmp / f"snapshot-{name}"
        shutil.copytree(w.root / "projects" / name, snap)
        return snap

    # -------------------------------------------------------------------- doc-links

    def test_broken_link_in_x_is_reported_y_is_not(self):
        w = self._two()
        w.write("projects/alpha/notes.md", fm() + "See [gone](missing.md).\n")
        w.write("projects/beta/notes.md", fm() + "See [gone](missing.md).\n")
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertEqual(code, 1, out)
        self.assertIn("alpha/notes.md: link target does not exist: missing.md", out)
        self.assertNotIn("beta", out)

    def test_broken_link_under_path_is_reported_y_is_not(self):
        w = self._two()
        snap = self._snapshot(w, "alpha")
        wtext(snap / "notes.md", fm() + "See [gone](missing.md).\n")
        code, out, _ = w.run("check", "--project", "alpha", "--path", str(snap))
        self.assertEqual(code, 1, out)
        self.assertIn("alpha/notes.md: link target does not exist: missing.md", out)
        self.assertNotIn("beta", out)

    def test_link_leaving_the_snapshot_resolves_against_the_real_tree(self):
        # A project doc links up out of its own directory, to the workspace's shared
        # governance docs (`../../governance/...`). A snapshot's own
        # directory carries none of the real tree's layout around it, so the link has to be
        # resolved from where the file really lives, not from the snapshot's (temporary, and
        # differently placed) one.
        w = self._two()
        snap = self._snapshot(w, "alpha")
        wtext(snap / "notes.md", fm() + "See [decisions](../../governance/DECISIONS.md).\n")
        code, out, _ = w.run("check", "--project", "alpha", "--path", str(snap))
        self.assertEqual(code, 0, out)
        self.assertNotIn("link target does not exist", out)

    def test_link_leaving_the_snapshot_to_nothing_real_still_errors(self):
        w = self._two()
        snap = self._snapshot(w, "alpha")
        wtext(snap / "notes.md", fm() + "See [gone](../../governance/NOPE.md).\n")
        code, out, _ = w.run("check", "--project", "alpha", "--path", str(snap))
        self.assertEqual(code, 1, out)
        self.assertIn("alpha/notes.md: link target does not exist: "
                      "../../governance/NOPE.md", out)

    def test_deleted_link_hint_asks_history_from_not_root(self):
        w = self._two()
        alpha = w.root / "projects/alpha"
        self._git(w.root, "init", "-q")
        self._git(w.root, "add", "-A")
        self._git(w.root, "commit", "-qm", "initial")
        gone = alpha / "gone.md"
        wtext(gone, fm())
        self._git(w.root, "add", "-A")
        self._git(w.root, "commit", "-qm", "add gone.md")
        gone.unlink()
        self._git(w.root, "add", "-A")
        self._git(w.root, "commit", "-qm", "delete gone.md")
        sha = subprocess.run(["git", "-C", str(w.root), "log", "-1", "--format=%h", "--",
                              "projects/alpha/gone.md"], check=True, capture_output=True,
                             text=True).stdout.strip()
        snap = self._snapshot(w, "alpha")
        wtext(snap / "notes.md", fm() + "See [gone](gone.md).\n")
        code, out, _ = w.run("check", "--project", "alpha", "--path", str(snap),
                             "--history-from", str(alpha))
        self.assertIn(f"deleted in {sha}; link the last commit that had it ({sha}^:gone.md)", out)

    def test_deleted_link_hint_empty_without_history_from(self):
        w = self._two()
        alpha = w.root / "projects/alpha"
        self._git(w.root, "init", "-q")
        self._git(w.root, "add", "-A")
        self._git(w.root, "commit", "-qm", "initial")
        gone = alpha / "gone.md"
        wtext(gone, fm())
        self._git(w.root, "add", "-A")
        self._git(w.root, "commit", "-qm", "add gone.md")
        gone.unlink()
        self._git(w.root, "add", "-A")
        self._git(w.root, "commit", "-qm", "delete gone.md")
        snap = self._snapshot(w, "alpha")
        wtext(snap / "notes.md", fm() + "See [gone](gone.md).\n")
        code, out, _ = w.run("check", "--project", "alpha", "--path", str(snap))
        self.assertIn("link target does not exist: gone.md", out)
        self.assertNotIn("deleted in", out)

    # -------------------------------------------------------------------- generated-blocks

    def test_stale_block_in_x_is_reported_y_is_not(self):
        w = self._two()
        log = w.root / "projects/alpha/DECISIONS.md"
        wtext(log, log.read_text(encoding="utf-8") + entry("A", 101))
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertEqual(code, 1, out)
        self.assertIn("alpha/DECISIONS.md: generated block 'decision-index' is stale", out)
        self.assertNotIn("beta", out)

    def test_stale_block_under_path_is_reported_y_is_not(self):
        w = self._two()
        snap = self._snapshot(w, "alpha")
        log = snap / "DECISIONS.md"
        wtext(log, log.read_text(encoding="utf-8") + entry("A", 101))
        code, out, _ = w.run("check", "--project", "alpha", "--path", str(snap))
        self.assertEqual(code, 1, out)
        self.assertIn("alpha/DECISIONS.md: generated block 'decision-index' is stale", out)
        self.assertNotIn("beta", out)

    # -------------------------------------------------------------------- ratchet

    def test_ratchet_breach_in_x_reported_y_baseline_kept(self):
        w = self._two(extra="\n[checks.governed-doc-count]\nmax_docs = 0\n")
        code, _, _ = w.run("baseline", "--allow-raise")
        self.assertEqual(code, 0)
        before = json.loads((w.root / layout.BASELINE).read_text(encoding="utf-8"))
        self.assertEqual(before.get("governed_docs:alpha"), 1)
        self.assertEqual(before.get("governed_docs:beta"), 1)
        w.write("projects/alpha/extra.md", fm())
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertEqual(code, 1, out)
        self.assertIn("'governed_docs:alpha' grew to 2 (baseline 1)", out)
        # beta's own recorded breach, unchanged, is neither reported here (it is not this run's
        # business) nor lost (`check` never writes the baseline).
        self.assertNotIn("beta", out)
        after = json.loads((w.root / layout.BASELINE).read_text(encoding="utf-8"))
        self.assertEqual(after, before)

    def test_ratchet_breach_under_path_reported_y_baseline_kept(self):
        w = self._two(extra="\n[checks.governed-doc-count]\nmax_docs = 0\n")
        code, _, _ = w.run("baseline", "--allow-raise")
        self.assertEqual(code, 0)
        snap = self._snapshot(w, "alpha")
        wtext(snap / "extra.md", fm())
        code, out, _ = w.run("check", "--project", "alpha", "--path", str(snap),
                             "--history-from", str(w.root / "projects/alpha"))
        self.assertEqual(code, 1, out)
        self.assertIn("'governed_docs:alpha' grew to 2 (baseline 1)", out)
        self.assertNotIn("beta", out)

    # -------------------------------------------------------------------- unchanged full check

    def test_full_check_output_is_unchanged(self):
        w = self._two()
        code, out, _ = w.run("check")
        self.assertEqual(code, 0, out)
        self.assertIn("[OK] workspace", out)
        self.assertIn("[OK] alpha", out)
        self.assertIn("[OK] beta", out)
        self.assertIn("[OK] total", out)

    def test_full_check_still_fails_on_the_same_findings(self):
        w = self._two()
        w.write("projects/alpha/notes.md", fm() + "See [gone](missing.md).\n")
        log = w.root / "projects/beta/DECISIONS.md"
        wtext(log, log.read_text(encoding="utf-8") + entry("B", 701))
        code, out, _ = w.run("check")
        self.assertEqual(code, 1, out)
        self.assertIn("alpha/notes.md: link target does not exist: missing.md", out)
        self.assertIn("beta/DECISIONS.md: generated block 'decision-index' is stale", out)

    def test_example_workspace_shaped_full_check_is_unchanged(self):
        # A workspace decision log, plus two projects whose own docs link out to it
        # (`../../governance/...`). A full `check` (no `--project`, no `--path`) never touches
        # `ctx.snapshot`/`ctx.real_scope_dir`, so ownership and real-path mapping must be no-ops
        # here.
        w = self._two()
        w.write("projects/alpha/notes.md", fm() + "See [decisions](../../governance/DECISIONS.md).\n")
        w.write("projects/beta/notes.md", fm() + "See [decisions](../../governance/DECISIONS.md).\n")
        code, out, _ = w.run("check")
        self.assertEqual(code, 0, out)
        self.assertIn("[OK] workspace", out)
        self.assertIn("[OK] alpha", out)
        self.assertIn("[OK] beta", out)
        self.assertIn("[OK] total", out)

    def test_default_doc_excludes_do_not_change_a_full_check(self):
        w = self._two()
        w.write("projects/alpha/node_modules/pkg/README.md", "not a governed doc\n")
        code, out, _ = w.run("check")
        self.assertEqual(code, 0, out)
        self.assertNotIn("node_modules", out)


if __name__ == "__main__":
    unittest.main()
