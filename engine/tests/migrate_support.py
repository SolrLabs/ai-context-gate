"""`govern migrate`: moving a project's logs and traps from other common shapes onto the
standard mechanically.

Fixtures cover those shapes: sections grouping decisions one heading level too deep,
hand-written numbered trap indexes over bullet bodies, and a superseded decision that may or may
not name its successor.

    python3 -m unittest discover -s engine/tests -k migrate
"""
from __future__ import annotations

import contextlib
import io
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(ENGINE))

from govern import __version__, blocks, cli, config, installer, layout, migrate, registry  # noqa: E402
from govern.context import Context  # noqa: E402

CFG = layout.CONFIG

CONFIG = """
[governance]
engine = "{version}"
schema = 1

[dialect]
markers = "gv"

[registry]
file = "projects.toml"
entries = "project"

[workspace]
required_docs = []
docs = ["governance/*.md"]
decision_log = "governance/DECISIONS.md"

[projects]
required_docs = []
trap_glob = "*traps*.md"

[blocks]
project = [{{ file = "DECISIONS.md", id = "decision-index" }}{trap_block}]

[checks.decision-log]
statuses = ["locked", "provisional", "superseded"]
reasons = {{ statuses = "older entries predate the standard" }}
"""

TRAP_BLOCK = ', { glob = "working-files/*traps*.md", id = "trap-index" }'

# The block shape migrate's own report suggests for a hand-written index that spans several
# files: one file holds the marker pair, `sources` reads the rest.
TRAP_BLOCK_SOURCES = (', { file = "working-files/traps.md", id = "trap-index", '
                      'sources = "working-files/*traps*.md" }')

REGISTRY = """
[workspace]
id_prefix = "W"
id_range = "1-99"

[[project]]
name = "alpha"
dir = "alpha"
tier = "full"
governance = "projects/alpha"
id_prefix = "D"
id_range = "500-599"
"""

FM = ("---\ndoc_type: reference\npurpose: test\naudience: agent\nload_when: test\n"
     "last_reviewed: 2026-09-18\n---\n")

DECISIONS_INDEX = ("<!-- gv:generated:start id=decision-index -->\n"
                   "<!-- gv:generated:end id=decision-index -->\n")


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                   check=True, capture_output=True)


def wtext(path: Path, content: str, newline: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline=newline) as fh:
        fh.write(content)


class Fixture:
    """A minimal governance root whose logs use another shape than the standard, with
    its own git
    repo so the dirty-file refusal and the report's `git add`/`git commit` commands have
    something real to check."""

    def __init__(self, tmp: Path, *, decisions_text: str, trap_files: dict[str, str] | None = None,
                declare_trap_block: bool = True, trap_block: str = TRAP_BLOCK,
                git_repo: bool = True, engine_pin: str | None = None, bom: bool = False,
                registry_text: str | None = None) -> None:
        self.root = tmp / "root"
        self.home = tmp / "home"
        self.home.mkdir()
        self.root.mkdir()
        wtext(self.root / CFG, CONFIG.format(
            version=engine_pin or __version__, trap_block=trap_block if declare_trap_block else ""))
        wtext(self.root / "projects.toml", registry_text if registry_text is not None else REGISTRY)
        wtext(self.root / "governance/DECISIONS.md",
             FM + "# Decisions\n\n" + DECISIONS_INDEX + "\n## W-1 — Root decision\n\n"
             "**Status:** locked\n\n**Rule:** r.\n\n**Why:** w.\n")
        wtext(self.root / "projects/alpha/DECISIONS.md",
             ("﻿" + decisions_text) if bom else decisions_text)
        for name, content in (trap_files or {}).items():
            wtext(self.root / "projects/alpha/working-files" / name, content)
        if git_repo:
            git(self.root, "init", "-q")
            git(self.root, "add", "-A")
            git(self.root, "commit", "-qm", "initial")

    def log(self) -> Path:
        return self.root / "projects/alpha/DECISIONS.md"

    def trap(self, name: str) -> Path:
        return self.root / "projects/alpha/working-files" / name

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

    def run(self, apply_: bool) -> tuple[int, str]:
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = migrate.run(self.root, apply_)
        finally:
            if old_home is not None:
                os.environ["HOME"] = old_home
        return code, out.getvalue() + err.getvalue()

    def report(self) -> str:
        return (self.root / layout.GOV_DIR / "migration-report.md").read_text(encoding="utf-8")

    def commit(self) -> None:
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "migrate")

