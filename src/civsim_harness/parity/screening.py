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
identity, cross-checked against the run's own located game process)
rather than, say, a stale placeholder slipping through a bug
upstream. That process cross-check is mandatory, not optional: it was
written as "supply one if you have it", no production caller ever did,
and so the check ran nowhere real until 2026-09-22.
*Geometry* then confirms the frame that came back actually matches
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

**And a technique that cannot match is not a technique (T299).** The fix above
was satisfied by a lie one layer down: the declared-text technique derived its
keywords by splitting the category id on ``"_"``, so ``firetuner_window``
required the word ``"window"`` -- which no window title supplies -- and was
*addressable* by a rule that could never fire. Coverage was complete, the
frame was delivered, and the record said it had been screened. Keywords are
now declared per category in ``screening_profiles.yaml`` and proved matchable
at load against the evidence source's own tokeniser, so a category with no
establishable vocabulary is visibly uncovered. Exactly one shipped category
survives that check (``developer_console``); the rest are covered by an image
technique, by :meth:`ScreeningProfiles.structurally_excluded` where the
platform's capture path is measured, or by nothing at all -- which on Windows
and macOS is the honest answer and the reason this is a release blocker there.

**Profile resolution never falls back to a permissive default, and never
resolves a value nobody declared.** :func:`resolve_screening_profile`
implements ``screening_profiles.yaml``'s own resolution rule over a closed
vocabulary (:class:`~civsim_harness.models.catalog.ScreeningProfileDeclaration`):
``"default"`` always means the strictest profile, ``"platform"`` means the
running host's own profile, and a platform with no dedicated entry --
including one this codebase does not yet know the name of -- resolves to that
same strictest profile, never to an absent check. **Any other declared value
is an error**, not a third behaviour: until 2026-09-22 every unrecognised
string fell into the platform branch, which meant a typo resolved silently
and the one string that was honoured (``"default"``) silently screened Linux
frames for Windows chrome -- 34 withholds across two runs named
``windows_capture_border`` on X11 hosts that cannot draw one. A check that
runs against the wrong data is not a check that ran; it is a check counted as
if it had worked.
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
from civsim_harness.host.port import CaptureFrame, GameProcess, GameWindow, window_text_tokens
from civsim_harness.models.catalog import (
    DeclarationKind,
    HudCorner,
    ParityDeclaration,
    ScreeningProfileDeclaration,
)
from civsim_harness.models.common import (
    CaptureId,
    CapturePath,
    DecisionStepId,
    DeclarationId,
    RunId,
    Timestamp,
)
from civsim_harness.models.turn import (
    BorderEdgeMetric,
    CaptureScreeningMetrics,
    CornerVarianceMetric,
    FrameEdge,
    ScreenCapture,
    ScreeningStatus,
    ScreeningTechnique,
    WithheldReason,
)

#: Re-exported so ``from civsim_harness.parity.screening import ScreeningTechnique`` keeps working
#: and, more importantly, so there is exactly one such enum. It moved to ``models.turn`` in T297
#: when it became part of a persisted record shape
#: (:class:`~civsim_harness.models.turn.CaptureScreeningMetrics`); a copy here would be a second
#: vocabulary free to drift from the stored one. (It is used throughout this module as well, so
#: this is a genuine import, not a re-export shim that a linter would strip.)

# --------------------------------------------------------------------------
# screening_profiles.yaml loading (data this module reads, never edits)
# --------------------------------------------------------------------------

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
DEFAULT_SCREENING_PROFILES_PATH: Final[Path] = _REPO_ROOT / "catalogs" / "screening_profiles.yaml"
DEFAULT_PROFILE_KEY: Final[str] = "default"


class RejectOrigin(StrEnum):
    """Where a reject category can appear, which decides whether structure can exclude it (T299).

    ``SEPARATE_WINDOW`` is a top-level window of some other process, sitting on the desktop;
    ``IN_FRAME`` is drawn inside the captured surface itself -- by the game, by a third party
    compositing into the client's own window (the measured Steam FPS overlay), or by the capture
    API (the Windows recording border). Only the first kind can be ruled out by a capture path
    that reads the game window's own backing store, which is why the distinction is declared per
    category rather than assumed.
    """

    SEPARATE_WINDOW = "separate_window"
    IN_FRAME = "in_frame"


@dataclass(frozen=True)
class CaptureScopeRule:
    """What one declared ``capture_scopes`` entry rules out (``screening_profiles.yaml``)."""

    name: str
    description: str
    excludes_origin: frozenset[RejectOrigin]


@dataclass(frozen=True)
class RejectDefinition:
    """One reject category: what it is, where it can appear, and what evidence proves it (T299).

    ``keyword_groups`` is the declared-text vocabulary, as an *any-of groups, all-of words*
    structure: a group matches when every word in it appears in the desktop evidence, and the
    category matches when any group does. It is the whole point of this type. Before T299 the
    vocabulary was the category id split on ``"_"``, which made "what this is called" and "what
    proves it is here" the same string -- so ``firetuner_window`` required the word ``"window"``,
    which no window title supplies, and was *addressable* by a technique that could never fire.

    An **empty** ``keyword_groups`` is a declared, reasoned absence, never a silent one: the
    loader requires ``text_unmatchable`` and ``text_settled_by`` in that case, and such a category
    is not addressed by :attr:`ScreeningTechnique.DECLARED_TEXT` at all. That is the difference
    between "this category has no vocabulary and the coverage map says so" and the old state,
    where it had a vocabulary that happened to match nothing.

    ``witnesses`` are exemplar strings from the evidence source. Every declared group must be
    produced by at least one of them under the production tokeniser
    (:func:`~civsim_harness.host.port.window_text_tokens`), or the catalog does not load -- that
    check is *matchability*, as opposed to the mere addressability the guard used to assert.
    """

    category_id: str
    description: str
    origin: RejectOrigin
    keyword_groups: tuple[frozenset[str], ...] = ()
    witnesses: tuple[str, ...] = ()
    text_basis: str = ""
    text_unmatchable: str = ""
    text_settled_by: str = ""

    @property
    def text_matchable(self) -> bool:
        """Whether the evidence source can, in principle, supply this category's vocabulary."""
        return bool(self.keyword_groups)


@dataclass(frozen=True)
class ScreeningProfile:
    """One named profile's reject set and capture scope (``catalogs/screening_profiles.yaml``)."""

    name: str
    description: str
    reject: frozenset[str]
    capture_scope: CaptureScopeRule
    capture_scope_basis: str


