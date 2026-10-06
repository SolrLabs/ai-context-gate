"""Comparing against a base (`govern.base`, `check --base <ref>`) and the `decision-changes`
check, against real git history: every fixture is a repository built in a temporary directory,
with a HOME of its own, committed to and branched the way a project is.

The property everything else here serves: a project with one writer doing ordinary work sees
nothing new. `Case.silent` proves it the strong way, by running the gate with the check
registered and without it and comparing every byte of what comes back.

    python3 -m unittest discover -s engine/tests -k test_base
"""
from __future__ import annotations

import collections
import contextlib
import io
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))

from govern import __version__, base, cli, config, layout, manifest, registry  # noqa: E402
from govern.context import REPO_ENV, Context  # noqa: E402

CHECK = "decision-changes"
TODAY = date.today().isoformat()
# With its UTC offset written out: without one git reads the date in the machine's own time
# zone, and a commit id would depend on where the test ran.
STAMP = "2020-01-01T00:00:00+00:00"
WARN, ERROR = "  warn   ", "  ERROR  "
LOG = "docs/DECISIONS.md"
TRAPS = "docs/working/traps.md"

BLOCK = ("<!-- gov:generated:start id=decision-index -->\n"
         "<!-- gov:generated:end id=decision-index -->\n")
TRAP_BLOCK = ("<!-- gov:generated:start id=trap-index -->\n"
              "<!-- gov:generated:end id=trap-index -->\n")

SOLO_CONFIG = """
[governance]
engine = "{version}"
schema = 1

[dialect]
markers = "gov"

[workspace]
docs = []
required_docs = []
{workspace}

[projects]
docs = ["docs/**/*.md"]
required_docs = []
decision_log = "docs/DECISIONS.md"
working_dir = "docs/working"

[blocks]
project = [{{ file = "docs/DECISIONS.md", id = "decision-index" }},
           {{ file = "docs/working/traps.md", id = "trap-index" }}]

[repo]
tier = "full"
id_prefix = "P"
id_range = "1-999"
{extra}
"""

TEAM_CONFIG = """
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
decision_log = "governance/DECISIONS.md"

[projects]
required_docs = []

[blocks]
project = [{{ file = "DECISIONS.md", id = "decision-index" }}]
{extra}
"""

TEAM_REGISTRY = """
[workspace]
id_prefix = "W"
id_range = "1-99"
"""

PROJECT = """
[[project]]
name = "{name}"
dir = "{dir}"
tier = "full"
governance = "{dir}"
id_prefix = "{prefix}"
id_range = "{lo}-{hi}"
"""


def wtext(path: Path, content: str) -> None:
    """Exactly these characters, on every OS: UTF-8, no newline translation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(content)


def rtext(path: Path) -> str:
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def fm(doc_type="control", **extra) -> str:
    fields = {"doc_type": doc_type, "purpose": "test", "audience": "agent",
              "load_when": "testing", **extra, "last_reviewed": TODAY}
    return "---\n" + "".join(f"{k}: {v}\n" for k, v in fields.items()) + "---\n"


def decision(prefix: str, num: int, status: str | None = "locked", sep: str = " - ",
             title: str | None = None, body: str | None = None) -> str:
    """One entry, as a project writes it."""
    if body is None:
        body = ("**Rule:** always write the rule down before acting on it.\n\n"
                "**Why:** the next reader was not in the room.\n")
    state = f"**Status:** {status}\n\n" if status else ""
    return f"\n## {prefix}-{num}{sep}{title or f'Title {num}'}\n\n{state}{body}"


def trap(num: int, title: str | None = None, body: str | None = None) -> str:
    return (f"\n## T-{num} - {title or f'Trap {num}'}\n\n**Bites when:** it is {num} o'clock.\n\n"
            f"{body or 'Look twice.'}\n")


# A rewording of a default entry's rule: of the first entry in a log, replaced once.
REWORD = ("**Rule:** always write", "**Rule:** never write")


def changed(log: str, ident: str, status: str = "locked") -> str:
    return (f"{log}: {ident} ({status}) changed without a new dated **Revised:** line "
            f"— add `**Revised:** YYYY-MM-DD (what changed)`, or supersede it with a new decision")


def removed(log: str, ident: str, status: str, heading: str) -> str:
    return (f"{log}: {ident} ({status}) was removed — a decision is superseded, not deleted: "
            f"reduce it to `{heading}`")


def taken(log: str, ident: str, ref: str, title: str) -> str:
    return (f"{log}: {ident} was also added on {ref} since this branch began (\"{title}\") — "
            f"if it is the same entry, merge {ref} into this branch; if not, renumber yours to an "
            f"id neither side uses and update what cites it")


def no_compare(ref: str, reason: str, repo: str | None = None) -> str:
    return (f"{CHECK}: could not compare {repo} with {ref} — {reason}" if repo
            else f"{CHECK}: could not compare with {ref} — {reason}")


@contextlib.contextmanager
def unregistered(cid: str = CHECK):
    """The engine as it was before this check existed, restored exactly on the way out."""
    checks, order = list(manifest.CHECKS.items()), list(manifest._ORDER)
    del manifest.CHECKS[cid]
    manifest._ORDER.remove(cid)
    try:
        yield
    finally:
        manifest.CHECKS.clear()
        manifest.CHECKS.update(checks)
        manifest._ORDER[:] = order


class Tree:
    """A governance root on disk: its files, the gate, and git — every git call with this
    fixture's own HOME, never the machine's, so no global setting (signing, a hook path, line
    ending conversion) reaches a test."""

    def __init__(self, root: Path, home: Path) -> None:
        self.root, self.home = root, home
        home.mkdir(parents=True, exist_ok=True)

    def write(self, rel: str, content: str | bytes) -> Path:
        path = self.root / rel
        if isinstance(content, bytes):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        else:
            wtext(path, content)
        return path

    def read(self, rel: str) -> str:
        return rtext(self.root / rel)

    def edit(self, rel: str, old: str, new: str) -> None:
        """Replace the one place `old` occurs: an edit that silently missed proves nothing."""
        text = self.read(rel)
        assert text.count(old) == 1, f"{rel}: {old!r} occurs {text.count(old)} times"
        self.write(rel, text.replace(old, new))

    def append(self, rel: str, more: str) -> None:
        self.write(rel, self.read(rel) + more)

    def run(self, *argv: str, env: dict | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        patch = {"HOME": str(self.home), "CONTEXT_GATE_NO_UPDATE_CHECK": "1",
                 "GIT_CONFIG_NOSYSTEM": "1", **(env or {})}
        with mock.patch.dict(os.environ, patch), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            try:
                code = cli.main(list(argv), root=self.root)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def context(self, base_ref: str | None = None) -> Context:
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}):
            cfg = config.load(self.root, layout.home())
            return Context(root=self.root, home=layout.home(), cfg=cfg,
                           registry=registry.load(cfg), prog="govern", base=base_ref)

    def git(self, *args: str, at: Path | None = None, check: bool = True) -> str:
        env = {k: v for k, v in os.environ.items() if k not in REPO_ENV}
        env.update(HOME=str(self.home), GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0",
                   GIT_AUTHOR_DATE=STAMP, GIT_COMMITTER_DATE=STAMP)
        res = subprocess.run(
            ["git", "-C", str(at or self.root), "-c", "user.email=t@t", "-c", "user.name=t",
             "-c", "tag.gpgSign=false", "-c", "commit.gpgSign=false", "-c", "core.autocrlf=false",
             "-c", "protocol.file.allow=always", *args],
            check=check, capture_output=True, env=env)
        return res.stdout.decode("utf-8")

    def init(self, at: Path | None = None, branch: str = "main") -> None:
        self.git("init", "-q", at=at)
        self.git("symbolic-ref", "HEAD", f"refs/heads/{branch}", at=at)

    def commit(self, message: str = "work", at: Path | None = None, index: bool = True) -> None:
        """Regenerate the blocks, as a project does before it commits, then commit everything."""
        if index:
            self.run("index")
        self.git("add", "-A", at=at)
        self.git("commit", "-qm", message, at=at)

    def branch(self, name: str = "work") -> None:
        self.git("checkout", "-qb", name)

    def switch(self, name: str) -> None:
        self.git("checkout", "-q", name)


class Solo(Tree):
    """A single repo, the shape most projects are: one decision log and one trap file under
    `docs/`, committed on `main`. Three decisions: `P-1` and `P-3` locked, `P-2` provisional.
    The decision index sits at the end of the log, so it is inside the last entry's body."""

    def __init__(self, tmp: Path, *, git: str | None = "commit", below: str | None = None,
                 extra: str = "", workspace: str = "", log: str | bytes | None = None) -> None:
        self.top = tmp / (below or "solo")
        super().__init__(self.top / "gov" if below else self.top, tmp / "home")
        self.write(layout.CONFIG, SOLO_CONFIG.format(version=__version__, extra=extra,
                                                     workspace=workspace))
        self.write(LOG, log if log is not None else
                   fm() + "# Decisions\n" + decision("P", 1) + decision("P", 2, "provisional")
                   + decision("P", 3) + "\n" + BLOCK)
        self.write(TRAPS, fm("working", status="active") + "# Traps\n\n" + TRAP_BLOCK + trap(1))
        self.run("index")
        if git:
            self.init(at=self.top)
        if git == "commit":
            self.commit("start", at=self.top)


class Team(Tree):
    """A workspace: its own log, and projects `alpha` and `gamma` in the same repository.
    With `nested`, a third project, `beta`, that is its own repository on a branch called
    `trunk`, in a directory the root's `.gitignore` lists."""

    PROJECTS = (("alpha", "projects/alpha", "A", 100), ("gamma", "projects/gamma", "G", 300))
    NESTED = ("beta", "beta", "B", 200)

    def __init__(self, tmp: Path, *, nested: bool = False, extra: str = "") -> None:
        super().__init__(tmp / "team", tmp / "home")
        projects = self.PROJECTS + ((self.NESTED,) if nested else ())
        self.write(layout.CONFIG, TEAM_CONFIG.format(version=__version__, extra=extra))
        self.write("projects.toml", TEAM_REGISTRY + "".join(
            PROJECT.format(name=n, dir=d, prefix=p, lo=lo, hi=lo + 99) for n, d, p, lo in projects))
        self.write("governance/DECISIONS.md", fm() + "# Decisions\n" + decision("W", 1)
                   + decision("W", 2, "provisional"))
        for _, where, prefix, lo in projects:
            self.write(f"{where}/DECISIONS.md", fm() + "# Decisions\n\n" + BLOCK
                       + decision(prefix, lo) + decision(prefix, lo + 1, "provisional"))
            self.write(f"{where}/working-files/traps.md",
                       fm("working", status="active") + "# Traps\n" + trap(1))
        self.write(".gitignore", "beta/\n")
        self.run("index")
        self.init()
        self.commit("start")
        if nested:
            self.init(at=self.root / "beta", branch="trunk")
            self.commit("start", at=self.root / "beta")


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        # Every git call the engine makes during a test, a direct `base.resolve` included,
        # reads this HOME and no system config: never the machine's own settings.
        env = mock.patch.dict(os.environ, {"HOME": str(self.tmp / "home"),
                                           "GIT_CONFIG_NOSYSTEM": "1",
                                           "CONTEXT_GATE_NO_UPDATE_CHECK": "1"})
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def solo(self, **kw) -> Solo:
        return Solo(self.tmp, **kw)

    def team(self, **kw) -> Team:
        return Team(self.tmp, **kw)

    @contextlib.contextmanager
    def fresh(self):
        """A temporary directory of its own for one case of a loop, gone when the case is."""
        outer = self.tmp
        with tempfile.TemporaryDirectory() as tmp:
            self.tmp = Path(tmp)
            try:
                yield
            finally:
                self.tmp = outer

    def silent(self, w: Tree, *flags: str, env: dict | None = None) -> tuple[int, str, str]:
        """The gate exits with, and prints, exactly what it would if this check did not exist:
        the same exit code, the same stdout and the same stderr, byte for byte."""
        with unregistered():
            without = w.run("check", *flags, env=env)
        with_it = w.run("check", *flags, env=env)
        self.assertEqual(with_it, without)
        return with_it

    def stand_in_git(self, body: str) -> dict:
        """The environment for a run whose `git` is a script that runs `body` (shell, with the
        real git in `$REAL`) and, unless `body` exits, then the real git with the same
        arguments."""
        if os.name == "nt":
            self.skipTest("the stand-in git is a shell script, which Windows does not run from PATH")
        shim = self.tmp / "stand-in"
        shim.mkdir(exist_ok=True)
        wtext(shim / "git", f"#!/bin/sh\nREAL='{shutil.which('git')}'\n{body}\nexec \"$REAL\" \"$@\"\n")
        (shim / "git").chmod(0o755)
        return {"PATH": f"{shim}{os.pathsep}{os.environ['PATH']}"}

    def git_calls(self, w: Tree, *argv: str) -> list[str]:
        """The git calls this check adds to a run, in order, each as its arguments after
        `-C <repository>`: what a recording `git` on PATH saw with the check registered, less
        what it saw without it."""
        log = self.tmp / "git-calls.log"
        env = self.stand_in_git(f"(IFS='|'; printf '%s\\n' \"$*\") >> '{log}'")

        def calls() -> list[str]:
            wtext(log, "")
            w.run(*argv, env=env)
            return rtext(log).splitlines()

        with unregistered():
            without = collections.Counter(calls())
        added = []
        for call in calls():
            if without[call] > 0:
                without[call] -= 1
            else:
                flag, _, args = call.split("|", 2)
                self.assertEqual(flag, "-C", call)
                added.append(args.replace("|", " "))
        return added

    def found(self, w: Tree, *flags: str, env: dict | None = None) -> tuple[int, list[str]]:
        """`(exit code, the finding lines this check added)`: every line of the gate's output
        that is not there without the check, status lines aside. The blocks are regenerated
        first, as a project does before it runs the gate, so an edited title is not also a stale
        index and the exit code is this check's doing."""
        w.run("index")
        with unregistered():
            _, without, err_without = w.run("check", *flags, env=env)
        code, out, err = w.run("check", *flags, env=env)
        self.assertEqual(err, err_without)
        extra = collections.Counter(out.splitlines()) - collections.Counter(without.splitlines())
        return code, [line for line in out.splitlines()
                      if line in extra and not line.startswith("[")]


