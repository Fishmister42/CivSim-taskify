# Phase 1 Data Model: Civilization-Playing Harness

**Feature**: `002-civ-playing-harness` | **Date**: 2026-09-19 | **Plan**: [plan.md](./plan.md)

Scope note: this describes the *logical* model the harness produces and consumes. The match-tracking
store (deliverable 3) owns physical schema, indexing, and durability; this document and
[contracts/match-store-port.md](./contracts/match-store-port.md) are what it must be able to hold.

Two classes of data run through this model and must not be conflated:

- **In-game data** — anything derived from the game. Subject to the parity boundary; may reach the
  agent only through a declared catalog entry.
- **Out-of-game data** — model identity, cost, latency, retries, save lineage, run configuration,
  wall-clock timing. Recorded in full, never presented to the playing agent as game information
  (FR-020). Every entity below is marked accordingly.

---

## Entity overview

```text
SeedSet ──< RunConfiguration ──< Run ──< TurnCycle ──< DecisionStep
                  │               │                        │
             GuidanceSet          │                        ├──< Observation ──< ObservationEntry
                                  │                        │              └──< ScreenCapture
                                  ├──< RunEvent            ├──< Decision ──< ActionExecution
                                  ├──< SavePoint           └──< ModelCall
                                  └── (lineage: parent Run + turn)

CatalogVersion ──< ParityDeclaration ──< IntegrationCapability
       └── referenced by every Run, Observation entry, and Decision
```

**`DecisionStep` is the load-bearing addition of the clarification session.** The turn is a loop, not
a phase (FR-008), so the things that used to hang off the turn — the observation, the images, the
model call — hang off the step instead. One step is one full observe → decide → execute → verify
iteration, and it is the grain at which the record must be reconstructable: SC-003 requires no step
gaps within a turn, not merely no turn gaps within a run.

---

## 1. SeedSet

*Out-of-game.* A named collection of map seeds sharing a civilization, ruleset, and **game build** so
runs within the set are comparable (FR-031).

| Field | Type | Notes |
|---|---|---|
| `seed_set_id` | id | |
| `name` | string | Unique |
| `seeds` | list[string] | Map seeds, non-empty |
| `civilization` | string | Fixed across the set |
| `leader` | string | Fixed across the set |
| `ruleset` | string | Fixed across the set |
| `mod_set` | list[ModRef] | Fixed across the set; each with id + version |
| `game_build` | string | The Civ VI build the set's runs are played on — a **composite of platform and version**, e.g. `win/1.0.12.9` (FR-031, R18, R20) |
| `accepted_build_changes` | list[BuildAcceptance] | Operator-accepted deviations; empty by default |
| `created_at` | timestamp | |

**BuildAcceptance** — `acceptance_id`, `from_build`, `to_build`, `accepted_by`, `accepted_at`,
`reason`, `is_platform_transition` (derived: true when the platform component of `from_build` and
`to_build` differ), `r20_spike_ref` (id?; **required when `is_platform_transition` is true** — the
recorded passing result of the R20 cross-platform save spike, T199). Recorded once on the set and
referenced by every run that relied on it (FR-002).

**Platform-crossing acceptances are gated on R20.** A version-only transition (e.g.
`win/1.0.12.9 → win/1.0.12.11`) needs no spike result and `r20_spike_ref` stays null. A transition
that also crosses platform (e.g. `win/1.0.12.9 → mac/1.0.12.9`) is rejected at creation unless
`r20_spike_ref` resolves to a recorded **passing** R20 result — this is the field that makes the gate
enforceable at runtime without moving T199 out of Phase 8: T174 and T175 ship the check early, but it
stays refusing every platform-crossing case until T199 records a passing result to reference
(FR-002, FR-031, R20).

**Why `game_build` is composite.** The macOS and Linux ports are separately built binaries whose
version numbering need not track the Windows build's, so a set whose runs span two platforms carries
the same silent-incomparability hazard as one spanning two versions — and nothing else in the record
would reveal it. Treating platform as part of the build identity means one mechanism covers both, and
accepting `win/1.0.12.9 → win/1.0.12.11` does **not** also accept `win → mac` (R20).

**Validation**:

- `civilization`, `leader`, `ruleset`, and `mod_set` are immutable after creation — varying them is a
  new seed set, not a variation within one (spec Assumptions). Seeds may be appended.
- `game_build` is recorded at creation. A run whose client reports a different build fails preflight
  unless a `BuildAcceptance` covers that exact `from_build → to_build` transition (FR-002).
- **A set carrying any `accepted_build_changes` is not uniform**, and must be reportable as such.
  Every run in it resolves to exactly one build, and the set's runs are partitionable by build — this
  is the property that stops a mixed set from reading like a clean one (FR-031).
- **A platform-crossing `BuildAcceptance` cannot be created without a passing R20 spike result on
  record.** A version-only transition needs none (FR-002, FR-031, R20).

