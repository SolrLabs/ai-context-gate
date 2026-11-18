"""capture.py's optional row: drawn from the statusline payload alone, switched by
statusline-row.json, and never able to break the statusline."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import profile_guard  # noqa: E402,F401

from test_usage_capture import CAPTURE, load_capture  # noqa: E402

RESETS = 1790499600
DIM, RESET_ = "\x1b[2m", "\x1b[0m"


def payload(**over):
    data = {"session_id": "S", "model": {"display_name": "Opus 5.5"},
            "workspace": {"current_dir": os.path.join(os.sep, "w", "ai-governance")},
            "context_window": {"used_percentage": 12},
            "rate_limits": {"five_hour": {"used_percentage": 40, "resets_at": RESETS},
                            "seven_day": {"used_percentage": 20, "resets_at": RESETS}}}
    data.update(over)
    return data


def limits(five=None, week=None):
    out = {}
    if five:
        out["five_hour"] = {"used_percentage": five, "resets_at": RESETS}
    if week:
        out["seven_day"] = {"used_percentage": week, "resets_at": RESETS}
    return {"rate_limits": out}


class Render(unittest.TestCase):
    def setUp(self):
        self.cap = load_capture()

    def draw(self, data, now=RESETS - 9000, branch="development", color=True):
        env = {} if color else {"NO_COLOR": "1"}
        with mock.patch.dict(os.environ, env, clear=False):
            if color:
                os.environ.pop("NO_COLOR", None)
            return self.cap.render(data, now, branch)

    def test_plain_row_matches_the_example(self):
        with mock.patch.object(self.cap.time, "localtime", return_value=time.gmtime(RESETS)):
            text = self.draw(payload(), color=False)
        stamp = time.strftime("%I:%M %p", time.gmtime(RESETS)).lstrip("0")
        head = ("ai-governance \u2502 development \u2502 Opus 5.5 \u2502 Ctx: 12% \u2502 "
                f"Usage: 40% \u2588\u2588\u2588\u2588\u2591\u2503\u2591\u2591\u2591\u2591\u2591 \u2192 Reset: {stamp}"
                " \u2502 Weekly: 20% \u00b7 today 0/")
        self.assertTrue(text.startswith(head), text)         # today's allowance depends on the local zone
        self.assertRegex(text[len(head):], r"^\d+%$")

    def test_no_color_has_no_escape_codes(self):
        self.assertNotIn("\x1b", self.draw(payload(), color=False))
        self.assertIn("\x1b[", self.draw(payload()))

    def test_context_color(self):
        for p, code in ((69, "32"), (70, "33"), (89, "33"), (90, "31")):
            with self.subTest(p=p):
                text = self.draw({"context_window": {"used_percentage": p}})
                self.assertIn(f"\x1b[{code}mCtx: {p}%", text)

    def test_context_color_follows_the_printed_number(self):
        for p, code, shown in ((69.6, "33", 70), (89.5, "31", 90)):
            with self.subTest(p=p):
                text = self.draw({"context_window": {"used_percentage": p}})
                self.assertIn(f"\x1b[{code}mCtx: {shown}%", text)

    def test_almost_full_prints_100_and_is_red(self):
        self.assertEqual(self.five_code(99.6, now=RESETS - 18000 + 300), "31")
        self.assertEqual(self.week_code(99.6, now=RESETS - 604800 + 600), "31")

    def week_code(self, used, now=RESETS - 302400):
        text = self.draw(limits(week=used), now=now, branch=None)
        return text.split("m", 1)[0].split("[")[1]

    def five_code(self, used, now=RESETS - 9000):
        text = self.draw(limits(five=used), now=now, branch=None)
        return text.split("m", 1)[0].split("[")[1]

    def test_five_hour_pace(self):
        for used, code in ((40, "32"), (51, "33"), (53, "31")):
            with self.subTest(used=used):
                self.assertEqual(self.five_code(used), code)

    def test_weekly_without_history_is_green_not_the_pace_color(self):
        for used in (50.4, 50.6, 40):
            with self.subTest(used=used):
                self.assertEqual(self.week_code(used), "32")
        self.assertNotEqual(self.cap.pace_color(50.6, 302400, 604800, 1), "32")    # the average alone says otherwise

    def test_early_window_is_noise_unless_full(self):
        self.assertEqual(self.five_code(30, now=RESETS - 18000 + 300), "32")
        self.assertEqual(self.five_code(100, now=RESETS - 18000 + 300), "31")
        # the weekly part no longer hides the first minutes: it is judged against today's allowance
        self.assertEqual(self.week_code(0.5, now=RESETS - 604800 + 600), "32")
        self.assertEqual(self.week_code(30, now=RESETS - 604800 + 600), "31")
        self.assertEqual(self.week_code(100, now=RESETS - 604800 + 600), "31")

    def test_full_is_red_outside_the_early_window_too(self):
        self.assertEqual(self.five_code(100), "31")
        self.assertEqual(self.week_code(100), "31")

    def test_bar_and_marker(self):
        bar = self.draw(limits(five=75), now=RESETS - 18000 + 3600, color=False)
        self.assertIn("\u2588\u2588\u2503\u2588\u2588\u2588\u2588\u2588\u2591\u2591\u2591", bar)
        bar = self.draw(limits(five=40), color=False)
        self.assertIn("\u2588\u2588\u2588\u2588\u2591\u2503\u2591\u2591\u2591\u2591\u2591", bar)

    def test_marker_is_bold_in_colour(self):
        self.assertIn("\x1b[0m\x1b[1m\u2503\x1b[0m\x1b[32m", self.draw(limits(five=40)))

    def test_branch_part_is_the_name_alone_in_green(self):
        self.assertEqual(self.draw({}, color=False), "development")
        self.assertEqual(self.draw({}), "\x1b[32mdevelopment\x1b[0m")

    @unittest.skipUnless(hasattr(time, "tzset"), "time.tzset is missing on Windows, so TZ cannot be fixed")
    def test_reset_time_is_local_twelve_hour_without_leading_zero(self):
        old = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"
        time.tzset()
        try:
            at = 1790436600          # 2026-09-27 15:30 UTC
            data = {"rate_limits": {"five_hour": {"used_percentage": 40, "resets_at": at}}}
            self.assertIn("Reset: 3:30 PM", self.draw(data, now=at - 9000, color=False))
        finally:
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()

    def test_missing_parts_are_left_out(self):
        data = payload(model={})
        text = self.draw(data, branch=None, color=False)
        self.assertNotIn("Opus", text)
        self.assertTrue(text.startswith("ai-governance \u2502 Ctx: 12%"))

    def test_no_rate_limits_leaves_out_both_usage_parts(self):
        data = payload()
        del data["rate_limits"]
        text = self.draw(data, color=False)
        self.assertNotIn("Usage", text)
        self.assertNotIn("Weekly", text)
        self.assertTrue(text.endswith("Ctx: 12%"))

    def test_windows_folder_name(self):
        data = payload(workspace={"current_dir": "C:\\Users\\me\\proj"})
        self.assertTrue(self.draw(data, color=False).startswith("proj \u2502"))

    def test_nothing_private_is_printed(self):
        home = os.path.join(os.sep, "home", "secretuser", "work")
        data = payload(session_id="SESSION-123", transcript_path=os.path.join(home, "t-9.jsonl"),
                       cost={"total_cost_usd": 4.7731}, workspace={"current_dir": os.path.join(home, "proj")})
        text = self.draw(data, color=False)
        for secret in ("SESSION-123", "t-9", "4.7731", "secretuser", home):
            self.assertNotIn(secret, text)
        self.assertTrue(text.startswith("proj \u2502"))


class Wrap(unittest.TestCase):
    def setUp(self):
        self.cap = load_capture()
        env = mock.patch.dict(os.environ, {"NO_COLOR": "1"})
        env.start()
        self.addCleanup(env.stop)

    def draw(self, width=None):
        return self.cap.render(payload(), RESETS - 9000, "development", None, width)

    def test_packs_parts_in_order_within_the_width(self):
        lines = self.draw(58).split("\n")
        self.assertGreaterEqual(len(lines), 2)
        for line in lines:
            self.assertLessEqual(len(line), 58)
            self.assertFalse(line.startswith(" \u2502") or line.endswith("\u2502 ") or line.endswith(" \u2502"))
        self.assertEqual(" \u2502 ".join(lines), self.draw())

    def test_the_separator_counts_toward_the_width(self):
        both = len("ai-governance") + 3 + len("development")
        self.assertEqual(self.draw(both).split("\n")[0], "ai-governance │ development")
        short = self.draw(both - 1).split("\n")      # 24 columns of parts, 27 with the separator
        self.assertEqual(short[0], "ai-governance")
        self.assertTrue(all(len(x) <= both - 1 for x in short if " │ " in x))   # a lone wide part may exceed

    def test_wide_enough_is_one_line_equal_to_the_unwrapped_render(self):
        self.assertEqual(self.draw(200), self.draw())
        self.assertNotIn("\n", self.draw())

    def test_a_part_wider_than_the_width_sits_alone(self):
        lines = self.draw(10).split("\n")
        self.assertIn("Opus 5.5", lines)
        self.assertTrue(any(len(x) > 10 for x in lines))
        self.assertEqual(" \u2502 ".join(lines), self.draw())

    def test_colour_codes_do_not_count_toward_the_width(self):
        os.environ.pop("NO_COLOR")
        lines = self.draw(len("ai-governance \u2502 development")).split("\n")
        self.assertEqual(re.sub(r"\x1b\[[0-9;]*m", "", lines[0]), "ai-governance \u2502 development")
        self.assertIn("\x1b[", lines[0])
        self.assertEqual(re.sub(r"\x1b\[[0-9;]*m", "", lines[1]), "Opus 5.5 \u2502 Ctx: 12%")


@unittest.skipUnless(shutil.which("git"), "git is not on PATH")
class Branch(unittest.TestCase):
    def setUp(self):
        self.cap = load_capture()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def git(self, repo, *args):
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(repo), *args],
                       check=True, capture_output=True)

    def test_branch_of_a_repo(self):
        repo = self.tmp / "repo"
        repo.mkdir()
        self.git(repo, "init", "-q")
        self.git(repo, "symbolic-ref", "HEAD", "refs/heads/feature/x")
        self.git(repo, "commit", "-q", "--allow-empty", "-m", "c")
        self.assertEqual(self.cap.branch(str(repo)), "feature/x")

    def test_not_a_repo_or_missing_is_none(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        self.assertIsNone(self.cap.branch(str(plain)))
        self.assertIsNone(self.cap.branch(str(self.tmp / "nope")))


class Main(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "a home"          # a space, on purpose
        self.share = self.home / ".local" / "share" / "context-gate"
        self.share.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def chain(self):
        script = self.home / "echo.py"
        script.write_text("import sys\nsys.stdin.read()\nsys.stdout.write('LINE:x')\nsys.exit(3)\n",
                          encoding="utf-8")
        (self.share / "statusline-chain.json").write_text(
            json.dumps({"statusLine": {"type": "command", "command": f'"{sys.executable}" "{script}"'}}),
            encoding="utf-8")

    def switch(self, text):
        (self.share / "statusline-row.json").write_text(text, encoding="utf-8")

    def run_(self, data, columns=None):
        env = {**os.environ, "HOME": str(self.home)}
        env.pop("NO_COLOR", None)
        env.pop("COLUMNS", None)
        if columns is not None:
            env["COLUMNS"] = columns
        return subprocess.run([sys.executable, str(CAPTURE)], input=json.dumps(data).encode(),
                              env=env, capture_output=True)

    def test_row_on_prints_the_row_and_skips_the_chain(self):
        self.chain()
        self.switch('{"row": true}')
        res = self.run_(payload())
        out = res.stdout.decode("utf-8")
        self.assertEqual(res.returncode, 0)
        self.assertNotIn("LINE:", out)
        self.assertIn("Opus 5.5", out)
        self.assertIn("Ctx: 12%", out)
        self.assertTrue(out.endswith("\n"))

    def test_columns_wraps_the_row(self):
        self.switch('{"row": true}')
        ansi = re.compile(r"\x1b\[[0-9;]*m")
        lines = self.run_(payload(), "60").stdout.decode("utf-8").splitlines()
        self.assertGreaterEqual(len(lines), 2)
        self.assertTrue(all(len(ansi.sub("", x)) <= 58 for x in lines))

    def test_columns_is_two_less_than_the_terminal(self):
        cap = load_capture()
        with mock.patch.dict(os.environ, {"COLUMNS": "60"}):
            self.assertEqual(cap.columns(), 58)

    def test_no_line_reaches_columns_minus_one(self):
        self.switch('{"row": true}')
        # "ai-governance │ Opus 5.5" is 24 columns: it fits COLUMNS - 1 = 24, not COLUMNS - 2 = 23
        lines = self.run_(payload(), "25").stdout.decode("utf-8").splitlines()
        plain = [re.sub(r"\x1b\[[0-9;]*m", "", x) for x in lines]
        self.assertEqual(plain[0], "ai-governance")
        self.assertTrue(all(len(x) <= 23 for x in plain if " │ " in x))     # a lone wide part may exceed

    def test_columns_unset_bad_or_tiny_is_one_line(self):
        self.switch('{"row": true}')
        for cols in (None, "abc", "10", ""):
            with self.subTest(columns=cols):
                out = self.run_(payload(), cols).stdout.decode("utf-8")
                self.assertEqual(out.count("\n"), 1)

    def test_row_off_is_the_chain_byte_for_byte(self):
        self.chain()
        for text in (None, '{"row": false}', '{"row": 1}', '{"row": true, "x": 1}', "garbage", "[true]"):
            with self.subTest(switch=text):
                if text is not None:
                    self.switch(text)
                res = self.run_(payload())
                self.assertEqual((res.stdout, res.returncode), (b"LINE:x", 3))

    def test_an_error_in_render_falls_back_to_the_chain(self):
        self.chain()
        self.switch('{"row": true}')
        res = self.run_(payload(context_window="oops"))
        self.assertEqual((res.stdout, res.returncode), (b"LINE:x", 3))


if __name__ == "__main__":
    unittest.main()
