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
import time
import unicodedata
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
reasons = {{ statuses = "older entries predate the standard" }}
{extra}
"""

# The fixture's reason for re-allowing 'superseded'; a test takes it out to see a widening refused.
REASON_LINE = 'reasons = { statuses = "older entries predate the standard" }\n'

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
license = "MIT"
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


class WritingRulesSkipEngineVocabulary(Base):
    """A project cannot rename the engine's own names (a check id, a setting, a registry fact),
    so writing-rules never flags one where it is used as a name: in the config, profile and
    registry files, and in Markdown code. Prose, and every other file, is checked in full, so a
    spelling rule holds whichever spelling the engine uses."""

    AMERICAN = '{ text = "licence", use = "license", ignore_case = true }'
    BRITISH = '{ text = "license", use = "licence" }'

    def rules_ws(self, rule: str, files: str = '["notes/*", ".context-gate/config.toml", '
                                                '"projects.toml"]', extra: str = "") -> Workspace:
        return self.ws(extra=f"""
[checks.writing-rules]
level = "error"
files = {files}
rules = [{rule}]
{extra}""")

    def findings(self, w: Workspace) -> list[str]:
        _, out, _ = w.run("check")
        return [line.strip() for line in out.splitlines() if " × " in line]

    def test_an_engine_name_in_a_markdown_code_span_is_not_flagged(self):
        w = self.rules_ws(self.AMERICAN)
        w.write("notes/a.md", "Turn on `licences`, set `[checks.licences]` and "
                              "`checks.licences.conflicts`; each entry sets `licence`.\n")
        self.assertEqual(self.findings(w), [])

    def test_an_engine_name_in_a_fenced_block_is_not_flagged(self):
        w = self.rules_ws(self.AMERICAN)
        w.write("notes/a.md", "Example:\r\n\r\n```toml\r\n[checks.licences]\r\n"
                              "require_declared = true\r\n```\r\n")
        self.assertEqual(self.findings(w), [])

    def test_the_old_check_name_in_the_config_is_not_flagged(self):
        # The config is always left out now (it is in the tool's own directory); this guards
        # the engine-owned exemption in case that ever changes.
        w = self.rules_ws(self.AMERICAN, extra='\n[checks.licences]\nrequire_declared = true\n')
        self.assertEqual(self.findings(w), [])

    def test_the_old_check_name_in_the_profile_is_not_flagged(self):
        w = self.rules_ws(self.AMERICAN, files='["prof/*"]')
        w.write("prof/principles.toml", f"[checks.writing-rules]\nrules = [{self.AMERICAN}]\n\n"
                                        "[checks.licences]\nrequire_declared = true\n")
        cfg = w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            "schema = 1", 'schema = 1\nprofile = "prof"', 1))
        self.assertEqual(self.findings(w), [])

    def test_a_registry_fact_in_the_registry_file_is_not_flagged(self):
        w = self.rules_ws(self.AMERICAN)
        reg = w.root / "projects.toml"
        wtext(reg, reg.read_text(encoding="utf-8").replace('license = "MIT"', 'licence = "MIT"'))
        self.assertEqual(self.findings(w), [])

    def test_prose_is_flagged(self):
        w = self.rules_ws(self.AMERICAN)
        w.write("notes/a.md", "## Licence\n\nThe `licences` check reads each licence.\n")
        self.assertEqual(self.findings(w), ["ERROR  notes/a.md: 2 × 'licence' (use 'license')"])

    def test_the_projects_own_code_is_flagged(self):
        w = self.rules_ws(self.AMERICAN)
        w.write("notes/b.py", "LICENCE_TEXT = 1\n")
        self.assertEqual(self.findings(w), ["ERROR  notes/b.py: 1 × 'licence' (use 'license')"])

    def test_a_code_span_that_is_not_an_engine_name_is_flagged(self):
        w = self.rules_ws(self.AMERICAN)
        w.write("notes/a.md", "Call `my_licence` here.\n")
        self.assertEqual(self.findings(w), ["ERROR  notes/a.md: 1 × 'licence' (use 'license')"])

    def test_a_british_rule_skips_the_new_names_and_flags_prose(self):
        w = self.rules_ws(self.BRITISH, extra='\n[checks.licenses]\nrequire_declared = true\n')
        w.write("notes/a.md", "Turn on `licenses` and set `license` per entry.\n")
        self.assertEqual(self.findings(w), [])
        w.write("notes/a.md", "Every repo needs a license.\n")
        self.assertEqual(self.findings(w), ["ERROR  notes/a.md: 1 × 'license' (use 'licence')"])

    def test_an_extension_check_id_is_an_engine_name(self):
        w = self.rules_ws('{ text = "colour", use = "color" }',
                          files='["notes/*"]')
        cfg = w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            "schema = 1", 'schema = 1\nextensions = ["gov-ext"]', 1))
        w.write("gov-ext/colour.py", """
from govern.findings import Findings
from govern.manifest import check

@check("colour-names", scope="workspace", since="0.1.1", origin="test-ext",
       summary="s.", question="q", rationale="r")
def colour_names(ctx, params):
    return Findings()
""")
        self.addCleanup(manifest.forget, "test-ext")
        w.write("notes/a.md", "Turn on `colour-names`.\n")
        self.assertEqual(self.findings(w), [])


class WritingRulesSelectsFilesLikeTheGate(Base):
    """writing-rules leaves out every file the gate never governs as a doc, however a `files`
    glob or path reaches it: this tool's own directory (whose reports quote the configured rules
    back), `.claude/`, the always-excluded directories at any depth and in any case, and files
    git ignores. `exclude` leaves out more, and adding to it is a loosening that needs a reason."""

    RULE = '{ text = "colour", use = "color" }'
    HIT = "ERROR  notes/a.md: 1 × 'colour' (use 'color')"

    def rules_ws(self, files: str = '["**/*.md"]', extra: str = "") -> Workspace:
        w = self.ws(extra=f"""
[checks.writing-rules]
level = "error"
files = {files}
rules = [{self.RULE}]
{extra}""")
        w.write("notes/a.md", "The colour of it.\n")
        return w

    def findings(self, w: Workspace) -> list[str]:
        code, out, err = w.run("check")
        self.assertNotEqual(code, 2, err)
        return [line.strip() for line in out.splitlines() if " × " in line]

    def test_the_tools_own_reports_are_never_checked(self):
        w = self.rules_ws()
        w.write(".context-gate/install-report.md", "Rule: colour → color.\n")
        w.write(".context-gate/upgrade-report.md", "A colour finding was fixed.\n")
        self.assertEqual(self.findings(w), [self.HIT])

    def test_a_listed_path_in_the_tools_own_dir_is_left_out_too(self):
        w = self.rules_ws(files='["notes/*.md", ".context-gate/install-report.md"]')
        w.write(".context-gate/install-report.md", "Rule: colour → color.\n")
        self.assertEqual(self.findings(w), [self.HIT])

    def test_claude_dir_and_excluded_dirs_at_any_depth_are_skipped(self):
        w = self.rules_ws()
        w.write(".claude/agents/helper.md", "colour\n")
        w.write("sub/Build/notes.md", "colour\n")
        w.write("sub/node_modules/pkg/README.md", "colour\n")
        self.assertEqual(self.findings(w), [self.HIT])

    def test_a_gitignored_file_is_skipped(self):
        w = self.rules_ws()
        subprocess.run(["git", "-C", str(w.root), "init", "-q"], check=True)
        w.write(".gitignore", "scratch/\n")
        w.write("scratch/draft.md", "colour\n")
        self.assertEqual(self.findings(w), [self.HIT])

    def test_a_file_a_nested_checkout_tracks_is_asked_of_that_checkout(self):
        # The root ignores the nested checkout wholesale; its own files are still checked.
        w = self.rules_ws()
        subprocess.run(["git", "-C", str(w.root), "init", "-q"], check=True)
        w.write(".gitignore", "alpha/\n")
        w.write("alpha/doc.md", "colour\n")
        subprocess.run(["git", "-C", str(w.root / "alpha"), "init", "-q"], check=True)
        self.assertEqual(self.findings(w), ["ERROR  alpha/doc.md: 1 × 'colour' (use 'color')",
                                            self.HIT])

    def test_exclude_with_a_reason_skips_its_files(self):
        w = self.rules_ws(files='["**/*.md", "**/*.py"]',
                          extra='exclude = ["engine/tests/**"]\n'
                                'reasons = { exclude = "fixtures spell it both ways" }\n')
        w.write("engine/tests/test_x.py", "COLOUR = 'colour'\n")
        w.write("engine/tests/data/sample.md", "colour\n")
        w.write("engine/x.py", "colour = 1\n")
        self.assertEqual(self.findings(w), ["ERROR  engine/x.py: 1 × 'colour' (use 'color')",
                                            self.HIT])

    def git_ignoring(self, w: Workspace, ignore: str) -> None:
        subprocess.run(["git", "-C", str(w.root), "init", "-q"], check=True)
        w.write(".gitignore", ignore)

    INCLUDE = "list it in include_ignored to check it anyway"

    def test_a_nested_checkouts_own_ignores_apply_below_its_root(self):
        # The root ignores the checkout wholesale; each file is asked of the checkout itself.
        w = self.rules_ws(files='["notes/*.md", "alpha/**/*.md"]')
        self.git_ignoring(w, "alpha/\n")
        w.write("alpha/.gitignore", "docs/scratch.md\n")
        w.write("alpha/docs/keep.md", "colour\n")
        w.write("alpha/docs/scratch.md", "colour\n")
        subprocess.run(["git", "-C", str(w.root / "alpha"), "init", "-q"], check=True)
        self.assertEqual(self.findings(w), [
            "ERROR  alpha/docs/keep.md: 1 × 'colour' (use 'color')", self.HIT])

    def drafts_ws(self, extra: str = "") -> Workspace:
        # The usual idiom ignores the files, not the directory, keeping it with a `.gitkeep`.
        w = self.rules_ws(files='["notes/*.md", "outbound/*.md"]', extra=extra)
        self.git_ignoring(w, "outbound/*\n!outbound/.gitkeep\n")
        w.write("outbound/.gitkeep", "")
        w.write("outbound/pr-1.md", "colour\n")
        return w

    def test_ignored_drafts_are_skipped_with_a_warning_naming_include_ignored(self):
        w = self.drafts_ws()
        _, out, _ = w.run("check")
        self.assertIn("writing-rules: 'outbound/*.md' matched 1 file(s), all left out "
                      f"(gitignored) — {self.INCLUDE}", out)
        self.assertEqual([line.strip() for line in out.splitlines() if " × " in line],
                         [self.HIT])

    def test_include_ignored_checks_ignored_drafts(self):
        w = self.drafts_ws(extra='include_ignored = ["outbound/*.md"]\n')
        self.assertEqual(self.findings(w), [
            self.HIT, "ERROR  outbound/pr-1.md: 1 × 'colour' (use 'color')"])

    def test_always_excluded_wins_over_include_ignored(self):
        w = self.rules_ws(extra='include_ignored = ["sub/build/*.md", ".context-gate/*.md"]\n')
        w.write("sub/build/x.md", "colour\n")
        w.write(".context-gate/install-report.md", "colour\n")
        self.assertEqual(self.findings(w), [self.HIT])

    def test_backslashes_and_a_leading_dot_slash_in_every_list(self):
        w = self.rules_ws(
            files="['./notes/*.md', './outbound/*.md', 'tests/**/*.md']",
            extra="exclude = ['tests\\fixtures\\**', './notes/skip.md']\n"
                  "include_ignored = ['.\\outbound\\*.md']\n"
                  'reasons = { exclude = "fixtures spell it both ways" }\n')
        self.git_ignoring(w, "outbound/\n")
        w.write("outbound/pr-1.md", "colour\n")
        w.write("notes/skip.md", "colour\n")
        w.write("tests/fixtures/f.md", "colour\n")
        w.write("tests/t.md", "colour\n")
        self.assertEqual(self.findings(w), [
            self.HIT, "ERROR  outbound/pr-1.md: 1 × 'colour' (use 'color')",
            "ERROR  tests/t.md: 1 × 'colour' (use 'color')"])

    def test_files_is_case_sensitive_on_every_os(self):
        # A case-insensitive file system answers `Notes/*.md` with `notes/a.md`: it is not
        # checked, and the entry says which file it reaches only in another spelling. A
        # case-sensitive one finds nothing, and says nothing.
        w = self.rules_ws(files='["Notes/*.md", "notes/*.MD"]')
        _, out, _ = w.run("check")
        self.assertNotIn(" × ", out)
        self.assertNotIn("matched", out)
        warning = "writing-rules: 'Notes/*.md' matches 'notes/a.md' only in another spelling"
        if (w.root / "NOTES").exists():
            self.assertIn(warning, out)
        else:
            self.assertNotIn("Notes/*.md", out)

    def test_an_entry_that_matches_nothing_says_nothing(self):
        w = self.rules_ws(files='["notes/*.md", "outbound/*.md"]')
        (w.root / "outbound").mkdir()
        _, out, _ = w.run("check")
        self.assertNotIn("outbound", out)

    def test_a_name_stored_in_another_unicode_form_is_on_disk(self):
        # A file system may store a name decomposed (NFD) that a config spells composed (NFC).
        from govern.checks.writing import _on_disk
        nfc, nfd = (unicodedata.normalize(f, "café.md") for f in ("NFC", "NFD"))
        wtext(self.tmp / nfd, "colour\n")
        self.assertTrue(_on_disk(self.tmp, nfc, {}))
        self.assertFalse(_on_disk(self.tmp, "cafe.md", {}))

    def test_a_directory_in_another_unicode_form_is_checked(self):
        nfc, nfd = (unicodedata.normalize(f, "café") for f in ("NFC", "NFD"))
        w = self.rules_ws(files=f'["notes/*.md", "{nfc}/*.md"]')
        (w.root / nfd).mkdir()
        wtext(w.root / nfd / "x.md", "colour\n")
        if not (w.root / nfc).exists():
            self.skipTest("this file system tells the two forms apart")
        self.assertEqual(len(self.findings(w)), 2)

    def test_repeated_slashes_are_one(self):
        w = self.rules_ws(files='["notes//*.md"]')
        self.assertEqual(self.findings(w), [self.HIT])

    def test_a_dot_segment_is_dropped(self):
        # `notes/./*.md` is `notes/*.md`: its file is checked, not reported as another spelling.
        for raw, want in (("notes/./*.md", "notes/*.md"), ("a/.", "a"), ("./a/./b/", "a/b/"),
                          (".\\a\\.\\b", "a/b"), ("a//./b", "a/b"), ("./", ""), (".", "")):
            self.assertEqual(config.normalize_glob(raw), want, raw)
        w = self.rules_ws(files='["notes/./*.md", "notes/."]')
        _, out, _ = w.run("check")
        self.assertNotIn("another spelling", out)
        self.assertEqual([line.strip() for line in out.splitlines() if " × " in line],
                         [self.HIT])

    def test_an_absolute_entry_is_refused(self):
        # `Path.glob` refuses one with a bare NotImplementedError; the load names the entry.
        cases = [(key, value) for key in ("files", "exclude", "include_ignored")
                 for value in ("/notes/*.md", "C:/x/*.md", r"C:\x\*.md", r"\\server\x",
                               "./C:/x")]
        for i, (key, value) in enumerate(cases):
            with self.subTest(key=key, value=value):
                files = f"['notes/*.md', '{value}']" if key == "files" else "['notes/*.md']"
                listed = "" if key == "files" else f"{key} = ['{value}']\n"
                (self.tmp / str(i)).mkdir()
                w = Workspace(self.tmp / str(i), extra=f"""
