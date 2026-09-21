"""Install context-gate into a project, upgrade it, and uninstall it.

    python3 -m govern.installer install --root DIR --config FILE
            [--entrypoint PATH]... [--migrate-baseline PATH | --baseline FILE] [--retire PATH]...
            [--no-report]
    python3 -m govern.installer upgrade --root DIR          # run with the engine to upgrade to
    python3 -m govern.installer migrate --root DIR [--apply]   # convert logs and traps in another format to the standard
    python3 -m govern.installer measure --root DIR [--json]    # read a project's layout (read-only)
    python3 -m govern.installer adopt --root DIR [--source S|none] [--marketplace OWNER/REPO]
            [--profile P] [--answers FILE] [--apply] [--json]
                                    # propose a config from measure; --apply installs it green
    python3 -m govern.installer enable-plugin --root DIR --id context-gate@context-gate
            [--marketplace OWNER/REPO]
    python3 -m govern.installer uninstall --root DIR [--force]
    python3 .context-gate/bin/uninstall [--force]    # the same, from inside a project

Everything the tool owns goes into `.context-gate/` at the root. What it has to touch
outside that directory is recorded in `.context-gate/installed.toml`, and uninstall
reverses exactly that before deleting the directory:

- `--entrypoint PATH`: a gate path the project's skills and scripts already call keeps working.
  The file there is backed up and replaced by a stub that runs `.context-gate/bin/govern`.
- `--migrate-baseline PATH`: an existing ratchet baseline moves into the directory.
- `--baseline FILE`: start from this baseline (a file anywhere, a project's carried one, say):
  checked, then copied in. Nothing outside the directory changes, so nothing is recorded, and
  uninstall removes it with the directory.
- `--retire PATH`: a file the new gate makes obsolete (a previous gate's tests, say) is backed
  up and removed.
- `--enable-plugin ID`: the project opts in to the Claude Code plugin (e.g.
  `context-gate@context-gate`) through `enabledPlugins` in `.claude/settings.json`, the
  line a teammate's Claude Code reads. For a marketplace plugin, the marketplace goes in
  `extraKnownMarketplaces` beside it (the public repository, or `--marketplace OWNER/REPO` on
  `adopt` and `enable-plugin`); `context-gate@skills-dir`, how development loads the plugin
  from ~/.claude/skills/, writes no marketplace. Uninstall restores each setting as it was.
  `enable-plugin` does the same for an existing install.

Install writes `install-report.md`: a previous gate's findings (run from an `--entrypoint` before it
is replaced) against the new gate's, grouped by the check that raised each new one, plus the
settings that differ from the engine's defaults. Upgrade writes `upgrade-report.md` the same way,
old engine against new, after pinning the project to the new engine and refreshing the tool's
own files.

Install also baselines: every current ratchet breach not already in the baseline (moved in by
`--migrate-baseline`, or empty otherwise) is recorded, so the project starts green — never
raising an entry already there. The keys added are listed in `install-report.md`.
`--no-report` still baselines; it only skips the report.

The project's own records (docs, decision logs, traps) are never moved, rewritten or removed.
Install is refused over an existing install and rolled back if the policy does not load.
Uninstall finds every conflict before changing anything, and changes nothing if there is one.
"""
from __future__ import annotations

import argparse
import codecs
import contextlib
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import tomllib
from datetime import date
from pathlib import Path

import govern.checks  # noqa: F401  (registers the built-in checks the config refers to)
from govern import __version__, config, layout, migrate, notice, ratchet, registry, releases, report
from govern.context import Context

TEMPLATES = Path(__file__).resolve().parent / "templates"
TOOL_FILES = ("bin/govern", "bin/upgrade", "bin/uninstall")
SOURCE_NONE = "none"      # `adopt --source none`: no [governance] source

README = """# {gov_dir}

Installed by {name} {version} on {date}. Everything the tool owns in this project is
in this directory:

| File | What |
|---|---|
| `config.toml` | This project's governance policy: limits, vocabularies, layout, which checks run |
| `baseline.json` | Recorded size breaches (the ratchet) |
| `bin/govern` | The gate: `python3 {gov_dir}/bin/govern check`; `explain` shows every setting and where it came from |
| `bin/upgrade` | Upgrades to the newest release (or `--to X.Y.Z`), installing it from `[governance] source` if needed, and writes `upgrade-report.md` |
| `bin/uninstall` | Removes the tool: reverses what `installed.toml` lists, then deletes this directory |
| `installed.toml` | What the install touched outside this directory |
| `install-report.md` | What the gate found at install (compared with a previous gate script, if one was named) |
| `migration-report.md` | What `migrate` changed (or would change), and the findings before and after |
| `backup/` | Originals of anything the install replaced or retired, restored by uninstall |

`bin/govern`, `bin/upgrade` and `bin/uninstall` are one script that acts on its own name.
The project's own records (decision logs, traps, handoffs) are not in here and
are never removed by the tool.
"""

