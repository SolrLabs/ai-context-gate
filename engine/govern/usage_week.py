"""`govern usage weights`: show the weekly allowance settings in effect, report a bad settings file,
and suggest weights from the days recorded. The math stays in capture.py, which is loaded here."""
from __future__ import annotations

import datetime
import os
import re
import tomllib
import types
from pathlib import Path

from govern import usage_setup

FILE = "usage-week.toml"
DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
KEYS = ("weights", "day_end", "later_cut")
MIN_DAYS = 14
DEFAULT_KEY = "~/.claude"


def load_capture(home: Path) -> types.ModuleType:
    """capture.py as a module (run with `exec`, so no bytecode is left beside the template), its
    paths pointed at `home`."""
    template = usage_setup.TEMPLATE
    mod = types.ModuleType("capture_template")
    mod.__file__ = str(template)
    exec(compile(template.read_text(encoding="utf-8"), str(template), "exec"), mod.__dict__)
    mod.SHARE = usage_setup.share_dir(home)
    mod.STATE = home / ".local" / "state" / "context-gate" / "usage"
    mod.WEEK_SETTINGS = mod.SHARE / FILE
    mod.DAYS = mod.STATE / "days.json"
    mod.DAY_TIMES = mod.STATE / "day-times.json"
    return mod


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(os.path.expanduser(path)))


def profile(arg: str | None) -> str:
    """The profile as capture.PROFILE spells it: "" when unset or blank, else an absolute path."""
    value = (arg if arg is not None else os.environ.get("CLAUDE_CONFIG_DIR", "")).strip()
    return os.path.abspath(os.path.expanduser(value)) if value else ""


def is_default(prof: str) -> bool:
    """Whether the profile is the default folder, spelled "" or as the path of ~/.claude."""
    return prof == "" or _norm(prof) == _norm(DEFAULT_KEY)


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _show(v) -> str:
    if isinstance(v, str):
        return f'"{v}"'
    if isinstance(v, bool):
        return "true" if v else "false"
    return f"{v:g}" if _is_num(v) else str(v)


def _minutes(v) -> int | None:
    found = re.fullmatch(r"(\d{1,2}):(\d{2})", v) if isinstance(v, str) else None
    if not found or int(found[2]) >= 60:
        return None
    total = int(found[1]) * 60 + int(found[2])
    return total if 1 <= total <= 1440 else None


def _check_table(table: dict, label: str) -> list[str]:
    out = []
    for key, value in table.items():
        if key == "profiles" and label == "[weekly]":
            continue
        if key not in KEYS:
            out.append(f"{FILE}: {label} {key}: unknown key")
        elif key == "weights":
            if not isinstance(value, dict):
                out.append(f"{FILE}: {label} weights = {_show(value)}: a table of weekdays")
                continue
            for day, w in value.items():
                if day not in DAY_NAMES:
                    out.append(f"{FILE}: {label} weights.{day} = {_show(w)}: a weekday is mon to sun")
                elif not _is_num(w) or not 0 <= w <= 10:
                    out.append(f"{FILE}: {label} weights.{day} = {_show(w)}: a weight is 0 to 10")
        elif key == "day_end":
            if _minutes(value) is None:
                out.append(f"{FILE}: {label} day_end = {_show(value)}: HH:MM from 00:01 to 24:00")
        elif not _is_num(value) or not 0 <= value <= 1:
            out.append(f"{FILE}: {label} later_cut = {_show(value)}: a number from 0 to 1")
    return out


def check(path: Path) -> list[str]:
    """One message per fault in the settings file; none when it is missing or sound."""
    try:
        data = tomllib.loads(path.read_bytes().decode("utf-8-sig"))
    except FileNotFoundError:
        return []
    except tomllib.TOMLDecodeError as exc:
        return [f"{FILE}: not valid TOML: {exc}"]
    except (OSError, UnicodeDecodeError) as exc:
        return [f"{FILE}: cannot read: {exc}"]
    weekly = data.get("weekly")
    if weekly is None:
        return []
    if not isinstance(weekly, dict):
        return [f"{FILE}: weekly = {_show(weekly)}: a table"]
    out = _check_table(weekly, "[weekly]")
    profiles = weekly.get("profiles", {})
    if not isinstance(profiles, dict):
        return out + [f"{FILE}: [weekly] profiles = {_show(profiles)}: a table of profile folders"]
    seen: dict[str, str] = {}
    for key, table in profiles.items():
        label = f'[weekly.profiles."{key}"]'
        first = seen.setdefault(_norm(key), label)
        if first != label:
            out.append(f"{FILE}: {first} and {label} name the same folder")
        if isinstance(table, dict):
            out += _check_table(table, label)
        else:
            out.append(f"{FILE}: {label}: a table")
    return out


def hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def describe(home: Path, profile_arg: str | None) -> list[str]:
    """The lines `govern usage weights` prints for the profile, or the faults (first line starts
    `usage-week.toml: `)."""
    cap = load_capture(home)
    faults = check(cap.WEEK_SETTINGS)
    if faults:
        return faults
    prof = profile(profile_arg)
    weights, end, cut = cap.week_settings(prof)
    settings = str(cap.WEEK_SETTINGS) if cap.WEEK_SETTINGS.is_file() else "none, every day weighs 1"
    return [f"settings: {settings}",
            f"profile: {'default (~/.claude)' if is_default(prof) else prof}",
            "weights: " + ", ".join(f"{d} {weights[d]:g}" for d in DAY_NAMES),
            f"day_end: {hhmm(end)}",
            f"later_cut: {cut:g}"]


def suggest(home: Path, profile_arg: str | None) -> tuple[int, list[str]]:
    """(exit code, lines): a table of weights from the days recorded for the profile."""
    cap = load_capture(home)
    faults = check(cap.WEEK_SETTINGS)
    if faults:
        return 1, faults
    prof = profile(profile_arg)
    days = cap.read_days()
    keys = ["", os.path.abspath(os.path.expanduser(DEFAULT_KEY)), prof] if is_default(prof) else [prof]
    today = datetime.date.today()
    totals: dict[datetime.date, float] = {}
    for key in keys:
        for date, used in cap.day_totals(days, key).items():
            try:
                day = datetime.date.fromisoformat(date)
            except ValueError:
                continue
            if day != today:
                totals[day] = max(used, totals.get(day, used))
    if len(totals) < MIN_DAYS:
        return 1, [f"not enough history: {len(totals)} days, needs {MIN_DAYS}"]
    by_day: dict[int, list[float]] = {i: [] for i in range(7)}
    for date, used in totals.items():
        by_day[date.weekday()].append(used)
    means = [sum(v) / len(v) if v else 0.0 for v in by_day.values()]
    top = max(means)
    if top <= 0:
        return 1, ["no use recorded"]
    cells = ", ".join(f"{d} = {round(m / top, 1):g}" for d, m in zip(DAY_NAMES, means))
    return 0, [f'[weekly.profiles."{DEFAULT_KEY if is_default(prof) else prof}"]', "weights = { " + cells + " }"]
