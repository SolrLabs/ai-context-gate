"""Agent runs: gates on what a subagent is launched with and claims to have produced, and
measurement of what each run cost. The work happens in the plugin's hooks (hooks/agents.py);
the checks `agent-gates` and `agent-measure` only carry the on/off switches through the options
cascade.

Standard library and govern.layout only: the hook imports this on every hooked event. Every
function takes the state home, the session's resolved settings and the hook input, and returns
the JSON to print, or None. Nothing here writes into a subagent's context except a gate's
refusal; the coordinator's own tool calls are never logged.

State lives outside the project, in ~/.local/state/context-gate/agents/<session>/, with the
session's resolved settings beside it as <session>.resolved.json.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import time
from pathlib import Path, PurePath

from govern.layout import TOOL

GATE_DEFAULTS = {"briefed": ["developer", "senior-developer"], "claims": True,
                 "deny_push": True, "max_refusals": 2, "checklist": False}
MEASURE_DEFAULTS = {"notice": True}
SESSION_STATE_DAYS = 30     # per-session state untouched this long is swept at session start
HELD_BRIEF_SECONDS = 30     # a held brief older than this belongs to a launch that never started
GENERAL = "general-purpose"
WAIT_FOR_LAST_TURN = 2.0    # seconds SubagentStop waits for the transcript to catch up
EDIT_TOOLS = {"Edit", "Write", "NotebookEdit"}
# What a run is told when its contract is unmet; the unmet lines go between the two.
BLOCK_HEAD = ("Your run is not finished and nothing you sent has been accepted. Your brief's "
              "contract is not met:\n- ")
BLOCK_TAIL = ("\nEach line above is part of the task you were given, and each path in it is a full "
              "path inside your own working tree. Put it right now, even if you have already sent "
              "a summary: write a missing file at exactly that path, and answer a checklist line "
              "as `Key: <answer>` on one line of the report. The Write tool refuses a file named "
              "report.md or findings.md; write those with a Bash heredoc. Then send your final "
              "message again, with the line `Report: <full path>`. If you cannot put a line right, "
              "say so in that message, with the reason.")
CONTRACT = re.compile(r"^\s*(Produces|Red):\s*(.+?)\s*$", re.MULTILINE)
BRIEF = re.compile(r"(?:^|\s)Brief:[ \t]*([^\n]*)")
# A final message that points the reader at a file it says it wrote: "Full report: /abs/path.md".
# One of these words, then at most 20 characters with no line break, then an absolute path that
# begins at a boundary (start of line, whitespace, a quote, a backtick or a colon) and names a
# file (its last part has an extension), so "/api/login" in a sentence is not a claim.
CLAIM = re.compile(r"(?:report|written to|saved to|wrote)[^\n]{0,20}?"
                   r"(?<![^\s`'\":])((?:[A-Za-z]:[\\/]|/)[^\s`'\"]*\.[A-Za-z0-9]+)", re.IGNORECASE)
STATUS = re.compile(r"\AStatus:[ \t]*([A-Za-z_]+)")
FINDINGS = re.compile(r"^Findings:[ \t]*critical=(\d+)[ \t]+important=(\d+)[ \t]+minor=(\d+)",
                      re.MULTILINE)
# A line a brief skeleton leaves for the coordinator to replace with what belongs under its
# heading. `govern build` writes them; a brief still holding one is not launched.
FILL = "<!-- fill:"
FILL_LINE = re.compile(r"<!-- fill:(?:(?!-->).)*-->[ \t]*")
BRIEF_MAX = 1024 * 1024
HEADING = re.compile(r"^ {0,3}#{1,6}[ \t]+(.*?)[ \t#]*$")
ASSIGNMENT = re.compile(r"^[A-Za-z_]\w*=")
HEREDOC = re.compile(r"<<(-?)[ \t]*(?:'([^']*)'|\"([^\"]*)\"|\\?([\w.-]+))")
# Shell keywords and wrappers that may stand before a command word; `env` is followed by its own
# VAR=x words, which the assignment test skips as well.
SHELL_WRAPPERS = {"if", "then", "else", "elif", "do", "while", "until", "{", "!", "time", "exec",
                  "command", "nohup", "env"}
# git's own options that take their value as the next word.
GIT_VALUE_OPTIONS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path",
                     "--super-prefix", "--config-env"}
SUGGEST_SENIOR_SHARE, SUGGEST_SENIOR_RUNS = 0.40, 5
SUGGEST_LARGE_RESULT = 50 * 1024


# --- state ------------------------------------------------------------------------------------

def agents_dir(home: Path) -> Path:
    return home / ".local" / "state" / TOOL / "agents"


def resolved_path(home: Path, sid: str) -> Path:
    return agents_dir(home) / f"{sid}.resolved.json"


def project_key(root) -> str:
    return hashlib.sha1(str(Path(root).resolve()).encode("utf-8")).hexdigest()[:12]


def durable_path(home: Path, root) -> Path:
    return agents_dir(home) / "runs" / f"{project_key(root)}.jsonl"


def _safe(name) -> bool:
    return isinstance(name, str) and bool(name) and "/" not in name and "\\" not in name \
        and name not in (".", "..")


def state_dir(home: Path, sid: str) -> Path:
    d = agents_dir(home) / sid
    d.mkdir(parents=True, exist_ok=True)
    return d


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


REPLACE_TRIES = 10
LOCK_TRIES = 40         # about 1.6 seconds in all before an append goes ahead unlocked
LOCK_STALE = 5.0


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp")
    tmp.write_text(json.dumps(value), encoding="utf-8")
    # Windows refuses a replace while another hook holds the target open for an instant, so
    # two sessions writing at once need a few tries; a temp file is never left behind.
    for attempt in range(REPLACE_TRIES):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == REPLACE_TRIES - 1:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.01 * (attempt + 1))


def append(path: Path, row: dict) -> None:
    """One line, whole, even when two hooks append at the same instant. An append is one write
    at the end of the file on POSIX; Windows seeks and then writes, so two writers can land on
    the same offset and one line is lost. A lock file beside the log orders them. A lock nobody
    releases (a killed hook) is taken over after LOCK_STALE seconds, and a writer that cannot get
    the lock in time writes anyway: a hook never waits on another for long."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(f".{path.name}.lock")
    held = False
    for attempt in range(LOCK_TRIES):
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            held = True
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > LOCK_STALE:
                    lock.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
        except OSError:
            pass        # Windows reports a lock being deleted as a permission error
        time.sleep(0.002 * (attempt + 1))
    try:
        with open(path, "a", encoding="utf-8", newline="") as f:
            f.write(json.dumps(row) + "\n")
    finally:
        if held:
            try:
                lock.unlink(missing_ok=True)
            except OSError:
                pass


