#!/usr/bin/env python3
"""Print one JSON object for the plugin's drive band: the newest `govern build drive` run of the
project holding the given folder (default: the working directory), or {} when there is none to
show. Reads the run's status.json; scrapes no process. Never raises, always exit 0.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SHOWN_AFTER_STOP = 600  # seconds a stopped run stays on the band
STILL_ACTIVE = 259


def _project_root(start: Path):
    here = start.resolve()
    for folder in (here, *here.parents):
        if (folder / ".context-gate").is_dir():
            return folder
    return None


def _same(a, b) -> bool:
    try:
        return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))
    except (OSError, ValueError, TypeError):
        return False


def _read(path: Path):
    try:
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _runs_for(folder: Path, runs: Path):
    try:
        folders = sorted((p for p in runs.glob(folder.name + "-*") if p.is_dir()),
                         key=lambda p: p.name, reverse=True)
    except OSError:
        return None
    for run in folders:
        doc = _read(run / "status.json")
        if doc is not None and _same(doc.get("cwd", ""), folder):
            return doc
    return None


def find_run(root: Path, home: Path):
    """The newest run whose cwd is `root` or, nearest first, the closest governed folder above it
    (a workspace's driver runs from the workspace root)."""
    runs = home / ".local" / "state" / "context-gate" / "drive" / "runs"
    folders = [root] + [p for p in root.parents if (p / ".context-gate").is_dir()]
    for folder in folders:
        doc = _runs_for(folder, runs)
        if doc is not None:
            return doc
    return None


def _own_name() -> str:
    doc = _read(Path(__file__).resolve().parent.parent / ".claude-plugin" / "plugin.json")
    name = doc.get("name") if doc else None
    return name if isinstance(name, str) else ""


def _enabled_plugins(root: Path, home: Path) -> dict:
    merged: dict = {}
    given = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    user = (Path(given).expanduser() if given else home / ".claude") / "settings.json"
    for path in (user, root / ".claude" / "settings.json",
                 root / ".claude" / "settings.local.json"):
        doc = _read(path)
        plugins = doc.get("enabledPlugins") if doc else None
        if isinstance(plugins, dict):
            merged.update(plugins)
    return merged


def _other_copy_draws(skill, root: Path, home: Path) -> bool:
    """True when the plugin the driver runs is another copy than this one and is enabled: it draws the band."""
    if not isinstance(skill, str) or not skill or skill == _own_name():
        return False
    return any(isinstance(k, str) and k.startswith(skill + "@") and v is True
               for k, v in _enabled_plugins(root, home).items())


def alive(pid) -> bool:
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        if os.name == "nt":
            import ctypes
            kernel = ctypes.windll.kernel32
            kernel.OpenProcess.restype = ctypes.c_void_p
            handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                return bool(kernel.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))) \
                    and code.value == STILL_ACTIVE
            finally:
                kernel.CloseHandle(ctypes.c_void_p(handle))
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except Exception:
        return False


def _epoch(text):
    if not isinstance(text, str):
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _round(value):
    return None if isinstance(value, bool) or not isinstance(value, (int, float)) else round(value)


def _last(last):
    if not isinstance(last, dict) or "kind" not in last:
        return None
    seconds = last.get("seconds")
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return str(last["kind"])
    seconds = int(seconds)
    span = f"{seconds // 60}m" if seconds >= 60 else f"{seconds}s"
    return f"{last['kind']} {span}"


def band(doc: dict, now: float) -> dict:
    state = doc.get("state")
    stopped = state == "stopped"
    is_alive = alive(doc.get("pid"))
    updated = _epoch(doc.get("updated"))
    age = max(0, int(now - updated)) if updated is not None else 0
    if (stopped or not is_alive) and (updated is None or age > SHOWN_AFTER_STOP):
        return {}
    running = is_alive and not stopped
    in_session = running and state == "in_session"
    started = _epoch(doc.get("session_started"))
    doing = doc.get("doing")
    since = _epoch(doc.get("doing_since"))
    out = {
        "running": running,
        "stopped": stopped,
        "project": doc.get("project"),
        "session": doc.get("session"),
        "elapsed_s": max(0, int(now - started)) if in_session and started is not None else None,
        "in_session": in_session,
        "doing": doing if isinstance(doing, str) else None,
        "doing_s": None if since is None else max(0, int(now - since)),
        "units": doc.get("units"),
        "seven_day": _round(doc.get("seven_day")),
        "cap": _round(doc.get("cap")),
        "last": _last(doc.get("last")),
        "age_s": age,
    }
    if "cap_source" in doc:
        out["cap_source"] = doc["cap_source"]
    return out


def home_dir() -> Path:
    """HOME first, as the driver (layout.home) and the other hooks do; Path.home() reads USERPROFILE on Windows."""
    return Path(os.environ.get("HOME") or Path.home())


def main(argv) -> dict:
    root = _project_root(Path(argv[1]) if len(argv) > 1 and argv[1] else Path.cwd())
    if root is None:
        return {}
    home = home_dir()
    doc = find_run(root, home)
    if doc is None or not doc.get("project"):
        return {}
    if _other_copy_draws(doc.get("skill"), root, home):
        return {}
    return band(doc, time.time())


if __name__ == "__main__":
    try:
        result = main(sys.argv)
    except Exception:
        result = {}
    try:
        print(json.dumps(result))
    except Exception:
        print("{}")
    sys.exit(0)
