# Phase 0 Research: Harness Liveness, Reconciliation, and Autonomous Recovery

**Feature**: `004-liveness-reconciliation-recovery` | **Date**: 2026-09-22 |
**Spec**: [spec.md](./spec.md)

This feature is unusual among the five deliverables in that **most of its unknowns were already
settled by measurement before planning began**, in the spec's own evidence base and in nine
clarifications. What Phase 0 had to do was therefore not "choose a design space" but answer four
questions the spec deliberately left to the plan:

1. **What does a phase have to do to "emit", such that the emission is evidence about the work and
   not about the emitter?** (R2 — the sharpest finding here, and it contradicts the obvious
   implementation, including the one currently in flight.)
2. **Where does a liveness signal go, and who reads it**, given one tuner connection, a
   no-delete/no-edit store, and FR-008's requirement that a dead monitor be distinguishable from a
   quiet run? (R3, R4)
3. **How do five dispositions get derived from the four outcomes the existing detector already
   produces**, without rebuilding it? (R5)
4. **What, exactly, makes a detector "proven"** on a project whose characteristic defect is a
   well-built mechanism that is never reached — and how does that differ from the two rosters spec
   002 already ships? (R11)

**One question this research did not have to answer, and says so deliberately**: how long the
silence bound should be. The owner ruled three minutes (spec Clarifications, 2026-09-22), and the
ruling changed the *mechanism* rather than the number — the bound measures silence, not duration.
R12 records every number in this feature against the measurement it came from, and the most
important row in that table is the one that reports **the number was removed rather than measured**.

---

## R1 — The emission precondition: what is silent today, what has just landed, and what the
enumeration obligation actually costs

**Decision**: Implementation **Phase 1 is emission**, and no rung and no silence bound may be
enabled before it completes. The enumeration required by FR-003 and SC-026 ships as a **rostered
data file with per-entry prose**, in the same shape as spec 002's `test_gate_inputs.py` roster, not
as a prose list in a document.

**Why it is a precondition rather than a companion task.** The spec states this three times and the
code agrees. At the time the spec was written:

| Phase | Typical duration | Bound | Emitted anything a reader outside the process could see? |
|---|---|---|---|
| Model-provider call | ~146 s measured; ~85% of a turn's wall clock; ~7.8 per turn | `openrouter.DEFAULT_REQUEST_TIMEOUT_S` = 120 s **per attempt**, and `chain.ProviderChain` may legitimately spend several in a row | **No.** No module in `src/civsim_harness/provider/` imported `logging` or `telemetry` at all |
| End-turn confirm poll | 47 s over 14 polls in the measured case; an end turn is confirmed only once every AI player has taken theirs | 200.0 s at both production call sites (`run/decision_loop.py::END_TURN_CONFIRM_TIMEOUT_S`, `run/turn_cycle.py::BACKSTOP_CONFIRM_TIMEOUT_S`) | **No.** The `ConfirmWindow` built every attempt was published only on the *final* verification result — after the wait is over |

Both bounds **exceed** the three-minute ceiling on their own. A watchdog enabled ahead of the
emissions would not be an incomplete watchdog; its first two kills would be the two healthiest long
operations in the system, during precisely the slow-regime turn the 200 s bound exists to
accommodate.

**The enumeration obligation is the expensive half, and it is easy to under-price.** Both silent
phases found so far were found *by searching their loops for a logging call and finding none* — not
by anyone noticing. SC-026 requires that every phase which can outlast the ceiling be enumerated and
audited, per release. That is a standing roster, not a one-time sweep, and a phase absent from it is
a phase nobody checked. The candidate set to audit in Phase 1 is wider than the two known entries:
save and load round trips, capture, the tuner heartbeat's own bounded wait, `ProviderChain`
fallback across attempts, the orphan sweep that runs on a write-mode store open (003 rule W6), and
any `time.sleep` or `while` in `run/`, `act/`, `saves/` and `nexus/`.

**Alternatives rejected.**

- *Widen the ceiling until the silent phases fit.* Rejected on the spec's own rule and the project's
  third named defect pattern: a threshold chosen without measuring what it bounds. It also inverts
  the design — a ceiling wide enough for a 200 s confirm plus a chain of 120 s provider attempts is
  wide enough to sleep through every incident this feature exists to catch.
- *Exempt the known-long phases from the bound.* Rejected because an exemption list is a silence
  allowlist: the first time a phase on it genuinely wedges, the watchdog is contractually blind to
  it. FR-003 is explicit that silence by construction is a defect **in the phase**.
- *A prose enumeration in this document.* Rejected because it cannot be re-run per release, which is
  exactly what SC-026 asks for.

---

## R2 — A ticker thread beside a blocking call is not an affirmative liveness signal

**This is the finding this research exists to produce, and it invalidates the obvious
implementation.**

**Decision**: liveness signals are split into two kinds, and the distinction is carried in the
registry (FR-002) and enforced at classification time (FR-001):

| Kind | Definition | What its presence establishes | May it clear the silence bound for the phase it describes? |
|---|---|---|---|
| **Work-derived** | The emission is a *byproduct of the work itself* — it cannot be produced unless the work made progress | The work is progressing | **Yes** |
| **Observer-derived** | The emission is produced by something *beside* the work — a timer, a ticker thread, a supervisor clock | The harness process and its emitter are alive | **No.** It may bound the gap between records; it may not be read as evidence the phase is progressing |

