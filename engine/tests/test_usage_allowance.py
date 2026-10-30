"""capture.py's weekly part: a daily allowance weighted per weekday and per profile, colored by where
today's pace ends the day. Dates are built with mktime on local dates, so the tests hold in any zone."""
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

from test_usage_capture import load_capture  # noqa: E402

GREEN, YELLOW, RED, GREY = "32", "33", "31", "38;5;243"
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def at(y, m, d, h=0, mi=0):
    return time.mktime((y, m, d, h, mi, 0, 0, 0, -1))


MON = at(2026, 10, 26, 10)            # Monday 10:00
THU = at(2026, 10, 29, 10)
SAT = at(2026, 10, 31, 8)
R = at(2026, 10, 31, 13)              # the reset: Saturday 13:00
WEIGHTS = {d: 1.0 for d in DAYS} | {"sat": 0.4}
WORK = "~/.claude-work"


class Base(unittest.TestCase):
    def setUp(self):
        self.cap = load_capture()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.share = Path(self._tmp.name) / "share"
        self.state = Path(self._tmp.name) / "a home" / "state"
        self.share.mkdir()
        self.cap.SHARE = self.share
        self.cap.WEEK_SETTINGS = self.share / "usage-week.toml"
        self.cap.STATE = self.state
        self.cap.HISTORY = self.state / "history.jsonl"
        self.cap.DAYS = self.state / "days.json"
        self.cap.DAY_TIMES = self.state / "day-times.json"
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("NO_COLOR", None)
        os.environ.pop("COLUMNS", None)

    def toml(self, text):
        self.cap.WEEK_SETTINGS.write_text(text, encoding="utf-8")

    def part(self, used, now, start, rate=0.0, weights=WEIGHTS, day_end=1440, cut=0.1, r=R,
             since=None):
        """weekly_part for a figure that rose `rate` percent points over the last hour. `since` is
        when today's first figure was seen; without it the week is a four-tuple."""
        samples = [(now - 3600, used - rate), (now, used)] if rate else []
        week = (weights, day_end, cut, start) if since is None else (weights, day_end, cut, start, since)
        return self.cap.weekly_part(used, r, now, samples, week)


