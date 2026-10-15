"""Sections, bullet traps, successors, indexes, atomic apply and git blockers for `govern migrate`."""
from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from migrate_support import *  # noqa: E402,F401,F403


class SectionsToTopics(Base):
    def test_sections_flatten_with_topic_and_reparse_correctly(self):
        w = base_fixture(self.tmp)
        code, _ = w.run(apply_=True)
        self.assertEqual(code, 0)
        text = w.log().read_text(encoding="utf-8")
        self.assertNotIn("## Server-side platform", text)
        self.assertIn("## D-500 — Use UTC timestamps", text)
        self.assertIn("**Topic:** Server-side platform", text)

        ctx = w.context()
        parsed = ctx.grammar.parse_file(w.log())
        by_id = {e.ident: e for e in parsed.entries}
        self.assertEqual(by_id["D-500"].topic, "Server-side platform")
        self.assertEqual(by_id["D-500"].title, "Use UTC timestamps")
        self.assertEqual(by_id["D-503"].topic, None)   # never sectioned: no topic invented
        # D-520's section had prose before its first entry, so it is deliberately left as the
        # old, one-level-too-deep heading — still malformed, and that is reported, not hidden.
        self.assertEqual(parsed.malformed, [(29, "### D-520 — Code comments explain the code")])

    def test_entry_that_already_has_a_topic_keeps_it(self):
        text = SECTIONS_LOG.replace(
            "### D-500 — Use UTC timestamps\n\n**Status:** locked",
            "### D-500 — Use UTC timestamps\n\n"
            "**Topic:** Already set\n\n**Status:** locked")
        w = Fixture(self.tmp, decisions_text=text)
        w.run(apply_=True)
        result = w.log().read_text(encoding="utf-8")
        self.assertIn("**Topic:** Already set", result)
        self.assertEqual(result.count("**Topic:**"), 1)

    def test_prose_before_first_entry_is_left_and_reported(self):
        w = base_fixture(self.tmp)
        code, out = w.run(apply_=False)
        self.assertEqual(code, 0)
        self.assertIn("would migrate", out)
        report = w.report()
        self.assertIn("## Client tooling (D-520–D-599)", w.log().read_text(encoding="utf-8"))
        self.assertIn("Client tooling", report)
        self.assertIn("cannot be placed mechanically", report)


class SupersededToPointer(Base):
    def test_named_successor_becomes_a_pointer(self):
        w = base_fixture(self.tmp)
        w.run(apply_=True)
        text = w.log().read_text(encoding="utf-8")
        self.assertIn("## D-501 — Replaced by D-503", text)
        self.assertNotIn("old rule", text)

        ctx = w.context()
        parsed = ctx.grammar.parse_file(w.log())
        entry = next(e for e in parsed.entries if e.ident == "D-501")
        self.assertEqual(entry.replaced_by, "D-503")
        self.assertEqual(entry.body.strip(), "")

    def test_no_successor_named_is_left_alone_and_reported(self):
        w = base_fixture(self.tmp)
        w.run(apply_=True)
        text = w.log().read_text(encoding="utf-8")
        self.assertIn("## D-104 — Log levels are set per module", text)
        self.assertIn("Superseded 2026-01-15 by a later design", text)
        report = w.report()
        self.assertIn("D-104 is superseded with no successor named", report)

    def test_successor_missing_from_the_log_is_not_turned_into_a_pointer(self):
        text = SECTIONS_LOG.replace("superseded by D-503", "superseded by D-999")
        w = Fixture(self.tmp, decisions_text=text)
        w.run(apply_=True)
        result = w.log().read_text(encoding="utf-8")
        # its section still flattens (mig 1 is independent of mig 2); only the pointer is refused
        self.assertIn("## D-501 — Cache invalidation on deploy", result)
        self.assertIn("old rule", result)
        self.assertIn("D-999, which is not in this log", w.report())


