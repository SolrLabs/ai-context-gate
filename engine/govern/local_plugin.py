"""Assemble the Claude Code plugin from one release's tree, and install it as the local plugin.

    python3 -m govern.local_plugin --tree DIR [--home HOME] [--name NAME]

DIR holds `plugin/` and `engine/govern/` as one release tag carries them (a clone of the tag,
say). What is installed is what `tools/release/install-plugin.py` installs for that tag:
`plugin/` plus the engine as `govern/`, so the skill, its hooks and the engine it bundles are
one version; the release stamp `govern/RELEASE` (`vX.Y.Z`), in the installed copy only; and
`defaultEnabled: false`, since a plugin under ~/.claude/skills/ is otherwise on in every
project. It lands in HOME/.claude/skills/<the plugin's own name>/ and loads in later sessions
as `<name>@skills-dir`.

`govern beta on` runs this module from the beta it fetched, so each beta lays out its own
plugin. Standard library, `layout` and `profile.rmtree` only: it runs from a bare clone of the
tag, whether or not that engine is installed anywhere.

The new copy is assembled beside the destination and moved into place, so a run that fails
leaves the earlier install whole. Refuses a tree whose plugin.json version and engine version
disagree, and with `--name` one whose plugin.json gives another name: the caller says which
plugin it came for, so a tree cannot choose what it replaces. Replaces an existing local install
of the plugin, which is a directory carrying the release stamp; one without it was not put
there by this tool and is refused. Never touches anything else under ~/.claude/.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path

from govern import layout
from govern.profile import rmtree

VERSION_RE = re.compile(r"\d+\.\d+\.\d+(-beta\.[1-9]\d*)?", re.ASCII)
NAME_RE = re.compile(r"\w[\w.-]*")


class PluginError(Exception):
    pass


def read(tree: Path) -> tuple[dict, str, str]:
    """The tree's plugin manifest, the plugin's name and the one version plugin and engine
    both carry. Raises PluginError for a tree that is not one release."""
    try:
        manifest = json.loads((tree / "plugin" / ".claude-plugin" / "plugin.json")
                              .read_text(encoding="utf-8"))
        engine_init = (tree / "engine" / "govern" / "__init__.py").read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        raise PluginError(f"{tree} does not hold the plugin and engine of a release ({exc})")
    if not isinstance(manifest, dict):
        raise PluginError(f"{tree}: plugin.json is not a JSON object")
    version, name = manifest.get("version"), manifest.get("name")
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise PluginError(f"{tree}: plugin.json names no release ({version!r})")
    if f'__version__ = "{version}"' not in engine_init:
        raise PluginError(f"v{version}: plugin.json says {version}, the engine says otherwise; "
                          "plugin and engine release together")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise PluginError(f"v{version}: plugin.json has no usable name ({name!r})")
    return manifest, name, version


def assemble(tree: Path, stage: Path) -> tuple[str, str]:
    """Build the tree's plugin in `stage`, which must not exist yet; the tree is left as it
    is. Returns the plugin's name and version."""
    manifest, name, version = read(tree)
    ignore = shutil.ignore_patterns("__pycache__")
    shutil.copytree(tree / "plugin", stage, ignore=ignore)
    # Off by default, in the installed copy only: on where a project's settings turn it on.
    # Written as the release tool writes it, so the two leave the same bytes.
    (stage / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({**manifest, "defaultEnabled": False}, indent=2) + "\n", encoding="utf-8")
    shutil.copytree(tree / "engine" / "govern", stage / "govern", ignore=ignore)
    # The release stamp, in the assembled copy only: adopt installs the engine it runs only
    # when it carries this.
    (stage / "govern" / layout.RELEASE_MARKER).write_text(f"v{version}\n", encoding="utf-8")
    return name, version


def install(tree: Path, home: Path, expected: str | None = None) -> tuple[str, str, Path]:
    """Install the tree's plugin as the local plugin under `home`, replacing an earlier local
    install of it. Returns the plugin's name, its version and where it is. Raises PluginError,
    with the earlier install as it was, when the tree is not one release, when it is not the
    plugin `expected` names, when what is in the plugin's place is not a local install (it
    has no release stamp), or when a step fails."""
    _, name, version = read(tree)
    if expected is not None and name != expected:
        raise PluginError(f"v{version} is the plugin {name!r}, not {expected!r}; nothing was "
                          "installed")
    out = home / ".claude" / "skills" / name
    # Beside the destination, so the move into place never crosses a filesystem; dot names, so
    # nothing takes either for a plugin while it exists.
    stage, aside = out.with_name(f".{name}.new"), out.with_name(f".{name}.old")
    if out.is_symlink():
        raise PluginError(f"{out} is a symbolic link, not an install of the plugin; remove it, "
                          "then run this again")
    if out.exists() and not out.is_dir():
        raise PluginError(f"{out} is not a directory; remove it, then run this again")
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        if aside.is_dir() and not out.exists():
            os.rename(aside, out)       # a run killed between the two moves: put it back first
        if out.exists() and not (out / "govern" / layout.RELEASE_MARKER).is_file():
            raise PluginError(f"{out} is not a local install of the plugin (it has no "
                              f"govern/{layout.RELEASE_MARKER}), so it is not replaced; move it "
                              "away, then run this again")
        rmtree(stage)
        rmtree(aside)
        assemble(tree, stage)
        if out.exists():
            os.rename(out, aside)
        try:
            os.rename(stage, out)
        except OSError as exc:
            if aside.exists():
                try:
                    os.rename(aside, out)
                except OSError:
                    try:
                        rmtree(stage)
                    except OSError:
                        pass
                    raise PluginError(
                        f"could not install plugin {name} {version} in {out.parent} ({exc}); "
                        f"the earlier install is in {aside}, and the next run puts it back")
            raise
    except OSError as exc:
        try:
            rmtree(stage)
        except OSError:
            pass
        raise PluginError(f"could not install plugin {name} {version} in {out.parent} ({exc})")
    try:
        rmtree(aside)
    except OSError:
        pass                            # the new plugin is in place; the next run clears this
    return name, version, out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m govern.local_plugin",
                                     description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--tree", required=True, type=Path,
                        help="a directory holding plugin/ and engine/govern/ of one release")
    parser.add_argument("--home", type=Path, default=None,
                        help="the home directory to install under (default: the user's)")
    parser.add_argument("--name", default=None,
                        help="the plugin expected: a tree whose plugin.json gives another name "
                             "is refused")
    args = parser.parse_args(argv)
    try:
        name, version, out = install(args.tree, args.home or layout.home(), args.name)
    except PluginError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"assembled plugin {name} {version} -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
