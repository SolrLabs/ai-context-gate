# context-gate — release notes

Newest first. Each release says what changed and, under **Upgrading**, anything a project has to
do or decide. The upgrade report quotes every entry between the engine a project ran and the one
it upgraded to.

## 0.7.0-beta.9 — 2026-10-11

- **One hand-back checklist in every role:** every role now ends with one binding sentence and a numbered checklist under `## Before you hand back`. The lines are `Scope`, `Red`, `Gate`, `Commit`, `Not verified` and `Report` for the roles that write, and the last two for the roles that review. It replaces the report paragraph added in the bullet below. A line is answered as `Key: <answer>`. A run that stops early still owes the file. `handback_checks` on `agent-roles` adds lines for every role.
- **The stop gate can read the checklist:** `checklist` on `agent-gates`, off by default. With it on, the gate refuses a report that leaves a checklist line unanswered. `python3 .context-gate/bin/govern runs resolve` now prints `checklist`, `checklist_keys` and `checklist_texts`.
- **Shorter role definitions:** the sentences the checklist now says are gone from the templates; no rule was removed. The note line in every brief and the stop gate's block reason have new wording.
- **The report is part of the task, written before the hand-back:** every role's Report section now opens with one shared paragraph saying the report file is part of the work, is written before any final message, and that the message carries `Report: <full path>`. Every brief the build commands write says the same in one line above its `Produces:` line. The stop gate's block message says the run is not finished, nothing sent has been accepted, to put each line right now even after a summary was sent, and to send the final message again with the `Report:` line.
- **A run blocked on its contract is said even when it never ends:** when the stop gate blocks a run
  on an unmet contract, the launch record keeps what was unmet. If the run never stops again, the
  completion notice says `[agent run] <type>: was stopped on an unmet contract and has not
  finished: ... Treat its report as not filed.` A resume clears it.
- **Helper agents are kept out of the run log:** a stop for an agent the launch hook never saw, with
  no type, no contract and no tool calls (a helper the harness starts itself), writes no row, so
  `govern runs report` no longer lists ` (None): token totals unavailable` rows for them.
- **A beta's plugin now loads:** Claude Code does not load a local plugin whose name an
  installed plugin has, so the beta plugin, named like the release, never loaded where the release
  was installed, and a project on a beta ran the beta engine with the release's hooks and skills.
  The beta's plugin is now `context-gate-beta` (`context-gate-beta@skills-dir`, skills
  `/context-gate-beta:<skill>`), and its hooks do nothing in a project that is not on a beta. A
  launcher from 0.6.1 or earlier enables the old id; the beta engine corrects it on any `govern`
  command and says so.
- **Two plugin skills run the build loop:** `/context-gate:plan` turns a spec into phases and
  units, each with a checkable "done when" line, routed by the project's triggers.
  `/context-gate:build` runs one phase with the rendered roles, resumes a build in flight, and with
  `--long` carries on through later phases unattended. Neither needs another plugin.
- **`govern build`, a ledger for one phase:** `start`, `unit add`, `unit set`, `launch`, `collect`, `ruling`,
  `status`, `review`, `fix` and `close`. The ledger is `.context-gate/build/<phase>/ledger.toml`.
  It is committed on the phase branch with the unit briefs. Reports, diffs and failing-test output
  under that folder are ignored by git. A session that lost its place reads `govern build status`.
- **A sixth gate in `agent-gates`:** a brief that still holds an unfilled skeleton line is
  refused at launch.
- **Session start tells the coordinator when a build is in flight** and what is next.
- **Changed role definitions and build rules:** when the launch prompt has a `Base:` line, the
  agent fast-forwards its worktree to it first, and a report is written inside the agent's own
  working tree. The build rules point at the two skills, say to commit the ledger and push the
  phase branch after each merged unit, and say they win over another plugin's way of launching or
  reviewing agents. A project with `agent-roles` on re-renders them on upgrade;
  `govern agents render` shows what changed.
- **Security is the only default `senior_triggers`:** the default list that sends a unit to
  `senior-developer` is now one item, security (credentials, tokens, signing, authentication,
  access control, or input from outside the project). The build rules add two bullets after the
  list: the coordinator may send a unit to `senior-developer` outside it when it can say why,
  rarely; and a unit for `senior-developer` holds only the work that sends it there. The plan
  skill's Routing paragraph says the second too.
- **The options skill writes an option the pinned release does not have into
  `.context-gate/local.toml`** when the project runs a beta.
- **A `.gitignore` that ignores `build/`:** a project whose `.gitignore` ignores `build/` would keep the ledger out of every
  commit. `govern build start` refuses and prints the line to add.

