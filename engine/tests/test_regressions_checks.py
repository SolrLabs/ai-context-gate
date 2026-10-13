"""Behaviour tests on small throwaway workspaces: writing rules, generic checks, renamed names, parsing edge cases and committed files."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import unicodedata
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from regressions_support import *  # noqa: E402,F401,F403

class Baseline(Base):
    def test_clean_workspace_passes(self):
        code, out, _ = self.ws().run("check")
        self.assertEqual(code, 0, out)


class MisplacedRegistryKey(Base):
    def test_handoff_read_from_profile(self):
        w = self.ws()
        w.write("projects/alpha/working-files/HANDOFF.md", fm("working", status="active")
                + "word " * 30)
        w.write(CFG, (w.root / CFG).read_text(encoding="utf-8")
                + "\n[checks.handoff-words]\nmax_words = 10\n")
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertIn("alpha: HANDOFF.md is 30 words, over handoff-words max_words=10", out)

    def test_key_configured_where_the_registry_does_not_put_it(self):
        code, out, _ = self.ws(handoff_key="handoff").run("check")
        self.assertIn("registry: 'alpha' has 'profile.handoff', but the engine reads handoff "
                      "from 'handoff'", out)
        self.assertEqual(code, 1)


class WritingRulesFiles(Base):
    def test_every_configured_file_set_is_checked(self):
        w = self.ws(extra="""
[checks.writing-rules]
level = "error"
files = ["one/pr-*.md", "two/pr-*.md"]
rules = [{ text = "colour", use = "color", ignore_case = true, why = "house style" },
         { text = "monster", use = "mob", level = "warn" }]
""")
        w.write("two/pr-draft.md", "The Colour of a monster.\n")
        code, out, _ = w.run("check")
        self.assertIn("ERROR  two/pr-draft.md: 1 × 'colour' (use 'color') — house style", out)
        self.assertIn("warn   two/pr-draft.md: 1 × 'monster' (use 'mob')", out)


class WritingRulesSkipEngineVocabulary(Base):
    """A project cannot rename the engine's own names (a check id, a setting, a registry fact),
    so writing-rules never flags one where it is used as a name: in the config, profile and
    registry files, and in Markdown code. Prose, and every other file, is checked in full, so a
    spelling rule holds whichever spelling the engine uses."""

    AMERICAN = '{ text = "licence", use = "license", ignore_case = true }'
    BRITISH = '{ text = "license", use = "licence" }'

    def rules_ws(self, rule: str, files: str = '["notes/*", ".context-gate/config.toml", '
                                                '"projects.toml"]', extra: str = "") -> Workspace:
        return self.ws(extra=f"""
[checks.writing-rules]
level = "error"
files = {files}
rules = [{rule}]
{extra}""")

    def findings(self, w: Workspace) -> list[str]:
        _, out, _ = w.run("check")
        return [line.strip() for line in out.splitlines() if " × " in line]

    def test_an_engine_name_in_a_markdown_code_span_is_not_flagged(self):
        w = self.rules_ws(self.AMERICAN)
        w.write("notes/a.md", "Turn on `licences`, set `[checks.licences]` and "
                              "`checks.licences.conflicts`; each entry sets `licence`.\n")
        self.assertEqual(self.findings(w), [])

    def test_an_engine_name_in_a_fenced_block_is_not_flagged(self):
        w = self.rules_ws(self.AMERICAN)
        w.write("notes/a.md", "Example:\r\n\r\n```toml\r\n[checks.licences]\r\n"
                              "require_declared = true\r\n```\r\n")
        self.assertEqual(self.findings(w), [])

    def test_the_old_check_name_in_the_config_is_not_flagged(self):
        # The config is always left out now (it is in the tool's own directory); this guards
        # the engine-owned exemption in case that ever changes.
        w = self.rules_ws(self.AMERICAN, extra='\n[checks.licences]\nrequire_declared = true\n')
        self.assertEqual(self.findings(w), [])

    def test_the_old_check_name_in_the_profile_is_not_flagged(self):
        w = self.rules_ws(self.AMERICAN, files='["prof/*"]')
        w.write("prof/principles.toml", f"[checks.writing-rules]\nrules = [{self.AMERICAN}]\n\n"
                                        "[checks.licences]\nrequire_declared = true\n")
        cfg = w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            "schema = 1", 'schema = 1\nprofile = "prof"', 1))
        self.assertEqual(self.findings(w), [])

    def test_a_registry_fact_in_the_registry_file_is_not_flagged(self):
        w = self.rules_ws(self.AMERICAN)
        reg = w.root / "projects.toml"
        wtext(reg, reg.read_text(encoding="utf-8").replace('license = "MIT"', 'licence = "MIT"'))
        self.assertEqual(self.findings(w), [])

    def test_prose_is_flagged(self):
        w = self.rules_ws(self.AMERICAN)
        w.write("notes/a.md", "## Licence\n\nThe `licences` check reads each licence.\n")
        self.assertEqual(self.findings(w), ["ERROR  notes/a.md: 2 × 'licence' (use 'license')"])

    def test_the_projects_own_code_is_flagged(self):
        w = self.rules_ws(self.AMERICAN)
        w.write("notes/b.py", "LICENCE_TEXT = 1\n")
        self.assertEqual(self.findings(w), ["ERROR  notes/b.py: 1 × 'licence' (use 'license')"])

    def test_a_code_span_that_is_not_an_engine_name_is_flagged(self):
        w = self.rules_ws(self.AMERICAN)
        w.write("notes/a.md", "Call `my_licence` here.\n")
        self.assertEqual(self.findings(w), ["ERROR  notes/a.md: 1 × 'licence' (use 'license')"])

    def test_a_british_rule_skips_the_new_names_and_flags_prose(self):
        w = self.rules_ws(self.BRITISH, extra='\n[checks.licenses]\nrequire_declared = true\n')
        w.write("notes/a.md", "Turn on `licenses` and set `license` per entry.\n")
        self.assertEqual(self.findings(w), [])
        w.write("notes/a.md", "Every repo needs a license.\n")
        self.assertEqual(self.findings(w), ["ERROR  notes/a.md: 1 × 'license' (use 'licence')"])

    def test_an_extension_check_id_is_an_engine_name(self):
        w = self.rules_ws('{ text = "colour", use = "color" }',
                          files='["notes/*"]')
        cfg = w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            "schema = 1", 'schema = 1\nextensions = ["gov-ext"]', 1))
        w.write("gov-ext/colour.py", """
