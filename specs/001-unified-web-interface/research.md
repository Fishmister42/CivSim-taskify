# Phase 0 Research: Unified Web Interface

**Feature**: `001-unified-web-interface` | **Date**: 2026-09-20 | **Plan**: [plan.md](./plan.md)

Purpose: resolve every unknown in the plan's Technical Context before design, and record why each
choice was made so a later reader can re-open it deliberately rather than by accident.

**Status**: all Technical Context unknowns resolved. No validation spikes are required — unlike 002,
this feature touches no unverified integration (no game client, no protocol, no capture hardware); its
open questions are all design trade-offs with a clear preferred answer, plus one dependency risk
(R3/C1) that is a coordination item rather than a spike.

---

## R1 — Language, framework, and why this feature shares 002's toolchain

**Decision**: Python 3.12+ with `uv`, FastAPI + Uvicorn as the web framework.

**Rationale**:

- The task brief is explicit that this feature should share 002's Python/`uv` toolchain unless
  spec.md makes that untenable. Nothing in spec.md does — there is no requirement here that needs a
  capability Python's web ecosystem lacks (no low-level protocol work, no OS-specific automation).
- FastAPI is not a new choice for this project: 002's plan already selects it for the operator surface
  (`contracts/operator-surface.md`), including the pattern this feature needs most — a typed response
  model and async request handling. Reusing it means one team-wide idiom for "a small HTTP surface
  over Python state," rather than two.
- FastAPI's native Pydantic integration is what makes the "one view model, two renderers" design
  (plan Summary point 3) cheap: the same response model that FastAPI would serialize to JSON on its
  own is just as easily handed to a Jinja2 template, so content negotiation is a small branch rather
  than a second parallel implementation.

**Alternatives considered**:

- **Flask** — simpler, but no native request/response model validation; the JSON/HTML equivalence
  this feature's Principle VI compliance rests on would need a hand-rolled schema layer that FastAPI
  provides for free. Rejected.
- **Django** — batteries-included, but its ORM and admin machinery solve problems this feature does
  not have (no schema of its own to own, no writes at all); adopting it would pull in far more surface
  than a read-only, six-route service needs. Rejected as disproportionate.
- **A non-Python framework** (e.g., Node/Express, Go) — would satisfy the UI requirements but
  introduces a second language/toolchain the task brief specifically asks to avoid absent a concrete
  reason from spec.md; none exists. Rejected.

---

## R2 — Frontend: server-rendered pages, no SPA, no build step

**Decision**: Jinja2 server-rendered HTML for the human-facing pages; vanilla JavaScript (no
framework, no bundler) for the two things static HTML cannot do — polling for live updates and
drawing trajectory charts — via hand-rolled inline SVG rather than a charting library.

**Rationale**:

- **No SPA framework.** A React/Vue/Svelte frontend would introduce Node and a build pipeline as a
  second ecosystem alongside Python/`uv`, which the task brief asks to avoid unless spec.md's
  requirements make it untenable. They don't: the interface's own UI Principle UP-003 ("glance, then
  drill") describes a page-and-drill-down structure that maps naturally onto server-rendered pages
  with a handful of interactive widgets, not a client-side application with complex state management.
- **No charting library.** The comparison view (FR-020, UP-007) needs per-turn line trajectories for
  up to five runs and a handful of named metrics — well within what plain SVG `<polyline>` elements,
  computed server-side from the same view model that serves JSON, can render without a dependency.
  Pulling in a library via CDN was rejected because it would make the interface depend on internet
  reachability for a tool whose entire purpose is a trusted, possibly-offline local network (FR-029,
  spec Assumptions); vendoring a library's source was rejected as unnecessary weight for a shape this
  simple, and as a second thing to keep patched.
- **No build step** keeps `static/` files servable as-is by FastAPI's `StaticFiles`, with nothing to
  compile, bundle, or source-map — consistent with 002's own preference for reviewable, unprocessed
  source (its `lua/` and `catalogs/` directories exist for the same reason).

**Alternatives considered**:

- **HTMX or a similar library for partial-page updates** — would reduce hand-written polling
  JavaScript, but it is still a third-party dependency for a small amount of code (poll, diff a
  turn/status number, refetch on change) that this feature can write directly in under 100 lines. Not
  adopted, but not rejected on principle either — recorded as a candidate a task implementer may still
  reach for if the hand-rolled version proves awkward, provided it is vendored rather than CDN-loaded.
- **Server-Sent Events or WebSockets for push updates** — see R5; rejected in favor of polling for
  this feature's scale.

---

## R3 — Depending on the `MatchStore` port before deliverable 3, and before 002 finishes

**Decision**: This feature is coded and tested against the published `MatchStore` Protocol contract
(`specs/002-civ-playing-harness/contracts/match-store-port.md`), via a local, Protocol-typed fake
implementing the same interface. The real import is wired once 002 exposes a stable module path for
it; until then, `src/civsim_web/store_client/` is the single seam where that swap happens.

**Rationale**:

- Deliverable 3 (the match-tracking store) does not exist. 002's own plan already builds against this
  same port with a local SQLite+blob reference adapter rather than blocking on deliverable 3 (its
  Complexity Tracking C2) — this feature applies the identical reasoning one layer further out:
  building against a published contract, with a fake standing in for the real implementation, lets
  three deliverables (002, 003, and this one) proceed in parallel instead of serializing on the
  slowest.
- Concretely, this feature does not need 002's *implementation* to exist at all to be fully designed,
  built, and tested — only the Protocol's method signatures and the record shapes in
  `specs/002-civ-playing-harness/data-model.md`, both of which are already published and stable enough
  to build against (002's plan marks its own data model Phase 1-complete).

**One dependency risk carried forward rather than hidden** (see plan Complexity Tracking C1): the
port's currently published read operations are shaped for the harness's own needs — recovery,
branching, retention, audit of a single run — not for FR-018/FR-019's "list every run, filterable and
sortable by any recorded attribute." No published read produces that projection efficiently. This is
not a blocker for design (the view model and route can be fully specified against a hypothetical
`list_runs(filter, sort) -> list[RunSummary]`-shaped read), but it is a coordination item this plan
flags for whoever finalizes deliverable 3's contract, rather than a problem this feature can solve by
itself by reading every run's full record (see plan C1 for why that alternative fails SC-008 at scale).

**Alternatives considered**:

- **Wait for 002 and/or deliverable 3 to land, then build this feature against the real thing** —
  rejected for the reason above: it serializes work the contract already lets run in parallel, and
  this task's own brief is explicit that 001 is a reader that must not be blocked on 002's completion
  to be planned.
- **Read the SQLite reference adapter's schema directly, bypassing the port** — rejected outright: it
  is exactly the bypassing path 002's own Constitution Check (Principle III) forbids for the harness,
  and adopting it here would make this feature unable to move to deliverable 3's store without a
  rewrite, defeating the entire point of the port existing.

---

## R4 — Comparison view rendering (elaborates on R2 for FR-020/FR-022)

**Decision**: Trajectory charts are inline SVG generated from the same `MetricSeriesView` data the
JSON endpoint returns — one `<svg>` per comparison, one `<polyline>` per compared run, computed
points shared between the SVG markup and a small JS layer that turns a point click into a navigation
to that run's turn (FR-022).

**Rationale**: keeps the "one view model, two renderers" property (plan Summary point 3) intact for
comparisons too — the chart is a rendering of the same JSON a machine caller would receive, not a
separate visual-only computation. Divergence-point interactivity (FR-022) is implemented as a data
attribute per point (`data-run`, `data-turn`) that a single small JS handler reads to build the
target URL, rather than a charting library's event API.

**Alternatives considered**: a server-side rasterized image (PNG) of the chart — rejected because it
is not machine-consumable in the way FR-007 requires (the directing session would receive pixels, not
data) and cannot support per-point navigation without a separate image-map mechanism.

---

## R5 — Live currency: polling interval and the inherited 5-second window

**Decision**: The live view polls its JSON endpoint every **2 seconds**, comfortably inside the
5-second currency window. The window itself is **not** a new number invented for this feature — it is
002's plan's own committed figure ("visible to deliverable 1 within its 5 s currency window"), carried
over unchanged so the two deliverables never quote different numbers for the same property.

**Rationale**:

- **Polling, not push.** This feature's audience is at most a small handful of simultaneous viewers
  (the user, on one or two devices, and the directing session making occasional on-demand requests) —
  the spec's edge cases describe "two devices on the local network," not a broadcast audience. At that
  scale, a persistent-connection push mechanism (SSE/WebSocket) buys lower latency than polling already
  provides for no meaningful benefit, while adding connection-lifecycle state (reconnect-on-drop,
  server-side fan-out) that the spec's own edge case ("the connection between the browser and the
  interface drops... recovers to live without a manual reload") is more simply satisfied by: a dropped
  poll is invisible except as a slightly stale timestamp, and the next poll recovers it automatically
  with no reconnect logic to write.
- **The directing session does not poll at all** in the push sense — FR-007 requires it can *retrieve*
  what the user sees, on demand, via the same routes; it has no standing subscription requirement.
  Polling degrades gracefully to "the session calls the endpoint when it wants current data," which is
  simply an on-demand `GET`.
- **2 seconds, not 5**, leaves headroom for request latency and clock skew between "recorded" and
  "displayed" while still comfortably meeting the 5-second budget SC-003 sets.

**Alternatives considered**:

- **Server-Sent Events**, pushing a message whenever this feature's own internal poll of the store
  detects a new turn — rejected as complexity disproportionate to the audience size; it would still
  require an internal poll loop against the store (SSE doesn't remove polling, it relocates it), while
  adding per-connection server state this feature would then need to test.
  Reconsider if concurrent-viewer count ever grows materially.
- **A shorter interval (e.g., 500ms)** — rejected as unnecessary load on the store for no requirement
  that asks for sub-second currency; SC-003's bound is 5 seconds and SC-001's is 10.

---

## R6 — Scale: catalog pagination and per-turn lazy loading

**Decision**: The run catalog route paginates server-side (default page size chosen so a page renders
well under the SC-008 2-second bound at 50+ total runs) and applies filter/sort server-side rather
than shipping the full catalog to the browser for client-side filtering. Within a turn, a turn's
ordered decision-step list loads its steps lazily (e.g., a bounded initial window plus "load more" /
step-scrubbing rather than rendering all of a several-hundred-step turn's structured detail at once).

**Rationale**: 002's data model states plainly that a turn's step count is unbounded and a late-game
turn may run to hundreds of steps; SC-008 requires every view to reach a usable state within 2 seconds
at that scale. Rendering an entire turn's step sequence in one response would make that bound
unreliable exactly on the runs most worth inspecting (the long, eventful ones). Server-side pagination
for the catalog is the direct analogue for FR-018/FR-019 at 50+ runs.

**Alternatives considered**: client-side virtualization of a fully-fetched dataset — rejected because
it still pays the full fetch and serialization cost up front, which is the part SC-008 bounds.

---

## R7 — Health-status derivation: read 002's signal, do not compute a second one

**Decision**: `Run` health, as surfaced by FR-003, is derived from `Run.lifecycle_state` plus the most
recent relevant entries in that run's `RunEvent` timeline (`crash_detected`, `hang_detected`,
`unresponsive_detected`, `provider_failure`, `provider_retry`, `provider_fallback`,
`turn_ended_on_no_progress`, `disk_headroom_low`, and lifecycle transitions). This feature computes no
independent judgment of "stalled" from raw timestamps.

**Rationale**: 002's plan already commits to detecting crash/hang/unresponsiveness within 60 seconds
(its SC-010, mirrored by this feature's SC-009) and recording the result as an event. If this feature
additionally ran its own staleness heuristic (e.g., "no new turn in N seconds ⇒ stalled"), the two
detectors could disagree — one could show "healthy" while the other shows "stalled" for the same run
at the same moment. That is precisely the asymmetry Principle VI exists to prevent, even though both
numbers would originate from the same underlying store; the spec's edge cases treat "distinguish a
stalled run from a slow turn" as a single fact to get right, not two independent opinions to reconcile
in the UI. Deriving health from 002's own recorded judgment keeps there being exactly one answer.

**A narrow addition, not a second detector**: this feature does independently track *its own view's*
staleness — "when was this page's data last confirmed current" (FR-002) — because that is a statement
about the interface's freshness, not about the run's health, and the two must not be conflated: a
run can be perfectly healthy while a browser tab's last successful poll was a few seconds ago.

**Alternatives considered**: computing "stalled" purely from `TurnCycle.started_at`/`ended_at` gaps
independently of 002's events — rejected for the disagreement risk above, and because it would
duplicate detection logic 002 already has a stronger vantage point to perform (it observes the game
process directly; this feature only observes the store after the fact).

---

## R8 — LAN bind strategy (the FR-029 risk the requirements checklist flagged)

**Decision**: At startup, the service enumerates the host's network interfaces and binds to the
addresses in the private/local ranges (RFC1918 IPv4, and the IPv4 loopback for same-machine access),
rather than binding the `0.0.0.0` wildcard. Every address actually bound is logged prominently at
startup so the operator can verify LAN-only reachability by reading the log rather than by trusting
the implementation.

**Rationale**: the requirements checklist for this spec explicitly called out "a permissive bind
address" as the implementation default most likely to violate FR-029 (never reachable beyond the LAN).
`0.0.0.0` does not *by itself* make a service internet-reachable — that additionally requires a
router configured to forward the port — but it does mean the service listens on *every* interface,
including one that might carry a public or otherwise non-local address on a host whose network
configuration the interface has no visibility into. Binding explicitly to addresses this feature can
identify as private-range makes "LAN-only" a property of the bound sockets themselves, checkable in
code and at a glance in the startup log, rather than an assumption about the surrounding network that
the spec's own Assumptions section places outside this feature's control (no port-forwarding, trusted
network) but that this feature can still defend in depth against.

**Alternatives considered**:

- **Bind `0.0.0.0`** — the simplest option, and arguably sufficient given the spec's own assumption
  that port-forwarding is out of scope. Not chosen because the checklist specifically flagged this as
  the highest-risk default, and the private-range enumeration costs little.
- **Require the operator to name a specific interface/IP in configuration** — more explicit still, but
  pushes a decision onto the operator that this feature can make correctly by inspection in the common
  case (a single-NIC home/office machine), reserving an explicit override for the uncommon case rather
  than requiring it always.

---

## Summary of resolved unknowns

| Technical Context field | Resolution |
|---|---|
| Language/Version | Python 3.12+, `uv` (R1) |
| Primary Dependencies | FastAPI, Uvicorn, Jinja2, vanilla JS, Pydantic v2, httpx (test-only) (R1, R2) |
| Storage | None owned; reads `MatchStore` port only, via a local fake until 002/deliverable 3 land (R3) |
| Testing | pytest, pytest-asyncio, syrupy — matches 002 (R3) |
| Target Platform | Same machine or LAN-reachable machine; desktop browser (plan) |
| Project Type | Single Python service, sibling package to 002's (plan) |
| Performance Goals | 5s currency (inherited from 002), 2s view-usable at scale, 60s health reflection (R5, R6, R7) |
| Constraints | Read-only structurally, second parity gate, second capture gate, no independent health detection, LAN-bound bind strategy, no secrets, comparison never needs captures, surfaces never merge (plan) |
| Scale/Scope | 50+ runs, 300+ turns/run, hundreds of steps/turn (R6) |