@dataclass(frozen=True)
class ScreeningProfiles:
    """The fully loaded, structurally validated ``screening_profiles.yaml``."""

    schema_version: int
    reject_definitions: Mapping[str, RejectDefinition]
    universal_reject: frozenset[str]
    profiles: Mapping[str, ScreeningProfile]
    capture_scopes: Mapping[str, CaptureScopeRule]

    @property
    def text_vocabulary(self) -> Mapping[str, tuple[frozenset[str], ...]]:
        """The declared keyword vocabulary, as the content gate consumes it.

        A category id missing from this mapping -- or present with an empty tuple -- is one the
        declared-text technique cannot address. Both forms mean the same thing on purpose, so a
        caller cannot accidentally get the old behaviour by passing a mapping that is merely
        incomplete.
        """
        return MappingProxyType(
            {
                category_id: definition.keyword_groups
                for category_id, definition in self.reject_definitions.items()
                if definition.keyword_groups
            }
        )

    def structurally_excluded(self, profile: ScreeningProfile) -> frozenset[str]:
        """The reject categories *profile*'s capture path cannot contain, whatever techniques say.

        This is the 2026-09-22 hypervisor ruling expressed where the coverage map can see it.
        Linux image delivery is open on structural grounds -- the X11 path reads the game window's
        own off-screen backing store with no screen-grab fallback, so another application's
        top-level window cannot be in the frame -- and that reasoning used to live only in prose,
        which meant the coverage map had to be satisfied by a technique instead. It was: by a
        declared-text rule that could not match. Reading the exclusion from data lets the honest
        answer ("nothing screens this, and nothing has to") be stated *and* keeps the platforms
        with no such structure visibly uncovered, which is where the release blocker actually is.

        A category is only excluded when its own declared :class:`RejectOrigin` is one the
        profile's declared capture scope rules out; ``in_frame`` chrome is never excluded by any
        scope, because every scope in this file still returns the client's own pixels.
        """
        excluded = profile.capture_scope.excludes_origin
        if not excluded:
            return frozenset()
        return frozenset(
            category
            for category in profile.reject
            if category in self.reject_definitions
            and self.reject_definitions[category].origin in excluded
        )


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

    capture_scopes = _validated_capture_scopes(data.get("capture_scopes"), path=resolved_path)

    reject_definitions_raw = data.get("reject_definitions")
    if not isinstance(reject_definitions_raw, Mapping) or not reject_definitions_raw:
        raise CatalogError(
            "screening_profiles.yaml is missing a non-empty reject_definitions mapping",
            detail={"path": str(resolved_path)},
        )
    reject_definitions = {
        str(key): _validated_reject_definition(str(key), body, path=resolved_path)
        for key, body in reject_definitions_raw.items()
    }
    known_ids = frozenset(reject_definitions)

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
        scope_key = body.get("capture_scope")
        if not isinstance(scope_key, str) or scope_key not in capture_scopes:
            raise CatalogError(
                "a screening profile must declare a capture_scope defined under capture_scopes "
                "(what this platform's capture path can structurally contain); a profile that "
                "does not say cannot be given the benefit of the doubt",
                detail={
                    "path": str(resolved_path),
                    "profile": str(name),
                    "declared": scope_key,
                    "defined": sorted(capture_scopes),
                },
            )
        scope = capture_scopes[scope_key]
        scope_basis = str(body.get("capture_scope_basis", "")).strip()
        if scope.excludes_origin and not scope_basis:
            raise CatalogError(
                "a profile whose capture_scope excludes a category origin must record the "
                "evidence for it in capture_scope_basis: a structural exemption with no basis "
                "is the prose-only ruling this field exists to replace",
                detail={
                    "path": str(resolved_path),
                    "profile": str(name),
                    "capture_scope": scope_key,
                },
            )
        profiles[str(name)] = ScreeningProfile(
            name=str(name),
            description=str(body.get("description", "")),
            reject=reject,
            capture_scope=scope,
            capture_scope_basis=scope_basis,
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
        reject_definitions=MappingProxyType(reject_definitions),
        universal_reject=universal_reject,
        profiles=MappingProxyType(profiles),
        capture_scopes=MappingProxyType(capture_scopes),
    )


def _validated_capture_scopes(raw: Any, *, path: Path) -> dict[str, CaptureScopeRule]:
    if not isinstance(raw, Mapping) or not raw:
        raise CatalogError(
            "screening_profiles.yaml is missing a non-empty capture_scopes mapping; every "
            "profile must state what its platform's capture path can contain",
            detail={"path": str(path)},
        )
    scopes: dict[str, CaptureScopeRule] = {}
    for name, body in raw.items():
        if not isinstance(body, Mapping):
            raise CatalogError(
                "a capture_scopes entry must be a mapping",
                detail={"path": str(path), "capture_scope": str(name)},
            )
        excludes_raw = body.get("excludes_origin")
        if excludes_raw is None or not isinstance(excludes_raw, list):
            raise CatalogError(
                "a capture_scopes entry must declare excludes_origin as a list (use [] for a "
                "scope that excludes nothing); omitting it would make 'excludes nothing' and "
                "'nobody said' the same thing",
                detail={"path": str(path), "capture_scope": str(name)},
            )
        origins: set[RejectOrigin] = set()
        for item in excludes_raw:
            try:
                origins.add(RejectOrigin(str(item)))
            except ValueError as exc:
                raise CatalogError(
                    "capture_scopes excludes_origin names an unknown category origin",
                    detail={
                        "path": str(path),
                        "capture_scope": str(name),
                        "origin": str(item),
                        "known": sorted(o.value for o in RejectOrigin),
                    },
                ) from exc
        scopes[str(name)] = CaptureScopeRule(
            name=str(name),
            description=str(body.get("description", "")),
            excludes_origin=frozenset(origins),
        )
    return scopes