class BulletTrapsToHeadings(Base):
    def test_bullets_become_headings_and_index_becomes_a_block(self):
        w = base_fixture(self.tmp)
        code, _ = w.run(apply_=True)
        self.assertEqual(code, 0)
        text = w.trap("traps.md").read_text(encoding="utf-8")
        self.assertIn("## T-1 — A committed text fixture is LF, whatever the machine wrote it "
                     "with", text)
        self.assertNotIn("- **1.", text)
        self.assertNotIn("## Index\n\n1.", text)
        self.assertIn("<!-- gv:generated:start id=trap-index -->", text)
        self.assertIn("<!-- gv:generated:end id=trap-index -->", text)

        platform = w.trap("platform-traps.md").read_text(encoding="utf-8")
        self.assertIn("## T-3 — Never cache an error response", platform)

        ctx = w.context()
        scope = ctx.registry.find("alpha")
        nums = {e.num: e.title for doc in ctx.trap_files(scope)
               for e in ctx.grammar.traps(doc, ctx.trap_prefix)}
        self.assertEqual(nums[1], "A committed text fixture is LF, whatever the machine wrote "
                                  "it with")
        self.assertEqual(nums[2], "A byte-identical restore is not enough — touch it")
        self.assertEqual(nums[3], "Never cache an error response")

    def test_dropped_index_number_with_no_body_is_reported_as_the_maximum(self):
        w = base_fixture(self.tmp)
        w.run(apply_=False)
        report = w.report()
        self.assertIn("T-49 has no body anywhere and was dropped from the index", report)
        self.assertIn("highest number ever mentioned", report)

    def test_missing_block_declaration_is_reported(self):
        w = base_fixture(self.tmp, declare_trap_block=False)
        w.run(apply_=False)
        report = w.report()
        self.assertIn('id = "trap-index"', report)
        # traps.md held the hand-written Index, so it — and only it — becomes the block's
        # target; T-3's body lives in platform-traps.md, so `sources` picks that up too. The
        # suggestion is a `glob`, never a `file`: a `[[blocks.project]]` entry applies to every
        # governed scope, and a `file` naming this exact path would error in any scope that
        # does not have one — a `glob` matching nothing there is simply empty.
        self.assertIn('glob = "working-files/traps.md"', report)
        self.assertNotIn('file = "working-files/traps.md"', report)
        self.assertIn('sources = "working-files/*traps*.md"', report)

    def test_block_already_declared_is_not_reported_again(self):
        w = base_fixture(self.tmp, declare_trap_block=True)
        w.run(apply_=False)
        report = w.report()
        self.assertNotIn("## Config", report)

    def test_bites_when_missing_is_noted_in_a_separate_notes_section(self):
        w = base_fixture(self.tmp)
        w.run(apply_=False)
        report = w.report()
        notes = report.split("## Notes", 1)[1].split("## Could not be migrated", 1)[0]
        self.assertIn("T-1 has no **Bites when:** line", notes)
        # the plan predicts trap-entries' own warning for exactly this — it belongs under
        # "Expected", never under "should not happen".
        expected = report.split("Expected after this migration", 1)[1]
        self.assertIn("T-1 has no **Bites when:** — the trap index shows it as a dash",
                     expected)
        should_not = report.split("New after this migration", 1)[1] \
            if "New after this migration" in report else ""
        self.assertNotIn("T-1 has no **Bites when:**", should_not)


