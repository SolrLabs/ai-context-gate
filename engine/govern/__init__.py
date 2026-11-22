"""context-gate engine: one implementation of the governance gate for every opted-in project.

Policy lives in each project's `.context-gate/config.toml`; this package is the logic. See
docs/how-it-works.md in this repository.
"""
__version__ = "0.9.5-beta.8"

import os as _os


def _as_release(version: str, env) -> str:
    """The release part of `version` (before the first `-`) when `env` names exactly this version, else `version`."""
    if env.get("GOVERN_TEST_AS_RELEASE") == version:
        return version.split("-", 1)[0]
    return version


# Only the test suite sets GOVERN_TEST_AS_RELEASE, to the source version it runs as its release;
# any other engine (a fixture at another beta) keeps its own version.
__version__ = _as_release(__version__, _os.environ)