# ---------------------------------------------------------------------------- the module

class BaseModule(Case):
    """`govern.base`: what a file held at a commit, asked of one repository."""

    def test_exists_tells_absent_from_present_and_from_git_not_running(self):
        w = self.solo()
        head = base.resolve(w.root, "HEAD")
        self.assertIs(base.exists(w.root, head, LOG), True)
        self.assertIs(base.exists(w.root, head, "docs/NOPE.md"), False)
        empty = self.tmp / "nothing-on-path"
        empty.mkdir()
        with mock.patch.dict(os.environ, {"PATH": str(empty)}):
            self.assertIsNone(base.exists(w.root, head, LOG))
        # A commit git cannot read holds nothing it can vouch for: not "the file is not there".
        self.assertIsNone(base.exists(w.root, "0" * 40, LOG))

    def test_files_lists_one_directory_at_a_commit_and_never_reads_a_failure_as_empty(self):
        w = self.solo()
        w.write("top.md", "# Top\n")
        w.write("docs/working/café.md", "# More\n")
        w.commit("more")
        head = base.resolve(w.root, "HEAD")
        self.assertEqual(base.files(w.root, head, "docs"), [LOG])          # not what is below it
        self.assertEqual(base.files(w.root, head, ""), ["top.md"])
        self.assertEqual(sorted(base.files(w.root, head, "docs/working")),
                         ["docs/working/café.md", TRAPS])
        self.assertEqual(base.files(w.root, head, "docs/nope"), [])
        with self.assertRaises(base.CannotCompare) as caught:
            base.files(w.root, "0" * 40, "docs")
        self.assertEqual(caught.exception.reason, "git could not list docs at 000000000000")

    def test_renames_says_what_was_renamed_between_a_commit_and_the_working_tree(self):
        w = self.solo()
        head = base.resolve(w.root, "HEAD")
        self.assertEqual(base.renames(w.root, head), {})
        w.git("mv", LOG, "docs/LOG é.md")
        w.append("docs/LOG é.md", decision("P", 4))          # renamed and edited
        w.append(TRAPS, trap(2))                             # edited only
        self.assertEqual(base.renames(w.root, head), {"docs/LOG é.md": LOG})
        with self.assertRaises(base.CannotCompare) as caught:
            base.renames(w.root, "0" * 40)
        self.assertEqual(caught.exception.reason,
                         "git could not say what was renamed since 000000000000")

    def test_since_also_says_what_was_deleted_in_the_same_one_question(self):
        w = self.solo()
        head = base.resolve(w.root, "HEAD")
        self.assertEqual(base.since(w.root, head), ({}, []))
        w.git("mv", LOG, "docs/LOG.md")                      # renamed: not deleted
        w.git("rm", "-q", TRAPS)                             # deleted, and git told
        w.write("docs/working/new é.md", "# New\n")          # not tracked: not git's to list
        self.assertEqual(base.since(w.root, head), ({"docs/LOG.md": LOG}, [TRAPS]))
        w.git("add", "-A")
        os.rename(w.root / "docs/LOG.md", w.root / "docs/elsewhere.md")
        # Moved without telling git: the tracked name is gone from the tree, which is deleted.
        self.assertEqual(base.since(w.root, head), ({}, [LOG, TRAPS]))
        with self.assertRaises(base.CannotCompare) as caught:
            base.since(w.root, "0" * 40)
        self.assertEqual(caught.exception.reason,
                         "git could not say what was renamed since 000000000000")

    def test_read_gives_the_committed_text_not_the_tree(self):
        w = self.solo()
        committed = w.read(LOG)
        w.append(LOG, decision("P", 4))
        self.assertEqual(base.read(w.root, base.resolve(w.root, "HEAD"), LOG), committed)

    def test_read_drops_a_bom_and_keeps_crlf(self):
        text = fm() + "# Decisions\n" + decision("P", 1)
        w = self.solo(git="init")
        w.write(LOG, ("\ufeff" + text.replace("\n", "\r\n")).encode("utf-8"))
        w.commit("start", index=False)
        got = base.read(w.root, base.resolve(w.root, "HEAD"), LOG)
        self.assertEqual(got, text.replace("\n", "\r\n"))

    def test_read_refuses_bytes_that_are_not_utf8_and_a_file_that_is_not_there(self):
        w = self.solo(git="init")
        w.write(LOG, b"# Decisions\n\xff\xfe\n")
        w.commit("start", index=False)
        head = base.resolve(w.root, "HEAD")
        with self.assertRaises(base.CannotCompare) as caught:
            base.read(w.root, head, LOG)
        self.assertEqual(caught.exception.reason,
                         f"{LOG} at {head[:12]} is not valid UTF-8 (byte 12)")
        with self.assertRaises(base.CannotCompare) as caught:
            base.read(w.root, head, "docs/NOPE.md")
        self.assertEqual(caught.exception.reason,
                         f"git could not read docs/NOPE.md at {head[:12]}")

    def test_resolve_and_merge_base(self):
        w = self.solo()
        start = w.git("rev-parse", "HEAD").strip()
        w.branch("work")
        w.append(LOG, decision("P", 4))
        w.commit("ours")
        w.switch("main")
        w.append(LOG, decision("P", 5))
        w.commit("theirs")
        w.switch("work")
        self.assertEqual(base.resolve(w.root, "main"), w.git("rev-parse", "main").strip())
        self.assertEqual(base.merge_base(w.root, "main"), start)
        self.assertIsNone(base.resolve(w.root, "origin/nope"))
        self.assertIsNone(base.merge_base(w.root, "origin/nope"))
        self.assertIsNone(base.resolve(w.root, LOG))        # a path is not a commit

    def test_a_ref_that_reads_as_an_option_is_never_handed_to_git(self):
        w = self.solo()
        for ref in ("", "-h", "--all", "--output=x"):
            with mock.patch("govern.base.git") as called:
                self.assertIsNone(base.resolve(w.root, ref))
                self.assertIsNone(base.merge_base(w.root, ref))
                called.assert_not_called()

    def test_ls_lists_a_directory_at_a_commit_by_its_real_names(self):
        w = self.solo(git="init")
        w.write("docs/working/traps-café.md", "# More\n")
        w.commit("start")
        head = base.resolve(w.root, "HEAD")
        self.assertEqual(sorted(base.ls(w.root, head, "docs/working")),
                         ["docs/working/traps-café.md", "docs/working/traps.md"])
        self.assertEqual(base.ls(w.root, head, "docs/nope"), [])
        self.assertIsNone(base.ls(w.root, "0" * 40, "docs/working"))

    def test_a_hooks_git_dir_does_not_redirect_the_question(self):
        # A pre-commit hook exports GIT_DIR; the answer must still be about the repository
        # asked, not the hook's.
        w = self.solo()
        other = self.tmp / "other"
        other.mkdir()
        w.init(at=other)
        with mock.patch.dict(os.environ, {"GIT_DIR": str(other / ".git")}):
            self.assertEqual(base.resolve(w.root, "HEAD"), w.git("rev-parse", "HEAD").strip())
            self.assertIs(base.exists(w.root, base.resolve(w.root, "HEAD"), LOG), True)

    def test_compare_returns_both_sides_per_repository(self):
        w = self.team(nested=True)
        w.append("governance/DECISIONS.md", decision("W", 3))
        w.append("beta/working-files/traps.md", trap(2))
        cmp = base.compare(w.context())
        self.assertEqual([(r.label, r.root, r.commit is not None) for r in cmp.repos],
                         [(".", True, True), ("beta", False, True)])
        root, beta = cmp.repos
        self.assertEqual(root.commit, w.git("rev-parse", "HEAD").strip())
        self.assertEqual(beta.commit, w.git("rev-parse", "HEAD", at=w.root / "beta").strip())
        self.assertEqual(root.scopes, {"workspace", "alpha", "gamma"})
        ids = lambda items, kind: [(i.log, i.entry.ident) for i in items if i.kind == kind]
        self.assertEqual(ids(root.base, base.DECISION), [
            ("governance/DECISIONS.md", "W-1"), ("governance/DECISIONS.md", "W-2"),
            ("projects/alpha/DECISIONS.md", "A-100"), ("projects/alpha/DECISIONS.md", "A-101"),
            ("projects/gamma/DECISIONS.md", "G-300"), ("projects/gamma/DECISIONS.md", "G-301")])
        self.assertEqual(ids(root.current, base.DECISION)[:3], [
            ("governance/DECISIONS.md", "W-1"), ("governance/DECISIONS.md", "W-2"),
            ("governance/DECISIONS.md", "W-3")])
        self.assertEqual(ids(beta.base, base.TRAP), [("beta/working-files/traps.md", "T-1")])
        self.assertEqual(ids(beta.current, base.TRAP), [("beta/working-files/traps.md", "T-1"),
                                                        ("beta/working-files/traps.md", "T-2")])
        self.assertEqual((cmp.ref, cmp.complete, cmp.not_compared, root.theirs), (None, True, [], []))

    def test_compare_names_what_could_not_be_compared_and_why(self):
        w = self.team(nested=True)
        cmp = base.compare(w.context("main"))
        self.assertEqual([(r.label, r.commit, r.reason) for r in cmp.not_compared],
                         [("beta", None, "main names no commit in this repository")])
        root = cmp.repos[0]
        self.assertEqual((root.commit, root.tip), (w.git("rev-parse", "main").strip(),) * 2)
        self.assertEqual(len(root.theirs), len(root.base))

    def test_two_projects_that_share_one_log_are_compared_once(self):
        w = self.team()
        w.append("projects.toml", PROJECT.format(name="delta", dir="projects/alpha", prefix="A",
                                                 lo=100, hi=199))
        cmp = base.compare(w.context(), traps=False)
        for side in ("base", "current"):
            self.assertEqual([i.entry.ident for i in cmp.side(side)
                              if i.log == "projects/alpha/DECISIONS.md"], ["A-100", "A-101"])

    def test_without_traps_no_trap_file_is_read(self):
        w = self.team()
        cmp = base.compare(w.context(), traps=False)
        self.assertEqual(cmp.side("base", base.TRAP) + cmp.side("current", base.TRAP), [])
        self.assertEqual(len(cmp.side("base", base.DECISION)), 6)


# ---------------------------------------------------------------------------- rule 1

