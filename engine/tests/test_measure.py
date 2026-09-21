"""`measure`: what a project looks like before it adopts the engine, read from the tree alone.

Fixtures are built in three shapes: a single repo (`single_repo`), a multi-project workspace
(`example_workspace`), and a workspace of member repos (`orbit`). Each is built in a temp dir,
with `git init` wherever git-ignore rules are in play.

    python3 -m unittest discover -s engine/tests -k measure
"""
from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))

from govern import installer, measure, profile  # noqa: E402

FM = ("---\ndoc_type: reference\npurpose: test\naudience: agent\nload_when: test\n"
      "last_reviewed: 2026-09-18\n---\n")
# A decision entry's body, complete enough for the gate: adopt's tests run it on these shapes.
BODY = "\n**Status:** locked\n\n**Rule:** x.\n\n**Why:** y.\n"


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                   check=True, capture_output=True)


def commit(root: Path) -> None:
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "initial")


def put(path: Path, content: str | bytes, newline: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
        return
    with open(path, "w", encoding="utf-8", newline=newline) as fh:
        fh.write(content)


def pair(prefix: str, bid: str, body: str = "") -> str:
    return (f"<!-- {prefix}:generated:start id={bid} -->\n{body}"
            f"<!-- {prefix}:generated:end id={bid} -->\n")


def snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file() and ".git" not in p.parts}


# -------------------------------------------------------------------- the three fixture shapes

def single_repo(root: Path) -> None:
    """A single repo: `docs/DECISIONS.md` with no entries yet, `issues.md` with `BUG-`
    headings beside `HANDOFF.md`, and a design doc whose headings are `### D17` (no hyphen)."""
    put(root / "docs/DECISIONS.md", FM + "# Decisions\n\n## Index\n\n"
        + pair("gov", "decision-index") + "\n```md\n## D-99 — An example inside a fence\n```\n")
    put(root / "docs/issues.md", FM + "# Issues\n\n## BUG-002 — One\n\nx\n\n"
        "## BUG-024 — Two\n\ny\n")
    put(root / "docs/HANDOFF.md", FM + "# Handoff\n\n<!--\n## D-98 — commented out\n-->\n")
    put(root / "docs/design/0001-design.md", FM + "# Design\n\n### D1 — One\n\n"
        "### D17 — Seventeen\n")
    put(root / "engine/README.md", FM + "# Engine\n")
    put(root / "engine/govern/CHANGELOG.md", "# Changelog\n")
    put(root / ".claude/agents/impl.md", "---\nname: impl\ndescription: x\nmodel: opus\n"
        "effort: high\nomitClaudeMd: true\n---\n# Agent\n")
    put(root / "scratch/DECISIONS.md", "## D-500 — ignored by git\n")
    put(root / "node_modules/pkg/DECISIONS.md", "## D-600 — vendored\n")
    put(root / ".gitignore", "scratch/\n")
    commit(root)


WORKSPACE_REGISTRY = """
[workspace]
id_prefix = "W"
id_range = "1-99"

[[project]]
name = "nova-app"
dir = "NovaApp"
tier = "full"
governance = "projects/nova-app"
id_prefix = "O"
id_range = "100-199"
[project.profile]
handoff = "projects/nova-app/working-files/HANDOFF.md"

[[project]]
name = "nova-launcher"
dir = "NovaApp"
tier = "full"
governance = "projects/nova-launcher"
id_prefix = "L"
id_range = "300-399"
[project.profile]
handoff = "projects/nova-launcher/working-files/HANDOFF.md"

[[project]]
name = "helper-bot"
dir = "HelperBot"
tier = "full"
governance = "projects/helper-bot"
id_prefix = "B"
id_range = "200-299"
[project.profile]
handoff = "projects/helper-bot/working-files/HANDOFF.md"

[[project]]
name = "core"
dir = "CORE"
tier = "registered"
governance = ""
"""


def trap_file(title: str, nums: list[int]) -> str:
    rows = "".join(f"| T-{n} | Trap {n} | x |\n" for n in nums)
    bodies = "".join(f"## T-{n} — Trap {n}\n\n**Bites when:** x.\n\n" for n in nums)
    return (FM + f"# {title}\n\n## Index\n\n"
            + pair("ex", "trap-index", "| # | Trap | Bites when |\n|---|---|---|\n" + rows)
            + "\n" + bodies)


