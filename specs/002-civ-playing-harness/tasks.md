---
description: "Task list for the Civilization-Playing Harness (feature 002)"
---

# Tasks: Civilization-Playing Harness

**Input**: Design documents from `/specs/002-civ-playing-harness/`

**Prerequisites**: [plan.md](./plan.md), [spec.md](./spec.md), [research.md](./research.md),
[data-model.md](./data-model.md), [contracts/](./contracts/)

**Tests**: Test tasks ARE included. The plan specifies a four-tier `pytest` strategy
(`tests/unit`, `tests/contract`, `tests/integration`, `tests/live`), the contracts name specific
conformance suites, and the spec makes several audits release-blocking (SC-006, SC-008, SC-009,
SC-018, SC-019). Tests are a requirement of this feature, not an option.

**Organization**: Tasks are grouped by user story so each story can be implemented, tested, and
demoed independently.

**Revision 3** — regenerated against plan.md revision 3, which corrected a factual error about
platform support: the tuner interface the harness speaks to ships in the native **Windows, macOS,
and Linux** builds, and only the Windows-only FireTuner *GUI* is Windows-bound — which the harness
never uses. Added a `HostPlatform` port and three adapters (T047–T055), per-platform capture spikes
and screening profiles, a support-tier gate, and platform as part of the seed set's pinned identity.
Task IDs were renumbered again; 205 tasks.

**Revision 2** — regenerated against plan.md revision 2, which re-planned the feature after the
spec's clarification session of 2026-09-19. Four decisions changed shape, and they touch most
phases: the turn became an **interactive decision-step loop** (FR-008), the **per-turn time budget
was removed** in favour of a no-progress step count (FR-014), save retention became
**explicit-archival-only** (FR-036), and the **Civilization VI build is pinned** per seed set
(FR-002, FR-031). Task IDs were renumbered; do not map them against the previous revision.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: Which user story the task belongs to (US1–US5)
- Every task names an exact file path

## Path Conventions

Single Python project, per plan.md Project Structure:

- `catalogs/` — the parity boundary as versioned YAML (not Python)
- `lua/gamecore/`, `lua/ingame/` — declared Lua as reviewable source
- `src/civsim_harness/` — the harness package, portable on every target platform
- `src/civsim_harness/host/` — **the only place an OS-specific import may appear** (research R19)
- `tests/{unit,contract,integration,live}/` — the four test tiers

Paths in task descriptions use POSIX separators. Where a task names a platform-specific location it
is resolved through the host port at runtime, never hard-coded.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Project initialization and basic structure

- [X] T001 Create the repository skeleton per plan.md Project Structure: `catalogs/observations/`, `catalogs/actions/`, `lua/gamecore/`, `lua/ingame/`, `src/civsim_harness/{config,nexus,capability,parity,observe,act,agent,provider,run,saves,resilience,store,operator,telemetry,models}/`, and `tests/{unit,contract,integration,live,fakes}/`, with an `__init__.py` in every Python package
- [X] T002 Initialize the `uv`-managed Python 3.12+ project in `pyproject.toml` with **portable** runtime dependencies only (`pydantic>=2`, `httpx`, `psutil`, `typer`, `fastapi`, `uvicorn`, `SQLAlchemy`, `Pillow`, `PyYAML`) and dev dependencies (`pytest`, `pytest-asyncio`, `syrupy`, `ruff`, `mypy`). Platform libraries go in T003's optional groups, never the base set — a macOS install must not pull `pywin32` (research R19)
- [X] T003 Declare per-platform optional dependency groups in `pyproject.toml` with environment markers: `windows` (`pywin32`, `winsdk`, `pydirectinput`), `macos` (`pyobjc` Quartz/ScreenCaptureKit bindings), `linux` (X11/XTest and portal-PipeWire bindings), so `uv sync` installs only the current host's extras (research R19)
- [X] T004 [P] Configure `ruff` and `mypy` (strict on `src/civsim_harness`) in `pyproject.toml`
- [X] T005 Configure `pytest` in `pyproject.toml`: `asyncio_mode = "auto"`, register the `live` marker, and set `addopts` so `tests/live` is excluded by default per plan.md Testing
- [X] T006 [P] Create `.gitignore` excluding the secrets file, `.env`, the local store database, capture blobs, and copied `.Civ6Save` files
- [X] T007 [P] Create `.github/workflows/ci.yml` running `uv run pytest tests/unit tests/contract tests/integration` as a **matrix across `windows-latest`, `macos-latest`, and `ubuntu-latest`** (the live tier stays excluded, per quickstart.md "CI-runnable subset"). The matrix is what catches an accidental OS-specific import in the portable core long before the live tier would (research R19)
- [X] T008 Add a lint rule in `pyproject.toml` (ruff `flake8-tidy-imports` banned-api or equivalent) forbidding imports of `pywin32`, `win32*`, `winsdk`, `pydirectinput`, `Quartz`, `AppKit`, `Xlib`, and `ctypes.windll` anywhere outside `src/civsim_harness/host/`, so "is the core platform-neutral?" is answerable from the import graph rather than by reading thirteen subpackages (plan Structure Decision, research R19)
- [X] T009 [P] Create `catalogs/VERSION` containing the single-line initial catalog version `2026.09.1` per contracts/capability-catalog.md
- [X] T010 [P] Create `configs/turn50-validation.yaml` as the example run configuration in the exact format of contracts/run-configuration.md (`schema_version: 1`, `stop_condition.type: turn_reached`, `turn: 50`, `no_progress_step_limit: 8`, `recovery_attempt_limit: 3`, `min_free_disk_gb: 25`). **No `turn_time_budget_s` field** — a turn has no time bound (FR-014)
- [X] T011 [P] Create `configs/seedsets/shuffle-classic-2026q3.yaml` as the example seed set in the format of contracts/run-configuration.md "Seed set definition", including `game_build` and an empty `accepted_build_changes` list (FR-031)
- [X] T012 Create the `civsim` console-script entry point in `pyproject.toml` wired to a Typer app stub in `src/civsim_harness/operator/cli.py`

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The parity boundary machinery, the record schemas, the game transport, and the store
port. Nothing can reach the game or the agent except through these, so no user story can begin
until they exist.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

**Telemetry and errors**

- [X] T013 Define the exception hierarchy in `src/civsim_harness/errors.py`: `HarnessError`, `PreflightError`, `CatalogError`, `NexusError`, `StoreWriteError`, `ParityViolation`, `ProviderChainExhausted`, `RecoveryLimitReached`, `ObservationAssemblyError`, `BuildMismatchError`, `DiskHeadroomError`
- [X] T014 Implement the credential redaction filter in `src/civsim_harness/telemetry/redaction.py`, applied to every record, log line, and exception payload — enforced as a filter, not a convention (FR-043, SC-018, invariant I8)
- [X] T015 Implement out-of-game structured logging in `src/civsim_harness/telemetry/logging.py` with the T014 redaction filter mandatory on every handler; harness telemetry is recorded here and never as game information (FR-020)
- [X] T016 [P] Unit test in `tests/unit/test_redaction.py`: key-shaped values planted in config, responses, and raised exceptions appear in no serialized record, log line, or traceback (SC-018)

**Record models**

- [X] T017 [P] Define shared value types in `src/civsim_harness/models/common.py`: id types, `ModRef` (id + version), `ModelRef` (provider + model), `Cost`, `CatalogVersionRef`, `BuildAcceptance` (`acceptance_id`, `from_build`, `to_build`, `accepted_by`, `accepted_at`, `reason`), timestamp aliases
- [X] T018 [P] Define `SeedSet`, `RunConfiguration`, `GuidanceSet`, `StopCondition`, and `ModelConfig` pydantic models in `src/civsim_harness/models/config.py` per data-model.md §1–3: `SeedSet.seeds` non-empty; `civilization`, `leader`, `ruleset`, `mod_set` immutable after creation (seeds may be appended); `SeedSet.game_build` required and `accepted_build_changes` defaulting to empty; `StopCondition` is exactly one of `turn_reached(n)` / `game_outcome` / `operator_stop`; `ModelConfig` carries `primary`, ordered `fallbacks` (possibly empty), `request_params`, and **no credentials**; `mod_set` empty list is meaningful, not absent. Together with T019 these models are the complete recorded definition FR-001 requires before a run may start
- [X] T019 Define `RunConfiguration`'s bound fields in `src/civsim_harness/models/config.py` per data-model.md §2: `no_progress_step_limit` (int, ≥ 1, **no upper bound**), `recovery_attempt_limit` (int), `min_free_disk_gb` (number). **A `turn_time_budget_s` field must not exist** — data-model.md §2 states its reappearance is a regression, not an addition (FR-014)
- [X] T020 [P] Define `Run` and its enums in `src/civsim_harness/models/run.py` per data-model.md §4: `lifecycle_state` ∈ {preparing, playing, waiting_on_model, waiting_on_game, paused, interrupted, resuming, finished, failed}; `record_completeness_status` ∈ {complete, has_gaps, unknown}; `comparability_status` ∈ {comparable, visually_degraded, not_comparable}; plus `observation_catalog_version`, `action_catalog_version`, `parent_run_id`, `parent_turn`, `client_identity`, `game_build`, `game_build_acceptance_ref` (nullable), `archived_at` (nullable), `capture_path`
- [X] T021 [P] Define `TurnCycle` in `src/civsim_harness/models/turn.py` per data-model.md §5: `turn_number ≥ 1`; `attempt_index` starts at 0; `save_point_id` **non-nullable**; `step_count` ≥ 1 and **unbounded above**; `outcome` ∈ {ended_by_agent, ended_on_no_progress, abandoned}; `final_no_progress_streak`; `visually_degraded` derived from its steps; `yields`; `persisted_at`. **No `observation_id`** — observations are step-scoped
- [X] T022 Define `DecisionStep` in `src/civsim_harness/models/turn.py` per data-model.md §6: `decision_step_id`, `turn_cycle_id`, `step_index` (1-based, contiguous), `observation_id` required, `decision_id` required, `model_call_id` required, `progress` ∈ {changed_state, no_change, rejected}, `no_progress_streak_after`, `visually_degraded`, `started_at`/`ended_at`. Exactly one decision and one model call per step (invariant I13)
- [X] T023 Define `Observation`, `ObservationEntry`, and `ScreenCapture` in `src/civsim_harness/models/turn.py` per data-model.md §7–8: `Observation.decision_step_id` (step-scoped, **not** turn-scoped); `ObservationEntry.declaration_id` required with `context` ∈ {GameCore_Tuner, InGame}; `ScreenCapture` binds `run_id`, `turn_number`, **and `decision_step_id`**, with `screening_status` ∈ {screened_clean, withheld}, `withheld_reason` ∈ {non_player_ui, geometry_mismatch, provenance_failure, capture_failed}, and `blob_ref` null when withheld
- [X] T024 [P] Define `Decision` and `ActionExecution` in `src/civsim_harness/models/decision.py` per data-model.md §9: `decision_step_id` (replacing any turn-level `order_index`); `action_declaration_id` and `model_call_id` both required; `trigger` ∈ {proactive, prompt_response} with `prompt_type` set when `prompt_response`; `is_end_turn` bool; `outcome` ∈ {applied, rejected, partially_applied}; `rejection_reason` ∈ {not_in_catalog, unavailable_to_human_now, illegal_in_context, out_of_parity_camera, verification_failed}
- [X] T025 [P] Define `ParityDeclaration`, `CatalogVersion`, and `IntegrationCapability` in `src/civsim_harness/models/catalog.py` per data-model.md §10–11: `parity_basis` required and non-empty; `kind` ∈ {observation, view, action}; `camera_requirements` (`mode` ∈ {world, strategic, city_screen, diplomacy, congress}, `zoom_range`, `target_must_be_revealed`) and `screening_profile` required when `kind == view` and absent otherwise; `path` ∈ {firetuner, bespoke}; `firetuner_gap` required and non-empty when `path == bespoke`
- [X] T026 [P] Define `ModelCall`, `SavePoint`, and `RunEvent` in `src/civsim_harness/models/records.py` per data-model.md §12–14: `ModelCall` carries `decision_step_id` and `outcome` ∈ {decision_returned, empty_response, failed, rate_limited, context_rejected} — **singular `decision_returned`**, since one call serves one step; `SavePoint.retention_status` ∈ {retained, eligible, removed} with `verified` and `missing` flags; `RunEvent` carries an optional `step_index` and an `event_type` covering the full 28-value enum in data-model.md §14 (including `turn_ended_on_no_progress`, `observation_assembly_failed`, `run_archived`, `game_build_change_accepted`, and `disk_headroom_low`, and **excluding** the removed `stall`)
- [X] T027 Implement JSON Schema export in `src/civsim_harness/models/export_schemas.py`, writing every record schema into `specs/002-civ-playing-harness/contracts/schemas/` with its schema version
- [X] T028 Contract test in `tests/contract/test_record_schemas.py`: every record model round-trips its published JSON Schema, and a schema change that removes or retypes a field fails the build (additive-only, per contracts/match-store-port.md "Schema evolution")

