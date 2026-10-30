"""Ids from fetched branches: `next-id` and `trap-add` count what other developers pushed, and
`refs.team_size` counts who has been committing.

    python3 -m unittest discover -s engine/tests -k test_refs_ids
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import profile_guard  # noqa: E402,F401

from govern import refs  # noqa: E402
from base_support import LOG, TRAPS, Case, Tree, decision, trap, wtext  # noqa: E402
from team_support import TwoClones  # noqa: E402

B_TRAPS = "docs/working/traps-b.md"
DAYS = 10000   # fixture commits are dated 2020; git reads a date before 1970 as none


class Ids(TwoClones):
    def run_in(self, clone: Path, *argv: str) -> tuple[int, str, str]:
        return Tree(clone, self.seed.home).run(*argv)

    def b_adds(self, rel: str, text: str) -> None:
        """B puts `text` on a branch of its own and pushes it."""
        self.seed.git("checkout", "-q", "-b", "feature-b", at=self.b)
        path = self.b / rel
        old = path.read_text(encoding="utf-8") if path.exists() else ""
        wtext(path, old + text)
        self.commit(self.b, "b adds")
        self.push(self.b)

    def test_a_fetched_branch_id_is_counted(self):
        self.b_adds(LOG, decision("P", 4))
        self.fetch(self.a)
        code, out, err = self.run_in(self.a, "next-id", "--project", self.a.name)
        self.assertEqual((code, out.strip(), err), (0, "P-5", ""))

    def test_an_unfetched_branch_is_not_counted(self):
        self.b_adds(LOG, decision("P", 4))
        code, out, _ = self.run_in(self.a, "next-id", "--project", self.a.name)
        self.assertEqual((code, out.strip()), (0, "P-4"))

    def test_fetch_flag_fetches_first(self):
        self.b_adds(LOG, decision("P", 4))
        code, out, err = self.run_in(self.a, "next-id", "--project", self.a.name, "--fetch")
        self.assertEqual((code, out.strip(), err), (0, "P-5", ""))

    def test_fetch_without_a_reachable_remote_warns_and_goes_on(self):
        self.seed.git("remote", "set-url", "origin", str(self.tmp / "missing.git"), at=self.a)
        code, out, err = self.run_in(self.a, "next-id", "--project", self.a.name, "--fetch")
        self.assertEqual((code, out.strip()), (0, "P-4"))
        self.assertIn("warning: git fetch failed (", err)
        self.assertIn("counting the branches already fetched", err)

    def test_a_ref_that_cannot_be_read_warns_and_is_skipped(self):
        self.b_adds(LOG, decision("P", 4))
        self.fetch(self.a)
        ctx = Tree(self.a, self.seed.home).context()
        real = refs.base.read

        def broken(repo, ref, rel):
            raise refs.base.CannotCompare("it is not UTF-8")
        refs.base.read = broken
        try:
            used, warnings = refs.used_numbers(ctx, [self.a / LOG], "P")
        finally:
            refs.base.read = real
        self.assertEqual(used, {1, 2, 3})
        self.assertTrue(warnings and all(w.startswith("could not read refs/remotes/")
                                         and w.endswith("it is not UTF-8")
                                         for w in warnings), warnings)

    def test_trap_add_counts_a_fetched_trap_file(self):
        self.b_adds(B_TRAPS, "# Traps B\n" + trap(2))
        self.fetch(self.a)
        code, out, err = self.run_in(self.a, "trap-add", "--project", self.a.name, "--title", "New",
                                     "--bites", "it bites")
        self.assertEqual(code, 0, out + err)
        self.assertIn("T-3 appended", out)
        self.assertIn("## T-3 - New", (self.a / TRAPS).read_text(encoding="utf-8"))

    def test_trap_add_without_fetching_does_not_see_it(self):
        self.b_adds(B_TRAPS, "# Traps B\n" + trap(2))
        code, out, err = self.run_in(self.a, "trap-add", "--project", self.a.name, "--title", "New",
                                     "--bites", "it bites")
        self.assertEqual(code, 0, out + err)
        self.assertIn("T-2 appended", out)


class TeamSize(Case):
    def test_without_a_remote_the_answer_is_unchanged(self):
        w = self.solo()
        code, out, err = w.run("next-id", "--project", "solo")
        self.assertEqual((code, out.strip(), err), (0, "P-4", ""))

    def test_team_size(self):
        w = self.solo()
        self.assertEqual(refs.team_size(w.root, days=DAYS), 1)
        w.git("-c", "user.email=Other@Example.com", "commit", "--allow-empty", "-qm", "x")
        self.assertEqual(refs.team_size(w.root, days=DAYS), 2)
        w.git("-c", "user.email=other@example.com", "commit", "--allow-empty", "-qm", "y")
        self.assertEqual(refs.team_size(w.root, days=DAYS), 2)

    def test_team_size_is_zero_without_git(self):
        self.assertEqual(refs.team_size(self.tmp / "nowhere"), 0)


if __name__ == "__main__":
    unittest.main()
