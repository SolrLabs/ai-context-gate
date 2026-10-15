"""`govern build launch` in a workspace: a writing agent works in a worktree of the member, made
inside the member's repository and named on a `Worktree:` line, and the agent gates find the agent's
tree from that line.

    python3 -m unittest discover -s engine/tests -k test_build_worktree
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_build_workspace as ws  # noqa: E402
from agent_runs_support import HookCase, setUpModule, tearDownModule  # noqa: E402,F401
from build_support import BUILD, Build, agent_runs, build  # noqa: E402

NOTE = "launch it without isolation: the worktree is made"


def fill(brief: Path) -> None:
    text = "".join("Filled.\n" if line.startswith(agent_runs.FILL) else line + "\n"
                   for line in brief.read_text(encoding="utf-8").splitlines())
    brief.write_text(text, encoding="utf-8")


class Worktree(ws.Workspace):
    def ready(self, uid: str = "u1") -> Path:
        """Alpha with a started build and a filled, committed brief for `uid`."""
        self.started("alpha")
        member = self.w.member("alpha")
        self.ok("--project", "alpha", "unit", "add", uid, "--title", "One")
        fill(member / BUILD / "p1" / uid / "brief.md")
        self.w.commit("briefs", at=member, index=False)
        return member

    def tree(self, uid: str = "u1") -> Path:
        return self.w.root / ".claude" / "worktrees" / f"alpha-p1-{uid}"

    def test_launch_makes_the_worktree_and_prints_the_three_lines(self):
        member = self.ready()
        head = self.w.git("rev-parse", "HEAD", at=member).strip()
        out = self.ok("--project", "alpha", "launch", "u1")
        self.assertEqual(out.splitlines(), [
            "launch developer with the prompt:",
            f"Brief: {BUILD.as_posix()}/p1/u1/brief.md",
            f"Base: {head}",
            f"Worktree: {self.tree()}",
            NOTE])
        self.assertTrue((self.tree() / "readme.txt").is_file())
        self.assertEqual(self.w.git("rev-parse", "--abbrev-ref", "HEAD", at=self.tree()).strip(),
                         "build/p1/u1")
        self.assertEqual(self.w.git("rev-parse", "HEAD", at=self.tree()).strip(), head)
        # It is a worktree of the member, not of the workspace root.
        self.assertIn("build/p1/u1", build.phase._worktrees(member))
        self.assertEqual(self.ledger("alpha")["unit"][0]["status"], "running")

    def test_a_second_launch_reuses_the_worktree(self):
        self.ready()
        self.ok("--project", "alpha", "launch", "u1")
        marker = self.tree() / "kept.txt"
        marker.write_text("mine\n", encoding="utf-8")
        out = self.ok("--project", "alpha", "launch", "u1")
        self.assertIn(f"Worktree: {self.tree()}", out.splitlines())
        self.assertTrue(marker.is_file())
        self.assertEqual(self.ledger("alpha")["unit"][0]["attempts"], 2)

    def test_something_else_at_the_path_is_refused_and_nothing_is_removed(self):
        self.ready()
        self.tree().mkdir(parents=True)
        keep = self.tree() / "mine.txt"
        keep.write_text("mine\n", encoding="utf-8")
        self.refused("is already there and is not a worktree of alpha on build/p1/u1",
                     "--project", "alpha", "launch", "u1")
        self.assertTrue(keep.is_file())
        self.assertEqual(self.ledger("alpha")["unit"][0]["status"], "pending")

    def test_a_worktree_of_another_branch_at_the_path_is_refused(self):
        member = self.ready()
        self.tree().parent.mkdir(parents=True)
        self.w.git("worktree", "add", str(self.tree()), "-b", "other", at=member)
        self.refused("is already there and is not a worktree of alpha on build/p1/u1",
                     "--project", "alpha", "launch", "u1")
        self.assertTrue(self.tree().is_dir())

    def test_a_reviewers_launch_names_the_member_and_makes_no_worktree(self):
        member = self.ready()
        self.ok("--project", "alpha", "unit", "set", "u1", "done")
        self.w.write("alpha/src/a.py", "print(1)\n")
        self.w.commit("code", at=member, index=False)
        self.ok("--project", "alpha", "review")
        for brief in (member / BUILD / "p1" / "review").glob("*-brief.md"):
            fill(brief)
        self.w.commit("review briefs", at=member, index=False)
        out = self.ok("--project", "alpha", "launch", "review")
        self.assertEqual(out.splitlines(), [
            "launch code-reviewer with the prompt:",
            f"Brief: {BUILD.as_posix()}/p1/review/code-reviewer-brief.md",
            f"Worktree: {member}"])
        self.assertFalse((self.w.root / ".claude").exists())

    def test_unit_set_branch_copies_a_report_written_in_the_members_worktree(self):
        member = self.ready()
        self.ok("--project", "alpha", "launch", "u1")
        report = self.tree() / BUILD / "p1" / "u1" / "developer-report.md"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("Status: DONE\n", encoding="utf-8")
        out = self.ok("--project", "alpha", "unit", "set", "u1", "done", "--branch", "build/p1/u1")
        self.assertIn(f"copied {BUILD.as_posix()}/p1/u1/developer-report.md", out)
        self.assertEqual((member / BUILD / "p1" / "u1" / "developer-report.md")
                         .read_text(encoding="utf-8"), "Status: DONE\n")

    def test_collect_copies_ignored_files_from_the_members_worktree(self):
        member = self.ready()
        self.ok("--project", "alpha", "launch", "u1")
        self.assertEqual(build._branch_tree(member, "build/p1/u1"), self.tree().resolve())
        self.assertIn("no worktree has nope checked out",
                      self.ok("--project", "alpha", "collect", "nope"))


class Single(Build):
    def test_a_single_repository_prints_no_worktree_line(self):
        self.started()
        brief = self.unit("u1")
        fill(brief)
        self.w.commit("briefs", at=self.w.top, index=False)
        out = self.ok("launch", "u1")
        self.assertEqual(out.splitlines(), [
            "launch developer with the prompt:", f"Brief: {BUILD.as_posix()}/p1/u1/brief.md",
            f"Base: {self.head()}"])
        self.assertNotIn("Worktree", out)
        self.assertFalse((self.w.root / ".claude").exists())


class Gates(HookCase):
    """The records a launch with a `Worktree:` line leaves: `Produces:` is the worktree's."""
    BRIEF = ".context-gate/build/p1/u1/brief.md"
    REPORT = ".context-gate/build/p1/u1/developer-report.md"

    def brief_in(self, folder: Path) -> None:
        path = folder / self.BRIEF
        path.parent.mkdir(parents=True)
        path.write_text(f"Task.\nProduces: {self.REPORT}\n", encoding="utf-8")

    def test_produces_resolves_to_the_named_worktree(self):
        tree = self.tmp / "tree"
        self.brief_in(tree)
        self.launch(f"Brief: {self.BRIEF}\nBase: abc\nWorktree: {tree}",
                    agent_type="developer", cwd=str(self.root), start_cwd=str(self.work))
        record = agent_runs.read_json(self.state / "launch-a1.json")
        self.assertEqual(record["launch_cwd"], str(tree))
        self.assertEqual(agent_runs.where(record, "produces", record["produces"][0])[0], tree / self.REPORT)
        out = self.stop()
        self.assertIn(str(tree / self.REPORT), out["reason"])
        (tree / self.REPORT).write_text("done\n", encoding="utf-8")
        self.assertIsNone(self.stop())

    def test_the_start_puts_the_promised_paths_in_the_worktree(self):
        tree = self.tmp / "tree"
        brief = tree / self.BRIEF
        brief.parent.mkdir(parents=True)
        brief.write_text("Task.\nProduces: developer-report.md\n", encoding="utf-8")
        want = str(tree / Path(self.BRIEF).parent / "developer-report.md")
        for n, start in enumerate((None, str(self.work))):
            agent = f"a{n}"
            self.launch(f"Brief: {self.BRIEF}\nBase: abc\nWorktree: {tree}", agent=agent,
                        agent_type="developer", cwd=str(self.root), start_cwd=start)
            record = agent_runs.read_json(self.state / f"launch-{agent}.json")
            self.assertEqual(record["produces"], [want])
            self.assertEqual(record["in_brief"]["produces"], [[want, "developer-report.md"]])

    def test_a_launch_without_the_line_resolves_as_before(self):
        self.brief_in(self.root)
        self.launch(f"Brief: {self.BRIEF}", agent_type="developer", cwd=str(self.root),
                    start_cwd=str(self.root))
        record = agent_runs.read_json(self.state / "launch-a1.json")
        self.assertEqual(record["launch_cwd"], str(self.root))
        self.assertNotIn("worktree", record)
        self.assertIn(str(self.root / self.REPORT), self.stop()["reason"])

    def test_a_relative_or_missing_worktree_is_ignored(self):
        self.brief_in(self.root)
        for n, line in enumerate(("Worktree: tree", f"Worktree: {self.tmp / 'gone'}")):
            agent = f"a{n}"
            self.launch(f"Brief: {self.BRIEF}\n{line}", agent=agent, agent_type="developer",
                        cwd=str(self.root), start_cwd=str(self.root))
            record = agent_runs.read_json(self.state / f"launch-{agent}.json")
            self.assertNotIn("worktree", record)
            self.assertEqual(agent_runs.where(record, "produces", record["produces"][0])[0], self.root / self.REPORT)


if __name__ == "__main__":
    unittest.main()
