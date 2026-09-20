# Feature Specification: Civilization-Playing Harness

**Feature Branch**: `002-civ-playing-harness`

**Created**: 2026-09-19

**Status**: Draft

**Input**: User description: "feature 2"

**Scope anchor**: Deliverable (2) of the constitution's Scope & Initial Deliverables — "the
Civilization-playing harness described by Principles I, II, IV, and VII."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Play an unattended run from a fixed seed to a stop condition (Priority: P1)

The researcher defines a run — map seed, civilization, ruleset, difficulty, stop condition, and which
model the agent uses — starts it, and walks away. The harness brings Civilization VI to the
configured starting position and then plays it turn after turn on its own: it looks at what a human
player would see, decides what to do and why, carries that decision out in the game, looks again at
what changed, decides again, answers whatever the game asks of it along the way, ends the turn when
the agent says it is done, and records everything before moving on.
The researcher returns to a finished run with a complete turn-by-turn record and no memory of having
touched the game client.

**Why this priority**: This is the deliverable. Everything else in the project — the interface, the
match store, the optimization layer — has nothing to display, store, or optimize until a run can play
itself end to end and record what it did. A single unattended run that reaches its stop condition
with a complete record is independently valuable on the day it first works.

**Independent Test**: Configure a run on a known seed with a turn-50 stop condition, start it, and do
not touch the keyboard or the game client until it stops. Verify it reached turn 50, that every turn
from 1 to 50 has a persisted record containing the state observed, the decisions made, the reasoning
given, and the resulting yields, and that no turn is missing. Then repeat with the stop condition set
to a game outcome and verify the run plays across era transitions to victory or defeat.

**Acceptance Scenarios**:

1. **Given** a complete run configuration, **When** the researcher starts the run, **Then** the
   harness brings the game to exactly that seed, civilization, ruleset, and settings, and begins
   playing without further human input.
2. **Given** a run configuration that cannot be applied exactly — seed unavailable, ruleset or mod
   set not active, setting unsupported — **When** the harness prepares the run, **Then** it fails the
   run before turn 1 with the mismatch recorded, rather than starting an approximate run.
3. **Given** a run is playing, **When** a turn begins, **Then** the harness takes a named quicksave
   before observing or acting on anything in that turn.
4. **Given** a turn is in progress, **When** the agent issues its decisions, **Then** each decision is
   carried out in the game, its effect is verified against game state, and the action is recorded as
   applied, rejected, or partially applied — never as applied without verification.
5. **Given** the game itself asks something of the player mid-turn — a unit promotion, a pantheon or
   religion choice, a great person selection, an AI diplomatic approach, a declaration of war, a
   World Congress vote, an era transition — **When** that prompt appears, **Then** the agent decides
   the response through a declared catalog entry and the decision is recorded like any other.
6. **Given** a turn's decisions have been executed, **When** the harness is ready to end the turn,
   **Then** the complete record for that turn is persisted before the turn is ended, and the run
   halts rather than advancing if persistence fails.
7. **Given** a run is playing, **When** the configured stop condition is met — turn reached, victory,
   defeat, or operator stop — **Then** the run ends with exactly that stop condition recorded.
8. **Given** a run is playing, **When** the researcher issues a lifecycle command such as pause or
   stop, **Then** the harness honours it without the researcher interacting with the game client.
9. **Given** a turn completes, **When** its record is written, **Then** a screen capture for that
   turn is recorded alongside it, or the capture is explicitly marked unavailable.
10. **Given** a run configured to play to a game outcome, **When** it crosses from one era to the
    next, **Then** it continues playing through the mechanics that era introduces rather than
    stalling on newly available systems.
11. **Given** a turn is in progress, **When** the agent's decision has been executed and verified,
    **Then** the agent is shown a freshly observed board reflecting that decision's effect before it
    is asked for its next one, and the turn ends only when the agent issues an end-turn decision or
    the no-progress backstop trips.

---

### User Story 2 - Prove the agent played within human parity (Priority: P2)

The researcher needs to be able to answer, for any run, "could a human have seen this, and could a
human have done this?" — not by trusting the harness, but by reading it off the record. Every kind of
information the agent can be given and every kind of action it can take is declared up front with the
in-client action a human would take to obtain or perform it, and each run carries the version of
those declarations that was in force while it played. Anything not declared never reaches the agent —
and because the agent is shown the game's own screen as well as structured state, the images it sees
are held to the same standard as the data it reads.

**Why this priority**: Constitution Principle I is non-negotiable and the project's entire research
value rests on it, but a boundary that cannot be audited is a boundary nobody can rely on. Until the
declarations and the per-run trace exist, no run's data is admissible as evidence of human-equivalent
play — which makes this the slice that turns Story 1's output from "a game was played" into "a result
that counts." It is a distinct, buildable surface on top of the loop, so it follows it.

**Independent Test**: Take one completed run and, working only from its record, enumerate every
distinct observation the agent received — structured and visual — and every distinct action it
issued; verify each one resolves to a declared parity basis, that every image it saw was screened,
and that no observation or action in the run lacks a declaration.

**Acceptance Scenarios**:

1. **Given** the harness is assembling a turn's context, **When** the game exposes a value that is not
   in the observation catalog, **Then** that value does not reach the agent's context, regardless of
   whether it was technically retrievable.
2. **Given** the agent requests an action, **When** that action is not in the action catalog or is not
   available to a human in the current game context, **Then** the harness rejects it, records the
   rejection with its reason, and does not perform it.
