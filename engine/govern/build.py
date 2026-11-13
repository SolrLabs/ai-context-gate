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

import functools
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

from govern import agent_runs, config, layout, phase, profile, registry, roles, tomlw
from govern.decisions import mask_lines
from govern.text import Markers

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
UNIT_KEYS = ("id", "title", "role", "status", "attempts", "branch", "commit", "note", "base", "agent")
HEADER = "The build ledger of one phase: written only by `govern build`."
BOM = "﻿"
REPLACE_TRIES = 10
# The roles whose agent works in a worktree of its own, cut from a commit that may be older than
# the phase branch's head: their launch prompt carries `Base:`.
WORKTREE_ROLES = ("developer", "senior-developer", "technical-writer")
PRODUCES_ROLE = re.compile(r"^Produces: (?:.*/)?(.+?)-report(?:-\d+)?\.md\s*$", re.MULTILINE)

# Everything under build/ is ignored except the ledgers and the briefs: reports, diffs and
# failing-test output stay out of history, and the plan of record stays in.
_IGNORE_RULES = """\
*
!*/
!.gitignore
!ledger.toml
!*brief.md
!*brief-[0-9]*.md
"""
GITIGNORE = """\
# Written once by context-gate. Reports, review packages and failing-test output stay out of
# history; the ledger and the briefs are the plan of record and stay in.
""" + _IGNORE_RULES
# With `commit_reports` (agent-roles) the unit and review reports are in history too; `red/` output
# and the review package files stay out.
GITIGNORE_REPORTS = """\
# Written once by context-gate. Unit and review reports are committed with the ledger and the
# briefs; review packages and failing-test output stay out of history.
""" + _IGNORE_RULES + "!*-report.md\n"


# Said above the brief's last lines: the report is part of the task.
REPORT_NOTE = "Write the report below before your final message, and answer your role's hand-back checklist in it. Every line of it is part of this task."
# Said directly above a brief's `Red:` and `Produces:` lines: a committed brief holds no path of
# this machine, and an agent in a worktree of its own reads them from the root of that tree.
PATHS_NOTE = "Paths on the lines below are from the repository root, in your own working tree."


def scratch_note(ledger: "Ledger", name: str) -> str:
    """The brief line that gives one agent its own scratch folder, so agents side by side never
    delete each other's files."""
    return (f"Scratch files go only in {BUILD_DIR}/{ledger.dir.name}/{name}/scratch/; create "
            f"nothing elsewhere and delete only what you created.")


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
    owner: str = ""                    # `git config user.email` of who started it; "" when unknown
    launched: list[str] = field(default_factory=list)   # the fix briefs `launch fix` has printed
    dropped: list[str] = field(default_factory=list)    # the ids `unit drop` removed
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


def state_root(repo: Path) -> Path:
    """The root that holds `repo`'s build state: `repo` itself, unless it is a member of a
    workspace whose registry entry sets `build_root`, a path from the workspace root. Then the
    state lives at `<workspace>/<build_root>`, in the workspace's own (private) repository."""
    moved = _build_root(repo.resolve())
    return repo if moved is None else moved


@functools.lru_cache(maxsize=None)
def _build_root(resolved: Path) -> Path | None:
    root = config.find_root(resolved.parent)
    if root is None:
        return None
    try:
        cfg = config.load(root, layout.home(), pin_check=False)
        if "registry" not in cfg.raw:
            return None
        reg = registry.load(cfg)
    except (config.ConfigError, registry.RegistryMissing) as exc:
        path = Path(str(exc)) if isinstance(exc, registry.RegistryMissing) \
            else root / config.CONFIG_NAME
        raise BuildError(f"{path}: cannot read the workspace registry ({exc}) - fix it, "
                         "then run the command again") from exc
    for scope in reg.scopes:
        where = scope.get("dir")
        if where and (root / where).resolve() == resolved:
            value = scope.get("build_root")
            return root / value if value else None
    return None


state_root.cache_clear = _build_root.cache_clear


def render(ledger: Ledger) -> str:
    """The ledger as TOML: the phase's own keys, then one `[[unit]]` table per unit."""
    head = {"phase": ledger.phase, "branch": ledger.branch, "base": ledger.base}
    if ledger.owner:
        head["owner"] = ledger.owner
    if ledger.project:
        head["project"] = ledger.project
    if ledger.origin:
        head["from"] = ledger.origin
    if ledger.plan:
        head["plan"] = ledger.plan
    head.update(high_risk=ledger.high_risk, visual=ledger.visual, stage=ledger.stage, rulings=list(ledger.rulings))
    if ledger.launched:
        head["launched"] = list(ledger.launched)
    if ledger.dropped:
        head["dropped"] = list(ledger.dropped)
    text = tomlw.dumps(head, HEADER)
    for unit in ledger.units:
        text += "\n[[unit]]\n" + tomlw.dumps({k: unit.get(k, "") if k in ("base", "agent") else unit[k] for k in UNIT_KEYS})
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
    if not isinstance(data.get("owner", ""), str):
        return "`owner` is not text"
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
    dropped = data.get("dropped", [])
    if not isinstance(dropped, list) or not all(isinstance(n, str) for n in dropped):
        return "`dropped` is not a list of text lines"
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
        for key in ("title", "role", "branch", "commit", "note", "base", "agent"):
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
    return parse(text, path)


def parse(text: str, path: Path) -> Ledger:
    """The ledger `text` holds, as the file at `path` (the phase folder's name is checked against
    it). Raises `BuildError` naming `path` when it is not TOML or is not a ledger."""
    def refuse(why: str) -> BuildError:
        return BuildError(f"{path}: {why} — fix the file or restore it from git; nothing was "
                          f"changed")
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
              "note": u.get("note", ""), "base": u.get("base", ""),
              "agent": u.get("agent", "")} for u in data.get("unit", [])]
    return Ledger(path=path, phase=data["phase"], branch=data["branch"], base=data["base"],
                  plan=data.get("plan", ""), origin=data.get("from", ""), high_risk=data.get("high_risk", False),
                  visual=data.get("visual", False),
                  stage=data["stage"], rulings=list(data.get("rulings", [])),
                  launched=list(data.get("launched", [])), dropped=list(data.get("dropped", [])),
                  units=units,
                  crlf="\r\n" in text, bom=text.startswith(BOM), project=data.get("project", ""),
                  owner=data.get("owner", ""))


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
    for path in sorted(build_dir(state_root(root)).glob(f"*/{LEDGER}")):
        try:
            good.append(load(path))
        except BuildError as exc:
            bad.append(exc)
    return good, bad


def _state_repo(state: Path) -> Path:
    """The git top-level of the nearest folder at or above `state` that exists: the repository
    that keeps the build state, which may not have the folder yet. Raises `BuildError`."""
    folder = state
    while not folder.is_dir() and folder != folder.parent:
        folder = folder.parent
    res = phase._git(folder, "rev-parse", "--show-toplevel")
    if res is None or res.returncode != 0:
        raise BuildError(f"{state} is not in a git repository: "
                         + ("git could not run" if res is None else phase._reason(res)))
    return Path(phase._out(res))


def _team(root: Path) -> tuple[list[tuple[Ledger, str]], list[tuple[str, str]], list[BuildError]]:
    """`team_ledgers`, and `(folder, branch)` for each remote ledger that does not parse: it
    protects its own branch, and one warning names it on stderr."""
    from govern import base
    try:
        remotes = sorted(phase._ask(root, "remote").split(), key=len, reverse=True)
        res = phase._run(root, "for-each-ref", "--format=%(refname)", "refs/remotes")
    except phase.PhaseError as exc:
        return [], [], [BuildError(str(exc))]
    if res.returncode != 0:
        return [], [], [BuildError(f"git for-each-ref failed: {phase._reason(res)}")]
    found: list[tuple[Ledger, str]] = []
    unreadable: list[tuple[str, str]] = []
    bad: list[BuildError] = []
    for ref in sorted(r for r in phase._out(res).splitlines() if r and not r.endswith("/HEAD")):
        name = next((ref.removeprefix(f"refs/remotes/{r}/") for r in remotes
                     if ref.startswith(f"refs/remotes/{r}/")), None)
        if name is None:
            continue
        listed = base.ls(root, ref, BUILD_DIR)
        if listed is None:
            bad.append(BuildError(f"{ref}: git could not list {BUILD_DIR}"))
            continue
        depth = BUILD_DIR.count("/") + 3
        for rel in listed:
            parts = rel.split("/")
            if len(parts) != depth or parts[-1] != LEDGER:
                continue
            try:
                ledger = parse(base.read(root, ref, rel), root / rel)
            except base.CannotCompare as exc:
                bad.append(BuildError(f"{ref}: {rel}: {exc.reason}"))
                continue
            except BuildError as exc:
                why = str(exc).split(": ", 1)[-1].split(" — ")[0]
                print(f"warning: {ref}:{rel} is not a readable ledger ({why}): its branch "
                      f"{name} is kept as an open build", file=sys.stderr)
                unreadable.append((parts[-2], name))
                continue
            if ledger.branch == name:
                found.append((ledger, ref))
    return found, unreadable, bad