class Allowance(Base):
    def test_the_worked_example(self):
        got, ends, zero = self.cap.allowance(10, R, MON, WEIGHTS, 1440, 10)
        self.assertAlmostEqual(got, 90 / (5 + 0.4 * 13 / 24), places=6)
        self.assertAlmostEqual(got, 17.25, places=2)
        self.assertEqual((ends, zero), (at(2026, 10, 27), False))

    def test_the_reset_day_is_pro_rated(self):
        friday = at(2026, 10, 30, 10)
        got, _, _ = self.cap.allowance(50, R, friday, WEIGHTS, 1440, 50)
        self.assertAlmostEqual(got, 50 / (1 + 0.4 * 13 / 24), places=6)

    def test_the_day_end_is_capped_at_the_reset(self):
        self.assertEqual(self.cap.allowance(10, R, SAT, WEIGHTS, 1440, 10)[1], R)
        self.assertEqual(self.cap.allowance(10, R, SAT, WEIGHTS, 18 * 60, 10)[1], R)
        self.assertEqual(self.cap.allowance(10, R, MON, WEIGHTS, 18 * 60, 10)[1], at(2026, 10, 26, 18))

    def test_a_window_opened_today_starts_at_zero(self):
        reset, now = at(2026, 11, 2, 13), at(2026, 10, 26, 15)
        opened = reset - 604800
        flat = {d: 1.0 for d in DAYS}
        zero = self.cap.allowance(50, reset, now, flat, 1440, 0)[0]
        today = (at(2026, 10, 27) - opened) / (at(2026, 10, 27) - at(2026, 10, 26))
        last = (reset - at(2026, 11, 2)) / (at(2026, 11, 3) - at(2026, 11, 2))
        self.assertAlmostEqual(zero, 100 * today / (6 + today + last), places=6)
        self.assertEqual(self.cap.allowance(50, reset, now, flat, 1440, 50)[0], zero)
        self.assertEqual(self.cap.allowance(50, reset, now, flat, 1440, None)[0], zero)

    def test_none_start_means_the_figure_now(self):
        self.assertEqual(self.cap.allowance(10, R, MON, WEIGHTS, 1440, None),
                         self.cap.allowance(10, R, MON, WEIGHTS, 1440, 10))

    def test_a_zero_weight_day_is_a_zero_day(self):
        got, _, zero = self.cap.allowance(10, R, MON, WEIGHTS | {"mon": 0}, 1440, 10)
        self.assertEqual((got, zero), (0.0, True))

    def test_nothing_left_to_share_is_a_zero_day(self):
        friday = at(2026, 10, 30, 10)
        off = WEIGHTS | {"fri": 0, "sat": 0}
        self.assertEqual(self.cap.allowance(10, R, friday, off, 1440, 10)[::2], (0.0, True))

    @unittest.skipUnless(hasattr(time, "tzset"), "time.tzset is missing on Windows, so TZ cannot be fixed")
    def test_a_twenty_five_hour_day_divides_by_twenty_five(self):
        old = os.environ.get("TZ")

        def restore():
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()

        self.addCleanup(restore)
        os.environ["TZ"] = "America/Chicago"
        time.tzset()
        reset, now = at(2026, 11, 8, 13), at(2026, 11, 1, 15)        # 2026-11-01 has 25 hours
        self.assertEqual(at(2026, 11, 2) - at(2026, 11, 1), 25 * 3600)
        flat = {d: 1.0 for d in DAYS}
        got = self.cap.allowance(0, reset, now, flat, 1440, 0)[0]
        self.assertAlmostEqual(got, 100 * (11 / 25) / (6 + 11 / 25 + 13 / 24), places=6)


