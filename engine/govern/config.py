"""The project's policy (`.context-gate/config.toml`): loading, validation, and
resolving each check's settings.

Resolution order, later winning: engine defaults from the manifest, then the
shared profile's `principles.toml`, then the project file.

Validation is strict on purpose. An unknown or misplaced key is an error, never a value that is
silently not read: a limit read from the wrong place is a limit that never fires.
"""
from __future__ import annotations

import importlib.util
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from govern import __version__, layout, manifest

CONFIG_NAME = layout.CONFIG
SCHEMA = 1

DIALECT_DEFAULTS = {
    "decision_heading": "any-dash",
    "next_id": "max-plus-one",
    "id_overlap": "prefix-aware",
    "agent_turns_prose": "must-match",
    "finished_age": "ge",
    "report": "per-scope",
    "markers": "gov",
}
DIALECT_CHOICES = {
    "decision_heading": ("em-dash", "any-dash"),
    "next_id": ("first-free", "max-plus-one"),
    "id_overlap": ("prefix-aware", "prefix-blind"),
    "agent_turns_prose": ("forbid", "must-match"),
    "finished_age": ("gt", "ge"),
    "report": ("per-scope", "aggregate"),
}

# Dialect settings that are a project's layout, not a competing format: no reason needed to set.
LAYOUT_DIALECT = ("markers",)

# Every key the engine reads from a registry entry, and where it reads it by default.
REGISTRY_KEYS = ("name", "dir", "tier", "role", "governance", "id_prefix", "id_range",
                 "purpose", "handoff", "licence", "upstream", "runtime_gate")

# section -> {key: type}. Types: str, int, bool, list, table, tables (array of tables).
LAYOUT = {
    "governance": {"engine": "str", "source": "str", "profile": "str", "schema": "int",
                   "extensions": "list",
                   "require_reasons": "bool"},
    "dialect": {**{k: "str" for k in DIALECT_DEFAULTS}, "reasons": "table"},
    # `skip`: registry entries, by name, this governance root leaves out entirely (each name is
    # checked against the registry file when `registry.load` reads it).
    "registry": {"file": "str", "entries": "str", "workspace": "str", "keys": "table",
                 "skip": "list"},
    # No [registry] table: this governance root is itself the one project (a plain single
    # repo, no sub-projects). [repo] carries the same facts a registry entry would (see
    # REGISTRY_KEYS), all optional — `registry.py` fills in the ones a single repo can only
    # mean one way (its name, its own directory).
    "repo": {k: "str" for k in REGISTRY_KEYS},
    "workspace": {"label": "str", "docs": "list", "required_docs": "list",
                  "decision_log": "str", "agents_dir": "str", "baseline": "str"},
    "projects": {"governed_tiers": "list", "docs": "list", "exclude": "list",
                 "required_docs": "list", "required_when": "tables",
                 "decision_log": "str", "working_dir": "str", "trap_glob": "str",
                 "trap_prefix": "str"},
    "blocks": {"workspace": "tables", "project": "tables", "placeholder": "str",
               "registry_columns": "tables"},
    "checks": {"workspace_order": "list", "project_order": "list"},
}

TYPES = {"str": str, "int": int, "bool": bool, "list": list, "table": dict, "tables": list}


class ConfigError(Exception):
    pass


@dataclass
class CheckSettings:
    level: str
    params: dict[str, Any]
    reason: str | None = None
    source: dict[str, str] = field(default_factory=dict)   # key -> which layer set it
    ratchet: bool = True       # for a check whose breaches the ratchet tracks


@dataclass
class Config:
    root: Path
    path: Path
    raw: dict
    dialect: dict[str, str]
    checks: dict[str, CheckSettings]
    notes: list[str] = field(default_factory=list)
    profile: Any = None                          # govern.profile.Profile, when one is named
    # (check, setting) pairs where the project changed what its profile set, with no reason
    profile_overrides: list[tuple[str, str]] = field(default_factory=list)
    # (check, setting, added, removed): the project widened a list past what it inherited (the
    # engine standard, or its profile), with no reason
    list_overrides: list[tuple[str, str, list, list]] = field(default_factory=list)

    def section(self, name: str) -> dict:
        return self.raw.get(name, {})

    def get(self, section: str, key: str, default=None):
        return self.raw.get(section, {}).get(key, default)


def series(version: str) -> tuple[int, int]:
    """The major.minor series of a version: a series pin ("0.4") runs only an engine from that
    series."""
    parts = version.split(".")
    if len(parts) < 2 or not all(p.isdigit() for p in parts[:2]):
        raise ConfigError(f"'{version}' is not a version like 0.1")
    return int(parts[0]), int(parts[1])