# ---------------------------------------------------------------------------- fixture text

SECTIONS_LOG = FM + "# Decisions\n\n" + DECISIONS_INDEX + """
## Server-side platform (D-500–D-599)

### D-500 — Use UTC timestamps

**Status:** locked

**Rule:** Store and compare times in UTC.

**Why:** One clock across machines.

### D-501 — Cache invalidation on deploy

**Status:** superseded by D-503 (2026-01-15: replaced by versioned keys)

**Rule:** old rule.

**Why:** old reason.

## Client tooling (D-520–D-599)

Some prose explaining the client tooling section, before any entry.

### D-520 — Code comments explain the code

**Status:** locked

**Rule:** r.

**Why:** w.

## D-503 — Versioned cache keys

**Status:** locked

**Rule:** new rule.

**Why:** new reason.

## D-104 — Log levels are set per module

**Status:** superseded

Superseded 2026-01-15 by a later design. No successor is named anywhere in this entry.

**Rule:** r.

**Why:** w.
"""

TRAPS_INDEX = FM + """
# Traps

## Index

1. A committed text fixture is LF, whatever the machine that wrote it had
2. A byte-identical restore is not enough — touch it
3. Never cache an error response — body in [platform-traps.md](platform-traps.md)
49. Retired 2026-01-15 — the check moved into CI

---

- **1. A committed text fixture is LF, whatever the machine wrote it with.** Normalise before counting, or assert something other than length.

- **2. A byte-identical restore is not enough — touch it.** Restoring a file with an older mtime leaves the stale build output in place.
"""

PLATFORM_TRAPS = FM + """
# Platform traps

- **3. Never cache an error response.** The error is served until the entry expires.
"""


def base_fixture(tmp: Path, **kw) -> Fixture:
    return Fixture(tmp, decisions_text=SECTIONS_LOG,
                  trap_files={"traps.md": TRAPS_INDEX, "platform-traps.md": PLATFORM_TRAPS},
                  **kw)

# ---------------------------------------------------------------------------- more fixture text

FENCED_LOG = FM + "# Decisions\n\n" + DECISIONS_INDEX + """
Documentation example (not real data):

```
## Old Section (D-900-D-999)

### D-900 — Example

**Topic:** Old Section
```

## D-600 — Real decision

**Status:** locked

**Rule:** r.

**Why:** w.
"""

FENCED_TRAPS = FM + """
# Traps

Documentation example (not real data):

```
## Index

1. Example item

- **7. Example only.** Body of an example that should not migrate.
```

## T-10 — Real trap

**Bites when:** always.

Body.
"""

AMBIGUOUS_SUCCESSOR_LOG = FM + "# Decisions\n\n" + DECISIONS_INDEX + """
## D-504 — Status line picks the right successor

**Status:** superseded by D-506

**Rule:** This replaced D-400, which was superseded by D-500 in turn. Now superseded by D-501.

**Why:** w.

## D-505 — Ambiguous successor is left for a person

**Status:** superseded

**Rule:** This replaced D-400, which was superseded by D-500 in turn. Now superseded by D-501.

**Why:** w.

## D-506 — Where D-504 went

**Status:** locked

**Rule:** r.

**Why:** w.
"""

MIDSENTENCE_TRAPS = FM + """
# Traps

## Index

1. First trap
2. Environment example

- **1. First trap.** Body one.
- **2. Environment variable expansion does not contain a space**, and on Unix systems the missing quoting still executes.
"""

BARE_EOF_LOG = (FM + "# Decisions\n\n" + DECISIONS_INDEX +
               "\n## Trailing Topic (D-700-D-799)\n\n### D-700 — Bare heading with nothing after it")

INDEX_TABLE_TRAPS = FM + """
# Traps

## Index

| # | Trap |
|---|---|
| 1 | Example |

- **1. Example.** Body.
"""

INDEX_BULLETS_TRAPS = FM + """
# Traps

## Index

- 1. First trap
- 2. Second trap

- **1. First trap.** Body.

- **2. Second trap.** Body.
"""

