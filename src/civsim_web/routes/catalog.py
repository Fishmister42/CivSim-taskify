"""``GET /runs`` -- the run catalog (T051, FR-018, FR-019).

The contract's route table: *"``list[RunSummaryView]``, paginated, filterable
and sortable by any ``RunSummaryView`` field via query params
(``?civilization=``, ``?model=``, ``?sort=turn_count``, ``?page=``)"*.

Three decisions worth reading before extending this route:

**Filtering and sorting are server-side** (research R6). SC-008 wants a usable
response at 50+ runs; shipping every row to the browser and filtering there
would also mean the directing session and the user were filtering with two
different implementations, which is a Principle VI drift waiting to happen. One
implementation, on the server, serving both readers -- the same reason the
content-negotiation seam exists.

**An unknown filter field is a 400, not a no-op.** A filter parameter that is
silently ignored produces an unfiltered catalog that looks filtered. Everywhere
else in this feature that rule is phrased as "never render absence as confirmed
fact"; here it is "never render *everything* as if it were a selection".

**Every row's trend eligibility is assessed.** The catalog is the entry point
to the comparison view, so it is where Principle III's question -- may these
results be trended at all? -- has to be answerable. ``store_client/catalog.py``
performs the ``turn_gaps()`` read per run and ``build_run_summary`` is called
with ``assess_trend_eligibility=True``; a run reaching this page without that
read would render as *ineligible, not assessed* rather than as fit to compare.
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
from civsim_web.store_client.catalog import CatalogListing, MetricsScope, list_catalog_rows
from civsim_web.viewmodels.catalog import (
    DEFAULT_CATALOG_PAGE_SIZE,
    MAX_CATALOG_PAGE_SIZE,
    NO_RUNS_RECORDED,
    CatalogListingView,
    UnknownCatalogField,
    apply_filters,
    leaf_paths,
    parse_filters,
    sort_rows,
)
from civsim_web.viewmodels.provenance import build_provenance
from civsim_web.viewmodels.summary import build_run_summary

__all__ = ["CATALOG_TEMPLATE", "RESERVED_QUERY_PARAMS", "build_catalog_view", "router"]

router = APIRouter()

CATALOG_TEMPLATE = "catalog/catalog.html"

#: Query parameters the catalog interprets itself; everything else is a filter.
#: ``format`` belongs to the negotiation seam and must never be read as a
#: filter on a field called "format".
RESERVED_QUERY_PARAMS: tuple[str, ...] = ("page", "page_size", "sort", "order", "format")

DEFAULT_SORT = "started_at"
DEFAULT_ORDER = "desc"


@router.get("/runs")
def list_runs(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=DEFAULT_CATALOG_PAGE_SIZE, ge=1, le=MAX_CATALOG_PAGE_SIZE),
    sort: str = Query(default=DEFAULT_SORT),
    order: str = Query(default=DEFAULT_ORDER, pattern="^(asc|desc)$"),
) -> Response:
    """Every run the port can reach, filtered, sorted and paginated."""
    store = require_store(request)
    registry = registry_of(request)
    # `MetricsScope.OUTCOME`, not `SERIES` (T075): a catalog row shows the final
    # turn's numbers and nothing else, and the port makes a full series cost one
    # read per turn. Asking for the series here meant 55 runs x 320 turns of
    # reads to render 55 numbers, which put `/runs` ten seconds past SC-008's
    # two-second budget at the scale that criterion actually names.
    listing = list_catalog_rows(store, registry=registry, metrics=MetricsScope.OUTCOME)
    view = build_catalog_view(
        listing,
        registry=registry,
        query_params=dict(request.query_params),
        page=page,
        page_size=page_size,
        sort=sort,
        order=order,
    )
    return respond(request, view, CATALOG_TEMPLATE)


def build_catalog_view(
    listing: CatalogListing,
    *,
    registry: Any,
    query_params: dict[str, str] | None = None,
    page: int = 1,
    page_size: int = DEFAULT_CATALOG_PAGE_SIZE,
    sort: str = DEFAULT_SORT,
    order: str = DEFAULT_ORDER,
) -> CatalogListingView:
    """Project the catalog listing into the page both readers receive.

    Factored out of the handler so the integration tests and any later caller
    build the identical view model without going through HTTP, the same
    discipline US1 applied to ``build_run_detail``.
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
        for row in listing.rows
    ]
    rows = [(summary, summary.model_dump(mode="json")) for summary in summaries]

    known: set[str] = set()
    for _, payload in rows:
        known |= leaf_paths(payload)

    try:
        filters = parse_filters(
            query_params or {}, reserved=RESERVED_QUERY_PARAMS, known=sorted(known)
        )
    except UnknownCatalogField as exc:
        raise _unknown_field_error(exc, kind="unknown_filter_field") from exc

    if known and sort not in known:
        raise _unknown_field_error(
            UnknownCatalogField(sort, sorted(known)), kind="unknown_sort_field"
        )

    selected = sort_rows(apply_filters(rows, filters), sort=sort, order=order)

    total = len(selected)
    page = max(1, page)
    page_size = max(1, page_size)
    start = (page - 1) * page_size
    window = selected[start : start + page_size]
    shown = tuple(summary for summary, _ in window)

    return CatalogListingView(
        runs=shown,
        page=page,
        page_size=page_size,
        total=total,
        has_more=start + len(window) < total,
        sort=sort,
        order=order,
        filters=filters,
        sortable_fields=tuple(sorted(known)),
        trend_eligible_count=sum(
            1 for summary, _ in selected if summary.trend_eligibility.eligible
        ),
        quarantined_run_ids=tuple(
            summary.run_id for summary, _ in selected if not summary.trend_eligibility.eligible
        ),
        listing_is_partial=listing.listing_is_partial,
        partial_reason=listing.partial_reason,
        empty_state_reason=None if shown else NO_RUNS_RECORDED,
        provenance=build_provenance(registry),
        panel_registry=registry_version(registry),
    )


def _unknown_field_error(exc: UnknownCatalogField, *, kind: str) -> WebError:
    """A named field nobody carries is a client error, said plainly.

    The known field list travels with the error because the caller most likely
    to hit this is the directing Claude Code session composing a query, and a
    400 that names the available columns is answerable without a second request.
    """
    return WebError(
        400,
        ErrorView(
            kind=kind,
            message=(
                f"No RunSummaryView field named {exc.field!r}. A filter or sort this "
                f"catalog cannot apply is refused rather than ignored: an ignored "
                f"filter shows an unfiltered catalog that looks filtered."
            ),
            detail={"field": exc.field, "known_fields": list(exc.known)},
        ),
    )
