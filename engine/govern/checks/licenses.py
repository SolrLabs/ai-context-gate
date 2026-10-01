"""Licenses across the registry: every entry declares one, and a configured incompatible pairing
is only allowed once a decision records why it is safe."""
from __future__ import annotations

from govern.findings import Findings
from govern.decisions import id_key
from govern.manifest import Param, check


def _matches(scope, side: dict) -> bool:
    declared = scope.get("license") or ""
    return (any(s in declared for s in side.get("licenses", []))
            and (not side.get("roles") or scope.get("role") in side["roles"]))


def _named(value) -> str:
    """A registry license fact as text: registry facts are untyped, so a list (a dual license)
    reads as its values joined."""
    return " + ".join(map(str, value)) if isinstance(value, list) else str(value)


def _suggest(ctx, s) -> str | None:
    scopes = ctx.registry.scopes
    if len(scopes) < 2:
        return None
    declared = sorted({_named(sc.get("license")) for sc in scopes if sc.get("license")})
    missing = [sc.name for sc in scopes if not sc.get("license")]
    if len(declared) < 2 and not missing:
        return None
    parts = ([", ".join(declared)] if declared else []) \
        + ([f"none declared: {', '.join(missing)}"] if missing else [])
    return f"{len(scopes)} registry entries; licenses: {'; '.join(parts)}"


@check("licenses", scope="workspace", since="0.4.0", default="off", suggest=_suggest,
       summary="Every registry entry declares a license, and each configured conflict (say, "
               "copyleft code beside permissive code) is recorded as a workspace decision.",
       question="Do repos under different licenses share this workspace? Which combinations "
                "need a recorded decision about how code stays separated?",
       rationale="A license firewall depends on knowing every license, and on the reasoning "
                 "being written down where the next session will find it.",
       params={
           "require_declared": Param("bool", True, "Every registry entry declares a license"),
           "rule": Param("str", "", "Decision id to cite when a license is undeclared"),
           "conflicts": Param("tables", [], "Pairings that need a recorded decision",
                              fields={"a": "table", "b": "table", "decision": "str"},
                              required=("a", "b", "decision")),
       })
def licenses(ctx, params) -> Findings:
    f = Findings()
    scopes = ctx.registry.scopes
    cite = f" ({params['rule']})" if params["rule"] else ""
    if params["require_declared"]:
        for s in scopes:
            if not s.get("license"):
                f.error(f"{s.name}: no license declared — the license rules depend on it{cite}")
    recorded = None
    for c in params["conflicts"]:
        a = [s.name for s in scopes if _matches(s, c["a"])]
        b = [s.name for s in scopes if _matches(s, c["b"])]
        if not (a and b):
            continue
        if recorded is None:
            log = ctx.workspace_log
            recorded = {e.key for e in ctx.grammar.parse_file(log).entries} \
                if log is not None and log.exists() else set()
        if id_key(c["decision"]) not in recorded:
            f.error(f"licenses: {', '.join(a)} and {', '.join(b)} coexist but "
                    f"{c['decision']} is not a recorded decision")
    return f
