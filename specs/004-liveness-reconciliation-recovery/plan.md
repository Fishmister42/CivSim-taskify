# Implementation Plan: Harness Liveness, Reconciliation, and Autonomous Recovery

**Branch**: `004-liveness-reconciliation-recovery` | **Date**: 2026-09-22 |
**Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/004-liveness-reconciliation-recovery/spec.md`
(59 functional requirements, 27 success criteria, 6 user stories, 9 clarifications, no open markers)

**Note on the helper script**: `pwsh` is absent on this host, so `setup-plan.ps1 -Json` was
hand-derived. `FEATURE_DIR` = `specs/004-liveness-reconciliation-recovery`, `FEATURE_SPEC` =
`./spec.md`, `IMPL_PLAN` = `./plan.md`, `BRANCH` = `004-liveness-reconciliation-recovery` (the
working git branch is `live/linux`; several lanes run different features concurrently).
`.specify/feature.json` still points at `003-match-tracking-store` and was **deliberately not
modified** for the same reason. `.specify/extensions.yml` does not exist, so no pre- or post-plan
hooks were checked.

---

## Summary

Make the harness able to tell a run that is thinking from a run that is stuck, name *which kind* of
stuck, and act on the answer with the least destructive move that fits — while recording every
divergence between what the harness believes and what the game says, whether or not play stopped.

The design turns on one inversion, which the spec's clarifications reached and which this plan is
built to preserve:

> **The bound measures silence, not duration.** A long think and a wedged board are
> indistinguishable only so long as neither is saying anything. So the fix is not a longer timer; it
> is an obligation on every long-running phase to *speak*, and an obligation on the watchdog to go
> and read it. Health is established affirmatively.

That inversion has a sequencing consequence that dominates everything else in this plan, and it is
not negotiable:

> **The provider package emits nothing.** The ~146-second model wait — about 85% of a turn's
> wall-clock and the most frequent wait in the system — publishes nothing a watchdog can read, and
> the end-turn confirm poll loop is silent on the same basis. FR-005's criterion is therefore
> **unimplementable against today's code in the most common case**. A watchdog enabled ahead of
> those emissions would not be an incomplete watchdog; it would be a timer that kills the two
> healthiest long operations it has. **Making the phases emit is implementation phase 1**, and no
> silence bound and no rung may be enabled before it.

Five load-bearing structures:

1. **An emission obligation on phases, with a roster.** Every phase that can outlast the ceiling is
   enumerated as data and audited per release for whether it actually emits — a periodic tick
   *during* the wait, empirically verified to stream (FR-003, SC-026, SC-027). Silence by
   construction is a defect in the phase; the ceiling is never widened to fit it.
2. **A signal registry that records what a signal's presence does *not* establish** (FR-002), with a
   hard split between **work-derived** signals, which are byproducts of the work and may clear the
   bound, and **observer-derived** signals, which come from a timer or a ticker thread beside the
   work and may only bound the gap between records (research [R2](./research.md)). This distinction
   is the plan's principal technical finding and it rejects the obvious implementation.
3. **A classifier, not a predicate.** Five dispositions — *live*, *blocked by the game*, *blocked by
   the harness*, *the game is unreachable*, *undetermined* — derived from the four signals the
   existing detector already produces plus one read taken **independently of the component under
   suspicion**. *Undetermined* is a first-class recorded outcome and only report-only is eligible
   under it (FR-009 – FR-015).
4. **An escalating ladder ordered by destructiveness**, rungs 0–4, each declaring the disposition it
   answers, the precondition it must *observe*, what it destroys, and the independent read that
   verifies it. **Rung 5 (restart the client) is declared unavailable on every host**, because no
   method anywhere in the harness launches, restarts or terminates the client — verified for this
   plan, not assumed (FR-027 – FR-039).
5. **Reconciliation as the general case, of which liveness is the subset that stops play.** An
   assertion about what happened must be derivable from an engine read, never from the return value
   of the call that requested it — and the re-read must be independent in **command and ordering**,
   or it manufactures divergences on healthy systems (FR-019 – FR-026).

**This feature is not greenfield**, and the plan is written against what is already wired: process
liveness, tuner heartbeat, per-operation bounds and screen identity already run on a ~10 s cadence;
one recovery move (reload the turn's start quicksave and re-observe) already exists and becomes rung
4; a no-progress turn backstop already exists. What is added is the classification those signals do
not produce, the rungs above and below the one that exists, and a reconciliation check with no
counterpart today.

---

## Technical Context

**Language/Version**: Python 3.12+ under `uv`. Inherited from spec 002 unchanged; this feature adds
no runtime of its own.

**Primary Dependencies**: **No new third-party dependency.** `psutil` (already present) supplies
process facts; the one dependency change is a *mode* change — requesting the provider completion as
a **stream** so that bytes arriving become a work-derived liveness signal (research R2). `httpx` is
already the decision-path client and already supports it.

**Storage**: The existing `MatchStore` port, with a deliberate split that is part of the design:

| Written to | What | Why |
|---|---|---|
| **The match store**, before the next action (FR-040, Principle III) | Detections, classifications, rung attempts, rung skips with reasons, successes, failures, divergence incidents, human overrides, stall wall-clock and discarded model spend | These are run events. They must be permanent, queryable, and summable across runs (SC-014) |
| **Telemetry log + a per-run append-only NDJSON liveness stream** | Liveness ticks | A tick every few seconds for the length of a run is write amplification against a no-delete store — and the store's own write path is one of the things that can wedge. A signal that requires the suspect component to be healthy cannot testify about it (research R3) |

No schema change to the store is required for the tick path. The event and incident records are
additive within the existing schema version, in line with the constitution's backward-readability
rule.

**Testing**: `pytest` in the four existing tiers, plus two obligations this feature makes
first-class rather than leaves to practice:

| Tier | What this feature puts there | Needs Civ VI? |
|---|---|---|
| `tests/unit` | Signal registry invariants, disposition selection, ladder ordering and monotonicity, lock run-ownership, independent-read construction refusal | No |
| `tests/contract` | The SC-026 long-phase roster, the reachable-**and-able-to-fire** structural check (FR-058), the inert-construction refusal (FR-059), Principle I audits (SC-007, SC-020, SC-023) | No |
| `tests/integration` | Injected-real-failure fixtures at the seams the real failures actually cross — a real TCP reset through `nexus/client.py`, a harness-belief/engine disagreement, a leaked lock, a silent phase | No |
| `tests/live` | The stranded diplomacy view and a real client death. Marked, excluded from CI, **owned by the live lane** | Yes |

- **Every detector and every rung ships as a fixture/negative-control *pair*** (FR-054, FR-055). The
  pair is one artifact because neither half is acceptable alone, and for reconciliation checks the
  control must be *a correct system a naive version of the check would call diverged*.
- **Every acceptance check is demonstrated failing against a deliberately broken build** before it
  counts (FR-057). This promotes the project's existing hand-run "revert-confirmed" discipline to a
  per-check requirement, with the demonstration recorded beside the check.

**Target Platform**: Unchanged from spec 002 — native Windows, macOS or Linux on the same machine as
the client. **Rung availability is per host and reported at run preparation** (FR-039, SC-022), and
two rungs are already known to be conditional: dismissal is unreachable on Wayland (synthetic input
blocked by design) and against the post-defeat exit-confirm modal (measured to ignore synthetic
input), and **rung 5 is unavailable everywhere**.

**Project Type**: Single Python project. This feature is one new subpackage plus extensions to
`resilience/`, `run/`, `act/`, `provider/`, `store/` and `catalogs/`.

**Performance Goals**:

- **A run emitting nothing affirmative for 180 s is stalled**, derived downward per run where that
  run's measured history supports it (FR-004, FR-005, SC-002). Measured on **silence, never on
  duration**.
- **Not in tension with spec 002's 60 s crash budget** (SC-010): a dead client is directly
  observable and must be caught fast; a stall is an inference. The record says which question it
  answered.
- **Stall onset to recovery-or-terminal-state ≤ 30 minutes** across ≥20 unattended runs, of which at
  most 3 minutes is detection (SC-025). The incidents this answers ran for hours.
- **Detection must be cheap enough not to be a cause** (FR-052). Contention has already produced
  `Errno 111` against a live, listening client on this box, and a classification that cannot rule
  the monitor out as the cause is *undetermined*.

**Constraints**:

- **The bound measures silence, never duration.** Nothing in this feature may bound a turn by
  wall-clock; spec 002 FR-008 and FR-014 forbid it and FR-007 restates the prohibition.
- **The watchdog's remit is harness game runs**, not arbitrary commands or processes (FR-005 scope
  clause). The project's own accepted test command is silent by construction for 225–270 s; a
  watchdog that reached it would kill a healthy suite and the lane would file it as a flaky test —
  the misattribution being the worse half.
- **One tuner connection.** A probe must not consume the run's only channel, and must not be the
  thing that wedges the run it is watching (FR-050).
- **No record is ever amended.** Corrections are additive records naming what they contradict
  (FR-022, FR-023, SC-015).
- **No rung fires without a positively observed precondition** (FR-028), and never on the absence of
  a contrary observation.
- **No escalation toward a rung this host cannot perform** (FR-039).
- **A probe's own failure is never the game's answer** (FR-051), and a missing engine method means
  *unknown*, never *no* (FR-053).
- **No detector or rung may be constructible into an inert state** (FR-059) — the direct descendant
  of the day's adopted root cause, an optional parameter with a safe-looking empty default that
  every unit test supplies and the single production call site does not.

**Scale/Scope**: **59 functional requirements (FR-001 – FR-059), in the eight groups `spec.md`
itself heads them under**: Progress signals and what each one measures; Detection and classification;
Reconciliation against the engine; The recovery ladder; Recording; Human parity and the boundary;
Probes, bounds and the capability surface; Proving the feature. *(The counting rule is stated so the
number can be re-derived: the bold headings in the spec's Functional Requirements section, counted.
Spec 002's plan carried three unreproducible figures for months, and the correction to it is the
reason this sentence exists.)* **27 success criteria (SC-001 – SC-027), of which exactly six carry
an explicit release-blocking clause**: SC-001, SC-004, SC-007, SC-015, SC-018, SC-020. SC-023 and
SC-026 are *audited per release* without one, and are listed separately here rather than rounded up
into the blocking set.

*(The counting rule for that six, because it nearly went wrong: a `grep` for "blocks release" over
`spec.md` returns **five**, because SC-015's clause wraps across two lines. The set was re-derived
by splitting the Measurable Outcomes section on criterion ids and flattening each body first. **A
negative search result is evidence only if the search could have matched** — this project adopted
that rule after a `grep` for a prose sentence returned zero against a passage that was still there
verbatim, and it fired again here, in this plan, while checking this plan's own number.)*

---

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

Constitution v1.0.0. The Development Workflow section requires every plan to explicitly verify
**Principle I** (no debug/provenance leakage in any new data path or action) and **Principle III**
(no new game-state write path bypasses turn-by-turn persistence), and to name the in-client human
action behind any new information or action surface. **Principles II and VII are directly
implicated** by this feature and are verified in the same detail rather than summarised.

### Principle I — Human-Parity Information & Action Boundary (NON-NEGOTIABLE)

**Initial check: PASS. Post-design re-check: PASS.**

This feature creates a second actor that can touch the game. That is the whole Principle I exposure,
and it is answered in three places: what the actor may *do*, what it may *know*, and what the record
says it *was*.

| Obligation | How this design satisfies it | Where |
|---|---|---|
| Every recovery action that touches the game is one a human could perform through the standard UI | Rung 1 performs only the human action registered for that view; Escape and a click on a control the prompt is already offering qualify, a debug call does not, however convenient | FR-046, SC-007 |
| Every such action is declared in the catalog with its parity basis | The dismissal is a declared action like any other; an undeclared game-touching path has no route to the executor | FR-046, FR-049 |
| The watchdog never becomes a second, undeclared player | **Agent first, then watchdog.** A view carrying a choice — including a single acknowledging button — is routed to the agent as a declared prompt and answered as a recorded decision. Only if the agent does not act within a bounded window, or is unavailable, does the harness dismiss it | FR-047, SC-010 |
| A watchdog dismissal is never presented as agent play | It is a **labelled intervention** that counts as nothing toward the agent's decision coverage and appears in **no** measure of what the agent played | FR-047, SC-010 |
| Diagnostics never reach the agent | Detection, classification and recovery reads are harness diagnostics; they do not enter the agent's context, and a stall is never presented to the agent as game information. The agent is never told it was stuck | FR-048, SC-020 |
| Liveness records carry no game state | A confirm tick carries the *static catalog predicate expression* — the shape of the question — never its bindings or a `last_read` quotation. A provider tick carries provider, model, elapsed and bound — never prompt or response content, built from values held before the call starts | FR-048, research R3 |
| No signal is named for something it does not observe | Every registered signal must state what its presence does **not** establish, audited per release | FR-002, SC-023 |

**New surfaces introduced by this plan, with their parity basis** — the Development Workflow
section's specific requirement:

| New surface | Kind | In-client human equivalent |
|---|---|---|
| **Dismiss a registered blocking view** (declared action; rung 1) | Action | Pressing Escape, or clicking the one button the view is already showing. The parity basis is unusually tight: the harness may only perform the action the registered view itself offers |
| **Engine availability read for a refused action** (rung 2's independent read) | Observation | Seeing whether the end-turn button is enabled, or whether the order is offered in the UI |
| **Engine turn-number read at a reconciliation checkpoint** | Observation | Reading the turn counter on the HUD |
| **Session/view-presence probe** where the build exposes one | Observation | Seeing which screen is on the monitor — this is spec 002's existing screen-identity observation, reused, not a new capability |
| **Run-liveness evidence for a lock claim** | Out-of-game | Not a game surface at all: the run's own lifecycle state in the store and its liveness stream. Recorded as harness provenance, never placed in the agent's context |
| **Liveness ticks** (provider, confirm loop, monitor pass) | Out-of-game | Not a game surface. Harness telemetry under FR-048, filtered from context like model identity or cost |

**One judgement recorded because it looks like a Principle I relaxation and is not.** FR-047's
"then watchdog" clause lets a non-agent actor dismiss a view that carried a player choice. The
drafted requirement said *never*; the owner's 2026-09-22 ruling superseded it. The ordering is
load-bearing in **both** directions and a future contributor must not collapse it to either extreme:
the agent goes first because letting the watchdog answer a choice makes it a second undeclared
player, which Principle I does not permit at any quality of intent; the watchdog goes second rather
than never because a view only the agent may answer, when the agent is wedged or gone, is an
indefinite freeze — the case that cost hours on the day this was specified. The **separate
labelling** is what keeps both true at once: the board gets unstuck and the play record stays honest
about who did it. This is a scoped, recorded, auditable exception, not a caveat, and SC-010 makes
any leak of it into agent-coverage metrics a finding.

### Principle III — Complete Match Telemetry

**Initial check: PASS. Post-design re-check: PASS.**

- **No new game-state write path bypasses turn-by-turn persistence.** This feature introduces
  exactly one class of game-touching write — a recovery rung's action — and every rung attempt,
  skip, success and failure is a `RunEvent` persisted **before the next action is taken** (FR-040).
  `run/turn_cycle.py` remains the only component permitted to advance the harness past a turn, and
  that advance is still sequenced strictly after a successful `MatchStore` commit. A rung does not
  advance a turn; rung 4 *abandons* an attempt and replays it under spec 002 FR-045 – FR-047, which
  is the existing write-before-advance discipline invoked, not a second path.
- **Liveness ticks are deliberately not store writes, and that is not a bypass.** A tick is not
  game state and asserts nothing about what happened in the game; it is harness telemetry about
  whether a phase is speaking. Nothing is reconstructed from a tick, nothing downstream reads one,
  and no assertion about the match rests on one. The *conclusions* drawn from ticks — a stall
  candidate, a classification, a rung — are all store writes.
- **The record is never edited to agree with the engine.** This is the sharpest Principle III
  obligation in the feature and it is stated as a refusal: automated recovery applies **only** to
  divergences that stopped play; a non-stalled divergence is recorded and surfaced, never
  auto-corrected (FR-022). A harness that rewrites its own turn record produces records
  indistinguishable from correct ones, so the very failure this feature exists to make visible would
  become invisible again. Corrections are **additive records naming what they contradict** (FR-023),
  reusing spec 002 FR-047's abandoned/authoritative pattern. SC-015 makes any finding
  release-blocking.
- **Gaps and inconsistencies are both visible.** `record_completeness_status` today means "no
  gaps"; after this feature an unresolved divergence incident must also set it, so Principle III's
  bar applies to a run that is *complete but internally inconsistent* (FR-024). This is called out
  because it is a value whose meaning changes, which is the kind of change that otherwise leaves a
  name asserting something it no longer means.
- **A run never comes to rest claiming it is still progressing.** A run left in `playing` after its
  client died is a lie the record tells, and it is how a stranded run went unnoticed for hours
  (FR-044, SC-013). A non-terminal resting state such as `paused` is acceptable **only** with a
  recorded reason. `StopResolution`'s five members — `TURN_REACHED`, `VICTORY`, `DEFEAT`,
  `OPERATOR_STOP`, `UNRECOVERABLE_FAILURE` — already cover the *why*; **no sixth member is
  proposed**, because what was missing when a run stranded was never the member but the detail.
  FR-037's "last-known good save and the highest rung reached" rides on `UNRECOVERABLE_FAILURE` as
  recovery detail.
- **Schema.** Event and incident records are additive within the existing schema version; no
  historical-trend query loses readability, so the constitution's migration clause is not triggered.

### Principle II — Firetuner-First, Skill-Extensible Harness

**PASS, with one documented gap — inherited, not newly created.**

Rung 1's dismissal is delivered by synthetic keyboard/mouse input, which is a bespoke path. It is
permitted because the gap is documented and measured: no Lua API reachable from `InGame` fires a
control's registered callback — `control:CallCallback(Mouse.eLClick)` returns without error and does
nothing, and `UI.RespondToPrompt` exists in none of Firaxis' 645 shipped Lua files. Spec 002 already
carries this as the live C1 deviation and already declares `prompts.orders` as `path: bespoke` with
the gap recorded verbatim.

What this feature adds is **not a new bespoke mechanism but a new obligation on it**: the FR-018
registry must record, per view, whether the input path can reach it **on this host**. Two cases are
already measured as unreachable — the post-defeat exit-confirm modal ignores synthetic input, and a
Wayland session blocks synthetic input by design — and both must be reported at preparation under
FR-039 rather than discovered mid-stall.

**The enforcement already exists and is inherited unchanged.**
`tests/contract/test_synthetic_input_declaration.py` re-derives every in-harness
`HostPlatform.send_input` call site from the source with `ast` and asserts the capability it serves
is `path: bespoke` with a non-empty `firetuner_gap`. A new dismissal call site that shipped
undeclared would be caught by a test this feature did not have to write. Firetuner-first still holds
*within* the capability: where a direct call exists it is the primary route and the click is the
fallback, with `path`/`path_reason` recording which ran and why.

### Principle IV — Reproducible, Seeded Experimentation

**PASS.** This feature selects *when* to invoke deliverable 2's existing discipline; it does not
redefine it. Rung 4 reloads the turn's own start quicksave (spec 002 FR-007) and replays under the
abandoned/authoritative pattern (FR-047), so a branch point is never destroyed by a recovery and a
replayed turn is never mistaken for a first attempt. The discarded model spend is attributed to the
stall (FR-043), which keeps a recovered run's cost comparable to an uninterrupted one rather than
silently inflated.

One strengthening worth naming: **a lock is never force-taken from a live owner** (FR-013, FR-032,
SC-012). Two runs interleaving into one client would produce two runs whose recorded starting
conditions are correct and whose actual play is entangled — incomparability that nothing in the
record would reveal, which is precisely the hazard this principle exists to prevent.

### Principle V — Guidebook-Before-Optimization Gate

**NOT TRIGGERED.** This feature runs no Monte Carlo search, ablation, or optimization. It does
strengthen the gate's *input*: FR-024 makes a run carrying an unresolved divergence identifiable as
unfit for trending, so when deliverable 4 arrives it can exclude internally inconsistent runs rather
than only gapped ones.

### Principle VI — Shared, Unified Observability

**PASS, with the boundary held rather than moved.** This feature produces records; deliverable 1
presents them. The operator surface gains exactly one capability — FR-017's human assertion of a
stall and authorisation of a named rung — which is **lifecycle control**, the category FR-053
already permits it, and it binds loopback only like everything else there.

The per-run NDJSON liveness stream is the one new readable artifact, and it is deliberately **the
same stream for both audiences**: the directing session and the user read the same records from the
same file. That is Principle VI's requirement applied to the new surface rather than an exception to
it. A human-initiated recovery is labelled as such (FR-017) so that neither audience can mistake it
for an autonomous one.

### Principle VII — Provider-Agnostic Model Access & Resilience

**PASS on the provider half. The resilience half is why this feature exists, and the honest verdict
is PASS-WITH-A-STATED-DEPENDENCY.**

- *Provider-agnostic*: untouched. The streaming change of research R2 is a transport mode on the
  existing OpenRouter HTTP adapter; no vendor SDK enters the decision path, the port still returns
  exactly one decision per call, and model choice remains configuration.
- *Detect crashes*: strengthened. Existing detection is kept as the signal source and gains a
  classification that can distinguish a dead client from a held board from a harness that blocked
  itself (FR-009 – FR-012). A client death that arrives as a bare transport error now reaches a
  recorded terminal state with a reason (FR-044, FR-045, SC-013) — FR-045's rule that fault handling
  be keyed on the **condition** rather than a chosen exception family generalises an instance
  already found and closed on this project, where purpose-built pre-save fault handling had never
  once run because a dead socket raises a system error while every handler was keyed on the
  harness's own error family.
- *Preserve the last-known save/state*: satisfied by rung 4 over the existing per-turn quicksave.
- *Resume **or restart** the run*: **restart is the stated dependency.** No method anywhere in the
  harness launches, restarts, or terminates the Civilization VI client or the tuner, on any
  platform. Verified for this plan rather than taken from the spec: `host/port.py`'s `HostPlatform`
  declares nine methods and none is a lifecycle method, and a search of `src/civsim_harness/` for
  any definition matching launch/restart/terminate/kill/spawn/start-client/stop-client returns
  nothing. The search could have matched, which is what makes the negative result evidence.

  Rung 5 is therefore **declared unavailable**, reported at preparation, and the ladder stops at
  rung 4 in a recorded failed state (FR-034, FR-037, SC-022). The clause of Principle VII that says
  "restart the run" is **unimplementable, not merely unimplemented**, and this plan does not paper
  over it. This is recorded in Complexity Tracking as **C2** rather than claimed as compliance.

**Gate result: PASS — proceed.** No unjustified violations. Three justified items are recorded in
Complexity Tracking (C1 — an inherited bespoke input path; C2 — Principle VII's restart clause
unsatisfiable on any host; C3 — liveness ticks outside the store), and one **sequencing gate** is
binding rather than advisory: **nothing below implementation phase 1 may be enabled until the
emission roster is clean.** If FR-003 is not implemented, FR-005 must not be either; the two ship
together or the watchdog becomes the timeout the owner rejected.

---

## Implementation sequencing

**A naming note, because the collision is real.** Speckit calls research "Phase 0" and design
"Phase 1"; this document is their output. The phases below are **implementation** phases and are
numbered independently. Where this plan says "phase 1" without qualification it means **emission**.

The order is forced by two gates and one preference:

- **Gate A (hard, spec-level):** nothing that acts on silence may exist before the phases emit.
- **Gate B (hard, spec-level):** no detector or rung is accepted without its injected-real-failure
  fixture *and* its negative control *and* a demonstration of its check failing against a broken
  build.
- **Preference:** rungs land in ladder order, so that at every intermediate state the most
  destructive thing the system can do is the least destructive thing that has landed.

### Phase 1 — Emission (the precondition)

**Delivers**: FR-003, SC-026, SC-027, and the enumeration obligation.
**Gate**: no silence bound, no classifier, no rung above report-only may be enabled until this
phase's roster is clean.

1. Enumerate every phase that can outlast the 180 s ceiling, as a **rostered data file with
   per-entry prose** (the shape `tests/contract/test_gate_inputs.py` already uses), re-run per
   release. The two known entries are the model-provider wait and the end-turn confirm loop; the
   candidate set to sweep is wider — save/load round trips, capture, the heartbeat's own bounded
   wait, `ProviderChain` fallback across attempts, the store's open-time orphan sweep, and every
   `while`/`sleep` in `run/`, `act/`, `saves/`, `nexus/`.
2. Make each enumerated phase emit a **periodic tick while waiting**, at an interval shorter than
   the bound. Bracketing is explicitly not acceptance: a phase that logs at entry and exit is silent
   for exactly the interval that matters.
3. **Verify streaming empirically** — observe the stream growing monotonically *while the phase
   runs*, not merely confirm an emit call exists. An emission that buffers and flushes at completion
   is the same defect wearing a fix.
4. Classify each signal as **work-derived** or **observer-derived** (research R2) and record the
   classification in the roster. A phase whose only signal is observer-derived is **not** discharged
   by this phase; it is recorded as such, and provider silence yields *undetermined* rather than a
   stall until a work-derived signal exists for it.

**Overlap with spec 002, and how the two are reconciled rather than duplicated** — this is live work
in the shared tree right now, uncommitted:

| In flight on 002 | Disposition here |
|---|---|
| `act/liveness.py` — per-attempt confirm-loop record, wired into `act/verify.py::confirm_execution` | **Adopt unchanged.** It is work-derived: each iteration completes a real tuner round trip, so an emitted iteration *proves* the tuner answered. Register it and move on |
| `provider/liveness.py` — daemon ticker beside the blocking call, wired into `provider/openrouter.py` | **Adopt, register honestly, and supplement.** It is observer-derived. It bounds the gap between records, which is worth having; it does not establish that the call is progressing, and must be registered saying so |
| `tests/contract/test_long_phase_liveness.py` (named by both docstrings, absent from the tree) | **Becomes the SC-026 roster's home**, extended from two entries to the enumeration |

**The rule that keeps the lanes from colliding**: *002 writes the emitters, because it owns the
provider layer, the turn cycle and the confirm loop. 004 owns the obligation, the enumeration, the
registry, the reader, and the refusal to enable the bound until the roster is clean.* Three
coordination items need settling by the hypervisor rather than by either lane: the in-flight work
cites task id `T290`, which in `specs/002-civ-playing-harness/tasks.md` already means a different
task (highest allocated is T294) and the emission work has no task of its own; the modules are
uncommitted, so nothing about them is verified under the clean-worktree rule; and the contract test
both docstrings name does not exist yet.

### Phase 2 — The proving apparatus

**Delivers**: FR-054 – FR-059, SC-004, SC-017, SC-018, SC-019.
**Gate**: every acceptance check from here on passes through it.

Built second, before the first detector, because FR-057 applies to *every* acceptance check in the
feature — including phase 1's. It has a real subject from day one: SC-027's "verified empirically to
stream" is exactly a check that must be demonstrated failing against a non-streaming build, so the
apparatus is exercised against phase 1's work and then retro-applied to it.

1. **The fixture/negative-control pair as one artifact.** A detector without a demonstrated negative
   control is not acceptable. For reconciliation checks the control must be *a correct system a
   naive version of the check would call diverged* — the same-command readback is the worked
   example, and it is buildable today from two recorded measurements.
2. **The mutation obligation.** Each check is demonstrated red against a deliberately broken build,
   with the demonstration recorded beside the check.
3. **Reachable *and able to fire*.** `tests/contract/test_reachability.py` already catches a symbol
   with no caller. That is too weak here: the day's most serious finding was a mechanism whose
   production call site existed and passed an empty default. The check extends
   `test_gate_inputs.py`'s rule to detectors and rungs — some production call site must supply the
   inputs that make it *capable of firing*.
4. **No inert construction** (FR-059). Detector and rung inputs are required with no defaults; a
   missing input is a startup failure, never a silent no-op.

Spec 002 research R21 records, with its measurements, that a broad syntactic scan for this family
was built and **rejected on evidence** — three formulations, 24/32/43 hits, none catching either
motivating finding. That negative result is inherited: a checker here keys on **meaning** and on the
`src/`-versus-`tests/` partition, never on a signature.

### Phase 3 — Signals, the interrogating reader, and rung 0

**Delivers**: FR-001, FR-002, FR-004 – FR-008, FR-029, FR-040, SC-002, SC-023.

The silence bound becomes enforceable here, and it can, because phase 1 landed.

1. The **signal registry**: every signal registers the event it directly observes, its source of
   truth, how often it can change, what its **absence** establishes, and what its **presence does
   not** establish. A signal may not be named for a condition it does not directly observe.
2. The **reader**, which *interrogates* — reads the liveness stream, queries state, observes
   in-flight work — rather than watching a clock. It records the observed interval between
   occurrences of each signal, **per run**, so a run whose own history supports a tighter bound than
   180 s is held to the tighter one (FR-004).
3. **Unavailable ≠ absent** (FR-006). A signal source the reader cannot reach is *instrument
   failure*: reported as unavailable, never as "no progress", and it pushes toward *undetermined*.
4. **The monitor's own observability** (FR-008): the monitor publishes a work-derived record at the
   end of each pass that actually completed its reads, into the same stream, so a dead monitor is
   distinguishable from a quiet run by a reader outside the process.
5. **Rung 0 — observe and report.** Always available, never destructive, and the only rung eligible
   under *undetermined*.

**One naming decision belongs here rather than in the data model, because getting it wrong would
quietly undo a correction spec 002 already made.** `RunEventType` records in its own docstring that
a `stall` member was **removed**, because *"it denoted a turn exceeding its time budget, and there
is no time budget any more"*. Reintroducing a member called `stall` for the candidate would read, to
any future contributor, as the time budget coming back — and this feature's entire premise is that
the bound measures **silence, not duration**. The candidate event is therefore
`liveness_silence_observed`, named for what it directly observes (FR-002); `stall_classified`
carries a verdict, not a duration; and the removed member stays removed.

At the end of phase 3 the system converts hours of silence into a named, timestamped incident and
does nothing else. That is US1's floor and, per the spec, most of the time and tokens being lost.

### Phase 4 — Classification

**Delivers**: US1 in full — FR-009 – FR-018, FR-048, FR-050 – FR-053, SC-003, SC-005 (detection
half), SC-011 (classification half), SC-020.

1. The five dispositions, produced **before** anything acts, exactly one per candidate.
2. **At least one observation independent of the component under suspicion** (FR-010) — the
   harness's belief checked against the engine's own answer. A liveness check that consults only
   harness state structurally cannot see the case where the harness is the problem, which is the
   most dangerous of the three classes.
3. **Sub-classification of *unreachable*** before any rung is eligible (FR-012): lock held by
   another party; connection refused inside a prior close's ~2 s refusal tail; client process
   absent; client present but not answering. One symptom has had several causes on one day.
4. **Probe discipline**: bounded, never consuming the run's only channel, cheap enough not to be a
   cause, recording its own load; a probe's own failure is evidence for *unreachable* and **never**
   the game's answer; a missing engine method is detected at preparation and means *unknown*, never
   *no*.
5. **Human assertion** (FR-017): an operator may assert a stall and authorise a named rung without
   waiting for the detector, recorded as human-initiated and excluded from any measurement of the
   detector's own latency.

### Phase 5 — The ladder

**Delivers**: US3, US4, US5 — FR-027 – FR-039, FR-041 – FR-047, FR-049, SC-001, SC-005 (recovery
half), SC-006 – SC-013, SC-021, SC-022, SC-024, SC-025.

Landed in ladder order, so the most destructive thing that can happen at any intermediate state is
the least destructive thing that has landed.

| Step | Rung | Notes |
|---|---|---|
| 5a | **1 — dismiss a registered blocking view** | Needs the blocking-view registry and a **declared dismissal action with a parity basis** — which does not exist today: synthetic input is reachable only from two hard-wired call sites. Verified by an independent re-read showing the view closed, never by the call's return value |
| 5b | **2 — re-derive the harness's belief from the engine** | The stale belief is **replaced by a value freshly observed in a separate command** — not cleared, not reset to a default. Clearing a flag makes the symptom go away without establishing what is true. Detected at the **first** refusal, not the eighth |
| 5c | **3 — re-establish the path to the game** | Selected by the FR-012 cause. Includes the lock fix: ownership resolved by whether **the run** that holds it is live, never by age, mtime, or the liveness of a process the lock merely names |
| 5d | **4 — reload and replay** | The existing recovery becomes one rung. Its precondition is named for **a fresh main menu versus a reused one**, which is what actually governs the refusal — "post-defeat" is a correlate, and a rung named for the correlate fires in the wrong place. Where the precondition does not hold the rung is **skipped with the reason recorded**, not attempted and retried |
| 5e | **5 — restart the client** | **Declared unavailable.** Reported at preparation; the ladder stops at rung 4 in a recorded failed state |

Cross-cutting within this phase: escalation is monotonic and the ladder never cycles (FR-035); a
repeated identical stall escalates rather than repeats; recovery is bounded (FR-037); after any
successful recovery the run resumes from a **freshly assembled observation** and never on state read
before the stall (FR-038, SC-024); and stall wall-clock and discarded model spend are attributed as
they are lost (FR-042, FR-043).

### Phase 6 — Reconciliation beyond the stall

**Delivers**: US6 — FR-019 – FR-026, SC-014, SC-015, SC-016.

Last because it changes no run's behaviour; it makes a class of silent corruption visible. It is
also what keeps the detector from being built too narrowly to catch two of the three measured
instances.

1. **Checkpoints**: per refused action, per turn boundary, per run end.
2. **The independent read gets its own type**, which cannot be constructed from the same tuner
   command as the write it verifies, and a comparison returns `agreed` / `diverged` /
   **`unverified`** — three outcomes, not two. A comparison whose read was not independent is
   `unverified`; collapsing it into `diverged` manufactures incidents on healthy runs, and into
   `agreed` hides real ones.
3. **Record, never correct.** A divergence is an incident whether or not play stopped; a
   non-stalled divergence gets no rung; no existing record is amended; corrections are additive.
4. **Artifact authority**: the store is authoritative; the timeline and any driver result output are
   reconciled against it at the end of every run, and disagreement is an incident rather than a
   silent resolution.
5. **Aggregation** (SC-014): stall and recovery cost readable per run *and summable across runs*,
   separately from time legitimately spent thinking. Today the project records per-incident costs
   and no aggregate at all, so "how much have freezes cost us" is unanswerable from the record.

### What is deliberately not in any phase

- **Auto-correction of a divergence that did not stop play.** Forbidden (FR-022, SC-015). A future
  contributor must not "finish" this feature by adding it.
- **Any wall-clock bound on a turn.** Forbidden (FR-007; spec 002 FR-008, FR-014).
- **Widening the ceiling to fit a silent phase.** Forbidden (FR-003).
- **Applying the watchdog outside harness game runs.** Forbidden (FR-005 scope clause).

---

## Project Structure

### Documentation (this feature)

```text
specs/004-liveness-reconciliation-recovery/
├── plan.md                          # This file (/speckit-plan output)
├── research.md                      # Phase 0 output
├── data-model.md                    # Phase 1 output
├── quickstart.md                    # Phase 1 output
├── contracts/                       # Phase 1 output
│   ├── README.md
│   ├── liveness-signal.md           # What a phase must publish, and what each kind may be read to mean
│   ├── stall-classification.md      # Dispositions, evidence, and the undetermined rule
│   ├── recovery-rung.md             # The ladder's declaration shape and availability reporting
│   ├── reconciliation-check.md      # Independent reads, three outcomes, artifact authority
│   ├── blocking-view-registry.md    # The FR-018 registry, as versioned data
│   └── client-lifecycle-port.md     # The capability that does not exist, specified so rung 5 is buildable when it does
├── checklists/
│   └── requirements.md              # Existing — spec quality gate
└── tasks.md                         # Phase 2 output (/speckit-tasks — NOT created here)
```

### Source Code (repository root)

```text
catalogs/
├── actions/
│   └── recovery.yaml                # NEW — the declared dismissal action, path: bespoke with its gap
└── views/
    └── blocking_views.yaml          # NEW — the FR-018 registry as versioned data

