"""`govern build`: review, the fix pass and close, launching agents, and the briefs."""
from __future__ import annotations

import json
import stat
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_support import *  # noqa: E402,F401,F403

class Review(Build):
    def ready(self, *flags: str, **kw) -> Solo:
        """Two units done, and a change on the phase branch committed with the ledger."""
        w = self.started(*flags, **kw)
        self.unit("u1", "done")
        self.ok("unit", "add", "u2", "--title", "Unit u2", "--role", "senior-developer")
        self.ok("unit", "set", "u2", "done")
        self.code("src/a.py", "".join(f"line {n}\n" for n in range(1, 41)))
        return w

    def package(self) -> str:
        return (self.dir() / "review" / "package.diff").read_text(encoding="utf-8")

    def test_it_is_refused_while_a_unit_is_not_done(self):
        self.started()
        self.refused("has no units", "review")
        self.unit("u1", "done")
        self.unit("u2", "running")
        self.unit("u3")
        self.refused("not every unit is done: u2 is running, u3 is pending", "review")
        self.assertEqual(self.ledger()["stage"], "build")
        self.assertFalse((self.dir() / "review").exists())

    def test_the_package_holds_the_commit_list_the_stat_and_the_diff_from_the_base(self):
        w = self.ready()
        w.write("src/a.py", w.read("src/a.py").replace("line 20\n", "line twenty\n"))
        w.commit("the twentieth line", at=w.top, index=False)
        self.ok("review")
        text = self.package()
        self.assertEqual(text.splitlines()[0], f"# Review package: p1 on phase/p1, "
                                               f"{self.head('main')[:12]}..{self.head()[:12]}")
        commits = text.split("## Commits\n\n")[1].split("\n\n## Stat")[0].splitlines()
        self.assertEqual([c.split(" ", 1)[1] for c in commits],
                         ["the twentieth line", "src/a.py"])
        self.assertIn(" src/a.py | 40 +", text.split("## Stat\n\n")[1].split("## Diff")[0])
        self.assertIn("+line twenty\n", text.split("## Diff\n\n")[1])
        self.assertTrue(text.endswith("\n"))

    def test_the_default_branch_merged_into_the_phase_is_not_the_phases_work(self):
        w = self.ready()
        w.switch("main")
        w.write("src/main_only.py", "moved on\n")
        w.commit("main moved on", at=w.top, index=False)
        w.switch("phase/p1")
        w.git("merge", "-q", "--no-edit", "main")
        self.ok("review")
        text = self.package()
        self.assertIn("src/a.py", text)
        self.assertNotIn("main_only", text)
        self.assertNotIn("main moved on", text)
        self.assertIn(f"{self.head('main')[:12]}..{self.head()[:12]}", text.splitlines()[0])

    def test_a_phase_cut_from_another_phase_measures_from_it_when_started_with_from(self):
        w = self.ready()
        self.assertEqual(self.ledger()["from"], "main")
        w.branch("phase/p2")
        self.ok("start", "p2", "--from", "phase/p1")
        self.assertEqual(self.ledger("p2")["from"], "phase/p1")
        self.assertEqual(self.ledger("p2")["base"], self.head("phase/p1"))
        self.ok("unit", "add", "u1", "--title", "T")
        self.ok("unit", "set", "u1", "done")
        self.code("src/b.py", "phase two\n")
        self.ok("review")
        text = (self.dir("p2") / "review" / "package.diff").read_text(encoding="utf-8")
        self.assertIn("src/b.py", text)
        self.assertNotIn("src/a.py", text)

    def test_a_from_that_is_not_a_branch_here_is_refused_at_start(self):
        self.project()
        self.refused("nope is not a branch here", "start", "p1", "--from", "nope")
        self.assertFalse(self.dir().exists())

    def test_the_diff_carries_ten_lines_of_context(self):
        w = self.ready()
        w.git("checkout", "-q", "main")
        w.git("merge", "-q", "--ff-only", "phase/p1")
        w.branch("phase/next")
        self.ok("start", "p2")
        self.ok("unit", "add", "u1", "--title", "T")
        self.ok("unit", "set", "u1", "done")
        w.write("src/a.py", w.read("src/a.py").replace("line 20\n", "line twenty\n"))
        w.commit("the twentieth line", at=w.top, index=False)
        self.ok("review")
        text = (self.dir("p2") / "review" / "package.diff").read_text(encoding="utf-8")
        self.assertIn("@@ -10,21 +10,21 @@", text)

    def test_the_package_never_holds_the_build_folder_itself(self):
        w = self.ready()
        tracked = w.git("ls-files", (BUILD / "p1").as_posix()).split()
        self.assertIn((BUILD / "p1" / build.LEDGER).as_posix(), tracked)
        self.ok("ruling", "a ledger change, committed with the next commit")
        w.commit("the ledger alone", at=w.top, index=False)
        self.ok("review")
        text = self.package()
        self.assertNotIn(BUILD.as_posix(), text)
        self.assertNotIn("ledger.toml", text)
        self.assertNotIn("the ledger alone", text)
        self.assertIn("src/a.py", text)

    def test_the_package_covers_the_repository_when_the_project_sits_below_its_top(self):
        w = self.ready(below="repo")
        (w.top / "outside.txt").write_text("outside the project's folder\n", encoding="utf-8")
        w.commit("outside", at=w.top, index=False)
        self.ok("review")
        text = self.package()
        self.assertIn("+outside the project's folder", text)
        self.assertIn("gov/src/a.py", text)
        self.assertNotIn("ledger.toml", text)

    def test_the_package_is_three_plain_git_calls_each_leaving_the_build_folder_out(self):
        self.ready()
        seen, real = [], phase._run

        def spy(repo, *args, **kw):
            seen.append(args)
            return real(repo, *args, **kw)
        with mock.patch.object(phase, "_run", spy):
            build.package(self.w.root, build.load(self.dir() / build.LEDGER))
        span = f"{self.head('main')}..HEAD"
        made = [a for a in seen if a[0] in ("log", "diff")]
        self.assertEqual([a[:a.index("--")] for a in made], [
            ("log", "--oneline", "--no-decorate", "--no-color", span),
            ("diff", "--stat", "--no-color", "--no-ext-diff", span),
            ("diff", "-U10", "--no-color", "--no-ext-diff", span)])
        for args in made:
            self.assertEqual(args[args.index("--") + 1:],
                             (":(top)", f":(top,exclude,literal){BUILD.as_posix()}"))

    def check_the_stat_file(self, *flags: str):
        self.ready(*flags)
        out = self.ok("review")
        d = self.dir() / "review"
        self.assertIn(f"wrote {BUILD / 'p1' / 'review' / 'package-stat.txt'}",
                      out.splitlines())
        stat = (d / "package-stat.txt").read_text(encoding="utf-8")
        self.assertIn("src/a.py", stat.split("## Commits\n\n")[1].split("\n\n## Stat")[0])
        self.assertIn("1 file changed", stat)
        self.assertNotIn("## Diff", stat)
        self.assertFalse([ln for ln in stat.splitlines() if ln.startswith("diff --git")])
        ledger = build.load(self.dir() / build.LEDGER)
        self.assertEqual((d / "package.diff").read_bytes(), build.package(self.w.root, ledger))
        self.assertEqual(stat.encode("utf-8"), build.package_stat(self.w.root, ledger))

    def test_review_also_writes_the_stat_without_a_diff_and_the_package_is_unchanged(self):
        self.check_the_stat_file()

    def test_a_high_risk_phase_gets_the_stat_file_too(self):
        self.check_the_stat_file("--high-risk")

    def test_the_code_reviewers_brief_names_the_plan_each_unit_and_the_package(self):
        w = self.project()
        w.write("docs/plan.md", "plan\n")
        w.commit("plan", at=w.top, index=False)
        self.ok("start", "p1", "--plan", "docs/plan.md")
        self.unit("u1", "done")
        self.ok("unit", "add", "u2", "--title", "Unit u2", "--role", "senior-developer")
        self.ok("unit", "set", "u2", "done")
        out = self.ok("review")
        d, brief = self.dir(), self.dir() / "review" / "code-reviewer-brief.md"
        text = brief.read_text(encoding="utf-8")
        self.assertIn("Review the phase `p1` as one diff;", text)
        self.assertIn("- Plan: ../../../../docs/plan.md\n", text)
        self.assertIn("- Unit u1, Unit u1: brief ../u1/brief.md, report "
                      "../u1/developer-report.md\n", text)
        self.assertIn("- Unit u2, Unit u2: brief ../u2/brief.md, report "
                      "../u2/senior-developer-report.md\n", text)
        self.assertIn("full diff from the phase's base): package.diff\n", text)
        self.assertNotIn(agent_runs.FILL, text)
        self.assertEqual(text.splitlines()[-2:],
                         [RELATIVE, f"Produces: {at('review', 'code-reviewer-report.md')}"])
        self.assertEqual(out.splitlines()[-1], then("review", "the briefs that need it"))
        self.assertNotIn("Brief:", out)
        self.assertFalse((d / "review" / "qa-engineer-brief.md").exists())
        self.assertEqual(self.ledger()["stage"], "review")

    def test_the_brief_repeats_nothing_the_role_definition_says(self):
        self.ready()
        self.ok("review")
        text = (self.dir() / "review" / "code-reviewer-brief.md").read_text(encoding="utf-8")
        for said_by_the_role in ("Critical", "Findings:", "Status:", "heredoc", "severity"):
            self.assertNotIn(said_by_the_role, text)
        self.assertLessEqual(len(text.splitlines()), 11)

    def test_without_a_plan_the_brief_names_none(self):
        self.ready()
        self.ok("review")
        text = (self.dir() / "review" / "code-reviewer-brief.md").read_text(encoding="utf-8")
        self.assertNotIn("Plan:", text)

    def test_a_high_risk_phase_also_gets_a_qa_brief_with_a_definition_of_done_to_fill(self):
        self.ready("--high-risk")
        out = self.ok("review")
        d = self.dir() / "review"
        text = (d / "qa-engineer-brief.md").read_text(encoding="utf-8")
        self.assertEqual(agent_runs.unfilled(text), ["Definition of done"])
        for rel in ("../u1/brief.md", "../u1/developer-report.md",
                    "../u2/senior-developer-report.md"):
            self.assertIn(rel, text)
        self.assertIn("- What changed (commit list and stat; open the files you probe, there "
                      "is no diff to read): package-stat.txt\n", text)
        self.assertNotIn("package.diff", text)
        self.assertIn("package.diff", (d / "code-reviewer-brief.md").read_text(encoding="utf-8"))
        self.assertIn("Scratch files go only in scratch.\n", text)
        self.assertEqual(text.splitlines()[-2:], [RELATIVE, f"Produces: {at('review', 'qa-engineer-report.md')}"])
        lines = out.splitlines()
        self.assertEqual(lines[-1], then("review", "the briefs that need it"))
        self.assertIn(f"fill in the definition of done in "
                      f"{BUILD / 'p1' / 'review' / 'qa-engineer-brief.md'}", lines)
        self.assertNotIn("Brief:", out)

    def test_a_visual_phase_also_gets_a_ux_brief_with_three_sections_to_fill(self):
        self.ready("--visual", extra=VISUAL_ON)
        out = self.ok("review")
        d = self.dir() / "review"
        self.assertFalse((d / "qa-engineer-brief.md").exists())
        text = (d / "ux-reviewer-brief.md").read_text(encoding="utf-8")
        lines = text.splitlines()
        self.assertEqual(lines[0], "# p1 / review — ux-reviewer")
        self.assertEqual([x for x in lines if x.startswith("## ")],
                         ["## What to launch and how", "## What to look at",
                          "## The design it must match"])
        self.assertEqual(agent_runs.unfilled(text), ["What to launch and how", "What to look at",
                                                     "The design it must match"])
        self.assertIn("../u1/brief.md", text)
        self.assertEqual(lines[-2:], [RELATIVE, f"Produces: {at('review', 'ux-reviewer-report.md')}"])
        self.assertNotIn("Base:", text)
        self.assertIn(f"wrote {BUILD / 'p1' / 'review' / 'ux-reviewer-brief.md'}", out)
        self.assertEqual(self.ok("review").count("kept"), 1)

    def test_without_visual_there_is_no_ux_brief(self):
        self.ready("--high-risk")
        self.ok("review")
        self.assertFalse((self.dir() / "review" / "ux-reviewer-brief.md").exists())

    def test_a_filled_qa_brief_is_kept_on_a_second_review(self):
        self.ready("--high-risk")
        self.ok("review")
        qa = self.dir() / "review" / "qa-engineer-brief.md"
        qa.write_text("the coordinator's checklist\n", encoding="utf-8")
        out = self.ok("review")
        self.assertEqual(qa.read_text(encoding="utf-8"), "the coordinator's checklist\n")
        self.assertIn(f"kept {BUILD / 'p1' / 'review' / 'qa-engineer-brief.md'} as it is", out)

    def test_a_second_review_changes_nothing(self):
        self.ready("--high-risk")
        self.ok("review")
        before = self.files()
        self.ok("review")
        self.assertEqual(self.files(), before)

    def test_a_source_branch_that_is_gone_writes_nothing_and_leaves_the_stage(self):
        self.ready()
        path = self.dir() / build.LEDGER
        path.write_text(path.read_text(encoding="utf-8").replace('from = "main"',
                                                                 'from = "gone"'),
                        encoding="utf-8")
        self.refused("gone is not a branch here", "review")
        self.assertEqual(self.ledger()["stage"], "build")


