# Phase 1 Data Model: Harness Liveness, Reconciliation, and Autonomous Recovery

**Feature**: `004-liveness-reconciliation-recovery` | **Date**: 2026-09-22 |
**Plan**: [plan.md](./plan.md) | **Research**: [research.md](./research.md)

This model exists to make three things structurally impossible rather than merely discouraged:

1. **A signal that asserts more than it observes.** `ProgressSignal` carries
   `presence_does_not_establish` as a **required, non-empty** field, and a `derivation` that says
   whether the signal comes from the work or from something beside it. A signal cannot be registered
   without answering both.
2. **A rung that fires on an unobserved precondition.** `RecoveryAttempt` cannot be constructed
   without a `precondition_observation`; a rung that could not observe its precondition produces a
   recorded **skip**, which is a different record from a failure.
3. **A verification that is not independent.** `IndependentRead` refuses construction unless the
   read is in a later tuner command than the write it verifies, so a same-command readback cannot
   reach a reconciliation comparison at all — it produces `unverified`, never `diverged`.

Entities marked **NEW** are introduced here. Entities marked **CHANGED** already exist in spec 002
or 003 and are altered; each such change names what the old value meant and what it means now,
because a name whose meaning silently widens is one of the three defects this feature is designed
against.

---

## Entity overview

| # | Entity | Grain | Persisted where |
|---|---|---|---|
| 1 | **ProgressSignal** (NEW) | one per registered signal kind | Registry as data; hashed onto the run |
| 2 | **LongPhaseRosterEntry** (NEW) | one per phase that can outlast the ceiling | Roster as data; audited per release |
| 3 | **LivenessRecord** (NEW) | one tick | Telemetry log + per-run NDJSON liveness stream |
| 4 | **SignalObservation** (NEW) | one reading of one signal at one moment | Held in the monitor; summarised onto a classification |
| 5 | **StallCandidate** (NEW) | one run's entry into the "something may be wrong" state | Match store (`RunEvent` — `liveness_silence_observed`) |
| 6 | **StallClassification** (NEW) | one disposition for one candidate | Match store (`RunEvent`) |
| 7 | **BlockingViewDescriptor** (NEW) | one registered view | `catalogs/views/blocking_views.yaml` |
| 8 | **RecoveryRung** (NEW) | one level of the ladder | Declared in code; availability resolved per host per run |
| 9 | **RecoveryAttempt** (NEW) | one firing or recorded skip | Match store (`RunEvent`) |
| 10 | **IndependentRead** (NEW) | one engine read used as evidence | Embedded in verifications and checks |
| 11 | **ReconciliationCheck** (NEW) | one comparison at one checkpoint | Match store (`RunEvent`) |
| 12 | **DivergenceIncident** (NEW) | one recorded disagreement | Match store (`RunEvent`) + run status |
| 13 | **ClientLifecycleCapability** (NEW) | one per host | Resolved at preparation; recorded on the run |
| 14 | **RunLockClaim** (CHANGED) | one lock file | Filesystem lock directory |
| 15 | **FailureFixture** (NEW) | one fixture **paired with** its negative control | Test tree, rostered as data |
| 16 | **StallAccounting** (NEW) | per stall, rolled up per run | Match store |
| — | **Run** (CHANGED) | — | `record_completeness_status` widens; rung availability recorded |
| — | **RunEvent** (CHANGED) | — | New event kinds; no schema version change |

---

## 1. ProgressSignal (NEW)

One directly observed event that establishes something about a run's progress. The registry of these
**is** the FR-002 boundary, in the same way the capability catalog is the Principle I boundary.

| Field | Type | Required | Notes |
|---|---|---|---|
| `signal_id` | string | yes | Stable identifier, e.g. `provider.call.bytes_arriving` |
| `observes` | string | yes | The event it **directly** observes. Not a paraphrase of its name |
| `source_of_truth` | string | yes | The component that is authoritative for it |
| `change_frequency` | string | yes | How often it can change — a ceiling, not an average |
| `absence_establishes` | string | yes | What it means when this signal does not appear |
| `presence_does_not_establish` | string | yes, **non-empty** | The clause that makes the entity honest. Empty string is rejected at load |
| `derivation` | enum | yes | `work_derived` \| `observer_derived` |
| `phase_id` | string \| null | yes | The `LongPhaseRosterEntry` it describes, where it describes one |

### Validation

- **`presence_does_not_establish` must be non-empty.** A signal cannot be registered without it.
  This is the entity-level form of FR-002 and SC-023, and it is a required field rather than
  documentation because the failure it prevents is a reviewer reading a name and supplying the
  meaning themselves.
