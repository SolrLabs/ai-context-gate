# Contributing to context-gate

Bug reports, confusing output and checks that fire on something that's fine are all welcome as
issues. This page is for changing the code.

## Layout

| Path | What |
|---|---|
| `engine/govern/` | The engine: checks, config, installer, adopt and upgrade. Module map in [engine/README.md](engine/README.md) |
| `engine/tests/` | The engine's tests |
| `plugin/` | The Claude Code plugin: the adopt and upgrade skills and the session-start hook |
| `tools/release/` | `install-engine.py` and `install-plugin.py`: install a tagged engine or plugin from a clone |
| `tools/render-checks.py` | Writes `docs/checks.md` from the engine's check manifest |
| `tools/ci/run-tests.sh` | The local test run |
| `docs/` | The user docs: how it works, configuration, checks |

The engine is Python 3.11+ and the standard library only: no dependencies.

## Running the tests

From the repo root:

```sh
tools/ci/run-tests.sh --host      # the engine's tests on this machine's python3, plus the plugin's validation
tools/ci/run-tests.sh             # and again on Python 3.11 in Docker
python3 -m unittest discover -s engine/tests -k <pattern>   # one area while iterating
```

The plugin is validated with `claude plugin validate --strict` when the Claude CLI is present.
Executable scripts in `tools/ci/extra.d/*.sh`, if that directory exists, run last as extra local
checks.

## Loading the plugin from a checkout

The plugin is `plugin/` plus the engine at the same tag. `install-plugin.py` assembles it from a
tag, never from the working tree:

```sh
python3 tools/release/install-plugin.py vX.Y.Z              # into ~/.claude/skills/context-gate/
claude plugin enable context-gate@skills-dir --scope project   # in each project that opts in
python3 tools/release/install-plugin.py vX.Y.Z --out DIR    # assemble only; then claude --plugin-dir DIR
```

A plugin under `~/.claude/skills/` loads as `context-gate@skills-dir`, with no marketplace. A
`.claude-plugin/marketplace.json` in a checkout must be named `context-gate-dev`, so installing from
a checkout never collides with the released `context-gate@context-gate`.
`python3 tools/release/install-engine.py vX.Y.Z` installs the engine at a tag where governed
projects look for it.

## Adding a check

Declare it with `@check(...)` in `engine/govern/checks/`, with a summary, the question adopt asks,
the rationale and typed parameters; [engine/README.md](engine/README.md) has the details. Add a
test that fails without it, then regenerate the reference page:

```sh
python3 tools/render-checks.py            # rewrite docs/checks.md
python3 tools/render-checks.py --check    # exit 1 if docs/checks.md is out of date
```

## Releases

Releases are tagged and published by the maintainers.
