"""Behaviour tests on small throwaway workspaces: topic words, excluded docs, generated blocks and one finding per breach."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from regressions_support import *  # noqa: E402,F401,F403

class TopicWords(Base):
    """A decision entry's word count excludes its own **Topic:** line, the same treatment
    word_count gives frontmatter and generated blocks: adopting Topic lines
    across a decision log must not read as every entry growing, since the gate would report
    the migration's own edit rather than anything an author wrote."""

    def test_a_topic_line_does_not_grow_a_baselined_entry(self):
        w = self.ws(extra="\nmax_words = 1\n")
        code, _, _ = w.run("baseline", "--allow-raise")
        self.assertEqual(code, 0)
        before = json.loads((w.root / layout.BASELINE).read_text(encoding="utf-8"))
        log = w.root / "projects/alpha/DECISIONS.md"
        wtext(log, log.read_text(encoding="utf-8").replace(
            "**Status:** locked", "**Status:** locked\n\n**Topic:** Sync"))
        w.run("index")
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertEqual(code, 0, out)
        after = json.loads((w.root / layout.BASELINE).read_text(encoding="utf-8"))
        self.assertEqual(after, before)

    def test_a_topic_line_inside_a_fence_still_counts(self):
        w = self.ws(extra="\nmax_words = 1\n")
        code, _, _ = w.run("baseline", "--allow-raise")
        self.assertEqual(code, 0)
        before = json.loads((w.root / layout.BASELINE).read_text(encoding="utf-8"))
        log = w.root / "projects/alpha/DECISIONS.md"
        wtext(log, log.read_text(encoding="utf-8").replace(
            "**Status:** locked",
            "**Status:** locked\n\n```\n**Topic:** Example\n```"))
        w.run("index")
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertEqual(code, 1, out)
        # 4 words added: two fence-marker lines and the masked Topic line's own two words —
        # inside a fence it is an example, not metadata, so it counts as body text.
        self.assertIn(f"'decision_words:alpha:A-100' grew to {before['decision_words:alpha:A-100'] + 4}", out)


class DocsEntryUnderAnAlwaysExcludedDir(Base):
    """An explicit `docs` entry under a directory the docs scan always leaves out matches
    nothing the gate governs. Loading the config says so once, on every command, naming the
    entry and the exclude that wins — a warning, never a new error."""

    def _ws(self, projects_docs: str, workspace_docs: str | None = None) -> Workspace:
        w = self.ws()
        cfg = w.root / CFG
        body = cfg.read_text(encoding="utf-8").replace(
            'required_docs = ["DECISIONS.md"]',
            f'required_docs = ["DECISIONS.md"]\ndocs = {projects_docs}')
        if workspace_docs is not None:
            body = body.replace('docs = ["AGENTS.md", "governance/*.md"]',
                                f"docs = {workspace_docs}")
        wtext(cfg, body)
        w.write("projects/alpha/.claude/x.md", fm() + "# X\n")
        return w

    WARNING = ("warning: [projects] docs entry '.claude/x.md' is never governed: the "
               "always-applied exclude '**/.claude/**' wins — move the doc, or drop the entry")

    def test_an_explicit_entry_under_claude_warns_once(self):
        w = self._ws('["**/*.md", ".claude/x.md"]')
        code, out, err = w.run("check")
        self.assertEqual(code, 0, out)
        self.assertEqual(err.count(self.WARNING), 1, err)
        self.assertEqual(err.count("warning:"), 1, err)
        self.assertNotIn(".claude", out)

    def test_every_command_says_it_and_parsed_output_is_unchanged(self):
        w = self._ws('["**/*.md", ".claude/x.md"]')
        code, out, err = w.run("next-id", "--project", "alpha")
        self.assertEqual((code, out), (0, "A-101\n"))
        self.assertIn(self.WARNING, err)

    def test_a_workspace_entry_names_its_own_section(self):
        w = self._ws('["**/*.md"]', '["AGENTS.md", "governance/*.md", ".context-gate/*.md"]')
        _, _, err = w.run("check")
        self.assertIn("warning: [workspace] docs entry '.context-gate/*.md' is never governed: "
                      "the always-applied exclude '**/.context-gate/**' wins", err)

    def test_a_non_string_projects_entry_is_a_config_error_naming_it(self):
        code, out, err = self._ws('["**/*.md", 1]').run("next-id", "--project", "alpha")
        self.assertEqual(code, 2, out + err)
        self.assertIn("[projects] docs[1]: expected str, got int", err)

    def test_a_non_string_workspace_entry_is_a_config_error_naming_it(self):
        code, out, err = self._ws('["**/*.md"]', '[1]').run("check")
        self.assertEqual(code, 2, out + err)
        self.assertIn("[workspace] docs[0]: expected str, got int", err)

    def test_a_glob_that_reaches_excluded_dirs_is_not_an_entry_under_one(self):
        _, _, err = self._ws('["**/*.md"]').run("check")
        self.assertNotIn("warning:", err)


