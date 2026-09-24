# Feature Specification: MCP Harness Pivot — Audit `civ6-mcp` as a Replacement for the Hand-Built Driver Layer

**Feature Branch**: `live/linux`

**Created**: 2026-09-24

**Status**: Draft

**Input**: User description: "the codebase here has changed dramatically, and it changes both the base of the all the specs, it also completely invalidates the codebase so far. https://github.com/lmwilki/civ6-mcp — This mcp protocol *is* full game programmatic control of civilization 6. We can rely on this and not hand wrap it as defined in the spec and failed to implement autonomously. This is something I think we can do in one session: fork and test civ6-mcp to audit its effectiveness inside of a more basic agent harness just wired up for mcp. If we like it, prep a context file for next turn to clean out the repo we have so far of our own drivers and software. Remember to update in the GitHub issues channel to keep me informed and to regularly take screenshots."

---

## Context: Why This Feature Exists

An external, MIT-licensed project (`lmwilki/civ6-mcp`) independently solved the problem this project has been hand-building for weeks, and appears to have solved more of it. It reached the same game-control substrate by the same route, and then went considerably further along it.

The measured gap, stated honestly, is the entire justification for this feature:

| Dimension | This project, measured 2026-09-22 | Upstream `civ6-mcp`, claimed |
|---|---|---|
| Game actions working against a live client | **11 of 41** (18 attempted while available) | 76 tools across the full gameplay loop |
| Actions found structurally impossible | **14 of 38** (28 invented accessor names) | — |
| Platform tiers | Linux live; Windows/macOS unvalidated | Windows, macOS, Linux all exercised |
| Full games completed | **0** (best: game turn 59, by defeat) | 300+ turn games, multi-model benchmark |

This feature does **not** assume the replacement is correct. It is an **audit with an explicit go/no-go gate**. The project's constitution makes two properties non-negotiable that a third-party benchmark harness has no reason to have honoured — Principle I (human-parity information and action boundary) and Principle III (complete match telemetry) — and a component that fails Principle I must be blocked rather than shipped with a caveat. The purpose of this feature is to answer, on evidence rather than on enthusiasm, whether adopting the upstream component is the right move, and to leave behind a decision record either way.

The cleanup of this repository's own driver and harness code is **explicitly out of scope here**. This feature produces the *plan* for that cleanup as a gated deliverable; a later session executes it.

---

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Prove the replacement can actually play the game (Priority: P1)

The owner needs to know whether the upstream component genuinely delivers programmatic control of Civilization VI on this machine, or whether its capability claims dissolve on contact with a real client the way this project's own claimed action surface did. He needs this demonstrated by a model actually playing, in the simplest possible harness — an agent wired to the MCP server and nothing else — so that what is being measured is the component, not this project's accumulated scaffolding.

**Why this priority**: Every other question is downstream of this one. If the component cannot drive a live game on this host, the constitutional audit, the decision record, and the cleanup plan are all moot. It is also the single cheapest way to falsify the premise, so it runs first.

**Independent Test**: Stand up a minimal agent session connected only to the MCP server, point it at a running game, and instruct it to play. Success is observable directly on screen and in the resulting turn record — no other part of this feature need exist.

**Acceptance Scenarios**:

1. **Given** Civilization VI is running on this host with the tuner interface enabled and a game loaded, **When** the minimal MCP-wired agent harness is started and instructed to play, **Then** the agent orients itself, issues game actions, and advances the game by at least one complete turn without operator intervention.
2. **Given** the agent is playing, **When** a contiguous block of turns is run, **Then** the game advances by at least 10 consecutive turns with no operator keyboard or mouse input during the block.
3. **Given** a completed play block, **When** the actions the agent successfully applied are tallied, **Then** the count of distinct game actions applied to a live client exceeds this project's measured baseline of 11.
4. **Given** a play block is running, **When** the operator looks at the screen, **Then** the game is visibly being played — units moving, cities producing, turns ending — and screenshot evidence of this is captured at regular intervals and published to the project's reporting channel.
5. **Given** an action the agent issues is rejected or fails, **When** the failure surfaces, **Then** the agent receives an intelligible reason and can act on it, rather than the failure being silently discarded.

