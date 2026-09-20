# Feature Specification: Unified Web Interface

**Feature Branch**: `001-unified-web-interface`

**Created**: 2026-09-19

**Amended**: 2026-09-20 — see [Amendments](#amendments) for what changed and why.

**Status**: Draft

**Input**: User description: "feature 1 web interface / UI principles"

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Watch a live run without opening the game (Priority: P1)

The user opens the interface while a Civilization VI run is in progress and, without launching or
touching the game client, understands where the run stands: which turn it is on, what the agent can
currently see, what it just decided and why, what that decision cost in time and model spend, and
whether the run is healthy or stuck. The view keeps itself current as turns advance.

**Why this priority**: This is the core of the deliverable. Today the only way to know what is
happening is to watch the game client, which the constitution explicitly forbids requiring. A live
view alone — with no history and no comparison — already replaces game-client babysitting and is
independently useful on the first run it observes.

**Independent Test**: Start a run, open the interface on a second screen, and follow ten consecutive
turns without ever focusing the game window; verify the observer can state the current turn, the
last action taken, the stated reason for it, and the run's health at any moment.

**Acceptance Scenarios**:

1. **Given** a run is in progress, **When** the user opens the interface, **Then** the current turn
   number, the agent's most recent decision with its stated reasoning, the current visible game
   state, and a healthy/degraded/stalled status indicator are all visible without further navigation.
2. **Given** the interface is open on a live run, **When** the harness records a new turn, **Then**
   the view updates to that turn without the user reloading or re-navigating.
3. **Given** a run is in progress, **When** the user inspects any displayed game information,
   **Then** every element shown is information a human player could obtain through the standard game
   UI at that same point in the run.
4. **Given** the game client has crashed or the run has stalled, **When** the user looks at the
   interface, **Then** the run is shown as interrupted with the last-known good turn identified, and
   the stale state is never presented as current.
5. **Given** a model provider call fails or is rate-limited, **When** the user looks at the run,
   **Then** the failure and any retry or provider fallback is visible as a run event rather than
   appearing as an unexplained pause.
6. **Given** a run is in progress, **When** the user views the current turn, **Then** the latest
   capture of the game's own screen is shown beside the structured panels for that same turn.
7. **Given** the user wants to intervene in a run, **When** they look for a control, **Then** the
   interface offers none, and instead shows the run identifier, lifecycle status, and last-known good
   save needed to act through the harness.

---

### User Story 2 - Same view for the user and the directing session (Priority: P2)

The directing Claude Code session and the user work from one shared picture. Anything the user can
see in the interface, the session can retrieve; anything the session is working from, the user can
open and look at. The user can point at a specific turn or panel by reference and the session can
open exactly that, so "look at what happened on turn 34" is unambiguous.

**Why this priority**: Constitution Principle VI makes shared visibility the point of this
deliverable, and collaborative debugging of the harness is impossible when the two parties are
describing different pictures to each other. It is a thin slice on top of Story 1 — the same
content, addressable and machine-retrievable — so it delivers quickly once the live view exists.

**Independent Test**: Have the directing session describe the current run state from its own
retrieval, and have the user read the same state off the screen; the two accounts must match panel
for panel, with no element present in one and absent from the other.

**Acceptance Scenarios**:

1. **Given** any panel visible to the user, **When** the directing session requests the state behind
   it, **Then** it receives the same content in a form it can consume, with no user-only omissions.
2. **Given** the user is looking at a specific turn of a specific run, **When** they share that
   view's reference with the session, **Then** the session resolves it to the identical run, turn,
   and panel.
3. **Given** information the harness holds but that a human player could not obtain in-client,
   **When** either the user or the session views the run, **Then** that information is absent from
   both views rather than shown to one of them.
4. **Given** the session has retrieved run state, **When** the user asks what the session is looking
   at, **Then** the user can open the same reference and see the same values.

---

### User Story 3 - Replay and inspect a completed run turn by turn (Priority: P3)

After a run ends, the user walks through it turn by turn — stepping, jumping to a specific turn, or
scrubbing a trajectory chart — and sees, for each turn, the state the agent was working from, the
decision it made, the reasoning it gave, and the yields and outcomes that followed. Turns whose
record is incomplete are visibly marked as such.

**Why this priority**: Post-mortem inspection is where strategy learning and guidebook material come
from, and it is what makes a finished run worth anything beyond its final score. It depends on the
live view's panels already existing, so it follows them.

**Independent Test**: Take one completed run, open it cold, and answer "what did the agent do on
turn 23 and why" plus "when did science output first diverge from the plan" using only the interface.

**Acceptance Scenarios**:

1. **Given** a completed run, **When** the user opens it and selects any turn, **Then** the state,
   decision, reasoning, and resulting yields for that turn are shown as they were at that turn.
2. **Given** a run whose turn-by-turn record has gaps, **When** the user views it, **Then** the
   missing turns are marked as incomplete and the run is flagged as unfit for trend comparison.
3. **Given** the user is on a given turn, **When** they step forward or backward, **Then** the
   adjacent turn's state loads without losing their current panel or focus.
4. **Given** a run that was resumed after a crash, **When** the user replays across the interruption,
   **Then** the interruption and the resume point appear in the turn sequence as events.
5. **Given** a turn with a recorded capture, **When** the user opens that turn, **Then** the capture
   taken at that turn is shown beside its structured panels; **and given** a turn whose capture is
   missing or was withheld, **Then** the panels still render with the capture marked unavailable.

---

### User Story 4 - Compare runs and spot trends across them (Priority: P4)

The user browses every run the project has produced — filtering by seed, civilization, ruleset,
model, and outcome — and puts several side by side to see how their science and culture trajectories
diverge, where they diverge, and what differed in the decisions at that point.

**Why this priority**: Cross-run trending is the payoff of the match-tracking store and the input to
the optimization work, but it is only meaningful once several complete runs exist, so it is the last
slice to become useful.

**Independent Test**: With several completed runs recorded, select five of them and identify which
reached the highest science output by turn 50 and the turn at which the leader separated from the
rest, using only the interface.

**Acceptance Scenarios**:

1. **Given** multiple recorded runs, **When** the user opens the run catalog, **Then** each run
   lists its seed, civilization, ruleset, model, turn count, outcome metrics, and record-completeness
   status.
2. **Given** the catalog, **When** the user filters by any recorded attribute, **Then** only matching
   runs remain listed.
3. **Given** several selected runs, **When** the user compares them, **Then** their key metric
   trajectories are shown on common axes and any incomplete-record run is excluded or visibly
   quarantined from the comparison.
4. **Given** a point of divergence on a comparison chart, **When** the user selects it, **Then** they
   can open the corresponding turn in each compared run.

---

### Edge Cases

- No runs exist yet, or the interface is opened before the first turn is recorded — the user gets an
  explanatory empty state, not an error or a blank screen.
- A turn's record is written partially or out of order — the interface shows the last complete turn
  as current and marks the partial one, rather than rendering a half-populated turn as fact.
- The harness stops writing turns while the run is nominally active — a stall must be distinguishable
  from a slow turn within a bounded time.
- The game client crashes and the run resumes from a save — the interface must show one continuous
  run with a visible interruption, not two unrelated runs.
- A turn is superseded because a branch was abandoned or rolled back — a reference to that turn must
  resolve to something explanatory rather than silently showing superseded state as current.
- The agent's reasoning for a turn is very long, empty, or malformed — the panel degrades gracefully
  and still identifies the action taken.
- A historical run predates a change to the recorded state's shape — it remains viewable, with
  unavailable fields marked absent rather than shown as zero.
- Several runs are recorded concurrently — the user can tell which run they are looking at at all
  times and the live view never mixes turns from different runs.
- The connection between the browser and the interface drops — the view marks itself as possibly
  stale and recovers to live without a manual reload.
- A panel would surface game information beyond human parity — it renders as unavailable rather than
  falling back to the unfiltered value.
- A turn's screen capture failed, is corrupt, or was never taken — the turn's structured panels
  render normally with the capture marked unavailable.
- A capture contains a debug overlay, tuner window, or other non-player UI — it is withheld from both
  the user and the directing session rather than shown with a warning.
- The interface is open on two devices on the local network at once — each stays current
  independently and neither sees a different picture of the same run.
- The viewing device is on a different network than the harness machine — the interface is simply
  unreachable rather than partially loading or exposing itself beyond the local network.
- The user wants to stop a run they are watching — the interface has no control to offer, so it must
  make the run identifier, status, and last-known good save immediately readable for acting elsewhere.

## Requirements *(mandatory)*

### Functional Requirements

**Live observation**

- **FR-001**: The interface MUST present the currently active run's turn number, current visible game
  state, most recent agent decision, and that decision's stated reasoning, on a single landing view
  with no navigation required.
- **FR-002**: The interface MUST update the live view as new turns are recorded, without the user
  reloading or re-navigating, and MUST indicate when its view was last confirmed current.
- **FR-003**: The interface MUST surface run health — running, waiting on the model, waiting on the
  game, stalled, crashed, resumed, paused, or finished — and MUST distinguish a stalled run from a
  slow turn. A lifecycle state the interface does not recognise MUST surface as an explicit *unknown*
  health rather than being mapped onto the nearest familiar value. *(Amended 2026-09-20 — see
  [Amendment C](#amendment-c--fr-003s-health-vocabulary-was-two-values-short).)*
- **FR-004**: The interface MUST show harness-level operational events as part of the run timeline,
  including model-provider failures, retries, provider fallbacks, crash detection, and save/resume
  points.
- **FR-005**: The interface MUST show per-turn cost and latency for agent model calls, including the
  model used.
- **FR-006**: The user MUST be able to understand a run's current state entirely through the
  interface, with no step requiring the Civilization VI client to be opened, focused, or inspected.

**Shared visibility**

- **FR-007**: Every element of game or run state presented to the user MUST be retrievable by the
  directing Claude Code session in a machine-consumable form, and vice versa; no element may exist in
  one view and not the other.
- **FR-008**: Every view MUST be addressable by a stable reference that identifies run, turn, and
  panel, and that both the user and the directing session can resolve to the identical content.
- **FR-009**: A reference to a turn that was superseded, rolled back, or abandoned MUST resolve to an
  explanation of its status rather than presenting superseded state as current.

**Human-parity boundary**

- **FR-010**: The interface MUST NOT display any game information a human player could not obtain
  through Civilization VI's standard game UI at the corresponding point in the run — including
  unrevealed map contents, opponent internal state, hidden AI intent, and any debug or provenance
  data — regardless of whether the harness holds it.
- **FR-011**: When a displayable value cannot be shown within the parity boundary, the interface MUST
  render it as unavailable rather than falling back to an unfiltered value.
- **FR-012**: Each panel MUST be traceable to the in-client action a human player would take to
  obtain the same information, so that parity is auditable panel by panel.
- **FR-013**: Harness operational data (model identity, cost, latency, errors, save lineage, run
  configuration) MUST be treated as out-of-game telemetry: visible to the user and to the directing
  session, and never presented to the playing agent as game information.

**History and replay**

- **FR-014**: Users MUST be able to open any recorded run and view any of its turns, showing the
  state, decision, reasoning, and resulting yields as of that turn.
- **FR-015**: Users MUST be able to move between turns by stepping, by jumping to a specific turn,
  and by selecting a point on a metric trajectory.
- **FR-016**: The interface MUST mark turns whose record is incomplete and MUST flag any run with
  gaps as unfit for trend comparison.
- **FR-017**: A run that was interrupted and resumed MUST be presented as one continuous run with its
  interruption and resume point visible in sequence.

**Catalog and comparison**

- **FR-018**: The interface MUST list all recorded runs with seed, civilization, ruleset, model, turn
  count, outcome metrics, record-completeness status, and start/end times.
- **FR-019**: Users MUST be able to filter and sort the run catalog by any listed attribute.
- **FR-020**: Users MUST be able to select multiple runs and view their key metric trajectories —
  including science and culture output per turn — on common axes.
- **FR-021**: Runs whose record is not **provably** complete MUST be excluded from or visibly
  quarantined within any comparison or trend view. A run's record is provably complete only when the
  store reports its record-completeness status as complete **and** reports no gaps in its
  turn-by-turn record. Either signal alone is enough to quarantine: a run the store calls complete
  while also listing gapped turns is quarantined, and so is a run that lists no gaps but carries a
  completeness status that is anything other than complete — including a value this interface does
  not recognise. The interface MUST read both signals verbatim and MUST NOT re-derive either, and a
  signal it could not read at all counts as absent, not as clean. *(Amended 2026-09-20 — see
  [Amendment A](#amendment-a--fr-021-now-fails-closed-on-either-signal-constitution-principle-iii).)*
- **FR-022**: From a point on a comparison view, users MUST be able to open the corresponding turn in
  each compared run.
- **FR-037**: Any comparison or trend view MUST state the basis on which the runs it shows are
  comparable — whether they share a seed, civilization, ruleset, and model — and MUST report any of
  those dimensions it cannot establish as **unverifiable** rather than as uniform. A comparison whose
  runs differ on one of those dimensions MUST say so alongside the comparison rather than presenting
  the trajectories as like-for-like. *(Added 2026-09-20 — see
  [Amendment B](#amendment-b--fr-037-added-constitution-principle-iv-had-no-requirement-behind-it).
  Numbered after FR-036 to continue the document's sequence; the existing numbers are referenced
  from the plan, the contracts, the data model, and the test suite and are not renumbered.)*

**Data boundaries**

- **FR-023**: The interface MUST read run data from the match-tracking store and MUST NOT communicate
  with the Civilization VI client directly.
- **FR-024**: The interface MUST NOT modify recorded run history.
- **FR-025**: The interface MUST remain able to open historical runs recorded before a change to the
  stored state's shape, marking fields that are unavailable for those runs rather than showing them
  as zero or absent-by-omission.
- **FR-026**: The interface MUST be read-only with respect to the harness and the game: it MUST NOT
  start, pause, resume, stop, branch, or otherwise command a run, and MUST NOT issue any game action.
- **FR-027**: Because the interface cannot intervene, it MUST make what is needed to intervene
  elsewhere immediately visible — the run's identifier, its current lifecycle status, and its
  last-known good save and turn — so the user can act through the harness without reconstructing
  context from scratch.

**Access**

- **FR-028**: The interface MUST be reachable from other devices on the user's local network by a
  stable address, in addition to the machine running the harness, and MUST NOT require
  authentication.
- **FR-029**: The interface MUST NOT be reachable from outside the local network.
- **FR-030**: The interface MUST NOT display credentials, API keys, or other secrets held in the
  harness configuration, since any device on the local network can view it unauthenticated.

**Visual presentation**

- **FR-031**: The interface MUST present captured images of the game's own screen alongside the
  structured panels for the same state — captures as evidence, structured data as the queryable
  record.
- **FR-032**: A capture MUST show only what a human player's screen would show; no debug overlay,
  developer console, tuner panel, or other non-player UI may be visible. A capture containing such an
  element MUST be withheld rather than displayed.
- **FR-033**: Each capture MUST be bound to the run and turn it belongs to, and MUST be viewable both
  in the live view and at its own turn during replay.
- **FR-034**: When no capture exists for a turn — capture failed, or the run predates capture — the
  interface MUST render that turn's structured panels normally and mark the capture as unavailable.
- **FR-035**: Comparison and trend views MUST operate on structured metric data, never on captures;
  no comparison question may require a capture to answer it.
- **FR-036**: The interface MUST remain usable for a run carrying a capture for every turn; viewing a
  turn MUST NOT require loading captures beyond those being viewed.

### UI Principles

These are durable design constraints that govern this interface and any later addition to it. Each
is stated so that a specific screen can be judged compliant or not.

- **UP-001 — Parity by construction**: Panels are built from a parity-filtered view of run state, so
  that showing out-of-parity game information requires deliberately bypassing the filter rather than
  merely forgetting to apply one.
- **UP-002 — One picture, two readers**: Every view is designed to be equally consumable by the user
  and by the directing session; a panel is not considered done until both can obtain its content.
- **UP-003 — Glance, then drill**: The landing view answers "what is happening right now and is it
  healthy" without interaction; all detail is reachable by drilling down from it, never required to
  reach that answer.
- **UP-004 — Decisions carry their evidence**: Any displayed agent decision is shown adjacent to the
  state it was made from and the reasoning given for it; a decision is never shown as a bare action.
- **UP-005 — Honest state**: The interface distinguishes current, stale, incomplete, superseded, and
  unavailable, and never renders any of them as confirmed current fact.
- **UP-006 — Everything is addressable**: Any view a user can reach has a stable reference they can
  hand to the directing session, and vice versa.
- **UP-007 — Comparison is first-class**: Historical runs are presented so that placing them side by
  side is a normal action rather than an export-and-analyze-elsewhere workaround.
- **UP-008 — No game client required**: No task the interface is responsible for may depend on the
  user opening, focusing, or reading the Civilization VI client.
- **UP-009 — Picture beside the numbers**: Captures and structured panels are shown together and
  neither substitutes for the other — the capture is what the player saw, the structured panel is
  what can be queried, compared, and trusted when the capture is missing.
- **UP-010 — Observer, not operator**: The interface reports; it never acts. Where the user would
  want to intervene, it shows what they need to intervene elsewhere instead of offering a control.

### Key Entities *(include if feature involves data)*

- **Run**: One playthrough attempt. Seed, civilization, ruleset, model configuration, start and end
  time, lifecycle status, record-completeness status, and lineage to any run it branched from.
- **Turn Snapshot**: The parity-filtered game state as of one turn of one run — the state the agent
  was working from — plus the yields and outcomes recorded for that turn.
- **Agent Decision**: The action the agent issued for a turn, its stated reasoning, and the model
  call that produced it (model identity, latency, cost, retries).
- **Run Event**: A non-turn occurrence on a run's timeline — crash detected, save taken, run resumed,
  provider failure, branch created or abandoned.
- **Metric Series**: A named per-turn measurement across a run (science output, culture output, and
  other tracked yields) used for trajectories and comparison.
- **Screen Capture**: An image of the game's own screen bound to one run and turn, with its capture
  time and a screened/withheld status recording whether non-player UI was detected in it.
- **Panel**: A named unit of presented information with a declared parity basis — the in-client
  action a human would take to obtain it — and a stable reference.
- **View Reference**: A stable identifier resolving to a run, a turn, and a panel, shared between the
  user and the directing session.
- **Comparison Basis**: The statement of *why* a set of runs is comparable — whether they share a
  seed, civilization, ruleset, and model — with each dimension reported as uniform, differing, or
  unverifiable. Carried by every comparison view (FR-037, Constitution Principle IV). *(Added
  2026-09-20 with FR-037.)*

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Within 10 seconds of opening the interface on a live run, a user can state the current
  turn, the agent's last action, its stated reason, and whether the run is healthy.
- **SC-002**: Across a defined list of run-monitoring tasks, 100% are completed without opening,
  focusing, or reading the Civilization VI client.
- **SC-003**: A newly recorded turn appears in an open live view within 5 seconds of being recorded.
- **SC-004**: 100% of panels presented to the user are retrievable by the directing session, and 100%
  of what the session retrieves is presentable to the user — verified by panel-level audit each
  release.
- **SC-005**: Zero panels display game information a human player could not obtain in-client; every
  panel has a documented parity basis, and any finding blocks release.
- **SC-006**: A user can open any specific turn of any recorded run within 30 seconds and 3
  interactions from the interface's landing view.
- **SC-007**: A user can compare the science and culture trajectories of 5 selected runs and identify
  the leader and the turn of divergence in under 2 minutes.
- **SC-008**: With a run of 300+ turns and 50+ recorded runs in the catalog, every view reaches a
  usable state within 2 seconds of being requested.
- **SC-009**: A stalled or crashed run is reflected in the interface within 60 seconds of the stall
  or crash, with the last-known good turn identified.
- **SC-010**: 100% of runs with gaps in their turn record are marked incomplete and are excluded from
  or quarantined within every comparison and trend view.
- **SC-011**: In 90% of cases, a user inspecting a past turn can state why the agent made that
  decision without asking the agent to explain it again.
- **SC-012**: A view reference handed from the user to the directing session resolves to the identical
  run, turn, and panel in 100% of attempts.
- **SC-013**: The interface opens from a second device on the local network with no login step and
  shows the same run state as the harness machine within the same 5-second currency window.
- **SC-014**: Zero displayed captures contain debug overlays, tuner windows, or other non-player UI —
  audited each release; any finding blocks release.
- **SC-015**: 100% of turns with a missing or withheld capture still render their structured panels,
  with the capture explicitly marked unavailable.
- **SC-016**: Every comparison and trend question answerable in the interface is answerable with all
  captures unavailable.

## Assumptions

- The match-tracking store (deliverable 3) is the single source of truth for run data; this feature
  reads from it and does not define its schema, nor does it write run history.
- The Civ-playing harness (deliverable 2) is responsible for producing turn snapshots, decisions, and
  events; this interface never talks to the game client and never issues game actions itself.
- The parity filter required by Constitution Principle I is applied to run state before it reaches
  this interface; this interface enforces the boundary at the panel level rather than re-deriving it.
- The primary audience is the project owner plus one directing Claude Code session; no multi-user
  accounts, roles, or permissions are needed in this feature.
- Run lifecycle control (starting, pausing, stopping, branching) happens through the harness itself,
  not through this interface; the interface's job is to make that decision well-informed.
- The user's local network is trusted, and the interface will not be port-forwarded or otherwise
  published beyond it; unauthenticated LAN access is an accepted, deliberate trade-off.
- Screen captures are produced by the harness (deliverable 2) and stored with the run record
  (deliverable 3); this feature displays them and does not define capture cadence or retention. One
  capture per turn is assumed as the baseline.
- Captures are screened for non-player UI before they reach the interface; the interface enforces
  FR-032 as a second gate rather than as the only one.
- Runs are single-player Civilization VI on a competitive-balance ruleset; the interface does not
  need to represent multiplayer or human-versus-agent sessions.
- Standard desktop browser on a wide screen is the target presentation; viewing from a laptop or
  tablet on the local network is supported, but a phone-optimized layout is out of scope.
- Branch, Monte Carlo, and optimization orchestration UI belongs to deliverable 4; this feature only
  needs to display branch lineage that already exists in the record.
- Authoring or editing `GUIDEBOOK.md` content is out of scope for this feature.
- "Science and culture output" are the headline tracked metrics because of the turn-50 goal, but the
  comparison view is expected to handle any per-turn metric the store records.


## Amendments

Changes made to this specification after its user stories were implemented, each recorded with
what changed and why, so the diff reads as a decision rather than as a rewrite.

The rule applied throughout: **where a requirement and the constitution diverged, the constitution
won and this document was amended up to it.** No code was weakened to agree with a requirement as
written. Where an implementation choice disagreed with an artifact and the artifact was right, the
code was fixed instead and no amendment was made.

### Amendment A — FR-021 now fails closed on either signal (Constitution Principle III)

**Amended**: 2026-09-20. **Owner-authorised.**

FR-021 originally read: *"Runs with incomplete records MUST be excluded from or visibly quarantined
within any comparison or trend view."* In practice that meant one signal, the store's
`record_completeness_status`.

Constitution Principle III says something different and stricter: *"A run's results MUST NOT be used
for trending, datamining, or optimization input if its turn-by-turn record has gaps."* The store
publishes `turn_gaps()` as a separate read of exactly that, so a store can answer the two questions
differently — it can report a run complete while also listing gapped turns, and nothing in the
published port makes that contradiction impossible.

The implementation resolved this fail-closed from the start: `derive_trend_eligibility()` quarantines
on *either* signal, on a completeness value it does not recognise, and on the gap read not having
happened at all, and `ComparisonView`'s own validator refuses to construct a model in which a
quarantined run has acquired a series, an axis, or a leadership claim. There is a test for the exact
contradiction case (`test_a_run_the_store_calls_complete_while_listing_gaps_is_still_quarantined`).

So the code was right and this requirement was wrong. FR-021 is amended up to the constitution rather
than the code being relaxed down to FR-021. The practical effect of the old wording, had anyone
implemented it literally, would have been a run with holes in its record quietly contributing to a
trend on the strength of one status column — the single failure Principle III exists to prevent.

### Amendment B — FR-037 added (Constitution Principle IV had no requirement behind it)

**Added**: 2026-09-20. **Owner-authorised.**

Constitution Principle IV requires that comparison work *"run against a fixed set of initial seeds
under a consistent civilization and ruleset"*, because comparing strategies across differing starting
conditions is not meaningful. FR-018 – FR-022 never asked the comparison view to say anything about
this: nothing required it to state that the runs on the chart share a seed, a civilization, a ruleset,
or a model, and nothing required it to admit when it cannot tell.

The implementation built `ComparisonBasis` anyway and reports `unverifiable` — not `uniform` — for any
dimension the published store port cannot reach. That is correct behaviour with no requirement
traceability: it satisfied the constitution while discharging no FR, which means a later contributor
could have removed it without any requirement noticing.

FR-037 states the obligation, so the behaviour is now traceable from constitution to requirement to
implementation to test. Two details in its wording are deliberate:

- **"unverifiable rather than uniform"** — a basis the interface cannot establish must not be reported
  as agreement. Silence read as sameness is how an incomparable comparison looks exactly like a
  comparable one.
- **"MUST say so alongside the comparison"** — differing runs are not refused. Comparing across a
  differing dimension is sometimes exactly the question being asked; what must never happen is
  presenting it as like-for-like.

It is numbered FR-037 rather than inserted at FR-023 because the existing numbers are referenced from
plan.md, both contracts, data-model.md, tasks.md and the test suite. Renumbering would have made every
one of those references silently wrong, which is a far worse outcome than a section whose numbers are
not contiguous.

### Amendment C — FR-003's health vocabulary was two values short

**Amended**: 2026-09-20.

FR-003 enumerated seven health states. The interface renders nine, and both additions are correct:

- **`paused`** is one of 002's own `Run.lifecycle_state` values. The derivation rule in
  `data-model.md` §2 maps every lifecycle state onto a health state and had nowhere to put this one.
  Rendering a paused run as `running` would have been a false statement about the run; rendering it
  as `stalled` would have been worse, since `stalled` is the word the interface uses for a run that
  has stopped responding and that a human should go look at.
- **`unknown`** is the fail-closed answer for a lifecycle state this feature has not been taught. 002
  owns that vocabulary and may extend it; mapping an unrecognised value onto the nearest familiar one
  would make a future harness state silently indistinguishable from a state this interface actually
  understands.

Neither is a new capability — both shipped from the foundation phase and were recorded as a finding
against this requirement at the time. FR-003 is amended to name them, so the rendered vocabulary and
the requirement agree and the enumeration is no longer quietly a lie about what the screen can show.

### Amendments to the other artifacts

Made in the same pass and recorded in the artifacts themselves rather than repeated here:

- **`contracts/web-read-api.md`** — the three collection routes (`GET /runs`, `GET
  /runs/{id}/events`, `GET /runs/{id}/metrics`) return wrapper objects, not bare lists; the
  step-level panel shape added to the view-reference table; two error-table rows for the
  step-window and `?series=` conventions; the turn route's query parameters stated.
- **`data-model.md`** — `HealthStatus.state` gains `paused`/`unknown` (with FR-003, Amendment C);
  `TurnCompleteness.is_gap`; `CaptureView.unavailable_reason` gains `unrecognized_status`;
  `DecisionView.action_label_is_declaration_id`; `DivergencePoint.kind`; `ComparisonView.basis`
  (with FR-037); and V10's enumeration now states why `DecisionStepView` is excluded from it.

One thing was fixed in **code** rather than amended, and is noted here because the reasoning is the
same one: the turn route's default response was returning *every* step, while `data-model.md` §5 is
explicit that "`steps` on the default response is a bounded window ... with the full ordered list
available by paging". The artifact was right and the implementation was not, so the default is now
bounded. The recorded reason for the unbounded default — that a window "would make every turn look
shorter than it is" — is answered by `StepWindow`, which states `total`, `has_more`, and every index
the page left out.

### What was deliberately *not* amended

Recorded here so their absence reads as a decision rather than an oversight.

- **The four optional probed store capabilities** (`RunConfigurationReader`, `CaptureBlobReader`,
  `RunCatalogReader`, `TurnAttemptReader`). These exist because
  `specs/002-civ-playing-harness/contracts/match-store-port.md` publishes no read that resolves a run
  configuration, returns capture bytes, enumerates terminal runs, or addresses a turn attempt by
  index. Four probed capabilities is not four local workarounds; it is an unpublished half of a port.
  But that port is **deliverable 2's contract, not this feature's**, and amending another
  deliverable's contract to make this one's tasks look closed is precisely the move that would bury
  the finding. They stay recorded as notes in `tasks.md` (Foundation note 2, US1 notes 1 and 3, US2
  note 1, US4 notes 2 and 3) and remain open against deliverable 3.
  *(Update, 2026-09-20, owner-authorised: the port contract has since been amended from its own
  side — `match-store-port.md`'s new **Capability extensions** section publishes the four reads as
  obligations on deliverable 3, and `get_run_configuration` is now a published read, keyed by
  `run_id`. This feature's probe still keys that name by `config_id`; the re-key is recorded there
  as the remaining consumer-side fix. The decision recorded here — that this feature would not edit
  another deliverable's contract — stands; the amendment was made by the contract's owner.)*
- **`SEPARATION_RATIO = 0.25`** — data-model.md §11 says divergence includes values separating
  "beyond a threshold" and names no number. The implementation chose 0.25 relative to the leading
  value. Writing that number into this spec would convert an implementation default into a product
  decision nobody has actually made. It stays a recorded, labelled default (US4 note 7).
- **`ViewReference` has no attempt component** (US2 note 5). An attempt-qualified reference travels as
  a query parameter, so data-model.md §12's "the reference *is* the URL path" holds for five of the
  six shapes and not for that one. Real, recorded, and not worth a spec change: the contract already
  makes `?attempt=` a query rather than a path segment, so the two artifacts are consistent with each
  other even though the prose overstates its own generality.
