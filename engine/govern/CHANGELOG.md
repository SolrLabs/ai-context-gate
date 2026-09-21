# context-gate — release notes

Newest first. Each release says what changed and, under **Upgrading**, anything a project has to
do or decide. The upgrade report quotes every entry between the engine a project ran and the one
it upgraded to.

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
