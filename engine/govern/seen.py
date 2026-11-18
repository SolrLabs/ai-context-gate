"""What teammates changed in governance since this developer's last session.

Session start for a team lists the decisions and traps that changed on the default branch
since the commit this clone last saw. That commit is kept per project in
`~/.local/state/context-gate/seen/<key>.json`, `{"branch": ..., "commit": ...}`, and moved to
the tip each time the list is made. Nothing here fetches or asks a remote: the tip is a ref this
clone already holds. Any failure says nothing and leaves the state as it was.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from govern import agent_runs, base, changes
from govern.context import git

SHOWN = 10
# An id as a heading carries it, safe to put inside a git regular expression unescaped.
IDENT_RE = re.compile(r"[A-Za-z0-9_]+-[0-9]+")


def state_path(ctx) -> Path:
    """The state file for this project: keyed by its origin URL, else by where it is."""
    url = git(ctx.root, "config", "remote.origin.url")
    seed = url if url else str(ctx.root.resolve())
    key = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]
    return agent_runs.agents_dir(ctx.home).parent / "seen" / f"{key}.json"


def _tip(root: Path) -> tuple[str, str] | None:
    """`(the default branch, the commit its tip is at)` from refs this clone has, and only
    those: the branch `refs/remotes/origin/HEAD` points at, else the first of `main` and
    `master` this clone holds. Never asks the remote: session start must not wait on one."""
    head = git(root, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    prefix = "refs/remotes/origin/"
    names = [head[len(prefix):]] if head and head.startswith(prefix) else ["main", "master"]
    for name in names:
        for ref in (f"refs/remotes/origin/{name}", f"refs/heads/{name}"):
            sha = git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
            if sha:
                return name, sha
    return None


def _span(text: str, ident: str) -> tuple[int, int] | None:
    """`(first, last)` line numbers of `ident`'s entry in `text`: its heading to the line
    before the next heading of the same level or generated block (the index the log carries,
    which every change to any entry rewrites), else the last line. None without a heading."""
    rows = text.splitlines()
    pattern = re.compile(rf"^(#+) {ident}([^0-9]|$)")
    for n, row in enumerate(rows):
        found = pattern.match(row)
        if found:
            same = re.compile(rf"^{'#' * len(found.group(1))} ")
            end = next((m for m in range(n + 1, len(rows))
                        if same.match(rows[m]) or rows[m].startswith("<!-- gov:generated:")),
                       len(rows))
            # Trailing blank lines are the gap before the next entry, not the entry: a
            # neighbour appended after it adds one, and would be taken for a change to it.
            while end > n + 1 and not rows[end - 1].strip():
                end -= 1
            return n + 1, end
    return None


def _author(ctx, old: str, tip: str, ident: str, log: str, change: str) -> str | None:
    """The email of the newest commit in `old..tip` that changed `ident`'s entry in `log`: for
    an entry still there, one that touched its lines (a body-only change included); for a
    removed one, one that touched its heading. None when none can be found."""
    root = ctx.root
    span = f"{old}..{tip}"
    if not IDENT_RE.fullmatch(ident):
        return None
    if change == "removed":
        return git(root, "log", "-1", "--format=%ae", "-G", f"^#+ {ident}([^0-9]|$)", span,
                   "--", log) or None
    try:
        found = _span(base.read(root, tip, log), ident)
    except base.CannotCompare:
        return None
    if found is None:
        return None
    return git(root, "log", "-1", "-s", "--format=%ae", "-L", f"{found[0]},{found[1]}:{log}",
               span) or None


def lines(ctx) -> list[str]:
    """`since your last session: <id> <change> (<author>)` for each decision or trap changed
    on the default branch since the last session, up to ten and a tail naming the rest. `[]`
    on a first run, a different branch, an unknown commit, no change or any failure."""
    try:
        return _lines(ctx)
    except Exception:
        return []


def _lines(ctx) -> list[str]:
    root = ctx.root
    found = _tip(root)
    if found is None:
        return []
    name, tip = found
    path = state_path(ctx)
    state = agent_runs.read_json(path)
    sound = (isinstance(state, dict) and isinstance(state.get("branch"), str)
             and isinstance(state.get("commit"), str))
    if not sound or state["branch"] != name:
        agent_runs.write_json(path, {"branch": name, "commit": tip})
        return []
    old = state["commit"]
    if old == tip:
        return []
    if git(root, "cat-file", "-e", f"{old}^{{commit}}") is None:
        agent_runs.write_json(path, {"branch": name, "commit": tip})
        return []
    rows = changes.between(ctx, old, tip)
    # Before any author is looked up: a session killed part way must not show these again.
    agent_runs.write_json(path, {"branch": name, "commit": tip})
    mine = (git(root, "config", "user.email") or "").strip().lower()
    out: list[str] = []
    examined = 0
    for row in rows:
        if len(out) >= SHOWN:
            break
        examined += 1
        ident, log = row.data["id"], row.data["log"]
        email = (_author(ctx, old, tip, ident, log, row.data["change"]) or "").strip()
        if mine and email.lower() == mine:
            continue
        who = email.partition("@")[0] if email else "unknown"
        out.append(f"since your last session: {ident} {row.data['change']} ({who})")
    if examined < len(rows):
        out.append(f"and {len(rows) - examined} more changes: run python3 "
                   f".context-gate/bin/govern diff")
    return out
