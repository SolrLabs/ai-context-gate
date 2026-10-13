"""Behaviour tests on small throwaway workspaces, each built in the shape a registry file
really takes rather than a shape the code happens to expect.

    python3 -m unittest discover -s engine/tests
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unicodedata
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ENGINE = Path(__file__).resolve().parent.parent
REPO = ENGINE.parent
sys.path.insert(0, str(ENGINE))

from govern import __version__, cli, config, installer, layout, manifest, notice  # noqa: E402
from govern import ratchet, registry, text  # noqa: E402
from govern.profile import rmtree as profile_rmtree  # noqa: E402

CFG = layout.CONFIG

TODAY = date.today().isoformat()

CONFIG = """
[governance]
engine = "{version}"
schema = 1
{extensions}

[dialect]
decision_heading = "em-dash"
markers = "t"

[registry]
file = "projects.toml"
entries = "project"

[registry.keys]
handoff = "{handoff_key}"

[workspace]
required_docs = ["AGENTS.md", "governance/DECISIONS.md"]
docs = ["AGENTS.md", "governance/*.md"]
decision_log = "governance/DECISIONS.md"

[projects]
required_docs = ["DECISIONS.md"]

[blocks]
project = [{{ file = "DECISIONS.md", id = "decision-index" }}]

[checks.decision-log]
statuses = ["locked", "provisional", "superseded"]
reasons = {{ statuses = "older entries predate the standard" }}
{extra}
"""

# The fixture's reason for re-allowing 'superseded'; a test takes it out to see a widening refused.
REASON_LINE = 'reasons = { statuses = "older entries predate the standard" }\n'

REGISTRY = """
[workspace]
id_prefix = "W"
id_range = "{ws_range}"

[[project]]
name = "alpha"
dir = "alpha"
tier = "full"
governance = "projects/alpha"
id_prefix = "A"
id_range = "{alpha_range}"
license = "MIT"
{alpha_extra}

