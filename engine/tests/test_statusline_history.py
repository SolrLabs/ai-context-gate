"""capture.py's shared history and recent-rate projection: every session's figures are merged,
and the row's color follows the last hour, not the whole window."""
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

from test_statusline_row import limits, payload  # noqa: E402
from test_usage_capture import load_capture  # noqa: E402

R = 1790499600
NOW = R - 7200               # 7200 s left in the 5-hour window
GREEN, YELLOW, RED = "32", "33", "31"
WMON = time.mktime((2026, 10, 26, 10, 0, 0, 0, 0, -1))       # Monday 10:00, local
WR = time.mktime((2026, 10, 31, 13, 0, 0, 0, 0, -1))         # the weekly reset: Saturday 13:00, local


class Base(unittest.TestCase):
    def setUp(self):
        self.cap = load_capture()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name) / "a home" / "state"          # a space, on purpose
        self.cap.STATE = self.state
        self.cap.HISTORY = self.state / "history.jsonl"
        self.cap.DAYS = self.state / "days.json"
        self.cap.DAY_TIMES = self.state / "day-times.json"
        self.cap.WEEK_SETTINGS = Path(self._tmp.name) / "usage-week.toml"       # absent: the defaults
        self.env = mock.patch.dict(os.environ)
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop("NO_COLOR", None)

    def lines(self):
        text = self.cap.HISTORY.read_text(encoding="utf-8")
        return [json.loads(x) for x in text.splitlines()]

    def seed(self, rows):
        self.state.mkdir(parents=True, exist_ok=True)
        self.cap.HISTORY.write_text("".join(json.dumps({"t": t, "w": w, "r": r, "u": u}) + "\n"
                                            for t, w, r, u in rows), encoding="utf-8")

    def payload5(self, u, r=R):
        return {"rate_limits": {"five_hour": {"used_percentage": u, "resets_at": r}}}

    def payload7(self, u, r=R):
        return {"rate_limits": {"seven_day": {"used_percentage": u, "resets_at": r}}}

    def code(self, data, now):
        self.cap.record(data, now)
        text = self.cap.render(data, now, None, self.cap.merged(data, now))
        return text.split("m", 1)[0].split("[")[1]


class Record(Base):
    def test_once_per_window_per_minute(self):
        data = payload()
        self.cap.record(data, NOW)
        self.cap.record(data, NOW + 30)
        self.assertEqual([(x["w"], x["u"]) for x in self.lines()], [("5h", 40), ("7d", 20)])
        self.cap.record(data, NOW + 61)
        self.assertEqual(len(self.lines()), 4)

    def test_a_higher_figure_is_recorded_a_lower_one_never(self):
        self.cap.record(self.payload5(40), NOW)
        self.cap.record(self.payload5(41), NOW + 5)
        self.cap.record(self.payload5(30), NOW + 10)
        self.assertEqual([x["u"] for x in self.lines()], [40, 41])

    def test_a_lower_figure_is_not_recorded_after_a_minute_either_when_one_is_recent(self):
        self.cap.record(self.payload5(40), NOW)
        self.cap.record(self.payload5(30), NOW + 50)
        self.assertEqual(len(self.lines()), 1)

    def test_a_lower_figure_after_a_minute_is_not_recorded_a_higher_or_equal_one_is(self):
        self.cap.record(self.payload5(38), NOW)
        self.cap.record(self.payload5(27), NOW + 120)
        self.assertEqual([x["u"] for x in self.lines()], [38])
        self.cap.record(self.payload5(38), NOW + 120)
        self.assertEqual([x["u"] for x in self.lines()], [38, 38])
        self.cap.record(self.payload5(39), NOW + 121)
        self.assertEqual([x["u"] for x in self.lines()], [38, 38, 39])

    def test_prunes_past_500_lines_to_the_last_two_hours(self):
        self.seed([(NOW - 20000 + i, "5h", R, 10) for i in range(300)]
                  + [(NOW - 100 + i % 50, "5h", R, 10) for i in range(210)])
        self.cap.record(self.payload5(50), NOW)
        rows = self.lines()
        self.assertEqual(len(rows), 211)
        self.assertTrue(all(x["t"] >= NOW - 7200 for x in rows))
        self.assertEqual(rows[-1]["u"], 50)

    def test_a_garbage_line_is_skipped(self):
        self.state.mkdir(parents=True)
        self.cap.HISTORY.write_text('not json\n{"t": "x"}\n[1]\n\n', encoding="utf-8")
        self.cap.record(self.payload5(40), NOW)
        self.assertEqual(self.cap.history("5h", R, NOW), [(NOW, 40)])

    def test_a_non_finite_figure_is_skipped(self):
        self.state.mkdir(parents=True)
        self.cap.HISTORY.write_text(
            '{"t": 1, "w": "5h", "r": Infinity, "u": 5}\n'
            '{"t": %d, "w": "5h", "r": %d, "u": NaN}\n'
            '{"t": NaN, "w": "5h", "r": %d, "u": 5}\n'
            '{"t": %d, "w": "5h", "r": %d, "u": 30}\n' % (NOW - 200, R, R, NOW - 100, R), encoding="utf-8")
        self.cap.record(self.payload5(40), NOW)
        self.assertEqual(self.cap.history("5h", R, NOW), [(NOW - 100, 30), (NOW, 40)])
        data = self.payload5(10)
        text = self.cap.render(data, NOW + 1, None, self.cap.merged(data, NOW + 1))
        self.assertIn("Usage: 40%", text)

    def test_a_failed_prune_leaves_no_temp_file(self):
        self.seed([(NOW - 100 + i % 50, "5h", R, 10) for i in range(510)])
        with mock.patch.object(self.cap.os, "replace", side_effect=OSError("locked")):
            with self.assertRaises(OSError):
                self.cap.record(self.payload5(50), NOW)
        self.assertEqual(list(self.state.glob(".history.*.tmp")), [])

    def test_a_payload_without_figures_writes_nothing(self):
        self.cap.record({"rate_limits": {"five_hour": {"used_percentage": "x"}}}, NOW)
        self.cap.record({}, NOW)
        self.assertFalse(self.cap.HISTORY.exists())


