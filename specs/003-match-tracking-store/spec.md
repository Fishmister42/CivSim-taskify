# Feature Specification: Match-Tracking Data Store

**Feature Branch**: `003-match-tracking-store`

**Created**: 2026-09-21

**Status**: Implemented (2026-09-21)

*The spec template (`.specify/templates/spec-template.md`) offers only `Draft` and has no status for
a shipped feature, so `Implemented` is this project's own value. It records that all 46 tasks in
[tasks.md](./tasks.md) are closed and that results are recorded in
[validation-results.md](./validation-results.md) — not that the feature is frozen.*

**Input**: User description: "Match-tracking data store (constitution deliverable 3, Principle III
"Complete Match Telemetry"). Today the store exists only as an implementation detail inside spec 002
(the reference adapter behind `specs/002-civ-playing-harness/contracts/match-store-port.md`,
extensions E1–E5) and spec 001 consumes it read-only. This feature makes the store its own
deliverable with its own contract: every turn's record written before the next turn begins with gaps
recorded rather than hidden; one published port that the web interface and the future optimization
layer read through, including the four reads 001 had to declare as optional probed capabilities;
model calls persisted and queryable as first-class rows; cross-run reads for trending that exclude
gapped runs by the store's own accounting; retention and archival that never lose the turn-by-turn
record; backward-readable schema changes with a migration path from today's store file; and a
portable export/import of a run for moving evidence between hosts."

## Context — why this is its own deliverable now

The constitution charters five deliverables and says each "MUST be elaborated through its own
feature spec". Deliverable 3 never was: the playing harness (002) needed somewhere to write on its
first day, so a local reference adapter was built behind a contract that 002 owns, and the web
interface (001) was then built as a pure reader of that same adapter. Two things happened that make
the missing spec a cost rather than a tidiness item:

- **The first consumer's contract could not serve the second consumer.** 001 had to declare four
  reads as *optional probed capabilities* — listing all runs, returning a capture's image bytes,
  addressing a specific turn attempt, resolving a run's configuration — because 002's port lacked
  them. The owner ruled (2026-09-20) that those reads are deliverable 3's obligation, and they sit
  today as an amendment on a contract deliverable 3 does not own.
- **The record already disagrees with itself.** On the night of 2026-09-21 the first real runs
  through the production harness were persisted, and their model calls — cost, tokens, latency,
  model served — were written *inside* each decision step's bundle while the dedicated model-call
  rows stayed empty. Per-run spend cannot be summed without unpacking every step. Nobody was wrong;
  the store simply has no owner whose job it is to notice.

This specification gives the store an owner. It inherits every obligation the 002 contract already
states (durability, atomicity, idempotency, archival, immutability) as its floor, and adds what the
two consumers and the constitution's Principle III require above that floor.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A turn is on the record before the game moves on (Priority: P1)

The playing harness finishes a turn — every decision step with what the agent saw, what it decided
and why, what the model call cost, every screen capture (kept or withheld), the quicksave taken at
the turn's start, and every event along the way — and hands the whole turn to the store. The store
answers only once the turn is safely on disk, and the harness ends the game turn on the strength of
that answer. If anything goes wrong mid-way — the process dies, the disk fills, the write is
retried after an ambiguous failure — the record shows the turn either wholly present or wholly
absent, never half of it, and never twice. A turn that was attempted but never completed shows up
as a **gap**, not as a shorter run.

**Why this priority**: This is Principle III verbatim. Every other deliverable — trending,
Monte Carlo, the web view — is only as trustworthy as this guarantee. It is also the one thing the
interim adapter already promises, so a store that cannot do this is not a candidate at all.

**Independent Test**: Drive a scripted run through the store's write port with faults injected at
every boundary (before, during and after each write; process kill mid-turn; duplicate write of the
same turn), then read the run back: every turn is either complete with its steps in order, or
absent and reported as a gap; no turn is duplicated; the run's completeness status matches the
gaps. Delivers value alone: a harness with only this store can already play and be audited.

**Acceptance Scenarios**:

1. **Given** a run in progress, **When** the harness writes turn *N* with 250 ordered decision steps,
   **Then** reading turn *N* back returns all 250 steps in their original order with their
   observations, decisions and model calls attached, and the write did not return before the record
   was durable.
2. **Given** the process is killed while turn *N* is being written, **When** the run is opened again,
   **Then** turn *N* is absent, is listed as a gap, and turns 1..*N*−1 are unaffected.
