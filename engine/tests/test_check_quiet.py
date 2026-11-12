"""`govern check --quiet`: every error, no warning lines, each status line counting what it hid."""
from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import profile_guard  # noqa: E402,F401

from regressions_support import *  # noqa: E402,F401,F403
from govern.findings import Findings, report_aggregate, report_per_scope  # noqa: E402


def captured(fn, *args, **kw) -> tuple[bool, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        ok = fn(*args, **kw)
    return ok, out.getvalue()


def found(errors=(), warnings=()) -> Findings:
    f = Findings()
    f.errors.extend(errors)
    f.warnings.extend(warnings)
    return f


class Reporters(unittest.TestCase):
    def test_aggregate_hides_warnings_and_counts_them(self):
        f = found(["bad"], ["w one", "w two"])
        ok, out = captured(report_aggregate, "demo", f, quiet=True)
        self.assertFalse(ok)
        self.assertEqual(out, "  ERROR  bad\n"
                              "[FAIL] demo  (1 errors, 2 warnings hidden, run without --quiet)\n")

    def test_default_output_is_unchanged(self):
        f = found(["bad"], ["w one"])
        _, out = captured(report_aggregate, "demo", f)
        self.assertEqual(out, "  ERROR  bad\n  warn   w one\n[FAIL] demo  (1 errors, 1 warnings)\n")

    def test_clean_scope_prints_the_same_line_in_both_modes(self):
        f = found()
        _, loud = captured(report_aggregate, "demo", f)
        _, quiet = captured(report_aggregate, "demo", f, quiet=True)
        self.assertEqual(loud, quiet)
        self.assertEqual(quiet, "[OK] demo  (0 errors, 0 warnings)\n")

    def test_per_scope_total_hides_its_count_too(self):
        groups = [("a", found(["e"], ["w"])), ("b", found(warnings=["x", "y"]))]
        ok, out = captured(report_per_scope, groups, quiet=True)
        self.assertFalse(ok)
        self.assertNotIn("  warn", out)
        self.assertIn("[FAIL] a  (1 errors, 1 warnings hidden, run without --quiet)", out)
        self.assertIn("[OK] b  (0 errors, 2 warnings hidden, run without --quiet)", out)
        self.assertIn("[FAIL] total  (1 errors, 3 warnings hidden, run without --quiet)", out)


class CheckCommand(Base):
    def broken(self):
        w = self.ws()
        w.write("projects/alpha/notes.md", "no frontmatter\n")
        return w

    def test_quiet_keeps_errors_drops_warnings_and_the_exit_status(self):
        w = self.broken()
        code, loud, _ = w.run("check")
        qcode, quiet, _ = w.run("check", "--quiet")
        self.assertEqual(code, 1)
        self.assertEqual(qcode, code)
        self.assertIn("  warn   ", loud)
        self.assertNotIn("  warn   ", quiet)
        self.assertIn("alpha/notes.md: no frontmatter", quiet)
        self.assertIn("[OK] workspace  (0 errors, 3 warnings hidden, run without --quiet)", quiet)
        self.assertIn("[FAIL] total  (1 errors, 3 warnings hidden, run without --quiet)", quiet)

    def test_short_flag(self):
        w = self.broken()
        self.assertEqual(w.run("check", "-q")[1], w.run("check", "--quiet")[1])

    def test_clean_scope_is_identical_in_both_modes(self):
        w = self.ws()
        code, loud, _ = w.run("check", "--project", "alpha")
        qcode, quiet, _ = w.run("check", "--project", "alpha", "-q")
        self.assertEqual((code, qcode), (0, 0))
        self.assertEqual(loud, quiet)
        self.assertIn("(0 errors, 0 warnings)", quiet)


if __name__ == "__main__":
    unittest.main()