**Why this matters more than it sounds.** FR-001's third clause says the signal "MUST NOT be
inferred from the process still existing, from a thread being alive, from a socket being open".
A ticker thread emitting `provider.call.waiting` every 15 s beside a blocking `httpx.Client.post`
is a *thread being alive*. The record it publishes is true and useful — it says 45 seconds have
elapsed on a call to this model with a 120 s bound — but **it is not a statement about the call**.
Put the exact failure the spec lists as an edge case against it — *"a model call that hangs with the
socket open and no bytes arriving"* — and the ticker ticks happily forever while the watchdog reads
a healthy run.

That is the same indistinguishability this feature exists to remove, moved up one level and made
harder to see, because now there *is* a signal and it looks affirmative.

It is also, precisely, this project's second named defect: **a value whose name asserts something it
does not mean.** `provider.call.waiting` asserts the call is waiting. It is emitted whether the call
is waiting, hung, or the socket is dead.

**Decision, per phase.** The fix is not to measure a better interval; it is to source the signal
from the work, which removes the question:

- **The end-turn confirm loop is already work-derived and needs nothing further.** Each iteration
  completes a real tuner round trip and gets an answer back; an iteration that publishes therefore
  *proves* the tuner answered within the last poll interval. `act/liveness.py::emit_confirm_liveness`
  is a correct FR-003 signal as landed.
- **The provider wait is observer-derived and must be supplemented before FR-005 may act on it.**
  The work-derived signal available here is the one FR-003 names outright: *provider bytes actually
  arriving on an in-flight call*. Obtaining it means requesting the completion as a **stream** and
  emitting on chunk arrival (cumulative bytes, chunk count, time since last chunk), then
  accumulating and parsing exactly as today. The provider port's contract — **exactly one decision
  per call** (spec 002 FR-037–FR-043) — is untouched: streaming changes how the bytes arrive, not
  what the call returns.
- **Both are kept.** The ticker is not deleted. It bounds the gap between records during connect,
  TLS, and the pre-first-byte wait, where there is genuinely no work-derived signal to have — and
  during that window the honest reading is *observer-derived only*, which under FR-015 pushes toward
  **undetermined**, never toward a kill.

**Alternatives considered.**

- *Keep the ticker alone and call FR-003 satisfied.* Rejected: it satisfies FR-003's periodicity
  clause and fails FR-001's "active is not emitting" clause. It would pass review — which is exactly
  the reviewer-level failure FR-003's two sub-clauses were written to catch, arriving through the
  clause nobody re-read.
- *Poll the socket for received-byte counts without streaming.* Rejected: `httpx`'s blocking `post`
  gives the caller no accessor for bytes-so-far, so this means reaching under the client into the
  transport — a bespoke, version-fragile path for a signal that streaming provides as a first-class
  API.
- *Shorten the provider timeout so silence is bounded by the call.* Rejected outright. It is the
  timeout the owner rejected, wearing a fix, and it trades a detector for a kill switch.
- *Lower the tick interval.* Rejected as the archetype of the mistake: it makes a signal that cannot
  observe the work observe it more often.

**Cost, stated honestly**: switching the decision-path call to streaming is a real change to the
most parity-sensitive code path in the harness and is why this is called out as an owner decision
rather than an implementation detail. It is also the change that makes SC-002 mean something in the
85% case.

---

## R3 — Where a liveness signal is published

**Decision**: liveness signals are **structured records on the `civsim_harness` logger**, which
`telemetry/logging.py` forces through the redacting filter and formatter, landing on the handler
`operator/cli.py` configures — i.e. the **driver log a watchdog polls**. They are additionally
mirrored to a per-run append-only NDJSON **liveness stream** at a path derived from the run id, so
that a reader outside the process (the operator surface, deliverable 1, or a human) can observe
growth without parsing an unstructured log or holding the tuner channel.

**Rationale, by elimination:**

| Candidate transport | Rejected because |
|---|---|
| **The match store** | A tick every 15 s for the length of a run is write amplification against a store whose no-delete/no-edit floor makes every row permanent, and the store's write path is *itself* one of the things that can wedge. A signal that requires the suspect component to be healthy cannot testify about it. Detections, classifications, rung attempts and divergence incidents **do** go to the store (FR-040) — those are events, not ticks |
| **The tuner connection** | There is exactly one, and FR-050 forbids a probe from consuming the run's only channel |
| **An in-memory object only** | Invisible to an out-of-process reader, so FR-008's dead-monitor case and SC-027's "observed growing while the phase runs" are both unverifiable |
| **A mutable "last heartbeat" file** | Not a stream. `st_mtime` on a rewritten file cannot distinguish "still ticking" from "one tick, then a clock that moved"; an append-only stream's **monotonic byte growth** is directly observable and is what SC-027 asks for |

**Why both, and not just the log.** The log is what the landed emitters already use and what a
human tails; the NDJSON stream is what makes "verified empirically to stream" a mechanical check
rather than an eyeball one. The stream is **derived from the same `log_event` call**, not a second
emit site, so the two cannot drift.

**Principle I note carried from the landed modules and made a rule**: a liveness record carries the
*shape* of the question and never its answer — declaration id and the static catalog predicate
expression, never predicate bindings or a `last_read` quotation; provider and model name, never
prompt or response content. These are harness diagnostics under FR-048 and have no path into an
`Observation`.

---

## R4 — Where the watchdog lives, and how a dead monitor is distinguished from a quiet run

**Decision**: a **two-part monitor**. An in-process `LivenessMonitor` on the existing ~10-second
detection cadence does the interrogation, classification and rung selection; it publishes **its own
work-derived heartbeat into the same liveness stream** on every pass, carrying the pass number and
what it read. An out-of-process reader — the operator surface and deliverable 1 — establishes
FR-008 by observing that the monitor's own records stopped while the run's did not, or that both
stopped.

