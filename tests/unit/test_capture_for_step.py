"""Unit tests for ``observe/capture.py``'s ``capture_for_step`` (T133/T157; T238's source half).

The integration story -- a clean frame's bytes reaching ``provider.complete`` and the record
reflecting actual attachment -- lives in ``tests/integration/test_image_attachment.py``. This
file pins the contract that makes that story possible at the source:

- a screened-clean capture comes back ``shown_to_agent=False`` (T238: shown-ness is a statement
  about a dispatched decision request, which does not exist at capture time -- the old
  ``shown_to_agent=outcome.is_clean`` recorded an intention);
- ``blob_media_type`` is populated exactly when the frame's format has a wire form (a raw
  framebuffer stores fine but can never be attached as-is);
- a step with no frame at all is a withheld, degraded record with its ``capture_failed`` and
  ``image_withheld`` events naming the step (T157/T133);
- a failing T249 capture-precondition preflight withholds the step WITHOUT ever asking the host
  for a frame, through the same degradation path, with the precondition's reason recorded -- and
  a passing preflight is consulted but changes nothing on the clean path (the far side, through
  the real turn cycle and store, lives in ``tests/integration/test_capture_preflight.py``).
"""

from __future__ import annotations

import hashlib
import io
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType

from PIL import Image as PILImage

from civsim_harness.capability.loader import Catalog
from civsim_harness.capability.registry import CapabilityRegistry
from civsim_harness.host.detect import HostInfo, LinuxSessionType, OperatingSystem
from civsim_harness.host.port import (
    CaptureFrame,
    CaptureResult,
    CaptureStatus,
    GameWindow,
    WindowRect,
)
from civsim_harness.models.catalog import (
    CameraMode,
    CameraRequirements,
    CatalogVersion,
    DeclarationKind,
    ParityDeclaration,
)
from civsim_harness.models.common import (
    CapabilityId,
    DecisionStepId,
    DeclarationId,
    LuaContext,
    RunId,
)
from civsim_harness.models.records import RunEventType
from civsim_harness.models.turn import ScreeningStatus, WithheldReason
from civsim_harness.observe.capture import capture_for_step
from civsim_harness.parity.screening import load_screening_profiles
from fakes.fake_host import FakeHostPlatform

VIEW_DECLARATION_ID = DeclarationId("views.test_world")

_WINDOW = GameWindow(
    handle=1,
    title="Sid Meier's Civilization VI (FAKE)",
    rect=WindowRect(left=0, top=0, width=8, height=8),
    pid=4_242,
)

_HOST_INFO = HostInfo(
    os=OperatingSystem.linux, os_version="test", session_type=LinuxSessionType.x11
)

_CAMERA_STATE = {"mode": "world", "zoom": 0.5}


def _registry_with_view() -> CapabilityRegistry:
    view = ParityDeclaration(
        declaration_id=VIEW_DECLARATION_ID,
        kind=DeclarationKind.VIEW,
        summary="Test-only world view.",
        parity_basis="Look at the world view on screen.",
        context=LuaContext.IN_GAME,
        capability_id=CapabilityId("test.views"),
        output_schema={"type": "object"},
        camera_requirements=CameraRequirements(
            mode=CameraMode.WORLD, zoom_range=(0.0, 1.0), target_must_be_revealed=False
        ),
        screening_profile="default",
        introduced_in_version="test",
    )
    catalog = Catalog(
        root=Path("."),
        version=CatalogVersion(
            version="test", content_hash="test", declaration_ids=[view.declaration_id]
        ),
        declarations=MappingProxyType({view.declaration_id: view}),
        capabilities=MappingProxyType({}),
    )
    return CapabilityRegistry(catalog=catalog)


def _frame(image_bytes: bytes, image_format: str) -> CaptureFrame:
    return CaptureFrame(
        width=_WINDOW.rect.width,
        height=_WINDOW.rect.height,
        rect=_WINDOW.rect,
        image_bytes=image_bytes,
        image_format=image_format,
    )


def _png_bytes() -> bytes:
    buffer = io.BytesIO()
    PILImage.new("RGB", (_WINDOW.rect.width, _WINDOW.rect.height), (12, 44, 92)).save(
        buffer, format="PNG"
    )
    return buffer.getvalue()


def _capture(host: FakeHostPlatform, *, window: GameWindow | None = _WINDOW):
    return capture_for_step(
        host=host,
        host_info=_HOST_INFO,
        window=window,
        view_declaration_id=VIEW_DECLARATION_ID,
        camera_state=dict(_CAMERA_STATE),
        run_id=RunId("run-capture-unit"),
        turn_number=3,
        decision_step_id=DecisionStepId("step-7"),
        step_index=7,
        captured_at=datetime(2026, 9, 20, tzinfo=UTC),
        registry=_registry_with_view(),
        profiles=load_screening_profiles(),
    )


