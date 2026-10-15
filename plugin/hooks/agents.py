#!/usr/bin/env python3
"""PreToolUse, PostToolUse, PostToolUseFailure, SubagentStart, SubagentStop, UserPromptSubmit:
gate and measure subagent work. Does nothing without the session's resolved file, which
SessionStart writes only when `agent-gates` or `agent-measure` is on. A subagent gets nothing
from this except a gate's refusal. Silent on any failure, never on stderr, always exit 0. Uses
govern.agent_runs from the engine bundled with this plugin.
"""
import json
import os
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def _standing_down() -> bool:
    """The beta copy of this plugin does nothing in a project that is not on a beta. `beta off`
    deletes local.toml, so from that moment the released plugin and this one never both act,
    even in a session that still has both loaded."""
    if not PLUGIN_ROOT.name.endswith("-beta"):
        return False
    root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    return not (root / ".context-gate" / "local.toml").is_file()


def main() -> None:
    if _standing_down():
        return
    data = json.loads(sys.stdin.buffer.read())
    if not isinstance(data, dict):
        return
    sid = data.get("session_id")
    if not isinstance(sid, str) or not sid or "/" in sid or "\\" in sid:
        return
    # A quick, stdlib-only check, before importing anything from govern: with both options off
    # (or unresolved) SessionStart wrote no resolved file, so every hooked event in every project
    # stays one stat call.
    home = Path(os.environ.get("HOME") or Path.home())
    resolved = home / ".local" / "state" / "context-gate" / "agents" / f"{sid}.resolved.json"
    if not os.path.isfile(resolved):
        return
    sys.path.insert(0, str(PLUGIN_ROOT))
    from govern import agent_runs, layout
    settings = agent_runs.read_json(resolved)
    if not isinstance(settings, dict):
        return
    out = agent_runs.handle(layout.home(), settings, data)
    if out:
        print(json.dumps(out))


try:
    main()
except Exception:
    pass
