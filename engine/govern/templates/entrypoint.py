#!/usr/bin/env python3
"""context-gate: this project's governance gate.

Installed as .context-gate/bin/govern (the gate), bin/upgrade (moves the project to a
newer engine) and bin/uninstall (removes the tool).
The logic lives in the context-gate engine; the policy lives in ../config.toml.

    python3 .context-gate/bin/govern check [--project P]
    python3 .context-gate/bin/govern index
    python3 .context-gate/bin/govern baseline [--allow-raise]
    python3 .context-gate/bin/govern next-id --project P
    python3 .context-gate/bin/govern show --project P IDS | --list | --lines
    python3 .context-gate/bin/govern find --project P PATTERN
    python3 .context-gate/bin/govern trap-add --project P --title T --bites B
    python3 .context-gate/bin/govern usage install | uninstall | resolve [--json]

Which engine runs: exactly the version config.toml pins, from
~/.local/share/context-gate/engines/<version>/. If it is not installed and config.toml
names a [governance] source (a git URL or path holding the engine, tagged v<version>), it is
installed from there first, so a fresh clone or a CI runner bootstraps itself. A pin of only a
series ("0.4") runs the newest installed release in that series.
.context-gate/local.toml, when present and naming an installed beta, runs that beta instead, on
this machine only (`govern beta`); it is never committed.
$GOVERN_ENGINE overrides all of this with an explicit engine directory, for engine development.

    python3 .context-gate/bin/govern beta [on [X.Y.Z-beta.N] | off]

switches this project, on this machine, to a beta engine (the newest installed or tagged at the
source, when none is named) and the local beta plugin, and back. `beta on` installs the engine
and the plugin from the source when either is missing; nothing else ever fetches a beta. It is
handled here, before any engine loads, so it works when the beta is broken or gone.
"""
import errno
import json
import os
import re
import sys
import tomllib
from pathlib import Path

GOV_DIR = Path(__file__).resolve().parent.parent
ROOT = GOV_DIR.parent
HOME = Path(os.environ.get("HOME") or Path.home())
ENGINES = HOME / ".local" / "share" / "context-gate" / "engines"
BETA_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)-beta\.([1-9]\d*)", re.ASCII)
LOCAL = GOV_DIR / "local.toml"
PLUGIN = "context-gate"
STABLE_PLUGIN = f"{PLUGIN}@context-gate"
BETA_PLUGIN = f"{PLUGIN}@skills-dir"
PLUGIN_DIR = HOME / ".claude" / "skills" / PLUGIN
# Seconds a clone may take before it is given up: generous for one shallow tag over a slow link,
# and short enough that a dead one ends a session start in a line instead of hanging it. A slower
# link raises it with CONTEXT_GATE_FETCH_TIMEOUT.
CLONE_TIMEOUT = 120
FETCH_TIMEOUT_VAR = "CONTEXT_GATE_FETCH_TIMEOUT"
# Seconds after which a stage directory in ENGINES is what a killed install left. A live install
# holds its stage for about a second, so nothing younger than this is touched.
STAGE_AGE = 300
SETTINGS_LOCAL = ROOT / ".claude" / "settings.local.json"


def die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(2)


def note(msg: str) -> None:
    print(f"context-gate: {msg}", file=sys.stderr)


def version_key(name: str) -> tuple:
    """Digits only: a beta is no version here, which is what keeps bin/upgrade stable-only."""
    parts = name.split(".")
    return tuple(int(x) for x in parts) if all(x.isdigit() for x in parts) else ()


def _local_governance(strict: bool = False) -> dict:
    """local.toml's [governance] table; {} when there is none. A file that cannot be read or
    parsed raises (OSError, ValueError) when strict, else reads as {}."""
    try:
        with LOCAL.open("rb") as fh:
            gov = tomllib.load(fh).get("governance")
    except (OSError, ValueError):
        if strict:
            raise
        return {}
    return gov if isinstance(gov, dict) else {}


def _local_engine(strict: bool = False) -> object:
    """local.toml's [governance] engine, whatever its type; None when there is none."""
    return _local_governance(strict).get("engine")


def local_beta(pin: str) -> Path | None:
    """The beta engine local.toml names, when it can run here; else None, after one line saying
    why. Never fatal: an unusable local.toml means the committed pin."""
    if not LOCAL.is_file():
        return None
    off = f"running the committed pin {pin}; govern beta off to clear it"
    try:
        beta = _local_engine(strict=True)
    except (OSError, ValueError) as exc:
        note(f"{GOV_DIR.name}/local.toml is unreadable ({exc}), {off}")
        return None
    if not isinstance(beta, str) or not BETA_RE.fullmatch(beta):
        note(f"{GOV_DIR.name}/local.toml names no beta engine ({beta!r}), {off}")
        return None
    path = ENGINES / beta
    if not (path / "govern" / "cli.py").is_file():
        note(f"beta {beta} is not installed, {off}")
        return None
    note(f"running beta {beta} on this machine (committed pin {pin}); govern beta off to leave it")
    return path