def _validated_reject_definition(category_id: str, raw: Any, *, path: Path) -> RejectDefinition:
    """One ``reject_definitions`` entry, with its keyword vocabulary proved matchable (T299).

    The load-time refusals here are the fix. A bare string -- the pre-T299 shape, where the entry
    was only prose and the vocabulary was the id's own tokens -- does not load, because that
    shape is exactly what made three shipped categories addressable and unmatchable. Neither does
    a keyword group no declared witness can produce: that is the matchability check, run against
    the *production* tokeniser rather than a copy of it, so a vocabulary that no evidence source
    could ever supply is refused at the door instead of passing a coverage guard.
    """
    detail = {"path": str(path), "reject_id": category_id}
    if isinstance(raw, str) or not isinstance(raw, Mapping):
        raise CatalogError(
            "a reject_definitions entry must be a mapping declaring at least description, "
            "origin and text_keywords; a bare description is the pre-T299 shape in which a "
            "category's keywords were its own name tokens, which is how three shipped "
            "categories came to be addressable by a technique that could never match them",
            detail=detail,
        )

    description = str(raw.get("description", "")).strip()
    if not description:
        raise CatalogError("a reject_definitions entry must carry a description", detail=detail)

    try:
        origin = RejectOrigin(str(raw.get("origin")))
    except ValueError as exc:
        raise CatalogError(
            "a reject_definitions entry must declare an origin (where this category can appear), "
            "because that is what decides whether a capture path's structure can exclude it",
            detail={**detail, "known": sorted(o.value for o in RejectOrigin)},
        ) from exc

    if "text_keywords" not in raw:
        raise CatalogError(
            "a reject_definitions entry must declare text_keywords (use [] and say why in "
            "text_unmatchable if no vocabulary can be established); silence here is the defect "
            "T299 exists to remove -- a category that matches nothing while looking addressed",
            detail=detail,
        )

    groups = _validated_keyword_groups(raw.get("text_keywords"), detail=detail)
    witnesses = tuple(str(item) for item in (raw.get("text_witnesses") or []))

    if not groups:
        unmatchable = str(raw.get("text_unmatchable", "")).strip()
        settled_by = str(raw.get("text_settled_by", "")).strip()
        if not unmatchable or not settled_by:
            raise CatalogError(
                "a reject category with an empty text_keywords must declare BOTH why no "
                "vocabulary could be established (text_unmatchable) and what evidence would "
                "settle it (text_settled_by); an unexplained absence is indistinguishable from "
                "an oversight, and an oversight is what this catalog had",
                detail=detail,
            )
        return RejectDefinition(
            category_id=category_id,
            description=description,
            origin=origin,
            witnesses=witnesses,
            text_unmatchable=unmatchable,
            text_settled_by=settled_by,
        )

    basis = str(raw.get("text_basis", "")).strip()
    if not basis:
        raise CatalogError(
            "a reject category declaring text_keywords must record text_basis: where the "
            "vocabulary comes from. A keyword list with no stated provenance is a guess, and a "
            "guess that happens to match nothing is the defect this replaced",
            detail=detail,
        )
    if not witnesses:
        raise CatalogError(
            "a reject category declaring text_keywords must declare text_witnesses: exemplar "
            "strings the evidence source could actually produce. Without one, nothing "
            "distinguishes a matchable vocabulary from an unmatchable one",
            detail=detail,
        )

    witness_tokens = [window_text_tokens(witness) for witness in witnesses]
    for group in groups:
        if not any(group <= tokens for tokens in witness_tokens):
            raise CatalogError(
                "a declared keyword group is not produced by any declared witness, so no "
                "evidence source is known to be able to supply it -- the category would be "
                "addressable by the declared-text technique and unmatchable in practice, which "
                "is precisely the state T299 was opened to end",
                detail={
                    **detail,
                    "keyword_group": sorted(group),
                    "witness_tokens": [sorted(tokens) for tokens in witness_tokens],
                },
            )

    return RejectDefinition(
        category_id=category_id,
        description=description,
        origin=origin,
        keyword_groups=groups,
        witnesses=witnesses,
        text_basis=basis,
    )


def _validated_keyword_groups(raw: Any, *, detail: Mapping[str, str]) -> tuple[frozenset[str], ...]:
    if not isinstance(raw, list):
        raise CatalogError(
            "text_keywords must be a YAML list of keyword groups", detail=dict(detail)
        )
    groups: list[frozenset[str]] = []
    for group in raw:
        if not isinstance(group, list) or not group:
            raise CatalogError(
                "each text_keywords entry must be a non-empty list of words, all of which must "
                "be present for that group to match",
                detail=dict(detail),
            )
        words = [str(word) for word in group]
        for word in words:
            if window_text_tokens(word) != frozenset({word}):
                raise CatalogError(
                    "a declared keyword is not a word the evidence source's tokeniser can "
                    "produce (it must be lower-case and alphanumeric, with no separators)",
                    detail={**dict(detail), "keyword": word},
                )
        groups.append(frozenset(words))
    return tuple(groups)


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

    Exactly two declared values are legal, and they are named
    (:class:`~civsim_harness.models.catalog.ScreeningProfileDeclaration`):

    - ``"default"`` -- the strictest union profile, regardless of *platform*.
    - ``"platform"`` -- the running host's own profile; a *platform* with no
      dedicated entry resolves to the strictest profile, never to an absent
      check.

    **Anything else raises** :class:`~civsim_harness.errors.CatalogError`
    (T292). It used to fall into the platform branch, which made every
    unrecognised string -- a typo, a profile key that does not exist, a value
    from a newer catalog -- resolve to *something*, silently. That is how 34
    withholds across two Linux runs came to name ``windows_capture_border``:
    the views declared ``default``, the one value whose meaning *was* honoured,
    and it means "screen this frame for every platform's chrome including the
    ones that cannot apply here". A resolution that cannot be wrong is worth
    more than one that always answers.
    """
    if declared_profile_key == ScreeningProfileDeclaration.DEFAULT:
        return profiles.profiles[DEFAULT_PROFILE_KEY]
    if declared_profile_key == ScreeningProfileDeclaration.PLATFORM:
        return profiles.profiles.get(platform, profiles.profiles[DEFAULT_PROFILE_KEY])
    raise CatalogError(
        f"declared screening_profile {declared_profile_key!r} is not a declarable value",
        detail={
            "declared_profile_key": declared_profile_key,
            "declarable": sorted(v.value for v in ScreeningProfileDeclaration),
            "platform": platform,
            "note": (
                "a platform profile key such as 'linux' is resolved from the running host by "
                "'platform'; it is never declared directly, and an unknown value must not "
                "resolve to a profile that merely looks strict"
            ),
        },
    )


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

    ``expected_process`` is the run's own located
    :class:`~civsim_harness.host.port.GameProcess`, checked against the
    declared window's pid by the source gate. ``None`` means the run could not
    identify its client, and the gate withholds: a check that did not run is
    not a check that passed.

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
    # A missing expected_process is a missing CHECK, not a passed one. It used to be treated as
    # optional plumbing and skipped, which meant the process-identity check -- the whole reason
    # this gate is more than a capture_path assertion -- never ran anywhere in production, since
    # the production caller never supplied one. ``observe/capture.py`` now locates the process
    # itself rather than omitting it, so arriving here with None means the run genuinely cannot
    # say which process it is looking at, and an unidentifiable frame is withheld.
    if attempt.expected_process is None:
        return (
            "no located game process was supplied, so the declared window's identity could not "
            "be checked against the run's own client (the check did not run; it did not pass)"
        )
    if attempt.window.pid != attempt.expected_process.pid:
        return "the declared game window's pid does not match the run's own located game process"
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


def _border_ring_statistics(image: Image.Image) -> tuple[BorderEdgeMetric, ...]:
    """The per-edge numbers :func:`_border_ring_is_suspect` decides on, without the decision.

    Split out in T297 so the statistic that is *persisted* is the statistic the verdict was
    computed from. The verdict below is a pure function of this tuple and the two module
    constants, so the recorded numbers cannot describe a different comparison than the one that
    actually ran -- there is only one computation.
    """
    width, height = image.size
    thickness = min(_BORDER_THICKNESS_PX, width // 4, height // 4)
    inset = thickness * 3
    if thickness < 1 or width <= inset * 2 or height <= inset * 2:
        return ()

    interior_mean = ImageStat.Stat(image.crop((inset, inset, width - inset, height - inset))).mean
    edges = {
        FrameEdge.TOP: image.crop((0, 0, width, thickness)),
        FrameEdge.BOTTOM: image.crop((0, height - thickness, width, height)),
        FrameEdge.LEFT: image.crop((0, 0, thickness, height)),
        FrameEdge.RIGHT: image.crop((width - thickness, 0, width, height)),
    }
    metrics: list[BorderEdgeMetric] = []
    for edge, band in edges.items():
        stat = ImageStat.Stat(band)
        metrics.append(
            BorderEdgeMetric(
                edge=edge,
                uniformity_stddev=sum(stat.stddev) / len(stat.stddev),
                interior_color_delta=sum(
                    abs(a - b) for a, b in zip(stat.mean, interior_mean, strict=True)
                )
                / len(stat.mean),
            )
        )
    return tuple(metrics)


def _border_ring_verdict(metrics: tuple[BorderEdgeMetric, ...]) -> bool:
    """The border-ring decision, as a pure function of the recorded statistics."""
    return any(
        metric.uniformity_stddev <= _BORDER_UNIFORMITY_STDDEV_MAX
        and metric.interior_color_delta >= _BORDER_INTERIOR_DELTA_MIN
        for metric in metrics
    )


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

    Since T297 this is the two-line composition of :func:`_border_ring_statistics` and
    :func:`_border_ring_verdict`; the behaviour is unchanged, but the numbers behind the
    behaviour now survive the frame.
    """
    return _border_ring_verdict(_border_ring_statistics(image))