class SettledDecisionChangedInPlace(Case):
    """A locked decision's text differs from the base, and no dated `**Revised:**` line says
    so."""

    EDIT = ("before acting on it.\n\n**Why:** the next reader was not in the room.\n\n## P-2",
            "before acting on it, always.\n\n**Why:** the next reader was not in the room.\n\n## P-2")

    def _edited(self, revised: str = "", **kw) -> Solo:
        """`P-1`, locked, reworded in the working tree, with `revised` put under its status."""
        w = self.solo(**kw)
        w.edit(LOG, *self.EDIT)
        if revised:
            w.edit(LOG, "Title 1\n\n**Status:** locked\n", f"Title 1\n\n**Status:** locked\n{revised}")
        return w

    def test_an_uncommitted_edit_is_a_warning_and_the_gate_still_exits_zero(self):
        w = self._edited()
        self.assertEqual(self.found(w), (0, [WARN + changed(LOG, "P-1")]))

    def test_under_base_the_same_edit_is_an_error(self):
        w = self._edited()
        self.assertEqual(self.found(w, "--base", "main"), (1, [ERROR + changed(LOG, "P-1")]))

    def test_committing_the_edit_ends_the_advice_but_not_the_gate(self):
        w = self.solo()
        w.branch("work")
        w.edit(LOG, *self.EDIT)
        w.commit("reword")
        self.silent(w)
        self.assertEqual(self.found(w, "--base", "main"), (1, [ERROR + changed(LOG, "P-1")]))

    def test_a_revised_line_added_makes_the_edit_legal(self):
        w = self._edited("\n**Revised:** 2026-10-06 (always, not sometimes)\n")
        self.silent(w)
        self.silent(w, "--base", "main")

    def test_a_revised_line_reworded_on_the_same_date_makes_a_second_edit_legal(self):
        w = self.solo()
        w.edit(LOG, "Title 1\n\n**Status:** locked\n",
               "Title 1\n\n**Status:** locked\n\n**Revised:** 2026-10-06 (first change)\n")
        w.commit("revised once")
        w.edit(LOG, *self.EDIT)
        self.assertEqual(self.found(w), (0, [WARN + changed(LOG, "P-1")]))
        w.edit(LOG, "(first change)", "(first change; and always)")
        self.silent(w)
        self.silent(w, "--base", "main")

    def test_a_revised_line_older_than_the_last_one_does_not(self):
        w = self.solo()
        w.edit(LOG, "Title 1\n\n**Status:** locked\n",
               "Title 1\n\n**Status:** locked\n\n**Revised:** 2026-05-01 (first change)\n")
        w.commit("revised once")
        w.edit(LOG, *self.EDIT)
        w.edit(LOG, "**Revised:** 2026-05-01 (first change)", "**Revised:** 2026-01-01 (backdated)")
        self.assertEqual(self.found(w), (0, [WARN + changed(LOG, "P-1")]))
        self.assertEqual(self.found(w, "--base", "main"), (1, [ERROR + changed(LOG, "P-1")]))

    def test_a_revised_line_whose_date_is_no_date_does_not(self):
        for line in ("**Revised:** 2026-02-30 (no such day)", "**Revised:** last week",
                     "**Revised:** (what changed)", "Revised: 2026-10-06 (not the field)"):
            with self.subTest(line=line), self.fresh():
                w = self._edited(f"\n{line}\n")
                self.assertEqual(self.found(w, "--base", "main"),
                                 (1, [ERROR + changed(LOG, "P-1")]))

    def test_removing_a_revised_line_is_not_a_revision(self):
        w = self.solo()
        w.edit(LOG, "Title 1\n\n**Status:** locked\n",
               "Title 1\n\n**Status:** locked\n\n**Revised:** 2026-05-01 (first change)\n")
        w.commit("revised once")
        w.edit(LOG, "\n**Revised:** 2026-05-01 (first change)\n", "")
        self.assertEqual(self.found(w, "--base", "main"), (1, [ERROR + changed(LOG, "P-1")]))

    def test_a_revised_line_inside_a_code_fence_is_an_example_not_the_field(self):
        w = self._edited("\n```\n**Revised:** 2026-10-06 (how the line looks)\n```\n")
        self.assertEqual(self.found(w, "--base", "main"), (1, [ERROR + changed(LOG, "P-1")]))

    def test_a_status_change_is_text_like_any_other(self):
        w = self.solo()
        w.edit(LOG, "Title 1\n\n**Status:** locked", "Title 1\n\n**Status:** provisional")
        self.assertEqual(self.found(w), (0, [WARN + changed(LOG, "P-1")]))
        self.assertEqual(self.found(w, "--base", "main"), (1, [ERROR + changed(LOG, "P-1")]))

    def test_a_typo_fix_and_a_title_change_need_the_line_too(self):
        for old, new in (("## P-1 - Title 1", "## P-1 - Title one"),
                         ("acting on it.\n\n**Why:** the next reader was not in the room.\n\n## P-2",
                          "acting on it.\n\n**Why:** the next reader wasn't in the room.\n\n## P-2")):
            with self.subTest(new=new), self.fresh():
                w = self.solo()
                w.edit(LOG, old, new)
                self.assertEqual(self.found(w, "--base", "main"),
                                 (1, [ERROR + changed(LOG, "P-1")]))

    def test_a_topic_line_carrying_another_field_is_text(self):
        # Only the line that is the topic and nothing else is left out of what is compared.
        w = self.solo()
        w.edit(LOG, "Title 1\n\n**Status:** locked\n",
               "Title 1\n\n**Topic:** X. **Rule:** do the opposite\n\n**Status:** locked\n")
        self.assertEqual(self.found(w), (0, [WARN + changed(LOG, "P-1")]))
        self.assertEqual(self.found(w, "--base", "main"), (1, [ERROR + changed(LOG, "P-1")]))

    def test_becoming_a_pointer_is_the_legal_way_to_replace_it(self):
        w = self.solo()
        head, _, rest = w.read(LOG).partition("\n## P-1 - Title 1\n")
        _, _, rest = rest.partition("\n## P-2")
        w.write(LOG, head + "\n## P-1 - Replaced by P-4\n\n## P-2" + rest)
        w.append(LOG, decision("P", 4))
        self.silent(w)
        self.silent(w, "--base", "main")

    def test_withdrawing_a_settled_decision_takes_two_steps(self):
        # One: change its status, with a Revised line saying so. Two, once that has landed:
        # remove it. It is no longer settled by then, so the removal is a warning.
        w = self.solo()
        w.branch("step-one")
        w.edit(LOG, "Title 1\n\n**Status:** locked\n",
               "Title 1\n\n**Status:** provisional\n\n**Revised:** 2026-10-06 (added by mistake)\n")
        self.silent(w)
        self.silent(w, "--base", "main")
        w.commit("no longer settled")
        w.switch("main")
        w.git("merge", "-q", "--ff-only", "step-one")
        w.branch("step-two")
        head, _, rest = w.read(LOG).partition("\n## P-1 - Title 1\n")
        w.write(LOG, head + "\n## P-2" + rest.partition("\n## P-2")[2])
        gone = WARN + removed(LOG, "P-1", "provisional", "## P-1 - Replaced by <id>")
        self.assertEqual(self.found(w), (0, [gone]))
        self.assertEqual(self.found(w, "--base", "main"), (0, [gone]))

    def test_both_steps_on_one_branch_are_still_a_removal_of_a_settled_decision(self):
        w = self.solo()
        w.branch("work")
        head, _, rest = w.read(LOG).partition("\n## P-1 - Title 1\n")
        w.write(LOG, head + "\n## P-2" + rest.partition("\n## P-2")[2])
        w.commit("gone")
        self.assertEqual(self.found(w, "--base", "main"),
                         (1, [ERROR + removed(LOG, "P-1", "locked", "## P-1 - Replaced by <id>")]))

    def test_an_explicit_level_error_does_not_make_the_plain_run_fail(self):
        # How strict the check is depends on how the gate is run, never on the setting; a
        # project that wants the errors on uncommitted work asks for the gate against HEAD.
        w = self._edited(extra=f'\n[checks.{CHECK}]\nlevel = "error"\n')
        code, out, _ = w.run("check")
        self.assertEqual((code, ERROR in out), (0, False), out)
        self.assertIn(WARN + changed(LOG, "P-1"), out)
        code, out, _ = w.run("check", "--base", "HEAD")
        self.assertEqual(code, 1, out)
        self.assertIn(ERROR + changed(LOG, "P-1"), out)

    def test_level_warn_demotes_the_gate_and_off_silences_it(self):
        reason = 'reason = "the team reviews every decision change by hand"\n'
        w = self._edited(extra=f'\n[checks.{CHECK}]\nlevel = "warn"\n{reason}')
        code, out, _ = w.run("check", "--base", "main")
        self.assertEqual(code, 0, out)
        self.assertIn(WARN + changed(LOG, "P-1"), out)
        w.edit(layout.CONFIG, 'level = "warn"', 'level = "off"')
        code, out, _ = w.run("check", "--base", "main")
        self.assertEqual((code, CHECK in out, "P-1 (locked) changed" in out), (0, False, False), out)


# ---------------------------------------------------------------------------- rule 3

class DecisionRemoved(Case):
    def _without(self, w: Tree, heading: str, rel: str = LOG) -> None:
        """Cut one whole entry out of a log."""
        head, found, rest = w.read(rel).partition(f"\n{heading}\n")
        assert found, heading
        nxt = re.search(r"^## ", rest, re.M)
        w.write(rel, head + ("\n" + rest[nxt.start():] if nxt else "\n"))

    def test_a_locked_decision_removed_warns_first_and_fails_the_gate(self):
        w = self.solo()
        self._without(w, "## P-1 - Title 1")
        line = removed(LOG, "P-1", "locked", "## P-1 - Replaced by <id>")
        self.assertEqual(self.found(w), (0, [WARN + line]))
        self.assertEqual(self.found(w, "--base", "main"), (1, [ERROR + line]))

    def test_a_provisional_decision_removed_is_a_warning_in_both_modes(self):
        w = self.solo()
        self._without(w, "## P-2 - Title 2")
        line = WARN + removed(LOG, "P-2", "provisional", "## P-2 - Replaced by <id>")
        self.assertEqual(self.found(w), (0, [line]))
        self.assertEqual(self.found(w, "--base", "main"), (0, [line]))

    def test_an_entry_with_no_status_says_so(self):
        w = self.solo(log=fm() + "# Decisions\n" + decision("P", 1) + decision("P", 2, None)
                      + "\n" + BLOCK)
        self._without(w, "## P-2 - Title 2")
        line = WARN + removed(LOG, "P-2", "no status", "## P-2 - Replaced by <id>")
        self.assertEqual(self.found(w, "--base", "main")[1], [line])

    def test_the_pointer_it_suggests_has_the_dash_of_the_heading_that_went(self):
        w = self.solo(log=fm() + "# Decisions\n" + decision("P", 1, sep=" — ")
                      + decision("P", 2, sep=" – ") + decision("P", 3) + "\n" + BLOCK)
        self._without(w, "## P-1 — Title 1")
        self._without(w, "## P-2 – Title 2")
        self.assertEqual(self.found(w, "--base", "main"), (1, [
            ERROR + removed(LOG, "P-1", "locked", "## P-1 — Replaced by <id>"),
            ERROR + removed(LOG, "P-2", "locked", "## P-2 – Replaced by <id>")]))

    def test_a_prefix_written_in_another_case_is_the_same_id(self):
        w = self.solo()
        w.edit(LOG, "## P-1 - Title 1", "## p-1 - Title 1")
        w.edit(TRAPS, "## T-1 - Trap 1", "## t-1 - Trap 1")
        # The grammar itself wants capitals: to every other check this heading is malformed.
        self.assertEqual([e.ident for e in w.context().grammar.parse(w.read(LOG)).entries],
                         ["P-2", "P-3"])
        self.silent(w)
        self.silent(w, "--base", "main")
        # The same id, so a change to it is still a change to a settled decision.
        w.edit(LOG, "## p-1 - Title 1", "## p-1 - Another title")
        self.assertEqual(self.found(w, "--base", "main")[1], [ERROR + changed(LOG, "p-1")])

    def test_deleting_the_whole_log_removes_every_decision_in_it(self):
        w = self.solo()
        (w.root / LOG).unlink()
        code, lines = self.found(w, "--base", "main")
        self.assertEqual(lines, [
            ERROR + removed(LOG, "P-1", "locked", "## P-1 - Replaced by <id>"),
            ERROR + removed(LOG, "P-3", "locked", "## P-3 - Replaced by <id>"),
            WARN + removed(LOG, "P-2", "provisional", "## P-2 - Replaced by <id>")])

    def test_an_id_out_of_sight_in_an_unreadable_log_is_not_called_removed(self):
        # B-200 moved from beta's log, in beta's own repository, to the workspace's log, which
        # then cannot be read: nothing can say it is gone, and the unreadable file is the other
        # checks' to report.
        w = self.team(nested=True)
        self._without(w, "## B-200 - Title 200", "beta/DECISIONS.md")
        w.write("governance/DECISIONS.md", b"# Decisions\n\xff\n")
        self.silent(w)
        # Readable again, and B-200 is in neither log: now it is known to be gone.
        w.git("checkout", "-q", "--", "governance/DECISIONS.md")
        self.assertEqual(self.found(w), (0, [WARN + removed(
            "beta/DECISIONS.md", "B-200", "locked", "## B-200 - Replaced by <id>")]))


# ---------------------------------------------------------------------------- rule 4

