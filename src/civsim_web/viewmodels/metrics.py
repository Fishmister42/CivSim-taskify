"""``MetricSeriesView`` -- one named per-turn measurement across a run (SS10).

**Ownership note.** tasks.md assigns this file to **T041 (US3)**. It was written
early, by US4, because **T053 (`GET /compare`) declares a dependency on T041**
and User Story 3 had not run: `ComparisonView.series` is typed
`dict[metric_name, list[MetricSeriesView]]` by data-model.md SS11, so US4 cannot
be built without this type existing. It is implemented to data-model.md SS10 and
to T041's own verbatim rule, and to nothing more -- US3's `GET /runs/{id}/metrics`
route (T042), its `?series=` narrowing, and any history-specific fields remain
T042's and T041's to add. See the US4 notes in tasks.md.

The verbatim rule T041 quotes is enforced here structurally rather than in the
caller:

    ``points`` never includes a turn present in that run's ``turn_gaps()`` as if
    it were a real value

A gapped turn is *omitted* from ``points`` and *named* in ``gapped_turns``, so
the break in the line is visible rather than implied -- never interpolated,
never zero-filled (FR-025, UP-005). The model validator below refuses to
construct a series that breaks the rule, so a future caller that filters
incorrectly fails at construction instead of shipping a plausible-looking
trend line.

**Why the store's ``yields`` is the source.** ``match-store-port.md`` publishes
no metric-series read; the per-turn numbers FR-020 wants live on
``TurnCycle.yields``, which this feature reads through the Panel Registry gate
like every other store field (the ``current_turn.yields`` and
``comparison.metric_series`` panels declare it). That composition is a fourth
instance of plan.md Complexity Tracking **C1** and belongs in the same
conversation with deliverable 3.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from pydantic import model_validator

from civsim_web.registry.loader import PanelRegistry
from civsim_web.viewmodels.base import UnavailableField, ViewModel
from civsim_web.viewmodels.gate import GatedReader

__all__ = [
    "MetricPoint",
    "MetricSeriesView",
    "build_metric_series",
    "metric_names_of",
    "numeric_yields",
]


class MetricPoint(ViewModel):
    """One ``{turn, value}`` pair (data-model.md SS10)."""

    turn: int
    value: float


class MetricSeriesView(ViewModel):
    """One named per-turn measurement across one run (data-model.md SS10).

    ``metric_name`` is not a closed enum: spec Assumptions state the comparison
    view "is expected to handle any per-turn metric the store records", so the
    names are whatever keys the run's ``yields`` actually carry.
    """

    run_id: str
    metric_name: str
    points: tuple[MetricPoint, ...] = ()
    gapped_turns: tuple[int, ...] = ()
    """Turn numbers omitted from ``points`` because the store records them as
    gaps. Carried on the series so the break in the line is a stated fact for
    both readers, not something a chart implies by leaving a hole."""
    missing_turns: tuple[int, ...] = ()
    """Turns inside the series' range that recorded no value for *this* metric
    and are not gaps -- a metric the store simply did not write that turn.
    Distinct from ``gapped_turns``, which is a missing turn record entirely."""
    unavailable: tuple[UnavailableField, ...] = ()

    @model_validator(mode="after")
    def _no_gapped_turn_is_a_point(self) -> MetricSeriesView:
        """T041's rule, enforced where it cannot be forgotten."""
        gaps = set(self.gapped_turns)
        offenders = sorted(point.turn for point in self.points if point.turn in gaps)
        if offenders:
            raise ValueError(
                f"metric series {self.metric_name!r} for run {self.run_id!r} carries "
                f"turns {offenders} as real values while the store records them as "
                f"gaps. points never includes a gapped turn (data-model.md SS10)"
            )
        ordering = [point.turn for point in self.points]
        if ordering != sorted(ordering):
            raise ValueError(
                f"metric series {self.metric_name!r} for run {self.run_id!r} is not "
                f"ordered by turn (data-model.md SS10)"
            )
        return self

    @property
    def values_by_turn(self) -> dict[int, float]:
        return {point.turn: point.value for point in self.points}


def numeric_yields(turn_cycle: Any, *, registry: PanelRegistry) -> dict[str, float]:
    """The numeric entries of one turn's recorded ``yields``, gated.

    Read through ``GatedReader`` like every other store field, so the registry
    remains the only path from a store record to a response (UP-001). Booleans
    are excluded deliberately: ``True`` is an ``int`` in Python and a flag
    plotted as ``1.0`` would be a fabricated measurement.
    """
    gate = GatedReader(registry, "TurnCycle", turn_cycle)
    return {
        str(name): float(value)
        for name, value in gate.mapping("yields").items()
        if isinstance(value, int | float) and not isinstance(value, bool)
    }


def metric_names_of(yields_by_turn: Mapping[int, Mapping[str, float]]) -> tuple[str, ...]:
    """Every metric name recorded anywhere in a run, sorted for stable output."""
    names: set[str] = set()
    for values in yields_by_turn.values():
        names.update(values)
    return tuple(sorted(names))


def build_metric_series(
    run_id: str,
    metric_name: str,
    *,
    yields_by_turn: Mapping[int, Mapping[str, float]],
    gapped_turns: Sequence[int] | Iterable[int] = (),
    unavailable: Sequence[UnavailableField] = (),
) -> MetricSeriesView:
    """One metric's series for one run, with gapped turns omitted and named.

    ``yields_by_turn`` is already-read data (see ``store_client/catalog.py``);
    this constructor performs no store access of its own, which is what lets the
    same function serve the single-run route US3 will add and the multi-run
    comparison US4 needs.
    """
    gaps = tuple(sorted({int(turn) for turn in gapped_turns}))
    gap_set = set(gaps)

    points: list[MetricPoint] = []
    missing: list[int] = []
    for turn in sorted(yields_by_turn):
        if turn in gap_set:
            # Verbatim rule: a gapped turn is never carried as a real value.
            continue
        values = yields_by_turn[turn]
        if metric_name not in values:
            missing.append(int(turn))
            continue
        points.append(MetricPoint(turn=int(turn), value=float(values[metric_name])))

    return MetricSeriesView(
        run_id=run_id,
        metric_name=metric_name,
        points=tuple(points),
        gapped_turns=gaps,
        missing_turns=tuple(missing),
        unavailable=tuple(unavailable),
    )
