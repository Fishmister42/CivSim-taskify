# Contract: Reconciliation, and the Independent Read

**Between**: this feature, **spec 003's store** (which holds the incidents), and whoever writes the
next check.
**Schema version**: 1.0 | **Date**: 2026-09-22

---

## 1. The rule

> **An assertion about what happened MUST be derivable from a read of the engine, not from the
> return value of the call that requested it.**

Liveness is the subset of this where the divergence happens to stop play. Three divergences were
measured on 2026-09-22 and **only one was a stall**; all three were invisible to a green test suite,
and every one was caught by reading the store or the engine rather than a call's return value.

---

## 2. `IndependentRead` — the type that makes the rule structural

```jsonc
{
  "schema_version": "1.0",
  "read_id": "rd-…",
  "what_was_read": "engine.game_turn",
  "value": 57,
  "write_command_seq": 4181,      // the tuner command that performed the action being verified
  "read_command_seq":  4183,      // the tuner command that performed THIS read
  "retried_until": "2026-09-22T14:08:44.000-04:00",
  "independence": "independent"   // | "same_command" | "bound_expired"
}
```

### 2.1 Construction fails unless `read_command_seq > write_command_seq`

**A readback issued in the same tuner command as the write returns the pre-call value.** This is not
a theory; it is the single most expensive measurement error on this project, and it has fired in
both directions:

- It produced a finding — *"the engine's close call returns success and does nothing"* — that the
  project's own ledger later **retracted**. The call was working all along; the check lied about it.
  Setting zoom to 0.5 read back `0.0499997` in-command and `0.50000047683716` in a later command.
- It is **live and open today** as spec 002's T293: `turn.end_turn` dispatched correctly, the
  harness re-read the turn number before the asynchronous advance landed, and **three turn cycles
  recorded `game_turn_advanced: False` while the engine went 56 → 57**. The harness caused three
  turns and credited itself with none.

The second of those is this feature's own divergence instance 2. A reconciliation detector built on
a same-command readback would have reported a **false divergence on a perfectly healthy system** —
and then a ladder would have acted on it.

### 2.2 Independent in *ordering*, not only in command

Some engine effects are asynchronous — the turn advance is the measured example. A read in a later
command that was issued **before the effect landed** is still not a verification. So a verification
read is retried under its own bound; a bound that expires yields `independence: "bound_expired"`.

### 2.3 A non-independent read never produces a divergence

It produces **`unverified`**. A verification that is not independent does not merely fail to
confirm — **it manufactures a false disagreement**, which under FR-021 becomes a recorded incident
and under FR-027 can make a rung eligible. That is the most likely first bug in this feature, and it
is the exact outcome the owner said must not happen, arriving through the detector rather than
through a timeout.

---

## 3. The comparison, and why it has three outcomes

```jsonc
{
  "schema_version": "1.0",
  "check_id": "chk-…",
  "run_id": "run-…",
  "checkpoint": "turn_boundary",        // | "refused_action" | "run_end"
  "harness_side": { "game_turn_advanced": false },      // verbatim
  "engine_side":  { /* IndependentRead */ },            // verbatim
  "outcome": "diverged"                                  // | "agreed" | "unverified"
}
```

| Outcome | Means | Consequence |
|---|---|---|
| `agreed` | both sides match, on an independent read | nothing |
| `diverged` | both sides differ, on an independent read | a `DivergenceIncident` |
| **`unverified`** | the read was not independent, or its bound expired | **recorded as unverified**; no incident, no rung |

**Collapsing `unverified` into `diverged` manufactures incidents on healthy runs. Collapsing it into
`agreed` hides real ones.** It is the same distinction the signal layer draws between "no signal was
emitted" and "the watchdog could not read one", applied to reads instead of to signals — and it is
the third place in this feature where *absence* and *unobservability* must not share a
representation.

---

## 4. Checkpoints and minimum comparisons

| Checkpoint | Compares | Catches (the measured instance) |
|---|---|---|
| `refused_action` | the harness's belief that an action is unavailable **vs** the engine's own availability answer | the `has_blocking_prompt` refusal — detected at the **first** refusal, not the eighth |
| `turn_boundary` | the turn number the record claims **vs** the engine's turn number, retried under its own bound | the turn-advance denial (engine 56 → 57, record says none) |
| `run_end` | the run timeline, a driver's own result output, and the store | the three-artifact goal-run disagreement |

Also required (FR-020): an action the harness recorded as **applied** against the engine state that
action should have produced.

---

## 5. The divergence incident, and the refusal that defines it

```jsonc
{
  "schema_version": "1.0",
  "incident_id": "div-…",
  "run_id": "run-…",
  "both_sides": { "harness": {...}, "engine": {...} },   // VERBATIM, both of them
  "play_stopped": false,
  "resolution_status": "open",       // | "explained" | "superseded_by_additive_record"
  "contradicts_record_ref": null     // present only on an additive correction
}
```

1. **A divergence is an incident whether or not play stopped** (FR-021).
2. **`play_stopped: false` ⇒ no rung fires.** Recorded and surfaced, never auto-corrected (FR-022).
3. **No existing turn record is ever amended, rewritten, or deleted** to agree with a later engine
   read. *A harness that edits its own evidence produces records indistinguishable from correct
   ones, so the very failure this feature exists to make visible would become invisible again.*
   Deliverable 3's no-delete/no-edit floor and its published `MUTATING_OPERATIONS` surface
   (`store/contract.py`) are the enforcement; this feature relies on them rather than restating
   them.
4. **A correction is an additive record naming what it contradicts** (FR-023), leaving the original
   intact and both readable — the same abandoned/authoritative pattern spec 002 FR-047 uses for a
   replayed turn.
5. **An unresolved incident sets the run's `record_completeness_status`** (FR-024), so Principle
   III's bar — no gapped run feeds trending — applies to a run that is *complete but internally
   inconsistent*. Note this widens what that value means: it currently means "no gaps".

**A future contributor MUST NOT "finish" this feature by adding auto-correction.** The scope
boundary is drawn where it is because rewriting a record to match a later engine read is far more
dangerous than the defect it patches.

---

## 6. Artifact authority

Where several artifacts of one run can disagree, **exactly one is authoritative and it is the match
store.** The run timeline and any driver result output are reconciled against it at the end of every
run, and disagreement is recorded as a divergence incident rather than resolved silently.

The store is authoritative because it is the only one of the three with a no-delete/no-edit floor,
the only one Principle III speaks about, and the only one another deliverable reads. Declaring it
authoritative also makes the disagreement *recordable*, because the record of the disagreement goes
to the thing that cannot rewrite it.

---

## 7. Every check carries a negative control that is a *correct* system

**This is a requirement on the check, not on its test suite** (FR-055). For every reconciliation
check there must exist a control which is a **correct system that a naive implementation of that
check would report as diverged**, and the check must be demonstrated not to fire on it.

Two are available today and both are buildable without a client:

| Control | The naive check would say | The correct check says |
|---|---|---|
| The retracted close-call measurement — a same-command readback against an engine call that was working | `diverged` ("the close call does nothing") | **`unverified`** |
| T293's turn-number readback issued before the asynchronous advance landed | `diverged` ("the turn did not advance") | **`unverified`**, then `agreed` once the retry lands |

A detector that would repeat the retraction must be caught **by its own negative control**, not in
the ledger afterwards. A check without a demonstrated negative control is not accepted.
