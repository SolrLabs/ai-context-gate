"""`agent-roles` params `fast_checks` and `worktree_setup`.

    python3 -m unittest discover -s engine/tests -k roles_fast_checks
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))

import profile_guard  # noqa: E402,F401

import govern.checks  # noqa: E402,F401  (registers the built-in checks)
from govern import roles  # noqa: E402
from test_roles import defaults, rendered  # noqa: E402

CODE = ("developer", "senior-developer")
FAST = ["python3 -m unittest discover -s tests -k lint", "make guard"]
SETUP = "make setup"
LINE = ("Run `make setup` in your worktree once, right after the Base merge and before the "
        "first edit; if it fails, stop and report.")


class FastChecks(unittest.TestCase):
    def test_the_code_roles_carry_the_line_directly_before_not_verified(self):
        for role in CODE:
            with self.subTest(role=role):
                keys = roles.handback_keys(role, defaults(fast_checks=FAST))
                self.assertEqual(keys[keys.index("Not verified") - 1], "Fast checks")
                text = rendered(role, fast_checks=FAST)
                self.assertIn("`Fast checks:` run each once after your scoped tests and give "
                              "each result: `python3 -m unittest discover -s tests -k lint`, "
                              "`make guard`\n", text)
                self.assertLess(text.index("`Fast checks:`"), text.index("`Not verified:`"))

    def test_it_follows_the_operators_own_lines(self):
        keys = roles.handback_keys("developer", defaults(fast_checks=FAST,
                                                         handback_checks=["Licences: x"]))
        self.assertEqual(keys[-4:-1], ["Licences", "Fast checks", "Not verified"])

    def test_the_other_roles_do_not_carry_it(self):
        for role in ("technical-writer", "code-reviewer"):
            self.assertNotIn("Fast checks", rendered(role, fast_checks=FAST))

    def test_empty_or_full_gate_renders_none(self):
        for over in ({}, {"fast_checks": [], "unit_gate": "scoped"},
                     {"fast_checks": FAST, "unit_gate": "full"}):
            for role in CODE:
                self.assertNotIn("Fast checks", rendered(role, **over))

    def test_an_empty_entry_is_a_problem(self):
        self.assertEqual(roles.problems(defaults(fast_checks=["make a", ""])),
                         ["[checks.agent-roles] fast_checks[1] = '': expected a command"])

    def test_the_key_is_the_engines_so_an_operator_cannot_reuse_it(self):
        self.assertEqual(len(roles.problems(defaults(handback_checks=["Fast checks: x"]))), 1)


class WorktreeSetup(unittest.TestCase):
    def test_the_line_renders_in_the_three_templates(self):
        for role in (*CODE, "technical-writer"):
            with self.subTest(role=role):
                text = rendered(role, worktree_setup=SETUP)
                self.assertEqual(text.count(LINE), 1)
                self.assertNotIn("{{", text)

    def test_the_line_sits_after_the_worktree_step_and_before_the_brief(self):
        for role in (*CODE, "technical-writer"):
            with self.subTest(role=role):
                text = rendered(role, worktree_setup=SETUP)
                self.assertLess(text.index("`Worktree:` line"), text.index(LINE))
                self.assertLess(text.index(LINE), text.index("`Brief:` line. "))

    def test_empty_renders_nothing_and_no_blank_line(self):
        for role in (*CODE, "technical-writer"):
            with self.subTest(role=role):
                plain = rendered(role)
                self.assertNotIn("worktree once", plain)
                self.assertNotIn("\n\n\n", plain)
        self.assertIn("Commit and write your report only there.\n- Your launch prompt names",
                      rendered("developer"))

    def test_a_newline_is_a_problem(self):
        self.assertEqual(roles.problems(defaults(worktree_setup="a\nb")),
                         ["[checks.agent-roles] worktree_setup: one line only"])
        self.assertEqual(roles.problems(defaults(worktree_setup=SETUP)), [])


if __name__ == "__main__":
    unittest.main()
