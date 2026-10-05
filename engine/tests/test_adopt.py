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
from test_measure import (BODY, FM, example_workspace, commit, orbit, pair, put,  # noqa: E402
                          single_repo, snapshot)


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
                                      "registry_file", "options", "options_answered",
                                      "measurement"])
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

    def test_options_offered_once_structure_is_settled(self):
        code, out, _ = self.adopt(self.ANSWERS, "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(sorted(o["id"] for o in data["options"]),
                         ["checkout-hygiene", "hooks-wired", "licenses", "usage", "writing-rules"])
        self.assertTrue(all(o["new"] for o in data["options"]))

    def test_an_option_answered_on_is_written_and_recorded(self):
        self.green(self.ANSWERS + '"option:writing-rules" = "on"\n'
                   '"option:writing-rules:files" = "docs/**/*.md, engine/README.md"\n'
                   '"option:writing-rules:rules" = [{ text = "colour", use = "color" }]\n')
        cfg = tomllib.loads((self.root / ".context-gate/config.toml").read_text("utf-8"))
        self.assertEqual(cfg["checks"]["writing-rules"],
                         {"level": "error", "files": ["docs/**/*.md", "engine/README.md"],
                          "rules": [{"text": "colour", "use": "color"}]})
        man = tomllib.loads((self.root / ".context-gate/installed.toml").read_text("utf-8"))
        self.assertEqual(man["options_answered"], ["writing-rules"])

    def test_an_answer_for_a_check_that_is_not_an_option_is_ignored(self):
        code, out, _ = self.adopt(self.ANSWERS + '"option:agents" = "on"\n', "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertNotIn("checks", data["config"])
        self.assertIn("answer 'option:agents' matches no question; ignored", data["notes"])

    def test_a_bad_option_value_is_noted(self):
        _, out, _ = self.adopt(self.ANSWERS + '"option:licenses" = "maybe"\n', "--json")
        self.assertIn("answer 'option:licenses' = 'maybe' is not on, off or inherit; ignored",
                      json.loads(out)["notes"])

    def test_off_over_a_profile_on_loads_and_warns(self):
        prof = self.tmp / "prof"
        prof.mkdir()
        (prof / "principles.toml").write_text(
            '[checks.writing-rules]\nlevel = "error"\n'
            'rules = [{ text = "colour", use = "color" }]\n', encoding="utf-8")
        code, out, err = self.adopt(self.ANSWERS + '"option:writing-rules" = "off"\n',
                                    "--apply", "--profile", prof.as_posix())
        self.assertEqual(code, 0, out + err)
        code, out = gate(self.root)
        self.assertIn("[checks.writing-rules] level overrides the profile with no reason", out)

    def test_off_with_no_profile_still_writes_level_off(self):
        code, out, _ = self.adopt(self.ANSWERS + '"option:licenses" = "off"\n', "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["config"]["checks"]["licenses"], {"level": "off"})

    def test_an_option_answered_under_its_old_name_is_written_under_the_new_one(self):
        # `licences` is `licenses` now: an answers file written before the rename still works.
        code, out, _ = self.adopt(self.ANSWERS + '"option:licences" = "on"\n', "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["config"]["checks"], {"licenses": {"level": "error"}})
        self.assertEqual(data["options_answered"], ["licenses"])
        self.assertIn("answer 'option:licences': 'licences' is now 'licenses' (the old name "
                      "still works; rename it)", data["notes"])

    def test_an_answer_for_an_old_fact_name_matches_no_question(self):
        code, out, _ = self.adopt(self.ANSWERS + '"option:licence" = "on"\n', "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertNotIn("checks", data["config"])
        self.assertIn("answer 'option:licence' matches no question; ignored", data["notes"])

    def test_inherit_writes_no_table_and_records_the_id(self):
        code, out, _ = self.adopt(self.ANSWERS + '"option:licenses" = "inherit"\n', "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertNotIn("checks", data["config"])
        self.assertEqual(data["options_answered"], ["licenses"])

    def test_a_mismatched_param_type_is_refused_and_notes_no_options(self):
        answers = self.ANSWERS + '"option:writing-rules:files" = 3\n'
        code, out, _ = self.adopt(answers, "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["options"], [])
        self.assertTrue(any("options not offered" in n for n in data["notes"]), data["notes"])
        before = snapshot(self.root)
        code, _, err = self.adopt(answers, "--apply")
        self.assertEqual(code, 2)
        self.assertIn("does not load", err)
        self.assertEqual(snapshot(self.root), before)

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


class ExistingDocRegistryRows(Base):
    """A workspace whose project's hand-kept `docs/INDEX.md` doc registry lists the project's
    own `../AGENTS.md`, outside the `docs/**/*.md` glob measurement proposes: the proposal
    covers it, so the first `index` keeps its row rather than dropping it silently."""

    ANSWERS = 'shape = "workspace"\nrepos = "alpha"\n'
    ROWS = ("| Doc | Load when |\n|---|---|\n| [`../AGENTS.md`](../AGENTS.md) | test |\n"
            "| [`guide.md`](guide.md) | test |\n| [`../NOTES.md`](../NOTES.md) | test |\n"
            "| [`site`](https://example.com/x.md) | test |\n")

    def setUp(self) -> None:
        super().setUp()
        put(self.root / "mods.toml", '[[mod]]\nname = "alpha"\ndir = "alpha"\ntier = "full"\n'
            'id_prefix = "A"\nid_range = "100-199"\n')
        put(self.root / "alpha/AGENTS.md", FM + "# Alpha agents\n")
        put(self.root / "alpha/NOTES.md", "# Notes, no frontmatter\n")
        put(self.root / "alpha/docs/INDEX.md", FM + "# Index\n\n" + pair("gov", "doc-registry",
                                                                         self.ROWS))
        put(self.root / "alpha/docs/guide.md", FM + "# Guide\n")
        put(self.root / "alpha/docs/DECISIONS.md", FM + "# Decisions\n\n"
            + pair("gov", "decision-index") + "\n## A-100 — First\n" + BODY)
        commit(self.root)

    def test_measure_reads_each_row_relative_to_its_index(self):
        from govern import measure
        rows = measure.measure(self.root).registry_rows
        self.assertEqual(rows, [("alpha/docs/INDEX.md", "alpha/AGENTS.md"),
                                ("alpha/docs/INDEX.md", "alpha/NOTES.md"),
                                ("alpha/docs/INDEX.md", "alpha/docs/guide.md")])

    def test_a_listed_doc_outside_the_proposed_glob_is_proposed_and_keeps_its_row(self):
        report = self.green(self.ANSWERS)
        cfg = tomllib.loads((self.root / layout.CONFIG).read_text(encoding="utf-8"))
        self.assertEqual(cfg["projects"]["docs"], ["docs/**/*.md", "AGENTS.md"])
        index = (self.root / "alpha/docs/INDEX.md").read_text(encoding="utf-8")
        self.assertIn("[`../AGENTS.md`](../AGENTS.md)", index)
        self.assertIn("[`guide.md`](guide.md)", index)
        self.assertIn("alpha/docs/INDEX.md's doc registry lists alpha/AGENTS.md, which the "
                      "proposed docs globs miss: [projects] docs lists 'AGENTS.md' so `index` "
                      "keeps its row", report)
        # A listed file that could never be governed is not added: it is named instead.
        self.assertNotIn("../NOTES.md", index)
        self.assertIn("alpha/docs/INDEX.md's doc registry lists alpha/NOTES.md, which has no "
                      "doc_type frontmatter: `index` drops its row", report)

    def test_a_path_another_project_holds_without_frontmatter_is_not_added(self):
        # `[projects] docs` is shared: adding 'AGENTS.md' would govern beta's too, which has no
        # frontmatter, and turn the gate red. It is left out, and the note names beta.
        put(self.root / "mods.toml", '[[mod]]\nname = "alpha"\ndir = "alpha"\ntier = "full"\n'
            'id_prefix = "A"\nid_range = "100-199"\n\n[[mod]]\nname = "beta"\ndir = "beta"\n'
            'tier = "full"\nid_prefix = "B"\nid_range = "200-299"\n')
        put(self.root / "beta/AGENTS.md", "# Beta agents, no frontmatter\n")
        put(self.root / "beta/docs/guide.md", FM + "# Guide\n")
        put(self.root / "beta/docs/DECISIONS.md", FM + "# Decisions\n\n"
            + pair("gov", "decision-index") + "\n## B-200 — First\n" + BODY)
        commit(self.root)
        report = self.green('shape = "workspace"\nrepos = "alpha,beta"\n')
        cfg = tomllib.loads((self.root / layout.CONFIG).read_text(encoding="utf-8"))
        self.assertEqual(cfg["projects"]["docs"], ["docs/**/*.md"])
        self.assertIn("alpha/docs/INDEX.md's doc registry lists alpha/AGENTS.md, which the "
                      "proposed docs globs miss; 'AGENTS.md' is not added to [projects] docs, "
                      "which every project shares, since beta's AGENTS.md has no doc_type "
                      "frontmatter: `index` drops its row", report)

    def test_a_second_proposal_adds_nothing_more(self):
        from govern import measure, propose
        m = measure.measure(self.root)
        answers = tomllib.loads(self.ANSWERS)
        first = propose.propose(m, None, None, answers).config["projects"]["docs"]
        again = propose.propose(m, None, None, answers).config["projects"]["docs"]
        self.assertEqual(first, again)
        self.assertEqual(first.count("AGENTS.md"), 1)


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


# ---------------------------------------------------------------------------- ignored tool files

class IgnoredToolFiles(Base):
    """A project's `bin/` rule for build output also ignores `.context-gate/bin/`, so a
    commit would ship a config with nothing to run it. Adopt names the ignored files as needing
    a person, with the fix, and `check` warns until the fix is in."""

    BIN = (".context-gate/bin/govern, .context-gate/bin/upgrade, .context-gate/bin/uninstall: "
           "ignored by git (.gitignore:2 'bin/'), so a commit leaves out what the gate needs — "
           "add '!.context-gate/bin/' to .gitignore")

    def setUp(self) -> None:
        super().setUp()
        single_repo(self.root)
        put(self.root / ".gitignore", "scratch/\nbin/\n")
        commit_all(self.root)

    def test_adopt_names_the_ignored_files_as_needing_a_person(self):
        code, out, err = self.adopt(SingleRepo.ANSWERS, "--apply")
        self.assertEqual(code, 0, out + err)                  # a warning, never red
        self.assertIn(f"needs a person  {self.BIN}", out)
        for name in ("adopt-report.md", "install-report.md"):
            report = (self.root / layout.GOV_DIR / name).read_text(encoding="utf-8")
            head = report.index("## Needs a person before committing")
            self.assertIn(f"- {self.BIN}\n", report[head:])
        report = (self.root / layout.GOV_DIR / "adopt-report.md").read_text(encoding="utf-8")
        self.assertLess(report.index("## Needs a person"), report.index("## Measured"))

    def test_check_warns_until_the_fix_is_in(self):
        self.adopt(SingleRepo.ANSWERS, "--apply")
        code, out = gate(self.root)
        self.assertEqual(code, 0, out)
        self.assertIn(f"warn   {self.BIN}", out)
        gitignore = self.root / ".gitignore"
        put(gitignore, gitignore.read_text(encoding="utf-8") + "!.context-gate/bin/\n")
        code, out = gate(self.root)
        self.assertEqual(code, 0, out)
        self.assertNotIn("ignored by git", out)

    def test_a_tracked_file_is_not_reported(self):
        self.adopt(SingleRepo.ANSWERS, "--apply")
        subprocess.run(["git", "-C", str(self.root), "add", "-f", ".context-gate/bin/"],
                       check=True, capture_output=True)
        code, out = gate(self.root)
        self.assertNotIn("ignored by git", out)

    def test_a_failing_git_call_warns_never_reads_as_clean(self):
        self.adopt(SingleRepo.ANSWERS, "--apply")
        real = subprocess.run

        def failing(argv, *a, **kw):
            if "check-ignore" in argv and "-v" in argv:
                return subprocess.CompletedProcess(argv, 128, b"", b"fatal: lock held")
            return real(argv, *a, **kw)
        with mock.patch("govern.checks.repo.subprocess.run", failing):
            code, out = gate(self.root)
        self.assertEqual(code, 0, out)
        self.assertIn("warn   .context-gate/: could not ask git whether it ignores the files the "
                      "gate needs committed (fatal: lock held)", out)

    def test_a_root_in_a_subdirectory_is_told_which_gitignore_and_the_advice_works(self):
        from govern.checks import repo
        top = self.tmp / "mono"
        put(top / ".gitignore", "bin/\n")
        commit(top)
        root = top / "tools" / "gate"
        for rel in (layout.CONFIG, *(f"{layout.GOV_DIR}/{t}" for t in layout.TOOL_FILES)):
            put(root / rel, "x\n")
        found = repo.ignored_findings(root)
        self.assertEqual(len(found), 1, found)
        self.assertTrue(found[0].endswith("add '!.context-gate/bin/' to tools/gate/.gitignore"),
                        found)
        put(root / ".gitignore", "!.context-gate/bin/\n")
        self.assertEqual(repo.ignored_findings(root), [])


class IgnoredToolFilesInANestedCheckout(Base):
    """A `mods.toml` workspace whose root `.gitignore` lists its member's checkout, a repo of
    its own, where adopt creates the member's decision log. The root's ignore rule is not the
    question for that file: its own repo is, and that repo does not ignore it. Advising
    `!mods/x/` would sweep the nested repo into the root's next `git add -A`."""

    def setUp(self) -> None:
        super().setUp()
        put(self.root / "mods.toml", '[workspace]\nid_prefix = "W"\nid_range = "1-99"\n\n'
            '[[mod]]\nname = "x"\ndir = "mods/x"\ntier = "full"\nid_prefix = "M"\n'
            'id_range = "100-199"\n')
        put(self.root / ".gitignore", "mods/x/\n")
        put(self.root / "AGENTS.md", FM + "# Agents\n")
        commit(self.root)
        put(self.root / "mods/x/README.md", FM + "# X\n")
        commit(self.root / "mods/x")

    def test_a_log_created_in_a_nested_checkout_is_asked_of_that_checkout(self):
        code, out, err = self.adopt('shape = "workspace"\nrepos = "x"\n', "--apply")
        self.assertEqual(code, 0, out + err)
        self.assertIn("created      mods/x/DECISIONS.md", out)
        report = (self.root / layout.GOV_DIR / "adopt-report.md").read_text(encoding="utf-8")
        self.assertNotIn("Needs a person before committing", report)
        self.assertNotIn("!mods/x/", report + out)

    def test_the_nested_checkouts_own_ignore_rule_is_reported_against_its_own_file(self):
        put(self.root / "mods/x/.gitignore", "DECISIONS.md\n")
        commit_all_new(self.root / "mods/x")
        code, out, err = self.adopt('shape = "workspace"\nrepos = "x"\n', "--apply")
        report = (self.root / layout.GOV_DIR / "adopt-report.md").read_text(encoding="utf-8")
        self.assertIn("- mods/x/DECISIONS.md: ignored by git (.gitignore:1 'DECISIONS.md'), so a "
                      "commit leaves out what the gate needs — add '!DECISIONS.md' to "
                      "mods/x/.gitignore", report)


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
