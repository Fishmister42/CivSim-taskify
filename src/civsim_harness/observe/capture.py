"""The per-decision-step capture pipeline (T102; T129/T133 screening; T157 degradation).

One capture attempt per decision step, bound to ``(run_id, turn_number, decision_step_id)`` with its
``camera_state`` and ``captured_at``. An earlier step's capture may **never** stand in for a later
one -- the board the agent is shown must reflect what it has already done this turn (FR-015). This
module never caches or reuses a prior result: :func:`capture_for_step` is a plain function callers
invoke fresh for every decision step.

**T133 -- the flip point, now flipped.** US1 (T102) shipped with every capture withheld
unconditionally, because the four image-screening gates (research R7: source, geometry, provenance,
content) did not exist yet -- FR-030 and SC-019 govern *stored* captures, not merely shown ones, so
until those gates existed no frame could be certified clean. They exist now
(:mod:`civsim_harness.parity.screening`, T129), so this module calls them for real: a screened-clean
frame may be stored and *offered for attachment*; a frame that fails any gate is written as a
``withheld`` record with its ``withheld_reason`` and ``blob_ref=None``, plus an ``image_withheld``
event carrying this step's ``step_index`` -- **that withheld record is the evidence screening
worked and is never deleted**.

**T238 -- clean never means shown.** Every :class:`~civsim_harness.models.turn.ScreenCapture`
built here carries ``shown_to_agent=False``, including a screened-clean one. ``shown_to_agent``
is a statement about a decision request actually dispatched to the provider, and no such request
exists at capture time -- this module used to set it from ``outcome.is_clean``, which recorded an
*intention*, and the record asserted the agent was shown an image the provider never received.
The one caller (``run/decision_loop.py``) flips the flag on a validated copy at the moment the
image is genuinely attached, through ``agent/context.py``'s ``select_screened_images`` chokepoint
(T134), and never anywhere else.

**T249 -- preconditions are checked before any frame exists.** Every attempt begins by asking the
host port's :meth:`~civsim_harness.host.port.HostPlatform.check_capture_preconditions` -- the
cheap per-capture hygiene preflight -- and a non-passing result means the frame is **never
taken**: ``capture_window`` is not called, and the step takes the same withheld-with-reason path
as any other host capture failure (no parallel bookkeeping). Passing preconditions grant nothing:
attachment stays gated on the run's VALIDATED tier (T099/T238, in ``run/decision_loop.py``).

**T157/T240 -- bounded retries, then degrade.** Research R7: "any gate failing means withhold and
re-capture" -- so a single bad frame (a transient overlay glitch, a capture race) is not immediately
fatal to the step. :func:`capture_for_step` retries the whole capture-then-screen attempt up to
*max_attempts* times, stopping at the first clean result. Only once every attempt in the bound is
exhausted without a clean result does this step count as degraded: the returned
:class:`StepCapture.visually_degraded` is set, and a ``capture_failed`` event carrying this
step's ``step_index`` is recorded -- the caller (``run/decision_loop.py``, the part of the turn
cycle that produces captures) rolls that per-step flag up to ``TurnCycle.visually_degraded`` (via
``run/turn_cycle.py``'s persist) and downgrades ``Run.comparability_status`` through the store's
``update_run`` (FR-050, SC-013); neither of those record types is constructed here.

**Persistence discipline (FR-051, D5).** This module never persists a frame's bytes itself and
never writes to any store -- writing a :class:`ScreenCapture` and its blob through
``MatchStore.write_capture(capture, blob)`` is the caller's job (``run/decision_loop.py``). A clean
result's raw bytes come back on :class:`StepCapture.blob`, verbatim, for the caller to pass to
that one write path -- there is no second, bypassing way for a frame's bytes to reach durable
storage from here.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from civsim_harness.capability.registry import CapabilityRegistry
from civsim_harness.host.detect import HostInfo
from civsim_harness.host.port import CaptureStatus, GameProcess, GameWindow, HostPlatform
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
from civsim_harness.parity.screening import (
    CaptureAttempt,
    ContentDetector,
    ScreeningOutcome,
    ScreeningProfiles,
    build_screen_capture,
    screen_capture,
    transcode_raw_frame_to_png,
)

#: T157's retry bound: after this many attempts (each a fresh host capture plus a fresh run
#: through the four screening gates) still produce nothing screened-clean, this decision step is
#: recorded visually degraded rather than retried indefinitely (research R7, FR-050).
DEFAULT_MAX_CAPTURE_ATTEMPTS: Final[int] = 3

#: Wire-ready media types by ``CaptureFrame.image_format`` (upper-cased). Only encoded formats a
#: provider call can actually carry appear here. A raw framebuffer format (``"BGRA8"`` and kin)
#: has no wire form of its own, which is why :func:`_attempt_once` re-encodes it to PNG before
#: screening (T252); a frame that still reaches this table in a format absent from it is stored
#: for the record with :attr:`StepCapture.blob_media_type` ``None``, and the run loop can never
#: attach it to a decision request -- recorded not shown, which is the truth (T238).
_WIRE_MEDIA_TYPES: Final[Mapping[str, str]] = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "JPG": "image/jpeg",
    "WEBP": "image/webp",
    "GIF": "image/gif",
    "BMP": "image/bmp",
}

#: A synthetic, host-level "no frame at all" outcome -- used both when *window* is ``None`` and
#: when :meth:`~civsim_harness.host.port.HostPlatform.capture_window` itself reports anything other
#: than :attr:`~civsim_harness.host.port.CaptureStatus.ok`. Never passed to
#: :func:`~civsim_harness.parity.screening.build_screen_capture` (that function requires a real
#: :class:`~civsim_harness.parity.screening.CaptureAttempt`, which does not exist in this case) --
#: see :func:`_withheld_capture_without_attempt`.
def _host_failure_outcome(reason: str) -> ScreeningOutcome:
    return ScreeningOutcome(
        status=ScreeningStatus.WITHHELD,
        withheld_reason=WithheldReason.CAPTURE_FAILED,
        failed_gate=None,
        detail=reason,
    )


@dataclass(frozen=True)
class StepCapture:
    """One decision step's capture outcome, ready for the caller (``run/decision_loop.py``) to
    persist.

    ``capture`` is always present -- clean or withheld, exactly one record per step (FR-015), and
    always with ``shown_to_agent=False``: whether the frame actually reaches a decision request is
    settled later, by the run loop, at the moment of attachment (T238; see module docstring).
    ``events`` carries ``image_withheld`` whenever ``capture`` is withheld, plus ``capture_failed``
    whenever ``visually_degraded`` is true (T157) -- both persisted the same way every other record
    in this wave's shape is: returned here, written by the caller, never written by this module.
    ``blob`` is the clean frame's raw bytes, present only when ``capture.screening_status ==
    screened_clean``; pass it verbatim to ``MatchStore.write_capture(capture, blob)`` (FR-051, D5).
    ``blob_media_type`` is the blob's wire media type when the frame format has one
    (:data:`_WIRE_MEDIA_TYPES`) -- the only form in which the run loop may build a
    ``provider.port.Image`` from this capture. ``visually_degraded`` is this step's own
    contribution to ``DecisionStep.visually_degraded`` / ``TurnCycle.visually_degraded`` (FR-050)
    -- this module does not construct either of those records itself.
    """

    capture: ScreenCapture
    events: tuple[RunEvent, ...]
    blob: bytes | None = None
    blob_media_type: str | None = None
    visually_degraded: bool = False


def _attempt_once(
    *,
    host: HostPlatform,
    host_info: HostInfo,
    window: GameWindow,
    view_declaration_id: DeclarationId,
    camera_state: Mapping[str, Any],
    platform: str,
    registry: CapabilityRegistry,
    profiles: ScreeningProfiles,
    detector: ContentDetector | None,
    expected_process: GameProcess | None,
    detected_text_tokens: frozenset[str] | None,
) -> tuple[CapturePath, CaptureAttempt | None, ScreeningOutcome]:
    """One host-capture-then-screen attempt. Never raises: a precondition failure, a host failure
    and a screening failure all come back as a withheld
    :class:`~civsim_harness.parity.screening.ScreeningOutcome`, the same shape
    :func:`capture_for_step`'s retry loop already knows how to interpret.
    """
    # T249: the port's capture-precondition preflight runs BEFORE any frame is taken. A failing
    # precondition means no frame may be taken at all -- not taken-and-discarded -- so the host is
    # never asked to capture, and the step takes the exact host-failure degradation path below
    # (withheld record, ``capture_failed``/``image_withheld`` events carrying the reason). This
    # check can only take a capture away, never grant one: T099's tier rule still gates actual
    # attachment in ``run/decision_loop.py``, untouched.
    preflight = host.check_capture_preconditions()
    if not preflight.passed:
        return (
            CapturePath.NONE,
            None,
            _host_failure_outcome(f"capture preconditions failed: {preflight.reason}"),
        )

    selection = select_capture_path(host=host, host_info=host_info, window=window)
    if selection.capture_result.status is not CaptureStatus.ok:
        reason = selection.capture_result.reason or "capture unavailable"
        return selection.capture_path, None, _host_failure_outcome(reason)

    assert selection.capture_result.frame is not None  # guaranteed by CaptureResult for status=ok
    # T252: a raw framebuffer frame is re-encoded losslessly to PNG *before* screening, so the
    # screened bytes, the stored blob and what the provider is handed are one and the same. A raw
    # frame that cannot even be decoded is a host failure for this attempt (retried like any
    # other), never screened blind.
    wire_frame = transcode_raw_frame_to_png(selection.capture_result.frame)
    if wire_frame is None:
        raw = selection.capture_result.frame
        return (
            selection.capture_path,
            None,
            _host_failure_outcome(
                f"the host's {raw.image_format} frame ({raw.width}x{raw.height}, "
                f"{len(raw.image_bytes)} bytes) could not be decoded for PNG encoding"
            ),
        )
    attempt = CaptureAttempt(
        frame=wire_frame,
        capture_path=selection.capture_path,
        window=window,
        view_declaration_id=view_declaration_id,
        camera_state=camera_state,
        platform=platform,
        expected_process=expected_process,
        detected_text_tokens=detected_text_tokens,
    )
    outcome = screen_capture(attempt, registry=registry, profiles=profiles, detector=detector)
    return selection.capture_path, attempt, outcome


def _withheld_capture_without_attempt(
    *,
    capture_id: CaptureId,
    run_id: RunId,
    turn_number: int,
    decision_step_id: DecisionStepId,
    captured_at: Timestamp,
    camera_state: Mapping[str, Any],
    view_declaration_id: DeclarationId,
    capture_path: CapturePath,
    withheld_reason: WithheldReason,
) -> ScreenCapture:
    """Build the withheld record for a step that never produced a frame at all (no window, or
    every host-capture attempt failed) -- mirrors
    :func:`~civsim_harness.parity.screening.build_screen_capture`'s own forced ``blob_ref=None`` /
    ``shown_to_agent=False`` shape, but without a :class:`~civsim_harness.parity.screening.
    CaptureAttempt` to build from (that function requires one; there is none here).
    """
    return ScreenCapture(
        capture_id=capture_id,
        run_id=run_id,
        turn_number=turn_number,
        decision_step_id=decision_step_id,
        captured_at=captured_at,
        camera_state=dict(camera_state),
        view_declaration_id=view_declaration_id,
        screening_status=ScreeningStatus.WITHHELD,
        withheld_reason=withheld_reason,
        shown_to_agent=False,
        # This withheld record is itself the audit evidence that the harness never showed or
        # stored an unscreened frame (research R7); it is retained, not treated as scratch state.
        retained_as_evidence=True,
        blob_ref=None,
        capture_path=capture_path,
    )


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
    step_index: int,
    captured_at: Timestamp,
    registry: CapabilityRegistry,
    profiles: ScreeningProfiles,
    capture_id: CaptureId | None = None,
    detector: ContentDetector | None = None,
    expected_process: GameProcess | None = None,
    detected_text_tokens: frozenset[str] | None = None,
    max_attempts: int = DEFAULT_MAX_CAPTURE_ATTEMPTS,
) -> StepCapture:
    """Produce exactly one capture record for this decision step (T129/T133/T157).

    Never reuses a prior step's capture and holds no cache between calls -- callers must invoke
    this fresh for every decision step (FR-015). *window* may be ``None`` when the run has no
    resolved game window at all (e.g. it vanished between steps); that, like every host-capture
    failure, is retried up to *max_attempts* times before this step is recorded degraded.

    *registry* and *profiles* are the same collaborators
    :func:`~civsim_harness.parity.screening.screen_capture` already takes -- this function is a
    thin retry/bookkeeping wrapper around that real gate, not a second implementation of it.
    *platform* is derived from *host_info* (``host_info.os.value``), matching
    ``catalogs/screening_profiles.yaml``'s own ``"windows" | "macos" | "linux"`` resolution keys,
    so callers need not thread a separate platform string through.

    *step_index* is this step's 1-based position within its turn attempt (``DecisionStep.
    step_index``) -- recorded on both the ``image_withheld`` event (T133) and, when this step is
    degraded, the ``capture_failed`` event (T157), so either event names exactly which step lost
    its image without a reader needing to cross-reference ``decision_step_id`` against the turn's
    step sequence separately.
    """
    resolved_capture_id = capture_id if capture_id is not None else CaptureId(uuid.uuid4().hex)
    platform = host_info.os.value

    attempt: CaptureAttempt | None = None
    outcome: ScreeningOutcome
    capture_path = CapturePath.NONE
    attempts_made = 0

    # The source gate's process-identity check needs the run's own located client. Callers that
    # already hold one pass it; the rest used to pass nothing, which made that check dead code in
    # production -- the gate silently skipped it on every real capture. Locating it here, through
    # the ``HostPlatform`` port this function already holds, is what makes the check actually run:
    # one process lookup per capture, against a port method every adapter already implements. A
    # host that cannot find its own client leaves this ``None``, and the gate withholds.
    resolved_process = (
        expected_process if expected_process is not None else host.locate_game_process()
    )

    if window is None:
        outcome = _host_failure_outcome("no game window is currently resolved to capture")
        attempts_made = 1
    else:
        outcome = _host_failure_outcome("capture never attempted")  # overwritten below
        for _ in range(max(1, max_attempts)):
            attempts_made += 1
            capture_path, attempt, outcome = _attempt_once(
                host=host,
                host_info=host_info,
                window=window,
                view_declaration_id=view_declaration_id,
                camera_state=camera_state,
                platform=platform,
                registry=registry,
                profiles=profiles,
                detector=detector,
                expected_process=resolved_process,
                detected_text_tokens=detected_text_tokens,
            )
            if outcome.is_clean:
                break

    degraded = not outcome.is_clean
    blob: bytes | None = None
    blob_ref: str | None = None
    blob_media_type: str | None = None
    if outcome.is_clean and attempt is not None:
        blob = attempt.frame.image_bytes
        blob_ref = hashlib.sha256(blob).hexdigest()
        blob_media_type = _WIRE_MEDIA_TYPES.get(attempt.frame.image_format.upper())

    if attempt is not None:
        capture = build_screen_capture(
            outcome,
            capture_id=resolved_capture_id,
            run_id=run_id,
            turn_number=turn_number,
            decision_step_id=decision_step_id,
            captured_at=captured_at,
            attempt=attempt,
            blob_ref=blob_ref,
            # T238: never `outcome.is_clean`. shown_to_agent is a statement about a dispatched
            # decision request, and none exists at capture time -- the run loop flips this on a
            # validated copy only at actual attachment, so the record can never claim the agent
            # saw an image the provider was not handed.
            shown_to_agent=False,
        )
    else:
        assert outcome.withheld_reason is not None  # every non-clean outcome carries one
        capture = _withheld_capture_without_attempt(
            capture_id=resolved_capture_id,
            run_id=run_id,
            turn_number=turn_number,
            decision_step_id=decision_step_id,
            captured_at=captured_at,
            camera_state=camera_state,
            view_declaration_id=view_declaration_id,
            capture_path=capture_path,
            withheld_reason=outcome.withheld_reason,
        )

    events: list[RunEvent] = []
    if degraded:
        # T157: bounded retries exhausted -- this step is visually degraded, distinguishable from
        # a step that never had images at all (FR-050, SC-013).
        events.append(
            RunEvent(
                event_id=EventId(uuid.uuid4().hex),
                run_id=run_id,
                turn_number=turn_number,
                step_index=step_index,
                event_type=RunEventType.CAPTURE_FAILED,
                occurred_at=captured_at,
                detail={
                    "decision_step_id": str(decision_step_id),
                    "attempts": attempts_made,
                    "withheld_reason": capture.withheld_reason.value
                    if capture.withheld_reason is not None
                    else None,
                    "reason": outcome.detail or "image screening withheld every attempt",
                },
            )
        )
        # T133: the withheld record's own evidence event, naming which gate caught it.
        events.append(
            RunEvent(
                event_id=EventId(uuid.uuid4().hex),
                run_id=run_id,
                turn_number=turn_number,
                step_index=step_index,
                event_type=RunEventType.IMAGE_WITHHELD,
                occurred_at=captured_at,
                detail={
                    "decision_step_id": str(decision_step_id),
                    "withheld_reason": capture.withheld_reason.value
                    if capture.withheld_reason is not None
                    else None,
                    # Gameplay 2026-09-21, block 10: this event carried no reason, so the ledger
                    # read "withheld non_player_ui, no detail" while the sibling capture_failed
                    # event named the matched categories. Both now say what the gate matched.
                    "reason": outcome.detail or "image screening withheld every attempt",
                },
            )
        )

    return StepCapture(
        capture=capture,
        events=tuple(events),
        blob=blob,
        blob_media_type=blob_media_type,
        visually_degraded=degraded,
    )