from govern.findings import Findings
from govern.manifest import check

@check("colour-names", scope="workspace", since="0.1.1", origin="test-ext",
       summary="s.", question="q", rationale="r")
def colour_names(ctx, params):
    return Findings()
""")
        self.addCleanup(manifest.forget, "test-ext")
        w.write("notes/a.md", "Turn on `colour-names`.\n")
        self.assertEqual(self.findings(w), [])


class WritingRulesSelectsFilesLikeTheGate(Base):
    """writing-rules leaves out every file the gate never governs as a doc, however a `files`
    glob or path reaches it: this tool's own directory (whose reports quote the configured rules
    back), `.claude/`, the always-excluded directories at any depth and in any case, and files
    git ignores. `exclude` leaves out more, and adding to it is a loosening that needs a reason."""

    RULE = '{ text = "colour", use = "color" }'
    HIT = "ERROR  notes/a.md: 1 × 'colour' (use 'color')"

    def rules_ws(self, files: str = '["**/*.md"]', extra: str = "") -> Workspace:
        w = self.ws(extra=f"""
[checks.writing-rules]
level = "error"
files = {files}
rules = [{self.RULE}]
{extra}""")
        w.write("notes/a.md", "The colour of it.\n")
        return w

    def findings(self, w: Workspace) -> list[str]:
        code, out, err = w.run("check")
        self.assertNotEqual(code, 2, err)
        return [line.strip() for line in out.splitlines() if " × " in line]

    def test_the_tools_own_reports_are_never_checked(self):
        w = self.rules_ws()
        w.write(".context-gate/install-report.md", "Rule: colour → color.\n")
        w.write(".context-gate/upgrade-report.md", "A colour finding was fixed.\n")
        self.assertEqual(self.findings(w), [self.HIT])

    def test_a_listed_path_in_the_tools_own_dir_is_left_out_too(self):
        w = self.rules_ws(files='["notes/*.md", ".context-gate/install-report.md"]')
        w.write(".context-gate/install-report.md", "Rule: colour → color.\n")
        self.assertEqual(self.findings(w), [self.HIT])

    def test_claude_dir_and_excluded_dirs_at_any_depth_are_skipped(self):
        w = self.rules_ws()
        w.write(".claude/agents/helper.md", "colour\n")
        w.write("sub/Build/notes.md", "colour\n")
        w.write("sub/node_modules/pkg/README.md", "colour\n")
        self.assertEqual(self.findings(w), [self.HIT])

    def test_a_gitignored_file_is_skipped(self):
        w = self.rules_ws()
        subprocess.run(["git", "-C", str(w.root), "init", "-q"], check=True)
        w.write(".gitignore", "scratch/\n")
        w.write("scratch/draft.md", "colour\n")
        self.assertEqual(self.findings(w), [self.HIT])

    def test_a_file_a_nested_checkout_tracks_is_asked_of_that_checkout(self):
        # The root ignores the nested checkout wholesale; its own files are still checked.
        w = self.rules_ws()
        subprocess.run(["git", "-C", str(w.root), "init", "-q"], check=True)
        w.write(".gitignore", "alpha/\n")
        w.write("alpha/doc.md", "colour\n")
        subprocess.run(["git", "-C", str(w.root / "alpha"), "init", "-q"], check=True)
        self.assertEqual(self.findings(w), ["ERROR  alpha/doc.md: 1 × 'colour' (use 'color')",
                                            self.HIT])

    def test_exclude_with_a_reason_skips_its_files(self):
        w = self.rules_ws(files='["**/*.md", "**/*.py"]',
                          extra='exclude = ["engine/tests/**"]\n'
                                'reasons = { exclude = "fixtures spell it both ways" }\n')
        w.write("engine/tests/test_x.py", "COLOUR = 'colour'\n")
        w.write("engine/tests/data/sample.md", "colour\n")
        w.write("engine/x.py", "colour = 1\n")
        self.assertEqual(self.findings(w), ["ERROR  engine/x.py: 1 × 'colour' (use 'color')",
                                            self.HIT])

    def git_ignoring(self, w: Workspace, ignore: str) -> None:
        subprocess.run(["git", "-C", str(w.root), "init", "-q"], check=True)
        w.write(".gitignore", ignore)

    INCLUDE = "list it in include_ignored to check it anyway"

    def test_a_nested_checkouts_own_ignores_apply_below_its_root(self):
        # The root ignores the checkout wholesale; each file is asked of the checkout itself.
        w = self.rules_ws(files='["notes/*.md", "alpha/**/*.md"]')
        self.git_ignoring(w, "alpha/\n")
        w.write("alpha/.gitignore", "docs/scratch.md\n")
        w.write("alpha/docs/keep.md", "colour\n")
        w.write("alpha/docs/scratch.md", "colour\n")
        subprocess.run(["git", "-C", str(w.root / "alpha"), "init", "-q"], check=True)
        self.assertEqual(self.findings(w), [
            "ERROR  alpha/docs/keep.md: 1 × 'colour' (use 'color')", self.HIT])

    def drafts_ws(self, extra: str = "") -> Workspace:
        # The usual idiom ignores the files, not the directory, keeping it with a `.gitkeep`.
        w = self.rules_ws(files='["notes/*.md", "outbound/*.md"]', extra=extra)
        self.git_ignoring(w, "outbound/*\n!outbound/.gitkeep\n")
        w.write("outbound/.gitkeep", "")
        w.write("outbound/pr-1.md", "colour\n")
        return w

    def test_ignored_drafts_are_skipped_with_a_warning_naming_include_ignored(self):
        w = self.drafts_ws()
        _, out, _ = w.run("check")
        self.assertIn("writing-rules: 'outbound/*.md' matched 1 file(s), all left out "
                      f"(gitignored) — {self.INCLUDE}", out)
        self.assertEqual([line.strip() for line in out.splitlines() if " × " in line],
                         [self.HIT])

    def test_include_ignored_checks_ignored_drafts(self):
        w = self.drafts_ws(extra='include_ignored = ["outbound/*.md"]\n')
        self.assertEqual(self.findings(w), [
            self.HIT, "ERROR  outbound/pr-1.md: 1 × 'colour' (use 'color')"])

    def test_always_excluded_wins_over_include_ignored(self):
        w = self.rules_ws(extra='include_ignored = ["sub/build/*.md", ".context-gate/*.md"]\n')
        w.write("sub/build/x.md", "colour\n")
        w.write(".context-gate/install-report.md", "colour\n")
        self.assertEqual(self.findings(w), [self.HIT])

    def test_backslashes_and_a_leading_dot_slash_in_every_list(self):
        w = self.rules_ws(
            files="['./notes/*.md', './outbound/*.md', 'tests/**/*.md']",
            extra="exclude = ['tests\\fixtures\\**', './notes/skip.md']\n"
                  "include_ignored = ['.\\outbound\\*.md']\n"
                  'reasons = { exclude = "fixtures spell it both ways" }\n')
        self.git_ignoring(w, "outbound/\n")
        w.write("outbound/pr-1.md", "colour\n")
        w.write("notes/skip.md", "colour\n")
        w.write("tests/fixtures/f.md", "colour\n")
        w.write("tests/t.md", "colour\n")
        self.assertEqual(self.findings(w), [
            self.HIT, "ERROR  outbound/pr-1.md: 1 × 'colour' (use 'color')",
            "ERROR  tests/t.md: 1 × 'colour' (use 'color')"])

    def test_files_is_case_sensitive_on_every_os(self):
        # A case-insensitive file system answers `Notes/*.md` with `notes/a.md`: it is not
        # checked, and the entry says which file it reaches only in another spelling. A
        # case-sensitive one finds nothing, and says nothing.
        w = self.rules_ws(files='["Notes/*.md", "notes/*.MD"]')
        _, out, _ = w.run("check")
        self.assertNotIn(" × ", out)
        self.assertNotIn("matched", out)
        warning = "writing-rules: 'Notes/*.md' matches 'notes/a.md' only in another spelling"
        if (w.root / "NOTES").exists():
            self.assertIn(warning, out)
        else:
            self.assertNotIn("Notes/*.md", out)

    def test_an_entry_that_matches_nothing_says_nothing(self):
        w = self.rules_ws(files='["notes/*.md", "outbound/*.md"]')
        (w.root / "outbound").mkdir()
        _, out, _ = w.run("check")
        self.assertNotIn("outbound", out)

    def test_a_name_stored_in_another_unicode_form_is_on_disk(self):
        # A file system may store a name decomposed (NFD) that a config spells composed (NFC).
        from govern.checks.writing import _on_disk
        nfc, nfd = (unicodedata.normalize(f, "café.md") for f in ("NFC", "NFD"))
        wtext(self.tmp / nfd, "colour\n")
        self.assertTrue(_on_disk(self.tmp, nfc, {}))
        self.assertFalse(_on_disk(self.tmp, "cafe.md", {}))

    def test_a_directory_in_another_unicode_form_is_checked(self):
        nfc, nfd = (unicodedata.normalize(f, "café") for f in ("NFC", "NFD"))
        w = self.rules_ws(files=f'["notes/*.md", "{nfc}/*.md"]')
        (w.root / nfd).mkdir()
        wtext(w.root / nfd / "x.md", "colour\n")
        if not (w.root / nfc).exists():
            self.skipTest("this file system tells the two forms apart")
        self.assertEqual(len(self.findings(w)), 2)

    def test_repeated_slashes_are_one(self):
        w = self.rules_ws(files='["notes//*.md"]')
        self.assertEqual(self.findings(w), [self.HIT])

    def test_a_dot_segment_is_dropped(self):
        # `notes/./*.md` is `notes/*.md`: its file is checked, not reported as another spelling.
        for raw, want in (("notes/./*.md", "notes/*.md"), ("a/.", "a"), ("./a/./b/", "a/b/"),
                          (".\\a\\.\\b", "a/b"), ("a//./b", "a/b"), ("./", ""), (".", "")):
            self.assertEqual(config.normalize_glob(raw), want, raw)
        w = self.rules_ws(files='["notes/./*.md", "notes/."]')
        _, out, _ = w.run("check")
        self.assertNotIn("another spelling", out)
        self.assertEqual([line.strip() for line in out.splitlines() if " × " in line],
                         [self.HIT])

    def test_an_absolute_entry_is_refused(self):
        # `Path.glob` refuses one with a bare NotImplementedError; the load names the entry.
        cases = [(key, value) for key in ("files", "exclude", "include_ignored")
                 for value in ("/notes/*.md", "C:/x/*.md", r"C:\x\*.md", r"\\server\x",
                               "./C:/x")]
        for i, (key, value) in enumerate(cases):
            with self.subTest(key=key, value=value):
                files = f"['notes/*.md', '{value}']" if key == "files" else "['notes/*.md']"
                listed = "" if key == "files" else f"{key} = ['{value}']\n"
                (self.tmp / str(i)).mkdir()
                w = Workspace(self.tmp / str(i), extra=f"""
