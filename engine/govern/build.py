"""`govern build`: the ledger of the one phase a branch has in flight, and the briefs its agents
are launched from.

A phase's state is `.context-gate/build/<phase>/ledger.toml`, written only here and committed on
the phase branch, so a compacted or restarted session can pick the phase up where it stood. The
current phase is the ledger whose `branch` is the checked-out branch; ledgers for other branches
are left alone. Beside the ledger sit the briefs: `<unit>/brief.md`, `review/`, `fix/brief.md`
and `close/brief.md`. Each one ends with its `Produces:` line, so the contract `agent-gates`
holds an agent to never depends on the coordinator typing it.

In a workspace a build works in one member's own repository (`--project`, see `target`): the
ledger names the project, and every command line printed carries it.

`next_action` is a function of the ledger alone: `build status` and the session-start line say
the same sentence. A ledger that cannot be read is an error naming the file, never an empty
phase. Several files are written all or none.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import stat
import sys
import time
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

from govern import agent_runs, layout, phase, profile, roles, tomlw
from govern.decisions import mask_lines

BUILD_DIR = f"{layout.GOV_DIR}/build"
LEDGER = "ledger.toml"
SLUG = re.compile(r"[a-z0-9][a-z0-9-]*")
STAGES = ("build", "review", "fix", "closing")
STATUSES = ("pending", "running", "done", "blocked", "failed")
# The rendered roles that write code: the only ones a unit goes to.
CODE_ROLES = ("developer", "senior-developer")
# The statuses of a unit that is not done: the only ones `unit drop` removes.
DROPPABLE = ("pending", "blocked", "failed")
# More commits than this between the review base and HEAD: `start` warns.
FAR_BEHIND = 50
# A phase's own folders, which no unit may be named after.
RESERVED = ("review", "fix", "close")
# File names the Write tool refuses a subagent: no brief names its report one of these.
REFUSED_NAMES = ("report.md", "findings.md", "summary.md", "analysis.md")
UNIT_KEYS = ("id", "title", "role", "status", "attempts", "branch", "commit", "note")
HEADER = "The build ledger of one phase: written only by `govern build`."
BOM = "﻿"
REPLACE_TRIES = 10
# The roles whose agent works in a worktree of its own, cut from a commit that may be older than
# the phase branch's head: their launch prompt carries `Base:`.
WORKTREE_ROLES = ("developer", "senior-developer", "technical-writer")
PRODUCES_ROLE = re.compile(r"^Produces: (?:.*/)?(.+?)-report(?:-\d+)?\.md\s*$", re.MULTILINE)

# Everything under build/ is ignored except the ledgers and the briefs: reports, diffs and
# failing-test output stay out of history, and the plan of record stays in.
GITIGNORE = """\
# Written once by context-gate. Reports, review packages and failing-test output stay out of
# history; the ledger and the briefs are the plan of record and stay in.
*
!*/
!.gitignore
!ledger.toml
!*brief.md
!*brief-[0-9]*.md
"""


# Said above the brief's last lines: the report is part of the task.
REPORT_NOTE = "Write the report below before your final message, and answer your role's hand-back checklist in it. Every line of it is part of this task."
# Said directly above a brief's `Red:` and `Produces:` lines: a committed brief holds no path of
# this machine, and an agent in a worktree of its own reads them from the root of that tree.
PATHS_NOTE = "Paths on the lines below are from the repository root, in your own working tree."


def _rel(folder: Path, path: Path) -> str:
    """`path` as a brief in `folder` names it: relative to that folder, with forward slashes."""
    return Path(os.path.relpath(path, folder)).as_posix()


def _from_root(ledger: "Ledger", folder: str, name: str) -> str:
    """A contract path as a brief gives it: from the repository root, forward slashes."""
    return f"{BUILD_DIR}/{ledger.dir.name}/{folder}/{name}"


def _fill(what: str) -> str:
    """A skeleton's instruction line: `agent-gates` refuses a launch from a brief that still
    holds one."""
    return f"{agent_runs.FILL} {what} -->"


# The sections of a unit's brief the coordinator fills, each with what belongs there.
UNIT_SECTIONS = (
    ("Context", "what the agent needs to know before the task: what this code is for, and why "
                "the unit exists"),
    ("Task", "what this unit must do, as a ranked list of five items at most"),
    ("Files", "the files this unit creates or changes"),
    ("Tests", "the tests that prove it, each by name, and the command that runs only them; "
                "name no whole-suite run here: the role's own rule and your gate after the "
                "merge cover that"),
    ("Follow", "an existing implementation to follow, read end to end, or \"none\""),
    ("Out of scope", "what this unit must leave alone"),
)
def fix_sections(red: str) -> tuple:
    """The fix brief's sections; `red` is the pass's own red folder (`red-2` for `brief-2.md`)."""
    return (
        ("Fixes", "the findings to fix, ranked, five at most; copy each finding in full (file, "
                  "line, the input that shows it), because no review report is in the fixer's "
                  "worktree; each names its file, the test to write first and see fail "
                  f"(its failing output is saved in {red}), then the change"),
        ("Out of scope", "the findings ruled out or left for later, and anything else this pass "
                         "must leave alone"),
    )


FIX_RUNNING_NOTE = ("A finding made on a running system is proved fixed on a running system, "
                    "never on a copied page or a fixture.")
UX_SECTIONS = (
    ("What to launch and how", "the command that starts an isolated copy, and how to stop it"),
    ("What to look at", "the pages or screens this phase changed, and what a user does on each"),
    ("The design it must match", "the spec, mock-up or earlier screen it is compared with"),
)
CLOSE_SECTION = ("What landed and what was decided", "what landed and what was decided in this "
                                                     "phase, in the coordinator's words")
RULINGS_HEADING = "## Rulings the coordinator made without the owner"
AS_BUILT_NOTE = ("What was built differently from the plan goes in the project's decision log, "
                 "never into the plan.")
DONE_SECTION = ("Definition of done", "the phase's definition of done, as a checklist of "
                                      "things a command can show, in the order to check them")


class BuildError(Exception):
    """Something that stops the command before it changes anything."""


class NoBuild(BuildError):
    """A workspace with no build in flight in any of its repositories: `status` says so and
    succeeds, as it does in one repository; every other command refuses."""


@dataclass
class Ledger:
    path: Path                         # the ledger file; its folder holds the phase's briefs
    phase: str
    branch: str
    base: str                          # the commit the phase branch left its source at, at start
    plan: str = ""                     # relative to the project's root; "" when there is none
    high_risk: bool = False
    visual: bool = False
    stage: str = "build"
    rulings: list[str] = field(default_factory=list)
    launched: list[str] = field(default_factory=list)   # the fix briefs `launch fix` has printed
    units: list[dict] = field(default_factory=list)
    origin: str = ""                   # `from`: the branch it was cut from; "" is the default
    crlf: bool = False                 # how the file on disk ends its lines, kept on a rewrite
    bom: bool = False
    project: str = ""                  # the workspace member this repository is; "" outside one
    folder: str = field(default="", compare=False)   # its folder from the workspace root; not stored

    @property
    def dir(self) -> Path:
        return self.path.parent

    def unit(self, uid: str) -> dict | None:
        return next((u for u in self.units if u["id"] == uid), None)

    def brief(self, uid: str) -> Path:
        return self.dir / uid / "brief.md"

    def report(self, unit: dict) -> Path:
        return self.dir / unit["id"] / f"{unit['role']}-report.md"


# ---------------------------------------------------------------------------- the ledger file

def build_dir(root: Path) -> Path:
    return root / BUILD_DIR


def render(ledger: Ledger) -> str:
    """The ledger as TOML: the phase's own keys, then one `[[unit]]` table per unit."""
    head = {"phase": ledger.phase, "branch": ledger.branch, "base": ledger.base}
    if ledger.project:
        head["project"] = ledger.project
    if ledger.origin:
        head["from"] = ledger.origin
    if ledger.plan:
        head["plan"] = ledger.plan
    head.update(high_risk=ledger.high_risk, visual=ledger.visual, stage=ledger.stage, rulings=list(ledger.rulings))
    if ledger.launched:
        head["launched"] = list(ledger.launched)
    text = tomlw.dumps(head, HEADER)
    for unit in ledger.units:
        text += "\n[[unit]]\n" + tomlw.dumps({k: unit[k] for k in UNIT_KEYS})
    return text


