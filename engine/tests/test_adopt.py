"""`adopt`: measure, propose, and with `--apply` install green in one step, on the three
fixture shapes `test_measure` builds, plus a workspace with no registry and the engine's
`[registry] skip`.

Every end-to-end test on the three fixture shapes ends with the gate run the way a project runs
it and exiting 0, with no hand step between, as does a workspace with no registry, whose
`projects.toml` adopt writes. Content outside the ratchet that keeps the gate red is listed
first in the report, and adopt exits 1 (`NeedsAPerson`).

    python3 -m unittest discover -s engine/tests -k adopt
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from govern import cli, config, installer, layout, migrate, profile, registry  # noqa: E402
from test_measure import (BODY, FM, example_workspace, commit, orbit, put, snapshot,  # noqa: E402
                          single_repo)


def run(*args: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = installer.main(list(args))
    return code, out.getvalue(), err.getvalue()


def gate(root: Path) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = cli.main(["check"], root=root)
    return code, out.getvalue()


def commits(root: Path) -> str:
    return subprocess.run(["git", "-C", str(root), "rev-list", "--count", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(profile.rmtree, self.tmp)
        self.root = self.tmp / "root"
        self.root.mkdir()
        env = {"HOME": str(self.tmp / "home"), "CONTEXT_GATE_NO_UPDATE_CHECK": "1"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def answers(self, text: str) -> str:
        path = self.tmp / "answers.toml"
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        return str(path)

    def adopt(self, answers: str | None, *extra: str) -> tuple[int, str, str]:
        args = ["adopt", "--root", str(self.root), *extra]
        if answers is not None:
            args += ["--answers", self.answers(answers)]
        return run(*args)

    def green(self, answers: str) -> str:
        """`adopt --apply` with these answers, then the gate: both exit 0. Returns the report."""
        code, out, err = self.adopt(answers, "--apply")
        self.assertEqual(code, 0, out + err)
        code, out = gate(self.root)
        self.assertEqual(code, 0, out)
        return (self.root / ".context-gate/adopt-report.md").read_text(encoding="utf-8")


# -------------------------------------------------------------------- the three fixture shapes

class SingleRepo(Base):
    ANSWERS = 'shape = "single"\n"docs:engine" = "only files with frontmatter"\n'

    def setUp(self) -> None:
        super().setUp()
        single_repo(self.root)

    def test_questions_open_exit_3_then_answered_exit_0(self):
        code, out, _ = self.adopt(None)
        self.assertEqual(code, 3)
        self.assertIn("shape: Is this one repo", out)
        self.assertIn("docs:engine:", out)
        code, out, _ = self.adopt(self.ANSWERS)
        self.assertEqual(code, 0, out)
        self.assertNotIn("question(s) open", out)
        self.assertFalse((self.root / layout.GOV_DIR).exists())   # a dry run writes nothing

    def test_json(self):
        code, out, _ = self.adopt(None, "--json")
        self.assertEqual(code, 3)
        data = json.loads(out)
        self.assertEqual(list(data), ["config", "questions", "create", "migrate", "notes",
                                      "registry_file", "measurement"])
        self.assertEqual(data["questions"][0]["key"], "shape")
        self.assertEqual(data["measurement"]["workspace"]["doc_files"],
                         {"engine": ["engine/README.md"]})

    def test_apply_refused_while_questions_remain(self):
        before = snapshot(self.root)
        code, _, err = self.adopt('shape = "single"\n', "--apply")
        self.assertEqual(code, 2)
        self.assertIn("docs:engine", err)
        self.assertEqual(snapshot(self.root), before)

    def test_adopts_green(self):
        report = self.green(self.ANSWERS)
        cfg = (self.root / layout.CONFIG).read_text(encoding="utf-8")
        self.assertIn('docs = ["docs/**/*.md", "engine/README.md"]', cfg)
        self.assertIn('id_range = "18-999"', cfg)
        settings = json.loads((self.root / ".claude/settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["enabledPlugins"], {layout.PLUGIN_ID: True})
        for section in ("## Measured", "## Chosen", "## Baseline", "## Gate",
                        "## To review and commit"):
            self.assertIn(section, report)
        self.assertIn("The gate is **green**", report)
        self.assertEqual(commits(self.root), "1")                # adopt never commits

    def test_an_existing_install_is_refused(self):
        self.green(self.ANSWERS)
        before = snapshot(self.root)
        code, _, err = self.adopt(self.ANSWERS)
        self.assertEqual(code, 2)
        self.assertIn("already exists", err)
        self.assertEqual(snapshot(self.root), before)

    def test_answers_must_be_strings(self):
        code, _, err = self.adopt("shape = 1\n")
        self.assertEqual(code, 2)
        self.assertIn("must be a string", err)

    def test_a_log_with_entries_keeps_them_in_range(self):
        log = self.root / "docs/DECISIONS.md"
        put(log, log.read_text(encoding="utf-8")
            + "".join(f"\n## D-{n} — Entry {n}\n" + BODY for n in range(1, 20)))
        commit_all(self.root)
        self.green(self.ANSWERS)
        self.assertIn('id_range = "1-999"',
                      (self.root / layout.CONFIG).read_text(encoding="utf-8"))

    def test_a_created_log_gets_frontmatter_and_an_empty_index(self):
        (self.root / "docs/DECISIONS.md").unlink()
        commit_all(self.root)
        report = self.green(self.ANSWERS)
        text = (self.root / "docs/DECISIONS.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\ndoc_type: reference\n"))
        self.assertIn("<!-- gov:generated:start id=decision-index -->", text)
        self.assertIn("- `docs/DECISIONS.md`: a decision log with an empty index", report)


def commit_all(root: Path) -> None:
    subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qam", "change"], check=True, capture_output=True)


class ExampleWorkspace(Base):
    ANSWERS = 'shape = "workspace"\nrepos = "nova-app,nova-launcher,helper-bot"\n'

    def setUp(self) -> None:
        super().setUp()
        example_workspace(self.root)

    def test_adopts_green(self):
        report = self.green(self.ANSWERS)
        cfg = (self.root / layout.CONFIG).read_text(encoding="utf-8")
        self.assertIn('markers = "ex"', cfg)
        self.assertNotIn("skip", cfg)
        self.assertIn('{ glob = "INDEX.md", id = "doc-registry" }', cfg)
        self.assertIn("projects/helper-bot/INDEX.md", report)            # regenerated by index
        self.assertEqual(commits(self.root), "1")

    def test_skip_leaves_an_entry_out(self):
        self.green('shape = "workspace"\nrepos = "nova-app,helper-bot"\n')
        cfg = (self.root / layout.CONFIG).read_text(encoding="utf-8")
        self.assertIn('skip = ["nova-launcher"]', cfg)
        code, out = gate(self.root)
        self.assertNotIn("nova-launcher", out)
        # never regenerated: the skipped scope is not governed at all
        self.assertIn("<!-- ex:generated:start id=decision-index -->\n<!-- ex:generated:end",
                      (self.root / "projects/nova-launcher/DECISIONS.md")
                      .read_text(encoding="utf-8"))

    def test_a_dirty_file_is_refused_and_nothing_changes(self):
        log = self.root / "projects/helper-bot/DECISIONS.md"
        put(log, log.read_text(encoding="utf-8") + "\nedited\n")
        before = snapshot(self.root)
        code, _, err = self.adopt(self.ANSWERS, "--apply")
        self.assertEqual(code, 2)
        self.assertIn("projects/helper-bot/DECISIONS.md", err.replace(os.sep, "/"))
        self.assertIn("uncommitted changes", err)
        self.assertEqual(snapshot(self.root), before)

    def test_a_failing_git_call_is_refused(self):
        before = snapshot(self.root)
        with mock.patch.object(migrate, "git", return_value=None):
            code, _, err = self.adopt(self.ANSWERS, "--apply")
        self.assertEqual(code, 2)
        self.assertIn("`git status` failed", err)
        self.assertEqual(snapshot(self.root), before)


class Orbit(Base):
    ANSWERS = 'shape = "workspace"\nrepos = "client,platform"\n'

    def setUp(self) -> None:
        super().setUp()
        orbit(self.root)

    def test_adopts_green_through_migrate(self):
        report = self.green(self.ANSWERS)
        traps = (self.root / "orbit-client/docs/working-files/traps.md").read_text(
            encoding="utf-8")
        self.assertIn("## T-1 — First", traps)
        self.assertIn("<!-- orbit:generated:start id=trap-index -->", traps)
        self.assertIn("| T-3 |", traps)                   # sources: bodies in other files
        self.assertIn("## Migrated", report)
        self.assertIn("T-1 — First: bullet -> heading", report)
        self.assertIn("orbit-client/docs/working-files/headless-traps.md", report)
        cfg = (self.root / layout.CONFIG).read_text(encoding="utf-8")
        self.assertIn('governance = "dir"', cfg)

    def test_a_gate_script_of_its_own_is_left_alone(self):
        # Adopt guesses no existing gate: replacing one is install's explicit --entrypoint.
        before = {rel: (self.root / rel).read_bytes() for rel in
                  ("governance/gate.py", "governance/gate-baseline.json",
                   "governance/test-gate.py")}
        report = self.green(self.ANSWERS)
        self.assertEqual({rel: (self.root / rel).read_bytes() for rel in before}, before)
        self.assertFalse((self.root / layout.BACKUP).exists())
        manifest = (self.root / layout.MANIFEST).read_text(encoding="utf-8")
        self.assertNotIn("[[entrypoint]]", manifest)
        self.assertNotIn("governance/gate", report)


# ---------------------------------------------------------------------------- no registry yet

def monorepo(root: Path) -> None:
    """A root with two nested checkouts and no registry: `app` keeps a decision log under
    docs/, `lib` has none."""
    put(root / "README.md", "# Mono\n")
    put(root / ".gitignore", "app/\nlib/\n")
    commit(root)
    put(root / "app/docs/DECISIONS.md", FM + "# Decisions\n\n## A-1 — One\n" + BODY)
    put(root / "app/docs/guide.md", FM + "# Guide\n")
    commit(root / "app")
    put(root / "lib/docs/notes.md", FM + "# Notes\n")
    commit(root / "lib")


class NoRegistry(Base):
    def setUp(self) -> None:
        super().setUp()
        monorepo(self.root)

    def test_questions(self):
        code, out, _ = self.adopt(None, "--json")
        self.assertEqual(code, 3)
        data = json.loads(out)
        self.assertEqual([q["options"] for q in data["questions"]],
                         [["workspace", "single"], ["app,lib", "app", "lib"]])
        # proposed on the recommendation: both checkouts measured as the registry's members
        self.assertEqual([s["name"] for s in data["measurement"]["scopes"]], ["app", "lib"])
        self.assertEqual(data["config"]["projects"]["decision_log"], "docs/DECISIONS.md")
        self.assertFalse((self.root / "projects.toml").exists())

    def test_a_workspace_writes_its_registry(self):
        report = self.green('shape = "workspace"\nrepos = "app,lib"\n')
        with (self.root / "projects.toml").open("rb") as fh:
            data = tomllib.load(fh)
        # app's log is A-; lib's is created, and the workspace has no log to take one from
        self.assertEqual(data, {"project": [
            {"name": "app", "dir": "app", "tier": "full", "id_prefix": "A", "id_range": "1-999"},
            {"name": "lib", "dir": "lib", "tier": "full", "id_prefix": "D",
             "id_range": "1-999"}]})
        cfg = (self.root / layout.CONFIG).read_text(encoding="utf-8")
        self.assertIn('file = "projects.toml"', cfg)
        self.assertTrue((self.root / "lib/docs/DECISIONS.md").is_file())    # created
        self.assertTrue((self.root / "DECISIONS.md").is_file())             # the workspace's
        self.assertIn("- `projects.toml`", report)

    def test_one_repo_selected(self):
        self.green('shape = "workspace"\nrepos = "app"\n')
        self.assertIn('name = "app"', (self.root / "projects.toml").read_text(encoding="utf-8"))
        self.assertNotIn("lib", (self.root / "projects.toml").read_text(encoding="utf-8"))
        self.assertFalse((self.root / "lib/docs/DECISIONS.md").exists())

    def test_an_existing_non_registry_file_is_never_overwritten(self):
        put(self.root / "projects.toml", "title = \"not a registry\"\n")
        commit_all_new(self.root)
        code, _, err = self.adopt('shape = "workspace"\nrepos = "app,lib"\n', "--apply")
        self.assertEqual(code, 2)
        self.assertIn("will not overwrite", err)
        self.assertEqual((self.root / "projects.toml").read_text(encoding="utf-8"),
                         "title = \"not a registry\"\n")


class SharedPrefix(Base):
    """Two checkouts whose logs both use `D-`: each is asked its own range."""

    def setUp(self) -> None:
        super().setUp()
        put(self.root / "README.md", "# Mono\n")
        put(self.root / ".gitignore", "app/\nlib/\n")
        commit(self.root)
        put(self.root / "app/docs/DECISIONS.md", FM + "# Decisions\n\n## D-1 — One\n" + BODY)
        commit(self.root / "app")
        put(self.root / "lib/docs/DECISIONS.md", FM + "# Decisions\n\n## D-1000 — One\n" + BODY)
        commit(self.root / "lib")

    def test_ranges_are_asked_then_green(self):
        base = 'shape = "workspace"\nrepos = "app,lib"\n'
        code, out, _ = self.adopt(base, "--json")
        self.assertEqual(code, 3)
        self.assertEqual([(q["key"], q["options"]) for q in json.loads(out)["questions"]],
                         [("id_range:app", ["1-999", "1000-1999"]),
                          ("id_range:lib", ["1000-1999", "1-999"])])
        self.green(base + '"id_range:app" = "1-999"\n"id_range:lib" = "1000-1999"\n')
        text = (self.root / "projects.toml").read_text(encoding="utf-8")
        self.assertIn('id_range = "1000-1999"', text)


class NeedsAPerson(Base):
    """Content outside the ratchet keeps the gate red: adopt exits 1 and lists it first."""

    def test_red_findings_come_first_with_file_and_fix(self):
        single_repo(self.root)
        log = self.root / "docs/DECISIONS.md"
        put(log, log.read_text(encoding="utf-8") + "\n## D-19 — Nineteen\n" + BODY
            + "\n## D-18 — Eighteen\n" + BODY)
        commit_all(self.root)
        code, out, err = self.adopt(SingleRepo.ANSWERS, "--apply")
        self.assertEqual(code, 1, out + err)
        self.assertIn("gate red", out)
        report = (self.root / ".context-gate/adopt-report.md").read_text(encoding="utf-8")
        self.assertIn("The gate is **red**", report)
        head = report.index("## Needs a person before this is green")
        self.assertLess(head, report.index("## Measured"))
        item = next(line for line in report[head:].splitlines()
                    if line.startswith("- `docs/DECISIONS.md") and "order" in line)
        self.assertEqual(item, "- `docs/DECISIONS.md`: D-18 is out of ascending order "
                               "(`decision-log`). Fix: the log is read by index")
        self.assertEqual(gate(self.root)[0], 1)

    def test_a_green_adopt_has_no_such_section(self):
        single_repo(self.root)
        report = self.green(SingleRepo.ANSWERS)
        self.assertNotIn("Needs a person", report)


def commit_all_new(root: Path) -> None:
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, capture_output=True)
    commit_all(root)


# ---------------------------------------------------------------------------- [registry] skip

REGISTRY = """
[[project]]
name = "a"
dir = "a"
tier = "full"

[[project]]
name = "b"
dir = "b"
tier = "full"
"""


class RegistrySkip(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        put(self.root / "projects.toml", REGISTRY)

    def load(self, skip: str) -> registry.Registry:
        put(self.root / layout.CONFIG, f'[governance]\nengine = "{config.__version__}"\n\n'
            f'[registry]\nfile = "projects.toml"\n{skip}')
        return registry.load(config.load(self.root, self.root / "home"))

    def test_a_skipped_entry_is_dropped_entirely(self):
        self.assertEqual([s.name for s in self.load("").scopes], ["a", "b"])
        self.assertEqual([s.name for s in self.load('skip = ["b"]\n').scopes], ["a"])

    def test_an_unknown_name_is_a_config_error(self):
        with self.assertRaisesRegex(config.ConfigError, "skip names c, not an entry in "
                                                        "projects.toml"):
            self.load('skip = ["c"]\n')

    def test_skip_is_a_list_of_names(self):
        with self.assertRaisesRegex(config.ConfigError, "skip: expected list"):
            self.load('skip = "b"\n')
        with self.assertRaisesRegex(config.ConfigError, r"skip\[0\]: expected str"):
            self.load("skip = [1]\n")


if __name__ == "__main__":
    unittest.main()
