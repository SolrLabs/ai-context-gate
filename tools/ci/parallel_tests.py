#!/usr/bin/env python3
"""Run a unittest tree in parallel, one subprocess per test class (large classes in shards).

    python tools/ci/parallel_tests.py <start-dir> [-j N] [--slowest K] [--times FILE]
                                  [--skip-tier NAME]

The tests spend their time starting processes (each builds git repositories), not in Python, so
classes run side by side in separate interpreters, longest first by the seconds the last run
recorded in tools/ci/.test-times.json. Passing classes print nothing; a failing class
prints its whole output. Exit 0 when every class passed, else 1. Standard library only.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import statistics
import subprocess
import sys
import time
import unittest
from pathlib import Path

FAILED_LOAD = "unittest.loader._FailedTest."
RAN = re.compile(r"^Ran (\d+) tests? in ", re.MULTILINE)
OUTCOME = re.compile(r"^(OK|FAILED)(?: \((.*)\))?\s*$", re.MULTILINE)
SKIPPED = re.compile(r"skipped=(\d+)")
REPO = Path(__file__).resolve().parents[2]
TIMES = Path(__file__).parent / ".test-times.json"
TIER_MARK = re.compile(r"^TIER\s*=\s*[\"']([^\"']+)[\"']\s*$", re.MULTILINE)
SHARD_ABOVE = 10   # a class with more tests than this is split
SHARD_SIZE = 8     # into shards of about this many


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def groups(start: Path) -> dict[str, list[str]]:
    """Class id -> test ids, in discovery order. A module that did not import is its own group,
    by module name, holding its one failed-load id."""
    suite = unittest.TestLoader().discover(str(start), top_level_dir=str(start))
    found: dict[str, list[str]] = {}
    for test in flatten(suite):
        tid = test.id()
        key = tid[len(FAILED_LOAD):] if tid.startswith(FAILED_LOAD) else tid.rsplit(".", 1)[0]
        found.setdefault(key, []).append(tid)
    return found


def tier_modules(start: Path, tiers: list[str]) -> list[str]:
    """Dotted names of the test modules under start whose text carries a top-level `TIER = "<name>"`
    line for one of `tiers`. Read as text, never imported."""
    wanted = set(tiers)
    out = []
    for path in sorted(start.rglob("test*.py")):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, ValueError):
            continue
        if wanted & set(TIER_MARK.findall(text)):
            out.append(".".join(path.relative_to(start).with_suffix("").parts))
    return out


def in_modules(key: str, modules: list[str]) -> bool:
    return any(key == m or key.startswith(m + ".") for m in modules)


def shards(found: dict[str, list[str]]) -> list[dict]:
    """Split each class of more than SHARD_ABOVE tests into ceil(count / SHARD_SIZE) near-equal
    consecutive chunks. A small class, or one that did not import, is one shard run by its class id."""
    out = []
    for key, ids in found.items():
        if len(ids) <= SHARD_ABOVE or ids[0].startswith(FAILED_LOAD):
            out.append({"key": key, "label": key, "targets": [key], "size": len(ids), "total": len(ids)})
            continue
        n = -(-len(ids) // SHARD_SIZE)
        base, extra = divmod(len(ids), n)
        at = 0
        for i in range(n):
            take = base + (1 if i < extra else 0)
            out.append({"key": key, "label": f"{key} [shard {i + 1}/{n}]", "targets": ids[at:at + take],
                        "size": take, "total": len(ids)})
            at += take
    return out


def start_key(start: Path) -> str:
    try:
        return start.relative_to(REPO).as_posix()
    except ValueError:
        return start.as_posix()


def read_times(path: Path, skey: str) -> dict[str, float]:
    """Class id -> seconds from the last run of this start directory; {} when the file is not usable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        entry = data[skey]
        if not isinstance(entry, dict):
            return {}
        return {k: float(v) for k, v in entry.items()
                if isinstance(v, (int, float)) and not isinstance(v, bool)}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def write_times(path: Path, skey: str, seconds: dict[str, float], keep: dict[str, float] | None = None) -> None:
    """Replace this start key's map, keep the others; temp file then os.replace. Never raises.
    `keep` (old seconds of classes skipped this run) is added under the run's own."""
    try:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data[skey] = {k: round(v, 3) for k, v in sorted({**(keep or {}), **seconds}.items())}
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        print(f"warning: could not write {path}: {exc}", file=sys.stderr)


