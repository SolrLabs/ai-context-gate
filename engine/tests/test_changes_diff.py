"""`govern diff`: how a decision change is worded, ordered and compared, and what the baseline is."""
from __future__ import annotations

import json
import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from changes_support import *  # noqa: E402,F401,F403

# ---------------------------------------------------------------------------- decisions

class DecisionWords(Diff):
    """One line per changed decision, with the most specific first-column word that applies."""

    def test_nothing_changed(self):
        w = self.solo()
        self.assertEqual(self.text(w), f"Governance changes since HEAD\n\n{NOTHING}\n\n"
                                       f"Compared\n- . at {self.head(w)}\n")

    def test_added(self):
        w = self.solo()
        w.append(LOG, decision("P", 4, "provisional", title="A new rule"))
        w.append(LOG, decision("P", 5, None, title="No status yet"))
        self.assertEqual(self.changed(w), {"Decisions": [
            f"- added      {LOG} P-4 - A new rule (provisional)",
            f"- added      {LOG} P-5 - No status yet (no status)"]})

    def test_removed(self):
        w = self.solo(log=fm() + "# Decisions\n" + decision("P", 1) + decision("P", 2, "deferred")
                      + decision("P", 3, None) + "\n" + BLOCK)
        cut(w, LOG, "P-2")
        cut(w, LOG, "P-3")
        self.assertEqual(self.changed(w), {"Decisions": [
            f"- removed    {LOG} P-2 (deferred)", f"- removed    {LOG} P-3 (no status)"]})

    def test_superseded(self):
        w = self.solo()
        cut(w, LOG, "P-1")
        w.append(LOG, "\n## P-1 - Replaced by P-4\n" + decision("P", 4, title="The new rule"))
        self.assertEqual(self.changed(w), {"Decisions": [
            f"- superseded {LOG} P-1 -> P-4",
            f"- added      {LOG} P-4 - The new rule (locked)"]})

    def test_moved(self):
        w = self.team()
        w.append("governance/DECISIONS.md", cut(w, "projects/alpha/DECISIONS.md", "A-100"))
        self.assertEqual(self.changed(w), {"Decisions": [
            "- moved      governance/DECISIONS.md A-100 (locked), from projects/alpha/DECISIONS.md"]})
        self.assertEqual(self.data(w)["decisions"], [
            row("moved", "governance/DECISIONS.md", "A-100", "Title 100", "locked",
                was_log="projects/alpha/DECISIONS.md")])

    def test_a_pointer_aimed_at_another_decision_is_superseded(self):
        w = self.solo(log=fm() + "# Decisions\n\n## P-1 - Replaced by P-2\n" + decision("P", 2)
                      + decision("P", 3) + "\n" + BLOCK)
        w.edit(LOG, "Replaced by P-2", "Replaced by P-02")      # the same id, written another way
        self.assertEqual(self.changed(w), {"Decisions": [f"- changed    {LOG} P-1 (no status)"]})
        w.edit(LOG, "Replaced by P-02", "Replaced by P-3")
        self.assertEqual(self.changed(w), {"Decisions": [f"- superseded {LOG} P-1 -> P-3"]})
        # As for any superseded entry, the title and the status are the base's.
        self.assertEqual(self.data(w)["decisions"], [
            row("superseded", LOG, "P-1", "Replaced by P-2", None, replaced_by="P-3")])

    def test_revised(self):
        w = self.solo()
        w.edit(LOG, "Title 1\n\n**Status:** locked\n",
               "Title 1\n\n**Status:** locked\n\n**Revised:** 2026-10-06 (shorter)\n")
        self.assertEqual(self.changed(w), {"Decisions": [
            f"- revised    {LOG} P-1 (locked), Revised 2026-10-06"]})

    def test_revised_names_the_newest_date(self):
        w = self.solo()
        w.edit(LOG, "Title 1\n\n**Status:** locked\n",
               "Title 1\n\n**Status:** locked\n\n**Revised:** 2026-03-01 (first)\n")
        w.commit("revised once")
        w.edit(LOG, "**Revised:** 2026-03-01 (first)\n",
               "**Revised:** 2026-03-01 (first)\n\n**Revised:** 2026-10-06 (second)\n")
        self.assertEqual(self.changed(w), {"Decisions": [
            f"- revised    {LOG} P-1 (locked), Revised 2026-10-06"]})

    def test_status(self):
        w = self.solo()
        w.edit(LOG, "Title 2\n\n**Status:** provisional", "Title 2\n\n**Status:** locked")
        self.assertEqual(self.changed(w), {"Decisions": [
            f"- status     {LOG} P-2 provisional -> locked"]})

    def test_status_taken_away_and_given(self):
        w = self.solo(log=fm() + "# Decisions\n" + decision("P", 1, "provisional")
                      + decision("P", 2, None, body="**Rule:** a rule.\n") + "\n" + BLOCK)
        w.edit(LOG, "Title 1\n\n**Status:** provisional\n\n", "Title 1\n\n")
        w.edit(LOG, "Title 2\n\n", "Title 2\n\n**Status:** locked\n\n")
        self.assertEqual(self.changed(w), {"Decisions": [
            f"- status     {LOG} P-1 provisional -> no status",
            f"- status     {LOG} P-2 no status -> locked"]})

    def test_changed(self):
        w = self.solo()
        w.edit(LOG, "## P-2 - Title 2", "## P-2 - Another title")
        self.assertEqual(self.changed(w), {"Decisions": [
            f"- changed    {LOG} P-2 (provisional)"]})

    def test_a_locked_decision_reworded_with_no_revised_line_is_changed(self):
        # The gate objects to it; the report only says what happened.
        w = self.solo()
        w.edit(LOG, "## P-1 - Title 1", "## P-1 - Title one")
        self.assertEqual(self.changed(w), {"Decisions": [f"- changed    {LOG} P-1 (locked)"]})


