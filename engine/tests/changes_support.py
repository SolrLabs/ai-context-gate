"""`govern diff`, the report of what a branch did to governance, and `govern ci github`, the
workflow that runs the gate and the report on a pull request. Against real git history, in the
fixtures `base_support` builds: a repository in a temporary directory with a HOME of its own.

The report's text layout and its JSON keys are a contract other tools read, so one fixture's
output is kept byte for byte in `expected/`. To regenerate both files after a deliberate change:

    GOVERN_UPDATE_EXPECTED=1 python3 -m unittest discover -s engine/tests -k ExpectedOutput

and review the difference like any other change.

    python3 -m unittest discover -s engine/tests -k test_changes
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from govern import __version__, base, layout  # noqa: E402
from govern.context import REPO_ENV  # noqa: E402
from base_support import BLOCK, LOG, TRAPS, Case, Tree, decision, fm, trap, wtext  # noqa: E402

EXPECTED = Path(__file__).resolve().parent / "expected"
TEMPLATE = ENGINE / "govern" / "templates" / "ci-github.yml"
CONFIGURATION = ENGINE.parent / "docs" / "configuration.md"
NOTHING = "- nothing changed"
NOT_COMPARED = "- nothing compared"


def cut(w: Tree, rel: str, ident: str, keep: bool = False) -> str:
    """Take one entry out of a log, heading and body, and return it; with `keep`, return it
    and leave it where it is."""
    text = w.read(rel)
    m = re.search(rf"\n## {ident}\b.*?(?=\n## |\n<!-- |\Z)", text, re.S)
    assert m, f"{rel}: no entry {ident}"
    if not keep:
        w.write(rel, text[:m.start()] + text[m.end():])
    return m.group(0)


def row(change: str, log: str, ident: str, title: str, status: str | None, **more) -> dict:
    """One decision of the JSON form: every key, `null` for what does not apply."""
    return {"change": change, "log": log, "id": ident, "title": title, "status": status,
            "was_status": None, "was_log": None, "replaced_by": None, "revised": None, **more}


def sections(out: str) -> dict[str, list[str]]:
    """The report's text as `{section: its lines}`, with the heading line under `title` and a
    list that has no section name of its own (`- nothing changed`, `- nothing compared`) under
    `""`."""
    head, *blocks = out.rstrip("\n").split("\n\n")
    found = {"title": [head]}
    for block in blocks:
        first, *rest = block.split("\n")
        if first.startswith("- "):
            found[""] = [first, *rest]
        else:
            found[first] = rest
    return found


def tree_state(*dirs: Path) -> dict[str, tuple[int, bytes]]:
    """Every file under `dirs`, git's own included, with when it was written and what it holds."""
    return {str(p): (p.stat().st_mtime_ns, p.read_bytes())
            for d in dirs if d.exists() for p in sorted(d.rglob("*")) if p.is_file()}


class Diff(Case):
    def full(self, w: Tree, at: Path | None = None, ref: str = "HEAD") -> str:
        """A commit's whole id, as the JSON gives it."""
        return w.git("rev-parse", ref, at=at).strip()

    def head(self, w: Tree, at: Path | None = None, ref: str = "HEAD") -> str:
        """A commit's first 12 characters, as the text names it."""
        return self.full(w, at, ref)[:12]

    def text(self, w: Tree, *flags: str, env: dict | None = None) -> str:
        """The report: always exit 0, and nothing on stderr."""
        code, out, err = w.run("diff", *flags, env=env)
        self.assertEqual((code, err), (0, ""), out)
        self.assertTrue(out.isascii(), out)
        return out

    def report(self, w: Tree, *flags: str, env: dict | None = None) -> dict[str, list[str]]:
        """The report by section, without its heading line."""
        found = sections(self.text(w, *flags, env=env))
        del found["title"]
        return found

    def data(self, w: Tree, *flags: str) -> dict:
        code, out, err = w.run("diff", "--json", *flags)
        self.assertEqual((code, err), (0, ""), out)
        return json.loads(out)

    def changed(self, w: Tree, *flags: str) -> dict[str, list[str]]:
        """The report without "Compared", which every report ends with."""
        found = self.report(w, *flags)
        self.assertIn("Compared", found)
        del found["Compared"]
        return found


# ---------------------------------------------------------------------------- expected output

