"""Usage alerts at run time: the orchestrator's usage steps, the owner's break points, and the
text the plugin's hook injects. Standard library and govern.layout only: the hook imports this on
every tool call. See docs/configuration.md ("Usage alerts").
"""
from __future__ import annotations

import json
import math
import os
import string
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

from govern.layout import TOOL

SIGNALS = ("context", "five_hour", "seven_day")
LABELS = {"context": "context", "five_hour": "5h", "seven_day": "7d"}
PREFIX = "⚠ usage alert (owner's prompt, {file}):"
PLACEHOLDERS = {"pct", "resets"}
STALE_SECONDS = 600
SETTLE_SECONDS = 2          # an alerts file younger than this may still be mid-write
CONTEXT_STEP = 5                    # the default; usage-alerts.toml's `context_step` may set 1-10
RULE = " (lines come when a value rises a step or a window resets)"
ACTIVE_GAP_SECONDS = 120           # calls further apart than this are an idle wait, not activity
STALE = (f"usage: no fresh usage data for {STALE_SECONDS // 60}+ minutes of activity (last at {{clock}}) "
         "— if this persists, python3 .context-gate/bin/govern usage install re-wraps the capture")
STALE_NO_CLOCK = STALE.replace(" (last at {clock})", "")
SAME_WINDOW_SECONDS = 120           # two resets_at this close are one window (seeded vs own reading)
FUTURE_SLACK = 60                   # a captured_at further ahead than this is not believable


class AlertsError(Exception):
    pass


@dataclass(frozen=True)
class Alert:
    signal: str
    at: int
    say: str


# ---------------------------------------------------------------------------- alerts file

def parse_alerts(text: str) -> list[Alert]:
    return parse_file(text)[0]