def example_workspace(root: Path) -> None:
    """A workspace: `[[project]]` entries holding `handoff` under `profile`, two of them sharing
    the `NovaApp` checkout with governance of their own, `ex:` markers, and heading traps in two
    files that each index their own."""
    put(root / "projects.toml", WORKSPACE_REGISTRY)
    put(root / ".gitignore", "/NovaApp/\n/HelperBot/\n/CORE/\n")
    put(root / "AGENTS.md", FM + "# Agents\n\n" + pair("ex", "agent-roster"))
    put(root / "governance/DECISIONS.md", FM + "# Workspace decisions\n\n"
        + pair("ex", "decision-index") + "\n## W-1 — One\n" + BODY + "\n## W-16 — Sixteen\n"
        + BODY)
    for name, first in (("nova-app", "O-100"), ("nova-launcher", "L-300"), ("helper-bot", "B-200")):
        base = root / "projects" / name
        put(base / "DECISIONS.md", FM + "# Decisions\n\n" + pair("ex", "decision-index")
            + f"\n## {first} — First\n" + BODY)
        put(base / "INDEX.md", FM + "# Index\n\n" + pair("ex", "doc-registry"))
        put(base / "working-files/HANDOFF.md", FM + "# Handoff\n")
    put(root / "projects/helper-bot/working-files/traps.md", trap_file("Traps", [1, 2, 13]))
    put(root / "projects/helper-bot/working-files/traps-build.md",
        trap_file("Traps — Harness", [4, 5, 39]))
    put(root / "projects/helper-bot/working-files/plan-feature.md", FM + "# Plan\n\n"
        "## T-40 — A trap the plan found\n\n## T-41 — And another\n")
    put(root / "projects/nova-app/working-files/traps.md", trap_file("Traps", [1]))
    commit(root)
    # The fork: its own repo, ignored by the workspace, and never measured.
    put(root / "NovaApp/README.md", "## X-5 — the fork's own heading\n")
    commit(root / "NovaApp")
    put(root / "HelperBot/README.md", "# HelperBot\n")


ORBIT_REGISTRY = """
[[repo]]
name = "public"
dir = "orbit-public"
tier = "registered"

[[repo]]
name = "client"
dir = "orbit-client"
tier = "full"
id_prefix = "D"
id_range = "0-499"
[repo.profile]
traps = "docs/working-files/traps.md"

[[repo]]
name = "platform"
dir = "orbit-platform"
tier = "full"
id_prefix = "D"
id_range = "500-599"
"""

SECTIONED = (FM + "# Decision Log\n\n## Index\n\n" + pair("orbit", "decision-index")
             + "\n## Project doctrine (D-001 – D-099)\n\n### D-001 — One\n" + BODY
             + "\n### D-002 — Two\n" + BODY + "\n## Sync (D-100 – D-199)\n\n### D-100 — Hundred\n"
             + BODY)


def orbit(root: Path) -> None:
    """A workspace of member checkouts, each its own repo and ignored by the root: `[[repo]]`
    with no `governance` key, `orbit:` markers, sectioned `### D-` logs, bullet traps over
    three files with one index, traps kept as a section in a second repo, and a gate script of
    its own with a baseline, a test and a git hook."""
    put(root / "repos.toml", ORBIT_REGISTRY)
    put(root / ".gitignore", "orbit-client/\norbit-platform/\norbit-public/\n")
    put(root / "AGENTS.md", FM + "# Agents\n\n" + pair("orbit", "agent-roster"))
    put(root / "governance/DECISIONS.md", FM + "# Workspace decisions\n\n"
        + pair("orbit", "decision-index") + "\n## Decisions (W-1 – W-99)\n\n### W-1 — One\n"
        + BODY)
    put(root / "governance/gate.py", "print('a gate of its own')\n")
    put(root / "governance/gate-baseline.json", "{}\n")
    put(root / "governance/test-gate.py", "\n")
    put(root / "governance/git-hooks/pre-commit", "#!/bin/sh\n")
    commit(root)

    client = root / "orbit-client"
    put(client / "docs/DECISIONS.md", SECTIONED)
    put(client / "docs/INDEX.md", FM + "# Index\n\n" + pair("orbit", "doc-registry"))
    put(client / "docs/working-files/HANDOFF.md", FM + "# Handoff\n")
    put(client / "docs/working-files/traps.md", FM + "# Traps\n\n## Index\n\n"
        "1. First\n2. Second — body in [`platform-traps.md`](platform-traps.md)\n"
        "3. Third — body in headless-traps.md\n\n## Traps\n\n- **1. First.** Body. **Bites when:** x.\n")
    put(client / "docs/working-files/platform-traps.md", FM + "# Traps — platform\n\n"
        "- **2. Second.** Body. **Bites when:** x.\n")
    put(client / "docs/working-files/headless-traps.md", FM + "# Traps — headless\n\n"
        "- **3. Third.** Body. **Bites when:** x.\n")
    put(client / "bin/DECISIONS.md", "## D-900 — build output\n")
    put(client / ".gitignore", "bin/\n")
    commit(client)

    platform = root / "orbit-platform"
    put(platform / "docs/DECISIONS.md", FM + "# Decision Log\n\n" + pair("orbit", "decision-index")
        + "\n## Service (D-500 – D-599)\n\n### D-520 — Twenty\n" + BODY)
    put(platform / "docs/working-files/iteration-log.md", FM + "# Iteration Log\n\n## Traps\n\n"
        "- **A test can be vacuous.** Body.\n")
    commit(platform)