# PROVENANCE OF THE THREE CONSTANTS BELOW -- stated honestly, because the honest answer is
# "chosen, then measured afterwards", and a comment inventing a derivation would be worse than
# none. They were introduced with this module in 63df35a alongside a single positive fixture
# (a flat frame with one uniform-random corner patch); no measurement, frame size or sample
# accompanied them in that commit, in research.md, or anywhere else in specs/ -- they are
# unexplained choices, not tuned values.
#
# What they HAVE now been measured against, after the fact:
# `specs/002-civ-playing-harness/spikes/frame-retro-audit-2026-09-22.md` re-ran this exact
# technique offline over all 188 distinct blobs the harness had shown a model -- every one a real
# Civilization VI frame at 1920x1200, PNG/RGB, X11/XComposite. Result: it fires on 5 of 188, all
# from one run's turn 2, and all five sit at a corner/whole variance ratio of 1.800-1.802 against
# the 1.8 threshold here -- i.e. decided by float-vs-histogram rounding, not by signal. Visual
# inspection of those five found ordinary game UI (leader portraits and a tooltip in the
# top-right), no contaminant. So on real frames this detector is a coin-flip at its own boundary,
# and `tests/unit/test_image_screening.py`'s realistic-frame negative control (added 2026-09-22)
# demonstrates the same false-positive shape from synthetic pixels.
#
# T283 (2026-09-22) addressed that WITHOUT touching these three numbers: what changed is which
# reference a corner is compared against when the view declares that corner as its own HUD (see
# `_corner_overlay_is_suspect`). The negative control, previously xfail(strict=True), now passes.
# An undeclared corner is still judged against the whole frame at exactly these values.
#
# That is a finding about the thresholds, NOT a licence to raise them. Moving a number until live
# frames pass would be tuning the gate to the outcome someone wanted rather than to evidence, and
# the frames the gate withheld were never stored (same spike, "Unknown: the 421 withheld
# captures"), so there is no measured negative population to tune against yet. Any future change
# here needs a real-frame sample on both sides and must be argued against the negative control,
# not against a guess.
_CORNER_FRACTION: Final[float] = 0.12
_CORNER_VARIANCE_RATIO_MIN: Final[float] = 1.8
_CORNER_VARIANCE_ABS_MIN: Final[float] = 200.0


def _corner_boxes(width: int, height: int) -> dict[HudCorner, tuple[int, int, int, int]]:
    corner_width = max(1, int(width * _CORNER_FRACTION))
    corner_height = max(1, int(height * _CORNER_FRACTION))
    return {
        HudCorner.TOP_LEFT: (0, 0, corner_width, corner_height),
        HudCorner.TOP_RIGHT: (width - corner_width, 0, width, corner_height),
        HudCorner.BOTTOM_LEFT: (0, height - corner_height, corner_width, height),
        HudCorner.BOTTOM_RIGHT: (
            width - corner_width,
            height - corner_height,
            width,
            height,
        ),
    }


def _corner_overlay_is_suspect(
    image: Image.Image, *, hud_corners: frozenset[HudCorner] = frozenset()
) -> bool:
    """A generic in-frame overlay detector: a corner patch anomalously busier than its reference.

    This is the technique that can catch chrome the game itself composites
    into its own window (the Linux 60 FPS overlay finding this task calls
    out) -- it never inspects what the patch says, only that one corner is
    anomalously higher-variance than what it is compared against.

    **What the comparison is depends on what the view declares** (T283, and the
    reason the two constants below did not have to move):

    - A corner the view does **not** declare as its own HUD is compared against
      the whole frame, exactly as before. Unchanged strictness: an overlay
      somewhere the game keeps no chrome still trips at the same numbers.
    - A corner the view **does** declare as HUD (``ParityDeclaration.hud_corners``)
      is compared against the busiest of the *other declared HUD corners*. A fixed
      HUD's corners are all dense chrome, so "busier than the whole frame" describes
      every one of them and says nothing; "busier than the other HUD corners" is a
      statement about this corner in particular. A debug overlay drawn *over* the
      minimap is still caught, because it still lifts that corner away from its peers.
      With no peers to compare against (a view declaring a single HUD corner), the
      whole-frame comparison is used -- the strict one, never no check.

    MEASURED on ``tests/unit/test_image_screening.py::_realistic_gameplay_frame``
    (1920x1200, built from the retro-audit's findings; the *only* negative control
    that exists, and synthetic -- the 421 withheld live captures were stored with
    ``blob_ref=NULL``, so there is no real negative population to tune against):

    ============================  ==========  ==================  ====================
    frame                         whole/var   worst whole-ratio   worst HUD-peer ratio
    ============================  ==========  ==================  ====================
    clean gameplay frame             590.6    5.02 (top-right)    1.53 (top-right)
    + full-corner overlay, TL        690.4    7.90                1.84
    + full-corner overlay, TR        668.4    8.16                2.82
    + full-corner overlay, BL        679.9    8.02                1.84
    + full-corner overlay, BR        688.6    7.92                1.84
    ============================  ==========  ==================  ====================

    So against ``_CORNER_VARIANCE_RATIO_MIN`` = 1.8 the HUD-peer comparison separates
    clean (1.53) from contaminated (1.84+) -- but by 15% on one side and 2% on the
    other, on one synthetic frame. That is a *narrow* band and it is stated here
    rather than rounded off: a smaller overlay drawn over a busy HUD corner (measured:
    a 220x60 patch on the bottom-left minimap reaches only 1.71) is **not** caught by
    this technique. It is not a residual-risk-free change; it is a technique that now
    carries information where it previously fired on everything.

    Since T297 this is the composition of :func:`_corner_overlay_statistics` and
    :func:`_corner_overlay_verdict`; the behaviour is unchanged, but the four ratios behind the
    behaviour -- the ones nobody could produce for the 421 withheld frames -- now survive it.
    """
    _, metrics = _corner_overlay_statistics(image, hud_corners=hud_corners)
    return _corner_overlay_verdict(metrics)