**Game transport**

- [X] T029 Implement the Nexus wire codec in `src/civsim_harness/nexus/codec.py`: `length` uint32 LE (payload size **including** the null terminator), `tag` int32 LE, NUL-terminated UTF-8 payload, `TAG_HANDSHAKE = 4`, `TAG_COMMAND = 3` (contracts/nexus-protocol.md)
- [X] T030 [P] Unit test in `tests/unit/test_nexus_codec.py`: frame/parse round-trips, payloads fragmented across packets, short and oversize frames
- [X] T031 Implement connection and handshake in `src/civsim_harness/nexus/client.py`: connect `127.0.0.1:4318`, send `APP:`, send `LSQ:`, resolve `GameCore_Tuner` and `InGame` state indices, and surface connection-refused as a *preparation* failure rather than a run failure
- [X] T032 Implement request/response discipline in `src/civsim_harness/nexus/client.py`: per-request nonce `---BEGIN:<nonce>---` / `---END:<nonce>---` wrapping, JSON document extraction between matched pairs, unmatched output routed to telemetry and never returned as a result, a **per-command timeout** (one of the per-operation bounds that satisfy SC-010 without a turn timer, research R12), and a lock serializing access to the single socket
- [X] T033 [P] Unit test in `tests/unit/test_nexus_sentinels.py`: stray game prints, a previous request's tail, and output with no matching nonce are all discarded to telemetry rather than parsed as results
- [X] T034 Implement the heartbeat probe in `src/civsim_harness/nexus/heartbeat.py`: periodic nonce round-trip through `GameCore_Tuner` with a bounded timeout

**The parity boundary machinery**

- [X] T035 Implement the catalog YAML loader in `src/civsim_harness/capability/loader.py` enforcing all seven load-time validations from contracts/capability-catalog.md — non-empty `parity_basis`; every `capability_id` resolves and every `path: bespoke` has a non-empty `firetuner_gap`; `declaration_id` unique across all files; actions have both `availability_predicate` and `verification_predicate`; observations and views have a valid `output_schema`; predicates reference only exposed symbols; `catalogs/VERSION` present. **All failures abort startup, not turn 1.**
- [X] T036 Implement catalog versioning in `src/civsim_harness/capability/version.py` (content hash over all catalog files, `declaration_ids` set) and the registry with wrong-context execution refusal in `src/civsim_harness/capability/registry.py` (FR-022, research R3)
- [X] T037 [P] Unit test in `tests/unit/test_catalog_load.py`: a missing `parity_basis` fails load; `path: bespoke` with an empty `firetuner_gap` fails load; duplicate `declaration_id` fails load; an action missing either predicate fails load; an observation missing `output_schema` fails load; executing an entry in the wrong Lua context is refused

**Store and provider ports**

- [X] T038 Define the `MatchStore` Protocol in `src/civsim_harness/store/port.py` exactly as specified in contracts/match-store-port.md — nine writes (including `archive_run`), eight reads (including `step_gaps` and `list_eligible_save_points`), and `ping()`
- [X] T039 Implement the SQLite + content-addressed blob reference adapter in `src/civsim_harness/store/sqlite_adapter.py` satisfying D1–D6: durable-before-return, raise on any failure, atomic whole-turn `write_turn_cycle` carrying **every decision step in order**, idempotency on `(run_id, turn_number, attempt_index)`, captures and save-point references through the same port, and `ping()`. Nothing a record depends on may exist only in local or ephemeral form — the adapter is a storage implementation of the port, not a second bypassing path (FR-051, D5). A turn of several hundred steps must round-trip without truncation
- [X] T040 Implement archival and eligibility in `src/civsim_harness/store/sqlite_adapter.py` per contracts/match-store-port.md A1–A4: `archive_run` sets `Run.archived_at`, writes a `run_archived` event, and transitions that run's save points to `eligible`; it is rejected on a non-terminal run; **nothing else may set `archived_at` or produce an `eligible` save point** — no TTL, no age rule, no quota, no retention window, no thinning (FR-036)
- [X] T041 Port conformance suite in `tests/contract/test_match_store_port.py` asserting D1–D6, A1–A4, idempotency under repeated writes, parent-immutability rejection, `turn_gaps` **and `step_gaps`**, step-order preservation on read-back, authoritative-attempt selection, and that a failed write surfaces as a raise rather than a falsy return
- [X] T042 Adversarial store tests in `tests/contract/test_match_store_port.py`: a turn of several hundred steps round-trips with its order intact and no truncation, and a finished-but-unarchived run's save points never appear in `list_eligible_save_points()` no matter how old the run is (invariant I17)
- [X] T043 Define the `ModelProvider` Protocol plus `ModelCapabilities`, `DecisionRequest` (carrying `step_index`), and `DecisionResponse` (carrying a **singular** `decision`, not a list) in `src/civsim_harness/provider/port.py` exactly as specified in contracts/model-provider-port.md

**Lifecycle and guards**

- [X] T044 Implement the run lifecycle state machine in `src/civsim_harness/run/lifecycle.py` per data-model.md §4, emitting a `lifecycle_transition` `RunEvent` on every transition (FR-003)
- [X] T045 [P] Unit test in `tests/unit/test_lifecycle.py`: legal and illegal transitions, an event recorded for every transition, `finished` requiring exactly one `stop_resolution`, and `archived_at` being orthogonal to `lifecycle_state` — an archived run keeps its records and stays readable (data-model.md §4)
- [X] T046 Implement the write-before-advance guard in `src/civsim_harness/store/guard.py`: the end-turn action is reachable only downstream of an acknowledged durable write, and a failed write raises and halts the run (FR-013, invariant I3)

**The host platform port — the only place OS-specific code may live**

- [X] T047 Define the `HostPlatform` Protocol in `src/civsim_harness/host/port.py` covering exactly the six capabilities the harness needs from an OS (research R19): locate the game process, identify its window, capture that window, resolve game directories (saves + `AppOptions.txt`), optional synthetic input, and free disk space. Nothing else belongs here, and nothing outside `host/` may import a platform library
- [X] T048 Implement platform and session detection in `src/civsim_harness/host/detect.py`: resolve OS, and on Linux distinguish **X11 from Wayland**, since they have different capture paths and Wayland blocks synthetic input entirely (research R19)
- [X] T049 Implement support-tier resolution in `src/civsim_harness/host/detect.py`: probe the adapter's capabilities and assign `VALIDATED` (full capability, capture-hygiene spike passed), `SUPPORTED` (verified quicksave path but no passing capture spike — runs visually degraded), or `UNSUPPORTED` (no verified quicksave path). **A platform is `UNSUPPORTED` until probed** — the safe default (research R19)
- [ ] T050 [P] Implement the Windows host adapter in `src/civsim_harness/host/windows/`: `pywin32` window identity, Windows.Graphics.Capture, SendInput, and `%USERPROFILE%\Documents\My Games\Sid Meier's Civilization VI\` directory resolution
- [ ] T051 [P] Implement the macOS host adapter in `src/civsim_harness/host/macos/`: Quartz window list, ScreenCaptureKit with an `SCContentFilter` scoped to the single game window, `CGEvent` input, and `~/Library/Application Support/Sid Meier's Civilization VI/` directory resolution. Detect and report missing **Screen Recording** and **Accessibility** grants as an actionable preflight failure rather than an opaque capture error
- [ ] T052 [P] Implement the Linux host adapter in `src/civsim_harness/host/linux/`: XComposite redirected-pixmap capture and `XTest` input on X11, `xdg-desktop-portal` ScreenCast on Wayland, and `~/.local/share/aspyr-media/Sid Meier's Civilization VI/` directory resolution. On Wayland, report synthetic input as unavailable rather than attempting it — the compositor blocks it by design (research R5, R19)
- [X] T053 `HostPlatform` port conformance suite in `tests/contract/test_host_platform_port.py`, running the **same** assertions against every adapter so "this platform is supported" is a test result rather than a claim: window identity resolves, capture returns a frame matching the client rect, directories resolve and exist, disk space reads, and an unavailable capability reports unavailable rather than raising an opaque error
- [X] T054 [P] Unit test in `tests/unit/test_platform_neutrality.py`: import every module under `src/civsim_harness/` except `host/` and assert none pulls a platform library, enforcing T008's rule at test time as well as lint time (research R19)

