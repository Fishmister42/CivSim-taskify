# Implementation Plan: Match-Tracking Data Store

**Branch**: `live/linux` (feature `003-match-tracking-store`) | **Date**: 2026-09-21 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/003-match-tracking-store/spec.md`

## Summary

Give the match-tracking store an owner and a published contract. The reference adapter that 002
built (`src/civsim_harness/store/sqlite_adapter.py`) already satisfies Principle III's floor; this
feature widens it, behind a new `MatchTrackingStore` Protocol that extends 002's `MatchStore`
unchanged, to: record model calls as rows inside the same atomic turn write; publish the reads the
web interface had to probe for plus paged listing, attempt addressing, tagged capture images and
model-call totals; own completeness and the trend-exclusion rule; declare a store schema version
with a copy-first migration of the 2026-09-21 file; and export/import a run as a portable bundle.
No persisted record shape changes, no harness call site changes (FR-016, spec Assumptions).

## Technical Context

**Language/Version**: Python 3.12 (measured 3.12.3), pydantic v2, stdlib `sqlite3` (SQLite 3.45.1)
**Primary Dependencies**: none new. `tarfile`, `hashlib`, `sqlite3` backup API — all standard library
**Storage**: one SQLite file (WAL, `synchronous=FULL`) + sibling content-addressed `blobs/` directory — unchanged
**Testing**: pytest; contract suite pattern of `tests/contract/test_match_store_port.py`; web routes via `tests/web_support/fixtures.py`
**Target Platform**: Linux, Windows, macOS — the store is pure Python; no OS library
**Project Type**: library + CLI sub-app inside the existing `civsim_harness` package
**Performance Goals**: list 1,000 runs, any page/sort/filter, < 2 s; one run's model-call totals < 1 s (SC-008); 30,000-step run round-trip (SC-002)
**Constraints**: single writer, many readers; no delete/edit of any record; read-only mode never migrates; nothing here may be imported by agent-facing code (FR-029)
**Scale/Scope**: ~10 runs today; designed for 1,000 runs × 300 turns × 100 steps

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

Constitution v1.0.0. **Initial check: PASS. Post-design re-check (after data-model.md and the
contract): PASS**, with three items justified in Complexity Tracking.

### Principle I — Human-Parity Information & Action Boundary (NON-NEGOTIABLE)

| Obligation | How this design satisfies it | Where |
|---|---|---|
| No new data path can reach the agent | Every new read serves the web, the operator CLI and the future optimisation layer. The harness's agent-facing path (`observe/assemble.py`) does not import the store, and `tests/contract/test_store_boundary.py` asserts that no module under `observe/`, `parity/`, `agent/`, `capability/`, `act/` or `provider/` imports `civsim_harness.store` | FR-029, research R11 |
| No debug/provenance leakage in a new data path | The store holds provenance by design (model identity, cost, build, host). The new paths carry it to readers *outside* the game loop only; the bundle is written to disk, never to a prompt | contract B1 |
| In-client human equivalent of any new surface | **None introduced.** This plan adds no observation or action surface — the workflow section's per-surface question is stated as moot rather than skipped | — |

### Principle II — Firetuner-First

Not engaged: the store neither controls nor observes the game. Stated for completeness.

### Principle III — Complete Match Telemetry

- No new game-state write path bypasses turn-by-turn persistence: the only write the harness makes
  is still `write_turn_cycle` behind `store/guard.py`; this feature adds model-call rows **inside**
  that same transaction (W1) and a verbatim `import_run` that is itself one atomic transaction (W4).
- Gapped records cannot feed trending: the exclusion rule lives in `metric_series` and `divergence`
  themselves (T1, T4), with each excluded run named — the rule is the store's, not the reader's.
- Completeness is maintained by the store on every write that can change it (W3), with the same
  rules `store/completeness.py` already applies, so the served status and the derivation cannot
  disagree.
- Migration derives, never drops: 1.0→1.1 adds tables, columns and rows and rewrites nothing (V3).

### Principle IV — Reproducible, Seeded Experimentation

Save lineage is carried verbatim through export/import (`SavePoint.lineage`, `Run.parent_run_id`/
`parent_turn`); import refuses a run id collision so two lineages can never merge; parent
immutability guards in the adapter are unchanged. Divergence compares runs from their branch floor.

### Principle V — Guidebook Gate

The trend reads *serve* deliverable 4 but run no optimisation; nothing here starts before
`GUIDEBOOK.md` exists because nothing here optimises.

### Principle VI — Shared, Unified Observability

One store, one contract, read by the web interface and by a Claude Code session through the same
CLI reads (`civsim store …`). Read-only mode lets the web open the file while a run writes.

### Principle VII — Provider-Agnostic Access & Resilience

Crash safety of the record is the store's contribution: atomic turn writes survive `SIGKILL`
(tested), and the migration's copy-first rule preserves the pre-feature file.

### Development Workflow gates

| Gate | Status |
|---|---|
| Schema change backward-readable or explicit migration | Both: 1.1 is additive; 1.0→1.1 is an explicit, recorded, copy-first migration (V1–V3) |
| Deviation justified in Complexity Tracking | C1–C3 below |

## Project Structure

### Documentation (this feature)

```text
specs/003-match-tracking-store/
├── plan.md              # This file
├── research.md          # Phase 0 — R1–R14
├── data-model.md        # Phase 1 — store metadata, indexed projection, read value types
├── quickstart.md        # Phase 1 — validation scenarios 1–8
├── contracts/
│   └── match-tracking-store.md   # The published contract (extends 002's match-store-port.md)
└── tasks.md             # Phase 2 output (/speckit-tasks)
```

### Source Code (repository root)

```text
src/civsim_harness/
├── errors.py                     # + StoreReadError, StoreSchemaError, BundleError
├── store/
│   ├── port.py                   # 002 port — UNCHANGED
│   ├── contract.py               # NEW: MatchTrackingStore Protocol + read value types (data-model §3)
│   ├── schema.py                 # NEW: DDL 1.1, StoreSchemaVersion, store_meta, migrations, copy-first open
│   ├── trends.py                 # NEW: pure derivations — turn metrics, fingerprints, divergence, exclusion
│   ├── bundle.py                 # NEW: RunRecordSet <-> directory / .tar.gz, manifest, hashes
│   ├── sqlite_reads.py           # NEW: the 003 reads as a mixin over the adapter's connection
│   ├── sqlite_adapter.py         # EXTENDED: uses schema.py; model-call rows in write_turn_cycle;
│   │                             #   index columns kept in sync; completeness re-derived; read_only; import_run
│   ├── completeness.py           # unchanged (rules reused)
│   └── guard.py                  # unchanged
└── operator/
    ├── cli.py                    # + one add_typer line for `store`
    └── store_cli.py              # NEW: civsim store info | migrate | runs | model-calls | export | import

