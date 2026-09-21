"""T258, Constitution III, end to end through the store: what `run/yields.py` records under
`TurnCycle.yields` is exactly what the match-tracking store's trend series (003) plot.

Lane A's store contract test proves `metric_series` over hand-written yield dicts; this one
proves the dict the harness now actually writes -- derived from a `player.yields` observation the
way `compute_yields_from_result` derives it -- comes back as the science / culture / gold / faith
series, with one point per turn and nothing fabricated for a turn that observed no value.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from civsim_harness.models.common import (
    CatalogVersionRef,
    DecisionStepId,
    LuaContext,
    ObservationId,
    RunId,
)
from civsim_harness.models.turn import Observation, ObservationEntry
from civsim_harness.run.yields import YIELDS_DECLARATION_ID, yields_from_observation
from civsim_harness.store.contract import TrendQuery
from civsim_harness.store.sqlite_adapter import SqliteMatchStore
from store_support.builders import record_run


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SqliteMatchStore]:
    adapter = SqliteMatchStore(tmp_path / "match.db")
    yield adapter
    adapter.close()


def _top_bar_observation(turn: int, *, gold: bool = True) -> Observation:
    value: dict[str, Any] = {
        "turn_number": turn,
        "science_per_turn": 5 + turn,
        "culture_per_turn": 2 + turn,
        "faith_per_turn": 1,
        "faith_balance": 3 * turn,
    }
    if gold:
        value["gold_per_turn"] = 2.5
        value["gold_balance"] = 40 + turn
    return Observation(
        observation_id=ObservationId(f"obs-{turn}"),
        decision_step_id=DecisionStepId(f"step-{turn}"),
        assembled_at=datetime(2026, 9, 21),
        catalog_version=CatalogVersionRef(version="2026.09.3", content_hash="cafe"),
        entries=[
            ObservationEntry(
                declaration_id=YIELDS_DECLARATION_ID,
                key="player.yields",
                value=value,
                context=LuaContext.IN_GAME,
            )
        ],
        captures=[],
        screen_identity="world",
    )


def test_recorded_top_bar_yields_come_back_as_the_stores_trend_series(
    store: SqliteMatchStore,
) -> None:
    record_run(
        store,
        "r-yields",
        turns=3,
        lifecycle_state="finished",
        yields_for=lambda turn: yields_from_observation(_top_bar_observation(turn)),
    )

    response = store.metric_series(
        TrendQuery(run_ids=(RunId("r-yields"),), metrics=("science", "culture", "gold", "faith"))
    )

    by_metric = {series.metric: series for series in response.series}
    assert set(by_metric) == {"science", "culture", "gold", "faith"}
    science = [(p.turn, p.value) for p in by_metric["science"].points]
    culture = [(p.turn, p.value) for p in by_metric["culture"].points]
    assert science == [(1, 6.0), (2, 7.0), (3, 8.0)]
    assert culture == [(1, 3.0), (2, 4.0), (3, 5.0)]
    assert [p.value for p in by_metric["gold"].points] == [2.5, 2.5, 2.5]
    assert [p.value for p in by_metric["faith"].points] == [1.0, 1.0, 1.0]
    assert "gold_balance" in response.metric_names and "faith_balance" in response.metric_names


def test_a_turn_that_observed_no_gold_has_no_gold_point_rather_than_a_zero(
    store: SqliteMatchStore,
) -> None:
    record_run(
        store,
        "r-gap",
        turns=3,
        lifecycle_state="finished",
        yields_for=lambda turn: yields_from_observation(
            _top_bar_observation(turn, gold=(turn != 2))
        ),
    )

    response = store.metric_series(
        TrendQuery(run_ids=(RunId("r-gap"),), metrics=("gold", "science"))
    )

    by_metric = {series.metric: series for series in response.series}
    assert [p.turn for p in by_metric["gold"].points] == [1, 3]
    assert [p.turn for p in by_metric["science"].points] == [1, 2, 3]