class FixAndClose(Build):
    def reviewed(self, *flags: str) -> Solo:
        w = self.started(*flags)
        self.unit("u1", "done")
        self.code()
        self.ok("review")
        return w

    def test_every_brief_says_the_report_is_part_of_the_task(self):
        self.reviewed("--high-risk")
        self.ok("fix")
        self.ok("close")
        d = self.dir()
        for brief in (d / "u1" / "brief.md", d / "review" / "code-reviewer-brief.md",
                      d / "review" / "qa-engineer-brief.md", d / "fix" / "brief.md",
                      d / "close" / "brief.md"):
            with self.subTest(brief=brief.name):
                lines = brief.read_text(encoding="utf-8").splitlines()
                self.assertEqual(lines.count(build.REPORT_NOTE), 1)
                self.assertEqual(build.REPORT_NOTE, "Write the report below before your final "
                                 "message, and answer your role's hand-back checklist in it. "
                                 "Every line of it is part of this task.")
                at_ = lines.index(build.REPORT_NOTE)
                self.assertEqual(lines[at_ + 1], build.PATHS_NOTE)
                self.assertTrue(lines[-1].startswith("Produces: "))

    def test_fix_writes_a_list_for_the_coordinator_to_fill_and_sets_the_stage(self):
        self.reviewed()
        out = self.ok("fix")
        brief = self.dir() / "fix" / "brief.md"
        text = brief.read_text(encoding="utf-8")
        self.assertEqual(agent_runs.unfilled(text), ["Fixes", "Out of scope"])
        self.assertIn("ranked, five at most", text)
        self.assertIn("the test to write first and see fail", text)
        self.assertIn(f"Base commit: {self.head()}", text.splitlines())
        self.assertEqual(text.splitlines()[-3:],
                         [RELATIVE, f"Red: {at('fix', 'red')}",
                          f"Produces: {at('fix', 'developer-report.md')}"])
        self.assertEqual(out.splitlines()[-1], then("fix", "its ranked list"))
        self.assertNotIn("Brief:", out)
        self.assertEqual(self.ledger()["stage"], "fix")
        self.assertIn("build close", self.ok("status").splitlines()[-1])

    def test_a_fix_brief_names_its_own_red_folder_and_says_where_findings_are_proved(self):
        self.reviewed()
        self.ok("fix")
        first = (self.dir() / "fix" / "brief.md").read_text(encoding="utf-8")
        self.assertIn("copy each finding in full (file, line, the input that shows it)", first)
        self.assertIn(".context-gate/build/p1/fix/red)", first)
        (self.dir() / "fix" / "brief.md").write_text("1. the ranked list\n", encoding="utf-8")
        self.ok("fix")
        text = (self.dir() / "fix" / "brief-2.md").read_text(encoding="utf-8")
        self.assertIn(".context-gate/build/p1/fix/red-2)", text)
        self.assertNotIn("fix/red)", text)
        note = "A finding made on a running system is proved fixed on a running system, never on a copied page or a fixture."
        self.assertEqual(text.splitlines().count(note), 1)
        lines = text.splitlines()
        self.assertEqual(lines[lines.index(note) + 2], build.REPORT_NOTE)

    def test_a_fix_brief_still_to_fill_is_refused_on_a_second_run(self):
        self.reviewed()
        self.ok("fix")
        before = self.files()
        err = self.refused("is not filled in yet", "fix")
        self.assertIn(str(BUILD / "p1" / "fix" / "brief.md"), err)
        self.assertEqual(self.files(), before)
        self.assertFalse((self.dir() / "fix" / "brief-2.md").exists())

    def test_a_refused_fix_does_not_move_the_stage_or_touch_the_ledger(self):
        self.reviewed()
        self.ok("fix")
        self.ok("close")
        self.assertEqual(self.ledger()["stage"], "closing")
        before = (self.dir() / build.LEDGER).read_bytes()
        self.refused("is not filled in yet", "fix")
        self.assertEqual((self.dir() / build.LEDGER).read_bytes(), before)
        self.assertEqual(self.ledger()["stage"], "closing")
        self.assertEqual(json.loads(self.ok("status", "--json"))["stage"], "closing")

    def test_a_filled_fix_brief_is_kept_and_the_next_pass_gets_a_brief_of_its_own(self):
        self.reviewed()
        self.ok("fix")
        folder = self.dir() / "fix"
        first = folder / "brief.md"
        first.write_text("1. the ranked list\n", encoding="utf-8")
        out = self.ok("fix")
        second = folder / "brief-2.md"
        self.assertEqual(first.read_text(encoding="utf-8"), "1. the ranked list\n")
        text = second.read_text(encoding="utf-8")
        self.assertEqual(agent_runs.unfilled(text), ["Fixes", "Out of scope"])
        self.assertEqual(text.splitlines()[-3:], [RELATIVE, f"Red: {at('fix', 'red-2')}",
                                                  f"Produces: {at('fix', 'developer-report-2.md')}"])
        self.assertIn(f"wrote {BUILD / 'p1' / 'fix' / 'brief-2.md'}", out)
        self.assertEqual(out.splitlines()[-1], then("fix", "its ranked list"))
        self.refused("is not filled in yet", "fix")
        self.assertFalse((folder / "brief-3.md").exists())
        second.write_text("1. the second list\n", encoding="utf-8")
        self.ok("fix")
        third = (folder / "brief-3.md").read_text(encoding="utf-8")
        self.assertEqual(third.splitlines()[-1],
                         f"Produces: {at('fix', 'developer-report-3.md')}")

    def test_fix_can_go_to_the_senior_developer_and_a_role_that_writes_no_code_is_refused(self):
        self.reviewed()
        self.refused("is not a rendered role that writes code", "fix", "--role", "code-reviewer")
        self.assertFalse((self.dir() / "fix").exists())
        out = self.ok("fix", "--role", "senior-developer")
        text = (self.dir() / "fix" / "brief.md").read_text(encoding="utf-8")
        self.assertEqual(text.splitlines()[-1],
                         f"Produces: {at('fix', 'senior-developer-report.md')}")
        self.assertEqual(out.splitlines()[-1], then("fix", "its ranked list"))

    def test_fix_is_refused_before_the_review(self):
        self.started()
        self.unit("u1", "done")
        self.refused("is in stage build", "fix")
        self.assertFalse((self.dir() / "fix").exists())
        self.assertEqual(self.ledger()["stage"], "build")

    def test_fix_after_the_close_writes_a_brief_and_sets_the_stage(self):
        self.reviewed()
        self.ok("close")
        self.ok("fix")
        self.assertTrue((self.dir() / "fix" / "brief.md").is_file())
        self.assertEqual(self.ledger()["stage"], "fix")
        self.assertEqual(json.loads(self.ok("status", "--json"))["stage"], "fix")

    def test_close_is_refused_before_the_review(self):
        self.started()
        self.unit("u1", "done")
        self.refused("is in stage build", "close")
        self.assertFalse((self.dir() / "close").exists())

    def test_close_after_the_review_or_the_fix_writes_the_writers_brief(self):
        for fix_first in (False, True):
            with self.subTest(fix_first=fix_first), self.fresh():
                self.reviewed()
                if fix_first:
                    self.ok("fix")
                self.ok("unit", "set", "u1", "done", "--commit", "3f2a9c1")
                self.ok("ruling", "kept the old flag; callers break if wrong")
                out = self.ok("close").splitlines()
                brief = self.dir() / "close" / "brief.md"
                text = brief.read_text(encoding="utf-8")
                self.assertIn("The phase `p1` on the branch `phase/p1` is closing.", text)
                self.assertIn("- Unit u1, Unit u1 (commit 3f2a9c1)\n", text)
                self.assertIn("- kept the old flag; callers break if wrong\n", text)
                self.assertNotIn("Delete", text)
                self.assertEqual(agent_runs.unfilled(text), ["What landed and what was decided"])
                self.assertEqual(self.ledger()["stage"], "closing")
                self.assertEqual(out[-5:], [
                    then("close"), "once its branch is merged, run:",
                    "git rm -r -q .context-gate/build/p1",
                    'git commit -m "Close phase p1: remove its build folder"',
                    f"{roles.GOVERN} phase close"])
                self.assertIn("  1. kept the old flag; callers break if wrong", out)
                self.assertIn("phase close", self.ok("status").splitlines()[-1])

    def test_the_writer_is_not_told_to_delete_the_folder_and_its_report_sits_in_it(self):
        self.reviewed()
        self.ok("close")
        text = (self.dir() / "close" / "brief.md").read_text(encoding="utf-8")
        self.assertNotIn("elete", text)
        self.assertEqual(text.splitlines()[-2:],
                         [RELATIVE, f"Produces: {at('close', 'technical-writer-report.md')}"])

    def test_a_second_close_brings_the_rulings_up_to_date_and_keeps_the_rest(self):
        self.reviewed()
        self.ok("close")
        brief = self.dir() / "close" / "brief.md"
        self.ok("ruling", "kept the flag")
        with brief.open("a", encoding="utf-8") as f:
            f.write("a hand-written line\n")
        out = self.ok("close")
        text = brief.read_text(encoding="utf-8")
        self.assertIn("## Rulings the coordinator made without the owner\n\n- kept the flag\n\n",
                      text)
        self.assertIn("a hand-written line\n", text)
        self.assertLess(text.index("- kept the flag"), text.index("## What landed and what"))
        self.assertIn(f"kept {BUILD / 'p1' / 'close' / 'brief.md'}, with its rulings "
                      "brought up to date", out)
        self.ok("ruling", "second")
        self.ok("close")
        text = brief.read_text(encoding="utf-8")
        self.assertEqual(text.count("Rulings the coordinator made"), 1)
        self.assertIn("- kept the flag\n- second\n\n", text)

    def test_a_rulings_heading_inside_a_fence_or_a_comment_is_never_the_section(self):
        self.reviewed()
        self.ok("ruling", "first")
        self.ok("close")
        brief = self.dir() / "close" / "brief.md"
        heading = "## Rulings the coordinator made without the owner"
        fenced = f"```\n{heading}\nfenced line\n```\n<!--\n{heading}\nin a comment\n-->\n"
        text = brief.read_text(encoding="utf-8")
        first, rest = text.split("\n", 1)
        brief.write_text(f"{first}\n{fenced}{rest}", encoding="utf-8", newline="")
        self.ok("ruling", "second")
        out = self.ok("close")
        got = brief.read_text(encoding="utf-8")
        self.assertIn(fenced, got)
        self.assertIn(f"{heading}\n\n- first\n- second\n\n", got.replace(fenced, ""))
        self.assertIn("with its rulings brought up to date", out)
        before = self.files()
        self.ok("close")
        self.assertEqual(self.files(), before)

    def test_a_close_brief_keeps_its_crlf_and_its_byte_order_mark_when_rulings_change(self):
        for bom in (False, True):
            with self.subTest(bom=bom), self.fresh():
                self.reviewed()
                self.ok("ruling", "first")
                self.ok("close")
                brief = self.dir() / "close" / "brief.md"
                text = brief.read_text(encoding="utf-8").replace("\n", "\r\n")
                brief.write_bytes((build.BOM if bom else "").encode("utf-8") + text.encode("utf-8"))
                self.ok("ruling", "second")
                self.ok("close")
                raw = brief.read_bytes()
                self.assertEqual(raw.startswith(build.BOM.encode("utf-8")), bom)
                body = raw.decode("utf-8").removeprefix(build.BOM)
                self.assertNotRegex(body, r"(?<!\r)\n")
                self.assertIn("- first\r\n- second\r\n\r\n", body)

    def test_a_close_brief_the_coordinator_filled_is_otherwise_kept(self):
        self.reviewed()
        self.ok("close")
        brief = self.dir() / "close" / "brief.md"
        brief.write_text("the coordinator's account\n", encoding="utf-8")
        before = self.files()
        out = self.ok("close")
        self.assertEqual(self.files(), before)
        self.assertIn(f"kept {BUILD / 'p1' / 'close' / 'brief.md'} as it is", out)
        self.assertIn(then("close"), out.splitlines())
        self.ok("ruling", "late")
        self.ok("close")
        self.assertEqual(brief.read_text(encoding="utf-8"), "the coordinator's account\n")

    def test_a_new_close_brief_says_where_as_built_notes_go(self):
        self.reviewed()
        self.ok("close")
        lines = (self.dir() / "close" / "brief.md").read_text(encoding="utf-8").splitlines()
        note = ("What was built differently from the plan goes in the project's decision log, "
                "never into the plan.")
        self.assertEqual(lines.count(note), 1)
        at_ = lines.index(note)
        self.assertEqual((lines[at_ + 1], lines[at_ + 2]), ("", build.REPORT_NOTE))

    def test_a_second_close_changes_nothing(self):
        self.reviewed()
        self.ok("close")
        before = self.files()
        self.ok("close")
        self.assertEqual(self.files(), before)