def team_ledgers(root: Path) -> tuple[list[tuple[Ledger, str]], list[BuildError]]:
    """`(ledger, ref)` for each open build on a remote-tracking branch already fetched: a ledger
    at `.context-gate/build/<phase>/ledger.toml` whose own `branch` is the remote branch's name
    (a branch cut from a phase before it closed holds a ledger for another branch, and is not
    open). Never fetches. A git call that fails is an error, never an empty answer; a ledger
    that does not parse is not one (`open_builds` keeps its branch)."""
    found, _, bad = _team(root)
    return found, bad


def open_builds(path: Path, remote: bool = True) -> tuple[list[tuple[str, str]], list[BuildError]]:
    """`(phase, branch)` for every tracked ledger anywhere in the repository `path` is in, and the
    error for each that does not read; with `remote`, also each open build on a fetched remote
    branch (`team_ledgers`) not already listed. When git cannot name the top (no repository, or
    git cannot run) it is `([], [])` and the caller's own check refuses. A git call that fails
    after that is an error, never an empty answer."""
    top = phase._git(path, "rev-parse", "--show-toplevel")
    if top is None or top.returncode != 0:
        return [], []     # no repository, or git cannot run: `phase.plan` refuses on its own check
    root = Path(phase._out(top))
    where, spec = root, f":(glob)**/{BUILD_DIR}/*/{LEDGER}"
    try:
        state = state_root(root)
        if state != root:
            where = _state_repo(state)
            inside = state.resolve().relative_to(where.resolve()).as_posix()
            spec = f":(glob){'' if inside == '.' else inside + '/'}{BUILD_DIR}/*/{LEDGER}"
    except (BuildError, ValueError) as exc:
        return [], [exc if isinstance(exc, BuildError) else BuildError(str(exc))]
    res = phase._git(where, "ls-files", "-z", "--", spec)
    if res is None or res.returncode != 0:
        return [], [BuildError("git could not list the ledgers")]
    good, bad = [], []
    for name in res.stdout.decode("utf-8", errors="replace").split("\0"):
        file = where / name
        if not name or not file.is_file():
            continue
        try:
            ledger = load(file)
        except BuildError as exc:
            bad.append(exc)
        else:
            good.append((ledger.phase, ledger.branch))
    if remote:
        found, unreadable, errors = _team(root)
        for ledger, _ in found:
            if (ledger.phase, ledger.branch) not in good:
                good.append((ledger.phase, ledger.branch))
        good.extend(u for u in unreadable if u not in good)
        bad.extend(errors)
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


def _rules(lines) -> str:
    """The project's own brief lines, under `## Project rules`; nothing when there are none."""
    return "## Project rules\n\n" + "".join(f"- {line}\n" for line in lines) + "\n" if lines else ""


def unit_brief(ledger: Ledger, unit: dict, red: bool, rules=()) -> str:
    return (f"# {ledger.phase} / {unit['id']} — {unit['title']}\n\n"
            + _sections(UNIT_SECTIONS)
            + _rules(rules)
            + f"{scratch_note(ledger, unit['id'])}\n{REPORT_NOTE}\n{PATHS_NOTE}\n"
            + (f"Red: {_from_root(ledger, unit['id'], 'red')}\n" if red else "")
            + f"Produces: {_from_root(ledger, unit['id'], ledger.report(unit).name)}\n")


def _material(root: Path, ledger: Ledger, folder: Path, diff: bool = True) -> str:
    """What a reviewer of the phase is handed: the plan, each unit's brief and report, and the
    review package (with `diff` False, its commit list and stat only), each as a path from
    `folder`, the one the brief is in."""
    lines = [f"- Plan: {_rel(folder, state_root(root) / ledger.plan)}"] if ledger.plan else []
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
            + f"{scratch_note(ledger, 'review-code')}\n{REPORT_NOTE}\n{PATHS_NOTE}\nProduces: {_from_root(ledger, 'review', 'code-reviewer-report.md')}\n")


def qa_engineer_brief(root: Path, ledger: Ledger) -> str:
    review = ledger.dir / "review"
    return (f"# {ledger.phase} / review — qa-engineer\n\n"
            f"Verify the phase `{ledger.phase}` against its definition of done.\n\n"
            + _sections((DONE_SECTION,))
            + _material(root, ledger, review, diff=False)
            + f"{scratch_note(ledger, 'review-qa')}\n{REPORT_NOTE}\n{PATHS_NOTE}\nProduces: {_from_root(ledger, 'review', 'qa-engineer-report.md')}\n")


def ux_reviewer_brief(root: Path, ledger: Ledger) -> str:
    review = ledger.dir / "review"
    return (f"# {ledger.phase} / review — ux-reviewer\n\n"
            f"Launch the product as the phase `{ledger.phase}` leaves it and look at what it "
            f"changed; the paths below are relative to the folder this brief is in.\n\n"
            + _material(root, ledger, review)
            + _sections(UX_SECTIONS)
            + f"{scratch_note(ledger, 'review-ux')}\n{REPORT_NOTE}\n{PATHS_NOTE}\nProduces: {_from_root(ledger, 'review', 'ux-reviewer-report.md')}\n")


def fix_brief(ledger: Ledger, role: str, number: int, rules=(), unit: str = "") -> str:
    """The fix pass's brief; the `number`th pass names its report `<role>-report-<number>.md`
    (the first has no number), so no pass overwrites another's. A pass for one `unit` says so on
    a `Unit:` line under its title."""
    suffix = f"-{number}" if number > 1 else ""
    return (f"# {ledger.phase} / fix — the fix pass{f' {number}' if number > 1 else ''}\n"
            + (f"Unit: {unit}\n" if unit else "") + "\n"
            + _sections(fix_sections(_from_root(ledger, "fix", f"red{suffix}")))
            + _rules(rules)
            + f"{scratch_note(ledger, f'fix-{number}')}\n\n"
            f"{FIX_RUNNING_NOTE}\n\n{REPORT_NOTE}\n{PATHS_NOTE}\n"
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
            + (f"Plan: {_rel(ledger.dir / 'close', state_root(root) / ledger.plan)}\n\n"
               if ledger.plan else "")
            + (f"Phase owner: {ledger.owner}. When the project keeps one handoff per owner, "
               f"update only the handoff whose frontmatter owner is this email.\n\n"
               if ledger.owner else "")
            + ("## What landed\n\n" + landed + "\n" if landed else "")
            + (RULINGS_HEADING + "\n\n" + rulings + "\n" if rulings else "")
            + _sections((CLOSE_SECTION,))
            + f"{scratch_note(ledger, 'close')}\n\n{AS_BUILT_NOTE}\n\n{REPORT_NOTE}\n{PATHS_NOTE}\nProduces: "
            f"{_from_root(ledger, 'close', 'technical-writer-report.md')}\n")


# ---------------------------------------------------------------------------- status

def _names(units: list[dict]) -> str:
    return _join([u["id"] for u in units])


def _join(ids: list[str]) -> str:
    return ids[0] if len(ids) == 1 else ", ".join(ids[:-1]) + " and " + ids[-1]


def govern_build(project: str | None) -> str:
    """The command that starts every `build` line the coordinator is told to run."""
    return f"{roles.GOVERN} build" + (f" --project {shlex.quote(project)}" if project else "")


def phase_close(project: str | None, folder: str) -> str:
    return f"{roles.GOVERN} phase close" + (f" --path {shlex.quote(folder)}" if project else "")


PHASE_HEADING = re.compile(r"^##\s.*?\(.*?\bslug:\s*(?P<slug>[^\s;)]+)\s*[;)]")
UNIT_HEADING = re.compile(r"^### ([A-Za-z0-9_-]+):\s")


def planned_units(root: Path, ledger: Ledger) -> list[str] | None:
    """The unit ids the plan names for this phase, in plan order: the `### <id>: <title>`
    headings under the phase's `## ` heading whose parenthetical holds `slug: <phase>`. None when
    the ledger has no plan, the plan cannot be read or no phase heading names the slug."""
    if not ledger.plan:
        return None
    try:
        text = (state_root(root) / ledger.plan).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None
    ids: list[str] = []
    inside = found = False
    lines = text.splitlines()
    live = mask_lines(lines, Markers("\x00"))   # fences and comments only: no real marker prefix
    for line, is_live in zip(lines, live):
        if not is_live:
            continue
        if line.startswith("## "):
            match = PHASE_HEADING.match(line)
            inside = bool(match and match["slug"] == ledger.phase)
            found = found or inside
        elif inside and (unit := UNIT_HEADING.match(line)):
            ids.append(unit[1])
    return ids if found else None


