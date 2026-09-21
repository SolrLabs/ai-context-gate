"""A `config.toml` with no `[registry]` table: the governance root governs itself, the one
project it is. The fixture is shaped like a real single repo — one decision log under `docs/`,
a `docs/HANDOFF.md`, agents under `.claude/agents/` — never a shape the code happens to expect.

    python3 -m unittest discover -s engine/tests -k single_repo
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))

from govern import __version__, cli, config, installer, layout, migrate, registry  # noqa: E402
from govern.context import Context  # noqa: E402

CFG = layout.CONFIG
TODAY = date.today().isoformat()

CONFIG = """
[governance]
engine = "{version}"
schema = 1

[dialect]
markers = "gov"

[workspace]
docs = ["docs/**/*.md"]
required_docs = []
decision_log = "docs/decisions/DECISIONS.md"
agents_dir = ".claude/agents"

[projects]
docs = ["docs/**/*.md"]
required_docs = []
decision_log = "docs/decisions/DECISIONS.md"

[blocks]
project = [{{ file = "docs/decisions/DECISIONS.md", id = "decision-index" }}]

[repo]
tier = "full"
id_prefix = "D"
id_range = "1-999"
handoff = "docs/HANDOFF.md"
{extra}
"""


def wtext(path: Path, content: str) -> None:
    """Exactly these characters, on every OS: UTF-8, no newline translation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(content)


def fm(doc_type="control", **extra) -> str:
    lines = ["---", f"doc_type: {doc_type}", "purpose: test", "audience: agent",
             "load_when: testing", f"last_reviewed: {extra.pop('last_reviewed', TODAY)}"]
    lines += [f"{k}: {v}" for k, v in extra.items()]
    return "\n".join(lines + ["---", ""])


def entry(num: int, status="locked", body="**Rule:** r.\n\n**Why:** w.\n") -> str:
    return f"\n## D-{num} — Title {num}\n\n**Status:** {status}\n\n{body}"


DECISIONS_BLOCK = ("<!-- gov:generated:start id=decision-index -->\n"
                   "<!-- gov:generated:end id=decision-index -->\n")


class SingleRepo:
    """A plain single repo: no registry, no sub-projects."""

    def __init__(self, tmp: Path, *, extra: str = "", decisions: str | None = None) -> None:
        self.root = tmp / "myproject"
        self.home = tmp / "home"
        self.home.mkdir()
        self.write(CFG, CONFIG.format(version=__version__, extra=extra))
        wtext(self.root / "docs/decisions/DECISIONS.md",
             fm() + "# Decisions\n\n" + DECISIONS_BLOCK
             + (decisions if decisions is not None else entry(1)))
        self.write("docs/HANDOFF.md", fm("working", status="active") + "# Handoff\n\nAll quiet.\n")
        self.write(".claude/agents/reviewer.md",
                  "---\nname: reviewer\ndescription: reviews things\nmodel: sonnet\n"
                  "effort: medium\nomitClaudeMd: true\n---\nReviews things.\n")
        self.index()

    def write(self, rel: str, content: str) -> Path:
        path = self.root / rel
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


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def repo(self, **kw) -> SingleRepo:
        return SingleRepo(self.tmp, **kw)


class CheckAndIndex(Base):
    def test_clean_fixture_passes(self):
        code, out, _ = self.repo().run("check")
        self.assertEqual(code, 0, out)

    def test_name_defaults_to_the_root_directory_name(self):
        r = self.repo()
        reg = r.context().registry
        self.assertEqual(len(reg.scopes), 1)
        self.assertEqual(reg.scopes[0].name, "myproject")
        self.assertEqual(reg.scopes[0].gov, r.root)

    def test_index_regenerates_the_decision_block(self):
        r = self.repo()
        r.write("docs/decisions/DECISIONS.md",
                fm() + "# Decisions\n\n" + DECISIONS_BLOCK + entry(1) + entry(2))
        code, out, _ = r.run("index")
        self.assertEqual(code, 0, out)
        self.assertIn("D-1", (r.root / "docs/decisions/DECISIONS.md").read_text(encoding="utf-8"))
        code, out, _ = r.run("check")
        self.assertEqual(code, 0, out)


class Ids(Base):
    def test_next_id_uses_the_repo_id_range(self):
        r = self.repo()
        code, out, _ = r.run("next-id", "--project", "myproject")
        self.assertEqual(code, 0, out)
        self.assertEqual(out.strip(), "D-2")

    def test_show_and_find(self):
        r = self.repo()
        code, out, _ = r.run("show", "--project", "myproject", "D-1")
        self.assertEqual(code, 0, out)
        self.assertIn("D-1", out)
        self.assertIn("Title 1", out)
        code, out, _ = r.run("find", "--project", "myproject", "Title")
        self.assertEqual(code, 0, out)
        self.assertIn("D-1", out)