class Launch(Build):
    """`build launch`: the one place a brief is checked, a unit marked running and a launch
    prompt printed."""

    def fill(self, brief: Path) -> None:
        text = "".join("Filled.\n" if line.startswith(agent_runs.FILL) else line + "\n"
                       for line in brief.read_text(encoding="utf-8").splitlines())
        brief.write_text(text, encoding="utf-8")

    def commit(self) -> None:
        self.w.commit("briefs", at=self.w.top, index=False)

    def ready(self, uid: str = "u1", *flags: str) -> Path:
        brief = self.unit(uid, None, *flags)
        self.fill(brief)
        self.commit()
        return brief

    def prompt(self, role: str, path: str) -> list[str]:
        return [f"launch {role} with the prompt:", f"Brief: {path}", f"Base: {self.head()}"]

    def reviewed(self, *flags: str) -> None:
        self.started(*flags, extra=VISUAL_ON if "--visual" in flags else "")
        self.unit("u1", "done")
        self.code()
        self.ok("review")

    def test_a_unit_is_marked_running_and_gets_its_role_its_brief_and_the_base(self):
        self.started()
        self.ready()
        out = self.ok("launch", "u1")
        self.assertEqual(out.splitlines(),
                         self.prompt("developer", ".context-gate/build/p1/u1/brief.md"))
        unit = self.ledger()["unit"][0]
        self.assertEqual((unit["status"], unit["attempts"]), ("running", 1))
        self.assertEqual(len(self.head()), 40)

    def test_a_senior_developer_works_in_a_worktree_too(self):
        self.started()
        self.ok("unit", "add", "u1", "--title", "T", "--role", "senior-developer")
        self.fill(self.dir() / "u1" / "brief.md")
        self.commit()
        out = self.ok("launch", "u1")
        self.assertEqual(out.splitlines()[0], "launch senior-developer with the prompt:")
        self.assertEqual(out.splitlines()[-1], f"Base: {self.head()}")

    def test_a_relaunch_is_one_more_attempt_from_the_brief_as_committed(self):
        self.started()
        brief = self.ready()
        before = brief.read_bytes()
        self.ok("launch", "u1")
        self.ok("unit", "set", "u1", "failed", "--note", "red")
        self.code()
        out = self.ok("launch", "u1")
        self.assertEqual(self.ledger()["unit"][0]["attempts"], 2)
        self.assertEqual(out.splitlines()[-1], f"Base: {self.head()}")
        self.assertEqual(brief.read_bytes(), before)

    def test_a_brief_still_to_fill_is_refused_by_name_and_nothing_changes(self):
        self.started()
        self.unit("u1")
        self.commit()
        err = self.refused("the brief is not filled in", "launch", "u1")
        self.assertIn(str(BUILD / "p1" / "u1" / "brief.md"), err)
        self.assertEqual(self.ledger()["unit"][0]["status"], "pending")

    def test_a_brief_not_committed_is_refused_by_name(self):
        self.started()
        self.unit("u1")
        self.fill(self.dir() / "u1" / "brief.md")
        err = self.refused("is not committed at HEAD", "launch", "u1")
        self.assertIn(str(BUILD / "p1" / "u1" / "brief.md"), err)

    def test_a_brief_changed_since_it_was_committed_is_refused(self):
        self.started()
        brief = self.ready()
        brief.write_text(brief.read_text(encoding="utf-8") + "More.\n", encoding="utf-8")
        self.refused("is not committed at HEAD", "launch", "u1")
        self.w.git("add", "-A", at=self.w.top)
        self.refused("is not committed at HEAD", "launch", "u1")

    def test_an_unknown_unit_is_refused(self):
        self.started()
        self.refused("has no unit u9", "launch", "u9")

    def test_the_path_is_from_the_repository_root_in_posix_form(self):
        self.started(below="sub")
        self.ready()
        out = self.ok("launch", "u1")
        rel = (self.w.root / BUILD / "p1" / "u1" / "brief.md").relative_to(self.w.top)
        self.assertEqual(out.splitlines()[1], f"Brief: {rel.as_posix()}")
        self.assertNotIn("\\", out)

    def test_review_prints_a_prompt_per_reviewer_brief_and_no_base(self):
        self.reviewed("--high-risk")
        d = self.dir() / "review"
        self.fill(d / "qa-engineer-brief.md")
        self.commit()
        out = self.ok("launch", "review")
        self.assertEqual(out.splitlines(), [
            "launch code-reviewer with the prompt:",
            "Brief: .context-gate/build/p1/review/code-reviewer-brief.md",
            "launch qa-engineer with the prompt:",
            "Brief: .context-gate/build/p1/review/qa-engineer-brief.md"])

    def test_review_prints_the_ux_reviewer_last_and_no_base(self):
        for flags, roles_ in ((("--visual",), ["code-reviewer", "ux-reviewer"]),
                              (("--visual", "--high-risk"),
                               ["code-reviewer", "qa-engineer", "ux-reviewer"])):
            with self.subTest(flags=flags), self.fresh():
                self.reviewed(*flags)
                d = self.dir() / "review"
                for brief in d.glob("*-brief.md"):
                    self.fill(brief)
                self.commit()
                lines = self.ok("launch", "review").splitlines()
                self.assertEqual(len(lines), 2 * len(roles_))
                self.assertEqual(lines[::2], [f"launch {r} with the prompt:" for r in roles_])
                self.assertEqual(lines[-1], "Brief: .context-gate/build/p1/review/ux-reviewer-brief.md")
                self.assertFalse(any(x.startswith("Base:") for x in lines))

    def test_review_without_high_risk_prints_the_code_reviewers_alone(self):
        self.reviewed()
        self.commit()
        self.assertEqual(len(self.ok("launch", "review").splitlines()), 2)

    def test_review_is_refused_while_the_qa_brief_is_unfilled(self):
        self.reviewed("--high-risk")
        self.commit()
        err = self.refused("the brief is not filled in", "launch", "review")
        self.assertIn("qa-engineer-brief.md", err)

    def two_fix_briefs(self):
        self.reviewed()
        self.ok("fix", "--role", "senior-developer")
        self.fill(self.dir() / "fix" / "brief.md")
        self.ok("fix", "--role", "senior-developer")
        self.fill(self.dir() / "fix" / "brief-2.md")
        # The ledger folder's .gitignore does not keep `brief-2.md` (`!*brief.md`): add it by force.
        self.w.git("add", str(self.dir() / "fix" / "brief-2.md"), at=self.w.top)
        self.commit()

    AGAIN = "every fix brief was launched before; this is the latest again:"

    def test_fix_prints_a_prompt_for_every_brief_not_yet_launched_then_the_latest_again(self):
        self.two_fix_briefs()
        first = self.prompt("senior-developer", ".context-gate/build/p1/fix/brief.md")
        second = self.prompt("senior-developer", ".context-gate/build/p1/fix/brief-2.md")
        self.assertEqual(self.ok("launch", "fix").splitlines(), first + second)
        self.assertEqual(self.ledger()["launched"], ["brief.md", "brief-2.md"])
        # A report on disk, or none, changes nothing: the ledger is the record.
        (self.dir() / "fix" / "senior-developer-report.md").write_text("done\n", encoding="utf-8")
        again = self.ok("launch", "fix").splitlines()
        self.assertEqual(again, [self.AGAIN] + second)
        self.assertEqual(self.ledger()["launched"], ["brief.md", "brief-2.md"])

    def test_a_new_fix_brief_after_a_launch_gives_only_itself(self):
        self.reviewed()
        self.ok("fix", "--role", "senior-developer")
        self.fill(self.dir() / "fix" / "brief.md")
        self.commit()
        self.assertEqual(self.ok("launch", "fix").splitlines(), self.prompt(
            "senior-developer", ".context-gate/build/p1/fix/brief.md"))
        self.ok("fix", "--role", "senior-developer")
        self.fill(self.dir() / "fix" / "brief-2.md")
        self.w.git("add", str(self.dir() / "fix" / "brief-2.md"), at=self.w.top)
        self.commit()
        self.assertEqual(self.ok("launch", "fix").splitlines(), self.prompt(
            "senior-developer", ".context-gate/build/p1/fix/brief-2.md"))
        self.assertEqual(self.ledger()["launched"], ["brief.md", "brief-2.md"])

    def test_a_ledger_without_launched_loads_as_none_launched_and_a_bad_one_is_refused(self):
        self.started()
        path = self.dir() / build.LEDGER
        self.assertEqual(build.load(path).launched, [])
        text = path.read_text(encoding="utf-8")
        path.write_text(text + 'launched = [1]\n', encoding="utf-8")
        self.assertIn("`launched`", self.refused("not a build ledger", "status"))

    def test_a_fix_brief_that_is_not_utf8_is_refused_without_a_traceback(self):
        self.reviewed()
        folder = self.dir() / "fix"
        folder.mkdir()
        (folder / "brief.md").write_bytes(b"\xff\xfe")
        self.refused(f"{BUILD / 'p1' / 'fix' / 'brief.md'} is not UTF-8", "launch", "fix")

    def test_close_prints_the_writers_prompt_with_a_base(self):
        self.reviewed()
        self.ok("close")
        self.fill(self.dir() / "close" / "brief.md")
        self.commit()
        self.assertEqual(self.ok("launch", "close").splitlines(), self.prompt(
            "technical-writer", ".context-gate/build/p1/close/brief.md"))

    def test_a_brief_the_command_has_not_written_yet_is_refused(self):
        self.started()
        self.refused("run `" + GOVERN + " build review` first", "launch", "review")

    def test_with_no_build_it_says_so(self):
        self.project()
        code, _, err = self.go("launch", "u1")
        self.assertEqual(code, 1)
        self.assertIn("error:", err)


