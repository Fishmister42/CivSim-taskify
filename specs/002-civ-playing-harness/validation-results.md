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
| II — Firetuner-First, Skill-Extensible Harness | **Compliant, after a correction (2026-09-22)** | `Network.SaveGame` is live-verified 4/4, so the planned *save-dialog* bespoke path is not merely unneeded but **forbidden** — but a bespoke path did ship for prompts (a real synthetic click at a control's rectangle) and was declared `path: firetuner` with a null gap until 2026-09-22. `prompts.orders` is now `path: bespoke` carrying its measured gap; 26 of 27 capabilities are `path: firetuner` |
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
  **Qualified 2026-09-22 (T299), and this qualification must travel with any citation of the
  content gate.** The content gate's declared-text technique matches a reject category only when
  **every** `_`-split token of the category id appears in the desktop evidence. Three shipped
  categories contain a word no window title ever supplies — **`firetuner_window`** needs "window",
  `harness_owned_ui` needs "owned"/"ui", `linux_panel` needs "linux" — so they are *addressable in
  the coverage map and unmatchable in practice*. **`firetuner_window` MUST NOT be cited as screened
  — not here, not in a scorecard, not in a release note.** The honest statement is: *the category is
  undetectable by the current technique set; the protection on Linux is **structural**, not
  gate-derived.* That structural protection is real and is why delivery stays open on Linux — the
  X11 `XComposite`/`NameWindowPixmap` path reads the game window's **own off-screen pixmap** with no
  screen-grab fallback, so another application's window cannot be in the frame, and the retro-audit
  examined all 188 distinct delivered frames and found nothing. **It does not exist on Windows or
  macOS**, whose capture is not window-scoped and whose coverage guard nevertheless passes — so
  T299 is a **release blocker for both**.
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
- **26 of the 27 capabilities** in `catalogs/capabilities.yaml` are `path: firetuner` (see
  Principle II below). *(Corrected 2026-09-22: this read "all 23 … are `path: firetuner` — there is
  currently no bespoke integration path in the codebase to audit".)* The one bespoke path,
  `prompts.orders`, writes only: it clicks a button the prompt itself is already offering, and the
  options it may click come from `screens.probe`, a declared observation that the parity filter
  sees like any other. A synthetic click carries no information back into the agent's context, so
  it is not a Principle-I leak surface — but the claim above should not have been that there was
  nothing to audit.
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

- **CORRECTED 2026-09-22.** This bullet read: "`catalogs/capabilities.yaml` currently declares 23
  capabilities; all 23 are `path: firetuner`, and `path: bespoke` appears zero times in the file."
  The file now declares **27** capabilities, **26** `path: firetuner` and **one** `path: bespoke`.
  The bespoke one is `prompts.orders`, and it was shipping as `path: firetuner` while issuing a real
  synthetic mouse click. **The audit statement was true of the file and false of the harness**, which
  is the failure mode this document exists to catch: `lua/ingame/screens.lua`'s `respond` returns a
  control's on-screen rectangle and `capability/executor.py` clicks its centre through
  `HostPlatform.send_input` (XTest on Linux). The gap was measured on 2026-09-21 (blocks 13-15) —
  no Lua API reachable from `InGame` fires a control's registered callback,
  `control:CallCallback(Mouse.eLClick)` returns without error and fires nothing, and the generic
  `UI.RespondToPrompt` exists in none of Firaxis' 645 shipped Lua files
  (`spikes/lua-accessor-audit-2026-09-21.md`) — but was recorded only in a Python comment. It now
  lives in the capability's `firetuner_gap`, which is what Principle II asks for.
- **The hole in the enforcement, and what closes it.** `models/catalog.py`'s `_bespoke_requires_gap`
  fires only on `path: bespoke`, so a mislabelled bespoke path satisfied it *vacuously* — the
  validator could not have caught this, and neither could the "`path: bespoke` appears zero times"
  check above, which is the same blind spot written as evidence.
  `tests/contract/test_synthetic_input_declaration.py` (new, 2026-09-22) pins every in-harness
  `HostPlatform.send_input` call site as data, re-derives the set from the source with `ast` so a
  new synthetic-input path cannot ship without being declared, and asserts the capability each one
  serves is `path: bespoke` with a non-empty `firetuner_gap`. Confirmed by reverting
  `prompts.orders` to `path: firetuner` / `firetuner_gap: null` and observing the new check fail
  with the capability named, then restoring (file checksum verified identical).
- **Firetuner-first still holds inside the corrected capability.** Where a direct Lua call exists it
  is the primary route and the click is the fallback, with `path` / `path_reason` recorded on every
  result: the diplomatic approach answers through `DiplomacyManager.AddResponse` / `CloseSession`
  keyed off the game's own `DiplomacySelections` rows; the acknowledge-only popups fall back to
  `UIManager:DequeuePopup` / `ContextPtr:SetHide`. The era dedication chooser and the congress page
  have no direct call at all. The capability was deliberately **not** split into a Firetuner half
  and a bespoke half, because every answerable prompt family can return a host-click request, so no
  declaration would have been left on the pure-Firetuner side.
- **The one candidate bespoke path on the save side is live-verified, and the evidence upgrades the verdict from
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

- **The load half — this bullet is also stale, flagged 2026-09-22.** It read: "no `load_game`
  capability is declared and no code path invokes `Network.LoadGame` today, so there is nothing to
  audit for a premature bespoke path." `src/civsim_harness/saves/load_game.py` (T217) invokes
  `Network.LoadGame` from the front end and is wired into `run/composition.py`; the T217 spike
  (`spikes/t217-RESOLVED-frontend-loadgame.md`) retracts the "refuses from Lua" finding of
  `spikes/load-path-linux.md`. So the Firetuner load path exists and the bespoke UI-driven load
  driver is, like the save-dialog driver, forbidden rather than merely undue.
- **One synthetic-input path remains outside the catalog entirely, and needs a ruling rather than
  a claim here.** That same loader presses `Escape` through `HostPlatform.send_input` to dismiss
  the leader-intro screen a load can stop on — necessarily while the tuner port is closed *by the
  load*, which is exactly why the screen can be neither observed nor dismissed through the tuner.
  The gap is measured and recorded in the module docstring, and the press refuses outright unless
  `focus_window` succeeds. But save loading is operator/branch lifecycle (FR-033, FR-045/FR-046),
  not an agent-facing action: there is no load declaration in `catalogs/actions/` and no capability
  for it, so Principle II's capability-path rule has nothing to bind to. **Open question for the
  hypervisor: should a lifecycle path that drives synthetic input outside the catalog carry a
  declaration of its own?** Pinned as data in
  `tests/contract/test_synthetic_input_declaration.py` with its reason, so it cannot go quiet while
  the question is open.
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

### XTest synthetic input: driven, with three defects found and fixed

`spikes/r5-xtest-input-linux.md` (T052, R5). All input injected into a nested `Xephyr` server and
verified by reading `xev`'s stream, never by trusting `InputResult.status`.

- **XTest events arrive as real device input (`synthetic NO`)**, indistinguishable from a human's
  keystroke — unlike `XSendEvent`, which arrives flagged `synthetic YES` and which games routinely
  ignore. That is the load-bearing reason to use XTest.
- 🔴 **Three defects in `send_input`, all reporting `ok` while dispatching nothing or the wrong
  thing** — the same class as the capture false-pass, and the reason each was measured rather than
  reviewed. (a) any key outside a five-entry table was **silently dropped**; (b)
  `InputEventKind.text` **had no branch at all**, so a text event did nothing — and it is the event
  the bespoke save path most needs, since a save dialog wants a filename typed into it; (c)
  **`button` was ignored**, so a right-click request delivered button 1. All three fixed, with
  undispatchable events now reported `failed` naming the offending event and index.
- ⚠️ **`InputEvent.x/y` are root-absolute, and the port does not say so.** The Civ window on this
  host sits at `2560,0` on a second monitor, so **a caller passing window-relative coordinates
  would click on the wrong monitor**. Behaviour is pinned by a regression test; **the port should
  state the coordinate space explicitly** — flagged, not changed.
- ⚠️ **XTest has no window targeting and `send_input` takes no window.** It injects into whatever
  holds focus, so **nothing stops synthetic input reaching the operator's own windows** if the
  client is not focused. No focus management exists anywhere in the harness.
- **Civ VI has still received no synthetic event from this adapter** — whether the client accepts
  XTest input is inference, not measurement, until the client can be launched.

### Reachability audit: capture has a window that is always `None`, and input has no caller at all

Run 2026-09-20 from the Linux node against its own contribution, after the owning side found the
same shape three times (fabricated V2, dead Lua `return`s, guards with no callers). **The question
asked was not "is this code correct" but "does anything call it".**

⚠️ **Scope caveat:** this reflects the code visible from `origin/002-civ-playing-harness` at
`8c3d8f8` plus `live/linux`. The Phase 9 composition-root commits (`adda5c2`, `2f301c2`, `d919786`)
**are not pushed to the remote**, so some of the below may already be wired there. Each finding
names exactly what was searched so it can be checked off quickly rather than re-derived.

- 🔴 **The synthetic input layer is reachable from nothing.** `grep -rn "InputEvent("` across `src/`
  returns **zero constructions** outside the port's own definition, and `send_input` has **no
  production caller** — only the three adapter definitions and the `HostPlatform` protocol.
  `InputEventKind` appears only in `host/`. So no end-turn keystroke, no save-dialog driving, and
  nothing that would exercise the bespoke save path. **The three `send_input` defects fixed today
  were, in production terms, fixes to dead code** — they matter the moment a caller exists, and not
  before.
- 🔴 **Capture is wired, but always receives `window=None`.** The chain
  `decision_loop -> capture_for_step -> select_capture_path -> host.capture_window` is real. But the
  window comes from `ctx.window_provider()`, declared as
  `window_provider: Callable[[], GameWindow | None] = field(default=lambda: None)`
  (`run/decision_loop.py:208`) and **assigned in exactly one place in the repository: a test**
  (`tests/integration/test_prompts.py:243`, supplying a hard-coded `DEFAULT_WINDOW`). Nothing in
  `src/` ever sets it. In production every step therefore captures `None`, is treated as a host
  failure, and is recorded **visually degraded** — regardless of the capture path beneath it.
- 🔴 **`find_game_window` has no production caller either**, on any platform. So even a caller
  wanting to set `window_provider` has nothing wired that resolves a window to give it.
- 🟡 **`capture_preconditions()` has no production caller — this one is ours.** It was added on the
  Linux node today, and it is the same shape: a preflight check that runs only when a test calls it.
  Flagged against our own work rather than waiting to be caught.

**The consequence worth stating plainly: the "last capture stub is gone" claim is true about the
stub and false about the outcome.** Real `BGRA8` pixels are produced on Linux/X11, and **no image
reaches an agent in production**, because no window is ever resolved to capture. The two gaps are
independent and both must close before Principle I's visual half is evidenced.

This is the same root cause the owning side named — *a fake that shares the defect's assumption
confirms production forever*. Here the fake is `DEFAULT_WINDOW`. **Generalised rule proposed: for
anything crossing a process or protocol boundary, assert on what the far side received, never on
what our side returned** — a fake cannot fabricate an X server's error reply or an `xev` stream.

### 🔑 T217 RESOLVED — the save load works from Lua; there is no Firetuner gap

`spikes/t217-RESOLVED-frontend-loadgame.md`. `Network.LoadGame` returned **`true`** and the client
came up in the saved position — verified on a fresh connection after the phase transition (turn 1,
`LEADER_ELEANOR_ENGLAND`, 2 units, 10 gold), not from the call's own return value.

The three failed rounds had **two** causes, neither of them the argument shape they were chasing:

1. **Phase.** Every previous attempt ran in `InGame`. The call is **front-end only** — Firaxis' own
   shipped automation gates it on `UI.IsInFrontEnd()` and calls `Events.ExitToMainMenu()` otherwise
   (`automation_dailysmoketest.lua:241`). In `InGame` it is callable and always refuses, which is
   why `ok=true ret=false` looked so much like a bad table.
2. **Enum names.** The real table is `SaveTypes`; `SaveGameTypes` does not exist, so the guessed
   member was a nil index inside a `pcall` — a missing field, not an error.

Consequences for the design, all measured:

- **Option B (bespoke UI driver) is withdrawn**, and the Principle II `firetuner_gap` is **not**
  declared — the gap does not exist. T177, T226's load and every `resume-from` are unblocked.
- **The tuner port closes for the duration of the load** and rebinds ~5 s after the game is
  interactive. A `SaveLoader` — and the crash detector of T233 — must treat connection-refused
  during a load as expected, not as a crash.
- ⚠️ **Open:** the load stopped on the leader-intro screen with a `CONTINUE GAME` button and waited
  indefinitely; one click dismissed it. Whether that screen appears for every load or only for a
  turn-1 save is **not yet known**, so a loader must assume one dismissal click may be needed. That
  is a narrow, single-purpose input — and the first real caller for the input layer that had none.
- **Reading Firaxis' shipped Lua** (`steamassets/base/assets/ui/automation/`) answered in one file
  what three rounds of probing could not. It also yields the event names option A was hunting
  (`LuaEvents.AutomationMainMenuStarted`, `AutomationGameStarted`, `AutoPlayEnd`) and an
  `AutoplayManager` API, none of which are needed now but none of which were known.

### ✅ Both host primitives verified against the real client — and four traps

`spikes/r7-live-client-session-linux.md`, `tests/live/test_civ6_real_frame.py`,
`tests/live/test_civ6_real_input.py`.

**Capture:** the full production chain (`find_game_window` → `capture_preconditions` →
`capture_window`) returns real 1920x1200 `BGRA8` frames from Civ VI in **34–47 ms**, faster than on
synthetic windows, with no flicker across back-to-back captures. The frame **cross-validates the Lua
reads** — pixels say turn 1/500, Eleanor, 10 gold, settler awaiting orders; the tuner says the same.

**Input:** Civ VI **accepts XTest events** — previously inference. Asserted on the client's pixels
changing, not on the returned `InputStatus`, and guarded so nothing is sent unless Civ holds focus.

**The loader is complete and coordinate-free.** The leader-intro screen appears on *every* load (not
just turn-1 saves) and blocks indefinitely; **`Escape` dismisses it**, `Return`/`space` do not. So
`SaveLoader` = one Lua call + one keystroke, with **no UI coordinates anywhere** — which retires the
platform-specific-coordinates objection to a UI-assisted load.

Four traps, three of the "succeeds while doing nothing useful" family:

1. 🔴 **`find_game_window()` returns `None` if the process is located by cmdline.** Under Steam's
   pressure-vessel runtime the wrapper/`reaper`/`pv-adverb` all carry the game path, and `pgrep -f`
   returns the wrapper first; only the real `Civ6` owns a window. Use `pgrep -x Civ6`. This is
   directly the unfixed `window_provider` defect's failure mode.
2. 🔴 **The client exited cleanly mid-session after an end-turn**, ~6 s later, with no dump and a
   normal Steam teardown. **Cause not established** — possibly earlier synthetic keys leaving a menu
   focused, possibly `ACTION_ENDTURN` from an unexpected UI state. Lesson: `send_input` returning
   `ok` says a key was dispatched, not that the client is where we think it is. Verify the expected
   screen before *and* after every key.
3. 🔴 **`steam://rungameid/289070` can resolve to Remote Play streaming from another machine** — a
   window titled `Sid Meier's Civilization VI (DX11) [Streaming]`, showing real gameplay, with **no
   local process and no tuner**. Readiness must require `pgrep -x Civ6` **and** a bound tuner port,
   never a window title.
4. 🟡 **`doctor` prints `capture path : none`** on this host right after capture returned three real
   frames (reported, not fixed — outside owned paths). Also **`provider key : MISSING` is real
   here too**, so both hosts are blocked on that for a decision-making run.

### 🔴 T218 — `major_count` reads the wrong number in-game

`spikes/t218-RESULTS-setting-getters.md`. Five of six getters resolve; one is unavailable; and one
returns a **plausible wrong answer** that no test against a fake can catch.

`GameConfiguration.GetAIPlayerCount()` returned **16** in-game, against **6** for the same call at
the Create Game screen. The per-player decomposition is unambiguous: **16 = 5 AI majors + 9
city-states + Free Cities + Barbarians**. In-game it counts every non-human player; at setup, where
city-states are not yet instantiated, it means major AI count. A preparation that records the setup
value and a verification that re-reads it in-game compare 6 against 16 and disagree forever.

Derive it instead by counting `player:IsAlive() and player:IsMajor()` over `Players`, minus the
local player — measured to give 6 majors / 5 opponents on the same client where the getter said 16.

Also settled: `map_seed` works via `GAME_SYNC_RANDOM_SEED`, and **there are two seeds** (game
`-986870912` vs map `-986870911`) — recording one loses the other. `map_size` resolves through
`GameInfo.Maps`, **not** `GameInfo.MapSizes`. `mod_set` works via `Modding.GetActiveMods()` and must
be keyed on `Id`, since names are partly unlocalised. **`resources` has no getter at all** in-game —
it should be recorded as not-observable rather than left returning `UnreadSetting`, which compares
unequal to everything and lands a run `failed` before turn 1.

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

### ✅ T248 — the production `LuaSaveLoader` loads a real save end-to-end (2026-09-20 night)

The production loader (`saves/load_game.py`, landed at `2f1a16d` with `focus_window` on the
`HostPlatform` port) was re-verified live with `tests/live/test_production_save_loader.py`
**unmodified**, on the same Aspyr 1.0.12.9 client that measured the original failure:

| run | shape | result |
|---|---|---|
| ATTEMPT 1 | production loader exactly as shipped, no external help | **PASS, 38.4 s** (was FAIL after 160.4 s) |
| ATTEMPT 2 | same loader + the test's own external Escape loop | PASS, 34.6 s |
| solo | production loader alone, counters read back | PASS, 42.5 s — `intro_dismiss_presses = 1`, `intro_dismiss_skipped = None` |

The one press means `focus_window` (EWMH `_NET_ACTIVE_WINDOW`, first production use) returned
`ok` on X11/Cinnamon with a browser and a terminal open on the same desktop, the single `Escape`
landed in the game, and the retry loop stopped the instant the tuner port answered. Full transcript
and the two honest caveats (the test's own recovery Escape during the T246 post-close tail; the
external presses being the test's, not the loader's) are in `spikes/t248-intro-dismissal-patch.md`,
"Re-verified". Windows/macOS `focus_window` halves still report `unavailable` — the loader refuses
to press there, loudly, until they are implemented.

### T202 — per-run cost and duration under the one-call-per-step model (2026-09-21, Linux node)

Every number below is read from `civsim-match-store.db`'s own step bundles (`model_call.cost`,
`model_call.latency_ms`, `turn_cycles.started_at/ended_at`) with a read-only query, not from the
spike narrative. Seven runs carry turn cycles: two fake-provider runs (zero-cost, the T213-era
probe and the landed-code demo `run-9505f323`) and **five model-driven runs** through the
production `OpenRouterProvider` (`anthropic/claude-sonnet-5`, text-only — no image reached the
model in any of them, see T252). Every model-driven run is early game (game turns 5–17) and
capped at 3 harness turns by the driver, so this is the *early-turn* cost of the accepted
trade-off; the late-game measurement (hundreds of steps in one turn) is T201's, and is unmeasured.

| run | outcome | harness turns | steps = calls | calls / turn | $ / turn | $ / call | tokens in / out | model time | wall-clock / turn |
|---|---|---|---|---|---|---|---|---|---|
| `run-e8cf4b9a` | paused (backstop readback) | 1 | 8 | 8.0 | $0.084 | $0.0105 | 26,744 / 3,091 | 54 s | 62 s |
| `run-bba7a243` | finished | 3 | 24 | 8.0 | $0.080 | $0.0100 | 80,232 / 8,054 | 149 s | 56–60 s |
| `run-ea014cb6` | finished | 3 | 24 | 8.0 | $0.132 | $0.0166 | 106,344 / 18,457 | 269 s | 70–112 s |
| `run-d1d3e263` | finished, **city founded** | 3 | 21 | 7.0 | $0.136 | $0.0194 | 95,790 / 21,641 | 332 s | 75–146 s |
| `run-8bc17e7e` | paused (unknown screen) | 2 | 16 | 8.0 | $0.147 | $0.0184 | 76,992 / 14,006 | 213 s | 99–126 s |
| **all five** | | **12** | **93** | **7.8** | **$0.119** | **$0.0153** | 386,102 / 65,249 | 1,017 s | ~97 s |

Observations, stated as measured:

- **Calls per turn is the backstop, not the model.** 8 of the 12 turns hit exactly the
  no-progress cap (8 steps) because the model's decisions were refused before dispatch; the one
  turn where a decision landed (attempt 4, turn 1: `units.found_city` applied) took 9 steps, and
  the turn after the city existed took 4. A turn where every decision lands will cost what the
  model actually needs, which nothing here has yet measured.
- **Per-call cost grew across the night, by design.** $0.010 → $0.019 per call as the observation
  text gained the action catalog (attempt 3 on) and the model's reasoning got longer; input is
  ~3.3–4.6 K tokens per step, all text.
- **Model latency is ~85 % of wall-clock.** 1,017 s of the ~1,170 s the twelve turns spanned was
  the provider; the fourteen-declaration Lua sweep, quicksave and end-turn confirmation are the
  rest (~10–20 s per turn, consistent with the fake-provider demo's 2 s per turn plus the AI turns).
- **Total: $1.425 for 93 calls over 12 harness turns**, matching the OpenRouter `auth/key` delta
  the spike recorded ($1.42). `model_call.cost.amount_usd` and both token counts are populated on
  every model-driven call — the store's own record is sufficient for this table without the key
  endpoint. (The `model_calls` *table* is still empty: successful calls ride inside the step bundle
  by design — `decision_loop.py` writes the table only for calls that produced no decision. Spec
  003 re-specifies model calls as rows; not changed here.)
- **Extrapolation, labelled as such:** at the measured ~$0.12–0.15 per early turn, a 300-turn
  soak (T201) model-driven would be roughly $40–45 if late turns cost what early ones do — they
  will not; late turns have more units, cities and steps. T201 should run with the fake provider
  first and a bounded model-driven segment second.

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

- **The harness's own `live`-marked test files for T177, T191–T194 and T198 do not exist yet.**
  (Superseded wording, 2026-09-21: it is no longer true that no live test has run against a real
  client — `tests/live/test_production_save_loader.py` passed unmodified against the real client
  (T248, 38.4 s), `tests/live/probe_observation_bodies.py` passed 14/14 (T213), and the production
  composition root played three turns with the fake provider and twelve with a real model
  (`spikes/demo-evidence-linux-landed/`, `spikes/first-model-driven-runs-linux.md`). What remains
  unevidenced is the specific per-task live assertions those six task ids name, each now annotated
  in `tasks.md` with what it needs.)
- **Capture pixel extraction is implemented on Linux/X11 only; Windows and macOS remain stubs.**
  The Linux `XComposite`/`NameWindowPixmap` path now returns real, correctly-ordered `BGRA8` pixels
  and is occlusion-immune through the harness's own code
  (`spikes/r6-xcomposite-readback-linux.md`), and the landed-code demo captured 12 real frames
  *of Civilization VI* through it (`run-9505f323`).
  **Corrected 2026-09-22 (T288). This passage previously read "No frame has yet been shown to the
  agent on any platform". That was false, and false in the unsafe direction.** Measured read-only
  from a copy of `civsim-match-store.db` on 2026-09-22: of **843** captures, **396** are
  `screened_clean` and **314 carry `shown_to_agent = true`** across **24** runs (290 dated
  2026-09-21, 24 dated 2026-09-22), and **301 of 670 `model_calls` carry an image**. The
  provenance gate accounts for **316** of the **447** withholds, not all captures; the other 131
  are `non_player_ui`. Real frames of Civilization VI have reached a model, in volume.
  **Why this mattered more than a stale number:** SC-009 and SC-019 are release-*blocking* audit
  obligations, and their real-frame test T194 (`tests/live/test_capture_hygiene.py`) has never
  run. An auditor reading the old sentence would have concluded that no real-frame audit was
  owed. **It is owed, on 301 delivered images, and it is unpaid.** Worse, the gate could not have
  caught the contaminant in any of them: until 2026-09-22 the content gate's declared-text
  technique was dead in production because no caller supplied `detected_text_tokens`, leaving
  eight of ten reject categories -- `firetuner_window` among them -- undetectable on every
  platform (T264). A separate retro-audit of the delivered backlog examined 188 distinct frames,
  by eye and programmatically, and found **0 contaminated, 301 clean, 0 unknown**. The correct
  wording for any sign-off citing that audit is **"no contamination was found"**, never "the
  screening gate held" -- and every frame in it was captured *before* the fail-closed fix landed,
  so it is evidence about the backlog and says nothing for or against that fix. Image delivery is
  now closed on all three platforms until the text-evidence plumbing lands. Linux/Wayland capture
  remains stubbed (T257); Windows and macOS pixel extraction is unwritten.
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

### Success criteria with no evidence behind them (audited 2026-09-22)

Named explicitly, by SC number, so this cannot be read as "covered." Each row below was verified
against the current tree, not carried over from a prior pass — where the tree had moved since a
task's own note was written, this row reflects the tree, not the note.

| SC | What it claims | What exists | What does not |
|---|---|---|---|
| **SC-001** | >= 90% of started attempts reach their stop condition unattended | Nothing to measure against by itself — the per-attempt failure/stop bookkeeping this would be computed from does exist | **No success-rate counter exists anywhere in `src/`** — there is no quantity for a test to assert a percentage against. Compounded by **T237** (open): a cold client still needs a human operator to reach turn 1, so a fully unattended attempt cannot even begin yet |
| **SC-002** | A run plays every era to victory or defeat, unattended | Five model-driven runs recorded and measured (T202) | **No test mentions eras or a full game.** Every recorded run is early-game (game turns 5-17); the **longest single recorded run is 3 harness turns**, capped by the driver. Late-game and full-era behavior is unmeasured, not merely unmodeled |
| **SC-010** | A genuinely killed client is detected and recorded within 60s | The detection wiring itself is production code with real callers (T233, landed): `DetectionAggregator`/`HeartbeatMonitor`/`ProcessLivenessMonitor` run inside the turn cycle, covered by `tests/integration/test_end_to_end_wiring.py` and `test_turn_cycle.py` | Every one of those tests exercises the wiring against **fakes on a simulated/configured clock** (`tests/unit/test_detection.py` asserts the 60s figure only as a *configuration bound* — `DEFAULT_OPERATION_BOUNDS_S[...] <= 60.0`). **No test has ever measured detection latency against a genuinely killed client process.** T193's live test file (`tests/live/test_crash_recovery.py`) does not exist |
| **SC-011** | Across >= 20 unattended runs, zero silently-missing turns | The **per-run** gap derivation (`FR-052`, record completeness) is well covered — unit, integration and contract tests all exercise it | **The >= 20-run fleet quantifier returns zero grep hits anywhere in `tests/`.** Nothing asserts the claim at fleet scale. The **largest recorded corpus is 7 runs** (T202: two fake-provider, five model-driven) |
| **SC-014** | Two branches from one save point begin from an identical game position; the parent's record is unchanged | `tests/integration/test_branching.py` asserts the **parent-immutability half** thoroughly (the parent's own save-point record is byte-identical before/after branching) and that both branches load the same recorded save point. A live test for the **position-equality half** now exists — `tests/live/test_branch_identity.py` (T177), written 2026-09-22 in commit `c440496` | That live test **has never been executed against a real client** — its own commit message says it only "collect[s]/skip[s] cleanly" so far. No test anywhere asserts that two independently-loaded branches' *live game state* (turn number, treasury, map seed) actually matches. The identical-position claim rests on the R5 spike narrative only |

**None of this is "everything is unverified."** The per-run mechanics underneath several of these
rows — completeness derivation, per-run cost/duration, lineage recording, the detection wiring's
own cadence — are well asserted and covered by tests today. What is empty, specifically, is the
**fleet-scale** claim (SC-011), the **full-game/every-era** claim (SC-002), the **unattended-rate**
claim (SC-001), and the **real-client** half of SC-010 and SC-014. An auditor should read each row
above as "the mechanism this depends on is tested; the claim itself has not been measured," not as
"nothing here works."

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
