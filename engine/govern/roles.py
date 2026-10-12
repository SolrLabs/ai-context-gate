"""Rendered roles: the agent definitions and the coordinator's build rules the engine writes
into a project (`govern agents render`, the `agent-roles` option).

A rendered file is a pure function of its template (`templates/agents/<role>.md`,
`templates/build-rules.md`), the option's resolved params and the project's own inserts
(`.context-gate/agents/all.md`, then `.context-gate/agents/<role>.md`), so the check compares
text and never a timestamp. Files are written with LF; a comparison and a recorded sha256 both
read line endings as LF, so a CRLF checkout is neither stale nor edited.

The first line of a rendered file's body is a note naming the role. A file without it was not
rendered here and is never overwritten or deleted: `plan` reports it as a conflict and leaves
it alone. `plan` builds everything in memory; `apply` writes all of it or none.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from govern import layout
from govern.text import has_bom, parse_frontmatter, read, read_text, write

CHECK = "agent-roles"
TEMPLATES = Path(__file__).resolve().parent / "templates"

# The five roles every project with the option on gets, and the two `extra_roles` can add.
CORE = ("developer", "senior-developer", "code-reviewer", "qa-engineer", "technical-writer")
EXTRA = ("ux-reviewer", "architect")
ROLES = CORE + EXTRA
# The name the coordinator's rules file goes by in its note.
RULES = "build-rules"

# role -> (model, effort), unless `models` or `efforts` says otherwise.
DEFAULTS = {
    "developer": ("sonnet", "medium"),
    "senior-developer": ("opus", "high"),
    "code-reviewer": ("opus", "medium"),
    "qa-engineer": ("opus", "high"),
    "technical-writer": ("sonnet", "low"),
    "ux-reviewer": ("sonnet", "medium"),
    "architect": ("opus", "high"),
}
SENIOR_TRIGGERS = [
    "security: credentials, tokens, signing, authentication, access control, or input from "
    "outside the project (a download, an archive, pasted text)",
]
RULES_FILE = f".claude/rules/{layout.TOOL}-build.md"
INSERTS = f"{layout.GOV_DIR}/agents"
# The project's entry point, as its skills call it from the project's root.
GOVERN = f"python3 {layout.ENTRYPOINT}"
RENDER = f"{GOVERN} agents render"

NO_AUTHORITY = "the plan or spec it names"
HANDBACK_BINDING = ("**Every line of this checklist is part of the task you were given. Copy it into your report file and answer each line. A run that hands back with a line unanswered has not done what it was asked, and its unit is treated as failed, whatever else it did.** Answer a line as `Key: <your answer>`, on one line. When the brief has no `Produces:` line there is no report file: answer the checklist in your final message.")
# The checklist an agent answers before it hands back: key to text. A later requirement is one
# more entry here and a place in `HANDBACK_ROLES`.
HANDBACK = {
    "Scope": "the files you changed. Each is a file the brief names; anything else you found is in the report, not in the diff.",
    "Red": "where you saved the failing output before you changed the code, or `none` when the brief has no `Red:` line.",
    "Gate": "the full gate's result line. You ran it once, at the end.",
    "Commit": "the hash, on your own branch. You did not push or merge, and the message has no attribution line when a rule above forbids one.",
    "Not verified": "each claim that rests on reading and not on a command you ran, or `none`.",
    "Report": "the full path of the report file. You wrote it to the brief's `Produces:` line before your final message, and that message carries this same line. The file is not committed, and that is expected. With no `Produces:` line, `Report: none`. A run that stops early (`BLOCKED`, `NEEDS_CONTEXT`) still writes the file and answers every line it can.",
}
HANDBACK_ROLES = {
    "developer": ["Scope", "Red", "Gate", "Commit", "Not verified", "Report"],
    "senior-developer": ["Scope", "Red", "Gate", "Commit", "Not verified", "Report"],
    "technical-writer": ["Scope", "Gate", "Commit", "Not verified", "Report"],
}
HANDBACK_DEFAULT = ["Not verified", "Report"]
NO_RULES = "- This project has added no rules of its own."
NO_TRIGGERS = "- nothing configured: `senior_triggers` names no trigger"
NO_HIGH_RISK = ("- nothing configured: no phase is high-risk until `high_risk_paths` names its "
                "paths")
HOUSEKEEPING = (
    "- When a phase's fix pass closes, launch `technical-writer` with a brief that says the "
    "phase is closing, before you start the next phase.\n"
    f"- Then run `{GOVERN} phase close`: it pushes the phase branch to its remote, confirms the "
    "remote holds it, and only then removes branches and worktrees already merged into it. It "
    "opens no pull request and merges nothing upward. Run it only when no agent is still "
    "working.\n")

ESCAPED_PIPE = "\\|"       # a `|` inside a table cell
SLOT_RE = re.compile(r"\{\{(.*?)\}\}")
NOTE_RE = re.compile(rf"<!-- {re.escape(layout.TOOL)}:rendered role=([\w-]+)\s")


class UnknownSlot(ValueError):
    """A template names a `{{slot}}` nothing fills."""

    def __init__(self, slot: str, role: str) -> None:
        super().__init__(f"the {role} template names {{{{{slot}}}}}, which nothing fills — "
                         f"an engine defect: report it")
        self.slot = slot
        self.role = role


# ---------------------------------------------------------------------------- render

def rendered(params: dict) -> list[str]:
    """The roles these params render, in the engine's own order."""
    return [r for r in ROLES if r in CORE or r in params["extra_roles"]]


