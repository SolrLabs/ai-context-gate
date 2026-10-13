"""Edge cases of `govern migrate`: pointers, conflicts, scopes, newly visible findings."""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from migrate_support import *  # noqa: E402,F401,F403


class PointerInsideSectionKeepsNoBody(Base):
    def test_pointer_nested_in_a_section_gets_no_topic_line(self):
        # D-501 is already a pointer (no body), but is one heading level too deep, inside
        # a section like any other entry there — flattening it must not give it a **Topic:**
        # line, which would leave it carrying a body forever (nothing later strips it, since
        # `_migrate_pointers` skips an entry that already has `replaced_by` set).
        w = Fixture(self.tmp, decisions_text=POINTER_IN_SECTION_LOG)
        code, _ = w.run(apply_=True)
        self.assertEqual(code, 0)
        text = w.log().read_text(encoding="utf-8")
        self.assertIn("## D-501 — Replaced by D-500", text)
        between = text.split("## D-501", 1)[1].split("## D-502", 1)[0]
        self.assertNotIn("**Topic:**", between)
        ctx = w.context()
        entry = next(e for e in ctx.grammar.parse_file(w.log()).entries if e.ident == "D-501")
        self.assertEqual(entry.body.strip(), "")
        self.assertEqual(entry.replaced_by, "D-500")
        # D-500 and D-502, real entries in the same section, still get their Topic.
        self.assertIn("**Topic:** Server", text)

    def test_superseded_entry_whose_successor_is_not_in_the_log_keeps_its_topic(self):
        # D-500 names D-999, which this log does not hold, so no pointer is made — and the
        # entry must not lose its Topic to a pointer that never happens.
        log = FM + "# Decisions\n\n" + DECISIONS_INDEX + (
            "\n## Auth (D-500–D-599)\n\n### D-500 — Old auth\n\n**Status:** superseded\n\n"
            "Superseded by D-999.\n")
        w = Fixture(self.tmp, decisions_text=log)
        code, _ = w.run(apply_=True)
        self.assertEqual(code, 0)
        text = w.log().read_text(encoding="utf-8")
        self.assertIn("## D-500 — Old auth", text)
        self.assertIn("**Topic:** Auth", text.split("## D-500", 1)[1])


class BulletConflictsWithExistingHeading(Base):
    def test_bullet_matching_an_existing_heading_number_is_left_as_a_bullet(self):
        w = Fixture(self.tmp, decisions_text=SECTIONS_LOG, trap_files=BULLET_CONFLICTS_TRAPS)
        code, _ = w.run(apply_=True)
        self.assertEqual(code, 0)
        platform = w.trap("platform-traps.md").read_text(encoding="utf-8")
        self.assertIn("- **3.", platform)
        self.assertNotIn("## T-3", platform)
        traps = w.trap("traps.md").read_text(encoding="utf-8")
        self.assertIn("## T-3 — Already a heading", traps)
        self.assertIn("## T-4 —", traps)   # the non-conflicting bullet still migrates
        report = w.report()
        self.assertIn("T-3 is already a heading", report)
        self.assertIn("needs a person to merge them", report)
        # `trap-ids` must never see a duplicate: only one T-3 exists after migration.
        ctx = w.context()
        findings = cli.collect(ctx, ctx.registry.scopes, workspace=True)
        errors = [m for cid, f in findings for m in f.errors if cid == "trap-ids"]
        self.assertEqual(errors, [])


class GitignoredFileRefusesApply(Base):
    def test_apply_refuses_when_a_touched_file_is_gitignored(self):
        w = Fixture(self.tmp, decisions_text=SECTIONS_LOG,
                   trap_files={"traps.md": FM + "# Traps\n\n- **4. New bullet.** Body four.\n"},
                   git_repo=False)
        wtext(w.root / ".gitignore", "working-files/\n")
        git(w.root, "init", "-q")
        git(w.root, "add", "-A")
        git(w.root, "commit", "-qm", "initial (working-files ignored)")
        before_trap = w.trap("traps.md").read_bytes()
        before_log = w.log().read_bytes()
        code, out = w.run(apply_=True)
        self.assertEqual(code, 2)
        self.assertIn("ignored by git", out)
        # nothing changed: not even the log, which was not itself ignored (all or none).
        self.assertEqual(w.trap("traps.md").read_bytes(), before_trap)
        self.assertEqual(w.log().read_bytes(), before_log)


