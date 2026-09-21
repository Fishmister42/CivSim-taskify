"""Goal runs: directed testing on top of the production harness.

Stochastic play samples uniformly from whatever the current state offers, so the rare,
multi-turn items -- founding a second city, spending a builder charge -- are effectively never
reached. A **goal run** hands the agent one narrow objective, lets it puzzle that single game
item through with the ordinary decision loop, and asks the harness itself whether the item
happened. The agent is never told the strategy and never directs one; it is told what to
accomplish and reads the same board a human would.

    uv run python -m tests.live.goal_run --goal found_second_city --provider openrouter \
        --turns 15 OUT_DIR
    uv run python -m tests.live.goal_run --goal move_unit_to_plot --provider stochastic \
        --provider-policy coverage OUT_DIR
    uv run python -m tests.live.goal_run --feasibility --store civsim-match-store.db

**Principle I (NON-NEGOTIABLE), stated plainly.** The agent receives *only* :attr:`Goal.objective`
-- prose describing the item to accomplish, carrying no state a human player could not see on
their own screen. The ``success`` predicate and the ``prerequisites`` are evaluated **by the
harness**, after the fact, against the observations the store already recorded for the run. They
are never rendered into ``DecisionRequest.system``, never into ``DecisionRequest.observation``,
and never handed to the model in any other form -- so a goal run tells the agent no more about
the game than an ordinary run does. The objective reaches the agent through the composition
root's existing guidance seam (see :func:`write_goal_guidance`), which
``agent/context.py``'s :func:`assemble_system_prompt` appends below the fixed role text; no new
path into the model's context is opened by this module.

**What is reused, not re-implemented.** ``tests/live/demo_landed_run.py`` already composes the
production path (``build_runner_dependencies`` -> ``Runner.start``) and records the run from the
production ``capture_window()`` path. This module imports its ``Recorder``, ``read_setup``,
``write_config``, ``store_counts`` and ``resolve_provider`` rather than copying them; the only
thing it builds for itself is the wiring the demo has no reason to have -- the per-goal guidance
file, the per-turn success check, and the stop-at-success.

**The predicate grammar is the catalog's own.** ``success`` and each ``prerequisites`` entry are
evaluated by ``civsim_harness.act.predicates.evaluate_predicate`` -- the same restricted,
AST-allow-listed evaluator that decides whether a catalogued action is available and whether it
verified. Two dynamic ``observed_*`` symbols (``catalogs/README.md`` SS4's own flat
harness-bound-snapshot namespace) carry what a single observation cannot:

- ``observed_start_<fact>`` -- the same fact read from this goal's **first** observation, which
  is what lets a goal say "one more city than there were" rather than a fixed count.
- ``observed_applied_<action id, dots as underscores>`` -- how many times the agent's decision to
  take that catalogued action was recorded ``applied`` during this goal run. An ``applied``
  outcome is already the harness's own re-read through the action's declared
  ``verification_predicate`` (FR-011), so this is a store record, not a second opinion.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import re
import sqlite3
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "src") not in sys.path:  # pragma: no cover - import-path setup
    sys.path.insert(0, str(REPO / "src"))

import yaml  # noqa: E402

from civsim_harness.act.predicates import (  # noqa: E402
    PredicateEvaluationError,
    evaluate_predicate,
)
from civsim_harness.capability.loader import load_catalog  # noqa: E402
from civsim_harness.models.catalog import DeclarationKind  # noqa: E402
from civsim_harness.run.composition import PROVIDER_POLICY_NAMES  # noqa: E402

#: The sampling policy `--provider-policy` defaults to, kept identical to
#: `tests/live/demo_landed_run.py`'s own default so the two drivers behave the same way on the
#: same flags. `uniform` is the original behaviour; `coverage` (T262) draws only from the actions
#: the decision request shows as available right now. Both are ignored by every provider without
#: a sampler (`openrouter`, `fake`).
DEFAULT_PROVIDER_POLICY = "uniform"

GOALS_DIR: Path = Path(__file__).resolve().parent / "goals"
CATALOG_ROOT: Path = REPO / "catalogs"
DEFAULT_STORE: Path = REPO / "civsim-match-store.db"

OBSERVED_START_PREFIX = "observed_start_"
OBSERVED_APPLIED_PREFIX = "observed_applied_"

#: Grammar, not symbols (``catalogs/README.md`` SS4) -- never checked against the fact table.
_PREDICATE_KEYWORDS: frozenset[str] = frozenset(
    {"and", "or", "not", "in", "true", "false", "null"}
)
_STRING_LITERAL_RE = re.compile(r"'[^']*'|\"[^\"]*\"")
_IDENTIFIER_CHAIN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")


class GoalError(ValueError):
    """A goal file is malformed, names an unknown fact, or carries an unevaluable predicate."""


# --------------------------------------------------------------------------
# 1. The goal library
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Goal:
    """One directed-testing goal, loaded from ``tests/live/goals/<id>.yaml``."""

    goal_id: str
    title: str
    objective: str
    """The ONLY field ever shown to the agent (Principle I)."""

    success: str
    prerequisites: tuple[str, ...]
    turn_cap: int
    order: int = 999
    depends_on: str | None = None
    blocked_by: str | None = None
    notes: str = ""

    @property
    def is_blocked(self) -> bool:
        return bool(self.blocked_by)


_REQUIRED_KEYS = ("id", "title", "objective", "success", "turn_cap")
_ALLOWED_KEYS = frozenset(
    {*_REQUIRED_KEYS, "order", "prerequisites", "depends_on", "blocked_by", "notes"}
)


def parse_goal(raw: Mapping[str, Any], *, source: str = "<memory>") -> Goal:
    """Validate one goal mapping and return a :class:`Goal`.

    Raises :class:`GoalError` for a missing or unknown key, a non-positive ``turn_cap``, or a
    ``success``/``prerequisites`` predicate that is not evaluable in this module's fact grammar
    (see :func:`validate_predicate`).
    """
    if not isinstance(raw, Mapping):  # pragma: no cover - defensive
        raise GoalError(f"{source}: a goal file must contain a mapping")
    missing = [key for key in _REQUIRED_KEYS if raw.get(key) in (None, "")]
    if missing:
        raise GoalError(f"{source}: goal is missing required key(s): {', '.join(missing)}")
    unknown = sorted(set(raw) - _ALLOWED_KEYS)
    if unknown:
        raise GoalError(f"{source}: goal carries unknown key(s): {', '.join(unknown)}")

    turn_cap = raw["turn_cap"]
    if not isinstance(turn_cap, int) or isinstance(turn_cap, bool) or turn_cap < 1:
        raise GoalError(f"{source}: turn_cap must be a positive integer, got {turn_cap!r}")

    prerequisites_raw = raw.get("prerequisites") or []
    if not isinstance(prerequisites_raw, Sequence) or isinstance(prerequisites_raw, str):
        raise GoalError(f"{source}: prerequisites must be a list of predicate strings")
    prerequisites = tuple(str(item).strip() for item in prerequisites_raw)

    for label, predicate in [("success", str(raw["success"]))] + [
        (f"prerequisites[{i}]", item) for i, item in enumerate(prerequisites)
    ]:
        validate_predicate(predicate, label=f"{source}:{label}")

    depends_on = raw.get("depends_on")
    return Goal(
        goal_id=str(raw["id"]),
        title=str(raw["title"]).strip(),
        objective=str(raw["objective"]).strip(),
        success=str(raw["success"]).strip(),
        prerequisites=prerequisites,
        turn_cap=turn_cap,
        order=int(raw.get("order", 999)),
        depends_on=str(depends_on) if depends_on else None,
        blocked_by=(str(raw["blocked_by"]).strip() or None) if raw.get("blocked_by") else None,
        notes=str(raw.get("notes") or "").strip(),
    )


def load_goals(goals_dir: Path | None = None) -> dict[str, Goal]:
    """Load and validate every goal in *goals_dir* (default: ``tests/live/goals``)."""
    root = goals_dir if goals_dir is not None else GOALS_DIR
    goals: dict[str, Goal] = {}
    for path in sorted(root.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        goal = parse_goal(raw, source=path.name)
        if goal.goal_id != path.stem:
            raise GoalError(f"{path.name}: goal id {goal.goal_id!r} does not match the file name")
        if goal.goal_id in goals:  # pragma: no cover - impossible with one file per id
            raise GoalError(f"duplicate goal id {goal.goal_id!r}")
        goals[goal.goal_id] = goal
    for goal in goals.values():
        if goal.depends_on is not None and goal.depends_on not in goals:
            raise GoalError(
                f"{goal.goal_id}: depends_on names an unknown goal {goal.depends_on!r}"
            )
    return goals


def load_goal(goal_id: str, goals_dir: Path | None = None) -> Goal:
    goals = load_goals(goals_dir)
    if goal_id not in goals:
        raise GoalError(f"unknown goal {goal_id!r}; known goals: {', '.join(sorted(goals))}")
    return goals[goal_id]


def resolve_chain(goal: Goal, goals: Mapping[str, Goal]) -> list[Goal]:
    """*goal* preceded by everything it ``depends_on``, dependencies first.

    A chain runs its parts back to back in the same client session: part two starts from the
    state part one left the client in, which is the whole point of the build -> use pair.
    """
    chain: list[Goal] = []
    seen: set[str] = set()
    current: Goal | None = goal
    while current is not None:
        if current.goal_id in seen:
            raise GoalError(f"depends_on cycle through {current.goal_id!r}")
        seen.add(current.goal_id)
        chain.append(current)
        current = goals[current.depends_on] if current.depends_on else None
    chain.reverse()
    return chain


# --------------------------------------------------------------------------
# 2. Facts -- what the harness derives from its own recorded observations
# --------------------------------------------------------------------------


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _number(value: Any) -> float | int | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def entries_by_declaration(entries: Iterable[Any]) -> dict[str, Any]:
    """``declaration_id -> value`` for one observation's entries.

    Accepts the raw JSON shape the store holds (``{"declaration_id": ..., "value": ...}``) and
    ``ObservationEntry`` models alike, so a caller may hand this either a store read-back or a
    live observation without converting first.
    """
    out: dict[str, Any] = {}
    for entry in entries:
        if isinstance(entry, Mapping):
            declaration_id = entry.get("declaration_id")
            value = entry.get("value")
        else:
            declaration_id = getattr(entry, "declaration_id", None)
            value = getattr(entry, "value", None)
        if declaration_id is None:
            continue
        out[str(declaration_id)] = value
    return out


def derive_facts(entries: Iterable[Any]) -> dict[str, Any]:
    """Derive the goal fact table from one observation's entries.

    Every fact below is a plain aggregation of a field some ``catalogs/observations/*.yaml``
    declaration actually declares -- no field name is invented here, and nothing is read that the
    agent's own observation did not already carry. A field the client did not report becomes
    ``None`` (or an empty list), never a fabricated zero: a predicate comparing ``None`` to a
    number raises, and :func:`check_predicate` reports that as *not met, with the reason*, which
    is the honest answer.
    """
    by_id = entries_by_declaration(entries)

    turn_state = _mapping(by_id.get("game.turn_state"))
    screen_state = _mapping(by_id.get("game.screen_state"))
    outcome_state = _mapping(by_id.get("game.outcome_state"))
    cities_state = _mapping(by_id.get("cities.state"))
    city_selection = _mapping(by_id.get("cities.selection"))
    units_state = _mapping(by_id.get("units.state"))
    research_state = _mapping(by_id.get("research.state"))
    government_state = _mapping(by_id.get("government.state"))
    religion_state = _mapping(by_id.get("religion.state"))
    diplomacy_state = _mapping(by_id.get("diplomacy.state"))
    yields_state = _mapping(by_id.get("player.yields"))
    map_state = _mapping(by_id.get("map.state"))

    cities = [_mapping(c) for c in _sequence(cities_state.get("cities"))]
    units = [_mapping(u) for u in _sequence(units_state.get("units"))]
    own_cities = [c for c in cities if c.get("owner_is_local_player") is True]
    own_units = [u for u in units if u.get("owner_is_local_player") is True]

    local_player_id: int | None = None
    for record in (*own_cities, *own_units):
        candidate = record.get("owner_player_id")
        if isinstance(candidate, int):
            local_player_id = candidate
            break

    unit_types = [str(u.get("unit_type") or "") for u in own_units]
    builders = [u for u in own_units if "BUILDER" in str(u.get("unit_type") or "")]
    builder_charges = sum(_number(u.get("charges_remaining")) or 0 for u in builders)
    selected_units = [u for u in own_units if u.get("is_selected") is True]
    selected_unit = selected_units[0] if selected_units else None

    available_productions: list[str] = []
    production_queue: list[str] = []
    for city in own_cities:
        available_productions.extend(str(i) for i in _sequence(city.get("available_productions")))
        production_queue.extend(str(i) for i in _sequence(city.get("production_queue")))

    relations = [_mapping(r) for r in _sequence(diplomacy_state.get("relations"))]
    met = [r for r in relations if r.get("has_met") is True]

    improved_owned = 0
    owned_plots = 0
    if local_player_id is not None:
        for plot in (_mapping(p) for p in _sequence(map_state.get("revealed_plots"))):
            if plot.get("owner_player_id") != local_player_id:
                continue
            owned_plots += 1
            if str(plot.get("improvement") or ""):
                improved_owned += 1

    available_policies = [str(p) for p in _sequence(government_state.get("available_policies"))]
    available_beliefs = [str(b) for b in _sequence(religion_state.get("available_beliefs"))]

    return {
        "game": {
            "turn_number": turn_state.get("turn_number"),
            "is_local_player_turn": turn_state.get("is_local_player_turn") is True,
            "is_waiting_for_other_players": (
                turn_state.get("is_waiting_for_other_players") is True
            ),
            "current_screen": str(screen_state.get("screen") or ""),
            "has_blocking_prompt": screen_state.get("has_blocking_prompt") is True,
            "prompt_options": [str(o) for o in _sequence(screen_state.get("prompt_options"))],
            "is_game_over": outcome_state.get("is_game_over") is True,
        },
        "player": {
            "player_id": local_player_id,
            "cities": {
                "count": len(own_cities),
                "ids": [c.get("city_id") for c in own_cities],
                "names": [str(c.get("name") or "") for c in own_cities],
                "total_population": sum(_number(c.get("population")) or 0 for c in own_cities),
                "available_productions": sorted(set(available_productions)),
                "production_queue": production_queue,
                "selected": (
                    city_selection.get("has_selection") is True
                    and city_selection.get("owner_is_local_player") is not False
                ),
                "selected_city_id": city_selection.get("selected_city_id"),
            },
            "units": {
                "count": len(own_units),
                "ids": [u.get("unit_id") for u in own_units],
                "types": unit_types,
                "settler_count": sum(1 for t in unit_types if "SETTLER" in t),
                "builder_count": len(builders),
                "builder_charges_total": builder_charges,
                "can_found_city_count": sum(
                    1 for u in own_units if u.get("can_found_city") is True
                ),
                "movable_count": sum(
                    1 for u in own_units if (_number(u.get("movement_remaining")) or 0) > 0
                ),
                "promotions_available_count": sum(
                    len(_sequence(u.get("available_promotions"))) for u in own_units
                ),
                "selected": selected_unit is not None,
                "selected_unit_id": (
                    selected_unit.get("unit_id") if selected_unit is not None else None
                ),
                "selected_plot": (
                    selected_unit.get("plot") if selected_unit is not None else None
                ),
                "selected_reachable_plots": (
                    _sequence(selected_unit.get("reachable_plots"))
                    if selected_unit is not None
                    else []
                ),
                # 720e30d: the unit panel's build buttons, which units.state reports for the
                # SELECTED unit only -- those buttons exist only on the panel a human has open.
                # `available_builds` is the live-button subset (what units.build_improvement's
                # own availability predicate reads); `available_builds_reason` is why the list is
                # empty when the panel's query could not be asked at all, which is deliberately
                # distinct from "asked, and this tile offers nothing".
                "selected_available_builds": (
                    [str(b) for b in _sequence(selected_unit.get("available_builds"))]
                    if selected_unit is not None
                    else []
                ),
                "selected_build_options_count": (
                    len(_sequence(selected_unit.get("build_options")))
                    if selected_unit is not None
                    else 0
                ),
                "selected_available_builds_reason": (
                    selected_unit.get("available_builds_reason")
                    if selected_unit is not None
                    else None
                ),
            },
            "current_research": research_state.get("current_research"),
            "current_civic": research_state.get("current_civic"),
            "researchable_techs": [
                str(t) for t in _sequence(research_state.get("researchable_techs"))
            ],
            "researchable_civics": [
                str(c) for c in _sequence(research_state.get("researchable_civics"))
            ],
            "current_government": government_state.get("current_government"),
            "available_governments": [
                str(g) for g in _sequence(government_state.get("available_governments"))
            ],
            "available_policies": available_policies,
            "available_policies_count": len(available_policies),
            "available_governors": [
                str(g) for g in _sequence(government_state.get("available_governors"))
            ],
            "pantheon_selected": religion_state.get("pantheon_selected") is True,
            "religion_founded": religion_state.get("religion_founded") is True,
            "available_beliefs": available_beliefs,
            "met_civ_count": len(met),
            "met_player_ids": [r.get("player_id") for r in met],
            "delegation_count": sum(1 for r in met if r.get("has_delegation") is True),
            "gold": _number(yields_state.get("gold_balance")),
            "faith": _number(yields_state.get("faith_balance")),
            "science_per_turn": _number(yields_state.get("science_per_turn")),
            "culture_per_turn": _number(yields_state.get("culture_per_turn")),
            "owned_plot_count": owned_plots,
            "owned_improved_plot_count": improved_owned,
        },
    }


#: Which facts get an ``observed_start_<name>`` flat snapshot. Kept explicit rather than derived
#: from the whole tree so the symbol table a goal may write against is enumerable and stable.
_START_SNAPSHOT_FACTS: Mapping[str, tuple[str, ...]] = {
    "turn_number": ("game", "turn_number"),
    "cities_count": ("player", "cities", "count"),
    "cities_total_population": ("player", "cities", "total_population"),
    "units_count": ("player", "units", "count"),
    "settler_count": ("player", "units", "settler_count"),
    "builder_count": ("player", "units", "builder_count"),
    "builder_charges_total": ("player", "units", "builder_charges_total"),
    "promotions_available_count": ("player", "units", "promotions_available_count"),
    "current_research": ("player", "current_research"),
    "current_civic": ("player", "current_civic"),
    "current_government": ("player", "current_government"),
    "available_policies_count": ("player", "available_policies_count"),
    "pantheon_selected": ("player", "pantheon_selected"),
    "religion_founded": ("player", "religion_founded"),
    "met_civ_count": ("player", "met_civ_count"),
    "delegation_count": ("player", "delegation_count"),
    "gold": ("player", "gold"),
    "owned_plot_count": ("player", "owned_plot_count"),
    "owned_improved_plot_count": ("player", "owned_improved_plot_count"),
}


def _dig(facts: Mapping[str, Any], path: Sequence[str]) -> Any:
    current: Any = facts
    for part in path:
        current = _mapping(current).get(part)
    return current


def start_snapshot(start_facts: Mapping[str, Any]) -> dict[str, Any]:
    """The ``observed_start_*`` half of the bindings, taken from this goal's first observation."""
    return {
        f"{OBSERVED_START_PREFIX}{name}": _dig(start_facts, path)
        for name, path in _START_SNAPSHOT_FACTS.items()
    }


def applied_action_symbol(declaration_id: str) -> str:
    return f"{OBSERVED_APPLIED_PREFIX}{declaration_id.replace('.', '_')}"


@lru_cache(maxsize=4)
def action_declaration_ids(catalog_root: Path | None = None) -> tuple[str, ...]:
    """Every ``kind: action`` declaration id in the loaded catalog.

    Read from the catalog rather than hard-coded so an ``observed_applied_*`` symbol can only
    ever name an action that really exists -- a goal naming an action the catalog does not
    declare fails :func:`validate_predicate` at load time instead of silently reading zero.
    """
    catalog = load_catalog(catalog_root if catalog_root is not None else CATALOG_ROOT)
    return tuple(
        sorted(
            str(declaration_id)
            for declaration_id, declaration in catalog.declarations.items()
            if declaration.kind is DeclarationKind.ACTION
        )
    )


def build_bindings(
    facts: Mapping[str, Any],
    *,
    start_facts: Mapping[str, Any] | None = None,
    applied_counts: Mapping[str, int] | None = None,
    catalog_root: Path | None = None,
) -> dict[str, Any]:
    """The full bindings mapping a goal predicate is evaluated against.

    Every catalogued action gets an ``observed_applied_*`` entry, zero when the agent never
    applied it, so a predicate can never fail to resolve merely because the run did not happen to
    use that action.
    """
    bindings: dict[str, Any] = dict(facts)
    bindings.update(start_snapshot(start_facts if start_facts is not None else facts))
    counts = dict(applied_counts or {})
    for declaration_id in action_declaration_ids(catalog_root):
        bindings[applied_action_symbol(declaration_id)] = counts.get(declaration_id, 0)
    return bindings


# --------------------------------------------------------------------------
# 3. Predicate validation and evaluation
# --------------------------------------------------------------------------


_PROTOTYPE_ENTRIES: tuple[dict[str, Any], ...] = (
    {
        "declaration_id": "game.turn_state",
        "value": {
            "turn_number": 1,
            "is_local_player_turn": True,
            "is_waiting_for_other_players": False,
        },
    },
    {
        "declaration_id": "game.screen_state",
        "value": {"screen": "world", "has_blocking_prompt": False, "prompt_options": []},
    },
    {"declaration_id": "game.outcome_state", "value": {"is_game_over": False, "outcome": "none"}},
    {"declaration_id": "cities.state", "value": {"cities": []}},
    {"declaration_id": "cities.selection", "value": {"has_selection": False}},
    {"declaration_id": "units.state", "value": {"units": []}},
    {
        "declaration_id": "research.state",
        "value": {
            "current_research": "",
            "current_civic": "",
            "researchable_techs": [],
            "researchable_civics": [],
        },
    },
    {
        "declaration_id": "government.state",
        "value": {
            "current_government": "",
            "available_governments": [],
            "available_policies": [],
            "available_governors": [],
            "governors": [],
        },
    },
    {
        "declaration_id": "religion.state",
        "value": {
            "pantheon_selected": False,
            "religion_founded": False,
            "available_beliefs": [],
            "city_majority_religions": [],
        },
    },
    {"declaration_id": "diplomacy.state", "value": {"relations": []}},
    {
        "declaration_id": "player.yields",
        "value": {
            "gold_balance": 0,
            "faith_balance": 0,
            "science_per_turn": 0,
            "culture_per_turn": 0,
        },
    },
    {"declaration_id": "map.state", "value": {"width": 1, "height": 1, "revealed_plots": []}},
)


def prototype_bindings(catalog_root: Path | None = None) -> dict[str, Any]:
    """A complete, representatively-typed bindings mapping used for load-time validation."""
    return build_bindings(derive_facts(_PROTOTYPE_ENTRIES), catalog_root=catalog_root)


def _flatten_names(value: Any, prefix: str = "") -> set[str]:
    names: set[str] = set()
    if isinstance(value, Mapping):
        for key, child in value.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            names.add(name)
            names |= _flatten_names(child, name)
    return names


@lru_cache(maxsize=4)
def known_fact_names(catalog_root: Path | None = None) -> frozenset[str]:
    """Every dotted name a goal predicate may reference."""
    return frozenset(_flatten_names(prototype_bindings(catalog_root)))


def predicate_symbols(predicate: str) -> frozenset[str]:
    """The dotted identifier chains *predicate* references, grammar keywords excluded."""
    stripped = _STRING_LITERAL_RE.sub(" ", predicate)
    return frozenset(
        match.group(0)
        for match in _IDENTIFIER_CHAIN_RE.finditer(stripped)
        if match.group(0).lower() not in _PREDICATE_KEYWORDS
    )


def validate_predicate(
    predicate: str, *, label: str = "predicate", catalog_root: Path | None = None
) -> None:
    """Raise :class:`GoalError` unless *predicate* is evaluable against the goal fact table.

    Three ways to fail, all at goal-load time rather than mid-run:

    1. It is not the catalog's predicate grammar at all (a function call, ``*``, an assignment),
       or it touches a root namespace outside ``EXPOSED_PREDICATE_NAMESPACES`` /
       ``observed_*`` -- ``evaluate_predicate`` itself refuses, structurally.
    2. It names a dotted fact this module does not derive (a typo, or a catalog field with no
       aggregation here). ``act.predicates`` resolves an unknown attribute on a mapping to
       ``None`` rather than raising, which would make a typo read as a quietly false predicate
       forever -- so the full chain is checked against :func:`known_fact_names` here.
    3. It compares incompatible types even against representative values.
    """
    bindings = prototype_bindings(catalog_root)
    unknown = sorted(predicate_symbols(predicate) - known_fact_names(catalog_root))
    if unknown:
        raise GoalError(
            f"{label}: predicate references unknown fact(s) {', '.join(unknown)} -- "
            f"goal predicates may only name facts derived from the observation catalog "
            f"(see derive_facts) or an {OBSERVED_START_PREFIX}*/{OBSERVED_APPLIED_PREFIX}* symbol"
        )
    try:
        evaluate_predicate(predicate, bindings)
    except PredicateEvaluationError as exc:
        raise GoalError(f"{label}: {exc}") from exc


