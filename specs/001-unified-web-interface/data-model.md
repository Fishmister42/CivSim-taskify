# Phase 1 Data Model: Unified Web Interface

**Feature**: `001-unified-web-interface` | **Date**: 2026-09-20 | **Plan**: [plan.md](./plan.md)

Scope note: this feature owns no persisted storage schema. Every entity below is a **view model** —
a Pydantic shape assembled at request time from 002's `MatchStore` port records
(`specs/002-civ-playing-harness/data-model.md`) and this feature's own Panel Registry
(`contracts/panel-registry.md`). None of these types are written back anywhere; they exist only to be
serialized as JSON or rendered into HTML by the single content-negotiation seam described in the plan.

Two things are true of every view model here and are stated once rather than repeated per entity:

- **It is the same object for both readers.** The JSON a machine caller receives and the values a
  template renders come from one constructed instance; there is no second, hand-maintained "API
  shape" that could drift from the "display shape" (Principle VI, FR-007).
- **Every field traces to a Panel Registry entry.** A field that cannot cite a registry entry's
  `parity_basis` (or `out_of_game_telemetry` marking) has no path onto any of these models — see
  [contracts/panel-registry.md](./contracts/panel-registry.md) for the registry schema itself.

---

## Entity overview

```text
MatchStore (002's port, read-only)
   │
   ├──< RunSummaryView ─────────────< used by the Catalog (US4) and Live (US1) landing views
   ├──< RunDetailView ──< HealthStatus
   │         │            └──< InterventionInfo   (FR-027 — never a control)
   │         └──< TurnCycleView ──< DecisionStepView ──< ObservationEntryView
   │                    │                    ├──< DecisionView ──< ModelCallView
   │                    │                    └──< CaptureView
   │                    └──< RunEventView (timeline)
   ├──< MetricSeriesView ──< ComparisonView   (US4, never depends on CaptureView — FR-035)
   └──< ViewReference   (resolves to exactly one of the above + a Panel)

PanelRegistryVersion ──< PanelDeclaration
       └── cited by every view model's `panel_basis` metadata
```

`DecisionStepView` mirrors 002's `DecisionStep` one-for-one rather than re-aggregating it, because
002's own data model made the step (not the turn) the unit at which record completeness and
reconstructability are asserted (SC-003 in 002's spec) — collapsing steps back into a single
turn-level blob here would silently discard exactly the granularity 002 went to the trouble of
recording.

---

## 1. RunSummaryView

*Projection of 002's `Run` + `RunConfiguration` for catalog rows and live-landing headers* (FR-018,
FR-001).

| Field | Type | Notes |
|---|---|---|
| `run_id` | id | |
| `seed` | string | From `RunConfiguration.map_seed` |
| `civilization` / `leader` | string | |
| `ruleset` | string | |
| `model_primary` | string | `RunConfiguration.model_config.primary` — out-of-game telemetry, never shown as if it were the agent's in-game identity |
| `lifecycle_state` | enum | Verbatim from `Run.lifecycle_state` |
| `health` | HealthStatus | See below — derived, not a raw store field |
| `turn_count` | int | Highest turn number with an authoritative attempt |
| `outcome_metrics` | object | Headline metric values at the run's last recorded turn (e.g. science, culture) — sourced from `MetricSeriesView`, not independently computed |
| `record_completeness_status` | enum | Verbatim from `Run.record_completeness_status` — never re-derived here (plan Constitution Check, Principle III) |
| `comparability_status` | enum | Verbatim from `Run.comparability_status` |
| `started_at` / `ended_at` | timestamp? | |
| `parent_run_id` / `parent_turn` | id? / int? | Branch lineage, display-only |

**Validation**:

- `health` is always present, even for a `finished` run (health for a finished run is simply
  `finished`, not absent) — a missing health value would be indistinguishable from a loading state
  (UP-005).
- `outcome_metrics` keys are whatever `MetricSeriesView` names for that run; this view never
  hard-codes "science and culture" as the only possible keys, since the spec's Assumptions state the
  comparison view "is expected to handle any per-turn metric the store records."
