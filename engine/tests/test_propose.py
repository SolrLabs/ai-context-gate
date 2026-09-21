"""`propose` over the three shapes adopt handles, and the TOML writer it proposes with. Each
fixture is a `Measurement` built directly, shaped like a project before it adopts the engine:
a single repo, a multi-project workspace (a registry whose governance dirs sit apart from the
checkouts) and a
workspace of member repos (a registry with no `governance` key, sectioned logs and bullet traps).

    python3 -m unittest discover -s engine/tests -k propose
"""
from __future__ import annotations

import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))

from govern import __version__, config, tomlw  # noqa: E402
from govern.measure import DecisionLog, Measurement, ScopeMeasure, TrapSet  # noqa: E402
from govern.propose import (FRONTMATTER_ONLY, SINGLE, WHOLE_DIR, WORKSPACE,  # noqa: E402
                            glob_matches, propose, validate)


PRINCIPLES = '''
[checks.writing-rules]
rules = [{ text = "colour", use = "color", ignore_case = true, why = "American English" }]
'''

WORKSPACE_REGISTRY = '''
[workspace]
id_prefix = "W"
id_range = "1-99"

[[project]]
name = "nova-app"
dir = "NovaApp"
tier = "full"
id_prefix = "O"
id_range = "100-199"
governance = "projects/nova-app"

[project.profile]
handoff = "projects/nova-app/working-files/HANDOFF.md"
has_runtime_gate = true

[[project]]
name = "nova-launcher"
dir = "NovaApp"
tier = "full"
id_prefix = "L"
id_range = "300-399"
governance = "projects/nova-launcher"

[project.profile]
handoff = "projects/nova-launcher/working-files/HANDOFF.md"

[[project]]
name = "helper-bot"
dir = "HelperBot"
tier = "full"
id_prefix = "B"
id_range = "200-299"
governance = "projects/helper-bot"

[project.profile]
handoff = "projects/helper-bot/working-files/HANDOFF.md"

[[project]]
name = "core"
dir = "CORE"
tier = "registered"
id_prefix = ""
id_range = ""
governance = ""
'''

ORBIT_REGISTRY = '''
[workspace]
id_prefix = "D"
id_range = "1-99"

[[repo]]
name = "client"
dir = "client"
tier = "full"
id_prefix = "D"
id_range = "100-499"

[[repo]]
name = "server"
dir = "server"
tier = "full"
'''


