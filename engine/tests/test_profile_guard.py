"""`profile_guard`: no test sees the running user's Claude profile.

    python3 -m unittest discover -s engine/tests -k profile_guard
"""
from __future__ import annotations

import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import profile_guard  # noqa: E402

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent


class ProfileGuard(unittest.TestCase):
    def test_every_test_module_imports_the_guard(self):
        files = [*HERE.glob("test_*.py"), *HERE.glob("*_support.py"),
                 *(REPO / "tools" / "tests").glob("test_*.py")]
        self.assertGreater(len(files), 20)
        missing = [str(f.relative_to(REPO)) for f in files
                   if not any(line.startswith("import profile_guard")
                              for line in f.read_text(encoding="utf-8").splitlines())]
        self.assertEqual(missing, [])

    def test_importing_it_removes_the_variable(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/somewhere/real"}):
            importlib.reload(profile_guard)
            self.assertNotIn("CLAUDE_CONFIG_DIR", os.environ)


if __name__ == "__main__":
    unittest.main()