class DefaultDocExcludesAtAnyDepth(Base):
    """The always-excluded directories are left out at any depth, by the docs scan and by
    `measure` alike — one rule, so what adopt measured is what the gate governs."""

    NESTED = ("sub/node_modules/x.md", "sub/.claude/x.md", "sub/deep/build/x.md",
              "sub/Build/x.md", "sub/Dist/x.md", "sub/deep/Vendor/x.md", "sub/Target/x.md")

    def _ws(self) -> Workspace:
        w = self.ws()
        for rel in self.NESTED:                     # a doc_type the gate would reject
            w.write(f"projects/alpha/{rel}", fm(doc_type="manual") + "# Not governed\n")
        w.write("projects/alpha/sub/real.md", fm("reference") + "# Governed\n")
        return w

    def test_nested_excluded_dirs_are_not_governed(self):
        w = self._ws()
        code, out, _ = w.run("check")
        self.assertEqual(code, 0, out)
        self.assertNotIn("x.md", out)
        from govern.context import Context
        cfg = config.load(w.root, w.home)
        ctx = Context(root=w.root, home=w.home, cfg=cfg, registry=registry.load(cfg),
                      prog="govern")
        alpha = ctx.registry.find("alpha")
        self.assertIn("sub/real.md", [rel for rel, _ in ctx.governed_docs(alpha.gov)])
        self.assertEqual([rel for rel, _ in ctx.governed_docs(alpha.gov) if "x.md" in rel], [])

    def test_nested_excluded_dirs_are_not_measured(self):
        from govern import measure
        m = measure.measure(self._ws().root)
        alpha = next(s for s in m.scopes if s.dir == "projects/alpha")
        self.assertEqual(alpha.doc_dirs["sub"], (1, 1))

    def test_a_nested_docs_entry_names_the_exclude_that_wins(self):
        from govern.context import default_exclude
        self.assertEqual(default_exclude("sub/.claude/x.md"), "**/.claude/**")
        self.assertEqual(default_exclude("sub/deep/build/x.md"), "**/build/**")
        self.assertIsNone(default_exclude("sub/building/x.md"))
        self.assertIsNone(default_exclude("build.md"))
        # In any case, on every OS: the engine before these rules matched `Build/` on Windows.
        self.assertEqual(default_exclude("sub/Build/x.md"), "**/build/**")
        self.assertEqual(default_exclude("Vendor/x.md"), "**/vendor/**")
        self.assertIsNone(default_exclude("sub/Building/x.md"))


