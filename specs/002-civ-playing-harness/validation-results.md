# Validation Results — Civilization-Playing Harness

**Feature**: `002-civ-playing-harness` | **Date**: 2026-09-20

This file is the Polish-phase record of validation evidence tasks.md Phase 8 calls for. It is
written incrementally by several tasks; this entry covers **T205 — the constitutional compliance
audit** required by the constitution's Governance section before implementation closes, plus the
live-client findings and open spec-amendment candidates that audit surfaced along the way. It does
not yet cover T200 (Scenarios 1–10 end to end), T201 (soak validation), or T202 (cost/duration
observations) — those require live-tier scenario runs beyond this entry's scope and should be
appended here by the tasks that own them, not folded into this section.

---

## T205 — Constitutional compliance audit (Constitution v1.0.0, Principles I–VII)

**This is an audit, not a certification exercise.** Its purpose is to say, plainly, what evidence
exists and what does not — including where the answer is "not yet covered." Passing every
principle was not assumed going in, and it did not turn out that way: Principles I and IV both have
open items below that block calling this deliverable fully compliant.

### Summary

| Principle | Status | Headline |
|---|---|---|
| I — Human-Parity Information & Action Boundary | **Partially evidenced** | Structural boundary is real and tested; capture pixel extraction is stubbed everywhere, and the live tier hasn't run |
| II — Firetuner-First, Skill-Extensible Harness | **Compliant** | All 23 capabilities are `path: firetuner`; the one candidate bespoke path was foreclosed by a spike that ran first |
| III — Complete Match Telemetry | **Compliant (structurally enforced)** | `TurnPersistedToken` makes end-turn unreachable without a durably-acknowledged write |
| IV — Reproducible, Seeded Experimentation | **Partially evidenced** | Save/quicksave/branch/lineage machinery is in place; the load half of the save path is unverified and a dependent guarantee rests on it |
| V — Guidebook-Before-Optimization Gate | **Correctly gated, not satisfied** | `GUIDEBOOK.md` does not exist, which is the gate working as intended, not a defect |
| VI — Shared, Unified Observability | **Not yet satisfiable** | Deliverable 1 (the web interface) is unimplemented; this harness's own operator endpoint is deliberately closed and not a substitute |
| VII — Provider-Agnostic Model Access & Resilience | **Compliant** | OpenRouter over plain `httpx`, no vendor SDK on the decision path; crash detection and recovery implemented |

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

**Not yet covered.**

- **Capture pixel extraction is stubbed on all three host adapters.** Confirmed by direct
  inspection:
  - `src/civsim_harness/host/windows/adapter.py:190` — "PrintWindow succeeded but pixel extraction
    (GetDIBits) is not implemented."
  - `src/civsim_harness/host/macos/adapter.py:173` — "ScreenCaptureKit bridging is not implemented
    pending the R6 capture-hygiene..."; `:200` — "CGWindowListCreateImage produced an image but
    pixel extraction... is not implemented."
  - `src/civsim_harness/host/linux/adapter.py:177` — "(NameWindowPixmap + XGetImage) is not
    implemented"; `:196` — "xdg-desktop-portal ScreenCast is not implemented: it needs an
    interactive..."

  This means the screening gates and the parity red-team suite above have been proven against
  *synthesized* capture data, not against a real captured frame on any platform. FR-050's "honest
  degradation" path (images withheld, `visually_degraded = true`) is exactly what a host with a
  stubbed pixel path is exercising today — which is the correct behavior for this state, but it
  also means the visual half of Principle I has no live-client evidence yet.
- **The live tier has not run.** `tests/live/test_capture_hygiene.py` (T194) and
  `tests/live/test_host_platform.py` (T198) are the tests that would exercise a real captured frame
  against a real client; neither has been executed as part of this audit (both require a live
  client and are out of this task's scope — see the Linux live-validation findings below, which
  *do* speak to this from spike work, short of a full test-suite run).

**Conclusion.** The structural boundary Principle I requires is real, tested at the unit/contract
level, and has no known gap in its *design*. What is missing is live-client proof that a real
captured frame, on a real host, clears the same gates the red-team suite proves against synthetic
data — and that proof cannot exist yet because pixel extraction itself is unimplemented on every
platform. This is the single largest open item in this audit.

---