STUB = '''#!/usr/bin/env python3
"""Installed by {name}: runs {gov_dir}/bin/govern, so everything that
calls this path keeps working. Uninstall restores whatever was here before."""
import runpy
from pathlib import Path

runpy.run_path(str(Path(__file__).resolve().parent / {rel!r}), run_name="__main__",
               init_globals={{"PROG": Path(__file__).name}})
'''


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def toml_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_manifest(path: Path, data: dict) -> None:
    lines = [f"# What {layout.DISPLAY_NAME} touched outside {layout.GOV_DIR}/. Read by uninstall.",
             f"engine = {toml_str(data['engine'])}", f"installed = {toml_str(data['installed'])}"]
    for stub in data["entrypoint"]:
        lines += ["", "[[entrypoint]]", f"path = {toml_str(stub['path'])}",
                  f"sha256 = {toml_str(stub['sha256'])}", f"backup = {toml_str(stub['backup'])}"]
    for move in data["moved"]:
        lines += ["", "[[moved]]", f"from = {toml_str(move['from'])}", f"to = {toml_str(move['to'])}"]
    for item in data["retired"]:
        lines += ["", "[[retired]]", f"path = {toml_str(item['path'])}",
                  f"backup = {toml_str(item['backup'])}"]
    for item in data["plugin"]:
        lines += ["", "[[plugin]]", f"settings = {toml_str(item['settings'])}",
                  f"id = {toml_str(item['id'])}", f"previous = {toml_str(item['previous'])}"]
    for item in data["marketplace"]:
        lines += ["", "[[marketplace]]", f"settings = {toml_str(item['settings'])}",
                  f"name = {toml_str(item['name'])}", f"repo = {toml_str(item['repo'])}",
                  f"previous = {toml_str(item['previous'])}"]
    for item in data["settings"]:
        lines += ["", "[[settings]]", f"path = {toml_str(item['path'])}",
                  f"backup = {toml_str(item['backup'])}",
                  f"created_dir = {'true' if item['created_dir'] else 'false'}",
                  f"sha256 = {toml_str(item['sha256'])}"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_manifest(root: Path) -> dict:
    with (root / layout.MANIFEST).open("rb") as fh:
        data = tomllib.load(fh)
    for key in ("entrypoint", "moved", "retired", "plugin", "marketplace", "settings"):
        data.setdefault(key, [])
    return data


def fail(msg: str, code: int = 2) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return code


def _inside(rel: str) -> bool:
    return not (Path(rel).is_absolute() or ".." in Path(rel).parts)


def _load_context(root: Path) -> Context:
    cfg = config.load(root, layout.home())
    return Context(root=root, home=layout.home(), cfg=cfg, registry=registry.load(cfg),
                   prog="govern")


def _new_findings(ctx: Context):
    from govern import cli
    return cli.collect(ctx, ctx.registry.scopes, workspace=True)


def _write_tool_files(gov: Path) -> None:
    (gov / "bin").mkdir(parents=True, exist_ok=True)
    for rel in TOOL_FILES:
        dest = gov / rel
        if dest.exists():
            dest.chmod(0o755)
        shutil.copyfile(TEMPLATES / "entrypoint.py", dest)
        dest.chmod(0o755)


def _gate_env() -> dict:
    """The environment for running an installed gate: no development engine override."""
    return {k: v for k, v in os.environ.items() if k not in ("GOVERN_ENGINE", "PYTHONPATH")}


# ---------------------------------------------------------------------------- plugin opt-in

SETTINGS = ".claude/settings.json"


def _read_settings(root: Path) -> dict:
    path = root / SETTINGS
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"{SETTINGS} is not a JSON object")
    return data


def _write_settings(root: Path, data: dict) -> None:
    """Write the settings as JSON, keeping an existing file's BOM and CRLF line endings."""
    path = root / SETTINGS
    bom, newline = "", "\n"
    if path.exists():
        raw = path.read_bytes()
        bom = "﻿" if raw.startswith(codecs.BOM_UTF8) else ""
        newline = "\r\n" if b"\r\n" in raw else "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline=newline) as fh:
        fh.write(bom + json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def _settings_record(root: Path) -> dict:
    """Before the first write to the settings: back up the file's bytes (`backup` is "" when
    there was no file, and `created_dir` says whether `.claude/` was missing too). `sha256` is
    filled in once the tool has written the file: uninstall restores the original bytes only
    while the file is still exactly what the tool wrote."""
    path = root / SETTINGS
    rec = {"path": SETTINGS, "backup": "", "created_dir": not path.parent.exists(), "sha256": ""}
    if path.exists():
        rec["backup"] = f"{layout.BACKUP}/{SETTINGS}"
        (root / rec["backup"]).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, root / rec["backup"])
    return rec


