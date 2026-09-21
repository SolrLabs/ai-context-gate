"""Governed documents: presence, frontmatter, review age, and size.

One doc check applies to every scope, workspace docs included, so the two can never drift into
different rules.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from govern.findings import Findings
from govern.manifest import Param, check
from govern.text import Unreadable, parse_frontmatter, read, word_count

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def status_keyword(status) -> str:
    """The first word of a status, lowercased: a working file's status leads with its state."""
    if isinstance(status, list):
        status = " ".join(status)
    words = str(status or "").strip().split()
    return words[0].rstrip(",").lower() if words else ""


def check_doc(ctx, params: dict, path: Path, label: str, f: Findings) -> None:
    try:
        raw = read(path)
    except Unreadable as exc:
        f.error(f"{label}: {exc.reason}")
        return
    fm, body = parse_frontmatter(raw)
    if not fm:
        f.error(f"{label}: no frontmatter")
        return
    for key in params["required_keys"]:
        if not fm.get(key):
            f.error(f"{label}: frontmatter missing '{key}'")
    dt = fm.get("doc_type")
    if dt and dt not in params["doc_types"]:
        f.error(f"{label}: doc_type '{dt}' not in {tuple(params['doc_types'])}")
    aud = fm.get("audience")
    if aud and aud not in params["audiences"]:
        f.error(f"{label}: audience '{aud}' not in {tuple(params['audiences'])}")
    if dt == "working" and not fm.get("status"):
        f.error(f"{label}: working file needs 'status' — it is what says which plan is active")
    elif dt == "working" and params["working_statuses"]:
        status = fm.get("status", "")
        if status_keyword(status) not in params["working_statuses"]:
            f.error(f"{label}: status '{status}' does not start with one of "
                    f"{tuple(params['working_statuses'])}")
    lr = fm.get("last_reviewed")
    if lr:
        if not isinstance(lr, str) or not DATE_RE.match(lr):
            f.error(f"{label}: last_reviewed '{lr}' is not YYYY-MM-DD")
        else:
            try:
                reviewed = date.fromisoformat(lr)
            except ValueError:
                f.error(f"{label}: last_reviewed '{lr}' is not YYYY-MM-DD")
            else:
                age = (date.today() - reviewed).days
                if age < 0:
                    f.error(f"{label}: last_reviewed '{lr}' is in the future")
                elif dt in params["stale_types"] and age > params["stale_days"]:
                    f.warn(f"{label}: last_reviewed {age}d ago exceeds "
                           f"doc-frontmatter stale_days={params['stale_days']}")
    for link in fm.get_list("related"):
        if link.startswith(("http://", "https://")):
            continue
        if not (path.parent / link).resolve().exists():
            f.error(f"{label}: related link '{link}' does not resolve")
    if dt == "working":
        wc = word_count(body, ctx.markers)
        if wc > params["max_working_words"]:
            f.warn(f"{label}: {wc} words exceeds doc-frontmatter max_working_words="
                   f"{params['max_working_words']} — put permanent content behind an index, or "
                   f"delete what is finished")


DOC_PARAMS = {
    "required_keys": Param("list", ["doc_type", "purpose", "audience", "load_when",
                                    "last_reviewed"], "Frontmatter keys every governed doc carries",
                           looser="fewer"),
    "doc_types": Param("list", ["control", "reference", "working"], "Allowed doc_type values",
                       looser="more"),
    "audiences": Param("list", ["agent", "human", "both"], "Allowed audience values",
                       looser="more"),
    "working_statuses": Param("list", ["active", "held", "planned", "complete", "superseded"],
                              "A working file's status must start with one of these; "
                              "empty allows free text", looser="more", empty_means_any=True),
    "stale_days": Param("int", 120, "Days since last_reviewed before a review is due",
                        looser="higher"),
    "stale_types": Param("list", ["control", "reference"],
                         "doc_types that go stale; working files are curated instead"),
    "max_working_words": Param("int", 6000, "Words in a working file before it is flagged",
                               looser="higher"),
}


@check("doc-frontmatter", scope="project", since="0.4.0", ratchets=True, applies="governed",
       summary="Every governed doc carries valid frontmatter, a current review date, and "
               "resolvable related links; working files carry a status and stay bounded.",
       question="Which frontmatter keys, doc types and working-file statuses do your docs use, "
                "and how often should control and reference docs be reviewed?",
       rationale="Frontmatter is what tells an agent whether and when to open a doc.",
       params=DOC_PARAMS)
def doc_frontmatter(ctx, params, scope) -> Findings:
    f = Findings()
    for rel, path in ctx.governed_docs(scope.gov):
        # Single-repo mode's one scope *is* the governance root: `scope.name/rel`
        # would prefix the repo-relative path with the root's own directory name a second
        # time. `ctx.rel(path)` is already repo-relative — the same label `workspace_docs`
        # uses below, so a finding on the same kind of file reads the same under either
        # section.
        label = ctx.rel(path) if ctx.registry.single else f"{scope.name}/{rel}"
        check_doc(ctx, params, path, label, f)
    return f


@check("workspace-docs", scope="workspace", since="0.4.0",
       summary="The workspace's own docs exist and pass the same doc check as project docs.",
       question="Which docs does the workspace root require, and which does it govern?",
       rationale="The layer that governs everything else is governed by the same rules.")
def workspace_docs(ctx, params) -> Findings:
    f = Findings()
    for rel in ctx.workspace("required_docs", []):
        if not (ctx.root / rel).exists():
            f.error(f"{ctx.workspace_label}: {rel} missing")
    doc_params = ctx.cfg.checks["doc-frontmatter"].params
    for path in ctx.workspace_docs():
        if ctx.owned_by_project(path):
            continue   # a project scope's own doc — same file, same rules, already checked
        check_doc(ctx, doc_params, path, ctx.rel(path), f)
    return f


@check("governed-doc-count", scope="project", since="0.4.0", ratchets=True, applies="governed", default="error",
       summary="Bounds how many governed docs a project carries.",
       question="How many governed docs should a project need before it is time to merge or "
                "archive some?",
       rationale="The doc system should stay possible to grow without growing by habit.",
       params={"max_docs": Param("int", 14, "Governed docs per project", looser="higher")})
def governed_doc_count(ctx, params, scope) -> Findings:
    f = Findings()
    n = len(ctx.governed_docs(scope.gov))
    if n > params["max_docs"]:
        f.warn(f"{scope.name}: {n} governed docs exceeds governed-doc-count max_docs={params['max_docs']} "
               f"— adding one should be a deliberate call; merge docs that duplicate each other")
    return f


@check("doc-set", scope="project", since="0.4.0", applies="doc-set",
       summary="A project whose tier requires governance carries the full doc set.",
       question="Which docs must every governed project carry?",
       rationale="A missing doc is discovered by the gate, not by the agent that needed it.")
def doc_set(ctx, params, scope) -> Findings:
    f = Findings()
    gov_rel = scope.get("governance")
    if scope.gov is None or not scope.gov.is_dir():
        f.error(f"{scope.name}: governance dir '{gov_rel}' does not exist")
        return f
    required = list(ctx.projects("required_docs", []))
    for rule in ctx.projects("required_when", []):
        if scope.get(rule["key"]):
            required += rule["docs"]
    for rel in required:
        if not (scope.gov / rel).exists():
            f.error(f"{scope.name}: required governance doc missing: {rel}")
    return f