def _corner_overlay_statistics(
    image: Image.Image, *, hud_corners: frozenset[HudCorner] = frozenset()
) -> tuple[float, tuple[CornerVarianceMetric, ...]]:
    """The whole-frame variance and the per-corner comparison :func:`_corner_overlay_is_suspect`
    decides on, without the decision.

    Every corner is measured, including ones the abs-min floor will discard -- the floor is a
    *threshold*, and a distribution that only contains the values which cleared a threshold
    cannot be used to evaluate it. That is the mistake this whole task exists to stop repeating.
    """
    width, height = image.size
    whole_stat = ImageStat.Stat(image)
    whole_variance = sum(whole_stat.var) / len(whole_stat.var)

    variances: dict[HudCorner, float] = {}
    for corner, box in _corner_boxes(width, height).items():
        stat = ImageStat.Stat(image.crop(box))
        variances[corner] = sum(stat.var) / len(stat.var)

    declared = frozenset(hud_corners) & frozenset(variances)
    metrics: list[CornerVarianceMetric] = []
    for corner, corner_variance in variances.items():
        peers = [variances[other] for other in declared if other is not corner]
        use_peer = corner in declared and bool(peers)
        metrics.append(
            CornerVarianceMetric(
                corner=corner,
                variance=corner_variance,
                reference_variance=max(peers) if use_peer else whole_variance,
                reference_is_hud_peer=use_peer,
            )
        )
    return whole_variance, tuple(metrics)


def _corner_overlay_verdict(metrics: tuple[CornerVarianceMetric, ...]) -> bool:
    """The corner-overlay decision, as a pure function of the recorded statistics."""
    return any(
        metric.variance >= _CORNER_VARIANCE_ABS_MIN
        and metric.variance >= metric.reference_variance * _CORNER_VARIANCE_RATIO_MIN
        for metric in metrics
    )


#: Which category-id tokens each *image* technique addresses. These two stay id-derived on
#: purpose: they describe what the technique physically looks for (a flat ring at an edge, a busy
#: corner patch), so a new reject id built from the same words is covered without a code change.
#: The declared-text technique is the one that must NOT work this way -- see
#: :func:`techniques_for_category`.
_IMAGE_TECHNIQUE_TRIGGER_TOKENS: Final[Mapping[ScreeningTechnique, frozenset[str]]] = (
    MappingProxyType(
        {
            ScreeningTechnique.BORDER_RING: frozenset({"border"}),
            ScreeningTechnique.CORNER_OVERLAY: frozenset({"overlay", "debug"}),
        }
    )
)

#: A category's declared keyword vocabulary, as :func:`techniques_for_category` and the detector
#: take it: category id -> the any-of groups of all-of words that indicate it. Obtained from
#: :attr:`ScreeningProfiles.text_vocabulary`; an id absent from it has no declared vocabulary.
TextVocabulary = Mapping[str, tuple[frozenset[str], ...]]

#: The fail-closed default for a caller that supplies no vocabulary at all: nothing is addressed
#: by the declared-text technique. Deliberately not "fall back to the id's own tokens" -- that
#: fallback IS the defect, and a silent one would restore it the first time a call site was
#: written without the argument.
NO_TEXT_VOCABULARY: Final[TextVocabulary] = MappingProxyType({})


def techniques_for_category(
    category_id: str, *, text_vocabulary: TextVocabulary = NO_TEXT_VOCABULARY
) -> frozenset[ScreeningTechnique]:
    """Which techniques *could* address *category_id*, ignoring what evidence is on hand.

    The image techniques apply where the id's tokens name what they look for. **The declared-text
    technique applies only where the catalog declares a keyword vocabulary for the id** (T299).
    It used to apply to every non-empty id, on the reasoning that "are this id's own tokens all
    present in the evidence?" is well-defined for any id -- which is true, and useless: well
    defined is not the same as satisfiable. ``firetuner_window`` required the word ``"window"``,
    ``harness_owned_ui`` required ``"owned"`` and ``"ui"``, ``linux_panel`` required ``"linux"``,
    and no window title supplies any of them, so three shipped categories were addressable in the
    coverage map and unmatchable on the wire. The frame was delivered and the guard said it had
    been screened.

    Now the vocabulary is data, validated at load against the evidence source's own tokeniser
    (:func:`_validated_reject_definition`), and a category with none is addressed by nothing here
    -- which is the true answer, and makes the platforms that depend on it visibly uncovered.
    """
    tokens = _category_tokens(category_id)
    if not tokens:
        return frozenset()
    techniques = {
        technique
        for technique, triggers in _IMAGE_TECHNIQUE_TRIGGER_TOKENS.items()
        if tokens & triggers
    }
    if text_vocabulary.get(category_id):
        techniques.add(ScreeningTechnique.DECLARED_TEXT)
    return frozenset(techniques)