def shipped(role: str) -> str:
    """A shipped template's text, with LF line endings whatever the engine's checkout has."""
    path = TEMPLATES / f"{RULES}.md" if role == RULES else TEMPLATES / "agents" / f"{role}.md"
    return read(path).replace("\r\n", "\n")


def use(role: str) -> str:
    """A role's one-line use: the first sentence of its template's description."""
    fm, _ = parse_frontmatter(shipped(role))
    return str(fm.get("description", "")).split(". ")[0].rstrip(".") + "."


def note(role: str) -> str:
    """The line that marks a file as rendered here, and says where to change it instead."""
    source = layout.CONFIG if role == RULES else f"{layout.CONFIG} or {INSERTS}/{role}.md"
    return (f"<!-- {layout.TOOL}:rendered role={role} — do not edit: change {source}, then "
            f"run {RENDER} -->")


def note_role(text: str) -> str | None:
    """The role a file's note names, or None when it carries no note: the first line after the
    frontmatter, or the first line of a file that has none."""
    _, body = parse_frontmatter(text)
    if len(body) < len(text):
        body = body.removeprefix("\n")
    m = NOTE_RE.match(body.split("\n", 1)[0])
    return m.group(1) if m else None


def _bullets(items: list, empty: str) -> str:
    return "\n".join(f"- {item}" for item in items) or empty


def _command(value: str) -> str:
    return f" (`{value}`)" if value else ""


def _model(role: str, params: dict) -> tuple[str, str]:
    model, effort = DEFAULTS[role]
    return params["models"].get(role, model), params["efforts"].get(role, effort)


def roles_table(params: dict) -> str:
    rows = ["| Role | Model | Effort | Use |", "|---|---|---|---|"]
    for role in rendered(params):
        model, effort = _model(role, params)
        rows.append(f"| `{role}` | {model} | {effort} | {use(role).replace('|', ESCAPED_PIPE)} |")
    return "\n".join(rows)


def _entries(params: dict) -> list[tuple[str, str, str, str | None]]:
    """The operator's checklist lines as (entry, key, text, complaint): complaint is None for a
    line that can be used. The one reading `problems` and the gate share."""
    out, seen = [], set(HANDBACK)
    for entry in params["handback_checks"]:
        key, sep, text = str(entry).partition(": ")
        complaint = None
        if not sep or not key.strip() or not text.strip():
            complaint = "expected `Key: text`"
        elif key != key.strip():
            complaint = "the key has a space before its colon"
        elif not key[0].isupper():
            complaint = "the key starts with a capital letter"
        elif key in seen:
            complaint = (f"the key '{key}' is "
                         + ("one of the engine's" if key in HANDBACK else "used twice"))
        else:
            seen.add(key)
        out.append((entry, key, text, complaint))
    return out


def _added(params: dict) -> dict[str, str]:
    """The operator's checklist lines, key to text. A line `problems` reports is left out."""
    return {key: text for _, key, text, complaint in _entries(params) if complaint is None}