class Briefs(Build):
    """What every brief the engine writes must hold, so `agent-gates` can hold its agent to it."""

    def every(self) -> dict[str, Path]:
        self.started("--high-risk")
        self.unit("u1", "done", "--red")
        self.code()
        self.ok("review")
        self.ok("fix")
        self.ok("close")
        d = self.dir()
        return {"unit": d / "u1" / "brief.md", "fix": d / "fix" / "brief.md",
                "code-reviewer": d / "review" / "code-reviewer-brief.md",
                "qa-engineer": d / "review" / "qa-engineer-brief.md",
                "close": d / "close" / "brief.md"}

    def test_each_ends_with_a_produces_line_from_the_repository_root(self):
        for name, brief in self.every().items():
            with self.subTest(brief=name):
                lines = brief.read_text(encoding="utf-8").splitlines()
                contract = [n for n, line in enumerate(lines) if line.startswith(("Red: ",
                                                                                 "Produces: "))]
                self.assertEqual(lines[contract[0] - 1], RELATIVE)
                self.assertEqual(contract[-1], len(lines) - 1)
                last = lines[-1]
                self.assertTrue(last.startswith("Produces: "), last)
                relative = last.removeprefix("Produces: ")
                self.assertFalse(Path(relative).is_absolute(), relative)
                report = self.w.root / relative
                self.assertEqual(report.parent, brief.parent)
                self.assertNotIn(report.name, build.REFUSED_NAMES)
                self.assertTrue(report.name.endswith("-report.md"), report.name)
                got = agent_runs.contract(build.launch_line(brief), str(self.w.root))
                launch = {**got, "cwd": str(self.w.root)}
                self.assertEqual(agent_runs.where(launch, "produces", got["produces"][0])[0],
                                 report)
                self.assertEqual(Path(got["brief"]), brief)

    def test_no_committed_file_holds_this_machines_path_to_the_repository(self):
        self.every()
        self.ok("unit", "add", "u2", "--title", "T")
        forms = {str(self.w.root), self.w.root.as_posix(), str(self.w.root.resolve()),
                 self.w.root.resolve().as_posix()}
        kept = [p for p in self.w.git("ls-files", "--others", "--cached", "--exclude-standard",
                                      "-z", BUILD.as_posix()).split("\0") if p]
        self.assertGreaterEqual(len(kept), 7)
        for rel in kept:
            text = (self.w.top / rel).read_text(encoding="utf-8", errors="replace")
            for form in forms:
                self.assertNotIn(form, text, rel)

    def test_a_brief_moved_to_another_checkout_names_its_files_from_that_checkouts_root(self):
        brief = self.every()["unit"]
        other = self.tmp / "elsewhere"
        moved = other / BUILD / "p1" / brief.parent.name
        moved.mkdir(parents=True)
        (moved / "brief.md").write_bytes(brief.read_bytes())
        got = agent_runs.contract(build.launch_line(moved / "brief.md"), str(other))
        launch = {**got, "cwd": str(other)}
        self.assertEqual(agent_runs.where(launch, "produces", got["produces"][0])[0],
                         moved / "developer-report.md")

    def test_no_report_shares_a_name_the_write_tool_refuses(self):
        self.assertEqual(build.REFUSED_NAMES,
                         ("report.md", "findings.md", "summary.md", "analysis.md"))

    def test_a_unit_brief_is_refused_at_launch_until_every_section_is_filled(self):
        brief = self.every()["unit"]
        home = self.tmp / "gate-home"

        def launch():
            return agent_runs.on_pre_launch(home, "s1", GATES, {"tool_input": {
                "subagent_type": "developer", "prompt": build.launch_line(brief)}})
        reason = launch()["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("the brief is not filled in: Context, Task, Files, Tests, Follow, Out of scope",
                      reason)
        text = brief.read_text(encoding="utf-8")
        for _, what in build.UNIT_SECTIONS[:-1]:
            text = text.replace(build._fill(what), "Filled.")
        brief.write_text(text, encoding="utf-8")
        self.assertIn("the brief is not filled in: Out of scope.",
                      launch()["hookSpecificOutput"]["permissionDecisionReason"])
        brief.write_text(text.replace(build._fill(build.UNIT_SECTIONS[-1][1]), "Nothing."),
                         encoding="utf-8")
        self.assertIsNone(launch())


