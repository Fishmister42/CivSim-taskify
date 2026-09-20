"""``CaptureView`` -- the second screening gate (T022, data-model.md SS7).

002 screens captures for non-player UI before it stores them. This is the
*second*, independent gate the plan commits to (FR-032, plan.md Constraints),
and its rule is stated verbatim in data-model.md SS7 because it is the
load-bearing rule of the whole model:

    ``available`` is computed as ``screening_status == "screened_clean"``, full
    stop. Any other value -- ``withheld``, an absent capture record, a
    ``screening_status`` value this feature's code does not recognize (e.g. a
    future schema addition) -- resolves to ``available = false``.

That is why ``screening_status`` arrives here as a bare string rather than an
enum (see ``store_client/port.py``): parsing it into a closed enum would turn a
future 002 status value into a *crash* instead of a render-as-unavailable, and a
crash is not fail-closed, it is just broken.

**A finding, recorded rather than smoothed over.** data-model.md SS7 enumerates
four ``unavailable_reason`` values -- ``withheld``, ``capture_failed``,
``never_captured``, ``missing_record`` -- and none of them describes the case
the very same paragraph requires this model to handle: a ``screening_status``
this code does not recognise. Folding it into ``withheld`` would assert
something about the capture that nobody recorded. ``unrecognized_status`` is
added for it, in the same spirit as ``HealthState.unknown`` in the foundation,
and ``screening_status`` is carried verbatim alongside so a reader can always
see the store's own word.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from civsim_web.registry.loader import PanelRegistry
from civsim_web.viewmodels.base import UnavailableField, ViewModel
from civsim_web.viewmodels.gate import GatedReader

__all__ = [
    "CAPTURE_IMAGE_PATH",
    "SCREENED_CLEAN",
    "CaptureUnavailableReason",
    "CaptureView",
    "build_capture_view",
    "capture_image_path",
]

#: The one status that renders. Spelled once, compared once (see ``build_``).
SCREENED_CLEAN = "screened_clean"

#: Captures are fetched lazily from their own route, never inlined as a data
#: URI -- FR-036: viewing a turn must not require loading captures beyond those
#: being viewed.
CAPTURE_IMAGE_PATH = "/captures/{capture_id}/image"


def capture_image_path(capture_id: str) -> str:
    return CAPTURE_IMAGE_PATH.format(capture_id=capture_id)


class CaptureUnavailableReason(StrEnum):
    """Why a capture is not shown. Out-of-game telemetry, safe to display."""

    WITHHELD = "withheld"
    CAPTURE_FAILED = "capture_failed"
    NEVER_CAPTURED = "never_captured"
    MISSING_RECORD = "missing_record"
    UNRECOGNIZED_STATUS = "unrecognized_status"


#: 002 data-model.md SS8's ``withheld_reason`` vocabulary, mapped to the reason
#: this feature displays. ``capture_failed`` is the one value that names a
#: mechanical failure rather than a screening decision, so it keeps its own
#: identity; the rest are all "screening withheld it", which is what a reader
#: needs to know.
_WITHHELD_REASON_MAP: dict[str, CaptureUnavailableReason] = {
    "capture_failed": CaptureUnavailableReason.CAPTURE_FAILED,
    "non_player_ui": CaptureUnavailableReason.WITHHELD,
    "geometry_mismatch": CaptureUnavailableReason.WITHHELD,
    "provenance_failure": CaptureUnavailableReason.WITHHELD,
}


class CaptureView(ViewModel):
    """One screen capture, or an honest account of why there isn't one.

    Always constructed as a value, never omitted: data-model.md SS6 makes
    ``capture`` a non-optional sibling of ``observation`` and ``decision`` on a
    step, so that "the panels still render with the capture marked unavailable"
    (FR-034, SC-015) is the shape of the type rather than a template's good
    manners.
    """

    capture_id: str | None = None
    available: bool = False
    image_url: str | None = None
    unavailable_reason: CaptureUnavailableReason | None = None
    screening_status: str | None = None
    """The store's own word, verbatim. Out-of-game telemetry (FR-013): it says
    what the screening decided, never what the screened-out image contained."""
    captured_at: datetime | None = None
    turn_number: int | None = None
    unavailable: tuple[UnavailableField, ...] = ()


def build_capture_view(
    record: Any | None,
    *,
    registry: PanelRegistry,
    expected: bool = True,
) -> CaptureView:
    """Build a ``CaptureView`` from a ``ScreenCapture`` record, or from nothing.

    ``expected=False`` means the step declared no capture at all -- a run that
    predates capture, or a step where none was taken -- which is
    ``never_captured``. ``expected=True`` with ``record=None`` means a capture
    *was* referenced and the store could not produce its record, which is
    ``missing_record``. The two are different facts and FR-034 wants both
    sayable.
    """
    if record is None:
        return CaptureView(
            available=False,
            unavailable_reason=(
                CaptureUnavailableReason.MISSING_RECORD
                if expected
                else CaptureUnavailableReason.NEVER_CAPTURED
            ),
        )

    gate = GatedReader(registry, "ScreenCapture", record)
    status = gate.text("screening_status")
    capture_id = gate.text("capture_id")
    turn_number = gate.get("turn_number")

    # The rule, in one comparison, with no branch that could widen it.
    available = status == SCREENED_CLEAN

    if available:
        return CaptureView(
            capture_id=capture_id,
            available=True,
            image_url=capture_image_path(capture_id) if capture_id else None,
            screening_status=status,
            captured_at=gate.get("captured_at"),
            turn_number=turn_number,
            unavailable=gate.missing,
        )

    return CaptureView(
        capture_id=capture_id,
        available=False,
        image_url=None,
        unavailable_reason=_reason_for(status, gate.text("withheld_reason")),
        screening_status=status,
        # `captured_at` is withheld with the image: a capture nobody may look at
        # should not still answer "when was the screen in that state".
        captured_at=None,
        turn_number=turn_number,
        unavailable=gate.missing,
    )


def _reason_for(status: str | None, withheld_reason: str | None) -> CaptureUnavailableReason:
    """Name why, without ever asserting more than the record says."""
    if status is None:
        return CaptureUnavailableReason.MISSING_RECORD
    if status == "withheld":
        if withheld_reason is None:
            return CaptureUnavailableReason.WITHHELD
        return _WITHHELD_REASON_MAP.get(withheld_reason, CaptureUnavailableReason.WITHHELD)
    # A status this code has never heard of. Fail closed, and say *that* --
    # not "withheld", which would claim a screening decision nobody made.
    return CaptureUnavailableReason.UNRECOGNIZED_STATUS
