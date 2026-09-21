"""Behaviour tests on small throwaway workspaces, each built in the shape a registry file
really takes rather than a shape the code happens to expect.

    python3 -m unittest discover -s engine/tests
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

ENGINE = Path(__file__).resolve().parent.parent
REPO = ENGINE.parent
sys.path.insert(0, str(ENGINE))

from govern import __version__, cli, config, installer, layout, manifest, notice  # noqa: E402
from govern import ratchet, registry, text  # noqa: E402

CFG = layout.CONFIG

TODAY = date.today().isoformat()

CONFIG = """
[governance]
engine = "{version}"
schema = 1
{extensions}

[dialect]
decision_heading = "em-dash"
markers = "t"

[registry]
file = "projects.toml"
entries = "project"

[registry.keys]
handoff = "{handoff_key}"

[workspace]
required_docs = ["AGENTS.md", "governance/DECISIONS.md"]
docs = ["AGENTS.md", "governance/*.md"]
decision_log = "governance/DECISIONS.md"

[projects]
required_docs = ["DECISIONS.md"]

[blocks]
project = [{{ file = "DECISIONS.md", id = "decision-index" }}]

[checks.decision-log]
statuses = ["locked", "provisional", "superseded"]
{extra}
"""

REGISTRY = """
[workspace]
id_prefix = "W"
id_range = "{ws_range}"

[[project]]
name = "alpha"
dir = "alpha"
tier = "full"
governance = "projects/alpha"
id_prefix = "A"
id_range = "{alpha_range}"
licence = "MIT"
{alpha_extra}

[project.profile]
handoff = "projects/alpha/working-files/HANDOFF.md"
"""


def wtext(path: Path, content: str) -> None:
    """Exactly these characters, on every OS: UTF-8, no newline translation."""
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(content)


def fm(doc_type="control", **extra) -> str:
    lines = ["---", f"doc_type: {doc_type}", "purpose: test", "audience: agent",
             "load_when: testing", f"last_reviewed: {extra.pop('last_reviewed', TODAY)}"]
    lines += [f"{k}: {v}" for k, v in extra.items()]
    return "\n".join(lines + ["---", ""])


def entry(prefix: str, num: int, status="locked", sep=" — ", extra="") -> str:
    return (f"\n## {prefix}-{num}{sep}Title {num}\n\n**Status:** {status}\n\n"
            f"**Rule:** r.\n\n**Why:** w.\n{extra}")


class Workspace:
    """A minimal governance root: one workspace log and one governed project."""

    def __init__(self, tmp: Path, *, handoff_key="profile.handoff", ws_range="1-99",
                 alpha_range="100-199", alpha_extra="", extensions="", extra=""):
        self.root = tmp / "ws"
        self.home = tmp / "home"
        self.home.mkdir()
        self.write(CFG, CONFIG.format(version=__version__, handoff_key=handoff_key,
                                                    extensions=extensions, extra=extra))
        self.write("projects.toml", REGISTRY.format(ws_range=ws_range, alpha_range=alpha_range,
                                                    alpha_extra=alpha_extra))
        self.write("AGENTS.md", fm() + "# Agents\n")
        self.write("governance/DECISIONS.md", fm() + "# Decisions\n" + entry("W", 1))
        self.write("projects/alpha/DECISIONS.md",
                   fm() + "# Decisions\n\n<!-- t:generated:start id=decision-index -->\n"
                   "<!-- t:generated:end id=decision-index -->\n" + entry("A", 100))
        self.index()

    def write(self, rel: str, content: str | bytes) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            wtext(path, content)
        return path

    def run(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    code = cli.main(list(argv), root=self.root)
                except SystemExit as exc:
                    code = exc.code
        finally:
            if old_home is not None:
                os.environ["HOME"] = old_home
        return code, out.getvalue(), err.getvalue()

    def index(self) -> None:
        self.run("index")


def installer_check(root: Path) -> tuple[int, str, str]:
    """`check`, against a project just installed with the real installer — `Workspace.run`
    without a `Workspace`, since installer tests build the root by hand."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main(["check"], root=root)
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def ws(self, **kw) -> Workspace:
        return Workspace(self.tmp, **kw)


class Baseline(Base):
    def test_clean_workspace_passes(self):
        code, out, _ = self.ws().run("check")
        self.assertEqual(code, 0, out)


class MisplacedRegistryKey(Base):
    def test_handoff_read_from_profile(self):
        w = self.ws()
        w.write("projects/alpha/working-files/HANDOFF.md", fm("working", status="active")
                + "word " * 30)
        w.write(CFG, (w.root / CFG).read_text(encoding="utf-8")
                + "\n[checks.handoff-words]\nmax_words = 10\n")
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertIn("alpha: HANDOFF.md is 30 words, over handoff-words max_words=10", out)

    def test_key_configured_where_the_registry_does_not_put_it(self):
        code, out, _ = self.ws(handoff_key="handoff").run("check")
        self.assertIn("registry: 'alpha' has 'profile.handoff', but the engine reads handoff "
                      "from 'handoff'", out)
        self.assertEqual(code, 1)


class WritingRulesFiles(Base):
    def test_every_configured_file_set_is_checked(self):
        w = self.ws(extra="""
[checks.writing-rules]
level = "error"
files = ["one/pr-*.md", "two/pr-*.md"]
rules = [{ text = "colour", use = "color", ignore_case = true, why = "house style" },
         { text = "monster", use = "mob", level = "warn" }]
""")
        w.write("two/pr-draft.md", "The Colour of a monster.\n")
        code, out, _ = w.run("check")
        self.assertIn("ERROR  two/pr-draft.md: 1 × 'colour' (use 'color') — house style", out)
        self.assertIn("warn   two/pr-draft.md: 1 × 'monster' (use 'mob')", out)


class GenericChecks(Base):
    """Generic engine checks, each configured from config.toml rather than hard-coded."""

    def test_hooks_wired(self):
        w = self.ws(extra="""
[checks.hooks-wired]
level = "error"
[[checks.hooks-wired.hooks]]
script = "hooks/guard.py"
event = "PreToolUse"
matcher = "Bash"
rule = "R-1"
unwired = "the rule is unenforced"
""")
        w.write("hooks/guard.py", "")
        w.write(".claude/settings.json", '{"hooks": {}}')
        code, out, _ = w.run("check")
        self.assertIn("R-1: no PreToolUse hook on Bash runs guard.py — the rule is unenforced", out)
        w.write(".claude/settings.json", json.dumps({"hooks": {"PreToolUse": [
            {"matcher": "Bash",
             "hooks": [{"type": "command", "command": "python3 hooks/guard.py"}]}]}}))
        code, out, _ = w.run("check")
        self.assertEqual(code, 0, out)

    def test_licences(self):
        w = self.ws(alpha_extra='role = "own"', extra="""
[checks.licences]
level = "error"
conflicts = [{ a = { licences = ["MIT"] }, b = { licences = ["MIT"], roles = ["own"] }, decision = "W-9" }]
""")
        code, out, _ = w.run("check")
        self.assertIn("W-9 is not a recorded decision", out)

    def test_checkout_hygiene(self):
        w = self.ws(alpha_extra='role = "contribute"', extra="""
[checks.checkout-hygiene]
level = "error"
roles = ["contribute"]
forbidden = ["AGENTS.md"]
rules = { contribute = "W-1" }
setup_hint = "run ./bootstrap.sh"
""")
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertIn("alpha: checkout 'alpha' not present — run ./bootstrap.sh", out)
        w.write("alpha/AGENTS.md", "x")
        subprocess.run(["git", "-C", str(w.root / "alpha"), "init", "-q"], check=True)
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertIn("alpha: 'AGENTS.md' is present but untracked inside the checkout (W-1)", out)

    def test_generic_checks_are_off_until_configured(self):
        code, out, _ = self.ws().run("check")
        self.assertEqual(code, 0, out)


class Extensions(Base):
    def test_project_extension_registers_and_runs(self):
        w = self.ws(extensions='extensions = ["gov-ext"]',
                    extra='\n[checks.no-todo]\nlevel = "error"\n')
        w.write("gov-ext/todo.py", """
from govern.findings import Findings
from govern.manifest import check

@check("no-todo", scope="workspace", since="0.1.1", origin="test-ext",
       summary="No TODO in AGENTS.md.", question="q", rationale="r")
def no_todo(ctx, params):
    f = Findings()
    if "TODO" in (ctx.root / "AGENTS.md").read_text(encoding="utf-8"):
        f.error("AGENTS.md: TODO")
    return f
""")
        self.addCleanup(manifest.forget, "test-ext")
        w.write("AGENTS.md", fm() + "TODO\n")
        code, out, _ = w.run("check")
        self.assertIn("AGENTS.md: TODO", out)


class MalformedHeading(Base):
    def test_hyphen_and_colon_headings_are_errors(self):
        w = self.ws()
        log = w.root / "projects/alpha/DECISIONS.md"
        wtext(log, log.read_text(encoding="utf-8") + entry("A", 101, sep=" - ") + entry("A", 102, sep=": "))
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertIn("'## A-101 - Title 101' is not a decision entry heading", out)
        self.assertIn("'## A-102: Title 102' is not a decision entry heading", out)


class MissingMarkers(Base):
    def test_deleted_markers_are_an_error(self):
        w = self.ws()
        w.write("projects/alpha/DECISIONS.md", fm() + entry("A", 100))
        code, out, _ = w.run("check")
        self.assertIn("generated block 'decision-index' has no markers", out)
        code, out, _ = w.run("index")
        self.assertEqual(code, 1)


class CorruptBaseline(Base):
    def test_never_overwritten(self):
        w = self.ws()
        path = w.write(layout.BASELINE, '{"x": 1,')
        code, out, _ = w.run("baseline")
        self.assertEqual(code, 1)
        self.assertEqual(path.read_text(encoding="utf-8"), '{"x": 1,')
        code, out, _ = w.run("check")
        self.assertIn("is not a valid baseline", out)


class NonUtf8File(Base):
    def test_non_utf8_reported_as_encoding(self):
        w = self.ws()
        w.write("projects/alpha/latin.md", "caf\xe9".encode("latin-1"))
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertIn("alpha/latin.md: not valid UTF-8", out)
        self.assertNotIn("latin.md: no frontmatter", out)


class GitDecoding(Base):
    """`context.git` decodes as UTF-8 with `errors="replace"` (never the platform locale, which
    is not UTF-8 by default on Windows) — a byte git wrote that the console's locale disagrees
    on never raises."""

    def test_non_utf8_output_is_replaced_not_raised(self):
        from unittest import mock
        from govern.context import git
        completed = subprocess.CompletedProcess(args=["git"], returncode=0,
                                                 stdout=b"caf\xe9\n", stderr=b"")
        with mock.patch("subprocess.run", return_value=completed) as spy:
            out = git(Path("."), "log", "-1")
        self.assertEqual(out, "caf�")
        self.assertNotIn("text", spy.call_args.kwargs)
        self.assertNotIn("encoding", spy.call_args.kwargs)


class FrontmatterParsing(Base):
    def test_misreadings(self):
        f, _ = text.parse_frontmatter(textwrap.dedent("""\
            ---
            doc_type: control
            nested:
              doc_type: working
            related:
              - a.md
              - b.md
            load_when: anything that "should work"
            quoted: "whole"
            one: x.md
            ---
            body"""))
        self.assertEqual(f.get("doc_type"), "control")
        self.assertEqual(f.get_list("related"), ["a.md", "b.md"])
        self.assertEqual(f.get("load_when"), 'anything that "should work"')
        self.assertEqual(f.get("quoted"), "whole")
        self.assertEqual(f.get_list("one"), ["x.md"])


class StatusCase(Base):
    def test_status_case_insensitive_and_missing_named(self):
        w = self.ws()
        log = w.root / "projects/alpha/DECISIONS.md"
        wtext(log, log.read_text(encoding="utf-8") + entry("A", 101, status="Locked")
                       + "\n## A-102 — No status\n\n**Rule:** r.\n\n**Why:** w.\n")
        w.index()
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertNotIn("A-101", out)
        self.assertIn("projects/alpha/DECISIONS.md: A-102 has no **Status:**", out)


class AgentEffort(Base):
    def test_effort_required(self):
        w = self.ws()
        w.write(".claude/agents/helper.md",
                "---\nname: helper\ndescription: d\nmodel: opus\nmaxTurns: 10\n---\nbody\n")
        code, out, _ = w.run("check")
        self.assertIn("agents/helper.md: frontmatter missing 'effort'", out)


class TrailingSection(Base):
    def test_last_entry_ends_at_next_section(self):
        w = self.ws()
        log = w.root / "projects/alpha/DECISIONS.md"
        wtext(log, log.read_text(encoding="utf-8") + "\n## Appendix\n\n" + "word " * 500 + "\n")
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertNotIn("decision-log max_words", out)


class FencesAndComments(Base):
    def test_examples_are_not_entries(self):
        w = self.ws()
        log = w.root / "projects/alpha/DECISIONS.md"
        wtext(log, log.read_text(encoding="utf-8") + "\n```\n## A-150 — Example in a fence\n```\n"
                       "\n<!--\n## A-151 — Example in a comment\n-->\n")
        code, out, _ = w.run("show", "--project", "alpha", "--list")
        self.assertNotIn("A-150", out)
        self.assertNotIn("A-151", out)


