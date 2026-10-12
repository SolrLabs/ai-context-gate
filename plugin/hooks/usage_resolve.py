#!/usr/bin/env python3
"""Resolve the usage option and the agent options (agent-gates, agent-measure) for one session
and write them, and, for a project with a build folder, the line that says which build the
checked-out branch has in flight: run as a child process by
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
    if len(sys.argv) > 3:
        # Before the config is loaded: the line needs none of it, and must not be lost to a
        # config that fails to load.
        _build(root, Path(sys.argv[3]))
    import govern.checks  # noqa: F401  (registers the built-in checks, usage among them)
    from govern import agent_runs, config, layout, usage, usage_setup
    home = layout.home()
    # pin_check=False: this resolver runs the engine bundled with the plugin, not whatever
    # engine version the project happens to pin between upgrades (an upgrade never turns
    # a project red, including "usage is silently off until the project's pin catches up").
    cfg = config.load(root, home, pin_check=False)
    failed = None
    for write in (lambda: _usage(cfg, root, home, sid, usage, usage_setup),
                  lambda: _agents(cfg, root, home, sid, agent_runs)):
        try:                      # one option's failure must not leave the other unresolved
            write()
        except Exception as exc:
            failed = failed or exc
    if failed:
        raise failed


def _build(root: Path, path: Path) -> None:
    """Write the session's build line to `path`, the file the parent named, or remove it when
    the checked-out branch has no build in flight. The parent passes `path` only for a project
    with a build folder. A ledger or a git that cannot say leaves no line and fails nothing:
    `govern build status` is where that is reported."""
    try:
        from govern import agent_runs, build
        line = build.session_line(root)
        if line:
            agent_runs.write_json(path, line)
            return
    except Exception:
        pass
    try:
        path.unlink()
    except OSError:
        pass


def _usage(cfg, root, home, sid, usage, usage_setup) -> None:
    resolved = usage_setup.resolve(cfg, root)
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


def _agents(cfg, root, home, sid, agent_runs) -> None:
    resolved = agent_runs.resolve(cfg, root)
    path = agent_runs.resolved_path(home, sid)
    if resolved["gates"] or resolved["measure"]:
        agent_runs.write_json(path, resolved)
    else:
        try:
            path.unlink()
        except OSError:
            pass


try:
    main()
except BaseException:
    os._exit(1)
