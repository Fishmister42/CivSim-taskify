"""The four image-screening gates (T129, research R7).

Every capture is checked through four independent gates, in order, before it
may be certified :attr:`~civsim_harness.models.turn.ScreeningStatus.SCREENED_CLEAN`.
**Any gate failing means withhold and re-capture** -- this module never
partially passes a frame, never edits pixels out of it, and never shows a
frame it could not certify:

- **source** -- the frame came from the declared game-window capture item --
  withheld as ``WithheldReason.CAPTURE_FAILED``.
- **geometry** -- frame dimensions match the client rect within tolerance --
  withheld as ``WithheldReason.GEOMETRY_MISMATCH``.
- **provenance** -- tagged with the declared view capability and camera
  state -- withheld as ``WithheldReason.PROVENANCE_FAILURE``.
- **content** -- a catalogs/screening_profiles.yaml-driven chrome detector --
  withheld as ``WithheldReason.NON_PLAYER_UI``.

That right-hand column is a deliberate, exhaustive one-to-one mapping onto
:class:`~civsim_harness.models.turn.WithheldReason`'s exactly four members --
there is no fifth "generic" reason to reach for, and no gate shares a reason
with another, so a withheld :class:`~civsim_harness.models.turn.ScreenCapture`
always names *which* of the four defences actually caught the problem.

**Why source and geometry are checked before provenance and content.**
:class:`~civsim_harness.host.port.HostPlatform` exposes exactly one capture
method, :meth:`~civsim_harness.host.port.HostPlatform.capture_window`, which
can only ever target one declared :class:`~civsim_harness.host.port.GameWindow`
-- there is no sibling method for a desktop or region grab anywhere on that
port. The *source* gate here is therefore not re-implementing occlusion
detection (that is the host adapter's job, research R6); it is the
belt-and-braces check that the attempt in hand really used that one
legitimate path (a real ``capture_path``, a window with a plausible
identity, optionally cross-checked against the run's own located game
process) rather than, say, a stale placeholder slipping through a bug
upstream. *Geometry* then confirms the frame that came back actually matches
that window's own rect -- catching a wrong-target capture or a stale
surface before any more expensive check runs.

**Why content is last, and why it exists at all given the above.** Source,
geometry, and provenance make contamination *structurally unlikely*; content
is what catches what they miss (research R7's own framing) -- most notably
FireTuner, a console, or a harness dialog that a compositor placed *inside*
the window's own composited surface in a way none of the first three checks
can see, since they only reason about which window/rect/declaration produced
the frame, never what is actually drawn inside it.

**The live finding this module is built to handle**: a real Civilization VI
client on Linux renders a 60 FPS overlay *inside the game's own window
frame* -- window-scoped capture cannot exclude it the way it excludes a
taskbar, because the game itself composites it into the same surface being
captured. Nothing in this module hardcodes that specific overlay (no
"60 FPS", no fixed text, no fixed colour): :class:`DefaultContentDetector`'s
corner-variance technique is a *general* capability for catching a small,
anomalously busy patch drawn inside an otherwise natural frame, driven
entirely by which reject categories a resolved :class:`ScreeningProfile`
actually names (read from ``catalogs/screening_profiles.yaml``, never edited
by this module) -- the same technique would catch a differently-worded
counter, a debug HUD, or any other in-frame chrome the profile data assigns
to a matching category id.

**The content gate withholds what it cannot screen, not only what it catches.**
A profile names reject categories; the detector implements techniques; the two
are related by :func:`techniques_for_category`, published as data. Before any
finding is consulted, :func:`_check_content` asks the detector which of the
profile's categories it can actually decide *on this attempt*, and withholds if
any is left over. This matters because the techniques are unequal: the
image-based ones always run, but declared-text matching only runs when the
caller gathered text evidence, and most categories (``firetuner_window``,
``developer_console``, ``linux_panel``, ...) have no other technique. With no
text evidence the old code asked those categories nothing, got no match, and
passed the frame -- a gate that failed open exactly where it was most needed.
It now fails closed, which is the only outcome Principle I permits: a capture
that cannot be proven clean is withheld.

**Profile resolution never falls back to a permissive default.**
:func:`resolve_screening_profile` implements ``screening_profiles.yaml``'s
own resolution rule exactly: a view's declared ``screening_profile`` of
``"default"`` always means the strictest profile; anything else resolves by
the running host's platform identifier, and a platform with no dedicated
entry -- including one this codebase does not yet know the name of --
resolves to that same strictest profile, never to an absent check.
"""

from __future__ import annotations

import io
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Protocol

import yaml
from PIL import Image, ImageStat

