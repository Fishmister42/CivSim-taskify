---

description: "Task list for feature implementation"
---

# Tasks: Unified Web Interface

**Input**: Design documents from `/specs/001-unified-web-interface/`
**Prerequisites**: [plan.md](./plan.md), [spec.md](./spec.md), [research.md](./research.md),
[data-model.md](./data-model.md), [contracts/web-read-api.md](./contracts/web-read-api.md),
[contracts/panel-registry.md](./contracts/panel-registry.md), [quickstart.md](./quickstart.md)

**Tests**: Included. Spec Success Criteria SC-004, SC-005, SC-012, SC-014, and SC-016 are worded as
release-blocking audits ("any finding blocks release," "100% of attempts"), and the plan's Testing
strategy and the two contracts each name the specific automated suite that discharges one of them.
Test tasks below make those audits mechanical (CI-run) rather than manual per-release checks.

**Organization**: Tasks are grouped by user story (spec.md's US1–US4, in priority order) so each story
is independently implementable, testable, and shippable on its own, per this feature's own
Independent Test criteria.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel — different files, no dependency on an incomplete task
- **[Story]**: Which user story this task belongs to (US1–US4); omitted for Setup, Foundational, and
  Polish tasks
- Every task names its exact file path

## Path Conventions

Single Python project, `civsim_web` as a sibling package to `002-civ-playing-harness`'s
`civsim_harness` (plan.md Structure Decision):

```text
panels/                 # Panel Registry data (YAML), not Python
src/civsim_web/          # This feature's code
tests/{unit,contract,integration}/   # This feature's tests
```

`pyproject.toml`, `src/civsim_harness/`, and `tests/` subpaths belonging to
`002-civ-playing-harness` are **out of scope for every task below except where a task explicitly adds
to a shared file** (T002); no task here modifies 002's own source, tests, or catalogs.

---

## Phase 1: Setup

**Purpose**: Stand up the package skeleton this feature's code and tests live in.

- [X] T001 Create the `src/civsim_web/` package skeleton with empty `__init__.py` files in
  `store_client/`, `registry/`, `viewmodels/`, `health/`, `refs/`, `routes/`, `negotiate/`,
  `templates/`, `static/`, and `net/`, plus a top-level `panels/` directory — the full layout named in
  plan.md's Project Structure section.
- [X] T002 Add `civsim_web` as a dependency group in `pyproject.toml` alongside the existing
  `civsim_harness` group, declaring `fastapi`, `uvicorn`, `jinja2`, `pydantic>=2`, `httpx` (test-only),
  `pytest`, `pytest-asyncio`, `syrupy`. Add entries only — do not remove, reorder, or otherwise alter
  any existing `civsim_harness` dependency declaration, since `pyproject.toml` is shared with the
  parallel `002-civ-playing-harness` effort.
- [X] T003 [P] Configure `ruff` (or the project's existing linter config) to cover `src/civsim_web/`
  and this feature's `tests/` paths, matching the formatting rules already applied to
  `src/civsim_harness/`.
- [X] T004 [P] Add `conftest.py` fixtures scaffolding for `tests/unit/`, `tests/contract/`, and
  `tests/integration/` under this feature's test tree (a seeded `MatchStore` fake fixture placeholder,
  a FastAPI `TestClient` fixture placeholder) — populated by Foundational tasks below.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The `MatchStore` seam, the Panel Registry, the shared view models, and the
content-negotiation mechanism every user story is built on.

**⚠️ CRITICAL**: No user story task may begin until this phase is complete.

- [X] T005 Define the `MatchStore` Protocol import seam in `src/civsim_web/store_client/port.py`: a
  typed re-export matching the method signatures published in
  `specs/002-civ-playing-harness/contracts/match-store-port.md`'s read operations only
  (`get_run`, `get_turn_cycle`, `list_active_runs`, `turn_gaps`, `step_gaps`, `list_save_points`,
  `get_last_known_good`, `ping`). Document this module as the **only** module in `civsim_web` permitted
  to reference the `MatchStore` type (plan.md Constraints — read-only, structurally).
- [X] T006 Implement a Protocol-conformant `MatchStore` fake in `src/civsim_web/store_client/fake.py`
  covering every read operation named in T005, seeded from fixture data. The fake MUST NOT expose any
  of the port's nine write operations (`create_run`, `update_run`, `write_turn_cycle`,
  `write_run_event`, `write_model_call`, `write_save_point`, `write_capture`,
  `mark_turn_superseded`, `archive_run`) — omit them entirely rather than stubbing them as no-ops, so a
  call to one is a type/attribute error, not a silent success. Depends on T005.
