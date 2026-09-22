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

### Live S3 — 2026-09-21 (14:24 → 16:10 EDT)

Deadlock fix landed `c795039`: a save-blocking prompt is answered through the ordinary decision
path *before* the turn-start quicksave (probe → prompt answer as a recorded step → quicksave →
rest of turn; save still precedes every non-prompt action; a save still refused pauses with the
error recorded). 3 new fake-driven tests, 2 existing adjusted; targeted run 15 passed; full suite
not completed on this head (background run truncated at ~40 % with 4 unnamed F marks — unverified
whether mine). **Block 20** (`run-5bd1a86b`, Sonnet 5, $1.27): game 49 → 53, 40/40 images to the
model, 0 applied — the Classical Era **dedication chooser** is an unmapped context; the model
read it from the frame alone and chose Free Inquiry ×40, refused each time (probe: `world`);
every turn ended on the backstop, each game-confirmed. `city.available_productions` is always
`[]` (body never fills it) so `cities.set_production` can never be available — recorded, not
fixed. No first meeting; deadlock fix not live-verified. **Stall:** 14:48 → 15:58 idle waiting
on background notifications that never arrived; caught by the hypervisor. Entry 7 on #3.
Coverage: actions 9/38 applied, 23 attempted; images 128/399 (32.1 %). Day spend $5.64.
Client on exit: InGame, game turn 53, dedication chooser up, tuner free, no lock, no runner.

### Executive close-out, live day — 2026-09-21 16:15 EDT