from civsim_harness.capability.registry import CapabilityRegistry
from civsim_harness.errors import CatalogError
from civsim_harness.host.port import CaptureFrame, GameProcess, GameWindow
from civsim_harness.models.catalog import DeclarationKind, ParityDeclaration
from civsim_harness.models.common import (
    CaptureId,
    CapturePath,
    DecisionStepId,
    DeclarationId,
    RunId,
    Timestamp,
)
from civsim_harness.models.turn import ScreenCapture, ScreeningStatus, WithheldReason

# --------------------------------------------------------------------------
# screening_profiles.yaml loading (data this module reads, never edits)
# --------------------------------------------------------------------------

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
DEFAULT_SCREENING_PROFILES_PATH: Final[Path] = _REPO_ROOT / "catalogs" / "screening_profiles.yaml"
DEFAULT_PROFILE_KEY: Final[str] = "default"


@dataclass(frozen=True)
class ScreeningProfile:
    """One named profile's reject set (``catalogs/screening_profiles.yaml``)."""

    name: str
    description: str
    reject: frozenset[str]


@dataclass(frozen=True)
class ScreeningProfiles:
    """The fully loaded, structurally validated ``screening_profiles.yaml``."""

    schema_version: int
    reject_definitions: Mapping[str, str]
    universal_reject: frozenset[str]
    profiles: Mapping[str, ScreeningProfile]


def load_screening_profiles(
    path: Path | str = DEFAULT_SCREENING_PROFILES_PATH,
) -> ScreeningProfiles:
    """Load and validate ``screening_profiles.yaml``.

    Raises :class:`~civsim_harness.errors.CatalogError` (this is catalog
    data, so it fails the same way any other malformed catalog file does) if
    the file is missing/malformed, a profile references an undefined reject
    id, there is no ``default`` profile, or ``default`` is not at least as
    strict as every named profile and the universal reject set -- the last
    check is a structural verification of the invariant the file's own
    header comment claims to hold, rather than trusting the comment alone.
    """
    resolved_path = Path(path)
    try:
        raw_text = resolved_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CatalogError(
            "screening_profiles.yaml could not be read", detail={"path": str(resolved_path)}
        ) from exc

    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise CatalogError(
            "screening_profiles.yaml is not valid YAML", detail={"path": str(resolved_path)}
        ) from exc

    if not isinstance(data, Mapping):
        raise CatalogError(
            "screening_profiles.yaml must contain a YAML mapping",
            detail={"path": str(resolved_path)},
        )

    reject_definitions_raw = data.get("reject_definitions")
    if not isinstance(reject_definitions_raw, Mapping) or not reject_definitions_raw:
        raise CatalogError(
            "screening_profiles.yaml is missing a non-empty reject_definitions mapping",
            detail={"path": str(resolved_path)},
        )
    known_ids = frozenset(str(key) for key in reject_definitions_raw)

    universal_reject = _validated_reject_ids(
        data.get("universal_reject") or [],
        known_ids,
        path=resolved_path,
        field="universal_reject",
    )

    profiles_raw = data.get("profiles")
    if not isinstance(profiles_raw, Mapping) or not profiles_raw:
        raise CatalogError(
            "screening_profiles.yaml has no profiles", detail={"path": str(resolved_path)}
        )

    profiles: dict[str, ScreeningProfile] = {}
    for name, body in profiles_raw.items():
        if not isinstance(body, Mapping):
            raise CatalogError(
                "a screening profile entry must be a mapping",
                detail={"path": str(resolved_path), "profile": str(name)},
            )
        reject = _validated_reject_ids(
            body.get("reject") or [],
            known_ids,
            path=resolved_path,
            field=f"profiles.{name}.reject",
        )
        profiles[str(name)] = ScreeningProfile(
            name=str(name),
            description=str(body.get("description", "")),
            reject=reject,
        )

    if DEFAULT_PROFILE_KEY not in profiles:
        raise CatalogError(
            f"screening_profiles.yaml has no {DEFAULT_PROFILE_KEY!r} profile "
            "(the strictest-available fallback is mandatory)",
            detail={"path": str(resolved_path)},
        )

    default_reject = profiles[DEFAULT_PROFILE_KEY].reject
    for name, profile in profiles.items():
        if name == DEFAULT_PROFILE_KEY:
            continue
        if not profile.reject <= default_reject:
            raise CatalogError(
                "the default screening profile must be a superset of every named profile's "
                "reject set (it must always be at least as strict)",
                detail={
                    "path": str(resolved_path),
                    "profile": name,
                    "not_covered_by_default": sorted(profile.reject - default_reject),
                },
            )
    if not universal_reject <= default_reject:
        raise CatalogError(
            "the default screening profile must cover the universal_reject set",
            detail={
                "path": str(resolved_path),
                "not_covered_by_default": sorted(universal_reject - default_reject),
            },
        )

    try:
        schema_version = int(data.get("schema_version", 1))
    except (TypeError, ValueError) as exc:
        raise CatalogError(
            "screening_profiles.yaml schema_version must be an integer",
            detail={"path": str(resolved_path)},
        ) from exc

    return ScreeningProfiles(
        schema_version=schema_version,
        reject_definitions=MappingProxyType(
            {str(key): str(value) for key, value in reject_definitions_raw.items()}
        ),
        universal_reject=universal_reject,
        profiles=MappingProxyType(profiles),
    )


