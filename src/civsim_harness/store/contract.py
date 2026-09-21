"""The `MatchTrackingStore` contract (deliverable 3) and the read value types it returns.

``specs/003-match-tracking-store/contracts/match-tracking-store.md`` is the normative source.
The Protocol here **extends** 002's ``MatchStore`` (``store/port.py``) without changing it: every
operation, durability rule (D1-D6), archival rule (A1-A4) and capability extension (E1-E5) of the
002 contract is inherited as this store's floor, and the harness keeps binding to ``MatchStore``
alone (003 FR-016). What is added is exactly what deliverable 3 owns above that floor -- the reads
the web interface had to probe for, paged listing, attempt addressing, tagged capture images,
model-call rows and totals, store-owned completeness, trend and divergence reads, and the two
portability operations.

Nothing in this module is persisted. The persisted record shapes are 002's models, unchanged
(``specs/003-match-tracking-store/data-model.md`` SS0). Value types are frozen dataclasses whose
``__post_init__`` enforces the data model's own validation rules structurally, the same pattern
``store/port.py``'s ``StoreHealth`` already uses -- a malformed answer cannot be constructed, so a
reader never has to second-guess one.

Rule ids in docstrings (``W1``, ``R5``, ``T1``, ``V4``, ``B1``) refer to the contract document.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import Field

from civsim_harness.models.common import CaptureId, HarnessModel, RunId, Timestamp, TurnCycleId
from civsim_harness.models.config import RunConfiguration
from civsim_harness.models.records import CallOutcome, ModelCall, RunEvent, SavePoint
from civsim_harness.models.run import (
    ComparabilityStatus,
    LifecycleState,
    RecordCompletenessStatus,
    Run,
)
from civsim_harness.models.turn import ScreenCapture, TurnOutcome, WithheldReason
from civsim_harness.store.port import MatchStore, TurnCycleRecord

# --------------------------------------------------------------------------
# Store schema version and migrations (data-model.md SS1)
# --------------------------------------------------------------------------


@dataclass(frozen=True, order=True)
class StoreSchemaVersion:
    """The store **file**'s schema version, ``major.minor`` (data-model.md SS1.1).

    Distinct from the *record* schema version (``models/export_schemas.py``, still 1). A reader
    refuses a file whose major exceeds its own (FR-026, V2); a higher minor is additive and
    readable (FR-024). A file with no ``store_meta`` table is ``1.0`` by definition.
    """

    major: int
    minor: int

    def __post_init__(self) -> None:
        if self.major < 1 or self.minor < 0:
            raise ValueError(f"StoreSchemaVersion out of range: {self.major}.{self.minor}")

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}"

    @classmethod
    def parse(cls, text: str) -> StoreSchemaVersion:
        major_text, _, minor_text = text.strip().partition(".")
        return cls(int(major_text), int(minor_text or "0"))

    def readable_by(self, reader: StoreSchemaVersion) -> bool:
        """Whether a reader at *reader* may open a file at this version (V2)."""
        return self.major <= reader.major


@dataclass(frozen=True)
class MigrationRecord:
    """One applied migration, as recorded in ``schema_migrations`` (data-model.md SS1.3)."""

    migration_id: str
    from_version: str
    to_version: str
    applied_at: Timestamp
    backup_path: str
    detail: Mapping[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Listing (data-model.md SS3.1)
# --------------------------------------------------------------------------


class RunSort(StrEnum):
    """The stated sort orders of ``query_runs`` (R2). NULL timestamps sort last in every order."""

    STARTED_AT_DESC = "started_at_desc"
    STARTED_AT_ASC = "started_at_asc"
    ENDED_AT_DESC = "ended_at_desc"
    RUN_ID_ASC = "run_id_asc"


MAX_PAGE_SIZE = 500


@dataclass(frozen=True)
class RunQuery:
    """One page of the run catalog, with every filter FR-011 names.

    ``archived=None`` means archived runs are listed like any other (the FR-011 default);
    ``True``/``False`` narrows to archived / not-archived respectively.
    """

    page: int = 1
    page_size: int = 25
    sort: RunSort = RunSort.STARTED_AT_DESC
    seed_set_id: str | None = None
    lifecycle_states: frozenset[LifecycleState] | None = None
    completeness: frozenset[RecordCompletenessStatus] | None = None
    comparability: frozenset[ComparabilityStatus] | None = None
    archived: bool | None = None

    def __post_init__(self) -> None:
        if self.page < 1:
            raise ValueError("RunQuery.page must be >= 1")
        if not 1 <= self.page_size <= MAX_PAGE_SIZE:
            raise ValueError(f"RunQuery.page_size must be within 1..{MAX_PAGE_SIZE}")


@dataclass(frozen=True)
class RunPage:
    """The page ``query_runs`` answered, carrying the **total** matching count (R2)."""

    runs: tuple[Run, ...]
    total: int
    page: int
    page_size: int
    sort: RunSort

    def __post_init__(self) -> None:
        if self.total < 0 or len(self.runs) > self.page_size:
            raise ValueError("RunPage is inconsistent with its own page_size/total")


# --------------------------------------------------------------------------
# Attempts (SS3.2)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TurnAttemptSummary:
    """One attempt of one turn, without its steps (R4)."""

    turn_number: int
    attempt_index: int
    is_authoritative: bool
    outcome: TurnOutcome
    step_count: int
    turn_cycle_id: TurnCycleId


# --------------------------------------------------------------------------
# Captures (SS3.3)
# --------------------------------------------------------------------------


class CaptureImageStatus(StrEnum):
    AVAILABLE = "available"
    WITHHELD = "withheld"
    MISSING = "missing"
    NO_SUCH_CAPTURE = "no_such_capture"


@dataclass(frozen=True)
class CaptureImage:
    """The tagged answer to "give me this capture's image" (R5, FR-014).

    ``content`` is set exactly when ``status == available``; ``withheld_reason`` exactly when
    ``status == withheld``; a ``missing`` answer carries the ``blob_ref`` that could not be
    resolved on disk, with the capture record itself intact.
    """

    status: CaptureImageStatus
    capture_id: CaptureId
    content: bytes | None = None
    withheld_reason: WithheldReason | None = None
    blob_ref: str | None = None

    def __post_init__(self) -> None:
        has_content = self.content is not None
        if has_content != (self.status is CaptureImageStatus.AVAILABLE):
            raise ValueError("CaptureImage.content is set exactly when status == available")
        if (self.withheld_reason is not None) != (self.status is CaptureImageStatus.WITHHELD):
            raise ValueError("CaptureImage.withheld_reason is set exactly when status == withheld")
        if self.status is CaptureImageStatus.MISSING and self.blob_ref is None:
            raise ValueError("a missing CaptureImage must name the blob_ref it could not resolve")


# --------------------------------------------------------------------------
# Model calls (SS3.4)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelCallRow:
    """One ``model_calls`` row: the call plus its turn/step address (FR-008).

    ``turn_number``/``step_index`` are ``None`` for a call that preceded any turn record -- a
    failed call recorded through ``write_model_call`` before its turn existed (002 FR-042).
    """

    call: ModelCall
    turn_number: int | None
    step_index: int | None


@dataclass(frozen=True)
class ModelCallTotals:
    """A run's model spend in one read, from ``model_calls`` rows only (R6, FR-015).

    ``cost_usd`` is ``None`` when no call of the run reported a dollar amount -- never zero for
    "unknown". Token totals are summed over the calls that reported them.
    """

    run_id: RunId
    call_count: int
    priced_call_count: int
    cost_usd: float | None
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    latency_ms_total: int
    fallback_count: int
    retry_count: int
    calls_by_outcome: Mapping[CallOutcome, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.priced_call_count > self.call_count:
            raise ValueError("priced_call_count cannot exceed call_count")
        if (self.cost_usd is None) != (self.priced_call_count == 0):
            raise ValueError("cost_usd is None exactly when no call was priced")


# --------------------------------------------------------------------------
# Trends (SS3.5)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MetricPoint:
    turn: int
    value: float


@dataclass(frozen=True)
class MetricSeries:
    """Per-turn values of one metric for one run (FR-018, FR-021, T2, T3).

    ``unavailable_reason`` is set, with empty ``points``, when the run's record carries no such
    metric -- the store never fabricates a value (research R4).
    """

    run_id: RunId
    metric: str
    points: tuple[MetricPoint, ...]
    in_progress: bool
    comparability_status: ComparabilityStatus
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        turns = [point.turn for point in self.points]
        if turns != sorted(turns) or len(set(turns)) != len(turns):
            raise ValueError("MetricSeries.points must be strictly ascending by turn")
        if self.unavailable_reason is not None and self.points:
            raise ValueError("an unavailable MetricSeries carries no points")


class ExclusionReason(StrEnum):
    HAS_GAPS = "has_gaps"
    COMPLETENESS_UNKNOWN = "completeness_unknown"
    NOT_COMPARABLE = "not_comparable"
    VISUALLY_DEGRADED = "visually_degraded"
    NO_SUCH_RUN = "no_such_run"
    NOT_IN_SEED_SET = "not_in_seed_set"


@dataclass(frozen=True)
class ExcludedRun:
    """A run the store left out of a trend or comparison, and why (FR-019, T1)."""

    run_id: RunId
    reason: ExclusionReason
    detail: str
    gaps: tuple[int, ...] = ()


@dataclass(frozen=True)
class TrendQuery:
    """A metric-series request: exactly one of ``run_ids`` / ``seed_set_id``.

    ``metrics=None`` asks for every metric the records carry. ``include_visually_degraded`` is the
    explicit, response-recorded opt-in of plan.md Complexity Tracking C3.
    """

    run_ids: tuple[RunId, ...] | None = None
    seed_set_id: str | None = None
    metrics: tuple[str, ...] | None = None
    include_visually_degraded: bool = False

    def __post_init__(self) -> None:
        if (self.run_ids is None) == (self.seed_set_id is None):
            raise ValueError("TrendQuery names exactly one of run_ids or seed_set_id")
        if self.run_ids is not None and not isinstance(self.run_ids, tuple):
            object.__setattr__(self, "run_ids", tuple(self.run_ids))
        if self.metrics is not None and not isinstance(self.metrics, tuple):
            object.__setattr__(self, "metrics", tuple(self.metrics))


@dataclass(frozen=True)
class TrendResponse:
    series: tuple[MetricSeries, ...]
    excluded: tuple[ExcludedRun, ...]
    metric_names: tuple[str, ...]
    included_visually_degraded: bool


# --------------------------------------------------------------------------
# Divergence (SS3.6)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TurnFingerprint:
    """What one authoritative turn attempt did and recorded (research R5).

    ``actions`` is the ordered tuple of ``(action_declaration_id, canonical parameters JSON)``
    per step; ``metrics`` the turn's derived metrics (``store/trends.py``).
    """

    turn: int
    actions: tuple[tuple[str, str], ...]
    metrics: Mapping[str, float] = field(default_factory=dict)
    city_count: int | None = None
    unit_count: int | None = None


@dataclass(frozen=True)
class DivergenceDetail:
    dimension: str
    value_a: str
    value_b: str


@dataclass(frozen=True)
class DivergenceReport:
    run_a: RunId
    run_b: RunId
    same_seed_set: bool
    compared_turns: tuple[int, ...]
    first_divergent_turn: int | None
    differed: tuple[DivergenceDetail, ...]
    excluded: tuple[ExcludedRun, ...] = ()

    def __post_init__(self) -> None:
        if (self.first_divergent_turn is None) != (len(self.differed) == 0):
            raise ValueError("first_divergent_turn is set exactly when something differed")


# --------------------------------------------------------------------------
# Portability (SS3.7)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RunRecordSet:
    """One run's complete record, in memory: what ``export_run`` returns and ``import_run``
    accepts (V4). ``images`` maps a kept capture's ``blob_ref`` (sha256) to its bytes.
    """

    run: Run
    configuration: RunConfiguration
    turn_cycles: tuple[TurnCycleRecord, ...]
    run_events: tuple[RunEvent, ...]
    model_calls: tuple[ModelCall, ...]
    save_points: tuple[SavePoint, ...]
    captures: tuple[ScreenCapture, ...]
    images: Mapping[str, bytes] = field(default_factory=dict)

    def counts(self) -> dict[str, int]:
        return {
            "runs": 1,
            "turn_cycles": len(self.turn_cycles),
            "decision_steps": sum(len(record.steps) for record in self.turn_cycles),
            "run_events": len(self.run_events),
            "model_calls": len(self.model_calls),
            "save_points": len(self.save_points),
            "captures": len(self.captures),
            "images": len(self.images),
        }


BUNDLE_FORMAT = 1


class BundleManifest(HarnessModel):
    """``manifest.json`` of a run bundle (V5). A pydantic model so a manifest written by another
    host is validated field-for-field on the way in (``extra="forbid"``)."""

    bundle_format: int = Field(ge=1)
    store_schema_version: str
    run_id: str
    exported_at: Timestamp
    source_host: str
    source_store_id: str
    counts: dict[str, int] = Field(default_factory=dict)
    files: dict[str, str] = Field(default_factory=dict)
    images: dict[str, str] = Field(default_factory=dict)


# --------------------------------------------------------------------------
# Store information (SS3.8)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class StoreInfo:
    schema_version: StoreSchemaVersion
    store_id: str
    host_platform: str
    read_only: bool
    counts: Mapping[str, int]
    migrations: tuple[MigrationRecord, ...] = ()
    runs_with_absent_parent: tuple[RunId, ...] = ()


# --------------------------------------------------------------------------
# The contract
# --------------------------------------------------------------------------


@runtime_checkable
class MatchTrackingStore(MatchStore, Protocol):
    """Deliverable 3's published contract: 002's ``MatchStore`` plus what is owned here.

    Every method below is a read except ``import_run`` (W4). The floor -- the nine writes, the
    eleven reads and ``ping`` -- is inherited from ``MatchStore`` unchanged.
    """

    # --- the reads deliverable 1 proved missing (E1) -- the exact probed names ---

    def list_runs(self) -> list[Run]:
        """Every run in every lifecycle state, archived included; ``started_at`` descending with
        NULLs last, then ``run_id`` (R1, FR-011)."""
        ...

    def get_capture_blob(self, capture_id: CaptureId) -> bytes | None:
        """A kept capture's bytes; ``None`` for withheld, missing or unknown (E4, R5)."""
        ...

    def get_turn_cycle_attempt(
        self, run_id: RunId, turn: int, attempt: int
    ) -> TurnCycleRecord | None:
        """Exactly that attempt, authoritative or not; ``None`` if never recorded (R4, FR-013)."""
        ...

    # --- one published contract, nothing missing (US2) ---

    def query_runs(self, query: RunQuery) -> RunPage:
        """One page, paged and filtered in the store, with the total (R2, FR-011, SC-008)."""
        ...

    def highest_recorded_turn(self, run_id: RunId) -> int:
        """The highest turn with any attempt or save point; ``0`` when none (R7, FR-017)."""
        ...

    def list_turn_attempts(self, run_id: RunId, turn: int) -> list[TurnAttemptSummary]:
        """Every attempt of one turn, by ``attempt_index``, without loading steps (R4)."""
        ...

    def get_capture_image(self, capture_id: CaptureId) -> CaptureImage:
        """available / withheld / missing / no_such_capture, tagged (R5, FR-014)."""
        ...

    def list_model_calls(
        self, run_id: RunId, *, turn: int | None = None, step: int | None = None
    ) -> list[ModelCallRow]:
        """The run's model-call rows, optionally at one turn / step (R6, FR-008, FR-015)."""
        ...

    def model_call_totals(self, run_id: RunId) -> ModelCallTotals:
        """Cost, tokens, calls, fallbacks -- from rows, never from step bundles (R6, FR-015)."""
        ...

    # --- completeness, owned by the store (W3, FR-009, FR-010) ---

    def record_completeness(self, run_id: RunId) -> RecordCompletenessStatus:
        """The store's own gap accounting for this run, as persisted on the ``Run`` (FR-010)."""
        ...

    # --- cross-run reads for trending (US3) ---

    def metric_series(self, query: TrendQuery) -> TrendResponse:
        """Per-turn metric series with the store's exclusion rule applied and every excluded
        run named (T1-T3, FR-018, FR-019, FR-021)."""
        ...

    def divergence(self, run_a: RunId, run_b: RunId) -> DivergenceReport:
        """The first turn two runs' fingerprints differ, with what differed (T4, FR-020)."""
        ...

    # --- evolution and portability (US4) ---

    def store_info(self) -> StoreInfo:
        """Schema version, identity, counts and migrations (FR-024)."""
        ...

    def export_run(self, run_id: RunId) -> RunRecordSet:
        """The run's complete record, every attempt and kept image included (V4, FR-027)."""
        ...

    def import_run(self, records: RunRecordSet) -> RunId:
        """Insert a complete record verbatim, atomically; refuse an existing run id and a set
        whose references do not close (W4, V6, FR-027)."""
        ...


__all__ = [
    "BUNDLE_FORMAT",
    "MAX_PAGE_SIZE",
    "BundleManifest",
    "CaptureImage",
    "CaptureImageStatus",
    "DivergenceDetail",
    "DivergenceReport",
    "ExcludedRun",
    "ExclusionReason",
    "MatchTrackingStore",
    "MetricPoint",
    "MetricSeries",
    "MigrationRecord",
    "ModelCallRow",
    "ModelCallTotals",
    "RunPage",
    "RunQuery",
    "RunRecordSet",
    "RunSort",
    "StoreInfo",
    "StoreSchemaVersion",
    "TrendQuery",
    "TrendResponse",
    "TurnAttemptSummary",
    "TurnFingerprint",
]