class Settings(Base):
    def test_no_file_is_the_defaults(self):
        self.assertEqual(self.cap.week_settings(""), ({d: 1.0 for d in DAYS}, 1440, 0.10))

    def test_the_file_and_a_profile_over_it(self):
        self.toml('[weekly]\nweights = { mon = 2, sat = 0.4 }\nday_end = "20:00"\nlater_cut = 0.2\n'
                  f'[weekly.profiles."{WORK}"]\nweights = {{ mon = 1, sat = 0, sun = 0 }}\nday_end = "18:00"\n')
        weights, end, cut = self.cap.week_settings("")
        self.assertEqual((weights["mon"], weights["sat"], weights["tue"], end, cut), (2, 0.4, 1, 1200, 0.2))
        work = os.path.abspath(os.path.expanduser(WORK))
        weights, end, cut = self.cap.week_settings(work)
        self.assertEqual((weights["mon"], weights["sat"], weights["sun"], end, cut), (1, 0, 0, 1080, 0.2))
        self.assertEqual(weights["wed"], 1)                  # weights are replaced whole: the rest weigh 1

    def test_profile_keys_match_without_case_on_a_case_blind_system(self):
        self.toml('[weekly.profiles."~/.claude-work"]\nday_end = "18:00"\n')
        with mock.patch.object(self.cap.os.path, "normcase", str.lower), \
                mock.patch.object(self.cap.os.path, "expanduser", lambda p: p.replace("~", "/H")):
            self.assertEqual(self.cap.week_settings("/h/.claude-work")[1], 1080)
            self.assertEqual(self.cap.week_settings("/H/.Claude-Work".lower())[1], 1080)

    def test_a_bom_settings_file_is_read(self):
        self.cap.WEEK_SETTINGS.write_bytes(b"\xef\xbb\xbf[weekly]\nweights = { sat = 0.4 }\n")
        self.assertEqual(self.cap.week_settings("")[0]["sat"], 0.4)

    def test_profile_keys_match_without_case_on_a_case_blind_system(self):
        self.toml('[weekly.profiles."~/.claude-work"]\nday_end = "18:00"\n')
        with mock.patch.object(self.cap.os.path, "normcase", str.lower), \
                mock.patch.object(self.cap.os.path, "expanduser", lambda p: p.replace("~", "/H")):
            self.assertEqual(self.cap.week_settings(os.path.abspath("/H/.Claude-Work"))[1], 1080)

    def test_a_bom_settings_file_is_read(self):
        self.cap.WEEK_SETTINGS.write_bytes(b"\xef\xbb\xbf[weekly]\nweights = { sat = 0.4 }\n")
        self.assertEqual(self.cap.week_settings("")[0]["sat"], 0.4)

    def test_the_default_profile_matches_dot_claude(self):
        self.toml('[weekly]\nlater_cut = 0.2\n[weekly.profiles."~/.claude"]\nlater_cut = 0.5\n'
                  'day_end = "17:30"\n')
        self.assertEqual(self.cap.week_settings("")[1:], (1050, 0.5))
        self.assertEqual(self.cap.week_settings(os.path.abspath(os.path.expanduser("~/.claude")))[1:], (1050, 0.5))
        self.assertEqual(self.cap.week_settings(os.path.abspath(os.path.expanduser(WORK)))[1:], (1440, 0.2))

    def test_a_bad_value_is_its_default_and_a_bad_file_is_all_defaults(self):
        defaults = ({d: 1.0 for d in DAYS}, 1440, 0.10)
        for text in ('[weekly\nbroken', '[weekly]\nweights = 3\n'):
            with self.subTest(text=text):
                self.toml(text)
                self.assertEqual(self.cap.week_settings(""), defaults)
        self.toml('[weekly]\nweights = { mon = 11, tue = true, wed = "x", thu = -1, fri = 10 }\n'
                  'day_end = "25:00"\nlater_cut = 2\n')
        weights, end, cut = self.cap.week_settings("")
        self.assertEqual([weights[d] for d in DAYS[:5]], [1, 1, 1, 1, 10])
        self.assertEqual((end, cut), (1440, 0.10))
        for bad in ('"00:00"', '"12:60"', '"6"', "7", '"24:30"'):
            with self.subTest(day_end=bad):
                self.toml(f"[weekly]\nday_end = {bad}\n")
                self.assertEqual(self.cap.week_settings("")[1], 1440)
        self.toml('[weekly]\nlater_cut = true\n')
        self.assertEqual(self.cap.week_settings("")[2], 0.10)
        self.toml('[weekly]\nlater_cut = 0\n')
        self.assertEqual(self.cap.week_settings("")[2], 0)

    def test_an_unreadable_file_is_all_defaults(self):
        self.cap.WEEK_SETTINGS.write_bytes(b"\xff\xfe[weekly]\n")
        self.assertEqual(self.cap.week_settings(""), ({d: 1.0 for d in DAYS}, 1440, 0.10))


