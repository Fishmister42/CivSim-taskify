# Quickstart & Validation Guide: Match-Tracking Data Store

**Feature**: `003-match-tracking-store` | **Date**: 2026-09-21 | **Plan**: [plan.md](./plan.md)

How to prove the store does what the spec says. Each scenario maps to a user story's Independent
Test and names the success criteria it discharges. Scenarios 1–7 run headless; scenario 8 needs a
live client and the owner's account.

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.12+, `uv sync --group civsim_web` | Scenario 5 drives the web interface in-process |
| A store file | `civsim-match-store.db` at the repo root (`CIVSIM_STORE_PATH` overrides). The 2026-09-21 file is gitignored and lives only on the Linux host; every scenario that needs it skips honestly elsewhere |
| **Never** run scenarios 6–7 against the real file without the copy | Migration copies the file first by design; export/import use `--store` on a temp path |

## Scenario 1 — A turn is on the record before the game moves on (US1; SC-001, SC-002)

```bash
uv run pytest tests/unit/test_store_fault_injection.py tests/contract/test_match_tracking_store.py -k "atomic or idempotent or gap or scale" -q
```

Expected: every injected interruption (≥ 200) leaves the turn wholly present or wholly absent, the
attempted-but-unwritten turn is a gap, a repeated write of attempt 0 is accepted once, and a
300-turn × 100-step run round-trips in order.

## Scenario 2 — One published contract (US2; SC-003 half, SC-004, SC-008)

```bash
uv run pytest tests/contract/test_match_store_port.py tests/contract/test_match_tracking_store.py -k "list_runs or query_runs or attempt or capture_image or model_call or highest" -q
```

Expected: 1,000 runs page correctly with the right total in under two seconds; attempt 0 of a
replayed turn is returned as attempt 0; a withheld capture answers `withheld`, a kept one answers
bytes, a copied-without-blobs store answers `missing`; model-call totals equal the bundle sums.

Against the real file (Linux host only):

```bash
uv run civsim store model-calls <run_id>
```

Sum the five model-driven runs' `cost_usd`; SC-004 is met when it reconciles with the provider's
billing for 2026-09-21 ($1.42 was reported). Record the figure in `validation-results.md`.

## Scenario 3 — Trends only from records that can bear them (US3; SC-007)

```bash
uv run pytest tests/unit/test_store_trends.py tests/contract/test_match_tracking_store.py -k "trend or series or diverg" -q
```

Expected: gapped runs are absent from every series and named with their gap; two runs identical to
turn 12 diverge at 12 with the differing dimension named; an in-progress run is marked so.

## Scenario 4 — Evidence moves between hosts (US4; SC-005, SC-006)

```bash
uv run pytest tests/unit/test_store_bundle.py tests/unit/test_store_schema.py -q
# and, by hand, on a temp store:
export CIVSIM_STORE_PATH=/tmp/civsim-a.db
uv run civsim store export <run_id> --out /tmp/bundle --archive
export CIVSIM_STORE_PATH=/tmp/civsim-b.db
uv run civsim store import /tmp/bundle/<run_id>.civsim-bundle.tar.gz
uv run civsim store import /tmp/bundle/<run_id>.civsim-bundle.tar.gz   # refused: run id exists
uv run civsim store info
```

Expected: identical counts per kind, byte-identical images, withheld captures still withheld, the
second import refused naming the run id.

## Scenario 5 — The web interface takes no degraded path (FR-016, SC-003)

```bash
uv run pytest tests/integration/test_web_against_tracking_store.py -q
```

Expected: the catalog is not partial and shows configuration columns, a kept capture's image route
returns the bytes, `?attempt=0` on a replayed turn returns attempt 0, all over `SqliteMatchStore`
opened read-only.

## Scenario 6 — The pre-feature file opens unchanged (US4; SC-005)

Linux host only; the file is copied first by the migration itself.

```bash
cp civsim-match-store.db /tmp/pre-feature.db && cp -r blobs /tmp/blobs 2>/dev/null || true
CIVSIM_STORE_PATH=/tmp/pre-feature.db uv run civsim store info        # read-only: reports 1.0, refuses to migrate
CIVSIM_STORE_PATH=/tmp/pre-feature.db uv run civsim store migrate     # copies to *.v1.0.bak-<utc>, migrates to 1.1
CIVSIM_STORE_PATH=/tmp/pre-feature.db uv run civsim store info        # 1.1, 10 runs, model_calls == decision_steps + failed calls
CIVSIM_STORE_PATH=/tmp/pre-feature.db uv run civsim store runs --page-size 50
```

Expected: ten runs read back with the statuses the night report recorded, `model_calls` rows now
exist for every step, and the backup file sits beside the store. The automated half runs in
`tests/unit/test_store_schema.py` against a synthetic 1.0 file and, when present, the real file
copied to a temp directory.

## Scenario 7 — Archival never touches the record (US5; SC-009)

```bash
uv run pytest tests/contract/test_match_store_port.py tests/contract/test_match_tracking_store.py tests/unit/test_retention.py -k "archiv or eligible or preset" -q
```

Expected: a before/after comparison of every record kind except save-point retention status is
identical; a paused run's archival is refused; a preset beside quicksaves is never eligible.

## Scenario 8 — The model plays through the new store (owner's "step 3.5")

Needs the live client on this host, the owner's account, and the provider key. Run the existing
model-driven driver against a store that is *already* 1.1:

```bash
uv run python -m tests.live.demo_landed_run spikes/store-003-model-run --provider openrouter --turns 3
uv run civsim store model-calls <run_id>
uv run civsim store export <run_id> --out spikes/store-003-model-run --archive
```

Expected: the run's model calls are rows the moment each turn is written (no unpacking), the totals
match the provider's usage for the run, and the exported bundle imports into an empty store with
identical counts. Roughly $0.30 at Sonnet 5 prices for three turns.
