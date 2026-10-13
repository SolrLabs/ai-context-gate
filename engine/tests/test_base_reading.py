"""Reading the base branch: how it is found and paired, the git calls, layouts and an ordinary day."""
from __future__ import annotations

import collections
import os
import shutil
import stat
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from base_support import *  # noqa: E402,F401,F403
import types  # noqa: E402
import test_base_decisions  # noqa: E402

# SettledDecisionChangedInPlace lives in test_base_decisions; a plain namespace keeps the test runner from collecting it twice.
SettledDecisionChangedInPlace = types.SimpleNamespace(EDIT=test_base_decisions.SettledDecisionChangedInPlace.EDIT)

# ---------------------------------------------------------------------------- reading the base

class ReadingTheBase(Case):
    """A log that is not in the base commit is a new log, and nothing is said. A log git could
    not read there, or holds there under another name, is not a new log: each of these once
    read as one, and the gate stayed green."""

    LOWER, RENAMED = "docs/decisions.md", "docs/LOG.md"

    def _named(self, w: Tree, rel: str) -> None:
        """Point the config at `rel` for the decision log (and its generated block)."""
        w.write(layout.CONFIG, w.read(layout.CONFIG).replace(LOG, rel))

    def _lower_case(self, w: Tree) -> None:
        """The config and the working copy say `docs/decisions.md`; git tracks
        `docs/DECISIONS.md`. One file where the file system ignores case, and the same
        question to git everywhere."""
        self._named(w, self.LOWER)
        os.rename(w.root / LOG, w.root / self.LOWER)

    def test_a_base_log_missing_from_the_object_store_fails_the_gate(self):
        w = self.solo()
        head = w.git("rev-parse", "HEAD").strip()
        w.edit(LOG, *SettledDecisionChangedInPlace.EDIT)
        blob = w.git("rev-parse", f"HEAD:{LOG}").strip()
        held = w.root / ".git" / "objects" / blob[:2] / blob[2:]
        held.chmod(stat.S_IREAD | stat.S_IWRITE)        # git's objects are read-only
        held.unlink()
        self.silent(w)
        self.assertEqual(self.found(w, "--base", "main"), (1, [ERROR + no_compare(
            "main", f"git could not read {LOG} at {head[:12]}")]))

    def test_a_log_git_tracks_under_a_name_in_another_case_is_read_from_it(self):
        w = self.solo()
        self._lower_case(w)
        self.assertEqual(w.git("ls-tree", "--name-only", "HEAD", "--", "docs/").split(),
                         [LOG, "docs/working"])
        self.silent(w)
        self.silent(w, "--base", "main")
        w.edit(self.LOWER, *SettledDecisionChangedInPlace.EDIT)
        line = changed(self.LOWER, "P-1")
        self.assertEqual(self.found(w), (0, [WARN + line]))
        self.assertEqual(self.found(w, "--base", "main"), (1, [ERROR + line]))

    def test_two_base_files_that_differ_only_in_case_are_not_guessed_between(self):
        w = self.solo()
        blob = w.git("rev-parse", f"HEAD:{LOG}").strip()
        exact = ("-c", "core.ignorecase=false")
        w.git(*exact, "update-index", "--add", "--cacheinfo", f"100644,{blob},docs/Decisions.md")
        w.git(*exact, "commit", "-qm", "the same name twice, where the file system ignores case")
        head = w.git("rev-parse", "HEAD").strip()
        self.assertEqual(w.git("ls-tree", "--name-only", "HEAD", "--", "docs/").split(),
                         [LOG, "docs/Decisions.md", "docs/working"])
        self._lower_case(w)
        w.edit(self.LOWER, *SettledDecisionChangedInPlace.EDIT)
        self.silent(w)
        self.assertEqual(self.found(w, "--base", "main"), (1, [ERROR + no_compare(
            "main", f"{head[:12]} holds {self.LOWER} under 2 names that differ only in case")]))

    def test_an_edit_made_in_the_same_change_as_a_rename_of_the_log_is_compared(self):
        w = self.solo()
        w.branch("work")
        w.git("mv", LOG, self.RENAMED)
        self._named(w, self.RENAMED)
        self.silent(w)                                   # renamed, and nothing else
        self.silent(w, "--base", "main")
        w.edit(self.RENAMED, *SettledDecisionChangedInPlace.EDIT)
        line = changed(self.RENAMED, "P-1")
        self.assertEqual(self.found(w), (0, [WARN + line]))
        self.assertEqual(self.found(w, "--base", "main"), (1, [ERROR + line]))
        w.commit("renamed and reworded")
        self.silent(w)
        self.assertEqual(self.found(w, "--base", "main"), (1, [ERROR + line]))

    def test_a_decision_removed_in_the_same_change_as_a_rename_of_the_log(self):
        w = self.solo()
        w.git("mv", LOG, self.RENAMED)
        self._named(w, self.RENAMED)
        head, _, rest = w.read(self.RENAMED).partition("\n## P-2 - Title 2\n")
        w.write(self.RENAMED, head + "\n## P-3" + rest.partition("\n## P-3")[2])
        self.assertEqual(self.found(w, "--base", "main"), (0, [WARN + removed(
            self.RENAMED, "P-2", "provisional", "## P-2 - Replaced by <id>")]))

    def test_an_id_taken_on_the_base_branch_is_found_from_a_renamed_log(self):
        # The base branch still has the log under its old name: that is where its P-4 is.
        w = self.solo()
        w.branch("work")
        w.git("mv", LOG, self.RENAMED)
        self._named(w, self.RENAMED)
        w.append(self.RENAMED, decision("P", 4, title="Ours"))
        w.commit("ours, in the renamed log")
        w.switch("main")
        w.append(LOG, decision("P", 4, title="Their title"))
        w.commit("theirs")
        w.switch("work")
        self.assertEqual(self.found(w, "--base", "main"),
                         (1, [ERROR + taken(self.RENAMED, "P-4", "main", "Their title")]))

    MORE = 52

    def _rewritten(self, w: Tree, tell_git: bool = True) -> None:
        """The log renamed, the config pointed at it, `P-1` (locked) reworded with no Revised
        line, and 52 decisions added, all in one change: so much that git reads no rename, only
        one file deleted and another added."""
        if tell_git:
            w.git("mv", LOG, self.RENAMED)
        else:
            os.rename(w.root / LOG, w.root / self.RENAMED)
        self._named(w, self.RENAMED)
        w.edit(self.RENAMED, *SettledDecisionChangedInPlace.EDIT)
        w.append(self.RENAMED, "".join(decision("P", n, "provisional")
                                       for n in range(4, 4 + self.MORE)))

    def test_a_log_renamed_and_rewritten_in_one_commit_is_found_by_the_ids_it_holds(self):
        w = self.solo()
        w.branch("work")
        self._rewritten(w)
        line = changed(self.RENAMED, "P-1")
        self.assertEqual(self.found(w), (0, [WARN + line]))
        w.commit("renamed, reworded, and 52 more")
        fork = base.resolve(w.root, "main")
        self.assertEqual(base.since(w.root, fork)[0], {})        # git reads no rename
        self.silent(w)
        self.assertEqual(self.found(w, "--base", "main"), (1, [ERROR + line]))
        cmp = base.compare(w.context("main"))
        self.assertEqual([(i.log, i.entry.ident) for i in cmp.side("base", base.DECISION)],
                         [(LOG, "P-1"), (LOG, "P-2"), (LOG, "P-3")])
        self.assertEqual(len(cmp.side("current", base.DECISION)), 3 + self.MORE)

    def test_a_log_moved_without_telling_git_and_rewritten_is_found_the_same_way(self):
        # `mv`, not `git mv`: the new file is one git does not track yet.
        w = self.solo()
        self._rewritten(w, tell_git=False)
        line = changed(self.RENAMED, "P-1")
        self.assertEqual(self.found(w), (0, [WARN + line]))
        self.assertEqual(self.found(w, "--base", "main"), (1, [ERROR + line]))

    def test_a_decision_dropped_while_the_log_was_renamed_and_rewritten_is_removed(self):
        w = self.solo()
        w.branch("work")
        self._rewritten(w)
        head, _, rest = w.read(self.RENAMED).partition("\n## P-3 - Title 3\n")
        w.write(self.RENAMED, head + "\n" + BLOCK + "\n## P-4" + rest.partition("\n## P-4")[2])
        w.commit("renamed, reworded, P-3 dropped, and 52 more")
        self.assertEqual(self.found(w, "--base", "main"), (1, [
            ERROR + changed(self.RENAMED, "P-1"),
            ERROR + removed(LOG, "P-3", "locked", "## P-3 - Replaced by <id>")]))

    def test_an_id_taken_on_the_base_branch_is_found_from_a_log_renamed_and_rewritten(self):
        # The base branch still has the log under its old name: that is where its P-60 is.
        w = self.solo()
        w.branch("work")
        self._rewritten(w)
        w.append(self.RENAMED, decision("P", 60, title="Ours"))
        w.commit("ours, in the rewritten log")
        w.switch("main")
        w.append(LOG, decision("P", 60, title="Their title"))
        w.commit("theirs")
        w.switch("work")
        self.assertEqual(self.found(w, "--base", "main"), (1, [
            ERROR + changed(self.RENAMED, "P-1"),
            ERROR + taken(self.RENAMED, "P-60", "main", "Their title")]))

    def _both_rewritten(self, w: Team) -> None:
        """On a branch: alpha's log and gamma's renamed `LOG.md`, the config pointed at the
        name, and 52 decisions added to each. Staged, not committed; git reads no rename."""
        w.branch("work")
        w.write(layout.CONFIG, w.read(layout.CONFIG).replace(
            '[projects]\n', '[projects]\ndecision_log = "LOG.md"\n').replace(
            'file = "DECISIONS.md"', 'file = "LOG.md"'))
        for where, prefix, lo in (("projects/alpha", "A", 100), ("projects/gamma", "G", 300)):
            w.git("mv", f"{where}/DECISIONS.md", f"{where}/LOG.md")
            w.append(f"{where}/LOG.md", "".join(decision(prefix, lo + n, "provisional")
                                               for n in range(2, 2 + self.MORE)))

    BOTH = [("governance/DECISIONS.md", "W-1", "workspace"),
            ("governance/DECISIONS.md", "W-2", "workspace"),
            ("projects/alpha/DECISIONS.md", "A-100", "alpha"),
            ("projects/alpha/DECISIONS.md", "A-101", "alpha"),
            ("projects/gamma/DECISIONS.md", "G-300", "gamma"),
            ("projects/gamma/DECISIONS.md", "G-301", "gamma")]

    def _base_side(self, w: Tree) -> list[tuple[str, str, str]]:
        cmp = base.compare(w.context("main"), traps=False)
        self.assertEqual(cmp.not_compared, [])
        return sorted((i.log, i.entry.ident, i.scope.name) for i in cmp.side("base"))

    def test_each_of_two_logs_renamed_and_rewritten_is_found_from_its_own(self):
        w = self.team()
        self._both_rewritten(w)
        w.edit("projects/alpha/LOG.md", "## A-100 - Title 100", "## A-100 - Another title")
        w.commit("both logs renamed and rewritten")
        self.assertEqual(base.since(w.root, base.resolve(w.root, "main"))[0], {})
        line = ERROR + changed("projects/alpha/LOG.md", "A-100")
        self.assertEqual(self.found(w, "--base", "main"), (1, [line]))
        self.assertEqual(self.found(w, "--project", "alpha", "--base", "main"), (1, [line]))
        self.silent(w, "--project", "gamma", "--base", "main")
        self.assertEqual(self._base_side(w), self.BOTH)

    def test_a_deleted_file_two_logs_hold_ids_of_is_read_once_and_is_its_own_projects(self):
        # G-301 moved to alpha's new log while both were renamed and rewritten, so gamma's old
        # log holds an id of each new one. It is still one file, gamma's, read once: a
        # decision dropped from it is gamma's removal and nobody else's.
        w = self.team()
        w.append("projects/gamma/DECISIONS.md", decision("G", 399))
        w.commit("a third decision of gamma's")
        self._both_rewritten(w)
        head, mark, entry = w.read("projects/gamma/LOG.md").partition("\n## G-301 - ")
        entry, _, rest = entry.partition("\n## G-399 - ")
        w.write("projects/gamma/LOG.md",
                head + "\n## G-302 - " + rest.partition("\n## G-302 - ")[2])
        w.append("projects/alpha/LOG.md", mark + entry)
        w.commit("both logs renamed and rewritten, G-301 moved and G-399 dropped")
        self.assertEqual(base.since(w.root, base.resolve(w.root, "main"))[0], {})
        self.assertEqual(self._base_side(w), self.BOTH + [
            ("projects/gamma/DECISIONS.md", "G-399", "gamma")])
        gone = ERROR + removed("projects/gamma/DECISIONS.md", "G-399", "locked",
                               "## G-399 - Replaced by <id>")
        self.assertEqual(self.found(w, "--base", "main"), (1, [gone]))
        self.assertEqual(self.found(w, "--project", "gamma", "--base", "main"), (1, [gone]))
        self.silent(w, "--project", "alpha", "--base", "main")

    def test_a_deleted_log_that_is_read_by_name_is_not_read_again_for_another_log(self):
        # Gamma has no log at the base. Alpha's is deleted outright, and A-100 is now in the
        # log gamma starts: alpha's old log is read as alpha's, once, and A-100 has moved.
        w = self.team()
        w.git("rm", "-q", "projects/gamma/DECISIONS.md")
        w.git("commit", "-qm", "gamma has no log yet")
        w.branch("work")
        moved = decision("A", 100)
        self.assertIn(moved, w.read("projects/alpha/DECISIONS.md"))
        w.git("rm", "-q", "projects/alpha/DECISIONS.md")
        w.write("projects/gamma/DECISIONS.md", fm() + "# Decisions\n\n" + BLOCK + moved + "".join(
            decision("G", 300 + n, "provisional") for n in range(self.MORE)))
        w.commit("alpha's log deleted, gamma's begun")
        self.assertEqual(base.since(w.root, base.resolve(w.root, "main"))[0], {})
        self.assertEqual(self._base_side(w), [row for row in self.BOTH if row[2] != "gamma"])
        # A project with no log is an error of other checks; this one adds one warning.
        self.assertEqual(self.found(w, "--base", "main")[1], [WARN + removed(
            "projects/alpha/DECISIONS.md", "A-101", "provisional",
            "## A-101 - Replaced by <id>")])

    def test_a_log_deleted_outright_with_no_successor_is_every_settled_decision_removed(self):
        # Nothing took its place, and other Markdown went in the same commit: still removals.
        w = self.solo()
        w.write("notes/old.md", "# Old notes\n")
        w.commit("notes")
        w.branch("work")
        w.git("rm", "-q", LOG, "notes/old.md")
        w.git("commit", "-qm", "the log, deleted")
        code, lines = self.found(w, "--base", "main")
        self.assertEqual((code, lines), (1, [
            ERROR + removed(LOG, "P-1", "locked", "## P-1 - Replaced by <id>"),
            ERROR + removed(LOG, "P-3", "locked", "## P-3 - Replaced by <id>"),
            WARN + removed(LOG, "P-2", "provisional", "## P-2 - Replaced by <id>")]))

    def test_an_unrelated_deleted_markdown_file_changes_nothing(self):
        # A log that is new, in a change that also deletes Markdown holding none of its ids:
        # an entry under another id, and the log's own P-1 only as an example in a code fence.
        w = self._no_log_yet()
        w.write("notes/old.md", "# Old notes\n" + decision("P", 900)
                + "\n```markdown\n## P-1 - Title 1\n```\n\n<!--\n## P-2 - Title 2\n-->\n")
        w.git("add", "notes")
        w.git("commit", "-qm", "notes")
        w.git("rm", "-q", "notes/old.md")
        self.silent(w)
        self.silent(w, "--base", "main")
        cmp = base.compare(w.context("main"))
        self.assertEqual((cmp.not_compared, cmp.side("base", base.DECISION)), ([], []))

    def test_an_unrelated_deleted_markdown_file_beside_the_log_it_was_changes_nothing(self):
        w = self.solo()
        w.write("notes/old.md", "# Old notes\n" + decision("P", 900))
        w.commit("notes")
        w.branch("work")
        w.git("rm", "-q", "notes/old.md")
        self._rewritten(w)
        w.commit("renamed, reworded, 52 more, and the notes deleted")
        self.assertEqual(self.found(w, "--base", "main"),
                         (1, [ERROR + changed(self.RENAMED, "P-1")]))
        cmp = base.compare(w.context("main"))
        self.assertEqual({i.log for i in cmp.side("base", base.DECISION)}, {LOG})

    def _deleted(self, count: int) -> Solo:
        """A project whose log is new, in a change that deletes `count` Markdown files."""
        w = self._no_log_yet()
        for n in range(count):
            w.write(f"notes/{n}.md", f"# Note {n}\n")
        w.git("add", "notes")
        w.git("commit", "-qm", "notes")
        w.git("rm", "-qr", "notes")
        return w

    def test_more_deleted_markdown_files_than_are_read_fails_the_gate_only(self):
        w = self._deleted(base.DELETED_READ + 1)
        head = w.git("rev-parse", "HEAD").strip()
        self.silent(w)
        self.assertEqual(self.found(w, "--base", "main"), (1, [ERROR + no_compare(
            "main", f"{LOG} is not at {head[:12]}, and 51 Markdown files were deleted since: "
                    f"more than the 50 that are read to find the log it was")]))

    def test_as_many_deleted_markdown_files_as_are_read_is_still_a_new_log(self):
        w = self._deleted(base.DELETED_READ)
        self.silent(w)
        self.silent(w, "--base", "main")
        self.assertEqual(base.compare(w.context("main")).not_compared, [])

    def test_a_deleted_markdown_file_that_is_not_utf8_fails_the_gate_only(self):
        # It may have been the log: it cannot be read, so nothing says it was not.
        w = self._no_log_yet()
        w.write("notes/old.md", b"# Old notes\n\xff\n")
        w.git("add", "notes")
        w.git("commit", "-qm", "notes")
        head = w.git("rev-parse", "HEAD").strip()
        w.git("rm", "-q", "notes/old.md")
        self.silent(w)
        self.assertEqual(self.found(w, "--base", "main"), (1, [ERROR + no_compare(
            "main", f"notes/old.md at {head[:12]} is not valid UTF-8 (byte 12)")]))

    def _no_log_yet(self) -> Solo:
        """A project whose first commit has no decision log: the one on disk is new."""
        w = self.solo(git="init")
        w.git("add", "-A")
        w.git("rm", "-q", "--cached", LOG)
        w.git("commit", "-qm", "before there was a log")
        return w

    def test_a_log_that_really_is_new_is_still_silent(self):
        w = self._no_log_yet()
        self.silent(w)
        self.silent(w, "--base", "main")
        cmp = base.compare(w.context("main"))
        self.assertEqual((cmp.not_compared, cmp.side("base", base.DECISION)), ([], []))
        self.assertEqual(len(cmp.side("current", base.DECISION)), 3)

    def test_a_git_that_cannot_list_the_base_fails_the_gate(self):
        w = self.solo()
        head = w.git("rev-parse", "HEAD").strip()
        w.edit(LOG, *SettledDecisionChangedInPlace.EDIT)
        env = self.stand_in_git('case " $* " in *" ls-tree -z "*) exit 1;; esac')
        self.silent(w, env=env)
        self.assertEqual(self.found(w, "--base", "main", env=env), (1, [ERROR + no_compare(
            "main", f"git could not list docs at {head[:12]}")]))

    def test_a_git_that_cannot_say_what_was_renamed_fails_the_gate(self):
        # A log on disk that is not at the base is new, or renamed: only git can say which.
        w = self._no_log_yet()
        head = w.git("rev-parse", "HEAD").strip()
        env = self.stand_in_git('case " $* " in *" diff "*) exit 1;; esac')
        self.silent(w, env=env)
        self.assertEqual(self.found(w, "--base", "main", env=env), (1, [ERROR + no_compare(
            "main", f"git could not say what was renamed since {head[:12]}")]))