[checks.writing-rules]
level = "error"
files = {files}
rules = [{self.RULE}]
{listed}reasons = {{ exclude = "r" }}
""")
                code, _, err = w.run("check")
                self.assertEqual(code, 2, err)
                self.assertIn(f"[checks.writing-rules] {key}: '{value}' must be relative to "
                              f"the governance root, not absolute", err)

    def test_an_entry_that_names_no_path_is_refused(self):
        for i, (key, value) in enumerate((("files", "./"), ("files", ""), ("exclude", "."),
                                          ("include_ignored", ".//"))):
            with self.subTest(key=key, value=value):
                files = f'["notes/*.md", "{value}"]' if key == "files" else '["notes/*.md"]'
                listed = "" if key == "files" else f'{key} = ["{value}"]\n'
                (self.tmp / str(i)).mkdir()
                w = Workspace(self.tmp / str(i), extra=f"""
[checks.writing-rules]
level = "error"
files = {files}
rules = [{self.RULE}]
{listed}reasons = {{ exclude = "r" }}
""")
                code, _, err = w.run("check")
                self.assertEqual(code, 2, err)
                self.assertIn(f"[checks.writing-rules] {key}: '{value}' names no path", err)

    def test_a_pattern_whose_matches_are_all_left_out_warns(self):
        w = self.rules_ws(files='["notes/*.md", "*/draft.md", "build/**/*.md", "none/*.md"]')
        self.git_ignoring(w, "scratch/\n")
        w.write("scratch/draft.md", "colour\n")
        w.write("build/a.md", "colour\n")
        w.write("build/out/b.md", "colour\n")
        code, out, _ = w.run("check")
        self.assertIn("writing-rules: '*/draft.md' matched 1 file(s), all left out (gitignored) "
                      f"— {self.INCLUDE}", out)
        self.assertIn("writing-rules: 'build/**/*.md' matched 2 file(s), all left out "
                      "(always excluded)", out)
        self.assertNotIn("(always excluded) —", out)
        self.assertNotIn("notes/*.md' matched", out)
        self.assertNotIn("none/*.md", out)
        self.assertEqual([line.strip() for line in out.splitlines() if " × " in line],
                         [self.HIT])

    def test_exclude_is_matched_as_a_glob_is(self):
        # `**` spans any number of directories, none included; `*` stays inside one.
        w = self.rules_ws(files='["**/*.md", "**/*.py"]',
                          extra='exclude = ["**/fixtures/**", "engine/tests/*"]\n'
                                'reasons = { exclude = "fixtures spell it both ways" }\n')
        w.write("fixtures/a.md", "colour\n")
        w.write("engine/tests/c.py", "colour = 1\n")
        w.write("engine/tests/a/b.py", "colour = 1\n")
        self.assertEqual(self.findings(w), [
            "ERROR  engine/tests/a/b.py: 1 × 'colour' (use 'color')", self.HIT])

    def test_exclude_is_case_sensitive_on_every_os(self):
        w = self.rules_ws(extra='exclude = ["Notes/**"]\n'
                                'reasons = { exclude = "fixtures spell it both ways" }\n')
        self.assertEqual(self.findings(w), [self.HIT])

    def test_exclude_without_a_reason_is_refused(self):
        w = self.rules_ws(extra='exclude = ["engine/tests/**"]\n')
        code, _, err = w.run("check")
        self.assertEqual(code, 2)
        self.assertIn("[checks.writing-rules] exclude adds 'engine/tests/**' — loosens past what "
                      "the project inherits without a reason", err)


class WritingRulesAllowMarker(Base):
    """A line carrying `writing-rules: allow <text>[, <text>...]` keeps that spelling: a rule
    whose `text` it names (in any case) is not counted on that line, the marker's own mention
    included. Every other line, and every other rule on that line, is checked as before."""

    RULES = ('[{ text = "colour", use = "color" }, '
             '{ text = "licence", use = "license", ignore_case = true }]')

    def rules_ws(self) -> Workspace:
        return self.ws(extra=f"""