def _shape(data: dict, folder: str) -> str | None:
    """Why this is not a ledger, or None when it is one."""
    for key in ("phase", "branch", "base"):
        if not isinstance(data.get(key), str) or not data[key]:
            return f"`{key}` is missing or is not text"
    if data["phase"] != folder:
        return f"`phase` is {data['phase']!r}, in a folder named {folder!r}"
    if not isinstance(data.get("plan", ""), str):
        return "`plan` is not text"
    if not isinstance(data.get("from", ""), str):
        return "`from` is not text"
    if not isinstance(data.get("project", ""), str):
        return "`project` is not text"
    if not isinstance(data.get("high_risk", False), bool):
        return "`high_risk` is not true or false"
    if not isinstance(data.get("visual", False), bool):
        return "`visual` is not true or false"
    if data.get("stage") not in STAGES:
        return f"`stage` is {data.get('stage')!r}, not one of {', '.join(STAGES)}"
    rulings = data.get("rulings", [])
    if not isinstance(rulings, list) or not all(isinstance(r, str) for r in rulings):
        return "`rulings` is not a list of text lines"
    launched = data.get("launched", [])
    if not isinstance(launched, list) or not all(isinstance(n, str) for n in launched):
        return "`launched` is not a list of text lines"
    units = data.get("unit", [])
    if not isinstance(units, list) or not all(isinstance(u, dict) for u in units):
        return "`unit` is not a list of tables"
    seen = set()
    for unit in units:
        uid = unit.get("id")
        if not isinstance(uid, str) or not SLUG.fullmatch(uid) or uid in RESERVED:
            return f"a unit's `id` is {uid!r}"
        if uid in seen:
            return f"unit `{uid}` is listed twice"
        seen.add(uid)
        if unit.get("status") not in STATUSES:
            return (f"unit `{uid}` has `status` {unit.get('status')!r}, not one of "
                    f"{', '.join(STATUSES)}")
        attempts = unit.get("attempts", 0)
        if isinstance(attempts, bool) or not isinstance(attempts, int):
            return f"unit `{uid}` has an `attempts` that is not a number"
        for key in ("title", "role", "branch", "commit", "note"):
            if not isinstance(unit.get(key, ""), str):
                return f"unit `{uid}` has a `{key}` that is not text"
    return None


def load(path: Path) -> Ledger:
    """The ledger at `path`. Raises `BuildError` naming the file when it cannot be read, is not
    TOML or is not a ledger: it is never read as an empty phase."""
    def refuse(why: str) -> BuildError:
        return BuildError(f"{path}: {why} — fix the file or restore it from git; nothing was "
                          f"changed")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise refuse(f"unreadable ({exc.strerror or exc})") from None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise refuse(f"not valid UTF-8 (byte {exc.start})") from None
    try:
        data = tomllib.loads(text.removeprefix(BOM))
    except tomllib.TOMLDecodeError as exc:
        raise refuse(f"not valid TOML ({exc})") from None
    why = _shape(data, path.parent.name)
    if why:
        raise refuse(f"not a build ledger: {why}")
    units = [{"id": u["id"], "title": u.get("title", ""), "role": u.get("role", CODE_ROLES[0]),
              "status": u["status"], "attempts": u.get("attempts", 0),
              "branch": u.get("branch", ""), "commit": u.get("commit", ""),
              "note": u.get("note", "")} for u in data.get("unit", [])]
    return Ledger(path=path, phase=data["phase"], branch=data["branch"], base=data["base"],
                  plan=data.get("plan", ""), origin=data.get("from", ""), high_risk=data.get("high_risk", False),
                  visual=data.get("visual", False),
                  stage=data["stage"], rulings=list(data.get("rulings", [])),
                  launched=list(data.get("launched", [])), units=units,
                  crlf="\r\n" in text, bom=text.startswith(BOM), project=data.get("project", ""))


def encoded(ledger: Ledger) -> bytes:
    """The ledger's bytes, with the line endings and the byte order mark its file already has."""
    text = render(ledger)
    if ledger.crlf:
        text = text.replace("\n", "\r\n")
    return ((BOM if ledger.bom else "") + text).encode("utf-8")


def _replace(tmp: Path, path: Path) -> None:
    """Move `tmp` over `path`. Windows refuses a replace while another process holds the target
    open for an instant, and refuses one onto a read-only file: a few tries, and a read-only
    target is made writable first."""
    for attempt in range(REPLACE_TRIES):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == REPLACE_TRIES - 1:
                raise
            try:
                if path.exists() and not os.access(path, os.W_OK):
                    os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            except OSError:
                pass
            time.sleep(0.01 * (attempt + 1))


def write_all(files: list[tuple[Path, bytes]]) -> None:
    """Write every file, or none of them: each is staged beside its target and moved into
    place, and a failure part-way puts back what was already replaced. Raises `OSError`."""
    staged: list[tuple[Path, Path]] = []
    placed: list[tuple[Path, bytes | None]] = []
    try:
        for path, data in files:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp")
            staged.append((tmp, path))
            tmp.write_bytes(data)
        for tmp, path in staged:
            old = path.read_bytes() if path.exists() else None
            _replace(tmp, path)
            placed.append((path, old))
    except OSError:
        for path, old in reversed(placed):
            try:
                path.unlink(missing_ok=True) if old is None else path.write_bytes(old)
            except OSError:
                pass
        for tmp, _ in staged:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def save(ledger: Ledger, beside: list[tuple[Path, bytes]] = ()) -> None:
    """Write the ledger, and with it the files that go with this change, all or none."""
    write_all([*beside, (ledger.path, encoded(ledger))])


def ledgers(root: Path) -> tuple[list[Ledger], list[BuildError]]:
    """Every ledger under the build folder that reads, and why each other one does not."""
    good, bad = [], []
    for path in sorted(build_dir(root).glob(f"*/{LEDGER}")):
        try:
            good.append(load(path))
        except BuildError as exc:
            bad.append(exc)
    return good, bad


def open_builds(path: Path) -> tuple[list[tuple[str, str]], list[BuildError]]:
    """`(phase, branch)` for every tracked ledger anywhere in the repository `path` is in, and the
    error for each that does not read. When git cannot name the top (no repository, or git cannot
    run) it is `([], [])` and the caller's own check refuses. A git call that fails after that is
    an error, never an empty answer."""
    top = phase._git(path, "rev-parse", "--show-toplevel")
    if top is None or top.returncode != 0:
        return [], []     # no repository, or git cannot run: `phase.plan` refuses on its own check
    root = Path(phase._out(top))
    spec = f":(glob)**/{BUILD_DIR}/*/{LEDGER}"
    res = phase._git(root, "ls-files", "-z", "--", spec)
    if res is None or res.returncode != 0:
        return [], [BuildError("git could not list the ledgers")]
    good, bad = [], []
    for name in res.stdout.decode("utf-8", errors="replace").split("\0"):
        file = root / name
        if not name or not file.is_file():
            continue
        try:
            ledger = load(file)
        except BuildError as exc:
            bad.append(exc)
        else:
            good.append((ledger.phase, ledger.branch))
    return good, bad


def current(root: Path, branch: str) -> Ledger | None:
    """The ledger whose `branch` is `branch`, or None when no build is in flight on it. Raises
    `BuildError` when that cannot be told: two ledgers name the branch, or none does and one
    cannot be read (it may be this branch's)."""
    good, bad = ledgers(root)
    mine = [g for g in good if g.branch == branch]
    if len(mine) > 1:
        raise BuildError(f"{len(mine)} ledgers name the branch {branch}: "
                         + ", ".join(str(g.path) for g in mine)
                         + " — one phase is in flight per branch; remove the one that is not it")
    if mine:
        return mine[0]
    if bad:
        raise bad[0]
    return None


