"""The housekeeping skill and the lines other files add for it."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from plugin_support import *  # noqa: E402,F401,F403


def flat(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


class HousekeepingSkill(unittest.TestCase):
    def setUp(self) -> None:
        self.path = PLUGIN / "skills" / "housekeeping" / "SKILL.md"
        self.text = flat(self.path)

    def test_frontmatter_names_the_skill(self) -> None:
        head = self.path.read_text(encoding="utf-8").split("---")[1]
        self.assertIn("name: housekeeping", head)
        self.assertIn("description:", head)

    def test_seven_steps_in_order(self) -> None:
        titles = re.findall(r"^## (\d)\. ", self.path.read_text(encoding="utf-8"), re.M)
        self.assertEqual(titles, list("1234567"))

    def test_measures_with_the_three_read_only_commands(self) -> None:
        for command in ("govern check", "govern sweep", "govern build status"):
            self.assertIn(command, self.text)

    def test_asks_live_sessions_before_touching_their_items(self) -> None:
        self.assertIn("ListAgents", self.text)
        self.assertIn("exact list", self.text)

    def test_handoff_is_refreshed_never_duplicated(self) -> None:
        self.assertIn("handoff-words", self.text)
        self.assertIn("the exact command that starts it", self.text)
        self.assertIn("Never create a second handoff file", self.text)

    def test_cull_list_waits_on_review_and_dirty(self) -> None:
        self.assertIn("`auto-remove` names go without asking", self.text)
        self.assertIn("every `REVIEW` and `DIRTY` item", self.text)
        self.assertIn("deletes its ignored files", self.text)

    def test_apply_uses_only_and_never_forces(self) -> None:
        self.assertIn("govern sweep --apply --only", self.text)
        self.assertIn("never passes", self.text)

    def test_close_never_pushes_and_reports_readiness(self) -> None:
        self.assertIn("0 errors", self.text)
        self.assertIn("Never push, merge or force", self.text)
        self.assertIn("Ready for a new session", self.text)
        self.assertIn("unpushed commits", self.text)


class LinesOtherFilesAdd(unittest.TestCase):
    def test_build_skill_runs_housekeeping_after_phase_close(self) -> None:
        text = flat(PLUGIN / "skills" / "build" / "SKILL.md")
        self.assertIn("When the session is ending, run the `housekeeping` skill", text)
        self.assertLess(text.index("govern phase close"), text.index("run the `housekeeping` skill"))

    def test_writer_handoff_ends_with_the_start_command(self) -> None:
        text = flat(ENGINE / "govern" / "templates" / "agents" / "technical-writer.md")
        self.assertIn("ending with the next step and the exact command that starts it", text)

    def test_options_skill_names_the_sweep_setting(self) -> None:
        text = flat(PLUGIN / "skills" / "options" / "SKILL.md")
        self.assertIn("`[sweep] auto_remove`", text)
        self.assertIn("older than 0.7.0", text)

    def test_options_skill_says_to_check_before_restarting(self) -> None:
        text = flat(PLUGIN / "skills" / "options" / "SKILL.md")
        self.assertIn("After `govern beta on`, run `python3 .context-gate/bin/govern check` once "
                      "before restarting Claude Code", text)


if __name__ == "__main__":
    unittest.main()
