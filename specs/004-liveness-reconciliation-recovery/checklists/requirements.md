# Specification Quality Checklist: Harness Liveness, Reconciliation, and Autonomous Recovery

**Purpose**: Validate specification completeness and quality before proceeding to planning
**Created**: 2026-09-22
**Feature**: [spec.md](../spec.md)

## Content Quality

- [x] No implementation details (languages, frameworks, APIs)
- [x] Focused on user value and business needs
- [x] Written for non-technical stakeholders
- [x] All mandatory sections completed

## Requirement Completeness

- [x] No [NEEDS CLARIFICATION] markers remain
- [x] Requirements are testable and unambiguous
- [x] Success criteria are measurable
- [x] Success criteria are technology-agnostic (no implementation details)
- [x] All acceptance scenarios are defined
- [x] Edge cases are identified
- [x] Scope is clearly bounded
- [x] Dependencies and assumptions identified

## Feature Readiness

- [x] All functional requirements have clear acceptance criteria
- [x] User scenarios cover primary flows
- [x] Feature meets measurable outcomes defined in Success Criteria
- [x] No implementation details leak into specification

## Notes

- Items marked incomplete require spec updates before `/speckit-clarify` or `/speckit-plan`

**Status: all items pass.** Spec is ready for `/speckit-plan`.

Result: 59 functional requirements (FR-001–FR-059, contiguous), 25 success criteria
(SC-001–SC-025, contiguous), zero `[NEEDS CLARIFICATION]` markers.

### Validation iteration 1 (2026-09-22)

Six questions were resolved during specification rather than deferred, because each had a
defensible answer grounded in an incident measured the same day. They are recorded in the spec's
Clarifications section:

1. **Liveness or reconciliation?** → Reconciliation. Three divergences were measured on 2026-09-22
   and only one stopped play; a liveness-shaped detector catches one of three. This is the single
   decision that most changes what gets built, and it was taken on evidence rather than left open.
2. **Does automated recovery cover every divergence?** → No: the freeze subset only. A non-stalled
   divergence is recorded and surfaced, never auto-corrected (FR-022). The reasoning is written into
   the spec so a future contributor does not "complete" the feature by adding auto-correction.
3. **Behaviour under an undecidable classification?** → Degrade toward the cheapest rung and toward
   reporting; *undetermined* is a first-class recorded disposition (FR-015).
4. **May recovery answer a blocking view that carries a choice?** → No (FR-047). A single-button
   "Goodbye" is the player's answer, not a dismissal.
5. **Fixed stall threshold?** → No (FR-004). With model calls at ~85% of turn wall-clock, a constant
   either trips on normal thinking or sleeps through a freeze.
6. **Does an independent re-read need constraints of its own?** → Yes (FR-025, FR-055). A readback
   in the same command as the write returns the pre-call value. This question was added late, after
   the evidence sweep found that one of the incidents in the original brief had been **retracted by
   the project's own ledger** on exactly those grounds.

### Evidence provenance and one unverified claim

The evidence base was checked against the project record rather than taken from the brief, and two
corrections resulted:

- **Corrected.** The brief's "`CloseSession()` returns success and does nothing" was retracted in
  `.specify/memory/hypervisor-log.md` as a measurement artifact — a same-command readback returning
  the pre-call value. The *stranding* is real and remains in the evidence base; the *cause* has been
  rewritten, and the retraction was promoted into FR-025 and FR-055 because a reconciliation detector
  that repeated the mistake would fabricate divergences on healthy runs.
- **Flagged, not asserted.** That `UI.CanEndTurn()` read `true` throughout the eight refused
  end-turns is reported by the live lane and is **not corroborated** in the written record
  co-located with that incident. The eight refusals and the wedged board are confirmed. The spec
  marks the engine-side reading as unconfirmed and requires the class-2 fixture to establish it
  (FR-054), rather than resting a requirement on an unverified half of a claimed disagreement.
- **Not found in the record.** The phrase "Surprise War" appears nowhere in the artifacts, as
  expected — the owner was looking at it live when this was commissioned. The documented sibling
  (a leader approach view where "Goodbye" was rejected 16 times running) is cited in its place.

### Constitution compliance

- **Principle I (human parity, non-negotiable)** — FR-046 (every game-touching recovery action is one
  a human could perform through the standard UI; Escape qualifies, a debug call does not), FR-047
  (recovery never makes a game decision for the agent), FR-048 (diagnostic reads never enter the
  agent's context; the agent is never told it was stuck), FR-049 (no recovery control path bypasses
  the declared catalogs). SC-007, SC-010, SC-020 enforce.
- **Principle II (Firetuner-first)** — FR-049 requires the synthetic-input path used for dismissal to
  record its Firetuner gap under spec 002 FR-028, and FR-018 requires the registry to record which
  views that path can and cannot reach on a given host.
- **Principle III (complete match telemetry)** — FR-040 puts every detection, classification, rung
  attempt, skip and incident on the run timeline before the next action; FR-021–FR-024 make a
  divergence an incident and forbid amending the record; FR-024 ties an unresolved divergence to the
  run's fitness for trending. SC-015, SC-016 enforce.
- **Principle VII (resilience)** — this feature is where the resilience half becomes implementable.
  FR-034 and the Dependencies section state plainly that the client lifecycle capability Principle VII
  presumes does not exist on any platform, and FR-039/SC-022 require a host to declare which rungs it
  has rather than escalate toward one it cannot perform.

### Quality-bar conformance

The project's characteristic defect — *a mechanism that exists, is well built, and is never reached
in the case that matters, with the suite green throughout* — is addressed as first-class
requirements, not as a testing note: FR-054 (injected real failure, not a mock of harness belief),
FR-055 (negative control mandatory), FR-056 (rungs proven not to fire on classes they do not
answer), FR-057 (every check demonstrated failing against a broken build), FR-058 (structural
reachability from a production path), FR-059 (no input default that renders a detector inert — the
`frozenset()`/`None` shape found three times on 2026-09-22). SC-004, SC-017, SC-018, SC-019 enforce.

The second defect — *a value whose name asserts something it does not mean* — is addressed by FR-002
(every signal registers what its presence does **not** establish), the Progress Signal entity, and
SC-023.

### Terminology note

"Firetuner", "Civilization VI", `has_blocking_prompt`, `UI.CanEndTurn()`, `diplomacy.send_delegation`
and `Errno 111` appear as named evidence from observed incidents and as inherited project
vocabulary, not as solution choices made by this spec. They identify *which* failure a requirement
answers; removing them would make the evidence base unverifiable.
