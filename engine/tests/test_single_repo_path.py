"""`check --project <name> --path <copy>` in a single repo: the root's own checks (writing-rules
and the rest) run on the copy too, as a plain `check` would, so a git pre-commit hook sees what
the later gate sees.

    python3 -m unittest discover -s engine/tests -k single_repo_path
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))

sys.path.insert(0, str(Path(__file__).resolve().parent))

import profile_guard  # noqa: E402,F401

from regressions_support import Workspace  # noqa: E402
from test_single_repo import SingleRepo, wtext  # noqa: E402

DASH = "—"
WRITING = f"""
[checks.writing-rules]
level = "error"
files = ["notes/*.md"]
rules = [{{ text = "{DASH}", use = "a comma" }}]
"""
CLEAN, DIRTY = "# Notes\n\nPlain words.\n", f"# Notes\n\nWords {DASH} with a dash.\n"


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def repo(self, notes: str) -> SingleRepo:
        r = SingleRepo(self.tmp, extra=WRITING)
        r.write("notes/a.md", notes)
        return r

    def copy(self, r: SingleRepo, name: str = "snap") -> Path:
        snap = self.tmp / name
        shutil.copytree(r.root, snap)
        return snap


class SingleRepoPath(Base):
    def test_a_forbidden_character_in_the_copy_fails_and_names_the_file(self):
        r = self.repo(CLEAN)
        snap = self.copy(r)
        wtext(snap / "notes/a.md", DIRTY)
        code, out, _ = r.run("check", "--project", "myproject", "--path", str(snap))
        self.assertEqual(code, 1, out)
        self.assertIn("notes/a.md", out)

    def test_a_clean_copy_passes_though_the_working_tree_is_not(self):
        r = self.repo(CLEAN)
        snap = self.copy(r)
        r.write("notes/a.md", DIRTY)
        code, out, _ = r.run("check", "--project", "myproject", "--path", str(snap))
        self.assertEqual(code, 0, out)
        self.assertNotIn("notes/a.md", out)

    def test_the_finding_appears_once(self):
        r = self.repo(CLEAN)
        snap = self.copy(r)
        wtext(snap / "notes/a.md", DIRTY)
        _, out, _ = r.run("check", "--project", "myproject", "--path", str(snap))
        self.assertEqual(out.count("notes/a.md"), 1, out)
        self.assertNotIn(str(snap), out)

    def test_without_path_the_project_run_checks_the_tree_too(self):
        r = self.repo(DIRTY)
        code, out, _ = r.run("check", "--project", "myproject")
        self.assertEqual(code, 1, out)
        self.assertEqual(out.count("notes/a.md"), 1, out)

    def test_workspace_only_is_refused_and_points_at_project(self):
        code, _, err = self.repo(CLEAN).run("check", "--workspace-only")
        self.assertEqual(code, 2, err)
        self.assertTrue(err.rstrip().endswith(
            "; check --project myproject already runs the root's checks"), err)


class RegistryWorkspace(Base):
    def test_project_path_still_leaves_the_workspace_checks_out(self):
        w = Workspace(self.tmp, extra=WRITING.replace("notes/*.md", "governance/*.md"))
        w.write("governance/notes.md", DIRTY)
        snap = self.tmp / "snap"
        shutil.copytree(w.root / "projects/alpha", snap)
        code, out, _ = w.run("check", "--project", "alpha", "--path", str(snap))
        self.assertEqual(code, 0, out)
        self.assertNotIn("governance/notes.md", out)
        code, out, _ = w.run("check")
        self.assertEqual(code, 1, out)


if __name__ == "__main__":
    unittest.main()
