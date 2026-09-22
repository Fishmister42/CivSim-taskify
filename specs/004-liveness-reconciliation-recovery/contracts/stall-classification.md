# Contract: Stall Classification

**Between**: the watchdog (spec 004) and everything that reads a diagnosis — **deliverable 1**, the
directing Claude Code session, and the recovery ladder.
**Schema version**: 1.0 | **Date**: 2026-09-22

---

## 1. Why this is a classification and not a predicate

A watchdog that only asks *"has progress stopped?"* cannot choose a recovery, because the correct
response to each of the three blocked classes is different and applying the wrong one is
destructive. The harness could not tell **"the game is blocked"** from **"I blocked myself"** from
**"I cannot reach a healthy game"** — and all three presented identically as a stalled run.

So: **classify before acting.** No rung above report-only becomes eligible until a disposition
exists.

---

## 2. The record

```jsonc
{
  "schema_version": "1.0",
  "classification_id": "cls-…",
  "run_id":       "run-…",
  "candidate_id": "cand-…",          // the silence observation that triggered this
  "disposition":  "blocked_by_harness",
  "cause":        null,              // REQUIRED when disposition == "unreachable"
  "evidence":     [ /* SignalObservation[] — every signal it rested on */ ],
  "independent_reads": [ /* IndependentRead[] — every engine read it rested on */ ],
  "monitor_load": { "pass_ms": 38, "tuner_round_trips": 1 },
  "concluded_at": "2026-09-22T14:08:02.119-04:00",
  "latency_ms":   174000,
  "initiated_by": "autonomous"       // or "human" (FR-017)
}
```

---

## 3. The five dispositions

| Disposition | MUST mean | MUST NOT be concluded from |
|---|---|---|
| `live` | an affirmative signal was observed | — |
| `blocked_by_game` | an **independent read confirms** the game is holding a view or state the board cannot leave | harness state alone; **a probe's own failure** |
| `blocked_by_harness` | the harness's belief and the engine's answer **disagree**, with the engine permitting what the harness refuses | elapsed time; the harness's belief alone |
| `unreachable` | the harness **cannot obtain an engine answer at all** | a game answer it did in fact receive |
| `undetermined` | the classifier cannot decide, or evidence for two dispositions conflicts | — |

**Exactly one per candidate.** These are distinct and MUST NOT collapse into a single "stalled"
state.

### `unreachable` is sub-classified before any rung is eligible

One symptom has had several causes on one day, and a recovery must never be selected from a symptom.

| `cause` | Recognised by |
|---|---|
| `lock_held_by_other_party` | a run lock whose **owning run** is live |
| `connection_refused_in_close_tail` | a connection attempted inside the refusal tail a previous close left behind (~2 s, measured on this build); already corroborated by one re-probe in the existing detector |
| `client_process_absent` | process liveness says the client is gone |
| `client_present_not_answering` | the process exists and a bounded engine read does not return |

---

## 4. Rules a consumer may rely on

1. **At least one observation obtained independently of the component under suspicion** (FR-010). A
   disposition about the harness's own state is **never** derivable from harness state alone — a
   liveness check that consults only harness state structurally cannot see the case where the
   harness is the problem, which is the most dangerous of the three classes.
2. **A probe's own failure is evidence for `unreachable` and never the game's answer** (FR-051). It
   can therefore **never** produce `blocked_by_game`.
3. **A missing engine method means *unknown*, never *no*** (FR-053). The session-active query does
   not exist on this build; its absence is detected at run preparation and is never read as "no
   session is open".
4. **`undetermined` is a correct outcome of the detector, not a failure of it** (FR-015). Under it
   only rung 0 is eligible. When the classifier cannot decide, it degrades toward the cheapest,
   least destructive rung and toward reporting. **Never escalate on uncertainty** — the asymmetry
   between killing a healthy run and tolerating a slow one is the whole design.
5. **A classification that cannot rule the monitor out as the cause is `undetermined`** (FR-052).
   Monitoring contention has already produced `Errno 111` against a live, listening client on this
   box; a monitor that adds contention is a cause, not an observer.
6. **Re-derivable from the record** (FR-014). `evidence` + `independent_reads` must be sufficient to
   reach the same disposition without re-running the detector.
7. **Elapsed time alone never classifies.** What makes a rung eligible is *elapsed time during which
   nothing affirmative was emitted*. A turn that has run for 146 seconds has told you nothing.
8. **Not progressing ≠ not supposed to be progressing.** A run legitimately `paused` by an operator,
   or waiting on a provider backoff, is not a stall. `WAITING_ON_MODEL` and `WAITING_ON_GAME` are
   *harness beliefs about what it is doing*, never signals of progress.

---

## 5. Relationship to the existing detection outcomes

The four outcomes spec 002 already produces — `crash_detected`, `hang_detected`,
`unresponsive_detected`, `unknown_screen` — **keep their meaning and are not replaced**. They record
*which probe noticed*. They become inputs here rather than the system's answer.

| Existing outcome | Contributes toward | Never sufficient alone for |
|---|---|---|
| `crash_detected` | `unreachable` / `client_process_absent` | anything else |
| `hang_detected` | `unreachable` | `blocked_by_game` — a probe failure is never a game answer |
| `unresponsive_detected` | `unreachable`, cause TBD | `blocked_by_harness` |
| `unknown_screen` | `blocked_by_game`, **only with** a confirming independent read | `blocked_by_harness`; an unregistered view stalls visibly instead |
| *(new)* belief/engine disagreement | `blocked_by_harness` | `blocked_by_game` |
| *(new)* silence past the derived bound | opens a **candidate** | **every** disposition — silence triggers classification, it never concludes it |

---

## 6. For deliverable 1

A stall must be presentable as: *what was observed, when, what it was diagnosed as, what it rested
on, what was done, and what it cost* — without reading logs, and summable across runs.

- **A human-initiated resolution is labelled** (FR-017) and is **excluded from detector-latency
  statistics**. Otherwise the feature's headline metric improves every time a human rescues a run,
  which is backwards.
- **A run with a dead monitor must not render as a quiet healthy run** (FR-008). The monitor's own
  pass records are in the same stream; their absence is the signal.
- **The same view goes to the user and to the directing session** (Principle VI). There is one
  stream and one set of records, not a summary for one audience and detail for the other.