class RealInstaller(Base):
    def test_install_with_the_real_installer(self):
        r = self.repo()
        cfg = self.tmp / "config.toml"
        (r.root / CFG).rename(cfg)
        (r.root / layout.GOV_DIR).rmdir()
        out = io.StringIO()
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(r.home)
        try:
            with contextlib.redirect_stdout(out):
                code = installer.main(["install", "--root", str(r.root), "--config", str(cfg)])
        finally:
            if old_home is not None:
                os.environ["HOME"] = old_home
        self.assertEqual(code, 0, out.getvalue())
        self.assertTrue((r.root / layout.CONFIG).is_file())
        self.assertTrue((r.root / layout.GOV_DIR / "install-report.md").is_file())


class MigrateDryRun(Base):
    def test_migrate_dry_run_on_an_already_standard_project(self):
        r = self.repo()
        code = migrate.run(r.root, False)
        self.assertEqual(code, 0)
        self.assertTrue((r.root / layout.GOV_DIR / "migration-report.md").is_file())


class RegistryStillErrors(Base):
    def test_registry_naming_a_missing_file_still_errors(self):
        r = self.repo(extra="")
        cfg = r.root / CFG
        text = cfg.read_text(encoding="utf-8")
        text = text.replace("[repo]\ntier = \"full\"\nid_prefix = \"D\"\nid_range = \"1-999\"\n"
                            "handoff = \"docs/HANDOFF.md\"\n", "[registry]\n")
        wtext(cfg, text)
        code, out, err = r.run("check")
        self.assertEqual(code, 2)
        self.assertIn("registry.toml", err)
        self.assertIn("not found", err)

    def test_registry_and_repo_both_set_is_refused(self):
        r = self.repo()
        cfg = r.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8") + "\n[registry]\n")
        code, out, err = r.run("check")
        self.assertEqual(code, 2)
        self.assertIn("[registry] and [repo]", err)


class NoDoubleReporting(Base):
    """The workspace's decision log and the one project's are configured to the same file —
    single-repo mode's default shape. A check that runs at both scopes must not report it
    twice."""

    def test_a_malformed_status_is_reported_once(self):
        r = self.repo(decisions=entry(1, status="bogus"))
        code, out, _ = r.run("check")
        self.assertEqual(code, 1)
        self.assertEqual(out.count("status 'bogus' not in"), 1, out)

    def test_a_doc_frontmatter_problem_is_reported_once(self):
        r = self.repo()
        r.write("docs/HANDOFF.md", "---\ndoc_type: manual\n---\n# Handoff\n")
        code, out, _ = r.run("check")
        self.assertEqual(code, 1)
        self.assertEqual(out.count("doc_type 'manual' not in"), 1, out)

    def test_missing_shared_log_is_reported_once(self):
        r = self.repo()
        (r.root / "docs/decisions/DECISIONS.md").unlink()
        code, out, _ = r.run("check")
        self.assertEqual(code, 1)
        self.assertEqual(out.count("decision log is missing"), 1, out)

    def test_a_broken_link_in_the_shared_doc_is_reported_once(self):
        r = self.repo()
        r.write("docs/HANDOFF.md", fm("working", status="active")
               + "# Handoff\n\nSee [gone](missing.md).\n")
        code, out, _ = r.run("check")
        self.assertEqual(code, 1, out)
        self.assertEqual(out.count("link target does not exist: missing.md"), 1, out)

    def test_a_decision_ratchet_breach_has_one_key_not_two(self):
        r = self.repo(extra="\n[checks.decision-log]\nmax_words = 1\n")
        code, _, _ = r.run("baseline", "--allow-raise")
        self.assertEqual(code, 0)
        baseline = json.loads((r.root / layout.BASELINE).read_text(encoding="utf-8"))
        decision_keys = [k for k in baseline if k.startswith("decision_words:")]
        self.assertEqual(len(decision_keys), 1, baseline)

    def test_second_collect_on_one_context_gives_identical_findings(self):
        r = self.repo(decisions=entry(1, status="bogus"))
        ctx = r.context()
        first = cli.collect(ctx, ctx.registry.scopes, workspace=True)
        second = cli.collect(ctx, ctx.registry.scopes, workspace=True)
        flatten = lambda results: [(cid, f.errors, f.warnings) for cid, f in results]
        self.assertEqual(flatten(first), flatten(second))
        self.assertTrue(any(f.errors for _, f in first), first)   # not a vacuous comparison


