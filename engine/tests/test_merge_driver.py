"""The merge driver: decision logs and trap files merge by entry id, generated blocks take ours.
`merging.drive` on three temp files, and once through git itself in two clones.

    python3 -m unittest discover -s engine/tests -k test_merge_driver
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import profile_guard  # noqa: E402,F401

from govern import merging  # noqa: E402
from base_support import LOG, TRAPS, Case, decision, rtext, trap, wtext  # noqa: E402
from team_support import TwoClones  # noqa: E402

LOG_RULE = "**Rule:** always write the rule down before acting on it."


class Drive(Case):
    def setUp(self) -> None:
        super().setUp()
        self.w = self.solo()
        self.base = self.w.read(LOG)
        self._old_cwd = os.getcwd()
        os.chdir(self.w.root)

    def tearDown(self) -> None:
        os.chdir(self._old_cwd)      # the folder cannot be deleted on Windows while we are in it
        super().tearDown()

    def test_the_temp_folder_is_not_the_current_directory_when_it_is_deleted(self):
        real, seen = self._tmp.cleanup, []

        def cleanup() -> None:
            seen.append(os.getcwd())
            real()
        self._tmp.cleanup = cleanup

        def after() -> None:
            self.assertEqual(len(seen), 1)
            self.assertFalse(Path(seen[0]).resolve().is_relative_to(self.tmp.resolve()), seen)
        self.addCleanup(after)

    def drive(self, base: str, ours: str, theirs: str, rel: str = LOG) -> tuple[int, str]:
        o, a, b = (self.tmp / n for n in ("o", "a", "b"))
        for path, text in ((o, base), (a, ours), (b, theirs)):
            wtext(path, text)
        code = merging.drive(self.w.context(), o, a, b, rel)
        return code, rtext(a)

    def with_entry(self, text: str, entry: str) -> str:
        """`entry` appended, as `next-id` and a hand edit both do."""
        return text + entry

    def test_both_add_different_entries_keeps_both_sorted(self):
        ours = self.with_entry(self.base, decision("P", 4))
        theirs = self.with_entry(self.base, decision("P", 5))
        code, text = self.drive(self.base, theirs, ours)    # ours holds P-5, theirs P-4
        self.assertEqual(code, 0, text)
        self.assertIn("## P-4 - Title 4", text)
        self.assertIn("## P-5 - Title 5", text)
        self.assertLess(text.index("## P-4 - "), text.index("## P-5 - "))
        self.assertNotIn("<<<<<<<", text)

    def test_one_revises_other_adds(self):
        ours = self.base.replace(LOG_RULE, "**Rule:** never write the rule down.", 1)
        theirs = self.with_entry(self.base, decision("P", 4))
        code, text = self.drive(self.base, ours, theirs)
        self.assertEqual(code, 0, text)
        self.assertIn("never write the rule down", text)
        self.assertIn("## P-4 - Title 4", text)

    def test_both_revise_one_entry_conflicts_inside_it_only(self):
        ours = self.base.replace("## P-2 - Title 2\n", "## P-2 - Title 2\n\nours line\n", 1)
        theirs = self.base.replace("## P-2 - Title 2\n", "## P-2 - Title 2\n\ntheirs line\n", 1)
        code, text = self.drive(self.base, ours, theirs)
        self.assertEqual(code, 1)
        lo, hi = text.index("## P-2 - "), text.index("## P-3 - ")
        for marker in ("<<<<<<< ours", "=======", ">>>>>>> theirs"):
            self.assertEqual(text.count(marker), 1, text)
            self.assertTrue(lo < text.index(marker) < hi, text)

    def test_same_new_id_names_it(self):
        ours = self.with_entry(self.base, decision("P", 4, title="Ours"))
        theirs = self.with_entry(self.base, decision("P", 4, title="Theirs"))
        code, text = self.drive(self.base, ours, theirs)
        self.assertEqual(code, 1)
        self.assertIn("<!-- context-gate: P-4 was added on both branches; keep one and give "
                      "the other a new id with govern renumber -->", text)
        self.assertIn("Ours", text)
        self.assertIn("Theirs", text)
        self.assertIn("<<<<<<< ours", text)

    def test_blocks_differing_on_both_sides_take_ours(self):
        start = "<!-- gov:generated:start id=decision-index -->\n"
        end = "<!-- gov:generated:end id=decision-index -->"
        i, j = self.base.index(start) + len(start), self.base.index(end)
        def with_block(content: str) -> str:
            return self.base[:i] + content + self.base[j:]
        code, text = self.drive(with_block("base row\n"), with_block("ours row\n"),
                                with_block("theirs row\n"))
        self.assertEqual(code, 0, text)
        self.assertEqual(text, with_block("ours row\n"))

    def test_a_non_log_file_with_a_block_merges_plainly(self):
        def readme(first: str, last: str, row: str) -> str:
            return (f"# Readme\n\n{first}\n\nmiddle\n\n{last}\n\n"
                    "<!-- gov:generated:start id=rows -->\n" + row +
                    "<!-- gov:generated:end id=rows -->\n")
        code, text = self.drive(readme("one", "two", "base\n"), readme("ONE", "two", "ours\n"),
                                readme("one", "TWO", "theirs\n"), rel="README.md")
        self.assertEqual(code, 0, text)
        self.assertEqual(text, readme("ONE", "TWO", "ours\n"))

    def test_a_trap_file_merges_by_entry_too(self):
        base = self.w.read(TRAPS)
        ours = base + trap(2)
        theirs = base + trap(3)
        code, text = self.drive(base, ours, theirs, rel=TRAPS)
        self.assertEqual(code, 0, text)
        self.assertLess(text.index("## T-2 - "), text.index("## T-3 - "))

    def test_a_trap_file_added_on_one_branch_only_counts(self):
        new = "docs/working/traps-extra.md"
        code, text = self.drive("", trap(1), trap(2), rel=new)
        self.assertEqual(code, 0, text)
        self.assertLess(text.index("## T-1 - "), text.index("## T-2 - "))

    def test_crlf_stays_crlf(self):
        crlf = self.base.replace("\n", "\r\n")
        ours = self.with_entry(crlf, decision("P", 4).replace("\n", "\r\n"))
        theirs = self.with_entry(crlf, decision("P", 5).replace("\n", "\r\n"))
        code, text = self.drive(crlf, ours, theirs)
        self.assertEqual(code, 0)
        self.assertNotRegex(text, r"[^\r]\n")

    def test_without_a_context_git_merges_the_whole_file(self):
        o, a, b = (self.tmp / n for n in ("o", "a", "b"))
        wtext(o, "1\n2\n3\n")
        wtext(a, "1\nA\n3\n")
        wtext(b, "1\n2\n3\nB\n")
        self.assertEqual(merging.drive(None, o, a, b, "x.md"), 0)
        self.assertEqual(rtext(a), "1\nA\n3\nB\n")
        wtext(a, "1\nA\n3\n")
        wtext(b, "1\nB\n3\n")
        self.assertEqual(merging.drive(None, o, a, b, "x.md"), 1)

    def test_a_deleted_entry_the_other_side_revised_conflicts(self):
        ours = self.base.replace(decision("P", 2, "provisional")[1:], "", 1)
        theirs = self.base.replace("provisional", "locked", 1)
        code, text = self.drive(self.base, ours, theirs)
        self.assertEqual(code, 1)
        self.assertIn("<<<<<<< ours", text)

    def test_the_command_takes_four_arguments(self):
        code, out, err = self.w.run("merge-driver", "a", "b")
        self.assertEqual(code, 2)
        self.assertIn("O A B P", err)


class Split(Case):
    def test_split_round_trips(self):
        w = self.solo()
        grammar = w.context().grammar
        text = "# Decisions\n\n## P-1 - A\n\nx\n\n## P-2 - B\n\ny\n\n## Index\n\nz\n"
        head, entries, tail = merging.split(text, grammar)
        self.assertEqual(head, "# Decisions\n\n")
        self.assertEqual(entries, {"P-1": "## P-1 - A\n\nx\n\n", "P-2": "## P-2 - B\n\ny\n\n"})
        self.assertEqual(tail, "## Index\n\nz\n")
        for sample in (text, w.read(LOG), w.read(TRAPS), "no entries\n", ""):
            head, entries, tail = merging.split(sample, grammar)
            self.assertEqual(head + "".join(entries.values()) + tail, sample)


class Separators(Case):
    """Entries merge with their trailing blank lines stripped and join with one blank line."""

    def setUp(self) -> None:
        super().setUp()
        self.w = self.solo()
        self._old_cwd = os.getcwd()
        os.chdir(self.w.root)

    def tearDown(self) -> None:
        os.chdir(self._old_cwd)      # the folder cannot be deleted on Windows while we are in it
        super().tearDown()

    drive = Drive.drive

    def test_appends_to_a_log_without_a_final_newline(self):
        o = "# D\n\n## P-1 - A\n\nx"
        code, text = self.drive(o, o + "\n\n## P-2 - B\n\ny", o + "\n\n## P-3 - C\n\nz")
        self.assertEqual(code, 0, text)
        self.assertEqual(text, "# D\n\n## P-1 - A\n\nx\n\n## P-2 - B\n\ny\n\n## P-3 - C\n\nz")

    def test_an_edit_to_the_last_line_and_an_append_do_not_conflict(self):
        o = "# D\n\n## P-1 - A\n\nx\n\n## P-2 - B\n\ny\n"
        code, text = self.drive(o, o.replace("\ny\n", "\nyy\n"), o + "\n## P-3 - C\n\nz\n")
        self.assertEqual(code, 0, text)
        self.assertEqual(
            text, "# D\n\n## P-1 - A\n\nx\n\n## P-2 - B\n\nyy\n\n## P-3 - C\n\nz\n")

    def test_appends_with_different_blank_lines_do_not_conflict(self):
        o = "# D\n\n## P-1 - A\n\nx\n\n## P-2 - B\n\ny\n"
        code, text = self.drive(o, o + "\n## P-3 - C\n\nz\n", o + "\n\n## P-4 - D\n\nw\n")
        self.assertEqual(code, 0, text)
        self.assertNotIn("<<<<<<<", text)
        self.assertIn("## P-3 - C\n\nz\n\n## P-4 - D\n\nw\n", text)

    def test_crlf_joins_with_crlf(self):
        o = "# D\r\n\r\n## P-1 - A\r\n\r\nx\r\n"
        code, text = self.drive(o, o + "\r\n## P-2 - B\r\n\r\ny\r\n",
                                o + "\r\n## P-3 - C\r\n\r\nz\r\n")
        self.assertEqual(code, 0, text)
        self.assertNotRegex(text, r"[^\r]\n")
        self.assertIn("y\r\n\r\n## P-3", text)

    def test_a_tail_starts_after_a_blank_line(self):
        o = "# D\n\n## P-1 - A\n\nx\n\n## Index\n\nt\n"
        code, text = self.drive(o, o.replace("x\n", "x\n\n## P-2 - B\n\ny\n", 1),
                                o.replace("x\n", "x\n\n## P-3 - C\n\nz\n", 1))
        self.assertEqual(code, 0, text)
        self.assertEqual(text, "# D\n\n## P-1 - A\n\nx\n\n## P-2 - B\n\ny\n\n"
                               "## P-3 - C\n\nz\n\n## Index\n\nt\n")


class Order(Case):
    """Ours' order stays; an entry only theirs has goes in place by id, or at the end."""

    def setUp(self) -> None:
        super().setUp()
        self.w = self.solo()
        self._old_cwd = os.getcwd()
        os.chdir(self.w.root)

    def tearDown(self) -> None:
        os.chdir(self._old_cwd)      # the folder cannot be deleted on Windows while we are in it
        super().tearDown()

    drive = Drive.drive

    @staticmethod
    def log(*ids: str) -> str:
        return "# D\n\n" + "".join(f"## {i} - T\n\nbody {i}\n\n" for i in ids)

    @staticmethod
    def heads(text: str) -> list[str]:
        return [line.split()[1] for line in text.splitlines() if line.startswith("## ")]

    def test_ascending_inserts_in_place(self):
        o = self.log("P-1", "P-3")
        code, text = self.drive(o, self.log("P-1", "P-3", "P-5"), self.log("P-1", "P-2", "P-3"))
        self.assertEqual(code, 0, text)
        self.assertEqual(self.heads(text), ["P-1", "P-2", "P-3", "P-5"])

    def test_descending_stays_descending(self):
        o = self.log("P-3", "P-2", "P-1")
        code, text = self.drive(o, o, self.log("P-4", "P-3", "P-2", "P-1"))
        self.assertEqual(code, 0, text)
        self.assertEqual(self.heads(text), ["P-4", "P-3", "P-2", "P-1"])

    def test_an_unordered_file_keeps_ours_and_appends_theirs(self):
        o = self.log("T-2", "T-1", "T-3")
        code, text = self.drive(o, o, self.log("T-2", "T-1", "T-3", "T-0"))
        self.assertEqual(code, 0, text)
        self.assertEqual(self.heads(text), ["T-2", "T-1", "T-3", "T-0"])
        code, text = self.drive(o, o, self.log("T-2", "T-9", "T-1", "T-3"))
        self.assertEqual(self.heads(text), ["T-2", "T-1", "T-3", "T-9"])


