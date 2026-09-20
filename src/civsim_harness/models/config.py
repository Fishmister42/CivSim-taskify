"""SeedSet, RunConfiguration, GuidanceSet, StopCondition, ModelConfig (T018, T019).

data-model.md SS1-3. Together with T019 (folded into this same file as
``RunConfiguration``'s bound fields), these are the complete recorded
definition FR-001 requires before a run may start.

There is deliberately no ``turn_time_budget_s`` field anywhere in this
module. The clarification session removed the per-turn time budget
entirely in favour of ``no_progress_step_limit`` (FR-008, FR-014); a field
of that name reappearing here would be a regression, not an addition.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from civsim_harness.models.common import (
    BuildAcceptance,
    ConfigId,
    GuidanceSetId,
    HarnessModel,
    ModelRef,
    ModRef,
    SeedSetId,
    Timestamp,
)
from civsim_harness.telemetry.redaction import redact_value

# --------------------------------------------------------------------------
# 1. SeedSet
# --------------------------------------------------------------------------


class SeedSet(HarnessModel):
    """A named collection of map seeds sharing a civilization, ruleset, and game build (FR-031).

    ``civilization``, ``leader``, ``ruleset``, and ``mod_set`` are immutable
    after creation -- pydantic's per-field ``frozen=True`` rejects any
    attempt to reassign them once the model exists. Varying one of them
    describes a different, incomparable set of runs, not a variation within
    this one. ``seeds`` may still be appended to (it is not frozen).
    """

    seed_set_id: SeedSetId
    name: str
    seeds: list[str] = Field(min_length=1)
    civilization: str = Field(frozen=True)
    leader: str = Field(frozen=True)
    ruleset: str = Field(frozen=True)
    mod_set: list[ModRef] = Field(default_factory=list, frozen=True)
    # Composite platform/version identity, e.g. "win/1.0.12.9" (FR-031, R18, R20).
    game_build: str
    accepted_build_changes: list[BuildAcceptance] = Field(default_factory=list)
    created_at: Timestamp

    @property
    def is_uniform(self) -> bool:
        """A set carrying any accepted build change is not uniform and must report as such."""
        return len(self.accepted_build_changes) == 0


# --------------------------------------------------------------------------
# StopCondition -- exactly one of turn_reached(n) / game_outcome / operator_stop
# --------------------------------------------------------------------------


class TurnReachedStopCondition(HarnessModel):
    type: Literal["turn_reached"] = "turn_reached"
    turn: int = Field(ge=1)


class GameOutcomeStopCondition(HarnessModel):
    type: Literal["game_outcome"] = "game_outcome"


class OperatorStopStopCondition(HarnessModel):
    type: Literal["operator_stop"] = "operator_stop"


StopCondition = Annotated[
    TurnReachedStopCondition | GameOutcomeStopCondition | OperatorStopStopCondition,
    Field(discriminator="type"),
]
"""A run's configured stop condition: exactly one of the three variants above.

This is deliberately a 3-way union, not the broader recorded outcome set --
see ``Run.stop_resolution`` in models/run.py for the 5-value recorded set
that additionally includes ``victory``, ``defeat``, and
``unrecoverable_failure``. Those three are things a run can *end with*, not
things an operator can *configure it to stop at*, and conflating the two
would let an unconfigurable outcome be requested as if it were a condition.
"""


# --------------------------------------------------------------------------
# ModelConfig
# --------------------------------------------------------------------------


class ModelConfig(HarnessModel):
    """Primary model plus an ordered fallback chain. Carries no credentials (FR-043).

    Keys resolve from environment or a secrets file at runtime, never from
    this record. ``request_params`` is validated at construction time to
    reject any credential-shaped key outright -- the strongest guarantee
    available for a structured field, and independent of (in addition to)
    the general-purpose redaction filter that protects everything else
    reachable from a serialized record or log line (T014).
    """

    primary: ModelRef
    fallbacks: list[ModelRef] = Field(default_factory=list)
    request_params: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _reject_credential_shaped_params(self) -> ModelConfig:
        redacted = redact_value(dict(self.request_params))
        if redacted != self.request_params:
            raise ValueError(
                "model_config.request_params must not contain a credential-shaped key; "
                "keys resolve from the environment or a secrets file at runtime (FR-043)"
            )
        return self


# --------------------------------------------------------------------------
# GuidanceSet
# --------------------------------------------------------------------------


class GuidanceSet(HarnessModel):
    """Run-independent strategic guidance supplied to the agent (FR-021).

    Must contain no state from the current run and no hidden state -- a
    review-time rule this model cannot enforce on its own, plus the runtime
    invariant it *can* enforce: identical content hashes to identical
    content (checked by whatever loads/caches these against other instances
    sharing a hash, not here, since a single instance has nothing to compare
    against).
    """

    guidance_set_id: GuidanceSetId
    content_hash: str
    content: str
    source_ref: str | None = None


# --------------------------------------------------------------------------
# 2. RunConfiguration
# --------------------------------------------------------------------------


class RunConfiguration(HarnessModel):
    """The complete recorded definition of a run before it starts (FR-001).

    Deliberately has no ``turn_time_budget_s`` field (see module docstring).
    ``no_progress_step_limit`` has no upper bound -- any validator that
    imposed one would silently cap a turn, which FR-014 forbids.

    Naming note for callers: the wire/record field is ``model_config``
    (contracts/run-configuration.md), which collides with
    ``pydantic.BaseModel``'s own reserved ``model_config`` class attribute.
    The Python attribute here is therefore ``agent_model_config``, with
    ``model_config`` kept as its serialization alias via
    ``populate_by_name=True`` -- so YAML/JSON round-trips still use the key
    ``model_config``, but ``instance.model_config`` in Python is (and must
    stay) pydantic's own config dict, never this field. Use
    ``.model_dump(by_alias=True)`` to get the ``model_config`` key back out.
    """

    model_config = {**HarnessModel.model_config, "populate_by_name": True}

    config_id: ConfigId
    seed_set_id: SeedSetId | None = None
    map_seed: str
    civilization: str
    leader: str
    ruleset: str
    # Exact set with versions; an empty list is meaningful ("no mods"), not
    # the absence of a decision about mods.
    mod_set: list[ModRef] = Field(default_factory=list)
    map_settings: dict[str, Any] = Field(default_factory=dict)
    game_settings: dict[str, Any] = Field(default_factory=dict)
    difficulty: str
    opponents: dict[str, Any] = Field(default_factory=dict)
    stop_condition: StopCondition
    agent_model_config: ModelConfig = Field(alias="model_config")
    guidance_set_id: GuidanceSetId | None = None
    # Consecutive no-progress decision steps that end a turn (FR-014).
    # No upper bound by design -- ge=1 only.
    no_progress_step_limit: int = Field(ge=1)
    recovery_attempt_limit: int = Field(ge=1)
    min_free_disk_gb: float = Field(ge=0)
    created_at: Timestamp
