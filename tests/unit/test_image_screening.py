"""Unit tests for the four image-screening gates (T124, research R7).

Each of the four gates -- source, geometry, provenance, content -- is
exercised independently: a fixture attempt that would otherwise be clean has
exactly one property broken at a time, and the assertion is that
:func:`screen_capture` withholds with the one :class:`WithheldReason` that
gate maps onto (see ``parity/screening.py``'s module docstring table). The
final section asserts the invariant the whole pipeline exists to guarantee
(SC-019, invariant I6): a withheld capture is never stored and never shown,
enforced structurally by :func:`build_screen_capture` and backstopped by
``ScreenCapture``'s own pydantic validator.
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageDraw
from pydantic import ValidationError

from civsim_harness.capability.loader import Catalog
from civsim_harness.capability.registry import CapabilityRegistry
from civsim_harness.errors import CatalogError
from civsim_harness.host.port import CaptureFrame, GameProcess, GameWindow, WindowRect
from civsim_harness.models.catalog import (
    CameraMode,
    CameraRequirements,
    CapabilityPath,
    CatalogVersion,
    DeclarationKind,
    IntegrationCapability,
    ParityDeclaration,
)
from civsim_harness.models.common import CapturePath, DecisionStepId, LuaContext, RunId
from civsim_harness.models.turn import ScreeningStatus, WithheldReason
from civsim_harness.parity import screening

WIDTH = 640
HEIGHT = 480

# --------------------------------------------------------------------------
# Fixtures: a minimal, otherwise-valid catalog with one view declaration.
# --------------------------------------------------------------------------


def _build_registry(
    *, target_must_be_revealed: bool = True, screening_profile: str = "default"
) -> CapabilityRegistry:
    capability = IntegrationCapability(
        capability_id="camera.control",
        path=CapabilityPath.FIRETUNER,
        implementation_ref="lua/gamecore/camera.lua",
        reads=["camera state"],
        writes=["camera state"],
        firetuner_gap=None,
        parity_basis=None,
    )
    view = ParityDeclaration(
        declaration_id="views.world",
        kind=DeclarationKind.VIEW,
        summary="The main map view.",
        parity_basis="Look at the main map view.",
        context=LuaContext.IN_GAME,
        capability_id="camera.control",
        camera_requirements=CameraRequirements(
            mode=CameraMode.WORLD,
            zoom_range=(0.2, 1.0),
            target_must_be_revealed=target_must_be_revealed,
        ),
        screening_profile=screening_profile,
        output_schema={"type": "object"},
        introduced_in_version="2026.09.1",
    )
    catalog = Catalog(
        root=Path("."),
        version=CatalogVersion(
            version="test", content_hash="deadbeef", declaration_ids=[view.declaration_id]
        ),
        declarations={view.declaration_id: view},
        capabilities={capability.capability_id: capability},
    )
    return CapabilityRegistry(catalog=catalog)


@pytest.fixture
def registry() -> CapabilityRegistry:
    return _build_registry()


@pytest.fixture
def profiles() -> screening.ScreeningProfiles:
    # Exercises the real, authored catalog data (T100) -- not a test fixture
    # copy -- since the content gate must be driven by it, never by a
    # hardcoded stand-in (task instructions).
    return screening.load_screening_profiles()


# --------------------------------------------------------------------------
# Image builders
# --------------------------------------------------------------------------


def _frame_from_image(
    image: Image.Image, *, width: int = WIDTH, height: int = HEIGHT
) -> CaptureFrame:
    data = image.convert("RGB").tobytes()
    return CaptureFrame(
        width=width,
        height=height,
        rect=WindowRect(left=0, top=0, width=width, height=height),
        image_bytes=data,
        image_format="RGB8",
    )


def _solid_frame(color: tuple[int, int, int] = (40, 90, 40)) -> CaptureFrame:
    return _frame_from_image(Image.new("RGB", (WIDTH, HEIGHT), color))


def _bordered_frame(
    *, interior: tuple[int, int, int] = (40, 90, 40), border: tuple[int, int, int] = (255, 241, 0)
) -> CaptureFrame:
    """A flat interior with a distinctly-coloured flat band drawn at every edge.

    Mirrors research R6's Windows Graphics Capture recording-border risk
    generically -- the exact colour is irrelevant to the detector, which
    reacts to "flat band, different colour from the scene it sits on", not
    to yellow specifically.
    """
    image = Image.new("RGB", (WIDTH, HEIGHT), interior)
    draw = ImageDraw.Draw(image)
    for offset in range(4):
        draw.rectangle([offset, offset, WIDTH - 1 - offset, HEIGHT - 1 - offset], outline=border)
    return _frame_from_image(image)


def _in_frame_overlay_frame(*, background: tuple[int, int, int] = (30, 70, 140)) -> CaptureFrame:
    """A flat background with one small, busy corner patch.

    Stands in for the Linux live finding (a 60 FPS overlay the game itself
    composites *inside* its own window): the detector never looks at what
    the patch says, only that a corner is far busier than the rest of an
    otherwise natural frame.
    """
    image = Image.new("RGB", (WIDTH, HEIGHT), background)
    pixels = image.load()
    rng = random.Random(1234)
    corner_w, corner_h = int(WIDTH * 0.12), int(HEIGHT * 0.12)
    for x in range(corner_w):
        for y in range(corner_h):
            pixels[x, y] = (rng.randrange(256), rng.randrange(256), rng.randrange(256))
    return _frame_from_image(image)


#: The one geometry every real frame the harness has ever shown a model was captured at
#: (frame-retro-audit-2026-09-22.md: 188 distinct blobs, all 1920x1200 PNG/RGB, X11/XComposite).
#: The corner heuristic's thresholds are ratios and absolute variances, both of which depend on
#: frame size, so the negative control is built at the real size rather than at this file's
#: convenient 640x480.
LIVE_WIDTH = 1920
LIVE_HEIGHT = 1200


def _realistic_gameplay_frame() -> CaptureFrame:
    """The negative control the corner heuristic never had: an ordinary, uncontaminated frame.

    Every ``SCREENED_CLEAN`` assertion in this file runs against ``_solid_frame`` -- a uniformly
    flat image -- and the single positive fixture ``_in_frame_overlay_frame`` is flat background
    plus uniform random noise in one corner. That pair is the best possible case for a
    corner-variance detector in both directions: nothing in it demonstrates the gate passes a
    frame that looks like Civilization VI.

    This fixture is built from what the frame-retro audit actually found in those 188 real
    frames: low-variance terrain over the middle, the game's own resource bar from row 0, leader
    portraits and a tooltip in the top-right, a minimap panel bottom-left and a unit panel
    bottom-right. It is *synthetic* and says so -- its corners come out busier relative to the
    whole frame (ratios ~2.3-5.0) than the real flagged frames did (1.800-1.802), because real
    game UI also lines the edges between the corners and lifts whole-frame variance. It is
    therefore a harder case than live, not a replica of one; the authoritative real-frame numbers
    are in the spike, not here.
    """
    rng = random.Random(20260922)
    image = Image.new("RGB", (LIVE_WIDTH, LIVE_HEIGHT), (58, 96, 62))
    pixels = image.load()
    for y in range(0, LIVE_HEIGHT, 2):
        for x in range(0, LIVE_WIDTH, 2):
            jitter = rng.randint(-9, 9)
            colour = (58 + jitter, 96 + jitter, 62 + jitter)
            for dx, dy in ((0, 0), (1, 0), (0, 1), (1, 1)):
                pixels[x + dx, y + dy] = colour

    draw = ImageDraw.Draw(image)
    # The game's own resource bar, which begins at row 0 on every real frame.
    draw.rectangle([0, 0, LIVE_WIDTH, 46], fill=(18, 20, 26))
    for index in range(14):
        left = 20 + index * 120
        draw.rectangle([left, 10, left + 18, 34], fill=(214, 190, 120))
        draw.rectangle([left + 24, 14, left + 92, 20], fill=(232, 232, 226))
        draw.rectangle([left + 24, 26, left + 70, 31], fill=(160, 168, 176))
    # Top-right: the leader portraits and tooltip the audit found behind its five flagged frames.
    draw.rectangle([LIVE_WIDTH - 300, 54, LIVE_WIDTH - 12, 250], fill=(24, 26, 32))
    for index in range(4):
        left = LIVE_WIDTH - 290 + index * 70
        draw.ellipse([left, 64, left + 56, 120], fill=(190, 140, 90))
        draw.rectangle([left, 126, left + 56, 134], fill=(206, 60, 60))
    draw.rectangle([LIVE_WIDTH - 290, 150, LIVE_WIDTH - 24, 238], fill=(40, 44, 54))
    for row in range(7):
        draw.rectangle(
            [LIVE_WIDTH - 280, 158 + row * 11, LIVE_WIDTH - 40 - (row % 3) * 40, 164 + row * 11],
            fill=(228, 228, 222),
        )
    # Bottom-left: the minimap panel.
    draw.rectangle([12, LIVE_HEIGHT - 260, 330, LIVE_HEIGHT - 12], fill=(20, 22, 28))
    for _ in range(240):
        left = rng.randrange(24, 318)
        top = rng.randrange(LIVE_HEIGHT - 248, LIVE_HEIGHT - 24)
        draw.rectangle(
            [left, top, left + 6, top + 6],
            fill=(rng.randrange(60, 220), rng.randrange(60, 220), rng.randrange(60, 200)),
        )
    # Bottom-right: the unit panel and its action buttons.
    draw.rectangle([LIVE_WIDTH - 340, LIVE_HEIGHT - 220, LIVE_WIDTH - 12, LIVE_HEIGHT - 12],
                   fill=(22, 24, 30))
    for index in range(10):
        left = LIVE_WIDTH - 326 + (index % 5) * 62
        top = LIVE_HEIGHT - 206 + (index // 5) * 70
        draw.rectangle([left, top, left + 50, top + 58], fill=(52, 58, 70))
        draw.ellipse([left + 12, top + 12, left + 38, top + 38], fill=(206, 186, 130))

    return _frame_from_image(image, width=LIVE_WIDTH, height=LIVE_HEIGHT)


def _window(*, pid: int = 1234, title: str = "Sid Meier's Civilization VI") -> GameWindow:
    return GameWindow(handle=1, title=title, rect=WindowRect(0, 0, WIDTH, HEIGHT), pid=pid)


_PROCESS = GameProcess(pid=1234, name="CivilizationVI")


def _attempt(**overrides: Any) -> screening.CaptureAttempt:
    base: dict[str, Any] = dict(
        frame=_solid_frame(),
        capture_path=CapturePath.WINDOWS_GRAPHICS_CAPTURE,
        window=_window(),
        view_declaration_id="views.world",
        camera_state={"mode": "world", "zoom": 0.5, "target_revealed": True},
        platform="windows",
        expected_process=_PROCESS,
        # An *empty* token set, not ``None``: this fixture models a caller whose text-evidence
        # source (window-title enumeration) ran and found nothing to report. ``None`` -- the
        # dataclass default, and what production passed until this was fixed -- means no such
        # source ran, which leaves most reject categories unexamined and must withhold. Every
        # SCREENED_CLEAN assertion below therefore rests on evidence having been gathered, which
        # is exactly the precondition the gate now enforces instead of assuming.
        detected_text_tokens=frozenset(),
    )
    base.update(overrides)
    return screening.CaptureAttempt(**base)


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


def test_clean_capture_passes_all_four_gates(registry, profiles) -> None:
    outcome = screening.screen_capture(_attempt(), registry=registry, profiles=profiles)

    assert outcome.status is ScreeningStatus.SCREENED_CLEAN
    assert outcome.withheld_reason is None
    assert outcome.failed_gate is None
    assert outcome.is_clean is True


# --------------------------------------------------------------------------
# Gate 1 -- source
# --------------------------------------------------------------------------


def test_source_gate_withholds_on_capture_path_none(registry, profiles) -> None:
    outcome = screening.screen_capture(
        _attempt(capture_path=CapturePath.NONE), registry=registry, profiles=profiles
    )

    assert outcome.status is ScreeningStatus.WITHHELD
    assert outcome.failed_gate is screening.ScreeningGate.SOURCE
    assert outcome.withheld_reason is WithheldReason.CAPTURE_FAILED


def test_source_gate_withholds_on_process_identity_mismatch(registry, profiles) -> None:
    other_process = GameProcess(pid=9999, name="not-civ6")

    outcome = screening.screen_capture(
        _attempt(expected_process=other_process), registry=registry, profiles=profiles
    )

    assert outcome.status is ScreeningStatus.WITHHELD
    assert outcome.failed_gate is screening.ScreeningGate.SOURCE
    assert outcome.withheld_reason is WithheldReason.CAPTURE_FAILED


def test_source_gate_withholds_when_no_located_process_is_supplied(registry, profiles) -> None:
    """Rewritten from ``test_source_gate_skips_process_check_when_none_supplied``.

    That test asserted SCREENED_CLEAN for ``expected_process=None`` -- which was *exactly* the
    shape every production capture had, because ``run/decision_loop.py`` never supplied a
    process. So the one test covering the production configuration of this gate certified it, and
    the process-identity check ran nowhere but in the tests that passed a process. A test that
    green-lights the broken configuration is worse than no test: it converts a defect into a
    documented guarantee and defends it against being fixed.

    The behaviour it described was wrong, not just under-covered. A capture whose window cannot
    be tied to the run's own client is a capture of *something*, and the gate exists to say which
    something. Not knowing is withholding.
    """
    outcome = screening.screen_capture(
        _attempt(expected_process=None), registry=registry, profiles=profiles
    )

    assert outcome.status is ScreeningStatus.WITHHELD
    assert outcome.failed_gate is screening.ScreeningGate.SOURCE
    assert outcome.withheld_reason is WithheldReason.CAPTURE_FAILED
    assert "did not run" in (outcome.detail or "")


# --------------------------------------------------------------------------
# Gate 2 -- geometry
# --------------------------------------------------------------------------


def test_geometry_gate_withholds_on_dimension_mismatch(registry, profiles) -> None:
    small_image = Image.new("RGB", (100, 100), (40, 90, 40))
    small_frame = _frame_from_image(small_image, width=100, height=100)

    outcome = screening.screen_capture(
        _attempt(frame=small_frame), registry=registry, profiles=profiles
    )

    assert outcome.status is ScreeningStatus.WITHHELD
    assert outcome.failed_gate is screening.ScreeningGate.GEOMETRY
    assert outcome.withheld_reason is WithheldReason.GEOMETRY_MISMATCH


def test_geometry_gate_withholds_on_stale_frame_rect(registry, profiles) -> None:
    """The frame reports the right width/height but its own rect disagrees (stale surface)."""
    stale_frame = CaptureFrame(
        width=WIDTH,
        height=HEIGHT,
        rect=WindowRect(left=0, top=0, width=100, height=100),
        image_bytes=Image.new("RGB", (WIDTH, HEIGHT), (40, 90, 40)).tobytes(),
        image_format="RGB8",
    )

    outcome = screening.screen_capture(
        _attempt(frame=stale_frame), registry=registry, profiles=profiles
    )

    assert outcome.status is ScreeningStatus.WITHHELD
    assert outcome.failed_gate is screening.ScreeningGate.GEOMETRY
    assert outcome.withheld_reason is WithheldReason.GEOMETRY_MISMATCH


def test_geometry_gate_tolerates_small_deltas(registry, profiles) -> None:
    slightly_off = _frame_from_image(
        Image.new("RGB", (WIDTH - 2, HEIGHT), (40, 90, 40)), width=WIDTH - 2, height=HEIGHT
    )

    outcome = screening.screen_capture(
        _attempt(frame=slightly_off, geometry_tolerance=0.02), registry=registry, profiles=profiles
    )

    assert outcome.status is ScreeningStatus.SCREENED_CLEAN


# --------------------------------------------------------------------------
# Gate 3 -- provenance
# --------------------------------------------------------------------------


def test_provenance_gate_withholds_on_unresolvable_declaration(registry, profiles) -> None:
    outcome = screening.screen_capture(
        _attempt(view_declaration_id="views.nonexistent"), registry=registry, profiles=profiles
    )

    assert outcome.status is ScreeningStatus.WITHHELD
    assert outcome.failed_gate is screening.ScreeningGate.PROVENANCE
    assert outcome.withheld_reason is WithheldReason.PROVENANCE_FAILURE


def test_provenance_gate_withholds_on_wrong_camera_mode(registry, profiles) -> None:
    outcome = screening.screen_capture(
        _attempt(camera_state={"mode": "strategic", "zoom": 0.5, "target_revealed": True}),
        registry=registry,
        profiles=profiles,
    )

    assert outcome.status is ScreeningStatus.WITHHELD
    assert outcome.failed_gate is screening.ScreeningGate.PROVENANCE
    assert outcome.withheld_reason is WithheldReason.PROVENANCE_FAILURE


def test_provenance_gate_withholds_on_zoom_out_of_range(registry, profiles) -> None:
    outcome = screening.screen_capture(
        _attempt(camera_state={"mode": "world", "zoom": 5.0, "target_revealed": True}),
        registry=registry,
        profiles=profiles,
    )

    assert outcome.status is ScreeningStatus.WITHHELD
    assert outcome.failed_gate is screening.ScreeningGate.PROVENANCE


def test_provenance_gate_withholds_on_unconfirmed_revealed_target(registry, profiles) -> None:
    outcome = screening.screen_capture(
        _attempt(camera_state={"mode": "world", "zoom": 0.5}),  # target_revealed omitted
        registry=registry,
        profiles=profiles,
    )

    assert outcome.status is ScreeningStatus.WITHHELD
    assert outcome.failed_gate is screening.ScreeningGate.PROVENANCE
    assert outcome.withheld_reason is WithheldReason.PROVENANCE_FAILURE


def test_provenance_gate_does_not_require_revealed_target_when_view_does_not_need_it(
    profiles,
) -> None:
    lenient_registry = _build_registry(target_must_be_revealed=False)

    outcome = screening.screen_capture(
        _attempt(camera_state={"mode": "world", "zoom": 0.5}),
        registry=lenient_registry,
        profiles=profiles,
    )

    assert outcome.status is ScreeningStatus.SCREENED_CLEAN


# --------------------------------------------------------------------------
# Gate 4 -- content
# --------------------------------------------------------------------------


def test_content_gate_withholds_on_capture_border_artifact(registry, profiles) -> None:
    outcome = screening.screen_capture(
        _attempt(frame=_bordered_frame()), registry=registry, profiles=profiles
    )

    assert outcome.status is ScreeningStatus.WITHHELD
    assert outcome.failed_gate is screening.ScreeningGate.CONTENT
    assert outcome.withheld_reason is WithheldReason.NON_PLAYER_UI
    assert "windows_capture_border" in (outcome.detail or "")


def test_content_gate_withholds_on_in_frame_overlay_general_finding(registry, profiles) -> None:
    """The Linux live finding: chrome the game composites *inside* its own frame.

    Nothing about this fixture encodes "60 FPS" or any specific overlay
    text -- it is a generic busy corner patch, standing in for whatever an
    in-engine overlay actually looks like, to prove the *general* capability
    catches in-frame chrome rather than only OS-level chrome around it.
    """
    outcome = screening.screen_capture(
        _attempt(frame=_in_frame_overlay_frame()), registry=registry, profiles=profiles
    )

    assert outcome.status is ScreeningStatus.WITHHELD
    assert outcome.failed_gate is screening.ScreeningGate.CONTENT
    assert outcome.withheld_reason is WithheldReason.NON_PLAYER_UI
    assert "debug_overlay" in (outcome.detail or "")


def test_the_corner_heuristic_fires_on_an_ordinary_gameplay_frame(registry, profiles) -> None:
    """The measurement, recorded so it cannot be lost: real-looking game UI trips the detector.

    Measured on this fixture, 1920x1200, ``_CORNER_FRACTION=0.12`` (a 230x144 corner box):
    whole-frame variance ~591, and all four corners over threshold -- top-left ~1937 (ratio
    3.28), top-right ~2964 (5.02), bottom-left ~1680 (2.85), bottom-right ~1362 (2.31) -- against
    ``_CORNER_VARIANCE_RATIO_MIN=1.8`` and ``_CORNER_VARIANCE_ABS_MIN=200.0``. Every corner of an
    ordinary Civ VI frame carries HUD, so "one corner is busier than the whole frame" describes
    the game, not an intruder.

    This is deliberately separate from the xfail below: this test pins *what the detector does*,
    so a future change to the technique or the thresholds shows up here as an explicit edit with
    a number attached, instead of silently moving the boundary.
    """
    image = screening._decode_frame(_realistic_gameplay_frame())  # noqa: SLF001 - whitebox
    assert image is not None
    assert screening._corner_overlay_is_suspect(image) is True  # noqa: SLF001


@pytest.mark.xfail(
    strict=True,
    reason=(
        "KNOWN DEFECT, not an accepted outcome: the corner-variance technique false-positives on "
        "ordinary gameplay frames. Corroborated on real pixels by "
        "specs/002-civ-playing-harness/spikes/frame-retro-audit-2026-09-22.md, where it fired on "
        "5 of 188 uncontaminated live frames, all at ratio 1.800-1.802 against a 1.8 threshold. "
        "strict=True on purpose: whoever makes this pass must come back and justify how, because "
        "the wrong way to make it pass -- raising _CORNER_VARIANCE_ABS_MIN or the ratio until "
        "live frames get through, dropping debug_overlay from a profile, deleting the technique "
        "-- weakens the one in-frame-chrome defence the harness has. The right way needs a "
        "real-frame sample of both contaminated and clean frames, which does not exist yet: the "
        "421 withheld captures were stored with blob_ref NULL, so their pixels are gone."
    ),
)
def test_a_realistic_gameplay_frame_should_pass_the_content_gate(registry, profiles) -> None:
    """The outcome a clean frame is entitled to, written down before anyone tunes anything.

    The three thresholds had a positive control and no negative one; this is the negative one.
    Its job is to exist and be red, so that the cost of the current thresholds is visible and any
    future adjustment is measured against a frame that looks like the game rather than against a
    flat rectangle.
    """
    outcome = screening.screen_capture(
        _attempt(
            frame=_realistic_gameplay_frame(),
            window=GameWindow(
                handle=1,
                title="Sid Meier's Civilization VI",
                rect=WindowRect(0, 0, LIVE_WIDTH, LIVE_HEIGHT),
                pid=1234,
            ),
            platform="linux",
        ),
        registry=registry,
        profiles=profiles,
    )

    assert outcome.status is ScreeningStatus.SCREENED_CLEAN


def test_content_gate_withholds_on_declared_text_token_match(registry, profiles) -> None:
    outcome = screening.screen_capture(
        _attempt(detected_text_tokens=frozenset({"firetuner", "window"})),
        registry=registry,
        profiles=profiles,
    )

    assert outcome.status is ScreeningStatus.WITHHELD
    assert outcome.failed_gate is screening.ScreeningGate.CONTENT
    assert "firetuner_window" in (outcome.detail or "")


def test_content_gate_ignores_partial_keyword_matches(registry, profiles) -> None:
    """A single stray token must not be enough -- avoids trivial false positives."""
    outcome = screening.screen_capture(
        _attempt(detected_text_tokens=frozenset({"window"})),  # "firetuner" is missing
        registry=registry,
        profiles=profiles,
    )

    assert outcome.status is ScreeningStatus.SCREENED_CLEAN


def test_content_gate_fails_closed_when_frame_cannot_be_decoded(registry, profiles) -> None:
    undecodable = CaptureFrame(
        width=WIDTH,
        height=HEIGHT,
        rect=WindowRect(0, 0, WIDTH, HEIGHT),
        image_bytes=b"not an image",
        image_format="PNG",
    )

    outcome = screening.screen_capture(
        _attempt(frame=undecodable), registry=registry, profiles=profiles
    )

    assert outcome.status is ScreeningStatus.WITHHELD
    assert outcome.failed_gate is screening.ScreeningGate.CONTENT


def test_content_gate_withholds_when_no_text_evidence_source_ran(registry, profiles) -> None:
    """The live defect: ``detected_text_tokens=None`` means nothing enumerated windows.

    Most reject categories (``firetuner_window``, ``developer_console``, ``harness_owned_ui``,
    the per-platform taskbar/panel/dock ids) have exactly one technique -- declared-text
    matching -- and it cannot run without evidence. Until this test existed the gate asked those
    categories nothing, saw no match, and *passed the frame*. Passing an unexamined frame is the
    one thing Principle I forbids, so the correct outcome is withheld.
    """
    outcome = screening.screen_capture(
        _attempt(detected_text_tokens=None), registry=registry, profiles=profiles
    )

    assert outcome.status is ScreeningStatus.WITHHELD
    assert outcome.failed_gate is screening.ScreeningGate.CONTENT
    assert outcome.withheld_reason is WithheldReason.NON_PLAYER_UI
    assert "firetuner_window" in (outcome.detail or "")
    assert "no available screening technique" in (outcome.detail or "")


def test_the_dataclass_default_is_the_fail_closed_one(registry, profiles) -> None:
    """A caller that simply does not mention text evidence must get the withholding behaviour.

    The defect reached production through a permissive *default*, not through an explicit
    decision: ``detected_text_tokens`` defaulted to an empty frozenset, which the gate read as
    "enumerated, found nothing". Constructing an attempt without the argument at all is the
    production shape, and it must fail closed.
    """
    bare = screening.CaptureAttempt(
        frame=_solid_frame(),
        capture_path=CapturePath.WINDOWS_GRAPHICS_CAPTURE,
        window=_window(),
        view_declaration_id="views.world",
        camera_state={"mode": "world", "zoom": 0.5, "target_revealed": True},
        platform="windows",
        expected_process=_PROCESS,
    )
    assert bare.detected_text_tokens is None

    outcome = screening.screen_capture(bare, registry=registry, profiles=profiles)

    assert outcome.status is ScreeningStatus.WITHHELD
    assert outcome.failed_gate is screening.ScreeningGate.CONTENT


def test_content_gate_withholds_when_the_detector_cannot_declare_its_coverage(
    registry, profiles
) -> None:
    """An injected detector that only reports findings has not said what it examined."""

    class _FindingsOnlyDetector:
        def detect(self, frame, *, reject_categories, detected_text_tokens) -> frozenset[str]:
            return frozenset()

    outcome = screening.screen_capture(
        _attempt(),
        registry=registry,
        profiles=profiles,
        detector=_FindingsOnlyDetector(),  # type: ignore[arg-type]
    )

    assert outcome.status is ScreeningStatus.WITHHELD
    assert outcome.failed_gate is screening.ScreeningGate.CONTENT


def test_natural_saturated_background_does_not_trip_the_border_detector(registry, profiles) -> None:
    """A uniformly-coloured natural scene reaching every edge must never be flagged as a border."""
    outcome = screening.screen_capture(
        _attempt(frame=_solid_frame((20, 140, 40))), registry=registry, profiles=profiles
    )

    assert outcome.status is ScreeningStatus.SCREENED_CLEAN


# --------------------------------------------------------------------------
# Gate <-> WithheldReason mapping is exhaustive and one-to-one
# --------------------------------------------------------------------------


def test_every_gate_maps_to_a_distinct_withheld_reason() -> None:
    mapping = screening._GATE_WITHHELD_REASON  # noqa: SLF001 - whitebox check of an invariant
    assert set(mapping.keys()) == set(screening.ScreeningGate)
    assert len(set(mapping.values())) == len(WithheldReason)
    assert set(mapping.values()) == set(WithheldReason)


# --------------------------------------------------------------------------
# The core invariant: a withheld capture is never stored, never shown (SC-019)
# --------------------------------------------------------------------------


def test_withheld_outcome_is_never_stored_or_shown_even_if_caller_tries(registry, profiles) -> None:
    withheld_outcome = screening.screen_capture(
        _attempt(capture_path=CapturePath.NONE), registry=registry, profiles=profiles
    )
    assert withheld_outcome.status is ScreeningStatus.WITHHELD

    capture = screening.build_screen_capture(
        withheld_outcome,
        capture_id="cap_1",  # type: ignore[arg-type]
        run_id=RunId("run_1"),
        turn_number=1,
        decision_step_id=DecisionStepId("step_1"),
        captured_at="2026-09-20T00:00:00Z",  # type: ignore[arg-type]
        attempt=_attempt(capture_path=CapturePath.NONE),
        blob_ref="should-never-survive",
        shown_to_agent=True,
    )

    assert capture.blob_ref is None
    assert capture.shown_to_agent is False
    assert capture.screening_status is ScreeningStatus.WITHHELD
    assert capture.withheld_reason is WithheldReason.CAPTURE_FAILED


def test_screen_capture_model_itself_refuses_a_withheld_blob_as_a_backstop() -> None:
    """Independent of build_screen_capture: the model's own validator is a second gate."""
    from civsim_harness.models.turn import ScreenCapture

    with pytest.raises(ValidationError):
        ScreenCapture(
            capture_id="cap_1",  # type: ignore[arg-type]
            run_id="run_1",  # type: ignore[arg-type]
            turn_number=1,
            decision_step_id="step_1",  # type: ignore[arg-type]
            captured_at="2026-09-20T00:00:00Z",  # type: ignore[arg-type]
            camera_state={},
            view_declaration_id="views.world",  # type: ignore[arg-type]
            screening_status=ScreeningStatus.WITHHELD,
            withheld_reason=WithheldReason.CAPTURE_FAILED,
            shown_to_agent=False,
            retained_as_evidence=True,
            blob_ref="sneaked-in",
            capture_path=CapturePath.NONE,
        )


