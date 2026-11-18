"""The text a compacted session keeps (`compact_text`) and the hooks that carry it.

    python3 -m unittest discover -s engine/tests -k test_build_compact
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import profile_guard  # noqa: E402,F401

from build_support import *  # noqa: E402,F401,F403
from plugin_support import ENGINE, PLUGIN  # noqa: E402


class Compact(Build):
    def flight(self):
        w = self.started()
        self.unit("u1")
        self.unit("u2")
        self.ok("ruling", "keep the ledger small")
        return w

    def built(self) -> Path:
        built = self.tmp / "built"
        if built.exists():
            return built
        shutil.copytree(PLUGIN, built)
        shutil.copytree(ENGINE / "govern", built / "govern",
                        ignore=shutil.ignore_patterns("__pycache__"))
        return built

    def run_hook(self, name: str, stdin: str = "{}"):
        home = self.tmp / "home"
        home.mkdir(exist_ok=True)
        env = {**os.environ, "HOME": str(home), "CLAUDE_PROJECT_DIR": str(self.w.root),
               "CONTEXT_GATE_NO_UPDATE_CHECK": "1"}
        return subprocess.run([sys.executable, str(self.built() / "hooks" / name)],
                              input=stdin, env=env, cwd=str(self.tmp),
                              capture_output=True, text=True)

    def test_compact_text_names_the_build(self):
        w = self.flight()
        text = build.compact_text(w.root)
        self.assertTrue(text.startswith("Build in flight"), text)
        for uid in ("u1", "u2"):
            self.assertIn(uid, text)
        self.assertIn("keep the ledger small", text)
        self.assertIn("\nNext: ", text)
        ledger = (self.dir() / build.LEDGER).relative_to(w.root).as_posix()
        self.assertTrue(text.endswith(
            f"The ledger at {ledger} is the truth; drop tool output and file contents from "
            f"the summary."), text)

    def test_compact_text_empty_without_a_build(self):
        w = self.project()
        self.assertEqual(build.compact_text(w.root), "")

    def test_precompact_hook_prints_the_text(self):
        w = self.flight()
        res = self.run_hook("compact.py")
        self.assertEqual((res.returncode, res.stderr), (0, ""))
        self.assertEqual(res.stdout.strip(), build.compact_text(w.root))
        self.assertTrue(res.stdout.startswith("Build in flight"))

    def test_precompact_hook_is_silent_without_a_build(self):
        self.project()
        res = self.run_hook("compact.py")
        self.assertEqual((res.returncode, res.stdout, res.stderr), (0, "", ""))

    def test_session_start_after_compact_adds_the_status(self):
        self.flight()
        after = self.run_hook("session_start.py", '{"source": "compact"}')
        self.assertEqual(after.returncode, 0, after.stderr)
        context = json.loads(after.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Build in flight", context)
        self.assertIn("u2", context)
        fresh = self.run_hook("session_start.py", '{"source": "startup"}')
        self.assertEqual(fresh.returncode, 0, fresh.stderr)
        self.assertNotIn("Build in flight", fresh.stdout)

    def test_hooks_json_has_precompact(self):
        hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        command = hooks["hooks"]["PreCompact"][0]["hooks"][0]["command"]
        self.assertIn("hooks/compact.py", command)


if __name__ == "__main__":
    unittest.main()
