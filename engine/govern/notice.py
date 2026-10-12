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

from govern import __version__, layout, versions

ORANGE, RESET = "\033[38;5;208m", "\033[0m"
# One whole line of `git ls-remote --tags`: the ref is read from its start, so a tag named
# `x/refs/tags/v1.2.3` is not release 1.2.3.
TAG_RE = re.compile(r"\S+\trefs/tags/v(\d+)\.(\d+)\.(\d+)")
DAY, RETRY = 24 * 3600, 3600
NO_UPDATE_CHECK = "CONTEXT_GATE_NO_UPDATE_CHECK"


def _key(version: str) -> tuple:
    """A release's order; () for anything else, a beta included: what this module offers as
    an upgrade is always a release."""
    return versions.key(version) if versions.is_stable(version) else ()


def installed_versions(home: Path) -> list[str]:
    d = layout.engines_dir(home)
    return [p.name for p in d.iterdir() if _key(p.name) and (p / "govern").is_dir()] \
        if d.is_dir() else []


def installed_betas(home: Path) -> list[str]:
    d = layout.engines_dir(home)
    return [p.name for p in d.iterdir()
            if versions.is_beta(p.name) and (p / "govern" / "cli.py").is_file()] \
        if d.is_dir() else []


def local_beta(root: Path, home: Path) -> str | None:
    """The beta this project runs on this machine: the one its local.toml names, when that
    engine is installed (bin/govern runs the committed pin otherwise). None for a project with
    no local.toml, which is every stable project."""
    import tomllib
    try:
        with (root / layout.LOCAL).open("rb") as fh:
            beta = tomllib.load(fh)["governance"]["engine"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return beta if isinstance(beta, str) and beta in installed_betas(home) else None


def released_versions(source: str, home: Path, fresh: bool = False) -> list[str]:
    """Release tags at the source, from a cache refreshed at most daily (hourly after a
    failure, so an offline machine does not retry on every run). A source starting with "-"
    is never asked: git would read it as an option, and some options name a command to run."""
    if os.environ.get(NO_UPDATE_CHECK) or (isinstance(source, str) and source.startswith("-")):
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
        res = subprocess.run(["git", "ls-remote", "--tags", "--refs", "--", source],
                             capture_output=True, text=True, timeout=5,
                             env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        ok = res.returncode == 0
        versions = [".".join(m.groups()) for line in res.stdout.splitlines()
                    if (m := TAG_RE.fullmatch(line))] if ok else []
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
    found = installed_versions(home) + (released_versions(source, home, fresh) if source else [])
    stable = [v for v in found if versions.is_stable(v)]
    return max(stable, key=_key) if stable else None


PLUGIN = layout.PLUGIN


def enabled_plugins(root: Path, home: Path) -> dict[str, bool]:
    """`enabledPlugins` as Claude Code merges it for this project: the user's settings, then the
    project's, then its settings.local.json, key by key, the later file winning. A file that is
    missing or unreadable, and a value that is not true or false, are skipped."""
    merged: dict[str, bool] = {}
    for settings in (home / ".claude" / "settings.json", root / ".claude" / "settings.json",
                     root / ".claude" / "settings.local.json"):
        try:
            enabled = json.loads(settings.read_text(encoding="utf-8-sig")).get("enabledPlugins", {})
            merged.update({k: v for k, v in enabled.items() if isinstance(v, bool)})
        except (OSError, ValueError, AttributeError):
            continue
    return merged


def both_plugins_warning(root: Path, home: Path) -> str | None:
    """The line a beta's plugin adds when the stable plugin is enabled here too: Claude Code
    runs the hooks of both, so every hook runs twice. None from a release, which leaves the
    saying to the beta."""
    if not versions.is_beta(__version__):
        return None
    enabled = enabled_plugins(root, home)
    if enabled.get(layout.SKILLS_DIR_PLUGIN_ID) and enabled.get(layout.PLUGIN_ID):
        return (f"{layout.DISPLAY_NAME}: both the beta and the stable plugin are enabled here, so every hook "
                f"runs twice; govern beta on {__version__} re-applies the switch")
    return None


def _rewrite_switch(root: Path, edit) -> bool:
    """Rewrite `<root>/.claude/settings.local.json`'s enabledPlugins with `edit`, which gets the
    key/value pairs and returns the new ones, or None to leave the file alone. The file's own BOM
    and line endings are kept, every other key stays where it was, and the write is atomic.
    False, with nothing changed, for any other state: no file, a file that cannot be read or is
    not a JSON object, or a write that fails."""
    path = root / ".claude" / "settings.local.json"
    tmp = path.with_name(path.name + ".tmp")
    try:
        raw = path.read_bytes()
        data = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(data, dict) or not isinstance(data.get("enabledPlugins"), dict):
            return False
        changed = edit(data["enabledPlugins"])
        if changed is None:
            return False
        data["enabledPlugins"] = changed
        text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        if b"\r\n" in raw:
            text = text.replace("\n", "\r\n")
        try:
            with tmp.open("w", encoding="utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8",
                          newline="") as fh:
                fh.write(text)
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    except (OSError, ValueError, RecursionError):
        return False
    return True


def correct_old_launcher_switch(root: Path) -> str | None:
    """An older launcher enables `context-gate@skills-dir`, which no plugin has now. When the project's settings.local.json has that id true and not the beta
    plugin's, set the second in the first's place and say so in the line returned; None, and
    nothing changed, in any other state. The caller prints the line and goes on, whatever
    happens here."""
    old, new = layout.OLD_SKILLS_DIR_PLUGIN_ID, layout.SKILLS_DIR_PLUGIN_ID

    def edit(enabled: dict) -> dict | None:
        if enabled.get(old) is not True or enabled.get(new) is True:
            return None
        # The new id where the old one was, so the old one is gone in the same write; a new id
        # that was there as false is set by that.
        out: dict = {}
        for key, value in enabled.items():
            if key == old:
                out[new] = True
            elif key != new:
                out[key] = value
        return out

    if not _rewrite_switch(root, edit):
        return None
    return (f"{layout.DISPLAY_NAME}: .claude/settings.local.json enabled {old}, the name the beta "
            f"plugin had before it had one of its own; it now enables {new}; restart Claude Code "
            "to load it")


def remove_beta_switch(root: Path) -> bool:
    """Remove the beta plugin's id from settings.local.json when it is true there: what an old
    launcher's `beta off` leaves behind. Whether the project is off the beta is the caller's to
    know. False, with nothing changed, when the id is not true or the file cannot be rewritten."""
    new = layout.SKILLS_DIR_PLUGIN_ID

    def edit(enabled: dict) -> dict | None:
        if enabled.get(new) is not True:
            return None
        return {k: v for k, v in enabled.items() if k != new}

    return _rewrite_switch(root, edit)


def plugin_available(root: Path, home: Path) -> bool:
    """Whether the Claude Code plugin is present for this project: enabled in the settings
    Claude Code merges for it, or installed as a local plugin that is on by default (an install
    with `defaultEnabled: false` loads only where the settings turn it on)."""
    enabled = enabled_plugins(root, home)
    if any(k.split("@")[0] in (PLUGIN, layout.BETA_PLUGIN) and v for k, v in enabled.items()):
        return True
    # The beta's own folder, and the release's: a plugin is available from the one whose
    # manifest is there, unless the project's settings turn its id off.
    for folder, plugin_id in ((layout.BETA_PLUGIN, layout.SKILLS_DIR_PLUGIN_ID),
                              (PLUGIN, layout.OLD_SKILLS_DIR_PLUGIN_ID)):
        local = home / ".claude" / "skills" / folder / ".claude-plugin" / "plugin.json"
        if not local.is_file() or enabled.get(plugin_id) is False:
            continue
        try:
            if json.loads(local.read_text(encoding="utf-8-sig")).get("defaultEnabled") is not False:
                return True
        except (OSError, ValueError, AttributeError):
            return True
    return False


def upgrade_command(root: Path, home: Path) -> str:
    """The plugin's skill when the plugin is present, else `bin/upgrade`."""
    if plugin_available(root, home):
        return f"/{PLUGIN}:upgrade"
    return f"python3 {layout.GOV_DIR}/bin/upgrade"


def for_project(root: Path, home: Path, running: str | None = None) -> str | None:
    """The notice for a governed project, from its config alone: the gate calls it after a
    command, and the plugin's session-start hook calls it before any command runs.

    `running` is the engine the project runs. The gate passes its own version. The hook passes
    nothing, because the engine it runs is the plugin's, not the project's: what the project
    runs is then read from its files, the beta local.toml names or else the committed pin."""
    import tomllib
    try:
        with (root / layout.CONFIG).open("rb") as fh:
            gov = tomllib.load(fh).get("governance", {})
    except (OSError, ValueError):
        return None
    pin, source = gov.get("engine"), gov.get("source")
    if not isinstance(pin, str):
        return None
    if running is None:
        running = local_beta(root, home)
    if running and versions.is_beta(running):
        newest = newest_available(home, source)
        if newest and versions.key(newest) > versions.key(running):
            return (f"{layout.DISPLAY_NAME} {newest} is out (this machine runs beta {running}): "
                    f"govern beta off, then {upgrade_command(root, home)}.")
        # One local beta plugin per machine: once a newer beta is installed, a project still on
        # this one runs the old engine under the new plugin's hooks.
        newer = [v for v in installed_betas(home) if versions.key(v) > versions.key(running)]
        if newer:
            newest = max(newer, key=versions.key)
            return (f"{layout.DISPLAY_NAME}: beta {newest} is installed (this project runs beta "
                    f"{running}): govern beta on {newest}")
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
