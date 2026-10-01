"""Engine versions: a beta, X.Y.Z-beta.N, orders between the release before it and its own."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from govern import config, versions  # noqa: E402


class Versions(unittest.TestCase):
    def test_order(self):
        seq = ["0.5.1", "0.6.0-beta.1", "0.6.0-beta.2", "0.6.0-beta.10", "0.6.0", "0.6.1"]
        self.assertEqual(sorted(reversed(seq), key=versions.key), seq)

    def test_not_versions(self):
        for v in ("0.6.0-rc1", "0.6.0-beta", "0.6.0-beta.0", "0.6", "v0.6.0", "0.6.0-beta.01",
                  "\uff10.\uff16.\uff10"):
            self.assertIsNone(versions.parse(v), v)
            self.assertFalse(versions.is_beta(v) or versions.is_stable(v), v)

    def test_kinds(self):
        self.assertTrue(versions.is_beta("0.6.0-beta.1"))
        self.assertTrue(versions.is_stable("0.6.0"))
        self.assertEqual(versions.final("0.6.0-beta.2"), "0.6.0")

    def test_key_refuses_a_non_version(self):
        with self.assertRaises(ValueError):
            versions.key("0.6.0-rc1")

    def test_series_of_a_beta_is_its_release_series(self):
        """A beta belongs to the series of the release it leads to."""
        self.assertEqual(config.series("0.6.0-beta.1"), (0, 6))


if __name__ == "__main__":
    unittest.main()
