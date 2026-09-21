"""Release notes ship with the engine, and every version has them."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from govern import __version__, releases  # noqa: E402

SAMPLE = """# Notes

## 0.2.0 — later
- two

## 0.1.9 — now
- one

**Upgrading:** do the thing.

## 0.1.8 — before
- zero
"""


class Releases(unittest.TestCase):
    def test_this_version_has_release_notes(self):
        """A release without notes cannot ship: the upgrade report quotes them."""
        self.assertIn(__version__, [v for v, _ in releases.entries()])

    def test_between_is_after_old_through_new(self):
        got = [v for v, _ in releases.between("0.1.8", "0.1.9", SAMPLE)]
        self.assertEqual(got, ["0.1.9"])
        got = [v for v, _ in releases.between("0.1.8", "0.2.0", SAMPLE)]
        self.assertEqual(got, ["0.2.0", "0.1.9"])

    def test_unknown_old_version_gives_the_new_entry(self):
        self.assertEqual([v for v, _ in releases.between(None, "0.1.9", SAMPLE)], ["0.1.9"])

    def test_report_quotes_the_notes(self):
        import tempfile
        from govern import report
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.md"
            old = report.GateRun("gate", 0, [], engine="0.1.8")
            report.write(path, "t", old, [], "", "0.1.9", releases.between("0.1.8", "0.1.9", SAMPLE))
            text = path.read_text(encoding="utf-8")
        self.assertIn("## What changed in the engine", text)
        self.assertIn("### 0.1.9 — now", text)
        self.assertIn("**Upgrading:** do the thing.", text)
        self.assertNotIn("0.1.8 — before", text)

    def test_entry_carries_its_upgrading_note(self):
        body = dict(releases.between("0.1.8", "0.1.9", SAMPLE))["0.1.9"]
        self.assertIn("**Upgrading:** do the thing.", body)


if __name__ == "__main__":
    unittest.main()
