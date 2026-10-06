"""`govern diff`, the report of what a branch did to governance, and `govern ci github`, the
workflow that runs the gate and the report on a pull request. Against real git history, in the
fixtures `test_base` builds: a repository in a temporary directory with a HOME of its own.

The report's text layout and its JSON keys are a contract other tools read, so one fixture's
output is kept byte for byte in `expected/`. To regenerate both files after a deliberate change:

    GOVERN_UPDATE_EXPECTED=1 python3 -m unittest discover -s engine/tests -k ExpectedOutput

and review the difference like any other change.

    python3 -m unittest discover -s engine/tests -k test_changes
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from govern import __version__, base, layout  # noqa: E402
from govern.context import REPO_ENV  # noqa: E402
from test_base import BLOCK, LOG, TRAPS, Case, Tree, decision, fm, trap, wtext  # noqa: E402

EXPECTED = Path(__file__).resolve().parent / "expected"
TEMPLATE = ENGINE / "govern" / "templates" / "ci-github.yml"
CONFIGURATION = ENGINE.parent / "docs" / "configuration.md"
NOTHING = "- nothing changed"
NOT_COMPARED = "- nothing compared"


def cut(w: Tree, rel: str, ident: str, keep: bool = False) -> str:
    """Take one entry out of a log, heading and body, and return it; with `keep`, return it
    and leave it where it is."""
    text = w.read(rel)
    m = re.search(rf"\n## {ident}\b.*?(?=\n## |\n<!-- |\Z)", text, re.S)
    assert m, f"{rel}: no entry {ident}"
    if not keep:
        w.write(rel, text[:m.start()] + text[m.end():])
    return m.group(0)


def row(change: str, log: str, ident: str, title: str, status: str | None, **more) -> dict:
    """One decision of the JSON form: every key, `null` for what does not apply."""
    return {"change": change, "log": log, "id": ident, "title": title, "status": status,
            "was_status": None, "was_log": None, "replaced_by": None, "revised": None, **more}


def sections(out: str) -> dict[str, list[str]]:
    """The report's text as `{section: its lines}`, with the heading line under `title` and a
    list that has no section name of its own (`- nothing changed`, `- nothing compared`) under
    `""`."""
    head, *blocks = out.rstrip("\n").split("\n\n")
    found = {"title": [head]}
    for block in blocks:
        first, *rest = block.split("\n")
        if first.startswith("- "):
            found[""] = [first, *rest]
        else:
            found[first] = rest
    return found


def tree_state(*dirs: Path) -> dict[str, tuple[int, bytes]]:
    """Every file under `dirs`, git's own included, with when it was written and what it holds."""
    return {str(p): (p.stat().st_mtime_ns, p.read_bytes())
            for d in dirs if d.exists() for p in sorted(d.rglob("*")) if p.is_file()}


class Diff(Case):
    def full(self, w: Tree, at: Path | None = None, ref: str = "HEAD") -> str:
        """A commit's whole id, as the JSON gives it."""
        return w.git("rev-parse", ref, at=at).strip()

    def head(self, w: Tree, at: Path | None = None, ref: str = "HEAD") -> str:
        """A commit's first 12 characters, as the text names it."""
        return self.full(w, at, ref)[:12]

    def text(self, w: Tree, *flags: str, env: dict | None = None) -> str:
        """The report: always exit 0, and nothing on stderr."""
        code, out, err = w.run("diff", *flags, env=env)
        self.assertEqual((code, err), (0, ""), out)
        self.assertTrue(out.isascii(), out)
        return out

    def report(self, w: Tree, *flags: str, env: dict | None = None) -> dict[str, list[str]]:
        """The report by section, without its heading line."""
        found = sections(self.text(w, *flags, env=env))
        del found["title"]
        return found

    def data(self, w: Tree, *flags: str) -> dict:
        code, out, err = w.run("diff", "--json", *flags)
        self.assertEqual((code, err), (0, ""), out)
        return json.loads(out)

    def changed(self, w: Tree, *flags: str) -> dict[str, list[str]]:
        """The report without "Compared", which every report ends with."""
        found = self.report(w, *flags)
        self.assertIn("Compared", found)
        del found["Compared"]
        return found


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


# ---------------------------------------------------------------------------- compared

