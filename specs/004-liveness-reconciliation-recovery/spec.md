# Feature Specification: Harness Liveness, Reconciliation, and Autonomous Recovery

**Feature Branch**: `004-liveness-reconciliation-recovery`

**Created**: 2026-09-22

**Status**: Draft

**Input**: User description: "You got war declared on you. I think we need to /speckit-specify closing the
loop on detecting these freezes. We need to let it plan without killing it but we need to detect and
recover from this til harness is solid. We're losing too much time and tokens."

**Scope anchor**: A deliverable-level feature, sibling to specs 001–003. The constitution charters
five deliverables and its Governance clause explicitly contemplates "any deliverable added later";
this is that. It exists to make the **resilience half of Principle VII** — "the harness MUST detect
Civilization VI crashes, preserve the last-known save/state, and resume or restart the run without
silent data loss" — actually implementable. Today it is not merely incomplete: the one recovery
Principle VII names outright, restarting the run, has no mechanism on any platform, and the
detectors that would trigger it cannot tell a stuck run from a thinking one. Deliverable 2
(spec 002) asserts this behaviour in FR-044–FR-050 and SC-010/SC-021 and cannot satisfy it.

---

## The problem this feature exists to solve

**A run that is thinking is, from the outside, indistinguishable from a run that is stuck.**

Model calls are approximately **85% of a turn's wall-clock time** on this project. That is measured,
not estimated: across twelve turns on the Linux node, **1,017 s of the ~1,170 s elapsed was the
provider**, at ~7.8 model calls per turn and ~97 s of wall-clock per turn; everything else — the
declaration sweep, the quicksave, the end-turn confirmation — is 10–20 s of it. Per-turn wall-clock
across those runs ranged from 56 s to 146 s, and a fake-provider run of the same loop takes **2 s per
turn**. So the spread between "a normal turn" and "a normal turn" is already 70×, before a late-game
board is considered.

A naive timeout therefore does not detect stalls; it kills legitimate planning and throws away paid
work. Any mechanism built here must be able to sit quietly through a forty-minute think and still
notice a board that has been frozen for four hours.

**Killing a healthy run is a worse failure than tolerating a slow one.** That asymmetry is not a
tuning preference; it is the governing constraint, and every threshold, default, and tie-break in
this feature is set on that basis.

And the second half, which is what makes a timeout not merely blunt but wrong:

> The harness could not tell **"the game is blocked"** from **"I blocked myself"** from **"I cannot
> reach a healthy game"** — and all three presented identically as a stalled run.

A watchdog that only asks *"has progress stopped?"* cannot choose a recovery, because the correct
response to each of the three is different and applying the wrong one is destructive. So detection
is not a predicate. It is a **classification**, and it must happen before anything acts.

### The evidence base — observed on this machine, 2026-09-21 and 2026-09-22

These are not hypotheticals. Each is a reproduced incident and each is a required fixture.

**Class 1 — the game is genuinely blocking.**

- A full-screen diplomacy view, opened by the harness's own `diplomacy.send_delegation`, which no
  harness action could close. The method to ask whether a session is open does not exist on this
  build. **One human Escape keypress cleared it.** The board sat frozen for hours.
  **Read the retraction with the incident.** The stranding was real and is recorded as a standing
  rule. The root cause first attributed to it — "the engine's close call returns success and does
  nothing" — was later **retracted by this project's own ledger as a measurement artifact**: a
  readback issued *in the same tuner command as the write* returns the pre-call value, so the close
  probably worked all along and the check lied about it. That retraction is not a footnote to this
  feature; it is one of its load-bearing facts. A reconciliation detector built on a same-command
  readback would have reported a **false divergence** on a perfectly healthy system, and then a
  ladder would have acted on it. See FR-025 and FR-055.
- The same class recurs with a leader conversation: an approach view where "Goodbye" was rejected
  16 times running because the exit needed the session's own close path rather than a response. As
  this spec was being written, an AI leader's Surprise War declaration and its single-button popup
  were holding the board the same way.

**Class 2 — the harness blocked itself.**

- A wrong prompt key was rejected by the game-side guard, so the prompt stayed unanswerable, the
  harness's own `has_blocking_prompt` predicate stayed permanently true, and `turn.end_turn` was
  refused **eight times out of eight** — honestly refused, never even issued — until the backstop
  paused the run. No run on that board could advance a turn at all. *(Provenance: the eight refusals
  and the wedged board are confirmed in the project record. That the engine's own `UI.CanEndTurn()`
  read **true** throughout is reported by the live lane and is **not** corroborated in the written
  record co-located with this incident. The fixture for this class MUST establish the engine-side
  reading as part of reproducing it — see FR-054 — because the whole class turns on the harness and
  the engine disagreeing, and an unverified side of a disagreement is not evidence of one.)*
  This class is the most dangerous, because every external symptom matches class 1 and the class-1
  recovery — dismiss the view — does nothing here.

**Class 3 — a healthy game is unreachable.**

- `tuner unreachable [Errno 111]` against a client that was alive and listening. This one symptom
  had **two distinct causes on the same day**: a run lock held by another run, and a freshly
  constructed client landing inside the connection-refusal tail created by a previous connection's
  own close. Even within one class, the cause must be distinguished before a recovery is chosen.
- A client segfault mid-run surfaced as a bare `ConnectionResetError` with no stop reason, stranding
  the run in `playing` forever — because the purpose-built fault handling was keyed on a different
  exception family than the one a dead socket actually raises.
- A run lock leaked by a run that had already **finished**, silently blocking every later run.
- After a defeat, the main menu refuses a save load and its exit-confirm modal ignores synthetic
  input; ending the process at the empty menu is data-safe, and a fresh menu comes back in ~38 s.

### What already exists, so this is not built twice

This feature is **not greenfield**, and the spec is written against what is already wired:

- Process-liveness, tuner-heartbeat, per-operation-bound and screen-identity detection already run
  during a turn on a ~10-second cadence, with each pass itself bounded, and classify into *crash
  detected*, *hang detected*, *unresponsive detected*, and *unknown screen*. A sustained run of eaten
  passes — not a single one — is what raises unresponsiveness, and a single dropped connection is
  re-probed once before classification, to avoid false positives from the tuner's own post-close
  refusal tail (measured at roughly two seconds on this build).
- Recovery already exists as exactly one move: abandon the turn attempt, reload that turn's start
  quicksave, re-observe, and stop after a bounded number of consecutive failures.
- A no-progress backstop already ends a turn that stops accomplishing anything, without fabricating
  an end-turn decision.
- That detection wiring was itself dead code until recently — built, tested, and never reached from
  the production path. It is the clearest available precedent for the risk this spec's proving
  requirements exist to close.

**The gap is therefore not "detect a crash".** It is (a) that the existing classification sorts
faults by *which probe noticed*, not by *what is actually wrong*, so it cannot choose between the
three classes below; (b) that there is exactly one recovery move, with nothing cheaper beneath it
and nothing above it; and (c) that a divergence which does not stop play is not looked for at all.

### The reframing: this is reconciliation, not just liveness

The check that catches class 2 — compare the harness's belief against an independent read of the
engine — is the general answer to a whole class of defect, of which freezes are only the subset
where the divergence happens to stop play. Three measured instances today, **only one of which was a
stall**:

