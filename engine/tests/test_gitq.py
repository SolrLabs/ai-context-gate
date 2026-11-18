"""`gitq`: read-only git lookups asked once per scope, never cached outside one.

    python3 -m unittest discover -s engine/tests -p test_gitq.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import profile_guard  # noqa: E402,F401
import base_support  # noqa: E402,F401  (puts the engine on the path)

from govern import build, gitq, phase  # noqa: E402


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


class GitQ(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve() / "repo"
        self.root.mkdir()
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.email", "t@example.com")
        git(self.root, "config", "user.name", "t")
        git(self.root, "commit", "-q", "--allow-empty", "-m", "first")
        self.calls: list[tuple] = []
        real_git = phase._git   # `_run` and `_ask` end here: every git call is counted once

        def counting_git(repo, *args, **kw):
            self.calls.append(args)
            return real_git(repo, *args, **kw)
        patch = mock.patch.object(phase, "_git", counting_git)
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_lookup_is_asked_once_in_a_scope(self):
        with gitq.scope():
            first = gitq.toplevel(self.root)
            second = gitq.toplevel(self.root)
        self.assertIs(first, second)
        self.assertEqual(len(self.calls), 1)

    def test_each_scope_asks_again(self):
        with gitq.scope():
            gitq.toplevel(self.root)
        with gitq.scope():
            gitq.toplevel(self.root)
        self.assertEqual(len(self.calls), 2)

    def test_outside_a_scope_nothing_is_cached(self):
        gitq.toplevel(self.root)
        gitq.toplevel(self.root)
        self.assertEqual(len(self.calls), 2)

    def test_a_nested_scope_does_not_clear(self):
        with gitq.scope():
            gitq.toplevel(self.root)
            with gitq.scope():
                gitq.toplevel(self.root)
            gitq.toplevel(self.root)
        self.assertEqual(len(self.calls), 1)

    def test_config_set_drops_that_key(self):
        with gitq.scope():
            self.assertEqual(gitq.config_get(self.root, "gitq.one").returncode, 1)
            self.assertEqual(gitq.config_get(self.root, "gitq.two").returncode, 1)
            self.assertEqual(gitq.config_set(self.root, "gitq.one", "v").returncode, 0)
            res = gitq.config_get(self.root, "gitq.one")
            self.assertEqual(phase._out(res), "v")
            before = len(self.calls)
            self.assertEqual(gitq.config_get(self.root, "gitq.two").returncode, 1)
            self.assertEqual(len(self.calls), before)

    def test_an_error_is_not_cached(self):
        bare = self.root.parent / "plain"
        bare.mkdir()
        with gitq.scope():
            for _ in range(2):
                with self.assertRaises(phase.PhaseError):
                    gitq.remotes(bare)
        self.assertEqual(len(self.calls), 2)

    def test_head_is_never_cached(self):
        with gitq.scope():
            self.assertEqual(build.checked_out(self.root), "main")
            git(self.root, "switch", "-q", "-c", "other")
            self.assertEqual(build.checked_out(self.root), "other")

    def test_a_failing_answer_is_cached_in_a_scope(self):
        plain = self.root.parent / "plain"
        plain.mkdir()
        for ask in (gitq.toplevel, gitq.git_dir_ok):
            with self.subTest(ask=ask.__name__):
                self.calls.clear()
                with gitq.scope():
                    first, second = ask(plain), ask(plain)
                self.assertEqual(first, second)
                self.assertEqual(len(self.calls), 1)

    def test_a_default_branch_error_is_not_cached(self):
        plain = self.root.parent / "plain"
        plain.mkdir()
        with gitq.scope():
            with self.assertRaises(phase.PhaseError):
                gitq.default_branch(plain, "origin")
            once = len(self.calls)
            with self.assertRaises(phase.PhaseError):
                gitq.default_branch(plain, "origin")
        self.assertGreater(once, 0)
        self.assertEqual(len(self.calls), 2 * once)

    def test_toplevel_text_raises_outside_a_repository(self):
        plain = self.root.parent / "plain"
        plain.mkdir()
        with self.assertRaises(phase.PhaseError):
            gitq.toplevel_text(plain)


class MainScope(base_support.Case):
    def test_cli_main_asks_a_lookup_once_per_command(self):
        w = self.solo()
        calls: list[tuple] = []
        real_git = phase._git
        real_check = base_support.manifest.CHECKS[base_support.CHECK]

        def counting_git(repo, *args, **kw):
            calls.append(args)
            return real_git(repo, *args, **kw)

        def asks_twice(*args, **kw):
            gitq.toplevel(w.root)
            gitq.toplevel(w.root)
            return real_check.fn(*args, **kw)

        with mock.patch.object(phase, "_git", counting_git), mock.patch.dict(
                base_support.manifest.CHECKS, {base_support.CHECK: base_support.dataclasses.replace(
                    real_check, fn=asks_twice)}):
            code, out, err = w.run("check")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(calls.count(("rev-parse", "--show-toplevel")), 1)


if __name__ == "__main__":
    unittest.main()