class GeneratedBlocksWords(Base):
    """A generated block's markers are HTML comments, so stripping comments first left
    no block to strip and every generated row counted toward the word ratchets. Only the prose a
    reader writes counts; a comment and a generated index add nothing."""

    PROSE = "Current state: shipping the parser."          # 5 words

    def _handoff(self) -> Workspace:
        w = self.ws(extra="""
[checks.handoff-words]
max_words = 5

[checks.doc-frontmatter]
max_working_words = 5
""")
        cfg = w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            'project = [{ file = "DECISIONS.md", id = "decision-index" }]',
            'project = [{ file = "DECISIONS.md", id = "decision-index" },\n'
            '           { file = "working-files/HANDOFF.md", id = "doc-registry" }]'))
        for n in range(4):
            w.write(f"projects/alpha/reference-{n}.md",
                    fm("reference") + f"# Reference {n}\n")
        w.write("projects/alpha/working-files/HANDOFF.md",
                fm("working", status="active") + self.PROSE + "\n\n"
                "<!-- a note for the next session, never read as prose -->\n\n"
                "<!-- t:generated:start id=doc-registry -->\n"
                "<!-- t:generated:end id=doc-registry -->\n")
        w.index()
        return w

    def test_word_count_counts_only_prose(self):
        w = self._handoff()
        body = text.parse_frontmatter(text.read(
            w.root / "projects/alpha/working-files/HANDOFF.md"))[1]
        self.assertIn("reference-3.md", body)              # the index really has rows
        self.assertEqual(text.word_count(body, text.Markers("t")), 5)

    def test_generated_index_adds_nothing_to_the_ratchets(self):
        w = self._handoff()
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertEqual(code, 0, out)
        self.assertNotIn("words", out)
        w.run("baseline", "--allow-raise")
        baseline = json.loads((w.root / layout.BASELINE).read_text(encoding="utf-8"))
        self.assertEqual([k for k in baseline if "words" in k], [], baseline)


class LocalLayerTracked(Base):
    """`local-layer`: a `local.toml` git tracks is an error, one it does not is silent."""

    def setUp(self) -> None:
        super().setUp()
        self.w = self.ws()
        self.env = {**os.environ, "HOME": str(self.w.home), "XDG_CONFIG_HOME": str(self.w.home)}
        self.git("init", "-q")

    def git(self, *args: str) -> None:
        """git under the temp HOME: a global ignore never makes `add -f` vacuous."""
        subprocess.run(["git", "-C", str(self.w.root), *args], env=self.env, check=True,
                       capture_output=True)

    def check(self) -> tuple[int, str, str]:
        return self.w.run("check")

    def test_an_untracked_local_toml_is_silent(self):
        self.w.write(layout.LOCAL, '[governance]\nengine = "0.5.2-beta.1"\n')
        code, out, err = self.check()
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("local.toml", out + err)

    def test_a_tracked_local_toml_fails_the_gate(self):
        self.w.write(layout.LOCAL, '[governance]\nengine = "0.5.2-beta.1"\n')
        self.git("add", "-f", layout.LOCAL)
        code, out, err = self.check()
        self.assertNotEqual(code, 0, out + err)
        self.assertIn("local.toml is tracked by git", out + err)

    def test_no_local_toml_is_silent(self):
        from unittest import mock
        from govern.checks import repo
        with mock.patch.object(repo, "git", side_effect=AssertionError("git was called")):
            code, out, err = self.check()
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("local-layer", out + err)

    def test_a_governance_root_in_a_subdirectory_of_the_repo(self):
        """The repo is the temp dir, the governance root its `ws/` subdirectory."""
        shutil.rmtree(self.w.root / ".git")
        self.git_at(self.tmp, "init", "-q")
        self.w.write(layout.LOCAL, '[governance]\nengine = "0.5.2-beta.1"\n')
        code, out, err = self.check()
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("local.toml", out + err)
        self.git_at(self.tmp, "add", "-f", f"ws/{layout.LOCAL}")
        code, out, err = self.check()
        self.assertNotEqual(code, 0, out + err)
        self.assertIn("local.toml is tracked by git", out + err)

    def test_no_git_repo_at_all_is_silent(self):
        shutil.rmtree(self.w.root / ".git")
        self.w.write(layout.LOCAL, '[governance]\nengine = "0.5.2-beta.1"\n')
        code, out, err = self.check()
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("local.toml", out + err)
        self.assertNotIn("Traceback", out + err)

    def git_at(self, where: Path, *args: str) -> None:
        subprocess.run(["git", "-C", str(where), *args], env=self.env, check=True,
                       capture_output=True)


class RatchetOwnsScopeNameWithColon(unittest.TestCase):
    """`owns` splits a `decision_words`/`governed_docs`/`handoff_words` key from the right: an
    id never contains ':', but nothing stops a scope name from containing one, and splitting
    from the left would cut such a name apart instead of matching it."""

    def _scope(self, name: str) -> registry.Scope:
        return registry.Scope(name=name, entry={}, keys={}, gov=None, id_prefix=None,
                              id_range=None, doc_set=False, governed=True)

    def test_decision_words_key_matches_a_scope_name_containing_colon(self):
        scope = self._scope("alpha:west")
        self.assertTrue(ratchet.owns(None, scope, "decision_words:alpha:west:D-1"))
        self.assertFalse(ratchet.owns(None, scope, "decision_words:alpha:east:D-1"))

    def test_governed_docs_and_handoff_words_keys_match_a_scope_name_containing_colon(self):
        scope = self._scope("alpha:west")
        self.assertTrue(ratchet.owns(None, scope, "governed_docs:alpha:west"))
        self.assertTrue(ratchet.owns(None, scope, "handoff_words:alpha:west"))