class Compared(Diff):
    """Each repository and the commit it was compared with, or why it was not. The report
    never fails: what could be compared is still reported."""

    def test_a_nested_repository_is_compared_with_its_own_head(self):
        w = self.team(nested=True)
        w.append("beta/DECISIONS.md", decision("B", 202))
        self.assertEqual(self.report(w), {
            "Decisions": ["- added      beta/DECISIONS.md B-202 - Title 202 (locked)"],
            "Compared": [f"- . at {self.head(w)}", f"- beta at {self.head(w, w.root / 'beta')}"]})

    def test_a_nested_repository_that_could_not_be_compared_is_named_and_the_rest_reported(self):
        w = self.team(nested=True)       # beta's branch is `trunk`: it has no `main`
        w.append("governance/DECISIONS.md", decision("W", 3))
        w.append("beta/DECISIONS.md", decision("B", 202))
        w.append("beta/working-files/traps.md", trap(2))
        self.assertEqual(self.report(w, "--base", "main"), {
            "Decisions": ["- added      governance/DECISIONS.md W-3 - Title 3 (locked)"],
            "Compared": [f"- . at {self.head(w)}",
                         "- beta: not compared (main names no commit in this repository)"]})
        self.assertEqual(self.data(w, "--base", "main")["compared"], [
            {"repo": ".", "commit": self.full(w), "reason": None},
            {"repo": "beta", "commit": None,
             "reason": "main names no commit in this repository"}])

    def test_no_git_at_all(self):
        w = self.solo()
        w.append(LOG, decision("P", 4))
        empty = self.tmp / "nothing-on-path"
        empty.mkdir()
        for flags in ((), ("--base", "main")):
            self.assertEqual(self.report(w, *flags, env={"PATH": str(empty)}), {
                "": [NOT_COMPARED], "Compared": ["- .: not compared (git is not available)"]})

    def test_not_a_repository_and_no_commit_yet(self):
        for git, why in ((None, "not a git repository"),
                         ("init", "the repository has no commit yet")):
            with self.subTest(git=git), self.fresh():
                w = self.solo(git=git)
                w.append(LOG, decision("P", 4))
                w.write(layout.BASELINE, '{"handoff_words:solo": 1720}\n')
                self.assertEqual(self.report(w), {
                    "": [NOT_COMPARED], "Compared": [f"- .: not compared ({why})"]})

    def test_an_unknown_ref_and_one_that_reads_as_an_option(self):
        w = self.solo()
        for ref in ("origin/nope", "--all"):
            out = self.text(w, f"--base={ref}")
            self.assertEqual(out, f"Governance changes since {ref}\n\n{NOT_COMPARED}\n\nCompared\n"
                                  f"- .: not compared ({ref} names no commit in this repository)\n")

    def test_a_shallow_clone_says_how_to_fix_it(self):
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
        w.git("fetch", "-q", "--depth", "1", "origin", "main:refs/remotes/origin/main")
        self.assertEqual(self.report(w, "--base", "origin/main")["Compared"], [
            "- .: not compared (HEAD and origin/main share no commit in this shallow clone: "
            "fetch the full history (fetch-depth: 0))"])

    def test_a_log_on_disk_that_is_not_utf8_is_named_not_a_traceback(self):
        w = self.solo()
        w.write(LOG, b"# Decisions\n\n## P-1 - Title 1\n\n\xff\xfe\n")
        found = self.report(w)
        self.assertEqual((list(found), found[""]), (["", "Compared"], [NOT_COMPARED]))
        [line] = found["Compared"]
        self.assertTrue(line.startswith(f"- .: not compared ({LOG} is "), line)

    def test_an_id_out_of_sight_in_an_unreadable_log_is_not_called_removed(self):
        w = self.team(nested=True)
        moved = cut(w, "beta/DECISIONS.md", "B-200")
        w.write("governance/DECISIONS.md",
                (w.read("governance/DECISIONS.md") + moved).encode("utf-8") + b"\xff\xfe\n")
        found = self.report(w)
        # One repository of the two was compared, and nothing changed in it.
        self.assertEqual((list(found), found[""]), (["", "Compared"], [NOTHING]))
        self.assertTrue(found["Compared"][0].startswith(
            "- .: not compared (governance/DECISIONS.md is "), found)
        self.assertEqual(found["Compared"][1], f"- beta at {self.head(w, w.root / 'beta')}")

    def test_a_log_at_the_base_that_is_not_utf8(self):
        w = self.solo(git="init")
        good = w.read(LOG)
        w.write(LOG, b"# Decisions\n\xff\xfe\n")
        w.commit("start", index=False)
        w.write(LOG, good)
        self.assertEqual(self.report(w), {"": [NOT_COMPARED], "Compared": [
            f"- .: not compared ({LOG} at {self.head(w)} is not valid UTF-8 (byte 12))"]})
        # In JSON nothing says so but the list itself: no commit, and the reason.
        self.assertEqual(self.data(w), {
            "base": "HEAD", "baseline": [], "decisions": [], "traps": [],
            "compared": [{"repo": ".", "commit": None, "reason":
                          f"{LOG} at {self.head(w)} is not valid UTF-8 (byte 12)"}]})

    def test_a_governance_root_below_the_git_top_is_still_dot(self):
        w = self.solo(below="outer")
        w.append(LOG, decision("P", 4))
        self.assertEqual(self.report(w), {
            "Decisions": [f"- added      {LOG} P-4 - Title 4 (locked)"],
            "Compared": [f"- . at {self.head(w)}"]})


class WhichCommit(Diff):
    def test_without_base_only_uncommitted_work_is_a_change(self):
        w = self.solo()
        w.branch("work")
        w.append(LOG, decision("P", 4))
        w.commit("ours")
        w.append(LOG, decision("P", 5))
        self.assertEqual(self.report(w), {
            "Decisions": [f"- added      {LOG} P-5 - Title 5 (locked)"],
            "Compared": [f"- . at {self.head(w)}"]})

    def test_with_base_it_is_everything_since_the_branch_began_and_only_the_branchs(self):
        w = self.solo()
        start = self.head(w)
        w.branch("work")
        w.append(LOG, decision("P", 4))
        w.commit("ours")
        w.switch("main")
        w.append(LOG, decision("P", 7, title="Theirs"))
        cut(w, LOG, "P-2")
        w.commit("theirs")
        w.switch("work")
        w.append(LOG, decision("P", 5))
        self.assertNotEqual(start, self.head(w, ref="main"))
        self.assertEqual(sections(self.text(w, "--base", "main")), {
            "title": ["Governance changes since main"],
            "Decisions": [f"- added      {LOG} P-4 - Title 4 (locked)",
                          f"- added      {LOG} P-5 - Title 5 (locked)"],
            "Compared": [f"- . at {start}"]})


