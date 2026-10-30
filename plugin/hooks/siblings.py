#!/usr/bin/env python3
"""Find the ids this branch added that another fetched branch also added, and write up to three
lines of them (and a count of the rest), then the decisions and traps teammates changed on the
default branch since this developer's last session (`govern.seen`), as a JSON list to the file
named: run as a child process by session_start.py for a team, so loading the project's config (a
profile fetch, the project's own extensions) never happens in the process that still has to print
the upgrade notice. The parent discards this process's output and reads the file; nothing to say
leaves no file.
"""
import os
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SHOWN = 3


def main() -> None:
    root, out = Path(sys.argv[1]), Path(sys.argv[2])
    sys.path.insert(0, str(PLUGIN_ROOT))
    import govern.checks  # noqa: F401  (registers the built-in checks the config load needs)
    from govern import agent_runs, cli, refs, seen
    ctx = cli.load_context(root, "govern")
    found = refs.sibling_collisions(ctx)
    lines = found[:SHOWN]
    if len(found) > SHOWN:
        lines.append(f"and {len(found) - SHOWN} more: run python3 .context-gate/bin/govern "
                     f"check --siblings")
    # The sibling lines go to the file before the seen lines are worked out: the parent kills
    # this process after a few seconds, and a kill during `seen` keeps what was already said.
    if lines:
        agent_runs.write_json(out, lines)
    else:
        try:
            out.unlink()
        except OSError:
            pass
    more = seen.lines(ctx)
    if more:
        agent_runs.write_json(out, lines + more)


try:
    main()
except BaseException:
    os._exit(1)