[checks.writing-rules]
level = "error"
files = {files}
rules = [{self.RULE}]
{listed}reasons = {{ exclude = "r" }}
""")
                code, _, err = w.run("check")
                self.assertEqual(code, 2, err)
                self.assertIn(f"[checks.writing-rules] {key}: '{value}' must be relative to "
                              f"the governance root, not absolute", err)

    def test_an_entry_that_names_no_path_is_refused(self):
        for i, (key, value) in enumerate((("files", "./"), ("files", ""), ("exclude", "."),
                                          ("include_ignored", ".//"))):
            with self.subTest(key=key, value=value):
                files = f'["notes/*.md", "{value}"]' if key == "files" else '["notes/*.md"]'
                listed = "" if key == "files" else f'{key} = ["{value}"]\n'
                (self.tmp / str(i)).mkdir()
                w = Workspace(self.tmp / str(i), extra=f"""
[checks.writing-rules]
level = "error"
files = {files}
rules = [{self.RULE}]
{listed}reasons = {{ exclude = "r" }}
""")
                code, _, err = w.run("check")
                self.assertEqual(code, 2, err)
                self.assertIn(f"[checks.writing-rules] {key}: '{value}' names no path", err)

    def test_a_pattern_whose_matches_are_all_left_out_warns(self):
        w = self.rules_ws(files='["notes/*.md", "*/draft.md", "build/**/*.md", "none/*.md"]')
        self.git_ignoring(w, "scratch/\n")
        w.write("scratch/draft.md", "colour\n")
        w.write("build/a.md", "colour\n")
        w.write("build/out/b.md", "colour\n")
        code, out, _ = w.run("check")
        self.assertIn("writing-rules: '*/draft.md' matched 1 file(s), all left out (gitignored) "
                      f"— {self.INCLUDE}", out)
        self.assertIn("writing-rules: 'build/**/*.md' matched 2 file(s), all left out "
                      "(always excluded)", out)
        self.assertNotIn("(always excluded) —", out)
        self.assertNotIn("notes/*.md' matched", out)
        self.assertNotIn("none/*.md", out)
        self.assertEqual([line.strip() for line in out.splitlines() if " × " in line],
                         [self.HIT])

    def test_exclude_is_matched_as_a_glob_is(self):
        # `**` spans any number of directories, none included; `*` stays inside one.
        w = self.rules_ws(files='["**/*.md", "**/*.py"]',
                          extra='exclude = ["**/fixtures/**", "engine/tests/*"]\n'
                                'reasons = { exclude = "fixtures spell it both ways" }\n')
        w.write("fixtures/a.md", "colour\n")
        w.write("engine/tests/c.py", "colour = 1\n")
        w.write("engine/tests/a/b.py", "colour = 1\n")
        self.assertEqual(self.findings(w), [
            "ERROR  engine/tests/a/b.py: 1 × 'colour' (use 'color')", self.HIT])

    def test_exclude_is_case_sensitive_on_every_os(self):
        w = self.rules_ws(extra='exclude = ["Notes/**"]\n'
                                'reasons = { exclude = "fixtures spell it both ways" }\n')
        self.assertEqual(self.findings(w), [self.HIT])

    def test_exclude_without_a_reason_is_refused(self):
        w = self.rules_ws(extra='exclude = ["engine/tests/**"]\n')
        code, _, err = w.run("check")
        self.assertEqual(code, 2)
        self.assertIn("[checks.writing-rules] exclude adds 'engine/tests/**' — loosens past what "
                      "the project inherits without a reason", err)


class WritingRulesAllowMarker(Base):
    """A line carrying `writing-rules: allow <text>[, <text>...]` keeps that spelling: a rule
    whose `text` it names (in any case) is not counted on that line, the marker's own mention
    included. Every other line, and every other rule on that line, is checked as before."""

    RULES = ('[{ text = "colour", use = "color" }, '
             '{ text = "licence", use = "license", ignore_case = true }]')

    def rules_ws(self) -> Workspace:
        return self.ws(extra=f"""
[checks.writing-rules]
level = "error"
files = ["notes/*"]
rules = {self.RULES}
""")

    def findings(self, w: Workspace) -> list[str]:
        code, out, err = w.run("check")
        self.assertNotEqual(code, 2, err)
        return [line.strip() for line in out.splitlines() if " × " in line]

    def test_the_marker_skips_the_named_text_on_its_line(self):
        w = self.rules_ws()
        w.write("notes/a.py", 'OLD = {"licences": "licenses"}  # writing-rules: allow Licence\n')
        self.assertEqual(self.findings(w), [])

    def test_the_same_text_on_the_next_line_still_flags(self):
        w = self.rules_ws()
        w.write("notes/a.py", 'OLD = "licence"  # writing-rules: allow licence\n'
                              'NEW = "licence"\n')
        self.assertEqual(self.findings(w), ["ERROR  notes/a.py: 1 × 'licence' (use 'license')"])

    def test_another_rules_text_on_the_marked_line_still_flags(self):
        w = self.rules_ws()
        w.write("notes/a.py", 'OLD = "licence colour"  # writing-rules: allow licence\n')
        self.assertEqual(self.findings(w), ["ERROR  notes/a.py: 1 × 'colour' (use 'color')"])

    def test_several_texts_in_one_marker(self):
        w = self.rules_ws()
        w.write("notes/a.py", 'OLD = "licence colour"  # writing-rules: allow licence, colour\n')
        self.assertEqual(self.findings(w), [])

    def test_a_marker_in_an_html_comment_in_markdown(self):
        w = self.rules_ws()
        w.write("notes/a.md", "The old colour stays. <!-- writing-rules: allow colour -->\r\n"
                              "A new colour does not.\r\n")
        self.assertEqual(self.findings(w), ["ERROR  notes/a.md: 1 × 'colour' (use 'color')"])

    def test_a_quoted_or_punctuated_text_and_any_case_keywords(self):
        w = self.rules_ws()
        w.write("notes/a.md", "A licence. <!-- writing-rules: allow licence. -->\n"
                              "A licence. <!-- writing-rules: allow \"licence\" -->\n"
                              "A licence. <!-- Writing-Rules: ALLOW `licence`; -->\n")
        self.assertEqual(self.findings(w), [])

    def test_allowing_a_text_no_rule_has_is_harmless(self):
        w = self.rules_ws()
        w.write("notes/a.md", "The colour. <!-- writing-rules: allow flavour -->\n")
        self.assertEqual(self.findings(w), ["ERROR  notes/a.md: 1 × 'colour' (use 'color')"])


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

    def test_licenses(self):
        w = self.ws(alpha_extra='role = "own"', extra="""
[checks.licenses]
level = "error"
conflicts = [{ a = { licenses = ["MIT"] }, b = { licenses = ["MIT"], roles = ["own"] }, decision = "W-9" }]
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