1. The harness refused its own turns while the engine said they were allowed. (Play stopped.)
2. The harness **caused three game turns and recorded that none advanced.** The engine went turn 56
   → 57; the record denies it. Play never stopped.
3. A goal run's timeline said the goal was reached, its own result file said it was not, and the
   store showed the second leg never executed a single decision step. Three artifacts of one run,
   mutually disagreeing. Play never stopped.

Every one of the three was caught by reading the store or the engine rather than a call's return
value, and every one was **invisible to a green test suite**. If this feature is specified as
"liveness" it will be built narrowly and will catch only the first. It is specified as
**reconciliation**: *an assertion about what happened must be derivable from a read of the engine,
not from what the harness asked for.* Principle III already demands exactly that of the turn record.

### The scope boundary — read this before "completing" the feature

- **Detection and reporting cover the full divergence class.** A divergence between the harness's
  record and an independent engine read is an **incident**, recorded as such, whether or not play
  has stopped.
- **Automated recovery covers the freeze subset only.** A stalled run gets the escalating ladder. A
  non-stalled divergence — instance 2 above — is **recorded and surfaced, never auto-corrected.**
- **Why the line is drawn there:** silently rewriting a turn record to match a later engine read is
  a harness that edits its own evidence. Principle III's completeness guarantee and deliverable 3's
  no-delete/no-edit floor both forbid it, and it would be far more dangerous than the defect it
  patches — an auto-corrected record is indistinguishable from a correct one, so the very failure
  this feature exists to make visible would become invisible again. A future contributor must not
  "finish" this feature by adding auto-correction.

---

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Tell a thinking run from a stuck one, and name which kind of stuck (Priority: P1)

The researcher starts an unattended run and leaves. At some point the run stops making progress —
or appears to. The harness notices, works out **why** it appears stopped, and says so: the run is
alive and thinking; or the game is holding a view the board cannot get past; or the harness is
refusing an action the game would accept; or the harness cannot reach a game that may be perfectly
healthy; or it genuinely cannot tell. It says which, it says what it looked at to decide, and it
does not touch the game to find out.

The researcher comes back to a named diagnosis within minutes, instead of a board that has been
frozen for four hours with no record of it.

**Why this priority**: This is the deliverable's floor and the only slice that is independently
valuable on day one. A correct classification with **no** recovery at all already converts "hours of
silence" into "a named incident on the timeline", which is most of the time and tokens the owner is
losing. Every recovery rung below is a choice that can only be made once this exists; a ladder built
on an unclassified stall picks its rung by guessing.

**Independent Test**: Run three reproductions back to back — the stranded diplomacy view, the
`has_blocking_prompt` refusal, and a tuner made unreachable — with recovery disabled entirely.
Verify each is classified into the correct one of the three classes, with its basis recorded. Then
run a healthy turn containing a model call longer than the stall threshold and verify the
classification is "live" and that nothing fired.

**Acceptance Scenarios**:

1. **Given** a run whose agent is in a model call lasting far longer than any previous call in that
   run, **When** the liveness monitor evaluates it, **Then** the run is classified as live, no
   recovery rung fires, and the long call is recorded as a long call rather than as a stall.
2. **Given** a run that has stopped making observable progress, **When** the monitor evaluates it,
   **Then** it produces exactly one disposition — live, blocked by the game, blocked by the harness,
   the game is unreachable, or undetermined — before any action is taken.
3. **Given** a classification is produced, **When** it is read, **Then** it names every signal and
   every read it rested on, so the decision can be re-derived from the record.
4. **Given** the harness believes an action is blocked, **When** the monitor classifies the stall,
   **Then** it consults the game engine's own answer to the equivalent question, and does not
   conclude "the game is blocking" from harness state alone.
5. **Given** the evidence for two classes conflicts, or no class can be established, **When** the
   monitor concludes, **Then** the disposition is "undetermined", that is recorded as a valid
   outcome rather than a detector failure, and no rung above report-only becomes eligible.
6. **Given** a signal's source is unavailable, **When** the monitor evaluates, **Then** that signal
   is reported as unavailable and is never reported as "no progress".
7. **Given** the monitor itself has stopped observing, **When** the run is inspected, **Then** that
   is distinguishable from a run with nothing to report.
8. **Given** the researcher is watching a frozen board, **When** they assert the stall and authorise
   a named rung, **Then** it is performed and recorded as human-initiated, distinguishable from an
   autonomous recovery.

---

### User Story 2 - Prove the watchdog fires only when it should (Priority: P2)

Before any of this is trusted to act unattended, the researcher needs to see each detector fire
against a **real, injected instance** of the failure it claims to detect — not a mock of the
harness's own belief about it — and to see each detector sit still against a healthy run wearing the
same external symptom. And for each acceptance check, they need to have seen it fail.

**Why this priority**: This project's characteristic defect, found eight-plus times on the day this
spec was written, is *a mechanism that exists, is well built, and is never reached in the case that
matters — with the test suite green throughout*. A watchdog is exactly the kind of component that
fails that way, and it fails silently: nothing distinguishes "no stalls occurred" from "the detector
never ran". A success criterion that cannot fail is the thing this feature must not ship. Three
real, reproduced, mutually distinct incidents are already available as fixtures, so this is buildable
immediately after US1 and gates the release of every story below it.

**Independent Test**: For each detector and each rung, run its injected-failure fixture and confirm
it fires; run its negative control and confirm it does not; then break the mechanism deliberately
and confirm the acceptance check goes red. A check that stays green against the broken build is
rejected.

**Acceptance Scenarios**:

1. **Given** a detector, **When** it is accepted, **Then** it has been fired by a reproduction of an
   observed incident — the real failure injected — and not only by a mock of the harness state that
   the failure would have produced.
2. **Given** a detector, **When** it is accepted, **Then** it has a negative control: a healthy run
   exhibiting the same external symptom, against which the detector demonstrably does not fire.
3. **Given** any acceptance check for this feature, **When** it is accepted, **Then** it has been
   demonstrated failing against a deliberately broken build.
4. **Given** the full set of detectors and rungs, **When** the release audit runs, **Then** each is
   shown to be reachable from at least one production path, and any that is exercised only by tests
   is a release-blocking finding.
5. **Given** a detector or rung whose required input is missing, **When** it is constructed, **Then**
   it fails to start — it MUST NOT be constructible into a state where it runs and never fires.
6. **Given** a recovery rung, **When** it is accepted, **Then** it has been shown not to fire on the
   classes it does not answer, as well as to work on the class it does.

---

### User Story 3 - Clear a blocking view the way a human would (Priority: P3)

The board is behind a full-screen view or a modal the game will not leave on its own — a diplomacy
session the harness itself opened, a war-declaration popup, a screen whose own close call lies. A
human sitting at the machine would press Escape, or click the one button. The harness does the same
thing, confirms by looking that the board came back, and the run carries on from a fresh
observation.

**Why this priority**: This is the cheapest and least destructive rung, it answers the class that
cost the most wall-clock today, and it loses nothing — no save reload, no replayed turn, no killed
process. It is the rung that turns "frozen for hours" into "a recorded blip". It needs US1's
classification to know it applies, which is the only reason it is not first.

**Independent Test**: Reproduce the stranded diplomacy view. Verify the harness classifies it as
blocked-by-the-game, identifies the registered dismissal for that view, performs it, confirms by an
independent read that the view closed and the board is interactive, and resumes the turn from a
fresh observation — with the whole sequence on the run timeline.

