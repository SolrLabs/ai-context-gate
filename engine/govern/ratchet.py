"""The ratchet: size limits are warnings, but each specific breach is recorded, and one that is
new or growing is an error. Accepting a bigger number is a decision made on purpose, never a
side effect of running a maintenance command.
"""
from __future__ import annotations

import json
from pathlib import Path

from govern.checks.working import handoff_is_glob, handoff_paths, handoff_words
from govern.text import has_bom, parse_frontmatter, read, word_count, write


class BaselineUnreadable(Exception):
    pass


# What to do about a *new* breach, by key kind (the part of a key before its first `:`), where
# the kind has a better first answer than recording it: a file just written over its limit is
# fixed, not accepted. A kind with no entry gets the ratchet check's generic advice.
NEW_BREACH_REMEDY = {
    "working_file_words": "split it into smaller files, put permanent content behind an index, "
                          "or delete what is finished",
}

# What a breach of a kind is measured in and which setting holds its limit, as the limit's own
# warning words it: `at {val} words (doc-frontmatter max_working_words=6000)`. A kind with no
# entry names neither.
BREACH_SETTING = {
    "working_file_words": lambda ctx: (
        "words", f"doc-frontmatter max_working_words="
                 f"{ctx.cfg.checks['doc-frontmatter'].params['max_working_words']}"),
}


def kind(key: str) -> str:
    """A key's kind: `working_file_words` of `working_file_words:docs/plan.md`."""
    return key.partition(":")[0]


def working_words_key(ctx, path) -> str:
    """The key of one working file's word-count breach: its real, repo-relative path
    (`Context.rel`), the same whichever check names the breach."""
    return f"working_file_words:{ctx.rel(path)}"


def compute(ctx, scope=None) -> dict[str, int]:
    """Every current breach of the ratcheted size rules, keyed by a stable identifier. A rule
    whose check is off does not ratchet.

    With `scope`, only that one project's breaches — used under `check --project X`, so a
    check of one project sees only its own numbers, never another's."""
    checks = ctx.cfg.checks
    on = lambda cid: checks[cid].level != "off" and checks[cid].ratchet
    breaches: dict[str, int] = {}
    scopes = ctx.project_scopes if scope is None else [scope]

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
                    breaches[working_words_key(ctx, path)] = wc
        if on("handoff-words"):
            glob = handoff_is_glob(s)
            for path in handoff_paths(ctx, s):
                if not path.exists():
                    continue
                wc = handoff_words(ctx, path)
                if wc > checks["handoff-words"].params["max_words"]:
                    key = f"handoff_words:{s.name}"
                    if glob:
                        key += f":{ctx.rel(path).replace(chr(92), '/')}"
                    breaches[key] = wc
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
    elif kind == "handoff_words":
        # `label`, or `label:path` for one file of a glob.
        return rest == scope.name or rest.startswith(scope.name + ":")
    else:
        # governed_docs: `rest` is the label alone.
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


def reasons_path(ctx) -> Path:
    """Why each raised or added breach was accepted: a flat key -> text file by the baseline."""
    return ctx.baseline_path.with_name("baseline-reasons.json")


def arg(key: str) -> str:
    """A key as a command-line word: quoted when it holds a space."""
    return f'"{key}"' if " " in key else key


def parse_reasons(text: str) -> dict[str, str]:
    """A reasons file's entries; ValueError (or JSONDecodeError) when it is not one."""
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    return {str(k): str(v) for k, v in data.items()}


def load_reasons(ctx) -> dict[str, str]:
    """The recorded reasons, read leniently (a BOM is fine); a missing or unreadable file holds
    none. `govern baseline`, which rewrites the file, reads it with `load_reasons_strict`."""
    try:
        return parse_reasons(reasons_path(ctx).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError):
        return {}


def load_reasons_strict(ctx) -> dict[str, str]:
    """The recorded reasons. A file that exists and does not parse raises
    `BaselineUnreadable`: treating it as empty would let the next raise delete every other
    reason."""
    path = reasons_path(ctx)
    if not path.exists():
        return {}
    try:
        return parse_reasons(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise BaselineUnreadable(f"{ctx.rel(path)} is not a valid reasons file ({exc})") from exc


def write_reasons(ctx, reasons: dict[str, str]) -> None:
    path = reasons_path(ctx)
    if not reasons:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    bom = "\ufeff" if has_bom(path) else ""
    write(path, bom + json.dumps(dict(sorted(reasons.items())), indent=2, sort_keys=True) + "\n")