def handback_texts(role: str, params: dict) -> dict[str, str]:
    """`role`'s checklist, key to text."""
    texts = {**HANDBACK, **_added(params)}
    return {key: texts[key] for key in handback_keys(role, params)}


def handback_keys(role: str, params: dict) -> list[str]:
    """The keys of `role`'s hand-back checklist: its own from `HANDBACK_ROLES`, with the
    operator's (`handback_checks`) just before `Not verified`."""
    keys = list(HANDBACK_ROLES.get(role, HANDBACK_DEFAULT))
    added = list(_added(params))
    at = keys.index("Not verified")
    return keys[:at] + added + keys[at:]


def handback(role: str, params: dict) -> str:
    """The binding sentence, a blank line, then one numbered line per key."""
    lines = [f"{n}. `{key}:` {text}"
             for n, (key, text) in enumerate(handback_texts(role, params).items(), 1)]
    return "\n".join([HANDBACK_BINDING, "", *lines])


def slots(role: str, params: dict, inserts: str = "") -> dict[str, str]:
    """Every slot `role`'s template may name, filled. A slot that belongs to the other kind of
    template (`{{model}}` in the rules, `{{roles_table}}` in a role) is left out, so naming it
    there is an error rather than an empty string."""
    out = {
        "generated_note": note(role),
        "govern": GOVERN,
        "authority": params["authority"] or NO_AUTHORITY,
        "scoped_test": _command(params["scoped_test"]),
        "full_gate": _command(params["full_gate"]),
        "senior_triggers": _bullets(params["senior_triggers"], NO_TRIGGERS),
        "high_risk_paths": _bullets(params["high_risk_paths"], NO_HIGH_RISK),
        "escalate_after": str(params["escalate_after"]),
    }
    if role == RULES:
        out["roles_table"] = roles_table(params)
        out["housekeeping_rule"] = HOUSEKEEPING if params["housekeeping"] else ""
        return out
    model, effort = _model(role, params)
    cap = params["max_turns"].get(role)
    out.update({
        "model": model,
        "effort": effort,
        "omit_claude_md": "false" if role in params["load_claude_md"] else "true",
        # No cap is said out loud: `false` is a stated choice, where a missing key is a default
        # nobody chose.
        "max_turns": "false" if cap is None else str(cap),
        "handback": handback(role, params),
        "project_rules": "\n".join([*(f"- {rule}" for rule in params["operator_rules"]),
                                    *([inserts] if inserts else [])]) or NO_RULES,
    })
    return out


def render(template: str, role: str, params: dict, inserts: str = "") -> str:
    """`template` with every slot filled for `role` (a role name, or `RULES`) from the option's
    resolved `params` and the project's `inserts`. One pass: text an insert brings in is never
    read for slots. Raises `UnknownSlot` for a slot nothing fills."""
    values = slots(role, params, inserts)

    def fill(m: re.Match) -> str:
        if m.group(1) not in values:
            raise UnknownSlot(m.group(1), role)
        return values[m.group(1)]
    return SLOT_RE.sub(fill, template)


def project_rules(root: Path, role: str) -> str:
    """The project's own rules for `role`: `all.md`, then `<role>.md`, each optional and
    verbatim but for its line endings. Raises `Unreadable` for one that cannot be read."""
    parts = []
    for name in ("all", role):
        path = root / INSERTS / f"{name}.md"
        if path.is_file():
            body = read(path).replace("\r\n", "\n").strip("\n")
            if body.strip():
                parts.append(body)
    return "\n".join(parts)


# ---------------------------------------------------------------------------- params

def inside(rel: str) -> bool:
    """Whether `rel` names a path inside the project: not empty, not rooted (a leading slash or
    a drive, which a join would follow out of the root on Windows) and with no `..` in it."""
    path = Path(rel)
    return bool(rel.strip()) and not path.anchor and ".." not in path.parts


def _not_rendered(role, live: list[str]) -> str:
    return (f"'{role}', which is not rendered"
            + (" (add it to extra_roles)" if role in EXTRA else f" (rendered: {', '.join(live)})"))


