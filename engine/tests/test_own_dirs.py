"""The tool's own directory and `.claude/` are never docs: a repo with no `docs/` falls back to
`[projects] docs = ["**/*.md"]`, and that glob must not sweep in the README and reports install
and adopt write under `.context-gate/`, nor agents and skills under `.claude/` (which have checks
of their own). The always-on excludes hold for a project's governed docs, the workspace's docs,
the doc-registry and `measure`; a project's own `[projects] exclude` adds to them.

    python3 -m unittest discover -s engine/tests -k own_dirs
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from govern import blocks, cli, config, installer, layout, profile, registry  # noqa: E402
from govern.context import Context  # noqa: E402
from govern.measure import measure  # noqa: E402
from test_measure import FM, commit, put  # noqa: E402

AGENT = ("---\nname: x\ndescription: x\nmodel: opus\neffort: high\nomitClaudeMd: true\n---\n"
         "# Agent\n")


def quiet(fn, *args, **kwargs) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = fn(*args, **kwargs)
    return code, out.getvalue()


class Adopted(unittest.TestCase):
    """A git repo with an agent under `.claude/agents/` and whatever `files` a test adds (path ->
    text), adopted as a single repo; `adopt_output` is what adopt printed."""

    def adopt(self, files: dict[str, str], git: bool = True) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(profile.rmtree, self.tmp)
        self.root = self.tmp / "root"
        env = {"HOME": str(self.tmp / "home"), "CONTEXT_GATE_NO_UPDATE_CHECK": "1"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for rel, text in files.items():
            put(self.root / rel, text)
        put(self.root / ".claude/agents/x.md", AGENT)
        put(self.root / ".gitignore", ".claude/worktrees/\n")
        if git:
            commit(self.root)
        answers = self.tmp / "answers.toml"
        put(answers, 'shape = "single"\n')
        code, self.adopt_output = quiet(installer.main, ["adopt", "--root", str(self.root),
                                                         "--answers", str(answers), "--apply"])
        self.assertEqual(code, 0, self.adopt_output)

    def gate(self) -> tuple[int, str]:
        return quiet(cli.main, ["check"], root=self.root)

    def context(self) -> Context:
        cfg = config.load(self.root, layout.home())
        return Context(root=self.root, home=layout.home(), cfg=cfg,
                       registry=registry.load(cfg), prog="govern")

    def set_config(self, old: str, new: str) -> None:
        path = self.root / layout.CONFIG
        text = path.read_text(encoding="utf-8")
        self.assertEqual(text.count(old), 1, text)
        put(path, text.replace(old, new))

    def governed(self) -> list[str]:
        ctx = self.context()
        (scope,) = [s for s in ctx.registry.scopes if s.governed]
        return [rel for rel, _ in ctx.governed_docs(scope.gov)]

    def projects(self) -> dict:
        with open(self.root / layout.CONFIG, "rb") as fh:
            return tomllib.load(fh).get("projects", {})


class NoDocsDir(Adopted):
    """A README with frontmatter (a governed doc on purpose), an agent and no `docs/`."""

    def setUp(self) -> None:
        self.adopt({"README.md": FM + "# Hello\n\nA project.\n"})

    def test_a_fresh_adopt_is_green_and_governs_neither_dir(self):
        self.assertTrue((self.root / ".context-gate/README.md").is_file())
        self.assertTrue((self.root / ".context-gate/install-report.md").is_file())
        code, out = self.gate()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.governed(), ["DECISIONS.md", "README.md"])
        ctx = self.context()
        (scope,) = [s for s in ctx.registry.scopes if s.governed]
        table = blocks.doc_registry(ctx, scope.gov, self.root / "INDEX.md")
        self.assertIn("README.md", table)
        self.assertNotIn(".context-gate", table)
        self.assertNotIn(".claude", table)

    def test_an_explicit_catch_all_glob_still_leaves_both_dirs_out(self):
        self.set_config("[projects]\n", '[projects]\ndocs = ["**/*.md"]\n')
        self.set_config("[governance]\n", '[workspace]\ndocs = ["**/*.md"]\n\n[governance]\n')
        code, out = self.gate()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.governed(), ["DECISIONS.md", "README.md"])
        self.assertEqual([p.relative_to(self.root).as_posix()
                          for p in self.context().workspace_docs()],
                         ["DECISIONS.md", "README.md"])

    def test_a_project_exclude_adds_to_the_always_on_excludes(self):
        put(self.root / "notes/scratch.md", "# No frontmatter\n")
        code, out = self.gate()
        self.assertEqual(code, 1, out)
        self.assertIn("notes/scratch.md", out)
        self.set_config("[projects]\n", '[projects]\nexclude = ["notes/**"]\n')
        code, out = self.gate()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.governed(), ["DECISIONS.md", "README.md"])

    def test_measure_neither_counts_the_dirs_nor_sees_an_agent_worktree(self):
        put(self.root / ".claude/skills/s/DECISIONS.md", FM + "# Decisions\n\n## D-700 — x\n")
        put(self.root / ".claude/worktrees/agent-1/.git", "gitdir: elsewhere\n")
        m = measure(self.root)
        self.assertEqual(m.subrepos, [])
        self.assertNotIn(700, m.max_ids.values())
        self.assertEqual([log.path for log in m.workspace.decision_logs], ["DECISIONS.md"])

    def test_a_readme_with_frontmatter_is_not_proposed_for_exclude(self):
        self.assertNotIn("exclude", self.projects())


class GitHubFiles(Adopted):
    """A typical repo's GitHub-facing files, none with frontmatter (GitHub would render it as a
    table): adopt proposes them as `[projects] exclude` when there is no docs dir, and only then."""

    PLAIN = {"README.md": "# Hello\n\nA project.\n", "CHANGELOG.md": "# Changelog\n",
             ".github/ISSUE_TEMPLATE/bug.md": "Describe the bug.\n"}

    def test_with_no_docs_dir_they_are_excluded_visibly_and_the_gate_is_green(self):
        self.adopt(self.PLAIN)
        code, out = self.gate()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.projects()["exclude"],
                         ["CHANGELOG.md", "README.md", ".github/ISSUE_TEMPLATE/bug.md"])
        self.assertNotIn("docs", self.projects())
        self.assertEqual(self.governed(), ["DECISIONS.md"])
        report = (self.root / ".context-gate/adopt-report.md").read_text(encoding="utf-8")
        self.assertIn("GitHub-facing files, not governed docs; remove from exclude to govern "
                      "them", report)

    def test_taking_one_out_of_exclude_governs_it_again(self):
        self.adopt(self.PLAIN)
        self.set_config('"README.md", ', "")
        self.assertEqual(self.governed(), ["DECISIONS.md", "README.md"])

    def test_with_a_docs_dir_nothing_is_excluded(self):
        self.adopt({**self.PLAIN, "docs/guide.md": FM + "# Guide\n"})
        code, out = self.gate()
        self.assertEqual(code, 0, out)
        self.assertNotIn("exclude", self.projects())
        self.assertEqual(self.projects()["docs"], ["docs/**/*.md"])
        report = (self.root / ".context-gate/adopt-report.md").read_text(encoding="utf-8")
        self.assertNotIn("GitHub-facing", report)

    def test_an_issue_template_with_githubs_own_frontmatter_is_excluded_too(self):
        # GitHub's template keys (name, about, labels) are not this tool's doc frontmatter.
        template = "---\nname: Bug report\nabout: Something is broken\nlabels: bug\n---\nSteps.\n"
        self.adopt({**self.PLAIN, ".github/ISSUE_TEMPLATE/bug.md": template})
        code, out = self.gate()
        self.assertEqual(code, 0, out)
        self.assertIn(".github/ISSUE_TEMPLATE/bug.md", self.projects()["exclude"])
        self.assertNotIn("not inside a git repository", out)


class NotAGitRepo(Adopted):
    """A project folder with no git repository: adopt still works, and the gate says, as a
    warning, that the checks reading git history or ignore rules check nothing there."""

    def test_the_gate_warns_that_it_is_not_a_git_repository(self):
        self.adopt(GitHubFiles.PLAIN, git=False)
        code, out = self.gate()
        self.assertEqual(code, 0, out)
        self.assertIn("not inside a git repository", out)
        self.assertIn("git init", out)


if __name__ == "__main__":
    unittest.main()