def unaddressed_reject_categories(
    reject_categories: frozenset[str],
    *,
    available_techniques: frozenset[ScreeningTechnique],
    text_vocabulary: TextVocabulary = NO_TEXT_VOCABULARY,
) -> frozenset[str]:
    """The reject categories *no available technique* can decide -- i.e. the unscreened ones.

    A non-empty result means the content gate has no way to certify this frame against part of
    its own profile, which is a **withhold**, never a pass (Principle I: a capture that cannot be
    proven clean is not shown). This is deliberately a function of what is *available on this
    attempt*, not of what the codebase can do in principle: a technique whose evidence the caller
    never gathered has not run, and a check that did not run cannot clear anything.

    Since T299 it is also a function of what the catalog can actually match: a category whose
    declared vocabulary is empty is not addressed by the declared-text technique however much
    evidence the caller gathered. Structural exclusion (:meth:`ScreeningProfiles.
    structurally_excluded`) is the separate, declared way such a category can still be covered.
    """
    return frozenset(
        category
        for category in reject_categories
        if not (
            techniques_for_category(category, text_vocabulary=text_vocabulary)
            & available_techniques
        )
    )


@dataclass(frozen=True)
class FrameStatistics:
    """Everything both image techniques compute for one frame, plus their verdicts (T297).

    The point of this object is *ordering*: it is produced once, while the decoded image is in
    memory, and both the gate's decision and the persisted
    :class:`~civsim_harness.models.turn.CaptureScreeningMetrics` are built from the same instance.
    The verdicts are properties over the stored numbers rather than separately computed flags, so
    "what was recorded" and "what was decided on" are the same computation by construction, not
    by a test that has to notice they drifted.

    ``decoded`` is ``False`` when the frame could not be opened at all; ``width``/``height`` then
    come from the frame's own declaration and every statistic is empty. That case is a withhold
    (research R7), and the record says which statistics do not exist rather than inventing them.
    """

    width: int
    height: int
    decoded: bool
    whole_frame_variance: float | None
    corners: tuple[CornerVarianceMetric, ...]
    border_edges: tuple[BorderEdgeMetric, ...]

    @property
    def border_ring_is_suspect(self) -> bool:
        return _border_ring_verdict(self.border_edges)

    @property
    def corner_overlay_is_suspect(self) -> bool:
        return _corner_overlay_verdict(self.corners)


def derive_frame_statistics(
    frame: CaptureFrame, *, hud_corners: frozenset[HudCorner] = frozenset()
) -> FrameStatistics:
    """Compute both image techniques' statistics for *frame*, once.

    **Call this while the pixels exist or not at all.** Nothing downstream can recompute it: a
    withheld frame is never persisted (SC-019) and must not be, so these numbers are the only
    trace of what the gate saw. See :class:`~civsim_harness.models.turn.CaptureScreeningMetrics`
    for why that constraint is the task rather than an inconvenience in it.
    """
    image = _decode_frame(frame)
    if image is None:
        return FrameStatistics(
            width=frame.width,
            height=frame.height,
            decoded=False,
            whole_frame_variance=None,
            corners=(),
            border_edges=(),
        )
    whole_frame_variance, corners = _corner_overlay_statistics(image, hud_corners=hud_corners)
    width, height = image.size
    return FrameStatistics(
        width=width,
        height=height,
        decoded=True,
        whole_frame_variance=whole_frame_variance,
        corners=corners,
        border_edges=_border_ring_statistics(image),
    )


@dataclass(frozen=True)
class DetectionReport:
    """What a detection run found *and* what it measured getting there (T297).

    ``matches`` is exactly what :meth:`DefaultContentDetector.detect` returns; the other three
    fields are the evidence that used to be discarded the moment the boolean was produced.
    ``techniques_run`` is which techniques had a category to decide on this attempt, and
    ``techniques_fired`` is the subset that reported evidence -- kept apart because "ran and found
    nothing" and "never ran" are the distinction this gate has already failed open on twice
    (T264, T299).
    """

    matches: frozenset[str]
    statistics: FrameStatistics
    techniques_run: frozenset[ScreeningTechnique]
    techniques_fired: frozenset[ScreeningTechnique]


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
        text_vocabulary: TextVocabulary = NO_TEXT_VOCABULARY,
        hud_corners: frozenset[HudCorner] = frozenset(),
    ) -> frozenset[str]:
        """Return the subset of *reject_categories* this detector found evidence of in *frame*.

        *text_vocabulary* is the catalog's declared per-category keyword vocabulary (T299); a
        detector must not derive keywords from the category id instead. *hud_corners* is the
        view's own declaration of which frame corners hold the game's HUD (T283). A detector is
        free to ignore either; the empty defaults are the strict reading of both.
        """
        ...

    def addressable_categories(
        self,
        reject_categories: frozenset[str],
        *,
        text_evidence_available: bool,
        text_vocabulary: TextVocabulary = NO_TEXT_VOCABULARY,
    ) -> frozenset[str]:
        """Return the subset of *reject_categories* this detector can actually decide."""
        ...