def check_pin(pin) -> str | None:
    """A project runs exactly the engine it pins: the same commit gets the same engine on every
    machine and in CI, and upgrading is a deliberate step. A series pin ("0.4") runs within its
    series, with a note, until the project upgrades. Returns that note, or None."""
    if pin is None:
        raise ConfigError(f"{CONFIG_NAME}: [governance] engine is required — pin the exact "
                          f"engine version this policy was written for (this engine is "
                          f"{__version__})")
    if not isinstance(pin, str):
        raise ConfigError(f"{CONFIG_NAME}: [governance] engine must be a string like "
                          f"\"{__version__}\"")
    parts = pin.split(".")
    if len(parts) == 3:
        if pin != __version__:
            raise ConfigError(f"{CONFIG_NAME} pins engine {pin}, but this is engine "
                              f"{__version__} — install {pin}, or upgrade the project to "
                              f"{__version__} on purpose")
        return None
    want, have = series(pin), series(__version__)
    if want != have:
        direction = "newer" if want > have else "older"
        raise ConfigError(f"{CONFIG_NAME} pins engine {pin}, which is {direction} than this "
                          f"engine ({__version__}) — install engine {pin}, or upgrade the "
                          f"project on purpose")
    return "series"


def find_root(start: Path) -> Path | None:
    """The governance root: the nearest directory holding the tool's directory."""
    for d in [start, *start.parents]:
        if (d / CONFIG_NAME).is_file():
            return d
    return None


def _load_toml(path: Path) -> dict:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path.name}: not valid TOML ({exc})") from exc
    except OSError as exc:
        raise ConfigError(f"{path}: unreadable ({exc.strerror})") from exc


def _check_type(where: str, value: Any, kind: str) -> None:
    want = TYPES[kind]
    ok = isinstance(value, want) and not (want is int and isinstance(value, bool))
    if kind == "tables" and ok:
        ok = all(isinstance(v, dict) for v in value)
    if not ok:
        raise ConfigError(f"{where}: expected {kind}, got {type(value).__name__}")


def _validate_layout(raw: dict, name: str) -> None:
    for section, value in raw.items():
        if section == "checks":
            continue
        if section not in LAYOUT:
            raise ConfigError(f"{name}: unknown section [{section}]")
        if not isinstance(value, dict):
            raise ConfigError(f"{name}: [{section}] must be a table")
        for key, v in value.items():
            if key not in LAYOUT[section]:
                raise ConfigError(f"{name}: unknown key '{key}' in [{section}]")
            _check_type(f"{name}: [{section}] {key}", v, LAYOUT[section][key])
    for key, choices in DIALECT_CHOICES.items():
        v = raw.get("dialect", {}).get(key)
        if v is not None and v not in choices:
            raise ConfigError(f"{name}: [dialect] {key} = '{v}' is not one of {choices}")
    for key, value in raw.get("registry", {}).get("keys", {}).items():
        if key not in REGISTRY_KEYS:
            raise ConfigError(f"{name}: [registry.keys] '{key}' is not a key the engine reads "
                              f"({', '.join(REGISTRY_KEYS)})")
        _check_type(f"{name}: [registry.keys] {key}", value, "str")
    for i, skip in enumerate(raw.get("registry", {}).get("skip", [])):
        _check_type(f"{name}: [registry] skip[{i}]", skip, "str")
    _validate_tables(raw, name)


BLOCK_RENDERERS = {"workspace": ("agent-roster", "registry", "decision-index"),
                   "project": ("doc-registry", "decision-index", "trap-index")}
COLUMN_FORMATS = ("plain", "dir", "github-slug", "id-range")

# A `[[blocks.project]]` entry needs exactly one of `file`/`glob` (its target: where the marker
# pair lives, and what `generated-blocks` checks). A `trap-index` entry may add `sources` (a
# glob, relative to the scope like `file`/`glob` are): every trap file it matches is read for
# entries, but none of them needs the marker pair — only the block's own target does. This lets
# one hand-written index covering several files become one generated block, without
# turning the other files into block targets.


def _is_absolute_pattern(pattern: str) -> bool:
    """Whether a glob pattern is rooted outside the scope it is meant to search relative to —
    POSIX (`/...`) or Windows (`C:\\...`, `C:x`, `\\x`, `\\\\...`). `Path.glob` refuses one of
    these with a bare `NotImplementedError`, so it is caught here, at load, with a reason."""
    return pattern.startswith(("/", "\\")) or bool(re.match(r"^[A-Za-z]:", pattern))