# ---------------------------------------------------------------------------- tests

class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(profile.rmtree, self.tmp)
        self.root = self.tmp / "root"
        self.root.mkdir()


class SingleRepoShape(Base):
    def setUp(self) -> None:
        super().setUp()
        single_repo(self.root)
        self.m = measure.measure(self.root)

    def test_single_repo(self):
        m = self.m
        self.assertIsNone(m.registry)
        self.assertEqual(m.scopes, [])
        self.assertFalse(m.governance_key_missing)
        self.assertEqual(m.workspace.dir, ".")
        self.assertEqual(m.workspace.name, "root")
        self.assertEqual(m.markers, "gov")
        self.assertEqual(m.blocks, [("docs/DECISIONS.md", "decision-index")])

    def test_named_log_with_no_entries_is_the_only_candidate(self):
        # issues.md sits under the working dir and is not named: never a candidate.
        self.assertEqual(self.m.workspace.decision_logs, [measure.DecisionLog(
            path="docs/DECISIONS.md", entries=0, level=2, prefixes=[], named=True)])
        self.assertEqual(self.m.workspace.working_dir, "docs")

    def test_max_ids_count_unhyphenated_headings_but_not_fenced_or_commented_ones(self):
        self.assertEqual(self.m.max_ids, {"D": 17, "BUG": 24})

    def test_doc_dirs_never_list_dot_dirs(self):
        self.assertEqual(self.m.workspace.doc_dirs, {"docs": (4, 4), "engine": (2, 1)})

    def test_a_mixed_doc_dir_lists_its_files_with_frontmatter(self):
        self.assertEqual(self.m.workspace.doc_files, {"engine": ["engine/README.md"]})

    def test_no_nested_checkouts(self):
        self.assertEqual(self.m.subrepos, [])

    def test_ignored_and_vendored_trees_are_never_measured(self):
        self.assertNotIn("scratch", self.m.workspace.doc_dirs)
        self.assertNotIn("node_modules", self.m.workspace.doc_dirs)

    def test_read_only_and_repeatable(self):
        before = snapshot(self.root)
        again = measure.measure(self.root)
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(again, self.m)