**Rationale.** The monitor has to *act* (dismiss a view, re-derive a belief, reload a save) and
every one of those goes through harness code holding the tuner connection, so it cannot sensibly
live outside the process. But an in-process monitor cannot report its own death, which is exactly
what FR-008 forbids relying on. Making the monitor's liveness a work-derived record in the same
stream costs nothing and makes "the monitor stopped observing" a *readable absence* rather than an
unobservable one.

**The monitor's own heartbeat must be work-derived too**, by R2's rule: it is emitted at the end of
a pass that actually completed its reads, carrying what those reads returned. A monitor that emitted
on a timer would be able to report health while its reads were wedged.

**FR-052 — detection must not cause the faults it looks for.** Machine contention has already
produced `Errno 111` against a live, listening client on this box. So: the monitor's passes are
bounded individually (as the existing detection already is), it records its own load per pass, and a
classification that cannot rule the monitor out as the cause is **undetermined** under FR-015. The
cheap signals — reading the liveness stream, reading harness state — are free of the tuner
entirely; only the engine probe of FR-010 costs a round trip, and it is taken at most once per
classification, not once per pass.

---

## R5 — Classification: five dispositions on top of four existing detector outcomes

**Decision**: do not rebuild detection. The existing layer stays as the **signal source**, and this
feature adds a `StallClassifier` above it that consumes those signals plus one independent engine
read and produces exactly one of five dispositions.

What is already wired, verified against the tree rather than taken from the spec:

| Component | Where | What it supplies |
|---|---|---|
| Process liveness | `resilience/liveness.py` — `is_process_alive`, `ProcessLivenessMonitor.check()` | Point-in-time `psutil` PID check |
| Tuner heartbeat | `resilience/heartbeat_monitor.py` — `HeartbeatMonitor.check() -> OK \| HANG \| CONNECTION_LOST`, over `nexus/heartbeat.py::probe_heartbeat` | Hang, deadlock, dropped connection |
| Per-operation bounds | `resilience/operation_bounds.py` — `run_bounded`, `OperationKind`, `OperationTimedOut` | An operation that never returns |
| Screen identity | the declared screen-identity observation | `unknown_screen` |
| Aggregation | `resilience/detector.py` — `DetectionAggregator.check_once(...)`; one pass, no loop or sleep of its own. `check_heartbeat` corroborates a connection loss with one re-probe after `DEFAULT_RECONNECT_REPROBE_DELAY_S = 2.0` | The four outcomes |
| Production wiring | `run/detection.py` — `DetectionWatch`, `run_under_detection(...)`; called from `run/turn_cycle.py` around the decision loop and between turns. `DEFAULT_DETECTION_INTERVAL_S = 10.0`, `DEFAULT_DETECTION_PASS_BOUND_S = 45.0`, `DEFAULT_SUSTAINED_HEARTBEAT_FAILURES = 2`, `RECOVERABLE_DETECTIONS` | The cadence the monitor rides |

**The monitor rides the existing cadence rather than adding one.** A second polling loop would be a
second source of contention against the thing FR-052 says detection must not cause, and there is one
tuner connection for both to share.

**The gap is precisely stated by the spec and worth restating as a design constraint**: the existing
classification sorts faults by *which probe noticed* (`crash_detected`, `hang_detected`,
`unresponsive_detected`, `unknown_screen`), not by *what is actually wrong*. Those four are an
excellent input and a useless output, because all three of the observed classes can present as
`hang_detected` or `unknown_screen`.

| Existing outcome | Contributes evidence toward | Never sufficient alone for |
|---|---|---|
| `crash_detected` (process gone) | *unreachable* / cause = client process absent | anything else |
| `hang_detected` (heartbeat nonce fails to round-trip) | *unreachable* (probe failure, FR-051) | *blocked by the game* — a probe failure is never the game's answer |
| `unresponsive_detected` (sustained eaten passes) | *unreachable*, cause TBD by FR-012 | *blocked by the harness* |
| `unknown_screen` | *blocked by the game* **only if** an independent read confirms a view is held | *blocked by the harness*; an unregistered view stalls visibly under FR-018 |
| *(new)* harness belief vs engine answer disagree | *blocked by the harness* | *blocked by the game* |
| *(new)* silence on the liveness stream past the derived bound | makes a candidate; contributes to none of the five by itself | every disposition — silence triggers classification, it does not conclude it |

**The load-bearing addition is the last-but-one row**, and it is the one no existing signal supplies:
FR-010 requires at least one observation obtained **independently of the component under suspicion**,
and a liveness check that consults only harness state structurally cannot see the case where the
harness is the problem. Concretely, for the class-2 fixture: the harness's `has_blocking_prompt`
belief is compared against the engine's own end-turn availability answer, read in a separate tuner
command (R6).

**`Stall Candidate` and `Stall Classification` are separate entities on purpose** (spec Key
Entities). The thing that *triggers* classification (silence past the bound) must not be the thing
that *concludes* it, or elapsed silence leaks back into the verdict and FR-001's second clause
becomes a timer again.

---

## R6 — An independent re-read, and the same-command readback that fabricates divergence

**Decision**: introduce an `IndependentRead` value that **cannot be constructed from the same tuner
command as the write it verifies**. It carries the command sequence number of the write and of the
read, and construction fails unless `read_seq > write_seq`. Every FR-025 verification, and every
FR-019/FR-020 reconciliation comparison, consumes an `IndependentRead` — nothing else is accepted.

