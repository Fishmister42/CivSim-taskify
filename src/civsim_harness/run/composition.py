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

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from civsim_harness.act.executor import ActionExecutor
from civsim_harness.capability.executor import CapabilityExecutor
from civsim_harness.capability.loader import Catalog, load_catalog
from civsim_harness.capability.registry import CapabilityRegistry
from civsim_harness.config.guidance import load_guidance
from civsim_harness.config.run_config import BranchFrom
from civsim_harness.config.seed_set import check_seed_set_agreement, load_seed_set_file
from civsim_harness.errors import HarnessError
from civsim_harness.host.detect import (
    HostInfo,
    SupportProbeResult,
    detect_host_info,
    probe_host_support,
)
from civsim_harness.host.port import GameWindow, HostPlatform
from civsim_harness.models.common import (
    CapturePath,
    CatalogVersionRef,
    DeclarationId,
    EventId,
    LuaContext,
    ModelRef,
    RunId,
    Timestamp,
    TurnCycleId,
)
from civsim_harness.models.config import GuidanceSet, RunConfiguration, StopCondition
from civsim_harness.models.records import RunEvent, RunEventType
from civsim_harness.models.run import LifecycleState, RecordCompletenessStatus, Run
from civsim_harness.nexus.client import NexusClient
from civsim_harness.observe.assemble import CapabilityResult
from civsim_harness.observe.capture_paths import select_capture_path
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
from civsim_harness.resilience.detector import DetectionAggregator
from civsim_harness.resilience.heartbeat_monitor import HeartbeatMonitor
from civsim_harness.resilience.liveness import ProcessLivenessMonitor
from civsim_harness.resilience.recovery import RecoveryEngine, SaveLoader
from civsim_harness.run.decision_loop import DecisionLoopContext
from civsim_harness.run.detection import DEFAULT_DETECTION_INTERVAL_S, DetectionWatch
from civsim_harness.run.identity_lock import RunIdentityLock
from civsim_harness.run.lifecycle import TERMINAL_STATES, transition
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
from civsim_harness.run.stop import GameOutcome, StopEvaluation, evaluate_stop
from civsim_harness.run.turn_cycle import TurnCycleDependencies
from civsim_harness.saves.branching import BranchFrom as SaveBranchFrom
from civsim_harness.saves.branching import (
    BranchSource,
    create_branch,
)
from civsim_harness.saves.save_game import LuaSaveCapability
from civsim_harness.store.port import MatchStore
from civsim_harness.telemetry.logging import get_harness_logger, log_event

DEFAULT_CATALOG_ROOT = Path("catalogs")
DEFAULT_SEEDSET_ROOT = Path("configs/seedsets")
DEFAULT_LUA_ROOT = Path(".")
#: Where `RunConfiguration.guidance_set_id`'s source reference (e.g. ``GUIDEBOOK.md@a1b2c3d``,
#: contracts/run-configuration.md) is resolved from -- the repository root, the same place
#: `GUIDEBOOK.md` itself is expected to live (constitution Principle V).
DEFAULT_GUIDANCE_ROOT = Path(".")
DEFAULT_VIEW_DECLARATION_ID = DeclarationId("views.world")

#: catalogs/observations/game.yaml (T216): the local player's own victory/defeat state, and the
#: only thing `_evaluate_stop_facts` has to resolve FR-005's `game_outcome` stop condition from.
#: Read as part of each decision step's ordinary observation sweep -- never as a side-channel.
GAME_OUTCOME_DECLARATION_ID = DeclarationId("game.outcome_state")

#: catalogs/observations/camera.yaml (T221): where the camera is actually looking, read fresh per
#: decision step so a capture's `camera_state` can be checked against the view's declared
#: `camera_requirements` (FR-026) instead of being empty.
CAMERA_STATE_DECLARATION_ID = DeclarationId("camera.read_state")

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
    guidance: GuidanceSet | None = None
    """T219: the run's resolved `GuidanceSet`, loaded once at preparation and handed to every
    decision step's `DecisionLoopContext`. `None` only when the configuration names none."""

    detection: DetectionWatch | None = None
    """T233: this run's binding of research R12's detection signals (FR-044, FR-045, SC-010).

    Built once at preparation, against the same `NexusClient` this run dispatches through and the
    PID of the client process the host actually located, and handed to every turn's
    `TurnCycleDependencies`. `None` only when the client's socket is somehow unusable for a
    heartbeat *and* the host could not locate a process -- i.e. when there is genuinely no signal
    to read, which is recorded rather than faked."""

    last_game_outcome: GameOutcome | None = None
    """T216: the most recent `game.outcome_state` this run actually read, updated by
    :class:`_OutcomeTrackingObservationReader` on every decision step's observation sweep and
    consumed by `evaluate_stop_facts` after the turn ends. `None` means "no outcome has been
    observed", which is distinct from "the game reported that no outcome has occurred" only in
    that the former can also mean the declaration was never in this run's read set at all."""


