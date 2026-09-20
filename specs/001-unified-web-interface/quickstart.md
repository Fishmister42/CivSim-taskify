# Quickstart & Validation Guide: Unified Web Interface

**Feature**: `001-unified-web-interface` | **Date**: 2026-09-20 | **Plan**: [plan.md](./plan.md)

How to stand this interface up and prove it satisfies its spec. Each scenario maps to a user story's
Independent Test and names the success criteria it discharges. Scenarios 1–5 run against the
`MatchStore` fake (research R3) and need no live harness or game client; Scenario 6 is the one manual
check that needs a real 002 process, once one exists.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.12+ and `uv` | Same toolchain as `002-civ-playing-harness` |
| A `MatchStore` implementation reachable at startup | The bundled fake for scenarios 1–5; 002's local reference adapter, or deliverable 3, for Scenario 6 |
| No Civilization VI client needed at any point | This feature never talks to the game (FR-023) |

## Setup

```bash
uv sync
uv run civsim-web doctor      # preflight: store reachable, Panel Registry loads clean, bind address resolved
```

`doctor` must report all green (five lines; addresses will differ):

```text
store             : ok (ping succeeded)
panel registry    : ok (version 1, 37 panels, frozen)
registry coverage : ok (167 fields scanned, 91 marked out-of-game, 27 registered, 49 unregistered and unrendered, 0 unregistered fields reachable from a view model)
routes            : ok (13 registered, all 13 contract routes present)
bind address(es)  : 192.168.1.42:8420, 127.0.0.1:8420   (LAN + loopback — no wildcard)
```

