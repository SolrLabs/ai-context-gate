"""`govern usage weights`: the settings in effect, strict validation, and --suggest."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import profile_guard  # noqa: E402,F401

from govern import cli, usage_setup, usage_week  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "my home"
        self.share = self.home / ".local" / "share" / "context-gate"
        self.state = self.home / ".local" / "state" / "context-gate" / "usage"
        self.share.mkdir(parents=True)
        self.state.mkdir(parents=True)
        self.file = self.share / "usage-week.toml"
        self.work = self.home / ".claude-work"
        env = {"HOME": str(self.home), "USERPROFILE": str(self.home)}
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("CLAUDE_CONFIG_DIR", None)

    def run_cli(self, *args, config_dir=None):
        out = io.StringIO()
        env = {"CLAUDE_CONFIG_DIR": config_dir} if config_dir else {}
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out):
            code = cli.main(["usage", "weights", *args])
        return code, out.getvalue()

    def write(self, text):
        self.file.write_text(text, encoding="utf-8")

    def days(self, profile, totals):
        """days.json with one window per date; `totals` maps date to points used that day."""
        data = {profile: {d.isoformat(): [[1, 0, used]] for d, used in totals.items()}}
        (self.state / "days.json").write_text(json.dumps(data), encoding="utf-8")

    def weeks(self, n=14):
        """n days ending on a Sunday: Saturday 4 points, every other day 10."""
        end = date(2026, 10, 25)
        out = {}
        for i in range(n):
            d = end - timedelta(days=i)
            out[d] = 4 if d.weekday() == 5 else 10
        return out


class Shown(Base):
    def test_defaults_with_no_file(self):
        code, out = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), [
            "settings: none, every day weighs 1",
            "profile: default (~/.claude)",
            "weights: mon 1, tue 1, wed 1, thu 1, fri 1, sat 1, sun 1",
            "day_end: 24:00",
            "later_cut: 0.1"])

    def test_profile_override_shown(self):
        self.write('[weekly]\nweights = { sat = 0.4 }\nday_end = "24:00"\n'
                   '[weekly.profiles."~/.claude-work"]\n'
                   'weights = { sat = 0, sun = 0 }\nday_end = "18:00"\n')
        code, out = self.run_cli(config_dir=str(self.work))
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), [
            f"settings: {self.file}",
            f"profile: {os.path.abspath(self.work)}",
            "weights: mon 1, tue 1, wed 1, thu 1, fri 1, sat 0, sun 0",
            "day_end: 18:00",
            "later_cut: 0.1"])
        code, out = self.run_cli("--profile", str(self.work))
        self.assertIn("day_end: 18:00", out)

    def test_default_profile_matches_tilde_claude(self):
        self.write('[weekly]\nlater_cut = 0.3\n[weekly.profiles."~/.claude"]\n'
                   'weights = { sun = 0 }\nday_end = "17:30"\n')
        for kw in ({}, {"config_dir": str(self.home / ".claude")}):
            code, out = self.run_cli(**kw)
            self.assertEqual(code, 0)
            self.assertIn("profile: default (~/.claude)", out)
            self.assertIn("sun 0", out)
            self.assertIn("day_end: 17:30", out)
            self.assertIn("later_cut: 0.3", out)


class Faults(Base):
    def fault(self, text, message):
        self.write(text)
        code, out = self.run_cli()
        self.assertEqual(code, 1)
        self.assertEqual(out.splitlines(), [message])

    def test_weight_out_of_range(self):
        self.fault('[weekly.profiles."x"]\nweights = { sat = 11 }\n',
                   'usage-week.toml: [weekly.profiles."x"] weights.sat = 11: a weight is 0 to 10')

    def test_later_cut_out_of_range(self):
        self.fault("[weekly]\nlater_cut = 2\n",
                   "usage-week.toml: [weekly] later_cut = 2: a number from 0 to 1")

    def test_day_end_out_of_range(self):
        self.fault('[weekly]\nday_end = "25:00"\n',
                   'usage-week.toml: [weekly] day_end = "25:00": HH:MM from 00:01 to 24:00')

    def test_unknown_weekday(self):
        self.fault("[weekly]\nweights = { fry = 1 }\n",
                   "usage-week.toml: [weekly] weights.fry = 1: a weekday is mon to sun")

    def test_unknown_key_at_both_levels(self):
        self.fault("[weekly]\ncolour = 1\n", "usage-week.toml: [weekly] colour: unknown key")
        self.fault('[weekly.profiles."x"]\ncolour = 1\n',
                   'usage-week.toml: [weekly.profiles."x"] colour: unknown key')

    def test_invalid_toml(self):
        self.write("[weekly\n")
        code, out = self.run_cli()
        self.assertEqual(code, 1)
        self.assertTrue(out.startswith("usage-week.toml: not valid TOML: "), out)

    def test_bool_is_not_a_number(self):
        self.fault("[weekly]\nweights = { mon = true }\n",
                   "usage-week.toml: [weekly] weights.mon = true: a weight is 0 to 10")

    def test_every_fault_is_listed(self):
        self.write('[weekly]\nlater_cut = 2\nday_end = "00:00"\n')
        code, out = self.run_cli()
        self.assertEqual(code, 1)
        self.assertEqual(len(out.splitlines()), 2)

    def test_suggest_reports_faults_too(self):
        self.write("[weekly]\nlater_cut = 2\n")
        code, out = self.run_cli("--suggest")
        self.assertEqual(code, 1)
        self.assertIn("later_cut", out)


class Keys(Base):
    def test_two_keys_for_one_folder_are_a_fault(self):
        # A TOML literal key: a Windows path holds backslashes, which a
        # double-quoted key would read as escapes.
        absolute = os.path.abspath(self.work)
        self.write('[weekly.profiles."~/.claude-work"]\nday_end = "18:00"\n'
                   f"[weekly.profiles.'{absolute}']\nday_end = \"17:00\"\n")
        code, out = self.run_cli()
        self.assertEqual(code, 1)
        self.assertEqual(out.splitlines(), [
            'usage-week.toml: [weekly.profiles."~/.claude-work"] and '
            f'[weekly.profiles."{absolute}"] name the same folder'])

    def test_a_literal_key_with_backslashes_parses_on_every_os(self):
        windows = "\\".join(["C:", "Users", "x"])
        self.write(f"[weekly.profiles.'{windows}']\nday_end = \"18:00\"\n")
        self.assertEqual(usage_week.check(self.file), [])

    def test_keys_differing_in_case_name_the_same_folder(self):
        self.write('[weekly.profiles."~/.claude-work"]\nday_end = "18:00"\n'
                   '[weekly.profiles."~/.CLAUDE-WORK"]\nday_end = "17:00"\n')
        with mock.patch("os.path.normcase", lambda s: s):
            self.assertEqual(usage_week.check(self.file), [])
        with mock.patch("os.path.normcase", str.lower):
            faults = usage_week.check(self.file)
        self.assertEqual(len(faults), 1)
        self.assertIn("name the same folder", faults[0])

    def test_different_folders_are_fine(self):
        self.write('[weekly.profiles."~/.claude-work"]\nday_end = "18:00"\n'
                   '[weekly.profiles."~/.claude-play"]\nday_end = "17:00"\n')
        self.assertEqual(usage_week.check(self.file), [])

    def test_a_bom_is_not_a_fault(self):
        self.file.write_bytes(b"\xef\xbb\xbf" + b'[weekly]\nlater_cut = 0.3\n')
        self.assertEqual(usage_week.check(self.file), [])
        code, out = self.run_cli()
        self.assertEqual(code, 0)


class Suggest(Base):
    def test_suggest_refuses_with_13_days(self):
        self.days("", self.weeks(13))
        code, out = self.run_cli("--suggest")
        self.assertEqual(code, 1)
        self.assertEqual(out.strip(), "not enough history: 13 days, needs 14")

    def test_suggest_prints_table_with_14_days(self):
        self.days("", self.weeks())
        code, out = self.run_cli("--suggest")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), [
            '[weekly.profiles."~/.claude"]',
            "weights = { mon = 1, tue = 1, wed = 1, thu = 1, fri = 1, sat = 0.4, sun = 1 }"])

    def test_suggest_uses_the_profiles_own_anchors(self):
        work = os.path.abspath(self.work)
        self.days(work, self.weeks())
        code, out = self.run_cli("--suggest")
        self.assertEqual(code, 1)                     # the default profile has no days
        for args, kw in ((("--suggest", "--profile", str(self.work)), {}),
                         (("--suggest",), {"config_dir": str(self.work)})):
            code, out = self.run_cli(*args, **kw)
            self.assertEqual(code, 0)
            self.assertEqual(out.splitlines()[0], f'[weekly.profiles."{work}"]')

    def test_suggest_reads_the_absolute_default_folder(self):
        default = os.path.abspath(self.home / ".claude")
        self.days(default, self.weeks())
        code, out = self.run_cli("--suggest", config_dir=default)
        self.assertEqual(code, 0, out)
        self.assertEqual(out.splitlines()[0], '[weekly.profiles."~/.claude"]')
        code, out = self.run_cli("--suggest", "--profile", "~/.claude")
        self.assertEqual(code, 0, out)

    def test_suggest_reads_empty_key_for_explicit_default(self):
        self.days("", self.weeks())
        code, out = self.run_cli("--suggest", "--profile", "~/.claude")
        self.assertEqual(code, 0, out)
        self.assertEqual(out.splitlines()[0], '[weekly.profiles."~/.claude"]')

    def test_suggest_takes_the_larger_total_under_both_keys(self):
        default = os.path.abspath(self.home / ".claude")
        full = self.weeks()
        data = {"": {d.isoformat(): [[1, 0, 2]] for d in full},
                default: {d.isoformat(): [[1, 0, u]] for d, u in full.items()}}
        (self.state / "days.json").write_text(json.dumps(data), encoding="utf-8")
        code, out = self.run_cli("--suggest")
        self.assertEqual(code, 0, out)
        self.assertIn("sat = 0.4", out)

    def test_describe_names_the_default_for_either_spelling(self):
        for kw in ({"config_dir": os.path.abspath(self.home / ".claude")}, {}):
            code, out = self.run_cli(**kw)
            self.assertIn("profile: default (~/.claude)", out)

    def full_days_and_today(self, n, today_points):
        today = date.today()
        out = {today - timedelta(days=i): (4 if (today - timedelta(days=i)).weekday() == 5 else 10)
               for i in range(1, n + 1)}
        out[today] = today_points
        return out

    def test_suggest_leaves_out_today(self):
        self.days("", self.full_days_and_today(14, 1))
        code, out = self.run_cli("--suggest")
        self.assertEqual(code, 0, out)
        self.assertEqual(out.splitlines()[1],
                         "weights = { mon = 1, tue = 1, wed = 1, thu = 1, fri = 1, sat = 0.4, sun = 1 }")

    def test_suggest_does_not_count_today_toward_the_minimum(self):
        self.days("", self.full_days_and_today(13, 10))
        code, out = self.run_cli("--suggest")
        self.assertEqual((code, out.strip()), (1, "not enough history: 13 days, needs 14"))

    def test_suggest_no_use(self):
        self.days("", {d: 0 for d in self.weeks()})
        code, out = self.run_cli("--suggest")
        self.assertEqual((code, out.strip()), (1, "no use recorded"))

    def test_suggest_writes_nothing(self):
        self.days("", self.weeks())
        before = {p: p.read_bytes() for p in self.home.rglob("*") if p.is_file()}
        self.run_cli("--suggest")
        after = {p: p.read_bytes() for p in self.home.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertFalse(self.file.exists())


class Capture(Base):
    def test_load_capture_points_at_home(self):
        cap = usage_week.load_capture(self.home)
        self.assertEqual(cap.WEEK_SETTINGS, self.file)
        self.assertEqual(cap.DAYS, self.state / "days.json")

    def test_no_bytecode_beside_the_template(self):
        cache = usage_setup.TEMPLATE.parent / "__pycache__"

        def listed():
            return sorted(cache.glob("*.pyc")) if cache.is_dir() else []

        before = listed()
        usage_week.load_capture(self.home)
        self.assertEqual(listed(), before)


if __name__ == "__main__":
    unittest.main()