class IdTakenOnTheBaseBranch(Case):
    """`work` and `main` each add the same id after they part."""

    def _fork(self, w: Tree, ours: str, theirs: str, rel: str = LOG) -> None:
        w.branch("work")
        w.append(rel, ours)
        w.commit("ours")
        w.switch("main")
        w.append(rel, theirs)
        w.commit("theirs")
        w.switch("work")

    def test_the_same_id_with_different_text_fails_on_the_branch(self):
        w = self.solo()
        self._fork(w, decision("P", 4, title="Ours"), decision("P", 4, title="Their title"))
        self.assertEqual(self.found(w, "--base", "main"),
                         (1, [ERROR + taken(LOG, "P-4", "main", "Their title")]))

    def test_the_other_sides_title_cannot_steer_the_terminal(self):
        # The title quoted is the other branch's text: an escape sequence, a bell or a delete
        # in it is printed as `\xNN`, in a decision's finding and in a trap's.
        title = "Red \x1b[31malert\x7f, a\ttab and a bell\x07 too"
        shown = "Red \\x1b[31malert\\x7f, a\\x09tab and a bell\\x07 too"
        w = self.solo()
        w.branch("work")
        w.append(LOG, decision("P", 4, title="Ours"))
        w.append(TRAPS, trap(2, title="Ours"))
        w.commit("ours")
        w.switch("main")
        w.append(LOG, decision("P", 4, title=title))
        w.append(TRAPS, trap(2, title=title))
        w.commit("theirs")
        w.switch("work")
        code, lines = self.found(w, "--base", "main")
        self.assertEqual((code, lines), (1, [ERROR + taken(LOG, "P-4", "main", shown),
                                             ERROR + taken(TRAPS, "T-2", "main", shown)]))
        self.assertEqual([c for line in lines for c in line if ord(c) < 0x20 or ord(c) == 0x7f], [])

    def test_without_base_nothing_is_said_about_it(self):
        w = self.solo()
        self._fork(w, decision("P", 4, title="Ours"), decision("P", 4, title="Their title"))
        self.silent(w)

    def test_the_same_id_with_the_same_text_is_a_cherry_pick(self):
        w = self.solo()
        # The same entry on both sides, wrapped differently and under another dash.
        self._fork(w, decision("P", 4, "provisional"),
                   decision("P", 4, "provisional", sep=" — ").replace("down before", "down\nbefore"))
        self.silent(w, "--base", "main")

    def test_after_merging_the_base_into_the_branch_it_is_silent(self):
        w = self.solo()
        self._fork(w, decision("P", 4, title="Ours"), decision("P", 4, title="Their title"))
        self.assertEqual(self.found(w, "--base", "main")[0], 1)
        w.git("merge", "-q", "main", check=False)        # conflicts: both wrote P-4
        w.write(LOG, w.git("show", f"main:{LOG}") + decision("P", 5, title="Ours"))
        w.commit("merge main, ours renumbered")
        self.assertEqual(w.git("merge-base", "main", "HEAD").strip(),
                         w.git("rev-parse", "main").strip())
        self.silent(w, "--base", "main")

    def test_an_entry_the_base_took_by_a_squash_merge_and_this_branch_then_retitled(self):
        # Nothing in git says the entry on the base branch is this branch's own, so the message
        # gives both ways out. Merging the base in is the one for this case: the retitle is
        # then an ordinary edit to an entry that was at the base.
        w = self.solo()
        w.branch("work")
        w.append(LOG, decision("P", 4, "provisional", title="As first written"))
        w.commit("ours")
        w.switch("main")
        w.git("merge", "-q", "--squash", "work")
        w.git("commit", "-qm", "work, squashed")
        w.switch("work")
        self.silent(w, "--base", "main")                   # the same text on both sides
        w.edit(LOG, "## P-4 - As first written", "## P-4 - As retitled")
        w.commit("retitled")
        self.assertEqual(self.found(w, "--base", "main"),
                         (1, [ERROR + taken(LOG, "P-4", "main", "As first written")]))
        ours = w.read(LOG)
        w.git("merge", "-q", "--no-commit", "main", check=False)
        w.write(LOG, ours)
        w.commit("merge main")
        self.assertEqual(w.git("merge-base", "main", "HEAD").strip(),
                         w.git("rev-parse", "main").strip())
        self.assertIn("## P-4 - As retitled", w.read(LOG))
        self.silent(w, "--base", "main")

    def test_the_rival_in_the_same_file_is_the_one_an_entry_is_held_against(self):
        # main has T-2 twice: in the file this branch wrote its own T-2 in, saying something
        # else, and in another file with this branch's very words. The one in the same file is
        # the collision, and its title is the one quoted.
        w = self.solo()
        w.branch("work")
        w.append(TRAPS, trap(2, "Ours"))
        w.commit("ours")
        w.switch("main")
        w.append(TRAPS, trap(2, "Their trap"))
        w.write("docs/working/traps-build.md", fm("working", status="active") + "# Build traps\n"
                + trap(2, "Ours"))
        w.commit("theirs")
        w.switch("work")
        self.assertEqual(self.found(w, "--base", "main")[1],
                         [ERROR + taken(TRAPS, "T-2", "main", "Their trap")])

    def test_an_id_in_a_repository_that_was_not_compared_is_not_called_this_branchs(self):
        # beta is its own repository and has no `main`: nothing says what its branch added, so
        # an id in its log that the root's `main` also took is not held against it.
        w = self.team(nested=True)
        w.branch("work")
        w.append("beta/DECISIONS.md", decision("W", 9, title="Beta's"))
        w.switch("main")
        w.append("governance/DECISIONS.md", decision("W", 9, title="The workspace's"))
        w.commit("theirs")
        w.switch("work")
        self.assertEqual(self.found(w, "--base", "main")[1], [WARN + no_compare(
            "main", "main names no commit in this repository", repo="beta")])

    def test_what_landed_on_the_base_after_the_branch_began_is_not_the_branchs(self):
        # main rewords a locked decision and removes another after `work` left it: compared
        # with main's tip that would read as this branch undoing both.
        w = self.solo()
        w.branch("work")
        w.append(LOG, decision("P", 4))
        w.commit("ours")
        w.switch("main")
        w.edit(LOG, *SettledDecisionChangedInPlace.EDIT)
        w.edit(LOG, "## P-3 - Title 3\n\n**Status:** locked", "## P-3 - Replaced by P-1")
        w.commit("theirs")
        w.switch("work")
        self.silent(w, "--base", "main")

    def test_a_trap_id_taken_on_the_base_branch_fails_too(self):
        w = self.solo()
        self._fork(w, trap(2, "Ours"), trap(2, "Their trap"), rel=TRAPS)
        self.assertEqual(self.found(w, "--base", "main"),
                         (1, [ERROR + taken(TRAPS, "T-2", "main", "Their trap")]))
        self.silent(w)

    def test_the_same_trap_on_both_sides_is_silent(self):
        w = self.solo()
        self._fork(w, trap(2), trap(2), rel=TRAPS)
        self.silent(w, "--base", "main")

    def test_a_trap_added_in_a_second_trap_file_still_collides(self):
        # Trap ids are one namespace across a project's trap files.
        w = self.solo()
        w.branch("work")
        w.write("docs/working/traps-build.md", fm("working", status="active") + "# Build traps\n"
                + trap(2, "Ours"))
        w.git("add", "-A")
        w.git("commit", "-qm", "ours")
        w.switch("main")
        w.append(TRAPS, trap(2, "Their trap"))
        w.commit("theirs")
        w.switch("work")
        self.assertEqual(self.found(w, "--base", "main")[1],
                         [ERROR + taken("docs/working/traps-build.md", "T-2", "main", "Their trap")])

    def test_the_same_trap_id_in_two_projects_is_two_traps(self):
        w = self.team()
        w.branch("work")
        w.append("projects/alpha/working-files/traps.md", trap(2, "Alpha's"))
        w.commit("ours")
        w.switch("main")
        w.append("projects/gamma/working-files/traps.md", trap(2, "Gamma's"))
        w.commit("theirs")
        w.switch("work")
        self.silent(w, "--base", "main")


# ---------------------------------------------------------------------------- cannot compare

class CannotCompare(Case):
    """Each reason a comparison cannot be made: silent on an ordinary run, where a project with
    no history is not nagged; under `--base` the gate fails closed."""

    def _edited(self, **kw) -> Solo:
        w = self.solo(**kw)
        w.edit(LOG, *SettledDecisionChangedInPlace.EDIT)
        return w

    def test_no_git_on_path(self):
        w = self._edited()
        empty = self.tmp / "nothing-on-path"
        empty.mkdir()
        env = {"PATH": str(empty)}
        self.silent(w, env=env)
        self.assertEqual(self.found(w, "--base", "main", env=env),
                         (1, [ERROR + no_compare("main", "git is not available")]))

    def test_not_a_repository(self):
        w = self._edited(git=None)
        self.silent(w)
        self.assertEqual(self.found(w, "--base", "main"),
                         (1, [ERROR + no_compare("main", "not a git repository")]))

    def test_no_commit_yet(self):
        w = self._edited(git="init")
        self.silent(w)
        self.assertEqual(self.found(w, "--base", "main"),
                         (1, [ERROR + no_compare("main", "the repository has no commit yet")]))

    def test_an_unknown_ref(self):
        w = self._edited()
        self.assertEqual(self.found(w, "--base", "origin/nope"), (1, [ERROR + no_compare(
            "origin/nope", "origin/nope names no commit in this repository")]))

    def test_a_ref_that_reads_as_an_option_is_an_unknown_ref(self):
        w = self._edited()
        self.assertEqual(self.found(w, "--base=--all"), (1, [ERROR + no_compare(
            "--all", "--all names no commit in this repository")]))

    def test_an_empty_base_is_a_usage_error(self):
        w = self._edited()
        code, out, err = w.run("check", "--base", "")
        self.assertEqual((code, out, err), (2, "", "error: --base needs a ref, e.g. --base origin/main\n"))

    def test_unrelated_histories(self):
        w = self._edited()
        w.git("stash", "-q")
        w.git("checkout", "-q", "--orphan", "island")
        w.git("commit", "-qm", "an island")
        w.switch("main")
        w.git("stash", "pop", "-q")
        self.assertEqual(self.found(w, "--base", "island"),
                         (1, [ERROR + no_compare("island", "HEAD and island share no history")]))

    def test_a_shallow_clone(self):
        src = self.solo()
        src.branch("work")
        src.append(LOG, decision("P", 4))
        src.commit("ours")
        src.switch("main")
        src.append(LOG, decision("P", 5))
        src.commit("theirs")
        clone = self.tmp / "clone"
        src.git("clone", "-q", "--depth", "1", "--branch", "work", src.root.as_uri(), str(clone),
                at=self.tmp)
        w = Tree(clone, src.home)
        self.assertEqual(w.git("rev-parse", "--is-shallow-repository").strip(), "true")
        w.edit(LOG, *SettledDecisionChangedInPlace.EDIT)
        # A depth-1 clone holds one branch: the ref a pull request names is not there at all.
        fix = "fetch the full history (fetch-depth: 0)"
        self.assertEqual(self.found(w, "--base", "origin/main"), (1, [ERROR + no_compare(
            "origin/main", f"origin/main names no commit in this shallow clone: {fix}")]))
        # Fetched just as shallowly, it is there, with nothing in common to compare from.
        w.git("fetch", "-q", "--depth", "1", "origin", "main:refs/remotes/origin/main")
        self.assertEqual(self.found(w, "--base", "origin/main"), (1, [ERROR + no_compare(
            "origin/main", f"HEAD and origin/main share no commit in this shallow clone: {fix}")]))
        # An ordinary run compares with HEAD, which a shallow clone does have.
        self.assertEqual(self.found(w), (0, [WARN + changed(LOG, "P-1")]))

    def test_a_base_log_that_is_not_utf8(self):
        good = fm() + "# Decisions\n" + decision("P", 1) + "\n" + BLOCK
        w = self.solo(git="init", log=good.replace("room", "r\xf6\xf6m").encode("latin-1"))
        w.commit("start", index=False)
        head = w.git("rev-parse", "HEAD").strip()
        w.write(LOG, good.replace("acting on it", "acting on it, always"))
        w.run("index")
        self.silent(w)
        code, lines = self.found(w, "--base", "main")
        self.assertEqual(code, 1)
        self.assertEqual(len(lines), 1, lines)
        self.assertRegex(lines[0], re.escape(ERROR + no_compare(
            "main", f"{LOG} at {head[:12]} is not valid UTF-8 (byte ")) + r"\d+\)$")

    def test_a_log_on_disk_that_is_not_utf8(self):
        w = self.solo()
        w.write(LOG, b"# Decisions\n\xff\n")
        self.silent(w)
        code, lines = self.found(w, "--base", "main")
        self.assertEqual(lines, [ERROR + no_compare("main", f"{LOG} is not valid UTF-8 (byte 12)")])

    def test_a_log_that_is_new_since_the_base_is_not_a_failure(self):
        w = self.solo(git="init")
        w.git("add", "-A")
        w.git("rm", "-q", "--cached", LOG)
        w.git("commit", "-qm", "before there was a log")
        self.silent(w)
        code, out, _ = self.silent(w, "--base", "main")
        self.assertNotIn("could not compare", out)
        w.git("add", "-A")
        w.git("commit", "-qm", "the log")
        self.silent(w, "--base", "main")

    def test_a_nested_project_repository_where_the_ref_means_nothing_is_a_warning(self):
        w = self.team(nested=True)
        note = WARN + no_compare("main", "main names no commit in this repository", repo="beta")
        self.assertEqual(self.found(w, "--base", "main"), (0, [note]))
        # The rest is still compared: the root's own logs against the root's `main`.
        w.edit("projects/alpha/DECISIONS.md", "## A-100 - Title 100", "## A-100 - Another title")
        self.assertEqual(self.found(w, "--base", "main"),
                         (1, [ERROR + changed("projects/alpha/DECISIONS.md", "A-100"), note]))
        # And a ref the nested repository does have is compared there.
        w.edit("beta/DECISIONS.md", "## B-200 - Title 200", "## B-200 - Another title")
        self.assertEqual(self.found(w, "--base", "trunk"), (1, [
            ERROR + no_compare("trunk", "trunk names no commit in this repository"),
            ERROR + changed("beta/DECISIONS.md", "B-200")]))

    def test_a_nested_project_with_no_repository_of_its_own_ref_is_only_its_own_business(self):
        # `check --project alpha`: beta's repository is not asked about at all.
        w = self.team(nested=True)
        self.silent(w, "--project", "alpha", "--base", "main")

    def test_a_project_asked_for_alone_whose_repository_has_no_such_ref_fails_the_gate(self):
        # In a full run it is a warning and the rest is compared (above). Asked about beta and
        # nothing else, there is no rest: the run compared nothing.
        w = self.team(nested=True)
        self.silent(w, "--project", "beta")
        self.assertEqual(self.found(w, "--project", "beta", "--base", "main"), (1, [
            ERROR + no_compare("main", "main names no commit in this repository", repo="beta")]))
        self.silent(w, "--project", "beta", "--base", "trunk")

    def test_workspace_only_is_not_told_about_a_project_repository_it_does_not_read(self):
        w = self.team(nested=True)
        self.silent(w, "--workspace-only", "--base", "main")


