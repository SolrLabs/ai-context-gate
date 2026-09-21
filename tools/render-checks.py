#!/usr/bin/env python3
"""Write docs/checks.md, the reference for every built-in check, from the engine's own manifest.

    python3 tools/render-checks.py            # rewrite docs/checks.md
    python3 tools/render-checks.py --check    # exit 1 if docs/checks.md is out of date

Every fact on the page (level, scope, parameters, question, rationale) is read from the
`@check` declarations in `engine/govern/checks/`, so the page cannot say something the engine
does not do. A test renders it and compares with the committed file. Stdlib only.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "docs" / "checks.md"
sys.path.insert(0, str(REPO / "engine"))

import govern.checks  # noqa: E402,F401  (registers the built-in checks)
from govern import manifest  # noqa: E402

# Sections of the page, in order: (title, what the checks in it have in common, the check
# modules it collects). A check in a module listed nowhere lands in "Other checks", so a new
# module never drops a check from the page.
GROUPS = [
    ("Registry and checkouts",
     "The projects a governance root covers, the facts recorded about each, and the "
     "repositories they live in.",
     ("registry", "checkouts", "licences")),
    ("Decision logs and traps",
     "Numbered decision entries (`## P-12 — Title`) and trap entries (`## T-3 — Title`).",
     ("decisions",)),
    ("Docs",
     "The governed docs: their frontmatter, count, links, generated index blocks and wording.",
     ("docs", "links", "blocks", "references", "writing")),
    ("Working files",
     "A project's working-files directory and its HANDOFF.",
     ("working",)),
    ("Agents, skills and Claude Code",
     "Agent and skill definitions, hooks, and the Claude Code memory index.",
     ("agents", "hooks", "memory")),
    ("The ratchet and overrides",
     "The rules about the rules: recorded breaches, and settings that differ from the standard.",
     ("ratchet", "overrides")),
]

SCOPES = {
    "workspace": "Runs once for the governance root",
    "project": "Runs once per project",
}
APPLIES = {
    "all": "",
    "governed": ", for each project with a governed tier and a governance directory",
    "doc-set": ", for each project with a governed tier",
}
LOOSER = {
    "higher": "raised",
    "lower": "lowered",
    "more": "a value is added",
    "fewer": "a value is removed",
}


def text(s: str) -> str:
    return " ".join(s.split())


def cell(s: str) -> str:
    return text(s).replace("|", "\\|")


def toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{k} = {toml_value(x)}" for k, x in v.items()) + "}"
    return str(v)


def param_type(p: manifest.Param) -> str:
    if p.type == "enum":
        return "one of " + ", ".join(f"`{c}`" for c in p.choices)
    if p.type == "tables":
        return "list of tables"
    return p.type


def loosens(p: manifest.Param) -> str:
    out = LOOSER.get(p.looser or "", "")
    if p.empty_means_any:
        out = f"{out}, or emptied" if out else "emptied"
    return out or "—"


def level_line(chk: manifest.Check) -> str:
    level = f"`{chk.default}`"
    if chk.core:
        level += " (fixed core: can be `warn` or `error`, never `off`)"
    return level


def scope_line(chk: manifest.Check) -> str:
    line = SCOPES[chk.scope] + APPLIES[chk.applies]
    if chk.also_workspace:
        line += "; also runs against the workspace's own files"
    if chk.also_project:
        line += "; under `check --project X`, runs for that one project"
    return line


def meaning(p: manifest.Param) -> str:
    """The parameter's help, then for a list of tables the keys each table takes."""
    out = text(p.help)
    if p.fields:
        keys = ", ".join(f"`{k}`" + ("" if k in p.required else " (optional)") for k in p.fields)
        out = f"{out.rstrip('.')}. Keys: {keys}." if out else f"Keys: {keys}."
    return out


def render_check(chk: manifest.Check) -> list[str]:
    rows = [
        f"### `{chk.id}`",
        "",
        text(chk.summary),
        "",
        "| | |",
        "|---|---|",
        f"| Default level | {level_line(chk)} |",
        f"| Scope | {scope_line(chk)} |",
        f"| Ratchet | {'Yes: size breaches are recorded in the baseline' if chk.ratchets else 'No'} |",
        f"| Since | {chk.since} |",
        "",
        f"**Question:** {text(chk.question)}",
        "",
        f"**Why:** {text(chk.rationale)}",
        "",
    ]
    if chk.params:
        rows += ["| Parameter | Type | Default | Looser when | Meaning |",
                 "|---|---|---|---|---|"]
        for name, p in chk.params.items():
            rows.append(f"| `{name}` | {param_type(p)} | `{cell(toml_value(p.default))}` | "
                        f"{loosens(p)} | {cell(meaning(p))} |")
    else:
        rows.append("No parameters: set its `level` only.")
    rows.append("")
    return rows


def render() -> str:
    checks = [manifest.CHECKS[cid] for cid in manifest._ORDER
              if manifest.CHECKS[cid].origin == "engine"]
    module = {c.id: c.fn.__module__.rsplit(".", 1)[-1] for c in checks}
    placed = {m for _, _, mods in GROUPS for m in mods}
    groups = [(title, intro, [c for c in checks if module[c.id] in mods])
              for title, intro, mods in GROUPS]
    other = [c for c in checks if module[c.id] not in placed]
    if other:
        groups.append(("Other checks", "", other))

    out = [
        "# Checks",
        "",
        "For anyone choosing or tuning a project's settings: every check the engine ships, "
        "with its default level and parameters.",
        "",
        "<!-- Generated by tools/render-checks.py from the engine's check manifest. "
        "Do not edit by hand. -->",
        "",
        "Each check has a level: `off`, `warn` or `error`. At `error`, a check reports what it "
        "finds as written, errors and warnings both; at `warn`, every error becomes a warning; "
        "`off` skips it. A check whose Ratchet row says yes reports its size limits as warnings, "
        "and the ratchet turns a new or grown breach into an error. Set a level or a parameter "
        "under `[checks.<id>]` in `.context-gate/config.toml`. A lower level or a limit moved "
        "in its looser direction needs a `reason`; a list widened in its looser direction is "
        "reported until it has one. See [configuration.md](configuration.md) for the syntax and "
        "[how-it-works.md](how-it-works.md) for the model. "
        "`govern explain <id>` prints a check's effective settings in your project.",
        "",
        "| Check | Default | Summary |",
        "|---|---|---|",
    ]
    for _, _, members in groups:
        for c in members:
            out.append(f"| [`{c.id}`](#{c.id.replace('.', '')}) | `{c.default}` | "
                       f"{cell(c.summary)} |")
    out.append("")
    for title, intro, members in groups:
        if not members:
            continue
        out += [f"## {title}", ""]
        if intro:
            out += [intro, ""]
        for c in members:
            out += render_check(c)
    return "\n".join(out).rstrip("\n") + "\n"


def main(argv: list[str]) -> int:
    rendered = render()
    if argv == ["--check"]:
        current = OUT.read_text(encoding="utf-8") if OUT.is_file() else ""
        if current != rendered:
            print(f"{OUT.relative_to(REPO).as_posix()} is out of date: "
                  f"run python3 tools/render-checks.py", file=sys.stderr)
            return 1
        return 0
    if argv:
        print(__doc__, file=sys.stderr)
        return 2
    with open(OUT, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(rendered)
    print(f"wrote {OUT.relative_to(REPO).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