class RepoRelativeLabels(Base):
    """A single repo's one scope is the governance root itself: a finding names the file by
    its repo-relative path, never that path prefixed with the scope's own name a second time,
    so `doc-frontmatter` (project) and `ratchet` (workspace) name the very same file alike."""

    def test_the_project_finding_matches_the_ratchet_key_on_the_same_file(self):
        r = self.repo(extra="\n[checks.doc-frontmatter]\nmax_working_words = 2\n")
        r.write("docs/HANDOFF.md", fm("working", status="active") + "# Handoff\n\n" + "word " * 10)
        code, out, _ = r.run("check")
        self.assertEqual(code, 1, out)
        self.assertIn("docs/HANDOFF.md: ", out)
        self.assertIn("working_file_words:docs/HANDOFF.md", out)
        self.assertNotIn("myproject/docs/HANDOFF.md", out)


class GeneratedBlockOwnership(Base):
    """A generated block configured under both `[blocks] workspace]` and `[blocks] project]`,
    pointed at the same file and id — single-repo mode's default shape stretched to blocks too:
    the project scope owns the file, so the workspace-shaped target is left to the project's own
    pass over `[blocks] project]`, and a stale block is reported once."""

    def repo(self) -> SingleRepo:
        return SingleRepo(self.tmp, extra='\n[[blocks.workspace]]\nfile = '
                          '"docs/decisions/DECISIONS.md"\nid = "decision-index"\n')

    def test_a_stale_shared_block_is_reported_once(self):
        r = self.repo()
        log = r.root / "docs/decisions/DECISIONS.md"
        wtext(log, log.read_text(encoding="utf-8") + entry(2))
        code, out, _ = r.run("check")
        self.assertEqual(code, 1, out)
        self.assertEqual(out.count("generated block 'decision-index' is stale"), 1, out)


MINIMAL_CONFIG = "[governance]\nengine = \"{version}\"\nschema = 1\n"


class MinimalRepo:
    """A `config.toml` with only `[governance]`, and a README — the smallest a single repo can
    be. No `[repo]`, `[workspace]` or `[projects]` table names anything at all."""

    def __init__(self, tmp: Path) -> None:
        self.root = tmp / "bare"
        self.home = tmp / "barehome"
        self.home.mkdir()
        self.write(CFG, MINIMAL_CONFIG.format(version=__version__))
        self.write("README.md", fm() + "# Bare\n")

    def write(self, rel: str, content: str) -> Path:
        path = self.root / rel
        wtext(path, content)
        return path

    def run(self, *argv: str) -> tuple[int, str, str]:
        return SingleRepo.run(self, *argv)

    def context(self) -> Context:
        return SingleRepo.context(self)


class MinimalSingleRepo(Base):
    def repo(self) -> MinimalRepo:
        return MinimalRepo(self.tmp)

    def test_it_does_not_crash_and_the_missing_log_is_reported_once(self):
        r = self.repo()
        code, out, _ = r.run("check")
        self.assertNotEqual(code, 2, out)          # never a config/usage error
        self.assertEqual(out.count("decision log is missing"), 1, out)

    def test_missing_log_is_reported_against_the_projects_own_path_not_a_phantom_workspace_one(self):
        # [repo] governance names a subdirectory, narrower than the root the workspace's own
        # decision_log default ("governance/DECISIONS.md") would fall under: with no
        # [workspace] decision_log of its own, single-repo mode has no second, workspace-only
        # log to be missing — only the project's own (at its governance dir) does.
        r = self.repo()
        cfg = r.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8") + '\n[repo]\ngovernance = "sub"\n')
        (r.root / "sub").mkdir()
        code, out, _ = r.run("check")
        self.assertEqual(out.count("decision log is missing"), 1, out)
        self.assertIn("sub/DECISIONS.md: decision log is missing", out)
        self.assertNotIn("governance/DECISIONS.md", out)

    def test_tier_defaults_to_the_first_governed_tier(self):
        r = self.repo()
        scope = r.context().registry.scopes[0]
        self.assertEqual(scope.get("tier"), "full")
        self.assertTrue(scope.doc_set)

    def test_governed_docs_skip_the_default_vendored_excludes(self):
        r = self.repo()
        r.write("node_modules/pkg/README.md", "not frontmatter at all\n")
        ctx = r.context()
        rels = [rel for rel, _ in ctx.governed_docs(ctx.registry.scopes[0].gov)]
        self.assertNotIn("node_modules/pkg/README.md", rels)
        code, out, _ = r.run("check")
        self.assertNotIn("node_modules", out)

    def test_governed_docs_skip_a_git_ignored_file(self):
        r = self.repo()

        def git(*args):
            subprocess.run(["git", "-C", str(r.root), "-c", "user.email=t@t",
                            "-c", "user.name=t", *args], check=True, capture_output=True)

        git("init", "-q")
        r.write(".gitignore", "scratch.md\n")
        r.write("scratch.md", "not frontmatter at all\n")
        ctx = r.context()
        rels = [rel for rel, _ in ctx.governed_docs(ctx.registry.scopes[0].gov)]
        self.assertNotIn("scratch.md", rels)
        self.assertIn("README.md", rels)


if __name__ == "__main__":
    unittest.main()
