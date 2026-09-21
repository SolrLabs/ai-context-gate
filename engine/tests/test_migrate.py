"""`govern migrate`: moving a project's logs and traps from other common shapes onto the
standard mechanically.

Fixtures cover those shapes: sections grouping decisions one heading level too deep,
hand-written numbered trap indexes over bullet bodies, and a superseded decision that may or may
not name its successor.

    python3 -m unittest discover -s engine/tests -k migrate
"""
from __future__ import annotations

import contextlib
import io
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))

from govern import __version__, blocks, cli, config, installer, layout, migrate, registry  # noqa: E402
from govern.context import Context  # noqa: E402

CFG = layout.CONFIG

CONFIG = """
[governance]
engine = "{version}"
schema = 1

[dialect]
markers = "gv"

[registry]
file = "projects.toml"
entries = "project"

[workspace]
required_docs = []
docs = ["governance/*.md"]
decision_log = "governance/DECISIONS.md"

[projects]
required_docs = []
trap_glob = "*traps*.md"

[blocks]
project = [{{ file = "DECISIONS.md", id = "decision-index" }}{trap_block}]

[checks.decision-log]
statuses = ["locked", "provisional", "superseded"]
"""

TRAP_BLOCK = ', { glob = "working-files/*traps*.md", id = "trap-index" }'
# The block shape migrate's own report suggests for a hand-written index that spans several
# files: one file holds the marker pair, `sources` reads the rest.
TRAP_BLOCK_SOURCES = (', { file = "working-files/traps.md", id = "trap-index", '
                      'sources = "working-files/*traps*.md" }')

REGISTRY = """
[workspace]
id_prefix = "W"
id_range = "1-99"

[[project]]
name = "alpha"
dir = "alpha"
tier = "full"
governance = "projects/alpha"
id_prefix = "D"
id_range = "500-599"
"""

FM = ("---\ndoc_type: reference\npurpose: test\naudience: agent\nload_when: test\n"
     "last_reviewed: 2026-09-18\n---\n")

DECISIONS_INDEX = ("<!-- gv:generated:start id=decision-index -->\n"
                   "<!-- gv:generated:end id=decision-index -->\n")


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                   check=True, capture_output=True)


def wtext(path: Path, content: str, newline: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline=newline) as fh:
        fh.write(content)


class Fixture:
    """A minimal governance root whose logs use another shape than the standard, with
    its own git
    repo so the dirty-file refusal and the report's `git add`/`git commit` commands have
    something real to check."""

    def __init__(self, tmp: Path, *, decisions_text: str, trap_files: dict[str, str] | None = None,
                declare_trap_block: bool = True, trap_block: str = TRAP_BLOCK,
                git_repo: bool = True, engine_pin: str | None = None, bom: bool = False,
                registry_text: str | None = None) -> None:
        self.root = tmp / "root"
        self.home = tmp / "home"
        self.home.mkdir()
        self.root.mkdir()
        wtext(self.root / CFG, CONFIG.format(
            version=engine_pin or __version__, trap_block=trap_block if declare_trap_block else ""))
        wtext(self.root / "projects.toml", registry_text if registry_text is not None else REGISTRY)
        wtext(self.root / "governance/DECISIONS.md",
             FM + "# Decisions\n\n" + DECISIONS_INDEX + "\n## W-1 — Root decision\n\n"
             "**Status:** locked\n\n**Rule:** r.\n\n**Why:** w.\n")
        wtext(self.root / "projects/alpha/DECISIONS.md",
             ("﻿" + decisions_text) if bom else decisions_text)
        for name, content in (trap_files or {}).items():
            wtext(self.root / "projects/alpha/working-files" / name, content)
        if git_repo:
            git(self.root, "init", "-q")
            git(self.root, "add", "-A")
            git(self.root, "commit", "-qm", "initial")

    def log(self) -> Path:
        return self.root / "projects/alpha/DECISIONS.md"

    def trap(self, name: str) -> Path:
        return self.root / "projects/alpha/working-files" / name

    def context(self) -> Context:
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        try:
            cfg = config.load(self.root, layout.home())
            return Context(root=self.root, home=layout.home(), cfg=cfg,
                           registry=registry.load(cfg), prog="govern")
        finally:
            if old_home is not None:
                os.environ["HOME"] = old_home

    def run(self, apply_: bool) -> tuple[int, str]:
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = migrate.run(self.root, apply_)
        finally:
            if old_home is not None:
                os.environ["HOME"] = old_home
        return code, out.getvalue() + err.getvalue()

    def report(self) -> str:
        return (self.root / layout.GOV_DIR / "migration-report.md").read_text(encoding="utf-8")

    def commit(self) -> None:
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "migrate")


# ---------------------------------------------------------------------------- fixture text

SECTIONS_LOG = FM + "# Decisions\n\n" + DECISIONS_INDEX + """
## Server-side platform (D-500–D-599)

### D-500 — Use UTC timestamps

**Status:** locked

**Rule:** Store and compare times in UTC.

**Why:** One clock across machines.

### D-501 — Cache invalidation on deploy

**Status:** superseded by D-503 (2026-01-15: replaced by versioned keys)

**Rule:** old rule.

**Why:** old reason.

## Client tooling (D-520–D-599)

Some prose explaining the client tooling section, before any entry.

### D-520 — Code comments explain the code

**Status:** locked

**Rule:** r.

**Why:** w.

## D-503 — Versioned cache keys

**Status:** locked

**Rule:** new rule.

**Why:** new reason.

## D-104 — Log levels are set per module

**Status:** superseded

Superseded 2026-01-15 by a later design. No successor is named anywhere in this entry.

**Rule:** r.

**Why:** w.
"""

TRAPS_INDEX = FM + """
# Traps

## Index

1. A committed text fixture is LF, whatever the machine that wrote it had
2. A byte-identical restore is not enough — touch it
3. Never cache an error response — body in [platform-traps.md](platform-traps.md)
49. Retired 2026-01-15 — the check moved into CI

---

- **1. A committed text fixture is LF, whatever the machine wrote it with.** Normalise before counting, or assert something other than length.

- **2. A byte-identical restore is not enough — touch it.** Restoring a file with an older mtime leaves the stale build output in place.
"""

PLATFORM_TRAPS = FM + """
# Platform traps

- **3. Never cache an error response.** The error is served until the entry expires.
"""


def base_fixture(tmp: Path, **kw) -> Fixture:
    return Fixture(tmp, decisions_text=SECTIONS_LOG,
                  trap_files={"traps.md": TRAPS_INDEX, "platform-traps.md": PLATFORM_TRAPS},
                  **kw)


# ---------------------------------------------------------------------------- more fixture text

FENCED_LOG = FM + "# Decisions\n\n" + DECISIONS_INDEX + """
Documentation example (not real data):

```
## Old Section (D-900-D-999)

### D-900 — Example

**Topic:** Old Section
```

## D-600 — Real decision

**Status:** locked

**Rule:** r.

**Why:** w.
"""