class RenamedNamesStillLoad(Base):
    """A name the engine has since respelled (the check `licences` is now `licenses`, the
    registry fact `licence` is now `license`) still loads, read as the new name, with one
    warning per place saying what to rename: an upgrade never turns a project red. Both
    spellings in one place is an error, since neither can be read over the other."""

    RENAMED = "(the old name still works; rename it)"

    def load(self, w: Workspace) -> config.Config:
        return config.load(w.root, w.home)

    def test_an_old_check_id_runs_as_the_new_check(self):
        w = self.ws(alpha_extra='role = "own"', extra="""
[checks.licences]
level = "error"
conflicts = [{ a = { licences = ["MIT"] }, b = { licences = ["MIT"], roles = ["own"] }, decision = "W-9" }]
""")
        code, out, err = w.run("check")
        self.assertIn("W-9 is not a recorded decision", out)
        self.assertIn(f"warning: {CFG} [checks]: 'licences' is now 'licenses' {self.RENAMED}",
                      err)
        self.assertIn(f"warning: {CFG} [checks.licenses] conflicts: 'licences' is now "
                      f"'licenses' {self.RENAMED}", err)
        self.assertEqual(err.count("warning:"), 2, err)
        s = self.load(w).checks["licenses"]
        self.assertEqual((s.level, s.source["level"]), ("error", "project"))

    def test_an_old_check_id_in_a_profile_loads_as_the_new_check(self):
        prof = self.tmp / "prof"
        prof.mkdir()
        wtext(prof / "principles.toml", '[checks.licences]\nlevel = "warn"\n')
        w = self.ws()
        cfg = w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            "schema = 1", f'schema = 1\nprofile = "{prof.as_posix()}"', 1))
        loaded = self.load(w)
        s = loaded.checks["licenses"]
        self.assertEqual((s.level, s.source["level"]), ("warn", "profile"))
        self.assertEqual(loaded.warnings, [
            f"profile {prof.as_posix()} (principles.toml) [checks]: 'licences' is now "
            f"'licenses' {self.RENAMED}"])

    def test_old_and_new_check_ids_together_are_an_error(self):
        w = self.ws(extra='\n[checks.licences]\nlevel = "error"\n'
                          '\n[checks.licenses]\nlevel = "warn"\n')
        code, out, err = w.run("check")
        self.assertEqual(code, 2, out + err)
        self.assertIn(f"{CFG} [checks]: 'licences' and 'licenses' are both set", err)

    def test_an_old_check_id_in_the_run_order_reads_as_the_new_one(self):
        w = self.ws(extra='\n[checks]\nworkspace_order = ["licences"]\n')
        loaded = self.load(w)
        self.assertEqual(loaded.raw["checks"]["workspace_order"], ["licenses"])
        self.assertEqual(loaded.warnings, [f"{CFG} [checks] workspace_order: 'licences' is now "
                                           f"'licenses' {self.RENAMED}"])

    def test_old_and_new_check_ids_in_one_run_order_are_an_error(self):
        w = self.ws(extra='\n[checks]\nworkspace_order = ["licences", "licenses"]\n')
        with self.assertRaisesRegex(config.ConfigError, r"\[checks\] workspace_order: "
                                    r"'licences' and 'licenses' are both set"):
            self.load(w)

    def test_an_old_registry_fact_reads_as_the_new_one(self):
        w = self.ws(extra='\n[checks.licenses]\nlevel = "error"\n')
        reg = w.root / "projects.toml"
        wtext(reg, reg.read_text(encoding="utf-8").replace('license = "MIT"', 'licence = "MIT"'))
        beta = ('\n[[project]]\nname = "beta"\ndir = "beta"\ntier = "registered"\n'
                'licence = "MIT"\n')
        wtext(reg, reg.read_text(encoding="utf-8") + beta)
        code, out, err = w.run("check")
        self.assertEqual(code, 0, out + err)
        self.assertIn(f"warning: projects.toml (alpha, beta): 'licence' is now 'license' "
                      f"{self.RENAMED}", err)
        self.assertEqual(err.count("warning:"), 1, err)
        wtext(reg, reg.read_text(encoding="utf-8").replace(beta, ""))
        wtext(reg, reg.read_text(encoding="utf-8").replace('licence = "MIT"', ""))
        code, out, _ = w.run("check")
        self.assertIn("alpha: no license declared", out)

    def test_an_entry_with_both_registry_facts_is_an_error(self):
        w = self.ws()
        reg = w.root / "projects.toml"
        wtext(reg, reg.read_text(encoding="utf-8").replace(
            'license = "MIT"', 'license = "MIT"\nlicence = "MIT"'))
        code, out, err = w.run("check")
        self.assertEqual(code, 2, out + err)
        self.assertIn("projects.toml (alpha): 'licence' and 'license' are both set", err)

    def test_an_old_registry_keys_mapping_reads_as_the_new_one(self):
        w = self.ws(extra='\n[checks.licenses]\nlevel = "error"\n')
        cfg, reg = w.root / CFG, w.root / "projects.toml"
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            'handoff = "profile.handoff"', 'handoff = "profile.handoff"\nlicence = "profile.lic"'))
        wtext(reg, reg.read_text(encoding="utf-8").replace('license = "MIT"\n', "").replace(
            "[project.profile]\n", '[project.profile]\nlic = "MIT"\n'))
        code, out, err = w.run("check")
        self.assertEqual(code, 0, out + err)
        self.assertIn(f"warning: {CFG} [registry.keys]: 'licence' is now 'license' "
                      f"{self.RENAMED}", err)

    def test_a_registry_that_keeps_the_old_name_maps_it_without_a_warning(self):
        # A registry file other tools read too keeps `licence` by saying where the fact lives.
        w = self.ws(extra='\n[checks.licenses]\nlevel = "error"\n')
        cfg, reg = w.root / CFG, w.root / "projects.toml"
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            'handoff = "profile.handoff"', 'handoff = "profile.handoff"\nlicense = "licence"'))
        wtext(reg, reg.read_text(encoding="utf-8").replace('license = "MIT"', 'licence = "MIT"'))
        code, out, err = w.run("check")
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("warning:", err)

    def test_an_old_registry_keys_mapping_still_serves_the_old_name(self):
        # Required under the old name, mapped under the old name: both read the one fact.
        w = self.ws(extra='\n[checks.registry]\nrequired_keys = ["name", "dir", "tier", '
                          '"licence"]\n')
        cfg, reg = w.root / CFG, w.root / "projects.toml"
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            'handoff = "profile.handoff"', 'handoff = "profile.handoff"\nlicence = "profile.lic"'))
        wtext(reg, reg.read_text(encoding="utf-8").replace('license = "MIT"\n', "").replace(
            "[project.profile]\n", '[project.profile]\nlic = "MIT"\n'))
        code, out, err = w.run("check")
        self.assertEqual(code, 0, out + err)
        ctx = cli.load_context(w.root, "govern")
        self.assertEqual(ctx.registry.scopes[0].get("licence"), "MIT")

    def test_a_word_that_is_only_an_old_fact_name_is_not_a_check_id(self):
        # `licence` was a registry fact, never a check: as a check id it is an unknown name.
        w = self.ws(extra='\n[checks]\nworkspace_order = ["licence"]\n')
        with self.assertRaisesRegex(config.ConfigError, "names 'licence', which is not a "
                                                        "registered check"):
            self.load(w)

    def test_an_old_fact_name_in_a_conflict_side_is_left_as_written(self):
        w = self.ws(extra='\n[checks.licenses]\nconflicts = [{ a = { licence = ["MIT"] }, '
                          'b = { licenses = ["MIT"] }, decision = "W-9" }]\n')
        loaded = self.load(w)
        self.assertEqual(loaded.warnings, [])
        self.assertEqual(loaded.checks["licenses"].params["conflicts"][0]["a"],
                         {"licence": ["MIT"]})

    def test_explain_and_record_accept_the_old_check_id(self):
        w = self.ws()
        code, out, err = w.run("explain", "licences")
        self.assertEqual(code, 0, err)
        self.assertEqual(out, w.run("explain", "licenses")[1])
        self.assertIn("warning: explain: 'licences' is now 'licenses'", err)
        wtext(w.root / layout.MANIFEST, 'engine = "0.5.0"\ninstalled = "2026-09-23"\n')
        code, out, err = w.run("options", "--record", "licences")
        self.assertEqual(code, 0, err)
        self.assertIn("recorded as answered: licenses", out)
        self.assertIn("warning: options --record: 'licences' is now 'licenses'", err)

    def test_an_old_registry_fact_in_required_when_reads_as_the_new_one(self):
        w = self.ws()
        cfg = w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            "[projects]\n", '[projects]\nrequired_when = [{ key = "licence", docs = ["NOTICE"] }]\n', 1))
        loaded = self.load(w)
        self.assertEqual(loaded.raw["projects"]["required_when"][0]["key"], "license")
        self.assertEqual(loaded.warnings, [f"{CFG} [projects] required_when[0] key: 'licence' is "
                                           f"now 'license' {self.RENAMED}"])


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

    def test_a_retired_key_is_named_no_longer_used(self):
        w = self.ws()
        cfg = w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            "[blocks]\n", '[blocks]\nplaceholder = "_Run index._"\n', 1))
        code, _, err = w.run("check")
        self.assertEqual(code, 2)
        self.assertIn(f"{CFG}: [blocks] placeholder is no longer used (it was never read): "
                      f"remove it", err)
        self.assertNotIn("unknown key", err)

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
BETA = f"{SERIES}.99-beta.1"


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

    # A beta this machine opted into, in .context-gate/local.toml (never committed).
    def _local(self, w, text):
        wtext(w.root / layout.LOCAL, text)

    def _check(self, entry, env):
        return subprocess.run([sys.executable, str(entry), "check"], env=env,
                              capture_output=True, text=True)

    def test_no_local_file_changes_nothing(self):
        w = self.ws()
        entry, env = self._installed(w)
        self._engines(w, (__version__, BETA))
        res = self._check(entry, env)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("beta", res.stderr)
        self.assertTrue(self._which(entry, env).endswith(__version__))

    def test_local_beta_runs(self):
        w = self.ws()
        entry, env = self._installed(w)
        self._engines(w, (__version__, BETA))
        self._local(w, f'[governance]\nengine = "{BETA}"\n')
        self.assertTrue(self._which(entry, env).endswith(BETA))
        res = self._check(entry, env)
        self.assertIn(f"context-gate: running beta {BETA} on this machine (committed pin "
                      f"{__version__}); govern beta off to leave it", res.stderr)

    def test_missing_beta_falls_back(self):
        w = self.ws()
        entry, env = self._installed(w)
        self._engines(w, (__version__,))
        self._local(w, f'[governance]\nengine = "{BETA}"\n')
        res = self._check(entry, env)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(f"beta {BETA} is not installed, running the committed pin "
                      f"{__version__}", res.stderr)
        self.assertEqual(res.stderr.count("context-gate: beta"), 1, res.stderr)
        self.assertTrue(self._which(entry, env).endswith(__version__))

    def test_local_naming_a_stable_is_ignored(self):
        w = self.ws()
        entry, env = self._installed(w)
        self._engines(w, (__version__, NEXT_RELEASE))
        self._local(w, f'[governance]\nengine = "{NEXT_RELEASE}"\n')
        self.assertTrue(self._which(entry, env).endswith(__version__))
        self.assertIn(f"local.toml names no beta engine ('{NEXT_RELEASE}')",
                      self._check(entry, env).stderr)

    def test_unreadable_local_runs_the_pin(self):
        w = self.ws()
        entry, env = self._installed(w)
        self._engines(w, (__version__, BETA))
        for text in (f'[governance\nengine = "{BETA}"\n', "[governance]\n",
                     'governance = "x"\n', b"\xff"):
            (w.root / layout.LOCAL).write_bytes(text if isinstance(text, bytes) else text.encode())
            res = self._check(entry, env)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn(f"running the committed pin {__version__}; govern beta off to clear it",
                          res.stderr)
            self.assertTrue(self._which(entry, env).endswith(__version__))