class DryRunAndApply(Base):
    def test_dry_run_changes_nothing_on_disk(self):
        w = base_fixture(self.tmp)
        before = w.log().read_bytes()
        before_traps = w.trap("traps.md").read_bytes()
        code, out = w.run(apply_=False)
        self.assertEqual(code, 0)
        self.assertEqual(w.log().read_bytes(), before)
        self.assertEqual(w.trap("traps.md").read_bytes(), before_traps)
        self.assertTrue((w.root / layout.GOV_DIR / "migration-report.md").is_file())
        self.assertIn("would migrate", out)

    def test_apply_refuses_when_a_touched_file_is_dirty(self):
        w = base_fixture(self.tmp)
        wtext(w.log(), w.log().read_text(encoding="utf-8") + "\n<!-- uncommitted edit -->\n")
        code, out = w.run(apply_=True)
        self.assertEqual(code, 2)
        self.assertIn("uncommitted changes", out)
        # nothing was migrated: the section is still there
        self.assertIn("## Server-side platform", w.log().read_text(encoding="utf-8"))

    def test_apply_writes_the_report_with_git_commands(self):
        w = base_fixture(self.tmp)
        w.run(apply_=True)
        report = w.report()
        self.assertIn("**Applied.**", report)
        self.assertIn("git add", report)
        self.assertIn("git commit", report)
        self.assertIn(str(w.root), report)

    def test_idempotent_second_run_reports_nothing_left_to_migrate(self):
        w = base_fixture(self.tmp)
        w.run(apply_=True)
        w.commit()
        code, out = w.run(apply_=True)
        self.assertEqual(code, 0)
        self.assertIn("migrated 0 file(s)", out)
        status = subprocess.run(["git", "-C", str(w.root), "status", "--porcelain",
                                 "--", "projects"], capture_output=True, text=True, check=True)
        self.assertEqual(status.stdout.strip(), "")

    def test_installer_wires_the_migrate_subcommand(self):
        w = base_fixture(self.tmp)
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(w.home)
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                code = installer.main(["migrate", "--root", str(w.root)])
        finally:
            if old_home is not None:
                os.environ["HOME"] = old_home
        self.assertEqual(code, 0)
        self.assertIn("would migrate", out.getvalue())


class CRLFPreserved(Base):
    def test_crlf_line_endings_survive_the_migration(self):
        crlf_text = SECTIONS_LOG.replace("\n", "\r\n")
        w = Fixture(self.tmp, decisions_text=crlf_text)
        w.run(apply_=True)
        raw = w.log().read_bytes()
        self.assertIn(b"\r\n", raw)
        # every line ending is CRLF: no lone "\n" survives once every "\r\n" is stripped out
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))
        self.assertIn(b"D-500", raw)
        self.assertIn(b"Replaced by D-503", raw)

    def test_crlf_trap_file_survives_the_migration(self):
        crlf_traps = TRAPS_INDEX.replace("\n", "\r\n")
        w = Fixture(self.tmp, decisions_text=SECTIONS_LOG, trap_files={"traps.md": crlf_traps})
        w.run(apply_=True)
        raw = w.trap("traps.md").read_bytes()
        self.assertIn(b"\r\n", raw)
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))
        self.assertIn(b"T-1", raw)


class FencedExamplesLeftAlone(Base):
    def test_fenced_bullet_index_and_topic_are_left_alone(self):
        w = Fixture(self.tmp, decisions_text=FENCED_LOG, trap_files={"traps.md": FENCED_TRAPS},
                   registry_text=REGISTRY.replace("500-599", "600-699"))
        before_log = w.log().read_bytes()
        before_traps = w.trap("traps.md").read_bytes()
        code, _ = w.run(apply_=True)
        self.assertEqual(code, 0)
        # the fenced section, index and topic are examples, not live markup: nothing
        # inside the fence is touched, even though the file also holds a real, already-flat
        # entry and a real, already-migrated trap right next to it
        self.assertEqual(w.log().read_bytes(), before_log)
        self.assertEqual(w.trap("traps.md").read_bytes(), before_traps)