def order(work: list[dict], known: dict[str, float]) -> list[dict]:
    """Longest estimated first. Known class: its seconds x shard share. Unknown: count x the median
    seconds per test of the known classes, or the bare count when nothing is known."""
    sizes = {}
    for w in work:
        sizes[w["key"]] = w["total"]
    rates = [known[k] / n for k, n in sizes.items() if k in known and n]
    rate = statistics.median(rates) if rates else 1.0

    def estimate(w: dict) -> float:
        if w["key"] in known:
            return known[w["key"]] * w["size"] / w["total"]
        return w["size"] * rate
    return sorted(work, key=lambda w: (-estimate(w), w["label"]))


# Measured on an 18-CPU Mac, engine suite with --skip-tier e2e: -j18 166.6 s, -j12 179.1 s,
# -j9 164.3 s, -j6 155.2 s. More workers than this only adds contention for process starts.
DEFAULT_JOBS = 6


def default_jobs() -> int:
    return min(DEFAULT_JOBS, os.cpu_count() or 1)


def run_group(start: Path, work: dict | str) -> dict:
    """Run one shard, or a whole class given by its id."""
    if isinstance(work, str):
        work = {"key": work, "label": work, "targets": [work]}
    key = work["key"]
    began = time.monotonic()
    proc = subprocess.run([sys.executable, "-m", "unittest", *work["targets"]], cwd=str(start),
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding="utf-8", errors="replace",
                          env={k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"})
    output = proc.stdout
    ran = RAN.search(output)
    outcome = OUTCOME.search(output)
    ok = proc.returncode == 0 and ran is not None and outcome is not None and outcome.group(1) == "OK"
    skipped = SKIPPED.search(outcome.group(2) or "") if outcome and ok else None
    return {"key": key, "label": work["label"], "ok": ok, "output": output, "seconds": time.monotonic() - began,
            "ran": int(ran.group(1)) if ran else 0, "skipped": int(skipped.group(1)) if skipped else 0}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("start", help="test start directory")
    ap.add_argument("-j", "--jobs", type=int, default=default_jobs(), help=f"workers (default: the CPU count, at most {DEFAULT_JOBS})")
    ap.add_argument("--slowest", type=int, default=0, metavar="K", help="also list the K slowest classes")
    ap.add_argument("--times", type=Path, default=TIMES, help="file holding seconds per class from the last run")
    ap.add_argument("--skip-tier", action="append", default=[], metavar="NAME",
                    help="skip test modules marked TIER = \"NAME\" (repeatable)")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):   # a narrow console must not lose the summary
        stream.reconfigure(errors="backslashreplace")
    start = Path(args.start).resolve()
    jobs = max(1, args.jobs)
    sys.path.insert(0, str(start))
    found = groups(start)
    skey = start_key(start)
    known = read_times(args.times, skey)
    by_tier = {t: tier_modules(start, [t]) for t in args.skip_tier}
    skipped_modules = sorted({m for mods in by_tier.values() for m in mods})
    dropped = {t: sum(any(in_modules(k, [m]) for k in found) for m in mods) for t, mods in by_tier.items()}
    found = {k: v for k, v in found.items() if not in_modules(k, skipped_modules)}
    work = order(shards(found), known)
    began = time.monotonic()
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        for res in pool.map(lambda w: run_group(start, w), work):
            results.append(res)
            if not res["ok"]:
                print(f"== FAILED {res['label']}")
                print(res["output"], flush=True)
    elapsed = time.monotonic() - began
    failed = sorted({r["key"] for r in results if not r["ok"]})
    per_class: dict[str, float] = {}
    for r in results:
        per_class[r["key"]] = per_class.get(r["key"], 0.0) + r["seconds"]
    write_times(args.times, skey, per_class, {k: v for k, v in known.items() if in_modules(k, skipped_modules)})
    total = sum(r["ran"] for r in results)
    head = f"Ran {total} tests in {elapsed:.1f}s, {jobs} workers: "
    note = "".join(f" [skipped tier {t}: {dropped[t]} modules]" for t in args.skip_tier)
    if failed:
        print(f"{head}FAILED ({len(failed)} classes: {', '.join(failed)}){note}")
    else:
        skipped = sum(r["skipped"] for r in results)
        print(f"{head}OK" + (f" (skipped={skipped})" if skipped else "") + note)
    if args.slowest > 0:
        print(f"Slowest {min(args.slowest, len(per_class))}:")
        for key, secs in sorted(per_class.items(), key=lambda kv: (-kv[1], kv[0]))[:args.slowest]:
            print(f"  {secs:7.1f}s  {key}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
