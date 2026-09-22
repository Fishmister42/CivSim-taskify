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

- [X] T019 [P] [US1] Author `panels/live.yaml` declarations for the live/glance view (current-turn
  overview fields, agent decision + reasoning, run header, intervention info) with a restated,
  non-empty `parity_basis` for every `category: in_game` entry, and `category: out_of_game_telemetry`
  with `parity_basis: null` for cost/latency fields (panel-registry P1/P2).
- [X] T020 [P] [US1] Implement `TurnCycleView` and `TurnCompleteness` in
  `src/civsim_web/viewmodels/turn.py` per data-model.md §5. Enforce verbatim: "A `TurnCycleView` for a
  turn number present in `turn_gaps()` is never constructed as if it were a normal turn."
- [X] T021 [P] [US1] Implement `DecisionStepView` and `ObservationEntryView` in
  `src/civsim_web/viewmodels/step.py` per data-model.md §6. Enforce verbatim: "an
  `ObservationEntry.declaration_id` that does not resolve to a Panel Registry entry is dropped, not
  passed through with a placeholder label."
- [X] T022 [P] [US1] Implement `CaptureView` in `src/civsim_web/viewmodels/capture.py` per
  data-model.md §7. Enforce the fail-closed rule verbatim: "`available` is computed as
  `screening_status == \"screened_clean\"`, full stop. Any other value ... resolves to
  `available = false`" — including a `screening_status` value this code does not recognize.
- [X] T023 [P] [US1] Implement `DecisionView` and `ModelCallView` in
  `src/civsim_web/viewmodels/decision.py` per data-model.md §8. Enforce verbatim: "an empty `reasoning`
  renders as an explicit 'no reasoning recorded' label ... a very long `reasoning` is truncated in the
  collapsed panel view with an expand affordance."
- [X] T024 [P] [US1] Implement `RunEventView` in `src/civsim_web/viewmodels/event.py` per
  data-model.md §9.
- [X] T025 [US1] Implement `RunDetailView` in `src/civsim_web/viewmodels/run_detail.py` per
  data-model.md §3, assembling `summary`, `current_turn`, `latest_decision`,
  `last_confirmed_current_at`, `intervention_info`, and `recent_events`. A run with zero authoritative
  turns MUST populate `current_turn = None` rather than a placeholder value. Depends on T009, T020,
  T021, T022, T023, T024.
- [X] T026 [US1] Implement `GET /` and `GET /runs/{run_id}` in `src/civsim_web/routes/live.py`,
  returning `RunDetailView` through T012's negotiation seam. Depends on T014, T025.
- [X] T027 [US1] Implement `GET /runs/{run_id}/turns/{turn_number}` and
  `GET /runs/{run_id}/turns/{turn_number}/steps/{step_index}` in `src/civsim_web/routes/turns.py`.
  Depends on T020, T021.
- [X] T028 [US1] Implement `GET /runs/{run_id}/events` in `src/civsim_web/routes/events.py`. Depends
  on T024.
- [X] T029 [US1] Implement `GET /captures/{capture_id}/image` in `src/civsim_web/routes/captures.py`,
  returning `404` naming `unavailable_reason` whenever `CaptureView.available` is false. Depends on
  T022.
- [X] T030 [P] [US1] Author `templates/live/landing.html` and `templates/live/run_detail.html`
  rendering `RunDetailView`: current turn, last decision + reasoning, health indicator, capture beside
  structured panels, and intervention info (`run_id`, `lifecycle_status`, last-known-good save/turn) —
  with an explicit empty state when `current_turn` is null, and with no control of any kind rendered
  anywhere on the page (FR-026, FR-027).
- [X] T031 [P] [US1] Author `static/poll.js`: polls `/runs/{run_id}` with `Accept: application/json`
  every 2 seconds (research R5), updates the DOM in place, refreshes the "last confirmed current at"
  indicator every successful poll, and silently continues after a missed poll with no manual reload
  required.
- [X] T032 [P] [US1] Author `static/style.css` for the landing and run-detail layout, including a
  visually distinct treatment for the out-of-game "cost & latency" sub-panel versus in-game observation
  panels (data-model.md §8 Validation).
- [X] T033 [US1] Contract tests in `tests/contract/test_web_read_api.py`: JSON/HTML field parity for
  `GET /`, `GET /runs/{run_id}`, `GET /runs/{run_id}/turns/{turn_number}`, and
  `GET /runs/{run_id}/events`; and the `CaptureView.available` fail-closed matrix across
  `screened_clean`, `withheld`, a missing capture record, and one intentionally-unrecognized
  `screening_status` value. Depends on T026, T027, T028, T029.
- [X] T034 [US1] Integration tests in `tests/integration/test_live_view.py`: an `advancing_run` case
  (a fake store seeded to add a turn every few seconds; asserts each new turn appears within 5 seconds
  and that stopping advancement plus recording a `hang_detected` event surfaces `stalled` within 60
  seconds); and an `empty_state` case (zero authoritative turns renders an explicit empty state, never
  a blank panel). Depends on T026.

**Checkpoint**: User Story 1 is fully functional and independently testable — this is the MVP.

### US1 notes for Phase 4-6 contributors

Written when T019–T034 landed. Read alongside the Foundation notes above.

**What US1 added that later stories build on**