- [X] T007 [P] Implement the Panel Registry loader/validator in `src/civsim_web/registry/loader.py`
  enforcing every rule in [contracts/panel-registry.md](./contracts/panel-registry.md): **P1** "A panel
  with `category: in_game` and an empty/missing `parity_basis` fails to load"; **P2** "A panel with
  `category: out_of_game_telemetry` must have `parity_basis: null`"; **P3** every `source_fields` entry
  resolves to a field actually present in `specs/002-civ-playing-harness/data-model.md`; **P4**
  `panel_id` unique across all four YAML files; **P5** `scope` consistent with every listed
  `source_fields` entry's granularity; **P6** declarations immutable within a version. A load failure
  on any rule MUST abort startup.
- [X] T008 [P] Create `panels/live.yaml`, `panels/history.yaml`, `panels/catalog.yaml`,
  `panels/shared.yaml` as valid-but-empty declaration lists (`[]`), and `panels/VERSION` containing
  `1`, so T007's loader has a valid target from the start.
- [X] T009 [P] Implement the view models shared across every user story in
  `src/civsim_web/viewmodels/base.py`: `RunSummaryView`, `HealthStatus`, `InterventionInfo`, and
  `PanelRegistryVersion`, exactly as specified in data-model.md §1, §2, §4, §13. `InterventionInfo`
  MUST carry no field through which a mutating request could be issued (data-model V9 / FR-026).
- [X] T010 [P] Implement `HealthStatus` derivation in `src/civsim_web/health/derive.py` per
  data-model.md §2's rule: "`state` is computed from `Run.lifecycle_state` first ... refined only by
  the *presence* of a matching, more specific `RunEvent`... never by an independent timestamp
  comparison this feature invents." Enforce the invariant verbatim: "`stalled` may only be set when a
  `RunEvent` of type `hang_detected` or `unresponsive_detected` exists for this run with no later
  `resumed`/`playing` transition after it."
- [X] T011 [P] Implement `ViewReference` parsing and serialization in
  `src/civsim_web/refs/reference.py` per data-model.md §12's canonical path shapes: run
  (`/runs/{run_id}`), turn (`/runs/{run_id}/turns/{turn_number}`), step
  (`/runs/{run_id}/turns/{turn_number}/steps/{step_index}`), turn-scoped panel
  (`/runs/{run_id}/turns/{turn_number}/panels/{panel_id}`), and run-scoped panel
  (`/runs/{run_id}/panels/{panel_id}`).
