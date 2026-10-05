---
doc_type: reference
purpose: Every table and key .context-gate/config.toml accepts, with types, defaults and examples.
audience: human
load_when: writing or changing a project's config.toml or a profile's principles.toml
last_reviewed: 2026-09-25
---

# Configuration reference

For anyone writing or changing a project's `.context-gate/config.toml`: every table and key the
engine accepts.

[how-it-works.md](how-it-works.md) explains the model behind these settings: layers, the
ratchet, single repo or workspace. [checks.md](checks.md) lists every check and its parameters.

## The file

`.context-gate/config.toml` sits at the governance root. Every command validates it before
doing anything else, and validation is strict:

- An unknown table, an unknown key, or a key in the wrong table is an error. A setting the engine
  would silently ignore is a limit that never fires, so there are none.
- Every value has a type (string, integer, boolean, list, table), and a value of the wrong type
  is an error.
- A config that does not load stops every command with exit code 2 and a message naming the key.

The smallest config pins the engine and nothing else. Everything else has a default:

```toml
[governance]
engine = "0.4.0"
```

To see the settings a project actually runs with, and which layer set each one, run
`python3 .context-gate/bin/govern explain`. Paths in the config use `/` on every platform.

The tables:

| Table | What it holds |
|---|---|
| [`[governance]`](#governance) | The engine pin, where to fetch engines, the profile, extensions |
| [`[repo]`](#repo-a-single-repo) | Facts about a single repo (no registry) |
| [`[registry]`](#registry-a-workspace) | Where a workspace's registry file is and how to read it |
| [`[workspace]`](#workspace) | The workspace's own docs, decision log and baseline |
| [`[projects]`](#projects) | Where each project keeps its decision log, docs and working files |
| [`[blocks]`](#blocks) | Generated index blocks |
| [`[dialect]`](#dialect) | Format settings |
| [`[checks]`](#checks) | Check order, and each check's level and parameters |

## `[governance]`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `engine` | string | required | The exact engine version this project runs, like `"0.4.0"`. The engine refuses to run under any other pin. A two-part pin such as `"0.4"` runs the newest installed release in that series, with a notice to pin exactly. |
| `source` | string | none | A git URL or path holding the engine, with a `v<version>` tag per release. `bin/govern` installs a missing pinned engine from it, `bin/upgrade` finds new releases there, and the upgrade notice checks it. |
| `profile` | string | none | The profile this project inherits: a directory (absolute, or relative to the governance root) or a git URL pinned with `#<tag>`. See [Profiles](#profiles). |
| `schema` | integer | `1` | The config schema version. This engine reads schema 1 only. |
| `extensions` | list of strings | `[]` | Directories, relative to the governance root, whose `*.py` files register extension checks. Each directory must exist. See [Extensions](#extensions). |
| `require_reasons` | boolean | `true` | Whether a loosened setting needs a `reason` (see [Loosening](#loosening-needs-a-reason)). |

```toml
[governance]
engine = "0.4.0"
source = "https://github.com/SolrLabs/ai-context-gate.git"
schema = 1
require_reasons = true
```

## One repo or a workspace

A config with no `[registry]` table governs a single repo: the governance root is the one
project. A config with a `[registry]` table governs a workspace of the projects its registry file
lists. Setting both `[registry]` and `[repo]` is an error.

### `[repo]`: a single repo

Optional. Every key is a string, and every key is optional.

| Key | Default | Meaning |
|---|---|---|
| `name` | the root directory's name | The project's name in output and for `--project` |
| `dir` | `"."` | The checkout, relative to the root |
| `tier` | the first of `[projects] governed_tiers` | The project's tier |
| `governance` | `"."` | The governance directory, relative to the root. `""` opts the repo out of the checks that need one. |
| `id_prefix` | none | The decision-id prefix, like `"P"` for `P-12` |
| `id_range` | none | The decision numbers this log may use, `"LO-HI"` |
| `handoff` | none | The HANDOFF file, relative to the root |
| `role` | none | Used by `checkout-hygiene` and `licenses` to pick entries by role |
| `license` | none | The project's license, read by `licenses` |
| `upstream` | none | The upstream remote URL, read by `checkout-hygiene` |
| `purpose`, `runtime_gate` | none | Descriptive facts, shown in a registry column if one is configured |

A project that keeps a decision log needs `id_prefix` and `id_range` (the `registry` check
reports it otherwise).

```toml
[governance]
engine = "0.4.0"

[repo]
name = "web"
id_prefix = "P"
id_range = "1-999"
handoff = "docs/HANDOFF.md"

[projects]
decision_log = "docs/DECISIONS.md"
working_dir = "docs"
docs = ["docs/**/*.md", "README.md"]
```

### `[registry]`: a workspace

| Key | Type | Default | Meaning |
|---|---|---|---|
| `file` | string | `"registry.toml"` | The registry file, relative to the governance root. It must exist. |
| `entries` | string | `"project"` | The name of the array of tables in the registry file that lists the projects (`[[project]]`) |
| `workspace` | string | `"workspace"` | The name of the table in the registry file that holds the workspace's own `id_prefix` and `id_range` |
| `keys` | table | each fact under its own name | Where each fact lives in an entry, as a dotted path: `handoff = "profile.handoff"` reads `[project.profile] handoff`. Only the facts listed under [`[repo]`](#repo-a-single-repo) can be mapped. |
| `skip` | list of strings | `[]` | Entries, by name, this root leaves out entirely. Each must be an entry in the file. |

Each registry entry carries the same facts as `[repo]`, with no defaults: `name`, `dir` and
`tier` are required (the `registry` check's `required_keys`), and a project in a governed tier
with a `governance` directory needs `id_prefix` and `id_range`. A fact found in an entry
somewhere other than where `[registry.keys]` says the engine reads it is an error, so a
misplaced key never goes unread.

```toml
# fragment: a registry file, projects.toml
[workspace]
id_prefix = "W"
id_range = "1-99"

[[project]]
name = "web"
dir = "web"
tier = "full"
governance = "governance/web"
id_prefix = "A"
id_range = "100-199"
license = "MIT"

[[project]]
name = "docs-site"
dir = "docs-site"
tier = "registered"
```

```toml
[governance]
engine = "0.4.0"

[registry]
file = "projects.toml"
entries = "project"
skip = ["docs-site"]

[registry.keys]
handoff = "profile.handoff"
```

## `[workspace]`

The workspace scope is the governance root as a whole. In a single repo it still exists, for
the checks that look at the root (agents, skills, generated blocks, the ratchet).

| Key | Type | Default | Meaning |
|---|---|---|---|
| `label` | string | `"workspace"` | The workspace's name in output, and the `--project` value that means the workspace's own log in `next-id`, `show` and `find` |
| `docs` | list of strings | `[]` | Globs, relative to the root, of the workspace's own governed docs. They follow the same frontmatter rules as project docs. |
| `required_docs` | list of strings | `[]` | Paths, relative to the root, that must exist |
| `decision_log` | string | `"governance/DECISIONS.md"` in a workspace; none in a single repo | The workspace's own decision log, relative to the root |
| `agents_dir` | string | `".claude/agents"` | Where agent definitions live |
| `baseline` | string | `".context-gate/baseline.json"` | The ratchet baseline |

When the workspace and a project resolve to the same file (a single repo's one decision log,
say), the file is checked once, by the project that contains it.

```toml
[governance]
engine = "0.4.0"

[registry]
file = "projects.toml"

[workspace]
label = "hub"
decision_log = "governance/DECISIONS.md"
docs = ["AGENTS.md", "governance/*.md"]
required_docs = ["AGENTS.md", "governance/DECISIONS.md"]
```

## `[projects]`

Where every project keeps its records. Paths are relative to each project's governance directory.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `governed_tiers` | list of strings | `["full"]` | Tiers whose projects carry the doc set and are governed |
| `docs` | list of strings | `["**/*.md"]` | Globs of the governed docs |
| `exclude` | list of strings | `[]` | Globs of docs to leave out, matched against the path relative to the governance directory, in addition to the excludes that always apply (below) |
| `required_docs` | list of strings | `[]` | Docs every governed project must carry |
| `required_when` | list of tables | `[]` | Docs required only when a project has a given fact: `{ key = "handoff", docs = ["working-files/HANDOFF.md"] }`. Both keys are required, and `key` must be one of the facts listed under [`[repo]`](#repo-a-single-repo). |
| `decision_log` | string | `"DECISIONS.md"` | The project's decision log |
| `working_dir` | string | `"working-files"` | The working-files directory |
| `trap_glob` | string | `"traps*.md"` | Trap files, inside the working-files directory |
| `trap_prefix` | string | `"T"` | The trap-id prefix, as in `T-3` |

Governed docs never include files under `node_modules/`, `.venv/`, `venv/`, `vendor/`, `dist/`,
`build/`, `target/`, `.git/`, `.context-gate/` (the tool's own files and reports) or `.claude/`
(agents and skills have checks of their own), at any depth (`sub/node_modules/x.md` too) and
in any case (`Build/` too, on every OS), or files git ignores; adopt's measurement skips the same directories. These always apply, even to
a `docs` glob such as `**/*.md` that would otherwise match them, and `exclude` adds to them. The
workspace's own `[workspace] docs` globs leave out the same directories. A `docs` entry under one
of them (`.claude/x.md`, say) is never governed, and every command warns about it once, naming
the entry and the exclude that wins. When adopt finds no docs
directory, it proposes an `exclude` listing the repo's GitHub-facing files that have no
frontmatter (`README.md`, `CHANGELOG.md` and the like), which you can edit.

```toml
[governance]
engine = "0.4.0"

[projects]
governed_tiers = ["full"]
docs = ["**/*.md"]
exclude = ["archive/**"]
required_docs = ["DECISIONS.md", "INDEX.md"]
required_when = [{ key = "handoff", docs = ["working-files/HANDOFF.md"] }]
decision_log = "DECISIONS.md"
working_dir = "working-files"
trap_glob = "traps*.md"
trap_prefix = "T"
```

## `[blocks]`

A generated block is a table between two HTML-comment markers in a doc. `govern index` rewrites
it, and the `generated-blocks` check fails when one is missing or out of date. The markers carry
the block's id and the [`[dialect] markers`](#dialect) prefix:

```markdown
<!-- gov:generated:start id=decision-index -->
<!-- gov:generated:end id=decision-index -->
```

| Key | Type | Meaning |
|---|---|---|
| `workspace` | list of tables | Blocks in the workspace's own docs. Each needs `id` and `file` (relative to the root) and may set `render`. |
| `project` | list of tables | Blocks in every governed project. Each needs `id` and exactly one of `file` or `glob` (relative to the governance directory), and may set `render` and `sources`. |
| `registry_columns` | list of tables | The columns of the `registry` block |

`render` names what the block shows and defaults to the block's `id`:

| Where | `render` | Shows |
|---|---|---|
| `workspace` | `agent-roster` | Every agent: model, effort, turn cap, description |
| `workspace` | `registry` | Every registry entry, in the configured columns |
| `workspace`, `project` | `decision-index` | Every decision in the file, grouped by `**Topic:**` |
| `project` | `doc-registry` | Every governed doc, grouped by doc type, with a working file's status |
| `project` | `trap-index` | Every trap: id, title and when it bites |

A `trap-index` block may set `sources`, a glob of more trap files (relative to the governance
directory) whose entries the index also lists. Only the block's own file needs the markers. Globs
must be relative, never absolute.

Each `registry_columns` table has a `header` (required), a `key` (the registry fact to show) and a
`format`:

| `format` | Shows |
|---|---|
| `plain` (default) | The fact named by `key` (required) |
| `dir` | The entry's `dir`, as a directory |
| `github-slug` | `owner/repo` from a GitHub SSH URL in the fact named by `key` (default `upstream`) |
| `id-range` | The entry's prefix and range, like `A-100-199` |

```toml
[governance]
engine = "0.4.0"

[registry]
file = "projects.toml"

[blocks]
workspace = [
  { file = "governance/DECISIONS.md", id = "decision-index" },
  { file = "AGENTS.md", id = "agents", render = "agent-roster" },
  { file = "README.md", id = "registry" },
]
project = [
  { file = "DECISIONS.md", id = "decision-index" },
  { file = "INDEX.md", id = "doc-registry" },
  { file = "working-files/traps.md", id = "trap-index", sources = "working-files/traps-*.md" },
]
registry_columns = [
  { header = "Project", key = "name" },
  { header = "Checkout", format = "dir" },
  { header = "Decisions", format = "id-range" },
  { header = "Upstream", key = "upstream", format = "github-slug" },
]
```

## `[dialect]`

Format settings. The defaults are the standard. A project may keep a different format, but the
`standard-overrides` check warns until the difference has a reason in `[dialect.reasons]`.

| Key | Default | Other value | Meaning |
|---|---|---|---|
| `decision_heading` | `"any-dash"` | `"em-dash"` | Which dash separates an entry's id from its title. `any-dash` accepts an em dash, an en dash or a hyphen, with spacing optional; `em-dash` requires ` — `. The examples in these docs use a hyphen (`## D-12 - Title`); an em or en dash is read too. A heading-shaped line that fails the grammar is an error either way. |
| `next_id` | `"max-plus-one"` | `"first-free"` | Whether `next-id` prints the highest id plus one, or reuses the first gap. A reused gap can collide with a deleted id that is still cited. |
| `id_overlap` | `"prefix-aware"` | `"prefix-blind"` | Whether id ranges are compared only within the same prefix, or across prefixes too |
| `agent_turns_prose` | `"must-match"` | `"forbid"` | Whether an agent's prose may state a turn count that matches its `maxTurns`, or may state none at all |
| `finished_age` | `"ge"` | `"gt"` | Whether a finished working file is flagged when its age reaches `max_days`, or only once it passes it |
| `report` | `"per-scope"` | `"aggregate"` | Whether `check` prints findings and a status line per scope, or one list and one status line |
| `markers` | `"gov"` | any string | The generated-block marker prefix. This is layout, not format: it needs no reason. |
| `reasons` | `{}` | | A reason per key above, for keeping a non-standard format |

```toml
[governance]
engine = "0.4.0"

[dialect]
decision_heading = "em-dash"
markers = "docs"

[dialect.reasons]
decision_heading = "Other tools here parse these headings with an em dash."
```

## `[checks]`

### Order

| Key | Type | Meaning |
|---|---|---|
| `workspace_order` | list of strings | The order workspace checks run and report in |
| `project_order` | list of strings | The order project checks run in, per project |

Each names check ids. A check not listed runs after the listed ones, in the default order. A name
that is not a registered check is an error.

### `[checks.<id>]`

One table per check, named by the check's id. [checks.md](checks.md) lists every id and its
parameters.

| Key | Type | Meaning |
|---|---|---|
| `level` | string | `"off"`, `"warn"` or `"error"`. At `warn`, the check's errors become warnings. The fixed-core checks (`registry`, `ratchet`) cannot be `off`. |
| `<param>` | the parameter's type | Replaces the parameter's value |
| `extend_<param>` | list | For a list parameter (or a list of tables): adds these items to what the earlier layers set, instead of replacing it |
| `reason` | string | Why this check is loosened (see below) |
| `reasons` | table | A reason per list parameter, for widening that list |
| `ratchet` | boolean | For a check whose size breaches the ratchet tracks (`decision-log`, `doc-frontmatter`, `governed-doc-count`, `handoff-words`): `false` takes them out of the ratchet |

An unknown key, or a parameter value of the wrong type, is an error that lists the keys the check
knows. An enumerated parameter must be one of its values. A list of tables is checked item by
item for unknown and missing keys.

```toml
[governance]
engine = "0.4.0"

[checks]
project_order = ["decision-log", "doc-frontmatter"]

[checks.working-file-count]
level = "error"
max_files = 8

[checks.writing-rules]
level = "error"
files = ["docs/public/**/*.md"]
rules = [
  { text = "utilize", use = "use" },
  { text = "e.g.", use = "for example", level = "warn" },
]

[checks.hooks-wired]
level = "error"
extend_hooks = [{ script = ".claude/hooks/guard.py", event = "PreToolUse", matcher = "Bash" }]
```

`writing-rules` never flags one of the engine's own names where it is used as a name, since a
project cannot rename it: a check id, a parameter, a config table or key, a registry fact, or an
old name that still loads (see [Renamed names](#renamed-names)). A match counts as used as a name
when the run of letters, digits, `_` and `-` around it is one of those names, and it sits
anywhere in the config, the profile's `principles.toml` or the registry file, or inside a Markdown
inline code span or fenced block. Prose, and every other file, is checked in full: the project's
own code is flagged, and so is a code span that is not one of the engine's names. The count in a
finding is the matches left once those are skipped.

A file under this tool's own directory (`.context-gate/`, whose reports quote the configured rules
back), `.claude/` or an always-excluded directory (`build/`, `node_modules/` and the rest, at any
depth and in any case) is never checked, whether a glob or an explicit path names it. Nor is a
file git ignores (asked of the repository that holds it, a nested checkout's own included),
unless `include_ignored` names it: list drafts kept out of git there to check them anyway. An
entry that matched files but kept none of them is reported as a warning, with why. `exclude` lists
more globs to leave out; adding to it loosens the check, so it needs its reason in `reasons`.
Adding to `include_ignored` checks more, so it needs none. All three lists are globs relative to
the governance root, matched as `Path.glob` matches (`*` within one directory, `**` across any
number) and case-sensitive on every OS; `\` reads as `/`, a run of `/` as one, and a `.` segment
(`./docs`, `docs/./*.md`) is dropped. A wildcard component matches a name in either Unicode form
(composed or decomposed) everywhere; a literal component does so only on a file system that
treats the two forms as one. An entry that names no path (`.`, `./`, empty) or is absolute
(`/docs/*.md`, `C:/docs/*.md`) stops the config loading, and one that finds files only in another
case (`Notes/*.md` for `notes/`) warns, naming one. For example:

```toml
[governance]
engine = "0.5.0"

[checks.writing-rules]
level = "error"
files = ["**/*.md", "**/*.py", "outbound/*.md"]
exclude = ["tests/fixtures/**"]
include_ignored = ["outbound/*.md"]
reasons = { exclude = "Fixtures spell the old names on purpose." }
```

A spelling that must stay, such as a deprecated name kept for back-compat, is kept by a marker on
the same line: `writing-rules: allow <text>`, or several texts separated by commas, usually in a
comment. A rule whose `text` the marker names (in any case, with or without quotes or backticks
around it) is not counted on that line, the marker's own mention included; the next line, and any
other rule's text on the marked line, are checked as usual. The list runs to the end of the line
or of the comment it sits in (`-->`, `*/`), so put no note after it on that line. A marker naming
a text no rule has does nothing.

```python
RENAMED = {"licence": "license"}  # writing-rules: allow licence
```

```markdown
The old `Licence` heading stays for inbound links. <!-- writing-rules: allow licence -->
```

### Loosening needs a reason

A setting looser than the engine standard needs a `reason` in the same check's table, or the
config does not load:

- a `level` lower than the check's default level;
- a number moved in its looser direction (a higher word limit, say; [checks.md](checks.md) shows
  each parameter's direction);
- `ratchet = false`.

```toml
[governance]
engine = "0.4.0"

[checks.decision-log]
max_words = 400
reason = "Entries here carry a short worked example."

[checks.memory-index]
level = "off"
reason = "Nobody here uses Claude Code's memory."
```

Widening a list (adding a value to an allow-list, removing one from a required list, or emptying
an allow-list where empty means anything goes) needs that list's own entry in the check's
`reasons` table, or the config does not load. The error names the check, the list and the values
added or removed. A `reason` written for one setting never excuses another. A profile that widens
a list past the engine standard needs the same entry in its own table.

```toml
[governance]
engine = "0.4.0"

[checks.doc-frontmatter]
extend_doc_types = ["runbook"]
reasons = { doc_types = "Runbooks are a doc type of their own here." }
```

The same check warns when a project changes a setting its profile set without a `reason`.
`[governance] require_reasons = false` removes the requirement for a `reason` and for a
`reasons` entry; the `standard-overrides` check warns instead, a widened list included.

## Running a beta locally

You can run a locally installed prerelease in one project, on one machine, without touching the
committed pin, CI or anyone else's checkout. `.context-gate/local.toml` is the switch:

```toml
# fragment: local.toml, not config.toml
[governance]
engine = "0.6.0-beta.1"
plugins_before = { "context-gate@context-gate" = true }

[checks.writing-rules]
level = "error"
```

- `[governance] engine` names the beta to run. It must be a beta version (`X.Y.Z-beta.N`).
- `[governance] plugins_before` records the plugin settings `govern beta on` found, so
  `govern beta off` can put them back. `govern beta on` writes it; do not write it by hand.
- `[checks.*]` tables hold the beta's own settings. They merge above the project's config under the
  same rules: a loosening needs its reason, and an unknown check or setting is an error. Layout
  tables (`[projects]`, `[registry]`, and so on) are a load error here.

The file is read only by the engine it names. Any other engine ignores it, so a stable engine never
sees a beta's settings. A committed `[governance] engine` pin can never be a beta; the engine
refuses one.

It is never committed. The `local-layer` check fails the gate when git tracks the file, so a beta
pin cannot reach CI or other clones. `govern beta on` adds `.context-gate/local.toml` and
`.claude/settings.local.json` to `.git/info/exclude` (never a committed `.gitignore`) when git does
not already ignore them.

A beta comes from a `vX.Y.Z-beta.N` tag. Install its engine and plugin from a clone of the
repository first, with `python3 tools/release/install-engine.py vX.Y.Z-beta.N` and
`python3 tools/release/install-plugin.py vX.Y.Z-beta.N`. The beta plugin is the local install,
`context-gate@skills-dir`; the stable plugin is the marketplace one, `context-gate@context-gate`.

Betas are published as prereleases of the repository, tagged `vX.Y.Z-beta.N`. No project is ever
offered one: an upgrade, the marketplace plugin and the upgrade notice only ever name a release.
To run a beta, fetch the tags in a clone of the repository, install the beta from its tag, then
turn it on in the project. A beta may break. `govern beta off` returns the project to its pinned
release.

```sh
git fetch --tags                                           # in a clone of the repository
python3 tools/release/install-engine.py vX.Y.Z-beta.N
python3 tools/release/install-plugin.py vX.Y.Z-beta.N
python3 .context-gate/bin/govern beta on                   # in the project
```

Four commands, run through the project's gate:

```sh
python3 .context-gate/bin/govern beta                      # show the state
python3 .context-gate/bin/govern beta on                   # switch to the newest installed beta
python3 .context-gate/bin/govern beta on X.Y.Z-beta.N      # switch this project to that beta
python3 .context-gate/bin/govern beta off                  # switch back
```

`govern beta on` checks before it writes anything. It refuses, changing nothing, when the version
is not a beta, when that engine or the local plugin is not installed at that version, or when the
project has no `.claude/` directory. It then writes `local.toml`, sets `enabledPlugins` in
`.claude/settings.local.json` to turn the beta plugin on and the stable plugin off, and keeps every
other key in that file. `govern beta off` removes `local.toml`, puts the two plugin keys back as it
found them, and works even when the beta engine is broken or gone. With no beta on, it says so and
touches nothing.

`govern beta on` without a version takes the newest beta engine installed, ordered by number, so
`beta.10` is above `beta.9`. It then runs the same checks as with the version named. It refuses
when no beta engine is installed, and when the newest one is a beta of a release the project
already runs (`0.6.0-beta.4` with a pin of `0.6.0` or `0.6`). Name the version to switch to that
beta anyway.

`govern beta off` removes `local.toml` whole. When the file holds anything besides its
`[governance]` table, `beta off` prints those lines after its usual output, so you can copy what
you want to keep into `.context-gate/config.toml`. A file it cannot parse is printed whole.

With another beta already on, `govern beta on` switches to the new one. It changes only the
`engine` value in `local.toml` and keeps the rest of the file: your `[checks.*]` tables, your
comments and `plugins_before`, so a later `govern beta off` still puts back the plugin settings from
before the first beta. If it cannot find one plain `engine = "…"` line under `[governance]` to
change, it changes nothing and asks you to edit that line by hand. It also changes nothing when
`local.toml` is read-only or cannot be read, and says which.

Only one beta plugin is installed on a machine at a time. Once a newer beta is installed, a project
whose `local.toml` still names the older one runs the older engine with the newer plugin. The
session start and `govern beta` then add one line:
`context-gate: beta B is installed (this project runs beta A): govern beta on B`. A newer release
is announced first. A project with no beta on is never told about a beta.

Plugins load when a session starts, so restart the Claude Code session after `beta on` or
`beta off`. If both plugins are enabled anyway, every hook runs twice; the beta plugin's session
start says so, and so does `govern beta`.

While a beta runs, every command prints one line saying so. If the beta engine is not installed,
or `local.toml` cannot be read or names no beta, the gate runs the committed pin instead, prints
one line saying why, and does not fail. Run `govern beta off` to clear the file.

Run `govern beta off` before `bin/uninstall`. Uninstall does not know about `local.toml`, and
the beta plugin keys would stay in `.claude/settings.local.json`.

## Renamed names

Engine 0.5.0 spells its own names in American English: the check `licences` is now `licenses`
(and so is the key a `licenses` conflict side lists licenses under), and the registry fact
`licence` is now `license`. An old name still loads, read as the new one, wherever the new one
is accepted: a `[checks.*]` table in the config or the profile, a run order, `[repo]`,
`[registry.keys]`, a `[projects] required_when` or `[blocks] registry_columns` key, the
`registry` check's `required_keys`, a registry entry, `govern explain` and
`govern options --record`. Every command then warns once per place (once per registry file,
naming its entries), saying what to rename. Setting both names in one place is an error. A
registry file other tools read too can keep `licence` with no warning by mapping it:
`[registry.keys] license = "licence"` (adopt proposes that mapping for such a registry).

## Options

An **option** is a check that ships `default = "off"` — today, `writing-rules`, `hooks-wired`,
`checkout-hygiene`, `licenses` and `usage`. Run `python3 .context-gate/bin/govern options` to see
every option's state, or `/context-gate:options` in Claude Code to change them.

| State | Project (`config.toml`) | Profile (`principles.toml`) |
|---|---|---|
| On | `level = "error"` | `level = "error"` |
| Off | `level = "off"` | the `level` key removed |
| Inherit | the `level` key removed | (not a profile state) |

Inherit only appears in a project that names a profile: it takes the profile's setting, shown
alongside it in the `options` table. Turning a project's option off when its profile turns it on
is a loosening, so it needs a `reason`, as any loosening does. An option that is on but missing a
parameter it needs to do anything — `writing-rules` with no `files`, `hooks-wired` with no
`hooks` — is reported as inert, not as on.

The table shows each option's `since`, whether it is new to this project, and a suggestion
computed from what adopt already measures (a fork with no `checkout-hygiene` role, say). `--json`
gives the same data as the field set the skills parse. `--global` shows the profile's own layer
only, for editing `principles.toml` in its checkout.

`.context-gate/installed.toml` carries `options_answered`, the ids this project has answered (on,
off or inherit). An id is added only once it is answered, by `govern options --record CHECK ...`;
a person who quits the panel partway is offered the rest again. Adopt writes the same answers from
`--answers` keys `option:<id>` (on, off or inherit), `option:<id>:<setting>` (a parameter the
option needs), and `option:<id>:reason` (for a loosening).

```
govern options [--global] [--json] [--record CHECK ...]
```

See [how-it-works.md](how-it-works.md#options) for when options come up, and
[checks.md](checks.md) for each option's parameters and `Needs` line.

## Usage alerts (`usage-alerts.toml`)

With the `usage` option on, the orchestrating agent is told its context, 5-hour and weekly usage
as they rise, and can be handed the owner's own instructions at break points set in
`usage-alerts.toml`. The file sits beside `principles.toml` in the profile, and at
`.context-gate/usage-alerts.toml` in a project. The closest file wins outright: when the project
has one, the profile's is not read. There is no merging and no `reason`, because these are the
owner's preferences, not loosened checks. With neither file, the orchestrator gets data lines and
no alerts.

### Usage alerts: setup

Turning the option on tells the plugin what to inject; getting any data at all also needs the
statusline wrapped, once per home directory (per user account):

```
python3 .context-gate/bin/govern usage install
```

This copies the writer to `~/.local/share/context-gate/capture.py`, then rewrites
`~/.claude/settings.json`: only `statusLine.command` changes, to run the writer, chained to
whatever `statusLine` ran before (saved so it can be restored). Before that write, and before any
other write this feature makes to `settings.json`, the file's current bytes are copied to
`~/.local/share/context-gate/settings.json.bak`, so a bad write is recoverable by hand. `govern usage install` can be run again safely: it refreshes the writer and the interpreter path
in place rather than wrapping twice.

With `usage` on, each session start re-wraps the statusline in `~/.claude/settings.json` if
another tool replaced it. Turning the option off stops the data lines and alerts, but leaves the
statusline wrapped until you run `govern usage uninstall`. Problems, not usage values, are logged
to `~/.local/state/context-gate/usage/usage.log`: a failed option lookup, an alerts file that
is missing or has an error, and a stale snapshot.

On Windows the writer runs your own statusline through Git Bash when it is installed, else
through PowerShell, following the order Claude Code documents, so a command written for either
keeps working once it is wrapped.

```
python3 .context-gate/bin/govern usage uninstall
```

puts `statusLine` back exactly as it was (or removes the key if there was none), and removes the
chain file.

Headless sessions (`claude -p`) never see the statusline, so they get no usage data and no alerts,
with no error — there is nothing to read.

```toml
# fragment: .context-gate/usage-alerts.toml
[[alert]]
signal = "seven_day"          # context | five_hour | seven_day
at = 93                        # whole percent, 1-100
say = """
Weekly usage at {pct}% (resets {resets}). Start a powerdown. Don't kill open agents, but start no
new ones. Let everything end cleanly, then run closeouts and housekeeping.
"""

[[alert]]
signal = "seven_day"
at = 96
say = "Weekly usage at {pct}%, past the 95% ceiling. Stop now: commit what is safe, write HANDOFF.md, end the session."
```

| Key | Rule |
|---|---|
| `signal` | One of `context`, `five_hour`, `seven_day` |
| `at` | Integer, 1–100 |
| `say` | Non-empty string. Placeholders: `{pct}`, the signal's current whole percent; `{resets}`, the local reset time as `Sun 06:00`, empty for `context`. No others. |
| `context_step` | Optional, top level (above the first `[[alert]]`). Integer, 1–10; default 5: how many points context rises below 90% between data lines. |

The orchestrator also sees a data line:

```
usage: context 37% (+5% in 1h20m) · account 5h 12% (resets 07:00) · 7d 56% (resets Sun 04:00)
```

Context is the session's own, with its rise since the session's first reading (restarted by a
compaction). The 5-hour and weekly windows are account-wide, shared by every session on the
account, so they carry their reset times and never a rise. A value not seen yet reads `pending`;
a session resumed before its own reading arrives takes the 5-hour and weekly values from another
session's fresh snapshot.

A line comes when a value rises a step (every 5 points below 90%, or every `context_step` points
for context; every point from 90% up), when a 5-hour or weekly window resets, and on the session's
first call, whose line says so. If the statusline capture stops, a session that has had data is
told once, after 10 minutes of activity without fresh data (`usage: no fresh usage data for 10+
minutes of activity (last at 14:05) — ...`; idle waits don't count), and gets a data line again
when it resumes.

One alert fires once per window — the current 5-hour or weekly period, or until context drops and
climbs again; list more alerts at higher `at` values to repeat the reminder as usage keeps climbing.
An invalid file (a bad alert or `context_step`) gives data lines only, with the default step;
`python3 .context-gate/bin/govern usage resolve` shows why.

When alerts fire, each comes first, marked so it never reads like the routine line, and the data
line comes last:

```
⚠ usage alert (owner's prompt, .context-gate/usage-alerts.toml):
Weekly usage at 93% (resets Sun 06:00). Start a powerdown. ...

usage: context 37% (+5% in 1h20m) · account 5h 12% (resets 07:00) · 7d 93% (resets Sun 06:00)
```

Edits to `usage-alerts.toml` apply mid-session, at the next tool call: added alerts fire when
reached, removed ones stop, a changed `at` counts as a new alert, and a new `context_step` takes
effect. An edit that makes the file invalid keeps the alerts already loaded, and the agent is told
once (`usage: .context-gate/usage-alerts.toml has an error (...); keeping the previous alerts`);
deleting the file keeps them too. Which file wins (project or profile) is settled at session start,
and turning the `usage` option on or off needs a new session.

## Profiles

A profile directory's `principles.toml` uses the same `[checks.<id>]` and `[dialect]` tables as a
project's config, and sits between the engine standard and the project. Its check settings and
`[dialect]` values are validated the same way as a project's; an error names the profile and the
key. It may also carry a `[profile]` table, which the engine does not read. Any other table
(layout) is an error: where a project keeps its files is the project's business.

```toml
# fragment: a profile's principles.toml
[checks.agents]
require_max_turns = true
warn_keys = []

[checks.working-file-count]
level = "error"

[dialect]
agent_turns_prose = "forbid"

[dialect.reasons]
agent_turns_prose = "A stated turn count changes how the agent behaves."
```

```toml
# fragment: needs the profile to exist
[governance]
engine = "0.4.0"
profile = "git@github.com:your-org/principles.git#v1"
```

## Extensions

```toml
# fragment: needs the directory to exist
[governance]
engine = "0.4.0"
extensions = [".context-gate/checks"]

[checks.no-todo]
level = "warn"
```

Every `*.py` file in each listed directory is imported, in name order, when the config loads.
The checks they register are configured like built-in ones. See
[how-it-works.md](how-it-works.md#extensions) for how to write one.
