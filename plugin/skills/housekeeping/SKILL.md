---
name: housekeeping
description: End-of-session cleanup - governance check, worktrees and branches that are merged, finished working files, the handoff and memory. Removes what is merged with a verified backup, asks before anything else, and never pushes or merges. Use when the user says housekeeping, or at the end of a session or a build.
allowed-tools: Bash(python3 .context-gate/bin/*), Bash(git *), Read, Edit, AskUserQuestion, SendMessage
---

# Housekeeping

The closing pass that keeps worktrees, merged branches and finished working files from piling
up. Run it at the end of a session, so the next one starts from the handoff alone. It never
pushes, never merges and never forces.

## 1. Measure before touching anything

Run `python3 .context-gate/bin/govern check`, `python3 .context-gate/bin/govern sweep` and
`python3 .context-gate/bin/govern build status`. All three only read. Put the numbers in one
table: errors and warnings, worktrees and branches by verdict, finished files, builds in flight,
and the `memory` line's count against its limit. Step 7 compares against it.

## 2. Respect other live sessions

Run `ListAgents`. When a worktree, branch or working file that steps 3 to 6 would touch belongs
to another live session, send that session the exact list with `SendMessage` and wait for its
answer. A worktree marked `IN-USE` has a process in it; a quiet session can still own one that
looks idle.

## 3. Working files

- Delete each file `sweep` lists as `finished`. Git keeps the history. Fix every link to a
  deleted file, then run `python3 .context-gate/bin/govern index`.
- Refresh the existing handoff to the current state: no running log, and under the
  `handoff-words` limit. Trim before adding when it is already over. End it with the next step
  and the exact command that starts it. Never create a second handoff file.

Read working files through the index, and only the ones you curate.

## 4. Memory

Read the memory index the `memory` line names. Merge overlapping lines and delete the wrong or
superseded ones, to stay under that line's limit. Only durable agreements belong in it, never a
project's internal detail.

## 5. The cull list

- Items whose verdict `auto-remove` names go without asking, once step 2 has cleared them.
- Present every `REVIEW` and `DIRTY` item with its evidence and your recommendation, and wait.
  For a branch, check `git cherry` against the default branch and the PR's state first.
- Warn that removing a worktree deletes its ignored files, such as build output and local
  settings. The backup keeps commits, not those files.
- Act on nothing the owner has not approved. A `DIRTY` worktree needs `--force`, which this skill
  never passes: hand the owner the literal command.

## 6. Apply

Run `python3 .context-gate/bin/govern sweep --apply --only <name>...`, naming exactly the items
to remove. It bundles the branches and verifies the bundle, runs `git worktree remove` without
`--force`, and deletes a branch only when its tip has not moved. A name that is not removable now
is skipped and reported. Keep the restore line it prints.

## 7. Close

- Run `python3 .context-gate/bin/govern check`. It must report 0 errors.
- Commit the changes on the current branch. On the default branch, leave them uncommitted and
  list them. Never push, merge or force.
- Report a before and after table of the step 1 numbers.
- End with "Ready for a new session": builds in flight, branches with unpushed commits, and the
  work branch when it is not merged into the default branch, with its PR's state.