class BriefsSayWhatIsTrue(Build):
    def reviewed(self) -> None:
        self.started()
        self.unit("u1", "done")
        self.code()
        self.ok("review")

    def test_fix_brief_has_its_red_line(self):
        self.reviewed()
        self.ok("fix")
        lines = (self.dir() / "fix" / "brief.md").read_text(encoding="utf-8").splitlines()
        red = f"Red: {at('fix', 'red')}"
        self.assertEqual(red, "Red: .context-gate/build/p1/fix/red")
        self.assertEqual(lines[lines.index(red) + 1][:10], "Produces: ")

    def test_second_fix_brief_has_its_own_red_folder(self):
        self.reviewed()
        self.ok("fix")
        (self.dir() / "fix" / "brief.md").write_text("1. the ranked list\n", encoding="utf-8")
        self.ok("fix")
        lines = (self.dir() / "fix" / "brief-2.md").read_text(encoding="utf-8").splitlines()
        self.assertIn("Red: .context-gate/build/p1/fix/red-2", lines)
        self.assertNotIn("Red: .context-gate/build/p1/fix/red", lines)

    def test_brief_reader_finds_the_fix_red_folder(self):
        self.reviewed()
        self.ok("fix")
        brief = self.dir() / "fix" / "brief.md"
        got = agent_runs.contract(f"Brief: {brief}")
        self.assertEqual(len(got["red"]), 1)
        self.assertTrue(Path(got["red"][0]).as_posix().endswith(at("fix", "red")))
        self.assertEqual(len(got["in_brief"]["red"]), 1)

    def test_close_brief_names_no_report(self):
        self.reviewed()
        self.ok("unit", "set", "u1", "done", "--commit", "3f2a9c1")
        self.ok("close")
        text = (self.dir() / "close" / "brief.md").read_text(encoding="utf-8")
        landed = text.split("## What landed\n\n")[1].split("\n\n")[0]
        self.assertEqual(landed, "- Unit u1, Unit u1 (commit 3f2a9c1)")
        self.assertNotIn("u1/developer-report.md", text)


if __name__ == "__main__":
    unittest.main()