FENCED_TRAPS = FM + """
# Traps

Documentation example (not real data):

```
## Index

1. Example item

- **7. Example only.** Body of an example that should not migrate.
```

## T-10 — Real trap

**Bites when:** always.

Body.
"""

AMBIGUOUS_SUCCESSOR_LOG = FM + "# Decisions\n\n" + DECISIONS_INDEX + """
## D-504 — Status line picks the right successor

**Status:** superseded by D-506

**Rule:** This replaced D-400, which was superseded by D-500 in turn. Now superseded by D-501.

**Why:** w.

## D-505 — Ambiguous successor is left for a person

**Status:** superseded

**Rule:** This replaced D-400, which was superseded by D-500 in turn. Now superseded by D-501.

**Why:** w.

## D-506 — Where D-504 went

**Status:** locked

**Rule:** r.

**Why:** w.
"""

MIDSENTENCE_TRAPS = FM + """
# Traps

## Index

1. First trap
2. Environment example

- **1. First trap.** Body one.
- **2. Environment variable expansion does not contain a space**, and on Unix systems the missing quoting still executes.
"""

BARE_EOF_LOG = (FM + "# Decisions\n\n" + DECISIONS_INDEX +
               "\n## Trailing Topic (D-700-D-799)\n\n### D-700 — Bare heading with nothing after it")

INDEX_TABLE_TRAPS = FM + """
# Traps

## Index

| # | Trap |
|---|---|
| 1 | Example |

- **1. Example.** Body.
"""

INDEX_BULLETS_TRAPS = FM + """
# Traps

## Index

- 1. First trap
- 2. Second trap

- **1. First trap.** Body.

- **2. Second trap.** Body.
"""

INDEX_WRAPPED_TRAPS = FM + """
# Traps

## Index

1. First trap
   continues on a wrapped line
2. Second trap

- **1. First trap.** Body.

- **2. Second trap.** Body.
"""

DUP_A = FM + "# Traps A\n\n- **21. Version check race.** Body A.\n"
DUP_B = FM + "# Traps B\n\n- **21. Version check race, different body.** Body B.\n"


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()


class SectionsToTopics(Base):
    def test_sections_flatten_with_topic_and_reparse_correctly(self):
        w = base_fixture(self.tmp)
        code, _ = w.run(apply_=True)
        self.assertEqual(code, 0)
        text = w.log().read_text(encoding="utf-8")
        self.assertNotIn("## Server-side platform", text)
        self.assertIn("## D-500 — Use UTC timestamps", text)
        self.assertIn("**Topic:** Server-side platform", text)

        ctx = w.context()
        parsed = ctx.grammar.parse_file(w.log())
        by_id = {e.ident: e for e in parsed.entries}
        self.assertEqual(by_id["D-500"].topic, "Server-side platform")
        self.assertEqual(by_id["D-500"].title, "Use UTC timestamps")
        self.assertEqual(by_id["D-503"].topic, None)   # never sectioned: no topic invented
        # D-520's section had prose before its first entry, so it is deliberately left as the
        # old, one-level-too-deep heading — still malformed, and that is reported, not hidden.
        self.assertEqual(parsed.malformed, [(29, "### D-520 — Code comments explain the code")])

    def test_entry_that_already_has_a_topic_keeps_it(self):
        text = SECTIONS_LOG.replace(
            "### D-500 — Use UTC timestamps\n\n**Status:** locked",
            "### D-500 — Use UTC timestamps\n\n"
            "**Topic:** Already set\n\n**Status:** locked")
        w = Fixture(self.tmp, decisions_text=text)
        w.run(apply_=True)
        result = w.log().read_text(encoding="utf-8")
        self.assertIn("**Topic:** Already set", result)
        self.assertEqual(result.count("**Topic:**"), 1)

    def test_prose_before_first_entry_is_left_and_reported(self):
        w = base_fixture(self.tmp)
        code, out = w.run(apply_=False)
        self.assertEqual(code, 0)
        self.assertIn("would migrate", out)
        report = w.report()
        self.assertIn("## Client tooling (D-520–D-599)", w.log().read_text(encoding="utf-8"))
        self.assertIn("Client tooling", report)
        self.assertIn("cannot be placed mechanically", report)


class SupersededToPointer(Base):
    def test_named_successor_becomes_a_pointer(self):
        w = base_fixture(self.tmp)
        w.run(apply_=True)
        text = w.log().read_text(encoding="utf-8")
        self.assertIn("## D-501 — Replaced by D-503", text)
        self.assertNotIn("old rule", text)

        ctx = w.context()
        parsed = ctx.grammar.parse_file(w.log())
        entry = next(e for e in parsed.entries if e.ident == "D-501")
        self.assertEqual(entry.replaced_by, "D-503")
        self.assertEqual(entry.body.strip(), "")

    def test_no_successor_named_is_left_alone_and_reported(self):
        w = base_fixture(self.tmp)
        w.run(apply_=True)
        text = w.log().read_text(encoding="utf-8")
        self.assertIn("## D-104 — Log levels are set per module", text)
        self.assertIn("Superseded 2026-01-15 by a later design", text)
        report = w.report()
        self.assertIn("D-104 is superseded with no successor named", report)

    def test_successor_missing_from_the_log_is_not_turned_into_a_pointer(self):
        text = SECTIONS_LOG.replace("superseded by D-503", "superseded by D-999")
        w = Fixture(self.tmp, decisions_text=text)
        w.run(apply_=True)
        result = w.log().read_text(encoding="utf-8")
        # its section still flattens (mig 1 is independent of mig 2); only the pointer is refused
        self.assertIn("## D-501 — Cache invalidation on deploy", result)
        self.assertIn("old rule", result)
        self.assertIn("D-999, which is not in this log", w.report())


