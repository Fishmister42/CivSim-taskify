# Interface Contracts: Civilization-Playing Harness

**Feature**: `002-civ-playing-harness` | **Date**: 2026-09-19

The harness is not purely internal — it sits between a game client, a model provider, an operator,
and a data store that another deliverable owns. Each of those boundaries is a contract that someone
else builds against, so each is written down here rather than left to emerge from the code.

| Contract | Who is on the other side | Why it must be pinned |
|---|---|---|
| [capability-catalog.md](./capability-catalog.md) | Auditors, and every developer adding a game-touching capability | It *is* the Principle I boundary. SC-006, SC-007, and SC-020 are audited against this format |
| [run-configuration.md](./run-configuration.md) | The operator, and deliverable 4's branch orchestration | A run is only reproducible if its definition is complete and exact (FR-001, FR-002) |
| [match-store-port.md](./match-store-port.md) | **Deliverable 3** | The harness cannot advance a turn without a successful write; deliverable 3 must satisfy this shape |
| [model-provider-port.md](./model-provider-port.md) | OpenRouter today, any provider later | Principle VII's swap-without-code-change only holds if the seam is specified (FR-037, FR-038) |
| [operator-surface.md](./operator-surface.md) | The researcher, and the directing Claude Code session | Lifecycle control without touching the game client (FR-004), bounded so it never becomes a second view (FR-053) |
| [nexus-protocol.md](./nexus-protocol.md) | The Civilization VI client | Undocumented by the vendor; reverse-engineered here so the transport is reviewable rather than folklore |

**On platforms**: none of these contracts is platform-specific, and that is deliberate. The tuner
interface ships in the native Windows, macOS, and Linux builds and the protocol is plain TCP, so the
game-facing contracts hold unchanged everywhere. The OS-specific work lives behind a `HostPlatform`
port described in plan.md and research R19 — deliberately *not* a contract here, because nobody
outside this feature builds against it.

## Conventions across all contracts

- **Versioning**: every schema carries a `schema_version`. Changes are additive within a major
  version; a field is deprecated before removal, never repurposed.
- **Time**: all timestamps are RFC 3339 with an explicit UTC offset.
- **Identifiers**: opaque strings. Callers must not parse structure out of an id — save-point
  *names* encode run and turn for human legibility, but addressing goes through the record (FR-032).
- **Credentials never appear** in any payload defined here. Where a provider key is needed it is
  resolved from the environment at call time and is excluded from every serialized form (FR-043).
- **In-game vs out-of-game** is marked per payload. Out-of-game data is recorded in full and never
  reaches the playing agent as game information (FR-020).
- **The decision step is the grain.** Since the spec's clarification made a turn a loop rather than
  a phase (FR-008), observations, images, and model calls are per step, and records must be
  reconstructable step by step. Where a contract below says "per turn" it means the whole loop; where
  it says "per step" it means one iteration, and the difference is load-bearing.

**Revision 2** updated all six contracts for the clarification session of 2026-09-19: the within-turn
decision loop, the removal of the turn time budget, explicit-archival-only save retention, and the
game-build pin. The shape changes worth knowing before reading: `ModelProvider.complete` returns one
decision rather than a list; `MatchStore` gained step-level gap detection and `archive_run`; the
operator surface gained `archive`, `accept-build`, and `reap`; `end_turn` became a declared catalog
action; and `turn_time_budget_s` was replaced by `no_progress_step_limit`.

## What is deliberately *not* a contract here

- **The store's physical schema, indexing, and migration strategy** — deliverable 3 owns those. This
  feature specifies only what must be writable and readable back.
- **Run presentation** — deliverable 1 reads the store directly; the harness exposes no presentation
  surface (FR-053).
- **Branch selection, sampling, and search policy** — deliverable 4 drives those through the
  branch-from-save mechanism specified in the operator surface; the harness does not decide what to
  branch or when.