@dataclass(frozen=True)
class PredicateResult:
    predicate: str
    met: bool
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"predicate": self.predicate, "met": self.met, "reason": self.reason}


def check_predicate(predicate: str, bindings: Mapping[str, Any]) -> PredicateResult:
    """Evaluate *predicate*, reporting an unevaluable predicate as **not met, with the reason**.

    Never returns ``met=True`` for a predicate that could not be evaluated -- the same discipline
    ``act.verify`` applies to a verification predicate (FR-011). An absent observation (no
    ``player.yields`` on this step, so ``player.gold`` is ``None``) therefore reads as "not
    confirmed", never as "confirmed".
    """
    try:
        met = bool(evaluate_predicate(predicate, bindings))
    except PredicateEvaluationError as exc:
        return PredicateResult(predicate=predicate, met=False, reason=str(exc))
    return PredicateResult(predicate=predicate, met=met)


def check_prerequisites(
    goal: Goal, bindings: Mapping[str, Any]
) -> tuple[bool, list[PredicateResult]]:
    results = [check_predicate(p, bindings) for p in goal.prerequisites]
    return all(r.met for r in results), results


# --------------------------------------------------------------------------
# 4. Reading the harness's own record back
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class StepRecord:
    turn_number: int
    step_index: int
    entries: tuple[Any, ...]
    action_declaration_id: str
    outcome: str
    rejection_reason: str | None