class BetaCommand(Base):
    """`govern beta`, handled by the entry point before any engine loads: on checks everything
    before writing anything, off needs no engine, and the user's own settings survive both."""

    def setUp(self) -> None:
        super().setUp()
        w = self.ws()
        self.entry, self.env = self._installed(w)
        self._engines(w, (__version__, BETA))
        self.root, self.home = w.root, w.home
        self.env = {**self.env, "XDG_CONFIG_HOME": str(self.home)}
        self.git("init", "-q", str(self.root))
        (self.root / ".claude").mkdir()
        self.settings = self.root / ".claude" / "settings.local.json"
        self.plugin_release = self.home / ".claude/skills/context-gate/govern/RELEASE"
        self.plugin_release.parent.mkdir(parents=True)
        wtext(self.plugin_release, f"v{BETA}\n")

    def git(self, *args: str, env=None) -> subprocess.CompletedProcess:
        """git under the temp HOME: the developer's own git config never reaches a test."""
        return subprocess.run(["git", *args], env=env or self.env, capture_output=True,
                              text=True)

    def run_beta(self, *args: str, env=None) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(self.entry), "beta", *args],
                              env=env or self.env, capture_output=True, text=True)

    def check_ignored(self) -> int:
        return self.git("-C", str(self.root), "check-ignore", "-q", layout.LOCAL).returncode

    def state(self) -> dict:
        """Every file in the project, its bytes, git's exclude included."""
        files = [p for p in self.root.rglob("*") if p.is_file() and ".git" not in p.parts]
        files.append(self.root / ".git" / "info" / "exclude")
        return {p: p.read_bytes() for p in files if p.is_file()}

    def test_on_then_off_restores_settings(self):
        own = {"permissions": {"allow": ["Bash(ls)"]}, "enabledPlugins": {"x@y": True},
               "note": "caf\u00e9"}
        wtext(self.settings, json.dumps(own, indent=2, ensure_ascii=False) + "\n")
        before = self.settings.read_bytes()
        res = self.run_beta("on", BETA)
        self.assertEqual(res.returncode, 0, res.stderr)
        got = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(got["enabledPlugins"], {"x@y": True, "context-gate@skills-dir": True,
                                                 "context-gate@context-gate": False})
        self.assertEqual(got["permissions"], own["permissions"])
        self.assertIn(f'engine = "{BETA}"', (self.root / layout.LOCAL).read_text(encoding="utf-8"))
        self.assertEqual(self.check_ignored(), 0)
        self.assertIn("Restart the Claude Code session", res.stdout)
        res = self.run_beta("off")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.settings.read_bytes(), before)
        self.assertFalse((self.root / layout.LOCAL).exists())
        self.assertIn("Back on the committed pin", res.stdout)

    def test_on_and_off_with_no_settings_file(self):
        self.assertEqual(self.run_beta("on", BETA).returncode, 0)
        self.assertTrue(self.settings.is_file())
        self.assertEqual(self.run_beta("off").returncode, 0)
        self.assertFalse(self.settings.exists())

    def test_settings_bom_and_crlf_survive(self):
        raw = b'\xef\xbb\xbf{\r\n  "permissions": {}\r\n}\r\n'
        self.settings.write_bytes(raw)
        self.assertEqual(self.run_beta("on", BETA).returncode, 0)
        on = self.settings.read_bytes()
        self.assertTrue(on.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn(b"\n", on.replace(b"\r\n", b""))
        self.assertEqual(self.run_beta("off").returncode, 0)
        self.assertEqual(self.settings.read_bytes(), raw)

    def test_on_twice_changes_nothing(self):
        self.assertEqual(self.run_beta("on", BETA).returncode, 0)
        once = self.state()
        self.assertEqual(self.run_beta("on", f"v{BETA}").returncode, 0)
        self.assertEqual(self.state(), once)
        exclude = (self.root / ".git" / "info" / "exclude").read_text(encoding="utf-8")
        self.assertEqual(exclude.count(layout.LOCAL), 1)

    def test_on_keeps_other_tables_for_the_same_beta(self):
        text = f'[governance]\nengine = "{BETA}"\n\n[checks.usage]\nenabled = true\n'
        wtext(self.root / layout.LOCAL, text)
        self.assertEqual(self.run_beta("on", BETA).returncode, 0)
        self.assertEqual((self.root / layout.LOCAL).read_text(encoding="utf-8"), text)

    def test_off_twice_and_with_engine_gone(self):
        self.assertEqual(self.run_beta("on", BETA).returncode, 0)
        shutil.rmtree(layout.engines_dir(self.home))
        res = self.run_beta("off")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertFalse((self.root / layout.LOCAL).exists())
        self.assertFalse(self.settings.exists())
        res = self.run_beta("off")
        self.assertEqual(res.returncode, 0)
        self.assertIn("no beta is on", res.stdout)

    def test_refusals_change_nothing(self):
        other = f"{SERIES}.99-beta.2"
        self._engines(SimpleNamespace(home=self.home), (other,))
        cases = [
            (("on", NEXT_RELEASE), "not a beta", None),
            (("on", f"{SERIES}.98-beta.1"), "install-engine.py", None),
            (("on", other), "install-plugin.py", None),
            (("on",), "usage", None),
            (("on", BETA), f"beta {other} is on here: govern beta off first",
             lambda: wtext(self.root / layout.LOCAL, f'[governance]\nengine = "{other}"\n')),
            (("on", BETA), "names no beta (None): govern beta off first",
             lambda: wtext(self.root / layout.LOCAL, "[governance\n")),
            (("on", BETA), "is unreadable", lambda: (self.root / layout.LOCAL).write_bytes(b"\xff")),
            (("on", BETA), "not valid settings JSON",
             lambda: wtext(self.settings, '{"enabledPlugins": {"context-gate@context-gate": "x"}}')),
            (("on", BETA), "no .claude/ directory",
             lambda: (self.root / ".claude").rmdir()),
            (("on", BETA), "not valid settings JSON", lambda: wtext(self.settings, "[]")),
            (("on", BETA), "not valid settings JSON",
             lambda: wtext(self.settings, '{"enabledPlugins": []}')),
            (("on", BETA), "not valid settings JSON",
             lambda: self.settings.write_bytes(b'{"a": "\xff"}')),
            (("off",), "not valid settings JSON", lambda: (
                wtext(self.settings, "{not json"),
                wtext(self.root / layout.LOCAL, f'[governance]\nengine = "{BETA}"\n'))),
            (("on", BETA), "git cannot say", lambda: shutil.rmtree(self.root / ".git")),
        ]
        for args, words, arrange in cases:
            with self.subTest(args=args, words=words):
                for path in (self.root / layout.LOCAL, self.settings):
                    if path.is_file():
                        path.unlink()
                (self.root / ".claude").mkdir(exist_ok=True)
                if arrange:
                    arrange()
                before = self.state()
                res = self.run_beta(*args)
                self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
                self.assertIn(words, res.stderr)
                self.assertNotIn("Traceback", res.stderr)
                self.assertEqual(self.state(), before)

    def test_plugin_must_match(self):
        wtext(self.plugin_release, "v0.0.1-beta.1\n")
        res = self.run_beta("on", BETA)
        self.assertEqual(res.returncode, 2)
        self.assertIn("install-plugin.py", res.stderr)
        self.plugin_release.unlink()
        res = self.run_beta("on", BETA)
        self.assertEqual(res.returncode, 2)
        self.assertIn("install-plugin.py", res.stderr)

    def test_invalid_settings_is_never_rewritten(self):
        wtext(self.settings, "{not json")
        self.assertEqual(self.run_beta("on", BETA).returncode, 2)
        self.assertEqual(self.settings.read_text(encoding="utf-8"), "{not json")
        self.assertFalse((self.root / layout.LOCAL).exists())

    def test_failed_write_leaves_the_originals(self):
        # A settings path that cannot be written (here, a directory): local.toml, written
        # first, is taken back.
        self.settings.mkdir()
        res = self.run_beta("on", BETA)
        self.assertEqual(res.returncode, 2, res.stderr)
        self.assertIn("nothing changed", res.stderr)
        self.assertFalse((self.root / layout.LOCAL).exists())

    def test_exclude_lands_in_the_enclosing_repo(self):
        # Review Focus 5: the project root is a subdirectory of the git repository.
        repo = self.root.parent
        shutil.rmtree(self.root / ".git")
        self.git("init", "-q", str(repo))
        res = self.run_beta("on", BETA)
        self.assertEqual(res.returncode, 0, res.stderr)
        rel = f"{self.root.name}/{layout.LOCAL}"
        res = self.git("-C", str(repo), "check-ignore", "-q", rel)
        self.assertEqual(res.returncode, 0)
        self.assertNotIn(".gitignore", {p.name for p in repo.iterdir()})
        self.assertNotIn(".gitignore", {p.name for p in self.root.iterdir()})

    def test_status(self):
        self.assertIn("no beta", self.run_beta().stdout)
        self.run_beta("on", BETA)
        out = self.run_beta().stdout
        self.assertIn(f"beta {BETA}", out)
        self.assertIn("engine: installed", out)
        self.assertIn("plugin: installed", out)
        self.assertIn("settings.local.json: beta on, stable off", out)
        wtext(self.plugin_release, f"v{SERIES}.99-beta.2\n")
        shutil.rmtree(layout.engines_dir(self.home) / BETA)
        out = self.run_beta().stdout
        self.assertIn("engine: missing", out)
        self.assertIn(f"plugin: is {SERIES}.99-beta.2", out)

    def test_status_says_when_both_plugins_load(self):
        """enabledPlugins merged as Claude Code merges it: user, then project, then local."""
        both = ("context-gate: both the beta and the stable plugin are enabled here, so every "
                f"hook runs twice; govern beta on {BETA} re-applies the switch")
        user, project = self.home / ".claude" / "settings.json", self.root / ".claude" / "settings.json"
        user.parent.mkdir(parents=True, exist_ok=True)
        wtext(user, '{"enabledPlugins": {"context-gate@context-gate": true}}\n')
        self.assertEqual(self.run_beta("on", BETA).returncode, 0)
        self.assertNotIn(both, self.run_beta().stdout)    # local's false wins over user's true
        data = json.loads(self.settings.read_text(encoding="utf-8"))
        del data["enabledPlugins"]["context-gate@context-gate"]
        wtext(self.settings, json.dumps(data))
        self.assertIn(both, self.run_beta().stdout)       # nothing above the user's true now
        wtext(project, '{"enabledPlugins": {"context-gate@context-gate": false}}\n')
        self.assertNotIn(both, self.run_beta().stdout)    # project's false over user's true
        wtext(project, "{not json")
        self.assertIn(both, self.run_beta().stdout)       # an unreadable file is skipped

    def test_prior_plugin_values_survive(self):
        own = {"enabledPlugins": {"context-gate@context-gate": True, "x@y": True}}
        wtext(self.settings, json.dumps(own, indent=2) + "\n")
        before = self.settings.read_bytes()
        self.assertEqual(self.run_beta("on", BETA).returncode, 0)
        self.assertIn('plugins_before = { "context-gate@context-gate" = true }',
                      (self.root / layout.LOCAL).read_text(encoding="utf-8"))
        self.assertEqual(self.run_beta("off").returncode, 0)
        self.assertEqual(self.settings.read_bytes(), before)

    def test_no_plugins_before_line_when_neither_key_existed(self):
        self.assertEqual(self.run_beta("on", BETA).returncode, 0)
        self.assertNotIn("plugins_before", (self.root / layout.LOCAL).read_text(encoding="utf-8"))

    def test_off_without_local_toml_touches_nothing(self):
        wtext(self.settings, '{"enabledPlugins": {"context-gate@context-gate": true}}\n')
        before = self.state()
        res = self.run_beta("off")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("no beta is on in this project", res.stdout)
        self.assertEqual(self.state(), before)

    def test_on_makes_git_ignore_the_settings_file(self):
        self.assertEqual(self.run_beta("on", BETA).returncode, 0)
        status = self.git("-C", str(self.root), "status", "--porcelain", "-uall").stdout
        self.assertNotIn(".claude/", status)

    def _fake_git(self, body: str) -> dict:
        real = shutil.which("git")
        bin_dir = self.tmp / "fakebin"
        bin_dir.mkdir()
        script = bin_dir / "git"
        wtext(script, f"#!{sys.executable}\nimport os, sys\nREAL = {real!r}\n{body}\n")
        script.chmod(0o755)
        return {**self.env, "PATH": f"{bin_dir}{os.pathsep}{self.env['PATH']}"}

    @unittest.skipIf(os.name == "nt", "the fake git is a shebang script, which Windows does not run from PATH")
    def test_exclude_survives_a_git_that_echoes_unknown_flags(self):
        # git < 2.31 prints an unknown rev-parse flag back and exits 0.
        env = self._fake_git(
            "args = sys.argv[1:]\n"
            "if '--path-format=absolute' in args:\n"
            "    print('--path-format=absolute')\n"
            "    args.remove('--path-format=absolute')\n"
            "os.execv(REAL, [REAL, *args])")
        res = self.run_beta("on", BETA, env=env)
        self.assertEqual(res.returncode, 0, res.stderr)
        exclude = (self.root / ".git" / "info" / "exclude").read_text(encoding="utf-8")
        self.assertIn(f"/{layout.LOCAL}", exclude)
        self.assertEqual(self.check_ignored(), 0)

    @unittest.skipIf(os.name == "nt", "the fake git is a shebang script, which Windows does not run from PATH")
    def test_exclude_outside_the_git_dir_is_refused(self):
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        env = self._fake_git(
            "args = sys.argv[1:]\n"
            "if '--git-path' in args:\n"
            f"    print({str(elsewhere / 'exclude')!r})\n"
            "    sys.exit(0)\n"
            "os.execv(REAL, [REAL, *args])")
        before = self.state()
        res = self.run_beta("on", BETA, env=env)
        self.assertEqual(res.returncode, 2, res.stdout)
        self.assertIn("which is not inside", res.stderr)
        self.assertEqual(self.state(), before)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_uninstall_does_not_take_beta(self):
        # Only the gate dispatches `beta`: bin/uninstall hands its argv to the installer.
        uninstall = self.root / layout.GOV_DIR / "bin" / "uninstall"
        res = subprocess.run([sys.executable, str(uninstall), "beta"], env=self.env,
                             capture_output=True, text=True)
        self.assertNotIn("no beta", res.stdout + res.stderr)


class LocalLayer(Base):
    """`.context-gate/local.toml` is the top config layer for the beta engine it names, on this
    machine only: that beta accepts the committed pin it does not match, and every other engine
    never reads the file."""

    def setUp(self) -> None:
        super().setUp()
        from unittest import mock
        self.mock = mock
        self.w = self.ws(extra='\n[checks.writing-rules]\nlevel = "warn"\n')

    def local(self, text: str | bytes = "") -> None:
        if isinstance(text, str):
            text = f'[governance]\nengine = "{BETA}"\n' + text
        self.w.write(layout.LOCAL, text)

    def load(self, engine: str | None = BETA) -> config.Config:
        if engine is None:
            return config.load(self.w.root, self.w.home)
        with self.mock.patch.object(config, "__version__", engine):
            return config.load(self.w.root, self.w.home)

    def refused(self, engine: str | None = BETA) -> str:
        with self.assertRaises(config.ConfigError) as ctx:
            self.load(engine)
        return str(ctx.exception)

    def test_the_beta_it_names_runs_the_committed_pin_and_local_wins(self):
        self.local('\n[checks.writing-rules]\nlevel = "error"\n')
        s = self.load().checks["writing-rules"]
        self.assertEqual((s.level, s.source["level"]), ("error", "local"))

    def test_the_settings_beta_on_writes_load(self):
        self.local('plugins_before = { "context-gate@context-gate" = true }\n')
        self.assertEqual(self.load().checks["writing-rules"].source["level"], "project")

    def test_overriding_a_principle_from_it_is_named(self):
        prof = self.tmp / "prof"
        prof.mkdir()
        wtext(prof / "principles.toml", '[checks.agents]\nmax_turns = 100\n'
              '\n[checks.working-file-count]\nlevel = "error"\n')
        cfg = self.w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            "schema = 1", f'schema = 1\nprofile = "{prof.as_posix()}"', 1))
        self.local('\n[checks.agents]\nmax_turns = 50\n'
                   '\n[checks.working-file-count]\nlevel = "warn"\n')
        self.assertEqual(sorted(self.load().profile_overrides),
                         [("agents", "max_turns"), ("working-file-count", "level")])

    def test_a_stable_engine_ignores_it(self):
        self.local('\n[checks.no-such-check]\nlevel = "error"\n')
        s = self.load(None).checks["writing-rules"]
        self.assertEqual((s.level, s.source["level"]), ("warn", "project"))
        code, _, err = self.w.run("check")
        self.assertNotEqual(code, 2, err)
        self.assertNotIn("no-such-check", err)

    def test_a_stable_engine_ignores_it_even_when_it_names_that_engine(self):
        self.w.write(layout.LOCAL, f'[governance]\nengine = "{__version__}"\n'
                     '\n[checks.no-such-check]\nlevel = "error"\n')
        self.assertEqual(self.load(None).checks["writing-rules"].level, "warn")

    def test_another_beta_ignores_it_and_refuses_the_committed_pin(self):
        self.local('\n[checks.no-such-check]\nlevel = "error"\n')
        err = self.refused(f"{SERIES}.99-beta.2")
        self.assertIn(f"pins engine {__version__}", err)
        self.assertNotIn("no-such-check", err)

    def test_no_local_file_and_a_beta_engine_is_refused(self):
        # A beta never runs a project silently: only local.toml lets it past the pin.
        self.assertIn(f"pins engine {__version__}, but this is engine {BETA}", self.refused())

    def test_an_unreadable_file_is_ignored_not_a_traceback(self):
        self.local(b'[governance]\nengine = "\xff"\n')
        self.assertIn(f"pins engine {__version__}", self.refused())

    def test_another_governance_key_is_refused(self):
        self.local('profile = "x"\n')
        err = self.refused()
        self.assertIn("local.toml", err)
        self.assertIn("profile", err)

    def test_a_layout_table_is_refused(self):
        self.local('\n[projects]\nrequired_docs = ["X.md"]\n')
        self.assertIn(f"{layout.LOCAL}: sets projects", self.refused())

    def test_a_check_order_is_refused(self):
        self.local('\n[checks]\nworkspace_order = ["writing-rules"]\n')
        self.assertIn(f"{layout.LOCAL}: [checks] sets workspace_order", self.refused())

    def test_plugins_before_must_map_ids_to_booleans(self):
        self.local('plugins_before = { "context-gate@context-gate" = "yes" }\n')
        self.assertIn("plugins_before must be a table", self.refused())

    def test_widening_a_list_needs_its_reason(self):
        self.local('\n[checks.decision-log]\n'
                   'statuses = ["locked", "provisional", "superseded", "dropped"]\n')
        self.assertTrue(self.refused().startswith(f"{layout.LOCAL}: [checks."))

    def test_a_beta_pin_in_the_committed_config_is_refused(self):
        cfg = self.w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            f'engine = "{__version__}"', f'engine = "{BETA}"', 1))
        self.assertIn(f"{CFG} pins beta {BETA}; a beta runs only from {layout.LOCAL} on one "
                      f"machine (govern beta on {BETA}) — pin a release", self.refused(None))

    def test_engine_alone_activates_it(self):
        # What `govern beta on` writes: only the engine it names.
        self.w.write(layout.LOCAL, f'[governance]\nengine = "{BETA}"\n')
        self.assertEqual(self.load().checks["writing-rules"].level, "warn")

    def pin(self, new: str) -> None:
        cfg = self.w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(f'engine = "{__version__}"', new, 1))

    def test_the_layer_does_not_excuse_a_missing_committed_pin(self):
        self.pin("")
        self.local()
        self.assertIn("[governance] engine is required", self.refused())

    def test_the_layer_does_not_excuse_a_beta_committed_pin(self):
        self.pin(f'engine = "{BETA}"')
        self.local()
        self.assertIn(f"pins beta {BETA}", self.refused())

    def test_an_override_set_there_is_reported_under_its_file(self):
        prof = self.tmp / "prof"
        prof.mkdir()
        wtext(prof / "principles.toml", '[checks.working-file-count]\nlevel = "error"\n')
        cfg = self.w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            "schema = 1", f'schema = 1\nprofile = "{prof.as_posix()}"', 1))
        self.local('\n[checks.working-file-count]\nlevel = "warn"\n')
        warns = self.overrides_warnings()
        self.assertTrue(any(w.startswith(f"{layout.LOCAL}: [checks.working-file-count]")
                            for w in warns), warns)

    def test_a_widening_set_there_is_reported_under_its_file(self):
        wtext(self.w.root / CFG, (self.w.root / CFG).read_text(encoding="utf-8").replace(
            "schema = 1", "schema = 1\nrequire_reasons = false", 1))
        self.local('\n[checks.decision-log]\n'
                   'statuses = ["locked", "provisional", "superseded", "dropped"]\n')
        warns = self.overrides_warnings()
        self.assertTrue(any(w.startswith(f"{layout.LOCAL}: [checks.decision-log] statuses")
                            for w in warns), warns)

    def overrides_warnings(self) -> list[str]:
        from govern.checks.overrides import standard_overrides
        cfg = self.load()
        return [str(x) for x in standard_overrides(self.mock.Mock(cfg=cfg), {}).warnings]

    def test_loosening_there_without_a_reason_names_the_file(self):
        self.local('\n[checks.agent-worktrees]\nlevel = "off"\n')
        err = self.refused()
        self.assertIn(layout.LOCAL, err)
        self.assertIn("agent-worktrees", err)


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
        from unittest import mock
        src = make_source(self.tmp, (__version__, "0.9.0"))
        w = self.ws()
        cfg = w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            "schema = 1", f'schema = 1\nsource = "{src.as_posix()}"', 1))
        os.environ.pop(notice.NO_UPDATE_CHECK, None)
        with mock.patch.object(notice, "__version__", "0.5.0"):   # a stable engine, whatever this is
            code, _, err = w.run("check")
        self.assertIn("context-gate 0.9.0 available", err)
        self.assertTrue((w.home / ".cache" / layout.TOOL / "releases.json").is_file())
        os.environ["CONTEXT_GATE_NO_UPDATE_CHECK"] = "1"
        self.addCleanup(os.environ.pop, "CONTEXT_GATE_NO_UPDATE_CHECK", None)
        (w.home / ".cache" / layout.TOOL / "releases.json").unlink()
        code, _, err = w.run("check")
        self.assertNotIn("available", err)
        self.assertFalse((w.home / ".cache" / layout.TOOL / "releases.json").exists())

    def _engines(self, home: Path, *versions: str) -> None:
        for v in versions:
            (layout.engines_dir(home) / v / "govern").mkdir(parents=True)

    def test_newest_available_is_never_a_beta(self):
        """Stable code paths never return a beta: an installed beta, even one numbered above
        every release, is not what a project is told to upgrade to."""
        home = self.tmp / "home"
        self._engines(home, "99.0.0-beta.1", "0.5.0")
        self.assertEqual(notice.newest_available(home, None), "0.5.0")
        self.assertEqual(notice.installed_versions(home), ["0.5.0"])

    def test_only_betas_installed_and_none_released_is_no_newest(self):
        home = self.tmp / "home"
        self._engines(home, "99.0.0-beta.1")
        self.assertIsNone(notice.newest_available(home, None))
        # Nor does a beta that reached the release cache count as a release.
        cache = home / ".cache" / layout.TOOL / "releases.json"
        cache.parent.mkdir(parents=True)
        cache.write_text(json.dumps({"src": {"checked": time.time(), "ok": True,
                                             "versions": ["99.0.0-beta.1"]}}), encoding="utf-8")
        self.assertIsNone(notice.newest_available(home, "src"))

    def test_a_beta_tag_at_the_source_is_not_a_release(self):
        src = make_source(self.tmp, ("0.5.0", "99.0.0-beta.1", "0.9.0"))
        self.assertEqual(sorted(notice.released_versions(src.as_posix(), self.tmp / "home", True)),
                         ["0.5.0", "0.9.0"])

    def test_a_beta_hears_only_of_its_own_release(self):
        """A machine running a beta is told when a release above it is out, and how to leave
        the beta for it; older releases say nothing."""
        w = self.ws()
        self._engines(w.home, "0.5.0", "0.5.1")
        self.assertIsNone(notice.for_project(w.root, w.home, "0.6.0-beta.1"))
        self._engines(w.home, "0.6.0")
        msg = notice.for_project(w.root, w.home, "0.6.0-beta.1")
        self.assertIn("0.6.0 is out (this machine runs beta 0.6.0-beta.1)", msg)
        self.assertIn("govern beta off, then", msg)


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


