# Contract: The Recovery Rung

**Between**: anyone adding or changing a rung, run preparation (which reports availability), and the
record.
**Schema version**: 1.0 | **Date**: 2026-09-22

---

## 1. The declaration

A rung may not exist without all seven fields. This is where the precondition, the destructiveness
and the parity basis are stated, and an unpinned shape is how a rung ships without one.

```jsonc
{
  "schema_version": "1.0",
  "rung": 1,                                  // the ladder's order IS its destructiveness order
  "answers_disposition": "blocked_by_game",
  "answers_cause": null,                      // required where the disposition is sub-classified
  "precondition": "a registered blocking view is confirmed present by an independent read, AND its dismissal is reachable by this host's input path",
  "cost": "one keypress or one click",
  "destroys": "nothing",
  "verified_by": "an independent re-read showing the view closed and the board interactive",
  "available_on_host": { "available": true, "reason": null }
}
```

---

## 2. The ladder

| Rung | Answers | Precondition (must be **observed**) | Destroys | Status |
|---|---|---|---|---|
| **0** observe and report | anything, incl. `undetermined` | none | nothing | new |
| **1** dismiss a registered blocking view as a human would | `blocked_by_game` | registry entry matched by an independent read **and** reachable on this host | nothing | new; **needs a declared dismissal action that does not exist today** |
| **2** re-derive the harness's belief from the engine | `blocked_by_harness` | belief and engine disagree, read in a separate command | the stale belief only | new |
| **3** re-establish the path to the game | `unreachable` + cause | that specific cause is established | nothing — a lock is never force-taken from a live owner | partly exists (the ~2 s close-tail re-probe) |
| **4** reload the last named save and replay the turn | `unreachable`, or an unresolved `blocked_by_game` | **the game is in a load-accepting context** | the in-progress attempt, retained as abandoned; the model spend it discards | exists — `resilience/recovery.py::RecoveryEngine` |
| **5** restart the game client | `unreachable`, client absent or unresponsive | — | the client process | **UNAVAILABLE on every host** |

---

## 3. Invariants

### 3.1 A rung never fires on the absence of a contrary observation

Only on a **positively observed** precondition. `RecoveryAttempt` is not constructible without a
`precondition_observation`, which makes this structural rather than a rule.

### 3.2 Ordering by destructiveness is the safety argument

The classifier will sometimes be wrong. If it calls a class-2 block a class-1 block, rung 1 fires,
costs a wasted Escape, verifies nothing changed, and escalates. **That is the intended shape of a
misclassification's cost** — and it only holds while the cheap rungs come first. Skipping rung 1
because it "only" answers one class inverts the whole design: the alternative recovery for a held
board is rung 4, which destroys the in-progress turn to clear a view one keypress clears.

### 3.3 Escalation is monotonic and the ladder never cycles

```text
classification ──► undetermined ──► rung 0 only
      │
      ▼
 rung N eligible ──► precondition observed? ──no──► SKIPPED(reason) ──► rung N+1
      │ yes
      ▼
   fire ──► independent verification ──► resolved ──► resume from a FRESH observation
      │                                     │
      │                                     └─ identical stall recurs ──► rung N+1, never rung N again
      ▼
   failed ──► rung N+1 ──► … ──► highest AVAILABLE rung exhausted
                                        │
                                        ▼
                    UNRECOVERABLE_FAILURE, with last-known good save + highest rung reached
```

A stall that recurs identically after a successful rung **escalates rather than repeats** —
otherwise rung 1 dismisses a view, the view returns, and the system presses Escape forever while
reporting success each time. This must **compose with** `run/decision_loop.py`'s existing
`MAX_UNPRODUCTIVE_REPLAYS = 3` rather than duplicate it: that constant already guards the case where
`RecoveryEngine` resets its own counter on every successful-but-unproductive reload.

### 3.4 A skip is a different record from a failure

`skipped` means the precondition was **observably unsatisfiable**, and the reason is recorded.
Conflating skip with failure is how "the ladder tried everything" gets recorded for a ladder that
could not try anything.

