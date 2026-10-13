"""The memory index check: it counts memories, leaves out the kinds that never go stale, and
asks for one memory per line.

    python3 -m unittest discover -s engine/tests -k test_memory
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from govern import text  # noqa: E402
from govern.checks.memory import memory_index  # noqa: E402
from base_support import Case  # noqa: E402
from regressions_support import Workspace  # noqa: E402


class Ctx:
    def __init__(self, root: Path, home: Path) -> None:
        self.root = root
        self.home = home


class MemoryIndex(Case):
    def setUp(self) -> None:
        super().setUp()
        root = self.tmp / "work"
        root.mkdir()
        self.ctx = Ctx(root, self.tmp / "home")
        self.mem = (self.ctx.home / ".claude" / "projects" / text.claude_project_slug(root)
                    / "memory")
        self.mem.mkdir(parents=True)
        self.params = {"max_lines": 40, "exempt_types": ["feedback"], "one_link_per_line": True}

    def memory(self, name: str, content: str | bytes) -> None:
        data = content.encode("utf-8") if isinstance(content, str) else content
        (self.mem / name).write_bytes(data)

    def index(self, lines: list[str]) -> None:
        (self.mem / "MEMORY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def entries(self, n: int, kind: str, start: int = 0) -> list[str]:
        for i in range(start, start + n):
            self.memory(f"m{i}.md", f"---\ntype: {kind}\n---\nbody\n")
        return [f"- [m{i}](m{i}.md) — note" for i in range(start, start + n)]

    def run_check(self, **over):
        return memory_index(self.ctx, {**self.params, **over})

    def counted(self, content: str | bytes | None) -> bool:
        """Whether a one-line index counts its line, with one memory file of this content (none
        when None) beside it; asked by setting the limit to 0."""
        if content is not None:
            self.memory("a.md", content)
        self.index(["- [a](a.md) — note"])
        return bool(self.run_check(max_lines=0).warnings)


class ExemptKinds(MemoryIndex):
    def test_forty_five_lines_of_which_ten_are_feedback_is_not_a_finding(self):
        self.index(self.entries(10, "feedback") + self.entries(35, "project", 10))
        f = self.run_check()
        self.assertEqual((f.errors, f.warnings), ([], []))

    def test_forty_five_lines_of_which_two_are_feedback_says_how_many_are_exempt(self):
        self.index(self.entries(2, "feedback") + self.entries(43, "project", 2))
        f = self.run_check()
        self.assertEqual(f.warnings, [
            f"memory index: 43 lines exceeds memory-index max_lines=40 "
            f"(2 more are exempt: feedback) ({self.mem / 'MEMORY.md'})"])

    def test_no_exempt_lines_keeps_the_message_as_it_was(self):
        self.index(self.entries(45, "project"))
        self.assertEqual(self.run_check().warnings, [
            f"memory index: 45 non-blank lines exceeds memory-index max_lines=40 "
            f"({self.mem / 'MEMORY.md'})"])

    def test_an_empty_exempt_list_restores_the_old_count(self):
        self.index(self.entries(10, "feedback") + self.entries(35, "project", 10))
        self.assertIn("45 non-blank lines", self.run_check(exempt_types=[]).warnings[0])

    def test_the_finding_is_a_warning(self):
        self.index(self.entries(45, "project"))
        f = self.run_check()
        self.assertEqual((len(f.errors), len(f.warnings)), (0, 1))

    def test_a_listed_kind_other_than_the_default_is_exempt(self):
        self.index(self.entries(45, "user"))
        self.assertEqual(self.run_check(exempt_types=["user"]).warnings, [])


class ThroughTheCommand(Case):
    """The real `check`, a real config and a temp HOME: not a stub context."""

    def index(self, w: Workspace) -> None:
        mem = (w.home / ".claude" / "projects" / text.claude_project_slug(w.root) / "memory")
        mem.mkdir(parents=True)
        lines = []
        for i in range(45):
            kind = "feedback" if i < 10 else "project"
            (mem / f"m{i}.md").write_text(f"---\ntype: {kind}\n---\nbody\n", encoding="utf-8")
            lines.append(f"- [m{i}](m{i}.md) — note")
        (mem / "MEMORY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_ten_feedback_lines_of_forty_five_give_no_finding(self):
        w = Workspace(self.tmp)
        self.index(w)
        code, out, _ = w.run("check")
        self.assertNotIn("memory index", out)

    def test_an_empty_exempt_types_in_the_config_gives_the_old_message(self):
        w = Workspace(self.tmp, extra="\n[checks.memory-index]\nexempt_types = []\n")
        self.index(w)
        code, out, _ = w.run("check")
        self.assertIn("memory index: 45 non-blank lines exceeds memory-index max_lines=40", out)


class ReadingTheKind(MemoryIndex):
    def test_type_under_metadata_is_the_kind(self):
        self.assertFalse(self.counted("---\nname: x\nmetadata:\n  type: feedback\n---\n"))

    def test_top_level_type_is_the_kind(self):
        self.assertFalse(self.counted("---\ntype: feedback\n---\n"))

    def test_a_quoted_type_is_the_kind(self):
        self.assertFalse(self.counted("---\ntype: \"feedback\"\n---\n"))
        self.assertFalse(self.counted("---\ntype: 'feedback'\n---\n"))

    def test_node_type_is_not_the_kind(self):
        self.assertTrue(self.counted("---\nnode_type: feedback\n---\n"))

    def test_a_type_after_the_frontmatter_is_not_the_kind(self):
        self.assertTrue(self.counted("---\nname: x\n---\ntype: feedback\n"))

    def test_a_missing_file_is_counted(self):
        self.assertTrue(self.counted(None))

    def test_a_file_without_frontmatter_is_counted(self):
        self.assertTrue(self.counted("type: feedback\nbody\n"))

    def test_a_file_that_is_not_utf8_is_counted(self):
        self.assertTrue(self.counted(b"---\ntype: feedback\n---\n\xff\xfe"))

    def test_a_line_without_a_link_is_counted(self):
        self.index(["- just a note"])
        self.assertTrue(self.run_check(max_lines=0).warnings)

    def test_a_link_out_of_the_memory_folder_is_not_followed(self):
        (self.mem.parent / "x.md").write_text("---\ntype: feedback\n---\n", encoding="utf-8")
        self.index(["- [x](../x.md) — note"])
        self.assertTrue(self.run_check(max_lines=0).warnings)
        self.index(["- [x](../../../../x.md) — note"])
        self.assertTrue(self.run_check(max_lines=0).warnings)


class OneMemoryPerLine(MemoryIndex):
    def test_a_line_with_two_memory_links_warns_once_naming_the_line(self):
        self.memory("a.md", "---\ntype: project\n---\n")
        self.memory("b.md", "---\ntype: project\n---\n")
        self.index(["- [a](a.md) — one", "", "- [a](a.md) and [b](b.md)"])
        f = self.run_check()
        self.assertEqual(f.errors, [])
        self.assertEqual(f.warnings, [
            f"memory index: line 3 links 2 memories — one memory per line, so the limit "
            f"counts memories ({self.mem / 'MEMORY.md'})"])

    def test_a_memory_link_and_a_url_is_one_memory(self):
        self.memory("a.md", "---\ntype: project\n---\n")
        self.index(["- [a](a.md) — see [docs](https://example.com/guide.md) and "
                    "[site](https://example.com)"])
        self.assertEqual(self.run_check().warnings, [])

    def test_turned_off_it_is_silent(self):
        self.index(["- [a](a.md) and [b](b.md)"])
        self.assertEqual(self.run_check(one_link_per_line=False).warnings, [])


if __name__ == "__main__":
    unittest.main()
