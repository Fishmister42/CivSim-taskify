<!--
Sync Impact Report
==================
Version change: (none — unratified template) → 1.0.0
Rationale: Initial ratification. No prior project-specific constitution existed; the file at this
path previously contained only the unfilled scaffold. This fills every placeholder for the first
time, which under the versioning policy below is treated as the initial MAJOR release (1.0.0).

Modified principles: n/a (initial set, no renames)

Principles added:
  - I. Human-Parity Information & Action Boundary (NON-NEGOTIABLE)
  - II. Firetuner-First, Skill-Extensible Harness
  - III. Complete Match Telemetry
  - IV. Reproducible, Seeded Experimentation
  - V. Guidebook-Before-Optimization Gate
  - VI. Shared, Unified Observability
  - VII. Provider-Agnostic Model Access & Resilience

Sections added:
  - Scope & Initial Deliverables
  - Development Workflow & Quality Gates
  - Governance (amendment procedure, versioning policy, compliance review)

Sections removed: none

Deferred / follow-up items:
  - GUIDEBOOK.md does not yet exist. Principle V gates optimization work on its existence; writing
    it is tracked as a deferred, non-governance intent (see command's Next Actions), not a
    constitution placeholder.
  - No RATIFICATION_DATE existed prior to this amendment; today's date is used as the ratification
    date since this is the act of initial adoption, not a backfill of a historical date.

Template consistency: Dependent templates (plan/spec/tasks/checklist) are read at runtime per the
scope guard for this command and were not modified here. A future /speckit-plan run should confirm
its Constitution Check section reflects Principles I and III at minimum.
-->

# CivSim Taskify Constitution
<!-- Agentic harness for playing Civilization VI (BBG-balanced) as an LLM research platform -->

## Core Principles

### I. Human-Parity Information & Action Boundary (NON-NEGOTIABLE)
The harness MUST expose to the agent only information and actions a human player could obtain or
perform through Civilization VI's standard game UI. No debug data, hidden AI state, unrevealed
fog-of-war contents, or any action a human could not issue via mouse/keyboard/UI MUST ever reach
the agent's context or appear as an available action, regardless of whether Firetuner or a bespoke
integration made it technically retrievable. Any data path capable of leaking debug-only or
provenance information MUST be filtered before it reaches the agent. Rationale: the project's
entire research value rests on the agent's performance reflecting genuine human-equivalent play;
a single debug leak or extra capability invalidates every downstream metric, ranking, and strategy
conclusion drawn from that run.

### II. Firetuner-First, Skill-Extensible Harness
Firetuner scripting is the default and preferred integration path for game control and observation.
New harness capability MUST be implemented through Firetuner unless a specific capability is
demonstrably unreachable through it. Bespoke solutions (screen/input automation, external tooling,
or other multimodal control) are permitted only to fill a documented Firetuner gap, and MUST still
satisfy Principle I. New skills MAY be authored on demand to solve uncovered surface area, but each
MUST record what it does and why Firetuner could not do it. Rationale: one sanctioned integration
path keeps the full action/observation surface auditable against Principle I; unreviewed bespoke
automation is the most likely place a debug advantage would silently creep in.

### III. Complete Match Telemetry
Every turn of every run MUST be persisted to the match-tracking store before the next turn begins,
with enough game state captured to reconstruct decisions, yields, and outcomes without replaying
the game. A run's results MUST NOT be used for trending, datamining, or optimization input if its
turn-by-turn record has gaps. Rationale: cross-run trending and Monte Carlo ablation (Goals 3 and 4)
are only trustworthy if the historical record is complete; capturing state as it happens is far
cheaper than trying to reconstruct it later.

### IV. Reproducible, Seeded Experimentation
Optimization, branching, backtracking, and ablation work MUST run against a fixed set of initial
seeds under a consistent civilization and ruleset, and MUST be resumable from a named, addressable
save. A quicksave MUST be taken at the start of every turn in addition to the game's own autosave
rotation, and any branch that mutates or abandons a save MUST record which lineage it branched
from. Rationale: comparing strategies across branches or runs is only meaningful when starting
conditions are identical and every branch point can be recovered exactly.

### V. Guidebook-Before-Optimization Gate
No Monte Carlo search, ablation, or automated optimization toward the 100-science/100-culture-by-
turn-50 goal (or any successor goal) MAY begin until `GUIDEBOOK.md` exists and captures the current
distillation of non-obvious Civilization VI strategy and mechanics. `GUIDEBOOK.md` MUST be amended
as the harness discovers strategies not yet documented in it. Rationale: the project is explicitly
staking its experimentation on first encoding known human strategic knowledge; skipping this step
burns compute rediscovering what is already understood.

### VI. Shared, Unified Observability
The web interface MUST present the same live and historical view to the user and to the directing
Claude Code session — information available to one MUST NOT be hidden from the other. The user
MUST NOT be required to interface with the game client directly to understand a run's current or
past state. Rationale: Goal 1 depends on a genuinely shared visual context between user and agent;
asymmetric visibility breaks the collaborative-debugging premise the whole interface is built on.

### VII. Provider-Agnostic Model Access & Resilience
Agent-model calls MUST be routed through OpenRouter or an equivalently pluggable provider layer
rather than a hard-coded single-vendor SDK, so models can be swapped for capability or cost. The
harness MUST detect Civilization VI crashes, preserve the last-known save/state, and resume or
restart the run without silent data loss. Rationale: cost-optimized experimentation requires model
flexibility, and long unattended runs will encounter crashes; both are first-class reliability
requirements for this project, not edge cases to handle later.

## Scope & Initial Deliverables

The project's initially chartered scope is five deliverables: (1) a unified web interface for live
and historical experiment data, (2) the Civilization-playing harness described by Principles I, II,
IV, and VII, (3) a match-tracking data store satisfying Principle III, (4) a sandboxed optimization
and Monte Carlo layer satisfying Principles IV and V, and (5) `GUIDEBOOK.md`, required by Principle
V before any deliverable-4 work runs. Each deliverable MUST be elaborated through its own feature
spec (`/speckit-specify`) before implementation; this constitution constrains how those specs may
be designed, it does not itself define their designs.

## Development Workflow & Quality Gates

Every feature plan produced by `/speckit-plan` MUST include a Constitution Check that explicitly
verifies compliance with Principle I (no debug/provenance leakage in any new data path or action)
and Principle III (no new game-state write path bypasses turn-by-turn persistence). Any harness
change that adds a new information or action surface MUST identify, in that same check, exactly
what a human player would need to do in-client to obtain the equivalent information or effect.
Database schema changes to the match-tracking store MUST remain backward-readable for existing
historical-trend queries, or ship an explicit migration. Deviation from any principle MUST be
justified in writing in the plan's Complexity Tracking (or equivalent) section before proceeding.

## Governance

This constitution supersedes any conflicting practice, template default, or ad hoc convention used
elsewhere in this project. Amendments are proposed by re-running `/speckit-constitution` with the
proposed change and rationale; the amendment is adopted by updating this file and its version per
the policy below.

Versioning policy (semantic versioning applied to this document):
- MAJOR: backward-incompatible removal or redefinition of a principle (e.g., relaxing Principle I
  or removing the guidebook gate in Principle V).
- MINOR: a new principle is added, or existing guidance is materially expanded.
- PATCH: clarifications, wording, typo fixes, or other non-semantic refinements.

Compliance review: every `/speckit-plan` and `/speckit-tasks` run for the five deliverables above
(and any deliverable added later) MUST re-check its artifacts against the Core Principles before
`/speckit-implement` begins. Any change that cannot satisfy Principle I MUST be blocked rather than
shipped with a caveat, since there is no acceptable partial compliance for information/action parity.

**Version**: 1.0.0 | **Ratified**: 2026-09-19 | **Last Amended**: 2026-09-19
