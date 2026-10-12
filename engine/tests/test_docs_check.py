"""The doc-frontmatter finding for a file with no frontmatter names what to add.

    python3 -m unittest discover -s engine/tests -k docs
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))

from govern.checks.docs import check_doc  # noqa: E402
from govern.findings import Findings  # noqa: E402

PARAMS = {"required_keys": ["doc_type", "audience"], "doc_types": ["manual", "working"],
          "audiences": ["agent", "human"], "working_statuses": [], "stale_types": [],
          "stale_days": 90, "max_working_words": 1000}


class NoFrontmatter(unittest.TestCase):
    def run_check(self, params):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.md"
            path.write_text("# Plain\n", encoding="utf-8")
            f = Findings()
            check_doc(None, params, path, "a.md", f)
        return f.errors

    def test_no_frontmatter_names_the_keys_and_allowed_values(self):
        self.assertEqual(self.run_check(PARAMS), [
            "a.md: no frontmatter \u2014 add doc_type, audience; doc_type is one of manual, "
            "working; audience is one of agent, human"])
        changed = {**PARAMS, "doc_types": ["note"], "required_keys": ["doc_type"]}
        self.assertEqual(self.run_check(changed), [
            "a.md: no frontmatter \u2014 add doc_type; doc_type is one of note"])
        none = {**PARAMS, "required_keys": []}
        self.assertEqual(self.run_check(none), ["a.md: no frontmatter"])
        aud = {**PARAMS, "required_keys": ["audience", "title"]}
        self.assertEqual(self.run_check(aud), [
            "a.md: no frontmatter \u2014 add audience, title; audience is one of agent, human"])


if __name__ == "__main__":
    unittest.main()