class Precedence(Diff):
    """When two words apply, the earlier of: removed, added, superseded, moved, revised,
    status, changed."""

    def test_a_pointer_left_in_another_log_is_superseded_not_moved(self):
        w = self.team()
        cut(w, "projects/alpha/DECISIONS.md", "A-100")
        w.append("governance/DECISIONS.md", "\n## A-100 - Replaced by W-3\n" + decision("W", 3))
        self.assertEqual(self.changed(w), {"Decisions": [
            "- superseded governance/DECISIONS.md A-100 -> W-3, from projects/alpha/DECISIONS.md",
            "- added      governance/DECISIONS.md W-3 - Title 3 (locked)"]})

    def test_a_moved_decision_whose_text_changed_is_the_text_change_and_keeps_the_move(self):
        w = self.team()
        moved = cut(w, "projects/alpha/DECISIONS.md", "A-101")
        w.append("governance/DECISIONS.md", moved.replace("Title 101", "Renamed"))
        self.assertEqual(self.changed(w), {"Decisions": [
            "- changed    governance/DECISIONS.md A-101 (provisional), "
            "from projects/alpha/DECISIONS.md"]})
        self.assertEqual(self.data(w)["decisions"], [
            row("changed", "governance/DECISIONS.md", "A-101", "Renamed", "provisional",
                was_log="projects/alpha/DECISIONS.md")])

    def test_every_word_of_an_entry_that_also_moved_ends_with_where_it_was(self):
        w, gov, alpha = self.team(), "governance/DECISIONS.md", "projects/alpha/DECISIONS.md"
        w.append(gov, cut(w, alpha, "A-100").replace(
            "**Status:** locked\n", "**Status:** locked\n\n**Revised:** 2026-10-06 (shorter)\n"))
        w.append(gov, cut(w, alpha, "A-101").replace("**Status:** provisional", "**Status:** locked"))
        self.assertEqual(self.changed(w), {"Decisions": [
            f"- revised    {gov} A-100 (locked), Revised 2026-10-06, from {alpha}",
            f"- status     {gov} A-101 provisional -> locked, from {alpha}"]})
        self.assertEqual(self.data(w)["decisions"], [
            row("revised", gov, "A-100", "Title 100", "locked", was_log=alpha,
                revised="2026-10-06"),
            row("status", gov, "A-101", "Title 101", "locked", was_log=alpha,
                was_status="provisional")])

    def test_was_status_is_the_base_status_whenever_it_is_not_the_status_now(self):
        w = self.solo()
        w.edit(LOG, "## P-2 - Title 2\n\n**Status:** provisional",
               "## P-2 - Title two\n\n**Status:** deferred")
        w.edit(LOG, "Title 1\n\n**Status:** locked\n",
               "Title 1\n\n**Status:** deferred\n\n**Revised:** 2026-10-06 (withdrawn)\n")
        cut(w, LOG, "P-3")
        self.assertEqual(self.data(w)["decisions"], [
            row("revised", LOG, "P-1", "Title 1", "deferred", was_status="locked",
                revised="2026-10-06"),
            row("changed", LOG, "P-2", "Title two", "deferred", was_status="provisional"),
            # A removed entry's status is the one it had: there is no other.
            row("removed", LOG, "P-3", "Title 3", "locked")])

    def test_a_revised_line_and_a_new_status_is_revised(self):
        w = self.solo()
        w.edit(LOG, "Title 1\n\n**Status:** locked\n",
               "Title 1\n\n**Status:** deferred\n\n**Revised:** 2026-10-06 (withdrawn)\n")
        self.assertEqual(self.changed(w), {"Decisions": [
            f"- revised    {LOG} P-1 (deferred), Revised 2026-10-06"]})

    def test_a_locked_status_changed_with_no_revised_line_is_status(self):
        w = self.solo()
        w.edit(LOG, "Title 1\n\n**Status:** locked", "Title 1\n\n**Status:** deferred")
        self.assertEqual(self.changed(w), {"Decisions": [
            f"- status     {LOG} P-1 locked -> deferred"]})

    def test_a_new_status_and_other_text_is_changed(self):
        w = self.solo()
        w.edit(LOG, "## P-2 - Title 2\n\n**Status:** provisional",
               "## P-2 - Title two\n\n**Status:** locked")
        self.assertEqual(self.changed(w), {"Decisions": [f"- changed    {LOG} P-2 (locked)"]})

    def test_a_revised_line_on_a_decision_that_was_not_settled_is_changed(self):
        w = self.solo()
        w.edit(LOG, "Title 2\n\n**Status:** provisional\n",
               "Title 2\n\n**Status:** provisional\n\n**Revised:** 2026-10-06 (reworded)\n")
        self.assertEqual(self.changed(w), {"Decisions": [
            f"- changed    {LOG} P-2 (provisional)"]})

    def test_a_revised_line_that_does_not_count_is_changed(self):
        # Older than nothing, but no date: what `base.dated_revision` refuses, the report
        # does not call a revision either.
        w = self.solo()
        w.edit(LOG, "Title 1\n\n**Status:** locked\n",
               "Title 1\n\n**Status:** locked\n\n**Revised:** 2026-02-30 (no such day)\n")
        self.assertEqual(self.changed(w), {"Decisions": [f"- changed    {LOG} P-1 (locked)"]})

    def test_settled_is_what_the_check_is_configured_to_call_settled(self):
        w = self.solo(extra='[checks.decision-changes]\nlocked_statuses = ["provisional"]\n')
        w.edit(LOG, "Title 2\n\n**Status:** provisional\n",
               "Title 2\n\n**Status:** provisional\n\n**Revised:** 2026-10-06 (reworded)\n")
        w.edit(LOG, "Title 1\n\n**Status:** locked\n",
               "Title 1\n\n**Status:** locked\n\n**Revised:** 2026-10-06 (reworded)\n")
        self.assertEqual(self.changed(w), {"Decisions": [
            f"- changed    {LOG} P-1 (locked)",
            f"- revised    {LOG} P-2 (provisional), Revised 2026-10-06"]})