class MissingStatusListsAllowedValues(Base):
    """A working file with no `status` is told which values the project allows, in the
    first error, not only after a wrong guess."""

    def test_missing_status_names_the_allowed_values(self):
        w = self.ws()
        w.write("projects/alpha/working-files/plan.md", fm("working") + "Plan.\n")
        code, out, _ = w.run("check")
        self.assertEqual(code, 1, out)
        self.assertIn("alpha/working-files/plan.md: working file needs 'status', starting with "
                      "one of ('active', 'held', 'planned', 'complete', 'superseded') — it is "
                      "what says which plan is active", out)

    def test_a_project_s_own_list_is_the_one_named(self):
        w = self.ws(extra='\n[checks.doc-frontmatter]\nworking_statuses = ["open", "shut"]\n'
                          'reasons = { working_statuses = "this project has its own words" }\n')
        w.write("projects/alpha/working-files/plan.md", fm("working") + "Plan.\n")
        _, out, _ = w.run("check")
        self.assertIn("working file needs 'status', starting with one of ('open', 'shut') — ",
                      out)

    def test_free_text_statuses_keep_the_message_without_a_list(self):
        w = self.ws(extra='\n[checks.doc-frontmatter]\nworking_statuses = []\n'
                          'reasons = { working_statuses = "status is free text here" }\n')
        w.write("projects/alpha/working-files/plan.md", fm("working") + "Plan.\n")
        code, out, _ = w.run("check")
        self.assertEqual(code, 1, out)
        self.assertIn("alpha/working-files/plan.md: working file needs 'status' — it is what "
                      "says which plan is active", out)
        self.assertNotIn("starting with one of", out)


