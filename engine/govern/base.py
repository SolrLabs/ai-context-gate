"""What a governed file held at a commit, and the two sides of a comparison against one.

Every check but one reads the state of the tree. A change-aware one compares the tree with a
*base*:

- on an ordinary run, `HEAD` of the repository holding each file: uncommitted work against the
  last commit;
- under `check --base <ref>`, the commit the branch left `<ref>` at (`merge-base <ref> HEAD`),
  so what landed on `<ref>` after the branch began is not blamed on the branch. The tip of
  `<ref>` is read too, for what the other side added since.

The governance root is not always the git top, and a project may be its own nested repository,
so every question is asked per repository (`context.repo_of`), with git-relative paths.

`context.git()` cannot do the read itself: it answers `None` both for "the file is not in that
commit" (a legal new log) and for a failure, and it decodes with `errors="replace"`. The calls
here keep those apart, with the same environment stripping and timeout. Whether a file is in a
commit is read off the commit's tree (`files`), never off a failed read: git exits the same way
for a blob that is not there and for one it could not produce, and only the first is a new log.
A log the commit holds under another name is still that log: it is found by the rename git
reads, or, when so much changed that git reads none, by the ids it holds (`_Asked.was_in`).

When a comparison cannot be made (no git, not a repository, no commit yet, an unknown ref, no
shared history as in a shallow clone, a base file git cannot read or that is not UTF-8), the
repository is returned as not compared, with the reason. What that means is the caller's to
decide: nothing here raises on it, prints, or guesses.
"""
from __future__ import annotations

import copy
import dataclasses
import re
import subprocess
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from govern.context import GIT_TIMEOUT, git, git_env, glob_matches, repo_of
from govern.decisions import Entry, mask_lines
from govern.registry import Scope
from govern.text import Markers, Unreadable
from govern.text import read as read_file

DECISION, TRAP = "decision", "trap"

# How a shallow clone's reason ends: the one fix there is, in the words a CI checkout takes it.
SHALLOW_FIX = "fetch the full history (fetch-depth: 0)"