def unadded(ledger: Ledger, planned: list[str] | None) -> list[str]:
    """The planned ids that are neither units of the ledger nor dropped, in plan order."""
    have = {u["id"] for u in ledger.units} | set(ledger.dropped)
    return [uid for uid in planned or [] if uid not in have]


def next_action(ledger: Ledger, planned: list[str] | None = None) -> str:
    """One sentence: what the coordinator does next, and the command for it. A function of the
    ledger and, when given, the unit ids the plan names for the phase."""
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
        missing = unadded(ledger, planned)
        if planned is None:
            return f"Every added unit is done: run `{govern} review`."
        if missing:
            return (f"Every added unit is done; the plan names {_join(missing)} not yet added: "
                    f"add them with `{govern} unit add`, or drop them from the plan.")
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


def _base(root: Path, path: Path) -> Path:
    """What a printed path is relative to: the project's root, or the workspace root for a file
    in the build state a `build_root` moved out of the project."""
    state = state_root(root)
    if state != root and (path == state or state in path.parents):
        top = config.find_root(root.resolve().parent)
        if top is not None:
            return top
    return root


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
                "total": 0, "next": nothing_in_flight(branch), "unadded": []}
    planned = planned_units(root, ledger)
    units = [{**{k: u[k] for k in UNIT_KEYS if k not in ("base", "agent")}, "brief": _posix(_base(root, ledger.brief(u["id"])), ledger.brief(u["id"])),
              "report": _posix(_base(root, ledger.report(u)), ledger.report(u))} for u in ledger.units]
    return {"phase": ledger.phase, "branch": ledger.branch, "base": ledger.base,
            "plan": ledger.plan or None, "high_risk": ledger.high_risk, "visual": ledger.visual, "stage": ledger.stage,
            "ledger": _posix(_base(root, ledger.path), ledger.path), "rulings": list(ledger.rulings), "units": units,
            "done": sum(1 for u in ledger.units if u["status"] == "done"),
            "total": len(ledger.units), "next": next_action(ledger, planned),
            "unadded": unadded(ledger, planned)}


def nothing_in_flight(branch: str, project: str | None = None) -> str:
    where = f"on {branch}" if branch else "on a detached HEAD"
    return (f"No build is in flight {where}: start one from a phase branch with "
            f"`{govern_build(project)} start <phase>`.")


def _rulings(ledger: Ledger) -> list[str]:
    if not ledger.rulings:
        return []
    return ["Rulings:"] + [f"  {n}. {r}" for n, r in enumerate(ledger.rulings, 1)]


def status_lines(ledger: Ledger, planned: list[str] | None = None) -> list[str]:
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
    return lines + _rulings(ledger) + [f"Next: {next_action(ledger, planned)}"]


def session_line(root: Path) -> str | None:
    """The line a session starts with when the checked-out branch has a build in flight, or
    None. Raises when git or a ledger cannot say."""
    if not build_dir(state_root(root)).is_dir():
        return None
    branch = checked_out(root)
    ledger = current(root, branch) if branch else None
    if ledger is None:
        return None
    done = sum(1 for u in ledger.units if u["status"] == "done")
    return (f"A build is in flight: {ledger.phase}, {done} of {len(ledger.units)} units done. "
            f"Next: {next_action(ledger, planned_units(root, ledger))}")


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


def git_email(root: Path) -> str | None:
    """`git config user.email` in `root`; None when git cannot say or it is not set."""
    try:
        res = phase._run(root, "config", "user.email")
    except phase.PhaseError:
        return None
    text = phase._out(res)
    return text if res.returncode == 0 and text else None


def _hygiene_refusal(ctx, project: str | None, root: Path, state: Path) -> None:
    """A member that `checkout-hygiene` keeps free of `.context-gate/build` cannot hold its own
    build state: refuse to start one there, and point at `build_root`."""
    check = ctx.cfg.checks.get("checkout-hygiene")
    scope = ctx.registry.find(project) if project else None
    if check is None or check.level == "off" or scope is None or state != root:
        return
    if scope.get("role") not in check.params.get("roles", []):
        return
    for entry in check.params.get("forbidden", []):
        held = str(entry).rstrip("/")
        if held == BUILD_DIR or BUILD_DIR.startswith(held + "/"):
            raise BuildError(f"{project}'s checkout must hold none of '{entry}' "
                             f"(checkout-hygiene), so its build state cannot live in it: set "
                             f"build_root on its registry entry - see docs/configuration.md")


def start(ctx, name: str, plan: str | None, high_risk: bool, source: str | None = None,
          visual: bool = False, project: str | None = None, session: Path | None = None,
          take_over: bool = False) -> int:
    """`session` is the governance root, where runs are logged; `ctx.root` is the member's.
    `take_over` restarts a phase another person owns, as its new owner (one write)."""
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
    state = state_root(root)
    _hygiene_refusal(ctx, project, root, state)
    if state != root:
        _state_clean(root, state)
    mine = current(root, branch)
    email = git_email(root)
    taken = ""
    if mine is not None and mine.owner and email is not None \
            and mine.owner.lower() != email.lower():
        if not take_over:
            raise BuildError(f"{mine.phase} belongs to {mine.owner}; run with --take-over to "
                             f"take it")
        taken = mine.owner
    # The same phase with no unit yet may be started again, to correct its base or origin.
    again = mine is not None and mine.phase == name and not mine.units
    if mine is not None and not again:
        raise BuildError(f"a build is already in flight on {branch}: {mine.phase} "
                         f"({_local(_base(root, mine.path), mine.path)})")
    for fetched, _ in team_ledgers(root)[0]:
        if fetched.phase == name and fetched.branch != branch:
            raise BuildError(f"the phase {name} is in flight on {fetched.branch} (a fetched "
                             f"branch): pick another name")
    path = build_dir(state) / name / LEDGER
    if path.exists() and not again:
        raise BuildError(f"the phase {name} already has a ledger, for the branch "
                         f"{load(path).branch} ({_local(_base(root, path), path)}): pick another name")
    rel = ""
    if plan:
        file = state / plan
        if not file.is_file():
            raise BuildError(f"--plan {plan}: no such file (a relative path is read from the "
                             f"{'build state' if state != root else 'project'}'s root)")
        rel = _posix(state, file)
    base = merge_base(root, refs, source or default)
    behind = _commits_since(root, base)
    if behind > FAR_BEHIND:
        _say([f"the review will cover {behind} commits since {base[:12]}: if the phase branch "
              f"was cut from another branch, start again with --from <branch>"])
    if again:
        ledger = replace(mine, base=base, plan=rel, high_risk=high_risk, visual=visual,
                         origin=source or default, owner=mine.owner or email or "")
        if taken:
            today = time.strftime("%Y-%m-%d", time.gmtime())
            ledger.rulings = [*mine.rulings, f"taken over from {taken} on {today}"]
            ledger.owner = email
    else:
        ledger = Ledger(path=path, phase=name, branch=branch, base=base, plan=rel,
                        high_risk=high_risk, visual=visual, origin=source or default,
                        project=project or "", owner=email or "")
    ledger.folder = _folder(ctx, project)
    ignore = build_dir(state) / ".gitignore"
    wrote_ignore = not ignore.exists()
    want = _gitignore(ctx)
    write_ignore = wrote_ignore
    ignore_bytes = want.encode("utf-8")
    if not wrote_ignore:
        try:
            raw = ignore.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            raw = None
        if raw is not None:
            bom = raw.startswith(BOM)
            have = raw.removeprefix(BOM).replace("\r\n", "\n")
            write_ignore = have in (GITIGNORE, GITIGNORE_REPORTS) and have != want
            if write_ignore:
                ignore_bytes = ((BOM if bom else "")
                                + (want.replace("\n", "\r\n") if "\r\n" in raw else want)
                                ).encode("utf-8")
    save(ledger, [(ignore, ignore_bytes)] if write_ignore else [])
    if not again:
        _keep_in_history(root, ledger, ignore if wrote_ignore else None)
    if state != root:
        _exclude_state(root)
    lines = [f"{'restarted' if again else 'started'} {name} on {branch}, from {ledger.origin} "
             f"at {ledger.base[:12]}", f"wrote {_local(_base(root, path), path)}"]
    measure = ctx.cfg.checks.get("agent-measure")
    if measure is not None and measure.level != "off" and not again:
        logged = session or root
        try:
            agent_runs.mark(ctx.home, logged, name)
            lines.append(f"phase '{name}' started in {agent_runs.durable_path(ctx.home, logged)}")
        except OSError as exc:
            print(f"warning: run record not written: "
                  f"{exc.filename or agent_runs.durable_path(ctx.home, logged)}: "
                  f"{exc.strerror or exc}", file=sys.stderr)
    if taken:
        lines.insert(0, f"took over {name} from {taken}")
    _say(lines + [f"Next: {next_action(ledger, planned_units(root, ledger))}"])
    return 0


