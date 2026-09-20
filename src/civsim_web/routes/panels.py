"""Panel references -- the finest grain of FR-008 (T035, T038).

```
GET /runs/{run_id}/panels/{panel_id}                                (T038)
GET /runs/{run_id}/turns/{turn}/panels/{panel_id}                   (T035)
GET /runs/{run_id}/turns/{turn}/steps/{step}/panels/{panel_id}      (see below)
```

These are the canonical ``ViewReference`` resolution targets. A user copies one
out of the address bar, the directing Claude Code session ``GET``s the identical
string with ``Accept: application/json``, and both receive the same content --
which is the whole of Principle VI stated as a URL (FR-007, FR-008, UP-006,
SC-012).

**No route here reads the store for panel content.** Each builds the *enclosing*
view through the very constructor its own page uses -- ``build_run_detail``,
``build_turn_view``, ``build_step_view`` -- and hands the serialized result to
``viewmodels/panel.py`` to cut the named panel out of it. So a panel endpoint
cannot show a value its page does not, or omit one its page has: the two are the
same bytes, sliced differently. Building the panel from its own store read would
have been simpler and would have re-introduced exactly the second data path
Principle VI exists to forbid.

**The third route is not in the contract's table, deliberately.** The Foundation
notes recorded it as finding 4: ``data-model.md`` SS12 describes
``/steps/{step_index}`` being inserted into the panel path for a step-scoped
panel, ``refs/reference.py`` parses that shape, and the route table in
``contracts/web-read-api.md`` lists only the turn- and run-scoped shapes. Twenty
of the registry's shipped panels are ``scope: step``. Leaving the shape
unrouted would mean a registered panel with no resolvable URL, which is UP-006
failing quietly, so the route exists and the contract gap is recorded in
tasks.md rather than papered over.

**Scope is not a filter on what resolves.** A turn-scoped reference to a
step-scoped panel answers with that panel across every step of the turn; a
run-scoped reference to a turn-scoped panel answers from the run's current turn,
because ``RunDetailView`` genuinely carries it and data-model.md SS12 says a
reference with no turn "resolves to the whole turn or run". A panel that truly
has no place on the requested view says so in ``absent_reason`` and still
returns its declaration -- an explanation, not a 404. The one 404 is an
unregistered ``panel_id``, which contracts/web-read-api.md calls a client-side
reference error rather than a fact about the run.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from civsim_web.negotiate.respond import respond
from civsim_web.refs.reference import ViewReference
from civsim_web.registry.loader import PanelDeclaration
from civsim_web.routes.common import ErrorView, RunContext, WebError, load_run_context
from civsim_web.routes.live import build_run_detail
from civsim_web.routes.turns import build_step_view, build_turn_view
from civsim_web.viewmodels.panel import (
    RUN_ENTITY_PATHS,
    STEP_ENTITY_PATHS,
    TURN_ENTITY_PATHS,
    PanelView,
    build_panel_view,
)

__all__ = ["build_run_panel_view", "build_step_panel_view", "build_turn_panel_view", "router"]

router = APIRouter()

PANEL_TEMPLATE = "panels/panel.html"


@router.get("/runs/{run_id}/panels/{panel_id}")
def get_run_panel(request: Request, run_id: str, panel_id: str) -> Response:
    """A run-scoped panel: the header, its configuration, the timeline, FR-027."""
    context = load_run_context(request, run_id)
    return respond(request, build_run_panel_view(context, panel_id), PANEL_TEMPLATE)


@router.get("/runs/{run_id}/turns/{turn_number}/panels/{panel_id}")
def get_turn_panel(
    request: Request,
    run_id: str,
    turn_number: int,
    panel_id: str,
    attempt: int | None = Query(default=None, ge=0),
) -> Response:
    """A panel scoped to one turn -- the canonical turn-panel reference (T035)."""
    context = load_run_context(request, run_id, with_events=False)
    view = build_turn_panel_view(context, turn_number, panel_id, attempt=attempt)
    return respond(request, view, PANEL_TEMPLATE)


@router.get("/runs/{run_id}/turns/{turn_number}/steps/{step_index}/panels/{panel_id}")
def get_step_panel(
    request: Request,
    run_id: str,
    turn_number: int,
    step_index: int,
    panel_id: str,
    attempt: int | None = Query(default=None, ge=0),
) -> Response:
    """A panel scoped to one decision step (data-model.md SS12's sixth shape)."""
    context = load_run_context(request, run_id, with_events=False)
    view = build_step_panel_view(context, turn_number, step_index, panel_id, attempt=attempt)
    return respond(request, view, PANEL_TEMPLATE)


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------


def build_run_panel_view(context: RunContext, panel_id: str) -> PanelView:
    """Cut ``panel_id`` out of this run's own glance view."""
    panel = _registered(context, panel_id)
    detail = build_run_detail(context)
    return build_panel_view(
        panel,
        data=detail.model_dump(mode="json"),
        entity_paths=RUN_ENTITY_PATHS,
        reference=ViewReference(run_id=context.run_id, panel_id=panel_id).path,
        context_reference=ViewReference(run_id=context.run_id).path,
        run_id=context.run_id,
        provenance=context.provenance,
    )


def build_turn_panel_view(
    context: RunContext, turn_number: int, panel_id: str, *, attempt: int | None = None
) -> PanelView:
    """Cut ``panel_id`` out of one turn's own view."""
    panel = _registered(context, panel_id)
    turn = build_turn_view(context, turn_number, attempt=attempt)
    return build_panel_view(
        panel,
        data=turn.model_dump(mode="json"),
        entity_paths=TURN_ENTITY_PATHS,
        reference=ViewReference(
            run_id=context.run_id, turn_number=turn_number, panel_id=panel_id
        ).path,
        context_reference=ViewReference(
            run_id=context.run_id, turn_number=turn_number
        ).path,
        run_id=context.run_id,
        turn_number=turn_number,
        provenance=context.provenance,
    )


def build_step_panel_view(
    context: RunContext,
    turn_number: int,
    step_index: int,
    panel_id: str,
    *,
    attempt: int | None = None,
) -> PanelView:
    """Cut ``panel_id`` out of one decision step's own view."""
    panel = _registered(context, panel_id)
    step = build_step_view(context, turn_number, step_index, attempt=attempt)
    return build_panel_view(
        panel,
        data=step.model_dump(mode="json"),
        entity_paths=STEP_ENTITY_PATHS,
        reference=ViewReference(
            run_id=context.run_id,
            turn_number=turn_number,
            step_index=step_index,
            panel_id=panel_id,
        ).path,
        context_reference=ViewReference(
            run_id=context.run_id, turn_number=turn_number, step_index=step_index
        ).path,
        run_id=context.run_id,
        turn_number=turn_number,
        step_index=step_index,
        provenance=context.provenance,
    )


def _registered(context: RunContext, panel_id: str) -> PanelDeclaration:
    """The declaration for ``panel_id``, or the 404 the contract asks for.

    The registry is a closed list (UP-001). An id that is not in it is not a
    valid reference at all, which is a different thing from a registered panel
    with nothing to show -- and contracts/web-read-api.md's error table keeps
    the two apart precisely so a client can tell "you asked for something that
    does not exist" from "this turn has no value for that".
    """
    panel = context.registry.get(panel_id)
    if panel is not None:
        return panel
    detail: dict[str, Any] = {
        "panel_id": panel_id,
        "panel_registry_version": context.registry.version,
    }
    raise WebError(
        404,
        ErrorView(
            kind="panel_not_found",
            message=(
                f"No panel {panel_id!r} is declared in Panel Registry version "
                f"{context.registry.version}. An unregistered panel is not a valid "
                f"reference -- the registry is a closed list, so this is a "
                f"reference error, not a field that happens to be unavailable."
            ),
            run_id=context.run_id,
            detail=detail,
        ),
    )
