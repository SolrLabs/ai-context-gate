"""Rendered roles are as the engine renders them, and docs name only agents that exist."""
from __future__ import annotations

import re

from govern import roles
from govern.decisions import mask_lines
from govern.findings import Findings
from govern.manifest import Param, check
from govern.text import read


@check(roles.CHECK, scope="workspace", since="0.7.0", default="off",
       summary="The engine's agent roles and the coordinator's build rules are rendered into "
               "the project, and each rendered file matches what `agents render` would write.",
       question="Should the engine render its standard agent roles (developer, senior "
                "developer, code reviewer, QA engineer, technical writer) and the coordinator's "
                "build rules into this project, with your models, commands and rules filled in?",
       rationale="A role copied by hand drifts from the standard the day after it is copied; "
                 "one rendered from the engine changes with it, and a hand edit is caught.",
       params={
           "extra_roles": Param("list", [],
                                "Roles rendered beyond the five every project gets: "
                                "ux-reviewer, architect"),
           "models": Param("table", {}, "A role's model, over the engine's choice for it"),
           "efforts": Param("table", {}, "A role's effort, over the engine's choice for it"),
           "max_turns": Param("table", {},
                              "A role's turn cap; a role without one renders `maxTurns: false`"),
           "load_claude_md": Param("list", [], "Roles rendered with `omitClaudeMd: false`"),
           "authority": Param("str", "",
                              "What a brief answers to (a plan, a spec); empty renders "
                              f"\"{roles.NO_AUTHORITY}\""),
           "scoped_test": Param("str", "", "The command that runs only the tests a unit touches"),
           "full_gate": Param("str", "", "The command that runs the project's full gate"),
           "senior_triggers": Param("list", list(roles.SENIOR_TRIGGERS),
                                    "What sends a unit to senior-developer instead of developer"),
           "operator_rules": Param("list", [],
                                   "Rules the operator gives every role, rendered above the "
                                   "project's own"),
           "handback_checks": Param("list", [],
                                    "Lines the operator adds to every role's hand-back "
                                    "checklist, each `Key: text`"),
           "high_risk_paths": Param("list", [],
                                    "Paths that make a phase high-risk, so qa-engineer runs "
                                    "beside code-reviewer"),
           "escalate_after": Param("int", 2,
                                   "Failed or handed-back attempts by developer before a unit "
                                   "goes to senior-developer"),
           "housekeeping": Param("bool", True,
                                 "Whether the rules tell the coordinator to launch "
                                 "technical-writer and close the phase when a fix pass closes"),
           "rules_file": Param("str", roles.RULES_FILE,
                               "Where the coordinator's build rules are rendered"),
       })
def agent_roles(ctx, params) -> Findings:
    f = Findings()
    try:
        todo = roles.plan(ctx, params)
    except roles.UnknownSlot as exc:
        f.error(str(exc))
        return f
    for msg in todo.problems:
        f.error(f"{msg} — nothing is rendered until this is fixed")
    for t in todo.targets:
        rel = ctx.rel(t.path)
        if t.state == roles.MISSING:
            f.error(f"{rel}: `{t.role}` is not rendered — run `{roles.RENDER}`")
        elif t.state == roles.STALE:
            f.error(f"{rel}: differs from what the engine renders for `{t.role}` (a setting, "
                    f"an insert or the engine changed, or the file was edited) — run "
                    f"`{roles.RENDER}`")
        elif t.state == roles.CONFLICT:
            f.error(roles.conflict(ctx, t))
    for path, role in todo.orphans:
        f.warn(f"{ctx.rel(path)}: rendered for `{role}`, which is no longer rendered — run "
               f"`{roles.RENDER}` to remove it")
    return f


