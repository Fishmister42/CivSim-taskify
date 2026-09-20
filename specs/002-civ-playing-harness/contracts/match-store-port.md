# Contract: MatchStore Port

**Feature**: `002-civ-playing-harness` | **Schema version**: 1
**Implemented by**: deliverable 3 (match-tracking store). A local SQLite + blob reference adapter
implements it in the interim (plan Complexity Tracking C2).

This is the contract the harness writes through and the one deliverable 3 must satisfy. It exists
because FR-013 makes a successful write a *precondition of advancing the game* — the harness cannot
be built or tested against a store that does not exist yet, and it must not be built against a store
whose shape it guessed.

## Operations

```python
class MatchStore(Protocol):
    # --- writes (all synchronous-durable; see durability contract below) ---
    def create_run(self, run: Run, config: RunConfiguration) -> RunId: ...
    def update_run(self, run_id: RunId, **fields) -> None: ...
    def write_turn_cycle(self, record: TurnCycleRecord) -> TurnCycleId: ...
    def write_run_event(self, event: RunEvent) -> EventId: ...
    def write_model_call(self, call: ModelCall) -> ModelCallId: ...
    def write_save_point(self, save: SavePoint) -> SavePointId: ...
    def write_capture(self, capture: ScreenCapture, blob: bytes | None) -> CaptureId: ...
    def mark_turn_superseded(self, run_id: RunId, turn: int, attempt: int) -> None: ...
    def archive_run(self, run_id: RunId, *, by: str, at: Timestamp) -> None: ...

    # --- reads (recovery, branching, retention, audit) ---
    def get_run(self, run_id: RunId) -> Run | None: ...
    def get_turn_cycle(self, run_id: RunId, turn: int, *, authoritative_only: bool = True) -> TurnCycleRecord | None: ...
    def list_save_points(self, run_id: RunId) -> list[SavePoint]: ...
    def get_last_known_good(self, run_id: RunId) -> SavePoint | None: ...
    def list_active_runs(self) -> list[Run]: ...
    def turn_gaps(self, run_id: RunId) -> list[int]: ...
    def step_gaps(self, run_id: RunId, turn: int) -> list[int]: ...
    def list_eligible_save_points(self) -> list[SavePoint]: ...
    def get_capture(self, capture_id: CaptureId) -> ScreenCapture | None: ...
    def list_run_events(self, run_id: RunId, *, event_types: Sequence[RunEventType] | None = None) -> list[RunEvent]: ...

    # --- health ---
    def ping(self) -> StoreHealth: ...
```

`TurnCycleRecord` is the whole turn as one unit — the turn cycle, its **ordered decision steps**, and
for each step the observation with its entries and capture references, the single decision with its
reasoning, the execution outcome, and the model call that produced it. It is written as one unit
deliberately (see Atomicity).

**The record's size is unbounded, because the turn is.** A turn may contain hundreds of steps, each
with its own observation and image reference, and the store must accept that rather than requiring
the harness to cap, sample, or summarise — any of which would violate FR-012's ordered-step
requirement and FR-014's no-truncation rule. Implementations that need to stream or chunk a large
turn may, provided D3's atomicity still holds from the caller's point of view.

## Durability contract — the load-bearing part

| # | Requirement | Why |
|---|---|---|
| **D1** | `write_turn_cycle` returns only after the record is durable. A buffered or best-effort acknowledgement is a contract violation | FR-013 — the end-turn action is issued on the strength of this return |
| **D2** | Any write failure raises. The harness halts the run; it must never be able to interpret a failure as "continue" | FR-013, edge case: store unreachable |
| **D3** | `write_turn_cycle` is atomic: a turn is wholly present or wholly absent, never half-written | Edge case: crash between executing an action and persisting the turn |
| **D4** | Writes are idempotent on `(run_id, turn_number, attempt_index)` — a retried write after an ambiguous failure must not duplicate | Recovery re-writes are expected, not exceptional |
| **D5** | Captures and save-point references go through this port. Nothing the record depends on may exist only in local or ephemeral form | FR-051 |
| **D6** | `ping()` failure before turn 1 aborts preparation rather than starting a run that cannot record | Edge case: store unreachable |

**Atomicity note**: D3 is why the turn is one write rather than several. A turn whose decisions
landed but whose observations did not would be a record that looks complete and is not — exactly the
half-written turn FR-013 and the spec's crash edge case forbid. With the turn now a sequence of
steps, this cuts more finely: a turn missing step 7 of 12 is half-written too, and the write must
either carry every step or none.

