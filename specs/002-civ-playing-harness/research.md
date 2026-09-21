# Phase 0 Research: Civilization-Playing Harness

**Feature**: `002-civ-playing-harness` | **Date**: 2026-09-19 | **Plan**: [plan.md](./plan.md)

Purpose: resolve every unknown in the plan's Technical Context before design, and record why each
choice was made so a later reader can re-open it deliberately rather than by accident.

**Status**: all Technical Context unknowns resolved. Three findings carry mandatory validation
spikes (R5, R6, R10) whose failure modes are designed for rather than assumed away.

**Revision 2 (spec clarification session 2026-09-19)**: R12 and R14 were rewritten, and R10, R5, and
R17 – R18 added or amended, after the spec resolved five questions that changed load-bearing
decisions — the within-turn interactive decision loop, the removal of the per-turn time budget in
favour of a no-progress backstop, explicit-archival-only save retention, and the game-build pin.
Superseded reasoning is marked rather than deleted where it explains why the earlier shape was
considered.

**Revision 3 (cross-platform correction)**: R1's platform claim was factually wrong — it declared
Linux unsupported by conflating the Windows-only FireTuner **GUI** with the tuner **interface in the
game client**, which is present in the native Windows, macOS, and Linux builds. R1, R5, and R6 were
corrected, and R19 – R20 added for the host-platform port and for platform as a comparability axis.
The game-facing design was unaffected: only the host layer was ever Windows-bound.

---

## R1 — Language, runtime, and host platform

**Decision**: Python 3.12+, managed with `uv`, running natively on **Windows, macOS, or Linux** on
the same machine as the Civilization VI client. Proton/Wine is excluded.

**Rationale**:

- The Nexus transport is a long-lived asynchronous TCP conversation with interleaved output; stdlib
  `asyncio` covers it without a dependency, and the whole harness is naturally one event loop
  coordinating a socket, a subprocess, a capture source, and an HTTP client.
- **Python runs everywhere Civ VI does**, and the OS-specific work the harness needs — window
  identity, process liveness, screen capture, synthetic input — has maintained bindings on all
  three platforms. None of it is in the transport, which is plain TCP; it is all in the host layer
  isolated behind the port in R19.
- Established prior art for FireTuner automation is Python 3.12+ and documents Windows, macOS, and
  Linux, which reduces protocol risk from "unknown" to "documented by an independent
  implementation" on every target platform.

**Revision 3 correction**: revision 1 stated "Windows 11" as the target and declared Linux
unsupported. That was wrong, and the error is worth naming because it nearly became structural. It
conflated two different things:

| Thing | Platforms | Does the harness need it? |
|---|---|---|
| **FireTuner.exe**, the SDK's GUI debugger, shipped with the Windows-only Development Tools | Windows only | **No.** The harness speaks the Nexus protocol directly over TCP and never opens the GUI — already stated in contracts/nexus-protocol.md |
| **The tuner interface in the game client** — the TCP 4318 listener the protocol talks to | Windows, macOS, Linux (all native builds) | **Yes**, and it is present on all three |

Because the harness was already designed to bypass the GUI — for image-hygiene reasons (R6, R7), not
portability ones — the integration path is cross-platform as designed. The Windows dependency was
in the *host* layer, never the *game* layer.

**Enabling the tuner differs by platform** and is an operator prerequisite, not harness code:

| Platform | How |
|---|---|
| Windows | In-game Options → "Tuner (disables achievements)" |
| macOS | Set `EnableTuner 1` in `~/Library/Application Support/Sid Meier's Civilization VI/Firaxis Games/Sid Meier's Civilization VI/AppOptions.txt` — the setting is not exposed in the menu |
| Linux | Set `EnableTuner 1` in `~/.local/share/aspyr-media/Sid Meier's Civilization VI/AppOptions.txt` — likewise not in the menu |

**Alternatives considered**:

- **C#/.NET** — the most natural fit for WinRT capture and the Civ VI SDK's own tooling, and a real
  contender. Rejected because the rest of the project (deliverables 3 and 4) is data- and
  model-facing work where Python's ecosystem is stronger, and a single-language repo keeps the
  catalog tooling, the store adapter, and the harness sharing one schema layer.
- **Rust** — best transport and reliability story, worst iteration speed for a research harness whose
  catalogs will change weekly. Rejected on that trade.
- **Node/TypeScript** — shares the web stack with deliverable 1, but Windows capture and input
  interop are markedly weaker. Rejected.

**Platform note — what remains genuinely out of scope**: Civ VI under **Proton or Wine**. The tuner
interface is built into the native binary and is not exposed when the Windows build runs under a
compatibility layer, so a Linux user must install the native Aspyr port rather than the Windows one.
This is the one part of revision 1's platform claim that survives, and it makes the spec's "same
machine as the client" assumption a hard constraint rather than a convenience.