---

## 2. RunConfiguration

*Out-of-game.* The complete recorded definition of a run before it starts (FR-001).

| Field | Type | Notes |
|---|---|---|
| `config_id` | id | |
| `seed_set_id` | id? | Null for one-off runs |
| `map_seed` | string | Required |
| `civilization` / `leader` | string | Required |
| `ruleset` | string | Required |
| `mod_set` | list[ModRef] | Exact set with versions; empty list is meaningful, not absent |
| `map_settings` | object | Map type, size, resources, and other generation settings |
| `game_settings` | object | Game speed, starting era, victory types enabled |
| `difficulty` | enum | Required |
| `opponents` | object | Count and composition |
| `stop_condition` | StopCondition | See below |
| `model_config` | ModelConfig | Primary + ordered fallback chain |
| `guidance_set_id` | id? | Out-of-game strategic guidance (FR-021) |
| `no_progress_step_limit` | int | Consecutive no-progress decision steps that end a turn (FR-014) |
| `recovery_attempt_limit` | int | Consecutive-failure bound (FR-048) |
| `min_free_disk_gb` | number | Headroom floor; below it the run halts rather than deleting a save (R17) |
| `created_at` | timestamp | |

**There is deliberately no `turn_time_budget_s`.** The clarification removed the per-turn time budget
entirely: a turn runs for as many decision steps and as long as the agent needs, and
`no_progress_step_limit` is the only backstop (FR-008, FR-014). A field of that name reappearing in
this model would be a regression, not an addition.

**StopCondition** — the **configured** stop condition, exactly one of: `turn_reached(n)`,
`game_outcome`, `operator_stop`. This is the 3-way set an operator sets before a run starts, and it
does not grow. The **recorded** resolution of a finished run is a separate, broader concept — see
`Run.stop_resolution` in §4 — because a run can also terminate on `unrecoverable_failure`, which is
not a configurable `StopCondition.type`, and because a `game_outcome` condition resolves to one of
two distinct recorded outcomes, victory or defeat.

**ModelConfig** — `primary: ModelRef`, `fallbacks: list[ModelRef]` (ordered, possibly empty),
`request_params: object`. Contains **no credentials** — keys resolve from environment or a secrets
file at runtime (FR-043).

**Validation**:

- Fully populated before a run may start; a missing required field aborts preflight, not turn 1.
- Every model in `primary + fallbacks` must pass the image-capability and context-length preflight
  or the run does not start (FR-039).
- If `seed_set_id` is set, `civilization`, `leader`, `ruleset`, and `mod_set` must match the set.

---

## 3. GuidanceSet

*Out-of-game in provenance, in-context for the agent.* Run-independent strategic guidance supplied to
the agent (FR-021), expected to come from `GUIDEBOOK.md`.

| Field | Type | Notes |
|---|---|---|
| `guidance_set_id` | id | |
| `content_hash` | string | Content-addressed; what was supplied is recorded with the run |
| `content` | text | The guidance as supplied |
| `source_ref` | string? | e.g. `GUIDEBOOK.md@<commit>` |

**Validation**: must contain no state from the current run and no hidden state. Enforced as a
review-time rule plus a runtime assertion that the content is identical across runs referencing the
same hash — guidance that varies per run is a contract violation, not a feature.

---

## 4. Run

*Out-of-game.* One execution of a configuration.

| Field | Type | Notes |
|---|---|---|
| `run_id` | id | |
| `config_id` | id | |
| `lifecycle_state` | enum | See state machine |
| `started_at` / `ended_at` | timestamp? | |
| `stop_resolution` | enum? | Exactly one on termination — `turn_reached` \| `victory` \| `defeat` \| `operator_stop` \| `unrecoverable_failure` (FR-005) |
| `record_completeness_status` | enum | `complete` \| `has_gaps` \| `unknown` (FR-052) |
| `comparability_status` | enum | `comparable` \| `visually_degraded` \| `not_comparable` (FR-050) |
| `observation_catalog_version` | CatalogVersionRef | In force while it played (FR-022) |
| `action_catalog_version` | CatalogVersionRef | In force while it played (FR-022) |
| `parent_run_id` / `parent_turn` | id? / int? | Branch lineage (FR-033) |
| `client_identity` | object | PID and tuner state indices resolved at handshake |
| `game_build` | string | The composite build this run actually played on — platform and version (FR-002, R18, R20) |
| `game_build_acceptance_ref` | id? | Set when the run relied on an operator-accepted build or platform change |
| `host_platform` | object | OS, version, session type where it matters (X11 vs Wayland), and the host adapter that served the run (R19) |
| `host_support_tier` | enum | `validated` \| `supported` — resolved at preflight; `unsupported` cannot produce a run (R19) |
| `archived_at` | timestamp? | Null while branchable; set by the operator archive action (FR-036) |
| `capture_path` | enum | Which capture mechanism served this run (R6), or `none` |

