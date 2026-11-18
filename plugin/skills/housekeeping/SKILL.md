---
name: housekeeping
description: End-of-session cleanup - governance check, worktrees and branches that are merged, finished working files, the handoff and memory. Removes what `auto_remove` names, and pushes the remote-branch deletions sweep judged removable, after a verified backup; asks before anything else, and never merges. Use when the user says housekeeping, or at the end of a session or a build.
allowed-tools: Bash(python3 .context-gate/bin/*), Bash(git *), Read, Edit, AskUserQuestion, SendMessage
---

# Housekeeping

The closing pass, so the next session starts from the handoff alone. It never pushes, merges
or forces; its one push deletes a remote branch `sweep` judged removable.

## 1. Measure before touching anything

Run `python3 .context-gate/bin/govern check`, `python3 .context-gate/bin/govern sweep` and
`python3 .context-gate/bin/govern build status`. All three only read (`runs report`, below, also records unmeasured sessions). Tabulate errors and
warnings, worktrees and branches by verdict, finished files, builds in flight, and the `memory`
line's count against its limit.

When `agent-measure` is on, also run `python3 .context-gate/bin/govern runs report` and pass its
`Suggestion:` lines on to the owner.

## 2. Respect other live sessions

Run `ListAgents`. When an item steps 3 to 6 would touch belongs to another live session, send
it the exact list with `SendMessage` and wait for its answer.

## 3. Working files

- Delete each file `sweep` lists as `finished`. Fix every link to it, then run
  `python3 .context-gate/bin/govern index`.
- Refresh the existing handoff to the current state: no running log, and under the
  `handoff-words` limit. End it with the next step and the exact command that starts it. Never
  create a second handoff file.

## 4. Memory

Read the memory index the `memory` line names. Merge overlapping lines and delete wrong or
superseded ones, to stay under its limit. Keep only durable agreements.

## 5. The cull list

- Items whose verdict `auto-remove` names go without asking, once step 2 has cleared them.
  `PUSHED` means the exact tip is on a remote and idle for `[sweep] stale_days` (default 14).
  `remote` items are remote branches; `STALE` is one unmerged, with no open PR, idle
  `stale_remote_days` (default 30).
- Present every `REVIEW` and `DIRTY` item with its evidence and your recommendation, and wait.
  For a branch, check `git cherry` and the PR's state first.
- Protected branches (the `merged-branches` `keep` list) are `IN-USE` and never on the list.
- Warn that removing a worktree deletes its ignored files, which the backup does not keep.
- Act on nothing the owner has not approved. A `DIRTY` worktree needs `--force`, which this skill
  never passes: hand the owner the literal command.

## 6. Apply

Run `python3 .context-gate/bin/govern sweep --apply --only <name>...` with `--home` before
`--only`, naming exactly the items to remove (`origin/b` for a remote branch). `--home` fetches
and leaves a removable branch for the default branch. It bundles and verifies the branches, runs
`git worktree remove` without `--force`, and deletes a branch only when its tip has not moved.
Keep the restore line it prints.

## 7. Close

- Run `python3 .context-gate/bin/govern check`. It must report 0 errors.
- Commit on the current branch. On the default branch, leave changes uncommitted and list them.
  Never push, merge or force.
- Report the step 1 table before and after.
- End with "Ready for a new session": on the default branch, level with its remote, a fresh
  `sweep` listing nothing removable. Name builds in flight, branches with unpushed commits, and
  an unmerged work branch with its PR's state.
