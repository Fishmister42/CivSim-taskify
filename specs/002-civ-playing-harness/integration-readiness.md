# Integration Readiness Audit -- 002-civ-playing-harness

**Date:** 2026-09-20
**Scope:** Trace the real call graph from `civsim run start` to a played, recorded turn. No code changed by this audit.

## Verdict

**No. A live run cannot be attempted today.** The CLI's `run start` command fails
immediately and unconditionally, before it even parses the run configuration file,
because nothing in production code ever wires a `RunnerProtocol` implementation into
`operator/cli.py`. This was reproduced directly:

```
$ uv run civsim run start configs/turn50-validation.yaml
no runner is configured -- civsim_harness.run.runner (T116) is a later wave's
addition; call civsim_harness.operator.cli.configure_runner_factory(...) to wire
one before invoking a `run` subcommand (see operator/runner_protocol.py).
(exit code 1)
```

Every component downstream of that point -- preparation/preflight, transport,
turn cycle, decision loop, observe/act, provider, store-for-a-run, saves -- is
individually well-built and unit/integration-tested against fakes, but **none of
it is composed together outside test fixtures.** There is no composition-root
module anywhere in `src/` that builds a `RunnerDependencies`, a
`TurnCycleDependencies`, or a `DecisionLoopContext` from real collaborators
(`NexusClient`, `CapabilityRegistry`, `OpenRouterProvider`, `SqliteMatchStore`,
a host-backed `ObservationReader`/`ActionExecutor`). The 933 passing tests
exercise every module in isolation, correctly, against hand-built fakes -- they
do not exercise the wiring, because the wiring does not exist yet.

The one genuinely live-wired path in the whole codebase is `civsim doctor`,
which really does construct a `NexusClient`, call `.connect()`, and (if a game
is loaded) `.resolve_game_states()`, against `operator/doctor.py`.

## Component Table