class TrapIndexFileOrderWithoutSources(Base):
    def test_no_sources_keeps_file_order_even_when_ids_are_out_of_order(self):
        w = base_fixture(self.tmp)
        ctx = w.context()
        path = self.tmp / "order-traps.md"
        wtext(path, FM + "# Traps\n\n## T-10 — Ten\n\n**Bites when:** a.\n\n"
                        "## T-2 — Two\n\n**Bites when:** b.\n")
        text = blocks.trap_index(ctx, path)
        rows = [line for line in text.split("\n") if line.startswith("| T-")]
        self.assertEqual([r.split("|")[1].strip() for r in rows], ["T-10", "T-2"])

    def test_sources_still_sorts_by_id(self):
        w = base_fixture(self.tmp)
        ctx = w.context()
        path = self.tmp / "order-traps.md"
        wtext(path, FM + "# Traps\n\n## T-10 — Ten\n\n**Bites when:** a.\n\n"
                        "## T-2 — Two\n\n**Bites when:** b.\n")
        text = blocks.trap_index(ctx, path, scope=ctx.registry.find("alpha"), sources="*.md")
        rows = [line for line in text.split("\n") if line.startswith("| T-")]
        self.assertEqual([r.split("|")[1].strip() for r in rows], ["T-2", "T-10"])


class TrapBlockSuggestionAcrossScopes(Base):
    def test_two_scopes_one_without_a_trap_index_after_suggested_config_is_clean(self):
        # `beta` has a decision log but no `working-files` directory at all — no trap-shaped
        # content anywhere — so it contributes nothing to the plan (`_plan_traps` returns
        # early), exactly like the member-repo workspace fixture where only one scope has a trap Index.
        w = base_fixture(self.tmp, declare_trap_block=False, registry_text=TWO_SCOPE_REGISTRY)
        wtext(w.root / "projects/beta/DECISIONS.md", FM + "# Decisions\n\n" + DECISIONS_INDEX +
             "\n## D-600 — Beta decision\n\n**Status:** locked\n\n**Rule:** r.\n\n**Why:** w.\n")
        git(w.root, "add", "-A")
        git(w.root, "commit", "-qm", "beta: no trap files at all")

        code, _ = w.run(apply_=True)
        self.assertEqual(code, 0)
        report = w.report()
        # Exactly one suggestion (alpha's), and it is a glob, not a file: a `file` naming this
        # exact path would error in beta, which has no such file at all; a `glob` matching
        # nothing there is simply empty.
        self.assertEqual(report.count("[[blocks.project]]"), 1)
        self.assertIn('glob = "working-files/traps.md"', report)
        self.assertNotIn('file = "working-files/traps.md"', report)

        # Add the suggested block for real, and confirm both scopes are clean.
        cfg_path = w.root / CFG
        wtext(cfg_path, cfg_path.read_text(encoding="utf-8").replace(
            'project = [{ file = "DECISIONS.md", id = "decision-index" }]',
            'project = [{ file = "DECISIONS.md", id = "decision-index" }, '
            '{ glob = "working-files/traps.md", id = "trap-index" }]'))
        ctx = w.context()
        self.assertEqual(cli.cmd_index(ctx), 0)
        findings = cli.collect(ctx, ctx.registry.scopes, workspace=True)
        errors = [m for cid, f in findings for m in f.errors if cid == "generated-blocks"]
        self.assertEqual(errors, [])


    def test_same_index_path_in_two_scopes_gets_one_suggestion_with_sources(self):
        # alpha's Index spans two files (needs `sources`); beta's covers only its own file.
        # Every `[[blocks.project]]` entry applies to every scope, so two entries with the same
        # glob would render alpha's block two ways and leave one of them stale forever.
        w = base_fixture(self.tmp, declare_trap_block=False, registry_text=TWO_SCOPE_REGISTRY)
        wtext(w.root / "projects/beta/DECISIONS.md", FM + "# Decisions\n\n" + DECISIONS_INDEX +
             "\n## D-600 — Beta decision\n\n**Status:** locked\n\n**Rule:** r.\n\n**Why:** w.\n")
        wtext(w.root / "projects/beta/working-files/traps.md",
             FM + "# Traps\n\n## Index\n\n9. Beta trap\n\n---\n\n- **9. Beta trap.** body.\n")
        git(w.root, "add", "-A")
        git(w.root, "commit", "-qm", "beta: a one-file trap index at the same path")

        code, _ = w.run(apply_=False)
        self.assertEqual(code, 0)
        report = w.report()
        self.assertEqual(report.count('glob = "working-files/traps.md"'), 1)
        self.assertIn("sources =", report)