def _restore_settings_bytes(root: Path, rec: dict) -> None:
    path = root / rec["path"]
    if rec["backup"]:
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / rec["backup"], path)
        return
    path.unlink(missing_ok=True)
    _remove_created_dir(root, rec)


def _remove_created_dir(root: Path, rec: dict) -> None:
    if rec["created_dir"]:
        with contextlib.suppress(OSError):
            (root / rec["path"]).parent.rmdir()        # only if the tool made it and it is empty


def _settings_edited(root: Path, rec: dict) -> str | None:
    """Why the settings are no longer exactly what the tool wrote, or None when they are."""
    path = root / rec["path"]
    if not path.exists():
        return f"{rec['path']} was removed after install" if rec["backup"] else None
    if sha(path) != rec["sha256"]:
        return f"{rec['path']} was edited after install"
    return None


REPO_RE = re.compile(r"^(?!\.+/)[A-Za-z0-9_.-]+/(?!\.+$)[A-Za-z0-9_.-]+$")
SAME = "same"   # a marketplace entry the project already held as written: left alone, both ways


def marketplace_entry(repo: str) -> dict:
    return {"source": {"source": "github", "repo": repo}}


def marketplace_for(plugin_id: str, repo: str | None) -> tuple[str, str] | None:
    """The marketplace an opt-in to `plugin_id` registers, as (name, owner/repo): `repo` when
    given (a fork), the public repository for the public plugin id, and none otherwise (a
    plugin under ~/.claude/skills/, which is how development runs it). ValueError when
    `plugin_id` or `repo` cannot mean a marketplace."""
    name = plugin_id.partition("@")[2]
    if not name:
        raise ValueError(f"plugin id '{plugin_id}' is not name@marketplace")
    if repo is None:
        return (name, layout.PUBLIC_REPO) if plugin_id == layout.PLUGIN_ID else None
    if not REPO_RE.match(repo):
        raise ValueError(f"marketplace '{repo}' is not a GitHub owner/repo")
    if plugin_id == layout.SKILLS_DIR_PLUGIN_ID:
        raise ValueError(f"{plugin_id} is loaded from ~/.claude/skills/, not a marketplace")
    return name, repo


def _opt_in(root: Path, plugin_id: str | None, market: tuple[str, str] | None
            ) -> tuple[dict | None, dict | None]:
    """In one write, register the marketplace (extraKnownMarketplaces[name]) and set
    enabledPlugins[plugin_id] = true, either one skipped when None. Returns the records
    uninstall needs to undo each key when the file has been edited since: the previous value
    is "absent", the JSON it held, or, for a marketplace entry already exactly as adopt would
    write it, SAME."""
    data = _read_settings(root)
    plugin_rec = market_rec = None
    if market is not None:
        name, repo = market
        known = data.setdefault("extraKnownMarketplaces", {})
        if not isinstance(known, dict):
            raise ValueError("extraKnownMarketplaces is not a JSON object")
        entry = marketplace_entry(repo)
        previous = ("absent" if name not in known
                    else SAME if known[name] == entry else json.dumps(known[name]))
        known[name] = entry
        market_rec = {"settings": SETTINGS, "name": name, "repo": repo, "previous": previous}
    if plugin_id is not None:
        enabled = data.setdefault("enabledPlugins", {})
        if not isinstance(enabled, dict):
            raise ValueError("enabledPlugins is not a JSON object")
        previous = "absent" if plugin_id not in enabled else json.dumps(enabled[plugin_id])
        enabled[plugin_id] = True
        plugin_rec = {"settings": SETTINGS, "id": plugin_id, "previous": previous}
    _write_settings(root, data)
    return plugin_rec, market_rec


def _restore_plugin(root: Path, item: dict) -> None:
    data = _read_settings(root)
    enabled = data.get("enabledPlugins", {})
    if item["previous"] == "absent":
        enabled.pop(item["id"], None)
        if not enabled:
            data.pop("enabledPlugins", None)
    else:
        enabled[item["id"]] = json.loads(item["previous"])
    _write_settings(root, data)


