"""`install --baseline FILE`: an install that starts from an existing baseline anywhere (one
carried over from a previous install, say). It is checked the way the gate reads a baseline, copied in,
and not recorded: nothing outside `.context-gate/` changed, so uninstall leaves no trace.

    python3 -m unittest discover -s engine/tests -k install_baseline
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from govern import installer, layout  # noqa: E402
from test_single_repo import CFG, SingleRepo  # noqa: E402

BASELINE = b'{\n  "decision_words:myproject:D-7": 300\n}\n'


def tree(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            if p.is_file() else "dir" for p in sorted(root.rglob("*"))}


class InstallBaseline(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = SingleRepo(self.tmp)
        self.cfg = self.tmp / "config.toml"
        (self.repo.root / CFG).rename(self.cfg)
        (self.repo.root / layout.GOV_DIR).rmdir()
        self.before = tree(self.repo.root)
        self.outside = self.tmp / "carried" / "baseline.json"      # outside the project
        self.outside.parent.mkdir()
        self.outside.write_bytes(BASELINE)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def installer(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.repo.home)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                code = installer.main(list(args))
        finally:
            if old_home is not None:
                os.environ["HOME"] = old_home
        return code, out.getvalue()

    def install(self, *extra: str) -> tuple[int, str]:
        return self.installer("install", "--root", str(self.repo.root), "--config",
                              str(self.cfg), "--no-report", *extra)

    def test_baseline_is_copied_in_and_not_recorded(self) -> None:
        code, out = self.install("--baseline", str(self.outside))
        self.assertEqual(code, 0, out)
        root = self.repo.root
        self.assertEqual((root / layout.BASELINE).read_bytes(), BASELINE)
        self.assertEqual(self.outside.read_bytes(), BASELINE)      # copied, not moved
        manifest = (root / layout.MANIFEST).read_text(encoding="utf-8")
        self.assertNotIn("[[moved]]", manifest)
        self.assertNotIn("baseline", manifest)
        code, out = self.installer("uninstall", "--root", str(root))
        self.assertEqual(code, 0, out)
        self.assertEqual(tree(root), self.before)                  # no trace of it

    def test_invalid_baseline_is_refused_with_nothing_changed(self) -> None:
        for name, data in (("not JSON", b"{not json"), ("not an object", b"[1, 2]\n"),
                           ("not a number", b'{"k": "many"}\n'), ("not UTF-8", b'{"\xff": 1}')):
            with self.subTest(name):
                self.outside.write_bytes(data)
                code, out = self.install("--baseline", str(self.outside))
                self.assertEqual(code, 2, out)
                self.assertIn("is not a valid baseline", out)
                self.assertIn("nothing was changed", out)
                self.assertEqual(tree(self.repo.root), self.before)
        code, out = self.install("--baseline", str(self.tmp / "missing.json"))
        self.assertEqual(code, 2, out)
        self.assertEqual(tree(self.repo.root), self.before)

    def test_baseline_and_migrate_baseline_together_are_refused(self) -> None:
        (self.repo.root / "old-baseline.json").write_bytes(BASELINE)
        self.before = tree(self.repo.root)
        code, out = self.install("--baseline", str(self.outside),
                                 "--migrate-baseline", "old-baseline.json")
        self.assertEqual(code, 2, out)
        self.assertIn("give one", out)
        self.assertEqual(tree(self.repo.root), self.before)


if __name__ == "__main__":
    unittest.main()
