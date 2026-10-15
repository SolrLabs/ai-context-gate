"""`govern build`: the ledger of the phase a branch has in flight, its briefs and its next action.
Against real repositories in temporary directories with a HOME of their own (the fixtures
`base_support` builds).

`build status --json` is a contract: its keys are held byte for byte in `expected/`. After a
deliberate change, regenerate with

    GOVERN_UPDATE_EXPECTED=1 python3 -m unittest discover -s engine/tests -k ExpectedStatus

and review the difference like any other change.

    python3 -m unittest discover -s engine/tests -k test_build
"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from govern import agent_runs, build, layout, phase, roles  # noqa: E402
from base_support import Case, Solo  # noqa: E402

EXPECTED = Path(__file__).resolve().parent / "expected"
BUILD = Path(layout.GOV_DIR) / "build"
GATES = {"briefed": ["developer", "senior-developer"], "claims": True, "deny_push": True,
         "max_refusals": 2}
RELATIVE = "Paths on the lines below are from the repository root, in your own working tree."


def at(folder: str, name: str) -> str:
    """A contract path as a brief writes it: from the repository root."""
    return f"{BUILD.as_posix()}/p1/{folder}/{name}"
GOVERN = roles.GOVERN
VISUAL_ON = '\n[checks.agent-roles]\nlevel = "warn"\nvisual_paths = ["web/**"]\n'
MEASURE_ON = '\n[checks.agent-measure]\nlevel = "warn"\n'


def then(what: str, brief: str = "the brief") -> str:
    """The last line of every command that writes a brief."""
    return f"fill in {brief}, commit it, then run `{GOVERN} build launch {what}`"


def ledger(folder: Path, *units: tuple, stage: str = "build", **more) -> build.Ledger:
    """A ledger in memory: each unit is (id, status) or (id, status, note)."""
    rows = [{"id": u[0], "title": f"Unit {u[0]}", "role": "developer", "status": u[1],
             "attempts": 0, "branch": "", "commit": "", "note": u[2] if len(u) > 2 else ""}
            for u in units]
    return build.Ledger(path=folder / "p1" / build.LEDGER, phase="p1", branch="phase/p1",
                        base="0" * 40, stage=stage, units=rows, **more)


class Build(Case):
    """A single repo on `phase/p1`, cut from `main`. With `remote`, `origin` is a bare
    repository holding the default branch."""

    def project(self, *, remote: bool = False, default: str = "main", branch: str = "phase/p1",
                extra: str = "", below: str | None = None) -> Solo:
        w = Solo(self.tmp, extra=extra, below=below)
        if default != "main":
            w.git("branch", "-m", "main", default)
        if remote:
            bare = self.tmp / "remote.git"
            bare.mkdir()
            w.git("init", "-q", "--bare", at=bare)
            w.git("symbolic-ref", "HEAD", f"refs/heads/{default}", at=bare)
            w.git("remote", "add", "origin", str(bare))
            w.git("push", "-q", "origin", default)
            w.git("remote", "set-head", "origin", default)
        if branch:
            w.branch(branch)
        self.w = w
        return w

    def go(self, *argv: str) -> tuple[int, str, str]:
        return self.w.run("build", *argv)

    def ok(self, *argv: str) -> str:
        code, out, err = self.go(*argv)
        self.assertEqual((code, err), (0, ""), out)
        return out

    def files(self) -> dict[str, bytes]:
        top = self.w.root / BUILD
        return {p.relative_to(top).as_posix(): p.read_bytes()
                for p in sorted(top.rglob("*")) if p.is_file()}

    def refused(self, message: str, *argv: str) -> str:
        before = self.files()
        code, out, err = self.go(*argv)
        self.assertEqual(code, 1, out + err)
        self.assertTrue(err.startswith("error: "), err)
        self.assertIn(message, err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(self.files(), before)
        return err

    def dir(self, name: str = "p1") -> Path:
        return self.w.root / BUILD / name

    def ledger(self, name: str = "p1") -> dict:
        return tomllib.loads((self.dir(name) / build.LEDGER).read_text(encoding="utf-8"))

    def head(self, ref: str = "HEAD") -> str:
        return self.w.git("rev-parse", ref).strip()

    def started(self, *flags: str, **kw) -> Solo:
        w = self.project(**kw)
        self.ok("start", "p1", *flags)
        return w

    def unit(self, uid: str = "u1", status: str | None = None, *flags: str) -> Path:
        self.ok("unit", "add", uid, "--title", f"Unit {uid}", *flags)
        if status:
            self.ok("unit", "set", uid, status)
        return self.dir() / uid / "brief.md"

    def code(self, name: str = "src/a.py", text: str = "print(1)\n") -> None:
        """A commit on the phase branch, the ledger and the briefs with it."""
        self.w.write(name, text)
        self.w.commit(name, at=self.w.top, index=False)


__all__ = [
    "json",
    "os",
    "stat",
    "sys",
    "tempfile",
    "tomllib",
    "unittest",
    "Path",
    "mock",
    "ENGINE",
    "agent_runs",
    "build",
    "layout",
    "phase",
    "roles",
    "Case",
    "Solo",
    "EXPECTED",
    "BUILD",
    "GATES",
    "RELATIVE",
    "at",
    "GOVERN",
    "VISUAL_ON",
    "MEASURE_ON",
    "then",
    "ledger",
    "Build",
]
