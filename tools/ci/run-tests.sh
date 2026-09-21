#!/bin/sh
# Local CI: the engine's tests on this machine's python3, then on Python 3.11 (the oldest
# Python the engine supports) in a Linux container, then the plugin's validation, then any extra
# local checks: the gate before merging an engine change.
#
#     tools/ci/run-tests.sh            # host + 3.11 in Docker
#     tools/ci/run-tests.sh --host     # host only
#
# Extra local checks: every executable tools/ci/extra.d/*.sh, if that directory exists, run last
# from the repo root with this script's arguments (so `--host` reaches them too).
set -eu
cd "$(dirname "$0")/../.."
mode=${1:-}

echo "== host: $(python3 --version)"
python3 -m unittest discover -s engine/tests

if [ "$mode" != "--host" ]; then
  if ! command -v docker >/dev/null 2>&1; then
    echo "docker not found: Python 3.11 not covered" >&2
    exit 1
  fi
  # In a linked worktree, `.git` is a file naming the main repo's git dir by absolute path: mount
  # that dir at the same path, or every git call in the container fails and git-backed tests break.
  set --
  if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    # --path-format needs git 2.31+; older git prints the dir relative to here, so resolve it.
    C=$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null) \
      || C=$(cd "$(git rev-parse --git-common-dir)" 2>/dev/null && pwd) || C=
    case $C in
      /*) set -- -v "$C":"$C":ro ;;
      *) echo "note: git common dir not found; a linked worktree's git tests may fail" >&2 ;;
    esac
  fi
  echo "== linux: python 3.11"
  docker run --rm -v "$PWD":/w "$@" -w /w python:3.11-slim sh -c '
    apt-get -qq update >/dev/null 2>&1 && apt-get -qq install -y git >/dev/null 2>&1
    git config --global user.email ci@example.invalid && git config --global user.name ci
    python -m unittest discover -s engine/tests'
fi

# The plugin as a release assembles it (plugin/ + the engine), validated by Claude Code itself
# when the CLI is here (the CI runners do not have it).
if command -v claude >/dev/null 2>&1; then
  echo "== plugin: claude plugin validate --strict"
  build=$(mktemp -d)
  cp -R plugin/. "$build/"
  cp -R engine/govern "$build/govern"
  claude plugin validate --strict "$build" >/dev/null || { echo "plugin validation failed" >&2; exit 1; }; echo "plugin OK"
  # A marketplace, checked when .claude-plugin/marketplace.json exists.
  if [ -f .claude-plugin/marketplace.json ]; then
    claude plugin validate --strict . >/dev/null || { echo "marketplace validation failed" >&2; exit 1; }; echo "marketplace OK"
  fi
  rm -rf "$build"
fi

if [ -d tools/ci/extra.d ]; then
  for extra in tools/ci/extra.d/*.sh; do
    [ -x "$extra" ] || continue
    echo "== extra: $extra"
    "$extra" ${mode:+"$mode"}
  done
fi