def read_json_lines(path: Path) -> list[dict]:
    out = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def sweep(home: Path, now: float) -> None:
    """Remove per-session state (and resolved files) untouched for more than SESSION_STATE_DAYS.
    The durable per-project logs are not session state and stay."""
    base = agents_dir(home)
    if not base.is_dir():
        return
    for p in base.iterdir():
        if p.name == "runs":
            continue
        try:
            newest = p.stat().st_mtime
            if p.is_dir():
                newest = max([newest] + [c.stat().st_mtime for c in p.iterdir()])
            if now - newest > SESSION_STATE_DAYS * 86400:
                shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink()
        except OSError:
            pass


def _checklist(cfg) -> dict:
    """Each rendered role's hand-back checklist, key to text, with the operator's extra lines.
    Empty when the `agent-roles` check is missing or off."""
    from govern import roles     # not at the top: the hook imports this module on every event
    chk = cfg.checks.get("agent-roles")
    if chk is None or chk.level == "off":
        return {}
    params = {"extra_roles": list(chk.params.get("extra_roles", [])),
              "handback_checks": list(chk.params.get("handback_checks", []))}
    return {role: roles.handback_texts(role, params) for role in roles.rendered(params)}


def resolve(cfg, root: Path) -> dict:
    """The two options' effective settings for this project: each option's params, or None when
    it is off. `root` is where the durable log is keyed."""
    def settings(cid: str, defaults: dict):
        chk = cfg.checks.get(cid)
        if chk is None or chk.level == "off":
            return None
        return {k: chk.params.get(k, v) for k, v in defaults.items()}
    gates = settings("agent-gates", GATE_DEFAULTS)
    if gates is not None:
        gates["briefed"] = [str(t) for t in gates["briefed"]]
        checklist = _checklist(cfg) if gates["checklist"] else {}
        gates["checklist_keys"] = {r: list(t) for r, t in checklist.items()}
        gates["checklist_texts"] = checklist
    return {"gates": gates, "measure": settings("agent-measure", MEASURE_DEFAULTS),
            "root": str(Path(root).resolve())}


# --- briefs and contracts ---------------------------------------------------------------------

def brief_ref(prompt: str, cwd: str | None = None) -> str | None:
    """The text after `Brief:` in a launch prompt. A quoted path (quotes or backticks) is read to
    its closing mark. Unquoted, it is the rest of the line, cut at spaces from the right until
    what is left is a file that exists (a relative one is looked for under `cwd`); with none, the
    whole rest of the line is the missing brief."""
    found = BRIEF.search(prompt or "")
    if not found:
        return None
    rest = found.group(1).strip()
    if rest[:1] in ("`", "'", '"'):
        end = rest.find(rest[0], 1)
        return rest[1:end if end > 0 else len(rest)].strip()
    rest = rest.rstrip(".,;:)`'\"")
    words = rest.split(" ")
    for n in range(len(words), 0, -1):
        candidate = " ".join(words[:n]).rstrip(".,;:)`'\"")
        try:
            if candidate and (Path(cwd) / candidate if cwd else Path(candidate)).is_file():
                return candidate
        except OSError:
            pass
    return rest


def brief_path(prompt: str, flavor: type[PurePath] = Path,
               cwd: str | None = None) -> Path | None:
    """The brief file a launch prompt names: an absolute name on `flavor` (the running platform's
    paths unless a test says otherwise) as it is; a relative one under `cwd`, the launching
    session's working folder, and None without one."""
    raw = brief_ref(prompt, cwd)
    if raw is None:
        return None
    if flavor(raw).is_absolute():
        return Path(raw)
    return Path(cwd) / raw if cwd and raw else None


def _resolve(raw: str, base: Path | None) -> str:
    if base is None or Path(raw).is_absolute():
        return raw
    return str(base / raw)


def contract(prompt: str, cwd: str | None = None) -> dict:
    """The paths a brief promises: `Produces: a, b` (files) and `Red: dir` (failing-test output).
    They are read from the dispatch prompt and from the brief file it names with `Brief: path`,
    so the contract does not depend on the coordinator retyping it into every launch. A relative
    path resolves against the brief file's folder, or against `cwd` when the line sits in the
    prompt itself."""
    out = {"produces": [], "red": []}
    cwd = cwd if isinstance(cwd, str) and cwd else None
    brief = brief_path(prompt, cwd=cwd)
    out["brief"] = str(brief) if brief else None
    ref = brief_ref(prompt, cwd)
    # Which paths came from the brief is kept, as (path beside the brief, path as written): a
    # brief named from the repository root is read here, from the launching session's copy, and
    # its relative paths are put again where the agent starts (see `on_start`); and a relative
    # path is looked for first from the root of the tree the agent started in (see `where`).
    rooted = brief is not None and not Path(ref).is_absolute()
    if brief is not None:
        out["in_brief"] = {"produces": [], "red": []}
    if rooted:
        out["brief_ref"] = ref
    if cwd:
        out["launch_cwd"] = cwd
    sources = [(prompt or "", Path(cwd) if cwd else None)]
    if brief is not None:
        try:
            sources.append((brief.read_text(encoding="utf-8"), brief.parent))
        except (OSError, ValueError):
            pass
    for n, (text, base) in enumerate(sources):
        for key, value in CONTRACT.findall(text):
            for p in (p.strip().strip("`'\"").rstrip(".") for p in value.split(",")):
                raw, p = p, _resolve(p, base) if p else p
                if p and p not in out[key.lower()]:
                    out[key.lower()].append(p)
                    if brief is not None and n and not Path(raw).is_absolute():
                        out["in_brief"][key.lower()].append([p, raw])
    return out