class DirtyFinishedFile(Base):
    def test_uncommitted_edit_is_touched_now(self):
        w = self.ws()
        path = w.write("projects/alpha/working-files/plan.md",
                       fm("working", status="complete") + "body\n")
        env = {**os.environ, "GIT_AUTHOR_DATE": "2020-01-01T00:00:00",
               "GIT_COMMITTER_DATE": "2020-01-01T00:00:00"}
        for cmd in (["init", "-q"], ["add", "-A"],
                    ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"]):
            subprocess.run(["git", "-C", str(w.root), *cmd], check=True, env=env)
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertIn("plan.md: status 'complete'", out)
        wtext(path, path.read_text(encoding="utf-8") + "edit\n")
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertNotIn("plan.md: status 'complete'", out)


class Committed(Base):
    """`Context.committed`: committed means "in git history", checked
    with `git log`, never `git status` — which misses a file staged but not yet committed, one
    hidden by `.gitignore` or `status.showUntrackedFiles=no`, and fails closed rather than
    reading any of those, or a failing git call, as clean."""

    def _git(self, root: Path, *args: str) -> None:
        subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t",
                        "-c", "tag.gpgSign=false", "-c", "commit.gpgSign=false", *args],
                       check=True, capture_output=True)

    def _plan(self, w: "Workspace") -> Path:
        return w.write("projects/alpha/working-files/plan.md",
                       fm("working", status="complete") + "body\n")

    def _assert_never_committed(self, out: str) -> None:
        self.assertIn("plan.md is finished but was never committed — commit it once so git "
                      "keeps it, then delete it", out)
        self.assertNotIn("last touched", out)

    def test_not_a_repo_is_flagged_instead_of_aged(self):
        w = self.ws()
        self._plan(w)
        code, out, _ = w.run("check", "--project", "alpha")
        self._assert_never_committed(out)

    def test_staged_not_committed_is_flagged_instead_of_aged(self):
        w = self.ws()
        self._git(w.root, "init", "-q")
        self._plan(w)
        self._git(w.root, "add", "projects/alpha/working-files/plan.md")
        code, out, _ = w.run("check", "--project", "alpha")
        self._assert_never_committed(out)

    def test_gitignored_file_is_flagged_instead_of_aged(self):
        w = self.ws()
        self._git(w.root, "init", "-q")
        w.write(".gitignore", "working-files/\n")
        self._git(w.root, "add", "-A")
        self._git(w.root, "commit", "-qm", "initial")
        self._plan(w)
        code, out, _ = w.run("check", "--project", "alpha")
        self._assert_never_committed(out)

    def test_status_hidden_untracked_file_is_flagged_instead_of_aged(self):
        w = self.ws()
        self._git(w.root, "init", "-q")
        self._git(w.root, "config", "status.showUntrackedFiles", "no")
        self._plan(w)
        code, out, _ = w.run("check", "--project", "alpha")
        self._assert_never_committed(out)

    def test_failing_git_is_flagged_instead_of_aged_even_when_committed(self):
        from unittest import mock
        from govern import context as context_mod
        w = self.ws()
        self._git(w.root, "init", "-q")
        self._plan(w)
        self._git(w.root, "add", "-A")
        self._git(w.root, "commit", "-qm", "x")
        with mock.patch.object(context_mod, "git", return_value=None):
            code, out, _ = w.run("check", "--project", "alpha")
        self._assert_never_committed(out)

    def test_committed_file_is_aged_not_flagged(self):
        w = self.ws()
        self._git(w.root, "init", "-q")
        self._plan(w)
        self._git(w.root, "add", "-A")
        env = {**os.environ, "GIT_AUTHOR_DATE": "2020-01-01T00:00:00",
               "GIT_COMMITTER_DATE": "2020-01-01T00:00:00"}
        subprocess.run(["git", "-C", str(w.root), "-c", "user.email=t@t", "-c", "user.name=t",
                       "-c", "tag.gpgSign=false", "-c", "commit.gpgSign=false",
                       "commit", "-qm", "x"], check=True, env=env, capture_output=True)
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertIn("plan.md: status 'complete', last touched", out)
        self.assertNotIn("never committed", out)


class MalformedRange(Base):
    def test_finding_not_crash(self):
        code, out, err = self.ws(alpha_range="100–199").run("check")
        self.assertEqual(code, 1)
        self.assertIn("registry: 'alpha' id_range '100–199' is not LO-HI", out)
        self.assertNotIn("Traceback", err)


class PipeInTitle(Base):
    def test_cell_escaped(self):
        w = self.ws()
        log = w.root / "projects/alpha/DECISIONS.md"
        wtext(log, log.read_text(encoding="utf-8").replace("Title 100", "Pick A | B"))
        w.index()
        self.assertIn("Pick A \\| B", log.read_text(encoding="utf-8"))
        code, out, _ = w.run("check")
        self.assertEqual(code, 0, out)


class DateFormat(Base):
    def test_strict_dates(self):
        w = self.ws()
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        w.write("projects/alpha/a.md", fm(last_reviewed="20260918"))
        w.write("projects/alpha/b.md", fm(last_reviewed="2026-9-5"))
        w.write("projects/alpha/c.md", fm(last_reviewed=tomorrow))
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertIn("alpha/a.md: last_reviewed '20260918' is not YYYY-MM-DD", out)
        self.assertIn("alpha/b.md: last_reviewed '2026-9-5' is not YYYY-MM-DD", out)
        self.assertIn(f"alpha/c.md: last_reviewed '{tomorrow}' is in the future", out)


class WorkspaceDocs(Base):
    def test_same_rules_as_project_docs(self):
        w = self.ws()
        old = (date.today() - timedelta(days=400)).isoformat()
        w.write("governance/notes.md", fm("working", status="active", last_reviewed=old))
        w.write("governance/bad.md", fm("manual"))
        code, out, _ = w.run("check")
        self.assertNotIn("notes.md: last_reviewed", out)
        self.assertIn("governance/bad.md: doc_type 'manual' not in", out)


class MemorySlug(Base):
    def test_slug_derived_from_root(self):
        self.assertEqual(text.claude_project_slug(Path("/home/a.b/My_Proj")),
                         "-home-a-b-My-Proj")

    def test_memory_index_found_under_root_slug(self):
        w = self.ws()
        mem = (w.home / ".claude" / "projects" / text.claude_project_slug(w.root) / "memory"
               / "MEMORY.md")
        mem.parent.mkdir(parents=True)
        wtext(mem, "- line\n" * 50)
        code, out, _ = w.run("check")
        self.assertIn("memory index: 50 non-blank lines", out)


class FindingNamesItsSetting(Base):
    def test_a_limit_finding_names_the_check_and_parameter_that_set_it(self):
        # What a project changes in config.toml to move the limit, not an internal name.
        w = self.ws(extra="\n[checks.doc-frontmatter]\nmax_working_words = 3\nstale_days = 10\n"
                          "\n[checks.governed-doc-count]\nmax_docs = 1\n")
        old = (date.today() - timedelta(days=30)).isoformat()
        w.write("projects/alpha/guide.md", fm("reference", last_reviewed=old))
        w.write("projects/alpha/working-files/notes.md",
                fm("working", status="active") + "word " * 10)
        mem = (w.home / ".claude" / "projects" / text.claude_project_slug(w.root) / "memory"
               / "MEMORY.md")
        mem.parent.mkdir(parents=True)
        wtext(mem, "- line\n" * 50)
        _, out, _ = w.run("check")
        self.assertIn("last_reviewed 30d ago exceeds doc-frontmatter stale_days=10", out)
        self.assertIn("words exceeds doc-frontmatter max_working_words=3", out)
        self.assertIn("governed docs exceeds governed-doc-count max_docs=1", out)
        self.assertIn("50 non-blank lines exceeds memory-index max_lines=40", out)


class RangesFromConfig(Base):
    def test_workspace_range_is_checked_for_overlap(self):
        w = self.ws(ws_range="1-150", alpha_range="100-199")
        code, out, _ = w.run("check")
        self.assertNotIn("id ranges overlap", out)   # prefix-aware: W and A cannot collide
        cfg = w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace('markers = "t"',
                                               'markers = "t"\nid_overlap = "prefix-blind"'))
        code, out, _ = w.run("check")
        self.assertIn("id ranges overlap: workspace 1-150 and alpha 100-199", out)


class ExitCodes(Base):
    def test_refused_baseline_exits_nonzero(self):
        w = self.ws(extra="\n[checks.governed-doc-count]\nmax_docs = 0\n")
        code, out, _ = w.run("baseline")
        self.assertEqual(code, 1)
        self.assertIn("refused new entry 'governed_docs:alpha'", out)
        code, out, _ = w.run("baseline", "--allow-raise")
        self.assertEqual(code, 0)


class NextIdPrefix(Base):
    def test_other_prefix_does_not_block(self):
        w = self.ws()
        log = w.root / "projects/alpha/DECISIONS.md"
        wtext(log, log.read_text(encoding="utf-8") + entry("X", 101))
        code, out, _ = w.run("next-id", "--project", "alpha")
        self.assertEqual(out.strip(), "A-101")


class UnknownScope(Base):
    def test_unknown_project_is_usage_error(self):
        w = self.ws()
        for argv in (["find", "--project", "nope", "x"], ["next-id", "--project", "nope"],
                     ["show", "--project", "nope", "--list"]):
            code, _, err = w.run(*argv)
            self.assertEqual(code, 2, argv)


class Config(Base):
    def test_unknown_key_rejected(self):
        code, _, err = self.ws(extra="\n[checks.decision-log]\nmax_wrods = 5\n").run("check")
        self.assertEqual(code, 2)

    def test_loosening_needs_a_reason(self):
        # agents.max_turns is excluded from this test: its engine default (0) means no ceiling,
        # already the loosest a ceiling can be, so no positive value can loosen it further.
        w = self.ws(extra="\n[checks.working-file-count]\nmax_files = 999\n")
        code, _, err = w.run("check")
        self.assertEqual(code, 2)
        self.assertIn("without a 'reason'", err)

    def test_fixed_core_cannot_be_off(self):
        code, _, err = self.ws(extra='\n[checks.ratchet]\nlevel = "off"\n').run("check")
        self.assertEqual(code, 2)
        self.assertIn("fixed core", err)


class EdgeCases(Base):
    """Unreadable logs, masking, malformed headings, bad config tables and other edge cases
    across the CLI."""

    def test_unreadable_log_blocks_every_write(self):
        w = self.ws(extra="\n[checks.governed-doc-count]\nmax_docs = 0\n")
        self.assertEqual(w.run("baseline", "--allow-raise")[0], 0)
        base = (w.root / layout.BASELINE).read_text(encoding="utf-8")
        log = w.root / "projects/alpha/DECISIONS.md"
        log.write_bytes(log.read_bytes() + b"\xff")
        for argv in (["baseline"], ["index"], ["next-id", "--project", "alpha"]):
            code, out, err = w.run(*argv)
            self.assertEqual(code, 1, argv)
            self.assertIn("not valid UTF-8", err)
        self.assertEqual((w.root / layout.BASELINE).read_text(encoding="utf-8"), base)
        code, out, _ = w.run("check")
        self.assertIn("projects/alpha/DECISIONS.md: not valid UTF-8", out)

    def test_masking(self):
        from govern.decisions import Grammar
        grammar = Grammar("em-dash", ["Rule", "Why"], text.Markers("t"))
        e = lambda n: f"## W-{n} — T{n}\n\n**Status:** locked\n"
        for mid in ("see `<!-- t:generated:start id=x -->` here\n", "use `<!--` to open\n",
                    "~~~\n```\n~~~\n", "````\n```\n## W-9 — phantom\n````\n",
                    "<!-- a --> x <!-- b\n## W-8 — hidden\n-->\n"):
            ids = [x.ident for x in grammar.parse(e(1) + mid + e(2) + e(3)).entries]
            self.assertEqual(ids, ["W-1", "W-2", "W-3"], mid)

    def test_malformed_heading_spends_its_number(self):
        w = self.ws()
        log = w.root / "projects/alpha/DECISIONS.md"
        wtext(log, log.read_text(encoding="utf-8") + entry("A", 101, sep=" - "))
        code, out, _ = w.run("next-id", "--project", "alpha")
        self.assertEqual(out.strip(), "A-102")

    def test_show_lines_matches_show(self):
        w = self.ws()
        log = w.root / "projects/alpha/DECISIONS.md"
        wtext(log, log.read_text(encoding="utf-8") + "\n```\n## Example\n```\nmore\n" + entry("A", 101))
        code, out, _ = w.run("show", "--project", "alpha", "--lines", "A-100")
        start, end = map(int, out.split()[0].rsplit(":", 1)[1].split("-"))
        lines = log.read_text(encoding="utf-8").splitlines()
        self.assertTrue(lines[start - 1].startswith("## A-100"))
        self.assertIn("more", lines[start - 1:end])

    def test_bad_config_tables_are_usage_errors(self):
        w = self.ws()
        cfg = w.root / CFG
        good = cfg.read_text(encoding="utf-8")
        for bad in ('project = [{ file = "DECISIONS.md", id = "nope" }]',
                    'project = [{ file = "DECISIONS.md", id = "decision-index" }]\n'
                    'registry_columns = [{ header = "X", kye = "role" }]'):
            wtext(cfg, good.replace(
                'project = [{ file = "DECISIONS.md", id = "decision-index" }]', bad))
            code, _, err = w.run("check")
            self.assertEqual(code, 2, bad)
            self.assertNotIn("Traceback", err)
        wtext(cfg, good)
        wtext((w.root / "projects.toml"), "[[project]\n")
        code, _, err = w.run("check")
        self.assertEqual(code, 2)

    def test_frontmatter_yaml_forms(self):
        f, _ = text.parse_frontmatter("---\nrelated:\n- a.md\n- b.md\npurpose: >\n  one\n"
                                      "  two\ndoc_type: control\n---\n")
        self.assertEqual(f.get_list("related"), ["a.md", "b.md"])
        self.assertEqual(f.get("purpose"), "one two")
        self.assertEqual(f.get("doc_type"), "control")

    def test_misplaced_leaf_beside_configured_one(self):
        code, out, _ = self.ws(alpha_extra='handoff = "x.md"').run("check")
        self.assertIn("registry: 'alpha' has 'handoff'", out)

    def test_missing_extension_dir(self):
        code, _, err = self.ws(extensions='extensions = ["nope"]').run("check")
        self.assertEqual(code, 2)
        self.assertIn("extensions directory 'nope' does not exist", err)

    def test_find_workspace_and_show_unknown(self):
        w = self.ws()
        code, out, _ = w.run("find", "--project", "workspace", "Title")
        self.assertEqual(code, 0)
        self.assertIn("W-1", out)
        code, _, _ = w.run("show", "--project", "typo", "W-1")
        self.assertEqual(code, 2)

    def test_section_titles_are_not_malformed_entries(self):
        w = self.ws()
        log = w.root / "projects/alpha/DECISIONS.md"
        wtext(log, log.read_text(encoding="utf-8") + "\n## UTF-8 and BOMs\n\n### A-150 — too deep\n")
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertNotIn("UTF-8 and BOMs", out)
        self.assertIn("'### A-150 — too deep' is not a decision entry heading", out)

    def test_finished_file_outside_git_is_flagged_not_aged_by_mtime(self):
        # "Committed" means "in git history" (`Context.committed`, checked with `git log`):
        # outside a repo entirely that can never be true, so the file is flagged as never
        # committed rather than falling back to its mtime (see the `Committed` test class).
        w = self.ws()
        path = w.write("projects/alpha/working-files/plan.md",
                       fm("working", status="complete") + "body\n")
        old = (date.today() - timedelta(days=30))
        stamp = datetime_timestamp(old)
        os.utime(path, (stamp, stamp))
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertIn("plan.md is finished but was never committed", out)
        self.assertNotIn("last touched", out)


