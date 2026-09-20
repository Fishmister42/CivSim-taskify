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
