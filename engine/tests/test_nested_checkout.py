"""A project that is its own git checkout, nested under a workspace root whose
`.gitignore` lists it. Every git question about a file inside it is asked of the checkout's own
repo, never of the root that ignores the whole directory — so `index`, the doc checks, `measure`
and the git history a check reads all see the project's docs.

    python3 -m unittest discover -s engine/tests -k nested
"""
from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))

from govern import __version__, cli, config, layout, measure, registry  # noqa: E402
from govern.context import Context  # noqa: E402

TODAY = date.today().isoformat()
NESTED = "nested-mod"

CONFIG = f"""
[governance]
engine = "{__version__}"
schema = 1

[dialect]
decision_heading = "em-dash"
markers = "t"

[registry]
file = "mods.toml"
entries = "mod"

[workspace]
required_docs = ["AGENTS.md"]
docs = ["AGENTS.md"]
decision_log = "governance/DECISIONS.md"

[projects]
required_docs = ["DECISIONS.md"]

[blocks]
project = [{{ file = "DECISIONS.md", id = "decision-index" }},
           {{ file = "docs/INDEX.md", id = "doc-registry" }}]

[checks.doc-reachability]
index = "docs/INDEX.md"

[checks.governed-doc-count]
max_docs = 1
"""

MODS = f"""
[workspace]
id_prefix = "W"
id_range = "1-99"

[[mod]]
name = "{NESTED}"
dir = "{NESTED}"
tier = "full"
governance = "{NESTED}"
id_prefix = "M"
id_range = "100-199"
"""

REGISTRY_BLOCK = ("<!-- t:generated:start id=doc-registry -->\n"
                  "<!-- t:generated:end id=doc-registry -->\n")


def fm(doc_type="reference", **extra) -> str:
    fields = {"doc_type": doc_type, "purpose": "test", "audience": "agent",
              "load_when": "testing", "last_reviewed": TODAY, **extra}
    return "---\n" + "".join(f"{k}: {v}\n" for k, v in fields.items() if v is not None) + "---\n"


def wtext(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(content)


def git(repo: Path, *args: str, env: dict | None = None) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                           "-c", "commit.gpgSign=false", *args], check=True, capture_output=True,
                          env=env).stdout.decode("utf-8")