class BulletTrapsToHeadings(Base):
    def test_bullets_become_headings_and_index_becomes_a_block(self):
        w = base_fixture(self.tmp)
        code, _ = w.run(apply_=True)
        self.assertEqual(code, 0)
        text = w.trap("traps.md").read_text(encoding="utf-8")
        self.assertIn("## T-1 — A committed text fixture is LF, whatever the machine wrote it "
                     "with", text)
        self.assertNotIn("- **1.", text)
        self.assertNotIn("## Index\n\n1.", text)
        self.assertIn("<!-- gv:generated:start id=trap-index -->", text)
        self.assertIn("<!-- gv:generated:end id=trap-index -->", text)

        platform = w.trap("platform-traps.md").read_text(encoding="utf-8")
        self.assertIn("## T-3 — Never cache an error response", platform)

        ctx = w.context()
        scope = ctx.registry.find("alpha")
        nums = {e.num: e.title for doc in ctx.trap_files(scope)
               for e in ctx.grammar.traps(doc, ctx.trap_prefix)}
        self.assertEqual(nums[1], "A committed text fixture is LF, whatever the machine wrote "
                                  "it with")
        self.assertEqual(nums[2], "A byte-identical restore is not enough — touch it")
        self.assertEqual(nums[3], "Never cache an error response")

    def test_dropped_index_number_with_no_body_is_reported_as_the_maximum(self):
        w = base_fixture(self.tmp)
        w.run(apply_=False)
        report = w.report()
        self.assertIn("T-49 has no body anywhere and was dropped from the index", report)
        self.assertIn("highest number ever mentioned", report)

    def test_missing_block_declaration_is_reported(self):
        w = base_fixture(self.tmp, declare_trap_block=False)
        w.run(apply_=False)
        report = w.report()
        self.assertIn('id = "trap-index"', report)
        # traps.md held the hand-written Index, so it — and only it — becomes the block's
        # target; T-3's body lives in platform-traps.md, so `sources` picks that up too. The
        # suggestion is a `glob`, never a `file`: a `[[blocks.project]]` entry applies to every
        # governed scope, and a `file` naming this exact path would error in any scope that
        # does not have one — a `glob` matching nothing there is simply empty.
        self.assertIn('glob = "working-files/traps.md"', report)
        self.assertNotIn('file = "working-files/traps.md"', report)
        self.assertIn('sources = "working-files/*traps*.md"', report)

    def test_block_already_declared_is_not_reported_again(self):
        w = base_fixture(self.tmp, declare_trap_block=True)
        w.run(apply_=False)
        report = w.report()
        self.assertNotIn("## Config", report)

    def test_bites_when_missing_is_noted_in_a_separate_notes_section(self):
        w = base_fixture(self.tmp)
        w.run(apply_=False)
        report = w.report()
        notes = report.split("## Notes", 1)[1].split("## Could not be migrated", 1)[0]
        self.assertIn("T-1 has no **Bites when:** line", notes)
        # the plan predicts trap-entries' own warning for exactly this — it belongs under
        # "Expected", never under "should not happen".
        expected = report.split("Expected after this migration", 1)[1]
        self.assertIn("T-1 has no **Bites when:** — the trap index shows it as a dash",
                     expected)
        should_not = report.split("New after this migration", 1)[1] \
            if "New after this migration" in report else ""
        self.assertNotIn("T-1 has no **Bites when:**", should_not)


class DryRunAndApply(Base):
    def test_dry_run_changes_nothing_on_disk(self):
        w = base_fixture(self.tmp)
        before = w.log().read_bytes()
        before_traps = w.trap("traps.md").read_bytes()
        code, out = w.run(apply_=False)
        self.assertEqual(code, 0)
        self.assertEqual(w.log().read_bytes(), before)
        self.assertEqual(w.trap("traps.md").read_bytes(), before_traps)
        self.assertTrue((w.root / layout.GOV_DIR / "migration-report.md").is_file())
        self.assertIn("would migrate", out)

    def test_apply_refuses_when_a_touched_file_is_dirty(self):
        w = base_fixture(self.tmp)
        wtext(w.log(), w.log().read_text(encoding="utf-8") + "\n<!-- uncommitted edit -->\n")
        code, out = w.run(apply_=True)
        self.assertEqual(code, 2)
        self.assertIn("uncommitted changes", out)
        # nothing was migrated: the section is still there
        self.assertIn("## Server-side platform", w.log().read_text(encoding="utf-8"))

    def test_apply_writes_the_report_with_git_commands(self):
        w = base_fixture(self.tmp)
        w.run(apply_=True)
        report = w.report()
        self.assertIn("**Applied.**", report)
        self.assertIn("git add", report)
        self.assertIn("git commit", report)
        self.assertIn(str(w.root), report)

    def test_idempotent_second_run_reports_nothing_left_to_migrate(self):
        w = base_fixture(self.tmp)
        w.run(apply_=True)
        w.commit()
        code, out = w.run(apply_=True)
        self.assertEqual(code, 0)
        self.assertIn("migrated 0 file(s)", out)
        status = subprocess.run(["git", "-C", str(w.root), "status", "--porcelain",
                                 "--", "projects"], capture_output=True, text=True, check=True)
        self.assertEqual(status.stdout.strip(), "")

    def test_installer_wires_the_migrate_subcommand(self):
        w = base_fixture(self.tmp)
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(w.home)
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                code = installer.main(["migrate", "--root", str(w.root)])
        finally:
            if old_home is not None:
                os.environ["HOME"] = old_home
        self.assertEqual(code, 0)
        self.assertIn("would migrate", out.getvalue())


class CRLFPreserved(Base):
    def test_crlf_line_endings_survive_the_migration(self):
        crlf_text = SECTIONS_LOG.replace("\n", "\r\n")
        w = Fixture(self.tmp, decisions_text=crlf_text)
        w.run(apply_=True)
        raw = w.log().read_bytes()
        self.assertIn(b"\r\n", raw)
        # every line ending is CRLF: no lone "\n" survives once every "\r\n" is stripped out
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))
        self.assertIn(b"D-500", raw)
        self.assertIn(b"Replaced by D-503", raw)

    def test_crlf_trap_file_survives_the_migration(self):
        crlf_traps = TRAPS_INDEX.replace("\n", "\r\n")
        w = Fixture(self.tmp, decisions_text=SECTIONS_LOG, trap_files={"traps.md": crlf_traps})
        w.run(apply_=True)
        raw = w.trap("traps.md").read_bytes()
        self.assertIn(b"\r\n", raw)
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))
        self.assertIn(b"T-1", raw)


class FencedExamplesLeftAlone(Base):
    def test_fenced_bullet_index_and_topic_are_left_alone(self):
        w = Fixture(self.tmp, decisions_text=FENCED_LOG, trap_files={"traps.md": FENCED_TRAPS},
                   registry_text=REGISTRY.replace("500-599", "600-699"))
        before_log = w.log().read_bytes()
        before_traps = w.trap("traps.md").read_bytes()
        code, _ = w.run(apply_=True)
        self.assertEqual(code, 0)
        # the fenced section, index and topic are examples, not live markup: nothing
        # inside the fence is touched, even though the file also holds a real, already-flat
        # entry and a real, already-migrated trap right next to it
        self.assertEqual(w.log().read_bytes(), before_log)
        self.assertEqual(w.trap("traps.md").read_bytes(), before_traps)


