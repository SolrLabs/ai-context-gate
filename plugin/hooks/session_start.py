#!/usr/bin/env python3
"""SessionStart: tell the session, and the person, when this project's governance has an upgrade.

Silent in projects without .context-gate/, and silent on any failure: a notice must
never get in the way of starting work. Uses the engine bundled with this plugin.
"""
import json
import os
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    sys.path.insert(0, str(PLUGIN_ROOT))
    from govern import layout
    if not (root / layout.CONFIG).is_file():
        return
    from govern import notice
    msg = notice.for_project(root, layout.home())
    if msg:
        print(json.dumps({"systemMessage": msg,
                          "hookSpecificOutput": {"hookEventName": "SessionStart",
                                                 "additionalContext": msg}}))


try:
    main()
except Exception:
    pass
