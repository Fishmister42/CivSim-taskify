# Contract: Panel Registry

**Feature**: `001-unified-web-interface` | **Schema version**: 1

This is this feature's own structural parity gate — the mechanism behind UI Principle UP-001 ("parity
by construction") and this plan's Constitution Check for Principle I. It exists because the parity
filter 002 already applies is upstream and out of this feature's control; this registry is the second,
independent gate the plan commits to, so that a field 002 fails to filter correctly still cannot reach
a screen here without someone deliberately adding it to this registry first.

It is modeled directly on 002's own capability catalog
(`specs/002-civ-playing-harness/contracts/capability-catalog.md`-equivalent pattern: versioned YAML
under a top-level, non-Python directory, loaded and validated at startup, immutable within a version).

## Location and format

```text
panels/
├── VERSION          # version string, recorded in every response's metadata
├── live.yaml         # panels for the live/glance view (US1)
├── history.yaml       # panels for turn-by-turn replay (US3)
├── catalog.yaml       # panels for the run catalog and comparison (US4)
└── shared.yaml        # panels reused across views (run header, intervention info, event timeline)
```

## Panel declaration schema

```yaml
- panel_id: current_turn.city_yields          # stable, human-readable
  title: "City Yields"                         # shown in the UI; also ObservationEntryView.label's source
  story: [US1, US3]                            # which user stories render this panel
  scope: step                                   # run | turn | step  — matches ViewReference granularity
  source_fields:                                # store fields this panel is permitted to read
    - Observation.entries[declaration_id=cities.yields]
  parity_basis: "Opening a city's Yields tab in the in-game City Panel"
  category: in_game                             # in_game | out_of_game_telemetry
  introduced_in_version: "1"

- panel_id: turn.model_call_cost
  title: "Model Call Cost & Latency"
  story: [US1, US2, US3]
  scope: step
  source_fields:
    - ModelCall.latency_ms
    - ModelCall.cost
    - ModelCall.model_served
  parity_basis: null                             # required to be null exactly when category is out_of_game_telemetry
  category: out_of_game_telemetry
  introduced_in_version: "1"
```

| Field | Type | Notes |
|---|---|---|
| `panel_id` | string | Stable, referenced by `ViewReference` and by URL paths verbatim |
| `title` | string | Human-facing label; also what `ObservationEntryView.label` and `DecisionView.action_label` draw from instead of a raw store key |
| `story` | list[enum] | `US1` \| `US2` \| `US3` \| `US4` — at least one, for traceability from spec to registry |
| `scope` | enum | `run` \| `turn` \| `step` — must match the granularity of every field it declares |
| `source_fields` | list[string] | Dotted references into 002's data-model entities; a field not listed here cannot be read by this panel's view-model constructor |
| `parity_basis` | string? | **Required, non-empty, when `category: in_game`.** The in-client action a human takes to obtain the same information — restated from the underlying `ParityDeclaration.parity_basis` where one exists, or authored fresh for information that is a display-level aggregation of several declared fields |
| `category` | enum | `in_game` \| `out_of_game_telemetry` — exactly one; determines which validation rule below applies |
| `introduced_in_version` | string | Registry version of first appearance |

## Validation (load-time — a load failure blocks startup, same discipline as 002's catalog)

| # | Rule | Rationale |
|---|---|---|
| **P1** | A panel with `category: in_game` and an empty/missing `parity_basis` fails to load | This is the field SC-005's per-panel audit reads; a panel without one is not auditable by construction (mirrors 002's `ParityDeclaration` validation) |
| **P2** | A panel with `category: out_of_game_telemetry` must have `parity_basis: null` | Prevents a telemetry panel from acquiring a fabricated in-client justification it does not have — FR-013 requires harness telemetry be treated as out-of-game, not laundered into looking like a parity-cleared observation |
| **P3** | Every entry in `source_fields` must resolve to a field actually present in 002's published `data-model.md` for that entity | Catches drift between this registry and 002's schema at load time rather than at render time |
| **P4** | `panel_id` is unique across all four YAML files | One namespace; a duplicate is ambiguous for `ViewReference` resolution |
| **P5** | `scope` must be consistent with every listed `source_fields` entry's own granularity (e.g. a `run`-scoped panel cannot list a `DecisionStep`-level field) | A mismatch here is exactly the kind of aggregation that could smuggle step-level detail into what looks like a run-level summary |
| **P6** | Declarations are immutable within a version; changing one is a new registry version | Mirrors 002's `ParityDeclaration` immutability rule, for the same auditability reason (a run's rendering should be traceable to the exact registry version active when it was viewed) |

## How a view model consumes this registry

`src/civsim_web/registry/` loads and validates the four YAML files once at startup (P1–P6), producing
an in-memory index from `panel_id` to declaration and from `(entity, field)` to the panel(s) permitted
to read it. Every view-model constructor in `src/civsim_web/viewmodels/` consults this index rather
than reading store fields directly — there is no code path from a raw `MatchStore` record to a
response that does not pass through a panel lookup. A store field with no registered panel is simply
never read by any constructor; it is not filtered *after* being read, it is never reached at all,
which is the "requires deliberately bypassing the filter rather than forgetting to apply one" property
UP-001 asks for.

## Conformance tests

`tests/contract/test_panel_registry.py` asserts P1–P6 against the shipped YAML, plus:

- **SC-005 audit**: every panel with `category: in_game` has a non-empty `parity_basis` — this test
  *is* the release-blocking audit the requirements checklist and spec SC-005 call for, runnable in CI
  rather than only manually per release.
- **Coverage**: every field enumerated in 002's `data-model.md` that is not explicitly marked
  out-of-game in that document either has a corresponding panel here or is confirmed absent from every
  view model by a reflection-based test — so a newly added store field defaults to *invisible* until
  someone deliberately registers it, never to *visible-by-omission*.