3. **Given** a run is being prepared, **When** any observation or action it could use lacks a declared
   parity basis, **Then** the run does not start.
4. **Given** a completed run, **When** the researcher audits it, **Then** the run's record identifies
   the exact version of the observation and action catalogs that were in force for it.
5. **Given** any turn of any run, **When** the agent's context for that turn is examined, **Then** it
   contains no unrevealed map contents, no opponent internal state, no hidden AI intent, no random
   number generator state, and no debug or provenance data.
6. **Given** an image is about to enter the agent's context, **When** it is screened, **Then** an
   image showing the Firetuner window, a developer console, a debug overlay, or any harness UI is
   withheld and never reaches the agent.
7. **Given** an image is about to enter the agent's context, **When** the camera position, zoom, or
   view mode that produced it is checked, **Then** it is one a human player could have reached
   through the standard UI, and the camera movement that produced it was itself a declared action.
8. **Given** the agent's context for a turn, **When** it is examined for harness telemetry, **Then**
   model identity, cost, latency, retries, save lineage, and run configuration are absent from it as
   game information.
9. **Given** a capability that Firetuner cannot provide, **When** a bespoke implementation is used for
   it, **Then** the record states what it does, what it reads or writes, why Firetuner could not do
   it, and its parity basis — under the same catalog rules as every other capability.

---

### User Story 3 - Keep a long run alive across crashes, stalls, and provider failures (Priority: P3)

Civilization VI will crash, hang, or present an unexpected screen; the model provider will rate-limit,
time out, or return nothing usable. The researcher expects the harness to notice, preserve what it
has, recover from the last good save, and carry on as the same run — or, when it genuinely cannot,
stop in a clearly recorded failed state. What it must never do is quietly skip a turn, invent an
action, or keep playing from a stale picture of the board.

**Why this priority**: Short runs can be babysat; a full game across every era cannot, and neither can
the many-run workload deliverable 4 will generate. This is what makes the harness usable unattended
in volume rather than only in demos. It builds directly on Story 1's loop and save discipline.

**Independent Test**: Start a run, kill the game client mid-turn, and verify the harness detects the
crash, records it, resumes from that turn's quicksave as the same run, re-observes before acting, and
completes the run — with both the abandoned attempt and the replayed turn present in the record.

**Acceptance Scenarios**:

1. **Given** a run is playing, **When** the game client crashes, hangs, or stops responding, **Then**
   the harness detects it within a bounded time and records it as a run event.
2. **Given** a crash has been detected, **When** the harness recovers, **Then** it resumes from that
   turn's quicksave as the same continuous run, with the interruption and resume point in sequence.
3. **Given** the harness has resumed after an interruption, **When** it acts again, **Then** it
   re-observes the game state first and never acts on observations or images taken before the
   interruption.
4. **Given** a turn was interrupted and replayed, **When** its record is read, **Then** both the
   abandoned attempt and the replayed attempt are present, with the replayed one marked authoritative.
5. **Given** a model call fails or is rate-limited, **When** the harness retries or falls back to a
   configured alternate, **Then** the failure, the retries, and the fallback are recorded as run
   events and the model that actually served the call is recorded.
6. **Given** every configured model option has been exhausted for a turn, **When** the harness cannot
   obtain a decision, **Then** it pauses the run in a recorded state and does not fabricate an action,
   skip the turn, or substitute a default move as if it were an agent decision.
7. **Given** repeated recovery attempts keep failing, **When** the configured limit is reached,
   **Then** the harness stops in a recorded failed state with the last-known good save identified,
   rather than looping indefinitely.
8. **Given** a run was interrupted, **When** the researcher inspects its record, **Then** no turn is
   silently missing — every gap is either recovered or explicitly marked, and the run's
   record-completeness status reflects it.
9. **Given** a turn's image cannot be produced or is withheld after bounded retries, **When** the turn
   proceeds, **Then** it is recorded as visually degraded and the run's comparability status reflects
   it, rather than the change in the agent's perception passing unrecorded.

---

### User Story 4 - Branch and replay a run from any turn (Priority: P4)

The researcher wants to ask "what if the agent had done something else on turn 23." They pick a
recorded turn of a recorded run, start a new run from that turn's save, and let it play forward under
different conditions — a different model, different guidance, or simply a different sample. The new
run knows what it branched from, and the original is untouched.

**Why this priority**: This is the mechanism deliverable 4's Monte Carlo and ablation work is built
on, and Principle IV requires the save discipline that makes it possible. It has no value until runs
exist to branch from and recovery is reliable, so it comes after those, but it must be designed into
the save model from the start rather than bolted on.

**Independent Test**: Take a completed run, branch from its turn 23 save twice under two different
model configurations, play both to turn 30, and verify each branch records its parent run and turn,
both diverge from the identical starting position, and the parent run's record is unchanged.

**Acceptance Scenarios**:

1. **Given** a recorded run, **When** the researcher starts a run from any of its turn-start saves,
   **Then** the new run begins from exactly that position and records the parent run and turn as its
   lineage.
2. **Given** a branch is playing, **When** it records its turns, **Then** the parent run's record is
   neither modified nor invalidated.
3. **Given** two branches from the same save point, **When** both are started, **Then** both begin
   from an identical game position.
4. **Given** a branch is abandoned or rolled back, **When** that happens, **Then** it is recorded as
   an event and its turns are marked superseded rather than deleted.
5. **Given** a run the operator has not archived, **When** save retention runs, **Then** its
   turn-start saves are not removed, however old the run is; only an archived run's saves become
   eligible.
