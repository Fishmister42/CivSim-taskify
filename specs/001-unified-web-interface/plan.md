# Implementation Plan: Unified Web Interface

**Branch**: `001-unified-web-interface` | **Date**: 2026-09-20 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/001-unified-web-interface/spec.md`

**Note on provenance**: this plan and its Phase 0/1 artifacts were authored directly against the
`speckit-plan` template rather than via `.specify/scripts/powershell/setup-plan.ps1`. That script
resolves "the current feature" through `.specify/feature.json`, which a parallel effort has pinned to
`specs/002-civ-playing-harness` while it is actively implemented; running the script would either
target the wrong feature or repoint that file out from under it. `.specify/feature.json` was not
modified to produce this plan and still reads `specs/002-civ-playing-harness`.

## Summary

Feature 001 is a **read-only** presentation layer over the match-tracking data 002 (the Civ-playing
harness) writes. It has no domain logic of its own: every fact it shows is already sitting in the
store behind 002's `MatchStore` port, already parity-filtered before it arrives. What this feature
adds is a second, structural filter (a small **Panel Registry**, mirroring 002's capability catalog)
that makes "no undeclared field reaches a screen" true by construction rather than by care; a single
JSON+HTML surface where the user's browser and the directing Claude Code session hit the exact same
endpoints and receive the exact same content, so the two views can never drift apart (Principle VI);
and four progressively richer views over that data — a live glance, a shared/addressable reference
scheme, a turn-by-turn replay, and a cross-run comparison — matching the spec's four prioritized user
stories.

The technical approach is one Python web service, built around three load-bearing structures:

1. **A read-only client of the `MatchStore` port** (`specs/002-civ-playing-harness/contracts/match-store-port.md`).
   This feature calls only the port's read operations (`get_run`, `get_turn_cycle`,
   `list_active_runs`, `turn_gaps`, `step_gaps`, `list_save_points`, `get_last_known_good`, `ping`)
   plus whatever catalog/run-listing reads deliverable 3 exposes; none of the port's nine writes are
   ever called from this codebase. Depending on the Protocol rather than the SQLite reference adapter
   is what lets deliverable 3 replace that adapter later without a code change here.
2. **A Panel Registry as data.** Every unit of information the UI can render is a named, versioned
   declaration — its source fields, its restated parity basis (carried from 002's `ParityDeclaration`
   where the field is in-game, or marked out-of-game telemetry where it is not), and which user story
   it serves. A field not named in the registry has no code path into a template, the same structural
   guarantee 002's capability catalog gives the agent's context (UP-001).
3. **One view model, two renderers.** Every route builds a single Pydantic view model from the store;
   content negotiation on the `Accept` header (or an explicit `?format=json`) decides whether the
   response is rendered as an HTML page (for the user) or returned as the view model's JSON (for the
   directing session, or for any other machine caller). Because both readers are served by the same
   function up to the last step, "the session can retrieve anything the user can see" (FR-007) is a
   property of the code path, not a promise about keeping two implementations in sync.

Live currency, addressability, replay, and comparison are built on top of these three: polling on a
fixed interval well inside the 5-second currency window 002's plan already commits to; a URL scheme
that is simultaneously the human-navigable page and the machine-resolvable reference (FR-008); replay
that reads exactly the same `TurnCycleRecord` structure 002 writes, including its abandoned attempts
and gap markers; and comparison views that never touch captures, operating on `MetricSeries` data
alone (FR-035).

## Technical Context

**Language/Version**: Python 3.12+, managed with `uv` — the same toolchain as `002-civ-playing-
harness`. Chosen specifically to share that toolchain rather than to introduce a second ecosystem:
see [research.md](./research.md) R1.

**Primary Dependencies**:

- **FastAPI** + **Uvicorn** — HTTP routing, request/response validation, and the content-negotiation
  branch point described above. FastAPI is already 002's choice for its operator surface
  (`contracts/operator-surface.md`), so this feature reuses a pattern the project has already vetted
  rather than adding a second web framework (R1).
- **Jinja2** — server-rendered HTML templates for the human-facing pages. No SPA framework, no
  frontend build step (R2).
- **Vanilla JavaScript**, served as static files — polling for live updates, and hand-rolled inline
  SVG for trajectory charts. No bundler, no npm dependency tree, no CDN script (R2, R4).
- **Pydantic v2** — the view models shared between the JSON and HTML renderers; the same library 002
  uses for its own schemas, so a `TurnCycleRecord`-shaped response can be validated against 002's
  published contract shape without a second modeling library.
- **httpx** — test client only (FastAPI's ASGI test transport).
- **Testing**: `pytest`, `pytest-asyncio`, `syrupy` for HTML/JSON snapshot equivalence — the same
  three 002 already uses.

No new runtime ecosystem is introduced. The one candidate that would have been a second ecosystem — a
JS charting library — is deliberately declined in favor of hand-rolled SVG (R4); this is recorded so a
future contributor does not read its absence as an oversight.

**Storage**: None of this feature's own. It is a pure reader of the `MatchStore` port
(`specs/002-civ-playing-harness/contracts/match-store-port.md`) and calls only its read operations —
`get_run`, `get_turn_cycle`, `list_active_runs`, `turn_gaps`, `step_gaps`, `list_save_points`,
`get_last_known_good`, `ping` — plus a run-catalog listing/filter capability that the port as
currently published does not yet name (see research R3 and Complexity Tracking C1). It must never
call any of the port's nine write operations, and no code path in this feature may do so (FR-023,
FR-024, FR-026). Because deliverable 3 (the match-tracking store) does not exist yet either, this
feature develops and tests against a local fake implementing the same `MatchStore` Protocol — the
same strategy 002's own plan uses (its Complexity Tracking C2) against deliverable 3, applied here one
layer further out (R3).

**Testing**: `pytest` in three tiers:

| Tier | Covers | Needs a live store? |
|---|---|---|
| `tests/unit` | Panel Registry load-time validation, view-model construction from raw store shapes, health-status derivation, currency/staleness computation, capture second-gate filtering | No |
| `tests/contract` | JSON/HTML equivalence per route (the same view model produced both ways), Panel Registry parity-basis completeness (SC-005 audit), capture screening audit (SC-014 audit), view-reference round-trip resolution (SC-012) | No — runs against the `MatchStore` fake |
| `tests/integration` | Full route behavior against the fake store: live polling reflects a newly written turn within the currency window, replay across a recorded interruption, catalog filter/sort, comparison-view divergence-point navigation | No |

A fourth, unmarked manual tier — walking the `quickstart.md` scenarios against 002's real local
reference adapter once it is running — is how this feature is validated end-to-end, but it is not a
CI tier of its own because it depends on a live 002 process.

**Target Platform**: Served from the same machine as the harness (or any machine with network access
to the configured `MatchStore`), reachable from other devices on the operator's local network by a
stable address. Standard desktop browser, wide screen, is the supported client (spec Assumptions);
phone layouts are out of scope.

**Project Type**: Web application, but collapsed into a single Python service rather than a
frontend/backend split — there is no separate frontend project because there is no frontend build
step (R2). `Structure Decision` below places it as a sibling package to `002`'s, not inside it.

**Performance Goals**:

- A newly recorded turn appears in an open live view within 5 seconds of being recorded (SC-003) —
  the currency window is 002's own plan number, carried over rather than reinvented (R5). The client
  polling interval is fixed well inside it (R5).
- Every view reaches a usable state within 2 seconds at 300+ turns per run and 50+ runs in the catalog
  (SC-008) — met by server-side catalog pagination/filtering and per-step lazy loading within a turn
  rather than shipping an entire run's decision-step sequence at once (R6).
- A stalled or crashed run is reflected within 60 seconds of the stall or crash (SC-009) — met by
  surfacing 002's own `crash_detected`/`hang_detected`/`unresponsive_detected` events rather than
  running a second, independent stall detector that could disagree with 002's (R7).
- A user opens any specific turn of any recorded run within 30 seconds and 3 interactions from the
  landing view (SC-006); a 5-run comparison answers leader-and-divergence in under 2 minutes (SC-007).

**Constraints**:

- **Read-only, structurally.** No route, handler, or dependency in this codebase may import or call a
  `MatchStore` write method; enforced by a lint/import-boundary check over the store client module,
  not by convention alone (FR-023, FR-024, FR-026).
- **Parity is a second, independent gate.** The Panel Registry is this feature's own structural filter
  over the store, sitting downstream of 002's own filter; a store field absent from the registry
  cannot reach a template regardless of what 002's adapter returns (UP-001, and the Development
  Workflow gate's requirement to verify Principle I in this plan).
- **Captures render only when `screening_status == screened_clean`.** Anything else — `withheld`, a
  missing record, or an unrecognized future status — is rendered as unavailable; this is FR-032's
  second gate, independent of 002's own screening (FR-011).
- **No independent health/stall detection.** Run health is derived from `Run.lifecycle_state` plus the
  most recent relevant `RunEvent`s 002 already records; this feature adds no timer-based judgment of
  its own about whether a run is stuck (Principle VI — a second, possibly disagreeing detector is
  exactly the asymmetry the principle forbids) (R7).
- **LAN-reachable, unauthenticated, never beyond the LAN.** The service binds explicitly to the host's
  detected private-range (RFC1918/ULA) interface addresses, never a wildcard `0.0.0.0`, and logs every
  bound address at startup so the operator can verify reachability by inspection rather than by
  assumption (FR-028, FR-029, R8). This is deliberately more conservative than 002's own
  loopback-only operator surface in the opposite direction: 002 binds *narrower* than the LAN because
  it must never be reachable from other devices at all; this feature binds *as wide as the LAN and no
  wider*.
- **No secrets rendered.** Response and view models never carry `ModelConfig` credentials or any field
  002's own redaction (FR-043) might have missed; this is a second redaction pass at the view-model
  boundary, not a rerun of 002's (FR-030).
- **Comparison never depends on a capture.** Every comparison and trend question is answerable with
  the `screening_status` of every capture in the compared runs set to withheld (FR-035, SC-016) —
  enforced by a test that removes all capture data from the fake store and re-runs the comparison
  assertions.
- **The two surfaces never merge.** This feature's routes are additive to, and structurally separate
  from, 002's operator surface (`specs/002-civ-playing-harness/contracts/operator-surface.md`): this
  feature presents; the operator surface commands. Nothing in this codebase calls an operator-surface
  endpoint, and nothing in the operator surface's closed status schema is duplicated here (Principle
  VI, 002 plan Complexity Tracking C3).

**Scale/Scope**: Catalog of 50+ runs; individual runs of 300+ turns with, per 002's data model,
potentially hundreds of decision steps per turn. 36 functional requirements across 6 requirement
groups (live observation, shared visibility, human-parity boundary, history/replay, catalog/
comparison, access) plus 10 UI Principles that apply across all of them.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

Constitution v1.0.0. The Development Workflow section requires every plan to explicitly verify
Principle I and Principle III, and to name the in-client human action behind any new information or
action surface. This feature exists specifically to satisfy Principle VI, so that principle is
checked in full alongside I and III as directed by this feature's task brief.

### Principle I — Human-Parity Information & Action Boundary (NON-NEGOTIABLE)

**Initial check: PASS. Post-design re-check: PASS.**

This feature introduces **no new observation or action surface against the game** — it reads records
002 already wrote, through 002's port, after 002's own parity filter has already run. Principle I
therefore binds here in a narrower but still load-bearing way: this feature must not *leak* what 002
already filtered out, and must not let its own presentation logic reconstruct or infer anything the
filter withheld.

| Obligation | How this design satisfies it | Where |
|---|---|---|
| No undeclared data reaches a screen | The Panel Registry is a closed list of renderable fields; a `TurnCycleRecord`/`Observation`/`Decision` field absent from it has no template path. Registry entries validate at load time exactly as 002's catalog does — a panel without a restated `parity_basis` fails to load | [contracts/panel-registry.md](./contracts/panel-registry.md) |
| Every surface names its in-client action | Each Panel Registry entry carries a `parity_basis` string, either restated from the underlying `ParityDeclaration.parity_basis` (in-game fields) or marked `out_of_game_telemetry` (model identity, cost, latency, errors, save lineage, run configuration — FR-013) | [contracts/panel-registry.md](./contracts/panel-registry.md) |
| Captures held to the same standard, as a second gate | A capture renders only when `screening_status == screened_clean`; `withheld`, missing, or unrecognized statuses render as unavailable regardless of any other field on the record | data-model.md `CaptureView` |
| No inference reconstructs withheld data | Panels render fields verbatim from the store or mark them unavailable; none derive a value from a combination of fields in a way that could reveal something no single field discloses (e.g., no panel computes "opponent's likely research" from visible signals) | [contracts/panel-registry.md](./contracts/panel-registry.md) validation rules |
| Boundary is auditable after the fact | Registry version and content hash recorded in every response's metadata alongside the run's own `observation_catalog_version`/`action_catalog_version`, so a panel can be traced to the registry version that rendered it | data-model.md `PanelRegistryVersion` |
| This interface never reaches the playing agent | This is a separate process with no code path back into 002's agent-context assembly; nothing rendered here is ever consumed by the model call that decides game actions (satisfies FR-013's "never presented to the playing agent" by construction, not by care) | Project Structure — no shared runtime with `civsim_harness.agent` |

**New surfaces introduced by this plan, with their parity basis** (the workflow section's specific
requirement): **none against the game.** The only new "surface" this feature adds is a presentation
one — a browser page and a JSON endpoint — and both are out-of-game infrastructure with no in-client
equivalent of their own, the same way 002's operator CLI needed none. What *is* new is the second
filtering gate described above, which is a restriction, not a surface.

### Principle III — Complete Match Telemetry

**Initial check: PASS. Post-design re-check: PASS.**

This feature cannot violate the persistence obligation directly — it never writes a turn — but it can
violate its *spirit* by rendering an incomplete or superseded record as if it were confirmed fact.

- **No new write path bypasses turn-by-turn persistence, because there is no write path at all.**
  Every `MatchStore` write method is structurally unreachable from this codebase (Constraints,
  above); the workflow section's write-path check is satisfied by absence rather than by a guard.
- **Gaps are surfaced, never inferred away.** Every turn view calls `get_turn_cycle(authoritative_only=True)`
  and cross-checks `turn_gaps`/`step_gaps` before rendering; a turn or step 002 has marked missing is
  shown as incomplete, never silently skipped or rendered as if contiguous (FR-016, UP-005).
  `record_completeness_status` and `comparability_status` are read from the `Run` record, not
  re-derived by this feature — a second, independently computed completeness judgment could disagree
  with 002's and reproduce the exact asymmetry Principle VI forbids.
- **Superseded and abandoned attempts resolve to an explanation, not to silence.** A reference to a
  turn whose authoritative attempt was superseded returns the abandoned attempt's status and a pointer
  to the current authoritative one, per FR-009, rather than a 404 or the stale data.
- **Schema evolution is read-tolerant.** A field absent from an older record (predating a schema
  version) renders as "unavailable for this run's recorded schema version," never as zero or as a
  silently omitted row (FR-025) — the same obligation the constitution places on deliverable 3's
  backward-readability, applied here at the rendering boundary.

### Principle VI — Shared, Unified Observability

**Initial check: PASS. Post-design re-check: PASS.** This is the principle this feature exists to
satisfy, so it is checked at the same depth as I and III rather than summarized.

- **One code path serves both readers.** Every route builds one Pydantic view model from the store;
  content negotiation on `Accept` (or `?format=json`) chooses HTML or JSON from that same value. There
  is no second, independently maintained "API view" that could drift from the "UI view" — they are the
  same object serialized two ways (FR-007, UP-002).
- **Every view is addressable, identically, by both parties.** The URL path
  `/runs/{run_id}/turns/{turn}/panels/{panel_id}` *is* the stable reference: a user can open it in a
  browser, and the directing session can `GET` the identical path with an `Accept: application/json`
  header and receive the same content, byte-for-byte in substance (FR-008, UP-006, SC-012). See
  [contracts/web-read-api.md](./contracts/web-read-api.md).
- **Nothing shown to one party is withheld from the other.** Because both are served by the Panel
  Registry through the same route, there is no configuration axis (role, client type, header) that
  changes which fields a response includes — only how they're serialized (FR-007, spec edge case on
  concurrent runs and multi-device viewing).
- **The user never needs the game client.** Every fact FR-001–FR-006 requires is drawn from the store
  through this feature's routes; no route or page instructs or expects the user to alt-tab to
  Civilization VI (UP-008, Principle VI's second clause).
- **The two presentation surfaces stay structurally separate.** This feature's LAN-bound, read-only
  routes and 002's loopback-only, command-only operator surface never call one another and share no
  response schema; FR-027's "make what's needed to intervene visible" is satisfied by *displaying*
  `run_id`, `lifecycle_status`, and `get_last_known_good()`'s result — read from the same `MatchStore`
  this feature already depends on, not by calling the operator surface — so intervention information
  and intervention *capability* remain on opposite sides of the line 002's plan already drew (FR-026,
  FR-027, 002 plan Complexity Tracking C3).

**Gate result: PASS — proceed.** No principle is violated by this design. One dependency risk is
recorded rather than hidden: the `MatchStore` port's published read operations
(`match-store-port.md`) do not yet name a catalog-listing/filter operation that FR-018/FR-019 need:
listing all runs with their catalog columns, filtered and sorted by any of them. This is addressed in
Complexity Tracking C1 rather than treated as a blocker, on the same reasoning 002 used for building
against a port before its implementation exists.

### Principles II, IV, V, VII

**Not triggered.** This feature touches no game integration (II), no experimentation/branching
mechanics of its own (IV — it only *displays* lineage 002 already records), no optimization or
Monte Carlo work (V), and no model-provider access (VII). It is a pure reader.

## Project Structure

### Documentation (this feature)

```text
specs/001-unified-web-interface/
├── plan.md                          # This file (/speckit-plan command output)
├── research.md                      # Phase 0 output
├── data-model.md                    # Phase 1 output
├── quickstart.md                    # Phase 1 output
├── contracts/                       # Phase 1 output
│   ├── web-read-api.md              # Routes, content negotiation, view-reference scheme
│   └── panel-registry.md            # The parity second-gate, as data
├── checklists/
│   └── requirements.md              # Existing — spec quality gate
└── tasks.md                         # Phase 2 output (/speckit-tasks — this run)
```

### Source Code (repository root)

```text
panels/                              # The parity second gate, as versioned reviewable data —
├── VERSION                          #   mirrors catalogs/ in 002 for the same auditability reason
├── live.yaml                        # Panels for the live/glance view (US1)
├── history.yaml                     # Panels for turn-by-turn replay (US3)
├── catalog.yaml                     # Panels for the run catalog and comparison (US4)
└── shared.yaml                      # Panels reused across views (run header, intervention info)

