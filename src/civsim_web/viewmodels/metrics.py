"""``MetricSeriesView`` -- one named per-turn measurement across a run (SS10).

**Ownership note.** tasks.md assigns this file to **T041 (US3)**. It was written
early, by US4, because **T053 (`GET /compare`) declares a dependency on T041**
and User Story 3 had not run: `ComparisonView.series` is typed
`dict[metric_name, list[MetricSeriesView]]` by data-model.md SS11, so US4 cannot
be built without this type existing.

**T041 was reviewed by US3 and closed rather than reimplemented.** What US4
wrote satisfies data-model.md SS10 (``run_id`` / ``metric_name`` /
``points[{turn, value}]``, ordered by turn) and T041's verbatim rule, enforced
in the model validator rather than in the caller. US3 added, on top of it and
without touching what was there:

- ``MetricAxis`` **moved here from ``viewmodels/comparison.py``** (which now
  imports it) so the single-run trajectory (T042/T045) and the multi-run overlay
  (T053/T056) describe their axes with one type. ``static/trajectory.js`` renders
  both pages from the same ``axes[metric]`` shape, and a chart that could be
  drawn to two different axis contracts is a Principle VI drift in visual form.
- ``MetricSeriesView.segments`` / ``break_turns`` -- the *rendering* rule that
  keeps SS10's "a break in the line" from being left to whoever draws it. See
  ``segments``.
- ``MetricSeriesPage``, the ``GET /runs/{run_id}/metrics`` response (T042).

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

from civsim_web.refs.reference import ViewReference
from civsim_web.registry.loader import PanelRegistry
from civsim_web.viewmodels.base import (
    PanelRegistryVersion,
    TrendEligibility,
    UnavailableField,
    ViewModel,
)
from civsim_web.viewmodels.gate import GatedReader
from civsim_web.viewmodels.provenance import Provenance

__all__ = [
    "MetricAxis",
    "MetricPoint",
    "MetricSeriesPage",
    "MetricSeriesView",
    "NO_METRICS_RECORDED",
    "axis_for",
    "build_metric_series",
    "build_metric_series_page",
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

    @property
    def segments(self) -> tuple[tuple[MetricPoint, ...], ...]:
        """``points``, split wherever the line must **break** (SS10, FR-025).

        data-model.md SS10 says a gapped turn is "either omitted (with the gap
        visible elsewhere on the chart, e.g. a break in the line) or explicitly
        flagged, never interpolated or zero-filled". ``build_metric_series``
        does the omitting; this does the breaking. Without it, a renderer that
        joins ``points`` end to end draws a straight line *across* the gap --
        which is interpolation, arrived at by not thinking about it, and it
        silently bridges the exact hole the whole quarantine machinery exists to
        surface.

        **The rule, stated once because two renderers implement it**: a new
        segment begins whenever two consecutive points are not on consecutive
        turns. It is deliberately the fail-closed form -- it does not consult
        ``gapped_turns`` or ``missing_turns`` and so cannot be defeated by a
        hole of a *third* kind (a turn nobody recorded and nobody listed). Any
        discontinuity in the turn sequence is a hole, and a hole is a break.

        ``static/trajectory.js`` implements the identical rule (see
        ``segmentsOf`` there); the two are kept in step by the rule being this
        simple, and by ``tests/integration/test_replay.py`` asserting the
        server-rendered page never draws a single polyline across a gap.
        """
        segments: list[tuple[MetricPoint, ...]] = []
        current: list[MetricPoint] = []
        for point in self.points:
            if current and point.turn != current[-1].turn + 1:
                segments.append(tuple(current))
                current = []
            current.append(point)
        if current:
            segments.append(tuple(current))
        return tuple(segments)

    @property
    def break_turns(self) -> tuple[int, ...]:
        """The first missing turn of each break -- where a marker belongs."""
        return tuple(
            segment[-1].turn + 1 for segment in self.segments[:-1]
        )


class MetricAxis(ViewModel):
    """The common axes one or more runs' series are drawn on (SS10, SS11).

    Present on the response rather than computed per renderer so the browser's
    chart and the directing session's reading of the same numbers agree about
    the scale -- a chart drawn to different bounds than the numbers were
    described against is a Principle VI drift in visual form.

    **Lives here rather than in ``viewmodels/comparison.py``** (which imports
    it) so the single-run trajectory and the multi-run overlay share one axis
    contract; ``static/trajectory.js`` renders both from it.
    """

    metric_name: str
    min_turn: int = 0
    max_turn: int = 0
    min_value: float = 0.0
    max_value: float = 0.0
    run_ids: tuple[str, ...] = ()


class MetricSeriesPage(ViewModel):
    """``GET /runs/{run_id}/metrics`` -- one run's series (T042, FR-020).

    The contract's route table says this route returns
    ``list[MetricSeriesView]``. A bare list can carry neither the axes a chart
    has to be drawn to, the ``?series=`` narrowing it was asked for, nor the
    provenance stamp invariant V10 requires of a top-level response -- so the
    list is wrapped rather than the three obligations being dropped. This is the
    same contract gap US1 recorded for ``GET /runs/{id}/events`` and US4 for
    ``GET /runs``; three routes now.

    ``trend_eligibility`` is on the page because FR-016 requires a run with gaps
    be flagged unfit for trend comparison *wherever it is shown*, and a
    trajectory is the most trendable-looking thing this feature renders.
    """

    run_id: str
    reference: str
    series: tuple[MetricSeriesView, ...] = ()
    axes: dict[str, MetricAxis] = {}
    metric_names: tuple[str, ...] = ()
    """Every metric this run recorded anywhere -- the full set ``?series=``
    narrows from, so a caller can discover the names without a second request."""
    requested_series: tuple[str, ...] = ()
    unrecorded_series: tuple[str, ...] = ()
    """Names ``?series=`` asked for that this run recorded nothing under. Not a
    ``400``: spec Assumptions make the metric set open-ended, so "no such
    metric" is a fact about the run rather than a malformed request -- but an
    empty chart with no explanation would read as a run that scored zero, which
    is the absence-as-fact UP-005 forbids."""
    turn_count: int = 0
    gapped_turns: tuple[int, ...] = ()
    trend_eligibility: TrendEligibility
    empty_state_reason: str | None = None
    unavailable: tuple[UnavailableField, ...] = ()
    provenance: Provenance
    panel_registry: PanelRegistryVersion


NO_METRICS_RECORDED = (
    "This run has no per-turn metric values recorded yet, so there is no "
    "trajectory to draw. That is not the same as a run that scored zero -- "
    "nothing was written, rather than nothing was earned."
)


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


def axis_for(metric_name: str, series_list: Sequence[MetricSeriesView]) -> MetricAxis:
    """The bounds one metric's series are drawn to, across every listed run."""
    turns = [point.turn for series in series_list for point in series.points]
    values = [point.value for series in series_list for point in series.points]
    return MetricAxis(
        metric_name=metric_name,
        min_turn=min(turns) if turns else 0,
        max_turn=max(turns) if turns else 0,
        min_value=min(values) if values else 0.0,
        max_value=max(values) if values else 0.0,
        run_ids=tuple(series.run_id for series in series_list),
    )