### Lifecycle state machine (FR-003)

```text
preparing ──> playing <──> waiting_on_model
    │  │         │  ^────> waiting_on_game
    │  │         │
    │  └───────> ├──> paused ──> playing
    │            ├──> interrupted ──> resuming ──> playing
    │            │         └──────────────────────> failed
    │            └──> finished
    └──> failed          (preflight mismatch, FR-002)
```

**Validation**:

- Every transition is recorded as a `RunEvent` (FR-003).
- **`preparing`/`playing` → `paused` is additionally legal for one external actor**: the orphan
  sweep (`run/orphans.py`), which pauses a run whose run-identity lock is absent or whose
  lock-holder PID is dead — a driver killed, crashed, or lost with the host — and records the
  lock path, PID and last recorded activity as `reason: orphaned` on the transition event, never
  moving such a run to `finished` or `failed`.
- `finished` requires exactly one `stop_resolution`. Where a stop resolution coincides with a
  victory, defeat, or crash on the same turn, one is recorded as the stop resolution and the others
  appear as events (spec edge case, invariant I10).
- **`stop_resolution` is deliberately broader than `StopCondition.type`.** `unrecoverable_failure`
  already terminates a run without being a configurable `StopCondition.type`, which is proof the
  recorded set of outcomes is necessarily broader than the configured set of conditions — victory and
  defeat are recorded for the same reason, as the two ways a configured `game_outcome` condition can
  actually resolve (FR-005).
- `record_completeness_status` is derived, not asserted: `complete` requires a contiguous
  authoritative turn sequence from 1 to the stop turn with no gap markers, **and** a contiguous
  step sequence within each of those turns (SC-003).
- A run reaching `failed` from `resuming` must identify its last-known-good save (FR-048).
- `game_build` is recorded from the client at preflight, never copied from the seed set — the whole
  point of the check is that the two can disagree (FR-002).
- `game_build_acceptance_ref` is non-null exactly when `game_build` differs from the seed set's
  `game_build` — in **either** component, platform or version. A run that differs without an
  acceptance cannot exist; preflight fails it (R20).
- `host_support_tier` is recorded so a `supported` host's visually degraded runs are never mistaken
  for a `validated` host's. A platform is `unsupported` until probed, and an `unsupported` host
  cannot start a run at all — FR-007 makes a turn without a verified quicksave impossible, so there
  is nothing to record (R19).
- **Archival is a terminal-state action, not a lifecycle state.** `archived_at` is orthogonal to
  `lifecycle_state`: an archived run keeps its records, events, and captures and remains readable
  and auditable forever. What archival changes is only the retention eligibility of its save points
  (FR-036). Archiving a run that is not terminal is rejected.
- **Nothing else sets `archived_at`.** No age rule, no quota, no background sweep. It is set by an
  explicit operator command and recorded as a `run_archived` event (FR-004, FR-036, R17).

---

## 5. TurnCycle

*Mixed — in-game content, out-of-game timing.* One attempt at one turn of one run.

| Field | Type | Notes |
|---|---|---|
| `turn_cycle_id` | id | |
| `run_id` | id | |
| `turn_number` | int | ≥ 1 |
| `attempt_index` | int | 0 for the first attempt; increments on replay |
| `is_authoritative` | bool | Exactly one authoritative attempt per `(run, turn)` (FR-047) |
| `save_point_id` | id | The turn-start quicksave; required (FR-007) |
| `step_count` | int | Number of decision steps in this attempt; ≥ 1, unbounded above |
| `outcome` | enum | `ended_by_agent` \| `end_turn_unconfirmed` \| `ended_on_no_progress` \| `abandoned` |
| `game_turn_advanced` | bool \| null | Did the *game's* turn counter advance? `false` with `end_turn_unconfirmed`; `null` = not recorded (pre-2026-09-21 records, `abandoned`, and `ended_on_no_progress`, whose end turn is issued after this record is durable) |
| `final_no_progress_streak` | int | The counter's value when the turn ended; equals the limit iff `ended_on_no_progress` |
| `visually_degraded` | bool | Derived: true if any step ran without its images (FR-050) |
| `yields` | object | Per-turn yields and outcomes recorded after execution |
| `started_at` / `ended_at` | timestamp | Out-of-game; unbounded duration is expected, not anomalous |
| `persisted_at` | timestamp | Must precede the end-turn action (FR-013) |

The turn no longer carries an `observation_id`: observations are per decision step. A turn holds an
*ordered sequence* of steps, and that sequence is what makes the turn reconstructable (FR-012).

**Validation — the load-bearing invariants**:

1. **No turn without its quicksave.** `save_point_id` is non-nullable; a failed quicksave means the
   turn never comes into existence as an attempt (FR-007, SC-004).
2. **Write before advance.** `persisted_at` must be set and the store write acknowledged before the
   end-turn action is issued. A failed write halts the run (FR-013).