def _restore_marketplace(root: Path, item: dict) -> None:
    if item["previous"] == SAME:
        return
    data = _read_settings(root)
    known = data.get("extraKnownMarketplaces", {})
    if item["previous"] == "absent":
        known.pop(item["name"], None)
        if not known:
            data.pop("extraKnownMarketplaces", None)
    else:
        known[item["name"]] = json.loads(item["previous"])
    _write_settings(root, data)


def _restore_keys(root: Path, record: dict, say=lambda msg: None) -> None:
    """Undo each recorded key in place, keeping edits made since; then, if there was no file
    before and nothing is left in it, remove it (and `.claude/`, if the tool made it)."""
    for item in record["plugin"]:
        try:
            _restore_plugin(root, item)
            say(f"  plugin       {item['id']} restored in {item['settings']}")
        except (OSError, ValueError):
            say(f"  plugin       {item['id']} left in {item['settings']} (unreadable)")
    for item in record["marketplace"]:
        try:
            _restore_marketplace(root, item)
            say(f"  marketplace  {item['name']} restored in {item['settings']}")
        except (OSError, ValueError):
            say(f"  marketplace  {item['name']} left in {item['settings']} (unreadable)")
    for rec in record["settings"]:
        with contextlib.suppress(OSError, ValueError):
            if not rec["backup"] and (root / rec["path"]).exists() and _read_settings(root) == {}:
                (root / rec["path"]).unlink()
                _remove_created_dir(root, rec)


def _restore_settings(root: Path, record: dict, say=lambda msg: None) -> None:
    """The settings as they were: the original bytes while the file is still exactly what the
    tool wrote (or, when the install record holds no bytes, key by key)."""
    rec = record["settings"][0] if record["settings"] else None
    if rec is not None and _settings_edited(root, rec) is None:
        _restore_settings_bytes(root, rec)
        say(f"  restored     {rec['path']}" if rec["backup"] else f"  removed      {rec['path']}")
        return
    if rec is not None and not (root / rec["path"]).exists():
        _restore_settings_bytes(root, rec)             # removed since: the original returns
        say(f"  restored     {rec['path']}")
        return
    _restore_keys(root, record, say)


def enable_plugin(root: Path, plugin_id: str, repo: str | None = None) -> int:
    if not (root / layout.MANIFEST).is_file():
        return fail(f"no {layout.MANIFEST} — install first")
    try:
        market = marketplace_for(plugin_id, repo)
    except ValueError as exc:
        return fail(f"{exc}; nothing was changed")
    record = read_manifest(root)
    plugin_id_to_set = None if any(p["id"] == plugin_id for p in record["plugin"]) else plugin_id
    if market is not None:
        known = [m for m in record["marketplace"] if m["name"] == market[0]]
        if known:
            had = known[0].get("repo")
            if repo is not None and had is not None and had != repo:
                return fail(f"marketplace {market[0]} is already recorded as {had}; uninstall "
                            f"or edit it; nothing was changed")
            market = None
    if plugin_id_to_set is None and market is None:
        print(f"{plugin_id} is already enabled by the install")
        return 0
    path = root / SETTINGS
    new_rec = not record["settings"]
    rec = _settings_record(root) if new_rec else record["settings"][0]
    before = sha(path) if path.exists() else ""
    try:
        plugin_rec, market_rec = _opt_in(root, plugin_id_to_set, market)
    except ValueError as exc:
        if new_rec and rec["backup"]:
            backup = root / rec["backup"]
            backup.unlink(missing_ok=True)
            for folder in (backup.parent, root / layout.BACKUP):
                with contextlib.suppress(OSError):
                    folder.rmdir()                      # only the empty ones it made
        return fail(f"{SETTINGS} could not be read ({exc}); nothing was changed")
    if new_rec or before == rec["sha256"]:
        # Still exactly what the tool wrote: uninstall can restore the original bytes. Edited
        # since, the recorded sha stays, so uninstall reports the edit and restores key by key.
        rec["sha256"] = sha(path)
    if new_rec:
        record["settings"].append(rec)
    if plugin_rec:
        record["plugin"].append(plugin_rec)
    if market_rec:
        record["marketplace"].append(market_rec)
    write_manifest(root / layout.MANIFEST, record)
    if market_rec:
        print(f"  marketplace  {market_rec['name']} ({market[1]}) known in {SETTINGS}")
    print(f"  plugin       {plugin_id} enabled in {SETTINGS} (commit it so teammates get it too)")
    return 0


class EngineNotInstalled(Exception):
    pass


