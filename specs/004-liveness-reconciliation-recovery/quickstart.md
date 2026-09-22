# Quickstart & Validation Guide: Harness Liveness, Reconciliation, and Autonomous Recovery

**Feature**: `004-liveness-reconciliation-recovery` | **Date**: 2026-09-22
**Plan**: [plan.md](./plan.md) | **Data model**: [data-model.md](./data-model.md) |
**Contracts**: [contracts/](./contracts/)

This guide is how someone proves the feature works — and, just as importantly, how they prove each
check **can fail**. On this project a green suite has repeatedly meant "the mechanism was never
reached", so every scenario below pairs a positive with a control and a deliberate break.

**Read the gate before running anything**: *scenarios 2 onward are not meaningful until scenario 1
passes.* A watchdog validated against silent phases validates a timer.

---

## Prerequisites

- The harness installed and its suite green at a committed hash, verified in a clean detached
  worktree (the project's standing rule; a shared working tree with concurrent lane edits reads
  half-saved files).
- A match store to write to.
- **For the two live scenarios only**: a Civilization VI client with the tuner enabled, and the
  live lane's ownership of it. Everything else runs without a client.

**Placeholders, deliberately.** Numbers that a release prints — catalog version, declaration counts,
roster size — are written as `<placeholder>` here rather than pinned. This project has had pinned
sample numbers drift twice and be read as measurements the second time.

---

## Setup

```bash
# The only accepted suite form on this project. Redirect and poll the log — do NOT pipe to tail:
# `-q` plus `| tail -N` makes a correct 225-270 s suite completely silent, which looks identical
# to a wedged process while working perfectly. A growing log IS the liveness signal.
timeout 900 uv run pytest -q -p no:cacheprovider -o faulthandler_timeout=120 \
  > /tmp/suite.log 2>&1

# Check no run lock is held before anything touches a client:
civsim doctor
```

---

## Scenario 0 (GATE) — Every long phase emits, and the emission streams

**Proves**: FR-003, SC-026, SC-027. **Nothing below is meaningful until this passes.**

```bash
uv run pytest -q tests/contract/test_long_phase_liveness.py > /tmp/roster.log 2>&1
```

Expect:

- **The roster is complete.** Every phase in `src/` that can outlast the 180 s ceiling has an entry.
  A phase that can outlast the ceiling and is **absent from the roster** fails the check — absence
  is the failure, not silence, because *a phase absent from the enumeration is a phase nobody
  checked*. Both silent phases found so far were found only by searching their loops for a logging
  call and finding none.
- **`emits: bracketing_only` fails.** A phase that logs at entry and exit is silent for exactly the
  interval that matters.
- **`streaming_verified` is established by observation**, not by the presence of an emit call: the
  stream is seen growing monotonically *while the phase runs*.
- **Each entry declares `work_derived` or `observer_derived`**, and an `observer_derived`-only phase
  is recorded as **not discharged** — it may not clear a silence bound.
- **Zero entries are discharged by widening a bound or the ceiling.**

**The break that must go red**: remove the tick from inside the confirm loop's poll body and leave
the entry/exit lines. The check must fail on `bracketing_only`. Restore, then buffer the emission
and flush at exit. The check must fail on `streaming_verified`. A check that survives either break
is rejected.

