"""`govern diff` flags and console output, the reference tree's expected output, and `govern ci github`."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from changes_support import *  # noqa: E402,F401,F403

# ---------------------------------------------------------------------------- compared

class Compared(Diff):
    """Each repository and the commit it was compared with, or why it was not. The report
    never fails: what could be compared is still reported."""

    def test_a_nested_repository_is_compared_with_its_own_head(self):
        w = self.team(nested=True)
        w.append("beta/DECISIONS.md", decision("B", 202))
        self.assertEqual(self.report(w), {
            "Decisions": ["- added      beta/DECISIONS.md B-202 - Title 202 (locked)"],
            "Compared": [f"- . at {self.head(w)}", f"- beta at {self.head(w, w.root / 'beta')}"]})

    def test_a_nested_repository_that_could_not_be_compared_is_named_and_the_rest_reported(self):
        w = self.team(nested=True)       # beta's branch is `trunk`: it has no `main`
        w.append("governance/DECISIONS.md", decision("W", 3))
        w.append("beta/DECISIONS.md", decision("B", 202))
        w.append("beta/working-files/traps.md", trap(2))
        self.assertEqual(self.report(w, "--base", "main"), {
            "Decisions": ["- added      governance/DECISIONS.md W-3 - Title 3 (locked)"],
            "Compared": [f"- . at {self.head(w)}",
                         "- beta: not compared (main names no commit in this repository)"]})
        self.assertEqual(self.data(w, "--base", "main")["compared"], [
            {"repo": ".", "commit": self.full(w), "reason": None},
            {"repo": "beta", "commit": None,
             "reason": "main names no commit in this repository"}])

    def test_no_git_at_all(self):
        w = self.solo()
        w.append(LOG, decision("P", 4))
        empty = self.tmp / "nothing-on-path"
        empty.mkdir()
        for flags in ((), ("--base", "main")):
            self.assertEqual(self.report(w, *flags, env={"PATH": str(empty)}), {
                "": [NOT_COMPARED], "Compared": ["- .: not compared (git is not available)"]})

    def test_not_a_repository_and_no_commit_yet(self):
        for git, why in ((None, "not a git repository"),
                         ("init", "the repository has no commit yet")):
            with self.subTest(git=git), self.fresh():
                w = self.solo(git=git)
                w.append(LOG, decision("P", 4))
                w.write(layout.BASELINE, '{"handoff_words:solo": 1720}\n')
                self.assertEqual(self.report(w), {
                    "": [NOT_COMPARED], "Compared": [f"- .: not compared ({why})"]})

    def test_an_unknown_ref_and_one_that_reads_as_an_option(self):
        w = self.solo()
        for ref in ("origin/nope", "--all"):
            out = self.text(w, f"--base={ref}")
            self.assertEqual(out, f"Governance changes since {ref}\n\n{NOT_COMPARED}\n\nCompared\n"
                                  f"- .: not compared ({ref} names no commit in this repository)\n")

    def test_a_shallow_clone_says_how_to_fix_it(self):
        src = self.solo()
        src.branch("work")
        src.append(LOG, decision("P", 4))
        src.commit("ours")
        src.switch("main")
        src.append(LOG, decision("P", 5))
        src.commit("theirs")
        clone = self.tmp / "clone"
        src.git("clone", "-q", "--depth", "1", "--branch", "work", src.root.as_uri(), str(clone),
                at=self.tmp)
        w = Tree(clone, src.home)
        w.git("fetch", "-q", "--depth", "1", "origin", "main:refs/remotes/origin/main")
        self.assertEqual(self.report(w, "--base", "origin/main")["Compared"], [
            "- .: not compared (HEAD and origin/main share no commit in this shallow clone: "
            "fetch the full history (fetch-depth: 0))"])

    def test_a_log_on_disk_that_is_not_utf8_is_named_not_a_traceback(self):
        w = self.solo()
        w.write(LOG, b"# Decisions\n\n## P-1 - Title 1\n\n\xff\xfe\n")
        found = self.report(w)
        self.assertEqual((list(found), found[""]), (["", "Compared"], [NOT_COMPARED]))
        [line] = found["Compared"]
        self.assertTrue(line.startswith(f"- .: not compared ({LOG} is "), line)

    def test_an_id_out_of_sight_in_an_unreadable_log_is_not_called_removed(self):
        w = self.team(nested=True)
        moved = cut(w, "beta/DECISIONS.md", "B-200")
        w.write("governance/DECISIONS.md",
                (w.read("governance/DECISIONS.md") + moved).encode("utf-8") + b"\xff\xfe\n")
        found = self.report(w)
        # One repository of the two was compared, and nothing changed in it.
        self.assertEqual((list(found), found[""]), (["", "Compared"], [NOTHING]))
        self.assertTrue(found["Compared"][0].startswith(
            "- .: not compared (governance/DECISIONS.md is "), found)
        self.assertEqual(found["Compared"][1], f"- beta at {self.head(w, w.root / 'beta')}")

    def test_a_log_at_the_base_that_is_not_utf8(self):
        w = self.solo(git="init")
        good = w.read(LOG)
        w.write(LOG, b"# Decisions\n\xff\xfe\n")
        w.commit("start", index=False)
        w.write(LOG, good)
        self.assertEqual(self.report(w), {"": [NOT_COMPARED], "Compared": [
            f"- .: not compared ({LOG} at {self.head(w)} is not valid UTF-8 (byte 12))"]})
        # In JSON nothing says so but the list itself: no commit, and the reason.
        self.assertEqual(self.data(w), {
            "base": "HEAD", "baseline": [], "decisions": [], "traps": [],
            "compared": [{"repo": ".", "commit": None, "reason":
                          f"{LOG} at {self.head(w)} is not valid UTF-8 (byte 12)"}]})

    def test_a_governance_root_below_the_git_top_is_still_dot(self):
        w = self.solo(below="outer")
        w.append(LOG, decision("P", 4))
        self.assertEqual(self.report(w), {
            "Decisions": [f"- added      {LOG} P-4 - Title 4 (locked)"],
            "Compared": [f"- . at {self.head(w)}"]})


class WhichCommit(Diff):
    def test_without_base_only_uncommitted_work_is_a_change(self):
        w = self.solo()
        w.branch("work")
        w.append(LOG, decision("P", 4))
        w.commit("ours")
        w.append(LOG, decision("P", 5))
        self.assertEqual(self.report(w), {
            "Decisions": [f"- added      {LOG} P-5 - Title 5 (locked)"],
            "Compared": [f"- . at {self.head(w)}"]})

    def test_with_base_it_is_everything_since_the_branch_began_and_only_the_branchs(self):
        w = self.solo()
        start = self.head(w)
        w.branch("work")
        w.append(LOG, decision("P", 4))
        w.commit("ours")
        w.switch("main")
        w.append(LOG, decision("P", 7, title="Theirs"))
        cut(w, LOG, "P-2")
        w.commit("theirs")
        w.switch("work")
        w.append(LOG, decision("P", 5))
        self.assertNotEqual(start, self.head(w, ref="main"))
        self.assertEqual(sections(self.text(w, "--base", "main")), {
            "title": ["Governance changes since main"],
            "Decisions": [f"- added      {LOG} P-4 - Title 4 (locked)",
                          f"- added      {LOG} P-5 - Title 5 (locked)"],
            "Compared": [f"- . at {start}"]})


# ---------------------------------------------------------------------------- flags and output

class Flags(Diff):
    def _busy(self) -> Tree:
        """A workspace where the workspace and two projects each changed a decision, a trap
        and a baseline number."""
        w = self.team()
        start = {"decision_words:workspace:W-1": 900, "handoff_words:alpha": 1600,
                 "handoff_words:gamma": 1700,
                 "working_file_words:projects/alpha/working-files/plan.md": 6100,
                 "working_file_words:projects/gamma/working-files/plan.md": 6200}
        w.write(layout.BASELINE, json.dumps(start, indent=2) + "\n")
        w.commit("a baseline")
        w.write(layout.BASELINE, json.dumps({k: v + 5 for k, v in start.items()}, indent=2) + "\n")
        w.append("governance/DECISIONS.md", decision("W", 3))
        for name, prefix, lo in (("alpha", "A", 100), ("gamma", "G", 300)):
            w.append(f"projects/{name}/DECISIONS.md", decision(prefix, lo + 2))
            w.append(f"projects/{name}/working-files/traps.md", trap(2))
        return w

    def test_project_limits_the_report_to_that_project(self):
        w = self._busy()
        self.assertEqual(self.report(w, "--project", "alpha"), {
            "Decisions": ["- added      projects/alpha/DECISIONS.md A-102 - Title 102 (locked)"],
            "Traps": ["- added      projects/alpha/working-files/traps.md T-2 - Trap 2"],
            "Baseline": [
                "- raised     handoff_words:alpha 1600 -> 1605",
                "- raised     working_file_words:projects/alpha/working-files/plan.md 6100 -> 6105"],
            "Compared": [f"- . at {self.head(w)}"]})
        whole = self.report(w)
        self.assertEqual((len(whole["Decisions"]), len(whole["Traps"]), len(whole["Baseline"])),
                         (3, 2, 5))

    def test_project_names_only_the_repositories_that_project_was_read_from(self):
        w = self.team(nested=True)
        w.append("beta/DECISIONS.md", decision("B", 202))
        w.append("projects/alpha/DECISIONS.md", decision("A", 102))
        # alpha lives in the root's repository; beta in its own, and its baseline is the root's.
        self.assertEqual(self.report(w, "--project", "alpha", "--base", "main"), {
            "Decisions": ["- added      projects/alpha/DECISIONS.md A-102 - Title 102 (locked)"],
            "Compared": [f"- . at {self.head(w)}"]})
        self.assertEqual(self.report(w, "--project", "beta"), {
            "Decisions": ["- added      beta/DECISIONS.md B-202 - Title 202 (locked)"],
            "Compared": [f"- . at {self.head(w)}", f"- beta at {self.head(w, w.root / 'beta')}"]})

    def test_a_decision_moved_between_projects_is_a_line_of_both(self):
        # Of the one it came to, and of the one it left: a project's report that said nothing
        # of a decision it no longer holds would hide the one change that matters to it.
        w = self.team()
        w.append("projects/gamma/DECISIONS.md", cut(w, "projects/alpha/DECISIONS.md", "A-100"))
        line = "- moved      projects/gamma/DECISIONS.md A-100 (locked), from projects/alpha/DECISIONS.md"
        self.assertEqual(self.changed(w, "--project", "gamma"), {"Decisions": [line]})
        self.assertEqual(self.changed(w, "--project", "alpha"), {"Decisions": [line]})
        self.assertEqual(self.data(w, "--project", "alpha")["decisions"],
                         self.data(w, "--project", "gamma")["decisions"])

    def test_a_decision_that_left_a_project_reworded_or_as_a_pointer_is_still_its_line(self):
        w, alpha, gamma = self.team(), "projects/alpha/DECISIONS.md", "projects/gamma/DECISIONS.md"
        w.append(gamma, cut(w, alpha, "A-101").replace("Title 101", "Renamed"))
        cut(w, alpha, "A-100")
        w.append(gamma, "\n## A-100 - Replaced by G-300\n")
        self.assertEqual(self.changed(w, "--project", "alpha"), {"Decisions": [
            f"- superseded {gamma} A-100 -> G-300, from {alpha}",
            f"- changed    {gamma} A-101 (provisional), from {alpha}"]})

    def test_usage_errors_exit_two(self):
        w = self.solo()
        self.assertEqual(w.run("diff", "--project", "nope"),
                         (2, "", "error: no project 'nope' in config.toml\n"))
        self.assertEqual(w.run("diff", "--base", ""),
                         (2, "", "error: --base needs a ref, e.g. --base origin/main\n"))
        code, out, err = w.run("diff", "--frobnicate")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("unrecognized arguments: --frobnicate", err)

    def test_a_config_that_does_not_load_exits_two(self):
        w = self.solo()
        w.write(layout.CONFIG, w.read(layout.CONFIG) + "\n[no-such-table]\nx = 1\n")
        code, out, err = w.run("diff")
        self.assertEqual((code, out), (2, ""))
        self.assertTrue(err.startswith("error: "), err)

    def test_json_is_one_object_with_every_key_and_null_for_what_is_absent(self):
        w = self.team()
        w.write(layout.BASELINE, '{"handoff_words:alpha": 1600}\n')
        w.commit("a baseline")
        w.write(layout.BASELINE, '{"handoff_words:alpha": 1500, "handoff_words:gamma": 1700}\n')
        w.append("governance/DECISIONS.md",
                 decision("W", 3, "provisional") + cut(w, "projects/alpha/DECISIONS.md", "A-101"))
        w.edit("governance/DECISIONS.md", "Title 1\n\n**Status:** locked\n",
               "Title 1\n\n**Status:** locked\n\n**Revised:** 2026-10-06 (shorter)\n")
        w.edit("projects/gamma/DECISIONS.md", "Title 301\n\n**Status:** provisional",
               "Title 301\n\n**Status:** locked")
        cut(w, "projects/gamma/DECISIONS.md", "G-300")
        cut(w, "projects/alpha/DECISIONS.md", "A-100")
        w.append("projects/alpha/DECISIONS.md", "\n## A-100 - Replaced by W-3\n")
        w.append("projects/alpha/working-files/traps.md", trap(2))
        cut(w, "projects/gamma/working-files/traps.md", "T-1")
        code, out, err = w.run("diff", "--json", "--base", "main")
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.endswith("}\n") and not out.endswith("\n\n"))
        self.assertTrue(out.isascii())
        self.assertEqual(out, json.dumps(json.loads(out), indent=2, sort_keys=True) + "\n")
        gov, alpha, gamma = ("governance/DECISIONS.md", "projects/alpha/DECISIONS.md",
                             "projects/gamma/DECISIONS.md")
        self.assertEqual(json.loads(out), {
            "base": "main",
            "compared": [{"repo": ".", "commit": self.full(w), "reason": None}],
            "decisions": [
                row("moved", gov, "A-101", "Title 101", "provisional", was_log=alpha),
                row("revised", gov, "W-1", "Title 1", "locked", revised="2026-10-06"),
                row("added", gov, "W-3", "Title 3", "provisional"),
                # What was superseded is the decision as it stood: its title and status then.
                row("superseded", alpha, "A-100", "Title 100", "locked", replaced_by="W-3"),
                row("removed", gamma, "G-300", "Title 300", "locked"),
                row("status", gamma, "G-301", "Title 301", "locked", was_status="provisional")],
            "traps": [
                {"change": "added", "log": "projects/alpha/working-files/traps.md", "id": "T-2",
                 "title": "Trap 2"},
                {"change": "removed", "log": "projects/gamma/working-files/traps.md",
                 "id": "T-1", "title": "Trap 1"}],
            "baseline": [
                {"change": "lowered", "key": "handoff_words:alpha", "from": 1600, "to": 1500,
                 "reason": None},
                {"change": "added", "key": "handoff_words:gamma", "from": None, "to": 1700,
                 "reason": None}]})

    def test_a_decision_row_has_the_same_keys_whatever_its_word_and_no_from_or_to(self):
        w = self._busy()
        cut(w, "projects/gamma/DECISIONS.md", "G-300")
        w.append("governance/DECISIONS.md", cut(w, "projects/alpha/DECISIONS.md", "A-100"))
        data = self.data(w)
        self.assertEqual({row["change"] for row in data["decisions"]}, {"added", "removed", "moved"})
        for found in data["decisions"]:
            self.assertEqual(sorted(found), ["change", "id", "log", "replaced_by", "revised",
                                             "status", "title", "was_log", "was_status"])
        for found in data["traps"]:
            self.assertEqual(sorted(found), ["change", "id", "log", "title"])
        for found in data["baseline"]:
            self.assertEqual(sorted(found), ["change", "from", "key", "reason", "to"])

    def test_json_with_no_base_says_head_and_with_nothing_changed_has_empty_lists(self):
        w = self.solo()
        self.assertEqual(self.data(w), {
            "base": "HEAD", "baseline": [], "decisions": [], "traps": [],
            "compared": [{"repo": ".", "commit": self.full(w), "reason": None}]})

    def test_json_names_a_commit_by_its_whole_id_and_the_text_by_twelve_characters(self):
        w = self.team(nested=True)
        commits = [row["commit"] for row in self.data(w)["compared"]]
        self.assertEqual(commits, [self.full(w), self.full(w, w.root / "beta")])
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{40}([0-9a-f]{24})?", c) for c in commits))
        self.assertEqual(self.report(w)["Compared"],
                         [f"- . at {commits[0][:12]}", f"- beta at {commits[1][:12]}"])

    def test_the_json_and_the_text_describe_the_same_changes(self):
        w = self._busy()
        cut(w, "projects/gamma/DECISIONS.md", "G-300")
        w.edit("projects/alpha/DECISIONS.md", "## A-101 - Title 101", "## A-101 - Renamed")
        w.append("projects/gamma/DECISIONS.md", cut(w, "governance/DECISIONS.md", "W-2"))
        for flags in ((), ("--base", "main"), ("--project", "gamma")):
            with self.subTest(flags=flags):
                text, data = self.report(w, *flags), self.data(w, *flags)
                for section in ("Decisions", "Traps", "Baseline", "Compared"):
                    lines, rows = text.get(section, []), data[section.lower()]
                    self.assertEqual(len(lines), len(rows), section)
                    if section == "Baseline":
                        self.assertEqual([line.split()[1:3] for line in lines],
                                         [[found["change"], found["key"]] for found in rows])
                    elif section != "Compared":
                        # Every decision and trap line begins `- <word> <log> <id>`.
                        self.assertEqual([line.split()[1:4] for line in lines],
                                         [[found["change"], found["log"], found["id"]]
                                          for found in rows])
                self.assertGreater(len(data["decisions"]), 0)


class Console(Diff):
    """What a Windows console or a pipe with a legacy encoding is sent never raises: the
    report's own words are ASCII, and a project's words the console cannot encode are escaped."""

    def test_a_title_the_console_cannot_encode_is_escaped_not_a_traceback(self):
        w = self.solo()
        w.append(LOG, decision("P", 4, sep=" — ", title="Café → 测试"))
        w.append(TRAPS, trap(2, title="Naïve — 测试"))
        env = {**os.environ, "HOME": str(w.home), "PYTHONPATH": str(ENGINE),
               "CONTEXT_GATE_NO_UPDATE_CHECK": "1", "GIT_CONFIG_NOSYSTEM": "1"}
        for codec in ("cp1252", "ascii", "utf-8"):
            with self.subTest(codec=codec):
                run = lambda *flags: subprocess.run(
                    [sys.executable, "-m", "govern", "diff", *flags], cwd=w.root,
                    env={**env, "PYTHONIOENCODING": codec}, capture_output=True)
                res = run()
                self.assertEqual((res.returncode, res.stderr), (0, b""))
                out = res.stdout.decode(codec).replace("\r\n", "\n")
                want = "Café → 测试".encode(codec, "backslashreplace").decode(codec)
                self.assertIn(f"- added      {LOG} P-4 - {want} (locked)\n", out)
                res = run("--json")
                self.assertEqual((res.returncode, res.stderr), (0, b""))
                data = json.loads(res.stdout.decode("ascii"))
                self.assertEqual(data["decisions"][0]["title"], "Café → 测试")
                self.assertEqual(data["traps"][0]["title"], "Naïve — 测试")

    def test_control_characters_in_a_title_are_escaped_in_the_text_and_kept_in_the_json(self):
        # A title is the project's own text, and a pull request's author writes it: an escape
        # sequence in one must not reach the terminal or the job summary the report is put in.
        w = self.solo()
        title = "Red \x1b[31malert\x7f, a\ttab and a bell\x07 too"
        w.append(LOG, decision("P", 4, title=title))
        w.append(TRAPS, trap(2, title=title))
        out = self.text(w)
        shown = "Red \\x1b[31malert\\x7f, a\\x09tab and a bell\\x07 too"
        self.assertIn(f"- added      {LOG} P-4 - {shown} (locked)\n", out)
        self.assertIn(f"- added      {TRAPS} T-2 - {shown}\n", out)
        self.assertEqual([c for c in out if c != "\n" and (ord(c) < 0x20 or ord(c) == 0x7f)], [])
        data = self.data(w)
        self.assertEqual((data["decisions"][0]["title"], data["traps"][0]["title"]), (title, title))

    def test_the_layout_is_ascii_whatever_the_project_wrote(self):
        w = self.solo()
        w.append(LOG, decision("P", 4, sep=" — ", title="An em dash heading"))
        cut(w, LOG, "P-1")
        w.append(LOG, "\n## P-1 — Replaced by P-4\n")
        out = self.text(w)       # asserts it is ASCII
        self.assertNotIn("—", out)
        self.assertIn(f"- superseded {LOG} P-1 -> P-4\n", out)


class ClosedPipe(Diff):
    """A reader that stops reading (`govern diff | head -1`) ends the command quietly: exit 0
    and nothing on stderr, never a traceback."""

    def setUp(self) -> None:
        if os.name == "nt":
            self.skipTest("Windows reports a closed pipe to the writer another way (an invalid "
                          "argument, not a broken pipe), which these tests do not produce")
        super().setUp()

    def _env(self, w: Tree) -> dict:
        return {**os.environ, "HOME": str(w.home), "PYTHONPATH": str(ENGINE),
                "CONTEXT_GATE_NO_UPDATE_CHECK": "1", "GIT_CONFIG_NOSYSTEM": "1"}

    def test_diff_whose_reader_closes_after_one_line(self):
        w = self.solo()
        # A report longer than any pipe's buffer, so the writer is still writing when the
        # reader goes.
        w.append(LOG, "".join(decision("P", n, title="A title long enough to fill a pipe " * 4)
                              for n in range(4, 1504)))
        self.assertGreater(len(self.text(w)), 200_000)
        for flags, first in (((), b"Governance changes since HEAD\n"), (("--json",), b"{\n")):
            with self.subTest(flags=flags):
                proc = subprocess.Popen([sys.executable, "-m", "govern", "diff", *flags],
                                        cwd=w.root, env=self._env(w), stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE)
                line = proc.stdout.readline()
                proc.stdout.close()
                err = proc.stderr.read()
                proc.stderr.close()
                self.assertEqual((proc.wait(), err, line), (0, b"", first))

    def test_ci_github_into_a_pipe_nobody_reads(self):
        w = self.solo()
        read_end, write_end = os.pipe()
        os.close(read_end)
        try:
            res = subprocess.run([sys.executable, "-m", "govern", "ci", "github"], cwd=w.root,
                                 env=self._env(w), stdout=write_end, stderr=subprocess.PIPE)
        finally:
            os.close(write_end)
        self.assertEqual((res.returncode, res.stderr), (0, b""))


class ExpectedOutput(Diff):
    """The text layout and the JSON keys, byte for byte (the files are stored with LF endings:
    `expected/.gitattributes`)."""

    def _compare(self, name: str, *flags: str) -> None:
        w = reference_tree(self.tmp)
        self.assertEqual((self.head(w, ref="main~1"), self.head(w, w.root / "projects/beta")),
                         ("9b5d6021d99d", "e761f8be87ae"),
                         "the fixture's commits have new ids: the fixture changed")
        code, out, err = w.run("diff", "--base", "main", *flags)
        self.assertEqual((code, err), (0, ""))
        got, path = out.encode("utf-8"), EXPECTED / name
        if os.environ.get("GOVERN_UPDATE_EXPECTED"):
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(got)
        self.assertEqual(got.decode("utf-8"), path.read_bytes().decode("utf-8"))
        self.assertEqual(got, path.read_bytes())

    def test_text(self):
        self._compare("diff.txt")

    def test_json(self):
        self._compare("diff.json", "--json")

    def test_the_fixture_uses_every_word_the_report_has(self):
        text = (EXPECTED / "diff.txt").read_text(encoding="utf-8")
        for word in ("removed", "added", "superseded", "moved", "revised", "status", "changed",
                     "raised", "lowered"):
            self.assertIn(f"- {word:<10} ", text)
        self.assertIn(": not compared (", text)
        # The one word it cannot hold beside the others, since an unreadable baseline is the
        # whole of its section: `Baseline.test_unreadable_in_both_forms` pins that line.
        self.assertNotIn("- unreadable ", text)
        # An entry in another log than at the base says so, whatever its word.
        self.assertEqual(len(re.findall(r"^- (?:moved|changed) .*, from \S+$", text, re.M)), 2)

    def test_the_two_files_hold_the_same_rows(self):
        text = sections((EXPECTED / "diff.txt").read_text(encoding="utf-8"))
        data = json.loads((EXPECTED / "diff.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(data), ["base", "baseline", "compared", "decisions", "traps"])
        for name in ("Decisions", "Traps"):
            self.assertEqual([line.split()[1:4] for line in text[name]],
                             [[found["change"], found["log"], found["id"]]
                              for found in data[name.lower()]])
        self.assertEqual([line.split()[1:3] for line in text["Baseline"]],
                         [[found["change"], found["key"]] for found in data["baseline"]])


class CiGithub(Case):
    def test_it_prints_the_workflow(self):
        w = self.solo()
        code, out, err = w.run("ci", "github")
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.isascii() and out.endswith("\n") and "\r" not in out and "\t" not in out)
        self.assertTrue(out.endswith(WORKFLOW_STEPS), out)
        lines = out.splitlines()
        live = [line for line in lines if line.strip() and not line.lstrip().startswith("#")]
        self.assertEqual(live[:8], [
            "name: context-gate", "on:", "  pull_request:", "permissions:", "  contents: read",
            "jobs:", "  gate:", "    runs-on: ubuntu-latest"])
        self.assertEqual(live[8], "    steps:")
        # YAML-shaped: spaces only, an even indent, every line a comment, a key or a list item.
        for line in lines:
            body = line.lstrip(" ")
            self.assertEqual((len(line) - len(body)) % 2, 0, line)
            self.assertTrue(not body or body.startswith(("#", "- ")) or ":" in body, line)
        # Every action is pinned by its major version tag.
        uses = [line.split("uses:")[1].strip() for line in lines if "uses:" in line]
        self.assertEqual(uses, ["actions/checkout@v4", "actions/setup-python@v5"])
        self.assertEqual(out.count("if: always()"), 1)

    def test_it_is_the_template_file_and_the_file_is_plain(self):
        w = self.solo()
        _, out, _ = w.run("ci", "github")
        raw = TEMPLATE.read_bytes()
        self.assertEqual(out, raw.decode("ascii").replace("\r\n", "\n"))
        for private in ("self-hosted", "secrets."):
            self.assertNotIn(private, out)

    def test_it_writes_nothing(self):
        w = self.solo()
        before = tree_state(w.root, w.home)
        w.run("ci", "github")
        self.assertEqual(tree_state(w.root, w.home), before)
        self.assertFalse((w.root / ".github").exists())

    def test_it_needs_no_project(self):
        bare = Tree(self.tmp / "bare", self.tmp / "home")
        bare.root.mkdir()
        code, out, err = bare.run("ci", "github")
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.endswith(WORKFLOW_STEPS))
        self.assertEqual(list(bare.root.iterdir()), [])

    def test_an_unknown_provider_is_a_usage_error_naming_github(self):
        w = self.solo()
        for argv in (("ci", "gitlab"), ("ci",), ("ci", "GitHub")):
            with self.subTest(argv=argv):
                code, out, err = w.run(*argv)
                self.assertEqual((code, out), (2, ""))
                self.assertTrue(err.startswith("error: "), err)
                self.assertIn("github", err)
        self.assertIn("'gitlab'", w.run("ci", "gitlab")[2])

    def test_the_commands_in_it_are_ones_this_engine_has(self):
        w = self.solo()
        _, out, _ = w.run("ci", "github")
        runs = [line.split("run:", 1)[1].strip() for line in out.splitlines() if "run:" in line]
        self.assertEqual(len(runs), 2)
        for run in runs:
            self.assertTrue(run.startswith(f"python3 {layout.ENTRYPOINT} "), run)
            argv = run.removeprefix(f"python3 {layout.ENTRYPOINT} ").split(" >> ")[0]
            self.assertEqual(argv.count('"origin/$BASE_REF"'), 1, run)
            argv = argv.replace('"origin/$BASE_REF"', "main").split()
            code, _, err = w.run(*argv)
            self.assertEqual((code, err), (0, ""), run)

    def test_nothing_github_fills_in_is_pasted_into_a_script(self):
        # `${{ ... }}` is replaced in the script's text before a shell reads it, so a branch
        # named `x;id` would run `id`. A value reaches the script as an environment variable,
        # quoted where it is used.
        w = self.solo()
        _, out, _ = w.run("ci", "github")
        runs = [line for line in out.splitlines() if line.lstrip().startswith("run:")]
        self.assertEqual(len(runs), 2)
        for run in runs:
            self.assertNotIn("${{", run)
            self.assertIn(' --base "origin/$BASE_REF"', run)
        self.assertEqual(out.count("          BASE_REF: ${{ github.base_ref }}\n"), 2)

    def test_it_checks_out_the_pull_requests_own_ref_with_the_whole_history(self):
        # A fork's head commit is in no branch of the base repository, so asking for it by its
        # id can fail to fetch; `refs/pull/<n>/head` is always there.
        w = self.solo()
        _, out, _ = w.run("ci", "github")
        self.assertIn("          ref: refs/pull/${{ github.event.pull_request.number }}/head\n", out)
        self.assertNotIn("head.sha", out)
        self.assertEqual(out.count("          fetch-depth: 0\n"), 1)
        self.assertIn("\npermissions:\n  contents: read\n", out)

    def test_the_comment_step_in_the_docs_pastes_nothing_into_its_script_either(self):
        with open(CONFIGURATION, encoding="utf-8", newline="") as fh:
            text = fh.read().replace("\r\n", "\n")
        [step] = [block for block in re.findall(r"^```yaml\n(.*?)^```", text, re.M | re.S)
                  if "gh pr comment" in block]
        settings, script = step.split("        run: |\n")
        self.assertNotIn("${{", script)
        self.assertIn('diff --base "origin/$BASE_REF" > ', script)
        self.assertIn('gh pr comment "$PR_URL" ', script)
        self.assertIn("          BASE_REF: ${{ github.base_ref }}\n", settings)
        self.assertIn("          PR_URL: ${{ github.event.pull_request.html_url }}\n", settings)


if __name__ == "__main__":
    unittest.main()
