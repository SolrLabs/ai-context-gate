"""What a branch did to governance: the report `govern diff` prints.

A report, never a gate. It says what changed in the decision logs, the trap files and the
ratchet baseline since a base (`HEAD`, or with `--base <ref>` the commit the branch left that
ref at), and which commit each repository was compared with. Whether a change is allowed is
the `decision-changes` check's business, not this module's.

It reads the two sides `base.compare` built and decides nothing about them that `govern.base`
already decides: which entries are the same entry (`base.match`), whether their text differs
(`base.compared_text`) and whether a change says so (`base.dated_revision`). The one thing it
asks git itself is what the baseline file held at the commit `compare` found.

The text is valid Markdown, so CI can append it to a pull request. Its layout (the words, the
punctuation, the arrows) is ASCII; the titles and paths in it are the project's own text, and
in the text form a control character in a title is printed as `\\xNN`, so no title steers the
terminal or the job summary the report is put in. The layout and the keys of the JSON form are
a contract other tools read: `engine/tests/expected/` holds one of each.
"""
from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field

from govern import base, ratchet
from govern.decisions import STATUS_RE, Entry, id_key
from govern.text import shown

# The check whose `locked_statuses` say which entries are settled.
SETTLED_BY = "decision-changes"

# The first column: as wide as its longest word, `superseded`.
WIDTH = 10

NO_STATUS = "no status"


@dataclass(frozen=True)
class Row:
    """One line of the report, both ways it is given: every line of the text is one entry of
    the JSON, built together so the two cannot disagree."""
    data: dict          # the entry `--json` gives
    line: str           # the text's line, without its leading `- `


@dataclass
class Report:
    base: str                               # the ref as given, or `HEAD`
    decisions: list[Row] = field(default_factory=list)
    traps: list[Row] = field(default_factory=list)
    baseline: list[Row] = field(default_factory=list)
    compared: list[Row] = field(default_factory=list)


def _change(word: str, rest: str, **data) -> Row:
    return Row({"change": word, **data}, f"{word:<{WIDTH}} {rest}")


def _but_status(entry: Entry) -> Entry:
    """The entry without the line its status was read from, to ask whether anything else
    differs."""
    m = STATUS_RE.search(entry.body)
    if m is None:
        return entry
    start = entry.body.rfind("\n", 0, m.start()) + 1
    end = entry.body.find("\n", m.end())
    end = len(entry.body) if end < 0 else end + 1
    return dataclasses.replace(entry, body=entry.body[:start] + entry.body[end:])