# ---------------------------------------------------------------------------- pairing, and what is asked

class Pairing(Case):
    """`base.match`: which base entry is which entry now, when one id is in several logs."""

    ALPHA, GAMMA, GOV = ("projects/alpha/DECISIONS.md", "projects/gamma/DECISIONS.md",
                         "governance/DECISIONS.md")

    def _twice(self) -> Team:
        """A-150 is in alpha's log and in gamma's, both locked, saying different things."""
        w = self.team()
        w.append(self.ALPHA, decision("A", 150, title="Alpha's"))
        w.append(self.GAMMA, decision("A", 150, title="Gamma's"))
        w.commit("one id, two logs")
        return w

    def _cut(self, w: Tree, rel: str) -> str:
        head, mark, entry = w.read(rel).partition("\n## A-150 - ")
        assert mark, rel
        w.write(rel, head)
        return mark + entry

    def test_an_id_in_two_logs_is_paired_log_by_log_before_anything_is_called_moved(self):
        # Gamma's A-150 moves to the workspace's log, which is read first: alpha's is still
        # alpha's, and neither changed.
        w = self._twice()
        w.append(self.GOV, self._cut(w, self.GAMMA))
        cmp = base.compare(w.context(), traps=False)
        matched, removed_, added = base.match(cmp.side("base"), cmp.side("current"))
        self.assertEqual([(was.log, now.log, now.entry.title) for was, now in matched
                          if was.entry.ident == "A-150"],
                         [(self.ALPHA, self.ALPHA, "Alpha's"), (self.GAMMA, self.GOV, "Gamma's")])
        self.assertEqual((removed_, added), ([], []))
        self.silent(w)
        self.silent(w, "--base", "main")

    def test_two_left_over_on_one_side_allow_two_readings_and_nothing_is_said(self):
        # Both are gone from their logs and one A-150, saying a third thing, is in the
        # workspace's: which of the two it was is anybody's guess.
        w = self._twice()
        self._cut(w, self.ALPHA)
        self._cut(w, self.GAMMA)
        w.append(self.GOV, decision("A", 150, title="A third thing"))
        cmp = base.compare(w.context(), traps=False)
        matched, removed_, added = base.match(cmp.side("base"), cmp.side("current"))
        self.assertEqual([was.entry.ident for was, _ in matched].count("A-150"), 0)
        self.assertEqual((removed_, added), ([], []))
        self.silent(w)
        self.silent(w, "--base", "main")
        # And the other way about: one at the base, two now, neither where it was.
        matched, removed_, added = base.match(cmp.side("current"), cmp.side("base"))
        self.assertEqual([was.entry.ident for was, _ in matched].count("A-150"), 0)
        self.assertEqual((removed_, added), ([], []))


