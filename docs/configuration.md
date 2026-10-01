---
doc_type: reference
purpose: Every table and key .context-gate/config.toml accepts, with types, defaults and examples.
audience: human
load_when: writing or changing a project's config.toml or a profile's principles.toml
last_reviewed: 2026-09-23
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
| `decision_heading` | `"any-dash"` | `"em-dash"` | Which dash separates an entry's id from its title. `any-dash` accepts an em dash, an en dash or a hyphen, with spacing optional; `em-dash` requires ` — `. A heading-shaped line that fails the grammar is an error either way. |
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
`checkout-hygiene` and `licenses`. Run `python3 .context-gate/bin/govern options` to see every
option's state, or `/context-gate:options` in Claude Code to change them.

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
