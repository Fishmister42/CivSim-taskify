# Contract: The Liveness Signal

**Between**: every phase that can outlast the silence ceiling (**spec 002 owns most of them**) and
the watchdog reader (**spec 004**).
**Schema version**: 1.0 | **Date**: 2026-09-22

This is the contract the whole feature rests on. If it is wrong, the watchdog either kills healthy
runs or sleeps through freezes, and there is no third outcome.

---

## 1. What a phase owes

> **Every phase that can run longer than the silence bound MUST publish an affirmative liveness
> signal the watchdog can read, at an interval shorter than that bound.**

This is an obligation on **phases**, not only on the watchdog. Three clauses, and a phase must
satisfy all three:

### 1.1 Periodic, not bracketing

A phase that emits at entry and at exit and nothing in between is **indistinguishable from a wedged
phase for the entire interval that matters**. Two log lines three minutes apart satisfy the letter
of "the phase emits" and still produce a watchdog that kills healthy runs.

`emits: bracketing_only` is a **failure**, not a partial pass.

### 1.2 Verified to stream

A signal that exists in the code but only materialises at completion — buffered, flushed at exit,
written on teardown — is the same defect wearing a fix. Verification is **empirical**: observe the
stream growing monotonically *during* the wait. Confirming that an emit call exists does not
satisfy this clause.

### 1.3 Derived from the work, if it is to clear the bound

**This is the clause that is easiest to pass review and hardest to get right.**

| Kind | Definition | Establishes | May clear the silence bound for its phase? |
|---|---|---|---|
| `work_derived` | The emission is a **byproduct of the work**. It cannot be produced unless the work made progress | The work is progressing | **Yes** |
| `observer_derived` | The emission comes from something **beside** the work — a timer, a ticker thread, a supervisor clock | The harness process and its emitter are alive | **No.** It bounds the gap between records. It is not evidence the phase is progressing |

**Why the distinction is not pedantry.** A daemon thread emitting "still waiting, 45 s elapsed"
every 15 s beside a blocking HTTP call is a *thread being alive* — precisely what FR-001 forbids
inferring health from. Put the real failure against it — *a model call that hangs with the socket
open and no bytes arriving* — and the ticker ticks happily forever while the watchdog reads a
healthy run. That is the feature's own indistinguishability, moved up one level and made harder to
see, because now there *is* a signal and it looks affirmative.

It is also, exactly, a **value whose name asserts something it does not mean**:
`provider.call.waiting` asserts the call is waiting; it is emitted whether the call is waiting,
hung, or dead.

**An observer-derived signal is not useless and is not to be deleted.** It bounds the interval
between records during connect, TLS, and the pre-first-byte wait, where no work-derived signal
exists to be had. During that window the honest reading is *observer-derived only*, which produces
**undetermined**, never a stall.

---

## 2. The record

```jsonc
{
  "schema_version": "1.0",
  "run_id":     "run-…",        // scopes the record to the thing the watchdog guards
  "signal_id":  "act.confirm_execution.waiting",   // MUST resolve in the registry
  "emitted_at": "2026-09-22T14:05:11.482-04:00",
  "seq":        1043,           // monotonic per run; the stream's growth is itself the signal
  "payload":    { /* bounded, flat, built from values the emitter already holds */ }
}
```

**Destinations, both written by one call so they cannot drift:**

1. The `civsim_harness` logger, which `telemetry/logging.py` forces through the redacting filter and
   formatter — i.e. the driver log a watchdog polls.
2. A per-run **append-only NDJSON liveness stream**, so an out-of-process reader can observe
   monotonic byte growth without parsing prose and without touching the tuner.

**Liveness records are NOT written to the match store.** A tick every few seconds for the length of
a run is write amplification against a store with a no-delete/no-edit floor, and — decisively — the
store's own write path is one of the things that can wedge. *A signal that requires the suspect
component to be healthy cannot testify about it.* The **conclusions** drawn from ticks are store
writes: a candidate, a classification, a rung attempt, a divergence incident.

---

## 3. The Principle I boundary on a tick

A liveness record carries **the shape of the question, never its answer**.

| Emitter | MAY carry | MUST NOT carry |
|---|---|---|
| Confirm loop | `declaration_id`, the **static catalog predicate expression**, attempt number, elapsed, timeout, poll interval | predicate *bindings*, any `last_read` quotation, any `Observation` |
| Provider call | provider name, model name, elapsed, bound, tick number, cumulative bytes/chunks | prompt text, response content, image bytes, any observation payload |
| Monitor pass | pass number, which signals were read, each read's status, the pass's own cost | anything the reads returned about game state |

**Never returned to a caller.** A tick is never merged into an `ExecutionVerification`, a
`DecisionResponse`, or an `Observation`. There is no code path by which one can reach the agent.

**A failed emission is swallowed.** Telemetry must never fail the work it describes — the emission
exists to describe the call, so letting it break the call inverts the whole point.

**The two emitters are deliberately not merged behind one helper.** Their Principle I exclusions are
*different* — one must exclude prompt and response content, the other must exclude game state — and
a single shared helper would put both exclusions one refactor away from each other with nothing red
to show for it.