**Why this is structural rather than a convention.** This project has already paid for the
alternative twice, in opposite directions:

- A readback issued in the same tuner command as the write returns the **pre-call value**. It
  produced a finding — "the engine's close call returns success and does nothing" — that the
  project's own ledger later **retracted**. The call was working; the check lied.
- The same artifact is live and open today as spec 002's **T293**: `turn.end_turn` dispatched
  correctly, the harness re-read the turn number before the asynchronous advance landed, and **three
  turn cycles recorded `game_turn_advanced: False` while the engine went 56 → 57**. That is the
  spec's own divergence instance 2, and its cause is an inline readback.

So the most likely first bug in this feature is a reconciliation detector that **reports healthy
runs as broken** — the outcome the owner said must not happen, arriving through the detector instead
of through a timeout. FR-055 anticipates it by requiring, for every reconciliation check, a negative
control that is a *correct system a naive version of the check would call diverged*. That control is
concrete and buildable today: replay the retracted close-call measurement and the T293 turn-advance
readback, and assert the check reports **`unverified`** — never `diverged`.

**The three-outcome rule that falls out of this, and it is not the obvious two**: a reconciliation
comparison returns `agreed`, `diverged`, or **`unverified`**. A comparison whose read was not
independent is `unverified`. Collapsing `unverified` into `diverged` manufactures incidents on
healthy runs; collapsing it into `agreed` hides real ones. It is the same asymmetry FR-006 draws
between "no signal was emitted" and "the watchdog could not read one", applied to reads.

**Ordering, not just command separation.** FR-025 says independent in *command and in ordering*.
Some engine effects are asynchronous — the turn advance is the measured example — so a read that is
in a later command but issued before the effect lands is still not a verification. The rule the
design adopts: a verification read is retried under its own bound until it changes or the bound
expires, and a bound that expires yields `unverified`, not `diverged`.

---

## R7 — The blocking-view registry, and the dismissal capability that does not exist yet

**Decision**: `catalogs/` gains a **blocking-view registry** as versioned data, alongside the
observation and action catalogs, with one entry per known view carrying the five fields FR-018
requires. The dismissal itself is a **declared action with a parity basis**, not a new control path.

**What is actually missing.** Synthetic input exists — `HostPlatform.send_input` with per-host
adapters — but it is reachable from only two hard-wired call sites: completing a popup
acknowledgement, and pressing Escape to clear the post-load leader-intro screen. There is **no
declared action that sends a dismissal to an arbitrary stuck view**. So FR-030's rung 1 depends on
one being declared.

**Principle II handling.** The declaration is `path: bespoke` with a `firetuner_gap` recorded
verbatim, exactly as `prompts.orders` now is — and spec 002 already ships the check that enforces
it: `tests/contract/test_synthetic_input_declaration.py` re-derives every in-harness `send_input`
call site from the source with `ast` and asserts the capability it serves is `path: bespoke` with a
non-empty gap. A new dismissal call site is therefore **caught automatically** if it ships
undeclared. This feature adds nothing to that mechanism; it inherits it.

**Reachability per host is a registry field, not a runtime discovery.** Two measured cases force
this: the post-defeat exit-confirm modal **ignores synthetic input**, and a Wayland session blocks
synthetic input by design. Both are preconditions to be observed at preparation under FR-039, not
limitations to be discovered mid-stall — a rung attempted and retried against an unsatisfiable
precondition burns the whole attempt limit and converts a recoverable stall into a failed run, which
is FR-033's recorded observation.

**Verification is a re-read, never the call's answer.** The registry's fifth field is the
independent read that confirms the view closed, and it is an `IndependentRead` under R6. This is not
theoretical here: the popup dismissal path exists *because* the engine's own click callback was
measured to report success and do nothing.

---

## R8 — Run-lock ownership: the signal must be named for the run, not for a process

**Decision**: `RunIdentityLock` gains a **run-liveness** determination that is distinct from, and
replaces, its present process-liveness determination for the purpose of reclaiming. Age, mtime and
timeouts remain excluded, by FR-013.

**The present code is the defect, verbatim.** `run/identity_lock.py` records `client_pid` — the
**Civilization VI client** process — and computes staleness as `psutil.pid_exists(client_pid)`,
surfaced on `LockInspection` as the field `pid_alive` and consumed by `holds`. The client outlives
the run. So a lock left behind by a run that has already **finished** has a live PID, never reads
stale, and silently blocks every later run — which is exactly the observed incident.

It is simultaneously two of the three named defect shapes this plan must design against:

- **A value whose name asserts something it does not mean.** `pid_alive` is read at the call site as
  "the owner is still there". It means "some process with this id exists".
- **A guard whose scope does not match the scope of the thing it guards.** The lock guards a *run*.
  Its evidence is about a *client process*. Those two have different lifetimes, and the gap between
  them is the leak.

**What replaces it.** The lock records the owning **run id** and the evidence by which that run's
liveness was determined — the run's own lifecycle state in the store, and its liveness stream still
growing. A lock is reclaimable only on positive evidence that its **run** is over; a lock whose
owner is live is **never** force-taken, and the run waits or stops rather than interleaving into
another run's client (FR-032, SC-012). `client_pid` is retained as recorded provenance — it is
genuinely useful for diagnosis — but it stops being the reclaim signal, and the field that drives
the decision is named for the run.