---

### User Story 2 - Establish whether the replacement can be trusted under the constitution (Priority: P1)

The owner needs a per-capability verdict on whether the upstream component respects the project's non-negotiable boundaries — above all that the agent receives only information and actions a human player could obtain through the standard game UI. An external benchmark harness had no obligation to honour this, and at least one of its capabilities (an arbitrary game-scripting escape hatch exposed directly as an agent-callable tool) is a boundary violation by construction. The owner needs to know exactly which capabilities are clean, which are violations, and which violations are remediable, before any decision to adopt.

**Why this priority**: Co-equal P1 with Story 1, because a component that plays brilliantly and leaks hidden state is not adoptable at any level of capability — the constitution admits no partial compliance here. Discovering this after the cleanup would be far more expensive than discovering it now. It is separated from Story 1 only because it is independently testable and could be run even if live play were blocked.

**Independent Test**: Enumerate every capability the component exposes to the agent and classify each against the human-parity boundary, citing the specific evidence for each verdict. Testable entirely from the component's own definitions plus the observed traffic of a play block; delivers a standalone conformance report.

**Acceptance Scenarios**:

1. **Given** the component's full capability surface, **When** the audit runs, **Then** every capability receives an explicit verdict of *clean*, *violation*, or *remediable violation*, with the evidence cited for each — and no capability is left unclassified.
2. **Given** a capability is judged clean on human-parity grounds, **When** its verdict is recorded, **Then** the record states what a human player would do in-client to obtain the equivalent information or effect.
3. **Given** the component exposes a capability that returns information no human player could obtain, **When** the audit encounters it, **Then** it is recorded as a violation regardless of how useful it is, and the record states whether it can be disabled without breaking the rest of the surface.
4. **Given** a play block has run, **When** the information actually delivered to the model during that block is examined, **Then** it is checked against the boundary as *delivered*, not merely as *declared* — closing the gap between what a capability is documented to return and what it returned in practice.
5. **Given** the audit is complete, **When** the conformance report is written, **Then** it states the verdict for each of the constitution's load-bearing principles separately, and names any principle it did **not** examine rather than implying full coverage.

---

### User Story 3 - Record a defensible adopt / reject decision (Priority: P2)

The owner needs the audit to terminate in a clear recommendation with its reasoning attached — adopt the upstream component, reject it, or adopt it subject to named remediations — so that the decision survives the session that made it and can be revisited if it turns out wrong. He needs to be kept informed as this happens through the project's existing reporting channel rather than having to ask.

**Why this priority**: The evidence from Stories 1 and 2 is only worth gathering if it is converted into a decision. Lower than P1 because it strictly depends on their output, but it is what makes the session's work durable.

**Independent Test**: Given the outputs of Stories 1 and 2, produce a decision record and confirm it states a recommendation, the evidence behind it, the conditions that would reverse it, and what was not examined. Reviewable on its own by someone who was not present.

**Acceptance Scenarios**:

1. **Given** the capability and conformance evidence, **When** the decision record is written, **Then** it states one of *adopt*, *reject*, or *adopt with remediations*, and lists the specific findings that drove it.
2. **Given** a recommendation to adopt, **When** the record is written, **Then** it names every remediation required before adoption and marks each as blocking or non-blocking.
3. **Given** any recommendation, **When** the record is written, **Then** it names what the audit could not determine, so that unexamined ground is not mistaken for cleared ground.
4. **Given** the audit is under way, **When** each significant finding lands, **Then** it is reported to the owner's reporting channel without waiting for the session to end.

---

### User Story 4 - Hand the next session a cleanup plan it can execute (Priority: P3)