6. **Given** any save point, **When** the researcher refers to it, **Then** it is addressable by run,
   turn, and lineage without inspecting the filesystem or the game client.

---

### User Story 5 - Swap the agent's model without touching the harness (Priority: P5)

The researcher wants to run the same seed under a cheaper model, a stronger model, or a different
vendor entirely, and compare. Changing which model plays is a change to the run's configuration and
nothing else — no code change, no new integration, no rewiring of how the agent sees or acts on the
game.

**Why this priority**: Cost-optimized experimentation across a seed set is a stated project goal and
Principle VII makes the pluggable provider layer a requirement rather than a convenience. It is the
last slice to pay off because it needs several completed runs before a comparison means anything, but
the provider layer it depends on is already required by Story 3's fallback behaviour.

**Independent Test**: Run the same seed and configuration twice, changing only the model identifier in
the run configuration, with no code or integration change between the two; verify both complete and
both records name the model that actually served each call.

**Acceptance Scenarios**:

1. **Given** two run configurations differing only by model, **When** both are run, **Then** both play
   through the same observation and action surface with no code change between them.
2. **Given** a run configuration naming a primary model and its fallbacks, **When** the run is
   prepared, **Then** it does not start unless every model in that chain can accept the agent's full
   context, images included.
3. **Given** any model call, **When** its record is read, **Then** it names the model that actually
   served it, its latency, its cost, its retry count, and any fallback that occurred.
4. **Given** a run fell back to an alternate model mid-run, **When** its record is read, **Then** the
   turns served by each model are distinguishable.
5. **Given** any record, capture, or agent context, **When** it is inspected, **Then** no provider
   credential or API key appears in it.

---

### Edge Cases

- The configured seed, ruleset, or mod set is unavailable or has changed version since the seed set
  was defined — the run must refuse to start rather than play a silently different game.
- Civilization VI patches itself between runs of the same seed set — the run refuses to start on the
  new build until an operator explicitly accepts the change for that set, and every run played under
  the accepted build carries that fact, so a set spanning two builds never reads as uniform.
- The game presents a prompt the harness has no declared handling for — a new popup, an unrecognised
  dialog, an era-transition screen it does not know — it must stall visibly rather than click blindly
  or dismiss the prompt.
- A game-initiated interrupt arrives between turns rather than within one — an AI declaring war, a
  city-state quest resolving, a World Congress session opening — and must be handled as a recorded
  decision rather than absorbed silently by turn advancement.
- The agent issues an action that is legal in general but illegal right now (moving a unit that has
  no movement left, building something already built) — rejected and recorded, without ending the
  turn in an inconsistent state.
- The agent issues no decision at all for a turn, or returns malformed output — the turn is not ended
  on a fabricated action; the harness retries within bounds and then pauses.
- The agent undoes or cancels its own earlier work later in the same turn, having seen the result —
  each step is executed and recorded as issued, and the resulting state is what is recorded, not the
  intent.
- The agent never issues an end-turn decision and keeps acting indefinitely — as long as its
  decisions keep changing the game state the turn continues, however long it takes; the no-progress
  backstop ends it only once it stops accomplishing anything.
- The agent's observation cannot be assembled part-way through a turn, after some decisions have
  already been executed and verified — the turn cannot continue on a stale board, so it is
  interrupted and replayed from its start quicksave rather than finished from the last good view.
- The agent asks to move the camera somewhere a human could not put it, or to see a tile it has not
  revealed — the camera request is rejected like any other out-of-parity action.
- An image is captured successfully but screening finds non-player UI in it — it is withheld from both
  the agent and storage, and the harness re-captures rather than passing it through.
- Images cannot be produced at all for a stretch of turns — those turns are recorded as visually
  degraded rather than silently played on structured state alone as if nothing changed.
- The game client crashes between executing an action and persisting the turn — recovery must not
  leave a half-written turn presented as complete.
- The match-tracking store is unreachable or rejects a write — the run halts rather than playing on
  into an unrecorded stretch.
- A quicksave fails to write at the start of a turn — the turn does not proceed, since recovery and
  branching both depend on it.
- The model provider responds very slowly — the turn simply takes longer, since no wall-clock bound
  ends a turn; the provider call's own timeout and retry behaviour is what handles a call that never
  returns.
- The provider returns a successful response containing no usable decision — treated as a failed call,
  not as a decision to do nothing.
- Every model in the configured chain rejects the context — for size, for image count, or for
  capability — the run pauses rather than quietly dropping the images to make the call fit.
- A late-game turn carries many units, cities, and decisions, running to hundreds of decision steps
  and hours of wall-clock time — it is allowed to, and must not be truncated; cost and duration are
  recorded and left visible rather than capped.
- A full game runs long enough that per-turn saves and per-step captures accumulate substantially —
  since nothing is deleted without an explicit archival, the harness must report disk headroom and
  halt the run before the disk fills, rather than freeing space by deleting a save nobody archived.
- Disk fills, or an archived run's save is removed while something still needs it — recovery must
  report the missing save rather than resume from a different turn.
- The game auto-advances or ends the turn on its own before the harness intends to — the record must
  reflect what actually happened in the game, not what the harness planned.
- Two harness instances are pointed at the same game client or the same run identity — the second
  must refuse rather than interleave actions into one game.
- The machine the harness runs on cannot provide a capability the run needs — most concretely, no
  working way to take the per-turn quicksave — and the run is refused before turn 1 naming the
  missing capability, rather than started and failed at its first turn.
