"""Fix pass 2 of the b19 field work: the single-repo `--path` run reads only snapshot-aware
root checks, the plan's unit list ignores fenced and commented lines, adopt's loose headings
count only in a decision-named file, a retried unit whose branch is behind HEAD is not merged,
and `usage install` does not say a kept capture.py was refreshed.

    python3 -m unittest discover -s engine/tests -k test_field_fixes
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import profile_guard  # noqa: E402,F401

from govern import __version__, build, measure, usage_setup  # noqa: E402

import migrate_support as ms  # noqa: E402
from build_support import Build  # noqa: E402
from test_build_launch_base import Launching  # noqa: E402
from test_single_repo_path import Base  # noqa: E402

BODY = "\n**Status:** locked\n\n**Rule:** x.\n\n**Why:** y.\n"


class SinglePathRootChecks(Base):
    def test_a_working_tree_problem_outside_the_copy_does_not_fail_the_copy(self):
        r = self.repo("# Notes\n\nPlain words.\n")
        snap = self.copy(r)
        # Only the working tree has it; the copy (the staged tree) is sound.
        r.write(".claude/agents/bad.md", "no frontmatter at all\n")
        code, plain, _ = r.run("check")
        self.assertEqual(code, 1, plain)
        code, out, _ = r.run("check", "--project", "myproject", "--path", str(snap))
        self.assertEqual(code, 0, out)
        self.assertNotIn("bad.md", out)
        code, out, _ = r.run("check", "--project", "myproject")
        self.assertEqual(code, 1, out)


PLAN = """# Plan

## Phase 1: one (slug: p1; high-risk: no)

### u1: First  (developer)

```
## Phase 2: fenced (slug: other)
### u9: fenced
```

<!--
### u8: commented
-->

### u2: Second  (developer)

## Release 1.0 (coordinator)

### u99: After
"""


class PlanMasking(Build):
    def planned(self, text: str):
        w = self.project()
        w.write("docs/plan.md", text)
        w.commit("plan", at=w.top, index=False)
        self.ok("start", "p1", "--plan", "docs/plan.md")
        led = build.load(self.dir() / build.LEDGER)
        return build.planned_units(self.w.root, led)

    def test_fenced_and_commented_headings_are_not_counted_or_ending_the_phase(self):
        self.assertEqual(self.planned(PLAN), ["u1", "u2"])


class LooseNeedsADecisionFile(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def measured(self, rel: str, text: str):
        root = self.tmp / "repo"
        ms.wtext(root / rel, text)
        ms.git(root, "init", "-q")
        ms.git(root, "add", "-A")
        ms.git(root, "commit", "-qm", "initial")
        return measure.measure(root)

    def test_a_roadmap_with_quarter_headings_is_not_a_decision_log(self):
        text = ms.FM + "# Roadmap\n\n" + "".join(f"## Q{n}: Goals\n\nWords.\n\n" for n in (1, 2, 3, 4))
        m = self.measured("docs/roadmap.md", text)
        self.assertEqual(m.workspace.decision_logs, [])

    def test_a_file_named_for_decisions_still_counts_loose_headings(self):
        text = ms.FM + "# Log\n\n" + "".join(f"## X{n}: Title\n{BODY}\n" for n in (1, 2))
        m = self.measured("docs/decision-log.md", text)
        self.assertEqual([(d.entries, d.loose) for d in m.workspace.decision_logs], [(2, 2)])


class RetryBehind(Launching):
    def test_a_branch_behind_head_is_not_merged_and_its_ref_stays(self):
        self.started()
        self.ready()
        self.ok("launch", "u1")
        self.w.git("branch", "u1-try")
        tip = self.head("u1-try")
        self.ok("unit", "set", "u1", "failed", "--branch", "u1-try")
        self.code("src/later.py", "phase moved on\n")
        head = self.head()
        out = self.ok("launch", "u1")
        lines = out.splitlines()
        self.assertIn(f"Base: {head}", lines)
        self.assertTrue(any(line.startswith("Phase branch: ") for line in lines), out)
        self.assertEqual(self.head("u1-try"), tip)


class KeptThenRefreshed(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.profile = Path(self._tmp.name) / "profile"
        self.profile.mkdir()
        env = mock.patch.dict(os.environ, {"HOME": str(self.home), "CLAUDE_CONFIG_DIR": str(self.profile)})
        env.start()
        self.addCleanup(env.stop)
        self.path = usage_setup.capture_path(self.home)

    def plant(self, version: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(f'"""old"""\n# context-gate capture {version}\nx = 1\n', encoding="utf-8")

    def test_a_kept_capture_is_not_called_refreshed(self):
        self.plant("9.9.9")
        usage_setup.install(self.home, python="/usr/bin/python3")
        msg = usage_setup.install(self.home, python="/usr/bin/python3")
        self.assertEqual(msg, f"capture.py is from 9.9.9, newer than this engine ({__version__}): "
                              "kept; usage capture already installed")

    def test_a_kept_capture_with_a_moved_interpreter_names_only_the_interpreter(self):
        self.plant("9.9.9")
        usage_setup.install(self.home, python="/usr/bin/python3")
        msg = usage_setup.install(self.home, python="/opt/other/python3")
        self.assertEqual(msg, f"capture.py is from 9.9.9, newer than this engine ({__version__}): "
                              "kept; usage capture already installed; the interpreter path refreshed")

    def test_a_replaced_capture_is_still_called_refreshed(self):
        self.plant("0.0.1")
        usage_setup.install(self.home, python="/usr/bin/python3")
        msg = usage_setup.install(self.home, python="/usr/bin/python3")
        self.assertEqual(msg, "usage capture already installed; capture.py refreshed")
        self.plant("0.0.1")
        msg = usage_setup.install(self.home, python="/opt/other/python3")
        self.assertEqual(msg, "usage capture already installed; capture.py and the interpreter "
                              "path refreshed")


if __name__ == "__main__":
    unittest.main()