# ---------------------------------------------------------------------------- flags and output

class Flags(Diff):
    def _busy(self) -> Tree:
        """A workspace where the workspace and two projects each changed a decision, a trap
        and a baseline number."""
        w = self.team()
        start = {"decision_words:workspace:W-1": 900, "handoff_words:alpha": 1600,
                 "handoff_words:gamma": 1700,
                 "working_file_words:projects/alpha/working-files/plan.md": 6100,
                 "working_file_words:projects/gamma/working-files/plan.md": 6200}
        w.write(layout.BASELINE, json.dumps(start, indent=2) + "\n")
        w.commit("a baseline")
        w.write(layout.BASELINE, json.dumps({k: v + 5 for k, v in start.items()}, indent=2) + "\n")
        w.append("governance/DECISIONS.md", decision("W", 3))
        for name, prefix, lo in (("alpha", "A", 100), ("gamma", "G", 300)):
            w.append(f"projects/{name}/DECISIONS.md", decision(prefix, lo + 2))
            w.append(f"projects/{name}/working-files/traps.md", trap(2))
        return w

    def test_project_limits_the_report_to_that_project(self):
        w = self._busy()
        self.assertEqual(self.report(w, "--project", "alpha"), {
            "Decisions": ["- added      projects/alpha/DECISIONS.md A-102 - Title 102 (locked)"],
            "Traps": ["- added      projects/alpha/working-files/traps.md T-2 - Trap 2"],
            "Baseline": [
                "- raised     handoff_words:alpha 1600 -> 1605",
                "- raised     working_file_words:projects/alpha/working-files/plan.md 6100 -> 6105"],
            "Compared": [f"- . at {self.head(w)}"]})
        whole = self.report(w)
        self.assertEqual((len(whole["Decisions"]), len(whole["Traps"]), len(whole["Baseline"])),
                         (3, 2, 5))

    def test_project_names_only_the_repositories_that_project_was_read_from(self):
        w = self.team(nested=True)
        w.append("beta/DECISIONS.md", decision("B", 202))
        w.append("projects/alpha/DECISIONS.md", decision("A", 102))
        # alpha lives in the root's repository; beta in its own, and its baseline is the root's.
        self.assertEqual(self.report(w, "--project", "alpha", "--base", "main"), {
            "Decisions": ["- added      projects/alpha/DECISIONS.md A-102 - Title 102 (locked)"],
            "Compared": [f"- . at {self.head(w)}"]})
        self.assertEqual(self.report(w, "--project", "beta"), {
            "Decisions": ["- added      beta/DECISIONS.md B-202 - Title 202 (locked)"],
            "Compared": [f"- . at {self.head(w)}", f"- beta at {self.head(w, w.root / 'beta')}"]})

    def test_a_decision_moved_between_projects_is_a_line_of_both(self):
        # Of the one it came to, and of the one it left: a project's report that said nothing
        # of a decision it no longer holds would hide the one change that matters to it.
        w = self.team()
        w.append("projects/gamma/DECISIONS.md", cut(w, "projects/alpha/DECISIONS.md", "A-100"))
        line = "- moved      projects/gamma/DECISIONS.md A-100 (locked), from projects/alpha/DECISIONS.md"
        self.assertEqual(self.changed(w, "--project", "gamma"), {"Decisions": [line]})
        self.assertEqual(self.changed(w, "--project", "alpha"), {"Decisions": [line]})
        self.assertEqual(self.data(w, "--project", "alpha")["decisions"],
                         self.data(w, "--project", "gamma")["decisions"])

    def test_a_decision_that_left_a_project_reworded_or_as_a_pointer_is_still_its_line(self):
        w, alpha, gamma = self.team(), "projects/alpha/DECISIONS.md", "projects/gamma/DECISIONS.md"
        w.append(gamma, cut(w, alpha, "A-101").replace("Title 101", "Renamed"))
        cut(w, alpha, "A-100")
        w.append(gamma, "\n## A-100 - Replaced by G-300\n")
        self.assertEqual(self.changed(w, "--project", "alpha"), {"Decisions": [
            f"- superseded {gamma} A-100 -> G-300, from {alpha}",
            f"- changed    {gamma} A-101 (provisional), from {alpha}"]})

    def test_usage_errors_exit_two(self):
        w = self.solo()
        self.assertEqual(w.run("diff", "--project", "nope"),
                         (2, "", "error: no project 'nope' in config.toml\n"))
        self.assertEqual(w.run("diff", "--base", ""),
                         (2, "", "error: --base needs a ref, e.g. --base origin/main\n"))
        code, out, err = w.run("diff", "--frobnicate")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("unrecognized arguments: --frobnicate", err)

    def test_a_config_that_does_not_load_exits_two(self):
        w = self.solo()
        w.write(layout.CONFIG, w.read(layout.CONFIG) + "\n[no-such-table]\nx = 1\n")
        code, out, err = w.run("diff")
        self.assertEqual((code, out), (2, ""))
        self.assertTrue(err.startswith("error: "), err)

    def test_json_is_one_object_with_every_key_and_null_for_what_is_absent(self):
        w = self.team()
        w.write(layout.BASELINE, '{"handoff_words:alpha": 1600}\n')
        w.commit("a baseline")
        w.write(layout.BASELINE, '{"handoff_words:alpha": 1500, "handoff_words:gamma": 1700}\n')
        w.append("governance/DECISIONS.md",
                 decision("W", 3, "provisional") + cut(w, "projects/alpha/DECISIONS.md", "A-101"))
        w.edit("governance/DECISIONS.md", "Title 1\n\n**Status:** locked\n",
               "Title 1\n\n**Status:** locked\n\n**Revised:** 2026-10-06 (shorter)\n")
        w.edit("projects/gamma/DECISIONS.md", "Title 301\n\n**Status:** provisional",
               "Title 301\n\n**Status:** locked")
        cut(w, "projects/gamma/DECISIONS.md", "G-300")
        cut(w, "projects/alpha/DECISIONS.md", "A-100")
        w.append("projects/alpha/DECISIONS.md", "\n## A-100 - Replaced by W-3\n")
        w.append("projects/alpha/working-files/traps.md", trap(2))
        cut(w, "projects/gamma/working-files/traps.md", "T-1")
        code, out, err = w.run("diff", "--json", "--base", "main")
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.endswith("}\n") and not out.endswith("\n\n"))
        self.assertTrue(out.isascii())
        self.assertEqual(out, json.dumps(json.loads(out), indent=2, sort_keys=True) + "\n")
        gov, alpha, gamma = ("governance/DECISIONS.md", "projects/alpha/DECISIONS.md",
                             "projects/gamma/DECISIONS.md")
        self.assertEqual(json.loads(out), {
            "base": "main",
            "compared": [{"repo": ".", "commit": self.full(w), "reason": None}],
            "decisions": [
                row("moved", gov, "A-101", "Title 101", "provisional", was_log=alpha),
                row("revised", gov, "W-1", "Title 1", "locked", revised="2026-10-06"),
                row("added", gov, "W-3", "Title 3", "provisional"),
                # What was superseded is the decision as it stood: its title and status then.
                row("superseded", alpha, "A-100", "Title 100", "locked", replaced_by="W-3"),
                row("removed", gamma, "G-300", "Title 300", "locked"),
                row("status", gamma, "G-301", "Title 301", "locked", was_status="provisional")],
            "traps": [
                {"change": "added", "log": "projects/alpha/working-files/traps.md", "id": "T-2",
                 "title": "Trap 2"},
                {"change": "removed", "log": "projects/gamma/working-files/traps.md",
                 "id": "T-1", "title": "Trap 1"}],
            "baseline": [
                {"change": "lowered", "key": "handoff_words:alpha", "from": 1600, "to": 1500,
                 "reason": None},
                {"change": "added", "key": "handoff_words:gamma", "from": None, "to": 1700,
                 "reason": None}]})

    def test_a_decision_row_has_the_same_keys_whatever_its_word_and_no_from_or_to(self):
        w = self._busy()
        cut(w, "projects/gamma/DECISIONS.md", "G-300")
        w.append("governance/DECISIONS.md", cut(w, "projects/alpha/DECISIONS.md", "A-100"))
        data = self.data(w)
        self.assertEqual({row["change"] for row in data["decisions"]}, {"added", "removed", "moved"})
        for found in data["decisions"]:
            self.assertEqual(sorted(found), ["change", "id", "log", "replaced_by", "revised",
                                             "status", "title", "was_log", "was_status"])
        for found in data["traps"]:
            self.assertEqual(sorted(found), ["change", "id", "log", "title"])
        for found in data["baseline"]:
            self.assertEqual(sorted(found), ["change", "from", "key", "reason", "to"])

    def test_json_with_no_base_says_head_and_with_nothing_changed_has_empty_lists(self):
        w = self.solo()
        self.assertEqual(self.data(w), {
            "base": "HEAD", "baseline": [], "decisions": [], "traps": [],
            "compared": [{"repo": ".", "commit": self.full(w), "reason": None}]})

    def test_json_names_a_commit_by_its_whole_id_and_the_text_by_twelve_characters(self):
        w = self.team(nested=True)
        commits = [row["commit"] for row in self.data(w)["compared"]]
        self.assertEqual(commits, [self.full(w), self.full(w, w.root / "beta")])
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{40}([0-9a-f]{24})?", c) for c in commits))
        self.assertEqual(self.report(w)["Compared"],
                         [f"- . at {commits[0][:12]}", f"- beta at {commits[1][:12]}"])

    def test_the_json_and_the_text_describe_the_same_changes(self):
        w = self._busy()
        cut(w, "projects/gamma/DECISIONS.md", "G-300")
        w.edit("projects/alpha/DECISIONS.md", "## A-101 - Title 101", "## A-101 - Renamed")
        w.append("projects/gamma/DECISIONS.md", cut(w, "governance/DECISIONS.md", "W-2"))
        for flags in ((), ("--base", "main"), ("--project", "gamma")):
            with self.subTest(flags=flags):
                text, data = self.report(w, *flags), self.data(w, *flags)
                for section in ("Decisions", "Traps", "Baseline", "Compared"):
                    lines, rows = text.get(section, []), data[section.lower()]
                    self.assertEqual(len(lines), len(rows), section)
                    if section == "Baseline":
                        self.assertEqual([line.split()[1:3] for line in lines],
                                         [[found["change"], found["key"]] for found in rows])
                    elif section != "Compared":
                        # Every decision and trap line begins `- <word> <log> <id>`.
                        self.assertEqual([line.split()[1:4] for line in lines],
                                         [[found["change"], found["log"], found["id"]]
                                          for found in rows])
                self.assertGreater(len(data["decisions"]), 0)