3. **Given** turn *N* attempt 0 was written and the acknowledgement was lost, **When** the harness
   writes the identical turn *N* attempt 0 again, **Then** exactly one record exists and the second
   write is accepted without error.
4. **Given** turn *N* attempt 0 was abandoned and attempt 1 completed, **When** the run is read,
   **Then** attempt 1 is the authoritative turn, attempt 0 remains retrievable and marked
   superseded, and the turn is not a gap.
5. **Given** a run whose harness process died after taking turn *N*'s quicksave but before writing
   turn *N*, **When** the run is no longer actively playing, **Then** turn *N* is reported as a gap
   and the run's completeness status says so.
6. **Given** a screen capture that screening withheld, **When** it is written, **Then** the record
   exists with the reason it was withheld and carries no image, and asking for its image answers
   "none" rather than a substitute.

---

### User Story 2 - Every reader uses one published contract, with nothing missing (Priority: P2)

The web interface, an operator at the command line, and the future optimization layer all read the
same records through one contract that answers every question they have today: list all runs
(finished, failed, paused and archived included) with paging, sorting and filtering; open a run's
configuration by the run's own id; open any specific attempt of any turn; open a capture's image;
and sum a run's model spend, tokens and latency directly from model-call records without opening
each decision step. Nothing a reader needs is reachable only by knowing how the store lays out its
files.

**Why this priority**: Four of these reads are the ones 001 proved missing and had to work around;
the fifth (model calls as rows) is what the first real runs proved broken. Without this story the
web interface renders honest "unavailable" panels over data that exists, and per-run cost — the
number the owner's $80 budget is measured in — is not queryable.

**Independent Test**: Run the web interface's complete read-side test suite against this store with
its "published port only" degradation mode disabled: every catalog column populates, every capture
image serves, every attempt is addressable, and no degraded-capability path is taken. Separately,
sum a known run's model calls through the store and match the total to the provider's own billing
for that run.

**Acceptance Scenarios**:

1. **Given** 1,000 runs in every lifecycle state including archived, **When** a reader asks for the
   run catalog sorted by start time, 25 per page, filtered to one seed set, **Then** it receives the
   correct page with the correct total, and an archived run appears exactly like any other.
2. **Given** a run whose turn 7 has attempts 0 (abandoned) and 1 (authoritative), **When** a reader
   asks for turn 7 attempt 0, **Then** it receives attempt 0's record, never attempt 1 substituted.
3. **Given** a run with 24 model calls across three turns, **When** a reader asks for the run's model
   calls, **Then** it receives 24 records with cost, tokens, latency, model requested and served, and
   outcome, and their cost sum equals the sum recorded inside the decision steps.
4. **Given** a capture that was kept, **When** a reader asks for its image, **Then** the bytes are
   returned; **Given** a capture that was withheld, **Then** the answer is explicitly "no image",
   never a placeholder.
5. **Given** an id that is a configuration id rather than a run id, **When** a reader asks for a run
   configuration by it, **Then** the answer is "no such run", never another run's configuration.

---

### User Story 3 - Trends are drawn only from records that can bear them (Priority: P3)

A researcher asks how science and culture per turn compared across every run of one seed set, and
where two runs of the same seed diverged. The store answers with per-turn metric series and
divergence points, and it leaves out — by its own rule, not the asker's discipline — any run whose
record has gaps or whose comparability is degraded, saying which runs were excluded and why.

**Why this priority**: The constitution forbids using a gapped run for trending, datamining or
optimization input. Today that rule lives in the reader's good behaviour. Putting it in the store
means deliverable 4 cannot accidentally optimise against a record that is missing turns.

**Independent Test**: Populate runs of one seed set where some have turn gaps, some are visually
degraded, and some are clean; request the metric series and divergence points; verify the gapped
runs are absent from every series, the exclusion list names each one with its reason, and the
divergence points match the first turn at which the clean runs' fingerprints differ.

**Acceptance Scenarios**:

1. **Given** five runs of one seed set, two with gaps, **When** the science-per-turn series is
   requested, **Then** three series are returned and the response names the two excluded runs and
   the gap that excluded each.
2. **Given** two complete runs of the same seed that took the same actions until turn 12,
   **When** divergence is requested, **Then** turn 12 is reported as the first divergence with what
   differed.
3. **Given** a run that is still playing, **When** a series is requested, **Then** its turns so far
   are included and it is marked as in progress, not as complete.

---

### User Story 4 - Evidence moves between hosts intact (Priority: P4)

