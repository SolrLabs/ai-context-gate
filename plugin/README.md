---
doc_type: reference
purpose: The Claude Code plugin — what it adds to a governed project and how to install it.
audience: both
load_when: changing the plugin's skills or hooks, or enabling it in a project
related: [../engine/README.md, ../docs/how-it-works.md, ../CONTRIBUTING.md]
last_reviewed: 2026-09-21
---

# context-gate plugin

The plugin adds two skills and a session notice to Claude Code. It bundles the engine at its own
version, so the skills and the hook never depend on which engine a project pins.

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
| SessionStart hook | In a governed project, tells the session and the person when a newer engine is available (the same notice the gate prints). Silent everywhere else. About 85 tokens a session |

Loading the plugin from a checkout of this repository, for development, is in
[CONTRIBUTING](../CONTRIBUTING.md).
