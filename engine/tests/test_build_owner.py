"""`govern build`: the phase's owner, the email it was started under, and `--take-over`."""
from __future__ import annotations

import dataclasses
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import profile_guard  # noqa: E402,F401

from build_support import *  # noqa: E402,F401,F403


class Owner(Build):
    def as_(self, email: str | None) -> None:
        """Play a person: the repository's own `user.email`."""
        if email is None:
            self.w.git("config", "--unset", "user.email", check=False)
        else:
            self.w.git("config", "user.email", email)

    def start_as(self, email: str | None) -> None:
        self.project()
        self.as_(email)
        self.ok("start", "p1")

    def raw(self) -> bytes:
        return (self.dir() / build.LEDGER).read_bytes()

    def test_start_records_the_owner(self):
        self.start_as("a@example.com")
        self.assertEqual(self.ledger()["owner"], "a@example.com")

    def test_no_email_records_no_owner(self):
        self.start_as(None)
        self.assertNotIn("owner", self.ledger())

    def test_another_email_is_refused(self):
        self.start_as("a@example.com")
        self.as_("b@example.com")
        before = self.raw()
        self.refused("belongs to a@example.com; run with --take-over", "ruling", "x")
        self.assertEqual(self.raw(), before)

    def test_status_is_allowed_for_another_email(self):
        self.start_as("a@example.com")
        self.as_("b@example.com")
        self.ok("status")

    def test_case_is_ignored(self):
        self.start_as("A@Example.com")
        self.as_("a@example.com")
        self.ok("ruling", "x")

    def test_no_current_email_is_not_refused(self):
        self.start_as("a@example.com")
        self.as_(None)
        self.ok("ruling", "x")

    def test_take_over_rules_and_moves(self):
        self.start_as("a@example.com")
        self.as_("b@example.com")
        out = self.ok("ruling", "x", "--take-over")
        self.assertIn("took over p1 from a@example.com", out)
        data = self.ledger()
        self.assertEqual(data["owner"], "b@example.com")
        today = time.strftime("%Y-%m-%d", time.gmtime())
        self.assertTrue(data["rulings"][0].startswith(f"taken over from a@example.com on {today}"),
                        data["rulings"])
        self.assertEqual(data["rulings"][-1], "x")

    def test_start_take_over_restarts_anothers_phase(self):
        self.start_as("b@example.com")
        self.w.commit("start", at=self.w.top, index=False)
        self.as_("a@example.com")
        self.refused("belongs to b@example.com; run with --take-over", "start", "p1")
        out = self.ok("start", "p1", "--take-over")
        self.assertIn("took over p1 from b@example.com", out)
        data = self.ledger()
        self.assertEqual(data["owner"], "a@example.com")
        self.assertEqual(len(data["rulings"]), 1)
        self.assertTrue(data["rulings"][0].startswith("taken over from b@example.com on "))
        changed = self.w.git("status", "--porcelain", "--untracked-files=no").split("\n")
        self.assertEqual([c for c in changed if c.strip()],
                         [f" M {(BUILD / 'p1' / build.LEDGER).as_posix()}"])

    def test_render_round_trips_owner(self):
        led = ledger(Path("build"), owner="a@example.com")
        self.assertIn('owner = "a@example.com"\n', build.render(led))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p1" / build.LEDGER
            path.parent.mkdir()
            path.write_bytes(build.encoded(dataclasses.replace(led, path=path)))
            self.assertEqual(build.load(path).owner, "a@example.com")

    def test_a_ledger_without_owner_is_unchanged(self):
        self.assertNotIn("owner", build.render(ledger(Path("build"))))

    def test_an_owner_that_is_not_text_is_refused(self):
        data = {"phase": "p1", "branch": "b", "base": "x", "stage": "build", "owner": 3}
        self.assertEqual(build._shape(data, "p1"), "`owner` is not text")


if __name__ == "__main__":
    unittest.main()
