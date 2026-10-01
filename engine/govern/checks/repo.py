"""The governance root is inside a git repository, which the history-based checks rely on, and
that repository does not ignore the files the gate needs committed."""
from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

from govern import layout
from govern.context import git_env, repo_of
from govern.findings import Findings
from govern.manifest import check


@check("git-repo", scope="workspace", since="0.4.1", default="warn",
       summary="The governance root is inside a git repository.",
       question="Is this project a git repository? Several checks read its history.",
       rationale="Outside git, the checks that read history or ignore rules (when a file was "
                 "last touched, whether it was ever committed, what git ignores) quietly check "
                 "nothing; the gate would look green while not looking.")
def git_repo(ctx, params) -> Findings:
    f = Findings()
    if not (ctx.root / ".git").exists() and repo_of(ctx.root) is None:
        f.warn(f"{ctx.root.name}: not inside a git repository, so the checks that read git "
               f"history or ignore rules check nothing — run `git init` and commit")
    return f


class GitFailed(Exception):
    """`git check-ignore` could not answer (git missing, `safe.directory`, a lock)."""


def tool_files(root: Path) -> list[str]:
    """Every file the install wrote that a commit must carry for the gate to run in another
    clone, and for uninstall to work there: the config, the baseline, the install record, the
    `bin/` entry points, the entry-point stubs the install record lists, and the originals it
    kept for uninstall to restore. Root-relative, posix; only those that exist."""
    rels = [layout.CONFIG, layout.BASELINE, layout.MANIFEST,
            *(f"{layout.GOV_DIR}/{t}" for t in layout.TOOL_FILES)]
    try:
        record = tomllib.loads((root / layout.MANIFEST).read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        record = {}
    for stub in record.get("entrypoint", []):
        rels += [stub.get("path"), stub.get("backup")]
    for key in ("retired", "settings"):
        rels += [item.get("backup") for item in record.get(key, [])]
    rels += [move.get("to") for move in record.get("moved", [])]
    out = []
    for rel in rels:
        if isinstance(rel, str) and rel and rel not in out and (root / rel).is_file():
            out.append(rel)
    return out


def _check_ignore(base: Path, ask: list[str]) -> dict[str, str]:
    """`{path: rule}` for each of `ask` (relative to `base`) that `git -C base check-ignore`
    says is ignored, the rule being the ignore file, line and pattern that matched. Raises
    `GitFailed` when git cannot answer."""
    try:
        res = subprocess.run(["git", "-C", str(base), "check-ignore", "-v", "-z", "--stdin"],
                             input="\0".join(ask).encode("utf-8"), capture_output=True,
                             timeout=20, env=git_env())
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitFailed(str(exc)) from exc
    if res.returncode not in (0, 1):
        detail = res.stderr.decode("utf-8", errors="replace").strip()
        raise GitFailed(detail.splitlines()[0] if detail else f"exit {res.returncode}")
    fields = res.stdout.decode("utf-8", errors="replace").split("\0")
    rules = {}
    for i in range(0, len(fields) - 3, 4):
        source, line, pattern, path = fields[i:i + 4]
        if pattern and not pattern.startswith("!"):
            rules[path] = f"{source}:{line} '{pattern}'"
    return rules


def ignored(root: Path, rels: list[str]) -> list[tuple[str, str, str, list[str]]]:
    """Which of `rels` (root-relative, posix) git ignores, grouped by fix: `(fix, rule,
    gitignore, paths)`. Each file is asked of the repo that holds it (`repo_of`), as
    `Context._ignored` does: a file in a nested checkout that is its own repo is that repo's
    question, never the outer repo's, which may ignore the whole checkout — advising the outer
    repo to re-include it would sweep the nested repo into its next `git add -A`.

    A file in the root's own repo is asked from the root, and its fix goes in the root's own
    `.gitignore`; one in a nested repo, from that repo's top, into that repo's `.gitignore`.
    The fix re-includes the outermost ignored directory on the path below that point
    (`!.context-gate/bin/` where a `bin/` rule for build output matched), or the file itself.
    `gitignore` names the file to put it in, relative to the top of the root's repo, so a root
    that is a subdirectory of its repo is told `sub/.gitignore`, not a bare `.gitignore`.

    Empty when `root` is in no git repo (`git-repo` says so). A git call that cannot answer
    raises `GitFailed`, never reads as "nothing ignored". A file git already tracks is never
    reported: ignore rules do not apply to it."""
    root_r = root.resolve()
    top = root_r if (root_r / ".git").exists() else repo_of(root_r)
    if top is None:
        return []
    by_base: dict[Path, dict[str, str]] = {}
    for rel in rels:
        path = root_r / rel
        repo = repo_of(path)
        if repo is None:
            continue
        base = root_r if repo == top else repo
        by_base.setdefault(base, {})[path.relative_to(base).as_posix()] = rel
    groups: dict[tuple[str, str, str], list[str]] = {}
    for base, files in by_base.items():
        ask: list[str] = []
        for here in files:
            parts = here.split("/")
            ask += ["/".join(parts[:i]) + "/" for i in range(1, len(parts))] + [here]
        rules = _check_ignore(base, list(dict.fromkeys(ask)))
        where_file = (base / ".gitignore").relative_to(top).as_posix()
        for here, rel in files.items():
            if here not in rules:
                continue
            parts = here.split("/")
            dirs = ["/".join(parts[:i]) + "/" for i in range(1, len(parts))]
            where = next((d for d in dirs if d in rules), here)
            groups.setdefault((f"!{where}", rules[where], where_file), []).append(rel)
    return [(fix, rule, gi, paths) for (fix, rule, gi), paths in groups.items()]


def ignored_findings(root: Path, extra: list[str] = ()) -> list[str]:
    """One line per fix, `paths: finding — fix`, for the tool files (and `extra`) git ignores:
    what `tool-files` warns and what the install and adopt reports list as needing a person."""
    try:
        found = ignored(root, [*tool_files(root), *(r for r in extra if r)])
    except GitFailed as exc:
        return [f"{layout.GOV_DIR}/: could not ask git whether it ignores the files the gate "
                f"needs committed ({exc}) — run `git check-ignore -v {layout.ENTRYPOINT}`"]
    return [f"{', '.join(paths)}: ignored by git ({rule}), so a commit leaves out what the "
            f"gate needs — add '{fix}' to {gitignore}" for fix, rule, gitignore, paths in found]


@check("tool-files", scope="workspace", since="0.5.0", default="warn",
       summary="Git ignores none of the files the gate needs committed: its config, baseline, "
               "install record and entry points.",
       question="Does this project's .gitignore leave the gate's own files alone? A `bin/` "
                "rule for build output also matches `.context-gate/bin/`.",
       rationale="A commit that leaves out an ignored entry point ships a config with nothing "
                 "to run it: every other clone, and CI, has no gate.")
def tool_files_check(ctx, params) -> Findings:
    f = Findings()
    for msg in ignored_findings(ctx.root):
        f.warn(msg)
    return f