def datetime_timestamp(d: date) -> float:
    from datetime import datetime
    return datetime(d.year, d.month, d.day).timestamp()


# Versions relative to this engine's, so a release bump never breaks the tests that need a newer
# or an older engine beside it.
_MAJOR, _MINOR = (int(x) for x in __version__.split(".")[:2])
SERIES = f"{_MAJOR}.{_MINOR}"
PREV_SERIES = f"{_MAJOR}.{_MINOR - 1}" if _MINOR else f"{_MAJOR - 1}.99"
NEXT_RELEASE = f"{_MAJOR}.{_MINOR + 1}.0"


class EnginePin(Base):
    """A project runs only the engine it pins (or, for a series pin, one from that series)."""

    def test_missing_newer_and_older_pins_are_refused(self):
        w = self.ws()
        cfg = w.root / CFG
        good = cfg.read_text(encoding="utf-8")
        for pin, words in (('', "is required"), (f'engine = "{_MAJOR}.{_MINOR + 1}"', "newer"),
                           (f'engine = "{PREV_SERIES}"', "older"),
                           (f'engine = "{SERIES}.999"', "but this is engine")):
            wtext(cfg, good.replace(f'engine = "{__version__}"', pin))
            code, _, err = w.run("check")
            self.assertEqual(code, 2, pin)
            self.assertIn(words, err)

    def test_series_pin_runs_with_a_note(self):
        w = self.ws()
        cfg = w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(f'engine = "{__version__}"',
                                                            f'engine = "{SERIES}"'))
        code, _, err = w.run("check")
        self.assertEqual(code, 0)
        self.assertIn(f"this project pins the series {SERIES}", err)
        self.assertIn(f"Use python3 .context-gate/bin/upgrade to pin {__version__} exactly", err)

    def _installed(self, w):
        """Install for real (the installer writes the entry point), under a throwaway HOME."""
        cfg = self.tmp / "config.toml"
        shutil.move(str(w.root / CFG), str(cfg))
        (w.root / layout.GOV_DIR).rmdir()
        env = {k: v for k, v in os.environ.items() if k != "GOVERN_ENGINE"}
        env["HOME"] = str(w.home)
        res = subprocess.run([sys.executable, "-m", "govern.installer", "install", "--root",
                              str(w.root), "--config", str(cfg)],
                             env={**env, "PYTHONPATH": str(ENGINE)}, capture_output=True,
                             text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        return w.root / layout.ENTRYPOINT, env

    def _engines(self, w, versions):
        for version in versions:
            shutil.copytree(ENGINE / "govern", layout.engines_dir(w.home) / version / "govern",
                            ignore=shutil.ignore_patterns("__pycache__"))

    def _which(self, entry, env) -> str:
        probe = ("import sys, runpy\n"
                 "try:\n"
                 f"    runpy.run_path({str(entry)!r}, run_name='__main__')\n"
                 "except SystemExit: pass\n"
                 "print([p for p in sys.path if 'engines' in p][0])")
        res = subprocess.run([sys.executable, "-c", probe], env=env, capture_output=True,
                             text=True)
        return res.stdout.strip()

    def test_entrypoint_runs_exactly_the_pinned_engine(self):
        w = self.ws()
        entry, env = self._installed(w)
        self._engines(w, (f"{PREV_SERIES}.0", __version__, f"{SERIES}.99", NEXT_RELEASE))
        res = subprocess.run([sys.executable, str(entry), "check"], env=env,
                             capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        self.assertTrue(self._which(entry, env).endswith(__version__))
        # A newer engine is installed: one plain line (not a terminal), with the command.
        self.assertIn(f"context-gate {NEXT_RELEASE} available (this project runs {__version__}). "
                      f"Use python3 .context-gate/bin/upgrade to upgrade.", res.stderr)
        self.assertNotIn("\033[", res.stderr)

    def test_series_pin_runs_newest_in_the_series(self):
        w = self.ws()
        entry, env = self._installed(w)
        cfg = w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(f'engine = "{__version__}"',
                                                            f'engine = "{SERIES}"'))
        self._engines(w, (f"{SERIES}.0", f"{SERIES}.10", f"{SERIES}.2", NEXT_RELEASE))
        self.assertIn(f"{SERIES}.10", self._which(entry, env))

    def test_missing_engine_bootstraps_from_source(self):
        src = self.tmp / "source"
        shutil.copytree(ENGINE / "govern", src / "engine" / "govern",
                        ignore=shutil.ignore_patterns("__pycache__"))
        git = lambda *a: subprocess.run(
            ["git", "-C", str(src), "-c", "user.email=t@t", "-c", "user.name=t",
             "-c", "tag.gpgSign=false", "-c", "commit.gpgSign=false", *a],
            check=True, capture_output=True)
        git("init", "-q")
        git("add", "-A")
        git("commit", "-qm", "engine")
        git("tag", f"v{__version__}")
        w = self.ws()
        entry, env = self._installed(w)
        cfg = w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            "schema = 1", f'schema = 1\nsource = "{src.as_posix()}"', 1))
        res = subprocess.run([sys.executable, str(entry), "check"], env=env,
                             capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        self.assertIn(f"installing engine {__version__}", res.stderr)
        self.assertTrue((layout.engines_dir(w.home) / __version__ / "govern" / "cli.py").is_file())

    def test_entrypoint_without_an_installed_engine(self):
        w = self.ws()
        entry, env = self._installed(w)
        res = subprocess.run([sys.executable, str(entry)], env=env, capture_output=True,
                             text=True)
        self.assertEqual(res.returncode, 2)
        self.assertIn(f"engine {__version__} is not installed", res.stderr)

class LineEndings(Base):
    """A CRLF checkout (git on Windows with autocrlf) behaves exactly like an LF one."""

    def test_crlf_files_index_and_check_clean(self):
        w = self.ws()
        for rel in ("AGENTS.md", "governance/DECISIONS.md", "projects/alpha/DECISIONS.md"):
            path = w.root / rel
            path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        code, out, _ = w.run("check")
        self.assertEqual(code, 0, out)
        before = (w.root / "projects/alpha/DECISIONS.md").read_bytes()
        code, out, _ = w.run("index")
        self.assertIn("(0 block(s) updated)", out)
        log = w.root / "projects/alpha/DECISIONS.md"
        with open(log, encoding="utf-8", newline="") as fh:
            wtext(log, fh.read() + entry("A", 101).replace("\n", "\r\n"))
        w.run("index")
        after = log.read_bytes()
        self.assertNotIn(b"\n", after.replace(b"\r\n", b""), "an edit mixed line endings")
        self.assertTrue(before.startswith(b"---\r\n"))

    def test_messages_use_forward_slashes(self):
        w = self.ws()
        w.write("projects/alpha/sub/bad.md", fm("manual"))
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertIn("alpha/sub/bad.md: doc_type 'manual'", out)


class Installer(Base):
    """Everything the tool owns lives in one directory; uninstall reverses the rest."""

    def project(self, extra=""):
        w = self.ws(extra=extra)
        cfg = self.tmp / "config.toml"
        shutil.move(str(w.root / CFG), str(cfg))
        (w.root / layout.GOV_DIR).rmdir()
        w.write("governance/gate.py", "print('old gate')\n")
        w.write("governance/old-baseline.json", '{"governed_docs:alpha": 3}\n')
        return w.root, cfg

    def install(self, root, cfg, *extra):
        return installer.main(["install", "--root", str(root), "--config", str(cfg), *extra])

    def snapshot(self, root):
        return {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}

    def test_install_then_uninstall_leaves_the_project_as_it_was(self):
        root, cfg = self.project()
        before = self.snapshot(root)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.install(root, cfg, "--entrypoint", "governance/gate.py",
                                          "--migrate-baseline", "governance/old-baseline.json"), 0)
        self.assertTrue((root / layout.CONFIG).is_file())
        self.assertTrue((root / layout.BASELINE).is_file())
        self.assertFalse((root / "governance/old-baseline.json").exists())
        self.assertIn("runpy", (root / "governance/gate.py").read_text(encoding="utf-8"))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(installer.main(["uninstall", "--root", str(root)]), 0)
        self.assertEqual(self.snapshot(root), before)
        self.assertFalse((root / layout.GOV_DIR).exists())

    def test_uninstall_refuses_over_an_edited_stub_and_changes_nothing(self):
        root, cfg = self.project()
        with contextlib.redirect_stdout(io.StringIO()):
            self.install(root, cfg, "--entrypoint", "governance/gate.py")
        wtext(root / "governance/gate.py", "edited\n")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(installer.main(["uninstall", "--root", str(root)]), 1)
        self.assertTrue((root / layout.GOV_DIR).is_dir())
        self.assertEqual((root / "governance/gate.py").read_text(encoding="utf-8"), "edited\n")

    def test_bad_config_rolls_back(self):
        root, cfg = self.project()
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(f'engine = "{__version__}"', 'engine = "9.9.9"'))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.install(root, cfg, "--entrypoint", "governance/gate.py"), 2)
        self.assertFalse((root / layout.GOV_DIR).exists())
        self.assertEqual((root / "governance/gate.py").read_text(encoding="utf-8"),
                         "print('old gate')\n")

    def test_refuses_over_an_existing_install(self):
        root, cfg = self.project()
        with contextlib.redirect_stdout(io.StringIO()):
            self.install(root, cfg)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.install(root, cfg), 2)


class InstallBaselinesTodaysBreaches(Base):
    """Installing must not leave the gate red over a breach that predates the install, and must
    never raise an entry already in the baseline."""

    project = Installer.project
    install = Installer.install

    def oversized(self):
        root, cfg = self.project(extra="\n[checks.doc-frontmatter]\nmax_working_words = 3\n")
        notes = root / "projects/alpha/working-files/notes.md"
        notes.parent.mkdir(parents=True, exist_ok=True)
        wtext(notes, fm("working", status="active") + "word " * 10)
        return root, cfg

    def test_check_exits_0_after_install(self):
        root, cfg = self.oversized()
        with contextlib.redirect_stdout(io.StringIO()):
            code = self.install(root, cfg, "--entrypoint", "governance/gate.py")
        self.assertEqual(code, 0)
        report_text = (root / layout.GOV_DIR / "install-report.md").read_text(encoding="utf-8")
        self.assertIn("working_file_words:projects/alpha/working-files/notes.md=10", report_text)
        code, out, _ = installer_check(root)
        self.assertEqual(code, 0, out)

    def test_no_report_still_baselines(self):
        root, cfg = self.oversized()
        with contextlib.redirect_stdout(io.StringIO()):
            code = self.install(root, cfg, "--no-report")
        self.assertEqual(code, 0)
        self.assertFalse((root / layout.GOV_DIR / "install-report.md").exists())
        baseline = json.loads((root / layout.BASELINE).read_text(encoding="utf-8"))
        self.assertIn("working_file_words:projects/alpha/working-files/notes.md", baseline)

    def test_an_existing_migrated_entry_that_has_since_grown_stays_an_error(self):
        root, cfg = self.oversized()
        wtext(root / "governance/old-baseline.json",
             json.dumps({"working_file_words:projects/alpha/working-files/notes.md": 5}))
        with contextlib.redirect_stdout(io.StringIO()):
            code = self.install(root, cfg, "--migrate-baseline", "governance/old-baseline.json",
                                "--no-report")
        self.assertEqual(code, 0)
        baseline = json.loads((root / layout.BASELINE).read_text(encoding="utf-8"))
        # never raised: the migrated value (5) stands even though the file is now 10 words
        self.assertEqual(baseline["working_file_words:projects/alpha/working-files/notes.md"], 5)
        code, out, _ = installer_check(root)
        self.assertEqual(code, 1, out)
        self.assertIn("grew to 10 (baseline 5)", out)