def _validate_tables(raw: dict, name: str) -> None:
    """The array-of-tables settings: every entry complete, every name one the engine knows."""
    def keys_of(where: str, t: dict, allowed: set, required: set) -> None:
        unknown, missing = set(t) - allowed, required - set(t)
        if unknown:
            raise ConfigError(f"{name}: {where} has unknown key(s) {sorted(unknown)}")
        if missing:
            raise ConfigError(f"{name}: {where} needs {sorted(missing)}")

    blocks = raw.get("blocks", {})
    for kind in ("workspace", "project"):
        allowed = {"file", "glob", "id", "render"} | ({"sources"} if kind == "project" else set())
        for i, t in enumerate(blocks.get(kind, [])):
            where = f"[blocks] {kind}[{i}]"
            keys_of(where, t, allowed, {"id"})
            if ("file" in t) == ("glob" in t) or (kind == "workspace" and "glob" in t):
                need = "'file' or 'glob'" if kind == "project" else "'file'"
                raise ConfigError(f"{name}: {where} needs exactly one of {need}")
            if "glob" in t and _is_absolute_pattern(t["glob"]):
                raise ConfigError(f"{name}: {where} glob '{t['glob']}' must be relative to the "
                                  f"scope, not absolute")
            render = t.get("render", t["id"])
            if render not in BLOCK_RENDERERS[kind]:
                raise ConfigError(f"{name}: {where} renders '{render}', which is not one of "
                                  f"{BLOCK_RENDERERS[kind]} — set 'render'")
            if "sources" in t:
                if render != "trap-index":
                    raise ConfigError(f"{name}: {where} sets 'sources', which only applies to "
                                      f"a trap-index block")
                _check_type(f"{name}: {where} sources", t["sources"], "str")
                if _is_absolute_pattern(t["sources"]):
                    raise ConfigError(f"{name}: {where} sources '{t['sources']}' must be "
                                      f"relative to the scope, not absolute")
    for i, c in enumerate(blocks.get("registry_columns", [])):
        where = f"[blocks] registry_columns[{i}]"
        keys_of(where, c, {"header", "key", "format"}, {"header"})
        fmt = c.get("format", "plain")
        if fmt not in COLUMN_FORMATS:
            raise ConfigError(f"{name}: {where} format '{fmt}' is not one of {COLUMN_FORMATS}")
        if fmt == "plain" and "key" not in c:
            raise ConfigError(f"{name}: {where} needs 'key' (which registry fact to show)")
    for i, r in enumerate(raw.get("projects", {}).get("required_when", [])):
        where = f"[projects] required_when[{i}]"
        keys_of(where, r, {"key", "docs"}, {"key", "docs"})
        if r["key"] not in REGISTRY_KEYS:
            raise ConfigError(f"{name}: {where} key '{r['key']}' is not a key the engine reads")


def _check_param(where: str, p: manifest.Param, value: Any) -> None:
    kind = {"int": "int", "str": "str", "bool": "bool", "list": "list", "enum": "str",
            "table": "table", "tables": "tables"}[p.type]
    _check_type(where, value, kind)
    if p.type == "enum" and value not in p.choices:
        raise ConfigError(f"{where} = '{value}' is not one of {p.choices}")
    if p.type == "tables":
        for i, item in enumerate(value):
            _check_fields(f"{where}[{i}]", p, item)
    if p.type == "table" and p.fields:
        _check_fields(where, p, value)


def _check_fields(where: str, p: manifest.Param, item: dict) -> None:
    unknown = set(item) - set(p.fields)
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {sorted(unknown)} "
                          f"(known: {', '.join(p.fields)})")
    missing = [k for k in p.required if k not in item]
    if missing:
        raise ConfigError(f"{where}: needs {missing}")
    for key, value in item.items():
        _check_type(f"{where} {key}", value, p.fields[key])


def _loosens(p: manifest.Param, default: Any, value: Any) -> bool:
    """Nothing is looser than a param's declared `unlimited` value (e.g. `max_turns`'s 0, "no
    ceiling"): only a param that says so treats its own default that way. Any other param whose
    default happens to be 0 is an ordinary ceiling like any other, and a project raising it past
    0 still owes a reason."""
    if p.unlimited is not None and default == p.unlimited:
        return False
    if p.looser == "higher":
        return value > default
    if p.looser == "lower":
        return value < default
    return False