**Sources**: [civ6-mcp](https://github.com/lmwilki/civ6-mcp),
[FireTuner setup](https://jonathanturnock.github.io/civ-vi-modding/docs/fire-tuner/),
[Civ VI Mac FAQ](https://support.aspyr.com/hc/en-us/articles/213487546-Civilization-VI-Mac-FAQ),
[Civ VI Linux FAQ](https://support.aspyr.com/hc/en-us/articles/360020392091-Civilization-VI-Linux-FAQ)

---

## R2 — Game transport: the Firaxis Nexus / FireTuner protocol

**Decision**: Implement a first-party asyncio client for the Firaxis Nexus wire protocol on TCP
`127.0.0.1:4318`. No third-party transport library.

**Protocol as implemented**:

| Field | Type | Meaning |
|---|---|---|
| `length` | `uint32` little-endian | Payload size in bytes, including the null terminator |
| `tag` | `int32` little-endian | Message class |
| `payload` | UTF-8, null-terminated | Command or response text |

- `TAG_HANDSHAKE = 4` — identifies the application and enumerates available Lua states, via `APP:`
  and `LSQ:` prefixed payloads.
- `TAG_COMMAND = 3` — executes Lua, payload `CMD:<state_index>:<lua_code>`.

**Rationale**: the protocol is small, undocumented by the vendor, and load-bearing for parity
auditing. A first-party implementation of ~200 lines is easier to review against Principle I than a
dependency, and it lets us add the correlation discipline below, which no off-the-shelf client has.

**Two refinements over the prior art**:

1. **Correlated sentinels rather than a fixed `---END---`.** Output arrives asynchronously and
   fragments across packets; a fixed sentinel cannot distinguish this request's output from stray
   prints or a previous request's tail. Each command is wrapped with a per-request nonce
   (`print("---BEGIN:<nonce>---")` … `print("---END:<nonce>---")`), and anything outside a matched
   pair is routed to out-of-game telemetry rather than parsed as a result.
2. **Structured payloads, not printed prose.** Commands return a single JSON document between the
   sentinels. The protocol has no native return values — everything is `print` output — so making
   the Lua side responsible for serialization keeps the Python side free of ad-hoc text parsing, and
   makes a capability's output shape testable against a schema.

**Alternatives considered**:

- **Memory reading / process injection** — rejected outright: it is the single most likely source of
  a Principle I violation, and an injected reader has no natural parity basis.
- **Pure UI automation with OCR for everything** — rejected as a default because it is brittle, slow,
  and lossy; it is retained only as the documented-gap path (R5), which is the ordering Principle II
  requires.
- **A mod that opens its own socket** — rejected: it duplicates FireTuner's role, and gameplay Lua
  scripts are not reloaded with a save, which would silently break resume and branching (FR-033).

**Sources**: [FireTuner connection layer](https://deepwiki.com/lmwilki/civ6-mcp/2.2-firetuner-connection-layer),
[civ6-mcp](https://github.com/lmwilki/civ6-mcp)

---

## R3 — Lua execution contexts

**Decision**: Treat the two Lua VMs as distinct, declared execution contexts. Every catalog entry
names the context it runs in; the client refuses to execute an entry in the wrong one.

- **`GameCore_Tuner`** — read-only state queries. Faster and more stable; cannot issue orders or
  reach UI-bound data.
- **`InGame`** — required for unit orders, city production changes, and UI-bound surfaces such as
  diplomacy and World Congress.

**Rationale**: the context split is not an implementation detail — it partitions the catalog into
"reading the board" and "touching the board," which is the same partition Principle I cares about.
Making it explicit means an observation entry that silently needs `InGame` is a schema error found at
catalog-load time rather than a surprise mid-run.

**Consequence for design**: state discovery happens at handshake and the resolved state indices are
recorded with the run, because indices are positional and not guaranteed stable across game versions.

---

## R4 — Single-connection constraint and run identity

**Decision**: Rely on the game's one-tuner-connection limit as the primary enforcement of FR-006,
and add a run-identity lock file keyed to the client's process ID and the run ID as a second gate.

**Rationale**: Civ VI accepts only one FireTuner client at a time, so a second harness instance
cannot interleave actions into the same game — the failure is loud at connect time rather than
silent corruption. The lock exists for the case the game's limit does not cover: two harness
instances pointed at the *same run identity* against *different* clients, which would produce two
divergent records under one run ID.

**Alternatives considered**: a coordination service — rejected as disproportionate for a
single-machine, single-client deliverable whose parallelism story is explicitly "run more machines."

---

## R5 — Named per-turn quicksave (the first documented Firetuner gap)

**Decision**: Attempt the save through FireTuner first; on a documented negative result, implement
`save_game` as a **bespoke** capability that drives the in-client Save Game dialog with synthetic
input and verifies the result on the filesystem. Record it under FR-028 with its gap statement.

**Finding**: Civ VI's Lua surface is substantially narrower than Civ V's. `UI.QuickSave` is a Civ V
API; no equivalent save-to-named-file call is documented for Civ VI's tuner-reachable contexts, and
the independent prior art routes save and load through OCR-driven menu navigation rather than Lua —
which is strong evidence of absence, though not proof.

**Therefore this is a spike, not an assumption.** Before implementation, probe the `InGame` context
for a callable save path (`Network.*`, `UI.*`, `Game.*` enumeration via the tuner). The spike has
three possible outcomes and the design handles all three:

| Outcome | Action |
|---|---|
| A Lua save path exists and works | Use it. `path: firetuner`. No bespoke capability, C1 disappears. |
| No Lua save path | Bespoke `save_game`, with the enumeration output captured as the documented gap evidence. |
| A path exists but is unreliable | Bespoke, with the reliability evidence as the gap statement. |

**Bespoke design when needed**:

- **Parity basis**: Esc → Save Game → type a name → Save. This is a path a human takes, which is why
  it is declarable at all.
- **Naming**: `civsim__<run_id>__t<turn:04d>` — collision-free, sortable, and parseable back to run
  and turn without a filesystem scan.
- **Verification is filesystem, not optimism**: the save is confirmed by the `.Civ6Save` appearing in
  the platform's save directory, with its size stable across two reads, before the turn proceeds. A
  quicksave that cannot be verified fails the turn (FR-007) rather than being recorded as taken.
- **Addressing**: the harness records a save-point record; callers refer to run/turn/lineage and
  never to a path (FR-032). This was already the rule, and it is what makes the per-platform save
  directory an implementation detail of one resolver rather than something threaded through the
  harness.

**Save directories differ by platform** and are resolved by the host port (R19), never hard-coded:

| Platform | Save directory |
|---|---|
| Windows | `%USERPROFILE%\Documents\My Games\Sid Meier's Civilization VI\Saves\Single\` |
| macOS | `~/Library/Application Support/Sid Meier's Civilization VI/Sid Meier's Civilization VI/Saves/Single/` |
| Linux | `~/.local/share/aspyr-media/Sid Meier's Civilization VI/Saves/Single/` |

The exact leaf layout is confirmed per platform by the R5 spike rather than assumed, since Aspyr has
relocated these directories across updates before.

**The bespoke fallback is per-platform, and this is where portability actually costs something.**
If R5 finds no Lua save path, driving the Save Game dialog with synthetic input has to be
implemented three times, and the platforms are not equally cooperative:

| Platform | Synthetic input path | Risk |
|---|---|---|
| Windows | `pydirectinput` / SendInput | Low — the documented prior art |
| macOS | `CGEvent` via Quartz | Medium — requires the user to grant Accessibility permission, which is a one-time operator prerequisite, not something the harness can self-serve |
| Linux / X11 | `XTest` (e.g. `xdotool`) | Low |
| Linux / Wayland | **Largely blocked** — Wayland deliberately prevents one client from synthesising input into another | High. See R19's support tiers |

This is load-bearing rather than cosmetic: FR-007 says a turn may not proceed without its quicksave,
so a platform where neither the Lua path nor synthetic input works cannot run the harness at all.
That is why R19 states support as tiers rather than a yes/no.

**Alternatives considered**:

- **Use the game's autosave rotation** — rejected: autosaves are unnamed, rotated, and not per-turn
  addressable, which breaks branch identity (FR-034) and retention safety (FR-036).
- **Copy the autosave file to a stable name** — rejected as a primary: it inherits the rotation's
  cadence rather than guaranteeing a save at *this* turn's start, which is precisely what Principle
  IV requires.

**Sources**: [UI.QuickSave (Civ5 API)](http://modiki.civfanatics.com/index.php?title=UI.QuickSave_(Civ5_API)),
[Current status of Lua modding](https://forums.civfanatics.com/threads/current-status-of-lua-modding.603706/),
[civ6-mcp](https://github.com/lmwilki/civ6-mcp)

---

## R6 — Screen capture path (and the hygiene gate)

**Decision**: Capture with **Windows.Graphics.Capture** via `IGraphicsCaptureItemInterop::CreateForWindow`
on the Civ VI window handle. Adopt it only after a **capture-hygiene spike** proves the resulting
frames are clean; until then, no run may show images to the agent.

**Why this path**: `CreateForWindow` targets a single window by HWND rather than a screen region.
The strong expectation is that it captures the window's own composed content independently of
z-order — which, if true, means a FireTuner window sitting on top of the game is *structurally*
absent from the frame rather than merely filtered out of it. That is a much better position for
FR-025 and FR-030 than any region-capture approach, because hygiene stops depending on detection.

**This is a hypothesis, and the spike must confirm it.** Microsoft's own documentation describes
`CreateForWindow` as targeting a single window but does not state occlusion behaviour. Two risks
must be cleared before the path is trusted:

1. **Occlusion**: place the FireTuner window, a console, and an unrelated window over the client and
   confirm none appears in the captured frame.
2. **The capture border**: Windows draws a yellow border on captured visuals as a user-facing
   recording indicator. That border is harness-caused, non-player UI inside the agent's image — a
   direct FR-025 problem. The spike must confirm it can be suppressed (`IsBorderRequired = false`,
   where the OS build permits) or that it falls outside the captured surface.

**Ranked fallbacks on Windows, if the spike fails**:

| Rank | Path | Trade-off |
|---|---|---|
| 1 | Windows.Graphics.Capture, `CreateForWindow` | Occlusion-immune if confirmed; border risk |
| 2 | DXGI Desktop Duplication + crop to client rect | Fast and reliable, but captures whatever is on top — hygiene reverts to detection, and the harness must additionally guarantee nothing is ever placed over the client |
| 3 | `PrintWindow` with `PW_RENDERFULLCONTENT` | Occlusion-immune in principle, unreliable for DirectX swapchains — likely blank or stale frames |
| 4 | No capture path | Every turn runs visually degraded under FR-050; the run is still valid and still recorded, just marked |

### Capture is per-platform, and so is its hygiene spike

Capture is the single most platform-bound capability in the harness, and the one where getting it
wrong is a release-blocking parity defect rather than an inconvenience (SC-009, SC-019). Each
platform gets its own ranked list and its **own** hygiene spike — a spike passing on Windows says
nothing about macOS.

| Platform | Preferred path | Occlusion immunity | Notes |
|---|---|---|---|
| Windows | Windows.Graphics.Capture `CreateForWindow` | Expected, unconfirmed | Capture-border risk (`IsBorderRequired = false`) |
| macOS | ScreenCaptureKit with an `SCContentFilter` scoped to the single game window | Expected, unconfirmed | Requires Screen Recording permission — an operator prerequisite. Legacy `CGWindowListCreateImage` is the fallback |
| Linux / X11 | `XComposite` redirected window pixmap | Expected, unconfirmed | Needs a compositing setup; plain `XGetImage` on the root window is **not** occlusion-immune |
| Linux / Wayland | `xdg-desktop-portal` ScreenCast (PipeWire) | Portal-dependent | Requires an interactive permission grant, which is hostile to unattended runs; may not scope to a single window on every compositor |
| Any | No capture path | n/a | Visually degraded under FR-050 — valid, recorded, marked |

**The rule does not bend per platform.** On every one of them, an image that cannot be proven clean
is withheld and the step recorded visually degraded. What varies is how likely each platform is to
reach "proven clean," which is what R19's tiers record.

**The failure mode is designed, not hypothetical.** Rank 4 is a legitimate operating state: FR-050
and SC-013 exist precisely so that "the agent could not see" is recorded rather than papered over.
The one outcome the design forbids is showing the agent an image that has not been proven clean.

**Sources**: [New ways to do screen capture](https://blogs.windows.com/windowsdeveloper/2019/09/16/new-ways-to-do-screen-capture/),
[IGraphicsCaptureItemInterop::CreateForWindow](https://learn.microsoft.com/en-us/windows/win32/api/windows.graphics.capture.interop/nf-windows-graphics-capture-interop-igraphicscaptureiteminterop-createforwindow),
[Windows.Graphics.Capture namespace](https://learn.microsoft.com/en-us/uwp/api/windows.graphics.capture)

---

## R7 — Image screening as defence in depth

**Decision**: Screen every image through four independent gates before it may enter the agent's
context or the store. Any gate failing means withhold and re-capture; bounded retries then mark that
**decision step** visually degraded, which rolls up to the turn and then to the run's comparability
status. Screening runs per step, because captures are per step (R14, FR-015) — a turn is not
uniformly clean or dirty, and recording it as such would lose which part of the turn the agent
played blind.

| Gate | Check | Catches |
|---|---|---|
| **Source** | The frame came from the declared game-window capture item, never a desktop or region grab | Whole classes of leak at the origin |
| **Geometry** | Frame dimensions match the client rect; aspect and size within tolerance | Wrong-target captures, stale surfaces |
| **Provenance** | The frame is tagged with the declared view capability and camera state that produced it | Undeclared views (FR-024), unreachable camera states (FR-026) |
| **Content** | Detector for FireTuner chrome, developer console, debug overlay, harness UI, and the WGC capture border | Residual contamination if source immunity is imperfect |

**Rationale**: the spec treats a contaminated image as a release-blocking defect (SC-009, SC-019),
so a single detector is the wrong shape — detectors have false negatives, and a false negative here
silently invalidates a run. The first three gates make contamination structurally unlikely; the
fourth catches what they miss. Screening happens once, before both the agent and storage, so the
agent and the human record cannot disagree about what was clean.

**Explicitly rejected**: screening after storage, or showing a contaminated image with a warning.
FR-025 and FR-030 both say *withhold*, and deliverable 1 re-checks as a second gate rather than the
only one.

---

## R8 — Camera control as a declared, rejectable action

**Decision**: Camera moves, zoom changes, and view-mode toggles are entries in
`catalogs/actions/camera.yaml`, executed through `InGame` Lua, and validated before execution.

**Validation rules**: the requested target must be a plot the run has revealed; zoom must be within
the range the standard UI permits; view mode must be one a human can toggle. A request outside those
bounds is rejected and recorded with its reason, exactly like an illegal unit order (FR-026).

**Rationale**: with images in the agent's context, the camera *is* an information channel — pointing
it at an unrevealed tile would be an information request wearing a movement request's clothes.
Declaring it as an action rather than treating it as harness plumbing is what makes that request
rejectable and auditable.

---

## R9 — The parity boundary: catalogs as versioned data

**Decision**: Catalogs are YAML files under `catalogs/`, loaded and validated at startup, hashed, and
recorded with every run. Code binds to catalog entries; it never reaches the game directly.

**Design rules**:

1. **No raw-Lua path into context assembly.** The assembler's only input type is a capability result.
   A developer wanting new data must add a declaration — there is no shortcut that also works.
2. **`parity_basis` is required and non-empty**, and `firetuner_gap` is required when `path: bespoke`.
   The schema enforces both, so an undocumented capability fails to load rather than failing review.
3. **Version + content hash recorded per run** (FR-022), so runs before and after a catalog change
   remain distinguishable and auditable after the fact.
4. **Start-up gate**: preflight resolves every capability the run could use; any unresolved or
   undeclared entry aborts before turn 1 (FR-023).
5. **A forbidden-field guard** runs on the assembled context as a belt-and-braces assertion — a
   red-team list of things that must never appear (unrevealed plots, opponent internals, RNG state,
   harness telemetry), asserted in tests and at runtime (FR-019, FR-020, SC-008).

**Rationale**: the spec's audit requirement (SC-007 — an auditor enumerates every observation and
action from the record alone) is only achievable if the boundary is a document, not a code path.
YAML also means the catalog diffs in review as English parity statements.

**Alternatives considered**: Python decorators on capability functions — rejected because the
boundary would then be readable only by reading code, and the versioned artifact the run must
reference would have to be generated from it anyway.

---

## R10 — Model provider layer

**Decision**: OpenRouter over `httpx` using its OpenAI-compatible chat completions shape, with
images as `image_url` content parts. A `ModelProvider` port sits between the harness and the adapter
so a second provider is an adapter, not a refactor.

**Design points**:

- **No vendor SDK on the decision path** (FR-037). The adapter speaks HTTP and JSON directly.
- **One call, one decision** (FR-008). The port's response carries a single decision with its
  reasoning, not a list. The within-turn loop of R14 calls the provider once per decision step, so a
  turn's call count equals its step count and every call is attributed to the step it served. A
  list-shaped response would make batching the easy path, which the spec now forbids.
- **Chain preflight** (FR-039, SC-017): before turn 1, every model in the chain — primary and all
  fallbacks — is checked for image-input support and for a context window that can carry a
  worst-case *decision step* (late-game structured state plus that step's declared views). A chain
  that fails aborts the run. The harness has no code path that drops images to make a call fit; the
  capability simply does not exist.
- **Accounting from provider-reported usage** (FR-040): the served model, latency, retry count,
  fallback occurrence, and cost are taken from the response's usage/generation data rather than
  priced independently — consistent with the spec's assumption and immune to pricing drift.
- **Retry and fallback as recorded events** (FR-041): exponential backoff with jitter on transient
  failures, then the next model in the chain; every attempt is a run event. Because fallback is
  decided per call and a call serves one step, a run may mix models *within* a turn — so
  "distinguishable" (SC-016) is resolved at step granularity and rolled up to the turn, not asserted
  at the turn.
- **The call's own timeout is the only clock on a slow provider.** With no turn budget, a provider
  that responds slowly simply makes the turn longer; what handles a call that never returns is the
  per-request timeout and the retry ladder above it, not anything at the turn level (spec edge case).
- **An empty-but-successful response is a failed call, not a decision to do nothing** — an explicit
  edge case in the spec, handled in the adapter rather than left to the caller.
- **Credentials** resolve from environment or a secrets file, never from run configuration, and a
  redaction filter sits on every record, log, and context write path (FR-043, SC-018).

**Spike**: model capability metadata (image support, context length) must be read from OpenRouter's
models endpoint at preflight rather than hard-coded, since the model roster moves faster than this
repo will. If the endpoint cannot confirm a model's image support, the chain fails closed.

**Alternatives considered**:

- **Direct vendor SDKs with an abstraction layer** — rejected: Principle VII names the pluggable
  layer specifically, and per-vendor image encodings would leak into the agent module.
- **A local proxy (LiteLLM etc.)** — rejected as an unnecessary process on the critical path; the
  port already provides the substitution point.

**Sources**: [OpenRouter — every modality, one API](https://openrouter.ai/blog/insights/every-modality-one-api/),
[OpenRouter pricing](https://apimart.ai/blog/openrouter-pricing-explained-model-costs-credits-provider-routing)

---

## R11 — The match-store dependency

**Decision**: Define a `MatchStore` port in `contracts/`, and ship a local SQLite + content-addressed
blob adapter behind it so this feature is buildable and testable before deliverable 3 exists.

**Rationale**: FR-013 makes persistence a precondition of every turn advance, so the harness cannot
run at all against a missing store. Publishing the port first also gets the dependency direction
right — deliverable 3 implements a contract this feature states, rather than this feature adapting
to whatever deliverable 3 happens to build.

**Guard rails**:

- Port conformance tests run against both the reference adapter and, later, the real store, so
  swapping implementations is a configuration change.
- The write-before-advance guard lives in the turn cycle, above the port, so no adapter can weaken
  it.
- Record schemas are versioned and additive-only; deliverable 3 owns backward-readable migration as
  the constitution requires.

**Alternatives considered**: blocking on deliverable 3 — rejected, it serializes two deliverables a
contract lets run in parallel. Writing local files directly — rejected as exactly the bypass
Principle III forbids.

---

## R12 — Crash, hang, and unresponsiveness detection

**Decision**: Three independent signals, any of which trips detection, all within the 60 s budget of
SC-010.

| Signal | Mechanism | Catches |
|---|---|---|
| **Process liveness** | `psutil` on the client PID | Hard crash, process exit |
| **Tuner heartbeat** | Periodic nonce round-trip through `GameCore_Tuner` with a bounded timeout | Hang, deadlock, dropped tuner connection |
| **Per-operation bounds** | Every Nexus command, capture, and post-action read-back carries its own timeout | A client that is alive and answers the heartbeat but will not service a specific operation |
| **Screen-identity probe** | The declared screen-identity observation of R13, polled between decision steps | A game stuck on an unrecognised modal — recorded as `unknown_screen`, not as a crash |

**Rationale**: a single signal picks one failure shape. Process liveness misses hangs; a heartbeat
misses a game that answers Lua while the UI is stuck on a modal; per-operation bounds catch the
operation that never returns; the screen probe distinguishes "stuck on a screen we do not know" from
"broken."

**What is deliberately *not* a detection signal: elapsed turn time.** The spec's clarification
removed the per-turn time budget entirely (FR-014), so a long turn is not evidence of anything. A
late-game turn may legitimately run to hundreds of decision steps and hours of wall clock, and the
harness must not infer a fault from that. Every bound above is scoped to a single operation, which
is what keeps SC-010's 60 s detection budget satisfiable without reintroducing a turn timer through
the back door.

*Superseded*: revision 1 used a turn-phase watchdog against a configured budget as the third signal
and had it produce FR-014's stall outcome. That conflated two unrelated things — a game that has
stopped responding, and an agent that has stopped accomplishing anything — and the latter is now the
turn cycle's own no-progress backstop (R14), tracked in the turn record rather than in the detector.

**Recovery** (FR-045 – FR-048): preserve last-known-good state, resume from *this turn's* start
quicksave as the same continuous run, re-observe and re-capture before acting (FR-046 — nothing
obtained before the interruption is reusable), record the abandoned attempt alongside the replayed
one with the replay marked authoritative (FR-047), and stop in a recorded failed state after the
configured consecutive-failure bound rather than looping (FR-048).

---

## R13 — Unknown screens and game-initiated prompts

**Decision**: Add a **screen-identity** observation — "which screen is currently up" — as a declared
catalog entry, probed every turn and after every interruption. Known screens map to declared prompt
handlers; anything unrecognised stalls the run visibly (FR-049).

**Rationale**: full-game coverage means the harness will meet screens it has never seen — era
transitions, new popups, patch-introduced dialogs. The spec's edge case is explicit that the wrong
answer is to click blindly or dismiss. Treating screen identity as an observation rather than
implicit control flow means "I do not recognise this" is a recordable state rather than an exception
trace.

**Prompts as decisions** (FR-010): each known prompt type — unit promotion, pantheon and religion,
great person, AI diplomatic approach, war declaration, city-state quest, Congress vote, era
transition — is an entry in `catalogs/actions/prompts.yaml`, answered by the agent and recorded like
any proactive decision, with a flag distinguishing responsive from proactive. Between-turn
interrupts route through the same path rather than being absorbed by turn advancement.

---

## R14 — The within-turn decision loop, its termination, and execution verification

**Decision**: a turn is an unbounded loop of **decision steps**, not one batched decision and not a
timed phase. Each step is observe → decide → execute → verify, and the loop's only two exits are the
agent's own end-turn decision and the no-progress backstop (FR-008, FR-014).

```text
turn start
  └─ quicksave (fails ⇒ turn does not exist)
     └─ loop, step n = 1, 2, 3, … unbounded:
          observe   — assemble a fresh parity-filtered context + fresh capture for THIS step
          decide    — one model call, one decision, with its reasoning
          execute   — dispatch through the action catalog
          verify    — read back game state against the action's verification predicate
          account   — changed state? reset no-progress counter. rejected or no change? increment
          exit?     — decision was end_turn, game confirmed it  ⇒ ended_by_agent
                      decision was end_turn, never confirmed    ⇒ end_turn_unconfirmed
                      counter == limit                         ⇒ ended_on_no_progress
     └─ persist the whole turn (fails ⇒ run halts)
        └─ issue the end-turn action
```

**Five consequences worth stating, because each is a place the obvious implementation is wrong:**

1. **One decision per model call, not a list.** FR-008 forbids asking the agent to commit to a later
   decision before it has seen the result of the earlier one. A provider port returning
   `list[RawDecision]` would make batching the path of least resistance, so the port returns exactly
   one decision (see [contracts/model-provider-port.md](./contracts/model-provider-port.md) P10).
   A turn contains as many model calls as it contained steps.

2. **The observation is step-scoped, not turn-scoped.** Re-observing after every executed decision is
   the whole point — the board the agent is shown must already reflect what it just did. The same
   holds for the image: FR-015 forbids reusing an earlier step's capture, so capture is inside the
   loop, once per step as the baseline.

3. **End-turn is a declared catalog action, not harness control flow.** It is dispatched, recorded,
   and distinguishable from a backstop exit (FR-008, SC-022). A turn in which the agent ends
   immediately without acting is a valid one-step turn, not a failure.

   **Amended 2026-09-21 (owner's ruling; gameplay block 7, `run-480aa573`).** This note used to say
   the loop ends `ended_by_agent` on the agent's end-turn *decision*, whatever its verification
   reported — written so a slow AI round could not be split across two records. Block 7 showed what
   that costs: five turn cycles, all at game turn 35, every one recorded `ended_by_agent`, each one
   an end turn that was dispatched and then `verification_failed` after the 45 s confirmation
   bound. The harness's turn had ended; the game's had not; and the record claimed the agent ended
   it. The bounded re-read already covers the slow-round case honestly, so past the bound the rule
   is now **record the truth and keep the liveness**:

   - An end turn dispatched but not confirmed within the bound is **`end_turn_unconfirmed`**, with
     `game_turn_advanced = false` on the turn cycle — never `ended_by_agent`.
   - The *harness's* own turn still advances on that outcome, so the loop cannot spin. That is the
     liveness half of this note and it is unchanged. `run/turn_cycle.py` issues no second end turn
     for it: the order already left the harness.
   - An end turn the dispatcher refused *before* it reached the game ended nothing and is a refused
     step like any other (amended earlier the same day, gameplay block 4).
   - The store's trending-eligibility rule excludes a run carrying such a cycle, exactly as it
     excludes one whose record has gaps (Constitution Principle III). It reads both the new flag and
     the game turn numbers already recorded in consecutive cycles' observations, so block 7's own
     five cycles are covered without a migration — see
     `specs/003-match-tracking-store/data-model.md` §3.5.

4. **No-progress is counted, not timed.** The counter increments on a step whose decision was
   rejected *or* whose verification showed no game-state change, and resets to zero on any step
   verified as having changed state. This targets a stuck agent specifically: a long, productive turn
   never trips it, and a fast agent spinning on rejected orders trips it quickly. The limit is run
   configuration (`no_progress_step_limit`), not a constant.

5. **A turn is never truncated for length or cost.** There is no wall-clock bound, no step cap, and
   no cost cap. Duration and per-call cost are recorded and left visible; the spec's assumption is
   explicit that unbounded per-turn cost is a deliberate trade for play fidelity.

**Mid-turn observation failure is an interruption, not a recovery-in-place.** If the context cannot
be assembled at step *n* after steps 1…*n*−1 have already executed and verified, the turn cannot be
finished from a stale board. The attempt is abandoned and replayed from its own start quicksave
under the R12 recovery path (spec edge case; FR-046). Continuing from the last good view would
silently violate the re-observe rule that justifies the loop's existence.

**Ordering and self-cancellation**: steps execute in issue order, which is now trivially true since
they are issued one at a time. The agent may undo or cancel its own earlier work later in the same
turn, having seen the result — each step is recorded as issued and the *resulting state* is what the
turn records, not the intent (spec edge case).

**Verification**: every action is verified against post-execution game state read back through
`GameCore_Tuner`, and recorded `applied`, `rejected`, or `partially_applied` with a reason. Each
catalog action entry declares its own verification predicate, so "verified" means something specific
per action rather than "the Lua call returned." FR-011 forbids recording an action as applied
without verification, which only has teeth if the verification is declared next to the action rather
than left to the executor's judgment. The verification result is doing double duty here — it is also
what feeds the no-progress counter, so a weak predicate that always reports success would both
mis-record the action and disable the backstop.

**Cost note**: one model call per decision step, each carrying images, is the dominant cost term in
the whole system. This is the accepted consequence of the clarification, recorded here so a later
reader sees it was chosen rather than stumbled into.

*Superseded*: revision 1 wrapped the turn in an `asyncio` timeout producing a `stalled` outcome, and
described decisions as a batch executed in issued order. Both are withdrawn — the spec's
clarification replaced the time budget with the no-progress count and the batch with the loop.

**Alternatives considered**:

- **Batched decisions with a mid-turn re-observation checkpoint** — cheaper by a large factor, and
  the shape revision 1 assumed. Rejected by the clarification: any batching means the agent commits
  to decision *n*+1 without seeing decision *n*'s effect, which is the specific thing FR-008 now
  forbids.
- **A step cap as a cheap backstop** — rejected because it cannot distinguish a productive late-game
  turn from a stuck one, which is exactly the distinction FR-014 asks for. The no-progress counter
  costs one comparison per step and makes that distinction correctly.

---

## R15 — Testing a harness that needs a running game

**Decision**: A recorded-transcript fake Nexus server plus a fake provider, so the full turn cycle
runs deterministically in CI; a separate marked live tier for what genuinely requires the client.

**Mechanism**: the real client records Nexus request/response transcripts during live sessions.
Those transcripts replay as a fake server, which makes turn-cycle logic, recovery paths, and record
shapes testable without Civ VI and reproducible on failure.

**The parity red-team suite** is the load-bearing test tier: a fixture list of values that must never
appear in an assembled context, asserted against contexts built from realistic transcripts. SC-006
and SC-008 make any finding release-blocking, so this runs on every build rather than per release.

**Live tier** covers what fakes cannot: run preparation against real settings, crash recovery with a
genuinely killed client, branch position identity, and capture hygiene. Marked and excluded from CI.

---

## R16 — Operator surface

**Decision**: A `typer` CLI for the common path, backed by a `fastapi` endpoint bound to `127.0.0.1`
only, exposing lifecycle commands (start, pause, resume, stop, resume-from-save, branch-from-save)
and diagnostics (lifecycle state, last error, connection health, last-known-good save).

**Rationale**: FR-004 requires lifecycle control without touching the game client, and deliverable 1
is deliberately read-only, so control has to live here. The risk FR-053 names is scope creep into a
second picture of run state — so the mitigation is structural rather than a policy: the loopback
binding means it is not reachable from the LAN devices deliverable 1 serves, and the response schema
carries no turn records, decisions, metrics, or captures. Wanting to add one would require changing
the contract, which is the point.

---

## R17 — Save retention: explicit archival, and the disk problem it creates

**Decision**: a save point becomes eligible for removal on exactly one event — an operator archiving
its run — and on no other basis. Not age, not a quota, not a retention window, not thinning a
finished run's saves (FR-036). Archival is itself a recorded lifecycle action and a run event.

**Rationale**: the clarification's reasoning is worth restating because it is the whole design.
Every turn-start save is a branch point, and deliverable 4's ablation work is the reason branch
points exist. Any automatic deletion rule — however generous — throws away a branch point that
nobody decided to give up, and it does so silently, months after the run, when the person who would
have objected is not watching. Making the operator say so is the only rule that cannot do that.

**The consequence is a disk problem, and it must be solved as one.** A 300+ turn full game produces
300+ saves and one capture per decision step; an unattended 20-run batch multiplies that. With
nothing deleting itself, the harness *will* meet a full disk. The spec's edge case is explicit about
which way to fail: report headroom and halt the run, never free space by deleting a save nobody
archived.

So disk headroom is a first-class run precondition, not an operational afterthought:

| Point | Behaviour |
|---|---|
| Preflight | Estimate the run's save + capture footprint from its stop condition; refuse to start below a configured floor |
| Per turn, before the quicksave | Check free space; below the floor, halt the run in a recorded state rather than taking the save |
| `doctor` | Reports current headroom and the estimated footprint of a configured run |
| Archival | An operator action that marks a run's saves eligible; records and captures are untouched |
| Reaper | A separate, explicitly-invoked operation over *already eligible* saves — never a background job, so it cannot run unattended |

**Why the reaper is not automatic**: a background reaper that only ever touches eligible saves would
be safe by construction today, and would be one refactor away from "eligible" quietly acquiring a
second meaning. Keeping deletion operator-invoked means the rule has no maintenance surface.

**Alternatives considered**:

- **Age- or quota-based retention with a generous window** — rejected by the clarification. It is
  the rule that looks harmless and deletes branch points nobody gave up.
- **Thinning a terminal run's saves to every Nth turn** — rejected for the same reason, more
  precisely: a terminal run is exactly the kind deliverable 4 wants to branch from.
- **Compressing saves in place** — not rejected, but out of scope here; it changes storage cost
  without touching the eligibility rule, so it can be added later without revisiting this decision.

---

## R18 — Pinning the Civilization VI build across a seed set

**Decision**: the seed set records the game build its runs are played on. At preflight the harness
reads the client's build and compares; a mismatch fails the run before turn 1 by default. An
operator may explicitly accept the new build *for that seed set*, which is recorded, and every run
that relies on the acceptance carries that fact (FR-002, FR-031).

**Rationale**: Civ VI patches itself without asking. A balance change between run 7 and run 8 of a
seed set makes the two halves incomparable, and the record would show nothing — the seed, the
civilization, the ruleset, and the mod set all still match. This is a silent-corruption failure of
exactly the kind Principle IV exists to prevent, and it cannot be detected after the fact from the
data.

**Design points**:

- **Reading the build.** Preferred path is a declared `GameCore_Tuner` observation of the game's
  version, which makes the check itself parity-declared like everything else. A spike confirms the
  version is reachable from Lua; the fallback is the client executable's file version, which is
  out-of-game provenance data and therefore never enters the agent's context either way.
- **Acceptance is per seed set, not global and not per run.** A global "ignore build changes" switch
  would be indistinguishable from not having the check. Recording it on the set is what lets an
  auditor see that a set spans two builds.
- **Acceptance is recorded on every dependent run** (`game_build_acceptance_ref` on the run), not
  only on the set. A run must be auditable from its own record without reconstructing the set's
  history at that moment in time.
- **The default is refusal.** An operator who wants to continue takes a deliberate, recorded action;
  an operator who is not watching gets a failed run, which is the loud outcome.

**Alternatives considered**:

- **Warn and continue** — rejected: it produces exactly the mixed-build set that reads as uniform,
  which is the failure being prevented.
- **Pinning the game to a version by blocking updates** — out of the harness's control and fragile;
  the check is needed regardless of whether updates are also suppressed.
- **Treating a build change as a new seed set automatically** — rejected: it discards the operator's
  judgment about whether the patch affects their experiment, and silently doubles the set count.

---

## R19 — The host platform port, and honest support tiers

**Decision**: isolate every OS-specific capability behind a single `HostPlatform` port with one
adapter per platform. Nothing outside `host/` may import a platform library or branch on the
operating system.

**What the harness actually needs from an OS** — a short list, which is why the abstraction is
cheap:

| Capability | Why | Windows | macOS | Linux |
|---|---|---|---|---|
| Locate the game process | Liveness detection (R12) | `psutil` | `psutil` | `psutil` |
| Identify the game window | Capture target, geometry gate (R7) | `pywin32` HWND | Quartz window list | X11/Wayland window id |
| Capture that window | The agent's images (R6) | WGC | ScreenCaptureKit | XComposite / portal |
| Resolve game directories | Saves, `AppOptions.txt` | `%USERPROFILE%\Documents\My Games\…` | `~/Library/Application Support/…` | `~/.local/share/aspyr-media/…` |
| Synthetic input *(only if R5 needs it)* | Save dialog driving | SendInput | CGEvent | XTest / blocked on Wayland |
| Free disk space | Headroom guard (R17) | stdlib | stdlib | stdlib |

**Everything else is already portable.** The Nexus client is plain TCP; the catalog, parity filter,
turn loop, store, and provider layer contain no OS-specific code and never should. That is the
argument for the port being small and strictly bounded rather than a general compatibility layer.

**Support tiers, stated honestly rather than aspirationally.** The spec's zero-tolerance parity
criteria mean a platform either meets them or it does not; pretending otherwise would let a
degraded platform's runs look like everyone else's.

| Tier | Meaning | Requirement |
|---|---|---|
| **Validated** | Full capability, proven by the live test tier on that platform, including a passing capture-hygiene spike | A seed set may be played here |
| **Supported** | Runs to completion with a verified quicksave path, but the capture spike has not passed — runs are visually degraded and marked as such | Usable, and comparable only to other runs in the same condition |
| **Unsupported** | No verified quicksave path (e.g. no Lua save call *and* synthetic input blocked) | The run refuses to start; FR-007 makes a turn without its quicksave impossible |

Wayland is the concrete case the tiers exist for: its capture story needs an interactive permission
grant that fights unattended operation, and its input story blocks synthetic events outright. If the
R5 spike finds a Lua save path, Wayland is plausibly Supported; if it does not, Wayland is
Unsupported and an X11 session is the documented workaround. **This is decided by spike results, not
declared here** — which is the same discipline R5 and R6 already follow.

**The harness reports its own tier** rather than leaving it to documentation: preflight resolves the
platform's capabilities and refuses to start an Unsupported configuration, and `doctor` prints the
tier with the reason.

**Alternatives considered**:

- **Windows-only, as revision 1 had it** — rejected once R1's error surfaced. The game layer was
  already portable; only the host layer was not, and that layer is six capabilities wide.
- **Per-platform forks of the harness** — rejected outright: it would duplicate the parity boundary,
  which is the one thing in this project that must have exactly one definition.
- **A general cross-platform GUI-automation dependency** — rejected. It would pull a large
  uninspectable surface into the most parity-sensitive code path, and the harness needs six narrow
  things rather than a framework.

**Sources**: [civ6-mcp](https://github.com/lmwilki/civ6-mcp),
[Civ VI Mac FAQ](https://support.aspyr.com/hc/en-us/articles/213487546-Civilization-VI-Mac-FAQ),
[Civ VI Linux FAQ](https://support.aspyr.com/hc/en-us/articles/360020392091-Civilization-VI-Linux-FAQ)

---

## R20 — Platform as a comparability axis

**Decision**: the seed set pins **platform together with game build**, and a run whose platform
differs from the set's fails preflight by default under exactly the FR-002 mechanism the build pin
already uses. An operator may accept a platform change for that set, recorded like any build
acceptance.

**Rationale**: this falls straight out of R18. The reason a seed set pins a build is that an
unrecorded difference between runs makes halves of the set incomparable while every other recorded
field still matches. Platform is the same hazard wearing different clothes — and arguably a worse
one, because the Aspyr macOS and Linux ports are separately built binaries whose version numbering
need not track the Windows build's. A set whose runs span Windows and macOS is not obviously uniform,
and nothing else in the record would reveal it.

**So `game_build` becomes a composite identity** — platform plus version string — rather than a bare
version. A `BuildAcceptance` covers a transition between two composite identities, so accepting
`win/1.0.12.9 → win/1.0.12.11` does not silently also accept `win → mac`.

**Cross-platform saves are not assumed to work.** Civ VI does offer cross-platform cloud saves, but
they are gated on a 2K account, documented for base-game saves, and version-matched — none of which
is a foundation for Principle IV's "two branches from the same save point begin from an identical
position." The conservative position, and the one taken:

- Branching from a run's save point is permitted **on the same platform** as the parent run.
- A cross-platform branch is refused by default with the platform mismatch named, under the same
  acceptance mechanism as a build change.
- Whether a `.Civ6Save` genuinely loads across platforms is a spike, not an assumption. If it proves
  reliable, the acceptance path is already the place to relax this.

**Why not just let it work and see**: because the failure is silent. A save that loads but resolves
differently produces a branch that diverges for reasons the record does not explain — which is
indistinguishable from the agent having played differently, and that is the exact confound the whole
seed-set apparatus exists to remove.

**Sources**: [Civ VI cross-platform cloud saves](https://www.gamedeveloper.com/game-platforms/-i-civilization-vi-i-update-brings-cross-platform-cloud-saves-to-steam-and-switch)

---

## Resolved unknowns summary

| Technical Context field | Resolution | Ref |
|---|---|---|
| Language/Version | Python 3.12+, `uv`, native Windows / macOS / Linux | R1 |
| Primary Dependencies | First-party Nexus client; pydantic, httpx, psutil, typer, fastapi, pytest — plus one per-platform host adapter set, isolated behind the `HostPlatform` port | R2, R6, R10, R16, R19 |
| Storage | `MatchStore` port + SQLite/blob reference adapter pending deliverable 3 | R11 |
| Testing | Four tiers; transcript-replay fakes; parity red-team suite in CI | R15 |
| Target Platform | Native Windows, macOS, or Linux on the same machine as the client; Proton/Wine unsupported. Capability stated as a per-platform tier | R1, R19 |
| Project Type | Single Python project (service + CLI) with per-platform host adapters | R1, R19 |
| Performance Goals | 60 s crash detection from per-operation bounds; write-before-advance; no turn budget — a no-progress step count is the only backstop; 300+ turns | R12, R14 |
| Constraints | One tuner connection; structural parity; no image dropping; no credential leakage; no turn truncation by time or cost; saves deleted only after explicit archival | R4, R9, R10, R14, R17 |

## Open items carried into implementation

These are scheduled spikes with designed failure modes, not unresolved clarifications:

1. **R5 — Lua save path enumeration.** Determines whether C1's bespoke capability is needed at all.
   Must run before save work begins, to preserve Principle II's Firetuner-first ordering. Its
   outcome now also decides Wayland's support tier (R19), since a Lua save path removes the
   synthetic-input requirement that Wayland blocks.
2. **R6 — Capture hygiene spike, once per platform.** Occlusion immunity and capture-border
   suppression. Blocks showing any image to the agent on the platform it ran on; failure falls back
   to visually degraded runs, not to unscreened images. A pass on one platform carries no
   implication for another.
3. **R10 — Provider capability metadata.** Confirms image support and context length per model at
   preflight; fails closed if unconfirmable. The context-length half is sized against a worst-case
   *decision step*, not a whole turn (R14).
4. **R18 — Game version readable from Lua.** Determines whether the build check is a declared
   `GameCore_Tuner` observation or falls back to the client executable's file version. Either way
   the check happens; the spike decides which path implements it.
5. **R19 — Per-platform host capability probe.** Confirms window identity, capture, directory
   resolution, and (if R5 requires it) synthetic input on each target platform, which is what
   assigns that platform its support tier. Until a platform is probed it is Unsupported by default
   — the safe direction.
6. **R20 — Cross-platform save loadability.** Whether a `.Civ6Save` written on one platform loads
   and resolves identically on another. Until proven, cross-platform branching is refused. This
   gates only the relaxation, not any core capability.