# A name an agent can go by. One holding `:` is a plugin's agent, defined outside the project.
_NAME = r"[A-Za-z][\w:-]*"
# The shapes a doc names an agent in. Anything looser (the word "agent" near a backticked word)
# reads a branch, a flag or a file as an agent.
SHAPES = (
    # `subagent_type=name`, quoted or not; after a colon or a space the name must be quoted
    re.compile(rf"""subagent_type["'`]?\s*=\s*["'`]?({_NAME})"""),
    re.compile(rf"""subagent_type["'`]?\s*:?\s*["'`]({_NAME})["'`]"""),
    # `name` agent, `name` subagent
    re.compile(rf"`({_NAME})` (?:sub)?agents?(?![\w-])"),
    # agent `name`, subagent `name`
    re.compile(rf"(?<![\w-])(?:sub)?agent `({_NAME})`"),
)
BUILTIN = ["general-purpose", "Explore", "Plan", "claude", "claude-code-guide",
           "statusline-setup"]
_CELL_SPLIT = re.compile(r"(?<!\\)\|")
_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")


def _cells(line: str) -> list[str]:
    return [c.strip() for c in _CELL_SPLIT.split(line.strip().strip("|"))]


def _named(lines: list[str], live: list[bool]) -> list[tuple[int, str]]:
    """(line number, name) for every agent the live lines name, in one of `SHAPES` or as a cell
    under a table header reading `Agent`."""
    out: list[tuple[int, str]] = []
    column = None                  # the `Agent` column of the table being read
    for i, line in enumerate(lines):
        if not live[i]:
            column = None
            continue
        names = [m.group(1) for shape in SHAPES for m in shape.finditer(line)]
        if "|" not in line:
            column = None
        elif column is not None:
            cells = _cells(line)
            cell = cells[column].strip("`*") if column < len(cells) else ""
            if re.fullmatch(_NAME, cell):
                names.append(cell)
        elif i + 1 < len(lines) and live[i + 1] and "|" in lines[i + 1] \
                and _SEPARATOR.match(lines[i + 1]):
            heads = [c.strip("`*") for c in _cells(line)]
            column = heads.index("Agent") if "Agent" in heads else None
        out += [(i + 1, name) for name in dict.fromkeys(names)]
    return out


@check("agent-references", scope="workspace", since="0.7.0", default="warn",
       summary="Every agent a governed doc, a skill, an agent definition or the build rules "
               "names has a definition in the agents directory.",
       question="Which agent names may your docs use that have no definition in this project "
                "(ones Claude Code or a plugin provides)?",
       rationale="A doc that launches an agent nobody defined gets a default agent or an "
                 "error, at the moment the work was meant to start.",
       params={
           "builtin": Param("list", list(BUILTIN),
                            "Agent names that need no definition here: the ones Claude Code "
                            "provides", looser="more"),
       })
def agent_references(ctx, params) -> Findings:
    f = Findings()
    adir = roles.agents_dir(ctx)
    defined = sorted(adir.glob("*.md")) if adir.is_dir() else []
    known = {p.stem for p in defined} | set(roles.ROLES) | set(params["builtin"])
    docs = ctx.workspace_docs()
    for s in ctx.project_scopes:
        if s.governed and s.gov is not None:
            docs += [p for _, p in ctx.governed_docs(s.gov)]
    sdir = ctx.root / ctx.cfg.checks["skills"].params["skills_dir"]
    docs += sorted(sdir.glob("*/SKILL.md")) if sdir.is_dir() else []
    docs += defined
    rules = ctx.cfg.checks[roles.CHECK].params["rules_file"]
    if roles.inside(rules):
        docs.append(ctx.root / rules)
    where = ctx.rel(adir)
    for path in dict.fromkeys(docs):
        if not path.is_file():
            continue
        lines = read(path).replace("\r\n", "\n").split("\n")
        for num, name in _named(lines, mask_lines(lines, ctx.markers)):
            if ":" in name or name in known:
                continue
            f.error(f"{ctx.rel(path)}:{num}: names agent `{name}`, which has no definition in "
                    f"{where} — define it or fix the name")
    return f