- A run whose `record_completeness_status != complete` still appears in the catalog (FR-016 requires
  marking, not hiding) but is excluded from default comparison selection per FR-021 — see
  `ComparisonView`.

---

## 2. HealthStatus

*Derived, not a raw store field* — see research R7.

| Field | Type | Notes |
|---|---|---|
| `state` | enum | `running` \| `waiting_on_model` \| `waiting_on_game` \| `stalled` \| `crashed` \| `resumed` \| `finished` (FR-003, verbatim vocabulary) |
| `reason_event_id` | id? | The `RunEvent` that justifies a non-`running`/`finished` state, e.g. the `crash_detected` or `hang_detected` event |
| `since` | timestamp | When the current `state` began |

**Derivation rule** (this is a rule, not a free-standing field, and belongs here because it is the one
piece of real logic this view model contains): `state` is computed from `Run.lifecycle_state` first
(`preparing`/`playing` → `running`; `waiting_on_model`/`waiting_on_game` map directly; `interrupted`/
`failed` → `crashed`; `resuming` → `resumed`; `finished` → `finished`), refined only by the *presence*
of a matching, more specific `RunEvent` at or after the lifecycle transition — never by an independent
timestamp comparison this feature invents (research R7). If no such event exists, the plain
lifecycle-derived value stands.

**Validation**: `stalled` may only be set when a `RunEvent` of type `hang_detected` or
`unresponsive_detected` exists for this run with no later `resumed`/`playing` transition after it —
never inferred from "no new turn recently" alone, which would be exactly the second, independently
computed judgment R7 rules out.

---

## 3. RunDetailView

*The landing view for a single run — the "glance" of UP-003* (FR-001, FR-006).

| Field | Type | Notes |
|---|---|---|
| `summary` | RunSummaryView | |
| `current_turn` | TurnCycleView? | The latest authoritative turn; null only for a run with zero authoritative turns yet (edge case: opened before first turn recorded) |
| `latest_decision` | DecisionView? | Convenience pointer to `current_turn`'s last decision step's decision, so FR-001's "single landing view, no navigation required" does not require drilling into the turn to find it |
| `last_confirmed_current_at` | timestamp | When *this response* was assembled — the FR-002 currency indicator; distinct from `HealthStatus`, see research R7 |
| `intervention_info` | InterventionInfo | Always present, never conditional — FR-027 |
| `recent_events` | list[RunEventView] | Bounded window (e.g., last 20) for the landing view; the full timeline is a separate paginated route |