- **A signal MUST NOT be named for a condition it does not directly observe.** Worked negatives,
  all three from this project's record: *"a model request is open"* is not *"the agent is
  thinking"*; *"the client process exists"* is not *"the game is responsive"*; *"the harness
  believes a prompt is blocking"* is not *"the game is blocking"*.
- **Only a `work_derived` signal may clear the silence bound for its phase** (research R2). An
  `observer_derived` signal bounds the gap between records and is evidence that the harness process
  and its emitter are alive — nothing more. A phase whose only signal is `observer_derived` yields
  **undetermined** on silence, never *stalled*.

### The initial registry

| `signal_id` | `derivation` | `presence_does_not_establish` |
|---|---|---|
| `provider.call.waiting` | `observer_derived` | that the provider call is progressing — only that the harness process and its ticker thread are alive |
| `provider.call.bytes_arriving` | `work_derived` | that the response will parse, or that the chain will not fall back |
| `act.confirm_execution.waiting` | `work_derived` | that the action succeeded — only that the tuner answered within the last poll |
| `watchdog.monitor.pass` | `work_derived` | that the run is healthy — only that the monitor completed its reads |
| `harness.record_written` | `work_derived` | that the turn advanced |
| `client.process_present` | `observer_derived` | that the game is responsive, or that it is the client this run owns |
| `tuner.heartbeat.round_trip` | `work_derived` | that the UI is interactive — a game can answer Lua while stuck on a modal |
| `engine.state_changed` | `work_derived` | that the harness caused the change |

*(`client.process_present` is `observer_derived` on purpose and the classification matters: it is the
signal behind the leaked-lock incident, where a live process was read as a live owner.)*

---

## 2. LongPhaseRosterEntry (NEW)

The SC-026 enumeration, as data rather than prose, so it can be re-run per release.

| Field | Type | Notes |
|---|---|---|
| `phase_id` | string | e.g. `provider.completion`, `act.confirm_execution` |
| `where` | string | Module and function |
| `can_outlast_ceiling` | bool | Why — with the bound that permits it |
| `bound_s` | number \| null | The phase's own bound, and its provenance |
| `emits` | enum | `no` \| `bracketing_only` \| `periodic_tick` |
| `signal_ids` | list | The `ProgressSignal`s it publishes |
| `streaming_verified` | bool | Empirically observed growing **during** the wait |
| `rationale` | prose | Per-entry, mandatory |

### Validation

- **`emits: bracketing_only` is a failure, not a partial pass.** A phase that logs at entry and exit
  is silent for exactly the interval that matters and is indistinguishable from a wedged phase
  throughout it.
- **`streaming_verified` must be established by observation**, never inferred from the presence of
  an emit call. An emission that buffers and flushes at completion is the same defect wearing a fix.
- **An entry may not be discharged by raising `bound_s` or the ceiling.** Widening a bound to
  accommodate silence is how a threshold gets chosen without measuring what it bounds.
- **A phase absent from the roster is a phase nobody checked**, and the release audit fails on an
  unrostered long phase rather than on a silent one only.

---

## 3. LivenessRecord (NEW)

One tick. Written to the telemetry log **and** to the per-run append-only NDJSON liveness stream by
the same `log_event` call, so the two cannot drift.

| Field | Type | Notes |
|---|---|---|
| `run_id` | string | Scopes the record to the thing the watchdog guards |
| `signal_id` | string | Must resolve in the registry |
| `emitted_at` | RFC 3339 | |
| `seq` | int | Monotonic per run; the stream's growth is the signal |
| `payload` | object | Bounded, flat, built from values the emitter already holds |

### Validation — the Principle I boundary on a tick

- **No game state.** A confirm tick carries the `declaration_id` and the **static catalog predicate
  expression** — the shape of the question — and never the predicate bindings or a `last_read`
  quotation.
- **No model content.** A provider tick carries provider, model, elapsed, bound and tick number, and
  is built **before the call starts** from values already in hand.
- **Never returned to a caller**, never merged into an `ExecutionVerification` or a
  `DecisionResponse`, never reaching an `Observation`. There is no code path by which a tick can
  reach the agent (FR-048, SC-020).
- **A failed emission is swallowed.** Telemetry must never fail the call it describes — the emission
  exists to describe the work, so letting it break the work inverts the point.

---

## 4. SignalObservation (NEW)

One reading of one signal at one moment on one run.

| Field | Type | Notes |
|---|---|---|
| `signal_id` | string | |
| `observed_at` | RFC 3339 | |
| `status` | enum | `present` \| `absent` \| **`unavailable`** |
| `value` | any \| null | |
| `unavailable_reason` | string \| null | Required when `status = unavailable` |