def test_screened_clean_capture_can_carry_a_blob_and_be_shown(registry, profiles) -> None:
    clean_outcome = screening.screen_capture(_attempt(), registry=registry, profiles=profiles)
    assert clean_outcome.status is ScreeningStatus.SCREENED_CLEAN

    capture = screening.build_screen_capture(
        clean_outcome,
        capture_id="cap_2",  # type: ignore[arg-type]
        run_id=RunId("run_1"),
        turn_number=1,
        decision_step_id=DecisionStepId("step_1"),
        captured_at="2026-09-20T00:00:00Z",  # type: ignore[arg-type]
        attempt=_attempt(),
        blob_ref="sha256:abc123",
        shown_to_agent=True,
    )

    assert capture.blob_ref == "sha256:abc123"
    assert capture.shown_to_agent is True
    assert capture.screening_status is ScreeningStatus.SCREENED_CLEAN
    assert capture.withheld_reason is None


# --------------------------------------------------------------------------
# Profile resolution: never a permissive default
# --------------------------------------------------------------------------


def test_profile_resolution_falls_back_to_default_for_unknown_platform(
    profiles: screening.ScreeningProfiles,
) -> None:
    resolved = screening.resolve_screening_profile(
        profiles, declared_profile_key="windows", platform="some_future_console_os"
    )

    assert resolved is profiles.profiles["default"]