FIXED_FM = ("---\ndoc_type: {doc_type}\npurpose: test\naudience: agent\nload_when: testing\n"
            "last_reviewed: 2026-01-01\n---\n")

REFERENCE_CONFIG = """
[governance]
engine = "{version}"
schema = 1

[dialect]
markers = "gov"

[registry]
file = "projects.toml"
entries = "project"

[workspace]
docs = []
required_docs = []
decision_log = "docs/DECISIONS.md"

[projects]
required_docs = []
"""

REFERENCE_REGISTRY = """
[workspace]
id_prefix = "D"
id_range = "1-99"

[[project]]
name = "alpha"
dir = "projects/alpha"
tier = "full"
governance = "projects/alpha"
id_prefix = "A"
id_range = "100-199"

[[project]]
name = "beta"
dir = "projects/beta"
tier = "full"
governance = "projects/beta"
id_prefix = "B"
id_range = "200-299"

[[project]]
name = "delta"
dir = "projects/delta"
tier = "full"
governance = "projects/delta"
id_prefix = "C"
id_range = "300-399"
"""


class Fixed(Tree):
    """A tree whose commits have the same ids on every machine and in every time zone: one
    author, one date with its offset written out, SHA-1 objects, exact bytes. The config is
    never committed, since it names the engine version and that changes with every release.

    SHA-1 is asked for in the environment (`GIT_DEFAULT_HASH`), not by `init --object-format`:
    a git too old to know the variable has no other format, and one that knows it obeys it,
    so the fixture runs on both."""

    WHEN = "2026-01-02T03:04:05+00:00"

    def git(self, *args: str, at: Path | None = None, check: bool = True) -> str:
        env = {k: v for k, v in os.environ.items() if k not in REPO_ENV}
        env.update(HOME=str(self.home), GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0",
                   GIT_AUTHOR_DATE=self.WHEN, GIT_COMMITTER_DATE=self.WHEN,
                   GIT_DEFAULT_HASH="sha1")
        res = subprocess.run(
            ["git", "-C", str(at or self.root), "-c", "user.email=t@t", "-c", "user.name=t",
             "-c", "tag.gpgSign=false", "-c", "commit.gpgSign=false", "-c", "core.autocrlf=false",
             "-c", "core.fileMode=false", *args],
            check=check, capture_output=True, env=env)
        return res.stdout.decode("utf-8")

    def init(self, at: Path | None = None, branch: str = "main") -> None:
        self.git("init", "-q", at=at)
        self.git("symbolic-ref", "HEAD", f"refs/heads/{branch}", at=at)

    def commit(self, message: str = "work", at: Path | None = None, index: bool = False) -> None:
        self.git("add", "-A", "--", ".", f":(exclude){layout.CONFIG}", at=at)
        self.git("commit", "-qm", message, at=at)


