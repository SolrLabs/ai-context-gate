"""Generated blocks: the tables an agent reads to decide which document to open.

Which blocks exist, and where, is configured in `[blocks]`. Every configured target must exist
and hold its markers: a deleted marker pair is reported, never silently dropped from checking.
Every cell is escaped, so a `|` in a title cannot break a table.

A `trap-index` entry may add `sources` (a glob, relative to the scope like `file`/`glob`): the
block still targets one file — the only one required to hold the marker pair — but reads every
trap in every file `sources` matches, linking a row to whichever file actually holds that entry
when it is not the block's own file. Without `sources`, a `trap-index` block covers only its own
file.
"""
from __future__ import annotations

import os
from pathlib import Path

from govern.text import eol, parse_frontmatter, read

PLACEHOLDER = "_Run `index` to generate._"


def cell(value) -> str:
    text = "—" if value is None or value == "" else str(value)
    return text.replace("|", "\\|")


def extract(ctx, text: str, bid: str) -> str | None:
    o, c = ctx.markers.open(bid), ctx.markers.close(bid)
    i, j = text.find(o), text.find(c)
    if i == -1 or j == -1 or j < i:
        return None
    return text[i + len(o): j].replace("\r\n", "\n").strip()


def replace(ctx, text: str, bid: str, content: str) -> tuple[str, bool]:
    o, c = ctx.markers.open(bid), ctx.markers.close(bid)
    i, j = text.find(o), text.find(c)
    if i == -1 or j == -1 or j < i:
        return text, False
    nl = eol(text)
    body = content.strip().replace("\n", nl)
    new = text[:i + len(o)] + nl + body + nl + text[j:]
    return new, new != text


# ---------------------------------------------------------------------------- renderers

RANK = {"control": 0, "reference": 1, "working": 2}


def doc_registry(ctx, gov: Path, index: Path) -> str:
    """Grouped by doc type (control, reference, working, then any other), each doc linked
    relative to the index that carries the table, with a working file's status beside it."""
    groups: dict[str, list] = {}
    for rel, path in ctx.governed_docs(gov):
        fm, _ = parse_frontmatter(read(path))
        if not fm:
            continue
        groups.setdefault(str(fm.get("doc_type", "?")).lower(), []).append((path, fm))
    out = []
    for kind in sorted(groups, key=lambda k: (RANK.get(k, 3), k)):
        rows = [f"**{kind[:1].upper() + kind[1:]}**", "", "| Doc | Load when |", "|---|---|"]
        for path, fm in sorted(groups[kind], key=lambda d: d[0]):
            link = os.path.relpath(path, index.parent).replace(os.sep, "/")
            status = fm.get("status")
            suffix = f" — _{cell(status)}_" if isinstance(status, str) and status.strip() else ""
            rows.append(f"| [`{cell(link)}`]({link}) | {cell(fm.get('load_when', '?'))}{suffix} |")
        out.append("\n".join(rows))
    return "\n\n".join(out)


def decision_index(ctx, log: Path) -> str:
    """Grouped by `**Topic:**` (entries without one first, ungrouped); a pointer shows where its
    rule went."""
    groups: dict[str | None, list] = {}
    for e in ctx.grammar.parse_file(log).entries:
        groups.setdefault(e.topic, []).append(e)
    out = []
    for topic in sorted(groups, key=lambda t: (t is not None, (t or "").lower())):
        rows = [] if topic is None else [f"**{cell(topic)}**", ""]
        rows += ["| ID | Decision | Status |", "|---|---|---|"]
        for e in sorted(groups[topic], key=lambda e: e.num):
            status = f"→ {e.replaced_by}" if e.replaced_by else cell(e.status)
            title = "*replaced*" if e.replaced_by else cell(e.title)
            rows.append(f"| {e.ident} | {title} | {status} |")
        out.append("\n".join(rows))
    return "\n\n".join(out) if out else "| ID | Decision | Status |\n|---|---|---|"


