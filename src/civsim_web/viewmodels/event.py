"""``RunEventView`` and the paginated timeline (T024, data-model.md SS9).

FR-004 wants harness-level operational events *in the run timeline*: provider
failures, retries, fallbacks, crash detection, save and resume points. Without
them a stalled provider chain reads to the user as an unexplained pause, which
is spec Acceptance Scenario US1 §5 exactly.

``detail`` is carried verbatim. 002's own contract redacts credentials before
writing it (its FR-043), and this feature deliberately does **not** re-redact:
a second redaction pass that disagreed with 002's would leave the user and the
directing session reading different event details, which is the asymmetry
Principle VI forbids. What this feature does instead is *verify* the redaction
in its contract-test audit (data-model.md SS9, plan.md Testing; the scan itself
is T039's ``no_secrets`` case).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from civsim_web.registry.loader import PanelRegistry
from civsim_web.viewmodels.base import UnavailableField, ViewModel
from civsim_web.viewmodels.gate import GatedReader
from civsim_web.viewmodels.provenance import Provenance

__all__ = [
    "DEFAULT_EVENT_PAGE_SIZE",
    "RECENT_EVENT_WINDOW",
    "RunEventPage",
    "RunEventView",
    "build_event_page",
    "build_run_event_view",
    "build_run_event_views",
]

#: The bounded window ``RunDetailView.recent_events`` carries (data-model.md
#: SS3). The full timeline is its own paginated route, so the landing view stays
#: a glance (UP-003) without the timeline being truncated anywhere it matters.
RECENT_EVENT_WINDOW = 20

DEFAULT_EVENT_PAGE_SIZE = 50


class RunEventView(ViewModel):
    """One entry on a run's timeline (data-model.md SS9)."""

    event_id: str | None = None
    event_type: str | None = None
    turn_number: int | None = None
    step_index: int | None = None
    occurred_at: datetime | None = None
    detail: dict[str, Any] = {}
    unavailable: tuple[UnavailableField, ...] = ()


class RunEventPage(ViewModel):
    """``GET /runs/{run_id}/events`` -- the timeline, paginated.

    The contract's route table says this route returns
    ``list[RunEventView]``. A bare list has nowhere to carry the pagination the
    same line calls for, nor the provenance stamp invariant V10 requires of a
    top-level response, so the list is wrapped rather than the two obligations
    being dropped. Recorded as a small contract gap, not resolved silently.
    """

    run_id: str
    events: tuple[RunEventView, ...] = ()
    page: int = 1
    page_size: int = DEFAULT_EVENT_PAGE_SIZE
    total: int = 0
    has_more: bool = False
    provenance: Provenance


def build_run_event_view(record: Any, *, registry: PanelRegistry) -> RunEventView:
    gate = GatedReader(registry, "RunEvent", record)
    detail = gate.get("detail")
    return RunEventView(
        event_id=gate.text("event_id"),
        event_type=gate.text("event_type"),
        turn_number=gate.get("turn_number"),
        step_index=gate.get("step_index"),
        occurred_at=gate.get("occurred_at"),
        detail=dict(detail) if isinstance(detail, dict) else {},
        unavailable=gate.missing,
    )


def build_run_event_views(
    records: Any, *, registry: PanelRegistry, limit: int | None = None
) -> tuple[RunEventView, ...]:
    """Project a run's events, newest last -- the order the store returns them.

    Chronological order is contractual on 002's ``list_run_events``; this
    feature reads it rather than re-sorting, for the same reason it never
    re-derives completeness (invariant V5).
    """
    ordered = tuple(records or ())
    if limit is not None:
        ordered = ordered[-limit:]
    return tuple(build_run_event_view(record, registry=registry) for record in ordered)


def build_event_page(
    records: Any,
    *,
    registry: PanelRegistry,
    provenance: Provenance,
    run_id: str,
    page: int = 1,
    page_size: int = DEFAULT_EVENT_PAGE_SIZE,
) -> RunEventPage:
    ordered = tuple(records or ())
    total = len(ordered)
    page = max(1, page)
    page_size = max(1, page_size)
    start = (page - 1) * page_size
    selected = ordered[start : start + page_size]
    return RunEventPage(
        run_id=run_id,
        events=tuple(build_run_event_view(record, registry=registry) for record in selected),
        page=page,
        page_size=page_size,
        total=total,
        has_more=start + len(selected) < total,
        provenance=provenance,
    )