def _entries(ctx, cmp: base.Comparison, kind: str, scope) -> list[Row]:
    """One row per changed decision or trap, each with the first word of these that applies:
    `removed`; `added`; `superseded` (it became a pointer, or a pointer to another id); `moved`
    (the same text, in another log); `revised` (a settled entry whose change carries a dated
    Revised line); `status` (only the status differs); `changed` (any other text).

    Every line is `<word> <log> <id> ...`, where the log is the one the entry is in now (for
    `removed`, the one it was in). A decision now in another log than at the base ends
    `, from <old log>` whatever its word, so one moved and reworded keeps the move. The rows
    are in order of that log's path, then the id's prefix, then its number as a number.

    A trap has no status and is not superseded, so it is `removed`, `added` or `changed`; one
    that only moved to another of its project's trap files has not changed.

    An id that pairs more than one way (`base.match` leaves it out) is reported without a
    guess at which copy was which: each copy now with no pair as `changed`, each copy at the
    base with none as `removed`.

    With `scope`, the entries whose log at the base or now is that project's; an id is still
    looked for in every log, so a decision moved to another project's log has not been
    removed, and is a line of the project it left as of the one it came to."""
    markers = ctx.markers
    decision = kind == base.DECISION
    settled = {str(s).lower() for s in ctx.cfg.checks[SETTLED_BY].params["locked_statuses"]}
    # An entry in a repository that could not be compared has no base to be new against.
    known = {id(item) for repo in cmp.repos if repo.commit is not None for item in repo.current}

    def mine(*items: base.Item) -> bool:
        return scope is None or any(item.scope.name == scope.name for item in items)

    def text(entry: Entry) -> str:
        return base.compared_text(entry, markers)

    def state(entry: Entry) -> str:
        return f" ({entry.status or NO_STATUS})" if decision else ""

    found: list[tuple[tuple, Row]] = []

    def add(word: str, item: base.Item, rest: str = "", was: base.Item | None = None,
            shown: Entry | None = None, revised: str | None = None) -> None:
        """One row for `item`, the entry where the line says it is. `was` is the same entry at
        the base, when it has one; `shown` the entry whose title and status the row gives,
        when that is not `item`'s own."""
        e = item.entry
        shown = shown or e
        line = f"{item.log} {e.ident}{rest}"
        data = {"log": item.log, "id": e.ident, "title": shown.title}
        if decision:
            elsewhere = was is not None and was.log != item.log
            differs = was is not None and was.entry.status != shown.status
            data.update(status=shown.status, was_status=was.entry.status if differs else None,
                        was_log=was.log if elsewhere else None, replaced_by=e.replaced_by,
                        revised=revised)
            if elsewhere:
                line += f", from {was.log}"
        found.append(((item.log, e.prefix, e.num, word), _change(word, line, **data)))

    pairs = base.match(cmp.side("base", kind), cmp.side("current", kind))

    # With a file on disk unreadable an id may only be out of sight (`Comparison.complete`).
    for was in pairs.removed + pairs.unpaired_base if cmp.complete else []:
        if mine(was):
            add("removed", was, state(was.entry))
    for now in pairs.added:
        if mine(now) and id(now) in known:
            # The text prints a title escaped (`text.shown`); the JSON form keeps it as
            # written, which JSON escapes itself.
            add("added", now, f" - {shown(now.entry.title)}{state(now.entry)}")
    for now in pairs.unpaired_current:
        if mine(now) and id(now) in known:
            add("changed", now, state(now.entry))
    for was, now in pairs.matched:
        if not mine(was, now):
            continue
        old, new = was.entry, now.entry
        same = text(old) == text(new)
        if not decision:
            if not same:
                add("changed", now)
        elif new.replaced_by and (not old.replaced_by
                                  or id_key(old.replaced_by) != id_key(new.replaced_by)):
            # What was superseded is the decision as it stood: its title and status then.
            add("superseded", now, f" -> {new.replaced_by}", was, shown=old)
        elif same:
            if was.log != now.log:
                add("moved", now, state(new), was)
        elif old.status in settled and base.dated_revision(old, new, markers):
            newest = max(day for day, _ in base.revised_lines(new, markers))
            add("revised", now, f"{state(new)}, Revised {newest}", was, revised=newest)
        elif old.status != new.status and text(_but_status(old)) == text(_but_status(new)):
            add("status", now, f" {old.status or NO_STATUS} -> {new.status or NO_STATUS}", was)
        else:
            add("changed", now, state(new), was)
    return [row for _, row in sorted(found, key=lambda pair: pair[0])]


def _baseline_repo(ctx, cmp: base.Comparison) -> tuple[base.Repo | None, str | None]:
    """`(the compared repository the baseline file is in, its path there)`; the repository is
    None when the comparison has none for it."""
    found = ctx._repo_rel(ctx.baseline_path)
    if found is None:
        # In no repository at all: that is the governance root's own answer when it is in none.
        home = cmp.repos[0]
        return (home if home.path is None else None), None
    return next((repo for repo in cmp.repos if repo.path == found[0]), None), found[1]


