"""A read-only ``MatchStore`` fake (T006).

Deliverable 3 does not exist yet and 002's SQLite reference adapter is being
built by a separate effort this feature must not serialise behind (plan.md
Complexity Tracking C2). So this package develops and tests against a fake
that satisfies the same ``MatchStore`` Protocol -- exactly the strategy 002
itself uses one layer further in.

**The fake has no write methods.** ``create_run``, ``update_run``,
``write_turn_cycle``, ``write_run_event``, ``write_model_call``,
``write_save_point``, ``write_capture``, ``mark_turn_superseded``, and
``archive_run`` are *omitted*, not stubbed as no-ops. A test or a route that
reaches for one gets an ``AttributeError``, not a silent success -- which is
the difference between a boundary and a convention (T006, FR-024).

Records are *seeded* through the constructor rather than written through
methods. Seeding is a test-fixture affordance, not a store operation: it takes
already-formed records and installs them, and no route or view model can reach
it, since nothing outside a test ever holds the concrete ``FakeMatchStore``
type.

**On capture blobs**: 002's port has no blob-fetch operation -- only the
record carrying ``blob_ref``. This fake records an optional in-memory
``blob`` alongside each capture so ``GET /captures/{id}/image`` has something
to serve in tests, and flags the gap: resolving a real ``blob_ref`` to bytes is
not something the published port can do, and that is a dependency to raise with
deliverable 3, not a hole to paper over here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from civsim_web.store_client.port import (
    CaptureId,
    RunConfigurationLike,
    RunId,
)

__all__ = [
    "FakeActionExecution",
    "FakeDecision",
    "FakeDecisionStep",
    "FakeDecisionStepBundle",
    "FakeMatchStore",
    "FakeModelCall",
    "FakeModelConfig",
    "FakeObservation",
    "FakeObservationEntry",
    "FakeRun",
    "FakeRunConfiguration",
    "FakeRunEvent",
    "FakeSavePoint",
    "FakeScreenCapture",
    "FakeStoreHealth",
    "FakeTurnCycle",
    "FakeTurnCycleRecord",
]


def _now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------
# Record shapes (002 data-model.md; only the fields this feature reads)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FakeStoreHealth:
    """002 ``store/port.py`` ``StoreHealth``."""

    ok: bool
    checked_at: datetime = field(default_factory=_now)
    detail: str | None = None


@dataclass(frozen=True)
class FakeModelConfig:
    """002 data-model.md SS2 ``ModelConfig``. Carries no credentials, ever."""

    primary: str
    fallbacks: tuple[str, ...] = ()


@dataclass(frozen=True)
class FakeRunConfiguration:
    """002 data-model.md SS2 ``RunConfiguration`` -- catalog columns only."""

    config_id: str
    map_seed: str
    civilization: str
    leader: str
    ruleset: str
    model_config: FakeModelConfig


@dataclass(frozen=True)
class FakeRun:
    """002 data-model.md SS4 ``Run``."""

    run_id: RunId
    config_id: str
    lifecycle_state: str
    started_at: datetime | None = None
    ended_at: datetime | None = None
    stop_resolution: str | None = None
    record_completeness_status: str = "unknown"
    comparability_status: str = "comparable"
    observation_catalog_version: str = "1"
    action_catalog_version: str = "1"
    parent_run_id: RunId | None = None
    parent_turn: int | None = None
    archived_at: datetime | None = None


@dataclass(frozen=True)
class FakeRunEvent:
    """002 data-model.md SS14 ``RunEvent``."""

    event_id: str
    run_id: RunId
    event_type: str
    occurred_at: datetime = field(default_factory=_now)
    turn_number: int | None = None
    step_index: int | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FakeSavePoint:
    """002 data-model.md SS13 ``SavePoint``."""

    save_point_id: str
    run_id: RunId
    turn_number: int
    save_name: str
    taken_at: datetime = field(default_factory=_now)
    verified: bool = True
    retention_status: str = "retained"
    missing: bool = False


@dataclass(frozen=True)
class FakeScreenCapture:
    """002 data-model.md SS8 ``ScreenCapture``.

    ``screening_status`` is a plain ``str``, not an enum, so a fixture can seed
    a value this feature does not recognise and prove ``CaptureView`` still
    fails closed (data-model.md V2, quickstart Scenario 5).
    """

    capture_id: CaptureId
    run_id: RunId
    turn_number: int
    decision_step_id: str
    screening_status: str = "screened_clean"
    captured_at: datetime = field(default_factory=_now)
    withheld_reason: str | None = None
    blob_ref: str | None = None
    shown_to_agent: bool = True
    blob: bytes | None = None
    """In-memory image bytes. Not a port concept -- see the module docstring."""


@dataclass(frozen=True)
class FakeObservationEntry:
    """002 data-model.md SS7 ``ObservationEntry``."""

    declaration_id: str
    key: str
    value: Any
    context: str = "InGame"


@dataclass(frozen=True)
class FakeObservation:
    """002 data-model.md SS7 ``Observation`` -- assembled per decision step."""

    observation_id: str
    decision_step_id: str
    entries: tuple[FakeObservationEntry, ...] = ()
    captures: tuple[CaptureId, ...] = ()
    screen_identity: str = "world"
    catalog_version: str = "1"
    assembled_at: datetime = field(default_factory=_now)


@dataclass(frozen=True)
class FakeActionExecution:
    """002 data-model.md SS9 ``ActionExecution``."""

    outcome: str = "applied"
    rejection_reason: str | None = None
    verification: dict[str, Any] = field(default_factory=dict)
    verified_at: datetime = field(default_factory=_now)


@dataclass(frozen=True)
class FakeDecision:
    """002 data-model.md SS9 ``Decision``."""

    decision_id: str
    decision_step_id: str
    action_declaration_id: str
    model_call_id: str
    reasoning: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    trigger: str = "proactive"
    prompt_type: str | None = None
    is_end_turn: bool = False
    execution: FakeActionExecution = field(default_factory=FakeActionExecution)


@dataclass(frozen=True)
class FakeModelCall:
    """002 data-model.md SS12 ``ModelCall`` -- out-of-game telemetry throughout."""

    model_call_id: str
    run_id: RunId
    turn_cycle_id: str
    decision_step_id: str
    model_requested: str
    model_served: str
    latency_ms: int = 0
    cost: dict[str, Any] = field(default_factory=dict)
    retry_count: int = 0
    fallback_occurred: bool = False
    image_count: int = 0
    outcome: str = "decision_returned"


@dataclass(frozen=True)
class FakeDecisionStep:
    """002 data-model.md SS6 ``DecisionStep``."""

    decision_step_id: str
    turn_cycle_id: str
    step_index: int
    observation_id: str
    decision_id: str
    model_call_id: str
    progress: str = "changed_state"
    no_progress_streak_after: int = 0
    visually_degraded: bool = False
    started_at: datetime = field(default_factory=_now)
    ended_at: datetime = field(default_factory=_now)


@dataclass(frozen=True)
class FakeDecisionStepBundle:
    """002 ``store/port.py`` ``DecisionStepBundle`` -- one element of a turn."""

    step: FakeDecisionStep
    observation: FakeObservation
    decision: FakeDecision
    model_call: FakeModelCall


@dataclass(frozen=True)
class FakeTurnCycle:
    """002 data-model.md SS5 ``TurnCycle``."""

    turn_cycle_id: str
    run_id: RunId
    turn_number: int
    save_point_id: str
    attempt_index: int = 0
    is_authoritative: bool = True
    step_count: int = 1
    outcome: str = "ended_by_agent"
    final_no_progress_streak: int = 0
    visually_degraded: bool = False
    yields: dict[str, Any] = field(default_factory=dict)
    started_at: datetime = field(default_factory=_now)
    ended_at: datetime = field(default_factory=_now)
    persisted_at: datetime = field(default_factory=_now)


@dataclass(frozen=True)
class FakeTurnCycleRecord:
    """002 ``store/port.py`` ``TurnCycleRecord`` -- the whole turn as one unit."""

    turn_cycle: FakeTurnCycle
    steps: tuple[FakeDecisionStepBundle, ...] = ()


# --------------------------------------------------------------------------
# The fake store
# --------------------------------------------------------------------------


class FakeMatchStore:
    """A read-only, in-memory ``MatchStore``.

    Satisfies ``civsim_web.store_client.port.MatchStore`` structurally, plus the
    optional ``RunConfigurationReader`` capability (plan.md C1) so catalog rows
    can be exercised before the real port publishes a configuration read.

    Every method below is a read. There is deliberately no method that mutates
    a record after construction: ``seed_*`` installs fixture data and is the
    only mutation path, and nothing outside a test ever holds this type.
    """

    def __init__(
        self,
        *,
        runs: Sequence[FakeRun] = (),
        configurations: Sequence[FakeRunConfiguration] = (),
        turn_cycles: Sequence[FakeTurnCycleRecord] = (),
        events: Sequence[FakeRunEvent] = (),
        save_points: Sequence[FakeSavePoint] = (),
        captures: Sequence[FakeScreenCapture] = (),
        healthy: bool = True,
        health_detail: str | None = None,
        turn_gaps: dict[RunId, list[int]] | None = None,
        step_gaps: dict[tuple[RunId, int], list[int]] | None = None,
        terminal_states: Sequence[str] = ("finished", "failed"),
    ) -> None:
        self._runs: dict[RunId, FakeRun] = {r.run_id: r for r in runs}
        self._configurations: dict[str, FakeRunConfiguration] = {
            c.config_id: c for c in configurations
        }
        # Keyed by (run_id, turn_number, attempt_index); attempt order is kept
        # so `authoritative_only=False` can return "the most recent attempt".
        self._turn_cycles: list[FakeTurnCycleRecord] = list(turn_cycles)
        self._events: list[FakeRunEvent] = list(events)
        self._save_points: list[FakeSavePoint] = list(save_points)
        self._captures: dict[CaptureId, FakeScreenCapture] = {
            c.capture_id: c for c in captures
        }
        self._healthy = healthy
        self._health_detail = health_detail
        self._explicit_turn_gaps = dict(turn_gaps or {})
        self._explicit_step_gaps = dict(step_gaps or {})
        self._terminal_states = tuple(terminal_states)

    # -- seeding (fixtures only; not a port operation) ----------------------

    def seed_run(
        self, run: FakeRun, configuration: FakeRunConfiguration | None = None
    ) -> None:
        """Install a run (and optionally its configuration) as fixture data."""
        self._runs[run.run_id] = run
        if configuration is not None:
            self._configurations[configuration.config_id] = configuration

    def seed_turn_cycle(self, record: FakeTurnCycleRecord) -> None:
        """Install one turn attempt as fixture data.

        Named ``seed_*`` rather than ``write_*`` on purpose: this is a fixture
        affordance, and the port's ``write_turn_cycle`` must not acquire a
        lookalike inside this package (T006).
        """
        self._turn_cycles.append(record)

    def seed_event(self, event: FakeRunEvent) -> None:
        """Install one run event as fixture data."""
        self._events.append(event)

    def seed_save_point(self, save_point: FakeSavePoint) -> None:
        """Install one save point as fixture data."""
        self._save_points.append(save_point)

    def seed_capture(self, capture: FakeScreenCapture) -> None:
        """Install one capture record as fixture data."""
        self._captures[capture.capture_id] = capture

    def set_health(self, *, ok: bool, detail: str | None = None) -> None:
        """Flip store reachability, for the 503-on-every-route contract case."""
        self._healthy = ok
        self._health_detail = detail

    # -- reads --------------------------------------------------------------

    def get_run(self, run_id: RunId) -> FakeRun | None:
        return self._runs.get(run_id)

    def get_turn_cycle(
        self, run_id: RunId, turn: int, *, authoritative_only: bool = True
    ) -> FakeTurnCycleRecord | None:
        candidates = [
            record
            for record in self._turn_cycles
            if record.turn_cycle.run_id == run_id and record.turn_cycle.turn_number == turn
        ]
        if not candidates:
            return None
        if authoritative_only:
            authoritative = [r for r in candidates if r.turn_cycle.is_authoritative]
            return authoritative[-1] if authoritative else None
        return max(candidates, key=lambda r: r.turn_cycle.attempt_index)

    def list_save_points(self, run_id: RunId) -> list[FakeSavePoint]:
        return sorted(
            (s for s in self._save_points if s.run_id == run_id),
            key=lambda s: s.turn_number,
        )

    def get_last_known_good(self, run_id: RunId) -> FakeSavePoint | None:
        usable = [
            s
            for s in self.list_save_points(run_id)
            if not s.missing and s.retention_status != "removed"
        ]
        return usable[-1] if usable else None

    def list_active_runs(self) -> list[FakeRun]:
        return [
            run
            for run in self._runs.values()
            if run.lifecycle_state not in self._terminal_states
        ]

    def turn_gaps(self, run_id: RunId) -> list[int]:
        """Turn numbers with no authoritative attempt, 1..highest recorded.

        A fixture may override the computed answer entirely (``turn_gaps=``),
        which is how a test seeds a run whose *trailing* turn is a gap -- the
        stopped-vs-actively-playing distinction 002's port documents and which
        this feature reads rather than re-derives (data-model.md V5).
        """
        if run_id in self._explicit_turn_gaps:
            return list(self._explicit_turn_gaps[run_id])
        authoritative = {
            record.turn_cycle.turn_number
            for record in self._turn_cycles
            if record.turn_cycle.run_id == run_id and record.turn_cycle.is_authoritative
        }
        if not authoritative:
            return []
        return [n for n in range(1, max(authoritative) + 1) if n not in authoritative]

    def step_gaps(self, run_id: RunId, turn: int) -> list[int]:
        key = (run_id, turn)
        if key in self._explicit_step_gaps:
            return list(self._explicit_step_gaps[key])
        record = self.get_turn_cycle(run_id, turn, authoritative_only=True)
        if record is None:
            # The turn itself having no authoritative attempt is `turn_gaps`'s
            # to report, not this method's (002 port read requirements).
            return []
        indices = {bundle.step.step_index for bundle in record.steps}
        if not indices:
            return []
        return [n for n in range(1, max(indices) + 1) if n not in indices]

    def get_capture(self, capture_id: CaptureId) -> FakeScreenCapture | None:
        return self._captures.get(capture_id)

    def list_run_events(
        self, run_id: RunId, *, event_types: Sequence[Any] | None = None
    ) -> list[FakeRunEvent]:
        wanted = set(event_types) if event_types is not None else None
        return sorted(
            (
                event
                for event in self._events
                if event.run_id == run_id
                and (wanted is None or event.event_type in wanted)
            ),
            key=lambda e: e.occurred_at,
        )

    def ping(self) -> FakeStoreHealth:
        if self._healthy:
            return FakeStoreHealth(ok=True)
        return FakeStoreHealth(
            ok=False, detail=self._health_detail or "fake store marked unreachable"
        )

    # -- optional capability (plan.md Complexity Tracking C1) ---------------

    def get_run_configuration(self, config_id: str) -> RunConfigurationLike | None:
        """See ``port.RunConfigurationReader`` -- not a published port read."""
        return self._configurations.get(config_id)