#: The Lua state the game-setup read-back runs in once the game is loaded. `HostGame` (the Create
#: Game screen's own state, where these getters were actually verified) no longer exists in the
#: state table at that point -- see `run/preparation.py`'s `LuaGameSetupReader` docstring.
_IN_GAME_STATE_NAME = "InGame"

#: The Create Game screen's Lua state. Its presence in the current state table is how this module
#: tells "the client is sitting in game setup" from "a game is already loaded" -- verified live:
#: the Create Game screen's 31-state table contains `HostGame` and contains neither
#: `GameCore_Tuner` nor `InGame`, and the in-game 136-state table is the exact reverse.
_HOST_GAME_STATE_NAME = "HostGame"


# --------------------------------------------------------------------------
# T216 -- resolving the game's own outcome
# --------------------------------------------------------------------------


def _interpret_game_outcome(value: Any) -> GameOutcome | None:
    """Translate one ``game.outcome_state`` capability result into ``run.stop``'s `GameOutcome`.

    Returns ``None`` for every shape that is not an explicit, recognised ``"victory"``/
    ``"defeat"`` -- including the declaration's own ``"none"`` (the game is still running) and
    ``"unresolved"`` (no local player, or nothing could be read). Nothing here infers an outcome:
    a run must not be stopped and recorded as a defeat because a Lua read came back malformed.
    """
    if not isinstance(value, Mapping):
        return None
    outcome = value.get("outcome")
    if outcome == GameOutcome.VICTORY.value:
        return GameOutcome.VICTORY
    if outcome == GameOutcome.DEFEAT.value:
        return GameOutcome.DEFEAT
    return None


class _OutcomeTrackingObservationReader:
    """Wraps the production `ObservationReader` and remembers this run's last observed outcome.

    **Why this indirection exists.** ``RunnerDependencies.evaluate_stop_facts`` is a *synchronous*
    callable, and `Runner._play_run` invokes it from inside its own coroutine on its own event
    loop -- so it cannot itself ``await`` a Lua read, and cannot block on one scheduled back onto
    the loop it is already running on without deadlocking. Reading the outcome here instead costs
    nothing extra: the declaration is already part of every step's ordinary observation sweep, so
    the value `evaluate_stop_facts` later reads is the one from that turn's final fresh read --
    exactly the "what actually happened *this* turn" fact FR-009 asks the stop evaluator for.

    It is also the Principle I-correct place for it. The outcome reaches the stop evaluator by
    the same declared, catalogued, agent-visible path everything else does, rather than through a
    private read the record would not show.
    """

    def __init__(self, reader: ObservationReader, context: _RunContext) -> None:
        self._reader = reader
        self._context = context

    async def __call__(self) -> tuple[Sequence[CapabilityResult], str]:
        results, screen_identity = await self._reader()
        for result in results:
            if result.declaration_id == GAME_OUTCOME_DECLARATION_ID:
                self._context.last_game_outcome = _interpret_game_outcome(result.value)
        return results, screen_identity


# --------------------------------------------------------------------------
# T221 -- the live camera state behind every capture
# --------------------------------------------------------------------------


def _to_camera_state(value: Any) -> Mapping[str, Any]:
    """Translate a ``camera.read_state`` result into the shape `parity.screening` checks.

    The one rename this performs is deliberate and is the whole reason this function exists:
    ``lua/ingame/camera.lua`` reports ``target_is_revealed`` (its own long-standing field name,
    which `catalogs/actions/camera.yaml`'s ``camera.move`` verification predicate also reads),
    while `CaptureAttempt`/`_check_provenance` look for ``target_revealed``. Mapping it here, in
    one named place, is better than renaming a field two existing predicates already bind to.

    An unrecognised or unreadable result becomes an empty camera state, which fails the provenance
    gate closed (FR-026 permits no permissive default) rather than passing a partial one through.
    """
    if not isinstance(value, Mapping):
        return {}
    state: dict[str, Any] = {
        "mode": value.get("mode"),
        "zoom": value.get("zoom"),
        "target_plot": value.get("target_plot"),
        "target_revealed": value.get("target_is_revealed") is True,
    }
    return state