### Validation

**`unavailable` is a first-class status and MUST NOT collapse into `absent`** (FR-006). A run
emitting nothing over a working channel is evidence of a stall. A signal source the watchdog cannot
reach — the stream is unreadable, the state query fails — is *instrument failure*: it is reported as
unavailable, is never reported as absence of progress, and pushes the classification toward
*undetermined* rather than toward a kill. Naming these the same thing is precisely the defect FR-002
exists to prevent, applied to readings instead of to signals.

---

## 5. StallCandidate (NEW)

A run that has stopped producing observable progress. **A candidate, not a verdict** — it exists so
that the thing which *triggers* classification is a separate record from the thing which *concludes*
it, and elapsed silence cannot leak into the verdict.

| Field | Type | Notes |
|---|---|---|
| `candidate_id` | string | |
| `run_id` | string | |
| `opened_at` | RFC 3339 | |
| `silence_started_at` | RFC 3339 | Last affirmative signal of any kind |
| `bound_applied_s` | number | The **derived** bound in force for this run |
| `bound_source` | enum | `ceiling` (180 s) \| `derived_from_run_history` |
| `bound_binding_was_ceiling` | bool | Release metric: in a correct system this is always false |
| `resolved_by` | ref \| null | The `StallClassification` that concluded it |

### Validation

- **Elapsed time alone MUST NOT classify a run as stalled** or make any rung above report-only
  eligible. What opens a candidate is *elapsed time during which nothing affirmative was emitted*.
- **`bound_binding_was_ceiling = true` is a finding about a missing emission**, not about the
  number. The ceiling is a backstop that should never bind once the roster is clean; the release
  audit reports its count and a non-zero count points at an unrostered or silent phase.

---

## 6. StallClassification (NEW)

| Field | Type | Notes |
|---|---|---|
| `classification_id` | string | |
| `candidate_id` | ref | |
| `disposition` | enum | `live` \| `blocked_by_game` \| `blocked_by_harness` \| `unreachable` \| `undetermined` |
| `cause` | enum \| null | Required when `disposition = unreachable` |
| `evidence` | list of `SignalObservation` | Every signal it rested on |
| `independent_reads` | list of `IndependentRead` | Every engine read it rested on |
| `monitor_load` | object | The monitor's own cost for this pass (FR-052) |
| `concluded_at` / `latency_ms` | | Excluded from latency stats when the resolution was human-initiated |

### Disposition rules

| Disposition | MUST mean | MUST NOT be concluded from |
|---|---|---|
| `live` | an affirmative signal was observed, **at least one of which did not come from the component whose health is being asserted** | harness state alone — see class 4 below |
| `blocked_by_game` | an **independent read confirms** the game is holding a view or state the board cannot leave | harness state alone; a probe failure (FR-051) |
| `blocked_by_harness` | the harness's belief and the engine's answer **disagree**, with the engine permitting what the harness refuses | elapsed time; the harness's belief alone |
| `unreachable` | the harness **cannot obtain an engine answer at all** | a game answer it did receive |
| `undetermined` | the classifier cannot decide, or evidence for two dispositions conflicts | — |

**`unreachable` causes** (FR-012), resolved *before* any rung is eligible:
`lock_held_by_other_party` · `connection_refused_in_close_tail` (~2 s, measured on this build) ·
`client_process_absent` · `client_present_not_answering`.

### Validation

- **Exactly one disposition**, produced **before** anything acts. No rung above report-only becomes
  eligible until a disposition exists.
- **At least one observation independent of the component under suspicion** (FR-010). A
  classification about the harness's own state is not derivable from harness state alone.
- **A `live` disposition resting only on harness state is recorded `undetermined`** (FR-010,
  SC-028). **Class 4 is why**, and it is the most expensive clause in this model: the screen probe
  answers `recognized=true, has_blocking_prompt=false` with a full-screen modal up, so the harness
  is not merely wrong but *confidently* wrong, about the one proposition that makes a watchdog stand
  down. A detector consulting harness state does not fail to notice — **it agrees, and certifies a
  dead board as healthy, repeatedly.** No elapsed-silence fallback rescues it, because the harness
  is not silent. So "the run looks fine" is **not a conclusion the classifier may reach from harness
  telemetry**, however much of it there is. Class 4 adds no sixth disposition — a board held by a
  modal is still `blocked_by_game` — it adds the case where no candidate opens at all.
- **Every detector, probe and comparison must be able to return "I do not know"** (FR-060). One that
  structurally cannot is an **allowlist being read as a detector** and is not accepted: used to
  permit, unknown means deny and the failure is a visible false refusal; used to detect, unknown
  means "nothing there" and the failure is an invisible, confident false all-clear.
