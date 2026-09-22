# Tasks: Match-Tracking Data Store

**Input**: Design documents from `specs/003-match-tracking-store/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/match-tracking-store.md, quickstart.md

**Tests**: Included. The spec's Independent Tests and SC-001/SC-002/SC-003/SC-008 are buildable test
work (fault-injection suite, scale checks, web suite over the real store), and the constitution's
"never report a pass that was not observed" rule makes them the evidence, not decoration.

**Organization**: by user story, so each story is an independently testable increment. Every task
names its file. `[P]` = different files, no unfinished dependency.

## Format: `[ID] [P?] [Story] Description`

## Path Conventions

Single project: `src/civsim_harness/`, `tests/` at repository root (plan.md Project Structure).
Contract rule ids (W1…, R1…, T1…, V1…, B1) refer to `contracts/match-tracking-store.md`.

---

## Phase 1: Setup

**Purpose**: the error types and the value-type module every story imports.

- [X] T001 Add `StoreReadError`, `StoreSchemaError` and `BundleError` (all `HarnessError` subclasses, with the one-line docstrings naming FR-026 / FR-027) to `src/civsim_harness/errors.py`
- [X] T002 Create `src/civsim_harness/store/contract.py` with every read value type in data-model.md §3 (`RunSort`, `RunQuery`, `RunPage`, `TurnAttemptSummary`, `CaptureImageStatus`, `CaptureImage`, `ModelCallRow`, `ModelCallTotals`, `MetricPoint`, `MetricSeries`, `ExclusionReason`, `ExcludedRun`, `TrendQuery`, `TrendResponse`, `TurnFingerprint`, `DivergenceDetail`, `DivergenceReport`, `RunRecordSet`, `BundleManifest`, `StoreSchemaVersion`, `MigrationRecord`, `StoreInfo`) as frozen dataclasses / `HarnessModel`s with the validators the data model states (§3.3 content-iff-available, §3.5 exactly-one-of in `TrendQuery`, `RunQuery` bounds), and the `MatchTrackingStore(MatchStore, Protocol)` declaration with every operation in the contract's Operations block, each carrying a docstring that names its rule ids
- [X] T003 [P] Unit test in `tests/unit/test_store_contract_types.py`: each validator in T002 rejects the malformed shape (a `CaptureImage` with bytes but status `withheld`, a `TrendQuery` naming both `run_ids` and `seed_set_id`, a `RunQuery` with `page_size=0`), and `SqliteMatchStore` is an `isinstance` of `MatchTrackingStore` once Phase 2 lands (initially `xfail(strict=True)`, flipped in T012)

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: the versioned schema, the copy-first migration, and the adapter refactor every story's
read or write sits on. **No user story work can begin until this phase is complete.**

- [X] T004 Create `src/civsim_harness/store/schema.py`: `STORE_SCHEMA_VERSION = StoreSchemaVersion(1, 1)`; the full 1.1 DDL (002's seven tables plus `store_meta`, `schema_migrations`, the `runs` columns `started_at`/`ended_at`/`seed_set_id`/`record_completeness_status`/`comparability_status`, the `model_calls` columns `turn_number`/`step_index`, and the indexes of data-model.md §2); `detect_version(conn)` returning 1.0 when `store_meta` is absent; `initialise_new_store(conn, host_platform)` writing the meta rows (`store_id` uuid4, `created_at`, `host_platform`, `schema_major/minor`) — V1, V2
- [X] T005 Implement `migrate_1_0_to_1_1(db_path, conn)` in `src/civsim_harness/store/schema.py`: (1) `PRAGMA wal_checkpoint(TRUNCATE)` then copy via `sqlite3.Connection.backup` to `<db_path>.v1.0.bak-<UTC %Y%m%dT%H%M%SZ>`; (2) in one `BEGIN IMMEDIATE` transaction: create the new tables, `ALTER TABLE … ADD COLUMN` each new column, backfill `runs` columns from `run_json`/`config_json` and `model_calls.turn_number/step_index` by joining `turn_cycles`/`decision_steps`, derive one `model_calls` row per `decision_steps.bundle_json` whose `model_call.model_call_id` is absent (content = the embedded `ModelCall`, `turn_number`/`step_index` from the bundle), write the meta rows, and insert the `schema_migrations` row (`1.0->1.1`, `backup_path`, `detail = {model_calls_derived, runs_backfilled}`); (3) return the `MigrationRecord` — V1, V3, FR-025
- [X] T006 Implement `open_store_file(db_path, *, read_only, host_platform) -> tuple[sqlite3.Connection, StoreSchemaVersion]` in `src/civsim_harness/store/schema.py`: a fresh file is initialised at 1.1; a 1.0 file is migrated (write mode) or refused with `StoreSchemaError` naming `civsim store migrate` (read-only); a major greater than 1 is refused with `StoreSchemaError` naming both versions and reading nothing (V2, FR-026); a minor greater than 1 opens with a logged notice; read-only opens with the `file:…?mode=ro` URI
- [X] T007 Refactor `src/civsim_harness/store/sqlite_adapter.py`: replace `_SCHEMA_SQL`/`executescript` with `schema.open_store_file`; change `_lock` to `threading.RLock` (C2); add `read_only: bool = False` to `__init__`, and make `_run_in_transaction` raise `StoreWriteError("store opened read-only")` before touching the connection when set (W5); move every read's `body` into a private `_<read>_body(conn, …)` function callable from inside a transaction; keep every existing method's behaviour byte-for-byte (the 002 conformance suite must pass unchanged)
- [X] T008 Keep the indexed projection in sync in `src/civsim_harness/store/sqlite_adapter.py` (data-model §2 I-A): `create_run`, `update_run` and `archive_run` write `started_at`, `ended_at`, `seed_set_id` (from the configuration), `record_completeness_status`, `comparability_status` alongside `run_json`
- [X] T009 [P] Create `tests/contract/test_store_boundary.py`: import every module under `src/civsim_harness/{observe,parity,agent,capability,act,provider}` and assert none imports `civsim_harness.store` (walk `sys.modules` deltas per import, the pattern of `tests/unit/test_platform_neutrality.py`) — B1, FR-029
- [X] T010 [P] Unit test in `tests/unit/test_store_schema.py` (part 1): a fresh store reports 1.1 with a `store_id` and no migrations; a synthetic 1.0 file (built in the test from 002's seven-table DDL, with two runs, one two-attempt turn of three steps, a failed-call `model_calls` row, a withheld and a kept capture) migrates on write-open with the backup beside it, reads every run back **equal** to the pre-migration `Run`s, derives exactly `decision_steps − existing` model-call rows, records the migration; the same file opened read-only refuses naming the command; a file with `schema_major = 2` is refused naming `2.0` and `1.1` and no table is read; a file at 1.2 opens
- [X] T011 [P] Unit test in `tests/unit/test_store_schema.py` (part 2, the real file): when `<repo>/civsim-match-store.db` exists, copy it **and its `blobs/`** to `tmp_path`, migrate the copy, and assert ten runs read back with their recorded lifecycle/completeness/comparability, `model_calls` has one row per decision step plus the pre-existing failed-call rows, and every kept capture's `get_capture_image` answers `available` or `missing` (never raises); `pytest.skip` with the reason when the file is absent (SC-005)
- [X] T012 Flip T003's `xfail` and run `uv run pytest tests/contract/test_match_store_port.py tests/unit/test_store_schema.py tests/contract/test_store_boundary.py -q`; the 002 suite passes unchanged

**Checkpoint**: the file is versioned, the old file migrates with a rollback copy, the adapter is
refactored with no behaviour change. Stories can start.

---

## Phase 3: User Story 1 — A turn is on the record before the game moves on (Priority: P1) 🎯 MVP

**Goal**: the Principle III floor, owned here: model calls durable with their turn, completeness
maintained by the store, and the guarantees proven under fault injection.

**Independent Test**: `tests/unit/test_store_fault_injection.py` + the US1 section of
`tests/contract/test_match_tracking_store.py` (quickstart Scenario 1).

### Implementation for User Story 1

- [X] T013 [US1] In `src/civsim_harness/store/sqlite_adapter.py::write_turn_cycle`, insert each `DecisionStepBundle.model_call` into `model_calls` (with `turn_number`, `step_index`) inside the same transaction; an existing row with equal content is a no-op, different content raises `StoreWriteError` (W1, W2, FR-008). In `write_model_call`, resolve `turn_number`/`step_index` from `turn_cycles`/`decision_steps` when present, else NULL
- [X] T014 [US1] Implement store-owned completeness in `src/civsim_harness/store/sqlite_adapter.py`: `_derive_completeness_body(conn, run_id)` applying exactly `store/completeness.py`'s rules (`first_owed_turn`, the `turn_gaps` stopped-vs-playing rule, `step_gaps` over attempted turns) through the `_…_body` reads of T007, persisted to `run_json` + the column; invoked inside the transactions of `write_turn_cycle`, `mark_turn_superseded`, `write_save_point`, and `update_run` when `lifecycle_state` changes; expose `record_completeness(run_id)` as a read (W3, FR-009, FR-010)
- [X] T015 [P] [US1] Create `tests/contract/test_match_tracking_store.py` with the shared builders (import the record builders from `tests/contract/test_match_store_port.py` or lift them into `tests/contract/store_builders.py` used by both) and the US1 section: a 250-step turn reads back in order with observations, decisions and model calls attached and `model_calls` has 250 rows (US1/AC1); attempt 0 rewritten identically is accepted once (AC3); attempt 0 superseded + attempt 1 authoritative → attempt 1 served, attempt 0 retrievable via `get_turn_cycle_attempt`, no gap (AC4); quicksave then death before the turn on a `paused` run → gap + `has_gaps` from `record_completeness` (AC5); a withheld capture answers `withheld` with reason and no bytes (AC6); a run persisted with `complete` then a superseded attempt with no replacement reports `has_gaps` without any caller refreshing it (FR-010)
- [X] T016 [P] [US1] Create `tests/unit/test_store_fault_injection.py`: a `_FaultingConnection` proxy (wraps `sqlite3.Connection`, raises `sqlite3.OperationalError` on the *k*-th `execute`) injected via a test-only hook on `SqliteMatchStore` (`_connection_factory` parameter added in T007 or monkeypatched); for each write operation × each *k* from 1 to the operation's statement count (≥ 200 interruptions in total, counted and asserted), reopen the file and assert: the turn is wholly present or wholly absent at turn **and** step granularity, `model_calls` rows match the present steps exactly, the unwritten turn is a gap once the run is `paused`, and the identical rewrite is accepted (SC-001, FR-002, FR-003, FR-004)
- [X] T017 [P] [US1] Add the `SIGKILL` case to `tests/unit/test_store_fault_injection.py`: a `subprocess` writes 200-step turns in a loop to a `tmp_path` store; the parent kills it with `SIGKILL` after the first turn lands; the parent opens the file and asserts every present turn has all its steps and calls and the highest present turn is contiguous from 1 (skip on Windows where `SIGKILL` semantics differ, with the reason)
- [X] T018 [P] [US1] Create `tests/unit/test_store_scale.py` (part 1): a 300-turn × 100-step run (minimal observations) writes and reads back all 30,000 steps in order with no truncation, timed and reported (SC-002)
- [X] T019 [US1] Reader isolation test in `tests/contract/test_match_tracking_store.py`: a second `SqliteMatchStore(path, read_only=True)` opened while the writer holds an uncommitted `write_turn_cycle` (drive the adapter's transaction with a `threading.Event` inside a patched body) sees the previous turn and not the partial one; every write on the reader raises `StoreWriteError` (W5, spec Edge Cases)

**Checkpoint**: US1 is the MVP — a harness with only this store can play and be audited.

---

## Phase 4: User Story 2 — Every reader uses one published contract (Priority: P2)

**Goal**: the reads the web proved missing, plus paging, attempt addressing, tagged images and
model-call totals; the web takes no degraded path.

**Independent Test**: E1–E5 in `tests/contract/test_match_store_port.py`, the US2 section of
`tests/contract/test_match_tracking_store.py`, and `tests/integration/test_web_against_tracking_store.py`
(quickstart Scenarios 2 and 5).

### Implementation for User Story 2

- [X] T020 [US2] Create `src/civsim_harness/store/sqlite_reads.py` with `_TrackingReadsMixin` (expects `_with_lock`, `_blob_dir`, `_read_only` from the adapter) implementing `list_runs()` (R1 order), `query_runs(RunQuery)` (SQL `WHERE`/`ORDER BY`/`LIMIT`/`OFFSET` + `COUNT(*)`, NULL timestamps last — R2), `highest_recorded_turn(run_id)` (max over `turn_cycles.turn_number` and `save_points.turn_number` — R7), `list_turn_attempts(run_id, turn)` (from `turn_cycles` only, no steps), `get_turn_cycle_attempt(run_id, turn, attempt)` (R4); mix it into `SqliteMatchStore`
- [X] T021 [US2] Add to `src/civsim_harness/store/sqlite_reads.py`: `get_capture_image(capture_id) -> CaptureImage` (`no_such_capture` / `withheld` with reason / `available` with bytes read from `blob_dir/<h[:2]>/<h>` / `missing` when the record has a `blob_ref` the directory cannot resolve — R5, FR-014) and `get_capture_blob(capture_id)` defined as `image.content` (E4)
- [X] T022 [US2] Add to `src/civsim_harness/store/sqlite_reads.py`: `list_model_calls(run_id, *, turn=None, step=None)` ordered by `(turn_number NULLS LAST, step_index, model_call_id)` reading `model_calls` only, and `model_call_totals(run_id)` computed in SQL over `call_json` fields (`json_extract`) with `cost_usd = None` when no call priced (R6, FR-015)
- [X] T023 [P] [US2] Append the E1–E5 section to `tests/contract/test_match_store_port.py` (the 002 contract said "this is where E1–E5 get asserted against the real store"): `list_runs` enumerates finished, failed, paused **and archived** runs; `get_capture_blob` returns bytes for kept, `None` for withheld; `get_turn_cycle_attempt` returns attempt 0 when 1 is authoritative; `get_run_configuration(config_id)` answers `None` (E5, US2/AC5)
- [X] T024 [P] [US2] US2 section of `tests/contract/test_match_tracking_store.py`: 1,000 runs across every lifecycle state and 40 seed sets, `query_runs` sorted `started_at_desc` page 3 of 25 filtered to one seed set returns the right rows and total and an archived run appears like any other (AC1); attempt 0 of a two-attempt turn is never substituted (AC2); a 24-call run totals equal the bundle sums (AC3); `get_capture_image` answers `available` / `withheld` / `missing` (after deleting the blob file) / `no_such_capture` (AC4, R5); `highest_recorded_turn` counts a save-point-only trailing turn (R7); `list_turn_attempts` lists both attempts without loading steps
- [X] T025 [P] [US2] `tests/unit/test_store_scale.py` (part 2): 1,000 runs — every `RunSort`, every filter combination in the query, any page — each under two seconds, and one run's `model_call_totals` over 300 turns × 10 calls under one second; timings asserted with a 2× headroom over SC-008 and reported (SC-008)
- [X] T026 [US2] Create `tests/integration/test_web_against_tracking_store.py`: populate a `tmp_path` `SqliteMatchStore` (three runs incl. one archived and one with a replayed turn, kept and withheld captures), open it `read_only=True`, build the app via `tests/web_support/fixtures.make_client(store=…)`, and assert: the catalog response has `listing_is_partial is False`, configuration columns populated for every row; `GET /captures/{id}/image` returns the kept bytes with the image media type and the withheld one an explicit no-image answer; `GET /runs/{id}/turns/{n}?attempt=0` returns attempt 0 with `is_authoritative=false`; the live view resolves the current turn — with no `civsim_web` source change (FR-016, SC-003)

**Checkpoint**: the web renders no honest-unavailable panel over data that exists.

---

## Phase 5: User Story 3 — Trends only from records that can bear them (Priority: P3)

**Goal**: per-turn metric series and divergence points with the exclusion rule owned by the store.

**Independent Test**: `tests/unit/test_store_trends.py` and the US3 section of
`tests/contract/test_match_tracking_store.py` (quickstart Scenario 3).

### Implementation for User Story 3

- [X] T027 [P] [US3] Create `src/civsim_harness/store/trends.py` (pure, no SQLite): `turn_metrics(record: TurnCycleRecord) -> dict[str, float]` (numeric non-bool `yields` keys + `city_count`/`unit_count` from the last step's `cities.state`/`units.state` entries, absent keys omitted — T2, R4); `turn_fingerprint(record) -> TurnFingerprint` (actions as ordered `(action_declaration_id, canonical JSON of parameters)`); `first_divergence(a: Sequence[TurnFingerprint], b: …) -> tuple[int | None, tuple[DivergenceDetail, …]]`; `exclusion_for(run, completeness, gaps, *, include_visually_degraded) -> ExcludedRun | None` (T1)
- [X] T028 [P] [US3] Unit test in `tests/unit/test_store_trends.py`: metrics from yields and counts, booleans excluded, missing keys absent; fingerprint equality is order-sensitive on actions and parameter-canonical (`{"a":1,"b":2}` == `{"b":2,"a":1}`); first divergence names every differing dimension; exclusion rule for each `ComparabilityStatus` × completeness × override
- [X] T029 [US3] Add `metric_series(TrendQuery) -> TrendResponse` to `src/civsim_harness/store/sqlite_reads.py`: resolve the run set (`run_ids` or every run whose configuration has `seed_set_id`), apply `exclusion_for` with the store's own `record_completeness` + `turn_gaps`, walk each included run's authoritative turns (skipping gaps — none remain for an included complete run; an in-progress run's trailing turn is simply absent), build one `MetricSeries` per requested metric per run with `in_progress = lifecycle not terminal`, `unavailable_reason` when a metric never appears, `metric_names` = union observed, `excluded` named with reasons and gaps (T1–T3, FR-018, FR-019, FR-021)
- [X] T030 [US3] Add `divergence(run_a, run_b) -> DivergenceReport` to `src/civsim_harness/store/sqlite_reads.py`: both runs pass the exclusion rule (visually-degraded included when both are — record it), `same_seed_set` from configurations, fingerprints over the turns both hold authoritatively from `max(first_owed_turn)`, result from `first_divergence` (T4, FR-020)
- [X] T031 [P] [US3] US3 section of `tests/contract/test_match_tracking_store.py`: five runs of one seed set, two gapped → three series and two named exclusions with their gap (AC1, SC-007); two complete runs identical to turn 11 and differing at 12 → `first_divergent_turn == 12` with `actions` named (AC2); a playing run is `in_progress=True` with its turns so far (AC3); a `not_comparable` run is excluded regardless of the override; `visually_degraded` excluded by default and included with the flag and `included_visually_degraded=True` on the response; a metric never recorded yields `unavailable_reason`

**Checkpoint**: deliverable 4 cannot optimise against a gapped record through this contract.

---

## Phase 6: User Story 4 — Evidence moves between hosts intact (Priority: P4)

**Goal**: export/import as one portable bundle; the old file opens; store info.

**Independent Test**: `tests/unit/test_store_bundle.py`, `tests/unit/test_store_schema.py`,
`tests/unit/test_store_cli.py` (quickstart Scenarios 4 and 6).

### Implementation for User Story 4

- [X] T032 [US4] Add `store_info() -> StoreInfo` (schema version, `store_id`, host, `read_only`, per-kind counts, migrations, `runs_with_absent_parent`) and `export_run(run_id) -> RunRecordSet` (every attempt via `turn_cycles` ordered by turn then attempt, events, calls, save points, captures, kept images via `get_capture_image`; `StoreReadError` for an unknown run) to `src/civsim_harness/store/sqlite_reads.py` (V4)
- [X] T033 [US4] Add `import_run(records: RunRecordSet) -> RunId` to `src/civsim_harness/store/sqlite_adapter.py`: one transaction; refuse `BundleError` naming the run id when it exists, refuse when a step's `turn_cycle_id`, a call's `decision_step_id`, or a kept capture's `blob_ref` is not carried by the set; write images first (content-addressed), then rows **verbatim** (including `archived_at`, `eligible`, `run_archived`), then the projection columns and completeness (W4, C1, FR-027)
- [X] T034 [P] [US4] Create `src/civsim_harness/store/bundle.py`: `write_bundle(records, dest_dir, *, archive=False) -> Path` writing the data-model §3.7 layout (`manifest.json`, `run.json`, `configuration.json` by alias, `*.jsonl`, `images/<sha256>`) with SHA-256 per file and image, POSIX relative paths, `bundle_format=1`, `source_host`, `source_store_id`, the store schema version; `archive=True` additionally produces `<dir>.tar.gz` of exactly that directory; `read_bundle(path) -> RunRecordSet` accepting a directory or a `.tar.gz`, verifying every hash and refusing (`BundleError`, naming the cause) a mismatch, a missing image, or a newer `bundle_format` / schema major (V5, V6, FR-028)
- [X] T035 [P] [US4] Unit test in `tests/unit/test_store_bundle.py`: a finished run with 3 turns (one replayed), 6 captures (2 withheld), 21 model calls, an archive event and eligible saves exports → directory and archive → imports into an empty store with identical counts per kind, identical ordering, byte-identical images, withheld still withheld, identical completeness/comparability/archived statuses (US4/AC1, SC-006); a second import is refused naming the run id (AC2); a tampered file, a deleted image, and `bundle_format=2` are each refused naming the cause (V6); manifest paths contain no backslash and no absolute path (FR-028); a branch whose parent is absent imports and `store_info().runs_with_absent_parent` names it
- [X] T036 [US4] Create `src/civsim_harness/operator/store_cli.py` (`store_app = typer.Typer()`), commands `info`, `migrate [--dry-run]` (dry-run reports the detected version and what would change without copying or writing), `runs [--seed-set] [--state] [--page] [--page-size] [--sort]`, `model-calls RUN_ID [--turn] [--step]` (rows + totals), `export RUN_ID --out DIR [--archive]`, `import PATH`; every command opens the store through `cli._open_store`-style resolution with `--store` / `CIVSIM_STORE_PATH`, `info` and `runs` and `model-calls` open **read-only**; exit code 2 with the error's message on `StoreSchemaError`/`BundleError`/`StoreReadError`
- [X] T037 [US4] Register `store_app` in `src/civsim_harness/operator/cli.py` (`app.add_typer(store_app, name="store")`) — one line beside the existing sub-apps
- [X] T038 [P] [US4] Unit test in `tests/unit/test_store_cli.py` with `typer.testing.CliRunner`: `info` on a fresh store prints `1.1`; `migrate --dry-run` on a synthetic 1.0 file changes nothing and names the backup path it would take; `migrate` performs it; `export --archive` then `import` on a second `--store` round-trips; a second `import` exits 2 naming the collision; `model-calls` prints the totals line matching `model_call_totals`; `runs --seed-set` filters
- [X] T039 [US4] Wire the web's store opening for the reference adapter: document in `specs/003-match-tracking-store/quickstart.md` (Scenario 5) the exact `--store module:attribute` value that opens `SqliteMatchStore` read-only — add `open_read_only(path: str | None = None) -> SqliteMatchStore` to `src/civsim_harness/store/__init__.py` (path from `CIVSIM_STORE_PATH` when omitted) so `civsim-web --store civsim_harness.store:open_read_only` needs no code in `civsim_web`; test it in `tests/unit/test_store_cli.py`

**Checkpoint**: a run's evidence crosses hosts as one file, and the old file is not stranded.

---

## Phase 7: User Story 5 — Archival and retention never touch the record (Priority: P5)

**Goal**: the 002 archival promise restated as this store's own, proven before/after.

**Independent Test**: US5 section of `tests/contract/test_match_tracking_store.py` (quickstart Scenario 7).

### Implementation for User Story 5

- [X] T040 [US5] US5 section of `tests/contract/test_match_tracking_store.py`: snapshot every record kind (turn cycles all attempts, steps, captures, events, model calls, configuration, `store_info().counts`) before `archive_run`, archive, snapshot after — everything identical except each retained save point now `eligible`, plus exactly one new `run_archived` event (SC-009, US5/AC1); the archived run still lists in `list_runs`/`query_runs` and, when complete, in `metric_series`; `archive_run` on a `paused` run is refused (AC2); a `paused` run made terminal via the existing `run abandon`/`update_run(lifecycle_state=failed, stop_resolution=…)` path can then be archived (spec Edge Cases)
- [X] T041 [P] [US5] Adversarial preset test in `tests/unit/test_retention.py` (extend): a saves directory holding `CivSim DEFAULT.Civ6Cfg` beside two quicksaves, one archived run and one finished-unarchived run — `list_eligible_save_points` names only the archived run's saves, `saves.reaper.plan_reap` never names the preset or the unarchived run's files, and a `SavePoint` with a `.Civ6Cfg`-shaped name cannot be constructed (FR-023, US5/AC3)

---

## Phase 8: Polish & Cross-Cutting Concerns

- [X] T042 [P] `uv run ruff check src/civsim_harness/store src/civsim_harness/operator/store_cli.py tests/unit/test_store_*.py tests/contract/test_match_tracking_store.py tests/contract/test_store_boundary.py tests/integration/test_web_against_tracking_store.py` and `uv run mypy --strict src/civsim_harness/store src/civsim_harness/operator/store_cli.py` clean
- [X] T043 [P] Module docstring on `src/civsim_harness/store/__init__.py` stating the package now implements deliverable 3 (`contracts/match-tracking-store.md`), which module owns which rule ids, and that `civsim_web` is unmodified by design (FR-016)
- [X] T044 Full suite `uv run pytest -q` green (baseline 1650 passed / 8 skipped — the skips are Windows/macOS-only paths); record the new totals in `specs/003-match-tracking-store/validation-results.md` together with the measured SC-002 and SC-008 timings and the SC-001 interruption count printed by the tests
- [X] T045 Run quickstart Scenario 6 against a **copy** of the real 2026-09-21 file (Linux host only) and record in `specs/003-match-tracking-store/validation-results.md`: ten runs, model-call rows per run, the sum of `cost_usd` over the five model-driven runs (SC-004's reconciliation figure against the reported $1.42), and the backup file name; never touch `civsim-match-store.db` itself
- [X] T046 Record in `specs/003-match-tracking-store/validation-results.md` what quickstart Scenario 8 (the owner's "step 3.5" model-driven run through the new store) needs and that it was **not** run (client, account, ~$0.30) — never a fabricated pass

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: none — start immediately
- **Foundational (Phase 2)**: depends on Phase 1 — **blocks every story**
- **US1 (Phase 3)**: after Phase 2. The MVP
- **US2 (Phase 4)**: after Phase 2; T024/T026 assert model-call totals and so also need T013
- **US3 (Phase 5)**: after Phase 2; `metric_series` uses T014's `record_completeness`
- **US4 (Phase 6)**: after Phase 2; `export_run` uses T021 (`get_capture_image`); T035 archival round-trip needs nothing from US5 (uses `archive_run` as shipped)
- **US5 (Phase 7)**: after Phase 4 (`list_runs`) and Phase 5 (`metric_series` inclusion assertion)
- **Polish (Phase 8)**: after every story

### Within each story

Pure modules (`trends.py`, `bundle.py`, `contract.py`) and their unit tests before the adapter
methods that use them; adapter reads before the contract tests; the web integration test last.

### Parallel opportunities

- Phase 1: T003 beside T001/T002. Phase 2: T009, T010, T011 beside T004–T008.
- Phase 3: T015, T016, T017, T018 in parallel once T013/T014 land.
- Phase 4: T023, T024, T025 in parallel after T020–T022; T026 after them.
- Phase 5: T027+T028 first (pure), then T029/T030, then T031.
- Phase 6: T034+T035 beside T032/T033; T036–T038 after; T039 any time after T007.
- Stories US2, US3 and US4 touch different methods of `sqlite_reads.py` — sequence their edits to that one file, everything else in parallel.

---

## Parallel Example: User Story 1

```bash
# after T013 and T014:
Task: "US1 contract section in tests/contract/test_match_tracking_store.py"        # T015
Task: "fault-injection proxy suite in tests/unit/test_store_fault_injection.py"    # T016
Task: "SIGKILL case in tests/unit/test_store_fault_injection.py"                   # T017
Task: "30,000-step scale test in tests/unit/test_store_scale.py"                   # T018
```

---

## Implementation Strategy

### MVP first (User Story 1 only)

1. Phase 1 → Phase 2 (schema, migration, refactor; the 002 suite still green).
2. Phase 3: model calls with their turn, store-owned completeness, fault injection.
3. **STOP and validate**: quickstart Scenario 1; the old file migrates (Scenario 6 on a copy).

### Incremental delivery

- + US2 → the web takes no degraded path (Scenario 5) — the biggest visible change.
- + US3 → trends and divergence.
- + US4 → bundles, CLI, store info.
- + US5 → archival proven before/after.
- Each story leaves the suite green and is committed on its own.

---

## Notes

- `civsim_web` is **never edited** here (FR-016). The three probe Protocols it still declares are a
  001 follow-up (plan.md Cross-spec follow-ups).
- `specs/002/**` is not edited; the contract amendment pointing 002's port at this feature is
  reported to the hypervisor.
- The real store file is only ever **copied**; nothing in these tasks writes to it.
- Never report a pass that was not observed: T045/T046 record what ran and what did not.

---

## Phase 9: Convergence (2026-09-22)

`/speckit-converge` re-check, attempt 2, against the shipped code after all 46 prior tasks were
`[X]` and the suite green at head `c7b5654` (**2138 passed / 19 skipped / 0 failed**, 203.89 s).
Assessed: FR-001 – FR-029, the buildable success criteria (SC-001 – SC-009), US1 – US5's
acceptance scenarios, the Edge Cases, `contracts/match-tracking-store.md`'s W1–W5 / R1–R8 /
T1–T4 / V1–V6 / B1, plan.md's C1 – C3, and Constitution Principles I, III, IV and VI.
Report: [analyze-2026-09-22.md](./analyze-2026-09-22.md).

**No constitution MUST is violated and nothing below is a Principle I leak.** The import boundary
still holds statically and at runtime, atomicity still survives 354 injected faults and a real
`SIGKILL`, and trend exclusion is still the store's rule rather than the reader's. The findings
have one shape in common, and it is not last pass's: **this store's published surface has grown
past the document that publishes it, and one requirement is enforced by nothing but the absence
of the code that would break it.** A contract narrower than its Protocol is how 001 ended up with
four probed capabilities; a requirement with no check is how `_read_setting` and `doctor`'s
hard-coded `0` survived.

- [X] T047 **HIGH** Assert structurally that the store exposes no operation that deletes or edits a
  record, per FR-006 (missing). **The finding**: FR-006's second clause — "the store MUST expose no
  operation that deletes or edits a turn, step, capture, event or model call" — is satisfied today
  by omission and by nothing else. Every `def` on `MatchStore` (`store/port.py`),
  `MatchTrackingStore` (`store/contract.py`) and `SqliteMatchStore` (`store/sqlite_adapter.py`,
  `store/sqlite_reads.py`) was read: the mutating surface is exactly `create_run`, `update_run`,
  the nine `write_*`, `mark_turn_superseded`, `archive_run` and `import_run`, and nothing deletes
  or edits. **Nothing asserts that.** There is no operation enumeration as data anywhere in
  `civsim_harness/store/`, no AST or `hasattr` scan, and the identifier `FR-006` appears nowhere in
  this feature's code or tests — every repo hit is 002's unrelated FR-006, the run-identity lock.
  Adding `delete_turn_cycle()` tomorrow would leave all 2138 tests green while breaking the
  immutability floor this store inherits verbatim from the 002 contract and that Principle III
  depends on. **The fix pattern already exists one package away and was built for the wrong port**:
  `tests/contract/test_read_only_boundary.py` enumerates `READ_OPERATIONS`/`WRITE_OPERATIONS` as
  data and AST-scans against them — for `civsim_web`'s *client*, not for the store that actually
  holds the records. Do the same here: publish the permitted mutating operations as data beside the
  Protocol, and assert no other mutating operation exists on `MatchTrackingStore` or
  `SqliteMatchStore`. **Pair it with a negative control that is run and recorded** — a guard nobody
  has watched fail is the same defect again.
- [X] T048 **MEDIUM** Publish `list_captures` and `trend_exclusion` in the contract's Operations
  block, per `contracts/match-tracking-store.md` (contradicts). The block declares 17 operations;
  `MatchTrackingStore` in `store/contract.py` declares 19.
  `list_captures(run_id) -> list[ScreenCapture]` (contract.py:487) is called by
  `store/coverage.py:849` and pinned by
  `test_us2_r5_list_captures_enumerates_one_run_s_records_without_reading_a_blob`;
  `trend_exclusion(run_id) -> ExcludedRun | None` (contract.py:517) is called by
  `operator/store_cli.py:276` and pinned by three tests in `tests/contract/test_match_tracking_store.py`.
  **Rule T1 already refers to `trend_exclusion` in its own prose while the block above it does not
  declare it**, so the document disagrees with itself; `list_captures` appears in no artifact at
  all — not the contract, not `data-model.md`, not this file. Add both with their rule ids
  (`list_captures` under R5, `trend_exclusion` under T1). This is the same failure 001 spent four
  tasks absorbing, beginning again from the publishing side.
- [X] T049 **MEDIUM** Correct rule T1's research citation, per `contracts/match-tracking-store.md`
  and `research.md` (contradicts). T1 attributes the `game_turn_did_not_advance` exclusion reason to
  "research R14". `research.md`'s R14 is **"Scale checks (SC-002, SC-008)"**, and the phrase appears
  nowhere in R1 – R14. The behaviour is real and well built
  (`store/completeness.py::turns_whose_game_turn_did_not_advance`,
  `contract.py::ExclusionReason.GAME_TURN_DID_NOT_ADVANCE`, `sqlite_reads.py:737`, six cases in
  `tests/unit/test_completeness.py`) and `data-model.md` §3.5 documents it properly — only the
  citation is wrong. R6 ("Trend exclusion rule and the visually-degraded question") is the item that
  should carry the decision and today carries nothing about it. Either extend R6 and point T1 at it,
  or drop the citation and let `data-model.md` be the reference. A citation to a research item that
  does not discuss the thing reads as due diligence that did not happen.
- [X] T050 **MEDIUM** Document the open-time orphan sweep and the shipped `civsim store` surface,
  per `contracts/match-tracking-store.md` W1 – W5 and `quickstart.md` (partial).
  `SqliteMatchStore.__init__` (`store/sqlite_adapter.py:109-150`) now runs `sweep_orphans` on every
  **write-mode** open, pausing runs whose identity lock is absent or whose holder pid is dead and
  recording a `lifecycle_transition` event with `reason: orphaned` — a write that happens before the
  caller has issued one, in a rule set meant to be the complete statement of what this store writes
  and when. The implementation is **correct** (skipped entirely for `read_only=True`, so W5 holds,
  and the docstring says so), which is exactly why this is a documentation gap rather than a defect.
  Alongside it the CLI ships nine commands — `info`, `migrate`, `runs`, `model-calls`, `coverage`,
  `export`, `import`, `repair` — where T036 and `quickstart.md` name six. Add a **W6** stating the
  sweep, its write-mode-only scope, and its relationship to W5, and list the shipped CLI surface.
  **`coverage` and `repair` were authored by the 002 lane in this feature's files** (`9f200d5`): the
  job here is to document what ships, not to claim the commands or move them.
- [X] T051 **LOW** Assert SC-004's reconciliation instead of only recording it, per SC-004
  (partial). The `$1.424694` total over the five priced runs of 2026-09-21 exists only in
  `validation-results.md` as a manual T045 observation; `tests/` contains no hit for `1.42`,
  `1.424694` or `SC-004`. `tests/unit/test_store_schema.py::test_the_real_pre_feature_file_migrates_and_reads_back`
  already opens a **copy** of the real file behind a `skipif` and asserts run and model-call counts —
  it is the natural home for the dollar total and does not have it, so a migration change that
  mis-derived costs would pass. Add the total there; it stays skipped on any host without the file,
  which is the correct behaviour. The observation was honestly made and honestly recorded — this
  converts the project's own evidence into a check, it does not doubt it.
- [X] T052 **LOW** Exercise FR-018's four unused metric names, per FR-018 (partial). FR-018 names
  "science, culture, gold, faith, production and food per turn, plus city and unit counts".
  `store/trends.py::turn_metrics` is a generic pass-through of `TurnCycle.yields`' numeric non-bool
  keys, so all six work by construction — but `gold`, `faith`, `production` and `food` appear in no
  fixture in `tests/unit/test_store_trends.py` or `tests/contract/test_match_tracking_store.py`.
  Add the four to one existing fixture so the requirement's named set is demonstrated rather than
  implied. **The real-data half stays blocked on 002** and stays recorded as such: 002's
  `compute_yields` is a no-op, so all six yield series are empty on every run this project has
  recorded (`validation-results.md`, plan.md Cross-spec follow-ups). Nothing here fabricates a yield.
- [X] T053 **HIGH** Point the SC-005 real-file test at the file it is about, per SC-005 and
  contract V3 (contradicts). **`tests/unit/test_store_schema.py::test_the_real_pre_feature_file_migrates_and_reads_back`
  had stopped testing.** It copied `REAL_FILE` — `civsim-match-store.db` — and called
  `pytest.skip("the real file is already at {version}; nothing to migrate")` whenever that file
  was no longer at schema 1.0. Ordinary use of this host migrated it to 1.1 on 2026-09-21 and it
  has since grown to 44 runs, so from that moment the test skipped at runtime **on the only
  machine that has the file**, and everything below the skip — including `assert len(before) == 10`,
  which is SC-005's entire claim — became dead code. A `skip` reads as "not applicable here",
  which is how a guard stops guarding with nobody noticing. Fourth instance of this project's
  recurring failure and the first where the check was not merely narrow but wholly inert.
  The file it was always about is `civsim-match-store.db.v1.0.bak-20260921T152108Z`, the frozen
  1.0 backup the migration itself took: ten runs, 97 decision steps, no `model_calls` rows.
  Repoint it there, turn the `nothing to migrate` skip into an assertion, and keep the real file
  copy-only.

### Phase 9 notes — what closed, and the one that had stopped testing

Written when T047–T053 landed. **All 53 tasks are now `[X]`.**

**T047 gave FR-006 its first check.** `MUTATING_OPERATIONS` in `store/contract.py` publishes the
closed mutating surface as data — the ten operations that exist and no others — and
`tests/contract/test_store_boundary.py` partitions the public surface of both
`MatchTrackingStore` and the concrete `SqliteMatchStore` against it, then scans every public
operation name against a delete/edit vocabulary. The partition is the load-bearing half: the read
set is pinned literally rather than derived as "not mutating", which would have made the check a
tautology, and it walks the MRO, so a mutating method added to the SQLite read base is caught too.
**Revert confirmation, run and recorded**: with `def delete_turn_cycle(self)` temporarily on
`SqliteMatchStore`, two tests fail —
`extra=['delete_turn_cycle'], missing=[]` from the partition, and
`{'delete_turn_cycle': ['delete']}` from the vocabulary scan, each naming FR-006 — and with the
method removed, eight pass. A requirement about what a type does *not* have cannot be tested by
calling it; it has to be tested by looking at it.

**T048–T050 brought the contract back level with the code.** `list_captures` and
`trend_exclusion` are in the Operations block with their rules (R5 and T1); T1's citation now
points at **R6**, which gained the decision it should always have carried — and R6 was the right
home all along, since it is the trend-exclusion research item. The wrong `R14` citation had spread
further than the contract: it was also in `store/contract.py` (twice), `store/completeness.py`
(twice), `store/sqlite_reads.py`, `store/trends.py` and `data-model.md` §3.5. All seven now read
R6. **W6** states the open-time orphan sweep — write-mode only, `paused` only, the event it
writes, the grace window, the `orphan_sweep=False` opt-out, and that W5 is unaffected — and the
Conformance section says plainly that W6's tests live in the 002 lane's files rather than leaving
a reader to find a rule with no test named for it.

**The CLI ships eight commands, not nine.** T050's own text says nine; it is wrong, and the count
is corrected here rather than in the task, per the standing rule that a closed task's text records
what the contributor actually hit. `info`, `migrate`, `runs`, `model-calls`, `coverage`, `export`,
`import`, `repair`. Separately, `operator/store_cli.py`'s module docstring says "Seven commands"
above a list of eight — **that file is outside this lane's permitted paths and was not edited**;
the correction was reported to the hypervisor instead of taken unilaterally.

**T053 is the one that matters, and it was not on the list when this phase was written.**

`test_the_real_pre_feature_file_migrates_and_reads_back` had stopped testing. It copied
`civsim-match-store.db` and called `pytest.skip("the real file is already at {version}; nothing
to migrate")` whenever that file was no longer at 1.0. Ordinary use of this host migrated it on
2026-09-21 — it is now 1.1 and holds 44 runs — so from that moment the test skipped at runtime on
**the only machine that has the file**, and every assertion below the skip, including
`assert len(before) == 10`, which is SC-005's entire claim, became dead code. Nothing was red.
Nothing was even yellow in a way anyone reads: a skip says "not applicable here".

This is the fourth time this project has found a check that was not checking — after 002's
`_read_setting` comparing a value to itself, `doctor`'s hard-coded `0`, and the four narrow audits
of 2026-09-21 — and it is the first where the check was not merely narrow but **wholly inert**.
The three before it were written wrong. This one was written right and then *aged* out of being a
test, because it was keyed to a file whose job is to change. That is a distinct failure mode and
worth naming: **a fixture that the system under test keeps writing to is not a fixture.**

The fix keys it to `civsim-match-store.db.v1.0.bak-20260921T152108Z`, the 1.0 backup the migration
itself took — ten runs, 97 decision steps, no `model_calls` rows, and a timestamp in its name that
guarantees nothing will move it — and turns the `nothing to migrate` skip into an assertion that
says why it must not be softened back. The real file is still only ever copied; its mtime was
checked before and after.

**And the moment it ran again, it failed — correctly.** The snapshot contains
`run-54a3cefb5024425488cbf3a54286d670`, left `preparing` on 2026-09-21 with no identity lock. A
default write-mode open runs W6's sweep, which pauses it and writes a `lifecycle_transition` event
with `reason: orphaned`. So the run no longer read back equal to its pre-migration `run_json`, and
**SC-005's and V3's "every run reads back unchanged" is false under a default open** — true of the
migration, not true of the open. Nobody knew, because the test had been skipping since the day the
sweep landed.

The resolution separates the two claims rather than relaxing either. Migration fidelity is asserted
with `orphan_sweep=False`, so V3 still proves the migration rewrites nothing. Then the same
migrated copy is re-opened at the default and **W6 is asserted on real data**: the sweep pauses
exactly the runs that are genuinely nobody's, every one of them was `preparing`/`playing` before,
and no other run's lifecycle state moves. `tests/unit/test_orphans.py` builds fixtures that are
orphans; this file merely *is* one, which is the stronger evidence and the first of its kind here.

**T051 and T052** closed as written. SC-004's $1.424694 is now asserted against the same frozen
snapshot (five priced runs, cent-rounded to the spec's $1.42, plus the exact sum to 1e-6), and
FR-018's six named yields plus the two derived counts are exercised in one fixture. **The
real-data half of FR-018 stays blocked on 002** and stays recorded as such: `compute_yields` is a
no-op, so every yield series is empty on every run this project has actually recorded. Nothing
here fabricates a yield outside a test fixture.
