"""Engine versions: a beta, X.Y.Z-beta.N, orders between the release before it and its own."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

sys.path.insert(0, str(Path(__file__).resolve().parent))

import profile_guard  # noqa: E402,F401

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


class ChannelPin(unittest.TestCase):
    """`[governance] channel = "beta"`: the engine runs as a beta newer than the release the
    project pins; the pin itself stays a release."""

    def pin(self, engine: str, pin, **kw):
        with mock.patch.object(config, "__version__", engine):
            return config.check_pin(pin, **kw)

    def test_a_newer_beta_runs_on_the_beta_channel(self):
        self.assertIsNone(self.pin("0.9.5-beta.3", "0.9.4", channel="beta"))
        self.assertIsNone(self.pin("0.10.0-beta.1", "0.9.4", channel="beta"))
        self.assertIsNone(self.pin("0.10.0-beta.1", "0.9", channel="beta"))

    def test_a_beta_not_newer_is_refused_as_before(self):
        for engine in ("0.9.4-beta.7", "0.9.3-beta.1"):
            with self.subTest(engine=engine), \
                    self.assertRaisesRegex(config.ConfigError, "but this is engine"):
                self.pin(engine, "0.9.4", channel="beta")
        with self.assertRaisesRegex(config.ConfigError, "newer than this"):
            self.pin("0.9.5-beta.1", "0.10", channel="beta")
        self.assertEqual(self.pin("0.9.5-beta.1", "0.9", channel="beta"), "series")

    def test_a_newer_beta_is_refused_off_the_beta_channel(self):
        for kw in ({}, {"channel": "stable"}, {"channel": None}):
            with self.subTest(**kw), \
                    self.assertRaisesRegex(config.ConfigError, "but this is engine"):
                self.pin("0.9.5-beta.3", "0.9.4", **kw)

    def test_a_newer_release_is_refused_on_the_beta_channel(self):
        with self.assertRaisesRegex(config.ConfigError, "but this is engine"):
            self.pin("0.9.5", "0.9.4", channel="beta")

    def test_a_beta_pin_is_refused_with_or_without_the_channel(self):
        for kw in ({}, {"channel": "beta"}):
            with self.subTest(**kw), self.assertRaisesRegex(config.ConfigError, "pins beta"):
                self.pin("0.9.5-beta.3", "0.9.5-beta.3", **kw)

    def test_a_channel_other_than_stable_or_beta_is_a_config_error(self):
        for value in ("nightly", "Beta", "", 1, True):
            with self.subTest(value=value), \
                    self.assertRaisesRegex(config.ConfigError, r"\[governance\] channel"):
                config._validate_layout({"governance": {"channel": value}}, "config.toml")
        with self.assertRaisesRegex(config.ConfigError, '"stable" or "beta"'):
            config._validate_layout({"governance": {"channel": "nightly"}}, "config.toml")
        for value in ("stable", "beta"):
            config._validate_layout({"governance": {"channel": value}}, "config.toml")


class InTreePin(unittest.TestCase):
    """`[governance] engine_dir`: the engine's own repository runs its tree, whatever it pins."""

    def pin(self, pin, **kw):
        with mock.patch.object(config, "__version__", "0.9.4"):
            return config.check_pin(pin, **kw)

    def test_a_different_release_pin_is_accepted_in_tree(self):
        self.assertIsNone(self.pin("0.9.3", in_tree=True))

    def test_the_same_pin_is_refused_without_it(self):
        with self.assertRaisesRegex(config.ConfigError, "but this is engine"):
            self.pin("0.9.3")

    def test_a_beta_pin_is_refused_in_tree(self):
        with self.assertRaises(config.ConfigError):
            self.pin("0.9.4-beta.1", in_tree=True)


if __name__ == "__main__":
    unittest.main()
