# Implementation Plan: Civilization-Playing Harness

**Branch**: `002-civ-playing-harness` | **Date**: 2026-09-19 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/002-civ-playing-harness/spec.md`

**Revision 2** — re-planned against the spec's clarification session of 2026-09-19, which changed
four load-bearing decisions: the turn became an interactive within-turn decision loop rather than
one batched decision (FR-008), the per-turn time budget was removed in favour of a no-progress step
count (FR-014), save retention became explicit-archival-only (FR-036), and the Civilization VI build
became a pinned, overridable part of a seed set (FR-002, FR-031). All Phase 0 and Phase 1 artifacts
were updated; `tasks.md` was subsequently regenerated against this revision and reflects all four
changes (the decision-step loop in T110–T114, the no-progress backstop in T111/T113, archival-only
retention in T168–T171, and the build pin in T071/T174–T175).

**Revision 3** — corrected a factual error about platform support. Revision 1 pinned Windows 11 and
declared Linux unsupported, conflating the Windows-only FireTuner **GUI** with the tuner
**interface in the game client**, which is present in the native Windows, macOS, and Linux builds
(research R1). Since the harness speaks the Nexus protocol directly and never opens the GUI, the
game-facing design was already portable; only the host layer was Windows-bound. This revision adds
a `HostPlatform` port with per-platform adapters (R19) and makes platform part of a seed set's
pinned identity (R20). `spec.md` needed no change — it contains no platform assumption.

## Summary

Build an unattended Civilization VI playing harness: it brings a game to a fully specified starting
position, then plays turn after turn until a stop condition, recovering from crashes and provider
failures as the same continuous run, and supporting branch-and-replay from any turn's save.

A turn is not one decision — it is an unbounded **loop of decision steps**. Each step assembles a
fresh parity-filtered observation with a fresh screened image, asks the agent for one decision and
its reasoning, executes it, and verifies the effect, then observes again so the agent sees what it
just did before choosing what to do next. The loop ends when the agent issues a declared end-turn
decision, or when the no-progress backstop trips. Nothing else ends a turn: there is no time budget,
no step cap, and no cost ceiling.

The technical approach is a single Python service on the Civ VI machine, built around five
load-bearing structures:

1. **A capability catalog as data.** Every observable, visual view, and action is a versioned YAML
   declaration naming its parity basis and the code path that implements it. The catalog — not the
   prompt, not the agent's restraint — is the parity boundary (FR-016 – FR-018, FR-023).
2. **A FireTuner (Firaxis Nexus) client** speaking the length-prefixed TCP protocol on port 4318,
   executing declared Lua in the `GameCore_Tuner` context to read and the `InGame` context to act.
   This is the default path for everything; bespoke paths exist only where a gap is documented
   (FR-027 – FR-029).
3. **A write-before-advance recorder.** The turn does not end until its full record — every decision
   step in order, with the observation given, the image shown, the decision issued, the reasoning
   stated, and the verified outcome — is durably in the match-tracking store; a failed write halts
   the run rather than advancing it (FR-013, FR-051).
4. **A provider-agnostic model layer** over OpenRouter's HTTP API with no vendor SDK on the decision
   path, returning exactly one decision per call, a preflight that refuses to start a run whose
   fallback chain cannot carry images, and per-call accounting of the model that actually served
   each request (FR-037 – FR-043).
5. **A `HostPlatform` port** isolating the six things the harness needs from an operating system —
   window identity, screen capture, game directories, optional synthetic input, process liveness,
   and free disk space — behind one interface with a Windows, macOS, and Linux adapter. Everything
   else is portable already: the tuner interface ships in all three native builds and the protocol
   is plain TCP (R19).

The agent's per-turn context is parity-filtered structured state **plus** screened images of the
game's own screen, which makes image hygiene a first-class correctness problem rather than a
cosmetic one: the capture path is chosen for occlusion immunity, and an image that cannot be proven
clean is withheld and the turn recorded as visually degraded (FR-024 – FR-026, FR-050).

## Technical Context

**Language/Version**: Python 3.12+ (project-managed with `uv`). Chosen for first-class asyncio TCP
(the Nexus protocol client), maintained OS-interop bindings on all three target platforms, and
alignment with the established prior art for FireTuner automation. See
[research.md](./research.md) R1.

**Primary Dependencies** — split deliberately into a portable core and a thin per-platform layer,
because that split is the whole cross-platform design (R19):

*Portable core — no OS-specific code, on any platform:*

- *Game transport*: no third-party library — a first-party `nexus` asyncio client implementing the
  Firaxis Nexus framing (R2). Plain TCP to `127.0.0.1:4318`; nothing about it is platform-bound.
  Declared Lua lives in `lua/` as reviewable source, not inline strings.
- *Schemas & validation*: `pydantic` v2 — run configuration, catalog declarations, and every record
  written to the store, with JSON Schema emitted into `contracts/`.
- *Model access*: `httpx` against OpenRouter's HTTP API. No vendor SDK is permitted on the decision
  path (FR-037).
- *Process state*: `psutil` for client liveness (cross-platform as-is).
- *Operator surface*: `typer` CLI plus a loopback-only `fastapi`/`uvicorn` control endpoint —
  lifecycle and diagnostics only (FR-004, FR-053).
- *Storage adapter*: `SQLAlchemy` + `sqlite3` for the reference match-store adapter (see Storage).
- *Image encoding*: `Pillow`.
- *Testing*: `pytest`, `pytest-asyncio`, `syrupy` for record-shape snapshots.

*Per-platform host adapters — the only place an OS-specific import may appear:*

| Capability | Windows | macOS | Linux |
|---|---|---|---|
| Window identity | `pywin32` (HWND) | Quartz window list | X11 / Wayland window id |
| Screen capture | Windows.Graphics.Capture (`winsdk`) | ScreenCaptureKit | XComposite, or portal/PipeWire on Wayland |
| Synthetic input *(only if R5 requires it)* | `pydirectinput` | `CGEvent` via Quartz | `XTest`; blocked on Wayland |
| Game directories | `%USERPROFILE%\Documents\My Games\…` | `~/Library/Application Support/…` | `~/.local/share/aspyr-media/…` |

Platform extras install as optional dependency groups, so a macOS install never pulls `pywin32`.
The bespoke input path remains gated by FR-028 and recorded per run wherever it is used.

**Platform support is stated as a tier, not a boolean** (R19): *Validated* (full capability, live
tests and capture-hygiene spike pass), *Supported* (verified quicksave path but no passing capture
spike — runs are visually degraded and marked), *Unsupported* (no verified quicksave path; the run
refuses to start, since FR-007 makes a turn without its quicksave impossible). Preflight resolves
the tier and `doctor` prints it with its reason. A platform is Unsupported until probed.

**Storage**: The match-tracking store **was deliverable 3, and landed on 2026-09-21 (`93a9b7e`)**.
*(This paragraph read "is deliverable 3 and does not yet exist" until 2026-09-22.)* This feature
defines and depends on a `MatchStore` port — the contract in
[contracts/match-store-port.md](./contracts/match-store-port.md) — and shipped a local reference
adapter (SQLite for records, content-addressed files on disk for captures and save-point blobs)
so the harness was buildable and testable before the store existed. **Deliverable 3 did not replace
that adapter; it extended it** — `SqliteMatchStore` is `class SqliteMatchStore(SqliteReadBase)` in
`store/sqlite_adapter.py`, the same class behind the same port, now carrying schema 1.1 with a
copy-first migration, model calls as rows, trends and bundles. The plan's "replaces the adapter
without harness changes" was a prediction about the shape of the landing, and the landing chose
continuity over replacement.

**Deliverable 3 also brought behaviour this plan did not anticipate: an open-time orphan sweep that
pauses runs.** Opening the store in write mode sweeps for orphaned runs and pauses them
(`store/sqlite_adapter.py`, 003 rule W6; `orphans_paused_on_open` reports what the opening did).
That makes *opening the store* a state-changing act on records this feature owns — a harness that
simply reads may find runs paused by its own open. It is 003's rule and correct there, but 002's
plan describes a store whose open is inert, and nothing here would have told a reader otherwise.
The sweep is documented in [data-model.md](./data-model.md) §"Orphan sweep"; this is the plan
acknowledging it rather than restating it.

Game saves themselves remain Civ VI `.Civ6Save` files in the
platform's own save directory — resolved by the host port, never hard-coded — and are referenced by
addressable save-point records rather than by path (FR-032, R19). That addressing rule, written for
auditability rather than portability, is what makes the per-platform directory a detail of one
resolver instead of something threaded through the harness. See Complexity Tracking C2 and C4.

**Testing**: `pytest` in four tiers, only the first three of which run in CI:

| Tier | What it covers | Needs Civ VI? |
|---|---|---|
| `tests/unit` | Nexus codec, parity filter, catalog resolution, redaction, state machines, no-progress accounting, retention eligibility, build-pin comparison | No |
| `tests/contract` | JSON Schema conformance, `MatchStore` port conformance, provider port conformance, **`HostPlatform` port conformance** | No |
| `tests/integration` | Full turn cycle — the multi-step loop, both turn endings, mid-turn observation failure and replay — against a recorded-transcript fake Nexus server, fake provider, and fake host | No |
| `tests/live` | Real client: run preparation, build-pin refusal, crash recovery, branch identity, capture hygiene — **run once per platform**, since each has its own host adapter | Yes — marked, excluded from CI |

The first three tiers run on every platform in CI, which is the point: the portable core is the
majority of the harness, and a matrix build across Windows, macOS, and Linux catches an accidental
OS-specific import long before the live tier would. The `HostPlatform` conformance suite runs the
same assertions against every adapter, so "this platform is supported" is a test result rather than
a claim.

Three of these are **negative** tests guarding the clarification's invariants, which is the shape
that matters for rules whose violation looks like a reasonable feature: a productive 500-step turn
must not end (I16); a save on a finished-but-unarchived run must never become eligible (I17); and a
provider adapter returning two decisions for one step must fail rather than be accepted (I13).

A dedicated **parity red-team suite** inside `tests/contract` asserts that a known set of forbidden
values (unrevealed plots, opponent internals, RNG state, harness telemetry) never appears in an
assembled agent context, and fails the build on any finding (SC-006, SC-008).

**Target Platform**: Native **Windows, macOS, or Linux**, running on the same machine as the
Civilization VI client with local access to its saves and to the tuner interface. The tuner
interface is present in all three native builds; it is enabled from the in-game Options menu on
Windows and by setting `EnableTuner 1` in `AppOptions.txt` on macOS and Linux (R1).

**Civ VI under Proton or Wine remains unsupported** — the tuner interface is built into the native
binary and is not exposed under a compatibility layer, so a Linux host must run the native Aspyr
port. This is the only part of revision 1's platform claim that survived; the rest was an error
(R1).

**Project Type**: Single Python project — a long-running harness service plus an operator CLI. Not a
web application; the presentation surface is deliverable 1, which reads the store.

**Performance Goals**:

- Crash, hang, or unresponsiveness detected and recorded within 60 s (SC-010), from **per-operation**
  bounds — command, capture, and read-back timeouts plus a tuner heartbeat. No turn-level timer
  contributes to this (R12).
- A completed turn's record durably persisted before the turn is ended, and visible to deliverable 1
  within its 5 s currency window.
- **No turn-duration target, because a turn has no time bound.** A turn runs for as many decision
  steps as the agent needs and is never truncated for length or cost; the only backstop is the
  configured consecutive no-progress step count (FR-014, SC-022). Per-step latency and cost are
  recorded so the trade-off stays visible, not so it can be capped.
- Sustains a 300+ turn full game — with late-game turns plausibly running to hundreds of decision
  steps and hours of wall clock — and a 20+ run unattended batch without record gaps (SC-011).
- Disk headroom is a run precondition, checked at preflight and before every quicksave: below the
  configured floor the run halts rather than deleting an unarchived save (R17).

**Constraints**:

- **One tuner connection at a time.** Civ VI accepts a single FireTuner client, which both enables
  and obliges FR-006's one-run-per-client rule; enforced additionally by a run-identity lock.
- **Parity is structural.** Nothing reaches the agent except through a declared catalog entry; there
  is no raw-Lua escape hatch on the context-assembly path (FR-018).
- **Images are never dropped to make a call fit.** A chain that cannot carry the full context fails
  the run at preflight (FR-039, SC-017).
- **No credential may reach any record, capture, log, or context** — enforced by a redaction filter
  on every write path, not by convention (FR-043, SC-018).
- **Write-before-advance.** Every game-state-advancing step is downstream of a successful store
  write (FR-013, Principle III).
- **A turn is never truncated.** No wall-clock bound, no step cap, no cost ceiling. The agent's
  end-turn decision and the no-progress backstop are the only two exits (FR-008, FR-014).
- **Saves are deleted only after explicit archival.** No age, quota, window, or thinning rule may
  make a save eligible (FR-036).
- **The game build is pinned per seed set**, where "build" is a composite of **platform and
  version**. A differing build or platform fails the run before turn 1 unless an operator has
  explicitly accepted the change for that set, which every dependent run records (FR-002, FR-031,
  R20).
- **No OS-specific import outside `host/`.** The portable core must run unchanged on every target
  platform; a platform check anywhere else is a defect, caught by the cross-platform CI matrix
  (R19).

**Scale/Scope**: Full games across every era (300+ turns) under seed sets of ~10–20 seeds. **One
capture and one model call per decision step**, not per turn — with an unbounded step count per
turn, this is the dominant cost and storage term in the system and is accepted deliberately (spec
Assumptions). **54 functional requirements (FR-001 – FR-054), in the nine groups `spec.md` itself
heads them under**: Run definition and lifecycle, Turn cycle, Human-parity boundary, Integration
path, Reproducibility/saves/branching, Model access, Resilience, Recording boundary, Host
environment.

*Corrected 2026-09-22.* This read "53 functional requirements across 12 subsystems". Both numbers
were wrong and they were wrong differently. The requirement count was simply stale — `spec.md` has
defined FR-054 since its initial commit. **The "12 subsystems" figure is not restated with a
different number, because there is no grouping anywhere in these artifacts that yields twelve**;
substituting a plausible count would repeat the defect rather than fix it. The nine named above are
re-derivable by anyone: they are the bold headings in `spec.md`'s Functional Requirements section,
counted. Where a number appears in this plan it should be possible to reproduce it from a named
source, which is the standard these three figures failed.

**FR-054 (Host environment) belongs in the Constitution Check and had no place in it.** It requires
the harness to verify, before a run starts, that the host can provide every capability the run needs
of it — at minimum a working path to take and verify FR-007's per-turn quicksave — and to refuse the
run before turn 1 naming the missing capability, while a merely *degraded* capability (FR-050's
images) is recorded and the run proceeds. That is the **Platform support is stated as a tier**
paragraph above made binding, and it is **Principle IV's** guard as much as a host concern: a run
that cannot take its quicksave cannot be branched from or compared to anything, so refusing it
before turn 1 is what stops an incomparable run from entering a seed set at all. It ships as
`observe/host_gate.py`'s `evaluate_host_gate`, wired into the no-live-client gate block of
`run/composition.py` alongside `catalog_preflight` and `debug_menu_preflight`; it raises on
`UNSUPPORTED` and records `visually_degraded` on `SUPPORTED`, which is exactly the split FR-054's
last sentence draws.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

Constitution v1.0.0. The Development Workflow section requires every plan to explicitly verify
Principle I and Principle III, and to name the in-client human action behind any new information or
action surface.

### Principle I — Human-Parity Information & Action Boundary (NON-NEGOTIABLE)

**Initial check: PASS (by design, with one gate deferred to a spike).**
**Post-design re-check: PASS.**

| Obligation | How this design satisfies it | Where |
|---|---|---|
| No undeclared data reaches the agent | Context assembly consumes only capability outputs; capabilities exist only as catalog entries; an undeclared field has no path into the assembler | `parity/`, `capability/` |
| Every surface names its in-client action | `parity_basis` is a required, non-empty field on every catalog entry; the schema rejects an entry without one | [contracts/capability-catalog.md](./contracts/capability-catalog.md) |
| Filtering is structural, not prompted | The filter runs before assembly and has no bypass; the prompt carries no parity instructions it could ignore | FR-018, `parity/filter.py` |
| Images held to the same standard | Screening is a precondition of entry, not a post-hoc check; capture targets the game window only; camera state is itself a declared action | R6, R7, R8 |
| Re-observation cannot smuggle data in | Each step's context is assembled by the same filter as the first; there is no "incremental update" path that bypasses it | FR-008, FR-018 |
| Boundary is auditable after the fact | Catalog version + content hash recorded on every run; every observation and decision record references the entry that produced it | FR-022, data-model `Run.catalog_versions` |
| Run refuses to start if incomplete | Preflight resolves every reachable capability to a declaration and aborts otherwise | FR-023 |

**New surfaces introduced by this plan, with their parity basis** (the workflow section's specific
requirement):

| New surface | Kind | In-client human equivalent |
|---|---|---|
| Nexus read capabilities (`GameCore_Tuner` Lua) | Observation | Reading the corresponding panel, tooltip, or map tile in the standard UI |
| Nexus act capabilities (`InGame` Lua) | Action | The mouse/keyboard path for that order, purchase, or selection |
| Window capture of the game's own screen | Observation (visual) | Looking at the screen |
| Camera move / zoom / strategic-view toggle | Action | Dragging, scrolling, or pressing the view hotkey |
| ~~Bespoke save/load dialog driving~~ — **never written; forbidden once R5 returned Outcome A** (row kept so the table is not silently rewritten) | Action | Esc → Save Game → type a name → Save |
| Bespoke synthetic click at a prompt control's rectangle (`prompts.orders`) | Action | Clicking that button on the prompt the game is already showing — the option is one the prompt itself offered |
| Screen-identity probe (which screen is up) | Observation | Seeing which screen is on the monitor |
| End turn | Action | Clicking the end-turn button, or pressing its hotkey |
| Game build / version read | Observation (out-of-game) | Reading the version string on the main menu — recorded as run provenance, never placed in the agent's context |

The last two are new with revision 2. **End turn** was previously harness control flow; FR-008 makes
it a decision the agent issues, so it needs a declaration like any other action — and it is exactly
the kind of surface that would otherwise reach the game without one. **The build read** is declared
for completeness of the audit even though its destination is the run record rather than the agent:
it is out-of-game provenance under FR-020 and is filtered out of context like model identity or cost.

**The prompt-click row is new on 2026-09-22, and the save/load row's strikethrough is the point.**
Until then this table listed *bespoke save/load dialog driving* as a surface this plan introduces,
while the Constitution Check a few paragraphs below called that same surface "forbidden rather than
merely unnecessary" and said it was never written — the table was simply not revisited when R5
returned. Deleting the row would have hidden a planned-and-abandoned surface; it is struck through
and kept. The surface that *did* arrive is the one underneath it, and it had no row at all: a
synthetic click at a prompt control's rectangle. Its parity basis is unusually tight — the harness
may only click a control the prompt is already offering, and the offered set comes from
`screens.probe`, a declared observation the parity filter sees like any other — but tight is not
the same as absent, and it needed stating here.

**Deferred gate, not a deviation**: the capture path's occlusion immunity is a hypothesis until the
hygiene spike in R6 passes. The design fails safe — if no capture path can be proven clean, runs
proceed as visually degraded under FR-050 rather than showing the agent an unscreened image. No
run may show images to the agent before that spike passes.

**Revision 3 — the parity boundary is platform-independent, and this was checked rather than
assumed.** Going cross-platform touches Principle I in exactly one place: capture is the only
parity-relevant capability that differs per platform, and image hygiene is release-blocking
(SC-009, SC-019).

| Concern | Resolution |
|---|---|
| Does the catalog change per platform? | **No.** Declarations describe what a human sees and does in the game's UI, which is identical across platforms. One catalog, one boundary — a per-platform catalog would be the one thing this project cannot have (R19) |
| Does the parity filter change? | **No.** It consumes capability results; nothing in it touches an OS |
| Does image screening change? | **The rule does not; the odds of passing it do.** All four gates (R7) apply identically. What varies is whether a platform's capture path can be *proven* occlusion-immune |
| Can a weaker platform quietly show worse images? | **No.** The spike is per platform, and a platform without a passing spike runs visually degraded under FR-050 — recorded, not hidden |
| Could a platform leak non-player UI the detector does not know? | Possible in principle, which is why the content gate's detector profile is per platform: a macOS menu bar or a Linux panel is not the same chrome as a Windows taskbar |

The load-bearing point: **a platform may be less capable, but it may not be less honest.** Every
tier below Validated produces marked, degraded runs rather than unmarked ones.

### Principle III — Complete Match Telemetry

**Initial check: PASS.** **Post-design re-check: PASS.**

- No game-state write path bypasses turn-by-turn persistence: `run/turn_cycle.py` is the only
  component permitted to **advance** the harness past a turn, and that advance is sequenced strictly
  after a successful `MatchStore` commit; a failed or timed-out write raises and halts the run
  (FR-013, FR-051). *(Corrected 2026-09-22, T279 — the earlier wording said "the only component
  permitted to call the end-turn action", which the code does not do and was never going to: for
  `ended_by_agent` and `end_turn_unconfirmed`, the two commonest outcomes, the agent's end turn is a
  **declared decision dispatched inside the T110 loop**, executed and verified through the same
  `act.dispatch`/`act.verify` path as every other decision, because FR-008 requires exactly that. So
  the in-client order can, and normally does, precede the commit. What the commit gates is everything
  downstream of it: the harness's own bookkeeping, the `preparing`/`playing` advance the run is told
  about, and any further action this process sends the client. Only `ended_on_no_progress` — the
  FR-014 backstop, which has no agent decision to dispatch — has the end-turn dispatch itself
  strictly after the write, in `_dispatch_backstop_end_turn`. The crash window this opens for the
  other two is covered rather than ignored: the turn-start quicksave (FR-007) and the
  abandoned-attempt rule (FR-047) mean a turn lost between dispatch and commit is replayed from its
  own save by `RecoveryEngine` and retained as an abandoned attempt, not silently missing — which is
  the same window D3's own "Why" column already named.)*
- Nothing exists only in ephemeral form: captures and save-point references are written through the
  same store port as records (FR-051). The local reference adapter is a *storage implementation* of
  that port, not a second, bypassing path.
- Gaps are visible rather than silent, **at two grains now**: `record_completeness_status` on the
  run, an authoritative/abandoned flag per turn attempt, explicit turn gap markers, and contiguous
  `step_index` within each turn. SC-003 requires no step gap within a turn as well as no turn gap
  within a run, so a turn that recorded its decisions but lost a step's observation is incomplete,
  not merely imperfect (FR-012, FR-052, SC-011).
- The unbounded turn does not weaken this: the record grows with the step count, and the turn is
  still written as one unit before it ends. A turn of 400 steps is 400 recorded steps or a halted
  run — there is no summarisation path that would let a long turn record less than a short one.
- Schema backward-readability is deliverable 3's obligation under the constitution; this feature's
  contribution is that its record schemas are versioned and additive-only, published in
  `contracts/`.

### Principle II — Firetuner-First, Skill-Extensible

**PASS with one documented gap.** Every capability declares `path: firetuner | bespoke`. The catalog
schema makes `firetuner_gap` a required field when `path: bespoke`, so an undocumented bespoke path
cannot validate. One bespoke capability is planned at the outset — save/load dialog driving (R5) —
and is recorded in Complexity Tracking C1.

**The R5 spike (T077) closed the save half.** `spikes/r5-save-path.md` recorded **Outcome A**
against a real client — `Network.SaveGame(gameFile)` in the `InGame` context writes a real, named
`.Civ6Save`. Under Principle II a working Firetuner path means the planned save/load **dialog
driver** is *forbidden rather than merely unnecessary*; it was never written, and `saves.save_game`
is declared `path: firetuner`.

**But C1 was not discharged — it moved, and this plan said otherwise until 2026-09-22.** The text
here previously read "there is now no bespoke capability at all … C1 is therefore discharged". That
was true of the *save* path and false of the harness. A bespoke synthetic-input path shipped
elsewhere: answering an in-game **prompt**. Measured 2026-09-21 (blocks 13-15), no Lua API reachable
from `InGame` fires a control's registered callback — `control:CallCallback(Mouse.eLClick)` returns
without error and does nothing, and the generic `UI.RespondToPrompt` this once fell through to
exists in none of Firaxis' 645 shipped Lua files (`spikes/lua-accessor-audit-2026-09-21.md`). So
`lua/ingame/screens.lua`'s `respond` returns the button's own on-screen rectangle and
`capability/executor.py` clicks its centre through `HostPlatform.send_input` (XTest on Linux). That
is a bespoke path in exactly the sense Principle II means, and until 2026-09-22 it was declared
`path: firetuner` with `firetuner_gap: null` — the measured gap recorded only in a Python comment.

**Corrected, not excused.** `prompts.orders` is now declared `path: bespoke` in
`catalogs/capabilities.yaml` and carries the measured gap verbatim, together with what the bespoke
path does. It was **not** split into a Firetuner capability plus a bespoke one, because every
answerable prompt family in `CivSim_Screens_RespondToPrompt` — the acknowledge-only popups, the
diplomatic approach, the era dedication chooser, the congress page — can return a host-click
request, so a split would leave no declaration on the pure-Firetuner side and would be a fiction.
Firetuner-first still holds *within* the capability: where a direct call exists
(`DiplomacyManager.AddResponse` / `CloseSession`, `UIManager:DequeuePopup`) it is the primary route,
the click is the fallback, and every result records `path` / `path_reason` saying which ran and why.
**C1 is therefore live, not discharged** (see Complexity Tracking below).

**The load-time validator could not have caught this, and now something can.**
`models/catalog.py`'s `_bespoke_requires_gap` only fires on `path: bespoke`, so a mislabelled
bespoke path passed it vacuously — the check was structurally blind to the one failure that
matters. `tests/contract/test_synthetic_input_declaration.py` closes that hole: it pins every
in-harness `HostPlatform.send_input` call site as data, re-derives the set from the source with
`ast` so a new synthetic-input path cannot ship without being declared, and asserts the capability
each one serves is `path: bespoke` with a non-empty `firetuner_gap`.

**Revision 3 strengthens rather than weakens this.** Firetuner-first is now also the *portability*
argument: everything on the FireTuner path is portable for free, because the tuner interface exists
in all three native builds and the protocol is plain TCP. Only the bespoke path has to be written
three times. That gives the principle a second, practical reason to hold — and makes the R5 spike
more valuable than it was, since finding a Lua save path removes the per-platform synthetic-input
work entirely and is what would let Wayland be supported at all (R19).

### Principle IV — Reproducible, Seeded Experimentation

**PASS.** Per-turn quicksave before any observation or action, with the turn refusing to proceed if
it fails (FR-007); save points addressable by run/turn/lineage without touching the filesystem
(FR-032); branch lineage recorded and parent records immutable (FR-033 – FR-035).

Two parts of this principle were strengthened in revision 2 and deserve stating rather than
summarising, because both defend against *silent* loss of comparability — the specific failure this
principle exists to prevent:

- **Retention is explicit-archival-only** (FR-036, R17). A save becomes eligible for removal when an
  operator archives its run, and on no other basis — not age, not a quota, not a retention window,
  not thinning a finished run. Every turn-start save is a branch point deliverable 4 may want, and
  an automatic rule discards branch points nobody decided to give up, months later, unobserved. The
  cost of this is a disk that fills; the design accepts that and halts the run at a configured
  headroom floor rather than deleting to continue.
- **The game build is pinned per seed set** (FR-002, FR-031, R18). Civ VI patches itself, and a
  balance change mid-set makes halves of the set incomparable while every other recorded field still
  matches. The build is checked at preflight and a mismatch fails the run before turn 1; an operator
  may accept the change for that set, which is recorded on the set, on the timeline, and on every
  run that relied on it, so a mixed-build set can never be mistaken for a uniform one.
- **Platform is part of that pinned identity** (revision 3, R20). The macOS and Linux ports are
  separately built binaries whose version numbering need not track the Windows build's, so a set
  spanning two platforms carries the same silent-incomparability hazard as one spanning two builds —
  and nothing else in the record would reveal it. `game_build` is therefore a composite of platform
  and version, and accepting `win/1.0.12.9 → win/1.0.12.11` does not also accept `win → mac`.
  Cross-platform branching is refused by default for the same reason: Civ VI's cross-platform saves
  are account-gated and version-matched, which is not a foundation for "two branches from the same
  save point begin from an identical position" (FR-034). Whether such a save loads identically is a
  spike, not an assumption — and only the relaxation depends on it, never a core capability.

### Principle V — Guidebook-Before-Optimization Gate

**NOT TRIGGERED.** This feature builds the branch-from-save mechanism; it does not run Monte Carlo
search, ablation, or optimization. Deliverable 4 is where the gate binds. FR-021 consumes
`GUIDEBOOK.md` as run-independent guidance when it exists and does not require it to exist.

### Principle VI — Shared, Unified Observability

**PASS, with the boundary made explicit.** The harness writes what deliverable 1 reads and presents
nothing itself beyond lifecycle control and diagnostics (FR-053). Enforcement is structural: the
operator surface binds loopback only (127.0.0.1) and exposes lifecycle state and diagnostics,
deliberately *not* turn records, decisions, metrics, or captures — so it cannot drift into a second
picture. Deliverable 1 binds the LAN address. See Complexity Tracking C3.

Revision 2 added two diagnostic fields — `current_step` and `requested_state` — because an unbounded
turn can legitimately occupy a run for hours and an operator otherwise cannot distinguish working
from wedged. Both are bare scalars; the step's observation, decision, and reasoning stay on the
store side of the FR-053 line. This is the boundary being held under pressure rather than moved.

### Principle VII — Provider-Agnostic Model Access & Resilience

**PASS.** OpenRouter over plain HTTP with no vendor SDK (FR-037); model choice is configuration only
(FR-038); retry, fallback, and served-model accounting are recorded as run events (FR-040, FR-041);
crash detection, save preservation, and bounded recovery satisfy the resilience half (FR-044 –
FR-048). With one call per decision step, fallback is decided per step, so a turn may be served by
more than one model and SC-016's distinguishability resolves at step granularity.

**Gate result: PASS — proceed.** Re-evaluated in full against the revised spec; no unjustified
violations. Four justified items are recorded in Complexity Tracking — C1–C3 unchanged by the
clarifications, which strengthened compliance with Principles III and IV rather than requiring any
new deviation, plus C4 for revision 3's host layer.

**2026-09-22 re-check — this gate should not have read PASS on Principle II, and now does again for
a different reason.** Between the prompt host-click landing and this correction, a bespoke
synthetic-input path was shipping declared `path: firetuner`; Principle II's requirement is not
that the bespoke path be avoided but that it be *declared and its gap recorded*, and it was not.
The gate reads PASS now because the declaration was corrected to match what ships — not because the
principle was relaxed to fit it. C1 is live rather than discharged, which is a justified deviation
recorded in Complexity Tracking, not a violation. **Every statement in this Constitution Check is
now a statement about what shipped**, which is the standard it failed at on Principle II: the
Principle I surfaces table (below) and the requirement count were both still describing the design.

**Revision 3 re-check**: no principle is weakened by going cross-platform, and two are better
served. Principle I is unaffected because the catalog and filter contain no OS-specific concept —
only capture varies, and it fails safe per platform. Principle II gains a second argument, since
everything on the FireTuner path is portable for free and only the bespoke path is written three
times. Principle IV is strengthened: platform joins the build pin, closing a silent-incomparability
hole that a Windows-only design had no reason to notice and a cross-platform one would otherwise
have opened.

**One judgement worth recording, because it looks like a deviation and is not**: the within-turn
loop makes per-turn cost unbounded, and no principle permits or forbids that. The spec's Assumptions
take it deliberately as a trade for play fidelity, and FR-014 forbids capping a turn to contain it.
So the harness records per-call cost and manages none of it. A future cost control would be a spec
change, not an implementation decision.

## Project Structure

### Documentation (this feature)

```text
specs/002-civ-playing-harness/
├── plan.md                          # This file (/speckit-plan command output)
├── research.md                      # Phase 0 output
├── data-model.md                    # Phase 1 output
├── quickstart.md                    # Phase 1 output
├── contracts/                       # Phase 1 output
│   ├── README.md
│   ├── capability-catalog.md
│   ├── run-configuration.md
│   ├── match-store-port.md
│   ├── model-provider-port.md
│   ├── operator-surface.md
│   └── nexus-protocol.md
├── checklists/
│   └── requirements.md              # Existing — spec quality gate
└── tasks.md                         # Phase 2 output (/speckit-tasks — NOT created here)
```

### Source Code (repository root)

```text
catalogs/                            # The parity boundary, as versioned reviewable data
├── VERSION                          # Catalog version string, recorded on every run
├── observations/                    # One YAML file per observation domain
│   ├── map.yaml  cities.yaml  units.yaml  research.yaml  diplomacy.yaml
│   ├── religion.yaml  government.yaml  congress.yaml  espionage.yaml
│   └── views.yaml                   # Declared visual views (camera state + what it shows)
└── actions/
    ├── units.yaml  cities.yaml  research.yaml  policies.yaml  religion.yaml
    ├── diplomacy.yaml  congress.yaml  great_people.yaml  prompts.yaml
    ├── camera.yaml                  # Camera moves as declared, rejectable actions
    └── turn.yaml                    # end_turn — a declared decision, not harness control flow