src/civsim_harness/
├── watchdog/                        # NEW — the whole of this feature's own logic
│   ├── signals.py                   #   Registry: what each signal observes, and what it does NOT establish
│   ├── stream.py                    #   The per-run append-only liveness stream (write + read)
│   ├── monitor.py                   #   The interrogating reader; publishes its own work-derived pass record
│   ├── classifier.py                #   Five dispositions; undetermined first-class
│   ├── ladder.py                    #   Rung declarations, ordering, monotonic escalation, host availability
│   ├── rungs/                       #   One module per rung; rung 5 present as a declared-unavailable stub
│   └── reconcile.py                 #   Checkpoints, IndependentRead, agreed/diverged/unverified
├── act/
│   ├── liveness.py                  # IN FLIGHT on 002 — confirm-loop tick; adopt + register
│   └── verify.py                    #   confirm_execution's poll loop, which now emits per attempt
├── provider/
│   ├── liveness.py                  # IN FLIGHT on 002 — observer-derived ticker; adopt + register honestly
│   ├── openrouter.py                # EXTEND — request the completion as a stream (research R2)
│   └── chain.py                     #   fallback across attempts: a long phase in its own right
├── resilience/                      # EXTEND — existing detection becomes the signal source, not the verdict
│   ├── detector.py                  #   DetectionAggregator feeds classifier.py; its four outcomes stop being the output
│   ├── heartbeat_monitor.py         #   OK / HANG / CONNECTION_LOST — a signal, not a disposition
│   └── recovery.py                  #   RecoveryEngine becomes rung 4 rather than the whole ladder
├── run/
│   ├── identity_lock.py             # EXTEND — reclaim on RUN liveness, not on client-process liveness
│   ├── detection.py                 # EXTEND — the monitor rides DetectionWatch's existing 10 s cadence
│   ├── decision_loop.py             #   END_TURN_CONFIRM_* constants; MAX_UNPRODUCTIVE_REPLAYS composes with the ladder
│   ├── preparation.py               # EXTEND — report available rungs; detect missing engine methods
│   └── orphans.py                   # EXTEND — reads lock inspections; must not read pid_alive as ownership
├── models/records.py                # EXTEND — new RunEventType members, named for silence not for duration
├── store/                           # EXTEND — event/incident records; completeness reflects divergence
└── operator/                        # EXTEND — FR-017 human assertion (lifecycle control only, loopback)