# ---------------------------------------------------------------------------- reading the base

class ReadingTheBase(Case):
    """A log that is not in the base commit is a new log, and nothing is said. A log git could
    not read there, or holds there under another name, is not a new log: each of these once
    read as one, and the gate stayed green."""

    LOWER, RENAMED = "docs/decisions.md", "docs/LOG.md"

    def _named(self, w: Tree, rel: str) -> None:
        """Point the config at `rel` for the decision log (and its generated block)."""
        w.write(layout.CONFIG, w.read(layout.CONFIG).replace(LOG, rel))

    def _lower_case(self, w: Tree) -> None:
        """The config and the working copy say `docs/decisions.md`; git tracks
        `docs/DECISIONS.md`. One file where the file system ignores case, and the same
        question to git everywhere."""
        self._named(w, self.LOWER)
        os.rename(w.root / LOG, w.root / self.LOWER)

    def test_a_base_log_missing_from_the_object_store_fails_the_gate(self):
        w = self.solo()
        head = w.git("rev-parse", "HEAD").strip()
        w.edit(LOG, *SettledDecisionChangedInPlace.EDIT)
        blob = w.git("rev-parse", f"HEAD:{LOG}").strip()
        held = w.root / ".git" / "objects" / blob[:2] / blob[2:]
        held.chmod(stat.S_IREAD | stat.S_IWRITE)        # git's objects are read-only
        held.unlink()
        self.silent(w)
        self.assertEqual(self.found(w, "--base", "main"), (1, [ERROR + no_compare(
            "main", f"git could not read {LOG} at {head[:12]}")]))

    def test_a_log_git_tracks_under_a_name_in_another_case_is_read_from_it(self):
        w = self.solo()
        self._lower_case(w)
        self.assertEqual(w.git("ls-tree", "--name-only", "HEAD", "--", "docs/").split(),
                         [LOG, "docs/working"])
        self.silent(w)
        self.silent(w, "--base", "main")
        w.edit(self.LOWER, *SettledDecisionChangedInPlace.EDIT)
        line = changed(self.LOWER, "P-1")
        self.assertEqual(self.found(w), (0, [WARN + line]))
        self.assertEqual(self.found(w, "--base", "main"), (1, [ERROR + line]))

    def test_two_base_files_that_differ_only_in_case_are_not_guessed_between(self):
        w = self.solo()
        blob = w.git("rev-parse", f"HEAD:{LOG}").strip()
        exact = ("-c", "core.ignorecase=false")
        w.git(*exact, "update-index", "--add", "--cacheinfo", f"100644,{blob},docs/Decisions.md")
        w.git(*exact, "commit", "-qm", "the same name twice, where the file system ignores case")
        head = w.git("rev-parse", "HEAD").strip()
        self.assertEqual(w.git("ls-tree", "--name-only", "HEAD", "--", "docs/").split(),
                         [LOG, "docs/Decisions.md", "docs/working"])
        self._lower_case(w)
        w.edit(self.LOWER, *SettledDecisionChangedInPlace.EDIT)
        self.silent(w)
        self.assertEqual(self.found(w, "--base", "main"), (1, [ERROR + no_compare(
            "main", f"{head[:12]} holds {self.LOWER} under 2 names that differ only in case")]))

    def test_an_edit_made_in_the_same_change_as_a_rename_of_the_log_is_compared(self):
        w = self.solo()
        w.branch("work")
        w.git("mv", LOG, self.RENAMED)
        self._named(w, self.RENAMED)
        self.silent(w)                                   # renamed, and nothing else
        self.silent(w, "--base", "main")
        w.edit(self.RENAMED, *SettledDecisionChangedInPlace.EDIT)
        line = changed(self.RENAMED, "P-1")
        self.assertEqual(self.found(w), (0, [WARN + line]))
        self.assertEqual(self.found(w, "--base", "main"), (1, [ERROR + line]))
        w.commit("renamed and reworded")
        self.silent(w)
        self.assertEqual(self.found(w, "--base", "main"), (1, [ERROR + line]))

    def test_a_decision_removed_in_the_same_change_as_a_rename_of_the_log(self):
        w = self.solo()
        w.git("mv", LOG, self.RENAMED)
        self._named(w, self.RENAMED)
        head, _, rest = w.read(self.RENAMED).partition("\n## P-2 - Title 2\n")
        w.write(self.RENAMED, head + "\n## P-3" + rest.partition("\n## P-3")[2])
        self.assertEqual(self.found(w, "--base", "main"), (0, [WARN + removed(
            self.RENAMED, "P-2", "provisional", "## P-2 - Replaced by <id>")]))

    def test_an_id_taken_on_the_base_branch_is_found_from_a_renamed_log(self):
        # The base branch still has the log under its old name: that is where its P-4 is.
        w = self.solo()
        w.branch("work")
        w.git("mv", LOG, self.RENAMED)
        self._named(w, self.RENAMED)
        w.append(self.RENAMED, decision("P", 4, title="Ours"))
        w.commit("ours, in the renamed log")
        w.switch("main")
        w.append(LOG, decision("P", 4, title="Their title"))
        w.commit("theirs")
        w.switch("work")
        self.assertEqual(self.found(w, "--base", "main"),
                         (1, [ERROR + taken(self.RENAMED, "P-4", "main", "Their title")]))

    MORE = 52

    def _rewritten(self, w: Tree, tell_git: bool = True) -> None:
        """The log renamed, the config pointed at it, `P-1` (locked) reworded with no Revised
        line, and 52 decisions added, all in one change: so much that git reads no rename, only
        one file deleted and another added."""
        if tell_git:
            w.git("mv", LOG, self.RENAMED)
        else:
            os.rename(w.root / LOG, w.root / self.RENAMED)
        self._named(w, self.RENAMED)
        w.edit(self.RENAMED, *SettledDecisionChangedInPlace.EDIT)
        w.append(self.RENAMED, "".join(decision("P", n, "provisional")
                                       for n in range(4, 4 + self.MORE)))

    def test_a_log_renamed_and_rewritten_in_one_commit_is_found_by_the_ids_it_holds(self):
        w = self.solo()
        w.branch("work")
        self._rewritten(w)
        line = changed(self.RENAMED, "P-1")
        self.assertEqual(self.found(w), (0, [WARN + line]))
        w.commit("renamed, reworded, and 52 more")
        fork = base.resolve(w.root, "main")
        self.assertEqual(base.since(w.root, fork)[0], {})        # git reads no rename
        self.silent(w)
        self.assertEqual(self.found(w, "--base", "main"), (1, [ERROR + line]))
        cmp = base.compare(w.context("main"))
        self.assertEqual([(i.log, i.entry.ident) for i in cmp.side("base", base.DECISION)],
                         [(LOG, "P-1"), (LOG, "P-2"), (LOG, "P-3")])
        self.assertEqual(len(cmp.side("current", base.DECISION)), 3 + self.MORE)

    def test_a_log_moved_without_telling_git_and_rewritten_is_found_the_same_way(self):
        # `mv`, not `git mv`: the new file is one git does not track yet.
        w = self.solo()
        self._rewritten(w, tell_git=False)
        line = changed(self.RENAMED, "P-1")
        self.assertEqual(self.found(w), (0, [WARN + line]))
        self.assertEqual(self.found(w, "--base", "main"), (1, [ERROR + line]))

    def test_a_decision_dropped_while_the_log_was_renamed_and_rewritten_is_removed(self):
        w = self.solo()
        w.branch("work")
        self._rewritten(w)
        head, _, rest = w.read(self.RENAMED).partition("\n## P-3 - Title 3\n")
        w.write(self.RENAMED, head + "\n" + BLOCK + "\n## P-4" + rest.partition("\n## P-4")[2])
        w.commit("renamed, reworded, P-3 dropped, and 52 more")
        self.assertEqual(self.found(w, "--base", "main"), (1, [
            ERROR + changed(self.RENAMED, "P-1"),
            ERROR + removed(LOG, "P-3", "locked", "## P-3 - Replaced by <id>")]))

    def test_an_id_taken_on_the_base_branch_is_found_from_a_log_renamed_and_rewritten(self):
        # The base branch still has the log under its old name: that is where its P-60 is.
        w = self.solo()
        w.branch("work")
        self._rewritten(w)
        w.append(self.RENAMED, decision("P", 60, title="Ours"))
        w.commit("ours, in the rewritten log")
        w.switch("main")
        w.append(LOG, decision("P", 60, title="Their title"))
        w.commit("theirs")
        w.switch("work")
        self.assertEqual(self.found(w, "--base", "main"), (1, [
            ERROR + changed(self.RENAMED, "P-1"),
            ERROR + taken(self.RENAMED, "P-60", "main", "Their title")]))

    def _both_rewritten(self, w: Team) -> None:
        """On a branch: alpha's log and gamma's renamed `LOG.md`, the config pointed at the
        name, and 52 decisions added to each. Staged, not committed; git reads no rename."""
        w.branch("work")
        w.write(layout.CONFIG, w.read(layout.CONFIG).replace(
            '[projects]\n', '[projects]\ndecision_log = "LOG.md"\n').replace(
            'file = "DECISIONS.md"', 'file = "LOG.md"'))
        for where, prefix, lo in (("projects/alpha", "A", 100), ("projects/gamma", "G", 300)):
            w.git("mv", f"{where}/DECISIONS.md", f"{where}/LOG.md")
            w.append(f"{where}/LOG.md", "".join(decision(prefix, lo + n, "provisional")
                                               for n in range(2, 2 + self.MORE)))

    BOTH = [("governance/DECISIONS.md", "W-1", "workspace"),
            ("governance/DECISIONS.md", "W-2", "workspace"),
            ("projects/alpha/DECISIONS.md", "A-100", "alpha"),
            ("projects/alpha/DECISIONS.md", "A-101", "alpha"),
            ("projects/gamma/DECISIONS.md", "G-300", "gamma"),
            ("projects/gamma/DECISIONS.md", "G-301", "gamma")]

    def _base_side(self, w: Tree) -> list[tuple[str, str, str]]:
        cmp = base.compare(w.context("main"), traps=False)
        self.assertEqual(cmp.not_compared, [])
        return sorted((i.log, i.entry.ident, i.scope.name) for i in cmp.side("base"))

    def test_each_of_two_logs_renamed_and_rewritten_is_found_from_its_own(self):
        w = self.team()
        self._both_rewritten(w)
        w.edit("projects/alpha/LOG.md", "## A-100 - Title 100", "## A-100 - Another title")
        w.commit("both logs renamed and rewritten")
        self.assertEqual(base.since(w.root, base.resolve(w.root, "main"))[0], {})
        line = ERROR + changed("projects/alpha/LOG.md", "A-100")
        self.assertEqual(self.found(w, "--base", "main"), (1, [line]))
        self.assertEqual(self.found(w, "--project", "alpha", "--base", "main"), (1, [line]))
        self.silent(w, "--project", "gamma", "--base", "main")
        self.assertEqual(self._base_side(w), self.BOTH)

    def test_a_deleted_file_two_logs_hold_ids_of_is_read_once_and_is_its_own_projects(self):
        # G-301 moved to alpha's new log while both were renamed and rewritten, so gamma's old
        # log holds an id of each new one. It is still one file, gamma's, read once: a
        # decision dropped from it is gamma's removal and nobody else's.
        w = self.team()
        w.append("projects/gamma/DECISIONS.md", decision("G", 399))
        w.commit("a third decision of gamma's")
        self._both_rewritten(w)
        head, mark, entry = w.read("projects/gamma/LOG.md").partition("\n## G-301 - ")
        entry, _, rest = entry.partition("\n## G-399 - ")
        w.write("projects/gamma/LOG.md",
                head + "\n## G-302 - " + rest.partition("\n## G-302 - ")[2])
        w.append("projects/alpha/LOG.md", mark + entry)
        w.commit("both logs renamed and rewritten, G-301 moved and G-399 dropped")
        self.assertEqual(base.since(w.root, base.resolve(w.root, "main"))[0], {})
        self.assertEqual(self._base_side(w), self.BOTH + [
            ("projects/gamma/DECISIONS.md", "G-399", "gamma")])
        gone = ERROR + removed("projects/gamma/DECISIONS.md", "G-399", "locked",
                               "## G-399 - Replaced by <id>")
        self.assertEqual(self.found(w, "--base", "main"), (1, [gone]))
        self.assertEqual(self.found(w, "--project", "gamma", "--base", "main"), (1, [gone]))
        self.silent(w, "--project", "alpha", "--base", "main")

    def test_a_deleted_log_that_is_read_by_name_is_not_read_again_for_another_log(self):
        # Gamma has no log at the base. Alpha's is deleted outright, and A-100 is now in the
        # log gamma starts: alpha's old log is read as alpha's, once, and A-100 has moved.
        w = self.team()
        w.git("rm", "-q", "projects/gamma/DECISIONS.md")
        w.git("commit", "-qm", "gamma has no log yet")
        w.branch("work")
        moved = decision("A", 100)
        self.assertIn(moved, w.read("projects/alpha/DECISIONS.md"))
        w.git("rm", "-q", "projects/alpha/DECISIONS.md")
        w.write("projects/gamma/DECISIONS.md", fm() + "# Decisions\n\n" + BLOCK + moved + "".join(
            decision("G", 300 + n, "provisional") for n in range(self.MORE)))
        w.commit("alpha's log deleted, gamma's begun")
        self.assertEqual(base.since(w.root, base.resolve(w.root, "main"))[0], {})
        self.assertEqual(self._base_side(w), [row for row in self.BOTH if row[2] != "gamma"])
        # A project with no log is an error of other checks; this one adds one warning.
        self.assertEqual(self.found(w, "--base", "main")[1], [WARN + removed(
            "projects/alpha/DECISIONS.md", "A-101", "provisional",
            "## A-101 - Replaced by <id>")])

    def test_a_log_deleted_outright_with_no_successor_is_every_settled_decision_removed(self):
        # Nothing took its place, and other Markdown went in the same commit: still removals.
        w = self.solo()
        w.write("notes/old.md", "# Old notes\n")
        w.commit("notes")
        w.branch("work")
        w.git("rm", "-q", LOG, "notes/old.md")
        w.git("commit", "-qm", "the log, deleted")
        code, lines = self.found(w, "--base", "main")
        self.assertEqual((code, lines), (1, [
            ERROR + removed(LOG, "P-1", "locked", "## P-1 - Replaced by <id>"),
            ERROR + removed(LOG, "P-3", "locked", "## P-3 - Replaced by <id>"),
            WARN + removed(LOG, "P-2", "provisional", "## P-2 - Replaced by <id>")]))

    def test_an_unrelated_deleted_markdown_file_changes_nothing(self):
        # A log that is new, in a change that also deletes Markdown holding none of its ids:
        # an entry under another id, and the log's own P-1 only as an example in a code fence.
        w = self._no_log_yet()
        w.write("notes/old.md", "# Old notes\n" + decision("P", 900)
                + "\n```markdown\n## P-1 - Title 1\n```\n\n<!--\n## P-2 - Title 2\n-->\n")
        w.git("add", "notes")
        w.git("commit", "-qm", "notes")
        w.git("rm", "-q", "notes/old.md")
        self.silent(w)
        self.silent(w, "--base", "main")
        cmp = base.compare(w.context("main"))
        self.assertEqual((cmp.not_compared, cmp.side("base", base.DECISION)), ([], []))

    def test_an_unrelated_deleted_markdown_file_beside_the_log_it_was_changes_nothing(self):
        w = self.solo()
        w.write("notes/old.md", "# Old notes\n" + decision("P", 900))
        w.commit("notes")
        w.branch("work")
        w.git("rm", "-q", "notes/old.md")
        self._rewritten(w)
        w.commit("renamed, reworded, 52 more, and the notes deleted")
        self.assertEqual(self.found(w, "--base", "main"),
                         (1, [ERROR + changed(self.RENAMED, "P-1")]))
        cmp = base.compare(w.context("main"))
        self.assertEqual({i.log for i in cmp.side("base", base.DECISION)}, {LOG})

    def _deleted(self, count: int) -> Solo:
        """A project whose log is new, in a change that deletes `count` Markdown files."""
        w = self._no_log_yet()
        for n in range(count):
            w.write(f"notes/{n}.md", f"# Note {n}\n")
        w.git("add", "notes")
        w.git("commit", "-qm", "notes")
        w.git("rm", "-qr", "notes")
        return w

    def test_more_deleted_markdown_files_than_are_read_fails_the_gate_only(self):
        w = self._deleted(base.DELETED_READ + 1)
        head = w.git("rev-parse", "HEAD").strip()
        self.silent(w)
        self.assertEqual(self.found(w, "--base", "main"), (1, [ERROR + no_compare(
            "main", f"{LOG} is not at {head[:12]}, and 51 Markdown files were deleted since: "
                    f"more than the 50 that are read to find the log it was")]))

    def test_as_many_deleted_markdown_files_as_are_read_is_still_a_new_log(self):
        w = self._deleted(base.DELETED_READ)
        self.silent(w)
        self.silent(w, "--base", "main")
        self.assertEqual(base.compare(w.context("main")).not_compared, [])

    def test_a_deleted_markdown_file_that_is_not_utf8_fails_the_gate_only(self):
        # It may have been the log: it cannot be read, so nothing says it was not.
        w = self._no_log_yet()
        w.write("notes/old.md", b"# Old notes\n\xff\n")
        w.git("add", "notes")
        w.git("commit", "-qm", "notes")
        head = w.git("rev-parse", "HEAD").strip()
        w.git("rm", "-q", "notes/old.md")
        self.silent(w)
        self.assertEqual(self.found(w, "--base", "main"), (1, [ERROR + no_compare(
            "main", f"notes/old.md at {head[:12]} is not valid UTF-8 (byte 12)")]))

    def _no_log_yet(self) -> Solo:
        """A project whose first commit has no decision log: the one on disk is new."""
        w = self.solo(git="init")
        w.git("add", "-A")
        w.git("rm", "-q", "--cached", LOG)
        w.git("commit", "-qm", "before there was a log")
        return w

    def test_a_log_that_really_is_new_is_still_silent(self):
        w = self._no_log_yet()
        self.silent(w)
        self.silent(w, "--base", "main")
        cmp = base.compare(w.context("main"))
        self.assertEqual((cmp.not_compared, cmp.side("base", base.DECISION)), ([], []))
        self.assertEqual(len(cmp.side("current", base.DECISION)), 3)

    def test_a_git_that_cannot_list_the_base_fails_the_gate(self):
        w = self.solo()
        head = w.git("rev-parse", "HEAD").strip()
        w.edit(LOG, *SettledDecisionChangedInPlace.EDIT)
        env = self.stand_in_git('case " $* " in *" ls-tree -z "*) exit 1;; esac')
        self.silent(w, env=env)
        self.assertEqual(self.found(w, "--base", "main", env=env), (1, [ERROR + no_compare(
            "main", f"git could not list docs at {head[:12]}")]))

    def test_a_git_that_cannot_say_what_was_renamed_fails_the_gate(self):
        # A log on disk that is not at the base is new, or renamed: only git can say which.
        w = self._no_log_yet()
        head = w.git("rev-parse", "HEAD").strip()
        env = self.stand_in_git('case " $* " in *" diff "*) exit 1;; esac')
        self.silent(w, env=env)
        self.assertEqual(self.found(w, "--base", "main", env=env), (1, [ERROR + no_compare(
            "main", f"git could not say what was renamed since {head[:12]}")]))