- **`undetermined` is a correct outcome of the detector, not a failure of it** (FR-015). Under it,
  only rung 0 is eligible, and the classifier degrades toward the cheapest, least destructive rung
  and toward reporting. **Never escalate on uncertainty.**
- **A classification that cannot rule the monitor out as the cause of the fault is `undetermined`**
  (FR-052).
- **Re-derivable from the record** (FR-014): `evidence` + `independent_reads` must be sufficient to
  reach the same disposition without re-running the detector.

---

## 7. BlockingViewDescriptor (NEW)

`catalogs/views/blocking_views.yaml`, versioned as data like the rest of the catalog.

| Field | Type | Notes |
|---|---|---|
| `view_id` | string | |
| `recognised_by` | string | The declared observation that identifies it |
| `carries_player_choice` | bool | **Including a single acknowledging button** |
| `human_action` | string | What a human does: Escape, or click this control |
| `declaration_id` | string | The declared action that performs it, with its parity basis |
| `reachable_by_input_path` | map host-profile → bool + reason | Per host, resolved at preparation |
| `closed_confirmed_by` | string | The independent read that confirms the view closed |

### Validation

- **A view that is not registered MUST stall the run visibly** with the unknown view recorded
  (spec 002 FR-049). Nothing is dismissed by guesswork.
- **`carries_player_choice = true` routes to the agent first** (FR-047). The worked case is
  "Goodbye": it looks like a dismissal and is in fact the player's answer. The watchdog's job is to
  make it answerable, not to answer it.
- **`closed_confirmed_by` may never be the return value of the dismissing call.** Two measured
  hazards, and satisfying one does not satisfy the other: a call can report success without acting
  (the popup click callback), and a re-read in the same command returns the pre-call value (the
  retracted close-call finding).
- **`reachable_by_input_path = false` is a precondition to be observed, not a limitation to be
  discovered.** Two cases are already measured false: the post-defeat exit-confirm modal ignores
  synthetic input, and a Wayland session blocks synthetic input by design.

---

## 8. RecoveryRung (NEW)

| Field | Type | Notes |
|---|---|---|
| `rung` | int | 0–5; the ladder's order **is** its destructiveness order |
| `answers_disposition` | enum | And `answers_cause` where sub-classified |
| `precondition` | string | What must be **observed**, named for what actually governs it |
| `cost` / `destroys` | string | What is lost when it fires |
| `verified_by` | string | The independent read that confirms its outcome |
| `available_on_host` | bool + reason | Resolved at preparation and reported (FR-039) |

| Rung | Answers | Precondition | Destroys | Status |
|---|---|---|---|---|
| 0 observe and report | anything, incl. `undetermined` | none | nothing | new |
| 1 dismiss a registered view | `blocked_by_game` | registry entry matched **and** reachable on this host | nothing | new; needs a declared dismissal action |
| 2 re-derive belief from the engine | `blocked_by_harness` | belief and engine disagree, read independently | the stale belief only | new |
| 3 re-establish the path | `unreachable` + cause | the specific cause is established | nothing | partly exists |
| 4 reload and replay | `unreachable`, or an unresolved `blocked_by_game` | **a load-accepting context — a fresh main menu, not a reused one** | the in-progress attempt, retained as abandoned | exists (`resilience/recovery.py`) |
| 5 restart the client | `unreachable`, client absent/unresponsive | — | the client process | **UNAVAILABLE on every host** |

### Validation

- **A rung MUST NOT fire on the absence of a contrary observation** (FR-028) — only on a positively
  observed precondition.
- **Rung 2 replaces the belief with a freshly observed value obtained in a separate command** — not
  cleared, not reset to a default, not re-derived from the state that produced it. Clearing a flag
  makes the symptom go away without establishing what is true.
- **Rung 3 never force-takes a lock whose owner is live.**
- **Rung 4's precondition is named for what governs it.** The measured distinction is a *fresh main
  menu versus a reused one*; "post-defeat" is a correlate, and a rung named for the correlate fires
  in the wrong place. Where the precondition does not hold the rung is **skipped with the reason
  recorded — not attempted and retried**, because re-issuing a call that cannot succeed burns the
  whole attempt limit and converts a recoverable stall into a failed run.
- **Rung 5 is declared unavailable, and the harness never escalates toward it** (FR-034, SC-022).
  The ladder stops at rung 4 in a recorded failed state naming the last-known good save and the
  highest rung reached.
- **A rung MUST NOT be constructible without its required inputs** (FR-059). No default renders it
  inert; a missing input is a startup failure.

