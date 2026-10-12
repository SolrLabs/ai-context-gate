---
doc_type: reference
purpose: The Claude Code plugin — what it adds to a governed project and how to install it.
audience: both
load_when: changing the plugin's skills or hooks, or enabling it in a project
related: [../engine/README.md, ../docs/how-it-works.md, ../CONTRIBUTING.md]
last_reviewed: 2026-10-10
---

# context-gate plugin

The plugin adds five skills, a session notice and usage hooks to Claude Code. It bundles the engine at its own
version, so the skills and the hooks never depend on which engine a project pins.

## Install

In Claude Code:

```
/plugin marketplace add SolrLabs/ai-context-gate
/plugin install context-gate@context-gate
```

A project that has adopted context-gate names the marketplace in `.claude/settings.json`, so
teammates are offered the same plugin.

## What it adds

| Component | What it does |
|---|---|
| `/context-gate:adopt` | Guided adoption: measures the project, asks only the questions measurement leaves open, then installs the gate green and explains the report — it never commits |
| `/context-gate:upgrade` | Guided upgrade: preflight (uncommitted work, commits a push would publish), `bin/upgrade`, the upgrade report explained by check, each policy choice priced in files and tokens, then commit — never an unasked push. The agent may run it when the user asks to upgrade (the notice names it); it never starts unprompted |
| `/context-gate:options` | Shows the checks that stay off until turned on (writing rules, hooks wired, checkout hygiene, licenses, usage alerts, agent roles, agent gates, agent measurement), each with its state and a suggestion, then turns the ones the user picks on, off or back to inherit and confirms each change is live. `--global` changes them in the user's profile instead |
| `/context-gate:plan` | Turns a spec into phases and units with checkable "done when" lines, routed by the project's triggers. Needs no other plugin |
| `/context-gate:build` | Runs one phase with the rendered roles on the `govern build` ledger, resumes a build in flight, carries on through later phases by default, and `--one` stops after the phase. Needs no other plugin |
| SessionStart hook | In a governed project, tells the session and the person when a newer engine is available (the same notice the gate prints). Silent everywhere else. About 85 tokens a session |
| Usage hooks (`PostToolUse`, `UserPromptSubmit`, `SessionEnd`) | Runs on every tool call in every project, but exits at once (a single file check, so no tokens; a short Python process still starts on each tool call) when the `usage` option is off. On: about 20 tokens per data line, sent every 5 points of usage (every point from 90), plus your alert text when a break point is crossed |
| Agent hooks (`PreToolUse`, `PostToolUse`, `SubagentStart`, `SubagentStop`, `UserPromptSubmit`) | Every shell call and tool call in every project with the plugin enabled starts one short Python process (about 19 ms measured on one machine), which exits at once when `agent-gates` and `agent-measure` are both off. `agent-gates` on: a role agent is launched only from a brief file, a subagent cannot stop while its brief's files are missing, and it cannot `git push`. `agent-measure` on: per-tool timing and result size inside subagents, real token totals per run, and one line to the coordinator per finished run |

Loading the plugin from a checkout of this repository, for development, is in
[CONTRIBUTING](../CONTRIBUTING.md).