An operator exports a run from the Linux host as one portable bundle — its records and its capture
images — copies it to the Windows host, imports it, and the web interface there shows the run
exactly as it appeared on Linux. Opening yesterday's store file with today's store works without
losing a single run, and the runs recorded before this feature existed read back with their model
calls now visible as rows.

**Why this priority**: The project runs on two machines with one shared coordination channel. Today
a run's evidence is a database file plus a directory of blobs plus a set of screenshots committed to
the repository by hand. A bundle is what lets the record, not a narrative about it, cross the gap.

**Independent Test**: Export a real run, verify the bundle's manifest, import it into an empty store
on another platform, and compare: identical record counts per kind, identical ordering, byte-identical
images, identical completeness and comparability statuses. Open the pre-feature store file with the
new store: all ten runs recorded on 2026-09-21 (nine that started, plus one that never left
`preparing`) read back unchanged and their model calls are queryable as rows.

**Acceptance Scenarios**:

1. **Given** a finished run with 3 turns, 6 captures (2 withheld) and 21 model calls, **When** it is
   exported and imported elsewhere, **Then** every count matches, every kept image is byte-identical,
   and the withheld captures import as withheld.
2. **Given** a bundle whose run id already exists in the importing store, **When** import is
   attempted, **Then** it is refused with the collision named — never merged, never silently renamed.
3. **Given** the 2026-09-21 store file (ten runs; model calls only inside step bundles),
   **When** the new store opens it, **Then** every run reads back identically and each run's model
   calls are available as rows with totals equal to the bundle-embedded values.
4. **Given** a store written by a newer schema version than the reader understands, **When** it is
   opened, **Then** the reader refuses with the two versions named rather than reading it partially.

---

### User Story 5 - Archival and retention never touch the record (Priority: P5)

An operator archives a finished run to let its quicksaves be reclaimed. Every turn, step, capture,
event and model call of that run remains readable forever; only its save files become eligible for
the separate, operator-invoked reaper — which can never see a preset file or a save belonging to a
run that was not archived, no matter how old.

**Why this priority**: This is the 002 contract's archival section, restated as the store's own
promise and extended to the one hazard found live: setup presets share the saves directory with
quicksaves. It ranks last because the interim adapter already honours it; it is here so the new
owner cannot drop it.

**Independent Test**: Archive a run; verify every record kind is still readable and the run still
appears in the catalog and in trends (if complete); verify only its save points became eligible;
run the reaper's listing and confirm no preset and no non-archived run's save appears.

**Acceptance Scenarios**:

1. **Given** a finished run, **When** it is archived, **Then** its turn cycles, steps, captures,
   events and model calls read back unchanged, an archive event is recorded, and its save points are
   the only new eligible entries.
2. **Given** a run that is paused rather than finished, **When** archival is requested, **Then** it
   is refused.
3. **Given** a saves directory containing a setup preset beside quicksaves, **When** eligible save
   points are listed, **Then** the preset is never among them.

---

### Edge Cases

- A turn of several hundred steps, each with an observation and a capture reference, is written as
  one unit; the store must not require the harness to cap, sample or summarise it.
- The harness process dies between taking turn *N*'s quicksave and writing turn *N*: the save point
  exists, the turn does not; once the run stops advancing this is a gap, while the run is actively
  playing it is the normal in-flight shape.
- A capture record exists but its image file is missing on disk (copied store without its blobs):
  the image read answers "missing" with the record intact, never an error that hides the record.
- Two hosts each have their own store; neither may write to the other's. A bundle import is the only
  way a run crosses hosts.
- Readers open the store while the single writer is mid-turn: they see the last complete turn, never
  a partial one.
- A run's writer died leaving it `paused` with no end time; it remains listable, its gaps reported,
  and it can be archived only after an operator marks it terminal.
- Runs recorded before model calls were rows: their model calls are derived from the step bundles
  once, and any later write of the same call is idempotent.
- The file layout must read identically on Linux and Windows: paths stored in records are
  platform-neutral references, never absolute host paths.

## Requirements *(mandatory)*

### Functional Requirements

**Writing the record (Principle III floor — inherited from the 002 contract and now owned here)**

- **FR-001**: The store MUST accept a whole turn — turn cycle, ordered decision steps, and each
  step's observation, decision, model call and capture references — as one write, and MUST confirm
  the write only after the record is durable.
- **FR-002**: A turn write MUST be atomic: after any interruption a turn is either wholly present or
  wholly absent, at turn *and* step granularity.