**Acceptance Scenarios**:

1. **Given** a registered blocking view is confirmed present by an independent read, **When** the
   rung fires, **Then** it performs the human action registered for that view and nothing else.
2. **Given** the dismissal has been performed, **When** its outcome is verified, **Then** the
   verification is an independent re-read showing the view closed — never the return value of the
   call that performed the dismissal.
3. **Given** a blocking view carries a player choice, **When** it is encountered, **Then** it is
   routed to the agent as a declared prompt and answered as a recorded decision; the watchdog MUST
   NOT answer it on the agent's behalf.
4. **Given** a blocking view that is not registered, **When** it is encountered, **Then** the run
   stalls visibly with the unknown view recorded, and nothing is dismissed by guesswork.
5. **Given** a view whose registered dismissal is known not to be reachable by the harness's input
   path — such as the post-defeat exit-confirm modal, which ignores synthetic input — **When** the
   rung is considered, **Then** its precondition is observably unsatisfiable, the rung is skipped
   with that reason recorded, and escalation continues.

---

### User Story 4 - Break the harness out of a block it created (Priority: P4)

The game is fine. The board is fine. The harness is refusing to act because of something it believes
about its own state — and that belief is wrong. The harness notices the disagreement between what it
believes and what the engine says, replaces the belief with an observed value read in a separate
command, and carries on.

**Why this priority**: It is the most dangerous class, because it is externally identical to class 1
and the class-1 recovery does nothing for it — so without this, a correctly-detected stall gets a
recovery that cannot work, escalates, and eventually destroys a perfectly good run. It is placed
after US3 only because it requires the reconciliation read that US1 establishes to be wired into the
recovery path, not just the detector.

**Independent Test**: Reproduce the wrong-prompt-key incident so that `has_blocking_prompt` is
permanently true while the engine's end-turn availability reads true. Verify the divergence is
detected at the **first** refusal rather than the eighth, that the rung re-derives the belief from a
separate engine read rather than clearing the flag, and that the turn then ends normally.

**Acceptance Scenarios**:

1. **Given** the harness refuses an action on its own internal belief, **When** the monitor
   evaluates, **Then** it compares that belief against the engine's own answer to the equivalent
   question and records both sides verbatim.
2. **Given** belief and engine disagree, **When** the rung fires, **Then** the stale belief is
   **replaced by a freshly observed value obtained in a separate command** — not cleared, not reset
   to a default, and not re-derived from the same state that produced it.
3. **Given** belief and engine agree, **When** the monitor evaluates, **Then** the class-2 detector
   does not fire, whatever the elapsed time.
4. **Given** the rung has fired, **When** the run resumes, **Then** it acts from a fresh observation
   and the refused attempts remain in the record as refused.

---

### User Story 5 - Restore a path to a healthy client (Priority: P5)

The harness cannot reach the game. That might mean the client is gone, or that another run holds the
lock, or that a connection was attempted inside the refusal window a previous close left behind, or
that the client is alive and simply not answering. These are one symptom and four situations, and the
harness works out which before it does anything — because force-taking a lock from a live run, or
killing a client that was merely busy, is how a recoverable stall becomes a destroyed one.

**Why this priority**: It answers a real and recurring class, but its top rung — restart the client —
**cannot be built today**, so the slice is partially blocked on a capability that does not exist.
What it can deliver now is the part that has already cost paid runs: distinguishing the causes,
resolving lock ownership honestly, and ending stranded runs in a recorded terminal state instead of
leaving them in `playing` forever.

**Independent Test**: Reproduce each cause separately — a lock held by a live run, a lock left by a
finished run, a connection attempted inside the refusal tail, and a client killed mid-run — and
verify each is sub-classified correctly and gets the rung that matches it, with the live-owner lock
never reclaimed.

**Acceptance Scenarios**:

1. **Given** the game is unreachable, **When** the monitor classifies, **Then** it resolves the cause
   — lock held by another party, connection refused within a prior close's refusal tail, client
   process absent, client present but not answering — before any rung becomes eligible.
2. **Given** a run lock is encountered, **When** ownership is resolved, **Then** it is resolved by
   determining whether the owner is still live, **not by the lock's age**, and a lock is reclaimed
   only on evidence that its owner is gone.
3. **Given** a lock whose owner is live, **When** recovery is considered, **Then** the lock is never
   force-taken and the run waits or stops rather than interleaving into another run's client.
4. **Given** the client process has died mid-run, **When** the fault surfaces — in whatever error
   form the transport produces — **Then** the run ends in a recorded terminal lifecycle state with a
   stop reason, and is never left in `playing` with no reason.
5. **Given** the game is in a context that refuses a save load — such as the post-defeat main menu —
   **When** the reload rung is considered, **Then** its precondition is observably unsatisfied and
   the rung is skipped with that reason recorded rather than attempted and failed.
6. **Given** ending the client process is the only remaining option, **When** it is considered,
   **Then** it fires only on an observation that nothing in flight would be lost, and never on a
   board mid-turn.
7. **Given** the host has no capability to start or restart the client, **When** the run is prepared,
   **Then** the harness reports that the top rung is unavailable on this host, and it never
   escalates to a rung it cannot perform.

---

### User Story 6 - Catch a divergence that did not stop play (Priority: P6)

Not every disagreement between the harness and the game freezes anything. The harness advanced three
game turns and recorded that none advanced. A goal run's timeline, its result file, and the store
told three different stories about the same run. Play continued the whole time, the suite was green
the whole time, and only a person reading the store found it. The researcher wants these surfaced
the same way a freeze is — as a recorded incident on the run's timeline, with both sides captured —
and wants the record left exactly as it was written.

**Why this priority**: It costs an incident record on top of machinery US1 already builds, and it is
what keeps the detector from being built too narrowly to catch two of the three measured instances.
It is last because it changes no run's behaviour — it only makes a class of silent corruption
visible, which matters most once runs are reliably finishing.

**Independent Test**: Reproduce the turn-advance denial (engine goes 56 → 57 while the record says
no turn advanced) and the three-artifact goal-run disagreement. Verify each is recorded as a
divergence incident with both sides verbatim, that the run's completeness status reflects it, that
play was not interrupted, and that **no existing record was amended**.

**Acceptance Scenarios**:

1. **Given** a reconciliation checkpoint, **When** the harness's record disagrees with an independent
   engine read, **Then** a divergence incident is recorded with both sides captured verbatim,
   whether or not play has stopped.
2. **Given** a divergence where play did not stop, **When** it is handled, **Then** it is recorded
   and surfaced and **no recovery rung fires**.
3. **Given** any divergence, **When** it is handled, **Then** no existing turn record is amended,
   rewritten, or deleted to agree with a later engine read.
4. **Given** a correction is warranted, **When** it is recorded, **Then** it is an additive record
   that names what it contradicts, leaving the original intact and both readable.
5. **Given** a run has an unresolved divergence incident, **When** its status is read, **Then** its
   record-completeness status reflects it, so it is identifiable as unfit for trending input.
6. **Given** a run has ended, **When** its artifacts are reconciled, **Then** exactly one is
   authoritative, the others are checked against it, and any disagreement is recorded as an
   incident rather than resolved silently.

---

### Edge Cases