---

## 4. The registry entry a signal must have

No signal may be read by the watchdog unless it is registered with all eight fields. See
[data-model.md §1](../data-model.md). The field that makes the registry honest is
**`presence_does_not_establish`**, which is **required and non-empty** and is rejected at load
otherwise.

A signal **MUST NOT be named for a condition it does not directly observe**. Worked negatives, all
three from this project's own record:

- "a model request is open" is **not** "the agent is thinking";
- "the client process exists" is **not** "the game is responsive";
- "the harness believes a prompt is blocking" is **not** "the game is blocking".

---

## 5. What the reader owes

- **Interrogate, do not wait.** Read the stream, query state, observe in-flight work. A watchdog
  that only watches a clock has not satisfied this contract — passive waiting cannot distinguish
  thinking from wedged, which is the entire problem.
- **Derive the bound per run.** Record the observed interval between occurrences of each signal, per
  run, and hold a run whose own history supports a tighter bound to the tighter one. The ceiling is
  a backstop.
- **Never conflate absent with unavailable.** A source the reader cannot reach is *instrument
  failure*: reported unavailable, never as "no progress", and it pushes toward **undetermined**.
- **Publish its own liveness, work-derived**, at the end of a pass that actually completed its
  reads — so a dead monitor is distinguishable from a quiet run by a reader outside the process.
- **Stay inside its remit.** The reader is constructed from a `run_id` and may observe and act on
  **that harness game run** only. It is not a general rule for every command or process: the
  project's own accepted test command is silent by construction for 225–270 s, and a watchdog that
  reached it would kill a healthy suite while the lane filed it as a flaky test — the misattribution
  being the worse half.

---

## 6. Current state of the contract

**Re-verified against `HEAD`** (revision 1). The emitters have **landed**: `act/liveness.py` and
`provider/liveness.py` are tracked and committed, `tests/contract/test_long_phase_liveness.py` is in
`HEAD` at 561 lines (added by `33348e3`), and the work carries task id `T302` — 21 citations across
7 files, excluding `tasks.md`'s own allocation line.

### 6.1 The roster

Six phases, three of them still silent. The allowlist requires a task id **and** an owning lane per
entry, and **fails the moment its phase starts emitting**, so it can only tighten.

| Phase | Bound | 1.1 periodic | 1.2 streams | 1.3 work-derived | Verdict |
|---|---|---|---|---|---|
| `provider.in_flight_call` | 120 s nominal — **`httpx` applies it per socket operation, not to the whole call**; measured 146 s | yes | yes | **no** | **Adequate for 1.1–1.2; fails 1.3.** Registered as observer-derived |
| `provider.chain` | unbounded in aggregate | yes | yes | yes | satisfies |
| `act.confirm_execution` | 200 s | yes — rides the existing 2 s poll | yes | **yes** — each iteration completes a real tuner round trip | **satisfies** |
| `run.backstop_end_turn_confirm` | 200 s | **no** | — | — | **silent.** A *second, hand-rolled* confirm loop that does not route through `confirm_execution`, so the `act/liveness.py` fix never reached it. Owner: **LIVE lane** |
| `saves.load_await_phase` | **300 s — 1.67× the whole budget**, longest explicit bound in `src/` | **no** | — | — | **silent**, in a package with no logging at all. Owner: **UNRESOLVED**. Highest-value entry |
| `resilience.recover` | unbounded; up to 300 s per attempt | **no** | — | — | **silent.** Publishes `RunEvent`s, which reach the store and not the log a watchdog polls. Owner: **UNRESOLVED** |

### 6.2 What the roster proved about itself

- **`run.backstop_end_turn_confirm` is this contract's justification in one row.** The
  `confirm_execution` fix looked complete. A *second copy of the same loop* elsewhere was untouched
  by it, and no amount of care on the first loop finds the second — only enumeration does.
- **`resilience.recover` is spec 004's own rung 4.** The ladder's rungs are themselves long phases,
  so an un-emitting rung is **detected as a stall while it is recovering from one**. Every rung must
  emit a work-derived tick for its duration, and the classifier must treat *a rung of this run is in
  flight* as a recorded state rather than as silence.
- **`provider.chain`'s own note** — that a `RunEvent` reaches the store and not the driver log —
  is §2's transport reasoning arrived at independently.

### 6.3 The supplement still owed

`provider.in_flight_call` is observer-derived. `provider/liveness.py`'s docstring now opens
**"OBSERVER-DERIVED, NOT WORK-DERIVED. Read this before treating a tick as progress"**, states the
clear-a-bound rule as normative, and names the successor: **stream the provider completion so that a
chunk arriving is the event.** FR-003 names "provider bytes actually arriving on an in-flight call",
and it is the only candidate that observes the work. The port's contract is untouched — exactly one
decision per call; streaming changes how the bytes arrive, not what the call returns.

**Until it lands, provider silence yields `undetermined`, not `stalled`.** Correct under FR-015, and
it means the 85% case is *reported* rather than *acted on*. That is the honest interim state, and it
is not the feature working.
