"""The registry itself: entries match the configuration, and ID ranges never overlap."""
from __future__ import annotations

from govern.findings import Findings
from govern.manifest import Param, check


@check("registry", scope="workspace", since="0.4.0", core=True,
       summary="Every registry entry is well formed: its required facts present, a known tier, "
               "ID facts where decisions are kept, and every fact where the configuration reads "
               "it.",
       question="Where does your registry keep each fact about a project, and which tiers do "
                "you use?",
       rationale="A key the engine looks for in the wrong place is a check that silently never "
                 "runs.",
       params={
           "required_keys": Param("list", ["name", "dir", "tier"],
                                  "Facts every registry entry carries", looser="fewer"),
           "tiers": Param("list", ["full", "registered"], "Allowed tier values", looser="more"),
       })
def registry(ctx, params) -> Findings:
    f = Findings()
    for problem in ctx.registry.problems:
        f.error(problem)
    governed = ctx.projects("governed_tiers", ["full"])
    for s in ctx.registry.scopes:
        for key in params["required_keys"]:
            if not s.get(key):
                f.error(f"registry: '{s.name}' is missing '{key}'")
        tier = s.get("tier")
        if tier and tier not in params["tiers"]:
            f.error(f"registry: '{s.name}' tier '{tier}' is not one of {tuple(params['tiers'])}")
        if tier in governed and s.gov is not None and not (s.id_prefix and s.get("id_range")):
            f.error(f"registry: '{s.name}' keeps a decision log, so it needs id_prefix and id_range")
    return f


@check("id-ranges", scope="workspace", since="0.4.0",
       summary="Decision-ID ranges claimed by the workspace and each project never overlap.",
       question="Do projects share one decision-ID namespace?",
       rationale="Two logs that can mint the same ID make every citation ambiguous.")
def id_ranges(ctx, params) -> Findings:
    f = Findings()
    reg = ctx.registry
    claimed = []
    if reg.workspace_range:
        claimed.append((ctx.workspace_label, reg.workspace_prefix or "W", reg.workspace_range))
    for s in reg.scopes:
        if s.id_range:
            claimed.append((s.name, s.id_prefix or "?", s.id_range))
    prefix_aware = ctx.cfg.dialect["id_overlap"] == "prefix-aware"
    for i, (n1, p1, (a1, b1)) in enumerate(claimed):
        for n2, p2, (a2, b2) in claimed[i + 1:]:
            if prefix_aware and p1 != p2:
                continue
            if a1 <= b2 and a2 <= b1:
                f.error(f"id ranges overlap: {n1} {a1}-{b1} and {n2} {a2}-{b2}")
    return f