class ExplainTrapsAndHints(Base):
    """`explain`, the ratchet opt-out, a trap's **Bites when:** line, stale references and the
    unknown-project hint."""

    def test_explain_shows_value_and_source(self):
        w = self.ws(extra="\n[checks.agents]\nmax_turns = 100\n")
        code, out, _ = w.run("explain", "agents")
        self.assertEqual(code, 0)
        self.assertIn("max_turns = 100  (project; engine default 0)", out)
        self.assertIn('efforts = ["low", "medium", "high", "xhigh", "max"]  (engine default)', out)

    def test_ratchet_opt_out_needs_a_reason_and_is_honoured(self):
        w = self.ws(extra="\n[checks.governed-doc-count]\nmax_docs = 0\nratchet = false\n")
        code, _, err = w.run("check")
        self.assertEqual(code, 2)
        self.assertIn("loosens ratchet", err)
        (self.tmp / "b").mkdir()
        w = Workspace(self.tmp / "b", extra="\n[checks.governed-doc-count]\nmax_docs = 0\n"
                      "ratchet = false\nreason = \"warn-only by policy\"\n")
        code, out, _ = w.run("check")
        self.assertEqual(code, 0, out)
        self.assertIn("governed docs exceeds", out)

    def test_trap_bites_when(self):
        w = self.ws()
        w.write("projects/alpha/working-files/traps.md", fm("working", status="active")
                + "\n## T-1 — Wraps\n\n**Bites when:** first line\ncontinues here.\n\nBody.\n"
                + "\n## T-2 — Silent\n\nBody only.\n")
        from govern.decisions import Grammar
        traps = Grammar("em-dash", [], text.Markers("t")).traps(
            w.root / "projects/alpha/working-files/traps.md")
        self.assertEqual(traps[0].bites, "first line continues here.")
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertIn("T-2 has no **Bites when:**", out)
        self.assertNotIn("T-1 has no", out)

    def test_stale_references(self):
        w = self.ws(extra='\n[checks.stale-references]\nnames = [{ text = "OldRules", now = "config" }]\n')
        w.write("governance/notes.md", fm() + "Limits are in OldRules.\n")
        code, out, _ = w.run("check")
        self.assertIn("governance/notes.md: mentions 'OldRules' 1× — now config", out)

    def test_unknown_project_hint(self):
        code, _, err = self.ws().run("show", "--project", "nope", "W-1")
        self.assertIn("'workspace' for the workspace log", err)


class UnlimitedDeclaration(Base):
    """"0 means no limit" is declared per param (`unlimited=`), not guessed from any int param
    that happens to default to 0 — `agents.max_turns` declares it; an extension's own ceiling
    that defaults to 0 for an unrelated reason still owes a reason once raised."""

    def test_max_turns_default_zero_is_still_exempt(self):
        w = self.ws(extra="\n[checks.agents]\nmax_turns = 100\n")
        code, out, _ = w.run("check")
        self.assertEqual(code, 0, out)

    def test_an_unrelated_zero_default_param_still_needs_a_reason_once_raised(self):
        from govern.findings import Findings

        @manifest.check("ext-ceiling", scope="workspace", since="0.2.1", origin="test-ext",
                        summary="An extension ceiling that happens to default to 0.",
                        question="q", rationale="r",
                        params={"cap": manifest.Param(
                            "int", 0, "Not 'no limit' — just an extension that starts at 0",
                            looser="higher")})
        def ext_ceiling(ctx, params):
            return Findings()

        self.addCleanup(manifest.forget, "test-ext")
        w = self.ws(extra='\n[checks.ext-ceiling]\ncap = 5\n')
        code, _, err = w.run("check")
        self.assertEqual(code, 2)
        self.assertIn("[checks.ext-ceiling] loosens cap past the engine default without a "
                      "'reason'", err)
        (self.tmp / "b").mkdir()
        w2 = Workspace(self.tmp / "b", extra='\n[checks.ext-ceiling]\ncap = 5\n'
                       'reason = "raised for this test"\n')
        code, out, _ = w2.run("check")
        self.assertEqual(code, 0, out)


def make_source(where: Path, tags: tuple) -> Path:
    """A git repository shaped like the engine's release source, tagged with each version (the
    engine's __version__ rewritten to match, as a real release would carry it)."""
    src = where / "source"
    git = lambda *a: subprocess.run(
        ["git", "-C", str(src), "-c", "user.email=t@t", "-c", "user.name=t",
         "-c", "tag.gpgSign=false", "-c", "commit.gpgSign=false", *a],
        check=True, capture_output=True)
    shutil.copytree(ENGINE / "govern", src / "engine" / "govern",
                    ignore=shutil.ignore_patterns("__pycache__"))
    git("init", "-q")
    init = src / "engine" / "govern" / "__init__.py"
    original = init.read_text(encoding="utf-8")
    for version in tags:
        wtext(init, original.replace(f'__version__ = "{__version__}"', f'__version__ = "{version}"'))
        git("add", "-A")
        git("commit", "-qm", version, "--allow-empty")
        git("tag", f"v{version}")
    return src


class Notice(Base):
    def test_release_at_the_source_is_announced_once_checked(self):
        src = make_source(self.tmp, (__version__, "0.9.0"))
        w = self.ws()
        cfg = w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            "schema = 1", f'schema = 1\nsource = "{src.as_posix()}"', 1))
        os.environ.pop(notice.NO_UPDATE_CHECK, None)
        code, _, err = w.run("check")
        self.assertIn("context-gate 0.9.0 available", err)
        self.assertTrue((w.home / ".cache" / layout.TOOL / "releases.json").is_file())
        os.environ["CONTEXT_GATE_NO_UPDATE_CHECK"] = "1"
        self.addCleanup(os.environ.pop, "CONTEXT_GATE_NO_UPDATE_CHECK", None)
        (w.home / ".cache" / layout.TOOL / "releases.json").unlink()
        code, _, err = w.run("check")
        self.assertNotIn("available", err)
        self.assertFalse((w.home / ".cache" / layout.TOOL / "releases.json").exists())


class InstallReportAndUpgrade(Base):
    def project(self):
        w = self.ws()
        cfg = self.tmp / "config.toml"
        shutil.move(str(w.root / CFG), str(cfg))
        (w.root / layout.GOV_DIR).rmdir()
        # An old gate that reports one finding the engine also reports, and one it does not.
        w.write("governance/gate.py", "print('  warn   alpha: something only the old gate says')\n"
                                      "raise SystemExit(0)\n")
        w.write("governance/test_gate.py", "# tests of the old gate\n")
        return w, cfg

    def test_install_writes_report_and_retires(self):
        w, cfg = self.project()
        w.write("projects/alpha/working-files/traps.md", fm("working", status="active")
                + "\n## T-1 — Silent\n\nBody.\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = installer.main(["install", "--root", str(w.root), "--config", str(cfg),
                                   "--entrypoint", "governance/gate.py",
                                   "--retire", "governance/test_gate.py"])
        self.assertEqual(code, 0, out.getvalue())
        self.assertFalse((w.root / "governance/test_gate.py").exists())
        rep = (w.root / layout.GOV_DIR / "install-report.md").read_text(encoding="utf-8")
        self.assertIn("### `trap-entries`", rep)
        self.assertIn("something only the old gate says", rep.split("## No longer reported")[1])
        self.assertIn("decision-log: error", rep)
        with contextlib.redirect_stdout(io.StringIO()):
            installer.main(["uninstall", "--root", str(w.root)])
        self.assertTrue((w.root / "governance/test_gate.py").is_file())

    def test_upgrade_pins_this_engine(self):
        w, cfg = self.project()
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(f'engine = "{__version__}"',
                                                            f'engine = "{SERIES}"'))
        with contextlib.redirect_stdout(io.StringIO()):
            installer.main(["install", "--root", str(w.root), "--config", str(cfg), "--no-report"])
            code = installer.main(["upgrade", "--root", str(w.root), "--no-report"])
        self.assertEqual(code, 0)
        self.assertIn(f'engine = "{__version__}"',
                      (w.root / CFG).read_text(encoding="utf-8"))
        self.assertIn(f'engine = "{__version__}"',
                      (w.root / layout.MANIFEST).read_text(encoding="utf-8"))

    def test_bin_upgrade_fetches_the_newest_release_and_upgrades(self):
        newer = f"{SERIES}.99"
        src = make_source(self.tmp, (__version__, newer))
        w, cfg = self.project()
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            "schema = 1", f'schema = 1\nsource = "{src.as_posix()}"', 1))
        with contextlib.redirect_stdout(io.StringIO()):
            installer.main(["install", "--root", str(w.root), "--config", str(cfg), "--no-report"])
        env = {k: v for k, v in os.environ.items() if k != "GOVERN_ENGINE"}
        env["HOME"] = str(w.home)
        res = subprocess.run([sys.executable, str(w.root / layout.GOV_DIR / "bin" / "upgrade")],
                             env=env, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        self.assertIn(f"upgraded context-gate {__version__} -> {newer}", res.stdout)
        self.assertIn(f'engine = "{newer}"', (w.root / CFG).read_text(encoding="utf-8"))
        self.assertTrue((w.root / layout.GOV_DIR / "upgrade-report.md").is_file())

    def test_bin_upgrade_runs_the_fetched_engine_from_a_directory_holding_another(self):
        # `python3 -m` puts the working directory ahead of PYTHONPATH: run from a directory that
        # holds a `govern/` package (this engine's own source tree), the upgrade loaded that one
        # and reported `X -> X` instead of running the fetched release.
        newer = f"{SERIES}.99"
        src = make_source(self.tmp, (__version__, newer))
        w, cfg = self.project()
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            "schema = 1", f'schema = 1\nsource = "{src.as_posix()}"', 1))
        with contextlib.redirect_stdout(io.StringIO()):
            installer.main(["install", "--root", str(w.root), "--config", str(cfg), "--no-report"])
        env = {k: v for k, v in os.environ.items() if k != "GOVERN_ENGINE"}
        env["HOME"] = str(w.home)
        res = subprocess.run([sys.executable, str(w.root / layout.GOV_DIR / "bin" / "upgrade")],
                             env=env, capture_output=True, text=True,
                             cwd=Path(installer.__file__).resolve().parent.parent)
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        self.assertIn(f"upgraded context-gate {__version__} -> {newer}", res.stdout)


