"""The ``MatchStore`` read seam (T005).

**This is the only module in ``civsim_web`` permitted to name the
``MatchStore`` type.** Everything else in this package -- every view model,
every route, every template -- receives already-read records, never the store
itself. That is what makes plan.md's "read-only, structurally" constraint a
property of the import graph rather than of reviewer diligence (FR-023,
FR-024, FR-026).

Two deliberate decisions are recorded here because both would otherwise look
like oversights:

**1. The Protocol is re-declared, not imported from ``civsim_harness``.**
``specs/002-civ-playing-harness/contracts/match-store-port.md`` is the
published contract; this module restates its *read* half against that
document. Importing ``civsim_harness.store.port`` would couple this
separately-deployed process to 002's package (plan.md Structure Decision) and
would drag the harness's whole dependency graph into a web process that has no
use for it. Because the declaration is structural (``Protocol``), 002's real
adapter and deliverable 3's eventual store both satisfy it without either side
knowing about this file.

**2. None of the port's nine write operations is named here.**
``create_run``, ``update_run``, ``write_turn_cycle``, ``write_run_event``,
``write_model_call``, ``write_save_point``, ``write_capture``,
``mark_turn_superseded``, and ``archive_run`` are absent from this Protocol by
construction, not stubbed and guarded. A ``civsim_web`` caller reaching for one
gets an attribute error from the type checker and at runtime, which is the
strongest form the "no write path at all" constraint can take.

``list_eligible_save_points`` is likewise absent: it is the retention reaper's
read, the sole input to a deletion path, and this feature has no business
knowing which saves are eligible for removal.

**Record shapes.** The records the port returns are described here as
structural ``Protocol``s covering only the fields this feature reads. They are
intentionally narrow: a field absent from these protocols is a field no view
model can reach, which is the same closed-list discipline the Panel Registry
applies one layer further in (UP-001).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

__all__ = [
    "CaptureBlobReader",
    "CaptureId",
    "MatchStore",
    "ModelConfigLike",
    "READ_OPERATIONS",
    "RunCatalogReader",
    "RunConfigurationLike",
    "RunEventLike",
    "RunId",
    "RunLike",
    "SavePointLike",
    "ScreenCaptureLike",
    "StoreHealthLike",
    "Timestamp",
    "TurnAttemptReader",
    "TurnCycleRecordLike",
    "WRITE_OPERATIONS",
]

# --------------------------------------------------------------------------
# Identifier aliases (002 data-model.md; opaque to this feature)
# --------------------------------------------------------------------------

RunId = str
CaptureId = str
SavePointId = str
EventId = str
Timestamp = Any  # a datetime in every implementation; opaque at this seam


# --------------------------------------------------------------------------
# Record shapes -- only the fields this feature reads
# --------------------------------------------------------------------------


class StoreHealthLike(Protocol):
    """Outcome of ``ping()`` (002 port D6). A tagged outcome, never a raise."""

    ok: bool
    checked_at: Timestamp
    detail: str | None


class ModelConfigLike(Protocol):
    """002 data-model.md SS2 ``ModelConfig``.

    ``primary`` and ``fallbacks`` only. Credential material is *not* on this
    protocol and never can be: 002's own contract states this record carries no
    credentials (FR-043), and FR-030 makes re-exposing one here a release
    blocker. Narrowing the shape is this feature's half of that guarantee.
    """

    primary: Any
    fallbacks: Sequence[Any]


class RunConfigurationLike(Protocol):
    """002 data-model.md SS2 ``RunConfiguration`` -- the catalog columns only."""

    config_id: str
    map_seed: str
    civilization: str
    leader: str
    ruleset: str
    model_config: ModelConfigLike


class RunLike(Protocol):
    """002 data-model.md SS4 ``Run``."""

    run_id: RunId
    config_id: str
    lifecycle_state: Any
    started_at: Timestamp | None
    ended_at: Timestamp | None
    stop_resolution: Any | None
    record_completeness_status: Any
    comparability_status: Any
    observation_catalog_version: Any
    action_catalog_version: Any
    parent_run_id: RunId | None
    parent_turn: int | None
    archived_at: Timestamp | None


class RunEventLike(Protocol):
    """002 data-model.md SS14 ``RunEvent``."""

    event_id: EventId
    run_id: RunId
    turn_number: int | None
    step_index: int | None
    event_type: Any
    occurred_at: Timestamp
    detail: Any


class SavePointLike(Protocol):
    """002 data-model.md SS13 ``SavePoint`` -- what ``InterventionInfo`` displays."""

    save_point_id: SavePointId
    run_id: RunId
    turn_number: int
    save_name: str
    taken_at: Timestamp
    verified: bool
    retention_status: Any
    missing: bool


class ScreenCaptureLike(Protocol):
    """002 data-model.md SS8 ``ScreenCapture``.

    ``screening_status`` is typed ``Any`` rather than a closed enum on purpose:
    ``CaptureView`` must fail *closed* on a value this feature does not
    recognise (data-model.md V2), and narrowing the type here would let a
    future 002 status value fail at parse time instead -- turning a
    render-as-unavailable into a crash.
    """

    capture_id: CaptureId
    run_id: RunId
    turn_number: int
    decision_step_id: str
    captured_at: Timestamp
    screening_status: Any
    withheld_reason: Any | None
    blob_ref: str | None


class TurnCycleRecordLike(Protocol):
    """002 ``store/port.py`` ``TurnCycleRecord`` -- the whole turn as one unit.

    ``steps`` is an ordered sequence of step bundles; its element shape is
    consumed by ``viewmodels/step.py`` in User Story 1 and is deliberately left
    untyped here so this seam does not have to restate 002's four nested record
    shapes before any of them is rendered.
    """

    turn_cycle: Any
    steps: Sequence[Any]


# --------------------------------------------------------------------------
# The port -- reads only
# --------------------------------------------------------------------------


class MatchStore(Protocol):
    """The read half of 002's ``MatchStore`` port, and nothing else.

    Every operation below is a read. The nine writes named in the module
    docstring are absent by construction. Signatures mirror
    ``specs/002-civ-playing-harness/contracts/match-store-port.md`` so that any
    implementation of that contract -- 002's SQLite reference adapter today,
    deliverable 3's store later, this package's own fake in tests -- satisfies
    this Protocol structurally, with no code change here (quickstart Scenario 6).
    """

    def get_run(self, run_id: RunId) -> RunLike | None:
        """Look up one run by id. ``None`` when the run never existed."""
        ...

    def get_run_configuration(self, run_id: RunId) -> RunConfigurationLike | None:
        """The ``RunConfiguration`` this run was created with (FR-018 columns).

        A published port read, **keyed by ``run_id``** -- the same key
        ``get_run`` takes, never ``Run.config_id``. The amended
        ``match-store-port.md`` (Capability extensions, E5) is explicit that a
        conforming store resolves run ids and nothing else: an id that is not
        a known ``run_id`` answers ``None``, and resolving against a secondary
        key such as ``config_id`` would turn a config_id/run_id collision into
        silently serving the wrong run's configuration.
        """
        ...

    def get_turn_cycle(
        self, run_id: RunId, turn: int, *, authoritative_only: bool = True
    ) -> TurnCycleRecordLike | None:
        """One turn attempt's full record, steps in order.

        ``authoritative_only=True`` (the default) returns the single
        authoritative attempt; ``False`` returns the most recent attempt
        regardless, which is how an abandoned attempt stays retrievable for
        FR-009's "explain itself rather than 404" response.
        """
        ...

    def list_save_points(self, run_id: RunId) -> list[SavePointLike]:
        """All save points for one run, in turn order."""
        ...

    def get_last_known_good(self, run_id: RunId) -> SavePointLike | None:
        """The save a failed or interrupted run should resume from (FR-027)."""
        ...

    def list_active_runs(self) -> list[RunLike]:
        """Every run not yet in a terminal lifecycle state.

        Note for the catalog (FR-018): this is *active* runs, not the full
        historical catalog. Composing a catalog from it is the gap plan.md
        Complexity Tracking C1 records against the port.
        """
        ...

    def turn_gaps(self, run_id: RunId) -> list[int]:
        """Turn numbers with no authoritative attempt (FR-016, V4)."""
        ...

    def step_gaps(self, run_id: RunId, turn: int) -> list[int]:
        """``step_index`` values missing from a turn's authoritative attempt."""
        ...

    def get_capture(self, capture_id: CaptureId) -> ScreenCaptureLike | None:
        """One capture *record* by id.

        The port has no blob-fetch operation -- only the record carrying
        ``blob_ref``. ``CaptureView`` gates on ``screening_status`` from this
        record; resolving ``blob_ref`` to bytes is not something this port can
        do (see the module note in ``fake.py``).
        """
        ...

    def list_run_events(
        self, run_id: RunId, *, event_types: Sequence[Any] | None = None
    ) -> list[RunEventLike]:
        """A run's ``RunEvent`` timeline, chronological by ``occurred_at``."""
        ...

    def ping(self) -> StoreHealthLike:
        """Store reachability. Never raises for an ordinary failure."""
        ...


