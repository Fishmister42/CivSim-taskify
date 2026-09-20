"""The `MatchStore` port, its composite write-bundle types, and the health
check value type (T038).

This is the contract `src/civsim_harness/store/sqlite_adapter.py` must
satisfy and every conformance test in
`tests/contract/test_match_store_port.py` asserts against --
`specs/002-civ-playing-harness/contracts/match-store-port.md` is the
normative source for every operation and durability rule named here.

Two composite types are defined in *this* module rather than in
`civsim_harness.models`, because they are store-write-bundle shapes, not
persisted domain records in their own right: `TurnCycleRecord` is the
atomic unit `write_turn_cycle` accepts (contracts/match-store-port.md
"Operations" and D3), and `DecisionStepBundle` is one of its ordered
elements. Both are assembled from the finished record models in
`models.turn` / `models.decision` / `models.records` and add only the
cross-record consistency checks that make an internally-inconsistent
bundle impossible to construct in the first place -- structural
enforcement, matching how the rest of `models/` already works.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from pydantic import Field, model_validator

from civsim_harness.models.common import (
    CaptureId,
    EventId,
    HarnessModel,
    ModelCallId,
    RunId,
    SavePointId,
    Timestamp,
    TurnCycleId,
)
from civsim_harness.models.config import RunConfiguration
from civsim_harness.models.decision import Decision
from civsim_harness.models.records import ModelCall, RunEvent, SavePoint
from civsim_harness.models.run import Run
from civsim_harness.models.turn import DecisionStep, Observation, ScreenCapture, TurnCycle

# --------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class StoreHealth:
    """The outcome of `MatchStore.ping()` (D6).

    A tagged outcome rather than a raise, mirroring the pattern
    `host.port.CaptureResult` / `InputResult` already use elsewhere in this
    codebase for "report unavailable, never raise an opaque error": `ping()`
    is a *check*, and its caller -- not this type -- decides what "not ok"
    means for preparation. D6 requires that a `ping()` failure before turn 1
    abort preparation; this type is what makes that failure legible to the
    caller rather than an opaque exception from deep inside the adapter.
    `detail` is populated exactly when `ok` is `False`.
    """

    ok: bool
    checked_at: Timestamp
    detail: str | None = None

    def __post_init__(self) -> None:
        if not self.ok and not self.detail:
            raise ValueError("StoreHealth.ok is False but no detail was given")
        if self.ok and self.detail:
            raise ValueError("StoreHealth.detail must be unset when ok is True")


# --------------------------------------------------------------------------
# TurnCycleRecord -- the atomic write-bundle write_turn_cycle accepts (D3)
# --------------------------------------------------------------------------


class DecisionStepBundle(HarnessModel):
    """One decision step plus everything it produced (data-model.md SS6-9,
    SS12), bundled as one element of an ordered `TurnCycleRecord.steps` list.

    Enforces the cross-record consistency `write_turn_cycle` depends on:
    every nested record must actually be *about* the same
    `decision_step_id` as `step` itself. This is what makes "the observation
    with its entries and capture references, the single decision with its
    reasoning, the execution outcome, and the model call that produced it"
    (contracts/match-store-port.md) a single well-formed unit rather than
    four independently-supplied objects that merely happen to sit next to
    each other in a list.
    """

    step: DecisionStep
    observation: Observation
    decision: Decision
    model_call: ModelCall

    @model_validator(mode="after")
    def _cross_record_consistency(self) -> DecisionStepBundle:
        step_id = self.step.decision_step_id
        mismatches = [
            name
            for name, actual in (
                ("observation.decision_step_id", self.observation.decision_step_id),
                ("decision.decision_step_id", self.decision.decision_step_id),
                ("model_call.decision_step_id", self.model_call.decision_step_id),
            )
            if actual != step_id
        ]
        if mismatches:
            raise ValueError(
                f"DecisionStepBundle fields disagree on decision_step_id: {mismatches} "
                f"do not match step.decision_step_id={step_id!r}"
            )
        if self.decision.model_call_id != self.model_call.model_call_id:
            raise ValueError(
                "decision.model_call_id must match model_call.model_call_id "
                "-- no fabricated decisions (FR-042, SC-012)"
            )
        return self


class TurnCycleRecord(HarnessModel):
    """The whole turn as one unit -- the atomic argument to `write_turn_cycle`
    (contracts/match-store-port.md, D3).

    `steps` must be supplied **in order** (strictly-increasing `step_index`)
    -- this is what "carrying every decision step, in order" means
    structurally: a caller cannot hand the store an unordered or
    duplicate-indexed bundle and have it silently accepted.

    Gaps (e.g. steps 1, 2, 4) are deliberately *not* rejected here: whether
    a turn is internally complete is a read-time completeness question
    (`MatchStore.step_gaps`, SC-003), not a write-time gate. The store's job
    is to durably record exactly what it was given, not to second-guess the
    harness's own bookkeeping -- and rejecting a sparse bundle here would
    make `step_gaps` untestable through the port's public write path.
    """

    turn_cycle: TurnCycle
    steps: list[DecisionStepBundle] = Field(min_length=1)

    @model_validator(mode="after")
    def _steps_belong_and_are_ordered(self) -> TurnCycleRecord:
        cycle_id = self.turn_cycle.turn_cycle_id
        previous_index: int | None = None
        for bundle in self.steps:
            if bundle.step.turn_cycle_id != cycle_id:
                raise ValueError(
                    f"step {bundle.step.decision_step_id!r} belongs to turn_cycle_id="
                    f"{bundle.step.turn_cycle_id!r}, not this record's {cycle_id!r}"
                )
            index = bundle.step.step_index
            if previous_index is not None and index <= previous_index:
                raise ValueError(
                    "TurnCycleRecord.steps must be strictly ascending by step_index "
                    f"(order preservation) -- got {previous_index} then {index}"
                )
            previous_index = index
        if self.turn_cycle.step_count != len(self.steps):
            raise ValueError(
                f"turn_cycle.step_count={self.turn_cycle.step_count} does not match "
                f"len(steps)={len(self.steps)}"
            )
        return self


# --------------------------------------------------------------------------
# The port
# --------------------------------------------------------------------------


@runtime_checkable
class MatchStore(Protocol):
    """The store every harness write and recovery/branching/retention read
    goes through (contracts/match-store-port.md).

    Nine writes (including `archive_run`), eight reads (including
    `step_gaps` and `list_eligible_save_points`), and `ping()`. Every write
    is synchronous-durable: see the D1-D6 durability contract on each
    method below and in the contract document. Later waves bind to this
    `Protocol`, never to `SqliteMatchStore` directly, so that deliverable
    3's real store is a drop-in configuration change (plan Complexity
    Tracking C2).
    """

    # --- writes (all synchronous-durable; see contracts/match-store-port.md
    # "Durability contract") ---

    def create_run(self, run: Run, config: RunConfiguration) -> RunId:
        """Durably record a new run and its configuration. Raises on failure (D2)."""
        ...

    def update_run(self, run_id: RunId, **fields: Any) -> None:
        """Durably update fields on an existing run.

        Must never accept `archived_at` -- that field has exactly one
        writer, `archive_run` (FR-036, A2). Raises on failure, including an
        attempt to set `archived_at` here (D2).
        """
        ...

    def write_turn_cycle(self, record: TurnCycleRecord) -> TurnCycleId:
        """Durably record one whole turn attempt, atomically (D1, D3).

        Returns only after the record is durable. Idempotent on
        `(run_id, turn_number, attempt_index)` (D4): a retried write of the
        *same* content is a no-op that returns the same id; a retried write
        under the same key with *different* content raises.
        """
        ...

    def write_run_event(self, event: RunEvent) -> EventId:
        """Durably record one timeline event. Raises on failure (D2)."""
        ...

    def write_model_call(self, call: ModelCall) -> ModelCallId:
        """Durably record one provider call, independent of whether it
        produced a decision step (e.g. `failed`, `rate_limited`,
        `empty_response`, `context_rejected` calls, which never appear in a
        `TurnCycleRecord`). Raises on failure (D2).
        """
        ...

    def write_save_point(self, save: SavePoint) -> SavePointId:
        """Durably record or update one save point's state (D5).

        Must reject `retention_status = eligible` unconditionally: that
        value has exactly one legitimate writer in the whole module,
        `archive_run` (FR-036, A2, invariant I17). No caller of this method
        may ever produce an eligible save point.
        """
        ...

    def write_capture(self, capture: ScreenCapture, blob: bytes | None) -> CaptureId:
        """Durably record one capture and, when not withheld, its blob (D5).

        `blob` is `None` exactly when the capture was withheld (SC-019); a
        shown/clean capture's blob must be durably stored through this same
        call, never left in local or ephemeral form.
        """
        ...

    def mark_turn_superseded(self, run_id: RunId, turn: int, attempt: int) -> None:
        """Mark a previously-written attempt abandoned and non-authoritative
        (FR-047). Never deletes it -- abandoned attempts remain retrievable.
        """
        ...

    def archive_run(self, run_id: RunId, *, by: str, at: Timestamp) -> None:
        """The *only* operation in this port that changes save-point
        eligibility (FR-036).

        Sets `Run.archived_at`, writes a `run_archived` `RunEvent`, and
        transitions that run's retained save points to `eligible` -- all
        three atomically (A1). Rejected when the run is not in a terminal
        lifecycle state (A3). Nothing else in this port, or in
        `store/sqlite_adapter.py`, may perform any part of this transition
        -- no age rule, no quota, no retention window, no thinning (A2).
        """
        ...

    # --- reads (recovery, branching, retention, audit) ---

    def get_run(self, run_id: RunId) -> Run | None:
        """Look up one run by id."""
        ...

    def get_turn_cycle(
        self, run_id: RunId, turn: int, *, authoritative_only: bool = True
    ) -> TurnCycleRecord | None:
        """Return one turn attempt's full record, steps in order.

        `authoritative_only=True` (default) returns the single authoritative
        attempt. `authoritative_only=False` returns the most recent attempt
        regardless of authoritative status, so an abandoned attempt remains
        retrievable (FR-047) even when the caller does not know its exact
        `attempt_index`.
        """
        ...

    def list_save_points(self, run_id: RunId) -> list[SavePoint]:
        """All save points for one run, in turn order (branching, retention)."""
        ...

    def get_last_known_good(self, run_id: RunId) -> SavePoint | None:
        """The most recent save point for this run that is neither `missing`
        nor `removed` -- what a failed or interrupted run should resume
        from (FR-048, SC-021).
        """
        ...

    def list_active_runs(self) -> list[Run]:
        """Every run not yet in a terminal lifecycle state (run-identity guard)."""
        ...

    def turn_gaps(self, run_id: RunId) -> list[int]:
        """Turn numbers between 1 and the highest recorded turn that have no
        authoritative attempt (FR-052, SC-011).
        """
        ...

    def step_gaps(self, run_id: RunId, turn: int) -> list[int]:
        """`step_index` values missing from the authoritative attempt at
        *turn*, between 1 and the highest recorded step (SC-003). Empty
        when the turn itself has no authoritative attempt at all -- that
        case is `turn_gaps`'s to report, not this method's.
        """
        ...

    def list_eligible_save_points(self) -> list[SavePoint]:
        """Save points whose run has been archived, and only those (FR-036,
        R17) -- the sole input to any operator-invoked save-file removal.
        """
        ...

    # --- health ---

    def ping(self) -> StoreHealth:
        """Check store reachability before turn 1 (D6). Never raises for an
        ordinary connectivity failure -- see `StoreHealth`.
        """
        ...
