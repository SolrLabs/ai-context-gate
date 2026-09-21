"""The ratchet: size limits are warnings, but each specific breach is recorded, and one that is
new or growing is an error. Accepting a bigger number is a decision made on purpose, never a
side effect of running a maintenance command.
"""
from __future__ import annotations

import json

from govern.checks.working import handoff_path, handoff_words
from govern.text import parse_frontmatter, read, word_count, write


class BaselineUnreadable(Exception):
    pass


def compute(ctx, scope=None) -> dict[str, int]:
    """Every current breach of the ratcheted size rules, keyed by a stable identifier. A rule
    whose check is off does not ratchet.

    With `scope`, only that one project's breaches — used under `check --project X`, so a
    check of one project sees only its own numbers, never another's."""
    checks = ctx.cfg.checks
    on = lambda cid: checks[cid].level != "off" and checks[cid].ratchet
    breaches: dict[str, int] = {}
    scopes = ctx.registry.scopes if scope is None else [scope]

    if on("decision-log"):
        limit = checks["decision-log"].params["max_words"]
        logs = []
        if scope is None and ctx.workspace_log is not None \
                and not ctx.owned_by_project(ctx.workspace_log):
            # A project scope's own log covers this file already (single-repo mode's default
            # shape, or any project whose own log happens to be the workspace's): one key per
            # breach, never `decision_words:workspace:…` and `decision_words:<scope>:…` both.
            logs.append((ctx.workspace_label, ctx.workspace_log))
        logs += [(s.name, ctx.scope_log(s)) for s in scopes if s.governed]
        for label, log in logs:
            if not log.exists():
                continue
            for e in ctx.grammar.parse_file(log).entries:
                if e.words > limit:
                    breaches[f"decision_words:{label}:{e.ident}"] = e.words

    for s in scopes:
        if not s.governed:
            continue
        docs = ctx.governed_docs(s.gov)
        if on("governed-doc-count") and len(docs) > checks["governed-doc-count"].params["max_docs"]:
            breaches[f"governed_docs:{s.name}"] = len(docs)
        if on("doc-frontmatter"):
            limit = checks["doc-frontmatter"].params["max_working_words"]
            for _, path in docs:
                fm, body = parse_frontmatter(read(path))
                if fm.get("doc_type") != "working":
                    continue
                wc = word_count(body, ctx.markers)
                if wc > limit:
                    breaches[f"working_file_words:{ctx.rel(path)}"] = wc
        path = handoff_path(ctx, s)
        if on("handoff-words") and path is not None and path.exists():
            wc = handoff_words(ctx, path)
            if wc > checks["handoff-words"].params["max_words"]:
                breaches[f"handoff_words:{s.name}"] = wc
    return breaches


def owns(ctx, scope, key: str) -> bool:
    """Whether a baseline key belongs to `scope` — the shapes `compute` builds, read backwards.
    Used under `check --project X` to keep the baseline comparison to X's own recorded entries,
    so a `--project` run never reports another project's baselined breach as "no longer
    breaches", or touches it at all.

    A `working_file_words` key always names the real, repo-relative path (`Context.rel` maps a
    `--path` snapshot back to it) — so it is matched against `scope`'s own real directory too:
    `ctx.real_scope_dir`, the directory `--path` swapped for the snapshot, when this is that same
    run; `scope.gov` (already the real directory) otherwise. Paths are compared resolved, since
    an identity check by string would miss a `..`-free but differently-spelled equivalent path."""
    kind, _, rest = key.partition(":")
    if kind == "working_file_words":
        real_gov = ctx.real_scope_dir if ctx.real_scope_dir is not None else scope.gov
        if real_gov is None:
            return False
        return (ctx.root / rest).resolve().is_relative_to(real_gov.resolve())
    if kind == "decision_words":
        # `rest` is `label:ident`; split from the right — an id never contains ':', but a scope
        # name (`label`) might, so splitting from the left would cut it apart.
        label = rest.rsplit(":", 1)[0]
    else:
        # governed_docs / handoff_words: `rest` is the label alone.
        label = rest
    return label == scope.name


def load_baseline(ctx) -> dict[str, int]:
    """The recorded breaches. An unreadable baseline raises: treating it as empty would let the
    next `baseline` run overwrite every recorded breach."""
    path = ctx.baseline_path
    if not path.exists():
        return {}
    try:
        return parse_baseline(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
        raise BaselineUnreadable(f"{ctx.rel(path)} is not a valid baseline ({exc})") from exc


def parse_baseline(text: str) -> dict[str, int]:
    """A baseline's entries; ValueError (or JSONDecodeError, TypeError) when it is not one."""
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    return {str(k): int(v) for k, v in data.items()}


def write_baseline(ctx, entries: dict[str, int]) -> None:
    path = ctx.baseline_path
    path.parent.mkdir(parents=True, exist_ok=True)
    write(path, json.dumps(dict(sorted(entries.items())), indent=2, sort_keys=True) + "\n")
