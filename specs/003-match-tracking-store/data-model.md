# Data Model: Match-Tracking Data Store

**Feature**: `003-match-tracking-store` | **Date**: 2026-09-21 | **Plan**: [plan.md](./plan.md)

The persisted record shapes are **002's models, unchanged** (`src/civsim_harness/models/`): `Run`,
`RunConfiguration`, `TurnCycle`, `DecisionStep`, `Observation`, `Decision`, `ModelCall`,
`ScreenCapture`, `SavePoint`, `RunEvent`, and the write bundle `TurnCycleRecord` /
`DecisionStepBundle` (`store/port.py`). This feature adds no persisted record kind and changes no
field on any of them (spec Assumptions: "changes nothing about what the harness records or when").
What it adds is (§1) the store file's own metadata, (§2) the indexed projection of existing records,
and (§3) the *read value types* the new contract returns.

---

## 1. Store metadata (new tables)

### 1.1 `StoreSchemaVersion`

| Field | Type | Notes |
|---|---|---|
| `major` | int ≥ 1 | A reader refuses a file whose major is greater than its own (FR-026) |
| `minor` | int ≥ 0 | Additive changes only; a higher minor than the reader's is readable (FR-024) |

Rendered `"1.1"`. **A file with no `store_meta` table is version 1.0 by definition** — that is the
2026-09-21 pre-feature file. This feature's store writes and requires **1.1**.

### 1.2 `store_meta` (key/value)

| Key | Value |
|---|---|
| `schema_major`, `schema_minor` | see §1.1 |
| `store_id` | UUID assigned when the file was created or first migrated; carried in every bundle's manifest as `source_store_id` |
| `created_at` | UTC ISO-8601 |
| `host_platform` | `platform.system()` of the creating host — provenance for bundles, never a path |

### 1.3 `MigrationRecord` (`schema_migrations`)

| Field | Type | Notes |
|---|---|---|
| `migration_id` | str PK | e.g. `1.0->1.1` |
| `from_version` / `to_version` | str | |
| `applied_at` | Timestamp | |
| `backup_path` | str | The pre-migration copy (the rollback); a platform-local path, operator-facing only |
| `detail` | dict | e.g. `{"model_calls_derived": 97, "runs_backfilled": 10}` |

**Validation**: a migration record exists for every version step the file has taken; `store_info()`
reports them (FR-024).

## 2. Indexed projection of existing records (schema 1.1, additive)

| Table | Added columns (all backfilled by migration, all NULL-tolerant) | Purpose |
|---|---|---|
| `runs` | `started_at`, `ended_at`, `seed_set_id`, `record_completeness_status`, `comparability_status` | FR-011 paging/sort/filter in SQL; SC-008 |
| `model_calls` | `turn_number`, `step_index` (NULL when the call preceded any turn record — a failed call, FR-042) | FR-008 addressing by run, turn, step |

Indexes: `runs(started_at)`, `runs(seed_set_id)`, `runs(lifecycle_state)`,
`model_calls(run_id, turn_number, step_index)`.

**Invariants**

- I-A: every column above is a projection of `run_json` / `config_json` / `call_json`; the JSON is
  authoritative and the column is rewritten whenever the JSON is (`create_run`, `update_run`,
  `archive_run`).
- I-B: `write_turn_cycle` inserts one `model_calls` row per `DecisionStepBundle` in the same
  transaction as the turn (R3). A row already present with identical content is a no-op; different
  content under the same `model_call_id` raises (D4 extended to calls).
- I-C: the store re-derives and persists `Run.record_completeness_status` inside the transaction of
  every write that can change it (`write_turn_cycle`, `mark_turn_superseded`, `write_save_point`,
  `update_run` of `lifecycle_state`) using the **same** rules as `store/completeness.py`
  (FR-010). `import_run` carries the source store's accounting verbatim — the imported run must
  read back identically (SC-006), and its records are the same records the source derived from.

## 3. Read value types (new, `store/contract.py`)

All are frozen dataclasses or `HarnessModel`s; none is persisted.

### 3.1 Listing

