# Contract: MatchTrackingStore

**Feature**: `003-match-tracking-store` | **Store schema version**: 1.1 | **Record schema version**: 1
(unchanged from 002)
**Extends**: [`specs/002-civ-playing-harness/contracts/match-store-port.md`](../../002-civ-playing-harness/contracts/match-store-port.md)
— every operation, durability rule (D1–D6), archival rule (A1–A4), immutability rule and capability
extension (E1–E5) of that contract is inherited **verbatim** as this store's floor. This document
adds only what deliverable 3 owns above that floor.

## Operations

```python
class MatchTrackingStore(MatchStore, Protocol):
    # --- the reads deliverable 1 proved missing (E1) — exact probed names ---
    def list_runs(self) -> list[Run]: ...
    def get_capture_blob(self, capture_id: CaptureId) -> bytes | None: ...
    def get_turn_cycle_attempt(self, run_id: RunId, turn: int, attempt: int) -> TurnCycleRecord | None: ...

    # --- one published contract, nothing missing (US2) ---
    def query_runs(self, query: RunQuery) -> RunPage: ...
    def highest_recorded_turn(self, run_id: RunId) -> int: ...
    def list_turn_attempts(self, run_id: RunId, turn: int) -> list[TurnAttemptSummary]: ...
    def get_capture_image(self, capture_id: CaptureId) -> CaptureImage: ...
    def list_model_calls(self, run_id: RunId, *, turn: int | None = None, step: int | None = None) -> list[ModelCallRow]: ...
    def model_call_totals(self, run_id: RunId) -> ModelCallTotals: ...

    # --- completeness, owned by the store (US1, FR-009, FR-010) ---
    def record_completeness(self, run_id: RunId) -> RecordCompletenessStatus: ...

    # --- cross-run reads for trending (US3) ---
    def metric_series(self, query: TrendQuery) -> TrendResponse: ...
    def divergence(self, run_a: RunId, run_b: RunId) -> DivergenceReport: ...

    # --- evolution and portability (US4) ---
    def store_info(self) -> StoreInfo: ...
    def export_run(self, run_id: RunId) -> RunRecordSet: ...
    def import_run(self, records: RunRecordSet) -> RunId: ...
```

Bundle serialisation is **not** a store operation: `store/bundle.py` turns a `RunRecordSet` into a
directory or `.tar.gz` and back, over any `MatchTrackingStore`, so a bundle never depends on a
store's file layout (FR-017, FR-027).

## Rules

### Writing (Principle III floor, restated as owned here)

| # | Requirement |
|---|---|
| **W1** | `write_turn_cycle` additionally records each step's `ModelCall` as a `model_calls` row **in the same transaction**; the turn and its calls are durable together or not at all (FR-001, FR-002, FR-008) |
| **W2** | A retried write of an identical call under an existing `model_call_id` is a no-op; different content raises (D4 extended) |
| **W3** | The store re-derives and persists `Run.record_completeness_status` inside every write that can change it, using the rules of `store/completeness.py`; a reader never computes completeness (FR-010) |
| **W4** | `import_run` is the only write that may insert `archived_at`, an `eligible` save point, or a `run_archived` event verbatim; it is atomic, refuses an existing `run_id` naming the collision, and refuses a record set whose references do not close (FR-027) |
| **W5** | A store opened `read_only=True` raises `StoreWriteError` on every write and never migrates (R10) |

### Reading

