---
doc_type: reference
purpose: How context-gate works for a user adopting it — what it governs, its pieces, how settings resolve, the ratchet, what it installs, and how engines are pinned and upgraded.
audience: human
load_when: adopting context-gate, or deciding how to configure or upgrade a governed project
last_reviewed: 2026-09-21
---

# How context-gate works

For anyone adopting context-gate in a project: what it checks, what it installs, and how its
settings are decided.

The full list of checks is in [checks.md](checks.md). Every setting is in
[configuration.md](configuration.md).

## What it governs, and why

A project that works with coding agents builds up records the agents read: decision logs,
notes on known traps, docs, working files, agent and skill definitions. Agents read those
records by index and pay for every word in tokens. A record that is out of date, too long,
unindexed or malformed costs every session that opens it, and it can send the agent the wrong
way. context-gate is a deterministic gate that keeps these records in a shape an agent can rely
on. It runs the same way on every machine and in CI.

| Record | What the gate keeps true |
|---|---|
| **Decision logs** | Entries are headed `## P-12 — Title` (a prefix, a number, any dash, a title), unique, in ascending order and inside the project's id range. Each carries a `**Status:**` (`locked`, `provisional` or `deferred`), the required fields (`**Rule:**` and `**Why:**`) and an optional `**Topic:**`, and stays short (250 words). A changed decision is rewritten in place; git keeps the old text. A replaced decision shrinks to a one-line pointer, `## P-3 — Replaced by P-40`, so citations still resolve. `next-id` prints the next free id, and gaps are never reused. |
| **Traps** | Known pitfalls, one entry each (`## T-3 — Title`) with a `**Bites when:**` line, in the working-files directory (`traps*.md`). A trap id lives in exactly one file. A generated index lists them all. |
| **Docs** | Every governed doc carries frontmatter (`doc_type`, `purpose`, `audience`, `load_when`, `last_reviewed`), is reviewed within 120 days if it is a control or reference doc, links only to files that exist, and is listed in the project's generated doc index when the project keeps one. The number of governed docs per project is bounded. |
| **Working files** | The working-files directory holds at most 12 files. A file whose status says it is finished should be deleted a week after it was last touched (git keeps it); the gate warns until it is, and one that was never committed is flagged until it is. A working file and the HANDOFF have word limits. |
| **Agents and skills** | Every agent definition pins its `name`, `description`, `model` and `effort`, and sets `omitClaudeMd` (a warning when missing); a turn cap, when set, stays under a configured ceiling, and prose never contradicts it. Every skill has a description. Hooks a rule depends on are wired. The Claude Code memory index stays short. |
| **Generated blocks** | Index tables inside docs (decision index, trap index, doc registry, agent roster, project registry) are generated between markers by `govern index`, and the gate fails when one is stale. |

Checks that only some projects need, such as fork hygiene, license conflicts, writing rules and
retired names, ship turned off or do nothing until they are configured. Each check's level
(`off`, `warn` or `error`) and parameters are yours to set.

## The pieces

| Piece | What it is | Where it lives |
|---|---|---|
| **The engine** | The `govern` Python package: the checks, the command line, the installer. Python 3.11 or later, standard library only. | `~/.local/share/context-gate/engines/<version>/`, one read-only directory per release |
| **The project's install** | The policy (`config.toml`), the ratchet baseline, and the scripts that run the pinned engine | `.context-gate/` at the project's governance root |
| **The plugin** (optional) | A Claude Code plugin with two skills, `/context-gate:adopt` and `/context-gate:upgrade`, and a session-start hook that shows the upgrade notice. It bundles the engine at its own version. | Installed by Claude Code from the `context-gate` marketplace |

The project's own records (its decision logs, traps, handoffs, and the generated
blocks inside them) are never inside `.context-gate/`. They belong to the project, and
uninstalling the tool never removes them.

The command-line tool is deterministic: it measures, checks and writes. The skills hold the
conversation: they ask the questions, explain the reports and price each choice before it is
made. They never parse the config or re-implement a check. Everything a skill does can be done
by hand with the commands below.

