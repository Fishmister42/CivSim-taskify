# Hypervisor Loop Log (Windows side)

Coordination peer: Linux hypervisor, via `Fishmister42/CivSim-taskify` issue #1.
Loop protocol: read constitution -> determine phase -> ensure TASKS.md current ->
increment attempt -> (attempt>0) `/speckit-analyze` -> `/speckit-implement` ->
`/speckit-converge` -> CONVERGED? done : loop.

**Convergence definition adopted:** a spec is CONVERGED when (a) `/speckit-converge`
appends zero new tasks, and (b) every open task is either done or explicitly gated on a
live Civilization VI client / owner action. Live-gated tasks are tracked as DEFERRED-LIVE,
not as failures — they cannot converge on a headless loop.

---

## Entry state (2026-09-20, session start)

| Spec | Tasks | Done | Open | Attempt | Status |
|---|---|---|---|---|---|
| 001-unified-web-interface | 65 | 0 | 64 | 0 | tasks.md current, never implemented |
| 002-civ-playing-harness | 211 | 194 | 17 | >0 | Phase 9 wiring open; rest live-gated |

Branch: `002-civ-playing-harness` (integration branch; `main` is behind).

### Spec 002 open-task triage
- **Implementable now:** T209 (composition root), T210 (preparation chain + phase-boundary
  `refresh_state_indices`), T211 (end-to-end wiring integration test). This is the critical
  path — the integration-readiness audit (dba5833) found 933 passing tests over components
  with no production caller; `civsim run start` failed on its first line.
- **DEFERRED-LIVE (needs a real client / per-platform host):** T050, T051, T052, T099,
  T177, T191, T192, T193, T194, T198, T199, T200, T201, T202.

---

## Run 1 — 2026-09-20