[project.profile]
handoff = "projects/alpha/working-files/HANDOFF.md"
"""


def wtext(path: Path, content: str) -> None:
    """Exactly these characters, on every OS: UTF-8, no newline translation."""
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(content)


def fm(doc_type="control", **extra) -> str:
    lines = ["---", f"doc_type: {doc_type}", "purpose: test", "audience: agent",
             "load_when: testing", f"last_reviewed: {extra.pop('last_reviewed', TODAY)}"]
    lines += [f"{k}: {v}" for k, v in extra.items()]
    return "\n".join(lines + ["---", ""])


def entry(prefix: str, num: int, status="locked", sep=" — ", extra="") -> str:
    return (f"\n## {prefix}-{num}{sep}Title {num}\n\n**Status:** {status}\n\n"
            f"**Rule:** r.\n\n**Why:** w.\n{extra}")


class Workspace:
    """A minimal governance root: one workspace log and one governed project."""

    def __init__(self, tmp: Path, *, handoff_key="profile.handoff", ws_range="1-99",
                 alpha_range="100-199", alpha_extra="", extensions="", extra=""):
        self.root = tmp / "ws"
        self.home = tmp / "home"
        self.home.mkdir()
        self.write(CFG, CONFIG.format(version=__version__, handoff_key=handoff_key,
                                                    extensions=extensions, extra=extra))
        self.write("projects.toml", REGISTRY.format(ws_range=ws_range, alpha_range=alpha_range,
                                                    alpha_extra=alpha_extra))
        self.write("AGENTS.md", fm() + "# Agents\n")
        self.write("governance/DECISIONS.md", fm() + "# Decisions\n" + entry("W", 1))
        self.write("projects/alpha/DECISIONS.md",
                   fm() + "# Decisions\n\n<!-- t:generated:start id=decision-index -->\n"
                   "<!-- t:generated:end id=decision-index -->\n" + entry("A", 100))
        self.index()

    def write(self, rel: str, content: str | bytes) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
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


def installer_check(root: Path) -> tuple[int, str, str]:
    """`check`, against a project just installed with the real installer — `Workspace.run`
    without a `Workspace`, since installer tests build the root by hand."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main(["check"], root=root)
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def ws(self, **kw) -> Workspace:
        return Workspace(self.tmp, **kw)

    def _installed(self, w):
        """Install for real (the installer writes the entry point), under a throwaway HOME."""
        cfg = self.tmp / "config.toml"
        shutil.move(str(w.root / CFG), str(cfg))
        (w.root / layout.GOV_DIR).rmdir()
        env = {k: v for k, v in os.environ.items() if k != "GOVERN_ENGINE"}
        env["HOME"] = str(w.home)
        res = subprocess.run([sys.executable, "-m", "govern.installer", "install", "--root",
                              str(w.root), "--config", str(cfg)],
                             env={**env, "PYTHONPATH": str(ENGINE)}, capture_output=True,
                             text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        return w.root / layout.ENTRYPOINT, env

    def _engines(self, w, versions):
        # Each version once: at an X.Y.0 engine, `SERIES.0` and `__version__` are the same one.
        for version in dict.fromkeys(versions):
            shutil.copytree(ENGINE / "govern", layout.engines_dir(w.home) / version / "govern",
                            ignore=shutil.ignore_patterns("__pycache__"))

    def _which(self, entry, env) -> str:
        probe = ("import sys, runpy\n"
                 "try:\n"
                 f"    runpy.run_path({str(entry)!r}, run_name='__main__')\n"
                 "except SystemExit: pass\n"
                 "print([p for p in sys.path if 'engines' in p][0])")
        res = subprocess.run([sys.executable, "-c", probe], env=env, capture_output=True,
                             text=True)
        return res.stdout.strip()


def datetime_timestamp(d: date) -> float:
    from datetime import datetime
    return datetime(d.year, d.month, d.day).timestamp()


# Versions relative to this engine's, so a release bump never breaks the tests that need a newer
# or an older engine beside it.
_MAJOR, _MINOR = (int(x) for x in __version__.split(".")[:2])
SERIES = f"{_MAJOR}.{_MINOR}"
PREV_SERIES = f"{_MAJOR}.{_MINOR - 1}" if _MINOR else f"{_MAJOR - 1}.99"
NEXT_RELEASE = f"{_MAJOR}.{_MINOR + 1}.0"
BETA = f"{SERIES}.99-beta.1"


def tree_of(top: Path) -> dict:
    """Every path under top: a file's bytes, None for a directory; {} when top is missing."""
    return {p.relative_to(top).as_posix(): p.read_bytes() if p.is_file() else None
            for p in sorted(top.rglob("*"))} if top.is_dir() else {}


# The agent-roles option turned on, and where its rules file is rendered by default.
ROLES_ON = '\n[checks.agent-roles]\nlevel = "error"\n'
ROLES_RULES = ".claude/rules/context-gate-build.md"


def make_source(where: Path, tags: tuple) -> Path:
    """A git repository shaped like the engine's release source, tagged with each version (the
    engine's __version__ rewritten to match, as a real release would carry it)."""
    src = where / "source"
    git = lambda *a: subprocess.run(
        ["git", "-C", str(src), "-c", "user.email=t@t", "-c", "user.name=t",
         "-c", "tag.gpgSign=false", "-c", "commit.gpgSign=false", *a],
        check=True, capture_output=True)
    shutil.copytree(ENGINE / "govern", src / "engine" / "govern",
                    ignore=shutil.ignore_patterns("__pycache__"))
    git("init", "-q")
    init = src / "engine" / "govern" / "__init__.py"
    original = init.read_text(encoding="utf-8")
    for version in tags:
        wtext(init, original.replace(f'__version__ = "{__version__}"', f'__version__ = "{version}"'))
        git("add", "-A")
        git("commit", "-qm", version, "--allow-empty")
        git("tag", f"v{version}")
    return src


AGENT = ("---\nname: helper\ndescription: d\nmodel: opus\neffort: low\nomitClaudeMd: true\n"
         "{extra}---\n{body}\n")


BETA_PROJECT = ('\n[[project]]\nname = "beta"\ndir = "beta"\ntier = "full"\n'
                'governance = "projects/beta"\nid_prefix = "B"\nid_range = "700-799"\n')


__all__ = [
    "contextlib",
    "hashlib",
    "io",
    "json",
    "os",
    "shutil",
    "subprocess",
    "sys",
    "tempfile",
    "textwrap",
    "time",
    "unicodedata",
    "unittest",
    "date",
    "timedelta",
    "Path",
    "SimpleNamespace",
    "mock",
    "ENGINE",
    "REPO",
    "__version__",
    "cli",
    "config",
    "installer",
    "layout",
    "manifest",
    "notice",
    "ratchet",
    "registry",
    "text",
    "profile_rmtree",
    "CFG",
    "TODAY",
    "CONFIG",
    "REASON_LINE",
    "REGISTRY",
    "wtext",
    "fm",
    "entry",
    "Workspace",
    "installer_check",
    "Base",
    "datetime_timestamp",
    "_MAJOR",
    "_MINOR",
    "SERIES",
    "PREV_SERIES",
    "NEXT_RELEASE",
    "BETA",
    "tree_of",
    "ROLES_ON",
    "ROLES_RULES",
    "make_source",
    "AGENT",
    "BETA_PROJECT",
]