Owner redirect at 10:45: the soak is parked; the deliverable is visible gameplay, documented and
posted, breadth over quality. Result: issue #3 holds seven entries plus two scorecards; game turn
17 → 53 across 20 blocks (Sonnet 5 and the $0 stochastic sampler alternating); actions demonstrated
live 3 → 9 of 38, attempted 5 → 23, screens 2 → 6 of 16, images delivered 0 → 128 of 399 steps, all
from `civsim store coverage`, none by hand. Eighteen defects found by play and fixed on the branch
(list in the #3 day summary). The two findings that matter most: the strict response schema had
made every decision target-less since the catalog was written (`86a92db`), and in blocks 18 and 20
the model read two unmapped contexts (era card, dedication chooser) off the picture alone while the
text probe said `world`. Spend $5.64 of the cap. Three operator interventions, all recorded.
Orchestration failure, owned here: Stage 3 idled 14:48 → 15:58 waiting on background-task
notifications that never woke it (feedback drafted); the next stage polls a detached run's stdout
on a timer instead. Client left up at game turn 53 on the dedication chooser; Linux holds Steam.
Suite on head `5361afe`: **not green** — the full suite hangs at about 40 % on head `5361afe` (two independent runs, 14:39 and 16:06, both stuck at the same point with four failure marks showing); the last green full run was 1989 passed / 9 skipped / 0 failed at `7b7eb04`, so the hang entered with `05766af` or `c795039`; diagnosis running, result to #1.
Next: full-suite gate; map the dedication chooser; fill `city.available_productions`; verify the
greeting click and the pre-save clearance live; T177/T191–T194/T198/T200 remain client-gated.

### Live S4 — 2026-09-21 (18:00–20:05 EDT)

Blocks 21–25 and goal runs 01–05 from the detached worktree; entries 8, 9, 10 on #3. Game turn 54 →
59 (defeat) → reload 42 → 53. New live actions: `prompts.era_transition`, `prompts.era_dedication`,
`prompts.natural_disaster` → **11 of 41** applied (from 9); images 277 of 592 steps. First game-over:
Persia taken by Georgia at t59, recorded as SaveVerificationError (no game-over detection; fixed
headless f31fb5b, unverified live). First defeat-to-reload recovery, ~17 min: post-defeat MainMenu
refuses Network.LoadGame (3 saves); the exit modal ignores synthetic input (xdotool, harness XTest);
killed at the empty menu, relaunched, fresh menu loaded t42 in 38.0 s. Production: the LIST fix
verified live (8 options); the SET never lands — 9× then 80× rejected "verification false" even after
59af4a2, while a direct API probe had CanStartOperation=true and RequestOperation accepted → next
suspect is head-selection / InGame state for the BUILD op. First-meeting greeting statement answer
fails verification on the reloaded board (48/48 across three goals) — distinct from the Goodbye/Exit
case. Congress: `WorldCongressIntro` unmapped (block 21); the session does not block end turn.
Operator interventions all recorded, none counted. End save `civsim-gameplay-2026-09-21-end`
(t53, size-stable). Client left InGame t53, city panel open, tuner free, no lock. Lessons: keep every
foreground command < 600 s (my own poll loop backgrounded twice); goal driver needs secrets in the
worktree; `git add` with one missing path stages nothing.

### Live S5 — 2026-09-21 (20:08–20:30 EDT)

One question answered: `cities.set_production`'s 80/80 rejection was NOT verification lag. Under
the bounded re-read (14f7418) it still failed 24/24 with `last_read production_queue []` after
4.1 s; a labelled operator probe issuing the exact `{PARAM_UNIT_TYPE=hash, PARAM_INSERT_MODE=
VALUE_EXCLUSIVE}` table filled the queue within 1 s (UNIT_BUILDER, stable at +10 s), and the
harness's own `cities.state` body then read `["UNIT_BUILDER"]`. Cause at the source: the executor
passes only the target, `CivSim_CityOrders_SetProduction(cityId, productionType)` gets the type
name as `cityId` (city_orders.lua:213–217, no lone-argument guard unlike unit_orders.lua:107),
answers `city_not_found`, and the executor drops the dispatch answer before verifying. Same class
as the promote bug (6606d4b). Fix for tomorrow: normalise the lone argument (selected city + target)
and record the Lua's answer on the step. The harness has still never built anything by its own
action. End save `civsim-gameplay-2026-09-21-end2` (t56, Persia alive); client InGame, no prompt,
no lock. Spend $0.82. Entry 11 on #3.

### Executive close-out, evening — 2026-09-21 20:35 EDT (Steam released 20:30)

Owner directives 17:45–18:30: reachable-state sampling only; directed goal runs ("build a builder
→ use a builder"); increase the run rate; compact the loop state in the repo; Steam back by 21:00;
monitor rigorously. Result on issue #3: entries 8–11 plus the audit finding. Game turn 54 → 59
(Persia defeated at t59 — the first observed game over) → labelled reload to 42 → 56. Coverage
3 → **11 of 41** actions applied live for the day, 18 attempted while available, images delivered
287 of 619 steps, 43 runs, store spend $13.40 cumulative (≈ $12 today).
Landed headless from live findings: availability rendering + `--provider-policy coverage`; goal driver
+ 13 goals; scorecard honesty; suite hang (unbounded replay loop) + pytest timeouts; production
list (verified live); `units.build_improvement`; `units.promote` (never could fire); game-over
detection; dedication/congress/first-meeting mappings (era card, dedication, eruption verified
live); bounded verification re-read with `last_read`; **accessor audit — 28 phantom methods, 10
permanently-empty fields, 14 of 38 actions structurally impossible** (`a606729`), bodies repaired
(`e076f83`, `03efcac`), allowlist + CI test landing. Root cause of `cities.set_production` pinned
live by Stage 5: the executor passes only the target and the city-orders Lua takes it as the city
id; the dispatch answer was discarded → fix + dispatch-answer recording in flight.
Process: live runs now execute from a detached worktree; a 15-min cron self-check; every agent
forbidden to wait on background notifications (one more fork stall caught within 10 min). Loader
follow-ups: post-defeat menu refuses Lua loads; exit modal ignores synthetic input (17-min recovery).
Autoplay still blocked on the owner's permission. Loop state: `.specify/memory/loop-state.md`.

### Live S7 — 2026-09-21 21:09–21:30 EDT (no play: client crashed in S6, account in use elsewhere)

Diagnosed S6's abort from primary sources: kernel `segfault … in libGameCore_XP2.so` at 20:46:35 +
core dump, Steam removed the game's processes at 20:46:45 — the client crashed during S6's turn 1,
the first live sweep on 454b2f8 (audit-repaired bodies, game_over.lua). Harness recorded only a
detection-pass `ConnectionResetError` and `stop_reason None` (finding: a death during the probe/
sweep is not named). Relaunch ×3 spawned nothing: Steam's dialog says the owner is playing Slay the
Spire 2 on another computer and continuing would disconnect him — clicked Cancel, did not launch.
Client down; resume from `…-end2` (t56); bisect 454b2f8 vs 14f7418 before any chain run. $0 spent.

### Convergence re-check 001/003 — 2026-09-22 (attempt 2, both specs, NOT CONVERGED on arrival)

Both were reported CONVERGED on 2026-09-21 (001: 75/75, 003: 46/46). This was an honest
re-check and both came back **NOT CONVERGED**: nine findings, two HIGH, all now closed.
Reports: `specs/001-unified-web-interface/analyze-2026-09-22.md`,
`specs/003-match-tracking-store/analyze-2026-09-22.md`. Commits `54d89aa`, `9597bbd`,
`9120fce`, `3beea75`.

**What was verified rather than trusted.** 001's ten Phase 9 fixes were re-checked against the
code, not against `tasks.md`'s claims. All ten landed with real code and real tests. Nothing was
a claim without a code path. So none of what follows is last pass's work undone.

**Where last pass's shape went.** 2026-09-21 named it: *a check written from the same mental
model as the code inherits that model's blind spot*. Re-running that lens would mostly re-find
its own fixes, so this pass audited where the class migrates to once checks are widened —
artifacts that assert a number the code computes, in-code prose that instructs a future
contributor, and statements made before the other deliverable landed. All three produced
findings, and two bigger things turned up that fit neither.

**The two that matter.**

1. **003 FR-006 had no check of any kind (T047).** "The store MUST expose no operation that
   deletes or edits a turn, step, capture, event or model call" was enforced by omission. The
   surface really was clean, but nothing asserted it and `FR-006` appeared nowhere in the feature.
   `delete_turn_cycle()` added tomorrow would have left the whole suite green while breaking the
   immutability floor Principle III rests on. `MUTATING_OPERATIONS` now publishes the closed
   surface as data, with a partition over the Protocol *and* the concrete adapter (MRO-walking,
   read set pinned literally so the partition is not a tautology) plus a delete/edit vocabulary
   scan. Revert-confirmed. The fix pattern had existed one package away the whole time, built for
   `civsim_web`'s client rather than for the store that holds the records.

2. **003's SC-005 test had stopped testing (T053).**
   `test_the_real_pre_feature_file_migrates_and_reads_back` read `civsim-match-store.db` and
   called `pytest.skip("already at 1.1; nothing to migrate")`. Ordinary use of this host migrated
   that file on 2026-09-21 — it now holds 44 runs — so from that moment the test skipped at
   runtime **on the only machine that has the file**, and `assert len(before) == 10`, SC-005's
   entire claim, was dead code. Nothing was red; a skip reads as "not applicable here". This is
   the **fourth** check this project has found that was not checking — after `_read_setting`,
   `doctor`'s hard-coded `0`, and the four narrow audits — and the first that was not merely
   narrow but wholly inert. It is also a **distinct failure mode** from the three before it: those
   were written wrong, this one was written right and *aged out*. Worth naming: **a fixture the
   system under test keeps writing to is not a fixture.** Repointed at the frozen
   `…v1.0.bak-20260921T152108Z` snapshot; the skip is now an assertion.

**And repointing it immediately caught a live interaction nobody had seen.** The snapshot holds
`run-54a3cefb…`, left `preparing` with no identity lock. A default write-mode open runs the
open-time orphan sweep (`9f200d5`, the 002 lane's work in 003's files) and correctly pauses it. So
**SC-005's and V3's "every run reads back unchanged" is true of the migration and false of a
default open**, and nobody knew because the test had been skipping since the day the sweep landed.
Resolved by separating the claims, not relaxing either: migration fidelity with
`orphan_sweep=False`, and **W6 now asserted against a real store file** — the sweep pauses exactly
the lockless run and moves no other run's lifecycle state. `test_orphans.py` builds fixtures that
*are* orphans; this file merely *is* one, which is the stronger evidence and the first of its kind.

**The rest.** 003: the contract's Operations block was two operations short of its own Protocol
(`list_captures`, `trend_exclusion` — and T1 already cited the latter in prose while the block did
not declare it); T1's "research R14" citation named an item about scale checks, and the wrong
citation had spread to six further places, all now R6; W6 documents the orphan sweep; the CLI
ships eight commands where six were documented; SC-004's $1.424694 is asserted rather than only
recorded; FR-018's six named yields are exercised. 001: `quickstart.md` pinned `167/91/49` where
`doctor` prints `180/103/50`, and the fix deliberately does **not** pin the number — four of those
five counts are over 002's data model, and putting a cross-deliverable count in 001's CI is how a
guard becomes something contributors edit to make green; `port.py`'s three probe docstrings still
ordered a contributor to delete Protocols that Phase 9 had decided to keep.

**One finding was produced by a fix (001 T078).** T077's new docstrings say "the probe is the
graceful-degradation path, and here is the test that holds it". True of two probes; false of
`CaptureBlobReader`, whose `503` was asserted **absent** and never asserted to fire, so it could
have been deleted or replaced with a placeholder image with the suite green. Writing down a
guarantee forces the question of whether it is held — the cheapest audit of the week.

**Suite.** Baseline at `c7b5654`: 2138 passed / 19 skipped / 0 failed (203.89 s). Final at the
tree these commits contain: **2147 passed / 18 skipped / 0 failed** (205.27 s) — +9 assertions and
−1 skip, which is exactly this pass: nine new checks, and SC-005's test running instead of
skipping. Only markdown changed after that run. `ruff` clean; `mypy --strict` clean over `store/`.
The `/compare` render-budget case passed in both runs. The real store file and its `.bak` snapshot
were only ever copied; mtimes verified unchanged. Every fix carries a revert confirmation that was
run and recorded.

**One ask, outside this lane's paths.** `src/civsim_harness/operator/store_cli.py`'s module
docstring says "Seven commands, all over the published contract" above a list of eight. Not edited
here; reported rather than taken unilaterally.

### Live S1 — 2026-09-22 (08:31–08:52 EDT) — the guard patch survives the board that killed the client

**Verified live: `882758e` does not crash.** Cold launch through Steam (no "logged in on another
computer" dialog — the account was free), tuner bound in 8 s, and the production `LuaSaveLoader`
loaded `civsim-gameplay-2026-09-21-end2` **from a fresh main menu in 40.9 s**. That last fact
corrects a standing belief: "only the UI path works" is too strong — `Network.LoadGame` works from a
*fresh* `MainMenu`; yesterday's three `returned false` results were all on a **reused** menu (post
exit-to-menu, post-defeat). Board read back at game turn 56, 1 city, 1 unit, Cyrus/Persia.

One crash-watched stochastic turn (`run-55e5bfeab48b41deb3329d6946f56ed8`, seed 22, policy coverage,
63.1 s) then ran the full 16-step observation sweep — the same sweep that segfaulted the client at
20:46:35 yesterday on the repaired `government.state` body. **No segfault.** The evidence is
positive, not merely absent: the kernel ring's last `Civ6 … segfault at b0 … libGameCore_XP2.so` is
still yesterday's, pid 1915400, with nothing after it and no entry for today's pid; `coredumpctl`
since 08:00 is empty; and the client **process never died** — the launch pid ran continuously
through the block and was still InGame at turn 56 afterwards. (`dmesg -T`'s date labels are
clock-skewed on this box and read "Sep 24"; the monotonic ring is the load-bearing anchor.)

**`dispatch_result` is live, and it earned its keep on the first block.** Every one of the 16 steps
carries the Lua's own answer, and it immediately separated three failures that all used to surface
as one `verification_failed`:

| action | `dispatch_result` | outcome | what it actually means |
|---|---|---|---|
| `camera.zoom` | `{"ok":true,"zoom":0.05,"mechanism":"UI.SetMapZoom"}` | rejected | the order landed; the **verifier** disagrees |
| `diplomacy.send_delegation` | `{"ok":true,"mechanism":"DiplomacyManager.RequestSession(…,\"DIPLOMATIC_DELEGATION\")","target_player_id":1}` | rejected | landed; `other_player.has_delegation` read false |
| `prompts.ai_diplomatic_approach` | `{"reason":"unknown_prompt","ok":false}` | rejected | **never dispatched — the mapping misses the prompt** |
| `turn.end_turn` ×8 | `null` | `unavailable_to_human_now` | honestly refused, never issued |

3 of 16 applied: `saves.save_game`, `research.set_tech` TECH_SHIPBUILDING (via
`PlayerOperations.RESEARCH`), `units.move_to` {46,37}.

**Two findings, both new.**

1. **The board is wedged on an unmapped prompt.** An AI leader's diplomatic-approach greeting came
   up at step 4 and stayed (screens: `world` ×3, then `prompt.diplomatic_approach` ×13). The only
   action that can clear it answers `unknown_prompt`, so `has_blocking_prompt` stays true, so all
   eight end-turn attempts were correctly refused and the backstop paused the run
   (`BackstopEndTurnNotConfirmed`). **No run on this board can advance a turn until the mapping is
   fixed** — which also means the guard patch is verified for exactly one turn's sweep, not for a
   game. Same blocker class as yesterday's block 07 and the 48/48 first-meeting failures, but this
   time `dispatch_result` *names* it instead of leaving it inferred.

2. **One out-of-range camera draw poisoned image delivery for the whole block.** The sampler drew
   `camera.zoom {"target": 0.05}`; the view's declared `zoom_range` is (0.2, 1.0); the engine
   **accepted** it and nothing clamped or refused it. The provenance gate then withheld **16 of 17**
   subsequent frames with `camera zoom 0.049999713897705 is outside the view's declared zoom_range`.
   1 image delivered where 17 were possible. The recorder itself was healthy (61 frames, 0 capture
   failures, 3.8 MB GIF). **An action argument the catalog declares out of range must be refused at
   availability time, not accepted by the engine and paid for by the capture path.**

**Coordination incident (recorded, not counted; ATTRIBUTION CORRECTED — see Live S2).** An agent
this lane itself spawned to write its own `tests/live/**` files, operating headless, started a
real run against this lane's client mid-stage (`run-09110770797041989212fe6559f57900`, lock at
12:45:58 UTC holding this client's pid, a `t0001` quicksave at 12:46:15, in no store). It cost this
stage one refused tuner probe — the tuner takes one connection — and left a lock that would have
blocked the next run. That agent confirmed and removed it, and added skip-guards that run *before*
any lock or tuner connection plus `try/finally` lock release. It was **not** the headless lane that
owns loader/recovery, host-generic, web and store; that lane did nothing and the original wording
wrongly implicated it. Incident on issue #1. The rule this
earns: **a `live`-marked test must prove the client is free before it touches it, and must not be
able to leave a lock behind.** Worth noting honestly: that collision's positive control *did* start
and finish a real charter-matched run, which is the substance of T191 — but it was not run as T191
and the box stays unchecked until it is.

Cost $0.00 (stochastic provider, 16 calls, no OpenRouter spend). Artifacts:
`specs/002-civ-playing-harness/spikes/gameplay-2026-09-22/block-01/`. Client left alive, InGame,
turn 56, tuner free — but with the greeting still up and the camera still at 0.05, so Stage 2 starts
by reseting the camera and reading the prompt's real identity back from `InGame`.

### Headless 002 — 2026-09-22, analyze + converge (attempt 2; NOT CONVERGED on arrival and on exit)

Report: `specs/002-civ-playing-harness/analyze-2026-09-22.md`. Converge appended **Phase 14,
T264–T291** (28 tasks). T261 and T262 were never allocated and stay unallocated. Baseline observed
at `acd08bc`: **2148 passed / 18 skipped / 0 failed** (212.83 s). Commits: `acd08bc` (the handed-over
`store_cli` docstring), `87b1293` (report + converge).

**Three audits looked at different surfaces and returned the same shape.** Requirement coverage
derived from code and tests, last night's five live findings, and cross-artifact consistency were
not looking for the same thing. The report therefore leads with the pattern rather than a findings
table:

> **The unreached mechanism** — a mechanism that exists, is well built, is covered by passing
> tests, and is never reached in the case it was written for, with the suite green throughout.

Seven instances in one day. The content screening gate's declared-text technique is dead in
production because `detected_text_tokens` defaults empty and **no production caller supplies it**,
so five of six Linux reject categories — including `firetuner_window` itself, on a harness that
requires FireTuner to run — have **no reachable technique at all**, and 287 frames went to a model
through it. The source gate's process-identity check is unwired at the same call site, and
`test_source_gate_skips_process_check_when_none_supplied` asserts the clean result for *exactly the
production shape*. A correct prompt-key helper had zero callers in `src/` while a naive prefix-swap
reimplementation wedged a whole game board. The catalog's bespoke validator fires only on
`path: bespoke`, so it reads the very label it is meant to verify. The orphan sweep cannot reach an
orphan. FR-011 has no check of any kind. And the end-turn guard's test passes a *fake* callable,
proving the guard orders its own callable while the real `Game.EndTurn()` dispatch never goes
through it — the cleanest instance, because the false claim sits two paragraphs above the code that
contradicts it.

**A second pattern, named separately because its countermeasure differs:** *a value whose name
asserts something the value does not mean.* `turn_reached` counts harness cycles from 1, not game
turns — already in the gotcha list, and it still bit twice today, the second time producing a false
published claim that had to be chased. `dispatch_result` `{"ok": true}` means the call **returned**,
not that it took effect (`UI.SetMapZoom(0.5, …)` measured returning cleanly while `UI.GetMapZoom()`
still read 0.0499997). `shown_to_agent` records an intention while its docstring claims an outcome,
with 13 live rows proving it. The fix for this family is to make the wrong reading impossible, not
better documented — a fact that must be *remembered* to avoid a wrong conclusion will keep
producing wrong conclusions.

**Two CRITICALs.** (1) The screening gate fails **open**: a reject category no technique addresses
passed the frame. Principle I is non-negotiable, so the ruling was fail closed first and push
within minutes, accepting that Linux image delivery stops until the real fix lands. (2) A bespoke
synthetic-input path ships declared `path: firetuner` with `firetuner_gap: null` while
`composition.py:1099-1101` records a *measured* Firetuner gap in a Python comment and
`capability/executor.py` issues real XTest clicks — and `plan.md:296-302` certifies "there is now
no bespoke capability at all" with C1 discharged. Three layers assert it and all three read the
same field.

**The practice that overturned this pass's wrong answers, recorded as practice rather than
anecdote.** The three most confident wrong statements made during it were each overturned by going
to the primary artifact: "no frame has ever been shown to the agent" (the store rows said 300
shown, 287 with images); "the headless lane took the stale run lock" (the lock file's own bytes
named a pid from the live lane's own Stage 1 launch — the hypervisor retracted it); and "this
structural check would have caught the gate defect" (it was implemented and run in three
formulations, 24/32/43 hits, **C0 and C0b absent from all three**). That last one killed a check
that had already been adopted: the real shape is not "a parameter nobody passes" — which describes
good dependency injection as often as a defect — but "a parameter whose empty default makes a
**safety gate** vacuous", and the signal lives in what the value feeds, not in the signature. The
broad version would have shipped ~40 hits of which ~35 are legitimate, been allowlisted to nothing,
and still missed the shape: this project's defining defect wearing the costume of its fix. It was
replaced by a narrow semantic check over the screening gates' inputs, plus a separate
unwired-public-helper arm, both **negative-control-first** — build the failing case, feed it the
real defect shape, and refuse to ship the checker if it cannot flag it. That ordering is now
standing practice for the swarm.

**Corollary adopted swarm-wide after a false finding:** in a tree several agents are editing,
anything asserted about *committed* state must be checked with `git show HEAD:<file>`, with
`git status` consulted before calling a file clean. A subagent filed a confident finding about a
`_decoy_revert_check()` command that existed only in another lane's transient working-tree state.

**Also corrected here:** 003's B6 rated a missing **producer** as a missing **fixture** — four of
FR-018's eight metrics have no producer at all, so their series can never be non-empty. Root cause
of the mis-rating is a stale prose comment at `tests/unit/test_store_trends.py:32` asserting that
`compute_yields` is a no-op, which is false and propagated into two separate audit reports.

**Scope call raised to the hypervisor:** Principle VII's resilience half is *unimplementable*, not
merely incomplete — `HostPlatform` has **no method to start, stop or restart the client on any
platform**, so the post-defeat relaunch has nothing to be built from (T269).

**Boundary note, stated at the width the evidence supports.** A run lock cleared by the live lane
mid-stage was initially attributed to this lane and the attribution was retracted after the lock
file was read. What this lane verified about its own agents is: six Typer `--help` invocations, two
`python -c` calls into `load_catalog`/`doctor._probe_catalog`, one ImportError and four static YAML
readers, with `SqliteMatchStore` construction confirmed to occur only inside `_open_store()`, the
root Typer callback confirmed to do nothing but configure logging, and the runner factory assigned
but never called. Entry points and named modules' import scope were checked, **not** the full
transitive import graph — "essentially zero, not provably zero". The durable fix is T287: the
safe/unsafe line was being inferred from a directory path rather than declared, since `pyproject.toml`
registered `live` as its only marker and `--ignore=tests/live` as its only exclusion.

### Live S2 — 2026-09-22 (09:00–09:31 EDT) — a turn ended, the queue filled, and a whole pattern turned out to be a measurement error

**The headline, bounded.** `run-e9d52051ce7b458c9f06482ae2eabf16` (goal `build_a_builder`, Sonnet 5
via OpenRouter, worktree `c211605`, 3 harness turns, $0.272110 / 8 calls):

- **Game turn 56 → 57, observed.** `game.turn_state.turn_number` reads 56 at t1/s1–t3/s1 and **57**
  at t3/s2–s4 — separate observation commands, later-read evidence.
- **`cities.set_production` APPLIED, 1 of 1. It was 0 applied of 113.** Verbatim:
  `{"ok":true,"reason":"issued_not_yet_confirmed","confirmed":false,"insert_mode":"exclusive",
  "city_id":65536,"production":"UNIT_BUILDER","kind":"unit"}`. `city_id: 65536` is a real city id,
  not the type name — **the lone-argument bug is dead.** The `applied` verdict came from the
  harness's later re-observation, not the call's own return.
- **The precise claim:** `production_queue` already held `["UNIT_BUILDER"]` at t1/s1, so this run did
  not place the *first* order. It read `[]` at t3/s2–s3 after that Builder completed; the harness
  dispatched at t3/s3; t3/s4 reads `["UNIT_BUILDER"]`. **The harness filled an empty queue by its
  own verified action** — the first time. The Builder that exists completed off the *pre-existing*
  order when the turn advanced; credit the turn advance, not `set_production`.
- Two more first-evers applied: `prompts.congress_intro` (host click, WorldCongressIntro/
  AcceptButton) and `prompts.natural_disaster` (Close). Coverage 11 → **13 of 41**, screens 9 → 10.

**The bug that had wedged everything was one string in Python, not Lua.** `act/executor.py` derived
the Lua prompt key by inline prefix swap — right for 12 of 13 prompt declarations, wrong for the
13th, because the catalog names the action for what the human does (`prompts.ai_diplomatic_approach`,
answering an *AI's* approach) and the screen for what is on screen (`prompt.diplomatic_approach`).
**That exception was already recorded, in both directions, in `act/prompts.py`'s
`PROMPT_ACTION_BY_SCREEN`, with an inverse helper `prompt_screen_for_declaration_id` — tested,
documented, and with zero callers in `src/`.** The table was added for screen → action and the
action → screen direction was left re-deriving it by hand. Proven live before any edit:
`respond("prompt.ai_diplomatic_approach","Goodbye")` → `{"reason":"unknown_prompt","ok":false}`;
`respond("prompt.diplomatic_approach","__probe__")` → `{"ok":false,"reason":"option_not_offered",
"offered":["Goodbye"],"prompt":"prompt.diplomatic_approach"}`. Fix `c211605` wires the helper and
adds a test that parses `CIVSIM_KNOWN_SCREENS` out of `lua/ingame/screens.lua` and asserts the
dispatched key for all 13 declarations (old derivation fails exactly 1 of 13). **Why a green suite
shipped this:** the pre-existing test that dispatches this declaration stubs the Lua so it discards
`promptType` entirely.

**Consequence, recorded because the catalog said otherwise:** `e0e82f0`'s `AddResponse` work has
**never once executed**. Yesterday's 48/48 first-meeting failures were never evidence about that
path. The greeting path remains **untried** — `path: add_response` NOT observed, and today's
success does not sweep it up.

**A RETRACTION that removes two entries from this ledger's own pattern list.** S1 recorded
`camera.zoom` as "the engine accepted an out-of-range value and nothing clamped it", and this lane
reported `DiplomacyManager.CloseSession()` as "returned ok and did nothing" — both filed as
*no-ops reported as success*. **Both were wrong, and the error was in the measurement.** A readback
issued in the **same tuner command** as the write returns the **pre-call** value. Measured three
ways: `SetMapZoom(0.5)` → same-command readback 0.0499997, later command 0.50000047683716;
`CloseSession` → conversation still open in-command, gone on a probe 2 s later; `set_production` →
`confirmed:false` in-command, verified applied on re-observation. **There is no modal camera freeze
— that hypothesis is dead**, and `CloseSession` was probably working all along. The general rule:
**a readback issued in the same tuner command as the write is not a readback**, and every
verification in the harness with that shape is suspect.

**The next defect, and it is Principle III.** `turn.end_turn` now **dispatches** (the prompt fix
working; no more `unavailable_to_human_now`) with an identical
`{"ok":true,"result_is_informative":false,"path":"UI.RequestAction"}` — but is recorded **0 applied
/ 3 refused, `verification_failed`**, and all three turn cycles carry `game_turn_advanced: False`
**while the game demonstrably went 56 → 57**. The harness caused three turns and credited itself
with none. A record that denies an advance it caused is a gap, and `store/completeness.py` excludes
such a run from trending. Fix delegated: re-read the turn number in a **separate** command after the
async advance settles, under a bounded wait, with `game_turn_advanced` derived from that read. The
real latency — how long after `UI.RequestAction(ACTION_ENDTURN)` the turn actually moves — has
**never been measured**, so any bound is an assumption until it is.

**Process, recorded against this lane.** (1) The Live S1 coordination incident was **misattributed**
to the headless lane that owns loader/recovery, host-generic, web and store. It was an agent *this
lane* spawned; that lane did nothing. Corrected above, on #3 entry 13, and here. (2) This lane
launched a stochastic block into an output directory while another run held the lock — **having
printed that lock in its own command output** — and the block died `tuner unreachable [Errno 111]`.
The lane that wrote "check the lock" in the morning is the one that did it. (3) Stage 2 was called
stalled on absence of *reports*; it had in fact committed the fix and launched the goal run and was
working the whole time. **Absence of reports is not absence of work** — the same defect as a record
disagreeing with what happened, applied to an agent instead of a field. Retracted in two minutes.

**Suite.** Not green on the shared tree (90 then 98 failures, varying between runs, in modules no
lane here touched) because two lanes hold uncommitted `src/` edits and the suite reads them
mid-write. `c211605` was committed on targeted evidence (78 passed / 2 skipped over its changed
paths; 134 passed on the prompt and executor tests). New standing rule adopted: **verify a commit in
a clean detached worktree at the committed hash, with a bare `uv run` — setting
`UV_PROJECT_ENVIRONMENT` or `PYTHONPATH` silently re-imports the shared tree and destroys the
isolation** (both the hypervisor and this lane fell into that trap within an hour of each other).

### Named pattern — "a guard whose scope does not match the scope of the thing it guards" (2026-09-22)

Distinct from the day's other pattern ("a mechanism that exists but is never reached") and sharper,
because it tells you what to check: **find the guard, find the thing guarded, compare their scopes.**
Two instances, both found live today, both invisible to a green suite.

1. **A readback scoped to the command; the write settles asynchronously.** `UI.SetMapZoom`,
   `DiplomacyManager.CloseSession` and `cities.set_production` each report the **pre-call** value when
   read back inside the same tuner command. The guard ("did it take effect?") is scoped to one
   command; the effect is scoped to the engine's next frame. This produced two *false* findings that
   this ledger had to retract — a camera "no-op" and a `CloseSession` "no-op" — and one *true* defect
   it masked: `turn.end_turn` records `verification_failed` and `game_turn_advanced: False` while the
   game demonstrably advances.

2. **A first-contact retry rule scoped per client object; the refusal tail is per session.**
   `nexus/client.py`'s bounded retry over the documented post-close connection-refusal tail (T246)
   lives only in `reconnect()`, and only for a client that has already held a live connection;
   `connect()` fails fast by design. A chain leg builds a **fresh** client, so leg 2 lands inside the
   tail that **leg 1's own close created** and is denied the retry budget that exists for exactly
   that situation. **Every leg 2 of every chained goal is structurally exposed**; `use_a_builder` is
   the only chained goal and is therefore unreachable through the driver. Measured, not inferred:
   leg 2 died on `Errno 111` at 90 s and 91 s after leg 1's "finished" line across two runs, the Civ6
   listener fd moved 168 → 180, and a manual probe 40 s after the abort connected 3/3 in 0.00 s.

**The trap this category sets, and it caught us today.** The obvious repair for (2) is to give
`connect()` the same retry budget. That budget is 5 attempts over 5.5 s; the observed window is
**90 s**. The fix would pass review, read as principled, and never fire in the case it was written
for. **The contract documents a ~2 s tail and leg 2 is refused at 90 s — those cannot both be right**,
and the two explanations (a far longer tail on this host vs. leg-2 setup holding the socket) imply
completely different fixes. So the mitigation is scoped to `tests/live/goal_run.py` (reuse the
session across legs, or wait out the tail) and **the 90 s is being measured as raw timings with the
poll interval stated before any bound is written.** Credit for the scope analysis: Stage 3.

### Operating rule adopted 2026-09-22 — conflicting instructions

When two hypervisor instructions conflict, **act on the one that serves the owner, and say that you
did.** The hypervisor issues orders from a stale picture; the lane holding the client holds the
current one. A lane that silently picks one is a problem; a lane that stalls waiting for the conflict
to be resolved is a slower problem. **Flag and proceed.** Instance: the owner's screen was frozen on
a blocking popup while a "stay idle for the suite window" order was in force; the block needed the
run lock and tuner, the suite needed CPU, so they did not contend, and the block proceeded.

### Headless 002 — 2026-09-22, implement phase (NOT CONVERGED; suite green and observed)

**Suite at `8992cf6`, observed by this lane in a private clean worktree with the lock directory
empty and nothing else running: `2200 passed, 19 skipped, 41 deselected, 1 xfailed, 0 failed`
(224.95 s).** The verifier was verified first — `civsim_harness.__file__` resolving into the
worktree, `jinja2` importable — because three successive versions of the clean-worktree rule had
each failed to isolate what they claimed to.

**Landed from Phase 14** (T264–T294 appended; T261/T262 remain unallocated): the screening gate
now **fails closed** — image delivery is stopped on **all three platforms**, which is the correct
outcome, not a regression — plus the source gate's process-identity check running in production
for the first time, the category→technique mapping pinned as data with the detector partitioning
on that same function, and a strict-xfail negative control. FR-011 has a validator and a
sole-producer scan. `prompts.orders` is re-declared `bespoke` with its measured gap written down,
and a new check fires on the **real condition** (any in-harness `send_input` call site) rather than
on the label. The camera refusal, the `shown_to_agent` two-phase write, the lock-side orphan scan,
the `OSError`→`NexusError` transport wrap, the `client` marker, the gate-input roster, and the
records/contracts corrections all landed.

**Three findings arrived as corrections to this pass's own claims, which is the point.**
`city_count`/`unit_count` were never missing — they are derived from the last observation rather
than from `TurnCycle.yields`, so grepping the producer made them look absent; only `production` and
`food` are a real gap, and FR-018 was amended for **those two** with the Principle I reason (Civ VI
publishes no empire-wide figure for either) recorded in the spec. The `UI.SetMapZoom` and
`DiplomacyManager.CloseSession()` "no-ops" were **withdrawn**: both calls worked, and the tuner
returns the pre-call value when the readback is inlined with the write. And the broad structural
check was built, run in three formulations, and **rejected on evidence** before shipping.

**The day's pattern, and this lane's own instances.** *A mechanism that exists, is well built, and
is never reached in the case it was written for, with the suite green throughout.* Ten-plus
instances. Two were ours and both are recorded rather than softened: three successive versions of
the clean-worktree verification rule that did not isolate the tree, and — worse — **nine parallel
suites saturating the machine we were measuring.** "The suite is green" was measuring a box this
lane was itself loading, so its failures carried no information and its passes carried less. That
is the same error as an inline readback: **measuring through a channel the measurement perturbs.**
It cost a paid live run (`tuner unreachable`, 90 s into its second leg) before it was caught.
Corrected: one suite at a time lane-wide, gated through the lane lead, and yield to any run lock.
**On the quiet box `/compare` passed** — so the "known flake" was us, and T282 survives as a real
finding about a 1.39× margin where the rest of the module has 7×.

**Operational facts worth keeping.** A broadcast stop is asynchronous — an instruction reaches an
agent only at its next tool round, so a lane keeps acting on stale orders for minutes; it took
repeated kill sweeps plus per-agent messages to reach quiet. And **a path-scoped `git add` is
scoped to the path, not to your changes to it**: on a shared tree that swept a half-landed import
into a commit and briefly made HEAD unimportable, and swept a whole-file line-ending conversion
into another. Check `git diff --stat <path>` against HEAD before staging.

**Still open and owned elsewhere**: T269 (Principle VII's resilience half is *unimplementable* —
no lifecycle method on any platform), T277 (the cross-view camera measurement), T293 (inline
readbacks — three turn cycles recorded `game_turn_advanced: False` while the game advanced 56→57),
the `detected_text_tokens` plumbing (blocked on a `HostPlatform` window-enumeration method), and
the production lock leak in `run/composition.py`, which needs one context manager rather than a
fixture guard.

### Correction to Live S2, and a third named pattern — "a threshold chosen without measuring the thing it bounds"

**Live S2 above files the `turn.end_turn` miss under the same-command-readback family and prescribes
"re-read in a separate command … with `game_turn_advanced` derived from that read". Both halves were
already true of the code.** Verified from the live store rather than from the code's appearance
(`run-e9d52051…`): `act/verify.py`'s `confirm_execution` already issues a genuinely separate tuner
command per poll — `confirm_attempts: 14` over ~47 s on the very cycles that failed, each a distinct
read — and `game_turn_advanced` already derived from that bounded re-read
(`decision_loop.py:1021`), never from the dispatch's own return.

**The actual defect: `END_TURN_CONFIRM_TIMEOUT_S = 45.0`, a bound set without measuring the thing it
bounds.** That is a **sibling** of the guard-scope mismatch, not an instance of it, and it earns its
own name. Fixed in `24c0655` (200.0, and `BACKSTOP_CONFIRM_TIMEOUT_S` with it), **labelled an
assumption in the source** pending a direct single-dispatch probe. `287dee7` verified green at 2201
in an isolated worktree; `CivSolver-live` advanced to it.

**Count the instances, because the count is the finding.** Three times in one day a threshold was
set, or nearly set, without measuring what it bounds: this 45 s bound; the 5-attempt / 5.5 s
reconnect budget nearly widened against a **90 s** observed window; and the replacement 200.0 itself,
which clears every observed sample but whose ceiling is unmeasured. **Two of the three were caught
before landing only because someone asked for the measurement first.**

**What the measurement actually says**, two independent datasets that reconcile:
- direct from the store's `confirm_*` fields, n=12, ±2.0 s poll: **8 passed at 11.5–16.9 s**, **4
  timed out at ~47 s**. **Bimodal — nothing between 17 s and 45 s.**
- a wall-clock bracket on one slow case: **75–155 s** after dispatch.

And the disclosure that makes the second usable: n=1; precision floor **12.4 s**, set by how far
apart two *unrelated* decision-step reads happened to fall; dispatch-to-first-observed-change, not
to confirm-success; biased **high** by the model round trip inside `step.started_at`; and
attributable to **either or both of two redundant dispatches** — so **no single-dispatch latency has
been measured at all**. The honest summary is "more than ~70 s, plausibly up to ~155 s, true value
unmeasured". A direct probe is still owed.

**Blast radius, bounded by invariant rather than by choice.** `decision_loop.py`'s I16 forbids any
wall-clock, step-count or cost check inside the module ("there must never be one added", guarded by
`test_no_truncation.py`). So each turn's confirm takes its full 200 s **independently and
additively**, and the backstop path pays its own on top: worst case ≈ N × 200 s before any other
work. The cap therefore **cannot** live in the module, which makes detached-execution-plus-polling
the only available mitigation rather than merely the preferable one. Every run brief from here states
worst case as turns × (bound + overhead) in advance, so an abort is a finding rather than a surprise.

**One more, found by checking instead of asserting.** The confirm loop was offered as the worked
example of a phase safe from a watchdog's kill ceiling, on the grounds that it polls every ~2 s and
is therefore demonstrably alive. A grep for any logging or telemetry call inside `confirm_execution`
returns **nothing**: it polls **silently**. **Active is not emitting.** From outside, the single
longest legitimate operation in the system — doing exactly the right thing — is indistinguishable
from a wedged process, so a three-minute ceiling would kill it during a legitimate slow-regime turn.
The requirement that follows: **the liveness signal must be emitted by the waiting phase itself,
never inferred from the process still existing**, or the criterion collapses back into the timer it
was written to replace. Routed to the lane owning `act/verify.py`, together with the open question —
not yet a finding — of whether the in-flight provider call emits anything, which would matter more.

### Two more named patterns, and the rule-writing principle that came out of applying them (2026-09-22)

**Pattern: "a docstring standing in for a mechanism."** `RunLockHandle.commit()` promised the lock was
*"registered somewhere a later, real release will find it."* It was not: the "later, real release"
(`_release_terminal_run_clients`) runs only when a **subsequent** run prepares, in the same process,
against an in-memory `run_contexts` dict a fresh process does not have. **This is a sibling of the
zero-caller helper and the discarded return value, and it is the worst of the three** — those are
passive, and nobody looked; this one *made looking feel unnecessary*. It reads as a guarantee to
anyone auditing the lock's lifetime, which is the likeliest reason the gap survived the commit that
was written to close it.

**Pattern: "the engine answers and we discard the answer."** Three instances now, all live-measured:
1. `cities.set_production`'s Lua answered `city_not_found` and the executor dropped the dispatch
   answer, surfacing it as `verification_failed` — the defect that hid the lone-argument bug.
2. `run/composition.py:746` calls the debug-menu reader and **discards the return**, so
   `Run.debug_menu_state` has been empty on every run ever made (T280).
3. The build-options read: the engine returns a **per-row failure reason** for each disabled build
   and our Lua **discards it**, so `available_builds_reason` came back `null` where it should have
   said "cannot build on a city centre". The field exists precisely to carry that explanation.

Distinct from "a mechanism that exists but is never reached": here the mechanism runs, the engine
replies, and the reply is thrown away — so the system is *less* informed than the game was willing
to make it, and the silence reads as absence of information rather than loss of it.

### The principle for writing the next rule

Today's rules divide cleanly by one property, and it predicts which held:

> **A rule that asks for an assessment returns the assessor's priors. A rule that asks for a lookup
> returns the fact.**

- The verification rule failed **three times** while it asked "is this isolated?" — an assessment.
  It started working when it asked "which path does the import resolve to?" — a lookup.
- "What measurement is this number from?" works because it is answerable without judgement.
- Reconciliation works because "which line, which commit" is a lookup.
- The case that proves it: asked *"is the lock fix good?"* the honest answer was **yes** — the guard
  really is correct. The question that found the gap was narrower and duller: **"which line releases
  it on each exit path?"** Same code, same reviewer, same day; only the second question found that
  the guard's protection ends at `commit()` and nothing spans the drive phase.

A rule phrased as an assessment feels like it is working right up until the moment it matters, because
it agrees with whoever is applying it. Prefer the dull question.

### Closing note, 2026-09-22 — the safeguard that worked was the verification step, not anyone's scepticism

The day's last correction is a correction about the day itself. The hypervisor credited this lane with
**refusing a ruling**: it had ruled for a `finally` spanning the drive phase, and the lane reported
that back as unimplementable. But the lane did **not** spot the flaw when the ruling was issued. The
agent that wrote `0187345` traced `runner.py:911-913`'s operator-stop bypass independently, and only
then did it become visible that `composition.py` has no scope to wrap at all — `_prepare_connected_run`
**returns**, and the run is driven by `Runner` afterwards. A `finally` placed in composition would have
looked like compliance, passed review, and covered nothing.

So the refusal came from a **measurement**, not from anyone's judgement of the ruling.

**That distinction matters more than the fix.** A safeguard that depends on someone happening to be
sceptical is precisely the class this whole day has been naming: a mechanism that exists, reads as
protection, and fires only when the person applying it was already suspicious. Scepticism is not a
control. It is a mood, unevenly distributed, and absent exactly when deference or time pressure is
highest.

**It also closes the loop on the morning's rule about how to write rules.** *A rule that asks for an
assessment returns the assessor's priors; a rule that asks for a lookup returns the fact.* The
verification step **is** the lookup. It replaced "do you think the ruling is sound?" — which returns
the answerer's deference — with "which line releases the lock on each exit path?", which returns a
fact and does not care who is asking or who they are answering to.

**Every catch today has that shape, including the three where the hypervisor was the one corrected**
(the camera clamp that would have made a declared range unreachable, the non-isolated verify
invocation, the `finally` with no scope to live in). None of them required anyone to be clever. All
of them required someone to be **made to look** — and the making was done by a rule, a gate, or a
store query, never by an instinct.

The practical form, for whoever runs the next lane: **when a ruling is handed down, the next action is
not to evaluate it — it is to run the lookup it implies.** If the lookup cannot be run, that is itself
the finding.

### Refinement to the lookup rule, and the better closing note (2026-09-22)

**A partial lookup is worse than none, because it launders the claim.** The T280 crossing was
misattributed by three parties in one exchange, and the decisive sentence was this lane's: *"Reading
the diff, it is T280 work."* One diff was read — `preparation.py`'s — and that authorship was
generalised to two files nobody opened. `decision_loop.py`'s change was T265 plumbing authorised hours
earlier under a different brief. The other lane inferred authorship from filenames; the hypervisor
ruled without reading any diff and then granted a retroactive crossing for something that had not
happened, papering over the error rather than exposing it.

Not running a check leaves a claim **visibly unsupported**. Running a partial one and naming it makes
the claim **sound verified**, and nobody re-checks a sentence that cites its own evidence. So:

> **Run the lookup on each thing you are about to claim, not on one and then generalise. A lookup
> answers exactly the question asked of it and nothing adjacent.**

**And the better closing note, demonstrated rather than asserted.** The only part of that handling
which cost nothing was the **mitigation** — flagging rather than objecting, and re-reading every file
immediately before editing it. Those held **while the belief behind them was wrong.** That is the
whole argument for procedural safeguards over correct beliefs: the safeguard does not need the actor
to be right, and today it was not.

### Wall-clock measurement on this host is uninformative, not merely noisy

Measured spread on an **unchanged** binary: **1.08 s to 3.71 s** — a **3.4× spread**, against the ~2×
a genuine regression would produce. The measurement therefore **cannot separate a regression from an
idle moment at all**, under any conditions. This is stronger than the earlier "inconclusive under
load" reading, which was right for a weaker reason. **No wall-clock margin is evidence for anything
on this box.** Count reads, operations or allocations instead (`125a74f` already does this for the
`/compare` budget case). The timing-or-budget clause in the verification rule stands, but its
justification is now this number rather than an impression of load.

### Stating a rule does not produce compliance with it (2026-09-22) — evidence from the author

The lookup rule was written today, by this lane, after a misattribution: *run the lookup on each thing
you are about to claim, not on one and then generalise.* **This lane then made the same error twice
more, the third time after writing the rule**, attributing a suite running in `CivSolver-chainverify`
to itself in a state check without reading whose process it was. It was another lane's.

That is the strongest evidence available for a claim the project has been making all day about its own
code, now made about its authors: **a rule is not a control.** The instances are not a failure of
memory or of care — the rule was fresh, self-authored, and had just been cited. They are what happens
because remembering is an input the system cannot guarantee.

**So: prefer the mechanical gate over the remembered principle.** A pre-slot scan that reads diffs
rather than filenames is a control. "Remember to read the diff" is not, and today it demonstrably was
not, three times, once by the person who wrote it down.

The same asymmetry runs through every finding today. The zero-caller helper, the discarded return, the
docstring standing in for a mechanism, the predicate referencing an absent field — each was a case
where the correct behaviour was **known and written down somewhere**, and nothing made it happen.

### Chain leg 2: the 90-second gap was a retry budget, resolved by reading rather than measuring

The abort is **not** `_prepare_run`'s `connect()`. It is `tests/live/demo_landed_run.py::read_setup()`,
called by `run_goal` **before** `build_runner_dependencies`, which already carries its own bounded
retry: `connect(retries=30)`, **3.0 s apart** (`demo_landed_run.py:206-216`). **30 x 3.0 s = 87-90 s;
the observed aborts were at 90 s and 91 s.** The gap was never a tail duration and never slow leg-2
setup — **it is a retry budget running out**, to the second. Every one of the 30 attempts failed with
the **refusal** signature rather than a slow-response timeout, which rules out slow setup and points
at a tail **longer than 90 s** — the opposite of what the raw gap suggested. A live probe approved to
measure this was cancelled; **reading beat measuring, for the third time today.**

Consequence for the landed fix: `CHAIN_LEG_TUNER_POLL_TIMEOUT_S = 240.0`'s comment **understates its
own evidence**. The honest statement is clearance over a tail that **exceeded 90 s of continuous
refusals** — not clearance over an observed 91 s gap, which is a measurement of *when we stopped
asking*, not of the tail.

**Session reuse was established impossible with the object and the line, not inferred from a plausible
seam** (`build_runner_dependencies`'s client factory *looks* like one): `_prepare_run` calls
`connect()` **unconditionally** on whatever the factory returns (`composition.py:783`), so identity
does not change what is called on it; and `evaluate_stop_facts` schedules
`_close_quietly(ctx.nexus_client)` (`composition.py:1616`) the instant a stop resolves, so production
has already closed leg 1's client before `run_goal()` regains control. The fix would be
**parameterising a production close for a test driver's convenience** — a worse dependency than the
one it removes. **The probe is therefore the correct answer, not a fallback.** Reuse is appended as a
task with this analysis attached so it reopens from here rather than from scratch.

### Pattern: an allowlist inverts its safety property depending on which way it is read (2026-09-22)

An allowlist is **safe when used to permit**: unknown means deny, and the failure mode is a false
refusal — visible, annoying, self-correcting. It is **unsafe when used to detect**: unknown means
"nothing there", and the failure mode is a **false all-clear** — invisible, and stated with
confidence. **Same data structure, opposite failure mode, and nothing in the code distinguishes the
two uses.**

Three instances today, and the third had already been fixed by the time the first was found:

1. **The screen watchlist.** A screen not on it reads as *no screen*. The probe reported
   `recognized=true`, `has_blocking_prompt=false`, screen `world` — with `EndGameMenu` full-screen in
   front of it. **`recognized=true` while wrong is categorically worse than `recognized=false`**: an
   unknown screen would have made the harness know it did not know. Instead every downstream consumer
   treated a false answer as settled. **CORRECTED 2026-09-22, later: the consequence stated next was
   not observed.** This entry went on to say the harness "dispatched into a modal and recorded the
   resulting failures as action failures rather than as a blocked board" — a mechanism relayed to this
   lane as confirmed, repeated here and in two further messages, and **never checked**. The audit finds
   modal blindness is **not positively identified anywhere in the store**: all 670 bundles read
   `recognized: true` and no end-game screen id appears at all. The nine steps that *were* dispatched
   into a blocking prompt are a **different and cheaper defect — an availability-gate gap**, where the
   harness **recognised** the prompt and dispatched anyway. The probe's false all-clear is real and is
   the finding here; the downstream consequence attributed to it is withdrawn.
   **Blast radius of one unverified relayed sentence: four documents before anyone ran the lookup.**
2. **`lua/ACCESSORS.txt`.** Answers "does this method exist". The segfault spike already records that
   it does **not** answer "is it safe to call here, on this object, from this context".
3. **The content screening gate.** A contaminant category no technique addressed read as **clean** —
   and the remedy already adopted was **fail-closed**: a category nothing can detect now **withholds**.

**So the probe's fix is the gate's fix, one subsystem over.** The gate went from *"no technique
addresses this category -> pass"* to *"-> withhold"*. The probe must go from *"no watchlist entry
matches -> world"* to *"-> unknown"*. That is not a new design argument; it is a **precedent set today
under the owner's own criterion, on the same class of defect**, which turns a debate into a
consistency requirement.

**The dull, mechanical test for any detector, in the family of "which line releases the lock on each
exit path":** *can this check ever return "I do not know"?* If it cannot, it is an allowlist read as a
detector, and it will produce confident false all-clears.

**And the taxonomy consequence.** Spec 004's classes were "the game is blocked", "I blocked myself",
"I cannot reach a healthy game". This is a fourth — **the harness believes nothing is blocked while a
modal is up** — and it is the worst, because a watchdog consulting harness state does not merely fail
to notice: **it agrees, and certifies a dead run as healthy.** That makes "compare the harness's
belief against an independent read of the engine" load-bearing for the whole spec rather than a
refinement of one class.

### A check that worked, recorded with the same care as the ones that did not

This ledger is heavy with mechanisms that could not fail in the way that mattered. One did, today, and
the record should say so or it teaches the wrong lesson.

`tests/contract/test_record_schemas.py::test_published_schema_matches_current_model` caught a real
drift between `Run` / `ParityDeclaration` and their published JSON schema **immediately**, by machine,
and **reproduced in two independent environments** — an isolated worktree at `7981ead` and the shared
tree at `2012c8e`, different venvs, different heads. No judgement, no interpretation, no one needing
to be suspicious. **This is what a check looks like when it is real**, and it is the counter-example
to every finding above it.

Its remaining risk is the tempting fix: regenerating the export **always** goes green, whether or not
that was the right direction. Whether the model gained a field it should have, or the export went
stale, is a question about **intent** that the test cannot answer and must be established from the
commit that caused the drift.

### A defect recurring inside its own remedy — twice today (2026-09-22)

Two instances, and the second is what makes it a finding about **how we fix things** rather than about
locks or rules:

1. **The lookup rule.** Written by this lane after a misattribution, then violated by this lane twice
   more — the third time *after* writing it. Stating a rule does not produce compliance with it.
2. **"A docstring standing in for a mechanism."** Named in the ledger after `RunLockHandle.commit()`
   promised the lock was *"registered somewhere a later, real release will find it"* when nothing
   guaranteed that. The commit that **removed** that promise, `d3cae2f`, introduced a new one: its
   scope note says what it cannot cover is *"a hard kill (`SIGKILL`, or any signal Python can't
   catch)"*. **`SIGTERM` is a signal Python can catch — it is simply not caught.** No `signal` handler
   exists in the file. So the documented boundary sits in the wrong place, and an auditor reading it
   would conclude `timeout` kills were covered.

**And `timeout` is what this project's own standing rules use** — `timeout 580` for live drivers,
`timeout 900` for the suite — and `timeout` sends `SIGTERM` by default, which Python's default
disposition handles by terminating **without running `atexit` handlers**. So the uncovered path is not
an edge case; **it is the kill our own conventions generate.** Ruling: install the handler *and*
correct the sentence — install only while a lock is held, restore the previous handler on release,
release-then-re-raise the default disposition so the process still dies as the caller expects, and
chain rather than clobber an existing handler.

**The general lesson.** A fix is written in the frame of the defect it is fixing, so the pattern it is
fixing is exactly the one it is least equipped to notice in itself. The remedy is not vigilance — that
failed twice today, once by the author of the rule — but **applying the same check to the fix that
was applied to the defect.** Concretely: when a fix's commit message states a boundary, run the lookup
on the boundary. *"Which signals does this actually catch?"* is a `grep` for `signal`, and it took
under a minute.

### Also worth recording: the fix that beat its own brief

`d3cae2f` was given an enumeration gate — prove every terminal path funnels through `_finish`, stop
and report if they do not. It ran the enumeration, **found they do not funnel** (`evaluate_stop_facts`
is reached on two of `Runner._play_run`'s several terminal paths; operator stop and every
`HarnessError` routed to paused/failed bypass it), and then chose a design that **does not need them
to funnel**: `commit()` arms an `atexit` backstop, and `RunIdentityLock.release()` — the one point
every release path already converges on — disarms it. That is *"ask whether the fix needs the number
at all"* one level up: it does not need the paths to funnel, so it does not care that they do not.
The convergence claim was verified rather than accepted, which is the only reason the design is known
to hold.

### Three rules from the day's last hour (2026-09-22)

**1. A record worse than the truth and a record better than the truth are different emergencies.**
Every defect found today makes the harness look **worse** than it is — an action that landed recorded
as refused, a turn caused and not credited, a capability that works reported as failed. Those are
**recoverable**: the truth is better than the record and the record corrects upward.

`make_peace` runs the other way. Its predicate is `!= "war"` against a field that **does not exist**,
so absence makes it evaluate **True** — it would record a **no-op as `applied`**. It is harmless only
because availability reads the same absent field first and refuses. **Fix the context without fixing
the predicate shape and an unreachable action becomes a silently lying one.**

> **A predicate compared against a possibly-absent field must distinguish absent from false. Its
> polarity alone decides whether it under-reports or fabricates.**

Under-reporting is a measurement problem. **Fabricating is a credibility problem: nothing in the data
would ever reveal it, and it poisons the claims that are currently sound along with the rest.** The
sweep's primary deliverable is therefore the **polarity list** — for every predicate over a possibly
absent field, does absence resolve False (under-reports, visible) or True (**fabricates, invisible**).
The True list did not exist, and it is the only artefact that can say whether the store already holds
a fabricated `applied`. Producing it and fixing nothing is the preferred outcome.

Ordering that follows: **predicate shape first, then the context probe, then wiring — never the
context alone.** This is spec 004's spine arriving in a third place: **absence and unobservability
must not share a representation.**

**2. Beware the fix that manufactures its own confirmation.**
`cities.set_production`'s 110 failures are a **dispatch** defect — an operator probe shows the same
order landing in **+1 s** when correctly parameterised. Raising `ACTION_CONFIRM_TIMEOUT_S` would have
moved that number **for reasons unrelated to bounds**, and the movement would have been read as
evidence the bound caused them. The real defect would then sit behind a green metric **with a
documented cause**, and nobody would look again.

> **The danger is not wasted effort. It is that the number moving is taken as evidence for the
> hypothesis that motivated the fix, so the wrong explanation gets confirmed and the question closes.**

Defence: **predict the specific observable before making the change.** A movement matching the
prediction counts; a movement that merely goes the right way does not.

**3. A mid-task message may narrow, never widen.**
The provenance rule — *a scope change is never a mid-task message, it is a fresh brief* — read as
*ignore all mid-task messages*, which would block a re-prioritisation exactly as hard as an
escalation. **An agent that cannot be told to stop is worse than one that cannot be told to start**,
and the stop channel was needed three times today, including the quiescence hold.

> **Does this message permit an action that was previously forbidden? If no, it is not an escalation.**

A message that forbids, reorders or re-prioritises **permits nothing new, so it carries no injection
risk by construction**. The worst a hostile narrowing achieves is making the agent do less — denial of
service, not privilege escalation. The test keeps the property that made the original refusal correct:
**it is mechanical and does not require judging the sender.**

### Another check that worked: the availability layer's 58 honest refusals (2026-09-22)

`units.found_city` stands at **1 applied / 58 refused**, and **all 58 refusals are
`unavailable_to_human_now`** — the **pre-dispatch** kind. Not one is a `verification_failed`. So across
58 draws the availability layer **correctly declined to offer an action the game was not offering**,
every time, with no false positive and no fabricated success.

That deserves recording precisely because the rest of today's ledger is failures: a screening gate
blind to the one contaminant guaranteed present, a refused column mis-scored by two independent
mechanisms, a verification that could not fail sitting in the day's headline claim, an orphan sweep
that could not see the case it existed for. **Against all of that, this column is clean** — and under
the rule adopted this morning (*name what held, not only what broke*), a ledger that recorded only the
failures would never have mentioned it.

**Its remaining risk, stated beside it, because praise without the caveat is how a working check gets
broken:** those 58 establish that the layer **refuses correctly when the game says no**. They say
**nothing** about whether it **offers correctly when the game says yes**. **A gate that always refused
would produce an identical column.** The positive half is unevidenced here and needs a board where the
action genuinely is available — which is exactly what the live lane is attempting now. Founding a city
by the harness's own action has happened **once, ever** (2026-09-20). If a city appears from the
current run, that is the second time in the project's history; if it does not, these 58 stand as
evidence the action was simply never offered on those boards, which is a different and still useful
fact.

### T310/T311: the same six declarations are vulnerable on BOTH operands

Worth stating explicitly, because the obvious reading is wrong. It is **not** six declarations exposed
on the observation side and a different set exposed on the target side. It is the **same six**,
independently, on **both**:

- **right operand** (T310): an absent observation field makes the predicate resolve **True**, so a
  no-op records as `applied`;
- **left operand** (T311): a **null `target`** does the same through `x not in y` / `not (x in y)`,
  **regardless of whether the observation field is present.** Confirmed live: today's headline
  `prompts.ai_diplomatic_approach` `applied` had `prompt.options == ["Goodbye"]` — **present** — and
  `target: null`, and passed at `confirm_attempts: 1, confirm_elapsed_s: 0.0`.

**Consequence: a fix addressing only the observation side would leave every one of the six still able
to fabricate.** Any repair must check **both sides of the comparison** for absent-or-null and pin the
resolved polarity in a test, or it closes the visible half of the defect and leaves the invisible half
in place — which is worse than not fixing it, because the task would be marked done.

### The reversal: four agreeing sources were four instruments reporting where they stopped (2026-09-22)

By late afternoon four figures agreed that `ACTION_CONFIRM_TIMEOUT_S = 4.0` was wrong by an order of
magnitude: a city founding still unconfirmed at **7.291 s**, `units.move_to` at **9.63 s**, end-turn's
**11.5–16.9 s** fast regime, and the owner, watching the screen directly, reporting **30–60 s per
interaction**. It looked overwhelming.

**A sub-second probe reversed it.** Poll interval 0.10 s, each poll its own tuner command, measured
bare-command round-trip floor 0.0663–0.0670 s (n=8):

```
cities.select  n=3   0.1337, 0.1334, 0.1333 s
units.select   n=5   0.1670, 0.1664, 0.1667, 0.1666, 0.1667 s
units.move_to  n=2   0.1668, 0.1668 s
camera.move    n=6   NOT OBSERVED in 20 s, 134 polls each
```

**Every sample was observed on poll #1** — volunteered by the probe, not asked for. So 0.133–0.167 s
is **the instrument's own floor, not the game's latency**: these actions were already complete the
first instant anything could look. **They are upper bounds, not measurements.** A less careful report
would have handed over "select settles in 0.167 s" and been wrong in the direction that sounds
precise.

**Why the four agreed: none of them measured a settle time.** Each was a **lower bound produced by
something giving up** — a bound expiring, a poll loop ending. That is not four independent
confirmations; it is four instruments reporting where they stopped. And the owner's 30–60 s is not
even the same quantity: he is watching a **human-visible interaction cycle dominated by ~48.5-second
model calls**, not an action's settle time. **0.167 s and "about a minute" are both true and about
different things.**

**So the store's 7.2–7.5 s cluster is two phenomena wearing one shape:** a genuinely slow class
(`units.found_city`, still unsatisfied at 7.291 s) and **a stopped clock** — a predicate that was
never going to become true, timing out at the bound and looking like slowness.

**Raising the bound would have waited longer, moved both numbers, and confirmed the wrong
explanation.** That is the manufactured-confirmation trap, named two hours earlier the same day, and
this lane was about to walk into it on what looked like overwhelming evidence.

**The only reason it stayed legible: the predicate fix was landed first, with touching the bound in
the same commit explicitly forbidden.** Had both landed together, the founding would have started
passing, the number would have moved, and the bound would have taken the credit for a predicate fix.

**What replaces the bound work: per-class settle-time measurement with an instrument that can resolve
below 0.1 s, plus the predicate sweep. No uniform number, and nobody proposes one.** Note the
distinction that survives: `END_TURN_CONFIRM_TIMEOUT_S`/`BACKSTOP_CONFIRM_TIMEOUT_S` at 200 s remain
defensible — an end turn genuinely waits on every AI player, a real slow class with measured 11.5–16.9 s
fast and 75–155 s slow regimes. **`ACTION_CONFIRM_TIMEOUT_S` is the one that must not be raised
uniformly**, because the classes beneath it differ by two orders of magnitude.

**Four actions are now proven to have landed while scored `rejected`:** the city founding (Pasargadae
in the next observation), the production order (`production_queue ["UNIT_SCOUT"]` on the board), and
two moves. **The refused column is not a list of failures. It is a list of things the harness could
not see.**

**Third instance of the lone-argument defect, code-confirmed.** `camera.move`'s dispatch returned
`{"target_plot": {"x": {"y": 31, "x": 44}}, "ok": true}` — the plot table **nested under `x`**.
`lua/ingame/camera.lua:66` is `CivSim_Camera_Move(x, y)`, two scalars with **no table guard**, so the
dispatcher's last-positional `target` lands in `x` with `y` nil; `UI.LookAtPlot(table, nil)` does not
throw, so `ok` is true and `camera.target_plot == target` is unsatisfiable. **`units.move_to` received
exactly this guard on 2026-09-21 and `camera.move` never did** — the fix pattern was already in the
tree, for the third time.

### Headless 002 — 2026-09-22, attempt 4 close (NOT CONVERGED; the day's green is `96dabb6`)

**Suite: `2311 passed / 19 skipped / 41 deselected / 0 failed`, 248.84 s, at `96dabb6`** — dedicated
worktree, pinned head, `uv sync --all-groups --all-extras`, bare `uv run`, **ungated**, isolation
proven *before* the run. Every provisional count quoted earlier in the day is discharged against it.
Board at close: **274 closed / 37 open** (the open count rose because lanes appended faster than
anyone closed, not because anything regressed).

**The three ruled priorities all landed.**

**T301 — the reload path was silent for a worst case of 510 s, and only the enumeration could say
so.** `RELOAD_PATH_WORST_CASE_S = 510` is **summed from the table, not asserted** — 2.8× the 180 s
budget. Neither roster row could state it alone, because *the 300 s in both rows is the same 300 s*:
`recover` is rung 4 and `_await_phase` is the wait rung 4 is built on. Patched apart, each looks
like a 300 s problem. **That is the argument for enumeration, made by the enumeration.** No thread,
no timer, no clock-driven emission — the record rate *is* the work rate, asserted by a test driving
a round trip that never returns and requiring silence. **No bound, poll interval or deadline was
changed: make it legible, do not make it shorter to fit a threshold.**

**T298 — the task was completeness lying; the finding was the guard failing open.**
`store/trends.py::exclusion_for` enumerated `has_gaps` and `unknown` and let **everything else fall
through to eligible** — so the one gate enforcing Principle III admitted any status it did not
recognise. Now an allowlist admitting only `COMPLETE`. 003's US3/AC3 was deliberately **narrowed**:
a mid-turn run is excluded because its shape is *indistinguishable from a run that died on turn N's
write*, and Principle III does not let the gate guess.

**T299 — the fix was not to make the guard pass, it was to make the guard able to fail.**
`firetuner_window` is **still not matchable and now says so**. The loader refuses a keyword group no
declared witness produces, checked against **the evidence source's own tokeniser**; today's shipped
state does not load under that rule. **Exactly one of ten categories has a usable vocabulary, pinned
by a test so it cannot grow by invention.** The blocker is **executable** — Windows and macOS under
`xfail(strict=True)` — so closing it takes a deliberate deletion, not a quiet green. Declaring
`firetuner_window: [[firetuner]]` was declined: mechanically different from the rejected host-side
`window` token, **identical in effect**, and it would have turned a red guard green without changing
what the gate can see.

**Two rules this pass earned, both from our own failures.**

**The fail-safe corollary.** *A fail-safe defect survives longest in exactly the mechanisms we trust
most, because a guard erring toward "not ready" or "withhold" never produces a visible incident — so
audit the fail-safe direction deliberately, because nothing else will make you.* Three findings
share it (`firetuner_window` withholding nothing while the map says covered; the suite gate
reporting busy on a clear box; completeness reporting `complete` for a run that lost data) and
**every one sat all day while the dangerous defects were found in minutes.** Its counterpart landed
the same day: T298's trending gate failed **open**. **The fail-safe direction hides a defect; the
fail-open direction is one** — and one rule catches both: a default's direction must be a decision,
never whatever the enumeration happened to omit.

**Gate on what the measurement is for.** A functional result is not load-sensitive; a timing result
is, and cannot be rescued by waiting. 248 s loaded against ~225 s quiet is ~10% — noise for pass/fail,
meaningless against the 3.4× spread measured on `/compare`. The "wait for quiet" gate was chasing a
state this machine never reaches. **This retires the last defence of the render budget as a
wall-clock assertion.**

**And a category: a heartbeat is not a progress signal.** During a save load the tuner port is closed
*by design*, so polls answer `unreachable` — work-derived **about the poll loop**, and not evidence
the load is advancing. Hence `reload.phase.polled`, never `loading`. Measure elapsed against **the
phase's own declared bound**, not the watchdog's ceiling; that is what makes a 510 s reload survivable
under a 180 s rule without bending it, and without it the ceiling kills every legitimate reload and
**the killed recovery looks exactly like the crash it was recovering from.**

**Two failures at two altitudes, both cheap:** *a claim nobody re-checks* (three relayed subagent
claims passed on as established, two false) and *an answer nobody looks up* (the correct
`grep -c "[b]in/pytest"` matcher **already existed in the tree** while two lanes used a form that
matched monitoring scripts and their own command lines). Neither is fixed by being more careful.

**Still open and highest-value:** T269 (Principle VII's resilience half is *unimplementable* — no
client-lifecycle method on any platform), T297 (we stored 421 withheld captures with null blobs and
destroyed the evidence needed to evaluate our own detectors — fix with derived statistics, never
pixels), T300, T313, and T299's executable Windows/macOS blocker.
