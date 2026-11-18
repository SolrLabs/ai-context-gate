"""Overnight driver measurements: one row per driver session, a shared log, a threshold suggestion."""
from __future__ import annotations

import json
import math
import statistics
from pathlib import Path

from govern import usage
from govern.coordinator_runs import TOKEN_KEYS

MIN_ROWS = 20
CLEAN_EXITS = ("continue", "plan-done", "stop")
META_KEYS = ("project", "plan", "phase", "next", "started", "ended", "exit")


def log_path(home: Path) -> Path:
    return usage.state_dir(home).parent / "drive" / "sessions.jsonl"


def _num(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _size(input_tokens, cache_read, cache_creation) -> int | None:
    """Input side of one request: fresh plus cache read plus cache creation."""
    values = (input_tokens, cache_read, cache_creation)
    if not all(_num(v) for v in values):
        return None
    return int(sum(values))


def _result_fields(result) -> tuple[str | None, dict | None, int | None]:
    if not isinstance(result, dict):
        return None, None, None
    session = result.get("session_id")
    if not isinstance(session, str):
        session = None
    raw = result.get("usage")
    tokens = None
    if isinstance(raw, dict):
        tokens = {k: raw[k] for k in TOKEN_KEYS if _num(raw.get(k))}
    window = None
    models = result.get("modelUsage")
    if isinstance(models, dict):
        best = -1
        for entry in models.values():
            if not isinstance(entry, dict):
                continue
            size = _size(entry.get("inputTokens", 0), entry.get("cacheReadInputTokens", 0),
                         entry.get("cacheCreationInputTokens", 0))
            win = entry.get("contextWindow")
            if size is not None and _num(win) and size > best:
                best, window = size, int(win)
    return session, tokens, window


def _transcript_fields(transcript) -> tuple[int | None, int | None]:
    """(peak_tokens, compacts); both None when the file is missing or not all JSON lines."""
    if transcript is None:
        return None, None
    try:
        text = Path(transcript).read_text(encoding="utf-8")
        entries = [json.loads(line) for line in text.splitlines() if line.strip()]
    except (OSError, ValueError):
        return None, None
    peak = None
    compacts = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") == "system" and entry.get("subtype") == "compact_boundary":
            compacts += 1
        if entry.get("type") == "assistant":
            message = entry.get("message")
            raw = message.get("usage") if isinstance(message, dict) else None
            if not isinstance(raw, dict):
                continue
            size = _size(raw.get("input_tokens", 0), raw.get("cache_read_input_tokens", 0),
                         raw.get("cache_creation_input_tokens", 0))
            if size is not None and (peak is None or size > peak):
                peak = size
    return peak, compacts


def session_row(transcript: Path | None, result: dict | None, meta: dict) -> dict:
    row = {k: meta.get(k) for k in META_KEYS}
    session, tokens, window = _result_fields(result)
    peak, compacts = _transcript_fields(transcript)
    row["session"] = session
    row["tokens"] = tokens
    row["window"] = window
    row["peak_tokens"] = peak
    row["compacts"] = compacts
    row["peak_pct"] = round(100 * peak / window, 1) if peak is not None and window else None
    return row


def append(home: Path, row: dict) -> None:
    path = log_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def rows(home: Path, project: str | None = None) -> list[dict]:
    try:
        text = log_path(home).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return []
    out = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and (project is None or row.get("project") == project):
            out.append(row)
    return out


def _pcts(rows_: list[dict]) -> list[float]:
    return sorted(r["peak_pct"] for r in rows_ if _num(r.get("peak_pct")))


def _p95(values: list[float]) -> float:
    return values[math.ceil(0.95 * len(values)) - 1]


def suggest(rows: list[dict]) -> int | None:
    values = _pcts(rows)
    if len(values) < MIN_ROWS:
        return None
    raised = math.ceil((_p95(values) + 10) / 5) * 5
    return max(20, min(90, raised))


def _block(name: str, rows_: list[dict]) -> list[str]:
    values = _pcts(rows_)
    lines = [name, f"  sessions: {len(rows_)}"]
    if values:
        lines.append(f"  peak %: median {statistics.median(values):.1f}, p95 {_p95(values):.1f}")
    else:
        lines.append("  peak %: no data")
    lines.append(f"  compacts: {sum(r['compacts'] for r in rows_ if _num(r.get('compacts')))}")
    lines.append(f"  not clean exits: {sum(1 for r in rows_ if r.get('exit') not in CLEAN_EXITS)}")
    suggestion = suggest(rows_)
    if suggestion is None:
        lines.append(f"  suggested --compact-pct: too few sessions ({len(values)} of {MIN_ROWS})")
    else:
        lines.append(f"  suggested --compact-pct: {suggestion}")
    return lines


def report(home: Path, project: str | None = None) -> str:
    every = rows(home, project)
    if project is not None:
        if not every:
            return f"no sessions recorded for {project}\n"
        return "\n".join(_block(project, every)) + "\n"
    names = sorted({str(r.get("project")) for r in every})
    out: list[str] = []
    for name in names:
        out += _block(name, [r for r in every if str(r.get("project")) == name]) + [""]
    out += _block("all projects", every)
    return "\n".join(out) + "\n"