class Color(Base):
    def rows(self):
        """(now, start, allowance, rest, hours to the day's end) of the three worked rows."""
        return ((MON, 10, 17.25, 72.75, 14), (THU, 60, 18.05, 21.95, 14), (SAT, 96, 4.0, 0.0, 5))

    def test_the_worked_rows_just_inside_and_outside_the_band_edges(self):
        for now, start, allow, rest, hours in self.rows():
            exact = self.cap.allowance(start, R, now, WEIGHTS, 1440, start)[0]
            with self.subTest(start=start):
                self.assertAlmostEqual(exact, allow, delta=0.01)
                self.assertAlmostEqual(100 - start - exact, rest, delta=0.01)
                green, yellow = exact / hours, (exact + 0.1 * (100 - start - exact)) / hours
                self.assertEqual(self.part(start, now, start, green - 0.001)[1], GREEN)
                self.assertEqual(self.part(start, now, start, yellow + 0.001)[1], RED)
                if rest > 0:
                    self.assertEqual(self.part(start, now, start, green + 0.001)[1], YELLOW)
                    self.assertEqual(self.part(start, now, start, yellow - 0.001)[1], YELLOW)
                else:
                    self.assertEqual(self.part(start, now, start, green + 0.001)[1], RED)

    def test_the_text(self):
        text, color = self.part(13, MON, 10)
        self.assertEqual((text, color), ("Weekly: 13% · today 3/17%", GREEN))

    def test_later_cut_changes_the_band(self):
        rate = (17.2519 + 10) / 14                    # an overrun of 10: past 7.27, inside 0.5 of 72.75
        self.assertEqual(self.part(10, MON, 10, rate, cut=0.1)[1], RED)
        self.assertEqual(self.part(10, MON, 10, rate, cut=0.5)[1], YELLOW)
        self.toml(f'[weekly]\nlater_cut = 0.1\n[weekly.profiles."{WORK}"]\nlater_cut = 0.5\n')
        work = os.path.abspath(os.path.expanduser(WORK))
        self.assertEqual(self.cap.week_settings(work)[2], 0.5)
        self.assertEqual(self.cap.week_settings("")[2], 0.1)

    def test_a_payload_past_its_reset_is_a_grey_figure_only(self):
        self.assertEqual(self.part(40, R + 600, 10), ("Weekly: 40%", GREY))
        self.assertEqual(self.part(100, R + 600, 10), ("Weekly: 100%", RED))

    def test_a_payload_past_its_reset_is_a_grey_figure_only(self):
        self.assertEqual(self.part(40, R + 600, 10), ("Weekly: 40%", GREY))
        self.assertEqual(self.part(100, R + 600, 10), ("Weekly: 100%", RED))

    def test_a_full_week_is_red(self):
        self.assertEqual(self.part(100, MON, 10)[1], RED)
        self.assertEqual(self.part(99.6, MON, 10)[1], RED)
        self.assertEqual(self.part(100, MON, 10, weights=WEIGHTS | {"mon": 0})[1], RED)

    def test_a_projection_to_a_full_week_is_red(self):
        self.assertEqual(self.part(90, MON, 90, 10)[1], RED)

    def test_off_day_text_and_colors(self):
        off = WEIGHTS | {"mon": 0}
        self.assertEqual(self.part(13, MON, 10, weights=off), ("Weekly: 13% · off day 3%", YELLOW))
        self.assertEqual(self.part(10.4, MON, 10, weights=off), ("Weekly: 10% · off day", GREY))
        self.assertEqual(self.part(100, MON, 10, weights=off)[1], RED)

    def test_nothing_left_to_share_behaves_as_an_off_day(self):
        friday = at(2026, 10, 30, 10)
        off = WEIGHTS | {"fri": 0, "sat": 0}
        self.assertEqual(self.part(13, friday, 10, weights=off), ("Weekly: 13% · off day 3%", YELLOW))
        self.assertEqual(self.part(10, friday, 10, weights=off), ("Weekly: 10% · off day", GREY))
        off = {d: 0 for d in DAYS}
        self.assertEqual(self.part(10, MON, 10, weights=off)[1], GREY)

    def test_after_the_day_end_nothing_is_projected(self):
        evening = at(2026, 10, 26, 19)
        self.assertEqual(self.part(10, evening, 10, 5.0, day_end=1440)[1], RED)
        self.assertEqual(self.part(10, evening, 10, 5.0, day_end=18 * 60)[1], GREEN)
        self.assertEqual(self.cap.allowance(10, R, evening, WEIGHTS, 18 * 60, 10)[1], at(2026, 10, 26, 18))

    def test_less_than_fifteen_minutes_of_history_projects_nothing(self):
        samples = [(MON - 600, 1)]
        text, color = self.cap.weekly_part(10, R, MON, samples, (WEIGHTS, 1440, 0.1, 10))
        self.assertEqual(color, GREEN)
        self.assertEqual(self.cap.weekly_part(10, R, MON, [], (WEIGHTS, 1440, 0.1, 10))[1], GREEN)

    def test_the_figure_is_the_freshest_any_session_saw(self):
        text, _ = self.cap.weekly_part(10, R, MON, [(MON - 3600, 12), (MON, 13)], (WEIGHTS, 1440, 0.1, 10))
        self.assertTrue(text.startswith("Weekly: 13% · today 3/"))

    def test_no_color_prints_no_codes(self):
        os.environ["NO_COLOR"] = "1"
        data = {"rate_limits": {"seven_day": {"used_percentage": 13, "resets_at": R}}}
        text = self.cap.render(data, MON, None, {}, None, (WEIGHTS, 1440, 0.1, 10))
        self.assertEqual(text, "Weekly: 13% · today 3/17%")


