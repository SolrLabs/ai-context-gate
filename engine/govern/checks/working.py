"""Working files: the part of the doc system that is supposed to be curated."""
from __future__ import annotations

from datetime import datetime
from fnmatch import fnmatch
from pathlib import Path

from govern.checks.docs import all_ticked, owner_name, status_keyword
from govern.findings import Findings
from govern.manifest import Param, check
from govern.text import Unreadable, parse_frontmatter, read, word_count


@check("working-file-count", scope="project", since="0.4.0", applies="governed", default="warn",
       summary="Bounds how many files sit directly in a project's working-files directory.",
       question="How many working files may a project hold at once?",
       rationale="The directory filling up is the failure, independent of any one file's length.",
       params={
           "max_files": Param("int", 12, "Files directly under the working dir",
                              looser="higher"),
           "extensions": Param("list", ["*.md", "*.html", "*.pdf"], "Which files count"),
           "per_owner_max": Param("int", 0, "Owned files (frontmatter owner) each owner may "
                                  "hold in the working dir; 0 counts every file against "
                                  "max_files", looser="higher"),
       })
def working_file_count(ctx, params, scope) -> Findings:
    f = Findings()
    wf = ctx.working_dir(scope)
    if not wf.is_dir():
        return f
    files = [p for ext in params["extensions"] for p in wf.glob(ext)]
    per_owner = params["per_owner_max"]
    if per_owner > 0:
        owned: dict[str, int] = {}
        unowned = []
        for p in files:
            owner = _file_owner(p)
            if owner:
                owned[owner] = owned.get(owner, 0) + 1
            else:
                unowned.append(p)
        for owner in sorted(owned):
            if owned[owner] > per_owner:
                f.error(f"{scope.name}: owner '{owner}' holds {owned[owner]} files in "
                        f"{wf.name}/, exceeds working-file-count per_owner_max={per_owner}")
        files = unowned
    if len(files) > params["max_files"]:
        f.error(f"{scope.name}: {len(files)} files in {wf.name}/ exceeds "
                f"working-file-count max_files={params['max_files']}")
    return f


def _file_owner(path) -> str:
    """The frontmatter `owner` of a file, or "" when it has none (or is not readable text)."""
    try:
        fm, _ = parse_frontmatter(read(path))
    except Unreadable:
        return ""
    return owner_name(fm.get("owner"))


def _finished_scan(ctx, params, scope):
    """`(path, kind, status, age)` for each working file `finished-files` has something to say
    about, in name order: kind `ticked` (every box ticked, status not finished), `uncommitted`
    (finished, never committed) or `stale` (finished, committed, untouched past `max_days`; the
    only kind with an age)."""
    wf = ctx.working_dir(scope)
    if not wf.is_dir():
        return
    inclusive = ctx.cfg.dialect["finished_age"] == "ge"
    limit = params["max_days"]
    for path in sorted(wf.glob("*.md")):
        if any(fnmatch(path.name, pat) for pat in params["exempt"]):
            continue
        fm, body = parse_frontmatter(read(path))
        status = fm.get("status", "")
        if status_keyword(status) not in params["finished_statuses"]:
            if params["ticked"] and all_ticked(body, ctx.markers):
                yield path, "ticked", status, 0
            continue
        if not ctx.committed(path):
            yield path, "uncommitted", status, 0
            continue
        age = (datetime.now() - ctx.last_touched(path)).days
        if age > limit or (inclusive and age == limit):
            yield path, "stale", status, age


def finished_candidates(ctx, params, scope) -> list[tuple[Path, str, int]]:
    """`(path, status, age in days)` for each finished, committed working file untouched past
    `max_days`: the ones `finished-files` says to delete. A finished file never committed is
    not one."""
    return [(path, status, age) for path, kind, status, age
            in _finished_scan(ctx, params, scope) if kind == "stale"]


@check("finished-files", scope="project", since="0.4.0", applies="governed", default="warn",
       summary="A working file whose status says it is finished should be deleted once it has "
               "sat untouched for a few days. A finished file that was never committed is "
               "flagged at once instead, so it is committed before it is deleted.",
       question="Which statuses mean finished, and how long may a finished file linger?",
       rationale="Git keeps the history, so nothing is lost by deleting a finished file once "
                 "it has been committed, and nothing is gained by every sweep re-reading it.",
       params={
           "max_days": Param("int", 7, "Days a finished file may sit untouched",
                             looser="higher"),
           "finished_statuses": Param("list", ["complete", "superseded"],
                                      "Statuses that mean the file is finished",
                                      looser="fewer"),
           "exempt": Param("list", ["HANDOFF.md", "traps*"],
                           "File names never treated as finished (glob patterns)",
                           looser="more"),
           "ticked": Param("bool", True,
                           "A working file whose every checkbox is ticked counts as finished"),
       })
def finished_files(ctx, params, scope) -> Findings:
    f = Findings()
    for path, kind, status, age in _finished_scan(ctx, params, scope):
        where = f"{scope.name}/{ctx.rel(path)}"
        if kind == "ticked":
            f.warn(f"{where}: every box is ticked: delete it (git keeps it), or set its "
                   f"status to complete")
        elif kind == "uncommitted":
            f.error(f"{where} is finished but was never committed — commit it once so git "
                    f"keeps it, then delete it")
        else:
            f.error(f"{where}: status '{status}', last touched {age}d ago "
                    f"(finished-files max_days={params['max_days']}) — delete it; git keeps "
                    f"history")
    return f


def handoff_path(ctx, scope):
    rel = scope.get("handoff")
    return ctx.root / rel if rel else None


def handoff_is_absolute(scope) -> bool:
    rel = scope.get("handoff") or ""
    return rel.startswith(("/", "\\")) or Path(rel).is_absolute()


def handoff_is_glob(scope) -> bool:
    return "*" in (scope.get("handoff") or "")


def handoff_paths(ctx, scope) -> list[Path]:
    """Every HANDOFF a scope names: none without the fact, each match (sorted) when the fact
    holds a `*`, else the one path, whether or not it exists."""
    rel = scope.get("handoff")
    if not rel:
        return []
    if "*" in rel:
        if handoff_is_absolute(scope):
            return []   # `Path.glob` refuses an absolute pattern; `handoff-words` says why
        return sorted(p for p in ctx.root.glob(rel) if p.is_file())
    return [ctx.root / rel]


def handoff_words(ctx, path) -> int:
    _, body = parse_frontmatter(read(path))
    return word_count(body, ctx.markers)


@check("handoff-words", scope="project", since="0.4.0", ratchets=True, applies="governed",
       summary="Bounds a project's HANDOFF: a snapshot of where things stand, not a log.",
       question="How long may a HANDOFF grow before it needs trimming?",
       rationale="A HANDOFF that keeps growing across sessions stops being read.",
       params={"max_words": Param("int", 1500, "Words in a HANDOFF", looser="higher")})
def handoff(ctx, params, scope) -> Findings:
    f = Findings()
    glob = handoff_is_glob(scope)
    if glob and handoff_is_absolute(scope):
        f.warn(f"{scope.name}: handoff glob {scope.get('handoff')} must be relative to the root")
    for path in handoff_paths(ctx, scope):
        if not path.exists():
            continue
        wc = handoff_words(ctx, path)
        if wc > params["max_words"]:
            name = ctx.rel(path) if glob else path.name
            f.warn(f"{scope.name}: {name} is {wc} words, over handoff-words max_words="
                   f"{params['max_words']} — current state only, not a log of sessions")
    return f
