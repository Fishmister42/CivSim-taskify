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

## 6. Current state of the contract, stated because two lanes are building against it

Verified against the working tree at `0187345`. Both new modules are **untracked and uncommitted**.

| Phase | Emitter | Clause 1.1 periodic | Clause 1.2 streams | Clause 1.3 work-derived | Verdict |
|---|---|---|---|---|---|
| End-turn confirm poll (`act/verify.py::confirm_execution`, ~200 s bound, 2 s poll) | `act/liveness.py::emit_confirm_liveness` → `act.confirm_execution.waiting`, emitted per attempt before evaluating it | **Yes** — rides the existing 2 s poll | To verify | **Yes** — each iteration completes a real tuner round trip, so an emitted iteration *proves* the tuner answered within the last poll | **Satisfies the contract** |
| Model-provider call (`provider/openrouter.py::OpenRouterProvider.complete`, 120 s per attempt, chained) | `provider/liveness.py::provider_call_liveness` → `started` / `waiting` / `finished`, daemon ticker at 15 s | **Yes** | To verify | **No** — a thread beside a blocking `httpx.Client.post` | **Adequate for clause 1.1–1.2; fails 1.3.** Register it honestly and **supplement** it |

**What "supplement" means, concretely**: request the completion as a **stream** and emit on chunk
arrival, carrying cumulative bytes, chunk count, and time since the last chunk. FR-003 names
"provider bytes actually arriving on an in-flight call" as an acceptable signal, and it is the only
listed one that observes the work. The provider port's contract is untouched — exactly one decision
per call; streaming changes how the bytes arrive, not what the call returns.

**Until that lands, provider silence yields `undetermined`, not `stalled`.** That is correct under
FR-015 and it means the 85% case is *reported* rather than *acted on*. It must be visible in the
record as an interim state, not implicit.

**Three coordination items, for whoever owns them** — not fixable inside this contract:

1. The in-flight modules cite task id **`T290`**, which in `specs/002-civ-playing-harness/tasks.md`
   is a different task ("record honestly which success criteria are unobserved"). The highest
   allocated id there is T294 and the emission work has **no task of its own**, so it is invisible
   to every tally.
2. The modules are **uncommitted**, so under the standing clean-worktree rule nothing about them is
   verified yet, and this feature's phase-1 gate cannot read as satisfied.
3. `tests/contract/test_long_phase_liveness.py` is **named by both shipped docstrings and does not
   exist in the tree** — a documentation claim ahead of its artifact.