@dataclass(frozen=True)
class DefaultContentDetector:
    """The built-in content detector: three general techniques, none hardcoded to a product.

    The two *image* techniques map their findings onto whichever of the
    caller's ``reject_categories`` the id's own tokens suggest they address
    (splitting on ``"_"`` -- e.g. ``windows_capture_border`` ->
    ``{"windows", "capture", "border"}``), which is sound because those tokens
    describe what the technique physically looks for. The *text* technique
    does not, and must not: its keywords come from the catalog (T299), because
    a category's name is not evidence that the category is present:

    1. **Border-ring uniformity** (:func:`_border_ring_is_suspect`) for any
       category whose tokens include ``"border"``.
    2. **Corner-overlay variance** (:func:`_corner_overlay_is_suspect`) for
       any category whose tokens include ``"overlay"`` or ``"debug"`` -- this
       is the general technique for chrome drawn *inside* the client's own
       frame (see module docstring).
    3. **Declared-text keyword matching** for any category the catalog gives a
       keyword vocabulary (``reject_definitions.<id>.text_keywords``), against
       the caller-supplied ``detected_text_tokens`` (e.g. from window-title
       enumeration or OCR upstream of this module). The vocabulary is data, not
       the category's own name split on ``"_"`` -- T299, and the reason this
       technique now *can* fail to address a category. It only *runs* when the
       caller states that a text-evidence source actually ran
       (``CaptureAttempt.detected_text_tokens is not None``); with no source,
       every category that depends on it is unaddressed and the gate withholds
       rather than reporting no match.

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
        text_vocabulary: TextVocabulary = NO_TEXT_VOCABULARY,
    ) -> frozenset[str]:
        """Which of *reject_categories* this detector can actually decide right now.

        The image techniques always run (there is always a frame). The
        declared-text technique counts only when the caller gathered text
        evidence **and** the category has a declared vocabulary to match it
        against; absent either, a category it is the sole technique for has not
        been checked by anything, and saying otherwise is how the gate came to
        fail open -- first by skipping the check (T264), then by claiming a
        check whose keywords nothing could supply (T299).
        """
        available = frozenset(_IMAGE_TECHNIQUE_TRIGGER_TOKENS) | (
            {ScreeningTechnique.DECLARED_TEXT} if text_evidence_available else frozenset()
        )
        return reject_categories - unaddressed_reject_categories(
            reject_categories,
            available_techniques=frozenset(available),
            text_vocabulary=text_vocabulary,
        )

    def detect(
        self,
        frame: CaptureFrame,
        *,
        reject_categories: frozenset[str],
        detected_text_tokens: frozenset[str],
        text_vocabulary: TextVocabulary = NO_TEXT_VOCABULARY,
        hud_corners: frozenset[HudCorner] = frozenset(),
        statistics: FrameStatistics | None = None,
    ) -> frozenset[str]:
        return self.detect_detailed(
            frame,
            reject_categories=reject_categories,
            detected_text_tokens=detected_text_tokens,
            text_vocabulary=text_vocabulary,
            hud_corners=hud_corners,
            statistics=statistics,
        ).matches

    def detect_detailed(
        self,
        frame: CaptureFrame,
        *,
        reject_categories: frozenset[str],
        detected_text_tokens: frozenset[str],
        text_vocabulary: TextVocabulary = NO_TEXT_VOCABULARY,
        hud_corners: frozenset[HudCorner] = frozenset(),
        statistics: FrameStatistics | None = None,
    ) -> DetectionReport:
        """:meth:`detect`, plus the statistics and the per-technique verdicts behind it (T297).

        *statistics* lets the caller hand in the :class:`FrameStatistics` it is going to persist,
        so the numbers in the record are literally the numbers this decision was taken on rather
        than a second, separately-decoded derivation of them. Omitted, they are derived here.
        """
        stats = (
            statistics
            if statistics is not None
            else derive_frame_statistics(frame, hud_corners=hud_corners)
        )
        if not reject_categories:
            return DetectionReport(
                matches=frozenset(),
                statistics=stats,
                techniques_run=frozenset(),
                techniques_fired=frozenset(),
            )

        if not stats.decoded:
            # Research R7: an image that cannot be proven clean is withheld, never passed on the
            # assumption it is probably fine. No technique ran, and the report says so -- the
            # record must not later read as "three techniques examined this and one fired".
            return DetectionReport(
                matches=frozenset(reject_categories),
                statistics=stats,
                techniques_run=frozenset(),
                techniques_fired=frozenset(),
            )

        matches: set[str] = set()
        techniques_run: set[ScreeningTechnique] = set()
        techniques_fired: set[ScreeningTechnique] = set()

        def _categories_for(technique: ScreeningTechnique) -> set[str]:
            return {
                c
                for c in reject_categories
                if technique in techniques_for_category(c, text_vocabulary=text_vocabulary)
            }

        border_categories = _categories_for(ScreeningTechnique.BORDER_RING)
        if border_categories:
            techniques_run.add(ScreeningTechnique.BORDER_RING)
            if stats.border_ring_is_suspect:
                techniques_fired.add(ScreeningTechnique.BORDER_RING)
                matches.update(border_categories)

        overlay_categories = _categories_for(ScreeningTechnique.CORNER_OVERLAY)
        if overlay_categories:
            techniques_run.add(ScreeningTechnique.CORNER_OVERLAY)
            if stats.corner_overlay_is_suspect:
                techniques_fired.add(ScreeningTechnique.CORNER_OVERLAY)
                matches.update(overlay_categories)

        text_categories = _categories_for(ScreeningTechnique.DECLARED_TEXT)
        if text_categories:
            techniques_run.add(ScreeningTechnique.DECLARED_TEXT)
        for category in text_categories:
            # T299: the words that indicate this category are declared in the catalog, not split
            # out of the category's own name. Any one group matching is a match; every word in
            # that group must be present, so a single stray token still cannot fire the gate.
            if any(group <= detected_text_tokens for group in text_vocabulary[category]):
                techniques_fired.add(ScreeningTechnique.DECLARED_TEXT)
                matches.add(category)

        return DetectionReport(
            matches=frozenset(matches),
            statistics=stats,
            techniques_run=frozenset(techniques_run),
            techniques_fired=frozenset(techniques_fired),
        )


DEFAULT_CONTENT_DETECTOR: Final[DefaultContentDetector] = DefaultContentDetector()


def _content_metrics(
    *,
    profile_name: str,
    statistics: FrameStatistics,
    text_evidence_available: bool,
    techniques_run: frozenset[ScreeningTechnique],
    techniques_fired: frozenset[ScreeningTechnique],
    matches: frozenset[str],
) -> CaptureScreeningMetrics:
    """Assemble the persisted record from the statistics this attempt actually computed.

    Nothing is measured here -- everything comes from *statistics*, which was derived once from
    the decoded frame. This function only reshapes it, and drops the one thing that may not be
    kept: the declared-text technique's evidence becomes the boolean ``text_match``.
    """
    return CaptureScreeningMetrics(
        profile_name=profile_name,
        frame_width=statistics.width,
        frame_height=statistics.height,
        whole_frame_variance=statistics.whole_frame_variance,
        corner_metrics=list(statistics.corners),
        border_edge_metrics=list(statistics.border_edges),
        text_evidence_available=text_evidence_available,
        text_match=ScreeningTechnique.DECLARED_TEXT in techniques_fired,
        techniques_run=sorted(techniques_run),
        techniques_fired=sorted(techniques_fired),
        reject_categories_matched=sorted(matches),
    )


def _check_content(
    attempt: CaptureAttempt,
    declaration: ParityDeclaration,
    *,
    profiles: ScreeningProfiles,
    detector: ContentDetector,
) -> tuple[str | None, CaptureScreeningMetrics | None]:
    """Run the content gate, returning its failure detail (if any) **and** what it measured.

    The metrics are the T297 half. They are returned on *every* path that reached a frame --
    clean, matched, and "no technique covers this category" alike -- because a detector is only
    evaluable against both populations, and because the coverage withhold is the shape that
    accounts for most of a Linux run's withholds today. Recording only the frames the detector
    got as far as judging would rebuild the same blind spot one gate earlier.
    """
    declared_profile_key = declaration.screening_profile
    assert declared_profile_key is not None  # guaranteed for kind == view

    try:
        profile = resolve_screening_profile(
            profiles, declared_profile_key=declared_profile_key, platform=attempt.platform
        )
    except CatalogError as exc:
        # Unreachable through a loaded catalog (ParityDeclaration validates the declared value at
        # load), and deliberately not fatal here: a run must not die mid-turn over a declaration,
        # and it must not screen against a profile nobody asked for either. Withheld, saying so.
        # No profile resolved means no profile name to group a statistic under, and no reject set
        # to say what was looked for. The frame is withheld and nothing is recorded about it --
        # an unlabelled measurement is not evidence.
        return (
            f"the view's declared screening_profile could not be resolved: {exc.message} "
            f"(detail: {exc.detail}); no profile means no certified screening",
            None,
        )

    hud_corners = frozenset(declaration.hud_corners or ())
    text_evidence_available = attempt.detected_text_tokens is not None
    # T297, and the ordering that is the whole lesson: derive the statistics HERE, while the
    # frame is in hand, before any branch can return. Every exit below either carries them or is
    # a path where no frame was ever measured; none of them can be reconstructed later, because
    # a withheld frame is never stored and must not be.
    statistics = derive_frame_statistics(attempt.frame, hud_corners=hud_corners)

    def metrics_for(
        *,
        techniques_run: frozenset[ScreeningTechnique] = frozenset(),
        techniques_fired: frozenset[ScreeningTechnique] = frozenset(),
        matches: frozenset[str] = frozenset(),
    ) -> CaptureScreeningMetrics:
        return _content_metrics(
            profile_name=profile.name,
            statistics=statistics,
            text_evidence_available=text_evidence_available,
            techniques_run=techniques_run,
            techniques_fired=techniques_fired,
            matches=matches,
        )

    # Coverage BEFORE findings (Principle I). "No technique reported a match" only means the
    # frame is clean for the categories some technique actually examined; for any other category
    # it means nothing was looked at, and this gate used to return that silence as a pass. A
    # detector that cannot say what it covers is taken to cover nothing.
    vocabulary = profiles.text_vocabulary
    declare_coverage = getattr(detector, "addressable_categories", None)
    covered: frozenset[str] = (
        declare_coverage(
            profile.reject,
            text_evidence_available=attempt.detected_text_tokens is not None,
            text_vocabulary=vocabulary,
        )
        if callable(declare_coverage)
        else frozenset()
    )
    # T299: a category the capture path cannot structurally contain is covered without a
    # technique, and only where the catalog declares both the platform's capture scope and the
    # category's origin. This is the one legitimate form of "nothing screens this": the frame
    # cannot contain it at all. It is kept separate from technique coverage on purpose, so a
    # withhold reason can never read as "screened" for a category nothing examined.
    excluded = profiles.structurally_excluded(profile)
    unscreened = profile.reject - covered - excluded
    if unscreened:
        noun = "category" if len(unscreened) == 1 else "categories"
        # The statistics are still recorded: the frame was measured even though the gate could
        # not certify it, and this is the withhold shape a Linux run produces most of.
        return (
            f"no available screening technique addresses reject {noun} {sorted(unscreened)} "
            f"of profile {profile.name!r} (capture_scope {profile.capture_scope.name!r}); the "
            "frame cannot be certified against it (withheld rather than passed unscreened)",
            metrics_for(),
        )

    detect_detailed = getattr(detector, "detect_detailed", None)
    if callable(detect_detailed):
        report: DetectionReport = detect_detailed(
            attempt.frame,
            reject_categories=profile.reject,
            detected_text_tokens=attempt.detected_text_tokens or frozenset(),
            text_vocabulary=vocabulary,
            # T283: the view's own statement of where the game keeps its HUD. Absent means
            # nothing is declared, which is the strict reading (every corner judged against the
            # whole frame).
            hud_corners=hud_corners,
            # The same object that will be persisted -- so "what was recorded" and "what was
            # decided on" are one computation, not two that agree today.
            statistics=statistics,
        )
        matches = report.matches
        metrics = metrics_for(
            techniques_run=report.techniques_run,
            techniques_fired=report.techniques_fired,
            matches=report.matches,
        )
    else:
        # An injected detector that does not report its own technique verdicts. Its findings are
        # honoured, and the image statistics still stand (this module measured them, not the
        # detector); what cannot be claimed is which technique fired, so nothing is.
        matches = detector.detect(
            attempt.frame,
            reject_categories=profile.reject,
            detected_text_tokens=attempt.detected_text_tokens or frozenset(),
            text_vocabulary=vocabulary,
            hud_corners=hud_corners,
        )
        metrics = metrics_for(matches=matches)

    if matches:
        noun = "category" if len(matches) == 1 else "categories"
        return f"content gate matched reject {noun}: {sorted(matches)}", metrics
    return None, metrics


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
    #: T297: what the content gate measured, for the record. ``None`` exactly when no frame
    #: reached the content gate -- an earlier gate short-circuited, or the caller built this
    #: outcome for a host-level failure where there is no frame at all. Optional with a default
    #: so every existing construction of this dataclass stays valid; a caller that cannot supply
    #: metrics is stating that none were derived, which is the honest reading.
    metrics: CaptureScreeningMetrics | None = None

    @property
    def is_clean(self) -> bool:
        return self.status is ScreeningStatus.SCREENED_CLEAN


_CLEAN_OUTCOME: Final[ScreeningOutcome] = ScreeningOutcome(
    status=ScreeningStatus.SCREENED_CLEAN, withheld_reason=None, failed_gate=None, detail=None
)


def _clean(metrics: CaptureScreeningMetrics | None) -> ScreeningOutcome:
    """A clean outcome carrying what the gate measured to reach it.

    Clean captures are recorded with statistics too, and that is not symmetry for its own sake: a
    detector is only evaluable against **both** populations. The retro-audit's 188 delivered
    frames are the one real clean sample this project has, and they exist only as prose in a
    spike document because nothing ever persisted the numbers.
    """
    if metrics is None:
        return _CLEAN_OUTCOME
    return ScreeningOutcome(
        status=ScreeningStatus.SCREENED_CLEAN,
        withheld_reason=None,
        failed_gate=None,
        detail=None,
        metrics=metrics,
    )


def _withhold(
    gate: ScreeningGate, detail: str, metrics: CaptureScreeningMetrics | None = None
) -> ScreeningOutcome:
    return ScreeningOutcome(
        status=ScreeningStatus.WITHHELD,
        withheld_reason=_GATE_WITHHELD_REASON[gate],
        failed_gate=gate,
        detail=detail,
        metrics=metrics,
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

    content_failure, metrics = _check_content(
        attempt, declaration, profiles=profiles, detector=resolved_detector
    )
    if content_failure is not None:
        return _withhold(ScreeningGate.CONTENT, content_failure, metrics)

    return _clean(metrics)


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
        # T297. This is the *only* trace of what the gate saw on a withheld frame, and it has to
        # be attached here because there is no later moment at which it could be: the pixels are
        # gone by the time this record is written and, for a withheld capture, they were never
        # ours to keep. Note it is not conditioned on ``is_clean`` -- unlike the blob, which is
        # forced to ``None`` above, the statistics are exactly as necessary on the withheld side.
        screening_metrics=outcome.metrics,
    )