| Type | Fields |
|---|---|
| `RunSort` (enum) | `started_at_desc` (default), `started_at_asc`, `ended_at_desc`, `run_id_asc` — NULL timestamps sort last |
| `RunQuery` | `page ≥ 1` (default 1), `page_size 1..500` (default 25), `sort`, `seed_set_id: str \| None`, `lifecycle_states: frozenset[LifecycleState] \| None`, `completeness: frozenset[RecordCompletenessStatus] \| None`, `comparability: frozenset[ComparabilityStatus] \| None`, `archived: bool \| None` (None = archived runs appear like any other, FR-011) |
| `RunPage` | `runs: tuple[Run, ...]`, `total: int`, `page`, `page_size`, `sort` |

### 3.2 Attempts

| Type | Fields |
|---|---|
| `TurnAttemptSummary` | `turn_number`, `attempt_index`, `is_authoritative`, `outcome`, `step_count`, `turn_cycle_id` |

### 3.3 Captures

| Type | Fields |
|---|---|
| `CaptureImageStatus` (enum) | `available`, `withheld`, `missing`, `no_such_capture` |
| `CaptureImage` | `status`, `capture_id`, `content: bytes \| None` (exactly when `available`), `withheld_reason: WithheldReason \| None` (exactly when `withheld`), `blob_ref: str \| None` |

**Validation**: `content` is set iff `status == available`; `withheld_reason` iff `status ==
withheld`; a `missing` answer carries the `blob_ref` that could not be resolved (FR-014).

### 3.4 Model calls

| Type | Fields |
|---|---|
| `ModelCallRow` | `call: ModelCall`, `turn_number: int \| None`, `step_index: int \| None` |
| `ModelCallTotals` | `run_id`, `call_count`, `priced_call_count`, `cost_usd: float \| None` (None when no call carried a dollar amount), `input_tokens`, `output_tokens`, `total_tokens` (each `int \| None`, summed over calls that reported them), `latency_ms_total`, `fallback_count`, `retry_count`, `calls_by_outcome: dict[CallOutcome, int]` |

**Validation**: `cost_usd` equals the sum of `cost.amount_usd` over the run's `model_calls` rows;
the same sum over the step bundles is equal by I-B (spec US2 scenario 3).

### 3.5 Trends

| Type | Fields |
|---|---|
| `MetricPoint` | `turn: int`, `value: float` |
| `MetricSeries` | `run_id`, `metric`, `points: tuple[MetricPoint, ...]` (ascending turns, gapped turns absent), `in_progress: bool` (FR-021), `comparability_status`, `unavailable_reason: str \| None` (set, with empty points, when the record carries no such metric — R4) |
| `ExclusionReason` (enum) | `has_gaps`, `game_turn_did_not_advance`, `completeness_unknown`, `not_comparable`, `visually_degraded`, `no_such_run`, `not_in_seed_set` |
| `ExcludedRun` | `run_id`, `reason`, `detail: str`, `gaps: tuple[int, ...]` |
| `TrendQuery` | exactly one of `run_ids: Sequence[RunId]` / `seed_set_id`; `metrics: Sequence[str] \| None` (None = every metric the records carry); `include_visually_degraded: bool = False` |
| `TrendResponse` | `series: tuple[MetricSeries, ...]`, `excluded: tuple[ExcludedRun, ...]`, `metric_names: tuple[str, ...]`, `included_visually_degraded: bool` |

**Derived metrics**: every numeric, non-boolean key of `TurnCycle.yields`, plus `city_count` and
`unit_count` from the authoritative attempt's last step observation (`cities.state.cities`,
`units.state.units`). Nothing else is ever synthesised (R4).

**Exclusion rule (store-owned, FR-019)**: a run is excluded when its record carries game turns
that did not advance (below), or its store-derived completeness is `has_gaps` or `unknown`, or its
`comparability_status` is `not_comparable`, or it is `visually_degraded` and the query did not opt
in. Excluded runs never contribute a point. `MatchTrackingStore.trend_exclusion(run_id)` publishes
the same verdict as its own read, so a listing can show eligibility without requesting a series.