- A branch is started on a machine whose platform differs from the parent run's — refused by default
  with the mismatch named, since a save is not assumed to resolve identically across builds; an
  operator may accept the change, and the branch then records that it relied on the acceptance.
- A capability is added mid-project that reads new game data — it cannot be used until it is declared
  in the observation catalog, and runs before and after the change must remain distinguishable by
  catalog version.
- The run reaches its stop condition on the same turn as a victory, defeat, or crash — exactly one
  stop condition is recorded, with the others present as events.

## Clarifications

### Session 2026-09-19

- Q: Within a single turn, does the agent decide everything up front from one look at the board, or does the harness show it the updated board after its actions and let it decide again before the turn ends? → A: B — full interactive loop. The harness re-observes and calls the agent again after every executed decision, until the agent issues an end-turn decision.
- Q: When is the harness allowed to delete a finished run's per-turn save files? → A: C — explicit archival only. A run stays branchable until an operator archives it; archiving makes its saves eligible for removal while its records and captures remain. No age-, quota-, or tier-based automatic deletion.
- Q: When a turn runs out of its time budget part-way through the decision loop, what happens to that turn? → A: There is no turn timer. The per-turn time budget is removed entirely; a turn runs as long as the agent needs. The only backstop is a no-progress rule — the turn ends when a configured number of consecutive decisions are rejected or verified as changing nothing in game state, which targets a stuck agent without penalising a long but productive turn.
- Q: For SC-001's "at least 90% of attempts", what counts as a failed attempt? → A: A — every started attempt counts, and any attempt not reaching its stop condition is a failure, including correctly-recorded failed states and losses to external causes. No post-hoc reclassification or exclusion.
- Q: Should a run refuse to start if Civilization VI has patched itself to a different build than the rest of the seed set was played on? → A: C — pin with a recorded override. The seed set records its game build and a differing build fails the run before turn 1 by default, but an operator may explicitly accept the change for that set; every run relying on the acceptance records it, so a set spanning two builds is never mistaken for a uniform one.

### Session 2026-09-20

- Q: Should FR-031's seed-set pin treat the host platform as part of the game-build identity? → A: Yes — composite identity. The recorded build is platform and version together (e.g. `win/1.0.12.9`), so a platform difference fails preflight exactly as a version difference does, and accepting one transition never implicitly accepts the other. The macOS and Linux ports are separately built binaries whose version numbering need not track the Windows build's, so a set spanning two platforms carries the same silent-incomparability hazard as one spanning two versions.
- Q: Should the harness refuse to start when the host machine lacks a capability the harness requires? → A: Refuse before turn 1, naming the missing capability. A host with no working way to take the per-turn quicksave cannot complete any turn under FR-007, so the run is refused at preparation rather than started and failed at turn 1. No separate capability tier is required in the record; FR-050's comparability status already distinguishes a degraded run from a fully capable one.
- Q: Should branching a run onto a different host platform than the parent be allowed? → A: Refused by default, overridable by a recorded operator acceptance — the same mechanism as a build change, which follows from platform being part of build identity. Whether a save written on one platform loads and resolves identically on another is unverified, and FR-034 requires two branches from one save point to begin from an identical position; a branch that diverged for platform reasons would be indistinguishable from one that diverged because the agent played differently.

## Requirements *(mandatory)*

### Functional Requirements

**Run definition and lifecycle**

- **FR-001**: A run MUST be fully defined before it starts by a recorded configuration covering at
  minimum: map seed, civilization and leader, ruleset and active mod set, map and game settings,
  difficulty, opponent composition, stop condition, agent model configuration including its fallback
  chain, the no-progress step limit of FR-014, the recovery attempt limit of FR-048, and the
  out-of-game guidance supplied to the agent.
- **FR-002**: The harness MUST bring the game to the configured starting state, and MUST fail the run
  before turn 1 — recording the mismatch — if any configured element cannot be applied exactly. The
  Civilization VI build the client is running — its **platform and version together** — MUST be
  checked against the build recorded on the run's seed set and treated as such an element: by default
  a differing build fails the run before turn 1. An operator MAY explicitly accept a build change for
  that seed set, which MUST be recorded on every run that relies on it, so a set spanning more than
  one game build is identifiable as such rather than appearing uniform. An acceptance MUST cover one
  specific transition only: accepting a version change MUST NOT implicitly accept a platform change,
  or the reverse.
- **FR-003**: The harness MUST maintain and record an explicit run lifecycle state — preparing,
  playing, waiting on the model, waiting on the game, paused, interrupted, resuming, finished, or
  failed — and MUST record every transition between them.
- **FR-004**: The harness MUST accept operator lifecycle commands — start, pause, resume, stop, resume
  from a save, branch from a save, and archive a run — without requiring the operator to open, focus,
  or interact with the Civilization VI client.
- **FR-005**: Every run MUST end on exactly one recorded stop condition: the configured turn was
  reached, victory, defeat, operator stop, or unrecoverable failure. A run MUST support playing to a
  game outcome, not only to a turn limit.
- **FR-006**: The harness MUST run at most one active run per game client instance, and MUST refuse to
  attach a second run to a client or run identity that is already active.

**Turn cycle**

- **FR-007**: The harness MUST take a named, addressable quicksave at the start of every turn, before
  any observation or action for that turn, in addition to the game's own autosave rotation. A turn
  MUST NOT proceed if its quicksave cannot be written.
