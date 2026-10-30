"""capture.py's days file: per profile, per local date, the first and last weekly figure of each window."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import profile_guard  # noqa: E402,F401

from test_statusline_row import payload  # noqa: E402
from test_usage_capture import load_capture  # noqa: E402

R, R2 = 1790499600, 1791104400
NOW = 1790000000
DAY = 86400


def date_of(t):
    return time.strftime("%Y-%m-%d", time.localtime(t))


class Days(unittest.TestCase):
    def setUp(self):
        self.cap = load_capture()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name) / "a home" / "state"
        self.cap.STATE = self.state
        self.cap.HISTORY = self.state / "history.jsonl"
        self.cap.DAYS = self.state / "days.json"
        self.cap.DAY_TIMES = self.state / "day-times.json"
        self.today = date_of(NOW)

    def week(self, u, r=R):
        return {"rate_limits": {"seven_day": {"used_percentage": u, "resets_at": r}}}

    def days(self):
        return json.loads(self.cap.DAYS.read_text(encoding="utf-8"))

    def test_first_figure_of_a_day_sets_the_triple(self):
        self.cap.record(self.week(10), NOW)
        self.assertEqual(self.days(), {"": {self.today: [[R, 10, 10]]}})

    def test_later_figures_raise_last(self):
        for u in (10, 12, 11):
            self.cap.record(self.week(u), NOW)
        self.assertEqual(self.days()[""][self.today], [[R, 10, 12]])

    def test_window_reset_within_a_day_adds_a_second_triple(self):
        self.cap.record(self.week(10), NOW)
        self.cap.record(self.week(0.5, R2), NOW + 60)
        self.assertEqual(self.days()[""][self.today], [[R, 10, 10], [R2, 0.5, 0.5]])

    def test_profiles_kept_apart(self):
        self.cap.record(self.week(10), NOW)
        with mock.patch.object(self.cap, "PROFILE", os.path.join(os.sep, "p", "work")):
            self.cap.record(self.week(30), NOW)
            self.cap.record(self.week(31), NOW)
            self.assertEqual(self.cap.day_start(self.cap.read_days(), self.cap.PROFILE, R, self.today), 30)
        data = self.days()
        self.assertEqual(data[""][self.today], [[R, 10, 10]])
        self.assertEqual(data[os.path.join(os.sep, "p", "work")][self.today], [[R, 30, 31]])

    def test_a_stale_session_does_not_lower_todays_first(self):
        self.cap.record(self.week(50), NOW)                   # session B, the evening before
        self.cap.record(self.week(30), NOW + DAY)             # idle session A redraws after midnight
        self.assertEqual(self.days()[""][date_of(NOW + DAY)], [[R, 50, 50]])

    def test_a_stale_session_does_not_lower_todays_first(self):
        self.cap.record(self.week(50), NOW)                   # session B, the evening before
        self.cap.record(self.week(30), NOW + DAY)             # idle session A redraws after midnight
        self.assertEqual(self.days()[""][date_of(NOW + DAY)], [[R, 50, 50]])

    def test_dates_over_35_days_dropped(self):
        old, edge = date_of(NOW - 36 * DAY), date_of(NOW - 35 * DAY)
        self.state.mkdir(parents=True)
        self.cap.DAYS.write_text(json.dumps({"": {old: [[R, 1, 2]], edge: [[R, 1, 2]]},
                                             "x": {old: [[R, 1, 2]]}}), encoding="utf-8")
        self.cap.record(self.week(10), NOW)
        data = self.days()
        self.assertEqual(set(data[""]), {edge, self.today})
        self.assertEqual(data["x"], {})

    def test_corrupt_file_reads_empty_and_is_rewritten(self):
        self.state.mkdir(parents=True)
        for junk in ("not json", "[1]"):
            self.cap.DAYS.write_text(junk, encoding="utf-8")
            self.assertEqual(self.cap.read_days(), {})
        self.cap.record(self.week(10), NOW)
        self.assertEqual(self.days(), {"": {self.today: [[R, 10, 10]]}})

    def test_no_write_when_nothing_changed(self):
        self.cap.record(self.week(10), NOW)
        os.utime(self.cap.DAYS, (1000, 1000))
        self.cap.record(self.week(10), NOW + 5)
        self.cap.record(self.week(9), NOW + 10)
        self.assertEqual(self.cap.DAYS.stat().st_mtime, 1000)
        self.cap.record(self.week(11), NOW + 15)
        self.assertNotEqual(self.cap.DAYS.stat().st_mtime, 1000)

    def test_a_days_error_never_stops_the_history(self):
        with mock.patch.object(self.cap, "record_days", side_effect=OSError("boom")):
            self.cap.record(payload(), NOW)
        self.assertTrue(self.cap.HISTORY.exists())

    def test_a_failed_write_leaves_no_temp_file(self):
        with mock.patch.object(self.cap.os, "replace", side_effect=OSError("locked")):
            with self.assertRaises(OSError):
                self.cap.record_days(self.week(10), NOW)
        self.assertEqual(list(self.state.glob(".days.*.tmp")), [])

    def times(self):
        return json.loads(self.cap.DAY_TIMES.read_text(encoding="utf-8"))

    def test_the_first_figure_records_its_time(self):
        self.cap.record(self.week(10), NOW + 0.7)
        self.assertEqual(self.times(), {"": {self.today: {str(R): int(NOW + 0.7)}}})
        self.assertEqual(self.cap.day_time(self.cap.read_day_times(), "", R, self.today), NOW)

    def test_later_figures_keep_the_first_time(self):
        self.cap.record(self.week(10), NOW)
        self.cap.record(self.week(12), NOW + 600)
        self.assertEqual(self.times(), {"": {self.today: {str(R): NOW}}})

    def test_a_second_window_in_a_day_gets_its_own_time(self):
        self.cap.record(self.week(10), NOW)
        self.cap.record(self.week(0.5, R2), NOW + 60)
        self.assertEqual(self.times()[""][self.today], {str(R): NOW, str(R2): NOW + 60})

    def test_day_times_profiles_kept_apart(self):
        self.cap.record(self.week(10), NOW)
        other = os.path.join(os.sep, "p", "work")
        with mock.patch.object(self.cap, "PROFILE", other):
            self.cap.record(self.week(30), NOW + 60)
        data = self.times()
        self.assertEqual(data[""][self.today], {str(R): NOW})
        self.assertEqual(data[other][self.today], {str(R): NOW + 60})

    def test_day_times_over_35_days_dropped(self):
        old, edge = date_of(NOW - 36 * DAY), date_of(NOW - 35 * DAY)
        self.state.mkdir(parents=True)
        self.cap.DAY_TIMES.write_text(json.dumps({"": {old: {str(R): 1}, edge: {str(R): 1}},
                                                  "x": {old: {str(R): 1}}}), encoding="utf-8")
        self.cap.record(self.week(10), NOW)
        data = self.times()
        self.assertEqual(set(data[""]), {edge, self.today})
        self.assertEqual(data["x"], {})

    def test_a_corrupt_day_times_file_reads_empty_and_the_days_file_is_still_written(self):
        self.state.mkdir(parents=True)
        for junk in ("not json", "[1]"):
            self.cap.DAY_TIMES.write_text(junk, encoding="utf-8")
            self.assertEqual(self.cap.read_day_times(), {})
        self.cap.record(self.week(10), NOW)
        self.assertEqual(self.days(), {"": {self.today: [[R, 10, 10]]}})
        self.assertEqual(self.times(), {"": {self.today: {str(R): NOW}}})
        with mock.patch.object(self.cap, "write_day_time", side_effect=OSError("boom")):
            self.cap.record(self.week(0.5, R2), NOW + 60)
        self.assertEqual(self.days()[""][self.today], [[R, 10, 10], [R2, 0.5, 0.5]])

    def test_week_args_returns_since(self):
        self.cap.record(self.week(10), NOW)
        with mock.patch.object(self.cap, "week_settings", return_value=({}, 1440, 0.1)):
            got = self.cap.week_args(self.week(10), NOW + 3600)
        self.assertEqual(got, ({}, 1440, 0.1, 10, NOW))

    def test_day_start(self):
        days = {"": {"2026-10-26": [[R, 10, 12], [R2, 0.5, 3], "bad"]}, "p": {"2026-10-26": [[R, 40, 41]]}}
        self.assertEqual(self.cap.day_start(days, "", R, "2026-10-26"), 10)
        self.assertEqual(self.cap.day_start(days, "", R2 + 0.7, "2026-10-26"), 0.5)
        self.assertEqual(self.cap.day_start(days, "p", R, "2026-10-26"), 40)
        self.assertIsNone(self.cap.day_start(days, "", R, "2026-10-27"))
        self.assertIsNone(self.cap.day_start(days, "q", R, "2026-10-26"))
        self.assertIsNone(self.cap.day_start({"": "junk"}, "", R, "2026-10-26"))

    def test_day_totals(self):
        days = {"": {"2026-10-26": [[R, 10, 12], [R2, 0.5, 3]],
                     "2026-10-27": [[R, 1, 2], [1, 2], "x", [R, "a", 3]], "2026-10-28": "junk"},
                "p": {"2026-10-26": [[R, 40, 41]]}}
        self.assertEqual(self.cap.day_totals(days, ""), {"2026-10-26": 4.5, "2026-10-27": 1})
        self.assertEqual(self.cap.day_totals(days, "p"), {"2026-10-26": 1})
        self.assertEqual(self.cap.day_totals({}, ""), {})