# ---------------------------------------------------------------------------- pairing, and what is asked

class Pairing(Case):
    """`base.match`: which base entry is which entry now, when one id is in several logs."""

    ALPHA, GAMMA, GOV = ("projects/alpha/DECISIONS.md", "projects/gamma/DECISIONS.md",
                         "governance/DECISIONS.md")

    def _twice(self) -> Team:
        """A-150 is in alpha's log and in gamma's, both locked, saying different things."""
        w = self.team()
        w.append(self.ALPHA, decision("A", 150, title="Alpha's"))
        w.append(self.GAMMA, decision("A", 150, title="Gamma's"))
        w.commit("one id, two logs")
        return w

    def _cut(self, w: Tree, rel: str) -> str:
        head, mark, entry = w.read(rel).partition("\n## A-150 - ")
        assert mark, rel
        w.write(rel, head)
        return mark + entry

    def test_an_id_in_two_logs_is_paired_log_by_log_before_anything_is_called_moved(self):
        # Gamma's A-150 moves to the workspace's log, which is read first: alpha's is still
        # alpha's, and neither changed.
        w = self._twice()
        w.append(self.GOV, self._cut(w, self.GAMMA))
        cmp = base.compare(w.context(), traps=False)
        matched, removed_, added = base.match(cmp.side("base"), cmp.side("current"))
        self.assertEqual([(was.log, now.log, now.entry.title) for was, now in matched
                          if was.entry.ident == "A-150"],
                         [(self.ALPHA, self.ALPHA, "Alpha's"), (self.GAMMA, self.GOV, "Gamma's")])
        self.assertEqual((removed_, added), ([], []))
        self.silent(w)
        self.silent(w, "--base", "main")

    def test_two_left_over_on_one_side_allow_two_readings_and_nothing_is_said(self):
        # Both are gone from their logs and one A-150, saying a third thing, is in the
        # workspace's: which of the two it was is anybody's guess.
        w = self._twice()
        self._cut(w, self.ALPHA)
        self._cut(w, self.GAMMA)
        w.append(self.GOV, decision("A", 150, title="A third thing"))
        cmp = base.compare(w.context(), traps=False)
        matched, removed_, added = base.match(cmp.side("base"), cmp.side("current"))
        self.assertEqual([was.entry.ident for was, _ in matched].count("A-150"), 0)
        self.assertEqual((removed_, added), ([], []))
        self.silent(w)
        self.silent(w, "--base", "main")
        # And the other way about: one at the base, two now, neither where it was.
        matched, removed_, added = base.match(cmp.side("current"), cmp.side("base"))
        self.assertEqual([was.entry.ident for was, _ in matched].count("A-150"), 0)
        self.assertEqual((removed_, added), ([], []))


class WhatIsKept(Case):
    def test_the_first_reason_a_repository_was_not_compared_is_the_one_kept(self):
        # A repository is compared whole or not at all, and says why once: a ref it does not
        # have before a file that cannot be read, the first such file before the next.
        w = self.team(nested=True)
        for rel in ("governance/DECISIONS.md", "projects/alpha/DECISIONS.md", "beta/DECISIONS.md"):
            w.write(rel, b"# Decisions\n\xff\n")
        cmp = base.compare(w.context("main"))
        self.assertFalse(cmp.complete)
        self.assertEqual([(r.label, r.commit, r.tip, r.reason, r.base, r.theirs) for r in cmp.repos], [
            (".", None, None, "governance/DECISIONS.md is not valid UTF-8 (byte 12)", [], []),
            ("beta", None, None, "main names no commit in this repository", [], [])])


