# Validation Results: Match-Tracking Data Store

**Feature**: `003-match-tracking-store` | **Recorded**: 2026-09-21 (Linux node, `live/linux`)
**Rule**: nothing below is a pass that was not observed. Each entry names the command or test that
produced it and the machine it ran on (this box: Linux, Python 3.12.3, SQLite 3.45.1).

## Suite

| What | Result |
|---|---|
| Full suite, `uv run pytest -q` (whole tree, including other lanes' in-flight work) | **1773 passed / 9 skipped / 0 failed** in 112 s (baseline before this feature: 1650 / 8 / 0; the skips are Windows/macOS-only paths) |
| 002 store conformance `tests/contract/test_match_store_port.py` | passes **unchanged**, plus the E1–E5 section added by this feature |
| `uv run ruff check` on every file this feature touched | clean |
| `uv run mypy --strict src/civsim_harness/store src/civsim_harness/operator/store_cli.py` | clean (11 files) |

## Success criteria

| SC | Evidence | Result |
|---|---|---|
| **SC-001** ≥ 200 interruptions at every write boundary | `tests/unit/test_store_fault_injection.py`: a fault-injecting connection proxy cut every write operation at every SQL statement, before and after the statement — **354 interruptions** over ten operations (`write_turn_cycle` 60, 20-step turn 144, `create_run` 8, `write_run_event` 10, `write_model_call` 12, `write_save_point` 20, `write_capture` 8, `mark_turn_superseded` 22, `update_run` 26, `archive_run` 44). After every one: turn wholly present or wholly absent at turn and step granularity, model-call rows exactly matching present steps, no orphan rows, the unwritten turn a gap once the run paused, the identical retry accepted once. Plus one real `SIGKILL` of a subprocess mid-200-step write: every present turn whole and contiguous | **PASS** |
| **SC-002** 300 turns × 100 steps round-trip | `tests/unit/test_store_scale.py`: 30,000 steps written in **3.29 s**, read back in order in **0.56 s**, no truncation, completeness `complete`, 30,000 model-call rows | **PASS** |
| **SC-003** web read-side suite over this store, zero degraded paths | `tests/integration/test_web_against_tracking_store.py` over `SqliteMatchStore(read_only=True)` with `civsim_web` **unmodified**: catalog `listing_is_partial=False` with configuration columns filled for every row (archived run included); kept capture served as `image/png` bytes; withheld capture answers `capture_unavailable`, never `capture_blob_unreachable`; `?attempt=0` returns the abandoned attempt, never `attempt_not_addressable` | **PASS** (4 tests) |
| **SC-004** per-run spend in one read; the 2026-09-21 runs reconcile to $1.42 | Quickstart Scenario 6 on a **copy** of the real file, then `model_call_totals` per run: five priced runs — 0.294044 + 0.407990 + 0.397258 + 0.241004 + 0.084398 = **$1.424694** (16 + 21 + 24 + 24 + 8 = 93 priced calls; two fake-provider runs unpriced; three runs with no calls). The night report's figure was $1.42 | **PASS** — reconciles to the cent |
| **SC-005** the 2026-09-21 file opens unchanged, model calls as rows | Scenario 6 (below) and `tests/unit/test_store_schema.py::test_the_real_pre_feature_file_migrates_and_reads_back` (runs on this host, skips elsewhere): **ten** runs read back equal to their 1.0 JSON; 97 model-call rows derived (= 97 decision steps; the 1.0 `model_calls` table held 0 rows) | **PASS** |
| **SC-006** export/import identical | `tests/unit/test_store_bundle.py`: directory and `.tar.gz` round trips; `export_run` of the imported run **equals** the original record set (every attempt, ordering, statuses, byte-identical images, withheld still withheld); second import refused naming the run id. Cross-**host** import (Windows) not performed here — the bundle format is POSIX-relative-path only and the test asserts no backslash or absolute path in a manifest | **PASS on one host**; other-host half **not observed** |
| **SC-007** 100% of gapped/degraded runs excluded and named | `tests/contract/test_match_tracking_store.py` US3 section: 5 runs of one seed set, 2 gapped → 3 series, 2 named with their gap; `not_comparable` always excluded; `visually_degraded` excluded by default, admitted only with the recorded opt-in | **PASS** |
| **SC-008** 1,000 runs list < 2 s; totals < 1 s | `tests/unit/test_store_scale.py`: 192 listings (every sort × page × filter combination) over 1,001 runs, slowest **1.7 ms**; 3,000-call totals in **7.8 ms** | **PASS** |
| **SC-009** archiving changes only save eligibility | `tests/contract/test_match_tracking_store.py::test_us5_ac1_sc009…`: before/after snapshots of every record kind identical except `retention_status` and one new `run_archived` event; `store_info().counts` differ by exactly one event | **PASS** |

## Quickstart scenarios

| Scenario | Ran? | Outcome |
|---|---|---|
| 1 — turn on the record (US1) | yes | see SC-001, SC-002 |
| 2 — one published contract (US2) | yes | E1–E5 asserted against the real store; `civsim store model-calls` on the real copy prints the totals line |
| 3 — trends (US3) | yes | see SC-007 |
| 4 — bundles (US4) | yes (headless, one host) | see SC-006; `civsim store export … --archive` / `import` round trip covered by `tests/unit/test_store_cli.py` |
| 5 — web takes no degraded path | yes | see SC-003; `civsim-web --store civsim_harness.store:open_read_only` is the configuration (the web CLI calls a callable reference with no arguments) |
| 6 — the pre-feature file | **yes, on a copy** (`scratchpad/scenario6/`; the real file untouched) | `civsim store info` → `1.0 (pre-feature file, 002 layout)`, names `civsim store migrate`; `migrate --dry-run` → 97 calls to derive, 10 runs to backfill, 7 columns, 2 tables; `migrate` → backup `store.db.v1.0.bak-20260921T130531Z` beside the file, `1.0 -> 1.1`; `info` → 1.1, 10 runs / 16 turn cycles / 97 steps / 276 events / 97 model calls / 19 save points / 115 captures; `runs` lists all ten with the lifecycle/completeness/comparability the night report recorded |
| 7 — archival (US5) | yes | see SC-009; preset-beside-quicksaves test in `tests/unit/test_retention.py` |
| 8 — the model plays through the new store | **NO — not run** | Needs the live client on this host, the owner's Steam account (one machine at a time), and the provider key; roughly $0.30 for three Sonnet 5 turns. The store is ready for it: the first `write_turn_cycle` of any new run lands its model calls as rows. Recorded as not observed, never as a pass |

## Not verified here (honest residue)

- The **Windows/macOS** half of SC-006 (import on the other platform) and any run of the store on
  those platforms. The code is pure Python and the bundle is path-neutral by construction and by
  test; the observation is still owed.
- The **9th skip** in the full suite is not this feature's: this feature's only conditional test
  (the real-file migration) ran here. The whole-tree run includes other lanes' in-flight changes.
- Science/culture/gold/faith/production/food series are **empty with a stated reason** on every run
  this project has recorded, because the harness records no per-turn yields yet (research R4; a
  002 follow-up). `city_count` and `unit_count` are served from the observations that exist.
