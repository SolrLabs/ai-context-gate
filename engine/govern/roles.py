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
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from govern import layout
from govern.context import GIT_TIMEOUT, git_env
from govern.text import Unreadable, has_bom, parse_frontmatter, read, read_text, write

CHECK = "agent-roles"
# Where the records go when `agent-roles` is on only in `local.toml`: the same `[[rendered]]`
# rows as the manifest's, in a file git is told to ignore.
LOCAL_RECORD = f"{layout.GOV_DIR}/rendered.local.toml"
EXCLUDE_START = "# context-gate: rendered on this machine"
EXCLUDE_END = "# context-gate: end"
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
    "security: credentials, keys, tokens, signing, sign-in and sessions, the code that decides "
    "who may read or change protected data, or input from outside the project (a download, an "
    "archive, pasted text). A product rule about who may do what is not security.",
]
RULES_FILE = f".claude/rules/{layout.TOOL}-build.md"
INSERTS = f"{layout.GOV_DIR}/agents"
# The project's entry point, as its skills call it from the project's root.
GOVERN = f"python3 {layout.ENTRYPOINT}"
RENDER = f"{GOVERN} agents render"
# Why a role file is missing or stale, by where the roles are kept.
LOST_NOTE = ("these files are kept on this machine only, and git removes them when a checkout "
             "crosses a commit that tracked them")
BRANCH_NOTE = "on a branch cut before the roles last changed, merge the default branch, or " \
              "render and commit"

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
# The roles that write code: what `unit_gate` changes for them.
SCOPED_ROLES = ("developer", "senior-developer")
# What a unit runs before it hands back (`unit_gate`): its own tests, or the full gate.
UNIT_GATES = ("scoped", "full")
GATE_SCOPED = ("the command you ran for your unit's tests and its result line (the count is not "
               "zero). The coordinator runs the full gate.")
FILTER_NOTE = "A filter that matches nothing prints a pass: check the total is not zero."
NO_FULL_GATE = "Do not run the full gate: the coordinator runs it once your unit is merged."
NO_RULES = "- This project has added no rules of its own."
NO_TRIGGERS = "- nothing configured: `senior_triggers` names no trigger"
NO_HIGH_RISK = ("- nothing configured: no phase is high-risk until `high_risk_paths` names its "
                "paths")
NO_VISUAL = ("- The project can also render `ux-reviewer`, which launches the built product and "
             "looks at it: set `visual_paths` on `agent-roles` to turn it on.")
VISUAL = ("- A phase is visual when it touches one of these. Start it with `--visual`, and "
          "`ux-reviewer` then looks at the running product beside `code-reviewer`:")
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
    return [r for r in ROLES if r in CORE or r in params["extra_roles"]
            or (r == "ux-reviewer" and params.get("visual_paths"))]


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
    if role in SCOPED_ROLES and params.get("unit_gate", "scoped") == "scoped":
        texts["Gate"] = GATE_SCOPED
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
    scoped = params.get("unit_gate", "scoped") == "scoped"
    if role == RULES:
        # A whole bullet and its line break, or nothing, so the file is as it was under `full`.
        out["merge_gate"] = (
            f"- After you merge a unit, run the full gate on the phase branch"
            f"{_command(params['full_gate'])}. A unit that turns it red is failed.\n"
            if scoped else "")
        out["roles_table"] = roles_table(params)
        out["visual_paths"] = (VISUAL + "\n" + _bullets(params["visual_paths"], "")
                               if params["visual_paths"] else NO_VISUAL)
        out["housekeeping_rule"] = HOUSEKEEPING if params["housekeeping"] else ""
        return out
    model, effort = _model(role, params)
    cap = params["max_turns"].get(role)
    gate = f"run the full gate once, before you report{_command(params['full_gate'])}."
    if scoped:
        out["unit_gate"] = (NO_FULL_GATE if role == "developer"
                            else f". {FILTER_NOTE} {NO_FULL_GATE}")
        out["done_line"] = "everything in the brief is done and your unit's tests are green."
    else:
        out["unit_gate"] = f"R{gate[1:]}" if role == "developer" else f"; {gate}"
        out["done_line"] = "everything in the brief is done and the full gate is green."
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
    if params.get("unit_gate", "scoped") not in UNIT_GATES:
        out.append(f"{where} unit_gate = {params['unit_gate']!r}: expected one of "
                   f"{', '.join(UNIT_GATES)}")
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


