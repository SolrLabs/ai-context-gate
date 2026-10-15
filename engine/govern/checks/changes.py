"""What changed in the decision logs: against the last commit, or against the branch a pull
request targets."""
from __future__ import annotations

from govern import base
from govern.decisions import heading_dash
from govern.findings import Findings
from govern.manifest import Param, check
from govern.text import shown


@check("decision-changes", scope="workspace", also_project=True, since="0.7.0",
       default="error",
       summary="A locked decision changed in place without a dated Revised line, a decision "
               "removed, or an id another branch already took. Advisory (warnings) unless run "
               "with `--base`.",
       question="Should a change to a settled decision have to say so?",
       rationale="A record that can be reworded silently is not a record; with more than one "
                 "writer, the rewording is somebody else's decision.",
       params={"locked_statuses": Param("list", ["locked"], "Statuses whose entries may only "
                                        "change with a dated Revised line")})
def decision_changes(ctx, params, scope=None) -> Findings:
    """Every other check reads the tree as it is; this one reads what changed (`govern.base`).

    Two modes. Plain `check` compares uncommitted work with `HEAD`, and everything it finds is
    a warning: an edit is seen before it is committed, and the finding goes once it is. No
    existing hook or CI job passes `--base`, so an upgraded project's runs gain warnings at
    most. `check --base <ref>` compares the branch with where it left `<ref>`, and is the gate:
    the findings below are errors, and a comparison that cannot be made is one too (a warning
    for a nested project's own repository, when the run is of the whole workspace).

    Restricted to one project (`check --project X`) or to the workspace (`--workspace-only`),
    the findings are that scope's own, but an id is still looked for in every log: a decision
    moved to another project's log has not been removed."""
    f = Findings()
    gate = ctx.base is not None
    cmp = base.compare(ctx, traps=gate)
    locked = {str(status).lower() for status in params["locked_statuses"]}

    def mine(item: base.Item) -> bool:
        """Whether an entry is this run's to report on."""
        if scope is not None:
            return item.scope.name == scope.name
        return item.scope.is_workspace if ctx.workspace_only else True

    def asked(repo: base.Repo) -> bool:
        """Whether a repository that could not be compared holds anything this run reads."""
        if scope is not None:
            return scope.name in repo.scopes
        if ctx.workspace_only:
            return repo.root or ctx.registry.workspace.name in repo.scopes
        return True

    def text(item: base.Item) -> str:
        return base.compared_text(item.entry, ctx.markers)

    if gate:
        # The caller asked for a gate, so it fails closed. The repository the governance root
        # is in not comparing is an error. So is any repository of the one project a run was
        # asked about (`--project X`), a `--path` snapshot with no `--history-from` among them:
        # that run was asked about nothing else, so it compared nothing. In a run of the whole
        # workspace a nested project's own repository where the ref means nothing is a
        # warning, and the others are still compared.
        for repo in cmp.without_base:
            if not asked(repo):
                continue
            if repo.root:
                f.error(f"decision-changes: could not compare with {ctx.base} — {repo.reason}")
                continue
            msg = (f"decision-changes: could not compare {repo.label} with {ctx.base} — "
                   f"{repo.reason}")
            if scope is not None:
                f.error(msg)
            else:
                f.warn(msg)

    # An id that pairs more than one way (`Pairs.unpaired_base`, `unpaired_current`) is in
    # neither list: the check says nothing rather than guess which copy was which.
    matched, removed, _ = base.match(cmp.side("base", base.DECISION),
                                     cmp.side("current", base.DECISION))
    for was, now in matched:
        # A settled decision changed in place. Becoming a pointer is the legal way out, and
        # whether what it points at exists is decision-log's business.
        if was.entry.status not in locked or not mine(now) or now.entry.replaced_by:
            continue
        if text(was) == text(now) or base.dated_revision(was.entry, now.entry, ctx.markers):
            continue
        f.error(f"{now.log}: {now.entry.ident} ({was.entry.status}) changed without a new "
                f"dated **Revised:** line — add `**Revised:** YYYY-MM-DD (what changed)`, or "
                f"supersede it with a new decision")

    # A removed decision. With a file on disk unreadable the id may only be out of sight, and
    # that file is reported by the checks that read it.
    for was in removed if cmp.complete else []:
        if not mine(was):
            continue
        e = was.entry
        msg = (f"{was.log}: {e.ident} ({e.status or 'no status'}) was removed — a decision is "
               f"superseded, not deleted: reduce it to `{'#' * ctx.grammar.level} {e.ident}"
               f"{heading_dash(e.heading)}Replaced by <id>`")
        if e.status in locked:
            f.error(msg)
        else:
            f.warn(msg)

    if gate:
        # An id this branch added (it is in the tree and was not at the base) that the base
        # branch has also added since, saying something else. The same text on both sides is a
        # cherry-pick, or the base merged in. Different text is another entry under the same
        # id, or this branch's own entry that the base branch took (a squash merge) before the
        # branch went on to edit it: nothing cheap tells the two apart, so the message gives
        # the way out of each. The title it quotes is the other side's text, printed with its
        # control characters escaped (`text.shown`).
        before = {item.key for item in cmp.side("base")}
        rivals: dict[tuple, list[base.Item]] = {}
        for item in cmp.side("theirs"):
            rivals.setdefault(item.key, []).append(item)
        for repo in cmp.repos:
            if repo.tip is None:
                continue        # not compared: nothing says what this branch added here
            for ours in repo.current:
                theirs = [] if ours.key in before or not mine(ours) else rivals.get(ours.key, [])
                theirs = [t for t in theirs if t.log == ours.log] or theirs
                if theirs and all(text(t) != text(ours) for t in theirs):
                    title = shown(theirs[0].entry.title)
                    f.error(f"{ours.log}: {ours.entry.ident} was also added on {ctx.base} "
                            f"since this branch began (\"{title}\") — if it is "
                            f"the same entry, merge {ctx.base} into this branch; if not, "
                            f"renumber yours to an id neither side uses and update what cites it")
    return f if gate else f.capped("warn")
