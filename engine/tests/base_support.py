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


__all__ = [
    "collections",
    "contextlib",
    "io",
    "os",
    "re",
    "shutil",
    "stat",
    "subprocess",
    "sys",
    "tempfile",
    "unittest",
    "date",
    "Path",
    "mock",
    "ENGINE",
    "__version__",
    "base",
    "cli",
    "config",
    "layout",
    "manifest",
    "registry",
    "REPO_ENV",
    "Context",
    "CHECK",
    "TODAY",
    "STAMP",
    "WARN",
    "ERROR",
    "LOG",
    "TRAPS",
    "BLOCK",
    "TRAP_BLOCK",
    "SOLO_CONFIG",
    "TEAM_CONFIG",
    "TEAM_REGISTRY",
    "PROJECT",
    "wtext",
    "rtext",
    "fm",
    "decision",
    "trap",
    "REWORD",
    "changed",
    "removed",
    "taken",
    "no_compare",
    "unregistered",
    "Tree",
    "Solo",
    "Team",
    "Case",
]
