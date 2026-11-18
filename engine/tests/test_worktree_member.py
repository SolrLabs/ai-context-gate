"""`govern check` from a linked worktree of a workspace member reads that worktree, not the
member's main checkout the registry names.

    python3 -m unittest discover -s engine/tests -k worktree_member
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import profile_guard  # noqa: E402,F401

from build_support import *  # noqa: E402,F401,F403
from base_support import BLOCK, Solo, Tree, decision, fm  # noqa: E402
from test_build_workspace import Space  # noqa: E402
from govern import cli  # noqa: E402

BAD = "no frontmatter\n"


class Worktrees(Case):
    def space(self) -> Space:
        """`alpha` has a clean decision log, committed; `beta` is left as the fixture makes it."""
        w = Space(self.tmp)
        w.write("alpha/DECISIONS.md", fm() + "# Decisions\n\n" + BLOCK + decision("A", 100))
        w.run("index")
        w.git("add", "-A", at=w.member("alpha"))
        w.git("commit", "-qm", "log", at=w.member("alpha"))
        self.w = w
        return w

    def worktree(self, w: Space, slug: str = "p1-u1") -> Path:
        top = w.root / ".claude" / "worktrees" / f"alpha-{slug}"
        w.git("worktree", "add", "-q", str(top), "-b", f"build/{slug}", at=w.member("alpha"))
        return top

    def run_from(self, w: Tree, cwd: Path, *argv: str) -> tuple[int, str, str]:
        with mock.patch.object(Path, "cwd", return_value=cwd):
            return w.run(*argv)


class FromAWorktree(Worktrees):
    def test_check_in_a_member_worktree_reads_the_worktree(self):
        w = self.space()
        top = self.worktree(w)
        w.write(".claude/worktrees/alpha-p1-u1/docs/bad.md", BAD)
        code, out, err = self.run_from(w, top, "check")
        self.assertEqual(code, 1, out + err)
        self.assertIn("bad.md", out)
        self.assertIn(f"checking alpha from the worktree {top.resolve()}", err)

    def test_check_in_a_member_worktree_ignores_the_main_checkout(self):
        w = self.space()
        top = self.worktree(w)
        w.write("alpha/docs/bad.md", BAD)
        code, out, err = self.run_from(w, top, "check")
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("bad.md", out)
        self.assertIn("checking alpha from the worktree", err)

    def test_project_naming_another_member_is_unchanged(self):
        w = self.space()
        top = self.worktree(w)
        code, out, err = self.run_from(w, top, "check", "--project", "beta")
        self.assertEqual(code, 1, out + err)
        self.assertIn("beta/DECISIONS.md", out)
        self.assertEqual(err, "")

    def test_a_single_repo_worktree_checks_itself(self):
        # Passes before the change too: a single repo's worktree has its own config, found from it.
        solo = Solo(self.tmp)
        wt = self.tmp / "solo-wt"
        solo.git("worktree", "add", "-q", str(wt), "-b", "work")
        tree = Tree(wt, self.tmp / "home")
        tree.write("docs/bad.md", BAD)
        code, out, err = self.run_from(tree, wt, "check")
        self.assertEqual(code, 1, out + err)
        self.assertIn("bad.md", out)
        self.assertNotIn("checking", err)

    def test_a_ratchet_entry_keeps_its_key_when_check_runs_from_the_worktree(self):
        w = self.space()
        w.write("alpha/DECISIONS.md", fm() + "# Decisions\n\n" + BLOCK + decision(
            "A", 100, body="**Rule:** " + "word " * 600 + "\n\n**Why:** x.\n"))
        w.run("index")
        w.git("add", "-A", at=w.member("alpha"))
        w.git("commit", "-qm", "long entry", at=w.member("alpha"))
        code, out, err = w.run("baseline", "--allow-raise", "--reason", "fixture")
        self.assertEqual(code, 0, out + err)
        top = self.worktree(w)
        code, out, err = self.run_from(w, top, "check", "--project", "alpha")
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("ratchet", out.lower())
        code, out, err = self.run_from(w, top, "check")
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("ratchet", out.lower())
        self.assertIn("checking alpha from the worktree", err)

    def test_worktree_member_is_none_outside_a_worktree(self):
        w = self.space()
        self.worktree(w)
        ctx = w.context()
        self.assertIsNone(cli._worktree_member(ctx, w.member("alpha")))
        self.assertIsNone(cli._worktree_member(ctx, w.root))
        self.assertIsNone(cli._worktree_member(ctx, w.root / "no-such-folder"))

    def test_worktree_member_names_the_member_and_the_worktree_gov(self):
        w = self.space()
        top = self.worktree(w)
        found = cli._worktree_member(w.context(), top)
        self.assertEqual(found, ("alpha", top.resolve(), top.resolve()))


REG = ("<!-- gov:generated:start id=registry -->\n"
       "<!-- gov:generated:end id=registry -->\n")
WS = 'workspace = [{ file = "README.md", id = "registry" }]\n'


class IndexFromAWorktree(Worktrees):
    def workspace(self) -> Space:
        w = Space(self.tmp, extra=WS)
        w.write("alpha/DECISIONS.md", fm() + "# Decisions\n\n" + BLOCK + decision("A", 100))
        w.write("beta/DECISIONS.md", fm() + "# Decisions\n\n" + BLOCK + decision("B", 200))
        w.write("README.md", REG)
        self.w = w
        return w

    def test_index_in_a_member_worktree_updates_the_worktree(self):
        w = self.space()
        top = self.worktree(w)
        log = top / "DECISIONS.md"
        log.write_text(log.read_text(encoding="utf-8") + decision("A", 101), encoding="utf-8")
        code, out, err = self.run_from(w, top, "index")
        self.assertEqual(code, 0, out + err)
        self.assertIn("A-101", log.read_text(encoding="utf-8").split("gov:generated:end")[0])
        self.assertIn(f"index: alpha from the worktree {top.resolve()}", err)
        self.assertEqual(w.git("status", "--porcelain", at=w.member("alpha")).strip(), "")

    def test_index_in_a_member_worktree_leaves_workspace_blocks(self):
        w = self.workspace()
        w.run("index")
        w.git("add", "-A", at=w.member("alpha"))
        w.git("commit", "-qm", "log", at=w.member("alpha"))
        top = self.worktree(w)
        stale = REG.replace("-->\n", "-->\nstale\n", 1)
        w.write("README.md", stale)
        log = top / "DECISIONS.md"
        log.write_text(log.read_text(encoding="utf-8") + decision("A", 101), encoding="utf-8")
        code, out, err = self.run_from(w, top, "index")
        self.assertEqual(code, 0, out + err)
        self.assertEqual((w.root / "README.md").read_text(encoding="utf-8"), stale)
        self.assertIn("workspace blocks are left alone", err)

    def test_index_outside_a_worktree_is_unchanged(self):
        w = self.workspace()
        code, out, err = self.run_from(w, w.root, "index")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(err, "")
        self.assertIn("regenerated README.md :: registry", out)
        self.assertIn("regenerated alpha", out.replace("\\", "/"))


if __name__ == "__main__":
    unittest.main()
