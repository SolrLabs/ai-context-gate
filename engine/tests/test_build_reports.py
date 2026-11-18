"""`govern build`: `commit_reports` (agent-roles) lets unit and review reports into history.

    python3 -m unittest discover -s engine/tests -k build_reports
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import profile_guard  # noqa: E402,F401

from build_support import *  # noqa: E402,F401,F403
from build_support import BUILD  # noqa: E402

ON = '\n[checks.agent-roles]\nlevel = "warn"\ncommit_reports = true\n'
OFF = '\n[checks.agent-roles]\nlevel = "warn"\ncommit_reports = false\n'


class Reports(Build):
    def ignored(self, rel: str) -> bool:
        return self.w.git("check-ignore", (BUILD / rel).as_posix(), check=False).strip() != ""

    def text(self) -> str:
        return (self.w.root / BUILD / ".gitignore").read_text(encoding="utf-8")

    def test_on_a_report_is_tracked(self):
        w = self.started(extra=ON)
        w.write((BUILD / "p1" / "u1" / "developer-report.md").as_posix(), "r\n")
        w.write((BUILD / "p1" / "u1" / "red" / "out.txt").as_posix(), "x\n")
        self.assertFalse(self.ignored("p1/u1/developer-report.md"))
        self.assertTrue(self.ignored("p1/u1/red/out.txt"))

    def test_off_is_today(self):
        w = self.started()
        w.write((BUILD / "p1" / "u1" / "developer-report.md").as_posix(), "r\n")
        self.assertEqual(self.text(), build.GITIGNORE)
        self.assertTrue(self.ignored("p1/u1/developer-report.md"))

    def test_an_engine_file_follows_the_option(self):
        w = self.started()
        self.assertEqual(self.text(), build.GITIGNORE)
        config = w.root / layout.CONFIG
        before = config.read_text(encoding="utf-8")
        config.write_text(before + ON, encoding="utf-8")
        w.commit("on", at=w.top, index=False)
        w.branch("phase/p2")
        self.ok("start", "p2")
        self.assertEqual(self.text(), build.GITIGNORE_REPORTS)
        config.write_text(before + OFF, encoding="utf-8")
        w.commit("off", at=w.top, index=False)
        w.branch("phase/p3")
        self.ok("start", "p3")
        self.assertEqual(self.text(), build.GITIGNORE)

    def follow(self, raw: bytes) -> bytes:
        """The engine's file, replaced by `raw`, after the option goes on and a phase starts."""
        w = self.started()
        ignore = w.root / BUILD / ".gitignore"
        ignore.write_bytes(raw)
        config = w.root / layout.CONFIG
        config.write_text(config.read_text(encoding="utf-8") + ON, encoding="utf-8")
        w.commit("on", at=w.top, index=False)
        w.branch("phase/p2")
        self.ok("start", "p2")
        return ignore.read_bytes()

    def test_a_crlf_engine_file_follows_the_option(self):
        got = self.follow(build.GITIGNORE.replace("\n", "\r\n").encode("utf-8"))
        self.assertEqual(got, build.GITIGNORE_REPORTS.replace("\n", "\r\n").encode("utf-8"))

    def test_a_bom_engine_file_follows_the_option(self):
        got = self.follow(build.BOM.encode("utf-8") + build.GITIGNORE.encode("utf-8"))
        self.assertEqual(got, build.BOM.encode("utf-8") + build.GITIGNORE_REPORTS.encode("utf-8"))

    def test_the_reports_header_says_reports_are_kept(self):
        self.assertIn("stay out", build.GITIGNORE.splitlines()[0] + build.GITIGNORE.splitlines()[1])
        self.assertNotIn("Reports, review packages", build.GITIGNORE_REPORTS.split("*\n")[0])
        self.assertIn("reports are committed", build.GITIGNORE_REPORTS.split("*\n")[0])
        self.assertTrue(build.GITIGNORE_REPORTS.endswith("!*-report.md\n"))

    def test_a_hand_edited_file_is_left(self):
        w = self.started(extra=ON)
        ignore = w.root / BUILD / ".gitignore"
        edited = build.GITIGNORE + "mine\n"
        ignore.write_text(edited, encoding="utf-8")
        w.branch("phase/p2")
        self.ok("start", "p2")
        self.assertEqual(self.text(), edited)


if __name__ == "__main__":
    unittest.main()