class SuccessorMarkupAndMultiId(Base):
    def test_bold_wrapped_successor_is_recognised(self):
        succ, found = migrate._successor("**Status:** superseded by **D-40**\n\nRule text.")
        self.assertEqual(succ, "D-40")
        self.assertEqual(found, ["D-40"])

    def test_linked_successor_is_recognised(self):
        succ, found = migrate._successor(
            "**Status:** superseded by [D-40](DECISIONS.md#d-40)\n\nRule text.")
        self.assertEqual(succ, "D-40")
        self.assertEqual(found, ["D-40"])

    def test_several_ids_in_one_phrase_with_and_is_ambiguous(self):
        succ, found = migrate._successor("**Status:** superseded\n\nSuperseded by D-40 and D-41.")
        self.assertIsNone(succ)
        self.assertEqual(found, ["D-40", "D-41"])

    def test_several_ids_in_one_phrase_with_comma_is_ambiguous(self):
        succ, found = migrate._successor("**Status:** superseded\n\nSuperseded by D-40, D-41.")
        self.assertIsNone(succ)
        self.assertEqual(found, ["D-40", "D-41"])

    def test_end_to_end_bold_successor_becomes_a_pointer(self):
        text = SECTIONS_LOG.replace("superseded by D-503", "superseded by **D-503**")
        w = Fixture(self.tmp, decisions_text=text)
        code, _ = w.run(apply_=True)
        self.assertEqual(code, 0)
        self.assertIn("## D-501 — Replaced by D-503", w.log().read_text(encoding="utf-8"))


class AbsoluteBlockPatternsRejected(Base):
    def test_helper_detects_posix_and_windows_absolute_patterns(self):
        self.assertTrue(config._is_absolute_pattern("/etc/passwd"))
        self.assertTrue(config._is_absolute_pattern("C:\\traps\\*.md"))
        self.assertTrue(config._is_absolute_pattern("\\\\server\\share\\x.md"))
        self.assertTrue(config._is_absolute_pattern("\\traps\\x.md"))    # rooted, no drive
        self.assertTrue(config._is_absolute_pattern("c:traps*.md"))       # drive-relative
        self.assertFalse(config._is_absolute_pattern("working-files/traps.md"))

    def test_absolute_glob_is_a_config_error_not_a_glob_crash(self):
        root = self.tmp / "root"
        root.mkdir()
        text = CONFIG.format(version=__version__, trap_block="").replace(
            '[{ file = "DECISIONS.md", id = "decision-index" }]',
            '[{ file = "DECISIONS.md", id = "decision-index" }, '
            '{ glob = "/etc/traps*.md", id = "trap-index" }]')
        wtext(root / CFG, text)
        wtext(root / "projects.toml", REGISTRY)
        with self.assertRaises(config.ConfigError) as cm:
            config.load(root, root)
        self.assertIn("absolute", str(cm.exception))

    def test_absolute_sources_is_a_config_error(self):
        root = self.tmp / "root"
        root.mkdir()
        text = CONFIG.format(version=__version__, trap_block="").replace(
            '[{ file = "DECISIONS.md", id = "decision-index" }]',
            '[{ file = "DECISIONS.md", id = "decision-index" }, '
            '{ file = "working-files/traps.md", id = "trap-index", sources = "/traps/*.md" }]')
        wtext(root / CFG, text)
        wtext(root / "projects.toml", REGISTRY)
        with self.assertRaises(config.ConfigError) as cm:
            config.load(root, root)
        self.assertIn("absolute", str(cm.exception))


class MidSentenceRuleRequiresAnUnfinishedSentence(Base):
    def test_title_ending_in_a_period_is_not_treated_as_mid_sentence(self):
        w = Fixture(self.tmp, decisions_text=SECTIONS_LOG,
                   trap_files={"traps.md": PERIOD_TITLE_TRAPS})
        code, _ = w.run(apply_=True)
        self.assertEqual(code, 0)
        text = w.trap("traps.md").read_text(encoding="utf-8")
        self.assertIn("## T-7 — Never force-push\n\ngit rewrites history other clones already "
                     "have.", text)
        report = w.report()
        self.assertNotIn("T-7: the bold text does not end its sentence", report)


