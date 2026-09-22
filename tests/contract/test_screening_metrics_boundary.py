"""Principle I: the screening statistics may describe the detector, never the frame or the desk.

T297, 2026-09-22. ``CaptureScreeningMetrics`` exists because every detector threshold in
``parity/screening.py`` is currently unfalsifiable: the three corner constants were introduced
with no recorded derivation, frame size or sample, and they cannot be derived now because all 421
withheld captures were stored with ``blob_ref = NULL``. We discarded exactly the evidence needed
to evaluate our own gate.

The fix is *not* to keep the withheld pixels. A withheld frame is the capture most likely to
contain the operator's desktop -- a FireTuner window, a notification toast, a panel -- which is
precisely the data T265 agreed to hash rather than keep; retaining it would convert a screening
gate into an archive of the thing the gate exists to suppress. So what is persisted is the
*derived statistics*: a handful of floats and some enums, computed at screening time, carrying no
image and unable to reconstruct one.

**This file exists because a docstring saying that is worth nothing.** It is built the way
``test_window_title_boundary.py`` is built -- two arms, the surface enumerated as data, every arm
with a negative control proving it can fire:

1. **Structural.** The exact key set of the record and of its two element types is asserted as a
   literal. A future author who widens the payload -- with a thumbnail, a token list, a per-region
   colour, a blob pointer -- gets a red test naming the field they added, rather than a silent
   Principle I regression dressed as a Principle III improvement. A second structural assertion
   bounds the payload's size and shows it does not grow with the pixel count, which is the
   information-theoretic form of "this cannot contain an image".

2. **Runtime.** The real production capture path is driven with a sentinel window title on the
   desktop and a real frame, and the metrics the path actually produces are scanned for the
   title, for its tokenised form, for the frame's bytes in base64, and for the frame's content
   hash. Both populations are driven, delivered and withheld, because the whole point of the
   record is that it exists on both.

The third group of tests is the ordering guarantee itself -- that the production path records
these at all -- and it is the negative control for the feature: comment the derivation out of
``_check_content`` and
``test_the_production_path_records_the_statistics_on_a_delivered_frame`` fails by name.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
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
    HudCorner,
    ParityDeclaration,
)
from civsim_harness.models.common import (
    CapabilityId,
    DecisionStepId,
    DeclarationId,
    LuaContext,
    RunId,
)
from civsim_harness.models.turn import (
    BorderEdgeMetric,
    CaptureScreeningMetrics,
    CornerVarianceMetric,
    ScreeningStatus,
    ScreeningTechnique,
)
from civsim_harness.observe.capture import capture_for_step
from civsim_harness.parity import screening
from civsim_harness.parity.screening import load_screening_profiles
from fakes.fake_host import DEFAULT_PROCESS, FakeHostPlatform

# --------------------------------------------------------------------------
# Arm 1: the structural boundary -- what may be in the record, stated as data
# --------------------------------------------------------------------------

#: The complete, exhaustive key set of the persisted statistics record. Written out as a literal,
#: not derived from the model, because a derived expectation cannot fail: the point is that
#: *adding a field is a decision someone has to come here and make*, in a file that says what may
#: and may not go in one.
_EXPECTED_METRIC_KEYS = frozenset(
    {
        "profile_name",
        "frame_width",
        "frame_height",
        "whole_frame_variance",
        "corner_metrics",
        "border_edge_metrics",
        "text_evidence_available",
        "text_match",
        "techniques_run",
        "techniques_fired",
        "reject_categories_matched",
    }
)

_EXPECTED_CORNER_KEYS = frozenset(
    {"corner", "variance", "reference_variance", "reference_is_hud_peer"}
)

_EXPECTED_BORDER_EDGE_KEYS = frozenset({"edge", "uniformity_stddev", "interior_color_delta"})

_WIDENING_HINT = (
    "A new key on the screening-statistics record is a Principle I decision, not a schema "
    "detail. Nothing here may describe the frame's contents (a thumbnail, a downsample, a "
    "per-region colour, a histogram, a blob pointer) or the operator's desktop (a window title, "
    "a matched token, an OCR string). The record describes the DETECTOR: what it compared, "
    "against what reference, and what it concluded. If the new field is genuinely one of those, "
    "add it here and say why in the model's docstring."
)


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        pytest.param(CaptureScreeningMetrics, _EXPECTED_METRIC_KEYS, id="CaptureScreeningMetrics"),
        pytest.param(CornerVarianceMetric, _EXPECTED_CORNER_KEYS, id="CornerVarianceMetric"),
        pytest.param(BorderEdgeMetric, _EXPECTED_BORDER_EDGE_KEYS, id="BorderEdgeMetric"),
    ],
)
def test_the_recorded_key_set_is_exactly_this_and_nothing_else(
    model: type, expected: frozenset[str]
) -> None:
    actual = frozenset(model.model_fields)
    assert actual == expected, (
        f"{model.__name__}'s persisted key set changed: added {sorted(actual - expected)}, "
        f"removed {sorted(expected - actual)}. {_WIDENING_HINT}"
    )


def test_the_record_forbids_extra_keys_so_the_key_set_is_a_boundary_not_a_suggestion() -> None:
    """``extra='forbid'`` is what makes the assertion above a boundary at runtime too.

    Without it a caller could hand ``model_validate`` a dict with a ``thumbnail`` key and pydantic
    would quietly drop it -- or, worse, a future ``extra='allow'`` would quietly keep it, and the
    key-set test above would still pass because ``model_fields`` does not list extras.
    """
    for model in (CaptureScreeningMetrics, CornerVarianceMetric, BorderEdgeMetric):
        assert model.model_config.get("extra") == "forbid", model.__name__

    with pytest.raises(ValueError, match="thumbnail"):
        CaptureScreeningMetrics.model_validate(
            {
                "profile_name": "linux",
                "frame_width": 8,
                "frame_height": 8,
                "text_evidence_available": True,
                "text_match": False,
                "thumbnail": "iVBORw0KGgo=",
            }
        )


@pytest.mark.parametrize(("width", "height"), [(64, 64), (1920, 1200)])
def test_the_payload_does_not_grow_with_the_pixel_count(width: int, height: int) -> None:
    """The information-theoretic half of "it cannot reconstruct an image".

    A 1920x1200 RGB frame is 6.9 MB of pixels. Whatever this record is, it is a fixed handful of
    scalars: the serialised payload for the large frame is within a few bytes of the small one
    (the difference is the digits in ``frame_width``/``frame_height``), and far below any size at
    which an image could hide. A future field that embedded even a 16x16 downsample would blow
    this bound, which is the point of asserting it rather than asserting a comment.
    """
    statistics = screening.derive_frame_statistics(
        _frame(width=width, height=height), hud_corners=frozenset(HudCorner)
    )
    metrics = screening._content_metrics(  # noqa: SLF001
        profile_name="linux",
        statistics=statistics,
        text_evidence_available=True,
        techniques_run=frozenset(ScreeningTechnique),
        techniques_fired=frozenset({ScreeningTechnique.CORNER_OVERLAY}),
        matches=frozenset({"linux_recording_overlay"}),
    )
    payload = metrics.model_dump_json()

    assert len(payload) < 2048, (
        f"the screening-statistics payload for a {width}x{height} frame is {len(payload)} bytes. "
        "It is meant to be a handful of floats and an enum; anything approaching image-sized is "
        f"the regression this record exists to prevent. {_WIDENING_HINT}"
    )
    assert len(metrics.corner_metrics) == 4
    assert len(metrics.border_edge_metrics) == 4


def test_the_recorded_statistics_are_the_ones_the_verdict_was_computed_from() -> None:
    """Derived-at-screening-time is worth nothing if the record is a second derivation.

    The verdicts are properties over the persisted numbers, so this asserts the identity directly:
    re-applying the thresholds to what was stored reproduces what the gate decided.
    """
    statistics = screening.derive_frame_statistics(
        _overlaid_frame(), hud_corners=frozenset(HudCorner)
    )

    assert statistics.corner_overlay_is_suspect is screening._corner_overlay_verdict(  # noqa: SLF001
        statistics.corners
    )
    assert statistics.border_ring_is_suspect is screening._border_ring_verdict(  # noqa: SLF001
        statistics.border_edges
    )
    # And it is the *same* answer the public technique gives on the same frame.
    image = PILImage.open(io.BytesIO(_overlaid_frame().image_bytes)).convert("RGB")
    assert statistics.corner_overlay_is_suspect is screening._corner_overlay_is_suspect(  # noqa: SLF001
        image, hud_corners=frozenset(HudCorner)
    )


def test_every_corner_is_recorded_including_the_ones_the_floor_discards() -> None:
    """A distribution of only the values that cleared a threshold cannot evaluate that threshold.

    This is the specific mistake the task exists to stop repeating, so it gets its own assertion:
    a flat frame's corners are all far below ``_CORNER_VARIANCE_ABS_MIN`` and every one of them is
    still recorded, with the reference it would have been compared against.
    """
    statistics = screening.derive_frame_statistics(_frame(color=(31, 61, 97)))

    assert {metric.corner for metric in statistics.corners} == set(HudCorner)
    assert all(
        metric.variance < screening._CORNER_VARIANCE_ABS_MIN  # noqa: SLF001
        for metric in statistics.corners
    )
    assert statistics.corner_overlay_is_suspect is False


def test_a_declared_hud_corner_records_which_reference_it_was_judged_against() -> None:
    """T283's two comparisons are distinguishable in the record, or the distribution is a mixture.

    The corner/whole-frame ratio and the corner/HUD-peer ratio are different quantities against
    the same threshold. Pooling them would produce exactly the uninterpretable band this task is
    meant to replace, so which one was used is recorded per corner.
    """
    declared = frozenset({HudCorner.TOP_LEFT, HudCorner.TOP_RIGHT})
    statistics = screening.derive_frame_statistics(_overlaid_frame(), hud_corners=declared)

    by_corner = {metric.corner: metric for metric in statistics.corners}
    assert by_corner[HudCorner.TOP_LEFT].reference_is_hud_peer is True
    assert by_corner[HudCorner.BOTTOM_RIGHT].reference_is_hud_peer is False
    assert by_corner[HudCorner.BOTTOM_RIGHT].reference_variance == pytest.approx(
        statistics.whole_frame_variance
    )
    assert by_corner[HudCorner.TOP_LEFT].reference_variance == pytest.approx(
        by_corner[HudCorner.TOP_RIGHT].variance
    )


def test_an_undecodable_frame_records_that_no_statistic_exists_rather_than_inventing_one() -> None:
    """Research R7's withhold must not be recorded as "measured and found clean"."""
    statistics = screening.derive_frame_statistics(
        CaptureFrame(
            width=8,
            height=8,
            rect=WindowRect(left=0, top=0, width=8, height=8),
            image_bytes=b"not an image",
            image_format="PNG",
        )
    )

    assert statistics.decoded is False
    assert statistics.whole_frame_variance is None
    assert statistics.corners == ()
    assert statistics.border_edges == ()


