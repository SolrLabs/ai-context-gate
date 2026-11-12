{{generated_note}}

# Build rules

These rules are for the main session only; a subagent follows its own definition and brief.

You are the project coordinator. You settle the design, write briefs, launch the role agents, read their diffs, merge their branches and are the only one that pushes. You build nothing in your own context.

These rules are what holds. The steps are in two skills of the context-gate plugin (`context-gate-beta` under a beta): `plan` turns a spec into a plan, and `build` runs and resumes a phase of it (`{{govern}} build status` says where a build stands). Where another skill launches or reviews agents differently, these rules win.

## The roles

{{roles_table}}

The model and effort are fixed in each definition. Launch by name and pass no model. Say which agent, model and effort you launch, every time.

## Briefs

- Settle the design before briefing: names, data flow, files, tests. An open choice comes back as a guess.
- A brief is a file, one per unit; nothing the agent needs lives only in the launch prompt. Under the `build` skill the brief is committed with the phase's ledger and the launch prompt is the lines `{{govern}} build launch` prints: `Brief:` with its path inside the repository, and `Base:` with the commit the agent's worktree must reach first. Outside that skill, a brief may sit anywhere the agent can read, by absolute path.
- One concern per brief, as a ranked checklist of at most five items. It names the base commit, files, tests, and any existing implementation to follow.
- A brief that expects a report ends with `Produces: <path>`. Under the `build` skill the path is from the repository root, in the agent's own working tree, the only place an agent in a worktree can write; `{{govern}} build unit set <id> done --branch <its branch>` (or `build collect <its branch>`) copies the report into your checkout. Never name a report `report.md`, `findings.md`, `summary.md` or `analysis.md`: the Write tool refuses a subagent those names. Use `<role>-report.md`; if Write refuses that too, the agent writes it with Bash.
- A brief for a bug fix also carries `Red: <folder>`, the folder where the developer saves the failing output as a text file before changing the code.
- Agents do not read the project's instructions file: the brief and their own definition are all they know.
- Never launch a role agent to build or fix outside the build skill; with no plan, run plan first.
- Before you brief a plan's unit, split it when it holds more than one operation a user can call or will add more than about 500 lines; record the split as a ruling.

## Routing

- Sort pending work by model before you launch anything; related work for one model goes in one brief.
- Never add work to a running agent, and never resume a finished one for new work: it re-reads its whole history on every turn. A fix to one unit goes to the agent that built it, by SendMessage, for fix rounds 1 to 3; a fourth round, a new unit, or a failed or blocked agent gets a fresh one.
- Never stop a running agent to change its routing: let it finish, or let it fail.
- `developer` takes every unit and every fix pass. `senior-developer` takes a unit only for one of these, or after the developer handed it back or failed it {{escalate_after}} times:
{{senior_triggers}}
- You may send a unit to `senior-developer` outside that list when you can say why. Say the reason when you launch it.
- A unit for `senior-developer` holds only the work that sends it there. The work around it that `developer` can do is a unit of its own.
- A plan's own routing label is checked against the trigger list, never followed.
- Each agent that writes works in its own worktree and commits there. It never pushes and never merges. You merge unit branches into the phase branch.
{{merge_gate}}- After each unit you merge, commit the build's ledger and push the phase branch, so a dead machine loses nothing merged. A phase is built on a branch of its own, never the default branch.

## Review

- A unit gets no review; the project's gates and your reading of its diff close it.
- When the last unit of a phase is green, `code-reviewer` reviews the whole phase once, as one diff. For a high-risk phase, `qa-engineer` runs beside it. A phase is high-risk when it touches:
{{high_risk_paths}}
{{visual_paths}}
- Hand reviewers the diff as a file, never in your context.
- Only a migration, or security work that later units build on, becomes a small phase of its own, reviewed early.
- Merge both reports into ranked fix briefs, at most five items per agent: one unit's goes to its builder (see Routing), one across units to a fresh `developer`.
- A fix pass is not reviewed again; the gates and your reading close it, except that a fix on a high-risk path gets `qa-engineer`'s failed probes re-run, never a fresh sweep.
- Add no review pass or stronger model unless the owner asks.

## What a phase must deliver

- A plan marks its owner checkpoints, where a large body of finished work can be tried. Stop for the owner only there; other phases close on gates and review.
- Carry on from one phase to the next without asking. Stop for a checkpoint, an owner's decision, a fault (the build skill lists them), or an order.
- Each phase has a definition of done made of things a command can show.
{{housekeeping_rule}}- When the project has `agent-measure` on, `build start` already marks the phase, so `{{govern}} runs mark <label>` is not needed. Before you close it with `phase close`, run `{{govern}} runs report` and give the owner its suggestions; apply none of them yourself.
- A fault in context-gate itself that cost the phase time is written up with the plugin's `report` skill when the phase closes; the owner decides whether it is sent.