class UpgradeReportAndPluginOptIn(Base):
    """The upgrade report when both runs used one engine, the report's settings format, and the
    plugin opt-in recorded on install and reversed on uninstall."""

    def project(self):
        w = self.ws()
        cfg = self.tmp / "config.toml"
        shutil.move(str(w.root / CFG), str(cfg))
        (w.root / layout.GOV_DIR).rmdir()
        return w, cfg

    def test_upgrade_report_says_when_both_runs_used_one_engine(self):
        w, cfg = self.project()
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(f'engine = "{__version__}"',
                                                            f'engine = "{SERIES}"'))
        shutil.copytree(ENGINE / "govern", layout.engines_dir(w.home) / __version__ / "govern",
                        ignore=shutil.ignore_patterns("__pycache__"))
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(w.home)
        self.addCleanup(lambda: os.environ.__setitem__("HOME", old_home) if old_home
                        else os.environ.pop("HOME", None))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            installer.main(["install", "--root", str(w.root), "--config", str(cfg), "--no-report"])
            installer.main(["upgrade", "--root", str(w.root)])
        self.assertIn(f"upgraded context-gate {SERIES} (ran {__version__}) -> {__version__}",
                      out.getvalue())
        rep = (w.root / layout.GOV_DIR / "upgrade-report.md").read_text(encoding="utf-8")
        self.assertIn(f"Both runs used engine {__version__}", rep)
        self.assertNotIn("## What changed in the engine", rep)   # same engine: nothing new
        self.assertIn(f"| Engine | {__version__} | {__version__} |", rep)

    def test_report_settings_use_explain_format(self):
        w, cfg = self.project()
        with contextlib.redirect_stdout(io.StringIO()):
            installer.main(["install", "--root", str(w.root), "--config", str(cfg)])
        rep = (w.root / layout.GOV_DIR / "install-report.md").read_text(encoding="utf-8")
        self.assertIn('statuses = ["locked", "provisional", "superseded"]', rep)
        self.assertNotIn("['locked'", rep)

    def test_plugin_opt_in_is_recorded_and_reversed(self):
        w, cfg = self.project()
        w.write(".claude/settings.json", '{\n  "hooks": {}\n}\n')
        before = (w.root / ".claude/settings.json").read_text(encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            installer.main(["install", "--root", str(w.root), "--config", str(cfg),
                            "--no-report", "--enable-plugin", "context-gate@skills-dir"])
        settings = json.loads((w.root / ".claude/settings.json").read_text(encoding="utf-8"))
        self.assertIs(settings["enabledPlugins"]["context-gate@skills-dir"], True)
        self.assertIn("[[plugin]]", (w.root / layout.MANIFEST).read_text(encoding="utf-8"))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(installer.main(["uninstall", "--root", str(w.root)]), 0)
        self.assertEqual(json.loads((w.root / ".claude/settings.json").read_text(encoding="utf-8")),
                         json.loads(before))

    def test_enable_plugin_on_an_existing_install_keeps_a_previous_value(self):
        w, cfg = self.project()
        w.write(".claude/settings.json",
                '{"enabledPlugins": {"context-gate@skills-dir": false}}')
        with contextlib.redirect_stdout(io.StringIO()):
            installer.main(["install", "--root", str(w.root), "--config", str(cfg), "--no-report"])
            installer.main(["enable-plugin", "--root", str(w.root),
                            "--id", "context-gate@skills-dir"])
            installer.main(["uninstall", "--root", str(w.root)])
        settings = json.loads((w.root / ".claude/settings.json").read_text(encoding="utf-8"))
        self.assertIs(settings["enabledPlugins"]["context-gate@skills-dir"], False)


class Standard(Base):
    """The standard: pointers for replaced decisions, topics, links, and overrides that carry
    their reason."""

    def log(self, w):
        return w.root / "projects/alpha/DECISIONS.md"

    def test_pointer_has_no_body_and_resolves(self):
        w = self.ws()
        log = self.log(w)
        wtext(log, log.read_text(encoding="utf-8") + entry("A", 101)
              + "\n## A-102 — Replaced by A-101\n"
              + "\n## A-103 — Replaced by A-199\n"
              + "\n## A-104 — Replaced by A-101\n\nOld text that misleads.\n")
        w.index()
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertNotIn("A-102", out)
        self.assertIn("A-103 points to A-199, which is not in this log", out)
        self.assertIn("A-104 is a pointer to A-101, so it carries no body", out)
        index = log.read_text(encoding="utf-8")
        self.assertIn("| A-102 | *replaced* | → A-101 |", index)

    def test_superseded_entry_is_told_to_rewrite_or_point(self):
        w = self.ws(extra="")
        cfg = w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            'statuses = ["locked", "provisional", "superseded"]', ""))
        log = self.log(w)
        wtext(log, log.read_text(encoding="utf-8") + entry("A", 101, status="superseded"))
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertIn("A-101 is superseded — rewrite it in place", out)

    def test_index_groups_by_topic(self):
        w = self.ws()
        log = self.log(w)
        wtext(log, log.read_text(encoding="utf-8")
              + entry("A", 101, extra="\n**Topic:** Sync\n")
              + entry("A", 102, extra="\n**Topic:** Sync\n"))
        w.index()
        text = log.read_text(encoding="utf-8")
        self.assertIn("**Sync**", text)
        self.assertLess(text.index("| A-100 |"), text.index("**Sync**"))

    def test_padded_and_unpadded_ids_are_one_id(self):
        w = self.ws()
        log = self.log(w)
        wtext(log, log.read_text(encoding="utf-8").replace("## A-100 —", "## A-0100 —"))
        code, out, _ = w.run("show", "--project", "alpha", "A-100")
        self.assertEqual(code, 0, out)
        self.assertIn("## A-0100 — Title 100", out)

    def test_doc_registry_is_grouped_and_links_from_the_index(self):
        w = self.ws()
        cfg = w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            'project = [{ file = "DECISIONS.md", id = "decision-index" }]',
            'project = [{ file = "DECISIONS.md", id = "decision-index" }, '
            '{ file = "INDEX.md", id = "doc-registry" }]'))
        w.write("projects/alpha/INDEX.md", fm() + "<!-- t:generated:start id=doc-registry -->\n"
                "<!-- t:generated:end id=doc-registry -->\n")
        w.write("projects/alpha/working-files/plan.md", fm("working", status="active") + "x\n")
        w.index()
        text = (w.root / "projects/alpha/INDEX.md").read_text(encoding="utf-8")
        self.assertIn("**Control**", text)
        self.assertIn("**Working**", text)
        self.assertIn("[`working-files/plan.md`](working-files/plan.md) | testing — _active_ |", text)
        code, out, _ = w.run("check")
        self.assertEqual(code, 0, out)
        w.write("projects/alpha/orphan.md", fm())
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertIn("alpha/orphan.md: not reachable from INDEX.md's doc registry", out)

    def test_broken_body_link(self):
        w = self.ws()
        w.write("projects/alpha/note.md", fm() + "See [the plan](plan.md) and `[x](nope.md)`.\n"
                "```\n[fenced](gone.md)\n```\n")
        code, out, _ = w.run("check")
        self.assertIn("alpha/note.md: link target does not exist: plan.md", out)
        self.assertNotIn("nope.md", out)
        self.assertNotIn("gone.md", out)

    def _git(self, root: Path, *args: str) -> None:
        subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t",
                        "-c", "tag.gpgSign=false", "-c", "commit.gpgSign=false", *args],
                       check=True, capture_output=True)

    def test_broken_link_to_a_deleted_file_names_the_commit(self):
        w = self.ws()
        self._git(w.root, "init", "-q")
        self._git(w.root, "add", "-A")
        self._git(w.root, "commit", "-qm", "initial")
        w.write("projects/alpha/plan.md", fm() + "# Plan\n")
        self._git(w.root, "add", "-A")
        self._git(w.root, "commit", "-qm", "add plan")
        (w.root / "projects/alpha/plan.md").unlink()
        self._git(w.root, "add", "-A")
        self._git(w.root, "commit", "-qm", "remove plan")
        res = subprocess.run(["git", "-C", str(w.root), "log", "-1", "--diff-filter=D",
                              "--format=%h", "--", "projects/alpha/plan.md"],
                             capture_output=True, text=True)
        sha = res.stdout.strip()
        self.assertTrue(sha)
        w.write("projects/alpha/note.md", fm() + "See [the plan](plan.md).\n")
        code, out, _ = w.run("check")
        self.assertIn(f"alpha/note.md: link target does not exist: plan.md — deleted in {sha}; "
                      f"link the last commit that had it ({sha}^:projects/alpha/plan.md) instead "
                      f"of removing the link", out)

    def test_broken_link_that_never_existed_has_no_hint(self):
        w = self.ws()
        self._git(w.root, "init", "-q")
        self._git(w.root, "add", "-A")
        self._git(w.root, "commit", "-qm", "initial")
        w.write("projects/alpha/note.md", fm() + "See [ghost](ghost.md).\n")
        code, out, _ = w.run("check")
        self.assertIn("alpha/note.md: link target does not exist: ghost.md", out)
        self.assertNotIn("deleted in", out)

    def test_broken_link_hint_is_cached_per_run(self):
        from unittest import mock
        w = self.ws()
        self._git(w.root, "init", "-q")
        self._git(w.root, "add", "-A")
        self._git(w.root, "commit", "-qm", "initial")
        w.write("projects/alpha/plan.md", fm() + "# Plan\n")
        self._git(w.root, "add", "-A")
        self._git(w.root, "commit", "-qm", "add plan")
        (w.root / "projects/alpha/plan.md").unlink()
        self._git(w.root, "add", "-A")
        self._git(w.root, "commit", "-qm", "remove plan")
        w.write("projects/alpha/note.md",
               fm() + "See [the plan](plan.md) and [it again](plan.md).\n")
        from govern.checks import links as links_mod
        with mock.patch.object(links_mod, "git", wraps=links_mod.git) as spy:
            code, out, _ = w.run("check")
        self.assertEqual(spy.call_count, 1)

    def test_skill_without_description(self):
        w = self.ws()
        w.write(".claude/skills/thing/SKILL.md", "---\nname: thing\n---\nbody\n")
        code, out, _ = w.run("check")
        self.assertIn("SKILL.md: skill is missing 'description'", out)

    def test_format_override_needs_a_reason(self):
        w = self.ws()
        code, out, _ = w.run("check")
        self.assertIn('[dialect] decision_heading = "em-dash" differs from the standard', out)
        cfg = w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            'markers = "t"', 'markers = "t"\n\n[dialect.reasons]\ndecision_heading = "old logs"'))
        code, out, _ = w.run("check")
        self.assertNotIn("differs from the standard", out)

    def test_widened_list_names_only_the_loosening_side(self):
        # The fixture's own [checks.decision-log] re-allows 'superseded':
        # statuses = ["locked", "provisional", "superseded"], no reason. It also drops
        # 'deferred', which tightens — a tightening must never be named as loosening.
        w = self.ws()
        code, out, _ = w.run("check")
        self.assertIn("[checks.decision-log] statuses adds 'superseded' — loosens past what "
                      "the project inherits with no reason (say why in reasons.statuses)",
                      out)
        self.assertNotIn("deferred", out)
        wtext(w.root / CFG, (w.root / CFG).read_text(encoding="utf-8").replace(
            'statuses = ["locked", "provisional", "superseded"]',
            'statuses = ["locked", "provisional", "superseded"]\n\n[checks.decision-log.reasons]'
            '\nstatuses = "our older entries predate the standard"'))
        code, out, _ = w.run("check")
        self.assertNotIn("statuses adds", out)

    def test_table_reason_does_not_silence_a_list_loosening(self):
        # A reason written for one setting (max_words) must never silently excuse another
        # (statuses re-allowing 'superseded') — only a per-setting reasons.statuses does.
        w = self.ws()
        cfg = w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            'statuses = ["locked", "provisional", "superseded"]',
            'statuses = ["locked", "provisional", "superseded"]\n'
            'max_words = 400\nreason = "measured elsewhere"'))
        code, out, _ = w.run("check")
        self.assertIn("[checks.decision-log] statuses adds 'superseded'", out)
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            'reason = "measured elsewhere"',
            'reason = "measured elsewhere"\n\n[checks.decision-log.reasons]\n'
            'statuses = "our older entries predate the standard"'))
        code, out, _ = w.run("check")
        self.assertNotIn("statuses adds", out)

    def test_emptied_required_list_needs_a_reason(self):
        w = self.ws(extra='required_fields = ["Rule"]\n')
        code, out, _ = w.run("check")
        self.assertIn("[checks.decision-log] required_fields drops 'Why' — loosens past what "
                      "the project inherits with no reason (say why in reasons.required_fields)",
                      out)
        wtext(w.root / CFG, (w.root / CFG).read_text(encoding="utf-8").replace(
            'required_fields = ["Rule"]',
            'required_fields = ["Rule"]\nreason = "traps carry no Why field"'))
        code, out, _ = w.run("check")
        self.assertIn("required_fields drops", out)   # a table-level reason no longer covers it
        wtext(w.root / CFG, (w.root / CFG).read_text(encoding="utf-8").replace(
            'reason = "traps carry no Why field"',
            'reason = "traps carry no Why field"\n\n[checks.decision-log.reasons]\n'
            'required_fields = "traps carry no Why field"'))
        code, out, _ = w.run("check")
        self.assertNotIn("required_fields drops", out)

    def test_emptying_an_anything_goes_list_needs_a_reason(self):
        w = self.ws(extra='\n[checks.doc-frontmatter]\nworking_statuses = []\n')
        code, out, _ = w.run("check")
        self.assertIn("[checks.doc-frontmatter] working_statuses drops 'active', 'held', "
                      "'planned', 'complete', 'superseded' — loosens past what the project "
                      "inherits with no reason (say why in reasons.working_statuses)", out)
        wtext(w.root / CFG, (w.root / CFG).read_text(encoding="utf-8").replace(
            "working_statuses = []",
            'working_statuses = []\n\n[checks.doc-frontmatter.reasons]\n'
            'working_statuses = "this project writes free-text status notes"'))
        code, out, _ = w.run("check")
        self.assertNotIn("working_statuses drops", out)

    def test_repo_alias_and_default_subcommand(self):
        w = self.ws()
        code, out, _ = w.run("--repo", "alpha")
        self.assertEqual(code, 0, out)
        self.assertIn("[OK] alpha", out)
        code, out, _ = w.run("next-id", "--repo", "alpha")
        self.assertEqual(out.strip(), "A-101")

    def test_registry_well_formed(self):
        w = self.ws(alpha_extra='', ws_range="1-99")
        reg = w.root / "projects.toml"
        wtext(reg, reg.read_text(encoding="utf-8").replace('tier = "full"', 'tier = "gold"'))
        code, out, _ = w.run("check")
        self.assertIn("registry: 'alpha' tier 'gold' is not one of", out)


AGENT = ("---\nname: helper\ndescription: d\nmodel: opus\neffort: low\nomitClaudeMd: true\n"
         "{extra}---\n{body}\n")


