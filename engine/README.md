---
doc_type: reference
purpose: Layout of the governance engine, how a project runs it, and how it is tested.
audience: both
load_when: changing the engine, adding a check, or wiring the engine into a project
related: [../docs/how-it-works.md, ../docs/configuration.md, ../docs/checks.md]
last_reviewed: 2026-09-21
---

# The engine (`govern`)

Stdlib-only Python 3.11+. Policy lives in each project's `.context-gate/config.toml`; this
package is the logic. How the pieces fit is in [how-it-works](../docs/how-it-works.md), every
setting in [configuration](../docs/configuration.md), and every check in
[checks](../docs/checks.md).

| Module | Owns |
|---|---|
| `layout.py` | Where the tool keeps things: `.context-gate/` in a project, the user-level dirs |
| `installer.py` | Install, upgrade and uninstall, recorded in `installed.toml` |
| `report.py` | The install and upgrade reports: findings before against after |
| `templates/entrypoint.py` | Installed as `bin/govern` and `bin/uninstall` |
| `cli.py` | Subcommands (`check`, `index`, `baseline`, `next-id`, `show`, `find`, `trap-add`), the check runner, exit codes. `check --project X --path DIR [--history-from DIR]` checks `X` from `DIR` instead of the checkout the registry names — a git pre-commit hook's staged-tree snapshot, say — dating any git question a project-scope check asks about those files from `--history-from`'s checkout instead, since the snapshot itself carries no history |
| `manifest.py` | The `@check` decorator: every check declares its scope, level, parameters, question and rationale |
| `config.py` | Loading and strictly validating `.context-gate/config.toml`; resolving each check's settings across layers; loading extensions |
| `registry.py` | Scopes from the project's registry file, with each fact's location configured and checked — or, with no `[registry]` table, the one scope a single repo is |
| `context.py` | What a check sees: the root, config, registry, layout paths, git, which scope owns a file (`owner`/`owned_by_project`), and (under `check --path`) a snapshot path's place in the real tree (`real_path`) |
| `text.py` | Reading files, frontmatter, word counts, generated-block markers |
| `decisions.py` | Decision-log and trap parsing under a named heading grammar |
| `migrate.py` | Moving a project's decision logs and traps from other common shapes onto the standard mechanically |
| `measure.py` | Reading a project's layout before it adopts: registry, scopes, decision logs, traps, working dir, docs, markers, nested checkouts (read-only, no config) |
| `adopt.py` | `measure`, then `propose`, then with `--apply` install, create logs, migrate, index, baseline and check in one step, reported in `adopt-report.md` |
| `propose.py` | A project's `config.toml` proposed from `measure`'s findings: layout only, with a question wherever measurement leaves two readings; `validate` loads a proposal the way the engine will |
| `tomlw.py` | A small TOML writer for proposed configs (the stdlib only reads TOML) |
| `blocks.py` | Generated-block targets and renderers |
| `ratchet.py` | Breach computation and the baseline file |
| `checks/` | The built-in checks, one module per concern |

## Registry, or a single repo

A `config.toml` with a `[registry]` table governs a workspace of several projects: `[registry]
file` names a `registry.toml` (or whatever `entries`/`workspace` are configured to), and each
`[[project]]` entry becomes a project scope. Naming a file that does not exist is an error
(`RegistryMissing`) — a `[registry]` table is a promise the file backs.

A `config.toml` with **no `[registry]` table at all** governs one repo: itself. There is exactly
one project scope, this governance root, described by an optional `[repo]` table carrying any
fact a registry entry could (`name`, `dir`, `tier`, `role`, `governance`, `id_prefix`,
`id_range`, `purpose`, `handoff`, `licence`, `upstream`, `runtime_gate` — see `REGISTRY_KEYS` in
`config.py`), all of them optional. Three locational facts default rather than being left unset,
since there is nowhere else they could sensibly mean or nothing else to compare them against:
`name` to the root directory's own name, `dir` and `governance` to `.`, and `tier` to the first of
`[projects] governed_tiers`. An id range and a licence are still the project's to set, exactly as
an entry that leaves them unset would be. Project-scope checks (decision log, doc frontmatter,
and the rest) run against this one scope unconditionally; workspace-scope checks still run too,
except that in single-repo mode the workspace scope has no decision log of its own unless
`[workspace] decision_log` is set — with nothing else the workspace could mean here, there is
nothing to be missing. Setting both `[registry]` and `[repo]` is refused as ambiguous — pick one.

When the workspace's and a project's settings resolve to the same file (a single repo's one
decision log, doing double duty as both, is the common case — but nothing stops a multi-project
workspace's own file from landing inside one project's directory too), a check that runs at both
scopes checks that file once: `Context.owner(path)` decides, from the registry's own scopes, which
one owns it — the project scope whose governance directory contains it, or the workspace when none
does — and a workspace-scope check (`decision-log`, `decision-history`, `workspace-docs`,
`doc-links`, `generated-blocks`, the ratchet) skips a file a project scope owns, leaving it to that
scope's own pass. Ownership is decided from the registry alone, never from which scope's check
happens to run first, so running every check twice against one `Context` gives identical findings
both times.

## Running it in a project

Everything the tool installs in a project lives in `.context-gate/`:
`config.toml`, `baseline.json`, `bin/govern` (the gate), `bin/upgrade`, `bin/uninstall`,
`installed.toml`.