class ExampleWorkspaceShape(Base):
    def setUp(self) -> None:
        super().setUp()
        example_workspace(self.root)
        self.m = measure.measure(self.root)

    def scope(self, name: str) -> measure.ScopeMeasure:
        return next(s for s in self.m.scopes if s.name == name)

    def test_registry_and_nested_keys(self):
        m = self.m
        self.assertEqual((m.registry, m.registry_entries), ("projects.toml", "project"))
        self.assertEqual(m.registry_keys, {"handoff": "profile.handoff"})
        self.assertFalse(m.governance_key_missing)

    def test_scopes_are_the_governance_dirs_never_the_shared_checkout(self):
        # `core` has governance = "" and a tier that isn't full: skipped.
        self.assertEqual([(s.name, s.dir) for s in self.m.scopes],
                         [("nova-app", "projects/nova-app"),
                          ("nova-launcher", "projects/nova-launcher"),
                          ("helper-bot", "projects/helper-bot")])
        self.assertNotIn("X", self.m.max_ids)            # the fork's code is never read

    def test_markers(self):
        self.assertEqual(self.m.markers, "ex")
        self.assertIn(("projects/helper-bot/working-files/traps-build.md", "trap-index"),
                      self.m.blocks)
        self.assertIn(("AGENTS.md", "agent-roster"), self.m.blocks)

    def test_trap_files_each_with_their_own_index(self):
        self.assertEqual(self.scope("helper-bot").trap_sets, [measure.TrapSet(
            files=["projects/helper-bot/working-files/traps.md",
                   "projects/helper-bot/working-files/traps-build.md"],
            index_spans_files=False, bullets=False, glob="traps*.md", has_index=True)])
        self.assertEqual(self.scope("nova-app").trap_sets[0].glob, "traps.md")

    def test_plan_with_trap_headings_is_not_a_decision_log(self):
        bot = self.scope("helper-bot")
        self.assertEqual([log.path for log in bot.decision_logs],
                         ["projects/helper-bot/DECISIONS.md"])
        self.assertEqual(bot.working_dir, "working-files")
        self.assertEqual(self.m.max_ids["T"], 41)

    def test_nested_checkouts_whether_or_not_the_registry_names_them(self):
        # NovaApp is a checkout; HelperBot is only a directory, not a checkout.
        self.assertEqual(self.m.subrepos, ["NovaApp"])

    def test_workspace_excludes_every_member(self):
        ws = self.m.workspace
        self.assertEqual([log.path for log in ws.decision_logs], ["governance/DECISIONS.md"])
        self.assertEqual(ws.decision_logs[0].prefixes, ["W"])
        self.assertEqual(ws.doc_dirs, {"governance": (1, 1)})


class OrbitShape(Base):
    def setUp(self) -> None:
        super().setUp()
        orbit(self.root)
        self.m = measure.measure(self.root)

    def scope(self, name: str) -> measure.ScopeMeasure:
        return next(s for s in self.m.scopes if s.name == name)

    def test_registry_without_governance_key(self):
        m = self.m
        self.assertEqual((m.registry, m.registry_entries), ("repos.toml", "repo"))
        self.assertTrue(m.governance_key_missing)
        self.assertEqual(m.registry_keys, {})
        self.assertEqual(m.markers, "orbit")

    def test_scope_dirs_measured_though_the_root_ignores_them(self):
        self.assertEqual([(s.name, s.dir) for s in self.m.scopes],
                         [("client", "orbit-client"), ("platform", "orbit-platform")])
        client = self.scope("client")
        self.assertEqual(client.decision_logs, [measure.DecisionLog(
            path="orbit-client/docs/DECISIONS.md", entries=3, level=3, prefixes=["D"],
            named=True, lowest=1)])
        self.assertEqual(client.working_dir, "docs/working-files")
        self.assertEqual(client.doc_dirs, {"docs": (6, 6)})    # bin/ is ignored by the member

    def test_bullet_traps_over_three_files_with_one_index(self):
        self.assertEqual(self.scope("client").trap_sets, [measure.TrapSet(
            files=["orbit-client/docs/working-files/traps.md",
                   "orbit-client/docs/working-files/headless-traps.md",
                   "orbit-client/docs/working-files/platform-traps.md"],
            index_spans_files=True, bullets=True, glob="*traps.md", has_index=True)])

    def test_traps_as_a_section_get_a_note(self):
        self.assertEqual(self.scope("platform").trap_sets, [])
        self.assertIn("orbit-platform/docs/working-files/iteration-log.md: traps kept as a "
                      "'## Traps' section of this file, not in a trap file", self.m.notes)

    def test_nested_checkouts_count_though_the_root_ignores_them(self):
        self.assertEqual(self.m.subrepos, ["orbit-client", "orbit-platform"])

    def test_max_ids(self):
        self.assertEqual(self.m.max_ids, {"D": 520, "W": 1})
        self.assertEqual(self.m.workspace.decision_logs[0].level, 3)


