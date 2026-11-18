"""Adopt reads decision headings written `## X1: Title` or `## X-1: Title`: they count as
entries, set the prefix, and the migration rewrites them to the canonical form.

    python3 -m unittest discover -s engine/tests -k adopt_loose
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import profile_guard  # noqa: E402,F401

from govern import measure, migrate  # noqa: E402
from govern.propose import propose  # noqa: E402

import migrate_support as ms  # noqa: E402

BODY = "\n**Status:** locked\n\n**Rule:** x.\n\n**Why:** y.\n"
TABLE = "| Area | Owner |\n|------|-------|\n| API  | Ana   |\n| UI   | Bo    |\n\n"
LOOSE = ms.FM + "# Decisions\n\n" + TABLE + "".join(
    f"## X{n}: Title {n}\n{BODY}\n" for n in (1, 2, 3))
CANON = ms.FM + "# Decisions\n\n" + "".join(
    f"## D-{n} - Title {n}\n{BODY}\n" for n in (1, 2, 3))


class LooseHeadings(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def log_of(self, text: str) -> measure.DecisionLog:
        root = self.tmp / "repo"
        ms.wtext(root / "docs/DECISIONS.md", text)
        ms.git(root, "init", "-q")
        ms.git(root, "add", "-A")
        ms.git(root, "commit", "-qm", "initial")
        self.m = measure.measure(root)
        return self.m.workspace.decision_logs[0]

    def test_loose_headings_count_and_set_the_prefix(self):
        log = self.log_of(LOOSE)
        self.assertEqual((log.entries, log.loose, log.prefixes, log.lowest, log.level),
                         (3, 3, ["X"], 1, 2))
        p = propose(self.m, None, None, {"shape": "single"})
        self.assertEqual(p.config["repo"]["id_prefix"], "X")
        self.assertTrue(p.config["repo"]["id_range"].startswith("1-"), p.config["repo"])
        self.assertTrue(p.migrate)

    def test_hyphenated_loose_heading_is_loose(self):
        log = self.log_of(ms.FM + "## X-2: Title\n" + BODY)
        self.assertEqual((log.entries, log.loose, log.lowest), (1, 1, 2))

    def test_canonical_heading_and_a_colon_without_space_are_not_loose(self):
        log = self.log_of(ms.FM + "## D-3 - Title\n" + BODY + "\n## X1:Title\n" + BODY)
        self.assertEqual((log.entries, log.loose), (1, 0))

    def test_canonical_log_is_measured_as_before(self):
        log = self.log_of(CANON)
        self.assertEqual((log.entries, log.loose, log.prefixes), (3, 0, ["D"]))
        p = propose(self.m, None, None, {"shape": "single"})
        self.assertFalse(p.migrate)

    def test_loose_heading_in_a_code_fence_is_ignored(self):
        log = self.log_of(ms.FM + "```\n## X1: Title\n```\n")
        self.assertEqual((log.entries, log.loose), (0, 0))


# In the migrate fixture's own prefix and range, with the index its config declares.
IN_RANGE = ms.FM + "# Decisions\n\n" + ms.DECISIONS_INDEX + "\n" + TABLE + "".join(
    f"## D{n}: Title {n}\n{BODY}\n" for n in (500, 501, 502))


class MigrateLoose(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def idents(self, w: "ms.Fixture") -> list[str]:
        parsed = w.context().grammar.parse_file(w.log())
        self.assertEqual(parsed.malformed, [])
        return [e.ident for e in parsed.entries]

    def test_loose_headings_are_rewritten_and_the_table_is_left(self):
        w = ms.Fixture(self.tmp, decisions_text=IN_RANGE)
        code, _ = w.run(apply_=True)
        self.assertEqual(code, 0)
        text = w.log().read_text(encoding="utf-8")
        self.assertIn(TABLE, text)
        self.assertNotIn(": Title", text)
        self.assertEqual(self.idents(w), ["D-500", "D-501", "D-502"])
        self.assertEqual([f for f in migrate._findings(w.context()) if f[0] == "decision-log"], [])

    def test_hyphenated_loose_heading_and_second_run_changes_nothing(self):
        w = ms.Fixture(self.tmp, decisions_text=ms.FM + "# Decisions\n\n## D-7: Title\n" + BODY)
        w.run(apply_=True)
        once = w.log().read_bytes()
        self.assertEqual(self.idents(w), ["D-7"])
        w.commit()
        w.run(apply_=True)
        self.assertEqual(w.log().read_bytes(), once)

    def test_crlf_stays_crlf(self):
        text = (ms.FM + "# Decisions\n\n## D-7: Title\n" + BODY).replace("\n", "\r\n")
        w = ms.Fixture(self.tmp, decisions_text="")
        ms.wtext(w.log(), text)
        w.commit()
        w.run(apply_=True)
        raw = w.log().read_bytes()
        self.assertIn(b"D-7 ", raw)
        self.assertNotIn(b"D-7:", raw)
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))


if __name__ == "__main__":
    unittest.main()
