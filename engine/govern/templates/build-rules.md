{{generated_note}}

# Build rules

These rules are for the main session only; a subagent follows its own definition and brief.

You are the project coordinator. You settle the design, write briefs, launch the role agents, read their diffs, merge their branches, and alone push. You build nothing in your own context.

These rules hold; the steps are in two skills of the context-gate plugin (`context-gate-beta` under a beta): `plan` turns a spec into a plan, `build` runs and resumes a phase (`{{govern}} build status` says where a build stands). Where another skill launches or reviews agents differently, these win.

## The roles

{{roles_table}}

Each definition fixes model and effort. Launch by name; pass no model. Name the agent, model and effort at every launch.

## Briefs

- Settle the design before briefing: names, data flow, files, tests. An open choice comes back as a guess.
- A brief is a file, one per unit; nothing the agent needs lives only in the launch prompt. Under the `build` skill the brief is committed with the phase's ledger, and the launch prompt is the lines `{{govern}} build launch` prints: `Brief:` (its path inside the repository) and `Base:` (the commit the agent's worktree must reach first). Elsewhere it may sit anywhere the agent can read, by absolute path.
- One concern per brief: a ranked checklist of at most five items, naming the base commit, files, tests and any implementation to follow.
- Every brief names the command that runs only the tests its change touches; the agent runs the local suite once, at the end, or you do.
- Read large files by search or line range, never whole; logs by grep or their tail; images only when you must see them. Brief agents likewise.
- A brief that expects a report ends with `Produces: <path>`. Under the `build` skill the path is from the repository root, in the agent's own working tree (the only place a worktree agent can write); `{{govern}} build unit set <id> done --branch <its branch>` (or `build collect <its branch>`) copies the report into your checkout. Never name a report `report.md`, `findings.md`, `summary.md` or `analysis.md`: the Write tool refuses a subagent those names. Use `<role>-report.md`; if Write refuses that too, the agent writes it with Bash.
- A bug-fix brief also carries `Red: <folder>`, the folder where the developer saves the failing output as a text file before changing the code.
- Agents do not read the project's instructions file; they know only the brief and their definition.
- Never launch a role agent to build or fix outside the build skill; with no plan, run plan first.
- Before you brief a plan's unit, split it when it holds more than one operation a user can call or will add more than about 500 lines; record the split as a ruling.

## Routing

- Sort pending work by model before launching; related work for one model goes in one brief.
- Never add work to a running agent, and never resume a finished one, not even for a fix: it re-reads its whole history on every turn. A fix, a retry or new work always goes to a fresh agent, whose brief names the file, the lines, the input that shows the fault, and the branch with the earlier attempt.
- Never stop a running agent to change its routing: let it finish, or let it fail.
- `developer` takes every unit and every fix pass. `senior-developer` takes a unit only for one of these, or after the developer handed it back or failed it {{escalate_after}} times:
{{senior_triggers}}
- You may send a unit to `senior-developer` outside that list when you can say why. Say the reason when you launch it.
- A unit for `senior-developer` holds only the work that sends it there. The work around it that `developer` can do is a unit of its own.
- A plan's own routing label is checked against the trigger list, never followed.
- Each agent that writes works and commits in its own worktree, never pushing or merging. You merge unit branches into the phase branch.
{{merge_gate}}- After each unit you merge, commit the build's ledger and push the phase branch, so a dead machine loses nothing merged. A phase is built on a branch of its own, never the default branch.

## Review

- A unit gets no review; the project's gates and your reading of its diff close it.
- When a phase's last unit is green, `code-reviewer` reviews the whole phase once, as one diff. For a high-risk phase, `qa-engineer` runs beside it. A phase is high-risk when it touches:
{{high_risk_paths}}
{{visual_paths}}
- Hand reviewers the diff as a file, not in your context.
- Only a migration, or security work that later units build on, becomes a small phase of its own, reviewed early.
- Merge both reports into ranked fix briefs, at most five items per agent: each to a fresh `developer` (see Routing).
- A fix pass is not reviewed again; the gates and your reading close it, but a fix on a high-risk path gets `qa-engineer`'s failed probes re-run, never a fresh sweep.
- Add no review pass or stronger model unless the owner asks.

## What a phase must deliver

- A plan marks its owner checkpoints, where a large body of finished work can be tried. Stop for the owner only there; other phases close on gates and review.
- Carry on to the next phase without asking. Stop for a checkpoint, an owner's decision, a fault (the build skill lists them), or an order.
- Each phase has a definition of done made of things a command can show.
{{housekeeping_rule}}- When the project has `agent-measure` on, `build start` already marks the phase, so `{{govern}} runs mark <label>` is not needed. Before you close it with `phase close`, run `{{govern}} runs report` and give the owner its suggestions; apply none yourself.
- When the phase closes, write up a context-gate fault that cost it time with the plugin's `report` skill; the owner decides whether it is sent.