lua/                                 # Declared Lua, reviewable as source not inline strings
├── gamecore/                        # Read-only queries (GameCore_Tuner context)
└── ingame/                          # Orders and UI-bound reads (InGame context)

src/civsim_harness/
├── config/          # Run configuration, seed sets, guidance loading, secrets resolution
├── nexus/           # Firaxis Nexus wire protocol: framing, handshake, contexts, heartbeat
├── capability/      # Catalog loading, versioning/hashing, registry, firetuner vs bespoke binding
├── parity/          # Structural filter, image screening, camera validation, forbidden-field guard
├── observe/         # Structured state assembly, capture pipeline, screen-identity probe
├── act/             # Action dispatch, execution verification, game-prompt/interrupt handling
├── agent/           # Context assembly, decision request/parse, reasoning capture
├── provider/        # Provider port, OpenRouter adapter, chain preflight, fallback, accounting
├── run/             # Lifecycle state machine, turn cycle, decision-step loop, no-progress
│                    #   backstop, stop conditions, build-pin preflight
├── saves/           # Quicksave, save-point addressing, lineage, branching, archival + disk headroom
├── resilience/      # Crash/hang detection, recovery, bounded retry, degradation marking
├── store/           # MatchStore port, SQLite+blob reference adapter, write-before-advance guard
├── operator/        # Typer CLI + loopback-only lifecycle API (no run-state presentation)
├── host/            # THE ONLY PLACE AN OS-SPECIFIC IMPORT MAY APPEAR
│   ├── port.py      #   HostPlatform protocol: window identity, capture, paths, input, disk
│   ├── detect.py    #   Platform + session detection (incl. X11 vs Wayland), tier resolution
│   ├── windows/     #   pywin32, Windows.Graphics.Capture, SendInput
│   ├── macos/       #   Quartz window list, ScreenCaptureKit, CGEvent
│   └── linux/       #   XComposite / portal capture, XTest input, Aspyr paths
└── telemetry/       # Out-of-game structured logging with mandatory credential redaction