@dataclass
class RunRecords:
    """Everything a goal check needs from one run, read straight out of the store."""

    steps: list[StepRecord] = field(default_factory=list)

    @property
    def latest_entries(self) -> tuple[Any, ...]:
        return self.steps[-1].entries if self.steps else ()

    @property
    def first_entries(self) -> tuple[Any, ...]:
        return self.steps[0].entries if self.steps else ()

    @property
    def turns_recorded(self) -> list[int]:
        return sorted({s.turn_number for s in self.steps})

    def applied_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for step in self.steps:
            if step.outcome == "applied":
                counts[step.action_declaration_id] = (
                    counts.get(step.action_declaration_id, 0) + 1
                )
        return counts

    def action_tally(self) -> dict[str, dict[str, int]]:
        """``{declaration_id: {outcome_or_rejection_reason: count}}`` -- what the issue entry
        quotes as "actions applied / refused by id"."""
        tally: dict[str, dict[str, int]] = {}
        for step in self.steps:
            key = step.rejection_reason if step.outcome == "rejected" else step.outcome
            bucket = tally.setdefault(step.action_declaration_id, {})
            bucket[key or step.outcome] = bucket.get(key or step.outcome, 0) + 1
        return tally


def read_run_records(store_path: Path, run_id: str) -> RunRecords:
    """Every authoritative decision step of *run_id*, in turn/step order.

    Read through plain SQLite over the store's own JSON columns, exactly as
    ``demo_landed_run.store_counts`` does: this is a read of the far-side record, never a value
    this process carried along in memory.
    """
    records = RunRecords()
    connection = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "select tc.turn_number, ds.step_index, ds.bundle_json "
            "from decision_steps ds "
            "join turn_cycles tc on tc.turn_cycle_id = ds.turn_cycle_id "
            "where tc.run_id = ? and tc.is_authoritative = 1 "
            "order by tc.turn_number, ds.step_index",
            (run_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return records
    finally:
        connection.close()

    for turn_number, step_index, bundle_json in rows:
        bundle = json.loads(bundle_json)
        observation = _mapping(bundle.get("observation"))
        decision = _mapping(bundle.get("decision"))
        execution = _mapping(decision.get("execution"))
        records.steps.append(
            StepRecord(
                turn_number=int(turn_number),
                step_index=int(step_index),
                entries=tuple(_sequence(observation.get("entries"))),
                action_declaration_id=str(decision.get("action_declaration_id") or ""),
                outcome=str(execution.get("outcome") or ""),
                rejection_reason=(
                    str(execution["rejection_reason"])
                    if execution.get("rejection_reason")
                    else None
                ),
            )
        )
    return records


def latest_run_id(store_path: Path) -> str | None:
    """The most recently started run in *store_path*.

    Falls back to insertion order on a 1.0-schema file, where ``started_at`` exists only inside
    ``run_json`` and not as a projection column (store/schema.py, ``ADDED_COLUMNS_1_1``).
    """
    connection = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)
    try:
        for statement in (
            "select run_id from runs order by coalesce(started_at, '') desc, rowid desc limit 1",
            "select run_id from runs order by rowid desc limit 1",
        ):
            try:
                row = connection.execute(statement).fetchone()
            except sqlite3.OperationalError:
                continue
            return str(row[0]) if row else None
        return None
    finally:
        connection.close()