def _state_clean(root: Path, state: Path) -> None:
    """Refuse when the repository that keeps the build state has uncommitted changes in the
    build folder: `start` writes there, and the member's own check cannot see that repository."""
    repo, folder = _state_repo(state), build_dir(state)
    res = phase._run(repo, "status", "--porcelain", "--", str(folder))
    if res.returncode != 0:
        raise BuildError(f"git status failed in {repo}: {phase._reason(res)}")
    if phase._out(res):
        raise BuildError(f"uncommitted changes under {_local(_base(root, folder), folder)}: "
                         "commit them first")


def _exclude_state(root: Path) -> None:
    """Make the member ignore its own `.context-gate/build/` through `info/exclude` in the git
    directory its worktrees share, so a build folder left there is never committed to the public
    repository. Written only when git does not ignore the folder yet, anchored (`/<prefix>...`)
    at the repository's top so it matches the member's own folder and nothing else."""
    res = phase._run(root, "check-ignore", "-q", f"{BUILD_DIR}/x/{LEDGER}")
    if res.returncode == 0:
        return
    if res.returncode != 1:
        raise BuildError(f"git check-ignore failed in {root}: {phase._reason(res)}")
    common = phase._run(root, "rev-parse", "--git-common-dir")
    if common.returncode != 0:
        raise BuildError(f"git rev-parse failed in {root}: {phase._reason(common)}")
    where = phase._run(root, "rev-parse", "--show-prefix")
    if where.returncode != 0:
        raise BuildError(f"git rev-parse failed in {root}: {phase._reason(where)}")
    exclude = root / phase._out(common) / "info" / "exclude"
    try:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        have = exclude.read_bytes() if exclude.exists() else b""
        gap = b"" if not have or have.endswith(b"\n") else b"\n"
        line = f"/{phase._out(where)}{BUILD_DIR}/\n"
        exclude.write_bytes(have + gap + line.encode("utf-8"))
    except OSError as exc:
        raise BuildError(f"cannot write {exclude}: {exc}") from exc


def _ledger_ignored(root: Path, ledger: Ledger) -> str | None:
    """The `source:line:pattern` of the rule that makes git ignore the ledger; None when git
    would commit it (a `!` negation keeps it)."""
    state = state_root(root)
    repo = root if state == root else _state_repo(state)
    res = phase._run(repo, "check-ignore", "-v", "--", str(ledger.path))
    if res.returncode != 0:
        return None
    # `source:line:pattern<TAB>path`; a pattern starting `!` is a rule that keeps the file.
    found = phase._out(res).split("\t", 1)[0]
    return None if found.split(":", 2)[-1].startswith("!") else found


def _ignored_refusal(found: str, then: str) -> BuildError:
    """The refusal for a ledger git ignores, worded as `_keep_in_history`'s is; `then` is what
    to run once the rule is fixed."""
    return BuildError(
        f"git ignores the ledger: {found} — a ledger git does not commit is lost on the next "
        f"clone. Add the line `!{BUILD_DIR}/` after that rule in the file named, and {then} "
        f"(a `build/` pattern also works if it becomes `/build/`, which matches only the "
        f"top level); nothing was changed")


def _keep_in_history(root: Path, ledger: Ledger, wrote: Path | None) -> None:
    """Ask git whether it would commit the ledger just written. A rule in a `.gitignore` above
    the build folder (`build/` is in the stock Python, Node, Gradle and CMake ones) ignores the
    whole folder, and the folder's own negations cannot undo that. If git ignores it, remove what
    `start` wrote and say which rule, in which file, and the line that fixes it."""
    state = state_root(root)
    found = _ledger_ignored(root, ledger)
    if found is None:
        return
    ledger.path.unlink(missing_ok=True)
    if wrote is not None:
        wrote.unlink(missing_ok=True)
    for folder in (ledger.dir, build_dir(state)):
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


def _gitignore(ctx) -> str:
    """The build folder's `.gitignore` the project's `agent-roles` params ask for."""
    check = ctx.cfg.checks.get(roles.CHECK)
    on = check is not None and check.params.get("commit_reports", False) is True
    return GITIGNORE_REPORTS if on else GITIGNORE


