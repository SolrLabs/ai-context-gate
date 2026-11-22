"""`unit_gate` in the rendered roles: what the developers and the build rules say about the
local suite under `scoped` and `full`.

    python3 -m unittest discover -s engine/tests -k roles_unit_gate
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

NO_FULL_GATE = ("Once, after your last commit, run `python3 .context-gate/bin/govern build gate` "
                "in your worktree and give its result line; run the local suite no other way.")
BOTH = ["ux-reviewer", "architect"]


class UnitGate(unittest.TestCase):
    """`unit_gate`: a unit runs its own tests, and the coordinator runs the local suite."""

    def test_scoped_has_the_developers_run_no_full_gate(self):
        for role in ("developer", "senior-developer"):
            with self.subTest(role=role):
                text = rendered(role, unit_gate="scoped")
                self.assertIn(NO_FULL_GATE, text)
                self.assertIn("A filter that matches nothing prints a pass: check the total "
                              "is not zero. " + NO_FULL_GATE, text)
                self.assertIn("`DONE`: everything in the brief is done and your unit's tests "
                              "are green.", text)
                self.assertNotIn("Run the local suite once", text)
                self.assertNotIn("run the local suite once", text)
                self.assertNotIn("the local suite is green", text)
                self.assertIn("3. `Gate:` the command you ran for your unit's tests and its "
                              "result line (the count is not zero), and the result line of "
                              "build gate.\n", text)

    def test_the_rules_hold_the_merge_gate_bullet_after_the_worktree_bullet(self):
        rules = rendered(roles.RULES, full_gate="make ci", unit_gate="scoped")
        self.assertIn(
            "; you merge unit branches into the phase branch.\n"
            "- After you merge a unit, `build unit set <id> done --branch <b>` accepts the "
            "unit's own gate when nothing outside the build folder and docs changed since it "
            "ran; otherwise run the local suite on the phase branch (`make ci`). A "
            "unit that turns it red is failed.\n- After each unit you merge, commit", rules)
        self.assertIn("on the phase branch. A unit that turns it red is failed.\n",
                      rendered(roles.RULES, unit_gate="scoped"))

    def test_the_roles_that_run_no_unit_gate_render_the_same_under_both(self):
        for role in ("architect", "code-reviewer", "qa-engineer", "technical-writer",
                     "ux-reviewer"):
            with self.subTest(role=role):
                self.assertEqual(rendered(role, extra_roles=BOTH, unit_gate="full"),
                                 rendered(role, extra_roles=BOTH, unit_gate="scoped"))

    def test_full_drops_the_scoped_text_from_the_developers_and_the_rules(self):
        for role in ("developer", "senior-developer", roles.RULES):
            with self.subTest(role=role):
                text = rendered(role, unit_gate="full")
                for gone in (roles.NO_FULL_GATE, roles.GATE_SCOPED, "After you merge a unit"):
                    self.assertNotIn(gone, text)
        for role in ("developer", "senior-developer"):
            with self.subTest(role=role):
                text = rendered(role, unit_gate="full")
                for kept in ("the local suite once, before you report",
                             "the local suite is green", roles.HANDBACK["Gate"]):
                    self.assertIn(kept, text)

    def test_full_renders_the_developers_with_the_full_gate(self):
        dev = rendered("developer", unit_gate="full", full_gate="make ci")
        self.assertIn("Run the local suite once, before you report (`make ci`).", dev)
        self.assertIn("`DONE`: everything in the brief is done and the local suite is green.", dev)
        senior = rendered("senior-developer", unit_gate="full", full_gate="make ci")
        self.assertIn("touches; run the local suite once, before you report (`make ci`).\n", senior)
        self.assertNotIn(NO_FULL_GATE, senior)
        self.assertIn("2. `Red:`", dev)
        self.assertIn("`Gate:` the local suite's result line. You ran it once, at the end.", dev)
        self.assertNotIn("After you merge a unit", rendered(roles.RULES, unit_gate="full"))

    def test_another_value_is_a_problem_that_names_it(self):
        found = roles.problems(defaults(unit_gate="sometimes"))
        self.assertEqual(len(found), 1)
        self.assertIn("sometimes", found[0])
        self.assertEqual(roles.problems(defaults(unit_gate="full")), [])
        self.assertEqual(roles.problems(defaults()), [])

    def test_the_technical_writer_renders_the_same_under_both(self):
        self.assertEqual(rendered("technical-writer", unit_gate="full"),
                         rendered("technical-writer", unit_gate="scoped"))

    def test_the_technical_writer_gate_line_is_the_check_result_under_every_unit_gate(self):
        want = "the result line of python3 .context-gate/bin/govern check."
        for gate in roles.UNIT_GATES:
            with self.subTest(unit_gate=gate):
                self.assertEqual(
                    roles.handback_texts("technical-writer", defaults(unit_gate=gate))["Gate"], want)

    def test_a_params_dict_with_scoped_reads_the_scoped_gate_line(self):
        self.assertEqual(roles.handback_texts("developer", {"handback_checks": [], "unit_gate": "scoped"})["Gate"],
                         roles.GATE_SCOPED)


PHASE_GATE = ("Do not run the local suite or build gate: the coordinator runs it once per "
              "phase, before review.")
FILTER = "A filter that matches nothing prints a pass: check the total is not zero."


class PhaseUnitGate(unittest.TestCase):
    """`unit_gate = "phase"`, the default: a unit runs its own tests; the local suite runs once
    per phase."""

    def test_the_developers_render_the_phase_text_by_default(self):
        for role in ("developer", "senior-developer"):
            with self.subTest(role=role):
                text = rendered(role)
                self.assertEqual(text, rendered(role, unit_gate="phase"))
                self.assertIn(FILTER + " " + PHASE_GATE, text)
                self.assertIn("`DONE`: everything in the brief is done and your unit's tests "
                              "are green.", text)
                self.assertIn("3. `Gate:` the command you ran for your unit's tests and its "
                              "result line (the count is not zero). The coordinator runs the "
                              "local suite once per phase.\n", text)
                self.assertNotIn(NO_FULL_GATE, text)
                self.assertNotIn("the result line of build gate", text)

    def test_the_fast_checks_render_as_under_scoped(self):
        for role in ("developer", "senior-developer"):
            with self.subTest(role=role):
                text = rendered(role, fast_checks=["make lint"])
                self.assertIn("`Fast checks:` run each once after your scoped tests and give "
                              "each result: `make lint`", text)

    def test_the_rules_hold_the_once_per_phase_bullet(self):
        rules = rendered(roles.RULES, full_gate="make ci")
        self.assertIn(
            "- Run the local suite once per phase (`make ci`), when every unit is merged and "
            "before review, and once after the fix pass. After you merge a unit, read its diff "
            "and run nothing else. When the run is red, find the unit by running it on the "
            "merge commits; that unit is failed.\n", rules)
        self.assertNotIn("After you merge a unit, `build unit set", rules)

    def test_phase_is_valid_and_the_default(self):
        self.assertEqual(roles.problems(defaults(unit_gate="phase")), [])
        self.assertEqual(roles.DEFAULT_UNIT_GATE, "phase")
        self.assertEqual(roles.handback_texts("developer", {"handback_checks": []})["Gate"],
                         roles.GATE_PHASE)
        found = roles.problems(defaults(unit_gate="never"))
        self.assertEqual(len(found), 1)
        self.assertIn("never", found[0])

    def test_the_other_roles_render_the_same_under_phase_and_scoped(self):
        for role in ("architect", "code-reviewer", "qa-engineer", "technical-writer",
                     "ux-reviewer"):
            with self.subTest(role=role):
                self.assertEqual(rendered(role, extra_roles=BOTH, unit_gate="phase"),
                                 rendered(role, extra_roles=BOTH, unit_gate="scoped"))
