"""`govern build`: the project's `brief_lines` (agent-roles) under `## Project rules` in every brief.

    python3 -m unittest discover -s engine/tests -k brief_lines
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import profile_guard  # noqa: E402,F401

from build_support import *  # noqa: E402,F401,F403
from test_roles import defaults  # noqa: E402

LINES = ["Commits carry no attribution line.", "A test file stays under 1,200 lines."]
ON = ('\n[checks.agent-roles]\nlevel = "warn"\nbrief_lines = ['
      + ", ".join(f'"{line}"' for line in LINES) + "]\n")
SECTION = "## Project rules\n\n" + "".join(f"- {line}\n" for line in LINES) + "\n"


class BriefLines(Build):
    def reviewed(self, **kw) -> None:
        self.started(**kw)
        self.unit("u1", "done")
        self.code()
        self.ok("review")

    def read(self, *parts: str) -> str:
        return self.dir().joinpath(*parts).read_text(encoding="utf-8")

    def test_a_unit_brief_and_a_fix_brief_hold_the_lines_before_the_report_note(self):
        self.reviewed(extra=ON)
        self.ok("fix")
        unit, fix = self.read("u1", "brief.md"), self.read("fix", "brief.md")
        for text in (unit, fix):
            self.assertEqual(text.count("## Project rules"), 1, text)
            self.assertIn(SECTION, text)
            self.assertLess(text.index(SECTION), text.index(build.REPORT_NOTE))
            self.assertLess(text.index(SECTION), text.index("Produces:"))
        self.assertLess(fix.index(SECTION), fix.index(build.FIX_RUNNING_NOTE))

    def test_with_no_lines_the_briefs_are_what_they_were(self):
        self.reviewed()
        self.ok("fix")
        unit, fix = self.read("u1", "brief.md"), self.read("fix", "brief.md")
        self.assertNotIn("Project rules", unit + fix)
        led = ledger(self.w.root / BUILD, ("u1", "done"))
        row = led.units[0]
        self.assertEqual(unit, build.unit_brief(led, row, False))
        self.assertEqual(unit, build.unit_brief(led, row, False, []))
        self.assertEqual(fix, build.fix_brief(led, "developer", 1))
        self.assertEqual(fix, build.fix_brief(led, "developer", 1, []))

    def test_the_lines_are_verbatim_and_in_order(self):
        led = ledger(self.tmp, ("u1", "pending"))
        rules = ["b", "a  x", "c"]
        self.assertIn("## Project rules\n\n- b\n- a  x\n- c\n\n",
                      build.unit_brief(led, led.units[0], False, rules))
        self.assertIn("## Project rules\n\n- b\n- a  x\n- c\n\n",
                      build.fix_brief(led, "developer", 1, rules))

    def test_a_line_that_is_not_text_is_a_config_error_naming_brief_lines(self):
        for value, shown in (([3], "3"), ([""], "''"), (["ok", "  "], "'  '")):
            found = roles.problems(defaults(brief_lines=value))
            self.assertEqual(len(found), 1, found)
            self.assertIn("brief_lines[", found[0])
            self.assertIn(f"= {shown}: expected a line of text", found[0])
        self.assertEqual(roles.problems(defaults(brief_lines=LINES)), [])

    def test_a_filled_brief_holding_the_section_passes_the_launch_gate(self):
        self.started(extra=ON)
        brief = self.unit("u1")
        text = "".join("Filled.\n" if line.startswith(agent_runs.FILL) else line + "\n"
                       for line in brief.read_text(encoding="utf-8").splitlines())
        self.assertIn("## Project rules", text)
        brief.write_text(text, encoding="utf-8")
        self.assertEqual(agent_runs.unfilled(text), [])
        self.w.commit("briefs", at=self.w.top, index=False)
        out = self.ok("launch", "u1")
        self.assertEqual(out.splitlines()[0], "launch developer with the prompt:")


if __name__ == "__main__":
    unittest.main()
