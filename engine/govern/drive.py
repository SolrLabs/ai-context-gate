"""`govern build drive`: one fresh headless coordinator session per build step, until the plan is
done, a person is needed, the weekly usage cap is near or a limit is reached.

Each session resumes from `govern build status`, the ledger and git; nothing is carried between
sessions but what those hold. The measurements of every session go to `drive_log`."""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import govern
from govern import coordinator_runs, drive_log, usage, usage_week, versions
from govern.build import _say as _say_raw

PROMPT = Path(__file__).resolve().parent / "templates" / "drive-prompt.md"
LOOP = "LOOP: "
KILL_AFTER = 10.0
PROBE_TIMEOUT = 120
PCT_RANGE = (20, 90)
DEFAULT_PCT = 50
DOING_EVERY = 2.0                   # seconds between writes of what the session is doing
STATUS_EVERY = 60                   # seconds between refreshes of the plan's progress
DOING_CHARS = 120
SKILLS = ("context-gate", "context-gate-beta")


class _Interrupted(Exception):
    """SIGTERM or SIGINT reached the driver while it ran."""


def _say(lines: list[str]) -> None:
    _say_raw(lines)
    sys.stdout.flush()


def _events(lines) -> list[dict]:
    out = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            out.append(event)
    return out


def stream_reading(lines) -> float | None:
    """The seven-day usage, in percent, of the first `rate_limit_event` in a stream."""
    for event in _events(lines):
        if event.get("type") != "rate_limit_event":
            continue
        info = event.get("rate_limit_info")
        windows = info.get("unifiedWindows") if isinstance(info, dict) else None
        week = windows.get("seven_day") if isinstance(windows, dict) else None
        value = week.get("utilization") if isinstance(week, dict) else None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value) * 100
        return None
    return None


def stream_reset(lines) -> float | None:
    """The seven-day window's reset, as epoch seconds, from the first `rate_limit_event` of a
    stream that carries one next to the utilization; None when it does not."""
    for event in _events(lines):
        if event.get("type") != "rate_limit_event":
            continue
        info = event.get("rate_limit_info")
        windows = info.get("unifiedWindows") if isinstance(info, dict) else None
        week = windows.get("seven_day") if isinstance(windows, dict) else None
        if not isinstance(week, dict):
            return None
        for key in ("resets_at", "resetsAt", "reset_at", "resetAt"):
            value = week.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                return float(value) / 1000 if value > 1e11 else float(value)
        return None
    return None


def _argv(claude: str, *args: str) -> list[str]:
    return [sys.executable, claude, *args] if claude.endswith(".py") else [claude, *args]


def probe_reading(claude: str, cwd: Path | None = None) -> tuple[float | None, str | None]:
    """The weekly usage as one cheap throwaway session reports it: (reading, why). `why` says
    the probe did not run or failed; a probe that ran cleanly with no event gives (None, None)."""
    reading, _reset, why, _commands = probe_full(claude, cwd)
    return reading, why


def probe_full(claude: str, cwd: Path | None = None) -> tuple[float | None, float | None,
                                                               str | None, list[str] | None]:
    """`probe_reading`, the seven-day reset the same event carries, and the commands the init
    event lists (a leading `/` stripped; None when the stream has none): (reading, reset, why,
    commands)."""
    argv = _argv(claude, "-p", "Reply with OK.", "--model", "haiku", "--max-turns", "1",
                 "--output-format", "stream-json", "--verbose")
    try:
        res = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=PROBE_TIMEOUT,
                             cwd=str(cwd) if cwd else None)
    except subprocess.TimeoutExpired:
        return None, None, f"timeout after {PROBE_TIMEOUT}s", None
    except (OSError, subprocess.SubprocessError) as exc:
        return None, None, f"did not start ({exc})", None
    lines = res.stdout.splitlines()
    commands = _init_commands(lines)
    reading = stream_reading(lines)
    if reading is not None:
        return reading, stream_reset(lines), None, commands
    if res.returncode != 0:
        return None, None, f"exit status {res.returncode}", commands
    return None, None, None, commands