```sh
PYTHONPATH=<engine dir> python3 -m govern.installer install --root <project> --config <config.toml> \
    [--entrypoint <existing gate path>]... [--migrate-baseline <existing baseline> | --baseline <file>] \
    [--retire <file>]...
PYTHONPATH=<new engine dir> python3 -m govern.installer upgrade --root <project>
PYTHONPATH=<engine dir> python3 -m govern.installer migrate --root <project> [--apply]
PYTHONPATH=<engine dir> python3 -P -m govern.installer measure --root <project> [--json]
PYTHONPATH=<engine dir> python3 -P -m govern.installer adopt --root <project> [--source S|none] \
    [--marketplace <owner/repo>] [--profile P] [--answers <answers.toml>] [--apply] [--json]
PYTHONPATH=<engine dir> python3 -m govern.installer enable-plugin --root <project> --id <plugin@marketplace> \
    [--marketplace <owner/repo>]
python3 <project>/.context-gate/bin/govern explain [check]
python3 <project>/.context-gate/bin/uninstall
```

Install writes `install-report.md`: the findings of a previous gate (run from `--entrypoint`
before it is replaced, if one is named) against the new gate's, each new finding under the check that raised it with that check's rationale,
plus every setting that differs from the engine's defaults. Upgrade pins the project to the
engine it is run with, refreshes `bin/`, and writes `upgrade-report.md` the same way. `explain`
prints every effective setting and where it came from.

After any command, the gate prints one line when a newer engine exists (installed here, or
released at `[governance] source`, checked at most daily and cached): orange in a terminal,
plain through a pipe so agents read the same words. `python3 .context-gate/bin/upgrade`
acts on it: it finds the newest release (or `--to X.Y.Z`), installs it from the source if
needed, and runs that engine's `upgrade`. When the Claude Code plugin is present (installed under `~/.claude/skills/` or enabled in
settings), the notice points at `/context-gate:upgrade` instead; the plugin's
session-start hook shows the same notice when a session opens (see `plugin/README.md`).
`CONTEXT_GATE_NO_UPDATE_CHECK=1` keeps the gate
off the network (the tests set it).

`migrate` moves a project's logs from other common shapes onto the standard mechanically —
sections grouping entries onto a `**Topic:**` line, bullet traps onto `## T-N — Title` headings, a superseded
decision with a named successor onto a one-line pointer. Upgrade first: `migrate` runs
under the engine `config.toml` pins, so a pin mismatch refuses (exit 2) the same way `check`
does — pin the project to this engine (`bin/upgrade`, or edit the pin by hand) before migrating.
It is a dry run by default,
writing `migration-report.md`; `--apply` makes the edits (refusing if a touched file has
uncommitted changes) and the report ends with the `git add`/`git commit` commands to run.

`adopt` is how a project that has never had the engine takes it on. It measures the tree,
proposes a config (layout only; check levels and limits come from the standard and the profile),
and prints it with the questions measurement cannot settle, exiting 3 while any are open.
`shape` ("single" or "workspace") is always asked, and for a workspace `repos`, a comma list of
the repos it governs: an existing registry is never edited, its unselected entries go in
`[registry] skip`; with none, adopt writes `projects.toml`. `--answers` is a TOML file of `key =
"option"`. `--apply` refuses while a question is open or a file it would touch has uncommitted
changes, then installs (with the plugin opt-in), creates missing decision logs, runs `migrate --apply` when needed, regenerates every
block, baselines what is newly visible (adding keys only), runs the gate and writes
`adopt-report.md`. It never commits. `/context-gate:adopt` holds the conversation.

The plugin opt-in is `enabledPlugins["context-gate@context-gate"]` with the marketplace beside it
in `extraKnownMarketplaces` (the public repository, or `--marketplace <owner/repo>` for a fork),
each recorded in `installed.toml` and restored by uninstall. `[governance] source` defaults to
the public repository, or the fork's GitHub URL with `--marketplace`; `--source none` leaves it
unset. `enable-plugin` writes the same two settings on an existing install;
`--id context-gate@skills-dir`, how development loads the plugin, writes no marketplace.

`--entrypoint` keeps a gate path the project's skills already call working: the original is
backed up and replaced by a stub that runs `bin/govern`, passing its own name so usage and
messages name the command the caller ran. Uninstall restores it.

**Projects run releases, never this working tree.** A release is installed with
`python3 tools/release/install-engine.py vX.Y.Z`, which exports the engine at that tag into
`~/.local/share/context-gate/engines/X.Y.Z/` and makes it read-only. `adopt --apply` does the
same for the engine it is running (the plugin's bundled copy, say) when that version is not
installed yet, and never overwrites one that is. `bin/govern` runs
exactly the version `config.toml` pins, installing it from `[governance] source` first if it is
missing; the engine refuses any other pin. (A series pin such as `"0.4"` runs the newest
installed release in that series, with a note to upgrade.) `$GOVERN_ENGINE` points the entry
point at an explicit engine directory, for engine development only.

Releases are tagged `vX.Y.Z`, with `__version__` and the plugin's `plugin.json` version
matching the tag; `tools/release/install-engine.py` and `tools/release/install-plugin.py`
install a tag, never the working tree.

## For contributors

The rest of this page is for changing the engine; [CONTRIBUTING](../CONTRIBUTING.md) covers the
repository as a whole.

### Adding a check

Declare it with `@check(...)` in `checks/`, with a summary, the question the adopt Q&A asks,
the rationale, and typed `Param`s. A `Param` that relaxes the rule in one direction sets
`looser=`, so a project loosening it past the engine default must give a `reason`. Import the
module from `checks/__init__.py`. Then add a test that fails without it, and run
`python3 tools/render-checks.py` to regenerate [checks](../docs/checks.md).

### Testing

```sh
python3 -m unittest discover -s engine/tests          # the engine's tests
tools/ci/run-tests.sh --host                          # the same, plus the plugin's validation
tools/ci/run-tests.sh                                 # and again on Python 3.11 in Docker
```

Run from the repo root. `engine/tests/test_regressions.py` holds the engine's behavioural tests,
each named after the behaviour it pins.