def install_running_engine(home: Path | None = None, engine: Path | None = None) -> str | None:
    """Install the engine running now (or `engine`, a `govern` package) as its version in the
    user's engines directory, read-only like a release `bin/govern` fetches, so a project
    adopted with an engine nothing else installed (the plugin's bundled copy) runs its gate
    with no fetch. Only a released copy is installed: one carrying `govern/RELEASE` (stamped
    by the release tools, never in the repository) that reads `v<version>`; a development
    tree would otherwise shadow the real release of its version for good. A version already
    there is never overwritten. Returns the line to report, or None when the version is
    installed already. EngineNotInstalled when the copy fails."""
    from govern import profile
    src = engine or Path(__file__).resolve().parent
    try:
        stamp = (src / layout.RELEASE_MARKER).read_text(encoding="utf-8")
    except OSError:
        stamp = None
    if stamp != f"v{__version__}\n":
        return f"engine {__version__} is a development tree; not installed"
    engines = layout.engines_dir(home)
    dest = engines / __version__
    if (dest / "govern" / "cli.py").is_file():
        return None
    if dest.exists():
        return (f"engine {__version__} in {dest} is broken (no govern/cli.py); not replaced: "
                f"remove it to let the gate reinstall it")
    try:
        engines.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix=f".{__version__}-", dir=engines))
    except OSError as exc:
        raise EngineNotInstalled(str(exc)) from exc
    try:
        shutil.copytree(src, tmp / "govern", ignore=shutil.ignore_patterns("__pycache__"))
        for path in sorted(tmp.rglob("*"), reverse=True):     # read-only before it appears
            path.chmod(path.stat().st_mode & ~0o222)
        for attempt in (1, 2):
            try:
                tmp.rename(dest)
                dest.chmod(dest.stat().st_mode & ~0o222)
                break
            except OSError:
                if (dest / "govern" / "cli.py").is_file():
                    profile.rmtree(tmp)
                    return None                     # another process installed it first
                if attempt == 2:
                    raise
    except OSError as exc:
        profile.rmtree(tmp)
        raise EngineNotInstalled(str(exc)) from exc
    return f"{__version__} installed in {dest}"


# ---------------------------------------------------------------------------- install

def install(root: Path, config_file: Path, entrypoints: list[str], migrate_baseline: str | None,
            retire: list[str], with_report: bool = True, plugin_id: str | None = None,
            marketplace: str | None = None, baseline: Path | None = None) -> int:
    """`marketplace` is the owner/repo `plugin_id`'s marketplace is in, when it is not the
    one `marketplace_for` defaults to. `baseline` is a baseline file to start from, copied in."""
    gov = root / layout.GOV_DIR
    if gov.exists():
        return fail(f"{gov} already exists — uninstall first, or edit its config.toml")
    if not config_file.is_file():
        return fail(f"no config at {config_file}")
    for rel in entrypoints + retire + ([migrate_baseline] if migrate_baseline else []):
        if not _inside(rel):
            return fail(f"'{rel}' must be a path inside the project")
    if migrate_baseline and not (root / migrate_baseline).is_file():
        return fail(f"no baseline at {migrate_baseline} to migrate")
    if baseline is not None:
        if migrate_baseline:
            return fail("--baseline and --migrate-baseline both name a baseline; give one")
        try:
            ratchet.parse_baseline(baseline.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
            return fail(f"{baseline} is not a valid baseline ({exc}); nothing was changed")
    for rel in retire:
        if not (root / rel).is_file():
            return fail(f"no file at {rel} to retire")
    try:
        market = marketplace_for(plugin_id, marketplace) if plugin_id else None
    except ValueError as exc:
        return fail(str(exc))

    # The previous gate, before anything changes: the first entry point that exists is the gate
    # the project has been running.
    old = None
    if with_report:
        existing = [ep for ep in entrypoints if (root / ep).is_file()]
        if existing:
            old = report.run_old_gate(root, [sys.executable, existing[0], "check"], _gate_env())

    record = {"engine": __version__, "installed": date.today().isoformat(),
              "entrypoint": [], "moved": [], "retired": [], "plugin": [], "marketplace": [],
              "settings": []}
    try:
        _write_tool_files(gov)
        shutil.copyfile(config_file, root / layout.CONFIG)
        (gov / "README.md").write_text(
            README.format(name=layout.DISPLAY_NAME, gov_dir=layout.GOV_DIR,
                          version=__version__, date=record["installed"]), encoding="utf-8")

        if migrate_baseline:
            shutil.move(str(root / migrate_baseline), str(root / layout.BASELINE))
            record["moved"].append({"from": migrate_baseline, "to": layout.BASELINE})
        elif baseline is not None:
            shutil.copyfile(baseline, root / layout.BASELINE)      # inside: nothing to record

        for rel in retire:
            backup = f"{layout.BACKUP}/{rel}"
            (root / backup).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(root / rel), str(root / backup))
            record["retired"].append({"path": rel, "backup": backup})

        for rel in entrypoints:
            path = root / rel
            backup = ""
            if path.exists():
                backup = f"{layout.BACKUP}/{rel}"
                (root / backup).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, root / backup)
            path.parent.mkdir(parents=True, exist_ok=True)
            target = os.path.relpath(root / layout.ENTRYPOINT, path.parent).replace(os.sep, "/")
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(STUB.format(name=layout.DISPLAY_NAME, gov_dir=layout.GOV_DIR, rel=target))
            path.chmod(0o755)
            record["entrypoint"].append({"path": rel, "sha256": sha(path), "backup": backup})

        if plugin_id:
            record["settings"].append(_settings_record(root))
            plugin_rec, market_rec = _opt_in(root, plugin_id, market)
            record["settings"][0]["sha256"] = sha(root / SETTINGS)
            record["plugin"].append(plugin_rec)
            if market_rec:
                record["marketplace"].append(market_rec)

        write_manifest(root / layout.MANIFEST, record)
        ctx = _load_context(root)   # the install stands only if the policy loads here
    except (config.ConfigError, registry.RegistryMissing, OSError, ValueError) as exc:
        _rollback(root, record)
        return fail(f"install rolled back: {exc}")

    added = _baseline_new(ctx)

    print(f"installed {layout.DISPLAY_NAME} {__version__} into {gov}")
    for stub in record["entrypoint"]:
        print(f"  entry point  {stub['path']} -> {layout.ENTRYPOINT}"
              + (f" (original kept in {stub['backup']})" if stub["backup"] else ""))
    for move in record["moved"]:
        print(f"  moved        {move['from']} -> {move['to']}")
    for item in record["retired"]:
        print(f"  retired      {item['path']} (kept in {item['backup']})")
    for item in record["marketplace"]:
        print(f"  marketplace  {item['name']} ({item['repo']}) known in {item['settings']}")
    for item in record["plugin"]:
        print(f"  plugin       {item['id']} enabled in {item['settings']}")
    if added:
        print(f"  baseline     {len(added)} breach(es) recorded so the project starts green")
    if with_report:
        _report(ctx, gov / "install-report.md", "Install report", old, baseline=added)
    return 0