def problems(params: dict) -> list[str]:
    """What in the option's params cannot be rendered: reported by the check, and a reason for
    `render` to write nothing. These are findings, not load errors: a list or a free table has
    no declared set of names for the loader to hold it to."""
    where = f"[checks.{CHECK}]"
    out = [f"{where} extra_roles names '{name}': the roles it can add are {', '.join(EXTRA)}"
           for name in params["extra_roles"] if name not in EXTRA]
    live = rendered(params)
    for key in ("models", "efforts", "max_turns"):
        for role, value in params[key].items():
            if role not in live:
                out.append(f"{where} {key} names {_not_rendered(role, live)}")
            elif key == "max_turns":
                if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                    out.append(f"{where} max_turns.{role} = {value!r}: a cap is a whole number "
                               f"above 0 (leave the role out for no cap)")
            elif not isinstance(value, str) or not value.strip():
                out.append(f"{where} {key}.{role} = {value!r}: expected a name")
    out += [f"{where} load_claude_md names {_not_rendered(role, live)}"
            for role in params["load_claude_md"] if role not in live]
    out += [f"{where} handback_checks = {entry!r}: {complaint}"
            for entry, _, _, complaint in _entries(params) if complaint]
    if not inside(params["rules_file"]):
        out.append(f"{where} rules_file = '{params['rules_file']}': name a file inside the "
                   f"project")
    return out


# ---------------------------------------------------------------------------- plan

MISSING, STALE, CURRENT, CONFLICT = "missing", "stale", "current", "conflict"


@dataclass
class Target:
    """One file the option renders: where, for which role, what it should hold, and how the
    file there now compares."""
    path: Path
    role: str
    text: str
    state: str
    why: str = ""         # a conflict's reason


@dataclass
class Plan:
    problems: list[str] = field(default_factory=list)
    targets: list[Target] = field(default_factory=list)
    # A file that carries the note for a role no longer rendered: (path, the role it names).
    orphans: list[tuple[Path, str]] = field(default_factory=list)

    @property
    def writes(self) -> list[Target]:
        return [t for t in self.targets if t.state in (MISSING, STALE)]

    @property
    def conflicts(self) -> list[Target]:
        return [t for t in self.targets if t.state == CONFLICT]


def lf(text: str) -> str:
    return text.replace("\r\n", "\n")


def digest(text: str) -> str:
    """The sha256 a rendered file is recorded under: of its text with LF line endings."""
    return hashlib.sha256(lf(text).encode("utf-8")).hexdigest()


def file_digest(path: Path) -> str | None:
    """`digest` of the file there now, or None when it cannot be read as text."""
    got = read_text(path)
    return None if got.error else digest(got.text)


def carries_note(path: Path) -> bool:
    """Whether the file there now is one the engine rendered: readable, and opening with the
    note."""
    got = read_text(path)
    return not got.error and note_role(got.text) is not None


def ours(path: Path, rules: set[Path]) -> str | None:
    """The role a file the engine rendered names, or None when it is not one to remove: it has
    to carry a note, and the note has to fit where the file is. A role's file is named for the
    role; the rules are one of the `rules` paths. A copy saved under another name keeps its
    note and stays the person's."""
    got = read_text(path)
    role = None if got.error else note_role(got.text)
    if role == RULES:
        return role if path in rules else None
    return role if role in ROLES and path.stem == role else None


def remove(root: Path, path: Path) -> None:
    """Delete a rendered file, then each directory above it (below `root`) it leaves empty."""
    _unlock(path)
    path.unlink()
    for folder in path.parents:
        if folder == root or root not in folder.parents:
            break
        try:
            folder.rmdir()
        except OSError:
            break                          # not empty


def agents_dir(ctx) -> Path:
    return ctx.root / ctx.workspace("agents_dir", ".claude/agents")


def recorded(root: Path) -> list[dict]:
    """The `[[rendered]]` records in `installed.toml`: none when nothing is installed here, or
    when an engine that does not know the table rewrote the file."""
    try:
        with (root / layout.MANIFEST).open("rb") as fh:
            rows = tomllib.load(fh).get("rendered", [])
    except (OSError, tomllib.TOMLDecodeError):
        return []
    return [r for r in rows if isinstance(r, dict) and isinstance(r.get("path"), str)
            and inside(r["path"])]