class NotAChange(Diff):
    """What `base.compared_text` does not count, the report does not print."""

    def test_rewrapping_a_dash_a_topic_and_a_regenerated_block(self):
        w = self.solo()
        w.edit(LOG, "before acting on it.\n\n**Why:** the next reader was not in the room.\n\n## P-2",
               "before\nacting on it.\n\n**Why:** the next reader was\nnot in the room.\n\n## P-2")
        w.edit(LOG, "## P-2 - Title 2", "## P-2 — Title 2")
        w.edit(LOG, "Title 3\n\n**Status:** locked\n",
               "Title 3\n\n**Topic:** Networking\n\n**Status:** locked\n")
        w.run("index")
        self.assertEqual(self.changed(w), {"": [NOTHING]})

    def test_crlf_logs(self):
        w = self.solo()
        crlf = w.read(LOG).replace("\n", "\r\n")
        w.write(LOG, crlf)      # the same log, checked out with CRLF
        self.assertEqual(self.changed(w), {"": [NOTHING]})
        w.git("add", "-A")
        w.git("commit", "-qm", "crlf")
        w.write(LOG, crlf.replace("## P-2 - Title 2\r\n", "## P-2 - Title two\r\n")
                + decision("P", 4, "provisional").replace("\n", "\r\n"))
        self.assertNotIn("\n", w.read(LOG).replace("\r\n", ""))
        out = self.text(w)
        self.assertNotIn("\r", out)
        self.assertEqual(sections(out)["Decisions"], [
            f"- changed    {LOG} P-2 (provisional)",
            f"- added      {LOG} P-4 - Title 4 (provisional)"])

    def test_a_log_with_a_bom(self):
        w = self.solo(git="init")
        w.write(LOG, ("﻿" + w.read(LOG)).encode("utf-8"))
        w.commit("start", index=False)
        self.assertEqual(self.changed(w), {"": [NOTHING]})
        w.write(LOG, (w.read(LOG) + decision("P", 4)).encode("utf-8"))
        self.assertEqual(self.changed(w), {"Decisions": [f"- added      {LOG} P-4 - Title 4 (locked)"]})

    def test_an_example_in_a_code_fence_is_not_an_entry(self):
        w = self.solo()
        w.append(LOG, "\nAn entry looks like this:\n\n```\n## P-9 - An example\n```\n\n"
                      "<!--\n## P-8 - Commented out\n-->\n")
        self.assertEqual(self.changed(w), {"Decisions": [f"- changed    {LOG} P-3 (locked)"]})