3. **Exactly one authoritative attempt** per `(run_id, turn_number)`. A replayed turn marks the
   earlier attempt `abandoned` and retains it (FR-047).
4. **The turn ends in exactly one of two ways, and they are distinguishable.** `ended_by_agent`
   means the agent issued the declared end-turn action; `ended_on_no_progress` means the backstop
   tripped. The harness has no third exit — it may not end a turn on its own initiative while the
   agent is making progress, and may not truncate a turn for length or cost (FR-008, FR-014,
   SC-022). `abandoned` is not an ending; it is an attempt that was interrupted and replaced.
   *(Amended 2026-09-21, research R14, gameplay block 7 `run-480aa573`: the agent's own ending
   carries two truthful names, not one. `ended_by_agent` means the end turn was dispatched **and**
   the game confirmed it within the bound; `end_turn_unconfirmed` — with `game_turn_advanced =
   false` — means it was dispatched and never confirmed. Same exit, same liveness: the harness's
   turn still advances. This is not a third way to end a turn, and the backstop exit stays exactly
   as distinguishable as before.)*
5. **No time or step cap.** `step_count` has no upper bound and `ended_at - started_at` has no
   maximum. A late-game turn of hundreds of steps and hours of wall clock is a valid turn. Any rule
   that ended a turn because it had grown long or expensive would violate FR-014 directly.
6. **A one-step turn is valid.** The agent seeing the board and immediately ending the turn is a
   correct turn, not a degenerate one (FR-008).
7. **Gaps are explicit, at both grains.** A turn number with no authoritative attempt carries a gap
   marker; so does a missing `step_index` within a turn. Either forces
   `record_completeness_status = has_gaps` (SC-003, SC-011).

### Turn attempt state machine

```text
quicksaving ──> ┌───────────────── decision-step loop ─────────────────┐ ──> persisting ──> ending_turn
     │          │  observing ──> deciding ──> executing ──> verifying  │         │              │
     │          │      ^                                        │      │         │              ├──> ended_by_agent
     │          │      └────────────── next step ───────────────┘      │         │              └──> ended_on_no_progress
     │          └──────────────────────────────────────────────────────┘         │
     │                                                                           │
     └───────────────────────── any phase, on interruption ─────────────────────────> abandoned
```

Exits from the loop: `verifying` yields the agent's end-turn decision ⇒ `ended_by_agent`; the
no-progress counter reaches `no_progress_step_limit` ⇒ `ended_on_no_progress`. There is no timed
exit.

**Where failures go, which is not uniform and matters:**

| Failure | Result |
|---|---|
| `quicksaving` fails | Run halts. The turn never comes into existence (FR-007) |
| `persisting` fails | Run halts. The turn is not ended (FR-013) |
| Observation cannot be assembled at step *n* > 1 | Attempt `abandoned` and replayed from its start quicksave — the turn may **not** be finished from a stale board (spec edge case, FR-046) |
| Model chain exhausted at any step | Run pauses in a recorded state; no fabricated decision, no skipped turn (FR-042) |
| Crash or hang at any phase | Attempt `abandoned`; recovery replays from the start quicksave (FR-045) |

The third row is the one that is easy to get wrong. After several steps have already executed and
verified, finishing the turn from the last good view is the tempting recovery — and it is precisely
the stale-board play the within-turn loop exists to prevent.

### No-progress accounting (FR-014)

```text
counter = 0
per step:
    execute + verify
    if decision rejected OR verification shows no game-state change:  counter += 1
    else:                                                             counter = 0
    if counter == no_progress_step_limit:  end turn as ended_on_no_progress
```

The reset on any state-changing step is what makes this a stuck-agent detector rather than a step
cap: a turn of 400 productive steps never approaches the limit, while an agent spinning on illegal
orders reaches it in `no_progress_step_limit` steps regardless of how long the turn has run.

---

## 6. DecisionStep

*Mixed — in-game content, out-of-game timing.* One iteration of the within-turn loop: the
observation assembled at that point, the single decision the agent issued from it, and that
decision's execution outcome (FR-008, FR-012).

| Field | Type | Notes |
|---|---|---|
| `decision_step_id` | id | |
| `turn_cycle_id` | id | |
| `step_index` | int | 1-based, contiguous within the attempt; the sequence the agent saw and did |
| `observation_id` | id | Assembled fresh for **this** step — required |
| `decision_id` | id | Exactly one decision per step — required |
| `model_call_id` | id | The call that produced it — required (FR-042) |
| `progress` | enum | `changed_state` \| `no_change` \| `rejected` — feeds the no-progress counter |
| `no_progress_streak_after` | int | The counter's value after this step |
| `visually_degraded` | bool | True if this step ran without its images (FR-050) |
| `started_at` / `ended_at` | timestamp | Out-of-game |

**Validation**:

- **One decision per step, and one step per decision.** The agent is never asked to commit to a
  later decision before seeing the earlier one's result, so a step carrying two decisions cannot be
  constructed (FR-008).
- **One model call per step.** A turn contains as many model calls as it contained steps; a step
  without a `model_call_id` is a fabricated decision and cannot exist (FR-042, SC-012).
- **The observation is this step's.** It is assembled after the previous step's effect was verified,
  and an observation from an earlier step may not be reused (FR-008). Likewise its captures are
  fresh — an earlier step's capture may not stand in for this one (FR-015).
- **`step_index` is contiguous from 1.** A missing index is a record gap, exactly as a missing turn
  number is (SC-003).
- **`progress` is derived from verification, never asserted.** `changed_state` requires the action's
  declared verification predicate to have observed a game-state change; `no_change` means it
  executed without changing anything; `rejected` means it was refused before or at execution.
- **The last step of a turn** is either the agent's end-turn decision (`ended_by_agent`) or the step
  whose `no_progress_streak_after` reached the configured limit (`ended_on_no_progress`).

**On self-cancelling work**: a later step may undo what an earlier step did, having seen the result.
Both steps are recorded as issued, both are `changed_state`, and the turn's recorded yields reflect
the resulting state rather than the intent (spec edge case).

---

## 7. Observation

*In-game, parity-filtered.* The view assembled for one **decision step** (FR-012, FR-024).

| Field | Type | Notes |
|---|---|---|
| `observation_id` | id | |
| `decision_step_id` | id | Step-scoped, not turn-scoped |
| `assembled_at` | timestamp | |
| `catalog_version` | CatalogVersionRef | |
| `entries` | list[ObservationEntry] | Structured state |
| `captures` | list[id] | ScreenCapture references shown to the agent at this step |
| `screen_identity` | string | Which game screen was up (R13) |

**ObservationEntry**

| Field | Type | Notes |
|---|---|---|
| `declaration_id` | id | The catalog entry that produced it — required |
| `key` | string | |
| `value` | json | |
| `context` | enum | `GameCore_Tuner` \| `InGame` |

**Validation**:

- Every entry resolves to a `ParityDeclaration` in the recorded catalog version. An entry without one
  cannot be constructed — there is no unattributed-value path (FR-016, SC-007).
- The forbidden-field guard asserts absence of: unrevealed map contents, opponent internal state,
  hidden AI intent, undisclosed opponent research or civics, unit/city data beyond what the standard
  UI reveals at that moment, RNG state, and any debug or provenance data (FR-019).
- No harness telemetry appears as game information — model identity, cost, latency, retries, save
  lineage, run configuration, wall-clock timing (FR-020, SC-008).
- After an interruption, an observation must be assembled fresh; observations predating the
  interruption are not reusable (FR-046). The same rule applies *within* a turn across steps: the
  observation exists to show the effect of the previous decision, so reuse defeats its purpose.
- If assembly fails mid-turn after earlier steps have executed, the attempt is abandoned and
  replayed rather than continued from the last good observation (spec edge case).

---

## 8. ScreenCapture

*In-game (the image) with out-of-game provenance.* An image of the game's own view bound to one run,
turn, **and decision step** (FR-015, FR-025, FR-030).

| Field | Type | Notes |
|---|---|---|
| `capture_id` | id | |
| `run_id` / `turn_number` | id / int | Binding is required |
| `decision_step_id` | id | Binding is required — captures are per step (FR-015) |
| `captured_at` | timestamp | |
| `camera_state` | object | Position, zoom, view mode that produced it |
| `view_declaration_id` | id | The declared visual view (FR-024) |
| `screening_status` | enum | `screened_clean` \| `withheld` |
| `withheld_reason` | enum? | `non_player_ui` \| `geometry_mismatch` \| `provenance_failure` \| `capture_failed` |
| `shown_to_agent` | bool | |
| `retained_as_evidence` | bool | |
| `blob_ref` | string? | Content-addressed; null when withheld |
| `capture_path` | enum | Which mechanism produced it (R6) |

**Validation**:

- **A withheld capture is never stored and never shown** — it is recorded as an event with its
  reason, not persisted as an image (FR-025, FR-030, SC-019).
- `shown_to_agent = true` requires `screening_status = screened_clean` and a
  `view_declaration_id` resolving in the run's catalog version.
- `camera_state` must be human-reachable, and the movement that produced it must itself have been a
  declared, accepted camera action (FR-026, SC-009).
- **No capture is reused across steps.** A capture belongs to exactly one `decision_step_id`;
  standing an earlier step's image in for a later one would show the agent a board that predates its
  own last action (FR-015).
- A step whose captures are unavailable after bounded retries sets `DecisionStep.visually_degraded`,
  which rolls up to `TurnCycle.visually_degraded` and downgrades `Run.comparability_status`
  (FR-050, SC-013). Degradation is recorded at the step it occurred, so a turn that lost its images
  part-way is distinguishable from one that never had them.