class Paced(Base):
    """Thu 10:00, 60% at the day's start, 61% at 09:00, 63% now: the last hour reads 2 points an hour
    (proj 91), the day's own pace 3 points in 3.5 hours (proj 75). The yellow edge is 80.25."""

    def test_a_quiet_day_paced_since_its_first_figure_is_green(self):
        first = at(2026, 10, 29, 6, 30)
        self.assertEqual(self.part(63, THU, 60, rate=2, since=first)[1], GREEN)
        self.assertEqual(self.part(63, THU, 60, rate=2)[1], RED)

    def test_under_fifteen_minutes_since_the_first_figure_uses_the_last_hour(self):
        self.assertEqual(self.part(63, THU, 63, rate=2, since=THU - 600)[1], RED)

    def test_since_without_a_start_uses_the_last_hour(self):
        first = at(2026, 10, 29, 6, 30)
        self.assertEqual(self.part(63, THU, None, rate=2, since=first),
                         self.part(63, THU, None, rate=2))

    def test_a_four_tuple_week_is_since_none(self):
        samples = [(THU - 3600, 61), (THU, 63)]
        four = self.cap.weekly_part(63, R, THU, samples, (WEIGHTS, 1440, 0.1, 60))
        five = self.cap.weekly_part(63, R, THU, samples, (WEIGHTS, 1440, 0.1, 60, None))
        self.assertEqual(four, five)


class Row(Base):
    def run_row(self, used, week_toml=None):
        (self.share / "statusline-row.json").write_text('{"row": true}', encoding="utf-8")
        if week_toml:
            self.toml(week_toml)
        self.state.mkdir(parents=True, exist_ok=True)
        today = time.strftime("%Y-%m-%d", time.localtime(MON))
        self.cap.DAYS.write_text(json.dumps({self.cap.PROFILE: {today: [[int(R), 10, 10]]}}), encoding="utf-8")
        data = {"rate_limits": {"seven_day": {"used_percentage": used, "resets_at": R}}}
        with mock.patch.object(self.cap.time, "time", return_value=MON):
            return self.cap.row(json.dumps(data).encode("utf-8")).decode("utf-8")

    def test_the_worked_example_through_the_row(self):
        weights = "[weekly]\nweights = { mon = 1, tue = 1, wed = 1, thu = 1, fri = 1, sat = 0.4, sun = 1 }\n"
        self.assertEqual(self.run_row(10, weights), "\x1b[32mWeekly: 10% · today 0/17%\x1b[0m\n")

    def test_a_zero_weight_day_through_the_row(self):
        self.assertEqual(self.run_row(13, "[weekly]\nweights = { mon = 0 }\n"),
                         "\x1b[33mWeekly: 13% · off day 3%\x1b[0m\n")

    def test_a_bad_settings_file_still_draws_the_row(self):
        text = self.run_row(10, "[weekly\nbroken")
        self.assertTrue(text.startswith("\x1b[32mWeekly: 10% · today 0/16%"))


if __name__ == "__main__":
    unittest.main()
