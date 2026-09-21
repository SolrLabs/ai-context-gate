"""Writing rules: strings a set of files must not contain, with the replacement to use."""
from __future__ import annotations

from govern.findings import Findings
from govern.manifest import Param, check
from govern.text import read


@check("writing-rules", scope="workspace", since="0.4.0", default="off",
       summary="Files matching the configured globs contain none of the configured strings.",
       question="Is there copy with a house style you must enforce (drafts that go to another "
                "project, public docs)? Which files, and which words or characters are out?",
       rationale="A style guide is only a rule if something enforces it.",
       params={
           "files": Param("list", [], "Globs, relative to the governance root"),
           "rules": Param("tables", [], "One per forbidden string",
                          fields={"text": "str", "use": "str", "why": "str", "level": "str",
                                  "ignore_case": "bool"},
                          required=("text",)),
       })
def writing_rules(ctx, params) -> Findings:
    f = Findings()
    paths = []
    for pattern in params["files"]:
        paths += [p for p in ctx.root.glob(pattern) if p.is_file() and p not in paths]
    for path in sorted(paths):
        text = read(path)
        rel = ctx.rel(path)
        for rule in params["rules"]:
            needle, hay = rule["text"], text
            if rule.get("ignore_case"):
                needle, hay = needle.lower(), hay.lower()
            n = hay.count(needle)
            if not n:
                continue
            msg = f"{rel}: {n} × '{rule['text']}'"
            if rule.get("use"):
                msg += f" (use '{rule['use']}')"
            if rule.get("why"):
                msg += f" — {rule['why']}"
            (f.warn if rule.get("level") == "warn" else f.error)(msg)
    return f