- A capture may exist with `screening_status = withheld` and no blob — that record is the evidence
  the screening worked, and deleting it would hide a signal SC-009 audits.

---

## 9. Decision

*In-game intent, recorded with out-of-game execution metadata.* The single action the agent issued
at one decision step (FR-012).

| Field | Type | Notes |
|---|---|---|
| `decision_id` | id | |
| `decision_step_id` | id | Step-scoped; position within the turn comes from the step's `step_index` |
| `action_declaration_id` | id | Catalog entry — required, including for end-turn |
| `parameters` | json | |
| `reasoning` | text | As stated by the agent |
| `trigger` | enum | `proactive` \| `prompt_response` (FR-010) |
| `prompt_type` | string? | Set when `trigger = prompt_response` |
| `is_end_turn` | bool | True for the declared end-turn action (FR-008) |
| `model_call_id` | id | The call that produced it |
| `execution` | ActionExecution | |

**ActionExecution**

| Field | Type | Notes |
|---|---|---|
| `outcome` | enum | `applied` \| `rejected` \| `partially_applied` |
| `rejection_reason` | enum? | `not_in_catalog` \| `unavailable_to_human_now` \| `illegal_in_context` \| `out_of_parity_camera` \| `verification_failed` |
| `verification` | object | The predicate declared on the action entry, and its observed result |
| `verified_at` | timestamp | |

**Validation**:

- **No action is recorded `applied` without verification** against resulting game state (FR-011).
  `outcome` is derived from the verification predicate, never asserted by the executor.
- A requested action absent from the catalog, or unavailable to a human in the current context, is
  rejected and recorded with its reason — and not performed (FR-017).
- Contradictory or self-cancelling decisions execute in step order; the resulting state is what is
  recorded, not the intent (spec edge case). Ordering is now inherent rather than enforced — the
  agent issues one decision at a time, so there is no batch to sequence.
- **End-turn is a decision like any other.** `is_end_turn` marks it, but it still resolves to a
  catalog declaration, still carries reasoning, and is still recorded through the same path — it is
  not harness control flow escaping the record (FR-008, SC-022).
- **No fabricated decisions.** A step may not exist without a `model_call_id`; the harness has no
  path that writes a default or heuristic move as an agent decision (FR-042, SC-012). This includes
  the turn's ending: a turn ended on no progress records the backstop on the `TurnCycle`, and does
  **not** synthesise an end-turn decision to represent it.
- Every game-initiated prompt encountered is either a `prompt_response` decision or a recorded stall
  — never dismissed, defaulted, or absorbed (FR-010, SC-005).

---

## 10. ParityDeclaration (catalog entry)

*Out-of-game.* The boundary itself: one observable, visual view, or action, with its human
equivalent. Stored as versioned YAML under `catalogs/`; see
[contracts/capability-catalog.md](./contracts/capability-catalog.md).

| Field | Type | Notes |
|---|---|---|
| `declaration_id` | string | Stable, human-readable (e.g. `units.move_to`) |
| `kind` | enum | `observation` \| `view` \| `action` |
| `summary` | string | What it exposes or does |
| `parity_basis` | string | **Required, non-empty.** The in-client action a human takes |
| `context` | enum | `GameCore_Tuner` \| `InGame` |
| `capability_id` | string | Implementing `IntegrationCapability` |
| `availability_predicate` | string? | When it is available to a human (actions) |
| `verification_predicate` | string? | How its effect is verified (actions) |
| `output_schema` | json-schema? | Shape of the value produced (observations) |
| `camera_requirements` | object? | **Required when `kind == view`, absent otherwise.** `mode` ∈ {world, strategic, city_screen, diplomacy, congress}, `zoom_range` (must be within what the standard UI allows), `target_must_be_revealed: true` (FR-024, FR-026) |
| `screening_profile` | string? | **Required when `kind == view`, absent otherwise.** Which detector profile applies (R7) |
| `introduced_in_version` | string | Catalog version of first appearance |

**Validation**: a declaration missing `parity_basis` fails catalog load, as does a `view` declaration
missing `camera_requirements` or `screening_profile` — matching the `view` entry shape in
contracts/capability-catalog.md exactly. Declarations are immutable within a version; a change
produces a new catalog version (FR-022).

### CatalogVersion

| Field | Type | Notes |
|---|---|---|
| `version` | string | From `catalogs/VERSION` |
| `content_hash` | string | Hash over all catalog files |
| `declaration_ids` | list[string] | The full set in force |

Recorded on every run so runs before and after a catalog change stay distinguishable (FR-022,
spec edge case on mid-project capability additions).

---

## 11. IntegrationCapability

*Out-of-game.* The implementation behind one or more declarations (FR-027 – FR-029).