def test_declared_default_profile_ignores_host_platform(
    profiles: screening.ScreeningProfiles,
) -> None:
    resolved = screening.resolve_screening_profile(
        profiles, declared_profile_key="default", platform="windows"
    )

    assert resolved is profiles.profiles["default"]


def test_default_profile_is_at_least_as_strict_as_every_named_profile(
    profiles: screening.ScreeningProfiles,
) -> None:
    default_reject = profiles.profiles["default"].reject
    for name, profile in profiles.profiles.items():
        if name == "default":
            continue
        assert profile.reject <= default_reject


# --------------------------------------------------------------------------
# screening_profiles.yaml loader validation
# --------------------------------------------------------------------------


def test_loader_rejects_a_profile_missing_the_default_key(tmp_path: Path) -> None:
    broken = tmp_path / "screening_profiles.yaml"
    broken.write_text(
        """
schema_version: 1
reject_definitions:
  firetuner_window: FireTuner.
universal_reject: [firetuner_window]
profiles:
  windows:
    description: windows
    reject: [firetuner_window]
""",
        encoding="utf-8",
    )

    with pytest.raises(CatalogError):
        screening.load_screening_profiles(broken)


def test_loader_rejects_a_default_profile_that_is_not_a_superset(tmp_path: Path) -> None:
    broken = tmp_path / "screening_profiles.yaml"
    broken.write_text(
        """
schema_version: 1
reject_definitions:
  firetuner_window: FireTuner.
  windows_taskbar: Taskbar.
universal_reject: [firetuner_window]
profiles:
  windows:
    description: windows
    reject: [firetuner_window, windows_taskbar]
  default:
    description: default
    reject: [firetuner_window]
""",
        encoding="utf-8",
    )

    with pytest.raises(CatalogError):
        screening.load_screening_profiles(broken)


def test_loader_rejects_an_undefined_reject_id(tmp_path: Path) -> None:
    broken = tmp_path / "screening_profiles.yaml"
    broken.write_text(
        """
schema_version: 1
reject_definitions:
  firetuner_window: FireTuner.
universal_reject: [firetuner_window]
profiles:
  default:
    description: default
    reject: [firetuner_window, some_undefined_id]
""",
        encoding="utf-8",
    )

    with pytest.raises(CatalogError):
        screening.load_screening_profiles(broken)


def test_the_real_repository_screening_profiles_file_loads_cleanly() -> None:
    loaded = screening.load_screening_profiles()

    assert "default" in loaded.profiles
    assert loaded.universal_reject <= loaded.profiles["default"].reject
