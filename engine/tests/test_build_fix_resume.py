"""A fix is never resumed: `unit set --agent`, `fix --unit` and `launch fix`, which always prints
a fresh launch.

    python3 -m unittest discover -s engine/tests -k build_fix
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import profile_guard  # noqa: E402,F401

import test_build_worktree as wt  # noqa: E402
from build_support import *  # noqa: E402,F401,F403


class FixResume(Build):
    def reviewed(self, *agent: str) -> None:
        self.started()
        self.unit("u1")
        self.ok("unit", "set", "u1", "done", *agent)
        self.code()
        self.ok("review")

    def brief(self, number: int) -> Path:
        return self.dir() / "fix" / ("brief.md" if number == 1 else f"brief-{number}.md")

    def fix(self, *flags: str) -> None:
        """Write the next fix brief, fill it in and commit it."""
        self.ok("fix", *flags)
        number = len(list((self.dir() / "fix").glob("brief*.md")))
        path = self.brief(number)
        text = "".join("Filled.\n" if line.startswith(agent_runs.FILL) else line + "\n"
                       for line in path.read_text(encoding="utf-8").splitlines())
        path.write_text(text, encoding="utf-8")
        self.w.commit("fix brief", at=self.w.top, index=False)

    def launched(self) -> list[str]:
        return self.ok("launch", "fix").splitlines()

    def test_the_agent_is_stored_and_a_fix_for_the_unit_is_a_fresh_launch(self):
        self.reviewed("--agent", "abc")
        self.assertEqual(self.ledger()["unit"][0]["agent"], "abc")
        self.fix("--unit", "u1")
        text = self.brief(1).read_text(encoding="utf-8")
        self.assertEqual(text.splitlines()[1], "Unit: u1")
        self.assertIn("## Tests", text)
        lines = self.launched()
        self.assertEqual(lines[:2], ["launch developer with the prompt:",
                                     f"Brief: {at('fix', 'brief.md')}"])
        self.assertEqual(lines[2], f"Base: {self.head()}")
        self.assertFalse([x for x in lines if x.startswith("Resume:")])

    def test_no_fix_brief_for_a_unit_is_ever_resumed(self):
        self.reviewed("--agent", "abc")
        for n in range(1, 5):
            self.fix("--unit", "u1")
            lines = self.launched()
            self.assertEqual(lines[0], "launch developer with the prompt:", n)
            self.assertFalse([x for x in lines if x.startswith("Resume:")], n)

    def test_a_brief_made_without_unit_is_a_fresh_launch(self):
        self.reviewed("--agent", "abc")
        self.fix()
        self.assertNotIn("Unit:", self.brief(1).read_text(encoding="utf-8"))
        self.assertEqual(self.launched()[0], "launch developer with the prompt:")

    def test_a_unit_set_failed_loses_its_agent(self):
        self.reviewed("--agent", "abc")
        self.ok("unit", "set", "u1", "failed")
        self.assertEqual(self.ledger()["unit"][0]["agent"], "")

    def test_a_relaunched_unit_forgets_the_agent_of_the_earlier_attempt(self):
        self.started()
        wt.fill(self.unit("u1"))
        self.w.commit("briefs", at=self.w.top, index=False)
        self.ok("unit", "set", "u1", "done", "--agent", "abc")
        self.ok("unit", "set", "u1", "pending")
        self.ok("launch", "u1")
        self.assertEqual(self.ledger()["unit"][0]["agent"], "")
        self.ok("unit", "set", "u1", "done")
        self.code()
        self.ok("review")
        self.fix("--unit", "u1")
        lines = self.launched()
        self.assertFalse([x for x in lines if x.startswith("Resume:")], lines)
        self.assertEqual(lines[0], "launch developer with the prompt:")

    def test_a_unit_that_does_not_exist_is_refused(self):
        self.reviewed("--agent", "abc")
        self.refused("has no unit u9 (u1)", "fix", "--unit", "u9")
        self.assertFalse((self.dir() / "fix").exists())

    def test_the_agent_is_not_in_the_status_json_and_a_ledger_without_it_parses(self):
        self.reviewed("--agent", "abc")
        rows = json.loads(self.ok("status", "--json"))["units"]
        self.assertNotIn("agent", rows[0])
        led = self.dir() / build.LEDGER
        text = led.read_text(encoding="utf-8").replace('agent = "abc"\n', "")
        self.assertNotIn("agent", text)
        led.write_text(text, encoding="utf-8")
        self.assertEqual(build.parse(text, led).units[0]["agent"], "")
        self.ok("status")


if __name__ == "__main__":
    unittest.main()