class WhatIsKept(Case):
    def test_the_first_reason_a_repository_was_not_compared_is_the_one_kept(self):
        # A repository is compared whole or not at all, and says why once: a ref it does not
        # have before a file that cannot be read, the first such file before the next.
        w = self.team(nested=True)
        for rel in ("governance/DECISIONS.md", "projects/alpha/DECISIONS.md", "beta/DECISIONS.md"):
            w.write(rel, b"# Decisions\n\xff\n")
        cmp = base.compare(w.context("main"))
        self.assertFalse(cmp.complete)
        self.assertEqual([(r.label, r.commit, r.tip, r.reason, r.base, r.theirs) for r in cmp.repos], [
            (".", None, None, "governance/DECISIONS.md is not valid UTF-8 (byte 12)", [], []),
            ("beta", None, None, "main names no commit in this repository", [], [])])


class GitCalls(Case):
    """What the check costs a run, in git calls: recorded by a `git` on PATH."""

    def _named(self, w: Tree, calls: list[str]) -> list[str]:
        """Each call with the commit ids in it replaced by what they are."""
        for name, args in (("<head>", ("rev-parse", "HEAD")), ("<tip>", ("rev-parse", "main")),
                           ("<base>", ("merge-base", "main", "HEAD"))):
            calls = [call.replace(w.git(*args).strip(), name) for call in calls]
        return calls

    def test_an_ordinary_run_asks_three_things_and_reads_no_trap_file(self):
        w = self.solo()
        w.branch("work")
        w.append(TRAPS, trap(2))
        w.commit("ours")
        self.assertEqual(self._named(w, self.git_calls(w, "check")), [
            "rev-parse --verify HEAD^{commit}",
            "ls-tree -z <head> -- docs/",
            f"cat-file blob <head>:{LOG}"])

    def test_the_gate_reads_both_sides_of_the_log_and_of_the_trap_files(self):
        w = self.solo()
        w.branch("work")
        w.append(TRAPS, trap(2))
        w.commit("ours")
        w.switch("main")
        w.append(LOG, decision("P", 4))
        w.commit("theirs")
        w.switch("work")
        self.assertEqual(self._named(w, self.git_calls(w, "check", "--base", "main")), [
            "rev-parse --verify HEAD^{commit}",
            "rev-parse --is-shallow-repository",
            "rev-parse --verify main^{commit}",
            "merge-base <tip> HEAD",
            "ls-tree -z <base> -- docs/",
            f"cat-file blob <base>:{LOG}",
            "ls-tree -z <tip> -- docs/",
            f"cat-file blob <tip>:{LOG}",
            "ls-tree -r --name-only -z <base> -- docs/working",
            f"cat-file blob <base>:{TRAPS}",
            "ls-tree -r --name-only -z <tip> -- docs/working",
            f"cat-file blob <tip>:{TRAPS}"])

    def test_a_base_that_is_the_tip_is_read_once(self):
        # `--base HEAD`, or a branch that has merged the base in: one commit, asked once.
        w = self.solo()
        calls = self._named(w, self.git_calls(w, "check", "--base", "HEAD"))
        self.assertEqual([call for call in calls if not call.startswith(("rev-parse", "merge-base"))], [
            "ls-tree -z <head> -- docs/",
            f"cat-file blob <head>:{LOG}",
            "ls-tree -r --name-only -z <head> -- docs/working",
            f"cat-file blob <head>:{TRAPS}"])

    def test_a_log_that_is_not_at_the_base_costs_one_question_about_renames(self):
        w = self.solo(git="init")
        w.git("add", "-A")
        w.git("rm", "-q", "--cached", LOG)
        w.git("commit", "-qm", "before there was a log")
        self.assertEqual(self._named(w, self.git_calls(w, "check")), [
            "rev-parse --verify HEAD^{commit}",
            "ls-tree -z <head> -- docs/",
            "-c diff.autoRefreshIndex=false diff -M --name-status -z <head>"])


    def test_a_log_found_by_its_ids_costs_one_read_for_each_deleted_markdown_file(self):
        w = self.solo()
        w.write("notes/old.md", "# Old notes\n")
        w.write("notes/old.txt", "## P-1 - Not Markdown, so not read\n")
        w.commit("notes")
        w.git("rm", "-q", "notes/old.md", "notes/old.txt")
        w.git("mv", LOG, ReadingTheBase.RENAMED)
        w.write(layout.CONFIG, w.read(layout.CONFIG).replace(LOG, ReadingTheBase.RENAMED))
        w.append(ReadingTheBase.RENAMED, "".join(decision("P", n) for n in range(4, 56)))
        self.assertEqual(self._named(w, self.git_calls(w, "check")), [
            "rev-parse --verify HEAD^{commit}",
            "ls-tree -z <head> -- docs/",
            "-c diff.autoRefreshIndex=false diff -M --name-status -z <head>",
            f"cat-file blob <head>:{LOG}",
            "cat-file blob <head>:notes/old.md"])


