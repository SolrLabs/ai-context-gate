#!/usr/bin/env python3
"""context-gate usage capture, installed as Claude Code's statusLine by `govern usage install`.

Saves four fields of the statusline payload for this session, then runs the statusline the user
had before, with the same input, and passes its output and exit code through. The capture step
can never break the user's statusline: any error in it is swallowed. When the switch file
`statusline-row.json` holds {"row": true}, it prints a row of its own instead of the chain's. On Windows the user's
statusline runs through the shell Claude Code itself runs statuslines with.
"""
import datetime
import json
import math
import ntpath
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

HOME = Path(os.environ.get("HOME") or Path.home())
SHARE = HOME / ".local" / "share" / "context-gate"
STATE = HOME / ".local" / "state" / "context-gate" / "usage"
# The Claude profile folder this statusline runs for; "" is the default profile. Two accounts on
# one machine each keep their own history lines (`p`).
PROFILE = os.path.abspath(os.path.expanduser(v)) if (v := os.environ.get("CLAUDE_CONFIG_DIR", "").strip()) else ""


def capture(raw: bytes) -> None:
    data = json.loads(raw)
    sid = data["session_id"]
    if not isinstance(sid, str) or not sid or "/" in sid or "\\" in sid:
        return
    snap = {"context_window": {"used_percentage": (data.get("context_window") or {}).get("used_percentage")},
            "rate_limits": data.get("rate_limits") or {},
            "transcript_path": data.get("transcript_path"),
            "captured_at": int(time.time())}
    STATE.mkdir(parents=True, exist_ok=True)
    path = STATE / f"{sid}.json"
    tmp = STATE / f".{sid}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(snap), encoding="utf-8")
    os.replace(tmp, path)


def chained() -> str | None:
    try:
        sl = json.loads((SHARE / "statusline-chain.json").read_text(encoding="utf-8")).get("statusLine")
    except (OSError, ValueError, AttributeError):
        return None
    return sl.get("command") if isinstance(sl, dict) else None


def git_bash() -> str | None:
    """bash.exe of Git for Windows, or None. Never the `bash` on PATH: on Windows that can be
    System32\\bash.exe, which is WSL, a different machine as far as the user's paths go."""
    env = os.environ
    mine = env.get("CLAUDE_CODE_GIT_BASH_PATH")      # Claude Code's own override
    if mine and os.path.isfile(mine):
        return mine
    found = []
    git = on_path("git")
    if git:
        # git.exe sits in <root>\cmd, <root>\bin or <root>\mingw64\bin; bash is <root>\bin\bash.exe.
        here = ntpath.dirname(git)
        above = ntpath.dirname(here)
        name = ntpath.basename(here).lower()
        if name == "bin" and ntpath.basename(above).lower() == "mingw64":
            found.append(ntpath.dirname(above))
        elif name in ("cmd", "bin"):
            found.append(above)
    for var, sub in (("ProgramFiles", "Git"), ("ProgramFiles(x86)", "Git"),
                     ("LocalAppData", ntpath.join("Programs", "Git"))):
        if env.get(var):
            found.append(ntpath.join(env[var], sub))
    for root in found:
        bash = ntpath.join(root, "bin", "bash.exe")
        if os.path.isfile(bash):
            return bash
    return None


def on_path(name: str) -> str | None:
    """shutil.which, but only an absolute hit. On Windows which() searches the current
    directory first and returns that hit relative: never run a program the repo brought."""
    found = shutil.which(name)
    return found if found and ntpath.isabs(found) else None


def windows_argv(cmd: str) -> tuple[list[str], dict | None] | None:
    """(argv, env) that runs `cmd` on Windows the way Claude Code runs a statusline: through Git
    Bash when it is installed, else through PowerShell. None when neither is found (or a lookup
    fails), and the caller runs it as before. Bash gets the command in the environment, not the
    argument list, so neither list2cmdline nor the MSYS runtime touches it (`~`, `\\`); env is
    None for PowerShell, which inherits."""
    try:
        bash = git_bash()
        if bash:
            return [bash, "-c", 'eval "$CONTEXT_GATE_STATUSLINE"'], {**os.environ, "CONTEXT_GATE_STATUSLINE": cmd}
        # Claude Code's docs say "PowerShell" without saying which: PowerShell 7 (pwsh) when it
        # is on PATH, else the Windows PowerShell every Windows has.
        exe = on_path("pwsh") or on_path("powershell.exe")
        if exe:
            return [exe, "-NoProfile", "-NonInteractive", "-Command", cmd], None
    except Exception:
        pass
    return None


