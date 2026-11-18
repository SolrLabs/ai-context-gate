"""`govern build drive`: one fresh headless coordinator session per build step, until the plan is
done, a person is needed, the weekly usage cap is near or a limit is reached.

Each session resumes from `govern build status`, the ledger and git; nothing is carried between
sessions but what those hold. The measurements of every session go to `drive_log`."""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import govern
from govern import coordinator_runs, drive_log, versions
from govern.build import _say as _say_raw

PROMPT = Path(__file__).resolve().parent / "templates" / "drive-prompt.md"
LOOP = "LOOP: "
KILL_AFTER = 10.0
PROBE_TIMEOUT = 120
PCT_RANGE = (20, 90)
DEFAULT_PCT = 50


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


def _argv(claude: str, *args: str) -> list[str]:
    return [sys.executable, claude, *args] if claude.endswith(".py") else [claude, *args]


def probe_reading(claude: str, cwd: Path | None = None) -> tuple[float | None, str | None]:
    """The weekly usage as one cheap throwaway session reports it: (reading, why). `why` says
    the probe did not run or failed; a probe that ran cleanly with no event gives (None, None)."""
    argv = _argv(claude, "-p", "Reply with OK.", "--model", "haiku", "--max-turns", "1",
                 "--output-format", "stream-json", "--verbose")
    try:
        res = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=PROBE_TIMEOUT,
                             cwd=str(cwd) if cwd else None)
    except subprocess.TimeoutExpired:
        return None, f"timeout after {PROBE_TIMEOUT}s"
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"did not start ({exc})"
    reading = stream_reading(res.stdout.splitlines())
    if reading is not None:
        return reading, None
    if res.returncode != 0:
        return None, f"exit status {res.returncode}"
    return None, None


def over_cap(readings: list[float], cap: float) -> bool:
    """True with no reading; else when the last reading plus the largest rise one step has
    caused reaches the cap."""
    if not readings:
        return True
    rises = [b - a for a, b in zip(readings, readings[1:]) if b > a]
    return readings[-1] + max(rises, default=0) >= cap


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


def skill_command(plan: str) -> str:
    """The session's first argument: the build skill of the plugin this engine ships with."""
    name = "context-gate-beta" if versions.is_beta(govern.__version__) else "context-gate"
    return f"/{name}:build {plan}"


def _roles_stale(ctx) -> str | None:
    """None when `agents render --check` passes; else the reason, its last output line if any."""
    argv = [sys.executable, str(ctx.root / ".context-gate" / "bin" / "govern"), "agents",
            "render", "--check"]
    try:
        res = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", cwd=str(ctx.root))
    except OSError:
        return ""
    if res.returncode == 0:
        return None
    lines = [ln.strip() for ln in (res.stdout + res.stderr).splitlines() if ln.strip()]
    return lines[-1] if lines else ""


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


def _session(argv, env, cwd: Path, out: Path, err: Path, deadline: float | None):
    """Run one session. Returns (stdout lines, killed)."""
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

        def pump() -> None:
            for line in proc.stdout:
                lines.append(line.rstrip("\r\n"))
                sink.write(line if line.endswith("\n") else line + "\n")
                sink.flush()

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
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


def run(ctx, plan, cap=90.0, hours=None, max_sessions=50, compact_pct="50",
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
    try:
        return _run(ctx, plan, cap, hours, max_sessions, compact_pct, permission_mode, claude,
                    project)
    except (_Interrupted, KeyboardInterrupt):
        _say(["interrupted"])
        return 130
    finally:
        for sig, previous in old.items():
            signal.signal(sig, previous)


def _run(ctx, plan, cap, hours, max_sessions, compact_pct, permission_mode, claude,
         project) -> int:
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
        print("error: agent roles are stale: run python3 .context-gate/bin/govern agents "
              "render, commit, and start the driver again", file=sys.stderr)
        if stale:
            print(stale, file=sys.stderr)
        return 1
    folder = None
    prompt = PROMPT.read_text(encoding="utf-8")
    env = {**os.environ, "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": str(pct),
           "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "0"}
    deadline = None if hours is None else time.time() + float(hours) * 3600
    readings: list[float] = []

    def probe() -> str | None:
        value, why = probe_reading(claude, ctx.root)
        if value is not None:
            readings.append(value)
        return why

    why = probe()
    if why:
        print(f"error: usage probe failed: {why}", file=sys.stderr)
        return 1
    text, data = first
    last_next = str(data.get("next", ""))
    idle = 0
    n = 0
    while True:
        if over_cap(readings, cap):
            if not readings:
                _say(["cap unknown: no usage reading"])
                return 0
            where = "before the first session" if n == 0 else f"last step {last_next} finished"
            _say([f"cap reached: 7d at {readings[-1]:.0f}%, {where}"])
            return 0
        if n >= max_sessions:
            _say([f"session limit: {n} sessions run"])
            return 0
        if deadline is not None and time.time() >= deadline:
            _say(["time limit"])
            return 0
        n += 1
        if folder is None:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            folder = drive_log.log_path(home).parent / "runs" / f"{name}-{stamp}"
            folder.mkdir(parents=True, exist_ok=True)
        before_next = str(data.get("next", ""))
        started = time.time()
        argv = _argv(claude, "-p", skill_command(plan), "--output-format",
                     "stream-json", "--verbose", "--permission-mode", permission_mode,
                     "--append-system-prompt", prompt)
        lines, killed = _session(argv, env, ctx.root, folder / f"{n}.jsonl",
                                 folder / f"{n}.err", deadline)
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
                print(f"error: usage probe failed: {why}", file=sys.stderr)
                return 1
        elif reading is not None:
            readings.append(reading)
        shown = readings[-1] if readings else None
        _say([f"session {n}: {kind} after {round(ended - started)}s, peak "
              f"{_pct(row['peak_pct'])}%, {_pct(row['compacts'])} compacts, 7d "
              f"{'n/a' if shown is None else f'{shown:.0f}'}%"])
        last_next = before_next
        progressed = after[0] != text
        text, data = after
        if killed:
            _say(["time limit"])
            return 0
        if kind == "plan-done":
            _say(["plan done"])
            return 0
        if kind == "stop":
            _say([f"stop: {exit_line[len('stop'):].strip()}"])
            return 0
        idle = 0 if progressed else idle + 1
        if idle >= 2:
            _say([f"no progress: {data.get('next', '')}"])
            return 1