### Principle II — Firetuner-First, Skill-Extensible Harness

**Evidence.**

- `catalogs/capabilities.yaml` currently declares 23 capabilities; all 23 are `path: firetuner`,
  and `path: bespoke` appears zero times in the file. The file's own header comment states this
  plainly: "Nothing in this assignment's scope... needed a bespoke capability."
- **The one candidate bespoke path was foreclosed by a spike that ran first, not after.** T077
  (research R5, `specs/002-civ-playing-harness/spikes/r5-save-path.md`) probed
  `Network.SaveGame`/`Network.LoadGame` under `pcall` against a live client *before* any save-path
  implementation work began. The spike found `Network.SaveGame` real and repeatable (four
  consecutive calls, four valid size-stable `.Civ6Save` files). Because the evidence came first,
  the bespoke input-automation save driver that `contracts/capability-catalog.md`'s own worked
  example had assumed was necessary did not just become unneeded — it became **forbidden**:
  Principle II requires the Firetuner path once one is known to exist. `saves.save_game` is
  declared `path: firetuner` in `catalogs/capabilities.yaml` as a direct result.
- This is the ordering the principle demands: a bespoke path is permitted only to fill a
  *demonstrated* gap, and "demonstrated" means a spike ran and found the gap, not that one was
  assumed and never checked.

**Not yet covered.** Nothing — this principle has no known gap. (Its assurance is bounded by the
same live-tier caveat noted under Principle I: the Lua paths themselves carry `UNVERIFIED` markers
where a live sweep hasn't yet confirmed every call — see [Live-client findings](#live-client-findings-from-the-linux-validation-node)
below for what has been confirmed so far.)

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

**Not yet covered.**

- **`Network.LoadGame` is unverified.** The R5 spike (`spikes/r5-save-path.md`) confirmed
  `Network.LoadGame` exists as a callable function in `InGame` (`type() == "function"`), but the
  spike's own scope statement is explicit: "This spike proves a save is *written*. It does **not**
  prove a save can be *loaded back*." FR-034's "two branches from the same save point begin from an
  identical position" guarantee rests on the load half of this path, and that half has not been
  spiked yet. This is recorded as an open item in the spike's own "Not yet verified" section, not
  something this audit is newly discovering — but it remains unresolved as of this writing.
- **Cross-platform save identity (R20/T199) has not run.** `specs/002-civ-playing-harness/spikes/`
  contains no `r20-cross-platform-saves.md`. Per tasks.md, this spike gates only the *relaxation* of
  the cross-platform branching refusal, not any core capability — cross-platform branching stays
  correctly refused until it exists. Confirmed absent as of this audit.

**Conclusion.** The reproducibility *machinery* (config, quicksave discipline, lineage, retention)
is built and structurally enforced. The one guarantee that still rests on an unverified primitive
is FR-034's identical-position claim across a branch, which needs the `Network.LoadGame` spike to
close.

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
  failures rather than retrying indefinitely (FR-048, SC-021). A T172 addition (visible in the
  in-flight diff at audit time) further hardens this: a save already known absent
  (`missing`/`retention_status == REMOVED`) is refused immediately rather than retried, and a save
  discovered absent only at load time is durably recorded via `saves.addressing.report_save_missing`
  before the run fails — never silently retargeted to a different turn's save.

