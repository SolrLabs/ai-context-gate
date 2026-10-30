"""`govern renumber OLD NEW`: an entry takes another id and everything that cites it follows.

    python3 -m unittest discover -s engine/tests -k test_renumber
"""
from __future__ import annotations

import os
import stat
import sys
import unittest
from unittest import mock
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import profile_guard  # noqa: E402,F401

from base_support import BLOCK, LOG, TRAPS, Case, Tree, decision, fm  # noqa: E402
from team_support import TwoClones  # noqa: E402

DOC = "docs/note.md"


class Renumber(Case):
    def ids(self, w: Tree, rel: str = LOG) -> list[str]:
        return [ln.split(" - ")[0] for ln in w.read(rel).splitlines() if ln.startswith("## ")]

    def test_heading_pointer_and_citations_move(self):
        w = self.solo()
        w.write(DOC, fm() + "# Note\n\nSee P-2 and P-22, also P-002.\n")
        w.write(".claude/agents/x.md", "---\nname: x\ndescription: x\nmodel: opus\neffort: high\n"
                "omitClaudeMd: true\n---\nFollows P-2.\n")
        w.write(".context-gate/build/p/u/brief.md", "Read P-2 first.\n")
        w.write(LOG, w.read(LOG).replace(BLOCK, "") + "\n## P-9 - Replaced by P-2\n\n" + BLOCK)
        code, out, err = w.run("renumber", "P-2", "P-7")
        self.assertEqual(code, 0, out + err)
        self.assertIn("P-2 is now P-7", out)
        self.assertIn("  renumbered ", out)
        text = w.read(LOG)
        self.assertIn("## P-7 - Title 2", text)
        self.assertIn("## P-9 - Replaced by P-7", text)
        self.assertNotIn("## P-2 ", text)
        self.assertEqual(w.read(DOC), fm() + "# Note\n\nSee P-7 and P-22, also P-7.\n")
        self.assertIn("Follows P-7.", w.read(".claude/agents/x.md"))
        self.assertEqual(w.read(".context-gate/build/p/u/brief.md"), "Read P-7 first.\n")
        code, out, err = w.run("check")
        self.assertEqual(code, 0, out + err)

    def test_sorted_place(self):
        log = (fm() + "# Decisions\n" + "".join(decision("P", n) for n in (1, 2, 4, 5))
               + "\n" + BLOCK)
        w = self.solo(log=log)
        code, out, err = w.run("renumber", "P-4", "P-3")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.ids(w), ["## P-1", "## P-2", "## P-3", "## P-5"])
        w.write(LOG, log)
        code, out, err = w.run("renumber", "P-1", "P-6")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.ids(w), ["## P-2", "## P-4", "## P-5", "## P-6"])
        self.assertIn("## P-6 - Title 1", w.read(LOG))
        self.assertEqual(w.run("check")[0], 0)
        code, out, err = w.run("renumber", "P-6", "P-3")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.ids(w), ["## P-2", "## P-3", "## P-4", "## P-5"])
        self.assertEqual(w.run("check")[0], 0)

    def test_crlf_and_bom_survive(self):
        w = self.solo()
        path = w.root / LOG
        path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes().replace(b"\n", b"\r\n"))
        code, out, err = w.run("renumber", "P-3", "P-10")
        self.assertEqual(code, 0, out + err)
        data = path.read_bytes()
        self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(data.count(b"\n"), data.count(b"\r\n"))
        self.assertIn(b"## P-10 - Title 3", data)

    def test_refusals(self):
        w = self.solo()
        before = w.read(LOG)
        cases = [(("P-50", "P-51"), "P-50 is not an entry"),
                 (("P-1", "T-9"), "T-9 has another prefix than P-1"),
                 (("P-1", "P-1000"), "P-1000 is outside 1-999"),
                 (("P-1", "P-3"), "P-3 is already used")]
        for argv, message in cases:
            code, out, err = w.run("renumber", *argv)
            self.assertEqual(code, 1, (argv, out, err))
            self.assertIn(message, err)
        self.assertEqual(w.read(LOG), before)

    def test_a_trap_renumbers(self):
        w = self.solo()
        w.write(DOC, fm() + "# Note\n\nBeware T-1.\n")
        code, out, err = w.run("renumber", "T-1", "T-4", "--project", "solo")
        self.assertEqual(code, 0, out + err)
        self.assertIn("## T-4 - Trap 1", w.read(TRAPS))
        self.assertIn("Beware T-4.", w.read(DOC))
        self.assertEqual(w.run("check")[0], 0)