**Compatibility.** A lock file written by the old shape carries no run-liveness evidence. It is
therefore **unresolvable**, not stale: it does not reclaim, it reports, and it is the operator's
call under FR-017. Treating an un-evidenced lock as reclaimable would reintroduce the failure in the
migration.

---

## R9 — The ladder: ordering by destructiveness, monotonic escalation, and the rung that cannot exist

**Decision**: six rungs, 0–5, each a declared object carrying the five fields FR-027 requires
(disposition-and-cause answered, observable precondition, cost and what it destroys, independent
verification, host availability). Escalation is monotonic within one stall; the ladder never cycles.

| Rung | Answers | Precondition (must be *observed*) | Destroys | Status |
|---|---|---|---|---|
| **0** Observe and report | anything, incl. *undetermined* | none | nothing | New |
| **1** Dismiss a registered blocking view as a human would | *blocked by the game* | registry entry matched **and** its dismissal reachable on this host | nothing | New; depends on R7's declared action |
| **2** Re-derive the harness's belief from the engine | *blocked by the harness* | belief and engine answer disagree, read independently (R6) | the stale belief only | New |
| **3** Re-establish the path to the game | *unreachable*, selected by FR-012 cause | the specific cause is established | nothing (a lock is never force-taken from a live owner) | Partly exists — the ~2 s post-close refusal-tail re-probe already ships |
| **4** Reload the run's last named save and replay the turn | *unreachable* or an unresolved *blocked by the game* | the game is in a context that **accepts a load** | the in-progress turn attempt, retained as abandoned; discarded model spend attributed to the stall | **Exists** — `resilience/recovery.py` is this rung today and becomes one rung rather than the whole ladder |
| **5** Restart the game client | *unreachable*, cause = client absent/unresponsive | — | the client process | **UNAVAILABLE. Declared as a dependency, not assumed** |

**Rung 5 is a stated dependency and the plan may not assume it.** Verified independently for this
plan rather than taken from the spec: `HostPlatform` (`host/port.py`) declares nine methods —
`locate_game_process`, `find_game_window`, `capture_window`, `check_capture_preconditions`,
`list_window_titles`, `resolve_game_directories`, `send_input`, `focus_window`, `free_disk_space` —
and a search of `src/civsim_harness/` for any definition matching launch / restart / terminate /
kill / spawn / start-client / stop-client returns **nothing on any platform**. The search could have
matched, which is what makes the negative result evidence. Until the capability exists, the ladder
**stops at rung 4 in a recorded failed state** (FR-034, FR-037) and reports the unavailability at
preparation (FR-039, SC-022), rather than presenting a ladder whose top it cannot climb.

**Rung 4's precondition must be named for what governs it, not for its correlate.** The measured
distinction is **a fresh main menu versus a reused one**: a load from a freshly launched menu works,
and the three refusals on record were all on a menu reached by exiting or losing a game.
"Post-defeat" is a correlate. A precondition named for the correlate fires in the wrong place — and
this is the third instance in this plan of *a guard whose scope does not match the thing it guards*,
which is why it is being called out rather than transcribed.

**Why ordering by destructiveness is the whole safety argument.** The classifier will sometimes be
wrong. If it calls a class-2 block a class-1 block, rung 1 fires, costs a wasted Escape, verifies
nothing changed, and escalates. That is the *intended shape of a misclassification's cost*. The
ordering is what converts a classifier error into a wasted keypress instead of a destroyed run, and
it is the reason FR-016's asymmetry is implementable at all.

---

## R10 — Reconciliation checkpoints, and which artifact is authoritative

**Decision**: reconciliation runs at three checkpoints, and the **match store is the authoritative
artifact** under FR-026.

| Checkpoint | Compares | Catches (measured instance) |
|---|---|---|
| **Per refused action** | the harness's belief that an action is unavailable vs the engine's own availability answer | the `has_blocking_prompt` refusal — detected at the **first** refusal, not the eighth (SC-005) |
| **Per turn boundary** | the turn number the record claims vs the engine's turn number, read independently and retried under its own bound | the turn-advance denial: engine 56 → 57, record says nothing advanced (T293) |
| **Per run end** | the run timeline, the driver's own result output, and the store | the three-artifact goal-run disagreement |

**Why the store is authoritative.** It is the only one of the three with a no-delete/no-edit floor
(deliverable 3 FR-006 and its published `MUTATING_OPERATIONS` surface), the only one Principle III
speaks about, and the only one another deliverable reads. A driver's result file is a convenience
output; a timeline can be reconstructed. Declaring the store authoritative also makes the
disagreement *recordable*, because the record of the disagreement goes to the thing that cannot
rewrite it.

**Automated recovery stops at the stall boundary, and this must survive a future contributor.**
Detection and reporting cover the full divergence class; only a divergence that *stopped play* gets
a rung. Two of the three measured instances never stopped play. The temptation to "finish" the
feature by auto-correcting the record is explicitly forbidden (FR-022, SC-015) — an auto-corrected
record is indistinguishable from a correct one, so the very failure this feature exists to make
visible would become invisible again. Corrections are **additive records naming what they
contradict** (FR-023), reusing spec 002 FR-047's abandoned/authoritative pattern rather than
inventing a second one.

**`record_completeness_status` already exists** on the run (spec 002 data-model §4) for gapped runs.
An unresolved divergence incident must set it, so Principle III's bar — no gapped run feeds trending
— applies to a run that is *complete but internally inconsistent* (FR-024). This is a value that
would otherwise assert something it does not mean: "complete" currently means "no gaps", and after
this feature it must mean "no gaps and no unresolved divergence".