class UpgradeRefreshesBlocks(Base):
    """An upgrade regenerates every generated block with the engine it pins, so it never turns a
    project red: a doc under a nested `build/` that 0.4.1 governed (its excludes matched only at
    the top) is left out now, and the doc registry 0.4.1 wrote would otherwise read as stale."""

    @staticmethod
    def _top_level_only(rel: str):
        """0.4.1's rule: an always-excluded directory matched only at the top."""
        from fnmatch import fnmatch
        from govern.context import ALWAYS_EXCLUDED_DIRS
        return next((f"{d}/**" for d in ALWAYS_EXCLUDED_DIRS if fnmatch(rel, f"{d}/**")), None)

    def test_a_doc_under_a_nested_build_dir_upgrades_green(self):
        from unittest import mock
        w = self.ws()
        cfg_text = (w.root / CFG).read_text(encoding="utf-8").replace(
            'project = [{ file = "DECISIONS.md", id = "decision-index" }]',
            'project = [{ file = "DECISIONS.md", id = "decision-index" },\n'
            '           { file = "INDEX.md", id = "doc-registry" }]').replace(
            f'engine = "{__version__}"', f'engine = "{SERIES}"')
        cfg = self.tmp / "config.toml"
        wtext(cfg, cfg_text)
        (w.root / CFG).unlink()
        (w.root / layout.GOV_DIR).rmdir()
        w.write("projects/alpha/INDEX.md", fm() + "# Index\n\n"
                "<!-- t:generated:start id=doc-registry -->\n"
                "<!-- t:generated:end id=doc-registry -->\n")
        w.write("projects/alpha/sub/build/notes.md", fm("reference") + "# Notes\n")
        with mock.patch("govern.context.default_exclude", self._top_level_only), \
                contextlib.redirect_stdout(io.StringIO()):
            installer.main(["install", "--root", str(w.root), "--config", str(cfg),
                            "--no-report"])
            w.run("index")
            code, out, _ = w.run("check")
        self.assertEqual(code, 0, out)                          # green on the old rule
        self.assertIn("sub/build/notes.md", (w.root / "projects/alpha/INDEX.md")
                      .read_text(encoding="utf-8"))
        code, out, _ = w.run("check")
        self.assertIn("generated block 'doc-registry' is stale", out)   # what upgrade must fix
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = installer.main(["upgrade", "--root", str(w.root), "--no-report"])
        self.assertEqual(code, 0, buf.getvalue())
        self.assertIn("  index        1 block(s) refreshed", buf.getvalue())
        code, out, _ = w.run("check")
        self.assertEqual(code, 0, out)
        self.assertNotIn("sub/build/notes.md", (w.root / "projects/alpha/INDEX.md")
                         .read_text(encoding="utf-8"))

    def _installed(self) -> Workspace:
        w = self.ws()
        cfg = self.tmp / "config.toml"
        wtext(cfg, (w.root / CFG).read_text(encoding="utf-8").replace(
            f'engine = "{__version__}"', f'engine = "{SERIES}"'))
        (w.root / CFG).unlink()
        (w.root / layout.GOV_DIR).rmdir()
        with contextlib.redirect_stdout(io.StringIO()):
            installer.main(["install", "--root", str(w.root), "--config", str(cfg),
                            "--no-report"])
        return w

    def test_a_tool_file_that_cannot_be_written_rolls_everything_back(self):
        # The first tool file is written, the next fails: the written one is put back too.
        from unittest import mock
        w = self._installed()
        gov = w.root / layout.GOV_DIR
        before = {p: p.read_bytes() for p in sorted(w.root.rglob("*")) if p.is_file()
                  and ".git" not in p.parts}
        def partial(dest_dir):
            (dest_dir / installer.TOOL_FILES[0]).write_bytes(b"half an upgrade\n")
            raise PermissionError(13, "Permission denied",
                                  str(dest_dir / installer.TOOL_FILES[-1]))
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(installer, "_write_tool_files", side_effect=partial), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = installer.main(["upgrade", "--root", str(w.root), "--no-report"])
        self.assertEqual(code, 2)
        self.assertIn("upgrade rolled back:", err.getvalue())
        self.assertIn("Permission denied — nothing was changed", err.getvalue())
        after = {p: p.read_bytes() for p in sorted(w.root.rglob("*")) if p.is_file()
                 and ".git" not in p.parts}
        self.assertEqual(after, before)
        self.assertIn(f'engine = "{SERIES}"', (w.root / CFG).read_text(encoding="utf-8"))
        self.assertTrue((gov / installer.TOOL_FILES[0]).is_file())

    def test_a_tool_file_that_cannot_be_put_back_is_named(self):
        from unittest import mock
        w = self._installed()
        def failing(dest_dir):
            (dest_dir / installer.TOOL_FILES[0]).write_bytes(b"half an upgrade\n")
            raise PermissionError(13, "Permission denied")
        real = Path.write_bytes
        def no_restore(self, data):
            if self.parent.name == "bin" and data != b"half an upgrade\n":
                raise PermissionError(13, "Permission denied")
            return real(self, data)
        err = io.StringIO()
        with mock.patch.object(installer, "_write_tool_files", side_effect=failing), \
                mock.patch.object(Path, "write_bytes", no_restore), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = installer.main(["upgrade", "--root", str(w.root), "--no-report"])
        self.assertEqual(code, 2)
        self.assertNotIn("nothing was changed", err.getvalue())
        self.assertIn(f"but {layout.GOV_DIR}/{installer.TOOL_FILES[0]} could not be put back",
                      err.getvalue())
        self.assertIn(f'engine = "{SERIES}"', (w.root / CFG).read_text(encoding="utf-8"))

    def test_the_pin_rewrite_changes_only_the_pin_line(self):
        # A CRLF config: the upgrade rewrites the pin and nothing else, byte for byte.
        w = self.ws()
        cfg = self.tmp / "config.toml"
        wtext(cfg, (w.root / CFG).read_text(encoding="utf-8").replace(
            f'engine = "{__version__}"', f'engine = "{SERIES}"'))
        (w.root / CFG).unlink()
        (w.root / layout.GOV_DIR).rmdir()
        with contextlib.redirect_stdout(io.StringIO()):
            installer.main(["install", "--root", str(w.root), "--config", str(cfg),
                            "--no-report"])
        text = (w.root / CFG).read_text(encoding="utf-8")
        before = text.replace("\n", "\r\n").encode("utf-8")
        (w.root / CFG).write_bytes(before)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = installer.main(["upgrade", "--root", str(w.root), "--no-report"])
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual((w.root / CFG).read_bytes(), before.replace(
            f'engine = "{SERIES}"\r\n'.encode(), f'engine = "{__version__}"\r\n'.encode()))

    def test_a_config_that_does_not_load_is_restored_byte_for_byte(self):
        # CRLF line endings survive the rollback: the original bytes return, not a re-encoded
        # copy of the text.
        w = self.ws()
        cfg = self.tmp / "config.toml"
        wtext(cfg, (w.root / CFG).read_text(encoding="utf-8").replace(
            f'engine = "{__version__}"', f'engine = "{SERIES}"'))
        (w.root / CFG).unlink()
        (w.root / layout.GOV_DIR).rmdir()
        with contextlib.redirect_stdout(io.StringIO()):
            installer.main(["install", "--root", str(w.root), "--config", str(cfg),
                            "--no-report"])
        text = (w.root / CFG).read_text(encoding="utf-8").replace(
            "[blocks]\n", '[blocks]\nplaceholder = "x"\n', 1)
        original = text.replace("\n", "\r\n").encode("utf-8")
        (w.root / CFG).write_bytes(original)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = installer.main(["upgrade", "--root", str(w.root), "--no-report"])
        self.assertNotEqual(code, 0, out.getvalue())
        self.assertIn("upgrade rolled back", err.getvalue())
        self.assertIn("[blocks] placeholder is no longer used", err.getvalue())
        self.assertEqual((w.root / CFG).read_bytes(), original)

    def test_an_unreadable_doc_rolls_the_upgrade_back_and_changes_nothing(self):
        w = self.ws()
        cfg = self.tmp / "config.toml"
        wtext(cfg, (w.root / CFG).read_text(encoding="utf-8").replace(
            f'engine = "{__version__}"', f'engine = "{SERIES}"'))
        (w.root / CFG).unlink()
        (w.root / layout.GOV_DIR).rmdir()
        with contextlib.redirect_stdout(io.StringIO()):
            installer.main(["install", "--root", str(w.root), "--config", str(cfg),
                            "--no-report"])
        log = w.root / "projects/alpha/DECISIONS.md"
        log.write_bytes(log.read_bytes() + b"\xff\xfe not UTF-8\n")   # the block's own file
        gov = w.root / layout.GOV_DIR
        before = {p: p.read_bytes() for p in sorted(gov.rglob("*")) if p.is_file()}
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = installer.main(["upgrade", "--root", str(w.root), "--no-report"])
        self.assertNotEqual(code, 0, out.getvalue())
        self.assertIn("projects/alpha/DECISIONS.md: not valid UTF-8", err.getvalue())
        self.assertIn("nothing was changed", err.getvalue())
        after = {p: p.read_bytes() for p in sorted(gov.rglob("*")) if p.is_file()}
        self.assertEqual(after, before)             # config.toml and installed.toml included


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

    def test_upgrade_lists_new_options_and_keeps_the_answered_ones(self):
        # The merged upgrade: blocks refreshed, then the report with the options not yet
        # answered; the install record's answers survive the manifest rewrite.
        from govern import options
        w, cfg = self.project()
        with contextlib.redirect_stdout(io.StringIO()):
            installer.main(["install", "--root", str(w.root), "--config", str(cfg), "--no-report"])
        first, *rest = options.ids()
        options.record(w.root, [first])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(installer.main(["upgrade", "--root", str(w.root)]), 0)
        self.assertEqual(options.answered(w.root), {first})
        self.assertIn("  index        ", out.getvalue())
        self.assertIn(f"  options      {len(rest)} to review: {', '.join(rest)}", out.getvalue())
        rep = (w.root / layout.GOV_DIR / "upgrade-report.md").read_text(encoding="utf-8")
        section = rep[rep.index("## New options"):rep.index("## New findings")]
        self.assertNotIn(f"`{first}`", section)
        for cid in rest:
            self.assertIn(f"`{cid}`", section)

    def test_a_list_valued_registry_license_never_breaks_options_or_upgrade(self):
        w, cfg = self.project()
        reg = w.root / "projects.toml"
        wtext(reg, reg.read_text(encoding="utf-8").replace(
            'license = "MIT"', 'license = ["MIT", "Apache-2.0"]')
            + '\n[[project]]\nname = "beta"\ndir = "beta"\ntier = "registered"\n'
              'license = "GPL-3.0"\n')
        with contextlib.redirect_stdout(io.StringIO()):
            installer.main(["install", "--root", str(w.root), "--config", str(cfg), "--no-report"])
        code, out, err = w.run("options", "--json")
        self.assertEqual(code, 0, err)
        lic = next(o for o in json.loads(out) if o["id"] == "licenses")
        self.assertEqual(lic["suggestion"],
                         "2 registry entries; licenses: GPL-3.0, MIT + Apache-2.0")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(installer.main(["upgrade", "--root", str(w.root)]), 0)
        self.assertIn("## New options", (w.root / layout.GOV_DIR / "upgrade-report.md")
                      .read_text(encoding="utf-8"))

    def test_an_upgrade_from_the_old_check_name_stays_green_and_reports_the_rename(self):
        # A project written for 0.4.1, when the check was `licences`: the old gate (a stand-in
        # engine that reports nothing) passed, the upgrade passes, and the report names the
        # rename as a new finding, so the project sees what to rename.
        w = self.ws(extra='\n[checks.licences]\nlevel = "error"\n')
        cfg = self.tmp / "config.toml"
        shutil.move(str(w.root / CFG), str(cfg))
        (w.root / layout.GOV_DIR).rmdir()
        old = layout.engines_dir(w.home) / "0.4.1" / "govern"
        old.mkdir(parents=True)
        wtext(old / "__init__.py", '__version__ = "0.4.1"\n')
        wtext(old / "cli.py", "def main(root=None, prog=None):\n    return 0\n")
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(w.home)
        self.addCleanup(lambda: os.environ.__setitem__("HOME", old_home) if old_home
                        else os.environ.pop("HOME", None))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(installer.main(["install", "--root", str(w.root), "--config",
                                             str(cfg), "--no-report"]), 0)
        pinned = w.root / CFG
        wtext(pinned, pinned.read_text(encoding="utf-8").replace(
            f'engine = "{__version__}"', 'engine = "0.4.1"'))
        said = ("'licences' is now 'licenses' (the old name still works; rename it)")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(installer.main(["upgrade", "--root", str(w.root)]), 0,
                             err.getvalue())
        self.assertIn(said, err.getvalue())
        rep = (w.root / layout.GOV_DIR / "upgrade-report.md").read_text(encoding="utf-8")
        self.assertIn(f"- **warn** {CFG} [checks]: {said}", rep[rep.index("## New findings"):])
        code, out, _ = w.run("check")
        self.assertEqual(code, 0, out)

    def test_load_warnings_are_said_and_read_as_unchanged_by_the_upgrade_report(self):
        # The old gate prints `warning: ...` for a docs entry it can never govern, and the
        # report reads that line as a finding: the new side counts the same warning, so it is
        # unchanged, never "no longer reported". Install and upgrade say it too.
        w, cfg = self.project()
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            f'engine = "{__version__}"', f'engine = "{SERIES}"').replace(
            "[projects]\n", '[projects]\ndocs = ["**/*.md", ".claude/x.md"]\n', 1))
        shutil.copytree(ENGINE / "govern", layout.engines_dir(w.home) / __version__ / "govern",
                        ignore=shutil.ignore_patterns("__pycache__"))
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(w.home)
        self.addCleanup(lambda: os.environ.__setitem__("HOME", old_home) if old_home
                        else os.environ.pop("HOME", None))
        said = "warning: [projects] docs entry '.claude/x.md' is never governed"
        for argv in (["install", "--root", str(w.root), "--config", str(cfg), "--no-report"],
                     ["upgrade", "--root", str(w.root)]):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(installer.main(argv), 0, err.getvalue())
            self.assertEqual(err.getvalue().count(said), 1, argv[0])
        self.assertIn(" 0 no longer reported", out.getvalue())
        rep = (w.root / layout.GOV_DIR / "upgrade-report.md").read_text(encoding="utf-8")
        self.assertNotIn(".claude/x.md", rep[rep.index("## New findings"):])

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

    def unreasoned(self, w: Workspace, extra: str = "") -> None:
        """Take away the fixture's `reasons.statuses`, so its re-allowed 'superseded' is a
        widening with no reason; `extra` goes in the same [checks.decision-log] table."""
        cfg = w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(REASON_LINE, extra))

    def test_widened_list_names_only_the_loosening_side(self):
        # The fixture's own [checks.decision-log] re-allows 'superseded':
        # statuses = ["locked", "provisional", "superseded"]. It also drops 'deferred', which
        # tightens — a tightening must never be named as loosening.
        w = self.ws()
        self.unreasoned(w)
        code, _, err = w.run("check")
        self.assertEqual(code, 2)
        self.assertIn("project: [checks.decision-log] statuses adds 'superseded' — loosens past "
                      "what the project inherits without a reason: say why in "
                      "[checks.decision-log.reasons] statuses, or keep the inherited list", err)
        self.assertNotIn("deferred", err)
        wtext(w.root / CFG, (w.root / CFG).read_text(encoding="utf-8").replace(
            'statuses = ["locked", "provisional", "superseded"]',
            'statuses = ["locked", "provisional", "superseded"]\n\n[checks.decision-log.reasons]'
            '\nstatuses = "our older entries predate the standard"'))
        code, out, err = w.run("check")
        self.assertNotEqual(code, 2, err)
        self.assertNotIn("statuses adds", out + err)

    def test_table_reason_does_not_silence_a_list_loosening(self):
        # A reason written for one setting (max_words) must never silently excuse another
        # (statuses re-allowing 'superseded') — only a per-setting reasons.statuses does.
        w = self.ws()
        self.unreasoned(w, 'max_words = 400\nreason = "measured elsewhere"\n')
        code, _, err = w.run("check")
        self.assertEqual(code, 2)
        self.assertIn("[checks.decision-log] statuses adds 'superseded'", err)
        wtext(w.root / CFG, (w.root / CFG).read_text(encoding="utf-8").replace(
            'reason = "measured elsewhere"',
            'reason = "measured elsewhere"\n\n[checks.decision-log.reasons]\n'
            'statuses = "our older entries predate the standard"'))
        code, out, err = w.run("check")
        self.assertNotEqual(code, 2, err)
        self.assertNotIn("statuses adds", out + err)

    def test_emptied_required_list_needs_a_reason(self):
        w = self.ws(extra='required_fields = ["Rule"]\n')
        code, _, err = w.run("check")
        self.assertEqual(code, 2)
        self.assertIn("project: [checks.decision-log] required_fields drops 'Why' — loosens "
                      "past what the project inherits without a reason: say why in "
                      "[checks.decision-log.reasons] required_fields", err)
        wtext(w.root / CFG, (w.root / CFG).read_text(encoding="utf-8").replace(
            'required_fields = ["Rule"]',
            'required_fields = ["Rule"]\nreason = "traps carry no Why field"'))
        code, _, err = w.run("check")
        self.assertIn("required_fields drops", err)   # a table-level reason does not cover it
        wtext(w.root / CFG, (w.root / CFG).read_text(encoding="utf-8").replace(
            REASON_LINE, 'reasons = { statuses = "older entries predate the standard", '
                         'required_fields = "traps carry no Why field" }\n'))
        code, out, err = w.run("check")
        self.assertNotEqual(code, 2, err)
        self.assertNotIn("required_fields drops", out + err)

    def test_emptying_an_anything_goes_list_needs_a_reason(self):
        w = self.ws(extra='\n[checks.doc-frontmatter]\nworking_statuses = []\n')
        code, _, err = w.run("check")
        self.assertEqual(code, 2)
        self.assertIn("project: [checks.doc-frontmatter] working_statuses drops 'active', "
                      "'held', 'planned', 'complete', 'superseded' — loosens past what the "
                      "project inherits without a reason", err)
        wtext(w.root / CFG, (w.root / CFG).read_text(encoding="utf-8").replace(
            "working_statuses = []",
            'working_statuses = []\n\n[checks.doc-frontmatter.reasons]\n'
            'working_statuses = "this project writes free-text status notes"'))
        code, out, err = w.run("check")
        self.assertNotEqual(code, 2, err)
        self.assertNotIn("working_statuses drops", out + err)

    def test_every_widened_list_of_a_check_is_named_at_once(self):
        w = self.ws()
        self.unreasoned(w, 'required_fields = ["Rule"]\n')
        code, _, err = w.run("check")
        self.assertEqual(code, 2)
        self.assertIn("statuses adds 'superseded'", err)
        self.assertIn("required_fields drops 'Why'", err)

    def test_without_require_reasons_a_widened_list_is_a_warning(self):
        w = self.ws()
        self.unreasoned(w)
        cfg = w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            "schema = 1", "schema = 1\nrequire_reasons = false", 1))
        code, out, err = w.run("check")
        self.assertNotEqual(code, 2, err)
        self.assertIn("config: [checks.decision-log] statuses adds 'superseded' — loosens past "
                      "what the project inherits with no reason (say why in reasons.statuses)",
                      out)

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
        code, _, err = w.run("check")
        self.assertEqual(code, 2)
        self.assertIn("project: [checks.decision-history] labels drops 'Superseded' — loosens "
                      "past what the project inherits without a reason: say why in "
                      "[checks.decision-history.reasons] labels", err)
        wtext(w.root / CFG, (w.root / CFG).read_text(encoding="utf-8").replace(
            'labels = ["Earlier", "Previously", "Formerly", "Was"]',
            'labels = ["Earlier", "Previously", "Formerly", "Was"]\n\n'
            '[checks.decision-history.reasons]\n'
            'labels = "\'Superseded\' collides with our status of the same name"'))
        code, out, err = w.run("check")
        self.assertNotEqual(code, 2, err)
        self.assertNotIn("labels drops", out + err)


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
        cfg = w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(REASON_LINE, ""))
        code, _, err = w.run("check")
        self.assertEqual(code, 2)
        self.assertIn("project: [checks.decision-log] statuses adds 'provisional', 'superseded'",
                      err)

    def test_list_widening_is_silent_when_the_project_matches_the_profile(self):
        prof = self.make_profile(self.tmp / "prof", '[checks.decision-log]\nstatuses = '
                                 '["locked", "provisional", "superseded"]\n'
                                 'reasons = { statuses = "profile default" }\n')
        w = self.with_profile(prof.as_posix())
        cfg = w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(REASON_LINE, ""))
        code, out, err = w.run("check")
        self.assertNotEqual(code, 2, err)
        self.assertNotIn("statuses adds", out + err)

    def test_list_widening_reports_once_not_also_as_a_profile_override(self):
        # Under require_reasons = false, a project loosening a list its profile set gets the
        # list message naming the values — not also the generic "overrides the profile"
        # warning (one warning per override).
        prof = self.make_profile(self.tmp / "prof", '[checks.decision-log]\nstatuses = '
                                 '["locked"]\nreason = "profile default"\n')
        w = self.with_profile(prof.as_posix())
        cfg = w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(REASON_LINE, "").replace(
            "schema = 1", "schema = 1\nrequire_reasons = false", 1))
        code, out, _ = w.run("check")
        self.assertIn("config: [checks.decision-log] statuses adds", out)
        self.assertNotIn("statuses overrides the profile with no reason", out)

    def test_a_profile_widening_a_list_needs_a_reason_in_either_direction(self):
        # Each is a loosening past the engine standard, refused in the profile as in a project:
        # a value added (looser "more"), a value removed (looser "fewer"), a list emptied where
        # empty means anything goes.
        cases = {
            "adding": ('[checks.decision-log]\nstatuses = ["locked", "provisional", "deferred", '
                       '"superseded"]\n', "[checks.decision-log] statuses adds 'superseded'",
                       '[checks.decision-log.reasons]\nstatuses = "old logs"\n'),
            "removing": ('[checks.decision-history]\nlabels = ["Earlier"]\n',
                         "[checks.decision-history] labels drops 'Previously', 'Formerly', "
                         "'Superseded', 'Was'",
                         '[checks.decision-history.reasons]\nlabels = "one label here"\n'),
            "emptying": ('[checks.doc-frontmatter]\nworking_statuses = []\n',
                         "[checks.doc-frontmatter] working_statuses drops 'active', 'held', "
                         "'planned', 'complete', 'superseded'",
                         '[checks.doc-frontmatter.reasons]\nworking_statuses = "free text"\n'),
        }
        for name, (table, finding, reasons) in cases.items():
            with self.subTest(name):
                prof = self.make_profile(self.tmp / f"prof-{name}", table)
                (self.tmp / name).mkdir()
                w = Workspace(self.tmp / name)
                cfg = w.root / CFG
                wtext(cfg, cfg.read_text(encoding="utf-8").replace(
                    "schema = 1", f'schema = 1\nprofile = "{prof.as_posix()}"', 1))
                code, _, err = w.run("check")
                self.assertEqual(code, 2)
                self.assertIn(f"profile {prof.as_posix()} (principles.toml): {finding} — "
                              f"loosens past the engine standard without a reason: say why "
                              f"under [checks.", err)
                self.assertIn("in the profile's principles.toml (a pinned profile: then tag "
                              "it and move the project's pin)", err)
                wtext(prof / "principles.toml", table + reasons)
                code, _, err = w.run("check")
                self.assertNotEqual(code, 2, err)

    def test_profile_may_not_set_layout(self):
        prof = self.make_profile(self.tmp / "prof", "[projects]\ndocs = []\n")
        code, _, err = self.with_profile(prof.as_posix()).run("check")
        self.assertEqual(code, 2)
        self.assertIn("a profile holds [checks.*] and [dialect] only", err)

    def test_a_profile_dialect_value_is_checked_against_its_choices(self):
        prof = self.make_profile(self.tmp / "prof", '[dialect]\nagent_turns_prose = "never"\n')
        code, _, err = self.with_profile(prof.as_posix()).run("check")
        self.assertEqual(code, 2)
        self.assertIn(f"profile {prof.as_posix()} (principles.toml): [dialect] agent_turns_prose "
                      f"= 'never' is not one of ('forbid', 'must-match')", err)

    def test_an_unknown_profile_dialect_key_names_the_profile(self):
        prof = self.make_profile(self.tmp / "prof", '[dialect]\nheading_style = "x"\n')
        code, _, err = self.with_profile(prof.as_posix()).run("check")
        self.assertEqual(code, 2)
        self.assertIn(f"profile {prof.as_posix()} (principles.toml): unknown [dialect] key "
                      f"'heading_style'", err)

    def test_a_profile_dialect_value_of_the_wrong_type_is_named(self):
        prof = self.make_profile(self.tmp / "prof", "[dialect]\nmarkers = 3\n")
        code, _, err = self.with_profile(prof.as_posix()).run("check")
        self.assertEqual(code, 2)
        self.assertIn(f"profile {prof.as_posix()} (principles.toml): [dialect] markers: "
                      f"expected str, got int", err)

    def test_a_valid_profile_dialect_value_loads(self):
        prof = self.make_profile(self.tmp / "prof", '[dialect]\nagent_turns_prose = "forbid"\n'
                                 '\n[dialect.reasons]\nagent_turns_prose = "principle"\n')
        code, _, err = self.with_profile(prof.as_posix()).run("check")
        self.assertNotEqual(code, 2, err)

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


