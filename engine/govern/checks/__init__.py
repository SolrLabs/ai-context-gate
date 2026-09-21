"""Importing this package registers every built-in check, in the default order."""
from govern.checks import (  # noqa: F401
    registry, docs, decisions, agents, blocks, memory, working, ratchet,
    checkouts, hooks, writing, licences, references, links, overrides,
)
