"""The base module and the decision rules it feeds: settled, removed, taken ids, and what cannot be compared."""
from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from base_support import *  # noqa: E402,F401,F403

# ---------------------------------------------------------------------------- the module

class BaseModule(Case):
    """`govern.base`: what a file held at a commit, asked of one repository."""

    def test_exists_tells_absent_from_present_and_from_git_not_running(self):
        w = self.solo()
        head = base.resolve(w.root, "HEAD")
        self.assertIs(base.exists(w.root, head, LOG), True)
        self.assertIs(base.exists(w.root, head, "docs/NOPE.md"), False)
        empty = self.tmp / "nothing-on-path"
        empty.mkdir()
        with mock.patch.dict(os.environ, {"PATH": str(empty)}):
            self.assertIsNone(base.exists(w.root, head, LOG))
        # A commit git cannot read holds nothing it can vouch for: not "the file is not there".
        self.assertIsNone(base.exists(w.root, "0" * 40, LOG))

    def test_files_lists_one_directory_at_a_commit_and_never_reads_a_failure_as_empty(self):
        w = self.solo()
        w.write("top.md", "# Top\n")
        w.write("docs/working/café.md", "# More\n")
        w.commit("more")
        head = base.resolve(w.root, "HEAD")
        self.assertEqual(base.files(w.root, head, "docs"), [LOG])          # not what is below it
        self.assertEqual(base.files(w.root, head, ""), ["top.md"])
        self.assertEqual(sorted(base.files(w.root, head, "docs/working")),
                         ["docs/working/café.md", TRAPS])
        self.assertEqual(base.files(w.root, head, "docs/nope"), [])
        with self.assertRaises(base.CannotCompare) as caught:
            base.files(w.root, "0" * 40, "docs")
        self.assertEqual(caught.exception.reason, "git could not list docs at 000000000000")

    def test_renames_says_what_was_renamed_between_a_commit_and_the_working_tree(self):
        w = self.solo()
        head = base.resolve(w.root, "HEAD")
        self.assertEqual(base.renames(w.root, head), {})
        w.git("mv", LOG, "docs/LOG é.md")
        w.append("docs/LOG é.md", decision("P", 4))          # renamed and edited
        w.append(TRAPS, trap(2))                             # edited only
        self.assertEqual(base.renames(w.root, head), {"docs/LOG é.md": LOG})
        with self.assertRaises(base.CannotCompare) as caught:
            base.renames(w.root, "0" * 40)
        self.assertEqual(caught.exception.reason,
                         "git could not say what was renamed since 000000000000")

    def test_since_also_says_what_was_deleted_in_the_same_one_question(self):
        w = self.solo()
        head = base.resolve(w.root, "HEAD")
        self.assertEqual(base.since(w.root, head), ({}, []))
        w.git("mv", LOG, "docs/LOG.md")                      # renamed: not deleted
        w.git("rm", "-q", TRAPS)                             # deleted, and git told
        w.write("docs/working/new é.md", "# New\n")          # not tracked: not git's to list
        self.assertEqual(base.since(w.root, head), ({"docs/LOG.md": LOG}, [TRAPS]))
        w.git("add", "-A")
        os.rename(w.root / "docs/LOG.md", w.root / "docs/elsewhere.md")
        # Moved without telling git: the tracked name is gone from the tree, which is deleted.
        self.assertEqual(base.since(w.root, head), ({}, [LOG, TRAPS]))
        with self.assertRaises(base.CannotCompare) as caught:
            base.since(w.root, "0" * 40)
        self.assertEqual(caught.exception.reason,
                         "git could not say what was renamed since 000000000000")

    def test_read_gives_the_committed_text_not_the_tree(self):
        w = self.solo()
        committed = w.read(LOG)
        w.append(LOG, decision("P", 4))
        self.assertEqual(base.read(w.root, base.resolve(w.root, "HEAD"), LOG), committed)

    def test_read_drops_a_bom_and_keeps_crlf(self):
        text = fm() + "# Decisions\n" + decision("P", 1)
        w = self.solo(git="init")
        w.write(LOG, ("\ufeff" + text.replace("\n", "\r\n")).encode("utf-8"))
        w.commit("start", index=False)
        got = base.read(w.root, base.resolve(w.root, "HEAD"), LOG)
        self.assertEqual(got, text.replace("\n", "\r\n"))

    def test_read_refuses_bytes_that_are_not_utf8_and_a_file_that_is_not_there(self):
        w = self.solo(git="init")
        w.write(LOG, b"# Decisions\n\xff\xfe\n")
        w.commit("start", index=False)
        head = base.resolve(w.root, "HEAD")
        with self.assertRaises(base.CannotCompare) as caught:
            base.read(w.root, head, LOG)
        self.assertEqual(caught.exception.reason,
                         f"{LOG} at {head[:12]} is not valid UTF-8 (byte 12)")
        with self.assertRaises(base.CannotCompare) as caught:
            base.read(w.root, head, "docs/NOPE.md")
        self.assertEqual(caught.exception.reason,
                         f"git could not read docs/NOPE.md at {head[:12]}")

    def test_resolve_and_merge_base(self):
        w = self.solo()
        start = w.git("rev-parse", "HEAD").strip()
        w.branch("work")
        w.append(LOG, decision("P", 4))
        w.commit("ours")
        w.switch("main")
        w.append(LOG, decision("P", 5))
        w.commit("theirs")
        w.switch("work")
        self.assertEqual(base.resolve(w.root, "main"), w.git("rev-parse", "main").strip())
        self.assertEqual(base.merge_base(w.root, "main"), start)
        self.assertIsNone(base.resolve(w.root, "origin/nope"))
        self.assertIsNone(base.merge_base(w.root, "origin/nope"))
        self.assertIsNone(base.resolve(w.root, LOG))        # a path is not a commit

    def test_a_ref_that_reads_as_an_option_is_never_handed_to_git(self):
        w = self.solo()
        for ref in ("", "-h", "--all", "--output=x"):
            with mock.patch("govern.base.git") as called:
                self.assertIsNone(base.resolve(w.root, ref))
                self.assertIsNone(base.merge_base(w.root, ref))
                called.assert_not_called()

    def test_ls_lists_a_directory_at_a_commit_by_its_real_names(self):
        w = self.solo(git="init")
        w.write("docs/working/traps-café.md", "# More\n")
        w.commit("start")
        head = base.resolve(w.root, "HEAD")
        self.assertEqual(sorted(base.ls(w.root, head, "docs/working")),
                         ["docs/working/traps-café.md", "docs/working/traps.md"])
        self.assertEqual(base.ls(w.root, head, "docs/nope"), [])
        self.assertIsNone(base.ls(w.root, "0" * 40, "docs/working"))

    def test_a_hooks_git_dir_does_not_redirect_the_question(self):
        # A pre-commit hook exports GIT_DIR; the answer must still be about the repository
        # asked, not the hook's.
        w = self.solo()
        other = self.tmp / "other"
        other.mkdir()
        w.init(at=other)
        with mock.patch.dict(os.environ, {"GIT_DIR": str(other / ".git")}):
            self.assertEqual(base.resolve(w.root, "HEAD"), w.git("rev-parse", "HEAD").strip())
            self.assertIs(base.exists(w.root, base.resolve(w.root, "HEAD"), LOG), True)

    def test_compare_returns_both_sides_per_repository(self):
        w = self.team(nested=True)
        w.append("governance/DECISIONS.md", decision("W", 3))
        w.append("beta/working-files/traps.md", trap(2))
        cmp = base.compare(w.context())
        self.assertEqual([(r.label, r.root, r.commit is not None) for r in cmp.repos],
                         [(".", True, True), ("beta", False, True)])
        root, beta = cmp.repos
        self.assertEqual(root.commit, w.git("rev-parse", "HEAD").strip())
        self.assertEqual(beta.commit, w.git("rev-parse", "HEAD", at=w.root / "beta").strip())
        self.assertEqual(root.scopes, {"workspace", "alpha", "gamma"})
        ids = lambda items, kind: [(i.log, i.entry.ident) for i in items if i.kind == kind]
        self.assertEqual(ids(root.base, base.DECISION), [
            ("governance/DECISIONS.md", "W-1"), ("governance/DECISIONS.md", "W-2"),
            ("projects/alpha/DECISIONS.md", "A-100"), ("projects/alpha/DECISIONS.md", "A-101"),
            ("projects/gamma/DECISIONS.md", "G-300"), ("projects/gamma/DECISIONS.md", "G-301")])
        self.assertEqual(ids(root.current, base.DECISION)[:3], [
            ("governance/DECISIONS.md", "W-1"), ("governance/DECISIONS.md", "W-2"),
            ("governance/DECISIONS.md", "W-3")])
        self.assertEqual(ids(beta.base, base.TRAP), [("beta/working-files/traps.md", "T-1")])
        self.assertEqual(ids(beta.current, base.TRAP), [("beta/working-files/traps.md", "T-1"),
                                                        ("beta/working-files/traps.md", "T-2")])
        self.assertEqual((cmp.ref, cmp.complete, cmp.not_compared, root.theirs), (None, True, [], []))

    def test_compare_names_what_could_not_be_compared_and_why(self):
        w = self.team(nested=True)
        cmp = base.compare(w.context("main"))
        self.assertEqual([(r.label, r.commit, r.reason) for r in cmp.not_compared],
                         [("beta", None, "main names no commit in this repository")])
        root = cmp.repos[0]
        self.assertEqual((root.commit, root.tip), (w.git("rev-parse", "main").strip(),) * 2)
        self.assertEqual(len(root.theirs), len(root.base))

    def test_two_projects_that_share_one_log_are_compared_once(self):
        w = self.team()
        w.append("projects.toml", PROJECT.format(name="delta", dir="projects/alpha", prefix="A",
                                                 lo=100, hi=199))
        cmp = base.compare(w.context(), traps=False)
        for side in ("base", "current"):
            self.assertEqual([i.entry.ident for i in cmp.side(side)
                              if i.log == "projects/alpha/DECISIONS.md"], ["A-100", "A-101"])

    def test_without_traps_no_trap_file_is_read(self):
        w = self.team()
        cmp = base.compare(w.context(), traps=False)
        self.assertEqual(cmp.side("base", base.TRAP) + cmp.side("current", base.TRAP), [])
        self.assertEqual(len(cmp.side("base", base.DECISION)), 6)


