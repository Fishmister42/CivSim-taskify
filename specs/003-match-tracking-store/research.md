# Research: Match-Tracking Data Store

**Feature**: `003-match-tracking-store` | **Date**: 2026-09-21 | **Plan**: [plan.md](./plan.md)

Phase 0 output. Every unknown the Technical Context raised is resolved here as a decision with its
rationale and the alternatives rejected. Findings below were measured against the repository and the
live store file (`civsim-match-store.db`, 2026-09-21, read-only) rather than assumed.

---

## R1 — Where the store lives: extend the reference adapter, do not fork it

**Decision**: Deliverable 3 is implemented in the existing `src/civsim_harness/store/` package. The
002 port (`store/port.py::MatchStore`) stays exactly as published; a new Protocol
`MatchTrackingStore(MatchStore)` in `store/contract.py` adds the reads and the one privileged write
this feature owns. `SqliteMatchStore` becomes the implementation of the wider contract.

**Rationale**: the harness binds to `MatchStore` (plan 002 C2), the web binds structurally to the
read half of the same port and *probes* for three extra names (`list_runs`, `get_capture_blob`,
`get_turn_cycle_attempt`). Widening by subclassing the Protocol means: the harness's nine writes and
eleven reads are untouched (FR-016), every existing conformance test keeps passing unchanged, and the
web's probes find real implementations under the exact names it probes (E2). The owner specified a
single local file with a sibling image directory; the adapter already is that.

**Alternatives rejected**: a new package (`civsim_store`) — duplicates the models, the guard and the
completeness derivation and gives the harness two stores to configure. Widening `MatchStore` in place
— would make every fake in `tests/` and `civsim_web` non-conformant at once for reads the harness
never calls.

## R2 — Schema versioning and the pre-feature file

**Finding**: the 002 contract declares "Schema version: 1" for *record* shapes
(`models/export_schemas.py::SCHEMA_VERSION = 1`), but the store **file** carries no version at all —
`sqlite_master` on the 2026-09-21 file shows seven tables (`runs`, `turn_cycles`, `decision_steps`,
`run_events`, `model_calls`, `save_points`, `captures`) and no metadata table. Row counts at
2026-09-21T12:17Z: runs 10, turn_cycles 16, decision_steps 97, **model_calls 0**, captures 115,
save_points 19, run_events 276. (The spec says nine runs; the file holds **ten** — `run-54a3cefb5`
is a `preparing` run that never started. The migration must read all ten unchanged.)

**Decision**: the store declares a file schema version `major.minor`. A file with no `store_meta`
table is version **1.0** by definition. This feature ships **1.1**: purely additive (new tables
`store_meta`, `schema_migrations`; new indexed columns on `runs` and `model_calls`; model-call rows
derived from step bundles). A reader refuses a file whose major exceeds its own, naming both
versions (FR-026); a higher minor is readable (FR-024). Migration 1.0→1.1 runs on first open in
write mode, **after** copying the file via the SQLite backup API to
`<file>.v1.0.bak-<UTC timestamp>` (the copy is the rollback), and records itself in
`schema_migrations`. A store opened read-only never migrates; it refuses with the command to run.

**Rationale**: the constitution's Development Workflow requires backward-readable changes or an
explicit migration; the spec requires both a declared version and a one-way migration with a copy.
The SQLite backup API is the only copy that is correct while WAL frames are outstanding.

**Alternatives rejected**: PRAGMA `user_version` alone — an integer cannot carry major/minor and
leaves no record of *what* migrated *when*. Migrating lazily on read — a reader that mutates the
file it was asked to read is the failure mode "readers see the last complete turn" forbids.

## R3 — Why `model_calls` is empty, and the fix that needs no harness change

**Finding**: `run/decision_loop.py` writes a `ModelCall` through `write_model_call` **only when the
call produced no decision** (FR-042 path). A successful call's `ModelCall` rides inside the
`DecisionStepBundle` that `write_turn_cycle` persists, and the reference adapter never projected it
into `model_calls`. Every one of the 97 recorded steps carries a complete `model_call` object (keys
verified on the live file), so nothing was lost — it was simply unindexed.

**Decision**: `write_turn_cycle` inserts each bundle's `ModelCall` into `model_calls` in the **same
transaction** as the turn (idempotent by id, content-checked like every other immutable write).
Migration 1.0→1.1 derives the missing rows once from `decision_steps.bundle_json`. A later
`write_model_call` of an identical call is the existing idempotent no-op. FR-008 is therefore the
store's obligation, discharged inside the atomic write, and the harness is untouched.

