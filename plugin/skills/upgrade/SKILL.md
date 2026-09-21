---
name: upgrade
description: Upgrade this project's context-gate to a newer engine and walk through what changed. Use when the user asks to upgrade or update governance, or agrees to after the "context-gate X available" notice. Never start it unprompted.
argument-hint: "[version]"
allowed-tools: Bash(git status:*), Bash(git log:*), Bash(git rev-list:*), Bash(git fetch:*), Bash(python3 .context-gate/bin/*), Read, Grep, Glob
---

# Upgrade context-gate

Move this project to a newer governance engine, then help the user act on what changed. The
engine does the upgrade; your job is to make it safe, explain the report, and make each policy
choice with the user, in the open. Never reimplement what the engine does.

Target version: `$ARGUMENTS` if given, otherwise the newest release.

## 1. Preflight: know what is at stake before touching anything

Run these and report them to the user in two or three lines:

- `git status --short`: uncommitted work. Other sessions may have work in progress here. Never
  stage, commit, stash or revert anything that is not part of this upgrade.
- `git fetch --quiet` then `git rev-list --left-right --count @{upstream}...HEAD`: commits
  behind and ahead of the remote. **If the branch is ahead, say how many commits and whose they
  are (`git log @{upstream}..HEAD --format='%h %an %s'`) before any push is even discussed.** A
  push publishes all of them, not just yours.
- `python3 .context-gate/bin/govern check`: the gate before the upgrade. If it has
  errors, say so: the upgrade report compares against this state.

Stop and ask if `.context-gate/` does not exist: this project is not governed yet, and
installing is a different job.

## 2. Upgrade

From the project root:

```
python3 .context-gate/bin/upgrade            # newest release
python3 .context-gate/bin/upgrade --to X.Y.Z  # a specific one
```

The upgrade pins `config.toml` to the new engine, refreshes `.context-gate/bin/`, and
writes `.context-gate/upgrade-report.md`. If it reports "rolled back", nothing changed:
show the user the error and stop.

## 3. Explain the report

Read `.context-gate/upgrade-report.md`. Tell the user, briefly:

- **What changed in the engine**: the release notes the report quotes for every version the
  upgrade brings. Lead with anything under **Upgrading**: those are actions or decisions for
  this project.
- **New findings, grouped by check.** For each check, one line of what it enforces and why (the
  report carries its summary and rationale), then its findings. Say plainly which are real
  problems the old engine missed and which come from a rule the old engine did not have.
- **Findings no longer reported**, if any, and why that is expected (a changed message, a fixed
  false positive) or suspicious.
- **Settings that differ from the defaults**: only mention one if the new engine changed its
  meaning or its default.

For any check the user asks about: `python3 .context-gate/bin/govern explain <check>`.

## 4. Decide policy with the user, one choice at a time

For each new check, and each finding that implies a policy choice, ask the user, recommending
one answer. Options are usually: fix the findings, adjust the setting in
`.context-gate/config.toml` (a loosening needs a `reason`), turn the check off
(`level = "off"`, with a reason), or accept current breaches into the ratchet
(`bin/govern baseline --allow-raise`).

**Price every choice that changes the project before taking it.** Count what it touches from the
findings or with Grep, and say it: "Fixing these touches 18 entries in 1 file", or "Tightening
this limit to N touches 1,308 files and will take a substantial amount of tokens. Continue?". A
choice that only applies to future work (the ratchet keeps existing breaches recorded) is
"stricter from now on", and say so, so the user can tell it from "rewrite the codebase".

Edit `config.toml` only with the user's agreement, then run `bin/govern check` to confirm it
loads and to show the effect.

## 5. Finish

- Run `python3 .context-gate/bin/govern check` and report the result against the preflight.
- Offer to commit exactly the upgrade (`.context-gate/`) and any changes the user agreed
  to, following the project's own commit conventions. Stage paths explicitly; never `git add -A`.
- If `.claude/settings.json` has an uncommitted `enabledPlugins` entry for `context-gate`, or
  an uncommitted `extraKnownMarketplaces` entry for its marketplace, say so: until it is
  committed, the plugin is enabled on this machine only. Ask whether to include it. If
  `.context-gate/installed.toml` has no `[[plugin]]` entry (the plugin was enabled by hand),
  suggest recording it so uninstall can reverse it:
  `PYTHONPATH=~/.local/share/context-gate/engines/<pinned version> python3 -P -m govern.installer enable-plugin --root . --id context-gate@context-gate`
  (add `--marketplace <owner/repo>` when the plugin comes from a fork; remove the hand-added
  lines first, so the install records that they were absent before).
- Never push without the user saying so, and only after repeating what the push will publish.
