#!/usr/bin/env python3
"""SessionStart: tell the session, and the person, when this project's governance has an upgrade,
and, from a beta, when the stable plugin is enabled here too, or the beta was switched off by a
launcher that leaves its switch behind (removed, and said); resolve the usage and agent
options for this session; and say which build the checked-out branch has in flight, if any.

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
    left = _beta_left_behind(root)
    if PLUGIN_ROOT.name.endswith("-beta") and not (root / layout.LOCAL).is_file():
        # The beta copy in a project that is not on a beta: say the one thing above, if it
        # applies, and nothing else. The released plugin speaks for this project.
        if left:
            print(json.dumps({"systemMessage": left,
                              "hookSpecificOutput": {"hookEventName": "SessionStart",
                                                     "additionalContext": left}}))
        return
    msgs = [notice.for_project(root, home), notice.both_plugins_warning(root, home), left]
    msgs.extend(_usage(root, home))
    msg = "\n".join(m for m in msgs if m)
    if msg:
        print(json.dumps({"systemMessage": msg,
                          "hookSpecificOutput": {"hookEventName": "SessionStart",
                                                 "additionalContext": msg}}))


def _beta_left_behind(root: Path) -> str | None:
    """From the beta plugin only: a launcher older than the beta plugin's own name leaves the
    plugin's id enabled in settings.local.json when it switches the beta off. When this project
    names no beta engine in local.toml, remove it and say so. None, and nothing changed, in any
    other state or on any failure."""
    try:
        import tomllib
        from govern import layout, notice, versions
        manifest = json.loads((PLUGIN_ROOT / ".claude-plugin" / "plugin.json")
                              .read_text(encoding="utf-8"))
        if not str(manifest.get("name")).endswith("-beta"):
            return None
        try:
            with (root / layout.LOCAL).open("rb") as fh:
                engine = (tomllib.load(fh).get("governance") or {}).get("engine")
        except FileNotFoundError:
            engine = None
        if isinstance(engine, str) and versions.parse(engine) and versions.is_beta(engine):
            return None
        if not notice.remove_beta_switch(root):
            return None
        return (f"the beta is off in this project; removed {layout.SKILLS_DIR_PLUGIN_ID} from "
                ".claude/settings.local.json. Restart Claude Code to unload the beta plugin.")
    except Exception:
        return None


def _mentions_usage(root: Path) -> bool:
    """Whether this project's config (config.toml and local.toml, either may be missing) could
    possibly turn `usage`, `agent-gates` or `agent-measure` on: it names one of the checks
    somewhere, or names a profile (whose own principles.toml might). Skipping the resolve child when neither holds saves a subprocess
    and a full config load on every session of every project that never touches the options."""
    from govern import layout
    texts = []
    for rel in (layout.CONFIG, layout.LOCAL):       # a beta's options live in local.toml
        try:
            texts.append((root / rel).read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except OSError:
            return True         # unreadable: let the real load report it, not this shortcut
    text = "\n".join(texts)
    if any(name in text for name in ("usage", "agent-gates", "agent-measure")):
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


def _usage(root: Path, home: Path) -> list:
    """Resolve the usage option for this session, and re-wrap the statusline if another tool
    replaced it; and, when the project has a build folder, say which build is in flight on the
    checked-out branch. The messages to print, if any. Never raises: usage alerts must not get
    in the way of the upgrade notice.

    Resolving means loading the project's config, which can run a `git clone` for a profile
    (no timeout of its own) or import the project's own extensions (which can print, write
    stderr, or exit). None of that may happen in this process, so resolving runs in a child
    process instead, with its output discarded and a short timeout; only the local, engine-only
    steps (sweep, re-wrap) run here. The same child asks git for the branch and reads the build
    ledger, and hands its one line back in a file this process named."""
    msgs = []
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return msgs        # run by hand, not by Claude Code: no hook input to wait for
        data = json.loads(sys.stdin.buffer.read() or b"{}")
        sid = data.get("session_id")
        if not isinstance(sid, str) or not sid or "/" in sid or "\\" in sid:
            return msgs
        from govern import layout
        # `build/.gitignore` outlives every phase: only a ledger one level down is a build.
        building = any((root / layout.GOV_DIR / "build").glob("*/ledger.toml"))
        if not building and not _mentions_usage(root):
            return msgs
        from govern import agent_runs, usage, usage_setup
        line = agent_runs.agents_dir(home) / f"{sid}.build.json"
        try:
            child_ok = subprocess.run(
                [sys.executable, str(PLUGIN_ROOT / "hooks" / "usage_resolve.py"), str(root), sid,
                 *([str(line)] if building else [])],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=7,
            ).returncode == 0
        except Exception:
            child_ok = False
        if building:
            # Read once and removed: a line left from an earlier start is never said again.
            said = agent_runs.read_json(line)
            msgs.append(said if isinstance(said, str) else None)
            try:
                line.unlink(missing_ok=True)
            except OSError:
                pass
        resolved = usage.read_json(usage.resolved_path(home, sid))
        agent_runs.sweep(home, time.time())
        if resolved is None:
            # Either the option is off (the child writes no file for that; not a failure), or
            # the child itself failed or timed out (worth a log line, once per session).
            if not child_ok:
                usage.state_dir(home).mkdir(parents=True, exist_ok=True)
                with open(usage.log_path(home), "a", encoding="utf-8") as fh:
                    fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {sid} usage resolution "
                            f"failed or timed out\n")
            return msgs
        usage.sweep(home, time.time())
        if resolved.get("enabled"):
            msgs.insert(0, usage_setup.rewrap(home))
        return msgs
    except Exception:
        return msgs


try:
    main()
except Exception:
    pass