## Running the gate

Every command runs through the project's own entry point, which starts the engine version the
project pins:

```sh
python3 .context-gate/bin/govern check                 # the gate
python3 .context-gate/bin/govern check --project NAME  # one project of a workspace
python3 .context-gate/bin/govern index                 # regenerate every generated block
python3 .context-gate/bin/govern baseline              # lower the ratchet baseline
python3 .context-gate/bin/govern explain [CHECK]       # every effective setting, and where it came from
python3 .context-gate/bin/govern next-id --project NAME
python3 .context-gate/bin/govern show --project NAME P-12 T-3   # one entry, not the whole file
python3 .context-gate/bin/govern find --project NAME PATTERN    # which entries mention this
python3 .context-gate/bin/govern trap-add --project NAME --title T --bites B
python3 .context-gate/bin/govern principles            # the profile's doctrine document
```

`show --list` prints every entry's id and title, and `show --lines` prints `file:start-end`
instead of the body. `find` takes a case-insensitive regular expression. In a single repo,
`--project` takes the repo's name, which defaults to its directory name.

| Exit code | Meaning |
|---|---|
| 0 | Clean: no errors (warnings may be printed) |
| 1 | An error finding, or a problem that needs a person (an unreadable file, a refused baseline change) |
| 2 | Usage or configuration error: the config does not load, or the pinned engine is missing |

The output is one line per finding, `ERROR` or `warn`, then a status line per scope:

```text
  ERROR  ratchet: 'handoff_words:web' is a new breach at 1720, not in baseline.json — fix it, or if it is accepted for now run `govern baseline --allow-raise`
[FAIL] workspace  (1 errors, 0 warnings)
  ERROR  DECISIONS.md: duplicate decision id P-14
  warn   web: HANDOFF.md is 1720 words, over handoff-words max_words=1500 — current state only, not a log of sessions
[FAIL] web  (1 errors, 1 warnings)
[FAIL] total  (2 errors, 1 warnings)
```

`[dialect] report = "aggregate"` prints one list and one status line instead.

For a git pre-commit hook, `check --project NAME --path DIR --history-from REPO` checks a
directory swapped in for the project's own (the staged tree, say) and answers questions about
file history from `REPO`, since the snapshot has none.

## How settings are decided

Each check's level and parameters are resolved in layers. A later layer wins, key by key:

1. **The engine standard**: each check's own defaults, the same for every user.
2. **The profile** (optional): an operator's or team's shared settings, which every project that
   names the profile inherits. See [Profiles](#profiles).
3. **The project**: `.context-gate/config.toml`.

A setting applies to the whole governance root; there are no per-path overrides. To leave files
out of a check, use the check's own path parameters (such as `exempt` or `files`) or the layout
keys that choose governed docs (`[projects] docs` and `exclude`).

### Loosening needs a reason

A setting that relaxes a check past the engine standard carries a `reason`: a lower level than
the check's default, a limit moved in its looser direction (a higher word limit, say), or the
ratchet turned off for a check. Without one, the config does not load. Relaxing a rule is allowed, but it is done in the open, where the
next reader can see why.

```toml
[checks.decision-log]
max_words = 400
reason = "Entries here carry a short worked example."
```

Some differences are reported as warnings by the `standard-overrides` check instead of refused:

- a format setting (`[dialect]`) that differs from the standard, with no entry in
  `[dialect.reasons]`;
- a project setting that changes what its profile set, with no `reason`;
- a list widened past what the project inherits (a value added to an allow-list, a value removed
  from a required list, an allow-list emptied), with no entry for that list in the check's
  `reasons` table.

`[governance] require_reasons = false` turns the reason requirement off for teams that find it
heavy.

### Seeing the result

`govern explain` prints every setting of every check with the layer that set it, and the
reason where one was given. `govern explain CHECK` prints one check:

```text
decision-log  error (engine default) · ratchet on
  Decision entries are well-formed, unique, in range and in order, carry a known status and the required fields, and stay short.
  max_words = 400  (project; engine default 250)
  statuses = ["locked", "provisional", "deferred"]  (engine default)
  required_fields = ["Rule", "Why"]  (engine default)
  ascending = true  (engine default)
  reason: Entries here carry a short worked example.
```

Use it rather than reading the files by hand: it shows what the engine will actually run with.

## One repo, or a workspace

A governance root runs in one of two shapes.

**A single repo** governs itself. The config has no `[registry]` table. The repo is the one
project, described by an optional `[repo]` table (its name, id prefix and id range, and so on).
Its decision log, docs and working files are found relative to the repo root.

**A workspace** governs several projects from one place. The config's `[registry]` table names
a registry file that lists the projects, one entry each: its checkout directory, its tier, its
governance directory, its decision-id prefix and range. The workspace has its own decision log
and id range too. Project checks run once per project; workspace checks run once for the whole
root. `check --project NAME` checks one project alone. The governance root can sit outside the
repos it governs.

A config with both `[registry]` and `[repo]` is refused. Which projects are governed depends on
their tier: `[projects] governed_tiers` (default `["full"]`) lists the tiers that must carry the
doc set.

## The ratchet

Size limits (words per decision entry, words per working file, words in a HANDOFF, docs per
project) are reported as warnings. The ratchet is what gives them force. Each current breach is
recorded in the baseline, `.context-gate/baseline.json`, as a key and a size:

```json
{
  "decision_words:web:P-14": 412,
  "handoff_words:web": 1720
}
```

- **At install, every existing breach is recorded**, so a project starts green. Nobody has to
  fix years of history on day one, and the numbers stay visible as debt.
- **The gate fails only on regressions**: a breach that is not in the baseline, or one that grew
  past its recorded size.
- **The baseline only goes down on its own.** `govern baseline` lowers entries that shrank and
  removes those that no longer breach. It refuses to add or raise an entry (exit 1) and says so.
- **Accepting a bigger number is a deliberate act**: `govern baseline --allow-raise` records new
  and grown breaches too. Commit the baseline so the choice is reviewed like any other change.

The ratchet and config validation are the fixed core: neither can be turned off. A single
check's size breaches can be taken out of the ratchet with `ratchet = false` (a loosening, so it
needs a reason); its limit is then a warning only. A baseline that cannot be read is an error,
and nothing overwrites it: restore it from git.

## What is installed where

### In the project