class SuccessorResolution(Base):
    def test_status_line_wins_over_distracting_body_mentions(self):
        body = ("**Status:** superseded by D-502\n\n"
               "This replaced D-400, which was superseded by D-500 in turn. Now superseded "
               "by D-501.")
        succ, found = migrate._successor(body)
        self.assertEqual(succ, "D-502")

    def test_bulleted_status_line_with_labelled_successor(self):
        # The one-line field style: `- **Status:** … · **Superseded by:** D-023 (date)`.
        body = ("- **Status:** superseded · **Origin:** review, 2026-01-10 · "
                "**Superseded by:** D-023 (2026-09-01)\n\nThis replaced D-400.")
        succ, found = migrate._successor(body)
        self.assertEqual(succ, "D-023")

    def test_several_distinct_body_successors_are_ambiguous(self):
        body = ("**Status:** superseded\n\n"
               "This replaced D-400, which was superseded by D-500 in turn. Now superseded "
               "by D-501.")
        succ, found = migrate._successor(body)
        self.assertIsNone(succ)
        self.assertEqual(set(found), {"D-500", "D-501"})

    def test_status_line_successor_and_ambiguous_body_end_to_end(self):
        w = Fixture(self.tmp, decisions_text=AMBIGUOUS_SUCCESSOR_LOG)
        code, _ = w.run(apply_=True)
        self.assertEqual(code, 0)
        text = w.log().read_text(encoding="utf-8")
        self.assertIn("## D-504 — Replaced by D-506", text)
        self.assertIn("## D-505 — Ambiguous successor is left for a person", text)
        report = w.report()
        self.assertIn("D-505 names several possible successors (D-500, D-501) and none is on "
                     "the **Status:** line", report)


class TrapIndexSources(Base):
    def test_sources_link_traps_across_files_with_no_generated_blocks_finding(self):
        w = base_fixture(self.tmp, declare_trap_block=True, trap_block=TRAP_BLOCK_SOURCES)
        code, _ = w.run(apply_=True)
        self.assertEqual(code, 0)
        ctx = w.context()
        self.assertEqual(cli.cmd_index(ctx), 0)
        findings = cli.collect(ctx, ctx.registry.scopes, workspace=True)
        errors = [m for cid, f in findings for m in f.errors if cid == "generated-blocks"]
        self.assertEqual(errors, [])
        text = w.trap("traps.md").read_text(encoding="utf-8")
        self.assertIn("T-1", text)
        self.assertIn("T-2", text)
        self.assertIn("T-3", text)
        # T-3's body lives in platform-traps.md, so its row links there
        self.assertIn("platform-traps.md", text)

    def test_sources_on_a_non_trap_index_block_is_rejected(self):
        root = self.tmp / "root"
        root.mkdir()
        text = CONFIG.format(version=__version__, trap_block="").replace(
            '{ file = "DECISIONS.md", id = "decision-index" }',
            '{ file = "DECISIONS.md", id = "decision-index", sources = "*.md" }')
        wtext(root / CFG, text)
        wtext(root / "projects.toml", REGISTRY)
        with self.assertRaises(config.ConfigError) as cm:
            config.load(root, root)
        self.assertIn("sources", str(cm.exception))
        self.assertIn("trap-index", str(cm.exception))

    def test_without_sources_behavior_is_unchanged(self):
        # The old, glob-based block (one marker pair required per matched file) is untouched by
        # the `sources` feature: migrate only ever adds markers to the file that held the
        # hand-written Index, so platform-traps.md — matched by the glob, never given markers —
        # is reported missing exactly as it would have been before `sources` existed.
        w = base_fixture(self.tmp, declare_trap_block=True, trap_block=TRAP_BLOCK)
        code, _ = w.run(apply_=True)
        self.assertEqual(code, 0)
        ctx = w.context()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.cmd_index(ctx)
        self.assertEqual(code, 1)
        self.assertIn("platform-traps.md :: trap-index (markers)", out.getvalue())
        text = w.trap("traps.md").read_text(encoding="utf-8")
        self.assertIn("T-1", text)
        self.assertIn("T-2", text)
        self.assertNotIn("platform-traps.md", text)   # no sources: only this file's own traps