class DocsEntryUnderAnAlwaysExcludedDir(Base):
    """An explicit `docs` entry under a directory the docs scan always leaves out matches
    nothing the gate governs. Loading the config says so once, on every command, naming the
    entry and the exclude that wins — a warning, never a new error."""

    def _ws(self, projects_docs: str, workspace_docs: str | None = None) -> Workspace:
        w = self.ws()
        cfg = w.root / CFG
        body = cfg.read_text(encoding="utf-8").replace(
            'required_docs = ["DECISIONS.md"]',
            f'required_docs = ["DECISIONS.md"]\ndocs = {projects_docs}')
        if workspace_docs is not None:
            body = body.replace('docs = ["AGENTS.md", "governance/*.md"]',
                                f"docs = {workspace_docs}")
        wtext(cfg, body)
        w.write("projects/alpha/.claude/x.md", fm() + "# X\n")
        return w

    WARNING = ("warning: [projects] docs entry '.claude/x.md' is never governed: the "
               "always-applied exclude '**/.claude/**' wins — move the doc, or drop the entry")

    def test_an_explicit_entry_under_claude_warns_once(self):
        w = self._ws('["**/*.md", ".claude/x.md"]')
        code, out, err = w.run("check")
        self.assertEqual(code, 0, out)
        self.assertEqual(err.count(self.WARNING), 1, err)
        self.assertEqual(err.count("warning:"), 1, err)
        self.assertNotIn(".claude", out)

    def test_every_command_says_it_and_parsed_output_is_unchanged(self):
        w = self._ws('["**/*.md", ".claude/x.md"]')
        code, out, err = w.run("next-id", "--project", "alpha")
        self.assertEqual((code, out), (0, "A-101\n"))
        self.assertIn(self.WARNING, err)

    def test_a_workspace_entry_names_its_own_section(self):
        w = self._ws('["**/*.md"]', '["AGENTS.md", "governance/*.md", ".context-gate/*.md"]')
        _, _, err = w.run("check")
        self.assertIn("warning: [workspace] docs entry '.context-gate/*.md' is never governed: "
                      "the always-applied exclude '**/.context-gate/**' wins", err)

    def test_a_non_string_projects_entry_is_a_config_error_naming_it(self):
        code, out, err = self._ws('["**/*.md", 1]').run("next-id", "--project", "alpha")
        self.assertEqual(code, 2, out + err)
        self.assertIn("[projects] docs[1]: expected str, got int", err)

    def test_a_non_string_workspace_entry_is_a_config_error_naming_it(self):
        code, out, err = self._ws('["**/*.md"]', '[1]').run("check")
        self.assertEqual(code, 2, out + err)
        self.assertIn("[workspace] docs[0]: expected str, got int", err)

    def test_a_glob_that_reaches_excluded_dirs_is_not_an_entry_under_one(self):
        _, _, err = self._ws('["**/*.md"]').run("check")
        self.assertNotIn("warning:", err)


