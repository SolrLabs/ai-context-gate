#!/usr/bin/env python3
"""Assemble the Claude Code plugin at a release tag, and install it as a local plugin.

    python3 tools/release/install-plugin.py vX.Y.Z              # into ~/.claude/skills/context-gate/
    python3 tools/release/install-plugin.py vX.Y.Z --out DIR    # assemble only (for --plugin-dir)

A tag may be a beta, vX.Y.Z-beta.N. A beta's plugin is named `context-gate-beta`: it is installed
in ~/.claude/skills/context-gate-beta/ and loads as `context-gate-beta@skills-dir`, because
Claude Code never loads a plugin under ~/.claude/skills/ whose name an installed marketplace
plugin has. ~/.claude/skills/context-gate/ is then left holding only `govern/RELEASE` and a
README, the marker launchers older than that look for (an existing local install there is
replaced by it). This is what `govern.local_plugin` installs for the same tag.

The plugin is `plugin/` plus the engine (`engine/govern`) at the same tag, so the skill, its
hooks and the engine it bundles are always one version. A plugin under
~/.claude/skills/ loads in later sessions as `<name>@skills-dir`, with no marketplace.

The installed copy sets `defaultEnabled: false`: a plugin under ~/.claude/skills/ is otherwise
on in every project. A project turns it on in `.claude/settings.local.json` (`govern beta on`)
or with `claude plugin enable context-gate-beta@skills-dir --scope local` (a release:
`context-gate@skills-dir`).

The bundled engine is stamped `govern/RELEASE` (`vX.Y.Z`), in the assembled copy only: adopt
installs the engine it runs into the user's engines directory only when it carries that stamp.

The plugin's name, and so its directory under ~/.claude/skills/, is the one the tag's own
plugin.json gives (a beta's with `-beta` added). Refuses a tag whose plugin.json version and engine version disagree.
Replaces an existing local install of the plugin, which is a directory carrying that stamp: one
without it was not assembled by this tool, and is refused rather than deleted. That rule is for
the default place under ~/.claude/skills/; `--out DIR` replaces DIR whatever it holds, as it
always did. Never touches anything else under ~/.claude/.
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def export(tag: str, paths: list[str], dest: Path) -> None:
    archive = subprocess.run(["git", "-C", str(REPO), "archive", "--format=tar", tag, *paths],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(dest, filter="data")


def main() -> int:
    args = sys.argv[1:]
    if not args or not re.fullmatch(r"v\d+\.\d+\.\d+(-beta\.[1-9]\d*)?", args[0]):
        print(__doc__, file=sys.stderr)
        return 2
    tag, version = args[0], args[0][1:]
    with tempfile.TemporaryDirectory() as tmp:
        try:
            export(tag, ["plugin", "engine/govern"], Path(tmp))
        except subprocess.CalledProcessError as exc:
            print(f"error: git archive {tag} failed: {exc.stderr.decode().strip()}", file=sys.stderr)
            return 1
        stage = Path(tmp) / "plugin"
        manifest = json.loads((stage / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        engine_init = (Path(tmp) / "engine" / "govern" / "__init__.py").read_text(encoding="utf-8")
        if manifest.get("version") != version or f'__version__ = "{version}"' not in engine_init:
            print(f"error: {tag}: plugin.json says {manifest.get('version')}, the engine says "
                  f"otherwise; plugin and engine release together", file=sys.stderr)
            return 1
        name = manifest.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"\w[\w.-]*", name):
            print(f"error: {tag}: plugin.json has no usable name ({name!r})", file=sys.stderr)
            return 1
        beta = "-beta." in version
        # Off by default, in the installed copy only: on where a project's settings turn it on.
        manifest["defaultEnabled"] = False
        if beta:
            manifest["name"] = f"{name}-beta"
        (stage / ".claude-plugin" / "plugin.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        home = Path(os.environ.get("HOME") or Path.home())
        skills = home / ".claude" / "skills"
        out = Path(args[args.index("--out") + 1]) if "--out" in args \
            else skills / manifest["name"]
        # What an older launcher reads, for a beta installed in its place only.
        marker = skills / name if beta and "--out" not in args else None
        for place in (out, marker):
            if place and "--out" not in args and (place.exists() or place.is_symlink()) \
                    and not (place / "govern" / "RELEASE").is_file():
                print(f"error: {place} is not a local install of the plugin (it has no "
                      "govern/RELEASE), so it is not replaced; move it away, then run this again",
                      file=sys.stderr)
                return 1
        shutil.copytree(Path(tmp) / "engine" / "govern", stage / "govern")
        # The release stamp (the engine's layout.RELEASE_MARKER), in the assembled copy only:
        # adopt installs the engine it runs only when it carries this.
        (stage / "govern" / "RELEASE").write_text(f"{tag}\n", encoding="utf-8")
        if out.exists():
            for path in out.rglob("*"):
                path.chmod(path.stat().st_mode | 0o200)
            shutil.rmtree(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(stage, out)
        if marker:
            if marker.exists():
                for path in marker.rglob("*"):
                    path.chmod(path.stat().st_mode | 0o200)
                shutil.rmtree(marker)
            (marker / "govern").mkdir(parents=True)
            (marker / "govern" / "RELEASE").write_text(f"{tag}\n", encoding="utf-8")
            (marker / "README.md").write_text(
                "This folder is a marker, not a plugin: older launchers read govern/RELEASE here.\n"
                f"The beta plugin is installed beside it, in {out.name}/.\n", encoding="utf-8")
    print(f"assembled plugin {name} {version} -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