def trap_index(ctx, doc: Path, scope=None, sources: str | None = None) -> str:
    """Every trap in `doc`, plus (when `sources` is set) every trap in the other files it
    matches under the scope — one hand-written index spanning several files becomes
    one block, with a row linking to whichever file actually holds an entry that isn't `doc`.

    Sorted by id only when `sources` pulls entries in from elsewhere — several files' natural
    orders have no one order to preserve. Without `sources`, rows keep `doc`'s own file order,
    exactly as a hand-written index would have, whatever order the numbers happen to fall in."""
    files = [doc]
    if sources and scope is not None:
        for p in sorted(scope.gov.glob(sources)):
            if p.is_file() and p not in files:
                files.append(p)
    entries = [(e, path) for path in files for e in ctx.grammar.traps(path, ctx.trap_prefix)]
    if sources:
        entries.sort(key=lambda pair: pair[0].num)
    rows = ["| # | Trap | Bites when |", "|---|---|---|"]
    for e, path in entries:
        title = cell(e.title)
        if path != doc:
            link = os.path.relpath(path, doc.parent).replace(os.sep, "/")
            title = f"[{title}]({link})"
        rows.append(f"| {e.ident} | {title} | {cell(e.bites)} |")
    return "\n".join(rows)


def agent_roster(ctx) -> str:
    rows = ["| Agent | Model | Effort | Turns | Delegate when |", "|---|---|---|---|---|"]
    adir = ctx.root / ctx.workspace("agents_dir", ".claude/agents")
    for path in sorted(adir.glob("*.md")) if adir.is_dir() else []:
        fm, _ = parse_frontmatter(read(path))
        rows.append(f"| `{path.stem}` | {cell(fm.get('model', '?'))} | "
                    f"{cell(fm.get('effort', '?'))} | {cell(fm.get('maxTurns', '?'))} | "
                    f"{cell(fm.get('description', '?'))} |")
    return "\n".join(rows)


def _format(scope, col: dict) -> str:
    fmt = col.get("format", "plain")
    if fmt == "dir":
        return f"`{cell(scope.get('dir'))}/`"
    if fmt == "github-slug":
        up = scope.get(col.get("key", "upstream")) or ""
        if up.startswith("git@github.com:"):
            return "`" + cell(up[len("git@github.com:"):].removesuffix(".git")) + "`"
        return cell(up)
    if fmt == "id-range":
        raw = scope.get("id_range")
        return f"`{cell(scope.id_prefix)}-{cell(raw)}`" if raw else "—"
    return cell(scope.get(col["key"]))


def registry_table(ctx) -> str:
    cols = ctx.cfg.get("blocks", "registry_columns", [])
    rows = ["| " + " | ".join(c["header"] for c in cols) + " |",
            "|" + "---|" * len(cols)]
    for s in ctx.registry.scopes:
        rows.append("| " + " | ".join(_format(s, c) for c in cols) + " |")
    return "\n".join(rows)


def targets(ctx, scope=None):
    """(path, block id, builder) for every configured block, in configuration order — or, with
    `scope`, just that one project's (a project-scoped run has no workspace-wide file to check,
    so the workspace blocks are left out entirely rather than filtered)."""
    workspace_builders = {
        "agent-roster": lambda path: agent_roster(ctx),
        "registry": lambda path: registry_table(ctx),
        "decision-index": lambda path: decision_index(ctx, path),
    }
    project_builders = {
        "doc-registry": lambda path, scope, sources: doc_registry(ctx, scope.gov, path),
        "decision-index": lambda path, scope, sources: decision_index(ctx, path),
        "trap-index": lambda path, scope, sources: trap_index(ctx, path, scope, sources),
    }
    if scope is None:
        for t in ctx.cfg.get("blocks", "workspace", []):
            path = ctx.root / t["file"]
            if ctx.owned_by_project(path):
                # A project scope's own file (single-repo mode's default shape, or a workspace
                # block pointed at a file inside a project dir): that scope's own project block
                # already targets it.
                continue
            build = workspace_builders[t.get("render", t["id"])]
            yield path, t["id"], (lambda b=build, p=path: b(p))
        scopes = ctx.registry.scopes
    else:
        scopes = [scope]
    for scope in scopes:
        if not scope.governed:
            continue
        for t in ctx.cfg.get("blocks", "project", []):
            build = project_builders[t.get("render", t["id"])]
            sources = t.get("sources")
            paths = sorted(scope.gov.glob(t["glob"]), key=lambda p: (p.name != "traps.md", p.name)) \
                if "glob" in t else [scope.gov / t["file"]]
            for path in paths:
                yield path, t["id"], (lambda b=build, p=path, s=scope, src=sources: b(p, s, src))
