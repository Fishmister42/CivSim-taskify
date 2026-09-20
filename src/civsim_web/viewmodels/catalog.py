"""``CatalogListingView`` -- ``GET /runs`` (supports T051, data-model.md SS1).

An unnumbered file, in the same spirit as US1's ``routes/common.py`` and
``viewmodels/provenance.py``. T051 names the route and T049 names the store
projection, but neither names a model for the *page*: the contract's route table
says ``GET /runs`` returns ``list[RunSummaryView]``, and a bare list can carry
neither the pagination the same line asks for ("paginated, filterable and
sortable by any ``RunSummaryView`` field via query params") nor invariant V10's
provenance stamp, which data-model.md SS13 explicitly requires of "catalog
listings". So the list is wrapped, exactly as ``RunEventPage`` wraps the event
timeline for the same two reasons. Recorded as a contract gap, not resolved
silently.

Filtering and sorting are implemented over the view models' **serialized**
form rather than over store records. Three things follow, all deliberate:

- *"filterable and sortable by any ``RunSummaryView`` field"* means exactly
  that, including nested ones -- ``health.state``, ``trend_eligibility.eligible``,
  ``outcome_metrics.science_output`` -- with no per-field allow-list to fall out
  of date as the model grows.
- A field the Panel Registry never permitted onto the model cannot be filtered
  on either, because it is not there to filter on (UP-001).
- An **unrecognised** filter field is an error, not a silently ignored
  parameter. A filter that quietly does nothing shows an unfiltered catalog that
  looks filtered, which is the one failure mode here capable of misleading a
  trend conclusion.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from civsim_web.viewmodels.base import (
    PanelRegistryVersion,
    RunSummaryView,
    UnavailableField,
    ViewModel,
)
from civsim_web.viewmodels.provenance import Provenance

__all__ = [
    "CatalogFilter",
    "CatalogListingView",
    "DEFAULT_CATALOG_PAGE_SIZE",
    "FILTER_OPERATORS",
    "MAX_CATALOG_PAGE_SIZE",
    "NO_RUNS_RECORDED",
    "UnknownCatalogField",
    "apply_filters",
    "leaf_paths",
    "parse_filters",
    "resolve_path",
    "sort_rows",
]

DEFAULT_CATALOG_PAGE_SIZE = 25
MAX_CATALOG_PAGE_SIZE = 200

NO_RUNS_RECORDED = (
    "No runs match. If no filters are active this store has recorded no runs "
    "the catalog can reach yet."
)

#: Comparison operators a filter may use, as a ``field__op=value`` suffix.
#: ``eq`` is the bare ``field=value`` form.
FILTER_OPERATORS: tuple[str, ...] = ("eq", "ne", "gt", "gte", "lt", "lte", "contains")


class UnknownCatalogField(LookupError):
    """A filter or sort naming a field no ``RunSummaryView`` carries."""

    def __init__(self, field: str, known: Sequence[str]) -> None:
        super().__init__(f"no RunSummaryView field named {field!r}")
        self.field = field
        self.known = tuple(sorted(known))


class CatalogFilter(ViewModel):
    """One active filter, echoed back so the page says what it is showing."""

    field: str
    operator: str
    value: str


class CatalogListingView(ViewModel):
    """``GET /runs`` -- the run catalog (FR-018, FR-019).

    ``trend_eligible_count`` and ``quarantined_run_ids`` are on the listing
    rather than left for the reader to count: Principle III makes "which of
    these may I actually trend?" the first question a catalog has to answer, and
    a number the user has to derive by scanning rows is a number the directing
    session and the user can disagree about.
    """

    runs: tuple[RunSummaryView, ...] = ()
    page: int = 1
    page_size: int = DEFAULT_CATALOG_PAGE_SIZE
    total: int = 0
    has_more: bool = False
    sort: str = "started_at"
    order: str = "desc"
    filters: tuple[CatalogFilter, ...] = ()
    sortable_fields: tuple[str, ...] = ()
    trend_eligible_count: int = 0
    quarantined_run_ids: tuple[str, ...] = ()
    listing_is_partial: bool = False
    partial_reason: str | None = None
    empty_state_reason: str | None = None
    unavailable: tuple[UnavailableField, ...] = ()
    provenance: Provenance
    panel_registry: PanelRegistryVersion


# --------------------------------------------------------------------------
# Filtering and sorting, over the serialized view models
# --------------------------------------------------------------------------


def leaf_paths(payload: Any, prefix: str = "") -> set[str]:
    """Every dotted path a filter or sort may name, from a serialized row.

    Lists are not indexable by a filter: ``unavailable.0.field`` is an artefact
    of one row's contents rather than a column of the catalog, so list paths
    stop at the list itself.
    """
    if isinstance(payload, dict):
        found: set[str] = {prefix} if prefix else set()
        for key, value in payload.items():
            found |= leaf_paths(value, f"{prefix}.{key}" if prefix else str(key))
        return found
    return {prefix} if prefix else set()


def resolve_path(payload: Any, path: str) -> Any:
    """Walk a dotted path through a serialized row. ``None`` when absent."""
    current = payload
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def parse_filters(
    params: Mapping[str, str], *, reserved: Sequence[str], known: Sequence[str]
) -> tuple[CatalogFilter, ...]:
    """Turn query parameters into filters, refusing any field nobody carries.

    ``known`` empty means there is nothing to validate against (an empty
    catalog), in which case the filters are accepted and simply match nothing --
    an error there would report a typo the user may not have made.
    """
    known_set = set(known)
    filters: list[CatalogFilter] = []
    for raw_key, value in params.items():
        if raw_key in reserved:
            continue
        field, _, suffix = raw_key.partition("__")
        operator = suffix or "eq"
        if operator not in FILTER_OPERATORS:
            raise UnknownCatalogField(raw_key, known_set)
        if known_set and field not in known_set:
            raise UnknownCatalogField(field, known_set)
        filters.append(CatalogFilter(field=field, operator=operator, value=value))
    return tuple(filters)


def _coerce(candidate: Any, raw: str) -> tuple[Any, Any]:
    """Compare a stored value against a query string in the stored value's type."""
    if isinstance(candidate, bool):
        return (candidate, raw.strip().lower() in ("1", "true", "yes", "on"))
    if isinstance(candidate, int | float):
        try:
            return (candidate, type(candidate)(raw))
        except (TypeError, ValueError):
            return (str(candidate), raw)
    return ("" if candidate is None else str(candidate), raw)


