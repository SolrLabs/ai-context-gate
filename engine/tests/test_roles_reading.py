"""The reading rule in every rendered role.

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
from govern import roles  # noqa: E402
from test_roles import rendered  # noqa: E402


class ReadingRule(unittest.TestCase):
    """`reading`: every role gets the one line on reading by range, from one constant."""

    def test_every_rendered_role_holds_the_reading_line_once(self):
        for role in ("architect", "code-reviewer", "developer", "qa-engineer",
                     "senior-developer", "technical-writer", "ux-reviewer"):
            with self.subTest(role=role):
                text = rendered(role)
                self.assertEqual(text.count(roles.READING), 1)
                self.assertNotIn("{{reading}}", text)


if __name__ == "__main__":
    unittest.main()