class OneFindingPerBreach(Base):
    """One over-long working file is one finding. New or grown, it is the ratchet's
    error (a new one leading with the fix, `baseline --allow-raise` last); recorded and
    unchanged, it is doc-frontmatter's warning. Wherever the ratchet does not report the file,
    the warning stays: a breach never ends with no finding at all."""

    NOTES = "projects/alpha/working-files/notes.md"
    KEY = "working_file_words:" + NOTES
    LIMIT = "\n[checks.doc-frontmatter]\nmax_working_words = 3\n"
    REMEDY = ("split it into smaller files, put permanent content behind an index, or delete "
              "what is finished")

    def over(self, w: Workspace, words: int = 10, rel: str | None = None) -> None:
        w.write(rel or self.NOTES, fm("working", status="active") + "word " * words)

    def about(self, out: str, name: str = "notes.md") -> list[str]:
        """Every finding line that names the file."""
        return [line for line in out.splitlines()
                if line.startswith(("  ERROR", "  warn")) and name in line]

    def assert_ratchet_new(self, out: str) -> None:
        found = self.about(out)
        self.assertEqual(len(found), 1, out)
        self.assertTrue(found[0].startswith(
            f"  ERROR  ratchet: '{self.KEY}' is a new breach at 10 words "
            f"(doc-frontmatter max_working_words=3), not in "
            f"{Path(layout.BASELINE).name} — {self.REMEDY}; to accept a file that predates the "
            f"gate, run `"), found[0])
        self.assertTrue(found[0].endswith(" baseline --allow-raise`"), found[0])

    def assert_warning_only(self, out: str, label: str = "alpha/working-files/notes.md") -> None:
        found = self.about(out)
        self.assertEqual(found, [f"  warn   {label}: 10 words exceeds doc-frontmatter "
                                 f"max_working_words=3 — {self.REMEDY}"], out)

    def test_a_new_breach_is_one_finding_the_ratchet_error_leading_with_splitting(self):
        w = self.ws(extra=self.LIMIT)
        self.over(w)
        code, out, _ = w.run("check")
        self.assertEqual(code, 1, out)
        self.assert_ratchet_new(out)

    def test_a_recorded_unchanged_breach_is_one_finding_the_warning(self):
        w = self.ws(extra=self.LIMIT)
        self.over(w)
        code, out, _ = w.run("baseline", "--allow-raise")
        self.assertEqual(code, 0, out)
        code, out, _ = w.run("check")
        self.assertEqual(code, 0, out)
        self.assert_warning_only(out)

    def test_a_grown_breach_is_one_finding_the_ratchet_error_as_before(self):
        w = self.ws(extra=self.LIMIT)
        self.over(w)
        w.run("baseline", "--allow-raise")
        self.over(w, words=12)
        code, out, _ = w.run("check")
        self.assertEqual(code, 1, out)
        found = self.about(out)
        self.assertEqual(len(found), 1, out)
        self.assertTrue(found[0].startswith(
            f"  ERROR  ratchet: '{self.KEY}' grew to 12 words (doc-frontmatter "
            f"max_working_words=3, baseline 10) — fix it, or `"), found[0])
        self.assertTrue(found[0].endswith(" baseline --allow-raise` to accept the new size"),
                        found[0])

    def test_a_kind_with_no_remedy_keeps_the_generic_new_breach_text(self):
        w = self.ws(extra="\n[checks.governed-doc-count]\nmax_docs = 0\n")
        code, out, _ = w.run("check")
        self.assertEqual(code, 1, out)
        line = next(ln for ln in out.splitlines() if "governed_docs:alpha" in ln)
        self.assertTrue(line.startswith(
            f"  ERROR  ratchet: 'governed_docs:alpha' is a new breach at 1, not in "
            f"{Path(layout.BASELINE).name} — fix it, or if it is accepted for now run `"), line)
        self.assertTrue(line.endswith(" baseline --allow-raise`"), line)
        self.assertIn("governed docs exceeds governed-doc-count max_docs=0", out)

    def test_a_governed_docs_breach_names_the_finished_working_files_that_could_go(self):
        w = self.ws(extra="\n[checks.governed-doc-count]\nmax_docs = 0\n")
        w.write("projects/alpha/working-files/old.md", fm("working", status="complete") + "Done.\n")
        # a finished file git does not hold is never named: deleting it loses it
        for args in (("init", "-q"), ("add", "-A"),
                     ("-c", "commit.gpgSign=false", "commit", "-qm", "work")):
            subprocess.run(["git", "-C", str(w.root), "-c", "user.email=t@t",
                            "-c", "user.name=t", *args], check=True, capture_output=True)
        code, out, _ = w.run("check")
        self.assertEqual(code, 1, out)
        line = next(ln for ln in out.splitlines()
                    if ln.startswith("  ERROR") and "governed_docs:alpha" in ln)
        self.assertTrue(line.endswith(
            " baseline --allow-raise` Finished working files that could go: "
            "alpha/working-files/old.md"), line)

    def test_a_governed_docs_breach_with_nothing_finished_keeps_its_message(self):
        w = self.ws(extra="\n[checks.governed-doc-count]\nmax_docs = 0\n")
        w.write("projects/alpha/working-files/live.md", fm("working", status="active") + "On.\n")
        code, out, _ = w.run("check")
        self.assertEqual(code, 1, out)
        line = next(ln for ln in out.splitlines()
                    if ln.startswith("  ERROR") and "governed_docs:alpha" in ln)
        self.assertTrue(line.endswith(" baseline --allow-raise`"), line)

    # ---- every way `check` runs: one finding where the ratchet reports, never none

    def test_project_run_new_breach_is_one_finding(self):
        w = self.ws(extra=self.LIMIT)
        self.over(w)
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertEqual(code, 1, out)
        self.assert_ratchet_new(out)

    def test_project_run_recorded_breach_keeps_the_warning(self):
        w = self.ws(extra=self.LIMIT)
        self.over(w)
        w.run("baseline", "--allow-raise")
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertEqual(code, 0, out)
        self.assert_warning_only(out)

    def test_path_snapshot_new_breach_is_one_finding(self):
        w = self.ws(extra=self.LIMIT)
        snap = self.tmp / "snapshot"
        shutil.copytree(w.root / "projects/alpha", snap)
        (snap / "working-files").mkdir(exist_ok=True)
        wtext(snap / "working-files/notes.md", fm("working", status="active") + "word " * 10)
        code, out, _ = w.run("check", "--project", "alpha", "--path", str(snap))
        self.assertEqual(code, 1, out)
        self.assert_ratchet_new(out)

    def test_path_snapshot_recorded_breach_keeps_the_warning(self):
        w = self.ws(extra=self.LIMIT)
        self.over(w)
        w.run("baseline", "--allow-raise")
        snap = self.tmp / "snapshot"
        shutil.copytree(w.root / "projects/alpha", snap)
        code, out, _ = w.run("check", "--project", "alpha", "--path", str(snap))
        self.assertEqual(code, 0, out)
        self.assert_warning_only(out)

    def test_a_workspace_doc_the_ratchet_never_measures_keeps_its_warning(self):
        # The ratchet measures project scopes' docs only: a workspace doc's breach has the
        # warning as its one finding, on a full run and under --workspace-only alike.
        w = self.ws(extra=self.LIMIT)
        self.over(w, rel="governance/notes.md")
        for argv in (["check"], ["check", "--workspace-only"]):
            code, out, _ = w.run(*argv)
            self.assertEqual(code, 0, out)
            self.assert_warning_only(out, label="governance/notes.md")

    def test_ratchet_off_for_the_rule_keeps_the_warning(self):
        w = self.ws(extra=self.LIMIT + 'ratchet = false\nreason = "warn-only by policy"\n')
        self.over(w)
        code, out, _ = w.run("check")
        self.assertEqual(code, 0, out)
        self.assert_warning_only(out)

    def test_an_unreadable_baseline_keeps_the_warning(self):
        w = self.ws(extra=self.LIMIT)
        self.over(w)
        w.write(layout.BASELINE, "{ not json")
        code, out, _ = w.run("check")
        self.assertEqual(code, 1, out)
        self.assertIn("is not a valid baseline", out)
        self.assert_warning_only(out)

    def test_a_ratchet_that_could_not_measure_keeps_the_warning(self):
        # One unreadable doc stops the ratchet measuring the scope at all: it reports that, and
        # nothing about notes.md, so the warning is still the breach's finding.
        w = self.ws(extra=self.LIMIT)
        self.over(w)
        w.write("projects/alpha/broken.md", b"---\ndoc_type: reference\n---\n\xff\xfe\n")
        code, out, _ = w.run("check")
        self.assertEqual(code, 1, out)
        self.assertIn("ratchet could not check it", out)
        self.assert_warning_only(out)

    def test_ratchet_at_warn_is_still_one_finding(self):
        w = self.ws(extra=self.LIMIT + '\n[checks.ratchet]\nlevel = "warn"\n'
                                       'reason = "advisory while adopting"\n')
        self.over(w)
        code, out, _ = w.run("check")
        self.assertEqual(code, 0, out)
        found = self.about(out)
        self.assertEqual(len(found), 1, out)
        self.assertTrue(found[0].startswith(f"  warn   ratchet: '{self.KEY}' is a new breach"),
                        found[0])

    def test_one_file_s_ratchet_error_does_not_hide_another_file_s_warning(self):
        w = self.ws(extra=self.LIMIT)
        self.over(w, rel="projects/alpha/working-files/older.md")
        w.run("baseline", "--allow-raise")
        self.over(w)
        code, out, _ = w.run("check")
        self.assertEqual(code, 1, out)
        self.assert_ratchet_new(out)
        self.assertEqual(self.about(out, "older.md"),
                         ["  warn   alpha/working-files/older.md: 10 words exceeds "
                          f"doc-frontmatter max_working_words=3 — {self.REMEDY}"], out)

    def test_every_other_finding_on_the_file_is_kept(self):
        # Only the size warning steps aside: an error on the same file is still reported.
        w = self.ws(extra=self.LIMIT)
        w.write(self.NOTES, fm("working", status="nonsense") + "word " * 10)
        code, out, _ = w.run("check")
        self.assertEqual(code, 1, out)
        self.assertIn("alpha/working-files/notes.md: status 'nonsense' does not start with", out)
        self.assertNotIn("words exceeds doc-frontmatter", out)
        self.assertIn(f"'{self.KEY}' is a new breach", out)