- **`decision-changes`, a check that reads what changed:** every other check reads the records as
  they are. This one compares the decision logs with git and reports three things. A locked
  decision whose text changed in place with no dated line saying so: ``<log>: P-12 (locked)
  changed without a new dated **Revised:** line — add `**Revised:** YYYY-MM-DD (what changed)`,
  or supersede it with a new decision``. A decision that was removed: ``<log>: P-12
  (locked) was removed — a decision is superseded, not deleted: reduce it to `## P-12 - Replaced
  by <id>` ``. And, under `--base` only, a decision or trap id your branch added that the base
  branch has also added since, with different text: `<log>: P-31 was also added on origin/main
  since this branch began ("<their title>") — if it is the same entry, merge origin/main into
  this branch; if not, renumber yours to an id neither side uses and update what cites it`.
  Adding a decision, editing a provisional one, re-wrapping a paragraph, changing a heading's
  dash or an entry's `**Topic:**`, moving an entry to another log, renaming a log and
  regenerating a block are not changes. `locked_statuses` (default `["locked"]`) names the
  statuses that count as locked.
- **`govern check --base <ref>`, the gate for a pull request:** it compares the branch with the
  commit where it left `<ref>` (the merge base), in each repository, so what landed on `<ref>`
  afterwards is not blamed on the branch. `decision-changes` then reports errors (a removed
  decision that was not locked stays a warning). `--base` combines with `--project`,
  `--workspace-only` and `--path` with `--history-from`. When the comparison cannot be made it
  fails: `decision-changes: could not compare with origin/main — <reason>`, where the reason for
  a shallow clone ends `fetch the full history (fetch-depth: 0)`. In a run of the whole
  workspace, a project that is its own repository and does not have the ref is a warning,
  `decision-changes: could not compare <project> with origin/main — <reason>`, and the rest is
  still compared. Under `--project NAME` that is an error, and so is `--path` with no
  `--history-from`: `decision-changes: could not compare <project> with origin/main — a snapshot
  needs --history-from to be compared`.
- **Without `--base`, the check only advises:** plain `govern check` compares uncommitted work
  with the last commit, and everything `decision-changes` finds is a warning that goes once the
  edit is committed. When there is nothing to compare with (no git, no commit yet) it says
  nothing.
- **`govern diff [--base <ref>] [--json]`, what a branch changed:** one line per changed decision
  (`removed`, `added`, `superseded`, `moved`, `revised`, `status` or `changed`), trap (`added`,
  `removed`, `changed`) and ratchet baseline number (`raised`, `lowered`, `added`, `removed`),
  then the commit each repository was compared with, or `not compared (<reason>)`. A baseline
  file that cannot be read is one line, `- unreadable <path> <reason>`. Every decision and trap
  line is `- <word> <log> <id>` and what the word needs, and a decision now in another log than
  at the base ends `, from <old log>` whatever its word. With no `--base` it compares with the
  last commit. It is a report and never fails: it exits 0 whatever changed and whatever could
  not be compared, and a reader that closes the pipe early (`govern diff | head -1`) ends it
  quietly. With nothing to report it says `- nothing changed`, or `- nothing compared` when no
  repository could be compared. The layout (the words, the punctuation, the arrows) is ASCII
  and the text is valid Markdown; titles and paths are the project's own text, and in the text
  a control character in a title is printed as `\xNN`. It names a commit by its first 12
  characters. `--json` gives the same report as one object with the keys `base`, `compared`,
  `decisions`, `traps` and `baseline`, and the full commit id: a tool that needs fields reads
  that, not the text. `--project NAME` limits it to one project: the entries whose log, at the
  base or now, is that project's.
- **`govern ci github`, a workflow for pull requests:** it prints a GitHub Actions workflow and
  writes nothing. The workflow checks out the pull request's own ref with the full history, runs
  `check --base` against the branch the pull request targets, and appends `govern diff` to the
  run's summary, also when the check failed. Put it in place with
  `python3 .context-gate/bin/govern ci github > .github/workflows/context-gate.yml`, from the top
  of the git repository. When `.context-gate/` is in a directory below the top, run it from that
  directory and write to `"$(git rev-parse --show-toplevel)/.github/workflows/context-gate.yml"`
  instead. The file is the project's from then on.
- **Agent roles, rendered from the engine (`agent-roles`, an option):** `govern agents render
  [--check]` writes five role definitions (`developer`, `senior-developer`, `code-reviewer`,
  `qa-engineer`, `technical-writer`; `ux-reviewer` and `architect` through `extra_roles`) into the
  project's agents folder, and the coordinator's build rules into
  `.claude/rules/context-gate-build.md`. They come from engine templates, the check's params
  (`models`, `efforts`, `max_turns`, `scoped_test`, `full_gate`, `senior_triggers`,
  `high_risk_paths` and more) and the project's own text in `.context-gate/agents/all.md` and
  `.context-gate/agents/<role>.md`. Each file starts with a `context-gate:rendered` note and is
  re-rendered on upgrade and removed on uninstall. A file the engine did not render is never
  written over: render names it and exits 1. `--check` writes nothing and exits 1 when anything
  would change.