**Test doubles**

- [X] T055 [P] Implement the fake host adapter in `tests/fakes/fake_host.py` with scriptable capability availability and tier, so the turn cycle, preflight tier gate, and degradation paths run deterministically on any CI platform
- [X] T056 [P] Implement the recorded-transcript fake Nexus server in `tests/fakes/fake_nexus.py`, replaying captured request/response transcripts so the full turn cycle runs deterministically without Civ VI, with scriptable mid-turn drops and per-operation stalls (research R15)
- [X] T057 [P] Implement the fake `ModelProvider` in `tests/fakes/fake_provider.py` with scriptable outcomes: a single decision, empty response, rate limit, context rejection, failure, and the contract-violating multi-decision response T180 asserts against

**Checkpoint**: Boundary machinery, records, transport, and store exist — user story work can begin

---

## Phase 3: User Story 1 - Play an unattended run from a fixed seed to a stop condition (Priority: P1) 🎯 MVP

**Goal**: A configured run brings Civ VI to exactly the specified starting position, then plays turn
after turn on its own. Each turn is a loop of decision steps — observe, decide, execute, verify,
observe again — ending when the agent issues its declared end-turn decision or the no-progress
backstop trips, with a complete step-by-step record and no human interaction with the game client.

**Independent Test**: Configure a run on a known seed with a turn-50 stop condition, start it, and do
not touch the keyboard or the game client until it stops. Verify it reached turn 50, that every turn
from 1 to 50 has a persisted record containing every decision step in order — the state observed at
that step, the decision made, the reasoning given — plus the resulting yields, and that no turn and
no step is missing. Then repeat with the stop condition set to a game outcome and verify it plays
across era transitions to victory or defeat.

### Tests for User Story 1 ⚠️

> Write these first and confirm they fail before implementing

- [ ] T058 [P] [US1] Integration test in `tests/integration/test_turn_cycle.py` against the fake Nexus and fake provider: the full cycle — quicksave → **multi-step loop** → persist → end turn — runs to a `turn_reached` stop with 50/50 authoritative turns, zero turn gaps, and zero step gaps (SC-001, SC-003)
- [ ] T059 [P] [US1] Integration test in `tests/integration/test_decision_loop.py`: for each step *n*, the observation is assembled **after** step *n−1*'s effect was verified; each step has a distinct capture and exactly one model call; and no observation or capture is reused across steps (FR-008, FR-015, invariants I13, I14). Include the **self-cancellation** edge case: an agent that undoes its own earlier work later in the same turn, having seen the result, produces two steps both recorded as issued and both `changed_state`, and the turn's recorded yields reflect the **resulting** state rather than the intent (spec Edge Cases, data-model §6)
- [ ] T060 [P] [US1] Integration test in `tests/integration/test_turn_endings.py`: a turn ends `ended_by_agent` when the agent issues `turn.end_turn`; a turn ends `ended_on_no_progress` when `no_progress_step_limit` consecutive rejected/no-change steps occur; a productive step mid-sequence **resets** the counter; the two endings are distinguishable in the record; and a one-step turn where the agent ends immediately is valid, not a failure (FR-008, FR-014, SC-022)
- [ ] T061 [P] [US1] Unit test in `tests/unit/test_no_truncation.py`: a scripted 500-step productive turn completes untouched — **no wall-clock bound, no step cap, and no cost ceiling ends it** (FR-014, invariant I16). This is a negative test; every plausible-sounding guard rail it would catch is a violation
- [ ] T062 [P] [US1] Unit test in `tests/unit/test_turn_state_machine.py`: a failed quicksave halts the run and the turn never comes into existence as an attempt; a failed persist halts the run before end-turn; a mid-turn observation-assembly failure at step *n* > 1 **abandons and replays** the attempt from its start quicksave rather than finishing from the last good view (invariants I2, I3, data-model.md §5 failure table)
- [ ] T063 [P] [US1] Contract test in `tests/contract/test_run_configuration.py` asserting V1, V3, V7, V8, V9, V12, and V13 from contracts/run-configuration.md — a partially specified run does not start, a `seed_set` mismatch is an error rather than an override, an unreachable store is rejected, a second run on the same client or identity is rejected, a key-shaped value in configuration is a validation error, `no_progress_step_limit` ≥ 1 with no silent default cap, and an `UNSUPPORTED` host platform is rejected before turn 1 with the missing capability recorded, while a merely degraded one starts and is marked instead (FR-054, V13, exercised through the fake host of T055)
- [ ] T064 [P] [US1] Contract test in `tests/contract/test_build_pin.py` asserting V10: a client build differing from the seed set's `game_build` fails the run before turn 1 with the mismatch recorded, and a matching `BuildAcceptance` permits it while recording `game_build_acceptance_ref` on the run (FR-002, FR-031)
- [ ] T065 [US1] Contract test in `tests/contract/test_build_pin.py` for the platform half of the composite pin: a **platform** difference fails preflight exactly as a version difference does, and an acceptance of `win/1.0.12.9 → win/1.0.12.11` does **not** also permit `win → mac`. A set carrying any accepted platform change must report as non-uniform with its runs partitioned by platform (FR-002, FR-031, research R20)
- [ ] T066 [P] [US1] Integration test in `tests/integration/test_preparation.py`: when the fake game reports a setting differing from the configuration, the run fails before turn 1 with a `preparation_mismatch` event recorded and no turn 1 attempted (FR-002, V2, US1 §2)
- [ ] T067 [P] [US1] Integration test in `tests/integration/test_prompts.py`: each declared prompt type is answered as a `prompt_response` decision at its own decision step, and an unrecognised screen stalls the run visibly rather than being dismissed, defaulted, or absorbed (FR-010, FR-049, SC-005)
- [ ] T068 [P] [US1] Integration test in `tests/integration/test_stop_conditions.py`: exactly one stop condition is recorded, and a stop condition coinciding with a victory, defeat, or crash on the same turn records one and leaves the others as events (FR-005, invariant I10)

### Implementation for User Story 1

**Configuration and preparation**

- [X] T069 [US1] Implement the run configuration loader in `src/civsim_harness/config/run_config.py`: parse the contracts/run-configuration.md YAML, check `schema_version`, and apply V1 (every required field present and non-null), V9 (no credential-shaped value present), and V12 (`no_progress_step_limit` ≥ 1)
- [X] T070 [US1] Implement seed set loading in `src/civsim_harness/config/seed_set.py` with the V3 agreement check — when `seed_set` is set, `civilization`, `leader`, `ruleset`, and `mod_set` must match the set — and loading of `game_build` and `accepted_build_changes` (FR-031)
- [X] T071 [US1] Implement the game build read in `src/civsim_harness/observe/game_build.py` following the research R18 spike: a declared `GameCore_Tuner` version observation where reachable, otherwise the client binary's version via the `HostPlatform` port. The value is a **composite of platform and version** (e.g. `mac/1.0.12.9`), since the Aspyr ports are separately built binaries whose numbering need not track the Windows build's. Either way it is **out-of-game provenance** and never enters the agent's context (FR-020, R18, R20)
- [X] T072 [US1] Implement the build-pin preflight in `src/civsim_harness/run/preparation.py` (V10): compare the client's build against the seed set's `game_build`; a mismatch fails the run before turn 1 with the mismatch recorded, unless a `BuildAcceptance` covers that exact `from_build → to_build` transition, in which case `Run.game_build_acceptance_ref` is recorded. A run whose build differs from its seed set's without an acceptance reference must not be constructible (FR-002, invariant I18, R18)
- [X] T073 [US1] Implement guidance loading in `src/civsim_harness/config/guidance.py`: content-addressed hash recorded with the run, plus the runtime assertion that content is identical across runs referencing the same hash — guidance that varies per run is a contract violation (FR-021, V6)
- [X] T074 [US1] Implement secrets resolution in `src/civsim_harness/config/secrets.py`: keys resolve from the environment or a secrets file at call time only, never from run configuration (FR-043)
- [X] T075 [US1] Implement the run-identity lock in `src/civsim_harness/run/identity_lock.py` keyed to the client PID and run ID, refusing a second attach to an active client or run identity (FR-006, V8, research R4)
- [X] T076 [US1] Implement preparation in `src/civsim_harness/run/preparation.py`: apply the configured seed, civilization, ruleset, mod set, map and game settings, difficulty, and opponents; read the actual setup back through declared observations and compare **field by field**; any difference creates the run in `failed` state with the mismatch recorded and no turn 1 (FR-002, V2)

**Saves — required before any turn may proceed**

- [X] T077 [US1] Run the research R5 spike: enumerate callable save paths (`Network.*`, `UI.*`, `Game.*`) in the `InGame` and `GameCore_Tuner` contexts through the tuner, and record the enumeration output as gap evidence in `specs/002-civ-playing-harness/spikes/r5-save-path.md`. **This must run before save work begins**, to preserve Principle II's Firetuner-first ordering
- [X] T078 [US1] Implement the save capability in `src/civsim_harness/saves/save_game.py` following the R5 outcome — the FireTuner Lua path if one exists, otherwise the bespoke Save Game dialog driver in `src/civsim_harness/saves/dialog_driver.py` with the R5 enumeration as its `firetuner_gap` statement (FR-007, FR-028, plan C1)
- [X] T079 [US1] Implement filesystem save verification in `src/civsim_harness/saves/verify.py`: the `.Civ6Save` must appear in the save directory **resolved through the `HostPlatform` port** — never a hard-coded path — with its size stable across two reads before the turn proceeds; an unverifiable quicksave fails the turn rather than being recorded as taken (FR-007, SC-004, research R5, R19)
- [X] T080 [US1] Implement save point naming and record writing in `src/civsim_harness/saves/save_point.py` using `civsim__<run_id>__t<turn:04d>`, writing a `SavePoint` record through the store with `verified` set from T079 (FR-032)
- [X] T081 [US1] Implement the disk headroom check in `src/civsim_harness/saves/headroom.py`: estimate the run's save and capture footprint at preflight (V11) and check free space **before every quicksave**; below `min_free_disk_gb` the run halts in a recorded state with a `disk_headroom_low` event. **Nothing is deleted to make room** — no save is eligible until its run is archived (spec edge case, research R17)

