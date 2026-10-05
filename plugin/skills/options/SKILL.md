---
name: options
description: Review and change this project's context-gate options, the checks that stay off until turned on (writing rules, hooks wired, checkout hygiene, licenses, usage alerts). Shows each option's state and a suggestion, then turns options on, off or back to inherit. With --global, changes them in the user's profile instead. Use when the user asks about options, opt-in checks, or turning one on.
argument-hint: "[--global]"
allowed-tools: Bash(python3 .context-gate/bin/*), Read, Edit, AskUserQuestion
---

# context-gate options

An option is a check that is off until someone turns it on. Show what is on and what is worth
turning on, change what the user picks, and prove each change is live.

## 1. Show

Run `python3 .context-gate/bin/govern options --json` (add `--global` if `$ARGUMENTS` has it) and
show the table from `python3 .context-gate/bin/govern options`, so the user sees every option at
once.

## 2. Ask

Use AskUserQuestion with one question per option and up to four per call. Use more calls for
more than four options.

- `header`: the option id. `question`: its `summary`, then *Why:* and its `rationale`.
- Options, in this order:
  - When `suggestion` is set, the choice it argues for goes first, labeled "(Recommended)", with
    the suggestion as its description.
  - **On**: the summary, as what it will enforce.
  - **Off**: what goes unchecked.
  - **Inherit**: "Use the profile's setting (currently <state>)". Only in a project whose
    `[governance]` names a profile; never with `--global`.
  - **Chat about this**: always offer it. If the user picks it, discuss the option, then ask
    again.

After each answer, record it right away, before the next question:
`python3 .context-gate/bin/govern options --record <id>`. Never record an option that was only
shown. At `--global`, record nothing: the answers belong to the profile, not to this project.

## 3. Set up

For each option turned on whose `missing` is not empty, ask its `question`. Offer likely values as
choices (for `files`: `**/*.md`, `docs/**/*.md`, `README.md`) and take Other as typed text. A
table-shaped setting (`rules`, `hooks`) is agreed in chat, then written.

When `usage` is turned on, run `python3 .context-gate/bin/govern usage install` once per home directory
and tell the user their statusline is now wrapped and how to undo it
(`python3 .context-gate/bin/govern usage uninstall`); then offer to create
`.context-gate/usage-alerts.toml`, or say that the profile's copy applies when there is one.

## 4. Write

Project scope: edit `.context-gate/config.toml` by hand, keeping its comments and layout.

- On: `[checks.<id>]` with `level = "error"` and the settings from step 3.
- Off: `level = "off"`. If the option came from the profile (`layer` is `profile`), ask for a
  `reason` and write it.
- Inherit: remove the `level` key, and the table if it is left empty.

`--global`: ask for the path to the profile's local checkout (for example `~/src/principles`).
Never edit the engine's cached copy. In its `principles.toml`, on is `level = "error"` and off is
removing the `level` key. Only levels and rule lists go there; paths belong to each project.
Commit in that checkout with `git -C <path> commit`; that call is not pre-approved, so it asks the
user for permission each time, which is intended. Do not push or tag: tell the user the push and
the tag to cut, and that every project on the profile moves its `profile` pin to the new tag.

## 5. Prove

Run `python3 .context-gate/bin/govern explain <id>` for each change and
`python3 .context-gate/bin/govern check`. An option the user turned on must read `on` in
`govern options`, not `inert`. Report what changed, what the check now finds, and the files to
review. Never commit project files; the commit is the user's.
