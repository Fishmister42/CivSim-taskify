# Specification Quality Checklist: Civilization-Playing Harness

**Purpose**: Validate specification completeness and quality before proceeding to planning
**Created**: 2026-09-19
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

### Validation iteration 1 (2026-09-19)

One failing item: *No [NEEDS CLARIFICATION] markers remain* — 2 markers present.

1. **Decision-surface breadth** (then FR-009) — opening-phase mechanics for the turn-50 goal vs.
   full-game mechanics. The constitution's turn-50 goal and deliverable 1's "300+ turn" performance
   case pointed in different directions, so no safe default was assumed.
2. **Agent perception modality** (then FR-023) — structured state only, or structured state plus
   rendered screen images. Principle II's Firetuner-first stance suggested structured-only, but
   Principle II also contemplates "other multimodal control", so the default was not unambiguous.

### Validation iteration 2 (2026-09-19)

Both clarifications answered by the user:

- **Q1 → B (full game, all mechanics)**. FR-009 now covers every era from start to victory or defeat,
  spanning unit promotions, city management, research and civics, policies and governors, religion,
  diplomacy and war, espionage, great people, and World Congress. This surfaced a requirement the
  opening-phase scope would not have needed: **FR-010**, game-initiated prompts and interrupts
  (AI war declarations, Congress votes, pantheon and great-person selections, era transitions) must be
  answered as recorded agent decisions rather than dismissed or defaulted. Added US1 scenarios 5 and
  10, SC-002 and SC-005, and edge cases for between-turn interrupts, late-game turn volume, and
  long-run save/capture accumulation.
- **Q2 → B (structured state plus screen images)**. FR-024 now defines the agent's context as both.
  Because an image can carry whatever is on screen, two supporting requirements were added to keep the
  parity boundary auditable: **FR-025** (screen every image for Firetuner window, developer console,
  debug overlay, or harness UI before the agent sees it — withhold and re-capture, never pass through)
  and **FR-026** (the camera state producing an agent-visible image must be human-reachable, and camera
  movement is itself a declared, rejectable action). **FR-050** covers the honest-degradation case:
  a turn that runs without its images is recorded as visually degraded rather than silently switching
  the agent's perception mode mid-run. Added US2 scenarios 6 and 7, SC-009, SC-013.

**Cross-cutting consequence of Q2 → B**: requiring images narrows the usable model set to those that
accept them, on *every* provider in a run's fallback chain — a real constraint on Principle VII's
swap-freely goal. **FR-039** makes this explicit: a run must not start unless its whole chain can carry
the full context, and the harness must never drop images to make a call succeed. SC-017 tests it, and
the trade-off is recorded in Assumptions rather than left implicit.

Result: 53 functional requirements (FR-001–FR-053, contiguous), 22 success criteria (SC-001–SC-022,
contiguous), zero markers.

### Deliberate non-markers

Reasonable defaults taken and recorded in Assumptions rather than asked about:

- **Crashed-turn recovery** → resume from that turn's own start quicksave and replay it (Principle IV
  mandates the quicksave for exactly this purpose); the abandoned attempt is retained, not discarded.
- **Concurrency** → one active run per game client instance; parallelism via multiple harness
  instances, since Civilization VI is a single desktop client.
- **Action-rate parity** → parity governs what the agent can know and do, not how fast; no
  actions-per-minute throttle. Recorded as an explicit reading of Principle I, not an oversight.
- **Difficulty, map settings, opponent composition** → per-run configuration, fixed within a seed set.
- **Cost management** → the harness records per-call cost but does not enforce a budget; with
  full-game runs and images in context, cost per run is dominated by those two answers and is left
  visible rather than capped.

### Terminology note

"Firetuner", "OpenRouter", and "Civilization VI" appear as named project constraints inherited from
Constitution Principles II and VII, not as solution choices made by this spec. They are treated as
in-scope domain vocabulary rather than implementation leakage.
