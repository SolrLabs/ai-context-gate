"""The engine reports its release part under test, so a beta version does not turn the suite red."""
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import profile_guard  # noqa: E402,F401
from govern import _as_release  # noqa: E402


class AsRelease(unittest.TestCase):
    def test_the_named_beta_becomes_its_release(self):
        self.assertEqual(_as_release("0.9.5-beta.8", {"GOVERN_TEST_AS_RELEASE": "0.9.5-beta.8"}), "0.9.5")

    def test_a_different_beta_is_unchanged(self):
        env = {"GOVERN_TEST_AS_RELEASE": "0.9.5-beta.8"}
        self.assertEqual(_as_release("0.9.5-beta.3", env), "0.9.5-beta.3")
        self.assertEqual(_as_release("9.9.0-beta.9", env), "9.9.0-beta.9")

    def test_release_unchanged(self):
        self.assertEqual(_as_release("0.9.4", {"GOVERN_TEST_AS_RELEASE": "0.9.5-beta.8"}), "0.9.4")

    def test_unset_keeps_the_beta(self):
        self.assertEqual(_as_release("0.9.5-beta.8", {}), "0.9.5-beta.8")

    def test_one_is_not_the_source_version(self):
        self.assertEqual(_as_release("0.9.5-beta.8", {"GOVERN_TEST_AS_RELEASE": "1"}), "0.9.5-beta.8")


class Printed(unittest.TestCase):
    def test_subprocess_prints_the_release_part_of_the_literal(self):
        text = (ENGINE / "govern" / "__init__.py").read_text(encoding="utf-8")
        literal = re.search(r'^__version__ = "([^"]+)"$', text, re.MULTILINE).group(1)
        with tempfile.TemporaryDirectory() as home:
            env = dict(os.environ, GOVERN_TEST_AS_RELEASE=literal, HOME=home, USERPROFILE=home,
                       PYTHONPATH=str(ENGINE))
            out = subprocess.run([sys.executable, "-c", "import govern; print(govern.__version__)"],
                                 cwd=home, env=env, capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.strip(), literal.split("-", 1)[0])


if __name__ == "__main__":
    unittest.main()