class Console(Diff):
    """What a Windows console or a pipe with a legacy encoding is sent never raises: the
    report's own words are ASCII, and a project's words the console cannot encode are escaped."""

    def test_a_title_the_console_cannot_encode_is_escaped_not_a_traceback(self):
        w = self.solo()
        w.append(LOG, decision("P", 4, sep=" — ", title="Café → 测试"))
        w.append(TRAPS, trap(2, title="Naïve — 测试"))
        env = {**os.environ, "HOME": str(w.home), "PYTHONPATH": str(ENGINE),
               "CONTEXT_GATE_NO_UPDATE_CHECK": "1", "GIT_CONFIG_NOSYSTEM": "1"}
        for codec in ("cp1252", "ascii", "utf-8"):
            with self.subTest(codec=codec):
                run = lambda *flags: subprocess.run(
                    [sys.executable, "-m", "govern", "diff", *flags], cwd=w.root,
                    env={**env, "PYTHONIOENCODING": codec}, capture_output=True)
                res = run()
                self.assertEqual((res.returncode, res.stderr), (0, b""))
                out = res.stdout.decode(codec).replace("\r\n", "\n")
                want = "Café → 测试".encode(codec, "backslashreplace").decode(codec)
                self.assertIn(f"- added      {LOG} P-4 - {want} (locked)\n", out)
                res = run("--json")
                self.assertEqual((res.returncode, res.stderr), (0, b""))
                data = json.loads(res.stdout.decode("ascii"))
                self.assertEqual(data["decisions"][0]["title"], "Café → 测试")
                self.assertEqual(data["traps"][0]["title"], "Naïve — 测试")

    def test_control_characters_in_a_title_are_escaped_in_the_text_and_kept_in_the_json(self):
        # A title is the project's own text, and a pull request's author writes it: an escape
        # sequence in one must not reach the terminal or the job summary the report is put in.
        w = self.solo()
        title = "Red \x1b[31malert\x7f, a\ttab and a bell\x07 too"
        w.append(LOG, decision("P", 4, title=title))
        w.append(TRAPS, trap(2, title=title))
        out = self.text(w)
        shown = "Red \\x1b[31malert\\x7f, a\\x09tab and a bell\\x07 too"
        self.assertIn(f"- added      {LOG} P-4 - {shown} (locked)\n", out)
        self.assertIn(f"- added      {TRAPS} T-2 - {shown}\n", out)
        self.assertEqual([c for c in out if c != "\n" and (ord(c) < 0x20 or ord(c) == 0x7f)], [])
        data = self.data(w)
        self.assertEqual((data["decisions"][0]["title"], data["traps"][0]["title"]), (title, title))

    def test_the_layout_is_ascii_whatever_the_project_wrote(self):
        w = self.solo()
        w.append(LOG, decision("P", 4, sep=" — ", title="An em dash heading"))
        cut(w, LOG, "P-1")
        w.append(LOG, "\n## P-1 — Replaced by P-4\n")
        out = self.text(w)       # asserts it is ASCII
        self.assertNotIn("—", out)
        self.assertIn(f"- superseded {LOG} P-1 -> P-4\n", out)


