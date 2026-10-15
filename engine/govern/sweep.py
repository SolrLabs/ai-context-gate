"""`govern sweep`: a read-only report, for the end of a session, of every linked worktree and
local branch of the session root (and, in a workspace, of each member that is a repository of its
own), each with a verdict saying whether it can go, plus finished working files, branches not
pushed, builds in flight and the size of the memory index.

Verdicts:
  MERGED      an ancestor of the default branch's tip
  PR-MERGED   its pull request into the default branch merged at this exact tip (a squash merge
              defeats the ancestry test)
  IN-USE      the default branch, the primary checkout's branch, the branch of a build in flight,
              or a worktree a process works in
  DIRTY       the worktree has uncommitted changes
  REVIEW      anything else a person should decide: an open pull request, a closed one, one merged
              into another base, commits beyond it, none at all, a worktree that is locked,
              detached or gone, or a question git, `gh` or `lsof` could not answer

The report removes nothing. Every git call fails closed: a repository git cannot read is reported,
and the exit code is 1, never an empty answer.

`--apply --only NAME...` removes exactly the named items whose verdict, worked out again, is MERGED
or PR-MERGED: a verified bundle of the branches first, then `git worktree remove` and `git branch
-D`, never forced.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from govern import build, phase

MERGED, PR_MERGED, IN_USE, DIRTY, REVIEW = "MERGED", "PR-MERGED", "IN-USE", "DIRTY", "REVIEW"
VERDICTS = (MERGED, PR_MERGED, IN_USE, DIRTY, REVIEW)
REMOVABLE = (MERGED, PR_MERGED)
GH_TIMEOUT = 30
LSOF_TIMEOUT = 20
GH_FIELDS = "number,state,headRefOid,baseRefName"


@dataclass
class Item:
    kind: str            # "worktree" | "branch"
    repo: Path           # the repository's top
    name: str            # the project's name, or the root's folder name
    branch: str          # "" when detached
    tip: str
    path: Path | None    # the worktree's folder, for kind == "worktree"
    verdict: str
    note: str = ""


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


def _judge(repo: Path, default: str, ref: str, branch: str, tip: str) -> tuple[str, str]:
    """The verdict and note for a branch nothing else claims: MERGED, PR-MERGED or REVIEW."""
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


def _worktree_verdict(fields: dict[str, str], branch: str, judged: tuple[str, str],
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
        return REVIEW, "locked"
    if not branch:
        return REVIEW, "detached HEAD"
    verdict, note = judged
    if verdict in REMOVABLE and cwds is None:
        return REVIEW, "cannot tell whether a process works in it"
    return verdict, note


def _survey(section: Section, cwds: set[str] | None, here: str) -> None:
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
    upstreams = {name: up for name, _, up in branches}
    judged: dict[str, tuple[str, str]] = {}

    def judge(branch: str, tip: str) -> tuple[str, str]:
        if branch not in judged:
            judged[branch] = (IN_USE, claimed[branch]) if branch in claimed \
                else _judge(repo, default, refs[0], branch, tip)
        return judged[branch]

    in_worktree = set()
    for fields in blocks[1:]:
        branch = fields.get("branch", "").removeprefix("refs/heads/")
        tip = fields.get("HEAD", "")
        if branch:
            in_worktree.add(branch)
        verdict, note = _worktree_verdict(fields, branch, judge(branch, tip) if branch
                                          else (REVIEW, ""), cwds, here)
        section.items.append(Item("worktree", repo, section.name, branch, tip,
                                  Path(fields["worktree"]), verdict, note))
    for branch, tip, _ in branches:
        if branch not in in_worktree:
            verdict, note = judge(branch, tip)
            section.items.append(Item("branch", repo, section.name, branch, tip, None,
                                      verdict, note))
    seen = set()
    for it in section.items:
        if it.branch and it.verdict != MERGED and it.branch not in seen:
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
    for sec in sections:
        if sec.error:
            continue
        try:
            _survey(sec, cwds, here)
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
            else:
                lines.append(f"branch    {it.verdict}  {it.branch}{_note(it.note)}")
        lines += sec.unpushed
        lines += [f"build     {slug} in flight on {branch}" for slug, branch in sec.builds]
        for path, status, age in sec.finished:
            lines.append(f"finished  {ctx.rel(path)}  status {status}, last touched {age}d ago")
    memory = _memory(ctx)
    if memory:
        lines.append(memory)
    auto = list(dict.fromkeys(ctx.cfg.get("sweep", "auto_remove", [])))
    lines.append(f"auto-remove  {', '.join(auto)}" if auto
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


def _bundle(repo: Path, branches: list[str]) -> tuple[Path | None, str]:
    """A verified bundle of the branches, or (None, why). A failed one is deleted."""
    try:
        dest = _bundle_path(repo)
    except OSError as err:
        return None, str(err)
    made = phase._git(repo, "bundle", "create", str(dest), *(f"refs/heads/{b}" for b in branches),
                      timeout=300)
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
        found = [it for it in items if it.branch and name in (it.branch, f"{it.name}:{it.branch}")]
        if not found:
            skipped.append(f"skipped  {name}  (not found)")
        elif len(found) > 1:
            where = ", ".join(f"{it.name}:{it.branch}" for it in found)
            skipped.append(f"skipped  {name}  (ambiguous: {where}; name one as project:branch)")
        elif found[0].verdict not in REMOVABLE:
            note = f": {found[0].note}" if found[0].note else ""
            skipped.append(f"skipped  {name}  ({found[0].verdict}{note})")
        elif id(found[0]) not in taken:     # a second name for a picked item is ignored
            taken.add(id(found[0]))
            picked.append((name, found[0]))
    return picked, skipped


def _remove(repo: Path, group: list[tuple[str, Item]], lines: list[str]) -> tuple[int, int, bool]:
    """Bundle, then remove, one repository's picked items. Returns (removed, kept, bundled)."""
    removed = kept = 0
    dest, why = _bundle(repo, [it.branch for _, it in group])
    if dest is None:
        for name, _ in group:
            lines.append(f"kept      {name}  (bundle failed, nothing removed: {why})")
        return 0, len(group), False
    lines.append(f"bundle    {dest}")
    keep: set[str] = set()
    for name, it in group:
        if it.kind != "worktree":
            continue
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
        if it.branch in keep:
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
    lines.append(f"restore with: git -C {_quote(str(repo))} fetch {_quote(str(dest))} "
                 f"{' '.join(gone) if gone else '<branch>:<branch>'}")
    return removed, kept, True


def apply(ctx, project: str | None, only: list[str]) -> tuple[list[str], int]:
    """The lines `--apply --only` prints and its exit code. Verdicts are worked out afresh; a
    name that is not MERGED or PR-MERGED now is skipped, and nothing is ever forced. The exit is
    1 when a repository cannot be read or a bundle failed."""
    sections = survey(ctx, project)
    picked, lines = _pick([it for sec in sections for it in sec.items], only)
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
    `--apply --only NAME...` removes the named items that can go; see `apply`."""
    only = getattr(args, "only", None)
    do_apply = getattr(args, "apply", False)
    if only and not do_apply:
        print("error: --only needs --apply", file=sys.stderr)
        return 1
    if do_apply and not only:
        print("error: --apply needs --only NAME...: name each branch to remove, as `branch` or "
              "`project:branch`", file=sys.stderr)
        return 1
    try:
        if do_apply:
            lines, code = apply(ctx, getattr(args, "project", None), only)
            _print(lines)
            return code
        sections = survey(ctx, getattr(args, "project", None))
    except (build.BuildError, phase.PhaseError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    _print(render(ctx, sections))
    return 1 if any(sec.error for sec in sections) else 0
