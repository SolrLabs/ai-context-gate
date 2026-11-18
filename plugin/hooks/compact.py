#!/usr/bin/env python3
"""PreCompact: when this project has a build in flight on the checked-out branch, print its
status as the compact instructions (plain stdout, exit 0), so the summary keeps the build's
state; SessionStart says it again word for word afterwards.

Silent in projects without .context-gate/, silent on any failure, and the beta copy stands
down in a project that is not on a beta. Uses the engine bundled with this plugin.
"""
import os
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    if PLUGIN_ROOT.name.endswith("-beta") and not (root / ".context-gate" / "local.toml").is_file():
        return
    sys.path.insert(0, str(PLUGIN_ROOT))
    from govern import layout
    if not (root / layout.CONFIG).is_file():
        return
    from govern import build
    text = build.compact_text(root)
    if text:
        sys.stdout.write(text + "\n")


try:
    main()
except Exception:
    pass
sys.exit(0)
