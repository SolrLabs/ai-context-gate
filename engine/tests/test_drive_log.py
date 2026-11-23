"""Overnight driver measurement rows: reading a session, the shared log, the threshold suggestion."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import profile_guard  # noqa: E402,F401

from govern import drive_log  # noqa: E402

META = {"project": "a", "plan": "p", "phase": "p1", "next": "u1", "started": 1.0, "ended": 2.0,
        "exit": "continue"}


def assistant(total: int) -> dict:
    return {"type": "assistant", "message": {"usage": {
        "input_tokens": 1, "cache_read_input_tokens": total - 11, "cache_creation_input_tokens": 10}}}


def result(window=1_000_000, models=None) -> dict:
    return {"type": "result", "session_id": "s1",
            "usage": {"input_tokens": 5, "output_tokens": 6, "service_tier": "x"},
            "modelUsage": models or {"m": {"inputTokens": 1, "cacheReadInputTokens": 1,
                                           "cacheCreationInputTokens": 1, "contextWindow": window}}}


def row_of(project: str, pct) -> dict:
    return {"project": project, "peak_pct": pct, "compacts": 1, "exit": "continue"}


class DriveLogTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)

    def write(self, lines) -> Path:
        path = self.home / "t.jsonl"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_row_from_session(self):
        boundary = {"type": "system", "subtype": "compact_boundary"}
        path = self.write([json.dumps(e) for e in
                           (assistant(10_000), boundary, assistant(60_000), boundary, assistant(15_000))])
        row = drive_log.session_row(path, result(), META)
        self.assertEqual(row["peak_tokens"], 60000)
        self.assertEqual(row["peak_pct"], 6.0)
        self.assertEqual(row["compacts"], 2)
        self.assertEqual(row["window"], 1000000)
        self.assertEqual(row["session"], "s1")
        self.assertEqual(row["tokens"], {"input_tokens": 5, "output_tokens": 6})
        for key, value in META.items():
            self.assertEqual(row[key], value)

    def test_window_from_largest_model(self):
        models = {"small": {"inputTokens": 1, "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0,
                            "contextWindow": 200_000},
                  "big": {"inputTokens": 1, "cacheReadInputTokens": 500, "cacheCreationInputTokens": 5,
                          "contextWindow": 1_000_000}}
        self.assertEqual(drive_log.session_row(None, result(models=models), META)["window"], 1_000_000)

    def test_bad_transcript(self):
        bad = self.write([json.dumps(assistant(100)), "not json"])
        for transcript in (self.home / "missing.jsonl", bad):
            row = drive_log.session_row(transcript, result(), META)
            for key in ("peak_tokens", "peak_pct", "compacts"):
                self.assertIsNone(row[key])
        row = drive_log.session_row(None, None, META)
        self.assertIsNone(row["window"])
        self.assertIsNone(row["session"])

    def test_append_and_rows(self):
        drive_log.append(self.home, row_of("a", 1.0))
        drive_log.append(self.home, row_of("b", 2.0))
        with drive_log.log_path(self.home).open("a", encoding="utf-8") as handle:
            handle.write("garbage\n")
        drive_log.append(self.home, row_of("a", 3.0))
        self.assertEqual([r["peak_pct"] for r in drive_log.rows(self.home, "a")], [1.0, 3.0])
        self.assertEqual(len(drive_log.rows(self.home)), 3)

    def test_suggest(self):
        self.assertIsNone(drive_log.suggest([row_of("a", 5.0)] * 19))
        self.assertEqual(drive_log.suggest([row_of("a", float(i)) for i in range(1, 21)]), 30)
        self.assertEqual(drive_log.suggest([row_of("a", 95.0)] * 20), 90)
        self.assertEqual(drive_log.suggest([row_of("a", 1.0)] * 20), 20)
        tail = [row_of("a", float(v)) for v in [1] * 17 + [34, 41, 46]]
        self.assertEqual(drive_log.suggest(tail), 55)

    def test_per_project_suggestion(self):
        for i in range(20):
            drive_log.append(self.home, row_of("big", float(i + 1)))
        for i in range(5):
            drive_log.append(self.home, row_of("small", 5.0))
        text = drive_log.report(self.home)
        self.assertEqual(text.count("suggested --compact-pct: 30"), 2)
        self.assertEqual(text.count("too few sessions (5 of 20)"), 1)

    def test_named_project_report(self):
        drive_log.append(self.home, row_of("x", 5.0))
        drive_log.append(self.home, row_of("y", 5.0))
        text = drive_log.report(self.home, "x")
        self.assertTrue(text.startswith("x\n"))
        self.assertNotIn("all projects", text)
        self.assertNotIn("y\n", text)
        self.assertEqual(drive_log.report(self.home, "nope"), "no sessions recorded for nope\n")

    def test_report(self):
        for i in range(20):
            drive_log.append(self.home, row_of("a" if i % 2 else "b", float(i + 1)))
        text = drive_log.report(self.home)
        for needle in ("a\n", "b\n", "all projects", "suggested --compact-pct: 30"):
            self.assertIn(needle, text)
        self.assertNotIn("$", text)
        few = drive_log.report(self.home, "a")
        self.assertIn("too few sessions (10 of 20)", few)


if __name__ == "__main__":
    unittest.main()