**Not yet covered.** Nothing at the design level for this principle specifically. As elsewhere, the
live-tier crash-recovery test (`tests/live/test_crash_recovery.py`, T193 — "a genuinely killed
client is detected within 60 s") has not been run as part of this audit.

---

## Live-client findings from the Linux validation node

Recorded here as validation evidence because they came from an actual live client rather than a
spike's reasoning alone, and because several of them materially changed a design assumption
elsewhere in this document.

- **The tuner is independent of `EnableDebugMenu`.**
  `specs/002-civ-playing-harness/spikes/principle-i-debugmenu-linux.md`: port 4318 binds
  identically (12.0s after launch) whether `EnableDebugMenu` is `0` or `1`; every symbol the
  harness uses or plans to use, the one fully-enumerable namespace (`Game`), and the full API state
  table were identical in both modes, with no evidence of any difference found. Recommendation
  adopted: require `EnableDebugMenu 0` for any run whose data feeds trending, metrics, or
  optimization — it costs nothing, since nothing the harness needs depends on it.
- **Window-scoped capture excludes occluding windows; a root-scoped grab leaks harness console
  text.** `specs/002-civ-playing-harness/spikes/r6-capture-hygiene-linux.md`: an `xmessage` dialog
  placed over the client is fully absent from a window-scoped `XGetImage` capture of the same
  region. The same coordinates captured root-scoped (screen grab) show not only the dialog but,
  beneath it, **legible harness command text from a terminal** — a direct, concrete demonstration
  that full-screen capture is a parity breach on this platform, not a stylistic preference.
  Conclusion adopted into the design: "the capture path must be window-scoped. A root/screen
  capture must never be used, even as a fallback."
- **Occlusion-immunity depends on a compositing window manager.** Same spike: the pass above is a
  property of the compositing WM (`Mutter (Muffin)`, X11 `Composite` extension present) redirecting
  windows to offscreen pixmaps — not a property of `import`/`XGetImage` or X11 in general. Without
  compositing, the same call would return the *occluding* window's pixels, silently turning a pass
  into a failure. Recommendation: verify compositing at preflight rather than infer it from
  `XDG_SESSION_TYPE=x11`, and treat "X11 without a compositor" as a distinct, non-`VALIDATED` case.
  **Not currently implemented** in `host/linux` as of this audit.
- **`mod_set` is 22 mods, not empty.** `specs/002-civ-playing-harness/spikes/lua-api-verification-linux.md`
  ("The mod set is not empty — it is 22 mods"): the live client's active mod list is substantial,
  correcting a placeholder assumption a seed set config might otherwise have carried; a seed set
  with a placeholder `mod_set` will fail the build-identity match against a real client.
- **A live sweep found three assumed Lua calls do not exist.** `Game.EndTurn`, `Cities.GetCity`,
  and `Units.GetUnit` all resolve to `nil` in both tuner contexts
  (`spikes/lua-api-verification-linux.md`, `spikes/sweep-raw/*.txt`). The real end-turn action is
  `UI.RequestAction(ActionTypes.ACTION_ENDTURN)`, whose return value carries no success signal —
  which is why `turn.end_turn`'s `verification_predicate` compares turn numbers rather than trusting
  a return value. This is the concrete proof behind the `UNVERIFIED` authoring convention documented
  in `docs/catalog-authoring.md` §5: a plausible-sounding guess would have shipped a call that
  silently does not exist.

---

## Open spec-amendment candidates

Three items surfaced by live-client spike work that are not yet reflected as formal spec changes.
Recorded here so they are not lost between this audit and whichever future session runs
`/speckit-clarify` or amends `spec.md` directly.

1. **`EnableDebugMenu` should be recorded at preflight.** The debug-menu spike's own recommendation
   (`spikes/principle-i-debugmenu-linux.md`): "Have preflight read and record the setting through
   the host port rather than assume it... so a run's parity configuration is reconstructible from
   its record alone rather than from a claim about how the host was set up." Not yet implemented —
   `doctor`/preflight does not currently read or record this value.
2. **Graphics settings are an input to the agent but are pinned nowhere.** Noted in
   `spikes/launch-tuning-linux.md`: "the owner configured this host for lowest graphics. Graphics
   settings change what the agent [sees]... but the harness does not [pin them]." A run's visual
   observation surface is affected by a setting that is not part of the seed set's pinned identity
   (`civilization`, `leader`, `ruleset`, `mod_set`, `game_build`), which is a comparability gap
   between runs played on hosts with different graphics settings.
3. **A minimized client may return stale frames.** `spikes/r6-capture-hygiene-linux.md`: capture
   succeeds and returns valid-looking, current game content when the client is minimized on this
   host — but whether Civ VI keeps *rendering new frames* while minimized was not tested. If it
   does not, the redirected pixmap would return the last-rendered frame indefinitely, which "still
   looks like a perfectly valid capture" — the exact stale-board failure Scenario 8a exists to
   prevent, arriving through the capture path instead of through observation reuse. The spike's own
   conclusion: "this must be settled before any unattended run is allowed to minimize the client...
   until then, keep the client unminimized during runs." Not yet resolved or encoded as a
   constraint anywhere in the harness itself (it is currently only a documented caution in the
   spike, not an enforced preflight/runtime check).
