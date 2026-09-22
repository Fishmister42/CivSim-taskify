"""Run and its enums (T020).

data-model.md SS4, as amended: ``stop_resolution`` replaced the earlier
``stop_condition_recorded`` naming and is now explicitly a 5-value recorded
set -- ``turn_reached | victory | defeat | operator_stop |
unrecoverable_failure`` -- distinct from ``StopCondition.type``
(models/config.py), which stays the 3-way *configured* set
(``turn_reached | game_outcome | operator_stop``).

The recorded set is necessarily broader than the configured set:
``unrecoverable_failure`` is a way a run can end without being a
configurable condition at all, and ``victory``/``defeat`` are recorded
because a configured ``game_outcome`` condition resolves to exactly one of
those two -- so the fully-resolved *outcome* needs both names even though
the *condition* that was configured only needed one (``game_outcome``).
Nobody should ever add ``victory``/``defeat`` to ``StopCondition.type``:
they are not things an operator configures a run to stop at, they are what
a configured ``game_outcome`` stop turns out to have been.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import Field, model_validator

from civsim_harness.models.common import (
    AcceptanceId,
    CapturePath,
    CatalogVersionRef,
    ConfigId,
    HarnessModel,
    RunId,
    Timestamp,
)


class LifecycleState(StrEnum):
    """See the state machine in data-model.md SS4 (FR-003)."""

    PREPARING = "preparing"
    PLAYING = "playing"
    WAITING_ON_MODEL = "waiting_on_model"
    WAITING_ON_GAME = "waiting_on_game"
    PAUSED = "paused"
    INTERRUPTED = "interrupted"
    RESUMING = "resuming"
    FINISHED = "finished"
    FAILED = "failed"


_TERMINAL_STATES = frozenset({LifecycleState.FINISHED, LifecycleState.FAILED})


class StopResolution(StrEnum):
    """The recorded outcome of a terminated run (FR-005) -- broader than
    ``StopCondition.type`` by design; see module docstring.
    """

    TURN_REACHED = "turn_reached"
    VICTORY = "victory"
    DEFEAT = "defeat"
    OPERATOR_STOP = "operator_stop"
    UNRECOVERABLE_FAILURE = "unrecoverable_failure"


class RecordCompletenessStatus(StrEnum):
    """Derived, never asserted (FR-052, SC-003, SC-011)."""

    COMPLETE = "complete"
    HAS_GAPS = "has_gaps"
    UNKNOWN = "unknown"


class DebugMenuState(StrEnum):
    """Whether ``EnableDebugMenu`` could be read from ``AppOptions.txt`` at preflight, and if so
    what it said (T204 hardening item 1; ``spikes/principle-i-debugmenu-linux.md``).

    Lives here rather than beside its reader in ``run/preparation.py`` because it is a field of
    :class:`Run`, and ``models/`` is the foundation layer -- it may not import from ``run/``.
    ``run.preparation`` re-exports this name, so existing imports of it from there keep working.

    This is **provenance, never a gate**. The originating spike ran the tuner three ways with the
    debug menu on and off and found the callable surface byte-identical, so there is no Principle I
    tension to enforce; what the spike asked for is that a run's parity configuration be
    "reconstructible from its record alone rather than from a claim about how the host was set up",
    which is what recording this on the ``Run`` is for.
    """

    ENABLED = "enabled"
    DISABLED = "disabled"
    UNKNOWN = "unknown"


class ComparabilityStatus(StrEnum):
    """FR-050."""

    COMPARABLE = "comparable"
    VISUALLY_DEGRADED = "visually_degraded"
    NOT_COMPARABLE = "not_comparable"


class HostSupportTier(StrEnum):
    """Resolved at preflight. ``unsupported`` has no member here: an
    unsupported host cannot produce a run at all, so there is nothing to
    record (R19) -- only the two tiers a run can actually carry are
    representable.
    """

    VALIDATED = "validated"
    SUPPORTED = "supported"


class Run(HarnessModel):
    """One execution of a configuration (data-model.md SS4).

    Archival is a terminal-state action, not a lifecycle state:
    ``archived_at`` is orthogonal to ``lifecycle_state`` and is set only by
    an explicit operator command (enforced by the store's ``archive_run``,
    not here) -- this model only enforces the narrower, purely structural
    half of that: ``archived_at`` may not be set unless ``lifecycle_state``
    is already terminal.
    """

    run_id: RunId
    config_id: ConfigId
    lifecycle_state: LifecycleState
    started_at: Timestamp | None = None
    ended_at: Timestamp | None = None
    stop_resolution: StopResolution | None = None
    record_completeness_status: RecordCompletenessStatus
    comparability_status: ComparabilityStatus
    observation_catalog_version: CatalogVersionRef
    action_catalog_version: CatalogVersionRef
    parent_run_id: RunId | None = None
    parent_turn: int | None = Field(default=None, ge=1)
    # PID and tuner state indices resolved at handshake.
    client_identity: dict[str, Any] = Field(default_factory=dict)
    game_build: str
    game_build_acceptance_ref: AcceptanceId | None = None
    # T280: the host's `EnableDebugMenu` setting as read at preflight, so "was this run made with
    # the debug menu on?" is answerable from the record rather than from a claim about the host.
    # Run provenance in exactly the sense `game_build` and `host_platform` are, and structurally
    # subject to the same Principle I property they are: nothing in `Run` has a path into context
    # assembly, which consumes catalog capability outputs only (`parity/filter.py`, FR-018).
    # Optional because runs recorded before this field existed have no answer, and because the
    # reader itself can honestly return `UNKNOWN`; `None` means "this run never asked", which is a
    # different fact from `UNKNOWN` ("asked, could not tell") and must not be conflated with it.
    debug_menu_state: DebugMenuState | None = None
    host_platform: dict[str, Any] = Field(default_factory=dict)
    host_support_tier: HostSupportTier
    archived_at: Timestamp | None = None
    capture_path: CapturePath

    @model_validator(mode="after")
    def _terminal_state_invariants(self) -> Run:
        is_terminal = self.lifecycle_state in _TERMINAL_STATES
        if self.lifecycle_state == LifecycleState.FINISHED and self.stop_resolution is None:
            raise ValueError("a finished run requires exactly one stop_resolution (FR-005)")
        if not is_terminal and self.stop_resolution is not None:
            raise ValueError("stop_resolution must be unset until the run reaches a terminal state")
        if self.archived_at is not None and not is_terminal:
            raise ValueError("archived_at may only be set once the run is terminal (FR-036)")
        return self
