"""The `agent-roster` block leaves out role files that exist on this machine only (UB-144).

    python3 -m unittest discover -s engine/tests -k test_roster_local
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))

from govern import blocks, layout, roles  # noqa: E402


def role(name: str) -> str:
    return f"---\nname: {name}\nmodel: sonnet\neffort: low\nmaxTurns: 5\ndescription: does {name}\n---\nbody\n"


class RosterLocal(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        adir = self.root / ".claude" / "agents"
        adir.mkdir(parents=True)
        for name in ("alpha", "beta", "gamma"):
            (adir / f"{name}.md").write_text(role(name), encoding="utf-8", newline="\n")
        (self.root / layout.GOV_DIR).mkdir()
        self.ctx = SimpleNamespace(root=self.root, workspace=lambda key, default: default)

    def record(self, text: str) -> None:
        (self.root / roles.LOCAL_RECORD).write_text(text, encoding="utf-8", newline="\n")

    def test_local_role_left_out(self) -> None:
        self.record('[[rendered]]\npath = ".claude/agents/gamma.md"\nrole = "gamma"\n')
        out = blocks.agent_roster(self.ctx)
        self.assertIn("`alpha`", out)
        self.assertIn("`beta`", out)
        self.assertNotIn("gamma", out)

    def test_no_record_lists_all(self) -> None:
        out = blocks.agent_roster(self.ctx)
        for name in ("alpha", "beta", "gamma"):
            self.assertIn(f"`{name}`", out)
        self.assertEqual(roles.local_paths(self.root), set())

    def test_unreadable_record_reads_empty(self) -> None:
        (self.root / roles.LOCAL_RECORD).write_bytes(b"\xff\xfe not toml [[")
        self.assertEqual(roles.local_paths(self.root), set())
        self.assertIn("`gamma`", blocks.agent_roster(self.ctx))

    def test_local_paths_posix(self) -> None:
        self.record('[[rendered]]\npath = ".claude/agents/gamma.md"\n')
        self.assertEqual(roles.local_paths(self.root), {".claude/agents/gamma.md"})


if __name__ == "__main__":
    unittest.main()