**Current expected state** (re-verified against `HEAD`): the roster has **six** entries and
**three are still silent** — `run.backstop_end_turn_confirm` (a *second*, hand-rolled 200 s confirm
loop the `act/verify.py` fix never reached; owner: LIVE lane), `saves.load_await_phase` (**300 s,
1.67× the whole budget**, in a package with no logging at all; owner: **unresolved**), and
`resilience.recover` (**this feature's own rung 4**; owner: **unresolved**). Each is allowlisted with
a task id and an owning lane, and the allowlist **fails the moment its phase starts emitting**, so
it can only tighten. `act.confirm_execution` satisfies all three clauses. `provider.in_flight_call`
satisfies periodicity and streaming and **fails work-derived**, so provider silence yields
*undetermined*. That is the honest interim state, and it must be visible in the record.

**Two things this roster proves that no per-phase care could have.** A *second copy* of an
already-fixed loop was found only by enumeration. And rung 4 is itself a long silent phase — so an
un-emitting rung is **detected as a stall while it is recovering from one**.

---

## Scenario 1 — A long think is not a stall, and a silent one is

**Proves**: FR-001, FR-005, FR-007, SC-001, SC-002. This is the criterion the owner cares about
most and the one most easily satisfied vacuously.

```bash
uv run pytest -q tests/integration/test_liveness_long_call.py
```

Expect:

- A model call **longer than the run's stall threshold**, still emitting → classified `live`, **zero
  rungs fire**, and the call is recorded **as a long call, not as a stall**.
- The set contains **at least 20 model calls that individually exceed the run's threshold**, so
  SC-001 is exercised rather than vacuously satisfied. A run with no long calls proves nothing about
  a watchdog.
- The same call with emission suppressed → a candidate opens.
- A productive multi-hour turn → **nothing fires**. No signal keys off elapsed turn time.
- The derived per-run bound is **tighter than 180 s** wherever that run's history supports it, and
  the release report includes **how often the 180 s ceiling actually bound**. In a correct system
  that count is **zero**; a non-zero count is a finding about a *missing emission*, not about the
  number.

**The break that must go red**: change the detector to measure "no completion for 180 s" instead of
"no signal for 180 s". Scenario 1 must fail — a detector that measured completion would be wrong for
a different reason and must be proven against explicitly.

---

## Scenario 2 — Four reproductions, four classes, recovery disabled

**Proves**: US1 — FR-009 – FR-015, FR-060, SC-003, SC-004, SC-011, SC-028.

Run with **recovery disabled entirely**, so the classification is judged on its own:

| Reproduction | Expected disposition | Expected basis in the record |
|---|---|---|
| Stranded full-screen diplomacy view *(live lane)* | `blocked_by_game` | an independent read confirming the view is held — **not** the screen probe alone |
| `has_blocking_prompt` permanently true while the engine permits end turn | `blocked_by_harness` | both sides captured verbatim; the engine's answer read **in a separate command** |
| Tuner made unreachable | `unreachable` **+ a resolved cause** | which of the four causes, and what established it |
| **Class 4** — the screen probe answering `recognized=true, has_blocking_prompt=false` **with a full-screen modal up** (`EndGameMenu`) | `blocked_by_game`, **or at minimum `undetermined`** | an independent read that did **not** come from the screen probe. **A `live` verdict here is the failure the whole scenario exists to catch** |
| A healthy turn with a model call past the threshold | `live` | nothing fired, **and the `live` rests on a signal from outside the component whose health it asserts** |

Expect also:

- **Exactly one disposition per candidate**, produced before anything acts.
- **Every classification names every signal and read it rested on**, so the decision is re-derivable
  from the record without re-running the detector.
- A signal whose source is unavailable is reported **`unavailable`** and never as "no progress".
- **Conflicting or insufficient evidence → `undetermined`**, recorded as a valid outcome, with **no
  rung above report-only eligible**.
- A **dead monitor** is distinguishable from a run with nothing to report.

And the rule class 4 forces on every other scenario:

- **No `live` disposition rests only on state produced by the component whose health it asserts**
  (FR-010, SC-028). Against class 4 a harness-state detector does not merely fail to notice — **it
  agrees, and certifies a dead board as healthy, repeatedly.** "The run looks fine" is not a
  conclusion the classifier may reach from harness telemetry, however much of it there is.
- **Every detector, probe and check in this run can return "I do not know"** (FR-060). Ask it of
  each, mechanically: *show me an input on which this returns unknown.* One that cannot is an
  **allowlist being read as a detector** — the screen watchlist, where an unlisted screen reads as
  *no screen*, is the instance that produced class 4.

**The two breaks that must go red**: (a) make the classifier decide `blocked_by_game` from harness
state alone — the class-2 reproduction must then misclassify and the check must fail; (b) let the
screen probe's negative answer count toward `live` — the class-4 reproduction must then return
`live` and the check must fail. A build that passes (b) is the build that certifies dead boards.

---

## Scenario 3 — Every detector fires on the real failure and sits still on the control

**Proves**: US2 — FR-054 – FR-060, SC-004, SC-017, SC-018, SC-019, SC-028.

```bash
uv run pytest -q tests/contract/test_watchdog_reachability.py
uv run pytest -q tests/integration -k "fixture or control"
```

Expect, for **each** detector and **each** rung:

1. It is fired by a **reproduction of an observed incident — the real failure injected at a seam the
   real failure actually crosses** — and **not** by a mock of the harness state the failure would
   have produced. *An injection at a seam the real failure never crosses does not satisfy FR-054.*
2. It has a **negative control**: a healthy run wearing the same external symptom, on which it
   demonstrably does not fire.
3. It has been **demonstrated failing against a deliberately broken build**. A check that stays
   green against the broken build is rejected and counts toward no criterion.
4. It is shown **reachable from at least one production path — and able to fire there**. This is
   stronger than "has a caller": the day's most serious finding was a mechanism whose production
   call site existed and passed an empty default, so it was reached and inert. Some production call
   site must supply the inputs that make it capable of firing.
5. **Constructing it without a required input fails**, rather than producing something that runs and
   never fires. No default renders a detector inert.
6. A rung is shown **not to fire on the classes it does not answer**, as well as working on the one
   it does.
7. **It can return "I do not know", demonstrated on an input where it does** (FR-060, SC-028). One
   that structurally cannot is recorded as **an allowlist being read as a detector** — not as a
   detector needing work — and is not accepted. *Used to permit, unknown means deny and the failure
   is a visible false refusal; used to detect, unknown means "nothing there" and the failure is an
   invisible, confident false all-clear. Same data structure, opposite failure mode.* The precedent
   is the content screening gate, where an unaddressed contaminant category moved from reading as
   *clean* to **withholding**.

**Any mechanism exercised only by tests is a release-blocking finding.**

---

## Scenario 4 — Clear a blocking view the way a human would *(live lane)*

**Proves**: US3 — FR-018, FR-030, FR-046, FR-047, SC-009, SC-010.

Reproduce the stranded diplomacy view. Expect, in order, on the run timeline:

1. Classified `blocked_by_game`, with the registry entry matched.
2. The **registered human action** performed, and **nothing else**.
3. Verified by an **independent re-read** showing the view closed and the board interactive —
   **never** the return value of the call that performed the dismissal.
4. The run resumes **from a fresh observation**.

And the parity half:

- A view carrying a player choice — **including a single acknowledging button** — is offered to the
  agent as a declared prompt **first**. Zero are dismissed without that offer having been made and
  its window having expired.
- Every watchdog dismissal is **labelled an intervention** and counts as **nothing** toward the
  agent's decision coverage. Zero appear in any measure of what the agent played.
- An **unregistered** view stalls the run visibly with the unknown view recorded. **Nothing is
  dismissed by guesswork.**
- A view whose dismissal is **known unreachable** on this host — the post-defeat exit-confirm modal,
  or anything on Wayland — is **skipped with that reason recorded**, and escalation continues. It is
  not attempted and retried.

---

## Scenario 5 — Break the harness out of a block it created

**Proves**: US4 — FR-031, SC-005.

Reproduce the wrong-prompt-key incident so `has_blocking_prompt` is permanently true while the
engine's end-turn availability reads true.

Expect:

- The divergence is detected at the **first refused action, not the eighth**.
- The stale belief is **replaced by a freshly observed value obtained in a separate command** — not
  cleared, not reset to a default, not re-derived from the state that produced it.
- The turn then ends normally, from a fresh observation, and **the refused attempts remain in the
  record as refused**.
- On a run where belief and engine **agree**, the class-2 detector fires **zero** times regardless
  of elapsed time.

**Note the fixture's own obligation.** That `UI.CanEndTurn()` read true throughout the original
incident is reported by the live lane and is **not corroborated** in the written record co-located
with it. The eight refusals and the wedged board are confirmed. The fixture MUST establish the
engine-side reading as part of reproducing it — the whole class turns on the harness and the engine
disagreeing, and *an unverified side of a disagreement is not evidence of one*.

---

## Scenario 6 — One symptom, four causes, and a lock that is never stolen

**Proves**: US5 — FR-012, FR-013, FR-032, FR-044, SC-011, SC-012, SC-013.

Reproduce each cause separately:

| Cause | Expected |
|---|---|
| Lock held by a **live** run | sub-classified correctly; the lock is **never** force-taken; the run waits or stops rather than interleaving into another run's client |
| Lock left by a **finished** run | reclaimed — on positive evidence that **the run** is over, **not** on the lock's age, mtime, or the liveness of the client process it merely names |
| Connection inside a prior close's ~2 s refusal tail | distinguished from the two lock cases behind the same connection-refused symptom |
| Client killed mid-run (a real TCP reset, not a clean EOF) | the run reaches a **recorded terminal state with a reason**, in whatever error form the transport produced — never left in `playing` with no reason |

Also:

- A load into a context that refuses it — the post-defeat menu — has its precondition **observably
  unsatisfied**, and the rung is **skipped with that reason**, not attempted and retried. The
  precondition is named for **a fresh main menu versus a reused one**, which is what actually
  governs the refusal; "post-defeat" is a correlate.
- **Rung 5 is reported unavailable at preparation** and **zero escalations are attempted toward
  it**.

**The break that must go red**: restore staleness to `psutil.pid_exists(client_pid)`. The
finished-run lock case must then fail — the client outlives the run, so the leaked lock never reads
stale and blocks every later run.

---

## Scenario 7 — A divergence that did not stop play

**Proves**: US6 — FR-019 – FR-026, SC-015, SC-016.

Reproduce the turn-advance denial (engine 56 → 57 while the record says nothing advanced) and the
three-artifact goal-run disagreement.

Expect:

- Each is recorded as a **divergence incident with both sides verbatim**.
- **Play is not interrupted**, and **no rung fires**.
- **No existing record is amended.** Any correction is an **additive record naming what it
  contradicts**, original intact and both readable.
- The run's **record-completeness status reflects it**, so it is identifiable as unfit for trending
  input.
- At run end, exactly one artifact — **the store** — is authoritative, the others are checked
  against it, and any disagreement is an incident rather than a silent resolution.

**The control that matters most, and it is a control on the check rather than on the system**: run
the checker against a **correct** system using a **same-command readback** — the retracted
close-call measurement, and T293's turn-number read issued before the asynchronous advance landed.
The check MUST return **`unverified`**, never `diverged`. A detector that would repeat the
retraction must be caught by its own negative control, not in the ledger afterwards.

---

## Scenario 8 — What did freezes cost us?

**Proves**: FR-042, FR-043, SC-014, SC-025.

From the record alone, without reading logs, answer for a run **and summed across runs**:

- wall-clock lost to stalls and recoveries, **separately from time legitimately spent thinking**;
- model spend **discarded** by a recovery, attributable to the stall that caused it;
- time from stall onset to either recovery or a recorded terminal state — **≤ 30 minutes**, of which
  at most 3 minutes is detection.

Today the project records per-incident costs — a defeat recovery at ~17 minutes of human
intervention, $0.464 on one hung driver, a paid live run lost 90 seconds to machine contention — and
**no aggregate at all**. This scenario passes when "how much have freezes cost us" is answerable.

A **human-initiated** recovery is labelled and excluded from detector-latency statistics, so the
detector's own latency is never measured against a human-rescued run.

---

## Scenario 9 — The audits that block release

Run per release. **Seven carry an explicit release-blocking clause** — any finding in those blocks
the release. SC-023 and SC-026 are audited per release without one; they are listed here because
they are audits, and separated because rounding them up into the blocking set would be a claim the
spec does not make.

| Audit | Blocking? | Asserts |
|---|---|---|
| SC-001 | **yes** | Zero healthy runs interrupted, across ≥20 unattended runs containing ≥20 over-threshold model calls |
| SC-028 | **yes** | Zero detectors, probes or checks incapable of returning "I do not know" — each demonstrated on an input where it does; and zero `live` dispositions resting only on the suspect component's own state |
| SC-004 | **yes** | Each of the **four** blocked classes has both an injected-real-failure fixture and a negative control |
| SC-007 | **yes** | Zero recovery actions a human could not perform through the standard game UI |
| SC-015 | **yes** | Zero existing turn records amended, rewritten or deleted |
| SC-018 | **yes** | Every detector and rung reachable from a production path **and able to fire there** |
| SC-020 | **yes** | Zero stall detections, classifications, recovery attempts or divergence incidents in the playing agent's context |
| SC-023 | audited | Every registered signal states what it observes **and what its presence does not establish**; zero signals named for a condition they do not directly observe |
| SC-026 | audited | Every phase that can outlast the ceiling is enumerated and audited; zero unenumerated; zero silent ones resolved by widening the ceiling |

---

## CI-runnable subset

Everything except scenario 4 and the client-death half of scenario 6 runs without a client, because
the seams the real failures cross are injectable: the transport (a real TCP reset, for which
`tests/fakes/aborting_tuner.py` already exists), the harness-belief/engine seam, the lock directory,
and the confirm loop.

The two live scenarios are marked, excluded from the default run by
`addopts = ["--ignore=tests/live", "-m", "not client"]` — **unconditionally**, because a marker that
skips when the client is *absent* runs when the client is *present*, which is exactly the case that
costs another lane its stage.

---

## Reference

- Requirements: [spec.md](./spec.md) — FR-001 – FR-060, SC-001 – SC-028, plus the post-plan
  amendment of 2026-09-22 (the work-derived split, class 4, and the "I do not know" audit)
- Design decisions: [research.md](./research.md) — R2 (work-derived vs observer-derived) and R12
  (every number and its provenance) are the two to read first
- Entities and invariants: [data-model.md](./data-model.md)
- Boundaries: [contracts/](./contracts/)