class TheSameLog(Diff):
    """`base.compare` reads a log from the name git holds it under at the base, and an id
    whatever the case of its prefix; the report follows, and says what changed, not that every
    entry is new."""

    def test_a_renamed_log_is_compared_with_the_log_it_was(self):
        w, renamed = self.solo(), "docs/LOG.md"
        w.branch("work")
        w.git("mv", LOG, renamed)
        w.write(layout.CONFIG, w.read(layout.CONFIG).replace(LOG, renamed))
        self.assertEqual(self.changed(w), {"": [NOTHING]})
        w.edit(renamed, "## P-2 - Title 2", "## P-2 - Title two")
        w.append(renamed, decision("P", 4, "provisional"))
        want = {"Decisions": [f"- changed    {renamed} P-2 (provisional)",
                              f"- added      {renamed} P-4 - Title 4 (provisional)"]}
        self.assertEqual(self.changed(w), want)
        w.commit("renamed, and two changes")
        self.assertEqual(self.changed(w), {"": [NOTHING]})
        self.assertEqual(self.changed(w, "--base", "main"), want)

    MORE = 52

    def _rewritten(self, w: Tree, renamed: str) -> None:
        """The log renamed, the config pointed at it, and 52 decisions added in one change."""
        w.git("mv", LOG, renamed)
        w.write(layout.CONFIG, w.read(layout.CONFIG).replace(LOG, renamed))
        w.append(renamed, "".join(decision("P", n, "provisional")
                                  for n in range(4, 4 + self.MORE)))

    def test_a_log_renamed_and_rewritten_in_one_commit_is_compared_with_the_log_it_was(self):
        # So much changed that git reads no rename: the log is found by the ids it holds, and
        # every entry it kept says where it was.
        w, renamed = self.solo(), "docs/LOG.md"
        w.branch("work")
        self._rewritten(w, renamed)
        w.edit(renamed, "## P-1 - Title 1", "## P-1 - Another title")
        want = {"Decisions": [
            f"- changed    {renamed} P-1 (locked), from {LOG}",
            f"- moved      {renamed} P-2 (provisional), from {LOG}",
            f"- moved      {renamed} P-3 (locked), from {LOG}",
            *(f"- added      {renamed} P-{n} - Title {n} (provisional)"
              for n in range(4, 4 + self.MORE))]}
        self.assertEqual(self.changed(w), want)
        w.commit("renamed, reworded, and 52 more")
        self.assertEqual(base.since(w.root, base.resolve(w.root, "main"))[0], {})
        self.assertEqual(self.changed(w), {"": [NOTHING]})
        self.assertEqual(self.changed(w, "--base", "main"), want)
        rows = self.data(w, "--base", "main")["decisions"]
        self.assertEqual(rows[0], row("changed", renamed, "P-1", "Another title", "locked",
                                      was_log=LOG))

    def test_added_is_never_said_of_an_id_that_was_in_a_log_at_the_base(self):
        # Under another name, and under the same name in another case.
        for renamed in ("docs/LOG.md", "docs/decisions.md"):
            with self.subTest(renamed=renamed), self.fresh():
                w = self.solo()
                w.branch("work")
                self._rewritten(w, renamed)
                for commit in (False, True):
                    if commit:
                        w.commit("renamed, and 52 more")
                    held = set(re.findall(r"^## (P-\d+) ", w.git("show", f"main:{LOG}"), re.M))
                    self.assertEqual(held, {"P-1", "P-2", "P-3"})
                    flags = ("--base", "main")
                    rows = self.data(w, *flags)["decisions"]
                    added = [r["id"] for r in rows if r["change"] == "added"]
                    self.assertEqual(added, [f"P-{n}" for n in range(4, 4 + self.MORE)], commit)
                    self.assertEqual(held & set(added), set())
                    self.assertNotIn("removed", {r["change"] for r in rows})
                    lines = self.changed(w, *flags)["Decisions"]
                    self.assertEqual([line.split()[3] for line in lines
                                      if line.startswith("- added ")], added)

    def test_asking_git_what_was_renamed_writes_nothing(self):
        # A file rewritten with what it already held is one git would refresh in its index,
        # given the chance: a report, like a check, leaves the repository as it found it.
        w = self.solo(git="init")
        w.git("add", "-A")
        w.git("rm", "-q", "--cached", LOG)
        w.git("commit", "-qm", "before there was a log")      # so the log is new, or renamed
        os.utime(w.write(TRAPS, w.read(TRAPS)), ns=(10 ** 18, 10 ** 18))
        before = tree_state(w.root, w.home)
        for argv in (("diff",), ("check",), ("check", "--base", "main")):
            w.run(*argv)
            self.assertEqual(tree_state(w.root, w.home), before, argv)

    def test_a_log_git_tracks_under_a_name_in_another_case_is_the_same_log(self):
        w, lower = self.solo(), "docs/decisions.md"
        w.write(layout.CONFIG, w.read(layout.CONFIG).replace(LOG, lower))
        os.rename(w.root / LOG, w.root / lower)      # git still tracks docs/DECISIONS.md
        self.assertEqual(self.changed(w), {"": [NOTHING]})
        w.append(lower, decision("P", 4))
        self.assertEqual(self.changed(w), {"Decisions": [
            f"- added      {lower} P-4 - Title 4 (locked)"]})

    def test_a_prefix_in_another_case_is_the_same_entry(self):
        w = self.solo()
        w.edit(LOG, "## P-1 - Title 1", "## p-1 - Title 1")
        w.edit(TRAPS, "## T-1 - Trap 1", "## t-1 - Trap 1")
        self.assertEqual(self.changed(w), {"": [NOTHING]})