def _clone_timeout() -> int:
    """The clone limit in seconds: CONTEXT_GATE_FETCH_TIMEOUT when it is a positive whole
    number, else CLONE_TIMEOUT."""
    raw = os.environ.get(FETCH_TIMEOUT_VAR, "").strip()
    return int(raw) if raw.isascii() and raw.isdigit() and int(raw) > 0 else CLONE_TIMEOUT


def _git_env() -> dict:
    """The environment for a git call that reaches a source: git is told not to prompt unless
    a person is at a terminal to answer."""
    try:
        interactive = sys.stdin is not None and sys.stdin.isatty()
    except (OSError, ValueError):           # stdin closed
        interactive = False
    return os.environ.copy() if interactive else {**os.environ, "GIT_TERMINAL_PROMPT": "0"}


def _run_clone(cmd: list[str], timeout: int):
    """Run `cmd` as subprocess.run would, in a process group of its own: when it does not
    finish in `timeout` seconds, git and everything it started are killed. Returns the
    CompletedProcess, or raises subprocess.TimeoutExpired."""
    import signal
    import subprocess
    kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" \
        else {"start_new_session": True}
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            env=_git_env(), **kw)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True, timeout=10)
            else:
                os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, subprocess.SubprocessError):
            proc.kill()
        try:
            proc.communicate(timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass                            # a grandchild still holds the pipe: carry on
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def _rmtree(path: Path) -> None:
    """Delete a directory this file made, read-only as an installed engine is; a symlink, or
    anything that is no directory, is left alone."""
    import shutil
    if path.is_symlink() or not path.is_dir():
        return
    for p in [path, *path.rglob("*")]:
        if not p.is_symlink():
            p.chmod(p.stat().st_mode | (0o700 if p.is_dir() else 0o600))
    shutil.rmtree(path)


def _holds_engine(path: Path) -> bool:
    return (path / "govern" / "cli.py").is_file()


def _refuse_option(source: object) -> None:
    """Die when git would read the source as an option: a value starting with "-" is no URL
    and no path, and some git options name a command to run. Every call that hands a source to
    git comes here first, and puts `--` before it."""
    if isinstance(source, str) and source.startswith("-"):
        die(f"{GOV_DIR.name}/config.toml [governance] source {source!r} starts with \"-\", "
            "which git would read as an option; name a git URL or a path")


def _tags(listing: str) -> list[str]:
    """What follows `refs/tags/v` in each line of `git ls-remote --tags` output, read from the
    start of the ref: a tag named `x/refs/tags/v1.2.3` is not version 1.2.3."""
    refs = (line.split("\t", 1)[-1] for line in listing.splitlines())
    return [ref.removeprefix("refs/tags/v") for ref in refs if ref.startswith("refs/tags/v")]


def _clear_stages(version: str) -> None:
    """Delete the stage directories killed installs of this version left in ENGINES. One
    younger than STAGE_AGE is left, and so is one that cannot be deleted: a dot name is never
    taken for an engine, and blocks nothing."""
    import time
    try:
        stages = [p for p in ENGINES.iterdir() if p.name.startswith(f".{version}.")]
    except OSError:
        return
    for stage in stages:
        try:
            if time.time() - stage.lstat().st_mtime > STAGE_AGE:
                _rmtree(stage)
        except OSError:
            pass


def _set_aside(dest: Path, aside: Path) -> None:
    """Take a directory with no engine in it out of an engine's place: moved aside, then
    deleted there, never deleted where it is. It is looked at again once aside, so an engine
    another run moved in at that instant is moved back, not lost. A symlink, or anything that
    is no directory, stays where it is."""
    if dest.is_symlink() or not dest.is_dir():
        return
    try:
        os.rename(dest, aside)
    except OSError:
        return                              # gone or held: the next move into place tells
    try:
        os.utime(aside)                     # fresh, so a sibling's _clear_stages leaves it
    except OSError:
        pass
    if _holds_engine(aside):
        try:
            os.rename(aside, dest)
            return
        except OSError:
            pass                            # a third run filled the place: this one is spare
    try:
        _rmtree(aside)
    except OSError:
        pass                                # a sibling deleted it first: it is gone either way


def _place(engine: Path, version: str) -> None:
    """Install a copy of `engine` (a govern package) as ENGINES/<version>, read-only. The copy
    is made in a directory of its own in ENGINES (a dot name, unique to this run), made
    read-only there, and moved into place with one rename: the engine directory is whole and
    read-only from the moment it exists, and two runs installing the same engine never share a
    stage. An engine that is in place by then, another run's, is kept and this copy discarded;
    a directory in place with no engine in it is set aside first. Raises OSError, with no
    stage left and no engine removed."""
    import shutil
    import tempfile
    import time
    dest = ENGINES / version
    ENGINES.mkdir(parents=True, exist_ok=True)
    _clear_stages(version)
    if _holds_engine(dest):
        return                              # installed while this run was fetching it
    stage = Path(tempfile.mkdtemp(dir=ENGINES, prefix=f".{version}."))
    try:
        shutil.copytree(engine, stage / "govern", ignore=shutil.ignore_patterns("__pycache__"))
        stage.chmod(0o755)                  # mkdtemp makes it 0700: others read an engine
        for path in sorted(stage.rglob("*"), reverse=True) + [stage]:
            path.chmod(path.stat().st_mode & ~0o222)
        for last in (False, True):
            if _holds_engine(dest):
                return
            try:
                for retry in range(6):      # Windows refuses a rename for a moment at times
                    try:
                        os.rename(stage, dest)
                        return
                    except PermissionError:
                        if retry == 5 or _holds_engine(dest):
                            raise
                        time.sleep(0.2)
            except OSError:
                if _holds_engine(dest):
                    return                  # another run's landed first: theirs is the engine
                if last:
                    raise
            _set_aside(dest, stage.with_name(stage.name + ".broken"))
    finally:
        try:
            _rmtree(stage)                  # still here unless it was moved into place
        except OSError:
            pass


def bootstrap(version: str, source: str, then=None) -> Path:
    """Install one engine release from its source: the tag's engine/govern, read-only, copied
    beside its place and moved in whole (_place), so a copy that fails leaves no engine
    directory. An engine already installed is left as it is, whenever it got there. `then`,
    when given, is called with the clone of the tag before the clone is deleted."""
    import subprocess
    import tempfile
    _refuse_option(source)
    dest = ENGINES / version
    have = _holds_engine(dest)
    if not have:
        print(f"context-gate: installing engine {version} from {source}", file=sys.stderr)
    limit = _clone_timeout()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        try:
            res = _run_clone(["git", "clone", "--quiet", "--depth", "1", "--branch",
                              f"v{version}", "--", source, tmp], limit)
        except subprocess.TimeoutExpired:
            die(f"could not fetch engine v{version} from {source}: git clone did not finish in "
                f"{limit} seconds; set {FETCH_TIMEOUT_VAR} to allow longer")
        except OSError as exc:              # no git to run
            die(f"could not fetch engine v{version} from {source}: {exc}")
        if res.returncode != 0:
            die(f"could not fetch engine v{version} from {source}: "
                + "; ".join(line.strip() for line in res.stderr.splitlines() if line.strip()))
        engine = Path(tmp) / "engine" / "govern"
        try:
            carried = (engine / "__init__.py").read_text(encoding="utf-8")
        except (OSError, ValueError):
            carried = ""
        if f'__version__ = "{version}"' not in carried:
            die(f"{source} tag v{version} does not carry engine {version}")
        if not have:
            try:
                _place(engine, version)
            except OSError as exc:
                die(f"could not install engine {version} in {ENGINES} ({exc})")
        if then:
            then(Path(tmp))
    return dest


def engine_path() -> Path:
    override = os.environ.get("GOVERN_ENGINE")
    if override:
        return Path(override)
    try:
        with (GOV_DIR / "config.toml").open("rb") as fh:
            gov = tomllib.load(fh).get("governance", {})
    except (OSError, tomllib.TOMLDecodeError) as exc:
        die(f"cannot read the engine pin from {GOV_DIR.name}/config.toml ({exc})")
    pin, source = gov.get("engine"), gov.get("source")
    if not isinstance(pin, str) or not pin:
        die(f"{GOV_DIR.name}/config.toml has no [governance] engine pin")
    if not version_key(pin):
        die(f"{GOV_DIR.name}/config.toml engine pin {pin} is not a release")
    beta = local_beta(pin)
    if beta:
        return beta
    if len(pin.split(".")) == 3:
        exact = ENGINES / pin
        if (exact / "govern" / "cli.py").is_file():
            return exact
        if source:
            return bootstrap(pin, source)
        die(f"context-gate engine {pin} is not installed in {ENGINES} — install it, or "
            f"name a [governance] source in config.toml to install it automatically")
    found = sorted((p for p in ENGINES.glob(pin + ".*")
                    if version_key(p.name) and (p / "govern" / "cli.py").is_file()),
                   key=lambda p: version_key(p.name))
    if not found:
        die(f"no context-gate engine {pin}.x is installed in {ENGINES}")
    return found[-1]


def upgrade(argv: list[str]) -> int:
    """Upgrade to the newest release (or --to X.Y.Z): install it from the source if needed,
    then run that engine's installer, which pins it, refreshes these scripts and writes
    upgrade-report.md."""
    import subprocess
    target = argv[argv.index("--to") + 1] if "--to" in argv else None
    with (GOV_DIR / "config.toml").open("rb") as fh:
        gov = tomllib.load(fh).get("governance", {})
    source = gov.get("source")
    if target is None:
        installed = [p.name for p in ENGINES.glob("*.*.*") if version_key(p.name)] \
            if ENGINES.is_dir() else []
        released = []
        if source:
            _refuse_option(source)
            res = subprocess.run(["git", "ls-remote", "--tags", "--refs", "--", source],
                                 capture_output=True, text=True, timeout=30,
                                 env=_git_env())
            released = [tag for tag in _tags(res.stdout) if version_key(tag)]
        candidates = installed + released
        if not candidates:
            die("no engine release found: none installed, and no [governance] source to ask")
        target = max(candidates, key=version_key)
    if BETA_RE.fullmatch(target):
        die(f"an upgrade takes a release, and {target} is a beta; "
            f"run a beta with govern beta on {target}")
    if target == gov.get("engine"):
        print(f"already on context-gate {target}")
        return 0
    engine = ENGINES / target
    if not (engine / "govern" / "cli.py").is_file():
        if not source:
            die(f"engine {target} is not installed and config.toml names no [governance] source")
        engine = bootstrap(target, source)
    env = {k: v for k, v in os.environ.items() if k != "GOVERN_ENGINE"}
    env["PYTHONPATH"] = str(engine)
    # -P: the working directory must not shadow the fetched engine on PYTHONPATH (3.11+).
    return subprocess.run([sys.executable, "-P", "-m", "govern.installer", "upgrade", "--root",
                           str(ROOT)], env=env).returncode


# govern beta: handled here, before any engine loads, so it works when the beta is broken or gone.
def _load_settings() -> dict:
    """settings.local.json as a dict ({} when absent); ValueError when it is not a JSON object
    whose enabledPlugins (if any) is an object."""
    if not SETTINGS_LOCAL.is_file():
        return {}
    data = json.loads(SETTINGS_LOCAL.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    if not isinstance(data.get("enabledPlugins", {}), dict):
        raise ValueError("its enabledPlugins is not a JSON object")
    return data


def _settings() -> dict:
    """settings.local.json as a dict ({} when absent). Dies, writing nothing, when it is not a
    JSON object: a file the user owns is never rewritten from a guess."""
    try:
        return _load_settings()
    except (OSError, ValueError) as exc:
        die(f"{SETTINGS_LOCAL} is not valid settings JSON ({exc}); fix it, then run this again")


def _write_settings(data: dict) -> None:
    """Write data back in the file's own BOM and line endings; an empty object removes the file."""
    if not data:
        if SETTINGS_LOCAL.is_file():
            SETTINGS_LOCAL.unlink()
        return
    raw = SETTINGS_LOCAL.read_bytes() if SETTINGS_LOCAL.is_file() else b""
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if b"\r\n" in raw:
        text = text.replace("\n", "\r\n")
    tmp = SETTINGS_LOCAL.with_name(SETTINGS_LOCAL.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8",
                      newline="") as fh:
            fh.write(text)
        os.replace(tmp, SETTINGS_LOCAL)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def _exclude_plan(*paths: Path) -> tuple[Path, list[str]] | None:
    """What making git ignore each path takes, through info/exclude, never a committed
    .gitignore: the exclude file and the lines to add to it; None when git ignores them all
    already. Only asks git, writing nothing, so it runs with the other checks. Dies when git
    cannot answer: a beta pin must never reach a commit unnoticed."""
    import subprocess

    def git(*args: str):
        try:
            return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True,
                                  text=True)
        except OSError as exc:
            die(f"git cannot say whether {paths[0].name} is ignored ({exc})")

    def where(res) -> Path:
        # Old git echoes a flag it does not know and exits 0; a relative answer is ROOT's.
        return (ROOT / res.stdout.strip()).resolve()

    todo = []
    for path in paths:
        rel = path.relative_to(ROOT).as_posix()
        ignored = git("check-ignore", "-q", rel)
        if ignored.returncode == 0:
            continue
        if ignored.returncode != 1:
            die(f"git cannot say whether {rel} is ignored: {ignored.stderr.strip()}")
        todo.append(path)
    if not todo:
        return None
    res = git("rev-parse", "--git-path", "info/exclude")
    top = git("rev-parse", "--show-toplevel")
    common = git("rev-parse", "--git-common-dir")
    if res.returncode or top.returncode or common.returncode:
        die(f"git cannot say where {todo[0].name} would be ignored: "
            f"{(res.stderr or top.stderr or common.stderr).strip()}")
    exclude, gitdir = where(res), where(common)
    if not exclude.parent.is_dir() or not exclude.parent.is_relative_to(gitdir):
        die(f"git names {exclude} for its exclude file, which is not inside {gitdir}")
    lines = []
    for path in todo:
        try:
            lines.append("/" + path.resolve().relative_to(Path(top.stdout.strip()).resolve())
                         .as_posix())
        except ValueError:
            die(f"{path} is not inside the git repository at {top.stdout.strip()}")
    return exclude, lines


def _exclude(plan: tuple[Path, list[str]] | None) -> None:
    """Add to git's exclude file the lines _exclude_plan found missing from it."""
    if not plan:
        return
    exclude, lines = plan
    raw = exclude.read_bytes() if exclude.is_file() else b""
    lead = "\n" if raw and not raw.endswith(b"\n") else ""
    with exclude.open("a", encoding="utf-8") as fh:
        fh.write(lead + "".join(f"{line}\n" for line in lines))


def _snapshot(*paths: Path) -> dict:
    return {p: p.read_bytes() if p.is_file() else None for p in paths}


def _restore(saved: dict) -> None:
    for path, raw in saved.items():
        if raw is not None:
            path.write_bytes(raw)
        elif path.is_file():
            path.unlink()


def _put_back(saved: dict, failed: str) -> None:
    """After a switch that failed part-way: put each file back as saved, then die saying what
    failed. A file that cannot be put back either is no traceback: the others are still tried,
    and the message names any file that is not as it was. It does not say nothing changed: the
    git exclude may hold new lines."""
    differ, why = [], None
    for path, raw in saved.items():
        try:
            _restore({path: raw})
        except OSError as exc:
            try:
                same = (path.read_bytes() if path.is_file() else None) == raw
            except OSError:
                same = False
            if not same:
                differ.append(path.name)
                why = exc
    if differ:
        die(f"{failed}; could not put {' and '.join(differ)} back as before ({why}), so check "
            "by hand")
    die(f"{failed}; local.toml and .claude/settings.local.json are as they were")


def _replace(path: Path, raw: bytes) -> None:
    """Write raw over path whole: to a temp file beside it, then moved over it, so a full disk
    or a kill mid-write leaves the old file as it was. A read-only file is refused on every OS:
    the move alone would replace one wherever the directory allows it, except on Windows."""
    if path.is_file() and not os.access(path, os.W_OK):
        raise PermissionError(errno.EACCES, "the file is read-only", str(path))
    path = path.resolve()       # a symlinked file is replaced where it points, not by a regular file
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_bytes(raw)
        if path.is_file():
            os.chmod(tmp, path.stat().st_mode & 0o7777)     # the file keeps its mode
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def _both_plugins_warning(version: str) -> str | None:
    """The line saying both the beta and the stable plugin are enabled here, from enabledPlugins
    merged as Claude Code merges it (the engine's notice.enabled_plugins, inline: this file
    imports nothing from govern): user, then project, then local settings, key by key, the later
    file winning; a file that is missing or unreadable is skipped."""
    enabled: dict = {}
    for settings in (HOME / ".claude" / "settings.json", ROOT / ".claude" / "settings.json",
                     SETTINGS_LOCAL):
        try:
            plugins = json.loads(settings.read_text(encoding="utf-8-sig")).get("enabledPlugins", {})
            enabled.update({k: v for k, v in plugins.items() if isinstance(v, bool)})
        except (OSError, ValueError, AttributeError):
            continue
    if enabled.get(BETA_PLUGIN) and enabled.get(STABLE_PLUGIN):
        return (f"context-gate: both the beta and the stable plugin are enabled here, so every "
                f"hook runs twice; govern beta on {version} re-applies the switch")
    return None


def _engine_swapped(raw: bytes, old: str, new: str) -> bytes | None:
    """local.toml's bytes with the one `engine = "old"` line of its [governance] table naming
    new, every other byte as it was; None when there is not exactly one such line. A text edit,
    so only the plain one-line form is understood: anything else is the user's to edit."""
    engine = re.compile(rb"([ \t]*engine[ \t]*=[ \t]*)([\"'])" + re.escape(old.encode())
                        + rb"\2([ \t]*(?:#.*)?\r?)")
    lines = raw.split(b"\n")
    found, inside = [], False
    for i, line in enumerate(lines):
        if re.match(rb"[ \t]*\[", line):
            inside = bool(re.fullmatch(rb"[ \t]*\[[ \t]*governance[ \t]*\][ \t]*(?:#.*)?\r?",
                                       line))
        elif inside and engine.fullmatch(line):
            found.append(i)
    if len(found) != 1:
        return None
    head, quote, tail = engine.fullmatch(lines[found[0]]).groups()
    lines[found[0]] = head + quote + new.encode() + quote + tail
    return b"\n".join(lines)


def beta_key(name: str) -> tuple:
    """A beta's order, as numbers (beta.10 is above beta.9); () for anything that is no beta."""
    m = BETA_RE.fullmatch(name)
    return tuple(int(x) for x in m.groups()) if m else ()


def _installed_betas() -> list[str]:
    """The beta engines installed on this machine, oldest first; [] when the engines directory
    is missing or cannot be listed."""
    try:
        return sorted((p.name for p in ENGINES.iterdir()
                       if beta_key(p.name) and (p / "govern" / "cli.py").is_file()), key=beta_key)
    except OSError:
        return []


def _pinned_release() -> tuple:
    """The release the committed pin runs on this machine, as numbers: the pin itself, or for a
    series pin the newest installed release in the series (its .0 when none is). Dies when
    config.toml gives no pin to compare with."""
    try:
        with (GOV_DIR / "config.toml").open("rb") as fh:
            gov = tomllib.load(fh).get("governance")
    except (OSError, ValueError) as exc:
        die(f"cannot read the engine pin from {GOV_DIR.name}/config.toml ({exc})")
    pin = gov.get("engine") if isinstance(gov, dict) else None
    if not isinstance(pin, str) or not pin:
        die(f"{GOV_DIR.name}/config.toml has no [governance] engine pin")
    if not version_key(pin):
        die(f"{GOV_DIR.name}/config.toml engine pin {pin} is not a release")
    key = version_key(pin)
    if len(key) == 3:
        return key
    found = [version_key(p.name) for p in ENGINES.glob(pin + ".*")
             if len(version_key(p.name)) == 3 and (p / "govern" / "cli.py").is_file()]
    return max(found, default=(key + (0, 0))[:3])


def _source() -> str | None:
    """config.toml's [governance] source; None when it names none or cannot be read."""
    try:
        with (GOV_DIR / "config.toml").open("rb") as fh:
            gov = tomllib.load(fh).get("governance")
    except (OSError, ValueError):
        return None
    source = gov.get("source") if isinstance(gov, dict) else None
    return source if isinstance(source, str) and source else None


def _source_betas(source: str | None) -> tuple[list[str], str | None]:
    """The betas tagged at the source, and None; or [] and why the source could not be asked,
    in git's own words: its first `fatal:` line, which names the cause, else its last line.
    Only `beta on` with no version asks: nothing else looks for a beta there."""
    import subprocess
    if not source:
        return [], f"{GOV_DIR.name}/config.toml names no [governance] source"
    _refuse_option(source)
    try:
        res = subprocess.run(["git", "ls-remote", "--tags", "--refs", "--", source],
                             capture_output=True, text=True, timeout=30,
                             env=_git_env())
    except (OSError, subprocess.SubprocessError) as exc:
        return [], f"{source}: {exc}"
    if res.returncode != 0:
        said = [line.strip() for line in res.stderr.splitlines() if line.strip()]
        fatal = [line for line in said if line.startswith("fatal:")]
        why = fatal[0] if fatal else said[-1] if said else f"git ls-remote exited {res.returncode}"
        return [], f"{source}: {why}"
    return [tag for tag in _tags(res.stdout) if beta_key(tag)], None


def _newest_beta() -> str:
    """The beta `beta on` takes when none is named: the newest of those installed and those
    tagged at the source (the installed ones alone, after one line saying why, when the source
    cannot be asked). Dies when there is none, saying why the source could not be asked when
    it could not, or when it is a beta of a release the committed pin already runs (or of an
    older one): that is a step back, which takes naming the version."""
    installed, source = _installed_betas(), _source()
    there, why = _source_betas(source)
    betas = sorted({*installed, *there}, key=beta_key)
    if not betas and not why:
        die(f"no beta engine is installed, and {source} has no beta tag")
    if not betas:
        die(f"no beta engine is installed, and the source could not be asked for one ({why}) "
            "— from a clone that has the beta's tag, run: "
            "python3 tools/release/install-engine.py vX.Y.Z-beta.N")
    if why:
        note(f"the source could not be asked for a newer beta ({why}); taking the newest "
             "installed")
    newest, runs = betas[-1], _pinned_release()
    if beta_key(newest)[:3] <= runs:
        die(f"the newest {'installed ' if newest in installed else ''}beta, {newest}, is not "
            f"newer than the release this project runs ({'.'.join(str(n) for n in runs)}); to "
            f"run it anyway: govern beta on {newest}")
    return newest


def _plugin_release() -> str | None:
    """The release the local plugin on this machine is stamped with (`vX.Y.Z`, or a beta's);
    None when there is no local plugin, or its stamp cannot be read."""
    try:
        return (PLUGIN_DIR / "govern" / "RELEASE").read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return None


def _missing(version: str) -> str | None:
    """What of beta `version` is not installed on this machine, as the refusal that says how to
    install it by hand; None when its engine and the local plugin both are."""
    if not (ENGINES / version / "govern" / "cli.py").is_file():
        return (f"engine {version} is not installed — from a clone of the repository, run: "
                f"python3 tools/release/install-engine.py v{version}")
    if _plugin_release() != f"v{version}":
        return (f"the local plugin is not {version} — from a clone of the repository, run: "
                f"python3 tools/release/install-plugin.py v{version}")
    return None


def _fetch_beta(version: str, source: str) -> None:
    """Install what of beta `version` is missing here from the source: its engine, as bootstrap
    installs any release, and the local plugin, from the same clone. The plugin is assembled by
    that beta's own engine (its govern.local_plugin), not by this file, so a beta can change its
    plugin's layout without a project upgrading these scripts. It is told which plugin it is here
    for (--name), so a tag whose plugin carries another name replaces nothing. A beta whose
    engine has no such module leaves the plugin as it is. Dies, the project untouched, when a
    step fails."""
    import subprocess

    def plugin(tree: Path) -> None:
        if not (tree / "engine" / "govern" / "local_plugin.py").is_file():
            return
        note(f"installing the local plugin {version} from {source}")
        env = {k: v for k, v in os.environ.items() if k != "GOVERN_ENGINE"}
        env["PYTHONPATH"] = str(tree / "engine")
        # -P: the working directory must not shadow the fetched engine; -B: nothing is written
        # into the clone the plugin is copied from.
        res = subprocess.run([sys.executable, "-P", "-B", "-m", "govern.local_plugin", "--tree",
                              str(tree), "--home", str(HOME), "--name", PLUGIN], env=env,
                             capture_output=True, text=True)
        if res.returncode != 0:
            said = (res.stderr.strip().splitlines() or [f"exit {res.returncode}"])[-1]
            die(f"could not install the local plugin {version} from {source}: "
                f"{said.removeprefix('error: ')}")

    bootstrap(version, source, then=plugin)


def _beta_on(version: str) -> int:
    """Check everything, then switch: local.toml, the git exclude, two settings keys. An engine
    or local plugin that is missing is fetched from the source between the two: after every
    check of the project, git's answers about the exclude file included, none of which writes
    anything, and before anything in the project is written. So a project that is refused
    leaves the machine's engines and local plugin as they were. With another beta on, only
    local.toml's engine value moves: the rest of the file is the user's, and plugins_before
    still says what the first `beta on` found."""
    if not BETA_RE.fullmatch(version):
        die(f"{version} is not a beta (X.Y.Z-beta.N)")
    missing = _missing(version)
    source = _source() if missing else None
    if missing and not source:
        die(missing)
    was, swapped = None, None
    by_hand = (f"{GOV_DIR.name}/local.toml names another beta in a form this command does not "
               f"edit: change it to engine = \"{version}\" under [governance] by hand, then run "
               "this again")
    if LOCAL.is_file():
        try:
            other = _local_engine(strict=True)
        except UnicodeDecodeError as exc:
            die(f"{GOV_DIR.name}/local.toml is unreadable ({exc}): govern beta off first")
        except OSError as exc:
            die(f"{GOV_DIR.name}/local.toml could not be read ({exc}); nothing changed")
        except ValueError:
            other = None
        if isinstance(other, str) and BETA_RE.fullmatch(other) and other != version:
            try:
                was, swapped = other, _engine_swapped(LOCAL.read_bytes(), other, version)
            except OSError as exc:
                die(f"{GOV_DIR.name}/local.toml could not be read ({exc}); nothing changed")
            if swapped is None:
                die(f"{by_hand}; nothing changed")
        elif other != version:
            die(f"{GOV_DIR.name}/local.toml names no beta ({other!r}): govern beta off first")
    if not (ROOT / ".claude").is_dir():
        die(f"{ROOT} has no .claude/ directory to enable the plugin in")
    data = _settings()
    plugins = data.setdefault("enabledPlugins", {})
    before = {k: plugins[k] for k in (STABLE_PLUGIN, BETA_PLUGIN) if k in plugins}
    if not all(isinstance(v, bool) for v in before.values()):
        die(f"{SETTINGS_LOCAL} is not valid settings JSON (an enabledPlugins value for "
            "context-gate is not true or false); fix it, then run this again")
    ignore = _exclude_plan(LOCAL, SETTINGS_LOCAL)
    plugin_was = _plugin_release()
    if missing:
        _fetch_beta(version, source)
        missing = _missing(version)
        if missing:
            die(missing)
    _exclude(ignore)
    saved = _snapshot(LOCAL, SETTINGS_LOCAL)
    try:
        if not LOCAL.is_file():
            recorded = ", ".join(f'"{k}" = {"true" if v else "false"}' for k, v in before.items())
            with LOCAL.open("w", encoding="utf-8", newline="") as fh:
                fh.write(f'[governance]\nengine = "{version}"\n')
                if recorded:
                    fh.write(f"plugins_before = {{ {recorded} }}\n")
        elif swapped is not None:
            _replace(LOCAL, swapped)
            if _local_engine() != version:    # the line found was not the one that is read
                _put_back(saved, by_hand)
        plugins[BETA_PLUGIN] = True
        plugins[STABLE_PLUGIN] = False
        _write_settings(data)
    except OSError as exc:
        _put_back(saved, f"could not switch to beta {version} ({exc})")
    kept = f" (was {was}; the rest of local.toml is kept)" if was else ""
    print(f"beta {version} on for this project, on this machine{kept}:\n"
          f"  {GOV_DIR.name}/local.toml          engine = \"{version}\"\n"
          f"  .claude/settings.local.json       {BETA_PLUGIN} on, {STABLE_PLUGIN} off")
    if plugin_was and plugin_was != f"v{version}":  # one local plugin on a machine: say whose
        other = plugin_was.removeprefix("v")
        print(f"The local plugin was {other}: other projects on this machine still on {other} "
              "now load this plugin, until they run govern beta on themselves.")
    print("Restart the Claude Code session to load the beta plugin.")
    return 0


def _own_settings(raw: bytes) -> list[str] | None:
    """What local.toml holds besides its [governance] table, as the lines to show: its text
    without that table's lines (the header through the line before the next table header, less
    a run of comment lines directly above that header), or all of it when it does not parse or
    when those lines do not parse to what the file holds besides [governance]. [] when nothing but comments is left; None when it is not
    UTF-8 text. Never raises: `beta off` must work whatever the file holds."""
    try:
        lines = [line.rstrip("\r") for line in raw.decode("utf-8-sig").split("\n")]
    except UnicodeDecodeError:
        return None
    try:
        parsed = tomllib.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        pass
    else:
        own = {k: v for k, v in parsed.items() if k != "governance"}
        if not own:
            return []
        kept, inside, held = [], False, []
        for line in lines:
            if re.match(r"[ \t]*\[", line):
                if inside:
                    kept.extend(held)       # the comments directly above the next table are its own
                held = []
                inside = bool(re.fullmatch(r"[ \t]*\[[ \t]*governance[ \t]*\][ \t]*(?:#.*)?", line))
            elif inside:
                held = held + [line] if line.lstrip().startswith("#") else []
            if not inside:
                kept.append(line)
        # The line filter is only trusted when what it keeps parses to what the file holds
        # besides [governance] (less its sub-tables, which are shown): a header look-alike in a
        # string or an array fools it.
        try:
            shown = tomllib.loads("\n".join(kept))
        except (ValueError, RecursionError):
            shown = None
        sub = shown.pop("governance", {}) if shown is not None else {}   # [governance.x] tables
        gov = parsed.get("governance")
        same = shown == own and isinstance(gov, dict) and all(
            k in gov and gov[k] == v for k, v in sub.items())
        if same:
            lines = kept
    while lines and not lines[-1].strip():
        lines.pop()
    while lines and not lines[0].strip():
        lines.pop(0)
    return lines


def _beta_off() -> int:
    """Remove local.toml and put the two plugin keys back as beta on found them (a key it did
    not find is deleted); needs no engine, and is idempotent. Whatever else the user kept in
    local.toml goes with it, so it is printed for them to copy."""
    if not LOCAL.is_file():
        print("no beta is on in this project")
        return 0
    data = _settings()
    plugins = data.get("enabledPlugins", {})
    before = _local_governance().get("plugins_before")
    before = before if isinstance(before, dict) else {}
    version = _local_engine()
    saved = _snapshot(LOCAL, SETTINGS_LOCAL)
    own = _own_settings(saved[LOCAL] or b"")
    changed = []
    try:
        for k in (BETA_PLUGIN, STABLE_PLUGIN):
            if k in before and isinstance(before[k], bool):
                if plugins.get(k) != before[k]:
                    plugins[k] = before[k]
                    changed.append(k)
            elif k in plugins:
                del plugins[k]
                changed.append(k)
        if "enabledPlugins" in data and not plugins:
            del data["enabledPlugins"]
        if changed:
            _write_settings(data)
        LOCAL.unlink()
    except OSError as exc:
        _put_back(saved, f"could not switch the beta off ({exc})")
    label = f"beta {version}" if isinstance(version, str) else "the beta"
    print(f"{label} off for this project, on this machine:")
    print(f"  {GOV_DIR.name}/local.toml          removed")
    if changed:
        print(f"  .claude/settings.local.json       {' and '.join(changed)} put back")
    print("Back on the committed pin. Anything the beta installed outside this project (see its "
          "release\nnotes) is still installed. Restart the Claude Code session to load the stable "
          "plugin.")
    if own is None:
        print("local.toml was not UTF-8 text, so what it held cannot be shown.")
    elif own:
        shown = "\n".join(f"  {line}" if line else "" for line in own)
        codec = sys.stdout.encoding or "utf-8"    # a console that cannot print it must not fail
        print("local.toml held settings of your own, now removed. Copy what you want to keep into "
              f"{GOV_DIR.name}/config.toml:\n"
              + shown.encode(codec, "backslashreplace").decode(codec, "replace"))
    return 0


def _beta_status() -> int:
    if not LOCAL.is_file():
        print("no beta in this project")
        return 0
    version = _local_engine()
    if not isinstance(version, str) or not BETA_RE.fullmatch(version):
        print(f"{GOV_DIR.name}/local.toml names no beta ({version!r}); govern beta off clears it")
        return 0
    try:
        plugin = (PLUGIN_DIR / "govern" / "RELEASE").read_text(encoding="utf-8").strip()
        plugin = "installed" if plugin == f"v{version}" else f"is {plugin.removeprefix('v')}"
    except OSError:
        plugin = "missing"
    try:
        plugins = _load_settings().get("enabledPlugins", {})
        flags = (f"beta {'on' if plugins.get(BETA_PLUGIN) is True else 'off'}, "
                 f"stable {'off' if plugins.get(STABLE_PLUGIN) is False else 'on'}")
    except (OSError, ValueError) as exc:
        flags = f"unreadable ({exc})"
    engine = "installed" if (ENGINES / version / "govern" / "cli.py").is_file() else "missing"
    print(f"beta {version}\n  engine: {engine}\n  plugin: {plugin}\n"
          f"  settings.local.json: {flags}")
    both = _both_plugins_warning(version)
    if both:
        print(both)
    newer = [b for b in _installed_betas() if beta_key(b) > beta_key(version)]
    if newer:   # notice.for_project's line, inline: this file imports nothing from govern
        print(f"context-gate: beta {newer[-1]} is installed (this project runs beta {version}): "
              f"govern beta on {newer[-1]}")
    return 0


def beta(argv: list[str]) -> int:
    if not argv:
        return _beta_status()
    if argv == ["off"]:
        return _beta_off()
    if argv == ["on"]:
        return _beta_on(_newest_beta())
    if len(argv) == 2 and argv[0] == "on":
        return _beta_on(argv[1].removeprefix("v"))
    die("usage: govern beta [on [X.Y.Z-beta.N] | off]")


if Path(__file__).name == "upgrade":
    raise SystemExit(upgrade(sys.argv[1:]))

if Path(__file__).name not in ("upgrade", "uninstall") and sys.argv[1:2] == ["beta"]:
    raise SystemExit(beta(sys.argv[2:]))

sys.path.insert(0, str(engine_path()))

if Path(__file__).name == "uninstall":
    from govern.installer import main as installer  # noqa: E402
    raise SystemExit(installer(["uninstall", "--root", str(ROOT), *sys.argv[1:]]))

from govern.cli import main  # noqa: E402

# A stub at another gate path (install --entrypoint) passes its own name, so usage and messages
# name the command the caller ran.
raise SystemExit(main(root=ROOT, prog=globals().get("PROG")))
