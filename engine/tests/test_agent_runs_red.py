"""The Red gate in `missing`: where it looks for the saved failing output, and what it says."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import profile_guard  # noqa: E402,F401

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from govern import agent_runs  # noqa: E402


class RedGate(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.red = Path(tmp.name).resolve() / "red-1"
        self.now = time.time()
        self.launch = {"t": self.now - 100, "red": [str(self.red)]}

    def save(self, rel: str, text: str, at: float) -> Path:
        p = self.red / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        os.utime(p, (at, at))
        return p

    def check(self, edit_at: float | None = None) -> list[str]:
        tools = [] if edit_at is None else [{"tool": "Edit", "t": edit_at}]
        return agent_runs.missing(self.launch, tools, "", False)

    def test_output_saved_in_a_subfolder_passes(self):
        self.save(str(Path("sub") / "out.txt"), "FAILED\n", self.now - 50)
        self.assertEqual(self.check(self.now - 10), [])

    def test_a_missing_or_empty_folder_holds_no_files(self):
        want = [f"{self.red} holds no files. Run the test against the code before your fix "
                "and save its real output there."]
        self.assertEqual(self.check(), want)
        self.save("empty.txt", "", self.now - 50)
        self.assertEqual(self.check(), want)

    def test_only_a_file_from_before_the_run_is_reported_as_stale(self):
        self.save("old.txt", "FAILED\n", self.now - 1000)
        self.assertEqual(self.check(), [
            f"{self.red} holds only files from before this run. Run the test against the code "
            "before your fix and save its real output there."])

    def test_output_saved_after_the_last_edit_still_fails(self):
        self.save("out.txt", "FAILED\n", self.now - 10)
        self.assertEqual(self.check(self.now - 50), [
            f"the output in {self.red} was saved after your last edit, so it does not show "
            "the test failing before the change."])


if __name__ == "__main__":
    unittest.main()
