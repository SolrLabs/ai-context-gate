"""The upgrade notice: one line, after a command's own output, when a newer engine exists.

"Newer" means installed on this machine, or released at the project's `[governance] source`
(checked at most once a day, cached, with a short timeout, and silent when offline). The line
is orange when stderr is a terminal and NO_COLOR is unset, and plain otherwise, so an agent
reading the output through a pipe sees the same words without escape codes.

CONTEXT_GATE_NO_UPDATE_CHECK=1 skips the network check (tests set it).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from govern import __version__, layout

ORANGE, RESET = "\033[38;5;208m", "\033[0m"
TAG_RE = re.compile(r"refs/tags/v(\d+)\.(\d+)\.(\d+)$")
DAY, RETRY = 24 * 3600, 3600
NO_UPDATE_CHECK = "CONTEXT_GATE_NO_UPDATE_CHECK"


def _key(version: str) -> tuple:
    parts = version.split(".")
    return tuple(int(p) for p in parts) if all(p.isdigit() for p in parts) else ()


def installed_versions(home: Path) -> list[str]:
    d = layout.engines_dir(home)
    return [p.name for p in d.iterdir() if _key(p.name) and (p / "govern").is_dir()] \
        if d.is_dir() else []


def released_versions(source: str, home: Path, fresh: bool = False) -> list[str]:
    """Release tags at the source, from a cache refreshed at most daily (hourly after a
    failure, so an offline machine does not retry on every run)."""
    if os.environ.get(NO_UPDATE_CHECK):
        return []
    cache = home / ".cache" / layout.TOOL / "releases.json"
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    entry = data.get(source, {})
    age = time.time() - entry.get("checked", 0)
    if not fresh and age < (DAY if entry.get("ok") else RETRY):
        return entry.get("versions", [])
    try:
        res = subprocess.run(["git", "ls-remote", "--tags", "--refs", source],
                             capture_output=True, text=True, timeout=5,
                             env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        ok = res.returncode == 0
        versions = [".".join(m.groups()) for line in res.stdout.splitlines()
                    if (m := TAG_RE.search(line))] if ok else []
    except (OSError, subprocess.SubprocessError):
        ok, versions = False, []
    data[source] = {"checked": time.time(), "ok": ok, "versions": versions or entry.get("versions", [])}
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(data, indent=1), encoding="utf-8")
    except OSError:
        pass
    return data[source]["versions"]


def resolve(pin: str, home: Path) -> str | None:
    """The engine a pin actually runs on this machine: the pin itself when exact, else the
    newest installed release in its series (what bin/govern picks)."""
    if len(pin.split(".")) == 3:
        return pin
    in_series = [v for v in installed_versions(home) if v.startswith(pin + ".")]
    return max(in_series, key=_key) if in_series else None


def newest_available(home: Path, source: str | None, fresh: bool = False) -> str | None:
    versions = installed_versions(home) + (released_versions(source, home, fresh) if source else [])
    return max(versions, key=_key) if versions else None


PLUGIN = layout.PLUGIN


def plugin_available(root: Path, home: Path) -> bool:
    """Whether the Claude Code plugin is present for this project: installed as a local plugin,
    or enabled in the project's or the user's settings."""
    if (home / ".claude" / "skills" / PLUGIN / ".claude-plugin" / "plugin.json").is_file():
        return True
    for settings in (root / ".claude" / "settings.json", root / ".claude" / "settings.local.json",
                     home / ".claude" / "settings.json"):
        try:
            enabled = json.loads(settings.read_text(encoding="utf-8")).get("enabledPlugins", {})
        except (OSError, ValueError, AttributeError):
            continue
        if any(k.split("@")[0] == PLUGIN and v for k, v in enabled.items()):
            return True
    return False


def upgrade_command(root: Path, home: Path) -> str:
    """The plugin's skill when the plugin is present, else `bin/upgrade`."""
    if plugin_available(root, home):
        return f"/{PLUGIN}:upgrade"
    return f"python3 {layout.GOV_DIR}/bin/upgrade"


def for_project(root: Path, home: Path, running: str | None = None) -> str | None:
    """The notice for a governed project, from its config alone: the gate calls it after a
    command, and the plugin's session-start hook calls it before any command runs."""
    import tomllib
    try:
        with (root / layout.CONFIG).open("rb") as fh:
            gov = tomllib.load(fh).get("governance", {})
    except (OSError, ValueError):
        return None
    pin, source = gov.get("engine"), gov.get("source")
    if not isinstance(pin, str):
        return None
    exact = len(pin.split(".")) == 3
    if running:
        current = running
    elif exact:
        current = pin
    else:   # a series pin runs the newest installed release in the series
        in_series = [v for v in installed_versions(home) if v.startswith(pin + ".")]
        current = max(in_series, key=_key) if in_series else __version__
    newest = newest_available(home, source)
    if newest and _key(newest) > _key(current):
        return (f"{layout.DISPLAY_NAME} {newest} available (this project runs {current}). "
                f"Use {upgrade_command(root, home)} to upgrade.")
    if not exact:
        return (f"{layout.DISPLAY_NAME}: this project pins the series {pin}, so machines can differ. "
                f"Use {upgrade_command(root, home)} to pin {current} exactly.")
    return None


def message(ctx) -> str | None:
    return for_project(ctx.root, ctx.home, __version__)


def emit(ctx) -> None:
    try:
        msg = message(ctx)
    except Exception:   # a notice must never break the command it follows
        return
    if not msg:
        return
    if sys.stderr.isatty() and not os.environ.get("NO_COLOR"):
        msg = f"{ORANGE}{msg}{RESET}"
    sys.stdout.flush()   # after the command's own output, even when stdout is a buffered pipe
    print(msg, file=sys.stderr)