class Fixture(unittest.TestCase):
    """A `mods.toml` workspace whose one mod is its own repo, which the root's `.gitignore`
    lists: three reference docs and the doc index under `docs/`, plus one scratch doc the mod's
    own `.gitignore` excludes."""

    DOCS = ("docs/a.md", "docs/b.md", "docs/c.md")

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "ws"
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.mod = self.root / NESTED
        self.write(layout.CONFIG, CONFIG)
        self.write("mods.toml", MODS)
        self.write("AGENTS.md", fm("control") + "# Agents\n")
        self.write("governance/DECISIONS.md", fm("control") + "# Decisions\n")
        self.write(".gitignore", f"{NESTED}/\n")
        git(self.root, "init", "-q")
        self.write(f"{NESTED}/DECISIONS.md",
                   fm("control") + "# Decisions\n\n<!-- t:generated:start id=decision-index -->\n"
                   "<!-- t:generated:end id=decision-index -->\n")
        self.write(f"{NESTED}/docs/INDEX.md", fm("control") + "# Index\n\n" + REGISTRY_BLOCK)
        for rel in self.DOCS:
            self.write(f"{NESTED}/{rel}", fm() + "# Doc\n")
        self.write(f"{NESTED}/.gitignore", "docs/scratch.md\n")
        self.write(f"{NESTED}/docs/scratch.md", "no frontmatter at all\n")
        git(self.mod, "init", "-q")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, rel: str, content: str) -> Path:
        path = self.root / rel
        wtext(path, content)
        return path

    def run_cli(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                try:
                    code = cli.main(list(argv), root=self.root)
                except SystemExit as exc:
                    code = exc.code
        finally:
            if old_home is not None:
                os.environ["HOME"] = old_home
        return code, out.getvalue()

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

    def governed(self) -> list[str]:
        ctx = self.context()
        return [rel for rel, _ in ctx.governed_docs(ctx.registry.scopes[0].gov)]

    def registry_block(self) -> str:
        text = (self.mod / "docs" / "INDEX.md").read_text(encoding="utf-8")
        return text.split("id=doc-registry -->", 1)[1].split("<!--", 1)[0]


class NestedCheckout(Fixture):
    # ------------------------------------------------------------------ ignores

    def test_a_nested_checkout_ignored_by_the_root_is_still_governed(self):
        self.assertEqual(self.governed(),
                         ["DECISIONS.md", "docs/INDEX.md", *self.DOCS])

    def test_the_checkouts_own_gitignore_still_excludes(self):
        self.assertNotIn("docs/scratch.md", self.governed())

    def test_measure_and_context_agree(self):
        m = measure.measure(self.root)
        scope = next(s for s in m.scopes if s.name == NESTED)
        docs = [rel for rel in self.governed() if rel.startswith("docs/")]
        self.assertEqual(scope.doc_dirs["docs"], (len(docs), len(docs)))

    def test_a_failing_git_in_one_repo_reads_as_not_ignored(self):
        real = subprocess.run

        def failing(argv, *a, **kw):
            if "check-ignore" in argv:
                return subprocess.CompletedProcess(argv, 128, b"", b"fatal: lock")
            return real(argv, *a, **kw)
        with mock.patch("govern.context.subprocess.run", failing):
            self.assertIn("docs/scratch.md", self.governed())

    # ------------------------------------------------------------------ index and checks

    def test_index_keeps_the_checkouts_docs(self):
        code, out = self.run_cli("index")
        self.assertEqual(code, 0, out)
        block = self.registry_block()
        for rel in self.DOCS:
            self.assertIn(f"]({Path(rel).name})", block)
        self.assertNotIn("scratch.md", block)
        self.assertNotIn("is now empty", out)

    def test_the_doc_checks_see_the_checkouts_docs(self):
        self.write(f"{NESTED}/docs/c.md", fm(purpose=None) + "# Doc\n")
        code, out = self.run_cli("check")
        # doc-reachability: the never-indexed registry lists none of the three docs.
        for rel in self.DOCS:
            self.assertIn(f"{NESTED}/{rel}: not reachable from docs/INDEX.md", out)
        # doc-frontmatter: the one doc missing a key is read.
        self.assertIn(f"{NESTED}/docs/c.md: frontmatter missing 'purpose'", out)
        # governed-doc-count: all five, and never the ignored scratch doc.
        self.assertIn(f"{NESTED}: 5 governed docs exceeds governed-doc-count", out)
        self.assertNotIn("scratch.md", out)

    def test_a_hooks_git_dir_never_redirects_the_checkouts_answers(self):
        """A git hook exports `GIT_DIR` (and friends) for its own repo; each overrides `-C`, so
        every answer about the nested checkout would come from the root's repo instead."""
        self.write(f"{NESTED}/docs/c.md", fm(purpose=None) + "# Doc\n")
        _, want_check = self.run_cli("check")
        hook_env = {"GIT_DIR": str(self.root / ".git"), "GIT_WORK_TREE": str(self.root),
                    "GIT_INDEX_FILE": str(self.root / ".git" / "index"), "GIT_PREFIX": ""}
        with mock.patch.dict(os.environ, hook_env):
            self.assertEqual(self.governed(), ["DECISIONS.md", "docs/INDEX.md", *self.DOCS])
            _, got_check = self.run_cli("check")
            code, out = self.run_cli("index")
        self.assertEqual(got_check, want_check)
        self.assertEqual(code, 0, out)
        for rel in self.DOCS:
            self.assertIn(f"]({Path(rel).name})", self.registry_block())
        self.assertNotIn("scratch.md", self.registry_block())

    # ------------------------------------------------------------------ git history

    def _commit_in_2020(self, rel: str) -> None:
        env = {**os.environ, "GIT_AUTHOR_DATE": "2020-01-01T00:00:00",
               "GIT_COMMITTER_DATE": "2020-01-01T00:00:00"}
        git(self.mod, "add", rel)
        git(self.mod, "commit", "-qm", "x", env=env)

    def test_committed_and_last_touched_ask_the_checkout(self):
        self._commit_in_2020("docs/a.md")
        ctx = self.context()
        self.assertTrue(ctx.committed(self.mod / "docs" / "a.md"))
        self.assertEqual(ctx.last_touched(self.mod / "docs" / "a.md").year, 2020)
        self.assertFalse(ctx.committed(self.mod / "docs" / "b.md"))
        self.assertGreater(ctx.last_touched(self.mod / "docs" / "b.md"),
                           datetime(2021, 1, 1))

    def test_a_deleted_link_target_is_found_in_the_checkouts_history(self):
        self._commit_in_2020("docs/a.md")
        git(self.mod, "rm", "-q", "docs/a.md")
        git(self.mod, "commit", "-qm", "gone")
        self.write(f"{NESTED}/docs/b.md", fm() + "# Doc\n\nSee [a](a.md).\n")
        code, out = self.run_cli("check")
        self.assertIn(f"{NESTED}/docs/b.md", out)
        self.assertIn("link target does not exist: a.md — deleted in", out)


class IndexEmptiesABlock(Fixture):
    """`index` still writes a block regenerated with no entries (removing every doc can be
    deliberate), but names it and says how many entries it removed."""

    def test_emptying_a_block_warns_and_still_writes(self):
        self.run_cli("index")
        for rel in self.DOCS:
            (self.mod / rel).unlink()
        # The two docs left lose their frontmatter, so neither is listed any more.
        self.write(f"{NESTED}/DECISIONS.md", "# Decisions\n\n"
                   "<!-- t:generated:start id=decision-index -->\n"
                   "<!-- t:generated:end id=decision-index -->\n")
        index = (self.mod / "docs" / "INDEX.md").read_text(encoding="utf-8")
        self.write(f"{NESTED}/docs/INDEX.md", index.split("---\n", 2)[2])
        code, out = self.run_cli("index")
        self.assertEqual(code, 0, out)
        self.assertIn(f"regenerated {NESTED}/docs/INDEX.md :: doc-registry", out)
        self.assertIn(f"warning     {NESTED}/docs/INDEX.md :: doc-registry is now empty: "
                      f"regenerating removed all 5 of its entries", out)
        self.assertEqual(self.registry_block().strip(), "")
        _, again = self.run_cli("index")
        self.assertNotIn("is now empty", again)

    def test_a_block_that_keeps_entries_does_not_warn(self):
        self.run_cli("index")
        (self.mod / "docs" / "a.md").unlink()
        _, out = self.run_cli("index")
        self.assertIn(f"regenerated {NESTED}/docs/INDEX.md :: doc-registry", out)
        self.assertNotIn("is now empty", out)


if __name__ == "__main__":
    unittest.main()