class TwoReadings(Diff):
    """An id that pairs more than one way (`base.match` leaves it out) is still reported, and
    nothing is guessed: each copy now that has no pair is `changed`, each copy at the base
    that has none is `removed`."""

    ALPHA, GAMMA, GOV = ("projects/alpha/DECISIONS.md", "projects/gamma/DECISIONS.md",
                         "governance/DECISIONS.md")

    def _twice(self) -> Tree:
        """A-150 is in alpha's log and in gamma's, saying different things."""
        w = self.team()
        w.append(self.ALPHA, decision("A", 150, title="Alpha's"))
        w.append(self.GAMMA, decision("A", 150, "provisional", title="Gamma's"))
        w.commit("one id, two logs")
        return w

    def test_a_decision_copied_into_another_log_is_a_line(self):
        w = self.team()
        w.append(self.GOV, cut(w, self.ALPHA, "A-100", keep=True))
        self.assertEqual(self.changed(w), {"Decisions": [f"- changed    {self.GOV} A-100 (locked)"]})
        self.assertEqual(self.data(w)["decisions"],
                         [row("changed", self.GOV, "A-100", "Title 100", "locked")])
        # The copy is the workspace's line, and no line of the project it was copied from.
        self.assertEqual(self.changed(w, "--project", "alpha"), {"": [NOTHING]})

    def test_two_copies_at_the_base_and_one_now_in_a_third_log_is_three_lines(self):
        w = self._twice()
        cut(w, self.ALPHA, "A-150")
        cut(w, self.GAMMA, "A-150")
        w.append(self.GOV, decision("A", 150, title="A third thing"))
        self.assertEqual(self.changed(w), {"Decisions": [
            f"- changed    {self.GOV} A-150 (locked)",
            f"- removed    {self.ALPHA} A-150 (locked)",
            f"- removed    {self.GAMMA} A-150 (provisional)"]})
        self.assertEqual(self.data(w)["decisions"], [
            row("changed", self.GOV, "A-150", "A third thing", "locked"),
            row("removed", self.ALPHA, "A-150", "Alpha's", "locked"),
            row("removed", self.GAMMA, "A-150", "Gamma's", "provisional")])
        self.assertEqual(self.changed(w, "--project", "gamma"), {"Decisions": [
            f"- removed    {self.GAMMA} A-150 (provisional)"]})
        # `base.match` still unpacks as the three lists it always gave, none of which holds
        # the id; the copies are beside them.
        cmp = base.compare(w.context(), traps=False)
        pairs = base.match(cmp.side("base"), cmp.side("current"))
        matched, gone, new = pairs
        self.assertEqual(([was.entry.ident for was, _ in matched].count("A-150"), gone, new),
                         (0, [], []))
        self.assertEqual(([item.log for item in pairs.unpaired_base],
                          [item.log for item in pairs.unpaired_current]),
                         ([self.ALPHA, self.GAMMA], [self.GOV]))

    def test_one_of_two_copies_deleted_is_removed(self):
        w = self._twice()
        cut(w, self.GAMMA, "A-150")
        self.assertEqual(self.changed(w), {"Decisions": [
            f"- removed    {self.GAMMA} A-150 (provisional)"]})

    def test_one_copy_at_the_base_and_two_now_elsewhere_is_three_lines(self):
        w = self.team()
        entry = cut(w, self.ALPHA, "A-100")
        w.append(self.GOV, entry)
        w.append(self.GAMMA, entry)
        self.assertEqual(self.changed(w), {"Decisions": [
            f"- changed    {self.GOV} A-100 (locked)",
            f"- removed    {self.ALPHA} A-100 (locked)",
            f"- changed    {self.GAMMA} A-100 (locked)"]})

    def test_a_copy_paired_log_by_log_is_still_compared_as_itself(self):
        w = self._twice()
        w.edit(self.GAMMA, "## A-150 - Gamma's", "## A-150 - Gamma's own")
        self.assertEqual(self.changed(w), {"Decisions": [
            f"- changed    {self.GAMMA} A-150 (provisional)"]})

    def test_with_a_log_out_of_sight_no_copy_is_called_removed_or_changed(self):
        # B-200 left beta's log for two logs of the root's repository, which could not be
        # compared: the copies there have no base to differ from, and with a file unreadable
        # the one that is gone may only be out of sight.
        w = self.team(nested=True)
        entry = cut(w, "beta/DECISIONS.md", "B-200")
        w.append(self.ALPHA, entry)
        w.append(self.GAMMA, entry)
        w.write(self.GOV, w.read(self.GOV).encode("utf-8") + b"\xff\xfe\n")
        found = self.report(w)
        compared = found.pop("Compared")
        self.assertEqual(found, {"": [NOTHING]})
        self.assertTrue(compared[0].startswith(f"- .: not compared ({self.GOV} is "), compared)
        self.assertEqual(compared[1], f"- beta at {self.head(w, w.root / 'beta')}")

    def test_a_trap_in_two_of_a_projects_files_is_reported_the_same_way(self):
        w, build = self.solo(git="init"), "docs/working/traps-build.md"
        w.write(build, fm("working", status="active") + "# Build traps\n"
                + trap(1, title="The other trap 1"))
        w.commit("one trap id, two files")
        cut(w, build, "T-1")
        self.assertEqual(self.changed(w), {"Traps": [f"- removed    {build} T-1"]})


