"""Overrides carry their reason: a project's format that differs from the
standard, a project's setting that differs from what its profile set, and a project's list
setting that widens past what it inherited — the standard, or its profile."""
from __future__ import annotations

from govern.config import DIALECT_DEFAULTS, LAYOUT_DIALECT
from govern.findings import Findings
from govern.manifest import check


@check("standard-overrides", scope="workspace", since="0.4.0", default="warn",
       summary="Every format setting that differs from the standard, every setting that differs "
               "from the project's profile, and every list setting widened past what it "
               "inherited, says why.",
       question="Where does this project keep a format or a setting that differs from the "
                "standard or its profile, or a list widened past what either sets, and why?",
       rationale="One standard, met or overridden on purpose: an override with no reason is a "
                 "fork nobody decided to keep.")
def standard_overrides(ctx, params) -> Findings:
    f = Findings()
    prof = ctx.cfg.profile
    pdialect = prof.settings.get("dialect", {}) if prof else {}
    raw = ctx.cfg.raw.get("dialect", {})
    reasons = {**pdialect.get("reasons", {}), **raw.get("reasons", {})}
    for key, value in ctx.cfg.dialect.items():
        if key in LAYOUT_DIALECT or value == DIALECT_DEFAULTS.get(key):
            continue
        if not str(reasons.get(key, "")).strip():
            where = "config" if key in raw else "profile"
            f.warn(f"{where}: [dialect] {key} = \"{value}\" differs from the standard "
                   f"(\"{DIALECT_DEFAULTS[key]}\") with no reason — move to the standard, or say "
                   f"why in [dialect.reasons]")
    for key, value in raw.items():
        if key != "reasons" and key in pdialect and pdialect[key] != value \
                and not str(raw.get("reasons", {}).get(key, "")).strip():
            f.warn(f"config: [dialect] {key} = \"{value}\" overrides the profile's "
                   f"\"{pdialect[key]}\" with no reason — say why in [dialect.reasons]")
    for cid, key in ctx.cfg.profile_overrides:
        f.warn(f"config: [checks.{cid}] {key} overrides the profile with no reason — follow the "
               f"profile, or add a reason")
    for cid, key, added, removed in ctx.cfg.list_overrides:
        # Only the loosening side: `looser="more"` never names what it also dropped, and
        # `looser="fewer"` never names what it also added — adding and removing in one list
        # must not read as a tightening being called loosening.
        change = "adds " + ", ".join(repr(v) for v in added) if added \
            else "drops " + ", ".join(repr(v) for v in removed)
        f.warn(f"config: [checks.{cid}] {key} {change} — loosens past what the project "
               f"inherits with no reason (say why in reasons.{key})")
    return f