class ClosedPipe(Diff):
    """A reader that stops reading (`govern diff | head -1`) ends the command quietly: exit 0
    and nothing on stderr, never a traceback."""

    def setUp(self) -> None:
        if os.name == "nt":
            self.skipTest("Windows reports a closed pipe to the writer another way (an invalid "
                          "argument, not a broken pipe), which these tests do not produce")
        super().setUp()

    def _env(self, w: Tree) -> dict:
        return {**os.environ, "HOME": str(w.home), "PYTHONPATH": str(ENGINE),
                "CONTEXT_GATE_NO_UPDATE_CHECK": "1", "GIT_CONFIG_NOSYSTEM": "1"}

    def test_diff_whose_reader_closes_after_one_line(self):
        w = self.solo()
        # A report longer than any pipe's buffer, so the writer is still writing when the
        # reader goes.
        w.append(LOG, "".join(decision("P", n, title="A title long enough to fill a pipe " * 4)
                              for n in range(4, 1504)))
        self.assertGreater(len(self.text(w)), 200_000)
        for flags, first in (((), b"Governance changes since HEAD\n"), (("--json",), b"{\n")):
            with self.subTest(flags=flags):
                proc = subprocess.Popen([sys.executable, "-m", "govern", "diff", *flags],
                                        cwd=w.root, env=self._env(w), stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE)
                line = proc.stdout.readline()
                proc.stdout.close()
                err = proc.stderr.read()
                proc.stderr.close()
                self.assertEqual((proc.wait(), err, line), (0, b"", first))

    def test_ci_github_into_a_pipe_nobody_reads(self):
        w = self.solo()
        read_end, write_end = os.pipe()
        os.close(read_end)
        try:
            res = subprocess.run([sys.executable, "-m", "govern", "ci", "github"], cwd=w.root,
                                 env=self._env(w), stdout=write_end, stderr=subprocess.PIPE)
        finally:
            os.close(write_end)
        self.assertEqual((res.returncode, res.stderr), (0, b""))


