"""Three messages and one param met on a first setup: the profile-override warning, the memory
index's per-line warning, and writing-rules' `include_excluded`.

    python3 -m unittest discover -s engine/tests -k test_field_messages
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from regressions_support import *  # noqa: E402,F401,F403
import test_memory  # noqa: E402


class ProfileOverrideMessage(Base):
    def test_the_warning_names_the_table_level_reason_key(self):
        prof = self.tmp / "prof"
        prof.mkdir()
        wtext(prof / "principles.toml", "[checks.agents]\nmax_turns = 100\n")
        wtext(prof / "PRINCIPLES.md", "# Principles\n\n1. Measure, then claim.\n")
        w = self.ws(extra="\n[checks.agents]\nmax_turns = 50\n")
        cfg = w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            "schema = 1", f'schema = 1\nprofile = "{prof.as_posix()}"', 1))
        _, out, _ = w.run("check")
        self.assertIn("[checks.agents] max_turns overrides the profile with no reason — follow "
                      'the profile, or add reason = "<why>" under [checks.agents]', out)


class MemoryLineMessage(test_memory.MemoryIndex):
    def test_the_per_line_warning_says_what_an_older_engine_counts(self):
        self.memory("a.md", "---\ntype: project\n---\nbody\n")
        self.memory("b.md", "---\ntype: project\n---\nbody\n")
        self.index(["- [a](a.md) and [b](b.md)"])
        warnings = self.run_check().warnings
        self.assertEqual(len(warnings), 1)
        self.assertIn("line 1 links 2 memories — one memory per line, so the limit counts "
                      "memories", warnings[0])
        self.assertTrue(warnings[0].endswith(
            "; an engine before 0.7.0 counts lines instead, so splitting a line can put it "
            "over max_lines there"), warnings[0])


class IncludeExcluded(Base):
    RULE = '{ text = "colour", use = "color" }'
    HIT = "ERROR  build/notes.md: 1 × 'colour' (use 'color')"
    HINT = "(always excluded) — list it in include_excluded to check it anyway"

    def rules_ws(self, extra: str = "") -> Workspace:
        w = self.ws(extra=f"""
[checks.writing-rules]
level = "error"
files = ["notes/*.md", "build/*.md"]
rules = [{self.RULE}]
{extra}""")
        w.write("notes/a.md", "The colour of it.\n")
        w.write("build/notes.md", "Release notes: colour.\n")
        return w

    def hits(self, w: Workspace) -> list[str]:
        code, out, err = w.run("check")
        self.assertNotEqual(code, 2, err)
        return [line.strip() for line in out.splitlines() if " × " in line]

    def test_a_file_under_build_is_left_out_with_the_hint(self):
        w = self.rules_ws()
        _, out, _ = w.run("check")
        self.assertIn(f"writing-rules: 'build/*.md' matched 1 file(s), all left out {self.HINT}",
                      out)
        self.assertEqual(self.hits(w), ["ERROR  notes/a.md: 1 × 'colour' (use 'color')"])

    def test_include_excluded_checks_it(self):
        w = self.rules_ws(extra='include_excluded = ["build/*.md"]\n')
        self.assertEqual(self.hits(w), ["ERROR  build/notes.md: 1 × 'colour' (use 'color')",
                                        "ERROR  notes/a.md: 1 × 'colour' (use 'color')"])
        _, out, _ = w.run("check")
        self.assertNotIn("all left out", out)

    def test_the_tools_folder_and_claude_stay_left_out(self):
        w = self.ws(extra=f"""
[checks.writing-rules]
level = "error"
files = ["notes/*.md", ".context-gate/*.md", ".claude/*.md"]
include_excluded = [".context-gate/*.md", ".claude/*.md"]
rules = [{self.RULE}]
""")
        w.write("notes/a.md", "The colour of it.\n")
        w.write(".context-gate/notes.md", "Release notes: colour.\n")
        w.write(".claude/notes.md", "Release notes: colour.\n")
        out = "\n".join(self.hits(w))
        self.assertNotIn(".context-gate/notes.md", out)
        self.assertNotIn(".claude/notes.md", out)

    def test_a_glob_that_matches_nothing_changes_nothing(self):
        w = self.rules_ws(extra='include_excluded = ["dist/*.md"]\n')
        _, out, _ = w.run("check")
        self.assertIn(f"all left out {self.HINT}", out)
        self.assertEqual(self.hits(w), ["ERROR  notes/a.md: 1 × 'colour' (use 'color')"])


if __name__ == "__main__":
    unittest.main()
