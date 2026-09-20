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
    introduced_in_version: str

    @model_validator(mode="after")
    def _kind_specific_shape(self) -> ParityDeclaration:
        if not self.parity_basis.strip():
            raise ValueError("parity_basis must be non-empty")

        if self.kind == DeclarationKind.VIEW:
            if self.camera_requirements is None:
                raise ValueError("camera_requirements is required when kind == view")
            if not self.screening_profile:
                raise ValueError("screening_profile is required when kind == view")
        else:
            if self.camera_requirements is not None:
                raise ValueError("camera_requirements must be absent unless kind == view")
            if self.screening_profile is not None:
                raise ValueError("screening_profile must be absent unless kind == view")

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
