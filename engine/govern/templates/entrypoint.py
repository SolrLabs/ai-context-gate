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
$GOVERN_ENGINE overrides all of this with an explicit engine directory, for engine development.
"""
import os
import sys
import tomllib
from pathlib import Path

GOV_DIR = Path(__file__).resolve().parent.parent
ROOT = GOV_DIR.parent
HOME = Path(os.environ.get("HOME") or Path.home())
ENGINES = HOME / ".local" / "share" / "context-gate" / "engines"


def die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(2)


def version_key(name: str) -> tuple:
    parts = name.split(".")
    return tuple(int(x) for x in parts) if all(x.isdigit() for x in parts) else ()


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


if Path(__file__).name == "upgrade":
    raise SystemExit(upgrade(sys.argv[1:]))

sys.path.insert(0, str(engine_path()))

if Path(__file__).name == "uninstall":
    from govern.installer import main as installer  # noqa: E402
    raise SystemExit(installer(["uninstall", "--root", str(ROOT), *sys.argv[1:]]))

from govern.cli import main  # noqa: E402

# A stub at another gate path (install --entrypoint) passes its own name, so usage and messages
# name the command the caller ran.
raise SystemExit(main(root=ROOT, prog=globals().get("PROG")))