class Order(Diff):
    def test_a_moved_entry_is_listed_under_the_log_it_is_in_now(self):
        w = self.team()
        w.append("projects/gamma/DECISIONS.md", cut(w, "governance/DECISIONS.md", "W-2"))
        w.append("projects/gamma/DECISIONS.md", decision("G", 302))
        w.append("governance/DECISIONS.md", decision("W", 3))
        self.assertEqual([line.split()[1:4] for line in self.changed(w)["Decisions"]], [
            ["added", "governance/DECISIONS.md", "W-3"],
            ["added", "projects/gamma/DECISIONS.md", "G-302"],      # in one log, by prefix first
            ["moved", "projects/gamma/DECISIONS.md", "W-2"]])

    def test_lines_are_by_log_then_by_id_as_a_number(self):
        w = self.team()
        w.append("projects/gamma/DECISIONS.md", decision("G", 310) + decision("G", 302))
        w.append("projects/alpha/DECISIONS.md", decision("A", 110) + decision("A", 9))
        w.append("governance/DECISIONS.md", decision("W", 10) + decision("W", 3))
        cut(w, "governance/DECISIONS.md", "W-2")
        self.assertEqual([line.split()[2:4] for line in self.changed(w)["Decisions"]], [
            ["governance/DECISIONS.md", "W-2"], ["governance/DECISIONS.md", "W-3"],
            ["governance/DECISIONS.md", "W-10"],
            ["projects/alpha/DECISIONS.md", "A-9"], ["projects/alpha/DECISIONS.md", "A-110"],
            ["projects/gamma/DECISIONS.md", "G-302"], ["projects/gamma/DECISIONS.md", "G-310"]])

    def test_a_second_run_prints_the_same_and_writes_nothing(self):
        w = self.solo()
        w.append(LOG, decision("P", 4))
        w.write(layout.BASELINE, '{"handoff_words:solo": 1700}\n')
        before = tree_state(w.root, w.home)
        first = w.run("diff", "--base", "main")
        self.assertEqual(w.run("diff", "--base", "main"), first)
        w.run("diff", "--json")
        self.assertEqual(tree_state(w.root, w.home), before)


# ---------------------------------------------------------------------------- traps

class Traps(Diff):
    def test_added_removed_and_changed(self):
        w = self.solo(git="init")
        w.append(TRAPS, trap(2) + trap(3, body="Look both ways."))
        w.commit("start")
        w.append(TRAPS, trap(4, title="A new trap"))
        cut(w, TRAPS, "T-1")
        w.edit(TRAPS, "Look both ways.", "Look every way.")
        self.assertEqual(self.changed(w), {"Traps": [
            f"- removed    {TRAPS} T-1", f"- changed    {TRAPS} T-3",
            f"- added      {TRAPS} T-4 - A new trap"]})

    def test_a_new_trap_file_is_named(self):
        w = self.solo()
        w.write("docs/working/traps-build.md",
                fm("working", status="active") + "# Build traps\n" + trap(2))
        self.assertEqual(self.changed(w), {"Traps": [
            "- added      docs/working/traps-build.md T-2 - Trap 2"]})

    def test_a_trap_moved_to_another_of_the_projects_trap_files_is_not_a_change(self):
        w = self.solo()
        w.write("docs/working/traps-build.md",
                fm("working", status="active") + "# Build traps\n" + cut(w, TRAPS, "T-1"))
        self.assertEqual(self.changed(w), {"": [NOTHING]})

    def test_the_same_id_in_two_projects_is_two_lines(self):
        w = self.team()
        for name in ("gamma", "alpha"):
            w.append(f"projects/{name}/working-files/traps.md", trap(7, title=f"In {name}"))
        cut(w, "projects/gamma/working-files/traps.md", "T-1")
        self.assertEqual(self.changed(w), {"Traps": [
            "- added      projects/alpha/working-files/traps.md T-7 - In alpha",
            "- removed    projects/gamma/working-files/traps.md T-1",
            "- added      projects/gamma/working-files/traps.md T-7 - In gamma"]})

    def test_a_decision_shaped_entry_in_a_trap_file_is_not_a_trap(self):
        w = self.solo()
        w.append(TRAPS, "\n## Q-9 - Not a trap\n\nNor a decision of this log.\n")
        self.assertEqual(self.changed(w), {"": [NOTHING]})


# ---------------------------------------------------------------------------- baseline