# --------------------------------------------------------------------------
# Arm 2: the runtime boundary -- driven through the real production path
# --------------------------------------------------------------------------

#: A title no other string in this repo could produce by accident, shaped like the thing the
#: boundary protects: a private window nobody should ever find in a run record. One word, so its
#: raw and tokenised forms are the same string and the scan catches a leaked token too.
_SENTINEL_TITLE = "zqxprivatebanking7f3a"

VIEW_DECLARATION_ID = DeclarationId("views.world")

_WINDOW = GameWindow(
    handle=1,
    title="Sid Meier's Civilization VI (FAKE)",
    rect=WindowRect(left=0, top=0, width=64, height=64),
    pid=DEFAULT_PROCESS.pid,
)
_HOST_INFO = HostInfo(
    os=OperatingSystem.linux, os_version="test", session_type=LinuxSessionType.x11
)


def _frame(*, width: int = 64, height: int = 64, color: tuple[int, int, int] = (12, 44, 92)):
    image = PILImage.new("RGB", (width, height), color)
    return _frame_from(image, width=width, height=height)


def _overlaid_frame() -> CaptureFrame:
    """A frame with one busy corner -- the shape the corner technique is meant to catch."""
    image = PILImage.new("RGB", (64, 64), (12, 44, 92))
    for x in range(0, 16):
        for y in range(0, 16):
            image.putpixel((x, y), ((x * 37) % 256, (y * 91) % 256, ((x + y) * 53) % 256))
    return _frame_from(image, width=64, height=64)