If the decision is to adopt, the owner needs a self-contained context document that lets a fresh session remove this project's own driver and harness code without re-deriving the audit — stating precisely what is deleted, what is kept, what the retained parts must be re-pointed at, and what evidence must survive the deletion.

**Why this priority**: Deliberately last, and deliberately gated. Producing a cleanup plan before the gate passes would prejudge it. Lowest priority because nothing in this feature *executes* the cleanup; this story only has to make the next session cheap and safe.

**Independent Test**: Hand the document to a session with no prior context and confirm it can identify every file to remove, every file to keep, and every fact it needs, without reading this feature's working notes. Testable by inspection.

**Acceptance Scenarios**:

1. **Given** an adopt decision, **When** the cleanup context file is produced, **Then** it classifies every top-level area of this project's own code as *remove*, *keep*, or *re-point*, with a one-line reason for each.
2. **Given** the cleanup context file, **When** a fresh session reads it, **Then** it contains every operational fact needed to proceed without re-running the audit, including how to run the replacement on this host.
3. **Given** a reject or adopt-with-remediations decision, **When** the gate is evaluated, **Then** no cleanup file is produced and the reason is recorded instead.
4. **Given** the cleanup plan, **When** it names anything for deletion, **Then** it separately names the evidence, measurements, and operational knowledge that must be preserved out of that deletion.
5. **Given** the existing specs whose premises this pivot invalidates, **When** the cleanup plan is written, **Then** it states the disposition of each existing spec rather than leaving them silently stranded.

---

### Edge Cases

- **The game client cannot be launched.** The account this host plays on can be in a game on only one machine at a time, and launching here pre-empts the owner's own session. If the account is unavailable, the live half of the audit must be reported as *not performed* rather than estimated, and the static half must still complete and be delivered.
- **The component works but only outside the human-parity boundary.** A capability surface that is excellent and non-conformant is a reject, not a compromise. The decision record must be able to express "capable but inadmissible."
- **The component crashes the game client.** This project has already recorded a crash caused by calling a valid engine method from the wrong context. A crash during the audit is a finding about the component, not an audit failure, and must be recorded with enough detail to attribute cause.
- **A capability is documented but never exercised.** A capability that the audit never triggered is *unverified*, never *working*. The report must distinguish "observed working," "observed failing," and "not exercised," and must not let the third quietly join the first — this project has already shipped a false claim of exactly that shape.
- **Only one connection to the game is permitted at a time.** Any pre-existing harness process or debug tool holding the connection will make the replacement look broken when it is not. The audit must establish a clean field before concluding anything negative about connectivity.
- **The upstream component and this project's own harness disagree about the same game state.** A disagreement is a finding worth recording, since this project's own reading may be the wrong one.
- **The audit's own evidence is contaminated by harness UI.** Screenshot evidence delivered to a model must be free of debug and harness chrome, the same standard this project already holds itself to.

---

## Requirements *(mandatory)*

### Functional Requirements

#### Obtaining and isolating the candidate

- **FR-001**: The project MUST hold its own fork of the upstream component, so that the audited artifact is fixed, attributable, and modifiable independently of upstream changes.
- **FR-002**: The audit MUST record the exact upstream revision audited, so any later re-test is a comparison rather than a fresh guess.
- **FR-003**: The candidate MUST be exercised in isolation from this project's existing harness code, so that what is measured is the candidate and not this project's scaffolding.
- **FR-004**: The audit MUST NOT modify this project's existing harness, driver, or store code. Findings about that code are recorded, not acted upon, within this feature.

#### The minimal agent harness

- **FR-005**: The system MUST provide an agent harness whose only means of reading or affecting the game is the candidate component's exposed capabilities.
- **FR-006**: The harness MUST be runnable unattended for a contiguous block of turns, with no operator input during the block.
- **FR-007**: The harness MUST record every capability call it makes, the arguments, and the outcome returned, so the capability tally is derived from evidence rather than recollection.
- **FR-008**: The harness MUST record the reason for every failed or rejected action, rather than discarding it — the specific defect that hid this project's own most significant action failure for weeks.
- **FR-009**: The harness MUST allow the play block to be stopped cleanly by the operator at any point, leaving its record intact and the game client in a recoverable state.