- **FR-008**: Each turn MUST be played as a loop of decision steps rather than one batched decision.
  At every step the harness MUST assemble a fresh parity-filtered observation of the current game
  state, obtain the agent's next decision and its stated reasoning, execute it, and verify its
  effect — then assemble a new observation reflecting that effect before asking for the next
  decision. The agent MUST NOT be asked to commit to a later decision before it has seen the result
  of the earlier one. The loop MUST end in exactly one of two ways: the agent issues an end-turn
  decision, which MUST itself be a declared catalog entry recorded like any other decision, or the
  no-progress backstop of FR-014 trips. The harness MUST NOT end a turn on its own initiative while
  the agent is still making progress, and a turn in which the agent ends immediately without acting
  is a valid turn, not a failure. A turn MUST NOT be bounded by wall-clock time: there is no turn
  time budget, and a turn may take as many decision steps and as long as the agent needs.
- **FR-009**: The harness MUST support an unbounded sequence of decision steps within a single turn
  across the game's full decision surface for a complete game: unit movement, orders, and promotions; city
  production, purchasing, and management; research and civics; policies, government, and governors;
  religion from pantheon through founding and religious units; diplomacy, war, peace, and city-state
  relations; espionage; great people; and World Congress participation. Coverage MUST extend across
  every era from the game's start to a victory or defeat outcome, not only the opening phase.
- **FR-010**: The harness MUST treat game-initiated prompts and interrupts that require a player
  response — unit promotions, pantheon and religion selections, great person selections, AI diplomatic
  approaches, declarations of war, city-state quests, World Congress votes, era transitions — as
  decisions the agent makes through declared catalog entries, recorded like any other decision. It
  MUST NOT dismiss, default, or absorb such a prompt without a recorded decision.
- **FR-011**: The harness MUST verify each executed action against resulting game state and record it
  as applied, rejected, or partially applied. It MUST NOT record an action as applied without
  verification.
- **FR-012**: The harness MUST record, for every decision step of every turn, the observation the
  agent was given at that step — structured state and the images it was shown — the decision it
  issued, the reasoning it gave, and the outcome that followed, together with the turn's resulting
  yields. A turn's record MUST preserve the order of its steps, so the sequence the agent actually
  saw and did can be reconstructed.
- **FR-013**: The harness MUST NOT end a turn until that turn's complete record has been persisted to
  the match-tracking store, and MUST halt the run rather than advance if persistence fails.
- **FR-014**: Each turn MUST be bounded by a no-progress backstop rather than by time: the harness
  MUST count consecutive decision steps whose decision was rejected or whose verification showed no
  change to game state, reset that count whenever a decision is verified as having changed game
  state, and end the turn once the count reaches a configured limit. A turn ended this way MUST be
  recorded as ended on no progress rather than as ended by the agent, so a stuck agent is visible in
  the record. A turn MUST NOT be ended for any other harness-initiated reason, and MUST NOT be
  truncated because it has grown long or expensive.
- **FR-015**: The harness MUST record a screen capture of the game's own view for each decision step,
  bound to that run, turn, and step, or explicitly mark the capture unavailable for that step. A
  turn MUST NOT reuse an earlier step's capture in place of a fresh one, since the board the agent
  is shown must reflect the effects of what it has already done this turn.

**Human-parity boundary**

- **FR-016**: Every observable the agent can receive MUST be registered in an observation catalog with
  a declared parity basis — the action a human player takes in the standard game UI to obtain the same
  information. Data not registered MUST NOT reach the agent's context.
- **FR-017**: Every action the agent can take MUST be registered in an action catalog with a declared
  parity basis — the mouse or keyboard path a human player uses to perform it. A requested action that
  is not registered, or that is unavailable to a human in the current game context, MUST be rejected
  and recorded with its reason.
- **FR-018**: Parity filtering MUST be applied structurally by the harness before the agent's context
  is assembled. It MUST NOT be delegated to instructions in the agent's prompt or to the agent's own
  restraint.
- **FR-019**: The agent's context MUST NOT contain unrevealed map contents, opponent internal state,
  hidden AI intent, other civilizations' undisclosed research or civic progress, unit or city data
  beyond what the standard UI reveals at that moment, random number generator state, or any debug or
  provenance data — in structured form or visible in an image — regardless of whether Firetuner or a
  bespoke path made it retrievable.
- **FR-020**: Harness operational telemetry — model identity, cost, latency, retries, save lineage,
  run configuration, and wall-clock timing — MUST be recorded as out-of-game data and MUST NOT be
  presented to the playing agent as game information.
- **FR-021**: Out-of-game strategic guidance MAY be supplied to the agent as context. Such guidance
  MUST be run-independent and MUST NOT contain state from the current run or any hidden state; what
  was supplied MUST be recorded with the run.
- **FR-022**: The observation and action catalogs MUST be versioned, and every run MUST record the
  catalog versions in force while it played, so its boundary is auditable after the fact.
- **FR-023**: A run MUST NOT start if any observation or action available to it lacks a declared
  parity basis.
- **FR-024**: The agent's per-turn context MUST consist of parity-filtered structured game state
  together with rendered images of the game's own view, so that what the agent perceives corresponds
  to what a human player sees on screen. Both the structured entries and the visual views MUST be
  declared in the observation catalog.
- **FR-025**: Every image entering the agent's context MUST be screened for non-player UI before the
  agent sees it. An image containing the Firetuner window, a developer console, a debug overlay, or
  any harness-owned UI MUST be withheld and re-captured, never passed through.