def _frame_from(image: PILImage.Image, *, width: int, height: int) -> CaptureFrame:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return CaptureFrame(
        width=width,
        height=height,
        rect=WindowRect(left=0, top=0, width=width, height=height),
        image_bytes=buffer.getvalue(),
        image_format="PNG",
    )


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
        # "platform", as every shipped view declares: the host's own profile, which on this
        # linux/XComposite fixture is the one platform with a measured capture scope.
        screening_profile="platform",
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


def _drive_production_path(desktop: list[str], frame: CaptureFrame):
    host = FakeHostPlatform()
    host.set_window_titles(desktop)
    host.set_capture_result(CaptureResult(status=CaptureStatus.ok, frame=frame))
    # Exactly the expression run/decision_loop.py uses -- not a re-derivation of it.
    detected_text_tokens = host.list_window_titles().text_tokens()
    return (
        capture_for_step(
            host=host,
            host_info=_HOST_INFO,
            window=_WINDOW,
            view_declaration_id=VIEW_DECLARATION_ID,
            camera_state={"mode": "world", "zoom": 0.5},
            run_id=RunId("run-metrics-boundary"),
            turn_number=3,
            decision_step_id=DecisionStepId("step-7"),
            step_index=7,
            captured_at=datetime(2026, 9, 22, tzinfo=UTC),
            registry=_registry_with_view(),
            profiles=load_screening_profiles(),
            detected_text_tokens=detected_text_tokens,
        ),
        detected_text_tokens,
    )


def _leak_probes(frame: CaptureFrame) -> dict[str, str]:
    """Literal values that must never appear in the payload, each named for the failure message."""
    encoded = base64.b64encode(frame.image_bytes).decode("ascii")
    return {
        "the operator's window title": _SENTINEL_TITLE,
        "the frame's content hash": hashlib.sha256(frame.image_bytes).hexdigest(),
        "the frame's bytes, base64-encoded": encoded[: min(len(encoded), 40)],
    }


def _scan(payload: str, frame: CaptureFrame) -> list[str]:
    return [name for name, needle in _leak_probes(frame).items() if needle in payload]


def test_negative_control_the_leak_scan_finds_a_planted_value() -> None:
    """The control arm 2 had to pass before it could ship.

    The realistic leak is a helpful free-text field -- "withheld because the desktop showed X" --
    so the control plants the sentinel in the one string field the record has, and the scan must
    name it. A scan that could not find this one would pass the real test forever for the wrong
    reason.
    """
    frame = _frame()
    planted = CaptureScreeningMetrics(
        profile_name=f"linux (matched {_SENTINEL_TITLE})",
        frame_width=64,
        frame_height=64,
        text_evidence_available=True,
        text_match=True,
    )

    assert _scan(planted.model_dump_json(), frame) == ["the operator's window title"]