**Attempt 002 = N+1, Attempt 001 = 1.** Two swarms launched in parallel in the primary
worktree. Files are disjoint by design (001 tasks.md scopes 002's source out); `pyproject.toml`
is the only shared file and 001's T002 is additive-only.

- **Swarm A** — 002 Phase 9 (T209-T211). Critical path.
- **Swarm B** — 001 Phases 1-2 (Setup + Foundational).

Git discipline this run: **agents do not commit**. The hypervisor commits sequentially after
each returns, to avoid two agents racing on the index in one worktree. This worked — both
returns were committed path-scoped with zero cross-contamination.

### Run 1 results

**Swarm B (001 Phases 1-2) -> commit `8ba697b`.** COMPLETE. T001-T018. 148 feature tests pass,
0 fail. `civsim-web doctor` verified green by the hypervisor directly, not taken on report.
Surfaced four spec/plan/contract contradictions, recorded in 001's tasks.md under "Foundation
notes for Phase 3-6 contributors" rather than silently patched. Hypervisor resolved its one
blocker (the `civsim-web` entry point + wheel packaging edit its additive-only constraint
forbade).

**Swarm A (002 Phase 9) -> commit `adda5c2`.** COMPLETE. T209-T211. 1117 passed, 3 skipped;
ruff + mypy clean across 107 files. `civsim run start` now reaches preparation.

**The finding of the run.** The convergence pass appended **Phase 10 (T212-T222)**: eleven
places where the composition root is assembled but hard-coded. T212 is the one that matters,
and the hypervisor verified it independently in `src/` rather than trusting the report:

> Nothing in `src/` constructs anything but `UNPROBED`, so `evaluate_host_gate` refuses
> **every run on every host** — including the validated Linux one.

The CLI's refusal message says "no verified quicksave path exists **on this host**", which
reads as host-specific and is not. The tell is `detail={'reason': 'not yet probed'}` rather
than `'probe failed'`. Posted to the board with that warning explicit, because the Linux peer
would otherwise have attributed the refusal to its own box.

**002 ledger after Run 1:** 197 done, 25 open (11 newly appended). NOT CONVERGED.

---

## Run 2 — 2026-09-20

Three swarms concurrent, lanes assigned by file ownership to make collisions structurally
impossible rather than merely unlikely:

- **Swarm C** — 001 Phase 3 (US1, the MVP). Owns `src/civsim_web/**`, `panels/**`, 001 specs.
  Went first and alone among the user stories because it *creates* `routes/turns.py` and
  `tests/contract/test_web_read_api.py`, which US2/US3/US4 only extend. The earlier
  "all four can go parallel" readiness call was wrong on this point — US1/US2/US3 all touch
  `routes/turns.py`, so four parallel agents would have collided.
- **Swarm D** — 002 Phase 10 headless batch: T212, T214, T216, T219, T220, T221, T222.
  Owns `run/composition.py`, `host/**`, `catalogs/**`, `config/guidance.py`. T212 prioritised.
- **Swarm E** — 002 T215 (`Runner.resume_from`). Owns `run/runner.py`. Explicitly barred from
  `composition.py`; D explicitly barred from `runner.py`.

### Orchestration lesson from Run 1 (do not repeat)

Swarm A **spawned a child that duplicated its own mandate**. Both parent and child implemented
T209-T211, each perceiving the other as an unannounced foreign agent editing "its" files. Cost:
~1.0M subagent tokens for one artifact. It cost correctness nothing — the parent converged onto
the child's work rather than overwriting it, and `git diff` against commit `adda5c2` came back
empty, confirming no work was lost — but it was pure waste.

**Cause: the hypervisor said nothing about delegation.** An agent given a multi-task mandate and
an Agent tool will delegate, and will not recognise its own child's edits as its own.
**Fix applied from Run 2 onward:** every agent prompt states explicitly whether it may spawn
subagents, and names which files it must serialise edits to personally. Swarm D carries exactly
that clause for `run/composition.py`.

### Three defects Phase 9 found that 933 passing tests did not

1. **V2 verification was fabricated.** `_read_setting` echoed each configured value back as its
   own "actual", so `verify_configuration` compared a value to itself and passed vacuously for
   every field, on every run, always. Replaced with `LuaGameSetupReader` + an `UnreadSetting`
   marker that compares unequal to everything, so unreadable fields fail the run closed.
2. **Preparation demanded two mutually exclusive game phases** — `resolve_game_states()`
   (in-game only) followed by leader selection via `HostGame` (Create Game screen only). No
   moment satisfies both; the path could never have succeeded against a real client.
3. **All four Python-embedded Lua bodies were dead** — each ended in `return { ... }`, and the
   tuner reads only *printed* output between nonce sentinels. None could ever return a value.

**Shared root cause, worth remembering:** each was tested against a fake that shared the defect's
assumption. A fake written by the same agent in the same sitting as the production code will
confirm that code forever. That is the predictor for where the next one hides.

**DEFERRED-LIVE grew to 17:** the original 14 plus T213 (verify four Lua bodies print JSON —
all previously ended in a bare `return`, which the tuner's print-only channel discards),
T217 (production `SaveLoader`, blocked on the load-path spike), T218 (five unverified setting
getters).

### Run 2 results (partial — Swarm D still running)

**Swarm C (001 Phase 3 / US1) -> commit `9010407`.** COMPLETE, T019-T034. 286 feature tests
pass. Built `tests/contract/test_web_parity_boundary.py` (80 tests) guarding the Principle I
asymmetry — the web path may show the *user* what the *playing agent* must never receive.
Rejected reading `blob_ref` off disk to work around a port gap, on the grounds that it reaches
around the port and creates a second unaudited path to run data. That was the right call.
Five further artifact contradictions recorded.

**Swarm E (002 T215) -> commit `2cad61e`.** DONE. `Runner.resume_from` implemented by
delegating to the existing `RecoveryEngine` — no second load path written, which given this
repo's failure mode was the point. Lineage recorded **before** the load as well as after, so a
rewind that dies mid-flight still records what it was attempting (Principle IV). Supersede
ordered after the load, keeping today's guaranteed T217 failure non-destructive.

### Open item to file as a task when Swarm D returns

**Attempt-index gap (found by Swarm E, reported not chased).** Once a real `SaveLoader` lands,
the replayed turn still cannot persist: `run/turn_cycle.py::run_turn_cycle` hardcodes
`attempt_index = 0`, and `sqlite_adapter.write_turn_cycle`'s D4 check rejects a second write of
`(run_id, turn_number, 0)` with different content. Needs an attempt-index base on
`TurnCycleDependencies`. Documented in `resume_from`'s docstring. **File as T223.**

---

## Run 3 — 2026-09-20

**Swarm D (002 Phase 10) still in flight** from Run 2; `composition.py` was observed mid-edit
(`NameError: _evaluate_stop_facts`), which is expected and is the source of the only failing
tests in the repo right now.

Two new 001 swarms, per Swarm C's extension-surface analysis:
- **Swarm F** — Phase 4 / US2 (T035-T042). Owns `routes/turns.py`.
- **Swarm G** — Phase 6 / US4 (T050-T058) **minus T056**, which extends a file US3 has not
  created yet. Owns `viewmodels/base.py`.

US3 (Phase 5) is deliberately **not** running: T043 extends `routes/turns.py`, which Swarm F
owns this wave. It follows F.

Both carry an explicit **no-subagent** clause after Run 1's duplication incident, and both are
told which two append-only files they share (`tests/contract/test_web_read_api.py`,
`routes/__init__.py`) with instructions to re-read on conflict.

**Hypervisor decision recorded: `panels/VERSION` stays unfrozen and no `VERSION.lock` is
created until Phase 6 completes.** Freezing mid-construction would force version bumps out of
concurrent story agents and record build churn as schema history. Revisit at Phase 7 (Polish).

### Correction to the hypervisor's own brief

I briefed Swarm F with "Phase 4 = T035-T042". **Wrong** — Phase 4 is T035-T039; T040-T049 is
Phase 5 / US3. The agent read `tasks.md`, closed the actual phase, and flagged the discrepancy
rather than quietly implementing three tasks belonging to a story it had been told not to start.
Lesson: derive task ranges from the file, not from a prior agent's prose summary. The Phase 6
range I gave Swarm G (T050-T058) was correct.

### 🔺 ESCALATION CANDIDATE — the published store port cannot serve deliverable 1

Swarm F's most important finding, and it is architectural rather than local. Spec 001 has now
had to declare **four optional probed capabilities** to work around gaps in `match-store-port.md`
(002's contract):

| Capability | What the published port cannot do |
|---|---|
| `RunConfigurationReader` | resolve `Run.config_id` -> seed / civilization / ruleset / model (FR-018, FR-031) |
| `CaptureBlobReader` | return capture **bytes**; `get_capture` yields only a `blob_ref` |
| `RunCatalogReader` | reach the capability catalog at all |
| `TurnAttemptReader` | address a specific attempt — `get_turn_cycle` selects by boolean, so only *authoritative* and *newest* are reachable, and FR-009's actual case (attempt 0 abandoned, attempt 1 authoritative) is **unreachable** |

Each is individually justified and each fails *honestly* (fields render `unavailable`, never
fabricated). But collectively: **a store satisfying `match-store-port.md` exactly cannot serve
the catalog, capture images, or FR-009.** Four is a pattern, not an accident — deliverable 1 is
quietly absorbing an under-specified contract that belongs to deliverable 3.

`?attempt={n}` (T036) is the sharp edge: **not implementable against the published port.**

**Hypervisor position:** stop letting 001 absorb this. File it against 002's contract as one
item. Deliverable 3 has no spec of its own yet (only 001 and 002 are specced), so
`contracts/match-store-port.md` is the right home and is in scope. Fold into the 002 convergence
pass now running.

---

## OWNER RULINGS (binding — do not re-litigate)

**T217 load path: try C, then A, then B.**
- **C (first, untested):** can the client be started with a save already loaded? Recovery already
  restarts the client, so if launch carries the save, the load path costs no UI automation and no
  event hunting. Most promising variant: the game's own *continue / resume last save* behaviour,
  since the save we want loaded is exactly the most recent one. Then binary flags, Steam launch
  options, `UserOptions.txt` / `AppOptions.txt`.
- **A (second):** finish the `UI.QuerySaveGameList` event-name hunt. Viable because Lua globals
  persist across tuner commands. **Timeboxed** — three rounds have already failed; if a focused
  session does not produce the event name, move on.
- **B (fallback, fully authorised):** documented Principle II `firetuner_gap` + bespoke UI driver,
  **scoped to exactly one operation, "load a named save."** Not a general UI automation layer.
- **Wayland is expendable — pre-approved.** The standing objection to B was that synthetic input is
  unavailable on Wayland by design, so Wayland hosts could never branch or recover. The owner
  accepted that cost explicitly. **Nobody may reopen this as a blocker.**

**Owner agreed with both hypervisor rulings** from the Phase 11 convergence: the additive
`MatchStore.get_run_configuration` read, and the binding `create_branch` ruling (never hard-code
`COMPARABLE`, never omit `host_platform`).

**Spec amendment: ALLOWED.** Scope — (1) 001 `spec.md`: the FR-021 vs Principle III disagreement,
resolved fail-closed in code but unstated in the spec; (2) 001 `spec.md`: Principle IV has no
requirement behind it (`ComparisonBasis` satisfies the constitution, not any FR); (3) extended by
hypervisor to 002 `contracts/match-store-port.md`, the four-probed-capabilities gap above, as the
same class of problem. **Blocked on the US3 agent releasing `specs/001-...`** — queued, not dropped.

**Long-horizon, explicitly NOT a current worry (owner):** the branch-identity fingerprint covers
turn, yields, units and cities, and omits fog of war, diplomatic state, AI internal state, RNG
state, and great-people/religion progress. Closed by T177 whenever branches start feeding real
comparisons. Do not spend effort here before then.

**Provider budget: the $80 OpenRouter cap is PRE-SPENT (2026-09-20).** The owner's words: "that
$80 is yours. Already written off by me it is for your experimentation and meant to be spent. If
we need more we'll talk then but don't ask for what's yours." Delivered via Claude remote, stored
in gitignored `secrets.yaml`, verified live (200, $80 remaining, paid tier). Consequences:
- **Never ask permission to spend within the cap** — asking is a violation of the ruling, not
  caution. Model-driven runs, provider probes, capability checks: just run them.
- Escalate to the owner only when the cap is *exhausted* and more is needed.
- The key exists in this session's transcript (owner knowingly accepted; cap is the mitigation).
  Rotation = swap the value in `secrets.yaml`, nothing else.
- Spend is still *observable*: accounting/telemetry record usage per run as designed, and
  `https://openrouter.ai/api/v1/auth/key` reports `usage`/`limit_remaining` without spending.

### Stale-spike hygiene (queued)

`spikes/load-path-linux.md`'s section *"Turns were advancing on their own — auto-end-turn is
enabled on this host"* is **retracted and wrong**. `UserOptions.txt`'s `AutoEndTurn 0` was already
correct; the real measured cause was `GameConfiguration.GetTurnTimerType()` returning
`TURNTIMER_STANDARD` in a single-player game, and `turn_timer_preflight` now refuses a run over it.
The spike still tells a reader to "settle auto-end-turn before any unattended run." **Annotate it**
— a stale spike that contradicts the code is exactly what burns a live client session. Verified by
the hypervisor before escalating, rather than raised as a false blocker.

Also standing from that spike: **never compare save bytes** in branch or replay verification.
Saves are not byte-stable across save -> load -> save; identical position, almost no shared bytes.

---

## Run 4 — 2026-09-20 (owner away, full machine autonomy)

Two agents were killed mid-edit by a **monthly spend limit**; both were resumed from transcript
rather than restarted, since they held the context. Tree was briefly RED (14 failures) and is now
**GREEN: 1449 passed, 2 skipped, 0 failed.** T233 landed; **T226 left honestly open** under the
standing instruction that a green tree outranks a complete feature.

### Reporting delegated

GitHub comms now belong to a **Scribe agent**, on the owner's instruction ("use an agent to report
to me, I don't want you wasting context on it"). Two channels:
- **Issue #1** — technical peer coordination with the Linux hypervisor. House style: `from:/re:/status:` header.
- **Issue #2** — owner-facing status. Created this run. Readable progress, decisions awaiting the
  owner, honest blockers.

The Scribe is instructed never to inflate and to verify counts and hashes against the repo before
publishing. **On its first run it corrected five errors in the hypervisor's own picture** — that
validated the delegation immediately and is the reason it is a standing role, not a one-shot.

### 🚨 Corrections to the hypervisor's picture (from the Scribe's first audit)

1. **Tree was GREEN, not red** — the hypervisor was working from a stale test run.
2. **002 counts were stale**: not 211/222 but **212 done / 21 open of 233**. Phase 11's convergence
   appended through T233. (001's 58/64 was correct.)
3. **🔴 Eight commits were never pushed.** Remote sat at `8c3d8f8`; local `HEAD` was `1b980e2`.
   Everything from Phase 9 forward — the entire composition root, Phases 10 and 11, and all four
   spec-001 user stories — existed **only on this machine**. The Linux peer merging the remote
   integration branch would have gotten a tree with none of it, and every evidence-image link would
   have 404ed. **Fixed: pushed.** *Lesson: commit discipline was tight, push discipline was absent.
   On a two-machine loop, an unpushed commit is an uncommitted one.*
4. **🔴 No OpenRouter key on this machine** — `civsim doctor`: `provider key : MISSING`. No real
   model decisions are possible today, and the owner is away.
5. Windows live-host status was **in flight, not landed** — no `r5-save-path-windows.md` or
   `r6-capture-hygiene-windows.md` exists yet. Windows `civsim doctor` still reads
   `tier UNSUPPORTED`, `capture path: none`.

### Hypervisor ruling: the missing API key does NOT block the demo

The owner's ask is that **the harness drives a real game** — tuner transport, capability catalog,
Lua execution via `implementation_ref`, observation sweeps, turn advancement, quicksaves. **None of
that needs an LLM.** The provider is an orthogonal axis, and proving it costs money we cannot spend
unattended anyway.

Directed approach, in preference order: (1) drive a real `Runner` against the **live client** with
`FakeModelProvider` injected via `build_runner_dependencies(provider=...)` — genuine transport,
catalog, dispatch and persistence, with scripted decisions standing in for model output; (2) if the
runner will not come up, connect `NexusClient` directly and execute real capabilities by hand.

**Binding on the write-up: state plainly that decisions were scripted and no live model call was
made.** A demo that lets a reader infer the agent played the game would be this loop's own defining
defect — a confident claim unbacked by reality — reproduced in a new medium.

### OBS

Owner volunteered OBS on both machines. Bring-up agent records the demo attempt, **starting before
the attempt** (a first attempt cannot be re-run; a captured failure is still evidence), producing a
GIF or key frames under `spikes/demo-evidence/` since GitHub will not play an mp4 from a raw URL.

**Distinction that must never be garbled:** OBS full-screen recording is a **human-facing artifact**
and is fine. The **harness** must still never take a root or full-screen grab, not even as a
fallback — Principle I, evidenced by the peer's `root-scoped-same-region-LEAKS.png`. A screen
recorder in the room is not a reason to relax capture hygiene.

### Linux peer's standing finding (highest-value structural idea so far)

Its reachability audit found **the entire synthetic input layer has no production caller** —
`InputEvent(` constructs zero times outside the port's own definition; `send_input` has no caller on
any platform. Three platform implementations, no entry point. Its own honest note: that morning's
three `send_input` fixes were, in production terms, **fixes to dead code**.

Consequence for the T217 ruling: **option B is not merely "build a UI driver" — it is "build a
driver AND wire the input layer beneath it."** That raises B's cost and makes option C worth more
than when the ruling was made.

**Peer's proposed rule, adopted:** *for anything crossing a process or protocol boundary, assert on
what the far side received, never on what our side returned.*

**Peer's proposed CI check, adopted and queued:** every `HostPlatform` port method and every
`Runner` collaborator must have at least one non-test caller, enforced mechanically. **That single
check would have caught seven distinct defects** found this loop: the input layer,
`capture_preconditions`, the fabricated V2 comparison, the dead Lua `return`s, both unwired
Principle I guards, the unredacted logger, and the unbuilt detection layer. Build it once the tree
is green and stays green — not into a red suite.

**Scribe's refinement (adopted into the check's design):** the string-literal doctor defect HAD a
caller — reachability alone would not catch the *fabricated-answer* variant of the family. The
full guard is two rules: reachability (a non-test caller exists) + negative controls (a test
proving each check can fail). Either alone leaves half the family alive.

---

## Run 5 — 2026-09-20

**SPEC 001 COMPLETE: 64/64** (`9c1e87b`, pushed). Deliverable 1 done. P6 freeze in force via
`panels/VERSION.lock` (load-bearing, twice-tested). Owner amendment landed; 002's port contract
deliberately untouched (amended by owner, not consumer). Two polish finds fixed in code: doctor's
string-literal coverage line (fabricated-answer family) and the unbounded turn route.

**Repair agent landed** (`fe5df53`, pushed): tree GREEN 1528/2/0 twice consecutively. T233 done
(killed client detected, revert-verified twice). T226 headless half (branch honesty **by
construction** — no defaults on comparability/host fields). T231/T232 done. T224 Windows GetDIBits
done. Bonus unfiled fix: `civsim doctor` crashed against a live client (unguarded NexusError paths
reachable only with a client running). It refused to flip the Windows R5 evidence flag on the
strength of an adapter comment — "a comment is not evidence" — correct call.

**🔑 T217 RESOLVED — merged from live/linux (`bf0b14a`, pushed).** `Network.LoadGame` is
**FrontEnd-only**; all three failed rounds called it from InGame where it returns `false` meaning
"not from here". Peer proved the precondition from Firaxis's own `automation_dailysmoketest.lua:241`
(`UI.IsInFrontEnd()` guard), loaded a save end-to-end from a one-shot tuner command, verified far-side
on a fresh connection. Working shape: `{Location=1, Type=1, Directory=0, Name=<save>}` → `true`.
**Option B dead — no bespoke driver, ever. Wayland concession never invoked. T177, branch load, all
resume-from unblocked.** Also merged: XComposite real pixels (evidence committed), XTest input fixed,
first live T218 result — **`major_count` reads wrong in-game** (first genuine V2 mismatch).

**In flight:** production SaveLoader build (T217 impl, headless, spike as spec); Windows bring-up
(r5/r6 raw evidence dirs appearing; spike .md files not yet written; demo attempt pending);
scribe publishing both items.

**Model note:** hypervisor now runs on Fable 5 (owner switched via /model mid-run).

### ⚠️ Open tension — resolve when the Windows bring-up agent reports

The uncommitted adapter comment says the Windows live verification ran against a **Steam** client
("build 1.0.12.68 (1023995), Steam"), and claims an options root under
`%LOCALAPPDATA%\Firaxis Games\`. Both sit oddly with the earlier direct finding that the **Epic**
profile (under `Documents\My Games\...(Epic)\`) is the configured one and the Steam Documents
profile is empty of options and mods.

Two candidate readings, NOT yet decided:
1. The agent enabled the tuner on the Steam profile (explicitly authorised) and drove the Steam
   install — the owner did say "playing steam". If so, no BBG → any demo run is **technical proof
   only, not charter-compliant**, and must be labelled so (instruction already given).
2. The Steam build reads its options from `%LOCALAPPDATA%\Firaxis Games\`, not Documents — which
   would mean the earlier "Steam profile has no options" conclusion was drawn from looking in the
   wrong root, and "Epic is the live one" was under-evidenced. The hypervisor's own finding may be
   the wrong one here.

**RESOLVED — reading 2 was right, and the hypervisor's Epic finding was WRONG.** Steam is the
live install (played 2026-09-16, build 1.0.12.68, **BBG 7.5.0 from steamapps/workshop** —
charter-compliant). Epic last played 2021-05-30; its BBG was never loaded. The Steam Documents
Mods folder looked empty because Workshop mods do not live there. Real T050 defect: Windows uses
TWO roots — saves under Documents, AppOptions.txt under %LOCALAPPDATA%\Firaxis Games\ — and the
preflights read a nonexistent file. Fixed. *Lesson: one-root evidence generalised to a two-root
platform; the correction came from an agent that checked last-played timestamps instead of
directory contents.*

---

## Run 5 (continued) — THE DEMO LANDED

**`eb6ade1` (pushed): the harness ended a real turn in a live game.** Steam client, BBG 7.5.0.
Launched the client, dismissed attract screen + leader modal via its own `send_input` (**the
input layer's first production caller** — the peer's dead layer is now alive), loaded an
autosave, 4 verified quicksaves, ended turn 8→9 with far-side assertion + game-log corroboration.
Window-scoped GIF at `spikes/demo-evidence/harness-ends-a-turn.gif`. **Scripted decisions, zero
model calls, labelled everywhere.** R5 windows: PASS (flag flipped on the spike file; probe test
now demands Windows cite its OWN spike; doctor: tier SUPPORTED). R6: PASS occlusion (0.0 matching
pixels, z-order-verified occluders) / FAIL in-frame chrome (FPS overlay) → capture stays
un-credited, honestly degraded. Machine changes recorded in spikes (steam_appid.txt critical —
without it Steam launches on the owner's laptop via Remote Play). 973MB desktop recording
deliberately uncommitted (shows whole desktop); GIF is the publishable artifact.

**🚨 THE FINDING THAT OUTRANKS THE DEMO — three defects in `nexus/client.py` mean the STOCK
harness cannot drive ANY client:** (1) `_handshake` never reads the `APP:` reply → `connect()`
always fails; (2) `_query_states` crashes on interleaved tag −1 frames; (3) `_await_result`
listens on tag 3, but the real client returns tag-3 EMPTY and prints results on tag −1 with an
`O\0<State>: ` prefix → every command times out while its Lua runs. The Linux R5 spike used a RAW
client, so this path was never exercised live on any platform: **1539 green tests and the core
transport did not work — the fake speaks the protocol the code expects, not the one the client
speaks.** Demo ran on session-local patches NOT in the tree. Proper fix in flight (fix client +
correct the fake to the REAL protocol + regression tests, evidence: `spikes/r5-raw-windows/` and
the peer's `t217-evidence/run_lua.py`; nexus-protocol.md to be updated). Linux peer asked to
re-run the raw probe to rule out a platform split.

**`9cbc2fa` (pushed): T217 production SaveLoader landed.** Spike-exact transcription,
revert-verified, branch + resume-from reach it cleanly. 1539/2/0. Live checklist ships in the
commit. Windows blockers noted: Steam relaunches every few minutes (LogonFailure, 8× today) —
no unattended Windows runs until it settles; DX12 exe exits instantly (DX11 only).

**In flight:** nexus transport fix (the critical one); scribe publishing the demo.

---

## Run 6 - 2026-09-20 evening (usage restored; repo PUBLIC; key provisioned)

**Landed and pushed (remote head `f45a5ed`):** `97180f5` T234 nexus transport fix - all three
client defects fixed, each revert-confirmed against its exact live symptom; fake corrected to the
REAL protocol with a raw-socket wire audit; `civsim doctor` handshakes live (GameCore_Tuner=10,
InGame=132). `f45a5ed` provider-test hermeticity (delenv fell through to the real repo-root
secrets.yaml; now CIVSIM_SECRETS_FILE points at an absent tmp file). Suite: **1547/2/0**.

**OpenRouter key provisioned** via Claude remote, gitignored secrets.yaml, verified live
(200, spend-capped, paid tier). Owner ruling recorded above: the cap is PRE-SPENT, never ask.

**In flight (three agents):** (1) live-run operator - FIRST model-driven run, bounded 3-5 turns,
evidence to spikes/demo-evidence/model-driven-*, no code edits allowed; (2) reachability CI +
negative controls - tests/contract/test_reachability.py + Phase 11 ledger entry, allowlist seeded
from T229-T234, UNRESOLVED findings come back for hypervisor review; (3) scribe posted the
bundled update (issue #2 owner comment 5752698028, issue #1 peer comment 5752699811; GIF
confirmed rendering publicly).

**STANDING OBLIGATION - Steam account handoff:** the Linux peer is BLOCKED on account contention
(their launch resolves to Remote Play from this machine). Scribe posted a hold request: peer
holds all launches until the Windows side posts an **ACCOUNT FREE** comment on issue #1. OWED:
when the model-driven run completes, shut the Windows client down cleanly, then have the scribe
post ACCOUNT FREE. Peer's next uses: raw_command_probe.py re-run (the empty-tag-3 universality
gap in nexus-protocol.md) + SaveLoader live checklist + T218/T213. A Linux login mid-run would
kick the Windows client and gap the run (Principle III).

**Peer hazard relayed:** X11 `import -window` without a timeout froze their desktop 80 min when
the window closed mid-grab. Correctness argument for the harness capture_window() path; the
harness never shells out to display-server grab tools.

**T235 landed (reachability enforcement + negative controls), 1568/2/0.** Hypervisor review of
its three UNRESOLVED findings, ruled 2026-09-20:
- `send_input` + `InputEvent` (host port, all three adapters, zero src/ callers): **retained
  dormant.** The planned consumer was T217 option B (bespoke UI driver), which died when C
  resolved (Network.LoadGame from FrontEnd). The owner-authorized fallback class - UI driving
  for operations the tuner cannot perform - remains plausible future work; the allowlist keeps
  the surface visible instead of silently dead. THE NEXT CONSUMER MUST DELETE THE ALLOWLIST
  ENTRIES. Do not build consumers to launder the entry away.
- `capture_preconditions` (linux adapter, callers only live tests + spikes): **suspected REAL
  wiring gap on the Linux side** - hygiene preconditions that run only in tests are exactly the
  pattern. Referred to the Linux peer (their adapter, their live evidence). QUEUED for the next
  issue #1 post (rides with ACCOUNT FREE).

### Run 6 results: first model-driven run attempt - BLOCKED at bring-up (host, not harness)

**Zero turns, zero model calls, $0 spent (auth/key: 80 remaining).** Steam shut itself down ~7s
after the tuner opened, on BOTH windows (bootstrap Shutdown 17:21:01 and 17:32:40 local); game
never left IntroScreen. Agent honored the single-relaunch rule and stopped. Evidence:
spikes/demo-evidence/model-driven-run-2026-09-20.md + two bringup JSON logs (uncommitted... commit
them with the doctor fix).

**CONTENTION HYPOTHESIS (posted to peer for correlation):** the Windows "Steam relaunch loop /
LogonFailure" and the peer's account contention may be ONE problem - two machines fighting over
one Steam logon. Peer asked to correlate their launch attempts against 17:21:01/17:32:40. If it
matches, the fix is pure scheduling: one side owns the account at a time.

**DECISION: account handed to the peer (ACCOUNT FREE posted).** Windows client cannot stay up
anyway; peer has raw_command_probe.py + SaveLoader checklist + T218/T213 queued. WINDOWS STANDS
DOWN FROM STEAM until the peer posts the account back. Windows continues headless.

**What the attempt proved live anyway:** composition root loads clean (catalog 53/0), provider
layer fully healthy through the production resolver - preflight_chain confirms sonnet-5 and
opus-5 (images, 1M ctx). The one unreachable step was nexus connect against a dead client.

**Findings:** A - doctor false-negative on file-only key -> FIXED as T236 (revert-confirmed).
B - no cold-client-to-turn-1 path -> FILED as T237 (DEFERRED-LIVE; the old T217-B UI-driver
authorization is SPENT, a new one needs an owner ruling). C - V2 getters would fail vs an
arbitrary loaded save (major_count 16-vs-majors, map_type "Continents.lua", RANDOM_SEED vs
GAME_SYNC_RANDOM_SEED) - corroborates T218, relayed to the peer who owns it. Suite: 1569/2/0.

### Owner-authorized spec amendment: EXECUTED (4ef22f6)

Items 1+2 (001 FR-021/Principle III fail-closed statement; FR-037 behind ComparisonBasis) were
found ALREADY LANDED in 9c1e87b - verified against code truth this pass, not re-amended. Item 3
landed now: match-store-port.md "Capability extensions" E1-E5 (four probed reads binding on
deliverable 3, no stub exposure, honest degradation, capture-withheld => None, run-id keying) +
get_turn_cycle flag-off = most recent attempt, previously implementation-defined. The ruling's
scope is now fully discharged.

**Deferred findings from the amendment pass:**
- get_run_configuration MIS-KEYING in 001's consumer (reads.py passes config_id where the
  published read takes run_id - wrong-configuration risk on collision) + stale port.py prose +
  READ_OPERATIONS omission + fixture stricter than contract -> FIX AGENT LAUNCHED (001 lane).
- Two further C1-family candidates if the contract grows (NOT amended, outside scope): no
  latest-turn/turn-count read (highest_recorded_turn forward-probes), no metric-series read
  (yields_by_turn is O(turns)). Parked - compositions over published reads are legal today.

### Convergence audit (read-only, at 60eb482): 001 CONVERGED (65/65); 002 NOT CONVERGED

Phase 12 appended and pushed (28dc5fe): T238 CRITICAL (decision loop never attaches images;
shown_to_agent can record that it did - falsifiable record), T239 (completeness served as
constructor constant), T240 (comparability never downgraded - docstring claims it is), T241
(abandon_branch zero production callers; T226's "only live remains" line is FALSE), T242 (V2
fallback vacuous incl. victory_types; stale mod_set justification), T243 (V11 footprint preflight
uncalled), T244 (E5 conformance untested), T245 (dead assembly-failure helper, T231 shape).
Audit verified all spot-checked Phase 10/11 claims TRUE in code; civsim_web has zero dead
surfaces; Principle V legally dormant. Parked: DEFAULT_WORST_CASE_CONTEXT_TOKENS placeholder;
T050/T224 Windows WGC halves are headless-progressable on this host (next wave candidate).

**Phase 12 wave in flight (3 agents, disjoint lanes, ledger writes reserved to hypervisor):**
A = T238+T240 (decision_loop/context/capture); B = T239+T241+T242+T243 (composition/runner/
store/operator); C = T244+T245+allowlist-citation refresh (conformance tests/assemble/recovery/
reachability). Baseline 1572/2/0 at 28dc5fe. Agents report LANDED paragraphs; hypervisor appends,
commits per lane, pushes.

### Peer live session results (issue #1, evening): contention CONFIRMED, protocol closed, 2 live bugs

- **CONTENTION CONFIRMED = the "Steam relaunch loop" RECLASSIFIED.** Peer's client ran
  continuously 17:14:38-17:35+, spanning BOTH Windows shutdowns (17:21:01, 17:32:40). One root
  cause: contested logon. Unattended Windows runs are UNBLOCKED whenever the Windows side owns
  the account under the handoff protocol. The 8x/day "LogonFailure" mystery is closed.
- **Tag-3 empty is UNIVERSAL** (1.0.12.9 matches 1.0.12.68). Contract caveat removed. "T234 is
  not a Windows workaround - it is the protocol."
- **End-turn crash caution walked back** by the peer (did not reproduce; suspicion moved to blind
  Return/space presses). Not a blocker.
- **Two live bugs in shipped Windows-owned code:** LuaSaveLoader never dismisses the leader-intro
  screen (fails every real load; strands the client) -> T248; capture_preconditions needs a real
  port preflight seam (peer confirmed gap, ruled stays UNRESOLVED) -> T249.
- **Start-new-game breakthrough:** CivSim DEFAULT preset LOADS FROM LUA (Network.LoadGame with
  FileType=GAME_CONFIGURATION, no UI) - also programmatically confirms TURNTIMER_NONE, closing
  the old turn-timer blocker for good. Remaining gap: no human player slot assigned (why
  HostGame silently no-ops). Network.HostGame appears nowhere in src/ (quantifies T237).
- Filed: T246 (2s post-close connection-refusal tail vs reconnect), T247 (zombie tuner invisible
  to process/window liveness; bounded-pass rule may eat the heartbeat signal), T248-T250 above.

**HYPERVISOR RULINGS (2026-09-20 evening):**
1. **Demo records with LANDED code only.** Neither a staged failure nor unlanded patches. Path:
   peer publishes the Escape-retry patch -> Windows transcribes into production LuaSaveLoader
   (T248) -> peer re-verifies live -> demo.
2. **T248 falls within the original T217-B grant** (the one authorized operation, "load a named
   save", is not complete while the intro screen holds the port closed). Input use bounded to
   dismissing that screen during that operation. T235 tripwire honored: wiring the consumer
   deletes the send_input/InputEvent allowlist entries.
3. **Peer's human-slot-assignment probe: GREENLIT.** Discovery on the live client is theirs;
   integration into preparation/composition stays Windows-side (T237 lane).
4. **Priority among the new work:** T248 first (blocks every real load, hence demo, branching,
   recovery), T249 second. T250 sequenced behind the in-flight T239-T243 lane (preparation.py).

### PHASE 12 COMPLETE (b5dcb51). Suite 1607/2/0; ruff + strict mypy clean; wave verified whole.

All eight audit findings landed, every fix revert-confirmed against its live symptom: T238 images
attach through the T134 chokepoint and shown_to_agent means attachment (R6 gate enforced via
Run.host_support_tier, fails closed); T239 completeness is the derivation at creation/persist/
read (first_owed_turn floors branches); T240 mid-run capture degradation downgrades comparability
via existing update_run; T241 civsim run abandon exists and BRANCH_ABANDONED is finally emitted
by production; T242 V2 fallback closed for the reference config + fallback-accepted fields
recorded on the preparing->playing event; T243 V11 refuses for free at preflight; T244 E5
conformance pinned (sqlite adapter passed untouched); T245 one assembly-failure builder in the
named home. 002 open: the DEFERRED-LIVE roster + T237 + T246-T250 (T248 blocked on the peer
publishing their Escape-retry patch; T250 now unblocked - the preparation.py lane landed).

NEXT HEADLESS CANDIDATES: T246 (reconnect tail), T247 (zombie tuner), T250 (in-game-only
getters), T249 seam half. T248 the moment the peer's patch appears. Then re-converge.

### Peer-findings wave COMPLETE. Suite 1637/2/0 verified whole by the hypervisor.

T246 (reconnect rides the post-close tail; first connect still fails fast), T247 (zombie tuner
detected via sustained heartbeat streak N=2, FR-014 respected, arithmetic asserted in-code),
T249 seam half (port preflight; peer's capture_preconditions is the wired Linux code; their
allowlist finding closed), T250 (in-game-only getters; major_count uses the spike's Players
derivation; resources recorded unobservable, not run-killing). Commits: 38903b4, cf0552c, +
the T246/T247 commit. Day total: 1539 -> 1637 (+98 tests), every fix revert-confirmed.

**002 remaining open:** DEFERRED-LIVE roster (T050-T202 live items, T213 extension, T218 Lua
shapes, T224 halves, T226 live half, T237) + T248 (BLOCKED on peer publishing the Escape-retry
patch) + T249 per-platform live halves. NOTHING headless-implementable remains unassigned.
Next converge pass after T248 lands. Peer still holds the Steam account.

## NIGHT HANDOFF - 2026-09-20 ~00:45 local. Windows host POWERING OFF; main work -> Linux hypervisor.

**Peer returned the account** (comment 5753920708) with a caveat worth keeping: their Steam+client
exited cleanly at 20:19:18 their-local after 2h10m of clean operation, unattributed, no crash dump
-- probably the owner (who is home), and the peer correctly notes it does NOT match the Windows
symptom's shape (7s-after-tuner vs 2h uptime). Not treated as corroboration of anything.

**Peer delivered everything:** T249 live-verified WITH negative control (bd1a1a2); T248 patch
published (44a2a00, measured FAIL 160.4s -> PASS 40.1s, retry-loop-in-_await_phase design);
human-slot probe DONE (bcb6d1a): **full programmatic game start works, pinned leader survives**
-- T237's live half is substantially answered; victory-progress parity evidence (a0999d0/266d28d):
the game's own World Rankings screen shows ALL alive majors' progress with unmet identities
anonymised via HasMet; LuaEvents auto-creates finding (7438659); spike-driven demo recording
(explicitly NOT the deliverable demo, honest README, every frame from capture_window()).

**RULINGS ISSUED (night handoff):**
1. **T248 -> Linux overnight** (patch author + client holder; Windows offline). Includes the
   flagged gap: **Option 1, add focus_window to the HostPlatform port** -- called before each
   press, same T217-B scope, reachability auto-guards it, T235 tripwire applies (send_input/
   InputEvent allowlist entries deleted with the consumer), Windows/macOS halves honest-minimal
   per T249 precedent. Option 2 rejected: fragile unattended runs, works-in-isolation class.
2. **Victory-progress parity: peer's redaction rule ACCEPTED** -- expose victory progress for all
   alive majors, anonymise identity of un-HasMet civs, exactly the asymmetry worldrankings.lua
   itself implements (progress shown, identity withheld). This IS Principle I: the human screen
   shows anonymous standings. The ban is lifted in favour of the redaction; implementation on
   whichever side touches the observation catalog next.
3. **Landed-code demo: GREENLIT on Linux** once T248 lands + test_production_save_loader.py
   re-verifies -- their capture-path recording discipline is proven (152 frames, window-scoped,
   zero model calls, honest labeling).
4. T251 filed (mod_set keys on Id not Name).

**MORNING CHECKLIST (Windows-me, read this first):** git fetch --all; expect T248 + focus_window
landed on live/linux (merge or already merged); read issue #1 from marker file forward
(.specify/memory/issue-watch-marker.txt); expect T248 re-verification transcript + possibly the
landed-code demo; Windows Steam may be used again ONLY per handoff protocol (check the board for
who holds the account); the model-driven run attempt is the standing next Windows live goal --
transport, images, record integrity, and provider are all verified ready; $80 budget untouched.

---

## LINUX OVERNIGHT — 2026-09-20 22:40 → (Linux hypervisor holds main work; Windows offline)

Read order honoured: issue #1 night handoff → this log → constitution. Both branches pulled
(`live/linux` `2f1a16d`, `002` `db16e41`, already merged). Steam up, account unambiguously Linux's.

**T248 LIVE-VERIFIED, 22:45 EDT.** `tests/live/test_production_save_loader.py` re-run UNMODIFIED
against the landed loader: ATTEMPT 1 (production loader as shipped, no external help) **PASS
38.4 s** — the line that read `FAILED after 160.4s` before the patch. ATTEMPT 2 PASS 34.6 s. Third
solo run read the loader's own counters back: `intro_dismiss_presses = 1`,
`intro_dismiss_skipped = None` — `focus_window` (EWMH `_NET_ACTIVE_WINDOW`, first production use)
returned `ok` live on X11/Cinnamon with a browser and terminal open on the same desktop; one Escape
landed; the retry loop stopped the instant the port answered. Recorded: spike doc "Re-verified",
`validation-results.md` T248 section, `tasks.md` T248 `[X]`. Windows/macOS `focus_window` halves
remain `unavailable` → Windows morning item, unchanged.

**Headless:** `uv sync --group civsim_web` was needed here first (jinja2 absent → the web
foundation test could not collect; environment, not code). Suite: **1635 passed / 8 skipped / 0 failed (every skip a Windows/macOS-only path; 6 new T251 tests)**.

**🔑 Provider key provisioned on Linux** (owner-supplied, gitignored `secrets.yaml`; doctor
`present`; `auth/key` limit 80 / remaining 80, paid). The cap is pre-spent per the standing ruling.
"No model-driven run possible on either host" is no longer true on this side.

**Finding → proposed T252 (composition.py, Windows lane):** `doctor` reports tier SUPPORTED and
`capture path : none` on the one host whose R6 spike PASSED, because
`run/composition.py:1240` calls `probe_host_support(...)` without `compositing_verified`, while
the T249 seam `check_capture_preconditions()` (Linux half checks `_NET_WM_CM_Sn`) is exactly that
verification and is already called per-capture in `observe/capture.py`. Until wired, the T238 R6
gate keeps images off the agent on Linux — a model-driven run here is text-only. Reported, not
fixed: outside the Linux lane and outside T248's grant.

**Suite hang FOUND and FIXED (tests/unit/test_nexus_client.py).** The full suite hung at 74% twice,
indefinitely, in `test_live_defect_1_handshake_consumes_the_app_reply_before_the_state_list` and
its two siblings: their fake handlers `return` on EOF without `writer.close()`, and from Python
3.12.1 `asyncio.Server.wait_closed()` waits for every accepted connection -- the server side sat in
CLOSE-WAIT forever (reproduced standalone with a task-stack dump; the faulthandler dump shows no
test frames because a suspended coroutine has none). One `writer.close()` in each handler. Not a
harness defect; it does mean the "1637 green" baseline was never runnable on 3.12.3 unmodified.

**FakeHostPlatform lacked `focus_window`** (2f1a16d added the port method, not the fake), so
`tests/fakes/test_fakes.py::test_fake_host_satisfies_the_host_platform_protocol` failed --
the 57 "targeted" tests last night did not include it. Added, with `set_focus_result` so a
scenario can prove the loader refuses to press at an unfocusable window.

**T251 LANDED, measured first (spike `t251-mod-set-identity-linux.md`).** Reading V2's own
snapshot off the live client before writing a demo configuration found that **`mod_set` could not
pass V2 on any modded host**: `GetActiveMods()` entries have no `Version` (the getter printed the
string "nil" ×22), ids are mixed-case in load order, and official content has no version at all.
Landed: version via `Modding.GetModProperty(m.Handle, "Version")` (integer handle -- the id-string
form errors), ids lower-cased, `canonical_mod_set` (sorted, case-folded) on both sides,
`ModRef.version: str | None` (a pin on id only is the only honest pin for DLC),
`reconcile_mod_set_versions` recording each unreportable pinned version as
`mod_set[<id>].version` in `v2_unobservable_fields` (never silently accepted, never fabricated),
V3/branch keys case-folded, seed set rewritten with the three measured workshop versions
(MPH 179, BBM 1.39.5, BBG 70500) and `null` for the 19 official packs, schemas regenerated.
Also observed, not chased: `civsim_resolve(..., "TurnTimerTypes", "TurnTimerType")` returns nil
for hash -1525060181 (= TURNTIMER_NONE per the seed set), so the turn-timer preflight records
UNVERIFIED on this build rather than the verified name. Does not refuse; the "VERIFIED" note on
`_TURN_TIMER_NAME_LUA` overstates what a snapshot shows.

**Pre-existing on Linux, not touched:** `mypy --strict` reports `ctypes.WinDLL` twice in
`host/windows/adapter.py` (Windows-only attribute); ruff reports import-order/line-length in
older `tests/live/*` spike scripts.

**Owner (mid-session, going to bed):** finish 002 with docs, document 001 and make it visible,
then move into deliverable 3; at each stage let a model-driven agent actually play ("step 2.5 /
3.5"); delegate. Provider key is on this host now, so the model-driven run is the next live item
after the landed-code demo.

### Linux overnight, second half (00:10 -> 01:30): the demo lands, then the model plays

**Landed-code demo COMPLETED** (`run-9505f323`, `demo-evidence-linux-landed/`): production
composition root end to end -- V10/V2/V3, identity lock, 3 turn cycles, 3 verified quicksaves,
14-declaration sweeps, far-side end turns (game 3 -> 5), 12 frames via `capture_window()`, zero
model calls, labelled. Seven attempts to get there, each stopped by something a fake never said:
build version unreadable on Linux (`UI.GetAppVersion()` is the accessor), `mod_set` un-passable
(T251), 5/14 observation bodies erroring (T213 -- `PlayersVisibility`, InGame-only unit API,
screen_state aggregate never wired), a Lua error costing a 30 s timeout (client now fails fast on
tag-3 `ERR:`), the quicksave verified before the file landed (bounded appearance wait), a stale
run-identity lock after a killed runner. All in `t213-observation-bodies-linux.md`.

**First model-driven runs** (`first-model-driven-runs-linux.md`, $1.13 of the $80): Claude Sonnet
5 decided correctly from the first call ("turn 5, no capital, found the city") and could not
reach the game for three reasons invisible to 1,648 green tests: the backstop's end-turn readback
was too early (client confirms after AI turns); `unit.is_selected` was a predicate field nothing
produced -- every unit/city action structurally unavailable since the catalog was written; and
README section 4's "selected unit" rule was documented, never implemented. Also: the model was
never shown the action catalog (DecisionRequest = role + observation). Fixed in `9f49292` and the
commit after it. **Attempt 4: `units.found_city {"target": 65536}` applied -- Pasargadae
founded**, read back on a fresh connection. Text-only (no image can reach the agent here: T252 +
no camera look-at getter on this build), stated on every artifact.

**001**: 27 screenshots + README + a self-contained gallery page, published as a claude.ai
artifact (`https://claude.ai/artifact/97njw182HGf2XUqUXSe8wo`, private). One real UI defect
found and fixed (stale marker CSS); three cosmetic ones recorded.

**Windows morning items (all reported on issue #1):** T252 (feed `check_capture_preconditions`
into `probe_host_support`; PNG encoding for Linux captures); Windows/macOS `focus_window` halves;
`civsim_resolve` TurnTimerTypes -> nil; `model_calls` table empty (records live in the step
bundle); no camera look-at getter -- captures withheld on Linux; `cities.state` is
GameCore_Tuner so `city.is_selected` cannot be produced there yet (`UI.GetHeadSelectedCity` is
InGame); deliverable 3 (match store) has no spec yet -- the owner asked for it next.

**Deliverable 3 specced** (owner's instruction: move into 003 after 002/001):
`specs/003-match-tracking-store/spec.md` via `/speckit-specify` -- five prioritised stories (turn on
the record before the game moves on; one published contract with the four formerly-probed reads +
model calls as rows; trends only from gap-free records by the store's own rule; export/import
bundles + migration from the 2026-09-21 file; archival never touches the record), 29 FRs, 9
measurable SCs, quality checklist passed, no clarification markers. Next: `/speckit-plan` (bundle
format directory-vs-archive is the one thing worth asking the owner). Not implemented tonight.

---

## LINUX SPEC-LOOP HYPERVISOR — 2026-09-21 08:10 EDT → (owner-directed; Windows offline)

Owner's instruction (08:07 EDT): orchestrate TASKS.md completion across all specs via the speckit
loop (analyze → implement → converge), executive-only, subagents do the work. Start state:
001 65/65 [X]; 002 232/251 (19 open, mostly live/other-platform); 003 spec only. Civ6 down, tuner
port closed, locks clear, Steam up. **Client not launched — owner's OK required for the account.**
pwsh absent: speckit helper JSON hand-derived. Three lanes on `live/linux`, path-scoped commits:
A = 003 plan→tasks→implement (fork); B = 002 residual audit + converge, headless (fork);
C = 001 analyze + converge (Opus). Lanes append their own sections below via `cat >>` only.

### Lane C — 001 convergence check (2026-09-21)

**Verdict: NOT converged on arrival; converged now.** analyze found 0 CRITICAL / 0 HIGH against the
artifacts (37 FRs, all with code; 65/65 tasks `[X]`); converge found **9 gaps in the code**, appended
as Phase 9 (T066–T074), all implemented and `[X]`. Severity: 4 HIGH, 4 MEDIUM, 1 LOW. No constitution
MUST violated, no Principle I leak.
One shape ran through all nine: **four audits iterated less than their own docstrings claimed, and two
correct behaviours had no guard at all** — the third instance of the self-comparing-check defect after
002's `_read_setting` and `doctor`'s literal `0`. Every fix is revert-confirmed.
Two were real defects: **FR-030** — `/healthz` rendered a store's `ping()` exception verbatim (DSN and
all) on the unauthenticated LAN page, and its view models sat in `app.py`, outside *both* FR-030 audits;
**FR-036** — the turn route read 200 capture records to render 50 steps. Also: `ParityDeclaration` was
outside the SC-005 coverage scan entirely (167 → 179 fields), and `poll.js`'s four couplings to the live
page were asserted by nothing.
Suite 1773 passed / 9 skipped / 0 failed (baseline 1650/8 — other lanes added ~110 tests, Lane C ~13).
Commits: `9113b1c` (converge phase), `6562a33` (implementation). Both pushed.
Not fixed, reported only: `tests/contract/test_match_tracking_store.py` has 6 ruff errors (Lane A's file).

### Lane A — 003 match-tracking store (2026-09-21)

Plan → tasks → implement → converge in one attempt, headless, `live/linux`. Fork could not spawn
subagents (fork hard rule), so the lane coded directly. Design: the 002 reference adapter is
widened behind `MatchTrackingStore(MatchStore)` (`store/contract.py`); 002's port, the harness
call sites and `civsim_web` are untouched (FR-016). Store file schema 1.0 → **1.1** (additive;
`store/schema.py`), copy-first migration via SQLite backup API to `<db>.v1.0.bak-<UTC>`; model
calls become rows **inside** `write_turn_cycle`'s transaction (the 2026-09-21 file had 0 rows for
97 steps — the harness only ever wrote failed calls); completeness re-derived in every write that
can change it; `list_runs`/`get_capture_blob`/`get_turn_cycle_attempt` under the web's probed
names; paged `query_runs`; tagged `get_capture_image`; `model_call_totals`; `metric_series` +
`divergence` with the exclusion rule owned by the store (visually-degraded opt-in recorded on the
response — every run to date is degraded); `export_run`/`import_run` + `store/bundle.py` (dir
canonical, `.tar.gz` transport); `civsim store info|migrate|runs|model-calls|export|import`.
Measured: SC-001 **354** injected interruptions + one SIGKILL; SC-002 30k steps 3.3 s / 0.6 s;
SC-008 slowest listing 1.7 ms; SC-004 the five model-driven runs sum to **$1.424694** ≈ $1.42;
Scenario 6 on a copy of the real file: 10 runs, 97 calls derived, backup beside it. Suite
**1773 / 9 skipped / 0 failed** (whole tree). NOT run: Scenario 8 (model plays through the new
store — needs client + account, ~$0.30) and the Windows half of SC-006. Cross-spec follow-ups
reported to the hypervisor: 002 contract doc should point at 003; 002 records no per-turn yields
(`compute_yields` no-op, no `player.yields` declaration) so science/culture series are honestly
empty; 001 may retire its three probe Protocols. Owner-reviewable: bundle format, degraded opt-in,
schema numbering, ten-not-nine runs.

### Lane B — 002 residuals, headless (2026-09-21)

Linux node, no client, no Steam touched. Attempts: audit (0) → analyze+converge+implement (1) →
coordinator items + T256 + final converge (2). Ledger: 19 open / 232 done → **16 open / 244 done**.
Every remaining open task is other-platform (T050, T051, T099, T224), client-gated (T177,
T191–T194, T198–T201, T260), other-session (T257) or owner-adjacent+client-gated (T237); each is
annotated in tasks.md with what it needs, wall-clock and $. Headless scope converged.

Landed, each with a full green suite (final: 1816 passed / 11 skipped / 0 failed):
d88038f audit + T202 record; 27ed7d4 T252 (probe consults the T249 seam; raw frames → PNG);
76218f1 T253 (tech/civic + boost popups acknowledgeable; Lua executed in tests via `--with lupa`);
585c72e T254 (turn-timer forward DB.MakeHash fallback); 8bdb5e8 T255 (cities.selection overlay
→ city.is_selected); 61d135d T258/T259 (player.yields from the top bar → TurnCycle.yields,
Constitution III; store port names its 003 implementer); 1b7e8ce T256 (target_kind rendered
per action). Catalog 2026.09.2 → 2026.09.3, 53 → 57 declarations, 24 → 26 capabilities.
Also: five raw NULs in tasks.md (Phase 12) replaced with `\x00` — they had made grep treat the
file as binary and hid T218/T224/T226/T237/T249 from the first audit; T218/T226/T249 closed on
existing evidence, T224/T237 annotated.

UNVERIFIED LIVE (all client-gated, all said so in code): `UIManager:DequeuePopup` from InGame,
`UI.GetHeadSelectedCity()`, the top-bar accessors, the forward-hash match on -1525060181, and
whether the rendered target example ends the id-for-plot confusion. T260 carries the camera
look-at lead (`UI.GetMapLookAtWorldTarget` + `UI.GetPlotCoordFromWorld`). Owner-reviewable:
T256's alternative (`subject`/`target` split); T237's production wiring; T201's $ shape.

### Executive close-out — 2026-09-21 (Linux spec-loop hypervisor)

Loop result: every spec converged for everything observable on this host, one pass each
(A: 1 attempt, B: 2, C: 1). **001** 65 → 75 `[X]`, 0 open — converge found nine gaps behind a
green suite, including an unauthenticated `/healthz` rendering a store exception with its DSN, and
`/runs` missing SC-008 five-fold at 55 runs × 320 turns (10.7 s → 0.13 s, T075). **002** 232/251 →
244/260, 16 open and every one annotated with what it needs: other-platform (T050 T051 T099 T199
T224), client-gated (T177 T191–T194 T198 T200 T201 T260), owner-adjacent (T237 production
bring-up wiring), other-session (T257 Wayland portal). **003** 0 → 46/46: `MatchTrackingStore`
extends 002's port unchanged; file schema 1.0 → 1.1 with a copy-first backup; model calls as rows
in the turn-cycle transaction; trends with the store-owned exclusion rule; directory bundle +
`.tar.gz` transport; `civsim store info|migrate|runs|model-calls|export|import`. Cross-spec landed:
T258 `player.yields` from the top bar (Principle III — series were empty for every run), T259
points 002's port contract at 003. Integrated head `a1e73b8`: suite **1816 passed / 11 skipped / 0 failed**. 13 commits,
all pushed to `origin/live/linux`. Lane C's section above predates T075.
Client never launched (owner's OK pending), Steam untouched, $0 model spend today.
Owner-reviewable: bundle format; strict trend exclusion with `include_visually_degraded` opt-in
(every run to date is degraded); T256 `target_kind`/`target_hint` vs a `subject`/`target` split;
T201 soak shape ($0 fake vs ≈ $40–45 model-driven); T237 wiring.
Next live items, in order: T253 popup acknowledge on the exact popup the client sat on; 003
quickstart Scenario 8 (three model turns ≈ $0.30); T177/T191–T194/T198/T200 (5–15 min each,
T200 ≈ 2 h); T201 last (5–12 h exclusive client).

### Owner rulings, 2026-09-21 ~10:30 EDT — launch OK; spend cap self-managed for the week

Owner: "Launch ok." The client may be launched on this host this session. And: "the entire spend
limit is tied to this week and entirely yours to self manage. You don't have to ask me about
spending any amount of money — if you use past my limit OpenRouter will refuse you, is all." So:
no per-spend approval; the provider's hard limit is the only stop; spend is planned by the
hypervisor (cheap models for soaks and batches, Sonnet-class where play is the evidence) and
recorded per run in the store. Live queue starts now in stages, one client owner at a time:
S1 bring-up + T253 live + Phase 13 live claims + 003 Scenario 8 + T260 look-at probe;
S2 T177/T191–T194/T198; S3 T200; S4 T201 soak (driver prepared headless in parallel).

### Live S1 — 2026-09-21 (11:14 → 12:48 EDT) — the harness plays, on the record, on issue #3

Owner's redirect at 11:20: visible gameplay, breadth, honesty, entries every 30–45 min. Eleven
blocks (7 Sonnet 5, 4 stochastic), game turn 17 → 42, three entries on issue #3, 13 commits.
Actions demonstrated live 4 → 8 of 39 (popup acknowledge, move, end turn, research, select city,
select unit, named save); attempted 5 → 22. Observations 16/16; screens encountered 2 → 3; views
0 → 1; images delivered 0 → 7 of ~260 steps (T260 live, block 9: screened_clean + shown +
image_count 1 at every step, tier validated, xcomposite). Fixed from live measurement: strict
schema forcing `parameters: {}` (86a92db); bounded verification re-read for every order, research
via PlayerOperations.RESEARCH, units/cities.select (47dcfba); a dispatcher-refused end turn no
longer ends the turn (dc67529); a provider-claimed prompt_type under a proactive trigger no
longer crashes the run (2139479); operator_intervention run events + schema (25282d1, b99f5d6).
Operator scripting, labelled and not counted: Australia's first-meeting greeting cleared through
the decline button's own call then Exit (AddResponse NEGATIVE, CloseSession), recorded on
run-221d541d. Open on exit: a second greeting on screen at game turn 42 (screens lane's mapping
reported it live; the sampler then derived a non-existent action id and the run paused instead of
refusing); every world/city frame withheld `non_player_ui` after provenance passes; five turn
cycles at game turn 35 recorded ended_by_agent on unconfirmed end turns (owner's ruling issued,
Stage 2's first item); the demo driver hangs on a paused run. Client left InGame, turn 42, tuner
free. Spend $2.95 of the week's cap. Ledger: spikes/gameplay-2026-09-21/README.md.

### Live S2 — 2026-09-21 (12:51 → 14:25 EDT)

Blocks 13–19, game turn 42 → 48 (block 18: Sonnet 5 saw the unmapped Classical Era card in 41 of 41 delivered frames and asked to acknowledge it 40 times while the text probe said `world` -- first live evidence a model uses the picture; EraReviewPopup mapped in 05766af), spend this stage $1.42 (blocks 13/15/17 $0). Route
blocker fixed first (c052c39: explicit `prompt.diplomatic_approach` → `prompts.ai_diplomatic_approach`
table; a blocking prompt with no registered answer is a recorded `not_in_catalog` stall, not a crash)
and verified live in block 13 — probe reports the greeting with both real statement texts, sampler
answers first. **Block 14: Sonnet 5 received an image at 8 of 8 steps** (screened_clean,
shown_to_agent, blob, image_count=1; tier validated, xcomposite) — first frames to a real model on
Linux; use of the picture not provable from its reasoning. **The click was a no-op**: `CallCallback`
appears nowhere in Firaxis' UI Lua; nothing reachable from InGame fires a control's callback. Replaced
by a real click: the Lua hands back the control's rect + `UIManager:GetScreenSizeVal()` (1024×768 on
a 1920×1200 window — the engine stretches UI space onto the window, scale 1.875 × 1.5625), the
executor clicks through the host input port. A manual click at the scaled spot answered the greeting
(operator intervention, recorded). Same path now serves every acknowledge-only popup (the midday
`close_control_callback` would have reported success with the popup still up). Gathering Storm's
eruption cinematic (`NaturalDisasterPopup`) blocked `Network.SaveGame` → blocks 16/17 paused in 12 s
on the turn-1 quicksave before any observation: mapped as `prompt.natural_disaster` /
`prompts.natural_disaster` (catalog 2026.09.6); **Stage 3 first: the turn-start quicksave must not
deadlock on a prompt that blocks saving.** Content gate: block 10's withholds were
`windows_capture_border` from the union profile; views now screen by platform (98c71bb);
`debug_overlay` corner heuristic still withholds busy-corner frames. Driver: paused = block over, lock
cleared. Coverage: actions 8 of 38 applied, images delivered 80 of 351 steps.
Commits: c052c39 98c71bb a58d4b0 56444cf 7b7eb04 e406ba3 05766af. Entry 4 posted on #3. Suite: 1989 passed / 9 skipped / 0 failed at 7b7eb04 (3:14 unloaded; a 12-minute contended run showed 10 wiring failures caused by a Lua comment that named the save call -- the test fake routes bodies by that substring -- reworded).
VERIFIED LIVE, block 19 (run-08566ab0, $0, 31 s): the era card acknowledged through the production executor's scaled click, then the turn ended by the agent and game-confirmed. Entries 4, 5, 6 on #3. Client on exit:
