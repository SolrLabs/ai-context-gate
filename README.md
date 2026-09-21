# context-gate

> **Alpha.** Early and changing. The config schema and check names may change between 0.x
> releases; each upgrade walks you through what changed.

A gate for the context your coding agents read. In a project built with Claude Code, agents
write most of the docs, decision logs and agent definitions, and without a check that material
drifts: decisions pile up with nothing to cite them by, superseded rules stay in files agents keep
reading, working notes outlive the work, handoffs grow until nobody reads them, and agents run
with settings nobody chose. context-gate puts one engine on those files, with each project's
policy in a single config file, and gives agents commands to query the records instead of
reading every file.

- **One engine, many projects.** Each project pins an engine version; a fix or a new check
  reaches every project through a guided upgrade.
- **Measured, not guessed.** Adoption reads your repo's current layout and proposes a config that
  fits it, asking only what measurement can't settle.
- **Existing debt doesn't block you.** Today's breaches go into a ratchet baseline at install. The
  gate fails only when something gets worse, and the baseline can only go down.
- **Overrides carry reasons.** Any default can be loosened, but the config says why, so the
  exception stays visible.
- **Stdlib Python, no dependencies.** Python 3.11+ and git, on Linux, Mac or Windows.

## What it checks

| Area | Checks |
|---|---|
| Decisions | A decision log with stable ids (`## D-12 — Title`), a generated index, replaced decisions reduced to a pointer, id ranges that don't collide across projects |
| Traps | Known pitfalls as `## T-N` entries, each with a **Bites when:** line |
| Docs | Frontmatter on governed docs, links that resolve, docs reachable from an index, word limits on handoffs, a cap on the governed doc count |
| Working files | How many are open, and finished ones left behind |
| Agents and skills | Required frontmatter on `.claude/agents/*.md` (model, effort, description …), well-formed skills, agent worktrees kept out of commits |
| Hygiene (opt-in) | Checkout state, hooks wired, licences declared per repo, writing rules, stale references |

`govern explain` in a governed project shows every effective setting and which layer it came from.

## Install

Needs Python 3.11+, git, and Claude Code. In Claude Code:

```
/plugin marketplace add SolrLabs/ai-context-gate
/plugin install context-gate@context-gate
```

Then, in the project you want to govern, start a session and run:

```
/context-gate:adopt
```

Adopt measures the repo, shows what it proposes, asks its questions, then installs into
`.context-gate/` and runs the gate. **It never commits.** Review the working tree and
`.context-gate/adopt-report.md`, then commit.

A fresh clone or a CI runner that lacks the pinned engine fetches it from this repository the
first time the gate runs. Teammates install the plugin once with the two commands above; the
project's `.claude/settings.json` already names the marketplace.

## Daily use

```sh
python3 .context-gate/bin/govern check              # the gate: 0 green, non-zero red
python3 .context-gate/bin/govern index              # rebuild generated indexes
python3 .context-gate/bin/govern next-id --project P
python3 .context-gate/bin/govern show --project P D-12
python3 .context-gate/bin/govern find --project P "retry policy"
python3 .context-gate/bin/govern explain
```

Run `govern check` from a pre-commit hook or CI, the way you run your tests.

**Upgrading:** when a newer engine is available, the plugin tells the session. `/context-gate:upgrade`
walks through each change and prices every policy choice in files and tokens.

**Removing it:** `python3 .context-gate/bin/uninstall` removes everything the tool added and restores
what it replaced.

## Profiles: your rules on top of the standard

The engine's defaults are a neutral standard. A team with house rules (tighter limits, required
agent fields, prose guidance for agents) keeps them in a **profile**: a git repo with a
`principles.toml`, pinned by tag, that every project it governs inherits:

```
/context-gate:adopt --profile https://github.com/your-org/principles.git#v1
```

Settings resolve in order: engine standard, profile, project config. A project that departs
from its profile records why.

## Layout

| Path | What |
|---|---|
| `engine/govern/` | The engine: checks, config, installer, adopt and upgrade |
| `plugin/` | The Claude Code plugin: adopt and upgrade skills, a session-start notice |
| `tools/release/` | Install a tagged engine or plugin from a clone |
| `CONTRIBUTING.md` | Running the tests, loading the plugin from a checkout, adding a check |
| `docs/how-it-works.md` | How the gate, the ratchet, profiles and upgrades fit together |
| `docs/configuration.md` | Every setting in `.context-gate/config.toml` and a profile |
| `docs/checks.md` | Every check: what it asks, why, and its parameters |

## No warranty

context-gate changes files in your repository when you adopt, upgrade or uninstall it. It never
commits or pushes, and adopt shows what it will change before `--apply`, but you are
responsible for reviewing every change before you commit it. The software is provided "as is",
without warranty of any kind, and without liability for any damage arising from its use, as set
out in sections 7 and 8 of the [licence](LICENSE).

## Feedback

This is an alpha. Open an issue for bugs, confusing output, or a check that fires on something
that's fine; include the `govern check` output and your `.context-gate/config.toml`.

## Licence

Apache License 2.0. Copyright 2026 SolrLabs LLC. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