def test_negative_control_the_leak_scan_finds_a_planted_frame_hash() -> None:
    """The second shape: a "pointer back to the pixels" smuggled in as a content hash.

    A withheld frame's blob is never stored, so a hash of it in the statistics would be a
    dangling reference -- but a *delivered* frame's would be a live one, and either way the
    record's job is to describe the detector, not to index the image.
    """
    frame = _frame()
    planted = CaptureScreeningMetrics(
        profile_name=hashlib.sha256(frame.image_bytes).hexdigest(),
        frame_width=64,
        frame_height=64,
        text_evidence_available=True,
        text_match=False,
    )

    assert _scan(planted.model_dump_json(), frame) == ["the frame's content hash"]


def test_negative_control_the_leak_scan_is_selective() -> None:
    """The positive twin, so the controls above are not simply always-true."""
    frame = _frame()
    clean = CaptureScreeningMetrics(
        profile_name="linux",
        frame_width=64,
        frame_height=64,
        text_evidence_available=True,
        text_match=False,
    )
    assert _scan(clean.model_dump_json(), frame) == []


@pytest.mark.parametrize(
    "desktop",
    [
        pytest.param([_WINDOW.title, _SENTINEL_TITLE], id="clean-frame-delivered"),
        pytest.param(
            [_WINDOW.title, _SENTINEL_TITLE, "Developer Console"], id="frame-withheld-by-the-gate"
        ),
    ],
)
def test_no_private_or_image_derived_value_reaches_the_recorded_statistics(
    desktop: list[str],
) -> None:
    """The runtime half, on both populations, through the real production capture path.

    Both outcomes are driven because the record exists on both, and because the *withheld* side is
    where a leak is most tempting: that is the path with something to explain.
    """
    frame = _frame()
    result, tokens = _drive_production_path(desktop, frame)

    assert tokens is not None and _SENTINEL_TITLE in tokens, (
        "the sentinel is not even in the evidence, so this test would pass without proving "
        "anything about where the evidence can travel"
    )
    metrics = result.capture.screening_metrics
    assert metrics is not None, "no statistics were recorded, so this scan proves nothing"

    leaks = _scan(metrics.model_dump_json(), frame)
    assert leaks == [], (
        f"the screening statistics carry {leaks}. They may describe what the detector compared "
        "and what it concluded; they may never carry the operator's desktop, the frame's pixels, "
        "or a pointer to them."
    )

    # Belt and braces: whatever the field-by-field scan might miss, the JSON the store writes
    # does not -- and the store writes the whole capture record, statistics included. The one
    # exemption is ``blob_ref``, which on a delivered frame *is* the frame's content hash by
    # design: it is the content address of a blob the run deliberately kept, and SC-019 already
    # forces it to null on the withheld side. The point of this probe is that no *other* field --
    # and above all no field inside the new statistics -- may carry that value.
    record = result.capture.model_dump(mode="json")
    record.pop("blob_ref")
    assert _scan(json.dumps(record), frame) == [], (
        "a probe value reached the capture record the store persists"
    )


def test_the_text_verdict_is_recorded_as_a_boolean_and_the_tokens_are_not() -> None:
    """The declared-text technique's evidence is the one input that is categorically private.

    ``detected_text_tokens`` comes from enumerating the operator's whole desktop. The distribution
    only ever needed to know *whether* a category matched, so that is all there is a field for --
    and this asserts the true-valued case, where a leak would actually have something to leak.
    """
    result, _ = _drive_production_path(
        [_WINDOW.title, _SENTINEL_TITLE, "Developer Console"], _frame()
    )
    metrics = result.capture.screening_metrics
    assert metrics is not None

    assert metrics.text_match is True
    assert metrics.text_evidence_available is True
    assert ScreeningTechnique.DECLARED_TEXT in metrics.techniques_fired
    assert isinstance(metrics.text_match, bool)
    # The categories are catalog ids, which is what "attributed to" means; the tokens are not.
    assert metrics.reject_categories_matched
    assert all(_SENTINEL_TITLE not in category for category in metrics.reject_categories_matched)


