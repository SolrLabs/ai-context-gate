"""tools/ci/parallel_tests.py gives each test child an empty stdin, so a test that reads stdin
does not hang when the runner itself is started with an open pipe."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import profile_guard  # noqa: E402,F401

REPO = Path(__file__).resolve().parents[2]
RUNNER = REPO / "tools" / "ci" / "parallel_tests.py"

MODULE = '''import sys
import unittest


class Reads(unittest.TestCase):
    def test_stdin_is_empty(self):
        self.assertEqual(sys.stdin.read(), "")


if __name__ == "__main__":
    unittest.main()
'''


class ChildStdin(unittest.TestCase):
    def test_a_test_that_reads_stdin_finishes_when_the_runner_has_an_open_pipe(self):
        with tempfile.TemporaryDirectory() as tmp:
            tests = Path(tmp) / "tests"
            tests.mkdir()
            (tests / "test_reads_stdin.py").write_text(MODULE, encoding="utf-8")
            env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
            env["HOME"] = tmp
            env["USERPROFILE"] = tmp
            proc = subprocess.Popen([sys.executable, str(RUNNER), str(tests), "-j", "1"],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, cwd=tmp, env=env)
            try:
                proc.wait(timeout=20)   # not communicate(): that would close the stdin pipe
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                proc.stdout.close()
                proc.stdin.close()
                self.fail("the runner hung: its child inherited the open stdin")
            out = proc.stdout.read()
            proc.stdout.close()
            proc.stdin.close()
            self.assertEqual(proc.returncode, 0, out)
            self.assertIn("Ran 1 tests", out)
            self.assertIn("OK", out)


if __name__ == "__main__":
    unittest.main()