def _target(path: Path, role: str, text: str) -> Target:
    if not path.exists():
        return Target(path, role, text, MISSING)
    got = read_text(path)
    if got.error:
        return Target(path, role, text, CONFLICT, got.error)
    if note_role(got.text) is None:
        return Target(path, role, text, CONFLICT, f"was not rendered by {layout.TOOL}")
    return Target(path, role, text, CURRENT if lf(got.text) == text else STALE)


def plan(ctx, params: dict | None = None) -> Plan:
    """What `render` would do, built in memory and writing nothing. With a problem in the
    params it plans nothing at all. Raises `Unreadable` for an insert that cannot be read, and
    `UnknownSlot` for a template naming a slot nothing fills."""
    params = ctx.cfg.checks[CHECK].params if params is None else params
    found = problems(params)
    adir = agents_dir(ctx)
    if not found:
        for role in rendered(params):
            if ctx.root / params["rules_file"] == adir / f"{role}.md":
                found = [f"[checks.{CHECK}] rules_file = '{params['rules_file']}': that is "
                         f"where the `{role}` role is rendered"]
    if found:
        return Plan(problems=found)
    out = Plan()
    for role in rendered(params):
        out.targets.append(_target(adir / f"{role}.md", role,
                                   render(shipped(role), role, params,
                                          project_rules(ctx.root, role))))
    out.targets.append(_target(ctx.root / params["rules_file"], RULES,
                               render(shipped(RULES), RULES, params)))
    wanted = {t.path for t in out.targets}
    # Rendered earlier and no longer wanted: in the agents directory, or wherever a record says
    # one was written (a rules file whose `rules_file` has since moved).
    seen = sorted(adir.glob("*.md")) if adir.is_dir() else []
    seen += [ctx.root / r["path"] for r in recorded(ctx.root)]
    rules = {ctx.root / r["path"] for r in recorded(ctx.root)} | {ctx.root / params["rules_file"]}
    live = rendered(params)
    for path in dict.fromkeys(seen):
        if path in wanted or not path.is_file():
            continue
        role = ours(path, rules)
        if role is not None and role not in live:
            out.orphans.append((path, role))
    return out


def conflict(ctx, target: Target) -> str:
    """A conflict as a finding: the file, why it is left alone, and both ways out."""
    if target.role == RULES:
        other = f"point [checks.{CHECK}] rules_file at another path"
    elif target.role in EXTRA:
        other = f"drop '{target.role}' from [checks.{CHECK}] extra_roles"
    else:
        other = f"turn {CHECK} off ('{target.role}' is always rendered)"
    return (f"{ctx.rel(target.path)}: {target.why}, so it is left alone — move the file and "
            f"run `{RENDER}`, or {other}")


# ---------------------------------------------------------------------------- apply

class WriteFailed(Exception):
    """A rendered file could not be written or removed. Everything this run had changed was put
    back, except `unrestored`."""

    def __init__(self, error: OSError, unrestored: list[Path]) -> None:
        super().__init__(str(error))
        self.error = error
        self.unrestored = unrestored


@dataclass
class Applied:
    """What `apply` changed, with what was there before, so a caller whose own later step fails
    can put it all back (`undo`)."""
    written: list[Path] = field(default_factory=list)
    removed: list[Path] = field(default_factory=list)
    saved: dict[Path, bytes | None] = field(default_factory=dict)   # None: there was no file
    made: list[Path] = field(default_factory=list)                  # directories created

    def undo(self) -> list[Path]:
        """Put every file back as it was. Returns the ones that could not be."""
        unrestored = []
        for path, data in self.saved.items():
            try:
                if data is None:
                    path.unlink(missing_ok=True)
                elif not path.is_file() or path.read_bytes() != data:
                    _unlock(path)
                    path.write_bytes(data)
            except OSError:
                unrestored.append(path)
        for folder in reversed(self.made):
            try:
                folder.rmdir()
            except OSError:
                pass                       # not empty: something else is in it now
        return unrestored


def _unlock(path: Path) -> None:
    """A read-only file is made writable first: Windows refuses to write or delete one."""
    if path.exists() and not os.access(path, os.W_OK):
        path.chmod(path.stat().st_mode | stat.S_IWRITE)