**Validation**: `current_turn` being null is rendered as an explicit empty state ("no turns recorded
yet"), never as a blank panel or a zero-filled turn (spec Edge Cases: "No runs exist yet, or the
interface is opened before the first turn is recorded").

---

## 4. InterventionInfo

*FR-027 — what the user needs to act elsewhere, never a control* (UP-010).

| Field | Type | Notes |
|---|---|---|
| `run_id` | id | |
| `lifecycle_status` | enum | Same vocabulary as `Run.lifecycle_state` |
| `last_known_good_turn` | int? | From `MatchStore.get_last_known_good()` |
| `last_known_good_save_id` | id? | Same source |

**Validation**: this model has **no action fields** — no start/pause/stop/branch method, no button
target, nothing a template could wire to a mutating request. This is enforced structurally: the type
itself carries no callable reference, only display data, so there is nothing for a future contributor
to accidentally wire a control to (FR-026, UP-010).

---

## 5. TurnCycleView

*Projection of 002's `TurnCycleRecord`, unpacked into an ordered `DecisionStepView` list* (FR-014).

| Field | Type | Notes |
|---|---|---|
| `run_id` / `turn_number` | id / int | |
| `attempt_index` | int | |
| `is_authoritative` | bool | Verbatim |
| `outcome` | enum | `ended_by_agent` \| `ended_on_no_progress` \| `abandoned` — verbatim, never relabeled |
| `steps` | list[DecisionStepView] | In `step_index` order; see lazy-loading note below |
| `yields` | object | Verbatim per-turn yields |
| `started_at` / `ended_at` | timestamp | |
| `completeness` | TurnCompleteness | See below |
| `superseded_by` | int? | Set when this attempt is not authoritative and a later attempt exists — resolves FR-009 |

### TurnCompleteness

| Field | Type | Notes |
|---|---|---|
| `is_complete` | bool | `true` only if this turn number is absent from `turn_gaps(run_id)` and has no entries in `step_gaps(run_id, turn_number)` |
| `missing_step_indices` | list[int] | Verbatim from `step_gaps` |

**Validation**:

- A `TurnCycleView` for a turn number present in `turn_gaps()` is never constructed as if it were a
  normal turn — the route instead returns an explicit "turn not recorded / gap" response naming the
  gap, so a client cannot mistake absence for a boring, complete turn (FR-016).
- **Lazy step loading** (research R6): the route accepts a step-range query; `steps` on the default
  response is a bounded window (e.g., first N and a cursor), with the full ordered list available by
  paging. The `step_index` ordering guarantee is preserved regardless of pagination — no page may skip
  an index without marking it, which would look identical to a genuine gap and must not.

---

## 6. DecisionStepView

*Projection of 002's `DecisionStep` + its `Observation`, `Decision`, and `ModelCall`* (FR-012).

| Field | Type | Notes |
|---|---|---|
| `step_index` | int | |
| `observation` | list[ObservationEntryView] | Every entry the agent saw at this step, parity-filtered twice over (002's filter, then the Panel Registry) |
| `capture` | CaptureView | See below — always present as a value, even when unavailable |
| `decision` | DecisionView | |
| `progress` | enum | `changed_state` \| `no_change` \| `rejected` — verbatim |

**Validation** (UP-004 — "decisions carry their evidence"): `observation`, `capture`, and `decision`
are non-optional siblings on this one model. There is no route that can return a `decision` without
its `observation`, which is the structural version of UP-004's "a decision is never shown as a bare
action."

### ObservationEntryView

| Field | Type | Notes |
|---|---|---|
| `label` | string | Human-readable, sourced from the Panel Registry entry's `title`, not the raw store `key` |
| `value` | json | Verbatim `ObservationEntry.value` |
| `panel_basis` | string | The Panel Registry entry's restated `parity_basis` — present on every entry, unconditionally (SC-005's per-panel auditability) |

**Validation**: an `ObservationEntry.declaration_id` that does not resolve to a Panel Registry entry
is **dropped**, not passed through with a placeholder label — the registry is a closed list (plan
Constitution Check, Principle I). This is expected to be rare (002's own catalog already filters), but
the rule holds regardless of how rare, since "rare" is not "never."

---

## 7. CaptureView

*The second gate on 002's `ScreenCapture`, independent of 002's own screening* (FR-032, FR-034).

| Field | Type | Notes |
|---|---|---|
| `available` | bool | `true` only if the source record's `screening_status == "screened_clean"` |
| `image_url` | string? | Present only when `available`; a lazy-loadable route, never inlined as a data URI (FR-036 — viewing a turn must not require loading captures beyond those being viewed) |
| `unavailable_reason` | enum? | Set only when `!available`: `withheld` \| `capture_failed` \| `never_captured` \| `missing_record` — out-of-game telemetry, safe to display (FR-013) |
| `captured_at` | timestamp? | Present only when `available` |

**Validation — the load-bearing rule of this whole model**: `available` is computed as
`screening_status == "screened_clean"`, full stop. Any other value — `withheld`, an absent capture
record, a `screening_status` value this feature's code does not recognize (e.g. a future schema
addition) — resolves to `available = false`. This is a fail-closed default: an unrecognized status is
treated as unavailable, never as clean-by-default, so a future 002 schema change cannot silently
un-gate a capture this feature was never updated to understand (plan Constraints — captures render
only when explicitly `screened_clean`).

---

## 8. DecisionView

*Projection of 002's `Decision` + `ActionExecution`* (FR-012, UP-004).

| Field | Type | Notes |
|---|---|---|
| `action_label` | string | From the Panel Registry / underlying `ParityDeclaration.summary`, not the raw `action_declaration_id` |
| `parameters` | json | Verbatim |
| `reasoning` | string | Verbatim `Decision.reasoning`; rendered even when empty or very long — see Validation |
| `is_end_turn` | bool | Verbatim |
| `execution_outcome` | enum | `applied` \| `rejected` \| `partially_applied` — verbatim |
| `rejection_reason` | enum? | Verbatim, shown when present |
| `model_call` | ModelCallView | |

**Validation** (spec Edge Case: "the agent's reasoning for a turn is very long, empty, or malformed"):
an empty `reasoning` renders as an explicit "no reasoning recorded" label, never as a blank space that
could be mistaken for a loading state; a very long `reasoning` is truncated in the collapsed panel
view with an expand affordance, never silently cut off with no indication more text exists — both
panels still identify the action taken regardless of what happened to the reasoning text (UP-005).

### ModelCallView

| Field | Type | Notes |
|---|---|---|
| `model_served` | string | Out-of-game telemetry (FR-013, FR-005) |
| `latency_ms` | int | |
| `cost` | object | Verbatim provider-reported usage |
| `fallback_occurred` | bool | |
| `outcome` | enum | Verbatim |

**Validation**: every field on this model is explicitly marked `out_of_game_telemetry` in the Panel
Registry (never `parity_basis`) — FR-013 requires this data be treated as harness telemetry, visible
to the user and session, and it is rendered in a visually distinct "cost & latency" sub-panel rather
than interleaved with in-game observation entries, so the two categories are never visually
ambiguous even though both appear on the same page (UP-001's "requires deliberately bypassing the
filter," applied here to category confusion rather than field leakage).

---

## 9. RunEventView

*Projection of 002's `RunEvent`, for the timeline* (FR-004).

| Field | Type | Notes |
|---|---|---|
| `event_type` | enum | Verbatim from 002's `RunEvent.event_type` vocabulary |
| `turn_number` / `step_index` | int? | Verbatim, where applicable |
| `occurred_at` | timestamp | |
| `detail` | json | Verbatim `RunEvent.detail` — already credential-redacted per 002's contract (FR-043); this feature does not re-redact but does verify redaction in its contract-test audit (see plan Testing) |

---

## 10. MetricSeriesView

*One named per-turn measurement across a run* (FR-020).

| Field | Type | Notes |
|---|---|---|
| `run_id` | id | |
| `metric_name` | string | e.g. `science_output`, `culture_output` — not a closed enum, per spec Assumptions |
| `points` | list[MetricPoint] | `{turn: int, value: number}`, ordered by turn |

**Validation**: `points` never includes a turn present in that run's `turn_gaps()` as if it were a
real value — a gapped turn is either omitted (with the gap visible elsewhere on the chart, e.g. a
break in the line) or explicitly flagged, never interpolated or zero-filled (FR-025, UP-005).

---

## 11. ComparisonView

*Several runs' `MetricSeriesView`s on common axes* (FR-020, FR-021, FR-022).

| Field | Type | Notes |
|---|---|---|
| `runs` | list[RunSummaryView] | The compared set, in the order selected |
| `series` | dict[metric_name, list[MetricSeriesView]] | One series per compared run, per metric |
| `quarantined_run_ids` | list[id] | Runs excluded or flagged for `record_completeness_status != complete` (FR-021) |
| `divergence_points` | list[DivergencePoint] | Precomputed turns where the leading run changes, or values separate beyond a threshold |

### DivergencePoint

| Field | Type | Notes |
|---|---|---|
| `turn` | int | |
| `metric_name` | string | |
| `leader_run_id` | id | |
| `refs` | dict[run_id, ViewReference] | One ready-to-navigate reference per compared run at this turn, so a client can jump to "this turn, in each compared run" (FR-022) with no extra round trip |

**Validation**: this entire model is constructed with `CaptureView` nowhere in its type — not merely
unused, but structurally absent, so a future addition to this view cannot accidentally introduce a
capture dependency into a comparison question without changing the type (FR-035, SC-016). A run in
`quarantined_run_ids` still appears in `runs` (so the user can see *why* it's excluded) but is excluded
from `series` and `divergence_points` computation, satisfying FR-021's "excluded from or visibly
quarantined."

---

## 12. ViewReference

*The stable identifier shared between the user and the directing session* (FR-008, FR-009, UP-006).

| Field | Type | Notes |
|---|---|---|
| `run_id` | id | |
| `turn_number` | int? | Absent for run-level or catalog-level references |
| `panel_id` | string? | A Panel Registry entry id; absent resolves to the whole turn or run |
| `step_index` | int? | Present only for step-scoped panels |

**Canonical serialization**: `/runs/{run_id}/turns/{turn_number}/panels/{panel_id}` (with
`/steps/{step_index}` inserted when step-scoped), matching the route structure in
[contracts/web-read-api.md](./contracts/web-read-api.md) exactly — the reference *is* the URL path,
not a separate encoding that maps to one.

**Validation**:

- Resolving a `ViewReference` whose `turn_number` names a superseded/abandoned attempt returns that
  attempt's `TurnCycleView` with `is_authoritative = false` and `superseded_by` set, rather than 404 or
  the current authoritative turn silently substituted (FR-009).
- Resolving a `ViewReference` against a run or turn that does not exist at all (never existed, not
  merely superseded) is the one case that *does* 404 — distinct from "exists but superseded," which
  spec FR-009 requires to explain itself rather than look like a dead link.
- Two resolutions of the identical reference string, one from the browser and one from the directing
  session's own `GET`, are guaranteed identical by construction (same route, same view-model
  constructor, differing only in the final serialization step) — this is what SC-012's "100% of
  attempts" is a property of the code path rather than a percentage to test toward.

---

## 13. PanelRegistryVersion / PanelDeclaration

*The Panel Registry's own version record, referenced by every response's metadata.* Full schema in
[contracts/panel-registry.md](./contracts/panel-registry.md); summarized here for completeness of the
entity graph.

| Field | Type | Notes |
|---|---|---|
| `version` | string | From `panels/VERSION` |
| `content_hash` | string | Hash over all registry files |
| `panel_ids` | list[string] | The full set in force |

Every top-level response (`RunDetailView`, `TurnCycleView`, `ComparisonView`, catalog listings)
includes this version alongside the underlying run's own `observation_catalog_version` /
`action_catalog_version` from 002, so a panel can always be traced to *both* the game-side catalog
version that produced the underlying data and the registry version that decided how to display it
(plan Constitution Check, Principle I — "boundary is auditable after the fact").

---

## Cross-cutting invariants

| # | Invariant | Requirements |
|---|---|---|
| **V1** | No field reaches any view model without a Panel Registry entry naming its `parity_basis` or marking it `out_of_game_telemetry` | FR-010, FR-012, UP-001 |
| **V2** | `CaptureView.available` is `true` if and only if the source `screening_status == "screened_clean"`; every other value, including unrecognized future values, is `false` | FR-032, FR-034 |
| **V3** | `HealthStatus.state` is derived only from `Run.lifecycle_state` and 002's own `RunEvent`s — never from an independently computed staleness heuristic | Principle VI, research R7 |
| **V4** | A turn or step present in `turn_gaps()`/`step_gaps()` is never rendered as if it were a normal, complete turn | FR-016, UP-005 |
| **V5** | `record_completeness_status` and `comparability_status` are always read verbatim from `Run`, never re-derived by this feature | Principle III |
| **V6** | A superseded/abandoned turn attempt resolves to an explanatory view, never to silent substitution of the current authoritative turn | FR-009 |
| **V7** | `ComparisonView` and everything it contains has no field, direct or transitive, typed as `CaptureView` | FR-035, SC-016 |
| **V8** | The identical `ViewReference` string, resolved by any caller, produces an identical view model before serialization | FR-008, SC-012 |
| **V9** | `InterventionInfo` carries no field through which a mutating request could be issued | FR-026, UP-010 |
| **V10** | Every top-level response names both the run's game-side catalog version and this feature's Panel Registry version | SC-005 |