class GitCalls(Case):
    """What the check costs a run, in git calls: recorded by a `git` on PATH."""

    def _named(self, w: Tree, calls: list[str]) -> list[str]:
        """Each call with the commit ids in it replaced by what they are."""
        for name, args in (("<head>", ("rev-parse", "HEAD")), ("<tip>", ("rev-parse", "main")),
                           ("<base>", ("merge-base", "main", "HEAD"))):
            calls = [call.replace(w.git(*args).strip(), name) for call in calls]
        return calls

    def test_an_ordinary_run_asks_three_things_and_reads_no_trap_file(self):
        w = self.solo()
        w.branch("work")
        w.append(TRAPS, trap(2))
        w.commit("ours")
        self.assertEqual(self._named(w, self.git_calls(w, "check")), [
            "rev-parse --verify HEAD^{commit}",
            "ls-tree -z <head> -- docs/",
            f"cat-file blob <head>:{LOG}"])

    def test_the_gate_reads_both_sides_of_the_log_and_of_the_trap_files(self):
        w = self.solo()
        w.branch("work")
        w.append(TRAPS, trap(2))
        w.commit("ours")
        w.switch("main")
        w.append(LOG, decision("P", 4))
        w.commit("theirs")
        w.switch("work")
        self.assertEqual(self._named(w, self.git_calls(w, "check", "--base", "main")), [
            "rev-parse --verify HEAD^{commit}",
            "rev-parse --is-shallow-repository",
            "rev-parse --verify main^{commit}",
            "merge-base <tip> HEAD",
            "ls-tree -z <base> -- docs/",
            f"cat-file blob <base>:{LOG}",
            "ls-tree -z <tip> -- docs/",
            f"cat-file blob <tip>:{LOG}",
            "ls-tree -r --name-only -z <base> -- docs/working",
            f"cat-file blob <base>:{TRAPS}",
            "ls-tree -r --name-only -z <tip> -- docs/working",
            f"cat-file blob <tip>:{TRAPS}"])

    def test_a_base_that_is_the_tip_is_read_once(self):
        # `--base HEAD`, or a branch that has merged the base in: one commit, asked once.
        w = self.solo()
        calls = self._named(w, self.git_calls(w, "check", "--base", "HEAD"))
        self.assertEqual([call for call in calls if not call.startswith(("rev-parse", "merge-base"))], [
            "ls-tree -z <head> -- docs/",
            f"cat-file blob <head>:{LOG}",
            "ls-tree -r --name-only -z <head> -- docs/working",
            f"cat-file blob <head>:{TRAPS}"])

    def test_a_log_that_is_not_at_the_base_costs_one_question_about_renames(self):
        w = self.solo(git="init")
        w.git("add", "-A")
        w.git("rm", "-q", "--cached", LOG)
        w.git("commit", "-qm", "before there was a log")
        self.assertEqual(self._named(w, self.git_calls(w, "check")), [
            "rev-parse --verify HEAD^{commit}",
            "ls-tree -z <head> -- docs/",
            "-c diff.autoRefreshIndex=false diff -M --name-status -z <head>"])


    def test_a_log_found_by_its_ids_costs_one_read_for_each_deleted_markdown_file(self):
        w = self.solo()
        w.write("notes/old.md", "# Old notes\n")
        w.write("notes/old.txt", "## P-1 - Not Markdown, so not read\n")
        w.commit("notes")
        w.git("rm", "-q", "notes/old.md", "notes/old.txt")
        w.git("mv", LOG, ReadingTheBase.RENAMED)
        w.write(layout.CONFIG, w.read(layout.CONFIG).replace(LOG, ReadingTheBase.RENAMED))
        w.append(ReadingTheBase.RENAMED, "".join(decision("P", n) for n in range(4, 56)))
        self.assertEqual(self._named(w, self.git_calls(w, "check")), [
            "rev-parse --verify HEAD^{commit}",
            "ls-tree -z <head> -- docs/",
            "-c diff.autoRefreshIndex=false diff -M --name-status -z <head>",
            f"cat-file blob <head>:{LOG}",
            "cat-file blob <head>:notes/old.md"])


class Fixture(Case):
    def test_a_trees_commit_ids_do_not_depend_on_the_time_zone(self):
        ids = set()
        for zone in ("UTC", "AAA-13", "BBB+8"):
            with self.subTest(zone=zone), self.fresh(), mock.patch.dict(os.environ, {"TZ": zone}):
                w = Tree(self.tmp / "tree", self.tmp / "home")
                w.write("a.md", "# A\n")
                w.init()
                w.git("add", "-A")
                w.git("commit", "-qm", "start")
                ids.add(w.git("rev-parse", "HEAD").strip())
        self.assertEqual(len(ids), 1, ids)


# ---------------------------------------------------------------------------- layouts

class Layouts(Case):
    def test_a_governance_root_below_the_git_top(self):
        w = self.solo(below="outer")
        self.assertTrue((w.top / ".git").is_dir() and not (w.root / ".git").exists())
        w.edit(LOG, *SettledDecisionChangedInPlace.EDIT)
        self.assertEqual(self.found(w), (0, [WARN + changed(LOG, "P-1")]))
        self.assertEqual(self.found(w, "--base", "main"), (1, [ERROR + changed(LOG, "P-1")]))
        cmp = base.compare(w.context())
        self.assertEqual([(r.label, r.root, r.path) for r in cmp.repos],
                         [(".", True, w.top.resolve())])

    def test_a_nested_project_repository_is_compared_with_its_own_head(self):
        w = self.team(nested=True)
        w.edit("beta/DECISIONS.md", "## B-200 - Title 200", "## B-200 - Another title")
        self.assertEqual(self.found(w), (0, [WARN + changed("beta/DECISIONS.md", "B-200")]))
        w.commit("reword", at=w.root / "beta")
        self.silent(w)

    def test_project_reports_that_projects_changes_only(self):
        w = self.team()
        w.edit("projects/alpha/DECISIONS.md", "## A-100 - Title 100", "## A-100 - Another title")
        w.edit("governance/DECISIONS.md", "## W-1 - Title 1", "## W-1 - Another title")
        alpha = changed("projects/alpha/DECISIONS.md", "A-100")
        ws = changed("governance/DECISIONS.md", "W-1")
        self.assertEqual(self.found(w, "--project", "alpha"), (0, [WARN + alpha]))
        self.silent(w, "--project", "gamma")
        self.assertEqual(self.found(w, "--project", "alpha", "--base", "main"), (1, [ERROR + alpha]))
        self.silent(w, "--project", "gamma", "--base", "main")
        self.assertEqual(self.found(w, "--base", "main"), (1, [ERROR + ws, ERROR + alpha]))

    def test_workspace_only_reports_the_workspaces_own_log_only(self):
        w = self.team()
        w.edit("projects/alpha/DECISIONS.md", "## A-100 - Title 100", "## A-100 - Another title")
        self.silent(w, "--workspace-only")
        self.silent(w, "--workspace-only", "--base", "main")
        w.edit("governance/DECISIONS.md", "## W-1 - Title 1", "## W-1 - Another title")
        ws = changed("governance/DECISIONS.md", "W-1")
        self.assertEqual(self.found(w, "--workspace-only"), (0, [WARN + ws]))
        self.assertEqual(self.found(w, "--workspace-only", "--base", "main"), (1, [ERROR + ws]))

    def _snapshot(self, w: Tree) -> Path:
        """Alpha's directory as `git checkout-index` leaves the staged tree: no `.git`."""
        snap = self.tmp / "snapshot"
        shutil.copytree(w.root / "projects/alpha", snap)
        return snap

    def test_a_snapshot_without_history_is_not_compared_and_that_fails_the_gate(self):
        w = self.team()
        snap = self._snapshot(w)
        log = snap / "DECISIONS.md"
        wtext(log, rtext(log).replace(REWORD[0], REWORD[1], 1))
        self.silent(w, "--project", "alpha", "--path", str(snap))
        # Asked for a gate, there is nothing to compare the snapshot with: not green.
        self.assertEqual(self.found(w, "--project", "alpha", "--path", str(snap), "--base", "main"),
                         (1, [ERROR + no_compare("main", "a snapshot needs --history-from to be "
                                                 "compared", repo="projects/alpha")]))

    def test_a_snapshot_is_compared_with_history_froms_head(self):
        w = self.team()
        snap = self._snapshot(w)
        flags = ("--project", "alpha", "--path", str(snap), "--history-from", "projects/alpha")
        self.silent(w, *flags)
        # An edit in the working tree only is not in the snapshot, so it is not reported.
        w.edit("projects/alpha/DECISIONS.md", "## A-100 - Title 100", "## A-100 - Unstaged")
        self.silent(w, *flags)
        log = snap / "DECISIONS.md"
        wtext(log, rtext(log).replace(REWORD[0], REWORD[1], 1))
        line = changed("projects/alpha/DECISIONS.md", "A-100")       # the real path, not the snapshot's
        self.assertEqual(self.found(w, *flags), (0, [WARN + line]))

    def test_a_snapshot_under_base_is_compared_with_the_merge_base_in_that_checkout(self):
        w = self.team()
        w.branch("work")
        w.edit("projects/alpha/DECISIONS.md", "## A-100 - Title 100", "## A-100 - Another title")
        w.commit("reword")
        snap = self._snapshot(w)
        flags = ("--project", "alpha", "--path", str(snap), "--history-from", "projects/alpha")
        self.silent(w, *flags)                                      # same as HEAD
        self.assertEqual(self.found(w, *flags, "--base", "main"),
                         (1, [ERROR + changed("projects/alpha/DECISIONS.md", "A-100")]))

    def test_a_snapshot_is_compared_with_the_projects_log_wherever_history_from_points(self):
        # `--history-from` names the checkout to ask: the repository's top, as a hook has it,
        # or the project's own directory inside it. Either way the file is the project's log.
        line = changed("projects/alpha/DECISIONS.md", "A-100")
        for history in (".", "projects/alpha"):
            with self.subTest(history=history), self.fresh():
                w = self.team()
                snap = self._snapshot(w)
                flags = ("--project", "alpha", "--path", str(snap), "--history-from", history)
                self.silent(w, *flags)
                self.silent(w, *flags, "--base", "HEAD")
                log = snap / "DECISIONS.md"
                wtext(log, rtext(log).replace(REWORD[0], REWORD[1], 1))
                self.assertEqual(self.found(w, *flags), (0, [WARN + line]))
                self.assertEqual(self.found(w, *flags, "--base", "HEAD"), (1, [ERROR + line]))
                # Given as a whole path, it is the same directory.
                whole = (*flags[:-1], str(w.root / history))
                self.assertEqual(self.found(w, *whole, "--base", "HEAD"), (1, [ERROR + line]))

    def test_a_snapshot_of_a_project_at_the_repository_top(self):
        w = self.solo()
        snap = self.tmp / "snapshot"
        shutil.copytree(w.root, snap, ignore=shutil.ignore_patterns(".git"))
        flags = ("--project", "solo", "--path", str(snap), "--history-from", ".")
        self.silent(w, *flags)
        wtext(snap / LOG, rtext(snap / LOG).replace(*SettledDecisionChangedInPlace.EDIT))
        self.assertEqual(self.found(w, *flags), (0, [WARN + changed(LOG, "P-1")]))
        self.assertEqual(self.found(w, *flags, "--base", "HEAD"),
                         (1, [ERROR + changed(LOG, "P-1")]))

    def test_a_snapshot_of_a_project_that_is_its_own_repository(self):
        # Asked of the workspace's repository, which does not track the project's files, the
        # log is still the one the project's own repository holds.
        line = changed("beta/DECISIONS.md", "B-200")
        for history in (".", "beta"):
            with self.subTest(history=history), self.fresh():
                w = self.team(nested=True)
                snap = self.tmp / "snapshot"
                shutil.copytree(w.root / "beta", snap, ignore=shutil.ignore_patterns(".git"))
                wtext(snap / "DECISIONS.md",
                      rtext(snap / "DECISIONS.md").replace(REWORD[0], REWORD[1], 1))
                flags = ("--project", "beta", "--path", str(snap), "--history-from", history)
                self.assertEqual(self.found(w, *flags), (0, [WARN + line]))
                self.assertEqual(self.found(w, *flags, "--base", "HEAD"), (1, [ERROR + line]))

    def test_a_snapshot_whose_history_is_no_repository_fails_the_gate_only(self):
        w = self.team()
        snap = self._snapshot(w)
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        flags = ("--project", "alpha", "--path", str(snap), "--history-from", str(elsewhere))
        self.silent(w, *flags)
        self.assertEqual(self.found(w, *flags, "--base", "main"),
                         (1, [ERROR + no_compare("main", "not a git repository",
                                                 repo="projects/alpha")]))

    def test_crlf_logs(self):
        w = self.solo()
        crlf = w.read(LOG).replace("\n", "\r\n")
        # The same log checked out with CRLF (git on Windows): nothing changed.
        w.write(LOG, crlf)
        self.silent(w)
        self.silent(w, "--base", "main")
        w.git("add", "-A")
        w.git("commit", "-qm", "crlf")
        self.assertIn(b"\r\n", w.git("show", f"HEAD:{LOG}").encode("utf-8"))
        self.assertEqual(crlf.count("\r\n\r\n## P-2"), 1)
        w.write(LOG, crlf.replace("in the room.\r\n\r\n## P-2", "in the room,\r\nor was.\r\n\r\n## P-2"))
        self.assertEqual(self.found(w), (0, [WARN + changed(LOG, "P-1")]))
        self.assertNotIn("\n", w.read(LOG).replace("\r\n", ""))
        w.edit(LOG, "Title 1\r\n\r\n**Status:** locked\r\n",
               "Title 1\r\n\r\n**Status:** locked\r\n\r\n**Revised:** 2026-10-06 (shorter)\r\n")
        self.silent(w)

    def test_a_log_mixing_hyphen_and_em_dash_headings(self):
        w = self.solo(log=fm() + "# Decisions\n" + decision("P", 1, sep=" — ") + decision("P", 2)
                      + decision("P", 3, sep="—") + "\n" + BLOCK)
        self.silent(w)
        self.silent(w, "--base", "main")
        w.edit(LOG, "## P-1 — Title 1", "## P-1 - Title 1")       # the dash is not text
        w.edit(LOG, "## P-3—Title 3", "## P-3 – Title 3")
        self.silent(w, "--base", "main")
        w.edit(LOG, "## P-3 – Title 3", "## P-3 – Title three")
        self.assertEqual(self.found(w, "--base", "main"), (1, [ERROR + changed(LOG, "P-3")]))

    def test_renamed_statuses_and_locked_statuses(self):
        extra = ('\n[checks.decision-log]\nstatuses = ["accepted", "proposed"]\n'
                 'reasons = { statuses = "the team\'s own words for them" }\n'
                 f'\n[checks.{CHECK}]\nlocked_statuses = ["Accepted"]\n')
        w = self.solo(extra=extra, log=fm() + "# Decisions\n" + decision("P", 1, "accepted")
                      + decision("P", 2, "proposed") + decision("P", 3, "locked") + "\n" + BLOCK)
        w.edit(LOG, "## P-2 - Title 2", "## P-2 - Another title")
        w.edit(LOG, "## P-3 - Title 3", "## P-3 - Another title")     # `locked` means nothing here
        for flags in ((), ("--base", "main")):
            _, out, _ = w.run("check", *flags)
            self.assertNotIn("changed without a new dated", out)
        w.edit(LOG, "## P-1 - Title 1", "## P-1 - Another title")
        _, out, _ = w.run("check")
        self.assertIn(WARN + changed(LOG, "P-1", "accepted"), out)
        _, out, _ = w.run("check", "--base", "main")
        self.assertIn(ERROR + changed(LOG, "P-1", "accepted"), out)
        self.assertEqual(out.count("changed without a new dated"), 1, out)

    def test_a_log_that_is_the_workspaces_and_the_projects_is_compared_once(self):
        w = self.solo(workspace='decision_log = "docs/DECISIONS.md"')
        w.edit(LOG, *SettledDecisionChangedInPlace.EDIT)
        self.assertEqual(self.found(w), (0, [WARN + changed(LOG, "P-1")]))
        self.assertEqual(self.found(w, "--project", "solo", "--base", "main"),
                         (1, [ERROR + changed(LOG, "P-1")]))

    def test_govern_base_alone_is_a_check(self):
        w = self.solo()
        w.edit(LOG, *SettledDecisionChangedInPlace.EDIT)
        code, out, _ = w.run("--base", "main")
        self.assertEqual(code, 1)
        self.assertIn(ERROR + changed(LOG, "P-1"), out)