# --------------------------------------------------------------------------
# 5. Feasibility -- which goals are runnable from this state
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Feasibility:
    goal_id: str
    title: str
    runnable: bool
    blocked_by: str | None
    prerequisites: list[PredicateResult]

    def as_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal_id,
            "title": self.title,
            "runnable": self.runnable,
            "blocked_by": self.blocked_by,
            "prerequisites": [p.as_dict() for p in self.prerequisites],
        }


def assess_feasibility(
    entries: Iterable[Any],
    *,
    goals: Mapping[str, Goal] | None = None,
    applied_counts: Mapping[str, int] | None = None,
    catalog_root: Path | None = None,
) -> list[Feasibility]:
    """Evaluate every goal's prerequisites against one observation's *entries*.

    A goal carrying ``blocked_by`` is never reported runnable, whatever its prerequisites say --
    the block is a statement about the harness, not about the game state.
    """
    library = goals if goals is not None else load_goals()
    facts = derive_facts(entries)
    bindings = build_bindings(facts, applied_counts=applied_counts, catalog_root=catalog_root)
    report: list[Feasibility] = []
    for goal in sorted(library.values(), key=lambda g: (g.order, g.goal_id)):
        met, results = check_prerequisites(goal, bindings)
        report.append(
            Feasibility(
                goal_id=goal.goal_id,
                title=goal.title,
                runnable=met and not goal.is_blocked,
                blocked_by=goal.blocked_by,
                prerequisites=results,
            )
        )
    return report