---

## R11 — What makes a detector "proven", and how it differs from the two rosters 002 already ships

**Decision**: extend spec 002's existing rosters rather than build a third mechanism, and add the
one thing neither of them checks.

**What already exists and is reused unchanged:**

- `tests/contract/test_reachability.py` — catches a symbol with **no caller at all**.
- `tests/contract/test_gate_inputs.py` — catches **a safety gate whose evidence is never gathered**:
  `(entry_point, parameter)` pairs rostered as data with per-entry prose, where a literal empty
  (`None`, `()`, `[]`, `{}`, `frozenset()`) counts as *not supplied*, the **outermost** production
  entry point is what is rostered, and `**kwargs` reports as *unprovable*, never as a pass.
- `tests/contract/test_synthetic_input_declaration.py` — `ast`-derives every `send_input` call site.

Research R21 of spec 002 also records, with its measurements, that a **broad syntactic scan for the
empty-default family was built and rejected on evidence** — three formulations, 24/32/43 hits, and
none of them caught either finding that motivated it. That negative result is inherited here
deliberately: **a checker for this feature must key on meaning and on the `src/`-versus-`tests/`
partition, never on a signature.**

**What is new, and it is the important part.** FR-058 asks that every detector and rung be
"reachable from at least one production path". Read literally, `test_reachability.py` already
answers it — and that reading is **too weak for this feature**, because the day's most serious
finding was a mechanism whose production call site *existed* and passed an empty default, so it was
reached and inert. The check this feature needs is therefore:

> **Reached *and able to fire*.** For every registered detector and every rung, a structural check
> asserts that some production call site supplies the inputs that make it capable of firing — the
> `test_gate_inputs.py` rule applied to detectors and rungs — and that its construction refuses a
> missing input outright (FR-059).

FR-059 is the constructor-side half of the same rule and is the direct descendant of the day's
adopted root cause: *an optional parameter with a safe-looking empty default that every unit test
supplies and the single production call site does not.* Detector and rung inputs are therefore
**required, with no defaults**, and a missing input is a startup failure rather than a silent no-op.

**The fixture/control pair is one entity, because neither half is acceptable alone** (spec Key
Entities, `Failure Fixture`). The worked instance in the project record is spec 002's T283: the
`debug_overlay` corner heuristic has **a positive control and no negative control** — every clean
assertion runs against a uniformly flat frame and the one positive fixture is flat background plus
noise in one corner, the best possible case for it — so nobody can currently demonstrate the gate
passes ordinary gameplay. *(One instance is cited rather than a count, because a count is what this
plan would otherwise be asserting without a source.)* The pair requirement is what makes SC-004
non-vacuous.

**The mutation obligation (FR-057) is the check on the checks.** Every acceptance check must be
demonstrated *failing* against a deliberately broken build before it counts. Mechanically this is
the "revert-confirmed" discipline the project already practises by hand — remove the arm, watch the
test go red, restore it — promoted to a per-check requirement with the broken-build demonstration
recorded beside the check. A check that stays green against the broken build is rejected.

**Where fixtures can live.** Reproducing the observed incidents needs a live client for some classes
and a seam injection for others, and the spec is explicit that *an injection at a seam the real
failure never crosses does not satisfy FR-054*. The seams that the real failures actually cross are
known and are all injectable without a client: the transport (`nexus/client.py` — a real TCP reset,
for which `tests/fakes/aborting_tuner.py` already exists and was built for exactly this), the
harness-belief-vs-engine seam, the lock directory, and the confirm loop. The two that genuinely need
a client — a stranded diplomacy view and a real client segfault — are `tests/live/`, marked, and
owned by the live lane.

---

## R12 — Every number in this feature, and the measurement it came from

This table exists because *"what measurement is this number from?"* is a standing review question on
this project, and because three thresholds were set, or nearly set, without measuring what they
bound on the single day before this plan. **The most important rows are the ones that say the number
was removed.**