- **`agents` accepts `maxTurns: false`:** it is an explicit "no cap" and satisfies
  `require_max_turns`. A missing key still does not.
- **`agent-references`, a check that finds an agent nobody defined:** ``<file>:<line>: names agent
  `<name>`, which has no definition in <agents_dir> — define it or fix the name``. `builtin` lists
  the names Claude Code provides.
- **Agent gates (`agent-gates`, an option; needs the plugin):** a role in `briefed` (default
  `developer`, `senior-developer`) is launched from a brief file, in one line, `Brief: <absolute
  path>`; the path may be quoted or contain spaces. A subagent cannot stop while a file its
  brief's `Produces:` line promises is missing or empty, while its final message points at a file
  (`report`, `written to`, `saved to` or `wrote`, then an absolute path) that does not exist, or,
  for a bug fix with a `Red:` folder, before the failing test output was saved ahead of the fix.
  A subagent cannot run `git push`. A gate refuses at most `max_refusals` times (default 2), then
  lets the agent go and tells the coordinator what was unmet.
- **Agent measurement (`agent-measure`, an option; needs the plugin):** per-tool timing and result
  size inside subagents, and real token totals per run, summed over its requests. The figure in a
  completion notice is only the context size at the last request. The coordinator gets one line
  per finished run. `govern runs mark <label>` starts a phase; `govern runs report [--all]
  [--json]` prints the runs since the mark, totals by role, model and tool, the largest results
  and fixed `Suggestion:` lines. Per-session state is swept after 30 days.
- **`govern phase close [--remote R] [--keep BRANCH] [--dry-run] [--path DIR]`:** pushes the phase
  branch to its remote, confirms the remote holds the commit, then removes the local branches and
  worktrees already contained in it. It opens no pull request, merges nothing upward and never
  forces a deletion; the reason for any refusal is printed, and it is not an error. It refuses a
  detached HEAD, the remote's default branch, uncommitted changes, and a branch whose upstream is
  the remote's default branch.
- **`memory-index` counts memories, not lines:** lines whose memory is of a kind in `exempt_types`
  (default `["feedback"]`) are not counted against `max_lines`, and a line that links two
  memories is a warning (`one_link_per_line`, default `true`).