**Skipping is mandatory where the precondition does not hold, not optional.** A recovery that
re-issues a call which cannot succeed burns its whole attempt limit and converts a recoverable stall
into a failed run — which is exactly what has been observed on this project, three times, against
the post-defeat menu.

### 3.5 A precondition is named for what governs it, not for its correlate

**Rung 4 is the worked case.** The measured distinction is **a fresh main menu versus a reused
one**: a load from a freshly launched menu works, and the three recorded refusals were all on a menu
reached by exiting a game or by losing one. "Post-defeat" is a *correlate*. A rung whose
precondition is named for the correlate fires in the wrong place — and a guard whose scope does not
match the scope of the thing it guards is one of this project's three named defect shapes.

### 3.6 Verification is never the call's own answer

Two measured hazards, and satisfying one does not satisfy the other:

- **The call can report success without acting.** A popup's own click callback was measured to
  report success and do nothing, which is why that dismissal is completed by a real host click.
- **The re-read can report the pre-call value.** A readback in the same tuner command as the write
  returns pre-write state. See [reconciliation-check.md](./reconciliation-check.md).

### 3.7 Resume from a fresh observation

After any successful recovery the run resumes from a **freshly assembled observation** and acts on
**no state read before the stall** (spec 002 FR-046). A recovery that succeeds into a different
state than expected — a reload landing at a menu rather than the board — is caught by this, because
verification is a read of where the game actually is and not an assumption that the action did what
it is named for.

### 3.8 Availability is resolved at preparation, never discovered mid-stall

The harness reports which rungs this host has **before the run starts** and never escalates toward
one it cannot perform. Three cases are already known:

| Case | Effect |
|---|---|
| Rung 5, every host | unavailable — no client lifecycle capability exists |
| Rung 1 on Wayland | unavailable — synthetic input is blocked by design |
| Rung 1 against the post-defeat exit-confirm modal | unavailable *for that view* — it ignores synthetic input; recorded per view in the registry |

### 3.9 A rung is not constructible into an inert state

Required inputs have **no defaults**; a missing input is a startup failure, never a silent no-op
(FR-059). This is the direct descendant of the project's adopted root cause — *an optional parameter
with a safe-looking empty default that every unit test supplies and the single production call site
does not* — which accounted for three of one day's four most serious findings.

### 3.10 Every game-touching rung is a declared action with a parity basis

Pressing Escape qualifies; clicking the one button a view is already offering qualifies. **A debug
call does not, however convenient** (FR-046, Principle I). Where the action is delivered by a
bespoke path rather than through Firetuner it records the documented gap (Principle II, spec 002
FR-028) — and `tests/contract/test_synthetic_input_declaration.py` already `ast`-derives every
`HostPlatform.send_input` call site in `src/` and asserts the capability it serves is
`path: bespoke` with a non-empty `firetuner_gap`, so an undeclared dismissal call site cannot ship.

---

## 4. The attempt record

```jsonc
{
  "schema_version": "1.0",
  "attempt_id": "att-…",
  "classification_id": "cls-…",
  "rung": 1,
  "outcome": "fired_succeeded",        // | "fired_failed" | "skipped"
  "skip_reason": null,                  // REQUIRED when "skipped"
  "precondition_observation": { /* required for any firing */ },
  "action_taken": "views.dismiss_blocking_view",   // a declared action, never a raw path
  "verification": { /* IndependentRead — required for any firing */ },
  "initiated_by": "autonomous",         // or "human"
  "duration_ms": 1830,
  "wallclock_lost_ms": 421000,
  "model_spend_discarded": { "calls": 0, "usd": 0.0 }
}
```

Every attempt, skip, success and failure is a `RunEvent` **persisted before the next action is
taken** (FR-040, Principle III).

`wallclock_lost_ms` and `model_spend_discarded` are attributed **at the point of loss**, separately
from time a turn legitimately spent thinking — so "how much did freezes cost us" is answerable from
the record and summable across runs. Today the project records per-incident costs and **no aggregate
at all**.
