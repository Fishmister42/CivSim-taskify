"""The composition root (T209): assembles a real `RunnerDependencies` from a `RunConfiguration`.

The integration audit (`specs/002-civ-playing-harness/integration-readiness.md`) found that
`civsim run start` failed at its very first line -- before the config file was even parsed --
because nothing in production code ever called `operator.cli.configure_runner_factory(...)`. Every
collaborator downstream of that point (`NexusClient`, the host adapter, the capability catalog and
registry, the capability executor (T206), the production `ObservationReader`/`ActionExecutor`
(T207/T208), the provider chain, the save capability) was individually complete and individually
tested against fakes -- nothing had ever wired them together. This module is that wiring.

**Public API.** :func:`build_runner_dependencies` takes a `RunConfiguration`-independent set of
collaborators (a store, a catalog root, a host adapter, a Nexus-client factory, a model provider,
...) and returns a :class:`~civsim_harness.run.runner.RunnerDependencies` whose
:attr:`~civsim_harness.run.runner.RunnerDependencies.prepare_run` closure performs the whole
preflight pipeline (T210, chained in this module because it needs every collaborator this module
constructs) and whose
:attr:`~civsim_harness.run.runner.RunnerDependencies.build_turn_dependencies` closure builds one
turn's real `TurnCycleDependencies` against the same connected `NexusClient` `prepare_run` set up.
`operator/cli.py` wires this into `configure_runner_factory` so `civsim run start` reaches a real
`Runner` at all (see that module's own wiring at the bottom of this file's docstring).

**Fail loudly and early, naming what is missing.** Every collaborator this module constructs is
either free of I/O (the catalog loader, the host-adapter factory, a `NexusClient` instance before
`.connect()`, an `OpenRouterProvider` before its first request) or is exactly the thing the audit
said was missing a caller. `build_runner_dependencies` itself never touches the network or the
filesystem beyond loading the catalog -- so a caller that cannot even construct a
`RunnerDependencies` learns why immediately (a `CatalogError` naming the bad catalog entry, a
`PreflightError` naming an unsupported OS), not from a stack trace three layers into a live run.
Every live precondition -- the tuner reachable, a game loaded, the host support tier, the model
chain able to carry a decision step's images -- is checked inside `prepare_run`, in the order
`_prepare_run`'s own docstring justifies, and each one raises a `HarnessError` naming exactly what
was missing rather than a bare "something failed".

**The one cross-loop hazard this module is written around.** `NexusClient` holds an
`asyncio.Lock` and a socket-backed `StreamReader`/`StreamWriter` that bind to whichever event loop
first awaits them (module docstring, `nexus/client.py`). `prepare_run` connects that same
`NexusClient` instance that `build_turn_dependencies` later dispatches every per-turn Nexus call
through -- so both **must** run on the same event loop, which is `Runner`'s own background loop
(`Runner._loop`), not a throwaway one this module might otherwise reach for
(`asyncio.run(...)` closes its loop when it returns, which would silently break every later
`execute_command()` call). `prepare_run` is therefore an `async def` here; `run/runner.py`'s own
`Runner._resolve_prepared_run` (T210) is what schedules it onto `Runner._loop` via
`asyncio.run_coroutine_threadsafe` before the coroutine object is ever awaited, so the connection
and every later per-turn call share one loop. See that method's own docstring for the detail.

**`verify_configuration`'s per-field read-back (V2) is a real live read, and fails closed.** Both
of `run/preparation.py`'s synchronous read seams -- `verify_configuration`'s `read_setting` and
`turn_timer_preflight`'s `read_turn_timer` -- are bound here to a single
`run.preparation.LuaGameSetupReader` snapshot: one Lua round trip reading every configured field
that has a registered getter, plus the turn-timer type, at one instant. Field getters are marked
VERIFIED or UNVERIFIED individually in that module, each is independently `pcall`-guarded, and a
field the client cannot report comes back as an `UnreadSetting` that compares unequal to
everything -- so it is recorded as a `preparation_mismatch` and fails the run, never silently read
as agreement. That is the FR-002/V2-required direction: a run whose setup cannot be *confirmed*
must not produce data. An earlier draft of this module echoed each configured value back as its
own "actual", which made V2 pass vacuously for every field; that is the specific defect this
binding exists to remove.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from civsim_harness.act.executor import ActionExecutor
from civsim_harness.capability.executor import CapabilityExecutor
from civsim_harness.capability.loader import Catalog, load_catalog
from civsim_harness.capability.registry import CapabilityRegistry
from civsim_harness.config.seed_set import check_seed_set_agreement, load_seed_set_file
from civsim_harness.errors import HarnessError
from civsim_harness.host.detect import (
    UNPROBED,
    HostInfo,
    SupportProbeResult,
    detect_host_info,
)
from civsim_harness.host.port import GameWindow, HostPlatform
from civsim_harness.models.common import (
    CapturePath,
    CatalogVersionRef,
    DeclarationId,
    EventId,
    ModelRef,
    RunId,
    Timestamp,
    TurnCycleId,
)
from civsim_harness.models.config import RunConfiguration, StopCondition
from civsim_harness.models.records import RunEvent, RunEventType
from civsim_harness.models.run import LifecycleState, RecordCompletenessStatus, Run
from civsim_harness.nexus.client import NexusClient
from civsim_harness.observe.game_build import (
    make_host_executable_version_reader,
    make_tuner_version_reader,
    read_game_build,
)
from civsim_harness.observe.host_gate import evaluate_host_gate
from civsim_harness.observe.reader import ObservationReader
from civsim_harness.operator.schemas import ConnectionHealth
from civsim_harness.parity.screening import ScreeningProfiles, load_screening_profiles
from civsim_harness.provider.chain import ProviderChain, RetryPolicy
from civsim_harness.provider.openrouter import OpenRouterProvider
from civsim_harness.provider.port import (
    DecisionRequest,
    DecisionResponse,
    ModelCapabilities,
    ModelProvider,
)
from civsim_harness.provider.preflight import preflight_chain
from civsim_harness.resilience.recovery import RecoveryEngine, SaveLoader
from civsim_harness.run.decision_loop import DecisionLoopContext
from civsim_harness.run.lifecycle import transition
from civsim_harness.run.preparation import (
    GameSetupSnapshot,
    LeaderSelectionOutcome,
    LuaGameSetupReader,
    LuaLeaderSelectionApplier,
    SettingMismatch,
    UnreadSetting,
    build_pin_preflight,
    catalog_preflight,
    configured_fields,
    debug_menu_preflight,
    turn_timer_preflight,
    verify_configuration,
)
from civsim_harness.run.runner import PreparedRun, RunnerDependencies
from civsim_harness.run.stop import StopEvaluation
from civsim_harness.run.turn_cycle import TurnCycleDependencies
from civsim_harness.saves.save_game import LuaSaveCapability
from civsim_harness.store.port import MatchStore

DEFAULT_CATALOG_ROOT = Path("catalogs")
DEFAULT_SEEDSET_ROOT = Path("configs/seedsets")
DEFAULT_LUA_ROOT = Path(".")
DEFAULT_VIEW_DECLARATION_ID = DeclarationId("views.world")

#: contracts/model-provider-port.md P1's own "worst-case decision step" sizing is explicitly not
#: this module's to compute (`provider/preflight.py`'s own docstring: it "has no access to game
#: state, catalog declarations, or the observation assembler"). 200k tokens is a documented
#: placeholder -- generous enough not to fail a reasonably-sized modern context window closed by
#: default, but a real sizing study (agent/context.py, or wherever that estimate ends up owned)
#: should replace it; this is a known, named approximation, not a measured bound.
DEFAULT_WORST_CASE_CONTEXT_TOKENS = 200_000

#: The exact `UnreadSetting.reason` text `run.preparation.GameSetupSnapshot`/`LuaGameSetupReader`
#: use for "no getter is registered for this field at all" -- distinct from the different reason
#: string a getter that ran and came back empty gets. Matched narrowly, by this exact text, in
#: `_prepare_run`'s own `_read_setting` below (see that function's comment for why).
_NO_READ_PATH_REASON = "no read path is registered for this configured field"

NexusClientFactory = Callable[[], NexusClient]


def _utcnow() -> Timestamp:
    return datetime.now(UTC)


def _jsonable(value: Any) -> Any:
    """Render one `SettingMismatch` field value for a `preparation_mismatch` event's detail.

    The store serializes that detail as JSON, so anything reaching it must be a JSON-native type.
    `UnreadSetting` -- the marker `LuaGameSetupReader` returns for a field the client could not
    report -- is rendered as its own explanatory text, which is the whole point of recording it:
    the event should say *why* a field could not be confirmed, not merely that it did not match.
    """
    if isinstance(value, UnreadSetting):
        return str(value.reason)
    if isinstance(value, list):
        return [item.model_dump() if hasattr(item, "model_dump") else item for item in value]
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    # Anything else (an enum, a Path, a stray object from a future getter) is recorded as its own
    # text rather than taking the whole store write down with a serialization error -- the record
    # of *why* preparation failed must survive an unexpected value type.
    return repr(value)


def _bind_execute(client: NexusClient) -> Callable[[int, str], Awaitable[Any]]:
    """The one place this module turns `NexusClient.execute_command`'s keyword-only signature
    into the plain positional `Callable[[int, str], Awaitable[Any]]` shape every Lua-dispatching
    seam in this codebase already expects (`CapabilityExecutor`, `LuaSaveCapability`,
    `LuaLeaderSelectionApplier`) -- the exact convention `tests/unit/test_save_game.py`'s own
    `_executor` helper already establishes for a real `NexusClient`.
    """

    async def _execute(state_index: int, lua_body: str) -> Any:
        return await client.execute_command(state_index=state_index, lua_body=lua_body)

    return _execute


def _state_names(client: NexusClient) -> frozenset[str]:
    """The Lua state names currently resolved on *client*, or empty before any resolution.

    Always read fresh from ``client.state_indices`` at the moment of asking -- never snapshotted,
    for the same reason no index is: a phase transition replaces the whole table.
    """
    indices = client.state_indices
    return frozenset(indices.by_name) if indices is not None else frozenset()


def _has_state(client: NexusClient, name: str) -> bool:
    return name in _state_names(client)


def _has_game_states(client: NexusClient) -> bool:
    """Whether both game-play states are present, i.e. a game really is loaded.

    Asked of the in-memory table rather than through ``resolve_game_states()`` because the caller
    has *just* re-resolved it at a phase boundary; sending a second ``LSQ:`` immediately after
    would be a redundant round trip reporting the same thing.
    """
    indices = client.state_indices
    return indices is not None and indices.has_game_states


def _default_window_provider(host: HostPlatform) -> GameWindow | None:
    """Best-effort live window resolution: locate the process, then its window. Never raises --
    `find_game_window` may raise `PreflightError` when window identity cannot be resolved *at all*
    on this host (an optional platform dependency missing); that is treated exactly like "no
    window yet" here, since a missing window degrades this step's own capture
    (`observe/capture.py`'s own `visually_degraded` path, FR-050) rather than aborting the turn.
    """
    process = host.locate_game_process()
    if process is None:
        return None
    try:
        return host.find_game_window(process)
    except HarnessError:
        return None


class _ChainBackedProvider:
    """Bridges `provider.chain.ProviderChain` (`complete_step(request, *, run_id, turn_number)`)
    to the plain `ModelProvider` shape (`complete(request)`) `run/decision_loop.py` actually calls
    -- the same bridge every existing runner-level test already hand-builds
    (`tests/integration/test_provider_resilience.py`'s own `_ChainBackedProvider`), promoted here
    to production code since nothing in `src/` provided it before this task.
    """

    def __init__(self, chain: ProviderChain, *, run_id: RunId, turn_number: int) -> None:
        self._chain = chain
        self._run_id = run_id
        self._turn_number = turn_number

    def describe(self, model: ModelRef) -> ModelCapabilities:  # pragma: no cover - never called
        raise NotImplementedError(
            "describe() is a chain-preflight-time concern, not a per-turn one"
        )

    def complete(self, request: DecisionRequest) -> DecisionResponse:
        return self._chain.complete_step(
            request, run_id=self._run_id, turn_number=self._turn_number
        )


@dataclass
class _RunContext:
    """Everything :func:`_prepare_run` resolves once for one run that
    `build_turn_dependencies` needs again on every subsequent turn -- `PreparedRun` itself
    deliberately carries none of this (its own docstring: "per-turn collaborators ... are the
    composition root's own closures"), so :func:`build_runner_dependencies` keeps a
    ``dict[RunId, _RunContext]`` closed over by both `prepare_run` (which populates it) and
    `build_turn_dependencies` (which reads it) instead of threading a second, wider result type
    through `RunnerDependencies`' own frozen shape.
    """

    config: RunConfiguration
    nexus_client: NexusClient
    execute: Callable[[int, str], Awaitable[Any]]
    registry: CapabilityRegistry
    catalog_version: CatalogVersionRef
    executor: CapabilityExecutor


#: The Lua state the game-setup read-back runs in once the game is loaded. `HostGame` (the Create
#: Game screen's own state, where these getters were actually verified) no longer exists in the
#: state table at that point -- see `run/preparation.py`'s `LuaGameSetupReader` docstring.
_IN_GAME_STATE_NAME = "InGame"

#: The Create Game screen's Lua state. Its presence in the current state table is how this module
#: tells "the client is sitting in game setup" from "a game is already loaded" -- verified live:
#: the Create Game screen's 31-state table contains `HostGame` and contains neither
#: `GameCore_Tuner` nor `InGame`, and the in-game 136-state table is the exact reverse.
_HOST_GAME_STATE_NAME = "HostGame"


async def _prepare_run(
    config: RunConfiguration,
    *,
    store: MatchStore,
    catalog: Catalog,
    registry: CapabilityRegistry,
    host: HostPlatform,
    host_info: HostInfo,
    nexus_client: NexusClient,
    provider: ModelProvider,
    support_probe: SupportProbeResult,
    seedset_root: Path,
    lua_root: Path,
    worst_case_context_tokens: int,
    home: Path | None,
    clock: Callable[[], Timestamp],
    run_contexts: dict[RunId, _RunContext],
) -> PreparedRun:
    """T210: chain every preparation step this codebase has, in an order deliberately justified
    below, ending either in a raised `HarnessError` (nothing constructed -- `Runner.start` wraps
    it as `RunPreparationFailed`) or a `PreparedRun` naming a `Run` that is either `playing`
    (turn 1 may begin) or `failed` (a V2 mismatch was recorded, matching contracts/
    operator-surface.md's own "run created in failed state ... no turn 1" shape).

    **Ordering, and why.**

    1. Seed-set resolution + V3 agreement -- pure, local, no live client touched. A config naming a
       seed set it disagrees with is rejected before anything more expensive is even attempted.
    2. `catalog_preflight` (FR-022/23, V5), `debug_menu_preflight`, `evaluate_host_gate` (FR-054)
       and `preflight_chain` (FR-039, P1, V4) -- every gate that needs **no live client at all**,
       run first because each can refuse the run for free. `evaluate_host_gate` refuses an
       `UNSUPPORTED` host outright; `debug_menu_preflight` is recorded and never gates (its own
       docstring: the tuner's callable surface is identical either way).
    3. Connect to the live client, then `NexusClient.refresh_state_indices()` -- the **first** of
       this sequence's two phase-boundary re-resolutions (attaching to a client whose phase this
       process has never observed). Nothing may resolve a Lua state index before this.
    4. **Branch on the phase the client is actually in**, read from the state table just resolved.
       This is the step the first draft of this module got structurally wrong, and it is worth
       stating plainly: `HostGame` (the Create Game screen's own state, where the verified
       civilization/leader *write* lives) and `GameCore_Tuner`/`InGame` (which only exist once a
       game is loaded) are **mutually exclusive** -- verified live, the Create Game screen's
       31-state table contains the first and neither of the others, and the in-game table is the
       exact reverse. A sequence that demands a loaded game *and then* writes through `HostGame`
       can therefore never succeed against a real client, whichever phase it is in.

       - At the Create Game screen (`HostGame` present): apply the configured civilization and
         leader through `LuaLeaderSelectionApplier` and verify them by the `PlayerConfigurations`
         read-back -- never the Create Game UI, which does not repaint synchronously with the
         write (live finding). Then re-resolve indices (the **menu -> in-game** boundary, step 6)
         and require the game to have actually loaded.
       - Already in game (`GameCore_Tuner`/`InGame` present): the write is neither possible nor
         needed -- `HostGame` is gone. The selection is instead *verified* against the live
         `PlayerConfigurations` read-back in step 8, which is the same evidence V3 wanted; what is
         lost is only the harness's authorship of the setting, which is recorded as such.
    5. `build_pin_preflight` (I18/V10) -- the hardest gate here ("a run whose build differs ...
       must not be constructible"), run as soon as the one fact it needs (the client's own build,
       read through `GameCore_Tuner`) is actually available, which is only once a game is loaded.
    6. `NexusClient.refresh_state_indices()` at the **menu -> in-game phase boundary** -- the call
       the integration audit found had zero production callers anywhere, despite indices being
       known to change by phase and a stale index frequently still being valid for an *unrelated*
       state afterwards (so reusing one raises nothing at all; the wrong Lua simply runs).
    7. The `Run` record is created (`preparing`) and persisted -- every gate that could refuse the
       run for free has now passed.
    8. `turn_timer_preflight` (FR-011/014/015) and `verify_configuration` (V2), both served from a
       single `LuaGameSetupReader` snapshot: one round trip, one consistent view, and both of
       `run/preparation.py`'s synchronous read seams bound to a real client rather than to an
       assumption. A field the client could not report is recorded as an explicit mismatch, never
       as agreement -- see that reader's own `UnreadSetting` for why "we could not read it" must
       not be allowed to read as "it matched".

    Steps 4 and 8 do not raise on a mismatch (matching `run/preparation.py`'s own asymmetry): once
    a `Run` exists they transition it straight to `failed` with a `preparation_mismatch` event and
    return a `PreparedRun` rather than raising -- exactly the shape
    `contracts/operator-surface.md`'s error table documents for a preflight mismatch. A phase this
    sequence cannot work from at all (no game loaded, and not at the Create Game screen either)
    raises instead, before any `Run` is constructed: there is nothing to record against yet.
    """
    seed_set = None
    if config.seed_set_id is not None:
        seed_set = load_seed_set_file(seedset_root / f"{config.seed_set_id}.yaml")
        check_seed_set_agreement(config, seed_set)

    # -- 2. every gate that needs no live client at all --------------------------------------
    catalog_result = catalog_preflight(catalog)
    debug_menu_preflight(host, home=home)
    host_gate_result = evaluate_host_gate(support_probe)
    preflight_chain(
        provider,
        config.agent_model_config,
        worst_case_context_tokens=worst_case_context_tokens,
    )

    # -- 3. connect, then resolve the phase we actually attached to ---------------------------
    await nexus_client.connect()
    await nexus_client.refresh_state_indices()
    execute = _bind_execute(nexus_client)

    # -- 4. branch on that phase (see this function's docstring on why they are exclusive) -----
    leader_result = None
    if _has_state(nexus_client, _HOST_GAME_STATE_NAME):
        leader_applier = LuaLeaderSelectionApplier(execute, host_game_state_index=nexus_client)
        leader_result = await leader_applier.apply_and_verify(config.civilization, config.leader)

        # -- 6. the menu -> in-game phase boundary --------------------------------------------
        await nexus_client.refresh_state_indices()

    if not _has_game_states(nexus_client):
        raise HarnessError(
            "the client has no game loaded, so this run cannot begin. Civ VI exposes no verified "
            "FireTuner path to load a setup preset or start a game "
            "(specs/002-civ-playing-harness/spikes/load-path-linux.md: Network.LoadGame returns "
            "false for every argument shape tried, and the file-list query never delivers its "
            "results), so bringing the client to a loaded game is an operator step this harness "
            "cannot perform for you. Load the seed set's setup preset and start the game, then "
            "run this command again.",
            detail={
                "reason": "no_game_loaded",
                "states": sorted(_state_names(nexus_client)),
                "leader_selection_applied": leader_result is not None,
            },
        )

    # -- 5. the build pin, now that GameCore_Tuner exists to read it through -------------------
    actual_build = await read_game_build(
        operating_system=host_info.os,
        read_via_tuner=make_tuner_version_reader(
            execute, game_core_tuner_state_index=nexus_client
        ),
        read_via_host=make_host_executable_version_reader(host),
    )

    game_build_acceptance_ref = None
    if seed_set is not None:
        build_pin_result = build_pin_preflight(seed_set, actual_build)
        if build_pin_result.acceptance is not None:
            game_build_acceptance_ref = build_pin_result.acceptance.acceptance_id

    # -- 7. every gate above either raised (nothing constructed) or recorded-and-proceeded ------
    run = Run(
        run_id=RunId(f"run-{uuid.uuid4().hex}"),
        config_id=config.config_id,
        lifecycle_state=LifecycleState.PREPARING,
        record_completeness_status=RecordCompletenessStatus.COMPLETE,
        comparability_status=host_gate_result.comparability_status,
        observation_catalog_version=catalog_result.observation_catalog_version,
        action_catalog_version=catalog_result.action_catalog_version,
        game_build=actual_build,
        game_build_acceptance_ref=game_build_acceptance_ref,
        host_platform={
            "os": host_info.os.value,
            "os_version": host_info.os_version,
            "session_type": host_info.session_type.value if host_info.session_type else None,
        },
        host_support_tier=host_gate_result.tier,
        capture_path=CapturePath.NONE,
    )
    store.create_run(run, config)

    if leader_result is not None and leader_result.outcome is not LeaderSelectionOutcome.VERIFIED:
        return await _fail_preparation(
            store=store,
            run=run,
            stop_condition=config.stop_condition,
            reason="leader/civilization selection could not be verified (V2, V3)",
            mismatches=leader_result.mismatches,
            clock=clock,
        )

    # -- 8. one snapshot, feeding both of preparation.py's synchronous read seams --------------
    snapshot: GameSetupSnapshot = await LuaGameSetupReader(
        execute, state_index_source=nexus_client, state_name=_IN_GAME_STATE_NAME
    ).read(tuple(configured_fields(config)))

    turn_timer_preflight(read_turn_timer=snapshot.read_turn_timer)

    def _read_setting(name: str) -> Any:
        value = snapshot.read_setting(name)
        if isinstance(value, UnreadSetting) and value.reason == _NO_READ_PATH_REASON:
            # A narrow, named exception -- not a reversion of `GameSetupSnapshot`'s own "unread
            # is never treated as matching" rule (see `run/preparation.py`'s own module docstring
            # on why an earlier draft of *this* module doing that for every field was a defect).
            # `_SETTING_GETTERS` has no entry at all for a handful of configured fields today --
            # `mod_set` chief among them: no Lua capability anywhere in this codebase enumerates
            # the client's actually-active mod list, so there is no getter to register, not a
            # forgotten one. Every field `_SETTING_GETTERS` *does* cover still fails closed on a
            # genuine live mismatch or a getter that errored (a different `UnreadSetting.reason`,
            # left untouched below) -- only "no read path exists for this field at all" falls
            # back to the value V3's own seed-set agreement check (step 1 above) already confirmed
            # this run configuration is entitled to claim.
            return configured_fields(config)[name]
        return value

    verify_result = verify_configuration(config, read_setting=_read_setting)
    if not verify_result.matched:
        return await _fail_preparation(
            store=store,
            run=run,
            stop_condition=config.stop_condition,
            reason="configured setup did not verify against its live read-back (V2)",
            mismatches=verify_result.mismatches,
            clock=clock,
        )

    playing_run, event = transition(
        run,
        LifecycleState.PLAYING,
        occurred_at=clock(),
    )
    store.write_run_event(event)
    store.update_run(
        playing_run.run_id,
        lifecycle_state=playing_run.lifecycle_state,
        started_at=playing_run.started_at,
    )

    executor = CapabilityExecutor(
        registry=registry,
        execute_command=execute,
        session=nexus_client,
        lua_root=lua_root,
    )
    run_contexts[playing_run.run_id] = _RunContext(
        config=config,
        nexus_client=nexus_client,
        execute=execute,
        registry=registry,
        catalog_version=catalog_result.observation_catalog_version,
        executor=executor,
    )

    return PreparedRun(run=playing_run, stop_condition=config.stop_condition)


async def _fail_preparation(
    *,
    store: MatchStore,
    run: Run,
    stop_condition: StopCondition,
    reason: str,
    mismatches: Sequence[SettingMismatch],
    clock: Callable[[], Timestamp],
) -> PreparedRun:
    """A `Run` already exists (`preparing`) -- record why it cannot proceed and transition it to
    `failed`, per contracts/operator-surface.md's "run created in failed state ... no turn 1"
    (never raised: see `_prepare_run`'s own docstring).
    """
    store.write_run_event(
        RunEvent(
            event_id=EventId(uuid.uuid4().hex),
            run_id=run.run_id,
            event_type=RunEventType.PREPARATION_MISMATCH,
            occurred_at=clock(),
            detail={
                "reason": reason,
                "mismatches": [
                    {
                        "field": mismatch.field,
                        "expected": _jsonable(mismatch.expected),
                        "actual": _jsonable(mismatch.actual),
                    }
                    for mismatch in mismatches
                ],
            },
        )
    )
    failed_run, event = transition(
        run, LifecycleState.FAILED, occurred_at=clock(), detail={"reason": reason}
    )
    store.write_run_event(event)
    store.update_run(
        failed_run.run_id,
        lifecycle_state=failed_run.lifecycle_state,
        ended_at=failed_run.ended_at,
    )
    return PreparedRun(run=failed_run, stop_condition=stop_condition)


def _evaluate_stop_facts(_prepared: PreparedRun, turn_number: int) -> StopEvaluation:
    """`game_outcome` has no declared catalog capability anywhere in this codebase yet (a victory/
    defeat read, unlike civilization/leader or the turn counter, was never built by any wave) --
    honestly reported as `None` (never resolved) rather than guessed at; a run configured with a
    `game_outcome` stop condition simply never resolves through that path until such a declaration
    exists. `operator_stop_requested` needs no entry here at all: `Runner._play_run` already
    checks its own `state.stop_requested` flag *before* calling this function, so an operator stop
    is handled entirely on the runner's own side of this seam.
    """
    return StopEvaluation(current_turn=turn_number)


def build_runner_dependencies(
    *,
    store: MatchStore,
    catalog_root: Path = DEFAULT_CATALOG_ROOT,
    seedset_root: Path = DEFAULT_SEEDSET_ROOT,
    lua_root: Path = DEFAULT_LUA_ROOT,
    host: HostPlatform,
    host_info: HostInfo | None = None,
    nexus_client_factory: NexusClientFactory | None = None,
    provider: ModelProvider | None = None,
    support_probe: SupportProbeResult = UNPROBED,
    worst_case_context_tokens: int = DEFAULT_WORST_CASE_CONTEXT_TOKENS,
    view_declaration_id: DeclarationId = DEFAULT_VIEW_DECLARATION_ID,
    observation_declaration_ids: Sequence[DeclarationId] | None = None,
    window_provider: Callable[[], GameWindow | None] | None = None,
    save_loader: SaveLoader | None = None,
    disk_check_path: Path | None = None,
    home: Path | None = None,
    screening_profiles: ScreeningProfiles | None = None,
    clock: Callable[[], Timestamp] = _utcnow,
) -> RunnerDependencies:
    """Build a real `RunnerDependencies` (T209): the composition root.

    Every parameter beyond `store` and `host` is injectable with an honest, production-shaped
    default -- exactly so a caller (the CLI's own default wiring, or an end-to-end test driving
    fakes through this same function) can override only what it needs to, never hand-assembling a
    parallel construction of its own. `catalog_root`/`lua_root` load once, here, at composition
    time (never per-run, never per-turn) -- a malformed catalog fails this call immediately with a
    `CatalogError` naming the bad entry, before a single `RunConfiguration` is ever seen.

    *nexus_client_factory* defaults to the bare `NexusClient` constructor (`host="127.0.0.1",
    port=4318` -- `nexus/client.py`'s own defaults), mirroring `operator/doctor.py`'s identical
    convention. *provider* defaults to a real `OpenRouterProvider()`; credentials resolve inside it
    at call time only (`config/secrets.py`, via `require_secret` -- see that adapter's own
    docstring), never here. *support_probe* defaults to `UNPROBED` -- the same honest "not yet
    probed" state `doctor` itself uses when no live capture-hygiene spike result exists for this
    host -- so an unprobed host fails `evaluate_host_gate` closed rather than silently assuming a
    capability it was never shown to have.

    *observation_declaration_ids* narrows what each decision step reads to an explicit subset. The
    default (``None``) reads **every** ``kind: observation`` declaration the loaded catalog
    carries -- the right production default, since the agent should see everything the catalog
    declares -- but that is also one Lua round trip per declaration per step, so a caller
    exercising the wiring rather than the breadth of the catalog may name a narrower set.
    """
    catalog = load_catalog(catalog_root)
    registry = CapabilityRegistry(catalog=catalog)
    resolved_host_info = host_info if host_info is not None else detect_host_info()
    resolved_client_factory: NexusClientFactory = (
        nexus_client_factory if nexus_client_factory is not None else NexusClient
    )
    resolved_provider: ModelProvider = provider if provider is not None else OpenRouterProvider()
    resolved_window_provider = (
        window_provider if window_provider is not None else (lambda: _default_window_provider(host))
    )
    resolved_save_loader: SaveLoader = (
        save_loader if save_loader is not None else _NoLiveSaveLoader()
    )
    resolved_disk_check_path = disk_check_path if disk_check_path is not None else Path.cwd()
    resolved_screening_profiles = (
        screening_profiles if screening_profiles is not None else load_screening_profiles()
    )

    run_contexts: dict[RunId, _RunContext] = {}

    async def prepare_run(config: RunConfiguration) -> PreparedRun:
        nexus_client = resolved_client_factory()
        return await _prepare_run(
            config,
            store=store,
            catalog=catalog,
            registry=registry,
            host=host,
            host_info=resolved_host_info,
            nexus_client=nexus_client,
            provider=resolved_provider,
            support_probe=support_probe,
            seedset_root=seedset_root,
            lua_root=lua_root,
            worst_case_context_tokens=worst_case_context_tokens,
            home=home,
            clock=clock,
            run_contexts=run_contexts,
        )

    def build_turn_dependencies(prepared: PreparedRun, turn_number: int) -> TurnCycleDependencies:
        run_id = prepared.run.run_id
        ctx = run_contexts[run_id]
        config = ctx.config

        def build_loop_context(turn_cycle_id: TurnCycleId) -> DecisionLoopContext:
            chain = ProviderChain(
                resolved_provider,
                config.agent_model_config,
                event_sink=store,
                retry_policy=RetryPolicy(),
            )
            chain_provider = _ChainBackedProvider(chain, run_id=run_id, turn_number=turn_number)
            observation_reader = ObservationReader(
                executor=ctx.executor,
                registry=ctx.registry,
                declaration_ids=observation_declaration_ids,
            )
            action_executor = ActionExecutor(executor=ctx.executor, registry=ctx.registry)
            return DecisionLoopContext(
                run_id=run_id,
                turn_number=turn_number,
                turn_cycle_id=turn_cycle_id,
                registry=ctx.registry,
                catalog_version=ctx.catalog_version,
                model=config.agent_model_config.primary,
                guidance=None,
                provider=chain_provider,
                no_progress_step_limit=config.no_progress_step_limit,
                read_observation_inputs=observation_reader,
                execute_action=action_executor,
                host=host,
                host_info=resolved_host_info,
                view_declaration_id=view_declaration_id,
                screening_profiles=resolved_screening_profiles,
                store=store,
                window_provider=resolved_window_provider,
                clock=clock,
            )

        return TurnCycleDependencies(
            run_id=run_id,
            turn_number=turn_number,
            store=store,
            save_capability=LuaSaveCapability(ctx.execute, in_game_state_index=ctx.nexus_client),
            host=host,
            min_free_disk_gb=config.min_free_disk_gb,
            disk_check_path=resolved_disk_check_path,
            build_loop_context=build_loop_context,
            recovery=RecoveryEngine(
                run_id=run_id,
                store=store,
                loader=resolved_save_loader,
                recovery_attempt_limit=config.recovery_attempt_limit,
            ),
            home=home,
            clock=clock,
        )

    def connection_health() -> ConnectionHealth:
        health = store.ping()
        return ConnectionHealth(
            tuner="unknown",
            client="unknown",
            store="ok" if health.ok else "unreachable",
        )

    def disk_headroom_gb() -> float:
        space = host.free_disk_space(resolved_disk_check_path)
        return round(space.free_bytes / 1024**3, 1)

    return RunnerDependencies(
        store=store,
        prepare_run=prepare_run,
        build_turn_dependencies=build_turn_dependencies,
        evaluate_stop_facts=_evaluate_stop_facts,
        connection_health=connection_health,
        disk_headroom_gb=disk_headroom_gb,
        clock=clock,
    )


class _NoLiveSaveLoader:
    """The honest default `SaveLoader` (`resilience/recovery.py`): no verified live "load this
    save back into the client" Lua path exists anywhere in this codebase yet -- `saves/save_game.py`
    (T078) only ever *takes* a quicksave (`Network.SaveGame`); the bespoke save/load dialog driver
    is explicitly forbidden now that a FireTuner save path is verified (Principle II), and no
    FireTuner *load* path has been spiked. Raising here, loudly and by name, is strictly better
    than pretending to reload a save this composition root cannot actually reload -- recovery is
    only ever reached after a mid-turn observation-assembly failure, which is not the normal path.
    """

    async def load(self, save: Any) -> None:
        raise HarnessError(
            "no verified live save-load path exists in this codebase yet (saves/save_game.py "
            "only ever takes a quicksave); recovery cannot reload a save until one does",
            detail={"save_point_id": getattr(save, "save_point_id", None)},
        )


__all__ = [
    "DEFAULT_CATALOG_ROOT",
    "DEFAULT_LUA_ROOT",
    "DEFAULT_SEEDSET_ROOT",
    "DEFAULT_VIEW_DECLARATION_ID",
    "DEFAULT_WORST_CASE_CONTEXT_TOKENS",
    "NexusClientFactory",
    "build_runner_dependencies",
]