def build_metric_series_page(
    run_id: str,
    *,
    yields_by_turn: Mapping[int, Mapping[str, float]],
    gapped_turns: Sequence[int] = (),
    requested_series: Sequence[str] = (),
    turn_count: int = 0,
    trend_eligibility: TrendEligibility,
    provenance: Provenance,
    panel_registry: PanelRegistryVersion,
    unavailable: Sequence[UnavailableField] = (),
) -> MetricSeriesPage:
    """One run's metric series, narrowed by ``?series=`` (T042, FR-020).

    ``yields_by_turn`` is already-read data (``store_client/catalog.py``), which
    is what lets this page and the comparison view draw from one composition
    rather than two -- a catalog column, a single-run trajectory and a multi-run
    overlay can then never disagree about the same run's same turn.
    """
    available = metric_names_of(yields_by_turn)
    requested = tuple(dict.fromkeys(requested_series))
    selected = requested or available
    unrecorded = tuple(name for name in requested if name not in available)

    series = tuple(
        build_metric_series(
            run_id,
            metric_name,
            yields_by_turn=yields_by_turn,
            gapped_turns=gapped_turns,
        )
        for metric_name in selected
    )
    return MetricSeriesPage(
        run_id=run_id,
        reference=ViewReference(run_id=run_id).path,
        series=series,
        axes={found.metric_name: axis_for(found.metric_name, [found]) for found in series},
        metric_names=available,
        requested_series=requested,
        unrecorded_series=unrecorded,
        turn_count=turn_count,
        gapped_turns=tuple(sorted({int(turn) for turn in gapped_turns})),
        trend_eligibility=trend_eligibility,
        empty_state_reason=None if available else NO_METRICS_RECORDED,
        unavailable=tuple(unavailable),
        provenance=provenance,
        panel_registry=panel_registry,
    )
