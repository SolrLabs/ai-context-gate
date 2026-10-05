"""Usage alerts: steps, break points, the injection text and the per-call hook logic.
    python3 -m unittest discover -s engine/tests -k usage
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))

from govern import usage  # noqa: E402
from govern.usage import Alert, AlertsError  # noqa: E402

NOW = 1_790_000_000
WEEK = NOW + 3 * 86400
FIVE = NOW + 4 * 3600


def clock(epoch, fmt):
    return time.strftime(fmt, time.localtime(epoch))


def snap(ctx=None, h5=None, d7=None, h5_reset=FIVE, d7_reset=WEEK, at=NOW):
    rl = {}
    if h5 is not None:
        rl["five_hour"] = {"used_percentage": h5, "resets_at": h5_reset}
    if d7 is not None:
        rl["seven_day"] = {"used_percentage": d7, "resets_at": d7_reset}
    s = {"context_window": {"used_percentage": ctx}, "captured_at": at, "transcript_path": "t"}
    if rl:
        s["rate_limits"] = rl
    return s


class Steps(unittest.TestCase):
    def test_edges(self):
        self.assertEqual([usage.step(v) for v in (0, 4.9, 5, 89.9, 90, 90.6, 99.9, 100)],
                         [0, 0, 5, 85, 90, 90, 99, 100])

    def test_clamped(self):
        self.assertEqual((usage.step(-3), usage.step(104)), (0, 100))

    def test_finer_step_below_90_only(self):
        self.assertEqual([usage.step(v, 2) for v in (0, 3.9, 37, 89.9, 90, 91.5)],
                         [0, 2, 36, 88, 90, 91])


class ParseAlerts(unittest.TestCase):
    def test_valid(self):
        a = usage.parse_alerts('[[alert]]\nsignal = "seven_day"\nat = 93\nsay = "At {pct}%, resets {resets}."\n')
        self.assertEqual(a, [Alert("seven_day", 93, "At {pct}%, resets {resets}.")])

    def test_empty_file_is_no_alerts(self):
        self.assertEqual(usage.parse_alerts(""), [])

    def test_invalid(self):
        bad = {
            "signal": '[[alert]]\nsignal = "daily"\nat = 5\nsay = "x"\n',
            "at": '[[alert]]\nsignal = "context"\nat = 101\nsay = "x"\n',
            "at type": '[[alert]]\nsignal = "context"\nat = 9.5\nsay = "x"\n',
            "say": '[[alert]]\nsignal = "context"\nat = 5\nsay = ""\n',
            "placeholder": '[[alert]]\nsignal = "context"\nat = 5\nsay = "{when}"\n',
            "duplicate": '[[alert]]\nsignal = "context"\nat = 5\nsay = "a"\n'
                         '[[alert]]\nsignal = "context"\nat = 5\nsay = "b"\n',
            "unknown key": '[[alert]]\nsignal = "context"\nat = 5\nsay = "a"\nstop = true\n',
            "toml": '[[alert]\n',
            "alert not a list": 'alert = 5\n',
            "format spec": '[[alert]]\nsignal = "context"\nat = 5\nsay = "{resets:d}"\n',
            "conversion": '[[alert]]\nsignal = "context"\nat = 5\nsay = "{pct!x}"\n',
            "nested field": '[[alert]]\nsignal = "context"\nat = 5\nsay = "{pct:{resets}}"\n',
        }
        for name, text in bad.items():
            with self.subTest(name), self.assertRaises(AlertsError):
                usage.parse_alerts(text)

    def test_context_step(self):
        self.assertEqual(usage.parse_file(""), ([], 5))
        self.assertEqual(usage.parse_file("context_step = 1\n"), ([], 1))
        self.assertEqual(usage.parse_file('context_step = 10\n[[alert]]\nsignal = "context"\n'
                                          'at = 60\nsay = "x"\n'), ([Alert("context", 60, "x")], 10))
        for bad in ("0", "11", "2.5", "true", '"2"'):
            with self.subTest(bad), self.assertRaisesRegex(AlertsError, "context_step"):
                usage.parse_file(f"context_step = {bad}\n")


class DataLine(unittest.TestCase):
    def test_all_present(self):
        line = usage.data_line({"context": 34.2, "five_hour": 12, "seven_day": 93},
                               {"five_hour": FIVE, "seven_day": WEEK})
        self.assertEqual(line, f"usage: context 34% · account 5h 12% (resets {clock(FIVE, '%H:%M')})"
                               f" · 7d 93% (resets {clock(WEEK, '%a %H:%M')})")

    def test_rate_limits_absent_is_pending(self):
        self.assertEqual(usage.data_line({"context": 7}, {}),
                         "usage: context 7% · account 5h pending · 7d pending")

    def test_context_absent_is_pending(self):
        self.assertEqual(usage.data_line({}, {}), "usage: context pending · account 5h pending · 7d pending")

    def test_trend(self):
        line = lambda pts, secs: usage.data_line({"context": 37}, {}, (pts, secs)).split(" · ")[0]
        self.assertEqual(line(5, 80 * 60), "usage: context 37% (+5% in 1h20m)")
        self.assertEqual(line(10, 2 * 3600 + 59), "usage: context 37% (+10% in 2h)")
        self.assertEqual(line(3, 45 * 60 + 30), "usage: context 37% (+3% in 45m)")
        self.assertEqual(line(0, 3600), "usage: context 37%")


class Evaluate(unittest.TestCase):
    A93 = Alert("seven_day", 93, "Weekly at {pct}%.")
    A96 = Alert("seven_day", 96, "Past the ceiling at {pct}%.")
    C10 = Alert("context", 10, "Context at {pct}%.")

    def run_(self, s, alerts=(), state=None):
        return usage.evaluate(s, list(alerts), state or {}, NOW, "usage-alerts.toml")

    def test_first_call_is_a_baseline(self):
        text, st = self.run_(snap(ctx=7, h5=1, d7=40))
        self.assertTrue(text.startswith("usage: context 7% · account 5h 1% (resets "))
        self.assertEqual(st["steps"], {"context": 5, "five_hour": 0, "seven_day": 40})

    def test_rule_only_on_the_first_line(self):
        text, st = self.run_(snap(ctx=7, h5=1, d7=40))
        self.assertTrue(text.endswith(" (lines come when a value rises a step or a window resets)"))
        text, _ = self.run_(snap(ctx=12, h5=1, d7=40), state=st)
        self.assertNotIn("lines come", text)

    def test_rule_waits_for_the_first_line_sent(self):
        _, st = self.run_(snap())                   # nothing to say yet: no line, no rule
        text, _ = self.run_(snap(ctx=7), state=st)
        self.assertIn("lines come", text)

    def test_rule_ends_the_data_line_after_the_alerts(self):
        text, _ = self.run_(snap(ctx=12), [self.C10])
        self.assertTrue(text.endswith("\n\nusage: context 12% · account 5h pending · 7d pending" + usage.RULE),
                        text)

    def test_ub13_one_alert_first_then_the_data_line(self):
        _, st = self.run_(snap(ctx=7, d7=92), [self.A93])
        text, _ = self.run_(snap(ctx=7, d7=93), [self.A93], st)
        self.assertEqual(text, "⚠ usage alert (owner's prompt, usage-alerts.toml):\nWeekly at 93%.\n\n"
                               f"usage: context 7% · account 5h pending · 7d 93% (resets {clock(WEEK, '%a %H:%M')})")

    def test_ub13_two_alerts_each_marked_blank_lines_between_data_line_last(self):
        _, st = self.run_(snap(ctx=7, d7=92), [self.A93, self.A96])
        text, _ = self.run_(snap(ctx=7, d7=97), [self.A96, self.A93], st)
        head = "⚠ usage alert (owner's prompt, usage-alerts.toml):\n"
        self.assertEqual(text, f"{head}Weekly at 97%.\n\n{head}Past the ceiling at 97%.\n\n"
                               f"usage: context 7% · account 5h pending · 7d 97% (resets {clock(WEEK, '%a %H:%M')})")

    def test_context_trend_from_the_first_reading(self):
        _, st = self.run_(snap(ctx=32, h5=1, d7=40))
        self.assertEqual((st["ctx0"], st["t0"]), (32, NOW))
        text, st = usage.evaluate(snap(ctx=37.5, h5=1, d7=40), [], st, NOW + 80 * 60, None)
        self.assertTrue(text.startswith("usage: context 37% (+5% in 1h20m) · account 5h 1% (resets"))
        self.assertEqual((st["ctx0"], st["t0"]), (32, NOW))

    def test_trend_after_a_drop_counts_from_the_drop(self):
        _, st = self.run_(snap(ctx=40))
        _, st = usage.evaluate(snap(ctx=12), [], st, NOW + 3600, None)
        self.assertEqual((st["ctx0"], st["t0"]), (12, NOW + 3600))
        text, _ = usage.evaluate(snap(ctx=21), [], st, NOW + 3600 + 45 * 60, None)
        self.assertTrue(text.startswith("usage: context 21% (+9% in 45m) ·"))

    def test_trend_waits_for_a_context_reading(self):
        _, st = self.run_(snap(d7=40))
        self.assertNotIn("ctx0", st)
        _, st = usage.evaluate(snap(ctx=20, d7=40), [], st, NOW + 600, None)
        self.assertEqual((st["ctx0"], st["t0"]), (20, NOW + 600))

    def test_no_rate_or_delta_for_account_windows(self):
        _, st = self.run_(snap(ctx=5, h5=10, d7=40))
        text, _ = usage.evaluate(snap(ctx=5, h5=30, d7=60), [], st, NOW + 3600, None)
        self.assertEqual(text, f"usage: context 5% · account 5h 30% (resets {clock(FIVE, '%H:%M')})"
                               f" · 7d 60% (resets {clock(WEEK, '%a %H:%M')})")

    def test_context_step(self):
        _, st = usage.evaluate(snap(ctx=30), [], {}, NOW, None, 2)
        self.assertEqual(st["steps"]["context"], 30)
        text, st = usage.evaluate(snap(ctx=31.9), [], st, NOW, None, 2)
        self.assertIsNone(text)
        text, st = usage.evaluate(snap(ctx=32), [], st, NOW, None, 2)
        self.assertIn("context 32%", text)
        _, st = usage.evaluate(snap(ctx=12), [], st, NOW, None, 2)   # a drop re-arms on 2s too
        self.assertEqual(st["steps"]["context"], 12)

    def test_context_step_leaves_account_windows_at_5(self):
        _, st = usage.evaluate(snap(h5=30, d7=40), [], {}, NOW, None, 1)
        text, _ = usage.evaluate(snap(h5=33, d7=44), [], st, NOW, None, 1)
        self.assertIsNone(text)

    def test_same_step_is_silent(self):
        _, st = self.run_(snap(ctx=7, h5=1, d7=40))
        text, _ = self.run_(snap(ctx=9, h5=2, d7=44), state=st)
        self.assertIsNone(text)

    def test_every_point_from_90(self):
        _, st = self.run_(snap(d7=90))
        text, _ = self.run_(snap(d7=91), state=st)
        self.assertIn("7d 91%", text)

    def test_alert_fires_once_with_prefix(self):
        _, st = self.run_(snap(d7=92), [self.A93])
        text, st = self.run_(snap(d7=93), [self.A93], st)
        self.assertIn("⚠ usage alert (owner's prompt, usage-alerts.toml):\nWeekly at 93%.", text)
        text, st = self.run_(snap(d7=93.5), [self.A93], st)
        self.assertIsNone(text)

    def test_fraction_does_not_round_up(self):
        _, st = self.run_(snap(d7=92), [self.A93])
        text, _ = self.run_(snap(d7=92.7), [self.A93], st)
        self.assertIsNone(text)

    def test_jump_sends_all_crossed_in_order(self):
        _, st = self.run_(snap(ctx=5, d7=91), [self.A96, self.A93, self.C10])
        text, _ = self.run_(snap(ctx=12, d7=97), [self.A96, self.A93, self.C10], st)
        self.assertLess(text.index("Context at 12%"), text.index("Weekly at 97%"))
        self.assertLess(text.index("Weekly at 97%"), text.index("Past the ceiling at 97%"))
        self.assertEqual(text.count("⚠ usage alert"), 3)

    def test_new_window_rearms(self):
        _, st = self.run_(snap(d7=94), [self.A93])
        text, _ = self.run_(snap(d7=94, d7_reset=WEEK + 7 * 86400), [self.A93], st)
        self.assertIn("Weekly at 94%.", text)

    def test_same_window_within_120s_does_not_rearm(self):
        _, st = self.run_(snap(d7=94), [self.A93])
        text, _ = self.run_(snap(d7=94, d7_reset=WEEK + 1), [self.A93], st)
        self.assertIsNone(text)                 # the seeded value, then the session's own
        text, _ = self.run_(snap(d7=94, d7_reset=WEEK + 3600), [self.A93], st)
        self.assertIn("Weekly at 94%.", text)

    def test_float_dip_keeps_the_trend_start(self):
        _, st = self.run_(snap(ctx=12.9))
        _, st = self.run_(snap(ctx=12.2), state=st)
        self.assertEqual(st["ctx0"], 12.9)
        _, st = self.run_(snap(ctx=11.9), state=st)
        self.assertEqual(st["ctx0"], 11.9)

    def test_context_drop_rearms(self):
        _, st = self.run_(snap(ctx=12), [self.C10])
        _, st = self.run_(snap(ctx=3), [self.C10], st)
        self.assertEqual(st["steps"]["context"], 0)
        text, _ = self.run_(snap(ctx=11), [self.C10], st)
        self.assertIn("Context at 11%.", text)

    def test_expired_window_is_absent(self):
        text, _ = self.run_(snap(ctx=7, d7=95, d7_reset=NOW - 1), [self.A93])
        self.assertIn("7d pending", text)
        self.assertNotIn("Weekly", text)

    def test_no_rate_limits(self):
        text, _ = self.run_(snap(ctx=7), [self.A93])
        self.assertEqual(text, "usage: context 7% · account 5h pending · 7d pending" + usage.RULE)

    def test_resets_placeholder(self):
        a = Alert("seven_day", 93, "resets {resets}")
        text, _ = self.run_(snap(d7=93), [a])
        self.assertRegex(text.split("\n\n")[0], r"resets \w{3} \d\d:\d\d$")


class OnCall(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def put(self, resolved=None, s=None):
        if resolved is not None:
            usage.write_json(usage.resolved_path(self.home, "S"), resolved)
        if s is not None:
            usage.write_json(usage.snapshot_path(self.home, "S"), s)

    def test_disabled_or_unresolved_is_silent(self):
        self.put(s=snap(ctx=7))
        self.assertIsNone(usage.on_call(self.home, "S", NOW))
        self.put(resolved={"enabled": False, "alerts": [], "alerts_file": None, "error": None})
        self.assertIsNone(usage.on_call(self.home, "S", NOW))

    def test_enabled_baseline_then_silent(self):
        self.put({"enabled": True, "alerts": [], "alerts_file": None, "error": None}, snap(ctx=7))
        self.assertEqual(usage.on_call(self.home, "S", NOW),
                         "usage: context 7% · account 5h pending · 7d pending" + usage.RULE)
        self.assertIsNone(usage.on_call(self.home, "S", NOW))

    def other(self, sid, s, mtime=None):
        usage.write_json(usage.snapshot_path(self.home, sid), s)
        if mtime is not None:
            import os
            os.utime(usage.snapshot_path(self.home, sid), (mtime, mtime))

    def test_newest_fresh_session_wins_whatever_the_listing_order(self):
        for older, newer in (("A", "Z"), ("Z", "A")):
            with self.subTest(older=older):
                for p in usage.state_dir(self.home).glob("*"):
                    p.unlink()
                self.put({"enabled": True, "alerts": [], "alerts_file": None, "error": None}, snap(ctx=7))
                self.other(older, snap(h5=10, d7=40, at=NOW - 60))
                self.other(newer, snap(h5=12, d7=56, at=NOW - 5))
                text = usage.on_call(self.home, "S", NOW)
                self.assertIn("account 5h 12% (resets ", text)
                self.assertIn("· 7d 56% (resets ", text)

    def test_account_windows_seeded_from_the_newest_fresh_session(self):
        self.put({"enabled": True, "alerts": [], "alerts_file": None, "error": None}, snap(ctx=7))
        self.other("OLDER", snap(ctx=50, h5=10, d7=40, at=NOW - 60))
        self.other("NEWER", snap(ctx=60, h5=12, d7=56, at=NOW - 5))
        self.other("STALE", snap(ctx=70, h5=99, d7=99, at=NOW - usage.STALE_SECONDS - 1))
        usage.write_json(usage.state_path(self.home, "NEWEST"),     # not a snapshot: ignored
                         {"captured_at": NOW, "rate_limits": snap(h5=1, d7=1)["rate_limits"]})
        text = usage.on_call(self.home, "S", NOW)
        self.assertTrue(text.startswith("usage: context 7% · account 5h 12% (resets "), text)
        self.assertIn("· 7d 56% (resets ", text)
        # The real reading arriving in this session's own snapshot, same windows: no repeat line.
        self.put(s=snap(ctx=7, h5=12, d7=56))
        self.assertIsNone(usage.on_call(self.home, "S", NOW + 5))

    def test_each_window_seeded_on_its_own(self):
        self.put({"enabled": True, "alerts": [], "alerts_file": None, "error": None}, snap(ctx=7, d7=60))
        self.other("A", snap(h5=12, at=NOW - 5))
        self.other("B", snap(h5=30, d7=99, at=NOW - 60))
        text = usage.on_call(self.home, "S", NOW)
        self.assertIn("account 5h 12% (resets ", text)
        self.assertIn("· 7d 60% (resets ", text)

    def test_pending_when_no_fresh_session_has_the_window(self):
        self.put({"enabled": True, "alerts": [], "alerts_file": None, "error": None}, snap(ctx=7))
        self.other("STALE", snap(h5=12, d7=56, at=NOW - usage.STALE_SECONDS - 1))
        self.other("OLDFILE", snap(h5=12, d7=56), mtime=NOW - usage.STALE_SECONDS - 1)
        self.other("EXPIRED", snap(h5=12, d7=56, h5_reset=NOW - 1, d7_reset=NOW - 1))
        usage.snapshot_path(self.home, "BROKEN").write_text("{half", encoding="utf-8")
        text = usage.on_call(self.home, "S", NOW)
        self.assertTrue(text.startswith("usage: context 7% · account 5h pending · 7d pending"), text)
        self.assertNotIn("n/a", text)

    def calls(self, start, n, every=30):
        """`n` hook calls `every` seconds apart from `start`: (time, text) for each that said something."""
        times = [start + i * every for i in range(n)]
        return [(t, text) for t in times if (text := usage.on_call(self.home, "S", t)) is not None]

    def fresh_baseline(self, **kw):
        self.put({"enabled": True, "alerts": [], "alerts_file": None, "error": None},
                 snap(ctx=7, h5=1, d7=40, **kw))
        self.assertIsNotNone(usage.on_call(self.home, "S", kw.get("at", NOW)))

    def state(self):
        return usage.read_json(usage.state_path(self.home, "S"))

    def test_stale_told_once_after_ten_active_minutes_then_recovery(self):
        self.fresh_baseline()
        # Every 30 s from 1 s after the capture, no new one: told at the first call with more than
        # 600 s of activity since it (about 10 minutes, not 20).
        said = self.calls(NOW + 1, 60)
        self.assertEqual(said, [(NOW + 601,
                                 f"usage: no fresh usage data for 10+ minutes of activity (last at "
                                 f"{clock(NOW, '%H:%M')}) — if this persists, python3 .context-gate/bin/"
                                 "govern usage install re-wraps the capture")])
        self.put(s=snap(ctx=7, h5=1, d7=40, at=NOW + 1800))     # same values, fresh again
        text = usage.on_call(self.home, "S", NOW + 1800)
        self.assertTrue(text.startswith("usage: context 7% · account 5h 1%"), text)
        self.assertNotIn("lines come", text)
        self.assertNotIn("stale_told", self.state())
        self.assertEqual(self.state()["active_stale"], 0)
        self.assertEqual([t for t, text in self.calls(NOW + 1830, 60) if "no fresh" in text],
                         [NOW + 1800 + 630])                    # stale a second time: told again

    def test_ub11_idle_wait_is_not_stale(self):
        self.fresh_baseline()
        self.assertIsNone(usage.on_call(self.home, "S", NOW + 3600))     # the prompt after an hour
        self.assertIsNone(usage.on_call(self.home, "S", NOW + 3610))     # tool calls 10 s apart
        self.put(s=snap(ctx=7, h5=1, d7=40, at=NOW + 3615))
        self.assertIsNone(usage.on_call(self.home, "S", NOW + 3620))     # recovered: same values
        self.assertEqual(self.state()["active_stale"], 0)
        self.assertNotIn("stale_told", self.state())

    def test_a_new_capture_resets_active_time_even_when_old(self):
        self.fresh_baseline()
        self.calls(NOW + 30, 15)                                    # 450 s of activity
        self.put(s=snap(ctx=12, h5=1, d7=40, at=NOW + 5))           # drawn after the last call
        self.assertIsNone(usage.on_call(self.home, "S", NOW + 3600))   # old: no data line
        self.assertEqual(self.state()["seen_captured"], NOW + 5)
        self.assertEqual(self.state()["active_stale"], 0)
        self.assertIsNone(usage.on_call(self.home, "S", NOW + 3610))   # seen now: counts again
        self.assertEqual(self.state()["active_stale"], 10)

    def test_a_new_old_capture_after_a_told_spell_lets_the_next_spell_warn_and_log_again(self):
        self.fresh_baseline()
        self.assertEqual(len(self.calls(NOW + 1, 30)), 1)           # told stale once
        self.put(s=snap(ctx=12, h5=1, d7=40, at=NOW + 700))         # new, but old by the next call
        self.assertIsNone(usage.on_call(self.home, "S", NOW + 5000))   # idle hour: not fresh, not told
        self.assertNotIn("stale_told", self.state())
        said = self.calls(NOW + 5030, 30)                           # then it dies under steady work
        self.assertEqual([t for t, _ in said], [NOW + 5000 + 630])
        self.assertEqual(usage.log_path(self.home).read_text(encoding="utf-8").count("stale snapshot"), 2)

    def test_negative_active_stale_is_reset(self):
        self.fresh_baseline()
        usage.write_json(usage.state_path(self.home, "S"), {**self.state(), "active_stale": -5000.0})
        usage.on_call(self.home, "S", NOW + 10)
        self.assertEqual(self.state()["active_stale"], 10)

    def test_within_stale_seconds_alone_does_not_reset_active_time(self):
        self.fresh_baseline()
        self.calls(NOW + 30, 10)                                    # 300 s, the capture still fresh
        self.assertEqual(self.state()["active_stale"], 300)
        self.assertEqual(self.state()["seen_captured"], NOW)

    def test_old_snapshot_after_an_idle_wait_is_not_logged(self):
        self.fresh_baseline()
        usage.on_call(self.home, "S", NOW + 3600)
        usage.on_call(self.home, "S", NOW + 3610)
        self.assertNotIn("stale snapshot", usage.log_path(self.home).read_text(encoding="utf-8")
                         if usage.log_path(self.home).exists() else "")
        self.calls(NOW + 3640, 30)                                  # now the agent is told
        self.assertEqual(usage.log_path(self.home).read_text(encoding="utf-8").count("stale snapshot"), 1)

    def test_only_gaps_of_two_minutes_or_less_are_active(self):
        self.fresh_baseline()
        for t in (700, 800, 921, 1041, 1341, 1441):     # gaps 700 100 121 120 300 100
            self.assertIsNone(usage.on_call(self.home, "S", NOW + t))
        self.assertEqual(self.state()["active_stale"], 320)

    def test_negative_or_nan_gap_counts_as_zero(self):
        for last in (float("nan"), NOW + 5000):
            with self.subTest(last=last):
                for p in usage.state_dir(self.home).glob("*"):
                    p.unlink()
                self.fresh_baseline()
                usage.on_call(self.home, "S", NOW + 1000)
                usage.write_json(usage.state_path(self.home, "S"),
                                 {**self.state(), "active_stale": 590, "last_call": last})
                self.assertIsNone(usage.on_call(self.home, "S", NOW + 2000))
                self.assertEqual(self.state()["active_stale"], 590)
                self.assertIn("no fresh usage data", usage.on_call(self.home, "S", NOW + 2020))

    def test_older_state_without_the_staleness_keys(self):
        self.put({"enabled": True, "alerts": [], "alerts_file": None, "error": None},
                 snap(ctx=7, h5=1, d7=40))
        usage.write_json(usage.state_path(self.home, "S"),
                         {"steps": {"context": 5, "five_hour": 0, "seven_day": 40}, "fired": [],
                          "resets": {"five_hour": FIVE, "seven_day": WEEK}, "had_fresh": True,
                          "baseline_sent": True, "ctx0": 7, "t0": NOW, "logged": []})
        self.assertIsNone(usage.on_call(self.home, "S", NOW + 3600))     # old, first call: gap 0
        self.assertEqual(self.state()["active_stale"], 0)
        self.assertTrue(self.state()["had_fresh"])
        self.assertEqual([t for t, _ in self.calls(NOW + 3630, 30)], [NOW + 3600 + 630])
        for p in usage.state_dir(self.home).glob("*.state.json"):
            p.unlink()
        usage.write_json(usage.state_path(self.home, "S"),
                         {"had_fresh": True, "last_call": "x", "active_stale": [], "seen_captured": "x"})
        self.assertIsNone(usage.on_call(self.home, "S", NOW + 9000))     # wrong-typed keys: defaults
        self.assertEqual(self.state()["active_stale"], 0)

    def test_future_or_non_finite_other_snapshot_is_not_chosen(self):
        for bad in (NOW + 61, float("nan"), float("inf")):
            with self.subTest(bad=bad):
                for p in usage.state_dir(self.home).glob("*"):
                    p.unlink()
                self.put({"enabled": True, "alerts": [], "alerts_file": None, "error": None}, snap(ctx=7))
                self.other("F", snap(h5=12, d7=56, at=bad))
                self.assertIn("account 5h pending · 7d pending", usage.on_call(self.home, "S", NOW))

    def test_future_or_non_finite_own_snapshot_is_told_stale_once(self):
        for bad in (NOW + 86400, float("nan")):
            with self.subTest(bad=bad):
                for p in usage.state_dir(self.home).glob("*"):
                    p.unlink()
                self.put({"enabled": True, "alerts": [], "alerts_file": None, "error": None}, snap(ctx=7))
                self.assertIsNotNone(usage.on_call(self.home, "S", NOW))
                self.put(s=snap(ctx=7, at=bad))
                said = self.calls(NOW + 30, 30)
                self.assertEqual([t for t, _ in said], [NOW + 630])
                self.assertTrue(said[0][1].startswith("usage: no fresh usage data for 10+ minutes of "
                                                      "activity — "), said[0][1])

    def test_missing_captured_at_says_for_a_while(self):
        self.put({"enabled": True, "alerts": [], "alerts_file": None, "error": None}, snap(ctx=7))
        usage.on_call(self.home, "S", NOW)
        s = snap(ctx=7)
        del s["captured_at"]
        self.put(s=s)
        self.assertEqual([text.split(" — ")[0] for _, text in self.calls(NOW + 30, 30)],
                         ["usage: no fresh usage data for 10+ minutes of activity"])

    def test_wrong_typed_state_is_rebuilt(self):
        self.put({"enabled": True, "alerts": [], "alerts_file": None, "error": None}, snap(ctx=7))
        for bad in ({"steps": "x"}, {"fired": {"a": 1}}, {"steps": {}, "fired": [1]},
                    {"resets": []}, {"ctx0": "x", "t0": 1}):
            with self.subTest(bad=bad):
                usage.write_json(usage.state_path(self.home, "S"), bad)
                self.assertTrue(usage.on_call(self.home, "S", NOW).startswith("usage: context 7%"))

    def test_wrong_typed_snapshot_is_skipped_like_a_corrupt_one(self):
        self.put({"enabled": True, "alerts": [], "alerts_file": None, "error": None}, snap(ctx=7))
        for bad in ({"captured_at": NOW, "context_window": "x"},
                    {"captured_at": NOW, "rate_limits": []},
                    {"captured_at": NOW, "rate_limits": {"five_hour": {"used_percentage": float("nan"),
                                                                       "resets_at": FIVE}}},
                    {"captured_at": "x", "context_window": {}}):
            with self.subTest(bad=bad):
                self.other("BAD", bad)
                self.assertIsNotNone(usage.on_call(self.home, "S", NOW))     # others do not break it
                self.put(s=bad)
                self.assertIsNone(usage.on_call(self.home, "S", NOW))
                self.put(s=snap(ctx=7))
                usage.state_path(self.home, "S").unlink()

    def test_context_step_comes_from_the_resolved_file(self):
        self.put({"enabled": True, "alerts": [], "alerts_file": None, "error": None, "context_step": 1},
                 snap(ctx=30))
        usage.on_call(self.home, "S", NOW)
        self.put(s=snap(ctx=31))
        self.assertIn("context 31%", usage.on_call(self.home, "S", NOW))
        self.put({"enabled": True, "alerts": [], "alerts_file": None, "error": None, "context_step": "x"},
                 snap(ctx=32))
        self.assertIsNone(usage.on_call(self.home, "S", NOW))     # a bad value falls back to 5

    def test_alerts_come_from_the_resolved_file(self):
        self.put({"enabled": True, "alerts_file": ".context-gate/usage-alerts.toml", "error": None,
                  "alerts": [{"signal": "context", "at": 5, "say": "ctx {pct}"}]}, snap(ctx=7))
        text = usage.on_call(self.home, "S", NOW)
        self.assertIn("(owner's prompt, .context-gate/usage-alerts.toml):\nctx 7", text)

    # ------------------------------------------------------------ mid-session alerts edits

    SHOWN = ".context-gate/usage-alerts.toml"

    def edit(self, text, tick):
        """Write the alerts file, with an mtime `tick` seconds past NOW - 100 (settled by NOW, and
        differing between edits even on a file system with coarse timestamps); returns its path."""
        f = self.home / "proj" / ".context-gate" / "usage-alerts.toml"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
        ns = (NOW - 100 + tick) * 1_000_000_000
        os.utime(f, ns=(ns, ns))
        return f

    def start(self, text, ctx=7):
        """A session resolved from `text` the way SessionStart writes it, and its first call."""
        f = self.alerts = self.edit(text, 0)
        alerts, step = usage.load_alerts(f)
        self.put({"enabled": True, "alerts_file": self.SHOWN, "alerts": alerts, "context_step": step,
                  "error": None, "alerts_path": str(f.resolve()), "alerts_mtime": f.stat().st_mtime_ns,
                  "alerts_size": f.stat().st_size},
                 snap(ctx=ctx))
        return usage.on_call(self.home, "S", NOW)

    def ctx_call(self, ctx, t=NOW):
        self.put(s=snap(ctx=ctx, at=t))
        return usage.on_call(self.home, "S", t)

    C20 = '[[alert]]\nsignal = "context"\nat = 20\nsay = "ctx20 at {pct}"\n'
    C5 = '[[alert]]\nsignal = "context"\nat = 5\nsay = "ctx5 at {pct}"\n'

    def log(self):
        p = usage.log_path(self.home)
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def test_ub12_alert_added_mid_session_fires_when_reached(self):
        self.assertNotIn("⚠", self.start(""))
        self.edit(self.C20, 1)
        self.assertNotIn("⚠", self.ctx_call(15))
        resolved = usage.read_json(usage.resolved_path(self.home, "S"))
        self.assertEqual([a["at"] for a in resolved["alerts"]], [20])
        self.assertIn(f"⚠ usage alert (owner's prompt, {self.SHOWN}):\nctx20 at 21", self.ctx_call(21))
        self.assertIsNone(self.ctx_call(21))

    def test_ub12_alert_removed_mid_session_stops_firing_and_its_fired_entry_stays(self):
        self.assertIn("ctx5 at 7", self.start(self.C5 + self.C20))
        self.assertEqual(self.state()["fired"], [["context", 5]])
        self.edit(self.C5.replace("ctx5", "kept"), 1)       # 20 removed, 5 kept as it was
        self.ctx_call(8)
        self.assertEqual(self.state()["fired"], [["context", 5]])
        self.edit("", 2)                                    # both removed
        self.ctx_call(8)
        self.assertEqual(self.state()["fired"], [["context", 5]])    # never pruned
        text = self.ctx_call(25)
        self.assertNotIn("⚠", text)
        self.assertNotIn("ctx20", text)

    def test_ub12_changed_at_fires_once(self):
        self.start(self.C20, ctx=22)
        self.assertEqual(self.state()["fired"], [["context", 20]])
        self.edit(self.C20.replace("at = 20", "at = 21"), 1)
        text = self.ctx_call(22)                            # already past 21: fires on this call
        self.assertIn("ctx20 at 22", text)
        self.assertEqual(self.state()["fired"], [["context", 20], ["context", 21]])
        self.assertIsNone(self.ctx_call(22))
        self.assertIsNone(self.ctx_call(23))

    def test_ub12_reload_of_an_empty_file_then_the_full_file_does_not_re_fire(self):
        self.assertIn("ctx5 at 7", self.start(self.C5))
        self.edit("", 1)                                    # half-written: empty
        self.assertIsNone(self.ctx_call(8))
        self.edit(self.C5, 2)                               # the full file, same window
        self.assertIsNone(self.ctx_call(8))
        self.assertEqual(self.state()["fired"], [["context", 5]])

    def test_ub12_changed_at_there_and_back_does_not_re_fire(self):
        a93 = '[[alert]]\nsignal = "context"\nat = 93\nsay = "ctx93 at {pct}"\n'
        self.assertIn("ctx93 at 94", self.start(a93, ctx=94))
        self.edit(a93.replace("at = 93", "at = 95"), 1)
        self.assertIsNone(self.ctx_call(94))
        self.edit(a93, 2)
        self.assertIsNone(self.ctx_call(94))

    def test_ub12_removed_alert_never_fires_again(self):
        self.assertIn("ctx5 at 7", self.start(self.C5))
        self.edit("", 1)
        self.ctx_call(8)
        self.edit(self.C5, 2)
        self.assertNotIn("⚠", self.ctx_call(9) or "")

    def test_ub12_file_younger_than_two_seconds_waits_for_a_later_call(self):
        self.start("", ctx=7)
        f = self.edit(self.C20, 0)
        ns = int((NOW - 1) * 1e9)
        os.utime(f, ns=(ns, ns))                            # written 1 s ago: may be mid-write
        self.assertNotIn("⚠", self.ctx_call(21))
        self.assertEqual(usage.read_json(usage.resolved_path(self.home, "S"))["alerts"], [])
        self.assertIn("ctx20 at 21", self.ctx_call(21, NOW + 1))     # settled on a later call

    def test_ub12_same_mtime_different_size_is_reloaded(self):
        self.start("", ctx=7)
        f = self.edit(self.C20, 0)                          # same mtime as at start, new size
        self.assertIn("ctx20 at 21", self.ctx_call(21))

    def test_ub12_context_step_changed_mid_session_takes_effect(self):
        self.start("", ctx=30)
        self.assertIsNone(self.ctx_call(31))                # default step 5
        self.edit("context_step = 1\n", 1)
        self.assertIn("context 32%", self.ctx_call(32))
        self.assertEqual(usage.read_json(usage.resolved_path(self.home, "S"))["context_step"], 1)

    def test_ub12_missing_file_keeps_the_alerts_and_logs_once(self):
        self.start(self.C20)
        self.alerts.unlink()
        self.assertIsNone(self.ctx_call(8))
        self.assertIn("ctx20 at 25", self.ctx_call(25))
        self.assertIsNone(self.ctx_call(25))
        self.assertEqual(self.log().count("alerts file gone; keeping the alerts loaded at session start"), 1)

    def test_ub12_invalid_file_keeps_the_previous_alerts_told_once_per_change(self):
        self.start("context_step = 1\n" + self.C20, ctx=10)
        self.edit(self.C20.replace("at = 20", "at = 0"), 1)
        said = ("usage: .context-gate/usage-alerts.toml has an error (alert 1: at must be a whole "
                "number 1-100); keeping the previous alerts")
        self.assertEqual(self.ctx_call(10), said)
        self.assertIsNone(self.ctx_call(10))                # not repeated while the file stands
        self.assertIn("context 11%", self.ctx_call(11))     # the previous context_step stands
        self.assertIn("ctx20 at 20", self.ctx_call(20))     # and the previous alerts
        self.assertEqual(self.log().count("has an error"), 1)
        self.edit("bad = 1\n", 2)                           # changed again, still invalid
        self.assertTrue(self.ctx_call(20).startswith("usage: .context-gate/usage-alerts.toml has an "
                                                     "error (unknown key(s): bad)"))
        self.assertEqual(self.log().count("has an error"), 2)

    def test_ub12_invalid_edit_is_told_with_a_stale_snapshot_too(self):
        self.start(self.C20)
        self.put(s=snap(ctx=7, at=NOW - usage.STALE_SECONDS - 1))
        self.edit("bad = 1\n", 1)
        self.assertIn("has an error (unknown key(s): bad)", usage.on_call(self.home, "S", NOW))

    def test_ub12_non_utf8_edit_is_an_error_not_a_traceback(self):
        self.start(self.C20)
        f = self.edit("", 1)
        f.write_bytes(b'[[alert]]\nsignal = "context"\nat = 20\nsay = "caf\xe9"\n')
        ns = (NOW - 98) * 1_000_000_000
        os.utime(f, ns=(ns, ns))
        self.assertIn("has an error (", self.ctx_call(7))
        self.assertIn("ctx20 at 25", self.ctx_call(25))

    def test_ub12_resolved_by_an_earlier_beta_does_not_reload(self):
        self.edit(self.C20.replace("at = 20", "at = 8"), 0)  # on disk, but the file never names it
        self.put({"enabled": True, "alerts_file": self.SHOWN, "error": None, "context_step": 5,
                  "alerts": [{"signal": "context", "at": 20, "say": "ctx20 at {pct}"}]}, snap(ctx=7))
        with mock.patch.object(usage, "load_alerts", side_effect=AssertionError("re-read")):
            usage.on_call(self.home, "S", NOW)
            self.assertNotIn("⚠", self.ctx_call(10))
            self.assertIn("ctx20 at 25", self.ctx_call(25))

    def test_ub12_unchanged_file_is_not_re_read(self):
        self.start(self.C20)
        real = usage.load_alerts
        with mock.patch.object(usage, "load_alerts", side_effect=real) as load:
            for i in range(5):
                self.ctx_call(8 + i)
            self.assertEqual(load.call_count, 0)
            self.edit(self.C20, 1)                           # same text, touched
            self.ctx_call(13)
            self.ctx_call(14)
            self.assertEqual(load.call_count, 1)

    def test_never_fresh_session_is_not_told_stale(self):
        self.put({"enabled": True, "alerts": [], "alerts_file": None, "error": None},
                 snap(ctx=7, at=NOW - usage.STALE_SECONDS - 1))
        self.assertIsNone(usage.on_call(self.home, "S", NOW))
        self.assertEqual(self.calls(NOW + 30, 60), [])     # however long it stays active
        self.assertGreater(self.state()["active_stale"], usage.STALE_SECONDS)

    def test_stale_or_missing_snapshot_logs_once(self):
        self.put({"enabled": True, "alerts": [], "alerts_file": None, "error": None})
        self.assertIsNone(usage.on_call(self.home, "S", NOW))
        self.put(s=snap(ctx=7, at=NOW - usage.STALE_SECONDS - 1))
        self.assertIsNone(usage.on_call(self.home, "S", NOW))
        self.assertIsNone(usage.on_call(self.home, "S", NOW))
        log = usage.log_path(self.home).read_text(encoding="utf-8")
        self.assertEqual(log.count("no snapshot"), 1)
        self.assertEqual(log.count("stale snapshot"), 1)

    def test_corrupt_files_are_silent(self):
        self.put({"enabled": True, "alerts": [], "alerts_file": None, "error": None})
        usage.snapshot_path(self.home, "S").write_text("{half", encoding="utf-8")
        self.assertIsNone(usage.on_call(self.home, "S", NOW))
        usage.state_path(self.home, "S").write_text("[]", encoding="utf-8")
        self.put(s=snap(ctx=7))
        self.assertIsNotNone(usage.on_call(self.home, "S", NOW))

    def test_resolve_error_is_logged_and_data_still_flows(self):
        self.put({"enabled": True, "alerts": [], "alerts_file": "x.toml", "error": "at must be 1-100"},
                 snap(ctx=7))
        self.assertIsNotNone(usage.on_call(self.home, "S", NOW))
        self.assertIn("at must be 1-100", usage.log_path(self.home).read_text(encoding="utf-8"))

    def test_end_session_and_sweep(self):
        self.put({"enabled": True, "alerts": [], "alerts_file": None, "error": None}, snap(ctx=7))
        usage.on_call(self.home, "S", NOW)
        usage.end_session(self.home, "S")
        self.assertEqual([p.name for p in usage.state_dir(self.home).glob("S.*")], [])
        old = usage.snapshot_path(self.home, "OLD")
        usage.write_json(old, {})
        import os
        os.utime(old, (NOW - 8 * 86400, NOW - 8 * 86400))
        usage.sweep(self.home, NOW)
        self.assertFalse(old.exists())


if __name__ == "__main__":
    unittest.main()