class CannotCompare(Exception):
    """Why something could not be compared with its base. `reason` is a whole clause, written
    to follow "could not compare with <ref> — " in a finding and to sit inside "not compared
    (...)" in a report."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------- one repository

def _git(repo: Path, *args: str) -> subprocess.CompletedProcess | None:
    """One git call for its exit code and its bytes, neither of which `context.git()` gives —
    or None when git itself could not run (missing, or timed out). The same `git_env()` and
    timeout as every other git call the engine makes."""
    try:
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                              timeout=GIT_TIMEOUT, env=git_env())
    except (OSError, subprocess.SubprocessError):
        return None


def _usable(ref: str) -> bool:
    """A ref git can be handed: not empty, and not one it would read as an option."""
    return bool(ref) and not ref.startswith("-")


def files(repo: Path, commit: str, directory: str) -> list[str]:
    """The files `commit` holds directly in `directory` (git-relative, `""` for the top), each
    by its whole git-relative name: `ls-tree -z`, so a name outside ASCII comes back as itself,
    not quoted. An empty list when the commit has no such directory. Raises `CannotCompare`
    when git could not list it: a failure is never an empty directory."""
    res = _git(repo, "ls-tree", "-z", commit, *(["--", directory + "/"] if directory else []))
    if res is None or res.returncode != 0:
        raise CannotCompare(f"git could not list {directory or 'the top directory'} "
                            f"at {commit[:12]}")
    found = []
    for line in res.stdout.decode("utf-8", errors="replace").split("\0"):
        meta, tab, name = line.partition("\t")
        if tab and meta.split()[1:2] == ["blob"]:
            found.append(name)
    return found


def exists(repo: Path, commit: str, rel: str) -> bool | None:
    """Whether `commit` holds a file at `rel` (git-relative, forward slashes), read off its
    tree (`files`). None when git could not say, whatever the cause: a file that is not in the
    commit is a legal new one, and a failure is never that."""
    try:
        return rel in files(repo, commit, rel.rpartition("/")[0])
    except CannotCompare:
        return None


def since(repo: Path, commit: str) -> tuple[dict[str, str], list[str]]:
    """`(renamed, deleted)` between `commit` and the working tree, from one question:
    `{the name now: the name at commit}` for every file git reads as renamed, and the name of
    every file it reads as deleted, a file moved without telling git among them.
    `diff -M --name-status -z <commit>`, which sees what is committed since and what is staged,
    not a file git does not track yet. With `diff.autoRefreshIndex` off, so asking never writes
    the index (git otherwise takes the chance to refresh it, whatever `--no-optional-locks`
    says); a file whose content is unchanged may then be listed as modified, which is neither
    and is not read here. Raises `CannotCompare` when git could not say."""
    res = _git(repo, "-c", "diff.autoRefreshIndex=false", "diff", "-M", "--name-status", "-z",
               commit)
    if res is None or res.returncode != 0:
        raise CannotCompare(f"git could not say what was renamed since {commit[:12]}")
    fields = iter(res.stdout.decode("utf-8", errors="replace").split("\0"))
    renamed, deleted = {}, []
    for status in fields:
        # A rename or a copy is followed by two names, anything else by one.
        old = next(fields, "")
        if status[:1] in ("R", "C"):
            new = next(fields, "")
            if status[:1] == "R":
                renamed[new] = old
        elif status[:1] == "D":
            deleted.append(old)
    return renamed, deleted


def renames(repo: Path, commit: str) -> dict[str, str]:
    """`{the name now: the name at commit}` for every file git reads as renamed between
    `commit` and the working tree (`since`). Raises `CannotCompare` when git could not say."""
    return since(repo, commit)[0]


def read(repo: Path, commit: str, rel: str) -> str:
    """The text `rel` held at `commit`, from the blob's bytes, decoded strictly as UTF-8 with a
    BOM dropped — what `text.read` gives for a file on disk. Raises `CannotCompare` when git
    cannot produce the blob or it is not UTF-8: never an empty string, which would read as a
    log with no entries."""
    res = _git(repo, "cat-file", "blob", f"{commit}:{rel}")
    if res is None or res.returncode != 0:
        raise CannotCompare(f"git could not read {rel} at {commit[:12]}")
    try:
        text = res.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CannotCompare(f"{rel} at {commit[:12]} is not valid UTF-8 "
                            f"(byte {exc.start})") from exc
    return text.removeprefix("\ufeff")


def resolve(repo: Path, ref: str) -> str | None:
    """The commit `ref` names in `repo`, as a full id: `rev-parse --verify <ref>^{commit}`.
    None when it names none there, or git failed."""
    if not _usable(ref):
        return None
    return git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}") or None


def merge_base(repo: Path, ref: str) -> str | None:
    """Where `HEAD` left `ref`: `merge-base <ref> HEAD`. None when the two share no commit this
    clone holds (a shallow one, or unrelated histories), or git failed."""
    if not _usable(ref):
        return None
    out = git(repo, "merge-base", ref, "HEAD")
    return out.splitlines()[0] if out else None


def ls(repo: Path, commit: str, directory: str) -> list[str] | None:
    """Every file `commit` holds under `directory`, at any depth, git-relative: `ls-tree -r
    --name-only`
    (with `-z`, so a name outside ASCII comes back as itself, not quoted). An empty list when
    the commit has no such directory; None when git failed."""
    out = git(repo, "ls-tree", "-r", "--name-only", "-z", commit, "--", directory or ".")
    return None if out is None else [name for name in out.split("\0") if name]


def _no_head(repo: Path) -> str:
    """Why `HEAD` names no commit in `repo`."""
    if git(repo, "--version") is None:
        return "git is not available"
    if git(repo, "rev-parse", "--git-dir") is None:
        return "git could not read the repository"
    return "the repository has no commit yet"


def _commits(repo: Path, ref: str | None) -> tuple[str, str | None]:
    """`(the base commit, the tip of ref)` for one repository: `HEAD` and None with no `ref`,
    else the merge base and the commit `ref` names. Raises `CannotCompare`."""
    head = resolve(repo, "HEAD")
    if head is None:
        raise CannotCompare(_no_head(repo))
    if ref is None:
        return head, None
    shallow = git(repo, "rev-parse", "--is-shallow-repository") == "true"
    tip = resolve(repo, ref)
    if tip is None:
        raise CannotCompare(f"{ref} names no commit in this shallow clone: {SHALLOW_FIX}"
                            if shallow else f"{ref} names no commit in this repository")
    common = merge_base(repo, tip)
    if common is None:
        raise CannotCompare(f"HEAD and {ref} share no commit in this shallow clone: "
                            f"{SHALLOW_FIX}" if shallow else f"HEAD and {ref} share no history")
    return common, tip


# ---------------------------------------------------------------------------- the two sides

@dataclass
class Item:
    """One entry of one decision log or trap file, on one side of a comparison."""
    kind: str          # DECISION or TRAP
    log: str           # its file, as a finding names it (`Context.rel`)
    scope: Scope       # the scope whose file that is (`Context.owner` for the workspace's log)
    entry: Entry

    @property
    def key(self) -> tuple:
        """What matches it to the other side. A decision id is one namespace across every log
        of a workspace (the registry's ranges never overlap), so a decision that moves between
        logs is still the same decision; a trap id is one namespace per project. The prefix
        counts in any case, as it does wherever the engine reads an id (`decisions.id_key`)."""
        ident = (self.entry.prefix.upper(), self.entry.num)     # `p-1` is P-1
        return (TRAP, self.scope.name, *ident) if self.kind == TRAP else (DECISION, *ident)


@dataclass
class Repo:
    """One repository's part of a comparison, or the files that are in none."""
    path: Path | None            # the repository's top; None for files in no repository
    label: str                   # `.` for the one holding the governance root, else its path
    root: bool                   # whether it holds the governance root
    commit: str | None = None    # the base commit compared with; None when not compared
    tip: str | None = None       # the commit `--base <ref>` names; None without `--base`
    reason: str | None = None    # why it was not compared
    # False for a `--path` snapshot given no `--history-from`: there was no history to ask.
    # Nothing failed, so an ordinary run and a report say nothing of it; a gate does.
    history: bool = True
    scopes: set[str] = field(default_factory=set)       # the scopes with a file here
    base: list[Item] = field(default_factory=list)
    current: list[Item] = field(default_factory=list)
    theirs: list[Item] = field(default_factory=list)    # at `tip`

    def fail(self, reason: str) -> None:
        """Not compared after all: a repository is compared whole or not at all, so one file
        that cannot be read never leaves half an answer."""
        if self.commit is not None or self.reason is None:
            self.reason = reason
        self.commit = self.tip = None
        self.base, self.theirs = [], []


@dataclass
class Comparison:
    ref: str | None              # `--base`, as given; None when the base is `HEAD`
    repos: list[Repo]
    # False when a file on disk could not be read: the current side has a hole in it, so an id
    # that is missing from it may only be out of sight.
    complete: bool = True

    def side(self, name: str, kind: str | None = None) -> list[Item]:
        """Every item on one side (`base`, `current` or `theirs`), across the repositories."""
        return [item for repo in self.repos for item in getattr(repo, name)
                if kind is None or item.kind == kind]

    @property
    def not_compared(self) -> list[Repo]:
        """The repositories a comparison was asked of and could not be made in."""
        return [repo for repo in self.repos if repo.commit is None and repo.history]

    @property
    def without_base(self) -> list[Repo]:
        """Every repository with nothing to compare its files against: `not_compared`, and a
        `--path` snapshot given no `--history-from`."""
        return [repo for repo in self.repos if repo.commit is None]


@dataclass(frozen=True)
class _Source:
    """One decision log, or one project's trap files."""
    kind: str
    scope: Scope       # whose it is
    here: Scope        # that scope as this run reads it: under `--path`, from the snapshot
    path: Path         # the log, or the working directory the trap files are in
    home: Path         # the directory it hangs off, to name files that are in no repository


_NO_HISTORY = object()

# How many deleted Markdown files are read at the base to find the log a current one was. More
# than that were deleted, and the repository is not compared: nothing says none of them was it.
DELETED_READ = 50

# Why a `--path` snapshot given no `--history-from` has no base, as a gate says it.
NO_HISTORY = "a snapshot needs --history-from to be compared"


class _Asked:
    """What one comparison has asked git, so nothing is asked twice: a directory's listing and
    a file's text at a commit (the base and the tip are one commit once a branch has merged
    its base in), and, once per repository, what was renamed and what was deleted since its
    base."""

    def __init__(self, ctx) -> None:
        # The project's grammar, reading an id's prefix in any case: `## p-1` is P-1, so the
        # entry under it is P-1's and not a decision that went missing. That such a heading is
        # malformed is `decision-log`'s to say.
        self.grammar = copy.copy(ctx.grammar)
        self.grammar.entry_re = re.compile(ctx.grammar.entry_re.pattern,
                                           ctx.grammar.entry_re.flags | re.IGNORECASE)
        self.trap_prefix = ctx.trap_prefix.upper()
        self._files: dict[tuple, list[str]] = {}
        self._texts: dict[tuple, str] = {}
        self._since: dict[tuple, tuple[dict[str, str], list[str]]] = {}
        self._below: dict[tuple, list[str]] = {}
        # Every decision log of the comparison, as `(repository, its path there with its case
        # folded)`: a deleted file that is one is read by name, as that log, and only so.
        self.logs: set[tuple] = set()
        self._was_in: dict[tuple, list[str]] = {}
        self._taken: set[tuple] = set()

    def entries(self, kind: str, text: str) -> list[Entry]:
        return [e for e in self.grammar.parse(text).entries
                if kind == DECISION or e.prefix.upper() == self.trap_prefix]

    def text(self, repo: Path, commit: str, name: str) -> str:
        key = (repo, commit, name)
        if key not in self._texts:
            self._texts[key] = read(repo, commit, name)
        return self._texts[key]

    def below(self, repo: Path, commit: str, directory: str) -> list[str]:
        """Every file `commit` holds under `directory` (`ls`). Raises `CannotCompare`."""
        key = (repo, commit, directory)
        if key not in self._below:
            listed = ls(*key)
            if listed is None:
                raise CannotCompare(f"git could not list {directory} at {commit[:12]}")
            self._below[key] = listed
        return self._below[key]

    def held(self, repo: Path, commit: str, rel: str) -> str | None:
        """The name `commit` holds the file at `rel` under: `rel`, or the one name in its
        directory that equals it ignoring case (which of the two the working copy uses is the
        file system's business). None when the commit has no such file. Raises `CannotCompare`
        when git could not list the directory, or two names there fit."""
        key = (repo, commit, rel.rpartition("/")[0])
        if key not in self._files:
            self._files[key] = files(*key)
        if rel in self._files[key]:
            return rel
        same = [name for name in self._files[key] if name.casefold() == rel.casefold()]
        if len(same) > 1:
            raise CannotCompare(f"{commit[:12]} holds {rel} under {len(same)} names that differ "
                                f"only in case")
        return same[0] if same else None

    def renamed_from(self, repo: Path, commit: str, rel: str) -> str | None:
        """The name the file now at `rel` had at `commit`, when git reads it as renamed since.
        Raises `CannotCompare` when git could not say."""
        found, _ = self._changed(repo, commit)
        if rel in found:
            return found[rel]
        same = [old for new, old in found.items() if new.casefold() == rel.casefold()]
        return same[0] if len(same) == 1 else None

    def _changed(self, repo: Path, commit: str) -> tuple[dict[str, str], list[str]]:
        key = (repo, commit)
        if key not in self._since:
            self._since[key] = since(repo, commit)
        return self._since[key]

    def was_in(self, repo: Path, commit: str, rel: str, ids: set[tuple]) -> list[str]:
        """The files a log may have been that is on disk at `rel`, is not in `commit` and is no
        rename git reads: every Markdown file deleted since `commit` that held, there, an entry
        with one of `ids` (the ids the log holds now, each a prefix in upper case and a
        number). A log renamed and rewritten in one change is one file deleted and another
        added, as far as git can tell; the ids it kept are what says it is the same log. Every
        file that fits is one, each for one log only: entries are matched by id across logs,
        so which log found it changes nothing.

        A deleted file that is itself a log of the comparison is not looked at: it is read by
        name. The same question git was asked about renames answers this one, so the cost is
        one read for each deleted Markdown file, and none when nothing was deleted. Raises
        `CannotCompare` when a file cannot be read, or more than `DELETED_READ` would be."""
        key = (repo, commit, rel)
        if key in self._was_in:
            return self._was_in[key]
        _, deleted = self._changed(repo, commit)
        names = sorted(name for name in deleted if name.lower().endswith(".md")
                       and (repo, name.casefold()) not in self.logs) if ids else []
        if len(names) > DELETED_READ:
            raise CannotCompare(
                f"{rel} is not at {commit[:12]}, and {len(names)} Markdown files were deleted "
                f"since: more than the {DELETED_READ} that are read to find the log it was")
        found = []
        for name in names:
            if (repo, name) in self._taken:
                continue
            held = {(e.prefix.upper(), e.num)
                    for e in self.entries(DECISION, self.text(repo, commit, name))}
            if held & ids:
                found.append(name)
                self._taken.add((repo, name))
        self._was_in[key] = found
        return found


def _sources(ctx, traps: bool) -> list[_Source]:
    """Every decision log of the workspace, each once however many scopes name it, and (with
    `traps`) every governed project's trap files. The workspace's own log comes first."""
    out: list[_Source] = []
    seen: set[Path] = set()
    for scope in ctx.registry.scopes:
        if not scope.governed:
            continue
        here = scope
        if ctx.snapshot is not None and scope.gov == ctx.real_scope_dir:
            here = dataclasses.replace(scope, gov=ctx.snapshot)
        log = ctx.scope_log(here)
        real = ctx.real_path(log).resolve()
        if real not in seen:
            seen.add(real)
            out.append(_Source(DECISION, scope, here, log, here.gov))
        if traps:
            out.append(_Source(TRAP, scope, here, ctx.working_dir(here), here.gov))
    log = ctx.workspace_log
    if log is not None and log.resolve() not in seen:
        owner = ctx.owner(log)
        out.insert(0, _Source(DECISION, owner, owner, log, log.parent))
    return out


def _where(ctx, path: Path):
    """`(repository, git-relative path)` for a file, `None` when it is in no repository, or
    `_NO_HISTORY` for a file of a `--path` snapshot given no `--history-from`.

    A snapshot file is asked about where the repository tracks it: its place in the real tree
    (`Context.real_path`), in the repository that holds it there, whichever directory of the
    repository `--history-from` names. A hook names the repository's top, and the project may
    live anywhere below it, in a repository of its own included. Only when `--history-from`'s
    repository does not hold the project's directory at all (a clone of it somewhere else) is
    the file the same relative path in that checkout."""
    rel = ctx._snapshot_rel(path)
    if rel is None:
        return ctx._repo_rel(path)
    if ctx.history_from is None:
        return _NO_HISTORY
    history = ctx.history_from.resolve()
    top = history if (history / ".git").exists() else repo_of(history)
    real = ctx.real_path(path).resolve()
    if ctx.real_scope_dir is not None and top is not None and real.is_relative_to(top):
        return ctx._repo_rel(real)
    return ctx._repo_rel(ctx.history_from / rel)


def _label(root: Path, repo: Path) -> str:
    """A repository's name in a message: its path from the governance root (both resolved, as
    `repo_of` found it), or its whole path when it is not under the root."""
    try:
        return repo.relative_to(root).as_posix()
    except ValueError:
        return repo.as_posix()


def _named(ctx, repo: Path, name: str) -> str:
    """A file of a repository that is no longer on disk, as a finding names it: its path from
    the governance root, or its whole path when it was not under the root (`Context.rel`)."""
    return _label(ctx.root.resolve(), repo / name)


def _on_disk(ctx, asked: _Asked, src: _Source) -> list[Item]:
    """A source's entries as the tree holds them now. Raises `Unreadable`."""
    if src.kind == DECISION:
        paths = [src.path] if src.path.exists() else []
    else:
        paths = ctx.trap_files(src.here)
    return [Item(src.kind, ctx.rel(path), src.scope, e)
            for path in paths for e in asked.entries(src.kind, read_file(path))]


def _at(ctx, asked: _Asked, repo: Repo, commit: str, src: _Source, rel: str,
        now: list[Item]) -> list[Item]:
    """A source's entries as `commit` (the repository's base, or its tip) holds them, parsed
    with the current grammar: a heading it rejects is not an entry, so it is absent, never an
    error. `now` is what the source holds on disk. Raises `CannotCompare`.

    A log is absent only when the commit's tree does not list it, under its name or that name
    in another case. One that is on disk now and absent is new, or renamed. When git reads it
    as renamed since the base, it is read from the name it had, as the same log. When git
    reads no rename and the base has no file for it, it is read from each Markdown file
    deleted since the base that held one of its ids (`_Asked.was_in`), and those entries are
    where they were: in that file, and its owner's (`Context.owner`). The log was renamed and
    rewritten in one change, or took over another file's entries. Either way an edit made in
    the same change is compared like any other. Everything a tree lists is read, and a read
    that fails is a failure."""
    here = (ctx.rel(src.path), src.scope)
    if src.kind == DECISION:
        name = asked.held(repo.path, commit, rel)
        names = {name: here} if name is not None else {}
        if name is None and src.path.exists():
            old = asked.renamed_from(repo.path, repo.commit, rel)
            if old is not None:
                name = asked.held(repo.path, commit, old)
                names = {name: here} if name is not None else {}
            elif asked.held(repo.path, repo.commit, rel) is None:
                ids = {(item.entry.prefix.upper(), item.entry.num) for item in now}
                for was in asked.was_in(repo.path, repo.commit, rel, ids):
                    # Deleted since the base, so the base holds it; the tip may not.
                    name = was if commit == repo.commit else asked.held(repo.path, commit, was)
                    if name is not None:
                        names[name] = (_named(ctx, repo.path, was),
                                       ctx.owner(repo.path / was))
    else:
        prefix = "" if rel == "." else rel + "/"
        pattern = ctx.projects("trap_glob", "traps*.md")
        names = {name: (ctx.rel(src.path / name[len(prefix):]), src.scope)
                 for name in sorted(asked.below(repo.path, commit, rel))
                 if name.startswith(prefix) and glob_matches(pattern, name[len(prefix):])}
    return [Item(src.kind, log, scope, e)
            for name, (log, scope) in names.items()
            for e in asked.entries(src.kind, asked.text(repo.path, commit, name))]


def compare(ctx, traps: bool = True) -> Comparison:
    """Both sides of every decision log and (with `traps`) trap file of the workspace, per
    repository: the entries at the base, the entries in the tree now and, under `--base`, the
    entries at the tip of that ref — and each repository that could not be compared, with why
    (`Comparison.not_compared`).

    The base is `HEAD` of each repository, or with `ctx.base` set the merge base of that ref and
    `HEAD`, resolved per repository. Under `--path` a snapshot file's base is the project's own
    file, in the repository `--history-from` is in (`_where`); with no `--history-from` that
    scope is not compared (`Comparison.without_base`), which only a gate calls a failure. The
    repository holding the governance root is always first, whether or not a log lives in it.

    It is the whole workspace whatever the run is restricted to: an id is matched across every
    log, so one project's entries cannot be judged without knowing where the others' are."""
    root = ctx.root.resolve()
    top = root if (root / ".git").exists() else repo_of(root)
    home = Repo(path=top, label=".", root=True,
                reason=None if top is not None else "not a git repository")
    repos: dict[object, Repo] = {top: home}
    placed: list[tuple[_Source, Repo, str | None]] = []
    for src in _sources(ctx, traps):
        # A trap source is a directory, and a directory that is itself a repository's top
        # belongs to the repository around it (`repo_of`): ask about a file inside it.
        found = _where(ctx, src.path if src.kind == DECISION else src.path / "-")
        rel = None
        if found is _NO_HISTORY:
            repo = repos.setdefault(("snapshot", src.home), Repo(
                path=None, label=ctx.rel(src.home), root=False, history=False,
                reason=NO_HISTORY))
        elif found is None and top is None:
            repo = home
        elif found is None:
            repo = repos.setdefault(("none", src.home), Repo(
                path=None, label=ctx.rel(src.home), root=False,
                reason="not a git repository"))
        else:
            where, rel = found
            if src.kind == TRAP:
                rel = rel.rpartition("/")[0] or "."
            repo = repos.setdefault(where, Repo(path=where, label=_label(root, where),
                                                root=False))
        repo.scopes.add(src.scope.name)
        placed.append((src, repo, rel))

    for repo in repos.values():
        if repo.path is None:
            continue
        try:
            repo.commit, repo.tip = _commits(repo.path, ctx.base)
        except CannotCompare as exc:
            repo.reason = exc.reason

    complete = True
    asked = _Asked(ctx)
    asked.logs = {(repo.path, rel.casefold()) for src, repo, rel in placed
                  if src.kind == DECISION and rel is not None}
    for src, repo, rel in placed:
        try:
            now = _on_disk(ctx, asked, src)
        except Unreadable as exc:
            complete = False
            repo.fail(f"{ctx.rel(exc.path)} is {exc.reason}")
            continue
        repo.current += now
        if repo.commit is None or rel is None:
            continue
        try:
            was = _at(ctx, asked, repo, repo.commit, src, rel, now)
            theirs = (_at(ctx, asked, repo, repo.tip, src, rel, now)
                      if repo.tip is not None else [])
        except CannotCompare as exc:
            repo.fail(exc.reason)
            continue
        repo.base += was
        repo.theirs += theirs
    return Comparison(ref=ctx.base, repos=list(repos.values()), complete=complete)


# ---------------------------------------------------------------------------- what counts as a change

TOPIC_LINE_RE = re.compile(r"^\*\*Topic:\*\*")
FIELD_RE = re.compile(r"\*\*[^*\n]+:\*\*")
REVISED_LINE_RE = re.compile(r"^\s*\*\*Revised:\*\*\s*(\d{4}-\d{2}-\d{2})(?!\d)")


def _live_lines(entry: Entry, markers: Markers) -> list[tuple[str, bool]]:
    """The body's lines, line endings normalized, each with whether it is live markdown: a
    line inside a code fence or an HTML comment is an example, not a field."""
    lines = entry.body.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return list(zip(lines, mask_lines(lines, markers)))


def _topic_only(line: str) -> bool:
    """Whether a line is the topic field and nothing else. One that goes on to another field
    (`**Topic:** X. **Rule:** ...`) says more than where the entry is filed."""
    m = TOPIC_LINE_RE.match(line)
    return m is not None and FIELD_RE.search(line, m.end()) is None


def compared_text(entry: Entry, markers: Markers) -> str:
    """What an entry says, for telling whether it changed: its title and body with the
    `**Topic:**` line removed, line endings normalized and every run of whitespace collapsed.
    Not the heading line, so the dash is not text; generated blocks are already out of the body.
    Re-wrapping a paragraph, changing the dash, filing the entry under another topic or
    `index` re-rendering a block is therefore not a change. Only a line that is the topic and
    nothing else is left out: one carrying another field after it is text."""
    kept = [line for line, live in _live_lines(entry, markers)
            if not (live and _topic_only(line))]
    return " ".join(f"{entry.title}\n{chr(10).join(kept)}".split())


def revised_lines(entry: Entry, markers: Markers) -> list[tuple[str, str]]:
    """An entry's `**Revised:** YYYY-MM-DD ...` lines, each as `(the date as written, the line
    with its whitespace collapsed)`, in order. A line without a date in that shape is not one."""
    out = []
    for line, live in _live_lines(entry, markers):
        m = REVISED_LINE_RE.match(line) if live else None
        if m:
            out.append((m.group(1), " ".join(line.split())))
    return out


def _real(day: str) -> bool:
    try:
        date.fromisoformat(day)
    except ValueError:
        return False
    return True


def dated_revision(was: Entry, now: Entry, markers: Markers) -> bool:
    """Whether the change from `was` to `now` says so: a `**Revised:**` line was added or its
    text changed, and the newest date now is a real date not older than the newest real one
    before. Removing a line, or adding one dated 2026-02-30, does not."""
    before, after = revised_lines(was, markers), revised_lines(now, markers)
    known = {line for _, line in before}
    if all(line in known for _, line in after):
        return False
    newest = max(day for day, _ in after)
    earlier = [day for day, _ in before if _real(day)]
    return _real(newest) and (not earlier or newest >= max(earlier))


@dataclass
class Pairs:
    """What `match` found. It unpacks as `(matched, removed, added)`, the three a caller
    that says nothing of an ambiguous id needs; the copies of such an id are beside them."""
    matched: list[tuple[Item, Item]]    # each base item with the current item it is, in base order
    removed: list[Item]                 # the base items whose key is nowhere now
    added: list[Item]                   # the current items whose key was nowhere at the base
    # Of a key that is on both sides and pairs more than one way: the copies with no pair.
    unpaired_base: list[Item] = field(default_factory=list)
    unpaired_current: list[Item] = field(default_factory=list)

    def __iter__(self):
        return iter((self.matched, self.removed, self.added))


def match(base: list[Item], current: list[Item]) -> Pairs:
    """`(matched, removed, added)`: each base item paired with the current item of the same
    key, in base order; the base items whose key is nowhere now; the current items whose key
    was nowhere at the base.

    One key in several files is paired file by file first; what is left pairs up only when it
    is one on each side (the entry moved). Anything else allows two readings, so it is in none
    of the three: those copies are returned as left over (`Pairs.unpaired_base`,
    `Pairs.unpaired_current`), for a caller that reports them without saying which was which."""
    was: dict[tuple, list[Item]] = {}
    now: dict[tuple, list[Item]] = {}
    for item in base:
        was.setdefault(item.key, []).append(item)
    for item in current:
        now.setdefault(item.key, []).append(item)
    found = Pairs([], [], [])
    for key, olds in was.items():
        news = list(now.get(key, []))
        if not news:
            found.removed += olds
            continue
        left = []
        for old in olds:
            at = next((i for i, new in enumerate(news) if new.log == old.log), None)
            if at is None:
                left.append(old)
            else:
                found.matched.append((old, news.pop(at)))
        if len(left) == 1 and len(news) == 1:
            found.matched.append((left[0], news[0]))
        else:
            found.unpaired_base += left
            found.unpaired_current += news
    found.added = [item for key, items in now.items() if key not in was for item in items]
    return found