- **The build, plan and options skills say four things they left out:** the build skill confirms each role is launchable in this session before the phase branch is cut, tells the coordinator to open the files a brief names and never to brief an agent to run a beta-only command (an agent's worktree runs the committed pin), runs a unit's gate without moving the coordinator's shell, and runs the run report before `phase close`. The options skill says the roles can be launched only from a new session. The plan skill says planning is a phase of its own. The build rules now run the run report before `phase close`.

- **`operator_rules`, a rule for every role:** a list on `agent-roles`, settable in a profile, rendered at the top of every role's project rules.
- **The build stops for the owner only at checkpoints:** the build rules stop only at phases a plan marks `checkpoint: yes`, and a build carries on from phase to phase by default. The build skill's `--long` section is now "Carrying on"; `--long` changes nothing and `--one` stops after the phase.
- **The developer roles say how to work under the worktree guard:** the `developer` and `senior-developer` roles say to run one plain git command per call and to put multi-line scripts in a scratch file.
- **A relative `Brief:` resolves from the project root:** the launch gate reads it from the session's project root first, then the hook's working folder.
- **The launcher reads its arguments first:** `bin/upgrade` and `bin/uninstall` print usage and exit 0 on `-h` or `--help`, and exit 2 on an argument they do not know, before anything is fetched or written.
- **Adopt says what the gate will do with untyped docs:** when no directory is fully typed, adopt asks what to do with a directory of markdown without frontmatter (`leave them out` writes `<dir>/*.md` to `[projects] exclude`; `govern them` says the gate will fail them), once per directory. The `no frontmatter` finding names the required keys and, when they are required, the allowed `doc_type` and `audience` values.
- **A contract paired with the wrong agent is corrected:** a starting agent claims its held launch by a rename, and at stop a contract paired with the wrong brief is corrected from the agent's own first prompt (not for a resumed agent, and only when the named brief exists).

- A beta: run it in a project on 0.6.1 or later with `python3 .context-gate/bin/govern beta on`.

**Upgrading:** a project already on a beta runs `govern beta on <this beta>` again, then any
`govern` command, then restarts Claude Code. Otherwise nothing to do. `decision-changes` arrives on, but no existing hook or CI job passes
`--base`, so a project's runs gain warnings at most and exit as they did. To gate pull requests,
run `python3 .context-gate/bin/govern check --base origin/<branch>` on a full clone, or commit
the workflow `govern ci github` prints. From then on an edit to a locked decision carries a
`**Revised:** YYYY-MM-DD (what changed)` line. A project whose statuses are not the standard ones
sets `locked_statuses` under `[checks.decision-changes]`. `level = "error"` does not make plain
`check` fail; to hold uncommitted work to the gate's rules locally, run `check --base HEAD`.

A project with `agent-roles` on that never set `senior_triggers` loses four triggers (schemas and public APIs, deleting or moving files the project did not create, a release or migration path, and the old wording for credentials and outside input) and its rendered files read as out of date until `govern agents render` is run; to keep the old list, set `senior_triggers` in the project's config before rendering.

The three agent options (`agent-roles`, `agent-gates`, `agent-measure`) are off until answered:
`/context-gate:options` offers them and `govern options` shows them. `agent-gates` and
`agent-measure` work only with the plugin installed. The plugin's new hooks start one short
Python process (about 19 ms measured on one machine) on every shell call and every tool call in
every project with the plugin enabled, even with both options off. `agent-references` and
`memory-index`'s new finding (a line that links two memories) arrive as warnings, not errors.
A project with `agent-roles` on, or that turns it on, runs
`python3 .context-gate/bin/govern agents render` to pick up the rules' and roles' new text, and to get the hand-back checklist into the role files; nothing turns red until then. `bin/upgrade` and `bin/uninstall` now exit 2 on an argument they used to ignore, so a script that passes one must drop it. The `no frontmatter` finding's text changed, for anything that parses it.

## 0.6.1 — 2026-10-06

- **Security: a source that starts with `-` is refused.** In 0.6.0 and earlier, a
  `[governance] source` (or a git profile) beginning with a dash was passed to git, which read it
  as an option, so a crafted `.context-gate/config.toml` could run a command at session start
  (the plugin's upgrade notice), on a gate run and on `bin/upgrade`. This matters to anyone who
  runs the gate or the plugin in a project whose `config.toml` they did not write. Every call now
  refuses such a source before git runs (`… source '…' starts with "-", which git would read as
  an option; name a git URL or a path`) and separates it with `--`. A tag whose name contains a
  slash is no longer read as a release or a beta.
- **A working file with no `status` is told the allowed values:** the `doc-frontmatter` error now
  reads `<file>: working file needs 'status', starting with one of ('active', 'held', 'planned',
  'complete', 'superseded') — it is what says which plan is active`, naming the project's own
  `working_statuses`. Before: `<file>: working file needs 'status' — it is what says which plan is
  active`, which is still what a project with an empty list (free text) gets.
- **One finding per over-long working file:** when the ratchet reports a working file's
  `max_working_words` breach as new or grown, the `doc-frontmatter` warning for the same file is
  no longer printed beside it. A breach recorded in the baseline that has not grown still shows
  the warning, and so does any file the ratchet says nothing about (a workspace doc, a rule with
  `ratchet = false`, a baseline or doc that cannot be read).
- **A new over-long working file leads with the fix:** ``ratchet: 'working_file_words:<file>' is
  a new breach at N words (doc-frontmatter max_working_words=M), not in baseline.json — split it
  into smaller files, put permanent content behind an index, or delete what is finished; to
  accept a file that predates the gate, run `govern baseline --allow-raise` ``. Before: ``… is a
  new breach at N, not in baseline.json — fix it, or if it is accepted for now run `govern
  baseline --allow-raise` ``, which every other kind of new breach still gets. A `grew` message
  for a working file now reads ``… grew to N words (doc-frontmatter max_working_words=M,
  baseline B) — fix it, …``, where it read ``… grew to N (baseline B) — fix it, …``; every other
  kind's is unchanged.
- **The over-long warning mentions splitting:** `<file>: N words exceeds doc-frontmatter
  max_working_words=M — split it into smaller files, put permanent content behind an index, or
  delete what is finished`. Before: `… — put permanent content behind an index, or delete what is
  finished`.
- **The "superseded" advice shows the project's dash:** the `decision-log` error for a superseded
  entry reads `… reduce it to `## D-12 - Replaced by <id>` …` in a hyphen-form log, and with the
  dash of the entry's own heading in any other. Before: always `## D-12 — Replaced by <id>`.
- **`bin/upgrade --to X.Y.Z-beta.N` installs nothing:** it exits 2 with `error: an upgrade takes
  a release, and X.Y.Z-beta.N is a beta; run a beta with govern beta on X.Y.Z-beta.N`. Before, it
  downloaded the beta into the engines directory and then refused to pin it.
- **The engine writes the heading separator the project uses:** `govern trap-add` and
  `installer migrate` (a bullet trap's heading, a pointer's) wrote `## T-N — Title` whatever the
  project's entries looked like. A pointer now keeps the dash of the heading it replaces. A new
  heading takes the dash of the last entry in that file, else of the project's decision log and
  other trap files, else a hyphen (`## T-N - Title`). With
  `decision_heading = "em-dash"` it is always ` — `. `govern show` prints an entry's heading as it
  is written in the file, where it printed an em dash.
- **The docs show the hyphen form:** examples read `## D-12 - Title`. The default `any-dash`
  grammar reads an em or en dash as before.
- **Betas are published as prereleases:** a `vX.Y.Z-beta.N` tag is now in the public repository,
  as a GitHub prerelease on a `beta` branch. Releases stay on `main`, and no project is offered a
  beta: the upgrade notice, `bin/upgrade`, the marketplace plugin and a series pin take releases
  only. To run one, see "Running a beta locally" in `docs/configuration.md`; it may break, and
  `govern beta off` returns the project to its pinned release.
- **`govern beta on` fetches the beta:** when the beta's engine or its local plugin is not
  installed, `beta on` installs it from the project's `[governance] source`, then switches. With
  no version it takes the newest beta of those installed and those tagged at the source. Before,
  it refused until both had been installed from a clone with `tools/release/install-engine.py`
  and `install-plugin.py`, which is still the way for a project with no `source`. Nothing else
  downloads a beta: not the gate, a session start or `bin/upgrade`.
- **Fetching an engine survives two sessions at once:** when the pinned engine is missing and two
  sessions start together, both now succeed (one used to end in a traceback). The fetch stages in
  its own directory, never replaces an engine that is already there, and no longer waits
  without end: every fetch of a pinned engine now has a 120 second limit, raised with
  `CONTEXT_GATE_FETCH_TIMEOUT` (whole seconds), and git is told not to prompt for a credential
  only when there is no terminal to answer.
- **`install-plugin.py` only replaces its own install by default:** it refuses a directory under
  `~/.claude/skills/` that it did not install, where it used to replace whatever was there.
  `--out DIR` still replaces DIR whatever it holds.

**Upgrading:** upgrade the plugin and each project: the source fix has two halves, one in the
plugin's engine (session start) and one in the project's own `bin/govern` and `bin/upgrade`,
which the upgrade replaces. Otherwise nothing to do, and no heading a project has written needs
to change. A reworded finding shows as new in `upgrade-report.md`, with the old wording as
gone; it is the same finding at the same severity. A script that
matches one of the messages above by its exact text needs the new wording. A project with
no entry heading yet gets hyphens from `trap-add` and `migrate`, where it got em dashes. The beta
fetch is in the project's own `bin/govern`, so it arrives with the upgrade to 0.6.1. On a slow
link, set `CONTEXT_GATE_FETCH_TIMEOUT` to more than 120 seconds, where a pinned engine's fetch
used to have no limit.

## 0.6.0 — 2026-10-05

- **New option `usage`:** the orchestrating agent (never a subagent) is told its context, 5-hour
  and weekly usage, and your own prompts from `usage-alerts.toml` at break points you set.
  `govern usage install` wraps your statusline to capture the data; `govern usage uninstall` puts
  it back. `SessionStart` now runs a bounded (7 second) resolve step in projects that mention
  `usage` or name a profile, and the plugin adds a `PostToolUse` hook that exits early on every
  tool call, in every project, when the option is off.
- **The data line:** `usage: context 37% (+5% in 1h20m) · account 5h 12% (resets 07:00) · 7d 56%
  (resets Sun 04:00)`. Context shows its rise since the session's first reading (a compaction
  restarts the count). The 5-hour and weekly figures are account-wide, so they are labeled
  `account`, show their own reset time and never show a rise. A value nobody has yet reads
  `pending`. A line comes when a value rises a step or a window resets; the session's first line
  says so.
- **`context_step` in `usage-alerts.toml`:** an optional whole number from 1 to 10 (default 5)
  for how many points context rises below 90% between lines. The 5-hour and weekly windows keep
  5; from 90% up every point counts.
- **Alerts stand apart, and edits apply mid-session:** each alert comes first, under
  `⚠ usage alert (owner's prompt, <file>):`, with the data line last. A change to
  `usage-alerts.toml` loads at the next tool call, without a restart; an invalid edit keeps the
  alerts already loaded and tells the agent once. Turning `usage` on or off still needs a new
  session.
- **A stopped capture is told:** after 10 minutes of activity without fresh data, a session that
  has had data is told once, with how to re-wrap the capture. Time spent idle (waiting on
  background agents, say) does not count.
- **Windows:** the capture runs the statusline you had through Git Bash when it is installed,
  else through PowerShell, following Claude Code's documented order, so a command written for Git
  Bash (`~/.claude/statusline.sh`, `$HOME`) keeps working once wrapped.
- **Moving between betas keeps `local.toml`:** `govern beta on` with another beta already on
  switches to the new one instead of asking for `beta off` first. Only the `engine` value in
  `.context-gate/local.toml` changes. Its first line of output reads `beta B on for this project,
  on this machine (was A; the rest of local.toml is kept):`. An `engine` line it cannot place, or
  a file that is read-only or cannot be read, is refused in one line.
- **`govern beta on` without a version** takes the newest beta engine installed, then runs the
  same checks as with the version named. It refuses when the newest one is a beta of a release
  the project already runs; naming that version still switches to it.
- **`govern beta off` shows what it removes:** when `local.toml` holds anything besides its
  `[governance]` table, `beta off` prints those lines for you to copy into
  `.context-gate/config.toml`.
- **A project on an older beta is told of a newer one:** with a newer beta engine installed, the
  session start and `govern beta` add `context-gate: beta B is installed (this project runs beta
  A): govern beta on B`. For a project on a beta, the session-start line about a newer release now
  reads `context-gate X is out (this machine runs beta B): …`. A project with no beta on is never
  told about any beta.

**Upgrading:** nothing to do — `usage` ships off. Turn it on with `/context-gate:options` (it runs
`govern usage install` for you), or by hand: set `level = "error"` under `[checks.usage]` in
`.context-gate/config.toml` (or a profile's `principles.toml`), then run
`govern usage install` once per machine. The `govern beta` changes are in the project's own
`.context-gate/bin/govern`, which the upgrade replaces.

## 0.5.1 — 2026-09-30

- **`govern beta`** runs a locally installed prerelease in one project, on one machine.
  `govern beta on X.Y.Z-beta.N` switches the project to that beta engine and its plugin;
  `govern beta off` switches back and works even when the beta is broken or gone; `govern beta`
  shows the state. It is handled by the entrypoint before any engine loads.
- **`.context-gate/local.toml`**, a new uncommitted config layer above the project's, holds the
  beta to run, its `[checks.*]` settings, and the plugin settings `beta on` found. Only the engine
  it names reads it. If that beta is not installed, the gate runs the committed pin with one line
  saying why.
- **`local-layer`** (error): fails the gate when git tracks `.context-gate/local.toml`, so a beta
  pin cannot reach CI or other clones. `govern beta on` keeps the file and
  `.claude/settings.local.json` out of git through `.git/info/exclude`.
- **Beta versions** (`X.Y.Z-beta.N`) are understood everywhere a version is parsed or ordered, and
  sort between the release before and their own release. A stable project is never offered one:
  the upgrade notice and `bin/upgrade` still see releases only.
- **A committed beta pin is refused.** `[governance] engine` in `config.toml` must be a release;
  betas run only from `local.toml`.
- **The local plugin install is off by default.** `tools/release/install-plugin.py` writes
  `defaultEnabled: false` into the installed copy, so it loads only where a project's
  `settings.local.json` turns it on. When both the local and the stable plugin are enabled, the
  local plugin's session start says every hook runs twice.
- **The release tools take beta tags.** `install-engine.py` and `install-plugin.py` install a
  `vX.Y.Z-beta.N` tag.

**Upgrading:** nothing to do. `local.toml` appears only after you run `govern beta on`.

## 0.5.0 — 2026-09-30

- **`govern options`** shows every opt-in check (`writing-rules`, `hooks-wired`,
  `checkout-hygiene`, `licenses`): its state (on, off or inherit), the layer that set it, whether
  it is new to this project, and a suggestion computed from what adopt already measures.
  `--global` shows the profile's own layer; `--json` adds the field set the skills parse;
  `--record CHECK ...` marks options as answered in `.context-gate/installed.toml`
  (`options_answered`), so a panel quit partway offers the rest again next time.
- **`/context-gate:options`**, a new plugin skill, shows the table, asks on, off or inherit per
  option with a recommendation where one applies, asks each turned-on option's setup question,
  edits the config by hand and confirms the change is live. `--global` edits a profile checkout
  and stops before pushing.
- **Adopt offers options** once the layout questions are settled. Answers carry
  `option:<id>` (on, off or inherit), `option:<id>:<setting>`, and `option:<id>:reason`, and turn
  into the matching `[checks.<id>]` tables.
- **Upgrade reports new options.** The upgrade report gains a New options section: any option not
  yet answered in this project, and any turned on without what it needs to run.
- **Generated blocks no longer count toward word limits.** A generated index's rows were counted
  as prose, so every decision or trap recorded shrank the word budget of the doc that indexes
  it. Only the words a person writes count now, as documented.
- **`check --workspace-only`** checks a registry workspace without its projects' checkouts, for
  the workspace's own CI: its docs, agents, generated blocks, registry and declared ID ranges.
  Every project is skipped, and the output names each one.
- **`tool-files`** (warn): says when git ignores a file the gate needs committed (its config,
  baseline, install record or `bin/` entry points), such as a `bin/` rule for build output that
  also matches `.context-gate/bin/`, and names the line to add and the `.gitignore` to add it
  to. Install and adopt list any such file first in their report, under "Needs a person before
  committing".
- **The always-excluded directories are left out at any depth, in any case.** `node_modules/`,
  `build/`, `dist/`, `.claude/` and the rest are never governed docs wherever they sit
  (`sub/build/notes.md` too) and however they are capitalized (`Build/`, on every OS), which is
  also what adopt measures.
- **A `docs` entry the gate can never govern is named.** An entry under an always-excluded
  directory (`.claude/x.md`, say) prints a warning on every command, naming the exclude that
  wins.
- **`check --path` no longer asks the governance root what git ignores.** A snapshot's files are
  asked of `--history-from`'s checkout, or the snapshot's own repository, so a project's own CI,
  whose copy of the workspace lacks the checkout, no longer reads its doc registry as stale.
- **Upgrade regenerates generated blocks** with the new engine and says how many changed, so a
  block the old engine rendered never reads as stale.
- **Adopt keeps the rows of an existing doc registry.** A doc it lists that the proposed
  `[projects] docs` globs miss (a project's `../AGENTS.md`, listed from `docs/INDEX.md`) is
  added to `docs` as an explicit path, so the first `index` keeps its row. A listed doc that can
  never be governed (no `doc_type` frontmatter, excluded, missing) is named in the adopt report,
  as is one whose path another project holds without `doc_type` (`docs` is shared, so adding it
  would govern that file too). The adopt installer's `measure --json`
  (`python3 -m govern.installer measure`) gains a `registry_rows` key listing the rows it
  read.
- **A widened list needs its reason to load.** Adding a value to an allow-list, removing one
  from a required list, or emptying an allow-list, with no entry for that list in the check's
  `reasons` table, was a `standard-overrides` warning; now the config does not load, and the
  error names the check, the list and the values. A profile's widening past the standard is
  held to the same rule. Under `require_reasons = false` it stays a warning.
- **A profile's `[dialect]` values are checked when it loads**, as a project's are: a value that
  is not one of the key's choices, or of the wrong type, is an error naming the profile and the
  key.
- **`[blocks] placeholder` is gone.** Earlier engines accepted the key but never read it; a
  config that still sets it is now an error that names it as no longer used.
- **The engine's own names are spelled in American English.** The check `licences` is now
  `licenses` (as is the key a conflict side lists licenses under), and the registry fact
  `licence` is now `license`, in messages, docs and the manifest alike. The old names still load
  wherever the new ones are accepted (a `[checks.*]` table in the config or the profile, a run
  order, `[repo]`, `[registry.keys]`, a `[projects] required_when` or `[blocks]
  registry_columns` key, the `registry` check's `required_keys`, a registry entry, an adopt
  answer, `options_answered`, `govern explain`, `govern options --record`), read as the new
  ones, and every command warns once per place that uses one (once per registry file). Both
  names in one place is an error. The upgrade report lists the warnings as new findings, under
  `config`. Adopt maps a registry that spells the fact `licence`
  (`[registry.keys] license = "licence"`) rather than asking for a rename.
- **writing-rules never flags the engine's own names used as names.** A check id, a parameter,
  a config table or key, a registry fact, or an old name that still loads, is skipped anywhere in
  the config, the profile's `principles.toml` and the registry file, and inside Markdown code (an
  inline code span or a fenced block), so a spelling rule holds whichever spelling the engine
  uses. Prose, and every other file the rules name, is checked in full, and a finding's count is
  the matches left.
- **writing-rules leaves out what the gate never governs, and takes `exclude`.** A `files` glob or
  path no longer reaches this tool's own directory (whose reports quote the configured rules),
  `.claude/`, or an always-excluded directory at any depth and in any case, nor a file git ignores
  (asked of the repository that holds it). `include_ignored` lists gitignored files to check
  anyway, such as drafts kept out of git. `exclude` lists globs to leave out as well; adding to it
  is a loosening, so it needs `reasons.exclude`. An entry that matched files but kept none is a
  warning. `files`, `exclude` and `include_ignored` match as `Path.glob` does, case-sensitive on
  every OS, with `\` read as `/`, a run of `/` as one and every `.` segment (`./docs`,
  `docs/./*.md`) dropped. A wildcard component matches a name in either Unicode form everywhere;
  a literal one does so only on a file system that treats the two forms as one. An entry that
  names no path (`.`, `./`, empty) or is absolute (`/docs/*.md`, `C:/docs/*.md`) is a load error.
- **writing-rules keeps a spelling a line marks as deliberate.** A line carrying
  `writing-rules: allow <text>` (or several texts, separated by commas), usually in a comment, is
  not counted for a rule whose `text` it names: for a spelling that must stay, such as an old name
  kept for back-compat. Only that line and those texts are exempt. The engine's own source marks
  its old names this way.

**Upgrading:**

- Run `/context-gate:upgrade`. It refreshes the generated blocks, so a doc now left out by the
  any-depth excludes drops out of its doc registry without turning the gate red.
- Then run `govern baseline` to lower the recorded word counts of files that carry a generated
  index.
- The upgrade rolls back, naming what to fix, if the config or its profile hits one of the new
  load errors:
  - **A project list widened with no reason** (the `standard-overrides` check warned about it):
    add a reason under `[checks.<id>.reasons]` in the config and upgrade again.
  - **A profile list widened past the standard with no reason:** add the reason in the profile's
    `principles.toml`, tag it, move the project's pin, then upgrade.
  - **A profile `[dialect]` value** that is not one of the key's choices, or of the wrong type:
    fix it in the profile.
  - **`[blocks] placeholder`** in the config: delete the line.
- A list is judged against what the project inherits. When a profile's new tag tightens a list, a
  project that sets that list wider with no reason stops loading once its pin moves to that tag:
  add the reason in the project's config then.
- Options need nothing further: run `/context-gate:options` or `govern options` to see the
  opt-in checks. A project that turns an option off when its profile turns it on is a loosening
  like any other, so `standard-overrides` now warns if it has no `reason`.
- writing-rules: a project whose `files` matched the tool's own reports, `.claude/`, an
  always-excluded directory or a gitignored file now sees fewer hits. A project that checks
  gitignored drafts adds them to `include_ignored`; an entry whose files are all gitignored warns
  and says so. A `files` glob spelled in another case than the files on disk no longer matches
  them on a case-insensitive file system; the entry warns, naming a file it reaches only in
  another spelling, so fix its case.
- writing-rules: a `files`, `exclude` or `include_ignored` entry that names no path (`.`, `./`)
  or is absolute (`/docs/*.md`, `C:/docs/*.md`, `\\server\docs`) now stops the config loading,
  naming the entry: remove it, or make it a glob relative to the governance root.
- `licences` → `licenses` and `licence` → `license`: the old names still work, with a warning
  naming each place that uses one; rename them. A registry file that other tools read too can
  keep `licence` by mapping it: `[registry.keys] license = "licence"`.
- The `licenses` check's messages changed spelling too: `<entry>: no license declared — the
  license rules depend on it` and `licenses: <a> and <b> coexist but <id> is not a recorded
  decision`. So does a `registry` finding for the fact: `<entry> is missing 'license'`, where it
  said `'licence'`. A script that matches any of these texts must change with them.

## 0.4.1 — 2026-09-21

- **A project checkout nested in a workspace is governed again.** Where a workspace's root
  `.gitignore` lists a project that is its own git repository, git questions about that project's
  files were answered by the root, so its docs read as ignored: `index` emptied its doc registry
  and the doc checks passed on no docs. Every git question now goes to the repository that holds
  the file.
- **A git hook's environment no longer redirects the gate.** `GIT_DIR`, `GIT_WORK_TREE` and the
  other repository variables a hook sets are dropped from every git call the engine makes.
- **`index` warns when it empties a block** that had entries.
- **A fresh adopt is green on a typical repo.** The tool's own `.context-gate/` and `.claude/` are
  never governed docs. With no `docs/` folder, adopt proposes `[projects] exclude` for the
  GitHub-facing files it finds (README, CHANGELOG, CONTRIBUTING, issue templates, …), with a note;
  a file that carries this tool's `doc_type` frontmatter stays governed.
- **`git-repo`** (warn): says when the project is not inside a git repository, where the checks that
  read git history or ignore rules check nothing.
- Adopt no longer prints a development-tree note when its engine version is already installed.

**Upgrading:** run `/context-gate:upgrade`. A workspace whose doc registry was emptied by 0.4.0:
restore the block from git, then run `govern index`.

## 0.4.0 — 2026-09-21

First release (Apache 2.0). context-gate is a governance gate for the documents AI agents read:
decision logs, traps, working files and agent definitions stay small, current and checkable.

- **Adopt from the marketplace.** `/plugin marketplace add SolrLabs/ai-context-gate`, then
  `/plugin install context-gate@context-gate` and `/context-gate:adopt`. Adopt measures the
  project, proposes its config and asks only what measurement cannot settle, then installs the
  gate green in one step. Everything it owns lives in `.context-gate/`; it writes the marketplace
  and plugin id into `.claude/settings.json` so teammates are offered the same plugin, and
  `--marketplace owner/repo` points a fork at itself.
- **The gate and the ratchet.** `.context-gate/bin/govern check` runs every check against the
  project's config: errors fail the gate, warnings do not. Size limits are ratcheted: each
  existing breach is recorded in a baseline, and only a new or growing one fails. Every check
  declares its level, parameters and the reason it exists; `govern explain` shows each setting
  and where it came from.
- **Profiles.** A shared `principles.toml` sits between the engine standard and each project,
  so a team sets its limits once; a project that loosens what it inherits says why.
- **Upgrades.** A project runs exactly the engine it pins. `/context-gate:upgrade` (or
  `.context-gate/bin/upgrade`) moves it to a newer release and writes a report of what changed,
  by check; an upgrade never turns a green project red. The gate prints one line when a newer
  release exists (`CONTEXT_GATE_NO_UPDATE_CHECK=1` turns the check off).
- **Uninstall.** `.context-gate/bin/uninstall` restores every file the tool changed, and
  `.claude/settings.json` byte for byte (or removes it when the tool created it); a file edited
  since install is reported as a conflict, and `--force` removes only the tool's own keys.

**Upgrading:** First release.
