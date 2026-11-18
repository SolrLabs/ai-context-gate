"""Session start shows what teammates changed since the last session, for a team only.

    python3 -m unittest discover -s engine/tests -k test_seen_hook
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import profile_guard  # noqa: E402,F401

from govern import refs  # noqa: E402
from base_support import LOG, Tree, decision  # noqa: E402
from changes_support import cut  # noqa: E402
from team_support import TwoClones  # noqa: E402

PLUGIN = ENGINE.parent / "plugin"
SAID = "since your last session: P-1 superseded (b)"


class SeenHook(TwoClones):
    def setUp(self) -> None:
        super().setUp()
        self.built = self.tmp / "built"
        shutil.copytree(PLUGIN, self.built)
        shutil.copytree(ENGINE / "govern", self.built / "govern",
                        ignore=shutil.ignore_patterns("__pycache__"))

    def now_commit(self, clone: Path, email: str, msg: str = "now") -> None:
        now = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
        res = subprocess.run(
            ["git", "-C", str(clone), "-c", f"user.email={email}", "-c", "user.name=x",
             "-c", "commit.gpgSign=false", "commit", "--allow-empty", "-qm", msg],
            capture_output=True, cwd=self.tmp,
            env=self.git_env(GIT_AUTHOR_DATE=now, GIT_COMMITTER_DATE=now))
        self.assertEqual(res.returncode, 0, res.stderr)

    def start(self) -> list[str]:
        env = {**os.environ, "HOME": str(self.seed.home), "CLAUDE_PROJECT_DIR": str(self.a),
               "CONTEXT_GATE_NO_UPDATE_CHECK": "1", "GIT_CONFIG_NOSYSTEM": "1"}
        res = subprocess.run(
            [sys.executable, str(self.built / "hooks" / "session_start.py")],
            input=json.dumps({"session_id": "S", "hook_event_name": "SessionStart"}),
            env=env, capture_output=True, text=True, cwd=self.tmp)
        self.assertEqual((res.returncode, res.stderr), (0, ""))
        return json.loads(res.stdout)["systemMessage"].splitlines() if res.stdout.strip() else []

    def b_supersedes(self) -> None:
        t = Tree(self.b, self.seed.home)
        cut(t, LOG, "P-1")
        t.append(LOG, "\n## P-1 - Replaced by P-4\n" + decision("P", 4, title="The new rule"))
        self.seed.git("add", "-A", at=self.b)
        res = subprocess.run(
            ["git", "-C", str(self.b), "-c", "commit.gpgSign=false", "commit", "-qm", "b"],
            capture_output=True, cwd=self.tmp,
            env=self.git_env(GIT_AUTHOR_EMAIL="b@example.com", GIT_COMMITTER_EMAIL="b@example.com",
                             GIT_AUTHOR_NAME="x", GIT_COMMITTER_NAME="x"))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.push(self.b)
        self.fetch(self.a)

    def test_a_team_is_told_what_changed(self):
        self.now_commit(self.a, "a@example.com")
        self.now_commit(self.a, "c@example.com")
        self.assertGreater(refs.team_size(self.a), 1)
        self.assertNotIn(SAID, self.start())
        self.b_supersedes()
        self.assertIn(SAID, self.start())
        self.assertNotIn(SAID, self.start())

    def collide(self) -> None:
        """B and A each add P-7 on a branch of their own, with different titles; A holds B's."""
        for clone, title, who in ((self.b, "Retry rule", "b"), (self.a, "Cache rule", "a")):
            self.seed.git("checkout", "-q", "-b", f"{who}-work", at=clone)
            Tree(clone, self.seed.home).append(LOG, decision("P", 7, title=title))
            self.seed.git("add", "-A", at=clone)
            self.now_commit(clone, f"{who}@example.com", f"{who} adds P-7")
            if clone == self.b:
                self.push(self.b)
        self.fetch(self.a)

    def test_seen_lines_follow_sibling_lines(self):
        self.now_commit(self.a, "a@example.com")
        self.now_commit(self.a, "c@example.com")
        self.start()
        self.b_supersedes()
        self.collide()
        got = self.start()
        sibling = [i for i, line in enumerate(got) if "P-7 is also added" in line]
        said = [i for i, line in enumerate(got) if line == SAID]
        self.assertTrue(sibling and said, got)
        self.assertLess(max(sibling), min(said))

    def run_hook(self, patch: str) -> Path:
        """Run `hooks/siblings.py` with `patch` (Python run before it) in a child process."""
        out = self.tmp / "lines.json"
        code = (f"import runpy, sys\nsys.path.insert(0, {str(self.built)!r})\n{patch}\n"
                f"sys.argv = ['siblings.py', {str(self.a)!r}, {str(out)!r}]\n"
                f"runpy.run_path({str(self.built / 'hooks' / 'siblings.py')!r}, "
                f"run_name='__main__')\n")
        env = {**os.environ, "HOME": str(self.seed.home), "GIT_CONFIG_NOSYSTEM": "1",
               "CONTEXT_GATE_NO_UPDATE_CHECK": "1"}
        subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                       cwd=self.tmp)
        return out

    def test_sibling_lines_survive_a_failing_seen(self):
        self.now_commit(self.a, "a@example.com")
        self.now_commit(self.a, "c@example.com")
        self.collide()
        patch = ("from govern import seen\n"
                 "def boom(ctx):\n    raise SystemExit(3)\n"
                 "seen.lines = boom\n")
        out = self.run_hook(patch)
        self.assertTrue(out.exists())
        lines = json.loads(out.read_text(encoding="utf-8"))
        self.assertTrue(any("P-7 is also added" in line for line in lines), lines)

    def test_one_author_is_told_nothing(self):
        self.now_commit(self.a, "a@example.com")
        self.assertEqual(refs.team_size(self.a), 1)
        self.start()
        self.b_supersedes()
        self.assertNotIn("since your last session", "\n".join(self.start()))


if __name__ == "__main__":
    unittest.main()