- **FR-003**: Turn writes MUST be idempotent on run, turn number and attempt index; a repeated write
  of the same attempt MUST NOT duplicate or corrupt it.
- **FR-004**: Any write failure MUST surface as an error the harness cannot mistake for success; the
  store MUST NOT buffer, defer or silently drop a write.
- **FR-005**: The store MUST record every run event, model call, save point and screen capture the
  harness writes, and a capture withheld by screening MUST be recorded with its reason and without
  an image.
- **FR-006**: Abandoned and superseded turn attempts MUST be retained and marked, never deleted; the
  store MUST expose no operation that deletes or edits a turn, step, capture, event or model call.
- **FR-007**: A branched run MUST never modify its parent's records; a write from a child that
  targets parent records MUST be rejected.
- **FR-008**: Model calls MUST be stored as first-class records queryable by run, turn and step —
  with cost, tokens, latency, model requested, model served, retry count, fallback flag and outcome
  — whether or not the same call is also embedded in a decision step.

**Completeness and gaps**

- **FR-009**: The store MUST report, for any run, the turn numbers with no authoritative attempt
  (turn gaps) and, within any turn, the missing step indices (step gaps), using the stopped-versus-
  actively-playing rule the 002 contract defines for trailing turns.
- **FR-010**: The store MUST maintain each run's record-completeness status from its own gap
  accounting, so that a reader never has to compute completeness itself.

**Reading the record (one published contract)**

- **FR-011**: Readers MUST be able to list all runs — every lifecycle state, archived included —
  with paging, a stated sort order, and filtering by seed set, lifecycle state, completeness and
  comparability; the response MUST carry the total count.
- **FR-012**: Readers MUST be able to resolve a run's configuration by the run's own id; any other
  key MUST answer "no such run".
- **FR-013**: Readers MUST be able to open any specific attempt of any turn by attempt index, and
  the authoritative attempt and the most recent attempt by their existing forms.
- **FR-014**: Readers MUST be able to obtain a kept capture's image bytes through the store; a
  withheld capture MUST answer "no image"; a kept capture whose image is missing on disk MUST
  answer "missing" with the record intact.
- **FR-015**: Readers MUST be able to obtain a run's model calls and their totals (cost, tokens,
  call count, fallback count) without opening decision steps.
- **FR-016**: Every read the web interface performs today MUST keep working unchanged; the
  interface's degraded-capability paths MUST no longer be taken against this store.
- **FR-017**: No store read MAY require a caller to know the store's file layout; every question
  answerable by inspecting files MUST be answerable through the contract.

**Cross-run reads for trending**

- **FR-018**: The store MUST provide per-turn metric series (at minimum science, culture, gold,
  faith, production and food per turn, plus city and unit counts) for one run or for all runs of a
  seed set.
- **FR-019**: Trend and comparison reads MUST exclude runs with record gaps or degraded
  comparability by the store's own rule, and MUST name each excluded run with its reason.
- **FR-020**: The store MUST report divergence points between two runs of the same seed set: the
  first turn at which their recorded fingerprints differ and what differed.
- **FR-021**: A run still in progress MUST be distinguishable from a complete one in every trend
  response.

**Archival and retention**

- **FR-022**: Archiving MUST leave every record of the run readable forever and MUST be the only
  operation that makes a run's save points eligible for reclamation; archival of a non-terminal run
  MUST be refused.
- **FR-023**: Eligible-save listing MUST never include a setup preset or a save belonging to a
  non-archived run, regardless of age or count.

**Evolution and portability**

- **FR-024**: The store MUST declare its schema version; records MUST remain readable across
  additive changes within a major version, and any other change MUST ship an explicit migration.
- **FR-025**: The store MUST open the pre-feature store file and read every run it contains
  unchanged, deriving model-call records from step bundles where the rows are absent.
- **FR-026**: A store written by a newer major schema version MUST be refused with both versions
  named, never read partially.
- **FR-027**: A run MUST be exportable as one portable bundle (records, kept images, a manifest with
  counts and content hashes) and importable into another store on any supported platform; import
  MUST refuse a run id that already exists.
- **FR-028**: Stored references to images and saves MUST be platform-neutral so the same store and
  bundle read identically on Linux and Windows.

**Boundary (Principle I)**

- **FR-029**: Nothing in the store MAY ever be read into the playing agent's context. The store
  holds out-of-game provenance — model identity, cost, game build, host, timing, save lineage — and
  the only agent-facing path in the system remains the harness's observation assembly, which does
  not read the store.

