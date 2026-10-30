"""`fixture_cache.copy_of`: a tree built once per key, copied for each caller, refused when it
names the directory it was built in.

    python3 -m unittest discover -s engine/tests -k fixture_cache
"""
from __future__ import annotations

import itertools
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import profile_guard  # noqa: E402,F401

from fixture_cache import copy_of  # noqa: E402

_KEYS = itertools.count()


class CopyOf(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.key = ("test_fixture_cache", next(_KEYS))

    def test_build_runs_once_per_key(self) -> None:
        calls = []

        def build(template: Path) -> None:
            calls.append(template)
            (template / "a.txt").write_text("built", encoding="utf-8")

        copy_of(self.key, self.tmp / "one", build)
        copy_of(self.key, self.tmp / "two", build)
        self.assertEqual(len(calls), 1)
        for name in ("one", "two"):
            self.assertEqual((self.tmp / name / "a.txt").read_text(encoding="utf-8"), "built")

    def test_copies_are_independent(self) -> None:
        template = []

        def build(path: Path) -> None:
            template.append(path)
            (path / "a.txt").write_text("built", encoding="utf-8")

        copy_of(self.key, self.tmp / "one", build)
        copy_of(self.key, self.tmp / "two", build)
        (self.tmp / "one" / "a.txt").write_text("changed", encoding="utf-8")
        self.assertEqual((self.tmp / "two" / "a.txt").read_text(encoding="utf-8"), "built")
        self.assertEqual((template[0] / "a.txt").read_text(encoding="utf-8"), "built")

    def test_a_tree_naming_its_template_is_refused(self) -> None:
        def build(path: Path) -> None:
            (path / "config").write_text(f"url = {path}/remote.git\n", encoding="utf-8")

        with self.assertRaises(AssertionError) as caught:
            copy_of(self.key, self.tmp / "one", build)
        self.assertIn("config", str(caught.exception))

    def test_a_tree_naming_the_resolved_template_is_refused(self) -> None:
        real = self.tmp / "real"
        real.mkdir()
        try:
            os.symlink(real, self.tmp / "alias", target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlinks are not permitted here: {exc}")
        made = self.tmp / "alias" / "template"
        made.mkdir()

        def build(path: Path) -> None:
            self.assertNotEqual(path.resolve(), path)
            (path / "config").write_text(f"url = {path.resolve()}/remote.git\n", encoding="utf-8")

        with mock.patch("fixture_cache.tempfile.mkdtemp", return_value=str(made)):
            with self.assertRaises(AssertionError):
                copy_of(self.key, self.tmp / "one", build)

    def test_a_symlink_into_the_template_is_refused(self) -> None:
        def build(path: Path) -> None:
            (path / "file").write_text("built", encoding="utf-8")
            try:
                os.symlink(path / "file", path / "link")
            except OSError as exc:
                self.skipTest(f"symlinks are not permitted here: {exc}")

        with self.assertRaises(AssertionError):
            copy_of(self.key, self.tmp / "one", build)

    def test_fix_runs_before_the_check(self) -> None:
        def build(path: Path) -> None:
            (path / "config").write_text(f"url = {path}/remote.git\n", encoding="utf-8")

        def fix(dest: Path) -> None:
            (dest / "config").write_text(f"url = {dest}/remote.git\n", encoding="utf-8")

        copy_of(self.key, self.tmp / "one", build, fix)


if __name__ == "__main__":
    unittest.main()
