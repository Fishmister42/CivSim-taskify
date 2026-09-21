# Contract: MatchStore Port

**Feature**: `002-civ-playing-harness` | **Schema version**: 1
**Implemented by**: deliverable 3 (match-tracking store), landed 2026-09-21 in `93a9b7e`:
`MatchTrackingStore(MatchStore)` in `src/civsim_harness/store/contract.py` extends this port
without changing it, and `src/civsim_harness/store/sqlite_adapter.py` (with `sqlite_reads.py`)
is the adapter the harness writes through. The local SQLite + blob reference adapter that
implemented it in the interim (plan Complexity Tracking C2) is that same adapter, now under
003's contract and schema.
**Amended**: 2026-09-20, owner-authorised — the [Capability extensions](#capability-extensions--the-reads-deliverable-1-proved-missing)
section was added and two read-table rows sharpened. Schema version unchanged: the amendment adds
read obligations, no record shape changed.

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
    def get_run_configuration(self, run_id: RunId) -> RunConfiguration | None: ...
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
| `get_turn_cycle(authoritative_only=True)` | Recovery, audit, replay | Returns the authoritative attempt with its steps in order; abandoned attempts remain retrievable with the flag off (FR-047). The flag-off read returns the **most recent** attempt (`sqlite_adapter.py`: `ORDER BY attempt_index DESC`) — previously unstated, which left "which attempt does a three-attempt turn return?" implementation-defined. The two forms together address at most two attempts of any turn; see Capability extensions below *(row amended 2026-09-20)* |
| `get_run_configuration` | Branching; deliverable 1's catalog columns | Returns the `RunConfiguration` `create_run` persisted for this run, verbatim -- never a merged, defaulted, or re-derived view. A branch inherits seed, civilization, ruleset, mod set, map and game settings from its **parent's** configuration and is refused if it restates any of them differently (FR-033, `config/run_config.py`), so a branch cannot be validated without reading the parent's configuration back. Also the read that resolves the web catalog's seed / civilization / ruleset / model columns, which live on `RunConfiguration` (001 FR-018); see Capability extensions below for the keying collision its consumer-side probe still carries *(row amended 2026-09-20)* |
| `get_last_known_good` | Crash recovery, failed-state reporting | Must identify the save a failed run stopped at (FR-048, SC-021) |
| `turn_gaps` | Completeness status | Returns turn numbers between 1 and the highest recorded turn with no authoritative attempt (FR-052, SC-011). "Highest recorded turn" is the highest `turn_number` with an authoritative `TurnCycle` -- except on a run whose `lifecycle_state` (`Run.lifecycle_state`) is not one of the three states in which a run is actively cycling through its own turn loop (`playing`, `waiting_on_model`, `waiting_on_game`), where it is instead the highest of that value and the highest turn with a save point at all (`list_save_points`). That extension is what makes a **trailing** attempted-but-never-recorded turn a reported gap once a run has stopped advancing -- paused (FR-042, e.g. after chain exhaustion), interrupted, resuming, or terminal (`finished`/`failed`) alike: a run's quicksave for turn N always precedes N's `TurnCycle` (FR-007), so on a run still actively playing that same shape is normal, not a gap, and is deliberately left unreported |
| `step_gaps` | Completeness status | Returns missing `step_index` values within a turn. SC-003 requires step-level contiguity, so a turn present but internally incomplete must be detectable — turn-level gap detection alone would call it complete |
| `list_save_points` | Branching, retention | Retention must be able to see which saves a resumable run still needs (FR-036) |
| `list_eligible_save_points` | The reaper | Returns save points whose run has been archived, and only those. It is the *only* way a deletion path learns what it may touch (FR-036, R17) |
| `list_active_runs` | Run-identity guard | Second gate on FR-006 alongside the single-tuner limit |
| `get_capture` | Parity, capabilities audit | Looks up one `ScreenCapture` by id so a visual declaration (`view_declaration_id`) can be resolved without reaching past the port |
| `list_run_events` | Prompt audit | Returns a run's `RunEvent` timeline, chronological by `occurred_at`, optionally filtered by `event_types`; what makes "every prompt is a recorded `prompt_response` decision or a recorded stall" (FR-005) checkable from the record alone |

## Capability extensions — the reads deliverable 1 proved missing

*Amendment (2026-09-20, owner-authorised). This is the contract that owns the gap; spec 001
recorded the same four findings from the consumer's side (its `tasks.md`: Foundation note 2, US1
notes 1 and 3, US2 note 1, US4 notes 2 and 3) and deliberately declined to edit this file, because
a consumer amending a producer's contract to make its own tasks look closed would bury the
finding. The owner ruled the amendment lands here instead.*

This contract was written for its first consumer — the harness, which writes through it. Building
its second consumer, the unified web interface (deliverable 1, a pure reader over this same port),
proved that **a store satisfying this contract exactly cannot serve the historical run catalog,
capture images, or an attempt-addressed turn read.** The consumer absorbed each gap as an
*optional probed capability* (`src/civsim_web/store_client/port.py`) rather than reaching around
the port into this store's blob and file layout — the right refusal — but four probed
capabilities is not four local workarounds; it is an unpublished half of this port. This section
publishes that half.