src/civsim_web/
├── store_client/    # MatchStore Protocol import + a local test fake; the ONLY module allowed to
│                     #   touch the port. No write method is ever called from here (FR-023/024/026)
├── registry/        # Panel Registry loading, versioning/hashing, field-declaration validation
├── viewmodels/       # One Pydantic model per view (RunSummary, TurnView, DecisionStepView,
│                     #   CaptureView, MetricSeriesView, ComparisonView, HealthStatus, InterventionInfo)
├── health/           # Derives run health from Run.lifecycle_state + RunEvent timeline (no
│                     #   independent stall/crash detection — R7)
├── refs/             # View-reference parsing/resolution: run+turn+panel <-> URL path
├── routes/           # FastAPI routers: live, runs/catalog, turns/replay, compare, captures
├── negotiate/        # The single content-negotiation seam: view model -> HTML or JSON
├── templates/        # Jinja2 templates — landing/live, run detail, turn replay, catalog, compare
├── static/           # Vanilla JS (polling, SVG trajectory charts), CSS. No build step.
└── net/              # LAN-interface detection and explicit bind-address resolution (R8)

tests/
├── unit/            # Registry validation, view-model construction, health derivation, capture gate
├── contract/         # JSON/HTML equivalence, registry parity-basis completeness, ref round-trip
└── integration/       # Full-route behavior against the MatchStore fake
```

**Structure Decision**: Single Python project, as a sibling of `002`'s under `src/`
(`src/civsim_web/` alongside `src/civsim_harness/`), not nested inside it — this feature is a
separate deployable process with its own dependency graph (a web framework, templates, static assets)
that 002's harness process has no reason to import, and 002's harness has no reason to import this
feature's code either. Collapsing the two into one package would blur exactly the write/read boundary
Principle VI depends on being structural. `panels/` is deliberately not Python, for the same reason
`catalogs/` in 002 is not: an auditor checking SC-005 reads declarations, not control flow. Test tiers
mirror 002's naming (`unit`/`contract`/`integration`) for a consistent project-wide vocabulary; this
feature has no `live` tier of its own since it never touches the game client, only the store.

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| **C1** — Depending on a `MatchStore` catalog-listing/filter read operation that `contracts/match-store-port.md` does not yet name | FR-018/FR-019 require listing every recorded run with seed, civilization, ruleset, model, turn count, outcome metrics, completeness status, and start/end times, filterable and sortable by any of them — no combination of the port's currently published reads (`get_run`, `list_active_runs`, `turn_gaps`, `step_gaps`, ...) produces this without either the harness or the store growing a new indexed read. | Building the catalog from `list_active_runs()` plus a per-run `get_run()` call was rejected because `list_active_runs` is documented for **active** runs (recovery/run-identity use), not the full historical catalog FR-018 requires, and fetching every historical run's full record to project six summary columns would not meet SC-008's 2-second target at 50+ runs. This is recorded as a dependency to raise with deliverable 3's design, not resolved unilaterally here, since the port contract belongs to that deliverable. |
| **C2** — Building and testing against a `MatchStore` fake rather than a real store | Deliverable 3 (the match-tracking store) does not exist yet, and 002's own local reference adapter is being built concurrently by a separate effort this plan must not depend on completing first. | Blocking this feature's design and test-writing on either deliverable 3 or 002's adapter landing was rejected for the same reason 002 rejected blocking on deliverable 3 in its own plan (C2): a published contract already exists to build against, and serializing three deliverables that could otherwise proceed in parallel wastes the parallelism the contract exists to enable. |
| **C3** — A second, independent parity filter (the Panel Registry) over data 002 already parity-filtered | FR-010/FR-012/UP-001 require this feature's own panels to be auditable against parity independently of 002's filter — "the parity filter is applied before this interface enforces it at the panel level" (spec Assumptions) means this feature cannot simply trust the upstream filter without its own declared, checkable boundary. | Trusting 002's filter alone was rejected because it would leave this feature with no structural defense if a future 002 change (or its eventual deliverable-3 replacement) ever returned a field 002 no longer filters correctly — the two gates are cheap insurance for a NON-NEGOTIABLE principle, not redundant caution. |