class Merge(Base):
    def test_history_is_a_running_maximum(self):
        self.seed([(100, "5h", R, 30), (200, "5h", R, 27), (300, "5h", R, 38), (50, "5h", R + 1, 99)])
        self.assertEqual(self.cap.history("5h", R, 400), [(100, 30), (200, 30), (300, 38)])

    def test_a_stale_session_shows_the_freshest_figure(self):
        self.cap.record(self.payload5(38), NOW)
        stale = self.payload5(27)
        text = self.cap.render(stale, NOW + 5, None, self.cap.merged(stale, NOW + 5))
        self.assertIn("Usage: 38%", text)
        fresh = self.payload5(38)
        self.assertIn("Usage: 38%", self.cap.render(fresh, NOW + 5, None, self.cap.merged(fresh, NOW + 5)))

    def test_a_new_window_ignores_the_old_one(self):
        self.seed([(NOW - 3600, "5h", R - 18000, 90), (NOW - 60, "5h", R - 18000, 95)])
        data = self.payload5(5)
        self.assertEqual(self.cap.merged(data, NOW), {"5h": []})
        self.assertIn("Usage: 5%", self.cap.render(data, NOW, None, self.cap.merged(data, NOW)))

    def test_render_without_history_is_what_it_was(self):
        data = payload()
        self.assertEqual(self.cap.render(data, NOW, "b"), self.cap.render(data, NOW, "b", {}))