### Escalation state machine (FR-035)

```text
candidate opened
      │
      ▼
 classification ──► undetermined ──► rung 0 only ──► (report; re-classify on new evidence)
      │
      ▼
 rung N eligible ──► precondition observed? ──no──► SKIPPED(reason) ──► rung N+1
      │ yes
      ▼
   fire ──► independent verification ──► resolved ──► resume from a FRESH observation
      │                                     │
      │                                     └─ same stall recurs identically ──► rung N+1  (never rung N again)
      ▼
   failed ──► rung N+1 ──► … ──► highest available rung exhausted
                                        │
                                        ▼
                         recorded FAILED state: last-known good save + highest rung reached
```

**Monotonic within one stall. The ladder never cycles.** A stall that recurs identically after a
successful rung escalates rather than repeats — otherwise rung 1 dismisses a view, the view returns,
and the system presses Escape forever while reporting success each time.

---

## 9. RecoveryAttempt (NEW)

One firing **or recorded skip** of one rung against one stall.

| Field | Type | Notes |
|---|---|---|
| `attempt_id` / `classification_id` | | |
| `rung` | int | |
| `outcome` | enum | `fired_succeeded` \| `fired_failed` \| **`skipped`** |
| `skip_reason` | string \| null | Required when `skipped`; must name the **observably unsatisfiable** precondition |
| `precondition_observation` | ref | **Required for any firing.** Not constructible without it |
| `action_taken` | string | The declared action, never a raw path |
| `verification` | `IndependentRead` | Required for any firing |
| `initiated_by` | enum | `autonomous` \| **`human`** (FR-017) |
| `duration_ms` | number | |
| `wallclock_lost_ms` / `model_spend_discarded` | | Attributed to the stall (FR-042, FR-043) |

**A skip is a different record from a failure**, and conflating them is how "the ladder tried
everything" gets recorded for a ladder that could not try anything.

---

## 10. IndependentRead (NEW)

The type that makes FR-025 structural.

| Field | Type | Notes |
|---|---|---|
| `read_id` | string | |
| `what_was_read` | string | |
| `value` | any | |
| `write_command_seq` | int \| null | The command that performed the action being verified |
| `read_command_seq` | int | The command that performed this read |
| `retried_until` | RFC 3339 \| null | For asynchronous effects |
| `independence` | enum | `independent` \| **`same_command`** \| `bound_expired` |

### Validation

- **Construction fails unless `read_command_seq > write_command_seq`.** A readback issued in the
  same tuner command as the write returns the **pre-call value**; that artifact produced a finding
  on this project — "the engine's close call returns success and does nothing" — which the project's
  own ledger later **retracted**, against a call that was working correctly. It is live and open
  today as spec 002's T293, where the harness caused three turn advances and recorded that none
  happened.
- **Independent in *ordering*, not only in command.** Some engine effects are asynchronous. A read
  in a later command that was issued before the effect landed is still not a verification, so a
  verification read is retried under its own bound; a bound that expires yields
  `independence = bound_expired`.
- **A non-independent read never produces a divergence.** It produces `unverified`. A verification
  that is not independent does not merely fail to confirm — **it manufactures a false
  disagreement**, which under FR-021 becomes a recorded incident and under FR-027 can make a rung
  eligible. That is the most likely first bug in this feature and it is the one the owner said must
  not happen, arriving through the detector rather than through a timeout.

---

## 11. ReconciliationCheck (NEW)

| Field | Type | Notes |
|---|---|---|
| `check_id` / `run_id` / `checkpoint` | | `refused_action` \| `turn_boundary` \| `run_end` |
| `harness_side` | any | Verbatim |
| `engine_side` | `IndependentRead` | Verbatim |
| `outcome` | enum | `agreed` \| `diverged` \| **`unverified`** |

**Three outcomes, not two.** A comparison whose read was not independent is `unverified`. Collapsing
it into `diverged` manufactures incidents on healthy runs; collapsing it into `agreed` hides real
ones. It is FR-006's absent/unavailable distinction applied to reads.

**Minimum comparisons** (FR-020): the turn number the record claims vs the engine's; the harness's
belief that an action is unavailable vs the engine's own availability answer; an action recorded as
applied vs the engine state it should have produced.

---

## 12. DivergenceIncident (NEW)

| Field | Type | Notes |
|---|---|---|
| `incident_id` / `run_id` | | |
| `both_sides` | object | Captured **verbatim**, both of them |
| `play_stopped` | bool | Decides whether any rung is eligible — and only that |
| `resolution_status` | enum | `open` \| `explained` \| `superseded_by_additive_record` |
| `contradicts_record_ref` | ref \| null | Present only on an additive correction |

