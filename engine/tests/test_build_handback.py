"""`govern build verify` and the check `unit set <id> done --branch B` runs first: a branch that
holds no commit, a dirty worktree, a forbidden commit line and an ignored ledger are refused.

    python3 -m unittest discover -s engine/tests -k build_handback
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import profile_guard  # noqa: E402,F401

from build_support import *  # noqa: E402,F401,F403
from test_roles import defaults  # noqa: E402

TRAILER = "Co-Authored-By: Claude <noreply@anthropic.com>"
FORBID = ('\n[checks.agent-roles]\nlevel = "warn"\n'
          "forbidden_trailers = ['(?im)^co-authored-by:.*claude', '(?i)generated with .*claude']\n")
BRANCH = "agent/u1"


class Handback(Build):
    def agent(self, *, commit: str | None = "src/a.py", message: str = "Add a") -> Path:
        """A branch for u1 cut from the phase's base, in a worktree of its own."""
        tree = self.tmp / "agent-tree"
        self.w.git("worktree", "add", "-q", "-b", BRANCH, str(tree), self.ledger()["base"])
        if commit:
            (tree / commit).parent.mkdir(parents=True, exist_ok=True)
            (tree / commit).write_text("print(1)\n", encoding="utf-8")
            self.w.git("add", "-A", at=tree)
            self.w.git("commit", "-qm", message, at=tree)
        return tree

    def ready(self, **kw) -> Path:
        self.started(**kw)
        self.unit("u1")
        return self.agent()

    def refused_verify(self, message: str) -> str:
        code, out, err = self.go("verify", "u1", "--branch", BRANCH)
        self.assertEqual((code, err), (1, ""), out)
        self.assertIn(message, out)
        return out

    def refused_done(self, message: str) -> str:
        err = self.refused(message, "unit", "set", "u1", "done", "--branch", BRANCH)
        self.assertEqual(self.ledger()["unit"][0]["status"], "pending")
        return err

    def test_a_clean_branch_passes_and_unit_set_done_goes_on(self):
        self.ready()
        out = self.ok("verify", "u1", "--branch", BRANCH)
        self.assertIn("is ready to be marked done", out)
        self.ok("unit", "set", "u1", "done", "--branch", BRANCH)
        self.assertEqual(self.ledger()["unit"][0]["status"], "done")

    def test_a_branch_with_no_commit_past_its_base_is_refused_by_both(self):
        self.started()
        self.unit("u1")
        self.agent(commit=None)
        short = self.ledger()["base"][:7]
        self.refused_verify(f"branch has no commit past its base {short}")
        self.refused_verify(f"branch changes nothing since its base {short}")
        self.refused_done(f"branch has no commit past its base {short}")

    def test_a_unit_base_replaces_the_ledger_base(self):
        self.ready()
        unit_base = self.head(BRANCH)
        self.assertNotEqual(unit_base, self.ledger()["base"])
        led = self.dir() / build.LEDGER
        text = led.read_text(encoding="utf-8")
        led.write_text(text.replace('base = ""', f'base = "{unit_base}"', 1),
                       encoding="utf-8")
        self.refused_verify(f"branch has no commit past its base {unit_base[:7]}")

    def test_uncommitted_files_in_the_worktree_are_refused(self):
        tree = self.ready()
        (tree / "stray.txt").write_text("x\n", encoding="utf-8")
        (tree / ".context-gate" / "build" / "p1").mkdir(parents=True)
        (tree / ".context-gate" / "build" / "p1" / "note.md").write_text("n\n", encoding="utf-8")
        out = self.refused_verify("uncommitted changes in")
        self.assertIn("stray.txt", out)
        self.assertNotIn("note.md", out)
        self.refused_done("stray.txt")

    def test_a_build_folder_alone_does_not_make_the_worktree_dirty(self):
        tree = self.ready()
        (tree / ".context-gate" / "build" / "p1").mkdir(parents=True)
        (tree / ".context-gate" / "build" / "p1" / "note.md").write_text("n\n", encoding="utf-8")
        self.ok("verify", "u1", "--branch", BRANCH)

    def test_a_forbidden_line_is_refused_only_with_the_param_set(self):
        self.started()
        self.unit("u1")
        self.agent(message=f"Fix x\n\n{TRAILER}")
        self.ok("verify", "u1", "--branch", BRANCH)

    def test_a_forbidden_line_names_the_commit_and_the_line(self):
        self.started(extra=FORBID)
        self.unit("u1")
        self.agent(message=f"Fix x\n\n{TRAILER}")
        sha = self.head(BRANCH)[:7]
        out = self.refused_verify(f"commit {sha} carries a forbidden line ({TRAILER}): "
                                  "rewrite the unpushed commit before merging")
        self.assertNotIn("Traceback", out)
        self.refused_done(TRAILER)

    def test_a_forbidden_line_in_the_middle_of_a_line_is_found_too(self):
        self.started(extra=FORBID)
        self.unit("u1")
        self.agent(message="Fix x\n\nGenerated with Claude Code")
        self.refused_verify("(Generated with Claude Code)")

    def test_an_ignored_build_folder_is_refused_at_unit_set(self):
        self.ready()
        self.w.write(".gitignore", "build/\n")
        self.refused_done("git ignores the ledger")
        self.refused_verify("git ignores the ledger")

    def test_a_scope_path_missing_from_the_diff_only_warns(self):
        self.ready()
        report = self.dir() / "u1" / "developer-report.md"
        report.write_text("Scope: `src/a.py`, `src/missing.py`\n", encoding="utf-8")
        out = self.ok("verify", "u1", "--branch", BRANCH)
        self.assertIn("warning: the report's Scope names src/missing.py", out)
        self.assertNotIn("src/a.py, which", out)
        code, _, err = self.go("unit", "set", "u1", "done", "--branch", BRANCH)
        self.assertEqual(code, 0)
        self.assertIn("src/missing.py", err)
        self.assertEqual(self.ledger()["unit"][0]["status"], "done")

    def test_an_ignored_file_beside_the_change_only_warns_and_caches_are_skipped(self):
        tree = self.ready()
        (tree / ".gitignore").write_text("*.log\n__pycache__/\n", encoding="utf-8")
        self.w.git("add", "-A", at=tree)
        self.w.git("commit", "-qm", "ignore", at=tree)
        (tree / "src" / "run.log").write_text("x\n", encoding="utf-8")
        (tree / "src" / "__pycache__").mkdir()
        (tree / "src" / "__pycache__" / "a.pyc").write_bytes(b"x")
        out = self.ok("verify", "u1", "--branch", BRANCH)
        self.assertIn("warning: git ignores src/run.log", out)
        self.assertNotIn("pyc", out)

    def test_verify_changes_nothing(self):
        self.ready()
        before = self.files()
        self.go("verify", "u1", "--branch", BRANCH)
        self.assertEqual(self.files(), before)
        self.assertEqual(self.ledger()["unit"][0]["status"], "pending")

    def test_a_missing_branch_is_an_error_not_a_pass(self):
        self.started()
        self.unit("u1")
        self.refused("there is no branch nope", "verify", "u1", "--branch", "nope")

    def test_a_bad_regex_in_forbidden_trailers_is_a_check_problem(self):
        found = roles.problems(defaults(forbidden_trailers=["ok", "(unclosed"]))
        self.assertEqual(len(found), 1, found)
        self.assertIn("[checks.agent-roles] forbidden_trailers[1]: ", found[0])
        self.assertIn("missing ), unterminated subpattern", found[0])
        self.assertEqual(roles.problems(defaults(forbidden_trailers=["ok"])), [])
        found = roles.problems(defaults(forbidden_trailers=[3]))
        self.assertIn("forbidden_trailers[0] = 3", found[0])


if __name__ == "__main__":
    unittest.main()