def _validated_reject_ids(
    raw: Any, known_ids: frozenset[str], *, path: Path, field: str
) -> frozenset[str]:
    if not isinstance(raw, list):
        raise CatalogError(
            f"{field} must be a YAML list", detail={"path": str(path), "field": field}
        )
    ids = frozenset(str(item) for item in raw)
    unknown = ids - known_ids
    if unknown:
        raise CatalogError(
            f"{field} references reject id(s) not defined in reject_definitions",
            detail={"path": str(path), "field": field, "unknown": sorted(unknown)},
        )
    return ids


def resolve_screening_profile(
    profiles: ScreeningProfiles,
    *,
    declared_profile_key: str,
    platform: str,
) -> ScreeningProfile:
    """Resolve which :class:`ScreeningProfile` applies (screening_profiles.yaml's own rule).

    ``declared_profile_key == "default"`` always resolves to the strictest
    profile regardless of *platform*. Otherwise resolution is keyed on
    *platform*; a platform with no dedicated entry resolves to the strictest
    profile too -- never to an absent check or a permissive default.
    """
    if declared_profile_key == DEFAULT_PROFILE_KEY:
        return profiles.profiles[DEFAULT_PROFILE_KEY]
    return profiles.profiles.get(platform, profiles.profiles[DEFAULT_PROFILE_KEY])


# --------------------------------------------------------------------------
# What one screening attempt needs (T133's construction site)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CaptureAttempt:
    """Everything the four gates need to evaluate one capture (T129's input shape).

    Deliberately shaped to match what ``observe/capture.py``'s
    ``capture_for_step`` already has on hand at its "flip point" (see that
    module's own docstring): a successful :class:`~civsim_harness.host.port.
    CaptureResult`'s frame, the :class:`~civsim_harness.host.port.CapturePath`
    and :class:`~civsim_harness.host.port.GameWindow` that produced it, the
    view/camera-state pair the step requested, and the host platform
    identifier for profile resolution.

    ``detected_text_tokens`` states what a text-evidence source (window-title
    enumeration, OCR) found, and **``None`` means no such source ran** --
    which is emphatically not the same as an empty set, and is the distinction
    the gate needs to tell "checked, nothing there" from "never checked". With
    ``None``, every reject category whose only technique is declared-text
    matching is *unaddressed*, and the content gate withholds the frame
    (:func:`unaddressed_reject_categories`). It does not skip the check and
    pass; a check that did not run clears nothing.

    ``camera_state`` is expected to carry ``"mode"`` and ``"zoom"`` (as
    every view's ``output_schema`` in ``catalogs/observations/views.yaml``
    already declares), and ``"target_revealed"`` -- a bool confirming the
    camera's target plot is one the run has revealed -- whenever the
    resolved view's ``camera_requirements.target_must_be_revealed`` is
    ``True``. A camera state missing that confirmation is treated as
    *not* revealed (fails closed; FR-026 permits no permissive default).
    """

    frame: CaptureFrame
    capture_path: CapturePath
    window: GameWindow
    view_declaration_id: DeclarationId
    camera_state: Mapping[str, Any]
    platform: str
    expected_process: GameProcess | None = None
    detected_text_tokens: frozenset[str] | None = None
    geometry_tolerance: float = 0.02


# --------------------------------------------------------------------------
# Gate 1 -- source
# --------------------------------------------------------------------------


def _check_source(attempt: CaptureAttempt) -> str | None:
    if attempt.capture_path is CapturePath.NONE:
        return "capture_path is NONE; no legitimate window-capture mechanism was used"
    if attempt.window.pid <= 0 or not attempt.window.title:
        return "the declared game window carries no plausible process/title identity"
    if (
        attempt.expected_process is not None
        and attempt.window.pid != attempt.expected_process.pid
    ):
        return (
            "the declared game window's pid does not match the run's own located game process"
        )
    return None


# --------------------------------------------------------------------------
# Gate 2 -- geometry
# --------------------------------------------------------------------------


def _within_tolerance(actual: int, expected: int, tolerance: float) -> bool:
    if expected <= 0:
        return actual == expected
    return abs(actual - expected) <= tolerance * expected


def _check_geometry(attempt: CaptureAttempt) -> str | None:
    frame, rect, tolerance = attempt.frame, attempt.window.rect, attempt.geometry_tolerance

    if not _within_tolerance(frame.width, rect.width, tolerance):
        return f"frame width {frame.width} does not match the client rect width {rect.width}"
    if not _within_tolerance(frame.height, rect.height, tolerance):
        return f"frame height {frame.height} does not match the client rect height {rect.height}"
    if not _within_tolerance(frame.rect.width, rect.width, tolerance) or not _within_tolerance(
        frame.rect.height, rect.height, tolerance
    ):
        return "the frame's own reported rect does not match the declared client rect"
    return None