def apply(plan: Plan) -> Applied:
    """Write every missing or stale target and delete every orphan: all of it or none. On an
    `OSError` what was changed is put back and `WriteFailed` raised. A conflict is never
    touched. A file that opens with a UTF-8 BOM keeps it."""
    done = Applied()
    try:
        for t in plan.writes:
            done.saved[t.path] = t.path.read_bytes() if t.path.exists() else None
            for folder in reversed([t.path.parent, *t.path.parent.parents]):
                if not folder.exists():
                    folder.mkdir()
                    done.made.append(folder)
            bom = "\ufeff" if has_bom(t.path) else ""
            _unlock(t.path)
            write(t.path, bom + t.text)
            done.written.append(t.path)
        for path, _ in plan.orphans:
            done.saved[path] = path.read_bytes()
            _unlock(path)
            path.unlink()
            done.removed.append(path)
    except OSError as exc:
        raise WriteFailed(exc, done.undo()) from exc
    return done


def _rel(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def records(root: Path, plan: Plan) -> list[dict]:
    """The `[[rendered]]` records for a plan once applied: every target that is ours."""
    return [{"path": _rel(root, t.path), "sha256": digest(t.text)}
            for t in plan.targets if t.state != CONFLICT]


def record(root: Path, plan: Plan) -> bool:
    """Record the rendered files in `installed.toml`, when there is one. Returns whether the
    file changed: a project with no install renders all the same and records nothing."""
    from govern import installer
    if not (root / layout.MANIFEST).is_file():
        return False
    data = installer.read_manifest(root)
    new = records(root, plan)
    if data["rendered"] == new:
        return False
    data["rendered"] = new
    installer.write_manifest(root / layout.MANIFEST, data)
    return True


# ---------------------------------------------------------------------------- command

def run(ctx, check_only: bool = False) -> int:
    """`govern agents render [--check]`. Exit 0 when every file is as rendered; 1 for a refused
    param, a conflict, a write that failed, or (under `--check`, which writes nothing) anything
    that would change."""
    label = "agents render --check" if check_only else "agents render"
    if ctx.cfg.checks[CHECK].level == "off":
        print(f"{CHECK} is off: nothing rendered. Turn the option on under [checks.{CHECK}] in "
              f"{layout.CONFIG} to render the roles.")
        return 0
    try:
        todo = plan(ctx)
    except UnknownSlot as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if todo.problems:
        for msg in todo.problems:
            print(f"  refused     {msg}")
        print(f"[FAIL] {label}  (nothing written: fix the settings above)")
        return 1
    for t in todo.conflicts:
        print(f"  conflict    {conflict(ctx, t)}")
    changes = len(todo.writes) + len(todo.orphans)
    if check_only:
        for t in todo.writes:
            print(f"  {t.state:<11} {ctx.rel(t.path)}")
        for path, role in todo.orphans:
            print(f"  orphaned    {ctx.rel(path)} (role {role} is no longer rendered)")
        failed = bool(changes or todo.conflicts)
        print(f"[{'FAIL' if failed else 'OK'}] {label}  ({changes} file(s) would change"
              + (f", {len(todo.conflicts)} conflict(s)" if todo.conflicts else "") + ")")
        return 1 if failed else 0
    try:
        done = apply(todo)
    except WriteFailed as exc:
        where = f"{exc.error.filename}: " if exc.error.filename else ""
        left = ", ".join(ctx.rel(p) for p in exc.unrestored)
        print(f"error: {where}{exc.error.strerror or exc.error} — "
              + (f"nothing was written, but {left} could not be put back" if left
                 else "nothing was written"), file=sys.stderr)
        return 1
    for path in done.written:
        print(f"  rendered    {ctx.rel(path)}")
    for path in done.removed:
        print(f"  removed     {ctx.rel(path)}")
    if record(ctx.root, todo):
        print(f"  recorded    {len(records(ctx.root, todo))} file(s) in {layout.MANIFEST}")
    print(f"[{'FAIL' if todo.conflicts else 'OK'}] {label}  ({len(done.written)} written, "
          f"{len(done.removed)} removed"
          + (f", {len(todo.conflicts)} conflict(s)" if todo.conflicts else "") + ")")
    return 1 if todo.conflicts else 0