### Validation — the refusal that defines this entity

- **A divergence is an incident whether or not play stopped** (FR-021).
- **`play_stopped = false` ⇒ no rung fires.** Recorded and surfaced, never auto-corrected (FR-022).
- **No existing turn record is ever amended, rewritten, or deleted to agree with a later engine
  read.** A harness that edits its own evidence produces records indistinguishable from correct
  ones, so the very failure this feature exists to make visible would become invisible again.
  Deliverable 3's no-delete/no-edit floor and its published `MUTATING_OPERATIONS` surface are the
  enforcement; this feature relies on them rather than restating them.
- **A correction is an additive record naming what it contradicts** (FR-023), leaving the original
  intact and both readable — the same abandoned/authoritative pattern spec 002 FR-047 uses for a
  replayed turn.
- **Artifact authority** (FR-026): the **match store is authoritative**; the run timeline and any
  driver result output are reconciled against it at the end of every run, and disagreement is
  recorded as an incident rather than resolved silently.

---

## 13. ClientLifecycleCapability (NEW)

Modelled as an entity **because it does not exist**, so that its absence is a recorded fact about a
host rather than an assumption anyone can drift into.

| Field | Type | Notes |
|---|---|---|
| `host_profile` | string | |
| `can_start` / `can_restart` / `can_terminate` | bool | **All `false` on every host today** |
| `basis` | string | What was checked, so the negative result is evidence |

Verified for this plan rather than inherited: `host/port.py`'s `HostPlatform` declares nine methods
— `locate_game_process`, `find_game_window`, `capture_window`, `check_capture_preconditions`,
`list_window_titles`, `resolve_game_directories`, `send_input`, `focus_window`, `free_disk_space` —
and none is a lifecycle method; a search of `src/civsim_harness/` for any definition matching
launch/restart/terminate/kill/spawn/start-client/stop-client returns nothing. **The search could
have matched, which is what makes the negative result evidence.**

---

## 14. RunLockClaim (CHANGED)

**What it is today**, and why it is the FR-013 defect verbatim: `run/identity_lock.py` writes one
`{run_id}.lock.json` per run under `DEFAULT_LOCK_DIR` (`/tmp/civsim_harness/run_locks/`), recording
`run_id`, `client_pid` and `acquired_at`. `ActiveRunLock.client_pid` is the **Civilization VI
client** process, and both `_active_locks()` (which `acquire` uses, and which clears what it judges
stale) and the read-only `inspect()` (used by `run/orphans.py` for diagnosis) compute staleness as
`psutil.pid_exists(client_pid)`, surfaced on `LockInspection` as `pid_alive` and consumed by
`holds`. The client outlives the run. So a lock left behind by a run that has already **finished**
has a live PID, never reads stale, and silently blocks every later run.

`acquired_at` is present and is **not** used for staleness, which is correct and must stay that way:
FR-013 excludes age outright.

That single field is two of this project's three named defect shapes at once:

- **A value whose name asserts something it does not mean.** `pid_alive` is read at the call site as
  "the owner is still there". It means "some process with this id exists".
- **A guard whose scope does not match the scope of the thing it guards.** The lock guards a *run*.
  Its evidence is about a *client process*. Those have different lifetimes, and the gap is the leak.

| Field | Status | Notes |
|---|---|---|
| `run_id` | kept | The owner |
| `client_pid` | **demoted** | Retained as recorded provenance — genuinely useful for diagnosis — but **no longer the reclaim signal** |
| `pid_alive` | **removed from the decision** | Kept as a diagnostic reading under a name that says what it is |
| `owner_run_alive` | **NEW** | The evidence by which the **run's** liveness was determined: its lifecycle state in the store, and its liveness stream still growing |
| `age` / `mtime` | **never an attribute that may justify reclaiming** | FR-013 |

### Validation

- **Reclaim only on positive evidence that the owning run is over.** Never on age, mtime, or any
  timeout, and never on the liveness of a process the lock merely names.
- **A lock whose owner is live is NEVER force-taken.** The run waits or stops rather than
  interleaving into another run's client — two runs sharing one client produce two runs whose
  recorded starting conditions look correct and whose actual play is entangled, which nothing else
  in the record would reveal.
- **A lock written in the old shape is `unresolvable`, not stale.** It carries no run-liveness
  evidence, so it reports and defers to the operator (FR-017). Treating an un-evidenced lock as
  reclaimable would reintroduce the exact failure inside the migration.

---

## 15. FailureFixture (NEW)