# ---------------------------------------------------------------------------- expected output

FIXED_FM = ("---\ndoc_type: {doc_type}\npurpose: test\naudience: agent\nload_when: testing\n"
            "last_reviewed: 2026-01-01\n---\n")

REFERENCE_CONFIG = """
[governance]
engine = "{version}"
schema = 1

[dialect]
markers = "gov"

[registry]
file = "projects.toml"
entries = "project"

[workspace]
docs = []
required_docs = []
decision_log = "docs/DECISIONS.md"

[projects]
required_docs = []
"""

REFERENCE_REGISTRY = """
[workspace]
id_prefix = "D"
id_range = "1-99"

[[project]]
name = "alpha"
dir = "projects/alpha"
tier = "full"
governance = "projects/alpha"
id_prefix = "A"
id_range = "100-199"

[[project]]
name = "beta"
dir = "projects/beta"
tier = "full"
governance = "projects/beta"
id_prefix = "B"
id_range = "200-299"

[[project]]
name = "delta"
dir = "projects/delta"
tier = "full"
governance = "projects/delta"
id_prefix = "C"
id_range = "300-399"
"""


class Fixed(Tree):
    """A tree whose commits have the same ids on every machine and in every time zone: one
    author, one date with its offset written out, SHA-1 objects, exact bytes. The config is
    never committed, since it names the engine version and that changes with every release.

    SHA-1 is asked for in the environment (`GIT_DEFAULT_HASH`), not by `init --object-format`:
    a git too old to know the variable has no other format, and one that knows it obeys it,
    so the fixture runs on both."""

    WHEN = "2026-01-02T03:04:05+00:00"

    def git(self, *args: str, at: Path | None = None, check: bool = True) -> str:
        env = {k: v for k, v in os.environ.items() if k not in REPO_ENV}
        env.update(HOME=str(self.home), GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0",
                   GIT_AUTHOR_DATE=self.WHEN, GIT_COMMITTER_DATE=self.WHEN,
                   GIT_DEFAULT_HASH="sha1")
        res = subprocess.run(
            ["git", "-C", str(at or self.root), "-c", "user.email=t@t", "-c", "user.name=t",
             "-c", "tag.gpgSign=false", "-c", "commit.gpgSign=false", "-c", "core.autocrlf=false",
             "-c", "core.fileMode=false", *args],
            check=check, capture_output=True, env=env)
        return res.stdout.decode("utf-8")

    def init(self, at: Path | None = None, branch: str = "main") -> None:
        self.git("init", "-q", at=at)
        self.git("symbolic-ref", "HEAD", f"refs/heads/{branch}", at=at)

    def commit(self, message: str = "work", at: Path | None = None, index: bool = False) -> None:
        self.git("add", "-A", "--", ".", f":(exclude){layout.CONFIG}", at=at)
        self.git("commit", "-qm", message, at=at)


def reference_tree(tmp: Path) -> Fixed:
    """A workspace on a branch that did one of everything the report has a word for: its own
    log and project `alpha` in one repository, project `beta` in a repository of its own, and
    project `delta` in one whose branch is not called `main`, so it cannot be compared."""
    w = Fixed(tmp / "reference", tmp / "home")
    control, working = FIXED_FM.format(doc_type="control"), FIXED_FM.format(doc_type="working")
    w.write(layout.CONFIG, REFERENCE_CONFIG.format(version=__version__))
    w.write("projects.toml", REFERENCE_REGISTRY)
    w.write(".gitignore", "projects/beta/\nprojects/delta/\n")
    w.write("docs/DECISIONS.md", control + "# Decisions\n"
            + decision("D", 8, title="Keep the log short").replace("## D-8 ", "## D-08 ")
            + decision("D", 9, "deferred", title="Maybe later").replace("## D-9 ", "## D-09 ")
            + decision("D", 12, title="One writer per file")
            + decision("D", 14, "provisional", title="Review before merge")
            + decision("D", 15, title="Alpha owns its schema")
            + decision("D", 20, "provisional", title="Weekly cleanup"))
    for name, prefix, lo in (("alpha", "A", 100), ("beta", "B", 200), ("delta", "C", 300)):
        w.write(f"projects/{name}/DECISIONS.md", control + "# Decisions\n" + decision(prefix, lo))
        w.write(f"projects/{name}/working-files/traps.md",
                working + "# Traps\n" + trap(3) + trap(5))
    w.write(layout.BASELINE, json.dumps({
        "decision_words:alpha:A-100": 900, "handoff_words:alpha": 1720,
        "working_file_words:docs/plan.md": 6100}, indent=2, sort_keys=True) + "\n")
    w.init()
    w.commit("start")
    w.init(at=w.root / "projects/beta")
    w.commit("start", at=w.root / "projects/beta")
    w.init(at=w.root / "projects/delta", branch="trunk")
    w.commit("start", at=w.root / "projects/delta")

    # What landed on main after the branch began is not the branch's.
    w.branch("work")
    w.switch("main")
    w.append("docs/DECISIONS.md", decision("D", 30, title="Landed on main meanwhile"))
    w.commit("theirs")
    w.switch("work")

    log = "docs/DECISIONS.md"
    cut(w, log, "D-09")                                                      # removed
    cut(w, log, "D-12")
    w.append(log, "\n## D-12 - Replaced by D-31\n")                          # superseded
    w.append("projects/alpha/DECISIONS.md", cut(w, log, "D-15"))             # moved
    w.append(log, cut(w, "projects/alpha/DECISIONS.md", "A-100")             # moved, and changed
             .replace("always write the rule down", "write the rule down"))
    w.edit(log, "Keep the log short\n\n**Status:** locked\n",                # revised
           "Keep the log short\n\n**Status:** locked\n\n**Revised:** 2026-10-06 (shorter)\n")
    w.edit(log, "Review before merge\n\n**Status:** provisional",            # status
           "Review before merge\n\n**Status:** locked")
    w.edit(log, "## D-20 - Weekly cleanup", "## D-20 - Monthly cleanup")     # changed
    w.append(log, decision("D", 31, "provisional", title="Two writers, one log"))   # added
    cut(w, "projects/alpha/working-files/traps.md", "T-3")
    w.append("projects/alpha/working-files/traps.md", trap(7, title="The cache is stale"))
    w.write(layout.BASELINE, json.dumps({
        "decision_words:alpha:A-100": 700, "governed_docs:alpha": 41,
        "working_file_words:docs/plan.md": 6400}, indent=2, sort_keys=True) + "\n")
    w.commit("ours")
    # Uncommitted work is part of what the branch did. The same trap id in another project,
    # in its own repository, is another trap.
    w.edit("projects/alpha/working-files/traps.md", "it is 5 o'clock", "it is after 5 o'clock")
    w.append("projects/beta/working-files/traps.md", trap(7, title="The clock is local"))
    w.append("projects/delta/DECISIONS.md", decision("C", 301))
    return w