def _make_camera_state_provider(
    context: _RunContext,
) -> Callable[[], Awaitable[Mapping[str, Any]]]:
    """This run's real `camera_state_provider` (T221, FR-026).

    **Degrades the capture rather than failing the step.** Every read in
    ``lua/ingame/camera.lua`` is still `UNVERIFIED` against a live client, and a transport or
    catalog failure here would otherwise propagate out of the decision loop and pause the whole
    run. FR-050 already has the proportionate answer for "this step has no trustworthy image":
    returning an empty camera state withholds the capture, records ``image_withheld`` and
    ``capture_failed`` events naming the step, and marks the run visually degraded -- all of which
    leave the failure in the record, visibly, without abandoning a turn the agent can still play.
    """

    async def _read_camera_state() -> Mapping[str, Any]:
        try:
            result = await context.executor.execute(
                CAMERA_STATE_DECLARATION_ID, context=LuaContext.IN_GAME
            )
        except HarnessError as exc:
            log_event(
                get_harness_logger(),
                logging.WARNING,
                "run/composition: live camera state could not be read; this step's capture will "
                "be withheld and the run recorded visually degraded (FR-050)",
                extra={
                    "declaration_id": str(CAMERA_STATE_DECLARATION_ID),
                    "error_type": type(exc).__name__,
                },
            )
            return {}
        return _to_camera_state(result.value)

    return _read_camera_state


# --------------------------------------------------------------------------
# T220 -- what capture mechanism this host actually resolved
# --------------------------------------------------------------------------


def _resolve_capture_path(
    *,
    host: HostPlatform,
    host_info: HostInfo,
    window_provider: Callable[[], GameWindow | None],
) -> CapturePath:
    """Resolve the `CapturePath` this host **actually** produced a frame through (FR-050, SC-013).

    Deliberately makes one real capture attempt rather than looking up this platform's
    highest-ranked candidate from `observe.capture_paths.ranked_capture_paths`. Recording a path
    the host was never shown to be able to use is precisely the claim `select_capture_path` exists
    to refuse: it resolves to `CapturePath.NONE` for anything other than an ``ok`` result, and
    "no capture path" is a legitimate recorded operating state (research R6 rank 4), not an error.

    Today this resolves to ``NONE`` on every host -- pixel extraction is stubbed in all three host
    adapters (`observe/capture_paths.py`'s own docstring) -- which is exactly why it must be
    *measured* rather than declared: the value flips on its own the moment an adapter can really
    capture, with no second place to remember to update.
    """
    window = window_provider()
    if window is None:
        return CapturePath.NONE
    try:
        return select_capture_path(
            host=host, host_info=host_info, window=window
        ).capture_path
    except HarnessError:
        # `capture_window` is contractually not allowed to raise (host/port.py), so this is
        # defensive only -- an adapter that breaks that contract must not take preparation down.
        return CapturePath.NONE


# --------------------------------------------------------------------------
# T233 -- the crash/hang detection layer, bound to this run
# --------------------------------------------------------------------------


def _build_detection_watch(
    *,
    store: MatchStore,
    run_id: RunId,
    nexus_client: NexusClient,
    client_pid: int | None,
    clock: Callable[[], Timestamp],
) -> DetectionWatch:
    """This run's `DetectionWatch` (T233, FR-044/FR-045/SC-010).

    `DetectionAggregator`, `HeartbeatMonitor` and `ProcessLivenessMonitor` were each complete and
    each individually tested, and **nothing in `src/` constructed any of them** -- so none of
    research R12's signals ever ran during a real run and SC-010 had no mechanism behind it at
    all. This function is the construction that was missing.

    Two of R12's four signals are wired here, and the omissions are deliberate rather than
    unfinished:

    - **Process liveness** -- keyed to the PID `host.locate_game_process()` actually reported (the
      same call T227's identity lock already makes, so no second probe is introduced). A host that
      could not locate the process supplies no monitor rather than a fabricated PID: the
      heartbeat still runs, and `check_heartbeat`'s own corroboration step degrades honestly to
      `unresponsive_detected` when it has no liveness signal to confirm a crash against.
    - **Tuner heartbeat** -- bound to *this run's own* connected `NexusClient`, the same one every
      per-turn call dispatches through, so the probe shares the client's single connection slot
      (research R4) instead of opening a second one the game would refuse.
    - **Per-operation bounds** are not a signal the aggregator polls: every Nexus command already
      carries `nexus/client.py`'s own per-command timeout (T032), and `run/detection.py` applies
      `resilience.operation_bounds.run_bounded` to the aggregate pass itself so a wedged pass
      cannot silently switch detection off.
    - **The screen-identity probe** is deliberately left unwired -- `run/decision_loop.py` already
      polls the declared `game.screen_state` observation between decision steps and raises
      `UnknownScreenEncountered` (FR-049, research R13). See `run/detection.py`'s docstring.
    """
    liveness = ProcessLivenessMonitor(pid=client_pid) if client_pid is not None else None
    aggregator = DetectionAggregator(
        liveness=liveness,
        heartbeat=HeartbeatMonitor(client=nexus_client),
    )
    return DetectionWatch(
        aggregator=aggregator,
        store=store,
        run_id=run_id,
        clock=clock,
        liveness=liveness,
    )