# ---------------------------------------------------------------------------- git

def checked_out(root: Path) -> str:
    """The checked-out branch, or "" on a detached HEAD. Raises when git cannot say."""
    if not phase._test(root, "rev-parse", "--git-dir"):
        raise BuildError("not inside a git repository")
    res = phase._run(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    return phase._out(res) if res.returncode == 0 else ""


def _known(root: Path, ref: str) -> bool:
    return phase._test(root, "rev-parse", "--verify", "--quiet", ref + "^{commit}")


def _remote(root: Path, branch: str) -> str | None:
    """The remote whose default branch this repository answers to: the branch's upstream's,
    else the only one, else `origin`. None when the repository has no remote."""
    remotes = phase._ask(root, "remote").split()
    if not remotes:
        return None
    mine = phase._ask(root, "for-each-ref", "--format=%(upstream:remotename)",
                      f"refs/heads/{branch}").splitlines()
    if mine and mine[0] in remotes:
        return mine[0]
    if len(remotes) == 1:
        return remotes[0]
    if "origin" in remotes:
        return "origin"
    raise BuildError("several remotes, none of them origin, and no upstream: set this branch's "
                     "upstream so the default branch can be told (" + ", ".join(remotes) + ")")


def default_branch(root: Path, branch: str) -> tuple[str, list[str]]:
    """The default branch's name as a person says it, and the refs that hold it here: the
    remote's default branch, or `main` or `master` in a repository with no remote."""
    remote = _remote(root, branch)
    if remote is None:
        if branch in ("main", "master"):
            return branch, [f"refs/heads/{branch}"]
        local = [n for n in ("main", "master") if _known(root, f"refs/heads/{n}")]
        if not local:
            raise BuildError("cannot tell which branch is the default: this repository has no "
                             "remote, and no branch named main or master")
        return local[0], [f"refs/heads/{local[0]}"]
    name = phase._default_branch(root, remote)
    refs = [r for r in (f"refs/remotes/{remote}/{name}", f"refs/heads/{name}") if _known(root, r)]
    if not refs and name != branch:
        raise BuildError(f"{remote}/{name} is not known here: run git fetch {remote}")
    return name, refs


def source_refs(root: Path, branch: str, name: str) -> list[str]:
    """The refs that hold the branch `name` here: the remote's copy and the local one."""
    remote = _remote(root, branch)
    refs = [r for r in ((f"refs/remotes/{remote}/{name}",) if remote else ())
            + (f"refs/heads/{name}",) if _known(root, r)]
    if not refs:
        raise BuildError(f"{name} is not a branch here: --from names the branch the phase was "
                         f"cut from")
    return refs


def review_base(root: Path, ledger: Ledger) -> str:
    """Where HEAD leaves the branch the phase was cut from, now: merging that branch into the
    phase, or cutting the next phase from this one, moves it, and the commits it passes are
    not this phase's."""
    if ledger.origin:
        return merge_base(root, source_refs(root, ledger.branch, ledger.origin), ledger.origin)
    name, refs = default_branch(root, ledger.branch)
    return merge_base(root, refs, name)


def merge_base(root: Path, refs: list[str], default: str) -> str:
    """Where HEAD left the default branch. With the remote's copy and a local one both here,
    the later of the two commits: a local default branch ahead of its remote, or behind it,
    must not pull commits of its own into the phase."""
    bases = []
    for ref in refs:
        res = phase._run(root, "merge-base", "HEAD", ref)
        if res.returncode == 0 and phase._out(res):
            bases.append(phase._out(res).splitlines()[0])
    if not bases:
        raise BuildError(f"this branch shares no commit with {default}: there is no base to "
                         f"measure the phase from")
    if len(bases) > 1 and bases[0] != bases[1] \
            and phase._test(root, "merge-base", "--is-ancestor", bases[0], bases[1]):
        return bases[1]
    return bases[0]


def _package_parts(root: Path, ledger: Ledger, diff: bool) -> bytes:
    """The package's heading, commit list and stat, and with `diff` the full diff too, each
    one plain git call, with the build folder itself left out of all of them."""
    prefix = phase._ask(root, "rev-parse", "--show-prefix")
    spec = ["--", ":(top)", f":(top,exclude,literal){prefix}{BUILD_DIR}"]
    base = review_base(root, ledger)
    span = f"{base}..HEAD"
    steps = [("Commits", ["log", "--oneline", "--no-decorate", "--no-color", span]),
             ("Stat", ["diff", "--stat", "--no-color", "--no-ext-diff", span])]
    if diff:
        steps.append(("Diff", ["diff", "-U10", "--no-color", "--no-ext-diff", span]))
    parts = []
    for title, args in steps:
        res = phase._run(root, *args, *spec)
        if res.returncode != 0:
            raise BuildError(f"git {args[0]} {span} failed: {phase._reason(res)} — no review "
                             f"package was written")
        parts.append(f"## {title}\n\n".encode("utf-8") + res.stdout.rstrip(b"\n") + b"\n")
    head = phase._ask(root, "rev-parse", "HEAD")
    top = (f"# Review package: {ledger.phase} on {ledger.branch}, "
           f"{base[:12]}..{head[:12]}\n\n")
    return top.encode("utf-8") + b"\n".join(parts)


def package(root: Path, ledger: Ledger) -> bytes:
    """The review package: the commit list, the stat and the full diff from the base to HEAD."""
    return _package_parts(root, ledger, True)


def package_stat(root: Path, ledger: Ledger) -> bytes:
    """The review package up to and including the stat, with no diff."""
    return _package_parts(root, ledger, False)


# ---------------------------------------------------------------------------- briefs

def launch_line(brief: Path | str) -> str:
    """The one line an agent is launched with. A path holding a space is quoted, with a mark
    the path itself does not hold."""
    text = str(brief)
    if " " not in text:
        return f"Brief: {text}"
    mark = next((m for m in ('"', "'", "`") if m not in text), '"')
    return f"Brief: {mark}{text}{mark}"


def _sections(sections) -> str:
    return "".join(f"## {name}\n\n{_fill(what)}\n\n" for name, what in sections)


def unit_brief(ledger: Ledger, unit: dict, red: bool) -> str:
    return (f"# {ledger.phase} / {unit['id']} — {unit['title']}\n\n"
            + _sections(UNIT_SECTIONS)
            + f"{REPORT_NOTE}\n{PATHS_NOTE}\n"
            + (f"Red: {_from_root(ledger, unit['id'], 'red')}\n" if red else "")
            + f"Produces: {_from_root(ledger, unit['id'], ledger.report(unit).name)}\n")


def _material(root: Path, ledger: Ledger, folder: Path, diff: bool = True) -> str:
    """What a reviewer of the phase is handed: the plan, each unit's brief and report, and the
    review package (with `diff` False, its commit list and stat only), each as a path from
    `folder`, the one the brief is in."""
    lines = [f"- Plan: {_rel(folder, root / ledger.plan)}"] if ledger.plan else []
    lines += [f"- Unit {u['id']}, {u['title']}: brief {_rel(folder, ledger.brief(u['id']))}, "
              f"report {_rel(folder, ledger.report(u))}" for u in ledger.units]
    if diff:
        lines.append(f"- Review package (commit list, stat, full diff from the phase's base): "
                     f"{_rel(folder, ledger.dir / 'review' / 'package.diff')}")
    else:
        lines.append(f"- What changed (commit list and stat; open the files you probe, there "
                     f"is no diff to read): "
                     f"{_rel(folder, ledger.dir / 'review' / 'package-stat.txt')}")
    return "\n".join(lines) + "\n\n"


def code_reviewer_brief(root: Path, ledger: Ledger) -> str:
    return (f"# {ledger.phase} / review — code-reviewer\n\n"
            f"Review the phase `{ledger.phase}` as one diff; the paths below are relative to "
            f"the folder this brief is in.\n\n"
            + _material(root, ledger, ledger.dir / "review")
            + f"{REPORT_NOTE}\n{PATHS_NOTE}\nProduces: {_from_root(ledger, 'review', 'code-reviewer-report.md')}\n")


def qa_engineer_brief(root: Path, ledger: Ledger) -> str:
    review = ledger.dir / "review"
    return (f"# {ledger.phase} / review — qa-engineer\n\n"
            f"Verify the phase `{ledger.phase}` against its definition of done.\n\n"
            + _sections((DONE_SECTION,))
            + _material(root, ledger, review, diff=False)
            + f"Scratch files go only in {_rel(review, review / 'scratch')}.\n\n"
            + f"{REPORT_NOTE}\n{PATHS_NOTE}\nProduces: {_from_root(ledger, 'review', 'qa-engineer-report.md')}\n")


def ux_reviewer_brief(root: Path, ledger: Ledger) -> str:
    review = ledger.dir / "review"
    return (f"# {ledger.phase} / review — ux-reviewer\n\n"
            f"Launch the product as the phase `{ledger.phase}` leaves it and look at what it "
            f"changed; the paths below are relative to the folder this brief is in.\n\n"
            + _material(root, ledger, review)
            + _sections(UX_SECTIONS)
            + f"{REPORT_NOTE}\n{PATHS_NOTE}\nProduces: {_from_root(ledger, 'review', 'ux-reviewer-report.md')}\n")


def fix_brief(ledger: Ledger, head: str, role: str, number: int) -> str:
    """The fix pass's brief; the `number`th pass names its report `<role>-report-<number>.md`
    (the first has no number), so no pass overwrites another's."""
    suffix = f"-{number}" if number > 1 else ""
    return (f"# {ledger.phase} / fix — the fix pass{f' {number}' if number > 1 else ''}\n\n"
            f"Base commit: {head}\n\n"
            + _sections(fix_sections(_from_root(ledger, "fix", f"red{suffix}")))
            + f"{FIX_RUNNING_NOTE}\n\n{REPORT_NOTE}\n{PATHS_NOTE}\n"
            f"Red: {_from_root(ledger, 'fix', f'red{suffix}')}\n"
            f"Produces: {_from_root(ledger, 'fix', f'{role}-report{suffix}.md')}\n")


def _with_rulings(text: str, ledger: Ledger, markers) -> str:
    """The close brief with its rulings section brought up to date; every other line is kept.
    A heading inside a code fence or an HTML comment is text, never the section."""
    block = [RULINGS_HEADING, ""] + [f"- {r}" for r in ledger.rulings] + [""]
    lines = text.splitlines(keepends=True)
    eol = "\r\n" if "\r\n" in text else "\n"
    block = [line + eol for line in block]
    live = mask_lines([line.rstrip("\r\n") for line in lines], markers)
    starts = [i for i, line in enumerate(lines) if live[i] and line.rstrip("\r\n") == RULINGS_HEADING]
    if starts:
        end = next((i for i in range(starts[0] + 1, len(lines))
                    if live[i] and lines[i].startswith("## ")), len(lines))
        lines[starts[0]:end] = block
    elif ledger.rulings:
        heading = f"## {CLOSE_SECTION[0]}"
        at_ = next((i for i, line in enumerate(lines)
                    if live[i] and line.rstrip("\r\n") == heading), None)
        if at_ is None:
            return text
        lines[at_:at_] = block
    return "".join(lines)


def close_brief(root: Path, ledger: Ledger) -> str:
    landed = "".join(
        f"- Unit {u['id']}, {u['title']}"
        + (f" (commit {u['commit']})" if u["commit"] else "")
        + "\n" for u in ledger.units)
    rulings = "".join(f"- {r}\n" for r in ledger.rulings)
    return (f"# {ledger.phase} / close — the phase is closing\n\n"
            f"The phase `{ledger.phase}` on the branch `{ledger.branch}` is closing.\n\n"
            + (f"Plan: {_rel(ledger.dir / 'close', root / ledger.plan)}\n\n"
               if ledger.plan else "")
            + ("## What landed\n\n" + landed + "\n" if landed else "")
            + (RULINGS_HEADING + "\n\n" + rulings + "\n" if rulings else "")
            + _sections((CLOSE_SECTION,))
            + f"{AS_BUILT_NOTE}\n\n{REPORT_NOTE}\n{PATHS_NOTE}\nProduces: "
            f"{_from_root(ledger, 'close', 'technical-writer-report.md')}\n")


# ---------------------------------------------------------------------------- status

def _names(units: list[dict]) -> str:
    ids = [u["id"] for u in units]
    return ids[0] if len(ids) == 1 else ", ".join(ids[:-1]) + " and " + ids[-1]


def govern_build(project: str | None) -> str:
    """The command that starts every `build` line the coordinator is told to run."""
    return f"{roles.GOVERN} build" + (f" --project {shlex.quote(project)}" if project else "")


def phase_close(project: str | None, folder: str) -> str:
    return f"{roles.GOVERN} phase close" + (f" --path {shlex.quote(folder)}" if project else "")


def next_action(ledger: Ledger) -> str:
    """One sentence: what the coordinator does next, and the command for it. A function of the
    ledger alone."""
    govern = govern_build(ledger.project)
    stuck = next((u for u in ledger.units if u["status"] in ("blocked", "failed")), None)
    if stuck:
        note = f" ({stuck['note']})" if stuck["note"] else " (no note)"
        return (f"Unit {stuck['id']} is {stuck['status']}{note}: settle what stopped it, then "
                f"launch a fresh {stuck['role']} with `{govern} launch {stuck['id']}`.")
    running = [u for u in ledger.units if u["status"] == "running"]
    awaited = (f"{'unit' if len(running) == 1 else 'units'} {_names(running)} "
               f"{'is' if len(running) == 1 else 'are'} running and awaited") if running else ""
    todo = next((u for u in ledger.units if u["status"] == "pending"), None)
    if todo:
        return (f"Fill in the brief of unit {todo['id']}, commit it, then run "
                f"`{govern} launch {todo['id']}`"
                + (f"; {awaited}." if running else "."))
    if running:
        return (f"{awaited[0].upper()}{awaited[1:]}: when one lands, merge it and run "
                f"`{govern} unit set <id> done --commit <hash>`.")
    if not ledger.units:
        return f"Add the first unit with `{govern} unit add <id> --title <title>`."
    if ledger.stage == "build":
        return f"Every unit is done: run `{govern} review`."
    if ledger.stage == "review":
        return (f"When the reviews are in, run `{govern} fix`, or `{govern} close` "
                f"if they found nothing.")
    if ledger.stage == "fix":
        return f"When the fix pass is merged and the gate is green, run `{govern} close`."
    return (f"When technical-writer's branch is merged, remove the phase folder as `{govern} "
            f"close` printed, then run `{phase_close(ledger.project, ledger.folder)}`.")


def _posix(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _local(root: Path, path: Path) -> str:
    """A path as a person on this platform reads it, relative to the project's root."""
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def status_data(root: Path, ledger: Ledger | None, branch: str) -> dict:
    """What `build status --json` prints. The keys are a contract; with no build in flight
    they are all there, `phase` is null and the lists are empty."""
    if ledger is None:
        return {"phase": None, "branch": branch, "base": None, "plan": None, "high_risk": False, "visual": False,
                "stage": None, "ledger": None, "rulings": [], "units": [], "done": 0,
                "total": 0, "next": nothing_in_flight(branch)}
    units = [{**{k: u[k] for k in UNIT_KEYS}, "brief": _posix(root, ledger.brief(u["id"])),
              "report": _posix(root, ledger.report(u))} for u in ledger.units]
    return {"phase": ledger.phase, "branch": ledger.branch, "base": ledger.base,
            "plan": ledger.plan or None, "high_risk": ledger.high_risk, "visual": ledger.visual, "stage": ledger.stage,
            "ledger": _posix(root, ledger.path), "rulings": list(ledger.rulings), "units": units,
            "done": sum(1 for u in ledger.units if u["status"] == "done"),
            "total": len(ledger.units), "next": next_action(ledger)}


def nothing_in_flight(branch: str, project: str | None = None) -> str:
    where = f"on {branch}" if branch else "on a detached HEAD"
    return (f"No build is in flight {where}: start one from a phase branch with "
            f"`{govern_build(project)} start <phase>`.")


def _rulings(ledger: Ledger) -> list[str]:
    if not ledger.rulings:
        return []
    return ["Rulings:"] + [f"  {n}. {r}" for n, r in enumerate(ledger.rulings, 1)]


def status_lines(ledger: Ledger) -> list[str]:
    facts = [f"stage {ledger.stage}", f"base {ledger.base[:12]}"]
    if ledger.plan:
        facts.append(f"plan {ledger.plan}")
    if ledger.high_risk:
        facts.append("high-risk")
    if ledger.visual:
        facts.append("visual")
    lines = [f"Phase {ledger.phase} on {ledger.branch}: " + ", ".join(facts)]
    wide = {k: max([len(u[k]) for u in ledger.units], default=0) for k in ("id", "status", "role")}
    for u in ledger.units:
        more = [f"attempts {u['attempts']}"] if u["attempts"] else []
        more += [f"{k} {u[k]}" for k in ("branch", "commit") if u[k]]
        more += [f"note: {u['note']}"] if u["note"] else []
        lines.append(f"  {u['id']:<{wide['id']}}  {u['status']:<{wide['status']}}  "
                     f"{u['role']:<{wide['role']}}  {u['title']}"
                     + (f"  ({'; '.join(more)})" if more else ""))
    if not ledger.units:
        lines.append("  no units yet")
    return lines + _rulings(ledger) + [f"Next: {next_action(ledger)}"]


def session_line(root: Path) -> str | None:
    """The line a session starts with when the checked-out branch has a build in flight, or
    None. Raises when git or a ledger cannot say."""
    if not build_dir(root).is_dir():
        return None
    branch = checked_out(root)
    ledger = current(root, branch) if branch else None
    if ledger is None:
        return None
    done = sum(1 for u in ledger.units if u["status"] == "done")
    return (f"A build is in flight: {ledger.phase}, {done} of {len(ledger.units)} units done. "
            f"Next: {next_action(ledger)}")


# ---------------------------------------------------------------------------- commands

def _top(folder: Path) -> bool:
    """Whether `folder` is the top of a git repository of its own. Git saying the folder is in no
    repository is False; git not being able to answer (missing, timed out, `safe.directory`, a
    lock) raises `BuildError`, never False."""
    if not folder.is_dir():
        return False
    res = phase._git(folder, "rev-parse", "--show-toplevel")
    if res is None:
        raise BuildError(f"cannot tell whether {folder} is a repository: git could not run")
    if res.returncode != 0:
        if "not a git repository" in res.stderr.decode("utf-8", errors="replace").lower():
            return False
        raise BuildError(f"cannot tell whether {folder} is a repository: {phase._reason(res)}")
    return Path(phase._out(res)).resolve() == folder.resolve()


def target(ctx, project: str | None, starting: bool) -> tuple[Path, str | None]:
    """The repository a build command works in, and the project's name: the governance root
    itself in one repository; in a workspace the member `--project` names, or, for a command
    that is not `start`, the one repository that has a build in flight. Raises `BuildError`
    (`NoBuild` when no repository has one)."""
    if ctx.registry.single:
        if project:
            raise BuildError("--project applies only in a workspace: this project has no "
                             "[registry]")
        return ctx.root, None
    members = [(s.name, ctx.root / str(s.get("dir")))
               for s in ctx.registry.scopes if s.get("dir")]
    names = ", ".join(n for n, _ in members)
    if project:
        scope = ctx.registry.find(project)
        if scope is None or not scope.get("dir"):
            raise BuildError(f"no project '{project}' in {ctx.registry.path.name}: one of {names}")
        folder = ctx.root / str(scope.get("dir"))
        if not _top(folder):
            raise BuildError(f"project '{project}' is a folder of this repository, not a "
                             f"repository of its own: build from the root without --project")
        return folder, project
    if starting:
        if not any(_top(folder) for _, folder in members):
            return ctx.root, None
        raise BuildError(f"in a workspace, name the project to build in: --project <one of: "
                         f"{names}>")
    found = []
    for name, folder in [*members, (None, ctx.root)]:
        if not _top(folder) or any(folder.resolve() == f.resolve() for _, f, _ in found):
            continue
        branch = checked_out(folder)
        ledger = current(folder, branch) if branch else None
        if ledger is not None:
            found.append((name, folder, ledger))
    if len(found) > 1:
        raise BuildError("builds are in flight in several repositories: "
                         + ", ".join(f"{n or 'the workspace root'} (phase {g.phase})"
                                     for n, _, g in found)
                         + " - name one with --project")
    if not found:
        raise NoBuild(f"No build is in flight in this workspace: start one from a phase branch "
                      f"of a project with `{roles.GOVERN} build --project <one of: {names}> "
                      f"start <phase>`.")
    return found[0][1], found[0][0]

def _say(lines: list[str]) -> None:
    """Print lines that hold a project's own words (a title, a note, a path) on a console that
    may not be able to encode them: what it cannot encode is escaped, never a traceback."""
    text = "\n".join(lines) + "\n"
    codec = sys.stdout.encoding or "utf-8"
    try:
        text = text.encode(codec, "backslashreplace").decode(codec, "replace")
    except LookupError:
        pass
    sys.stdout.write(text)


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


def _folder(ctx, project: str | None) -> str:
    """The project's folder from the workspace root, as the registry gives it; "" with no project."""
    scope = ctx.registry.find(project) if project else None
    return str(scope.get("dir")) if scope is not None and scope.get("dir") else ""


def _need(ctx, project: str | None = None) -> Ledger:
    """The checked-out branch's ledger, for a command that has nothing to do without one."""
    branch = checked_out(ctx.root)
    ledger = current(ctx.root, branch) if branch else None
    if ledger is None:
        said = nothing_in_flight(branch, project)
        raise BuildError(said[0].lower() + said[1:])
    ledger.project = ledger.project or project or ""
    ledger.folder = _folder(ctx, ledger.project)
    return ledger


def _then(what: str, brief: str = "the brief", project: str | None = None) -> str:
    """The last thing a command that writes a brief says: the brief is the coordinator's to fill,
    and `build launch` is what checks it and prints the prompt."""
    return f"fill in {brief}, commit it, then run `{govern_build(project)} launch {what}`"


def _commits_since(root: Path, base: str) -> int:
    """How many commits HEAD has past `base`; 0 when git cannot say."""
    try:
        res = phase._run(root, "rev-list", "--count", f"{base}..HEAD")
    except phase.PhaseError:
        return 0
    text = phase._out(res)
    return int(text) if res.returncode == 0 and text.isdigit() else 0


def start(ctx, name: str, plan: str | None, high_risk: bool, source: str | None = None,
          visual: bool = False, project: str | None = None, session: Path | None = None) -> int:
    """`session` is the governance root, where runs are logged; `ctx.root` is the member's."""
    root = ctx.root
    if not SLUG.fullmatch(name):
        raise BuildError(f"{name!r} is not a phase name: lower-case letters, digits and "
                         f"hyphens, starting with a letter or a digit")
    if visual:
        _need_ux_reviewer(ctx)
    branch = checked_out(root)
    if not branch:
        raise BuildError("HEAD is detached: check out the phase branch first")
    default, refs = default_branch(root, branch)
    if source:
        refs = source_refs(root, branch, source)
    if branch == default:
        raise BuildError(f"{branch} is the default branch: a phase is built on a branch of its "
                         f"own; cut one first, e.g. git checkout -b phase/{name}")
    if phase._ask(root, "status", "--porcelain", "--untracked-files=no"):
        raise BuildError("uncommitted changes: commit or set them aside first")
    mine = current(root, branch)
    # The same phase with no unit yet may be started again, to correct its base or origin.
    again = mine is not None and mine.phase == name and not mine.units
    if mine is not None and not again:
        raise BuildError(f"a build is already in flight on {branch}: {mine.phase} "
                         f"({_local(root, mine.path)})")
    path = build_dir(root) / name / LEDGER
    if path.exists() and not again:
        raise BuildError(f"the phase {name} already has a ledger, for the branch "
                         f"{load(path).branch} ({_local(root, path)}): pick another name")
    rel = ""
    if plan:
        file = root / plan
        if not file.is_file():
            raise BuildError(f"--plan {plan}: no such file (a relative path is read from the "
                             f"project's root)")
        rel = _posix(root, file)
    base = merge_base(root, refs, source or default)
    behind = _commits_since(root, base)
    if behind > FAR_BEHIND:
        _say([f"the review will cover {behind} commits since {base[:12]}: if the phase branch "
              f"was cut from another branch, start again with --from <branch>"])
    if again:
        ledger = replace(mine, base=base, plan=rel, high_risk=high_risk, visual=visual,
                         origin=source or default)
    else:
        ledger = Ledger(path=path, phase=name, branch=branch, base=base, plan=rel,
                        high_risk=high_risk, visual=visual, origin=source or default,
                        project=project or "")
    ledger.folder = _folder(ctx, project)
    ignore = build_dir(root) / ".gitignore"
    wrote_ignore = not ignore.exists()
    save(ledger, [(ignore, GITIGNORE.encode("utf-8"))] if wrote_ignore else [])
    if not again:
        _keep_in_history(root, ledger, ignore if wrote_ignore else None)
    lines = [f"{'restarted' if again else 'started'} {name} on {branch}, from {ledger.origin} "
             f"at {ledger.base[:12]}", f"wrote {_local(root, path)}"]
    measure = ctx.cfg.checks.get("agent-measure")
    if measure is not None and measure.level != "off" and not again:
        logged = session or root
        agent_runs.mark(ctx.home, logged, name)
        lines.append(f"phase '{name}' started in {agent_runs.durable_path(ctx.home, logged)}")
    _say(lines + [f"Next: {next_action(ledger)}"])
    return 0


def _keep_in_history(root: Path, ledger: Ledger, wrote: Path | None) -> None:
    """Ask git whether it would commit the ledger just written. A rule in a `.gitignore` above
    the build folder (`build/` is in the stock Python, Node, Gradle and CMake ones) ignores the
    whole folder, and the folder's own negations cannot undo that. If git ignores it, remove what
    `start` wrote and say which rule, in which file, and the line that fixes it."""
    res = phase._run(root, "check-ignore", "-v", "--", str(ledger.path))
    if res.returncode != 0:
        return
    # `source:line:pattern<TAB>path`; a pattern starting `!` is a rule that keeps the file.
    found = phase._out(res).split("\t", 1)[0]
    if found.split(":", 2)[-1].startswith("!"):
        return
    ledger.path.unlink(missing_ok=True)
    if wrote is not None:
        wrote.unlink(missing_ok=True)
    for folder in (ledger.dir, build_dir(root)):
        try:
            folder.rmdir()
        except OSError:
            break
    raise BuildError(
        f"git ignores the ledger: {found} — a ledger git does not commit is lost on the next "
        f"clone. Add the line `!{BUILD_DIR}/` after that rule in the file named, and run start "
        f"again (a `build/` pattern also works if it becomes `/build/`, which matches only the "
        f"top level); nothing was written")


def _need_ux_reviewer(ctx) -> None:
    """A visual phase goes to the `ux-reviewer`: refused when that role is not rendered."""
    if "ux-reviewer" not in roles.rendered(ctx.cfg.checks[roles.CHECK].params):
        raise BuildError("this phase is visual and `ux-reviewer` is not rendered: set "
                         "`visual_paths` on `agent-roles`, run `" + roles.GOVERN
                         + " agents render`, then try again")


def _code_role(ctx, role: str | None) -> str:
    """`role`, or the first rendered role that writes code when none is given; refused when it
    is not one."""
    allowed = [r for r in roles.rendered(ctx.cfg.checks[roles.CHECK].params) if r in CODE_ROLES]
    role = role or allowed[0]
    if role not in allowed:
        raise BuildError(f"{role} is not a rendered role that writes code: a unit goes to "
                         + " or ".join(allowed))
    return role


def unit_add(ctx, uid: str, title: str, role: str | None, red: bool,
             project: str | None = None) -> int:
    root = ctx.root
    ledger = _need(ctx, project)
    if not SLUG.fullmatch(uid):
        raise BuildError(f"{uid!r} is not a unit id: lower-case letters, digits and hyphens, "
                         f"starting with a letter or a digit")
    if uid in RESERVED:
        raise BuildError(f"{uid} is one of the phase's own folders ({', '.join(RESERVED)}): "
                         f"pick another id")
    if ledger.unit(uid) is not None:
        raise BuildError(f"the phase {ledger.phase} already has a unit {uid}")
    role = _code_role(ctx, role)
    title = _one_line(title)
    if not title:
        raise BuildError("--title is empty")
    brief = ledger.brief(uid)
    if brief.exists():
        raise BuildError(f"{_local(root, brief)} is already there and no unit owns it: remove "
                         f"it, or pick another id")
    unit = {"id": uid, "title": title, "role": role, "status": "pending", "attempts": 0,
            "branch": "", "commit": "", "note": ""}
    ledger.units.append(unit)
    save(ledger, [(brief, unit_brief(ledger, unit, red).encode("utf-8"))])
    _say([f"added unit {uid} for {role}: {title}", f"wrote {_local(root, brief)}",
          _then(uid, project=ledger.project)])
    return 0


def unit_drop(ctx, uid: str, project: str | None = None) -> int:
    """Remove a unit that is not done. Its folder goes too, unless git does not hold all of it (an
    untracked, staged or edited file): that is the only copy."""
    ledger = _need(ctx, project)
    unit = ledger.unit(uid)
    if unit is None:
        known = ", ".join(u["id"] for u in ledger.units) or "it has none yet"
        raise BuildError(f"the phase {ledger.phase} has no unit {uid} ({known})")
    if unit["status"] not in DROPPABLE:
        raise BuildError(f"{uid} is {unit['status']}: only a pending, blocked or failed unit "
                         f"can be dropped")
    ledger.units.remove(unit)
    save(ledger)
    folder = ledger.dir / uid
    say = [f"dropped {uid}"]
    if folder.exists():
        rel = _local(ctx.root, folder)
        try:
            res = phase._run(ctx.root, "status", "--porcelain", "--untracked-files=all", "--",
                             str(folder))
            clean = res.returncode == 0 and not phase._out(res)
        except phase.PhaseError:
            clean = False
        if clean:
            profile.rmtree(folder)
        else:
            say.append(f"kept {rel}: it holds files git does not have")
    _say(say)
    return 0


def _branch_tree(root: Path, branch: str) -> Path | None:
    """The project's folder in the other worktree that has `branch` checked out; None when no
    worktree has it, or it is the checkout the command runs in."""
    here = Path(phase._ask(root, "rev-parse", "--show-toplevel")).resolve()
    tree = phase._worktrees(root).get(branch)
    if tree is None or Path(tree).resolve() == here:
        return None
    return Path(tree) / root.resolve().relative_to(here)


def _copy_from_branch(root: Path, branch: str, folder: str, ignored_only: bool) -> list[str]:
    """Copy what a branch's worktree holds under `folder` (relative to the project's root) into
    the same place here, overwriting older copies and creating folders; the worktree is only
    read. Every file but a `brief.md` for a unit's folder, or, with `ignored_only`, only the files
    git ignores there. The lines to say: one per file copied, or one saying why none was."""
    tree = _branch_tree(root, branch)
    if tree is None:
        return [f"no worktree has {branch} checked out; nothing copied"]
    if ignored_only:
        res = phase._run(tree, "ls-files", "--others", "--ignored", "--exclude-standard", "-z",
                         "--", folder)
        names = [n for n in res.stdout.decode("utf-8", errors="replace").split("\0") if n]
        # `ls-files` names paths from the folder it ran in, the project's folder there.
        found = [Path(n) for n in names] if res.returncode == 0 else []
    else:
        base = tree / folder
        found = [f.relative_to(tree) for f in sorted(base.rglob("*"))
                 if f.is_file() and not (f.parent == base and f.name == "brief.md")] \
            if base.is_dir() else []
    lines = []
    for rel in found:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(tree / rel, target)
        lines.append(f"copied {rel.as_posix()}")
    return lines or [f"nothing to copy from the worktree of {branch}"]


def collect(ctx, branch: str, project: str | None = None) -> int:
    """The files an agent that is not a unit left in its worktree, in this checkout."""
    ledger = _need(ctx, project)
    _say(_copy_from_branch(ctx.root, branch, f"{BUILD_DIR}/{ledger.dir.name}", True))
    return 0


def unit_set(ctx, uid: str, status: str, branch: str | None, commit: str | None,
             note: str | None, role: str | None = None, project: str | None = None) -> int:
    ledger = _need(ctx, project)
    unit = ledger.unit(uid)
    if unit is None:
        known = ", ".join(u["id"] for u in ledger.units) or "it has none yet"
        raise BuildError(f"the phase {ledger.phase} has no unit {uid} ({known})")
    if status not in STATUSES:
        raise BuildError(f"{status!r} is not a status: {', '.join(STATUSES)}")
    role = _code_role(ctx, role) if role else unit["role"]
    earlier, unit["role"] = unit["role"], role
    unit["status"] = status
    if status == "running":
        unit["attempts"] += 1
    for key, value in (("branch", branch), ("commit", commit), ("note", note)):
        if value is not None:
            unit[key] = _one_line(value)
    brief = ledger.brief(uid)
    old = new = brief.read_bytes() if brief.is_file() else b""
    if role != earlier:
        # An escalated unit writes a report of its own: the earlier role's is not overwritten.
        new = new.replace(f"/{uid}/{earlier}-report.md".encode(),
                          f"/{uid}/{role}-report.md".encode(), 1)
    beside = [(brief, new)] if new != old else []
    save(ledger, beside)
    said = [f"unit {uid} is {status}"
            + (f" (attempt {unit['attempts']})" if status == "running" else "")
            + (f", now for {role}" if role != earlier else "")]
    if status == "done" and branch:
        said += _copy_from_branch(ctx.root, branch, f"{BUILD_DIR}/{ledger.dir.name}/{uid}", False)
    _say(said)
    return 0


def ruling(ctx, text: str, project: str | None = None) -> int:
    ledger = _need(ctx, project)
    text = _one_line(text)
    if not text:
        raise BuildError("a ruling is one line: what was decided, why, and what it costs if "
                         "wrong")
    ledger.rulings.append(text)
    save(ledger)
    _say([f"ruling {len(ledger.rulings)} recorded for {ledger.phase}"])
    return 0


def status(ctx, as_json: bool, project: str | None = None) -> int:
    branch = checked_out(ctx.root)
    ledger = current(ctx.root, branch) if branch else None
    if ledger is not None:
        ledger.project = ledger.project or project or ""
        ledger.folder = _folder(ctx, ledger.project)
    if as_json:
        _say([json.dumps(status_data(ctx.root, ledger, branch), indent=2, ensure_ascii=False)])
    else:
        _say(status_lines(ledger) if ledger else [nothing_in_flight(branch, project)])
    return 0


def review(ctx, project: str | None = None) -> int:
    root = ctx.root
    ledger = _need(ctx, project)
    if ledger.visual:
        _need_ux_reviewer(ctx)
    if not ledger.units:
        raise BuildError(f"the phase {ledger.phase} has no units: there is nothing to review")
    waiting = [f"{u['id']} is {u['status']}" for u in ledger.units if u["status"] != "done"]
    if waiting:
        raise BuildError("not every unit is done: " + ", ".join(waiting))
    folder = ledger.dir / "review"
    ledger.stage = "review"
    reviewer, qa = folder / "code-reviewer-brief.md", folder / "qa-engineer-brief.md"
    ux = folder / "ux-reviewer-brief.md"
    files = [(folder / "package.diff", package(root, ledger)),
             (folder / "package-stat.txt", package_stat(root, ledger)),
             (reviewer, code_reviewer_brief(root, ledger).encode("utf-8"))]
    lines = [f"wrote {_local(root, path)}" for path, _ in files]
    if ledger.high_risk:
        if qa.exists():
            lines.append(f"kept {_local(root, qa)} as it is (delete it to have it written "
                         f"again)")
        else:
            files.append((qa, qa_engineer_brief(root, ledger).encode("utf-8")))
            lines.append(f"wrote {_local(root, qa)}")
    if ledger.visual:
        if ux.exists():
            lines.append(f"kept {_local(root, ux)} as it is (delete it to have it written "
                         f"again)")
        else:
            files.append((ux, ux_reviewer_brief(root, ledger).encode("utf-8")))
            lines.append(f"wrote {_local(root, ux)}")
    save(ledger, files)
    if ledger.high_risk:
        lines.append(f"fill in the definition of done in {_local(root, qa)}")
    if ledger.visual:
        lines.append(f"fill in what to launch and look at in {_local(root, ux)}")
    _say(lines + [_then("review", "the briefs that need it", ledger.project)])
    return 0


def _filled(brief: Path) -> bool:
    """Whether the coordinator has filled the brief in: no skeleton instruction line is left."""
    return not agent_runs.unfilled(brief.read_text(encoding="utf-8"))


def fix(ctx, role: str | None = None, project: str | None = None) -> int:
    root = ctx.root
    ledger = _need(ctx, project)
    if ledger.stage not in ("review", "fix", "closing"):
        raise BuildError(f"the phase {ledger.phase} is in stage {ledger.stage}: a fix pass "
                         f"follows its review, so run `{govern_build(ledger.project)} review` first")
    role = _code_role(ctx, role)
    folder = ledger.dir / "fix"
    # The first brief nobody has filled in, else the next number: a filled brief is a pass
    # already briefed, and is never touched.
    number = 1
    while True:
        brief = folder / ("brief.md" if number == 1 else f"brief-{number}.md")
        if not brief.exists() or not _filled(brief):
            break
        number += 1
    if brief.exists():
        raise BuildError(f"{_local(root, brief)} is not filled in yet: fill it in, or delete "
                         "it, before asking for another fix brief")
    ledger.stage = "fix"
    head = phase._ask(root, "rev-parse", "HEAD")
    save(ledger, [(brief, fix_brief(ledger, head, role, number).encode("utf-8"))])
    _say([f"wrote {_local(root, brief)}", _then("fix", "its ranked list", ledger.project)])
    return 0


def close(ctx, project: str | None = None) -> int:
    root = ctx.root
    ledger = _need(ctx, project)
    if ledger.stage not in ("review", "fix", "closing"):
        raise BuildError(f"the phase {ledger.phase} is in stage {ledger.stage}: it closes "
                         f"after its review, so run `{govern_build(ledger.project)} review` first")
    brief = ledger.dir / "close" / "brief.md"
    ledger.stage = "closing"
    if brief.exists():
        old = brief.read_bytes()
        try:
            text = old.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        new = _with_rulings(text, ledger, ctx.markers) if text is not None else None
        if new is not None and new != text:
            save(ledger, [(brief, new.encode("utf-8"))])
            lines = [f"kept {_local(root, brief)}, with its rulings brought up to date"]
        else:
            save(ledger)
            lines = [f"kept {_local(root, brief)} as it is (delete it to have it written again)"]
    else:
        save(ledger, [(brief, close_brief(root, ledger).encode("utf-8"))])
        lines = [f"wrote {_local(root, brief)}"]
    folder = f"{BUILD_DIR}/{ledger.phase}"
    git = f"git -C {shlex.quote(ledger.folder)}" if ledger.project else "git"
    _say([*lines, *_rulings(ledger), _then("close", project=ledger.project),
          "once its branch is merged, run:", f"{git} rm -r -q {folder}",
          f'{git} commit -m "Close phase {ledger.phase}: remove its build folder"',
          phase_close(ledger.project, ledger.folder)])
    return 0


def _text(root: Path, brief: Path) -> str:
    """A brief's text; a brief that is not UTF-8 is an error naming it, never a traceback."""
    try:
        return brief.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise BuildError(f"{_local(root, brief)} is not UTF-8") from None


def _fix_briefs(ledger: Ledger) -> list[Path]:
    """The fix briefs, in number order."""
    return sorted((ledger.dir / "fix").glob("brief*.md"), key=lambda p: (len(p.name), p.name))


def _covered(root: Path, ledger: Ledger, what: str) -> list[tuple[str, Path]]:
    """The (role, brief) pairs `build launch <what>` covers, each brief there."""
    first = what if what in RESERVED else ""
    if what == "review":
        found = [("code-reviewer", ledger.dir / "review" / "code-reviewer-brief.md")]
        if ledger.high_risk:
            found.append(("qa-engineer", ledger.dir / "review" / "qa-engineer-brief.md"))
        if ledger.visual:
            found.append(("ux-reviewer", ledger.dir / "review" / "ux-reviewer-brief.md"))
        first = "review"
    elif what == "fix":
        # Every pass not launched yet, in number order; when all were, the latest again.
        numbered = _fix_briefs(ledger)
        chosen = [p for p in numbered if p.name not in ledger.launched] or numbered[-1:]
        found = []
        for path in chosen:
            named = PRODUCES_ROLE.search(_text(root, path))
            found.append((named.group(1) if named else "developer", path))
        first = "fix"
    elif what == "close":
        found = [("technical-writer", ledger.dir / "close" / "brief.md")]
    else:
        unit = ledger.unit(what)
        if unit is None:
            known = ", ".join(u["id"] for u in ledger.units) or "it has none yet"
            raise BuildError(f"the phase {ledger.phase} has no unit {what} ({known})")
        found = [(unit["role"], ledger.brief(what))]
    gone = [brief for _, brief in found if not brief.is_file()]
    if not found or gone:
        raise BuildError((f"{_local(root, gone[0])} is not there" if gone else
                          f"there is no {what} brief")
                         + (f": run `{govern_build(ledger.project)} {first}` first" if first else
                            ": restore it from history"))
    return found


AGAIN = "every fix brief was launched before; this is the latest again:"


def _agent_tree(member: Path, session: Path, project: str, slug: str, uid: str) -> Path:
    """The worktree of `member` an agent of unit `uid` works in, made if it is not there:
    `<session>/.claude/worktrees/<project>-<slug>-<uid>` on the branch `build/<slug>/<uid>`. One
    already there for that branch is reused; anything else at the path, or the branch checked out
    elsewhere, is refused and nothing is removed."""
    branch = f"build/{slug}/{uid}"
    path = session / ".claude" / "worktrees" / f"{project}-{slug}-{uid}"
    try:
        have = phase._worktrees(member)
        if path.exists() or path.is_symlink():
            if branch in have and Path(have[branch]).resolve() == path.resolve():
                return path
            raise BuildError(f"{path} is already there and is not a worktree of {project} on "
                             f"{branch}: move it away, then launch")
        if branch in have:
            raise BuildError(f"{branch} is already checked out in {have[branch]}: remove that "
                             "worktree, then launch")
        head = phase._ask(member, "rev-parse", "HEAD")
        path.parent.mkdir(parents=True, exist_ok=True)
        res = phase._run(member, "worktree", "add", str(path), "-b", branch, head)
        if res.returncode != 0:
            raise BuildError(f"git worktree add {path} failed: {phase._reason(res)}")
    except phase.PhaseError as exc:
        raise BuildError(str(exc)) from None
    return path


def launch(ctx, what: str, project: str | None = None, session: Path | None = None) -> int:
    """Check every brief `what` covers (filled in, committed at HEAD), mark a unit running, and
    print each launch prompt: the role, the brief's path from the repository root, and for a role
    that works in a worktree the commit that worktree must start from. In a workspace (`project`)
    that worktree is made here, inside the member's repository, and named on a `Worktree:` line;
    a role that writes nothing is told the member's folder. `session` is the governance root."""
    root = ctx.root
    ledger = _need(ctx, project)
    found = _covered(root, ledger, what)
    for _, brief in found:
        shown = _local(root, brief)
        left = agent_runs.unfilled(_text(root, brief))
        if left:
            raise BuildError(f"{shown}: the brief is not filled in ({', '.join(left)})")
        # Untracked, staged or changed since HEAD: anything but clean (--ignored, so that an
        # untracked file git ignores is not read as clean).
        if phase._ask(root, "status", "--porcelain", "--ignored", "--", str(brief)):
            raise BuildError(f"{shown} is not committed at HEAD: commit it, then launch")
    head = phase._ask(root, "rev-parse", "HEAD")
    prefix = phase._ask(root, "rev-parse", "--show-prefix")
    lines = []
    again = what == "fix" and all(p.name in ledger.launched for p in _fix_briefs(ledger))
    if again:
        lines.append(AGAIN)
    for role, brief in found:
        lines += [f"launch {role} with the prompt:", launch_line(prefix + _posix(root, brief))]
        if role in WORKTREE_ROLES:
            lines.append(f"Base: {head}")
            if project:
                uid = what if what not in RESERVED else brief.parent.name \
                    + ("-" + brief.stem if what == "fix" else "")
                tree = _agent_tree(root, session or root, project, ledger.dir.name, uid)
                lines += [f"Worktree: {tree}",
                          "launch it without isolation: the worktree is made"]
        elif project:
            lines.append(f"Worktree: {root}")
    unit = ledger.unit(what) if what not in RESERVED else None
    if unit is not None:
        unit["status"] = "running"
        unit["attempts"] += 1
        save(ledger)
    elif what == "fix" and not again:
        ledger.launched += [brief.name for _, brief in found]
        save(ledger)
    _say(lines)
    return 0


def run(ctx, args) -> int:
    """`govern build ...`. Exit 0 when the command did its work; 1 when it refused, with the
    reason as `error:` on stderr and nothing changed. In a workspace the command works in one
    member's repository (`target`): `ctx` is that repository's, from here on."""
    try:
        try:
            root, project = target(ctx, getattr(args, "project", None), args.build_cmd == "start")
        except NoBuild as exc:
            if args.build_cmd != "status":
                raise BuildError(str(exc)[0].lower() + str(exc)[1:]) from None
            # Nothing to report is not a refusal: the same exit `status` has in one repository.
            data = {**status_data(ctx.root, None, ""), "next": str(exc)}
            _say([json.dumps(data, indent=2, ensure_ascii=False)] if args.as_json else [str(exc)])
            return 0
        session, ctx = ctx.root, replace(ctx, root=root)
        if args.build_cmd == "start":
            return start(ctx, args.phase, args.plan, args.high_risk, args.source, args.visual,
                         project, session)
        if args.build_cmd == "unit":
            if args.unit_cmd == "add":
                return unit_add(ctx, args.id, args.title, args.role, args.red, project)
            if args.unit_cmd == "drop":
                return unit_drop(ctx, args.id, project)
            return unit_set(ctx, args.id, args.status, args.branch, args.commit, args.note,
                            args.role, project)
        if args.build_cmd == "launch":
            return launch(ctx, args.what, project, session)
        if args.build_cmd == "collect":
            return collect(ctx, args.branch, project)
        if args.build_cmd == "ruling":
            return ruling(ctx, " ".join(args.text), project)
        if args.build_cmd == "status":
            return status(ctx, args.as_json, project)
        if args.build_cmd == "review":
            return review(ctx, project)
        if args.build_cmd == "fix":
            return fix(ctx, args.role, project)
        return close(ctx, project)
    except (BuildError, phase.PhaseError) as exc:
        print(f"error: {exc}", file=sys.stderr)
    except OSError as exc:
        where = f"{exc.filename}: " if exc.filename else ""
        print(f"error: {where}{exc.strerror or exc} — nothing was written", file=sys.stderr)
    return 1