def _baseline(ctx, repo: base.Repo | None, rel: str | None, scope) -> list[Row]:
    """The ratchet baseline at the base against the one on disk: each number `raised`,
    `lowered`, `added` or `removed`, by key. A baseline that cannot be read on either side is
    one `unreadable` row: the line is its path, as a finding names it, and the reason, and the
    JSON gives the two as `key` and `reason`. A reason never holds an absolute path. Nothing
    when its repository was not compared: "Compared" already says so.

    With `scope`, the keys that project owns (`ratchet.owns`)."""
    path = ctx.baseline_path
    name = ctx.rel(path)

    def number(word: str, key: str, rest: str, old: int | None, new: int | None) -> Row:
        return _change(word, f"{key}{rest}", key=key, reason=None, **{"from": old, "to": new})

    def unreadable(reason: str) -> list[Row]:
        return [_change("unreadable", f"{name} {reason}", key=name, reason=reason,
                        **{"from": None, "to": None})]

    if repo is None:
        # In a repository no log or trap file is in, or in none: nothing was asked of it.
        return unreadable("is outside the repositories compared") if path.exists() else []
    if repo.commit is None or rel is None:
        return []
    at = f"at {repo.commit[:12]}"
    held = base.exists(repo.path, repo.commit, rel)
    if held is None:
        return unreadable(f"{at} could not be read (git could not say whether it is there)")
    try:
        then = ratchet.parse_baseline(base.read(repo.path, repo.commit, rel)) if held else {}
    except base.CannotCompare as exc:
        # `base.read` names the file by its path in the repository; the report names it as a
        # finding would.
        why = exc.reason.removeprefix(f"{rel} at {repo.commit[:12]} ")
        return unreadable(f"{at} {why}" if why != exc.reason
                          else f"{at} could not be read ({exc.reason})")
    except (ValueError, TypeError) as exc:
        return unreadable(f"{at} is not a valid baseline ({exc})")
    if path.exists() and not path.is_file():
        return unreadable("is not a file")
    try:
        now = ratchet.load_baseline(ctx)
    except ratchet.BaselineUnreadable as exc:
        cause = exc.__cause__
        if isinstance(cause, OSError):
            # The system's own words for it, without the whole path it puts after them.
            return unreadable(f"could not be read ({cause.strerror or type(cause).__name__})")
        return unreadable(str(exc).removeprefix(f"{name} "))

    rows = []
    for key in sorted(set(then) | set(now)):
        if scope is not None and not ratchet.owns(ctx, scope, key):
            continue
        old, new = then.get(key), now.get(key)
        if old is None:
            rows.append(number("added", key, f" = {new}", old, new))
        elif new is None:
            rows.append(number("removed", key, f" (was {old})", old, new))
        elif new != old:
            rows.append(number("raised" if new > old else "lowered", key, f" {old} -> {new}",
                               old, new))
    return rows


def build(ctx, scope=None) -> Report:
    """The report for the comparison `ctx` asks for: against `HEAD`, or with `ctx.base` set
    against the commit the branch left that ref at, per repository.

    With `scope`, limited to that project: its entries, the baseline keys it owns, and under
    "Compared" the repositories it was read from."""
    cmp = base.compare(ctx)
    held_in, rel = _baseline_repo(ctx, cmp)
    report = Report(base=cmp.ref or "HEAD",
                    decisions=_entries(ctx, cmp, base.DECISION, scope),
                    traps=_entries(ctx, cmp, base.TRAP, scope),
                    baseline=_baseline(ctx, held_in, rel, scope))
    for repo in cmp.repos:
        if not repo.history:
            continue
        if scope is not None and scope.name not in repo.scopes and repo is not held_in:
            continue
        # The text names a commit by its first 12 characters; a tool gets the whole id.
        reason = None if repo.commit is not None else repo.reason
        report.compared.append(Row(
            {"repo": repo.label, "commit": repo.commit, "reason": reason},
            f"{repo.label} at {repo.commit[:12]}" if repo.commit is not None
            else f"{repo.label}: not compared ({reason})"))
    return report


SECTIONS = (("Decisions", "decisions"), ("Traps", "traps"), ("Baseline", "baseline"))


def text(report: Report) -> str:
    """The report as text: a heading, a section for each kind of record with a change in it,
    then what was compared. A section with nothing in it is left out; with nothing at all, one
    line says so: `- nothing changed`, or `- nothing compared` when no repository could be
    compared, which is not the same as nothing having changed."""
    lines = [f"Governance changes since {report.base}", ""]
    shown = [(title, getattr(report, name)) for title, name in SECTIONS if getattr(report, name)]
    if not shown:
        compared = any(row.data["commit"] is not None for row in report.compared)
        lines += ["- nothing changed" if compared else "- nothing compared", ""]
    for title, rows in shown:
        lines += [title, *(f"- {row.line}" for row in rows), ""]
    lines += ["Compared", *(f"- {row.line}" for row in report.compared)]
    return "\n".join(lines) + "\n"


def as_json(report: Report) -> str:
    """The report as one JSON object: keys sorted, absent values `null`, ASCII only, and a
    trailing newline."""
    data = {"base": report.base, "compared": [row.data for row in report.compared]}
    data.update({name: [row.data for row in getattr(report, name)] for _, name in SECTIONS})
    return json.dumps(data, indent=2, sort_keys=True) + "\n"