- **FR-026**: The camera position, zoom, and view mode that produce an image the agent sees MUST be
  reachable by a human player through the standard UI, and changing the camera MUST itself be a
  declared action in the action catalog, subject to rejection like any other.

**Integration path**

- **FR-027**: Firetuner MUST be the default path for both observation and action; new harness
  capability MUST be implemented through it unless a specific gap is demonstrated.
- **FR-028**: A bespoke capability — screen or input automation, external tooling, or any other
  control path — MAY be used only to fill a documented Firetuner gap, and MUST record what it does,
  what it reads or writes, why Firetuner could not do it, and its parity basis.
- **FR-029**: Skills added on demand to cover uncovered surface area MUST enter the observation and
  action catalogs with parity declarations before first use, under the same rules as any other
  capability.
- **FR-030**: The Firetuner window, developer console, and any harness-owned UI MUST NOT be visible in
  a stored capture; a capture containing one MUST be withheld rather than stored or displayed.

**Reproducibility, saves, and branching**

- **FR-031**: The harness MUST support executing a named seed set under a consistent civilization,
  ruleset, and Civilization VI build across the whole set, so runs within a set are comparable. The
  seed set MUST record the game build its runs are played on, and any run in the set played on a
  different build MUST be identifiable from the record. The recorded build MUST identify the **host
  platform together with the version**, since the game's platform ports are separately built and
  their version numbering need not correspond; a set whose runs span more than one platform MUST be
  identifiable as such rather than appearing uniform, on the same basis as one spanning more than
  one version.
- **FR-032**: Every save point MUST be addressable by run, turn, and lineage, without inspecting the
  filesystem or the game client.
- **FR-033**: The harness MUST be able to start a new run from any recorded save point, recording the
  parent run and turn as that run's lineage. A branch whose game build — platform or version —
  differs from the parent run's MUST be refused by default with the mismatch recorded, since FR-034
  requires both branches to begin from an identical position and a save is not assumed to resolve
  identically across builds. An operator MAY explicitly accept such a branch under the same recorded
  mechanism as a build change in FR-002, and every branch relying on that acceptance MUST record it.
- **FR-034**: A branch MUST NOT modify or invalidate its parent run's record, and two branches from
  the same save point MUST begin from an identical game position.
- **FR-035**: Abandoning or rolling back a branch MUST be recorded as an event, and the affected turns
  MUST be marked superseded rather than deleted.
- **FR-036**: Save retention MUST NOT remove the turn-start saves of a run that is still resumable or
  branchable; a recovery that finds a required save missing MUST report it rather than resume from a
  different turn. A run MUST be treated as branchable until an operator explicitly archives it:
  archiving is a recorded lifecycle action that marks that run's saves eligible for removal while
  leaving its records and captures intact. The harness MUST NOT make a save eligible on any other
  basis — not age, not a disk quota, not a retention window, and not by thinning a terminal run's
  saves — because each of those deletes a branch point nobody decided to give up.

**Model access**

- **FR-037**: All agent model calls MUST be routed through a pluggable provider layer such as
  OpenRouter; no single-vendor SDK may be a hard dependency of the decision path.
- **FR-038**: Which model plays a run MUST be part of run configuration and changeable without any
  code or integration change.
- **FR-039**: Every model in a run's configured chain — primary and all fallbacks — MUST be able to
  accept the agent's full context including its images. A run MUST NOT start if any model in the chain
  cannot, and the harness MUST NOT drop images from the context to make a call succeed.
- **FR-040**: Every model call MUST record the model that actually served it, its latency, its cost,
  its retry count, and any fallback that occurred, such that the turns served by each model in a run
  are distinguishable.
- **FR-041**: The harness MUST retry transient provider failures with backoff and fall back to
  configured alternates, recording each failure, retry, and fallback as a run event.
- **FR-042**: When no decision can be obtained for a turn, the harness MUST pause the run in a
  recorded state. It MUST NOT fabricate an action, skip the turn, or substitute a default or heuristic
  move recorded as if it were an agent decision.
- **FR-043**: Provider credentials and API keys MUST NOT appear in any run record, capture, log, or
  agent context.

**Resilience**

- **FR-044**: The harness MUST detect game client crashes, hangs, unresponsiveness, and unexpected UI
  states within a bounded time, and record each as a run event.
- **FR-045**: On detecting a crash or hang, the harness MUST preserve the last-known good save and
  state, and either resume from that turn's quicksave as the same continuous run or stop in a recorded
  failed state — never silently continue.
- **FR-046**: After any interruption, the harness MUST re-observe game state and re-capture its images
  before acting, and MUST NOT act on anything obtained before the interruption.
- **FR-047**: A turn that was interrupted and replayed MUST record both the abandoned attempt and the
  replayed attempt, with the replayed attempt marked authoritative.
- **FR-048**: Recovery MUST be bounded; after the configured number of consecutive failed recovery
  attempts the harness MUST stop in a recorded failed state identifying the last-known good save,
  rather than retrying indefinitely.
- **FR-049**: An unexpected game screen or prompt the harness has no declared handling for MUST stall
  the run visibly rather than being dismissed or acted on blindly.
- **FR-050**: When an image required for the agent's context cannot be produced or is withheld after
  bounded retries, the turn MUST be recorded as visually degraded and the run's comparability status
  MUST reflect it; the harness MUST NOT proceed on structured state alone without recording that the
  agent's perception changed.

**Recording boundary**

- **FR-051**: Every turn record, run event, model call, save point reference, and capture MUST be
  written to the match-tracking store; none of it may exist only in local or ephemeral form.