class SuccessorResolution(Base):
    def test_status_line_wins_over_distracting_body_mentions(self):
        body = ("**Status:** superseded by D-502\n\n"
               "This replaced D-400, which was superseded by D-500 in turn. Now superseded "
               "by D-501.")
        succ, found = migrate._successor(body)
        self.assertEqual(succ, "D-502")

    def test_bulleted_status_line_with_labelled_successor(self):
        # The one-line field style: `- **Status:** … · **Superseded by:** D-023 (date)`.
        body = ("- **Status:** superseded · **Origin:** review, 2026-01-10 · "
                "**Superseded by:** D-023 (2026-09-01)\n\nThis replaced D-400.")
        succ, found = migrate._successor(body)
        self.assertEqual(succ, "D-023")

    def test_several_distinct_body_successors_are_ambiguous(self):
        body = ("**Status:** superseded\n\n"
               "This replaced D-400, which was superseded by D-500 in turn. Now superseded "
               "by D-501.")
        succ, found = migrate._successor(body)
        self.assertIsNone(succ)
        self.assertEqual(set(found), {"D-500", "D-501"})

    def test_status_line_successor_and_ambiguous_body_end_to_end(self):
        w = Fixture(self.tmp, decisions_text=AMBIGUOUS_SUCCESSOR_LOG)
        code, _ = w.run(apply_=True)
        self.assertEqual(code, 0)
        text = w.log().read_text(encoding="utf-8")
        self.assertIn("## D-504 — Replaced by D-506", text)
        self.assertIn("## D-505 — Ambiguous successor is left for a person", text)
        report = w.report()
        self.assertIn("D-505 names several possible successors (D-500, D-501) and none is on "
                     "the **Status:** line", report)


class TrapIndexSources(Base):
    def test_sources_link_traps_across_files_with_no_generated_blocks_finding(self):
        w = base_fixture(self.tmp, declare_trap_block=True, trap_block=TRAP_BLOCK_SOURCES)
        code, _ = w.run(apply_=True)
        self.assertEqual(code, 0)
        ctx = w.context()
        self.assertEqual(cli.cmd_index(ctx), 0)
        findings = cli.collect(ctx, ctx.registry.scopes, workspace=True)
        errors = [m for cid, f in findings for m in f.errors if cid == "generated-blocks"]
        self.assertEqual(errors, [])
        text = w.trap("traps.md").read_text(encoding="utf-8")
        self.assertIn("T-1", text)
        self.assertIn("T-2", text)
        self.assertIn("T-3", text)
        # T-3's body lives in platform-traps.md, so its row links there
        self.assertIn("platform-traps.md", text)

    def test_sources_on_a_non_trap_index_block_is_rejected(self):
        root = self.tmp / "root"
        root.mkdir()
        text = CONFIG.format(version=__version__, trap_block="").replace(
            '{ file = "DECISIONS.md", id = "decision-index" }',
            '{ file = "DECISIONS.md", id = "decision-index", sources = "*.md" }')
        wtext(root / CFG, text)
        wtext(root / "projects.toml", REGISTRY)
        with self.assertRaises(config.ConfigError) as cm:
            config.load(root, root)
        self.assertIn("sources", str(cm.exception))
        self.assertIn("trap-index", str(cm.exception))

    def test_without_sources_behavior_is_unchanged(self):
        # The old, glob-based block (one marker pair required per matched file) is untouched by
        # the `sources` feature: migrate only ever adds markers to the file that held the
        # hand-written Index, so platform-traps.md — matched by the glob, never given markers —
        # is reported missing exactly as it would have been before `sources` existed.
        w = base_fixture(self.tmp, declare_trap_block=True, trap_block=TRAP_BLOCK)
        code, _ = w.run(apply_=True)
        self.assertEqual(code, 0)
        ctx = w.context()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.cmd_index(ctx)
        self.assertEqual(code, 1)
        self.assertIn("platform-traps.md :: trap-index (markers)", out.getvalue())
        text = w.trap("traps.md").read_text(encoding="utf-8")
        self.assertIn("T-1", text)
        self.assertIn("T-2", text)
        self.assertNotIn("platform-traps.md", text)   # no sources: only this file's own traps


class BeforeAfterOverlay(Base):
    def test_generated_blocks_reads_migrated_text_and_predicts_stale(self):
        w = base_fixture(self.tmp, declare_trap_block=True, trap_block=TRAP_BLOCK_SOURCES)
        ctx = w.context()
        p = migrate.plan(ctx)
        before = migrate._findings(ctx)
        after = migrate._findings(ctx, p.edits)
        before_msgs = [m for _, _, m in before if "trap-index" in m]
        after_msgs = [m for _, _, m in after if "trap-index" in m]
        self.assertTrue(any("has no markers" in m for m in before_msgs), before_msgs)
        self.assertFalse(any("has no markers" in m for m in after_msgs), after_msgs)
        self.assertTrue(any("is stale" in m for m in after_msgs), after_msgs)

    def test_finding_that_only_moved_lines_is_neither_fixed_nor_new(self):
        w = base_fixture(self.tmp)
        ctx = w.context()
        p = migrate.plan(ctx)
        before = migrate._findings(ctx)
        after = migrate._findings(ctx, p.edits)
        before_line = next(m for _, _, m in before
                          if "D-520" in m and "is not a decision entry heading" in m)
        after_line = next(m for _, _, m in after
                         if "D-520" in m and "is not a decision entry heading" in m)
        self.assertNotEqual(before_line, after_line)   # the line number really did move
        self.assertEqual(migrate._norm(before_line), migrate._norm(after_line))
        report_path = self.tmp / "report.md"
        migrate._write_report(ctx, report_path, p, before, after, applied=False)
        report = report_path.read_text(encoding="utf-8")
        fixed_section = report.split("Fixed by this migration:", 1)[1].split("\n\n", 1)[0] \
            if "Fixed by this migration:" in report else ""
        new_section = report.split("New after this migration", 1)[-1] \
            if "New after this migration" in report else ""
        self.assertNotIn("is not a decision entry heading", fixed_section)
        self.assertNotIn("is not a decision entry heading", new_section)


class BOMHandling(Base):
    def test_bom_survives_apply(self):
        w = base_fixture(self.tmp, bom=True)
        code, _ = w.run(apply_=True)
        self.assertEqual(code, 0)
        raw = w.log().read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        text = raw.decode("utf-8-sig")
        self.assertIn("## D-500 — Use UTC timestamps", text)


class UnreadableFiles(Base):
    def test_non_utf8_trap_file_gives_exit_2(self):
        w = base_fixture(self.tmp)
        bad_bytes = b"\xff\xfe not valid utf-8"
        bad_path = w.trap("platform-traps.md")
        bad_path.write_bytes(bad_bytes)
        before_log = w.log().read_bytes()
        code, out = w.run(apply_=True)
        self.assertEqual(code, 2)
        self.assertIn(str(bad_path), out)
        self.assertNotIn("Traceback", out)
        self.assertEqual(bad_path.read_bytes(), bad_bytes)
        self.assertEqual(w.log().read_bytes(), before_log)


class ApplyGitBlockers(Base):
    def test_apply_refuses_when_a_file_is_outside_any_git_repo(self):
        w = base_fixture(self.tmp, git_repo=False)
        before_log = w.log().read_bytes()
        code, out = w.run(apply_=True)
        self.assertEqual(code, 2)
        self.assertIn("not inside a git repo", out)
        self.assertEqual(w.log().read_bytes(), before_log)

    def test_apply_refuses_when_git_status_fails(self):
        w = base_fixture(self.tmp)
        before_log = w.log().read_bytes()
        with mock.patch("govern.migrate.git", return_value=None):
            code, out = w.run(apply_=True)
        self.assertEqual(code, 2)
        self.assertIn("git status", out)
        self.assertEqual(w.log().read_bytes(), before_log)