| # | Requirement |
|---|---|
| **R1** | `list_runs` returns **every** run in every lifecycle state, archived included, in `started_at` descending order with NULLs last, then `run_id` (E1, FR-011) |
| **R2** | `query_runs` pages in the store (not in the caller), applies every filter the query names, and returns the **total** matching count; an archived run is filtered and sorted like any other (FR-011, SC-008) |
| **R3** | `get_run_configuration` resolves `run_id` and nothing else — a `config_id` answers `None` (E5, FR-012) |
| **R4** | `get_turn_cycle_attempt` returns exactly the attempt asked for, authoritative or not, or `None` if it was never recorded — never a substitute (FR-013). `list_turn_attempts` lists every attempt of a turn without loading steps |
| **R5** | `get_capture_image` answers one of `available` (with bytes), `withheld` (with the reason, no bytes), `missing` (record intact, `blob_ref` unresolvable on disk), `no_such_capture`. `get_capture_blob` is the E4 form: bytes only when `available`, else `None` (FR-014) |
| **R6** | `list_model_calls` and `model_call_totals` read `model_calls` rows only — never step bundles; totals equal the bundle-embedded sums by W1 (FR-015) |
| **R7** | `highest_recorded_turn` is the highest turn with any attempt or save point for the run; `0` when none (FR-017 — the read the web derived by probing) |
| **R8** | No read requires knowledge of the file layout; the blob directory is reachable only through `get_capture_image` / `get_capture_blob` (FR-017) |

### Trends

| # | Requirement |
|---|---|
| **T1** | `metric_series` excludes, by the store's rule, any run whose completeness is `has_gaps`/`unknown`, whose comparability is `not_comparable`, or which is `visually_degraded` unless the query opted in; every excluded run is named with its reason and its gaps (FR-019, SC-007) |
| **T2** | Metrics are the numeric keys of each turn's recorded `yields` plus derived `city_count` and `unit_count`; a metric the record does not carry yields an empty series with `unavailable_reason` set — never a fabricated zero (FR-018, R4) |
| **T3** | A non-terminal run's series carries `in_progress=True` (FR-021) |
| **T4** | `divergence` compares fingerprints over turns both runs hold authoritatively and reports the first unequal turn with every unequal dimension; a run excluded for gaps, unknown completeness or `not_comparable` produces no comparison and is named. Visual degradation concerns images, not the actions compared, so `visually_degraded` runs are admitted to divergence (the report carries no series and needs no opt-in) (FR-020) |

### Evolution and portability

| # | Requirement |
|---|---|
| **V1** | A file with no `store_meta` is version 1.0. Opening 1.0 in write mode copies the file (SQLite backup API) to `<file>.v1.0.bak-<UTC>` then migrates to 1.1 in one transaction, recording the migration; opening read-only refuses with the command to run (FR-024, FR-025) |
| **V2** | A file whose major version exceeds the reader's is refused with both versions named; no partial read (FR-026) |
| **V3** | Migration 1.0→1.1 reads every run unchanged and derives one `model_calls` row per step bundle whose call is absent (FR-025, SC-005) |
| **V4** | `export_run` returns the run's complete record: every attempt, every event, call, save point and capture, and the bytes of every kept image; `import_run` reproduces it verbatim so that counts, ordering, statuses and image bytes are identical (FR-027, SC-006) |
| **V5** | Bundle: directory canonical, `.tar.gz` of that directory as transport; manifest carries counts, per-file SHA-256, image hashes, source host, `source_store_id`, `bundle_format=1` and the store schema version; POSIX relative paths only (FR-028) |
| **V6** | Import refuses: an existing run id; a manifest hash mismatch; a missing image for a kept capture; a bundle format or schema **major** newer than the importer's — each named (FR-027) |

### Boundary

| # | Requirement |
|---|---|
| **B1** | No module under `civsim_harness/{observe,parity,agent,capability,act,provider}` imports `civsim_harness.store`; asserted by `tests/contract/test_store_boundary.py` (FR-029, Principle I) |

## Conformance tests

`tests/contract/test_match_store_port.py` keeps asserting the inherited floor, and gains the E1–E5
assertions the 002 contract deferred to this feature. `tests/contract/test_match_tracking_store.py`
asserts W1–W5, R1–R8, T1–T4, V1–V6 against `SqliteMatchStore`. `tests/integration/
test_web_against_tracking_store.py` runs the web interface's routes over the real store and asserts
no degraded-capability path is taken (FR-016, SC-003).
