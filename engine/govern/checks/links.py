"""Links between documents: every link in a governed doc resolves, and every governed doc can be
reached from its project's index."""
from __future__ import annotations

import os
import re
from pathlib import Path

from govern import blocks
from govern.context import git
from govern.findings import Findings
from govern.manifest import Param, check
from govern.text import read

LINK_RE = re.compile(r"\[(?P<text>[^\]]*)\]\((?P<target>[^)\s]+)(?:\s+\"[^\"]*\")?\)")
FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
INLINE_CODE_RE = re.compile(r"(`+).*?\1")
SKIP = ("#", "http://", "https://", "mailto:", "tel:")


def link_targets(text: str) -> list[str]:
    """Relative link targets outside code fences and inline code."""
    out, fence = [], None
    for line in text.split("\n"):
        m = FENCE_RE.match(line)
        if fence is not None:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence):
                fence = None
            continue
        if m:
            fence = m.group(1)
            continue
        for lm in LINK_RE.finditer(INLINE_CODE_RE.sub("", line)):
            target = lm.group("target").strip()
            if target and not target.lower().startswith(SKIP):
                out.append(target)
    return out


def _docs(ctx, scope=None):
    """(label, path) for every governed doc: the workspace's, then each project's — or, with
    `scope`, just that one project's (the workspace's own docs, e.g. AGENTS.md, are not part of
    any one project, so a project-scoped run leaves them out entirely)."""
    seen = []
    if scope is None:
        for pattern in ctx.workspace("docs", []):
            for p in sorted(ctx.root.glob(pattern)):
                if p.is_file() and p not in [q for _, q in seen] and not ctx.owned_by_project(p):
                    # A project scope's own doc (single-repo mode's default shape, or a doc
                    # glob wide enough to reach into a project dir): that scope's own pass over
                    # its governed docs, below, checks its links already.
                    seen.append((ctx.rel(p), p))
        scopes = ctx.registry.scopes
    else:
        scopes = [scope]
    for s in scopes:
        if s.governed and s.gov is not None:
            seen += [(f"{s.name}/{rel}", p) for rel, p in ctx.governed_docs(s.gov)]
    return seen


def _history_repo(ctx, path: Path) -> tuple[Path, str] | None:
    """Where to ask git about `path`, as `(repo, rel)` — or `None` when no checkout can answer
    at all. `path` outside any `--path` snapshot: the ordinary checkout, `ctx.root`. Inside one:
    `--history-from`'s checkout, with `path`'s place there, since the snapshot itself carries no
    history of its own — or `None` when no `--history-from` was given to ask instead."""
    if ctx.snapshot is None:
        return ctx.root, ctx.rel(path)
    try:
        rel = path.relative_to(ctx.snapshot)
    except ValueError:
        return ctx.root, ctx.rel(path)
    if ctx.history_from is None:
        return None
    return ctx.history_from, rel.as_posix()


def _deleted_hint(ctx, missing: Path, cache: dict[str, str | None]) -> str:
    """When git history shows `missing` (an absolute path) was deleted, the short sha of the
    commit that removed it — cached per check run, one `git log` per missing target. Empty when
    git fails, the path never existed, or (`--path` with no `--history-from`) there is no
    checkout with any history to ask at all: the caller's message is then unchanged."""
    found = _history_repo(ctx, missing)
    if found is None:
        return ""
    repo, rel = found
    key = str(missing)
    if key not in cache:
        cache[key] = git(repo, "log", "-1", "--diff-filter=D", "--format=%h", "--", rel)
    sha = cache[key]
    if not sha:
        return ""
    return (f" — deleted in {sha}; link the last commit that had it ({sha}^:{rel}) instead of "
            f"removing the link")


@check("doc-links", scope="workspace", since="0.4.0", also_project=True,
       summary="Every relative link in a governed doc points at a file that exists.",
       question="Should broken links between your docs fail the gate?",
       rationale="A link to nothing sends the next reader, human or agent, to guess.")
def doc_links(ctx, params, scope=None) -> Findings:
    f = Findings()
    cache: dict[str, str | None] = {}
    for label, path in _docs(ctx, scope):
        for target in link_targets(read(path)):
            rel = target.split("#")[0]
            if not rel:
                continue
            missing = Path(os.path.normpath(path.parent / rel))
            if ctx.snapshot is not None:
                try:
                    missing.relative_to(ctx.snapshot)
                except ValueError:
                    # The link climbs out of the snapshot (`../../governance/X.md`, say): there
                    # is nothing at that spot inside a directory that only ever held one
                    # project's files, so it is resolved and checked against the real tree
                    # instead, from the real place `path` lives.
                    missing = Path(os.path.normpath(ctx.real_path(path).parent / rel))
            if not missing.exists():
                f.error(f"{label}: link target does not exist: {rel}"
                        f"{_deleted_hint(ctx, missing, cache)}")
    return f


@check("doc-reachability", scope="project", since="0.4.0", applies="governed",
       summary="Every governed doc is listed in the project's generated doc registry.",
       question="Should every doc be reachable from the project's index?",
       rationale="An agent finds a doc through the index; one the index does not list is a doc "
                 "nobody opens.",
       params={"index": Param("str", "INDEX.md", "The doc that carries the doc-registry block, "
                                                 "relative to the governance dir")})
def doc_reachability(ctx, params, scope) -> Findings:
    f = Findings()
    index = scope.gov / params["index"]
    if not index.is_file():
        return f      # a missing index is doc-set's finding, not this one's
    registry = blocks.extract(ctx, read(index), "doc-registry")
    if registry is None:
        return f      # missing markers are generated-blocks' finding
    for rel, path in ctx.governed_docs(scope.gov):
        if path == index:
            continue
        link = os.path.relpath(path, index.parent).replace(os.sep, "/")
        if f"]({link})" not in registry:
            f.error(f"{scope.name}/{rel}: not reachable from {params['index']}'s doc registry — "
                    f"run `index`")
    return f
