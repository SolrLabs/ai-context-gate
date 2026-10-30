"""The git merge driver is assigned to the files it merges."""
from __future__ import annotations

from govern import merging
from govern.findings import Findings
from govern.manifest import check
from govern.text import Unreadable, read


@check("merge-driver", scope="workspace", since="0.9.0", default="off",
       summary="`.gitattributes` assigns context-gate's merge driver to the decision logs, "
               "trap files, generated-block files and the ratchet baseline, so two branches "
               "that each add an entry merge without a conflict.",
       question="Do several people or agents work on branches of this repository at once, and "
                "so add decision or trap entries in parallel?",
       rationale="Git merges a log by line, so two branches that each append an entry conflict "
                 "at the end of the file; the driver merges by entry id instead. Git applies "
                 "it only where `.gitattributes` says so.")
def merge_driver(ctx, params, scope=None) -> Findings:
    f = Findings()
    path = ctx.root / ".gitattributes"
    try:
        text = read(path) if path.is_file() else ""
    except Unreadable as exc:
        f.error(f".gitattributes: {exc.reason}")
        return f
    have = merging.attributes_current(text)
    if have is None:
        f.error(".gitattributes: the context-gate merge region is missing — run index")
    elif have != merging.attributes_lines(ctx):
        f.error(".gitattributes: the context-gate merge region is stale — run index")
    root_repo = ctx._repo_rel(path)
    for scope in ctx.registry.scopes:
        log = ctx.scope_log(scope)
        found = ctx._repo_rel(log)
        if found is not None and root_repo is not None and found[0] != root_repo[0]:
            f.warn(f"{scope.name}: its own git checkout; the merge driver does not cover "
                   f"{ctx.rel(log)}")
    return f