def unfilled(text: str) -> list[str]:
    """The headings of a brief's sections that still hold a skeleton's instruction line (a line
    that is only an HTML comment starting `<!-- fill:` and ending `-->`, at column 0), each once,
    in the brief's order; a line above the first heading is named `line N`. A quotation of the
    marker, indented, fenced or inside a sentence, is not that line."""
    out, heading = [], None
    for n, line in enumerate(text.removeprefix("\ufeff").splitlines(), 1):
        found = HEADING.match(line)
        if found:
            heading = found.group(1)
        elif FILL_LINE.fullmatch(line.rstrip()):
            name = heading or f"line {n}"
            if name not in out:
                out.append(name)
    return out


# --- the push gate ----------------------------------------------------------------------------

def _segments(command: str) -> list[str] | None:
    """The command split into simple commands on ; && || | & ( ) backticks and newlines, outside
    quotes, with heredoc bodies and comments dropped. None when it cannot be split (an unclosed
    quote)."""
    segs, cur, pending = [], [], []
    quote, i, n = None, 0, len(command)
    while i < n:
        c = command[i]
        if quote:
            cur.append(c)
            if c == "\\" and quote == '"' and i + 1 < n:
                cur.append(command[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            cur.append(command[i:i + 2])
            i += 2
            continue
        if c in "'\"":
            quote = c
        elif c == "#" and (not cur or cur[-1][-1:] in " \t"):
            while i < n and command[i] != "\n":
                i += 1
            continue
        elif command.startswith("<<", i) and not command.startswith("<<<", i):
            found = HEREDOC.match(command, i)
            if found:
                pending.append((found.group(1) == "-",
                                next(g for g in found.groups()[1:] if g is not None)))
                cur.append(" ")
                i = found.end()
                continue
        elif c == "\n":
            segs.append("".join(cur))
            cur = []
            i += 1
            for strip_tabs, delim in pending:
                while i < n:
                    end = command.find("\n", i)
                    line = command[i:] if end < 0 else command[i:end]
                    i = n if end < 0 else end + 1
                    if (line.lstrip("\t") if strip_tabs else line) == delim:
                        break
            pending = []
            continue
        elif c in ";|&()`":
            segs.append("".join(cur))
            cur = []
            i += 1
            continue
        cur.append(c)
        i += 1
    if quote:
        return None
    segs.append("".join(cur))
    return segs


def pushes(command: str) -> bool:
    """Whether any simple command in `command` is `git push`, by token: its first word is git
    and the first word after git's own options is push. Text in quotes, in a heredoc, in a commit
    message or after `echo` never matches. A command that cannot be parsed does not match."""
    if not isinstance(command, str):
        return False
    try:
        segs = _segments(command)
        if segs is None:
            return False
        for seg in segs:
            words = shlex.split(seg)
            while words and (ASSIGNMENT.match(words[0]) or words[0] in SHELL_WRAPPERS):
                words.pop(0)
            if not words or words[0] != "git":
                continue
            i = 1
            while i < len(words) and words[i].startswith("-"):
                i += 2 if words[i] in GIT_VALUE_OPTIONS else 1
            if i < len(words) and words[i] == "push":
                return True
    except ValueError:
        return False
    return False


def _deny(reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


# --- launch -----------------------------------------------------------------------------------

def _brief_base(home, sid, data, prompt) -> str | None:
    """The folder a relative `Brief:` is read from: the session's resolved root when the file is
    there (the coordinator's shell may have moved into a worktree), else the hook's `cwd`."""
    cwd = data.get("cwd") if isinstance(data.get("cwd"), str) and data.get("cwd") else None
    root = (read_json(resolved_path(home, sid)) or {}).get("root")
    if isinstance(root, str) and root:
        ref = brief_ref(prompt, root)
        if ref and not Path(ref).is_absolute() and (Path(root) / ref).is_file():
            return root
    return cwd


def on_pre_launch(home, sid, gates, data) -> dict | None:
    inp = data.get("tool_input") or {}
    agent_type = inp.get("subagent_type") or GENERAL
    prompt = inp.get("prompt") or ""
    cwd = _brief_base(home, sid, data, prompt)
    if gates and agent_type in gates["briefed"]:
        ref = brief_ref(prompt, cwd)
        if ref is None:
            why = "has no `Brief:` line"
        elif not (Path(cwd) / ref if cwd else Path(ref)).is_file():
            why = f"names `Brief: {ref}`, which does not exist"
        else:
            why = None
        if why:
            return _deny(
                f"A {agent_type} is launched from a brief file, and this prompt {why}. Write the "
                "brief to a file (the task, then `Produces: <report path>` and, for a bug fix, "
                "`Red: <folder>`), and launch again with a prompt that holds the line "
                "`Brief: <path to that file>` (from the repository root, or absolute).")
    brief = brief_path(prompt, cwd=cwd) if gates else None
    if brief is not None:
        # Whatever the agent's type: a brief it is launched from must have been filled in. One
        # that cannot be read, is over 1 MB or is not UTF-8 holds no instruction line anyone can
        # name, and is let through.
        try:
            with open(brief, "rb") as fh:
                raw = fh.read(BRIEF_MAX + 1)
            left = unfilled(raw.decode("utf-8")) if len(raw) <= BRIEF_MAX else []
        except (OSError, ValueError):
            left = []
        if left:
            return _deny(
                f"A {agent_type} cannot be launched from {brief}: the brief is not filled in: "
                f"{', '.join(left)}. Replace each line that starts `{FILL}` under those "
                "headings with what belongs there, then launch again.")
    d = state_dir(home, sid)
    write_json(d / f"pending-{time.time():.6f}-{os.getpid()}.json", {
        "agent_type": agent_type, "description": inp.get("description"),
        "tool_use_id": data.get("tool_use_id"), "prompt_chars": len(prompt),
        **contract(prompt, cwd)})
    return None


def on_launch_failed(home, sid, data) -> None:
    """A launch that was refused after PreToolUse held its brief: drop that brief."""
    tool_use_id = data.get("tool_use_id")
    if not isinstance(tool_use_id, str) or not tool_use_id:
        return
    for pending in state_dir(home, sid).glob("pending-*.json"):
        if read_json(pending, {}).get("tool_use_id") == tool_use_id:
            pending.unlink(missing_ok=True)


def on_pre_resume(home, sid, data) -> None:
    """A message to an agent this session launched resumes it. Its contract is the message's
    own `Produces:` and `Red:` lines, never the first brief's again."""
    inp = data.get("tool_input") or {}
    to = inp.get("to")
    if not _safe(to):
        return
    d = state_dir(home, sid)
    if (d / f"launch-{to}.json").is_file():
        write_json(d / f"resume-{to}.json", contract(inp.get("message") or "", data.get("cwd")))


def on_pre_bash(gates, data) -> dict | None:
    if gates and gates["deny_push"] and data.get("agent_id") \
            and pushes((data.get("tool_input") or {}).get("command") or ""):
        return _deny("Pushing is the coordinator's job, not a subagent's. Do not run `git push`: "
                     "commit your work, and say in your final message that it is committed and "
                     "ready to push.")
    return None


def on_start(home, sid, data) -> None:
    """Pair the agent with the oldest held brief of its type written in the last
    HELD_BRIEF_SECONDS; any older one, of any type, is a launch that never started and is
    deleted. A held brief is claimed by a rename, so two starts never take one. Briefs of one type
    launched in the same instant can still swap; SubagentStop then corrects the pairing from the
    transcript's own copy (see `_repair_pairing`).
    SubagentStart fires again when an agent is resumed: keep what its earlier runs were seen to
    spend, so the new run's row holds only its own."""
    d = state_dir(home, sid)
    now = time.time()
    # A claimed file is unlinked at once; one left behind is from a crash between the two.
    for pending in [*d.glob("pending-*.json"), *d.glob("claimed-*.json")]:
        try:
            if now - pending.stat().st_mtime > HELD_BRIEF_SECONDS:
                pending.unlink(missing_ok=True)
        except OSError:
            pass
    path = d / f"launch-{data['agent_id']}.json"
    earlier = read_json(path)
    if isinstance(earlier, dict):
        resume = d / f"resume-{data['agent_id']}.json"
        earlier.update({"produces": [], "red": [], **read_json(resume, {}),
                        "t": time.time(), "blocks": 0, "paired": True, "resumed": True,
                        "ended": None})
        earlier.pop("blocked_on", None)
        resume.unlink(missing_ok=True)
        write_json(path, earlier)
        return
    held = {}
    for pending in sorted(d.glob("pending-*.json")):
        if (read_json(pending, {}) or {}).get("agent_type") != data.get("agent_type"):
            continue
        # Claimed by a rename, which only one process can win: a second start that read the same
        # file finds it gone and goes on to the next.
        claimed = d / f"claimed-{data['agent_id']}.json"
        try:
            os.replace(pending, claimed)
        except OSError:
            continue
        held = read_json(claimed, {})
        claimed.unlink(missing_ok=True)
        if isinstance(held, dict) and held:
            break
        held = {}
    cwd = data.get("cwd") if isinstance(data.get("cwd"), str) and data.get("cwd") else None
    record = {"agent_type": data.get("agent_type"), "produces": [], "red": [], **held,
              "t": time.time(), "paired": bool(held), "blocks": 0, "seen": {}, "ended": None}
    if cwd:
        record["cwd"] = cwd
        if held.get("brief_ref"):
            _in_agents_tree(record, Path(cwd))
    write_json(path, record)


def _in_agents_tree(record: dict, cwd: Path) -> None:
    """A brief named from the repository root is found in the tree the agent started in, which
    may be a worktree of its own: the paths it promised are put there, beside the brief."""
    folder = cwd / Path(record["brief_ref"]).parent
    for key in ("produces", "red"):
        found = (record.get("in_brief") or {}).get(key, [])
        moved = {here: str(folder / raw) for here, raw in found}
        record[key] = [moved.get(p, p) for p in record.get(key) or []]
        if found:
            record["in_brief"][key] = [[moved[here], raw] for here, raw in found]


def _repair_pairing(launch: dict, first_prompt: str, cwd) -> None:
    """A launch paired with another unit's brief (starts that arrive out of order) is put right
    from the agent's own first prompt: when it names a brief that is not the record's, the
    contract is read again from that one. A prompt naming no brief, or a brief that is not a file,
    leaves the record as it is; so does a resumed agent, whose contract is its message's and
    whose first prompt is still the first launch."""
    if launch.get("resumed"):
        return
    base = launch.get("launch_cwd") or (cwd if isinstance(cwd, str) and cwd else None)
    named = brief_ref(first_prompt, base)
    ours = (launch.get("brief_ref"), launch.get("brief"))
    if not named or not any(ours) or named in ours:
        return
    try:
        if not (Path(base) / named if base else Path(named)).is_file():
            return
    except OSError:
        return
    for key in ("produces", "red", "brief", "in_brief", "brief_ref", "launch_cwd"):
        launch.pop(key, None)
    launch.update(contract(first_prompt, base))
    if launch.get("cwd") and launch.get("brief_ref"):
        _in_agents_tree(launch, Path(launch["cwd"]))
    launch["repaired"] = True


def on_launched(home, sid, gates, measure, data) -> dict | None:
    """The model goes in a file of its own: for a background agent this hook runs alongside
    SubagentStart, so the launch record may not exist yet to hold it."""
    resp = data.get("tool_response") or {}
    agent_id, model = resp.get("agentId"), resp.get("resolvedModel")
    if not _safe(agent_id):
        return None
    d = state_dir(home, sid)
    if model:
        write_json(d / f"model-{agent_id}.json", model)
        if measure:
            fill_model(d, home, sid, agent_id, model)
    if data.get("agent_id") or resp.get("status") != "completed":
        return None
    # A foreground agent: its result is this call's result, so no completion notice follows
    # and this is the one place to hand the coordinator its line.
    return context("PostToolUse", notice_line(d, agent_id, gates, measure))


def fill_model(d: Path, home: Path, sid: str, agent_id: str, model: str) -> None:
    """A foreground agent's row is written before its model is known; fill it in, and tell the
    durable log (which is only ever appended to) with a row of its own."""
    rows = read_json_lines(d / "runs.jsonl")
    if any(r.get("agent") == agent_id and not r.get("model") for r in rows):
        for r in rows:
            if r.get("agent") == agent_id and not r.get("model"):
                r["model"] = model
        tmp = d / f".runs.jsonl.{os.getpid()}.{os.urandom(4).hex()}.tmp"
        tmp.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        os.replace(tmp, d / "runs.jsonl")
        root = (read_json(resolved_path(home, sid)) or {}).get("root")
        if root:
            append(durable_path(home, root), {"model_for": agent_id, "session": sid,
                                              "model": model})


# --- one tool call ----------------------------------------------------------------------------

def label(inp: dict) -> str:
    for key in ("file_path", "command", "pattern", "url", "query", "description"):
        if isinstance(inp.get(key), str):
            return inp[key][-120:] if key == "file_path" else inp[key][:120]
    return ""


def on_tool(home, sid, data, failed: bool) -> None:
    inp = data.get("tool_input") or {}
    append(state_dir(home, sid) / "tools.jsonl", {
        "t": time.time(), "agent": data.get("agent_id"), "type": data.get("agent_type"),
        "tool": data.get("tool_name"), "ms": data.get("duration_ms"),
        "in_bytes": len(json.dumps(inp)),
        "out_bytes": len(json.dumps(data.get("tool_response") or data.get("error") or "")),
        "failed": failed, "label": label(inp)})


# --- stop: the gates, then the row ------------------------------------------------------------

def transcript_totals(path: str, final: str = "") -> dict | None:
    """Token totals over every request the agent made. The transcript's shape is internal to
    Claude Code and may change in any release, so anything unexpected gives None, never a guess.
    One request is written as several lines sharing a message id; the last line's usage counts.

    SubagentStop can fire before the agent's last turn reaches the file, which would leave the
    run one request short: wait, briefly, until the file ends with the message the hook was
    handed as the agent's last."""
    deadline = time.time() + WAIT_FOR_LAST_TURN
    while True:
        totals = _read_transcript(path)
        if totals is None or not final or totals.get("last_text", "").strip() == final.strip() \
                or time.time() >= deadline:
            return totals
        time.sleep(0.1)


def _read_transcript(path: str) -> dict | None:
    try:
        per_request, first_prompt, last_text = {}, None, ""
        with open(path, encoding="utf-8") as f:
            for line in f:
                entry = json.loads(line)
                msg = entry.get("message") or {}
                content = msg.get("content")
                if first_prompt is None and entry.get("type") == "user":
                    first_prompt = content if isinstance(content, str) else json.dumps(content)
                usage = msg.get("usage")
                if entry.get("type") == "assistant" and usage and msg.get("id"):
                    per_request[msg["id"]] = usage
                    for block in content if isinstance(content, list) else []:
                        if block.get("type") == "text":
                            last_text = block.get("text") or ""
        if not per_request:
            return None
        keys = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
                "output_tokens")
        total = {k: sum(int(u.get(k) or 0) for u in per_request.values()) for k in keys}
        last = list(per_request.values())[-1]
        return {"requests": len(per_request), **total,
                "context_at_end": sum(int(last.get(k) or 0) for k in keys),
                "first_prompt": first_prompt, "last_text": last_text}
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def agent_tools(d: Path, agent_id: str) -> list[dict]:
    return [r for r in read_json_lines(d / "tools.jsonl") if r.get("agent") == agent_id]


def where(launch: dict, key: str, raw: str) -> list[Path]:
    """Where a contract path may be: a relative one written in the brief first from the root of
    the tree the agent started in (the SubagentStart `cwd`, else the launching session's), then
    where it resolved beside the brief. A path with `..` in it is only looked for where it
    resolved: neither lookup reaches outside the agent's tree or the brief's folder."""
    written = {here: as_written for here, as_written in
               (launch.get("in_brief") or {}).get(key) or []}.get(raw)
    root = launch.get("cwd") or launch.get("launch_cwd")
    if not written or not root or ".." in Path(written).parts or Path(written).is_absolute():
        return [Path(raw)]
    first = Path(root) / written
    return [first] if first == Path(raw) else [first, Path(raw)]


def _fresh_file(p: Path, since: float) -> bool:
    return p.is_file() and p.stat().st_size > 0 and p.stat().st_mtime >= since - 1


_LIST_MARK = re.compile(r"^(?:\d+[.)]|[-*+]|\[[ xX]\])[ \t]+")


def _normal(line: str) -> str:
    """A line without its list marks, backticks and bold marks."""
    line = line.strip()
    while (m := _LIST_MARK.match(line)):
        line = line[m.end():].lstrip()
    return line.replace("`", "").replace("*", "").strip()


def answers(text: str, key: str, texts: dict[str, str] | None = None) -> bool:
    """Whether `text` answers the checklist line `key`: a line that starts with the key and a
    colon, with an answer after it or on the next line, that is not the checklist's own words.
    `texts` is the whole checklist, key to text."""
    texts = texts or {}
    lines = [_normal(line) for line in text.splitlines()]
    own = (texts.get(key) or "").replace("`", "").lower()[:20]
    keys = [k.lower() + ":" for k in texts]
    for n, line in enumerate(lines):
        if not line.lower().startswith(key.lower() + ":"):
            continue
        answer = line[len(key) + 1:].strip()
        if not answer and n + 1 < len(lines):
            nxt = lines[n + 1]
            if nxt and not any(nxt.lower().startswith(k) for k in keys):
                answer = nxt
        if answer and not (own and answer.lower().startswith(own)):
            return True
    return False


def missing(launch: dict, tools: list[dict], final: str, claims: bool,
            keys: list[str] = (), texts: dict[str, str] | None = None) -> list[str]:
    """What the brief's contract promised, or the final message claims, and is not there."""
    since, problems = launch.get("t") or 0, []
    if claims:
        for raw in CLAIM.findall(final or ""):
            raw = raw.rstrip(".,;:)`'\"")
            if Path(raw).is_absolute() and raw not in (launch.get("produces") or []) \
                    and not Path(raw).exists():
                problems.append(f"your final message names {raw}, which does not exist.")
    reports = []          # the Produces: files that passed the checks, for the checklist
    for raw in launch.get("produces") or []:
        paths = where(launch, "produces", raw)
        p = next((q for q in paths if q.is_file()), paths[0])
        issue = None
        if not p.is_file():
            issue = f"{p} does not exist."
        elif p.stat().st_size == 0:
            issue = f"{p} is empty."
        elif p.stat().st_mtime < since - 1:
            issue = f"{p} is older than this run; it was not written by you."
        if issue and not any(_fresh_file(q, since) for q in paths):
            problems.append(issue)
        elif not issue and keys:
            reports.append(p)
    if reports:
        found, first = [], None
        for p in reports:
            try:
                found.append(p.read_text(encoding="utf-8-sig", errors="replace"))
                first = first or p
            except OSError:
                problems.append(f"{p} could not be read for the checklist.")
        if found:
            problems += [f"{first} does not answer the checklist line `{key}`."
                         for key in keys if not any(answers(t, key, texts) for t in found)]
    edits = [r["t"] for r in tools if r.get("tool") in EDIT_TOOLS and not r.get("failed")]
    for raw in launch.get("red") or []:
        paths = where(launch, "red", raw)
        found = [[f for f in q.glob("*") if f.is_file() and f.stat().st_size
                  and f.stat().st_mtime >= since - 1] if q.is_dir() else [] for q in paths]
        files = next((f for f in found if f), [])
        if not files:
            problems.append(f"{paths[0]} holds no failing-test output from this run. Run the "
                            "test against the code before your fix and save its real output "
                            "there.")
        elif edits and min(f.stat().st_mtime for f in files) > max(edits):
            problems.append(f"the output in {paths[0]} was saved after your last edit, so it "
                            "does not show the test failing before the change.")
    return problems


def phase_label(home: Path, root) -> str | None:
    label_ = None
    for row in read_json_lines(durable_path(home, root)):
        if "mark" in row:
            label_ = row["mark"]
    return label_


def summarize(tools: list[dict]) -> dict:
    """What a run's tool calls add up to: per-tool totals, the three largest results, and the
    shell commands run three times or more."""
    by_tool, repeats = {}, {}
    for t in tools:
        slot = by_tool.setdefault(t.get("tool") or "?", {"calls": 0, "ms": 0, "bytes": 0,
                                                         "failed": 0})
        slot["calls"] += 1
        slot["ms"] += int(t.get("ms") or 0)
        slot["bytes"] += int(t.get("out_bytes") or 0)
        slot["failed"] += 1 if t.get("failed") else 0
        if t.get("tool") == "Bash" and t.get("label"):
            repeats[t["label"]] = repeats.get(t["label"], 0) + 1
    largest = sorted(tools, key=lambda t: -int(t.get("out_bytes") or 0))[:3]
    return {"by_tool": by_tool,
            "largest": [{"tool": t.get("tool"), "bytes": int(t.get("out_bytes") or 0),
                         "label": t.get("label")} for t in largest if t.get("out_bytes")],
            "repeated": [{"command": c, "n": n} for c, n in
                         sorted(repeats.items(), key=lambda kv: -kv[1]) if n >= 3]}


def parse_final(final: str) -> dict:
    """`status` from a `Status:` first line and `findings` from a `Findings:` line, when the
    final message has them."""
    status = STATUS.match((final or "").lstrip("﻿"))
    found = FINDINGS.search(final or "")
    return {"status": status.group(1) if status else None,
            "findings": dict(zip(("critical", "important", "minor"), map(int, found.groups())))
            if found else None}


def on_stop(home, sid, gates, measure, root, data) -> dict | None:
    agent_id = data.get("agent_id")
    if not _safe(agent_id):
        return None
    d = state_dir(home, sid)
    launch_path = d / f"launch-{agent_id}.json"
    launch = read_json(launch_path)
    final = data.get("last_assistant_message") or ""
    unseen = not isinstance(launch, dict)
    if unseen:
        # No launch record (the launch hook did not see this agent): fall back to the brief as
        # the transcript holds it.
        launch = {"t": 0, "agent_type": data.get("agent_type"), "blocks": 0, "seen": {}}
    totals = None
    if measure or gates:
        totals = transcript_totals(data.get("agent_transcript_path") or "", final)
    if gates and not launch.get("paired"):
        launch.update(contract((totals or {}).get("first_prompt") or "", data.get("cwd")))
    elif gates and totals:
        _repair_pairing(launch, totals.get("first_prompt") or "", data.get("cwd"))
    tools = agent_tools(d, agent_id)
    if unseen and not launch.get("agent_type") and not tools \
            and not launch.get("produces") and not launch.get("red"):
        # A helper the harness started itself: no type, no brief, no calls. Nothing to gate or log.
        return None
    keys = ((gates.get("checklist_keys") or {}).get(launch.get("agent_type")) or []) if gates else []
    texts = ((gates.get("checklist_texts") or {}).get(launch.get("agent_type")) or {}) if gates else {}
    problems = missing(launch, tools, final, gates["claims"], keys, texts) if gates else []
    if problems and launch.get("blocks", 0) < gates["max_refusals"]:
        launch["blocks"] = launch.get("blocks", 0) + 1
        launch["blocked_on"] = {"unmet": problems, "t": time.time()}
        write_json(launch_path, launch)
        return {"decision": "block", "reason": BLOCK_HEAD + "\n- ".join(problems) + BLOCK_TAIL}
    launch.pop("blocked_on", None)
    launch["ended"] = {"unmet": problems, "blocks": launch.get("blocks", 0),
                       "produced": [] if problems else [
                           str(next((q for q in where(launch, "produces", p) if q.is_file()),
                                    Path(p))) for p in launch.get("produces") or []]}
    now = time.time()
    if measure:
        # The run's own figures: a resumed agent's transcript holds every earlier run too, so
        # take the difference from what the last stop saw.
        seen, run = launch.get("seen") or {}, {}
        if totals:
            for k in ("requests", "input_tokens", "cache_creation_input_tokens",
                      "cache_read_input_tokens", "output_tokens"):
                run[k] = totals[k] - int(seen.get(k) or 0)
            run["context_at_end"] = totals["context_at_end"]
        mine = [r for r in tools if (r.get("t") or 0) >= (launch.get("t") or 0)]
        row = {
            "t": now, "agent": agent_id, "type": launch.get("agent_type"),
            "model": read_json(d / f"model-{agent_id}.json"),
            "description": launch.get("description"),
            "resumed": bool(launch.get("resumed")),
            "wall_s": round(now - (launch.get("t") or now), 1),
            "tokens": run or None, "tool_calls": len(mine),
            "tool_ms": sum(int(r.get("ms") or 0) for r in mine),
            "failed_calls": sum(1 for r in mine if r.get("failed")),
            "result_bytes": sum(int(r.get("out_bytes") or 0) for r in mine),
            **summarize(mine), **parse_final(final),
            "gate_blocks": launch.get("blocks", 0), "gate_unmet": problems,
            "produced": launch["ended"]["produced"]}
        append(d / "runs.jsonl", row)
        if root:
            append(durable_path(home, root),
                   {**row, "session": sid, "phase": phase_label(home, root)})
    launch["seen"] = {**{k: totals[k] for k in totals if k in (
        "requests", "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
        "output_tokens")}, "t": now} if totals and measure else {"t": now}
    launch["blocks"] = 0
    write_json(launch_path, launch)
    return None


# --- the coordinator's one line ---------------------------------------------------------------

def context(event: str, text: str | None) -> dict | None:
    if not text:
        return None
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}


def notice_line(d: Path, agent_id: str, gates, measure) -> str | None:
    """The coordinator's line for a finished run: the routine one with measurement (and `notice`),
    else, with gates, one only when a contract ended unmet."""
    launch = read_json(d / f"launch-{agent_id}.json") or {}
    ended = launch.get("ended") or {}
    if gates and launch.get("blocked_on") and not ended:
        return (f"[agent run] {launch.get('agent_type')}: was stopped on an unmet contract and "
                "has not finished: " + " ".join(launch["blocked_on"].get("unmet") or [])
                + " Treat its report as not filed.")
    if measure and measure.get("notice"):
        rows = [r for r in read_json_lines(d / "runs.jsonl") if r.get("agent") == agent_id]
        if rows:
            return one_line(rows[-1])
    if gates and ended.get("unmet"):
        return f"[agent run] {launch.get('agent_type')}:" + unmet_text(ended)
    if gates and ended.get("produced"):
        return f"[agent run] {launch.get('agent_type')}:" + reports(ended)
    return None


def on_notice(home, sid, gates, measure, data) -> dict | None:
    if data.get("agent_id"):
        return None
    prompt = data.get("prompt") or ""
    found = re.search(r"<task-notification>.*?<task-id>([^<]+)</task-id>", prompt, re.DOTALL)
    if not found or not _safe(found.group(1)):
        return None
    return context("UserPromptSubmit",
                   notice_line(state_dir(home, sid), found.group(1), gates, measure))


def unmet_text(ended: dict) -> str:
    return (f" CONTRACT NOT MET after {ended.get('blocks', 0)} refusals: "
            + " ".join(ended.get("unmet") or []))


def reports(row: dict) -> str:
    """` Report: <full path>` for each file a met contract promised."""
    return "".join(f" Report: {p}" for p in row.get("produced") or [])


def one_line(row: dict) -> str:
    tok = row.get("tokens")
    if tok:
        fresh = tok["input_tokens"] + tok["cache_creation_input_tokens"]
        burn = (f"{tok['requests']} requests; tokens: {fresh:,} new input, "
                f"{tok['cache_read_input_tokens']:,} cached re-read, {tok['output_tokens']:,} "
                f"output; context at end {tok['context_at_end']:,}")
    else:
        burn = "token totals unavailable (transcript unreadable)"
    line = (f"[agent run] {row.get('type')} ({row.get('model')}): {burn}; "
            f"{row['tool_calls']} tool calls, {row['tool_ms'] / 1000:.1f}s in tools, "
            f"{row['wall_s']}s wall.")
    if row.get("status"):
        line += f" Status {row['status']}."
    if row.get("gate_unmet"):
        line += unmet_text({"blocks": row.get("gate_blocks"), "unmet": row["gate_unmet"]})
    elif row.get("gate_blocks"):
        line += f" Contract met after {row['gate_blocks']} refusal(s)."
    return line + reports(row)


# --- the entry point --------------------------------------------------------------------------

def handle(home: Path, resolved: dict, data: dict) -> dict | None:
    """One hook event: the JSON to print, or None. `resolved` is the session's resolved file."""
    gates, measure = resolved.get("gates"), resolved.get("measure")
    if not (gates or measure) or not isinstance(data, dict):
        return None
    sid, event, tool = data.get("session_id"), data.get("hook_event_name"), data.get("tool_name")
    if not _safe(sid):
        return None
    root = resolved.get("root")
    if event == "UserPromptSubmit":
        return on_notice(home, sid, gates, measure, data) \
            if "<task-notification>" in (data.get("prompt") or "") else None
    if event == "SubagentStop":
        return on_stop(home, sid, gates, measure, root, data)
    if event == "SubagentStart":
        if _safe(data.get("agent_id")):
            on_start(home, sid, data)
        return None
    if event == "PreToolUse":
        if tool == "Agent":
            return on_pre_launch(home, sid, gates, data)
        if tool == "SendMessage":
            on_pre_resume(home, sid, data)
        elif tool == "Bash":
            return on_pre_bash(gates, data)
        return None
    if event in ("PostToolUse", "PostToolUseFailure"):
        out = None
        if tool == "Agent" and event == "PostToolUse":
            out = on_launched(home, sid, gates, measure, data)
        elif tool == "Agent":
            on_launch_failed(home, sid, data)
        if data.get("agent_id"):
            on_tool(home, sid, data, failed=event == "PostToolUseFailure")
        return out
    return None


# --- the phase log and its report -------------------------------------------------------------

def mark(home: Path, root, name: str, now: float | None = None) -> Path:
    """Start a phase in the project's durable log."""
    path = durable_path(home, root)
    append(path, {"mark": name, "t": time.time() if now is None else now})
    return path


def load_runs(home: Path, root, everything: bool = False) -> tuple[list[dict], str | None]:
    """The project's runs since the last mark (all of them with `everything`), and that mark's
    label (None when there is none or `everything`)."""
    runs, name, models = [], None, {}
    for row in read_json_lines(durable_path(home, root)):
        if "mark" in row:
            if not everything:
                runs, name = [], row["mark"]
        elif "model_for" in row:
            models[(row.get("session"), row["model_for"])] = row.get("model")
        elif "agent" in row:
            runs.append(row)
    for r in runs:
        if not r.get("model"):
            r["model"] = models.get((r.get("session"), r.get("agent")))
    return runs, name


def _all_tokens(row: dict) -> int:
    tok = row.get("tokens") or {}
    return sum(int(tok.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens",
                                                "cache_read_input_tokens", "output_tokens"))


def build_report(runs: list[dict]) -> dict:
    by_role, by_tool = {}, {}
    for r in runs:
        slot = by_role.setdefault(f"{r.get('type')} ({r.get('model')})", {
            "runs": 0, "tokens": 0, "new_input": 0, "cached_reread": 0, "output": 0,
            "tool_calls": 0, "wall_s": 0.0})
        tok = r.get("tokens") or {}
        slot["runs"] += 1
        slot["tokens"] += _all_tokens(r)
        slot["new_input"] += int(tok.get("input_tokens") or 0) \
            + int(tok.get("cache_creation_input_tokens") or 0)
        slot["cached_reread"] += int(tok.get("cache_read_input_tokens") or 0)
        slot["output"] += int(tok.get("output_tokens") or 0)
        slot["tool_calls"] += int(r.get("tool_calls") or 0)
        slot["wall_s"] = round(slot["wall_s"] + float(r.get("wall_s") or 0), 1)
        for name, t in (r.get("by_tool") or {}).items():
            agg = by_tool.setdefault(name, {"calls": 0, "ms": 0, "bytes": 0, "failed": 0})
            for k in agg:
                agg[k] += int(t.get(k) or 0)
    largest = sorted((dict(x, agent=r.get("agent"), type=r.get("type"))
                      for r in runs for x in r.get("largest") or []),
                     key=lambda x: -x["bytes"])[:5]
    return {"runs": runs, "by_role": by_role, "by_tool": by_tool, "largest": largest,
            "suggestions": suggestions(runs)}


def suggestions(runs: list[dict]) -> list[str]:
    """Fixed rules, each printed only when its pattern is present. Never applied."""
    out = []
    total = sum(_all_tokens(r) for r in runs)
    senior = sum(_all_tokens(r) for r in runs if r.get("type") == "senior-developer")
    if len(runs) >= SUGGEST_SENIOR_RUNS and total and senior / total > SUGGEST_SENIOR_SHARE:
        out.append(f"senior-developer used {senior / total:.0%} of the tokens over {len(runs)} "
                   "runs: review `senior_triggers`, so it is launched only where a trigger applies.")
    resumed = [r for r in runs if r.get("resumed")]
    if len(resumed) >= 2:
        cached = sum(int((r.get("tokens") or {}).get("cache_read_input_tokens") or 0)
                     for r in resumed)
        out.append(f"{len(resumed)} runs were resumed and re-read {cached:,} cached tokens "
                   "between them: prefer a fresh agent with a short brief.")
    commands = {}
    for r in runs:
        for c in {x["command"] for x in r.get("repeated") or []}:
            commands[c] = commands.get(c, 0) + 1
    for command, n in sorted(commands.items(), key=lambda kv: -kv[1]):
        if n >= 2:
            out.append(f"`{command[:70]}` ran three times or more in {n} runs: run scoped "
                       "tests while iterating, and the full command once at the end.")
    big = [x for r in runs for x in r.get("largest") or [] if x["bytes"] > SUGGEST_LARGE_RESULT]
    if big:
        top = max(big, key=lambda x: x["bytes"])
        out.append(f"a {top['tool']} result was {top['bytes'] / 1024:.0f} KB "
                   f"({str(top.get('label'))[-50:]}): read by search or line range, because "
                   "every later turn of that agent re-reads it.")
    refused = [r for r in runs if r.get("gate_blocks")]
    if len(refused) >= 3:
        out.append(f"contracts were refused in {len(refused)} runs: the briefs are not "
                   "carrying the contract; put `Produces:` in the brief file.")
    blocked = {}
    for r in runs:
        if r.get("status") == "BLOCKED":
            blocked[r.get("type")] = blocked.get(r.get("type"), 0) + 1
    for role, n in sorted(blocked.items(), key=lambda kv: -kv[1]):
        if n >= 3:
            out.append(f"{role} finished BLOCKED {n} times: the briefs leave design open; "
                       "settle it in the plan before launching.")
    return [f"Suggestion: {s}" for s in out]


def render_report(report: dict, name: str | None, everything: bool) -> str:
    runs = report["runs"]
    scope = "all runs" if everything else f"since mark '{name}'" if name else "no mark yet"
    out = [f"{len(runs)} agent runs ({scope}).", ""]
    out += [one_line(r).replace("[agent run] ", "- ") for r in runs]
    if report["by_role"]:
        out += ["", "By role and model (runs, tokens, new input, cached re-read, output):"]
        out += [f"- {k}: {v['runs']}, {v['tokens']:,}, {v['new_input']:,}, "
                f"{v['cached_reread']:,}, {v['output']:,}"
                for k, v in sorted(report["by_role"].items(), key=lambda kv: -kv[1]["tokens"])]
    if report["by_tool"]:
        out += ["", "By tool (calls, seconds, result KB, failed):"]
        out += [f"- {k}: {v['calls']}, {v['ms'] / 1000:.1f}s, {v['bytes'] / 1024:.1f} KB, "
                f"{v['failed']}" for k, v in sorted(report["by_tool"].items(),
                                                     key=lambda kv: -kv[1]["bytes"])]
    if report["largest"]:
        out += ["", "Largest results (each is re-read on every later turn of its agent):"]
        out += [f"- {x['bytes'] / 1024:.1f} KB {x['tool']} {str(x.get('label'))[-70:]}"
                for x in report["largest"]]
    if report["suggestions"]:
        out += [""] + report["suggestions"]
    return "\n".join(out)