class DefaultDocExcludesAtAnyDepth(Base):
    """The always-excluded directories are left out at any depth, by the docs scan and by
    `measure` alike — one rule, so what adopt measured is what the gate governs."""

    NESTED = ("sub/node_modules/x.md", "sub/.claude/x.md", "sub/deep/build/x.md",
              "sub/Build/x.md", "sub/Dist/x.md", "sub/deep/Vendor/x.md", "sub/Target/x.md")

    def _ws(self) -> Workspace:
        w = self.ws()
        for rel in self.NESTED:                     # a doc_type the gate would reject
            w.write(f"projects/alpha/{rel}", fm(doc_type="manual") + "# Not governed\n")
        w.write("projects/alpha/sub/real.md", fm("reference") + "# Governed\n")
        return w

    def test_nested_excluded_dirs_are_not_governed(self):
        w = self._ws()
        code, out, _ = w.run("check")
        self.assertEqual(code, 0, out)
        self.assertNotIn("x.md", out)
        from govern.context import Context
        cfg = config.load(w.root, w.home)
        ctx = Context(root=w.root, home=w.home, cfg=cfg, registry=registry.load(cfg),
                      prog="govern")
        alpha = ctx.registry.find("alpha")
        self.assertIn("sub/real.md", [rel for rel, _ in ctx.governed_docs(alpha.gov)])
        self.assertEqual([rel for rel, _ in ctx.governed_docs(alpha.gov) if "x.md" in rel], [])

    def test_nested_excluded_dirs_are_not_measured(self):
        from govern import measure
        m = measure.measure(self._ws().root)
        alpha = next(s for s in m.scopes if s.dir == "projects/alpha")
        self.assertEqual(alpha.doc_dirs["sub"], (1, 1))

    def test_a_nested_docs_entry_names_the_exclude_that_wins(self):
        from govern.context import default_exclude
        self.assertEqual(default_exclude("sub/.claude/x.md"), "**/.claude/**")
        self.assertEqual(default_exclude("sub/deep/build/x.md"), "**/build/**")
        self.assertIsNone(default_exclude("sub/building/x.md"))
        self.assertIsNone(default_exclude("build.md"))
        # In any case, on every OS: the engine before these rules matched `Build/` on Windows.
        self.assertEqual(default_exclude("sub/Build/x.md"), "**/build/**")
        self.assertEqual(default_exclude("Vendor/x.md"), "**/vendor/**")
        self.assertIsNone(default_exclude("sub/Building/x.md"))