# --------------------------------------------------------------------------
# The ordering guarantee: the production path records these, on both populations
# --------------------------------------------------------------------------


def _assert_full_statistics(metrics: CaptureScreeningMetrics | None) -> CaptureScreeningMetrics:
    assert metrics is not None, (
        "the production capture path recorded no screening statistics. They cannot be recovered "
        "afterwards -- a withheld frame is never stored and must not be -- so a capture that "
        "reached the content gate without them is evidence destroyed, which is the whole defect "
        "T297 exists to close."
    )
    assert metrics.whole_frame_variance is not None
    assert len(metrics.corner_metrics) == 4
    assert len(metrics.border_edge_metrics) == 4
    assert metrics.frame_width == 64
    assert metrics.frame_height == 64
    assert metrics.profile_name
    return metrics


def test_the_production_path_records_the_statistics_on_a_delivered_frame() -> None:
    """The clean population. A detector is only evaluable against both of them.

    The retro-audit's 188 delivered frames are the one real clean sample this project has, and
    they exist only as prose in a spike document because nothing ever persisted the numbers.
    """
    result, _ = _drive_production_path([_WINDOW.title], _frame())

    assert result.capture.screening_status is ScreeningStatus.SCREENED_CLEAN, (
        f"expected a delivered frame for this fixture; got {result.capture.withheld_reason}"
    )
    metrics = _assert_full_statistics(result.capture.screening_metrics)
    assert metrics.reject_categories_matched == []
    assert metrics.techniques_fired == []
    assert metrics.techniques_run, "a clean verdict with no technique run is not a clean verdict"


def test_the_production_path_records_the_statistics_on_a_withheld_frame() -> None:
    """The withheld population -- the 421 captures whose numbers were lost.

    Note what is asserted alongside: the record still has no blob (SC-019 is untouched). The
    statistics are what replaces the pixels, not a way to smuggle them back.
    """
    result, _ = _drive_production_path(
        [_WINDOW.title, _SENTINEL_TITLE, "Developer Console"], _frame()
    )

    assert result.capture.screening_status is ScreeningStatus.WITHHELD
    assert result.capture.blob_ref is None
    assert result.capture.shown_to_agent is False
    metrics = _assert_full_statistics(result.capture.screening_metrics)
    assert metrics.reject_categories_matched
    assert metrics.techniques_fired


def test_the_statistics_survive_a_store_round_trip() -> None:
    """The record is only evidence if it is still there when the run is read back.

    ``captures`` stores the whole record as ``capture_json``, so this is a serialisation check
    rather than a DDL one -- which is exactly why it needs asserting: nothing about the table
    would have complained if the field silently failed to round-trip.
    """
    result, _ = _drive_production_path([_WINDOW.title], _frame())
    original = _assert_full_statistics(result.capture.screening_metrics)

    revived = type(result.capture).model_validate_json(result.capture.model_dump_json())

    assert revived.screening_metrics == original


def test_the_technique_vocabulary_is_one_enum_not_two() -> None:
    """``parity.screening`` re-exports the persisted enum rather than defining a parallel one.

    Two enums with the same members is how a stored vocabulary and a running one drift apart
    without anything failing until a reader tries to interpret old records.
    """
    assert screening.ScreeningTechnique is ScreeningTechnique


def test_the_metrics_record_refuses_a_verdict_it_could_not_have_reached() -> None:
    """Two invariants the record enforces on itself, independently of who built it."""
    base: dict[str, Any] = {
        "profile_name": "linux",
        "frame_width": 64,
        "frame_height": 64,
        "text_evidence_available": False,
        "text_match": False,
    }

    with pytest.raises(ValueError, match="fired but did not run"):
        CaptureScreeningMetrics.model_validate(
            {**base, "techniques_fired": [ScreeningTechnique.BORDER_RING]}
        )

    with pytest.raises(ValueError, match="no text-evidence source ran"):
        CaptureScreeningMetrics.model_validate({**base, "text_match": True})