RESET, BOLD, DIM, RED, GREEN, YELLOW, BLUE = "0", "1", "2", "31", "32", "33", "34"
GREY = "38;5;243"                                  # #767676: 4.5:1 on white, 4.6:1 on black
FIVE_HOUR, WEEK = 18000, 604800
FIVE_HOUR_LOOKBACK, WEEK_LOOKBACK = 3600, 3600     # seconds of recent use the projection looks at
MIN_SPAN = 900                                     # under this much history, use the window average
ANSI = re.compile(r"\x1b\[[0-9;]*m")
HISTORY = STATE / "history.jsonl"
DAYS = STATE / "days.json"       # {profile: {date: [[r, first, last], ...]}}: the weekly figure at each local day's start
DAY_TIMES = STATE / "day-times.json"   # {profile: {date: {"<r>": epoch}}}: when that first figure was seen
WEEK_SETTINGS = SHARE / "usage-week.toml"   # the weekday weights, the day's end and the yellow band
LATER_CUT = 0.10                 # the part of what is left that today may overrun and still be yellow
DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAYS_KEEP = 35                  # days of dates kept
HISTORY_MAX_LINES, HISTORY_KEEP = 500, 7200
WINDOWS = (("5h", "five_hour"), ("7d", "seven_day"))


def row_on() -> bool:
    try:
        data = json.loads((SHARE / "statusline-row.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and list(data) == ["row"] and data["row"] is True


def num(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def weight_of(value) -> float:
    n = num(value)
    return n if n is not None and 0 <= n <= 10 else 1.0


def week_settings(profile: str) -> tuple[dict[str, float], int, float]:
    """(weights by weekday `mon`..`sun`, day_end in minutes after midnight, later_cut) for `profile`:
    `[weekly]` of the settings file, then the matching `[weekly.profiles."<folder>"]` over it key by
    key. A bad value falls back to its default, a bad file to every default. Never raises."""
    try:
        text = Path(WEEK_SETTINGS).read_bytes().decode("utf-8-sig")      # a BOM (older Notepad) is not TOML
        weekly = tomllib.loads(text).get("weekly")
        merged = dict(weekly) if isinstance(weekly, dict) else {}
        profiles = merged.get("profiles")
        here = os.path.normcase(profile)
        for key, table in (profiles.items() if isinstance(profiles, dict) else ()):
            if not isinstance(table, dict):
                continue
            path = os.path.normcase(os.path.abspath(os.path.expanduser(key)))
            if path == here or (profile == "" and path == os.path.normcase(os.path.abspath(os.path.expanduser("~/.claude")))):
                merged.update({k: table[k] for k in ("weights", "day_end", "later_cut") if k in table})
        weights = merged.get("weights")
        weights = weights if isinstance(weights, dict) else {}
        end = merged.get("day_end")
        found = re.fullmatch(r"(\d{1,2}):(\d{2})", end) if isinstance(end, str) else None
        minutes = int(found[1]) * 60 + int(found[2]) if found and int(found[2]) < 60 else 0
        cut = num(merged.get("later_cut"))
        return ({d: weight_of(weights.get(d, 1)) for d in DAY_NAMES},
                minutes if 1 <= minutes <= 1440 else 1440,
                cut if cut is not None and 0 <= cut <= 1 else LATER_CUT)
    except Exception:
        return {d: 1.0 for d in DAY_NAMES}, 1440, LATER_CUT


def context_color(p: float) -> str:
    return GREEN if p < 70 else YELLOW if p < 90 else RED


def pace_color(used: float, left: float, window: int, margin: int) -> str:
    elapsed = window - left
    if round(used) >= 100:                # 99.6 prints 100%, so it is full
        return RED
    if elapsed < 0.02 * window:
        return GREEN
    projected = used * window / elapsed
    return GREEN if projected <= 100 else YELLOW if projected <= 100 + margin else RED


def read_history() -> list[dict]:
    """Every parsable line of the history file, every profile's; a garbage line is skipped, a missing file is empty."""
    out = []
    try:
        text = HISTORY.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return out
    for line in text.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if (isinstance(item, dict) and item.get("w") in ("5h", "7d")
                and all((v := num(item.get(k))) is not None and math.isfinite(v) for k in ("t", "r", "u"))):
            out.append(item)
    return out


def read_days() -> dict:
    """The days file, parsed; {} when it is missing, unreadable or not an object."""
    try:
        data = json.loads(DAYS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def read_day_times() -> dict:
    """The day-times file, parsed; {} when it is missing, unreadable or not an object."""
    try:
        data = json.loads(DAY_TIMES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def day_time(times: dict, profile: str, r: float, date: str) -> float | None:
    """When the figure `day_start` returns was first seen, as epoch seconds, or None."""
    try:
        t = num(times[profile][date][str(int(r))])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    return t if t is not None and math.isfinite(t) else None


def write_day_time(date: str, r: int, now: float) -> None:
    """Store `now` as the time the first figure of `date` for the window `r` was seen, and drop dates
    past DAYS_KEEP."""
    times = read_day_times()
    mine = times.get(PROFILE)
    if not isinstance(mine, dict):
        mine = times[PROFILE] = {}
    stamps = mine.get(date)
    if not isinstance(stamps, dict):
        stamps = mine[date] = {}
    stamps[str(r)] = int(now)
    today = datetime.date.fromisoformat(date)
    for prof in list(times):
        dates = times[prof]
        for d in list(dates) if isinstance(dates, dict) else ():
            try:
                old = (today - datetime.date.fromisoformat(d)).days > DAYS_KEEP
            except ValueError:
                old = True
            if old:
                del dates[d]
    tmp = STATE / f".day-times.{os.getpid()}.tmp"
    try:
        tmp.write_text(json.dumps(times), encoding="utf-8")
        os.replace(tmp, DAY_TIMES)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def triple(item) -> tuple[int, float, float] | None:
    """(r, first, last) of a stored [r, first, last], or None when it is malformed."""
    if isinstance(item, (list, tuple)) and len(item) == 3:
        r, first, last = (num(v) for v in item)
        if r is not None and first is not None and last is not None and all(map(math.isfinite, (r, first, last))):
            return int(r), first, last
    return None


def day_start(days: dict, profile: str, r: float, date: str) -> float | None:
    """The weekly figure first seen on `date` (YYYY-MM-DD) for the window that resets at `r`, or None."""
    try:
        for item in days[profile][date]:
            t = triple(item)
            if t and t[0] == int(r):
                return t[1]
    except (KeyError, TypeError, ValueError, OverflowError):
        pass
    return None


def day_totals(days: dict, profile: str) -> dict[str, float]:
    """Per date, the weekly figure used that day: the sum of last minus first over its windows."""
    out = {}
    mine = days.get(profile) if isinstance(days, dict) else None
    for date, items in (mine.items() if isinstance(mine, dict) else ()):
        if isinstance(items, list):
            out[date] = sum(t[2] - t[1] for t in map(triple, items) if t)
    return out


def record_days(data: dict, now: float) -> None:
    """Note this payload's weekly figure in the days file: the first and the highest seen today for
    its window. The file is rewritten only when a window is new today or its highest figure rose."""
    win = (data.get("rate_limits") or {}).get("seven_day") or {}
    u, r = num(win.get("used_percentage")), num(win.get("resets_at"))
    if u is None or r is None or not math.isfinite(u) or not math.isfinite(r):
        return
    r = int(r)
    # a session reports limits only as of its last request: an idle one redraws a stale figure all night
    u = max([u] + [i["u"] for i in read_history()
                   if i.get("p", "") == PROFILE and i["w"] == "7d" and int(i["r"]) == r])
    date = time.strftime("%Y-%m-%d", time.localtime(now))
    days = read_days()
    mine = days.get(PROFILE)
    if not isinstance(mine, dict):
        mine = days[PROFILE] = {}
    items = mine.get(date)
    if not isinstance(items, list):
        items = mine[date] = []
    first = False
    for i, item in enumerate(items):
        t = triple(item)
        if t and t[0] == r:
            if u <= t[2]:
                return
            items[i] = [r, t[1], u]
            break
    else:
        items.append([r, u, u])
        first = True
    today = datetime.date.fromisoformat(date)
    for prof in list(days):
        dates = days[prof]
        for d in list(dates) if isinstance(dates, dict) else ():
            try:
                old = (today - datetime.date.fromisoformat(d)).days > DAYS_KEEP
            except ValueError:
                old = True
            if old:
                del dates[d]
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = STATE / f".days.{os.getpid()}.tmp"
    try:
        tmp.write_text(json.dumps(days), encoding="utf-8")
        os.replace(tmp, DAYS)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    if first:
        try:                              # the time is only a better rate: losing it loses nothing else
            write_day_time(date, r, now)
        except Exception:
            pass


def record(data: dict, now: float) -> None:
    """Append this payload's figures to the history shared by every session: one line per window
    per minute, and one more whenever the figure is higher than any recorded for that window; a
    figure below the highest recorded is never written."""
    try:
        record_days(data, now)
    except Exception:                 # the days file only feeds a later display: never stop the history
        pass
    limits = data.get("rate_limits") or {}
    items = read_history()
    fresh = []
    for w, key in WINDOWS:
        win = limits.get(key) or {}
        u, r = num(win.get("used_percentage")), num(win.get("resets_at"))
        if u is None or r is None:
            continue
        same = [i for i in items if i.get("p", "") == PROFILE and i["w"] == w and int(i["r"]) == int(r)]
        recent = any(i["t"] >= now - 60 for i in same)
        top = max((i["u"] for i in same), default=None)
        if top is None or u > top or (u == top and not recent):
            fresh.append(json.dumps({"t": int(now), "w": w, "r": int(r), "u": u, **({"p": PROFILE} if PROFILE else {})}))
    if not fresh:
        return
    STATE.mkdir(parents=True, exist_ok=True)
    with open(HISTORY, "a", encoding="utf-8") as fh:
        fh.write("".join(line + "\n" for line in fresh))      # one write: sessions do not interleave
    if len(items) + len(fresh) > HISTORY_MAX_LINES:
        keep = [i for i in read_history() if i["t"] >= now - HISTORY_KEEP]
        tmp = STATE / f".history.{os.getpid()}.tmp"
        try:
            tmp.write_text("".join(json.dumps({k: i[k] for k in ("t", "w", "r", "u", "p") if k in i}) + "\n" for i in keep),
                           encoding="utf-8")
            os.replace(tmp, HISTORY)     # an append between the read and the replace is lost: acceptable
        finally:
            try:
                tmp.unlink()             # gone after a replace; left behind only when it failed
            except OSError:
                pass


def history(w: str, r: float, now: float) -> list[tuple[float, float]]:
    """(t, u) of window `w` that resets at `r`, by time, each u the running maximum: within one
    window the true figure never goes down, so a lower one is a session that has not caught up."""
    rows = sorted((i["t"], i["u"]) for i in read_history()
                  if i.get("p", "") == PROFILE and i["w"] == w and int(i["r"]) == int(r))
    out, top = [], 0.0
    for t, u in rows:
        top = max(top, u)
        out.append((t, top))
    return out


def merged(data: dict, now: float) -> dict:
    """{"5h": samples, "7d": samples} for the windows in the payload."""
    out = {}
    limits = data.get("rate_limits") or {}
    for w, key in WINDOWS:
        r = num((limits.get(key) or {}).get("resets_at"))
        if r is not None:
            out[w] = history(w, r, now)
    return out


def project(used: float, left: float, window: int, margin: int, samples: list, lookback: int, now: float):
    """(figure, color). The figure is the freshest any session has seen; the color projects the
    rate over the last `lookback` seconds to the reset. Under MIN_SPAN of history the color is the
    window's average."""
    if samples:
        used = max(used, samples[-1][1])
    then = None
    for t, u in samples:
        if t <= now - lookback:
            then = (t, u)
    if then is None and samples:
        then = samples[0]
    if then is None or now - then[0] < MIN_SPAN:
        return used, pace_color(used, left, window, margin)
    if round(used) >= 100:
        return used, RED
    projected = used + (used - then[1]) / (now - then[0]) * left
    return used, GREEN if projected <= 100 else YELLOW if projected <= 100 + margin else RED


def midnight(day: datetime.date) -> float:
    return time.mktime((day.year, day.month, day.day, 0, 0, 0, 0, 0, -1))


def allowance(used: float, r: float, now: float, weights: dict, day_end: int,
              start: float | None) -> tuple[float, float, bool]:
    """(today's allowance in percent points, today's end as an epoch, zero-day). The weekly window
    [r - WEEK, r) is shared over its local dates by weight; a date counts by the part of its real
    length (a DST day is 23 or 25 hours) that lies inside the window. `start` is the weekly figure
    when today began; None means `used`. Zero-day: today weighs nothing, or nothing is left to share."""
    first = r - WEEK
    today = datetime.date.fromtimestamp(now)
    w = W = share_today = 0.0
    day, last = today, datetime.date.fromtimestamp(r)
    while day <= last:
        begin, end = midnight(day), midnight(day + datetime.timedelta(days=1))
        share = max(0.0, min(end, r) - max(begin, first)) / (end - begin)
        weight = weights.get(DAY_NAMES[day.weekday()], 1.0)
        W += weight * share
        if day == today:
            w, share_today = weight, share
        day += datetime.timedelta(days=1)
    if first >= midnight(today):
        start = 0.0
    elif start is None:
        start = used
    if day_end >= 1440:
        ends = midnight(today + datetime.timedelta(days=1))
    else:
        ends = time.mktime((today.year, today.month, today.day, day_end // 60, day_end % 60, 0, 0, 0, -1))
    zero = w == 0 or W == 0
    return (0.0 if zero else (100 - start) * w * share_today / W), min(ends, r), zero


def weekly_part(used: float, r: float, now: float, samples: list, week: tuple | None) -> tuple[str, str]:
    """(text, color) of the weekly part: the figure, today's spend against today's allowance, colored
    by where today's pace would leave the figure when the day ends: the rate since the first figure
    seen today (`since`), or the last hour's when that is under MIN_SPAN old or missing. `week` is
    (weights, day_end, later_cut, start[, since]); a four-tuple has no `since`."""
    weights, day_end, later_cut, start, since = (tuple(week) + (None,))[:5] if week else ({}, 1440, LATER_CUT, None, None)
    paced = start if since is not None and start is not None and now - since >= MIN_SPAN else None
    if samples:
        used = max(used, samples[-1][1])
    if now >= r:                          # an idle session's payload from before the reset: nothing to pace
        return f"Weekly: {int(round(used))}%", RED if round(used) >= 100 else GREY
    then = None
    for t, u in samples:
        if t <= now - WEEK_LOOKBACK:
            then = (t, u)
    if then is None and samples:
        then = samples[0]
    allow, ends, zero = allowance(used, r, now, weights, day_end, start)
    if r - WEEK >= midnight(datetime.date.fromtimestamp(now)):
        start = 0.0                       # the window opened today
    elif start is None:
        start = used
    proj = used
    if paced is not None:
        proj = used + (used - paced) / (now - since) * max(0.0, ends - now)
    elif then is not None and now - then[0] >= MIN_SPAN:
        proj = used + (used - then[1]) / (now - then[0]) * max(0.0, ends - now)
    spent = max(0.0, used - start)
    rest = 100 - start - allow
    overrun = proj - (start + allow)
    head = f"Weekly: {int(round(used))}% · "
    if zero:
        text = head + (f"off day {int(round(spent))}%" if round(spent) >= 1 else "off day")
    else:
        text = head + f"today {int(round(spent))}/{int(round(allow))}%"
    if round(used) >= 100 or proj >= 100:
        return text, RED
    if zero:
        return text, YELLOW if round(spent) >= 1 else GREY
    return text, GREEN if overrun <= 0 else YELLOW if overrun <= later_cut * rest else RED


def render(data: dict, now: float, branch: str | None, hist: dict | None = None, width: int | None = None,
           week: tuple | None = None) -> str:
    """The row, drawn from the statusline payload and `hist` (merged()'s result). Pure: no I/O, no clock.
    With `width` the parts are packed into lines of at most that many visible columns. `week` is
    (weights, day_end, later_cut, start, since) for the weekly part, `since` optional; None means
    the defaults."""
    hist = hist or {}
    color = "NO_COLOR" not in os.environ

    def sgr(code: str) -> str:
        return f"\x1b[{code}m" if color else ""

    def paint(code: str, text: str) -> str:
        return f"{sgr(code)}{text}{sgr(RESET)}"

    parts = []
    cwd = (data.get("workspace") or {}).get("current_dir")
    if isinstance(cwd, str) and cwd.rstrip("\\/"):
        cwd = cwd.rstrip("\\/")
        name = (ntpath.basename if "\\" in cwd else os.path.basename)(cwd)
        if name:
            parts.append(paint(BLUE, name))
    if branch:
        parts.append(paint(GREEN, branch))
    model = (data.get("model") or {}).get("display_name")
    if isinstance(model, str) and model:
        parts.append(paint(YELLOW, model))
    ctx = num((data.get("context_window") or {}).get("used_percentage"))
    if ctx is not None:
        shown = int(round(ctx))           # color what is printed: 69.6 shows 70%, so it is yellow
        parts.append(paint(context_color(shown), f"Ctx: {shown}%"))
    limits = data.get("rate_limits") or {}
    five = limits.get("five_hour") or {}
    used, resets = num(five.get("used_percentage")), num(five.get("resets_at"))
    if used is not None and resets is not None:
        used, code = project(used, resets - now, FIVE_HOUR, 5, hist.get("5h") or [], FIVE_HOUR_LOOKBACK, now)
        k = max(0, min(10, int(round((FIVE_HOUR - (resets - now)) / FIVE_HOUR * 10))))
        full = max(0, min(10, int(used // 10)))
        cells = "█" * full + "░" * (10 - full)
        bar = cells[:k] + sgr(RESET) + paint(BOLD, "┃") + sgr(code) + cells[k:]
        stamp = time.strftime("%I:%M %p", time.localtime(resets)).lstrip("0")
        parts.append(paint(code, f"Usage: {int(round(used))}% {bar} → Reset: {stamp}"))
    seven = limits.get("seven_day") or {}
    used, resets = num(seven.get("used_percentage")), num(seven.get("resets_at"))
    if used is not None and resets is not None:
        text, code = weekly_part(used, resets, now, hist.get("7d") or [], week)
        parts.append(paint(code, text))
    sep = paint(DIM, " │ ")
    if width is None:
        return sep.join(parts)
    lines, line, have = [], [], 0
    for part in parts:
        size = len(ANSI.sub("", part))
        if line and have + 3 + size > width:
            lines.append(sep.join(line))
            line, have = [], 0
        have += size + (3 if line else 0)
        line.append(part)
    if line:
        lines.append(sep.join(line))
    return "\n".join(lines)


def branch(cwd) -> str | None:
    try:
        res = subprocess.run(["git", "-C", cwd, "branch", "--show-current"], capture_output=True,
                             timeout=1, text=True, encoding="utf-8")
    except Exception:
        return None
    return (res.stdout.strip() or None) if res.returncode == 0 else None


def columns() -> int | None:
    """The row's width: COLUMNS less two spare columns (notifications share the row), or None."""
    try:
        cols = int(os.environ.get("COLUMNS", ""))
    except ValueError:
        return None
    return cols - 2 if cols >= 20 else None


def week_args(data: dict, now: float) -> tuple | None:
    """render's `week`: this profile's settings and the figure the weekly window showed when today
    began, and when it was first seen. What cannot be had is left at its default."""
    try:
        weights, day_end, cut = week_settings(PROFILE)
    except Exception:
        return None
    start = since = None
    try:
        r = num(((data.get("rate_limits") or {}).get("seven_day") or {}).get("resets_at"))
        if r is not None:
            date = time.strftime("%Y-%m-%d", time.localtime(now))
            start = day_start(read_days(), PROFILE, r, date)
            since = day_time(read_day_times(), PROFILE, r, date)
    except Exception:
        pass
    return weights, day_end, cut, start, since


def row(raw: bytes) -> bytes | None:
    """The row as bytes, or None when it is off or cannot be drawn: the caller runs the chain."""
    try:
        if not row_on():
            return None
        data = json.loads(raw)
        cwd = (data.get("workspace") or {}).get("current_dir")
        found = branch(cwd) if isinstance(cwd, str) and cwd else None
        now = time.time()
        try:
            record(data, now)
        except Exception:                 # a history error only loses history
            pass
        try:
            hist = merged(data, now)
        except Exception:
            hist = {}
        return (render(data, now, found, hist, columns(), week_args(data, now)) + "\n").encode("utf-8")
    except Exception:
        return None


def main() -> int:
    raw = sys.stdin.buffer.read()
    try:
        capture(raw)
    except Exception:
        pass
    drawn = row(raw)
    if drawn is not None:
        sys.stdout.buffer.write(drawn)
        return 0
    cmd = chained()
    if not cmd:
        return 0
    found = windows_argv(cmd) if os.name == "nt" else None
    try:
        if found:
            res = subprocess.run(found[0], input=raw, capture_output=True, env=found[1])
        else:
            res = subprocess.run(cmd, shell=True, input=raw, capture_output=True)
    except Exception:                     # the shell could not be started: as with no chain
        return 0
    sys.stdout.buffer.write(res.stdout)
    sys.stderr.buffer.write(res.stderr)
    return res.returncode


if __name__ == "__main__":
    sys.exit(main())