tests/
├── unit/                            # Registry invariants, disposition selection, ladder ordering, lock ownership
├── contract/
│   ├── test_long_phase_liveness.py  # NEW — the SC-026 roster
│   ├── test_watchdog_reachability.py# NEW — reached AND able to fire (FR-058, FR-059)
│   ├── test_reachability.py         # EXISTING — inherited unchanged
│   ├── test_gate_inputs.py          # EXISTING — its rule is extended to detectors and rungs
│   └── test_synthetic_input_declaration.py  # EXISTING — catches an undeclared dismissal call site for free
├── integration/                     # Injected-real-failure fixtures + negative controls, at the real seams
└── live/                            # Stranded diplomacy view, real client death — LIVE LANE OWNS
```

**Structure Decision**: one new subpackage, `watchdog/`, holding everything that is this feature's
own — registry, stream, monitor, classifier, ladder, rungs, reconciliation — and **extensions**
everywhere else. The split is drawn so that the question *"what does this feature add?"* is
answerable by reading one directory, which is the same reasoning that puts the parity boundary in
`catalogs/` as data and every OS-specific import in `host/`.

`resilience/` is deliberately **not** absorbed. It is the signal source and stays one; what changes
is that its four outcomes stop being the system's answer and become inputs to one. Collapsing the
two would make it impossible to tell, at review, whether a disposition came from a probe noticing
something or from a classifier concluding something — and that distinction is the feature.

The two new `catalogs/` files are data rather than code for the same reason the rest of the catalog
is: an auditor satisfying SC-007 and SC-023 should read declarations, not trace control flow.

---

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| **C1** — A bespoke synthetic-input path used by a recovery rung, alongside Principle II's Firetuner-first rule. *(Inherited, not newly created: this is spec 002's live C1, extended to a new caller.)* | Rung 1 is the cheapest and least destructive recovery and answers the class that cost the most wall-clock on the day of specification. Its action — press Escape, click the one button the view is already offering — has a measured Firetuner gap: no Lua API reachable from `InGame` fires a control's registered callback, and `UI.RespondToPrompt` exists in none of Firaxis' 645 shipped Lua files. | **Pure Firetuner** rejected because the capability is measured absent, not awkward. **No rung 1 at all** rejected because the alternative recovery for a held board is rung 4 — reload and replay — which destroys the in-progress turn to clear a view that one keypress clears; ordering the ladder by destructiveness is the whole safety argument and skipping the cheap rung inverts it. Mitigation is inherited rather than invented: the path must be declared `path: bespoke` with its gap recorded, and `test_synthetic_input_declaration.py` `ast`-derives every call site so an undeclared one cannot ship. |
| **C2** — **Principle VII's "restart the run" clause cannot be satisfied on any host, and this plan ships a ladder that stops one rung short of it.** | No method anywhere in `src/civsim_harness/` launches, restarts, or terminates the Civilization VI client or the tuner; `HostPlatform` declares nine methods and none is a lifecycle method. Verified for this plan by a search that could have matched. Rung 5 depends on that capability. | **Building the capability inside this feature** rejected because it is a host-layer capability needing three platform implementations and a separate safety argument about when ending a live client is data-safe — a second deliverable's worth of work, gated on measurements nobody has taken. **Assuming it and escalating toward it** rejected outright: FR-039 and SC-022 exist because a run that escalates toward a rung its host cannot perform converts a recoverable stall into a failed one while reporting that it tried everything. **Declaring the ladder complete at rung 4** rejected as dishonest — the gap is recorded as a dependency and reported at preparation, so the record says the ladder is four rungs tall on this host and why. |
| **C3** — Liveness ticks are written to a telemetry log and a per-run NDJSON stream rather than to the match store, which Principle III makes the home of everything else this feature records. | A tick every few seconds for the length of a run is write amplification against a store with a no-delete/no-edit floor, and — decisively — the store's own write path is one of the things that can wedge. A signal that requires the suspect component to be healthy cannot testify about it. | **Ticks as store rows** rejected on both counts above. **Ticks in memory only** rejected because FR-008's dead-monitor case and SC-027's "observed growing while the phase runs" both need an out-of-process reader. **A mutable last-heartbeat file** rejected because it is not a stream: `st_mtime` cannot distinguish "still ticking" from "one tick and a clock that moved", whereas an append-only stream's monotonic byte growth is directly observable and is exactly what SC-027 asks for. The boundary is drawn at *conclusions*: a stall candidate, a classification, a rung attempt and a divergence incident are all store writes (FR-040), and nothing downstream ever reconstructs a fact about the match from a tick. |

**One judgement recorded because it looks like a deviation and is not.** This plan proposes changing
the decision-path provider call to **streaming** (research R2) in order to obtain a work-derived
liveness signal. That touches the most parity-sensitive path in the harness, so it reads like a
complexity item. It is not a deviation from any principle: the provider port's contract is unchanged
(exactly one decision per call), no vendor SDK enters the path, and nothing about what reaches the
agent changes. It is listed here only so that nobody discovers it in a diff. It *is* flagged for the
owner, because a change to the decision path deserves a decision rather than an implementation note.
