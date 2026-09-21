"""T260: what the camera's look-at answer does to a capture, on both sides of the provenance gate.

Every capture on the Linux node (200 of them, measured 2026-09-21) was withheld
``provenance_failure`` because ``lua/ingame/camera.lua`` could not say which plot the camera was
looking at, so ``target_revealed`` was never true. ``lua/ingame/camera.lua`` now resolves it
through ``UI.GetMapLookAtWorldTarget()`` + ``UI.GetPlotCoordFromWorld(...)`` (Firaxis' own pairing;
``tests/unit/test_camera_lua.py`` executes that Lua for real). This file pins what the Python side
does with the two answers:

- a confirmed-revealed target produces a screened-clean capture whose PNG bytes travel the real
  delivery seam -- ``select_screened_images`` -> ``assemble_context`` -> a provider call reporting
  ``image_count == 1`` (the production loop's own wiring of that seam is asserted far-side in
  ``tests/integration/test_image_attachment.py``);
- an unconfirmed one is still withheld -- but the withheld record's reason now names the accessor
  that failed, rather than stating a bare unexplained false.

The accessors themselves remain UNVERIFIED LIVE: nothing here proves the client answers them.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

from PIL import Image as PILImage

from civsim_harness.agent.context import assemble_context, select_screened_images
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
    CatalogVersionRef,
    DecisionStepId,
    DeclarationId,
    LuaContext,
    ModelRef,
    ObservationId,
    RunId,
)
from civsim_harness.models.turn import Observation, ScreeningStatus, WithheldReason
from civsim_harness.observe.capture import StepCapture, capture_for_step
from civsim_harness.parity.screening import load_screening_profiles
from civsim_harness.provider.port import Image
from civsim_harness.provider.stochastic import StochasticModelProvider
from civsim_harness.run.composition import _to_camera_state
from fakes.fake_host import FakeHostPlatform

VIEW_DECLARATION_ID = DeclarationId("views.test_world")
STEP_ID = DecisionStepId("step-lookat-1")

_WINDOW = GameWindow(
    handle=1,
    title="Sid Meier's Civilization VI (FAKE)",
    rect=WindowRect(left=0, top=0, width=8, height=8),
    pid=4_242,
)
_HOST_INFO = HostInfo(
    os=OperatingSystem.linux, os_version="test", session_type=LinuxSessionType.x11
)

#: What `lua/ingame/camera.lua` now reports on a build that answers both accessors, after
#: `run/composition.py::_to_camera_state` has renamed the reveal confirmation.
_LOOKING_AT_A_REVEALED_PLOT: dict[str, Any] = {
    "mode": "world",
    "zoom": 0.70710706710815,
    "target_plot": {"x": 24, "y": 30},
    "target_revealed": True,
}


def _registry_with_view() -> CapabilityRegistry:
    """A view that genuinely requires a revealed target -- the requirement every real view in
    ``catalogs/observations/views.yaml`` carries, and the one no Linux capture could satisfy."""
    view = ParityDeclaration(
        declaration_id=VIEW_DECLARATION_ID,
        kind=DeclarationKind.VIEW,
        summary="Test-only world view.",
        parity_basis="Look at the world view on screen.",
        context=LuaContext.IN_GAME,
        capability_id=CapabilityId("test.views"),
        output_schema={"type": "object"},
        camera_requirements=CameraRequirements(
            mode=CameraMode.WORLD, zoom_range=(0.0, 1.0), target_must_be_revealed=True
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


def _png_bytes() -> bytes:
    buffer = io.BytesIO()
    PILImage.new("RGB", (_WINDOW.rect.width, _WINDOW.rect.height), (12, 44, 92)).save(
        buffer, format="PNG"
    )
    return buffer.getvalue()


def _capture(camera_state: dict[str, Any], *, registry: CapabilityRegistry) -> StepCapture:
    host = FakeHostPlatform()
    host.set_capture_result(
        CaptureResult(
            status=CaptureStatus.ok,
            frame=CaptureFrame(
                width=_WINDOW.rect.width,
                height=_WINDOW.rect.height,
                rect=_WINDOW.rect,
                image_bytes=_png_bytes(),
                image_format="PNG",
            ),
        )
    )
    return capture_for_step(
        host=host,
        host_info=_HOST_INFO,
        window=_WINDOW,
        view_declaration_id=VIEW_DECLARATION_ID,
        camera_state=camera_state,
        run_id=RunId("run-lookat-unit"),
        turn_number=3,
        decision_step_id=STEP_ID,
        step_index=7,
        captured_at=datetime(2026, 9, 20, tzinfo=UTC),
        registry=registry,
        profiles=load_screening_profiles(),
        max_attempts=1,
    )


def _observation() -> Observation:
    return Observation(
        observation_id=ObservationId("obs-lookat"),
        decision_step_id=STEP_ID,
        assembled_at=datetime(2026, 9, 20, tzinfo=UTC),
        catalog_version=CatalogVersionRef(version="test", content_hash="test"),
        entries=[],
        captures=[],
        screen_identity="WorldScreen",
    )


def _reasons(step_capture: StepCapture) -> str:
    return " ".join(str(event.detail) for event in step_capture.events)


# --------------------------------------------------------------------------
# The pass path -- a confirmed look-at plot puts a PNG in front of the agent
# --------------------------------------------------------------------------


def test_a_confirmed_revealed_look_at_target_passes_the_provenance_gate() -> None:
    registry = _registry_with_view()

    result = _capture(dict(_LOOKING_AT_A_REVEALED_PLOT), registry=registry)

    assert result.capture.screening_status is ScreeningStatus.SCREENED_CLEAN
    assert result.capture.withheld_reason is None
    assert result.visually_degraded is False
    assert result.blob == _png_bytes()
    assert result.blob_media_type == "image/png"
    # Still not shown: attachment is settled later, at dispatch (T238).
    assert result.capture.shown_to_agent is False


def test_the_clean_captures_png_reaches_a_provider_call_as_one_image() -> None:
    """The delivery seam, end to end from the capture: the same bytes the gates screened are the
    bytes the request carries, and the provider's own accounting says one image arrived."""
    registry = _registry_with_view()
    result = _capture(dict(_LOOKING_AT_A_REVEALED_PLOT), registry=registry)
    assert result.blob is not None and result.blob_media_type is not None

    images = select_screened_images(
        observation=_observation(),
        registry=registry,
        candidates=[(result.capture, Image(media_type=result.blob_media_type, data=result.blob))],
    )

    assert [image.data for image in images] == [result.blob]
    request = assemble_context(
        observation=_observation(),
        guidance=None,
        model=ModelRef(provider="stochastic", model="stochastic-v1"),
        step_index=7,
        response_schema={"type": "object"},
        images=images,
        actions=None,
    )
    assert [image.data for image in request.images] == [result.blob]
    assert StochasticModelProvider(seed=3).complete(request).image_count == 1