def reference_tree(tmp: Path) -> Fixed:
    """A workspace on a branch that did one of everything the report has a word for: its own
    log and project `alpha` in one repository, project `beta` in a repository of its own, and
    project `delta` in one whose branch is not called `main`, so it cannot be compared."""
    w = Fixed(tmp / "reference", tmp / "home")
    control, working = FIXED_FM.format(doc_type="control"), FIXED_FM.format(doc_type="working")
    w.write(layout.CONFIG, REFERENCE_CONFIG.format(version=__version__))
    w.write("projects.toml", REFERENCE_REGISTRY)
    w.write(".gitignore", "projects/beta/\nprojects/delta/\n")
    w.write("docs/DECISIONS.md", control + "# Decisions\n"
            + decision("D", 8, title="Keep the log short").replace("## D-8 ", "## D-08 ")
            + decision("D", 9, "deferred", title="Maybe later").replace("## D-9 ", "## D-09 ")
            + decision("D", 12, title="One writer per file")
            + decision("D", 14, "provisional", title="Review before merge")
            + decision("D", 15, title="Alpha owns its schema")
            + decision("D", 20, "provisional", title="Weekly cleanup"))
    for name, prefix, lo in (("alpha", "A", 100), ("beta", "B", 200), ("delta", "C", 300)):
        w.write(f"projects/{name}/DECISIONS.md", control + "# Decisions\n" + decision(prefix, lo))
        w.write(f"projects/{name}/working-files/traps.md",
                working + "# Traps\n" + trap(3) + trap(5))
    w.write(layout.BASELINE, json.dumps({
        "decision_words:alpha:A-100": 900, "handoff_words:alpha": 1720,
        "working_file_words:docs/plan.md": 6100}, indent=2, sort_keys=True) + "\n")
    w.init()
    w.commit("start")
    w.init(at=w.root / "projects/beta")
    w.commit("start", at=w.root / "projects/beta")
    w.init(at=w.root / "projects/delta", branch="trunk")
    w.commit("start", at=w.root / "projects/delta")

    # What landed on main after the branch began is not the branch's.
    w.branch("work")
    w.switch("main")
    w.append("docs/DECISIONS.md", decision("D", 30, title="Landed on main meanwhile"))
    w.commit("theirs")
    w.switch("work")

    log = "docs/DECISIONS.md"
    cut(w, log, "D-09")                                                      # removed
    cut(w, log, "D-12")
    w.append(log, "\n## D-12 - Replaced by D-31\n")                          # superseded
    w.append("projects/alpha/DECISIONS.md", cut(w, log, "D-15"))             # moved
    w.append(log, cut(w, "projects/alpha/DECISIONS.md", "A-100")             # moved, and changed
             .replace("always write the rule down", "write the rule down"))
    w.edit(log, "Keep the log short\n\n**Status:** locked\n",                # revised
           "Keep the log short\n\n**Status:** locked\n\n**Revised:** 2026-10-06 (shorter)\n")
    w.edit(log, "Review before merge\n\n**Status:** provisional",            # status
           "Review before merge\n\n**Status:** locked")
    w.edit(log, "## D-20 - Weekly cleanup", "## D-20 - Monthly cleanup")     # changed
    w.append(log, decision("D", 31, "provisional", title="Two writers, one log"))   # added
    cut(w, "projects/alpha/working-files/traps.md", "T-3")
    w.append("projects/alpha/working-files/traps.md", trap(7, title="The cache is stale"))
    w.write(layout.BASELINE, json.dumps({
        "decision_words:alpha:A-100": 700, "governed_docs:alpha": 41,
        "working_file_words:docs/plan.md": 6400}, indent=2, sort_keys=True) + "\n")
    w.commit("ours")
    # Uncommitted work is part of what the branch did. The same trap id in another project,
    # in its own repository, is another trap.
    w.edit("projects/alpha/working-files/traps.md", "it is 5 o'clock", "it is after 5 o'clock")
    w.append("projects/beta/working-files/traps.md", trap(7, title="The clock is local"))
    w.append("projects/delta/DECISIONS.md", decision("C", 301))
    return w


# ---------------------------------------------------------------------------- ci github

WORKFLOW_STEPS = """\
    steps:
      - uses: actions/checkout@v4
        with:
          # The whole history: the gate compares with the commit this branch left the base at.
          fetch-depth: 0
          # The branch's own commit, not GitHub's merge of it into the base: by the pull
          # request's own ref, which is there to fetch when the branch is in a fork.
          ref: refs/pull/${{ github.event.pull_request.number }}/head
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: Gate
        env:
          BASE_REF: ${{ github.base_ref }}
        run: python3 .context-gate/bin/govern check --base "origin/$BASE_REF"
      - name: What this pull request changes
        if: always()
        env:
          BASE_REF: ${{ github.base_ref }}
        run: python3 .context-gate/bin/govern diff --base "origin/$BASE_REF" >> "$GITHUB_STEP_SUMMARY"
"""


__all__ = [
    "json",
    "os",
    "re",
    "subprocess",
    "sys",
    "unittest",
    "Path",
    "ENGINE",
    "__version__",
    "base",
    "layout",
    "REPO_ENV",
    "BLOCK",
    "LOG",
    "TRAPS",
    "Case",
    "Tree",
    "decision",
    "fm",
    "trap",
    "wtext",
    "EXPECTED",
    "TEMPLATE",
    "CONFIGURATION",
    "NOTHING",
    "NOT_COMPARED",
    "cut",
    "row",
    "sections",
    "tree_state",
    "Diff",
    "FIXED_FM",
    "REFERENCE_CONFIG",
    "REFERENCE_REGISTRY",
    "Fixed",
    "reference_tree",
    "WORKFLOW_STEPS",
]