class GeneratedBlocksWords(Base):
    """A generated block's markers are HTML comments, so stripping comments first left
    no block to strip and every generated row counted toward the word ratchets. Only the prose a
    reader writes counts; a comment and a generated index add nothing."""

    PROSE = "Current state: shipping the parser."          # 5 words

    def _handoff(self) -> Workspace:
        w = self.ws(extra="""
[checks.handoff-words]
max_words = 5

[checks.doc-frontmatter]
max_working_words = 5
""")
        cfg = w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            'project = [{ file = "DECISIONS.md", id = "decision-index" }]',
            'project = [{ file = "DECISIONS.md", id = "decision-index" },\n'
            '           { file = "working-files/HANDOFF.md", id = "doc-registry" }]'))
        for n in range(4):
            w.write(f"projects/alpha/reference-{n}.md",
                    fm("reference") + f"# Reference {n}\n")
        w.write("projects/alpha/working-files/HANDOFF.md",
                fm("working", status="active") + self.PROSE + "\n\n"
                "<!-- a note for the next session, never read as prose -->\n\n"
                "<!-- t:generated:start id=doc-registry -->\n"
                "<!-- t:generated:end id=doc-registry -->\n")
        w.index()
        return w

    def test_word_count_counts_only_prose(self):
        w = self._handoff()
        body = text.parse_frontmatter(text.read(
            w.root / "projects/alpha/working-files/HANDOFF.md"))[1]
        self.assertIn("reference-3.md", body)              # the index really has rows
        self.assertEqual(text.word_count(body, text.Markers("t")), 5)

    def test_generated_index_adds_nothing_to_the_ratchets(self):
        w = self._handoff()
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertEqual(code, 0, out)
        self.assertNotIn("words", out)
        w.run("baseline", "--allow-raise")
        baseline = json.loads((w.root / layout.BASELINE).read_text(encoding="utf-8"))
        self.assertEqual([k for k in baseline if "words" in k], [], baseline)


class LocalLayerTracked(Base):
    """`local-layer`: a `local.toml` git tracks is an error, one it does not is silent."""

    def setUp(self) -> None:
        super().setUp()
        self.w = self.ws()
        self.env = {**os.environ, "HOME": str(self.w.home), "XDG_CONFIG_HOME": str(self.w.home)}
        self.git("init", "-q")

    def git(self, *args: str) -> None:
        """git under the temp HOME: a global ignore never makes `add -f` vacuous."""
        subprocess.run(["git", "-C", str(self.w.root), *args], env=self.env, check=True,
                       capture_output=True)

    def check(self) -> tuple[int, str, str]:
        return self.w.run("check")

    def test_an_untracked_local_toml_is_silent(self):
        self.w.write(layout.LOCAL, '[governance]\nengine = "0.5.2-beta.1"\n')
        code, out, err = self.check()
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("local.toml", out + err)

    def test_a_tracked_local_toml_fails_the_gate(self):
        self.w.write(layout.LOCAL, '[governance]\nengine = "0.5.2-beta.1"\n')
        self.git("add", "-f", layout.LOCAL)
        code, out, err = self.check()
        self.assertNotEqual(code, 0, out + err)
        self.assertIn("local.toml is tracked by git", out + err)

    def test_no_local_toml_is_silent(self):
        from unittest import mock
        from govern.checks import repo
        with mock.patch.object(repo, "git", side_effect=AssertionError("git was called")):
            code, out, err = self.check()
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("local-layer", out + err)

    def test_a_governance_root_in_a_subdirectory_of_the_repo(self):
        """The repo is the temp dir, the governance root its `ws/` subdirectory."""
        shutil.rmtree(self.w.root / ".git")
        self.git_at(self.tmp, "init", "-q")
        self.w.write(layout.LOCAL, '[governance]\nengine = "0.5.2-beta.1"\n')
        code, out, err = self.check()
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("local.toml", out + err)
        self.git_at(self.tmp, "add", "-f", f"ws/{layout.LOCAL}")
        code, out, err = self.check()
        self.assertNotEqual(code, 0, out + err)
        self.assertIn("local.toml is tracked by git", out + err)

    def test_no_git_repo_at_all_is_silent(self):
        shutil.rmtree(self.w.root / ".git")
        self.w.write(layout.LOCAL, '[governance]\nengine = "0.5.2-beta.1"\n')
        code, out, err = self.check()
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("local.toml", out + err)
        self.assertNotIn("Traceback", out + err)

    def git_at(self, where: Path, *args: str) -> None:
        subprocess.run(["git", "-C", str(where), *args], env=self.env, check=True,
                       capture_output=True)


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