def _rows(path: Path) -> list[dict]:
    """The `[[rendered]]` rows in a TOML file: none when it is missing or unreadable, or when an
    engine that does not know the table rewrote it."""
    try:
        with path.open("rb") as fh:
            rows = tomllib.load(fh).get("rendered", [])
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return []
    return [r for r in rows if isinstance(r, dict) and isinstance(r.get("path"), str)
            and inside(r["path"])]


def local_paths(root: Path) -> set[str]:
    """The `path` of each row in the machine-local record, from the project root, as posix
    strings: the files that exist on this machine only. Empty when the record is missing."""
    return {Path(r["path"]).as_posix() for r in _rows(root / LOCAL_RECORD)}


def recorded(root: Path) -> list[dict]:
    """The `[[rendered]]` records in `installed.toml`, then those in the machine-local record,
    no path twice."""
    out, seen = [], set()
    for rel in (layout.MANIFEST, LOCAL_RECORD):
        for row in _rows(root / rel):
            if row["path"] not in seen:
                seen.add(row["path"])
                out.append(row)
    return out


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
        params = ctx.cfg.checks[CHECK].params
        ways = []
        if target.role in params["extra_roles"] or not (
                target.role == "ux-reviewer" and params.get("visual_paths")):
            ways.append(f"drop '{target.role}' from [checks.{CHECK}] extra_roles")
        if target.role == "ux-reviewer" and params.get("visual_paths"):
            ways.append(f"empty [checks.{CHECK}] visual_paths")
        other = " and ".join(ways)
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


def record(root: Path, plan: Plan, local: bool = False) -> str | None:
    """Record the rendered files, and return the file that changed (None when nothing did).
    `local`, for a project whose `agent-roles` is on only on this machine: the rows go to
    `LOCAL_RECORD` and `installed.toml` is left holding none. Otherwise they go to
    `installed.toml`, when there is one (a project with no install renders all the same and
    records nothing), and a `LOCAL_RECORD` left from before is deleted."""
    from govern import installer
    new = records(root, plan)
    manifest, here = root / layout.MANIFEST, root / LOCAL_RECORD
    changed = None
    if local:
        if _rows(here) != new or not here.is_file():
            lines = [f"# What {layout.DISPLAY_NAME} rendered on this machine only. Git ignores it."]
            for item in new:
                lines += ["", "[[rendered]]", f"path = {installer.toml_str(item['path'])}",
                          f"sha256 = {installer.toml_str(item['sha256'])}"]
            write(here, "\n".join(lines) + "\n")
            changed = LOCAL_RECORD
        new = []
    elif here.exists():
        here.unlink()
    if manifest.is_file():
        data = installer.read_manifest(root)
        if data["rendered"] != new:
            data["rendered"] = new
            installer.write_manifest(manifest, data)
            changed = changed or layout.MANIFEST
    return changed


def exclude_lines(root: Path, plan: Plan) -> list[str]:
    """The lines of the exclude block for a local-only project: each rendered path and
    `LOCAL_RECORD`, from the top of the repository. Raises `GitFailed` when git cannot say."""
    prefix = _prefix(root)
    return [f"/{prefix}{r['path']}" for r in records(root, plan)] + [f"/{prefix}{LOCAL_RECORD}"]


def settle(ctx, plan: Plan) -> str | None:
    """Record the rendered files where `local_only` says, and for a local-only project keep them
    out of git through the exclude file. Returns the file `record` changed. Raises `GitFailed`,
    `Unreadable` and `OSError`; whatever `record` wrote by then stays."""
    solo = local_only(ctx)
    changed = record(ctx.root, plan, local=solo)
    if solo:
        where = exclude_file(ctx.root)
        if where is not None:
            set_block(where, exclude_lines(ctx.root, plan))
    return changed


