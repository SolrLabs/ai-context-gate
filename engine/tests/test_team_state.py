"""The `team-state` block: handoffs with their owners, and the builds tracked in this checkout.

    python3 -m unittest discover -s engine/tests -k test_team_state
"""
from __future__ import annotations

import dataclasses
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import profile_guard  # noqa: E402,F401
from base_support import Case, Tree, fm, rtext, wtext  # noqa: E402
from build_support import ledger  # noqa: E402
from team_support import TwoClones  # noqa: E402
from govern import build, layout  # noqa: E402

INDEX = "docs/TEAM.md"
BLOCK = ("<!-- gov:generated:start id=team-state -->\n"
         "<!-- gov:generated:end id=team-state -->\n")
GLOB = 'handoff = "docs/handoff/*.md"'


def wire(w, *, fact: str = GLOB) -> None:
    """A `team-state` block in docs/TEAM.md, and the `handoff` fact after `[repo]`."""
    path = w.root / layout.CONFIG
    text = rtext(path).replace(
        "project = [", f'project = [{{ file = "{INDEX}", id = "team-state" }},\n           ', 1)
    wtext(path, text + (fact + "\n" if fact else ""))
    wtext(w.root / INDEX, fm("working", status="active") + "# Team\n\n" + BLOCK)


def handoff(w, name: str, body: str, owner: str = "") -> None:
    extra = {"owner": owner} if owner else {}
    wtext(w.root / "docs/handoff" / name, fm("working", status="active", **extra) + body)


def block(w) -> str:
    text = rtext(w.root / INDEX)
    return text.split("-->\n", 1)[1].rsplit("<!--", 1)[0].strip()


class TeamState(Case):
    def test_the_block_lists_each_handoff_with_owner_and_first_sentence(self):
        w = self.solo()
        wire(w)
        handoff(w, "ana.md", "\n# Handoff\n\nPhase 2 is closed. More follows.\n", "ana@example.com")
        handoff(w, "bo.md", "No full stop here\nsecond line\n")
        w.run("index")
        out = block(w)
        self.assertIn("| [`docs/handoff/ana.md`](handoff/ana.md) | ana@example.com "
                      "| Phase 2 is closed. |", out)
        self.assertIn("| [`docs/handoff/bo.md`](handoff/bo.md) | - | No full stop here |", out)
        self.assertLess(out.index("ana.md"), out.index("bo.md"))

    def test_the_block_lists_builds_tracked_here(self):
        w = self.solo()
        wire(w, fact="")
        led = dataclasses.replace(
            ledger(w.root, owner="a@example.com"), phase="t4-aware", branch="phase/t4-aware",
            path=w.root / build.BUILD_DIR / "t4-aware" / build.LEDGER)
        led.path.parent.mkdir(parents=True)
        led.path.write_bytes(build.encoded(led))
        w.git("add", "-A")
        w.run("index")
        self.assertIn("| t4-aware | phase/t4-aware | a@example.com |", block(w))

    def test_first_sentence_skips_comments_and_fences(self):
        w = self.solo()
        wire(w)
        handoff(w, "ana.md", "<!-- owner notes -->\n\n```\ncode here\n```\n\nReal line. More.\n")
        w.run("index")
        out = block(w)
        self.assertIn("| Real line. |", out)
        self.assertNotIn("owner notes", out)
        self.assertNotIn("code here", out)

    def test_owner_per_phase_and_branch(self):
        w = self.solo()
        wire(w, fact="")
        for base, branch, owner in ((w.root, "phase/t4-a", "a@example.com"),
                                    (w.root / "nested", "phase/t4-b", "b@example.com")):
            led = dataclasses.replace(
                ledger(w.root, owner=owner), phase="t4", branch=branch,
                path=base / build.BUILD_DIR / "t4" / build.LEDGER)
            led.path.parent.mkdir(parents=True)
            led.path.write_bytes(build.encoded(led))
        w.git("add", "-A")
        w.run("index")
        out = block(w)
        self.assertIn("| t4 | phase/t4-a | a@example.com |", out)
        self.assertIn("| t4 | phase/t4-b | - |", out)   # the nested ledger is not this root's

    def test_empty_tables_say_none(self):
        w = self.solo()
        wire(w, fact="")
        w.run("index")
        self.assertEqual(block(w),
                         "**Handoffs**\n\n_none_\n\n**Builds in this checkout**\n\n_none_")

    def test_index_then_check_is_green(self):
        w = self.solo()
        wire(w)
        handoff(w, "ana.md", "Done. Next.\n", "ana@example.com")
        code, out, _ = w.run("index")
        self.assertEqual(code, 0, out)
        code, out, _ = w.run("check")
        self.assertEqual(code, 0, out)
        self.assertNotIn("generated-blocks", out)


class Fetched(TwoClones):
    def test_a_fetched_build_is_not_listed(self):
        self.seed.git("checkout", "-qb", "phase/b-x", at=self.b)
        code, out, err = Tree(self.b, self.seed.home).run("build", "start", "b-x")
        self.assertEqual(code, 0, out + err)
        self.commit(self.b, "start b-x")
        self.push(self.b)
        self.fetch(self.a)
        self.assertEqual(build.open_builds(self.a)[0], [("b-x", "phase/b-x")])
        a = Tree(self.a, self.seed.home)
        wire(a, fact="")
        a.run("index")
        self.assertEqual(block(a),
                         "**Handoffs**\n\n_none_\n\n**Builds in this checkout**\n\n_none_")


class CloseBrief(unittest.TestCase):
    def test_close_brief_names_the_owner(self):
        text = build.close_brief(Path("."), ledger(Path("build"), owner="a@example.com"))
        self.assertIn("Phase owner: a@example.com. When the project keeps one handoff per owner, "
                      "update only the handoff whose frontmatter owner is this email.", text)

    def test_close_brief_without_owner_is_unchanged(self):
        self.assertNotIn("Phase owner", build.close_brief(Path("."), ledger(Path("build"))))


if __name__ == "__main__":
    unittest.main()
