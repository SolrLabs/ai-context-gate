"""Usage alerts, set up: install the statusline capture on this machine, take it out again,
re-wrap it when another tool replaced it, and resolve a project's option and alerts file.
See docs/configuration.md ("Usage alerts").
"""
from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

from govern import installer, usage
from govern.layout import GOV_DIR, TOOL

ALERTS_NAME = "usage-alerts.toml"
TEMPLATE = Path(__file__).resolve().parent / "templates" / "capture.py"
MARK = f"{TOOL}/capture.py"


def share_dir(home: Path) -> Path:
    return home / ".local" / "share" / TOOL


def capture_path(home: Path) -> Path:
    return share_dir(home) / "capture.py"


def chain_path(home: Path) -> Path:
    return share_dir(home) / "statusline-chain.json"


def _off_told(home: Path) -> Path:
    """A marker for the once-per-machine "no statusLine is set" message: present once it has
    been said, cleared once install or a re-wrap fixes the statusLine again."""
    return usage.state_dir(home) / "statusline-off-told"


def is_ours(statusline) -> bool:
    cmd = statusline.get("command") if isinstance(statusline, dict) else None
    return isinstance(cmd, str) and MARK in cmd.replace("\\", "/")


def _backup_settings(home: Path) -> None:
    """Copy the settings file's current bytes to `settings.json.bak` (overwriting any earlier
    backup) right before this module writes it, so a bad write is recoverable by hand."""
    path = home / installer.SETTINGS
    if not path.exists():
        return
    share_dir(home).mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, share_dir(home) / "settings.json.bak")


def _wrap(home: Path, data: dict, current, python: str) -> None:
    usage.write_json(chain_path(home), {"statusLine": current})
    new = dict(current) if isinstance(current, dict) else {"type": "command"}
    new["command"] = f'"{python}" "{capture_path(home)}"'
    data["statusLine"] = new
    _backup_settings(home)
    installer._write_settings(home, data)


def install(home: Path, python: str = sys.executable, now: float | None = None) -> str:
    share_dir(home).mkdir(parents=True, exist_ok=True)
    shutil.copyfile(TEMPLATE, capture_path(home))
    usage.sweep(home, time.time() if now is None else now)
    try:
        _off_told(home).unlink()          # install always leaves a statusLine, so the notice
    except OSError:                       # that none is set no longer applies
        pass
    data = installer._read_settings(home)
    current = data.get("statusLine")
    if is_ours(current):
        wanted = f'"{python}" "{capture_path(home)}"'
        if current.get("command") != wanted:
            # The interpreter this install runs as has moved (or the machine's default
            # changed) since the last install: rewrite the command in place. The chain file
            # already holds the statusLine from before context-gate, so it stays untouched.
            _backup_settings(home)
            data["statusLine"] = {**current, "command": wanted}
            installer._write_settings(home, data)
            return "usage capture already installed; capture.py and the interpreter path refreshed"
        return "usage capture already installed; capture.py refreshed"
    _wrap(home, data, current, python)
    return "usage capture installed as the statusLine, chained to the one you had"


def uninstall(home: Path) -> str:
    chain = usage.read_json(chain_path(home))
    data = installer._read_settings(home)
    ours = is_ours(data.get("statusLine"))
    if chain is None:
        if not ours:
            return "usage capture is not installed"
        # Stuck: the statusLine still points at capture.py but the chain file that remembers
        # what it wrapped is gone or unreadable. There is nothing to restore to, so clear it
        # rather than leave the statusLine pointing at a tool that was just removed.
        _backup_settings(home)
        data.pop("statusLine", None)
        installer._write_settings(home, data)
        return ("usage capture removed, but the statusLine you had before could not be "
                 "restored (chain file missing); statusLine is now unset")
    if ours:
        saved = chain.get("statusLine")
        _backup_settings(home)
        if saved is None:
            data.pop("statusLine", None)
        else:
            data["statusLine"] = saved
        installer._write_settings(home, data)
    chain_path(home).unlink()
    return "usage capture removed; your statusLine is as it was"


def rewrap(home: Path, python: str = sys.executable) -> str | None:
    if not chain_path(home).exists():
        return None
    data = installer._read_settings(home)
    current = data.get("statusLine")
    if is_ours(current):
        return None
    if current is None:
        marker = _off_told(home)
        if marker.exists():
            return None               # already said once this machine; stay silent
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        return "usage data off: no statusLine is set; run govern usage install"
    _wrap(home, data, current, python)
    try:
        _off_told(home).unlink()
    except OSError:
        pass
    return ("usage: statusLine was changed by another tool; context-gate re-wrapped it "
            "(govern usage uninstall to stop)")


def resolve(cfg, root: Path) -> dict:
    # `alerts_path`, `alerts_mtime` and `alerts_size` let the per-call hook reload the file when
    # it is edited mid-session; `alerts_file` is the name the agent is shown.
    out = {"enabled": cfg.checks["usage"].level != "off", "alerts_file": None, "alerts": [],
           "context_step": usage.CONTEXT_STEP, "error": None, "alerts_path": None,
           "alerts_mtime": None, "alerts_size": None}
    if not out["enabled"]:
        return out
    project = root / GOV_DIR / ALERTS_NAME
    prof = getattr(cfg.profile, "path", None)
    if project.is_file():
        path, shown = project, f"{GOV_DIR}/{ALERTS_NAME}"
    elif prof is not None and (Path(prof) / ALERTS_NAME).is_file():
        path, shown = Path(prof) / ALERTS_NAME, f"profile {ALERTS_NAME}"
    else:
        return out
    out["alerts_file"], out["alerts_path"] = shown, str(path.resolve())
    try:
        st = path.stat()                                  # before the read: a later edit reloads
        # Modified under SETTLE_SECONDS ago: still loaded (there is no earlier state to keep), but
        # mtime 0 so the first settled call reads it again.
        settled = time.time() - st.st_mtime_ns / 1e9 >= usage.SETTLE_SECONDS
        out["alerts_mtime"], out["alerts_size"] = (st.st_mtime_ns if settled else 0), st.st_size
        out["alerts"], out["context_step"] = usage.load_alerts(path)
    except (usage.AlertsError, OSError) as exc:
        out["error"] = str(exc)
    return out
