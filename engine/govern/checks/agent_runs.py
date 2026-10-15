"""Agent gates and agent measurement: two opt-in switches. The work happens in the plugin's hooks
(govern.agent_runs); these checks only carry the on/off switches and their params through the
options cascade."""
from __future__ import annotations

from govern.agent_runs import GATE_DEFAULTS, MEASURE_DEFAULTS
from govern.findings import Findings
from govern.manifest import Param, check


@check("agent-gates", scope="workspace", since="0.7.0", default="off",
       summary="Refuses launching a briefed role without a brief file, ends a subagent's run only "
               "when the files its brief promised exist, and keeps subagents from running "
               "`git push`.",
       question="Should a subagent be held to its brief: launched only with a brief file, and "
                "stopped from finishing until the files it promised are written?",
       rationale="A coordinator drops contract lines when it retypes them into a prompt, and a "
                 "subagent reports files it never wrote; a gate that refuses a few times, then "
                 "lets go and says so, catches both without trapping the agent.",
       params={
           "briefed": Param("list", GATE_DEFAULTS["briefed"],
                            "Agent types that may only be launched with a `Brief:` file",
                            looser="fewer"),
           "claims": Param("bool", GATE_DEFAULTS["claims"],
                           "Refuse a final message that points at a file that does not exist"),
           "checklist": Param("bool", GATE_DEFAULTS["checklist"],
                              "Refuse a report that leaves a line of the role's hand-back "
                              "checklist unanswered"),
           "deny_push": Param("bool", GATE_DEFAULTS["deny_push"],
                              "Deny a subagent's `git push`"),
           "max_refusals": Param("int", GATE_DEFAULTS["max_refusals"],
                                 "Refusals in one run before the agent is let go and the unmet "
                                 "items go to the coordinator", looser="lower"),
       })
def agent_gates(ctx, params) -> Findings:
    return Findings()


@check("agent-measure", scope="workspace", since="0.7.0", default="off",
       summary="Records what each subagent run cost (tokens summed over its requests, tool "
               "calls and time, largest results) and tells the coordinator in one line per run; "
               "`govern runs report` summarizes a phase.",
       question="Should each subagent run be measured, with one line per finished run for the "
                "coordinator and a report of where a phase's time and context went?",
       rationale="The token figure in a completion notice is one request's context, not the "
                 "run's cost; real totals are the only way to see which roles and habits spend "
                 "the most.",
       params={
           "notice": Param("bool", MEASURE_DEFAULTS["notice"],
                           "Send the coordinator one line per finished run"),
       })
def agent_measure(ctx, params) -> Findings:
    return Findings()
