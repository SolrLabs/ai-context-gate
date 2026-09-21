"""Everything a check needs about the governance root it is running against."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from govern import layout
from govern.config import Config
from govern.decisions import Grammar
from govern.registry import Registry, Scope
from govern.text import Markers


def git(repo: Path, *args: str) -> str | None:
    """Run git and return its stdout, or `None` on any failure (missing git, no repo, a lock,
    a non-zero exit) so a caller fails closed rather than reading a failure as an empty answer.
    Output is decoded as UTF-8 with `errors="replace"`, never the platform's locale encoding
    (which is not UTF-8 by default on Windows), so a byte git wrote and the console's locale
    disagree on never raises."""
    try:
        res = subprocess.run(["git", "-C", str(repo), *args],
                             capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    return res.stdout.decode("utf-8", errors="replace").strip()


# A governed doc under one of these, in any scope, is skipped without a project having to
# list it in its own [projects] exclude — vendored and build trees are never a project's own
# documentation, in any project (generic, not one project's list of directory names).
DEFAULT_DOC_EXCLUDES = ("node_modules/**", ".venv/**", "venv/**", "vendor/**", "dist/**",
                        "build/**", "target/**", ".git/**")


@dataclass
class Context:
    root: Path
    home: Path
    cfg: Config
    registry: Registry
    prog: str
    # `check --path`: one scope's own checkout, swapped for a directory named on the command
    # line (a git pre-commit hook's staged-tree snapshot, say). `check --history-from`: the
    # checkout git questions about a file under `snapshot` are asked of instead, since the
    # snapshot itself carries no history of its own. Both None on an ordinary run.
    snapshot: Path | None = None
    history_from: Path | None = None
    # The scope's own directory `--path` replaced with `snapshot` — the registry's `gov` for
    # that scope before the swap. None on an ordinary run. `real_path` maps a path inside
    # `snapshot` back to this directory, so a finding, a ratchet key, or a baseline lookup never
    # names the snapshot's own (temporary) location.
    real_scope_dir: Path | None = None
    # Which of `governed_docs`'s candidate files git says are ignored, cached across the whole
    # run: one `git check-ignore --stdin` call per batch of files not already answered, not one
    # per file and not one per call site.
    _ignore_cache: dict = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.markers = Markers(self.cfg.dialect["markers"])
        fields = self.cfg.checks["decision-log"].params["required_fields"]
        self.grammar = Grammar(self.cfg.dialect["decision_heading"], fields, self.markers)

    # ------------------------------------------------------------------ ownership

    def owner(self, path: Path) -> Scope:
        """Which scope owns `path` — decided from the registry's own scopes, never from
        whichever check happens to ask first: the project scope whose governance directory
        contains it (the one with the longest such directory, when more than one project's
        does, so a nested project wins over an outer one), or the workspace when no project's
        does. A workspace-scope check uses this to skip a file a project scope already covers,
        so the two are never asked about the same file under two different labels."""
        resolved = path.resolve()
        best: Scope | None = None
        for s in self.registry.scopes:
            if s.gov is None:
                continue
            gov = s.gov.resolve()
            if resolved == gov or gov in resolved.parents:
                if best is None or len(gov.parts) > len(best.gov.resolve().parts):
                    best = s
        return best if best is not None else self.registry.workspace

    def owned_by_project(self, path: Path) -> bool:
        return not self.owner(path).is_workspace

    # ------------------------------------------------------------------ layout

    def real_path(self, path: Path) -> Path:
        """`path`'s place in the real tree: itself, everywhere but inside a `--path` snapshot,
        where it is the same relative path inside `real_scope_dir` — the scope's own directory
        before `--path` swapped it for the snapshot. Used wherever a finding names a file, or a
        ratchet key identifies one, so the snapshot's own (temporary) location never leaks into
        output or a baseline key."""
        rel = self._snapshot_rel(path)
        if rel is None or self.real_scope_dir is None:
            return path
        return self.real_scope_dir / rel

    def rel(self, path: Path) -> str:
        """`path`, relative to the governance root, for messages and reports — the real repo
        path even when `path` lies inside a `--path` snapshot (`real_path`). A path a
        misconfigured layout (an absolute `working_dir`, say) placed outside the root entirely
        cannot be made relative — reporting its own path is still better than a traceback."""
        real = self.real_path(path)
        try:
            return real.relative_to(self.root).as_posix()
        except ValueError:
            return real.as_posix()

    def workspace(self, key: str, default=None):
        return self.cfg.get("workspace", key, default)

    def projects(self, key: str, default=None):
        return self.cfg.get("projects", key, default)

    @property
    def workspace_label(self) -> str:
        return self.workspace("label", "workspace")

    @property
    def workspace_log(self) -> Path | None:
        """The workspace's own decision log — or None when this is a single repo (no
        `[registry]` at all) naming no `[workspace] decision_log` of its own: with one scope
        that already is the whole repository, there is no second, workspace-only log to point
        at, so there is nothing here to be missing."""
        if self.registry.single and "decision_log" not in self.cfg.raw.get("workspace", {}):
            return None
        return self.root / self.workspace("decision_log", "governance/DECISIONS.md")

    @property
    def baseline_path(self) -> Path:
        return self.root / self.workspace("baseline", layout.BASELINE)

    def scope_log(self, scope: Scope) -> Path:
        return scope.gov / self.projects("decision_log", "DECISIONS.md")

    def working_dir(self, scope: Scope) -> Path:
        return scope.gov / self.projects("working_dir", "working-files")

    def trap_files(self, scope: Scope) -> list[Path]:
        """`traps.md` first: it is the default every tool falls back to."""
        wf = self.working_dir(scope)
        if not wf.is_dir():
            return []
        return sorted(wf.glob(self.projects("trap_glob", "traps*.md")),
                      key=lambda p: (p.name != "traps.md", p.name))

    @property
    def trap_prefix(self) -> str:
        return self.projects("trap_prefix", "T")

    def governed_docs(self, gov: Path) -> list[tuple[str, Path]]:
        """A project's governed docs: `[projects] docs` globs under its governance dir (every
        markdown file by default), minus `DEFAULT_DOC_EXCLUDES` and `[projects] exclude`, minus
        anything git ignores, sorted by path. A vendored or build tree is never a project's own
        documentation in any project; a file git ignores is excluded the same way — unless
        git itself cannot answer (missing, no repo, a lock), when it is kept rather than hidden
        from the gate silently."""
        if not gov.is_dir():
            return []
        from fnmatch import fnmatch
        patterns = self.projects("docs", ["**/*.md"])
        exclude = list(DEFAULT_DOC_EXCLUDES) + list(self.projects("exclude", []))
        candidates: dict[str, Path] = {}
        for pattern in patterns:
            for p in gov.glob(pattern):
                if not p.is_file():
                    continue
                rel = p.relative_to(gov).as_posix()
                if not any(fnmatch(rel, x) for x in exclude):
                    candidates[rel] = p
        ignored = self._ignored(list(candidates.values()))
        return sorted((rel, p) for rel, p in candidates.items() if p not in ignored)

    def _ignored(self, paths: list[Path]) -> set:
        """Which of `paths` git ignores. One `git check-ignore --stdin` call for every batch not
        already answered this run — cached on the context, so the same file asked about from more
        than one scope or check costs one process, never one per call site, and never one per
        file. A failing git call (missing git, no repo, a lock — exit code neither 0 nor 1)
        answers "not ignored" for the whole batch: excluding a doc from the gate silently is
        worse than checking one a real git would have skipped."""
        resolved = {p: p.resolve() for p in paths}
        todo = {p: r for p, r in resolved.items() if r not in self._ignore_cache}
        if todo:
            real_root = self.root.resolve()
            lines: dict[Path, str] = {}
            for p, r in todo.items():
                real = self.real_path(p).resolve()
                try:
                    lines[r] = real.relative_to(real_root).as_posix()
                except ValueError:
                    lines[r] = str(real)
            try:
                res = subprocess.run(["git", "-C", str(self.root), "check-ignore", "--stdin"],
                                     input="\n".join(lines.values()).encode("utf-8"),
                                     capture_output=True, timeout=20)
            except (OSError, subprocess.SubprocessError):
                res = None
            if res is None or res.returncode not in (0, 1):
                for r in todo.values():
                    self._ignore_cache[r] = False
            else:
                hits = set(res.stdout.decode("utf-8", errors="replace").splitlines())
                for r, line in lines.items():
                    self._ignore_cache[r] = line in hits
        return {p for p, r in resolved.items() if self._ignore_cache.get(r, False)}

    # ------------------------------------------------------------------ time

    def _snapshot_rel(self, path: Path) -> Path | None:
        """`path`'s place inside a `--path` snapshot, when it has one — the same relative path
        `--history-from`'s checkout is asked about. None outside the snapshot, or when this run
        set no snapshot at all."""
        if self.snapshot is None:
            return None
        try:
            return path.relative_to(self.snapshot)
        except ValueError:
            return None

    def last_touched(self, path: Path) -> datetime:
        """Last commit time, or now when the file has uncommitted edits or is untracked: a file
        being edited is being touched.

        Under `--path`, the snapshot carries no history of its own; `--history-from` answers
        instead, from the same relative path in that checkout, by comparing the snapshot's
        content against that checkout's committed blob — differing content reads the same way a
        dirty working tree does elsewhere: touched now. Without `--history-from` this fails
        closed exactly like a directory outside any git repo: the file's own mtime is all there
        is to go on."""
        rel = self._snapshot_rel(path)
        if rel is not None:
            if self.history_from is None:
                return datetime.fromtimestamp(path.stat().st_mtime)
            return self._history_last_touched(rel, path)
        rel_str = self.rel(path)
        status = git(self.root, "status", "--porcelain", "--", rel_str)
        if status is None:
            # Not a git repo: the file's own mtime is the only history there is.
            return datetime.fromtimestamp(path.stat().st_mtime)
        if not status:
            ts = git(self.root, "log", "-1", "--format=%at", "--", rel_str)
            if ts:
                return datetime.fromtimestamp(int(ts))
        return datetime.now()

    def _history_last_touched(self, rel: Path, path: Path) -> datetime:
        rel_str = rel.as_posix()
        # `HEAD:./path`, not `HEAD:path`: the plain form is relative to the repo's own top level,
        # which `history_from` need not be (it may be one project's checkout inside a bigger one).
        committed = git(self.history_from, "rev-parse", "--verify", "--quiet",
                        f"HEAD:./{rel_str}")
        if committed:
            have = git(self.history_from, "hash-object", f"--path={rel_str}", str(path))
            if committed == have:
                ts = git(self.history_from, "log", "-1", "--format=%at", "--", rel_str)
                if ts:
                    return datetime.fromtimestamp(int(ts))
        return datetime.now()

    def committed(self, path: Path) -> bool:
        """True only when git confirms the file has at least one commit touching it: `git log
        -1` prints a sha. False for anything else — staged but never committed, gitignored,
        hidden by `status.showUntrackedFiles=no`, not in a repo, or a failing git call — so a
        caller cannot mistake any of those for "committed" (a failing git call fails closed).

        Under `--path`, the answer comes from `--history-from`; without it there is no history
        to confirm anything from, so this is false."""
        rel = self._snapshot_rel(path)
        if rel is not None:
            if self.history_from is None:
                return False
            return bool(git(self.history_from, "log", "-1", "--format=%h", "--",
                            f"./{rel.as_posix()}"))
        return bool(git(self.root, "log", "-1", "--format=%h", "--", self.rel(path)))