**Declared Lua and catalog content — the game surface**

- [X] T082 [P] [US1] Write `GameCore_Tuner` read Lua for map, plots, units, and cities in `lua/gamecore/map.lua`, `lua/gamecore/units.lua`, and `lua/gamecore/cities.lua`, each emitting a single JSON document between the sentinels (contracts/nexus-protocol.md "Structured payloads")
- [X] T083 [P] [US1] Write `GameCore_Tuner` read Lua for research, civics, government, religion, diplomacy, congress, espionage, and great people in `lua/gamecore/research.lua`, `lua/gamecore/government.lua`, `lua/gamecore/religion.lua`, `lua/gamecore/diplomacy.lua`, `lua/gamecore/congress.lua`, `lua/gamecore/espionage.lua`, and `lua/gamecore/great_people.lua`
- [X] T084 [P] [US1] Write `InGame` Lua for unit movement, orders, and promotions in `lua/ingame/unit_orders.lua` (FR-009)
- [X] T085 [P] [US1] Write `InGame` Lua for city production, purchasing, and management in `lua/ingame/city_orders.lua` (FR-009)
- [X] T086 [P] [US1] Write `InGame` Lua for research, civics, policies, government, and governors in `lua/ingame/empire_orders.lua` (FR-009)
- [X] T087 [P] [US1] Write `InGame` Lua for religion (pantheon through founding and religious units), diplomacy/war/peace/city-states, espionage, great people, and World Congress in `lua/ingame/religion.lua`, `lua/ingame/diplomacy.lua`, `lua/ingame/espionage.lua`, `lua/ingame/great_people.lua`, and `lua/ingame/congress.lua` (FR-009)
- [X] T088 [P] [US1] Write `InGame` Lua for the screen-identity probe and prompt detection/response in `lua/ingame/screens.lua` (research R13)
- [X] T089 [P] [US1] Write `InGame` Lua for ending the turn and reading back the turn number in `lua/ingame/turn_control.lua`, supporting the `turn.end_turn` verification predicate (FR-008)
- [X] T090 [P] [US1] Author observation declarations in `catalogs/observations/{map,cities,units,research,diplomacy,religion,government,congress,espionage}.yaml`, each with a non-empty `parity_basis` naming the in-client action, a `context`, a resolving `capability_id`, a valid `output_schema`, and `introduced_in_version: "2026.09.1"` (FR-016)
- [X] T091 [P] [US1] Author action declarations in `catalogs/actions/{units,cities,research,policies,religion,diplomacy,congress,great_people}.yaml`, each with a non-empty `parity_basis`, an `availability_predicate` (when a human could do this right now) and a `verification_predicate` (how the effect is confirmed) (FR-017, FR-009)
- [X] T092 [P] [US1] Author the `turn.end_turn` declaration in `catalogs/actions/turn.yaml` exactly as specified in contracts/capability-catalog.md: `parity_basis` "Click the end-turn button in the lower-right action panel, or press its hotkey"; `availability_predicate` refusing an end-turn while a blocking prompt is up (so FR-010 requires the prompt be answered first); and a `verification_predicate` distinguishing a turn that actually ended from a click the game swallowed (FR-008)
- [X] T093 [P] [US1] Author prompt-response action declarations in `catalogs/actions/prompts.yaml` covering unit promotion, pantheon and religion selection, great person selection, AI diplomatic approach, declaration of war, city-state quest, World Congress vote, and era transition (FR-010, research R13)
- [X] T094 [P] [US1] Author the `IntegrationCapability` declarations in `catalogs/capabilities.yaml`, binding every declaration to its `lua/` file or module path with `path: firetuner | bespoke`, `reads`, `writes`, and a non-empty `firetuner_gap` for every bespoke entry (FR-027, FR-028, SC-020)

**Observe — per decision step**

- [X] T095 [US1] Implement structured state assembly in `src/civsim_harness/observe/assemble.py`: consume capability results only, validate each produced value against its declaration's `output_schema`, and attach the producing `declaration_id` to every `ObservationEntry`. **Assembly runs once per decision step**, with no incremental-update or caching path that would skip the filter or return a stale board (FR-012, FR-018, FR-024, invariant I14)
- [X] T096 [US1] Implement assembly-failure handling in `src/civsim_harness/observe/assemble.py`: a failure at step *n* > 1, after earlier steps have executed and verified, raises `ObservationAssemblyError`, records an `observation_assembly_failed` event, and abandons the attempt for replay — **the turn may not be finished from the last good view** (spec edge case, FR-046)
- [X] T097 [US1] Implement the screen-identity probe in `src/civsim_harness/observe/screen_identity.py`, probed between decision steps and after every interruption, with unrecognised screens surfaced as a recordable state rather than an exception (research R13)
- [X] T098 [US1] Implement capture-path selection in `src/civsim_harness/observe/capture_paths.py`: ask the `HostPlatform` adapter for its ranked paths and select the highest available, recording the chosen path on `Run.capture_path`. Each platform carries its own ranking per research R6 — Windows (WGC → DXGI+crop → `PrintWindow` → none), macOS (ScreenCaptureKit → `CGWindowListCreateImage` → none), Linux (XComposite on X11, portal/PipeWire on Wayland → none). The selector itself contains no OS-specific code
- [ ] T099 [US1] Run the research R6 capture-hygiene spike **once per target platform** and record each result in `specs/002-civ-playing-harness/spikes/r6-capture-hygiene-<platform>.md`: confirm that a console and unrelated windows placed over the client are absent from the captured frame, and that platform-specific chrome is excluded — the WGC capture border on Windows (`IsBorderRequired = false`), the menu bar and Dock on macOS, panels and notification toasts on Linux. **A pass on one platform carries no implication for another.** Until a platform's spike passes, no run on it may show images to the agent — those runs proceed visually degraded under FR-050 (research R6, R19)
- [X] T100 [P] [US1] Author per-platform non-player-chrome profiles as **catalog data** in `catalogs/screening_profiles.yaml` — one per platform, enumerating what the content gate must reject: a Windows taskbar, a macOS menu bar and Dock, a Linux panel or notification toast, plus FireTuner chrome, developer consoles, and debug overlays on every platform. These are declarations, not code: T129 implements the gate that reads them, so the data precedes the code rather than the reverse. A platform with no profile resolves to the strictest available, never a permissive default (FR-025, FR-030, SC-009, SC-019, contracts/capability-catalog.md `screening_profile`)
- [X] T101 [US1] Implement the host-capability preflight gate in `src/civsim_harness/run/preparation.py` satisfying **FR-054** and **V13**: resolve the host tier via T049 and refuse an `UNSUPPORTED` run before turn 1, **recording which capability was missing**. FR-054 grounds this in FR-007 — a host with no verified quicksave path cannot complete a single turn — rather than in FR-002, which governs configured elements rather than host capability. A merely *degraded* capability must **not** refuse the run: a `SUPPORTED` host starts and marks its runs visually degraded under FR-050, which is the distinction FR-054's last sentence draws. The tier is recorded on the run so a degraded platform's data is never mistaken for a validated platform's (FR-054, V13, FR-007, FR-050, research R19)
- [X] T102 [US1] Implement the capture pipeline in `src/civsim_harness/observe/capture.py`: **one capture per decision step**, bound to `(run_id, turn_number, decision_step_id)` with its `camera_state` and `captured_at`. An earlier step's capture may **never** stand in for a later one — the board the agent is shown must reflect what it has already done this turn (FR-015). **No frame may be persisted before the screening gates of T129 exist.** FR-030 and SC-019 govern *stored* captures, not merely shown ones, so until US2 lands this pipeline records each step's capture as explicitly unavailable — which US1 §9 permits in as many words — and marks the step visually degraded under FR-050. US1 is a complete and honest increment without stored images; it would not be a complete one with unscreened stored images

**Agent and provider**

- [X] T103 [US1] Implement agent context assembly in `src/civsim_harness/agent/context.py`: system role plus out-of-game guidance plus this step's parity-filtered structured state. Images are attached only once screening exists (US2); harness telemetry is never included (FR-020, FR-021, FR-024)
- [X] T104 [US1] Implement the decision response schema and parsing in `src/civsim_harness/agent/decisions.py`: **exactly one** decision carrying its stated reasoning, with malformed, multi-decision, or decision-free output treated as a failed call rather than a decision to do nothing (FR-008, FR-012, P4, P10)
- [X] T105 [US1] Implement the OpenRouter adapter's `complete()` in `src/civsim_harness/provider/openrouter.py` over `httpx` using the OpenAI-compatible chat completions shape with images as `image_url` content parts — **no vendor SDK on the decision path** — mapping a successful-but-empty response to `CallOutcome.empty_response`, and coupling no timeout to the turn (only the request timeout and its retry ladder) (FR-037, P4, P11)

**Act**

- [X] T106 [US1] Implement the restricted predicate evaluator in `src/civsim_harness/act/predicates.py` exposing a fixed symbol table with no arbitrary evaluation (contracts/capability-catalog.md load-time rule 6)
- [X] T107 [US1] Implement action dispatch in `src/civsim_harness/act/dispatch.py`: resolve the step's single requested action to a catalog declaration, evaluate its `availability_predicate`, and reject an unregistered action as `not_in_catalog` and an unavailable one as `unavailable_to_human_now`, with the reason recorded and the action never performed. A rejection also increments the turn's no-progress counter (FR-017, FR-014)
- [X] T108 [US1] Implement execution verification in `src/civsim_harness/act/verify.py`: read state back through `GameCore_Tuner`, evaluate the declaration's `verification_predicate`, and **derive** `applied | rejected | partially_applied` from the result — never assert it from the executor. The same verification result determines the step's `progress` ∈ {changed_state, no_change, rejected}, so a weak predicate both mis-records the action and disables the backstop (FR-011, FR-014, invariant I4, research R14)
- [X] T109 [US1] Implement prompt and interrupt handling in `src/civsim_harness/act/prompts.py`: game-initiated prompts and between-turn interrupts route through `catalogs/actions/prompts.yaml` as `prompt_response` decisions at their own decision steps; an unrecognised screen records an `unknown_screen` event and stalls the run visibly rather than clicking or dismissing (FR-010, FR-049, SC-005)

**Run loop**