| Path | What |
|---|---|
| `.context-gate/config.toml` | The project's policy |
| `.context-gate/baseline.json` | The ratchet baseline |
| `.context-gate/bin/govern` | The gate: runs the pinned engine |
| `.context-gate/bin/upgrade` | Moves the project to a newer engine |
| `.context-gate/bin/uninstall` | Removes the tool |
| `.context-gate/installed.toml` | Everything the install touched outside `.context-gate/`, so uninstall can reverse it |
| `.context-gate/README.md` | What each file is |
| `.context-gate/install-report.md`, `upgrade-report.md`, `adopt-report.md`, `migration-report.md` | What each operation found and changed |
| `.context-gate/backup/` | Originals of anything the install replaced or retired |
| `.context-gate/checks/` | Your extension checks, if you write any (by convention; see [Extensions](#extensions)) |

`bin/govern`, `bin/upgrade` and `bin/uninstall` are one script that acts on its own name.

Outside `.context-gate/`, the install may touch:

- **`.claude/settings.json`**, when the project opts in to the plugin:
  `enabledPlugins["context-gate@context-gate"] = true`, and the marketplace in
  `extraKnownMarketplaces["context-gate"]` (a GitHub source naming the public repository, or your
  fork). Commit the file so teammates get the plugin too. The original bytes are backed up first,
  and a BOM and CRLF line endings are kept.
- When installing by hand with `govern.installer install` (see engine/README.md):
  **an existing gate script** your skills or CI already call (`--entrypoint`): it is backed up and
  replaced by a stub that runs `bin/govern`, so every caller keeps working.
- Likewise by hand: **an existing baseline** moved into `.context-gate/` (`--migrate-baseline`), and **retired
  files** made obsolete by the new gate (`--retire`), each kept in `backup/`.

Install is refused over an existing install, and rolled back if the policy does not load.

### In your home directory

| Path | What |
|---|---|
| `~/.local/share/context-gate/engines/<version>/` | Installed engines, one read-only directory per release, shared by every project. A release is never overwritten. |
| `~/.cache/context-gate/profiles/` | Profiles fetched from git, one directory per source and tag |
| `~/.cache/context-gate/releases.json` | The upgrade notice's cache of release tags |

These are user-level caches. They are not recorded in `installed.toml`, and uninstall leaves
them.

### Uninstalling

```sh
python3 .context-gate/bin/uninstall [--force]
```

Uninstall reverses what `installed.toml` lists, then deletes `.context-gate/`. It first looks for
every conflict (a stub edited since install, a path it must restore that is now occupied,
settings edited or removed since install) and changes nothing if it finds one. `--force`
restores the originals over the conflicts.

For `.claude/settings.json`:

- If the file is still exactly what the tool wrote, its original bytes come back, or it is
  removed if there was none before (with `.claude/`, if the tool created that directory and it is
  empty).
- If it was edited since, that is a conflict. With `--force`, only the tool's own keys are
  undone, each back to its previous value, and your other edits stay.
- If it was deleted since, that is a conflict too. With `--force`, the original comes back.

## Engine versions

### Pinning

`config.toml` pins the exact engine it runs:

```toml
[governance]
engine = "0.4.0"
source = "https://github.com/SolrLabs/ai-context-gate.git"
```

`bin/govern` runs exactly that version from `~/.local/share/context-gate/engines/`. The engine
refuses to run under any other pin. The same commit gets the same engine on every machine and in
CI, and moving to a new engine is always a deliberate step. (A two-part pin such as `"0.4"` runs
the newest installed release in that series, with a notice to pin exactly.)

### Bootstrapping from the source

If the pinned engine is not installed and `[governance] source` is set (a git URL or path), the
first run of `bin/govern` installs it: it clones the tag `v<version>` from the source, checks
that the tag carries that engine version, and copies `engine/govern` into the engines directory,
read-only. A fresh clone or a CI runner needs only Python 3.11 and git. Without a source, a
missing engine is an error that says what to install.

`$GOVERN_ENGINE`, set to an engine directory, overrides the pin. It is meant for developing the
engine itself.

### Released engines

A released engine carries a stamp, `govern/RELEASE`, holding `v<version>`. The plugin bundles a
stamped engine. When adopt runs from such an engine and that version is not installed yet, it
installs it into the engines directory, so the new gate runs with no download. An engine
without the stamp (a development checkout) is never installed this way, so it can never stand in
for a real release.

### Upgrading

After every command, the gate prints one line when a newer engine exists, installed on this
machine or released at `[governance] source`. The source is asked at most once a day, with a
short timeout, and the answer is cached. The line is colored in a terminal and plain through a
pipe, so an agent reads the same words. When the plugin is present, its session-start hook
shows the same notice when a session opens. `CONTEXT_GATE_NO_UPDATE_CHECK=1` keeps the gate off
the network, and `NO_COLOR` turns off the color.

To upgrade, use `/context-gate:upgrade` in Claude Code, or:

```sh
python3 .context-gate/bin/upgrade              # the newest release
python3 .context-gate/bin/upgrade --to X.Y.Z   # a specific one
```

The upgrade installs the target engine from the source if needed, rewrites the pin, refreshes
`bin/`, and writes `upgrade-report.md`: the old engine's findings against the new one's, each new
finding under the check that raised it with that check's rationale, the release notes for every
version in between, and every setting that differs from the defaults. If the policy does not
load on the new engine, the pin is put back and nothing changes.

An upgrade does not add entries to the baseline. For each new finding, decide: fix it, adjust
the setting (with a reason if it loosens), turn the check off (with a reason), or accept the
current breaches with `govern baseline --allow-raise`. The upgrade skill walks through these one
at a time, with the cost of each.

## Adopting a project

With the plugin, run `/context-gate:adopt` in Claude Code. Without it, run the installer from any
copy of the engine (the `engine/` directory of a clone of the public repository, say):

```sh
PYTHONPATH=<engine dir> python3 -P -m govern.installer adopt --root .            # propose
PYTHONPATH=<engine dir> python3 -P -m govern.installer adopt --root . --answers answers.toml
PYTHONPATH=<engine dir> python3 -P -m govern.installer adopt --root . --answers answers.toml --apply
```

Adopt measures the project (its registry if any, decision logs, traps, working files, docs,
generated-block markers) and proposes a config. The proposal covers layout
only: where things are. Check levels and limits come from the engine standard and your profile.
Where the measurement allows two readings, adopt asks instead of guessing, and exits 3 while any
question is open. It always asks the shape (`single` or `workspace`), and for a workspace, which
repos to govern. Answers go in a TOML file, one `key = "option"` per line.

`--apply` refuses while a question is open or while a file it would touch has uncommitted
changes. Then it installs `.context-gate/`, opts the project in to the plugin, creates missing
decision logs, converts logs and traps in another format to the standard one where needed,
regenerates every block, records existing breaches in the baseline, runs the gate and writes
`adopt-report.md`. It never commits. Review the changes and commit them yourself.

Useful options: `--profile SOURCE` names a profile, `--source none` leaves `[governance] source`
unset, and `--marketplace OWNER/REPO` points teammates at a fork's marketplace (the source then
defaults to that fork).

Teammates install the plugin once, in Claude Code: `/plugin marketplace add
SolrLabs/ai-context-gate`, then `/plugin install context-gate@context-gate`. The gate itself needs
nothing installed: it fetches its engine from the source.

## Profiles

A profile holds the settings an operator or team wants in every project, so no project has to
copy them and personal preferences never have to become the public standard. A project names
one:

```toml
[governance]
engine = "0.4.0"
profile = "git@github.com:your-org/principles.git#v1"
```

The source is a directory (absolute, or relative to the project root) or a git URL pinned to a
tag or branch after `#`. A git profile with no `#ref` is refused, so every machine reads the same
settings. It is fetched once into `~/.cache/context-gate/profiles/` and read from there after,
offline too.

A profile directory holds:

- **`principles.toml`**: `[checks.<id>]` and `[dialect]` settings, in the same form as a
  project's config. Layout (where a project keeps its files) is never a profile's business, and a
  profile that sets it is refused.
- **`PRINCIPLES.md`** (optional): prose doctrine for the people and agents working in the
  project. `govern principles` prints it. A project that keeps its own
  `.context-gate/PRINCIPLES.md` uses that copy instead.

To keep principles inside one project, put `principles.toml` in `.context-gate/` and set
`profile = ".context-gate"`.

## Extensions

A rule that fits no built-in check can be written as an extension: a Python file in a directory
the config lists in `[governance] extensions`. It registers with the same `@check` decorator the
built-in checks use, and must declare the same things: its id, `since`, scope, summary, question
and rationale, and typed parameters.

```python
from govern.findings import Findings
from govern.manifest import check


@check("no-todo", scope="workspace", since="1.0.0", origin="my-project",
       summary="AGENTS.md carries no TODO.",
       question="Should a TODO in AGENTS.md fail the gate?",
       rationale="AGENTS.md is read by every session; a TODO there is read as an instruction.")
def no_todo(ctx, params):
    f = Findings()
    if "TODO" in (ctx.root / "AGENTS.md").read_text(encoding="utf-8"):
        f.error("AGENTS.md: TODO")
    return f
```

```toml
[governance]
engine = "0.4.0"
extensions = [".context-gate/checks"]
```

A workspace check is called once as `fn(ctx, params)`; a project check is called once per
project as `fn(ctx, params, scope)`. An extension's settings go under `[checks.<id>]` like any
other, and `govern explain` marks it as an extension. Extensions run with the same trust as the
project's own scripts: they are ordinary Python, loaded from the project.