| Number | Where it binds | Provenance | Status |
|---|---|---|---|
| **180 s** silence ceiling | FR-005, SC-002 | **Owner ruling**, 2026-09-22 clarification. Not a measurement | **RULING.** It is a *backstop that should never bind* once R1 lands — see below |
| **Tick interval, provider** | how often a provider record appears | 15 s as landed, justified against the 180 s budget: twelve consecutive misses before a run looks stuck, ~8 extra lines on a 120 s request | **DERIVED FROM THE BOUND**, which is a ruling. Acceptable because it is a *publication* interval — raising it cannot hide a hang, only coarsen the evidence |
| **Tick interval, confirm loop** | how often a confirm record appears | **None needed.** The loop already polls every 2 s doing real work; the emission rides the existing iteration | **NUMBER REMOVED** — the preferred outcome |
| **Provider byte-arrival signal** | R2's work-derived provider signal | **None needed.** A chunk arriving is the event | **NUMBER REMOVED** — this is the fix that beats a well-measured wait |
| **Agent-response window** (FR-047) | how long the agent gets before the watchdog dismisses | **None needed.** Spec Assumptions bind it to the same affirmative-liveness rule: an emitting agent keeps its turn; a silent one triggers dismissal | **NUMBER REMOVED** |
| **200.0 s** confirm bound | `run/decision_loop.py::END_TURN_CONFIRM_TIMEOUT_S` (normative) and `run/turn_cycle.py::BACKSTOP_CONFIRM_TIMEOUT_S`, which cross-references it as "the same assumption" | Replaced a 45.0 s bound **measured too short** against an advance landing at 142.5–155 s. The replacement clears every observed sample; its **ceiling is unmeasured** and the constant's own comment says so | **ASSUMPTION, inherited from spec 002 and labelled as one there.** Probe that settles it: instrument confirm-to-advance latency from a late-game autoplay save, where the AI turn count — the thing the wait is actually waiting on — is highest. This feature neither changes nor ratifies it |
| **2.0 s** confirm poll interval | `END_TURN_CONFIRM_POLL_S`, `BACKSTOP_CONFIRM_POLL_S` | Existing; now doubles as the confirm loop's emission interval at no cost | Inherited — and it is why the confirm loop needed **no new number** |
| **4.0 s / 1.0 s** ordinary action confirm | `ACTION_CONFIRM_TIMEOUT_S`, `ACTION_CONFIRM_POLL_S` | Existing | Inherited. Well under the ceiling; not a long phase |
| **45.0 s** detection pass bound; **2** sustained heartbeat failures; **2.0 s** reconnect re-probe delay | `run/detection.py`, `resilience/detector.py` | Existing spec 002 configuration; the 2.0 s matches the measured post-close refusal tail | Inherited |
| **3** unproductive replays | `run/decision_loop.py::MAX_UNPRODUCTIVE_REPLAYS` | Existing guard against `RecoveryEngine` resetting its counter on every successful-but-unproductive reload | Inherited — and it is the **existing** answer to FR-035's "a stall that recurs identically must escalate rather than repeat". The ladder's escalation must compose with it, not duplicate it |
| **~2 s** post-close refusal tail | rung 3's cause discrimination | Measured on this build | Measured |
| **~146 s** model wait; **85%** of turn wall clock; **1,017 s of ~1,170 s** over 12 turns; **56–146 s** per-turn range; **2 s** fake-provider turn | the case for the whole feature | Measured, Linux node, 2026-09-21/22 | Measured |
| **120 s** provider per-attempt bound | `openrouter.DEFAULT_REQUEST_TIMEOUT_S` | Inherited configuration | Inherited — note a **chain** of attempts can legitimately exceed 180 s in aggregate, which is why R2's per-attempt work-derived signal matters |
| **~10 s** detection cadence | existing detector pass interval | Inherited from spec 002 R12 | Inherited |
| **60 s** crash budget | spec 002 SC-010 | Inherited. Not in tension with 180 s: a dead client is directly observable, a stall is an inference — and the record says which question it answered (SC-002) | Inherited |
| **~38 s** fresh-menu return | rung 4/5 reasoning | Measured | Measured |
| **30 min** stall-to-resolution | SC-025 | Spec-level envelope: ≤3 min detection + a bounded ladder | Derived from SC-002 + FR-037 |
| **Consecutive-failure bound** | FR-037; `RecoveryEngine.recovery_attempt_limit`, raising `RecoveryLimitReached` into `failed` | `models/config.py::recovery_attempt_limit: int = Field(ge=1)` — **a required config field with no hardcoded default**, threaded through `run/composition.py` | Inherited, and correctly shaped already: it cannot be constructed inert, which is FR-059's rule arrived at independently |
| **20 runs** / **20 long calls** | SC-001, SC-025 | Spec-level sample size, chosen so the criterion is exercised rather than vacuously satisfied | Spec |

**The 180 s ceiling deserves its own paragraph, because the honest thing to say about it is
uncomfortable.** It is a ruling, not a measurement, and this plan does not propose to measure it —
because once every long phase emits, *nothing in a healthy system should ever be decided by it*. The
derived-per-run bound of FR-004 is what actually governs, and the ceiling is the ultimate backstop.
That gives a better check than measuring it would: **instrument how often the ceiling binds.** In a
correct system the answer is zero, and any non-zero count is a finding about a **missing emission**
(a phase absent from the SC-026 roster), not about the number. Release audit records the count.

---

## R13 — Making "how much did freezes cost us" answerable

**Decision**: stall and recovery cost is **attributed at the point of loss** and aggregated by the
store, not reconstructed from logs.

**The present state, stated plainly**: the project records per-incident costs — a defeat recovery at
~17 minutes of human intervention, $0.464 burned on one hung driver, a paid live run lost 90 seconds
into its second leg to machine contention — and **no aggregate at all**. SC-014 is met when the
question is answerable from the record without reading logs, and when the figures are summable
*across* runs.

Two attributions, and they are different facts:

- **Wall-clock** lost to a stall and to its recovery, recorded separately from time a turn
  legitimately spent thinking (FR-042). The separation is load-bearing: without it, the feature's
  own reporting cannot distinguish the thing it fixed from the thing it must never touch.
- **Model spend discarded** by a recovery — a paid call whose result a reload or replay threw away
  (FR-043). Spec 002 already records per-call cost with the model that actually served it; this
  feature adds the attribution of a *discarded* call to the stall that caused it. The abandoned
  attempt is retained under FR-047, so the spend is on a record that still exists.

A human-initiated recovery under FR-017 is labelled as such, so the detector's own latency is never
measured against a human-rescued run — otherwise the feature's headline metric improves every time a
human rescues it, which is backwards.

---

## R14 — Reconciling with the work already in flight on spec 002

**Decision**: **this feature does not re-implement the emission; it adopts it, registers it, and
supplies the half that is missing.** The reconciliation is stated here so the two lanes do not build
it twice or, worse, build two emitters.

**What is in flight right now** (working tree at `0187345`, uncommitted, both modules untracked):