def _matches(candidate: Any, filter_: CatalogFilter) -> bool:
    if filter_.operator == "contains":
        return filter_.value.lower() in ("" if candidate is None else str(candidate)).lower()
    left, right = _coerce(candidate, filter_.value)
    try:
        if filter_.operator == "eq":
            return bool(left == right)
        if filter_.operator == "ne":
            return bool(left != right)
        if filter_.operator == "gt":
            return bool(left > right)
        if filter_.operator == "gte":
            return bool(left >= right)
        if filter_.operator == "lt":
            return bool(left < right)
        return bool(left <= right)
    except TypeError:
        return False


def apply_filters(
    rows: Sequence[tuple[RunSummaryView, dict[str, Any]]],
    filters: Sequence[CatalogFilter],
) -> list[tuple[RunSummaryView, dict[str, Any]]]:
    """Every filter must match -- filters narrow, they never widen (FR-019)."""
    selected = list(rows)
    for filter_ in filters:
        selected = [
            pair for pair in selected if _matches(resolve_path(pair[1], filter_.field), filter_)
        ]
    return selected


#: Sort key for a value the row does not carry. Missing sorts last in ascending
#: order rather than colliding with zero or the empty string, so "no end time"
#: is never ordered as if it were the earliest possible end time (UP-005).
_MISSING = (1, "")


def _sort_key(payload: dict[str, Any], path: str) -> tuple[int, Any]:
    value = resolve_path(payload, path)
    if value is None:
        return _MISSING
    if isinstance(value, bool):
        return (0, int(value))
    if isinstance(value, int | float):
        return (0, float(value))
    return (0, str(value))


def sort_rows(
    rows: Sequence[tuple[RunSummaryView, dict[str, Any]]],
    *,
    sort: str,
    order: str,
) -> list[tuple[RunSummaryView, dict[str, Any]]]:
    """Order the catalog. Ties break on ``run_id`` so paging is stable.

    Without the tiebreak, two runs with the same sort value could swap places
    between page 1 and page 2 and one of them would never be seen -- a paging
    bug that looks exactly like a missing run.
    """
    descending = order.lower() == "desc"
    mixed: list[tuple[RunSummaryView, dict[str, Any]]] = sorted(
        rows, key=lambda pair: pair[0].run_id
    )
    return sorted(mixed, key=lambda pair: _sort_key(pair[1], sort), reverse=descending)