- **A legitimately enormous think.** The agent's model call runs for an hour on a late-game board.
  Nothing fires. The call is recorded as long, and the run's measured distribution absorbs it.
- **A model call that hangs with the socket open and no bytes arriving.** The transport is alive but
  the call is not progressing — a distinct condition from both "thinking" and "the game is stuck",
  and it belongs to the provider layer's own timeout and fallback behaviour (spec 002 FR-041), not
  to a game-side rung. The liveness record must say which one it observed.
- **The probe itself hangs.** The bounded engine read used to classify does not return. That is
  evidence for "unreachable" and never for "blocked by the game" — a probe failure must not be read
  as a game answer.
- **The probe and the run share the only channel.** There is one connection to the tuner. A liveness
  probe must not consume the run's only channel, and must not be the thing that wedges the run it is
  watching.
- **The monitor causes the fault it looks for.** A probe that adds machine contention can produce the
  very `Errno 111` it is meant to diagnose. Detection must be cheap enough not to be a cause.
- **The engine method needed to classify does not exist on this build.** `IsSessionActive()` is
  absent here. A missing method must be detected as missing at preparation time and must never be
  treated as a negative answer.
- **The close call lies.** An engine call returns success and performs nothing. A recovery's outcome
  can never be taken from the return value of the call that performed it.
- **The recovery succeeds into a different state than expected.** A reload lands at a menu rather
  than the board. Verification is a read of where the game actually is, not an assumption that the
  action did what it is named for.
- **Two runs contend for one client.** A recovery must never resolve contention by taking something
  from a live owner.
- **A lock outlives its run.** A `finished` run leaves its lock behind and blocks every later run.
  Age is not evidence; liveness of the owner is.
- **The stall is in the watchdog.** The monitor stops observing. A run with a dead monitor must not
  read as a quiet healthy run.
- **A recovery loop.** Rung fires, run stalls again the same way, rung fires again. Escalation is
  monotonic within one stall and the ladder never cycles; a repeated identical stall escalates rather
  than repeats.
- **The classifier is simply wrong.** It calls a class-2 block a class-1 block. The class-1 rung is
  non-destructive, so the cost is a wasted Escape and an escalation — which is the intended shape of
  a misclassification's cost, and the reason the ladder is ordered by destructiveness.
- **A recovery destroys work.** A save reload discards the in-progress turn. The abandoned attempt
  stays in the record and the replayed attempt is authoritative, exactly as spec 002 FR-047 requires;
  the discarded model spend is attributed to the stall.
- **A blocking view with exactly one button.** "Goodbye" looks like a dismissal and is in fact the
  player's answer. It is a declared prompt, routed to the agent — the watchdog's job is to make it
  answerable, not to answer it.
- **The human is watching.** The owner can see the freeze before the monitor concludes. A human
  assertion must be able to short-circuit the wait, and must be recorded as human-initiated so the
  detector's own latency is never measured against a human-rescued run.
- **Nothing is wrong and nothing is happening.** A run legitimately paused by an operator, or waiting
  on a provider backoff, is not a stall. The disposition must distinguish "not progressing" from
  "not supposed to be progressing".
- **Recovery is unavailable on this host.** The top rung needs a capability no platform has. The run
  must be told at preparation which rungs exist for it, rather than discovering it at the top of the
  ladder.

## Clarifications

### Session 2026-09-22

- Q: Should the detector be specified as liveness (does the run still progress?) or as reconciliation
  (does the record agree with the engine?) → A: **Reconciliation.** Liveness is the subset where the
  divergence stops play. Three divergences were measured on 2026-09-22 and only one was a stall; a
  liveness-shaped detector would have caught one of three, and all three were invisible to a green
  suite. Specifying it as reconciliation makes the detector correct rather than larger.
- Q: Does automated recovery apply to every divergence, or only to those that stopped play? → A:
  **Only to those that stopped play.** Detection and reporting cover the full divergence class; a
  non-stalled divergence is recorded and surfaced, never auto-corrected. Rewriting a turn record to
  agree with a later engine read is a harness editing its own evidence — forbidden by Principle III's
  completeness guarantee and deliverable 3's no-delete/no-edit floor, and more dangerous than the
  defect it would patch, since an auto-corrected record reads exactly like a correct one.
- Q: When the classifier cannot decide, what happens? → A: **Degrade toward the cheapest rung and
  toward reporting.** "Undetermined" is a first-class recorded disposition, and only report-only is
  eligible under it. Never escalate on uncertainty; the asymmetry between killing a healthy run and
  tolerating a slow one is the whole design.
- Q: May the recovery ladder answer a blocking view that carries a player choice? → A: **No.** A view
  with no player choice may be dismissed by the harness; a view that carries a choice is a declared
  prompt routed to the agent and answered as a recorded decision (spec 002 FR-010). Otherwise the
  watchdog silently becomes a second, undeclared player, which Principle I does not permit at any
  quality of intent.
- Q: Should the stall threshold be a constant? → A: **No — it is derived per run from that run's own
  measured progress intervals, with an absolute detection ceiling.** With model calls at ~85% of
  wall-clock and measured per-turn times spanning 2 s (fake provider) to 146 s (live, late board), a
  fixed constant either trips on normal thinking or sleeps through a real freeze. The ceiling bounds
  how long a genuine freeze can go unnoticed; the derived floor is what keeps a long think from
  being killed.
- Q: Is an independent re-read enough to verify a recovery, or does the re-read itself need
  constraints? → A: **It needs constraints, and this was nearly missed.** A readback issued in the
  same command as the write returns the pre-call value; that artifact produced a retracted finding on
  this project against an engine call that was working correctly. Verification must be independent in
  **command and ordering**, not merely a separate call in one round trip (FR-025), and every
  reconciliation check must carry a negative control that is a *correct* system a naive version of
  that check would call diverged (FR-055). Without this clause, the most likely first bug in this
  feature is a watchdog that reports healthy runs as broken — the exact outcome the owner said must
  not happen, arriving through the detector rather than through a timeout.

## Requirements *(mandatory)*

### Functional Requirements

**Progress signals, and what each one actually measures**

- **FR-001**: Liveness MUST be defined in terms of observable progress signals. Elapsed time MAY be
  an input to a signal but MUST NOT, on its own, be sufficient to classify a run as stalled or to
  make any recovery rung above report-only eligible.
- **FR-002**: Every progress signal MUST be registered with: the event it directly observes, the
  component that is the source of truth for it, how often it can change, what its **absence**
  establishes, and — explicitly — **what its presence does not establish**. A signal MUST NOT be
  named for a condition it does not directly observe. "A model request is open" is not "the agent is
  thinking"; "the client process exists" is not "the game is responsive"; "the harness believes a
  prompt is blocking" is not "the game is blocking".
- **FR-003**: The registered signal set MUST include, and MUST keep distinguishable: an in-flight
  model call and its transport-level liveness; the recording of a completed decision step; a verified
  change in game state; a bounded round trip that returns the game engine's own answer; the presence
  of the client process; and the last time the harness recorded anything at all. A stall MUST NOT be
  concluded from any single one of these.
- **FR-004**: The harness MUST record the observed interval between occurrences of each signal, per
  run, and MUST derive its stall threshold from that run's own measured behaviour rather than from a
  constant fixed in advance. Model-call duration is approximately 85% of a turn's wall-clock on this
  project, so a threshold applied to "time since the last decision step" is dominated by model
  latency and measures nothing else.