def _list_widening(chk: manifest.Check, before: dict[str, Any], s: CheckSettings,
                   table: dict) -> list[tuple[str, list, list]]:
    """Which list params this layer's table widened past what it inherited (the state just
    before this table was applied — the engine standard, or a profile that set them first), with
    no per-setting reason in this same table's `reasons`. A table-level `reason` covers
    numeric and level loosening (see `_resolve_checks`) but never a list, since one `reason`
    written for one setting must not silently excuse another (a `reason` written for
    `max_words` must not also excuse `statuses` re-allowing 'superseded'). Returns
    (key, added, removed) per widened param, naming only the side that loosens — the other is
    always empty, so adding and removing in one list never reads as a tightening being called
    loosening."""
    reasons = table.get("reasons", {})
    out = []
    for key, p in chk.params.items():
        if p.type != "list" or (p.looser not in ("more", "fewer") and not p.empty_means_any):
            continue
        old, new = before.get(key), s.params.get(key)
        if old is None or new is None or list(old) == list(new):
            continue
        if str(reasons.get(key, "")).strip():
            continue
        if p.empty_means_any and not new and old:
            out.append((key, [], list(old)))
            continue
        added = [v for v in new if v not in old]
        removed = [v for v in old if v not in new]
        if p.looser == "more" and added:
            out.append((key, added, []))
        elif p.looser == "fewer" and removed:
            out.append((key, [], removed))
    return out


def _resolve_checks(layers: list[tuple[str, dict]], require_reasons: bool,
                    overrides: list | None = None,
                    list_overrides: list | None = None) -> dict[str, CheckSettings]:
    overrides = [] if overrides is None else overrides
    list_overrides = [] if list_overrides is None else list_overrides
    settings: dict[str, CheckSettings] = {}
    for cid, chk in manifest.CHECKS.items():
        s = CheckSettings(level=chk.default, params={k: p.default for k, p in chk.params.items()})
        s.source = {k: "engine" for k in ["level", *chk.params]}
        settings[cid] = s
    for layer, raw in layers:
        for cid, table in raw.get("checks", {}).items():
            if cid in LAYOUT["checks"]:
                continue
            if cid not in manifest.CHECKS:
                raise ConfigError(f"{layer}: [checks.{cid}] is not a registered check")
            if not isinstance(table, dict):
                raise ConfigError(f"{layer}: [checks.{cid}] must be a table")
            chk, s = manifest.CHECKS[cid], settings[cid]
            before = dict(s.params) if layer == "project" else None
            for key, value in table.items():
                where = f"{layer}: [checks.{cid}] {key}"
                base = key.removeprefix("extend_")
                if key.startswith("extend_") and base in chk.params \
                        and chk.params[base].type in ("list", "tables"):
                    # Add to what an earlier layer set, rather than replacing it.
                    _check_param(where, chk.params[base], value)
                    s.params[base] = list(s.params[base]) + list(value)
                    s.source[base] = f"{s.source.get(base, 'engine')} + {layer}"
                    continue
                if key == "level":
                    if value not in manifest.LEVELS:
                        raise ConfigError(f"{where} = '{value}' is not one of {manifest.LEVELS}")
                    if chk.core and value == "off":
                        raise ConfigError(f"{where}: '{cid}' is part of the fixed core and "
                                          f"cannot be turned off")
                    s.level = value
                elif key == "reason":
                    _check_type(where, value, "str")
                    s.reason = value
                elif key == "reasons":
                    # A per-setting reason for list loosening: one reason per key, so a
                    # reason written for one setting (e.g. max_words) never silently excuses
                    # another (e.g. statuses).
                    _check_type(where, value, "table")
                    listy = {k for k, p in chk.params.items() if p.type == "list"}
                    unknown = set(value) - listy
                    if unknown:
                        raise ConfigError(f"{where}: unknown key(s) {sorted(unknown)} (known: "
                                          f"{', '.join(sorted(listy))})")
                    for k, v in value.items():
                        _check_type(f"{where}.{k}", v, "str")
                elif key == "ratchet" and chk.ratchets:
                    _check_type(where, value, "bool")
                    s.ratchet = value
                elif key in chk.params:
                    _check_param(where, chk.params[key], value)
                    if layer == "project" and s.source.get(key) == "profile" \
                            and s.params[key] != value and not table.get("reason"):
                        overrides.append((cid, key))
                    s.params[key] = value
                else:
                    known = ["level", "reason", "reasons",
                             *(["ratchet"] if chk.ratchets else []), *chk.params]
                    raise ConfigError(f"{where}: unknown setting (known: {', '.join(known)})")
                s.source[key] = layer
            if before is not None:
                widened = _list_widening(chk, before, s, table)
                widened_keys = {key for key, _, _ in widened}
                # A list's own loosening message covers the override completely (one warning
                # per override): drop the generic "overrides the profile" entry for the same key.
                overrides[:] = [ov for ov in overrides
                                if not (ov[0] == cid and ov[1] in widened_keys)]
                for key, added, removed in widened:
                    list_overrides.append((cid, key, added, removed))
    if require_reasons:
        for cid, s in settings.items():
            chk = manifest.CHECKS[cid]
            loosened = [k for k, p in chk.params.items() if _loosens(p, p.default, s.params[k])]
            if manifest.LEVELS.index(s.level) < manifest.LEVELS.index(chk.default):
                loosened.append("level")
            if not s.ratchet:
                loosened.append("ratchet")
            if loosened and not s.reason:
                raise ConfigError(
                    f"[checks.{cid}] loosens {', '.join(loosened)} past the engine default "
                    f"without a 'reason' — relaxing a rule is a choice made in the open")
    return settings