# ---------------------------------------------------------------------------- rule 1

class SettledDecisionChangedInPlace(Case):
    """A locked decision's text differs from the base, and no dated `**Revised:**` line says
    so."""

    EDIT = ("before acting on it.\n\n**Why:** the next reader was not in the room.\n\n## P-2",
            "before acting on it, always.\n\n**Why:** the next reader was not in the room.\n\n## P-2")

    def _edited(self, revised: str = "", **kw) -> Solo:
        """`P-1`, locked, reworded in the working tree, with `revised` put under its status."""
        w = self.solo(**kw)
        w.edit(LOG, *self.EDIT)
        if revised:
            w.edit(LOG, "Title 1\n\n**Status:** locked\n", f"Title 1\n\n**Status:** locked\n{revised}")
        return w

    def test_an_uncommitted_edit_is_a_warning_and_the_gate_still_exits_zero(self):
        w = self._edited()
        self.assertEqual(self.found(w), (0, [WARN + changed(LOG, "P-1")]))

    def test_under_base_the_same_edit_is_an_error(self):
        w = self._edited()
        self.assertEqual(self.found(w, "--base", "main"), (1, [ERROR + changed(LOG, "P-1")]))

    def test_committing_the_edit_ends_the_advice_but_not_the_gate(self):
        w = self.solo()
        w.branch("work")
        w.edit(LOG, *self.EDIT)
        w.commit("reword")
        self.silent(w)
        self.assertEqual(self.found(w, "--base", "main"), (1, [ERROR + changed(LOG, "P-1")]))

    def test_a_revised_line_added_makes_the_edit_legal(self):
        w = self._edited("\n**Revised:** 2026-10-06 (always, not sometimes)\n")
        self.silent(w)
        self.silent(w, "--base", "main")

    def test_a_revised_line_reworded_on_the_same_date_makes_a_second_edit_legal(self):
        w = self.solo()
        w.edit(LOG, "Title 1\n\n**Status:** locked\n",
               "Title 1\n\n**Status:** locked\n\n**Revised:** 2026-10-06 (first change)\n")
        w.commit("revised once")
        w.edit(LOG, *self.EDIT)
        self.assertEqual(self.found(w), (0, [WARN + changed(LOG, "P-1")]))
        w.edit(LOG, "(first change)", "(first change; and always)")
        self.silent(w)
        self.silent(w, "--base", "main")

    def test_a_revised_line_older_than_the_last_one_does_not(self):
        w = self.solo()
        w.edit(LOG, "Title 1\n\n**Status:** locked\n",
               "Title 1\n\n**Status:** locked\n\n**Revised:** 2026-05-01 (first change)\n")
        w.commit("revised once")
        w.edit(LOG, *self.EDIT)
        w.edit(LOG, "**Revised:** 2026-05-01 (first change)", "**Revised:** 2026-01-01 (backdated)")
        self.assertEqual(self.found(w), (0, [WARN + changed(LOG, "P-1")]))
        self.assertEqual(self.found(w, "--base", "main"), (1, [ERROR + changed(LOG, "P-1")]))

    def test_a_revised_line_whose_date_is_no_date_does_not(self):
        for line in ("**Revised:** 2026-02-30 (no such day)", "**Revised:** last week",
                     "**Revised:** (what changed)", "Revised: 2026-10-06 (not the field)"):
            with self.subTest(line=line), self.fresh():
                w = self._edited(f"\n{line}\n")
                self.assertEqual(self.found(w, "--base", "main"),
                                 (1, [ERROR + changed(LOG, "P-1")]))

    def test_removing_a_revised_line_is_not_a_revision(self):
        w = self.solo()
        w.edit(LOG, "Title 1\n\n**Status:** locked\n",
               "Title 1\n\n**Status:** locked\n\n**Revised:** 2026-05-01 (first change)\n")
        w.commit("revised once")
        w.edit(LOG, "\n**Revised:** 2026-05-01 (first change)\n", "")
        self.assertEqual(self.found(w, "--base", "main"), (1, [ERROR + changed(LOG, "P-1")]))

    def test_a_revised_line_inside_a_code_fence_is_an_example_not_the_field(self):
        w = self._edited("\n```\n**Revised:** 2026-10-06 (how the line looks)\n```\n")
        self.assertEqual(self.found(w, "--base", "main"), (1, [ERROR + changed(LOG, "P-1")]))

    def test_a_status_change_is_text_like_any_other(self):
        w = self.solo()
        w.edit(LOG, "Title 1\n\n**Status:** locked", "Title 1\n\n**Status:** provisional")
        self.assertEqual(self.found(w), (0, [WARN + changed(LOG, "P-1")]))
        self.assertEqual(self.found(w, "--base", "main"), (1, [ERROR + changed(LOG, "P-1")]))

    def test_a_typo_fix_and_a_title_change_need_the_line_too(self):
        for old, new in (("## P-1 - Title 1", "## P-1 - Title one"),
                         ("acting on it.\n\n**Why:** the next reader was not in the room.\n\n## P-2",
                          "acting on it.\n\n**Why:** the next reader wasn't in the room.\n\n## P-2")):
            with self.subTest(new=new), self.fresh():
                w = self.solo()
                w.edit(LOG, old, new)
                self.assertEqual(self.found(w, "--base", "main"),
                                 (1, [ERROR + changed(LOG, "P-1")]))

    def test_a_topic_line_carrying_another_field_is_text(self):
        # Only the line that is the topic and nothing else is left out of what is compared.
        w = self.solo()
        w.edit(LOG, "Title 1\n\n**Status:** locked\n",
               "Title 1\n\n**Topic:** X. **Rule:** do the opposite\n\n**Status:** locked\n")
        self.assertEqual(self.found(w), (0, [WARN + changed(LOG, "P-1")]))
        self.assertEqual(self.found(w, "--base", "main"), (1, [ERROR + changed(LOG, "P-1")]))

    def test_becoming_a_pointer_is_the_legal_way_to_replace_it(self):
        w = self.solo()
        head, _, rest = w.read(LOG).partition("\n## P-1 - Title 1\n")
        _, _, rest = rest.partition("\n## P-2")
        w.write(LOG, head + "\n## P-1 - Replaced by P-4\n\n## P-2" + rest)
        w.append(LOG, decision("P", 4))
        self.silent(w)
        self.silent(w, "--base", "main")

    def test_withdrawing_a_settled_decision_takes_two_steps(self):
        # One: change its status, with a Revised line saying so. Two, once that has landed:
        # remove it. It is no longer settled by then, so the removal is a warning.
        w = self.solo()
        w.branch("step-one")
        w.edit(LOG, "Title 1\n\n**Status:** locked\n",
               "Title 1\n\n**Status:** provisional\n\n**Revised:** 2026-10-06 (added by mistake)\n")
        self.silent(w)
        self.silent(w, "--base", "main")
        w.commit("no longer settled")
        w.switch("main")
        w.git("merge", "-q", "--ff-only", "step-one")
        w.branch("step-two")
        head, _, rest = w.read(LOG).partition("\n## P-1 - Title 1\n")
        w.write(LOG, head + "\n## P-2" + rest.partition("\n## P-2")[2])
        gone = WARN + removed(LOG, "P-1", "provisional", "## P-1 - Replaced by <id>")
        self.assertEqual(self.found(w), (0, [gone]))
        self.assertEqual(self.found(w, "--base", "main"), (0, [gone]))

    def test_both_steps_on_one_branch_are_still_a_removal_of_a_settled_decision(self):
        w = self.solo()
        w.branch("work")
        head, _, rest = w.read(LOG).partition("\n## P-1 - Title 1\n")
        w.write(LOG, head + "\n## P-2" + rest.partition("\n## P-2")[2])
        w.commit("gone")
        self.assertEqual(self.found(w, "--base", "main"),
                         (1, [ERROR + removed(LOG, "P-1", "locked", "## P-1 - Replaced by <id>")]))

    def test_an_explicit_level_error_does_not_make_the_plain_run_fail(self):
        # How strict the check is depends on how the gate is run, never on the setting; a
        # project that wants the errors on uncommitted work asks for the gate against HEAD.
        w = self._edited(extra=f'\n[checks.{CHECK}]\nlevel = "error"\n')
        code, out, _ = w.run("check")
        self.assertEqual((code, ERROR in out), (0, False), out)
        self.assertIn(WARN + changed(LOG, "P-1"), out)
        code, out, _ = w.run("check", "--base", "HEAD")
        self.assertEqual(code, 1, out)
        self.assertIn(ERROR + changed(LOG, "P-1"), out)

    def test_level_warn_demotes_the_gate_and_off_silences_it(self):
        reason = 'reason = "the team reviews every decision change by hand"\n'
        w = self._edited(extra=f'\n[checks.{CHECK}]\nlevel = "warn"\n{reason}')
        code, out, _ = w.run("check", "--base", "main")
        self.assertEqual(code, 0, out)
        self.assertIn(WARN + changed(LOG, "P-1"), out)
        w.edit(layout.CONFIG, 'level = "warn"', 'level = "off"')
        code, out, _ = w.run("check", "--base", "main")
        self.assertEqual((code, CHECK in out, "P-1 (locked) changed" in out), (0, False, False), out)