class Edges(Base):
    def test_git_failure_fails_closed(self):
        single_repo(self.root)
        with mock.patch.object(measure, "git", return_value=None):
            with self.assertRaises(measure.MeasureError):
                measure.measure(self.root)
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                code = installer.main(["measure", "--root", str(self.root)])
        self.assertEqual(code, 2)
        self.assertIn("git ls-files", err.getvalue())

    def test_no_git_at_all_measures_everything(self):
        put(self.root / "DECISIONS.md", "## D-3 — Three\n")
        m = measure.measure(self.root)
        self.assertEqual(m.workspace.decision_logs[0].entries, 1)

    def test_non_utf8_is_a_note_not_a_traceback(self):
        put(self.root / "docs/DECISIONS.md", "## D-1 — One\n")
        put(self.root / "docs/bad.md", b"## D-2 \xff\xfe\n")
        m = measure.measure(self.root)
        self.assertIn("docs/bad.md: not valid UTF-8 (byte 7); not measured", m.notes)
        self.assertEqual(m.max_ids, {"D": 1})

    def test_bom_and_crlf(self):
        put(self.root / "DECISIONS.md", "﻿" + FM.replace("\n", "\r\n")
            + "# Log\r\n\r\n## D-1 — One\r\n\r\n## D-2 — Two\r\n", newline="")
        log = measure.measure(self.root).workspace.decision_logs[0]
        self.assertEqual((log.entries, log.prefixes), (2, ["D"]))

    def test_two_registries_are_ambiguous(self):
        put(self.root / "a/x.md", "# x\n")
        put(self.root / "one.toml", '[[project]]\nname = "a"\ndir = "a"\n')
        put(self.root / "two.toml", '[[repo]]\nname = "a"\ndir = "a"\n')
        m = measure.measure(self.root)
        self.assertIsNone(m.registry)
        self.assertTrue(any("several files look like a registry" in n for n in m.notes))

    def test_cargo_bin_is_not_a_registry(self):
        put(self.root / "Cargo.toml", '[[bin]]\nname = "x"\npath = "src/main.rs"\n')
        put(self.root / "src/main.rs", "")
        self.assertIsNone(measure.measure(self.root).registry)

    def test_a_trap_set_without_an_index(self):
        put(self.root / "working-files/HANDOFF.md", FM + "# Handoff\n")
        put(self.root / "working-files/traps.md", FM + "# Traps\n\n## T-1 — One\n")
        ts = measure.measure(self.root).workspace.trap_sets[0]
        self.assertFalse(ts.has_index)
        put(self.root / "working-files/traps.md", FM + "# Traps\n\n"
            + pair("gov", "trap-index") + "\n## T-1 — One\n")
        self.assertTrue(measure.measure(self.root).workspace.trap_sets[0].has_index)

    def test_planned_registry_is_measured_as_if_on_disk(self):
        put(self.root / "app/docs/DECISIONS.md", FM + "# Log\n\n## A-1 — One\n")
        commit(self.root / "app")
        m = measure.measure(self.root, planned=("projects.toml", {"project": [
            {"name": "app", "dir": "app", "tier": "full"}]}))
        self.assertEqual((m.registry, m.registry_entries), ("projects.toml", "project"))
        self.assertTrue(m.governance_key_missing)
        self.assertEqual([(s.name, s.dir) for s in m.scopes], [("app", "app")])
        self.assertEqual(m.subrepos, ["app"])
        self.assertFalse((self.root / "projects.toml").exists())

    def test_narrowest_glob(self):
        g = measure.narrowest_glob
        self.assertEqual(g(["traps.md"]), "traps.md")
        self.assertEqual(g(["traps.md", "traps-build.md"]), "traps*.md")
        self.assertEqual(g(["traps.md", "platform-traps.md", "headless-traps.md"]), "*traps.md")
        self.assertEqual(g(["a-traps.md", "b-traps-x.md"]), "*.md")

    def test_cli_json_and_summary(self):
        orbit(self.root)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = installer.main(["measure", "--root", str(self.root), "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out.getvalue())
        self.assertEqual(data["registry"], "repos.toml")
        self.assertEqual(data["scopes"][0]["trap_sets"][0]["glob"], "*traps.md")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = installer.main(["measure", "--root", str(self.root)])
        self.assertEqual(code, 0)
        self.assertIn("registry: repos.toml [[repo]]", out.getvalue())


if __name__ == "__main__":
    unittest.main()
