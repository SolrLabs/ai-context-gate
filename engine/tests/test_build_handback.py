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

    def commit_files(self, tree: Path, *names: str) -> None:
        for name in names:
            (tree / name).parent.mkdir(parents=True, exist_ok=True)
            (tree / name).write_text("x\n", encoding="utf-8")
        self.w.git("add", "-A", at=tree)
        self.w.git("commit", "-qm", "more", at=tree)

    def scope_warnings(self, scope: str) -> list[str]:
        (self.dir() / "u1" / "developer-report.md").write_text(f"Scope: {scope}\n",
                                                               encoding="utf-8")
        out = self.ok("verify", "u1", "--branch", BRANCH)
        return [ln for ln in out.splitlines() if ln.startswith("warning:")]

    def test_scope_patterns_covering_the_branch_do_not_warn(self):
        tree = self.ready()
        self.commit_files(tree, *(f"rnd/runlog/issues/I-000{n}.json" for n in (1, 2, 3)),
                          "rnd/tests/fixtures/runlog/run-full/a.json",
                          "rnd/tests/fixtures/runlog/run-bare/b.json",
                          "rnd/runlog/runs/2026/r.json", "rnd/runlog/games/g.json")
        (self.dir() / "u1" / "developer-report.md").write_text(
            "Scope: `rnd/runlog/issues/I-0001..I-0003.json`, "
            "`rnd/tests/fixtures/runlog/{run-full/, run-bare/}`, `rnd/runlog/runs/2026/`, "
            "`rnd/runlog/games/*.json`\n", encoding="utf-8")
        out = self.ok("verify", "u1", "--branch", BRANCH)
        self.assertIn("is ready to be marked done", out)
        self.assertNotIn("warning:", out)

    def test_a_missing_range_member_is_named(self):
        tree = self.ready()
        self.commit_files(tree, *(f"rnd/runlog/issues/I-000{n}.json" for n in (1, 2, 3)))
        found = self.scope_warnings("`rnd/runlog/issues/I-0001..I-0004.json`")
        self.assertEqual(found, ["warning: the report's Scope names "
                                 "rnd/runlog/issues/I-0001..I-0004.json, which the branch does "
                                 "not change (1 of 4 not changed: rnd/runlog/issues/I-0004.json)"])

    def test_a_plain_path_the_branch_does_not_change_still_warns(self):
        self.ready()
        found = self.scope_warnings("`src/a.py`, `src/missing.py`")
        self.assertEqual(found, ["warning: the report's Scope names src/missing.py, which the "
                                 "branch does not change"])

    def test_a_file_an_earlier_round_changed_still_counts(self):
        tree = self.ready()
        self.commit_files(tree, "src/x.py")
        led = self.dir() / build.LEDGER
        text = led.read_text(encoding="utf-8")
        led.write_text(text.replace('base = ""', f'base = "{self.head(BRANCH)}"', 1),
                       encoding="utf-8")
        self.commit_files(tree, "src/y.py")
        self.assertEqual(self.scope_warnings("src/x.py, src/y.py"), [])

    def test_an_unresolvable_entry_is_one_line(self):
        self.ready()
        found = self.scope_warnings("`a{b/c.py`, `x/1..y.py`")
        self.assertEqual(found, ["warning: could not resolve the report's Scope entries: "
                                 "a{b/c.py, x/1..y.py"])

    def test_glob_stars_in_a_scope_are_not_stripped_as_bold(self):
        tree = self.ready()
        self.commit_files(tree, "engine/tests/test_x.py", "README.md")
        self.assertEqual(self.scope_warnings("`**/test_*.py`, `*.md`"), [])
        self.assertEqual(build._scope_paths("Scope: `**src/a.py**`, `**/t_*.py`, `*.md`"),
                         ["src/a.py", "**/t_*.py", "*.md"])

    def test_a_glob_ending_in_a_slash_covers_a_folder_it_matches(self):
        cover = build._scope_cover
        self.assertEqual(cover("rnd/*/", ["rnd/runlog/a.json"]), [])
        self.assertEqual(cover("rnd/*/", ["other/a.json"]), ["rnd/*/"])

    def test_scope_cover_globs_folders_and_limits(self):
        cover = build._scope_cover
        self.assertEqual(cover("a/**/b.py", ["a/b.py"]), [])
        self.assertEqual(cover("a/**/b.py", ["a/x/y/b.py"]), [])
        self.assertEqual(cover("a/*.py", ["a/x/b.py"]), ["a/*.py"])
        self.assertEqual(cover("rnd/runlog", ["rnd/runlog/x"]), [])
        self.assertEqual(cover("rnd/run", ["rnd/runlog/x"]), ["rnd/run"])
        self.assertEqual(cover("./a\\b.py", ["a/b.py"]), [])
        self.assertEqual(cover("{a,b}/{c,d}.py", ["a/c.py", "b/d.py"]), ["a/d.py", "b/c.py"])
        self.assertIsNone(cover("{a,{b,c}}/x.py", []))
        self.assertIsNone(cover("a{b", []))
        self.assertIsNone(cover("1..y.py", []))
        self.assertIsNone(cover("I-0009..I-0001.json", []))
        self.assertEqual(len(cover("I-0001..I-1000.json", [])), 1000)
        self.assertIsNone(cover("I-0001..I-1001.json", []))


if __name__ == "__main__":
    unittest.main()