class BeforeAfterOverlay(Base):
    def test_generated_blocks_reads_migrated_text_and_predicts_stale(self):
        w = base_fixture(self.tmp, declare_trap_block=True, trap_block=TRAP_BLOCK_SOURCES)
        ctx = w.context()
        p = migrate.plan(ctx)
        before = migrate._findings(ctx)
        after = migrate._findings(ctx, p.edits)
        before_msgs = [m for _, _, m in before if "trap-index" in m]
        after_msgs = [m for _, _, m in after if "trap-index" in m]
        self.assertTrue(any("has no markers" in m for m in before_msgs), before_msgs)
        self.assertFalse(any("has no markers" in m for m in after_msgs), after_msgs)
        self.assertTrue(any("is stale" in m for m in after_msgs), after_msgs)

    def test_finding_that_only_moved_lines_is_neither_fixed_nor_new(self):
        w = base_fixture(self.tmp)
        ctx = w.context()
        p = migrate.plan(ctx)
        before = migrate._findings(ctx)
        after = migrate._findings(ctx, p.edits)
        before_line = next(m for _, _, m in before
                          if "D-520" in m and "is not a decision entry heading" in m)
        after_line = next(m for _, _, m in after
                         if "D-520" in m and "is not a decision entry heading" in m)
        self.assertNotEqual(before_line, after_line)   # the line number really did move
        self.assertEqual(migrate._norm(before_line), migrate._norm(after_line))
        report_path = self.tmp / "report.md"
        migrate._write_report(ctx, report_path, p, before, after, applied=False)
        report = report_path.read_text(encoding="utf-8")
        fixed_section = report.split("Fixed by this migration:", 1)[1].split("\n\n", 1)[0] \
            if "Fixed by this migration:" in report else ""
        new_section = report.split("New after this migration", 1)[-1] \
            if "New after this migration" in report else ""
        self.assertNotIn("is not a decision entry heading", fixed_section)
        self.assertNotIn("is not a decision entry heading", new_section)


class BOMHandling(Base):
    def test_bom_survives_apply(self):
        w = base_fixture(self.tmp, bom=True)
        code, _ = w.run(apply_=True)
        self.assertEqual(code, 0)
        raw = w.log().read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        text = raw.decode("utf-8-sig")
        self.assertIn("## D-500 — Use UTC timestamps", text)


class UnreadableFiles(Base):
    def test_non_utf8_trap_file_gives_exit_2(self):
        w = base_fixture(self.tmp)
        bad_bytes = b"\xff\xfe not valid utf-8"
        bad_path = w.trap("platform-traps.md")
        bad_path.write_bytes(bad_bytes)
        before_log = w.log().read_bytes()
        code, out = w.run(apply_=True)
        self.assertEqual(code, 2)
        self.assertIn(str(bad_path), out)
        self.assertNotIn("Traceback", out)
        self.assertEqual(bad_path.read_bytes(), bad_bytes)
        self.assertEqual(w.log().read_bytes(), before_log)


class ApplyGitBlockers(Base):
    def test_apply_refuses_when_a_file_is_outside_any_git_repo(self):
        w = base_fixture(self.tmp, git_repo=False)
        before_log = w.log().read_bytes()
        code, out = w.run(apply_=True)
        self.assertEqual(code, 2)
        self.assertIn("not inside a git repo", out)
        self.assertEqual(w.log().read_bytes(), before_log)

    def test_apply_refuses_when_git_status_fails(self):
        w = base_fixture(self.tmp)
        before_log = w.log().read_bytes()
        with mock.patch("govern.migrate.git", return_value=None):
            code, out = w.run(apply_=True)
        self.assertEqual(code, 2)
        self.assertIn("git status", out)
        self.assertEqual(w.log().read_bytes(), before_log)


class AtomicApply(Base):
    def test_partial_replace_failure_restores_every_original(self):
        w = base_fixture(self.tmp)
        before = {p: p.read_bytes() for p in
                 (w.log(), w.trap("traps.md"), w.trap("platform-traps.md"))}
        real_replace = os.replace
        calls: list = []

        def flaky(src, dst):
            calls.append(dst)
            if len(calls) == 2:
                raise OSError("disk full")
            real_replace(src, dst)

        with mock.patch("govern.migrate.os.replace", side_effect=flaky):
            code, out = w.run(apply_=True)
        self.assertEqual(code, 2)
        self.assertGreaterEqual(len(calls), 2)
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)
        report = w.report()
        self.assertIn("Apply failed and was rolled back", report)
        self.assertIn("Nothing was changed", report)


