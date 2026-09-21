"""Where context-gate keeps things, and the names it goes by.

Everything the tool installs into a project lives in one directory at the governance root, so
uninstalling is removing that directory (plus the few outside touches `installed.toml` lists).
The project's own records (decision logs, traps, handoffs) are never in it: they
belong to the project and outlive the tool.
"""
from __future__ import annotations

import os
from pathlib import Path

TOOL = "context-gate"
DISPLAY_NAME = "context-gate"
PLUGIN = "context-gate"
MARKETPLACE = "context-gate"
PLUGIN_ID = f"{PLUGIN}@{MARKETPLACE}"
# How a plugin under ~/.claude/skills/ is enabled: how development tests the plugin.
SKILLS_DIR_PLUGIN_ID = f"{PLUGIN}@skills-dir"
PUBLIC_REPO = "SolrLabs/ai-context-gate"
PUBLIC_SOURCE = f"https://github.com/{PUBLIC_REPO}.git"
# In a released, assembled engine only (never in the repository): `govern/RELEASE` holding
# "v<version>\n". Adopt installs the engine it runs only when it carries this.
RELEASE_MARKER = "RELEASE"
GOV_DIR = f".{TOOL}"
CONFIG = f"{GOV_DIR}/config.toml"
BASELINE = f"{GOV_DIR}/baseline.json"
ENTRYPOINT = f"{GOV_DIR}/bin/govern"
MANIFEST = f"{GOV_DIR}/installed.toml"
BACKUP = f"{GOV_DIR}/backup"


def home() -> Path:
    """$HOME when set, else the OS's notion of home (on Windows, USERPROFILE). Every part of
    the tool resolves home this way, so they agree."""
    return Path(os.environ.get("HOME") or Path.home())


def engines_dir(home_dir: Path | None = None) -> Path:
    return (home_dir or home()) / ".local" / "share" / TOOL / "engines"
