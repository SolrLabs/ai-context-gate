"""A test must never see the running user's Claude profile.

`layout.claude_dir` follows `CLAUDE_CONFIG_DIR` whenever the home is the machine's own, and the
fixtures point `HOME` at a temporary folder, so a test run in a shell that sets the variable
would read and rewrite the real profile, and every subprocess it starts would too. Every test
module imports this before anything from `govern`; the runner removes the variable as well, but
`python3 -m unittest discover -s engine/tests` does not go through the runner.
"""
from __future__ import annotations

import os

os.environ.pop("CLAUDE_CONFIG_DIR", None)
