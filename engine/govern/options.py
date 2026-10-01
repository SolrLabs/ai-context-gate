"""Options: the checks that are off by default, which a project or its profile turns on by
choice. `govern options`, the upgrade report and adopt all read `collect`; the skills parse its
JSON, so `FIELDS` is a contract."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from govern import config, layout, manifest


@dataclass
class Option:
    id: str
    summary: str
    rationale: str
    question: str
    since: str
    state: str                # "on" | "off" | "inert" (on, but a param it needs is empty)
    level: str                # the effective level
    layer: str                # where the level comes from: "engine" | "profile" | "project" | "local"
    inherit: bool             # the project sets no level of its own
    new: bool                 # not yet answered in this project
    suggestion: str | None
    needs: list[str]
    missing: list[str]


FIELDS = tuple(Option.__dataclass_fields__)


def ids() -> list[str]:
    return [cid for cid, c in manifest.CHECKS.items() if c.default == "off" and not c.core]


def answered(root: Path) -> set[str] | None:
    """The options this project has answered, or None when nothing is installed here. An id
    recorded under an old name (`config.RENAMED`) counts under its new one."""
    path = root / layout.MANIFEST
    if not path.is_file():
        return None
    with path.open("rb") as fh:
        return {config.check_id(c) for c in tomllib.load(fh).get("options_answered", [])}


def _empty(value) -> bool:
    return value in ([], {}, "", None)


def _option(chk, level: str, layer: str, inherit: bool, new: bool, missing: list[str],
            suggestion: str | None) -> Option:
    state = "off" if level == "off" else "inert" if missing else "on"
    return Option(chk.id, chk.summary, chk.rationale, chk.question, chk.since, state, level,
                  layer, inherit, new, suggestion, list(chk.needs), missing)


def collect(ctx, done: set[str] | None = None) -> list[Option]:
    """Every option as this project resolves it. `done` is what `answered` returned."""
    done = done or set()
    raw = ctx.cfg.raw.get("checks", {})
    out = []
    for cid in ids():
        chk, s = manifest.CHECKS[cid], ctx.cfg.checks[cid]
        missing = [n for n in chk.needs if _empty(s.params[n])]
        opt = _option(chk, s.level, s.source.get("level", "engine"),
                      "level" not in raw.get(cid, {}), cid not in done, missing, None)
        if chk.suggest is not None and opt.state != "on":
            # A suggestion is a hint, and an extension's `suggest` is the project's own code:
            # one that fails (a registry fact of a shape it did not expect, say) offers none
            # rather than taking down adopt, `options` or an upgrade.
            try:
                opt.suggestion = chk.suggest(ctx, s)
            except Exception:  # noqa: BLE001
                opt.suggestion = None
        out.append(opt)
    return out


def collect_global(ctx) -> list[Option]:
    """The profile's layer alone: on or off per option, as the profile sets it."""
    if ctx.cfg.profile is None:
        raise ValueError("no profile: [governance] profile names none, so there is no global "
                         "layer to show")
    tables = ctx.cfg.profile.settings.get("checks", {})
    out = []
    for cid in ids():
        chk, t = manifest.CHECKS[cid], tables.get(cid, {})
        level = t.get("level", chk.default)
        out.append(_option(chk, level, "profile" if "level" in t else "engine", False, False,
                           [], None))
    return out


def record(root: Path, cids: list[str]) -> None:
    """Add answered options to `installed.toml`. Called once per answer, never for a panel only
    shown, so a person who quits partway is offered the rest again."""
    from govern import installer
    unknown = [c for c in cids if c not in ids()]
    if unknown:
        raise ValueError(f"not an option: {', '.join(unknown)} (options: {', '.join(ids())})")
    data = installer.read_manifest(root)
    data["options_answered"] = sorted(set(data.get("options_answered", [])) | set(cids))
    installer.write_manifest(root / layout.MANIFEST, data)


def table(opts: list[Option], global_: bool = False) -> str:
    rows = [("option", "state", "since", "new", "suggestion")]
    for o in opts:
        note = o.suggestion or (f"needs {', '.join(o.missing)}" if o.state == "inert" else "")
        rows.append((o.id, f"{o.state} ({o.layer})", o.since,
                     "yes" if o.new and not global_ else "", note))
    widths = [max(len(r[i]) for r in rows) for i in range(4)]
    return "\n".join(("  ".join(c.ljust(w) for c, w in zip(r[:4], widths)) + "  " + r[4]).rstrip()
                     for r in rows)
