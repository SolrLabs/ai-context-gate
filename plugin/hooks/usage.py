#!/usr/bin/env python3
"""PostToolUse, UserPromptSubmit, SessionEnd: tell the orchestrating agent its usage, and inject
the owner's pre-set prompts at break points. Subagents get nothing. Silent on any failure, never
on stderr, always exit 0. Uses govern.usage from the engine bundled with this plugin.
"""
import json
import os
import sys
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    data = json.loads(sys.stdin.buffer.read())
    if not isinstance(data, dict) or data.get("agent_id"):
        return
    sid, event = data.get("session_id"), data.get("hook_event_name")
    if not isinstance(sid, str) or not sid or "/" in sid or "\\" in sid:
        return
    if event != "SessionEnd":
        # A quick, stdlib-only check, before importing anything from govern: with the option
        # off (or unresolved), SessionStart wrote no resolved file, so every tool call in every
        # project stays a single stat call, not an import and a state-directory read.
        home = Path(os.environ.get("HOME") or Path.home())
        resolved = home / ".local" / "state" / "context-gate" / "usage" / f"{sid}.resolved.json"
        if not os.path.isfile(resolved):
            return
    sys.path.insert(0, str(PLUGIN_ROOT))
    from govern import layout, usage
    home = layout.home()
    if event == "SessionEnd":
        usage.end_session(home, sid)
        return
    text = usage.on_call(home, sid, time.time())
    if text:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": event,
                                                 "additionalContext": text}}))


try:
    main()
except Exception:
    pass