### Key Entities

- **Run**: one playthrough attempt; lifecycle state, stop resolution, completeness status,
  comparability status, host and build identity, archive marker, parent run and branch point when
  branched.
- **Run Configuration**: the seed, civilization, leader, ruleset, mod set, map and game settings,
  difficulty, opponents, stop condition and model configuration a run was created with; stored
  verbatim, resolved by run id.
- **Turn Cycle**: one attempt at one turn; attempt index, authoritative flag, outcome, step count,
  the quicksave it started from.
- **Decision Step**: one step inside a turn; ordered; carries an Observation, a Decision (action,
  target, reasoning, execution outcome), and a Model Call.
- **Model Call**: one provider call; model requested and served, latency, cost and tokens, retries,
  fallback, outcome, image count; addressable by run, turn and step.
- **Screen Capture**: one screening-gated frame; kept (with image) or withheld (with reason); tied
  to a decision step; whether it was shown to the agent.
- **Save Point**: one quicksave; turn, name, verification, retention status (retained, eligible,
  missing).
- **Run Event**: one dated occurrence on a run's timeline (lifecycle transition, save taken, capture
  withheld, no-progress backstop, unknown screen, archive).
- **Metric Series**: per-turn values of one metric for one run, with in-progress and exclusion
  markers.
- **Run Bundle**: a portable export of one run — records, kept images, manifest with counts and
  hashes, source host and schema version.
- **Schema Version / Migration Record**: the store's declared version and the migrations applied to
  a given file, with when and from which version.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Across a fault-injection suite of at least 200 interruptions at every write boundary,
  zero turns are ever partially present or duplicated, and every attempted-but-unwritten turn is
  reported as a gap.
- **SC-002**: A 300-turn run with 100 steps per turn writes and reads back with all 30,000 steps in
  order and no truncation.
- **SC-003**: The web interface's complete read-side suite passes against this store with zero
  degraded-capability paths taken (no partial listing, no unresolvable image, no unaddressable
  attempt, no unavailable configuration column).
- **SC-004**: A run's total model spend is available in one read and matches the provider's billing
  for that run to the cent; the first five model-driven runs of 2026-09-21 reconcile to $1.42.
- **SC-005**: The ten runs recorded on 2026-09-21 open unchanged in the new store, and each run
  that made model calls shows them as rows.
- **SC-006**: A run exported on one host and imported on the other reproduces identical record
  counts, ordering, statuses and byte-identical images, and the web interface on the second host
  renders it identically.
- **SC-007**: In every trend response, 100% of runs with gaps or degraded comparability are absent
  from the series and present in the exclusion list with a reason.
- **SC-008**: Listing 1,000 runs, any page, any supported sort and filter, completes in under two
  seconds; a single run's model-call totals in under one second.
- **SC-009**: Archiving a run changes the eligibility of its save points and nothing else: a
  before/after comparison of every other record kind is identical.

## Assumptions

- The reference implementation remains a single local file with a sibling directory of images, as
  the owner specified; a networked or multi-writer store is out of scope. Each host runs one store;
  runs cross hosts only as bundles.
- One writer (the run in progress) and any number of readers at a time; readers may open the store
  while a run is writing and see complete turns only.
- The 002 contract's durability (D1–D6), archival (A1–A4), immutability and capability-extension
  rules (E1–E5) are inherited verbatim as this store's floor; this specification restates them as
  requirements so that the obligation is owned here, and the 002 contract will be amended to point
  at this feature as its implementer once the plan lands.
- The metric set for trends is the set the web interface's metrics page already renders plus city
  and unit counts; the store does not invent metrics the harness does not record.
- Divergence uses the branch-identity fingerprint the harness already records (turn, yields, units,
  cities); the owner has ruled the fingerprint's known omissions are not a current concern.
- "Terminal" means finished or failed; a paused run whose writer died is made terminal by an
  explicit operator action before it can be archived — the store does not infer death from silence.
- Migration of the pre-feature file is one-way and happens on first open with the new store, after
  taking a copy; the copy is the rollback.
- The bundle format (a directory or a single archive file) and the manifest layout are planning
  decisions; the specification fixes only what a bundle must contain and how import must behave.
- This feature changes nothing about what the harness records or when; it changes where and how
  records are stored and read. Spec 002's turn-cycle and decision-loop behaviour is unaffected.