class Defaults(Base):
    """The engine's neutral defaults for agents and working files: stricter house rules belong
    in a principles profile, not in the engine."""

    def commit_all(self, w: "Workspace", when: str | None = None) -> None:
        env = {**os.environ}
        if when:
            env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = when
        for cmd in (["init", "-q"], ["add", "-A"],
                    ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"]):
            subprocess.run(["git", "-C", str(w.root), *cmd], check=True, env=env)

    # ---------------------------------------------------------------- agents

    def test_missing_omit_claude_md_is_a_warning_by_default(self):
        w = self.ws()
        w.write(".claude/agents/helper.md", AGENT.format(extra="", body="body")
                .replace("omitClaudeMd: true\n", ""))
        code, out, _ = w.run("check")
        self.assertEqual(code, 0, out)
        self.assertIn("warn   agents/helper.md: frontmatter missing 'omitClaudeMd'", out)

    def test_warn_keys_empty_makes_it_an_error(self):
        w = self.ws(extra="\n[checks.agents]\nwarn_keys = []\n")
        w.write(".claude/agents/helper.md", AGENT.format(extra="", body="body")
                .replace("omitClaudeMd: true\n", ""))
        code, out, _ = w.run("check")
        self.assertEqual(code, 1)
        self.assertIn("ERROR  agents/helper.md: frontmatter missing 'omitClaudeMd'", out)

    def test_max_turns_optional_by_default(self):
        w = self.ws()
        w.write(".claude/agents/helper.md", AGENT.format(extra="", body="body"))
        code, out, _ = w.run("check")
        self.assertEqual(code, 0, out)

    def test_require_max_turns_flags_a_missing_cap(self):
        w = self.ws(extra="\n[checks.agents]\nrequire_max_turns = true\n")
        w.write(".claude/agents/helper.md", AGENT.format(extra="", body="body"))
        code, out, _ = w.run("check")
        self.assertIn("agents/helper.md: no maxTurns — set a cap high enough that a properly "
                      "performing agent never hits it, but a runaway agent is stopped for "
                      "review", out)

    def test_max_turns_zero_means_no_ceiling(self):
        w = self.ws()
        w.write(".claude/agents/helper.md",
                AGENT.format(extra="maxTurns: 5000\n", body="body"))
        code, out, _ = w.run("check")
        self.assertEqual(code, 0, out)

    def test_positive_ceiling_still_enforced(self):
        w = self.ws(extra="\n[checks.agents]\nmax_turns = 50\n")
        w.write(".claude/agents/helper.md",
                AGENT.format(extra="maxTurns: 100\n", body="body"))
        code, out, _ = w.run("check")
        self.assertIn("agents/helper.md: maxTurns 100 exceeds agents max_turns=50", out)

    def test_max_turns_must_be_a_positive_integer(self):
        w = self.ws()
        w.write(".claude/agents/helper.md", AGENT.format(extra="maxTurns: 0\n", body="body"))
        code, out, _ = w.run("check")
        self.assertIn("agents/helper.md: maxTurns '0' is not a positive integer", out)

    def test_malformed_max_turns_warns_not_errors(self):
        # A warning, not an error: an existing project carrying one must not turn red on
        # upgrade.
        for i, extra in enumerate(("maxTurns: 0\n", "maxTurns: -5\n", "maxTurns: soon\n")):
            (self.tmp / f"case{i}").mkdir()
            w = Workspace(self.tmp / f"case{i}")
            w.write(".claude/agents/helper.md", AGENT.format(extra=extra, body="body"))
            code, out, _ = w.run("check")
            self.assertEqual(code, 0, out)
            self.assertIn("warn   agents/helper.md: maxTurns", out)
            self.assertNotIn("ERROR  agents/helper.md: maxTurns", out)

    def test_malformed_max_turns_with_prose_turn_count_reports_once(self):
        w = self.ws()
        w.write(".claude/agents/helper.md",
                AGENT.format(extra="maxTurns: 0\n", body="Stop after 10 turns."))
        code, out, _ = w.run("check")
        self.assertIn("maxTurns '0' is not a positive integer", out)
        self.assertNotIn("no maxTurns to match", out)

    def test_agent_prose_must_match_by_default(self):
        w = self.ws()
        w.write(".claude/agents/helper.md",
                AGENT.format(extra="maxTurns: 10\n", body="Stop well before 10 turns."))
        code, out, _ = w.run("check")
        self.assertEqual(code, 0, out)
        w.write(".claude/agents/helper.md",
                AGENT.format(extra="maxTurns: 10\n", body="Stop well before 20 turns."))
        code, out, _ = w.run("check")
        self.assertIn("agents/helper.md: prose says 20 turns, frontmatter says 10", out)

    # ---------------------------------------------------------- working files

    def test_finished_files_warn_by_default(self):
        w = self.ws()
        w.write("projects/alpha/working-files/plan.md",
                fm("working", status="complete") + "body\n")
        self.commit_all(w, "2020-01-01T00:00:00")
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertEqual(code, 0, out)
        self.assertIn("warn   ", out)
        self.assertIn("plan.md: status 'complete'", out)

    def test_never_committed_finished_file_is_flagged_instead_of_aged(self):
        w = self.ws()
        self.commit_all(w)
        w.write("projects/alpha/working-files/plan.md",
                fm("working", status="complete") + "body\n")
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertIn("plan.md is finished but was never committed — commit it once so git "
                      "keeps it, then delete it", out)
        self.assertNotIn("last touched", out)

    def test_max_days_zero_reports_a_committed_file_at_once(self):
        w = self.ws(extra="\n[checks.finished-files]\nmax_days = 0\n")
        w.write("projects/alpha/working-files/plan.md",
                fm("working", status="complete") + "body\n")
        self.commit_all(w)
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertIn("plan.md: status 'complete', last touched 0d ago "
                      "(finished-files max_days=0)", out)

    def test_working_file_count_warns_above_twelve(self):
        w = self.ws()
        for i in range(13):
            w.write(f"projects/alpha/working-files/f{i}.md",
                    fm("working", status="active") + "x\n")
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertEqual(code, 0, out)
        self.assertIn("warn   alpha: 13 files in working-files/ exceeds "
                      "working-file-count max_files=12", out)


class AgentWorktrees(Base):
    """An agent worktree under `.claude/worktrees/` that a checkout does not ignore is swept into
    its next `git add -A`. Each checkout's own repo is asked, never the workspace root's."""

    NOT_IGNORED = "does not ignore .claude/worktrees/; an agent worktree there is swept into"

    def git_init(self, where: Path, ignore: str | None = None) -> None:
        where.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-C", str(where), "init", "-q"], check=True)
        if ignore is not None:
            wtext(where / ".gitignore", ignore)

    def member_ws(self, *, root_ignore: str, alpha_ignore: str | None) -> "Workspace":
        # The root ignores the member's whole directory, the way a workspace root keeps its
        # members out of its own commits: asking the root about a member path would answer
        # "ignored" whatever the member's own .gitignore says.
        w = self.ws()
        self.git_init(w.root, root_ignore)
        self.git_init(w.root / "alpha", alpha_ignore)
        w.write(".claude/agents/helper.md", AGENT.format(extra="", body="body"))
        return w

    def test_member_that_does_not_ignore_worktrees_warns(self):
        w = self.member_ws(root_ignore="alpha/\n.claude/worktrees/\n", alpha_ignore=None)
        code, out, _ = w.run("check")
        self.assertEqual(code, 0, out)
        self.assertIn(f"alpha: 'alpha' {self.NOT_IGNORED}", out)
        self.assertEqual(out.count(self.NOT_IGNORED), 1, out)

    def test_member_that_ignores_worktrees_is_clean(self):
        w = self.member_ws(root_ignore="alpha/\n.claude/worktrees/\n",
                           alpha_ignore=".claude/worktrees/\n")
        code, out, _ = w.run("check")
        self.assertEqual(code, 0, out)
        self.assertNotIn(self.NOT_IGNORED, out)
        self.assertNotIn("could not ask git", out)

    def test_workspace_root_that_does_not_ignore_worktrees_warns(self):
        w = self.member_ws(root_ignore="alpha/\n", alpha_ignore=".claude/worktrees/\n")
        code, out, _ = w.run("check")
        self.assertIn(f"workspace: '.' {self.NOT_IGNORED}", out)
        self.assertEqual(out.count(self.NOT_IGNORED), 1, out)

    def test_member_with_its_own_agents_fires_without_root_agents(self):
        w = self.ws()
        self.git_init(w.root, "alpha/\n")
        self.git_init(w.root / "alpha")
        w.write("alpha/.claude/agents/helper.md", AGENT.format(extra="", body="body"))
        code, out, _ = w.run("check")
        self.assertIn(f"alpha: 'alpha' {self.NOT_IGNORED}", out)
        self.assertNotIn("workspace: '.'", out)

    def test_no_agents_dir_no_finding(self):
        w = self.ws()
        self.git_init(w.root)
        self.git_init(w.root / "alpha")
        code, out, _ = w.run("check")
        self.assertNotIn(self.NOT_IGNORED, out)
        self.assertNotIn("could not ask git", out)

    def test_git_failure_is_a_warning_naming_it_never_clean(self):
        w = self.member_ws(root_ignore="alpha/\n", alpha_ignore=None)
        wtext(w.home / ".gitconfig", "[bad\n")    # every git call now fails: exit 128
        code, out, _ = w.run("check")
        self.assertEqual(code, 0, out)
        self.assertIn("alpha: could not ask git whether 'alpha' ignores .claude/worktrees/ "
                      "(fatal: bad config", out)
        self.assertNotIn(self.NOT_IGNORED, out)

    def test_a_root_that_is_not_a_repo_is_skipped_its_members_are_not(self):
        w = self.ws()                  # a plain folder of repos, with agents at the top
        self.git_init(w.root / "alpha")
        w.write(".claude/agents/helper.md", AGENT.format(extra="", body="body"))
        code, out, _ = w.run("check")
        self.assertEqual(code, 0, out)
        self.assertNotIn("could not ask git", out)
        self.assertNotIn("workspace: '.'", out)
        self.assertIn(f"alpha: 'alpha' {self.NOT_IGNORED}", out)

    def test_repo_env_from_a_hook_never_redirects_the_question(self):
        # A pre-commit hook in a linked worktree exports GIT_DIR; `git -C alpha` would then ask
        # that repo, which here ignores the worktrees, instead of alpha's own.
        w = self.member_ws(root_ignore="alpha/\n.claude/worktrees/\n", alpha_ignore=None)
        other = self.tmp / "other"
        self.git_init(other)
        wtext(other / ".git" / "info" / "exclude", ".claude/worktrees/\n")
        old = os.environ.get("GIT_DIR")
        os.environ["GIT_DIR"] = str(other / ".git")
        try:
            code, out, _ = w.run("check")
        finally:
            if old is None:
                del os.environ["GIT_DIR"]
            else:
                os.environ["GIT_DIR"] = old
        self.assertIn(f"alpha: 'alpha' {self.NOT_IGNORED}", out)

    def test_a_global_excludes_file_does_not_hide_it(self):
        w = self.member_ws(root_ignore="alpha/\n.claude/worktrees/\n", alpha_ignore=None)
        excludes = w.home / "excludes"
        wtext(excludes, ".claude/worktrees/\n")
        wtext(w.home / ".gitconfig", f"[core]\n\texcludesFile = {excludes.as_posix()}\n")
        code, out, _ = w.run("check")
        self.assertIn(f"alpha: 'alpha' {self.NOT_IGNORED}", out)

    # A monorepo member: a directory with no repo of its own. Its agents' worktrees land at the
    # root repo's top, so the root's pass answers for it.

    def monorepo(self, root_ignore: str) -> "Workspace":
        w = self.ws()
        self.git_init(w.root, root_ignore)
        w.write("alpha/.claude/agents/helper.md", AGENT.format(extra="", body="body"))
        return w

    def test_monorepo_member_with_agents_fires_the_roots_pass_once(self):
        w = self.monorepo("")
        code, out, _ = w.run("check")
        self.assertIn(f"workspace: '.' {self.NOT_IGNORED}", out)
        self.assertEqual(out.count(self.NOT_IGNORED), 1, out)

    def test_monorepo_member_is_clean_when_the_root_ignores_worktrees(self):
        w = self.monorepo(".claude/worktrees/\n")
        code, out, _ = w.run("check")
        self.assertNotIn(self.NOT_IGNORED, out)
        self.assertNotIn("could not ask git", out)

    def test_monorepo_member_is_never_asked_about_its_own_path(self):
        # The root ignores only its own top-level worktrees, which is where they land; asking
        # about alpha/.claude/worktrees/ would warn about a directory nothing creates.
        w = self.monorepo("/.claude/worktrees/\n")
        code, out, _ = w.run("check")
        self.assertNotIn(self.NOT_IGNORED, out)

    def test_single_repo_is_asked_once(self):
        root, home = self.tmp / "single", self.tmp / "singlehome"
        home.mkdir()
        self.git_init(root)
        (root / CFG).parent.mkdir(parents=True, exist_ok=True)
        wtext(root / CFG, f"[governance]\nengine = \"{__version__}\"\nschema = 1\n")
        agent = root / ".claude" / "agents" / "helper.md"
        agent.parent.mkdir(parents=True)
        wtext(agent, AGENT.format(extra="", body="body"))
        run = lambda: Workspace.run(SimpleNamespace(root=root, home=home), "check")  # noqa: E731
        code, out, _ = run()       # red for its missing log; only this check matters here
        self.assertEqual(out.count(self.NOT_IGNORED), 1, out)
        self.assertIn(f"single: '.' {self.NOT_IGNORED}", out)
        wtext(root / ".gitignore", ".claude/worktrees/\n")
        code, out, _ = run()
        self.assertNotIn(self.NOT_IGNORED, out)
        self.assertNotIn("could not ask git", out)


class DecisionHistory(Base):
    """A rewritten decision entry keeps no history line."""

    def log(self, w):
        return w.root / "projects/alpha/DECISIONS.md"

    def test_bold_history_label_is_flagged(self):
        w = self.ws()
        log = self.log(w)
        wtext(log, log.read_text(encoding="utf-8")
              + entry("A", 101, extra="\n**Earlier:** the old rule required a Y field.\n"))
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertIn('alpha/DECISIONS.md: A-101 keeps history ("Earlier:") — a rewritten '
                      'entry states only today\'s rule; git keeps the old one', out)

    def test_plain_history_label_is_flagged(self):
        w = self.ws()
        log = self.log(w)
        wtext(log, log.read_text(encoding="utf-8")
              + entry("A", 101, extra="\nPreviously: the old rule.\n"))
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertIn('A-101 keeps history ("Previously:")', out)

    def test_history_label_is_case_insensitive(self):
        w = self.ws()
        log = self.log(w)
        wtext(log, log.read_text(encoding="utf-8")
              + entry("A", 101, extra="\nearlier: the old rule.\n"))
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertIn('A-101 keeps history ("Earlier:")', out)   # configured spelling, not the file's

    def test_history_label_inside_a_fence_is_not_flagged(self):
        w = self.ws()
        log = self.log(w)
        wtext(log, log.read_text(encoding="utf-8")
              + entry("A", 101, extra="\n```\nEarlier: example text, not history.\n```\n"))
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertNotIn("keeps history", out)

    def test_pointer_entries_are_unaffected(self):
        w = self.ws()
        log = self.log(w)
        wtext(log, log.read_text(encoding="utf-8") + entry("A", 101)
              + "\n## A-102 — Replaced by A-101\n")
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertNotIn("keeps history", out)

    def test_no_history_label_is_silent(self):
        w = self.ws()
        log = self.log(w)
        wtext(log, log.read_text(encoding="utf-8") + entry("A", 101))
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertNotIn("keeps history", out)

    def test_dropping_a_label_needs_a_reason(self):
        # Fewer labels flagged means more history text passes unnoticed — dropping one is
        # loosening.
        w = self.ws(extra='\n[checks.decision-history]\n'
                          'labels = ["Earlier", "Previously", "Formerly", "Was"]\n')
        code, out, _ = w.run("check")
        self.assertIn("[checks.decision-history] labels drops 'Superseded' — loosens past what "
                      "the project inherits with no reason (say why in reasons.labels)",
                      out)
        wtext(w.root / CFG, (w.root / CFG).read_text(encoding="utf-8").replace(
            'labels = ["Earlier", "Previously", "Formerly", "Was"]',
            'labels = ["Earlier", "Previously", "Formerly", "Was"]\n\n'
            '[checks.decision-history.reasons]\n'
            'labels = "\'Superseded\' collides with our status of the same name"'))
        code, out, _ = w.run("check")
        self.assertNotIn("labels drops", out)


class ProfileLayer(Base):
    """The shared profile between the standard and each project."""

    RULES = '''
[checks.writing-rules]
rules = [{ text = "colour", use = "color", ignore_case = true, why = "American English" }]
'''

    def make_profile(self, where: Path, settings: str = RULES) -> Path:
        where.mkdir(parents=True, exist_ok=True)
        wtext(where / "principles.toml", settings)
        wtext(where / "PRINCIPLES.md", "# Principles\n\n1. Measure, then claim.\n")
        return where

    def with_profile(self, source: str, extra: str = "") -> Workspace:
        w = self.ws(extra=extra)
        cfg = w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            "schema = 1", f'schema = 1\nprofile = "{source}"', 1))
        return w

    def test_profile_rules_apply_to_the_project_files(self):
        prof = self.make_profile(self.tmp / "prof")
        w = self.with_profile(prof.as_posix(), extra='''
[checks.writing-rules]
level = "error"
files = ["drafts/*.md"]
extend_rules = [{ text = "monster", use = "mob", level = "warn" }]
''')
        w.write("drafts/a.md", "The colour of the monster.\n")
        code, out, _ = w.run("check")
        self.assertIn("drafts/a.md: 1 × 'colour' (use 'color') — American English", out)
        self.assertIn("drafts/a.md: 1 × 'monster' (use 'mob')", out)
        code, out, _ = w.run("explain", "writing-rules")
        self.assertIn("(profile + project", out)

    def test_overriding_a_principle_needs_a_reason(self):
        prof = self.make_profile(self.tmp / "prof", "[checks.agents]\nmax_turns = 100\n")
        w = self.with_profile(prof.as_posix(), extra="\n[checks.agents]\nmax_turns = 50\n")
        code, out, _ = w.run("check")
        self.assertIn("[checks.agents] max_turns overrides the profile with no reason", out)
        cfg = w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            "max_turns = 50", 'max_turns = 50\nreason = "short tasks here"'))
        code, out, _ = w.run("check")
        self.assertNotIn("overrides the profile", out)

    def test_list_widening_is_relative_to_the_profile(self):
        # The fixture's project table always sets statuses = ["locked", "provisional",
        # "superseded"]; a profile narrower than that is what the project widens past.
        prof = self.make_profile(self.tmp / "prof", '[checks.decision-log]\nstatuses = '
                                 '["locked"]\nreason = "profile default"\n')
        w = self.with_profile(prof.as_posix())
        code, out, _ = w.run("check")
        self.assertIn("[checks.decision-log] statuses adds", out)
        self.assertIn("'provisional'", out)
        self.assertIn("'superseded'", out)

    def test_list_widening_is_silent_when_the_project_matches_the_profile(self):
        prof = self.make_profile(self.tmp / "prof", '[checks.decision-log]\nstatuses = '
                                 '["locked", "provisional", "superseded"]\n'
                                 'reason = "profile default"\n')
        w = self.with_profile(prof.as_posix())
        code, out, _ = w.run("check")
        self.assertNotIn("statuses adds", out)

    def test_list_widening_reports_once_not_also_as_a_profile_override(self):
        # A project loosening a list its profile set, with no reason, gets the list message
        # naming the values — not also the generic "overrides the profile" warning (one
        # warning per override).
        prof = self.make_profile(self.tmp / "prof", '[checks.decision-log]\nstatuses = '
                                 '["locked"]\nreason = "profile default"\n')
        w = self.with_profile(prof.as_posix())
        code, out, _ = w.run("check")
        self.assertIn("[checks.decision-log] statuses adds", out)
        self.assertNotIn("statuses overrides the profile with no reason", out)

    def test_profile_may_not_set_layout(self):
        prof = self.make_profile(self.tmp / "prof", "[projects]\ndocs = []\n")
        code, _, err = self.with_profile(prof.as_posix()).run("check")
        self.assertEqual(code, 2)
        self.assertIn("a profile holds [checks.*] and [dialect] only", err)

    def test_principles_document_and_local_override(self):
        prof = self.make_profile(self.tmp / "prof")
        w = self.with_profile(prof.as_posix())
        code, out, _ = w.run("principles")
        self.assertIn("Measure, then claim.", out)
        w.write(".context-gate/PRINCIPLES.md", "# Ours\n")
        code, out, _ = w.run("principles")
        self.assertEqual(out.strip(), "# Ours")

    def test_git_profile_is_pinned_fetched_once_and_cached(self):
        src = self.make_profile(self.tmp / "profrepo")
        git = lambda *a: subprocess.run(
            ["git", "-C", str(src), "-c", "user.email=t@t", "-c", "user.name=t",
             "-c", "tag.gpgSign=false", "-c", "commit.gpgSign=false", *a],
            check=True, capture_output=True)
        git("init", "-q")
        git("add", "-A")
        git("commit", "-qm", "principles")
        git("tag", "v1")
        url = f"file://{src.as_posix()}"
        code, _, err = self.with_profile(url).run("check")
        self.assertEqual(code, 2)
        self.assertIn("no pinned ref", err)
        shutil.rmtree(self.tmp / "ws")
        shutil.rmtree(self.tmp / "home")
        w = self.with_profile(f"{url}#v1")
        code, out, _ = w.run("principles")
        self.assertEqual(code, 0, out)
        from govern.profile import rmtree
        rmtree(src)                             # the source is gone; the cache is enough
        code, out, _ = w.run("principles")
        self.assertIn("Measure, then claim.", out)