class Fixture(Case):
    def test_a_trees_commit_ids_do_not_depend_on_the_time_zone(self):
        ids = set()
        for zone in ("UTC", "AAA-13", "BBB+8"):
            with self.subTest(zone=zone), self.fresh(), mock.patch.dict(os.environ, {"TZ": zone}):
                w = Tree(self.tmp / "tree", self.tmp / "home")
                w.write("a.md", "# A\n")
                w.init()
                w.git("add", "-A")
                w.git("commit", "-qm", "start")
                ids.add(w.git("rev-parse", "HEAD").strip())
        self.assertEqual(len(ids), 1, ids)


# ---------------------------------------------------------------------------- layouts

class Layouts(Case):
    def test_a_governance_root_below_the_git_top(self):
        w = self.solo(below="outer")
        self.assertTrue((w.top / ".git").is_dir() and not (w.root / ".git").exists())
        w.edit(LOG, *SettledDecisionChangedInPlace.EDIT)
        self.assertEqual(self.found(w), (0, [WARN + changed(LOG, "P-1")]))
        self.assertEqual(self.found(w, "--base", "main"), (1, [ERROR + changed(LOG, "P-1")]))
        cmp = base.compare(w.context())
        self.assertEqual([(r.label, r.root, r.path) for r in cmp.repos],
                         [(".", True, w.top.resolve())])

    def test_a_nested_project_repository_is_compared_with_its_own_head(self):
        w = self.team(nested=True)
        w.edit("beta/DECISIONS.md", "## B-200 - Title 200", "## B-200 - Another title")
        self.assertEqual(self.found(w), (0, [WARN + changed("beta/DECISIONS.md", "B-200")]))
        w.commit("reword", at=w.root / "beta")
        self.silent(w)

    def test_project_reports_that_projects_changes_only(self):
        w = self.team()
        w.edit("projects/alpha/DECISIONS.md", "## A-100 - Title 100", "## A-100 - Another title")
        w.edit("governance/DECISIONS.md", "## W-1 - Title 1", "## W-1 - Another title")
        alpha = changed("projects/alpha/DECISIONS.md", "A-100")
        ws = changed("governance/DECISIONS.md", "W-1")
        self.assertEqual(self.found(w, "--project", "alpha"), (0, [WARN + alpha]))
        self.silent(w, "--project", "gamma")
        self.assertEqual(self.found(w, "--project", "alpha", "--base", "main"), (1, [ERROR + alpha]))
        self.silent(w, "--project", "gamma", "--base", "main")
        self.assertEqual(self.found(w, "--base", "main"), (1, [ERROR + ws, ERROR + alpha]))

    def test_workspace_only_reports_the_workspaces_own_log_only(self):
        w = self.team()
        w.edit("projects/alpha/DECISIONS.md", "## A-100 - Title 100", "## A-100 - Another title")
        self.silent(w, "--workspace-only")
        self.silent(w, "--workspace-only", "--base", "main")
        w.edit("governance/DECISIONS.md", "## W-1 - Title 1", "## W-1 - Another title")
        ws = changed("governance/DECISIONS.md", "W-1")
        self.assertEqual(self.found(w, "--workspace-only"), (0, [WARN + ws]))
        self.assertEqual(self.found(w, "--workspace-only", "--base", "main"), (1, [ERROR + ws]))

    def _snapshot(self, w: Tree) -> Path:
        """Alpha's directory as `git checkout-index` leaves the staged tree: no `.git`."""
        snap = self.tmp / "snapshot"
        shutil.copytree(w.root / "projects/alpha", snap)
        return snap

    def test_a_snapshot_without_history_is_not_compared_and_that_fails_the_gate(self):
        w = self.team()
        snap = self._snapshot(w)
        log = snap / "DECISIONS.md"
        wtext(log, rtext(log).replace(REWORD[0], REWORD[1], 1))
        self.silent(w, "--project", "alpha", "--path", str(snap))
        # Asked for a gate, there is nothing to compare the snapshot with: not green.
        self.assertEqual(self.found(w, "--project", "alpha", "--path", str(snap), "--base", "main"),
                         (1, [ERROR + no_compare("main", "a snapshot needs --history-from to be "
                                                 "compared", repo="projects/alpha")]))

    def test_a_snapshot_is_compared_with_history_froms_head(self):
        w = self.team()
        snap = self._snapshot(w)
        flags = ("--project", "alpha", "--path", str(snap), "--history-from", "projects/alpha")
        self.silent(w, *flags)
        # An edit in the working tree only is not in the snapshot, so it is not reported.
        w.edit("projects/alpha/DECISIONS.md", "## A-100 - Title 100", "## A-100 - Unstaged")
        self.silent(w, *flags)
        log = snap / "DECISIONS.md"
        wtext(log, rtext(log).replace(REWORD[0], REWORD[1], 1))
        line = changed("projects/alpha/DECISIONS.md", "A-100")       # the real path, not the snapshot's
        self.assertEqual(self.found(w, *flags), (0, [WARN + line]))

    def test_a_snapshot_under_base_is_compared_with_the_merge_base_in_that_checkout(self):
        w = self.team()
        w.branch("work")
        w.edit("projects/alpha/DECISIONS.md", "## A-100 - Title 100", "## A-100 - Another title")
        w.commit("reword")
        snap = self._snapshot(w)
        flags = ("--project", "alpha", "--path", str(snap), "--history-from", "projects/alpha")
        self.silent(w, *flags)                                      # same as HEAD
        self.assertEqual(self.found(w, *flags, "--base", "main"),
                         (1, [ERROR + changed("projects/alpha/DECISIONS.md", "A-100")]))

    def test_a_snapshot_is_compared_with_the_projects_log_wherever_history_from_points(self):
        # `--history-from` names the checkout to ask: the repository's top, as a hook has it,
        # or the project's own directory inside it. Either way the file is the project's log.
        line = changed("projects/alpha/DECISIONS.md", "A-100")
        for history in (".", "projects/alpha"):
            with self.subTest(history=history), self.fresh():
                w = self.team()
                snap = self._snapshot(w)
                flags = ("--project", "alpha", "--path", str(snap), "--history-from", history)
                self.silent(w, *flags)
                self.silent(w, *flags, "--base", "HEAD")
                log = snap / "DECISIONS.md"
                wtext(log, rtext(log).replace(REWORD[0], REWORD[1], 1))
                self.assertEqual(self.found(w, *flags), (0, [WARN + line]))
                self.assertEqual(self.found(w, *flags, "--base", "HEAD"), (1, [ERROR + line]))
                # Given as a whole path, it is the same directory.
                whole = (*flags[:-1], str(w.root / history))
                self.assertEqual(self.found(w, *whole, "--base", "HEAD"), (1, [ERROR + line]))

    def test_a_snapshot_of_a_project_at_the_repository_top(self):
        w = self.solo()
        snap = self.tmp / "snapshot"
        shutil.copytree(w.root, snap, ignore=shutil.ignore_patterns(".git"))
        flags = ("--project", "solo", "--path", str(snap), "--history-from", ".")
        self.silent(w, *flags)
        wtext(snap / LOG, rtext(snap / LOG).replace(*SettledDecisionChangedInPlace.EDIT))
        self.assertEqual(self.found(w, *flags), (0, [WARN + changed(LOG, "P-1")]))
        self.assertEqual(self.found(w, *flags, "--base", "HEAD"),
                         (1, [ERROR + changed(LOG, "P-1")]))

    def test_a_snapshot_of_a_project_that_is_its_own_repository(self):
        # Asked of the workspace's repository, which does not track the project's files, the
        # log is still the one the project's own repository holds.
        line = changed("beta/DECISIONS.md", "B-200")
        for history in (".", "beta"):
            with self.subTest(history=history), self.fresh():
                w = self.team(nested=True)
                snap = self.tmp / "snapshot"
                shutil.copytree(w.root / "beta", snap, ignore=shutil.ignore_patterns(".git"))
                wtext(snap / "DECISIONS.md",
                      rtext(snap / "DECISIONS.md").replace(REWORD[0], REWORD[1], 1))
                flags = ("--project", "beta", "--path", str(snap), "--history-from", history)
                self.assertEqual(self.found(w, *flags), (0, [WARN + line]))
                self.assertEqual(self.found(w, *flags, "--base", "HEAD"), (1, [ERROR + line]))

    def test_a_snapshot_whose_history_is_no_repository_fails_the_gate_only(self):
        w = self.team()
        snap = self._snapshot(w)
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        flags = ("--project", "alpha", "--path", str(snap), "--history-from", str(elsewhere))
        self.silent(w, *flags)
        self.assertEqual(self.found(w, *flags, "--base", "main"),
                         (1, [ERROR + no_compare("main", "not a git repository",
                                                 repo="projects/alpha")]))

    def test_crlf_logs(self):
        w = self.solo()
        crlf = w.read(LOG).replace("\n", "\r\n")
        # The same log checked out with CRLF (git on Windows): nothing changed.
        w.write(LOG, crlf)
        self.silent(w)
        self.silent(w, "--base", "main")
        w.git("add", "-A")
        w.git("commit", "-qm", "crlf")
        self.assertIn(b"\r\n", w.git("show", f"HEAD:{LOG}").encode("utf-8"))
        self.assertEqual(crlf.count("\r\n\r\n## P-2"), 1)
        w.write(LOG, crlf.replace("in the room.\r\n\r\n## P-2", "in the room,\r\nor was.\r\n\r\n## P-2"))
        self.assertEqual(self.found(w), (0, [WARN + changed(LOG, "P-1")]))
        self.assertNotIn("\n", w.read(LOG).replace("\r\n", ""))
        w.edit(LOG, "Title 1\r\n\r\n**Status:** locked\r\n",
               "Title 1\r\n\r\n**Status:** locked\r\n\r\n**Revised:** 2026-10-06 (shorter)\r\n")
        self.silent(w)

    def test_a_log_mixing_hyphen_and_em_dash_headings(self):
        w = self.solo(log=fm() + "# Decisions\n" + decision("P", 1, sep=" — ") + decision("P", 2)
                      + decision("P", 3, sep="—") + "\n" + BLOCK)
        self.silent(w)
        self.silent(w, "--base", "main")
        w.edit(LOG, "## P-1 — Title 1", "## P-1 - Title 1")       # the dash is not text
        w.edit(LOG, "## P-3—Title 3", "## P-3 – Title 3")
        self.silent(w, "--base", "main")
        w.edit(LOG, "## P-3 – Title 3", "## P-3 – Title three")
        self.assertEqual(self.found(w, "--base", "main"), (1, [ERROR + changed(LOG, "P-3")]))

    def test_renamed_statuses_and_locked_statuses(self):
        extra = ('\n[checks.decision-log]\nstatuses = ["accepted", "proposed"]\n'
                 'reasons = { statuses = "the team\'s own words for them" }\n'
                 f'\n[checks.{CHECK}]\nlocked_statuses = ["Accepted"]\n')
        w = self.solo(extra=extra, log=fm() + "# Decisions\n" + decision("P", 1, "accepted")
                      + decision("P", 2, "proposed") + decision("P", 3, "locked") + "\n" + BLOCK)
        w.edit(LOG, "## P-2 - Title 2", "## P-2 - Another title")
        w.edit(LOG, "## P-3 - Title 3", "## P-3 - Another title")     # `locked` means nothing here
        for flags in ((), ("--base", "main")):
            _, out, _ = w.run("check", *flags)
            self.assertNotIn("changed without a new dated", out)
        w.edit(LOG, "## P-1 - Title 1", "## P-1 - Another title")
        _, out, _ = w.run("check")
        self.assertIn(WARN + changed(LOG, "P-1", "accepted"), out)
        _, out, _ = w.run("check", "--base", "main")
        self.assertIn(ERROR + changed(LOG, "P-1", "accepted"), out)
        self.assertEqual(out.count("changed without a new dated"), 1, out)

    def test_a_log_that_is_the_workspaces_and_the_projects_is_compared_once(self):
        w = self.solo(workspace='decision_log = "docs/DECISIONS.md"')
        w.edit(LOG, *SettledDecisionChangedInPlace.EDIT)
        self.assertEqual(self.found(w), (0, [WARN + changed(LOG, "P-1")]))
        self.assertEqual(self.found(w, "--project", "solo", "--base", "main"),
                         (1, [ERROR + changed(LOG, "P-1")]))

    def test_govern_base_alone_is_a_check(self):
        w = self.solo()
        w.edit(LOG, *SettledDecisionChangedInPlace.EDIT)
        code, out, _ = w.run("--base", "main")
        self.assertEqual(code, 1)
        self.assertIn(ERROR + changed(LOG, "P-1"), out)


