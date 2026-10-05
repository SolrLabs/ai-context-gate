#!/usr/bin/env python3
"""SessionStart: tell the session, and the person, when this project's governance has an upgrade,
and, from a beta, when the stable plugin is enabled here too; and resolve the usage option for
this session.

Silent in projects without .context-gate/, and silent on any failure: a notice must
never get in the way of starting work. Uses the engine bundled with this plugin.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    sys.path.insert(0, str(PLUGIN_ROOT))
    from govern import layout
    if not (root / layout.CONFIG).is_file():
        return
    from govern import notice
    home = layout.home()
    msgs = [notice.for_project(root, home), notice.both_plugins_warning(root, home)]
    msgs.append(_usage(root, home))
    msg = "\n".join(m for m in msgs if m)
    if msg:
        print(json.dumps({"systemMessage": msg,
                          "hookSpecificOutput": {"hookEventName": "SessionStart",
                                                 "additionalContext": msg}}))


def _mentions_usage(root: Path) -> bool:
    """Whether this project's config could possibly turn `usage` on: it names the check
    somewhere, or names a profile (whose own principles.toml might). Skipping the resolve child
    when neither holds saves a subprocess and a full config load on every session of every
    project that never touches the option."""
    from govern import layout
    try:
        text = (root / layout.CONFIG).read_text(encoding="utf-8")
    except OSError:
        return True             # unreadable: let the real load report it, not this shortcut
    if "usage" in text:
        return True
    try:
        import tomllib
        data = tomllib.loads(text)
    except Exception:
        # Malformed, or this interpreter predates tomllib (3.10 or earlier; the hook itself
        # is not held to the engine's 3.11+ floor): either way, let the real load report it,
        # not this shortcut.
        return True
    return bool((data.get("governance") or {}).get("profile"))


def _usage(root: Path, home: Path) -> str | None:
    """Resolve the usage option for this session, and re-wrap the statusline if another tool
    replaced it. Never raises: usage alerts must not get in the way of the upgrade notice.

    Resolving means loading the project's config, which can run a `git clone` for a profile
    (no timeout of its own) or import the project's own extensions (which can print, write
    stderr, or exit). None of that may happen in this process, so resolving runs in a child
    process instead, with its output discarded and a short timeout; only the local, engine-only
    steps (sweep, re-wrap) run here."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return None        # run by hand, not by Claude Code: no hook input to wait for
        data = json.loads(sys.stdin.buffer.read() or b"{}")
        sid = data.get("session_id")
        if not isinstance(sid, str) or not sid or "/" in sid or "\\" in sid:
            return None
        if not _mentions_usage(root):
            return None
        from govern import usage, usage_setup
        try:
            child_ok = subprocess.run(
                [sys.executable, str(PLUGIN_ROOT / "hooks" / "usage_resolve.py"), str(root), sid],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=7,
            ).returncode == 0
        except Exception:
            child_ok = False
        resolved = usage.read_json(usage.resolved_path(home, sid))
        if resolved is None:
            # Either the option is off (the child writes no file for that; not a failure), or
            # the child itself failed or timed out (worth a log line, once per session).
            if not child_ok:
                usage.state_dir(home).mkdir(parents=True, exist_ok=True)
                with open(usage.log_path(home), "a", encoding="utf-8") as fh:
                    fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {sid} usage resolution "
                            f"failed or timed out\n")
            return None
        usage.sweep(home, time.time())
        return usage_setup.rewrap(home) if resolved.get("enabled") else None
    except Exception:
        return None


try:
    main()
except Exception:
    pass