class PreCommitPath(Base):
    """`check --project X --path DIR --history-from DIR`: a git pre-commit hook's shape (a hook
    that exports the staged tree to a temporary snapshot with no history of its own, then checks
    that snapshot with the checkout named for git history)."""

    def _git(self, root: Path, *args: str, env: dict | None = None) -> None:
        subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t",
                        "-c", "tag.gpgSign=false", "-c", "commit.gpgSign=false", *args],
                       check=True, capture_output=True, env=env)

    def _snapshot(self, w: "Workspace") -> Path:
        """A copy of alpha's governed checkout, the shape `git checkout-index` would leave: no
        `.git` of its own — a staged-tree snapshot carries no history."""
        snap = self.tmp / "snapshot"
        shutil.copytree(w.root / "projects/alpha", snap)
        return snap

    def test_finding_only_in_the_snapshot_is_reported(self):
        w = self.ws()
        snap = self._snapshot(w)
        wtext(snap / "orphan.md", fm(doc_type="manual"))
        code, out, _ = w.run("check", "--project", "alpha", "--path", str(snap))
        self.assertIn("alpha/orphan.md: doc_type 'manual' not in", out)

    def test_finding_only_in_the_working_tree_is_not_reported(self):
        w = self.ws()
        snap = self._snapshot(w)
        w.write("projects/alpha/orphan.md", fm(doc_type="manual"))
        code, out, _ = w.run("check", "--project", "alpha", "--path", str(snap))
        self.assertNotIn("orphan.md", out)

    def test_git_history_answers_come_from_history_from(self):
        w = self.ws()
        w.write("projects/alpha/working-files/plan.md",
               fm("working", status="complete") + "body\n")
        env = {**os.environ, "GIT_AUTHOR_DATE": "2020-01-01T00:00:00",
               "GIT_COMMITTER_DATE": "2020-01-01T00:00:00"}
        self._git(w.root, "init", "-q", env=env)
        self._git(w.root, "add", "-A", env=env)
        self._git(w.root, "commit", "-qm", "x", env=env)
        snap = self._snapshot(w)
        history = w.root / "projects/alpha"
        code, out, _ = w.run("check", "--project", "alpha", "--path", str(snap),
                             "--history-from", str(history))
        self.assertIn("plan.md: status 'complete'", out)   # unchanged: dated from the old commit
        plan = snap / "working-files" / "plan.md"
        wtext(plan, plan.read_text(encoding="utf-8") + "edit\n")
        code, out, _ = w.run("check", "--project", "alpha", "--path", str(snap),
                             "--history-from", str(history))
        self.assertNotIn("plan.md: status 'complete'", out)  # a staged-style edit: touched now

    def test_working_file_words_breach_stays_baselined_under_path(self):
        # A ratchet key for a project's own file must name the real, repo-relative path (never
        # the snapshot's), and `owns` must match it against the scope's real directory — so a
        # breach baselined from an ordinary run still reads as baselined, not new, under
        # `--path`. Built from the snapshot's own absolute path instead, and compared against
        # the snapshot, the key would make every pre-commit run report it as a new breach.
        w = self.ws(extra="\n[checks.doc-frontmatter]\nmax_working_words = 3\n")
        w.write("projects/alpha/working-files/notes.md",
               fm("working", status="active") + "word " * 10)
        code, out, _ = w.run("baseline", "--allow-raise")
        self.assertEqual(code, 0, out)
        baseline = json.loads((w.root / layout.BASELINE).read_text(encoding="utf-8"))
        key = "working_file_words:projects/alpha/working-files/notes.md"
        self.assertIn(key, baseline, baseline)
        snap = self._snapshot(w)
        history = w.root / "projects/alpha"
        code, out, _ = w.run("check", "--project", "alpha", "--path", str(snap),
                             "--history-from", str(history))
        self.assertEqual(code, 0, out)
        self.assertNotIn("new breach", out)

    def test_missing_path_is_exit_2(self):
        w = self.ws()
        missing = self.tmp / "nope"
        code, out, err = w.run("check", "--project", "alpha", "--path", str(missing))
        self.assertEqual(code, 2)
        self.assertIn(f"--path {missing} does not exist", err)

    def test_path_without_project_is_exit_2(self):
        w = self.ws()
        code, out, err = w.run("check", "--path", str(self.tmp / "snapshot"))
        self.assertEqual(code, 2)
        self.assertIn("--path needs --project", err)


