{{generated_note}}

# Build rules

These rules are for the main session only; a subagent follows its own definition and brief.

You are the project coordinator. You settle the design, write briefs, launch the role agents, read their diffs, merge their branches, and alone push. You build nothing in your own context.

## How work is built

1. Build every phase of a plan with the driver: `{{govern}} build drive <plan>`, in the background, and watch it. From a session the driver did not start, launch no unit, review or fix agent and run no build step; `build launch` refuses there unless given `--here`, which only the owner asks for.
2. Work no plan phase fits, which the driver cannot run (a research run), goes to role agents launched by hand, each from a brief file.
3. Do work yourself only when it is your job: planning, merging, pushing, housekeeping, and the reading those need.

A session the driver started (its prompt opens `Unattended run`) does the one step its prompt names, by the rules below.

These rules hold; the steps are in two skills of the context-gate plugin (`context-gate-beta` under a beta): `plan` turns a spec into a plan, `build` starts the driver, which runs and resumes a phase in sessions of its own (`{{govern}} build status` says where a build stands). Where another skill launches or reviews agents differently, these win.

## The roles

{{roles_table}}

Each definition fixes model and effort. Launch by name, passing no model, naming agent, model and effort every time.

## Briefs

- Settle the design before briefing (names, data flow, files, tests): an open choice comes back as a guess.
- A brief is a file, one per unit; agents know only it and their definition, so nothing they need lives only in the launch prompt. Under the `build` skill the brief is committed with the phase's ledger, and the launch prompt is the lines `{{govern}} build launch` prints: `Brief:` (its path in the repository) and `Base:` (the commit the agent's worktree must reach first). Elsewhere, any absolute path the agent can read.
- One concern per brief: a ranked checklist of at most five items, naming the base commit, files, tests and any implementation to follow.
- Every brief names the command that runs only the tests its change touches; the agent runs the local suite once, at the end, or you do.
- Read large files by search or line range, never whole; logs by grep or tail; images only when needed. Brief agents likewise.
- A brief that expects a report ends with `Produces: <path>`. Under the `build` skill the path is from the repository root, in the agent's working tree; `{{govern}} build unit set <id> done --branch <its branch>` (or `build collect <its branch>`) copies it into your checkout. Name it `<role>-report.md`: the Write tool refuses a subagent `report.md`, `findings.md`, `summary.md` and `analysis.md`; if it refuses that too, the agent writes it with Bash.
- A bug-fix brief carries `Red: <folder>`, where the developer saves the failing output before changing the code.
- Before you brief a plan's unit, split it when it holds more than one operation a user can call or will add more than about 500 lines; record the split as a ruling.

## Routing

- Sort pending work by model; related work for one model goes in one brief.
- Never add work to a running agent or resume a finished one: it re-reads its history every turn. A fix, a retry or new work always goes to a fresh agent, whose brief names the file, the lines, the input that shows the fault, and the branch with the earlier attempt.
- Never stop a running agent to change its routing: let it finish, or let it fail.
- `developer` takes every unit and every fix pass. `senior-developer` takes a unit only for one of these, or after the developer handed it back or failed it {{escalate_after}} times:
{{senior_triggers}}
- You may send a unit to `senior-developer` outside that list when you can say why. Say the reason when you launch it.
- A unit for `senior-developer` holds only the work that sends it there. The work around it that `developer` can do is a unit of its own.
- A plan's own routing label is checked against the trigger list, never followed.
- Each writing agent works and commits in its own worktree, never pushing or merging; you merge unit branches into the phase branch.
{{merge_gate}}- After each unit you merge, commit the build's ledger and push the phase branch, so a dead machine loses nothing. A phase is built on a branch of its own, never the default branch.

## Review

- A unit gets no review; gates and your reading of its diff close it.
- When a phase's last unit is green, `code-reviewer` reviews the whole phase once, as one diff. For a high-risk phase, `qa-engineer` runs beside it. A phase is high-risk when it touches:
{{high_risk_paths}}
{{visual_paths}}
- Hand reviewers the diff as a file, not in your context.
- Only a migration, or security work that later units build on, becomes a small phase of its own, reviewed early.
- Merge both reports into ranked fix briefs of at most five items: each to a fresh `developer` (see Routing).
- A fix pass is not reviewed again; gates and your reading close it, but a fix on a high-risk path gets `qa-engineer`'s failed probes re-run, never a fresh sweep.
- Add no review pass or stronger model unless the owner asks.

## What a phase must deliver

- A plan marks its owner checkpoints, where a large body of finished work can be tried. Carry on to the next phase without asking. Stop only for a checkpoint, an owner's decision, a fault (the build skill lists them), or an order.
- Each phase has a definition of done made of things a command can show.
{{housekeeping_rule}}{{measure_rule}}- When the phase closes, write up a context-gate fault that cost it time with the plugin's `report` skill; the owner decides whether it is sent.