A reproduction of a real observed incident **paired with** its negative control. **The pair is one
entity because neither half is acceptable alone.** The worked instance in the project record is spec
002's T283 — the `debug_overlay` corner heuristic, which has a positive control and no negative one,
so nobody can demonstrate it passes ordinary gameplay.

| Field | Type | Notes |
|---|---|---|
| `fixture_id` | string | |
| `incident_ref` | string | The observed incident it reproduces |
| `seam` | string | Where the injection enters — **must be a seam the real failure actually crosses** |
| `fires_detector` | ref | |
| `negative_control` | **required** | A healthy run wearing the same external symptom |
| `broken_build_demo` | required | The demonstration that the check goes red against a deliberately broken build (FR-057) |
| `tier` | enum | `integration` \| `live` |

### The minimum set

| Fixture | Negative control | Tier |
|---|---|---|
| Stranded full-screen diplomacy view | a healthy turn showing a registered, agent-answered view | live |
| `has_blocking_prompt` permanently true while the engine permits end turn | belief and engine **agree** — the detector must fire **zero** times regardless of elapsed time | integration |
| **Class 4**: the screen probe answering `recognized=true, has_blocking_prompt=false` with a full-screen modal up (`EndGameMenu`) | a genuinely clear board answering the same way — the classifier must reach `live` there, and must **not** reach `live` on the class-4 input | integration |
| A detector asked *"show me an input on which you return unknown"* (FR-060) | — this **is** the control; a detector that cannot produce one is an allowlist read as a detector | contract |
| Tuner unreachable: lock held by a **live** run | lock left by a **finished** run — the two must be distinguished, not merged | integration |
| Tuner unreachable: connection inside the ~2 s close tail | a connection outside it | integration |
| Client death arriving as a bare transport error (real TCP reset) | a clean EOF, which is already handled | integration |
| Turn-advance denial (engine 56 → 57, record says none) | **a correct system a naive check calls diverged** — the same-command readback. The check must return `unverified`, never `diverged` | integration |
| Three-artifact goal-run disagreement | three artifacts that agree | integration |
| A legitimately long model call, longer than the run's bound | — this **is** a control; it is in the set so SC-001 is exercised rather than vacuously satisfied | integration |
| A silent phase (emission removed) | the same phase emitting | contract |

---

## 16. StallAccounting (NEW)

Makes SC-014 answerable. Today the project records per-incident costs — a defeat recovery at ~17
minutes of human intervention, $0.464 on one hung driver, a paid live run lost 90 seconds to machine
contention — and **no aggregate at all**.

| Field | Notes |
|---|---|
| `wallclock_lost_ms` | Attributed to the stall, **separately from time a turn legitimately spent thinking** |
| `model_spend_discarded` | A paid call whose result a reload or replay threw away, attributable to the stall that caused it |
| `initiated_by` | A human-initiated recovery is excluded from detector-latency statistics — otherwise the headline metric improves every time a human rescues the run |

Rolled up per run and **summable across runs**, without reading logs.

---

## Changes to existing entities

### Run (CHANGED)

- **`record_completeness_status` widens.** Today it means "no turn or step gaps". After this feature
  it must **also** reflect an unresolved `DivergenceIncident`, so Principle III's bar applies to a
  run that is *complete but internally inconsistent* (FR-024). This is called out rather than done
  quietly because a value whose meaning silently widens is one of the defects this feature exists to
  design against.
- **Rung availability is recorded at preparation** (FR-039, SC-022), so the record says which rungs
  this host had and why — including that rung 5 had none.
- **A run never comes to rest asserting it is still progressing** (FR-044). A run left in `playing`
  after its client died is a lie the record tells. `paused` is acceptable **only** with a recorded
  reason.

### RunEvent (CHANGED)

New kinds, additive within the existing schema version — no migration is triggered:

`liveness_silence_observed` · `stall_classified` · `recovery_attempted` · `recovery_skipped` ·
`recovery_succeeded` · `recovery_failed` · `divergence_detected` · `human_override`.

**The first of those is named for silence, not for a stall, and that is deliberate.**
`RunEventType` in `models/records.py` records in its own docstring that a `stall` member was
**removed** because *"it denoted a turn exceeding its time budget, and there is no time budget any
more"*. Reintroducing a member called `stall` for the candidate would look, to any future reader, as
though the time budget had come back — and the whole point of this feature is that the bound
measures **silence, not duration**. So the candidate event is named for the thing it directly
observes (FR-002), `stall_classified` carries a verdict rather than a duration, and the removed
member stays removed.

The four existing detection members — `crash_detected`, `hang_detected`, `unresponsive_detected`,
`unknown_screen` — are **kept unchanged and keep their meaning**. They remain records of *which
probe noticed*, which is what they have always been; what changes is that they become inputs to
`stall_classified` rather than the system's final answer.