# ---------------------------------------------------------------------------- rule 3

class DecisionRemoved(Case):
    def _without(self, w: Tree, heading: str, rel: str = LOG) -> None:
        """Cut one whole entry out of a log."""
        head, found, rest = w.read(rel).partition(f"\n{heading}\n")
        assert found, heading
        nxt = re.search(r"^## ", rest, re.M)
        w.write(rel, head + ("\n" + rest[nxt.start():] if nxt else "\n"))

    def test_a_locked_decision_removed_warns_first_and_fails_the_gate(self):
        w = self.solo()
        self._without(w, "## P-1 - Title 1")
        line = removed(LOG, "P-1", "locked", "## P-1 - Replaced by <id>")
        self.assertEqual(self.found(w), (0, [WARN + line]))
        self.assertEqual(self.found(w, "--base", "main"), (1, [ERROR + line]))

    def test_a_provisional_decision_removed_is_a_warning_in_both_modes(self):
        w = self.solo()
        self._without(w, "## P-2 - Title 2")
        line = WARN + removed(LOG, "P-2", "provisional", "## P-2 - Replaced by <id>")
        self.assertEqual(self.found(w), (0, [line]))
        self.assertEqual(self.found(w, "--base", "main"), (0, [line]))

    def test_an_entry_with_no_status_says_so(self):
        w = self.solo(log=fm() + "# Decisions\n" + decision("P", 1) + decision("P", 2, None)
                      + "\n" + BLOCK)
        self._without(w, "## P-2 - Title 2")
        line = WARN + removed(LOG, "P-2", "no status", "## P-2 - Replaced by <id>")
        self.assertEqual(self.found(w, "--base", "main")[1], [line])

    def test_the_pointer_it_suggests_has_the_dash_of_the_heading_that_went(self):
        w = self.solo(log=fm() + "# Decisions\n" + decision("P", 1, sep=" — ")
                      + decision("P", 2, sep=" – ") + decision("P", 3) + "\n" + BLOCK)
        self._without(w, "## P-1 — Title 1")
        self._without(w, "## P-2 – Title 2")
        self.assertEqual(self.found(w, "--base", "main"), (1, [
            ERROR + removed(LOG, "P-1", "locked", "## P-1 — Replaced by <id>"),
            ERROR + removed(LOG, "P-2", "locked", "## P-2 – Replaced by <id>")]))

    def test_a_prefix_written_in_another_case_is_the_same_id(self):
        w = self.solo()
        w.edit(LOG, "## P-1 - Title 1", "## p-1 - Title 1")
        w.edit(TRAPS, "## T-1 - Trap 1", "## t-1 - Trap 1")
        # The grammar itself wants capitals: to every other check this heading is malformed.
        self.assertEqual([e.ident for e in w.context().grammar.parse(w.read(LOG)).entries],
                         ["P-2", "P-3"])
        self.silent(w)
        self.silent(w, "--base", "main")
        # The same id, so a change to it is still a change to a settled decision.
        w.edit(LOG, "## p-1 - Title 1", "## p-1 - Another title")
        self.assertEqual(self.found(w, "--base", "main")[1], [ERROR + changed(LOG, "p-1")])

    def test_deleting_the_whole_log_removes_every_decision_in_it(self):
        w = self.solo()
        (w.root / LOG).unlink()
        code, lines = self.found(w, "--base", "main")
        self.assertEqual(lines, [
            ERROR + removed(LOG, "P-1", "locked", "## P-1 - Replaced by <id>"),
            ERROR + removed(LOG, "P-3", "locked", "## P-3 - Replaced by <id>"),
            WARN + removed(LOG, "P-2", "provisional", "## P-2 - Replaced by <id>")])

    def test_an_id_out_of_sight_in_an_unreadable_log_is_not_called_removed(self):
        # B-200 moved from beta's log, in beta's own repository, to the workspace's log, which
        # then cannot be read: nothing can say it is gone, and the unreadable file is the other
        # checks' to report.
        w = self.team(nested=True)
        self._without(w, "## B-200 - Title 200", "beta/DECISIONS.md")
        w.write("governance/DECISIONS.md", b"# Decisions\n\xff\n")
        self.silent(w)
        # Readable again, and B-200 is in neither log: now it is known to be gone.
        w.git("checkout", "-q", "--", "governance/DECISIONS.md")
        self.assertEqual(self.found(w), (0, [WARN + removed(
            "beta/DECISIONS.md", "B-200", "locked", "## B-200 - Replaced by <id>")]))