def load_extensions(root: Path, dirs: list[str]) -> None:
    """Import a project's own checks. They register through the same manifest as the engine's,
    and run with the same trust as the project's other scripts."""
    for d in dirs:
        if not (root / d).is_dir():
            raise ConfigError(f"{CONFIG_NAME}: extensions directory '{d}' does not exist")
        for path in sorted((root / d).glob("*.py")):
            name = f"govern_ext_{path.stem.replace('-', '_')}"
            spec = importlib.util.spec_from_file_location(name, path)
            if spec is None or spec.loader is None:
                raise ConfigError(f"extension {path} could not be loaded")
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)


def load(root: Path, home: Path) -> Config:
    path = root / CONFIG_NAME
    raw = _load_toml(path)
    _validate_layout(raw, CONFIG_NAME)
    if "registry" in raw and "repo" in raw:
        raise ConfigError(f"{CONFIG_NAME}: [registry] and [repo] are both set — a registry "
                          f"file names the projects; [repo] only describes this root itself, "
                          f"for when there is no registry at all. Keep one.")
    load_extensions(root, raw.get("governance", {}).get("extensions", []))
    for key in ("workspace_order", "project_order"):
        for cid in raw.get("checks", {}).get(key, []):
            if cid not in manifest.CHECKS:
                raise ConfigError(f"{CONFIG_NAME}: [checks] {key} names '{cid}', which is not a "
                                  f"registered check")
    gov = raw.get("governance", {})
    pin_note = check_pin(gov.get("engine"))
    if gov.get("schema", SCHEMA) != SCHEMA:
        raise ConfigError(f"{CONFIG_NAME}: schema {gov.get('schema')} is not supported "
                          f"(this engine reads schema {SCHEMA})")
    layers: list[tuple[str, dict]] = []
    prof = None
    if gov.get("profile"):
        from govern import profile as profiles
        try:
            prof = profiles.load(gov["profile"], root, home)
        except profiles.ProfileError as exc:
            raise ConfigError(str(exc)) from exc
        extra = sorted(set(prof.settings) - {"checks", "dialect", "profile"})
        if extra:
            raise ConfigError(f"profile {profiles.PRINCIPLES}: sets {', '.join(extra)}; a profile "
                              f"holds [checks.*] and [dialect] only — layout belongs to projects")
        pd = prof.settings.get("dialect", {})
        for key in pd:
            if key != "reasons" and key not in DIALECT_DEFAULTS:
                raise ConfigError(f"profile {profiles.PRINCIPLES}: unknown [dialect] key '{key}'")
        layers.append(("profile", prof.settings))
    layers.append(("project", raw))
    dialect = {**DIALECT_DEFAULTS,
               **{k: v for k, v in (prof.settings.get("dialect", {}) if prof else {}).items()
                  if k != "reasons"},
               **{k: v for k, v in raw.get("dialect", {}).items() if k != "reasons"}}
    overrides: list = []
    list_overrides: list = []
    checks = _resolve_checks(layers, gov.get("require_reasons", True), overrides, list_overrides)
    return Config(root=root, path=path, raw=raw, dialect=dialect, checks=checks,
                  notes=[pin_note] if pin_note else [], profile=prof,
                  profile_overrides=overrides, list_overrides=list_overrides)