Every one of the new kinds is persisted **before the next action is taken** (FR-040, Principle III).

### Lifecycle and stop resolution (CHANGED — in obligations, not in shape)

`LifecycleState` (`models/run.py`) already has nine members: `PREPARING`, `PLAYING`,
`WAITING_ON_MODEL`, `WAITING_ON_GAME`, `PAUSED`, `INTERRUPTED`, `RESUMING`, `FINISHED`, `FAILED`.
Rung 4 already drives `PLAYING → INTERRUPTED → RESUMING → PLAYING` through
`resilience/recovery.py::RecoveryEngine.recover`, so **this feature adds no state**.

Two consequences worth stating, because both are places a name could be read as more than it means:

- **`WAITING_ON_MODEL` and `WAITING_ON_GAME` are harness beliefs, not signals.** They say what the
  harness thinks it is doing. They are exactly the "active is not emitting" case (FR-001), and they
  MUST NOT be registered as `ProgressSignal`s or read as evidence of progress. They *are* useful for
  the spec's "nothing is wrong and nothing is happening" edge case: together with `PAUSED` they let
  the disposition distinguish *not progressing* from *not supposed to be progressing*.
- **`StopResolution` has five members** — `TURN_REACHED`, `VICTORY`, `DEFEAT`, `OPERATOR_STOP`,
  `UNRECOVERABLE_FAILURE` — set exactly once and only in a terminal state. **None of them names a
  watchdog outcome**, so FR-037's "recorded failed state naming its last-known good save and the
  highest rung reached" resolves to `UNRECOVERABLE_FAILURE` **plus** a recovery detail carrying those
  two facts. Adding a sixth member is deliberately *not* proposed: the resolution says why the run
  stopped, and "unrecoverable failure" is exactly why. What was missing was never the member — it
  was the detail, and FR-044's rule is that a run must not come to rest without a recorded reason.

---

## Cross-cutting invariants

| # | Invariant | Enforced by |
|---|---|---|
| **I1** | Absence of **completion** is not evidence of a stall; absence of an affirmative **signal** is | `StallCandidate` opens on silence only |
| **I2** | Active is not emitting — a signal is published by the waiting phase itself, never inferred from a live process, thread, or open socket | `ProgressSignal.derivation`; only `work_derived` clears a bound |
| **I3** | `unavailable` never reads as `absent` | `SignalObservation.status` |
| **I4** | Exactly one disposition, before anything acts | `StallClassification` |
| **I5** | `undetermined` is a correct outcome; only rung 0 is eligible under it | Classifier + ladder |
| **I6** | No rung fires without a positively observed precondition | `RecoveryAttempt` cannot be constructed without one |
| **I7** | No outcome is taken from the return value of the call that produced it | `IndependentRead` |
| **I8** | A non-independent read yields `unverified`, never `diverged` | `IndependentRead.independence` |
| **I9** | No existing record is amended; corrections are additive | `DivergenceIncident`; 003's no-delete floor |
| **I10** | A lock is reclaimed only on evidence that its **run** is over | `RunLockClaim.owner_run_alive` |
| **I11** | Escalation is monotonic; the ladder never cycles | Escalation state machine |
| **I12** | The harness never escalates toward a rung this host lacks | `RecoveryRung.available_on_host`, resolved at preparation |
| **I13** | A probe's own failure is never the game's answer | `blocked_by_game` requires an independent read that **returned** |
| **I14** | A missing engine method means *unknown*, never *no* | Detected at preparation (FR-053) |
| **I15** | No detector or rung is constructible into an inert state | Required inputs, no defaults (FR-059) |
| **I16** | Every detector has a negative control; for reconciliation, a *correct system a naive check calls diverged* | `FailureFixture` pair |
| **I17** | Nothing this feature reads or records reaches the playing agent | FR-048; audited per release (SC-020) |
| **I18** | The watchdog's remit is **harness game runs**, never arbitrary commands | The monitor is constructed from a `run_id` and can only observe and act on that run |
| **I19** | A `live` disposition never rests only on state produced by the component whose health it asserts | `StallClassification` validation; proven against the class-4 reproduction |
| **I20** | Every detector, probe and check can return "I do not know" | FR-060's per-release enumeration; a check that cannot is an allowlist read as a detector |
| **I21** | **Absence and unobservability never share a representation, anywhere** | The single rule behind I3 (`unavailable` ≠ `absent`), I8 (`unverified` ≠ `diverged`) and I20 (*I cannot tell* ≠ *nothing is wrong*) |