class WorkingDirOutsideScopeNeverTracebacks(Base):
    def test_absolute_working_dir_outside_scope_reports_a_problem_not_a_traceback(self):
        root = self.tmp / "root"
        home = self.tmp / "home"
        home.mkdir()
        root.mkdir()
        shared = root / "shared-traps"
        wtext(shared / "traps.md", SHARED_TRAPS_WITH_INDEX)
        cfg_text = CONFIG.format(version=__version__, trap_block="").replace(
            '[projects]\nrequired_docs = []\ntrap_glob = "*traps*.md"',
            '[projects]\nrequired_docs = []\ntrap_glob = "*traps*.md"\n'
            f'working_dir = "{shared.as_posix()}"')
        wtext(root / CFG, cfg_text)
        wtext(root / "projects.toml", REGISTRY)
        wtext(root / "governance/DECISIONS.md", FM + "# Decisions\n\n" + DECISIONS_INDEX +
             "\n## W-1 — Root decision\n\n**Status:** locked\n\n**Rule:** r.\n\n**Why:** w.\n")
        wtext(root / "projects/alpha/DECISIONS.md", SECTIONS_LOG)
        git(root, "init", "-q")
        git(root, "add", "-A")
        git(root, "commit", "-qm", "initial")
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(home)
        try:
            code = migrate.run(root, False)
        finally:
            if old_home is not None:
                os.environ["HOME"] = old_home
        self.assertEqual(code, 0)    # a clean dry run — never a traceback
        report = (root / layout.GOV_DIR / "migration-report.md").read_text(encoding="utf-8")
        self.assertIn("working_dir", report)
        self.assertIn("outside", report)


class NewlyVisibleFindings(Base):
    def test_over_word_limit_entry_is_newly_visible_not_should_not_happen(self):
        # Before migration, `### D-500` is one heading level too deep — malformed, so nothing
        # reads it as an entry, and its word count is never checked. After, it is a real entry
        # at its own level: the word-limit warning that follows was hidden by the old format,
        # not created by the migration.
        w = Fixture(self.tmp, decisions_text=OVER_LIMIT_LOG)
        code, _ = w.run(apply_=True)
        self.assertEqual(code, 0)
        report = w.report()
        self.assertIn("Newly visible: existing content the old format hid from the gate:", report)
        newly = report.split("Newly visible: existing content the old format hid from the gate:",
                             1)[1].split("## Commit", 1)[0]
        self.assertIn("D-500 is", newly)
        self.assertIn("over decision-log max_words", newly)
        should_not = (report.split("New after this migration", 1)[1]
                     if "New after this migration" in report else "")
        self.assertNotIn("decision-log max_words", should_not)
        self.assertIn("## Next steps", report)
        next_steps = report.split("## Next steps", 1)[1]
        self.assertIn("govern index", next_steps)
        self.assertIn("govern baseline --allow-raise", next_steps)
        self.assertLess(next_steps.index("govern index"),
                        next_steps.index("govern baseline --allow-raise"))

    def test_newly_visible_missing_field_is_not_sent_to_the_baseline(self):
        # A missing **Why:** is a decision-log error the baseline never records: the next
        # steps must say to fix the entry, not to run `baseline`.
        log = FM + "# Decisions\n\n" + DECISIONS_INDEX + (
            "\n## Auth (D-500–D-599)\n\n### D-500 — No why\n\n**Status:** locked\n\n"
            "**Rule:** r.\n")
        w = Fixture(self.tmp, decisions_text=log)
        code, _ = w.run(apply_=True)
        self.assertEqual(code, 0)
        next_steps = w.report().split("## Next steps", 1)[1]
        self.assertNotIn("govern baseline", next_steps)
        self.assertIn("fixed in the entries themselves", next_steps)


