"""Working files: the part of the doc system that is supposed to be curated."""
from __future__ import annotations

from datetime import datetime
from fnmatch import fnmatch

from govern.checks.docs import status_keyword
from govern.findings import Findings
from govern.manifest import Param, check
from govern.text import parse_frontmatter, read, word_count


@check("working-file-count", scope="project", since="0.4.0", applies="governed", default="warn",
       summary="Bounds how many files sit directly in a project's working-files directory.",
       question="How many working files may a project hold at once?",
       rationale="The directory filling up is the failure, independent of any one file's length.",
       params={
           "max_files": Param("int", 12, "Files directly under the working dir",
                              looser="higher"),
           "extensions": Param("list", ["*.md", "*.html", "*.pdf"], "Which files count"),
       })
def working_file_count(ctx, params, scope) -> Findings:
    f = Findings()
    wf = ctx.working_dir(scope)
    if not wf.is_dir():
        return f
    files = [p for ext in params["extensions"] for p in wf.glob(ext)]
    if len(files) > params["max_files"]:
        f.error(f"{scope.name}: {len(files)} files in {wf.name}/ exceeds "
                f"working-file-count max_files={params['max_files']}")
    return f


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
       })
def finished_files(ctx, params, scope) -> Findings:
    f = Findings()
    wf = ctx.working_dir(scope)
    if not wf.is_dir():
        return f
    inclusive = ctx.cfg.dialect["finished_age"] == "ge"
    limit = params["max_days"]
    for path in sorted(wf.glob("*.md")):
        if any(fnmatch(path.name, pat) for pat in params["exempt"]):
            continue
        fm, _ = parse_frontmatter(read(path))
        status = fm.get("status", "")
        if status_keyword(status) not in params["finished_statuses"]:
            continue
        where = f"{scope.name}/{ctx.rel(path)}"
        if not ctx.committed(path):
            f.error(f"{where} is finished but was never committed — commit it once so git "
                    f"keeps it, then delete it")
            continue
        age = (datetime.now() - ctx.last_touched(path)).days
        if age > limit or (inclusive and age == limit):
            f.error(f"{where}: status '{status}', last touched {age}d ago "
                    f"(finished-files max_days={limit}) — delete it; git keeps history")
    return f


def handoff_path(ctx, scope):
    rel = scope.get("handoff")
    return ctx.root / rel if rel else None


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
    path = handoff_path(ctx, scope)
    if path is None or not path.exists():
        return f
    wc = handoff_words(ctx, path)
    if wc > params["max_words"]:
        f.warn(f"{scope.name}: {path.name} is {wc} words, over handoff-words max_words="
               f"{params['max_words']} — current state only, not a log of sessions")
    return f