# --------------------------------------------------------------------------
# Gate 3 -- provenance
# --------------------------------------------------------------------------


def _unrevealed_target_detail(camera_state: Mapping[str, Any]) -> str:
    """The provenance gate's reason for an unconfirmed reveal, as specific as the state allows.

    Three shapes, in order of how much the camera state actually knows (T260):
    the Lua's own ``target_unavailable_reason`` (which accessor failed), a look-at plot that
    resolved but is not revealed to this player, or -- when neither is present -- the original
    bare statement. Only the wording varies; the capture is withheld in every case.
    """
    base = "the view requires a revealed target plot, and none was confirmed revealed"
    reason = camera_state.get("target_unavailable_reason")
    if isinstance(reason, str) and reason:
        return f"{base}: {reason}"
    plot = camera_state.get("target_plot")
    if isinstance(plot, Mapping):
        x, y = plot.get("x"), plot.get("y")
        if x is not None and y is not None:
            return (
                f"{base}: the camera is looking at plot ({x}, {y}), "
                "which this player has not revealed"
            )
    return base


def _check_provenance(
    attempt: CaptureAttempt, *, registry: CapabilityRegistry
) -> tuple[ParityDeclaration | None, str | None]:
    try:
        declaration = registry.resolve(attempt.view_declaration_id)
    except CatalogError as exc:
        return None, f"view_declaration_id does not resolve in the loaded catalog: {exc.message}"

    if declaration.kind is not DeclarationKind.VIEW:
        return None, "declaration_id does not name a view (FR-024)"

    requirements = declaration.camera_requirements
    assert requirements is not None  # guaranteed for kind == view (ParityDeclaration validator)

    mode = attempt.camera_state.get("mode")
    if mode != requirements.mode.value:
        return None, f"camera mode {mode!r} does not match the declared view's mode"

    zoom = attempt.camera_state.get("zoom")
    if isinstance(zoom, bool) or not isinstance(zoom, (int, float)):
        return None, "camera_state carries no numeric zoom to check against the declared range"
    low, high = requirements.zoom_range
    if not (low <= zoom <= high):
        return None, (
            f"camera zoom {zoom!r} is outside the view's declared zoom_range {(low, high)}"
        )

    target_revealed = attempt.camera_state.get("target_revealed")
    if requirements.target_must_be_revealed and target_revealed is not True:
        # T260: say WHY, when the camera state knows. ``target_unavailable_reason`` is the Lua's
        # own account of which accessor could not answer (``lua/ingame/camera.lua``); failing that,
        # a resolved-but-unrevealed plot is named, so "withheld" never reads as an unexplained
        # false. The verdict is unchanged either way -- unconfirmed still means withheld (FR-026).
        return None, _unrevealed_target_detail(attempt.camera_state)

    return declaration, None


# --------------------------------------------------------------------------
# Gate 4 -- content
# --------------------------------------------------------------------------


def _category_tokens(category_id: str) -> frozenset[str]:
    return frozenset(part for part in category_id.split("_") if part)


_RAW_MODE_MAP: Final[dict[str, tuple[str, str]]] = {
    "BGRA8": ("RGBA", "BGRA"),
    "RGBA8": ("RGBA", "RGBA"),
    "RGB8": ("RGB", "RGB"),
    "BGR8": ("RGB", "BGR"),
}


def transcode_raw_frame_to_png(frame: CaptureFrame) -> CaptureFrame | None:
    """T252: give a raw framebuffer frame a wire form, losslessly, before it is screened.

    A host adapter's `BGRA8` (Linux XComposite, Windows `PrintWindow`) is the truth of the pixels
    but nothing a provider call can carry (`observe/capture.py`'s ``_WIRE_MEDIA_TYPES``), so
    until this existed every clean Linux frame was stored and never shown (measured 2026-09-21:
    twelve frames in the landed-code demo, `blob_media_type=None` on all of them). The re-encoding
    is PNG (lossless) of the same decoded RGB pixels :func:`_decode_frame` screens -- alpha is
    dropped, since on both raw paths it is padding, not transparency (`host/windows/adapter.py`'s
    `extract_bgra8` docstring) -- and the result keeps the frame's width, height and rect. An
    already-encoded frame is returned unchanged; a raw frame that cannot be decoded (wrong length,
    zero size) returns ``None`` so the caller can withhold the step with a reason rather than
    screen a frame it cannot even read. Done *before* screening on purpose: one byte string is
    then screened, hashed, stored and sent, so the record's ``blob_ref`` names exactly what the
    agent received.
    """
    if frame.image_format.upper() not in _RAW_MODE_MAP:
        return frame
    decoded = _decode_frame(frame)
    if decoded is None:
        return None
    buffer = io.BytesIO()
    decoded.save(buffer, format="PNG")
    return CaptureFrame(
        width=frame.width,
        height=frame.height,
        rect=frame.rect,
        image_bytes=buffer.getvalue(),
        image_format="PNG",
    )


