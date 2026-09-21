#!/usr/bin/env python3
"""Install a released engine where governed projects look for it.

    python3 tools/release/install-engine.py vX.Y.Z

Exports `engine/` at the given tag (never the working tree) into
~/.local/share/<tool>/engines/<version>/, where <tool> is the name the tag's own
`govern/layout.py` gives (`context-gate`), and makes it read-only. A governed project's entry point
runs exactly the version its `.context-gate/config.toml` pins, so work in progress in this repo
never reaches a project's gate.

Refuses a tag whose `layout.py` names no tool: it is not a release of this tool. Refuses to
overwrite an installed version: a release is immutable. Stdlib only.
"""
from __future__ import annotations

import io
import os
import re
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TOOL_RE = re.compile(r'^TOOL = "(\w[\w.-]*)"$', re.M)


def main() -> int:
    if len(sys.argv) != 2 or not re.fullmatch(r"v\d+\.\d+\.\d+", sys.argv[1]):
        print(__doc__, file=sys.stderr)
        return 2
    tag = sys.argv[1]
    version = tag[1:]
    res = subprocess.run(["git", "-C", str(REPO), "show", f"{tag}:engine/govern/layout.py"],
                         capture_output=True, text=True, encoding="utf-8")
    if res.returncode != 0:
        print(f"error: git show {tag}:engine/govern/layout.py failed: {res.stderr.strip()}",
              file=sys.stderr)
        return 1
    tool = TOOL_RE.search(res.stdout)
    if not tool:
        print(f"error: {tag}: not a release tag of this tool (engine/govern/layout.py names "
              f"no TOOL)", file=sys.stderr)
        return 1
    home = Path(os.environ.get("HOME") or Path.home())
    dest = home / ".local" / "share" / tool.group(1) / "engines" / version
    if dest.exists():
        print(f"error: {dest} already exists; a release is never overwritten", file=sys.stderr)
        return 1
    try:
        archive = subprocess.run(["git", "-C", str(REPO), "archive", "--format=tar", tag,
                                  "engine/govern"], check=True, capture_output=True).stdout
    except subprocess.CalledProcessError as exc:
        print(f"error: git archive {tag} failed: {exc.stderr.decode().strip()}", file=sys.stderr)
        return 1
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        members = [m for m in tar.getmembers() if m.name.startswith("engine/")]
        for m in members:
            m.name = m.name[len("engine/"):]
        dest.mkdir(parents=True)
        tar.extractall(dest, members=members, filter="data")
    have = (dest / "govern" / "__init__.py").read_text(encoding="utf-8")
    if f'__version__ = "{version}"' not in have:
        print(f"warning: {tag} carries a different __version__; check the release", file=sys.stderr)
    for path in sorted(dest.rglob("*"), reverse=True) + [dest]:
        mode = path.stat().st_mode
        path.chmod(mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    print(f"installed engine {version} -> {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