# ---------------------------------------------------------------------------- one machine

def local_only(ctx) -> bool:
    """Whether `agent-roles` is on only because `local.toml` says so: it is on now, and was off
    just before `local.toml` set its level."""
    s = ctx.cfg.checks[CHECK]
    return "local" in s.source.get("level", "") and s.level != "off" \
        and s.level_before_local == "off"


def mixed(ctx) -> list[str]:
    """The params `local.toml` sets while the check is on in committed config: they would be
    rendered into files everyone gets. Sorted; none when the check is local-only."""
    if local_only(ctx):
        return []
    return sorted(k for k, src in ctx.cfg.checks[CHECK].source.items()
                  if k != "level" and "local" in src)


class GitFailed(Exception):
    """A git call this module needed failed: git's own words."""


def _git(root: Path, *args: str) -> subprocess.CompletedProcess | None:
    """`git -C root ...`; None when git could not run at all."""
    try:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                              timeout=GIT_TIMEOUT, env=git_env())
    except (OSError, subprocess.SubprocessError):
        return None


def in_repo(root: Path) -> bool:
    """Whether `root` is inside a git work tree. Raises `GitFailed` when git cannot say: git
    missing, a timeout, `safe.directory`, a lock. Only git's own "not a git repository" is False."""
    res = _git(root, "rev-parse", "--is-inside-work-tree")
    if res is not None and res.returncode == 0:
        return True
    if res is not None and b"not a git repository" in res.stderr:
        return False
    raise GitFailed(_stderr(res))


def exclude_file(root: Path) -> Path | None:
    """The repository's own exclude file (`info/exclude`, in the main repository for a linked
    worktree), or None when `root` is not in a repository. Raises `GitFailed` in one when git
    cannot say."""
    if not in_repo(root):
        return None
    res = _git(root, "rev-parse", "--git-path", "info/exclude")
    if res is None or res.returncode or not res.stdout.strip():
        raise GitFailed(_stderr(res))
    path = Path(res.stdout.decode("utf-8", errors="replace").strip())
    return path if path.is_absolute() else root / path


def _prefix(root: Path) -> str:
    """Where `root` is below its repository's top, as `sub/` (empty at the top): exclude lines
    are relative to the top. Raises `GitFailed` when git cannot say."""
    res = _git(root, "rev-parse", "--show-prefix")
    if res is None or res.returncode:
        raise GitFailed(_stderr(res))
    return res.stdout.decode("utf-8", errors="replace").strip()


def _lines(text: str) -> list[str]:
    return re.findall(r"[^\n]*\n|[^\n]+", text)


