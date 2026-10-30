"""Reading a coordinator session: one main-session transcript, plus the project's phase marks,
into numbers per phase, and recording those rows in the project's durable run log.

The transcript's shape is internal to Claude Code and may change in any release, so anything
unexpected gives None, never a guess. A row holds numbers only: no conversation text, no tool
input, no path from the conversation."""
from __future__ import annotations

import bisect
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from govern import agent_runs, layout

TOKEN_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
              "output_tokens")
LARGEST = 5


def transcript_folder(home: Path, root: Path) -> Path:
    """Where Claude Code keeps this project's session transcripts: the root's absolute path with
    every character that is not an ASCII letter or digit turned into `-`."""
    return layout.claude_dir(home) / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(root))


def main_sessions(home: Path, root: Path, since: float) -> list[Path]:
    """The project's main-session transcripts modified at or after `since`, oldest first. A
    subagent's transcript sits in a subfolder and is not one."""
    folder = transcript_folder(home, root)
    found = []
    try:
        for p in folder.glob("*.jsonl"):
            if p.is_file() and p.stat().st_mtime >= since:
                found.append((p.stat().st_mtime, p.name, p))
    except OSError:
        return []
    return [p for _, _, p in sorted(found)]


def _epoch(entry: dict) -> float:
    stamp = entry["timestamp"]
    if not isinstance(stamp, str):
        raise TypeError("timestamp")
    when = datetime.fromisoformat(stamp)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.timestamp()


def _entries(path: Path) -> list[dict]:
    entries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            entry = json.loads(line)
            if not isinstance(entry, dict):
                raise TypeError("entry")
            entries.append(entry)
    return entries


def _usage(msg: dict) -> dict:
    usage = msg["usage"]
    if not isinstance(usage, dict):
        raise TypeError("usage")
    out = {}
    for key in TOKEN_KEYS:
        value = usage.get(key) or 0
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(key)
        out[key] = value
    return out


def _blank() -> dict:
    return {"requests": [], "owner_turns": 0, "handbacks": 0, "compactions": 0,
            "tools": {}, "results": []}