class HeadingSeparator(Base):
    """Under the `em-dash` grammar (this fixture's), the engine writes ` — `, the one
    separator that grammar reads, whatever a file holds."""

    TRAPS = "projects/alpha/working-files/traps.md"

    def test_trap_add_under_the_em_dash_grammar_writes_an_em_dash(self):
        w = self.ws()
        w.write(self.TRAPS, fm("working", status="active") + "# Traps\n")
        code, out, _ = w.run("trap-add", "--project", "alpha", "--title", "First",
                             "--bites", "always")
        self.assertEqual(code, 0, out)
        self.assertIn("\n## T-1 — First\n\n**Bites when:** always\n",
                      (w.root / self.TRAPS).read_text(encoding="utf-8"))
        code, out, _ = w.run("check")
        self.assertEqual(code, 0, out)

    def test_show_prints_the_heading_as_written(self):
        w = self.ws()
        code, out, _ = w.run("show", "--project", "alpha", "A-100")
        self.assertEqual(code, 0, out)
        self.assertEqual(out.splitlines()[0], "## A-100 — Title 100")


class LocalLayerFollowsTheRelease(unittest.TestCase):
    """local.toml is read when it names a beta of this engine's release, whichever beta."""

    def named(self, engine):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / layout.GOV_DIR).mkdir()
            body = "" if engine is None else f"engine = {json.dumps(engine)}\n"
            (root / layout.LOCAL).write_text(f"[governance]\n{body}", encoding="utf-8")
            with mock.patch.object(config, "__version__", "0.7.0-beta.9"):
                return config._local_layer(root)

    def test_an_earlier_beta_of_the_release_is_read(self):
        self.assertIsNotNone(self.named("0.7.0-beta.8"))

    def test_the_same_beta_is_read(self):
        self.assertIsNotNone(self.named("0.7.0-beta.9"))

    def test_a_later_beta_of_the_release_is_read(self):
        self.assertIsNotNone(self.named("0.7.0-beta.12"))

    def test_a_beta_of_another_minor_is_not_read(self):
        self.assertIsNone(self.named("0.8.0-beta.1"))

    def test_the_stable_release_is_not_read(self):
        self.assertIsNone(self.named("0.7.0"))

    def test_a_beta_of_another_patch_is_not_read(self):
        self.assertIsNone(self.named("0.7.1-beta.1"))

    def test_a_missing_or_non_string_engine_is_not_read(self):
        self.assertIsNone(self.named(None))
        self.assertIsNone(self.named(7))

    def test_a_stable_engine_never_reads_local_toml(self):
        """The guard at the top of `_local_layer`: a stable engine, whatever the file names."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / layout.GOV_DIR).mkdir()
            (root / layout.CONFIG).write_text("[governance]\nschema = 1\n", encoding="utf-8")
            (root / layout.LOCAL).write_text(
                '[governance]\nengine = "0.7.0-beta.9"\n[checks.agent-measure]\nlevel = "warn"\n',
                encoding="utf-8")
            with mock.patch.object(config, "__version__", "0.7.0"):
                self.assertIsNone(config._local_layer(root))
                cfg = config.load(root, root / "home", pin_check=False)
            self.assertEqual(cfg.checks["agent-measure"].level, "off")

    def test_session_start_says_nothing_for_a_beta_of_another_release(self):
        import importlib.util
        import govern
        spec = importlib.util.spec_from_file_location(
            "session_start_under_test", ENGINE.parent / "plugin" / "hooks" / "session_start.py")
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / layout.GOV_DIR).mkdir()
            (root / layout.LOCAL).write_text('[governance]\nengine = "0.8.0-beta.1"\n',
                                             encoding="utf-8")
            with mock.patch.object(govern, "__version__", "0.7.0-beta.9"):
                self.assertIsNone(hook._beta_differs(root))
                (root / layout.LOCAL).write_text('[governance]\nengine = "0.7.0-beta.8"\n',
                                                 encoding="utf-8")
                self.assertIsNotNone(hook._beta_differs(root))


if __name__ == "__main__":
    unittest.main()