# ---------------------------------------------------------------------------- rule 4

class IdTakenOnTheBaseBranch(Case):
    """`work` and `main` each add the same id after they part."""

    def _fork(self, w: Tree, ours: str, theirs: str, rel: str = LOG) -> None:
        w.branch("work")
        w.append(rel, ours)
        w.commit("ours")
        w.switch("main")
        w.append(rel, theirs)
        w.commit("theirs")
        w.switch("work")

    def test_the_same_id_with_different_text_fails_on_the_branch(self):
        w = self.solo()
        self._fork(w, decision("P", 4, title="Ours"), decision("P", 4, title="Their title"))
        self.assertEqual(self.found(w, "--base", "main"),
                         (1, [ERROR + taken(LOG, "P-4", "main", "Their title")]))

    def test_the_other_sides_title_cannot_steer_the_terminal(self):
        # The title quoted is the other branch's text: an escape sequence, a bell or a delete
        # in it is printed as `\xNN`, in a decision's finding and in a trap's.
        title = "Red \x1b[31malert\x7f, a\ttab and a bell\x07 too"
        shown = "Red \\x1b[31malert\\x7f, a\\x09tab and a bell\\x07 too"
        w = self.solo()
        w.branch("work")
        w.append(LOG, decision("P", 4, title="Ours"))
        w.append(TRAPS, trap(2, title="Ours"))
        w.commit("ours")
        w.switch("main")
        w.append(LOG, decision("P", 4, title=title))
        w.append(TRAPS, trap(2, title=title))
        w.commit("theirs")
        w.switch("work")
        code, lines = self.found(w, "--base", "main")
        self.assertEqual((code, lines), (1, [ERROR + taken(LOG, "P-4", "main", shown),
                                             ERROR + taken(TRAPS, "T-2", "main", shown)]))
        self.assertEqual([c for line in lines for c in line if ord(c) < 0x20 or ord(c) == 0x7f], [])

    def test_without_base_nothing_is_said_about_it(self):
        w = self.solo()
        self._fork(w, decision("P", 4, title="Ours"), decision("P", 4, title="Their title"))
        self.silent(w)

    def test_the_same_id_with_the_same_text_is_a_cherry_pick(self):
        w = self.solo()
        # The same entry on both sides, wrapped differently and under another dash.
        self._fork(w, decision("P", 4, "provisional"),
                   decision("P", 4, "provisional", sep=" — ").replace("down before", "down\nbefore"))
        self.silent(w, "--base", "main")

    def test_after_merging_the_base_into_the_branch_it_is_silent(self):
        w = self.solo()
        self._fork(w, decision("P", 4, title="Ours"), decision("P", 4, title="Their title"))
        self.assertEqual(self.found(w, "--base", "main")[0], 1)
        w.git("merge", "-q", "main", check=False)        # conflicts: both wrote P-4
        w.write(LOG, w.git("show", f"main:{LOG}") + decision("P", 5, title="Ours"))
        w.commit("merge main, ours renumbered")
        self.assertEqual(w.git("merge-base", "main", "HEAD").strip(),
                         w.git("rev-parse", "main").strip())
        self.silent(w, "--base", "main")

    def test_an_entry_the_base_took_by_a_squash_merge_and_this_branch_then_retitled(self):
        # Nothing in git says the entry on the base branch is this branch's own, so the message
        # gives both ways out. Merging the base in is the one for this case: the retitle is
        # then an ordinary edit to an entry that was at the base.
        w = self.solo()
        w.branch("work")
        w.append(LOG, decision("P", 4, "provisional", title="As first written"))
        w.commit("ours")
        w.switch("main")
        w.git("merge", "-q", "--squash", "work")
        w.git("commit", "-qm", "work, squashed")
        w.switch("work")
        self.silent(w, "--base", "main")                   # the same text on both sides
        w.edit(LOG, "## P-4 - As first written", "## P-4 - As retitled")
        w.commit("retitled")
        self.assertEqual(self.found(w, "--base", "main"),
                         (1, [ERROR + taken(LOG, "P-4", "main", "As first written")]))
        ours = w.read(LOG)
        w.git("merge", "-q", "--no-commit", "main", check=False)
        w.write(LOG, ours)
        w.commit("merge main")
        self.assertEqual(w.git("merge-base", "main", "HEAD").strip(),
                         w.git("rev-parse", "main").strip())
        self.assertIn("## P-4 - As retitled", w.read(LOG))
        self.silent(w, "--base", "main")

    def test_the_rival_in_the_same_file_is_the_one_an_entry_is_held_against(self):
        # main has T-2 twice: in the file this branch wrote its own T-2 in, saying something
        # else, and in another file with this branch's very words. The one in the same file is
        # the collision, and its title is the one quoted.
        w = self.solo()
        w.branch("work")
        w.append(TRAPS, trap(2, "Ours"))
        w.commit("ours")
        w.switch("main")
        w.append(TRAPS, trap(2, "Their trap"))
        w.write("docs/working/traps-build.md", fm("working", status="active") + "# Build traps\n"
                + trap(2, "Ours"))
        w.commit("theirs")
        w.switch("work")
        self.assertEqual(self.found(w, "--base", "main")[1],
                         [ERROR + taken(TRAPS, "T-2", "main", "Their trap")])

    def test_an_id_in_a_repository_that_was_not_compared_is_not_called_this_branchs(self):
        # beta is its own repository and has no `main`: nothing says what its branch added, so
        # an id in its log that the root's `main` also took is not held against it.
        w = self.team(nested=True)
        w.branch("work")
        w.append("beta/DECISIONS.md", decision("W", 9, title="Beta's"))
        w.switch("main")
        w.append("governance/DECISIONS.md", decision("W", 9, title="The workspace's"))
        w.commit("theirs")
        w.switch("work")
        self.assertEqual(self.found(w, "--base", "main")[1], [WARN + no_compare(
            "main", "main names no commit in this repository", repo="beta")])

    def test_what_landed_on_the_base_after_the_branch_began_is_not_the_branchs(self):
        # main rewords a locked decision and removes another after `work` left it: compared
        # with main's tip that would read as this branch undoing both.
        w = self.solo()
        w.branch("work")
        w.append(LOG, decision("P", 4))
        w.commit("ours")
        w.switch("main")
        w.edit(LOG, *SettledDecisionChangedInPlace.EDIT)
        w.edit(LOG, "## P-3 - Title 3\n\n**Status:** locked", "## P-3 - Replaced by P-1")
        w.commit("theirs")
        w.switch("work")
        self.silent(w, "--base", "main")

    def test_a_trap_id_taken_on_the_base_branch_fails_too(self):
        w = self.solo()
        self._fork(w, trap(2, "Ours"), trap(2, "Their trap"), rel=TRAPS)
        self.assertEqual(self.found(w, "--base", "main"),
                         (1, [ERROR + taken(TRAPS, "T-2", "main", "Their trap")]))
        self.silent(w)

    def test_the_same_trap_on_both_sides_is_silent(self):
        w = self.solo()
        self._fork(w, trap(2), trap(2), rel=TRAPS)
        self.silent(w, "--base", "main")

    def test_a_trap_added_in_a_second_trap_file_still_collides(self):
        # Trap ids are one namespace across a project's trap files.
        w = self.solo()
        w.branch("work")
        w.write("docs/working/traps-build.md", fm("working", status="active") + "# Build traps\n"
                + trap(2, "Ours"))
        w.git("add", "-A")
        w.git("commit", "-qm", "ours")
        w.switch("main")
        w.append(TRAPS, trap(2, "Their trap"))
        w.commit("theirs")
        w.switch("work")
        self.assertEqual(self.found(w, "--base", "main")[1],
                         [ERROR + taken("docs/working/traps-build.md", "T-2", "main", "Their trap")])

    def test_the_same_trap_id_in_two_projects_is_two_traps(self):
        w = self.team()
        w.branch("work")
        w.append("projects/alpha/working-files/traps.md", trap(2, "Alpha's"))
        w.commit("ours")
        w.switch("main")
        w.append("projects/gamma/working-files/traps.md", trap(2, "Gamma's"))
        w.commit("theirs")
        w.switch("work")
        self.silent(w, "--base", "main")