def _brief_lines(ctx) -> list[str]:
    """The lines the project's `agent-roles` params give every brief."""
    check = ctx.cfg.checks.get(roles.CHECK)
    return list(check.params.get("brief_lines", [])) if check is not None else []


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
        raise BuildError(f"{_local(_base(root, brief), brief)} is already there and no unit owns it: remove "
                         f"it, or pick another id")
    unit = {"id": uid, "title": title, "role": role, "status": "pending", "attempts": 0,
            "branch": "", "commit": "", "note": "", "base": "", "agent": ""}
    ledger.units.append(unit)
    save(ledger, [(brief, unit_brief(ledger, unit, red, _brief_lines(ctx)).encode("utf-8"))])
    _say([f"added unit {uid} for {role}: {title}", f"wrote {_local(_base(root, brief), brief)}",
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
    ledger.dropped.append(uid)
    save(ledger)
    folder = ledger.dir / uid
    say = [f"dropped {uid}"]
    if folder.exists():
        rel = _local(_base(ctx.root, folder), folder)
        try:
            state = state_root(ctx.root)
            repo = ctx.root if state == ctx.root else _state_repo(state)
            res = phase._run(repo, "status", "--porcelain", "--untracked-files=all", "--",
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


def _copy_from_branch(root: Path, branch: str, folder: str, ignored_only: bool,
                      dest: Path | None = None) -> list[str]:
    """Copy what a branch's worktree holds under `folder` (relative to the project's root) into
    the same place under `dest` (the project's root unless given), overwriting older copies and
    creating folders; the worktree is only read. Every file but a `brief.md` for a unit's folder, or, with `ignored_only`, only the files
    git ignores there. The lines to say: one per file copied, or one saying why none was."""
    dest = root if dest is None else dest
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
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(tree / rel, target)
        lines.append(f"copied {_posix(_base(root, target), target)}")
    return lines or [f"nothing to copy from the worktree of {branch}"]


# Folders and files an ignored-file warning does not name: caches a tool rebuilds.
_CACHE = ("__pycache__", "node_modules", ".pytest_cache")


def _scope_paths(text: str) -> list[str]:
    """The paths a report's `Scope:` line names, in order. Fenced code and comments are not read."""
    lines = text.splitlines()
    live = mask_lines(lines, Markers("\x00"))
    for line, ok in zip(lines, live):
        found = re.match(r"^[\s>*\-\d.]*`?scope`?\s*:`?\s*(.*)$", line, re.IGNORECASE) if ok else None
        if not found:
            continue
        rest = found.group(1)
        names = re.findall(r"`([^`]+)`", rest) or _split_outside_braces(rest)
        out = []
        for name in names:
            name = re.sub(r",\s+(?=[^{}]*\})", ",", name.strip())
            if len(name) > 4 and name.startswith("**") and name.endswith("**"):
                name = name[2:-2]
            name = name.rstrip(".,;:").replace("\\", "/")
            if name and ("/" in name or "." in name.strip(".")) and " " not in name:
                out.append(name)
        return out
    return []


def _split_outside_braces(text: str) -> list[str]:
    """`text` split at commas, semicolons and whitespace that are not inside `{...}`."""
    out, depth, cur = [], 0, []
    for ch in text:
        depth = max(0, depth + (ch == "{") - (ch == "}"))
        if depth == 0 and (ch in ",;" or ch.isspace()):
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return [x for x in out if x]


_RANGE_LIMIT = 1000
_RANGE = re.compile(r"^(?P<stem>.*?)(?P<lo>\d+)\.\.(?:(?P=stem))?(?P<hi>\d+)(?P<tail>.*)$")


def _braces(name: str) -> list[str] | None:
    """`name` with each `{a,b}` group expanded, or None for nested or unbalanced braces."""
    start = name.find("{")
    if start < 0:
        return None if "}" in name else [name]
    end = name.find("}", start)
    if end < 0 or "{" in name[start + 1:end]:
        return None
    rest = _braces(name[end + 1:])
    if rest is None:
        return None
    return [name[:start] + part + tail for part in name[start + 1:end].split(",")
            for tail in rest]


def _glob_regex(pattern: str) -> re.Pattern:
    """`*` and `?` within one path part, `**` across parts, `[...]` as fnmatch."""
    out, i = [], 0
    while i < len(pattern):
        ch = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif ch == "*":
            out.append("[^/]*")
            i += 1
        elif ch == "?":
            out.append("[^/]")
            i += 1
        elif ch == "[" and pattern.find("]", i + 2) > 0:
            close = pattern.find("]", i + 2)
            body = pattern[i + 1:close].replace("\\", "\\\\")
            out.append("[" + ("^" + body[1:] if body.startswith("!") else body) + "]")
            i = close + 1
        else:
            out.append(re.escape(ch))
            i += 1
    return re.compile("".join(out))


def _range_names(name: str) -> list[str] | None:
    """The names a `<stem><lo>..<hi><tail>` range in the last path part stands for."""
    head, sep, last = name.rpartition("/")
    found = _RANGE.match(last)
    if not found or ".." in head:
        return None
    lo, hi = found.group("lo"), found.group("hi")
    if int(lo) > int(hi) or int(hi) - int(lo) + 1 > _RANGE_LIMIT:
        return None
    return [f"{head}{sep}{found.group('stem')}{str(n).zfill(len(lo))}{found.group('tail')}"
            for n in range(int(lo), int(hi) + 1)]


def _parents(path: str) -> list[str]:
    """The folders above `path`, as `a`, `a/b` for `a/b/c.py`."""
    parts = path.split("/")[:-1]
    return ["/".join(parts[:n]) for n in range(1, len(parts) + 1)]


def _scope_check(token: str, changed: list[str]) -> tuple[int, list[str]] | None:
    """How many names `token` stands for and which no changed file matches, or None."""
    name = token.replace("\\", "/")
    while name.startswith("./"):
        name = name[2:]
    groups = _braces(name)
    if groups is None:
        return None
    names: list[str] = []
    for group in groups:
        if ".." in group:
            expanded = _range_names(group)
            if expanded is None:
                return None
            names += expanded
        else:
            names.append(group)
    missing = []
    for one in names:
        if re.search(r"[*?\[]", one):
            if one.endswith("/"):
                rx = _glob_regex(one.rstrip("/"))
                hit = any(rx.fullmatch(parent) for c in changed for parent in _parents(c))
            else:
                hit = any(_glob_regex(one).fullmatch(c) for c in changed)
        elif one.endswith("/"):
            hit = any(c.startswith(one) for c in changed)
        else:
            hit = one in changed or any(c.startswith(one + "/") for c in changed)
        if not hit:
            missing.append(one)
    return len(names), missing


def _scope_cover(token: str, changed: list[str]) -> list[str] | None:
    """The expansions of a Scope `token` that no changed file matches ([] = covered), or None
    when the token cannot be resolved. Understands `{a,b}`, `lo..hi` ranges, globs and folders."""
    checked = _scope_check(token, changed)
    return None if checked is None else checked[1]


def _handback(ctx, ledger: Ledger, unit: dict, branch: str) -> tuple[list[str], list[str]]:
    """What stops a unit from being marked done on `branch`, and what only deserves a look.
    Reads git and the worktree; changes nothing."""
    root = ctx.root
    base = unit.get("base") or ledger.base
    short = base[:7]
    refusals: list[str] = []
    warnings: list[str] = []
    if not phase._yes(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"):
        raise BuildError(f"there is no branch {branch} here: nothing was changed")
    span = f"{base}..refs/heads/{branch}"
    count = phase._ask(root, "rev-list", "--count", span)
    if count == "0":
        refusals.append(f"branch has no commit past its base {short}")
    diff = phase._run(root, "diff", "--name-only", "-z", span)
    if diff.returncode != 0:
        raise BuildError(f"git diff failed: {phase._reason(diff)}")
    changed = [n for n in diff.stdout.decode("utf-8", errors="replace").split("\0") if n]
    if not changed:
        refusals.append(f"branch changes nothing since its base {short}")
    tree = _branch_tree(root, branch)
    top = None
    if tree is not None:
        top = Path(phase._ask(tree, "rev-parse", "--show-toplevel"))
        res = phase._run(tree, "status", "--porcelain", "-z", "--untracked-files=all")
        if res.returncode != 0:
            raise BuildError(f"git status failed in {tree}: {phase._reason(res)}")
        dirty, skip = [], False
        for entry in res.stdout.decode("utf-8", errors="replace").split("\0"):
            if skip:
                skip = False      # the old name of a rename or copy
                continue
            if len(entry) < 4:
                continue
            skip = entry[0] in "RC" or entry[1] in "RC"
            path = entry[3:]
            if not (path.startswith(f"{BUILD_DIR}/") or f"/{BUILD_DIR}/" in path):
                dirty.append(path)
        if dirty:
            more = f" (and {len(dirty) - 10} more)" if len(dirty) > 10 else ""
            refusals.append(f"uncommitted changes in {tree}: {', '.join(dirty[:10])}{more}")
    found = _ledger_ignored(root, ledger)
    if found is not None:
        refusals.append(_ignored_refusal(found, "run this again").args[0])
    patterns = roles.forbidden_trailers(ctx.cfg.checks[roles.CHECK].params)
    if patterns and count != "0":
        res = phase._run(root, "log", "--format=%h%x1f%B%x1e", span)
        for entry in res.stdout.decode("utf-8", errors="replace").split("\x1e"):
            sha, _, body = entry.strip("\n").partition("\x1f")
            for rx in patterns:
                hit = rx.search(body)
                if hit:
                    row = body[:hit.start()].count("\n")
                    line = body.splitlines()[row] if row < len(body.splitlines()) else hit.group(0)
                    refusals.append(f"commit {sha.strip()} carries a forbidden line "
                                    f"({line.strip()}): rewrite the unpushed commit before "
                                    f"merging")
                    break
    report = ledger.report(unit)
    sources = [report]
    if tree is not None and root.resolve() in report.resolve().parents:
        sources.append(tree / report.resolve().relative_to(root.resolve()))
    for source in sources:
        try:
            text = source.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        seen = set(changed)       # earlier rounds sit below a relaunched unit base: read the whole branch
        log = phase._run(root, "log", "--name-only", "-z", "--format=",
                         f"{ledger.base}..refs/heads/{branch}")
        if log.returncode == 0:
            seen.update(n.strip("\n") for n in log.stdout.decode("utf-8", errors="replace")
                        .split("\0") if n.strip("\n"))
        listed = sorted(seen)
        lost = []
        for name in _scope_paths(text):
            checked = _scope_check(name, listed)
            if checked is None:
                lost.append(name)
            elif checked[1]:
                more = (f" ({len(checked[1])} of {checked[0]} not changed: "
                        f"{', '.join(checked[1][:3])})" if checked[0] > 1 else "")
                warnings.append(f"the report's Scope names {name}, which the branch does not "
                                f"change{more}")
        if lost:
            warnings.append(f"could not resolve the report's Scope entries: {', '.join(lost)}")
        break
    if top is not None:
        folders = sorted({Path(n).parent.as_posix() for n in changed
                          if Path(n).parent.as_posix() != "."})
        res = phase._run(top, "ls-files", "--others", "--ignored", "--exclude-standard", "-z",
                         "--", *folders) if folders else None
        if res is not None and res.returncode == 0:
            left = [n for n in res.stdout.decode("utf-8", errors="replace").split("\0")
                    if n and not n.startswith(f"{BUILD_DIR}/")      # reports: `unit set` copies them
                    and not any(part in _CACHE or part.endswith(".pyc")
                                for part in Path(n).parts)]
            if left:
                warnings.append(f"git ignores {', '.join(left[:5])}"
                                + (f" (and {len(left) - 5} more)" if len(left) > 5 else "")
                                + " in a folder the branch changes")
    return refusals, warnings


def verify(ctx, uid: str, branch: str, project: str | None = None) -> int:
    """Say whether `branch` could be marked done for unit `uid`; changes nothing. Exit 1 on a
    refusal."""
    ledger = _need(ctx, project)
    unit = ledger.unit(uid)
    if unit is None:
        known = ", ".join(u["id"] for u in ledger.units) or "it has none yet"
        raise BuildError(f"the phase {ledger.phase} has no unit {uid} ({known})")
    refusals, warnings = _handback(ctx, ledger, unit, branch)
    _say([f"refused: {r}" for r in refusals] + [f"warning: {w}" for w in warnings]
         or [f"{branch} is ready to be marked done"])
    return 1 if refusals else 0


def collect(ctx, branch: str, project: str | None = None) -> int:
    """The files an agent that is not a unit left in its worktree, in this checkout."""
    ledger = _need(ctx, project)
    _say(_copy_from_branch(ctx.root, branch, f"{BUILD_DIR}/{ledger.dir.name}", True,
                           state_root(ctx.root)))
    return 0


GATE_RECORD = "gate/last.json"
GATE_LOG = "gate/last.log"
GATE_TAIL = 15
AFTER_RED = ('run python3 .context-gate/bin/govern build gate and get it green, or pass '
             '--accept-red "<reason>"')


def _gate_params(ctx) -> dict:
    check = ctx.cfg.checks.get(roles.CHECK)
    return check.params if check is not None else {}


def _full_gate(ctx) -> str:
    return str(_gate_params(ctx).get("full_gate", "") or "").strip()


def _unit_gate(ctx) -> str:
    return _gate_params(ctx).get("unit_gate", "scoped")


def _gate_record(ledger: Ledger) -> dict | None:
    """What `build gate` last wrote for this phase; None when there is none or it is unreadable."""
    try:
        data = json.loads((ledger.dir / GATE_RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_record(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")
    _replace(tmp, path)


def _outside_build(root: Path, *args: str) -> list[str] | None:
    """The lines git prints for `args`, less the build folder; None when git cannot say."""
    try:
        res = phase._run(root, *args, "--", ".", f":(exclude){BUILD_DIR}")
    except phase.PhaseError:
        return None
    if res.returncode != 0:
        return None
    return [ln for ln in phase._out(res).splitlines() if ln.strip()]


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _head(root: Path) -> str:
    try:
        return phase._out(phase._run(root, "rev-parse", "HEAD"))
    except phase.PhaseError:
        return ""


def _run_check(session: Path | None, ctx, project: str | None) -> tuple[int, str]:
    """The project's own `govern check`, as `govern check [--project P]` runs it: its status and
    its output. A failure to load or run it is a red check, the message its output."""
    import contextlib
    import io
    from govern import cli
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            wctx = cli.load_context(session or ctx.root, ctx.prog)
            rc = cli.cmd_check(wctx, project)
    except Exception as exc:  # noqa: BLE001 - any failure of the check is a red check
        out.write(f"{type(exc).__name__}: {exc}\n")
        rc = 1
    return (rc or 0), out.getvalue()


def gate(ctx, cmd: str | None = None, project: str | None = None,
         session: Path | None = None) -> int:
    """Run the project's full gate and its `govern check`; record the result for this phase
    unless `cmd` is given, which runs only that command."""
    ledger = _need(ctx, project)
    root = ctx.root
    run_cmd = cmd or _full_gate(ctx)
    if not run_cmd:
        print("no full_gate set under [checks.agent-roles]; nothing was run")
        return 1
    record, log = ledger.dir / GATE_RECORD, ledger.dir / GATE_LOG
    if not cmd:
        _write_record(record, {"status": "running", "at": _now()})
    log.parent.mkdir(parents=True, exist_ok=True)
    # What the gate tests is the tree as it is when it starts: a commit made while it runs (a
    # merge in the background) leaves the record stale, never recorded as tested.
    head = _head(root)
    changed = _outside_build(root, "status", "--porcelain")
    dirty = changed is None or bool(changed)
    started = time.monotonic()
    with open(log, "wb") as out:
        rc = subprocess.run(run_cmd, shell=True, cwd=root, stdin=subprocess.DEVNULL,
                            stdout=out, stderr=subprocess.STDOUT).returncode
    gate_rc = rc
    gate_out = log.read_text(encoding="utf-8", errors="replace").splitlines()
    check_rc, check_text = 0, ""
    if not cmd:
        check_rc, check_text = _run_check(session, ctx, project)
        with open(log, "ab") as out:
            out.write(b"\n== govern check ==\n" + check_text.encode("utf-8"))
        rc = rc or (1 if check_rc else 0)
    seconds = round(time.monotonic() - started)
    if _head(root) != head:
        dirty = True
    if not cmd:
        _write_record(record, {"status": "done", "rc": rc, "check_rc": check_rc, "head": head,
                               "dirty": dirty, "seconds": seconds,
                               "at": _now(), "cmd": run_cmd})
    lines = [f"gate {'green' if rc == 0 else 'red'} (rc {rc}, {seconds} s, head {head[:7]})"]
    lines.append(f"log: {_local(_base(root, log), log)}")
    if gate_rc:
        lines += gate_out[-GATE_TAIL:]
    if check_rc:
        lines.append("check: red (govern check" + (f" --project {project}" if project else "") + ")")
        wrong = [ln for ln in check_text.splitlines() if re.search(r"error|ERROR|FAIL", ln)]
        lines += wrong[:GATE_TAIL] if wrong else check_text.splitlines()[-GATE_TAIL:]
    _say(lines)
    return 0 if rc == 0 else 1


def _need_green(ctx, ledger: Ledger, step: str) -> str | None:
    """Why `step` may not go on: the phase's last full gate is red, missing or stale; None when
    it is green for this tree. Work under the build folder does not make a record stale."""
    tail = f": {AFTER_RED}"
    rec = _gate_record(ledger)
    if rec is None or rec.get("status") != "done":
        return f"cannot {step}: no full gate has run on this phase{tail}"
    head = str(rec.get("head") or "")
    short = head[:7]
    if rec.get("rc") != 0:
        return f"cannot {step}: the last full gate is red (rc {rec.get('rc')} at {short}){tail}"
    if rec.get("dirty") is not False:
        return (f"cannot {step}: the last full gate is stale: the tree had uncommitted changes "
                f"at {short}{tail}")
    moved = _outside_build(ctx.root, "diff", "--name-only", head, "HEAD") if head else None
    if moved is None:
        return (f"cannot {step}: the last full gate is stale: git cannot compare {short} with "
                f"HEAD{tail}")
    if moved:
        return (f"cannot {step}: the last full gate is stale: {len(moved)} files changed since "
                f"{short}{tail}")
    return None


def _gated(ctx, ledger: Ledger, step: str, accept_red: str | None,
           project: str | None) -> Ledger:
    """Refuse `step` unless the full gate is green; `accept_red` lets it through with a ruling.
    Returns the ledger to go on with: reloaded when the ruling changed it."""
    refusal = _need_green(ctx, ledger, step)
    if refusal is None:
        return ledger
    if not accept_red or not accept_red.strip():
        raise BuildError(refusal)
    what = refusal.split(": ", 1)[1].rsplit(": " + AFTER_RED, 1)[0]
    ruling(ctx, f"{step} accepted although {what}. Reason: {accept_red}", project)
    return _need(ctx, project)


def unit_set(ctx, uid: str, status: str, branch: str | None, commit: str | None,
             note: str | None, role: str | None = None, project: str | None = None,
             accept_red: str | None = None, agent: str | None = None) -> int:
    ledger = _need(ctx, project)
    unit = ledger.unit(uid)
    if unit is None:
        known = ", ".join(u["id"] for u in ledger.units) or "it has none yet"
        raise BuildError(f"the phase {ledger.phase} has no unit {uid} ({known})")
    if status not in STATUSES:
        raise BuildError(f"{status!r} is not a status: {', '.join(STATUSES)}")
    role = _code_role(ctx, role) if role else unit["role"]
    if status == "done" and branch:
        refusals, warnings = _handback(ctx, ledger, unit, branch)
        if refusals:
            raise BuildError(f"{branch} cannot be marked done for {uid}: " + "; ".join(refusals)
                             + " — nothing was changed")
        for w in warnings:
            print(f"warning: {w}", file=sys.stderr)
    if status == "done" and _full_gate(ctx) and _unit_gate(ctx) == "scoped":
        ledger = _gated(ctx, ledger, f"mark {uid} done", accept_red, project)
        unit = ledger.unit(uid)
    earlier, unit["role"] = unit["role"], role
    unit["status"] = status
    if status == "running":
        unit["attempts"] += 1
    for key, value in (("branch", branch), ("commit", commit), ("note", note), ("agent", agent)):
        if value is not None:
            unit[key] = _one_line(value)
    if status in ("failed", "blocked"):
        unit["agent"] = ""
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
        said += _copy_from_branch(ctx.root, branch, f"{BUILD_DIR}/{ledger.dir.name}/{uid}", False,
                                  state_root(ctx.root))
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


def _last_commit(root: Path, ref: str) -> str | None:
    """The committer date of `ref` (ISO 8601); None when git cannot say."""
    try:
        res = phase._run(root, "log", "-1", "--format=%cI", ref)
    except phase.PhaseError:
        return None
    text = phase._out(res)
    return text if res.returncode == 0 and text else None


def team_rows(root: Path) -> tuple[list[dict], list[BuildError]]:
    """One row per open build, local ledgers and fetched remote ones, one per `(phase, branch)`
    with the local ledger winning, sorted by phase then branch."""
    local, bad = ledgers(root)
    rows: dict[tuple[str, str], tuple[Ledger, str]] = {
        (g.phase, g.branch): (g, f"refs/heads/{g.branch}") for g in local}
    found, errors = team_ledgers(root)
    for ledger, ref in found:
        rows.setdefault((ledger.phase, ledger.branch), (ledger, ref))
    out = [{"phase": g.phase, "branch": g.branch, "owner": g.owner or None, "stage": g.stage,
            "done": sum(1 for u in g.units if u["status"] == "done"), "total": len(g.units),
            "last_commit": _last_commit(root, ref)}
           for _, (g, ref) in sorted(rows.items())]
    return out, [*bad, *errors]


def team_status(ctx, as_json: bool) -> int:
    rows, bad = team_rows(ctx.root)
    for exc in bad:
        print(f"warning: {exc}", file=sys.stderr)
    if as_json:
        _say([json.dumps({"builds": rows}, indent=2, ensure_ascii=False)])
    elif not rows:
        _say(["no builds in flight"])
    else:
        _say([f"{r['phase']}  {r['branch']}  {r['owner'] or '-'}  {r['stage']}  "
              f"{r['done']}/{r['total']}  {r['last_commit'] or '-'}" for r in rows])
    return 0


def status(ctx, as_json: bool, project: str | None = None, team: bool = False) -> int:
    if team:
        return team_status(ctx, as_json)
    branch = checked_out(ctx.root)
    ledger = current(ctx.root, branch) if branch else None
    if ledger is not None:
        ledger.project = ledger.project or project or ""
        ledger.folder = _folder(ctx, ledger.project)
    if as_json:
        _say([json.dumps(status_data(ctx.root, ledger, branch), indent=2, ensure_ascii=False)])
    else:
        _say(status_lines(ledger, planned_units(ctx.root, ledger)) if ledger else [nothing_in_flight(branch, project)])
    return 0


def review(ctx, project: str | None = None, accept_red: str | None = None) -> int:
    root = ctx.root
    ledger = _need(ctx, project)
    if ledger.visual:
        _need_ux_reviewer(ctx)
    if not ledger.units:
        raise BuildError(f"the phase {ledger.phase} has no units: there is nothing to review")
    waiting = [f"{u['id']} is {u['status']}" for u in ledger.units if u["status"] != "done"]
    if waiting:
        raise BuildError("not every unit is done: " + ", ".join(waiting))
    found = _ledger_ignored(root, ledger)
    if found is not None:
        raise _ignored_refusal(found, "run review again")
    if _full_gate(ctx):
        ledger = _gated(ctx, ledger, "start review", accept_red, project)
    folder = ledger.dir / "review"
    ledger.stage = "review"
    reviewer, qa = folder / "code-reviewer-brief.md", folder / "qa-engineer-brief.md"
    ux = folder / "ux-reviewer-brief.md"
    files = [(folder / "package.diff", package(root, ledger)),
             (folder / "package-stat.txt", package_stat(root, ledger)),
             (reviewer, code_reviewer_brief(root, ledger).encode("utf-8"))]
    lines = [f"wrote {_local(_base(root, path), path)}" for path, _ in files]
    if ledger.high_risk:
        if qa.exists():
            lines.append(f"kept {_local(_base(root, qa), qa)} as it is (delete it to have it written "
                         f"again)")
        else:
            files.append((qa, qa_engineer_brief(root, ledger).encode("utf-8")))
            lines.append(f"wrote {_local(_base(root, qa), qa)}")
    if ledger.visual:
        if ux.exists():
            lines.append(f"kept {_local(_base(root, ux), ux)} as it is (delete it to have it written "
                         f"again)")
        else:
            files.append((ux, ux_reviewer_brief(root, ledger).encode("utf-8")))
            lines.append(f"wrote {_local(_base(root, ux), ux)}")
    save(ledger, files)
    if ledger.high_risk:
        lines.append(f"fill in the definition of done in {_local(_base(root, qa), qa)}")
    if ledger.visual:
        lines.append(f"fill in what to launch and look at in {_local(_base(root, ux), ux)}")
    _say(lines + [_then("review", "the briefs that need it", ledger.project)])
    return 0


def _filled(brief: Path) -> bool:
    """Whether the coordinator has filled the brief in: no skeleton instruction line is left."""
    return not agent_runs.unfilled(brief.read_text(encoding="utf-8"))


def fix(ctx, role: str | None = None, project: str | None = None, unit: str | None = None) -> int:
    root = ctx.root
    ledger = _need(ctx, project)
    if unit is not None and ledger.unit(unit) is None:
        known = ", ".join(u["id"] for u in ledger.units) or "it has none yet"
        raise BuildError(f"the phase {ledger.phase} has no unit {unit} ({known})")
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
        raise BuildError(f"{_local(_base(root, brief), brief)} is not filled in yet: fill it in, or delete "
                         "it, before asking for another fix brief")
    ledger.stage = "fix"
    save(ledger, [(brief, fix_brief(ledger, role, number, _brief_lines(ctx), unit or "").encode("utf-8"))])
    _say([f"wrote {_local(_base(root, brief), brief)}", _then("fix", "its ranked list", ledger.project)])
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
            lines = [f"kept {_local(_base(root, brief), brief)}, with its rulings brought up to date"]
        else:
            save(ledger)
            lines = [f"kept {_local(_base(root, brief), brief)} as it is (delete it to have it written again)"]
    else:
        save(ledger, [(brief, close_brief(root, ledger).encode("utf-8"))])
        lines = [f"wrote {_local(_base(root, brief), brief)}"]
    folder = f"{BUILD_DIR}/{ledger.phase}"
    state = state_root(root)
    if state != root:
        where = _base(root, state)
        git = f"git -C {shlex.quote(_posix(where, state))}"
    else:
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
        raise BuildError(f"{_local(_base(root, brief), brief)} is not UTF-8") from None


def _fix_briefs(ledger: Ledger) -> list[Path]:
    """The fix briefs, in number order."""
    return sorted((ledger.dir / "fix").glob("brief*.md"), key=lambda p: (len(p.name), p.name))


def _fix_unit(text: str) -> str:
    """The unit a fix brief is for: its `Unit:` line, directly under the title; else empty."""
    lines = text.splitlines()
    return lines[1][len("Unit: "):].strip() if len(lines) > 1 and lines[1].startswith("Unit: ") else ""


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
        raise BuildError((f"{_local(_base(root, gone[0]), gone[0])} is not there" if gone else
                          f"there is no {what} brief")
                         + (f": run `{govern_build(ledger.project)} {first}` first" if first else
                            ": restore it from history"))
    return found


AGAIN = "every fix brief was launched before; this is the latest again:"


def _no_index(folder: Path) -> None:
    """Ask the desktop search indexer not to index `folder`; any failure is ignored."""
    try:
        (folder / ".metadata_never_index").touch()
    except OSError:
        pass


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
        _no_index(path.parent)
        res = phase._run(member, "worktree", "add", str(path), "-b", branch, head)
        if res.returncode != 0:
            raise BuildError(f"git worktree add {path} failed: {phase._reason(res)}")
    except phase.PhaseError as exc:
        raise BuildError(str(exc)) from None
    return path


def _short(ref: str) -> str:
    for prefix in ("refs/heads/", "refs/remotes/"):
        if ref.startswith(prefix):
            return ref[len(prefix):]
    return ref


def _default_ahead(root: Path) -> None:
    """Refuse when the default branch has commits HEAD lacks: an agent's worktree is cut from
    that branch, so it could not fast-forward to the base the launch names. A repository whose
    default branch cannot be told has nothing to compare."""
    try:
        _, refs = default_branch(root, phase._ask(root, "rev-parse", "--abbrev-ref", "HEAD"))
    except BuildError:
        return
    for ref in refs:
        if not phase._yes(root, "merge-base", "--is-ancestor", ref, "HEAD"):
            short = _short(ref)
            raise BuildError(f"{short} has commits this phase branch does not: agent worktrees "
                             f"are cut from it and cannot fast-forward to Base. Run git merge "
                             f"--no-edit {short}, commit, then launch again.")


def _retry_base(root: Path, ledger: Ledger, unit: dict, head: str) -> str:
    """Where a retried unit's agent starts: its own branch with HEAD merged in, so the first
    attempt's commits are there. HEAD when the unit has no branch here; the branch's tip when
    it already has HEAD, and HEAD when the branch holds nothing HEAD lacks."""
    branch = unit["branch"]
    ref = f"refs/heads/{branch}"
    if not branch or not phase._yes(root, "rev-parse", "--verify", "--quiet", ref):
        return head
    if phase._yes(root, "merge-base", "--is-ancestor", "HEAD", ref):
        return phase._ask(root, "rev-parse", ref)
    if phase._yes(root, "merge-base", "--is-ancestor", ref, "HEAD"):
        return head           # nothing the attempt made that HEAD lacks: no merge, ref unmoved
    refusal = BuildError(f"{branch} does not merge cleanly with HEAD: merge it by hand, then "
                         "launch again.")
    tip = phase._ask(root, "rev-parse", ref)
    tree = phase._worktrees(root).get(branch)
    if tree is not None:
        if phase._ask(Path(tree), "status", "--porcelain"):
            raise refusal
        if phase._run(Path(tree), "merge", "--no-edit", head).returncode != 0:
            phase._run(Path(tree), "merge", "--abort")
            raise refusal
        return phase._ask(root, "rev-parse", ref)
    res = phase._run(root, "merge-tree", "--write-tree", ref, "HEAD")
    if res.returncode != 0:
        raise refusal
    merged = phase._out(res).splitlines()[0]
    new = phase._ask(root, "commit-tree", merged, "-p", tip, "-p", head, "-m",
                     f"Merge {ledger.branch} into {branch} for attempt {unit['attempts'] + 1}")
    phase._ask(root, "update-ref", ref, new, tip)
    return new


def _resumes(root: Path, ledger: Ledger, found: list[tuple[str, Path]]) -> dict[Path, str]:
    """The fix briefs whose unit's builder is resumed, each with that agent: the unit is done and
    has an agent, and the brief is among the first 3 fix briefs that name the unit."""
    seen: dict[str, int] = {}
    out = {}
    for path in _fix_briefs(ledger):
        uid = _fix_unit(_text(root, path))
        unit = ledger.unit(uid) if uid else None
        if unit is None:
            continue
        seen[uid] = seen.get(uid, 0) + 1
        if seen[uid] <= 3 and unit["agent"] and unit["status"] == "done":
            out[path] = unit["agent"]
    return {brief: out[brief] for _, brief in found if brief in out}


def launch(ctx, what: str, project: str | None = None, session: Path | None = None) -> int:
    """Check every brief `what` covers (filled in, committed at HEAD), mark a unit running, and
    print each launch prompt: the role, the brief's path from the repository root, and for a role
    that works in a worktree the commit that worktree must start from. In a workspace (`project`)
    that worktree is made here, inside the member's repository, and named on a `Worktree:` line;
    a role that writes nothing is told the member's folder. `session` is the governance root."""
    root = ctx.root
    ledger = _need(ctx, project)
    found = _covered(root, ledger, what)
    ignored = _ledger_ignored(root, ledger)
    if ignored is not None:
        raise _ignored_refusal(ignored, "run launch again")
    state = state_root(root)
    moved = state != root
    # The brief's state is in the workspace's repository when `build_root` moved it.
    where = _state_repo(state) if moved else root
    for _, brief in found:
        shown = _local(_base(root, brief), brief)
        left = agent_runs.unfilled(_text(root, brief))
        if left:
            raise BuildError(f"{shown}: the brief is not filled in ({', '.join(left)})")
        # Untracked, staged or changed since HEAD: anything but clean (--ignored, so that an
        # untracked file git ignores is not read as clean).
        if phase._ask(where, "status", "--porcelain", "--ignored", "--", str(brief)):
            raise BuildError(f"{shown} is not committed at HEAD: commit it, then launch")
    if not project:
        _default_ahead(root)
    head = phase._ask(root, "rev-parse", "HEAD")
    unit = ledger.unit(what) if what not in RESERVED else None
    base = _retry_base(root, ledger, unit, head) if unit is not None else head
    prefix = phase._ask(root, "rev-parse", "--show-prefix")
    lines = []
    again = what == "fix" and all(p.name in ledger.launched for p in _fix_briefs(ledger))
    if again:
        lines.append(AGAIN)
    resume = _resumes(root, ledger, found) if what == "fix" else {}
    for role, brief in found:
        if brief in resume:
            lines += [f"resume {resume[brief]} with SendMessage, the prompt:", f"Resume: {resume[brief]}",
                      launch_line(str(brief) if moved else prefix + _posix(root, brief))]
        else:
            lines += [f"launch {role} with the prompt:",
                      launch_line(str(brief) if moved else prefix + _posix(root, brief))]
        if role in WORKTREE_ROLES:
            if (root / ".claude" / "worktrees").is_dir():
                _no_index(root / ".claude" / "worktrees")
            lines += [f"Base: {base}", f"Phase branch: {ledger.branch}"]
            if project:
                uid = what if what not in RESERVED else brief.parent.name \
                    + ("-" + brief.stem if what == "fix" else "")
                tree = _agent_tree(root, session or root, project, ledger.dir.name, uid)
                lines += [f"Worktree: {tree}",
                          "launch it without isolation: the worktree is made"]
        elif project:
            lines.append(f"Worktree: {root}")
    if unit is not None:
        unit["status"] = "running"
        unit["attempts"] += 1
        unit["base"] = base
        unit["agent"] = ""
        save(ledger)
    elif what == "fix" and not again:
        ledger.launched += [brief.name for _, brief in found]
        save(ledger)
    _say(lines)
    return 0


def _owned(root: Path, take_over: bool) -> None:
    """Refuse a change to a phase another person started, unless they take it over. Nothing is
    said when the ledger has no owner, git has no email, or no build is in flight."""
    branch = checked_out(root)
    ledger = current(root, branch) if branch else None
    email = git_email(root)
    if ledger is None or not ledger.owner or email is None or ledger.owner.lower() == email.lower():
        return
    if not take_over:
        raise BuildError(f"{ledger.phase} belongs to {ledger.owner}; run with --take-over to "
                         f"take it")
    old = ledger.owner
    today = time.strftime("%Y-%m-%d", time.gmtime())
    ledger.rulings.append(f"taken over from {old} on {today}")
    ledger.owner = email
    save(ledger)
    _say([f"took over {ledger.phase} from {old}"])


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
            if getattr(args, "team", False):
                _say([json.dumps({"builds": []}, indent=2)] if args.as_json
                     else ["no builds in flight"])
                return 0
            # Nothing to report is not a refusal: the same exit `status` has in one repository.
            data = {**status_data(ctx.root, None, ""), "next": str(exc)}
            _say([json.dumps(data, indent=2, ensure_ascii=False)] if args.as_json else [str(exc)])
            return 0
        session, ctx = ctx.root, replace(ctx, root=root)
        if args.build_cmd not in ("status", "start"):
            _owned(root, getattr(args, "take_over", False))
        if args.build_cmd == "start":
            return start(ctx, args.phase, args.plan, args.high_risk, args.source, args.visual,
                         project, session, getattr(args, "take_over", False))
        if args.build_cmd == "unit":
            if args.unit_cmd == "add":
                return unit_add(ctx, args.id, args.title, args.role, args.red, project)
            if args.unit_cmd == "drop":
                return unit_drop(ctx, args.id, project)
            return unit_set(ctx, args.id, args.status, args.branch, args.commit, args.note,
                            args.role, project, args.accept_red, args.agent)
        if args.build_cmd == "launch":
            return launch(ctx, args.what, project, session)
        if args.build_cmd == "collect":
            return collect(ctx, args.branch, project)
        if args.build_cmd == "verify":
            return verify(ctx, args.id, args.branch, project)
        if args.build_cmd == "gate":
            return gate(ctx, args.gate_cmd, project, session)
        if args.build_cmd == "ruling":
            return ruling(ctx, " ".join(args.text), project)
        if args.build_cmd == "status":
            return status(ctx, args.as_json, project, args.team)
        if args.build_cmd == "review":
            return review(ctx, project, args.accept_red)
        if args.build_cmd == "fix":
            return fix(ctx, args.role, project, args.unit)
        return close(ctx, project)
    except (BuildError, phase.PhaseError) as exc:
        print(f"error: {exc}", file=sys.stderr)
    except OSError as exc:
        where = f" at {exc.filename}" if exc.filename else ""
        print(f"error: stopped{where}: {exc.strerror or exc}; anything written before it "
              "stays (python3 .context-gate/bin/govern build status shows it)", file=sys.stderr)
    return 1