- [X] T012 [P] Implement the content-negotiation seam in `src/civsim_web/negotiate/respond.py`: one
  function taking a view model and a Jinja2 template name, returning the view model's JSON
  serialization on `Accept: application/json` or `?format=json`, and the rendered template with that
  same view model as context otherwise. This MUST be the only branch point of its kind in the codebase
  (contracts/web-read-api.md — "there is no route that computes the HTML response differently from the
  JSON one").
- [X] T013 [P] Implement LAN bind-address resolution in `src/civsim_web/net/bind.py`: enumerate host
  network interfaces, select RFC1918 IPv4 private-range addresses plus IPv4 loopback, and refuse a
  wildcard (`0.0.0.0`) bind (research R8).
- [X] T014 Wire the FastAPI application in `src/civsim_web/app.py`: mount `static/`, configure the
  Jinja2 environment against `templates/`, register `GET /healthz` returning this service's liveness
  plus the configured store's `ping()` result, and call T013's resolver at startup, logging every bound
  address. Depends on T012, T013.
- [X] T015 Implement `civsim-web doctor` and `civsim-web serve` CLI entry points in
  `src/civsim_web/cli.py`: `doctor` reports store `ping()` status, Panel Registry load status, and the
  resolved bind address list, matching quickstart.md's expected output shape; `serve` starts T014's
  application. Depends on T006, T007, T013, T014.
- [X] T016 [P] Contract test in `tests/contract/test_panel_registry.py` covering P1–P6: one
  deliberately-broken fixture YAML per rule, asserting each fails to load with an error naming the
  violated rule. Depends on T007.
- [X] T017 [P] Unit test in `tests/unit/test_health_derivation.py` asserting the invariant verbatim:
  "'stalled' only ever set from a `hang_detected`/`unresponsive_detected` event, never from an
  independently computed timestamp gap." Depends on T010.
- [X] T018 [P] Unit test in `tests/unit/test_view_reference.py` asserting round-trip parse/serialize
  for every path shape in T011. Depends on T011.

**Checkpoint**: Foundation ready — user story implementation can begin.

### Foundation notes for Phase 3–6 contributors

Written when T001–T018 landed. Read before starting a user story; three of these
are findings against the design artifacts, not implementation choices.

**What exists to build on**

- `civsim_web.store_client.port` — the read-only `MatchStore` Protocol, plus
  record-shape Protocols (`RunLike`, `RunEventLike`, `SavePointLike`,
  `ScreenCaptureLike`, `TurnCycleRecordLike`). `READ_OPERATIONS` /
  `WRITE_OPERATIONS` are exported as data; `tests/contract/test_read_only_boundary.py`
  AST-scans the whole package against them, so a write call fails CI.
- `civsim_web.store_client.fake.FakeMatchStore` — seeded via constructor or
  `seed_*` helpers, never `write_*`. Record dataclasses for all of 002's
  entities already exist there.
- `civsim_web.registry.loader.load_panel_registry()` → `PanelRegistry` with
  `get(panel_id)` and `panels_for_field(entity, field)`. **Every view-model
  constructor must consult `panels_for_field` rather than reading a store field
  directly** — that is what makes UP-001 structural.
- `civsim_web.viewmodels.base` — `ViewModel` (frozen, `extra='forbid'`),
  `RunSummaryView`, `HealthStatus`, `InterventionInfo`, `PanelRegistryVersion`,
  and `UnavailableField`. Subclass `ViewModel`, and use `UnavailableField`
  rather than omitting a key, for FR-011/FR-025/C1 cases alike.
- `civsim_web.negotiate.respond.respond(request, model, template_name)` — the
  only negotiation branch. Templates receive `view` (the model) and `data` (its
  `model_dump(mode="json")`); rendering `data` generically is what keeps the
  JSON/HTML parity test (T033/T039) honest as models grow.
- Test fixtures live in `tests/web_support/fixtures.py` (this feature's
  counterpart to 002's `tests/fakes/`). `tests/{unit,contract,integration}/conftest.py`
  expose `web_store`, `web_store_factory`, `web_app`, `web_client`, all with
  lazy imports so a break here cannot affect 002's tests in the same directories.

**Findings that affect Phase 3–6 work**

1. **T005's read list is short two operations.** `get_capture` and
   `list_run_events` are published reads on `match-store-port.md` and are
   required by `GET /captures/{id}/image` (T029) and `GET /runs/{id}/events`
   (T028). Both are on the Protocol as implemented.
2. **The port cannot reach `RunConfiguration`, and has no blob fetch.** FR-018's
   seed/civilization/ruleset/model columns live on `RunConfiguration`, which no
   published read resolves from `Run.config_id`; and `get_capture` returns the
   record with `blob_ref`, not bytes, so `GET /captures/{id}/image` has nothing
   to serve from the published port. The first is handled by an optional
   `RunConfigurationReader` capability the store is *probed* for (fields render
   unavailable when absent); the second is unresolved and is T029's problem.
   Both are the same C1 dependency and should be raised with deliverable 3.
3. **`HealthState` carries two values beyond FR-003's seven.** `paused` (002 has
   the lifecycle state; data-model.md SS2's derivation rule does not map it) and
   `unknown` (fail-closed for an unrecognised future state). Templates must
   render both.
4. **A step-scoped panel's reference shape is not in the route table.**
   data-model.md SS12 describes `/steps/{step_index}` being inserted into the
   panel path; `contracts/web-read-api.md`'s resolution table lists only the
   turn- and run-scoped panel shapes. `refs/reference.py` accepts the
   step-scoped shape; **T035/T038 need a matching route** or a `scope: step`
   panel has no resolvable URL.

---

## Phase 3: User Story 1 - Watch a live run without opening the game (Priority: P1) 🎯 MVP

**Goal**: A single landing view shows current turn, current visible state, most recent decision with
its reasoning, health, and a capture beside the structured panels — updating within the 5-second
currency window, with no control offered anywhere (FR-001 – FR-006, FR-026, FR-027, FR-031, FR-034).

**Independent Test**: Start a run, open the interface on a second screen, and follow ten consecutive
turns without ever focusing the game window; verify the observer can state the current turn, the last
action taken, the stated reason for it, and the run's health at any moment.

### Implementation for User Story 1

- [ ] T019 [P] [US1] Author `panels/live.yaml` declarations for the live/glance view (current-turn
  overview fields, agent decision + reasoning, run header, intervention info) with a restated,
  non-empty `parity_basis` for every `category: in_game` entry, and `category: out_of_game_telemetry`
  with `parity_basis: null` for cost/latency fields (panel-registry P1/P2).
- [ ] T020 [P] [US1] Implement `TurnCycleView` and `TurnCompleteness` in
  `src/civsim_web/viewmodels/turn.py` per data-model.md §5. Enforce verbatim: "A `TurnCycleView` for a
  turn number present in `turn_gaps()` is never constructed as if it were a normal turn."
- [ ] T021 [P] [US1] Implement `DecisionStepView` and `ObservationEntryView` in
  `src/civsim_web/viewmodels/step.py` per data-model.md §6. Enforce verbatim: "an
  `ObservationEntry.declaration_id` that does not resolve to a Panel Registry entry is dropped, not
  passed through with a placeholder label."
- [ ] T022 [P] [US1] Implement `CaptureView` in `src/civsim_web/viewmodels/capture.py` per
  data-model.md §7. Enforce the fail-closed rule verbatim: "`available` is computed as
  `screening_status == \"screened_clean\"`, full stop. Any other value ... resolves to
  `available = false`" — including a `screening_status` value this code does not recognize.
- [ ] T023 [P] [US1] Implement `DecisionView` and `ModelCallView` in
  `src/civsim_web/viewmodels/decision.py` per data-model.md §8. Enforce verbatim: "an empty `reasoning`
  renders as an explicit 'no reasoning recorded' label ... a very long `reasoning` is truncated in the
  collapsed panel view with an expand affordance."
- [ ] T024 [P] [US1] Implement `RunEventView` in `src/civsim_web/viewmodels/event.py` per
  data-model.md §9.
- [ ] T025 [US1] Implement `RunDetailView` in `src/civsim_web/viewmodels/run_detail.py` per
  data-model.md §3, assembling `summary`, `current_turn`, `latest_decision`,
  `last_confirmed_current_at`, `intervention_info`, and `recent_events`. A run with zero authoritative
  turns MUST populate `current_turn = None` rather than a placeholder value. Depends on T009, T020,
  T021, T022, T023, T024.
- [ ] T026 [US1] Implement `GET /` and `GET /runs/{run_id}` in `src/civsim_web/routes/live.py`,
  returning `RunDetailView` through T012's negotiation seam. Depends on T014, T025.
- [ ] T027 [US1] Implement `GET /runs/{run_id}/turns/{turn_number}` and
  `GET /runs/{run_id}/turns/{turn_number}/steps/{step_index}` in `src/civsim_web/routes/turns.py`.
  Depends on T020, T021.
- [ ] T028 [US1] Implement `GET /runs/{run_id}/events` in `src/civsim_web/routes/events.py`. Depends
  on T024.
- [ ] T029 [US1] Implement `GET /captures/{capture_id}/image` in `src/civsim_web/routes/captures.py`,
  returning `404` naming `unavailable_reason` whenever `CaptureView.available` is false. Depends on
  T022.
- [ ] T030 [P] [US1] Author `templates/live/landing.html` and `templates/live/run_detail.html`
  rendering `RunDetailView`: current turn, last decision + reasoning, health indicator, capture beside
  structured panels, and intervention info (`run_id`, `lifecycle_status`, last-known-good save/turn) —
  with an explicit empty state when `current_turn` is null, and with no control of any kind rendered
  anywhere on the page (FR-026, FR-027).
- [ ] T031 [P] [US1] Author `static/poll.js`: polls `/runs/{run_id}` with `Accept: application/json`
  every 2 seconds (research R5), updates the DOM in place, refreshes the "last confirmed current at"
  indicator every successful poll, and silently continues after a missed poll with no manual reload
  required.
- [ ] T032 [P] [US1] Author `static/style.css` for the landing and run-detail layout, including a
  visually distinct treatment for the out-of-game "cost & latency" sub-panel versus in-game observation
  panels (data-model.md §8 Validation).
- [ ] T033 [US1] Contract tests in `tests/contract/test_web_read_api.py`: JSON/HTML field parity for
  `GET /`, `GET /runs/{run_id}`, `GET /runs/{run_id}/turns/{turn_number}`, and
  `GET /runs/{run_id}/events`; and the `CaptureView.available` fail-closed matrix across
  `screened_clean`, `withheld`, a missing capture record, and one intentionally-unrecognized
  `screening_status` value. Depends on T026, T027, T028, T029.
- [ ] T034 [US1] Integration tests in `tests/integration/test_live_view.py`: an `advancing_run` case
  (a fake store seeded to add a turn every few seconds; asserts each new turn appears within 5 seconds
  and that stopping advancement plus recording a `hang_detected` event surfaces `stalled` within 60
  seconds); and an `empty_state` case (zero authoritative turns renders an explicit empty state, never
  a blank panel). Depends on T026.

**Checkpoint**: User Story 1 is fully functional and independently testable — this is the MVP.

---

## Phase 4: User Story 2 - Same view for the user and the directing session (Priority: P2)

**Goal**: Every view from User Story 1 becomes an addressable, machine-resolvable reference; a
reference to a superseded turn attempt explains itself instead of silently substituting the current
authoritative turn (FR-007 – FR-009, UP-006).

**Independent Test**: Have the directing session describe the current run state from its own
retrieval, and have the user read the same state off the screen; the two accounts must match panel for
panel, with no element present in one and absent from the other.

### Implementation for User Story 2

- [ ] T035 [US2] Implement `GET /runs/{run_id}/turns/{turn_number}/panels/{panel_id}` in
  `src/civsim_web/routes/panels.py` — the canonical `ViewReference` resolution target for a turn-scoped
  panel. Depends on T011, T020.
- [ ] T036 [US2] Extend `GET /runs/{run_id}/turns/{turn_number}` in `src/civsim_web/routes/turns.py`
  with `?attempt={n}` handling: when the named attempt is not the current authoritative one, return
  that attempt's `TurnCycleView` with `is_authoritative=false` and `superseded_by` set — "rather than
  404 or the current authoritative turn silently substituted" (FR-009). Depends on T027.
- [ ] T037 [P] [US2] Author `panels/shared.yaml` entries for the run-level panels referenced by
  `ViewReference`'s run-scoped shape (`/runs/{run_id}/panels/{panel_id}`).
- [ ] T038 [US2] Implement `GET /runs/{run_id}/panels/{panel_id}` in `src/civsim_web/routes/panels.py`
  for run-scoped panel resolution. Depends on T035, T037.
- [ ] T039 [US2] Contract tests in `tests/contract/test_web_read_api.py`: (a) `json_html_parity` — a
  full matrix across every route registered so far, asserting a field present in the JSON body is
  present in the rendered HTML and vice versa (the SC-004 audit, made mechanical); (b) `ref_resolution`
  — the identical `ViewReference` string, resolved once with no `Accept` header (simulated browser) and
  once with `Accept: application/json` (simulated session), produces identical content before
  serialization (SC-012); (c) `superseded_turn` — a reference to a superseded attempt returns an
  explanatory response per T036, while a reference to a run/turn that never existed 404s as a distinct
  case; (d) `no_secrets` — every response schema registered so far is scanned for credential-shaped
  field names and values, asserting zero matches (FR-030). Depends on T035, T036, T038.

**Checkpoint**: User Stories 1 and 2 both work independently — every US1 view is now also a stable,
auditable reference.

---

## Phase 5: User Story 3 - Replay and inspect a completed run turn by turn (Priority: P3)

**Goal**: Turn-by-turn replay with gaps and superseded attempts marked, step navigation that preserves
panel focus, and a single-run metric trajectory that can be scrubbed to jump to a turn (FR-014 –
FR-017, FR-015's trajectory-selection clause).

**Independent Test**: Take one completed run, open it cold, and answer "what did the agent do on turn
23 and why" plus "when did science output first diverge from the plan" using only the interface.

### Implementation for User Story 3

- [ ] T040 [P] [US3] Author `panels/history.yaml` declarations for replay-specific panels (per-turn
  yields, attempt/gap markers).
- [ ] T041 [P] [US3] Implement `MetricSeriesView` in `src/civsim_web/viewmodels/metrics.py` per
  data-model.md §10. Enforce verbatim: "`points` never includes a turn present in that run's
  `turn_gaps()` as if it were a real value."
- [ ] T042 [US3] Implement `GET /runs/{run_id}/metrics` in `src/civsim_web/routes/metrics.py`, with
  `?series=name,name` narrowing. Depends on T041.
- [ ] T043 [US3] Extend `GET /runs/{run_id}/turns/{turn_number}` in `src/civsim_web/routes/turns.py`
  with step-window pagination per data-model.md §5: "`steps` on the default response is a bounded
  window ... with the full ordered list available by paging. The `step_index` ordering guarantee is
  preserved regardless of pagination — no page may skip an index without marking it." Depends on T036.
- [ ] T044 [P] [US3] Author `templates/history/turn_replay.html`: turn detail with step-forward/
  step-backward and jump-to-turn controls that preserve the current panel focus across navigation, gap/
  incomplete/superseded markers, and an inline single-run SVG metric trajectory with clickable turn
  points (research R4).
- [ ] T045 [P] [US3] Author `static/trajectory.js`: renders the single-run SVG trajectory from
  `MetricSeriesView` JSON and wires point clicks to jump-to-turn navigation (FR-015).
- [ ] T046 [US3] Integration test in `tests/integration/test_replay.py`: a completed-run fixture
  containing a turn-number gap, an abandoned-then-replayed attempt pair (crash/resume), and a
  withheld-capture turn; asserts the gap is marked and the run is flagged unfit for trend comparison,
  the abandoned/authoritative attempt pair both appear with timeline events, and stepping between
  adjacent turns preserves the current panel/focus. Depends on T043, T044.
- [ ] T047 [US3] Contract test in `tests/contract/test_web_read_api.py`, `metric_series_gaps` case:
  `MetricSeriesView.points` never includes a gapped turn as a real value. Depends on T042.

**Checkpoint**: User Stories 1, 2, and 3 all work independently.

---

## Phase 6: User Story 4 - Compare runs and spot trends across them (Priority: P4)

**Goal**: A filterable/sortable run catalog and a multi-run comparison view with incomplete-run
quarantine and divergence-point navigation, none of it dependent on captures (FR-018 – FR-022, FR-035).

**Independent Test**: With several completed runs recorded, select five of them and identify which
reached the highest science output by turn 50 and the turn at which the leader separated from the
rest, using only the interface.

### Implementation for User Story 4

- [ ] T048 [P] [US4] Author `panels/catalog.yaml` declarations for catalog-row and comparison-view
  fields.
- [ ] T049 [US4] Implement the catalog-listing projection in `src/civsim_web/store_client/catalog.py`,
  composing `list_active_runs()` plus per-run `get_run()`/metric reads into the FR-018 projection.
  Mark the module with a code comment noting the dependency risk in plan.md Complexity Tracking C1 —
  `match-store-port.md` does not yet publish a dedicated catalog-listing read — so this module is the
  single place that absorbs the gap and can be swapped for a real listing read without touching routes
  or view models. Depends on T006.
- [ ] T050 [US4] Extend `RunSummaryView` population in `src/civsim_web/viewmodels/base.py` to project
  seed, civilization, ruleset, model, turn count, outcome metrics, completeness status, and start/end
  times from T049's catalog projection. Depends on T049.
- [ ] T051 [US4] Implement `GET /runs` in `src/civsim_web/routes/catalog.py`: server-side pagination,
  and filter/sort by any `RunSummaryView` field via query parameters (research R6). Depends on T049,
  T050.
- [ ] T052 [P] [US4] Implement `ComparisonView` and `DivergencePoint` in
  `src/civsim_web/viewmodels/comparison.py` per data-model.md §11. Enforce verbatim: "this entire model
  is constructed with `CaptureView` nowhere in its type" and "A run in `quarantined_run_ids` still
  appears in `runs` ... but is excluded from `series` and `divergence_points` computation" for any run
  whose `record_completeness_status != complete`.
- [ ] T053 [US4] Implement `GET /compare` in `src/civsim_web/routes/compare.py` with `?runs=` and
  `?metrics=` query parameters, computing `divergence_points` with a ready-to-navigate `refs` entry per
  compared run (FR-022). Depends on T041, T052.
- [ ] T054 [P] [US4] Author `templates/catalog/catalog.html`: filterable/sortable run listing.
- [ ] T055 [P] [US4] Author `templates/catalog/compare.html`: multi-run SVG trajectory comparison with
  clickable divergence points.
- [ ] T056 [US4] Extend `static/trajectory.js` to support multi-series overlay rendering and
  divergence-point click-through to each compared run's matching turn. Depends on T045.
- [ ] T057 [US4] Contract tests in `tests/contract/test_web_read_api.py`: (a)
  `comparison_never_needs_captures` — identical `ComparisonView` output whether every capture in the
  fixture is `screened_clean` or entirely `withheld` (FR-035, SC-016); (b) `quarantine` — an incomplete
  run appears in `runs` but is excluded from `series` and `divergence_points`. Depends on T052, T053.
- [ ] T058 [US4] Integration test in `tests/integration/test_catalog_and_compare.py`: a 50+ run catalog
  fixture exercising filter/sort correctness, and a 5-run comparison where following a divergence
  point's `refs` opens the matching turn in each compared run. Depends on T051, T053.

**Checkpoint**: All four user stories are independently functional.

---

## Phase 7: Polish & Cross-Cutting Concerns

**Purpose**: Release-blocking audits named directly in spec.md's Success Criteria, plus scale
validation and operator-facing wiring.

- [ ] T059 [P] Panel Registry coverage reflection test in `tests/contract/test_panel_registry.py`:
  every field in `specs/002-civ-playing-harness/data-model.md` not explicitly marked out-of-game either
  has a corresponding panel or is confirmed absent from every view model by reflection — a new store
  field defaults to invisible until deliberately registered (contracts/panel-registry.md Conformance
  tests).
- [ ] T060 [P] Performance test in `tests/integration/test_scale.py`: a synthetic fixture of 300+
  turns per run (with late-game turns running into hundreds of steps) and 50+ catalog runs; asserts
  every route exercised in Phases 3–6 reaches a usable response within 2 seconds (SC-008).
- [ ] T061 [P] Schema-evolution contract test in `tests/contract/test_web_read_api.py`,
  `schema_evolution` case: a fixture record predating a field renders that field with an explicit
  "unavailable for this run's recorded schema version" marker, never as zero or a silently omitted
  value (FR-025).
- [ ] T062 Wire `civsim-web doctor`'s full preflight output end-to-end in `src/civsim_web/cli.py`
  against the routes built in Phases 3–6, matching quickstart.md's expected output shape verbatim.
  Depends on T015, T051.
- [ ] T063 [P] Scripted smoke test of quickstart.md Scenarios 1–5 against the `MatchStore` fake in
  `tests/integration/test_quickstart_scenarios.py`.
- [ ] T064 [P] Add a usage section (bind address, `doctor`, `serve`) for `civsim_web` to the project's
  existing top-level documentation, coordinating placement so it does not collide with
  `002-civ-playing-harness`'s own operator documentation.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — start immediately.
- **Foundational (Phase 2)**: Depends on Setup completion. **Blocks all user stories.**
- **User Stories (Phase 3–6)**: All depend on Foundational completion.
  - US1 (P1) has no dependency on any other story and is the MVP.
  - US2 (P2) extends US1's routes (`routes/turns.py`) and view models (`TurnCycleView`) — it is
    additive to US1's files, not a fork of them, so US1 SHOULD be complete before US2 begins even
    though US2 introduces no new user-visible capability US1 lacks a route for.
  - US3 (P3) extends `routes/turns.py` again (step pagination) and is independently testable once US1
    exists; it does not require US2's reference-resolution work to function, though both touch the
    same file.
  - US4 (P4) is the most independent of the four: it adds new routes/view models
    (`store_client/catalog.py`, `routes/catalog.py`, `routes/compare.py`, `viewmodels/comparison.py`)
    that no other story's files depend on, and only reuses `MetricSeriesView` from US3 and
    `RunSummaryView` from Foundational.
- **Polish (Phase 7)**: Depends on all four user stories being complete (T060's scale test and T062's
  end-to-end `doctor` wiring specifically exercise routes from every phase).

### Within Each User Story

- View models before routes; routes before templates/static that consume them; routes and view models
  before the tests that exercise them.
- `routes/turns.py` is touched by US1 (T027), US2 (T036), and US3 (T043) in that order — these three
  tasks MUST be done sequentially regardless of story-level parallelism, since they are successive
  edits to the same file.
- `tests/contract/test_web_read_api.py` accumulates test functions across US1 (T033), US2 (T039), US3
  (T047), US4 (T057), and Polish (T061) — each addition is sequential relative to the others touching
  this file, even though the stories themselves may otherwise proceed in parallel.

### Parallel Opportunities

- All Setup tasks marked [P] (T003, T004) run in parallel once T001 exists.
- Within Foundational, T007–T013 all touch different files with no dependency on each other and can
  run in parallel once T005/T006 exist; T016–T018 (their tests) are likewise parallel once their
  respective subjects exist.
- Within US1, the six view-model tasks (T020–T024) are independent files and can run in parallel; the
  three template/static tasks (T030–T032) are independent files and can run in parallel once the
  routes they render exist.
- **US4 can be staffed in parallel with US2 and US3** once Foundational is done: it shares no file with
  either except `viewmodels/base.py` (T009, already done in Foundational) and `static/trajectory.js`
  (T045 from US3, which T056 extends — this one dependency should be sequenced, or T056 deferred until
  T045 lands).

---

## Parallel Example: User Story 1

```bash
# Launch all US1 view models together (after Foundational is complete):
Task: "Implement TurnCycleView and TurnCompleteness in src/civsim_web/viewmodels/turn.py"
Task: "Implement DecisionStepView and ObservationEntryView in src/civsim_web/viewmodels/step.py"
Task: "Implement CaptureView in src/civsim_web/viewmodels/capture.py"
Task: "Implement DecisionView and ModelCallView in src/civsim_web/viewmodels/decision.py"
Task: "Implement RunEventView in src/civsim_web/viewmodels/event.py"

# Once routes exist, launch template/static work together:
Task: "Author templates/live/landing.html and templates/live/run_detail.html"
Task: "Author static/poll.js"
Task: "Author static/style.css"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup.
2. Complete Phase 2: Foundational — the `MatchStore` seam, Panel Registry, and negotiation mechanism
   every later phase depends on.
3. Complete Phase 3: User Story 1.
4. **STOP and VALIDATE**: run quickstart.md Scenario 1 against the fake store; confirm the Independent
   Test passes without any US2–US4 work in place.

### Incremental Delivery

1. Setup + Foundational → foundation ready.
2. Add User Story 1 → validate independently → this is a demonstrable MVP (a live, read-only, no-
   controls view of a run).
3. Add User Story 2 → validate independently → the same views are now addressable and audited for
   JSON/HTML parity.
4. Add User Story 3 → validate independently → completed runs are now replayable turn by turn.
5. Add User Story 4 → validate independently → the catalog and comparison views complete the spec.
6. Polish → the release-blocking audits (registry coverage, scale, schema evolution) and operator
   wiring close out the feature.

### Parallel Team Strategy

With multiple contributors, after Foundational completes:

- One contributor takes US1 (the MVP path).
- A second can start US4 immediately in parallel — it is the most file-independent of the four stories
  (Dependencies note above).
- US2 and US3 both extend `routes/turns.py`, so they are best sequenced one after the other (or after
  US1) rather than staffed fully in parallel with each other, to avoid two contributors editing the
  same route function at once.

---

## Notes

- [P] tasks touch different files with no dependency on an incomplete task; tasks appending to a
  shared, growing file (`routes/turns.py`, `tests/contract/test_web_read_api.py`,
  `viewmodels/base.py`, `static/trajectory.js`) are deliberately left unmarked and sequenced, even
  across story boundaries, to avoid conflicting edits.
- Every task that names a data-model.md or contracts/ validation rule quotes it verbatim, so the rule
  is not left to implementation-time discretion.
- This feature has no `tests/live` tier (unlike 002): it never touches the game client, only the
  `MatchStore` port, so every test tier here runs against the fake and is CI-runnable with no live
  dependency.
- Commit after each task or logical group; stop at any checkpoint to validate a story independently
  before proceeding to the next.
