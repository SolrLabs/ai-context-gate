"""A role context-gate began rendering after a project may have written it by hand: the
`architect`, skipped with a note when the project's own file is there.

    python3 -m unittest discover -s engine/tests -k roles
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))

sys.path.insert(0, str(Path(__file__).resolve().parent))

import profile_guard  # noqa: E402,F401

import govern.checks  # noqa: E402,F401  (registers the built-in checks)
from test_roles import AGENTS, BY_HAND, ON, raw, rendered  # noqa: E402
from test_single_repo import Base, SingleRepo  # noqa: E402


class HandMade(Base):
    def on(self, more: str = "", **kw) -> SingleRepo:
        return self.repo(extra=ON + more, **kw)

    def test_a_hand_made_architect_is_skipped_with_a_note_not_a_conflict(self):
        r = self.on()
        own = BY_HAND.replace("developer", "architect")
        r.write(f"{AGENTS}/architect.md", own)
        before = raw(r.root / AGENTS / "architect.md")
        for args in (("agents", "render"), ("agents", "render", "--check")):
            code, out, err = r.run(*args)
            self.assertIn("the `architect` role is not rendered", out)
            self.assertIn(f"{AGENTS}/architect.md", out)
            self.assertNotIn("conflict", out)
        r.run("agents", "render")
        code, out, _ = r.run("agents", "render", "--check")
        self.assertEqual(code, 0, out)
        code, out, _ = r.run("check")
        self.assertNotIn("architect", out)
        self.assertEqual(raw(r.root / AGENTS / "architect.md"), before)

    def test_a_hand_made_architect_named_in_extra_roles_is_still_a_conflict(self):
        r = self.on('extra_roles = ["architect"]\n')
        r.write(f"{AGENTS}/architect.md", BY_HAND.replace("developer", "architect"))
        code, out, _ = r.run("agents", "render")
        self.assertEqual(code, 1, out)
        self.assertIn("conflict", out)

    def test_a_hand_made_developer_is_still_a_conflict(self):
        r = self.on()
        r.write(f"{AGENTS}/developer.md", BY_HAND)
        code, out, _ = r.run("agents", "render")
        self.assertEqual(code, 1, out)
        self.assertIn("conflict", out)


class QaEngineerGivenNoDiff(unittest.TestCase):
    def test_the_rendered_qa_engineer_says_it_is_given_no_diff(self):
        text = rendered("qa-engineer")
        self.assertIn("You are given no diff: open the files you probe.", text)
        self.assertNotIn("full diff", text)


if __name__ == "__main__":
    unittest.main()
