"""``GET /compare`` -- several runs on common axes (T053, FR-020 - FR-022).

``?runs={id,id,...}&metrics={name,name,...}``. Both are comma-separated; omit
``metrics`` and every metric recorded by any of the selected runs is compared.

**This route never touches a capture** (FR-035, SC-016). It does not import
``viewmodels/capture.py``, does not read ``get_capture``, and builds a
``ComparisonView`` whose type graph contains no ``CaptureView``. A comparison
question must remain answerable with every capture in every compared run
withheld, which ``tests/contract/test_web_read_api.py``'s
``comparison_never_needs_captures`` case asserts by running the identical
comparison twice over two fixtures differing only in screening status.

**The quarantine is applied in the view model, not here** (FR-021, Principle
III). This route reads every selected run -- quarantined ones included -- and
hands them all to ``build_comparison_view``, which is the single place that
decides which runs a trend line may be drawn from and whose validator refuses a
model where that decision was skipped. A route is a place someone can forget.

**Empty and unknown selections are different answers.** No ``?runs=`` at all is
a 200 with an empty state ("choose runs from the catalog") -- the comparison
view never picks a set on the user's behalf, because which runs are comparable
is a Principle IV question rather than a default. A named run that never existed
is the contract's 404.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from civsim_web.negotiate.respond import respond
from civsim_web.routes.common import (
    ErrorView,
    WebError,
    registry_of,
    registry_version,
    require_store,
)
from civsim_web.store_client.catalog import CatalogRow, list_catalog_rows
from civsim_web.viewmodels.comparison import (
    NOTHING_SELECTED,
    ComparisonView,
    build_comparison_view,
)
from civsim_web.viewmodels.metrics import build_metric_series, metric_names_of
from civsim_web.viewmodels.provenance import build_provenance
from civsim_web.viewmodels.summary import build_run_summary

__all__ = ["COMPARE_TEMPLATE", "MAX_COMPARED_RUNS", "build_compare_view", "router"]

router = APIRouter()

COMPARE_TEMPLATE = "catalog/compare.html"

#: Upper bound on one comparison. spec Scale names a five-run comparison as the
#: worked case; this is generous headroom, not a target. It exists so a hand-
#: typed query cannot turn one request into an unbounded per-turn read walk
#: across the whole catalog (plan.md C1 -- the port publishes no metric read).
MAX_COMPARED_RUNS = 20


def _split(raw: str | None) -> tuple[str, ...]:
    """Parse a comma-separated query parameter, preserving the user's order.

    Order matters: data-model.md SS11 says ``runs`` is "the compared set, **in
    the order selected**", and a chart whose series order changed between the
    user's page and the session's JSON would be a small Principle VI drift.
    """
    if not raw:
        return ()
    seen: list[str] = []
    for part in raw.split(","):
        candidate = part.strip()
        if candidate and candidate not in seen:
            seen.append(candidate)
    return tuple(seen)


@router.get("/compare")
def compare(
    request: Request,
    runs: str | None = Query(default=None),
    metrics: str | None = Query(default=None),
) -> Response:
    """Compare several runs' metric trajectories (FR-020, FR-021, FR-022)."""
    store = require_store(request)
    registry = registry_of(request)

    requested = _split(runs)
    if len(requested) > MAX_COMPARED_RUNS:
        raise WebError(
            400,
            ErrorView(
                kind="too_many_runs",
                message=(
                    f"At most {MAX_COMPARED_RUNS} runs can be compared in one request; "
                    f"{len(requested)} were named."
                ),
                detail={"limit": MAX_COMPARED_RUNS, "requested": len(requested)},
            ),
        )

    listing = list_catalog_rows(store, registry=registry, run_ids=requested or [])
    found = {row.run_id for row in listing.rows}
    for run_id in requested:
        if run_id not in found:
            raise WebError.no_such_run(run_id)

    view = build_compare_view(
        listing.rows,
        registry=registry,
        requested_run_ids=requested,
        metric_names=_split(metrics),
    )
    return respond(request, view, COMPARE_TEMPLATE)


def build_compare_view(
    rows: tuple[CatalogRow, ...],
    *,
    registry: Any,
    requested_run_ids: tuple[str, ...],
    metric_names: tuple[str, ...] = (),
) -> ComparisonView:
    """Assemble the comparison from already-read catalog rows.

    Every selected run's series is built, quarantined runs included; the
    quarantine filter lives in ``build_comparison_view`` and nowhere else.
    """
    summaries = [
        build_run_summary(
            row.run,
            registry=registry,
            configuration=row.configuration,
            turn_count=row.turn_count,
            outcome_metrics=row.outcome_metrics,
            gapped_turns=row.gapped_turns,
            assess_trend_eligibility=True,
        )
        for row in rows
    ]
    # Restore the user's selection order (data-model.md SS11).
    order = {run_id: index for index, run_id in enumerate(requested_run_ids)}
    summaries.sort(key=lambda summary: order.get(summary.run_id, len(order)))

    available: set[str] = set()
    for row in rows:
        available |= set(metric_names_of(row.yields_by_turn))
    selected_metrics = tuple(metric_names) if metric_names else tuple(sorted(available))

    series_by_run = {
        row.run_id: {
            metric_name: build_metric_series(
                row.run_id,
                metric_name,
                yields_by_turn=row.yields_by_turn,
                gapped_turns=row.gapped_turns,
            )
            for metric_name in selected_metrics
        }
        for row in rows
    }

    return build_comparison_view(
        runs=summaries,
        series_by_run=series_by_run,
        metrics=selected_metrics,
        requested_run_ids=requested_run_ids,
        provenance=build_provenance(registry),
        panel_registry=registry_version(registry),
        empty_state_reason=None if summaries else NOTHING_SELECTED,
    )