- [ ] T110 [US1] Implement the decision-step loop in `src/civsim_harness/run/decision_loop.py` per research R14: for step *n* = 1, 2, 3 … unbounded — assemble a fresh observation and capture, obtain one decision and its reasoning, execute, verify, then assemble a new observation reflecting that effect before asking for the next. The agent is **never** asked to commit to a later decision before seeing the earlier one's result (FR-008, invariants I13, I14)
- [ ] T111 [US1] Implement no-progress accounting in `src/civsim_harness/run/no_progress.py` per data-model.md §5: increment the counter on a step whose decision was rejected or whose verification showed no game-state change, **reset it to zero** on any step verified as having changed state, and end the turn once it reaches `no_progress_step_limit`. Record `final_no_progress_streak` and a `turn_ended_on_no_progress` event (FR-014, SC-022)
- [ ] T112 [US1] Implement loop termination in `src/civsim_harness/run/decision_loop.py`: the loop exits **only** on the agent's `turn.end_turn` decision (`outcome = ended_by_agent`) or the T111 backstop (`outcome = ended_on_no_progress`). There is no timed exit, no step cap, and no cost ceiling; a turn must not be ended for any other harness-initiated reason nor truncated because it has grown long or expensive (FR-008, FR-014, invariant I16)
- [ ] T113 [US1] Ensure the backstop synthesises no decision in `src/civsim_harness/run/no_progress.py`: a turn ended on no progress records the outcome on the `TurnCycle` and **does not** write an `end_turn` decision to represent it — fabricating one would be a defaulted move recorded as an agent decision and would destroy the distinction SC-022 requires (FR-042, SC-012, SC-022)
- [ ] T114 [US1] Implement the turn cycle orchestrator in `src/civsim_harness/run/turn_cycle.py` — quicksave → T110 loop → persist → end turn — as the **only** component permitted to issue the end-turn action, sequenced strictly after an acknowledged store commit, halting the run on quicksave or persistence failure (FR-007, FR-008, FR-013, invariants I2, I3)
- [ ] T115 [US1] Implement stop resolution evaluation in `src/civsim_harness/run/stop.py`: resolve the run's configured `StopCondition.type` (`turn_reached` | `game_outcome` | `operator_stop`) to exactly one recorded `Run.stop_resolution` — `turn_reached`, `victory`, `defeat`, `operator_stop`, or `unrecoverable_failure` — with coincident conditions written as events (FR-005, invariant I10)
- [ ] T116 [US1] Implement the run orchestrator in `src/civsim_harness/run/runner.py` driving preparing → playing across every era to the stop condition, recording each lifecycle transition, and handling the game auto-advancing a turn by recording what actually happened rather than what was planned (FR-003, FR-009, SC-002)
- [ ] T117 [US1] Wire per-turn record persistence in `src/civsim_harness/run/turn_cycle.py`: the whole `TurnCycleRecord` — **every decision step in order**, each with its observation, entries, capture references, single decision, reasoning, execution outcome, and model call, plus the turn's resulting yields — written as one atomic unit before the turn ends. The record is never summarised, sampled, or streamed incrementally, however many steps the turn contained (FR-012, FR-013, D3)

**Operator surface**

- [ ] T118 [US1] Implement the Typer CLI in `src/civsim_harness/operator/cli.py`: `run start`, `run pause`, `run resume`, `run stop`, `run status`, and `doctor` (FR-004, contracts/operator-surface.md)
- [ ] T119 [US1] Implement the loopback HTTP endpoint in `src/civsim_harness/operator/api.py` bound to `127.0.0.1` only, with a **closed** status response schema carrying `run_id`, `lifecycle_state`, `requested_state`, `current_turn`, `current_step`, `last_known_good_save`, `last_error`, `connection_health`, `record_completeness_status`, `comparability_status`, `archived`, and `disk_headroom_gb` — and no turn records, observations, decisions, reasoning, yields, metric series, or captures (FR-053, Principle VI)
- [ ] T120 [US1] Implement command handling in `src/civsim_harness/operator/commands.py`: every command recorded as a `lifecycle_command_received` event **before** it takes effect, with pause and stop landing on a turn boundary rather than mid-turn. A pause requested during a long turn waits for that turn to end and is surfaced through `requested_state`; the harness must **not** cut the turn short to honour it faster (FR-004, FR-008, SC-022)
- [ ] T121 [US1] Implement `doctor` in `src/civsim_harness/operator/doctor.py` reporting tuner connection with resolved state indices, client liveness **with its build**, store reachability, catalog version with declaration and undeclared counts, capture path with hygiene-spike status, disk headroom, and provider key *presence* only (never its value) (quickstart.md Setup, FR-043)

**Checkpoint**: A run plays itself from turn 1 to its stop condition with a complete step-by-step record — the MVP

---

## Phase 4: User Story 2 - Prove the agent played within human parity (Priority: P2)

**Goal**: Every observation and action resolves to a versioned parity declaration, images enter the
agent's context only after screening, and an auditor can verify the whole boundary from the run's
record alone.

**Independent Test**: Take one completed run and, working only from its record, enumerate every
distinct observation the agent received — structured and visual — and every distinct action it
issued; verify each resolves to a declared parity basis, that every image it saw was screened, and
that no observation or action in the run lacks a declaration.

### Tests for User Story 2 ⚠️

- [X] T122 [P] [US2] Parity red-team suite in `tests/contract/test_parity_redteam.py`: a fixture list of forbidden values — unrevealed map contents, opponent internal state, hidden AI intent, undisclosed opponent research or civics, unit/city data beyond what the standard UI reveals, RNG state, debug and provenance data — asserted absent from contexts assembled from realistic transcripts, **at every decision step, not once per turn**. Any finding fails the build (FR-019, SC-006, SC-008, research R15)
- [ ] T123 [P] [US2] Contract test in `tests/contract/test_catalog_coverage.py`: preflight aborts when any reachable capability lacks a declaration, and the run record carries the catalog version and content hash in force (FR-022, FR-023, SC-007)
- [X] T124 [P] [US2] Unit test in `tests/unit/test_image_screening.py`: each of the four gates (source, geometry, provenance, content) independently withholds, and a withheld image is never stored and never shown (invariant I6, SC-019)
- [ ] T125 [P] [US2] Unit test in `tests/unit/test_camera_validation.py`: an unrevealed target plot, an out-of-range zoom, and a non-human view mode are each rejected with `out_of_parity_camera` and recorded (FR-026, SC-009)
- [X] T126 [P] [US2] Unit test in `tests/unit/test_telemetry_exclusion.py`: model identity, cost, latency, retries, save lineage, run configuration, wall-clock timing, and the game build are absent from every assembled agent context (FR-020, SC-008)

### Implementation for User Story 2

- [X] T127 [US2] Implement the structural parity filter in `src/civsim_harness/parity/filter.py`: the assembler's only input type is a capability result, with **no raw-Lua input path** — a developer wanting new data must add a declaration, because there is no shortcut that also works (FR-018, invariant I1, research R9)
- [X] T128 [US2] Implement the forbidden-field guard in `src/civsim_harness/parity/forbidden.py`: the red-team list asserted at runtime against every assembled context before it leaves the harness — once per decision step — covering both game-state leakage (FR-019) and harness telemetry presented as game information (FR-020)
- [X] T129 [US2] Implement the four screening gates in `src/civsim_harness/parity/screening.py` per research R7 — **source** (the frame came from the declared game-window capture item, never a desktop or region grab), **geometry** (dimensions match the client rect within tolerance), **provenance** (tagged with the declared view capability and camera state that produced it), and **content** (a detector driven by T100's per-platform profile, covering FireTuner chrome, developer console, debug overlay, harness UI, the platform's own chrome, and the WGC capture border) — with any gate failing meaning withhold and re-capture, applied per step. **This task is what unblocks blob persistence in T102**: until it lands, no frame is stored at all (FR-025, FR-030, FR-015, SC-019)
- [X] T130 [P] [US2] Author view declarations in `catalogs/observations/views.yaml` with `camera_requirements` (`mode` ∈ {world, strategic, city_screen, diplomacy, congress}, `zoom_range` within what the standard UI allows, `target_must_be_revealed: true`) and a `screening_profile` (FR-024, contracts/capability-catalog.md)
- [X] T131 [P] [US2] Author camera action declarations in `catalogs/actions/camera.yaml` — move, zoom, and view-mode toggle — each with its `parity_basis`, `availability_predicate`, and `verification_predicate` so camera changes are rejectable like any other action (FR-026, research R8)
- [ ] T132 [US2] Implement camera validation and execution in `src/civsim_harness/act/camera.py`: the target must be a plot the run has revealed, zoom must be within the range the standard UI permits, and the view mode must be one a human can toggle; violations are rejected with `out_of_parity_camera` and recorded (FR-026)
- [ ] T133 [US2] Implement withheld-capture recording and enable blob persistence in `src/civsim_harness/observe/capture.py`: a screened-clean frame may now be stored; `screening_status = withheld` with its `withheld_reason` is written with `blob = None` and an `image_withheld` event carrying the `step_index` — the record is the evidence screening worked and is never deleted. This is the task that lifts T102's no-persistence restriction, and it must not land before T129 (FR-025, FR-030, SC-019)
- [ ] T134 [US2] Attach screened images to the agent context in `src/civsim_harness/agent/context.py`: a capture may be shown only when `screening_status = screened_clean`, its `view_declaration_id` resolves in the run's catalog version, **and it belongs to the current decision step**; unscreened or stale-step images have no path in (FR-024, FR-025, FR-015)
- [ ] T135 [US2] Record the observation and action catalog versions plus content hash on every run in `src/civsim_harness/run/preparation.py`, so runs before and after a catalog change stay distinguishable (FR-022, FR-029)
- [ ] T136 [US2] Implement the preflight capability-resolution gate in `src/civsim_harness/run/preparation.py`: every observation and action the run could use resolves to a declaration, or the run does not start, naming the offending capability (FR-023, V5, SC-006)
- [ ] T137 [US2] Enforce declaration attribution on the record path in `src/civsim_harness/observe/assemble.py` and `src/civsim_harness/act/dispatch.py`: an `ObservationEntry` without a `declaration_id` and a `Decision` without an `action_declaration_id` cannot be constructed — including the end-turn decision, which resolves to `turn.end_turn` like any other (SC-007, FR-008)
- [ ] T138 [US2] Implement `civsim audit parity <run_id>` in `src/civsim_harness/operator/audit.py`: from the record alone, enumerate every distinct observation and action the run used — structured and visual, across every decision step — and resolve each to its parity declaration and catalog version, reporting any unresolved entry as a finding (SC-006, SC-007)
- [ ] T139 [US2] Implement `civsim audit prompts <run_id>` and `civsim audit decisions <run_id>` in `src/civsim_harness/operator/audit.py`: every prompt encountered is a recorded `prompt_response` decision or a recorded stall, and zero decisions exist without a `model_call_id` (SC-005, SC-012, invariant I5)
- [ ] T140 [US2] Implement `civsim audit steps <run_id>` and `civsim audit loop <run_id> --turn N` in `src/civsim_harness/operator/audit.py`: assert one decision, one model call, and one observation per step; contiguous `step_index` from 1; and that each step's observation was assembled after the prior step's verification with no observation or capture reused (SC-003, invariants I13, I14, I15)
- [ ] T141 [US2] Implement `civsim audit capabilities <run_id>` in `src/civsim_harness/operator/audit.py`: every capability the run used is `path: firetuner` or carries a non-empty `firetuner_gap` stating what it does, what it reads or writes, and why Firetuner could not do it (FR-028, SC-020)