- **FR-005**: Time spent inside a model call MUST be attributed to that model call and MUST NOT count
  toward any threshold applied to the game side. A long think MUST NOT be able to present as a game
  stall.
- **FR-006**: A signal whose source is unavailable MUST be reported as unavailable and MUST NOT be
  reported as absence of progress. Absence of evidence is not evidence of a stall.
- **FR-007**: Liveness detection MUST NOT become a turn timer by another name. Spec 002 FR-008 and
  FR-014 forbid bounding a turn by wall-clock; detecting a stall MUST NOT end a turn that is making
  progress, and MUST NOT truncate a turn for being long or expensive.
- **FR-008**: The liveness monitor's own operation MUST be observable. A monitor that has stopped
  observing MUST be distinguishable, from the record alone, from a run that has nothing to report.

**Detection and classification**

- **FR-009**: On observing a candidate stall the harness MUST **classify before it acts**, producing
  exactly one disposition: *live*, *blocked by the game*, *blocked by the harness*, *the game is
  unreachable*, or *undetermined*. No recovery rung above report-only may become eligible until a
  disposition exists.
- **FR-010**: Classification MUST rest on at least one observation obtained **independently of the
  component under suspicion**. In particular, a disposition concerning the harness's own state MUST
  NOT be derivable from harness state alone: the harness's belief MUST be checked against the game
  engine's own answer to the equivalent question. A liveness check that consults only harness state
  cannot see the case where the harness is the problem.
- **FR-011**: *Blocked by the game* MUST mean an independent read confirms the game is holding a view
  or state the board cannot leave. *Blocked by the harness* MUST mean the harness's belief and the
  engine's answer disagree, with the engine permitting what the harness refuses. *The game is
  unreachable* MUST mean the harness cannot obtain an engine answer at all. These are distinct
  dispositions and MUST NOT collapse into a single "stalled" state.
- **FR-012**: *The game is unreachable* MUST be sub-classified by cause before any rung becomes
  eligible — at minimum: a run lock held by another party; a connection refused within the refusal
  tail left by a previous connection's own close; the client process absent; the client process
  present but not answering. One symptom has had several causes on one day; a recovery MUST NOT be
  selected from the symptom.
- **FR-013**: A run lock encountered during classification MUST be resolved by determining whether
  **the run that holds it** is still live — not by the lock's age, mtime, or any timeout, and not by
  the liveness of a process the lock merely names. The observed leak is exactly this confusion: the
  lock records the game client's process, the client outlives the run, so a lock left behind by a
  run that had already **finished** never reads as stale and silently blocks every later run. A lock
  MUST be reclaimable on positive evidence that its own run is over, and the signal used for that
  MUST be named for the run's liveness rather than for a process's.
- **FR-014**: Every classification MUST record the signals and reads it rested on, such that the
  decision can be re-derived from the record without re-running the detector.
- **FR-015**: *Undetermined* MUST be a first-class, recorded disposition — a correct outcome of the
  detector, not a failure of it. When the classifier cannot decide, or the evidence for two
  dispositions conflicts, the harness MUST degrade toward the cheapest, least destructive rung and
  toward reporting. It MUST NOT select a rung whose precondition it could not observe.
- **FR-016**: Killing, reloading, or restarting a healthy run MUST be treated as a strictly worse
  outcome than allowing a slow run to continue. Every threshold, default, and tie-break in this
  feature MUST be set on that basis, and each MUST record that justification where it is defined.
- **FR-017**: An operator MUST be able to assert a stall and authorise recovery from a named rung
  without waiting for the detector, and such a recovery MUST be recorded as human-initiated and be
  distinguishable from an autonomous one — including when the feature's own detection latency is
  measured.
- **FR-018**: Known blocking views MUST be registered with: how the view is recognised, the human
  action that dismisses or answers it, whether that action is reachable by the harness's input path
  on this host, and the independent read that confirms the view closed. A view that is not registered
  MUST stall the run visibly (spec 002 FR-049) rather than be acted on by guesswork.

**Reconciliation against the engine**

- **FR-019**: At defined checkpoints the harness MUST reconcile its own record of what happened
  against an independent read of the game engine's state. An assertion about what happened MUST be
  derivable from an engine read, not from the return value of the call that requested it.
- **FR-020**: Reconciliation MUST compare at minimum: the turn number the record claims against the
  engine's turn number; the harness's belief that an action is unavailable against the engine's own
  availability answer for that action; and an action the harness recorded as applied against the
  engine state that action should have produced.
- **FR-021**: A divergence MUST be recorded as an incident on the run timeline **whether or not play
  has stopped**, with both sides of the disagreement captured verbatim.
- **FR-022**: **Automated recovery applies only to divergences where play has stopped.** A divergence
  that did not stop play MUST be recorded and surfaced and MUST NOT be auto-corrected. The harness
  MUST NOT amend, rewrite, or delete an existing turn record so that it agrees with a later engine
  read — a harness that edits its own evidence produces records indistinguishable from correct ones,
  which is the exact failure this feature exists to make visible.
- **FR-023**: Where a correction is warranted it MUST be recorded additively as a new reconciliation
  record naming what it contradicts, leaving the original intact and both readable — the same
  abandoned/authoritative pattern spec 002 FR-047 uses for a replayed turn.
- **FR-024**: A run carrying an unresolved divergence incident MUST have its record-completeness
  status reflect it, so that Principle III's bar — no gapped run feeds trending or optimization — can
  be applied to a run that is complete but internally inconsistent.
- **FR-025**: The outcome of any action MUST NOT be taken from the return value of the call that
  performed it; verification MUST be an independent re-read of the state the action was supposed to
  change. Two distinct hazards MUST both be satisfied, and satisfying one does not satisfy the other:
  - **The call can report success without acting.** A popup's own click callback was measured to do
    nothing while reporting success, which is why that dismissal is completed by a real host click.
  - **The re-read can report the pre-call value.** A readback issued *in the same command as the
    write* returns state from before the write — the artifact that produced a retracted "this engine
    call is broken" finding on this project. An independent re-read MUST therefore be independent in
    **command and in ordering**, not merely a second call inside one round trip. A verification that
    is not independent in this sense does not merely fail to confirm; it manufactures a false
    disagreement, which under FR-021 becomes a recorded incident and under FR-027 can make a rung
    eligible.
- **FR-026**: Where several artifacts of one run can disagree — the run timeline, a driver's own
  result output, and the match store — exactly one MUST be declared authoritative, the others MUST be
  reconciled against it at the end of every run, and any disagreement MUST be recorded as a
  divergence incident rather than resolved silently.

**The recovery ladder**

- **FR-027**: Recovery MUST be an ordered ladder, cheapest and least destructive first. Each rung MUST
  declare: the disposition (and, where sub-classified, the cause) it answers; the precondition that
  MUST be **observed** before it may fire; what it costs and what it destroys; and the independent
  read that verifies its outcome.
- **FR-028**: A rung MUST NOT fire unless its precondition has been positively observed. It MUST NOT
  fire on the absence of a contrary observation.
- **FR-029**: **Rung 0 — observe and report.** Always available, never destructive, and the only rung
  eligible under an *undetermined* disposition.
- **FR-030**: **Rung 1 — dismiss a registered blocking view the way a human would.** Answers *blocked
  by the game*. Performs only the human action registered for that view, and is verified by an
  independent read showing the view closed and the board interactive.
