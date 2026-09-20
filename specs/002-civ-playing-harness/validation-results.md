# Validation Results — Civilization-Playing Harness

**Feature**: `002-civ-playing-harness` | **Date**: 2026-09-20

This file is the Polish-phase record of validation evidence tasks.md Phase 8 calls for. It is
written incrementally by several tasks; this entry covers **T205 — the constitutional compliance
audit** required by the constitution's Governance section before implementation closes, plus the
live-client findings and open spec-amendment candidates that audit surfaced along the way. It does
not yet cover T200 (Scenarios 1–10 end to end), T201 (soak validation), or T202 (cost/duration
observations) — those require live-tier scenario runs beyond this entry's scope and should be
appended here by the tasks that own them, not folded into this section.

**Update note.** Since T205 was first written, a live Civilization VI client on a separate Linux
validation node produced a large body of spike evidence (`specs/002-civ-playing-harness/spikes/`)
and several real defects were found against it and fixed. This revision folds that evidence in:
principle statuses are revised only where live evidence actually changed them, a defects section is
added, and the open spec-amendment list is brought current. The original T205 grading is preserved
wherever live evidence did not change it.

---

## T205 — Constitutional compliance audit (Constitution v1.0.0, Principles I–VII)

**This is an audit, not a certification exercise.** Its purpose is to say, plainly, what evidence
exists and what does not — including where the answer is "not yet covered." Passing every
principle was not assumed going in, and it did not turn out that way: Principles I and IV both have
open items below that block calling this deliverable fully compliant.

### Summary

| Principle | Status | Headline |
|---|---|---|
| I — Human-Parity Information & Action Boundary | **Partially evidenced** | Structural boundary is real and tested; the `EnableDebugMenu` question is resolved with no constitutional tension; capture pixel extraction now works on Linux/X11 through the harness itself, but is still stubbed on Windows/macOS/Wayland and has never run against a real Civ VI frame |
| II — Firetuner-First, Skill-Extensible Harness | **Compliant** | All 23 capabilities are `path: firetuner`; `Network.SaveGame` is now live-verified 4/4, so the one candidate bespoke path is not merely unneeded but **forbidden** |
| III — Complete Match Telemetry | **Compliant (structurally enforced)** | `TurnPersistedToken` still makes end-turn unreachable without a durably-acknowledged write; two real defects that would have violated this principle's spirit were found by the integration tier and fixed |
| IV — Reproducible, Seeded Experimentation | **Partially evidenced** | FR-034's identical-position guarantee is now verified field-for-field across a save/load round trip; but `Network.LoadGame` itself refuses from Lua, so the *automated* load path does not exist — only the UI path does |
| V — Guidebook-Before-Optimization Gate | **Correctly gated, not satisfied** | `GUIDEBOOK.md` does not exist, which is the gate working as intended, not a defect |
| VI — Shared, Unified Observability | **Not yet satisfiable** | Deliverable 1 (the web interface) is unimplemented; this harness's own operator endpoint is deliberately closed and not a substitute |
| VII — Provider-Agnostic Model Access & Resilience | **Compliant** | OpenRouter over plain `httpx`, no vendor SDK on the decision path; crash detection was corroborated and corrected against a live false positive |

---

### Principle I — Human-Parity Information & Action Boundary (NON-NEGOTIABLE)

**Evidence.**

- **The catalog is the boundary.** `src/civsim_harness/parity/filter.py`'s `CapabilityResult` is a
  frozen dataclass requiring a `declaration_id` up front, and `filter_to_entries` immediately
  resolves it against the loaded `CapabilityRegistry` — there is no constructor path, anywhere in
  the package, that turns a bare Lua table, a raw Nexus payload string, or an unattributed dict
  into an `ObservationEntry`. This is the module's own stated purpose: "there is no other function
  in this package (and there must be none anywhere else in the harness) that builds an
  `ObservationEntry` from a bare Lua table."
- **`src/civsim_harness/parity/forbidden.py`** runs `enforce_parity_boundary` once per decision
  step (not once per turn) as a detective backstop behind the preventive controls above: structural
  key-name scanning (`scan_observation_entries`) plus literal value scanning
  (`find_literal_leaks`/`assert_no_literal_leaks`) against the fully assembled context a provider
  call would actually receive.
- **Four screening gates** in `src/civsim_harness/parity/screening.py` — source, geometry,
  provenance, content (`_check_source`, `_check_geometry`, `_check_provenance`, `_check_content`) —
  gate every capture before it can reach an agent.
- **Camera validation**: `src/civsim_harness/act/camera.py`'s `validate_camera_action` validates
  the three declared `camera.*` actions specifically, refusing to act as a general-purpose
  replacement for the declared surface.
- **The parity red-team suite** (`tests/contract/test_parity_redteam.py`, T122, research R15) — a
  fixture list of forbidden values (unrevealed map contents, opponent internal state, hidden AI
  intent, undisclosed opponent research/civics, RNG state, debug/provenance data) asserted absent
  at every decision step of realistic, catalog-driven multi-step turns, including a "contaminated"
  half that feeds capability results with extra undeclared properties a buggy Lua implementation
  could return — proving the forbidden-field guard actually catches what schema validation alone
  does not.
- **All 23 capabilities** in `catalogs/capabilities.yaml` are `path: firetuner` (see Principle II
  below) — there is currently no bespoke integration path in the codebase to audit for a
  Principle-I leak in the first place.
- **`EnableDebugMenu` is resolved, and there is no constitutional tension.** The concern going in
  was real: if the tuner depended on debug mode, every run would be non-parity by construction.
  `spikes/principle-i-debugmenu-linux.md` diffed a genuine before/after on one host across three
  surfaces: a curated 95-symbol × 2-context probe (10/10 byte-identical), the one genuinely
  enumerable namespace `Game` (53/41 members, byte-identical), and the full Lua state table (136
  states, identical indices). **State this at the scope the spike itself claims, no wider**: `_G` is
  nil and `UI`/`Network` are opaque userdata under `pairs()`, so exhaustive enumeration is
  impossible here. What is established is that *every symbol the harness uses or plans to use* is
  identical in both modes, *the one namespace that can be enumerated completely* is identical, and
  the tuner does not *depend* on debug mode — **not** that "the client exposes nothing extra
  anywhere," a wider claim the spike's own author explicitly declines to make.
  `run/preparation.py`'s `debug_menu_preflight` now reads and records the value at preflight (T204
  hardening item 1), recorded but not enforced, matching the evidence's actual strength.