[checks.writing-rules]
level = "error"
files = ["notes/*"]
rules = {self.RULES}
""")

    def findings(self, w: Workspace) -> list[str]:
        code, out, err = w.run("check")
        self.assertNotEqual(code, 2, err)
        return [line.strip() for line in out.splitlines() if " × " in line]

    def test_the_marker_skips_the_named_text_on_its_line(self):
        w = self.rules_ws()
        w.write("notes/a.py", 'OLD = {"licences": "licenses"}  # writing-rules: allow Licence\n')
        self.assertEqual(self.findings(w), [])

    def test_the_same_text_on_the_next_line_still_flags(self):
        w = self.rules_ws()
        w.write("notes/a.py", 'OLD = "licence"  # writing-rules: allow licence\n'
                              'NEW = "licence"\n')
        self.assertEqual(self.findings(w), ["ERROR  notes/a.py: 1 × 'licence' (use 'license')"])

    def test_another_rules_text_on_the_marked_line_still_flags(self):
        w = self.rules_ws()
        w.write("notes/a.py", 'OLD = "licence colour"  # writing-rules: allow licence\n')
        self.assertEqual(self.findings(w), ["ERROR  notes/a.py: 1 × 'colour' (use 'color')"])

    def test_several_texts_in_one_marker(self):
        w = self.rules_ws()
        w.write("notes/a.py", 'OLD = "licence colour"  # writing-rules: allow licence, colour\n')
        self.assertEqual(self.findings(w), [])

    def test_a_marker_in_an_html_comment_in_markdown(self):
        w = self.rules_ws()
        w.write("notes/a.md", "The old colour stays. <!-- writing-rules: allow colour -->\r\n"
                              "A new colour does not.\r\n")
        self.assertEqual(self.findings(w), ["ERROR  notes/a.md: 1 × 'colour' (use 'color')"])

    def test_a_quoted_or_punctuated_text_and_any_case_keywords(self):
        w = self.rules_ws()
        w.write("notes/a.md", "A licence. <!-- writing-rules: allow licence. -->\n"
                              "A licence. <!-- writing-rules: allow \"licence\" -->\n"
                              "A licence. <!-- Writing-Rules: ALLOW `licence`; -->\n")
        self.assertEqual(self.findings(w), [])

    def test_allowing_a_text_no_rule_has_is_harmless(self):
        w = self.rules_ws()
        w.write("notes/a.md", "The colour. <!-- writing-rules: allow flavour -->\n")
        self.assertEqual(self.findings(w), ["ERROR  notes/a.md: 1 × 'colour' (use 'color')"])


class GenericChecks(Base):
    """Generic engine checks, each configured from config.toml rather than hard-coded."""

    def test_hooks_wired(self):
        w = self.ws(extra="""