- **FR-031**: **Rung 2 — re-derive the harness's belief from the engine.** Answers *blocked by the
  harness*. The stale belief MUST be **replaced by a value freshly observed in a separate command** —
  not cleared, not reset to a default, and not re-derived from the state that produced it. Clearing a
  flag makes the symptom go away without establishing what is true.
- **FR-032**: **Rung 3 — re-establish the path to the game.** Answers *the game is unreachable*,
  selected by the cause established under FR-012: resolve lock ownership (FR-013), wait out a prior
  close's refusal tail, or construct a new connection. A lock whose owner is live MUST NEVER be
  force-taken.
- **FR-033**: **Rung 4 — reload the run's last named save and replay the interrupted turn**, under
  spec 002 FR-045–FR-047: the abandoned attempt is retained and the replayed attempt is
  authoritative. Its precondition is that the game is in a context that accepts a load, and that
  precondition MUST be named for what actually governs it. The measured distinction is **a fresh
  main menu versus a reused one** — a load from a freshly launched menu works; the three refusals
  recorded on this project were all on a menu reached by exiting a game or by losing one. "Post-
  defeat" is a correlate, not the condition, and a rung whose precondition is named for the
  correlate will fire in the wrong place. Where the precondition does not hold, the rung MUST be
  skipped under FR-035 — **not attempted and retried**: a recovery that re-issues a call which cannot
  succeed burns its whole attempt limit and converts a recoverable stall into a failed run, which is
  exactly what has been observed.
- **FR-034**: **Rung 5 — restart the game client.** This rung **depends on a client lifecycle
  capability that does not exist on any platform today**; there is no method anywhere in the harness
  that launches, restarts, or terminates the Civilization VI client or the tuner. Until that
  capability exists the rung MUST be declared unavailable, and the harness MUST stop at rung 4 in a
  recorded failed state rather than present a ladder whose top it cannot climb.
- **FR-035**: Escalation MUST be monotonic within one stall: rungs are attempted in order; a rung is
  skipped only when its precondition is **observably unsatisfiable**, with the skip and its reason
  recorded; and the ladder MUST NOT cycle. A stall that recurs identically after a successful rung
  MUST escalate rather than repeat.
- **FR-036**: Ending the client process is destructive and MUST be gated on a positive observation
  that nothing in flight would be lost. An empty post-defeat menu qualifies; a board mid-turn does
  not.
- **FR-037**: Recovery MUST be bounded (spec 002 FR-048). After the configured number of consecutive
  failed attempts the run MUST stop in a recorded failed state naming its last-known good save and
  the highest rung reached, rather than retrying indefinitely.
- **FR-038**: After any successful recovery the run MUST resume from a freshly assembled observation
  (spec 002 FR-046) and MUST NOT act on any state read before the stall.
- **FR-039**: The harness MUST report, at run preparation, which rungs are available on this host —
  following the capability-refusal pattern of spec 002 FR-054 — so a run never escalates toward a
  rung this host cannot perform.

**Recording**

- **FR-040**: Every detection, classification, rung attempt, rung skip, success, failure, divergence
  incident, and human override MUST be a run event on the run timeline, persisted to the match store
  before the next action is taken (Principle III).
- **FR-041**: A recovery attempt's record MUST name: the disposition and cause it answered; the
  precondition observation that permitted it; the action taken; the independent verification of its
  outcome; and its duration.
- **FR-042**: Wall-clock lost to a stall and to its recovery MUST be recorded and attributed
  separately from time a turn legitimately spent thinking, so "how much time did freezes cost" is
  answerable from the record alone.
- **FR-043**: Model spend discarded by a recovery — a paid call whose result a reload or replay threw
  away — MUST be recorded and attributable to the stall that caused it.
- **FR-044**: A run interrupted by a fault MUST come to rest in a state that carries a recorded
  reason and is surfaced to the operator. It MUST NOT come to rest in a state that asserts the run
  is still progressing — a run left in `playing` after its client died is a lie the record tells,
  and it is how a stranded run went unnoticed for hours. A non-terminal resting state such as
  `paused` is acceptable **only** where the reason is recorded with it; coming to rest with no
  reason at all is not.
- **FR-045**: Fault handling MUST be keyed on the **condition** — the game is unreachable, the socket
  is dead, the board is held — and not on a chosen error or exception family. A handler that a fault
  can bypass by arriving as a different error type is not handling that fault. One instance of this
  has already been found and closed on this project — purpose-built pre-save fault handling had never
  once run, because a dead socket raises a system error while every handler was keyed on the
  harness's own error family — and this requirement generalizes it: each fault MUST be proven against
  the fault **as it actually arrives**, not as the handler expects it to.

**Human parity and the boundary**

- **FR-046**: Every recovery action that touches the game MUST be one a human player could perform
  through the standard game UI, and MUST be registered in the action catalog with its parity basis
  (spec 002 FR-017). Pressing Escape qualifies. A debug call does not, however convenient.
- **FR-047**: A recovery MUST NOT make a game decision on the agent's behalf. A blocking view that
  carries no player choice MAY be dismissed by the harness; a view that carries a choice — including
  one presented as a single acknowledging button — MUST be routed to the agent as a declared prompt
  and answered as a recorded decision (spec 002 FR-010). The recovery layer MUST NOT become a second,
  undeclared player.
- **FR-048**: Reads performed for detection, classification, and reconciliation are harness
  diagnostics. They MUST NOT enter the playing agent's context, and a stall, its classification, and
  its recovery MUST NOT be presented to the agent as game information (spec 002 FR-019, FR-020).
  Where a diagnostic read is also wanted as an agent observation it MUST be declared in the
  observation catalog like any other.
- **FR-049**: Recovery MUST NOT introduce a control path that bypasses the declared catalogs. Where a
  recovery action is performed by a bespoke path rather than through Firetuner — synthetic keyboard
  input, for instance — it MUST record what it does and the documented Firetuner gap that justifies
  it (Principle II, spec 002 FR-028), and the registry of FR-018 MUST record which views that path
  can and cannot reach on this host. The post-defeat exit-confirm modal ignores synthetic input, and
  that is a precondition to be observed, not a limitation to be discovered mid-recovery. On host
  configurations where synthetic input is unavailable altogether — a Wayland session blocks it by
  design — the dismissal rung MUST be reported unavailable at preparation under FR-039 rather than
  attempted and found wanting mid-stall.

**Probes, bounds, and the capability surface**

- **FR-050**: Every engine probe used for classification MUST be bounded and MUST NOT wedge the run
  it is watching. Where the harness and the probe share a single connection to the game, the probe
  MUST NOT consume the run's only channel.
- **FR-051**: A probe's own failure is evidence for *the game is unreachable* and MUST NEVER be read
  as the game's answer, and so MUST NEVER produce a *blocked by the game* disposition.
- **FR-052**: Detection MUST be cheap enough not to cause the faults it looks for, and MUST record
  its own load so that a stall correlated with monitoring is identifiable as such. Machine contention
  has already produced `Errno 111` against a live, listening client and cost a paid live run 90
  seconds into its second leg — the project's own account of it is *measuring through a channel the
  measurement perturbs*. A monitor that adds contention is a cause, not an observer, and a
  classification that cannot rule itself out as the cause is *undetermined* under FR-015.