**Checkpoint**: A completed run's parity boundary is auditable end to end from its record alone

---

## Phase 5: User Story 3 - Keep a long run alive across crashes, stalls, and provider failures (Priority: P3)

**Goal**: The harness detects a crashed, hung, or unresponsive client and a failing provider within
bounded time, recovers from the last good save as the same continuous run, and — when it genuinely
cannot — stops in a clearly recorded failed state. It never skips a turn, invents an action, or
plays on from a stale picture of the board.

**Independent Test**: Start a run, kill the game client mid-turn, and verify the harness detects the
crash, records it, resumes from that turn's quicksave as the same run, re-observes before acting, and
completes the run — with both the abandoned attempt and the replayed turn present in the record.

### Tests for User Story 3 ⚠️

- [ ] T142 [P] [US3] Integration test in `tests/integration/test_recovery.py`: the fake Nexus drops mid-turn at step *n*; a `crash_detected` event is recorded, the run resumes from **that turn's** quicksave as the same run, re-observes before acting, and the turn carries one `abandoned` and one authoritative attempt (FR-044 – FR-047)
- [ ] T143 [P] [US3] Integration test in `tests/integration/test_provider_resilience.py`: transient failures retry with backoff, then fall back to the next model, with every attempt a recorded run event; chain exhaustion pauses the run in a recorded state with zero fabricated, skipped, or defaulted turns (FR-041, FR-042, SC-012)
- [X] T144 [P] [US3] Unit test in `tests/unit/test_detection.py`: each of the four signals — process liveness, tuner heartbeat, per-operation bounds, and the screen-identity probe — independently trips detection within the 60 s budget. **No signal may key off elapsed turn time**: a scripted multi-hour productive turn must trip nothing (SC-010, FR-014, research R12)
- [ ] T145 [P] [US3] Unit test in `tests/unit/test_completeness.py`: a turn number with no authoritative attempt forces `record_completeness_status = has_gaps`; a missing `step_index` within a turn does the same; a contiguous authoritative turn sequence with contiguous steps yields `complete` (FR-052, SC-003, SC-011, invariant I11)
- [ ] T146 [P] [US3] Contract test in `tests/contract/test_provider_resilience_contract.py` asserting P5 (retry then fall back, every attempt a recorded event), P6 (exhaustion raises; the harness has no fabricate, skip, or default-move path), and P11 (no turn-level clock cancels a call)

### Implementation for User Story 3

- [X] T147 [P] [US3] Implement the process liveness monitor in `src/civsim_harness/resilience/liveness.py` using `psutil` against the client PID, catching hard crashes and process exit (research R12)
- [X] T148 [P] [US3] Implement heartbeat monitoring in `src/civsim_harness/resilience/heartbeat_monitor.py` on top of the T034 probe, raising `hang_detected` when the nonce fails to round-trip within its bound (research R12)
- [X] T149 [P] [US3] Implement per-operation bound enforcement in `src/civsim_harness/resilience/operation_bounds.py`: every Nexus command, capture, and post-action read-back carries its own timeout, catching a client that is alive and answers the heartbeat but will not service a specific operation. **These bounds are scoped to a single operation, never to a turn** — a long turn is not evidence of a fault (research R12, FR-014)
- [X] T150 [US3] Implement the detection aggregator in `src/civsim_harness/resilience/detector.py`: any of the four signals trips detection within 60 s, each recorded as its own run event — `crash_detected`, `hang_detected`, `unresponsive_detected`, or `unknown_screen`. The removed `stall` event type must not reappear; a turn that stops accomplishing anything is the turn cycle's no-progress backstop, not a detection concern (FR-044, SC-010, data-model.md §14)
- [X] T151 [US3] Implement recovery in `src/civsim_harness/resilience/recovery.py`: preserve the last-known-good save and state, resume from **this turn's** start quicksave as the same continuous run, and re-observe and re-capture before acting — nothing obtained before the interruption is reusable (FR-045, FR-046)
- [ ] T152 [US3] Implement attempt bookkeeping in `src/civsim_harness/run/turn_cycle.py`: the abandoned attempt is retained with all the steps it completed, and the replayed attempt is marked authoritative, with exactly one authoritative attempt per `(run_id, turn_number)` (FR-047, invariant I9)
- [X] T153 [US3] Wire mid-turn observation failure into recovery in `src/civsim_harness/resilience/recovery.py`: T096's `ObservationAssemblyError` takes the same abandon-and-replay path as a crash, since a turn cannot continue on a stale board after earlier steps have executed (spec edge case, FR-046)
- [X] T154 [US3] Implement the recovery bound in `src/civsim_harness/resilience/recovery.py`: after `recovery_attempt_limit` consecutive failures, stop in a recorded `failed` state identifying the last-known-good save via `get_last_known_good`, with a `recovery_limit_reached` event and no indefinite retry loop (FR-048, SC-021)
- [X] T155 [US3] Implement retry and fallback in `src/civsim_harness/provider/chain.py`: exponential backoff with jitter on transient failures (HTTP 429 and transient 5xx), then the next model in the chain, recording `provider_failure`, `provider_retry`, and `provider_fallback` events; context-length and modality refusals map to `context_rejected` as a chain-level failure rather than a retry. Fallback is decided **per call**, so a single turn may be served by more than one model (FR-041, P5)
- [X] T156 [US3] Implement chain exhaustion handling in `src/civsim_harness/provider/chain.py` and `src/civsim_harness/run/runner.py`: the provider layer raises `ProviderChainExhausted`, a `model_chain_exhausted` event is recorded, and the run pauses in a recorded state — there is no fabricate, skip, or default-move path to take instead (FR-042, P6, SC-012)
- [ ] T157 [US3] Implement bounded capture retries and degradation marking in `src/civsim_harness/observe/capture.py`: after bounded retries, set `DecisionStep.visually_degraded = true`, roll it up to `TurnCycle.visually_degraded`, downgrade `Run.comparability_status`, and record a `capture_failed` event with its `step_index` — so a turn that lost its images part-way is distinguishable from one that never had them, rather than proceeding on structured state alone as if nothing changed (FR-050, SC-013)
- [ ] T158 [US3] Implement completeness derivation in `src/civsim_harness/store/completeness.py`: gap markers from `turn_gaps` **and `step_gaps`** drive `record_completeness_status`, so a turn present but internally incomplete is not reported as complete (FR-052, SC-003, SC-011)
- [X] T159 [US3] Implement reconnect discipline in `src/civsim_harness/nexus/client.py`: a reconnect re-runs the full handshake and re-resolves state indices; indices from before a disconnect are never reused (contracts/nexus-protocol.md)
- [ ] T160 [US3] Implement `civsim audit recovery <run_id>` and `civsim audit completeness <run_id>` in `src/civsim_harness/operator/audit.py`, reporting crash/resume event pairs, abandoned-vs-authoritative attempts per turn, and zero silently missing turns or steps (SC-003, SC-011, SC-021)

**Checkpoint**: A run survives client crashes and provider failures unattended, or fails loudly

---

## Phase 6: User Story 4 - Branch and replay a run from any turn (Priority: P4)

**Goal**: Any recorded turn's save can start a new run that records what it branched from, plays
forward under different conditions, and leaves the parent's record untouched — and no save is ever
removed except by an operator's explicit archival.

**Independent Test**: Take a completed run, branch from its turn 23 save twice under two different
model configurations, play both to turn 30, and verify each branch records its parent run and turn,
both diverge from the identical starting position, and the parent run's record is unchanged.

### Tests for User Story 4 ⚠️

- [ ] T161 [P] [US4] Integration test in `tests/integration/test_branching.py`: two branches from the same save point each record `parent_run_id` and `parent_turn`, and the parent's record is byte-identical before and after (FR-033, FR-034, invariant I12, SC-014)
- [ ] T162 [P] [US4] Unit test in `tests/unit/test_retention.py`: a **finished, unarchived, year-old** run yields zero eligible save points — not by age, not by quota, not by retention window, not by being terminal, not by thinning. Only `archive_run` makes a save eligible, and a required save found missing is reported rather than worked around (FR-036, invariant I17, research R17)
- [ ] T163 [P] [US4] Unit test in `tests/unit/test_headroom.py`: with free space below `min_free_disk_gb`, the run halts in a recorded state with a `disk_headroom_low` event and **zero saves deleted** — the harness must never free space by removing an unarchived save (spec edge case, research R17)
- [ ] T164 [P] [US4] Contract test in `tests/contract/test_branch_configuration.py`: a `branch_from` configuration that restates seed, civilization, ruleset, mod set, map, or game settings differently is rejected; only `model_config`, `guidance_set`, `stop_condition`, `no_progress_step_limit`, `recovery_attempt_limit`, and `min_free_disk_gb` may vary (contracts/run-configuration.md "Branch configuration")

### Implementation for User Story 4