def wtext(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(content)


def single_repo(root: Path) -> Measurement:
    """Pre-install: `docs/DECISIONS.md` holds only its index pair; `issues.md` (BUG-) and a
    design doc (`### D17`) sit under the working dir, so neither is a log candidate."""
    ws = ScopeMeasure(
        name=root.name, dir=".",
        decision_logs=[DecisionLog("docs/DECISIONS.md", 0, 2, [], True)],
        trap_sets=[], working_dir="docs",
        # measure never lists a dot-dir; `.claude` is here to prove propose would skip it too
        doc_dirs={"docs": (31, 31), "engine": (4, 1), "plugin": (3, 1), "tools": (2, 0),
                  ".claude": (6, 6)},
        doc_files={"engine": ["engine/README.md"], "plugin": ["plugin/README.md"]})
    return Measurement(
        root=str(root), registry=None, registry_entries=None, registry_keys={},
        governance_key_missing=False, workspace=ws, scopes=[], markers="gov",
        blocks=[("docs/DECISIONS.md", "decision-index")], max_ids={"D": 17, "BUG": 24},
        notes=[])


def example_workspace(root: Path) -> Measurement:
    wtext(root / "projects.toml", WORKSPACE_REGISTRY)

    def scope(name: str, traps: list[str], glob: str) -> ScopeMeasure:
        d = f"projects/{name}"
        return ScopeMeasure(
            name=name, dir=d,
            decision_logs=[DecisionLog(f"{d}/DECISIONS.md", 12, 2, [name[0].upper()], True)],
            trap_sets=[TrapSet([f"{d}/working-files/{t}" for t in traps], False, False, glob)],
            working_dir="working-files", doc_dirs={"working-files": (5, 5)})

    scopes = [scope("nova-app", ["traps.md"], "traps.md"),
              scope("nova-launcher", ["traps.md"], "traps.md"),
              scope("helper-bot", ["traps.md", "traps-build.md"], "traps*.md")]
    blocks = [("governance/DECISIONS.md", "decision-index"), ("AGENTS.md", "agent-roster")]
    for s in scopes:
        blocks.append((f"{s.dir}/DECISIONS.md", "decision-index"))
        blocks += [(f, "trap-index") for f in s.trap_sets[0].files]
    ws = ScopeMeasure(
        name="workspace", dir=".",
        decision_logs=[DecisionLog("governance/DECISIONS.md", 20, 2, ["W"], True)],
        trap_sets=[], working_dir=None, doc_dirs={"governance": (4, 4)})
    return Measurement(
        root=str(root), registry="projects.toml", registry_entries="project",
        registry_keys={"handoff": "profile.handoff", "runtime_gate": "profile.has_runtime_gate"},
        governance_key_missing=False, workspace=ws, scopes=scopes, markers="ex", blocks=blocks,
        max_ids={"W": 19, "O": 140, "L": 305, "B": 212, "T": 30}, notes=[])


def orbit(root: Path) -> Measurement:
    wtext(root / "repos.toml", ORBIT_REGISTRY)
    wd = "client/docs/working"
    client = ScopeMeasure(
        name="client", dir="client",
        decision_logs=[DecisionLog("client/docs/DECISIONS.md", 40, 3, ["D"], True)],
        trap_sets=[TrapSet([f"{wd}/traps.md", f"{wd}/render-traps.md", f"{wd}/net-traps.md"],
                           True, True, "*traps.md", has_index=True)],
        working_dir="docs/working", doc_dirs={"docs": (12, 12)})
    server = ScopeMeasure(
        name="server", dir="server",
        decision_logs=[DecisionLog("server/docs/DECISIONS.md", 15, 3, ["D"], True)],
        trap_sets=[], working_dir="docs/working", doc_dirs={"docs": (6, 6)})
    ws = ScopeMeasure(
        name="workspace", dir=".",
        decision_logs=[DecisionLog("DECISIONS.md", 9, 3, ["D"], True)],
        trap_sets=[], working_dir=None, doc_dirs={})
    return Measurement(
        root=str(root), registry="repos.toml", registry_entries="repo", registry_keys={},
        governance_key_missing=True, workspace=ws, scopes=[client, server], markers="orbit",
        blocks=[("DECISIONS.md", "decision-index"),
                ("client/docs/DECISIONS.md", "decision-index"),
                ("server/docs/DECISIONS.md", "decision-index")],
        max_ids={"D": 480},
        notes=["server: traps kept as a section of docs/working/iteration-log.md"])


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.profile = self.tmp / "principles"
        wtext(self.profile / "principles.toml", PRINCIPLES)

    def tearDown(self):
        self._tmp.cleanup()

    def root(self, name: str) -> Path:
        root = self.tmp / name
        root.mkdir()
        return root

    def loads(self, p, m) -> list[str]:
        """The proposed config loads through `config.load` and `registry.load`."""
        return validate(p.config, m, self.home, p.registry_file)

    # The shape and repo answers each fixture's own tests take as given (asked in `Shape`).
    base: dict[str, str] = {}

    def propose(self, m, source, profile, answers):
        return propose(m, source, profile, {**self.base, **answers})


class SingleRepo(Base):
    base = {"shape": "single"}

    def setUp(self):
        super().setUp()
        self.m = single_repo(self.root("tool-repo"))

    def test_proposal(self):
        p = self.propose(self.m, "git@example.invalid:org/engine.git", str(self.profile), {})
        c = p.config
        self.assertEqual(c["governance"], {"engine": __version__,
                                           "source": "git@example.invalid:org/engine.git",
                                           "profile": str(self.profile), "schema": 1})
        self.assertNotIn("dialect", c)
        self.assertNotIn("registry", c)
        self.assertNotIn("workspace", c)
        self.assertEqual(c["repo"], {"name": "tool-repo", "id_prefix": "D",
                                     "id_range": "18-999"})
        self.assertEqual(c["projects"]["decision_log"], "docs/DECISIONS.md")
        self.assertEqual(c["projects"]["working_dir"], "docs")
        self.assertEqual(c["projects"]["docs"], ["docs/**/*.md"])
        self.assertEqual(c["blocks"], {"project": [{"file": "docs/DECISIONS.md",
                                                    "id": "decision-index"}]})
        self.assertEqual(p.create, [])
        self.assertFalse(p.migrate)
        self.assertEqual([q.key for q in p.questions], ["docs:engine", "docs:plugin"])
        self.assertEqual(p.questions[0].options, [FRONTMATTER_ONLY, WHOLE_DIR])
        self.assertIn("lists engine/README.md", p.questions[0].why)
        self.assertTrue(any("tools/" in n for n in p.notes), p.notes)
        self.assertEqual(self.loads(p, self.m), [])

    def test_answers_remove_their_questions(self):
        p = self.propose(self.m, None, str(self.profile),
                    {"docs:engine": WHOLE_DIR, "docs:plugin": FRONTMATTER_ONLY})
        self.assertEqual(p.questions, [])
        # "only files with frontmatter" adds those files, listed
        self.assertEqual(p.config["projects"]["docs"],
                         ["docs/**/*.md", "engine/**/*.md", "plugin/README.md"])
        self.assertEqual(self.loads(p, self.m), [])

    def test_an_answer_that_is_no_option_asks_again(self):
        p = self.propose(self.m, None, None, {"docs:engine": "some", "docs:plugin": WHOLE_DIR})
        self.assertEqual([q.key for q in p.questions], ["docs:engine"])
        self.assertTrue(any("answer 'docs:engine' = 'some'" in n for n in p.notes), p.notes)

    def test_the_only_named_log_wins_over_one_with_entries(self):
        self.m.workspace.decision_logs.append(DecisionLog("notes/log.md", 9, 2, ["N"], False))
        p = self.propose(self.m, None, None, {})
        self.assertEqual(p.config["projects"]["decision_log"], "docs/DECISIONS.md")

    def test_two_named_logs_are_a_question(self):
        self.m.workspace.decision_logs = [DecisionLog("docs/DECISIONS.md", 0, 2, [], True),
                                          DecisionLog("DECISIONS.md", 0, 2, [], True)]
        p = self.propose(self.m, None, None, {})
        q = [q for q in p.questions if q.key == "decision_log"]
        self.assertEqual(q[0].options, ["docs/DECISIONS.md", "DECISIONS.md"])
        self.assertNotIn("decision_log", p.config["projects"])
        self.assertNotIn("blocks", p.config)
        p = self.propose(self.m, None, None, {"decision_log": "DECISIONS.md"})
        self.assertEqual([q.key for q in p.questions if q.key == "decision_log"], [])
        self.assertEqual(p.config["projects"]["decision_log"], "DECISIONS.md")

    def test_no_log_is_created_under_docs(self):
        self.m.workspace.decision_logs = []
        self.m.blocks = []
        p = self.propose(self.m, None, None, {})
        self.assertEqual(p.create, ["docs/DECISIONS.md"])
        self.assertEqual(p.config["projects"]["decision_log"], "docs/DECISIONS.md")
        self.assertEqual(p.config["repo"]["id_prefix"], "D")
        # adopt writes the pair into the log it creates, so the block is proposed
        self.assertEqual(p.config["blocks"]["project"],
                         [{"file": "docs/DECISIONS.md", "id": "decision-index"}])

    def test_create_lists_only_missing_paths(self):
        self.m.workspace.decision_logs = []
        wtext(Path(self.m.root) / "docs/DECISIONS.md", "# excluded from measure, say\n")
        self.assertEqual(self.propose(self.m, None, None, {}).create, [])

    def test_id_range_widens_past_900(self):
        self.m.max_ids["D"] = 950
        self.assertEqual(self.propose(self.m, None, None, {}).config["repo"]["id_range"], "951-9999")

    def test_an_answer_to_no_question_is_noted(self):
        p = self.propose(self.m, None, None, {"nonsense": "x"})
        self.assertIn("answer 'nonsense' matches no question; ignored", p.notes)


class ExampleWorkspace(Base):
    base = {"shape": "workspace", "repos": "nova-app,nova-launcher,helper-bot"}

    def setUp(self):
        super().setUp()
        self.m = example_workspace(self.root("example-workspace"))

    def test_proposal(self):
        p = self.propose(self.m, None, str(self.profile), {})
        c = p.config
        self.assertEqual(c["dialect"], {"markers": "ex"})
        self.assertEqual(c["registry"], {
            "file": "projects.toml", "entries": "project",
            "keys": {"handoff": "profile.handoff", "runtime_gate": "profile.has_runtime_gate"}})
        self.assertEqual(c["workspace"], {"decision_log": "governance/DECISIONS.md"})
        self.assertEqual(c["projects"], {"governed_tiers": ["full"], "decision_log": "DECISIONS.md",
                                         "working_dir": "working-files", "trap_glob": "traps*.md",
                                         "docs": ["working-files/**/*.md"]})
        self.assertEqual(c["blocks"], {
            "workspace": [{"file": "governance/DECISIONS.md", "id": "decision-index"}],
            "project": [{"file": "DECISIONS.md", "id": "decision-index"},
                        {"glob": "working-files/traps*.md", "id": "trap-index"}]})
        self.assertEqual(p.questions, [])
        self.assertEqual(p.create, [])
        self.assertFalse(p.migrate)
        self.assertIn("AGENTS.md: an existing 'agent-roster' block is not proposed; add it to "
                      "[blocks] after install to keep it generated", p.notes)
        self.assertFalse([n for n in p.notes if n.startswith("registry:")], p.notes)
        self.assertEqual(self.loads(p, self.m), [])

    def test_scopes_that_disagree_are_a_question(self):
        self.m.scopes[2].decision_logs[0].path = "projects/helper-bot/docs/DECISIONS.md"
        self.m.blocks[self.m.blocks.index(("projects/helper-bot/DECISIONS.md", "decision-index"))] \
            = ("projects/helper-bot/docs/DECISIONS.md", "decision-index")
        p = self.propose(self.m, None, None, {})
        q = next(q for q in p.questions if q.key == "decision_log")
        self.assertEqual(q.options, ["DECISIONS.md", "docs/DECISIONS.md"])
        self.assertNotIn("decision_log", p.config["projects"])
        p = self.propose(self.m, None, None, {"decision_log": "DECISIONS.md"})
        self.assertEqual(p.questions, [])
        self.assertIn("helper-bot: its decision log is docs/DECISIONS.md, but [projects] "
                      "decision_log is DECISIONS.md", p.notes)
        self.assertNotIn({"file": "DECISIONS.md", "id": "decision-index"},
                         p.config["blocks"]["project"])
        self.assertEqual(self.loads(p, self.m), [])

    def test_ambiguous_scope_log_is_asked_per_scope(self):
        self.m.scopes[0].decision_logs = [
            DecisionLog("projects/nova-app/DECISIONS.md", 3, 2, ["O"], True),
            DecisionLog("projects/nova-app/docs/decisions.md", 5, 2, ["O"], True)]
        p = self.propose(self.m, None, None, {})
        self.assertEqual([q.key for q in p.questions], ["decision_log:nova-app"])
        p = self.propose(self.m, None, None, {"decision_log:nova-app": "DECISIONS.md"})
        self.assertEqual(p.questions, [])
        self.assertEqual(p.config["projects"]["decision_log"], "DECISIONS.md")

    def test_a_scope_with_no_log_gets_one_created(self):
        self.m.scopes[1].decision_logs = []
        p = self.propose(self.m, None, None, {})
        self.assertEqual(p.create, ["projects/nova-launcher/DECISIONS.md"])
        self.assertIn({"file": "DECISIONS.md", "id": "decision-index"},
                      p.config["blocks"]["project"])

    def test_a_trap_file_without_its_pair_gets_no_trap_index(self):
        self.m.blocks.remove(("projects/helper-bot/working-files/traps-build.md", "trap-index"))
        p = self.propose(self.m, None, None, {})
        # every scope's files matching a trap-index glob need the pair: none is proposed
        self.assertEqual(p.config["blocks"]["project"],
                         [{"file": "DECISIONS.md", "id": "decision-index"}])
        self.assertTrue(any("working-files/traps*.md" in n and "traps-build.md" in n
                            for n in p.notes), p.notes)
        self.assertEqual(self.loads(p, self.m), [])

    def test_missing_id_range_is_noted(self):
        wtext(Path(self.m.root) / "projects.toml",
              WORKSPACE_REGISTRY.replace('id_range = "200-299"\n', ""))
        p = self.propose(self.m, None, None, {})
        self.assertIn("registry: 'helper-bot' has no id_prefix and id_range; add them to "
                      "projects.toml", p.notes)


class Orbit(Base):
    base = {"shape": "workspace", "repos": "client,server"}

    def setUp(self):
        super().setUp()
        self.m = orbit(self.root("orbit"))

    def test_proposal(self):
        p = self.propose(self.m, None, str(self.profile), {})
        c = p.config
        self.assertEqual(c["dialect"], {"markers": "orbit"})
        self.assertEqual(c["registry"], {"file": "repos.toml", "entries": "repo",
                                         "keys": {"governance": "dir"}})
        self.assertEqual(c["workspace"], {"decision_log": "DECISIONS.md"})
        self.assertEqual(c["projects"]["decision_log"], "docs/DECISIONS.md")
        self.assertEqual(c["projects"]["working_dir"], "docs/working")
        self.assertEqual(c["projects"]["trap_glob"], "*traps.md")
        self.assertEqual(c["blocks"]["project"], [
            {"file": "docs/DECISIONS.md", "id": "decision-index"},
            {"glob": "docs/working/traps.md", "id": "trap-index",
             "sources": "docs/working/*traps.md"}])
        self.assertTrue(p.migrate)
        self.assertEqual(p.questions, [])
        self.assertIn("server: traps kept as a section of docs/working/iteration-log.md", p.notes)
        self.assertIn("registry: 'server' has no id_prefix and id_range; add them to repos.toml",
                      p.notes)
        self.assertEqual(self.loads(p, self.m), [])

    def test_a_relative_profile_is_read_from_the_project(self):
        wtext(Path(self.m.root) / "principles/principles.toml", PRINCIPLES)
        p = self.propose(self.m, None, "principles", {})
        self.assertEqual(p.config["governance"]["profile"], "principles")
        self.assertEqual(self.loads(p, self.m), [])

    def test_validation_catches_a_config_that_does_not_load(self):
        p = self.propose(self.m, None, None, {})
        p.config["blocks"]["project"].append({"glob": "/abs/*.md", "id": "trap-index"})
        with self.assertRaises(config.ConfigError):
            self.loads(p, self.m)


class Shape(Base):
    """`shape` is always asked first; `repos` for a workspace, answered as a comma list."""

    def test_a_single_repo_is_recommended_single(self):
        m = single_repo(self.root("tool-repo"))
        q = propose(m, None, None, {}).questions[0]
        self.assertEqual((q.key, q.options), ("shape", [SINGLE, WORKSPACE]))

    def test_a_registry_recommends_a_workspace_and_its_governed_entries(self):
        m = example_workspace(self.root("example-workspace"))
        p = propose(m, None, None, {})
        self.assertEqual([q.key for q in p.questions], ["shape", "repos"])
        self.assertEqual(p.questions[0].options, [WORKSPACE, SINGLE])
        # first the recommended answer, then every name; `core` is registered, not governed
        self.assertEqual(p.questions[1].options, ["nova-app,nova-launcher,helper-bot", "nova-app",
                                                  "nova-launcher", "helper-bot", "core"])
        # until answered, the proposal follows the recommendation
        self.assertEqual(p.config["registry"]["file"], "projects.toml")
        self.assertNotIn("skip", p.config["registry"])
        self.assertIsNone(p.registry_file)

    def test_unselected_governed_entries_are_skipped(self):
        m = example_workspace(self.root("example-workspace"))
        p = propose(m, None, None, {"shape": WORKSPACE, "repos": "nova-app, helper-bot"})
        self.assertEqual(p.questions, [])
        self.assertEqual(p.config["registry"]["skip"], ["nova-launcher"])
        self.assertFalse([n for n in p.notes if "nova-launcher" in n], p.notes)
        self.assertEqual(self.loads(p, m), [])

    def test_selecting_an_ungoverned_entry_is_noted_not_edited(self):
        m = example_workspace(self.root("example-workspace"))
        p = propose(m, None, None, {"shape": WORKSPACE,
                                    "repos": "nova-app,nova-launcher,helper-bot,core"})
        self.assertNotIn("skip", p.config["registry"])
        self.assertTrue(any(n.startswith("repos: 'core' is not governed") for n in p.notes))

    def test_an_unknown_repo_is_asked_again(self):
        m = example_workspace(self.root("example-workspace"))
        p = propose(m, None, None, {"shape": WORKSPACE, "repos": "nova-app,nope"})
        self.assertEqual([q.key for q in p.questions], ["repos"])
        self.assertIn("answer 'repos' names nope, not a repo here; asked again", p.notes)

    def test_an_unlisted_checkout_is_offered_and_noted(self):
        m = example_workspace(self.root("example-workspace"))
        m.subrepos = ["NovaApp", "tools/extra"]     # NovaApp is an entry's dir; extra is not
        p = propose(m, None, None, {"shape": WORKSPACE})
        self.assertEqual(p.questions[-1].options[-1], "extra")
        p = propose(m, None, None, {"shape": WORKSPACE, "repos": "nova-app,extra"})
        self.assertEqual(p.config["registry"]["skip"], ["nova-launcher", "helper-bot"])
        self.assertIn("repos: 'extra' (tools/extra) has no entry in projects.toml; add one "
                      "there to govern it", p.notes)

    def test_single_over_a_registry(self):
        m = example_workspace(self.root("example-workspace"))
        p = propose(m, None, None, {"shape": SINGLE})
        self.assertNotIn("registry", p.config)
        self.assertEqual(p.config["repo"]["name"], "example-workspace")
        self.assertEqual(p.config["projects"]["decision_log"], "governance/DECISIONS.md")
        self.assertIn("projects.toml: not used; the root is adopted as a single repo", p.notes)
        self.assertEqual(self.loads(p, m), [])

    def test_a_workspace_with_no_registry_plans_one(self):
        root = self.root("mono")
        m = single_repo(root)
        m.subrepos = ["app", "libs/core"]
        p = propose(m, None, None, {"shape": WORKSPACE})
        self.assertEqual([q.key for q in p.questions], ["repos"])
        self.assertEqual(p.questions[0].options, ["app,core", "app", "core"])
        p = propose(m, None, None, {"shape": WORKSPACE, "repos": "core"})
        self.assertEqual(p.questions, [])
        self.assertEqual(p.registry_file, {"project": [
            {"name": "core", "dir": "libs/core", "tier": "full"}]})
        self.assertEqual(p.config["registry"], {"file": "projects.toml", "entries": "project",
                                                "keys": {"governance": "dir"}})
        self.assertTrue(any("measure(root, planned=...)" in n for n in p.notes), p.notes)
        # measured again as the planned registry's members, the layout follows
        core = ScopeMeasure(name="core", dir="libs/core",
                            decision_logs=[DecisionLog("libs/core/DECISIONS.md", 2, 2, ["C"],
                                                       True)],
                            trap_sets=[], working_dir=None, doc_dirs={})
        planned = Measurement(
            root=str(root), registry="projects.toml", registry_entries="project",
            registry_keys={}, governance_key_missing=True, workspace=m.workspace, scopes=[core],
            markers="gov", blocks=[], max_ids={"C": 2}, notes=[], subrepos=m.subrepos)
        p = propose(planned, None, None, {"shape": WORKSPACE, "repos": "core",
                                          "docs:engine": WHOLE_DIR, "docs:plugin": WHOLE_DIR})
        self.assertEqual(p.questions, [])
        self.assertEqual(p.registry_file["project"][0]["name"], "core")
        self.assertEqual(p.config["projects"]["decision_log"], "DECISIONS.md")
        # its log's prefix, and the whole range: no other scope shares the prefix
        self.assertEqual(p.registry_file["project"][0],
                         {"name": "core", "dir": "libs/core", "tier": "full", "id_prefix": "C",
                          "id_range": "1-999"})
        self.assertEqual(self.loads(p, planned), [])

    def test_planned_scopes_sharing_a_prefix_are_asked_their_ranges(self):
        root = self.root("mono")
        m = single_repo(root)
        m.subrepos = ["app", "lib", "cli"]

        def scope(name: str, prefixes: list[str]) -> ScopeMeasure:
            logs = [DecisionLog(f"{name}/DECISIONS.md", 1, 2, prefixes, True)] if prefixes \
                else []
            return ScopeMeasure(name=name, dir=name, decision_logs=logs, trap_sets=[],
                                working_dir=None, doc_dirs={})
        planned = Measurement(
            root=str(root), registry="projects.toml", registry_entries="project",
            registry_keys={}, governance_key_missing=True, workspace=m.workspace,
            scopes=[scope("app", ["D"]), scope("lib", ["D"]), scope("cli", [])], markers="gov",
            blocks=[], max_ids={"D": 950}, notes=[], subrepos=m.subrepos)
        ans = {"shape": WORKSPACE, "repos": "app,lib,cli", "decision_log": "DECISIONS.md",
               "docs:engine": WHOLE_DIR, "docs:plugin": WHOLE_DIR}
        p = propose(planned, None, None, ans)
        # cli's log is created: it takes the workspace log's prefix, here none, so D too
        self.assertEqual([(q.key, q.options) for q in p.questions], [
            ("id_range:app", ["1-999", "1000-1999", "2000-2999"]),
            ("id_range:lib", ["1000-1999", "1-999", "2000-2999"]),
            ("id_range:cli", ["2000-2999", "1-999", "1000-1999"])])
        self.assertNotIn("id_range", p.registry_file["project"][0])
        p = propose(planned, None, None, {**ans, "id_range:app": "1-999",
                                          "id_range:lib": "1000-1999", "id_range:cli": "x"})
        self.assertEqual([q.key for q in p.questions], ["id_range:cli"])
        self.assertIn("answer 'id_range:cli' = 'x' is not a range like 1-999; asked again",
                      p.notes)
        self.assertEqual([e.get("id_range") for e in p.registry_file["project"]],
                         ["1-999", "1000-1999", None])

    def test_one_prefix_past_900_widens_to_9999(self):
        root = self.root("mono")
        m = single_repo(root)
        m.subrepos = ["app"]
        app = ScopeMeasure(name="app", dir="app",
                           decision_logs=[DecisionLog("app/DECISIONS.md", 1, 2, ["A"], True)],
                           trap_sets=[], working_dir=None, doc_dirs={})
        planned = Measurement(
            root=str(root), registry="projects.toml", registry_entries="project",
            registry_keys={}, governance_key_missing=True, workspace=m.workspace, scopes=[app],
            markers="gov", blocks=[], max_ids={"A": 950}, notes=[], subrepos=m.subrepos)
        p = propose(planned, None, None, {"shape": WORKSPACE, "repos": "app"})
        self.assertEqual(p.registry_file["project"][0]["id_range"], "1-9999")

    def test_trap_index_readiness_needs_an_index_migrate_can_convert(self):
        m = orbit(self.root("orbit"))
        m.scopes[0].trap_sets[0].has_index = False
        p = propose(m, None, None, {"shape": WORKSPACE, "repos": "client,server"})
        self.assertNotIn("trap-index", [b["id"] for b in p.config["blocks"]["project"]])
        self.assertTrue(any(n.startswith("no trap-index block for docs/working/traps.md")
                            for n in p.notes), p.notes)


class GlobMatches(unittest.TestCase):
    def test_segments(self):
        self.assertTrue(glob_matches("traps*.md", "traps-net.md"))
        self.assertFalse(glob_matches("traps*.md", "sub/traps.md"))
        self.assertTrue(glob_matches("**/*.md", "a/b/c.md"))
        self.assertTrue(glob_matches("**/*.md", "c.md"))


class TomlWriter(unittest.TestCase):
    def roundtrip(self, data: dict) -> str:
        text = tomlw.dumps(data)
        self.assertEqual(tomllib.loads(text), data)
        return text

    def test_nested_inline_tables(self):
        text = self.roundtrip({"checks": {"licences": {"conflicts": [
            {"a": {"licences": ["GPL"], "roles": ["runtime"], "deep": {"x": 1}}}]}}})
        self.assertIn('{ a = { licences = ["GPL"], roles = ["runtime"], deep = { x = 1 } } }', text)

    def test_lists_of_inline_tables(self):
        text = self.roundtrip({"blocks": {"project": [{"file": "D.md", "id": "decision-index"},
                                                      {"glob": "w/t*.md", "id": "trap-index"}],
                                          "workspace": []}})
        self.assertIn('  { file = "D.md", id = "decision-index" },\n', text)

    def test_nested_tables_and_empty_ones(self):
        text = self.roundtrip({"registry": {"file": "r.toml", "keys": {"handoff": "p.h"}},
                               "empty": {}, "only": {"sub": {"k": True}}})
        self.assertIn("[registry.keys]\nhandoff = \"p.h\"", text)
        self.assertNotIn("[only]\n", text)

    def test_escaping(self):
        s = 'quote " backslash \\ tab \t newline \n cr \r bell \x07 del \x7f é —'
        text = self.roundtrip({"k": s, "odd key.name": 1, "": "empty key"})
        self.assertNotIn("\x07", text)
        self.assertIn('"odd key.name" = 1', text)

    def test_key_order(self):
        data = {"z": 1, "a": {"y": 2, "b": 3}, "m": False}
        text = tomlw.dumps(data)
        self.assertEqual(tomllib.loads(text), data)
        self.assertEqual(list(tomllib.loads(text)), ["z", "m", "a"])   # tables follow plain keys
        self.assertEqual(list(tomllib.loads(text)["a"]), ["y", "b"])
        self.assertLess(text.index("z ="), text.index("m ="))

    def test_header(self):
        text = tomlw.dumps({"a": 1}, header="line one\n\nline two")
        self.assertTrue(text.startswith("# line one\n#\n# line two\n\na = 1\n"))

    def test_unsupported_type(self):
        with self.assertRaises(TypeError):
            tomlw.dumps({"f": 1.5})


if __name__ == "__main__":
    unittest.main()