tests/
├── unit/            # Codec, filter, catalog resolution, redaction, state machines
├── contract/        # Schema conformance, store/provider port conformance, parity red-team suite
├── integration/     # Turn cycle against recorded-transcript fake Nexus + fake provider
└── live/            # Marked: real Civ VI client — preparation, recovery, branching, hygiene
```

**Structure Decision**: Single Python project. The harness is one process against one game client,
so splitting it into services would add coordination without buying isolation. The two directories
that are deliberately *not* Python — `catalogs/` and `lua/` — carry the parity boundary and the
game-touching code as reviewable data and source, so that an auditor satisfying SC-006, SC-007, and
SC-020 reads declarations rather than tracing control flow. `src/` subpackages map one-to-one onto
the spec's requirement groups, which keeps the traceability in `tasks.md` mechanical.

**`host/` is the one subpackage that maps to a platform rather than a requirement group**, and it is
drawn that way on purpose. Collecting every OS-specific import into one directory makes "is the
harness platform-neutral?" answerable by looking at an import graph instead of by reading the other
**fifteen** — the same reasoning that puts the parity boundary in `catalogs/` as data.
*(Corrected 2026-09-22 from "thirteen subpackages". `src/civsim_harness/` holds **16** top-level
packages, counting directories one level down and excluding `__pycache__`: `act`, `agent`,
`capability`, `config`, `host`, `models`, `nexus`, `observe`, `operator`, `parity`, `provider`,
`resilience`, `run`, `saves`, `store`, `telemetry`. Fifteen is what is left once `host/` itself is
set aside. The counting rule is stated so the number can be re-derived rather than taken on
trust.)* A lint rule
enforcing it is cheap; the CI matrix catches what the lint rule misses.

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| **C1** — *(LIVE, and not where it was planned — corrected 2026-09-22. The R5 spike returned Outcome A, so the **save/load dialog driver** this row was written for was never taken and is now forbidden. But a bespoke synthetic-input path did ship, for a different surface: answering an in-game **prompt**, where the measured gap is that no Lua API reachable from `InGame` fires a control's registered callback. `prompts.orders` is declared `path: bespoke` with that gap. This row previously read DISCHARGED, which was true of the save path and false of the harness.)* A bespoke input-automation path alongside the Firetuner-first rule of Principle II | Reliable named per-turn quicksave is required by Principle IV and FR-007, and no documented Civ VI Lua call performs a save-to-named-file from the tuner contexts (R5). A turn cannot proceed without its quicksave, so this is load-bearing, not convenience. | Pure Firetuner rejected because the capability appears absent, not merely awkward — the spike in R5 keeps the Firetuner-first order by requiring a documented negative result before the bespoke path is enabled. Relying on the game's own autosave rotation rejected because autosaves are neither named nor addressable per turn, which breaks FR-032 and branch identity (FR-034). |
| **C2** — *(RESOLVED BY LANDING, not by replacement — annotated 2026-09-22. Deliverable 3 landed on 2026-09-21 (`93a9b7e`) and **extended** this adapter rather than replacing it: `class SqliteMatchStore(SqliteReadBase)` in `store/sqlite_adapter.py` is the same class behind the same port. The dependency direction this row defends held, so the deviation cost nothing — but "deliverable 3 implements the same port and replaces the adapter" did not happen and should not be read as a pending step. One behaviour arrived with it that 002's plan does not otherwise describe: an open-time orphan sweep that pauses runs on write-mode open (003 rule W6), so opening the store is not inert.)* A local SQLite + blob reference adapter for a store that deliverable 3 owns | FR-013 and FR-051 make persistence a precondition of every turn advance, so the harness cannot be built or tested before deliverable 3 exists. The port keeps the dependency direction correct: the harness depends on a contract, not on an implementation. | Blocking on deliverable 3 rejected because it serializes two deliverables that a published contract lets proceed in parallel. Writing local files directly rejected because it would be exactly the bypassing path Principle III forbids — the adapter is behind the same port the real store will implement, and port conformance tests run against both. |
| **C3** — An operator control surface on a harness whose presentation belongs to deliverable 1 | FR-004 requires lifecycle commands without touching the game client, and deliverable 1 is deliberately read-only (its FR-026), so lifecycle control has nowhere else to live. | A shared surface rejected because FR-053 and Principle VI forbid a second presentation of run state that could diverge. Mitigation is structural rather than a rule: loopback-only binding and a command/diagnostics-only schema that carries no turn records, decisions, metrics, or captures. |
| **C4** — Three implementations of the host layer, and a support tier that admits some platforms are weaker | No principle requires cross-platform support, so this is complexity taken on deliberately rather than forced. It is justified by what it removes: revision 1's Windows pin was based on a factual error (R1), and leaving it in place would have hard-coded a false constraint into the one deliverable everything else depends on. Capture and synthetic input genuinely differ per OS and cannot be abstracted away, only isolated. | **Windows-only** rejected because the constraint was never real — the tuner interface ships in all three native builds and the harness already bypasses the Windows-only GUI. **A single cross-platform automation framework** rejected because it would pull a large uninspectable surface into the most parity-sensitive path for six narrow capabilities. **Claiming uniform support** rejected as the actively harmful option: Wayland blocks synthetic input and gates capture behind an interactive grant, so a uniform claim would produce silently worse runs on some platforms — the tiers exist so a weaker platform is less capable without being less honest. |