[checks.hooks-wired]
level = "error"
[[checks.hooks-wired.hooks]]
script = "hooks/guard.py"
event = "PreToolUse"
matcher = "Bash"
rule = "R-1"
unwired = "the rule is unenforced"
""")
        w.write("hooks/guard.py", "")
        w.write(".claude/settings.json", '{"hooks": {}}')
        code, out, _ = w.run("check")
        self.assertIn("R-1: no PreToolUse hook on Bash runs guard.py — the rule is unenforced", out)
        w.write(".claude/settings.json", json.dumps({"hooks": {"PreToolUse": [
            {"matcher": "Bash",
             "hooks": [{"type": "command", "command": "python3 hooks/guard.py"}]}]}}))
        code, out, _ = w.run("check")
        self.assertEqual(code, 0, out)

    def test_licenses(self):
        w = self.ws(alpha_extra='role = "own"', extra="""
[checks.licenses]
level = "error"
conflicts = [{ a = { licenses = ["MIT"] }, b = { licenses = ["MIT"], roles = ["own"] }, decision = "W-9" }]
""")
        code, out, _ = w.run("check")
        self.assertIn("W-9 is not a recorded decision", out)

    def test_checkout_hygiene(self):
        w = self.ws(alpha_extra='role = "contribute"', extra="""
[checks.checkout-hygiene]
level = "error"
roles = ["contribute"]
forbidden = ["AGENTS.md"]
rules = { contribute = "W-1" }
setup_hint = "run ./bootstrap.sh"
""")
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertIn("alpha: checkout 'alpha' not present — run ./bootstrap.sh", out)
        w.write("alpha/AGENTS.md", "x")
        subprocess.run(["git", "-C", str(w.root / "alpha"), "init", "-q"], check=True)
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertIn("alpha: 'AGENTS.md' is present but untracked inside the checkout (W-1)", out)

    def test_generic_checks_are_off_until_configured(self):
        code, out, _ = self.ws().run("check")
        self.assertEqual(code, 0, out)


class RenamedNamesStillLoad(Base):
    """A name the engine has since respelled (the check `licences` is now `licenses`, the
    registry fact `licence` is now `license`) still loads, read as the new name, with one
    warning per place saying what to rename: an upgrade never turns a project red. Both
    spellings in one place is an error, since neither can be read over the other."""

    RENAMED = "(the old name still works; rename it)"

    def load(self, w: Workspace) -> config.Config:
        return config.load(w.root, w.home)

    def test_an_old_check_id_runs_as_the_new_check(self):
        w = self.ws(alpha_extra='role = "own"', extra="""
