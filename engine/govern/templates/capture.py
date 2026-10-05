#!/usr/bin/env python3
"""context-gate usage capture, installed as Claude Code's statusLine by `govern usage install`.

Saves four fields of the statusline payload for this session, then runs the statusline the user
had before, with the same input, and passes its output and exit code through. The capture step
can never break the user's statusline: any error in it is swallowed. On Windows the user's
statusline runs through the shell Claude Code itself runs statuslines with.
"""
import json
import ntpath
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HOME = Path(os.environ.get("HOME") or Path.home())
SHARE = HOME / ".local" / "share" / "context-gate"
STATE = HOME / ".local" / "state" / "context-gate" / "usage"


def capture(raw: bytes) -> None:
    data = json.loads(raw)
    sid = data["session_id"]
    if not isinstance(sid, str) or not sid or "/" in sid or "\\" in sid:
        return
    snap = {"context_window": {"used_percentage": (data.get("context_window") or {}).get("used_percentage")},
            "rate_limits": data.get("rate_limits") or {},
            "transcript_path": data.get("transcript_path"),
            "captured_at": int(time.time())}
    STATE.mkdir(parents=True, exist_ok=True)
    path = STATE / f"{sid}.json"
    tmp = STATE / f".{sid}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(snap), encoding="utf-8")
    os.replace(tmp, path)


def chained() -> str | None:
    try:
        sl = json.loads((SHARE / "statusline-chain.json").read_text(encoding="utf-8")).get("statusLine")
    except (OSError, ValueError, AttributeError):
        return None
    return sl.get("command") if isinstance(sl, dict) else None


def git_bash() -> str | None:
    """bash.exe of Git for Windows, or None. Never the `bash` on PATH: on Windows that can be
    System32\\bash.exe, which is WSL, a different machine as far as the user's paths go."""
    env = os.environ
    mine = env.get("CLAUDE_CODE_GIT_BASH_PATH")      # Claude Code's own override
    if mine and os.path.isfile(mine):
        return mine
    found = []
    git = on_path("git")
    if git:
        # git.exe sits in <root>\cmd, <root>\bin or <root>\mingw64\bin; bash is <root>\bin\bash.exe.
        here = ntpath.dirname(git)
        above = ntpath.dirname(here)
        name = ntpath.basename(here).lower()
        if name == "bin" and ntpath.basename(above).lower() == "mingw64":
            found.append(ntpath.dirname(above))
        elif name in ("cmd", "bin"):
            found.append(above)
    for var, sub in (("ProgramFiles", "Git"), ("ProgramFiles(x86)", "Git"),
                     ("LocalAppData", ntpath.join("Programs", "Git"))):
        if env.get(var):
            found.append(ntpath.join(env[var], sub))
    for root in found:
        bash = ntpath.join(root, "bin", "bash.exe")
        if os.path.isfile(bash):
            return bash
    return None


def on_path(name: str) -> str | None:
    """shutil.which, but only an absolute hit. On Windows which() searches the current
    directory first and returns that hit relative: never run a program the repo brought."""
    found = shutil.which(name)
    return found if found and ntpath.isabs(found) else None


def windows_argv(cmd: str) -> tuple[list[str], dict | None] | None:
    """(argv, env) that runs `cmd` on Windows the way Claude Code runs a statusline: through Git
    Bash when it is installed, else through PowerShell. None when neither is found (or a lookup
    fails), and the caller runs it as before. Bash gets the command in the environment, not the
    argument list, so neither list2cmdline nor the MSYS runtime touches it (`~`, `\\`); env is
    None for PowerShell, which inherits."""
    try:
        bash = git_bash()
        if bash:
            return [bash, "-c", 'eval "$CONTEXT_GATE_STATUSLINE"'], {**os.environ, "CONTEXT_GATE_STATUSLINE": cmd}
        # Claude Code's docs say "PowerShell" without saying which: PowerShell 7 (pwsh) when it
        # is on PATH, else the Windows PowerShell every Windows has.
        exe = on_path("pwsh") or on_path("powershell.exe")
        if exe:
            return [exe, "-NoProfile", "-NonInteractive", "-Command", cmd], None
    except Exception:
        pass
    return None


def main() -> int:
    raw = sys.stdin.buffer.read()
    try:
        capture(raw)
    except Exception:
        pass
    cmd = chained()
    if not cmd:
        return 0
    found = windows_argv(cmd) if os.name == "nt" else None
    try:
        if found:
            res = subprocess.run(found[0], input=raw, capture_output=True, env=found[1])
        else:
            res = subprocess.run(cmd, shell=True, input=raw, capture_output=True)
    except Exception:                     # the shell could not be started: as with no chain
        return 0
    sys.stdout.buffer.write(res.stdout)
    sys.stderr.buffer.write(res.stderr)
    return res.returncode


if __name__ == "__main__":
    sys.exit(main())
