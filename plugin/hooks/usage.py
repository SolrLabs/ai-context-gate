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


def _standing_down() -> bool:
    """The beta copy of this plugin does nothing in a project that is not on a beta. `beta off`
    deletes local.toml, so from that moment the released plugin and this one never both act,
    even in a session that still has both loaded."""
    if not PLUGIN_ROOT.name.endswith("-beta"):
        return False
    root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    return not (root / ".context-gate" / "local.toml").is_file()


def _record_coordinator(home: Path, sid: str, data: dict) -> None:
    """With `agent-measure` on for this session's project, put the coordinator's own numbers in
    the project's run log. The resolved file is written at SessionStart; no file, or `measure`
    null, means off."""
    from govern import agent_runs, coordinator_runs
    resolved = agent_runs.read_json(agent_runs.resolved_path(home, sid))
    if not isinstance(resolved, dict) or resolved.get("measure") is None:
        return
    root, transcript = resolved.get("root"), data.get("transcript_path")
    if isinstance(root, str) and root and isinstance(transcript, str) and transcript:
        coordinator_runs.record(home, Path(root), Path(transcript))


def main() -> None:
    if _standing_down():
        return
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
        try:
            _record_coordinator(home, sid, data)
        except Exception:
            pass
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