# ---------------------------------------------------------------------------- the ordinary day

class OrdinaryDay(Case):
    """What one writer does all day, each of which must print nothing: uncommitted, committed,
    and on a branch a pull request gates."""

    def quiet(self, w: Tree, at: Path | None = None) -> None:
        self.silent(w)
        self.silent(w, "--base", "main")
        w.git("checkout", "-qb", "ordinary", at=at)
        w.commit("an ordinary change", at=at)
        self.silent(w)
        self.silent(w, "--base", "main")

    def test_rewrapping_a_paragraph(self):
        w = self.solo()
        w.edit(LOG, "## P-1 - Title 1\n\n**Status:** locked\n\n**Rule:** always write the rule "
                    "down before acting on it.\n",
               "## P-1 - Title 1\n\n**Status:** locked\n\n\n**Rule:** always write\nthe rule   down "
               "before\nacting on it.   \n\n")
        self.quiet(w)

    def test_changing_the_heading_dash(self):
        w = self.solo()
        w.edit(LOG, "## P-1 - Title 1", "## P-1 — Title 1")
        w.edit(LOG, "## P-3 - Title 3", "## P-3–Title 3")
        self.quiet(w)

    def test_adding_a_topic(self):
        w = self.solo()
        w.edit(LOG, "Title 1\n\n**Status:** locked\n", "Title 1\n\n**Topic:** Process\n\n**Status:** locked\n")
        self.quiet(w)

    def test_changing_a_topic(self):
        w = self.solo(log=fm() + "# Decisions\n" + decision("P", 1).replace(
            "**Status:**", "**Topic:** Process\n\n**Status:**") + "\n" + BLOCK)
        w.edit(LOG, "**Topic:** Process", "**Topic:** How we work")
        self.quiet(w)

    def test_index_rerendering_a_block_inside_a_locked_entry(self):
        w = self.solo()
        before = w.read(LOG)
        head, mark, block = before.partition("<!-- gov:generated:start")
        self.assertIn("P-3", head.rpartition("\n## ")[2])       # the block is in P-3's body
        w.write(LOG, head.rstrip("\n") + "\n" + decision("P", 4, "provisional") + "\n" + mark + block)
        code, out, _ = w.run("index")
        self.assertIn(f"regenerated {LOG} :: decision-index", out)
        # The block was in P-3's body, a locked entry; it is in P-4's now, and it changed.
        self.assertNotEqual(before.partition("<!--")[2], w.read(LOG).partition("<!--")[2])
        self.quiet(w)

    def test_installer_migrate_apply_on_a_log(self):
        old_shape = (fm() + "# Decisions\n" + decision("P", 1)
                     + decision("P", 2, "superseded", body="Superseded by P-1.\n")
                     + "\n## Networking (P-10-P-19)\n"
                     + decision("P", 10).replace("\n## ", "\n### ")
                     + decision("P", 11, "superseded", body="Superseded by P-10.\n").replace(
                         "\n## ", "\n### ")
                     + "\n" + BLOCK)
        w = self.solo(log=old_shape)
        res = subprocess.run(
            [sys.executable, "-m", "govern.installer", "migrate", "--root", str(w.root), "--apply"],
            env={**os.environ, "PYTHONPATH": str(ENGINE), "HOME": str(w.home),
                 "CONTEXT_GATE_NO_UPDATE_CHECK": "1"}, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        self.assertIn("migrated 1 file(s)", res.stdout)
        migrated = w.read(LOG)
        self.assertIn("## P-2 - Replaced by P-1\n", migrated)
        self.assertIn("## P-10 - Title 10\n\n**Topic:** Networking\n", migrated)
        self.assertIn("## P-11 - Replaced by P-10\n", migrated)
        self.quiet(w)

    def test_moving_a_decision_to_another_log(self):
        w = self.team()
        alpha = w.read("projects/alpha/DECISIONS.md")
        head, _, rest = alpha.partition("\n## A-100 - Title 100\n")
        body, _, tail = rest.partition("\n## A-101")
        w.write("projects/alpha/DECISIONS.md", head + "\n## A-101" + tail)
        w.append("governance/DECISIONS.md", "\n## A-100 - Title 100\n" + body)
        self.silent(w, "--project", "alpha")
        self.silent(w, "--project", "alpha", "--base", "main")
        self.quiet(w)

    def test_renaming_the_log_file(self):
        w = self.solo()
        w.git("mv", LOG, "docs/LOG.md")
        w.write(layout.CONFIG, w.read(layout.CONFIG).replace("docs/DECISIONS.md", "docs/LOG.md"))
        self.quiet(w)

    def test_adding_a_decision(self):
        w = self.solo()
        w.append(LOG, decision("P", 4) + decision("P", 5, "provisional"))
        self.quiet(w)

    def test_editing_a_provisional_decision(self):
        w = self.solo()
        w.edit(LOG, "## P-2 - Title 2\n\n**Status:** provisional\n\n**Rule:** always write the rule "
                    "down before acting on it.",
               "## P-2 - A better title\n\n**Status:** provisional\n\n**Rule:** write it down first.")
        self.quiet(w)

    def test_promoting_provisional_to_locked(self):
        w = self.solo()
        w.edit(LOG, "## P-2 - Title 2\n\n**Status:** provisional", "## P-2 - Title 2\n\n**Status:** locked")
        self.quiet(w)

    def test_adding_a_trap_and_a_new_trap_file(self):
        w = self.solo()
        code, out, _ = w.run("trap-add", "--project", "solo", "--title", "A new one",
                             "--bites", "late")
        self.assertEqual(code, 0, out)
        w.write("docs/working/traps-build.md",
                fm("working", status="active") + "# Build traps\n" + trap(3))
        self.quiet(w)

    def test_a_new_log_in_a_project_that_had_none(self):
        w = self.solo(git="init")
        w.git("add", "-A")
        w.git("rm", "-q", "--cached", LOG)
        w.git("commit", "-qm", "before there was a log")
        self.quiet(w)

    def test_a_nested_projects_ordinary_work(self):
        w = self.team(nested=True)
        w.append("beta/DECISIONS.md", decision("B", 202))
        w.edit("beta/DECISIONS.md", "## B-201 - Title 201\n\n**Status:** provisional",
               "## B-201 - Title 201\n\n**Status:** locked")
        self.silent(w)
        w.commit("ordinary", at=w.root / "beta")
        self.silent(w)


class OneWriter(Case):
    """The claim itself: a single-author repository of ordinary commits gets no extra line of
    any kind from this check, at any point in its history."""

    CLEAN = "[OK] workspace  (0 errors, 0 warnings)\n[OK] solo  (0 errors, 0 warnings)\n" \
            "[OK] total  (0 errors, 0 warnings)\n"

    def test_a_single_author_history_is_byte_identical_with_and_without_the_check(self):
        w = self.solo()
        steps = (
            lambda: w.append(LOG, decision("P", 4, "provisional")),
            lambda: w.edit(LOG, "## P-4 - Title 4", "## P-4 - Title four, reconsidered"),
            lambda: w.edit(LOG, "four, reconsidered\n\n**Status:** provisional",
                           "four, reconsidered\n\n**Status:** locked"),
            lambda: w.run("trap-add", "--project", "solo", "--title", "Another", "--bites", "soon"),
            lambda: w.edit(LOG, "## P-1 - Title 1\n\n**Status:** locked\n",
                           "## P-1 — Title 1\n\n**Topic:** Process\n\n**Status:** locked\n"),
            lambda: w.edit(LOG, "## P-3 - Title 3\n\n**Status:** locked\n", "## P-3 - A clearer "
                           "title\n\n**Status:** locked\n\n**Revised:** 2026-10-06 (the title)\n"),
        )
        for n, step in enumerate(steps):
            step()
            w.run("index")
            self.assertEqual(self.silent(w), (0, self.CLEAN, ""), f"dirty, after step {n}")
            w.commit(f"step {n}")
            self.assertEqual(self.silent(w), (0, self.CLEAN, ""), f"committed, after step {n}")
        self.assertEqual(w.git("rev-list", "--count", "HEAD").strip(), str(len(steps) + 1))

    def test_a_project_that_is_not_in_git_at_all(self):
        w = self.solo(git=None)
        w.append(LOG, decision("P", 4))
        w.edit(LOG, *SettledDecisionChangedInPlace.EDIT)
        w.run("index")
        self.silent(w)


class UpgradedProject(Case):
    """A project that upgrades with a locked decision edited and not yet committed: every way
    it already runs the gate (none of which passes `--base`) exits as it did before, with a
    warning at most."""

    def test_every_existing_invocation_exits_as_before(self):
        for report in ("per-scope", "aggregate"):
            with self.subTest(report=report), self.fresh():
                self._every_shape(report)

    def _every_shape(self, report: str) -> None:
        w = Team(self.tmp, nested=True)
        w.edit(layout.CONFIG, 'markers = "gov"', f'markers = "gov"\nreport = "{report}"')
        w.commit("how it reports")
        snap = self.tmp / "snapshot"
        for rel in ("governance/DECISIONS.md", "projects/alpha/DECISIONS.md", "beta/DECISIONS.md"):
            w.edit(rel, "\n\n**Status:** locked\n\n**Rule:** always", "\n\n**Status:** locked\n\n**Rule:** never")
        shutil.copytree(w.root / "projects/alpha", snap)
        shapes = {
            ("check",): 3,
            ("check", "--project", "alpha"): 1,
            ("--project", "alpha"): 1,
            ("check", "--repo", "beta"): 1,
            ("check", "--workspace-only"): 1,
            ("check", "--project", "alpha", "--path", str(snap)): 0,
            ("check", "--project", "alpha", "--path", str(snap),
             "--history-from", "projects/alpha"): 1,
        }
        for argv, warnings in shapes.items():
            with unregistered():
                before = w.run(*argv)
            after = w.run(*argv)
            self.assertEqual(before[0], 0, (argv, before))
            self.assertEqual((after[0], after[2]), (before[0], before[2]), argv)
            added = collections.Counter(after[1].splitlines()) - collections.Counter(
                before[1].splitlines())
            new = [line for line in added.elements() if not line.startswith("[")]
            self.assertEqual(len(new), warnings, (argv, new))
            self.assertTrue(all(line.startswith(WARN) and "(locked) changed without a new dated" in line
                                for line in new), (argv, new))

    def test_the_check_declares_itself_completely(self):
        chk = manifest.CHECKS[CHECK]
        self.assertEqual((chk.scope, chk.also_project, chk.since, chk.default, chk.origin),
                         ("workspace", True, "0.7.0", "error", "engine"))
        self.assertTrue(chk.summary and chk.question and chk.rationale)
        self.assertEqual({k: (p.type, p.default) for k, p in chk.params.items()},
                         {"locked_statuses": ("list", ["locked"])})
        w = self.solo()
        code, out, _ = w.run("explain", CHECK)
        self.assertEqual(code, 0)
        self.assertIn(f"{CHECK}  error (engine default)", out)
        self.assertIn('locked_statuses = ["locked"]  (engine default)', out)


if __name__ == "__main__":
    unittest.main()
