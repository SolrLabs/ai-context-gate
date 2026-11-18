"""`govern sweep`: a read-only report, for the end of a session, of every linked worktree and
local branch of the session root (and, in a workspace, of each member that is a repository of its
own), each with a verdict saying whether it can go, plus finished working files, branches not
pushed, builds in flight and the size of the memory index.

Verdicts:
  MERGED      an ancestor of the default branch's tip
  PR-MERGED   its pull request into the default branch merged at this exact tip (a squash merge
              defeats the ancestry test)
  IN-USE      the default branch, the primary checkout's branch, the branch of a build in flight,
              a branch the `merged-branches` check's `keep` list names, or a worktree a process
              works in
  DIRTY       the worktree has uncommitted changes
  PUSHED      its exact tip is on a remote and has sat idle `[sweep] stale_days` days (the remote
              and the bundle both keep it)
  STALE       a remote branch with no open pull request, not merged, idle `[sweep]
              stale_remote_days` days (the bundle keeps it)
  REVIEW      anything else a person should decide: an open pull request, a closed one, one merged
              into another base, commits beyond it, none at all, a worktree locked by a live
              process or by none we can name, detached or gone, or a question git, `gh` or `lsof` could not answer

The report removes nothing. Every git call fails closed: a repository git cannot read is reported,
and the exit code is 1, never an empty answer.

A remote branch (`origin/spike`) is an item of kind `remote`, judged the same way, MERGED when its
tip is in the remote's default branch.

`--apply --only NAME...` removes exactly the named items whose verdict, worked out again, is MERGED,
PR-MERGED, PUSHED (the remote still holding that tip, asked again) or STALE: a verified bundle of the
branches first, then `git worktree remove` and `git branch -D`, and for a remote branch
`git push <remote> --delete` when the remote still holds the judged tip, never forced. `--home`
first fetches, and moves the checkout from a removable branch to the default branch.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import date
from fnmatch import fnmatchcase
from pathlib import Path

from govern import build, config, manifest, phase

MERGED, PR_MERGED, PUSHED, STALE = "MERGED", "PR-MERGED", "PUSHED", "STALE"
IN_USE, DIRTY, REVIEW = "IN-USE", "DIRTY", "REVIEW"
VERDICTS = (MERGED, PR_MERGED, PUSHED, STALE, IN_USE, DIRTY, REVIEW)
REMOVABLE = (MERGED, PR_MERGED, PUSHED, STALE)
GH_TIMEOUT = 30
FETCH_TIMEOUT = 60
LS_REMOTE_TIMEOUT = 60
LSOF_TIMEOUT = 20
GH_FIELDS = "number,state,headRefOid,baseRefName"
# Remote branches that outlive a release: at or behind the default branch they would read MERGED.
LONG_LIVED = ("develop", "dev", "staging", "stage", "production", "prod", "trunk")


@dataclass
class Item:
    kind: str            # "worktree" | "branch" | "remote"
    repo: Path           # the repository's top
    name: str            # the project's name, or the root's folder name
    branch: str          # "" when detached; for a remote item, the branch on the remote
    tip: str
    path: Path | None    # the worktree's folder, for kind == "worktree"
    verdict: str
    note: str = ""
    remote: str = ""     # for PUSHED: the remote that holds the tip; for a remote item, its name

    def label(self) -> str:
        """The name `--only` uses: the branch, or `remote/branch` for a remote item."""
        return f"{self.remote}/{self.branch}" if self.kind == "remote" else self.branch


@dataclass
class Section:
    name: str
    shown: str                               # the repository's path from the session root
    repo: Path
    items: list[Item] = field(default_factory=list)
    unpushed: list[str] = field(default_factory=list)
    builds: list[tuple[str, str]] = field(default_factory=list)
    finished: list[tuple[Path, str, int]] = field(default_factory=list)
    error: str = ""


def _run(argv: list[str], cwd: Path | None, timeout: int) -> subprocess.CompletedProcess | None:
    """One outside program (`gh`, `lsof`), or None when it could not run. Tests replace this."""
    try:
        return subprocess.run(argv, cwd=str(cwd) if cwd else None, capture_output=True,
                              timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def _text(res: subprocess.CompletedProcess) -> str:
    return res.stdout.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------- outside programs

def gh_prs(repo: Path, branch: str) -> list[dict] | None:
    """The pull requests whose head is `branch`, newest first, or None when `gh` cannot be
    asked: missing, failing, or not answering in JSON."""
    gh = os.environ.get("CONTEXT_GATE_GH") or "gh"
    res = _run([gh, "pr", "list", "--state", "all", "--head", branch, "--json", GH_FIELDS,
                "--limit", "20"], repo, GH_TIMEOUT)
    if res is None or res.returncode != 0:
        return None
    try:
        prs = json.loads(_text(res))
    except ValueError:
        return None
    return [p for p in prs if isinstance(p, dict)] if isinstance(prs, list) else None


def process_cwds() -> set[str] | None:
    """The working directory of every process of this user, or None when that cannot be told:
    not a POSIX system, or `lsof` is missing or fails."""
    getuid = getattr(os, "getuid", None)
    if os.name != "posix" or getuid is None:
        return None
    res = _run(["lsof", "-a", "-u", str(getuid()), "-d", "cwd", "-Fn"], None, LSOF_TIMEOUT)
    if res is None or res.returncode != 0:
        return None
    return {os.path.realpath(line[1:]) for line in _text(res).splitlines()
            if line.startswith("n/")}


def _within(folder: str, cwds: set[str]) -> bool:
    here = os.path.normcase(folder)
    return any(c == here or c.startswith(here + os.sep)
               for c in (os.path.normcase(c) for c in cwds))


# ---------------------------------------------------------------------------- one repository

def _blocks(repo: Path) -> list[dict[str, str]]:
    """`git worktree list --porcelain` as one dict per worktree, the primary checkout first."""
    found = []
    for block in phase._ask(repo, "worktree", "list", "--porcelain").split("\n\n"):
        fields = dict((line.split(" ", 1) + [""])[:2] for line in block.splitlines() if line)
        if "worktree" in fields:
            found.append(fields)
    return found


def _branches(repo: Path) -> list[tuple[str, str, str]]:
    """`(name, tip, upstream)` for every local branch, by name; upstream is "" when it has none."""
    out = phase._ask(repo, "for-each-ref", "--format=%(refname:short)%00%(objectname)%00"
                     "%(upstream:short)", "refs/heads/")
    rows = []
    for line in out.splitlines():
        parts = line.split("\0")
        if len(parts) == 3 and parts[0]:
            rows.append((parts[0], parts[1], parts[2]))
    return rows


def _idle_days(repo: Path, tip: str) -> int | None:
    """Whole days since the tip's commit, or None when git cannot say."""
    when = phase._git(repo, "log", "-1", "--format=%ct", tip)
    if when is None or when.returncode != 0 or not phase._out(when).isdigit():
        return None
    return max(0, int(time.time() - int(phase._out(when))) // 86400)


def _pushed(repo: Path, branch: str, tip: str, stale_days: int) -> tuple[str, str] | None:
    """`(remote, note)` when a remote-tracking ref of `branch` is exactly at `tip` and the tip's
    commit is at least `stale_days` days old, else None (also when git cannot say)."""
    res = phase._git(repo, "for-each-ref", "--format=%(refname)%00%(objectname)", "refs/remotes/")
    names = phase._git(repo, "remote")
    if res is None or res.returncode != 0 or names is None or names.returncode != 0:
        return None
    holders = {f"refs/remotes/{r}/{branch}": r for r in phase._out(names).split()}
    found = []
    for line in phase._out(res).splitlines():
        ref, _, sha = line.partition("\0")
        if sha == tip and ref in holders:
            found.append(holders[ref])
    if not found:
        return None
    days = _idle_days(repo, tip)
    if days is None or days < stale_days:
        return None
    remote = min(found)
    return remote, f"on {remote}/{branch}, idle {days} days"


def _judge(repo: Path, default: str, ref: str, branch: str, tip: str,
           stale_days: int = config.SWEEP_STALE_DAYS_DEFAULT) -> tuple[str, str, str]:
    """The verdict, note and (for PUSHED) remote of a branch nothing else claims: MERGED,
    PR-MERGED, PUSHED or REVIEW."""
    verdict, note = _decide(repo, default, ref, branch, tip)
    if verdict == REVIEW and (note == f"not in {default} and no PR"
                              or note.endswith("closed unmerged at this tip")):
        found = _pushed(repo, branch, tip, stale_days)
        if found:
            return PUSHED, found[1], found[0]
    return verdict, note, ""


def _judge_remote(repo: Path, default: str, ref: str, branch: str, tip: str,
                  stale_remote_days: int) -> tuple[str, str]:
    """The verdict and note of a remote branch: MERGED, PR-MERGED, STALE or REVIEW."""
    verdict, note = _decide(repo, default, ref, branch, tip)
    if verdict == REVIEW and stale_remote_days > 0 and (
            note == f"not in {default} and no PR" or note.endswith("closed unmerged at this tip")):
        days = _idle_days(repo, tip)
        if days is not None and days >= stale_remote_days:
            return STALE, f"idle {days} days"
    return verdict, note


def _remote_items(section: Section, default: str, remote: str | None, claimed: dict[str, str],
                  keep: list[str], stale_remote_days: int, checked_out: str = "") -> None:
    """Add an item for each branch of `remote` (the one the default branch is judged against:
    `gh` answers for one repository) that nothing else claims, and whose local namesake, if
    there is one, is MERGED or PR-MERGED (any other namesake is judged locally first, and a
    PUSHED one is kept by that very remote branch). `checked_out` is the primary checkout's
    branch when it is MERGED or PR-MERGED: it is claimed, but its remote goes with it. Branches
    named in `LONG_LIVED` are never listed."""
    repo = section.repo
    if not remote:
        return
    out = phase._ask(repo, "for-each-ref", "--format=%(refname)%00%(objectname)",
                     f"refs/remotes/{remote}/")
    local: dict[str, bool] = {}
    for it in section.items:
        if it.branch:
            local[it.branch] = local.get(it.branch, True) and it.verdict in (MERGED, PR_MERGED)
    if checked_out:
        local[checked_out] = True
    for line in out.splitlines():
        ref, _, tip = line.partition("\0")
        branch = ref.removeprefix(f"refs/remotes/{remote}/")
        if not branch or branch == "HEAD" or branch == default or branch in LONG_LIVED:
            continue
        if (branch in claimed and branch != checked_out) or any(
                fnmatchcase(branch, p) for p in keep):
            continue
        if not local.get(branch, True):
            continue
        base = f"refs/remotes/{remote}/{default}"
        if build._known(repo, base):
            verdict, note = _judge_remote(repo, default, base, branch, tip, stale_remote_days)
        else:
            verdict, note = REVIEW, f"{remote}/{default} is not known here"
        section.items.append(Item("remote", repo, section.name, branch, tip, None, verdict,
                                  note, remote))


def _decide(repo: Path, default: str, ref: str, branch: str, tip: str) -> tuple[str, str]:
    prs = gh_prs(repo, branch)
    for pr in prs or []:
        if pr.get("state") == "OPEN":
            return REVIEW, f"open PR #{pr.get('number')}"
    if phase._yes(repo, "merge-base", "--is-ancestor", tip, ref):
        return MERGED, ""
    if prs is None:
        return REVIEW, f"not in {default}, and gh could not be asked"
    for pr in prs:
        state, number = pr.get("state"), pr.get("number")
        if state not in ("MERGED", "CLOSED"):
            continue
        if pr.get("headRefOid") != tip:
            return REVIEW, f"PR #{number} {str(state).lower()}, but the branch has moved since"
        if state == "CLOSED":
            return REVIEW, f"PR #{number} closed unmerged at this tip"
        base = pr.get("baseRefName") or "?"
        if base != default:
            return REVIEW, f"PR #{number} merged into {base}, not {default}"
        return PR_MERGED, f"PR #{number}"
    return REVIEW, f"not in {default} and no PR"


def _unpushed(repo: Path, branch: str, upstream: str) -> str:
    if upstream:
        res = phase._git(repo, "rev-list", "--count", f"{upstream}..refs/heads/{branch}")
        if res is None:
            raise phase.PhaseError("git could not run")
        if res.returncode == 0:
            n = int(phase._out(res) or 0)
            return f"unpushed  {branch}  {n} commit(s) not on {upstream}" if n else ""
        return f"unpushed  {branch}  upstream {upstream} is gone"
    return f"unpushed  {branch}  no upstream"


def _worktree_verdict(fields: dict[str, str], branch: str, judged: tuple[str, str, str],
                      cwds: set[str] | None, here: str) -> tuple[str, str]:
    path = Path(fields["worktree"])
    if "prunable" in fields or not path.is_dir():
        return REVIEW, "directory missing; `git worktree prune` clears it"
    status = phase._git(path, "status", "--porcelain")
    if status is None or status.returncode != 0:
        return REVIEW, "git status failed"
    changes = phase._out(status)
    if changes:
        return DIRTY, f"{len(changes.splitlines())} uncommitted change(s)"
    real = os.path.realpath(path)
    if _within(real, {here} | (cwds or set())):
        return IN_USE, "a process is working in it"
    if "locked" in fields:
        pid = phase.lock_pid(fields["locked"])
        if pid is None:
            return REVIEW, "locked"
        if phase.pid_state(pid) not in ("gone", "session"):
            return REVIEW, f"locked by pid {pid}"
    if not branch:
        return REVIEW, "detached HEAD"
    verdict, note, _ = judged
    if verdict in REMOVABLE and cwds is None:
        return REVIEW, "cannot tell whether a process works in it"
    return verdict, note


PROTECTED_NOTE = "a protected branch (merged-branches keep)"


def _keep(ctx) -> list[str]:
    """The `merged-branches` check's `keep` patterns, whatever its level."""
    settings = ctx.cfg.checks.get("merged-branches")
    if settings is None or "keep" not in settings.params:
        return [str(p) for p in manifest.CHECKS["merged-branches"].params["keep"].default]
    return [str(p) for p in settings.params["keep"]]


def _survey(section: Section, cwds: set[str] | None, here: str,
            keep: list[str] | None = None,
            stale_days: int = config.SWEEP_STALE_DAYS_DEFAULT,
            stale_remote_days: int = config.SWEEP_STALE_REMOTE_DAYS_DEFAULT) -> None:
    repo = section.repo
    blocks = _blocks(repo)
    primary = blocks[0].get("branch", "").removeprefix("refs/heads/") if blocks else ""
    default, refs = build.default_branch(repo, primary or "(detached HEAD)")
    if not refs:
        raise build.BuildError(f"{default} is not known here")
    open_builds, bad = build.open_builds(repo)
    if bad:
        raise build.BuildError(f"cannot tell which builds are open: {bad[0]}")
    section.builds = list(open_builds)
    claimed = {default: "the default branch"}
    if primary:
        claimed.setdefault(primary, "checked out in the primary checkout")
    for slug, branch in open_builds:
        claimed.setdefault(branch, f"the branch of the build {slug}")

    branches = _branches(repo)
    for name, _, _ in branches:
        if any(fnmatchcase(name, p) for p in keep or ()):
            claimed.setdefault(name, PROTECTED_NOTE)
    upstreams = {name: up for name, _, up in branches}
    judged: dict[str, tuple[str, str, str]] = {}

    def judge(branch: str, tip: str) -> tuple[str, str, str]:
        if branch not in judged:
            judged[branch] = (IN_USE, claimed[branch], "") if branch in claimed \
                else _judge(repo, default, refs[0], branch, tip, stale_days)
        return judged[branch]

    in_worktree = set()
    for fields in blocks[1:]:
        branch = fields.get("branch", "").removeprefix("refs/heads/")
        tip = fields.get("HEAD", "")
        if branch:
            in_worktree.add(branch)
        found = judge(branch, tip) if branch else (REVIEW, "", "")
        verdict, note = _worktree_verdict(fields, branch, found, cwds, here)
        section.items.append(Item("worktree", repo, section.name, branch, tip,
                                  Path(fields["worktree"]), verdict, note,
                                  found[2] if verdict == PUSHED else ""))
    for branch, tip, _ in branches:
        if branch not in in_worktree:
            verdict, note, remote = judge(branch, tip)
            section.items.append(Item("branch", repo, section.name, branch, tip, None,
                                      verdict, note, remote))
    checked_out = ""
    if primary and claimed.get(primary) == "checked out in the primary checkout" and not any(
            fnmatchcase(primary, p) for p in keep or ()):
        tip = phase._ask(repo, "rev-parse", f"refs/heads/{primary}")
        if _judge(repo, default, refs[0], primary, tip, stale_days)[0] in (MERGED, PR_MERGED):
            checked_out = primary
    _remote_items(section, default, build._remote(repo, primary or "(detached HEAD)"), claimed,
                  keep or [], stale_remote_days, checked_out)
    seen = set()
    for it in section.items:
        if it.kind == "remote":
            continue
        if it.branch and it.verdict not in (MERGED, PUSHED) and it.branch not in seen:
            seen.add(it.branch)
            line = _unpushed(repo, it.branch, upstreams.get(it.branch, ""))
            if line:
                section.unpushed.append(line)


# ---------------------------------------------------------------------------- the repositories

def _sections(ctx, project: str | None) -> list[Section]:
    """The repositories to sweep: the one repository of a single project; in a workspace, the
    member `--project` names, else the root and every member that is a repository of its own."""
    def shown(folder: Path) -> str:
        try:
            rel = folder.resolve().relative_to(ctx.root.resolve())
        except ValueError:
            return str(folder)
        return str(rel)

    if ctx.registry.single:
        if project:
            raise build.BuildError("--project applies only in a workspace: this project has no "
                                   "[registry]")
        return [Section(ctx.root.name, ".", ctx.root)]
    if project:
        folder, name = build.target(ctx, project, True)
        return [Section(name or folder.name, shown(folder), folder)]
    found = []
    folders = [(ctx.root.name, ctx.root)] + [
        (s.name, ctx.root / str(s.get("dir"))) for s in ctx.registry.scopes if s.get("dir")]
    for name, folder in folders:
        sec = Section(name, shown(folder), folder)
        try:
            if not build._top(folder):
                continue        # a folder of the root's repository, or not there: no repository
        except build.BuildError as exc:
            sec.error = str(exc)      # git could not say: reported, never skipped
        found.append(sec)
    return found


def _finished(ctx, sections: list[Section]) -> None:
    """Hand each governed project's finished working files to the section whose repository
    holds the project (the deepest one when they nest)."""
    from govern.checks import working
    settings = ctx.cfg.checks.get("finished-files")
    if settings is None or settings.level == "off":
        return
    for scope in ctx.registry.scopes:
        if not scope.governed or scope.gov is None:
            continue
        owners = []
        for sec in sections:
            try:
                scope.gov.resolve().relative_to(sec.repo.resolve())
            except ValueError:
                continue
            owners.append(sec)
        if owners:
            max(owners, key=lambda s: len(s.repo.resolve().parts)).finished.extend(
                working.finished_candidates(ctx, settings.params, scope))


def survey(ctx, project: str | None = None) -> list[Section]:
    """Every repository's section, read. A repository git cannot answer for has its `error` set
    and no items; the rest are still read."""
    sections = _sections(ctx, project)
    cwds = process_cwds()
    here = os.path.realpath(os.getcwd())
    keep = _keep(ctx)
    stale_days = ctx.cfg.get("sweep", "stale_days", config.SWEEP_STALE_DAYS_DEFAULT)
    stale_remote_days = ctx.cfg.get("sweep", "stale_remote_days",
                                    config.SWEEP_STALE_REMOTE_DAYS_DEFAULT)
    for sec in sections:
        if sec.error:
            continue
        try:
            _survey(sec, cwds, here, keep, stale_days, stale_remote_days)
        except (build.BuildError, phase.PhaseError) as exc:
            sec.items, sec.unpushed, sec.builds, sec.error = [], [], [], str(exc)
    _finished(ctx, sections)
    return sections


def collect(ctx, project: str | None = None) -> list[Item]:
    """Every worktree and local branch of every repository swept, with its verdict."""
    return [it for sec in survey(ctx, project) for it in sec.items]


# ---------------------------------------------------------------------------- the report

def _memory(ctx) -> str:
    from govern.checks import memory
    from govern.text import read
    settings = ctx.cfg.checks.get("memory-index")
    path = memory.memory_index_path(ctx)
    if settings is None or not path.is_file():
        return ""
    exempt = list(settings.params["exempt_types"])
    n = 0
    for line in read(path).splitlines():
        if line.strip() and not (exempt and memory.memory_kind(path, line) in exempt):
            n += 1
    return f"memory    {n} of {settings.params['max_lines']} lines  {path}"


def _note(note: str) -> str:
    return f"  ({note})" if note else ""


def render(ctx, sections: list[Section]) -> list[str]:
    lines, counts = [], {}
    for sec in sections:
        lines.append(f"== {sec.name} ({sec.shown}) ==")
        if sec.error:
            lines.append(f"error     {sec.error}")
        for it in sec.items:
            counts[it.verdict] = counts.get(it.verdict, 0) + 1
            if it.path is not None:
                try:
                    where = os.path.relpath(os.path.realpath(it.path),
                                            os.path.realpath(sec.repo))
                except ValueError:
                    where = str(it.path)
                lines.append(f"worktree  {it.verdict}  {it.branch or '(detached)'}  {where}"
                             f"{_note(it.note)}")
            elif it.kind == "remote":
                lines.append(f"remote    {it.verdict}  {it.label()}{_note(it.note)}")
            else:
                lines.append(f"branch    {it.verdict}  {it.branch}{_note(it.note)}")
        lines += sec.unpushed
        lines += [f"build     {slug} in flight on {branch}" for slug, branch in sec.builds]
        for path, status, age in sec.finished:
            lines.append(f"finished  {ctx.rel(path)}  status {status}, last touched {age}d ago")
    memory = _memory(ctx)
    if memory:
        lines.append(memory)
    configured = ctx.cfg.get("sweep", "auto_remove")
    auto = list(dict.fromkeys(config.SWEEP_AUTO_REMOVE_DEFAULT if configured is None
                              else configured))
    source = " (profile)" if "auto_remove" in ctx.cfg.sweep_from_profile else ""
    lines.append(f"auto-remove  {', '.join(auto)}{source}" if auto
                 else "auto-remove  none: ask before every removal")
    tally = ", ".join(f"{v} {counts[v]}" for v in VERDICTS if v in counts) or "none"
    failed = any(sec.error for sec in sections)
    lines.append(f"[{'FAIL' if failed else 'OK'}] sweep  ({tally})")
    return lines


# ---------------------------------------------------------------------------- --apply

def _tip(repo: Path, branch: str) -> str:
    """The tip of a local branch now, or empty when it cannot be read. Tests replace this."""
    res = phase._git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    return phase._out(res) if res is not None and res.returncode == 0 else ""


def _quote(text: str) -> str:
    """Quote one path for the printed restore line, the way this platform's shell reads it."""
    if os.name == "nt":
        return subprocess.list2cmdline([text])
    return shlex.quote(text)


def _why(res:subprocess.CompletedProcess | None) -> str:
    return "git could not run" if res is None else phase._reason(res)


def _bundle_path(repo: Path) -> Path:
    """`sweep.bundle` (`sweep-2.bundle` and on when it exists) under today's folder of the
    repository's shared git directory, so every linked worktree sees the same backups."""
    common = Path(phase._ask(repo, "rev-parse", "--git-common-dir"))
    if not common.is_absolute():
        common = repo / common
    folder = common.resolve() / "context-gate" / "sweep" / date.today().isoformat()
    folder.mkdir(parents=True, exist_ok=True)
    dest, n = folder / "sweep.bundle", 2
    while dest.exists():
        dest, n = folder / f"sweep-{n}.bundle", n + 1
    return dest


def _bundle(repo: Path, refs: list[str]) -> tuple[Path | None, str]:
    """A verified bundle of the refs, or (None, why). A failed one is deleted."""
    try:
        dest = _bundle_path(repo)
    except OSError as err:
        return None, str(err)
    made = phase._git(repo, "bundle", "create", str(dest), *refs, timeout=300)
    why = _why(made)
    if made is not None and made.returncode == 0:
        checked = phase._git(repo, "bundle", "verify", str(dest))
        if checked is not None and checked.returncode == 0:
            return dest, ""
        why = f"the bundle did not verify: {_why(checked)}"
    try:
        dest.unlink(missing_ok=True)
    except OSError:
        pass        # nothing was written there
    return None, why


def _pick(items: list[Item], only: list[str]) -> tuple[list[tuple[str, Item]], list[str]]:
    """The removable item each name picks, and a `skipped` line for each name that picks none."""
    picked, skipped = [], []
    taken: set[int] = set()
    for name in dict.fromkeys(only):
        found = [it for it in items
                 if it.branch and name in (it.label(), f"{it.name}:{it.label()}")]
        if not found:
            skipped.append(f"skipped  {name}  (not found)")
        elif len(found) > 1:
            where = ", ".join(f"{it.name}:{it.label()}" for it in found)
            skipped.append(f"skipped  {name}  (ambiguous: {where}; name one as project:branch)")
        elif found[0].verdict not in REMOVABLE:
            note = f": {found[0].note}" if found[0].note else ""
            skipped.append(f"skipped  {name}  ({found[0].verdict}{note})")
        elif id(found[0]) not in taken:     # a second name for a picked item is ignored
            taken.add(id(found[0]))
            picked.append((name, found[0]))
    return picked, skipped


def _remote_tip(repo: Path, remote: str, branch: str) -> str:
    """The tip `remote` holds for `branch` now, or empty when it cannot be told."""
    res = phase._git(repo, "ls-remote", remote, f"refs/heads/{branch}", timeout=LS_REMOTE_TIMEOUT)
    if res is None or res.returncode != 0:
        return ""
    for line in phase._out(res).splitlines():
        sha, _, ref = line.partition("\t")
        if ref == f"refs/heads/{branch}":
            return sha
    return ""


def _ref(it: Item) -> str:
    return f"refs/remotes/{it.label()}" if it.kind == "remote" else f"refs/heads/{it.branch}"


def _remove(repo: Path, group: list[tuple[str, Item]], lines: list[str]) -> tuple[int, int, bool]:
    """Bundle, then remove, one repository's picked items. Returns (removed, kept, bundled)."""
    removed = kept = 0
    dest, why = _bundle(repo, [_ref(it) for _, it in group])
    if dest is None:
        for name, _ in group:
            lines.append(f"kept      {name}  (bundle failed, nothing removed: {why})")
        return 0, len(group), False
    lines.append(f"bundle    {dest}")
    keep: set[str] = set()
    locked = {os.path.realpath(p) for p in phase._locks(repo)}
    for name, it in group:
        if it.kind != "worktree":
            continue
        if os.path.realpath(it.path) in locked:
            phase._git(repo, "worktree", "unlock", str(it.path))   # the remove reports a failure
        res = phase._git(repo, "worktree", "remove", str(it.path))
        if res is not None and res.returncode == 0:
            lines.append(f"removed   worktree {it.path}")
            removed += 1
        else:
            keep.add(it.branch)
            kept += 1
            lines.append(f"kept      {name}  (git worktree remove refused: {_why(res)})")
    gone = []
    for name, it in group:
        if it.branch in keep or it.kind == "remote":
            continue
        if _tip(repo, it.branch) != it.tip:
            lines.append(f"kept      {name}  (its tip moved since it was judged)")
            kept += 1
            continue
        res = phase._git(repo, "branch", "-D", it.branch)
        if res is not None and res.returncode == 0:
            lines.append(f"removed   branch {it.branch}")
            removed += 1
            gone.append(f"{it.branch}:{it.branch}")
        else:
            lines.append(f"kept      {name}  (git branch -D failed: {_why(res)})")
            kept += 1
    if any(it.kind != "remote" for _, it in group):
        lines.append(f"restore with: git -C {_quote(str(repo))} fetch {_quote(str(dest))} "
                     f"{' '.join(gone) if gone else '<branch>:<branch>'}")
    deleted = []
    for name, it in group:
        if it.kind != "remote":
            continue
        if _remote_tip(repo, it.remote, it.branch) != it.tip:
            lines.append(f"kept      {name}  ({name} moved since it was judged)")
            kept += 1
            continue
        res = phase._git(repo, "push", f"--force-with-lease=refs/heads/{it.branch}:{it.tip}",
                         it.remote, "--delete", it.branch, timeout=LS_REMOTE_TIMEOUT)
        if res is not None and res.returncode != 0 and "stale info" in _why(res):
            lines.append(f"kept      {name}  ({name} moved since it was judged)")
            kept += 1
            continue
        if res is None or res.returncode != 0:
            lines.append(f"kept      {name}  (git push --delete failed: {_why(res)})")
            kept += 1
            continue
        phase._git(repo, "update-ref", "-d", _ref(it), it.tip)
        lines.append(f"removed   remote {name}")
        removed += 1
        deleted.append(it)
    for it in deleted:
        lines.append(f"restore remote: git -C {_quote(str(repo))} fetch {_quote(str(dest))} "
                     f"{_ref(it)}:refs/restored/{it.label()}, then git -C {_quote(str(repo))} "
                     f"push {_quote(it.remote)} refs/restored/{it.label()}:refs/heads/{it.branch}")
    return removed, kept, True


def _fast_forward(repo: Path, remote: str, default: str, lines: list[str]) -> bool:
    """`git merge --ff-only` of the checked-out default branch to its remote's. True on failure."""
    res = phase._git(repo, "merge", "--ff-only", f"{remote}/{default}")
    if res is None or res.returncode != 0:
        lines.append(f"error     git merge --ff-only {remote}/{default} failed: {_why(res)}")
        return True
    return False


def _level(repo: Path, default: str, lines: list[str]) -> bool:
    """`--home` on the default branch already: fast-forward it to its remote when it has one and
    is clean. True when that failed."""
    remote = build._remote(repo, default)
    if not remote or not build._known(repo, f"refs/remotes/{remote}/{default}"):
        return False
    status = phase._git(repo, "status", "--porcelain")
    if status is None or status.returncode != 0 or phase._out(status):
        return False
    return _fast_forward(repo, remote, default, lines)


def _home_repo(ctx, repo: Path, lines: list[str]) -> bool:
    """Bring one repository home: fetch every remote, then, when the checkout sits on a branch
    that can go and the tree is clean, switch to the default branch and fast-forward it. True
    when it must stop (a fetch failed, or the default branch has commits its remote lacks)."""
    remotes = phase._ask(repo, "remote").split()
    for remote in remotes:
        res = phase._git(repo, "fetch", "--prune", remote, timeout=FETCH_TIMEOUT)
        if res is None or res.returncode != 0:
            lines.append(f"error     git fetch --prune {remote} failed, nothing removed: "
                         f"{_why(res)}")
            return True
    head = phase._git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    current = phase._out(head) if head is not None and head.returncode == 0 else ""
    if not current:
        lines.append("home      stays: HEAD is detached")
        return False
    default, refs = build.default_branch(repo, current)
    if current == default:
        return _level(repo, default, lines)
    open_builds, bad = build.open_builds(repo)
    if bad:
        raise build.BuildError(f"cannot tell which builds are open: {bad[0]}")
    if current in {branch for _, branch in open_builds} or any(
            fnmatchcase(current, pat) for pat in _keep(ctx)):
        lines.append(f"home      stays on {current}: it is protected or a build's branch")
        return False
    status = phase._git(repo, "status", "--porcelain")
    if status is None or status.returncode != 0:
        lines.append(f"home      stays on {current}: git status failed")
        return False
    if phase._out(status):
        lines.append(f"home      stays on {current}: the work tree has uncommitted changes")
        return False
    tip = phase._ask(repo, "rev-parse", f"refs/heads/{current}")
    verdict, note, _ = _judge(repo, default, refs[0], current, tip,
                              ctx.cfg.get("sweep", "stale_days", config.SWEEP_STALE_DAYS_DEFAULT))
    if verdict not in REMOVABLE:
        why = f": {note}" if note else ""
        lines.append(f"home      stays on {current}: {verdict}{why}")
        return False
    remote = build._remote(repo, default)
    upstream = f"refs/remotes/{remote}/{default}" if remote else ""
    if upstream and build._known(repo, upstream):
        if build._known(repo, f"refs/heads/{default}") and phase._ask(
                repo, "rev-list", "--count", f"{upstream}..refs/heads/{default}") != "0":
            lines.append(f"error     the default branch has commits {remote} does not: push or "
                         f"reconcile it first")
            return True
    else:
        upstream = ""
    res = phase._git(repo, "switch", default)
    if res is None or res.returncode != 0:
        lines.append(f"error     git switch {default} failed: {_why(res)}")
        return True
    if upstream and _fast_forward(repo, remote, default, lines):
        return True
    lines.append(f"home      on {default}" + (f", level with {remote}/{default}" if upstream else "")
                 + f" (left {current})")
    return False


def apply(ctx, project: str | None, only: list[str], home: bool = False) -> tuple[list[str], int]:
    """The lines `--apply --only` prints and its exit code. Verdicts are worked out afresh; a
    name that is not removable now is skipped, and nothing is ever forced. The exit is
    1 when a repository cannot be read or a bundle failed. With `home`, each repository is
    fetched and brought to its default branch first (see `_home_repo`); a failure there stops
    before anything is removed."""
    home_lines: list[str] = []
    if home:
        for sec in _sections(ctx, project):
            if not sec.error and _home_repo(ctx, sec.repo, home_lines):
                return home_lines, 1
    sections = survey(ctx, project)
    picked, lines = _pick([it for sec in sections for it in sec.items], only)
    lines = home_lines + lines
    removed = kept = 0
    failed = False
    for sec in sections:
        if sec.error:
            lines.append(f"error     {sec.name}: {sec.error}")
            failed = True
    by_repo: dict[Path, list[tuple[str, Item]]] = {}
    for name, it in picked:
        by_repo.setdefault(it.repo, []).append((name, it))
    for repo, group in by_repo.items():
        held = []
        for name, it in group:
            if it.verdict == PUSHED and _remote_tip(repo, it.remote, it.branch) != it.tip:
                lines.append(f"kept      {name}  ({it.remote} no longer holds this tip)")
                kept += 1
            else:
                held.append((name, it))
        if not held:
            continue
        group = held
        try:
            r, k, bundled = _remove(repo, group, lines)
        except phase.PhaseError as exc:
            lines.append(f"error     {exc}")
            failed = True
            continue
        removed, kept, failed = removed + r, kept + k, failed or not bundled
    lines.append(f"[{'FAIL' if failed else 'OK'}] sweep --apply  ({removed} removed, {kept} kept)")
    return lines, 1 if failed else 0


def _print(lines: list[str]) -> None:
    text = "\n".join(lines) + "\n"
    codec = sys.stdout.encoding or "utf-8"
    try:
        text = text.encode(codec, "backslashreplace").decode(codec, "replace")
    except LookupError:
        pass
    sys.stdout.write(text)


def run(ctx, args) -> int:
    """`govern sweep [--project NAME]`: print the report. Exit 0; 1 when git cannot read a
    repository (the rest is still printed) or the command's arguments are refused.
    `--apply --only NAME...` removes the named items that can go; see `apply`. `--home` (with
    `--apply`) fetches and brings the checkout to the default branch first."""
    only = getattr(args, "only", None)
    do_apply = getattr(args, "apply", False)
    if only and not do_apply:
        print("error: --only needs --apply", file=sys.stderr)
        return 1
    home = getattr(args, "home", False)
    if home and not do_apply:
        print("error: --home needs --apply", file=sys.stderr)
        return 1
    if do_apply and not only:
        print("error: --apply needs --only NAME...: name each branch to remove, as `branch` or "
              "`project:branch`", file=sys.stderr)
        return 1
    try:
        if do_apply:
            lines, code = apply(ctx, getattr(args, "project", None), only, home)
            _print(lines)
            return code
        sections = survey(ctx, getattr(args, "project", None))
    except (build.BuildError, phase.PhaseError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    _print(render(ctx, sections))
    return 1 if any(sec.error for sec in sections) else 0