def format_feasibility(report: Sequence[Feasibility]) -> str:
    lines = [
        f"{'goal':<24} {'runnable':<9} why not",
        f"{'-' * 24} {'-' * 9} {'-' * 40}",
    ]
    for row in report:
        if row.runnable:
            why = ""
        elif row.blocked_by:
            why = f"BLOCKED: {' '.join(row.blocked_by.split())[:80]}"
        else:
            unmet = [p for p in row.prerequisites if not p.met]
            why = "; ".join(p.predicate for p in unmet) or "(no reason recorded)"
        lines.append(f"{row.goal_id:<24} {'yes' if row.runnable else 'no':<9} {why}")
    runnable = [r.goal_id for r in report if r.runnable]
    lines.append("")
    lines.append(f"runnable now ({len(runnable)}): {', '.join(runnable) if runnable else '(none)'}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 6. The objective's path to the agent (Principle I)
# --------------------------------------------------------------------------


GOAL_GUIDANCE_PREAMBLE = (
    "Objective for this session. Accomplish the following in the game, using only what you can "
    "see on screen and the actions available to you. This is the one thing you are asked to do; "
    "once it is done, keep playing normally.\n\n"
)


def write_goal_guidance(goal: Goal, directory: Path) -> Path:
    """Write *goal*'s objective where the composition root's guidance seam will read it.

    ``build_runner_dependencies(guidance_root=...)`` plus ``guidance_set: <file>`` in the run
    configuration is the hook this module uses: ``_prepare_run`` loads it through
    ``config/guidance.py``'s ``load_guidance`` (content-addressed, recorded on the run by hash,
    V6) and hands the resulting ``GuidanceSet`` to every decision step, where
    ``agent/context.py``'s ``assemble_system_prompt`` appends its ``content`` verbatim below the
    fixed ``ROLE_TEXT``. No new path into the model's context is opened, and nothing
    run-specific, telemetric, or hidden can travel this way -- ``GuidanceSet`` has no field for
    any of it (that module's own docstring).

    **Only the objective is written.** ``success``, ``prerequisites``, ``blocked_by`` and
    ``notes`` stay harness-side; writing any of them here would hand the agent game information
    a human player does not have, which Principle I forbids outright.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"goal-{goal.goal_id}.md"
    path.write_text(GOAL_GUIDANCE_PREAMBLE + goal.objective + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# 7. The driver
# --------------------------------------------------------------------------


def _demo() -> Any:
    """``tests/live/demo_landed_run.py``, imported lazily.

    Lazily because it pulls Pillow and the host adapter in, which a feasibility report and every
    unit test have no use for; by ``__package__`` because this module is imported as
    ``tests.live.goal_run`` when run with ``-m`` and as ``live.goal_run`` from the test suite.
    """
    package = __package__ or "tests.live"
    return importlib.import_module(f"{package}.demo_landed_run")


def build_goal_provider(name: str, *, seed: int, policy: str) -> Any:
    """The provider this driver hands the composition root, resolved as the demo driver does.

    Its own named function, rather than an inline call inside :func:`run_goal`, so a unit test can
    exercise **this exact call** against the real ``run/composition.build_provider`` for every
    provider name. MEASURED the hard way (2026-09-21): T262 gave ``build_provider`` and the demo
    driver's ``resolve_provider`` a new required ``policy`` argument, and this driver -- whose
    only call site was three frames inside a live run -- aborted with a ``TypeError`` on the
    client instead of in CI. A signature drift must fail in the suite now.
    """
    return _demo().resolve_provider(name, seed=seed, policy=policy)


def _patch_config_for_goal(config_path: Path, goal: Goal, guidance_file: str) -> None:
    """Point the written run configuration at this goal's guidance file and turn cap.

    ``demo_landed_run.write_config`` is reused unchanged for everything it already does -- the
    charter assertions against the live read-back, the seed set, the model chain -- and only the
    two goal-specific keys are set here, on the file it produced.
    """
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["guidance_set"] = guidance_file
    config["config_id"] = f"goal-{goal.goal_id}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


def _evaluate_against_store(
    goal: Goal, records: RunRecords, *, catalog_root: Path | None = None
) -> tuple[PredicateResult, dict[str, Any]]:
    start_facts = derive_facts(records.first_entries)
    facts = derive_facts(records.latest_entries)
    bindings = build_bindings(
        facts,
        start_facts=start_facts,
        applied_counts=records.applied_counts(),
        catalog_root=catalog_root,
    )
    return check_predicate(goal.success, bindings), bindings


def run_goal(
    goal: Goal,
    part_dir: Path,
    *,
    provider: str,
    provider_seed: int,
    provider_policy: str = DEFAULT_PROVIDER_POLICY,
    turns: int,
    store_path: Path,
    host: Any,
    recorder: Any,
    use_seed_set: bool,
    force: bool,
    catalog_root: Path | None = None,
) -> dict[str, Any]:
    """Play one goal through the production composition root and report what the store says.

    Composed exactly as ``demo_landed_run`` composes it -- ``build_runner_dependencies`` ->
    ``Runner.start(config)`` -- with one addition: ``guidance_root`` points at *part_dir*, where
    this goal's objective has just been written.
    """
    demo = _demo()
    from civsim_harness.run.composition import build_runner_dependencies
    from civsim_harness.run.runner import Runner, RunPreparationFailed
    from civsim_harness.store.sqlite_adapter import SqliteMatchStore

    part_dir.mkdir(parents=True, exist_ok=True)
    guidance_path = write_goal_guidance(goal, part_dir)
    recorder.note(
        f"goal {goal.goal_id}: objective written to {guidance_path.name} (objective text only)"
    )

    # The production `LuaGameSetupReader` read-back, and the charter assertions over it, exactly
    # as the demo driver performs them -- reused, not re-implemented.
    setup = asyncio.run(demo.read_setup())
    config_path, start_turn = demo.write_config(
        part_dir, setup, provider=provider, turns=turns, use_seed_set=use_seed_set
    )
    _patch_config_for_goal(config_path, goal, guidance_path.name)

    store = SqliteMatchStore(store_path)
    deps = build_runner_dependencies(
        store=store,
        host=host,
        catalog_root=catalog_root if catalog_root is not None else CATALOG_ROOT,
        guidance_root=part_dir,
        provider=build_goal_provider(provider, seed=provider_seed, policy=provider_policy),
    )
    runner = Runner(deps)
    recorder.note(
        f"PRODUCTION: Runner.start({config_path.name}) [goal={goal.goal_id} "
        f"provider={provider} provider_policy={provider_policy} turn_cap={turns}]"
    )
    try:
        run_id = runner.start(config_path)
    except RunPreparationFailed as exc:
        recorder.note(f"goal {goal.goal_id}: run preparation FAILED: {exc}")
        return {
            "goal": goal.goal_id,
            "title": goal.title,
            "turn_cap": turns,
            "provider": provider,
            "provider_policy": provider_policy,
            "reached": False,
            "blocked_by": goal.blocked_by,
            "error": str(exc),
        }
    recorder.note(f"goal {goal.goal_id}: run {run_id}")

    result = drive_goal(
        goal,
        runner=runner,
        run_id=run_id,
        read_records=lambda: read_run_records(store_path, str(run_id)),
        turn_cap=turns,
        force=force,
        note=recorder.note,
        catalog_root=catalog_root,
    )
    result["provider"] = provider
    result["provider_policy"] = provider_policy
    result["game_start_turn"] = start_turn
    result["store"] = demo.store_counts(store_path, str(run_id))
    if result.get("final_state") == "paused":
        # This process is the run's only holder; a stale run-identity lock would refuse the next
        # goal on the same client (T235 tripwire) -- the same operator step the demo driver takes.
        lock = Path("/tmp/civsim_harness/run_locks") / f"{run_id}.lock.json"
        if lock.exists():
            lock.unlink()
            recorder.note(f"goal {goal.goal_id}: stale run-identity lock cleared ({lock.name})")
    return result


def drive_goal(
    goal: Goal,
    *,
    runner: Any,
    run_id: Any,
    read_records: Callable[[], RunRecords],
    turn_cap: int,
    force: bool = False,
    note: Callable[[str], None] = lambda _message: None,
    catalog_root: Path | None = None,
    sleep: Callable[[float], None] = time.sleep,
    poll_interval_s: float = 2.0,
) -> dict[str, Any]:
    """Poll a started run, evaluate *goal* harness-side after every turn, and stop at the answer.

    Split out from :func:`run_goal` with every collaborator injected so the stop rules -- stop at
    success, stop at the turn cap, stop when the starting observation says the goal is not
    feasible -- are exercised against fakes rather than only against a live client.

    Three reasons this function asks the run to stop, each recorded:

    - **success**: the goal's ``success`` predicate holds against the observations recorded so
      far. This is the point of the exercise, and the run stops the moment it is true rather than
      playing out the cap.
    - **turn cap**: the run configuration already carries ``stop_condition: turn_reached``, so the
      runner normally ends itself; this is the driver's own backstop for a configuration that
      somehow outlives it, so a goal run can never become an unbounded run.
    - **prerequisites**: they did not hold in the run's *first* observation, so the goal was never
      feasible from this state. Stopping costs a step instead of a cap's worth of model calls.
      ``--force`` plays it anyway.
    """
    from civsim_harness.models.run import LifecycleState

    terminal = {LifecycleState.FINISHED, LifecycleState.FAILED}
    result: dict[str, Any] = {
        "goal": goal.goal_id,
        "title": goal.title,
        "run_id": str(run_id),
        "turn_cap": turn_cap,
        "reached": False,
        "reached_at_turn": None,
        "prerequisites_met": None,
        "blocked_by": goal.blocked_by,
        "stop_reason": None,
    }
    prerequisites_checked = False
    last_key: tuple[Any, ...] | None = None
    started = time.perf_counter()
    while True:
        status = runner.get_status(run_id)
        key = (status.lifecycle_state, status.current_turn, status.current_step)
        if key != last_key:
            note(
                f"goal {goal.goal_id}: {status.lifecycle_state.value} "
                f"turn={status.current_turn} step={status.current_step}"
            )
            last_key = key

        records = read_records()
        if records.steps and not prerequisites_checked:
            prerequisites_checked = True
            met, checks = check_prerequisites(
                goal,
                build_bindings(derive_facts(records.first_entries), catalog_root=catalog_root),
            )
            result["prerequisites_met"] = met
            result["prerequisites"] = [c.as_dict() for c in checks]
            if not met and not force:
                unmet = "; ".join(c.predicate for c in checks if not c.met)
                note(
                    f"goal {goal.goal_id}: prerequisites NOT met in the starting observation "
                    f"({unmet}); stopping the run"
                )
                result["stop_reason"] = "prerequisites_not_met"
                runner.request_stop(run_id)

        if records.steps:
            success, _bindings = _evaluate_against_store(goal, records, catalog_root=catalog_root)
            if success.met and not result["reached"]:
                result["reached"] = True
                result["reached_at_turn"] = records.turns_recorded[-1]
                result["stop_reason"] = "success"
                note(
                    f"goal {goal.goal_id}: REACHED at recorded turn "
                    f"{result['reached_at_turn']} -- stopping the run"
                )
                runner.request_stop(run_id)
            elif (
                records.turns_recorded
                and records.turns_recorded[-1] >= turn_cap
                and result["stop_reason"] is None
            ):
                result["stop_reason"] = "turn_cap"
                note(f"goal {goal.goal_id}: turn cap {turn_cap} reached -- stopping the run")
                runner.request_stop(run_id)

        if status.lifecycle_state in terminal:
            break
        if status.lifecycle_state is LifecycleState.PAUSED:
            note(f"goal {goal.goal_id}: run paused -- this part's terminal state")
            break
        sleep(poll_interval_s)

    records = read_records()
    success, _bindings = _evaluate_against_store(goal, records, catalog_root=catalog_root)
    if success.met and not result["reached"]:
        result["reached"] = True
        result["reached_at_turn"] = records.turns_recorded[-1] if records.steps else None
        result["stop_reason"] = result["stop_reason"] or "success"
    result.update(
        {
            "final_state": status.lifecycle_state.value,
            "elapsed_s": round(time.perf_counter() - started, 1),
            "success_predicate": goal.success,
            "success_check": success.as_dict(),
            "steps": len(records.steps),
            "turns_recorded": records.turns_recorded,
            "actions": records.action_tally(),
            "actions_applied": records.applied_counts(),
        }
    )
    return result


def summarize(results: Mapping[str, Any]) -> str:
    """A Markdown block ready to paste into a GitHub issue entry."""
    lines: list[str] = []
    parts = results.get("parts") or []
    head = parts[-1] if parts else {}
    chain = " -> ".join(str(p.get("goal")) for p in parts)
    lines.append(f"### Goal run: {chain or results.get('goal', '?')}")
    lines.append("")
    lines.append(
        f"- provider: `{results.get('provider')}`"
        f" (policy `{results.get('provider_policy')}`) | started {results.get('started_at')} "
        f"| finished {results.get('finished_at')}"
    )
    for part in parts:
        reached = "REACHED" if part.get("reached") else "not reached"
        lines.append("")
        lines.append(f"**{part.get('goal')} -- {part.get('title')}: {reached}**")
        lines.append("")
        lines.append(
            f"- run: `{part.get('run_id', 'n/a')}` | final state: {part.get('final_state')}"
        )
        lines.append(
            f"- turns recorded: {part.get('turns_recorded')} (cap {part.get('turn_cap')}) "
            f"| decision steps: {part.get('steps')}"
        )
        if part.get("reached"):
            lines.append(f"- reached at recorded turn {part.get('reached_at_turn')}")
        lines.append(f"- success predicate (harness-side): `{part.get('success_predicate')}`")
        check = part.get("success_check") or {}
        if check.get("reason"):
            lines.append(f"  - not evaluable: {check['reason']}")
        if part.get("prerequisites_met") is False:
            unmet = [
                p["predicate"] for p in (part.get("prerequisites") or []) if not p.get("met")
            ]
            lines.append(f"- prerequisites NOT met at start: {', '.join(unmet)}")
        if part.get("blocked_by"):
            lines.append(f"- blocked_by: {' '.join(str(part['blocked_by']).split())}")
        actions = part.get("actions") or {}
        if actions:
            lines.append("- actions by id:")
            for action_id in sorted(actions):
                outcomes = ", ".join(f"{k}={v}" for k, v in sorted(actions[action_id].items()))
                lines.append(f"  - `{action_id}`: {outcomes}")
        store = part.get("store") or {}
        lines.append(
            f"- model calls: {store.get('model_calls', 0)} "
            f"| cost: ${store.get('model_cost_usd', 0)} "
            f"| images shown: {store.get('captures_shown_to_agent', 0)}"
        )
    recording = results.get("recording") or {}
    if recording.get("frames"):
        lines.append("")
        lines.append(
            f"- frames: {recording.get('frames')} "
            f"({recording.get('gif')}, {recording.get('gif_kb')} KB); "
            f"keyframes: {', '.join(recording.get('keyframes') or [])}"
        )
    lines.append("")
    lines.append(
        "_Principle I: the agent was given the objective text only. The success predicate and "
        "the prerequisites above were evaluated by the harness against the observations already "
        "in the store; neither was shown to the agent._"
    )
    if head.get("error"):
        lines.append("")
        lines.append(f"- error: `{head['error']}`")
    return "\n".join(lines) + "\n"


def _feasibility_main(args: argparse.Namespace) -> int:
    store_path = Path(args.store)
    run_id = args.run_id or latest_run_id(store_path)
    if run_id is None:
        print(f"no run found in {store_path}", file=sys.stderr)
        return 2
    records = read_run_records(store_path, run_id)
    if not records.steps:
        print(f"run {run_id} has no recorded decision steps in {store_path}", file=sys.stderr)
        return 2
    report = assess_feasibility(
        records.latest_entries, applied_counts=records.applied_counts()
    )
    latest = records.steps[-1]
    print(f"store:      {store_path}")
    print(f"run:        {run_id}")
    print(f"observation: turn {latest.turn_number}, step {latest.step_index}")
    print()
    print(format_feasibility(report))
    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {
                    "store": str(store_path),
                    "run_id": run_id,
                    "turn_number": latest.turn_number,
                    "step_index": latest.step_index,
                    "goals": [row.as_dict() for row in report],
                },
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one directed-testing goal (or a depends_on chain) against a live client."
    )
    parser.add_argument("out", nargs="?", help="output directory for this goal run")
    parser.add_argument("--goal", help="goal id from tests/live/goals/")
    parser.add_argument("--provider", choices=("fake", "openrouter", "stochastic"), default="fake")
    parser.add_argument("--provider-seed", type=int, default=0)
    # T262, same choices and same default as tests/live/demo_landed_run.py: ignored by every
    # provider without a sampler, so a goal run on `openrouter` is unaffected by it.
    parser.add_argument(
        "--provider-policy",
        choices=PROVIDER_POLICY_NAMES,
        default=DEFAULT_PROVIDER_POLICY,
        help=(
            "how --provider stochastic samples: 'coverage' draws only from the actions the "
            "request shows as available now; 'uniform' (default) draws from everything it lists"
        ),
    )
    parser.add_argument(
        "--turns", type=int, default=None, help="override the goal's own turn_cap"
    )
    parser.add_argument("--store", default=str(DEFAULT_STORE))
    parser.add_argument("--no-seed-set", action="store_true")
    parser.add_argument(
        "--force",
        action="store_true",
        help="play the goal even when its prerequisites do not hold at the first observation",
    )
    parser.add_argument(
        "--feasibility",
        action="store_true",
        help="evaluate every goal's prerequisites against the latest observation and exit",
    )
    parser.add_argument("--run-id", help="--feasibility: which run to read (default: the latest)")
    parser.add_argument("--json", help="--feasibility: also write the report as JSON here")
    parser.add_argument("--list", action="store_true", help="list the goal library and exit")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.list:
        for goal in sorted(load_goals().values(), key=lambda g: (g.order, g.goal_id)):
            blocked = goal.blocked_by
            flag = f"  BLOCKED: {' '.join(blocked.split())[:90]}..." if blocked else ""
            depends = f" (after {goal.depends_on})" if goal.depends_on else ""
            print(f"{goal.goal_id:<24} cap={goal.turn_cap:<3} {goal.title}{depends}{flag}")
        return 0

    if args.feasibility:
        return _feasibility_main(args)

    if not args.goal or not args.out:
        print("--goal and OUT_DIR are required (or use --feasibility / --list)", file=sys.stderr)
        return 2

    goals = load_goals()
    if args.goal not in goals:
        print(f"unknown goal {args.goal!r}; known: {', '.join(sorted(goals))}", file=sys.stderr)
        return 2
    chain = resolve_chain(goals[args.goal], goals)

    demo = _demo()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    host = demo.get_host_platform()
    recorder = demo.Recorder(host)
    recorder.start()
    recorder.note(
        "recording started: every frame via the production capture_window() (window-scoped)"
    )
    results: dict[str, Any] = {
        "goal": args.goal,
        "chain": [g.goal_id for g in chain],
        "provider": args.provider,
        "provider_seed": args.provider_seed,
        "provider_policy": args.provider_policy,
        "store": str(args.store),
        "started_at": datetime.now(UTC).isoformat(),
        "parts": [],
    }
    try:
        for index, goal in enumerate(chain, start=1):
            part = run_goal(
                goal,
                out / f"{index:02d}-{goal.goal_id}",
                provider=args.provider,
                provider_seed=args.provider_seed,
                provider_policy=args.provider_policy,
                turns=args.turns if args.turns is not None else goal.turn_cap,
                store_path=Path(args.store),
                host=host,
                recorder=recorder,
                use_seed_set=not args.no_seed_set,
                force=args.force,
            )
            results["parts"].append(part)
            if not part.get("reached"):
                recorder.note(
                    f"goal {goal.goal_id} not reached; the chain stops here "
                    f"(a later part starts from a state that never arrived)"
                )
                break
    except BaseException as exc:  # noqa: BLE001 - the record must be written whatever happened
        recorder.note(f"ABORTED: {type(exc).__name__}: {exc}")
        results["aborted"] = f"{type(exc).__name__}: {exc}"
    finally:
        recorder.stop()
        results["recording"] = recorder.write(out)
        results["finished_at"] = datetime.now(UTC).isoformat()
        results["reached"] = bool(
            results["parts"] and all(p.get("reached") for p in results["parts"])
        )
        (out / "result.json").write_text(
            json.dumps(results, indent=2, default=str), encoding="utf-8"
        )
        (out / "summary.md").write_text(summarize(results), encoding="utf-8")
        print(json.dumps(results, indent=2, default=str))
    return 0 if results.get("reached") else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
