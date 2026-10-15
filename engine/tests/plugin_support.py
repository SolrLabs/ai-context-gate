"""The Claude Code plugin: its session-start hook, the notice that points at its skill, and the
rule that plugin and engine release as one version."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parent.parent

REPO = ENGINE.parent

PLUGIN = REPO / "plugin"

sys.path.insert(0, str(ENGINE))

from govern import __version__, layout, notice  # noqa: E402

BETA = "9.9.0-beta.1"

BOTH = ("context-gate: both the beta and the stable plugin are enabled here, so every hook "
        f"runs twice; govern beta on {BETA} re-applies the switch")

__all__ = [
    "json",
    "os",
    "re",
    "shutil",
    "subprocess",
    "sys",
    "tempfile",
    "unittest",
    "Path",
    "mock",
    "ENGINE",
    "REPO",
    "PLUGIN",
    "__version__",
    "layout",
    "notice",
    "BETA",
    "BOTH",
]
