"""The project's policy (`.context-gate/config.toml`): loading, validation, and
resolving each check's settings.

Resolution order, later winning: engine defaults from the manifest, then the
shared profile's `principles.toml`, then the project file, then `local.toml` when this engine is
the beta it names.

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

from govern import __version__, layout, manifest, versions

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
                 "purpose", "handoff", "license", "upstream", "runtime_gate")

# Names an earlier engine spelled differently, old -> new. An old name still loads, read as the
# new one, with a warning to rename it (an upgrade never turns a project red); both names in one
# place is an error, since neither can be read over the other. Two lookups, so a word is only
# ever renamed where it was a name of that kind:
# - check ids, and the key a built-in check's table-typed settings read under the same word (a
#   `licenses` conflict side lists licenses under `licenses`);
RENAMED_CHECKS = {"licences": "licenses"}  # writing-rules: allow licence, licences
# - registry facts, wherever the engine reads a fact's name (`Scope.get` maps them too).
RENAMED_FACTS = {"licence": "license"}  # writing-rules: allow licence
# Every old name, for writing-rules' vocabulary.
RENAMED = {**RENAMED_CHECKS, **RENAMED_FACTS}

# Settings whose values name registry facts: (check, list param).
FACT_LISTS = {("registry", "required_keys")}

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
    "blocks": {"workspace": "tables", "project": "tables", "registry_columns": "tables"},
    "checks": {"workspace_order": "list", "project_order": "list"},
}

# Keys an earlier engine accepted and this one no longer reads: named with what to do, rather than
# reported as unknown, so a config written for the earlier engine says why it stopped loading.
RETIRED = {("blocks", "placeholder"): "no longer used (it was never read): remove it"}

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
    # (layer, check, setting, added, removed): a layer widened a list past what it inherited
    # (the engine standard, or the profile), with no reason, under `require_reasons = false`
    # (with it, the default, loading refuses the widening)
    list_overrides: list[tuple[str, str, str, list, list]] = field(default_factory=list)
    # What loading found that is legal but almost certainly not meant (`excluded_docs`), for
    # every command to say once, before its own output
    warnings: list[str] = field(default_factory=list)

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


def check_pin(pin, local: bool = False) -> str | None:
    """A project runs exactly the engine it pins: the same commit gets the same engine on every
    machine and in CI, and upgrading is a deliberate step. A series pin ("0.4") runs within its
    series, with a note, until the project upgrades. Returns that note, or None. `local`: the beta
    that local.toml names runs whatever release is pinned, so only the version match is skipped."""
    if pin is None:
        raise ConfigError(f"{CONFIG_NAME}: [governance] engine is required — pin the exact "
                          f"engine version this policy was written for (this engine is "
                          f"{__version__})")
    if not isinstance(pin, str):
        raise ConfigError(f"{CONFIG_NAME}: [governance] engine must be a string like "
                          f"\"{__version__}\"")
    if versions.is_beta(pin):
        raise ConfigError(f"{CONFIG_NAME} pins beta {pin}; a beta runs only from {layout.LOCAL} "
                          f"on one machine (govern beta on {pin}) — pin a release")
    if local:
        return None
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


def _local_layer(root: Path) -> dict | None:
    """local.toml, when this engine is the beta it names: the top layer, this machine only.
    Any other engine never reads it (a stable engine must not trip on a beta's checks). Read as
    the entry point reads it, so the two agree on which engine the file names."""
    if not versions.is_beta(__version__):
        return None
    try:
        with (root / layout.LOCAL).open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError):
        return None
    gov = data.get("governance", {})
    if not isinstance(gov, dict) or gov.get("engine") != __version__:
        return None
    extra = sorted(set(gov) - {"engine", "plugins_before"}) \
        + sorted(set(data) - {"governance", "checks"})
    if extra:
        raise ConfigError(f"{layout.LOCAL}: sets {', '.join(extra)}; it holds [governance] "
                          f"engine, plugins_before and [checks.*] only")
    # What `govern beta on` found in settings.local.json, for `govern beta off` to put back:
    # not a setting, so only its shape is checked.
    before = gov.get("plugins_before", {})
    if not isinstance(before, dict) or not all(isinstance(v, bool) for v in before.values()):
        raise ConfigError(f"{layout.LOCAL}: [governance] plugins_before must be a table of "
                          f"plugin id = true or false")
    checks = data.get("checks", {})
    if not isinstance(checks, dict):
        raise ConfigError(f"{layout.LOCAL}: [checks] must be a table")
    order = sorted(set(checks) & set(LAYOUT["checks"]))
    if order:
        raise ConfigError(f"{layout.LOCAL}: [checks] sets {', '.join(order)}; it holds "
                          f"[governance] engine, plugins_before and [checks.*] only")
    return data


def renamed(where: str, old: str, new: str) -> str:
    """The warning for an old name (RENAMED_CHECKS, RENAMED_FACTS) that still loads."""
    return f"{where}: '{old}' is now '{new}' (the old name still works; rename it)"


def rename_clash(where: str, old: str, new: str) -> ConfigError:
    return ConfigError(f"{where}: '{old}' and '{new}' are both set — '{old}' is the old name "
                       f"of '{new}', so neither can be read over the other: keep '{new}'")


def check_id(cid: str) -> str:
    """A check id as this engine names it: an old name (RENAMED_CHECKS) read as the new one,
    unless a check (an extension's) is registered under the old name itself."""
    return cid if cid in manifest.CHECKS else RENAMED_CHECKS.get(cid, cid)


def fact_name(key: str) -> str:
    """A registry fact's name as this engine reads it: an old name (RENAMED_FACTS) read as the
    new one."""
    return RENAMED_FACTS.get(key, key)


def _renamed_keys(table: dict, renames: dict[str, str], known, where: str,
                  warnings: list[str]) -> dict:
    """`table` with each old key in `renames` whose new name is in `known` (None: any) read as
    the new one, in its place, and one warning per old key used."""
    out = dict(table)
    for old, new in renames.items():
        if old not in table or (known is not None and (new not in known or old in known)):
            continue
        if new in table:
            raise rename_clash(where, old, new)
        msg = renamed(where, old, new)
        if msg not in warnings:
            warnings.append(msg)
        out = {(new if k == old else k): v for k, v in out.items()}
    return out


def _renamed_checks(raw: dict, where: str, warnings: list[str]) -> dict:
    """A config's or a profile's settings with every old check id read as the new one — a
    `[checks.*]` table, a `workspace_order`/`project_order` entry — and every old key inside a
    built-in check's table-typed fields (the sides of a `licenses` conflict). Run once the
    project's extensions are registered: a check an extension registers under an old name is
    that check, not the renamed one."""
    checks = raw.get("checks")
    if not isinstance(checks, dict):
        return raw
    checks = _renamed_keys(checks, RENAMED_CHECKS, manifest.CHECKS, f"{where} [checks]",
                           warnings)
    for key in LAYOUT["checks"]:
        names = checks.get(key)
        if not isinstance(names, list):
            continue
        out = []
        for cid in names:
            new = check_id(cid) if isinstance(cid, str) else cid
            if new != cid:
                if new in names:
                    raise rename_clash(f"{where} [checks] {key}", cid, new)
                warnings.append(renamed(f"{where} [checks] {key}", cid, new))
            out.append(new)
        checks[key] = out
    for cid, table in checks.items():
        chk = manifest.CHECKS.get(cid)
        if chk is None or not isinstance(table, dict) \
                or not chk.fn.__module__.startswith("govern.checks."):
            continue
        table = dict(table)
        for key, value in table.items():
            base = key.removeprefix("extend_")
            if (cid, base) in FACT_LISTS and isinstance(value, list):
                table[key] = _renamed_values(value, f"{where} [checks.{cid}] {key}", warnings)
                continue
            p = chk.params.get(base)
            sides = [f for f, kind in (p.fields if p else {}).items() if kind == "table"]
            if not sides or p.type not in ("table", "tables"):
                continue
            items = value if isinstance(value, list) else [value]
            fixed = [{f: (_renamed_keys(v, RENAMED_CHECKS, None, f"{where} [checks.{cid}] {key}",
                                        warnings)
                          if f in sides and isinstance(v, dict) else v)
                      for f, v in item.items()} if isinstance(item, dict) else item
                     for item in items]
            table[key] = fixed if isinstance(value, list) else fixed[0]
        checks[cid] = table
    return {**raw, "checks": checks}


def _renamed_layout(raw: dict, where: str, warnings: list[str]) -> dict:
    """The project's settings with every old registry fact read as the new one: a `[repo]`
    key, a `[registry.keys]` key, a `[projects] required_when` key. Run before validation, which
    would otherwise refuse the old name as unknown."""
    raw = dict(raw)
    if isinstance(raw.get("repo"), dict):
        raw["repo"] = _renamed_keys(raw["repo"], RENAMED_FACTS, REGISTRY_KEYS, f"{where} [repo]",
                                    warnings)
    reg = raw.get("registry")
    if isinstance(reg, dict) and isinstance(reg.get("keys"), dict):
        raw["registry"] = {**reg, "keys": _renamed_keys(reg["keys"], RENAMED_FACTS, REGISTRY_KEYS,
                                                        f"{where} [registry.keys]", warnings)}
    for section, key in (("projects", "required_when"), ("blocks", "registry_columns")):
        table = raw.get(section)
        if isinstance(table, dict) and isinstance(table.get(key), list):
            raw[section] = {**table, key: [
                _renamed_fact_key(item, f"{where} [{section}] {key}[{i}] key", warnings)
                for i, item in enumerate(table[key])]}
    return raw


def _renamed_fact_key(item, where: str, warnings: list[str]):
    """A table whose `key` names a registry fact, with an old name read as the new one."""
    old = item.get("key") if isinstance(item, dict) else None
    if not isinstance(old, str) or fact_name(old) == old:
        return item
    warnings.append(renamed(where, old, fact_name(old)))
    return {**item, "key": fact_name(old)}


def _renamed_values(names: list, where: str, warnings: list[str]) -> list:
    """A list of registry fact names, with each old name read as the new one."""
    out = []
    for name in names:
        new = fact_name(name) if isinstance(name, str) else name
        if new != name:
            if new in names:
                raise rename_clash(where, name, new)
            warnings.append(renamed(where, name, new))
        out.append(new)
    return out


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
            if (section, key) in RETIRED:
                raise ConfigError(f"{name}: [{section}] {key} is {RETIRED[section, key]}")
            if key not in LAYOUT[section]:
                raise ConfigError(f"{name}: unknown key '{key}' in [{section}]")
            _check_type(f"{name}: [{section}] {key}", v, LAYOUT[section][key])
            if key == "docs":
                # Each a glob: read at load (`excluded_docs`), so a non-string one is named
                # here rather than failing every command with a traceback.
                for i, item in enumerate(v):
                    _check_type(f"{name}: [{section}] docs[{i}]", item, "str")
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


def normalize_glob(glob: str) -> str:
    """A configured glob as `Path.glob` and `glob_matches` read it: `/` for every `\\`, one `/`
    for a run of them, and no `.` segment (`./docs/*.md` and `docs/./*.md` are `docs/*.md`, `a/.`
    is `a`), so a glob written for Windows or with a `.` in it means what it says. A leading or
    trailing `/` stays. `''` when it names no path at all."""
    glob = re.sub(r"/{2,}", "/", glob.replace("\\", "/"))
    return "/".join(s for s in glob.split("/") if s != ".")


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
    if p.globs:
        for item in value:
            _check_type(where, item, "str")
            if _is_absolute_pattern(item) or _is_absolute_pattern(normalize_glob(item)):
                raise ConfigError(f"{where}: '{item}' must be relative to the governance root, "
                                  f"not absolute")
            if normalize_glob(item) == "":
                raise ConfigError(f"{where}: '{item}' names no path — a glob names files "
                                  f"relative to the governance root")
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


def widening(added: list, removed: list) -> str:
    """What a list widening did, naming only its loosening side (see `_list_widening`)."""
    return ("adds " + ", ".join(repr(v) for v in added) if added
            else "drops " + ", ".join(repr(v) for v in removed))


def _resolve_checks(layers: list[tuple[str, dict]], require_reasons: bool,
                    overrides: list | None = None,
                    list_overrides: list | None = None,
                    profile_name: str = "profile") -> dict[str, CheckSettings]:
    """`profile_name`: how an error names the profile layer (its source), so a widening it
    refuses points at the file to fix."""
    overrides = [] if overrides is None else overrides
    list_overrides = [] if list_overrides is None else list_overrides
    settings: dict[str, CheckSettings] = {}
    for cid, chk in manifest.CHECKS.items():
        s = CheckSettings(level=chk.default, params={k: p.default for k, p in chk.params.items()})
        s.source = {k: "engine" for k in ["level", *chk.params]}
        settings[cid] = s
    for layer, raw in layers:
        label = layout.LOCAL if layer == "local" else layer
        for cid, table in raw.get("checks", {}).items():
            if cid in LAYOUT["checks"]:
                continue
            if cid not in manifest.CHECKS:
                raise ConfigError(f"{label}: [checks.{cid}] is not a registered check")
            if not isinstance(table, dict):
                raise ConfigError(f"{label}: [checks.{cid}] must be a table")
            chk, s = manifest.CHECKS[cid], settings[cid]
            before = dict(s.params)
            for key, value in table.items():
                where = f"{label}: [checks.{cid}] {key}"
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
                    if layer in ("project", "local") and s.source.get("level") == "profile" \
                            and manifest.LEVELS.index(value) < manifest.LEVELS.index(s.level) \
                            and not table.get("reason"):
                        overrides.append((cid, "level"))
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
                    if layer in ("project", "local") and s.source.get(key) == "profile" \
                            and s.params[key] != value and not table.get("reason"):
                        overrides.append((cid, key))
                    s.params[key] = value
                else:
                    known = ["level", "reason", "reasons",
                             *(["ratchet"] if chk.ratchets else []), *chk.params]
                    raise ConfigError(f"{where}: unknown setting (known: {', '.join(known)})")
                s.source[key] = layer
            widened = _list_widening(chk, before, s, table)
            if widened and require_reasons:
                # Widening a list is loosening, refused like a loosened limit: a profile's
                # widening as much as a project's.
                if layer == "profile":
                    from govern.profile import PRINCIPLES
                    raise ConfigError("; ".join(
                        f"{profile_name}: [checks.{cid}] {key} {widening(added, removed)} — "
                        f"loosens past the engine standard without a reason: say why under "
                        f"[checks.{cid}.reasons] {key} in the profile's {PRINCIPLES} (a pinned "
                        f"profile: then tag it and move the project's pin), or keep the "
                        f"standard's list"
                        for key, added, removed in widened))
                raise ConfigError("; ".join(
                    f"{label}: [checks.{cid}] {key} {widening(added, removed)} — loosens past "
                    f"what the {'committed config' if layer == 'local' else layer} inherits "
                    f"without a reason: say why in "
                    f"[checks.{cid}.reasons] {key}, or keep the inherited list"
                    for key, added, removed in widened))
            widened_keys = {key for key, _, _ in widened}
            # A list's own loosening message covers the override completely (one warning
            # per override): drop the generic "overrides the profile" entry for the same key.
            overrides[:] = [ov for ov in overrides
                            if not (ov[0] == cid and ov[1] in widened_keys)]
            for key, added, removed in widened:
                list_overrides.append((layer, cid, key, added, removed))
    if require_reasons:
        for cid, s in settings.items():
            chk = manifest.CHECKS[cid]
            loosened = [k for k, p in chk.params.items() if _loosens(p, p.default, s.params[k])]
            if manifest.LEVELS.index(s.level) < manifest.LEVELS.index(chk.default):
                loosened.append("level")
            if not s.ratchet:
                loosened.append("ratchet")
            if loosened and not s.reason:
                # Name the file when the local layer set what loosens.
                src = layout.LOCAL if any(s.source.get(k) == "local" for k in loosened) \
                    else None
                raise ConfigError(
                    f"{src + ': ' if src else ''}[checks.{cid}] loosens {', '.join(loosened)} "
                    f"past the engine default without a 'reason' — relaxing a rule is a choice "
                    f"made in the open")
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


def excluded_docs(raw: dict) -> list[str]:
    """A `docs` entry that names a path under a directory the docs scan always leaves out
    (`context.DEFAULT_DOC_EXCLUDES`: `.claude/x.md`, say) matches nothing the gate governs, and
    was dropped without a word. One warning per such entry, naming it and the exclude that wins.
    Said at load, so every command says it, not only `explain`, which nobody runs to find out
    why a doc they listed is not checked."""
    from govern.context import default_exclude
    out = []
    for section in ("workspace", "projects"):
        for entry in raw.get(section, {}).get("docs", []):
            wins = default_exclude(entry)
            if wins is not None:
                out.append(f"[{section}] docs entry '{entry}' is never governed: the "
                           f"always-applied exclude '{wins}' wins — move the doc, or drop the "
                           f"entry")
    return out


def load(root: Path, home: Path, *, pin_check: bool = True) -> Config:
    """`pin_check=False` skips `check_pin` entirely: for a caller that resolves a setting's
    cascade (profile, then project) without running the project's own pinned engine — the
    plugin's usage resolver, which runs the engine it is bundled with, not whatever a project
    between upgrades happens to pin. Every other caller keeps the default: a project's own
    commands must run exactly the engine it pins."""
    path = root / CONFIG_NAME
    warnings: list[str] = []
    raw = _renamed_layout(_load_toml(path), CONFIG_NAME, warnings)
    _validate_layout(raw, CONFIG_NAME)
    if "registry" in raw and "repo" in raw:
        raise ConfigError(f"{CONFIG_NAME}: [registry] and [repo] are both set — a registry "
                          f"file names the projects; [repo] only describes this root itself, "
                          f"for when there is no registry at all. Keep one.")
    load_extensions(root, raw.get("governance", {}).get("extensions", []))
    raw = _renamed_checks(raw, CONFIG_NAME, warnings)
    for key in ("workspace_order", "project_order"):
        for cid in raw.get("checks", {}).get(key, []):
            if cid not in manifest.CHECKS:
                raise ConfigError(f"{CONFIG_NAME}: [checks] {key} names '{cid}', which is not a "
                                  f"registered check")
    gov = raw.get("governance", {})
    # The beta local.toml names runs whatever the committed pin is; that pin is for every
    # other machine and CI.
    local = _local_layer(root)
    pin_note = check_pin(gov.get("engine"), local=bool(local)) if pin_check else None
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
        prof.settings = _renamed_checks(prof.settings,
                                        f"profile {prof.source} ({profiles.PRINCIPLES})", warnings)
        extra = sorted(set(prof.settings) - {"checks", "dialect", "profile"})
        if extra:
            raise ConfigError(f"profile {profiles.PRINCIPLES}: sets {', '.join(extra)}; a profile "
                              f"holds [checks.*] and [dialect] only — layout belongs to projects")
        pd = prof.settings.get("dialect", {})
        name = f"profile {prof.source} ({profiles.PRINCIPLES})"
        if not isinstance(pd, dict):
            raise ConfigError(f"{name}: [dialect] must be a table")
        for key, value in pd.items():
            if key != "reasons" and key not in DIALECT_DEFAULTS:
                raise ConfigError(f"{name}: unknown [dialect] key '{key}'")
            # Checked as a project's are (`_validate_layout`): a value no dialect reads would
            # otherwise reach every check that branches on it.
            _check_type(f"{name}: [dialect] {key}", value, LAYOUT["dialect"][key])
            if key in DIALECT_CHOICES and value not in DIALECT_CHOICES[key]:
                raise ConfigError(f"{name}: [dialect] {key} = '{value}' is not one of "
                                  f"{DIALECT_CHOICES[key]}")
        layers.append(("profile", prof.settings))
    layers.append(("project", raw))
    if local:
        layers.append(("local", _renamed_checks(local, layout.LOCAL, warnings)))
    dialect = {**DIALECT_DEFAULTS,
               **{k: v for k, v in (prof.settings.get("dialect", {}) if prof else {}).items()
                  if k != "reasons"},
               **{k: v for k, v in raw.get("dialect", {}).items() if k != "reasons"}}
    overrides: list = []
    list_overrides: list = []
    checks = _resolve_checks(layers, gov.get("require_reasons", True), overrides, list_overrides,
                             f"profile {prof.source} ({profiles.PRINCIPLES})" if prof else "profile")
    return Config(root=root, path=path, raw=raw, dialect=dialect, checks=checks,
                  notes=[pin_note] if pin_note else [], profile=prof,
                  profile_overrides=overrides, list_overrides=list_overrides,
                  warnings=warnings + excluded_docs(raw))