- **FR-052**: Each run MUST carry a record-completeness status reflecting whether its turn-by-turn
  record has gaps, so incomplete runs are identifiable as unfit for trending and optimization input.
- **FR-053**: The harness's operator surface is for lifecycle control and diagnostics only; it MUST
  NOT become a second presentation of run state that could diverge from the unified interface.

**Host environment**

- **FR-054**: The harness MUST verify, before a run starts, that the machine it is running on can
  provide every capability the run requires of it — at minimum a working path to take and verify the
  per-turn quicksave of FR-007. Where a required capability is unavailable, the harness MUST refuse
  the run before turn 1 and record which capability was missing, rather than starting a run that
  FR-007 guarantees cannot complete a single turn. A capability that is merely degraded rather than
  absent — one whose loss is already recorded elsewhere, such as the images of FR-050 — MUST NOT
  refuse the run; it is recorded and the run proceeds.

### Key Entities *(include if feature involves data)*

- **Run Configuration**: The complete, recorded definition of a run before it starts — seed,
  civilization and leader, ruleset and mod set, map and game settings, difficulty, opponents, stop
  condition, model configuration and fallback chain, and guidance set.
- **Run**: One execution of a configuration. Identity, lifecycle state, start and end time, recorded
  stop condition, record-completeness status, comparability status, catalog versions in force, the
  Civilization VI build it played on — platform and version — and whether that relied on an accepted
  build change, whether it has been archived, and lineage to any run and turn it branched from.
- **Turn Cycle**: One turn of one run — the quicksave taken at its start, the ordered sequence of
  decision steps it contains, the resulting yields, how the turn ended (agent end-turn decision or
  the no-progress backstop), whether it is an authoritative or abandoned attempt, and whether it ran
  visually degraded.
- **Decision Step**: One iteration of the within-turn loop — the observation assembled at that point,
  the single decision the agent issued from it, and that decision's execution outcome. Its position
  in the turn is what makes the sequence the agent saw and did reconstructable.
- **Observation**: The parity-filtered view of game state assembled for one decision step —
  structured entries plus the images the agent was shown at that step — each resolvable to the
  observation-catalog entries that produced it.
- **Decision**: One action the agent issued at one decision step, with its stated reasoning, whether
  it was proactive or a response to a game-initiated prompt, and its execution outcome — applied,
  rejected, or partially applied — with the reason for any rejection.
- **Parity Declaration**: A catalog entry for one observable, one visual view, or one action, stating
  what it exposes or does and the in-client action a human player would take to obtain or perform the
  same thing; versioned, and referenced by every run that used it.
- **Integration Capability**: A Firetuner script or bespoke skill that implements one or more catalog
  entries, recording which path it uses and, for bespoke ones, the documented Firetuner gap that
  justifies it.
- **Model Call**: One request to the provider layer for one decision step's decision — model
  requested, model served, latency, cost, retry count, and fallback, if any. A turn contains as many
  model calls as it contained decision steps.
- **Save Point**: A named, addressable game save bound to a run and turn, with its lineage and its
  retention status — eligible for removal only once its run has been archived.
- **Run Event**: A non-turn occurrence on a run's timeline — crash detected, hang detected, unknown
  screen stall, turn ended on no progress, save taken, resumed, provider failure, fallback, image
  withheld, branch created, branch abandoned, run archived, game build change accepted, lifecycle
  command received.
- **Screen Capture**: An image of the game's own view bound to one run, turn, and decision step, with
  its capture time, the camera state that produced it, a screened or withheld status recording whether non-player
  UI was detected, and whether it was shown to the agent, retained as human evidence, or both.
- **Seed Set**: A named collection of map seeds run under a consistent civilization, ruleset, and
  Civilization VI build — where the build identifies platform and version together — so that runs
  within the set are comparable, together with any operator-accepted build changes and the runs that
  were played under each.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A configured run plays from turn 1 to its stop condition with zero human interaction
  with the game client, in at least 90% of attempts on a validated seed set. Every started attempt
  counts in the denominator and any attempt that does not reach its stop condition counts as a
  failure — including a run that stops in a correctly recorded failed state, and one lost to a cause
  outside the harness such as a host reboot. The 10% headroom is what absorbs those; no attempt is
  reclassified or excluded after the fact.
- **SC-002**: A run configured to play to a game outcome completes across every era to victory or
  defeat without human interaction with the game client, and without stalling on a mechanic that era
  introduced.
- **SC-003**: 100% of completed turns have a persisted record containing every decision step in
  order — the observation given at that step, the decision issued, and the reasoning stated — plus
  the turn's resulting yields, verified by the absence of any turn number gap between 1 and the stop
  turn and of any step gap within a turn.
- **SC-004**: 100% of turns begin with a successfully written, addressable quicksave; any turn that
  could not take one did not proceed.
- **SC-005**: 100% of game-initiated prompts encountered are either answered through a declared
  catalog entry with the decision recorded, or stall the run visibly; zero are dismissed, defaulted,
  or absorbed without a record.
- **SC-006**: Zero observations reach the agent, and zero actions are performed, without a declared
  parity basis — audited per release, and any finding blocks release.
- **SC-007**: For any completed run, an auditor can enumerate every distinct observation and action it
  used — structured and visual — and resolve each to its parity declaration and catalog version, using
  only the recorded data.
- **SC-008**: Zero instances of debug data, hidden AI state, unrevealed map contents, or harness
  operational telemetry appearing in an agent context — audited per release, and any finding blocks
  release.