class AtomicApply(Base):
    def test_partial_replace_failure_restores_every_original(self):
        w = base_fixture(self.tmp)
        before = {p: p.read_bytes() for p in
                 (w.log(), w.trap("traps.md"), w.trap("platform-traps.md"))}
        real_replace = os.replace
        calls: list = []

        def flaky(src, dst):
            calls.append(dst)
            if len(calls) == 2:
                raise OSError("disk full")
            real_replace(src, dst)

        with mock.patch("govern.migrate.os.replace", side_effect=flaky):
            code, out = w.run(apply_=True)
        self.assertEqual(code, 2)
        self.assertGreaterEqual(len(calls), 2)
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)
        report = w.report()
        self.assertIn("Apply failed and was rolled back", report)
        self.assertIn("Nothing was changed", report)


class BulletTitleContinuations(Base):
    def test_mid_sentence_title_and_missing_blank_lines(self):
        w = Fixture(self.tmp, decisions_text=SECTIONS_LOG,
                   trap_files={"traps.md": MIDSENTENCE_TRAPS})
        code, _ = w.run(apply_=True)
        self.assertEqual(code, 0)
        text = w.trap("traps.md").read_text(encoding="utf-8")
        self.assertIn("## T-1 — First trap", text)
        self.assertIn("## T-2 — Environment variable expansion does not contain a space", text)
        self.assertIn("Environment variable expansion does not contain a space, and on Unix "
                     "systems the missing quoting still executes.", text)
        self.assertNotIn("\n, and on Unix", text)
        self.assertIn("\n\n## T-2 —", text)     # a blank line inserted, though the source had none
        report = w.report()
        self.assertIn("T-2: the bold text does not end its sentence", report)


class BodylessEntryAtEOF(Base):
    def test_bare_heading_at_end_of_file_gets_its_topic(self):
        w = Fixture(self.tmp, decisions_text=BARE_EOF_LOG,
                   registry_text=REGISTRY.replace("500-599", "700-799"))
        code, _ = w.run(apply_=True)
        self.assertEqual(code, 0)
        text = w.log().read_text(encoding="utf-8")
        self.assertIn("## D-700 — Bare heading with nothing after it\n\n"
                     "**Topic:** Trailing Topic", text)
        report = w.report()
        self.assertIn("'Trailing Topic (D-700-D-799)' -> **Topic:** Trailing Topic on 1 entry",
                     report)


class MalformedIndexLeftAlone(Base):
    def test_table_index_is_left_alone(self):
        self._check(INDEX_TABLE_TRAPS, "table, bullets, or wrapped/indented lines")

    def test_bulleted_index_is_left_alone(self):
        self._check(INDEX_BULLETS_TRAPS, "table, bullets, or wrapped/indented lines")

    def test_wrapped_index_is_left_alone(self):
        self._check(INDEX_WRAPPED_TRAPS, "table, bullets, or wrapped/indented lines")

    def _check(self, content, needle):
        w = Fixture(self.tmp, decisions_text=SECTIONS_LOG, trap_files={"traps.md": content})
        code, _ = w.run(apply_=True)
        self.assertEqual(code, 0)
        text = w.trap("traps.md").read_text(encoding="utf-8")
        self.assertIn("## Index", text)                     # the hand-written heading survives
        self.assertNotIn("<!-- gv:generated:start id=trap-index -->", text)
        self.assertIn("## T-1 —", text)     # the real bullets still migrate independently
        report = w.report()
        self.assertIn(needle, report)
        self.assertNotIn("replaced by the generated", report)


class DuplicateTrapNumber(Base):
    def test_same_number_two_files_neither_migrated_both_reported(self):
        w = Fixture(self.tmp, decisions_text=SECTIONS_LOG,
                   trap_files={"traps.md": DUP_A, "other-traps.md": DUP_B})
        code, _ = w.run(apply_=True)
        self.assertEqual(code, 0)
        a = w.trap("traps.md").read_text(encoding="utf-8")
        b = w.trap("other-traps.md").read_text(encoding="utf-8")
        self.assertIn("- **21.", a)
        self.assertIn("- **21.", b)
        self.assertNotIn("## T-21", a)
        self.assertNotIn("## T-21", b)
        report = w.report()
        self.assertIn("T-21 has a body in more than one file", report)
        self.assertIn("traps.md:", report)
        self.assertIn("other-traps.md:", report)


class PinRefusal(Base):
    def test_pinned_to_other_engine_version_refuses_and_says_to_upgrade(self):
        w = base_fixture(self.tmp, engine_pin="0.1.0")
        code, out = w.run(apply_=False)
        self.assertEqual(code, 2)
        self.assertIn("upgrade", out.lower())

    def test_readme_migrate_section_says_to_upgrade_first(self):
        readme = (ENGINE / "README.md").read_text(encoding="utf-8")
        migrate_section = readme.split("`migrate` moves a project", 1)[1]
        self.assertIn("upgrade", migrate_section[:600].lower())


class CommitCommandsAndParagraphs(Base):
    def test_commit_paths_with_spaces_are_quoted(self):
        root = self.tmp / "root"
        home = self.tmp / "home"
        home.mkdir()
        root.mkdir()
        wtext(root / CFG, CONFIG.format(version=__version__, trap_block=""))
        registry_text = REGISTRY.replace('governance = "projects/alpha"',
                                         'governance = "projects/alpha two"')
        wtext(root / "projects.toml", registry_text)
        wtext(root / "governance/DECISIONS.md", FM + "# Decisions\n\n" + DECISIONS_INDEX +
             "\n## W-1 — Root decision\n\n**Status:** locked\n\n**Rule:** r.\n\n**Why:** w.\n")
        wtext(root / "projects/alpha two/DECISIONS.md", SECTIONS_LOG)
        git(root, "init", "-q")
        git(root, "add", "-A")
        git(root, "commit", "-qm", "initial")
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(home)
        try:
            code = migrate.run(root, False)
        finally:
            if old_home is not None:
                os.environ["HOME"] = old_home
        self.assertEqual(code, 0)
        report = (root / layout.GOV_DIR / "migration-report.md").read_text(encoding="utf-8")
        self.assertIn("git add 'projects/alpha two/DECISIONS.md'", report)

    def test_engine_line_and_applied_are_separate_paragraphs(self):
        w = base_fixture(self.tmp)
        w.run(apply_=True)
        report = w.report()
        self.assertIn(f"Engine {__version__}.\n\n**Applied.**", report)

    def test_log_shared_by_two_scopes_is_planned_once(self):
        registry_text = REGISTRY + (
            '\n[[project]]\nname = "alpha-mirror"\ndir = "alpha"\ntier = "full"\n'
            'governance = "projects/alpha"\nid_prefix = "D"\nid_range = "500-599"\n')
        w = Fixture(self.tmp, decisions_text=SECTIONS_LOG, registry_text=registry_text)
        ctx = w.context()
        p = migrate.plan(ctx)
        topic_changes = [c.detail for c in p.changes
                         if c.path == w.log() and "Server-side platform" in c.detail]
        self.assertEqual(len(topic_changes), 1)
        prose_problems = [pr.reason for pr in p.problems
                          if pr.path == w.log() and "cannot be placed mechanically" in pr.reason]
        self.assertEqual(len(prose_problems), 1)


