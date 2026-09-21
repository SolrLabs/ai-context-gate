"""Decision logs and trap files."""
from __future__ import annotations

import re

from govern.decisions import id_key, mask_lines
from govern.findings import Findings
from govern.manifest import Param, check


def log_for(ctx, scope):
    return ctx.workspace_log if scope.is_workspace else ctx.scope_log(scope)


def _history_label(line: str, labels: list[str]) -> str | None:
    """The configured label this line opens with, bold (`**Earlier:**`) or plain (`Earlier:` as
    the line's first word), case-insensitive — or None. Returned in the label's configured
    spelling, not the line's own case."""
    text = line.strip()
    for label in labels:
        esc = re.escape(label)
        if re.match(rf"^\*\*{esc}\s*:\*\*", text, re.IGNORECASE) \
                or re.match(rf"^{esc}\s*:", text, re.IGNORECASE):
            return label
    return None


@check("decision-log", scope="project", since="0.4.0", ratchets=True, applies="governed", also_workspace=True,
       summary="Decision entries are well-formed, unique, in range and in order, carry a known "
               "status and the required fields, and stay short.",
       question="Which statuses and fields does a decision entry need, and how long may one be?",
       rationale="The log is read by index; an entry the index cannot see, or one that is really "
                 "a design doc, defeats that.",
       params={
           "max_words": Param("int", 250, "Words per entry before it is flagged",
                              looser="higher"),
           "statuses": Param("list", ["locked", "provisional", "deferred"],
                             "Allowed **Status:** values (case-insensitive). There is no "
                             "'superseded': a replaced decision becomes a one-line pointer",
                             looser="more"),
           "required_fields": Param("list", ["Rule", "Why"],
                                    "Bold fields every entry carries, e.g. **Rule:**",
                                    looser="fewer"),
           "ascending": Param("bool", True, "Entries must appear in ascending id order"),
       })
def decision_log(ctx, params, scope) -> Findings:
    f = Findings()
    log = log_for(ctx, scope)
    if log is None:
        return f    # single-repo mode, no [workspace] decision_log of its own to check
    if scope.is_workspace and ctx.owned_by_project(log):
        # A project scope's own log (single-repo mode's default shape, or any project whose
        # own log happens to be the workspace's): that scope's own run already checks it.
        return f
    label = ctx.rel(log)
    if not log.exists():
        f.error(f"{label}: decision log is missing")
        return f
    parsed = ctx.grammar.parse_file(log)
    for line, heading in parsed.malformed:
        f.error(f"{label}:{line}: '{heading[:80]}' is not a decision entry "
                f"heading — nothing reads an entry the grammar rejects")
    prefix, rng = scope.id_prefix, scope.id_range
    statuses = tuple(params["statuses"])
    known = {e.key for e in parsed.entries}
    seen: set[int] = set()
    last = 0
    for e in parsed.entries:
        eid = e.ident
        if prefix and e.prefix != prefix:
            f.error(f"{label}: {eid} uses prefix '{e.prefix}', expected '{prefix}'")
        if e.num in seen:
            f.error(f"{label}: duplicate decision id {eid}")
        seen.add(e.num)
        if params["ascending"] and e.num < last:
            f.error(f"{label}: {eid} is out of ascending order — the log is read by index")
        last = e.num
        if rng and not (rng[0] <= e.num <= rng[1]):
            f.error(f"{label}: {eid} outside declared range {rng[0]}-{rng[1]}")
        if e.replaced_by:
            # A pointer carries nothing but where the rule went.
            if e.body.strip():
                f.error(f"{label}: {eid} is a pointer to {e.replaced_by}, so it carries no body — "
                        f"the rule lives in {e.replaced_by}")
            if id_key(e.replaced_by) not in known:
                f.error(f"{label}: {eid} points to {e.replaced_by}, which is not in this log")
            continue
        if e.status == "superseded" and "superseded" not in statuses:
            f.error(f"{label}: {eid} is superseded — rewrite it in place if the rule changed, or "
                    f"reduce it to `{'#' * ctx.grammar.level} {eid} — Replaced by <id>` if another "
                    f"decision replaced it; superseded text misleads whoever reads it next")
            continue
        if e.status is None:
            f.error(f"{label}: {eid} has no **Status:**")
        elif e.status not in statuses:
            f.error(f"{label}: {eid} status '{e.status}' not in {statuses}")
        for name, present in e.fields.items():
            if not present:
                f.error(f"{label}: {eid} has no **{name}:**")
        if e.words > params["max_words"]:
            f.warn(f"{label}: {eid} is {e.words} words, over decision-log max_words="
                   f"{params['max_words']} — cut history and restatement, not a rule or a road "
                   f"not taken")
    return f


@check("decision-history", scope="project", since="0.4.0", applies="governed", default="warn",
       also_workspace=True,
       summary="A decision entry's body carries no history label — a rewritten entry states "
               "only today's rule.",
       question="When a decision entry is rewritten, does it ever keep a line naming what it "
                "used to say?",
       rationale="An agent reading the entry pays for something no longer true; git already "
                 "keeps the old text.",
       params={"labels": Param("list", ["Earlier", "Previously", "Formerly", "Superseded",
                                        "Was"],
                               "History labels flagged at the start of a body line, bold "
                               "(`**Earlier:**`) or plain (`Earlier:`)", looser="fewer")})
def decision_history(ctx, params, scope) -> Findings:
    f = Findings()
    log = log_for(ctx, scope)
    if log is None:
        return f
    if scope.is_workspace and ctx.owned_by_project(log):
        return f
    if not log.exists():
        return f
    label = ctx.rel(log)
    for e in ctx.grammar.parse_file(log).entries:
        if e.replaced_by:
            continue           # a pointer has no body
        lines = e.body.split("\n")
        live = mask_lines(lines, ctx.markers)
        for i, line in enumerate(lines):
            if not live[i]:
                continue
            hit = _history_label(line, params["labels"])
            if hit:
                f.warn(f"{label}: {e.ident} keeps history (\"{hit}:\") — a rewritten entry "
                       f"states only today's rule; git keeps the old one")
                break
    return f


@check("trap-ids", scope="project", since="0.4.0", applies="governed",
       summary="A trap id is owned by exactly one of a project's trap files.",
       question="Do your projects split traps across more than one file?",
       rationale="The same id in two files leaves `show`, `find` and `trap-add` no principled "
                 "way to pick.")
def trap_ids(ctx, params, scope) -> Findings:
    f = Findings()
    seen = {}
    for doc in ctx.trap_files(scope):
        for e in ctx.grammar.traps(doc, ctx.trap_prefix):
            prior = seen.get(e.num)
            if prior is not None:
                f.error(f"{scope.name}: {ctx.trap_prefix}-{e.num} appears in both "
                        f"{ctx.rel(prior)} and {ctx.rel(doc)}")
            else:
                seen[e.num] = doc
    return f


@check("trap-entries", scope="project", since="0.4.0", applies="governed", default="warn",
       summary="Every trap entry says when it bites: the **Bites when:** line is what the trap "
               "index shows.",
       question="Do your trap entries carry a **Bites when:** line?",
       rationale="An entry without one shows as a dash in the index, so nobody reading the "
                 "index can tell whether it applies to them.")
def trap_entries(ctx, params, scope) -> Findings:
    f = Findings()
    for doc in ctx.trap_files(scope):
        for e in ctx.grammar.traps(doc, ctx.trap_prefix):
            if not e.bites:
                f.warn(f"{scope.name}: {ctx.rel(doc)} {e.ident} has no **Bites when:** — the "
                       f"trap index shows it as a dash")
    return f
