---
name: build
description: Run one phase of a plan with the project's role agents - brief each unit, launch it, check and merge it, review the whole phase once, one fix pass, then close. Resumes a build already in flight. Carries on through the plan's later phases unless told `--one`. Typed by a person it starts the driver in the background; `--here`, a phase or `--one` build in this session. Use when the user asks to build a plan or a phase, or to continue a build.
argument-hint: "[plan file] [phase] [--one] [--here]"
allowed-tools: Bash(python3 .context-gate/bin/*), Bash(git:*), Bash(ls:*), Read, Write, Edit, Grep, Glob, Agent, SendMessage, AskUserQuestion
---

# Build a phase

You are the project coordinator. You brief, launch, read, merge and push. You do not write the
code. The project's build rules (`.claude/rules/context-gate-build.md`) are already in your
context and win over anything here that seems to differ.

After a summary or in a new session, trust `python3 .context-gate/bin/govern build status` and `git log` over what you
remember.

## Who builds

When this session's instructions contain a paragraph that starts `Unattended run` (the driver
appends it), or the arguments hold `--here`, a phase name or `--one`, go on to step 0. Otherwise
build nothing here and launch no role agent: run `python3 .context-gate/bin/govern build drive
<plan>` with the Bash tool's `run_in_background`, adding `--project <name>` in a workspace and `--cap <n>` only when the person named one. With no
plan argument, take the plan from the first line of `python3 .context-gate/bin/govern build status`
(`plan <file>`); if it names none, ask which. Tell the person the run folder it prints. When it
exits, report its last lines (the `stop:` reason) and what the owner must do.

## 0. Is a build already in flight?

Run `python3 .context-gate/bin/govern build status --here`. If it names a phase, do not start over: go to
the step its **Next** line names. Otherwise start at 1. In a `--here` build, a **Next** line that begins "Start the driver" is followed by the step in its parentheses, not by starting the driver. If it is not recognized,
the engine is older than this skill: stop and say the project needs a release or beta with
`govern build`.

## 1. Before the first unit

1. `python3 .context-gate/bin/govern agents render --check`. If it fails, the role definitions are
   missing or stale: stop and say so. Check that each role this phase will launch is
   among the agent types this session can launch. If one is missing (roles rendered after this session began), stop and ask for a new
   session before you cut the phase branch.
2. `git status --short` must be empty. Never stash, revert or commit someone else's work: stop and ask.
3. Read the plan (`$ARGUMENTS`, or ask which). Pick the phase: the one named, else the first not
   done. Read that phase in full and the plan's constraints. If there is no plan and the work is
   one unit, say so and treat the request as a one-unit phase; for more, use this plugin's `plan` skill.
   When the plan has an `## Agents` section whose `Reviewed:` line is not filled in, stop and ask
   the owner to review it.
4. Cut the phase branch from the up-to-date default branch: `git switch -c phase/<slug>`. Never
   build on the default branch.
5. Run the project's local suite once on the fresh branch. If it is red before you start, stop and
   say so: you cannot tell your breakage from what was already there.
   A gate that looks slow is timed twice before anyone calls it slow.
6. `python3 .context-gate/bin/govern build start <slug> --plan <file>` (add `--high-risk` when the
   plan marks the phase so). To have `qa-engineer` on a phase that touches no high-risk path, start it with
   `--high-risk` all the same. Add `--visual` when the phase touches a path the build rules list as
   visual. Add `--from <branch>` whenever the phase branch was not cut from the default branch.
7. Scan the phase's units against each other: any two that change the same file or interface run
   in order, not side by side. Where the plan is silent or two units disagree, decide and record it: `python3 .context-gate/bin/govern build ruling "<what you decided> - <why> - <what it costs
   if wrong>"`.
8. Tell the user in three or four lines what you are about to build and in what order. Do not wait for an answer. A table of units has an Agent column, `role (model)`, e.g. `senior-developer (opus)`.

## 2. Each unit

1. `python3 .context-gate/bin/govern build unit add <id> --title "<title>"` (add
   `--role senior-developer` only for a trigger in the build rules; add `--red` for a bug fix). It
   writes the brief's skeleton.
2. Fill the brief from the plan: the task, the exact files, the tests and their command, what it
   uses from earlier units by exact name, an implementation to follow, what is out of scope. One
   concern, five items at most. Everything the agent needs is in this file; it reads no
   project instructions and knows nothing of this conversation. Confirm each file the brief names
   to follow has the function or test. An agent's worktree has no
   `.context-gate/local.toml`, so `govern` there runs the project's committed pin, not a beta:
   never brief an agent to run `govern agents render` or a command only the beta has. Leave the
   last lines (`Produces:`, `Red:`) as the command wrote them. A brief asks for a test "written
   first and seen failing" only for a unit added with `--red`.
   Before the brief is committed, read `checklists.md` beside this file and check the brief
   against it. Units side by side share no host port: a brief gives a server unit its own.
3. Commit the ledger and the brief on the phase branch.
4. `python3 .context-gate/bin/govern build launch <id> --here`. It checks the brief is filled in and
   committed, marks the unit running, and prints the launch prompt: a `Brief:` line and a `Base:`
   line. Say which agent, model and effort you launch, then launch it in a worktree of its
   own with exactly those lines as the whole prompt.
   Units that share no files may be launched together, in one message. Never add work to a running
   agent or switch the checkout's branch. After an outage, confirm with
   `ListAgents` before relaunching. Remove a worktree only after its last hand-back, never with `--force`.
5. When it hands back, read its first line.
   - `DONE` or `DONE_WITH_CONCERNS`: read its report file, then the diff itself
     (`git diff <base>..<its branch>`, where the base is the `Base:` you launched it with, never
     `HEAD~1`).
     Check it did what the brief says, nothing else. Run
     `python3 .context-gate/bin/govern build verify <id> --branch <b>`. Merge its branch with
     the phase branch checked out (move any other branch with `git branch -f`). Follow the
     build rules' gate bullet. Per-merge bullet: run
     `python3 .context-gate/bin/govern build unit set <id> done --branch <b>`; if it refuses as
     stale, run `build gate`. Once-per-phase bullet: run nothing now (step 3.1). With no bullet,
     the agent ran the suite: read its result. A long gate runs with `run_in_background`; wait on a Monitor
     until-loop on `gate/last.json`, never `| tail`. Green: record it with
     `python3 .context-gate/bin/govern build unit set <id> done --branch <b> --agent <agent id>`.
     Red: the unit you just merged is failed; leave the merge in place, launch nothing else from the phase branch
     until it is green again, and go to the failure bullet.
   - `BLOCKED` or `NEEDS_CONTEXT`: `... unit set <id> blocked --note "<why>"`. Decide what it
     needs. If the brief was wrong, fix the brief and launch a **fresh** agent. If it stopped on a
     `senior-developer` trigger, or the developer has now failed it as many times as the build
     rules allow, hand it up: `... unit set <id> pending --role senior-developer` (the brief then
     promises a report of its own, so the earlier one is kept), add the earlier report's file name
     to the brief, and launch `senior-developer`. Never resume the old agent. A fresh agent starts on the current Base. Never merge it while that would leave the phase branch red.
   - The gate is red after its merge, or the diff is not what the brief asked: `... unit set <id>
     failed --note "<what>"`, then a fresh agent with a brief that says exactly what is wrong.
     The fresh agent's brief says which tests fail and names the earlier attempt's branch, read
     only for what it needs. A gate that is red for a reason outside the unit's diff and green when run again is
     not the unit's failure: record it with `build ruling` and go on.
   - A line telling you a contract was not met means the report or the failing-test output the
     brief promised does not exist. Treat the unit as failed.
6. Commit the ledger (never `git add -f` the build folder), and push the phase branch
   (`git push -u <remote> phase/<slug>` the first time).
7. Next unit. `govern build status` always says which.

## 3. Review, once

When every unit is `done`:

1. Run `python3 .context-gate/bin/govern build gate` on the phase branch.
2. `python3 .context-gate/bin/govern build review`. It writes the review package and the
   reviewers' briefs and prints the launch lines. For a high-risk phase, fill the definition of
   done into the `qa-engineer` brief from the plan's "Done when" list. Commit the ledger and the
   briefs.
3. `python3 .context-gate/bin/govern build launch review --here` prints a launch prompt for
   `code-reviewer`, and one for `qa-engineer` when the phase is high-risk. Launch them in one
   message, without a worktree: they change nothing. Tell them nothing about
   what you expect them to find.
4. Read both reports. Rule on each finding: it stands, or not and why. A finding that
   contradicts a locked decision or the plan is yours to rule on, not the fixer's.

## 4. One fix pass

If nothing stands, skip to 5.

1. `python3 .context-gate/bin/govern build fix` (`--unit <id>` for one unit), then fill its brief: the findings that stand,
   most severe first, five at most per agent, each with its file and line and the input that shows
   it, each fixed test first. Add one line for each finding you ruled out and why, so the fixer
   does not reopen it. More than five findings: run `build fix` again once the first brief is
   filled; it writes a second brief, for a second fresh agent, on files the first does not touch.
   A brief whose findings meet a `senior-developer` trigger is made with
   `build fix --role senior-developer`. Commit the ledger and the briefs.
2. `python3 .context-gate/bin/govern build launch fix --here` prints a launch prompt for each fix brief.
   Launch a fresh agent for each in its own worktree. Check and merge its work as in
   step 2.5, then `python3 .context-gate/bin/govern build collect <its branch>` to bring its
   report into this checkout.
3. The fix is not reviewed again. The gate and your own reading of the fix diff close it. The one
   exception: a fix that touched a high-risk path gets `qa-engineer`'s failed probes re-run, by a
   brief that lists only those probes. A finding made on a running system is proved fixed on a
   running system, with the reviewer's own steps.

## 5. Close

1. The owner's turn, when the plan's "Done when" has a line for them: launch an isolated copy of
   the build if the project says how. Before a command that removes data, confirm which
   copy it points at; never run one from a worktree against the owner's own. Confirm the change is there, then tell the
   owner it is ready and what to look at, with what right and wrong look like. Do not hand them a
   command to work out. Wait for their word before closing. Changes the owner asks for there are a fix pass:
   run `python3 .context-gate/bin/govern build fix` again, which works after `build close`. With
   no such line, do not stop for the owner: tick the lines on your own evidence and go on. When the owner passes a plan's last
   phase, delete the plan file, fix the pointers to it, and say so.
2. Tick the plan's "Done when" lines against evidence you saw in this session, never against a
   report alone. A line you could not show stays open, marked `(unverified - needs <what>)`.
3. `python3 .context-gate/bin/govern build close`. Fill in what its brief leaves for you (what
   landed, what was decided, what is unverified), commit, then run
   `python3 .context-gate/bin/govern build launch close --here` and launch `technical-writer` in its own
   worktree with the prompt it prints. Merge its branch. Run the two commands `build close` printed: they remove
   the phase's folder and commit that.
4. If the build rules order the run report, run it now, before the phase is closed: it reads more
   of the phase's runs than it can afterwards. Pass its suggestions on. Apply none.
5. `python3 .context-gate/bin/govern phase close`. It pushes the phase branch, confirms the remote
   holds it, and removes the branches and worktrees this phase merged. It opens no pull request
   and merges nothing into the default branch: that is the owner's, or the project's rule.
   When the session is ending, run the `housekeeping` skill now.
6. Report: what landed, the gate line, what is unverified and what it needs, each ruling
   with what it costs if wrong, what the owner owes (the exact command, link or decision), and
   what the reviews found and what you did about each, and any fault in context-gate itself, with
   the draft the `report` skill made.

## In a workspace

When the config has a `[registry]`, every `govern build` command takes `--project <name>`. Cut and
switch the phase branch with `git -C <member folder>`, and start the phase `--from` the member's
integration branch. Launch each agent with the lines
`build launch` prints, `Worktree:` included, without isolation: the worktree is already made.
A close prints two writer launches; merge the second's branch in the workspace repository.
The gate is the member's own (its registry entry's `runtime_gate`, or what the plan names). Close
with `phase close --path <member folder>`. No registry: ignore this section.

## Carrying on

This session carries on through later phases; `--one` stops when this phase is closed.
Everything above holds:

- **Finishing a phase is not a reason to stop.** Close it (step 5; stop at 5.1 only when the phase's
  "Done when" has a line for the owner), cut the next phase's branch from the phase you
  closed, start it with `build start <slug> --plan <file> --from phase/<the one you closed>`
  so its review holds only its work, and go on.
- **Checkpoint constantly.** The ledger commit and the push after every unit are what a crash
  leaves you. Have `technical-writer` refresh the handoff at every phase boundary, never "at the
  end": the end is the one thing a crash reliably removes.
- **Route around what is blocked.** Build what does not depend on it, mark the rest
  `(unverified - needs <what>)`, write down exactly what a person must do, and move on. Do not
  fake what is missing or retry it.
- **Decide, and record it.** An answerable question is a ruling, not a stop.
- **Heed the usage alerts** if the project has them: read context and usage from the newest
  `~/.local/state/context-gate/usage/<session>.json`. Stop starting units when your context is about two thirds full; close the
  phase and hand over.

**Stop, and say why, only when:**

1. The plan is done, or everything left waits on a person.
2. Going on would contradict a locked decision, or move a phase boundary the plan set.
3. The same approach has failed three times for the same reason.
4. The only way forward is destructive or cannot be undone: a forced push, rewriting history,
   deleting what the project did not create, publishing, sending something to a third party,
   spending money.
5. A process will not die or the machine is acting damaged. Write down every command you ran on it
   and what each did, in the ledger (`build ruling`) and the handoff, and push, before anything
   else. Never ask for a restart before that record is off the machine.

**Not reasons to stop:** a failing test, a finished phase, a blocked item you can route around, a
design question you can answer, uncertainty about something you can undo.

When you stop, give the report from step 5.6 for everything since you started.