class BulletTitleContinuations(Base):
    def test_mid_sentence_title_and_missing_blank_lines(self):
        w = Fixture(self.tmp, decisions_text=SECTIONS_LOG,
                   trap_files={"traps.md": MIDSENTENCE_TRAPS})
        code, _ = w.run(apply_=True)
        self.assertEqual(code, 0)
        text = w.trap("traps.md").read_text(encoding="utf-8")
        self.assertIn("## T-1 — First trap", text)
        self.assertIn("## T-2 — Environment variable expansion does not contain a space", text)
        self.assertIn("Environment variable expansion does not contain a space, and on Unix "
                     "systems the missing quoting still executes.", text)
        self.assertNotIn("\n, and on Unix", text)
        self.assertIn("\n\n## T-2 —", text)     # a blank line inserted, though the source had none
        report = w.report()
        self.assertIn("T-2: the bold text does not end its sentence", report)


class BodylessEntryAtEOF(Base):
    def test_bare_heading_at_end_of_file_gets_its_topic(self):
        w = Fixture(self.tmp, decisions_text=BARE_EOF_LOG,
                   registry_text=REGISTRY.replace("500-599", "700-799"))
        code, _ = w.run(apply_=True)
        self.assertEqual(code, 0)
        text = w.log().read_text(encoding="utf-8")
        self.assertIn("## D-700 — Bare heading with nothing after it\n\n"
                     "**Topic:** Trailing Topic", text)
        report = w.report()
        self.assertIn("'Trailing Topic (D-700-D-799)' -> **Topic:** Trailing Topic on 1 entry",
                     report)


class MalformedIndexLeftAlone(Base):
    def test_table_index_is_left_alone(self):
        self._check(INDEX_TABLE_TRAPS, "table, bullets, or wrapped/indented lines")

    def test_bulleted_index_is_left_alone(self):
        self._check(INDEX_BULLETS_TRAPS, "table, bullets, or wrapped/indented lines")

    def test_wrapped_index_is_left_alone(self):
        self._check(INDEX_WRAPPED_TRAPS, "table, bullets, or wrapped/indented lines")

    def _check(self, content, needle):
        w = Fixture(self.tmp, decisions_text=SECTIONS_LOG, trap_files={"traps.md": content})
        code, _ = w.run(apply_=True)
        self.assertEqual(code, 0)
        text = w.trap("traps.md").read_text(encoding="utf-8")
        self.assertIn("## Index", text)                     # the hand-written heading survives
        self.assertNotIn("<!-- gv:generated:start id=trap-index -->", text)
        self.assertIn("## T-1 —", text)     # the real bullets still migrate independently
        report = w.report()
        self.assertIn(needle, report)
        self.assertNotIn("replaced by the generated", report)


class DuplicateTrapNumber(Base):
    def test_same_number_two_files_neither_migrated_both_reported(self):
        w = Fixture(self.tmp, decisions_text=SECTIONS_LOG,
                   trap_files={"traps.md": DUP_A, "other-traps.md": DUP_B})
        code, _ = w.run(apply_=True)
        self.assertEqual(code, 0)
        a = w.trap("traps.md").read_text(encoding="utf-8")
        b = w.trap("other-traps.md").read_text(encoding="utf-8")
        self.assertIn("- **21.", a)
        self.assertIn("- **21.", b)
        self.assertNotIn("## T-21", a)
        self.assertNotIn("## T-21", b)
        report = w.report()
        self.assertIn("T-21 has a body in more than one file", report)
        self.assertIn("traps.md:", report)
        self.assertIn("other-traps.md:", report)


class PinRefusal(Base):
    def test_pinned_to_other_engine_version_refuses_and_says_to_upgrade(self):
        w = base_fixture(self.tmp, engine_pin="0.1.0")
        code, out = w.run(apply_=False)
        self.assertEqual(code, 2)
        self.assertIn("upgrade", out.lower())

    def test_readme_migrate_section_says_to_upgrade_first(self):
        readme = (ENGINE / "README.md").read_text(encoding="utf-8")
        migrate_section = readme.split("`migrate` moves a project", 1)[1]
        self.assertIn("upgrade", migrate_section[:600].lower())