- **SC-009**: Zero images shown to the agent contain the Firetuner window, developer console, debug
  overlay, or other harness UI, and zero were produced from a camera state a human could not reach —
  audited per release, and any finding blocks release.
- **SC-010**: A game client crash is detected and recorded within 60 seconds, and the run resumes as
  the same continuous run losing at most the one turn in progress.
- **SC-011**: Across a set of at least 20 unattended runs, zero runs contain a silently missing turn —
  every gap is either recovered or explicitly marked, and the run's completeness status reflects it.
- **SC-012**: Zero turns are ended on a fabricated, defaulted, or heuristic action recorded as an
  agent decision.
- **SC-013**: 100% of turns that ran without their images are recorded as visually degraded, and their
  run's comparability status reflects it.
- **SC-014**: Two runs started from the same save point begin from an identical game position, and
  branching a run leaves the parent's record unchanged.
- **SC-015**: The same seed and configuration can be run under a different model by changing run
  configuration alone, with zero code or integration changes, verified on at least two providers.
- **SC-016**: 100% of model calls record the model that actually served them, and turns served by a
  fallback model are distinguishable from those served by the primary.
- **SC-017**: Zero model calls are made with images dropped from the context to fit a model's limits;
  any run whose chain cannot carry the full context fails to start rather than degrading silently.
- **SC-018**: Zero credentials or API keys appear in any run record, capture, log, or agent context —
  audited per release, and any finding blocks release.
- **SC-019**: Zero stored captures contain the Firetuner window, developer console, or other
  non-player UI — audited per release, and any finding blocks release.
- **SC-020**: Every capability in use is either implemented through Firetuner or carries a recorded
  justification naming the Firetuner gap it fills; 100% coverage, verified per release.
- **SC-021**: A run that cannot recover stops in a recorded failed state identifying its last-known
  good save within the configured recovery bound, in 100% of unrecoverable cases, with zero
  indefinite retry loops observed.
- **SC-022**: Zero turns are ended by the harness while the agent is still making progress, and every
  turn ended by the no-progress backstop is recorded as such and distinguishable from one the agent
  chose to end.

## Assumptions

- Runs are single-player Civilization VI against the game's standard AI opponents on a
  competitive-balance ruleset; multiplayer and human-versus-agent sessions are out of scope.
- The harness supports full games to a victory or defeat outcome across every era. The turn-50
  science and culture goal is one configured stop condition among several, not the boundary of what
  the decision surface covers.
- The agent perceives the game as parity-filtered structured state together with rendered images of
  the game's own view. Both are declared observations; the images are held to the same parity standard
  as the data, which is why camera control is a declared action and screening is a precondition rather
  than a post-hoc check.
- Requiring images in the agent's context narrows the usable model set to those that accept them, on
  every provider in a run's fallback chain. This is an accepted constraint on Principle VII's
  swap-freely goal, taken deliberately in exchange for closer perceptual parity.
- Per-turn cost is materially higher with images in context than with structured state alone, a
  full-game run is far longer than a turn-50 run, and the within-turn decision loop means a turn
  costs one model call per decision rather than one per turn. Cost per run is expected to be
  dominated by these three choices; the harness records per-call cost so the trade-off stays visible
  rather than managing a budget itself. Nothing bounds a turn by time or cost: a turn runs for as
  many decision steps as the agent needs, and only the no-progress backstop ends one the agent has
  not chosen to end. Accepting unbounded per-turn cost is a deliberate trade for play fidelity.
- The match-tracking store (deliverable 3) owns the schema and durability of run data; this harness
  writes through its interface and does not define it. This feature depends on that store existing.
- The unified web interface (deliverable 1) is the presentation surface for run data; this harness
  produces the turn records, events, and captures that interface reads, and presents nothing itself
  beyond lifecycle control and diagnostics.
- `GUIDEBOOK.md` (deliverable 5), where it exists, is the expected source of the out-of-game strategic
  guidance referenced by FR-021; authoring its content is out of scope here. A human player would
  bring comparable prior knowledge to a game, so supplying run-independent strategy text is treated as
  parity-consistent.
- Human parity governs what the agent can know and do, not how fast it can act; the harness is not
  required to throttle itself to a human's actions-per-minute. This is recorded as an explicit
  interpretation of Principle I rather than an oversight.
- One active run per game client instance; running several seeds concurrently is achieved by running
  several harness instances against separate clients or machines, not by multiplexing one client.
- A crashed turn is recovered by resuming from that turn's own start quicksave and replaying it, which
  is the purpose Principle IV's per-turn quicksave exists to serve; the abandoned attempt is retained
  in the record rather than discarded.
- Run lifecycle control is exercised through the harness's own operator surface, consistent with
  deliverable 1's decision to be a read-only observer.
- Seeds, civilization, and ruleset are fixed per seed set; varying them is a new seed set rather than a
  variation within one, so that runs within a set stay comparable.
- The harness runs on the same machine as the Civilization VI client, with local access to its saves
  and to Firetuner.
- Branch orchestration, sampling strategy, and search policy belong to deliverable 4; this harness
  provides the branch-from-save mechanism and lineage recording those will drive, and does not decide
  what to branch or when.
- Captures are produced here at one per decision step as the baseline, since each step shows the
  agent a board its own previous action changed; a step may involve more than one image where its
  declared views call for it. Deliverable 1 enforces the non-player-UI rule again as a second gate
  on what it displays.
- Model cost accounting is recorded per call from provider-reported usage; the harness does not
  independently price tokens.
