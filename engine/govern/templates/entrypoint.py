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

Which engine runs: exactly the version config.toml pins, from
~/.local/share/context-gate/engines/<version>/. If it is not installed and config.toml
names a [governance] source (a git URL or path holding the engine, tagged v<version>), it is
installed from there first, so a fresh clone or a CI runner bootstraps itself. A pin of only a
series ("0.4") runs the newest installed release in that series.
.context-gate/local.toml, when present and naming an installed beta, runs that beta instead, on
this machine only (`govern beta`); it is never committed.
$GOVERN_ENGINE overrides all of this with an explicit engine directory, for engine development.

    python3 .context-gate/bin/govern beta [on X.Y.Z-beta.N | off]

switches this project, on this machine, to a beta engine and the local beta plugin, and back. It
is handled here, before any engine loads, so it works when the beta is broken or gone.
"""
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
STABLE_PLUGIN = "context-gate@context-gate"
BETA_PLUGIN = "context-gate@skills-dir"
PLUGIN_DIR = HOME / ".claude" / "skills" / "context-gate"
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


def bootstrap(version: str, source: str) -> Path:
    """Install one engine release from its source: the tag's engine/govern, read-only."""
    import shutil
    import subprocess
    import tempfile
    dest = ENGINES / version
    print(f"context-gate: installing engine {version} from {source}", file=sys.stderr)
    with tempfile.TemporaryDirectory() as tmp:
        res = subprocess.run(["git", "clone", "--quiet", "--depth", "1", "--branch",
                              f"v{version}", source, tmp], capture_output=True, text=True)
        if res.returncode != 0:
            die(f"could not fetch engine v{version} from {source}: {res.stderr.strip()}")
        engine = Path(tmp) / "engine" / "govern"
        if f'__version__ = "{version}"' not in (engine / "__init__.py").read_text(encoding="utf-8"):
            die(f"{source} tag v{version} does not carry engine {version}")
        dest.mkdir(parents=True)
        shutil.copytree(engine, dest / "govern", ignore=shutil.ignore_patterns("__pycache__"))
    for path in sorted(dest.rglob("*"), reverse=True) + [dest]:
        path.chmod(path.stat().st_mode & ~0o222)
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
    if not isinstance(pin, str) or not version_key(pin):
        die(f"{GOV_DIR.name}/config.toml has no [governance] engine pin")
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
            res = subprocess.run(["git", "ls-remote", "--tags", "--refs", source],
                                 capture_output=True, text=True, timeout=30,
                                 env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
            released = [line.rsplit("/v", 1)[1] for line in res.stdout.splitlines()
                        if "refs/tags/v" in line and version_key(line.rsplit("/v", 1)[1])]
        candidates = installed + released
        if not candidates:
            die("no engine release found: none installed, and no [governance] source to ask")
        target = max(candidates, key=version_key)
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


def _exclude(*paths: Path) -> None:
    """Make git ignore each path, through info/exclude, never a committed .gitignore. Dies,
    writing nothing, when git cannot answer: a beta pin must never reach a commit unnoticed."""
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
        return
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


def _beta_on(version: str) -> int:
    """Check everything, then switch: local.toml, the git exclude, two settings keys."""
    if not BETA_RE.fullmatch(version):
        die(f"{version} is not a beta (X.Y.Z-beta.N)")
    if not (ENGINES / version / "govern" / "cli.py").is_file():
        die(f"engine {version} is not installed — from a clone of the repository, run: "
            f"python3 tools/release/install-engine.py v{version}")
    try:
        plugin = (PLUGIN_DIR / "govern" / "RELEASE").read_text(encoding="utf-8").strip()
    except OSError:
        plugin = None
    if plugin != f"v{version}":
        die(f"the local plugin is not {version} — from a clone of the repository, run: "
            f"python3 tools/release/install-plugin.py v{version}")
    if LOCAL.is_file():
        try:
            other = _local_engine(strict=True)
        except UnicodeDecodeError as exc:
            die(f"{GOV_DIR.name}/local.toml is unreadable ({exc}): govern beta off first")
        except (OSError, ValueError):
            other = None
        if isinstance(other, str) and BETA_RE.fullmatch(other) and other != version:
            die(f"beta {other} is on here: govern beta off first")
        if other != version:
            die(f"{GOV_DIR.name}/local.toml names no beta ({other!r}): govern beta off first")
    if not (ROOT / ".claude").is_dir():
        die(f"{ROOT} has no .claude/ directory to enable the plugin in")
    data = _settings()
    plugins = data.setdefault("enabledPlugins", {})
    before = {k: plugins[k] for k in (STABLE_PLUGIN, BETA_PLUGIN) if k in plugins}
    if not all(isinstance(v, bool) for v in before.values()):
        die(f"{SETTINGS_LOCAL} is not valid settings JSON (an enabledPlugins value for "
            "context-gate is not true or false); fix it, then run this again")
    _exclude(LOCAL, SETTINGS_LOCAL)
    saved = _snapshot(LOCAL, SETTINGS_LOCAL)
    try:
        if not LOCAL.is_file():
            recorded = ", ".join(f'"{k}" = {"true" if v else "false"}' for k, v in before.items())
            with LOCAL.open("w", encoding="utf-8", newline="") as fh:
                fh.write(f'[governance]\nengine = "{version}"\n')
                if recorded:
                    fh.write(f"plugins_before = {{ {recorded} }}\n")
        plugins[BETA_PLUGIN] = True
        plugins[STABLE_PLUGIN] = False
        _write_settings(data)
    except OSError as exc:
        _restore(saved)
        die(f"could not switch to beta {version} ({exc}); nothing changed")
    print(f"beta {version} on for this project, on this machine:\n"
          f"  {GOV_DIR.name}/local.toml          engine = \"{version}\"\n"
          f"  .claude/settings.local.json       {BETA_PLUGIN} on, {STABLE_PLUGIN} off\n"
          "Restart the Claude Code session to load the beta plugin.")
    return 0


def _beta_off() -> int:
    """Remove local.toml and put the two plugin keys back as beta on found them (a key it did
    not find is deleted); needs no engine, and is idempotent."""
    if not LOCAL.is_file():
        print("no beta is on in this project")
        return 0
    data = _settings()
    plugins = data.get("enabledPlugins", {})
    before = _local_governance().get("plugins_before")
    before = before if isinstance(before, dict) else {}
    version = _local_engine()
    saved = _snapshot(LOCAL, SETTINGS_LOCAL)
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
        _restore(saved)
        die(f"could not switch the beta off ({exc}); nothing changed")
    label = f"beta {version}" if isinstance(version, str) else "the beta"
    print(f"{label} off for this project, on this machine:")
    print(f"  {GOV_DIR.name}/local.toml          removed")
    if changed:
        print(f"  .claude/settings.local.json       {' and '.join(changed)} put back")
    print("Back on the committed pin. Anything the beta installed outside this project (see its "
          "release\nnotes) is still installed. Restart the Claude Code session to load the stable "
          "plugin.")
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
    return 0


def beta(argv: list[str]) -> int:
    if not argv:
        return _beta_status()
    if argv == ["off"]:
        return _beta_off()
    if len(argv) == 2 and argv[0] == "on":
        return _beta_on(argv[1].removeprefix("v"))
    die("usage: govern beta [on X.Y.Z-beta.N | off]")


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
