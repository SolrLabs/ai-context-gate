"""Docs that still point at something the install moved, replaced or retired."""
from __future__ import annotations

import tomllib

from govern import layout
from govern.findings import Findings
from govern.manifest import Param, check
from govern.text import read


def _install_renames(ctx) -> list[tuple[str, str]]:
    """(old, now) for every path installed.toml says the install moved or retired."""
    path = ctx.root / layout.MANIFEST
    if not path.is_file():
        return []
    with path.open("rb") as fh:
        record = tomllib.load(fh)
    out = [(m["from"], m["to"]) for m in record.get("moved", [])]
    out += [(r["path"], "retired at install") for r in record.get("retired", [])]
    return out


@check("stale-references", scope="workspace", since="0.4.0", default="warn",
       summary="Governed docs no longer mention paths the install moved or retired, or names "
               "the project retired.",
       question="Has your project retired names or paths that docs may still mention? What "
                "replaced them?",
       rationale="A doc that points at something gone sends the next reader to the wrong place.",
       params={
           "names": Param("tables", [], "Retired names and what replaced them",
                          fields={"text": "str", "now": "str"}, required=("text", "now")),
       })
def stale_references(ctx, params) -> Findings:
    f = Findings()
    renames = _install_renames(ctx) + [(n["text"], n["now"]) for n in params["names"]]
    if not renames:
        return f
    docs = ctx.workspace_docs()
    for s in ctx.registry.scopes:
        if s.governed and s.gov is not None:
            docs += [p for _, p in ctx.governed_docs(s.gov) if p not in docs]
    docs += [p for p in sorted((ctx.root / ".claude").rglob("*.md"))
             if (ctx.root / ".claude").is_dir() and p not in docs]
    for path in docs:
        text = read(path)
        for old, now in renames:
            n = text.count(old)
            if n:
                f.warn(f"{ctx.rel(path)}: mentions '{old}' {n}× — now {now}")
    return f
