"""``GET /runs/{run_id}/events`` -- the full run timeline (T028).

FR-004 requires harness-level operational events to appear *in* the run
timeline: model-provider failures, retries, provider fallbacks, crash
detection, and save/resume points. Without them, a provider chain stuck on
retries reads to the user as an unexplained pause -- spec Acceptance Scenario
US1 SS5, exactly.

The landing view carries a bounded recent window (``RECENT_EVENT_WINDOW``); this
route is the whole thing, paginated. Both read the same
``MatchStore.list_run_events`` and the same constructor, so the timeline on the
glance can never disagree with the timeline on its own page.

**Order is the store's, not ours.** ``list_run_events`` is contractually
chronological by ``occurred_at``; re-sorting here would be a second opinion
about a run's sequence, which is the kind of independently-derived judgment
invariant V5 and research R7 rule out.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from civsim_web.negotiate.respond import respond
from civsim_web.routes.common import RunContext, load_run_context
from civsim_web.viewmodels.event import (
    DEFAULT_EVENT_PAGE_SIZE,
    RunEventPage,
    build_event_page,
)

__all__ = ["build_events_page", "router"]

router = APIRouter()

EVENTS_TEMPLATE = "events/timeline.html"

MAX_EVENT_PAGE_SIZE = 500


@router.get("/runs/{run_id}/events")
def get_events(
    request: Request,
    run_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=DEFAULT_EVENT_PAGE_SIZE, ge=1, le=MAX_EVENT_PAGE_SIZE),
) -> Response:
    """A run's event timeline, paginated (FR-004)."""
    context = load_run_context(request, run_id)
    view = build_events_page(context, page=page, page_size=page_size)
    return respond(request, view, EVENTS_TEMPLATE)


def build_events_page(
    context: RunContext, *, page: int = 1, page_size: int = DEFAULT_EVENT_PAGE_SIZE
) -> RunEventPage:
    return build_event_page(
        context.events,
        registry=context.registry,
        provenance=context.provenance,
        run_id=context.run_id,
        page=page,
        page_size=page_size,
    )
