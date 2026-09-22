# Contract: Stall Classification

**Between**: the watchdog (spec 004) and everything that reads a diagnosis — **deliverable 1**, the
directing Claude Code session, and the recovery ladder.
**Schema version**: 1.0 | **Date**: 2026-09-22

---

## 1. Why this is a classification and not a predicate

A watchdog that only asks *"has progress stopped?"* cannot choose a recovery, because the correct
response to each of the blocked classes is different and applying the wrong one is destructive. The
harness could not tell **"the game is blocked"** from **"I blocked myself"** from **"I cannot reach
a healthy game"** — and all three presented identically as a stalled run.

**And there is a fourth that does not present as a stalled run at all**: *"I am certain nothing is
blocking me, and I am wrong"* (§4.0). That one never asks the question, because nothing looks wrong.
It is the reason §4.0 comes before the rest of the rules rather than after them.

So: **classify before acting** — and never from the suspect's own account of itself. No rung above
report-only becomes eligible until a disposition exists.

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
| `live` | an affirmative signal was observed, **at least one of which did not come from the component whose health is being asserted** | harness state alone — see §4.0 |
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

### 4.0 The rule the rest of this contract rests on — class 4

**The screen-identity probe answers `recognized=true, has_blocking_prompt=false` while a full-screen
modal is up.** Observed live against `EndGameMenu`, 2026-09-22.

Class 2 is *"I blocked myself."* **Class 4 is "I am certain nothing is blocking me, and I am
wrong"** — and it is worse, because in class 2 the run visibly stops and *something* looks wrong,
while in class 4 **nothing looks wrong at all**. A detector consulting harness state does not merely
fail to notice: **it agrees with the harness and certifies a dead board as healthy, repeatedly,
while the run is dead.** Every signal it has says fine. No elapsed-silence fallback rescues it,
because the harness is not silent — it is confidently reporting health.

Two consequences a consumer may rely on:

- **Every `live` disposition rests on a signal that did not come from the component whose health is
  being asserted.** A `live` supported only by harness state is recorded **`undetermined`**
  (FR-010, SC-028).
- **Class 4 adds no sixth disposition.** A board held by a modal is still `blocked_by_game`. What it
  adds is the case where **no candidate opens at all**, which is why it lands on FR-010 and FR-060
  rather than on FR-009.

### 4.1 The rest

1. **At least one observation obtained independently of the component under suspicion** (FR-010). A
   disposition about the harness's own state is **never** derivable from harness state alone — a
   liveness check that consults only harness state structurally cannot see the case where the
   harness is the problem. Per §4.0 this is **load-bearing for the whole feature**, not a refinement
   of one class.
2. **Every detector, probe and comparison here can return "I do not know"** (FR-060, SC-028). A
   check that structurally cannot is an **allowlist being read as a detector**: used to *permit*,
   unknown means deny and the failure is a visible false refusal; used to *detect*, unknown means
   "nothing there" and the failure is an invisible, confident false all-clear. Same data structure,
   opposite failure mode. The screen watchlist is the instance that produced class 4, and the
   content screening gate's own remedy — an unaddressed contaminant category moving from *clean* to
   **withhold** — is the precedent this follows.
3. **A probe's own failure is evidence for `unreachable` and never the game's answer** (FR-051). It
   can therefore **never** produce `blocked_by_game`.
4. **A missing engine method means *unknown*, never *no*** (FR-053). The session-active query does
   not exist on this build; its absence is detected at run preparation and is never read as "no
   session is open".
5. **`undetermined` is a correct outcome of the detector, not a failure of it** (FR-015). Under it
   only rung 0 is eligible. When the classifier cannot decide, it degrades toward the cheapest,
   least destructive rung and toward reporting. **Never escalate on uncertainty** — the asymmetry
   between killing a healthy run and tolerating a slow one is the whole design.
6. **A classification that cannot rule the monitor out as the cause is `undetermined`** (FR-052).
   Monitoring contention has already produced `Errno 111` against a live, listening client on this
   box; a monitor that adds contention is a cause, not an observer.
7. **Re-derivable from the record** (FR-014). `evidence` + `independent_reads` must be sufficient to
   reach the same disposition without re-running the detector.
8. **Elapsed time alone never classifies.** What makes a rung eligible is *elapsed time during which
   nothing affirmative was emitted*. A turn that has run for 146 seconds has told you nothing.
9. **Not progressing ≠ not supposed to be progressing.** A run legitimately `paused` by an operator,
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
| *(class 4)* the screen probe reporting **no** blocking prompt | **nothing.** Its *presence* establishes only that the watchlist did not match — which is not "no screen is up" | **`live`**, ever. This is the allowlist-read-as-detector instance (FR-060), and reading it as an all-clear is what let a modal hold a board while every signal said fine |
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
