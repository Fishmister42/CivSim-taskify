"""Shared value types for every record model (T017).

Defined once here because every other model in :mod:`civsim_harness.models`
imports from this module: id types (kept distinct per entity via
``NewType`` so a ``RunId`` can never be silently accepted where a
``TurnCycleId`` was meant), the small cross-entity value objects
(``ModRef``, ``ModelRef``, ``Cost``, ``CatalogVersionRef``,
``BuildAcceptance``), and the timestamp alias.

``HarnessModel`` is the shared pydantic base for every record and value
type in this package: ``extra="forbid"`` so a typo'd or stray field is a
validation error rather than silently accepted (and so JSON Schema export
in T027 produces ``additionalProperties: false``, which is what makes an
additive-only schema violation in T028 detectable).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import NewType

from pydantic import BaseModel, ConfigDict

# --------------------------------------------------------------------------
# Base model
# --------------------------------------------------------------------------


class HarnessModel(BaseModel):
    """Shared pydantic base for every harness record and value type."""

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# Id types
# --------------------------------------------------------------------------
#
# Each entity's id is its own NewType over str so that mypy rejects passing
# one entity's id where another's is expected, even though both are plain
# strings at runtime (pydantic validates a NewType against its supertype
# transparently, so YAML/JSON input needs no special handling).

SeedSetId = NewType("SeedSetId", str)
ConfigId = NewType("ConfigId", str)
GuidanceSetId = NewType("GuidanceSetId", str)
RunId = NewType("RunId", str)
TurnCycleId = NewType("TurnCycleId", str)
DecisionStepId = NewType("DecisionStepId", str)
ObservationId = NewType("ObservationId", str)
DecisionId = NewType("DecisionId", str)
ModelCallId = NewType("ModelCallId", str)
CaptureId = NewType("CaptureId", str)
SavePointId = NewType("SavePointId", str)
EventId = NewType("EventId", str)
AcceptanceId = NewType("AcceptanceId", str)

# Catalog identifiers are stable, human-readable strings (e.g.
# "units.move_to"), not generated ids -- kept as distinct NewTypes anyway so
# they cannot be confused with a record id or with each other.
DeclarationId = NewType("DeclarationId", str)
CapabilityId = NewType("CapabilityId", str)

# --------------------------------------------------------------------------
# Timestamps
# --------------------------------------------------------------------------

Timestamp = datetime
"""Alias for the timestamp type used on every out-of-game timing field."""


# --------------------------------------------------------------------------
# Shared enums (used by more than one entity's model file)
# --------------------------------------------------------------------------


class LuaContext(StrEnum):
    """Which Lua execution context a catalog entry or observation entry came from.

    Shared by ``ObservationEntry.context`` (models/turn.py) and
    ``ParityDeclaration.context`` (models/catalog.py) -- both name the same
    two contexts (contracts/capability-catalog.md), so it is defined once
    here rather than twice.
    """

    GAME_CORE_TUNER = "GameCore_Tuner"
    IN_GAME = "InGame"


class CapturePath(StrEnum):
    """Which screen-capture mechanism served a run or a capture (research R6).

    One member per platform's decided primary/fallback path, plus ``NONE``
    for a run with no capture mechanism available at all (visually degraded
    under FR-050 -- valid and recorded, never an error on its own).
    ``Run.capture_path`` may be ``NONE``; ``ScreenCapture.capture_path``
    never is, since a capture record only exists once some mechanism was
    actually attempted.
    """

    WINDOWS_GRAPHICS_CAPTURE = "windows_graphics_capture"
    SCREEN_CAPTURE_KIT = "screencapturekit"
    CG_WINDOW_LIST_IMAGE = "cg_window_list_image"
    XCOMPOSITE = "xcomposite"
    PIPEWIRE_PORTAL = "pipewire_portal"
    NONE = "none"


# --------------------------------------------------------------------------
# Shared value types
# --------------------------------------------------------------------------


class ModRef(BaseModel):
    """A mod reference: id + version. Immutable by convention (mods are pinned, not mutated)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    version: str


class ModelRef(BaseModel):
    """A model reference: provider + model name (contracts/model-provider-port.md)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    model: str


class Cost(HarnessModel):
    """Cost as reported by the provider layer's usage data; never independently priced.

    All fields are optional because they come verbatim from whatever a given
    provider reports -- some providers report token counts, some report a
    dollar amount, some report both (contracts/model-provider-port.md P7).
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    amount_usd: float | None = None


class CatalogVersionRef(HarnessModel):
    """A lightweight reference to the catalog version in force for a run/observation/decision."""

    version: str
    content_hash: str


class BuildAcceptance(HarnessModel):
    """An operator-accepted deviation from a seed set's pinned game build (FR-002, R20).

    Recorded once on the seed set and referenced (via ``AcceptanceId``) by
    every run that relied on it.
    """

    acceptance_id: AcceptanceId
    from_build: str
    to_build: str
    accepted_by: str
    accepted_at: Timestamp
    reason: str
