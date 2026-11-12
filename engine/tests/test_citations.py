"""The decision-citations check: ids cited in docs and code that do not exist or were replaced.

    python3 -m unittest discover -s engine/tests -k test_citations
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import profile_guard  # noqa: E402,F401

from base_support import LOG, Case, decision, trap  # noqa: E402

ON = '\n[checks.decision-citations]\nlevel = "warn"\n'
FM = "---\nstatus: active\n---\n"


def doc(w, rel: str, body: str) -> None:
    w.write(rel, "# X\n\n" + body)


class Citations(Case):
    def check(self, w) -> str:
        code, out, err = w.run("check")
        return out + err

    def test_a_missing_id_warns(self):
        w = self.solo(extra=ON)
        doc(w, "docs/x.md", "See P-9 for why.\n")
        self.assertIn("docs/x.md:3: P-9 does not exist", self.check(w))

    def test_existing_ids_and_padding_pass(self):
        w = self.solo(extra=ON)
        doc(w, "docs/x.md", "See P-1, P-003 and T-1.\n")
        self.assertNotIn("does not exist", self.check(w))

    def test_a_replaced_id_warns(self):
        w = self.solo(extra=ON)
        w.write(LOG, w.read(LOG) + decision("P", 4, None, title="Replaced by P-5", body="\n")
                + decision("P", 5))
        doc(w, "docs/x.md", "See P-4.\n")
        self.assertIn("docs/x.md:3: P-4 was replaced by P-5", self.check(w))

    def test_ids_in_a_block_or_heading_are_not_citations(self):
        w = self.solo(extra=ON)
        doc(w, "docs/x.md", "```\nP-9\n```\n\n<!-- P-8 -->\n\n## P-7 - A heading\n\n"
                            "`code` and P-6 here.\n")
        out = self.check(w)
        for gone in ("P-9", "P-8", "P-7"):
            self.assertNotIn(f"{gone} does not exist", out)
        self.assertIn("docs/x.md:11: P-6 does not exist", out)

    def test_utf_8_is_not_a_citation(self):
        w = self.solo(extra=ON)
        doc(w, "docs/x.md", "Files are UTF-8, and UTF-16 too.\n")
        self.assertNotIn("UTF", self.check(w))

    def test_code_globs_are_read(self):
        w = self.solo(extra=ON + 'code_globs = ["src/*.py"]\n')
        w.write("src/a.py", "# see P-9\n")
        w.write("lib/b.py", "# see P-8\n")
        out = self.check(w)
        self.assertIn("src/a.py:1: P-9 does not exist", out)
        self.assertNotIn("P-8", out)

    def test_agent_definitions_and_briefs_are_read(self):
        w = self.solo(extra=ON)
        w.write(".claude/agents/a.md", "Follow P-9.\n")
        w.write(".context-gate/build/p/brief.md", "Read P-8.\n")
        out = self.check(w)
        self.assertIn(".claude/agents/a.md:1: P-9 does not exist", out)
        self.assertIn(".context-gate/build/p/brief.md:1: P-8 does not exist", out)

    def test_an_unreadable_file_hides_nothing_else(self):
        w = self.solo(extra=ON)
        (w.root / "docs").mkdir(exist_ok=True)
        (w.root / "docs" / "bad.md").write_bytes(FM.encode() + b"# X\n\n\xff\xfe not utf-8 P-8\n")
        doc(w, "docs/x.md", "See P-9.\n")
        out = self.check(w)
        self.assertIn("docs/x.md:3: P-9 does not exist", out)
        self.assertIn("docs/bad.md: not read (", out)
        self.assertIn("); its citations were not checked", out)

    def test_a_crlf_doc_is_found_at_the_right_line(self):
        w = self.solo(extra=ON)
        (w.root / "docs").mkdir(exist_ok=True)
        (w.root / "docs" / "c.md").write_bytes(b"# X\r\n\r\nSee P-9.\r\n")
        self.assertIn("docs/c.md:3: P-9 does not exist", self.check(w))

    def test_a_trap_is_resolved_in_its_own_project(self):
        w = self.team(extra=ON + 'code_globs = ["projects/*/src/*.py", "tools/*.py"]\n')
        gamma = "projects/gamma/working-files/traps.md"
        w.write(gamma, w.read(gamma) + trap(9))
        w.write("projects/alpha/src/a.py", "# see T-9\n")
        w.write("projects/gamma/src/g.py", "# see T-9\n")
        w.write("tools/t.py", "# see T-9\n")
        out = self.check(w)
        self.assertIn("projects/alpha/src/a.py:1: T-9 does not exist", out)
        self.assertNotIn("gamma/src/g.py", out)
        self.assertNotIn("tools/t.py", out)

    def test_a_marker_drops_its_ids_on_its_line_and_the_next(self):
        w = self.solo(extra=ON)
        doc(w, "docs/x.md", "<!-- decision-citations: allow P-42 -->\n"
                            "Cites P-42 and P-7.\n\n"
                            "Cites P-42 again. <!-- decision-citations: allow T-3, P-42 -->\n"
                            "Cites P-42 and T-3 and T-4.\n")
        out = self.check(w)
        self.assertIn("docs/x.md:4: P-7 does not exist", out)
        self.assertNotIn("docs/x.md:3:", out)
        self.assertNotIn("P-42", out)
        self.assertNotIn("T-3", out)
        self.assertIn("docs/x.md:7: T-4 does not exist", out)

    def test_a_marker_on_the_line_above_covers_only_the_next_line(self):
        w = self.solo(extra=ON)
        doc(w, "docs/x.md", "<!-- decision-citations: allow P-42 -->\n"
                            "Cites P-42.\n"
                            "Cites P-42 too.\n")
        out = self.check(w)
        self.assertNotIn("docs/x.md:4:", out)
        self.assertIn("docs/x.md:5: P-42 does not exist", out)

    def test_example_ids_are_dropped_everywhere(self):
        w = self.solo(extra=ON + 'example_ids = ["P-42", "t-3"]\n')
        doc(w, "docs/x.md", "Cites P-42, T-3 and P-7.\n")
        out = self.check(w)
        self.assertIn("docs/x.md:3: P-7 does not exist", out)
        self.assertNotIn("P-42", out)
        self.assertNotIn("T-3", out)

    def test_a_non_string_example_id_is_a_config_problem_and_skipped(self):
        w = self.solo(extra=ON + 'example_ids = [3, "P-42"]\n')
        doc(w, "docs/x.md", "Cites P-42 and P-7.\n")
        out = self.check(w)
        self.assertNotIn("Traceback", out)
        self.assertIn("[checks.decision-citations] example_ids[0] = 3: expected an id like P-1",
                      out)
        self.assertIn("docs/x.md:3: P-7 does not exist", out)
        self.assertNotIn("P-42 does not exist", out)

    def test_off_reports_nothing(self):
        w = self.solo()
        doc(w, "docs/x.md", "See P-9.\n")
        self.assertNotIn("P-9", self.check(w))


if __name__ == "__main__":
    unittest.main()