## R4 — What the record actually holds for trends

**Finding**: `TurnCycle.yields` is `{}` on every recorded turn. `run/turn_cycle.py` takes a
`compute_yields` dependency whose production default is `_no_yields` ("no per-turn yield derivation
exists in this wave"). No observation declaration in `catalogs/observations/*.yaml` reports
science/culture/gold/faith/production/food. The recorded observation *does* carry `cities.state`
(`{"cities": [...]}`) and `units.state` (`{"units": [...]}`) on every step.

**Decision**: the store's metric series read returns exactly what the record holds and never
invents a value: every numeric key of `TurnCycle.yields` is a metric (the web already renders these
by name via `numeric_yields`), plus two metrics the store derives from the authoritative attempt's
final observation — `city_count` and `unit_count`. A requested metric the record does not carry is
reported as **unavailable with the reason**, per run, never as zero. The names FR-018 lists
(science, culture, gold, faith, production, food) are the yields keys the store expects once the
harness records them; until then a series for them is honestly empty.

**Cross-spec finding for the hypervisor (not fixed here — outside `specs/003`)**: 002 needs a
`player.yields` observation declaration (human parity: the yields are on the top bar) and a real
`compute_yields`. That is a 002 change; this store is ready to serve it the day it lands.

## R5 — The divergence fingerprint

**Finding**: nothing in `src/` records a "fingerprint". The spec's assumption ("the branch-identity
fingerprint the harness already records — turn, yields, units, cities") names data that is present
per turn in the record: the decisions taken (`Decision.action_declaration_id` + parameters per step,
in order), `TurnCycle.yields`, and the unit/city lists in the final observation. The web computes
metric-based divergence (leader change / separation) from series; that is a *chart* notion, not
"the first turn the runs did something different".

**Decision**: `TurnFingerprint(turn, actions, metrics, city_count, unit_count)` derived from a
turn's authoritative attempt, with `actions` the ordered tuple of `(action_declaration_id,
canonical parameters)`. The first divergent turn is the first turn where the two fingerprints differ;
the response names every differing dimension with both values. Runs are compared only over turns
both have authoritatively; the compared range and any exclusion are stated.

## R6 — Trend exclusion rule and the visually-degraded question

**Finding**: all ten recorded runs are `comparability_status = visually_degraded` (no image reached
the agent on Linux — T252). A literal "exclude any degraded comparability" rule returns zero series
for every run this project has.

**Decision**: exclusion is the store's rule and is applied by default: a run is excluded from every
series and named with its reason when (a) its own gap accounting says `has_gaps` or `unknown`, or
(b) its comparability is `not_comparable`, or (c) its comparability is `visually_degraded`. The
request may set `include_visually_degraded=True`; when it does, the response says so and every
series carries its run's `comparability_status`, so a reader cannot mistake a degraded series for a
validated one. **Owner-reviewable decision** (plan Complexity Tracking C3).

**Alternatives rejected**: silently including degraded runs (violates FR-019); no override at all
(every trend read on this host answers "nothing", which is honest but makes deliverable 4 untestable
until a validated host exists).

**Revision (2026-09-21) — a fourth exclusion, `game_turn_did_not_advance`**: gameplay block 7
(`run-480aa573`) recorded five consecutive turn cycles all at game turn 35 — every harness turn and
every step present, so gap accounting alone calls the record `complete` — because each end turn was
dispatched and then `verification_failed` after the bound: the harness's own turn counter advanced
while the game's did not. A record like that is gap-free but not honest about what it played, so it
is excluded the same way a gapped one is, under its own reason rather than folded into `has_gaps`.
The check (`store/completeness.py::turns_whose_game_turn_did_not_advance`) reads two independent
signals over a run's authoritative attempts in turn order, either sufficient alone: `TurnCycle.
game_turn_advanced is False` (the writer's own statement, set when an attempt ends
`end_turn_unconfirmed`), or two consecutive attempts recording the same game turn number from
`game.turn_state.turn_number` in the last step's observation. The second signal is what covers
records written before `game_turn_advanced` existed, like block 7 itself — no migration, no
backfill, no rewrite of a historical record. `None` is never read as a stall: an attempt with
neither the flag nor a recorded game turn contributes nothing, and the baseline carried into the
next comparison is the last *known* game turn, not reset by a gap, since a run's authoritative game
turns are treated as monotonic. **Owner-reviewable decision**, same footing as the visually-degraded
default above.

## R7 — Bundle format

**Decision (owner-reviewable, hypervisor default)**: a **directory** is the canonical bundle; a
single-file **`.tar.gz` of exactly that directory** is the transport wrapper. One exporter writes
the directory and, on request, the archive of it; one importer accepts either. Manifest with
counts, per-file SHA-256, image hashes, source host, store schema version and bundle format version.

**Rationale**: a directory is inspectable, diff-able and works with every copy tool; the archive is
what actually crosses hosts as one file (the spec's "one portable bundle"). Producing the archive from
the directory means the two can never disagree. `tarfile` is standard library and preserves POSIX
paths; forward slashes are mandated in the manifest so Windows and Linux read the same bundle.

**Alternatives rejected**: zip — no advantage over tar.gz here and worse streaming; a single SQLite
file per bundle — reintroduces the file-layout coupling FR-017 forbids and cannot carry images
without a second format.

## R8 — Import as a privileged, atomic write

**Finding**: the nine published writes cannot reproduce an exported run verbatim: `write_save_point`
rejects `retention_status=eligible` by contract (A2), `archive_run` stamps *now* and writes a new
event, and `update_run` may not set `archived_at`. Replaying an archived run through them would
fabricate timestamps and events.

**Decision**: the contract gains one privileged write, `import_run(RunRecordSet)`, which inserts a
run's complete record verbatim in one transaction, refuses when the run id already exists (naming the
collision), and refuses a record set whose internal references do not close (a step naming a turn
cycle the set does not carry, an image hash the set does not carry). `export_run(run_id)` is its
read-side twin. Bundle serialisation is a pure function over these two. Plan Complexity Tracking C1.

## R9 — Listing at scale (SC-008)

**Decision**: `runs` gains indexed columns `started_at`, `ended_at`, `seed_set_id`,
`record_completeness_status`, `comparability_status`, kept in sync by `create_run`/`update_run` and
backfilled by migration; `query_runs` pages in SQL with `COUNT(*)` for the total. Sort orders are
an enumerated set (`started_at` desc/asc with NULLs last, `run_id`, `ended_at` desc). `list_runs()`
— the web's probe — returns every run in the default order.

## R10 — Concurrent readers

**Finding**: the adapter already opens with `journal_mode=WAL`, so a second connection sees only
committed transactions — a mid-turn write is invisible until `COMMIT`.

**Decision**: `SqliteMatchStore(path, read_only=True)` opens the file with `mode=ro`, refuses every
write with `StoreWriteError`, and refuses to migrate. The web opens the store this way. A test opens
a reader while a writer holds an uncommitted turn and asserts the reader sees the last complete
turn.

## R11 — Principle I boundary as a test, not a sentence

**Finding**: no module under `observe/`, `parity/`, `agent/`, `capability/`, `act/` or `provider/`
imports `civsim_harness.store` today.

**Decision**: `tests/contract/test_store_boundary.py` asserts it stays that way (FR-029), the same
shape as `tests/unit/test_platform_neutrality.py` does for OS libraries.

## R12 — Presets and the eligible listing (FR-023)

**Finding**: a `SavePoint` can only be constructed with `save_name` matching
`civsim__<run>__t<turn>` (model-level pattern), and `list_eligible_save_points` reads records, never
the directory. A `.Civ6Cfg` preset can therefore never be a save point at all; the reaper deletes
only paths named by eligible records.

**Decision**: no new mechanism; an adversarial test seeds a saves directory with a preset beside
quicksaves and asserts the listing and the reaper's plan never name it.

## R13 — Fault injection for SC-001

**Decision**: two layers. (1) A deterministic fault-injecting connection proxy that raises on the
k-th SQL statement of a write, for every k across every write operation, after which the store is
reopened and the record checked: wholly present or wholly absent, gap reported, idempotent rewrite
accepted. This yields well over the 200 interruptions SC-001 asks for. (2) One real `SIGKILL` of a
subprocess mid-`write_turn_cycle`, then integrity checked from the parent.

## R14 — Scale checks (SC-002, SC-008)

**Decision**: a 300-turn × 100-step run written and read back in the suite with minimal
observations (30,000 steps); 1,000 runs listed with paging and filters under two seconds and one
run's model-call totals under one second, both timed in the test on the suite's own machine.