[checks.licences]
level = "error"
conflicts = [{ a = { licences = ["MIT"] }, b = { licences = ["MIT"], roles = ["own"] }, decision = "W-9" }]
""")
        code, out, err = w.run("check")
        self.assertIn("W-9 is not a recorded decision", out)
        self.assertIn(f"warning: {CFG} [checks]: 'licences' is now 'licenses' {self.RENAMED}",
                      err)
        self.assertIn(f"warning: {CFG} [checks.licenses] conflicts: 'licences' is now "
                      f"'licenses' {self.RENAMED}", err)
        self.assertEqual(err.count("warning:"), 2, err)
        s = self.load(w).checks["licenses"]
        self.assertEqual((s.level, s.source["level"]), ("error", "project"))

    def test_an_old_check_id_in_a_profile_loads_as_the_new_check(self):
        prof = self.tmp / "prof"
        prof.mkdir()
        wtext(prof / "principles.toml", '[checks.licences]\nlevel = "warn"\n')
        w = self.ws()
        cfg = w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            "schema = 1", f'schema = 1\nprofile = "{prof.as_posix()}"', 1))
        loaded = self.load(w)
        s = loaded.checks["licenses"]
        self.assertEqual((s.level, s.source["level"]), ("warn", "profile"))
        self.assertEqual(loaded.warnings, [
            f"profile {prof.as_posix()} (principles.toml) [checks]: 'licences' is now "
            f"'licenses' {self.RENAMED}"])

    def test_old_and_new_check_ids_together_are_an_error(self):
        w = self.ws(extra='\n[checks.licences]\nlevel = "error"\n'
                          '\n[checks.licenses]\nlevel = "warn"\n')
        code, out, err = w.run("check")
        self.assertEqual(code, 2, out + err)
        self.assertIn(f"{CFG} [checks]: 'licences' and 'licenses' are both set", err)

    def test_an_old_check_id_in_the_run_order_reads_as_the_new_one(self):
        w = self.ws(extra='\n[checks]\nworkspace_order = ["licences"]\n')
        loaded = self.load(w)
        self.assertEqual(loaded.raw["checks"]["workspace_order"], ["licenses"])
        self.assertEqual(loaded.warnings, [f"{CFG} [checks] workspace_order: 'licences' is now "
                                           f"'licenses' {self.RENAMED}"])

    def test_old_and_new_check_ids_in_one_run_order_are_an_error(self):
        w = self.ws(extra='\n[checks]\nworkspace_order = ["licences", "licenses"]\n')
        with self.assertRaisesRegex(config.ConfigError, r"\[checks\] workspace_order: "
                                    r"'licences' and 'licenses' are both set"):
            self.load(w)

    def test_an_old_registry_fact_reads_as_the_new_one(self):
        w = self.ws(extra='\n[checks.licenses]\nlevel = "error"\n')
        reg = w.root / "projects.toml"
        wtext(reg, reg.read_text(encoding="utf-8").replace('license = "MIT"', 'licence = "MIT"'))
        beta = ('\n[[project]]\nname = "beta"\ndir = "beta"\ntier = "registered"\n'
                'licence = "MIT"\n')
        wtext(reg, reg.read_text(encoding="utf-8") + beta)
        code, out, err = w.run("check")
        self.assertEqual(code, 0, out + err)
        self.assertIn(f"warning: projects.toml (alpha, beta): 'licence' is now 'license' "
                      f"{self.RENAMED}", err)
        self.assertEqual(err.count("warning:"), 1, err)
        wtext(reg, reg.read_text(encoding="utf-8").replace(beta, ""))
        wtext(reg, reg.read_text(encoding="utf-8").replace('licence = "MIT"', ""))
        code, out, _ = w.run("check")
        self.assertIn("alpha: no license declared", out)

    def test_an_entry_with_both_registry_facts_is_an_error(self):
        w = self.ws()
        reg = w.root / "projects.toml"
        wtext(reg, reg.read_text(encoding="utf-8").replace(
            'license = "MIT"', 'license = "MIT"\nlicence = "MIT"'))
        code, out, err = w.run("check")
        self.assertEqual(code, 2, out + err)
        self.assertIn("projects.toml (alpha): 'licence' and 'license' are both set", err)

    def test_an_old_registry_keys_mapping_reads_as_the_new_one(self):
        w = self.ws(extra='\n[checks.licenses]\nlevel = "error"\n')
        cfg, reg = w.root / CFG, w.root / "projects.toml"
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            'handoff = "profile.handoff"', 'handoff = "profile.handoff"\nlicence = "profile.lic"'))
        wtext(reg, reg.read_text(encoding="utf-8").replace('license = "MIT"\n', "").replace(
            "[project.profile]\n", '[project.profile]\nlic = "MIT"\n'))
        code, out, err = w.run("check")
        self.assertEqual(code, 0, out + err)
        self.assertIn(f"warning: {CFG} [registry.keys]: 'licence' is now 'license' "
                      f"{self.RENAMED}", err)

    def test_a_registry_that_keeps_the_old_name_maps_it_without_a_warning(self):
        # A registry file other tools read too keeps `licence` by saying where the fact lives.
        w = self.ws(extra='\n[checks.licenses]\nlevel = "error"\n')
        cfg, reg = w.root / CFG, w.root / "projects.toml"
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            'handoff = "profile.handoff"', 'handoff = "profile.handoff"\nlicense = "licence"'))
        wtext(reg, reg.read_text(encoding="utf-8").replace('license = "MIT"', 'licence = "MIT"'))
        code, out, err = w.run("check")
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("warning:", err)

    def test_an_old_registry_keys_mapping_still_serves_the_old_name(self):
        # Required under the old name, mapped under the old name: both read the one fact.
        w = self.ws(extra='\n[checks.registry]\nrequired_keys = ["name", "dir", "tier", '
                          '"licence"]\n')
        cfg, reg = w.root / CFG, w.root / "projects.toml"
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            'handoff = "profile.handoff"', 'handoff = "profile.handoff"\nlicence = "profile.lic"'))
        wtext(reg, reg.read_text(encoding="utf-8").replace('license = "MIT"\n', "").replace(
            "[project.profile]\n", '[project.profile]\nlic = "MIT"\n'))
        code, out, err = w.run("check")
        self.assertEqual(code, 0, out + err)
        ctx = cli.load_context(w.root, "govern")
        self.assertEqual(ctx.registry.scopes[0].get("licence"), "MIT")

    def test_a_word_that_is_only_an_old_fact_name_is_not_a_check_id(self):
        # `licence` was a registry fact, never a check: as a check id it is an unknown name.
        w = self.ws(extra='\n[checks]\nworkspace_order = ["licence"]\n')
        with self.assertRaisesRegex(config.ConfigError, "names 'licence', which is not a "
                                                        "registered check"):
            self.load(w)

    def test_an_old_fact_name_in_a_conflict_side_is_left_as_written(self):
        w = self.ws(extra='\n[checks.licenses]\nconflicts = [{ a = { licence = ["MIT"] }, '
                          'b = { licenses = ["MIT"] }, decision = "W-9" }]\n')
        loaded = self.load(w)
        self.assertEqual(loaded.warnings, [])
        self.assertEqual(loaded.checks["licenses"].params["conflicts"][0]["a"],
                         {"licence": ["MIT"]})

    def test_explain_and_record_accept_the_old_check_id(self):
        w = self.ws()
        code, out, err = w.run("explain", "licences")
        self.assertEqual(code, 0, err)
        self.assertEqual(out, w.run("explain", "licenses")[1])
        self.assertIn("warning: explain: 'licences' is now 'licenses'", err)
        wtext(w.root / layout.MANIFEST, 'engine = "0.5.0"\ninstalled = "2026-09-23"\n')
        code, out, err = w.run("options", "--record", "licences")
        self.assertEqual(code, 0, err)
        self.assertIn("recorded as answered: licenses", out)
        self.assertIn("warning: options --record: 'licences' is now 'licenses'", err)

    def test_an_old_registry_fact_in_required_when_reads_as_the_new_one(self):
        w = self.ws()
        cfg = w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            "[projects]\n", '[projects]\nrequired_when = [{ key = "licence", docs = ["NOTICE"] }]\n', 1))
        loaded = self.load(w)
        self.assertEqual(loaded.raw["projects"]["required_when"][0]["key"], "license")
        self.assertEqual(loaded.warnings, [f"{CFG} [projects] required_when[0] key: 'licence' is "
                                           f"now 'license' {self.RENAMED}"])


class Extensions(Base):
    def test_project_extension_registers_and_runs(self):
        w = self.ws(extensions='extensions = ["gov-ext"]',
                    extra='\n[checks.no-todo]\nlevel = "error"\n')
        w.write("gov-ext/todo.py", """
from govern.findings import Findings
from govern.manifest import check

@check("no-todo", scope="workspace", since="0.1.1", origin="test-ext",
       summary="No TODO in AGENTS.md.", question="q", rationale="r")
def no_todo(ctx, params):
    f = Findings()
    if "TODO" in (ctx.root / "AGENTS.md").read_text(encoding="utf-8"):
        f.error("AGENTS.md: TODO")
    return f
