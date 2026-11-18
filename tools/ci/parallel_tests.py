#!/usr/bin/env python3
"""Run a unittest tree in parallel, one subprocess per test class.

    python tools/ci/parallel_tests.py <start-dir> [-j N] [--slowest K]

The tests spend their time starting processes (each builds git repositories), not in Python, so
classes run side by side in separate interpreters. Passing classes print nothing; a failing class
prints its whole output. Exit 0 when every class passed, else 1. Standard library only.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path

FAILED_LOAD = "unittest.loader._FailedTest."
RAN = re.compile(r"^Ran (\d+) tests? in ", re.MULTILINE)
OUTCOME = re.compile(r"^(OK|FAILED)(?: \((.*)\))?\s*$", re.MULTILINE)
SKIPPED = re.compile(r"skipped=(\d+)")


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def groups(start: Path) -> dict[str, int]:
    """Class id -> test count. A module that did not import is its own group, by module name."""
    suite = unittest.TestLoader().discover(str(start), top_level_dir=str(start))
    found: dict[str, int] = {}
    for test in flatten(suite):
        tid = test.id()
        key = tid[len(FAILED_LOAD):] if tid.startswith(FAILED_LOAD) else tid.rsplit(".", 1)[0]
        found[key] = found.get(key, 0) + 1
    return found


def run_group(start: Path, key: str) -> dict:
    began = time.monotonic()
    proc = subprocess.run([sys.executable, "-m", "unittest", key], cwd=str(start),
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding="utf-8", errors="replace",
                          env={k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"})
    output = proc.stdout
    ran = RAN.search(output)
    outcome = OUTCOME.search(output)
    ok = proc.returncode == 0 and ran is not None and outcome is not None and outcome.group(1) == "OK"
    skipped = SKIPPED.search(outcome.group(2) or "") if outcome and ok else None
    return {"key": key, "ok": ok, "output": output, "seconds": time.monotonic() - began,
            "ran": int(ran.group(1)) if ran else 0, "skipped": int(skipped.group(1)) if skipped else 0}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("start", help="test start directory")
    ap.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 1, help="workers (default: CPUs)")
    ap.add_argument("--slowest", type=int, default=0, metavar="K", help="also list the K slowest classes")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):   # a narrow console must not lose the summary
        stream.reconfigure(errors="backslashreplace")
    start = Path(args.start).resolve()
    jobs = max(1, args.jobs)
    sys.path.insert(0, str(start))
    found = groups(start)
    order = sorted(found, key=lambda k: (-found[k], k))
    began = time.monotonic()
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        for res in pool.map(lambda k: run_group(start, k), order):
            results.append(res)
            if not res["ok"]:
                print(f"== FAILED {res['key']}")
                print(res["output"], flush=True)
    elapsed = time.monotonic() - began
    failed = sorted(r["key"] for r in results if not r["ok"])
    total = sum(r["ran"] for r in results)
    head = f"Ran {total} tests in {elapsed:.1f}s, {jobs} workers: "
    if failed:
        print(f"{head}FAILED ({len(failed)} classes: {', '.join(failed)})")
    else:
        skipped = sum(r["skipped"] for r in results)
        print(f"{head}OK" + (f" (skipped={skipped})" if skipped else ""))
    if args.slowest > 0:
        print(f"Slowest {min(args.slowest, len(results))}:")
        for r in sorted(results, key=lambda r: -r["seconds"])[:args.slowest]:
            print(f"  {r['seconds']:7.1f}s  {r['key']}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
