"""Licences across the registry: every entry declares one, and a configured incompatible pairing
is only allowed once a decision records why it is safe."""
from __future__ import annotations

from govern.findings import Findings
from govern.decisions import id_key
from govern.manifest import Param, check


def _matches(scope, side: dict) -> bool:
    licence = scope.get("licence") or ""
    return (any(s in licence for s in side.get("licences", []))
            and (not side.get("roles") or scope.get("role") in side["roles"]))


@check("licences", scope="workspace", since="0.4.0", default="off",
       summary="Every registry entry declares a licence, and each configured conflict (say, "
               "copyleft code beside permissive code) is recorded as a workspace decision.",
       question="Do repos under different licences share this workspace? Which combinations "
                "need a recorded decision about how code stays separated?",
       rationale="A licence firewall depends on knowing every licence, and on the reasoning "
                 "being written down where the next session will find it.",
       params={
           "require_declared": Param("bool", True, "Every registry entry declares a licence"),
           "rule": Param("str", "", "Decision id to cite when a licence is undeclared"),
           "conflicts": Param("tables", [], "Pairings that need a recorded decision",
                              fields={"a": "table", "b": "table", "decision": "str"},
                              required=("a", "b", "decision")),
       })
def licences(ctx, params) -> Findings:
    f = Findings()
    scopes = ctx.registry.scopes
    cite = f" ({params['rule']})" if params["rule"] else ""
    if params["require_declared"]:
        for s in scopes:
            if not s.get("licence"):
                f.error(f"{s.name}: no licence declared — the licence rules depend on it{cite}")
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
            f.error(f"licences: {', '.join(a)} and {', '.join(b)} coexist but "
                    f"{c['decision']} is not a recorded decision")
    return f