# --------------------------------------------------------------------------
# T214 -- the single-connection NexusClient's lifetime
# --------------------------------------------------------------------------


async def _release_terminal_run_clients(
    store: MatchStore, run_contexts: dict[RunId, _RunContext], run_lock: RunIdentityLock
) -> None:
    """Close and drop the `NexusClient` of every run this process is still holding a context for
    that has since reached a terminal state (T214, FR-006).

    **Why a sweep at the start of the next run, rather than only an event at the end of the last.**
    `evaluate_stop_facts` closes the client on the ordinary stop path, but it is not on *every*
    path into a terminal state: `Runner._play_run` checks `state.stop_requested` at the top of its
    loop and calls `_finish` directly, without consulting the stop evaluator at all, and
    `RecoveryEngine` drives a run to `failed` before raising, likewise never coming back through
    it. `RunnerDependencies` exposes no terminal-state callback and `run/runner.py` is not this
    task's to edit, so the store -- which every one of those paths *does* write through -- is the
    one place all of them are visible from. Asking it here costs one read per held context, once
    per `run start`, and it is asked at exactly the moment the answer matters: the tuner accepts
    one connection at a time (research R4), so a client held by a finished run is a client the
    next run cannot have.

    A context whose run has no store record at all is treated as terminal too: nothing will ever
    transition a run that was never persisted, so its client would otherwise be held forever.
    """
    for run_id in list(run_contexts):
        context = run_contexts[run_id]
        run = store.get_run(run_id)
        is_terminal = run is None or run.lifecycle_state in TERMINAL_STATES
        if is_terminal or not context.nexus_client.is_connected:
            run_contexts.pop(run_id, None)
            # T227: the run identity is released with the client it was claimed alongside --
            # same sweep, same reason (this is the one place every terminal path is visible
            # from). A lock whose recorded PID has since died is cleared by the lock module
            # itself on the next `acquire`, so a crashed harness never wedges its own run id.
            run_lock.release(run_id)
            await _close_quietly(context.nexus_client)


async def _close_quietly(client: NexusClient) -> None:
    """Close *client*, never raising (T214).

    Every caller below is already on a failure or terminal path, where a socket that would not
    shut down cleanly must not become the error the operator sees instead of the real one.
    """
    try:
        await client.close()
    except Exception as exc:  # noqa: BLE001 - see docstring: this must never mask the real error
        log_event(
            get_harness_logger(),
            logging.WARNING,
            "run/composition: the Nexus client could not be closed cleanly",
            extra={"error_type": type(exc).__name__},
        )


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
    guidance_root: Path,
    lua_root: Path,
    window_provider: Callable[[], GameWindow | None],
    worst_case_context_tokens: int,
    home: Path | None,
    clock: Callable[[], Timestamp],
    run_contexts: dict[RunId, _RunContext],
    run_lock: RunIdentityLock,
    save_loader: SaveLoader,
    branch_from: BranchFrom | None = None,
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

    # -- 1b. guidance (T219, FR-021, V6) ------------------------------------------------------
    # Resolved here, with the other pure local gates, and deliberately *before* anything live is
    # touched: a configuration naming guidance that does not exist, or whose content collides with
    # a different content already registered under the same hash, must refuse the run for free
    # rather than at the agent's first decision step. `guidance_set_id` carries the source
    # reference verbatim (contracts/run-configuration.md's `guidance_set: GUIDEBOOK.md@a1b2c3d`;
    # config/run_config.py maps that key onto this field), and `load_guidance` recomputes the
    # content hash from what it actually read rather than trusting the `@...` pin.
    guidance: GuidanceSet | None = None
    if config.guidance_set_id is not None:
        guidance = load_guidance(
            str(config.guidance_set_id),
            root=guidance_root,
            guidance_set_id=config.guidance_set_id,
        )

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
    # From here on the client holds the tuner's single connection slot (research R4), so every
    # path that leaves this function without handing the client to a live `_RunContext` must close
    # it -- otherwise a second `run start` in this process can never connect (T214).
    await nexus_client.connect()
    try:
        return await _prepare_connected_run(
            config,
            store=store,
            catalog=catalog,
            registry=registry,
            host=host,
            host_info=host_info,
            nexus_client=nexus_client,
            seed_set=seed_set,
            guidance=guidance,
            catalog_result=catalog_result,
            host_gate_result=host_gate_result,
            lua_root=lua_root,
            window_provider=window_provider,
            clock=clock,
            run_contexts=run_contexts,
            run_lock=run_lock,
            save_loader=save_loader,
            branch_from=branch_from,
        )
    except BaseException:
        await _close_quietly(nexus_client)
        raise


