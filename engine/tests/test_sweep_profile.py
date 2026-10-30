"""`[sweep]` in a principles profile: accepted, validated as a project's is, and resolved per key
(engine default < profile < project). Against real repositories with a bare `origin`.

    python3 -m unittest discover -s engine/tests -k test_sweep_profile
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import profile_guard  # noqa: E402,F401

from test_sweep_pushed import Pushed  # noqa: E402

LIST = '[sweep]\nauto_remove = ["merged", "pr-merged", "pushed", "stale"]\n'


class Profile(Pushed):
    def use_profile(self, text: str) -> None:
        """A principles.toml holding `text`, named by the project's config."""
        prof = self.tmp / "prof"
        prof.mkdir(exist_ok=True)
        (prof / "principles.toml").write_text(text, encoding="utf-8")
        cfg = Path(self.w.root) / ".context-gate" / "config.toml"
        body = cfg.read_text(encoding="utf-8")
        self.assertIn("schema = 1", body)
        cfg.write_text(body.replace("schema = 1", f'schema = 1\nprofile = "{prof.as_posix()}"', 1),
                       encoding="utf-8")

    def test_the_profiles_list_reaches_sweep_and_says_so(self):
        self.use_profile(LIST)
        self.assertIn("auto-remove  merged, pr-merged, pushed, stale (profile)", self.lines())

    def test_the_project_sets_a_key_and_it_replaces_the_profiles(self):
        self.use_profile(LIST)
        self.set_config('[sweep]\nauto_remove = ["merged"]\n')
        self.assertIn("auto-remove  merged", self.lines())

    def test_an_empty_project_list_asks_before_every_removal(self):
        self.use_profile(LIST)
        self.set_config("[sweep]\nauto_remove = []\n")
        self.assertIn("auto-remove  none: ask before every removal", self.lines())

    def test_a_profile_stale_days_reaches_the_verdict(self):
        self.commit_aged("spike", 5)
        self.assertNotIn("PUSHED", "\n".join(self.lines()))
        self.use_profile("[sweep]\nstale_days = 3\n")
        self.assertIn("branch    PUSHED  spike  (on origin/spike, idle 5 days)", self.lines())

    def test_a_project_stale_days_replaces_the_profiles(self):
        self.commit_aged("spike", 5)
        self.use_profile("[sweep]\nstale_days = 3\n")
        self.set_config("[sweep]\nstale_days = 9\n")
        self.assertNotIn("PUSHED", "\n".join(self.lines()))

    def test_a_bad_profile_value_names_the_profile(self):
        self.use_profile('[sweep]\nauto_remove = ["x"]\n')
        code, out, err = self.sweep()
        self.assertNotEqual(code, 0)
        text = out + err
        self.assertIn("profile", text)
        self.assertIn("principles.toml", text)
        self.assertIn("auto_remove[0] = 'x' is not one of", text)

    def test_an_unknown_profile_sweep_key_is_named(self):
        self.use_profile("[sweep]\nbogus = 1\n")
        code, out, err = self.sweep()
        self.assertNotEqual(code, 0)
        self.assertIn("bogus", out + err)

    def test_a_negative_profile_stale_days_is_refused(self):
        self.use_profile("[sweep]\nstale_days = -1\n")
        code, out, err = self.sweep()
        self.assertNotEqual(code, 0)
        self.assertIn("stale_days", out + err)


if __name__ == "__main__":
    unittest.main()