# ---------------------------------------------------------------------------- cannot compare

class CannotCompare(Case):
    """Each reason a comparison cannot be made: silent on an ordinary run, where a project with
    no history is not nagged; under `--base` the gate fails closed."""

    def _edited(self, **kw) -> Solo:
        w = self.solo(**kw)
        w.edit(LOG, *SettledDecisionChangedInPlace.EDIT)
        return w

    def test_no_git_on_path(self):
        w = self._edited()
        empty = self.tmp / "nothing-on-path"
        empty.mkdir()
        env = {"PATH": str(empty)}
        self.silent(w, env=env)
        self.assertEqual(self.found(w, "--base", "main", env=env),
                         (1, [ERROR + no_compare("main", "git is not available")]))

    def test_not_a_repository(self):
        w = self._edited(git=None)
        self.silent(w)
        self.assertEqual(self.found(w, "--base", "main"),
                         (1, [ERROR + no_compare("main", "not a git repository")]))

    def test_no_commit_yet(self):
        w = self._edited(git="init")
        self.silent(w)
        self.assertEqual(self.found(w, "--base", "main"),
                         (1, [ERROR + no_compare("main", "the repository has no commit yet")]))

    def test_an_unknown_ref(self):
        w = self._edited()
        self.assertEqual(self.found(w, "--base", "origin/nope"), (1, [ERROR + no_compare(
            "origin/nope", "origin/nope names no commit in this repository")]))

    def test_a_ref_that_reads_as_an_option_is_an_unknown_ref(self):
        w = self._edited()
        self.assertEqual(self.found(w, "--base=--all"), (1, [ERROR + no_compare(
            "--all", "--all names no commit in this repository")]))

    def test_an_empty_base_is_a_usage_error(self):
        w = self._edited()
        code, out, err = w.run("check", "--base", "")
        self.assertEqual((code, out, err), (2, "", "error: --base needs a ref, e.g. --base origin/main\n"))

    def test_unrelated_histories(self):
        w = self._edited()
        w.git("stash", "-q")
        w.git("checkout", "-q", "--orphan", "island")
        w.git("commit", "-qm", "an island")
        w.switch("main")
        w.git("stash", "pop", "-q")
        self.assertEqual(self.found(w, "--base", "island"),
                         (1, [ERROR + no_compare("island", "HEAD and island share no history")]))

    def test_a_shallow_clone(self):
        src = self.solo()
        src.branch("work")
        src.append(LOG, decision("P", 4))
        src.commit("ours")
        src.switch("main")
        src.append(LOG, decision("P", 5))
        src.commit("theirs")
        clone = self.tmp / "clone"
        src.git("clone", "-q", "--depth", "1", "--branch", "work", src.root.as_uri(), str(clone),
                at=self.tmp)
        w = Tree(clone, src.home)
        self.assertEqual(w.git("rev-parse", "--is-shallow-repository").strip(), "true")
        w.edit(LOG, *SettledDecisionChangedInPlace.EDIT)
        # A depth-1 clone holds one branch: the ref a pull request names is not there at all.
        fix = "fetch the full history (fetch-depth: 0)"
        self.assertEqual(self.found(w, "--base", "origin/main"), (1, [ERROR + no_compare(
            "origin/main", f"origin/main names no commit in this shallow clone: {fix}")]))
        # Fetched just as shallowly, it is there, with nothing in common to compare from.
        w.git("fetch", "-q", "--depth", "1", "origin", "main:refs/remotes/origin/main")
        self.assertEqual(self.found(w, "--base", "origin/main"), (1, [ERROR + no_compare(
            "origin/main", f"HEAD and origin/main share no commit in this shallow clone: {fix}")]))
        # An ordinary run compares with HEAD, which a shallow clone does have.
        self.assertEqual(self.found(w), (0, [WARN + changed(LOG, "P-1")]))

    def test_a_base_log_that_is_not_utf8(self):
        good = fm() + "# Decisions\n" + decision("P", 1) + "\n" + BLOCK
        w = self.solo(git="init", log=good.replace("room", "r\xf6\xf6m").encode("latin-1"))
        w.commit("start", index=False)
        head = w.git("rev-parse", "HEAD").strip()
        w.write(LOG, good.replace("acting on it", "acting on it, always"))
        w.run("index")
        self.silent(w)
        code, lines = self.found(w, "--base", "main")
        self.assertEqual(code, 1)
        self.assertEqual(len(lines), 1, lines)
        self.assertRegex(lines[0], re.escape(ERROR + no_compare(
            "main", f"{LOG} at {head[:12]} is not valid UTF-8 (byte ")) + r"\d+\)$")

    def test_a_log_on_disk_that_is_not_utf8(self):
        w = self.solo()
        w.write(LOG, b"# Decisions\n\xff\n")
        self.silent(w)
        code, lines = self.found(w, "--base", "main")
        self.assertEqual(lines, [ERROR + no_compare("main", f"{LOG} is not valid UTF-8 (byte 12)")])

    def test_a_log_that_is_new_since_the_base_is_not_a_failure(self):
        w = self.solo(git="init")
        w.git("add", "-A")
        w.git("rm", "-q", "--cached", LOG)
        w.git("commit", "-qm", "before there was a log")
        self.silent(w)
        code, out, _ = self.silent(w, "--base", "main")
        self.assertNotIn("could not compare", out)
        w.git("add", "-A")
        w.git("commit", "-qm", "the log")
        self.silent(w, "--base", "main")

    def test_a_nested_project_repository_where_the_ref_means_nothing_is_a_warning(self):
        w = self.team(nested=True)
        note = WARN + no_compare("main", "main names no commit in this repository", repo="beta")
        self.assertEqual(self.found(w, "--base", "main"), (0, [note]))
        # The rest is still compared: the root's own logs against the root's `main`.
        w.edit("projects/alpha/DECISIONS.md", "## A-100 - Title 100", "## A-100 - Another title")
        self.assertEqual(self.found(w, "--base", "main"),
                         (1, [ERROR + changed("projects/alpha/DECISIONS.md", "A-100"), note]))
        # And a ref the nested repository does have is compared there.
        w.edit("beta/DECISIONS.md", "## B-200 - Title 200", "## B-200 - Another title")
        self.assertEqual(self.found(w, "--base", "trunk"), (1, [
            ERROR + no_compare("trunk", "trunk names no commit in this repository"),
            ERROR + changed("beta/DECISIONS.md", "B-200")]))

    def test_a_nested_project_with_no_repository_of_its_own_ref_is_only_its_own_business(self):
        # `check --project alpha`: beta's repository is not asked about at all.
        w = self.team(nested=True)
        self.silent(w, "--project", "alpha", "--base", "main")

    def test_a_project_asked_for_alone_whose_repository_has_no_such_ref_fails_the_gate(self):
        # In a full run it is a warning and the rest is compared (above). Asked about beta and
        # nothing else, there is no rest: the run compared nothing.
        w = self.team(nested=True)
        self.silent(w, "--project", "beta")
        self.assertEqual(self.found(w, "--project", "beta", "--base", "main"), (1, [
            ERROR + no_compare("main", "main names no commit in this repository", repo="beta")]))
        self.silent(w, "--project", "beta", "--base", "trunk")

    def test_workspace_only_is_not_told_about_a_project_repository_it_does_not_read(self):
        w = self.team(nested=True)
        self.silent(w, "--workspace-only", "--base", "main")


if __name__ == "__main__":
    unittest.main()