# ``RunConfigurationReader`` used to be declared here as the first optional
# capability (plan.md Complexity Tracking C1): the pre-amendment
# ``match-store-port.md`` had no operation that resolved a ``Run.config_id``,
# so this feature probed for one. The port's owner has since published
# ``get_run_configuration`` as a first-class read -- keyed by **run_id**, not
# config_id (amended contract, Capability extensions E5) -- so the Protocol
# was retired exactly as its own docstring promised: deleted, with
# ``MatchStore`` above widened to match the published contract. The three
# Protocols below remain probed capabilities: the amendment makes them
# obligations on deliverable 3 (E1), but the interim reference adapter may
# predate them, and structural probing stays the discovery mechanism (E2).


class RunCatalogReader(Protocol):
    """An **optional** capability -- the listing half of the C1 gap.

    ``list_active_runs`` is documented for *active* runs (recovery and run
    identity). FR-018's catalog is the full historical record: every run ever
    recorded, with its columns, filterable and sortable. The published
    ``match-store-port.md`` names no operation that enumerates terminal runs,
    which plan.md Complexity Tracking **C1** already records as the gap this
    feature must absorb rather than resolve.

    Probed for exactly as the other two are. A store that offers it gets a real
    historical catalog; a store that does not gets the *active* runs plus an
    explicit marker saying the listing is partial and why -- never a page that
    looks like the whole history while quietly being a slice of it, which is the
    failure mode UP-005 exists to prevent and the one most likely to corrupt a
    trend conclusion (Principle III).

    **This is a dependency to raise with deliverable 3, not a decision taken
    here.** When the port publishes a catalog listing, delete this Protocol and
    widen ``MatchStore`` to match.
    """

    def list_runs(self) -> list[RunLike]:
        """Every recorded run, active and terminal alike."""
        ...