**Not yet covered.**

- **Capture pixel extraction is stubbed on all three host adapters**: `host/windows/adapter.py:190`
  (PrintWindow succeeds, `GetDIBits` not implemented), `host/macos/adapter.py:173,200`
  (ScreenCaptureKit/`CGWindowListCreateImage` bridging not implemented), `host/linux/adapter.py:177,196`
  (`NameWindowPixmap`+`XGetImage`, and the `xdg-desktop-portal` ScreenCast path, both not
  implemented). Linux is furthest along: T052 confirmed `Xlib.ext.composite` does expose
  `NameWindowPixmap`, so the path is known viable with `python-xlib` — just not yet written. The
  screening gates and parity red-team suite have therefore only been proven against *synthesized*
  capture data, not a real frame through the harness's own adapter on any platform. FR-050's honest
  degradation (`visually_degraded = true`) is exactly what a stubbed pixel path exercises today —
  correct for this state, but the visual half of Principle I has no live-client evidence yet
  *through the harness*.
- **The Linux capture-hygiene spike (R6, below) is evidence about the game and window manager, not
  about the harness's own capture path.** It used a raw, harness-independent `import -window` call,
  not `host/linux/adapter.py`'s own (still stubbed) `capture_window`. Strong, concrete evidence the
  design will work once implemented — not proof the shipped adapter clears the same gates.

  > **Superseded for Linux, 2026-09-20.** `capture_window` is implemented and returns real pixels;
  > occlusion immunity has been re-demonstrated **through the harness's own adapter**
  > (`spikes/r6-xcomposite-readback-linux.md`). Two caveats keep this from closing Principle I's
  > visual half: the frames were captured from ordinary X11 windows rather than **Civilization VI
  > itself** (the client could not be launched — `spikes/steam-dependency-linux.md`), and Windows
  > and macOS pixel extraction remain stubbed.
- **The live tier has not run.** `tests/live/test_capture_hygiene.py` (T194) and
  `tests/live/test_host_platform.py` (T198) are the tests that would exercise a real captured frame
  against a real client; neither has been executed as part of this audit.

**Conclusion.** The structural boundary Principle I requires is real, tested at the unit/contract
level, and has no known gap in its *design*. The one open worry that could have made every run
non-parity by construction — dependence on `EnableDebugMenu` — is resolved, at the scope the
evidence actually supports. What is still missing is live-client proof that a real captured frame,
through the harness's own (not-yet-implemented) pixel extraction, clears the same gates the
red-team suite proves against synthetic data. That remains the single largest open item in this
audit.

---

### Principle II — Firetuner-First, Skill-Extensible Harness

**Evidence.**

- `catalogs/capabilities.yaml` currently declares 23 capabilities; all 23 are `path: firetuner`,
  and `path: bespoke` appears zero times in the file.