BETA_PROJECT = ('\n[[project]]\nname = "beta"\ndir = "beta"\ntier = "full"\n'
                'governance = "projects/beta"\nid_prefix = "B"\nid_range = "700-799"\n')


class ProjectScopedWorkspaceChecks(Base):
    """`check --project X` also runs the workspace checks that are really about one project's
    files (doc-links, generated-blocks, ratchet), restricted to X — the shape a pre-commit hook
    needs."""

    def _git(self, root: Path, *args: str) -> None:
        subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t",
                        "-c", "tag.gpgSign=false", "-c", "commit.gpgSign=false", *args],
                       check=True, capture_output=True)

    def _two(self, **kw) -> Workspace:
        """A second governed project, `beta`, beside `alpha`."""
        w = self.ws(**kw)
        wtext(w.root / "projects.toml",
             (w.root / "projects.toml").read_text(encoding="utf-8") + BETA_PROJECT)
        w.write("projects/beta/DECISIONS.md",
               fm() + "# Decisions\n\n<!-- t:generated:start id=decision-index -->\n"
               "<!-- t:generated:end id=decision-index -->\n" + entry("B", 700))
        w.index()
        return w

    def _snapshot(self, w: Workspace, name: str) -> Path:
        """A copy of one project's own checkout, the shape `git checkout-index` would leave: no
        `.git` of its own — a staged-tree snapshot carries no history."""
        snap = self.tmp / f"snapshot-{name}"
        shutil.copytree(w.root / "projects" / name, snap)
        return snap

    # -------------------------------------------------------------------- doc-links

    def test_broken_link_in_x_is_reported_y_is_not(self):
        w = self._two()
        w.write("projects/alpha/notes.md", fm() + "See [gone](missing.md).\n")
        w.write("projects/beta/notes.md", fm() + "See [gone](missing.md).\n")
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertEqual(code, 1, out)
        self.assertIn("alpha/notes.md: link target does not exist: missing.md", out)
        self.assertNotIn("beta", out)

    def test_broken_link_under_path_is_reported_y_is_not(self):
        w = self._two()
        snap = self._snapshot(w, "alpha")
        wtext(snap / "notes.md", fm() + "See [gone](missing.md).\n")
        code, out, _ = w.run("check", "--project", "alpha", "--path", str(snap))
        self.assertEqual(code, 1, out)
        self.assertIn("alpha/notes.md: link target does not exist: missing.md", out)
        self.assertNotIn("beta", out)

    def test_link_leaving_the_snapshot_resolves_against_the_real_tree(self):
        # A project doc links up out of its own directory, to the workspace's shared
        # governance docs (`../../governance/...`). A snapshot's own
        # directory carries none of the real tree's layout around it, so the link has to be
        # resolved from where the file really lives, not from the snapshot's (temporary, and
        # differently placed) one.
        w = self._two()
        snap = self._snapshot(w, "alpha")
        wtext(snap / "notes.md", fm() + "See [decisions](../../governance/DECISIONS.md).\n")
        code, out, _ = w.run("check", "--project", "alpha", "--path", str(snap))
        self.assertEqual(code, 0, out)
        self.assertNotIn("link target does not exist", out)

    def test_link_leaving_the_snapshot_to_nothing_real_still_errors(self):
        w = self._two()
        snap = self._snapshot(w, "alpha")
        wtext(snap / "notes.md", fm() + "See [gone](../../governance/NOPE.md).\n")
        code, out, _ = w.run("check", "--project", "alpha", "--path", str(snap))
        self.assertEqual(code, 1, out)
        self.assertIn("alpha/notes.md: link target does not exist: "
                      "../../governance/NOPE.md", out)

    def test_deleted_link_hint_asks_history_from_not_root(self):
        w = self._two()
        alpha = w.root / "projects/alpha"
        self._git(w.root, "init", "-q")
        self._git(w.root, "add", "-A")
        self._git(w.root, "commit", "-qm", "initial")
        gone = alpha / "gone.md"
        wtext(gone, fm())
        self._git(w.root, "add", "-A")
        self._git(w.root, "commit", "-qm", "add gone.md")
        gone.unlink()
        self._git(w.root, "add", "-A")
        self._git(w.root, "commit", "-qm", "delete gone.md")
        sha = subprocess.run(["git", "-C", str(w.root), "log", "-1", "--format=%h", "--",
                              "projects/alpha/gone.md"], check=True, capture_output=True,
                             text=True).stdout.strip()
        snap = self._snapshot(w, "alpha")
        wtext(snap / "notes.md", fm() + "See [gone](gone.md).\n")
        code, out, _ = w.run("check", "--project", "alpha", "--path", str(snap),
                             "--history-from", str(alpha))
        self.assertIn(f"deleted in {sha}; link the last commit that had it ({sha}^:gone.md)", out)

    def test_deleted_link_hint_empty_without_history_from(self):
        w = self._two()
        alpha = w.root / "projects/alpha"
        self._git(w.root, "init", "-q")
        self._git(w.root, "add", "-A")
        self._git(w.root, "commit", "-qm", "initial")
        gone = alpha / "gone.md"
        wtext(gone, fm())
        self._git(w.root, "add", "-A")
        self._git(w.root, "commit", "-qm", "add gone.md")
        gone.unlink()
        self._git(w.root, "add", "-A")
        self._git(w.root, "commit", "-qm", "delete gone.md")
        snap = self._snapshot(w, "alpha")
        wtext(snap / "notes.md", fm() + "See [gone](gone.md).\n")
        code, out, _ = w.run("check", "--project", "alpha", "--path", str(snap))
        self.assertIn("link target does not exist: gone.md", out)
        self.assertNotIn("deleted in", out)

    # -------------------------------------------------------------------- generated-blocks

    def test_stale_block_in_x_is_reported_y_is_not(self):
        w = self._two()
        log = w.root / "projects/alpha/DECISIONS.md"
        wtext(log, log.read_text(encoding="utf-8") + entry("A", 101))
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertEqual(code, 1, out)
        self.assertIn("alpha/DECISIONS.md: generated block 'decision-index' is stale", out)
        self.assertNotIn("beta", out)

    def test_stale_block_under_path_is_reported_y_is_not(self):
        w = self._two()
        snap = self._snapshot(w, "alpha")
        log = snap / "DECISIONS.md"
        wtext(log, log.read_text(encoding="utf-8") + entry("A", 101))
        code, out, _ = w.run("check", "--project", "alpha", "--path", str(snap))
        self.assertEqual(code, 1, out)
        self.assertIn("alpha/DECISIONS.md: generated block 'decision-index' is stale", out)
        self.assertNotIn("beta", out)

    # -------------------------------------------------------------------- ratchet

    def test_ratchet_breach_in_x_reported_y_baseline_kept(self):
        w = self._two(extra="\n[checks.governed-doc-count]\nmax_docs = 0\n")
        code, _, _ = w.run("baseline", "--allow-raise")
        self.assertEqual(code, 0)
        before = json.loads((w.root / layout.BASELINE).read_text(encoding="utf-8"))
        self.assertEqual(before.get("governed_docs:alpha"), 1)
        self.assertEqual(before.get("governed_docs:beta"), 1)
        w.write("projects/alpha/extra.md", fm())
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertEqual(code, 1, out)
        self.assertIn("'governed_docs:alpha' grew to 2 (baseline 1)", out)
        # beta's own recorded breach, unchanged, is neither reported here (it is not this run's
        # business) nor lost (`check` never writes the baseline).
        self.assertNotIn("beta", out)
        after = json.loads((w.root / layout.BASELINE).read_text(encoding="utf-8"))
        self.assertEqual(after, before)

    def test_ratchet_breach_under_path_reported_y_baseline_kept(self):
        w = self._two(extra="\n[checks.governed-doc-count]\nmax_docs = 0\n")
        code, _, _ = w.run("baseline", "--allow-raise")
        self.assertEqual(code, 0)
        snap = self._snapshot(w, "alpha")
        wtext(snap / "extra.md", fm())
        code, out, _ = w.run("check", "--project", "alpha", "--path", str(snap),
                             "--history-from", str(w.root / "projects/alpha"))
        self.assertEqual(code, 1, out)
        self.assertIn("'governed_docs:alpha' grew to 2 (baseline 1)", out)
        self.assertNotIn("beta", out)

    # -------------------------------------------------------------------- unchanged full check

    def test_full_check_output_is_unchanged(self):
        w = self._two()
        code, out, _ = w.run("check")
        self.assertEqual(code, 0, out)
        self.assertIn("[OK] workspace", out)
        self.assertIn("[OK] alpha", out)
        self.assertIn("[OK] beta", out)
        self.assertIn("[OK] total", out)

    def test_full_check_still_fails_on_the_same_findings(self):
        w = self._two()
        w.write("projects/alpha/notes.md", fm() + "See [gone](missing.md).\n")
        log = w.root / "projects/beta/DECISIONS.md"
        wtext(log, log.read_text(encoding="utf-8") + entry("B", 701))
        code, out, _ = w.run("check")
        self.assertEqual(code, 1, out)
        self.assertIn("alpha/notes.md: link target does not exist: missing.md", out)
        self.assertIn("beta/DECISIONS.md: generated block 'decision-index' is stale", out)

    def test_example_workspace_shaped_full_check_is_unchanged(self):
        # A workspace decision log, plus two projects whose own docs link out to it
        # (`../../governance/...`). A full `check` (no `--project`, no `--path`) never touches
        # `ctx.snapshot`/`ctx.real_scope_dir`, so ownership and real-path mapping must be no-ops
        # here.
        w = self._two()
        w.write("projects/alpha/notes.md", fm() + "See [decisions](../../governance/DECISIONS.md).\n")
        w.write("projects/beta/notes.md", fm() + "See [decisions](../../governance/DECISIONS.md).\n")
        code, out, _ = w.run("check")
        self.assertEqual(code, 0, out)
        self.assertIn("[OK] workspace", out)
        self.assertIn("[OK] alpha", out)
        self.assertIn("[OK] beta", out)
        self.assertIn("[OK] total", out)

    def test_default_doc_excludes_do_not_change_a_full_check(self):
        w = self._two()
        w.write("projects/alpha/node_modules/pkg/README.md", "not a governed doc\n")
        code, out, _ = w.run("check")
        self.assertEqual(code, 0, out)
        self.assertNotIn("node_modules", out)


class TopicWords(Base):
    """A decision entry's word count excludes its own **Topic:** line, the same treatment
    word_count gives frontmatter and generated blocks: adopting Topic lines
    across a decision log must not read as every entry growing, since the gate would report
    the migration's own edit rather than anything an author wrote."""

    def test_a_topic_line_does_not_grow_a_baselined_entry(self):
        w = self.ws(extra="\nmax_words = 1\n")
        code, _, _ = w.run("baseline", "--allow-raise")
        self.assertEqual(code, 0)
        before = json.loads((w.root / layout.BASELINE).read_text(encoding="utf-8"))
        log = w.root / "projects/alpha/DECISIONS.md"
        wtext(log, log.read_text(encoding="utf-8").replace(
            "**Status:** locked", "**Status:** locked\n\n**Topic:** Sync"))
        w.run("index")
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertEqual(code, 0, out)
        after = json.loads((w.root / layout.BASELINE).read_text(encoding="utf-8"))
        self.assertEqual(after, before)

    def test_a_topic_line_inside_a_fence_still_counts(self):
        w = self.ws(extra="\nmax_words = 1\n")
        code, _, _ = w.run("baseline", "--allow-raise")
        self.assertEqual(code, 0)
        before = json.loads((w.root / layout.BASELINE).read_text(encoding="utf-8"))
        log = w.root / "projects/alpha/DECISIONS.md"
        wtext(log, log.read_text(encoding="utf-8").replace(
            "**Status:** locked",
            "**Status:** locked\n\n```\n**Topic:** Example\n```"))
        w.run("index")
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertEqual(code, 1, out)
        # 4 words added: two fence-marker lines and the masked Topic line's own two words —
        # inside a fence it is an example, not metadata, so it counts as body text.
        self.assertIn(f"'decision_words:alpha:A-100' grew to {before['decision_words:alpha:A-100'] + 4}", out)


class RatchetOwnsScopeNameWithColon(unittest.TestCase):
    """`owns` splits a `decision_words`/`governed_docs`/`handoff_words` key from the right: an
    id never contains ':', but nothing stops a scope name from containing one, and splitting
    from the left would cut such a name apart instead of matching it."""

    def _scope(self, name: str) -> registry.Scope:
        return registry.Scope(name=name, entry={}, keys={}, gov=None, id_prefix=None,
                              id_range=None, doc_set=False, governed=True)

    def test_decision_words_key_matches_a_scope_name_containing_colon(self):
        scope = self._scope("alpha:west")
        self.assertTrue(ratchet.owns(None, scope, "decision_words:alpha:west:D-1"))
        self.assertFalse(ratchet.owns(None, scope, "decision_words:alpha:east:D-1"))

    def test_governed_docs_and_handoff_words_keys_match_a_scope_name_containing_colon(self):
        scope = self._scope("alpha:west")
        self.assertTrue(ratchet.owns(None, scope, "governed_docs:alpha:west"))
        self.assertTrue(ratchet.owns(None, scope, "handoff_words:alpha:west"))


if __name__ == "__main__":
    unittest.main()