class TwoGitRepos(Base):
    def test_two_repos_get_separate_commit_blocks(self):
        root = self.tmp / "root"
        home = self.tmp / "home"
        home.mkdir()
        root.mkdir()
        registry_text = """
[workspace]
id_prefix = "W"
id_range = "1-99"

[[project]]
name = "alpha"
dir = "alpha"
tier = "full"
governance = "projects/alpha"
id_prefix = "D"
id_range = "500-599"

[[project]]
name = "beta"
dir = "beta"
tier = "full"
governance = "projects/beta"
id_prefix = "D"
id_range = "600-699"
"""
        wtext(root / CFG, CONFIG.format(version=__version__, trap_block=""))
        wtext(root / "projects.toml", registry_text)
        wtext(root / "governance/DECISIONS.md", FM + "# Decisions\n\n" + DECISIONS_INDEX +
             "\n## W-1 — Root decision\n\n**Status:** locked\n\n**Rule:** r.\n\n**Why:** w.\n")
        beta_log = (SECTIONS_LOG.replace("D-500", "D-600").replace("D-501", "D-601")
                   .replace("D-503", "D-603").replace("D-520", "D-620").replace("D-104", "D-604"))
        wtext(root / "projects/alpha/DECISIONS.md", SECTIONS_LOG)
        wtext(root / "projects/beta/DECISIONS.md", beta_log)
        for sub in ("projects/alpha", "projects/beta"):
            git(root / sub, "init", "-q")
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(home)
        try:
            code = migrate.run(root, False)
        finally:
            if old_home is not None:
                os.environ["HOME"] = old_home
        self.assertEqual(code, 0)
        report = (root / layout.GOV_DIR / "migration-report.md").read_text(encoding="utf-8")
        commit_section = report.split("## Commit", 1)[1]
        self.assertEqual(commit_section.count("In `"), 2)
        self.assertIn(str(root / "projects/alpha"), commit_section)
        self.assertIn(str(root / "projects/beta"), commit_section)


# ---------------------------------------------------------------------------- edge cases

POINTER_IN_SECTION_LOG = FM + "# Decisions\n\n" + DECISIONS_INDEX + """
## Server (D-500–D-599)

### D-500 — Kept

**Status:** locked

**Rule:** r.

**Why:** w.

### D-501 — Replaced by D-500

### D-502 — Last one

**Status:** locked

**Rule:** r.

**Why:** w.
"""


class PointerInsideSectionKeepsNoBody(Base):
    def test_pointer_nested_in_a_section_gets_no_topic_line(self):
        # D-501 is already a pointer (no body), but is one heading level too deep, inside
        # a section like any other entry there — flattening it must not give it a **Topic:**
        # line, which would leave it carrying a body forever (nothing later strips it, since
        # `_migrate_pointers` skips an entry that already has `replaced_by` set).
        w = Fixture(self.tmp, decisions_text=POINTER_IN_SECTION_LOG)
        code, _ = w.run(apply_=True)
        self.assertEqual(code, 0)
        text = w.log().read_text(encoding="utf-8")
        self.assertIn("## D-501 — Replaced by D-500", text)
        between = text.split("## D-501", 1)[1].split("## D-502", 1)[0]
        self.assertNotIn("**Topic:**", between)
        ctx = w.context()
        entry = next(e for e in ctx.grammar.parse_file(w.log()).entries if e.ident == "D-501")
        self.assertEqual(entry.body.strip(), "")
        self.assertEqual(entry.replaced_by, "D-500")
        # D-500 and D-502, real entries in the same section, still get their Topic.
        self.assertIn("**Topic:** Server", text)

    def test_superseded_entry_whose_successor_is_not_in_the_log_keeps_its_topic(self):
        # D-500 names D-999, which this log does not hold, so no pointer is made — and the
        # entry must not lose its Topic to a pointer that never happens.
        log = FM + "# Decisions\n\n" + DECISIONS_INDEX + (
            "\n## Auth (D-500–D-599)\n\n### D-500 — Old auth\n\n**Status:** superseded\n\n"
            "Superseded by D-999.\n")
        w = Fixture(self.tmp, decisions_text=log)
        code, _ = w.run(apply_=True)
        self.assertEqual(code, 0)
        text = w.log().read_text(encoding="utf-8")
        self.assertIn("## D-500 — Old auth", text)
        self.assertIn("**Topic:** Auth", text.split("## D-500", 1)[1])


BULLET_CONFLICTS_TRAPS = {
    "traps.md": FM + "# Traps\n\n## T-3 — Already a heading\n\n**Bites when:** x.\n\nBody.\n\n"
               "- **4. New bullet.** body four.\n",
    "platform-traps.md": FM + "# Platform traps\n\n"
                        "- **3. Same number as a heading elsewhere.** body.\n",
}


class BulletConflictsWithExistingHeading(Base):
    def test_bullet_matching_an_existing_heading_number_is_left_as_a_bullet(self):
        w = Fixture(self.tmp, decisions_text=SECTIONS_LOG, trap_files=BULLET_CONFLICTS_TRAPS)
        code, _ = w.run(apply_=True)
        self.assertEqual(code, 0)
        platform = w.trap("platform-traps.md").read_text(encoding="utf-8")
        self.assertIn("- **3.", platform)
        self.assertNotIn("## T-3", platform)
        traps = w.trap("traps.md").read_text(encoding="utf-8")
        self.assertIn("## T-3 — Already a heading", traps)
        self.assertIn("## T-4 —", traps)   # the non-conflicting bullet still migrates
        report = w.report()
        self.assertIn("T-3 is already a heading", report)
        self.assertIn("needs a person to merge them", report)
        # `trap-ids` must never see a duplicate: only one T-3 exists after migration.
        ctx = w.context()
        findings = cli.collect(ctx, ctx.registry.scopes, workspace=True)
        errors = [m for cid, f in findings for m in f.errors if cid == "trap-ids"]
        self.assertEqual(errors, [])


class GitignoredFileRefusesApply(Base):
    def test_apply_refuses_when_a_touched_file_is_gitignored(self):
        w = Fixture(self.tmp, decisions_text=SECTIONS_LOG,
                   trap_files={"traps.md": FM + "# Traps\n\n- **4. New bullet.** Body four.\n"},
                   git_repo=False)
        wtext(w.root / ".gitignore", "working-files/\n")
        git(w.root, "init", "-q")
        git(w.root, "add", "-A")
        git(w.root, "commit", "-qm", "initial (working-files ignored)")
        before_trap = w.trap("traps.md").read_bytes()
        before_log = w.log().read_bytes()
        code, out = w.run(apply_=True)
        self.assertEqual(code, 2)
        self.assertIn("ignored by git", out)
        # nothing changed: not even the log, which was not itself ignored (all or none).
        self.assertEqual(w.trap("traps.md").read_bytes(), before_trap)
        self.assertEqual(w.log().read_bytes(), before_log)