| Capability read | Signature (as probed) | What the published operations cannot do without it |
|---|---|---|
| **Run catalog** | `list_runs() -> list[Run]` | `list_active_runs` is documented for *active* runs (the run-identity guard); no operation enumerates terminal or archived runs, so the full historical catalog (001 FR-018/FR-019) cannot be assembled. A catalog silently missing finished runs is the failure most likely to corrupt the population a trend is drawn from (Principle III) |
| **Capture bytes** | `get_capture_blob(capture_id: CaptureId)` — returns the image bytes, or `None` | `get_capture` returns the record carrying `blob_ref`, a content address no published operation resolves to bytes. Serving an image without this read means reading `blob_ref` off the record and opening the file directly — a second, unaudited path into this store's layout (001 FR-031) |
| **Attempt addressing** | `get_turn_cycle_attempt(run_id: RunId, turn: int, attempt: int)` — returns that attempt's `TurnCycleRecord`, or `None` if it was never recorded | `get_turn_cycle`'s two forms address exactly two attempts of any turn: the authoritative one and the most recent one (see the amended row above). The case 001 FR-009 is actually about — attempt 0 abandoned, attempt 1 authoritative — is unreachable through the published reads |
| **Configuration resolution** | *(published above — `get_run_configuration(run_id)`)* | Originally: nothing resolved `Run.config_id` to the `RunConfiguration` carrying the catalog's seed, civilization, ruleset and model columns (001 FR-018). Closed at the contract level when `get_run_configuration` was published for branching (FR-033); the residue is a keying collision, stated below |

Rules, binding on implementations:

| # | Requirement |
|---|---|
| **E1** | **Deliverable 3 MUST offer all of these reads.** For the interim reference adapter they remain optional: it predates them, and the consumer's degraded rendering (E3) is the documented interim behaviour, not a defect |
| **E2** | **Discovery is structural, so absence is the only honest "cannot".** The consumer probes for each read by name and treats the name's presence as the capability's presence (`src/civsim_web/store_client/reads.py`, `catalog.py`). A store MUST NOT expose one of these names as a stub — fabricated, empty-but-plausible, or partial answers under a probed name are a contract violation, strictly worse than not offering the read, because a probe cannot tell a stub from the real thing |
| **E3** | **Absence degrades honestly, never silently.** Without `list_runs`, the catalog is marked `listing_is_partial` with the reason on the response; without `get_capture_blob`, an explicit "this store cannot resolve capture blobs" answer, never a placeholder image; without `get_turn_cycle_attempt`, the two reachable attempts plus a plain statement that the rest are unaddressable — never the authoritative attempt silently substituted for the one asked for. This behaviour exists and is tested on the consumer side (`tests/web_support/fixtures.py::published_port_only` runs the consumer's suite against a store offering exactly this contract and nothing more) |
| **E4** | **`get_capture_blob` on a withheld capture returns `None`.** Withheld captures are recorded without their blob (Immutability below), and the record — not a substitute image — is the evidence that screening worked |
| **E5** | **`get_run_configuration` resolves run ids and nothing else.** An id that is not a known `run_id` MUST answer `None`; resolving against any secondary key — `config_id` in particular — would turn the collision below into silently serving the wrong run's configuration |

**The keying collision, stated so nobody rediscovers it.** The consumer's probe
(`RunConfigurationReader`, `src/civsim_web/store_client/port.py`) predates this contract's
`get_run_configuration` and binds the **same method name** with a **`config_id`** argument —
`reads.py::run_configuration` passes `run.config_id` — while the read published above is keyed by
**`run_id`**. A store implementing this contract is therefore probed as capable and then
mis-keyed: under E5 it answers `None`, the consumer renders those columns unavailable, and the
capability appears offered-but-empty rather than absent. Honest, but not the populated catalog
001 FR-018 wants. The fix is consumer-side — re-key the probe to `run_id` and retire the
`RunConfigurationReader` protocol in favour of the published read — and is recorded here, on the
contract that surfaced it, so it is filed rather than folklore.

The conformance suite below asserts none of these extensions today; the consumer's own suite
exercises both their presence (its fake store offers all four) and their absence
(`published_port_only`). When deliverable 3 lands them, `tests/contract/test_match_store_port.py`
is where E1–E5 get asserted against the real store.

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
failed write surfaces as a raise rather than a falsy return. It also covers `turn_gaps`'s
stopped-vs-actively-playing distinction above directly: a trailing attempted-but-unrecorded turn
is asserted as a reported gap on a run that has stopped advancing (covering both a terminal run
and a merely `paused` one, since FR-042 pauses rather than fails a run on chain exhaustion), and
asserted as **not** reported on an otherwise identical still-playing run — so a future change to
either side of that distinction fails this suite rather than silently drifting from what this
table documents.

Two cases are deliberately adversarial: a turn of several hundred steps must round-trip with its
order intact and no truncation, and a finished-but-unarchived run's save points must never appear in
`list_eligible_save_points()` no matter how old the run is.