def test_a_screened_clean_capture_is_never_born_shown() -> None:
    """T238: clean means storable and offerable -- never already-shown. The record's
    ``shown_to_agent`` may only be flipped by the run loop at actual attachment."""
    png = _png_bytes()
    host = FakeHostPlatform()
    host.set_capture_result(CaptureResult(status=CaptureStatus.ok, frame=_frame(png, "PNG")))

    result = _capture(host)

    assert result.capture.screening_status is ScreeningStatus.SCREENED_CLEAN
    assert result.capture.shown_to_agent is False
    assert result.visually_degraded is False
    assert result.blob == png
    assert result.capture.blob_ref == hashlib.sha256(png).hexdigest()
    assert result.blob_media_type == "image/png"
    assert result.events == ()


def test_a_raw_framebuffer_frame_stores_its_blob_but_has_no_wire_media_type() -> None:
    """A clean BGRA8 frame is evidence worth storing, but it has no wire form -- so the loop can
    never attach it as-is, and ``blob_media_type`` says so."""
    raw = bytes(_WINDOW.rect.width * _WINDOW.rect.height * 4)
    host = FakeHostPlatform()
    host.set_capture_result(CaptureResult(status=CaptureStatus.ok, frame=_frame(raw, "BGRA8")))

    result = _capture(host)

    assert result.capture.screening_status is ScreeningStatus.SCREENED_CLEAN
    assert result.capture.shown_to_agent is False
    assert result.blob == raw
    assert result.blob_media_type is None


def test_a_failing_capture_precondition_withholds_the_step_without_taking_a_frame() -> None:
    """T249: a non-passing preflight means the frame must never be taken -- not taken and
    discarded. The host's ``capture_window`` is never called, and the step takes the exact
    T157/T133 degradation path with the precondition's reason recorded on the timeline event."""
    reason = "scripted: no compositing manager owns _NET_WM_CM_S0"
    host = FakeHostPlatform()
    host.set_capture_result(
        CaptureResult(status=CaptureStatus.ok, frame=_frame(_png_bytes(), "PNG"))
    )  # a clean frame WOULD come back, proving the preflight is what withheld it
    host.set_capture_preconditions_failed(reason)

    result = _capture(host)

    assert host.capture_calls == []  # the frame was never taken at all
    assert host.capture_precondition_calls >= 1
    assert result.capture.screening_status is ScreeningStatus.WITHHELD
    assert result.capture.withheld_reason is WithheldReason.CAPTURE_FAILED
    assert result.capture.shown_to_agent is False
    assert result.capture.blob_ref is None
    assert result.blob is None
    assert result.blob_media_type is None
    assert result.visually_degraded is True
    assert [event.event_type for event in result.events] == [
        RunEventType.CAPTURE_FAILED,
        RunEventType.IMAGE_WITHHELD,
    ]
    assert reason in str(result.events[0].detail["reason"])


def test_a_passing_precondition_is_consulted_and_changes_nothing_on_the_clean_path() -> None:
    """T249's other side: the preflight really runs before a clean capture, and passing it
    grants nothing beyond what T238 already pinned -- clean, stored, and still never born
    shown (attachment stays the run loop's VALIDATED-tier decision)."""
    png = _png_bytes()
    host = FakeHostPlatform()
    host.set_capture_result(CaptureResult(status=CaptureStatus.ok, frame=_frame(png, "PNG")))

    result = _capture(host)

    assert host.capture_precondition_calls == 1
    assert host.capture_calls != []
    assert result.capture.screening_status is ScreeningStatus.SCREENED_CLEAN
    assert result.capture.shown_to_agent is False
    assert result.blob == png


def test_a_step_with_no_frame_is_withheld_degraded_and_named_by_its_events() -> None:
    """T157/T133: a step whose window vanished produces a withheld record (no blob, never
    shown), counts as visually degraded, and carries ``capture_failed`` + ``image_withheld``
    events naming this exact step."""
    result = _capture(FakeHostPlatform(), window=None)

    assert result.capture.screening_status is ScreeningStatus.WITHHELD
    assert result.capture.withheld_reason is WithheldReason.CAPTURE_FAILED
    assert result.capture.shown_to_agent is False
    assert result.blob is None
    assert result.blob_media_type is None
    assert result.visually_degraded is True
    assert [event.event_type for event in result.events] == [
        RunEventType.CAPTURE_FAILED,
        RunEventType.IMAGE_WITHHELD,
    ]
    assert {event.step_index for event in result.events} == {7}