def _block_text(path: Path, lines: list[str]) -> tuple[str, str]:
    """(the file's text now, the text with its marked block set to `lines`), BOM left off both.
    A file git cannot read as UTF-8, or whose block has a start and no end, raises `Unreadable`:
    which lines are ours cannot be told."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raw = b""
    except OSError as exc:
        raise Unreadable(path, f"unreadable ({exc.strerror})") from exc
    try:
        old = raw.decode("utf-8").removeprefix("﻿")
    except UnicodeDecodeError as exc:
        raise Unreadable(path, f"not valid UTF-8 (byte {exc.start})") from exc
    nl = "\r\n" if "\r\n" in old else "\n"
    rows = _lines(old)
    start = next((i for i, r in enumerate(rows) if r.rstrip("\r\n") == EXCLUDE_START), None)
    end = None
    if start is not None:
        end = next((i for i in range(start + 1, len(rows))
                    if rows[i].rstrip("\r\n") == EXCLUDE_END), None)
        if end is None:
            raise Unreadable(path, f"a block opened by '{EXCLUDE_START}' never ends")
    block = [EXCLUDE_START + nl, *(f"{line}{nl}" for line in sorted(lines)), EXCLUDE_END + nl]
    if start is None:
        if not lines:
            return old, old
        if rows and not rows[-1].endswith("\n"):
            rows[-1] += nl
        return old, "".join(rows + block)
    if not rows[end].endswith("\n"):                  # the file ended on the block's last line
        block[-1] = EXCLUDE_END
    return old, "".join(rows[:start] + (block if lines else []) + rows[end + 1:])


def set_block(path: Path, lines: list[str]) -> bool:
    """Set the marked block of `path` to one line per entry of `lines`, sorted; no lines
    removes the block. Every other line of the file is kept byte for byte, a BOM and the file's
    line endings included. A missing file or folder is created only to hold lines. Returns
    whether the file changed."""
    old, new = _block_text(path, lines)
    if new == old:
        return False
    bom = b"\xef\xbb\xbf" if path.is_file() and has_bom(path) else b""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_bytes(bom + new.encode("utf-8"))
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    return True


def tracked(root: Path, paths: list[str]) -> list[str]:
    """Which of `paths` (from `root`) git tracks. Raises `GitFailed` when git cannot say: a
    failure is never an empty answer."""
    if not paths:
        return []
    res = _git(root, "ls-files", "-z", "--", *paths)
    if res is None or res.returncode:
        raise GitFailed(_stderr(res))
    return [p for p in res.stdout.decode("utf-8", errors="replace").split("\0") if p]


def _tracked_line(held: list[str]) -> str:
    return (f"  tracked     {', '.join(held)}: agent-roles is on only on this machine; run "
            f"govern agents render --untrack to stop committing them")


def untrack_files(root: Path, paths: list[str]) -> None:
    """Stop git tracking `paths`, leaving the files where they are."""
    res = _git(root, "rm", "--cached", "--quiet", "--", *paths)
    if res is None or res.returncode:
        raise GitFailed(_stderr(res))


def _stderr(res: subprocess.CompletedProcess | None) -> str:
    if res is None:
        return "git could not be run"
    return res.stderr.decode("utf-8", errors="replace").strip() or f"exit {res.returncode}"


def _forget(ctx) -> int:
    """`agent-roles` is off and rendered files were recorded for this machine only: take the
    exclude block out, remove each file still as it was rendered, then the record, last, so a
    failure leaves the record that brings the next run back here. A file edited since, or one git
    tracks, is kept and named."""
    root = ctx.root
    rows = _rows(root / LOCAL_RECORD)
    try:
        where = exclude_file(root)
        held = tracked(root, [r["path"] for r in rows]) if where is not None else []
        if where is not None and where.exists():
            set_block(where, [])
    except (Unreadable, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except GitFailed as exc:
        print(f"error: git: {exc}", file=sys.stderr)
        return 1
    for row in rows:
        path = root / row["path"]
        if not path.is_file():
            continue
        if row["path"] in held:
            print(f"  kept        {row['path']} (git tracks it)")
            continue
        if file_digest(path) != row.get("sha256"):
            print(f"  kept        {row['path']} (changed since it was rendered)")
            continue
        try:
            remove(root, path)
        except OSError as exc:
            print(f"error: {row['path']}: {exc.strerror or exc}", file=sys.stderr)
            return 1
        print(f"  removed     {row['path']}")
    (root / LOCAL_RECORD).unlink()
    return 0


# ---------------------------------------------------------------------------- command

def run(ctx, check_only: bool = False, untrack: bool = False) -> int:
    """`govern agents render [--check] [--untrack]`. Exit 0 when every file is as rendered; 1
    for a refused param, a conflict, a write that failed, or (under `--check`, which writes
    nothing) anything that would change.

    With `agent-roles` on only in `local.toml`, the files stay on this machine: recorded in
    `LOCAL_RECORD`, and kept out of git through the repository's exclude file. A param
    `local.toml` sets while committed config has the check on is refused."""
    label = "agents render --check" if check_only else "agents render"
    solo = local_only(ctx)
    if untrack and (check_only or not solo):
        print(f"error: --untrack applies only when agent-roles is on in {layout.LOCAL} alone",
              file=sys.stderr)
        return 1
    if ctx.cfg.checks[CHECK].level == "off":
        if (ctx.root / LOCAL_RECORD).is_file() and not check_only:
            code = _forget(ctx)
            if code:
                return code
        print(f"{CHECK} is off: nothing rendered. Turn the option on under [checks.{CHECK}] in "
              f"{layout.CONFIG} to render the roles.")
        return 0
    keys = mixed(ctx)
    for key in keys:
        print(f"  refused     {key} is set in {layout.LOCAL} and would be rendered into "
              f"committed files: move it to {layout.CONFIG}")
    if keys:
        print(f"[FAIL] {label}  (nothing written: fix the settings above)")
        return 1
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
    root = ctx.root
    mine = [r["path"] for r in records(root, todo)]
    where = None
    lines: list[str] = []
    try:
        if solo:
            where = exclude_file(root)
        if where is not None:
            lines = exclude_lines(root, todo)
        block = where is not None and len(set(_block_text(where, lines))) > 1
        held = tracked(root, mine) if where is not None else []
    except (Unreadable, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except GitFailed as exc:
        print(f"error: git: {exc}", file=sys.stderr)
        return 1
    if check_only:
        for t in todo.writes:
            print(f"  {t.state:<11} {ctx.rel(t.path)}")
        for path, role in todo.orphans:
            print(f"  orphaned    {ctx.rel(path)} (role {role} is no longer rendered)")
        if block:
            changes += 1
            print(f"  exclude     {_rel(root, where)}")
        if held:
            print(_tracked_line(held))
        failed = bool(changes or todo.conflicts)
        if failed:
            if solo and any(t.state == MISSING for t in todo.writes):
                print(f"  note        {LOST_NOTE}")
            elif not solo and any(t.state == STALE for t in todo.writes):
                print(f"  note        {BRANCH_NOTE}")
            if changes:      # a conflict is left alone by the render
                print(f"  note        run {RENDER}")
        print(f"[{'FAIL' if failed else 'OK'}] {label}  ({changes} file(s) would change"
              + (f", {len(todo.conflicts)} conflict(s)" if todo.conflicts else "") + ")")
        return 1 if failed else 0
    switching = not solo and (root / LOCAL_RECORD).is_file()
    try:
        done = apply(todo)
    except WriteFailed as exc:
        where_ = f"{exc.error.filename}: " if exc.error.filename else ""
        left = ", ".join(ctx.rel(p) for p in exc.unrestored)
        print(f"error: {where_}{exc.error.strerror or exc.error} — "
              + (f"nothing was written, but {left} could not be put back" if left
                 else "nothing was written"), file=sys.stderr)
        return 1
    for path in done.written:
        print(f"  rendered    {ctx.rel(path)}")
    for path in done.removed:
        print(f"  removed     {ctx.rel(path)}")
    kept = record(root, todo, local=solo)
    if kept:
        print(f"  recorded    {len(mine)} file(s) in {kept}")
    if solo and where is None:
        print("  note        not a git repository: rendered files recorded locally, nothing "
              "excluded")
    elif solo:
        try:
            if set_block(where, lines):
                print(f"  excluded    {len(lines)} path(s) in {_rel(root, where)}")
            if held:
                print(_tracked_line(held))
            if untrack and held:
                untrack_files(root, held)
                print(f"  untracked   {', '.join(held)}")
        except (Unreadable, OSError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except GitFailed as exc:
            print(f"error: git: {exc}", file=sys.stderr)
            return 1
    elif switching:
        try:
            where = exclude_file(root)
            if where is not None and where.exists():
                set_block(where, [])
        except (Unreadable, OSError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except GitFailed as exc:
            print(f"error: git: {exc}", file=sys.stderr)
            return 1
    print(f"[{'FAIL' if todo.conflicts else 'OK'}] {label}  ({len(done.written)} written, "
          f"{len(done.removed)} removed"
          + (f", {len(todo.conflicts)} conflict(s)" if todo.conflicts else "") + ")")
    return 1 if todo.conflicts else 0