class CaptureBlobReader(Protocol):
    """Another **optional** capability, and a further half of the same gap.

    ``get_capture`` returns the capture *record*, carrying ``blob_ref`` -- a
    content address. The published ``match-store-port.md`` has no operation that
    resolves that address to bytes, so ``GET /captures/{id}/image`` (T029) has
    nothing to serve from the port as published.

    Probed for rather than required, exactly as ``RunCatalogReader`` is. A
    store that offers it serves images; a store that does not gets an honest
    "this store cannot resolve capture blobs" response naming the port gap,
    never a placeholder image standing in for the real one -- which
    contracts/web-read-api.md rules out explicitly, since a placeholder could be
    mistaken for content.

    The alternative -- reading ``blob_ref`` off the record and opening the file
    ourselves -- is deliberately not taken. It would reach around the port into
    002's storage layout, which is precisely the coupling plan.md's "pure reader
    of the MatchStore port" constraint exists to prevent, and it would make this
    process's correctness depend on a path convention nobody published.

    **This is a dependency to raise with deliverable 3, not a decision taken
    here.** When the port publishes a blob read, this Protocol should be deleted
    and ``MatchStore`` widened to match.
    """

    def get_capture_blob(self, capture_id: CaptureId) -> bytes | None:
        """Resolve a capture's ``blob_ref`` to its image bytes."""
        ...


class TurnAttemptReader(Protocol):
    """A third **optional** capability -- the one FR-009 needs (US2/T036).

    ``contracts/web-read-api.md`` gives ``GET /runs/{id}/turns/{n}`` an
    ``?attempt={k}`` parameter that "selects a specific (including abandoned)
    attempt", and FR-009 requires a reference naming a since-superseded attempt
    to return *that* attempt rather than the current authoritative one. The
    published ``match-store-port.md`` has no read that addresses an attempt by
    index: ``get_turn_cycle`` takes only ``authoritative_only``, whose flag-off
    form is documented as "abandoned attempts remain retrievable" without saying
    *which* one a turn with three attempts returns. Every implementation --
    002's adapter and this package's fake alike -- returns the most recent.

    So the two published reads between them address exactly two attempts of any
    turn: the authoritative one, and the newest one. A turn replayed after a
    crash (attempt 0 abandoned, attempt 1 authoritative) has its abandoned
    attempt unreachable, which is precisely the reference FR-009 is about.

    Probed for exactly as the other three are. A store that offers it can answer
    any ``?attempt=``; a store that does not answers the two reachable ones and
    says plainly, for the rest, that the published port cannot address them --
    never a silent substitution of the authoritative turn, which is the one
    outcome FR-009 rules out by name.

    **This is a dependency to raise with deliverable 3, not a decision taken
    here.** When the port publishes an attempt-addressed read (or documents
    ``authoritative_only=False`` as returning every attempt), delete this
    Protocol and widen ``MatchStore`` to match.
    """

    def get_turn_cycle_attempt(
        self, run_id: RunId, turn: int, attempt: int
    ) -> TurnCycleRecordLike | None:
        """One specific attempt of one turn, authoritative or not."""
        ...


# --------------------------------------------------------------------------
# The boundary, as data
# --------------------------------------------------------------------------

#: Every read operation this feature is permitted to call. Enumerated as data
#: so the import-boundary test can assert the Protocol and this tuple agree --
#: a read added to one and not the other is a drift this feature notices.
READ_OPERATIONS: tuple[str, ...] = (
    "get_run",
    "get_run_configuration",
    "get_turn_cycle",
    "list_save_points",
    "get_last_known_good",
    "list_active_runs",
    "turn_gaps",
    "step_gaps",
    "get_capture",
    "list_run_events",
    "ping",
)

#: The port's nine write operations. None of these names may appear anywhere
#: under ``src/civsim_web/`` -- asserted by
#: ``tests/contract/test_read_only_boundary.py`` (plan.md Constraints). The
#: names are spelled here, in the one module that documents the boundary, so
#: the assertion has something to check against; that single occurrence is the
#: test's own allowance.
WRITE_OPERATIONS: tuple[str, ...] = (
    "create_run",
    "update_run",
    "write_turn_cycle",
    "write_run_event",
    "write_model_call",
    "write_save_point",
    "write_capture",
    "mark_turn_superseded",
    "archive_run",
)