#### Live capability audit

- **FR-010**: The audit MUST determine, for each capability the candidate exposes, whether it was observed working, observed failing, or not exercised — with these three states kept distinct in every report.
- **FR-011**: The audit MUST verify each applied action's effect by reading the game state back, rather than treating a call that returned without error as proof the action occurred.
- **FR-012**: The audit MUST capture screenshot evidence of the game at regular intervals during live play, and this evidence MUST be published to the owner's reporting channel as the audit proceeds.
- **FR-013**: The audit MUST record the number of consecutive game turns completed under agent control without operator intervention.
- **FR-014**: The audit MUST record any client crash, hang, or disconnection, with the conditions that preceded it.

#### Constitutional conformance

- **FR-015**: The audit MUST classify every capability the candidate exposes to the agent against the human-parity boundary, leaving none unclassified.
- **FR-016**: For every capability judged admissible, the audit MUST state what a human player would do in-client to obtain the equivalent information or effect.
- **FR-017**: The audit MUST identify every capability that exposes information a human player could not obtain, or permits an action a human player could not perform, and state for each whether it can be removed without breaking the remaining surface.
- **FR-018**: The audit MUST examine the information actually delivered to the model during live play, not only the declared behaviour of each capability — a capability's documentation is not evidence of what it returned.
- **FR-019**: The audit MUST assess whether the candidate's own record of a game is complete enough to satisfy this project's turn-by-turn telemetry obligation, and state what would be lost, gained, or duplicated relative to the existing match record.
- **FR-020**: The audit MUST assess whether the candidate's model access can be routed through this project's required provider-agnostic path, and what changes that would take.
- **FR-021**: The audit MUST assess how the candidate behaves when the game client crashes or a run is interrupted, against this project's requirement to preserve state and resume without silent data loss.
- **FR-022**: The conformance report MUST name every principle it did not examine, so that silence is never read as a pass.

#### Decision and handover

- **FR-023**: The audit MUST terminate in a recorded recommendation of *adopt*, *reject*, or *adopt with remediations*, with the findings that drove it cited.
- **FR-024**: Any recommendation to adopt MUST list its required remediations, each marked blocking or non-blocking.
- **FR-025**: The decision record MUST state what the audit could not determine.
- **FR-026**: The cleanup context file MUST be produced only if the decision is *adopt* or *adopt with remediations*; otherwise the reason for withholding it MUST be recorded.
- **FR-027**: The cleanup context file MUST classify every top-level area of this project's own code as *remove*, *keep*, or *re-point*, with a reason for each.
- **FR-028**: The cleanup context file MUST state the disposition of each existing feature specification whose premises this pivot changes.
- **FR-029**: The cleanup context file MUST name the evidence, measurements, and operational knowledge that must survive the deletion, separately from what is deleted.
- **FR-030**: The cleanup context file MUST be self-contained — a session reading it without this feature's working notes must be able to proceed.

#### Reporting

- **FR-031**: Significant findings MUST be reported to the owner's coordination channel as they land, not batched to the end of the session.
- **FR-032**: Every claim in every report MUST be marked as observed or not observed. A capability that was not exercised MUST NOT be reported as working.
- **FR-033**: The audit MUST announce its hold on the shared game account before taking it, and release it explicitly when done.

### Key Entities

- **Candidate Component**: The forked third-party game-control server under audit. Identified by fork location and exact upstream revision.
- **Capability**: One agent-callable unit of the candidate's surface. Carries a name, a declared behaviour, an observed outcome state (working / failing / not exercised), and a human-parity verdict (clean / violation / remediable violation) with cited evidence.
- **Play Block**: One contiguous unattended run of the minimal harness against a live game. Carries start and end game turns, the capability calls made with arguments and outcomes, the screenshot evidence captured, and any crash or interruption.
- **Conformance Report**: The per-principle verdict across the whole capability surface, including an explicit list of principles not examined.
- **Decision Record**: The adopt / reject / adopt-with-remediations recommendation, its supporting findings, its required remediations, the conditions that would reverse it, and what remained undetermined.
- **Cleanup Context File**: The gated handover artifact. Classifies this project's own code areas, states each existing specification's disposition, and names the evidence that must survive.

