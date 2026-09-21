"""The registry of scopes a governance root covers: the workspace, plus one scope per registry
entry (a project or repo) — or, when the config
sets no `[registry]` table at all, the one scope a plain single repo is: itself.

Where each fact lives in an entry is configured (`[registry.keys]`), and the registry is checked
against that configuration: a key found somewhere other than where the engine reads it is an
error, and a malformed ID range is a finding, not a crash.
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from govern.config import CONFIG_NAME, REGISTRY_KEYS, Config, ConfigError

RANGE_RE = re.compile(r"^(\d+)-(\d+)$")


def parse_range(value: str) -> tuple[int, int] | None:
    m = RANGE_RE.match(value.strip()) if isinstance(value, str) else None
    if not m or int(m.group(1)) > int(m.group(2)):
        return None
    return int(m.group(1)), int(m.group(2))


def dig(entry: dict, dotted: str) -> Any:
    cur: Any = entry
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


@dataclass
class Scope:
    name: str
    entry: dict
    keys: dict[str, str]
    gov: Path | None
    id_prefix: str | None
    id_range: tuple[int, int] | None
    doc_set: bool       # tier requires the doc set
    governed: bool      # ...and a governance dir is named
    is_workspace: bool = False

    def get(self, key: str, default=None):
        v = dig(self.entry, self.keys.get(key, key))
        return default if v is None else v


@dataclass
class Registry:
    path: Path
    data: dict
    scopes: list[Scope]
    workspace_prefix: str | None
    workspace_range: tuple[int, int] | None
    workspace: Scope
    problems: list[str] = field(default_factory=list)
    # No `[registry]` table at all: this governance root is the one project there is (the
    # single-repo shape). A single-repo config gives the workspace scope no decision log of its
    # own by default (`Context.workspace_log`), and a file's ownership (`Context.owner`) is
    # decided the same way either way — it just always resolves to this one project scope, since
    # its own directory is the root itself.
    single: bool = False

    def find(self, name: str) -> Scope | None:
        return next((s for s in self.scopes if s.name == name), None)


class RegistryMissing(Exception):
    pass


def load(cfg: Config) -> Registry:
    """A `[registry]` table means a multi-project workspace: load it, and a missing file it
    names is an error. No `[registry]` table at all means a single repo: this governance root
    is the one project there is, described by an optional
    `[repo]` table instead of a registry entry."""
    if "registry" not in cfg.raw:
        return _load_single(cfg)
    reg = cfg.section("registry")
    path = cfg.root / reg.get("file", "registry.toml")
    if not path.exists():
        raise RegistryMissing(str(path))
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path.name}: not valid TOML ({exc})") from exc
    keys = {k: k for k in REGISTRY_KEYS}
    keys.update(reg.get("keys", {}))
    entries = data.get(reg.get("entries", "project"), [])
    skip = reg.get("skip", [])
    unknown = [n for n in skip if n not in {e.get("name") for e in entries}]
    if unknown:
        raise ConfigError(f"{CONFIG_NAME}: [registry] skip names {', '.join(unknown)}, not an "
                          f"entry in {path.name}")
    # A skipped entry is dropped entirely: no scope, no check, no problem reported against it.
    entries = [e for e in entries if e.get("name") not in skip]
    ws = data.get(reg.get("workspace", "workspace"), {})
    problems: list[str] = []
    tiers = cfg.get("projects", "governed_tiers", ["full"])

    # A fact the engine reads from one place, found in another: the configured leaf anywhere
    # else, or the key's own name at the top level when it is configured inside a sub-table.
    for key, dotted in keys.items():
        leaf = dotted.rsplit(".", 1)[-1]
        for e in entries:
            spots = [(leaf, e)] + [(f"{k}.{leaf}", v) for k, v in e.items() if isinstance(v, dict)]
            found = [where for where, table in spots if leaf in table and where != dotted]
            if "." in dotted and key != leaf and key in e:
                found.append(key)
            for where in found:
                problems.append(f"registry: '{e.get('name', '?')}' has '{where}', but the engine "
                                f"reads {key} from '{dotted}' — fix the entry or [registry.keys]")

    scopes = []
    for e in entries:
        name = e.get("name", "?")
        raw_range = dig(e, keys["id_range"])
        rng = None
        if raw_range:
            rng = parse_range(raw_range)
            if rng is None:
                problems.append(f"registry: '{name}' id_range '{raw_range}' is not LO-HI")
        gov_rel = dig(e, keys["governance"])
        doc_set = dig(e, keys["tier"]) in tiers
        scopes.append(Scope(
            name=name, entry=e, keys=keys, gov=(cfg.root / gov_rel) if gov_rel else None,
            id_prefix=dig(e, keys["id_prefix"]), id_range=rng, doc_set=doc_set,
            governed=doc_set and bool(gov_rel)))

    ws_range = None
    if ws.get("id_range"):
        ws_range = parse_range(ws["id_range"])
        if ws_range is None:
            problems.append(f"registry: workspace id_range '{ws['id_range']}' is not LO-HI")
    label = cfg.get("workspace", "label", "workspace")
    workspace = Scope(name=label, entry=ws, keys={}, gov=None, id_prefix=ws.get("id_prefix"),
                      id_range=ws_range, doc_set=False, governed=True, is_workspace=True)
    return Registry(path=path, data=data, scopes=scopes, workspace_prefix=ws.get("id_prefix"),
                    workspace_range=ws_range, workspace=workspace, problems=problems)


def _load_single(cfg: Config) -> Registry:
    """The one scope a single repo is: the governance root itself. Its facts come from an
    optional `[repo]` table (any key a registry entry could carry — REGISTRY_KEYS); three that
    can only mean one thing when there is no registry at all default accordingly: `name` to the
    root directory's own name, `dir` and `governance` to the root itself ('.'), and `tier` to the
    first of `[projects] governed_tiers` (there being no other project's tier for it to be
    compared against). An id range and a licence are still the project's to say, exactly as they
    would be in a registry entry that leaves them out.

    Project-scope checks apply to this one scope unconditionally (`doc_set` and `governed` are
    always true here): a single repo adopting the engine has nothing else a tier could be
    gating against. Setting `[repo] governance` to an empty string still opts the scope out of
    governance-dir-shaped checks, the same way an entry with no governance dir does."""
    entry = dict(cfg.section("repo"))
    entry.setdefault("name", cfg.root.name)
    entry.setdefault("dir", ".")
    entry.setdefault("governance", ".")
    tiers = cfg.get("projects", "governed_tiers", ["full"])
    if tiers:
        entry.setdefault("tier", tiers[0])
    keys = {k: k for k in REGISTRY_KEYS}
    problems: list[str] = []
    raw_range = dig(entry, keys["id_range"])
    rng = None
    if raw_range:
        rng = parse_range(raw_range)
        if rng is None:
            problems.append(f"registry: '{entry['name']}' id_range '{raw_range}' is not LO-HI")
    gov_rel = dig(entry, keys["governance"])
    gov = (cfg.root / gov_rel) if gov_rel else None
    scope = Scope(name=entry["name"], entry=entry, keys=keys, gov=gov,
                  id_prefix=dig(entry, keys["id_prefix"]), id_range=rng,
                  doc_set=True, governed=gov is not None)
    label = cfg.get("workspace", "label", "workspace")
    workspace = Scope(name=label, entry={}, keys={}, gov=None, id_prefix=None, id_range=None,
                      doc_set=False, governed=True, is_workspace=True)
    return Registry(path=cfg.path, data={}, scopes=[scope], workspace_prefix=None,
                    workspace_range=None, workspace=workspace, problems=problems, single=True)