- **FR-053**: Where an engine call required for classification does not exist on the running build —
  as the session-active query does not on this one — its absence MUST be detected at run preparation
  and MUST NEVER be treated as a negative answer. A missing method means "unknown", not "no".

**Proving the feature**

- **FR-054**: Every detector MUST be proven against an **injected instance of the real failure** — a
  reproduction of an observed incident — and MUST NOT be accepted on the strength of a mock of the
  harness state the failure would have produced. The three observed classes are the minimum fixture
  set.
- **FR-055**: Every detector MUST have a **negative control**: a healthy run against which the
  detector is demonstrated not to fire. The control set MUST include, at minimum, a run exhibiting
  the same external symptom as the failure — a legitimately long model call, a legitimately long
  turn — and, for every reconciliation check, **a correct system that a naive implementation of that
  check would report as diverged**. The same-command readback of FR-025 is the worked example: it
  produced a retracted finding against an engine call that was working correctly, and a detector that
  would repeat it MUST be caught by its own negative control rather than in the ledger afterwards. A
  detector without a demonstrated negative control MUST NOT be accepted.
- **FR-056**: Every recovery rung MUST be proven end to end against an injected instance of the class
  it answers, and MUST additionally be proven **not** to fire on the classes it does not answer.
- **FR-057**: Every acceptance check for this feature MUST be demonstrated failing against a
  deliberately broken build before it is accepted as passing. A check that cannot be made to fail is
  not a check, and MUST NOT count toward any success criterion here.
- **FR-058**: Every registered detector and every rung MUST be shown, by a structural check that runs
  per release, to be reachable from at least one production path. A mechanism exercised only by tests
  MUST be a release-blocking finding — this project's characteristic defect is a well-built mechanism
  that is never reached in the case that matters, with the suite green throughout.
- **FR-059**: A detector or rung MUST NOT be constructible into a state where it runs and never
  fires. Inputs a detector requires MUST have no defaults that render it inert, and a missing input
  MUST be a startup failure rather than a silent no-op.

### Key Entities *(include if feature involves data)*

- **Progress Signal**: One directly observed event that establishes something about a run's progress
  — what it observes, its source of truth, how often it can change, what its absence establishes,
  and what its presence does not. The last clause is part of the entity, not documentation of it.
- **Signal Observation**: One reading of one signal at one moment on one run, including the
  "unavailable" reading, which is distinct from "no progress".
- **Stall Candidate**: A run that has stopped producing observable progress. A candidate, not a
  verdict — it exists so that the thing which triggers classification is separate from the thing
  which concludes it.
- **Stall Classification**: The disposition reached for one stall candidate — live, blocked by the
  game, blocked by the harness, unreachable, or undetermined — with its sub-classified cause where
  it has one, the signals and independent reads it rested on, and the time it took to reach.
- **Blocking View Descriptor**: A registered game view that can hold the board — how it is
  recognised, whether it carries a player choice, the human action that dismisses or answers it,
  whether that action is reachable by the harness's input path on this host, and the independent
  read that confirms it closed.
- **Recovery Rung**: One level of the ladder — the disposition and cause it answers, its observable
  precondition, its cost and what it destroys, its independent verification, and whether it is
  available on this host.
- **Recovery Attempt**: One firing (or recorded skip) of one rung against one stall — what permitted
  it, what it did, what an independent read then saw, how long it took, whether it was autonomous or
  human-initiated, and the wall-clock and model spend it consumed or discarded.
- **Reconciliation Check**: One comparison of a harness assertion against an independent engine read
  at a defined checkpoint — what was compared, both sides verbatim, and whether they agreed.
- **Divergence Incident**: A recorded disagreement between the harness's record and the engine, or
  between two artifacts of one run — carrying both sides, whether play stopped, and its resolution
  status. Never a correction to an existing record.
- **Client Lifecycle Capability**: The ability to start, restart, or terminate the game client on a
  given host. Modelled as an entity because it **does not exist on any platform today** and rung 5
  depends on it; its absence must be a recorded fact about a host, not an assumption.
- **Run Lock Claim**: A claim on a client or run identity, with the identity of its owner and the
  evidence by which that owner's liveness was determined. Age is deliberately not an attribute that
  may justify reclaiming.
- **Failure Fixture**: A reproduction of a real observed incident, usable to fire one detector or one
  rung, paired with the **negative control** — a healthy run wearing the same external symptom —
  that the same detector must not fire on. The pair is one entity because neither is acceptable
  alone.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Across a set of at least 20 unattended runs, **zero healthy runs are interrupted by
  this feature** — zero recovery actions fire against a run that was in fact making progress. The set
  MUST contain at least 20 model calls that individually exceed the run's stall threshold, so the
  criterion is exercised rather than vacuously satisfied. Any finding blocks release.
- **SC-002**: No stall goes unclassified for more than 10 minutes after the last observable progress
  signal, and no classification is produced before the run's own measured quiet-interval distribution
  has been exceeded. Measured against the incidents this feature was written for, which ran for hours
  with no record. This budget is **not** in tension with spec 002's SC-010, which gives crash
  detection 60 seconds: a dead client is directly observable and must be caught fast, while a stall
  is an *inference* over signals dominated by ~97-second turns and ~85%-model wall-clock, and must
  not be rushed. Two different questions, two different budgets, and the record must say which one it
  answered.
- **SC-003**: 100% of stalls receive a named disposition before any rung above report-only fires, and
  zero rungs above report-only fire under an *undetermined* disposition.
- **SC-004**: Each of the three blocked classes has both an injected-real-failure fixture that its
  detector fires on and a negative control that it does not fire on; 100% coverage. A class missing
  either half blocks release.
- **SC-005**: On the reproduced `has_blocking_prompt` incident, the divergence between the harness's
  belief and the engine's availability answer is detected **within one refused action**, not after
  eight; and on a run where belief and engine agree, the class-2 detector fires zero times regardless
  of elapsed time.
- **SC-006**: Zero rungs fire without their precondition recorded as positively observed.
- **SC-007**: Zero recovery actions are performed that a human player could not perform through the
  standard game UI — audited per release, and any finding blocks release.
- **SC-008**: Zero recovery outcomes are taken from the return value of the call that performed the
  action; 100% are verified by an independent re-read. Proven against the engine close call that
  returns success and does nothing.
- **SC-009**: On the reproduced stranded diplomacy view — the one that sat frozen for hours — the
  registered human dismissal returns the board to an interactive state, confirmed by an independent
  read, in 100% of reproductions, and the run resumes from a fresh observation.
- **SC-010**: Zero blocking views carrying a player choice are dismissed by the recovery layer; 100%
  are routed to the agent as declared prompts and answered as recorded decisions.
- **SC-011**: 100% of *unreachable* classifications resolve to a specific cause before a rung fires,
  and both causes observed behind the single connection-refused symptom — a lock held by another run,
  and a connection inside a prior close's refusal tail — are distinguished on reproduction.
- **SC-012**: Zero run locks are reclaimed from a live owner, and 100% of locks reclaimed are
  reclaimed on positive evidence that the owner is gone. Proven against both a lock leaked by a
  finished run and a lock held by a running one.
- **SC-013**: Zero runs end in a non-terminal lifecycle state without a stop reason. Proven by
  reproducing a client death that surfaces as a bare transport error and confirming the run reaches a
  terminal state with a reason.