def _baseline_new(ctx: Context) -> list[str]:
    """Record every current ratchet breach not already in the baseline: a project must start
    green, but never by raising an entry already recorded (the same rule `cmd_baseline`
    enforces without `--allow-raise`). Unlike
    `cmd_baseline`, this never lowers or removes an entry either: install only adds."""
    try:
        old = ratchet.load_baseline(ctx)
    except ratchet.BaselineUnreadable:
        return []
    current = ratchet.compute(ctx)
    new = dict(old)
    added = []
    for key, val in current.items():
        if key not in old:
            new[key] = val
            added.append(f"{key}={val}")
    if added:
        ratchet.write_baseline(ctx, new)
    return added


def _report(ctx: Context, path: Path, title: str, old, notes=None, baseline=None) -> None:
    counts = report.write(path, title, old, _new_findings(ctx),
                          report.non_default_settings(ctx), __version__, notes, baseline)
    rel = path.relative_to(ctx.root).as_posix()
    if old is None:
        print(f"  report       {rel}: {counts['new']} finding(s); no previous gate to compare")
    else:
        print(f"  report       {rel}: {counts['new']} new, {counts['gone']} no longer "
              f"reported, {counts['same']} unchanged"
              + (f" (new from: {', '.join(counts['checks'])})" if counts["checks"] else ""))


def _rollback(root: Path, record: dict) -> None:
    for rec in record["settings"]:
        with contextlib.suppress(OSError):
            _restore_settings_bytes(root, rec)      # written moments ago: the original returns
    for move in record["moved"]:
        if (root / move["to"]).exists():
            shutil.move(str(root / move["to"]), str(root / move["from"]))
    for item in record["retired"]:
        if (root / item["backup"]).exists():
            shutil.move(str(root / item["backup"]), str(root / item["path"]))
    for stub in record["entrypoint"]:
        path = root / stub["path"]
        if stub["backup"]:
            shutil.copy2(root / stub["backup"], path)
        elif path.exists():
            path.unlink()
    shutil.rmtree(root / layout.GOV_DIR, ignore_errors=True)


# ---------------------------------------------------------------------------- upgrade

PIN_RE = re.compile(r'^(\s*engine\s*=\s*)"[^"]*"', re.M)