# ---------------------------------------------------------------------------- the ordinary day

class OrdinaryDay(Case):
    """What one writer does all day, each of which must print nothing: uncommitted, committed,
    and on a branch a pull request gates."""

    def quiet(self, w: Tree, at: Path | None = None) -> None:
        self.silent(w)
        self.silent(w, "--base", "main")
        w.git("checkout", "-qb", "ordinary", at=at)
        w.commit("an ordinary change", at=at)
        self.silent(w)
        self.silent(w, "--base", "main")

    def test_rewrapping_a_paragraph(self):
        w = self.solo()
        w.edit(LOG, "## P-1 - Title 1\n\n**Status:** locked\n\n**Rule:** always write the rule "
                    "down before acting on it.\n",
               "## P-1 - Title 1\n\n**Status:** locked\n\n\n**Rule:** always write\nthe rule   down "
               "before\nacting on it.   \n\n")
        self.quiet(w)

    def test_changing_the_heading_dash(self):
        w = self.solo()
        w.edit(LOG, "## P-1 - Title 1", "## P-1 — Title 1")
        w.edit(LOG, "## P-3 - Title 3", "## P-3–Title 3")
        self.quiet(w)

    def test_adding_a_topic(self):
        w = self.solo()
        w.edit(LOG, "Title 1\n\n**Status:** locked\n", "Title 1\n\n**Topic:** Process\n\n**Status:** locked\n")
        self.quiet(w)

    def test_changing_a_topic(self):
        w = self.solo(log=fm() + "# Decisions\n" + decision("P", 1).replace(
            "**Status:**", "**Topic:** Process\n\n**Status:**") + "\n" + BLOCK)
        w.edit(LOG, "**Topic:** Process", "**Topic:** How we work")
        self.quiet(w)

    def test_index_rerendering_a_block_inside_a_locked_entry(self):
        w = self.solo()
        before = w.read(LOG)
        head, mark, block = before.partition("<!-- gov:generated:start")
        self.assertIn("P-3", head.rpartition("\n## ")[2])       # the block is in P-3's body
        w.write(LOG, head.rstrip("\n") + "\n" + decision("P", 4, "provisional") + "\n" + mark + block)
        code, out, _ = w.run("index")
        self.assertIn(f"regenerated {LOG} :: decision-index", out)
        # The block was in P-3's body, a locked entry; it is in P-4's now, and it changed.
        self.assertNotEqual(before.partition("<!--")[2], w.read(LOG).partition("<!--")[2])
        self.quiet(w)

    def test_installer_migrate_apply_on_a_log(self):
        old_shape = (fm() + "# Decisions\n" + decision("P", 1)
                     + decision("P", 2, "superseded", body="Superseded by P-1.\n")
                     + "\n## Networking (P-10-P-19)\n"
                     + decision("P", 10).replace("\n## ", "\n### ")
                     + decision("P", 11, "superseded", body="Superseded by P-10.\n").replace(
                         "\n## ", "\n### ")
                     + "\n" + BLOCK)
        w = self.solo(log=old_shape)
        res = subprocess.run(
            [sys.executable, "-m", "govern.installer", "migrate", "--root", str(w.root), "--apply"],
            env={**os.environ, "PYTHONPATH": str(ENGINE), "HOME": str(w.home),
                 "CONTEXT_GATE_NO_UPDATE_CHECK": "1"}, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        self.assertIn("migrated 1 file(s)", res.stdout)
        migrated = w.read(LOG)
        self.assertIn("## P-2 - Replaced by P-1\n", migrated)
        self.assertIn("## P-10 - Title 10\n\n**Topic:** Networking\n", migrated)
        self.assertIn("## P-11 - Replaced by P-10\n", migrated)
        self.quiet(w)

    def test_moving_a_decision_to_another_log(self):
        w = self.team()
        alpha = w.read("projects/alpha/DECISIONS.md")
        head, _, rest = alpha.partition("\n## A-100 - Title 100\n")
        body, _, tail = rest.partition("\n## A-101")
        w.write("projects/alpha/DECISIONS.md", head + "\n## A-101" + tail)
        w.append("governance/DECISIONS.md", "\n## A-100 - Title 100\n" + body)
        self.silent(w, "--project", "alpha")
        self.silent(w, "--project", "alpha", "--base", "main")
        self.quiet(w)

    def test_renaming_the_log_file(self):
        w = self.solo()
        w.git("mv", LOG, "docs/LOG.md")
        w.write(layout.CONFIG, w.read(layout.CONFIG).replace("docs/DECISIONS.md", "docs/LOG.md"))
        self.quiet(w)

    def test_adding_a_decision(self):
        w = self.solo()
        w.append(LOG, decision("P", 4) + decision("P", 5, "provisional"))
        self.quiet(w)

    def test_editing_a_provisional_decision(self):
        w = self.solo()
        w.edit(LOG, "## P-2 - Title 2\n\n**Status:** provisional\n\n**Rule:** always write the rule "
                    "down before acting on it.",
               "## P-2 - A better title\n\n**Status:** provisional\n\n**Rule:** write it down first.")
        self.quiet(w)

    def test_promoting_provisional_to_locked(self):
        w = self.solo()
        w.edit(LOG, "## P-2 - Title 2\n\n**Status:** provisional", "## P-2 - Title 2\n\n**Status:** locked")
        self.quiet(w)

    def test_adding_a_trap_and_a_new_trap_file(self):
        w = self.solo()
        code, out, _ = w.run("trap-add", "--project", "solo", "--title", "A new one",
                             "--bites", "late")
        self.assertEqual(code, 0, out)
        w.write("docs/working/traps-build.md",
                fm("working", status="active") + "# Build traps\n" + trap(3))
        self.quiet(w)

    def test_a_new_log_in_a_project_that_had_none(self):
        w = self.solo(git="init")
        w.git("add", "-A")
        w.git("rm", "-q", "--cached", LOG)
        w.git("commit", "-qm", "before there was a log")
        self.quiet(w)

    def test_a_nested_projects_ordinary_work(self):
        w = self.team(nested=True)
        w.append("beta/DECISIONS.md", decision("B", 202))
        w.edit("beta/DECISIONS.md", "## B-201 - Title 201\n\n**Status:** provisional",
               "## B-201 - Title 201\n\n**Status:** locked")
        self.silent(w)
        w.commit("ordinary", at=w.root / "beta")
        self.silent(w)


class OneWriter(Case):
    """The claim itself: a single-author repository of ordinary commits gets no extra line of
    any kind from this check, at any point in its history."""

    CLEAN = "[OK] workspace  (0 errors, 0 warnings)\n[OK] solo  (0 errors, 0 warnings)\n" \
            "[OK] total  (0 errors, 0 warnings)\n"

    def test_a_single_author_history_is_byte_identical_with_and_without_the_check(self):
        w = self.solo()
        steps = (
            lambda: w.append(LOG, decision("P", 4, "provisional")),
            lambda: w.edit(LOG, "## P-4 - Title 4", "## P-4 - Title four, reconsidered"),
            lambda: w.edit(LOG, "four, reconsidered\n\n**Status:** provisional",
                           "four, reconsidered\n\n**Status:** locked"),
            lambda: w.run("trap-add", "--project", "solo", "--title", "Another", "--bites", "soon"),
            lambda: w.edit(LOG, "## P-1 - Title 1\n\n**Status:** locked\n",
                           "## P-1 — Title 1\n\n**Topic:** Process\n\n**Status:** locked\n"),
            lambda: w.edit(LOG, "## P-3 - Title 3\n\n**Status:** locked\n", "## P-3 - A clearer "
                           "title\n\n**Status:** locked\n\n**Revised:** 2026-10-06 (the title)\n"),
        )
        for n, step in enumerate(steps):
            step()
            w.run("index")
            self.assertEqual(self.silent(w), (0, self.CLEAN, ""), f"dirty, after step {n}")
            w.commit(f"step {n}")
            self.assertEqual(self.silent(w), (0, self.CLEAN, ""), f"committed, after step {n}")
        self.assertEqual(w.git("rev-list", "--count", "HEAD").strip(), str(len(steps) + 1))

    def test_a_project_that_is_not_in_git_at_all(self):
        w = self.solo(git=None)
        w.append(LOG, decision("P", 4))
        w.edit(LOG, *SettledDecisionChangedInPlace.EDIT)
        w.run("index")
        self.silent(w)


class UpgradedProject(Case):
    """A project that upgrades with a locked decision edited and not yet committed: every way
    it already runs the gate (none of which passes `--base`) exits as it did before, with a
    warning at most."""

    def test_every_existing_invocation_exits_as_before(self):
        for report in ("per-scope", "aggregate"):
            with self.subTest(report=report), self.fresh():
                self._every_shape(report)

    def _every_shape(self, report: str) -> None:
        w = Team(self.tmp, nested=True)
        w.edit(layout.CONFIG, 'markers = "gov"', f'markers = "gov"\nreport = "{report}"')
        w.commit("how it reports")
        snap = self.tmp / "snapshot"
        for rel in ("governance/DECISIONS.md", "projects/alpha/DECISIONS.md", "beta/DECISIONS.md"):
            w.edit(rel, "\n\n**Status:** locked\n\n**Rule:** always", "\n\n**Status:** locked\n\n**Rule:** never")
        shutil.copytree(w.root / "projects/alpha", snap)
        shapes = {
            ("check",): 3,
            ("check", "--project", "alpha"): 1,
            ("--project", "alpha"): 1,
            ("check", "--repo", "beta"): 1,
            ("check", "--workspace-only"): 1,
            ("check", "--project", "alpha", "--path", str(snap)): 0,
            ("check", "--project", "alpha", "--path", str(snap),
             "--history-from", "projects/alpha"): 1,
        }
        for argv, warnings in shapes.items():
            with unregistered():
                before = w.run(*argv)
            after = w.run(*argv)
            self.assertEqual(before[0], 0, (argv, before))
            self.assertEqual((after[0], after[2]), (before[0], before[2]), argv)
            added = collections.Counter(after[1].splitlines()) - collections.Counter(
                before[1].splitlines())
            new = [line for line in added.elements() if not line.startswith("[")]
            self.assertEqual(len(new), warnings, (argv, new))
            self.assertTrue(all(line.startswith(WARN) and "(locked) changed without a new dated" in line
                                for line in new), (argv, new))

    def test_the_check_declares_itself_completely(self):
        chk = manifest.CHECKS[CHECK]
        self.assertEqual((chk.scope, chk.also_project, chk.since, chk.default, chk.origin),
                         ("workspace", True, "0.7.0", "error", "engine"))
        self.assertTrue(chk.summary and chk.question and chk.rationale)
        self.assertEqual({k: (p.type, p.default) for k, p in chk.params.items()},
                         {"locked_statuses": ("list", ["locked"])})
        w = self.solo()
        code, out, _ = w.run("explain", CHECK)
        self.assertEqual(code, 0)
        self.assertIn(f"{CHECK}  error (engine default)", out)
        self.assertIn('locked_statuses = ["locked"]  (engine default)', out)


if __name__ == "__main__":
    unittest.main()