| Component | Status | Evidence |
|---|---|---|
| CLI -> `RunnerProtocol` wiring | **MISSING** | `operator/cli.py:121-141` -- `_runner_factory` defaults to `None`; `configure_runner_factory()` is defined but called nowhere in `src/` (only in `tests/unit/test_operator_api.py`). `_get_runner()` (`cli.py:132-141`) raises before `run_start` (`cli.py:230-235`) even loads the config file. Reproduced live: `uv run civsim run start configs/turn50-validation.yaml` -> exit 1, "no runner is configured". |
| `operator/commands.py::start` | WIRED (as far as it goes) | `commands.py:72-79` is a correct thin passthrough to `RunnerProtocol.start` -- but there is no production `RunnerProtocol` to receive the call. |
| `run/runner.py::Runner` | SEAM ONLY | `Runner.__init__`/`start()` (`runner.py:171-204`) are complete and correct against `RunnerDependencies` (`runner.py:105-130`), but `RunnerDependencies(...)` is constructed **only** in `tests/unit/test_runner.py`, `tests/integration/test_stop_conditions.py`, `tests/integration/test_provider_resilience.py`, `tests/unit/test_operator_api.py`. Zero production constructors. `Runner.branch`/`Runner.resume_from` (`runner.py:282-301`) explicitly `raise HarnessError("not implemented in this wave")`. |
| `run/preparation.py` (build-pin, catalog, turn-timer, debug-menu, leader-selection preflights) | SEAM ONLY | `build_pin_preflight` (`preparation.py:155`), `catalog_preflight` (`:341`), `turn_timer_preflight` (`:565`), `debug_menu_preflight` (`:425`), `apply_and_verify_leader_selection` (`:750`), `verify_configuration` (`:287`) are all self-contained, well-tested functions. Grepping the whole `src/` tree for calls to any of them turns up **no caller** -- they are referenced only by their own module and by test files. Nothing composes them into a `prepare_run` callable for `RunnerDependencies`. |
| `observe/host_gate.py::evaluate_host_gate` | SEAM ONLY | `host_gate.py:40`. Its own docstring (`:59-65`) names `run/preparation.py` as the "intended call site" -- but `preparation.py` never imports or calls it. Only caller anywhere is `tests/contract/test_run_configuration.py:284,293,302` (one of the two test files a concurrent agent is currently editing). |
| Leader-selection path (`apply_and_verify_leader_selection`) | SEAM ONLY, and its own injected writer has no implementation anywhere | `preparation.py:711` (`LeaderSelectionApplier` type) states explicitly: "no implementation of this type exists anywhere in this repository." Nothing constructs one, live or fake-for-production. |
| `NexusClient` construction / `connect()` / `resolve_game_states()` | **WIRED, but only inside `civsim doctor`** | `nexus/client.py:247` (class), `:303` (`connect`), `:423` (`resolve_game_states`), `:473` (`refresh_state_indices`). Production construction found in exactly one place: `operator/doctor.py:303-305` (`nexus_client_factory ... else NexusClient`), used by `_probe_tuner` (`doctor.py:175-193`). Verified live in this environment: `uv run civsim doctor` -> `tuner connection: unreachable (Could not connect to the Nexus tuner interface)`, reached genuinely, not stubbed. |
| `refresh_state_indices()` at menu->in-game boundary | **MISSING** | Zero production callers anywhere. Only referenced in `nexus/client.py` itself, its own contract doc, and test files (`tests/unit/test_nexus_client.py`, `tests/integration/test_recovery.py`, `tests/fakes/*`). Nothing in this codebase re-resolves indices at the phase boundary the live finding says matters. |
| `run/turn_cycle.py::run_turn_cycle` / `TurnCycleDependencies` | SEAM ONLY | Dataclass at `turn_cycle.py:135-162`; logic (`_take_quicksave`, the attempt loop, backstop end-turn) is complete and carefully reasoned. `TurnCycleDependencies(...)` is constructed only in 8 test files (`tests/unit/test_runner.py`, `tests/integration/test_stop_conditions.py`, `test_recovery.py`, `test_provider_resilience.py`, `test_turn_cycle.py`, `test_turn_endings.py`, `tests/unit/test_turn_state_machine.py`, `tests/integration/test_decision_loop.py`). No production constructor. |
| `run/decision_loop.py::DecisionLoopContext` | SEAM ONLY | Dataclass at `decision_loop.py:181-210`. Constructed only in the same 8 test files plus `tests/integration/test_prompts.py`, `tests/unit/test_no_truncation.py` -- 10 test files total, zero production. |
| `ObservationReader` / `ActionExecutor` protocols | **MISSING a production implementation** | Defined at `decision_loop.py:151` and `:166`. Referenced by name only in `run/runner.py` (docstring/comment) and `run/decision_loop.py` itself. No class anywhere implements either against `NexusClient.execute_command`. |
| `observe/assemble.py::assemble_observation` | WIRED as a pure function, but its only real input source is unbound | `assemble.py` is correctly invoked by `decision_loop.py` (`_observe`, `:236-282`) and `turn_cycle.py` (`_dispatch_backstop_end_turn`, `:228-238`) -- but both call sites feed it from `ctx.read_observation_inputs()`, i.e. the unbound `ObservationReader` above. No path from a catalog capability to a live Nexus call exists. |
| `capability/registry.py` -> `nexus/client.py` dispatch | **MISSING** | `CapabilityRegistry` (`registry.py:41-95`) is lookup + wrong-context refusal only, by its own docstring ("does not talk to Nexus itself... that is... a later dispatch wave", `registry.py:14-16`). `IntegrationCapability.implementation_ref` (`models/catalog.py:134`) holds real values like `lua/gamecore/map.lua` (`catalogs/capabilities.yaml:17`), but `implementation_ref` is read nowhere else in `src/` -- grep for the string across `src/` returns only its own model-field definition. Nothing loads a `lua/*.lua` file or turns it into a `lua_body` for `NexusClient.execute_command` (`nexus/client.py:508-514`). |
| `lua/` files dispatched | **MISSING** | 12 files under `lua/gamecore/` and `lua/ingame/` exist and are referenced by catalog YAML comments/`implementation_ref` values only. No Python code path reads them. |
| `provider/chain.py::ProviderChain` | SEAM ONLY | Class is complete (retry/fallback/no-image-drop policy, `chain.py`). Constructed only in `tests/unit/test_runner.py`, `tests/integration/test_provider_resilience.py`, `tests/contract/test_provider_resilience_contract.py`, `tests/unit/test_model_swap.py`, `tests/contract/test_model_provider_port.py`. Zero production construction. |
| `provider/openrouter.py::OpenRouterProvider` | SEAM ONLY | Class defined at `openrouter.py:118`. Constructed only in `tests/unit/test_openrouter_adapter.py`. Never instantiated in production. |
| `provider/preflight.py::preflight_chain` | **MISSING a caller** | Defined at `preflight.py:84`. Zero calls anywhere outside its own module. Not called before "turn 1" because there is no "turn 1" composition path to call it from. |
| `store/sqlite_adapter.py::SqliteMatchStore` | **WIRED, but only for `doctor`/`audit`/`saves`, not for a run** | Constructed in production at `operator/cli.py:183` (`_open_store`), used by `doctor`, every `audit` subcommand, and `saves reap`. It is real, working infrastructure -- verified live (`uv run civsim doctor` -> `store: ok`). It is never passed into a `RunnerDependencies.store` in production because no such construction exists. |
| `saves/save_game.py::LuaSaveCapability` | SEAM ONLY | Class at `save_game.py:189`. Requires a `state_index_source` (an `InGameStateIndexSource`, not a bare int -- `save_game.py:123-136`, the fix a concurrent agent's edits target). Constructed only in `tests/unit/test_save_game.py`. No production constructor exists to pass it a real `NexusClient`-shaped source. |

## What would happen if someone ran it today

1. Operator runs `civsim run start <config>.yaml` against the live host.
2. `operator/cli.py::run_start` calls `_get_runner()` **before** touching the config file at all (`cli.py:233`, `cli.py:132-141`).
3. `_runner_factory` is `None` (nothing in any production code path ever calls `configure_runner_factory`).
4. The CLI prints `"no runner is configured -- civsim_harness.run.runner (T116) is a later wave's addition..."` to stderr and exits 1.
5. **Nothing else happens.** No config is loaded, no build-pin check, no host gate, no Nexus connection, no leader selection, no turn is attempted. This is the actual, reproduced first failure (see the Verdict section's transcript).

This is a wiring failure, not a logic or environment failure -- it would fail identically on the live Linux box with a running client, tuner, and API key, exactly as it failed here with none of those present.

## Critical path to a first live turn (shortest first)

1. **Write a composition root** (a new module, e.g. `run/__main__.py` or similar) that, at process start, calls `operator.cli.configure_runner_factory(...)` with a factory returning a real `Runner(RunnerDependencies(...))`. This is the single blocking item -- without it nothing downstream can ever be reached, regardless of how complete those downstream pieces are.
2. **Build `RunnerDependencies.prepare_run`**: a function that loads the seed set, calls `build_pin_preflight`, `catalog_preflight`, `debug_menu_preflight`, `turn_timer_preflight`, `evaluate_host_gate`, `apply_configuration`/`verify_configuration`, and `apply_and_verify_leader_selection` in sequence against a real, connected `NexusClient`, producing a `PreparedRun`. The leader-selection step is blocked on a still-unknown live Lua call (`preparation.py:696-710` says so explicitly) -- this needs the pending live spike to land before it can be wired for real, or `run start` will legitimately fail every time at that gate (which is honest, not a bug).
3. **Implement a production `ObservationReader`** that, for each capability a step's catalog declarations need, reads `IntegrationCapability.implementation_ref`, loads the corresponding `lua/*.lua` body, calls `NexusClient.execute_command(state_index=..., lua_body=...)`, and returns `CapabilityResult`s for `assemble_observation`. This is the largest missing piece by volume of new code.
4. **Implement a production `ActionExecutor`** the same way, for the action side.
5. **Call `NexusClient.refresh_state_indices()` at the menu->in-game boundary** inside step 2's `prepare_run` (after the game is loaded, before turn 1) -- currently nothing calls it anywhere.
6. **Build `RunnerDependencies.build_turn_dependencies`**: construct one `TurnCycleDependencies` per turn, wiring in a real `LuaSaveCapability` (now requiring the state-index source from step 5, not a bare int), the real `HostPlatform`, and a `build_loop_context` closure producing `DecisionLoopContext` with the observation reader/action executor from steps 3-4, a real `CapabilityRegistry` from the loaded catalog, and a real `ProviderChain` wrapping `OpenRouterProvider` (needs `OPENROUTER_API_KEY` set on the Linux box -- ground truth says it is not there yet).
7. **Call `preflight_chain`** once at composition time (or inside `prepare_run`) before turn 1, so a broken/missing key fails fast and recorded rather than on the first decision.
8. **Wire `SqliteMatchStore`** (already proven working via `civsim doctor`) into `RunnerDependencies.store` -- trivial once the rest exists, since `operator/cli.py:_open_store` already shows the working construction.

Steps 3+4 (real observation/action I/O against Nexus) are the largest genuinely unbuilt pieces; step 2's leader-selection sub-step is blocked on external information (the live spike), not on engineering effort.

## Known-degraded-but-acceptable

- **Capture pixel extraction is stubbed on all adapters** -- confirmed in `host/linux/adapter.py:169-216`: `_capture_via_xcomposite` redirects the window but returns `CaptureStatus.failed` before pixmap readback ("not implemented"); `_capture_via_portal` raises `NotImplementedError` outright. This correctly degrades to `visually_degraded` under FR-050/`ComparabilityStatus.VISUALLY_DEGRADED` (`observe/host_gate.py:88-92`) rather than blocking a run. This is an honest recorded state, not a blocker, and is out of scope for this audit's critical path.
- **`civsim doctor` and `civsim audit *`/`saves reap`** are genuinely wired end-to-end today (verified live) and can be used as-is against the live host right now for diagnostics, independent of the `run start` blocker above.
- **Turn-timer precondition's `UNVERIFIED` state** (`preparation.py:539-563`) is a legitimate, by-design "proceed but record the gap" outcome, not a defect -- once wired.
- **Debug-menu setting** is by design never a gate (`preparation.py:425-482`) -- recorded, never enforced, per the live spike's own finding.

## Verification run (this audit, no code changed)

```
uv run pytest tests/unit tests/contract tests/integration tests/fakes -q
  -> 933 passed, 3 skipped, 3 warnings in 56.66s

uv run civsim --help
  -> lists version, doctor, run, audit, saves, seedset -- CLI itself loads and dispatches correctly.

uv run civsim doctor
  -> platform: ok (windows, tier UNSUPPORTED)
     tuner connection: unreachable (Could not connect to the Nexus tuner interface)
     client: not running
     store: ok
     catalog: ok (version 2026.09.1, 51 declarations, 0 undeclared)
     capture path: none (runs will be visually degraded)
     provider key: MISSING
     disk headroom: 899.1 GB free

uv run civsim run start configs/turn50-validation.yaml
  -> "no runner is configured..." / exit code 1  (reproduced live, see Verdict)
```

Test counts are unchanged from before this audit (no source, test, or config file was modified).
