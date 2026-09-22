"""T258, Constitution III: the turn's yields, from the human's top bar, onto the turn record.

Every turn of every run must be persisted "with enough game state captured to reconstruct
decisions, yields, and outcomes without replaying the game" (Constitution III), and a run whose
record has gaps may not feed trending. Until this module existed, ``run/turn_cycle.py``'s
``compute_yields`` was its ``_no_yields`` placeholder in production, so ``TurnCycle.yields`` was
``{}`` on every turn of every run and the match-tracking store's science / culture / gold / faith
series (003) were empty by construction.

The source is the ``player.yields`` observation (``catalogs/observations/yields.yaml``,
``lua/ingame/yields.lua``): the seven numbers on the local player's top bar, read through the
accessors Firaxis's own ``toppanel.lua`` uses and nothing else (Principle I). This module takes
them from the turn's **last** observation -- the board as it stood when the turn ended, the same
observation the store's ``turn_metrics_from`` derives city and unit counts from -- and renders
them under the metric names the store and the web interface plot (``science``, ``culture``,
``gold``, ``faith``, ``tourism``, plus the two balances). A metric the client did not answer is
absent, never zero: the record then carries a gap for it, which is what Constitution III wants a
gap to look like.

**Why this list is seven and not 003 FR-018's eight** (T272, 2026-09-22). FR-018 named "science,
culture, gold, faith, production and food per turn, plus city and unit counts". Of the four that
this module does not emit:

* ``city_count`` and ``unit_count`` were never this module's to emit and are **not missing**. The
  store derives them per turn from the attempt's last observation (``store/trends.py``'s
  ``turn_metrics_from`` -> ``city_count_from`` / ``unit_count_from``, off ``cities.state`` and
  ``units.state``). MEASURED on the live store, 2026-09-22: all 93 recorded last-step observations
  carry both lists, so both series are populated on real data today.
* ``production`` and ``food`` have no producer **and no empire-level source to produce them from**.
  Civilization VI shows food and production per city -- the city panel and the city banner -- and
  displays no empire-wide figure for either anywhere in the standard UI, so there is nothing on the
  top bar this module could read and no parity basis for claiming there is. FR-018 was amended for
  exactly those two rather than satisfied with a harness-computed aggregate
  (``specs/003-match-tracking-store/spec.md``, FR-018 + its Principle I note). The per-city figures
  are a separate, legitimate future series, and reading them needs a Lua accessor that does not
  exist yet -- see that note for the ask.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from civsim_harness.models.common import DeclarationId
from civsim_harness.models.turn import Observation
from civsim_harness.run.decision_loop import DecisionLoopResult

#: The observation this module reads.
YIELDS_DECLARATION_ID: Final[DeclarationId] = DeclarationId("player.yields")

#: Observation field -> recorded metric name. The per-turn rates take the plain names the store's
#: trend queries and the web's metrics page use (``science``, ``culture``, ``gold``, ``faith``,
#: ``tourism``); the two balances keep their explicit names.
YIELD_METRIC_FIELDS: Final[Mapping[str, str]] = {
    "science_per_turn": "science",
    "culture_per_turn": "culture",
    "gold_per_turn": "gold",
    "faith_per_turn": "faith",
    "tourism_per_turn": "tourism",
    "gold_balance": "gold_balance",
    "faith_balance": "faith_balance",
}

#: T272: the emitted key set, pinned as data so the produced list and the published list
#: (``store/trends.py``'s ``FR018_METRIC_NAMES``, which is 003 FR-018's own named set) cannot drift
#: apart again without a test saying so. Derived from :data:`YIELD_METRIC_FIELDS` rather than
#: retyped beside it -- a second hand-maintained copy is the drift, not the guard against it.
RECORDED_YIELD_METRICS: Final[tuple[str, ...]] = tuple(sorted(set(YIELD_METRIC_FIELDS.values())))


def yields_from_observation(observation: Observation) -> dict[str, float]:
    """The recorded yields for one observation: every :data:`YIELD_METRIC_FIELDS` entry the
    ``player.yields`` value carries as a real number, under its metric name. ``{}`` when the
    observation has no such entry or the entry answered nothing numeric -- a gap, not a zero."""
    for entry in observation.entries:
        if entry.declaration_id != YIELDS_DECLARATION_ID:
            continue
        value = entry.value
        if not isinstance(value, Mapping):
            return {}
        recorded: dict[str, float] = {}
        for field, metric in YIELD_METRIC_FIELDS.items():
            number = value.get(field)
            if isinstance(number, bool) or not isinstance(number, int | float):
                continue
            recorded[metric] = float(number)
        return recorded
    return {}


def compute_yields_from_result(result: DecisionLoopResult) -> dict[str, Any]:
    """``TurnCycleDependencies.compute_yields`` for production: the yields of the turn's last
    step's observation (the board when the turn ended). A turn with no steps, or whose steps never
    observed ``player.yields``, records ``{}`` -- the gap Constitution III says a missing
    measurement must be, and the store's series simply have no point for that turn."""
    for bundle in reversed(result.steps):
        recorded = yields_from_observation(bundle.observation)
        if recorded:
            return recorded
    return {}