def _init_commands(lines: list[str]) -> list[str] | None:
    """The `slash_commands` of the first init event in the stream, or None."""
    for event in _events(lines):
        if event.get("type") == "system" and event.get("subtype") == "init":
            found = event.get("slash_commands")
            if isinstance(found, list):
                return [c[1:] if c.startswith("/") else c for c in found if isinstance(c, str)]
            return None
    return None


def over_cap(readings: list[float], cap: float) -> bool:
    """True with no reading; else when the last reading plus the largest rise one step has
    caused reaches the cap."""
    if not readings:
        return True
    rises = [b - a for a, b in zip(readings, readings[1:]) if b > a]
    return readings[-1] + max(rises, default=0) >= cap


def _now() -> float:
    return time.time()


def _state_reset(home: Path, now: float) -> float | None:
    """The seven-day reset, still ahead, in the newest usage snapshot that holds one."""
    try:
        files = sorted(usage.state_dir(home).glob("*.json"), key=lambda p: p.stat().st_mtime,
                       reverse=True)
    except OSError:
        return None
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            week = data["rate_limits"]["seven_day"]["resets_at"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if isinstance(week, (int, float)) and not isinstance(week, bool) and week > now:
            return float(week)
    return None


def today_line(home: Path, reading: float | None, reset: float | None,
               now: float) -> tuple[float | None, str | None]:
    """(today's line, why not): the weekly figure when today began plus today's allowance, with
    the numbers the status row uses. The line is None, with the reason, on an off day or with no
    reading or no reset time."""
    if reading is None:
        return None, "no usage reading"
    if reset is None:
        reset = _state_reset(home, now)
    if reset is None:
        return None, "no reset time"
    cap = usage_week.load_capture(home)
    weights, day_end, _cut = cap.week_settings(cap.PROFILE)
    date = time.strftime("%Y-%m-%d", time.localtime(now))
    start = cap.day_start(cap.read_days(), cap.PROFILE, reset, date)
    allow, _ends, zero = cap.allowance(reading, reset, now, weights, day_end, start)
    if zero:
        return None, "off day"
    if reset - cap.WEEK >= cap.midnight(datetime.fromtimestamp(now).date()):
        start = 0.0                       # the window opened today
    elif start is None:
        start = reading
    return start + allow, None


def _status(ctx, project: str | None) -> tuple[str, dict] | None:
    argv = [sys.executable, str(ctx.root / ".context-gate" / "bin" / "govern"), "build",
            "status", "--json"]
    if project:
        argv += ["--project", project]
    try:
        res = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", cwd=str(ctx.root))
        data = json.loads(res.stdout)
    except (OSError, ValueError):
        return None
    if res.returncode != 0 or not isinstance(data, dict):
        return None
    return res.stdout, data


def skill_command(plan: str, commands: list[str] | None) -> str | None:
    """The session's first argument: the build skill the probe saw loaded, the engine's own
    plugin first. With no list (the probe's stream had none), the engine's own plugin. None when
    the list holds neither skill."""
    mine = "context-gate-beta" if versions.is_beta(govern.__version__) else "context-gate"
    if commands is None:
        return f"/{mine}:build {plan}"
    commands = [c.removeprefix("/") for c in commands]
    other = SKILLS[0] if mine == SKILLS[1] else SKILLS[1]
    for name in (mine, other):
        if f"{name}:build" in commands:
            return f"/{name}:build {plan}"
    return None


def _roles_stale(ctx) -> str | None:
    """None when `agents render --check` passes; else its whole output (the error's text when
    the check could not start)."""
    argv = [sys.executable, str(ctx.root / ".context-gate" / "bin" / "govern"), "agents",
            "render", "--check"]
    try:
        res = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", cwd=str(ctx.root))
    except OSError as exc:
        return str(exc)
    if res.returncode == 0:
        return None
    return res.stdout + res.stderr


def _exit_line(result: dict | None) -> str | None:
    text = result.get("result") if isinstance(result, dict) else None
    if not isinstance(text, str):
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if lines and lines[-1].startswith(LOOP):
        return lines[-1][len(LOOP):].strip()
    return None


def _stop_group(proc: subprocess.Popen) -> None:
    """End the session and everything it started: TERM, a wait for the leader, then KILL for
    the whole group whether or not the leader is still there."""
    try:
        if os.name == "nt":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
    except (OSError, ValueError):
        pass
    try:
        proc.wait(timeout=KILL_AFTER)
    except subprocess.TimeoutExpired:
        pass
    try:
        if os.name == "nt":
            proc.kill()
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        proc.wait(timeout=KILL_AFTER)
    except subprocess.TimeoutExpired:
        pass


def _doing(line: str) -> str | None:
    """What an assistant event's last tool call is doing, or None when the line holds none."""
    if '"tool_use"' not in line:
        return None
    try:
        event = json.loads(line)
        blocks = event["message"]["content"]
    except (ValueError, KeyError, TypeError):
        return None
    if event.get("type") != "assistant" or not isinstance(blocks, list):
        return None
    uses = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"]
    if not uses:
        return None
    block = uses[-1]
    given = block.get("input")
    given = given if isinstance(given, dict) else {}
    for text in (given.get("description"), given.get("skill"), block.get("name")):
        if isinstance(text, str) and text:
            return text[:DOING_CHARS]
    return None


def _session(argv, env, cwd: Path, out: Path, err: Path, deadline: float | None,
             on_doing=None):
    """Run one session. Returns (stdout lines, killed). `on_doing(text)` is called with what the
    last tool call does and the time it was seen, at most once per DOING_EVERY seconds; a step
    held back by that limit is written by a timer thread, never after this returns."""
    lines: list[str] = []
    flags = {}
    if os.name == "nt":
        flags["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        flags["start_new_session"] = True
    with out.open("w", encoding="utf-8", newline="\n") as sink, \
            err.open("wb") as errsink:
        proc = subprocess.Popen(argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=errsink, text=True,
                                encoding="utf-8", errors="replace", **flags)

        guard = threading.Lock()
        wake = threading.Condition(guard)
        closed = threading.Event()
        held: dict = {"text": None, "seen": 0.0, "last": float("-inf")}

        def emit(now: float) -> None:
            # caller holds `guard`: write the held step, unless the session is already closed
            if held["text"] is not None and not closed.is_set():
                on_doing(held["text"], held["seen"])
                held["text"], held["last"] = None, now

        def pump() -> None:
            for line in proc.stdout:
                lines.append(line.rstrip("\r\n"))
                sink.write(line if line.endswith("\n") else line + "\n")
                sink.flush()
                if on_doing is None:
                    continue
                text = _doing(line)
                with guard:
                    if text is not None:
                        held["text"], held["seen"] = text, time.time()
                    now = time.monotonic()
                    if now - held["last"] >= DOING_EVERY:
                        emit(now)
                    if held["text"] is not None:
                        wake.notify()

        def flush() -> None:
            # waits for the moment the throttle window closes, not on a clock of its own
            with wake:
                while not closed.is_set():
                    if held["text"] is None:
                        wake.wait()
                        continue
                    now = time.monotonic()
                    left = held["last"] + DOING_EVERY - now
                    if left <= 0:
                        emit(now)
                    else:
                        wake.wait(left)

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        flusher = None
        if on_doing is not None:
            flusher = threading.Thread(target=flush, name="drive-doing", daemon=True)
            flusher.start()
        killed = False
        try:
            proc.wait(timeout=None if deadline is None else max(0.0, deadline - time.time()))
        except subprocess.TimeoutExpired:
            killed = True
            _stop_group(proc)
        except BaseException:           # interrupted: the session does not outlive the driver
            _stop_group(proc)
            raise
        reader.join(timeout=5)
        with wake:
            closed.set()
            wake.notify_all()
        if flusher is not None:
            flusher.join()
        if not reader.is_alive():
            proc.stdout.close()
    return lines, killed


def _utc(when: float) -> str:
    return datetime.fromtimestamp(when, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pct(value) -> str:
    return "n/a" if value is None else f"{value}"


def _confirmed_done(data: dict) -> bool:
    """`build status --json` says the plan is over: no phase in flight, nothing pending."""
    units = data.get("units")
    return (data.get("phase") is None and not data.get("unadded")
            and all(isinstance(u, dict) and u.get("status") == "done"
                    for u in (units if isinstance(units, list) else [])))


class _Status:
    """`status.json` in the run folder: one small file that says what the driver is doing, kept
    current by atomic replace. Nothing is written before `begin`."""

    def __init__(self, ctx, plan, cap, hours) -> None:
        self.folder: Path | None = None
        self.doc = {
            "pid": os.getpid(), "project": ctx.root.name, "cwd": str(ctx.root), "plan": plan,
            "cap": cap, "cap_source": "today" if cap is None else "--cap", "hours": hours, "started": _utc(time.time()), "state": "starting",
            "session": 0, "session_started": None, "seven_day": None, "last": None,
            "next": None, "units": None, "stop_reason": None, "updated": None,
            "skill": None, "doing": None, "doing_since": None,
            "owner_session": os.environ.get("CLAUDE_CODE_SESSION_ID") or None}
        self.lock = threading.Lock()

    def begin(self, folder: Path, data: dict) -> None:
        self.folder = folder
        self.write(**self.progress(data))

    @staticmethod
    def progress(data: dict) -> dict:
        nxt, done, total = data.get("next"), data.get("done"), data.get("total")
        phase = data.get("phase")
        ok = isinstance(phase, str) and all(
            isinstance(v, int) and not isinstance(v, bool) for v in (done, total))
        return {"next": nxt if isinstance(nxt, str) else None,
                "units": f"{phase} \u00b7 {done}/{total} units" if ok else None}

    def write(self, **fields) -> None:
        if self.folder is None:
            return
        with self.lock:
            self.doc.update(fields)
            self.doc["updated"] = _utc(time.time())
            path = self.folder / "status.json"
            tmp = path.with_name("status.json.tmp")
            try:
                tmp.write_text(json.dumps(self.doc, indent=2) + "\n", encoding="utf-8",
                               newline="\n")
                os.replace(tmp, path)
            except OSError:
                pass        # the file is a view of the run; losing a write must not stop it

    def doing(self, text: str | None, since: float | None = None) -> None:
        when = time.time() if since is None else since
        self.write(doing=text, doing_since=None if text is None else _utc(when))

    def stop(self, reason: str) -> None:
        self.write(state="stopped", stop_reason=reason)


def run(ctx, plan, cap=None, hours=None, max_sessions=50, compact_pct="50",
        permission_mode="auto", claude="claude", project=None) -> int:
    """Drive the plan. SIGTERM and SIGINT end the running session's group, then return 130."""
    old: dict[int, object] = {}

    def handler(signum, frame):
        raise _Interrupted()

    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            old[sig] = signal.signal(sig, handler)
    except ValueError:      # not the main thread: no handlers, the caller owns the signals
        pass
    status = _Status(ctx, plan, cap, hours)
    try:
        return _run(ctx, plan, cap, hours, max_sessions, compact_pct, permission_mode, claude,
                    project, status)
    except (_Interrupted, KeyboardInterrupt):
        status.stop("interrupted")
        _say(["interrupted"])
        return 130
    finally:
        for sig, previous in old.items():
            signal.signal(sig, previous)


def _run(ctx, plan, cap, hours, max_sessions, compact_pct, permission_mode, claude,
         project, status) -> int:
    home = ctx.home
    name = ctx.root.name
    auto = str(compact_pct) == "auto"
    if not auto:
        try:
            pct = int(float(compact_pct))
        except (ValueError, OverflowError):
            pct = -1
        if not PCT_RANGE[0] <= pct <= PCT_RANGE[1]:
            print(f"error: --compact-pct must be 'auto' or a number from {PCT_RANGE[0]} to "
                  f"{PCT_RANGE[1]}, not {compact_pct}", file=sys.stderr)
            return 1
    else:
        pct = drive_log.suggest(drive_log.rows(home, name)) or DEFAULT_PCT
    found = claude if Path(claude).is_file() else shutil.which(claude)
    if not found:
        print(f"error: claude not found: {claude}", file=sys.stderr)
        return 1
    claude = found
    first = _status(ctx, project)
    if first is None:
        print("error: `govern build status --json` failed", file=sys.stderr)
        return 1
    stale = _roles_stale(ctx)
    if stale is not None:
        changes = [int(m.group(1)) for m in re.finditer(r"(\d+) file\(s\) would change", stale)]
        if any(changes):
            print("error: agent roles are stale: run python3 .context-gate/bin/govern agents "
                  "render, commit, and start the driver again", file=sys.stderr)
        else:
            print("error: agent roles: python3 .context-gate/bin/govern agents render --check "
                  "failed", file=sys.stderr)
        lines = [ln.strip() for ln in stale.splitlines() if ln.strip()]
        if lines:
            print(lines[-1], file=sys.stderr)
        return 1
    prompt = PROMPT.read_text(encoding="utf-8")
    env = {**os.environ, "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": str(pct),
           "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "0"}
    deadline = None if hours is None else time.time() + float(hours) * 3600
    readings: list[float] = []
    reset: list[float | None] = [None]
    probed: list[list[str] | None] = []         # the commands of the first probe only

    def probe() -> str | None:
        value, found, why, commands = probe_full(claude, ctx.root)
        if not probed:
            probed.append(commands)
        if value is not None:
            readings.append(value)
            reset[0] = found if found is not None else reset[0]
        return why

    why = probe()
    if why:
        print(f"error: usage probe failed: {why}", file=sys.stderr)
        return 1
    chosen = skill_command(plan, probed[0])
    if chosen is None:
        print("error: no context-gate build skill is loaded in this project (looked for "
              "context-gate:build, context-gate-beta:build)", file=sys.stderr)
        return 1
    text, data = first
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = drive_log.log_path(home).parent / "runs" / f"{name}-{stamp}"
    folder.mkdir(parents=True, exist_ok=True)
    print(f"run folder: {folder}")
    sys.stdout.flush()
    status.begin(folder, data)
    status.write(skill=chosen.split(":", 1)[0][1:])
    last_next = str(data.get("next", ""))
    idle = 0
    n = 0
    while True:
        if cap is None:
            in_force, why = today_line(home, readings[-1] if readings else None, reset[0], _now())
            source = "today"
        else:
            in_force, why, source = cap, None, "--cap"
        status.write(cap=None if in_force is None else round(in_force, 1), cap_source=source)
        if why:
            status.stop(f"cap unknown: {why}")
            _say([f"cap unknown: {why}"])
            return 0
        tag = f"cap {in_force:.0f}% ({source})"
        if over_cap(readings, in_force):
            if not readings:
                status.stop("cap unknown: no usage reading")
                _say(["cap unknown: no usage reading"])
                return 0
            where = "before the first session" if n == 0 else f"last step {last_next} finished"
            reason = f"cap reached: 7d at {readings[-1]:.0f}%, {tag}, {where}"
            status.stop(reason)
            _say([reason])
            return 0
        if n >= max_sessions:
            reason = f"session limit: {n} sessions run"
            status.stop(reason)
            _say([reason])
            return 0
        if deadline is not None and time.time() >= deadline:
            status.stop("time limit")
            _say(["time limit"])
            return 0
        n += 1
        before_next = str(data.get("next", ""))
        started = time.time()
        status.write(state="in_session", session=n, session_started=_utc(started))
        argv = _argv(claude, "-p", chosen, "--name", f"drive-{name}-s{n}", "--output-format",
                     "stream-json", "--verbose", "--permission-mode", permission_mode,
                     "--append-system-prompt", prompt)
        stop_timer = threading.Event()

        def refresh() -> None:
            while not stop_timer.wait(STATUS_EVERY):
                got = _status(ctx, project)
                if got is not None:
                    status.write(**status.progress(got[1]))

        timer = threading.Thread(target=refresh, name="drive-status", daemon=True)
        timer.start()
        try:
            lines, killed = _session(argv, env, ctx.root, folder / f"{n}.jsonl",
                                     folder / f"{n}.err", deadline, status.doing)
        finally:
            stop_timer.set()
            timer.join()
            status.doing(None)
        ended = time.time()
        events = _events(lines)
        results = [e for e in events if e.get("type") == "result"]
        result = results[-1] if results else None
        exit_line = _exit_line(result)
        if killed:
            kind = "killed"
        elif result is None or result.get("is_error") is True:
            kind = "error"
        elif exit_line is None:
            kind = "unfinished"
        elif exit_line == "plan-done":
            kind = "plan-done"
        elif exit_line == "continue":
            kind = "continue"
        elif exit_line == "stop" or exit_line.startswith("stop "):
            kind = "stop"
        else:
            kind = "unfinished"
        after = _status(ctx, project)
        if after is None:
            after = (text, data)
        if kind == "plan-done" and not _confirmed_done(after[1]):
            _say([f"plan-done not confirmed: {after[1].get('next', '')}"])
            kind = "continue"
        session_id = next((e["session_id"] for e in [result or {}, *events]
                           if isinstance(e.get("session_id"), str)), None)
        transcript = (coordinator_runs.transcript_folder(home, ctx.root) / f"{session_id}.jsonl"
                      if session_id else None)
        row = drive_log.session_row(transcript, result, {
            "project": name, "plan": str(plan), "phase": data.get("phase"),
            "next": before_next[:200], "started": _utc(started), "ended": _utc(ended),
            "exit": kind})
        drive_log.append(home, row)
        reading = stream_reading(lines)
        if reading is None and not killed:
            why = probe()
            if why:
                status.stop(f"error: usage probe failed: {why}")
                print(f"error: usage probe failed: {why}", file=sys.stderr)
                return 1
        elif reading is not None:
            readings.append(reading)
            found = stream_reset(lines)
            reset[0] = found if found is not None else reset[0]
        shown = readings[-1] if readings else None
        _say([f"session {n}: {kind} after {round(ended - started)}s, peak "
              f"{_pct(row['peak_pct'])}%, {_pct(row['compacts'])} compacts, 7d "
              f"{'n/a' if shown is None else f'{shown:.0f}'}%, {tag}"])
        last_next = before_next
        progressed = after[0] != text
        text, data = after
        status.write(state="between", seven_day=shown, **status.progress(data),
                     last={"kind": kind, "seconds": round(ended - started),
                           "peak_pct": row["peak_pct"]})
        if killed:
            status.stop("time limit")
            _say(["time limit"])
            return 0
        if kind == "plan-done":
            status.stop("plan done")
            _say(["plan done"])
            return 0
        if kind == "stop":
            reason = f"stop: {exit_line[len('stop'):].strip()}"
            status.stop(reason)
            _say([reason])
            return 0
        idle = 0 if progressed else idle + 1
        if idle >= 2:
            reason = f"no progress: {data.get('next', '')}"
            status.stop(reason)
            _say([reason])
            return 1
