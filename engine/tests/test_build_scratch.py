"""`govern build`: a scratch folder for each agent, and the messages of a write that fails.

    python3 -m unittest discover -s engine/tests -k build_scratch
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import profile_guard  # noqa: E402,F401

from build_support import *  # noqa: E402,F401,F403


class Scratch(Build):
    def folders(self, text: str) -> list[str]:
        return [line for line in text.splitlines() if line.startswith("Scratch files go only in ")]

    def test_every_brief_kind_names_its_own_scratch_folder(self):
        led = ledger(self.tmp, ("u2", "done"))
        name = led.dir.name
        root = self.tmp
        briefs = {
            "u2": build.unit_brief(led, led.units[0], False),
            "fix-1": build.fix_brief(led, "developer", 1),
            "fix-2": build.fix_brief(led, "developer", 2),
            "review-code": build.code_reviewer_brief(root, led),
            "review-qa": build.qa_engineer_brief(root, led),
            "review-ux": build.ux_reviewer_brief(root, led),
            "close": build.close_brief(root, led),
        }
        seen = []
        for folder, text in briefs.items():
            found = self.folders(text)
            self.assertEqual(found, [build.scratch_note(led, folder)], folder)
            self.assertIn(f"{BUILD.as_posix()}/{name}/{folder}/scratch/; create nothing "
                          "elsewhere and delete only what you created.", found[0])
            seen.append(found[0])
        self.assertEqual(len(set(seen)), len(seen))

    def test_a_write_that_fails_after_the_ledger_is_saved_says_what_stays(self):
        self.project()
        with mock.patch.object(build, "_keep_in_history", side_effect=OSError(5, "boom", "/x/y")):
            code, out, err = self.go("start", "p1")
        self.assertEqual(code, 1, out)
        self.assertEqual(err, "error: stopped at /x/y: boom; anything written before it stays "
                              "(python3 .context-gate/bin/govern build status shows it)\n")
        self.assertTrue((self.dir() / build.LEDGER).is_file())

    def test_an_error_with_no_path_says_stopped_and_the_reason(self):
        self.project()
        with mock.patch.object(build, "_keep_in_history", side_effect=OSError("boom")):
            code, out, err = self.go("start", "p1")
        self.assertEqual(code, 1, out)
        self.assertTrue(err.startswith("error: stopped: boom; anything written"), err)

    def test_a_run_record_that_cannot_be_written_is_a_warning_and_the_command_goes_on(self):
        self.project(extra=MEASURE_ON)
        with mock.patch.object(build.agent_runs, "mark", side_effect=OSError(13, "denied", "/log/x")):
            code, out, err = self.go("start", "p1")
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "warning: run record not written: /log/x: denied\n")
        self.assertIn("started p1", out)
        self.assertTrue((self.dir() / build.LEDGER).is_file())


if __name__ == "__main__":
    unittest.main()
