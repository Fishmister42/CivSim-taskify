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
