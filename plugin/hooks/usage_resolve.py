#!/usr/bin/env python3
"""Resolve the usage option for one session and write it: run as a child process by
session_start.py, so a slow profile fetch (no timeout of its own) or a project's own extension
(which may print, write stderr, or exit) never runs in the process that still has to print the
upgrade notice. The parent discards this process's own stdout and stderr either way, and tells
success from failure by exit code alone: this exits 0 only once the work is actually done
(the resolved file written, or confirmed off and any stale one removed); any exception, or a
`SystemExit` raised anywhere in that work (an extension calling `sys.exit(0)` included — its own
exit code must not be mistaken for this resolver's), exits non-zero instead.
"""
import os
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    root, sid = Path(sys.argv[1]), sys.argv[2]
    sys.path.insert(0, str(PLUGIN_ROOT))
    import govern.checks  # noqa: F401  (registers the built-in checks, usage among them)
    from govern import config, layout, usage, usage_setup
    home = layout.home()
    # pin_check=False: this resolver runs the engine bundled with the plugin, not whatever
    # engine version the project happens to pin between upgrades (an upgrade never turns
    # a project red, including "usage is silently off until the project's pin catches up").
    resolved = usage_setup.resolve(config.load(root, home, pin_check=False), root)
    path = usage.resolved_path(home, sid)
    if resolved.get("enabled"):
        usage.write_json(path, resolved)
    else:
        # Nothing to carry through to the per-call hook: no file means "off" to it, the same
        # as a stale one left over from an earlier, enabled session must not.
        try:
            path.unlink()
        except OSError:
            pass


try:
    main()
except BaseException:
    os._exit(1)