- **SC-014**: For any run, the wall-clock and model spend lost to stalls and recoveries are readable
  from the record, separately from time legitimately spent thinking, without reading logs — and the
  same figures are summable **across** runs. Today the project records per-incident costs (a defeat
  recovery at ~17 minutes of human intervention; $0.464 burned on one hung driver; a paid live run
  lost 90 seconds into its second leg to machine contention) and **no aggregate at all**, so "how
  much have freezes cost us" is currently unanswerable from the record. This criterion is met when
  it is answerable.
- **SC-015**: Zero existing turn records are amended, rewritten, or deleted by this feature; 100% of
  reconciliation corrections are additive records naming what they contradict. Any finding blocks
  release.
- **SC-016**: 100% of detected divergences are recorded as incidents whether or not play stopped, and
  all three observed instances — the refused end-turn, the turn-advance denial where the engine went
  56 → 57 while the record said nothing advanced, and the three-artifact goal-run disagreement — are
  each reproduced and each detected.
- **SC-017**: Every acceptance check for this feature has been demonstrated failing against a
  deliberately broken build; zero checks in the feature's suite pass unconditionally. A check that
  cannot be made to fail does not count toward any criterion here.
- **SC-018**: Every registered detector and every rung is shown by a per-release structural check to
  be reachable from at least one production path; a mechanism exercised only by tests is a
  release-blocking finding.
- **SC-019**: Zero detectors or rungs can be constructed into an inert state: for each, constructing
  it without a required input fails rather than producing something that runs and never fires.
- **SC-020**: Zero stall detections, classifications, recovery attempts, or divergence incidents
  appear in the playing agent's context — audited per release, and any finding blocks release.
- **SC-021**: 100% of unrecoverable stalls stop within the configured bound in a recorded failed state
  naming the last-known good save and the highest rung reached, with zero indefinite retry loops and
  zero ladder cycles observed.
- **SC-022**: On a host lacking the client lifecycle capability, the run reports at preparation which
  rungs are unavailable, and zero escalations are attempted toward an unavailable rung.
- **SC-023**: Every registered signal states what it observes and what its presence does not
  establish, and zero signals are named for a condition they do not directly observe — audited per
  release.
- **SC-024**: 100% of recovered runs resume from an observation assembled after the recovery; zero
  resume on state read before the stall.
- **SC-025**: Across at least 20 unattended runs, no run spends more than 30 minutes in a stalled
  state without either recovering or stopping in a recorded terminal state. The incidents this
  feature answers ran for hours.

## Assumptions

- The three classes — the game is blocking, the harness blocked itself, a healthy game is unreachable
  — are treated as exhaustive for the failures observed to date, plus *live* and *undetermined* as
  dispositions. A stall that fits none of them resolves to *undetermined*, which is a recorded
  outcome rather than a gap, and new classes are added by observation rather than by anticipation.
- Model-call duration being roughly 85% of a turn's wall-clock is taken from the project's own
  measurements and is treated as a property of this workload, not a constant. The requirement that
  thresholds be derived per run (FR-004) is what keeps the design correct if that share changes.
- The harness holds a single connection to the game. Probing shares that constraint, which is why
  FR-050 exists; a design that assumed a second channel would be cheaper and is not available here.
- Recovery acts on behalf of the harness, not the agent. The agent is never told it was stuck, never
  gains a recovery action, and never has a game decision made for it. This is read as the correct
  application of Principle I rather than a restriction added by this feature.
- Escape and equivalent keypresses are treated as human-parity actions with a parity basis in the
  standard UI. They are delivered by a bespoke input path rather than through Firetuner, which
  Principle II permits as a documented gap and which spec 002 FR-028 already governs; this feature
  adds the requirement that the path's reach be recorded per view rather than assumed.
- Save reload and turn replay reuse deliverable 2's existing discipline — the per-turn quicksave of
  spec 002 FR-007 and the abandoned/authoritative attempt pattern of FR-047. This feature selects
  when to invoke them; it does not redefine them.
- The match-tracking store (deliverable 3) owns the persistence of run events and incidents; this
  feature writes through its interface and relies on its no-delete/no-edit floor rather than
  restating it.
- The unified web interface (deliverable 1) is where a stall, a classification, and a divergence
  incident become visible to the researcher; this feature produces those records and presents nothing
  itself beyond operator control and diagnostics, consistent with spec 002 FR-053.
- Provider-side failures — a model call that hangs, rate-limits, or returns nothing usable — remain
  deliverable 2's concern under spec 002 FR-041 and FR-042. This feature must distinguish them from
  game-side stalls and must not duplicate their handling.
- The detection ceiling of 10 minutes (SC-002) and the stalled-time bound of 30 minutes (SC-025) are
  chosen against the observed cost — freezes measured in hours — and against the asymmetry in FR-016.
  They are deliberately loose: a tighter ceiling buys little and moves the design toward the failure
  the owner explicitly named.
- Reproducing the observed incidents requires a live client, so the fixtures of FR-054 are expected to
  be a mix of live-marked reproductions and faithful injections at the seam the real failure crosses.
  An injection at a seam the real failure never crosses does not satisfy FR-054.

## Dependencies

- **A client lifecycle capability does not exist.** No method anywhere in the harness launches,
  restarts, or terminates the Civilization VI client or the tuner, on any platform. Rung 5 (FR-034)
  depends on it and is declared unavailable until it exists. This is stated as a dependency rather
  than assumed, because Principle VII names "restart the run" as a requirement and that clause is
  currently **unimplementable**, not merely unimplemented.
- **A session-active query does not exist on this build**, and the corresponding close call returns
  success without acting. Classification of a stranded full-screen view therefore cannot be settled
  by asking the engine whether the session is open, and FR-053 requires the absence to be detected
  rather than read as "no".
- **An existing detection and recovery layer is the substrate, not a competitor.** Process liveness,
  tuner heartbeat, per-operation bounds, screen identity, the single reload-the-start-quicksave
  recovery, and the no-progress turn backstop already exist and are wired. This feature's
  dispositions are expected to be derived from those signals rather than to replace them, and its
  ladder is expected to have that existing recovery as one rung rather than to re-implement it. What
  this feature adds is the classification those signals do not currently produce, the rungs above and
  below the one that exists, and the reconciliation check that has no counterpart today.
- **A per-view dismissal path exists but is not a general capability.** Synthetic keyboard and mouse
  input is implemented per host, but it is reachable only from two hard-wired call sites — completing
  a popup acknowledgement that the engine's own click callback was measured not to perform, and
  pressing Escape to clear the post-load leader-intro screen. There is no declared action that sends
  a dismissal to an arbitrary stuck view, so FR-030 depends on one being declared with a parity
  basis. That the popup path exists *because* the engine's own callback does nothing is the second
  independent instance of FR-025's problem on this build.
- **Spec 002** owns the turn cycle, the quicksave, the observation and action catalogs, the provider
  layer, and the abandoned/authoritative replay pattern this feature invokes. Its FR-044–FR-050 and
  SC-010/SC-021 describe the behaviour this feature makes achievable; they are expected to be read
  against this spec rather than duplicated by it.
- **Spec 003** owns the store this feature's events and incidents are written to, and the
  no-delete/no-edit floor that FR-022 and FR-023 rely on.
- **Spec 001** owns the surface where a stall and a divergence incident become visible to a human.
