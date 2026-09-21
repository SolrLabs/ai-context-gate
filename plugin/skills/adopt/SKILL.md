---
name: adopt
description: Adopt context-gate in a project that isn't governed yet. Measure the repo, propose its policy, ask the few questions measurement can't settle, then install it green. Use when the user asks to adopt, install or set up governance here. Never start it unprompted.
argument-hint: "[--profile <source>] [--marketplace <owner/repo>]"
allowed-tools: Bash(git status:*), Bash(git log:*), Bash(ls:*), Read, Write, Grep, Glob, AskUserQuestion
---

# Adopt context-gate

Put this project under governance in one pass. The engine measures, proposes and installs; your job
is to show the proposal plainly, ask only the questions it returns, and leave the user a working
tree to review. Never write the config by hand, never reimplement what the engine does, and never
commit.

Arguments: `$ARGUMENTS` (for example `--profile git@github.com:org/principles.git#v1`).

`--source` is not needed: `[governance] source`, where a fresh clone or a CI runner fetches the
engine, defaults to the public repository (`https://github.com/SolrLabs/ai-context-gate.git`).
If the user installed the plugin from a fork, pass `--marketplace <owner/repo>` (the fork's GitHub
repository): teammates are then pointed at the fork's marketplace, and the source defaults to the
fork too. `--source none` leaves the source unset.

## 1. Preflight

Run these and report them in two or three lines:

- `ls .context-gate 2>/dev/null`: if it exists, stop. This project is already governed; use
  the upgrade skill instead.
- `git status --short`: uncommitted work. Adopt refuses to touch a dirty file, and other sessions
  may have work in progress here. Never stage, commit, stash or revert anything.
- **Find the engine.** Use the newest release in `~/.local/share/context-gate/engines/`
  (`ls` it). If there is none, use the copy bundled with this plugin: the plugin's root directory
  (two levels above this skill file) holds `govern/`. Call it `<engine>` below. Every engine command
  runs from the project root as:

  ```
  PYTHONPATH=<engine> python3 -P -m govern.installer <command> --root . …
  ```

Ask the user which profile to use if `$ARGUMENTS` names none and they haven't said. A profile is
optional: without one the project runs the engine's standard alone.

## 2. Measure and propose

```
PYTHONPATH=<engine> python3 -P -m govern.installer adopt --root . --json [--profile P] [--marketplace M]
```

Exit 0 means the proposal is complete; exit 3 means it has questions; exit 2 is a refusal (show the
message and stop). Read the JSON and show the user a short table:

| What | Proposed |
|---|---|
| Shape | single repo, or a workspace and which repos |
| Decision log(s) | path per scope, and any the adopt will create |
| Working files, traps | paths and globs |
| Governed docs | the globs |
| Format conversion | whether logs or traps in another format will be converted |

Then the `notes`, briefly: what was inferred and from where.

## 3. Ask

Ask each entry in `questions` with AskUserQuestion: its `text`, its `options` in order (the first
is the recommendation; label it so), and its `why` as the option descriptions allow. Two special
cases:

- `shape`: single repo or workspace.
- `repos`: which repos to govern. `options[0]` is the recommended selection as a comma list;
  `options[1:]` are the candidate names. With 4 candidates or fewer, ask it as a multiSelect with
  the recommended ones marked. With more, show a numbered list with the recommended ones marked,
  and take the answer as typed text (names or numbers); AskUserQuestion shows at most 4 options.
  The answer written back is a comma list of names.

Write the answers to a scratch file as TOML, one `key = "option"` per line (a `repos` answer is a
comma-separated list of names), and rerun step 2 with `--answers <file>`. Answering can open
follow-up questions; repeat until the exit code is 0.

## 4. Apply

Say what is about to happen: `.context-gate/` is created, the plugin (`context-gate@context-gate`)
and its marketplace are enabled in `.claude/settings.json`, the engine is installed in
`~/.local/share/context-gate/engines/` if that version is not there yet, and, if the proposal says
so, logs and traps in another format are converted and new decision logs created. Then:

```
PYTHONPATH=<engine> python3 -P -m govern.installer adopt --root . --apply --answers <file> [--profile P] [--marketplace M]
```

It installs, converts, rebuilds the indexes, records today's breaches in the baseline, runs the
gate and writes `.context-gate/adopt-report.md`. It never commits.

## 5. Hand back

Read `.context-gate/adopt-report.md` and tell the user, briefly:

- the gate result. If it is red, the report's "Needs a person before this is green" section lists
  existing content the baseline can't absorb (a bad agent frontmatter, an unknown registry tier):
  walk the user through each fix it names. A red finding outside that section is an engine defect
  to report, not something to patch by hand
- what migrate left for a person (entries it could not convert)
- how many breaches were baselined, and that each is a known debt, not a pass
- the files to review and commit (`git status --short`)
- once the settings are committed, each teammate installs the plugin once, in Claude Code:
  `/plugin marketplace add SolrLabs/ai-context-gate` (or the `--marketplace` fork), then
  `/plugin install context-gate@context-gate`. The gate itself needs nothing installed: a fresh
  clone or CI runner fetches the engine from `[governance] source`

The commit is the user's. If they ask you to commit, commit only the files the adopt created or
changed, and say what else a push would publish before any push.
