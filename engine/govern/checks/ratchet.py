"""The ratchet check. Part of the fixed core: it is what makes a warning mean something."""
from __future__ import annotations

from govern import ratchet
from govern.findings import Findings
from govern.manifest import check


@check("ratchet", scope="workspace", since="0.4.0", core=True, also_project=True,
       summary="A size breach not recorded in the baseline, or one that grew past its record, "
               "is an error.",
       question="(Not optional.) Existing breaches are recorded at adoption; new ones must be "
                "fixed or accepted on purpose.",
       rationale="A warning nobody acts on is not a softer rule; it is a rule that stopped "
                 "functioning.")
def check_ratchet(ctx, params, scope=None) -> Findings:
    f = Findings()
    try:
        baseline = ratchet.load_baseline(ctx)
    except ratchet.BaselineUnreadable as exc:
        f.error(f"ratchet: {exc} — restore it from git; nothing will overwrite it")
        return f
    current = ratchet.compute(ctx, scope)
    if scope is not None:
        # Restricted to one project (`check --project X`): X's own recorded entries only, so
        # another project's baselined breach is neither reported as new here nor offered as
        # "no longer breaches" — it simply is not this run's business.
        baseline = {k: v for k, v in baseline.items() if ratchet.owns(ctx, scope, k)}
    name = ctx.baseline_path.name
    for key, val in current.items():
        base = baseline.get(key)
        if base is None:
            f.error(f"ratchet: '{key}' is a new breach at {val}, not in {name} — "
                    f"fix it, or if it is accepted for now run "
                    f"`{ctx.prog} baseline --allow-raise`")
        elif val > base:
            f.error(f"ratchet: '{key}' grew to {val} (baseline {base}) — fix it, or "
                    f"`{ctx.prog} baseline --allow-raise` to accept the new size")
        elif val < base:
            f.warn(f"ratchet: '{key}' shrank to {val} (baseline {base}) — "
                   f"run `{ctx.prog} baseline` to lower it")
    for key, base in baseline.items():
        if key not in current:
            f.warn(f"ratchet: '{key}'={base} no longer breaches — removable via "
                   f"`{ctx.prog} baseline`")
    return f
