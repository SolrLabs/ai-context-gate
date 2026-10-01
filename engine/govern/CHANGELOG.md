# context-gate — release notes

Newest first. Each release says what changed and, under **Upgrading**, anything a project has to
do or decide. The upgrade report quotes every entry between the engine a project ran and the one
it upgraded to.

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