""")
        self.addCleanup(manifest.forget, "test-ext")
        w.write("AGENTS.md", fm() + "TODO\n")
        code, out, _ = w.run("check")
        self.assertIn("AGENTS.md: TODO", out)


class MalformedHeading(Base):
    def test_hyphen_and_colon_headings_are_errors(self):
        w = self.ws()
        log = w.root / "projects/alpha/DECISIONS.md"
        wtext(log, log.read_text(encoding="utf-8") + entry("A", 101, sep=" - ") + entry("A", 102, sep=": "))
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertIn("'## A-101 - Title 101' is not a decision entry heading", out)
        self.assertIn("'## A-102: Title 102' is not a decision entry heading", out)


class MissingMarkers(Base):
    def test_deleted_markers_are_an_error(self):
        w = self.ws()
        w.write("projects/alpha/DECISIONS.md", fm() + entry("A", 100))
        code, out, _ = w.run("check")
        self.assertIn("generated block 'decision-index' has no markers", out)
        code, out, _ = w.run("index")
        self.assertEqual(code, 1)


class CorruptBaseline(Base):
    def test_never_overwritten(self):
        w = self.ws()
        path = w.write(layout.BASELINE, '{"x": 1,')
        code, out, _ = w.run("baseline")
        self.assertEqual(code, 1)
        self.assertEqual(path.read_text(encoding="utf-8"), '{"x": 1,')
        code, out, _ = w.run("check")
        self.assertIn("is not a valid baseline", out)


class NonUtf8File(Base):
    def test_non_utf8_reported_as_encoding(self):
        w = self.ws()
        w.write("projects/alpha/latin.md", "caf\xe9".encode("latin-1"))
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertIn("alpha/latin.md: not valid UTF-8", out)
        self.assertNotIn("latin.md: no frontmatter", out)


class GitDecoding(Base):
    """`context.git` decodes as UTF-8 with `errors="replace"` (never the platform locale, which
    is not UTF-8 by default on Windows) — a byte git wrote that the console's locale disagrees
    on never raises."""

    def test_non_utf8_output_is_replaced_not_raised(self):
        from unittest import mock
        from govern.context import git
        completed = subprocess.CompletedProcess(args=["git"], returncode=0,
                                                 stdout=b"caf\xe9\n", stderr=b"")
        with mock.patch("subprocess.run", return_value=completed) as spy:
            out = git(Path("."), "log", "-1")
        self.assertEqual(out, "caf�")
        self.assertNotIn("text", spy.call_args.kwargs)
        self.assertNotIn("encoding", spy.call_args.kwargs)


class FrontmatterParsing(Base):
    def test_misreadings(self):
        f, _ = text.parse_frontmatter(textwrap.dedent("""\
            ---
            doc_type: control
            nested:
              doc_type: working
            related:
              - a.md
              - b.md
            load_when: anything that "should work"
            quoted: "whole"
            one: x.md
            ---
            body"""))
        self.assertEqual(f.get("doc_type"), "control")
        self.assertEqual(f.get_list("related"), ["a.md", "b.md"])
        self.assertEqual(f.get("load_when"), 'anything that "should work"')
        self.assertEqual(f.get("quoted"), "whole")
        self.assertEqual(f.get_list("one"), ["x.md"])


class StatusCase(Base):
    def test_status_case_insensitive_and_missing_named(self):
        w = self.ws()
        log = w.root / "projects/alpha/DECISIONS.md"
        wtext(log, log.read_text(encoding="utf-8") + entry("A", 101, status="Locked")
                       + "\n## A-102 — No status\n\n**Rule:** r.\n\n**Why:** w.\n")
        w.index()
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertNotIn("A-101", out)
        self.assertIn("projects/alpha/DECISIONS.md: A-102 has no **Status:**", out)


class AgentEffort(Base):
    def test_effort_required(self):
        w = self.ws()
        w.write(".claude/agents/helper.md",
                "---\nname: helper\ndescription: d\nmodel: opus\nmaxTurns: 10\n---\nbody\n")
        code, out, _ = w.run("check")
        self.assertIn("agents/helper.md: frontmatter missing 'effort'", out)


class TrailingSection(Base):
    def test_last_entry_ends_at_next_section(self):
        w = self.ws()
        log = w.root / "projects/alpha/DECISIONS.md"
        wtext(log, log.read_text(encoding="utf-8") + "\n## Appendix\n\n" + "word " * 500 + "\n")
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertNotIn("decision-log max_words", out)


class FencesAndComments(Base):
    def test_examples_are_not_entries(self):
        w = self.ws()
        log = w.root / "projects/alpha/DECISIONS.md"
        wtext(log, log.read_text(encoding="utf-8") + "\n```\n## A-150 — Example in a fence\n```\n"
                       "\n<!--\n## A-151 — Example in a comment\n-->\n")
        code, out, _ = w.run("show", "--project", "alpha", "--list")
        self.assertNotIn("A-150", out)
        self.assertNotIn("A-151", out)


class DirtyFinishedFile(Base):
    def test_uncommitted_edit_is_touched_now(self):
        w = self.ws()
        path = w.write("projects/alpha/working-files/plan.md",
                       fm("working", status="complete") + "body\n")
        env = {**os.environ, "GIT_AUTHOR_DATE": "2020-01-01T00:00:00",
               "GIT_COMMITTER_DATE": "2020-01-01T00:00:00"}
        for cmd in (["init", "-q"], ["add", "-A"],
                    ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"]):
            subprocess.run(["git", "-C", str(w.root), *cmd], check=True, env=env)
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertIn("plan.md: status 'complete'", out)
        wtext(path, path.read_text(encoding="utf-8") + "edit\n")
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertNotIn("plan.md: status 'complete'", out)


class TickedPlans(Base):
    """`finished-files` `ticked`: a working file whose every box is ticked is finished."""

    WARN = "plan.md: every box is ticked: delete it (git keeps it), or set its status to complete"
    PLAN = "projects/alpha/working-files/plan.md"

    def run_plan(self, body, status="active", name="plan.md", ticked=True):
        extra = '\n[checks.finished-files]\nlevel = "error"\n'
        if not ticked:
            extra += ('ticked = false\n[checks.finished-files.reasons]\n'
                      'ticked = "plans here are never deleted"\n')
        w = self.ws(extra=extra)
        w.write(f"projects/alpha/working-files/{name}",
                fm("working", status=status) + body)
        return w.run("check", "--project", "alpha")

    def assertWarned(self, out):
        self.assertIn(f"  warn   alpha/projects/alpha/working-files/{self.WARN}\n", out)

    def assertQuiet(self, out):
        self.assertNotIn("every box is ticked", out)

    def test_every_box_ticked_warns_never_errors(self):
        code, out, _ = self.run_plan("- [x] a\n- [x] b\n")
        self.assertWarned(out)
        self.assertEqual(out.count("every box is ticked"), 1)
        self.assertNotIn("ERROR", out)
        self.assertEqual(code, 0, out)

    def test_a_star_item_is_an_unticked_box(self):
        self.assertQuiet(self.run_plan("- [x] a\n* [ ] b\n")[1])

    def test_a_plus_item_is_an_unticked_box(self):
        self.assertQuiet(self.run_plan("- [x] a\n+ [ ] b\n")[1])

    def test_a_numbered_item_is_an_unticked_box(self):
        self.assertQuiet(self.run_plan("- [x] a\n1. [ ] b\n")[1])

    def test_a_paren_numbered_item_is_an_unticked_box(self):
        self.assertQuiet(self.run_plan("- [x] a\n2) [ ] b\n")[1])

    def test_a_box_with_no_text_is_an_unticked_box(self):
        self.assertQuiet(self.run_plan("- [x] a\n- [ ]\n")[1])

    def test_one_unticked_box(self):
        self.assertQuiet(self.run_plan("- [x] a\n- [ ] b\n")[1])

    def test_no_boxes(self):
        self.assertQuiet(self.run_plan("Nothing to tick.\n")[1])

    def test_only_ticked_box_in_a_code_fence(self):
        self.assertQuiet(self.run_plan("```\n- [x] a\n```\n")[1])

    def test_unticked_box_in_a_comment_does_not_count(self):
        self.assertWarned(self.run_plan("- [x] a\n<!--\n- [ ] b\n-->\n")[1])

    def test_exempt_name(self):
        self.assertQuiet(self.run_plan("- [x] a\n", name="HANDOFF.md")[1])

    def test_ticked_false(self):
        self.assertQuiet(self.run_plan("- [x] a\n", ticked=False)[1])

    def test_finished_status_gets_todays_finding_only(self):
        code, out, _ = self.run_plan("- [x] a\n", status="complete")
        self.assertQuiet(out)
        self.assertIn("plan.md is finished but was never committed", out)

    def test_capital_x_counts(self):
        self.assertWarned(self.run_plan("- [X] a\n")[1])

    def test_indented_box_counts(self):
        self.assertWarned(self.run_plan("  - [x] a\n")[1])


class Committed(Base):
    """`Context.committed`: committed means "in git history", checked
    with `git log`, never `git status` — which misses a file staged but not yet committed, one
    hidden by `.gitignore` or `status.showUntrackedFiles=no`, and fails closed rather than
    reading any of those, or a failing git call, as clean."""

    def _git(self, root: Path, *args: str) -> None:
        subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t",
                        "-c", "tag.gpgSign=false", "-c", "commit.gpgSign=false", *args],
                       check=True, capture_output=True)

    def _plan(self, w: "Workspace") -> Path:
        return w.write("projects/alpha/working-files/plan.md",
                       fm("working", status="complete") + "body\n")

    def _assert_never_committed(self, out: str) -> None:
        self.assertIn("plan.md is finished but was never committed — commit it once so git "
                      "keeps it, then delete it", out)
        self.assertNotIn("last touched", out)

    def test_not_a_repo_is_flagged_instead_of_aged(self):
        w = self.ws()
        self._plan(w)
        code, out, _ = w.run("check", "--project", "alpha")
        self._assert_never_committed(out)

    def test_staged_not_committed_is_flagged_instead_of_aged(self):
        w = self.ws()
        self._git(w.root, "init", "-q")
        self._plan(w)
        self._git(w.root, "add", "projects/alpha/working-files/plan.md")
        code, out, _ = w.run("check", "--project", "alpha")
        self._assert_never_committed(out)

    def test_gitignored_file_is_flagged_instead_of_aged(self):
        w = self.ws()
        self._git(w.root, "init", "-q")
        w.write(".gitignore", "working-files/\n")
        self._git(w.root, "add", "-A")
        self._git(w.root, "commit", "-qm", "initial")
        self._plan(w)
        code, out, _ = w.run("check", "--project", "alpha")
        self._assert_never_committed(out)

    def test_status_hidden_untracked_file_is_flagged_instead_of_aged(self):
        w = self.ws()
        self._git(w.root, "init", "-q")
        self._git(w.root, "config", "status.showUntrackedFiles", "no")
        self._plan(w)
        code, out, _ = w.run("check", "--project", "alpha")
        self._assert_never_committed(out)

    def test_failing_git_is_flagged_instead_of_aged_even_when_committed(self):
        from unittest import mock
        from govern import context as context_mod
        w = self.ws()
        self._git(w.root, "init", "-q")
        self._plan(w)
        self._git(w.root, "add", "-A")
        self._git(w.root, "commit", "-qm", "x")
        with mock.patch.object(context_mod, "git", return_value=None):
            code, out, _ = w.run("check", "--project", "alpha")
        self._assert_never_committed(out)

    def test_committed_file_is_aged_not_flagged(self):
        w = self.ws()
        self._git(w.root, "init", "-q")
        self._plan(w)
        self._git(w.root, "add", "-A")
        env = {**os.environ, "GIT_AUTHOR_DATE": "2020-01-01T00:00:00",
               "GIT_COMMITTER_DATE": "2020-01-01T00:00:00"}
        subprocess.run(["git", "-C", str(w.root), "-c", "user.email=t@t", "-c", "user.name=t",
                       "-c", "tag.gpgSign=false", "-c", "commit.gpgSign=false",
                       "commit", "-qm", "x"], check=True, env=env, capture_output=True)
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertIn("plan.md: status 'complete', last touched", out)
        self.assertNotIn("never committed", out)


if __name__ == "__main__":
    unittest.main()