def test_a_capture_from_a_different_step_is_never_delivered_for_this_one() -> None:
    """FR-015: the reveal confirmation buys this step's own frame a way through, nothing more."""
    registry = _registry_with_view()
    result = _capture(dict(_LOOKING_AT_A_REVEALED_PLOT), registry=registry)
    assert result.blob is not None and result.blob_media_type is not None
    stale = result.capture.model_copy(update={"decision_step_id": DecisionStepId("step-earlier")})

    images = select_screened_images(
        observation=_observation(),
        registry=registry,
        candidates=[(stale, Image(media_type=result.blob_media_type, data=result.blob))],
    )

    assert images == []


# --------------------------------------------------------------------------
# The withheld path -- unconfirmed still means withheld, now with a reason that names why
# --------------------------------------------------------------------------


def test_an_absent_look_at_accessor_withholds_the_capture_naming_the_accessor() -> None:
    """The 200-capture Linux state, told honestly: the record must blame
    ``UI.GetMapLookAtWorldTarget``, not merely report an unconfirmed reveal."""
    registry = _registry_with_view()
    camera_state = {
        "mode": "world",
        "zoom": 0.70710706710815,
        "target_plot": None,
        "target_revealed": False,
        "target_unavailable_reason": (
            "UI.GetMapLookAtWorldTarget is absent on this build and UI.GetCameraTargetPlot "
            "answered no plot"
        ),
    }

    result = _capture(camera_state, registry=registry)

    assert result.capture.screening_status is ScreeningStatus.WITHHELD
    assert result.capture.withheld_reason is WithheldReason.PROVENANCE_FAILURE
    assert result.visually_degraded is True
    assert result.blob is None
    assert "UI.GetMapLookAtWorldTarget is absent" in _reasons(result)


def test_a_resolved_but_unrevealed_target_is_withheld_naming_the_plot() -> None:
    """Principle I: a human cannot see unrevealed terrain, so this capture is withheld even though
    every accessor answered -- and the reason says which plot, not "something failed"."""
    registry = _registry_with_view()
    camera_state = {
        "mode": "world",
        "zoom": 0.5,
        "target_plot": {"x": 24, "y": 30},
        "target_revealed": False,
    }

    result = _capture(camera_state, registry=registry)

    assert result.capture.screening_status is ScreeningStatus.WITHHELD
    assert result.capture.withheld_reason is WithheldReason.PROVENANCE_FAILURE
    assert "(24, 30)" in _reasons(result)
    assert "not revealed" in _reasons(result)


def test_a_camera_state_that_knows_nothing_still_withholds_with_the_original_wording() -> None:
    registry = _registry_with_view()

    result = _capture({"mode": "world", "zoom": 0.5}, registry=registry)

    assert result.capture.screening_status is ScreeningStatus.WITHHELD
    assert result.capture.withheld_reason is WithheldReason.PROVENANCE_FAILURE
    assert "none was confirmed revealed" in _reasons(result)


# --------------------------------------------------------------------------
# The translation seam the reason travels through
# --------------------------------------------------------------------------


def test_the_lua_reason_rides_through_to_the_camera_state_the_gate_reads() -> None:
    state = _to_camera_state(
        {
            "mode": "world",
            "zoom": 0.4,
            "target_plot": None,
            "target_is_revealed": False,
            "target_unavailable_reason": "UI.GetPlotCoordFromWorld errored: boom",
        }
    )

    assert state["target_revealed"] is False
    assert state["target_unavailable_reason"] == "UI.GetPlotCoordFromWorld errored: boom"


def test_no_reason_is_invented_when_the_lua_reports_none() -> None:
    state = _to_camera_state(
        {
            "mode": "world",
            "zoom": 0.4,
            "target_plot": {"x": 24, "y": 30},
            "target_is_revealed": True,
        }
    )

    assert state["target_revealed"] is True
    assert "target_unavailable_reason" not in state
