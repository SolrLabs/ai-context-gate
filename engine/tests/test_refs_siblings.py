"""Sibling collisions: an id this branch added that another fetched branch also added with a
different title, in `govern check --siblings` and at session start for a team.

    python3 -m unittest discover -s engine/tests -k test_refs_siblings
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
from unittest import mock

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import profile_guard  # noqa: E402,F401

from govern import refs  # noqa: E402
from base_support import LOG, Case, Tree, decision, wtext  # noqa: E402
from team_support import TwoClones  # noqa: E402

PLUGIN = ENGINE.parent / "plugin"
LINE = ('docs/DECISIONS.md: P-4 is also added on origin/b-work ("Retry rule"); '
        'renumber one with govern renumber')


class Siblings(TwoClones):
    def now_commit(self, clone: Path, msg: str, email: str) -> None:
        """A commit dated now, so `team_size` (90 days) counts its author."""
        now = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
        self.seed.git("add", "-A", at=clone)
        res = subprocess.run(
            ["git", "-C", str(clone), "-c", f"user.email={email}", "-c", "user.name=x",
             "-c", "commit.gpgSign=false", "commit", "--allow-empty", "-qm", msg],
            capture_output=True, env=self.git_env(GIT_AUTHOR_DATE=now, GIT_COMMITTER_DATE=now))
        self.assertEqual(res.returncode, 0, res.stderr)

    def add(self, clone: Path, branch: str, title: str, email: str, num: int = 4) -> None:
        self.seed.git("checkout", "-q", "-b", branch, at=clone)
        path = clone / LOG
        wtext(path, path.read_text(encoding="utf-8") + decision("P", num, title=title))
        self.now_commit(clone, f"add {title}", email)

    def collide(self, a_title: str = "Cache rule", b_title: str = "Retry rule") -> None:
        self.add(self.b, "b-work", b_title, "b@example.com")
        self.push(self.b)
        self.add(self.a, "a-work", a_title, "a@example.com")
        self.fetch(self.a)

    def tree(self, clone: Path) -> Tree:
        return Tree(clone, self.seed.home)

    def test_a_collision_is_named(self):
        self.collide()
        self.assertEqual(refs.sibling_collisions(self.tree(self.a).context()), [LINE])

    def test_the_same_entry_on_both_is_silent(self):
        self.collide(a_title="Retry rule")
        self.assertEqual(refs.sibling_collisions(self.tree(self.a).context()), [])

    def test_an_unfetched_branch_is_not_seen(self):
        self.add(self.b, "b-work", "Retry rule", "b@example.com")
        self.push(self.b)
        self.add(self.a, "a-work", "Cache rule", "a@example.com")
        self.assertEqual(refs.sibling_collisions(self.tree(self.a).context()), [])

    def test_check_siblings_warns_and_keeps_the_exit_code(self):
        self.collide()
        w = self.tree(self.a)
        plain = w.run("check")
        code, out, err = w.run("check", "--siblings")
        self.assertEqual((code, err), (plain[0], plain[2]))
        self.assertIn(f"  warn   siblings: {LINE}", out.splitlines())
        self.assertNotIn("siblings:", plain[1])


class MissingRemote(Case):
    def test_a_missing_remote_does_not_crash_check_siblings(self):
        w = self.solo()
        w.git("remote", "add", "origin", str(self.tmp / "nowhere"))
        plain = w.run("check")
        code, out, err = w.run("check", "--siblings")
        self.assertNotIn("Traceback", err)
        self.assertEqual((code, err), (plain[0], plain[2]))


class NoNetwork(Siblings):
    """`sibling_collisions` reads local refs only: it never asks a remote for its default."""

    def test_no_remote_head_runs_no_ls_remote(self):
        self.collide()
        self.seed.git("remote", "set-head", "origin", "--delete", at=self.a)
        real = subprocess.run
        asked: list[list] = []

        def spy(args, *a, **kw):
            asked.append(list(args))
            if "ls-remote" in args:
                raise subprocess.SubprocessError("no network")
            return real(args, *a, **kw)

        with mock.patch("subprocess.run", spy):
            got = refs.sibling_collisions(self.tree(self.a).context())
        self.assertFalse([a for a in asked if "ls-remote" in a])
        self.assertIn(got, ([], [LINE]))


class FetchWarning(Siblings):
    def test_an_unreachable_remote_names_the_cause(self):
        self.seed.git("remote", "set-url", "origin", str(self.tmp / "nowhere"), at=self.a)
        why = refs.fetch(self.a)
        self.assertIn("does not appear to be a git repository", why or "")


class SessionStart(Siblings):
    def setUp(self) -> None:
        super().setUp()
        self.built = self.tmp / "built"
        shutil.copytree(PLUGIN, self.built)
        shutil.copytree(ENGINE / "govern", self.built / "govern",
                        ignore=shutil.ignore_patterns("__pycache__"))
        self.sib = (self.seed.home / ".local" / "state" / "context-gate" / "agents"
                    / "S.siblings.json")

    def start(self) -> str:
        env = {**os.environ, "HOME": str(self.seed.home), "CLAUDE_PROJECT_DIR": str(self.a),
               "CONTEXT_GATE_NO_UPDATE_CHECK": "1", "GIT_CONFIG_NOSYSTEM": "1"}
        res = subprocess.run(
            [sys.executable, str(self.built / "hooks" / "session_start.py")],
            input=json.dumps({"session_id": "S", "hook_event_name": "SessionStart"}),
            env=env, capture_output=True, text=True, cwd=self.tmp)
        self.assertEqual((res.returncode, res.stderr), (0, ""))
        return res.stdout

    def test_session_start_shows_collisions_for_a_team(self):
        self.collide()
        self.now_commit(self.a, "empty", "c@example.com")
        self.assertGreater(refs.team_size(self.a), 1)
        said = json.loads(self.start())["systemMessage"].splitlines()
        self.assertIn(LINE, said)
        self.assertFalse(self.sib.exists())

    def test_solo_runs_no_child(self):
        self.seed.git("checkout", "-q", "-b", "b-work", at=self.b)
        wtext(self.b / LOG, (self.b / LOG).read_text(encoding="utf-8")
              + decision("P", 4, title="Retry rule"))
        self.now_commit(self.b, "b", "a@example.com")
        self.push(self.b)
        self.add(self.a, "a-work", "Cache rule", "a@example.com")
        self.fetch(self.a)
        # fixture history is dated 2020, so the only author in the window is A
        self.assertEqual(refs.team_size(self.a), 1)
        out = self.start()
        self.assertNotIn("also added", out)
        self.assertEqual(out, "")
        self.assertFalse(self.sib.exists())


if __name__ == "__main__":
    unittest.main()