class CommitCommandsAndParagraphs(Base):
    def test_commit_paths_with_spaces_are_quoted(self):
        root = self.tmp / "root"
        home = self.tmp / "home"
        home.mkdir()
        root.mkdir()
        wtext(root / CFG, CONFIG.format(version=__version__, trap_block=""))
        registry_text = REGISTRY.replace('governance = "projects/alpha"',
                                         'governance = "projects/alpha two"')
        wtext(root / "projects.toml", registry_text)
        wtext(root / "governance/DECISIONS.md", FM + "# Decisions\n\n" + DECISIONS_INDEX +
             "\n## W-1 — Root decision\n\n**Status:** locked\n\n**Rule:** r.\n\n**Why:** w.\n")
        wtext(root / "projects/alpha two/DECISIONS.md", SECTIONS_LOG)
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
        self.assertEqual(code, 0)
        report = (root / layout.GOV_DIR / "migration-report.md").read_text(encoding="utf-8")
        self.assertIn("git add 'projects/alpha two/DECISIONS.md'", report)

    def test_engine_line_and_applied_are_separate_paragraphs(self):
        w = base_fixture(self.tmp)
        w.run(apply_=True)
        report = w.report()
        self.assertIn(f"Engine {__version__}.\n\n**Applied.**", report)

    def test_log_shared_by_two_scopes_is_planned_once(self):
        registry_text = REGISTRY + (
            '\n[[project]]\nname = "alpha-mirror"\ndir = "alpha"\ntier = "full"\n'
            'governance = "projects/alpha"\nid_prefix = "D"\nid_range = "500-599"\n')
        w = Fixture(self.tmp, decisions_text=SECTIONS_LOG, registry_text=registry_text)
        ctx = w.context()
        p = migrate.plan(ctx)
        topic_changes = [c.detail for c in p.changes
                         if c.path == w.log() and "Server-side platform" in c.detail]
        self.assertEqual(len(topic_changes), 1)
        prose_problems = [pr.reason for pr in p.problems
                          if pr.path == w.log() and "cannot be placed mechanically" in pr.reason]
        self.assertEqual(len(prose_problems), 1)


class TwoGitRepos(Base):
    def test_two_repos_get_separate_commit_blocks(self):
        root = self.tmp / "root"
        home = self.tmp / "home"
        home.mkdir()
        root.mkdir()
        registry_text = """
[workspace]
id_prefix = "W"
id_range = "1-99"

[[project]]
name = "alpha"
dir = "alpha"
tier = "full"
governance = "projects/alpha"
id_prefix = "D"
id_range = "500-599"

[[project]]
name = "beta"
dir = "beta"
tier = "full"
governance = "projects/beta"
id_prefix = "D"
id_range = "600-699"
"""
        wtext(root / CFG, CONFIG.format(version=__version__, trap_block=""))
        wtext(root / "projects.toml", registry_text)
        wtext(root / "governance/DECISIONS.md", FM + "# Decisions\n\n" + DECISIONS_INDEX +
             "\n## W-1 — Root decision\n\n**Status:** locked\n\n**Rule:** r.\n\n**Why:** w.\n")
        beta_log = (SECTIONS_LOG.replace("D-500", "D-600").replace("D-501", "D-601")
                   .replace("D-503", "D-603").replace("D-520", "D-620").replace("D-104", "D-604"))
        wtext(root / "projects/alpha/DECISIONS.md", SECTIONS_LOG)
        wtext(root / "projects/beta/DECISIONS.md", beta_log)
        for sub in ("projects/alpha", "projects/beta"):
            git(root / sub, "init", "-q")
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(home)
        try:
            code = migrate.run(root, False)
        finally:
            if old_home is not None:
                os.environ["HOME"] = old_home
        self.assertEqual(code, 0)
        report = (root / layout.GOV_DIR / "migration-report.md").read_text(encoding="utf-8")
        commit_section = report.split("## Commit", 1)[1]
        self.assertEqual(commit_section.count("In `"), 2)
        self.assertIn(str(root / "projects/alpha"), commit_section)
        self.assertIn(str(root / "projects/beta"), commit_section)


if __name__ == "__main__":
    unittest.main()