class MigrateKeepsTheProjectsSeparator(Base):
    """Every heading `migrate` writes (a flattened entry, a bullet trap, a pointer)
    uses the separator the project's own entries use, and a hyphen where there are none."""

    def headings(self, path: Path) -> list[str]:
        return [ln for ln in path.read_text(encoding="utf-8").splitlines()
                if ln.startswith("## ")]

    def test_hyphen_form_entries_get_hyphen_headings(self):
        w = Fixture(self.tmp, decisions_text=hyphens(SECTIONS_LOG),
                    trap_files={"traps.md": hyphens(TRAPS_INDEX),
                                "platform-traps.md": PLATFORM_TRAPS})
        code, _ = w.run(apply_=True)
        self.assertEqual(code, 0)
        log = self.headings(w.log())
        self.assertIn("## D-500 - Use UTC timestamps", log)          # flattened
        self.assertIn("## D-501 - Replaced by D-503", log)           # pointer
        traps = self.headings(w.trap("traps.md"))
        self.assertIn("## T-1 - A committed text fixture is LF, whatever the machine wrote it "
                      "with", traps)                                 # bullet
        self.assertIn("## T-3 - Never cache an error response",
                      self.headings(w.trap("platform-traps.md")))    # no entry of its own
        for line in log + traps + self.headings(w.trap("platform-traps.md")):
            self.assertNotIn("—", line)
        w.commit()
        code, out = w.run(apply_=True)                               # idempotent
        self.assertEqual(code, 0)
        self.assertIn("migrated 0 file(s)", out)

    def test_em_dash_form_entries_get_em_dash_headings(self):
        w = base_fixture(self.tmp)
        code, _ = w.run(apply_=True)
        self.assertEqual(code, 0)
        self.assertIn("## D-501 — Replaced by D-503", self.headings(w.log()))
        self.assertIn("## T-2 — A byte-identical restore is not enough — touch it",
                      self.headings(w.trap("traps.md")))
        self.assertIn("## T-3 — Never cache an error response",
                      self.headings(w.trap("platform-traps.md")))

    def test_a_scope_with_no_entry_anywhere_gets_hyphens(self):
        w = Fixture(self.tmp, decisions_text=EMPTY_LOG, trap_files={"traps.md": TRAPS_INDEX})
        code, _ = w.run(apply_=True)
        self.assertEqual(code, 0)
        self.assertIn("## T-1 - A committed text fixture is LF, whatever the machine wrote it "
                      "with", self.headings(w.trap("traps.md")))

    def test_the_em_dash_grammar_gets_em_dashes_with_no_entry_anywhere(self):
        w = Fixture(self.tmp, decisions_text=EMPTY_LOG, trap_files={"traps.md": TRAPS_INDEX})
        cfg = w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            "[dialect]\n", '[dialect]\ndecision_heading = "em-dash"\n', 1))
        w.commit()
        code, _ = w.run(apply_=True)
        self.assertEqual(code, 0)
        self.assertIn("## T-1 — A committed text fixture is LF, whatever the machine wrote it "
                      "with", self.headings(w.trap("traps.md")))

    def test_a_trap_takes_the_separator_of_a_log_this_run_flattens(self):
        # The log's only entries sit one level too deep until this same run flattens them: a
        # trap heading follows what the log will hold, never the fallback.
        for name, form, sep in (("em", SECTIONS_ONLY_LOG, " — "),
                                ("hyphen", hyphens(SECTIONS_ONLY_LOG), " - ")):
            with self.subTest(sep=sep):
                (self.tmp / name).mkdir()
                w = Fixture(self.tmp / name, decisions_text=form,
                            trap_files={"platform-traps.md": PLATFORM_TRAPS})
                code, _ = w.run(apply_=True)
                self.assertEqual(code, 0)
                self.assertIn(f"## D-500{sep}Use UTC timestamps", self.headings(w.log()))
                self.assertIn(f"## T-3{sep}Never cache an error response",
                              self.headings(w.trap("platform-traps.md")))

    def test_a_trap_file_s_own_entries_decide_ahead_of_the_log(self):
        traps = FM + ("\n# Traps\n\n## T-1 – Already a heading\n\n**Bites when:** x.\n\n"
                      "- **2. Still a bullet.** Body.\n")
        w = Fixture(self.tmp, decisions_text=SECTIONS_LOG, trap_files={"traps.md": traps})
        code, _ = w.run(apply_=True)
        self.assertEqual(code, 0)
        self.assertIn("## T-2 – Still a bullet", self.headings(w.trap("traps.md")))

    def test_a_pointer_keeps_the_dash_of_the_heading_it_replaces(self):
        # The log's last entry is em-dash form; the superseded one before it is a hyphen.
        log = FM + "# Decisions\n\n" + DECISIONS_INDEX + """
## D-500 - Title

**Status:** superseded by D-502

**Rule:** old rule.

**Why:** old reason.

## D-502 — Successor

**Status:** locked

**Rule:** r.

**Why:** w.
"""
        w = Fixture(self.tmp, decisions_text=log)
        code, _ = w.run(apply_=True)
        self.assertEqual(code, 0)
        self.assertIn("## D-500 - Replaced by D-502", self.headings(w.log()))
        self.assertNotIn("## D-500 — Replaced by D-502", self.headings(w.log()))
        w.commit()
        code, out = w.run(apply_=True)                               # idempotent
        self.assertEqual(code, 0)
        self.assertIn("nothing to migrate", out)

    def test_crlf_hyphen_log_keeps_both(self):
        w = Fixture(self.tmp, decisions_text=hyphens(SECTIONS_LOG).replace("\n", "\r\n"))
        code, _ = w.run(apply_=True)
        self.assertEqual(code, 0)
        raw = w.log().read_bytes()
        self.assertIn(b"\r\n## D-501 - Replaced by D-503\r\n", raw)
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))


if __name__ == "__main__":
    unittest.main()