A registry that fails to load (a panel missing `parity_basis`, a duplicate `panel_id`, a
`source_fields` entry that no longer exists in 002's data model) aborts startup rather than serving a
partially-validated registry — the same discipline 002's `doctor` applies to its own catalog.

The **coverage** count is computed, not asserted: `registry/coverage.py` walks 002's `data-model.md`
and this feature's view models and reports how many fields 002 records could render with no panel
permitting them. That number must be `0`; `doctor` exits non-zero if it is not. (Before T062 the line
printed a literal `0`, which is the kind of self-confirming preflight this project has learned to
distrust.)

**`frozen`** means `panels/VERSION.lock` exists and the shipped declarations still hash to what it
records — registry rule P6 in force. If it says `NOT FROZEN`, a warning line follows and declarations
can be edited in place with nothing objecting; that is legitimate only while a version is still under
authorship. Adding or changing a panel after the freeze means bumping `panels/VERSION` and recording
the new version's block in `VERSION.lock`.

The **routes** line builds the application and checks every path `contracts/web-read-api.md` names is
actually registered. It is the end-to-end half of the preflight: a router that fails to import is a
`doctor` failure rather than a 404 discovered later.

```bash
uv run civsim-web serve       # starts the service; logs every bound address
```

---

## Scenario 1 — Watch a live run without opening the game

**Validates**: User Story 1 · FR-001 – FR-006 · SC-001, SC-002, SC-003, SC-009

With the fake store seeded to simulate a run advancing one turn every few seconds:

```bash
uv run pytest tests/integration/test_live_view.py -k advancing_run
```

Then, manually, open `http://<bound-address>:8420/` in a browser and watch for ten simulated turns
without touching anything else.

**Expected**:

- Current turn number, most recent decision with its stated reasoning, current visible game state,
  and a health indicator are all visible on `/runs/{run_id}` with no click required (FR-001).
- Each new simulated turn appears within 5 seconds without a manual reload (FR-002, SC-003).
- The page shows a "last confirmed current at HH:MM:SS" indicator that updates every poll (FR-002).
- Stopping the fake store's advancement (simulating a stall) surfaces as `stalled` within 60 seconds,
  distinguishable from a merely slow turn still in progress (FR-003, SC-009).
- A capture is shown beside the structured panels for the current turn when one exists; when the fake
  marks a step's capture `withheld`, the panel still renders with the capture explicitly unavailable
  (FR-031, FR-034).
- No control of any kind is present anywhere on the page — only `run_id`, `lifecycle_status`, and the
  last-known-good save/turn (FR-026, FR-027).

```bash
uv run pytest tests/unit/test_health_derivation.py
# expect: 'stalled' only ever set from a hang_detected/unresponsive_detected event, never from
#         an independently computed timestamp gap
```

---

## Scenario 2 — Same view for the user and the directing session

**Validates**: User Story 2 · FR-007 – FR-009 · SC-004, SC-012

```bash
curl -s http://<bound-address>:8420/runs/<run_id>/turns/<turn> -H 'Accept: application/json' | jq .
```

Open the same path in a browser at the same moment.

**Expected**:

- Every field visible on the rendered page appears in the JSON body, and vice versa — no field in one
  and not the other (FR-007).
- The exact URL, shared as text, resolves to the identical run/turn/panel from both the browser and a
  second `curl` call (FR-008, SC-012).
- Requesting a reference to a turn attempt that has since been superseded returns that attempt's data
  with `is_authoritative: false` and a `superseded_by` turn/attempt pointer — never the current
  authoritative turn silently substituted (FR-009).

```bash
uv run pytest tests/contract/test_web_read_api.py -k json_html_parity
# expect: 100% field parity across every route in the matrix
```

**SC-004's audit, made mechanical**: the same contract test doubles as the "every panel presented to
the user is retrievable by the session and vice versa" audit — it is not a manual release checklist
item, it runs on every build.

---

## Scenario 3 — Replay and inspect a completed run turn by turn

**Validates**: User Story 3 · FR-014 – FR-017 · SC-006, SC-010, SC-011

Seed the fake store with a completed run that includes: a gap (a turn number with no authoritative
attempt), an abandoned-then-replayed turn (a crash/resume pair), and a turn with a withheld capture.

```bash
uv run pytest tests/integration/test_replay.py
```

Then, manually: open the run cold and answer "what did the agent do on turn 23 and why" using only the
interface, timing yourself against the 30-second / 3-interaction bound (SC-006).

**Expected**:

- Any turn's state, decision, reasoning, and yields render as they were at that turn.
- The gapped turn is visibly marked incomplete, and the run itself is flagged unfit for trend
  comparison — not silently skipped in the turn sequence (FR-016, SC-010, SC-011).
- The abandoned/replayed pair both appear in the turn's attempt history, with the crash and resume
  visible as timeline events (FR-017).
- Stepping forward/backward between adjacent turns preserves the current panel/focus rather than
  resetting to the landing view (spec Acceptance Scenario US3 §3).

---

## Scenario 4 — Compare runs and spot trends across them

**Validates**: User Story 4 · FR-018 – FR-022 · SC-007

Seed the fake store with at least five completed runs, one of them incomplete.

```bash
curl -s 'http://<bound-address>:8420/compare?runs=r1,r2,r3,r4,r5&metrics=science_output,culture_output' \
  -H 'Accept: application/json' | jq .
```

**Expected**:

- The catalog (`/runs`) lists all five with seed, civilization, ruleset, model, turn count, outcome
  metrics, and completeness status, filterable/sortable by any of those (FR-018, FR-019).
- The comparison view shows all five trajectories on common axes, with the incomplete run excluded
  from or visibly quarantined within the comparison rather than plotted as if comparable (FR-021).
- Selecting a divergence point's `refs` for each compared run opens that exact turn in each run
  (FR-022) — verify by following one `ref` per compared run and confirming the turn numbers match the
  divergence point's `turn` field.
- Time the whole exercise: leader and divergence-turn identification should complete in under two
  minutes (SC-007).

```bash
uv run pytest tests/contract/test_web_read_api.py -k comparison_never_needs_captures
# expect: identical ComparisonView output whether every capture in the fixture is available or
#         entirely withheld — the comparison answer must not change either way (FR-035, SC-016)
```

---

## Scenario 5 — The parity and capture audits, made mechanical

**Validates**: FR-010 – FR-013, FR-032 · SC-005, SC-014

```bash
uv run pytest tests/contract/test_panel_registry.py
uv run pytest tests/contract/test_web_read_api.py -k capture_fail_closed
```

**Expected**:

- Every `in_game`-category panel in the registry has a non-empty `parity_basis`; the registry fails to
  load otherwise (SC-005, made release-blocking by running in CI rather than only per-release by hand).
- A capture matrix covering `screened_clean`, `withheld`, a missing record, and an intentionally
  unrecognized future `screening_status` value all resolve `CaptureView.available` correctly — only
  `screened_clean` is `true`, everything else, including the unknown value, is `false` (SC-014's
  "zero displayed captures contain non-player UI," verified here as "the fail-closed rule holds for
  every status this feature knows to check *and* for one it doesn't").
- No harness telemetry field (model identity, cost, latency, retries, save lineage, run configuration)
  appears anywhere flagged `category: in_game` in the registry (FR-013).

---

## Scenario 6 — Against a real 002 process (manual, once 002 exists)

**Validates**: R3's dependency risk resolved in practice; the plan's Complexity Tracking C1/C2

Once a real 002 harness is running against its local reference adapter (or deliverable 3 lands):

```bash
export MATCH_STORE_URL=<002's configured store location>
uv run civsim-web serve
```

**Expected**: swapping the fake for the real `MatchStore` implementation requires **no code change**
in this feature — only a configuration value — since every route and view model is written against
the Protocol, not the fake's internals (research R3). If this swap requires touching `viewmodels/` or
`routes/`, that is itself a finding: it means a dependency leaked past `store_client/`, the one module
this plan designates as the seam.

---

## CI-runnable subset

Scenarios 1–5 need no live client and no live 002 process — they run entirely against the
`MatchStore` fake and gate every build:

```bash
uv run pytest tests/unit tests/contract tests/integration
```

Scenario 6 is manual and cannot be a CI tier of its own, since it depends on a live 002 process this
feature does not control the lifecycle of.

## Reference

- Requirement-by-requirement detail: [spec.md](./spec.md)
- View-model shapes and invariants: [data-model.md](./data-model.md)
- Route and panel-registry contracts: [contracts/](./contracts/)
- Decisions and their rationale: [research.md](./research.md)
