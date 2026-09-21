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
