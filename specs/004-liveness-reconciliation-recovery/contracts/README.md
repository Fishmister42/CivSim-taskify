# Interface Contracts: Harness Liveness, Reconciliation, and Autonomous Recovery

**Feature**: `004-liveness-reconciliation-recovery` | **Date**: 2026-09-22

Most of this feature is internal. Six boundaries are not, and each is written down here because
somebody outside this feature builds against it — in two cases, **concurrently, right now**.

| Contract | Who is on the other side | Why it must be pinned |
|---|---|---|
| [liveness-signal.md](./liveness-signal.md) | **Spec 002, today.** It owns the provider layer, the turn cycle and the confirm loop, and is landing the emitters while this plan is being written | The emitters and the reader are being built by different lanes. If "what a phase must publish" is not pinned, two lanes ship two answers — and the one that ships first becomes the answer by default |
| [stall-classification.md](./stall-classification.md) | **Deliverable 1**, which presents a stall to a human, and the directing Claude Code session | Principle VI requires both audiences to see the same thing. A disposition that renders differently in two places is a second view of run state |
| [recovery-rung.md](./recovery-rung.md) | Anyone adding a rung; run preparation, which reports availability | A rung is the only thing in this feature that touches the game. Its declaration is where the precondition, the destructiveness and the parity basis are stated, and an unpinned shape is how a rung ships without one |
| [reconciliation-check.md](./reconciliation-check.md) | **Spec 003's store**, which holds the incidents; whoever writes the next check | The three-outcome rule (`agreed` / `diverged` / `unverified`) is the difference between a detector that finds real divergences and one that fabricates them on healthy runs |
| [blocking-view-registry.md](./blocking-view-registry.md) | Auditors under SC-007; whoever meets a new view | It is the Principle I boundary for rung 1, in the same way the capability catalog is the boundary for play |
| [client-lifecycle-port.md](./client-lifecycle-port.md) | **Nobody yet** — that is the point | Rung 5 depends on a capability that exists on no platform. Specifying it now means the absence is a recorded fact with a shape, rather than a gap someone fills ad hoc under pressure during an incident |

## Conventions across all contracts

- **Versioning**: every schema carries a `schema_version`. Changes are additive within a major
  version; a field is deprecated before removal, never repurposed.
- **Time**: RFC 3339 with an explicit UTC offset. Durations in seconds, named with an `_s` suffix;
  milliseconds with `_ms`.
- **In-game vs out-of-game is marked per payload.** Everything in these contracts is **out-of-game
  harness diagnostics** and none of it reaches the playing agent (FR-048, SC-020). Where a
  diagnostic read is also wanted as an agent observation it must be declared in the observation
  catalog like any other, and it then travels by that route, not this one.
- **Every number states its provenance** — the measurement it came from, or the word `ASSUMPTION`
  with the probe that would settle it. This is a standing review question on this project, not a
  courtesy, and three thresholds were set or nearly set without it on the day before this plan.
- **No outcome is ever taken from the return value of the call that produced it.** Every
  verification in every contract here is an `IndependentRead`
  ([reconciliation-check.md](./reconciliation-check.md)).
- **Absence and unavailability are different facts, everywhere.** A thing that did not happen and a
  thing that could not be observed must never share a representation. This appears three times —
  `SignalObservation.status`, the `unverified` comparison outcome, and a rung `skipped` versus
  `fired_failed` — and each time it is the same rule.

## The two-lane note

The emitters **landed on 002 under `T302`** while this plan was being written, and the SC-026 roster
came with them. [liveness-signal.md](./liveness-signal.md) §6 records the verified state: six
rostered phases, three still silent, and the one clause the provider emitter does not yet satisfy.
It is written as a contract rather than as a description on purpose — the point is what the emitters
owe the reader, not what any particular module currently does, and §6 is the part that will go stale
while §§1–5 do not.
