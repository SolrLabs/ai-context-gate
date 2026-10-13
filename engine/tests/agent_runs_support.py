"""Agent gates and measurement: synthetic hook input through the plugin's hook, no model involved.

    python3 -m unittest discover -s engine/tests -k agent_runs
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path, PurePosixPath, PureWindowsPath
from unittest import mock

ENGINE = Path(__file__).resolve().parent.parent
PLUGIN = ENGINE.parent / "plugin"
sys.path.insert(0, str(ENGINE))

from govern import agent_runs, layout  # noqa: E402
from test_options import repo  # noqa: E402

BUILT = None            # the plugin as install-plugin assembles it: plugin/ plus the engine
_BUILD = None
GATES = {"briefed": ["developer", "senior-developer"], "claims": True, "deny_push": True,
         "max_refusals": 2}
MEASURE = {"notice": True}
USAGE = {"input_tokens": 2, "cache_creation_input_tokens": 100,
         "cache_read_input_tokens": 1000, "output_tokens": 10}


def setUpModule():
    global BUILT, _BUILD
    _BUILD = tempfile.TemporaryDirectory()
    BUILT = Path(_BUILD.name) / "built"
    shutil.copytree(PLUGIN, BUILT, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(ENGINE / "govern", BUILT / "govern",
                    ignore=shutil.ignore_patterns("__pycache__"))


def tearDownModule():
    _BUILD.cleanup()


class HookCase(unittest.TestCase):
    """One session ("s1") with both options on, driven through hooks/agents.py."""
    gates = GATES
    measure = MEASURE

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.root = self.tmp / "project"
        self.root.mkdir()
        self.state = agent_runs.agents_dir(self.home) / "s1"
        self.env = {**os.environ, "HOME": str(self.home)}
        self.resolve(self.gates, self.measure)

    def tearDown(self):
        self._tmp.cleanup()

    def resolve(self, gates, measure):
        agent_runs.write_json(agent_runs.resolved_path(self.home, "s1"),
                              {"gates": gates, "measure": measure, "root": str(self.root)})

    def run_hook(self, stdin: bytes, env=None):
        return subprocess.run([sys.executable, str(BUILT / "hooks" / "agents.py")], input=stdin,
                              capture_output=True, env=env or self.env, timeout=20)

    def fire(self, event, **fields):
        data = {"session_id": "s1", "hook_event_name": event, **fields}
        done = self.run_hook(json.dumps(data).encode())
        self.assertEqual(done.returncode, 0)
        self.assertEqual(done.stderr, b"")
        return json.loads(done.stdout) if done.stdout.strip() else None

    def launch(self, prompt, agent="a1", agent_type="general-purpose", start_cwd=None, **extra):
        """`extra` goes to PreToolUse (its `cwd` is the launching session's); `start_cwd` is the
        folder SubagentStart reports the agent started in."""
        out = self.fire("PreToolUse", tool_name="Agent", tool_use_id="t1",
                        tool_input={"prompt": prompt, "subagent_type": agent_type}, **extra)
        if out is None:
            self.fire("SubagentStart", agent_id=agent, agent_type=agent_type,
                      **({"cwd": start_cwd} if start_cwd else {}))
        return out

    def notice(self, agent="a1"):
        out = self.fire("UserPromptSubmit", prompt="<task-notification>\n<task-id>"
                        f"{agent}</task-id></task-notification>")
        return out["hookSpecificOutput"]["additionalContext"] if out else None

    def stop(self, agent="a1", final="done", transcript=""):
        return self.fire("SubagentStop", agent_id=agent, agent_type="general-purpose",
                         last_assistant_message=final, agent_transcript_path=transcript)

    def tool(self, name, agent="a1", **inp):
        self.fire("PostToolUse", agent_id=agent, agent_type="general-purpose", tool_name=name,
                  tool_input=inp, tool_response={"ok": True}, duration_ms=5)

    def rows(self):
        return agent_runs.read_json_lines(self.state / "runs.jsonl")

    def durable(self):
        return agent_runs.read_json_lines(agent_runs.durable_path(self.home, self.root))

    def transcript(self, requests):
        """A transcript with one assistant request per (id, usage, text) given."""
        path = self.tmp / "agent.jsonl"
        lines = [{"type": "user", "message": {"content": "the brief"}}]
        for mid, usage, text in requests:
            lines.append({"type": "assistant", "message": {
                "id": mid, "usage": usage, "content": [{"type": "text", "text": text}]}})
        path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
        return str(path)


TEXTS = {"Scope": "the files you changed. Each is a file the brief names.",
         "Gate": "the full gate's result line.", "Not verified": "each claim that rests on reading."}


def row(agent, type_="developer", tokens=None, **extra):
    tokens = tokens if tokens is not None else {
        "requests": 1, "input_tokens": 0, "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0, "output_tokens": 100, "context_at_end": 100}
    return {"t": 1.0, "agent": agent, "type": type_, "model": "sonnet", "resumed": False,
            "wall_s": 1.0, "tokens": tokens, "tool_calls": 0, "tool_ms": 0, "failed_calls": 0,
            "result_bytes": 0, "by_tool": {}, "largest": [], "repeated": [], "status": None,
            "findings": None, "gate_blocks": 0, "gate_unmet": [], **extra}


__all__ = [
    "contextlib",
    "io",
    "json",
    "os",
    "shutil",
    "statistics",
    "subprocess",
    "sys",
    "tempfile",
    "threading",
    "time",
    "unittest",
    "Path",
    "PurePosixPath",
    "PureWindowsPath",
    "mock",
    "ENGINE",
    "PLUGIN",
    "agent_runs",
    "layout",
    "repo",
    "BUILT",
    "_BUILD",
    "GATES",
    "MEASURE",
    "USAGE",
    "setUpModule",
    "tearDownModule",
    "HookCase",
    "TEXTS",
    "row",
]
