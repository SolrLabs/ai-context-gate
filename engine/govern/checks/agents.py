"""Agent definitions: pinned model, effort and turn cap."""
from __future__ import annotations

import re

from govern.findings import Findings
from govern.manifest import Param, check
from govern.text import parse_frontmatter, read

TURNS_RE = re.compile(r"(\d+)\s+turns")


@check("agents", scope="workspace", since="0.4.0",
       summary="Every agent definition pins its name, model, effort and whether it may read "
               "CLAUDE.md, an optional turn cap stays under the ceiling, and prose never "
               "contradicts the cap.",
       question="Which frontmatter keys does every agent carry, is a turn cap required, and "
                "what is the most turns any agent may run?",
       rationale="An agent riding on defaults is one nobody controls; a cap, where one is set, "
                 "is a ceiling that surfaces a non-converging agent, and a cap stated twice is "
                 "invisible drift.",
       params={
           "required_keys": Param("list",
                                  ["description", "model", "effort", "omitClaudeMd"],
                                  "Frontmatter every agent carries besides name and maxTurns",
                                  looser="fewer"),
           "warn_keys": Param("list", ["omitClaudeMd"],
                              "Keys in required_keys that only warn when absent; a profile "
                              "sets this to [] to make them errors", looser="more"),
           "efforts": Param("list", ["low", "medium", "high", "xhigh", "max"],
                            "Allowed effort values", looser="more"),
           "require_max_turns": Param("bool", False,
                                      "Whether every agent must set maxTurns"),
           "max_turns": Param("int", 0, "Ceiling on maxTurns, 0 for no ceiling",
                              looser="higher", unlimited=0),
       })
def agents(ctx, params) -> Findings:
    f = Findings()
    adir = ctx.root / ctx.workspace("agents_dir", ".claude/agents")
    if not adir.is_dir():
        return f
    prose_mode = ctx.cfg.dialect["agent_turns_prose"]
    for path in sorted(adir.glob("*.md")):
        fm, body = parse_frontmatter(read(path))
        where = f"agents/{path.name}"
        if not fm:
            f.error(f"{where}: no frontmatter")
            continue
        if fm.get("name") != path.stem:
            f.error(f"{where}: name '{fm.get('name')}' != filename '{path.stem}'")
        for key in params["required_keys"]:
            if not fm.get(key):
                msg = f"{where}: frontmatter missing '{key}'"
                (f.warn if key in params["warn_keys"] else f.error)(msg)
        effort = fm.get("effort")
        if effort and effort not in params["efforts"]:
            f.error(f"{where}: effort '{effort}' not in {tuple(params['efforts'])}")
        turns = fm.get("maxTurns")
        n = None
        malformed = False   # maxTurns was given but is not usable — reported once, below
        if not turns:
            if params["require_max_turns"]:
                f.error(f"{where}: no maxTurns — set a cap high enough that a properly "
                        f"performing agent never hits it, but a runaway agent is stopped for "
                        f"review")
        else:
            try:
                n = int(turns)
            except (ValueError, TypeError):
                # A warning, not an error: an existing project carrying a 0, negative or
                # non-integer value must not turn red on upgrade.
                f.warn(f"{where}: maxTurns '{turns}' is not an integer")
                malformed = True
            else:
                if n <= 0:
                    f.warn(f"{where}: maxTurns '{turns}' is not a positive integer")
                    n = None
                    malformed = True
                elif params["max_turns"] and n > params["max_turns"]:
                    f.error(f"{where}: maxTurns {n} exceeds agents max_turns="
                            f"{params['max_turns']}")
        for m in TURNS_RE.finditer(body):
            if prose_mode == "forbid":
                f.error(f"{where}: prose names a turn count ({m.group(0)}) — say when to stop "
                        f"instead; maxTurns is the only place the cap lives")
            elif n is not None:
                if int(m.group(1)) != n:
                    f.error(f"{where}: prose says {m.group(1)} turns, frontmatter says {n}")
            elif not malformed:
                # Genuinely no maxTurns to match against — a malformed one was already
                # reported once, above (one finding per agent problem).
                f.error(f"{where}: prose says {m.group(1)} turns, but there is no maxTurns to "
                        f"match")
    return f


@check("skills", scope="workspace", since="0.4.0",
       summary="Every skill definition has frontmatter with a description.",
       question="Does this project define its own Claude Code skills?",
       rationale="Claude Code skips a skill without a description, silently.",
       params={"skills_dir": Param("str", ".claude/skills", "Where the project's skills live")})
def skills(ctx, params) -> Findings:
    f = Findings()
    sdir = ctx.root / params["skills_dir"]
    for path in sorted(sdir.glob("*/SKILL.md")) if sdir.is_dir() else []:
        fm, _ = parse_frontmatter(read(path))
        where = ctx.rel(path)
        if not fm:
            f.error(f"{where}: no frontmatter")
        elif not fm.get("description"):
            f.error(f"{where}: skill is missing 'description' — Claude Code skips it silently")
    return f
