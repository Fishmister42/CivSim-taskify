"""The per-decision-step capture pipeline (T102).

One capture attempt per decision step, bound to ``(run_id, turn_number, decision_step_id)`` with its
``camera_state`` and ``captured_at``. An earlier step's capture may **never** stand in for a later
one -- the board the agent is shown must reflect what it has already done this turn (FR-015). This
module never caches or reuses a prior result: :func:`capture_for_step` is a plain function callers
invoke fresh for every decision step.

**The US1 restriction, stated plainly rather than hidden.** The four image-screening gates
(research R7: source, geometry, provenance, content) do not exist yet -- they are T129, part of US2.
FR-030 and SC-019 govern *stored* captures, not merely shown ones, so until T129 lands, no frame --
successfully captured or not -- may be certified clean, and this pipeline withholds **every** step's
capture unconditionally and marks the step visually degraded (FR-050). US1 §9 permits exactly this:
a complete, honest increment without stored images would be violated by storing or showing an
unscreened one instead. :data:`_SCREENING_AVAILABLE` is the one flag this module tests to decide
that; flipping it to a real call into the T129 screening gates (once they exist) is the whole of
what T133 needs to do here -- nothing else in this module's shape should need to change.

This module never persists a frame's bytes itself: writing a :class:`ScreenCapture`'s blob through
the store is ``MatchStore.write_capture``'s job (``run/turn_cycle.py``, a later wave, not owned by
this module). Its own withholding invariant guarantees there is never a blob to write in the first
place while :data:`_SCREENING_AVAILABLE` is ``False`` -- every :class:`ScreenCapture` this module
returns has ``blob_ref=None``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from civsim_harness.host.detect import HostInfo
from civsim_harness.host.port import CaptureStatus, GameWindow, HostPlatform
from civsim_harness.models.common import (
    CaptureId,
    CapturePath,
    DecisionStepId,
    DeclarationId,
    EventId,
    RunId,
    Timestamp,
)
from civsim_harness.models.records import RunEvent, RunEventType
from civsim_harness.models.turn import ScreenCapture, ScreeningStatus, WithheldReason
from civsim_harness.observe.capture_paths import select_capture_path

#: T133's flip point (see module docstring): the T129 screening gates do not exist in this codebase
#: yet, so every capture is withheld unconditionally regardless of whether a raw frame was actually
#: obtained. Once T129 lands, the branch below that currently raises ``NotImplementedError`` is
#: where the real screening call goes.
_SCREENING_AVAILABLE = False


@dataclass(frozen=True)
class StepCapture:
    """One decision step's capture outcome: the (always withheld, in US1) :class:`ScreenCapture`
    record, plus every :class:`RunEvent` this attempt produced (``image_withheld`` always;
    additionally ``capture_failed`` when the host itself could not produce a frame at all).
    Persisting either is the caller's responsibility (``run/turn_cycle.py``), consistent with how
    every other pure builder in this wave (``observe.assemble``, ``act.dispatch``, ``act.verify``)
    returns records rather than writing them.
    """

    capture: ScreenCapture
    events: tuple[RunEvent, ...]


def capture_for_step(
    *,
    host: HostPlatform,
    host_info: HostInfo,
    window: GameWindow | None,
    view_declaration_id: DeclarationId,
    camera_state: dict[str, Any],
    run_id: RunId,
    turn_number: int,
    decision_step_id: DecisionStepId,
    captured_at: Timestamp,
    capture_id: CaptureId | None = None,
) -> StepCapture:
    """Produce exactly one capture record for this decision step.

    Never reuses a prior step's capture and holds no cache between calls -- callers must invoke
    this fresh for every decision step (FR-015). *window* may be ``None`` when the run has no
    resolved game window at all (e.g. it vanished between steps); that is treated identically to an
    unavailable host capture path rather than as an error, since from the agent's point of view
    "no window to capture" and "no working capture path" both mean the same thing: no image this
    step.
    """
    resolved_capture_id = capture_id if capture_id is not None else CaptureId(uuid.uuid4().hex)

    host_reason: str | None
    if window is None:
        capture_path = CapturePath.NONE
        host_ok = False
        host_reason = "no game window is currently resolved to capture"
    else:
        selection = select_capture_path(host=host, host_info=host_info, window=window)
        capture_path = selection.capture_path
        host_ok = selection.capture_result.status is CaptureStatus.ok
        host_reason = selection.capture_result.reason

    events: list[RunEvent] = []

    if not host_ok:
        withheld_reason = WithheldReason.CAPTURE_FAILED
        events.append(
            RunEvent(
                event_id=EventId(uuid.uuid4().hex),
                run_id=run_id,
                turn_number=turn_number,
                step_index=None,
                event_type=RunEventType.CAPTURE_FAILED,
                occurred_at=captured_at,
                detail={"reason": host_reason or "capture unavailable"},
            )
        )
    elif not _SCREENING_AVAILABLE:
        # A frame WAS obtained, but no gate exists yet to certify it clean (T129/US2): withheld on
        # provenance grounds -- this build has no path that ever tags a frame as having passed the
        # R7 provenance gate, so none can be certified regardless of how the pixels look.
        withheld_reason = WithheldReason.PROVENANCE_FAILURE
    else:  # pragma: no cover - exercised once T133 flips _SCREENING_AVAILABLE and wires real gates
        raise NotImplementedError(
            "image screening (T129) is not implemented in this module; T133 replaces this branch "
            "with a real call into the screening pipeline once it exists"
        )

    events.append(
        RunEvent(
            event_id=EventId(uuid.uuid4().hex),
            run_id=run_id,
            turn_number=turn_number,
            step_index=None,
            event_type=RunEventType.IMAGE_WITHHELD,
            occurred_at=captured_at,
            detail={
                "decision_step_id": str(decision_step_id),
                "withheld_reason": withheld_reason.value,
            },
        )
    )

    capture = ScreenCapture(
        capture_id=resolved_capture_id,
        run_id=run_id,
        turn_number=turn_number,
        decision_step_id=decision_step_id,
        captured_at=captured_at,
        camera_state=dict(camera_state),
        view_declaration_id=view_declaration_id,
        screening_status=ScreeningStatus.WITHHELD,
        withheld_reason=withheld_reason,
        shown_to_agent=False,
        # This withheld record is itself the audit evidence that the harness never showed or stored
        # an unscreened frame (research R7); it is retained, not treated as scratch state.
        retained_as_evidence=True,
        blob_ref=None,
        capture_path=capture_path,
    )
    return StepCapture(capture=capture, events=tuple(events))
