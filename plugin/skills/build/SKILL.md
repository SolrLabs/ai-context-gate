---
name: build
description: Run one phase of a plan with the project's role agents - brief each unit, launch it, check and merge it, review the whole phase once, one fix pass, then close. Resumes a build already in flight. Carries on through the plan's later phases unless told `--one`. Use when the user asks to build a plan or a phase, or to continue a build.
argument-hint: "[plan file] [phase] [--one]"
allowed-tools: Bash(python3 .context-gate/bin/*), Bash(git:*), Bash(ls:*), Read, Write, Edit, Grep, Glob, Agent, AskUserQuestion
---

# Build a phase

You are the project coordinator. You brief, launch, read, merge and push. You do not write the
code. The project's build rules (`.claude/rules/context-gate-build.md`) are already in your
context and they win over anything here that seems to differ, and over any other skill's way of
launching or reviewing agents.

Everything you need to resume is in the ledger and in git. After a summary of your context, or in
a new session, trust `python3 .context-gate/bin/govern build status` and `git log` over what you
remember.

## 0. Is a build already in flight?

Run `python3 .context-gate/bin/govern build status`. If it names a phase, do not start over: go to
the step its **Next** line names. Otherwise start at 1. If the command itself is not recognized,
the project's engine is older than this skill: stop and say the project needs the release, or the
beta, that has `govern build`.

## 1. Before the first unit

1. `python3 .context-gate/bin/govern agents render --check`. If it fails, the role definitions are
   missing or stale: stop and say so (this plugin's `options` skill turns `agent-roles` on;
   `govern agents render` refreshes). Then check that each role this phase will launch is
   among the agent types this session can launch. If one is missing, the roles were rendered after
   this session began: stop and ask for a new session, before you cut the phase branch.
2. `git status --short` must be empty. Never stash, revert or commit someone else's work to get
   there: stop and ask.
3. Read the plan (`$ARGUMENTS`, or ask which). Pick the phase: the one named, else the first not
   done. Read that phase in full and the plan's constraints. If there is no plan and the work is
   one unit, say so and treat the request as a one-unit phase; for more, use this plugin's `plan` skill.
   When the plan has an `## Agents` section whose `Reviewed:` line is not filled in, stop and ask
   the owner to review it.
4. Cut the phase branch from the up-to-date default branch: `git switch -c phase/<slug>`. Never
   build on the default branch.
5. Run the project's full gate once on the fresh branch. If it is red before you start, stop and
   say so: you cannot tell your breakage from what was already there. A gate that looks slow is
   timed twice before anyone calls it slow.
6. `python3 .context-gate/bin/govern build start <slug> --plan <file>` (add `--high-risk` when the
   plan marks the phase so). Add `--visual` when the phase touches a path the build rules list as
   visual. Add `--from <branch>` whenever the phase branch was not cut from the default branch.
7. Scan the phase's units against each other: any two that change the same file or interface run
   in order, not side by side. Where the plan is silent or two units disagree, decide, and record
   it: `python3 .context-gate/bin/govern build ruling "<what you decided> - <why> - <what it costs
   if wrong>"`. A ruling is how you keep moving without deciding in secret.
8. Tell the user in three or four lines what you are about to build and in what order. Do not wait
   for an answer.

## 2. Each unit

1. `python3 .context-gate/bin/govern build unit add <id> --title "<title>"` (add
   `--role senior-developer` only for a trigger in the build rules; add `--red` for a bug fix). It
   writes the brief's skeleton.
2. Fill the brief from the plan: the task, the exact files, the tests and their command, what it
   uses from earlier units by exact name, an implementation to follow, what is out of scope. One
   concern, five items at most. Everything the agent needs is in this file; it will not read the
   project's instructions and it knows nothing of this conversation. Open each file the brief names
   to follow and confirm the function or test is there. An agent's worktree has no
   `.context-gate/local.toml`, so `govern` there runs the project's committed pin, not a beta:
   never brief an agent to run `govern agents render` or a command only the beta has. Leave the
   last lines (`Produces:`, `Red:`) as the command wrote them. A brief asks for a test "written
   first and seen failing" only for a unit added with `--red`.
   Before the brief is committed, check it against itself. Each test it asks for is read against
   the task line it proves. A rule with a sign, an order or a position carries one worked example.
   The existing tests the unit will change are named, so the agent does not stop on them. It
   points at no file under `.context-gate/build` that git ignores (a report is not in the next
   agent's worktree: copy what it needs into the brief).
3. Commit the ledger and the brief on the phase branch.
4. `python3 .context-gate/bin/govern build launch <id>`. It checks the brief is filled in and
   committed, marks the unit running, and prints the launch prompt: a `Brief:` line and a `Base:`
   line. Say which agent, model and effort you are launching, then launch it in a worktree of its
   own with exactly those lines as the whole prompt. The agent fast-forwards its worktree to the
   base before it reads the brief, so what you committed is what it gets.
   Units that share no files may be launched together, in one message. Never add work to an agent
   that is running.
5. When it hands back, read its first line.
   - `DONE` or `DONE_WITH_CONCERNS`: read its report file, then the diff itself
     (`git diff <base>..<its branch>`, where the base is the `Base:` you launched it with, never
     `HEAD~1`). An agent in a worktree writes its report inside that worktree, beside its copy of
     the brief; the line you are shown when it finishes names the file.
     Check it did what the brief says and nothing else. Then merge its branch into the phase
     branch. Then run the project's full gate on the phase branch (when the project's build
     rules have no "After you merge a unit" line, the agent ran the full gate itself: read its
     result and skip this run). Green: record it with
     `python3 .context-gate/bin/govern build unit set <id> done --branch <b> --commit <c>`.
     That also copies the unit's report and failing output out of the agent's worktree into
     this checkout's build folder, where the reviewers will look for them. Red: the unit you
     just merged is failed; leave the merge in place, launch nothing else from the phase branch
     until it is green again, and go to the failure bullet.
   - `BLOCKED` or `NEEDS_CONTEXT`: `... unit set <id> blocked --note "<why>"`. Decide what it
     needs. If the brief was wrong, fix the brief and launch a **fresh** agent. If it stopped on a
     `senior-developer` trigger, or the developer has now failed it as many times as the build
     rules allow, hand it up: `... unit set <id> pending --role senior-developer` (the brief then
     promises a report of its own, so the earlier one is kept), add the earlier report's file name
     to the brief, and launch `senior-developer`. Never resume the old agent. A first attempt's
     work reaches the next agent by its own branch: have it committed there and name the branch
     in the new brief. Never merge it into the phase branch while that would leave the phase
     branch red: every unit launched after starts from it.
   - The gate is red after its merge, or the diff is not what the brief asked: `... unit set <id>
     failed --note "<what>"`, then a fresh agent with a brief that says exactly what is wrong.
     The fresh agent's brief says which tests fail and that the first attempt is already in its
     base.
   - A line telling you a contract was not met means the report or the failing-test output the
     brief promised does not exist. Treat the unit as failed.
6. Commit the ledger, and push the phase branch (`git push -u <remote> phase/<slug>` the first
   time). A machine that dies now loses nothing that was merged.
7. Next unit. `govern build status` always says which.

## 3. Review, once

When every unit is `done`:

1. Run the project's full gate on the phase branch.
2. `python3 .context-gate/bin/govern build review`. It writes the review package and the
   reviewers' briefs and prints the launch lines. For a high-risk phase, fill the definition of
   done into the `qa-engineer` brief from the plan's "Done when" list. Commit the ledger and the
   briefs.
3. `python3 .context-gate/bin/govern build launch review` prints a launch prompt for
   `code-reviewer`, and one for `qa-engineer` when the phase is high-risk. Launch them in one
   message so they run together, without a worktree: they change nothing. Tell them nothing about what you expect them to find.
4. Read both reports. Rule on each finding: it stands, or it does not and why. A finding that
   contradicts a locked decision or the plan is yours to rule on, not the fixer's.

## 4. One fix pass

If nothing stands, skip to 5.

1. `python3 .context-gate/bin/govern build fix`, then fill its brief: the findings that stand,
   most severe first, five at most per agent, each with its file and line and the input that shows
   it, each fixed test first. Add one line for each finding you ruled out and why, so the fixer
   does not reopen it. More than five findings: run `build fix` again once the first brief is
   filled; it writes a second brief, for a second fresh agent, on files the first does not touch.
   A brief whose findings meet a `senior-developer` trigger is made with
   `build fix --role senior-developer`. Commit the ledger and the briefs.
2. `python3 .context-gate/bin/govern build launch fix` prints a launch prompt for each fix brief.
   Launch a fresh agent for each, in a worktree of its own. Check and merge its work as in
   step 2.5, then `python3 .context-gate/bin/govern build collect <its branch>` to bring its
   report into this checkout.
3. The fix is not reviewed again. The gate and your own reading of the fix diff close it. The one
   exception: a fix that touched a high-risk path gets `qa-engineer`'s failed probes re-run, by a
   brief that lists only those probes. A finding made on a running system is proved fixed on a
   running system, with the reviewer's own steps.

## 5. Close

1. The owner's turn, when the plan's "Done when" has a line for them: launch an isolated copy of
   the build yourself if the project says how, confirm the change is really there, then tell the
   owner it is ready and what to look at, with what right and wrong look like. Do not hand them a
   command to work out. Wait for their word before closing. Changes the owner asks for there are a fix pass: run
   `python3 .context-gate/bin/govern build fix` again, which works after `build close`. With no such line, do not stop for the owner: tick the
   lines on your own evidence and go on. When the owner passes the checkpoint of a plan's last
   phase, delete the plan file, fix the pointers to it, and say so: git keeps it.
2. Tick the plan's "Done when" lines against evidence you saw in this session, never against a
   report alone. A line you could not show stays open, marked `(unverified: needs <what>)`.
3. `python3 .context-gate/bin/govern build close`. Fill in what its brief leaves for you (what
   landed, what was decided, what is unverified), commit, then run
   `python3 .context-gate/bin/govern build launch close` and launch `technical-writer` in a
   worktree of its own with the prompt it prints. Merge its branch. Then run the two commands `build close` printed: they remove
   the phase's folder from the repository and commit that, so the tree is clean. Git keeps the
   ledger and the briefs in history.
4. If the build rules mention the run report, run it now, before the phase is closed: it reads more
   of the phase's runs than it can afterwards. Pass its suggestions on. Apply none.
5. `python3 .context-gate/bin/govern phase close`. It pushes the phase branch, confirms the remote
   holds it, and removes the branches and worktrees this phase merged. It opens no pull request
   and merges nothing into the default branch: that is the owner's, or the project's own rule.
6. Report, short: what landed, the gate line, what is unverified and what it needs, each ruling
   with what it costs if wrong, what the owner owes (the exact command, link or decision), and
   what the reviews found and what you did about each.

## Carrying on

A build carries on through the plan's later phases by default; `--long` is still accepted and
changes nothing, and `--one` stops when this phase is closed. Everything above holds, and:

- **Finishing a phase is not a reason to stop.** Close it (step 5; stop at 5.1 only when the phase's
  "Done when" has a line for the owner), cut the next phase's branch from the phase you
  just closed, start it with `build start <slug> --plan <file> --from phase/<the one you closed>`
  so its review holds only its own work, and go on.
- **Checkpoint constantly.** The ledger commit and the push after every unit are what a crash
  leaves you. Have `technical-writer` refresh the handoff at every phase boundary, never "at the
  end": the end is the one thing a crash reliably removes.
- **Route around what is blocked.** Build what does not depend on it, mark the rest
  `(unverified: needs <what>)`, write down exactly what a person must do, and move on. Do not
  fake what is missing, and do not retry it.
- **Decide, and record it.** An answerable question is a ruling, not a stop.
- **Heed the usage alerts** if the project has them: they are the owner's own words about when to
  slow down or wrap up. Stop starting units when your context is about two thirds full; close the
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
