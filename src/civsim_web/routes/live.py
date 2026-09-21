"""``GET /`` and ``GET /runs/{run_id}`` -- the live glance (T026).

``GET /runs/{run_id}`` is the view User Story 1 exists for: current turn,
current visible state, the most recent decision with its reasoning, health, a
capture beside the structured panels, and what FR-027 needs to intervene
elsewhere -- all without navigating and without opening the game client
(FR-001, FR-006, UP-003, UP-008).

``GET /`` is deliberately not a second implementation of that view.
contracts/web-read-api.md makes it *"a redirect to whichever ``/runs/{run_id}``
is currently active"*, and that is what it does when exactly one run is active.
Two cases have nowhere to redirect to and render a landing page instead:

- **several runs are active** -- spec Edge Cases requires the user can always
  tell which run they are looking at, so the interface asks rather than picks;
- **none is active** -- an explanatory empty state, not an error and not a blank
  screen.

The contract also expects machine callers to hit ``/runs`` rather than depend on
"whichever run happens to be live right now". ``/runs`` is US4's route (T051);
until it exists, the no-active-runs case renders the landing page for both
readers rather than redirecting somewhere that would 404.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from civsim_web.negotiate.respond import respond
from civsim_web.routes.common import (
    RunContext,
    load_run_context,
    registry_of,
    registry_version,
    require_store,
)
from civsim_web.store_client import reads
from civsim_web.viewmodels.event import RECENT_EVENT_WINDOW, build_run_event_views
from civsim_web.viewmodels.run_detail import (
    NO_RUNS_RECORDED,
    LandingView,
    RunDetailView,
    build_intervention_info,
    build_run_detail_view,
    utcnow,
)
from civsim_web.viewmodels.summary import build_run_summary
from civsim_web.viewmodels.turn import TurnCycleView, TurnIsGap, build_turn_cycle_view

__all__ = ["build_run_detail", "router"]

router = APIRouter()

LANDING_TEMPLATE = "live/landing.html"
RUN_DETAIL_TEMPLATE = "live/run_detail.html"


@router.get("/")
def landing(request: Request) -> Response:
    """Whichever run is live, or an honest account of why that is ambiguous."""
    store = require_store(request)
    registry = registry_of(request)
    active = list(store.list_active_runs() or ())

    if len(active) == 1:
        target = f"/runs/{getattr(active[0], 'run_id', '')}"
        query = request.url.query
        # The query string carries `?format=json` through the redirect, so a
        # machine caller's explicit negotiation survives the hop.
        return RedirectResponse(f"{target}?{query}" if query else target, status_code=307)

    summaries = tuple(
        build_run_summary(
            run,
            registry=registry,
            events=reads.list_run_events(store, str(getattr(run, "run_id", ""))),
            configuration=reads.run_configuration(store, run),
            turn_count=reads.highest_recorded_turn(store, str(getattr(run, "run_id", ""))),
        )
        for run in active
    )
    view = LandingView(
        active_runs=summaries,
        empty_state_reason=None if summaries else NO_RUNS_RECORDED,
        last_confirmed_current_at=utcnow(),
        panel_registry=registry_version(registry),
    )
    return respond(request, view, LANDING_TEMPLATE)


@router.get("/runs/{run_id}")
def run_detail(request: Request, run_id: str) -> Response:
    """One run, at a glance (FR-001 - FR-006, FR-026, FR-027, FR-031)."""
    context = load_run_context(request, run_id)
    return respond(request, build_run_detail(context), RUN_DETAIL_TEMPLATE)


def build_run_detail(context: RunContext) -> RunDetailView:
    """Assemble the glance from one run's reads.

    Factored out of the handler so US2's reference resolution and the
    integration tests can build the identical view model without going through
    HTTP -- invariant V8 ("the identical reference, resolved by any caller,
    produces an identical view model before serialization") is only credible if
    there is one constructor to point at.
    """
    store = context.store
    registry = context.registry

    current_turn = _current_turn(context)
    turn_count = current_turn.turn_number if current_turn is not None else 0

    summary = build_run_summary(
        context.run,
        registry=registry,
        events=context.events,
        configuration=reads.run_configuration(store, context.run),
        turn_count=turn_count,
        outcome_metrics=_outcome_metrics(current_turn),
    )

    return build_run_detail_view(
        summary=summary,
        current_turn=current_turn,
        intervention_info=build_intervention_info(
            context.run,
            registry=registry,
            last_known_good=reads.last_known_good(store, context.run_id),
        ),
        panel_registry=context.registry_version,
        recent_events=build_run_event_views(
            context.events, registry=registry, limit=RECENT_EVENT_WINDOW
        ),
    )


def _current_turn(context: RunContext) -> TurnCycleView | None:
    """The latest authoritative turn, or ``None`` for a run with none yet.

    ``None`` is a real answer here, not a failure: a run opened before its first
    turn is recorded renders an explicit empty state rather than a blank panel
    or a zero-filled turn (data-model.md SS3 Validation).
    """
    record = reads.latest_authoritative_turn(context.store, context.run_id)
    if record is None:
        return None
    turn_number = getattr(getattr(record, "turn_cycle", record), "turn_number", 0)
    try:
        return build_turn_cycle_view(
            record,
            registry=context.registry,
            provenance=context.provenance,
            turn_gaps=reads.turn_gaps(context.store, context.run_id),
            step_gaps=tuple(context.store.step_gaps(context.run_id, turn_number) or ()),
            # The whole turn, deliberately (T067). The turn route reads captures
            # for its step window only, because it *has* a window; the glance
            # renders every step of the current turn on purpose, since
            # `latest_decision_of` reads the **last** step and a bounded window
            # here would make FR-001's "most recent agent decision" show step 50
            # of 200. Every capture read here is a capture being viewed, which
            # is what FR-036 actually asks.
            captures=reads.capture_records_for_turn(
                context.store, getattr(record, "steps", ()) or ()
            ),
        )
    except TurnIsGap:
        # `latest_authoritative_turn` already skips gaps; reaching here means the
        # store's own gap list and its turn records disagree. Showing the empty
        # state is the honest answer -- inventing a turn would not be.
        return None


def _outcome_metrics(turn: TurnCycleView | None) -> dict[str, float]:
    """Headline metrics at the run's last recorded turn (data-model.md SS1).

    Sourced from the turn's own recorded yields rather than computed here, and
    only the numeric ones: a yield the store records as text is shown in the
    turn panel verbatim, but it is not a metric a catalog column can compare.
    """
    if turn is None:
        return {}
    return {
        name: float(value)
        for name, value in turn.yields.items()
        if isinstance(value, int | float) and not isinstance(value, bool)
    }