tests/
├── contract/
│   ├── test_match_store_port.py          # + E1–E5 section
│   ├── test_match_tracking_store.py      # NEW: W/R/T/V rules
│   └── test_store_boundary.py            # NEW: FR-029 import boundary
├── unit/
│   ├── test_store_schema.py              # NEW: versioning, refusal, migration (synthetic 1.0 + real file when present)
│   ├── test_store_trends.py              # NEW: pure derivations
│   ├── test_store_bundle.py              # NEW: export/import, manifest, refusals
│   ├── test_store_fault_injection.py     # NEW: SC-001 (proxy faults + SIGKILL)
│   ├── test_store_scale.py               # NEW: SC-002, SC-008 timings
│   └── test_store_cli.py                 # NEW: the six commands
└── integration/
    └── test_web_against_tracking_store.py  # NEW: SC-003 / FR-016 over the real store
```

**Structure Decision**: single project, extending the existing `store/` package (research R1). No
new top-level package, no new dependency. `civsim_web` is **not modified** (FR-016: its reads keep
working unchanged); retiring its three probe Protocols is a 001 follow-up recorded below.

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| **C1** — `import_run`, a privileged write that inserts `archived_at`, `eligible` save points and `run_archived` events verbatim, alongside a contract (A2) that reserves those to `archive_run` | A bundle must reproduce a run *identically* on the other host (SC-006): statuses, timestamps, events. `archive_run` stamps *now* and writes a fresh event; `write_save_point` rejects `eligible` | Replaying through the nine writes fabricates timestamps and events — a record that says it was archived at import time is a false record. `import_run` is atomic, refuses collisions, and is the *only* verbatim path; A2's intent (no automatic eligibility) is preserved because import creates nothing new |
| **C2** — the adapter re-derives completeness inside its own transactions, a second call site for a rule `completeness.py` owns | FR-010: a reader must never compute completeness; the harness only refreshes it at its own moments, and a dead writer refreshes nothing | Leaving derivation to callers is the status quo the spec names as the defect. The adapter calls the **same** rules on the same connection (RLock), so there is one definition, two call sites |
| **C3** — `TrendQuery.include_visually_degraded`, an opt-in that admits degraded runs to a series | Every run recorded to date is `visually_degraded` (no image reaches the agent on Linux, T252); a rule with no override makes every trend read on this host empty | Silent inclusion violates FR-019. The override is explicit, recorded on the response, and every series carries its comparability, so the store's rule still decides and still says so. **Owner-reviewable** |

## Decisions the owner may want to review

1. **Bundle format** (research R7): directory canonical, `.tar.gz` transport — hypervisor default.
2. **Trend override** (C3) — default strict, explicit opt-in.
3. **Store schema numbering**: pre-feature file = 1.0; this feature = 1.1; migration backup
   `<file>.v1.0.bak-<UTC>` beside the file, never deleted by the store.
4. **Ten runs, not nine**, in the 2026-09-21 file (research R2) — the `preparing` run counts.

## Cross-spec follow-ups (reported, not done here — outside `specs/003`)

- **002 contract amendment**: `match-store-port.md` should point at this feature as its implementer
  (spec Assumptions) — a `specs/002` edit.
- **002 yields**: `compute_yields` is a no-op and no observation declares per-turn yields, so the
  science/culture/gold/faith/production/food series are honestly empty until 002 adds a
  `player.yields` declaration (research R4).
- **001 probes**: `civsim_web/store_client/port.py` may now retire `RunCatalogReader`,
  `CaptureBlobReader`, `TurnAttemptReader` and widen its `MatchStore` — its own docstrings promise
  exactly that when deliverable 3 lands.

## Phase 0 / Phase 1 outputs

- [research.md](./research.md) — all unknowns resolved (R1–R14)
- [data-model.md](./data-model.md)
- [contracts/match-tracking-store.md](./contracts/match-tracking-store.md)
- [quickstart.md](./quickstart.md)