def parse_file(text: str) -> tuple[list[Alert], int]:
    """The alerts and the context step a usage-alerts.toml sets (the step is CONTEXT_STEP when
    it sets none)."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise AlertsError(f"not valid TOML: {exc}") from None
    extra = set(data) - {"alert", "context_step"}
    if extra:
        raise AlertsError(f"unknown key(s): {', '.join(sorted(extra))}")
    ctx_step = data.get("context_step", CONTEXT_STEP)
    if not _valid_step(ctx_step):
        raise AlertsError("context_step must be a whole number 1-10")
    alerts = data.get("alert", [])
    if not isinstance(alerts, list):
        raise AlertsError("alert must be a list of [[alert]] tables")
    out: list[Alert] = []
    seen: set[tuple[str, int]] = set()
    for i, a in enumerate(alerts, 1):
        where = f"alert {i}"
        if not isinstance(a, dict):
            raise AlertsError(f"{where}: must be a table")
        unknown = set(a) - {"signal", "at", "say"}
        if unknown:
            raise AlertsError(f"{where}: unknown key(s): {', '.join(sorted(unknown))}")
        sig, at, say = a.get("signal"), a.get("at"), a.get("say")
        if sig not in SIGNALS:
            raise AlertsError(f"{where}: signal must be one of {', '.join(SIGNALS)}")
        if not isinstance(at, int) or isinstance(at, bool) or not 1 <= at <= 100:
            raise AlertsError(f"{where}: at must be a whole number 1-100")
        if not isinstance(say, str) or not say.strip():
            raise AlertsError(f"{where}: say must be non-empty text")
        try:
            names = {f for _, f, _, _ in string.Formatter().parse(say) if f is not None}
        except ValueError as exc:
            raise AlertsError(f"{where}: say: {exc}") from None
        if names - PLACEHOLDERS:
            raise AlertsError(f"{where}: say may use only {{pct}} and {{resets}}")
        # A format spec or conversion (`{pct!x}`) only raises here, not on every call at run
        # time. Two trials: an empty `resets` (the usual "no weekly signal" case) alone would
        # miss a nested field in `pct`'s own spec (`{pct:{resets}}`) — `resets=""` leaves that
        # spec empty, which is harmless for an int; a non-empty one (as the data line's own
        # `Www HH:MM` is) is not.
        for resets in ("", "Mon 00:00"):
            try:
                say.format(pct=0, resets=resets)
            except Exception as exc:
                raise AlertsError(f"{where}: say: {exc}") from None
        if (sig, at) in seen:
            raise AlertsError(f"{where}: a second alert for {sig} at {at}")
        seen.add((sig, at))
        out.append(Alert(sig, at, say.strip()))
    return out, ctx_step


def load_alerts(path: Path) -> tuple[list[dict], int]:
    """The alerts, as the resolved file holds them, and the context step of the alerts file at
    `path`: read the same way at session start and when the file changes mid-session. Raises
    AlertsError for a file that is invalid (not UTF-8 included), OSError for one not readable."""
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise AlertsError(str(exc)) from None
    alerts, ctx_step = parse_file(text)
    return [{"signal": a.signal, "at": a.at, "say": a.say} for a in alerts], ctx_step


def _valid_step(n) -> bool:
    return isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= 10


# ---------------------------------------------------------------------------- steps and text

def step(v: float, size: int = 5) -> int:
    """The step a value sits on: every `size` points below 90, every point from 90 up."""
    f = math.floor(min(max(v, 0), 100))
    return f if f >= 90 else f // size * size


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def snap_ok(snap: dict) -> bool:
    """Whether a snapshot has the shapes `values` reads (a wrong-typed one is skipped like a
    corrupt one)."""
    cw, rl, cap = snap.get("context_window"), snap.get("rate_limits"), snap.get("captured_at")
    return (cw is None or isinstance(cw, dict)) and (rl is None or isinstance(rl, dict)) \
        and (cap is None or isinstance(cap, (int, float)) and not isinstance(cap, bool))


def state_ok(state: dict) -> bool:
    """Whether a state file has the shapes `evaluate` reads."""
    steps, fired, last = state.get("steps"), state.get("fired"), state.get("resets")
    return ((steps is None or isinstance(steps, dict) and all(isinstance(k, str) and _num(v)
                                                              for k, v in steps.items()))
            and (fired is None or isinstance(fired, list) and all(
                isinstance(f, list) and len(f) == 2 and isinstance(f[0], str) and _num(f[1])
                for f in fired))
            and (last is None or isinstance(last, dict) and all(isinstance(k, str) and _num(v)
                                                                for k, v in last.items()))
            and all(state.get(k) is None or _num(state[k]) for k in ("ctx0", "t0"))
            and (state.get("logged") is None or isinstance(state["logged"], list)
                 and all(isinstance(k, str) for k in state["logged"])))


def values(snap: dict, now: float) -> dict[str, float]:
    out: dict[str, float] = {}
    ctx = (snap.get("context_window") or {}).get("used_percentage")
    if _num(ctx):
        out["context"] = ctx
    for sig, w in (snap.get("rate_limits") or {}).items():
        if sig in SIGNALS and isinstance(w, dict) and _num(w.get("used_percentage")) \
                and _num(w.get("resets_at")) and w["resets_at"] > now:
            out[sig] = w["used_percentage"]
    return out


def resets(snap: dict, now: float) -> dict[str, int]:
    rl = snap.get("rate_limits") or {}
    return {s: int(rl[s]["resets_at"]) for s in ("five_hour", "seven_day")
            if s in values(snap, now)}


def _clock(epoch: int | None, fmt: str = "%a %H:%M") -> str:
    return time.strftime(fmt, time.localtime(epoch)) if epoch else ""


def _pct(v: float) -> int:
    return math.floor(min(max(v, 0), 100))


def _elapsed(seconds: float) -> str:
    m = max(int(seconds // 60), 0)
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    return f"{h}h{m}m" if m else f"{h}h"


def data_line(vals: dict[str, float], rs: dict[str, int],
              trend: tuple[int, float] | None = None) -> str:
    """All three signals, whichever changed. `trend` is the session's own context rise, in
    whole points, and the seconds it took. 5h and 7d are account-wide, shared by every session,
    so they carry their reset time and never a rise of their own."""
    if "context" in vals:
        ctx = f"context {_pct(vals['context'])}%"
        if trend and trend[0] >= 1:
            ctx += f" (+{trend[0]}% in {_elapsed(trend[1])})"
    else:
        ctx = "context pending"
    acct = []
    for sig, fmt in (("five_hour", "%H:%M"), ("seven_day", "%a %H:%M")):
        if sig not in vals:
            acct.append(f"{LABELS[sig]} pending")
            continue
        part = f"{LABELS[sig]} {_pct(vals[sig])}%"
        if rs.get(sig):
            part += f" (resets {_clock(rs[sig], fmt)})"
        acct.append(part)
    return f"usage: {ctx} · account {acct[0]} · {acct[1]}"


def evaluate(snap: dict, alerts: list[Alert], state: dict, now: float,
             alerts_file: str | None, ctx_step: int = CONTEXT_STEP) -> tuple[str | None, dict]:
    vals = values(snap, now)
    rs = resets(snap, now)
    size = {"context": ctx_step}
    steps = dict(state.get("steps") or {})
    fired = {tuple(f) for f in state.get("fired") or []}
    last = dict(state.get("resets") or {})
    for sig, r in rs.items():                       # a new window re-arms its signal
        if sig not in last or abs(last[sig] - r) > SAME_WINDOW_SECONDS:
            fired = {f for f in fired if f[0] != sig}
            steps.pop(sig, None)
            last[sig] = r
    ctx0, t0 = state.get("ctx0"), state.get("t0")
    if "context" in vals:
        if "context" in steps and step(vals["context"], ctx_step) < steps["context"]:
            steps["context"] = step(vals["context"], ctx_step)   # compaction re-arms above it
            fired = {f for f in fired if not (f[0] == "context" and f[1] > _pct(vals["context"]))}
        if not isinstance(ctx0, (int, float)) or not isinstance(t0, (int, float)) \
                or _pct(vals["context"]) < _pct(ctx0):
            ctx0, t0 = vals["context"], now     # the session's first reading, or a compaction
    rose = [s for s in SIGNALS if s in vals and step(vals[s], size.get(s, 5)) > steps.get(s, -1)]
    for s in rose:
        steps[s] = step(vals[s], size.get(s, 5))
    new = sorted((a for a in alerts
                  if a.signal in vals and _pct(vals[a.signal]) >= a.at and (a.signal, a.at) not in fired),
                 key=lambda a: (SIGNALS.index(a.signal), a.at))
    fired |= {(a.signal, a.at) for a in new}
    # Fresh data after a stale spell is told like a step change, so the agent knows it is back.
    recovered = bool(state.get("stale_told"))
    out = {k: v for k, v in state.items() if k != "stale_told"}
    out.update(steps=steps, fired=sorted(fired), resets=last, had_fresh=True)
    if ctx0 is not None:
        out.update(ctx0=ctx0, t0=t0)
    if not rose and not new and not recovered:
        return None, out
    trend = (_pct(vals["context"]) - _pct(ctx0), now - t0) if "context" in vals else None
    line = data_line(vals, rs, trend)
    if not state.get("baseline_sent"):
        line += RULE                            # the emission rule, said once per session
        out["baseline_sent"] = True
    # Each alert first, under its own marker, and the data line last: an alert never reads like
    # the routine line.
    head = PREFIX.format(file=alerts_file or "usage-alerts.toml")
    says = [f"{head}\n" + a.say.format(pct=_pct(vals[a.signal]), resets=_clock(rs.get(a.signal)))
            for a in new]
    return "\n\n".join(says + [line]), out


# ---------------------------------------------------------------------------- files

def state_dir(home: Path) -> Path:
    return home / ".local" / "state" / TOOL / "usage"


def snapshot_path(home: Path, sid: str) -> Path:
    return state_dir(home) / f"{sid}.json"


def resolved_path(home: Path, sid: str) -> Path:
    return state_dir(home) / f"{sid}.resolved.json"


def state_path(home: Path, sid: str) -> Path:
    return state_dir(home) / f"{sid}.state.json"


def log_path(home: Path) -> Path:
    return state_dir(home) / "usage.log"


def read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


def _seed(home: Path, sid: str, snap: dict, now: float) -> dict:
    """`snap` with any 5h or 7d window it lacks taken from the newest fresh snapshot of another
    session that has it: those windows are account-wide, so another session's reading is this
    one's too (a resumed session has none until its first API response)."""
    missing = [s for s in ("five_hour", "seven_day") if s not in values(snap, now)]
    if not missing:
        return snap
    found: dict[str, tuple[float, dict]] = {}
    try:
        names = os.listdir(state_dir(home))
    except OSError:
        return snap
    for name in names:
        if not name.endswith(".json") or name.count(".") != 1 or name == f"{sid}.json":
            continue                            # snapshots only: not .state/.resolved, not ours
        path = state_dir(home) / name
        try:
            if now - path.stat().st_mtime > STALE_SECONDS:
                continue                        # a stat, not a read, for an old file
        except OSError:
            continue
        other = read_json(path)
        captured = other.get("captured_at") if other and snap_ok(other) else None
        if not _num(captured) or now - captured > STALE_SECONDS or captured - now > FUTURE_SLACK:
            continue
        have = values(other, now)
        for sig in missing:
            if sig in have and captured > found.get(sig, (-1.0, {}))[0]:
                found[sig] = (captured, other["rate_limits"][sig])
    if not found:
        return snap
    rl = dict(snap.get("rate_limits") or {})
    rl.update({sig: w for sig, (_, w) in found.items()})
    return {**snap, "rate_limits": rl}


