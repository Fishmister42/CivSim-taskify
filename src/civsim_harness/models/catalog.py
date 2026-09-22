"""ParityDeclaration, CatalogVersion, IntegrationCapability (T025).

data-model.md SS10-11, as amended: ``ParityDeclaration`` now carries
``camera_requirements`` and ``screening_profile``, both required exactly
when ``kind == view`` and absent otherwise, mirroring the ``view`` entry
shape in contracts/capability-catalog.md exactly (including
``camera_requirements``'s own sub-fields: ``mode``, ``zoom_range``, and
``target_must_be_revealed``). Without these fields the catalog loader and
the screening pipeline have nothing to validate a capture's camera state
against.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import Field, model_validator

from civsim_harness.models.common import CapabilityId, DeclarationId, HarnessModel, LuaContext

# --------------------------------------------------------------------------
# 10. ParityDeclaration
# --------------------------------------------------------------------------


class DeclarationKind(StrEnum):
    OBSERVATION = "observation"
    VIEW = "view"
    ACTION = "action"


class TargetKind(StrEnum):
    """What an action's single ``parameters.target`` names (T256, FR-020/FR-024).

    MEASURED (2026-09-21, attempts 4-5 of the first model-driven runs): the one ``target`` field
    carries a unit id, a plot, a technology name or a prompt option depending on the action, and
    the model sent the warrior's *unit id* as ``units.move_to``'s target sixteen times in a row
    even after the summary said ``{"target": {"x": .., "y": ..}}``. The declaration now says the
    kind, and ``agent/context.py`` renders one concrete example per action from it -- what a human
    sees as the shape of the command, no predicate text (Principle I / FR-024). Additive: an
    action without it renders as before. The alternative -- separate ``subject`` and ``target``
    parameters -- changes the predicate binder, the Lua argument convention and every unit/city
    order, and is recorded in tasks.md T256 for the owner's ruling rather than taken.
    """

    NONE = "none"
    PLOT = "plot"
    UNIT_ID = "unit_id"
    CITY_ID = "city_id"
    PLAYER_ID = "player_id"
    RESOLUTION_ID = "resolution_id"
    INDIVIDUAL_ID = "individual_id"
    SPY_ID = "spy_id"
    NAME = "name"
    OPTION = "option"
    NUMBER = "number"


class CameraMode(StrEnum):
    """contracts/capability-catalog.md ``view`` entries."""

    WORLD = "world"
    STRATEGIC = "strategic"
    CITY_SCREEN = "city_screen"
    DIPLOMACY = "diplomacy"
    CONGRESS = "congress"


class CameraRequirements(HarnessModel):
    """Required on every ``view`` declaration (FR-024, FR-026)."""

    mode: CameraMode
    # [min, max]; must be within what the standard UI allows.
    zoom_range: tuple[float, float]
    # FR-026 -- a view may not point at an unrevealed plot.
    target_must_be_revealed: bool


class ScreeningProfileDeclaration(StrEnum):
    """The closed vocabulary a view's ``screening_profile`` may use (T292).

    MEASURED, 2026-09-22 (``spikes/frame-retro-audit-2026-09-22.md``): 34 content-gate withholds
    across ``run-227998759f75`` and ``run-fd9c08a1df1d`` named ``windows_capture_border`` on hosts
    that recorded ``os=linux, session_type=x11``. A Windows recording-border category cannot apply
    to an XComposite frame, so those withholds were a check running against the wrong data and
    being counted as protection. The cause was this field: the views declared ``default``, which
    resolves to the strictest *union* profile on every host, so Linux frames were screened for
    Windows chrome. 98c71bb changed the declaration to ``platform`` -- a value that is **not a key**
    in ``catalogs/screening_profiles.yaml`` and only worked because
    ``parity.screening.resolve_screening_profile`` treated *every* non-``default`` string as
    "resolve by host platform". Under that rule ``platfrom``, ``Default`` and ``linux-x11`` would
    all have resolved silently too, and the one value that meant something specific -- ``default``
    -- silently meant "screen this Linux frame for Windows chrome".

    Naming the two legal values makes the declaration say what it does: ``default`` is a deliberate
    request for the strictest union profile regardless of host; ``platform`` is a request for the
    running host's own profile (falling back to the strictest one when the host has no dedicated
    entry, never to an absent check). Anything else is a typo or a profile key that does not exist,
    and fails here, at catalog load, rather than resolving to something that merely looks strict.
    """

    DEFAULT = "default"
    PLATFORM = "platform"


class HudCorner(StrEnum):
    """A frame corner a view declares as holding the game's own HUD (T283).

    See ``parity.screening._corner_overlay_is_suspect`` for what the content gate does with it:
    a declared corner is not exempt, it is judged against the *other declared HUD corners*
    instead of against the whole frame, which is what makes the detector sharper rather than
    more permissive.
    """

    TOP_LEFT = "top_left"
    TOP_RIGHT = "top_right"
    BOTTOM_LEFT = "bottom_left"
    BOTTOM_RIGHT = "bottom_right"


class ParityDeclaration(HarnessModel):
    """One observable, visual view, or action, with its human equivalent
    (data-model.md SS10; contracts/capability-catalog.md).

    ``parity_basis`` is required and non-empty for every kind (the
    enforcement point for FR-016/FR-017). ``camera_requirements`` and
    ``screening_profile`` are required exactly when ``kind == view`` and
    forbidden otherwise -- enforced below rather than left to the catalog
    loader alone, since a malformed declaration should never validate
    successfully as this type in the first place.
    """

    declaration_id: DeclarationId
    kind: DeclarationKind
    summary: str
    parity_basis: str = Field(min_length=1)
    context: LuaContext
    capability_id: CapabilityId
    availability_predicate: str | None = None
    verification_predicate: str | None = None
    output_schema: dict[str, Any] | None = None
    camera_requirements: CameraRequirements | None = None
    screening_profile: str | None = None
    # T283, views only, optional and additive. Which corners of this view's frame hold the game's
    # own HUD -- declared per view because it is a property of the screen being looked at, not of
    # the detector. Absent (or empty) means "nothing declared", which is the strictest reading and
    # the one every view had before: every corner is then judged against the whole frame.
    hud_corners: tuple[HudCorner, ...] | None = None
    introduced_in_version: str
    # T256: actions only, optional (additive). `target_hint` is one short human-facing sentence
    # rendered after the kind's example, e.g. "a destination plot from the selected unit's
    # reachable_plots -- the plot, not the unit".
    target_kind: TargetKind | None = None
    target_hint: str | None = None

    @model_validator(mode="after")
    def _kind_specific_shape(self) -> ParityDeclaration:
        if not self.parity_basis.strip():
            raise ValueError("parity_basis must be non-empty")
        if self.kind is not DeclarationKind.ACTION and (
            self.target_kind is not None or self.target_hint is not None
        ):
            raise ValueError("target_kind / target_hint are only meaningful on an action")
        if self.target_hint is not None and self.target_kind is None:
            raise ValueError("target_hint requires target_kind")

        if self.kind == DeclarationKind.VIEW:
            if self.camera_requirements is None:
                raise ValueError("camera_requirements is required when kind == view")
            if not self.screening_profile:
                raise ValueError("screening_profile is required when kind == view")
            # T292: a closed vocabulary, checked at load. An unrecognised value used to resolve
            # silently -- by host platform, because that was the catch-all branch -- so a typo
            # screened frames against whichever profile the host happened to have, and the one
            # recognised value (``default``) screened Linux frames for Windows chrome. Both read
            # as a working gate in the record. See ScreeningProfileDeclaration for the measurement.
            if self.screening_profile not in set(ScreeningProfileDeclaration):
                raise ValueError(
                    f"screening_profile {self.screening_profile!r} is not a declarable value; "
                    f"use one of {sorted(v.value for v in ScreeningProfileDeclaration)} "
                    "(a platform profile key such as 'linux' is resolved *from* the running host "
                    "by 'platform'; it is not declared here)"
                )
            if self.hud_corners is not None and len(set(self.hud_corners)) != len(self.hud_corners):
                raise ValueError("hud_corners must not repeat a corner")
        else:
            if self.camera_requirements is not None:
                raise ValueError("camera_requirements must be absent unless kind == view")
            if self.screening_profile is not None:
                raise ValueError("screening_profile must be absent unless kind == view")
            if self.hud_corners is not None:
                raise ValueError("hud_corners must be absent unless kind == view")

        if self.kind == DeclarationKind.ACTION:
            if not self.availability_predicate:
                raise ValueError("availability_predicate is required for an action declaration")
            if not self.verification_predicate:
                raise ValueError("verification_predicate is required for an action declaration")
        elif self.output_schema is None:
            raise ValueError("output_schema is required for an observation or view declaration")

        return self


class CatalogVersion(HarnessModel):
    """The catalog version in force for a run (FR-022)."""

    version: str
    content_hash: str
    declaration_ids: list[DeclarationId] = Field(default_factory=list)


# --------------------------------------------------------------------------
# 11. IntegrationCapability
# --------------------------------------------------------------------------


class CapabilityPath(StrEnum):
    FIRETUNER = "firetuner"
    BESPOKE = "bespoke"


class IntegrationCapability(HarnessModel):
    """The implementation behind one or more declarations (FR-027-FR-029).

    ``path == bespoke`` without a non-empty ``firetuner_gap`` fails catalog
    load (FR-028, SC-020) -- enforced below. Conversely a ``firetuner`` path
    carries no gap statement at all (``null`` in the YAML shape), so one
    being present there is equally rejected.
    """

    capability_id: CapabilityId
    path: CapabilityPath
    implementation_ref: str
    reads: list[str] = Field(default_factory=list)
    writes: list[str] = Field(default_factory=list)
    firetuner_gap: str | None = None
    # Optional (data-model.md SS11): "inherited or restated". Every
    # ParityDeclaration this capability implements already carries its own
    # required, non-empty parity_basis (enforced above in
    # ParityDeclaration), so omitting it here means the capability's parity
    # basis is inherited from those declarations. A value present here is a
    # restatement and, like the declaration-level field, must be non-empty --
    # never an empty string standing in for "absent".
    parity_basis: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _bespoke_requires_gap(self) -> IntegrationCapability:
        if self.path == CapabilityPath.BESPOKE and not (self.firetuner_gap or "").strip():
            raise ValueError(
                "firetuner_gap is required and non-empty when path == bespoke (FR-028, SC-020)"
            )
        if self.path == CapabilityPath.FIRETUNER and self.firetuner_gap is not None:
            raise ValueError("firetuner_gap must be absent (null) when path == firetuner")
        return self
