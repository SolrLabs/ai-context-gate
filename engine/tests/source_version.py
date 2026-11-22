"""The version the engine's source declares, as the text of `engine/govern/__init__.py` says it.

Under test `govern.__version__` is the release part of it (`govern._as_release`), and
`GOVERN_TEST_AS_RELEASE` is set to this value. A fixture that rewrites or looks for the literal
`__version__ = "..."` line uses this, not `__version__`.
"""
import re
from pathlib import Path

SOURCE_VERSION = re.search(
    r'^__version__ = "([^"]+)"$',
    (Path(__file__).resolve().parents[1] / "govern" / "__init__.py").read_text(encoding="utf-8"),
    re.MULTILINE).group(1)