def _log_once(home: Path, sid: str, state: dict, key: str, line: str) -> dict:
    logged = set(state.get("logged") or [])
    if key in logged:
        return state
    with open(log_path(home), "a", encoding="utf-8") as fh:
        fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {sid} {line}\n")
    return {**state, "logged": sorted(logged | {key})}


def _reload(home: Path, sid: str, resolved: dict, state: dict, now: float) -> tuple[dict, dict, str | None]:
    """The alerts file edited since it was last read: its alerts and context step replace the
    resolved ones; `fired` is never pruned (its keys are `(signal, at)`, so a changed `at` is a new
    alert and an entry for a removed one is inert). Only a settled file is read: one whose mtime is
    under SETTLE_SECONDS old may still be mid-write, so it waits for a later call. A missing or
    invalid file keeps what is loaded. Only a stat when the file is unchanged. Returns the resolved
    data, the state, and a line for the agent when the edit is invalid."""
    path = resolved.get("alerts_path")
    if not isinstance(path, str) or not path:
        return resolved, state, None            # no alerts file, or resolved by an earlier beta
    def gone():
        return resolved, _log_once(home, sid, state, "alerts-gone",
                                   "alerts file gone; keeping the alerts loaded at session start"), None
    try:
        st = os.stat(path)
    except OSError:
        return gone()
    mtime, size = st.st_mtime_ns, st.st_size
    if mtime == resolved.get("alerts_mtime") and size == resolved.get("alerts_size"):
        return resolved, state, None
    if now - mtime / 1e9 < SETTLE_SECONDS:
        return resolved, state, None            # possibly mid-write: try again on a later call
    shown = resolved.get("alerts_file") or "usage-alerts.toml"
    notice = None
    try:
        alerts, ctx_step = load_alerts(Path(path))
    except FileNotFoundError:
        return gone()
    except (AlertsError, OSError) as exc:
        # Told once per version of the file: the new mtime is recorded, so the next call does
        # not read it again until it changes.
        notice = f"usage: {shown} has an error ({exc}); keeping the previous alerts"
        state = _log_once(home, sid, state, f"alerts-invalid-{mtime}-{size}",
                          f"alerts file {shown} has an error ({exc}); keeping the previous alerts")
        resolved = {**resolved, "alerts_mtime": mtime, "alerts_size": size}
    else:
        resolved = {**resolved, "alerts": alerts, "context_step": ctx_step, "error": None,
                    "alerts_mtime": mtime, "alerts_size": size}
    write_json(resolved_path(home, sid), resolved)
    return resolved, state, notice