**Game turns that did not advance (added 2026-09-21, research R6; Constitution Principle III)**: a
gap-free record is not automatically a trendable one. Gameplay block 7 (`run-480aa573`) recorded
five turn cycles *all at game turn 35* — every turn and every step present — because each end turn
was dispatched and then `verification_failed` after the bound, so the harness's turn ended and the
game's did not. Such a run is excluded as `game_turn_did_not_advance`, for the same reason a gapped
one is: its turn-by-turn record does not describe turns the game actually played. The rule
(`store/completeness.py::turns_whose_game_turn_did_not_advance`) reads **two** signals over the
run's authoritative attempts in turn order, and either alone is sufficient:

- `TurnCycle.game_turn_advanced is False` — the writer's own statement, set when the attempt ends
  `end_turn_unconfirmed`.
- consecutive attempts whose recorded game turn (`game.turn_state.turn_number` in the attempt's
  last step observation) is unchanged — which covers records written before that field existed,
  block 7's own among them, with no column and no migration.

### 3.6 Divergence

| Type | Fields |
|---|---|
| `TurnFingerprint` | `turn`, `actions: tuple[tuple[str, str], ...]` (action declaration id, canonical JSON of parameters, in step order), `metrics: dict[str, float]`, `city_count: int \| None`, `unit_count: int \| None` |
| `DivergenceDetail` | `dimension: str` (`actions`, `city_count`, `unit_count`, or a metric name), `value_a`, `value_b` (rendered strings) |
| `DivergenceReport` | `run_a`, `run_b`, `same_seed_set: bool`, `compared_turns: tuple[int, ...]`, `first_divergent_turn: int \| None`, `differed: tuple[DivergenceDetail, ...]`, `excluded: tuple[ExcludedRun, ...]` |

**Rule**: compare turn by turn over the turns both runs hold authoritatively, from the higher of the
two `first_owed_turn`s; the first turn with unequal fingerprints is the answer, with every unequal
dimension named. An excluded run (§3.5 rule) yields no comparison and is named.

### 3.7 Portability

| Type | Fields |
|---|---|
| `RunRecordSet` | `run: Run`, `configuration: RunConfiguration`, `turn_cycles: tuple[TurnCycleRecord, ...]` (every attempt, ordered by turn then attempt), `run_events`, `model_calls`, `save_points`, `captures: tuple[ScreenCapture, ...]`, `images: dict[str, bytes]` (sha256 → bytes, kept captures only) |
| `BundleManifest` | `bundle_format: int = 1`, `store_schema_version: str`, `run_id`, `exported_at`, `source_host`, `source_store_id`, `counts: dict[str, int]`, `files: dict[str, str]` (relative POSIX path → sha256), `images: dict[str, str]` (capture_id → sha256) |

**Validation (import refuses, naming the cause)**: run id already present; a file whose hash
disagrees with the manifest; a capture with `blob_ref` whose image is absent from the set; a step
whose `turn_cycle_id` the set does not carry; a `bundle_format` or schema major newer than the
importer's. Import of a branch whose parent is absent is **allowed** — the lineage fields are
carried verbatim and `store_info` can report dangling parents — because refusing would make a child
un-movable without its whole ancestry.

### 3.8 Store information

| Type | Fields |
|---|---|
| `StoreInfo` | `schema_version: StoreSchemaVersion`, `store_id`, `host_platform`, `read_only: bool`, `counts: dict[str, int]` (per record kind), `migrations: tuple[MigrationRecord, ...]`, `runs_with_absent_parent: tuple[RunId, ...]` |

## 4. State transitions

None new. Lifecycle (002 §4), archival (A1–A4) and retention statuses are unchanged; `import_run`
reproduces states verbatim and does not transition anything.

## 5. Platform neutrality (FR-028)

Stored references are already neutral: `blob_ref` is a content hash, `save_name` is a name, ids are
UUID strings. The bundle manifest uses relative POSIX paths only. The two platform-local paths this
feature knows — the store file and a migration backup — are operator-facing (`StoreInfo`,
`MigrationRecord.backup_path`) and never stored inside a record or a bundle.