async def _prepare_connected_run(
    config: RunConfiguration,
    *,
    store: MatchStore,
    catalog: Catalog,
    registry: CapabilityRegistry,
    host: HostPlatform,
    host_info: HostInfo,
    nexus_client: NexusClient,
    seed_set: Any,
    guidance: GuidanceSet | None,
    catalog_result: Any,
    host_gate_result: Any,
    lua_root: Path,
    window_provider: Callable[[], GameWindow | None],
    clock: Callable[[], Timestamp],
    run_contexts: dict[RunId, _RunContext],
    run_lock: RunIdentityLock,
    save_loader: SaveLoader,
    branch_from: BranchFrom | None = None,
) -> PreparedRun:
    """Steps 3b-8 of :func:`_prepare_run`, split out purely so its caller can own one
    ``try``/``except`` around the whole post-connect sequence (T214).

    Every ``return`` below either registers *nexus_client* in ``run_contexts`` (the run is
    playing, and the client is now that run's) or closes it (the run reached ``failed`` before
    turn 1, and nothing will ever dispatch through it again). Anything raised is closed by the
    caller. There is no fourth path out of this function.
    """
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
    host_platform = {
        "os": host_info.os.value,
        "os_version": host_info.os_version,
        "session_type": host_info.session_type.value if host_info.session_type else None,
    }
    # T220: measured, not declared -- one real capture attempt against the live window, whose
    # result names the R6-ranked mechanism that actually produced a frame (or `NONE`).
    capture_path = _resolve_capture_path(
        host=host, host_info=host_info, window_provider=window_provider
    )

    if branch_from is not None:
        # T226, FR-033/FR-034, Principle IV. `saves/branching.py` owns every branch-specific
        # step -- the parent must exist, its build must agree with this client's (T174/T175),
        # its save at `branch_from.turn` must be available (FR-036, never retargeted), that save
        # must actually load, and the lineage plus `branch_created` must be recorded. All of it
        # happens here rather than in a second preparation path of its own, so a branch passes
        # through the *same* gates 1-6 above and the *same* V2 verification and
        # `preparing -> playing` transition below that every other run does.
        run, _save_point, _branch_event = await create_branch(
            store,
            save_loader,
            branch_from=SaveBranchFrom(run_id=branch_from.run_id, turn=branch_from.turn),
            child=BranchSource(
                run_id=RunId(f"run-{uuid.uuid4().hex}"),
                config=config,
                game_build=actual_build,
                observation_catalog_version=catalog_result.observation_catalog_version,
                action_catalog_version=catalog_result.action_catalog_version,
                host_support_tier=host_gate_result.tier,
                capture_path=capture_path,
                # The binding half of T226: both come from this host's own gate and this
                # process's own `HostInfo`, exactly as the non-branch construction below does.
                # A branch on a degraded host records itself degraded.
                comparability_status=host_gate_result.comparability_status,
                host_platform=host_platform,
            ),
            accepted_build_changes=(
                tuple(seed_set.accepted_build_changes) if seed_set is not None else ()
            ),
            occurred_at=clock(),
        )
        # Loading a save is a phase boundary like any other: the state table the client reported
        # before the load cannot be assumed valid after it (`nexus/client.py`).
        await nexus_client.refresh_state_indices()
    else:
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
            host_platform=host_platform,
            host_support_tier=host_gate_result.tier,
            capture_path=capture_path,
        )
        store.create_run(run, config)

    # T227, FR-006/V8, research R4: claim this run identity before a single turn is played.
    # The game's own one-tuner-at-a-time limit already stops two harnesses attaching to the
    # *same* client; what it cannot see is two harness processes playing the *same* `run_id`
    # against *different* clients, writing two divergent records under one run id. Keyed to the
    # located client's PID, which is exactly the fact the lock module was designed around and
    # which nothing in `src/` had ever supplied it. A host that cannot locate a client process
    # has no PID to key on -- the lock is skipped rather than keyed to a fabricated one, and the
    # run proceeds (it has already passed the host gate, which is what decides whether a host
    # may run at all).
    client_process = host.locate_game_process()
    if client_process is not None:
        run_lock.acquire(run_id=run.run_id, client_pid=client_process.pid, now=clock())

    if leader_result is not None and leader_result.outcome is not LeaderSelectionOutcome.VERIFIED:
        return await _fail_preparation(
            store=store,
            run=run,
            stop_condition=config.stop_condition,
            reason="leader/civilization selection could not be verified (V2, V3)",
            mismatches=leader_result.mismatches,
            clock=clock,
            nexus_client=nexus_client,
            run_lock=run_lock,
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
            nexus_client=nexus_client,
            run_lock=run_lock,
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
        guidance=guidance,
        # T233: built from the PID step 7 already located for T227's lock and from this run's own
        # connected client -- no second process probe, no second tuner connection.
        detection=_build_detection_watch(
            store=store,
            run_id=playing_run.run_id,
            nexus_client=nexus_client,
            client_pid=client_process.pid if client_process is not None else None,
            clock=clock,
        ),
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
    nexus_client: NexusClient,
    run_lock: RunIdentityLock | None = None,
) -> PreparedRun:
    """A `Run` already exists (`preparing`) -- record why it cannot proceed and transition it to
    `failed`, per contracts/operator-surface.md's "run created in failed state ... no turn 1"
    (never raised: see `_prepare_run`'s own docstring).

    *nexus_client* is closed here (T214): `failed` is terminal, this run will never play a turn,
    and nothing else will ever close it -- no `_RunContext` is registered on this path, so the
    tuner's single connection slot would otherwise stay held for the life of the process.

    *run_lock* is released here for the same reason (T227): the run identity was claimed in step 7
    and this run will never play, so holding the claim would refuse a corrected re-run of the very
    configuration this failure is telling the operator to fix.
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
    if run_lock is not None:
        run_lock.release(failed_run.run_id)
    await _close_quietly(nexus_client)
    return PreparedRun(run=failed_run, stop_condition=stop_condition)


def build_runner_dependencies(
    *,
    store: MatchStore,
    catalog_root: Path = DEFAULT_CATALOG_ROOT,
    seedset_root: Path = DEFAULT_SEEDSET_ROOT,
    guidance_root: Path = DEFAULT_GUIDANCE_ROOT,
    lua_root: Path = DEFAULT_LUA_ROOT,
    host: HostPlatform,
    host_info: HostInfo | None = None,
    nexus_client_factory: NexusClientFactory | None = None,
    provider: ModelProvider | None = None,
    support_probe: SupportProbeResult | None = None,
    worst_case_context_tokens: int = DEFAULT_WORST_CASE_CONTEXT_TOKENS,
    view_declaration_id: DeclarationId = DEFAULT_VIEW_DECLARATION_ID,
    observation_declaration_ids: Sequence[DeclarationId] | None = None,
    window_provider: Callable[[], GameWindow | None] | None = None,
    save_loader: SaveLoader | None = None,
    run_lock: RunIdentityLock | None = None,
    disk_check_path: Path | None = None,
    home: Path | None = None,
    screening_profiles: ScreeningProfiles | None = None,
    detection_interval_s: float = DEFAULT_DETECTION_INTERVAL_S,
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
    docstring), never here.

    *support_probe* defaults to the real R19 per-platform probe
    (:func:`~civsim_harness.host.detect.probe_host_support`, T212), run against *host* and
    *host_info* at composition time. It previously defaulted to `UNPROBED`, and because nothing in
    `src/` ever constructed anything else, `evaluate_host_gate` refused **every** run on **every**
    host -- including the Linux one that had already passed live validation -- with a message that
    read as host-specific and was not. The probe reports recorded per-platform spike evidence and
    never generalises across platforms, so a host with no spike on record still refuses, now
    naming which spike is missing on which platform rather than "not yet probed". An explicit
    *support_probe* still wins, which is what lets a test (or a host whose spike has since been
    run) supply its own evidence.

    *run_lock* defaults to a :class:`~civsim_harness.run.identity_lock.RunIdentityLock` over its
    module default directory (T227, FR-006/V8). It is claimed once per run, keyed to the located
    client's PID, and released on every terminal path alongside T214's client close. Nothing in
    `src/` constructed one before this task, so the guard against two harness processes playing
    one `run_id` against two different clients existed only in tests. A caller supplying its own
    (a test, or a machine wanting the locks somewhere specific) overrides the directory.

    *guidance_root* is where `RunConfiguration.guidance_set_id`'s source reference resolves from
    (T219); it is read once per run during preparation, never per turn.

    *detection_interval_s* is how often the in-turn watchdog asks research R12's detection signals
    whether the client is still healthy (T233, FR-044/SC-010) -- **not** a turn timer: it bounds
    how long the harness may go without asking, never how long a turn, a step, or an operation may
    take (FR-014). The detection layer itself is always wired; before T233 nothing in `src/`
    constructed it, so SC-010 had no mechanism at all.

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
    resolved_run_lock = run_lock if run_lock is not None else RunIdentityLock()
    resolved_disk_check_path = disk_check_path if disk_check_path is not None else Path.cwd()
    resolved_screening_profiles = (
        screening_profiles if screening_profiles is not None else load_screening_profiles()
    )
    resolved_support_probe: SupportProbeResult = (
        support_probe
        if support_probe is not None
        else probe_host_support(host, resolved_host_info, home=home)
    )

    run_contexts: dict[RunId, _RunContext] = {}

    async def _prepare(
        config: RunConfiguration, branch_from: BranchFrom | None
    ) -> PreparedRun:
        # T214: release any client still held by an already-terminal run before asking the tuner
        # for its single connection slot again -- see `_release_terminal_run_clients`.
        await _release_terminal_run_clients(store, run_contexts, resolved_run_lock)

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
            support_probe=resolved_support_probe,
            seedset_root=seedset_root,
            guidance_root=guidance_root,
            lua_root=lua_root,
            window_provider=resolved_window_provider,
            worst_case_context_tokens=worst_case_context_tokens,
            home=home,
            clock=clock,
            run_contexts=run_contexts,
            run_lock=resolved_run_lock,
            save_loader=resolved_save_loader,
            branch_from=branch_from,
        )

    async def prepare_run(config: RunConfiguration) -> PreparedRun:
        return await _prepare(config, None)

    async def prepare_branch(config: RunConfiguration, branch_from: BranchFrom) -> PreparedRun:
        """T226, FR-033/FR-034: prepare a **branch** of an existing run.

        Deliberately the same call as `prepare_run` with one extra argument, not a second
        preparation pipeline: a branch must pass every gate an ordinary run passes (seed-set
        agreement, catalog, host capability, model chain, build pin, V2 read-back) *plus* the
        branch-specific ones, and the surest way to keep that true is for there to be only one
        sequence that can be added to. See `_prepare_connected_run`'s step 7.

        Fails loudly and specifically rather than starting an unbranched run: a missing parent
        (`BranchSourceMissingError`), a build disagreement (`BranchBuildMismatchError` /
        `BranchPlatformSpikeRequiredError`), an absent save (`SaveAddressingError`, never
        retargeted), or -- today, universally -- the missing save-*load* capability itself
        (`_NoLiveSaveLoader`, T217), each raised by name before any child `Run` is constructed.
        """
        return await _prepare(config, branch_from)

    def _next_attempt_index(run_id: RunId, turn_number: int) -> int:
        """T223 (FR-004, FR-047): the `attempt_index` this play of *turn_number* must start at.

        Zero unless this `(run_id, turn_number)` already has attempts on record, which happens
        exactly when the turn is being played a second time -- a `resume-from` rewind or a branch
        replay. Those mark the earlier attempts non-authoritative, but superseding does **not**
        free their `(run_id, turn_number, attempt_index)` triples: `store/sqlite_adapter.py`'s D4
        idempotency check rejects a second write of the same triple carrying different content, so
        a replay starting at 0 would fail to persist and, under FR-013, halt the run instead of
        advancing. One index past the highest attempt on record is the smallest value that keeps
        **both** attempts readable, which is what FR-047 asks for.

        Read through the port's existing `get_turn_cycle(..., authoritative_only=False)`, which
        already returns the highest-`attempt_index` record for the turn -- deliberately not a new
        store method, and deliberately resolved here rather than inside `run/turn_cycle.py`, so
        this stays the single auditable place that decides a replay's index.
        """
        record = store.get_turn_cycle(run_id, turn_number, authoritative_only=False)
        if record is None:
            return 0
        return record.turn_cycle.attempt_index + 1

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
            observation_reader = _OutcomeTrackingObservationReader(
                ObservationReader(
                    executor=ctx.executor,
                    registry=ctx.registry,
                    declaration_ids=observation_declaration_ids,
                ),
                ctx,
            )
            action_executor = ActionExecutor(executor=ctx.executor, registry=ctx.registry)
            return DecisionLoopContext(
                run_id=run_id,
                turn_number=turn_number,
                turn_cycle_id=turn_cycle_id,
                registry=ctx.registry,
                catalog_version=ctx.catalog_version,
                model=config.agent_model_config.primary,
                guidance=ctx.guidance,
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
                camera_state_provider=_make_camera_state_provider(ctx),
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
            attempt_index_base=_next_attempt_index(run_id, turn_number),
            # T233, FR-044/FR-045/SC-010: the detection layer this run was prepared with. Without
            # this line the aggregator is constructed and never asked anything, which is exactly
            # the defect the whole of Phase 11 was written about.
            detection=ctx.detection,
            detection_interval_s=detection_interval_s,
        )

    def evaluate_stop_facts(prepared: PreparedRun, turn_number: int) -> StopEvaluation:
        """Report what `run.stop.evaluate_stop` needs after *turn_number* finished (T216, FR-005).

        ``game_outcome`` comes from the last ``game.outcome_state`` this run actually observed --
        the declaration T216 added to the catalog, read as part of every decision step's ordinary
        observation sweep and recorded on the run's `_RunContext` by
        :class:`_OutcomeTrackingObservationReader`. It is therefore the outcome as of this turn's
        *final* fresh read, which is exactly the "what actually happened this turn" fact FR-009
        asks for, and it resolves for every run regardless of configured `StopCondition` -- a run
        configured to stop at `turn_reached(50)` must still recognise a victory on turn 30
        (`run/stop.py`'s own contract).

        ``None`` when this run never read that declaration (a caller narrowed
        *observation_declaration_ids* past it, or no step has completed yet). That is the honest
        report -- never an assumed "no victory" that a caller could mistake for an observation.

        ``operator_stop_requested`` needs no entry: `Runner._play_run` checks its own
        `state.stop_requested` before ever calling this. ``unrecoverable_failure`` likewise has no
        source here -- a failure that terminates a run reaches the runner as a raised
        `HarnessError`, which `_handle_run_failure` routes on its own side of this seam, never by
        coming back around through this function.

        **T214's terminal-state close lives here too**, and this is the only seam that can host
        it: `RunnerDependencies` exposes no terminal-state callback, and `run/runner.py` is not
        this task's to edit. Re-deriving the same `StopDecision` the runner is about to derive
        from these identical facts tells this function whether the run it just reported on is
        finishing -- and if it is, the client that run has held is released here, before
        `Runner._finish` records the transition. The re-derivation is not a second source of
        truth: `evaluate_stop` is a pure function and the runner calls it on this very return
        value a moment later, so the two cannot disagree.
        """
        ctx = run_contexts.get(prepared.run.run_id)
        facts = StopEvaluation(
            current_turn=turn_number,
            game_outcome=ctx.last_game_outcome if ctx is not None else None,
        )

        if ctx is not None and evaluate_stop(prepared.stop_condition, facts).resolution is not None:
            run_contexts.pop(prepared.run.run_id, None)
            resolved_run_lock.release(prepared.run.run_id)  # T227, alongside T214's close
            try:
                asyncio.get_running_loop().create_task(_close_quietly(ctx.nexus_client))
            except RuntimeError:  # pragma: no cover - only if called off the runner's own loop
                log_event(
                    get_harness_logger(),
                    logging.WARNING,
                    "run/composition: no running event loop to close this run's Nexus client on; "
                    "the tuner's single connection slot may stay held until the process exits",
                    extra={"run_id": str(prepared.run.run_id)},
                )

        return facts

    def connection_health() -> ConnectionHealth:
        """Real tuner and client reachability (T222, FR-053).

        Both were hard-coded ``"unknown"``, which made `run status` unable to tell a live client
        from a dead one -- the single thing this field exists for. Neither value below performs
        any I/O of its own: the tuner's is read from the `NexusClient`'s own socket state, and the
        client's from the same `locate_game_process()` call `doctor` already reports from, so
        `status` stays as cheap as it was and can never block on a hung socket.

        The vocabulary is deliberately the short free-form one `ConnectionHealth` documents, not a
        closed enum -- ``"no_run"`` (this process is driving no run, so there is no tuner
        connection to have) is genuinely distinct from ``"disconnected"`` (there is a run and its
        connection is gone), and collapsing them would hide exactly the case an operator is
        looking at `status` to understand.
        """
        health = store.ping()

        if not run_contexts:
            tuner = "no_run"
        elif any(ctx.nexus_client.is_connected for ctx in run_contexts.values()):
            tuner = "ok"
        else:
            tuner = "disconnected"

        try:
            client = "ok" if host.locate_game_process() is not None else "not_running"
        except Exception:  # noqa: BLE001 - psutil's own failures must not take `status` down
            client = "unknown"

        return ConnectionHealth(
            tuner=tuner,
            client=client,
            store="ok" if health.ok else "unreachable",
        )

    def disk_headroom_gb() -> float:
        space = host.free_disk_space(resolved_disk_check_path)
        return round(space.free_bytes / 1024**3, 1)

    return RunnerDependencies(
        store=store,
        prepare_run=prepare_run,
        prepare_branch=prepare_branch,
        build_turn_dependencies=build_turn_dependencies,
        evaluate_stop_facts=evaluate_stop_facts,
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
    "CAMERA_STATE_DECLARATION_ID",
    "DEFAULT_CATALOG_ROOT",
    "DEFAULT_GUIDANCE_ROOT",
    "DEFAULT_LUA_ROOT",
    "DEFAULT_SEEDSET_ROOT",
    "DEFAULT_VIEW_DECLARATION_ID",
    "DEFAULT_WORST_CASE_CONTEXT_TOKENS",
    "GAME_OUTCOME_DECLARATION_ID",
    "NexusClientFactory",
    "build_runner_dependencies",
]
