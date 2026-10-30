"""Citations of decision and trap ids that name nothing, or something replaced."""
from __future__ import annotations

from govern import citations, decisions
from govern.findings import Findings
from govern.manifest import Param, check


@check("decision-citations", scope="workspace", since="0.9.0", default="off",
       summary="Docs, agent definitions, build briefs and listed code files cite only decision "
               "and trap ids that exist and were not replaced.",
       question="Do docs, agents or code cite decision or trap ids by number, so a renumbered "
                "or replaced entry leaves a dangling reference?",
       rationale="An id that names nothing, or an entry replaced by another, sends the reader "
                 "to the wrong rule; a citation is only as good as the id it points at.",
       params={
           "code_globs": Param("list", [], "Code files whose comments cite ids, as globs "
                               "relative to the governance root", globs=True),
       })
def decision_citations(ctx, params) -> Findings:
    f = Findings()
    logs = [ctx.workspace_log] if ctx.workspace_log is not None else []
    logs += [ctx.scope_log(s) for s in ctx.registry.scopes]
    for s in ctx.registry.scopes:
        logs += ctx.trap_files(s)
    entries: dict[tuple[str, int], decisions.Entry] = {}
    for path in logs:
        if path.is_file():
            for e in ctx.grammar.parse_file(path).entries:
                entries.setdefault(e.key, e)
    # A trap id is per project: resolve it against the trap files of the scope that owns the
    # citing file; a file no project owns sees every project's traps.
    trap_keys: dict[str, set[tuple[str, int]]] = {}
    for s in ctx.registry.scopes:
        held = trap_keys.setdefault(s.name, set())
        for path in ctx.trap_files(s):
            if path.is_file():
                held.update(e.key for e in ctx.grammar.parse_file(path).entries)
    unreadable: list = []
    for c in citations.scan(ctx, params["code_globs"], unreadable):
        key = decisions.id_key(c.ident)
        entry = entries.get(key)
        if key is not None and key[0] == ctx.trap_prefix.upper() and entry is not None:
            owner = ctx.owner(c.path)
            mine = trap_keys.get(owner.name)
            if mine is not None and key not in mine:
                entry = None
        where = f"{ctx.rel(c.path)}:{c.line}"
        if entry is None:
            f.warn(f"{where}: {c.ident} does not exist")
        elif entry.replaced_by:
            f.warn(f"{where}: {c.ident} was replaced by {entry.replaced_by}")
    for exc in unreadable:
        f.warn(f"{ctx.rel(exc.path)}: not read ({exc.reason}); its citations were not checked")
    return f
