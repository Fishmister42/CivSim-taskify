"""``GET /runs/{run_id}/metrics`` -- one run's metric trajectories (T042).

The contract's route table: *"``list[MetricSeriesView]``,
``?series=science_output,culture_output`` to narrow"*. FR-015's third clause
("selecting a point on a metric trajectory") is answered from this route: the
replay page draws its chart from exactly this body, and each point carries the
turn it belongs to, so a click is a navigation to a URL that already exists
rather than a lookup.

**The gap rule is the whole point of this route.** ``MetricSeriesView`` omits a
gapped turn from ``points`` and names it in ``gapped_turns``, and
``MetricSeriesView.segments`` breaks the line there so no renderer can join
across it. A run whose record has gaps still gets its trajectory -- FR-016
requires marking, not hiding -- but the page and the JSON both say, in
``trend_eligibility``, that it may not be trended (Principle III).

**Where the numbers come from** is ``store_client/catalog.yields_by_turn``, the
same composition the catalog rows and the comparison view read. One composition
means a catalog column, a single-run trajectory and a multi-run overlay cannot
disagree about the same run's same turn. It is also the **fourth instance of
plan.md Complexity Tracking C1**: the published port has no metric-series read,
so a series costs one ``get_turn_cycle`` per turn (bounded by
``METRIC_TURN_LIMIT``). T060's scale test should be read as a check on this
composition specifically.

**An unrecognised ``?series=`` name is not a 400**, unlike ``GET /runs``'s
unknown filter field. There the field set is closed -- it is whatever
``RunSummaryView`` carries -- so a name outside it is provably a client error.
Here spec Assumptions make the metric set open-ended ("any per-turn metric the
store records"), so a name this run has no values for is a fact about the run.
It is reported as ``unrecorded_series`` rather than silently producing an empty
chart that would read as a run that scored zero (UP-005).
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from civsim_web.negotiate.respond import respond
from civsim_web.routes.common import RunContext, load_run_context
from civsim_web.store_client import reads
from civsim_web.store_client.catalog import yields_by_turn
from civsim_web.viewmodels.base import derive_trend_eligibility
from civsim_web.viewmodels.gate import GatedReader
from civsim_web.viewmodels.metrics import MetricSeriesPage, build_metric_series_page

__all__ = ["METRICS_TEMPLATE", "build_metrics_page", "router"]

router = APIRouter()

METRICS_TEMPLATE = "history/metrics.html"


def _split(raw: str | None) -> tuple[str, ...]:
    """Parse a comma-separated ``?series=``, preserving the caller's order.

    Order matters for the same reason it does on ``/compare``: the order the
    series are listed is the order they are drawn and legended, and a chart
    whose series order changed between the user's page and the session's JSON
    would be a small Principle VI drift.
    """
    if not raw:
        return ()
    seen: list[str] = []
    for part in raw.split(","):
        candidate = part.strip()
        if candidate and candidate not in seen:
            seen.append(candidate)
    return tuple(seen)


@router.get("/runs/{run_id}/metrics")
def get_metrics(
    request: Request,
    run_id: str,
    series: str | None = Query(default=None),
) -> Response:
    """One run's per-turn metric series, narrowed by ``?series=`` (FR-020)."""
    context = load_run_context(request, run_id, with_events=False)
    view = build_metrics_page(context, requested_series=_split(series))
    return respond(request, view, METRICS_TEMPLATE)


def build_metrics_page(
    context: RunContext, *, requested_series: tuple[str, ...] = ()
) -> MetricSeriesPage:
    """Assemble the page, with the gap read performed unconditionally.

    ``turn_gaps()`` is read for every request, never optionally: it is what
    keeps a gapped turn out of ``points``, what breaks the line where one was,
    and one of the two facts ``derive_trend_eligibility`` needs. A page
    assembled without it could only ever be *ineligible, not assessed*, which is
    the fail-closed default it is given here anyway if the read is ever dropped.
    """
    gaps = reads.turn_gaps(context.store, context.run_id)
    turn_count = reads.highest_recorded_turn(context.store, context.run_id)
    collected = yields_by_turn(
        context.store,
        context.run_id,
        registry=context.registry,
        highest_turn=turn_count,
        gapped_turns=gaps,
    )

    # Read through the registry gate like every other store field, so this
    # route cannot become a second path from the store to a response that skips
    # the Panel Registry (UP-001). `history.gap_marker` is the declaration.
    gate = GatedReader(context.registry, "Run", context.run)
    completeness = gate.text("record_completeness_status", default="") or ""

    return build_metric_series_page(
        context.run_id,
        yields_by_turn=collected,
        gapped_turns=gaps,
        requested_series=requested_series,
        turn_count=turn_count,
        trend_eligibility=derive_trend_eligibility(
            record_completeness_status=completeness,
            gapped_turns=gaps,
            assessed=True,
        ),
        provenance=context.provenance,
        panel_registry=context.registry_version,
        unavailable=gate.missing,
    )