| Artifact | State | This feature's disposition |
|---|---|---|
| `src/civsim_harness/act/liveness.py` — `emit_confirm_liveness` / `emit_harness_liveness`, publishing `act.confirm_execution.waiting` per attempt, wired into `act/verify.py::confirm_execution` | New, untracked | **Adopt as-is.** Work-derived under R2 and a correct FR-003 signal. Register it in the FR-002 signal registry |
| `src/civsim_harness/provider/liveness.py` — `provider_call_liveness` context manager, daemon ticker at `PROVIDER_TICK_INTERVAL_S = 15.0`, publishing `provider.call.started/waiting/finished`, wired into `provider/openrouter.py` | New, untracked | **Adopt, and supplement.** Observer-derived under R2. Register it honestly with `presence_does_not_establish: "that the provider call is progressing"`, and add the work-derived byte-arrival signal before FR-005 may act on provider silence |
| `act/verify.py` — `confirm_execution[ReadT]` at `:257`, whose `while True` at `:296` now calls `emit_confirm_liveness(...)` at `:312` before evaluating each attempt | Modified | Adopt. `timeout_s`, `poll_s` and the fail-closed behaviour are untouched, which is the right shape |
| `provider/chain.py`, `run/decision_loop.py` | Modified | Adopt; these are the wiring |
| `tests/contract/test_long_phase_liveness.py` (referenced by both new modules' docstrings) | **Does not exist in the tree** | **Becomes the SC-026 roster's home**, extended from two entries to the full enumeration |

**Three coordination findings to settle before Phase 1 starts, none of which this plan may fix
unilaterally:**

1. **The work cites task id `T290`, which already means something else.** Spec 002's T290 is
   "Record honestly which success criteria are unobserved"; the highest allocated id is T294. The
   emission work has **no task of its own** in `specs/002-civ-playing-harness/tasks.md`. Whichever
   lane owns it needs a real id, or the claim "T290 landed" is true of a different task and the
   emission work is invisible to every tally.
2. **The modules are uncommitted.** Under the standing rule that a commit is verified in a clean
   detached worktree at the committed hash, nothing about them is verified yet, and this feature's
   Phase 1 gate cannot read as satisfied until they are.
3. **`test_long_phase_liveness.py` does not exist in the tree yet** while two shipped docstrings
   name it. That is a documentation claim ahead of its artifact — small, but it is the shape this
   project keeps removing.

**Where the boundary sits.** Spec 002 owns making its own phases emit — it owns the provider layer,
the turn cycle and the confirm loop. This feature owns **the obligation** (FR-003), **the
enumeration** (SC-026), **the registry** (FR-002), and **the reader** (FR-004). Stated as a rule:
*002 writes the emitters; 004 says which phases must have one, what each signal may be read to mean,
and refuses to enable the bound until the roster is clean.*

---

## Resolved unknowns summary

| Technical Context field | Resolution | Ref |
|---|---|---|
| Language/Version | Python 3.12+ under `uv`; inherited from spec 002 unchanged | 002 R1 |
| Primary Dependencies | No new third-party dependency. `psutil` (already present) for process facts; `httpx` streaming for the work-derived provider signal — a mode change on an existing dependency, not a new one | R2 |
| Storage | The existing `MatchStore` port. Detections, classifications, rung attempts, skips and divergence incidents are `RunEvent`s; **liveness ticks are not** and go to the telemetry log plus a per-run NDJSON stream | R3 |
| Testing | `pytest` in the four existing tiers, plus the fixture/negative-control pair as a first-class artifact and a per-release mutation obligation | R11 |
| Target Platform | Unchanged. Rung availability is per host and reported at preparation; rung 5 unavailable everywhere | R9 |
| Project Type | Single Python project; this feature is new subpackage(s) plus extensions to `resilience/`, `run/` and `catalogs/` | plan.md |
| Performance Goals | Detection ≤180 s of silence, derived tighter per run; stall-to-resolution ≤30 min; detection cheap enough not to be a cause | R4, R12 |
| Constraints | One tuner connection; no record amendment; no rung without an observed precondition; no escalation toward an unavailable rung; the bound measures silence, never duration | R4, R6, R9, R10 |

## Open items carried into implementation

These are scheduled decisions with designed failure modes, not unresolved clarifications.

1. **Streaming the provider call (R2).** Owner-level, because it changes the decision path. Until it
   lands, provider silence yields **undetermined** rather than a stall — correct under FR-015, and
   it means the 85% case is *reported* rather than *acted on*. That is the honest interim state and
   it must be visible in the record, not implicit.
2. **The SC-026 enumeration (R1).** The two known entries are not the set. The audit must run before
   the bound is enabled and per release thereafter; its first run is expected to find phases nobody
   has looked at.
3. **The declared dismissal action (R7).** Blocks rung 1. Needs a catalog declaration with a parity
   basis and a recorded Firetuner gap, plus per-host reachability for the two known
   unreachable cases.
4. **Rung 5's client lifecycle capability (R9).** Blocks nothing in this plan — the ladder is
   designed to stop at rung 4 — but it is the clause of Principle VII that remains
   *unimplementable* until a host adapter grows the capability, and that should not fade from view.
5. **The 200.0 s confirm bound (R12).** Inherited as an ASSUMPTION with a named probe. This feature
   neither uses nor ratifies it; whoever probes it should record the result against this row.
6. **Spec 002 T293 (R6).** The same-command readback is open and is the live cause of the spec's
   divergence instance 2. This feature's reconciliation check must be built against it as a fixture,
   and will keep reporting it until T293 lands — which is the correct behaviour, not a conflict.
