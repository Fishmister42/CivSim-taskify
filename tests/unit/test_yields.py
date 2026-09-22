"""T258, Constitution III: the turn's yields come from the human's top bar onto the turn record.

Before `run/yields.py`, `TurnCycle.yields` was `{}` on every turn of every run -- the production
composition never replaced `turn_cycle.py`'s `_no_yields` placeholder -- so the store's science /
culture / gold / faith series were empty by construction. These tests pin the derivation: which
observation is read, which fields become which metrics, and that a missing measurement is a gap
(absent), never a fabricated zero.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from civsim_harness.models.common import (
    CatalogVersionRef,
    DecisionStepId,
    LuaContext,
    ObservationId,
)
from civsim_harness.models.turn import Observation, ObservationEntry, TurnOutcome
from civsim_harness.run.decision_loop import DecisionLoopResult
from civsim_harness.run.yields import (
    RECORDED_YIELD_METRICS,
    YIELD_METRIC_FIELDS,
    YIELDS_DECLARATION_ID,
    compute_yields_from_result,
    yields_from_observation,
)
from civsim_harness.store.trends import DERIVED_METRICS, FR018_METRIC_NAMES
from store_support.builders import make_step_bundle

_TOP_BAR: dict[str, Any] = {
    "turn_number": 17,
    "science_per_turn": 6.5,
    "culture_per_turn": 3,
    "faith_per_turn": 0,
    "faith_balance": 12,
    "gold_per_turn": 2.4,
    "gold_balance": 41,
    "tourism_per_turn": 0,
}


def _observation(entries: list[ObservationEntry], step: str = "step-1") -> Observation:
    return Observation(
        observation_id=ObservationId(f"obs-{step}"),
        decision_step_id=DecisionStepId(step),
        assembled_at=datetime(2026, 9, 21),
        catalog_version=CatalogVersionRef(version="2026.09.3", content_hash="cafe"),
        entries=entries,
        captures=[],
        screen_identity="world",
    )


def _yields_entry(value: Any) -> ObservationEntry:
    return ObservationEntry(
        declaration_id=YIELDS_DECLARATION_ID,
        key="player.yields",
        value=value,
        context=LuaContext.IN_GAME,
    )


def test_the_top_bar_becomes_the_recorded_yields_under_the_stores_metric_names() -> None:
    recorded = yields_from_observation(_observation([_yields_entry(_TOP_BAR)]))
    assert recorded == {
        "science": 6.5,
        "culture": 3.0,
        "faith": 0.0,
        "faith_balance": 12.0,
        "gold": 2.4,
        "gold_balance": 41.0,
        "tourism": 0.0,
    }
    assert all(isinstance(v, float) for v in recorded.values())
    # `turn_number` is bookkeeping on the observation, not a yield: never a metric.
    assert "turn_number" not in recorded
    assert set(YIELD_METRIC_FIELDS.values()) == set(recorded)


def test_the_emitted_key_set_is_pinned_and_matches_the_published_fr_018_set() -> None:
    """T272: the produced list and the published list, pinned as data on both sides.

    ``store/trends.py``'s ``turn_metrics_from`` is a generic pass-through -- it plots whatever
    numbers a turn record happens to carry and holds no list of its own -- so nothing anywhere
    compared what 003 FR-018 *names* against what this harness actually *emits*. They disagreed:
    FR-018 named eight metrics and four of them had no entry in ``YIELD_METRIC_FIELDS``, which
    means those series could never be non-empty no matter what the consumer did. Two of the four
    (``city_count``, ``unit_count``) turned out to be produced elsewhere and fine; two
    (``production``, ``food``) had no empire-level source at all and FR-018 was amended for them.

    This is the guard that makes the next divergence a red test instead of an empty chart: one
    literal list of the requirement's names, one list derived from the producer's own mapping, and
    an equality between them. Adding a yield field without amending FR-018 fails here, and so does
    amending FR-018 without a producer.
    """
    assert RECORDED_YIELD_METRICS == (
        "culture",
        "faith",
        "faith_balance",
        "gold",
        "gold_balance",
        "science",
        "tourism",
    )
    assert set(FR018_METRIC_NAMES) == set(RECORDED_YIELD_METRICS) | set(DERIVED_METRICS)
    # And the published set really is a set of distinct, sorted names -- not a list that grew a
    # duplicate or an ordering nobody can diff.
    assert FR018_METRIC_NAMES == tuple(sorted(set(FR018_METRIC_NAMES)))


def test_a_value_the_client_did_not_answer_is_absent_never_zero() -> None:
    """Constitution III: a gap must look like a gap. `lua/ingame/yields.lua` omits any read that
    errored; the record then has no point for that metric."""
    partial = {"science_per_turn": 4, "gold_per_turn": None, "faith_balance": "n/a"}
    recorded = yields_from_observation(_observation([_yields_entry(partial)]))
    assert recorded == {"science": 4.0}


def test_booleans_and_non_mappings_are_never_read_as_measurements() -> None:
    assert yields_from_observation(_observation([_yields_entry({"science_per_turn": True})])) == {}
    assert yields_from_observation(_observation([_yields_entry("garbage")])) == {}
    only_reason = _observation([_yields_entry({"reason": "no local player"})])
    assert yields_from_observation(only_reason) == {}


def test_an_observation_without_the_declaration_records_nothing() -> None:
    other = ObservationEntry(
        declaration_id="units.state",
        key="units.state",
        value={"units": []},
        context=LuaContext.IN_GAME,
    )
    assert yields_from_observation(_observation([other])) == {}
    assert yields_from_observation(_observation([])) == {}


def _result(*bundles: Any) -> DecisionLoopResult:
    return DecisionLoopResult(
        outcome=TurnOutcome.ENDED_BY_AGENT,
        steps=tuple(bundles),
        final_no_progress_streak=0,
        events=(),
    )


def _bundle(step_index: int, top_bar: dict[str, Any] | None) -> Any:
    entries = (
        []
        if top_bar is None
        else [
            {
                "declaration_id": str(YIELDS_DECLARATION_ID),
                "key": "player.yields",
                "value": top_bar,
                "context": str(LuaContext.IN_GAME),
            }
        ]
    )
    return make_step_bundle("run-y", "tc-y", step_index, entries=entries)


def test_the_turns_last_observation_is_what_gets_recorded() -> None:
    """The board as it stood when the turn ended -- the same observation the store derives city
    and unit counts from -- not the first step's, which predates every decision of the turn."""
    first = _bundle(1, {**_TOP_BAR, "science_per_turn": 1})
    last = _bundle(2, {**_TOP_BAR, "science_per_turn": 9})
    assert compute_yields_from_result(_result(first, last))["science"] == 9.0


def test_a_turn_whose_last_step_did_not_observe_yields_falls_back_to_the_last_that_did() -> None:
    observed = _bundle(1, _TOP_BAR)
    unobserved = _bundle(2, None)
    assert compute_yields_from_result(_result(observed, unobserved))["gold"] == 2.4


def test_a_turn_with_no_steps_or_no_yield_observation_records_the_gap() -> None:
    assert compute_yields_from_result(_result()) == {}
    assert compute_yields_from_result(_result(_bundle(1, None), _bundle(2, None))) == {}