- `routes/common.py` — `WebError`/`ErrorView` (raise this, **not** FastAPI's
  `HTTPException`: the latter bypasses the negotiation seam and would answer a
  browser with raw JSON), `load_run_context` (the read every run-scoped route
  starts with, returning run + events + provenance), `require_store` (the
  contract's blanket 503).
- `routes/__init__.py` — `ROUTER_MODULES`. A new route is **one appended line
  here**; `app.py` needs no further edit by any story.
- `store_client/reads.py` — every store read composition, and the single place
  the two optional capabilities are probed for. `highest_recorded_turn` and
  `latest_authoritative_turn` live here.
- `viewmodels/gate.py` — `GatedReader`. **Every view-model constructor reads
  store fields through this**, which is what makes UP-001 structural; it also
  accumulates the `UnavailableField` entries a model carries.
- `registry/lookup.py` — `panel_for_observation_entry` / `panel_for_action`,
  the `[declaration_id=…]` selector resolution behind SS6's drop rule.
- `viewmodels/provenance.py` — `Provenance` (invariant V10's four strings) for
  responses without a `RunSummaryView` to carry catalog versions.
- `templates/_macros.html` — `node()` walks a model's serialized form and emits
  `data-field="<json.path>"` per leaf. **A field added to a view model appears
  on the page with no template edit**, which is what keeps T039's parity matrix
  passing as models grow. Add bespoke markup only for values needing more than
  a label (images, links, expanders).

**Extending the two shared files**

- `routes/turns.py` — its module docstring names exactly where T036 (`?attempt=`)
  and T043 (step-window pagination) hook in. Both are query-parameter additions
  forwarded to `build_turn_view`, which already accepts `attempt`,
  `superseded_by`, `step_offset`, and `step_limit`. `TurnCycleView` already
  carries `is_authoritative`, `superseded_by`, and `StepWindow` (including
  `skipped_step_indices` for SS5's "no page may skip an index without marking
  it"), so neither task needs a view-model change.
- `tests/contract/test_web_read_api.py` — `ROUTES` is the parity matrix. T039's
  "full matrix across every route registered so far" is discharged by appending
  a `Route`, not by writing a second parity test. Add cases under your story's
  banner section.

**Findings recorded against the design artifacts**

1. **`GET /captures/{id}/image` is implementable but leans on an optional
   capability.** The published port resolves no `blob_ref` to bytes, so
   `store_client/port.py` now declares a probed `CaptureBlobReader` alongside
   `RunConfigurationReader`. A store without it gets a `503` naming the port
   gap, distinct from the `404` a non-`screened_clean` capture gets. Reading
   `blob_ref` off the filesystem was rejected — it would reach around the port
   into 002's storage layout. **Raise with deliverable 3 alongside C1.**
2. **`data-model.md` SS7's `unavailable_reason` enum has no value for the case
   the same paragraph requires.** An unrecognized `screening_status` must fail
   closed, but `withheld`/`capture_failed`/`never_captured`/`missing_record`
   all assert something nobody recorded. `unrecognized_status` was added, in
   the same spirit as the foundation's `HealthState.unknown`.
3. **No published read answers "what turn is this run on".** FR-001's first
   question. `highest_recorded_turn` composes `list_save_points` (Principle IV
   guarantees a quicksave per turn) with a bounded `get_turn_cycle` probe. A
   single indexed read would replace both — same conversation as C1.
4. **`GET /runs/{id}/events` returns a wrapper, not a bare list.** The contract
   says `list[RunEventView]`, but a bare list can carry neither the pagination
   the same line asks for nor V10's provenance stamp. `RunEventPage` wraps it.
5. **`DecisionView.action_label` has no registry-sourced label for most
   actions.** SS8 wants the label from the registry, but the panel schema has
   no per-action label field. `live.yaml` demonstrates the mechanism with
   `[declaration_id=…]` selector panels for four actions; everything else falls
   back to the raw declaration id and sets
   `action_label_is_declaration_id: true` rather than inventing one.
6. **`/runs/{id}/turns/{n}/steps/{i}` is a top-level response V10's enumeration
   omits.** SS13 lists `RunDetailView`, `TurnCycleView`, `ComparisonView`, and
   catalog listings. `DecisionStepView` carries no provenance, per that list.
7. **`panels/VERSION` was not bumped.** `live.yaml` went from `[]` to 29
   declarations, all `introduced_in_version: "1"`. No `panels/VERSION.lock`
   exists, so version 1 is still under authorship and rule P6's freeze check is
   not in force. US2/US3/US4 should likewise add to version 1 until someone
   writes the lock file; after that, adding a panel means bumping VERSION.

**One unnumbered file was added**: `tests/contract/test_web_parity_boundary.py`
— the counterpart to `test_read_only_boundary.py` in the other direction. It
asserts no module in `civsim_web` imports `civsim_harness` or holds an outbound
HTTP client (so plan.md's "no code path back into 002's agent-context
assembly" is checked rather than only claimed), plus quickstart Scenario 5's
"no harness telemetry declared `in_game`".

---

## Phase 4: User Story 2 - Same view for the user and the directing session (Priority: P2)

**Goal**: Every view from User Story 1 becomes an addressable, machine-resolvable reference; a
reference to a superseded turn attempt explains itself instead of silently substituting the current
authoritative turn (FR-007 – FR-009, UP-006).

**Independent Test**: Have the directing session describe the current run state from its own
retrieval, and have the user read the same state off the screen; the two accounts must match panel for
panel, with no element present in one and absent from the other.

### Implementation for User Story 2

- [X] T035 [US2] Implement `GET /runs/{run_id}/turns/{turn_number}/panels/{panel_id}` in
  `src/civsim_web/routes/panels.py` — the canonical `ViewReference` resolution target for a turn-scoped
  panel. Depends on T011, T020.
- [X] T036 [US2] Extend `GET /runs/{run_id}/turns/{turn_number}` in `src/civsim_web/routes/turns.py`
  with `?attempt={n}` handling: when the named attempt is not the current authoritative one, return
  that attempt's `TurnCycleView` with `is_authoritative=false` and `superseded_by` set — "rather than
  404 or the current authoritative turn silently substituted" (FR-009). Depends on T027.
- [X] T037 [P] [US2] Author `panels/shared.yaml` entries for the run-level panels referenced by
  `ViewReference`'s run-scoped shape (`/runs/{run_id}/panels/{panel_id}`).
- [X] T038 [US2] Implement `GET /runs/{run_id}/panels/{panel_id}` in `src/civsim_web/routes/panels.py`
  for run-scoped panel resolution. Depends on T035, T037.
- [X] T039 [US2] Contract tests in `tests/contract/test_web_read_api.py`: (a) `json_html_parity` — a
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

### US2 notes for Phase 5-7 contributors

Written when T035–T039 landed. Read alongside the Foundation and US1 notes above.

**What US2 added that later stories build on**

- `routes/panels.py` — three routes, not two. The contract's table names the
  run- and turn-scoped panel shapes; the **step-scoped** shape
  (`/runs/{id}/turns/{n}/steps/{i}/panels/{panel_id}`) is also routed, because
  `refs/reference.py` parses it and twenty of the registry's shipped panels are
  `scope: step` — Foundation note 4's open question, now closed.
- `viewmodels/panel.py` — `PanelView`, and the projection that makes a panel
  response a *slice of an already-built enclosing view* rather than a second
  store read. **A new route that should be panel-addressable needs an
  `ENTITY_PATHS` entry, nothing more**: the map says where each 002 entity sits
  inside a view, `source_fields` does the rest, and `FIELD_ALIASES` records the
  nine places a view model renamed a store field. Adding a *panel* needs no
  change here at all.
- `store_client/reads.turn_attempt` + `AttemptLookup` — attempt-addressed reads
  and the honest statement of what the published port can reach.
- `viewmodels/turn.build_turn_cycle_view(..., allow_gap=True)` — the only way to
  build a view for a gapped turn. US3's replay work (T043/T046) will meet gapped
  turns: the default refusal is still correct for a bare turn reference, and
  `allow_gap` exists for a reference that names an attempt.
- Fixtures: `make_store(replayed_turns=[n])` seeds an abandoned attempt 0 plus an
  authoritative attempt 1 — the crash/resume pair T046 needs. `attempt_reader=False`
  drops the fake's optional capability so the published-port fallback is exercised.

**For T043 (US3), which extends `routes/turns.py` next**

`get_turn` now takes `attempt: int | None`. T043's `?step_offset=` / `?step_limit=`
are **additions to that signature only** — `build_turn_view` already accepts and
forwards both, and `_load_turn` / `_load_attempt` need no change. Do not route the
step window through `build_step_view`: it calls `build_turn_view` with the full
step list on purpose, so a step opened directly is never missing because of
someone else's page size. The module docstring's failure table now lists five
shapes; add the window's own (a `step_offset` past the end) there rather than
inventing a second convention.

**Findings recorded against the design artifacts**

1. **`?attempt={n}` is not implementable against the published port.** The route
   contract says it "selects a specific (including abandoned) attempt", and
   `match-store-port.md` has no read that addresses an attempt by index:
   `get_turn_cycle` takes a boolean, and its flag-off form is documented only as
   "abandoned attempts remain retrievable" — every implementation returns the
   *most recent*. So the two published reads address exactly two attempts of any
   turn, and the one FR-009 is actually about (attempt 0 abandoned, attempt 1
   authoritative) is not one of them. A probed optional `TurnAttemptReader`
   capability was added, the fourth of its kind. **Raise with deliverable 3
   alongside C1, C-captures and C-catalog** — four probed capabilities is no
   longer a workaround, it is an unpublished half of the port.
2. **`TurnCompleteness.is_gap` was authored but unreachable.** The field existed
   on the model from T020 and the builder hard-coded it `False`, because the same
   builder refused to construct a view for a gapped turn at all. T036's
   `allow_gap` is what reaches it: a turn whose *every* attempt was abandoned
   (spec Edge Cases, "a branch was abandoned or rolled back") is now answerable
   by a reference that names the attempt, marked as the gap it belongs to.
3. **The registry's run-scoped panels were declared in the wrong file.** T019
   put `run.header`, `run.configuration`, `run.intervention` and `run.timeline`
   in `live.yaml`; `contracts/panel-registry.md` and plan.md both name
   `shared.yaml` for exactly those ("run header, intervention info, event
   timeline"), and P4's single namespace makes re-declaring them impossible.
   T037 **relocated** them. The move is provably inert — the loader excludes
   `declared_in` from P6's hash, so neither the declarations nor the registry's
   `content_hash` changed — and `panels/VERSION` was not bumped.
4. **T039(a)'s "every route registered so far" is ambiguous under parallel
   staffing.** US4 landed `/runs` and `/compare` concurrently with this phase;
   both are in `ROUTES` now (appended by US4's own work). T047 and T057 should
   keep appending rather than reading "so far" as a snapshot of their own phase.
5. **`ViewReference` has no attempt component.** `superseded_by` is an attempt
   *index*, so a reference to a superseded attempt cannot itself be re-serialized
   as a reference — the `?attempt=` query parameter carries it, and the canonical
   `reference` field on the response stays the bare turn path. That is defensible
   (the contract makes `?attempt=` a query, not a path segment) but it means
   data-model.md SS12's "the reference *is* the URL path" is true of five of the
   six shapes and not of an attempt-qualified one. Recorded, not resolved.
6. **T039(b) asks for the wrong header.** It describes the browser side of the
   SC-012 check as "resolved once with **no** `Accept` header (simulated
   browser)", but the negotiation seam's documented rule 5 answers a request
   with no `Accept` in **JSON** — deliberately, since a client expressing no
   preference is far likelier to be a script than a browser, and browsers always
   send an `Accept` naming `text/html`. Taking the task literally would have
   compared JSON to JSON and proved nothing. The test sends `Accept: text/html`
   for the browser side.

---

## Phase 5: User Story 3 - Replay and inspect a completed run turn by turn (Priority: P3)

**Goal**: Turn-by-turn replay with gaps and superseded attempts marked, step navigation that preserves
panel focus, and a single-run metric trajectory that can be scrubbed to jump to a turn (FR-014 –
FR-017, FR-015's trajectory-selection clause).

**Independent Test**: Take one completed run, open it cold, and answer "what did the agent do on turn
23 and why" plus "when did science output first diverge from the plan" using only the interface.

### Implementation for User Story 3

- [X] T040 [P] [US3] Author `panels/history.yaml` declarations for replay-specific panels (per-turn
  yields, attempt/gap markers).
- [X] T041 [P] [US3] Implement `MetricSeriesView` in `src/civsim_web/viewmodels/metrics.py` per
  data-model.md §10. Enforce verbatim: "`points` never includes a turn present in that run's
  `turn_gaps()` as if it were a real value."
- [X] T042 [US3] Implement `GET /runs/{run_id}/metrics` in `src/civsim_web/routes/metrics.py`, with
  `?series=name,name` narrowing. Depends on T041.
- [X] T043 [US3] Extend `GET /runs/{run_id}/turns/{turn_number}` in `src/civsim_web/routes/turns.py`
  with step-window pagination per data-model.md §5: "`steps` on the default response is a bounded
  window ... with the full ordered list available by paging. The `step_index` ordering guarantee is
  preserved regardless of pagination — no page may skip an index without marking it." Depends on T036.
- [X] T044 [P] [US3] Author `templates/history/turn_replay.html`: turn detail with step-forward/
  step-backward and jump-to-turn controls that preserve the current panel focus across navigation, gap/
  incomplete/superseded markers, and an inline single-run SVG metric trajectory with clickable turn
  points (research R4).
- [X] T045 [P] [US3] Author `static/trajectory.js`: renders the single-run SVG trajectory from
  `MetricSeriesView` JSON and wires point clicks to jump-to-turn navigation (FR-015).
- [X] T046 [US3] Integration test in `tests/integration/test_replay.py`: a completed-run fixture
  containing a turn-number gap, an abandoned-then-replayed attempt pair (crash/resume), and a
  withheld-capture turn; asserts the gap is marked and the run is flagged unfit for trend comparison,
  the abandoned/authoritative attempt pair both appear with timeline events, and stepping between
  adjacent turns preserves the current panel/focus. Depends on T043, T044.
- [X] T047 [US3] Contract test in `tests/contract/test_web_read_api.py`, `metric_series_gaps` case:
  `MetricSeriesView.points` never includes a gapped turn as a real value. Depends on T042.

**Checkpoint**: User Stories 1, 2, and 3 all work independently.

### US3 notes for Phase 7 contributors

Written when T040–T047 landed, together with **T056** (a Phase 6 task deferred
here because it extends `static/trajectory.js`, which T045 creates). Read
alongside the Foundation, US1, US2 and US4 notes.

**T041 was a review, and `viewmodels/metrics.py` passed it.** What US4 wrote
satisfies data-model.md §10 and T041's verbatim rule, and enforces the rule in a
`@model_validator` rather than in the caller — a series that carries a gapped
turn as a value fails construction. Nothing was reimplemented. Three things were
*added* on top:

- **`MetricAxis` moved from `viewmodels/comparison.py` to `viewmodels/metrics.py`**
  (comparison re-exports it, so `from ...comparison import MetricAxis` still
  works). The single-run trajectory and the multi-run overlay now describe their
  axes with one type, and `static/trajectory.js` renders both pages from the
  same `axes[metric]` shape. Two axis contracts would have been a Principle VI
  drift in visual form.
- **`MetricSeriesView.segments` / `.break_turns`** — see below. They are
  properties, not fields: a serialized `segments` would duplicate `points` in
  every body and give the two representations a way to disagree.
- **`MetricSeriesPage`**, the `GET /runs/{id}/metrics` response.

**The one line worth reading twice.** data-model.md §10 permits a gapped turn to
be omitted from `points` *only because the gap stays visible* — "a break in the
line". The server already omits the turn, so **a renderer that joins `points`
end to end draws a straight line across the gap**, which is the interpolation
the same paragraph forbids, arrived at by not thinking about it. `segments`
splits wherever two consecutive points are not on consecutive turns. It is
deliberately the fail-closed form: it consults neither `gapped_turns` nor
`missing_turns`, so a hole of a third kind still breaks the line.

`templates/catalog/compare.html` **was drawing one polyline per series and
therefore joining across holes** when US3 arrived. Quarantine keeps a *gapped*
run out of `series` entirely, so `gapped_turns` can never be non-empty there and
no Principle III violation had shipped — but `missing_turns` (a turn that
recorded no value for *this* metric) does reach that chart, and the line joined
across it. Fixed in place: one polyline per segment, plus a drawn break marker.

**What US3 added that Phase 7 builds on**

- `routes/metrics.py` (`GET /runs/{run_id}/metrics`, `?series=` narrowing),
  registered in `ROUTER_MODULES`, in the `ROUTES` parity matrix, template
  `templates/history/metrics.html`.
- `templates/history/turn_replay.html` **replaced `templates/turns/turn.html`**
  as the turn route's template (the old file is deleted). Keeping two templates
  for one route would have let the turn a user reads and the turn a user replays
  drift apart.
- `TurnCycleView.focus_panel_id` and `?focus=` on the turn route — FR-015's
  "preserve the current panel focus across navigation", carried in the URL so it
  survives a plain `<a href>` with no client-side state, and so the directing
  session resolving the identical reference sees the identical page. An
  unregistered `panel_id` is a `400 unknown_focus_panel`, not a silent no-op.
- `_macros.html`: `turn_panel(path, turn, focus=none)` and
  `step_panel(path, step, focus=none)` — both default to `none`, so US1's
  callers are unchanged. The `focused` class is applied server-side.
- `?step_offset=` / `?step_limit=` on the turn route, plus a sixth failure
  `kind`, `step_window_out_of_range`.
- Fixtures: `make_store(gap_turns=[...])` omits a turn's records entirely so the
  fake's own `turn_gaps()` *computes* the gap (distinct from the `turn_gaps=`
  override, which asserts one the records do not show), and
  `make_store(withheld_capture_turns=[...])` withholds named turns' captures
  only.

**Findings recorded against the design artifacts**

1. **`GET /runs/{id}/metrics` returns a wrapper, not a bare list** — the third
   instance of the same contract gap US1 recorded for `GET /runs/{id}/events`
   (its finding 4) and US4 for `GET /runs` (its finding 5). A bare
   `list[MetricSeriesView]` can carry neither the axes a chart must be drawn to,
   nor the `?series=` narrowing it was asked for, nor invariant V10's provenance
   stamp. **Three routes now differ from the contract's route table in the same
   way; the table should say so.**
2. **An unrecognised `?series=` name is *not* a 400**, unlike `GET /runs`'s
   unknown filter field. There the field set is closed (whatever
   `RunSummaryView` carries), so a name outside it is provably a client error.
   Here spec Assumptions make the metric set open-ended, so a name this run has
   no values under is a fact about the run. Reported as `unrecorded_series`
   rather than silently drawing an empty chart that would read as a score of
   zero. The two routes therefore answer an unknown name differently, on
   purpose.
3. **T044's "inline single-run SVG metric trajectory" is a link plus an
   enhancement on the turn page, not a rendered chart in the turn response.**
   Building one costs `yields_by_turn`, which is one `get_turn_cycle` per turn
   (plan.md C1), and paying that on *every* turn page would put SC-008's
   two-second budget at risk on a 300-turn run. The figure ships as a real
   `<a>` to `/runs/{id}/metrics`, whose own chart is fully server-rendered with
   a clickable point per turn; `trajectory.js` inlines that same chart when
   scripting is available. Both readers reach identical content at an identical
   URL, so Principle VI holds — but **the task as written asks for something
   that is either a performance problem or a second composition, and the
   artifacts should pick one.** T060's scale test will meet this directly.
4. **`static/trajectory.js` has no executable test, and this project has no
   place to put one.** plan.md's Primary Dependencies decline a bundler and an
   npm dependency tree (research R2), so adding a JS runner to assert one
   function would introduce the second ecosystem R2 rejected. The behavioural
   guard therefore lives where the load-bearing chart lives — on the server, in
   `tests/integration/test_replay.py::test_a_gapped_turn_is_a_break_in_the_trajectory_not_a_join`
   and the contract suite's `metric_series_gaps` cases, both of which fail if a
   series broken by a gap is drawn as one joined polyline. Two source-level
   guards in `tests/contract/test_web_parity_boundary.py` cover the JS copy of
   the rule (that a polyline is only ever built from a segment, and that the
   script draws from `series` and reads `runs` only for the palette index).
   **This is the honest state: the JS is guarded, not tested.**
5. **The step window's out-of-range case had no convention to follow.** §5 says
   only that "no page may skip an index without marking it". A `step_offset`
   past the last step is answered `404 step_window_out_of_range` rather than as
   an empty page, on the same reasoning: a page of no steps for a turn that has
   steps is indistinguishable from a turn whose steps were never recorded.
   `?step_offset=0` on a genuinely step-less turn is *not* that error.
6. **`panels/VERSION` is still not bumped and `VERSION.lock` still does not
   exist**, per the standing decision. `history.yaml` went from `[]` to three
   declarations, all `introduced_in_version: "1"`; the registry is now **37
   panels**. With Phase 5 closed, every story's declarations exist and **the set
   is stable enough to freeze** — see the Phase 7 note under T059/T064 below.

**Two unnumbered files were added**: `src/civsim_web/templates/history/metrics.html`
(the `GET /runs/{id}/metrics` page — the server-rendered chart T045 enhances)
and `src/civsim_web/routes/metrics.py`'s template pair. One file was **deleted**:
`src/civsim_web/templates/turns/turn.html`, replaced by `history/turn_replay.html`.

---

## Phase 6: User Story 4 - Compare runs and spot trends across them (Priority: P4)

**Goal**: A filterable/sortable run catalog and a multi-run comparison view with incomplete-run
quarantine and divergence-point navigation, none of it dependent on captures (FR-018 – FR-022, FR-035).

**Independent Test**: With several completed runs recorded, select five of them and identify which
reached the highest science output by turn 50 and the turn at which the leader separated from the
rest, using only the interface.

### Implementation for User Story 4

- [X] T048 [P] [US4] Author `panels/catalog.yaml` declarations for catalog-row and comparison-view
  fields.
- [X] T049 [US4] Implement the catalog-listing projection in `src/civsim_web/store_client/catalog.py`,
  composing `list_active_runs()` plus per-run `get_run()`/metric reads into the FR-018 projection.
  Mark the module with a code comment noting the dependency risk in plan.md Complexity Tracking C1 —
  `match-store-port.md` does not yet publish a dedicated catalog-listing read — so this module is the
  single place that absorbs the gap and can be swapped for a real listing read without touching routes
  or view models. Depends on T006.
- [X] T050 [US4] Extend `RunSummaryView` population in `src/civsim_web/viewmodels/base.py` to project
  seed, civilization, ruleset, model, turn count, outcome metrics, completeness status, and start/end
  times from T049's catalog projection. Depends on T049.
- [X] T051 [US4] Implement `GET /runs` in `src/civsim_web/routes/catalog.py`: server-side pagination,
  and filter/sort by any `RunSummaryView` field via query parameters (research R6). Depends on T049,
  T050.
- [X] T052 [P] [US4] Implement `ComparisonView` and `DivergencePoint` in
  `src/civsim_web/viewmodels/comparison.py` per data-model.md §11. Enforce verbatim: "this entire model
  is constructed with `CaptureView` nowhere in its type" and "A run in `quarantined_run_ids` still
  appears in `runs` ... but is excluded from `series` and `divergence_points` computation" for any run
  whose `record_completeness_status != complete`.
- [X] T053 [US4] Implement `GET /compare` in `src/civsim_web/routes/compare.py` with `?runs=` and
  `?metrics=` query parameters, computing `divergence_points` with a ready-to-navigate `refs` entry per
  compared run (FR-022). Depends on T041, T052.
- [X] T054 [P] [US4] Author `templates/catalog/catalog.html`: filterable/sortable run listing.
- [X] T055 [P] [US4] Author `templates/catalog/compare.html`: multi-run SVG trajectory comparison with
  clickable divergence points.
- [X] T056 [US4] Extend `static/trajectory.js` to support multi-series overlay rendering and
  divergence-point click-through to each compared run's matching turn. Depends on T045.
  **Closed by the US3 contributor alongside T045**, which created the file. See the
  US3 notes in Phase 5 for what changed, including the fix to
  `templates/catalog/compare.html`'s polyline, which was joining across
  `missing_turns`.
- [X] T057 [US4] Contract tests in `tests/contract/test_web_read_api.py`: (a)
  `comparison_never_needs_captures` — identical `ComparisonView` output whether every capture in the
  fixture is `screened_clean` or entirely `withheld` (FR-035, SC-016); (b) `quarantine` — an incomplete
  run appears in `runs` but is excluded from `series` and `divergence_points`. Depends on T052, T053.
- [X] T058 [US4] Integration test in `tests/integration/test_catalog_and_compare.py`: a 50+ run catalog
  fixture exercising filter/sort correctness, and a 5-run comparison where following a divergence
  point's `refs` opens the matching turn in each compared run. Depends on T051, T053.

**Checkpoint**: All four user stories are independently functional.

### US4 notes for Phase 5 and Phase 7 contributors

Written when T048–T055, T057 and T058 landed. **T056 was left open here and was
closed in Phase 5** with T045, which created the file it extends — see the US3
notes above. Read alongside the Foundation, US1 and US2 notes.

**What US4 added that later work builds on**

- `store_client/catalog.py` — the FR-018 projection, and **the single place
  plan.md C1 is absorbed**. `list_catalog_rows()` returns `CatalogRow` records
  (store records plus already-read numbers, never view models), and
  `CatalogListing.listing_is_partial` says out loud when the store could only
  enumerate *active* runs. When deliverable 3 publishes an indexed catalog read,
  this file changes and nothing above it does.
- `viewmodels/base.py` — `TrendEligibility` and `derive_trend_eligibility()`
  (T050), plus `RunSummaryView.trend_eligibility`. **The default is ineligible,
  not-assessed**; `build_run_summary(..., assess_trend_eligibility=True,
  gapped_turns=...)` is the only way to get an eligible verdict, and there is no
  argument combination that produces one without the `turn_gaps()` read.
- `viewmodels/comparison.py` — `ComparisonView`, `DivergencePoint`,
  `ComparisonBasis`, `MetricAxis`. The quarantine filter lives in
  `build_comparison_view()` and **the model's own validator re-checks its
  outcome**, so deleting the filter raises rather than silently averaging.
- `viewmodels/catalog.py` — `CatalogListingView` plus the filter/sort machinery,
  which works over the **serialized** view models. "Filterable and sortable by
  any `RunSummaryView` field" therefore includes nested ones
  (`health.state`, `trend_eligibility.eligible`, `outcome_metrics.science_output`)
  with no allow-list to fall out of date, and a field the Panel Registry never
  permitted onto the model cannot be filtered on because it is not there.
- `routes/catalog.py` (`GET /runs`) and `routes/compare.py` (`GET /compare`).
  Both registered in `ROUTER_MODULES`; both appear in the `ROUTES` parity matrix.
- `tests/web_support/fixtures.py` — `make_catalog_store(...)` (multi-run,
  shaped metric trajectories, per-run completeness and gap control) and
  `published_port_only(store)`, which hides the three optional capabilities so a
  test can exercise the store the *published* contract actually promises.

**How a gapped run is kept out of trending (Principle III)**

Three layers, in the order a value would have to pass through them:

1. `store_client/catalog.py` reads `turn_gaps(run_id)` for **every** row, always,
   not optionally.
2. `derive_trend_eligibility()` fails closed on any of: a
   `record_completeness_status` that is not `complete` (read verbatim, never
   re-derived — invariant V5); a non-empty `turn_gaps()`; a completeness value
   this code does not recognise; or the gap read not having happened at all.
3. `build_comparison_view()` excludes ineligible runs from `series`, `axes` and
   `divergence_points` while keeping them in `runs` — and `ComparisonView`'s
   `@model_validator` then **refuses to construct** a model in which a
   quarantined run has a series, an axis, a leadership claim, or a `refs` entry,
   *and* refuses one in which a quarantined run has been hidden from `runs`
   instead of marked.

Layer 3 is what makes this survive a careless edit. Deleting the filter in
`build_comparison_view` does not produce a quietly-wrong chart; it produces a
`ValidationError` naming Principle III.

**Findings recorded against the design artifacts**

1. **T053's stated dependency on T041 crosses a story boundary that tasks.md's
   own parallelism note denies.** "Parallel Opportunities" says US4 "shares no
   file with either [US2 or US3] except `viewmodels/base.py` and
   `static/trajectory.js`" — but T053 depends on T041, and
   `ComparisonView.series` is typed `dict[str, list[MetricSeriesView]]` by
   data-model.md §11, so US4 cannot be built without T041's file.
   **`src/civsim_web/viewmodels/metrics.py` was therefore written by US4**, to
   data-model.md §10 and T041's verbatim rule and nothing more. **T041 is left
   unchecked**: what remains of it is a review, plus whatever US3 needs beyond
   `MetricSeriesView`/`MetricPoint`/`build_metric_series`. T042 (`GET
   /runs/{id}/metrics` with `?series=` narrowing) is untouched and still US3's.
   T047's `metric_series_gaps` case is also still US3's, though US4's
   `test_a_metric_series_never_carries_a_gapped_turn_as_a_value` already covers
   the rule at the model level.
2. **The port publishes no run-catalog listing — the third instance of C1.**
   `list_active_runs` is documented for *active* runs; nothing enumerates
   terminal ones. An optional `RunCatalogReader` capability is probed for
   (`store_client/port.py`), and a store without it gets
   `listing_is_partial: true` with the reason on the response. A partial catalog
   that looked complete is the one failure mode here capable of quietly
   truncating the population a trend is drawn from. **Raise with deliverable 3
   alongside C1.**
3. **The port publishes no metric-series read — the fourth instance of C1.**
   The per-turn numbers FR-020 wants live on `TurnCycle.yields`, so a series
   costs one `get_turn_cycle` per turn (`store_client/catalog.yields_by_turn`,
   bounded by `METRIC_TURN_LIMIT`). T060's scale test should be read as a check
   on this composition specifically.
4. **FR-021 and Principle III do not say the same thing, and the difference
   matters.** FR-021 quarantines on `record_completeness_status != complete`;
   the constitution quarantines on the turn-by-turn record *having gaps*.
   `turn_gaps()` is a separately published read of exactly that, so a store can
   answer the two questions differently. This feature fails closed on either,
   which is stricter than FR-021 as written. **The spec should say so, or say
   why not.**
5. **`GET /runs` returns a wrapper, not a bare list** — the same contract gap
   US1 recorded for `GET /runs/{id}/events` (its finding 4). The route table
   says `list[RunSummaryView]`, but a bare list can carry neither the pagination
   the same line asks for nor the provenance stamp data-model.md §13 explicitly
   requires of "catalog listings". `CatalogListingView` wraps it.
6. **`DivergencePoint` has no field for *why* a turn is a divergence point.**
   data-model.md §11's prose names two distinct causes — "turns where the
   leading run changes, or values separate beyond a threshold" — and its table
   has no discriminator between them. A `kind` field (`leader_change` |
   `separation`) was added; a user navigating to a point needs to know which
   they are looking at.
7. **Nothing in the artifacts states the separation threshold.** §11 says
   "beyond a threshold" and names no number. `SEPARATION_RATIO = 0.25` (relative
   to the leading value, because spec Assumptions make the metric set
   open-ended) is this implementation's choice and is **a product decision
   nobody has actually made**.
8. **Principle IV has no requirement behind it in spec.md.** FR-018–FR-022 never
   require the comparison view to say that the runs being compared differ in
   seed, civilization, ruleset, or model — yet the constitution makes a
   comparison across differing starting conditions meaningless. `ComparisonBasis`
   exists to satisfy the *constitution*, not a requirement, and reports
   `unverifiable` (not `uniform`) for dimensions the port cannot reach. **The
   spec should grow a requirement for this.**
9. **`panels/VERSION` is still not bumped, and Phase 6 is now the moment to
   freeze it.** `catalog.yaml` went from `[]` to five declarations, all
   `introduced_in_version: "1"`, per the standing decision that the panel set
   stays unfrozen while the stories are authored. With US4 complete, no
   `panels/VERSION.lock` exists, so rule **P6's immutability check is still not
   in force**. Whoever closes Phase 6 should write the lock file; after that,
   adding a panel means bumping `VERSION`.

**Two unnumbered files were added**: `src/civsim_web/viewmodels/catalog.py`
(`CatalogListingView` — see finding 5) and `src/civsim_web/viewmodels/metrics.py`
(T041's file — see finding 1).

**T056, for whoever picks it up cold** — *done; kept as the record of what it
required.* The brief below was accurate except for its last clause: one thing
about the comparison view did have to change (`compare.html` drew one polyline
per series, which joins across a hole; it now draws one per segment). See US3
note on `segments`.

- **The page renders a complete multi-series SVG with no JavaScript at all.**
  `templates/catalog/compare.html` draws one `<figure class="trajectory"
  data-metric="…">` per metric, containing `<svg class="trajectory-svg"
  viewBox="0 0 720 260">` with one `<polyline class="series"
  data-series-run="{run_id}">` per compared run and one `<a class="divergence"
  href="{ref}" data-divergence-turn="…" data-divergence-kind="…">` per
  divergence point. **T056 must enhance this, not replace it** — a chart that
  only exists once a script runs is a chart the directing session cannot see,
  which is the asymmetry Principle VI forbids.
- **The data it should render from** is `GET /compare?runs=…&metrics=…` with
  `Accept: application/json` — the identical URL the page was served from.
  `series` is `{metric_name: [MetricSeriesView, …]}`; each series carries
  `run_id`, ordered `points[{turn, value}]`, `gapped_turns`, and
  `missing_turns`. **`gapped_turns` must render as a visible break in the line,
  never as a join between the points either side of it** (data-model.md §10,
  FR-025) — the server has already omitted those turns from `points`, so a naive
  line join would silently bridge the gap.
- **The common axes are already computed**: `axes[metric_name]` carries
  `min_turn`, `max_turn`, `min_value`, `max_value`, `run_ids`. Use them rather
  than recomputing, so the script's chart and the JSON's description of it
  cannot disagree.
- **Divergence click-through** is `divergence_points[i].refs`, a
  `{run_id: path}` map of canonical view-reference paths
  (`/runs/{run_id}/turns/{turn}`) — one per compared, non-quarantined run at
  that turn. FR-022's "jump to this turn in each compared run" is a navigation
  to those paths and needs no extra round trip. The paths are already live
  routes; `tests/integration/test_catalog_and_compare.py::
  test_following_a_divergence_ref_opens_that_turn_in_each_compared_run` fetches
  every one of them.
- **Quarantined runs must not acquire a line.** `quarantined_run_ids` lists
  them, and they are already absent from `series`; a script that drew from
  `runs` instead of from `series` would reintroduce exactly the Principle III
  violation the server side prevents.
- **The colour order the page uses** is the palette in `compare.html`, indexed
  by each run's position in `runs`. Matching it keeps the legend honest.
- T056 has **no test of its own in tasks.md**. *Decision taken in Phase 5:* it
  gets no JS test runner — plan.md's dependencies decline npm (research R2), and
  adding one to assert a single function would introduce the ecosystem R2
  rejected. The behavioural guard lives on the server instead, where the
  load-bearing chart lives (`test_replay.py::test_a_gapped_turn_is_a_break_in_the_trajectory_not_a_join`,
  plus the `metric_series_gaps` contract cases); two source-level guards in
  `test_web_parity_boundary.py` cover the JS copy of the break rule. The JS is
  guarded, not tested, and US3 note 4 says so plainly.


---

## Phase 7: Polish & Cross-Cutting Concerns

**Purpose**: Release-blocking audits named directly in spec.md's Success Criteria, plus scale
validation and operator-facing wiring.

**Freeze the panel set first. — ✅ DONE, 2026-09-20.** `panels/VERSION.lock`
now records version 1's 37 per-panel hashes plus the registry `content_hash`.
**Rule P6's immutability check is in force from here on**, and two tests in
`tests/contract/test_panel_registry.py` prove it rather than assert it:
`test_the_shipped_registry_is_frozen_by_version_lock` (the lock exists, covers
the version in force, and matches) and
`test_an_edit_to_a_shipped_declaration_is_refused_by_the_shipped_lock` (copies
the shipped `panels/` wholesale, edits one *real* declaration under the
*shipped* lock, and requires the load to fail naming P6 — so the check bites on
what ships, not only on a fixture).

**Adding or changing a panel now means bumping `panels/VERSION` and adding that
version's block to `VERSION.lock`.** Re-hashing version 1 in place to match an
edited declaration defeats the whole mechanism; the lock file says so at the
top of itself.

- [X] T059 [P] Panel Registry coverage reflection test in `tests/contract/test_panel_registry.py`:
  every field in `specs/002-civ-playing-harness/data-model.md` not explicitly marked out-of-game either
  has a corresponding panel or is confirmed absent from every view model by reflection — a new store
  field defaults to invisible until deliberately registered (contracts/panel-registry.md Conformance
  tests).
- [X] T060 [P] Performance test in `tests/integration/test_scale.py`: a synthetic fixture of 300+
  turns per run (with late-game turns running into hundreds of steps) and 50+ catalog runs; asserts
  every route exercised in Phases 3–6 reaches a usable response within 2 seconds (SC-008).
- [X] T061 [P] Schema-evolution contract test in `tests/contract/test_web_read_api.py`,
  `schema_evolution` case: a fixture record predating a field renders that field with an explicit
  "unavailable for this run's recorded schema version" marker, never as zero or a silently omitted
  value (FR-025).
- [X] T062 Wire `civsim-web doctor`'s full preflight output end-to-end in `src/civsim_web/cli.py`
  against the routes built in Phases 3–6, matching quickstart.md's expected output shape verbatim.
  Depends on T015, T051.
- [X] T063 [P] Scripted smoke test of quickstart.md Scenarios 1–5 against the `MatchStore` fake in
  `tests/integration/test_quickstart_scenarios.py`.
- [X] T064 [P] Add a usage section (bind address, `doctor`, `serve`) for `civsim_web` to the project's
  existing top-level documentation, coordinating placement so it does not collide with
  `002-civ-playing-harness`'s own operator documentation.

### Phase 7 notes — what closed, what was fixed, and what stays a note

Written when T059–T064 landed, together with the owner-authorised spec
amendment. **All 64 tasks are now `[X]`.**

**The panel set is frozen.** See the note at the top of this phase.

**`civsim-web doctor` was printing a number it had not computed.** Its panel
registry line ended `0 unregistered fields reachable from a view model`, and
that `0` was a string literal — the same shape as 002's `_read_setting` defect
(a check that compared a value to itself and passed vacuously, forever). T062
replaced it with `registry/coverage.py`, which walks 002's `data-model.md` and
this feature's view models and *counts*. `doctor` now exits non-zero when the
count is not zero, and T059's parametrised negative control is what keeps the
count honest: removing the registration of a field the views demonstrably render
must make the check fail, and does.

**`doctor` now reaches the routes.** T062's preflight builds the application and
checks every path `contracts/web-read-api.md` names is registered. `cli.py`'s
`CONTRACT_ROUTES` is transcribed from the contract deliberately rather than
derived from `ROUTER_MODULES` — deriving it would compare the application to
itself and pass forever.

**T060 found a real defect, and it was fixed in code, not amended away.** The
turn route's *default* response was returning every step of a turn:
`step_limit` defaulted to `None`. `data-model.md` §5 is explicit that "`steps`
on the default response is a bounded window ... with the full ordered list
available by paging", and T043's own task text quotes that verbatim. The
recorded reason for the unbounded default — that a window "would make every turn
look shorter than it is" — is exactly what `StepWindow` answers: it carries
`total`, `has_more`, and every `skipped_step_indices`. The default is now
`DEFAULT_STEP_PAGE_SIZE = 50`. `build_step_view` still builds its enclosing turn
with the full list, so a step opened directly is never missing because of
someone else's page size. **No other test in the tree changed behaviour**, which
is itself worth noting: nothing was depending on the unbounded default.

**US3 note 3 asked the artifacts to pick one, so Phase 7 picked.** T044's
"inline single-run SVG metric trajectory" shipped as a link plus a progressive
enhancement, not a chart composed into the turn response, because composing one
costs `yields_by_turn` (one `get_turn_cycle` per turn) on *every* turn page.
T060 measured both against a 320-turn run: `/runs/{id}/metrics` renders its own
server-side chart within budget, and the turn page stays cheap by not paying for
it. **That is the resolution — the link, not the composition.** Both readers
reach identical content at an identical URL, so Principle VI holds.

#### The amendment (owner-authorised)

Recorded in the artifacts themselves, each with its rationale, so the diff reads
as a decision. Summary of what moved:

- **`spec.md`** — FR-021 amended up to Constitution Principle III (quarantine on
  *either* completeness status or a non-empty `turn_gaps()`, fail closed on
  both, never re-derive either); **FR-037 added**, giving Principle IV the
  requirement it never had (`ComparisonBasis` existed and discharged no FR);
  FR-003's health vocabulary grew `paused` and `unknown`; `ComparisonBasis`
  added to Key Entities; a new **Amendments** section carries the reasoning.
- **`contracts/web-read-api.md`** — the three collection routes now say they
  return wrappers, because three routes differing from one table in one way is a
  defect in the table; the step-level panel shape added to the view-reference
  table (22 of 37 shipped panels are `scope: step` and had no documented URL);
  two error-table rows for the `step_window_out_of_range` and `?series=`
  conventions, both of which were decided in code and written down nowhere a
  machine caller could read them; the turn route's query parameters stated.
- **`data-model.md`** — `HealthStatus.state` gains `paused`/`unknown`;
  `TurnCompleteness.is_gap` (shipped from T020, absent from the table);
  `CaptureView.unavailable_reason` gains `unrecognized_status`;
  `DecisionView.action_label_is_declaration_id`; `DivergencePoint.kind` (the
  prose named two causes and the table had no discriminator);
  `ComparisonView.basis`; and V10's enumeration now states *why*
  `DecisionStepView` is excluded rather than leaving it an apparent omission.
- **`quickstart.md`** — `doctor`'s documented output matches what it prints.

#### Deliberately left as notes, not amended

**The four probed store capabilities** (`RunConfigurationReader`,
`CaptureBlobReader`, `RunCatalogReader`, `TurnAttemptReader`) — Foundation note
2, US1 notes 1 and 3, US2 note 1, US4 notes 2 and 3. They exist because
`specs/002-civ-playing-harness/contracts/match-store-port.md` publishes no read
that resolves a run configuration, returns capture bytes, enumerates terminal
runs, or addresses a turn attempt by index. **That contract belongs to
deliverable 2 and was not edited here.** Amending another deliverable's contract
to make this one's tasks look closed is the move that would bury the finding.
They stay open against deliverable 3.

Also left as notes, each for its own reason:

- **`SEPARATION_RATIO = 0.25`** (US4 note 7). §11 says "beyond a threshold" and
  names no number. Writing this implementation default into the spec would
  convert it into a product decision nobody has made. It stays labelled.
- **`ViewReference` has no attempt component** (US2 note 5). The contract
  already makes `?attempt=` a query rather than a path segment, so the artifacts
  agree with each other; only §12's prose overstates its own generality.
- **`static/trajectory.js` is guarded, not tested** (US3 note 4). Unchanged and
  still true: plan.md's dependencies decline npm (research R2), the behavioural
  guard lives on the server where the load-bearing chart lives, and two
  source-level guards in `test_web_parity_boundary.py` cover the JS copy of the
  break rule.
- **Task-text inaccuracies** — T005's read list was two operations short
  (Foundation note 1), T039(b) named the wrong `Accept` header (US2 note 6),
  T039(a)'s "so far" was ambiguous under parallel staffing (US2 note 4), and
  T053's dependency crossed a boundary the parallelism note denied (US4 note 1).
  All four were found, worked around correctly, and recorded at the time. They
  are defects in *this file*, now closed, and rewriting the task text after the
  fact would erase the record of what the contributor actually hit.

#### One parse limitation found and not fixed

`registry/harness_schema.py` cannot see `ParityDeclaration`: 002 heads that
section `## 10. ParityDeclaration (catalog entry)`, and the entity-heading
pattern requires the name to end the line. The consequence is that a panel
declaring `ParityDeclaration.<field>` would be **rejected** by rule P3 rather
than admitted — it fails closed, no panel declares one, and the fix belongs with
whoever next touches that parser. Recorded rather than fixed because widening
the pattern to accept trailing prose is exactly the kind of loosening that
should be done deliberately, not in passing.

#### Three unnumbered files were added

`src/civsim_web/registry/coverage.py` (T059's rule, and T062's computed
`doctor` line), `tests/integration/test_scale.py` (T060) and
`tests/integration/test_quickstart_scenarios.py` (T063). `panels/VERSION.lock`
is data, not code, and is described above.

---

## Phase 8: Amendment conformance (2026-09-20, post-T064)

The owner-authorised amendment to
`specs/002-civ-playing-harness/contracts/match-store-port.md` (Capability
extensions, E1–E5) published `get_run_configuration` as a first-class port
read, keyed by **`run_id`** (E5: run ids resolve and nothing else does). The
amendment review found this feature's consumer side did not conform.

- [X] T065 Re-key the configuration probe and align the seam with the amended
  contract. **The defect**: `store_client/reads.py::run_configuration` probed
  the published operation name but passed `run.config_id` where the published
  read takes `run_id` — so against a conforming store the probe bound, the
  lookup mis-keyed, the store answered `None` (E5), and the catalog's
  seed/civilization/ruleset/model columns rendered *unavailable* on a store
  that was fully capable; worse, a `config_id` colliding with another run's
  `run_id` would have silently displayed the **wrong run's** configuration —
  the exact asymmetric-visibility failure Principle VI (shared observability)
  forbids. **The fix**: the call site now passes the run's own `run_id`;
  `RunConfigurationReader` was retired exactly as its docstring promised
  (deleted, with `MatchStore` in `store_client/port.py` widened to the
  published read) and `READ_OPERATIONS` gained `get_run_configuration`, so
  `tests/web_support/fixtures.py::_PublishedPortOnly` no longer models a store
  *stricter* than the published contract (it now forwards the read); the
  fake's `get_run_configuration` was re-keyed to model E5 (an unknown
  `run_id` answers `None` even when it equals some run's `config_id`); stale
  "the port has no operation that resolves a config_id" prose in
  `store_client/{port,reads,catalog,fake}.py` was corrected, along with the
  fixture docstring's miscount of the optional capabilities ("three" while
  four existed; three remain now that one is published). **Guarding tests**
  (`tests/contract/test_web_read_api.py`):
  `test_the_store_receives_the_runs_own_run_id_for_a_configuration_read`
  asserts on what the far side RECEIVED — a recording conforming store with
  `run_id != config_id` must receive the run's own `run_id` and the columns
  must render available — and
  `test_a_config_id_colliding_with_another_runs_run_id_never_serves_that_runs_configuration`
  pins the collision case at the seam and through the route. **Revert
  confirmation**: with `config_id` temporarily restored at the call site, the
  first fails `assert {'cfg-1'} == {'run-1'}` (the store received the wrong
  key) and the second fails `assert 'ROME' == 'GREECE'` (the collider run
  rendered the victim's configuration); with the fix restored both pass. No
  other test enshrined the stricter-than-contract fixture: the sole
  `published_port_only` consumer (`test_the_catalog_says_so_when_the_port_can_
  only_reach_active_runs`) asserts only the catalog-listing gap, which remains
  a probed capability, and needed no change.

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

---

## Phase 9: Convergence (2026-09-21)

`/speckit-converge` against the shipped code, after Phase 8 closed and all 65 prior tasks were `[X]`
with the suite green (1650 passed / 8 skipped). Assessed: FR-001 – FR-037, the buildable success
criteria (SC-003/004/005/008/010/012/014/015/016), the ten UI Principles, `data-model.md`'s V1 – V10,
both contracts, and Constitution Principles I, III, IV and VI.

**No constitution MUST is violated and nothing below is a Principle I leak.** The gate still refuses
unregistered fields, the capture rule still fails closed on an unrecognised status, the read-only
boundary still holds by AST scan, and quarantine still refuses to construct rather than silently
averaging. The findings have a thinner shape in common: **four audits are narrower than the sentence
that describes them, and two correct behaviours are guarded by nothing**, so a later edit could undo
them with all 1650 tests still passing. That is the failure mode this project has caught twice
before (002's `_read_setting` comparing a value to itself; `doctor`'s hard-coded `0`), found here a
third time by looking at what each audit actually iterates rather than at what its docstring claims.

- [X] T066 **HIGH** Bring `GET /healthz` inside the FR-030 credential audits per FR-030 (partial).
  **The defect**: `test_no_view_model_declares_a_credential_shaped_field` walks
  `pkgutil.iter_modules(civsim_web.viewmodels.__path__)` plus `ErrorView`, and
  `test_no_secrets_in_any_response` is parametrised over the hand-written `ROUTES` list.
  `StoreHealthView` and `ServiceHealthView` are declared in `src/civsim_web/app.py` — *outside* the
  `viewmodels` package — and `/healthz` is not in `ROUTES`, so the one route that reports a real
  store's connection state is in **neither** scan, while the schema test's own docstring claims it
  walks "every view model reachable from a registered route's response". `StoreHealthView.detail` is
  set from `getattr(health, "detail", None)` or `f"ping raised: {exc}"` — a raw store exception
  rendered verbatim on a page FR-028 requires to be unauthenticated on the LAN, which is exactly
  where a DSN or bearer token surfaces. Fix: redact `detail` at the `StoreHealthView` boundary
  (FR-030 is a *second* pass, not a rerun of the store's own), add `/healthz` to `ROUTES`, and make
  the schema scan cover every `ViewModel` subclass in the whole `civsim_web` package rather than one
  sub-package — with a negative control proving the widened scan can fail.
- [X] T067 **HIGH** Read captures only for the steps the step window returns, in
  `src/civsim_web/routes/turns.py`, per FR-036 (contradicts). `reads.capture_records_for_turn(store,
  record)` iterates `record.steps` — the **full** list — and issues one `store.get_capture` per
  declared id, and the route calls it *before* `build_turn_cycle_view` applies `step_offset`/
  `step_limit` via `viewmodels/turn.py::_window`. So rendering 50 steps of a 200-step turn performs
  ~200 capture-record reads: "viewing a turn MUST NOT require loading captures beyond those being
  viewed", violated at the record level. (Image **bytes** are correctly lazy — own route,
  `loading="lazy"`, cached — so only the record reads are at fault.) Fix: promote `_window`'s
  selection to a shared helper and read captures for the selected bundles only, so the window and
  the capture reads cannot disagree. **`routes/live.py` is deliberately out of scope**: it renders
  the whole turn on purpose, because `latest_decision_of` reads the *last* step and a bounded window
  there would make FR-001's "most recent agent decision" show step 50 of 200. Add a test that counts
  `get_capture` calls on a run carrying a capture on every step — the current 300+-turn fixture is
  built with `captures=[]` and says so, which is why nothing caught this.
- [X] T068 **HIGH** Guard the live page's four couplings to `static/poll.js` per FR-002 and spec Edge
  Cases ("the connection drops — the view marks itself as possibly stale and recovers to live without
  a manual reload") (partial). The behaviour is implemented and correct:
  `templates/live/run_detail.html` carries `data-live="true"`, `data-run-id`, `<script
  src="/static/poll.js" defer>` and `<span id="stale-marker" hidden>`, and `poll.js` sets
  `data-stale` on a missed poll and clears it on recovery. **Nothing asserts any of it.** The only
  test touching the file regex-reads `POLL_INTERVAL_MS`; `test_static_is_mounted` asserts a `/static`
  mount exists and nothing more; a `<script>` tag is not a `data-field`, so `test_json_html_parity`
  cannot see it. Deleting any one of the four lines leaves all 1650 tests green while the live view
  silently stops updating. US3 note 4 already established the pattern for this exact situation —
  source-level guards in `tests/contract/test_web_parity_boundary.py` over `trajectory.js`, because
  research R2 declines a JS runner — and it was never applied to the poll script. Add the equivalent
  guards there, plus a rendered-markup assertion that the live page wires the script and ships the
  stale marker.
- [X] T069 **HIGH** Widen the entity-heading pattern in `src/civsim_web/registry/harness_schema.py`
  so `ParityDeclaration` is scanned, per SC-005 and `contracts/panel-registry.md` Conformance
  (partial). `_NUMBERED_ENTITY = r"^##\s+\d+\.\s+(\w+)\s*$"` requires the entity name to end the
  line; 002 heads that section `## 10. ParityDeclaration (catalog entry)`, so the entity never
  parses and `_OTHER_H2` closes the block. The consequence is bigger than the Phase 7 note recorded:
  `registry/coverage.py` iterates `schema.fields_by_entity`, so the SC-005 coverage audit — and the
  line `civsim-web doctor` prints, "167 fields scanned" — leaves **one whole 002 entity outside the
  scan**, in an audit the contract calls release-blocking. The fix is safe to make: `ParityDeclaration`
  has no `ENTITY_PATHS` placement, so its twelve fields classify as `invisible` and no panel declares
  one. Assert `ParityDeclaration` is in the scanned set and that the count rises, so the widening
  cannot silently regress.
- [X] T070 **MEDIUM** Document `GET /`'s real behaviour and `LandingView` in
  `contracts/web-read-api.md` and `data-model.md`, per `contracts/web-read-api.md` (contradicts).
  `LandingView` (`viewmodels/run_detail.py`) is a top-level response served at `GET /` and it appears
  in **no** artifact. The contract disagrees with itself and with the code three ways: its route
  table says `GET /` returns "the single active run (or a chooser, if several are active)"; its
  amendment paragraph says `GET /` "is a redirect ... (or to `/runs` if none is)" with "no `Accept:
  application/json` equivalent of its own"; the code redirects `307` only when exactly **one** run is
  active and otherwise renders a `LandingView` as HTML *or* JSON — the chooser and the explanatory
  empty state the spec's Edge Cases require. The code is right and all three cases are tested; the
  artifacts are wrong. Also add `LandingView` to §13's V10 enumeration, which was amended to be
  exhaustive-with-a-stated-exception (`DecisionStepView`) and now has an unstated second omission.
- [X] T071 **MEDIUM** Cite FR-037 where `ComparisonBasis` is implemented and tested, per spec
  Amendment B (partial). Amendment B added FR-037 so the behaviour would be "traceable from
  constitution to requirement to implementation to test" — and the identifier **FR-037 appears
  nowhere in this feature's code, tests, or task list**. `viewmodels/comparison.py` and its three
  pinning tests cite "Principle IV" only; the repo's sole `FR-037` hit is 002's unrelated
  requirement. The behaviour is fully correct; the traceability the amendment claims to have
  established does not yet exist, which is the precise failure Amendment B said it was preventing.
- [X] T072 **MEDIUM** Test that the auto-detect bind path *drops* a non-private address, per FR-029
  (partial). The explicit-config path is well pinned (`test_a_wildcard_bind_is_refused` over
  `0.0.0.0`/`::`/`*`/`""`; `test_non_private_and_link_local_addresses_are_refused` over `8.8.8.8`,
  `203.0.113.5`, `169.254.10.1`). But `detect_private_addresses` — the default whenever `--bind` is
  unset — is asserted only by `test_detected_addresses_always_include_loopback_last`, which checks
  `all(is_bindable_address(a))` over output that the function already filtered with that same
  predicate. It is self-referential: deleting the filter still passes on any RFC1918-only host. Stub
  the interface enumeration with a public and a link-local address and assert neither is returned.
- [X] T073 **MEDIUM** Pin three implemented-but-unasserted requirement clauses (partial): (a)
  FR-021's "a signal it could not read at all counts as absent" — `derive_trend_eligibility` with
  `gapped_turns=None` yields `not_assessed`, and neither that reason nor
  `record_completeness_status_unrecognized` is asserted anywhere under `tests/`; (b) FR-033's
  run+turn binding — no test asserts a *clean* capture renders at its own turn during replay, and
  `capture["turn_number"]` is never asserted, so nothing would catch turn N showing turn N−1's
  capture; (c) FR-019's `unknown_sort_field` 400 branch in `routes/catalog.py::_unknown_field_error`,
  whose sibling filter-field branch is tested and whose sort-field branch is not.
- [X] T074 **LOW** Correct two in-code claims the 2026-09-20 amendments made false (partial):
  `cli.py`'s `CONTRACT_ROUTES` comment says the step-scoped panel shape "is in `data-model.md` SS12
  and in neither table", but `contracts/web-read-api.md`'s view-reference table gained that row in
  the amendment; and `viewmodels/base.py::HealthState`'s docstring still says "the first seven are
  FR-003's verbatim vocabulary", calls `paused`/`unknown` states "the vocabulary lacks", and reports
  both as "findings against the spec" — Amendment C folded both into FR-003, so the enum is correct
  and only the prose is stale.

- [X] T075 **HIGH** Time `/runs` and `/compare` at the scale SC-008 actually names, and fix what that
  exposes, per SC-008 (contradicts). **The finding**: Phase 9 recorded as a note that
  `tests/integration/test_scale.py` times the catalog routes only against 30-turn runs. Read against
  SC-008 — *"With a run of 300+ turns **and** 50+ recorded runs in the catalog, **every view** reaches
  a usable state within 2 seconds"* — that is not a note, it is an untested success criterion: the
  sentence names both halves at once and the fixture only ever had one of them (55 runs × 30 turns).
  **What it exposed**: `/runs` took **10.7 s** at 55 × 320, five times the budget, and its HTML
  rendering 9.5 s. `routes/catalog.py` asked `list_catalog_rows` for `with_metrics=True`, which read
  the *entire* per-turn series for every run — 17,600 `get_turn_cycle` calls — so that
  `CatalogRow.outcome_metrics` could keep `yields_by_turn[max(...)]` and discard the rest. The port
  publishes no metric-series read (plan.md C1), so each of those turns is its own store call.
  **The fix**: `MetricsScope` (`series` / `outcome` / `none`) replaces the boolean, and
  `latest_yields` walks *down* from the same upper bound to the first turn with recorded yields —
  provably the same turn `max(yields_by_turn)` selects, so the catalog column and the chart still
  come from one source as data-model.md §1 requires. `/runs` is now **0.13 s**, its HTML 0.31 s.
  `/compare` was already within budget (it reads only the runs being compared) and is unchanged.
  **Guards**: five timed catalog paths plus both HTML pages against a new 55 × 320 fixture, a
  `/compare` case asserting the trajectories really carry 300+ points, and
  `test_the_catalog_row_reads_only_the_turns_it_shows`, which asserts `latest_yields` picks the
  identical turn the full walk would — a wall-clock assertion alone would go green again on a faster
  machine with the defect restored. **Revert confirmation**: with `MetricsScope.SERIES` put back at
  the call site, `/runs` fails at `8.56s` and `?sort=turn_count` at `8.91s`; with the fix, both pass.

### Phase 9 notes — what closed, and the one shape all nine had

Written when T066–T075 landed. **All 75 tasks are now `[X]`.**

**The shape.** *(Written at T074 and corrected at T075 — the original wording is
kept because being wrong about this is the point.)* It first read: "Not one of
these was unbuilt work. Every requirement was implemented and behaving correctly
before this phase started." That was true of T066–T074 and **false of T075**,
which found `/runs` five times over SC-008's budget. The correction matters
because the original sentence is exactly the assumption that let the gap sit
there: a feature whose tasks are all `[X]` invites the reading that only the
checks can be wrong, and T075 is the counter-example — a *note* deferring a
measurement turned out to be deferring a defect.

What the nine findings before it had in common is that the *checks* were
narrower than the sentences describing them. Four audits iterated less than their own docstrings claimed
(`/healthz` outside both FR-030 scans; `ParityDeclaration` outside the SC-005
coverage scan; `detect_private_addresses` asserted against its own predicate;
the `ROUTES` matrix hand-maintained with nothing checking it covered the app),
and two correct behaviours had no guard at all (`poll.js`'s four couplings to
the live page; the capture/window relationship). This is the third time this
project has found a check that compared something to itself — after 002's
`_read_setting` and `doctor`'s hard-coded `0` — and it is worth naming as a
class rather than a coincidence: **a check written from the same mental model
as the code inherits that model's blind spot.** Every fix below was therefore
paired with a revert confirmation, run and recorded, because a guard nobody has
watched fail is the same defect again.

**Two were real behavioural defects, not just coverage.**

- **T067 (FR-036).** `routes/turns.py` read a capture record for every step of a
  turn before the window trimmed it: 200 `get_capture` calls to render 50 steps.
  Revert-confirmed — with the old call restored the new test reports `200
  capture records read to render 5 steps`, and 5 with the fix. `select_step_window`
  is now public and both the route and `build_turn_cycle_view` call it, so the
  steps whose captures are read and the steps that render are one set by
  construction. **`routes/live.py` was deliberately left alone**, and now has its
  own test saying why: the glance renders the whole turn because
  `latest_decision_of` reads the *last* step, and a window there would make
  FR-001's "most recent agent decision" show step 50 of 200. That test exists to
  stop someone "fixing" the glance to match.
- **T066 (FR-030).** `StoreHealthView.detail` rendered a store's `ping()` failure
  verbatim -- `f"ping raised: {exc}"` -- on a page FR-028 requires to be
  unauthenticated on the LAN, and a connection error naming its own DSN is the
  likeliest way a credential reaches this interface. Both health models were
  declared in `app.py`, outside the `viewmodels` package the schema audit walks,
  and `/healthz` was outside the response audit too, so *neither* FR-030 check
  had ever looked at the one route that renders a real store's state. The models
  moved into `viewmodels/service_health.py` (inside the audit by construction,
  not by someone remembering to widen it), `redact.py` is the second redaction
  pass plan.md's Constraints already called for, and
  `test_the_secret_scan_covers_every_registered_route` resolves every registered
  path against the paths the scan fetches — verified to name `/healthz` when run
  against the pre-fix list.

**T069 was bigger than the Phase 7 note that recorded it.** That note said a
panel declaring a `ParityDeclaration` field would be *rejected* by rule P3 — true,
and the fail-closed direction. What it missed is that `registry/coverage.py`
iterates the same parse, so the SC-005 audit and the line `civsim-web doctor`
prints had one of 002's fourteen numbered entities outside them entirely. The
scan went 167 → 179 fields, and `test_every_scanned_field_lands_in_exactly_one_bucket`
had passed throughout — a partition check cannot catch a missing *input*, which
is why the new test reads 002's headings directly. The widening is narrow on
purpose: a parenthesised aside after the name, not `(.*)$`, which would invent
an entity out of any prose heading.

**One thing stayed a note.** `panels/catalog.yaml`'s `catalog.comparison_basis`
declaration is the natural place to cite FR-037 and it was **not** edited: the
declaration's hash is frozen in `panels/VERSION.lock`, so adding a comment there
means bumping `panels/VERSION` and adding a version block — a schema history
entry for a citation. T071's citations live in `viewmodels/comparison.py` and
the three pinning tests instead. The freeze working as designed is the point.

**The three probed capabilities stay, now that 003 has landed** (considered
2026-09-21 and declined, so the question is not re-opened by the next reader).
`civsim_harness.store.contract.MatchTrackingStore` does publish `list_runs`,
`get_capture_image` and the attempt read under the names the probes look for, so
retiring `CaptureBlobReader` / `RunCatalogReader` / `TurnAttemptReader` looks
free. It is not, for two independent reasons:

1. **It would not be behaviour-preserving.** The probes *are* the graceful
   degradation: a store without the capability gets a catalog explicitly marked
   partial, a `503` naming the port gap instead of a placeholder image, and the
   published-port attempt fallback. Making the three reads mandatory deletes
   those paths and the tests that hold them (`published_port_only`,
   `make_store(attempt_reader=False)`). The amended port contract says the same
   thing from its own side: E1 makes these obligations on *deliverable 3*, and
   **E2 keeps structural probing as the discovery mechanism** — `civsim_web` can
   be pointed at the fake or at 002's interim adapter, neither of which is 003.
2. **Importing 003's contract is forbidden here.**
   `test_web_parity_boundary.py::test_no_module_imports_the_harness` fails on any
   `civsim_harness` import from `src/civsim_web/**`, and that guard exists for
   plan.md's Constitution Check under Principle I.

The conformance question that *is* worth asking — do the probes still bind
against the real store? — is already answered from 003's side by
`tests/integration/test_web_against_tracking_store.py`, which asserts the three
degraded answers are gone when the app runs on a real `SqliteMatchStore`, and
which states in its own docstring that `civsim_web` is not modified by
deliverable 3. Nothing to add here.

**Also deliberately still open**, unchanged from Phase 7:
`SEPARATION_RATIO = 0.25` as a labelled default,
`ViewReference` carrying no attempt component, and `static/trajectory.js` being
guarded rather than tested — `static/poll.js` now joins it on the same terms
(T068), for the same reason research R2 gives.

**~~Recorded, not tasked~~ — escalated to T075 and closed.** This note originally
read that `/compare` and `/runs` were timed only against 30-turn runs and that
this was "worth a Phase 10 if anyone opens one". That was the wrong call, and
re-reading SC-008 is what showed it: the criterion names both halves in one
sentence — *"a run of 300+ turns **and** 50+ recorded runs"* — so an untimed
combination is not a coverage preference, it is a success criterion with no test
behind it. Writing the test found `/runs` five times over budget. **The lesson is
the phase's own, one layer up**: the nine findings were checks narrower than the
sentences describing them, and this note was a *note* narrower than the sentence
describing it. See T075.

---

## Phase 10: Convergence re-check (2026-09-22)

`/speckit-converge`, attempt 2, against the shipped code after Phase 9 closed and all 75 prior
tasks were `[X]`. Suite at head `c7b5654`: **2138 passed / 19 skipped / 0 failed** in 203.89 s
— green, including the `/compare` render-budget case that is load-sensitive on this box.
Report: [analyze-2026-09-22.md](./analyze-2026-09-22.md).

**Phase 9's ten fixes were re-verified against the code, not against this file's claims.**
All ten landed with real code and real tests behind them: `viewmodels/service_health.py` +
`redact.py` + `test_the_secret_scan_covers_every_registered_route` (T066); `select_step_window`
public and called from both the route and `build_turn_cycle_view`, with
`test_a_turn_reads_only_the_captures_of_the_steps_it_returns` counting the reads (T067); four
source-level `poll.js` guards plus a rendered-markup assertion (T068); the widened
`_NUMBERED_ENTITY` pattern with `ParityDeclaration` asserted into the scanned set (T069); the
`GET /` amendment in **both** artifacts (T070); `FR-037` cited in `viewmodels/comparison.py` and
its pinning tests (T071); `detect_private_addresses` stubbed with a public and a link-local
address (T072); all three unasserted clauses pinned (T073); both stale in-code claims corrected
(T074); `MetricsScope` and the 55 × 320 fixture with `test_the_catalog_row_reads_only_the_turns_it_shows`
(T075). Nothing was found to be a claim without a code path behind it.

**So the findings below are not Phase 9's work undone.** They are where that phase's own shape —
*a check narrower than the sentence describing it* — migrated to once the checks were widened:
**a number an artifact asserts that nothing recomputes**, and **in-code prose that instructs a
future contributor and that nothing can make wrong.** Both are load-bearing in the way a test is,
and neither fails when it stops being true.

- [ ] T076 **MEDIUM** Correct `quickstart.md`'s `civsim-web doctor` sample and guard it at the
  level it can actually hold, per T062 and `quickstart.md` (contradicts). **The finding**:
  `quickstart.md:32` documents `registry coverage : ok (167 fields scanned, 91 marked
  out-of-game, 27 registered, 49 unregistered and unrendered, 0 unregistered fields reachable
  from a view model)`. Running `uv run civsim-web doctor` at this head prints **`180 … 103 … 27
  … 50 …`** — four of the five numbers are wrong. T069 widened the scan (167 → 179) and did not
  update the document; 002's `data-model.md` has since grown it again to 180, so **the figure
  moves whenever another lane adds a field to a deliverable this feature does not own**. T062's
  own text promised output "matching quickstart.md's expected output shape *verbatim*", and the
  test that discharges it —
  `tests/integration/test_quickstart_scenarios.py::test_setup_doctor_reports_all_green_in_the_documented_shape`
  — asserts the five **labels** and nothing else, so the drift was invisible: the audit is
  narrower than the sentence describing it, one layer up from where Phase 9 found it.
  **The fix is not to pin the number.** Tightening the test to compare the whole line would put a
  cross-deliverable count in this feature's CI and break it on every 002 field addition — that is
  how a guard becomes something contributors edit to make green. Correct the documented line to
  what `doctor` prints, say in the document that the field counts track 002's `data-model.md` and
  will move, and tighten the test to assert the part that is this feature's own invariant: five
  labelled lines, each `ok`, and **the coverage line's last number is `0`** — which is the claim
  `doctor` exits non-zero on and the only one of the five that means anything about parity.
- [ ] T077 **MEDIUM** Replace the three probe docstrings' now-false instruction with the decision
  that was actually taken, per Phase 9's "The three probed capabilities stay, now that 003 has
  landed" (contradicts). **The finding**: `src/civsim_web/store_client/port.py` declares
  `RunCatalogReader` (line 295), `CaptureBlobReader` (322) and `TurnAttemptReader` (353), and each
  closes with **"This is a dependency to raise with deliverable 3, not a decision taken here. …
  delete this Protocol and widen `MatchStore` to match."** Deliverable 3 landed on 2026-09-21 and
  publishes all three reads (`specs/003-match-tracking-store/contracts/match-tracking-store.md`
  Operations: `list_runs`, `get_capture_blob`, `get_turn_cycle_attempt`). The condition each
  docstring names has been met, so each is now an instruction to do the thing **this feature
  deliberately decided not to do** — and the two reasons for that decision (retiring them deletes
  the graceful-degradation paths and the tests that hold them, `published_port_only` and
  `make_store(attempt_reader=False)`; and the amended port's E2 keeps structural probing as the
  discovery mechanism) live only in the Phase 9 notes of this file. A contributor reading
  `port.py` alone is told to delete three Protocols and would take three tests with them.
  **The template for the fix is eight lines above the first one**: the retired
  `RunConfigurationReader` comment records what happened and why. Write the same for the three
  that stayed — the decision, its two reasons, and what would have to change to re-open it.
  Same task, two smaller instances of the same class: `src/civsim_web/routes/__init__.py:22` says
  "twenty shipped panels are `scope: step`" where the shipped registry has **22** of 37
  (`grep -c 'scope: step' panels/*.yaml`), which is what `contracts/web-read-api.md`'s own
  amendment says; the number was right when US2 wrote it and went stale when US3 added
  `history.yaml`.

### Recorded, not tasked

Each was checked this pass and each is a deliberate non-finding. Written down so the next reader
spends the pass on something else.

- **The SC-004 parity matrix still has no coverage guard of its own.** T066's
  `test_the_secret_scan_covers_every_registered_route` resolves every registered route against
  `ROUTES + SECRET_SCAN_EXTRA`, which guards FR-030. A future HTML-rendering route could satisfy
  it by being appended to `SECRET_SCAN_EXTRA` and would then be audited for secrets but never for
  JSON/HTML parity. **Not a present defect** — all 13 registered routes are correctly placed, and
  `/healthz` and `/captures/{id}/image` genuinely cannot sit in a parity matrix. Enforcing
  "renders HTML ⇒ must be in `ROUTES`" needs the test to know which routes negotiate, which is a
  second model of the application and the exact thing this project keeps finding wrong.
- **T071 says "three pinning tests" and two test functions cite `FR-037`.** Both statements are
  true: the divergent and uniform cases share one function
  (`test_a_comparison_of_runs_with_different_starting_conditions_says_so`) and the unverifiable
  case is its own. Three cases, two functions. Amendment B's wording is loose, the coverage is
  not. Recorded rather than rewritten, per Phase 7's standing rule — editing a closed task's text
  after the fact erases the record of what the contributor actually hit.
- **T066's text says "/healthz to `ROUTES`" and the code put it in `SECRET_SCAN_EXTRA`.** The code
  is right and says why in its own docstring: `ROUTES` is the HTML-**and**-JSON matrix, and the
  parity test would parse a JSON-only body as markup. Same standing rule, same treatment.
- **T069's field count is not pinned by a number**, only by `ParityDeclaration`'s presence in the
  scanned set. That is the correct choice for the same reason T076 gives, and the live count (180)
  confirms the widening holds.
- **UP-007 ("Comparison is first-class") is the only UI Principle with no citation** in
  `src/civsim_web/**` or `tests/`. The behaviour exists and is well tested; only the thread is
  missing, and T071 established that the fix is a comment plus test docstrings. One citation is
  not a task.
- **SC-006 and SC-013 each have a mechanically checkable half that no test states.** Both hold in
  fact — the landing → run → turn path is two hops, and no route in the tree reads a credential, a
  cookie or a session. Asserting "no route requires authentication" over a codebase containing no
  authentication code is a check that compares the absence of a thing to itself, which is the
  anti-pattern the Phase 9 notes name three times. Deliberately not written.