def upgrade(root: Path, with_report: bool = True) -> int:
    """Pin the project to this engine, refresh the tool's own files, and report what changed.
    Run it with the engine being upgraded to (installed first with install-engine)."""
    if not (root / layout.MANIFEST).is_file():
        return fail(f"no {layout.MANIFEST} — install first")
    cfg_path = root / layout.CONFIG
    text = cfg_path.read_text(encoding="utf-8")
    if not PIN_RE.search(text):
        return fail(f"{layout.CONFIG} has no [governance] engine pin to upgrade")
    old_pin = tomllib.loads(text).get("governance", {}).get("engine")
    old_engine = notice.resolve(old_pin, layout.home()) if isinstance(old_pin, str) else None
    old = None
    if with_report:
        old = report.run_old_gate(root, [sys.executable, str(root / layout.ENTRYPOINT), "check"],
                                  _gate_env())
        old.engine = old_engine
    new_text = PIN_RE.sub(lambda m: f'{m.group(1)}"{__version__}"', text, count=1)
    cfg_path.write_text(new_text, encoding="utf-8")
    try:
        ctx = _load_context(root)
    except (config.ConfigError, registry.RegistryMissing) as exc:
        cfg_path.write_text(text, encoding="utf-8")
        return fail(f"upgrade rolled back: the policy does not load on engine {__version__}: {exc}")
    _write_tool_files(root / layout.GOV_DIR)
    record = read_manifest(root)
    record["engine"] = __version__
    write_manifest(root / layout.MANIFEST, record)
    ran = f"{old_pin} (ran {old_engine})" if old_engine and old_engine != old_pin else old_pin
    print(f"upgraded {layout.DISPLAY_NAME} {ran} -> {__version__} in {root / layout.GOV_DIR}")
    if with_report:
        notes = releases.between(old_engine, __version__)
        _report(ctx, root / layout.GOV_DIR / "upgrade-report.md",
                f"Upgrade report: {ran} to {__version__}", old, notes)
        if notes:
            print(f"  notes        {len(notes)} release(s) in the report: "
                  f"{', '.join(v for v, _ in notes)}")
    return 0


# ---------------------------------------------------------------------------- uninstall

def uninstall(root: Path, force: bool = False) -> int:
    """Reverse an install. Every conflict is found before anything changes: if one exists (an
    entry-point stub edited since install, a path to restore that is occupied), nothing is
    touched unless --force, which restores the originals over them."""
    if not (root / layout.MANIFEST).is_file():
        return fail(f"no {layout.MANIFEST} — nothing installed here, or installed by hand")
    record = read_manifest(root)
    conflicts = []
    for move in record["moved"]:
        if (root / move["from"]).exists():
            conflicts.append(f"{move['from']} exists, so {move['to']} cannot move back")
    for item in record["retired"]:
        if (root / item["path"]).exists():
            conflicts.append(f"{item['path']} exists, so the retired original cannot return")
    for stub in record["entrypoint"]:
        path = root / stub["path"]
        if path.exists() and sha(path) != stub["sha256"]:
            conflicts.append(f"{stub['path']} was edited after install")
    for rec in record["settings"]:
        edited = _settings_edited(root, rec)
        if edited:
            conflicts.append(edited + (" (--force restores the original)" if "removed" in edited
                                       else " (--force removes only the plugin's own lines)"))
    by_key = (record["plugin"] or record["marketplace"]) and (
        not record["settings"] or _settings_edited(root, record["settings"][0]))
    if by_key:
        try:
            _read_settings(root)
        except ValueError as exc:
            conflicts.append(f"{SETTINGS} cannot be read ({exc}), so the plugin lines cannot be "
                             f"removed")
    if conflicts and not force:
        for c in conflicts:
            print(f"  conflict     {c}", file=sys.stderr)
        return fail("nothing was changed — resolve the conflicts, or rerun with --force to "
                    "restore the originals over them", 1)
    for move in record["moved"]:
        src, dst = root / move["to"], root / move["from"]
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                dst.unlink()
            shutil.move(str(src), str(dst))
            print(f"  moved back   {move['to']} -> {move['from']}")
    for item in record["retired"]:
        src, dst = root / item["backup"], root / item["path"]
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                dst.unlink()
            shutil.move(str(src), str(dst))
            print(f"  restored     {item['path']}")
    for stub in record["entrypoint"]:
        path = root / stub["path"]
        if stub.get("backup") and (root / stub["backup"]).exists():
            shutil.copy2(root / stub["backup"], path)
            print(f"  restored     {stub['path']}")
        elif path.exists():
            path.unlink()
            print(f"  removed      {stub['path']}")
    _restore_settings(root, record, print)
    shutil.rmtree(root / layout.GOV_DIR)
    print(f"uninstalled {layout.DISPLAY_NAME} from {root}")
    return 0


