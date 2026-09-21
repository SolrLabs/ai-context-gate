"""Findings before and after an install or upgrade.

Written at install and at upgrade, so nobody has to rebuild the previous gate's results by hand to
tell which findings are new. Every new finding is grouped under the check that raised it, with
that check's summary and rationale: each is either something the previous gate missed, or a rule it
did not have, and the rationale says which rule. Findings that disappeared are listed too, and
so are the settings that differ from the engine's defaults.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from govern import manifest

FINDING_RE = re.compile(r"^\s*(ERROR|error|warn|WARN|warning|WARNING)\b[:\s]+(.*\S)\s*$")


@dataclass
class GateRun:
    command: str
    exit: int | None
    findings: list[tuple[str, str]] = field(default_factory=list)   # (level, message)
    unparsed: bool = False
    engine: str | None = None     # the engine version that actually ran, when known


def value(v) -> str:
    """A setting's value as `explain` prints it."""
    return f'"{v}"' if isinstance(v, str) else json.dumps(v, ensure_ascii=False)


def run_old_gate(root: Path, argv: list[str], env: dict | None = None) -> GateRun:
    """Run a gate as a subprocess and read its findings from the common `ERROR msg` / `warn msg`
    line shape. A gate whose output has none of those lines, but did not pass, is marked
    unparsed rather than read as clean."""
    try:
        res = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=600, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        return GateRun(" ".join(argv), None, unparsed=True, findings=[("error", str(exc))])
    found = []
    for line in (res.stdout + "\n" + res.stderr).splitlines():
        m = FINDING_RE.match(line)
        if m:
            found.append(("error" if m.group(1).lower() == "error" else "warn", m.group(2)))
    return GateRun(" ".join(argv), res.returncode, found,
                   unparsed=not found and res.returncode != 0)


def _norm(msg: str) -> str:
    return " ".join(msg.split())


def write(path: Path, title: str, old: GateRun | None, new: list[tuple[str, object]],
          explain: str, engine: str, notes: list[tuple[str, str]] | None = None,
          baseline: list[str] | None = None) -> dict:
    """Write the report; return the counts for a one-line summary."""
    new_flat = [(cid, lvl, msg) for cid, f in new
                for lvl, msgs in (("error", f.errors), ("warn", f.warnings)) for msg in msgs]
    old_set = {_norm(m) for _, m in old.findings} if old else set()
    new_set = {_norm(m) for _, _, m in new_flat}
    fresh = [(cid, lvl, msg) for cid, lvl, msg in new_flat if _norm(msg) not in old_set]
    gone = [(lvl, msg) for lvl, msg in (old.findings if old else []) if _norm(msg) not in new_set]
    same = len(new_flat) - len(fresh)

    lines = [f"# {title}", "", f"Engine {engine}.", ""]
    if old is None:
        lines += ["No previous gate to compare with: every finding below is new.", ""]
    else:
        if old.engine == engine:
            lines += [f"**Both runs used engine {engine}, so there was nothing to compare.** "
                      f"The previous pin had already resolved to {engine} on this machine "
                      f"(a series pin runs the newest installed release in its series), so "
                      f"the findings below are not a comparison between engines.", ""]
        lines += ["| | Before | After |", "|---|---|---|",
                  f"| Engine | {old.engine or 'not an engine (a previous checker)'} | {engine} |",
                  f"| Command | `{old.command}` | the engine |",
                  f"| Exit | {old.exit} | {1 if any(l == 'error' for _, l, _ in new_flat) else 0} |",
                  f"| Errors | {sum(1 for l, _ in old.findings if l == 'error')} | "
                  f"{sum(1 for _, l, _ in new_flat if l == 'error')} |",
                  f"| Warnings | {sum(1 for l, _ in old.findings if l == 'warn')} | "
                  f"{sum(1 for _, l, _ in new_flat if l == 'warn')} |", ""]
        if old.unparsed:
            lines += ["**The previous gate's output could not be read as findings**, so everything "
                      "below is listed as new. Compare by hand before trusting the split.", ""]
        lines += [f"Unchanged: {same} finding(s) reported both before and after.", ""]
    if notes:
        lines += ["## What changed in the engine", "",
                  "Release notes for every version this upgrade brings, newest first. Anything "
                  "under **Upgrading** is for the project to do or decide.", ""]
        for _, body in notes:
            lines += [body.replace("\n## ", "\n### ").replace("## ", "### ", 1), ""]
    if baseline is not None:
        lines += ["## Baseline", "",
                  "Every current ratchet breach not already recorded, added so the project "
                  "starts green — never raising an entry already there.", ""]
        lines += [f"- `{row}`" for row in baseline] or ["None."]
        lines += [""]
    lines += ["## New findings", ""]
    if not fresh:
        lines += ["None.", ""]
    by_check: dict[str, list] = {}
    for cid, lvl, msg in fresh:
        by_check.setdefault(cid, []).append((lvl, msg))
    for cid, items in by_check.items():
        chk = manifest.CHECKS[cid]
        lines += [f"### `{cid}`", "", f"{chk.summary} *Why:* {chk.rationale}", ""]
        lines += [f"- **{lvl}** {msg}" for lvl, msg in items] + [""]
    if old is not None:
        lines += ["## No longer reported", "",
                  "A rule the engine does not have, or a finding it words differently.", ""]
        lines += [f"- **{lvl}** {msg}" for lvl, msg in gone] or ["None."]
        lines += [""]
    lines += ["## Settings that differ from the engine's defaults", "", "```", explain.rstrip(),
              "```", ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return {"new": len(fresh), "gone": len(gone), "same": same,
            "checks": list(by_check)}


def non_default_settings(ctx) -> str:
    """The settings a project changed, as `explain` shows them: what the report's reader needs
    to compare limits before and after."""
    out = []
    for cid, s in ctx.cfg.checks.items():
        chk = manifest.CHECKS[cid]
        changed = [(k, v) for k, v in s.params.items() if s.source.get(k, "engine") != "engine"]
        level = s.source.get("level", "engine") != "engine"
        if not (changed or level or not s.ratchet):
            continue
        head = f"{cid}: {s.level}" + ("" if s.ratchet or not chk.ratchets else ", ratchet off")
        out.append(head)
        for k, v in changed:
            out.append(f"  {k} = {value(v)}  (engine default {value(chk.params[k].default)})")
    raw = ctx.cfg.raw.get("dialect", {})
    if raw:
        out.append("dialect: " + ", ".join(f"{k} = {value(v)}" for k, v in raw.items()))
    return "\n".join(out) or "(none: every setting is the engine default)"