- [ ] T165 [US4] Implement save point addressing in `src/civsim_harness/saves/addressing.py`: resolve any save point by run, turn, and lineage without inspecting the filesystem or the game client — `save_name` is a naming convention, not the address (FR-032)
- [ ] T166 [US4] Implement branch configuration in `src/civsim_harness/config/run_config.py`: the `branch_from` block (`run_id`, `turn`), with seed, civilization, ruleset, mod set, map, and game settings **inherited from the parent and non-overridable**, and only `model_config`, `guidance_set`, `stop_condition`, `no_progress_step_limit`, `recovery_attempt_limit`, and `min_free_disk_gb` variable
- [ ] T167 [US4] Implement branch creation in `src/civsim_harness/saves/branching.py`: load the parent's turn-start save, create a new run recording `parent_run_id` and `parent_turn` as its lineage, and emit a `branch_created` event. A branch from a save that no longer exists is rejected with the missing save named, never retargeted to a nearby turn (FR-033, FR-036)
- [ ] T168 [US4] Enforce parent immutability in `src/civsim_harness/store/sqlite_adapter.py`: reject any write from a child run that targets parent records; the port has no delete operation on turn records and that is deliberate (FR-034, invariant I12)
- [ ] T169 [US4] Implement branch abandonment in `src/civsim_harness/saves/branching.py`: record a `branch_abandoned` event and mark the affected turns superseded via `mark_turn_superseded` rather than deleting them (FR-035)
- [ ] T170 [US4] Implement archival in `src/civsim_harness/saves/archival.py`: `archive_run` is the **only** operation that makes a save point eligible for removal. It sets `Run.archived_at`, writes a `run_archived` event, leaves records, events, and captures intact, and is rejected on a non-terminal run. No age, quota, retention-window, or thinning rule may exist anywhere in this module (FR-036, invariant I17, research R17)
- [ ] T171 [US4] Implement the operator-invoked reaper in `src/civsim_harness/saves/reaper.py`: delete save **files** only for save points returned by `list_eligible_save_points()`, defaulting to `--dry-run`. It is never a background job and never runs unattended; the `SavePoint` record survives the file so a later branch attempt fails explainably (FR-036, research R17)
- [ ] T172 [US4] Implement missing-save reporting in `src/civsim_harness/saves/addressing.py` and `src/civsim_harness/resilience/recovery.py`: a required save found absent records a `save_missing` event and fails, never resuming from a different turn (FR-036)
- [ ] T173 [US4] Implement `run branch`, `run resume-from`, `run archive`, and `saves reap` in `src/civsim_harness/operator/cli.py` and `src/civsim_harness/operator/api.py`, rejecting a `resume-from` or `branch` whose save is missing with the missing save named, and rejecting `archive` on a non-terminal run (FR-004, contracts/operator-surface.md)
- [ ] T174 [US4] Implement `civsim seedset accept-build` in `src/civsim_harness/operator/cli.py`: append a `BuildAcceptance` to the seed set, record a `game_build_change_accepted` event, and make the acceptance referenceable by every dependent run. Acceptance is scoped to one set and **one composite transition** (platform + version) — there is no global override, and accepting a version bump never implicitly accepts a platform change. **A version-only transition** (e.g. `win/1.0.12.9 → win/1.0.12.11`) **requires no spike result; a platform-crossing transition** (e.g. `win/1.0.12.9 → mac/1.0.12.9`) **is refused unless a passing R20 spike result (T199) is on record** — the command populates `BuildAcceptance.r20_spike_ref` from that recorded result or rejects the acceptance (FR-002, research R18, R20)
- [ ] T175 [US4] Enforce same-build branching in `src/civsim_harness/saves/branching.py` per **FR-033**: a branch whose game build — **platform or version** — differs from the parent run's is refused by default with the mismatch recorded, under the same recorded-acceptance mechanism as FR-002's build change, and a branch relying on an acceptance records it. **A version-only branch transition needs no spike result; a platform-crossing branch is refused unless the `BuildAcceptance` it relies on carries a passing R20 spike result (`r20_spike_ref`, T199)** — Civ VI's cross-platform saves are account-gated and version-matched, which is not a foundation for FR-034's "two branches from the same save point begin from an identical position" (FR-033, FR-034, research R20)
- [ ] T176 [US4] Implement `civsim audit lineage <branch_id>`, `civsim audit immutability <run_id>`, and `civsim audit builds <seed_set>` in `src/civsim_harness/operator/audit.py`, the last reporting a set with any accepted build change as **non-uniform** with its runs partitioned by build (quickstart.md Scenarios 5 and 9, FR-031)
- [ ] T177 [P] [US4] Live test in `tests/live/test_branch_identity.py` (marked `live`): two branches from one save point begin from an identical game position (SC-014)

**Checkpoint**: Branching and replay work, the parent record is provably untouched, and no save disappears without an operator saying so

---

## Phase 7: User Story 5 - Swap the agent's model without touching the harness (Priority: P5)

**Goal**: Changing which model plays is a change to run configuration and nothing else, with every
call recording the model that actually served it and no path that drops images to make a call fit.

**Independent Test**: Run the same seed and configuration twice, changing only the model identifier
in the run configuration, with no code or integration change between the two; verify both complete
and both records name the model that actually served each call.

### Tests for User Story 5 ⚠️

- [X] T178 [P] [US5] `ModelProvider` conformance suite in `tests/contract/test_model_provider_port.py` asserting P1–P11 against both the OpenRouter adapter and the fake provider (contracts/model-provider-port.md)
- [X] T179 [US5] Red-team test in `tests/contract/test_model_provider_port.py`: a fake provider advertising a tiny context or `accepts_images = False` produces a **failed run**, never a reduced-image call (P2, FR-039, SC-017)
- [X] T180 [US5] Red-team test in `tests/contract/test_model_provider_port.py`: a fake returning **two decisions for one request raises** — the extras are neither dropped nor queued, since silently discarding the second is as wrong as executing it (P10, FR-008, invariant I13)
- [X] T181 [US5] Red-team test in `tests/contract/test_model_provider_port.py`: a key planted in the environment appears in no serialized record, log line, or raised exception (P8, FR-043, SC-018)
- [X] T182 [P] [US5] Unit test in `tests/unit/test_model_swap.py`: no model identifier appears anywhere outside `RunConfiguration.model_config`, so a swap is configuration-only. Additionally, run the same seed and configuration through two distinct-vendor models routed via OpenRouter (e.g. `anthropic/claude-sonnet-5` and `google/gemini-3-pro`) and verify both complete and both records name the model that actually served each call, discharging SC-015's "verified on at least two providers" against two vendors rather than two models from one (FR-038, P9, SC-015)

### Implementation for User Story 5

