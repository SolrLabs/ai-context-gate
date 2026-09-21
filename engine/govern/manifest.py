"""The check manifest: every check declares itself, and the adopt and upgrade
Q&A, opt-outs, `explain` and config validation all read this one registry.

A check without a complete declaration does not register. Extensions (Python files in the
directories `[governance] extensions` names, `.context-gate/checks` by convention) use the same
decorator, so one can later move into the engine unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

LEVELS = ("off", "warn", "error")
SCOPES = ("workspace", "project")
APPLIES = ("all", "doc-set", "governed")


@dataclass(frozen=True)
class Param:
    """A tunable value. `looser` says which direction relaxes the check, so a project that
    loosens it past the engine default has to say why. For a `list` param, `looser` is
    "more" (adding a value loosens, e.g. an allow-list of statuses) or "fewer" (removing a value
    loosens, e.g. a list of required fields) — widening a list this way is loosening too.
    `empty_means_any=True` marks a list where an empty value means "anything goes" (e.g. free
    text instead of a fixed set of statuses): emptying such a list is always loosening, even
    though it removes rather than adds. `unlimited`, on an int param, is the one value nothing
    is looser than (e.g. `max_turns`'s 0 meaning no ceiling) — declared per param, not guessed
    from a default of 0, so an unrelated param that happens to default to 0 is not silently
    exempted from needing a reason when raised."""
    type: str                      # int | str | bool | list | enum | table | tables
    default: Any
    help: str
    looser: str | None = None      # "higher" | "lower" | "more" | "fewer" | None (no direction)
    choices: tuple = ()
    # For `tables` (a list of tables) and `table`: the keys an item may carry, each with its
    # type, and which of them are required.
    fields: dict[str, str] = field(default_factory=dict)
    required: tuple = ()
    empty_means_any: bool = False
    unlimited: Any = None


@dataclass(frozen=True)
class Check:
    id: str
    scope: str
    since: str
    default: str
    summary: str
    question: str
    rationale: str
    applies: str
    params: dict[str, Param]
    fn: Callable
    origin: str = "engine"
    core: bool = False          # the fixed core: cannot be turned off
    also_workspace: bool = False  # a project check that also runs against the workspace scope
    also_project: bool = False  # a workspace check that can also run restricted to one project
                                 # scope, under `check --project X` — `fn(ctx, params, scope)`
                                 # in that case, `fn(ctx, params)` otherwise
    ratchets: bool = False      # its size breaches feed the ratchet (a project may opt out)


CHECKS: dict[str, Check] = {}
_ORDER: list[str] = []


def check(id: str, *, scope: str, since: str, summary: str, question: str, rationale: str,
          default: str = "error", applies: str = "all",
          params: dict[str, Param] | None = None, origin: str = "engine",
          core: bool = False, also_workspace: bool = False, also_project: bool = False,
          ratchets: bool = False):
    """Register a check. Workspace checks are called `fn(ctx, params)`; project checks
    `fn(ctx, params, scope)` once per registry entry the check applies to. `also_project` marks
    a workspace check that also knows how to restrict itself to one project (its `fn` then takes
    an optional trailing `scope`, `None` on an ordinary run): under `check --project X` it runs
    once, for `X` only, instead of sitting out the way an ordinary workspace check does."""
    if scope not in SCOPES:
        raise ValueError(f"check {id}: scope must be one of {SCOPES}")
    if also_project and scope != "workspace":
        raise ValueError(f"check {id}: also_project only makes sense on a workspace check")
    if also_workspace and scope != "project":
        raise ValueError(f"check {id}: also_workspace only makes sense on a project check")
    if default not in LEVELS:
        raise ValueError(f"check {id}: default must be one of {LEVELS}")
    if applies not in APPLIES:
        raise ValueError(f"check {id}: applies must be one of {APPLIES}")
    for name, text in (("summary", summary), ("question", question), ("rationale", rationale)):
        if not text.strip():
            raise ValueError(f"check {id}: a manifest entry needs a {name}")

    def deco(fn: Callable) -> Callable:
        if id in CHECKS:
            raise ValueError(f"check {id} is registered twice")
        CHECKS[id] = Check(id, scope, since, default, summary, question, rationale, applies,
                           dict(params or {}), fn, origin, core, also_workspace, also_project,
                           ratchets)
        _ORDER.append(id)
        return fn
    return deco


def registration_order(scope: str) -> list[str]:
    return [cid for cid in _ORDER if CHECKS[cid].scope == scope]


def forget(origin: str) -> None:
    """Unregister every check from one origin. Only tests need this: a real run is one process
    per governance root."""
    for cid in [c for c, chk in CHECKS.items() if chk.origin == origin]:
        del CHECKS[cid]
        _ORDER.remove(cid)