| Field | Type | Notes |
|---|---|---|
| `capability_id` | string | |
| `path` | enum | `firetuner` \| `bespoke` |
| `implementation_ref` | string | Lua file or module path |
| `reads` / `writes` | list[string] | What it touches (FR-028) |
| `firetuner_gap` | string? | **Required when `path = bespoke`** — why Firetuner could not do it |
| `parity_basis` | string? | **Optional — inherited or restated.** Omitted means inherited: the `ParityDeclaration`(s) this capability implements already each carry their own required, non-empty `parity_basis`, so the fact need not be duplicated here. When present, it is a restatement and must be non-empty, same as the declaration-level field |

**Validation**: `path = bespoke` without a non-empty `firetuner_gap` fails catalog load — this is
what makes SC-020's 100 % coverage checkable rather than aspirational (FR-028). A present-but-empty
`parity_basis` also fails catalog load; an absent `parity_basis` is valid and means inherited.

---

## 12. ModelCall

*Out-of-game.* One request to the provider layer for one decision step's decision (FR-040). A turn
contains as many model calls as it contained decision steps.

| Field | Type | Notes |
|---|---|---|
| `model_call_id` | id | |
| `run_id` / `turn_cycle_id` / `decision_step_id` | id | Attributed to the step it served |
| `model_requested` | ModelRef | |
| `model_served` | ModelRef | May differ — fallback |
| `latency_ms` | int | |
| `cost` | object | From provider-reported usage; not independently priced |
| `retry_count` | int | |
| `fallback_occurred` | bool | |
| `image_count` | int | Images actually sent |
| `outcome` | enum | `decision_returned` \| `empty_response` \| `failed` \| `rate_limited` \| `context_rejected` |

**Validation**:

- Every call records the model that actually served it. Because fallback is decided per call and a
  call serves one step, a single turn may be served by more than one model — so "distinguishable"
  (SC-016) resolves at the step and rolls up to the turn, rather than being a per-turn property
  (FR-040).
- **A successful call returns exactly one decision**, not a list. `decision_returned` is singular by
  design: a list-shaped outcome would readmit the batching FR-008 forbids.
- Per-call cost and latency are recorded and never aggregated away, because per-turn cost is now
  unbounded by construction and the spec's assumption is that the trade-off stays visible rather
  than being managed.
- `image_count` must equal the number of images in the assembled observation. A mismatch is a defect,
  not a degradation — the harness must never drop images to fit a limit (FR-039, SC-017).
- `empty_response` is a failed call, not a decision to do nothing (spec edge case).
- No credential appears in this record or anywhere reachable from it (FR-043, SC-018).

---

## 13. SavePoint

*Out-of-game.* A named, addressable save bound to a run and turn (FR-032).

| Field | Type | Notes |
|---|---|---|
| `save_point_id` | id | |
| `run_id` / `turn_number` | id / int | |
| `save_name` | string | `civsim__<run_id>__t<turn:04d>` |
| `taken_at` | timestamp | |
| `verified` | bool | Filesystem-confirmed, size-stable (R5) |
| `lineage` | object | Parent run and turn where applicable |
| `retention_status` | enum | `retained` \| `eligible` \| `removed` |
| `missing` | bool | Set when recovery finds it absent |

**Validation**:

- Addressable by run, turn, and lineage without inspecting the filesystem or the game client
  (FR-032) — `save_name` is a naming convention, not the address.
- **`retention_status = eligible` has exactly one cause: `Run.archived_at` is set** (FR-036, R17).
  Not age. Not a disk quota. Not a retention window. Not the run being terminal, finished, failed,
  or superseded. Not thinning. A run is branchable until an operator says otherwise, however old it
  is, because every turn-start save is a branch point and an automatic rule deletes branch points
  nobody decided to give up.
- **Disk pressure is not a cause either.** When free space falls below `min_free_disk_gb` the run
  halts in a recorded state rather than freeing space by deleting an unarchived save (spec edge
  case, R17).
- A recovery finding a required save missing reports it and fails rather than resuming from a
  different turn (FR-036) — hence `missing` is recorded, not inferred. This includes a save removed
  after its run was archived: the record survives the file, which is what makes the failure
  explainable rather than mysterious.
- Two runs started from the same save point begin from an identical position (FR-034, SC-014).

---

## 14. RunEvent

*Out-of-game.* Any non-turn occurrence on a run's timeline.

| Field | Type | Notes |
|---|---|---|
| `event_id` | id | |
| `run_id` | id | |
| `turn_number` | int? | Where applicable |
| `step_index` | int? | Where the occurrence is attributable to a decision step |
| `event_type` | enum | See below |
| `occurred_at` | timestamp | |
| `detail` | json | Type-specific, credential-redacted |