# ---------------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m govern.installer", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("install", help="install into a project")
    i.add_argument("--root", required=True, type=Path)
    i.add_argument("--config", required=True, type=Path)
    i.add_argument("--entrypoint", action="append", default=[],
                   help="an existing gate path to keep working (repeatable)")
    i.add_argument("--migrate-baseline", default=None,
                   help="an existing ratchet baseline to move into the tool's directory")
    i.add_argument("--baseline", default=None, type=Path, metavar="FILE",
                   help="a baseline to start from, copied in (anywhere; not recorded)")
    i.add_argument("--retire", action="append", default=[],
                   help="a file the new gate makes obsolete, backed up and removed (repeatable)")
    i.add_argument("--no-report", action="store_true", help="skip install-report.md")
    i.add_argument("--enable-plugin", default=None, metavar="ID",
                   help="opt the project in to the Claude Code plugin, e.g. "
                        + layout.SKILLS_DIR_PLUGIN_ID)
    g = sub.add_parser("upgrade", help="pin the project to this engine and report what changed")
    g.add_argument("--root", required=True, type=Path)
    g.add_argument("--no-report", action="store_true", help="skip upgrade-report.md")
    m = sub.add_parser("migrate", help="convert logs and traps in another format to the standard")
    m.add_argument("--root", required=True, type=Path)
    m.add_argument("--apply", action="store_true",
                   help="make the edits (default is a dry run that only writes the report)")
    e = sub.add_parser("enable-plugin", help="opt an installed project in to the plugin")
    e.add_argument("--root", required=True, type=Path)
    e.add_argument("--id", required=True,
                   help=f"{layout.PLUGIN_ID}, or {layout.SKILLS_DIR_PLUGIN_ID} for development")
    e.add_argument("--marketplace", default=None, metavar="OWNER/REPO",
                   help=f"the GitHub repo the plugin's marketplace is in (default for "
                        f"{layout.PLUGIN_ID}: {layout.PUBLIC_REPO})")
    ms = sub.add_parser("measure", help="measure a project before adopting (read-only)")
    ms.add_argument("--root", required=True, type=Path)
    ms.add_argument("--json", action="store_true", help="print the measurement as JSON")
    ad = sub.add_parser("adopt", help="propose a config from measure; --apply installs it")
    ad.add_argument("--root", required=True, type=Path)
    ad.add_argument("--source", default=None,
                    help=f"[governance] source: where releases come from (default "
                         f"{layout.PUBLIC_SOURCE}, or the --marketplace fork's; "
                         f"'{SOURCE_NONE}' leaves it unset)")
    ad.add_argument("--marketplace", default=None, metavar="OWNER/REPO",
                    help=f"the GitHub repo teammates install the plugin from (default "
                         f"{layout.PUBLIC_REPO}; a fork's)")
    ad.add_argument("--profile", default=None, help="[governance] profile: the principles profile")
    ad.add_argument("--answers", default=None, type=Path,
                    help='a TOML file of key = "option", one per question')
    ad.add_argument("--apply", action="store_true",
                    help="install the proposal (refused while a question is open)")
    ad.add_argument("--json", action="store_true",
                    help="print the proposal and the measurement as JSON")
    u = sub.add_parser("uninstall", help="reverse an install")
    u.add_argument("--root", required=True, type=Path)
    u.add_argument("--force", action="store_true",
                   help="restore originals even over edits made since install")
    args = ap.parse_args(argv)
    root = args.root.resolve()
    if args.cmd == "install":
        return install(root, args.config.resolve(), args.entrypoint, args.migrate_baseline,
                       args.retire, not args.no_report, args.enable_plugin,
                       baseline=args.baseline.resolve() if args.baseline else None)
    if args.cmd == "enable-plugin":
        return enable_plugin(root, args.id, args.marketplace)
    if args.cmd == "upgrade":
        return upgrade(root, not args.no_report)
    if args.cmd == "migrate":
        return migrate.run(root, args.apply)
    if args.cmd == "measure":
        from govern import measure
        return measure.run(root, args.json)
    if args.cmd == "adopt":
        from govern import adopt
        return adopt.run(root, args.source, args.profile,
                         args.answers.resolve() if args.answers else None, args.apply, args.json,
                         args.marketplace)
    return uninstall(root, args.force)


if __name__ == "__main__":
    raise SystemExit(main())
