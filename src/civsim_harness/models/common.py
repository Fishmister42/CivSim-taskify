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

from pydantic import BaseModel, ConfigDict, model_validator

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
    """A mod reference: id + version. Immutable by convention (mods are pinned, not mutated).

    ``id`` is the mod's GUID as the client reports it. GUIDs are case-insensitive by definition
    and the live client reports them in **mixed** case -- ``Modding.GetActiveMods()`` on Linux
    returns lower-case ids for workshop mods and upper-case ids for official content in the same
    list (T251, measured 2026-09-20) -- so every comparison normalises on ``id.lower()``
    (``run/preparation.py``'s ``canonical_mod_set``, ``config/seed_set.py``'s ``_mod_set_key``).

    ``version`` is ``None`` for a mod that reports no version. Official Firaxis content
    (expansions, DLC and persona packs) carries no ``Version`` property at all, while workshop mods
    do -- read through ``Modding.GetModProperty(handle, "Version")``, never off the
    ``GetActiveMods()`` entry, which has no such field (T251). A ``None`` pin means "pinned on id
    only"; it is not a wildcard against a mod that *does* report a version, which is compared.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    version: str | None = None


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


def _build_platform(build: str) -> str:
    """The platform component of a composite ``<platform>/<version>`` build
    string (research R20).

    Deliberately duplicated rather than imported from
    ``observe.game_build.split_build``: ``models/`` is the foundation layer
    every other module in this package imports from (module docstring
    above), never the reverse, and this one-line split needs none of that
    module's stricter error handling to serve as ``BuildAcceptance``'s own
    derivation. A build string with no ``/`` is treated as all-platform,
    all-version (no split), which only ever makes two builds compare as the
    *same* platform -- the conservative direction for a derived field.
    """
    platform, _, _ = build.partition("/")
    return platform


class BuildAcceptance(HarnessModel):
    """An operator-accepted deviation from a seed set's pinned game build (FR-002, R20).

    Recorded once on the seed set and referenced (via ``AcceptanceId``) by
    every run that relied on it.

    ``is_platform_transition`` is derived (data-model.md SS1), not supplied
    by the caller: the ``model_validator`` below recomputes it from
    ``from_build``/``to_build`` after every validation, so a caller may
    omit it -- every construction site in this codebase does -- or pass any
    value and get the correct answer either way. ``r20_spike_ref`` is the
    recorded passing result of the R20 cross-platform save spike (T199);
    data-model.md marks it required when ``is_platform_transition`` is
    true, but that gate is enforced by the caller that *creates* an
    acceptance (the `seedset accept-build` command, T174) and by
    ``saves.branching.check_branch_build``, which both fail closed on a
    missing spike ref -- not by this model. A platform-crossing
    ``BuildAcceptance`` with no spike result on record is still a
    constructible value; it is simply refused wherever it would be relied
    upon.
    """

    acceptance_id: AcceptanceId
    from_build: str
    to_build: str
    accepted_by: str
    accepted_at: Timestamp
    reason: str
    is_platform_transition: bool = False
    r20_spike_ref: str | None = None

    @model_validator(mode="after")
    def _derive_is_platform_transition(self) -> BuildAcceptance:
        self.is_platform_transition = _build_platform(self.from_build) != _build_platform(
            self.to_build
        )
        return self