def session_rows(path: Path, marks: list[tuple[float, str]]) -> list[dict] | None:
    """One row per phase the session touched, or None when the transcript is not of the shape
    this reads. A request belongs to the last mark at or before its first line's time, and
    before any mark to phase "" (planning); owner turns, hand-backs, compactions and tool calls
    belong to the phase of their own time."""
    try:
        entries = _entries(Path(path))
        ordered = sorted(marks)
        times = [t for t, _ in ordered]
        labels = [""] + [label for _, label in ordered]

        def phase_of(when: float) -> str:
            return labels[bisect.bisect_right(times, when)]

        phases: dict[str, dict] = {label: _blank() for label in labels}
        requests: dict[str, dict] = {}       # message id -> the request, in first-seen order
        uses: dict[str, dict] = {}           # tool_use id -> name, start, phase
        for entry in entries:
            kind = entry.get("type")
            if kind == "assistant":
                msg = entry["message"]
                if not isinstance(msg, dict):
                    raise TypeError("message")
                if not msg.get("usage") and not msg.get("id"):
                    continue
                when = _epoch(entry)
                mid = msg["id"]
                if not isinstance(mid, str):
                    raise TypeError("id")
                model = msg["model"]
                if not isinstance(model, str):
                    raise TypeError("model")
                use = _usage(msg)
                req = requests.setdefault(mid, {"phase": phase_of(when)})
                req["usage"], req["model"] = use, model
                content = msg.get("content")
                for block in content if isinstance(content, list) else []:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        name, uid = block["name"], block["id"]
                        if not isinstance(name, str) or not isinstance(uid, str):
                            raise TypeError("tool_use")
                        uses[uid] = {"name": name, "start": when, "phase": phase_of(when)}
                        slot = phases[phase_of(when)]["tools"].setdefault(
                            name, {"calls": 0, "seconds": 0.0, "bytes": 0})
                        slot["calls"] += 1
            elif kind == "system":
                if entry.get("subtype") == "compact_boundary":
                    phases[phase_of(_epoch(entry))]["compactions"] += 1
            elif kind == "user":
                when = _epoch(entry)
                if "toolUseResult" in entry:
                    content = entry["message"]["content"]
                    if not isinstance(content, list):
                        raise TypeError("content")
                    for block in content:
                        if not isinstance(block, dict) or block.get("type") != "tool_result":
                            continue
                        use = uses.get(block["tool_use_id"])
                        if use is None:
                            continue
                        size = len(json.dumps(block["content"]))
                        slot = phases[use["phase"]]["tools"][use["name"]]
                        slot["seconds"] += max(0.0, when - use["start"])
                        slot["bytes"] += size
                        phases[use["phase"]]["results"].append((size, use["name"]))
                else:
                    origin = entry.get("origin")
                    if origin is None:
                        continue
                    if not isinstance(origin, dict):
                        raise TypeError("origin")
                    if origin.get("kind") == "human":
                        phases[phase_of(when)]["owner_turns"] += 1
                    elif origin.get("kind") == "peer":
                        phases[phase_of(when)]["handbacks"] += 1
        for req in requests.values():
            phases[req["phase"]]["requests"].append(req)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None
    built = time.time()
    rows = []
    for label in dict.fromkeys(labels):
        p = phases[label]
        if not (p["requests"] or p["owner_turns"] or p["handbacks"] or p["compactions"]
                or p["tools"]):
            continue
        reqs = p["requests"]
        total = {k: sum(r["usage"][k] for r in reqs) for k in TOKEN_KEYS}
        biggest = sorted(p["results"], key=lambda r: -r[0])[:LARGEST]
        rows.append({
            "coordinator": True, "role": "coordinator", "session": Path(path).stem,
            "phase": label, "model": reqs[-1]["model"] if reqs else "",
            "requests": len(reqs), **total,
            "peak_context": max((sum(r["usage"].values()) for r in reqs), default=0),
            "compactions": p["compactions"], "owner_turns": p["owner_turns"],
            "handbacks": p["handbacks"],
            "tools": {name: {"calls": t["calls"], "seconds": round(t["seconds"], 1),
                             "result_kb": round(t["bytes"] / 1024, 1)}
                      for name, t in p["tools"].items()},
            "largest": [{"tool": name, "kb": round(size / 1024, 1)} for size, name in biggest],
            "t": built})
    return rows


def _marks(log: list[dict]) -> list[tuple[float, str]]:
    return [(float(r["t"]), str(r["mark"])) for r in log
            if "mark" in r and isinstance(r.get("t"), (int, float))]


def record(home: Path, root: Path, path: Path) -> int:
    """Append the rows of one session to the project's durable log; the number written."""
    target = agent_runs.durable_path(home, root)
    rows = session_rows(Path(path), _marks(agent_runs.read_json_lines(target)))
    for row in rows or []:
        agent_runs.append(target, row)
    return len(rows or [])


def catch_up(home: Path, root: Path) -> int:
    """Record the sessions that never ended (or ended before this was recorded): every main
    session touched since the oldest phase mark, except one the log already holds as of its last
    write. The number of rows written."""
    log = agent_runs.read_json_lines(agent_runs.durable_path(home, root))
    marks = _marks(log)
    if not marks:
        return 0
    seen: dict[str, float] = {}
    for r in log:
        if r.get("coordinator") and isinstance(r.get("t"), (int, float)):
            seen[r.get("session")] = max(seen.get(r.get("session"), 0.0), float(r["t"]))
    written = 0
    for p in main_sessions(home, root, min(t for t, _ in marks)):
        try:
            if seen.get(p.stem, -1.0) >= p.stat().st_mtime:
                continue
        except OSError:
            continue
        written += record(home, root, p)
    return written