class RenumberTeam(Case):
    NOTE = "projects/{}/docs/note.md"
    TRAPS = "projects/{}/working-files/traps.md"

    def two_notes(self):
        w = self.team()
        for name in ("alpha", "gamma"):
            w.write(self.NOTE.format(name), fm() + "# Note\n\nBeware T-1.\n")
        return w

    def test_a_trap_leaves_another_projects_citations(self):
        w = self.two_notes()
        w.write(".context-gate/build/p/u/brief.md", "Read T-1 first.\n")
        code, out, err = w.run("renumber", "T-1", "T-5", "--project", "alpha")
        self.assertEqual(code, 0, out + err)
        self.assertIn("Beware T-5.", w.read(self.NOTE.format("alpha")))
        self.assertIn("## T-5 - Trap 1", w.read(self.TRAPS.format("alpha")))
        self.assertIn("Beware T-1.", w.read(self.NOTE.format("gamma")))
        self.assertIn("## T-1 - Trap 1", w.read(self.TRAPS.format("gamma")))
        self.assertEqual(w.read(".context-gate/build/p/u/brief.md"), "Read T-1 first.\n")
        self.assertIn("  left         .context-gate/build/p/u/brief.md:1: T-1 may be another "
                      "project's", out)

    def test_an_id_held_by_two_projects_needs_a_project(self):
        w = self.two_notes()
        before = w.read(self.TRAPS.format("alpha")), w.read(self.NOTE.format("gamma"))
        code, out, err = w.run("renumber", "T-1", "T-5")
        self.assertEqual(code, 1, out + err)
        self.assertEqual(out, "")
        self.assertTrue(err.startswith("error: T-1 is an entry in more than one file; name the "
                                       "project with --project"), err)
        self.assertEqual(before, (w.read(self.TRAPS.format("alpha")),
                                  w.read(self.NOTE.format("gamma"))))


class RenumberWrites(Case):
    def snapshot(self, w: Tree) -> dict:
        return {p.relative_to(w.root): p.read_bytes()
                for p in sorted(w.root.rglob("*.md")) if ".git" not in p.parts}

    def test_a_directory_that_cannot_be_written_changes_nothing(self):
        if os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0):
            self.skipTest("a directory mode does not block this writer here")
        w = self.solo()
        w.write(DOC, fm() + "# Note\n\nSee P-2.\n")
        w.write("docs/sub/more.md", fm() + "# More\n\nSee P-2.\n")
        before = self.snapshot(w)
        locked = w.root / "docs" / "sub"
        locked.chmod(stat.S_IRUSR | stat.S_IXUSR)
        try:
            code, out, err = w.run("renumber", "P-2", "P-7")
        finally:
            locked.chmod(stat.S_IRWXU)
        self.assertEqual(code, 1, out + err)
        self.assertIn("nothing was written", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(out, "")
        self.assertEqual(self.snapshot(w), before)

    def test_a_cited_file_that_is_not_utf8_changes_nothing(self):
        w = self.solo()
        w.write(DOC, fm() + "# Note\n\nSee P-2.\n")
        w.write("docs/bad.md", b"\xff\xfe See P-2.\n")
        before = self.snapshot(w)
        code, out, err = w.run("renumber", "P-2", "P-7")
        self.assertEqual(code, 1, out + err)
        self.assertIn("nothing was written", err)
        self.assertTrue(err.startswith("error: "), err)
        self.assertEqual(self.snapshot(w), before)

    def test_streams(self):
        w = self.solo()
        w.write(DOC, fm() + "# Note\n\nSee P-2.\n")
        with mock.patch("govern.refs.used_numbers", return_value=(set(), ["a ref was skipped"])):
            code, out, err = w.run("renumber", "P-2", "P-7")
        self.assertEqual(code, 0, out + err)
        self.assertIn("warning: a ref was skipped", err)
        self.assertNotIn("warning", out)
        self.assertIn("  renumbered ", out)
        self.assertIn("P-2 is now P-7", out)
        self.assertNotIn("renumbered", err)

    def test_ascending_is_the_settings_not_the_file(self):
        log = (fm() + "# Decisions\n" + "".join(decision("P", n) for n in (1, 2, 4, 5))
               + "\n" + BLOCK)
        w = self.solo(log=log, extra='\n[checks.decision-log]\nascending = false\n')
        code, out, err = w.run("renumber", "P-1", "P-6")
        self.assertEqual(code, 0, out + err)
        self.assertEqual([ln.split(" - ")[0] for ln in w.read(LOG).splitlines()
                          if ln.startswith("## ")], ["## P-6", "## P-2", "## P-4", "## P-5"])


class Fetched(TwoClones):
    def test_used_on_a_fetched_branch_is_refused(self):
        self.seed.git("checkout", "-q", "-b", "feature-b", at=self.b)
        path = self.b / LOG
        path.write_text(path.read_text(encoding="utf-8") + decision("P", 4),
                        encoding="utf-8", newline="")
        self.commit(self.b, "b adds")
        self.push(self.b)
        self.fetch(self.a)
        code, out, err = Tree(self.a, self.seed.home).run("renumber", "P-3", "P-4")
        self.assertEqual(code, 1, out + err)
        self.assertIn("P-4 is already used", err)


if __name__ == "__main__":
    unittest.main()