**Why not stream steps as they happen?** It is the obvious optimisation for a long turn and it is
wrong here. A turn written incrementally is partially present at every moment, so a crash mid-turn
leaves a record that a reader cannot distinguish from a complete short turn without consulting
something else. The write stays at turn granularity; the cost is holding one turn's steps in memory
until it ends, which is bounded by the turn and paid once per turn.

## Read requirements

| Operation | Used for | Requirement |
|---|---|---|
| `get_turn_cycle(authoritative_only=True)` | Recovery, audit, replay | Returns the authoritative attempt with its steps in order; abandoned attempts remain retrievable with the flag off (FR-047) |
| `get_last_known_good` | Crash recovery, failed-state reporting | Must identify the save a failed run stopped at (FR-048, SC-021) |
| `turn_gaps` | Completeness status | Returns turn numbers with no authoritative attempt (FR-052, SC-011) |
| `step_gaps` | Completeness status | Returns missing `step_index` values within a turn. SC-003 requires step-level contiguity, so a turn present but internally incomplete must be detectable — turn-level gap detection alone would call it complete |
| `list_save_points` | Branching, retention | Retention must be able to see which saves a resumable run still needs (FR-036) |
| `list_eligible_save_points` | The reaper | Returns save points whose run has been archived, and only those. It is the *only* way a deletion path learns what it may touch (FR-036, R17) |
| `list_active_runs` | Run-identity guard | Second gate on FR-006 alongside the single-tuner limit |
| `get_capture` | Parity, capabilities audit | Looks up one `ScreenCapture` by id so a visual declaration (`view_declaration_id`) can be resolved without reaching past the port |
| `list_run_events` | Prompt audit | Returns a run's `RunEvent` timeline, chronological by `occurred_at`, optionally filtered by `event_types`; what makes "every prompt is a recorded `prompt_response` decision or a recorded stall" (FR-005) checkable from the record alone |

## Archival and retention

`archive_run` is the single operation that changes save-point eligibility (FR-036).

| # | Requirement |
|---|---|
| **A1** | `archive_run` sets `Run.archived_at`, writes a `run_archived` event, and transitions that run's save points to `eligible`. Records, events, and captures are untouched and stay readable forever |
| **A2** | **Nothing else may set `archived_at` or produce an `eligible` save point.** Not age, not a quota, not a retention window, not a run reaching a terminal state, not thinning. A store implementation that expires rows on a TTL violates this contract |
| **A3** | `archive_run` on a non-terminal run is rejected |
| **A4** | Archival is not deletion. Removing the save *files* is a separate, operator-invoked step over `list_eligible_save_points()`; the `SavePoint` record survives with `missing` recordable, so a later branch attempt fails explainably rather than mysteriously |

A2 is the one worth restating in the implementer's terms: deliverable 3 will be a database, and
databases grow retention policies. This port forbids one on save points. The reasoning is in research
R17 — every turn-start save is a branch point, and an automatic rule discards branch points nobody
decided to give up, long after the fact, with no one watching.

## Immutability

- **A branch never modifies its parent** (FR-034, I12). Writes carrying a `parent_run_id` touch only
  the child's rows. The store must reject any write from a child run that targets parent records.
- **Abandoned and superseded turns are marked, never deleted** (FR-035, FR-047). There is no delete
  operation on turn records in this port, which is deliberate.
- **Withheld captures are recorded without their blob** (`blob=None`). The record is the evidence
  screening worked; removing it would hide a signal SC-009 audits.
- **Captures and events have no delete or mutate path either** (same reasoning as turn records,
  FR-034, I12). `get_capture` and `list_run_events` are reads only — nothing in this port lets a
  caller remove or revise a capture or event once written.

## Schema evolution

Record schemas are versioned and additive-only within a major version. The constitution requires the
store to stay backward-readable for existing historical-trend queries or ship an explicit migration;
that obligation is deliverable 3's, and this contract's part is that the harness never writes a
field it has not declared in a published schema version.

## Conformance tests

`tests/contract/test_match_store_port.py` runs the same suite against any implementation — the
reference adapter today, deliverable 3's store when it lands. Swapping implementations is a
configuration change, and the suite is what makes that claim true rather than hopeful. It asserts:
D1–D6, A1–A4, idempotency under repeated writes, parent-immutability rejection, turn **and step**
gap detection, step-order preservation on read-back, authoritative-attempt selection, `get_capture`
and `list_run_events` (including chronological ordering and `event_types` filtering), and that a
failed write surfaces as a raise rather than a falsy return.

Two cases are deliberately adversarial: a turn of several hundred steps must round-trip with its
order intact and no truncation, and a finished-but-unarchived run's save points must never appear in
`list_eligible_save_points()` no matter how old the run is.