class ExpectedOutput(Diff):
    """The text layout and the JSON keys, byte for byte (the files are stored with LF endings:
    `expected/.gitattributes`)."""

    def _compare(self, name: str, *flags: str) -> None:
        w = reference_tree(self.tmp)
        self.assertEqual((self.head(w, ref="main~1"), self.head(w, w.root / "projects/beta")),
                         ("9b5d6021d99d", "e761f8be87ae"),
                         "the fixture's commits have new ids: the fixture changed")
        code, out, err = w.run("diff", "--base", "main", *flags)
        self.assertEqual((code, err), (0, ""))
        got, path = out.encode("utf-8"), EXPECTED / name
        if os.environ.get("GOVERN_UPDATE_EXPECTED"):
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(got)
        self.assertEqual(got.decode("utf-8"), path.read_bytes().decode("utf-8"))
        self.assertEqual(got, path.read_bytes())

    def test_text(self):
        self._compare("diff.txt")

    def test_json(self):
        self._compare("diff.json", "--json")

    def test_the_fixture_uses_every_word_the_report_has(self):
        text = (EXPECTED / "diff.txt").read_text(encoding="utf-8")
        for word in ("removed", "added", "superseded", "moved", "revised", "status", "changed",
                     "raised", "lowered"):
            self.assertIn(f"- {word:<10} ", text)
        self.assertIn(": not compared (", text)
        # The one word it cannot hold beside the others, since an unreadable baseline is the
        # whole of its section: `Baseline.test_unreadable_in_both_forms` pins that line.
        self.assertNotIn("- unreadable ", text)
        # An entry in another log than at the base says so, whatever its word.
        self.assertEqual(len(re.findall(r"^- (?:moved|changed) .*, from \S+$", text, re.M)), 2)

    def test_the_two_files_hold_the_same_rows(self):
        text = sections((EXPECTED / "diff.txt").read_text(encoding="utf-8"))
        data = json.loads((EXPECTED / "diff.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(data), ["base", "baseline", "compared", "decisions", "traps"])
        for name in ("Decisions", "Traps"):
            self.assertEqual([line.split()[1:4] for line in text[name]],
                             [[found["change"], found["log"], found["id"]]
                              for found in data[name.lower()]])
        self.assertEqual([line.split()[1:3] for line in text["Baseline"]],
                         [[found["change"], found["key"]] for found in data["baseline"]])


# ---------------------------------------------------------------------------- ci github

WORKFLOW_STEPS = """\
    steps:
      - uses: actions/checkout@v4
        with:
          # The whole history: the gate compares with the commit this branch left the base at.
          fetch-depth: 0
          # The branch's own commit, not GitHub's merge of it into the base: by the pull
          # request's own ref, which is there to fetch when the branch is in a fork.
          ref: refs/pull/${{ github.event.pull_request.number }}/head
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: Gate
        env:
          BASE_REF: ${{ github.base_ref }}
        run: python3 .context-gate/bin/govern check --base "origin/$BASE_REF"
      - name: What this pull request changes
        if: always()
        env:
          BASE_REF: ${{ github.base_ref }}
        run: python3 .context-gate/bin/govern diff --base "origin/$BASE_REF" >> "$GITHUB_STEP_SUMMARY"
"""


class CiGithub(Case):
    def test_it_prints_the_workflow(self):
        w = self.solo()
        code, out, err = w.run("ci", "github")
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.isascii() and out.endswith("\n") and "\r" not in out and "\t" not in out)
        self.assertTrue(out.endswith(WORKFLOW_STEPS), out)
        lines = out.splitlines()
        live = [line for line in lines if line.strip() and not line.lstrip().startswith("#")]
        self.assertEqual(live[:8], [
            "name: context-gate", "on:", "  pull_request:", "permissions:", "  contents: read",
            "jobs:", "  gate:", "    runs-on: ubuntu-latest"])
        self.assertEqual(live[8], "    steps:")
        # YAML-shaped: spaces only, an even indent, every line a comment, a key or a list item.
        for line in lines:
            body = line.lstrip(" ")
            self.assertEqual((len(line) - len(body)) % 2, 0, line)
            self.assertTrue(not body or body.startswith(("#", "- ")) or ":" in body, line)
        # Every action is pinned by its major version tag.
        uses = [line.split("uses:")[1].strip() for line in lines if "uses:" in line]
        self.assertEqual(uses, ["actions/checkout@v4", "actions/setup-python@v5"])
        self.assertEqual(out.count("if: always()"), 1)

    def test_it_is_the_template_file_and_the_file_is_plain(self):
        w = self.solo()
        _, out, _ = w.run("ci", "github")
        raw = TEMPLATE.read_bytes()
        self.assertEqual(out, raw.decode("ascii").replace("\r\n", "\n"))
        for private in ("self-hosted", "secrets."):
            self.assertNotIn(private, out)

    def test_it_writes_nothing(self):
        w = self.solo()
        before = tree_state(w.root, w.home)
        w.run("ci", "github")
        self.assertEqual(tree_state(w.root, w.home), before)
        self.assertFalse((w.root / ".github").exists())

    def test_it_needs_no_project(self):
        bare = Tree(self.tmp / "bare", self.tmp / "home")
        bare.root.mkdir()
        code, out, err = bare.run("ci", "github")
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.endswith(WORKFLOW_STEPS))
        self.assertEqual(list(bare.root.iterdir()), [])

    def test_an_unknown_provider_is_a_usage_error_naming_github(self):
        w = self.solo()
        for argv in (("ci", "gitlab"), ("ci",), ("ci", "GitHub")):
            with self.subTest(argv=argv):
                code, out, err = w.run(*argv)
                self.assertEqual((code, out), (2, ""))
                self.assertTrue(err.startswith("error: "), err)
                self.assertIn("github", err)
        self.assertIn("'gitlab'", w.run("ci", "gitlab")[2])

    def test_the_commands_in_it_are_ones_this_engine_has(self):
        w = self.solo()
        _, out, _ = w.run("ci", "github")
        runs = [line.split("run:", 1)[1].strip() for line in out.splitlines() if "run:" in line]
        self.assertEqual(len(runs), 2)
        for run in runs:
            self.assertTrue(run.startswith(f"python3 {layout.ENTRYPOINT} "), run)
            argv = run.removeprefix(f"python3 {layout.ENTRYPOINT} ").split(" >> ")[0]
            self.assertEqual(argv.count('"origin/$BASE_REF"'), 1, run)
            argv = argv.replace('"origin/$BASE_REF"', "main").split()
            code, _, err = w.run(*argv)
            self.assertEqual((code, err), (0, ""), run)

    def test_nothing_github_fills_in_is_pasted_into_a_script(self):
        # `${{ ... }}` is replaced in the script's text before a shell reads it, so a branch
        # named `x;id` would run `id`. A value reaches the script as an environment variable,
        # quoted where it is used.
        w = self.solo()
        _, out, _ = w.run("ci", "github")
        runs = [line for line in out.splitlines() if line.lstrip().startswith("run:")]
        self.assertEqual(len(runs), 2)
        for run in runs:
            self.assertNotIn("${{", run)
            self.assertIn(' --base "origin/$BASE_REF"', run)
        self.assertEqual(out.count("          BASE_REF: ${{ github.base_ref }}\n"), 2)

    def test_it_checks_out_the_pull_requests_own_ref_with_the_whole_history(self):
        # A fork's head commit is in no branch of the base repository, so asking for it by its
        # id can fail to fetch; `refs/pull/<n>/head` is always there.
        w = self.solo()
        _, out, _ = w.run("ci", "github")
        self.assertIn("          ref: refs/pull/${{ github.event.pull_request.number }}/head\n", out)
        self.assertNotIn("head.sha", out)
        self.assertEqual(out.count("          fetch-depth: 0\n"), 1)
        self.assertIn("\npermissions:\n  contents: read\n", out)

    def test_the_comment_step_in_the_docs_pastes_nothing_into_its_script_either(self):
        with open(CONFIGURATION, encoding="utf-8", newline="") as fh:
            text = fh.read().replace("\r\n", "\n")
        [step] = [block for block in re.findall(r"^```yaml\n(.*?)^```", text, re.M | re.S)
                  if "gh pr comment" in block]
        settings, script = step.split("        run: |\n")
        self.assertNotIn("${{", script)
        self.assertIn('diff --base "origin/$BASE_REF" > ', script)
        self.assertIn('gh pr comment "$PR_URL" ', script)
        self.assertIn("          BASE_REF: ${{ github.base_ref }}\n", settings)
        self.assertIn("          PR_URL: ${{ github.event.pull_request.html_url }}\n", settings)


if __name__ == "__main__":
    unittest.main()