- [X] T183 [US5] Implement `describe()` in `src/civsim_harness/provider/openrouter.py` reading OpenRouter's models endpoint for modality and context length rather than hard-coding them, returning `confirmed = False` when the endpoint cannot confirm (research R10)
- [X] T184 [US5] Implement chain preflight in `src/civsim_harness/provider/preflight.py`: before turn 1, `describe()` every model in `primary + fallbacks`; any model with `accepts_images = False`, `confirmed = False`, or context insufficient for a **worst-case decision step** (late-game structured state plus that step's declared views) fails the run, naming the failing model (FR-039, P1, V4, SC-017)
- [X] T185 [US5] Enforce the no-image-drop rule in `src/civsim_harness/provider/chain.py`: `image_count` must equal `len(request.images)`, and a mismatch raises as a defect — there is no code path that removes images to make a call fit (FR-039, P2, invariant I7)
- [X] T186 [US5] Implement per-call accounting in `src/civsim_harness/provider/accounting.py`: write a `ModelCall` record carrying `decision_step_id`, `model_requested`, `model_served`, `latency_ms`, provider-reported `cost`, `retry_count`, `fallback_occurred`, `image_count`, and `outcome` through the store. Per-call cost and latency are recorded and never aggregated away, since per-turn cost is unbounded by construction and the trade-off must stay visible (FR-040, P3, P7)
- [X] T187 [US5] Link every `Decision` to the `ModelCall` that produced it through its `DecisionStep` in `src/civsim_harness/agent/decisions.py`, so the model that served each step is resolvable and fallback-served steps roll up to a turn that is distinguishable from a primary-served one (FR-040, SC-016, invariant I5)
- [X] T188 [US5] Enforce adapter obligations in `src/civsim_harness/provider/port.py`: an adapter may not modify the images or observation it was handed, substitute a different model without reporting it in `model_served`, swallow an error and return `decision = None` with a successful outcome, return more than one decision, carry state between calls, or read run configuration or game state directly (contracts/model-provider-port.md "Adapter obligations")
- [X] T189 [US5] Implement call-time credential handling in `src/civsim_harness/provider/openrouter.py`: keys resolve from the environment or secrets file at call time, never appear in a request record, log line, error message, or exception trace (FR-043, P8)
- [ ] T190 [US5] Implement `civsim audit models <run_id>` and `civsim audit secrets <run_id>` in `src/civsim_harness/operator/audit.py`: every call names its served model with latency, cost, and retry count, resolved per step and rolled up per turn; zero credential-shaped values appear in records, captures, or logs (SC-016, SC-018)

**Checkpoint**: All five user stories are independently functional

---

## Phase 8: Polish & Cross-Cutting Concerns

**Purpose**: The live tier, the release gates, and the validation the quickstart specifies

- [ ] T191 [P] Live test in `tests/live/test_preparation.py` (marked `live`): real client, real settings — an exact match starts, and any mismatch fails before turn 1 with the mismatch recorded (FR-002, V2)
- [ ] T192 [P] Live test in `tests/live/test_build_pin.py` (marked `live`): a seed set pinned to a build the client does not report fails the run before turn 1; after `seedset accept-build`, the run starts and records its acceptance reference, and the set reports as non-uniform (FR-002, FR-031, quickstart.md Scenario 9)
- [ ] T193 [P] Live test in `tests/live/test_crash_recovery.py` (marked `live`): a genuinely killed client is detected within 60 s and the run resumes as the same continuous run losing at most the turn in progress (SC-010)
- [ ] T194 [P] Live test in `tests/live/test_capture_hygiene.py` (marked `live`): the FireTuner window, a developer console, and an unrelated window placed over the client are absent from captured frames, and no capture border appears (SC-009, SC-019)
- [ ] T195 [P] Regenerate the published JSON Schemas into `specs/002-civ-playing-harness/contracts/schemas/` and confirm additive-only evolution against the previous version
- [ ] T196 [P] Write the catalog authoring guide in `docs/catalog-authoring.md`: how to add a declaration with its parity basis **before first use**, and how a new capability bumps `catalogs/VERSION` (FR-029, spec edge case on mid-project capability additions)
- [ ] T197 [P] Write `README.md` covering setup, expected `doctor` output, and the CI-runnable subset from quickstart.md
- [ ] T198 [P] Live test in `tests/live/test_host_platform.py` (marked `live`), run **on each target platform**: window identity resolves against the real client, capture returns a frame matching the client rect, directories resolve to the real install, and the reported tier matches what the capabilities actually support (research R19)
- [ ] T199 Run the research R20 cross-platform save spike and record the result in `specs/002-civ-playing-harness/spikes/r20-cross-platform-saves.md`: whether a `.Civ6Save` written on one platform loads and resolves identically on another. Until this passes, cross-platform branching stays refused — the spike gates only the relaxation, never a core capability. **Consumed by T174 and T175** (Phase 6): both implement the platform-crossing check against `BuildAcceptance.r20_spike_ref` ahead of this task, but that check has nothing passing to reference — and therefore keeps refusing every platform-crossing acceptance and branch — until this task records a passing result here
- [ ] T200 Run the quickstart.md Scenarios 1–10 end to end against a real client and record the outcomes in `specs/002-civ-playing-harness/validation-results.md`, **noting the platform and tier each scenario ran under** — a scenario passing on a `VALIDATED` host says nothing about a `SUPPORTED` one
- [ ] T201 Soak validation recorded in `specs/002-civ-playing-harness/validation-results.md`: one 300+ turn full game to a victory or defeat outcome — including at least one late-game turn of several hundred decision steps completing untruncated — and a 20+ run unattended batch, both with zero turn or step gaps (SC-002, SC-011, FR-014)
- [ ] T202 Record per-run cost and duration observations in `specs/002-civ-playing-harness/validation-results.md`: calls per turn, cost per turn, and total run cost under the one-call-per-step model, so the trade-off the spec accepted is measured rather than assumed (spec Assumptions, plan Scale/Scope)
- [ ] T203 Add the per-release parity audit gate to `.github/workflows/release.yml`, running `audit parity`, `audit capabilities`, `audit secrets`, `audit steps`, and the red-team suite, and blocking release on any finding (SC-006, SC-008, SC-009, SC-018, SC-019, SC-020)
- [ ] T204 Performance pass in `src/civsim_harness/run/turn_cycle.py` and `src/civsim_harness/store/sqlite_adapter.py`: confirm the store write leaves deliverable 1's 5 s currency window intact for a several-hundred-step turn, and that detection stays inside the 60 s budget under a late-game load (plan Performance Goals)
- [ ] T205 Re-check the delivered harness against Constitution v1.0.0 Principles I–VII and record the result in `specs/002-civ-playing-harness/validation-results.md`, as the Governance section's compliance review requires before implementation closes

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — starts immediately
- **Foundational (Phase 2)**: Depends on Setup — **BLOCKS all user stories**
- **User Story 1 (Phase 3)**: Depends on Foundational. No dependency on any other story
- **User Story 2 (Phase 4)**: Depends on Foundational. Independently testable against a US1 run, but its screening and audit work assumes US1's per-step capture pipeline and turn records exist
- **User Story 3 (Phase 5)**: Depends on Foundational and on US1's turn cycle and save discipline. T157 (degradation marking) also assumes US2's screening
- **User Story 4 (Phase 6)**: Depends on Foundational and on US1's save points; recovery reporting (T172) touches US3's recovery module
- **User Story 5 (Phase 7)**: Depends on Foundational and US1's provider adapter; T185 builds on US3's chain module
- **Polish (Phase 8)**: Depends on all desired stories being complete

### User Story Dependencies

- **US1 (P1)**: Foundational only — the MVP, and the only story with no upstream story
- **US2 (P2)**: Structurally independent (the filter and screening are new modules), but its Independent Test reads a completed US1 run
- **US3 (P3)**: Builds on US1's turn cycle and quicksaves; its Independent Test kills a client mid-run
- **US4 (P4)**: Builds on US1's save points; designed into the save model from the start rather than bolted on
- **US5 (P5)**: Builds on US1's provider adapter; its Independent Test needs two completed runs

### Within Each User Story

- Tests are written and failing before implementation
- Spikes before the work they gate: **T077 (R5) before any save work**; **T099 (R6) before any image reaches the agent**; **T071 (R18) before the build pin is wired**; **T199 (R20) before cross-platform branching is relaxed**
- **The host port (T047–T049) precedes its adapters (T050–T052), which precede anything that
  captures, saves, or resolves a game directory.** Everything downstream talks to the port, never to
  a platform library (research R19)
- Lua and catalog declarations before the modules that bind to them
- Models before services; services before orchestration; orchestration before the operator surface
- Within the run loop specifically: T110 (the loop) → T111 (no-progress accounting) → T112 (termination) → T114 (turn cycle) → T117 (persistence). T113 is a constraint on T111 and lands with it
- Each story is complete and checkpointed before the next priority begins

### Parallel Opportunities

- **`[P]` means a different file from every other task running with it.** Where several tasks edit one file — `pyproject.toml`, `models/config.py`, `models/turn.py`, and the shared contract-test modules — only the first carries `[P]` and the rest are sequential behind it, even though they look independent
- Setup: T004, T006, T007, T009, T010, and T011 run together after T001–T003; T005, T008, and T012 follow T004 sequentially because they all edit `pyproject.toml`
- Foundational: T017, T018, T020, T021, and T024–T026 run together (T019 follows T018, and T022–T023 follow T021, on the same files); the codec (T029–T034), catalog (T035–T037), store (T038–T042), provider (T043), and host-adapter (T050–T052) tracks are independent of each other and of the model definitions
- All test tasks within a story marked [P] run together before that story's implementation
- US1's Lua files (T082–T089) and catalog files (T090–T094) are all separate files and run together
- Once Foundational completes, separate developers can take US1, and then US2/US3/US4/US5 in parallel once US1's turn cycle lands

---

## Parallel Example: User Story 1

```bash
# Launch all US1 tests together (they must fail first):
Task: "Integration test full turn cycle in tests/integration/test_turn_cycle.py"
Task: "Integration test the decision-step loop in tests/integration/test_decision_loop.py"
Task: "Integration test both turn endings in tests/integration/test_turn_endings.py"
Task: "Unit test no-truncation of a 500-step turn in tests/unit/test_no_truncation.py"
Task: "Unit test turn attempt state machine in tests/unit/test_turn_state_machine.py"
Task: "Contract test run configuration validation in tests/contract/test_run_configuration.py"
Task: "Contract test the build pin in tests/contract/test_build_pin.py"
Task: "Integration test preparation mismatch in tests/integration/test_preparation.py"
Task: "Integration test prompt handling in tests/integration/test_prompts.py"
Task: "Integration test stop conditions in tests/integration/test_stop_conditions.py"

# Launch all declared Lua together (separate files, no shared state):
Task: "GameCore_Tuner read Lua for map/units/cities in lua/gamecore/"
Task: "InGame Lua for unit orders in lua/ingame/unit_orders.lua"
Task: "InGame Lua for city orders in lua/ingame/city_orders.lua"
Task: "InGame Lua for empire orders in lua/ingame/empire_orders.lua"
Task: "InGame Lua for screens and prompts in lua/ingame/screens.lua"
Task: "InGame Lua for turn control in lua/ingame/turn_control.lua"

# Launch all catalog authoring together:
Task: "Observation declarations in catalogs/observations/*.yaml"
Task: "Action declarations in catalogs/actions/*.yaml"
Task: "The end_turn declaration in catalogs/actions/turn.yaml"
Task: "Prompt declarations in catalogs/actions/prompts.yaml"
Task: "IntegrationCapability declarations in catalogs/capabilities.yaml"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup
2. Complete Phase 2: Foundational — **blocks everything**; the catalog loader, store port, and Nexus
   client are what every later phase binds to
3. Complete Phase 3: User Story 1
4. **STOP and VALIDATE**: run quickstart.md Scenario 1 — a turn-50 run with zero human interaction,
   50/50 authoritative turns, zero step gaps, 50/50 verified quicksaves, zero decisions without a
   `model_call_id` — and Scenario 8, which checks the loop re-observes between decisions, a long
   turn is not truncated, and the backstop ends a stuck turn and is recorded as such
5. That run is independently valuable on the day it first works: it is the deliverable the interface,
   the store, and the optimization layer all have nothing to show until it exists

### Incremental Delivery

1. Setup + Foundational → the boundary and the transport exist
2. + US1 → **MVP**: a run plays itself end to end, one decision step at a time, and records every
   step (structured state; images follow, and until then runs are honestly marked visually degraded)
3. + US2 → the run's data becomes *admissible*: every observation and action resolves to a declared
   parity basis, and screened images enter the agent's context at each step
4. + US3 → the harness becomes usable unattended in volume rather than only in demos
5. + US4 → branch-and-replay, the mechanism deliverable 4's Monte Carlo and ablation work needs,
   with saves that persist until someone decides otherwise
6. + US5 → cost-optimized experimentation across a seed set under swappable models

### Parallel Team Strategy

With multiple developers, after Foundational completes:

1. One developer drives US1 to the MVP checkpoint — it is on everyone else's critical path
2. Then in parallel: Developer A on US2 (parity, screening, audit), Developer B on US3 (detection,
   recovery, provider resilience), Developer C on US4 + US5 (saves, branching, archival, provider
   layer)
3. The two cross-story touch points to coordinate are `observe/capture.py` (US1 per-step pipeline,
   US2 screening, US3 degradation) and `provider/chain.py` (US3 fallback, US5 accounting)

---

## Notes

- **The spikes are gates, not chores.** T077 (R5) must produce a documented negative result
  before the bespoke save path is enabled, or Principle II's Firetuner-first ordering is violated.
  T099 (R6) must pass before any image reaches the agent **on that platform**; if it fails, runs there proceed visually
  degraded under FR-050 — a legitimate operating state, not a blocker. T071 (R18) decides whether
  the build read is a declared Lua observation or the executable's file version; either way the
  check happens.
- **Four negative tests guard the clarification's invariants**, and they matter more than they look,
  because each forbids something that would pass review as a sensible feature:
  - T061 — a 500-step productive turn completes untouched (no time, step, or cost cap) (I16)
  - T162 — a finished, unarchived, year-old run yields zero eligible saves (I17)
  - T180 — a provider returning two decisions for one step raises (I13)
  - T059 — a step reusing the prior step's observation or capture fails (I14)
- **Zero-tolerance criteria drive test placement.** SC-006, SC-008, SC-009, SC-018, SC-019, and
  SC-020 are worded so any finding blocks release, which is why the red-team suites live in
  `tests/contract` and run on every build rather than per release.
- **`turn_time_budget_s` must not reappear anywhere.** It was removed by the spec's clarification and
  data-model.md §2 records its return as a regression rather than an addition.
- `[P]` tasks touch different files and have no dependency on incomplete work
- `[Story]` labels map tasks to spec.md user stories for traceability; Setup, Foundational, and
  Polish tasks carry no story label by design
- Commit after each task or logical group; stop at any checkpoint to validate a story independently
- Avoid: recording an action applied without verification, ending a turn before its record is
  durable, batching decisions within a turn, reusing a step's observation or capture, deleting a
  save nobody archived, and any code path that would drop images to make a model call fit