---

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A model playing through the candidate component alone advances a real game by **at least 10 consecutive turns** with zero operator input during the block.
- **SC-002**: The number of distinct game actions successfully applied to a live client during the audit **exceeds 11** — this project's own measured ceiling after four weeks of hand-building.
- **SC-003**: **100% of the candidate's agent-callable capabilities** carry a human-parity verdict with cited evidence; none are left unclassified.
- **SC-004**: **100% of capabilities** are recorded in exactly one of *observed working*, *observed failing*, or *not exercised* — and the report's own totals reconcile to the full capability count.
- **SC-005**: Every capability judged admissible states the in-client human equivalent; **zero** admissible verdicts rest on assertion alone.
- **SC-006**: Screenshot evidence of live agent play is captured at intervals of **no more than 15 minutes** during play blocks and published to the owner's reporting channel, with **zero** published frames containing debug or harness interface chrome.
- **SC-007**: The owner receives a reported update at **every significant finding**, and no update is deferred to the end of the session.
- **SC-008**: The audit terminates in a single recorded recommendation; a reader who was not present can identify the recommendation, its evidence, and its reversal conditions **without asking a question**.
- **SC-009**: Where the decision is to adopt, a fresh session can execute the cleanup from the context file alone — **every** top-level code area classified, **every** affected specification given a disposition, and **zero** operational facts requiring the audit to be re-run.
- **SC-010**: The audit's own report distinguishes what it examined from what it did not, such that **no principle** of the project's constitution is silently skipped.
- **SC-011**: The live audit leaves the game client and the shared account in a recoverable, explicitly released state, with **zero** stale locks or orphaned runs left behind.

---

## Assumptions

- **The owner's premise is treated as a hypothesis to test, not a conclusion to implement.** The instruction was to "audit its effectiveness"; this specification therefore gates adoption on evidence. A reject outcome is a successful execution of this feature, not a failure of it.
- **"A more basic agent harness just wired up for mcp"** is read as: an agent session whose only game-facing capability is the candidate component, built as thinly as possible, and explicitly *not* a replacement for the existing harness. Its purpose is measurement, and it is expected to be discarded or absorbed after the decision.
- **The live half of the audit requires the shared game account**, which can be in a game on only one machine at a time. The audit announces its hold and releases it explicitly. If the owner needs the account back mid-audit, the live half stops and is reported as partial.
- **This host is the only node that can run the game**, so the audit's live findings are Linux-specific unless stated otherwise. Claims about the candidate's other supported platforms are recorded as upstream claims, not as verified here.
- **The existing match record, web interface, and their specifications are not modified by this feature.** Their disposition is a finding delivered to the next session, not an action taken in this one.
- **The cleanup is out of scope.** This feature produces the plan; a later session executes it. This is the owner's stated sequencing.
- **The candidate is MIT-licensed**, so forking, modifying, and depending on it are permitted. Attribution obligations are carried into any adoption.
- **The audit's arbiter of human-parity is the standard game interface as this project already interprets it** under its existing constitution; this feature introduces no new interpretation of that boundary.
- **Evidence already gathered by this project retains its value** regardless of the outcome — the host operational knowledge, the recorded gameplay, and the measured baselines are inputs to this audit, not casualties of it.

## Dependencies

- A working game client on this host with its debug scripting interface enabled, and exclusive use of the shared account for the duration of a play block.
- Network access to the upstream project and to a model provider for the agent's own reasoning.
- The owner's existing coordination and reporting channels, which this feature reports into rather than replacing.
- The project constitution, which supplies the admissibility standard the audit measures against.