**Event types**: `lifecycle_transition`, `lifecycle_command_received`, `preparation_mismatch`,
`crash_detected`, `hang_detected`, `unresponsive_detected`, `unknown_screen`, `save_taken`,
`save_failed`, `save_missing`, `resumed`, `turn_abandoned`, `turn_ended_on_no_progress`,
`observation_assembly_failed`, `provider_failure`, `provider_retry`, `provider_fallback`,
`context_rejected`, `model_chain_exhausted`, `image_withheld`, `capture_failed`,
`operator_intervention`, `game_over_detected`, `branch_created`,
`branch_abandoned`, `run_archived`, `game_build_change_accepted`, `disk_headroom_low`,
`persistence_failure`, `recovery_limit_reached`.

`game_over_detected` is the turn-boundary reading of the game's own ending (2026-09-21): the local
player's alive state, the winning team if there is one, and — only once the game is already over,
because only then does a human see them — the victory type's name and the winning civilization as
the end-game screen states them. Written by `run/turn_cycle.py` **before** the start-of-turn
quicksave and before the run is transitioned, so the timeline says why the run stopped even if the
transition then fails. Measured cause: a defeat at game turn 59 made the next turn's quicksave
impossible, and the run paused with a save error instead of finishing with its defeat.

Five of these are new with the clarification session and are worth naming individually, since each
is the audit trail for a rule that would otherwise be invisible:

| Event | Why it must be an event |
|---|---|
| `turn_ended_on_no_progress` | SC-022 requires a backstop-ended turn to be distinguishable from one the agent chose to end. The `TurnCycle.outcome` records it; the event puts it on the timeline |
| `observation_assembly_failed` | Marks the mid-turn stale-board case that forces a replay rather than a continuation (FR-046) |
| `run_archived` | Archival is the *only* thing that makes a save eligible for removal (FR-036). An eligibility change with no recorded cause would be exactly the silent deletion R17 forbids |
| `game_build_change_accepted` | The operator override on the build pin. Recorded on the seed set and on every dependent run, and here on the timeline (FR-002) |
| `disk_headroom_low` | The warning before the halt. Without it the halt looks arbitrary, and the alternative — deleting a save to continue — is forbidden |

**`stall` is gone.** It denoted a turn exceeding its time budget; there is no time budget. What
replaced it is not a rename: `turn_ended_on_no_progress` is a normal, recorded turn ending rather
than a fault, while a genuinely stuck *game* now surfaces as `hang_detected` or `unknown_screen`.

**Validation**: every run lifecycle transition, every provider failure/retry/fallback, every withheld
image, every crash, and every archival or build acceptance produces an event — these are the timeline
entries deliverable 1 renders (its FR-004) and the audit trail SC-011 and SC-021 read.

---

## Cross-cutting invariants

Stated once here because they hold across entities and are what the test suites assert:

| # | Invariant | Requirements |
|---|---|---|
| **I1** | Nothing reaches the agent that does not resolve to a `ParityDeclaration` in the run's recorded catalog version | FR-016, FR-018, FR-023, SC-006, SC-007 |
| **I2** | No turn exists without a verified turn-start quicksave | FR-007, SC-004 |
| **I3** | No turn ends before its complete record is durably persisted | FR-013, SC-003 |
| **I4** | No action is recorded `applied` without verification against game state | FR-011 |
| **I5** | No decision exists without a `ModelCall` that produced it | FR-042, SC-012 |
| **I6** | No image is shown or stored without passing screening; failures are withheld and recorded | FR-025, FR-030, SC-009, SC-019 |
| **I7** | No model call carries fewer images than the observation contains | FR-039, SC-017 |
| **I8** | No credential appears in any record, capture, log, or agent context | FR-043, SC-018 |
| **I9** | Exactly one authoritative attempt per `(run, turn)`; abandoned attempts are retained | FR-047 |
| **I10** | Exactly one recorded stop resolution per terminated run | FR-005 |
| **I11** | Every gap in the turn sequence is explicitly marked and reflected in completeness status | FR-052, SC-011 |
| **I12** | A branch never modifies or invalidates its parent's record | FR-034, SC-014 |
| **I13** | Exactly one decision and one model call per decision step; no step carries a batch | FR-008 |
| **I14** | Every step's observation and captures are assembled after the previous step's effect was verified; neither is reused across steps | FR-008, FR-015 |
| **I15** | `step_index` is contiguous from 1 within every authoritative turn attempt | SC-003 |
| **I16** | A turn ends only by the agent's declared end-turn decision or the no-progress backstop — never by elapsed time, step count, or cost | FR-008, FR-014, SC-022 |
| **I17** | A save point is `eligible` only because its run was explicitly archived | FR-036 |
| **I18** | A run whose build differs from its seed set's carries an acceptance reference, or does not exist | FR-002, FR-031 |

**I13 – I16 are the clarification session's invariants**, and I16 is the one most likely to be
eroded by a well-meaning change: every plausible-sounding guard rail — a turn timeout "for safety", a
step cap "to bound cost", a cost ceiling — is a violation. The spec's position is that an expensive
turn is the price of play fidelity, and that the only turn the harness may end is one that has
stopped accomplishing anything.