class TrapIndexFileOrderWithoutSources(Base):
    def test_no_sources_keeps_file_order_even_when_ids_are_out_of_order(self):
        w = base_fixture(self.tmp)
        ctx = w.context()
        path = self.tmp / "order-traps.md"
        wtext(path, FM + "# Traps\n\n## T-10 — Ten\n\n**Bites when:** a.\n\n"
                        "## T-2 — Two\n\n**Bites when:** b.\n")
        text = blocks.trap_index(ctx, path)
        rows = [line for line in text.split("\n") if line.startswith("| T-")]
        self.assertEqual([r.split("|")[1].strip() for r in rows], ["T-10", "T-2"])

    def test_sources_still_sorts_by_id(self):
        w = base_fixture(self.tmp)
        ctx = w.context()
        path = self.tmp / "order-traps.md"
        wtext(path, FM + "# Traps\n\n## T-10 — Ten\n\n**Bites when:** a.\n\n"
                        "## T-2 — Two\n\n**Bites when:** b.\n")
        text = blocks.trap_index(ctx, path, scope=ctx.registry.find("alpha"), sources="*.md")
        rows = [line for line in text.split("\n") if line.startswith("| T-")]
        self.assertEqual([r.split("|")[1].strip() for r in rows], ["T-2", "T-10"])


TWO_SCOPE_REGISTRY = REGISTRY + (
    '\n[[project]]\nname = "beta"\ndir = "beta"\ntier = "full"\n'
    'governance = "projects/beta"\nid_prefix = "D"\nid_range = "600-699"\n')


class TrapBlockSuggestionAcrossScopes(Base):
    def test_two_scopes_one_without_a_trap_index_after_suggested_config_is_clean(self):
        # `beta` has a decision log but no `working-files` directory at all — no trap-shaped
        # content anywhere — so it contributes nothing to the plan (`_plan_traps` returns
        # early), exactly like the member-repo workspace fixture where only one scope has a trap Index.
        w = base_fixture(self.tmp, declare_trap_block=False, registry_text=TWO_SCOPE_REGISTRY)
        wtext(w.root / "projects/beta/DECISIONS.md", FM + "# Decisions\n\n" + DECISIONS_INDEX +
             "\n## D-600 — Beta decision\n\n**Status:** locked\n\n**Rule:** r.\n\n**Why:** w.\n")
        git(w.root, "add", "-A")
        git(w.root, "commit", "-qm", "beta: no trap files at all")

        code, _ = w.run(apply_=True)
        self.assertEqual(code, 0)
        report = w.report()
        # Exactly one suggestion (alpha's), and it is a glob, not a file: a `file` naming this
        # exact path would error in beta, which has no such file at all; a `glob` matching
        # nothing there is simply empty.
        self.assertEqual(report.count("[[blocks.project]]"), 1)
        self.assertIn('glob = "working-files/traps.md"', report)
        self.assertNotIn('file = "working-files/traps.md"', report)

        # Add the suggested block for real, and confirm both scopes are clean.
        cfg_path = w.root / CFG
        wtext(cfg_path, cfg_path.read_text(encoding="utf-8").replace(
            'project = [{ file = "DECISIONS.md", id = "decision-index" }]',
            'project = [{ file = "DECISIONS.md", id = "decision-index" }, '
            '{ glob = "working-files/traps.md", id = "trap-index" }]'))
        ctx = w.context()
        self.assertEqual(cli.cmd_index(ctx), 0)
        findings = cli.collect(ctx, ctx.registry.scopes, workspace=True)
        errors = [m for cid, f in findings for m in f.errors if cid == "generated-blocks"]
        self.assertEqual(errors, [])


    def test_same_index_path_in_two_scopes_gets_one_suggestion_with_sources(self):
        # alpha's Index spans two files (needs `sources`); beta's covers only its own file.
        # Every `[[blocks.project]]` entry applies to every scope, so two entries with the same
        # glob would render alpha's block two ways and leave one of them stale forever.
        w = base_fixture(self.tmp, declare_trap_block=False, registry_text=TWO_SCOPE_REGISTRY)
        wtext(w.root / "projects/beta/DECISIONS.md", FM + "# Decisions\n\n" + DECISIONS_INDEX +
             "\n## D-600 — Beta decision\n\n**Status:** locked\n\n**Rule:** r.\n\n**Why:** w.\n")
        wtext(w.root / "projects/beta/working-files/traps.md",
             FM + "# Traps\n\n## Index\n\n9. Beta trap\n\n---\n\n- **9. Beta trap.** body.\n")
        git(w.root, "add", "-A")
        git(w.root, "commit", "-qm", "beta: a one-file trap index at the same path")

        code, _ = w.run(apply_=False)
        self.assertEqual(code, 0)
        report = w.report()
        self.assertEqual(report.count('glob = "working-files/traps.md"'), 1)
        self.assertIn("sources =", report)

class SuccessorMarkupAndMultiId(Base):
    def test_bold_wrapped_successor_is_recognised(self):
        succ, found = migrate._successor("**Status:** superseded by **D-40**\n\nRule text.")
        self.assertEqual(succ, "D-40")
        self.assertEqual(found, ["D-40"])

    def test_linked_successor_is_recognised(self):
        succ, found = migrate._successor(
            "**Status:** superseded by [D-40](DECISIONS.md#d-40)\n\nRule text.")
        self.assertEqual(succ, "D-40")
        self.assertEqual(found, ["D-40"])

    def test_several_ids_in_one_phrase_with_and_is_ambiguous(self):
        succ, found = migrate._successor("**Status:** superseded\n\nSuperseded by D-40 and D-41.")
        self.assertIsNone(succ)
        self.assertEqual(found, ["D-40", "D-41"])

    def test_several_ids_in_one_phrase_with_comma_is_ambiguous(self):
        succ, found = migrate._successor("**Status:** superseded\n\nSuperseded by D-40, D-41.")
        self.assertIsNone(succ)
        self.assertEqual(found, ["D-40", "D-41"])

    def test_end_to_end_bold_successor_becomes_a_pointer(self):
        text = SECTIONS_LOG.replace("superseded by D-503", "superseded by **D-503**")
        w = Fixture(self.tmp, decisions_text=text)
        code, _ = w.run(apply_=True)
        self.assertEqual(code, 0)
        self.assertIn("## D-501 — Replaced by D-503", w.log().read_text(encoding="utf-8"))


