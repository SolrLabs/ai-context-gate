"""Claude Code hooks a governance rule depends on are present and wired."""
from __future__ import annotations

import json

from govern.findings import Findings
from govern.manifest import Param, check
from govern.text import read


def _prefix(hook: dict) -> str:
    return f"{hook['rule']}: " if hook.get("rule") else ""


@check("hooks-wired", scope="workspace", since="0.4.0", default="off",
       summary="Each configured hook script exists and is wired in .claude/settings.json on "
               "its event and matcher.",
       question="Does any rule of yours rely on a Claude Code hook (blocking outbound actions, "
                "guarding a tool)? Which script, on which event and matcher?",
       rationale="A rule enforced by a hook is only a rule while the hook is wired; a malformed "
                 "settings file disables every hook in it.",
       params={
           "settings": Param("str", ".claude/settings.json", "Settings file the hooks live in"),
           "hooks": Param("tables", [], "Hooks that must be wired",
                          fields={"script": "str", "event": "str", "matcher": "str",
                                  "rule": "str", "missing": "str", "unwired": "str"},
                          required=("script", "event", "matcher")),
       })
def hooks_wired(ctx, params) -> Findings:
    f = Findings()
    hooks = params["hooks"]
    if not hooks:
        return f
    settings = ctx.root / params["settings"]
    rules = ", ".join(dict.fromkeys(h["rule"] for h in hooks if h.get("rule")))
    lead = f"{rules}: " if rules else ""
    if not settings.exists():
        f.error(f"{lead}{params['settings']} is missing — no hook is wired")
        return f
    try:
        conf = json.loads(read(settings))
    except json.JSONDecodeError as exc:
        f.error(f"{lead}{params['settings']} is not valid JSON ({exc}) — "
                f"a malformed file disables every setting in it")
        return f
    for hook in hooks:
        script = hook["script"]
        name = script.rsplit("/", 1)[-1]
        if not (ctx.root / script).exists():
            f.error(f"{_prefix(hook)}{script} is missing"
                    + (f" — {hook['missing']}" if hook.get("missing") else ""))
            continue
        wired = any(
            h.get("type") == "command" and name in h.get("command", "")
            for entry in conf.get("hooks", {}).get(hook["event"], [])
            if hook["matcher"] in (entry.get("matcher") or "")
            for h in entry.get("hooks", []))
        if not wired:
            f.error(f"{_prefix(hook)}no {hook['event']} hook on {hook['matcher']} runs {name}"
                    + (f" — {hook['unwired']}" if hook.get("unwired") else ""))
    return f
