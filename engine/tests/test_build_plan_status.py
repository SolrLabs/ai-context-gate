"""`govern build status` compares the units added with the units the plan names.

    python3 -m unittest discover -s engine/tests -k test_build_plan_status
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import profile_guard  # noqa: E402,F401

from build_support import *  # noqa: E402,F401,F403

PLAN = """# Plan

## Phase 1: earlier (slug: p0; high-risk: no)

### u1: Not this phase's unit  (developer)

## Phase 2: the one built (slug: p1; high-risk: yes; checkpoint: no)

### u1: First  (developer)

### u2: Second  (developer)

### u3: Third  (developer)

### u10: Tenth  (developer)

## Release 1.0 (coordinator)

### u99: After the phase
"""


class PlannedUnits(Build):
    def with_plan(self, text: str = PLAN) -> None:
        w = self.project()
        w.write("docs/plan.md", text)
        w.commit("plan", at=w.top, index=False)
        self.ok("start", "p1", "--plan", "docs/plan.md")

    def led(self) -> build.Ledger:
        return build.load(self.dir() / build.LEDGER)

    def next_line(self) -> str:
        return self.ok("status").splitlines()[-1]

    def test_ids_come_in_plan_order_from_this_phase_only(self):
        self.with_plan()
        self.assertEqual(build.planned_units(self.w.root, self.led()), ["u1", "u2", "u3", "u10"])

    def test_no_plan_or_an_unreadable_one_or_another_slug_is_none(self):
        self.with_plan()
        led = self.led()
        self.assertIsNone(build.planned_units(self.w.root, build.Ledger(
            path=led.path, phase="p1", branch="b", base="0", plan="")))
        self.assertIsNone(build.planned_units(self.w.root, build.Ledger(
            path=led.path, phase="p1", branch="b", base="0", plan="docs/none.md")))
        self.assertIsNone(build.planned_units(self.w.root, build.Ledger(
            path=led.path, phase="p9", branch="b", base="0", plan="docs/plan.md")))

    def test_units_not_added_are_named(self):
        self.with_plan()
        self.unit("u1", "done")
        self.assertEqual(
            self.next_line(),
            f"Next: Every added unit is done; the plan names u2, u3 and u10 not yet added: add "
            f"them with `{GOVERN} build unit add`, or drop them from the plan.")
        data = json.loads(self.ok("status", "--json"))
        self.assertEqual(data["unadded"], ["u2", "u3", "u10"])

    def test_a_dropped_unit_is_not_missing(self):
        self.with_plan()
        self.unit("u1", "done")
        self.unit("u2")
        self.ok("unit", "drop", "u2")
        self.assertEqual(self.ledger()["dropped"], ["u2"])
        self.assertIn("the plan names u3 and u10 not yet added", self.next_line())
        self.assertEqual(json.loads(self.ok("status", "--json"))["unadded"], ["u3", "u10"])

    def test_one_missing_unit_is_named_alone(self):
        self.with_plan(PLAN.replace("### u10: Tenth  (developer)\n", ""))
        self.unit("u1", "done")
        self.unit("u2", "done")
        self.assertIn("the plan names u3 not yet added", self.next_line())

    def test_every_planned_unit_done_goes_to_review(self):
        self.with_plan()
        for uid in ("u1", "u2", "u3", "u10"):
            self.unit(uid, "done")
        self.assertEqual(self.next_line(), f"Next: Every unit is done: run `{GOVERN} build review`.")
        self.assertEqual(json.loads(self.ok("status", "--json"))["unadded"], [])

    def test_no_plan_says_every_added_unit(self):
        self.started()
        self.unit("u1", "done")
        self.assertEqual(self.next_line(),
                         f"Next: Every added unit is done: run `{GOVERN} build review`.")
        self.assertEqual(json.loads(self.ok("status", "--json"))["unadded"], [])

    def test_a_slug_the_plan_does_not_name_says_every_added_unit(self):
        self.with_plan(PLAN.replace("slug: p1", "slug: other"))
        self.unit("u1", "done")
        self.assertIn("Every added unit is done: run", self.next_line())

    def test_no_units_at_all_still_adds_the_first(self):
        self.with_plan()
        self.assertIn("Add the first unit", self.next_line())

    def test_the_session_line_compares_too(self):
        self.with_plan()
        self.unit("u1", "done")
        self.assertIn("the plan names u2, u3 and u10 not yet added",
                      build.session_line(self.w.root))

    def test_a_ledger_without_dropped_loads_and_saves_without_it(self):
        self.started()
        self.unit("u1")
        self.assertEqual(self.led().dropped, [])
        self.assertNotIn("dropped", self.ledger())
        self.ok("unit", "set", "u1", "running")
        self.assertNotIn("dropped", self.ledger())

    def test_a_dropped_that_is_not_a_list_of_text_is_refused(self):
        self.started()
        path = self.dir() / build.LEDGER
        path.write_text(path.read_text(encoding="utf-8") + 'dropped = [1]\n', encoding="utf-8")
        with self.assertRaises(build.BuildError) as raised:
            self.led()
        self.assertIn("`dropped` is not a list of text lines", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
