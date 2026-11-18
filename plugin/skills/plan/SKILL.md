---
name: plan
description: Turn a spec or a request into a build plan the build skill can run - phases with a checkable definition of done, units role agents can take, each routed by the project's own triggers. Use when the user asks to plan work for agents, or before /context-gate:build when no plan exists.
argument-hint: "[spec file or a sentence describing the work]"
allowed-tools: Bash(python3 .context-gate/bin/*), Bash(git status:*), Bash(git log:*), Bash(git diff:*), Bash(ls:*), Read, Write, Edit, Grep, Glob, Agent, AskUserQuestion
---

# Plan a build

Write the plan a coordinator can run without deciding anything new midway. A plan
that leaves a design choice open comes back as a guess, and a guess costs a review round.

You write the plan yourself.

## 1. What you are planning from

`$ARGUMENTS` names a spec file or describes the work. Read the spec in full. If there is no spec
and the work is more than one unit, say what you understand it to be and ask
the user to confirm it. Ask only what the spec, the code or the decisions cannot settle.

Read the project's build rules (`.claude/rules/context-gate-build.md`) for the roles this project
has and the triggers that route work to `senior-developer`. If the file is missing, the
`agent-roles` option is off: say so, and offer this plugin's `options` skill.

Read the project's decisions by index and open only the entries the work touches. Read the code the
work will change, its tests, and any implementation a unit could follow.

Before you plan a new check or command, search what exists: `python3 .context-gate/bin/govern
explain` lists every check and its settings. Say in the plan's Constraints what you found.

## 2. Cut it

**Phases.** A phase is everything up to the next owner checkpoint: all of it is built, then
reviewed once as one diff, fixed once and closed once, and it ends in something a person can check.

- Mark the owner checkpoints: a phase is one when a large body of work becomes usable at its end,
  so the owner tries it before more is built on it. The plan's last phase is always one. A phase a command can judge is not.
- Only a migration, or security work that later units build on, is a small phase reviewed
  early: every phase pays for a review, a fix pass and a close.
- Mark a phase **high-risk** when it touches one of the project's high-risk paths.

**Units.** A unit is the smallest piece that carries its own tests and that one agent can finish
in one run.

- Fold setup and docs into the unit that needs them.
- Mark a unit **risky** when it holds a state machine or run loop, cancel, disable or timeout
  interplay, a held resource, or more than one exit path.
- Size each unit: one holding more than one operation a user can call, or expected to add more than
  about 500 lines with its tests, is split into units run in series or side by side. Each tool call
  is re-read on every later turn, so one long run costs more than fresh ones.
- Units that share no files may run side by side: say which.
- A bug fix is marked **red-first**: the developer saves the failing test's output before the fix.

**Routing.** Every unit goes to `developer` unless it meets one of the triggers in the build
rules. Name the trigger when you route to `senior-developer`. Do not route by how hard a unit
feels. A `senior-developer` unit holds only the work that sends it there; the rest is a `developer` unit.

## 3. Write it

Save the plan where the project keeps working documents, with the frontmatter of its other
working documents. Keep it under the project's word limit for a working file; a
plan that will not fit is two plans. Run the project's `python3 .context-gate/bin/govern check`
before the plan is committed, since a new working file can break a document limit. Read
`checklists.md`, beside this file, and apply each line to the plan.

```markdown
# Plan: <name>

Spec: <path, or "none: agreed in chat on <date>">
Goal: <one sentence>

## Constraints
<exact values from the spec that every unit must respect, one line each>

## Agents
| Step | Agent | Model | Effort | Why this one |
|---|---|---|---|---|
| <the units, each review role, the fix pass, the close: one row per kind of run> | | | | |

Reviewed: <who and when, or "(not yet)">

## Phase 1: <name>          (slug: <p1-name>; high-risk: yes|no; checkpoint: yes|no)

Done when:
- [ ] <something a command or a person can show>
- [ ] The owner has tried <what>, by <how to launch it>. (a checkpoint phase only)

### u1: <title>            (developer; red-first: no; risky: no)
- Does: <what changes, in two or three lines>
- Files: <exact paths to create or change, and the test file>
- Uses: <what it consumes from earlier units, with exact names>
- Gives: <what later units rely on from it, with exact names>
- Follow: <an existing implementation to copy, or "none">
- Tests: <what each test shows, and the command that runs them>

### u2: ...                (senior-developer: <the trigger>; red-first: no; risky: no)
```

No "to be decided", no "handle errors appropriately", no "like u1": say it.

## 4. Check it before you hand it over

Do this in order and fix what you find:

1. **Coverage.** Every requirement in the spec lands in a unit. List any you left out and why.
2. **Conflicts.** Take the units in pairs: two that change the same file or the same interface are
   either ordered or merged. Write the order down.
3. **Names.** Anything one unit gives and another uses has the same name in both.
4. **Done.** Each "Done when" line can be shown by a command or by the owner looking.
5. **Routing.** Each `senior-developer` unit names a real trigger; nothing else is routed there.

If the plan has more than one phase or a risky unit, launch `architect`: write a brief for it with
`Produces:` on its last line, launch it with `Brief: <path>`, and rule on what it finds. Say which
findings you did not accept and why. When `architect` is not rendered, offer `extra_roles` to turn
it on.

## 5. Hand over

Tell the user where the plan is, each phase's unit count, what goes to `senior-developer` and why,
and what you decided that they might decide differently.

The owner reviews the `## Agents` table before any build; fill its `Reviewed:` line only on the
owner's word.

Planning is a phase of its own. Before the build starts, commit the plan on a branch, push it, update the project's handoff and
merge it the way the project merges: the build needs a clean tree.

The build starts with this plugin's `build` skill and the plan file. Never start it yourself.