class FencedBlocks(Case):
    EX = ("<!-- gov:generated:start id=decision-index -->\n"
          "{}"
          "<!-- gov:generated:end id=decision-index -->\n")

    def setUp(self) -> None:
        super().setUp()
        self.w = self.solo()
        self._old_cwd = os.getcwd()
        os.chdir(self.w.root)

    def tearDown(self) -> None:
        os.chdir(self._old_cwd)      # the folder cannot be deleted on Windows while we are in it
        super().tearDown()

    drive = Drive.drive

    def doc(self, quoted: str, row: str, fence: str = "```") -> str:
        return (f"# Doc\n\nExample:\n\n{fence}\n{self.EX.format(quoted)}{fence}\n\n"
                f"{self.EX.format(row)}")

    def test_a_marker_in_a_fence_is_text(self):
        for fence in ("```", "~~~"):
            o = self.doc("q\n", "base\n", fence)
            code, text = self.drive(o, self.doc("q\n", "ours\n", fence),
                                    self.doc("q edited\n", "base\n", fence), rel="README.md")
            self.assertEqual(code, 0, text)
            self.assertEqual(text, self.doc("q edited\n", "ours\n", fence))


class Prefix(unittest.TestCase):
    def test_a_prefix_with_a_capital_x(self):
        from types import SimpleNamespace
        from govern.text import Markers
        ctx = SimpleNamespace(markers=Markers("XGOV"))
        text = ("a\n<!-- XGOV:generated:start id=idx -->\nrows\n"
                "<!-- XGOV:generated:end id=idx -->\nb\n")
        emptied, held = merging.empty_blocks(ctx, text)
        self.assertEqual(held, {"idx": "rows\n"})
        self.assertEqual(merging.refill_blocks(ctx, emptied, held), text)


class Git(TwoClones):
    def test_two_clones_merge_without_conflict(self):
        driver = f'"{sys.executable}" -m govern merge-driver %O %A %B %P'
        for clone in (self.a, self.b):
            self.seed.git("config", "merge.context-gate.driver", driver, at=clone)
            wtext(clone / ".gitattributes", f"{LOG} merge=context-gate\n")
            self.commit(clone, "attributes")
        self.seed.git("push", "-q", "origin", "HEAD:main", at=self.a)
        self.fetch(self.b)
        self.seed.git("reset", "-q", "--hard", "origin/main", at=self.b)
        for clone, num in ((self.a, 4), (self.b, 5)):
            path = clone / LOG
            wtext(path, rtext(path) + decision("P", num))
            self.commit(clone, f"add P-{num}")
        self.push(self.b)
        self.fetch(self.a)
        res = self.merge(self.a, "origin/main", PYTHONPATH=str(ENGINE))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        text = rtext(self.a / LOG)
        self.assertIn("## P-4 - Title 4", text)
        self.assertIn("## P-5 - Title 5", text)
        self.assertNotIn("<<<<<<<", text)


if __name__ == "__main__":
    unittest.main()