class Projection(Base):
    def test_five_hour_examples(self):
        for then, now_u, code in ((30, 40, GREEN), (30, 60, RED), (50, 50, GREEN), (30, 55, YELLOW)):
            with self.subTest(then=then, now=now_u):
                self.state.mkdir(parents=True, exist_ok=True)
                self.seed([(NOW - 3600, "5h", R, then)])
                self.assertEqual(self.code(self.payload5(now_u), NOW), code)

    def test_an_idle_hour_is_green_though_the_average_would_be_red(self):
        self.seed([(NOW - 3600, "5h", R, 50)])
        late = R - 3600 * 1.0                        # 3600 s left, 14400 elapsed: average projects 118.75
        self.assertEqual(self.cap.pace_color(95, 3600, 18000, 5), RED)    # the average rule alone
        self.seed([(late - 3600, "5h", R, 95)])
        self.assertEqual(self.code(self.payload5(95), late), GREEN)

    def test_weekly_examples(self):
        # Monday 10:00, the window resets Saturday 13:00, every day weighs 1: 16.2 points are
        # allowed today, and the last hour's rate runs 14 hours to midnight.
        for then, code in ((10, GREEN), (8.5, YELLOW), (8, RED)):
            with self.subTest(then=then):
                self.seed([(WMON - 3600, "7d", WR, then)])
                self.assertEqual(self.code(self.payload7(10, WR), WMON), code)

    def test_without_an_old_enough_sample_the_earliest_is_used(self):
        self.seed([(NOW - 1800, "5h", R, 30)])
        self.assertEqual(self.code(self.payload5(60), NOW), RED)    # 60 + 30 / 1800 * 7200 = 180

    def test_under_fifteen_minutes_falls_back_to_pace_color(self):
        for used, code in ((40, GREEN), (63, YELLOW), (70, RED)):
            with self.subTest(used=used):
                self.seed([(NOW - 899, "5h", R, used)])       # a flat 899 s would be green if it counted
                want = self.cap.pace_color(used, R - NOW, 18000, 5)
                self.assertEqual(want, code)
                self.assertEqual(self.code(self.payload5(used), NOW), want)

    def weekly_text(self, data, now):
        self.cap.record(data, now)
        return self.cap.render(data, now, None, self.cap.merged(data, now))

    def test_weekly_without_history_is_green_with_the_allowance(self):
        text = self.cap.render(self.payload7(37, WR), WMON, None, {})
        self.assertIn("\x1b[32mWeekly: 37% · today 0/11%", text)

    def test_weekly_with_ten_minutes_of_history_projects_nothing(self):
        self.seed([(WMON - 600, "7d", WR, 30)])           # 7 points in ten minutes would be red if it counted
        self.assertIn("\x1b[32mWeekly: 37% · today 0/11%", self.weekly_text(self.payload7(37, WR), WMON))

    def test_weekly_full_is_red_even_without_history(self):
        text = self.cap.render(self.payload7(100, WR), WMON, None, {})
        self.assertIn("\x1b[31mWeekly: 100%", text)

    def test_weekly_with_twenty_minutes_of_history_is_projected(self):
        for then, now_u, code in ((36, 37, RED), (37, 37, GREEN)):
            with self.subTest(then=then):
                self.seed([(WMON - 1200, "7d", WR, then)])
                self.assertEqual(self.code(self.payload7(now_u, WR), WMON), code)

    def test_five_hour_without_history_keeps_its_pace_color(self):
        text = self.cap.render(self.payload5(70), NOW, None, {})
        self.assertIn(f"\x1b[{self.cap.pace_color(70, R - NOW, 18000, 5)}mUsage: 70%", text)

    def test_weekly_is_plain_under_no_color(self):
        os.environ["NO_COLOR"] = "1"
        text = self.cap.render(self.payload7(37, WR), WMON, None, {})
        self.assertEqual(text, "Weekly: 37% · today 0/11%")

    def test_full_is_red(self):
        self.seed([(NOW - 3600, "5h", R, 100)])
        self.assertEqual(self.code(self.payload5(100), NOW), RED)


class Main(Base):
    def test_a_failed_append_still_shows_the_merged_figure(self):
        share = Path(self._tmp.name) / "share"
        share.mkdir()
        (share / "statusline-row.json").write_text('{"row": true}', encoding="utf-8")
        self.cap.SHARE = share
        self.seed([(int(__import__("time").time()) - 5, "5h", R, 38)])
        with mock.patch.object(self.cap, "record", side_effect=OSError("read-only")):
            out = self.cap.row(json.dumps(self.payload5(27)).encode("utf-8"))
        self.assertIn("Usage: 38%", out.decode("utf-8"))

    def test_an_unwritable_state_still_draws_the_row(self):
        blocker = Path(self._tmp.name) / "blocker"
        blocker.write_text("a file where the directory should be", encoding="utf-8")
        self.cap.STATE = blocker
        self.cap.HISTORY = blocker / "history.jsonl"
        share = Path(self._tmp.name) / "share"
        share.mkdir()
        (share / "statusline-row.json").write_text('{"row": true}', encoding="utf-8")
        self.cap.SHARE = share
        out = self.cap.row(json.dumps(payload(**limits(five=40, week=20))).encode("utf-8"))
        self.assertIsNotNone(out)
        self.assertIn("Usage: 40%", out.decode("utf-8"))

    def test_row_records_and_merges(self):
        share = Path(self._tmp.name) / "share"
        share.mkdir()
        (share / "statusline-row.json").write_text('{"row": true}', encoding="utf-8")
        self.cap.SHARE = share
        self.seed([(int(__import__("time").time()) - 5, "5h", R, 38)])
        out = self.cap.row(json.dumps(self.payload5(27)).encode("utf-8"))
        self.assertIn("Usage: 38%", out.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