class Baseline(Diff):
    START = {"working_file_words:docs/plan.md": 6100, "decision_words:solo:P-4": 900,
             "handoff_words:solo": 1720}

    def _solo(self, start: dict | str | bytes | None = None) -> Tree:
        w = self.solo(git="init")
        start = self.START if start is None else start
        w.write(layout.BASELINE, start if isinstance(start, (str, bytes))
                else json.dumps(start, indent=2) + "\n")
        w.commit("start")
        return w

    def _now(self, w: Tree, entries: dict) -> None:
        w.write(layout.BASELINE, json.dumps(entries, indent=2) + "\n")

    def test_raised_lowered_added_and_removed(self):
        w = self._solo()
        self._now(w, {"working_file_words:docs/plan.md": 6400, "decision_words:solo:P-4": 700,
                      "governed_docs:solo": 41})
        self.assertEqual(self.changed(w), {"Baseline": [
            "- lowered    decision_words:solo:P-4 900 -> 700",
            "- added      governed_docs:solo = 41",
            "- removed    handoff_words:solo (was 1720)",
            "- raised     working_file_words:docs/plan.md 6100 -> 6400"]})

    def test_the_same_numbers_written_another_way_are_not_a_change(self):
        w = self._solo()
        w.write(layout.BASELINE, json.dumps(dict(reversed(self.START.items()))))
        self.assertEqual(self.changed(w), {"": [NOTHING]})

    def test_a_project_with_no_baseline_has_no_baseline_section(self):
        w = self.solo()
        self.assertFalse((w.root / layout.BASELINE).exists())
        self.assertEqual(self.changed(w), {"": [NOTHING]})

    def test_a_baseline_that_is_new_or_deleted(self):
        w = self.solo()
        self._now(w, {"handoff_words:solo": 1720})
        self.assertEqual(self.changed(w), {"Baseline": ["- added      handoff_words:solo = 1720"]})
        w.commit("a baseline")
        (w.root / layout.BASELINE).unlink()
        self.assertEqual(self.changed(w), {"Baseline": ["- removed    handoff_words:solo (was 1720)"]})

    def test_under_base_it_is_compared_with_where_the_branch_began(self):
        w = self._solo()
        w.branch("work")
        w.switch("main")
        self._now(w, {**self.START, "handoff_words:solo": 1800})
        w.commit("theirs")
        w.switch("work")
        self._now(w, {**self.START, "handoff_words:solo": 1750})
        w.commit("ours")
        self.assertEqual(self.changed(w), {"": [NOTHING]})
        self.assertEqual(self.changed(w, "--base", "main"),
                         {"Baseline": ["- raised     handoff_words:solo 1720 -> 1750"]})

    def test_a_baseline_unreadable_now_is_one_line(self):
        for broken, why in (("{not json", "Expecting property name enclosed in double quotes"),
                            ("[1, 2]\n", "not a JSON object"),
                            (b"\xff\xfe{}", "'utf-8' codec can't decode byte 0xff")):
            with self.subTest(broken=broken), self.fresh():
                w = self._solo()
                w.write(layout.BASELINE, broken)
                w.append(LOG, decision("P", 4))
                found = self.changed(w)
                self.assertEqual(found["Decisions"], [f"- added      {LOG} P-4 - Title 4 (locked)"])
                [line] = found["Baseline"]
                self.assertTrue(line.startswith(
                    f"- unreadable {layout.BASELINE} is not a valid baseline ({why}"), line)

    def test_a_baseline_unreadable_at_the_base_is_one_line(self):
        for broken, why in (('{"a": "many"}\n', "is not a valid baseline (invalid literal"),
                            (b"\xff\xfe{}", "is not valid UTF-8 (byte 0)")):
            with self.subTest(broken=broken), self.fresh():
                w = self._solo(broken)
                self._now(w, self.START)
                [line] = self.changed(w)["Baseline"]
                self.assertTrue(line.startswith(
                    f"- unreadable {layout.BASELINE} at {self.head(w)} {why}"), line)
                [found] = self.data(w)["baseline"]
                self.assertEqual({**found, "reason": None}, {
                    "change": "unreadable", "key": layout.BASELINE, "from": None, "to": None,
                    "reason": None})
                self.assertEqual(line, f"- unreadable {layout.BASELINE} {found['reason']}")

    def test_unreadable_in_both_forms(self):
        # The text is the word, the baseline's path as a finding prints it, and the reason; the
        # JSON gives the path as `key` and the reason on its own.
        w = self._solo(b"\xff\xfe{}")
        self._now(w, self.START)
        reason = f"at {self.head(w)} is not valid UTF-8 (byte 0)"
        self.assertEqual(self.changed(w), {"Baseline": [
            f"- unreadable {layout.BASELINE} {reason}"]})
        self.assertEqual(self.data(w)["baseline"], [
            {"change": "unreadable", "key": layout.BASELINE, "from": None, "to": None,
             "reason": reason}])
        w.commit("a baseline that can be read")
        w.write(layout.BASELINE, "[1, 2]\n")
        reason = "is not a valid baseline (not a JSON object)"
        self.assertEqual(self.changed(w), {"Baseline": [
            f"- unreadable {layout.BASELINE} {reason}"]})
        self.assertEqual(self.data(w)["baseline"], [
            {"change": "unreadable", "key": layout.BASELINE, "from": None, "to": None,
             "reason": reason}])

    def test_every_other_baseline_row_has_no_reason(self):
        w = self._solo()
        self._now(w, {"working_file_words:docs/plan.md": 6400, "decision_words:solo:P-4": 700,
                      "governed_docs:solo": 41})
        self.assertEqual(self.data(w)["baseline"], [
            {"change": "lowered", "key": "decision_words:solo:P-4", "from": 900, "to": 700,
             "reason": None},
            {"change": "added", "key": "governed_docs:solo", "from": None, "to": 41,
             "reason": None},
            {"change": "removed", "key": "handoff_words:solo", "from": 1720, "to": None,
             "reason": None},
            {"change": "raised", "key": "working_file_words:docs/plan.md", "from": 6100,
             "to": 6400, "reason": None}])

    def test_a_baseline_that_is_a_directory_is_not_a_file_and_no_whole_path_is_printed(self):
        w = self._solo()
        (w.root / layout.BASELINE).unlink()
        (w.root / layout.BASELINE).mkdir()
        out = self.text(w)
        self.assertEqual(sections(out)["Baseline"],
                         [f"- unreadable {layout.BASELINE} is not a file"])
        self.assertNotIn(w.root.as_posix(), out.replace("\\", "/"))
        self.assertEqual(self.data(w)["baseline"][0]["reason"], "is not a file")

    def test_a_baseline_that_cannot_be_opened_gives_the_systems_reason_without_the_path(self):
        if os.name == "nt" or os.geteuid() == 0:
            self.skipTest("needs a file its owner cannot read: not Windows, and not root")
        w = self._solo()
        path = w.root / layout.BASELINE
        path.chmod(0)
        try:
            out, as_json = self.text(w), w.run("diff", "--json")[1]
        finally:
            path.chmod(0o644)
        self.assertEqual(sections(out)["Baseline"],
                         [f"- unreadable {layout.BASELINE} could not be read (Permission denied)"])
        self.assertNotIn(w.root.as_posix(), out + as_json)

    def test_the_configured_path_is_the_one_read(self):
        w = self.solo(git="init", workspace='baseline = "docs/sizes.json"')
        w.write("docs/sizes.json", '{"handoff_words:solo": 1720}\n')
        w.write(layout.BASELINE, '{"handoff_words:solo": 1}\n')
        w.commit("start")
        w.write("docs/sizes.json", '{"handoff_words:solo": 1500}\n')
        w.write(layout.BASELINE, '{"handoff_words:solo": 2}\n')
        self.assertEqual(self.changed(w),
                         {"Baseline": ["- lowered    handoff_words:solo 1720 -> 1500"]})

    def test_a_baseline_outside_the_repositories_compared_is_one_line(self):
        # No log or trap file is in the repository the baseline is kept in, so the comparison
        # never asked it anything: say so rather than read that as "no change".
        w = self.solo(git="init", workspace='baseline = "sizes/baseline.json"')
        w.write(".gitignore", "sizes/\n")
        w.commit("start")
        self.assertEqual(self.changed(w), {"": [NOTHING]})       # and no baseline: nothing to say
        w.write("sizes/baseline.json", '{"handoff_words:solo": 1720}\n')
        w.init(at=w.root / "sizes")
        w.commit("start", at=w.root / "sizes", index=False)
        self.assertEqual(self.report(w), {
            "Baseline": ["- unreadable sizes/baseline.json is outside the repositories compared"],
            "Compared": [f"- . at {self.head(w)}"]})
        self.assertEqual(self.data(w)["baseline"], [
            {"change": "unreadable", "key": "sizes/baseline.json", "from": None, "to": None,
             "reason": "is outside the repositories compared"}])

    def test_a_baseline_in_no_repository_at_all_is_outside_them_too(self):
        elsewhere = self.tmp / "elsewhere" / "baseline.json"
        w = self.solo(workspace=f'baseline = "{elsewhere.as_posix()}"')
        self.assertEqual(self.changed(w), {"": [NOTHING]})       # and no baseline: nothing to say
        wtext(elsewhere, '{"handoff_words:gov": 1720}\n')
        [line] = self.changed(w)["Baseline"]
        self.assertTrue(line.startswith("- unreadable "), line)
        self.assertTrue(line.endswith("/elsewhere/baseline.json is outside the repositories compared"),
                        line)

    def test_below_the_git_top_the_baseline_is_found_by_its_git_path(self):
        w = self.solo(git="init", below="outer")
        self._now(w, {"handoff_words:gov": 1720})
        w.commit("start", at=w.top)
        self._now(w, {"handoff_words:gov": 1900})
        self.assertEqual(self.report(w), {
            "Baseline": ["- raised     handoff_words:gov 1720 -> 1900"],
            "Compared": [f"- . at {self.head(w)}"]})


if __name__ == "__main__":
    unittest.main()