def on_call(home: Path, sid: str, now: float) -> str | None:
    """The hook's work for one orchestrator tool call or prompt: the text to inject, or None."""
    resolved = read_json(resolved_path(home, sid))
    if not resolved or not resolved.get("enabled"):
        return None
    state_dir(home).mkdir(parents=True, exist_ok=True)
    state = read_json(state_path(home, sid)) or {}
    if not state_ok(state):
        state = {}                              # parses but wrong-typed: rebuilt, not silenced
    if resolved.get("error"):
        state = _log_once(home, sid, state, "alerts-error",
                          f"alerts file {resolved.get('alerts_file')}: {resolved['error']}")
    resolved, state, notice = _reload(home, sid, resolved, state, now)
    text = _respond(home, sid, now, resolved, state)
    return "\n\n".join(t for t in (notice, text) if t) or None


def _respond(home: Path, sid: str, now: float, resolved: dict, state: dict) -> str | None:
    """`on_call` from the snapshot on: the data line, alerts and staleness, and the state saved."""
    snap = read_json(snapshot_path(home, sid))
    if snap is not None and not snap_ok(snap):
        snap = None
    # The staleness keys are read defensively rather than checked by state_ok: a missing or wrong
    # one (older state has none) means 0 or None, and the rest of the state stands.
    last_call = state.get("last_call")
    gap = now - last_call if isinstance(last_call, (int, float)) and not isinstance(last_call, bool) \
        else 0.0
    if not (math.isfinite(gap) and gap >= 0):
        gap = 0.0                               # a first call, or a clock that went back
    state = {**state, "last_call": now}
    if snap is None:
        write_json(state_path(home, sid), _log_once(
            home, sid, state, "no-snapshot",
            "no snapshot: the statusline capture is not running (headless session, or not installed)"))
        return None
    captured = snap.get("captured_at")
    usable = _num(captured) and captured - now <= FUTURE_SLACK
    seen = state.get("seen_captured")
    # Staleness is active time since the last new capture, not the snapshot's age: the statusline
    # redraws only while the session is active, so after an idle wait (a gap longer than
    # ACTIVE_GAP_SECONDS) an old snapshot is old by nature, not a stopped capture. Only a capture
    # this session has not seen resets it; being within STALE_SECONDS of now does not.
    new_capture = usable and (not _num(seen) or captured > seen)
    active = state.get("active_stale") if _num(state.get("active_stale")) and state["active_stale"] >= 0 else 0.0
    if new_capture:
        active, state = 0, {**state, "seen_captured": captured}
    elif gap <= ACTIVE_GAP_SECONDS:
        active += gap
    state = {**state, "active_stale": active}
    if not usable or now - captured > STALE_SECONDS:
        text = None
        if new_capture:
            # A capture too old to reach evaluate (which clears stale_told) still ends the spell:
            # the next one may be told, and logged, again.
            state = {k: v for k, v in state.items() if k != "stale_told"}
            state["logged"] = [k for k in state.get("logged") or [] if k != "stale"]
        # Told once, and only to a session that has had fresh data (`had_fresh`, set by
        # evaluate): one that never had any, a headless one say, stays silent.
        if active > STALE_SECONDS and state.get("had_fresh") and not state.get("stale_told"):
            state = {**state, "stale_told": True}
            text = STALE.format(clock=_clock(int(captured), "%H:%M")) \
                if usable and captured else STALE_NO_CLOCK
        # Logged when the agent is told, or once for a session that never had fresh data; an old
        # snapshot after an idle wait is not noise.
        if text or not state.get("had_fresh"):
            state = _log_once(home, sid, state, "stale", "stale snapshot")
        write_json(state_path(home, sid), state)
        return text
    snap = _seed(home, sid, snap, now)
    ctx_step = resolved.get("context_step", CONTEXT_STEP)
    alerts = [Alert(a["signal"], a["at"], a["say"]) for a in resolved.get("alerts") or []]
    text, state = evaluate(snap, alerts, state, now, resolved.get("alerts_file"),
                           ctx_step if _valid_step(ctx_step) else CONTEXT_STEP)
    write_json(state_path(home, sid), state)
    return text


def end_session(home: Path, sid: str) -> None:
    for p in (snapshot_path(home, sid), resolved_path(home, sid), state_path(home, sid)):
        try:
            p.unlink()
        except OSError:
            pass


def sweep(home: Path, now: float, days: int = 7) -> None:
    d = state_dir(home)
    if not d.is_dir():
        return
    for p in d.glob("*.json"):
        try:
            if now - p.stat().st_mtime > days * 86400:
                p.unlink()
        except OSError:
            pass