class AbsoluteBlockPatternsRejected(Base):
    def test_helper_detects_posix_and_windows_absolute_patterns(self):
        self.assertTrue(config._is_absolute_pattern("/etc/passwd"))
        self.assertTrue(config._is_absolute_pattern("C:\\traps\\*.md"))
        self.assertTrue(config._is_absolute_pattern("\\\\server\\share\\x.md"))
        self.assertTrue(config._is_absolute_pattern("\\traps\\x.md"))    # rooted, no drive
        self.assertTrue(config._is_absolute_pattern("c:traps*.md"))       # drive-relative
        self.assertFalse(config._is_absolute_pattern("working-files/traps.md"))

    def test_absolute_glob_is_a_config_error_not_a_glob_crash(self):
        root = self.tmp / "root"
        root.mkdir()
        text = CONFIG.format(version=__version__, trap_block="").replace(
            '[{ file = "DECISIONS.md", id = "decision-index" }]',
            '[{ file = "DECISIONS.md", id = "decision-index" }, '
            '{ glob = "/etc/traps*.md", id = "trap-index" }]')
        wtext(root / CFG, text)
        wtext(root / "projects.toml", REGISTRY)
        with self.assertRaises(config.ConfigError) as cm:
            config.load(root, root)
        self.assertIn("absolute", str(cm.exception))

    def test_absolute_sources_is_a_config_error(self):
        root = self.tmp / "root"
        root.mkdir()
        text = CONFIG.format(version=__version__, trap_block="").replace(
            '[{ file = "DECISIONS.md", id = "decision-index" }]',
            '[{ file = "DECISIONS.md", id = "decision-index" }, '
            '{ file = "working-files/traps.md", id = "trap-index", sources = "/traps/*.md" }]')
        wtext(root / CFG, text)
        wtext(root / "projects.toml", REGISTRY)
        with self.assertRaises(config.ConfigError) as cm:
            config.load(root, root)
        self.assertIn("absolute", str(cm.exception))


PERIOD_TITLE_TRAPS = FM + """
# Traps

- **7. Never force-push.** git rewrites history other clones already have.
"""


class MidSentenceRuleRequiresAnUnfinishedSentence(Base):
    def test_title_ending_in_a_period_is_not_treated_as_mid_sentence(self):
        w = Fixture(self.tmp, decisions_text=SECTIONS_LOG,
                   trap_files={"traps.md": PERIOD_TITLE_TRAPS})
        code, _ = w.run(apply_=True)
        self.assertEqual(code, 0)
        text = w.trap("traps.md").read_text(encoding="utf-8")
        self.assertIn("## T-7 — Never force-push\n\ngit rewrites history other clones already "
                     "have.", text)
        report = w.report()
        self.assertNotIn("T-7: the bold text does not end its sentence", report)


SHARED_TRAPS_WITH_INDEX = FM + """
# Traps

## Index

1. New bullet

---

- **1. New bullet.** Body one.
"""


class WorkingDirOutsideScopeNeverTracebacks(Base):
    def test_absolute_working_dir_outside_scope_reports_a_problem_not_a_traceback(self):
        root = self.tmp / "root"
        home = self.tmp / "home"
        home.mkdir()
        root.mkdir()
        shared = root / "shared-traps"
        wtext(shared / "traps.md", SHARED_TRAPS_WITH_INDEX)
        cfg_text = CONFIG.format(version=__version__, trap_block="").replace(
            '[projects]\nrequired_docs = []\ntrap_glob = "*traps*.md"',
            '[projects]\nrequired_docs = []\ntrap_glob = "*traps*.md"\n'
            f'working_dir = "{shared.as_posix()}"')
        wtext(root / CFG, cfg_text)
        wtext(root / "projects.toml", REGISTRY)
        wtext(root / "governance/DECISIONS.md", FM + "# Decisions\n\n" + DECISIONS_INDEX +
             "\n## W-1 — Root decision\n\n**Status:** locked\n\n**Rule:** r.\n\n**Why:** w.\n")
        wtext(root / "projects/alpha/DECISIONS.md", SECTIONS_LOG)
        git(root, "init", "-q")
        git(root, "add", "-A")
        git(root, "commit", "-qm", "initial")
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(home)
        try:
            code = migrate.run(root, False)
        finally:
            if old_home is not None:
                os.environ["HOME"] = old_home
        self.assertEqual(code, 0)    # a clean dry run — never a traceback
        report = (root / layout.GOV_DIR / "migration-report.md").read_text(encoding="utf-8")
        self.assertIn("working_dir", report)
        self.assertIn("outside", report)


OVER_LIMIT_LOG = (FM + "# Decisions\n\n" + DECISIONS_INDEX +
                  "\n## Server-side platform (D-500–D-599)\n\n"
                  "### D-500 — Verbose decision\n\n**Status:** locked\n\n**Rule:** r.\n\n"
                  "**Why:** " + " ".join(["word"] * 260) + ".\n")


class NewlyVisibleFindings(Base):
    def test_over_word_limit_entry_is_newly_visible_not_should_not_happen(self):
        # Before migration, `### D-500` is one heading level too deep — malformed, so nothing
        # reads it as an entry, and its word count is never checked. After, it is a real entry
        # at its own level: the word-limit warning that follows was hidden by the old format,
        # not created by the migration.
        w = Fixture(self.tmp, decisions_text=OVER_LIMIT_LOG)
        code, _ = w.run(apply_=True)
        self.assertEqual(code, 0)
        report = w.report()
        self.assertIn("Newly visible: existing content the old format hid from the gate:", report)
        newly = report.split("Newly visible: existing content the old format hid from the gate:",
                             1)[1].split("## Commit", 1)[0]
        self.assertIn("D-500 is", newly)
        self.assertIn("over decision-log max_words", newly)
        should_not = (report.split("New after this migration", 1)[1]
                     if "New after this migration" in report else "")
        self.assertNotIn("decision-log max_words", should_not)
        self.assertIn("## Next steps", report)
        next_steps = report.split("## Next steps", 1)[1]
        self.assertIn("govern index", next_steps)
        self.assertIn("govern baseline --allow-raise", next_steps)
        self.assertLess(next_steps.index("govern index"),
                        next_steps.index("govern baseline --allow-raise"))

    def test_newly_visible_missing_field_is_not_sent_to_the_baseline(self):
        # A missing **Why:** is a decision-log error the baseline never records: the next
        # steps must say to fix the entry, not to run `baseline`.
        log = FM + "# Decisions\n\n" + DECISIONS_INDEX + (
            "\n## Auth (D-500–D-599)\n\n### D-500 — No why\n\n**Status:** locked\n\n"
            "**Rule:** r.\n")
        w = Fixture(self.tmp, decisions_text=log)
        code, _ = w.run(apply_=True)
        self.assertEqual(code, 0)
        next_steps = w.report().split("## Next steps", 1)[1]
        self.assertNotIn("govern baseline", next_steps)
        self.assertIn("fixed in the entries themselves", next_steps)


if __name__ == "__main__":
    unittest.main()
