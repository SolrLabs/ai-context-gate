"""The usage option: the orchestrating agent is told its context, 5-hour and weekly usage, and
the owner's pre-set prompts at break points. The work happens in the plugin's hooks; this check
only carries the on/off switch through the options cascade. See docs/configuration.md ("Usage alerts")."""
from __future__ import annotations

from govern.findings import Findings
from govern.manifest import check


@check("usage", scope="workspace", since="0.6.0", default="off",
       summary="Tells the orchestrating agent its context, 5-hour and weekly usage as they rise, "
               "and injects the owner's prompts from usage-alerts.toml at break points. Needs "
               "`govern usage install` run once per machine to capture the data.",
       question="Should the orchestrating agent be told its context and plan usage, and get your "
               "own instructions at break points you set (say, start a powerdown at 93% weekly)?",
       rationale="A long run that hits a usage limit stops mid-step; an agent that sees the limit "
                "coming can stop cleanly, commit and hand off.")
def usage(ctx, params) -> Findings:
    return Findings()