INDEX_WRAPPED_TRAPS = FM + """
# Traps

## Index

1. First trap
   continues on a wrapped line
2. Second trap

- **1. First trap.** Body.

- **2. Second trap.** Body.
"""

DUP_A = FM + "# Traps A\n\n- **21. Version check race.** Body A.\n"

DUP_B = FM + "# Traps B\n\n- **21. Version check race, different body.** Body B.\n"


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

# ---------------------------------------------------------------------------- edge cases

POINTER_IN_SECTION_LOG = FM + "# Decisions\n\n" + DECISIONS_INDEX + """
## Server (D-500–D-599)

### D-500 — Kept

**Status:** locked

**Rule:** r.

**Why:** w.

### D-501 — Replaced by D-500

### D-502 — Last one

**Status:** locked

**Rule:** r.

**Why:** w.
"""

BULLET_CONFLICTS_TRAPS = {
    "traps.md": FM + "# Traps\n\n## T-3 — Already a heading\n\n**Bites when:** x.\n\nBody.\n\n"
               "- **4. New bullet.** body four.\n",
    "platform-traps.md": FM + "# Platform traps\n\n"
                        "- **3. Same number as a heading elsewhere.** body.\n",
}

TWO_SCOPE_REGISTRY = REGISTRY + (
    '\n[[project]]\nname = "beta"\ndir = "beta"\ntier = "full"\n'
    'governance = "projects/beta"\nid_prefix = "D"\nid_range = "600-699"\n')

PERIOD_TITLE_TRAPS = FM + """
# Traps

- **7. Never force-push.** git rewrites history other clones already have.
"""

SHARED_TRAPS_WITH_INDEX = FM + """
# Traps

## Index

1. New bullet

---

- **1. New bullet.** Body one.
"""

OVER_LIMIT_LOG = (FM + "# Decisions\n\n" + DECISIONS_INDEX +
                  "\n## Server-side platform (D-500–D-599)\n\n"
                  "### D-500 — Verbose decision\n\n**Status:** locked\n\n**Rule:** r.\n\n"
                  "**Why:** " + " ".join(["word"] * 260) + ".\n")


def hyphens(text: str) -> str:
    """A fixture's text with every ` — ` written ` - `: the hyphen heading form."""
    return text.replace(" — ", " - ")

SECTIONS_ONLY_LOG = FM + "# Decisions\n\n" + DECISIONS_INDEX + """
## Server-side platform (D-500–D-599)

### D-500 — Use UTC timestamps

**Status:** locked

**Rule:** Store and compare times in UTC.

**Why:** One clock across machines.
"""

EMPTY_LOG = FM + "# Decisions\n\n" + DECISIONS_INDEX

__all__ = [
    "contextlib",
    "io",
    "os",
    "shlex",
    "subprocess",
    "sys",
    "tempfile",
    "unittest",
    "Path",
    "mock",
    "ENGINE",
    "__version__",
    "blocks",
    "cli",
    "config",
    "installer",
    "layout",
    "migrate",
    "registry",
    "Context",
    "CFG",
    "CONFIG",
    "TRAP_BLOCK",
    "TRAP_BLOCK_SOURCES",
    "REGISTRY",
    "FM",
    "DECISIONS_INDEX",
    "git",
    "wtext",
    "Fixture",
    "SECTIONS_LOG",
    "TRAPS_INDEX",
    "PLATFORM_TRAPS",
    "base_fixture",
    "FENCED_LOG",
    "FENCED_TRAPS",
    "AMBIGUOUS_SUCCESSOR_LOG",
    "MIDSENTENCE_TRAPS",
    "BARE_EOF_LOG",
    "INDEX_TABLE_TRAPS",
    "INDEX_BULLETS_TRAPS",
    "INDEX_WRAPPED_TRAPS",
    "DUP_A",
    "DUP_B",
    "Base",
    "POINTER_IN_SECTION_LOG",
    "BULLET_CONFLICTS_TRAPS",
    "TWO_SCOPE_REGISTRY",
    "PERIOD_TITLE_TRAPS",
    "SHARED_TRAPS_WITH_INDEX",
    "OVER_LIMIT_LOG",
    "hyphens",
    "SECTIONS_ONLY_LOG",
    "EMPTY_LOG",
]
