"""The closed `status` response schema (T119; contracts/operator-surface.md, FR-053).

`RunStatusView` is the *entire* wire shape both `civsim run status <run_id>` and
`GET /runs/{id}/status` may ever return: lifecycle and diagnostic fields only,
never a turn record, observation, decision, reasoning, yield, metric series, or
capture. Those live in the store and are deliverable 1's to present -- Principle
VI's "same view for user and directing session" is satisfied by that shared
store-backed view, not by this surface growing a second one.

The bound is enforced **structurally**, not by convention: every model below
subclasses `HarnessModel` (`extra="forbid"`), so a field this contract does not
name cannot be added to the wire shape without editing *this* module -- there is
no way for a stray keyword argument, a serialization helper, or a future
`.update()` call elsewhere to silently widen what `status` returns. Anyone
tempted to attach richer data here has to come to this file and its docstring
first.

`archived` is deliberately a plain `bool` here, not read from a stored field of
that name: contracts/operator-surface.md and data-model.md SS4 are explicit that
`archived_at` (a nullable timestamp) is the only source of truth, and `archived`
is a projection of it for operator convenience (`archived = Run.archived_at is
not null`). Whoever builds a `RunStatusView` computes this boolean at
construction time; there is no `archived_at` field on this model to keep that
projection from silently becoming a second, divergent source of the same fact.
"""

from __future__ import annotations

from civsim_harness.models.common import HarnessModel, RunId, SavePointId, Timestamp
from civsim_harness.models.run import ComparabilityStatus, LifecycleState, RecordCompletenessStatus

__all__ = [
    "ConnectionHealth",
    "LastError",
    "LastKnownGoodSave",
    "RunStatusView",
]


class LastKnownGoodSave(HarnessModel):
    """The save an operator would resume from right now (contracts/operator-surface.md).

    Present on purpose: deliverable 1's FR-027 requires the user to be able to
    see what they need in order to act *here*, so this is the one place the two
    surfaces meet -- and nowhere else (no lineage, no verification detail, no
    save history beyond this single pointer).
    """

    turn: int
    save_point_id: SavePointId


class LastError(HarnessModel):
    """The most recent fault on this run's timeline, named but not detailed.

    `type` is a short, free-form label (e.g. ``"crash_detected"``) rather than
    a closed enum: the set of things that can go wrong on a run's timeline is
    `RunEventType` (models/records.py), which is intentionally not imported
    here -- coupling this diagnostic surface to that enum's exact membership
    would make every new `RunEventType` member a `status`-schema change, which
    is precisely the coupling FR-053 exists to prevent. `detail` beyond the
    type and timestamp -- reasoning, a stack trace, event payloads -- has no
    field here at all.
    """

    type: str
    at: Timestamp


class ConnectionHealth(HarnessModel):
    """Coarse reachability, not diagnostics: exactly what an operator needs to
    tell "the run is fine" from "something between it and the world is not."

    Each field is a short free-form status word (e.g. ``"ok"``,
    ``"disconnected"``, ``"not_running"``) rather than a closed enum, for the
    same reason as `LastError.type`: the exact vocabulary is a runner
    implementation detail, and pinning it here would make this diagnostic
    surface change shape every time that vocabulary grows.
    """

    tuner: str
    client: str
    store: str


class RunStatusView(HarnessModel):
    """The whole of what `status` may return (contracts/operator-surface.md).

    Carries **only** lifecycle and diagnostic fields -- `run_id`,
    `lifecycle_state`, `requested_state`, `current_turn`, `current_step`,
    `last_known_good_save`, `last_error`, `connection_health`,
    `record_completeness_status`, `comparability_status`, `archived`, and
    `disk_headroom_gb` -- and nothing else (FR-053). No turn records, no
    observations, no decisions, no reasoning, no yields, no metric series, no
    captures: those are deliverable 1's, reading the store directly.

    `current_step` is a bare integer progress indicator, not a record: with
    unbounded turns (FR-014) a run can legitimately sit on one turn for hours,
    and without this an operator cannot tell a working run from a wedged one --
    but the step's observation, decision, and reasoning stay on the other side
    of this line. `requested_state` exposes a pause or stop that has been
    accepted (and recorded via `lifecycle_command_received`, T120) but not yet
    landed on a turn boundary, for the same diagnostic reason: a pause may take
    a long time to land, and that is correct, not a hang.
    """

    run_id: RunId
    lifecycle_state: LifecycleState
    requested_state: LifecycleState | None = None
    current_turn: int | None = None
    current_step: int | None = None
    last_known_good_save: LastKnownGoodSave | None = None
    last_error: LastError | None = None
    connection_health: ConnectionHealth
    record_completeness_status: RecordCompletenessStatus
    comparability_status: ComparabilityStatus
    archived: bool
    disk_headroom_gb: float