- **The one candidate bespoke path is now live-verified, and the evidence upgrades the verdict from
  "unneeded" to "forbidden."** T077 (research R5, `spikes/r5-save-path.md`) probed
  `Network.SaveGame` under `pcall` against a live Linux client and got **Outcome A**:
  `Network.SaveGame(gameFile)`, called from `InGame`, writes a real, valid `.Civ6Save` — four
  consecutive calls, four valid, size-stable files, **4/4, zero failures** ("a path exists but is
  unreliable" is explicitly not supported by this result). Because the evidence is now positive and
  repeatable rather than an open spike, Principle II's requirement is not merely that a bespoke save
  driver was unnecessary — it is that one is **forbidden**, since the principle mandates the
  Firetuner path once it is known to work. `saves.save_game` is declared `path: firetuner`
  accordingly, removing the only known Principle II bespoke candidate on the save side across **all
  three platforms at once** (the blocked-Wayland and macOS-Accessibility-grant risks in R5's
  original risk table were both consequences of needing *synthetic input* to save, which a Lua save
  path avoids entirely).
- This is the ordering the principle demands: a bespoke path is permitted only to fill a
  *demonstrated* gap, and "demonstrated" means a spike ran and found the gap, not that one was
  assumed and never checked.

**Not yet covered.**

- **The load half is a live, open question that belongs to this principle as much as Principle
  IV.** `spikes/load-path-linux.md` found `Network.LoadGame` refuses from Lua (see
  [Principle IV](#principle-iv--reproducible-seeded-experimentation) for the full account). If that
  gap is never closed, the harness may eventually need a documented `firetuner_gap` and a bespoke
  UI-driven load driver — the *first* legitimate bespoke path under this principle's own ordering
  rule. Not due yet: no `load_game` capability is declared and no code path invokes
  `Network.LoadGame` today, so there is nothing to audit for a premature bespoke path.
- Otherwise no known gap. Assurance is bounded by the same live-tier caveat as Principle I.

---

### Principle III — Complete Match Telemetry

**Evidence.**

- `src/civsim_harness/store/guard.py`'s `TurnPersistedToken` makes the constraint structural
  rather than conventional. It is a frozen dataclass that only `persist_turn_before_advance` can
  construct, and only after `store.write_turn_cycle(record)` has returned normally (the port's own
  contract: either the write returns durable, or it raises — there is no third outcome, and this
  module does not catch that exception). `advance_turn`'s signature *requires* a
  `TurnPersistedToken` to invoke an end-turn callable at all — there is no overload accepting a
  bare run/turn/attempt triple. A caller with a failed or skipped write has no token, and therefore
  no way to call end-turn through this module.
- This directly implements invariant I3 ("no turn ends before its complete record is durably
  persisted") as a type-level guarantee rather than a rule a reviewer has to trace by hand.
- **Three real defects that would have violated this principle in practice were found and fixed**
  — full detail in [Defects found and fixed](#defects-found-and-fixed) items 3–5, cited here only by
  headline: (1, `7885745`) a run stranding in `playing` forever via an illegal transition whose
  exception was silently swallowed, indistinguishable from a healthy run; (2, `7885745`) `turn_gaps`
  failing to detect a **trailing** attempted-but-unrecorded turn, so a run that died mid-persist
  reported itself `complete` — a direct violation of this principle's "no gaps" guarantee, now fixed
  in the published port contract and its conformance suite; (3, `8204d58`) the no-progress backstop
  recording `ended_on_no_progress` without ever issuing the matching end-turn in the live game,
  fixed by dispatching it strictly *after* the persist step so invariant I3's ordering holds.

**Not yet covered.** Nothing at the design level. As with Principle I, full confidence that this
holds under a real multi-hundred-step turn against a real store still depends on the live tier
(T191–T194, T198, T200–T202), none of which are in this audit's scope.

---

### Principle IV — Reproducible, Seeded Experimentation

**Evidence.**

- Seeded configuration (`RunConfiguration`/`SeedSet`), per-turn quicksave (enforced alongside the
  turn-persistence guard above), branch lineage recording (`parent_run_id`/`parent_turn`), and
  `archive_run` as the *only* path to save-retention eligibility (`saves reap --dry-run` reports 0
  eligible saves for a finished-but-unarchived run, however old — research R17) are all present in
  the codebase per `data-model.md` and the save/branch modules under `src/civsim_harness/saves/`
  and `src/civsim_harness/run/`.
- **FR-034's identical-position guarantee is now verified — live, field-for-field.**
  `spikes/load-path-linux.md`, the direct follow-up to R5: a turn-1 position was fingerprinted
  (turn, local player, yields, exact unit type/position/HP/moves, city count/position/population),
  the game advanced to turn 19, the turn-1 `Network.SaveGame`-written save loaded back through the
  client's UI, and the position re-fingerprinted. Every compared field matched exactly, **18 turns
  downstream** — live measurement, not inference, and the strongest single-FR evidence in this
  document.
- **`Network.LoadGame` itself refuses from Lua — the automated load path does not exist.** Six
  different `gameFile` table shapes were tried, including the exact table `Network.SaveGame`
  accepts: every one returned `ok=true, ret=false` — well-formed, declined without error. The
  working hypothesis is that the client needs a file record produced by `UI.QuerySaveGameList`'s
  async result. That query's completion events were *located* (`LuaEvents.FileListQueryComplete`,
  but only on the `LoadGameMenu`/`SaveGameMenu` states, not `InGame`), yet registering a handler and
  issuing the query produced **zero deliveries** in two attempts. **Status: unresolved**, not merely
  unverified — actively attempted twice, did not succeed. One usable lead survived: Lua globals
  persist across tuner commands, so the async register-then-poll pattern is implementable once the
  right event/parameter combination is found.
- **The identical-position result above came from the UI path, which is not automatable today.**
  `Escape` → Menu → Load Game → select → Load Game button → a `CONFIRM LOAD FILE` modal → Yes → an
  intro screen whose continue button reads `Continue Game` after a load, not `Begin Game`. Real,
  parity-valid evidence, but not something Firetuner alone can currently drive unattended.
- **Save files are not byte-stable, and the codebase already does not rely on them being so.** Save
  → load → save from the identical position above produced files differing in **636,132 of ~683,000
  bytes** — compression/serialization non-determinism, not a content difference. A direct audit of
  `src/civsim_harness/saves/` and `src/civsim_harness/run/` confirms no module compares `.Civ6Save`
  bytes anywhere: `saves/verify.py`'s stability check compares only file **size** across two reads
  (confirming the async write finished, not asserting content identity), and
  `saves/branching.py`'s parent-immutability guarantee is enforced structurally — never writing to a
  store row keyed by the parent's `run_id` — never by comparing save bytes. Already the right
  design, not a gap the spike found.
- **Cross-platform save identity (R20/T199) has not run.** No `r20-cross-platform-saves.md` exists.
  This spike gates only the *relaxation* of the cross-platform branching refusal — branching stays
  correctly refused until it exists.

**Not yet covered.**

- The automated load path (above) — the concrete remaining gap, now much better characterized than
  "unverified": it was attempted and did not succeed, with a specific located-but-unproven lead.
- Cross-platform save identity, as above.

**Conclusion.** The reproducibility *machinery* is built, and the guarantee that matters most —
FR-034's identical-position claim — now has direct live verification instead of resting on an
unspiked primitive. What remains is narrower but sharper: not "is FR-034 satisfiable" (yes,
verified) but "can the load half be driven automatically through Firetuner" (not yet — six
`Network.LoadGame` attempts failed, and the `UI.QuerySaveGameList` async lead is real but
unfinished). Until it closes, branching/resume-from work needing an automated load either waits, or
takes on a documented `firetuner_gap` for a bespoke UI-driven load driver — the first legitimate
bespoke path this project would have, per Principle II's own ordering rule.

---

### Principle V — Guidebook-Before-Optimization Gate

**Evidence.** `GUIDEBOOK.md` does not exist at the repository root (confirmed: `ls GUIDEBOOK.md`
returns "No such file or directory"). No Monte Carlo search, ablation, or automated optimization
work has been built against the 100-science/100-culture-by-turn-50 goal, and deliverable 4
(the sandboxed optimization layer this principle gates) has not been specified or planned.

**Conclusion.** This is the gate correctly holding, not a gap to remediate. The constitution's own
Sync Impact Report flagged `GUIDEBOOK.md`'s absence as a known deferred item at ratification time;
this audit confirms that state is unchanged and that nothing in this deliverable has attempted to
route around it. **Say so plainly: this principle is not satisfied, by design, until `GUIDEBOOK.md`
exists — and deliverable 2 correctly does not depend on it being satisfied.**

---

### Principle VI — Shared, Unified Observability

**Evidence.** None in this deliverable's favor yet, and that is the honest reading.

- **Deliverable 1 (the unified web interface) is planned but unimplemented.**
  `specs/001-unified-web-interface/` contains a full spec, plan, data model, research, and tasks
  file, but no corresponding implementation exists under `src/` — there is no web/UI package in
  this repository at all.
- **This harness's own operator endpoint is deliberately closed, and is explicitly not the shared
  view the principle requires.** `src/civsim_harness/operator/api.py` binds only `127.0.0.1`
  (`LOOPBACK_HOST`, `run_server`'s own docstring: "the server only ever binds `127.0.0.1`... and
  nowhere else") and exposes exactly five endpoints (`POST /runs`, `/runs/{id}/pause`,
  `/runs/{id}/resume`, `/runs/{id}/stop`, `GET /runs/{id}/status`, `GET /health`) returning only
  `RunStatusView` — lifecycle and diagnostic status, never turn records, decisions, metrics, or
  captures (`cli.py`'s own comment: "The CLI never presents turn records, decisions, metrics, or
  captures (FR-053, Principle VI)"). This is deliberate scoping for deliverable 2, not an attempt
  to satisfy Principle VI on its own.

**Conclusion.** Principle VI requires the web interface and the directing Claude Code session to
see the same live and historical view, with the user never needing to interface with the game
client directly. None of that exists yet. This deliverable's operator surface is intentionally
narrow (lifecycle control and audits only) and was never meant to discharge this principle —
deliverable 1 is what would. **State plainly: Principle VI is not satisfied by anything built so
far, and nothing in deliverable 2 claims otherwise.**

---

### Principle VII — Provider-Agnostic Model Access & Resilience

**Evidence.**

- **No vendor SDK on the decision path.** `src/civsim_harness/provider/openrouter.py`'s own module
  docstring states it directly: "the only third-party import here is `httpx` (plain HTTP) — never
  `openai`, `anthropic`, or any other vendor client." Confirmed by import inspection: `httpx` is
  the sole non-stdlib import; model calls are plain HTTP against OpenRouter's chat-completions
  endpoint.
- **Crash detection and recovery are implemented**, not stubbed: `src/civsim_harness/resilience/`
  contains `detector.py`, `heartbeat_monitor.py`, `liveness.py`, `operation_bounds.py`, and
  `recovery.py`. `recovery.py`'s `RecoveryEngine.recover` resumes a killed client from the
  interrupted turn's quicksave as the same continuous run (not a new run), re-observes and
  re-captures before acting on anything, records both the abandoned attempt and the authoritative
  replay, and stops in a recorded `failed` state after `recovery_attempt_limit` consecutive
  failures rather than retrying indefinitely (FR-048, SC-021). A save already known absent
  (`missing`/`retention_status == REMOVED`) is refused immediately rather than retried, and a save
  discovered absent only at load time is durably recorded via `saves.addressing.report_save_missing`
  before the run fails — never silently retargeted to a different turn's save.
- **A live probe corroborated the detector and found a real false-positive bug, since fixed
  (`1f44f93`).** A live tuner probe hit `ConnectionRefusedError` against a healthy client: Civ VI
  accepts one tuner connection at a time and refuses a rapid reconnect while the prior socket is
  still releasing. As written, detection would have manufactured `crash_detected` on a healthy game
  and triggered recovery, abandoning good work while looking like a genuine crash in the record.
  `check_heartbeat` now re-probes once after a bounded delay before falling through to process
  liveness (PID dead → `crash_detected`, PID alive → `unresponsive_detected`); worst case consumes
  22s of SC-010's 60s budget. Both directions are covered by separate tests, so the fix could not
  merely have suppressed the symptom.

**Not yet covered.** Nothing at the design level for this principle specifically. The live-tier
crash-recovery test (`tests/live/test_crash_recovery.py`, T193 — "a genuinely killed client is
detected within 60 s") has not been run as part of this audit; the finding above corroborates the
*detection* mechanism's behavior against a real client but is not that full test.

---

## Live-client findings from the Linux validation node

Recorded here because they came from an actual live client rather than a spike's reasoning alone,
and because several materially changed a design assumption or a piece of code elsewhere in this
project. Findings that are load-bearing for a specific principle are covered in full above and only
cross-referenced here to avoid duplication.

### Capture hygiene: window-scoped only, and it depends on a compositor

`spikes/r6-capture-hygiene-linux.md` (T099, Linux/X11, `Mutter (Muffin)` compositing WM):

- **Window-scoped capture passes occlusion; a root-scoped grab of the same region leaks harness
  console text.** An `xmessage` dialog over the client is fully absent from a window-scoped
  `XGetImage` capture (`r6-evidence/window-scoped-occluded-region.png`). The identical coordinates
  captured **root-scoped** show the dialog and, beneath it, **legible harness command text from a
  terminal** (`r6-evidence/root-scoped-same-region-LEAKS.png`). **Rule adopted: the capture path
  must be window-scoped; a root/screen capture must never be used, even as a fallback.** A degraded
  FR-050 run is a recorded, honest state; a screen grab is a parity breach, demonstrated concretely.
- **Occlusion-immunity is a property of the compositing WM, not of X11 in general.** Under a
  compositor with the `Composite` extension present, windows redirect to offscreen pixmaps, so a
  window-scoped `XGetImage` reads the client's own backing content. **Without a compositor, the
  same call would return the occluding window's pixels — a silent parity failure that looks like a
  pass.** Not yet implemented in `host/linux`: verify compositing at preflight rather than infer it
  from `XDG_SESSION_TYPE=x11`. **"X11 without a compositor" must not resolve to `VALIDATED`.**
- **A minimized client returns valid-looking current content — but whether it keeps *rendering new
  frames* while minimized is untested and dangerous if wrong.** A stopped-rendering client would
  return the last frame indefinitely, the exact stale-board failure Scenario 8a exists to prevent,
  arriving through the capture path instead of observation reuse. Kept open below.
- **One in-frame contaminant survives window scoping by construction**: Steam's FPS overlay
  composites *inside* the client's frame, so no window-scoping choice can exclude it. Generalizes:
  a third party can composite content into the client's own frame without the client knowing
  (Discord, MangoHud, driver overlays land the same way) — the screening profile should treat
  "foreign content inside the game frame" as a detection class, not a one-overlay special case.
- **Wayland is not validated by this spike** (host was X11 throughout). `find_game_window` on
  Wayland, the `xdg-desktop-portal` ScreenCast path, and reporting synthetic input as unavailable
  rather than attempting it are all unexecuted code. A Linux pass is not Linux coverage in general.

### XComposite pixmap readback: implemented, and the two traps in it

`spikes/r6-xcomposite-readback-linux.md` (T052, Linux/X11, `Composite` 0.4, python-xlib 0.33).
`capture_window` is no longer a stub — it returns real `BGRA8` pixels, ~79 ms for 1920×1200.

- **A running compositor is not sufficient; the harness must redirect the window itself.**
  `NameWindowPixmap` fails with **`BadMatch`** unless the calling client has issued
  `CompositeRedirectWindow` for that window. Muffin redirects root's *subwindows*, which does not
  satisfy it. Measured both ways: naming without redirecting is `BadMatch`, the identical call after
  `redirect_window` succeeds. `RedirectAutomatic` leaves the client's own display untouched.
- **python-xlib reports X errors asynchronously, and this produced a confident false pass.** Both
  requests return an object even when the server rejects them; the `BadMatch` above surfaced only as
  an out-of-band print, then resurfaced later as a misleading `BadDrawable`. **Every request now
  carries an explicit `CatchError` + `sync()`.** Without it, `capture_window` reports success while
  holding a pixmap the server never created — the worst failure available to this function.
- **Channel order was verified with known colours, not inferred.** `#ff0000` reads back
  `00 00 ff ff` and `#3366cc` reads back `cc 66 33 ff` — B,G,R,pad, emitted as `BGRA8`, which the
  screening gates already decode. A silent R/B swap would pass every size assertion while corrupting
  every image the agent sees; MSBFirst servers are **refused rather than guessed at**.
- **Occlusion immunity re-proven through the harness's own adapter**, not `import`: with an occluder
  raised over 420×260 px of the target, the harness-captured target frame is complete and contains
  no trace of it (`r6-evidence/harness-xcomposite-target.png`). No root grab appears anywhere in the
  evidence script, including to illustrate the overlap.
- **Preflight implemented** as `capture_preconditions()`: compositing via `_NET_WM_CM_Sn` selection
  ownership (never `XDG_SESSION_TYPE`), plus `unredirect-fullscreen-windows`, which warns on `true`
  *and* on unknown. It is additive to the port — **a port-level preflight hook is the right
  long-term home, flagged for the owning side rather than changed here.**
- **Still outstanding: a real Civilization VI frame through this path.** Verified against ordinary
  X11 windows only, because the client could not be launched (see below). Fullscreen capture, GPU
  overlays, and redirect churn on a live game window are all unmeasured.

### Steam is a hard dependency of the live node

`spikes/steam-dependency-linux.md`. Civ VI **cannot** run without a running, signed-in Steam client:
launched directly it fails `SteamAPI_Init` and puts up a DRM dialog that exits. Since an account can
be in a game on only one machine at a time, **the live node is unavailable whenever the owner is
gaming on that account** — live tasks must be scheduled around it, and an unattended run can be
pre-empted. Steam offline mode does not help from a signed-out state (logout clears the credential
cache), nor does Family Sharing (borrowing is blocked while the lender plays).

### The Lua sandbox: what exists, what doesn't, and what changes by phase

`spikes/lua-api-verification-linux.md`, `spikes/load-path-linux.md`, `spikes/r5-save-path.md`:

- **`_G` is nil; `UI` and `Network` are opaque userdata yielding zero entries under `pairs()`.**
  Only `Game` is genuinely iterable, which is why every sweep here used direct name probing under
  `pcall` rather than enumeration — there is no way to ask the client for its complete symbol list.
- **`Game.EndTurn`, `Cities.GetCity`, and `Units.GetUnit` do not exist** — all resolve `nil` in both
  tuner contexts. The real end-turn action is `UI.RequestAction(ActionTypes.ACTION_ENDTURN)`,
  live-proven turn 1 → 2, guarded by `UI.CanEndTurn()`. Its return value is `nil` and carries zero
  information, so verification must always compare `Game.GetCurrentGameTurn()` before/after rather
  than trust the dispatch. `Cities.GetCity`/`Units.GetUnit` are reachable instead through `Players`.
- **`ActionTypes.ACTION_ENDTURN` is a hash (`751412917`), not a stable enum** — and this generalizes
  across `GameConfiguration`, which mostly returns Civ VI type hashes, not readable strings.
  `DB.MakeHash(name)` hashes a known name for assertions (no table scan, localization-independent);
  the matching `GameInfo` table can be reverse-scanned by `row.Hash == value` for a display name.
  Store the hash as authoritative; resolve a display name only for the human-readable record.
- **Lua state indices differ by game *phase*, not only by version or connection** (`3e9bd15`): **31
  states at Create Game vs. 136 in game, `LoadGameMenu` at index 18 vs. 112** across that same
  transition. Caching indices once at connect would silently execute Lua against whatever now
  occupies that index after a phase change — same silent-corruption class as the `_parse_state_list`
  defect below. Fixed with `refresh_state_indices()` (re-adopted at known phase boundaries) plus a
  membership check in `execute_command()` rejecting a stale index before it reaches the wire. The
  follow-up risk flagged in the same commit — `saves/save_game.py` caching a raw index at
  construction — is resolved in the current tree: it now re-resolves on every call.
- **The sandbox is narrower than stock Lua.** No JSON library (the hand-rolled encoder in `lua/` is
  correct and must stay), no `require` (every Lua body self-contained), no `io`, no `debug` (arity
  only discoverable by calling). `os` and `loadstring` are present.
- **Screen identity is reachable, but not via `UIManager`** (`GetScreen`/`GetTopmostScreen`/
  `GetCurrentPopup` all `nil`). Every UI screen is its own Lua state; `ContextPtr:IsHidden()`
  reports whether it's showing — proven by one `Escape` flipping exactly one of 26 watched states.
  Answers "is screen X open," not "what is topmost" (z-order not exposed); one round-trip per
  screen checked.
- **`mod_set` is 22 mods, not empty** — a placeholder value fails the build-identity match (V3).
  `GetEnableModsMetaString()`'s JSON of `modid`/`version`/`title` is the better pinning basis, since
  display titles are localized and can carry color markup.

### Turn timer: a run-blocker traced to `Play Now`, not the mod set

`spikes/turn-timer-blocker-linux.md`, resolved same session: a single-player "Play Now" game ran
with `GameConfiguration.GetTurnTimerType() == TURNTIMER_STANDARD` — turns advanced roughly every
25s with **zero input**, invalidating FR-014 (turn expires mid-thought), FR-011 (a timer-advanced
turn is indistinguishable from an agent-ended one), and FR-015/SC-022 (the no-progress backstop's
`ended_by_agent`/`ended_on_no_progress` distinction breaks the same way) — silently: the run would
keep going, producing a plausible-looking dataset whose decisions and turn advances are not
causally related. An earlier report blaming `AutoEndTurn` was retracted after direct measurement
(`UserOptions.txt` correctly had it at `0`).

**Resolution: the timer is a property of `Play Now`, not the host or the mod set.**
`GameConfiguration` read back `TURNTIMER_STANDARD` under `Play Now` and `TURNTIMER_NONE` under both
Create Game's defaults and the `CivSim DEFAULT` preset. The control is Advanced Setup →
`Smart-Timer` (BBG/Multiplayer-Helper), `CivLan / CWC 2025` under `Play Now` and `Off` in the
preset. **Consequence: preparation must never use `Play Now`** — already the direction for a
separate reason (it randomizes the leader; see below).

The preflight check this demanded is implemented, not just recommended: `run/preparation.py`'s
`turn_timer_preflight` refuses to start unless the timer resolves to `TURNTIMER_NONE` or
`NO_TURNTIMER` (both accepted; hashes computed via `DB.MakeHash` at runtime, never hard-coded). An
undeterminable reading is recorded `UNVERIFIED` and never defaults to safe.

Collateral finding: a probe opening a fresh tuner connection per reading can hit
`ConnectionRefusedError` while the client is fine (see Principle VII) — the client accepts one
tuner connection at a time and can refuse a rapid reconnect while the prior socket releases.

### The `CivSim DEFAULT` preset: location, parameters, and what it does not pin

`spikes/civsim-default-preset-linux.md`, `spikes/preset_readback.txt`:

- **Location**: `Saves/Single/CivSim DEFAULT.Civ6Cfg` — **setup configurations live in the same
  directory as save files**, using the same `CIV6` container as `.Civ6Save`. This gives an
  independent cross-check for the seed set's platform+build pin, read from the file rather than the
  client — the same property `.Civ6Save`'s "Saved By Version"/"Tuner Active" fields provide for
  saves. Sibling files (`Saves/Single/auto/AutoConfigGame_01.Civ6Cfg`) confirm the pattern is the
  game's own. **Checked against the current reaper (`saves/reaper.py`): not at risk.** It never
  globs the save directory — its only input is `MatchStore.list_eligible_save_points()`, deleting
  exactly the files named by store-recorded `SavePoint` paths. The spike's caution about a
  hypothetical globbing reaper is recorded below as something to keep true, not something broken.
- **Parameters, read back authoritatively through `GameConfiguration` after loading the preset**
  (observed values, not file inference): `RULESET_EXPANSION_2`, turn timer `TURNTIMER_NONE`,
  difficulty **`DIFFICULTY_EMPEROR`**, game speed **`GAMESPEED_ONLINE`**, `ERA_ANCIENT`,
  `Pangaea.lua` (Small), unlimited turns, 6 AI players. **This directly contradicts the worked
  example in `contracts/run-configuration.md`**, which shows `GAMESPEED_STANDARD` /
  `DIFFICULTY_PRINCE` — the preset this project built uses the fastest game speed and a harder
  difficulty than the contract's own example assumes.
  ✅ **RESOLVED by the owner: `GAMESPEED_ONLINE` is deliberate.** The preset is authoritative and the
  contract's worked example is the thing that is out of date. **Consequence to carry forward:** the
  constitution's "100 science / 100 culture by turn 50" goal was not calibrated at this speed —
  turns cover far fewer game-years at Online — so that threshold means something materially
  different here and must not be compared against any standard-speed baseline without restating it.
- **The fixed leader is NOT set in the preset**, despite the intent behind building it — every
  player slot reads back `civ=nil leader=nil human=false`, and the UI shows `Random Leader` in
  every slot after loading it. (`LEADER_CYRUS`/`CIVILIZATION_PERSIA` strings visible in a raw
  `strings` dump are the installation's available-options roster, not a selection — an earlier read
  mistook them for one and was corrected in the same document.)
- ✅ **RESOLVED — the preset does not need to pin it.** The owner confirmed the saved configuration
  will not hold a civ selection and **ruled that runs start with `LEADER_CYRUS` (Persia)**.
  `PlayerConfigurations` exposes working setters at the `HostGame` state, so preparation pins it
  itself after loading the configuration:
  `pc:SetLeaderTypeName("LEADER_CYRUS")` / `pc:SetCivilizationTypeName("CIVILIZATION_PERSIA")`.
  Verified live with read-back (`slot0` went `nil`/`nil` → `LEADER_CYRUS`/`CIVILIZATION_PERSIA`) and
  the Create Game UI refreshed to show Cyrus with the Persia icon. **The UI does not repaint
  immediately on the Lua write**, so it must not be used as the verification signal — read back
  through `PlayerConfigurations`, which is the FR-002/V2 pattern regardless. Related setters
  confirmed present: `GameConfiguration.RemovePlayer` (returns `true`),
  `SetParticipatingPlayerCount`, `GetAIPlayerIDs`; **`SetAIPlayerCount` does not exist** — player
  count changes by adding/removing players.
  The full preparation path is therefore reachable without UI automation beyond loading the
  configuration: load preset → set leader/civ in Lua → read back field by field → start.
- `Play Now` is confirmed unusable for seeded work independent of the timer finding: it randomized
  the leader across three consecutive launches (England/Eleanor, Korea/Seondeok, Mali/Mansa Musa).

---

## Defects found and fixed

Real defects, found by live-client evidence or by the integration test tier, fixed before this
record was written. Grouped by how they were found because the pattern itself is the lesson: **every
defect below is either a wrong constant/format guessed without a live client, or a code path the
unit tier cannot exercise (multi-module ordering, a swallowed exception, a directory glob).**

1. **`_parse_state_list` implemented an unverified wire-format guess** (`506b450`). Assumed format:
   "newline-separated state names in positional order." Real format: **NUL-separated alternating
   `<index>\0<name>\0` pairs** — not one newline in the payload. The dangerous part: `connect()`
   would have **succeeded** while resolving the wrong state indices, leaving every downstream
   observation and action subtly wrong with no obvious symptom. Fixed to read indices from the
   payload's own fields rather than list position; the captured bytes are now a byte-exact
   regression fixture labelled real-client-captured. Coupled second defect, same commit:
   `connect()` required both `GameCore_Tuner` and `InGame` to exist, failing outright at the main
   menu (only `Main State`/`DebugHotloadCache` exist there) — split so `connect()` succeeds with
   whatever states exist, and a separate `resolve_game_states()` requires both once a game loads.

2. **A `.gitignore` rule silently excluded the entire `saves/` package from every commit**
   (`cc4d195`). The unanchored pattern `saves/` matched both the intended per-machine save-file
   directory *and* `src/civsim_harness/saves/`, including `__init__.py` since Wave 1 — every clone
   had a broken package. Anchored to `/saves/` (and the same latent defect in `/captures/`/
   `/blobs/`); found by an agent reporting a problem in a file it did not own rather than working
   around it. **A second instance of the same bug class hit this very file**: the initial commit's
   `.gitignore` also excluded `specs/*/validation-results.md` as "validation output," fixed
   separately in `751b68a` — an audit nobody can read from a clone is not a record.

3. **A run could strand in `playing` forever** (`7885745`, defect 1). `runner.py`'s failure handler
   transitioned `playing → failed` directly, a transition `data-model.md` §4 permits only from
   `preparing`/`resuming`. `transition()` correctly raised — but the exception was swallowed inside
   a fire-and-forget coroutine nothing awaits, so the run stayed in `playing` with nothing recorded:
   unrecoverable, and indistinguishable from a healthy run from the outside. Fixed by routing
   failures to the state the legal graph actually permits (`paused`, per FR-042/FR-049) rather than
   widening `LEGAL_TRANSITIONS` to make the illegal edge legal, which would have buried the bug.

4. **A run with a trailing unrecorded turn reported itself `complete`** (`7885745`, defect 2) — a
   direct Principle III violation (a wrong `complete` corrupts every downstream trending or
   optimization conclusion). `turn_gaps` derived the highest recorded turn from `MAX(turn_number)`
   over persisted `TurnCycle` rows, so a turn whose quicksave was taken but whose record never
   persisted was invisible — no *later* turn to make it look like a hole. Fixed in `turn_gaps`
   itself (the published port contract) by extending "highest recorded turn" to include the highest
   turn with a save point, but **only** on a run that has stopped actively cycling — a still-
   `playing` run's quicksave-precedes-its-`TurnCycle` shape is normal (FR-007) and left unreported.
   Found by running the integration tier, not by reasoning; contract and conformance suite updated.

5. **The no-progress backstop recorded a turn ended without ending it in the game** (`8204d58`). See
   [Principle III](#principle-iii--complete-match-telemetry) — the backstop's bookkeeping said the
   turn was over while the live client sat on the same turn number forever, which would have
   presented as a hang rather than a logic error.

6. **The Linux adapter used the Windows process name** (`d8bbdf0`, generalized in `7cde231`).
   `_PROCESS_NAMES` was `("CivilizationVI",)` — correct on Windows, matching nothing on the native
   Aspyr Linux build (`Civ6`). Process detection found no client at all on Linux, and every
   capability depending on it was dead. Generalized: macOS carries the identical single-guess bug
   (unconfirmed, no macOS machine exists), broadened to include `Civ6`; Windows broadened with bare
   (no `.exe`) candidates as defense in depth. New contract tests prove each adapter's candidate set
   actually matches through `locate_game_process()`. **Not resolvable from the repo alone**: the
   R1/R5 macOS save-directory contradiction remains open, needing a live macOS machine.

7. **Window coordinates were frame-relative on two platforms, both plausible and both wrong.**
   - **Linux** (`d8bbdf0`): `find_game_window()` built its rect from `get_geometry()`, which under a
     reparenting WM reports coordinates relative to the WM frame — `(0, 0)` for a managed window,
     not its screen position. `left=0` for a window actually at `x=2560` on a two-monitor desktop;
     synthetic mouse input computed from it would aim at the wrong monitor. Fixed via
     `root.translate_coords(candidate, 0, 0)` (the *destination*-window convention — the reverse
     order looks equally plausible and is wrong), verified byte-exact against `xwininfo`.
   - **Windows** (`0a53884`): `find_game_window` built `WindowRect` from `GetClientRect`, which by
     Win32 documentation always returns `(0, 0)` for its origin. Identical bug shape to the Linux
     fix, and survives review for the same reason: `left=0` is *plausible* — correct on a single
     monitor, wrong only off the primary display. Fixed with `GetClientRect` for size and
     `ClientToScreen` on the client-area origin for position (deliberately not `GetWindowRect`,
     which includes the frame/title bar). **Corrected by reasoning, Win32 documentation, and the
     Linux precedent, not verified against a real client** — no Windows machine with Civ VI
     installed exists here; the `UNVERIFIED` marker is kept and made accurate rather than upgraded.

---

## Open spec-amendment candidates

Live-client spike work surfaced items not yet reflected as formal spec changes. Recorded here so
they are not lost between this audit and whichever future session runs `/speckit-clarify` or amends
`spec.md` directly. One item from the prior version of this list is now resolved and moved out.

**Resolved since the last revision of this list:**

- ~~`EnableDebugMenu` should be recorded at preflight.~~ **Implemented.**
  `run/preparation.py`'s `debug_menu_preflight` (T204 hardening item 1) reads `AppOptions.txt` and
  records the value at preflight, exactly as the debug-menu spike recommended. It is recorded, not
  enforced — matching the strength of the evidence, which found no dependence on the setting, not
  proof of none existing anywhere.

**Still open:**

1. **`GAMESPEED_ONLINE` in the `CivSim DEFAULT` preset needs an owner decision, and the spec's own
   example disagrees with the preset actually built.** `GAMESPEED_ONLINE` is the fastest game speed
   in the game; the constitution's "100 science / 100 culture by turn 50" target means something
   materially different there than at `GAMESPEED_STANDARD`. The built preset reads back
   `GAMESPEED_ONLINE`/`DIFFICULTY_EMPEROR`, while `contracts/run-configuration.md`'s own worked
   example shows `GAMESPEED_STANDARD`/`DIFFICULTY_PRINCE` — the artifacts on disk contradict each
   other, and no owner decision has resolved which is authoritative.
2. **The preset does not pin a leader, which blocks validation V3.** Every player slot reads back
   `civ=nil leader=nil human=false`; the UI shows `Random Leader` after loading it. V3 requires
   `civilization`/`leader` to match the seed set. Either the preset needs the leader pinned and
   re-saved, or preparation must set it explicitly after loading — neither has happened.
3. **Graphics settings are an input to the agent but are pinned nowhere.** `SeedSet` pins
   `civilization`, `leader`, `ruleset`, `mod_set`, `game_build` — not graphics quality (confirmed
   absent from `models/common.py`). `spikes/launch-tuning-linux.md` records this host's full
   `GraphicsOptions.txt` (lowest-quality throughout) precisely so early runs stay reproducible even
   though the harness does not yet pin them — a comparability gap across hosts remains.
4. **A minimized client may return stale frames — unverified, and the code has no guard for it.**
   Capture returns valid-looking, current content when minimized, but whether Civ VI keeps
   *rendering new frames* while minimized was not tested. Confirmed: no `minimiz`-anything exists
   anywhere in `src/civsim_harness`. Unaddressed: keep the client unminimized during runs.

---

## What is still NOT evidenced

Stated plainly, gathered in one place rather than left scattered across the principle sections
above:

- **No live test has run against a real Civilization VI client.** Everything above from spike work is
  real live-client evidence gathered by hand-written probe scripts deliberately independent of
  `civsim_harness` (per `spikes/r5-save-path.md`: "these results are evidence about the game rather
  than about our implementation"), not the harness's own live test suite against a client.

  Partial exception, 2026-09-20: `tests/live/test_linux_xcomposite_capture.py` (5 tests) **has** run
  green and does exercise the harness's own `capture_window`. It targets ordinary X11 windows rather
  than Civ VI, so it proves the pixel pipeline, not the client integration.
- **Capture pixel extraction is implemented on Linux/X11 only; Windows and macOS remain stubs.**
  The Linux `XComposite`/`NameWindowPixmap` path now returns real, correctly-ordered `BGRA8` pixels
  and is occlusion-immune through the harness's own code
  (`spikes/r6-xcomposite-readback-linux.md`). **No platform has yet proven a real frame *of
  Civilization VI* clears the parity screening gates through the harness's own code** — on Linux
  because the client could not be launched (`spikes/steam-dependency-linux.md`), elsewhere because
  the extraction is unwritten. Linux/Wayland also remains stubbed.
- **The Linux live node depends on Steam and is not available on demand.** Civ VI refuses to launch
  without a running, signed-in Steam client, and an account can be in a game on only one machine at
  a time — so live work is pre-empted whenever the owner is playing. Scheduling constraint, not a
  defect; see `spikes/steam-dependency-linux.md`.
- **macOS has no machine anywhere that can execute it.** Every macOS-specific claim in this document
  and the codebase (`host/macos/adapter.py`) is reasoning by analogy to Linux, not independent
  verification. The **R1/R5 save-directory contradiction for macOS is unresolvable without a live
  macOS host** — not a research gap that more reading closes.
- **Wayland is entirely untested.** The Linux capture-hygiene result is X11-only, explicitly scoped
  that way by its own spike. `find_game_window`'s Wayland branch, the `xdg-desktop-portal`
  ScreenCast path, and reporting synthetic input as unavailable are all unexecuted code.
- **The automated `Network.LoadGame` path does not exist** (Principle IV) — attempted twice, not
  merely unverified.
- **Cross-platform save identity (R20/T199)** has no spike on disk.

---

## Verification

```
uv run pytest tests/unit tests/contract tests/integration tests/fakes -q
```

**933 passed, 3 skipped, 0 failed** — the full CI-runnable set (unit, contract, integration, and the
fakes' own smoke tests). `ruff check .` and `mypy --strict src/civsim_harness` are both clean across
103 source files.

This is higher than the count cited in any individual commit message above, because those numbers
are snapshots from earlier points in the same history — cite this file's own number as current, not
a commit message's.

The **3 skips are the Windows, macOS, and Linux host adapters** whose platform extras are not
installed on the machine this was run from. That is the expected and correct result here, and it is
worth stating plainly rather than reading as incidental: **every host-adapter assertion on this
machine is skipped, not passed.** On the Linux validation node, where the extras are installed, the
same conformance suite executes its Linux cases for real — which is the asymmetry the `HostPlatform`
port exists to make visible.

`tests/live` is excluded by default and has never run. See "What is still NOT evidenced".