def _decode_frame(frame: CaptureFrame) -> Image.Image | None:
    """Decode a raw or encoded :class:`CaptureFrame` into an RGB Pillow image, or ``None``."""
    image_format = frame.image_format.upper()
    try:
        if image_format in _RAW_MODE_MAP:
            mode, raw_mode = _RAW_MODE_MAP[image_format]
            expected_length = frame.width * frame.height * len(raw_mode)
            if frame.width <= 0 or frame.height <= 0 or len(frame.image_bytes) < expected_length:
                return None
            decoded = Image.frombuffer(
                mode, (frame.width, frame.height), frame.image_bytes, "raw", raw_mode, 0, 1
            )
            return decoded.convert("RGB")
        return Image.open(io.BytesIO(frame.image_bytes)).convert("RGB")
    except Exception:  # noqa: BLE001 - any decode failure means "cannot be proven clean"
        return None


_BORDER_THICKNESS_PX: Final[int] = 4
_BORDER_UNIFORMITY_STDDEV_MAX: Final[float] = 12.0
_BORDER_INTERIOR_DELTA_MIN: Final[float] = 40.0


def _border_ring_is_suspect(image: Image.Image) -> bool:
    """A generic recording/capture-border detector: a flat band overlaid at any edge.

    Deliberately compares each edge band against the frame's own *interior*
    rather than judging the band on its own -- a naturally uniform, saturated
    background (a solid ocean or grass tile reaching the frame's edge, say)
    must never trip this on its own, since that would falsely withhold
    ordinary game content. What is actually suspicious is a band that is
    internally flat (low variance -- a synthetic overlay, not a photographed
    gradient) *and* a distinctly different colour from the scene it sits on
    top of, which is what a capture-tool recording border looks like
    regardless of which colour any particular platform happens to draw it in.
    """
    width, height = image.size
    thickness = min(_BORDER_THICKNESS_PX, width // 4, height // 4)
    inset = thickness * 3
    if thickness < 1 or width <= inset * 2 or height <= inset * 2:
        return False

    interior_mean = ImageStat.Stat(image.crop((inset, inset, width - inset, height - inset))).mean
    edges = (
        image.crop((0, 0, width, thickness)),
        image.crop((0, height - thickness, width, height)),
        image.crop((0, 0, thickness, height)),
        image.crop((width - thickness, 0, width, height)),
    )
    for edge in edges:
        stat = ImageStat.Stat(edge)
        mean_stddev = sum(stat.stddev) / len(stat.stddev)
        color_delta = sum(abs(a - b) for a, b in zip(stat.mean, interior_mean, strict=True)) / len(
            stat.mean
        )
        is_flat = mean_stddev <= _BORDER_UNIFORMITY_STDDEV_MAX
        is_distinct_from_scene = color_delta >= _BORDER_INTERIOR_DELTA_MIN
        if is_flat and is_distinct_from_scene:
            return True
    return False


_CORNER_FRACTION: Final[float] = 0.12
_CORNER_VARIANCE_RATIO_MIN: Final[float] = 1.8
_CORNER_VARIANCE_ABS_MIN: Final[float] = 200.0


def _corner_overlay_is_suspect(image: Image.Image) -> bool:
    """A generic in-frame overlay detector: a corner patch far busier than the whole frame.

    This is the technique that can catch chrome the game itself composites
    into its own window (the Linux 60 FPS overlay finding this task calls
    out) -- it never inspects what the patch says, only that one corner is
    anomalously higher-variance than the rest of the captured frame, which is
    the generic shape any small fixed-position HUD/counter/overlay takes.
    """
    width, height = image.size
    corner_width = max(1, int(width * _CORNER_FRACTION))
    corner_height = max(1, int(height * _CORNER_FRACTION))
    whole_stat = ImageStat.Stat(image)
    whole_variance = sum(whole_stat.var) / len(whole_stat.var)
    corners = (
        image.crop((0, 0, corner_width, corner_height)),
        image.crop((width - corner_width, 0, width, corner_height)),
        image.crop((0, height - corner_height, corner_width, height)),
        image.crop((width - corner_width, height - corner_height, width, height)),
    )
    for corner in corners:
        stat = ImageStat.Stat(corner)
        corner_variance = sum(stat.var) / len(stat.var)
        if corner_variance >= _CORNER_VARIANCE_ABS_MIN and corner_variance >= (
            whole_variance * _CORNER_VARIANCE_RATIO_MIN
        ):
            return True
    return False


class ScreeningTechnique(StrEnum):
    """The content gate's techniques, named so coverage can be asserted as data.

    A reject category is only *screened* if at least one of these can actually
    run against it on the attempt in hand. Before this enum existed the
    category-to-technique relationship was implicit in
    :meth:`DefaultContentDetector.detect`'s three ``if`` blocks, so a category
    no technique addressed silently produced "no matches" -- indistinguishable
    from "checked and clean". Publishing it as data is what lets
    :func:`unaddressed_reject_categories` tell those two apart, and lets a test
    assert the invariant over every shipped profile.
    """

    BORDER_RING = "border_ring"
    CORNER_OVERLAY = "corner_overlay"
    DECLARED_TEXT = "declared_text"


#: Which category-id tokens each *image* technique addresses. The token vocabulary is
#: ``catalogs/screening_profiles.yaml``'s own reject ids split on ``"_"`` (see
#: :func:`_category_tokens`), so a new reject id built from the same words is covered without a
#: code change -- and a new id built from words no technique names is *visibly* uncovered rather
#: than silently passed.
_IMAGE_TECHNIQUE_TRIGGER_TOKENS: Final[Mapping[ScreeningTechnique, frozenset[str]]] = (
    MappingProxyType(
        {
            ScreeningTechnique.BORDER_RING: frozenset({"border"}),
            ScreeningTechnique.CORNER_OVERLAY: frozenset({"overlay", "debug"}),
        }
    )
)


def techniques_for_category(category_id: str) -> frozenset[ScreeningTechnique]:
    """Which techniques *could* address *category_id*, ignoring what evidence is on hand.

    :attr:`ScreeningTechnique.DECLARED_TEXT` applies to every non-empty category id (its rule is
    "are this id's own tokens all present in the text evidence?", which is well-defined for any
    id); the image techniques apply only where the id's tokens name what they look for.
    """
    tokens = _category_tokens(category_id)
    if not tokens:
        return frozenset()
    techniques = {
        technique
        for technique, triggers in _IMAGE_TECHNIQUE_TRIGGER_TOKENS.items()
        if tokens & triggers
    }
    techniques.add(ScreeningTechnique.DECLARED_TEXT)
    return frozenset(techniques)


def unaddressed_reject_categories(
    reject_categories: frozenset[str],
    *,
    available_techniques: frozenset[ScreeningTechnique],
) -> frozenset[str]:
    """The reject categories *no available technique* can decide -- i.e. the unscreened ones.

    A non-empty result means the content gate has no way to certify this frame against part of
    its own profile, which is a **withhold**, never a pass (Principle I: a capture that cannot be
    proven clean is not shown). This is deliberately a function of what is *available on this
    attempt*, not of what the codebase can do in principle: a technique whose evidence the caller
    never gathered has not run, and a check that did not run cannot clear anything.
    """
    return frozenset(
        category
        for category in reject_categories
        if not (techniques_for_category(category) & available_techniques)
    )


class ContentDetector(Protocol):
    """The pluggable interface the content gate runs (T129).

    A caller may inject a sharper detector (e.g. a real OCR/template-matching
    implementation, once one exists) via :func:`screen_capture`'s ``detector``
    parameter; :class:`DefaultContentDetector` is the built-in, dependency-free
    implementation used when none is supplied.

    A detector must declare its *coverage* as well as its findings: returning
    an empty match set is not evidence of a clean frame unless the detector can
    say it actually checked. A detector object that does not implement
    :meth:`addressable_categories` is treated by :func:`_check_content` as
    covering nothing at all, so every category withholds -- fail closed, never
    on the assumption that an unknown detector probably looked.
    """

    def detect(
        self,
        frame: CaptureFrame,
        *,
        reject_categories: frozenset[str],
        detected_text_tokens: frozenset[str],
    ) -> frozenset[str]:
        """Return the subset of *reject_categories* this detector found evidence of in *frame*."""
        ...

    def addressable_categories(
        self,
        reject_categories: frozenset[str],
        *,
        text_evidence_available: bool,
    ) -> frozenset[str]:
        """Return the subset of *reject_categories* this detector can actually decide."""
        ...


@dataclass(frozen=True)
class DefaultContentDetector:
    """The built-in content detector: three general techniques, none hardcoded to a product.

    Each technique maps its own finding onto whichever of the caller's
    ``reject_categories`` its id *tokens* suggest it addresses (splitting the
    category id on ``"_"`` -- e.g. ``windows_capture_border`` ->
    ``{"windows", "capture", "border"}``), so this detector automatically
    covers any future reject id built from the same vocabulary without a code
    change, and never claims a category its techniques have no bearing on:

    1. **Border-ring uniformity** (:func:`_border_ring_is_suspect`) for any
       category whose tokens include ``"border"``.
    2. **Corner-overlay variance** (:func:`_corner_overlay_is_suspect`) for
       any category whose tokens include ``"overlay"`` or ``"debug"`` -- this
       is the general technique for chrome drawn *inside* the client's own
       frame (see module docstring).
    3. **Declared-text keyword matching** for any category whose full token
       set is contained in the caller-supplied ``detected_text_tokens``
       (e.g. from window-title enumeration or OCR upstream of this module).
       This technique only *runs* when the caller states that a text-evidence
       source actually ran (``CaptureAttempt.detected_text_tokens is not
       None``); with no source, every category that depends on it is
       unaddressed and the gate withholds rather than reporting no match.

    Which of the three applies to which category is published as data --
    :func:`techniques_for_category`, built from
    :data:`_IMAGE_TECHNIQUE_TRIGGER_TOKENS` -- and :meth:`detect` below
    partitions the caller's categories with that same function, so the
    coverage :meth:`addressable_categories` reports and the coverage
    :meth:`detect` actually exercises cannot drift apart.

    If the frame cannot be decoded at all, every requested category is
    reported matched -- research R7: an image that cannot be proven clean is
    withheld, never passed on the assumption it is probably fine.
    """

    def addressable_categories(
        self,
        reject_categories: frozenset[str],
        *,
        text_evidence_available: bool,
    ) -> frozenset[str]:
        """Which of *reject_categories* this detector can actually decide right now.

        The image techniques always run (there is always a frame). The
        declared-text technique only counts when the caller gathered text
        evidence; absent that, a category it is the sole technique for has not
        been checked by anything, and saying otherwise is how the gate came to
        fail open.
        """
        available = frozenset(_IMAGE_TECHNIQUE_TRIGGER_TOKENS) | (
            {ScreeningTechnique.DECLARED_TEXT} if text_evidence_available else frozenset()
        )
        return reject_categories - unaddressed_reject_categories(
            reject_categories, available_techniques=frozenset(available)
        )

    def detect(
        self,
        frame: CaptureFrame,
        *,
        reject_categories: frozenset[str],
        detected_text_tokens: frozenset[str],
    ) -> frozenset[str]:
        if not reject_categories:
            return frozenset()

        image = _decode_frame(frame)
        if image is None:
            return frozenset(reject_categories)

        matches: set[str] = set()

        def _categories_for(technique: ScreeningTechnique) -> set[str]:
            return {c for c in reject_categories if technique in techniques_for_category(c)}

        border_categories = _categories_for(ScreeningTechnique.BORDER_RING)
        if border_categories and _border_ring_is_suspect(image):
            matches.update(border_categories)

        overlay_categories = _categories_for(ScreeningTechnique.CORNER_OVERLAY)
        if overlay_categories and _corner_overlay_is_suspect(image):
            matches.update(overlay_categories)

        for category in _categories_for(ScreeningTechnique.DECLARED_TEXT):
            if _category_tokens(category) <= detected_text_tokens:
                matches.add(category)

        return frozenset(matches)


DEFAULT_CONTENT_DETECTOR: Final[DefaultContentDetector] = DefaultContentDetector()


def _check_content(
    attempt: CaptureAttempt,
    declaration: ParityDeclaration,
    *,
    profiles: ScreeningProfiles,
    detector: ContentDetector,
) -> str | None:
    declared_profile_key = declaration.screening_profile
    assert declared_profile_key is not None  # guaranteed for kind == view

    profile = resolve_screening_profile(
        profiles, declared_profile_key=declared_profile_key, platform=attempt.platform
    )

    # Coverage BEFORE findings (Principle I). "No technique reported a match" only means the
    # frame is clean for the categories some technique actually examined; for any other category
    # it means nothing was looked at, and this gate used to return that silence as a pass. A
    # detector that cannot say what it covers is taken to cover nothing.
    declare_coverage = getattr(detector, "addressable_categories", None)
    covered: frozenset[str] = (
        declare_coverage(
            profile.reject, text_evidence_available=attempt.detected_text_tokens is not None
        )
        if callable(declare_coverage)
        else frozenset()
    )
    unscreened = profile.reject - covered
    if unscreened:
        noun = "category" if len(unscreened) == 1 else "categories"
        return (
            f"no available screening technique addresses reject {noun} {sorted(unscreened)} "
            f"of profile {profile.name!r}; the frame cannot be certified against it "
            "(withheld rather than passed unscreened)"
        )

    matches = detector.detect(
        attempt.frame,
        reject_categories=profile.reject,
        detected_text_tokens=attempt.detected_text_tokens or frozenset(),
    )
    if matches:
        noun = "category" if len(matches) == 1 else "categories"
        return f"content gate matched reject {noun}: {sorted(matches)}"
    return None


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


class ScreeningGate(StrEnum):
    """Which of the four gates a withheld outcome failed."""

    SOURCE = "source"
    GEOMETRY = "geometry"
    PROVENANCE = "provenance"
    CONTENT = "content"


#: Exhaustive, one-to-one map onto WithheldReason's four members (see module docstring table).
_GATE_WITHHELD_REASON: Final[dict[ScreeningGate, WithheldReason]] = {
    ScreeningGate.SOURCE: WithheldReason.CAPTURE_FAILED,
    ScreeningGate.GEOMETRY: WithheldReason.GEOMETRY_MISMATCH,
    ScreeningGate.PROVENANCE: WithheldReason.PROVENANCE_FAILURE,
    ScreeningGate.CONTENT: WithheldReason.NON_PLAYER_UI,
}


@dataclass(frozen=True)
class ScreeningOutcome:
    """The result of running :func:`screen_capture` on one :class:`CaptureAttempt`."""

    status: ScreeningStatus
    withheld_reason: WithheldReason | None
    failed_gate: ScreeningGate | None
    detail: str | None

    @property
    def is_clean(self) -> bool:
        return self.status is ScreeningStatus.SCREENED_CLEAN


_CLEAN_OUTCOME: Final[ScreeningOutcome] = ScreeningOutcome(
    status=ScreeningStatus.SCREENED_CLEAN, withheld_reason=None, failed_gate=None, detail=None
)


def _withhold(gate: ScreeningGate, detail: str) -> ScreeningOutcome:
    return ScreeningOutcome(
        status=ScreeningStatus.WITHHELD,
        withheld_reason=_GATE_WITHHELD_REASON[gate],
        failed_gate=gate,
        detail=detail,
    )


def screen_capture(
    attempt: CaptureAttempt,
    *,
    registry: CapabilityRegistry,
    profiles: ScreeningProfiles,
    detector: ContentDetector | None = None,
) -> ScreeningOutcome:
    """Run all four gates against *attempt*, in order: source, geometry, provenance, content.

    Short-circuits on the first failing gate (each is independently capable
    of withholding; see tests/unit/test_image_screening.py) and never runs a
    later, more expensive check once an earlier one has already failed.
    Returns :data:`_CLEAN_OUTCOME`-shaped success only once every gate has
    passed.
    """
    resolved_detector = detector if detector is not None else DEFAULT_CONTENT_DETECTOR

    source_failure = _check_source(attempt)
    if source_failure is not None:
        return _withhold(ScreeningGate.SOURCE, source_failure)

    geometry_failure = _check_geometry(attempt)
    if geometry_failure is not None:
        return _withhold(ScreeningGate.GEOMETRY, geometry_failure)

    declaration, provenance_failure = _check_provenance(attempt, registry=registry)
    if provenance_failure is not None or declaration is None:
        return _withhold(ScreeningGate.PROVENANCE, provenance_failure or "provenance check failed")

    content_failure = _check_content(
        attempt, declaration, profiles=profiles, detector=resolved_detector
    )
    if content_failure is not None:
        return _withhold(ScreeningGate.CONTENT, content_failure)

    return _CLEAN_OUTCOME


# --------------------------------------------------------------------------
# Convenience: building the persisted ScreenCapture record from an outcome
# --------------------------------------------------------------------------


def build_screen_capture(
    outcome: ScreeningOutcome,
    *,
    capture_id: CaptureId,
    run_id: RunId,
    turn_number: int,
    decision_step_id: DecisionStepId,
    captured_at: Timestamp,
    attempt: CaptureAttempt,
    blob_ref: str | None,
    shown_to_agent: bool = False,
) -> ScreenCapture:
    """Build the :class:`~civsim_harness.models.turn.ScreenCapture` record for one screened attempt.

    This is the "clean API" ``observe/capture.py`` (T133) is meant to call
    once real screening replaces its current unconditional withholding: pass
    the :class:`ScreeningOutcome` from :func:`screen_capture` and the same
    *attempt*, plus the identifiers/timestamp the caller already tracks.

    ``blob_ref`` is forced to ``None`` whenever *outcome* is not
    :attr:`~civsim_harness.models.turn.ScreeningStatus.SCREENED_CLEAN`, and
    ``shown_to_agent`` likewise, regardless of what the caller passes -- this
    function cannot be made to construct a withheld record that carries a
    blob or claims to have been shown (SC-019); the underlying
    ``ScreenCapture`` model's own validator enforces the same invariant a
    second time, independently, should a caller ever bypass this function.
    """
    is_clean = outcome.status is ScreeningStatus.SCREENED_CLEAN
    return ScreenCapture(
        capture_id=capture_id,
        run_id=run_id,
        turn_number=turn_number,
        decision_step_id=decision_step_id,
        captured_at=captured_at,
        camera_state=dict(attempt.camera_state),
        view_declaration_id=attempt.view_declaration_id,
        screening_status=outcome.status,
        withheld_reason=outcome.withheld_reason,
        shown_to_agent=shown_to_agent and is_clean,
        retained_as_evidence=True,
        blob_ref=blob_ref if is_clean else None,
        capture_path=attempt.capture_path,
    )
