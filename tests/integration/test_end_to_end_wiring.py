"""T211: `civsim run start` plays and persists a turn through the **real** composition root.

Every other test in this suite builds its own `RunnerDependencies`/`TurnCycleDependencies`/
`DecisionLoopContext` by hand. That is what let 933 tests pass while `civsim run start` failed on
its first line with "no runner is configured"
(`specs/002-civ-playing-harness/integration-readiness.md`): each component was correct in
isolation and nothing assembled them. This module is the one test that assembles nothing itself.

**What is real here, and what is a fake.** The composition is real --
`run.composition.build_runner_dependencies` builds every collaborator, the `civsim run start` CLI
command is the entry point, and the run flows through the real `Runner`, `run_turn_cycle`,
`run_decision_loop`, `CapabilityExecutor`, `ObservationReader`, `ActionExecutor`,
`LuaSaveCapability`, `ProviderChain`, and `SqliteMatchStore`. The real `catalogs/` tree and the
real `lua/` files are loaded, so `IntegrationCapability.implementation_ref` -- the field the audit
found was "read nowhere outside its own model definition" -- is resolved to an actual file on disk
and dispatched. What is faked is only what cannot exist in a test: the game (a `FakeNexusServer`
speaking the real wire protocol), the model (`FakeModelProvider`), and the OS
(`FakeHostPlatform`).

**The fake game is stateful, not a recording.** `turn.end_turn`'s own verification predicate is
`game.turn_number == observed_turn_number + 1 or game.is_waiting_for_other_players`, so a turn
only verifies as `applied` if the turn counter the harness reads back has genuinely advanced. A
static transcript cannot satisfy that; `_FakeGame` below therefore advances its own counter when
it sees the end-turn dispatch, and writes a real save file when it sees `Network.SaveGame` -- the
two things a real client does that the harness immediately checks afterwards.
"""

from __future__ import annotations

import asyncio
import atexit
import os
import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil
import pytest
import yaml
from typer.testing import CliRunner

from civsim_harness.config.run_config import load_run_configuration_file
from civsim_harness.host.detect import (
    HostInfo,
    LinuxSessionType,
    OperatingSystem,
    SupportProbeResult,
)
from civsim_harness.host.port import DiskSpace, GameProcess
from civsim_harness.models.common import CapturePath, DeclarationId, ModelRef, RunId
from civsim_harness.models.run import ComparabilityStatus, LifecycleState, StopResolution
from civsim_harness.nexus.client import NexusClient
from civsim_harness.operator import cli
from civsim_harness.provider.port import ModelCapabilities, RawDecision
from civsim_harness.run.composition import build_runner_dependencies
from civsim_harness.run.identity_lock import RunIdentityLock
from civsim_harness.run.runner import Runner
from civsim_harness.run.turn_cycle import run_turn_cycle
from civsim_harness.store.sqlite_adapter import SqliteMatchStore
from fakes.fake_host import FakeHostPlatform
from fakes.fake_nexus import (
    STATE_INDEX_IN_GAME,
    STATE_INDEX_MAIN_MENU,
    FakeNexusServer,
    ReceivedCommand,
    front_end_state_table,
    loaded_game_state_table,
)
from fakes.fake_provider import FakeModelProvider

REPO_ROOT = Path(__file__).resolve().parents[2]

TURN_STATE = DeclarationId("game.turn_state")
SCREEN_STATE = DeclarationId("game.screen_state")
END_TURN = DeclarationId("turn.end_turn")

#: The run plays to this turn and stops. Two turns rather than one: a single turn would not
#: distinguish "the wiring produced a turn" from "the wiring produced *the* turn", and the
#: turn-to-turn boundary (a fresh quicksave, a fresh `TurnCycleDependencies`, the stop condition
#: re-evaluated against what actually happened) is exactly where a composition root's per-run vs
#: per-turn split goes wrong.
STOP_AT_TURN = 2

GAME_BUILD_VERSION = "1.0.12.9"
GAME_BUILD = f"linux/{GAME_BUILD_VERSION}"

PRIMARY_MODEL = ModelRef(provider="fake", model="primary")


# --------------------------------------------------------------------------
# The fake game: stateful, and doing the two things the harness checks it did
# --------------------------------------------------------------------------


class _FakeGame:
    """The scripted client behind `FakeNexusServer`, holding the state a real one would."""

    def __init__(self, *, saves_dir: Path, config_values: dict[str, Any]) -> None:
        self.turn = 1
        self.saves_dir = saves_dir
        self.config_values = config_values
        self.end_turns_issued = 0
        self.saves_written: list[str] = []
        self.setup_lua_bodies: list[str] = []

    def read_turn_number(self, _command: ReceivedCommand) -> dict[str, Any]:
        return {
            "turn_number": self.turn,
            "is_local_player_turn": True,
            "is_waiting_for_other_players": False,
        }

    def probe_screen(self, _command: ReceivedCommand) -> dict[str, Any]:
        return {
            "screen": "world",
            "raw_screen_id": "WorldView",
            "recognized": True,
            "has_blocking_prompt": False,
            "prompt_options": [],
        }

    def end_turn(self, _command: ReceivedCommand) -> dict[str, Any]:
        """A real `UI.RequestAction(ACTION_ENDTURN)` returns `nil` and proves nothing -- the turn
        counter advancing is the only evidence, which is why this advances it."""
        self.end_turns_issued += 1
        self.turn += 1
        return {"ok": True, "path": "UI.RequestAction"}

    def save_game(self, command: ReceivedCommand) -> dict[str, Any]:
        """Write the file a real client would, so the filesystem verification `run/turn_cycle.py`
        performs right after the dispatch has something real to find."""
        save_name = _extract_save_name(command.lua_body)
        self.saves_dir.mkdir(parents=True, exist_ok=True)
        (self.saves_dir / f"{save_name}.Civ6Save").write_bytes(b"fake-civ6-save-payload")
        self.saves_written.append(save_name)
        return {"issued": True, "error": ""}

    def read_version(self, _command: ReceivedCommand) -> dict[str, Any]:
        return {"ok": True, "version": GAME_BUILD_VERSION}

    def read_setup(self, command: ReceivedCommand) -> dict[str, Any]:
        """Answer the single `LuaGameSetupReader` snapshot with the configured setup, keyed the
        way that reader flattens dotted field names. The dispatched body is kept so a test can
        assert on the Lua the tuner actually received (T250: which getters ran, and which must
        not have)."""
        self.setup_lua_bodies.append(command.lua_body)
        return {
            **{key.replace(".", "__"): value for key, value in self.config_values.items()},
            "turn_timer_type": "TURNTIMER_NONE",
            "turn_timer_hash": -1525060181,
        }


def _extract_save_name(lua_body: str) -> str:
    """Pull `gameFile.Name = "<name>"` back out of the dispatched save body."""
    marker = 'gameFile.Name = "'
    start = lua_body.index(marker) + len(marker)
    return lua_body[start : lua_body.index('"', start)]


# --------------------------------------------------------------------------
# Running the fake server on its own thread
# --------------------------------------------------------------------------


class _ServerThread:
    """Runs `FakeNexusServer` on a dedicated event loop in its own thread.

    Necessary because `Runner` owns its own loop on its own thread and `RunnerProtocol.start` is
    synchronous: the test thread blocks inside `runner.start(...)` for the whole run, so a server
    sharing the test's loop would never be pumped and the client's connect would hang forever.
    Two real loops over a real TCP socket is also the honest shape -- the real client is a
    separate process.
    """

    def __init__(self, server: FakeNexusServer) -> None:
        self._server = server
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="fake-nexus-loop", daemon=True
        )

    def __enter__(self) -> FakeNexusServer:
        self._thread.start()
        asyncio.run_coroutine_threadsafe(self._server.start(), self._loop).result(timeout=10)
        return self._server

    def __exit__(self, *_exc_info: object) -> None:
        # `FakeNexusServer.stop()` awaits `wait_closed()`, which does not return while a client
        # is still connected -- and nothing in the composition root closes its `NexusClient` when
        # a run finishes, so one always is. Cancel the connection handlers explicitly instead, on
        # the loop and while it is still running, so each unwinds through its own `finally`;
        # stopping the loop out from under a suspended handler would finalize the coroutine
        # mid-await and surface as an unraisable `GeneratorExit`.
        asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop).result(timeout=10)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10)
        self._loop.close()

    async def _shutdown(self) -> None:
        self._server.close_now()
        pending = [
            task for task in asyncio.all_tasks() if task is not asyncio.current_task()
        ]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


# --------------------------------------------------------------------------
# Fixtures: a run configuration and the seed set it must agree with (V3)
# --------------------------------------------------------------------------

_CIVILIZATION = "CIVILIZATION_PERSIA"
_LEADER = "LEADER_CYRUS"
_RULESET = "RULESET_EXPANSION_2"
_DIFFICULTY = "DIFFICULTY_PRINCE"
_MAP_SEED = "1414213562"


def _write_seed_set(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "e2e-wiring.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "name": "e2e-wiring",
                "civilization": _CIVILIZATION,
                "leader": _LEADER,
                "ruleset": _RULESET,
                "mod_set": [],
                "game_build": GAME_BUILD,
                "seeds": [_MAP_SEED],
                "accepted_build_changes": [],
            }
        ),
        encoding="utf-8",
    )


def _write_run_config(
    path: Path,
    *,
    extra_game_settings: dict[str, Any] | None = None,
    extra_map_settings: dict[str, Any] | None = None,
    extra_opponents: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write the run configuration and return the flattened setup the fake game must report back.

    Deliberately carries only the configured fields `run.preparation`'s own setup reader has a
    registered getter for: a field with no read path is recorded on the run as unverified (T242)
    rather than silently treated as agreeing, which is a separate behaviour with its own test
    below rather than something to work around here. *extra_game_settings* /
    *extra_map_settings* / *extra_opponents* let such tests add exactly the fields they are
    about; a test whose client must *not* report one back (an unobservable field, T250) pops it
    from the returned mapping.
    """
    document = {
        "schema_version": 1,
        "config_id": "e2e-wiring",
        "seed_set": "e2e-wiring",
        "map_seed": _MAP_SEED,
        "civilization": _CIVILIZATION,
        "leader": _LEADER,
        "ruleset": _RULESET,
        "mod_set": [],
        "map_settings": dict(extra_map_settings or {}),
        "game_settings": dict(
            {"game_speed": "GAMESPEED_ONLINE", "starting_era": "ERA_ANCIENT"},
            **(extra_game_settings or {}),
        ),
        "difficulty": _DIFFICULTY,
        "opponents": dict({"city_state_count": 10}, **(extra_opponents or {})),
        "stop_condition": {"type": "turn_reached", "turn": STOP_AT_TURN},
        "model_config": {
            "primary": {"provider": "fake", "model": "primary"},
            "fallbacks": [],
        },
        "no_progress_step_limit": 8,
        "recovery_attempt_limit": 3,
        "min_free_disk_gb": 0,
    }
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return {
        "map_seed": _MAP_SEED,
        "civilization": _CIVILIZATION,
        "leader": _LEADER,
        "ruleset": _RULESET,
        # `configured_fields` always emits `mod_set`, even when empty -- "no mods" is a
        # meaningful claim, not the absence of one -- so the client must report it back too.
        "mod_set": [],
        "difficulty": _DIFFICULTY,
        "game_settings.game_speed": "GAMESPEED_ONLINE",
        "game_settings.starting_era": "ERA_ANCIENT",
        **{f"game_settings.{key}": value for key, value in (extra_game_settings or {}).items()},
        **{f"map_settings.{key}": value for key, value in (extra_map_settings or {}).items()},
        "opponents.city_state_count": 10,
        **{f"opponents.{key}": value for key, value in (extra_opponents or {}).items()},
    }


def _build_provider() -> FakeModelProvider:
    provider = FakeModelProvider()
    provider.set_capabilities(
        PRIMARY_MODEL,
        ModelCapabilities(
            max_context_tokens=1_000_000,
            accepts_images=True,
            max_images_per_request=8,
            confirmed=True,
        ),
    )
    # One decision per step, always "end the turn" -- this test is about the wiring carrying a
    # decision all the way to the game and back, not about strategy.
    provider.set_default_decision(
        RawDecision(
            action_declaration_id=END_TURN,
            reasoning="end the turn",
            is_end_turn=True,
        )
    )
    return provider


def _dispatch_match(module: str, function: str) -> str:
    """The *appended dispatch line* `CapabilityExecutor` puts after a Lua file's own source.

    Matching the bare `CivSim_TurnControl.end_turn` substring is not good enough, and the way it
    fails is silent: every `lua/**/*.lua` file ends with a commented example of its own dispatch
    calls (`--   print(CivSim_JsonEncode(CivSim_TurnControl.end_turn()))`), so that substring is
    present in *every* dispatch of that file regardless of which function is actually being
    called. A read would then be answered by the end-turn script, or an end-turn by the read's --
    and the run would look like it played turns while never issuing one. Anchoring on the
    newline-prefixed, uncommented `print(` the executor itself appends is what distinguishes the
    real call from the file's own documentation of it.
    """
    return f"\n\nprint(CivSim_JsonEncode({module}.{function}("


def _script_game(server: FakeNexusServer, game: _FakeGame) -> None:
    """Every command this run issues, answered by the stateful fake game.

    All repeatable: the run's length is what is under test, so the number of times each read
    happens must not be baked into the script.
    """
    server.queue_response(
        game.read_version, match="Modding.GetActiveGameVersion", repeatable=True
    )
    # Matched on `civsim_resolve`, the hash -> name reverse-lookup helper only the setup reader
    # emits. Matching the JSON prelude instead would also swallow the save and leader-selection
    # dispatches, which carry the same prelude.
    server.queue_response(game.read_setup, match="civsim_resolve", repeatable=True)
    server.queue_response(game.save_game, match="Network.SaveGame", repeatable=True)
    server.queue_response(
        game.read_turn_number,
        match=_dispatch_match("CivSim_TurnControl", "read_turn_number"),
        repeatable=True,
    )
    server.queue_response(
        game.probe_screen, match=_dispatch_match("CivSim_Screens", "probe"), repeatable=True
    )
    server.queue_response(
        game.end_turn, match=_dispatch_match("CivSim_TurnControl", "end_turn"), repeatable=True
    )
    # T233: the tuner heartbeat the in-turn watchdog round-trips through `GameCore_Tuner`
    # (`nexus/heartbeat.py`'s `print(true)`). A real client answers it; without this the fake
    # would answer with its generic default, which `probe_heartbeat` correctly reads as a failed
    # round-trip -- i.e. the *fake* would be the hang, not the harness.
    server.queue_response(lambda _command: True, match="print(true)", repeatable=True)


@pytest.fixture(autouse=True)
def _restore_cli_wiring() -> Any:
    """`operator/cli.py` wires its real default factory at import (T209); every test here replaces
    it with one returning a runner composed against fakes, then puts the real one back.

    Restores to `cli.default_runner_factory` itself, never to whatever `cli._runner_factory`
    happened to hold when this test started: `tests/unit/test_operator_api.py`'s own
    `_reset_cli_wiring` fixture deliberately leaves the module-level global cleared to `None` after
    its last test (that is its own correct "clean slate" behaviour) -- capturing and restoring
    that already-corrupted value here would propagate the pollution to every later test in this
    file instead of healing it.
    """
    yield
    cli.configure_runner_factory(cli.default_runner_factory)


def _compose(
    tmp_path: Path,
    *,
    port: int,
    provider: FakeModelProvider,
    run_lock: RunIdentityLock | None = None,
    client_process: GameProcess | None = None,
) -> tuple[Runner, Any]:
    """Build a real `Runner` through the real composition root, against fakes."""
    deps, store = _compose_dependencies(
        tmp_path,
        port=port,
        provider=provider,
        run_lock=run_lock,
        client_process=client_process,
    )
    return Runner(deps), store


def _live_client_process() -> GameProcess:
    """A "game client" PID that is genuinely a live process on this machine (T233).

    `FakeHostPlatform`'s default process is a synthetic PID (42424) that almost certainly does not
    exist, and T233 wired `resilience/liveness.py` -- a real `psutil` check -- into the production
    run loop against whatever PID `locate_game_process()` reports. A fake host claiming a process
    that is not there is a fake host describing a crashed client, and the harness now correctly
    says so. Pointing the fake at this test process keeps the *real* liveness path in the loop
    (no stub, no monkeypatch) while making the fake's claim true.
    """
    return GameProcess(pid=os.getpid(), name="CivilizationVI_FAKE", executable_path=None)


def _compose_dependencies(
    tmp_path: Path,
    *,
    port: int,
    provider: FakeModelProvider,
    run_lock: RunIdentityLock | None = None,
    client_process: GameProcess | None = None,
    save_loader: Any = None,
    free_disk_bytes: int | None = None,
) -> tuple[Any, SqliteMatchStore]:
    store = SqliteMatchStore(tmp_path / "match-store.db")
    host = FakeHostPlatform()
    host.set_process(client_process if client_process is not None else _live_client_process())
    if free_disk_bytes is not None:
        host.set_disk_space(
            DiskSpace(path=tmp_path, free_bytes=free_disk_bytes, total_bytes=free_disk_bytes * 2)
        )
    deps = build_runner_dependencies(
        store=store,
        # The real catalog and the real lua/ tree: this is what makes `implementation_ref`
        # resolution a fact rather than a claim.
        catalog_root=REPO_ROOT / "catalogs",
        lua_root=REPO_ROOT,
        seedset_root=tmp_path / "seedsets",
        host=host,
        host_info=HostInfo(
            os=OperatingSystem.linux, os_version="test", session_type=LinuxSessionType.x11
        ),
        nexus_client_factory=lambda: NexusClient(host="127.0.0.1", port=port),
        provider=provider,
        support_probe=SupportProbeResult(
            quicksave_path_verified=True,
            capture_hygiene_spike_passed=False,
            reason="test: quicksave path verified, capture hygiene spike not run",
        ),
        observation_declaration_ids=(TURN_STATE, SCREEN_STATE),
        window_provider=lambda: None,
        run_lock=run_lock if run_lock is not None else RunIdentityLock(tmp_path / "run-locks"),
        disk_check_path=tmp_path,
        home=tmp_path,
        save_loader=save_loader,
    )
    return deps, store


# --------------------------------------------------------------------------
# The test
# --------------------------------------------------------------------------


def test_run_start_plays_and_persists_turns_through_the_real_composition_root(
    tmp_path: Path,
) -> None:
    """`civsim run start` reaches a runner, plays to its stop condition, and records every turn."""
    _write_seed_set(tmp_path / "seedsets")
    config_path = tmp_path / "run.yaml"
    config_values = _write_run_config(config_path)

    saves_dir = FakeHostPlatform().resolve_game_directories(home=tmp_path).saves_dir
    game = _FakeGame(saves_dir=saves_dir, config_values=config_values)
    provider = _build_provider()

    with _ServerThread(FakeNexusServer()) as server:
        _script_game(server, game)
        runner, store = _compose(tmp_path, port=server.port, provider=provider)
        cli.configure_runner_factory(lambda: runner)

        result = CliRunner().invoke(cli.app, ["run", "start", str(config_path)])

        assert result.exit_code == 0, _failure_report(result.output, store)
        run_id = _wait_for_terminal_run(runner, store, result.output)

    # -- the run itself ---------------------------------------------------------------------
    run = store.get_run(run_id)
    assert run is not None
    assert run.lifecycle_state is LifecycleState.FINISHED
    assert run.stop_resolution is StopResolution.TURN_REACHED
    assert run.game_build == GAME_BUILD

    # -- the turns were played, in the game, and recorded ------------------------------------
    assert game.end_turns_issued == STOP_AT_TURN
    assert store.turn_gaps(run_id) == []
    for turn_number in range(1, STOP_AT_TURN + 1):
        cycle = store.get_turn_cycle(run_id, turn_number)
        assert cycle is not None, f"turn {turn_number} was never persisted"
        assert cycle.steps, f"turn {turn_number} recorded no decision steps"
        assert store.step_gaps(run_id, turn_number) == []

    # -- FR-007: a real quicksave per turn, verified on the filesystem -----------------------
    assert len(game.saves_written) == STOP_AT_TURN
    assert len(store.list_save_points(run_id)) == STOP_AT_TURN

    # -- T214/FR-006: the run's NexusClient is released once the run is terminal --------------
    # The tuner accepts one connection at a time (research R4), so a client held past a finished
    # run is a client a second `run start` in this process can never get. `connection_health`
    # reports `no_run` only once this run's context (and with it its client) has been dropped.
    health = runner.get_status(run_id).connection_health
    assert health.tuner == "no_run", (
        "the finished run's Nexus client was never released; a second run in this process "
        f"could not connect (connection_health={health!r})"
    )

    # -- T220/FR-050: capture_path is measured, not declared ---------------------------------
    # This composition resolves no game window (`window_provider=lambda: None`), so there is no
    # capture mechanism to name -- `NONE` here is the measured result, not a placeholder.
    assert run.capture_path is CapturePath.NONE


def _wait_for_terminal_run(runner: Runner, store: SqliteMatchStore, output: str) -> str:
    """`run start` returns as soon as the run is *scheduled* -- the play loop runs on the runner's
    own background thread. Poll its status rather than sleeping a guessed interval."""
    run_id = _parse_run_id(output)
    deadline = 60.0
    waited = 0.0
    while waited < deadline:
        status = runner.get_status(run_id)
        if status.lifecycle_state in (LifecycleState.FINISHED, LifecycleState.FAILED):
            return run_id
        threading.Event().wait(0.05)
        waited += 0.05
    pytest.fail(
        f"run {run_id} never reached a terminal state; last status: "
        f"{runner.get_status(run_id)!r}\n{_failure_report(output, store)}"
    )


def _failure_report(output: str, store: SqliteMatchStore) -> str:
    """CLI output plus whatever preparation actually recorded.

    A failed preparation prints only `lifecycle_state: failed`; the field-by-field reason lives in
    the `preparation_mismatch` event. Surfacing it here is the difference between a diagnosable
    failure and a re-run with a debugger attached.
    """
    lines = [output, "--- recorded run events ---"]
    try:
        run_id = _parse_run_id(output)
    except AssertionError:
        return "\n".join(lines + ["(no run was created)"])
    for event in store.list_run_events(run_id):
        lines.append(f"{event.event_type.value}: {event.detail}")
    return "\n".join(lines)


def _parse_run_id(output: str) -> str:
    for line in output.splitlines():
        if line.startswith("run_id:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"no run_id in CLI output:\n{output}")


def test_unreadable_configured_field_fails_the_run_closed_rather_than_passing_v2(
    tmp_path: Path,
) -> None:
    """A configured field the client cannot report is recorded as a mismatch, never as agreement.

    This is the specific regression guard for the composition root's earlier `_read_setting`,
    which echoed each configured value back as its own "actual" -- making V2 pass vacuously for
    every field, including ones no live read could ever confirm. FR-002/V2 require the opposite
    direction: a run whose setup cannot be *confirmed* must not produce data.
    """
    _write_seed_set(tmp_path / "seedsets")
    config_path = tmp_path / "run.yaml"
    config_values = _write_run_config(config_path)

    saves_dir = FakeHostPlatform().resolve_game_directories(home=tmp_path).saves_dir
    game = _FakeGame(saves_dir=saves_dir, config_values=config_values)
    # The client answers everything except the difficulty -- the shape of a build whose
    # configuration API does not expose one of the fields this run configured.
    game.read_setup = lambda _command: {  # type: ignore[method-assign]
        **{
            key.replace(".", "__"): value
            for key, value in config_values.items()
            if key != "difficulty"
        },
        "turn_timer_type": "TURNTIMER_NONE",
        "turn_timer_hash": -1525060181,
    }
    provider = _build_provider()

    with _ServerThread(FakeNexusServer()) as server:
        _script_game(server, game)
        runner, store = _compose(tmp_path, port=server.port, provider=provider)
        cli.configure_runner_factory(lambda: runner)

        result = CliRunner().invoke(cli.app, ["run", "start", str(config_path)])

        # A preflight mismatch creates the run in `failed` and exits non-zero -- it does not
        # raise, and it never plays a turn.
        assert result.exit_code == 1, result.output
        run_id = _parse_run_id(result.output)

    run = store.get_run(run_id)
    assert run is not None
    assert run.lifecycle_state is LifecycleState.FAILED
    assert game.end_turns_issued == 0
    assert store.get_turn_cycle(run_id, 1) is None

    mismatch_events = [
        event
        for event in store.list_run_events(run_id)
        if event.event_type.value == "preparation_mismatch"
    ]
    assert mismatch_events, "the unreadable field was not recorded as a preparation mismatch"
    fields = {
        mismatch["field"]
        for event in mismatch_events
        for mismatch in event.detail.get("mismatches", [])
    }
    assert "difficulty" in fields


def test_a_replayed_turn_is_recorded_alongside_the_attempt_it_superseded(
    tmp_path: Path,
) -> None:
    """T223 / FR-047: the composition root gives a replayed turn a fresh `attempt_index`.

    A rewind (`Runner.resume_from`) marks the attempt at its target turn non-authoritative and
    plays that turn again. Superseding does **not** free the attempt's
    `(run_id, turn_number, attempt_index)` triple, and `run_turn_cycle` used to open every turn at
    a hard-coded `attempt_index = 0` -- so the replay's own persist hit
    `sqlite_adapter.write_turn_cycle`'s D4 check ("different content for the same triple") and, per
    FR-013, halted the run instead of advancing it. FR-047 requires **both** attempts on record.

    Driven through the real composition root deliberately. Passing an `attempt_index_base` by hand
    to `run_turn_cycle` would prove only that the seam exists; what this asserts is that the
    production wiring actually resolves it from what the store already holds -- the difference the
    integration audit was written about.
    """
    _write_seed_set(tmp_path / "seedsets")
    config_path = tmp_path / "run.yaml"
    config_values = _write_run_config(config_path)

    saves_dir = FakeHostPlatform().resolve_game_directories(home=tmp_path).saves_dir
    game = _FakeGame(saves_dir=saves_dir, config_values=config_values)
    provider = _build_provider()

    loop = asyncio.new_event_loop()
    with _ServerThread(FakeNexusServer()) as server:
        _script_game(server, game)
        deps, store = _compose_dependencies(tmp_path, port=server.port, provider=provider)
        config = load_run_configuration_file(config_path)
        try:
            # One loop for the whole test: `prepare_run` connects the `NexusClient` whose lock and
            # streams bind to whichever loop first awaits them, and every per-turn dispatch below
            # goes through that same client (composition.py's "one cross-loop hazard").
            prepared = loop.run_until_complete(deps.prepare_run(config))
            run_id = prepared.run.run_id

            first = deps.build_turn_dependencies(prepared, 1)
            assert first.attempt_index_base == 0, (
                "a turn with nothing on record is not a replay; it must open at attempt 0"
            )
            outcome = loop.run_until_complete(run_turn_cycle(first, run=prepared.run))

            # What a rewind to turn 1 does to the record, before replaying it (FR-047, I9).
            store.mark_turn_superseded(run_id, 1, 0)

            replay = deps.build_turn_dependencies(prepared, 1)
            assert replay.attempt_index_base == 1, (
                "the replay reused the superseded attempt's index; its persist would be "
                "rejected by the store's D4 check and the run would halt (T223)"
            )
            loop.run_until_complete(run_turn_cycle(replay, run=outcome.run))
        finally:
            loop.close()

    # Both attempts are readable, and exactly one is authoritative (FR-047, invariant I9).
    attempts = _attempt_rows(tmp_path / "match-store.db", run_id, turn_number=1)
    assert [row[0] for row in attempts] == [0, 1]
    assert [row[1] for row in attempts] == [0, 1], (
        "the superseded attempt must stay on record as non-authoritative and the replay must be "
        f"the authoritative one; got {attempts!r}"
    )
    # The replay is genuinely the turn as replayed, not a duplicate row of the abandoned one.
    record = store.get_turn_cycle(run_id, 1)
    assert record is not None
    assert record.turn_cycle.attempt_index == 1
    assert record.steps, "the replayed attempt recorded no decision steps"
    assert store.turn_gaps(run_id) == []


def _attempt_rows(db_path: Path, run_id: str, *, turn_number: int) -> list[tuple[int, int]]:
    """Every attempt row for one turn, read straight from SQLite.

    `get_turn_cycle` answers with one record at a time by design, so it cannot show that two
    attempts coexist -- which is the whole claim FR-047 makes.
    """
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        return [
            (int(index), int(authoritative))
            for index, authoritative in conn.execute(
                "SELECT attempt_index, is_authoritative FROM turn_cycles "
                "WHERE run_id = ? AND turn_number = ? ORDER BY attempt_index",
                (run_id, turn_number),
            ).fetchall()
        ]
    finally:
        conn.close()


def test_run_start_reaches_a_runner_without_any_bootstrap_wiring() -> None:
    """The audit's headline failure, guarded directly: importing the CLI is enough.

    Before T209 nothing in production ever called `configure_runner_factory`, so `run start` died
    on its first line with "no runner is configured" -- before the configuration file was even
    parsed. The factory is assigned at import and constructs nothing until asked, so this asserts
    the wiring exists without building a catalog, a store, or a socket.

    Checked in a **subprocess** rather than against this process's own `cli` module: the wiring
    under test is "what a fresh `civsim` process sees", and `_runner_factory` is module-global
    state other test modules legitimately clear and re-point at their own fakes. Asserting it
    in-process would make this test a reporter of suite ordering rather than of the wiring.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import civsim_harness.operator.cli as c;"
            "assert c._runner_factory is c.default_runner_factory, c._runner_factory;"
            "print('wired')",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "wired" in result.stdout


class _RecordingRunIdentityLock(RunIdentityLock):
    """A real lock (same files, same refusals) that also records what it was asked to do.

    Subclassed rather than faked on purpose: the claim under test is that the *production*
    preparation path claims and releases a run identity, so the lock's own filesystem behaviour
    must stay real -- a stand-in would only prove this test can call `acquire` itself, which is
    precisely the shape of assurance T227 was filed about.
    """

    def __init__(self, lock_dir: Path) -> None:
        super().__init__(lock_dir)
        self.acquired: list[tuple[str, int]] = []
        self.released: list[str] = []

    def acquire(self, *, run_id: Any, client_pid: int, now: Any) -> Any:
        lock = super().acquire(run_id=run_id, client_pid=client_pid, now=now)
        self.acquired.append((str(run_id), client_pid))
        return lock

    def release(self, run_id: Any) -> None:
        super().release(run_id)
        self.released.append(str(run_id))


def test_a_run_claims_and_releases_its_run_identity(tmp_path: Path) -> None:
    """T227 / FR-006, V8, research R4: the run-identity lock is claimed in production.

    `run/identity_lock.py` was complete from the day it was written and **constructed nowhere in
    `src/`**, so the case it exists for -- two harness processes playing one `run_id` against two
    *different* clients, writing two divergent records under one run id -- was unguarded. The
    game's own one-tuner-at-a-time limit does not cover it; that is the case the lock module
    explicitly says it does not duplicate.

    Also asserts the release, because a claim that is never given up is its own defect: a finished
    run would refuse its own `resume-from`, and an operator would have to delete a file by hand.
    """
    _write_seed_set(tmp_path / "seedsets")
    config_path = tmp_path / "run.yaml"
    config_values = _write_run_config(config_path)

    saves_dir = FakeHostPlatform().resolve_game_directories(home=tmp_path).saves_dir
    game = _FakeGame(saves_dir=saves_dir, config_values=config_values)
    lock = _RecordingRunIdentityLock(tmp_path / "run-locks")

    with _ServerThread(FakeNexusServer()) as server:
        _script_game(server, game)
        runner, store = _compose(
            tmp_path, port=server.port, provider=_build_provider(), run_lock=lock
        )
        cli.configure_runner_factory(lambda: runner)
        result = CliRunner().invoke(cli.app, ["run", "start", str(config_path)])
        assert result.exit_code == 0, _failure_report(result.output, store)
        run_id = _wait_for_terminal_run(runner, store, result.output)

    # Claimed exactly once, for this run, keyed to the PID the host actually located -- never a
    # fabricated or placeholder one.
    assert lock.acquired == [(run_id, _live_client_process().pid)]

    # ...and given up once the run reached a terminal state.
    assert run_id in lock.released
    assert not lock.is_active(RunId(run_id))
    assert list((tmp_path / "run-locks").glob("*.lock.json")) == []


def test_a_raise_between_lock_acquire_and_hand_off_still_releases_it(tmp_path: Path) -> None:
    """T227/T289: the specific leak this lane was asked to fix.

    Before `RunIdentityLock.guard` (identity_lock.py), `run/composition.py`'s
    `_prepare_connected_run` acquired the run-identity lock once and released it from three
    independently *conditional* branches (the two `_fail_preparation` calls, and -- far later --
    `evaluate_stop_facts`/the terminal-run sweep) with no `try`/`finally` spanning acquire through
    hand-off. Anything raised between the acquire and one of those branches -- a live read that
    failed, a store write that failed, anything this function did not already name -- reached
    neither release and left `<lock_dir>/<run_id>.lock.json` on disk forever, silently blocking
    every later run under that identity. This is exactly such a path: preparation gets all the way
    past BOTH `_fail_preparation` checks (leader selection is not in play here, and V2 verification
    matches, the same configuration `test_a_run_claims_and_releases_its_run_identity` above proves
    succeeds end to end) and only then does `store.write_run_event` -- persisting the
    `preparing -> playing` transition -- fail.

    Without the fix in `run/composition.py`/`run/identity_lock.py`, this test fails: the lock file
    is still there after the raise. With it, `run_lock.guard`'s own `finally` releases it because
    `lock_handle.commit()` is never reached on this path.
    """
    _write_seed_set(tmp_path / "seedsets")
    config_path = tmp_path / "run.yaml"
    config_values = _write_run_config(config_path)

    saves_dir = FakeHostPlatform().resolve_game_directories(home=tmp_path).saves_dir
    game = _FakeGame(saves_dir=saves_dir, config_values=config_values)
    lock = _RecordingRunIdentityLock(tmp_path / "run-locks")

    loop = asyncio.new_event_loop()
    with _ServerThread(FakeNexusServer()) as server:
        _script_game(server, game)
        deps, store = _compose_dependencies(
            tmp_path, port=server.port, provider=_build_provider(), run_lock=lock
        )
        config = load_run_configuration_file(config_path)

        boom = RuntimeError(
            "simulated store failure between the run-identity lock's acquire and this run's "
            "hand-off to play -- a path none of the three old conditional releases covered"
        )

        def _boom(*_args: Any, **_kwargs: Any) -> Any:
            raise boom

        # The first `write_run_event` call in this (leader-check-skipped, V2-matching) path is
        # composition.py's own `preparing -> playing` transition -- well past both
        # `_fail_preparation` branches, and well before `run_contexts[...]` is populated or
        # `lock_handle.commit()` is reached.
        store.write_run_event = _boom  # type: ignore[method-assign]

        try:
            with pytest.raises(RuntimeError) as excinfo:
                loop.run_until_complete(deps.prepare_run(config))
            assert excinfo.value is boom
        finally:
            loop.close()

    # The acquire genuinely happened -- otherwise this test would prove nothing.
    assert lock.acquired, "the run-identity lock was never acquired; this test proves nothing"
    run_id, _pid = lock.acquired[0]

    # T227/T289: the raise must still have released it.
    assert run_id in lock.released, f"{run_id}'s lock leaked across the raise"
    assert not lock.is_active(RunId(run_id))
    assert list((tmp_path / "run-locks").glob("*.lock.json")) == [], (
        "the lock file is still on disk after an exception between acquire and hand-off -- "
        "exactly the production defect this test is written to catch"
    )


def test_a_raise_between_commit_and_stop_resolution_still_releases_the_lock_at_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gap `test_a_raise_between_lock_acquire_and_hand_off_still_releases_it` does NOT cover:
    `RunIdentityLock.guard`'s own `finally` only protects the span up to `lock_handle.commit()`
    (`run/composition.py:~1140`) -- after that, the guard's `with` block has already exited (the
    lock's remaining lifetime was deliberately handed to the run, per `RunLockHandle.commit`'s own
    docstring), so an exception raised anywhere past that point -- which, measured against the
    real runner, is most of `Runner._play_run`'s own terminal paths (an operator stop, or any of
    `RecoveryLimitReached`/`SaveAddressingError`/`UnknownScreenEncountered`/
    `ProviderChainExhausted`/the unclassified tail landing the run in `paused`/`failed`) -- never
    reaches `composition.py`'s `evaluate_stop_facts`, and the lock is never released by anything
    still on the stack. This is that shape, reproduced directly against `RunIdentityLock`/
    `RunLockHandle` (production code, real lock file, no fake game needed: the leak is in the lock
    module's own contract, not in anything the game wiring does).

    A run that never reaches stop-resolution must still not wedge the run identity forever once
    this *process* exits -- `RunLockHandle.commit` arms a process-exit backstop for exactly this,
    which this test triggers directly (standing in for the interpreter's own `atexit` dispatch,
    without touching the real global `atexit` registry -- pytest, coverage, and everything else
    also registered there must keep working after this test runs).
    """
    registered: list[Any] = []
    unregistered: list[Any] = []
    monkeypatch.setattr(
        atexit, "register", lambda fn, *a, **k: (registered.append(fn), fn)[1]
    )
    monkeypatch.setattr(atexit, "unregister", lambda fn: unregistered.append(fn))

    lock_dir = tmp_path / "run-locks"
    lock = RunIdentityLock(lock_dir)
    run_id = RunId("run-post-commit-raise")
    lock_path = lock_dir / f"{run_id}.lock.json"

    class _SimulatedMidRunFailure(Exception):
        """Stands in for e.g. `RecoveryLimitReached` -- a `HarnessError` `Runner._handle_run_
        failure` routes to `paused`/`failed` WITHOUT ever calling `evaluate_stop_facts`."""

    with pytest.raises(_SimulatedMidRunFailure):
        with lock.guard(run_id=run_id, client_pid=os.getpid(), now=datetime.now(UTC)) as handle:
            # T227/T289: exactly `run/composition.py:1140`'s own call -- past this point, the
            # guard's `finally` will NOT release the lock; the run is on its own.
            handle.commit()
            # ...the drive phase would begin here, in a separate scheduled coroutine the guard's
            # own stack frame is long gone by the time it runs (`Runner._begin` schedules
            # `_play_run` as its own task) -- this raise stands in for any of the several
            # terminal paths that reach neither `evaluate_stop_facts` nor a later `prepare()`.
            raise _SimulatedMidRunFailure("simulated failure after commit, before stop-resolution")

    # commit() must have armed exactly one process-exit backstop for this run id -- if it did
    # not, nothing is left to release the lock once this process exits, and everything below
    # would (correctly) fail.
    assert len(registered) == 1, (
        "commit() did not arm a process-exit backstop; a run that raises after commit() and "
        "before stop-resolution would leak its lock forever once this process exits"
    )
    # The guard's own `finally` must NOT have released a committed lock -- unchanged from
    # `0187345`, and the whole reason a backstop is needed at all.
    assert lock_path.exists(), (
        "the guard released a COMMITTED lock on its own -- that would break resume-from/branch, "
        "which need the lock held for as long as the run is genuinely still playing"
    )
    assert not unregistered, "nothing released the lock yet, so nothing should be disarmed yet"

    # Stand in for the interpreter's own exit sequence invoking the one backstop it has for this
    # run id -- this is the load-bearing step: without Part 1's fix, `registered` is empty above
    # and this line is unreachable; with it, this is what actually happens at process exit.
    registered[0]()

    # The load-bearing assertion: the lock the run never released itself is gone once this
    # process's own exit sequence has run -- an exception between `commit()` and stop-resolution
    # no longer wedges the run identity forever.
    assert not lock_path.exists(), (
        "the lock file is still on disk after the process-exit backstop ran -- an exception "
        "between commit() and stop-resolution still leaks the lock"
    )
    assert unregistered == [registered[0]], "the fired backstop must disarm itself"


# --------------------------------------------------------------------------
# T233 -- the crash/hang detection layer, through the real composition root
# --------------------------------------------------------------------------


def _pid_that_is_not_running() -> int:
    """A PID that is genuinely not a live process on this machine, right now.

    Searched rather than hard-coded: a fixed "obviously dead" PID is exactly the assumption that
    makes a detection test pass for the wrong reason on somebody else's box. `psutil.pid_exists`
    is the same question `resilience/liveness.py` asks, so a candidate it rejects is one the
    production path will also read as gone.
    """
    for candidate in range(4_000_000, 4_001_000):
        if not psutil.pid_exists(candidate):
            return candidate
    raise AssertionError("no free PID could be found to stand in for a killed client")


def test_a_killed_client_is_detected_and_recorded_by_the_production_run_loop(
    tmp_path: Path,
) -> None:
    """T233 / FR-044, SC-010: the detection layer runs during a real run and records the crash.

    `DetectionAggregator`, `HeartbeatMonitor` and `ProcessLivenessMonitor` were complete and
    individually tested, and **constructed nowhere in `src/`** -- so SC-010 ("a game client crash
    is detected and recorded within 60 seconds") had no mechanism behind it at all, and T193's
    live test would have failed for that headless reason rather than for anything about a client.

    Driven through the real composition root, against a host reporting a PID that is genuinely not
    a running process -- which is what a killed client looks like to `locate_game_process()`. The
    assertion is on the **recorded** `crash_detected` event, not on an internal flag: SC-010 asks
    for detected *and recorded*, and a detection nobody wrote down satisfies half of it.

    Removing the `detection=ctx.detection` line in `run/composition.py`'s
    `build_turn_dependencies` (or the `_detect_between_turns` call in `run/turn_cycle.py`) makes
    this test fail -- verified by reverting each in turn.
    """
    _write_seed_set(tmp_path / "seedsets")
    config_path = tmp_path / "run.yaml"
    config_values = _write_run_config(config_path)

    saves_dir = FakeHostPlatform().resolve_game_directories(home=tmp_path).saves_dir
    game = _FakeGame(saves_dir=saves_dir, config_values=config_values)
    dead_pid = _pid_that_is_not_running()

    with _ServerThread(FakeNexusServer()) as server:
        _script_game(server, game)
        runner, store = _compose(
            tmp_path,
            port=server.port,
            provider=_build_provider(),
            client_process=GameProcess(
                pid=dead_pid, name="CivilizationVI_FAKE", executable_path=None
            ),
        )
        cli.configure_runner_factory(lambda: runner)
        result = CliRunner().invoke(cli.app, ["run", "start", str(config_path)])
        assert result.exit_code == 0, _failure_report(result.output, store)
        run_id = _parse_run_id(result.output)
        _wait_for_recorded_event(runner, store, run_id, "crash_detected")

    crash_events = [
        event
        for event in store.list_run_events(RunId(run_id))
        if event.event_type.value == "crash_detected"
    ]
    assert crash_events, (
        "the client PID was not a live process for the whole run and nothing detected it; "
        "SC-010 has no mechanism behind it. Recorded events: "
        f"{[event.event_type.value for event in store.list_run_events(RunId(run_id))]}"
    )
    assert crash_events[0].detail["pid"] == dead_pid

    # The crash is caught *before* the turn commits to anything: no quicksave was taken, no turn
    # was persisted, and the game was never told to end a turn.
    assert game.saves_written == []
    assert game.end_turns_issued == 0
    assert store.get_turn_cycle(RunId(run_id), 1) is None

    # ...and the run stops visibly rather than grinding on against a client that is not there.
    status = runner.get_status(RunId(run_id))
    assert status.lifecycle_state is LifecycleState.PAUSED
    assert status.last_error is not None
    assert status.last_error.type == "ClientFaultDetected"


def test_every_turn_is_built_with_the_detection_layer_attached(tmp_path: Path) -> None:
    """T233: the composition root hands each turn its `DetectionWatch`, not merely builds one.

    The whole shape of defect Phase 11 was written about is a collaborator that is constructed and
    then never asked anything. Asserting on `TurnCycleDependencies.detection` is the narrowest
    statement of the wiring itself: it fails if `build_turn_dependencies` stops passing it through,
    even in a run where nothing ever trips.
    """
    _write_seed_set(tmp_path / "seedsets")
    config_path = tmp_path / "run.yaml"
    config_values = _write_run_config(config_path)

    saves_dir = FakeHostPlatform().resolve_game_directories(home=tmp_path).saves_dir
    game = _FakeGame(saves_dir=saves_dir, config_values=config_values)

    loop = asyncio.new_event_loop()
    with _ServerThread(FakeNexusServer()) as server:
        _script_game(server, game)
        deps, _store = _compose_dependencies(
            tmp_path, port=server.port, provider=_build_provider()
        )
        config = load_run_configuration_file(config_path)
        try:
            prepared = loop.run_until_complete(deps.prepare_run(config))
            turn_deps = deps.build_turn_dependencies(prepared, 1)
        finally:
            loop.close()

    assert turn_deps.detection is not None, (
        "the run's detection layer never reaches the turn cycle; research R12's signals are "
        "constructed and then asked nothing, which is SC-010 with no mechanism behind it (T233)"
    )
    assert turn_deps.detection_interval_s > 0


def _wait_for_recorded_event(
    runner: Runner, store: SqliteMatchStore, run_id: str, event_type: str
) -> None:
    """Poll until *event_type* is on *run_id*'s timeline, or the run stops, or time runs out.

    `run start` returns as soon as the run is *scheduled*; the detection pass happens on the
    runner's own background thread. Polling the record rather than sleeping a guessed interval is
    the same discipline `_wait_for_terminal_run` uses.
    """
    deadline = 60.0
    waited = 0.0
    while waited < deadline:
        recorded = {
            event.event_type.value for event in store.list_run_events(RunId(run_id))
        }
        if event_type in recorded:
            return
        if runner.get_status(RunId(run_id)).lifecycle_state in (
            LifecycleState.FINISHED,
            LifecycleState.FAILED,
            LifecycleState.PAUSED,
        ):
            # The run stopped; one more read so a race between the stop and the event write
            # cannot report a false negative.
            if event_type in {
                event.event_type.value for event in store.list_run_events(RunId(run_id))
            }:
                return
            return
        threading.Event().wait(0.05)
        waited += 0.05


# --------------------------------------------------------------------------
# T226 -- `civsim run branch` reaches a branch
# --------------------------------------------------------------------------


def _write_branch_config(path: Path, *, parent_run_id: str, turn: int) -> None:
    """A branch document: a `branch_from` block plus only what a branch may vary.

    Deliberately restates none of the inherited fields (seed, civilization, ruleset, mod set, map
    and game settings) -- they come from the *parent's* recorded `RunConfiguration`, which is the
    whole reason the branch loader needs `MatchStore.get_run_configuration` and the reason a
    branch document cannot be loaded by the ordinary run-configuration loader.
    """
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "config_id": "e2e-wiring-branch",
                "branch_from": {"run_id": parent_run_id, "turn": turn},
                # A branch may vary its stop condition; it may not vary its seed.
                "stop_condition": {"type": "turn_reached", "turn": turn},
            }
        ),
        encoding="utf-8",
    )


def _branch_argv(parent_run_id: str, turn: int, branch_path: Path, tmp_path: Path) -> list[str]:
    return [
        "run",
        "branch",
        parent_run_id,
        "--turn",
        str(turn),
        "--config",
        str(branch_path),
        "--store-path",
        str(tmp_path / "match-store.db"),
    ]


def test_run_branch_reaches_a_branch_instead_of_failing_at_configuration_load(
    tmp_path: Path,
) -> None:
    """T226 / FR-033, FR-034, SC-014: `run branch` gets past the loader and records lineage.

    **The defect this replaces.** `operator/cli.py`'s `run_branch` forwarded the document to
    `Runner.start`, which used the **non-branch** loader -- and `RunConfiguration` is
    `extra="forbid"`, so a `branch_from` block failed validation outright (`PreflightError ...
    'Extra inputs are not permitted'`). Every branch command therefore died before preparation
    began, and the whole branch machinery (`load_branch_configuration_file`, `parse_branch_from`,
    `check_branch_build`, `create_branch`, `BranchSource`) had zero production callers.

    The save load is a scripted recording loader here, so the assertions can be about *which*
    save was loaded; the production default (`saves/load_game.py`'s `LuaSaveLoader`, T217) is
    exercised by the two tests that follow. Everything else is real: the real CLI, the real
    composition root, the real `create_branch`, the real store.
    """
    _write_seed_set(tmp_path / "seedsets")
    config_path = tmp_path / "run.yaml"
    config_values = _write_run_config(config_path)
    saves_dir = FakeHostPlatform().resolve_game_directories(home=tmp_path).saves_dir
    game = _FakeGame(saves_dir=saves_dir, config_values=config_values)

    loaded: list[Any] = []

    class _RecordingLoader:
        async def load(self, save: Any) -> None:
            loaded.append(save)

    with _ServerThread(FakeNexusServer()) as server:
        _script_game(server, game)
        deps, store = _compose_dependencies(
            tmp_path,
            port=server.port,
            provider=_build_provider(),
            save_loader=_RecordingLoader(),
        )
        runner = Runner(deps)
        cli.configure_runner_factory(lambda: runner)

        started = CliRunner().invoke(cli.app, ["run", "start", str(config_path)])
        assert started.exit_code == 0, _failure_report(started.output, store)
        parent_run_id = _wait_for_terminal_run(runner, store, started.output)

        branch_path = tmp_path / "branch.yaml"
        _write_branch_config(branch_path, parent_run_id=parent_run_id, turn=1)
        branched = CliRunner().invoke(
            cli.app, _branch_argv(parent_run_id, 1, branch_path, tmp_path)
        )
        assert "Extra inputs are not permitted" not in branched.output, (
            "run branch is still routing the branch document through the non-branch loader"
        )
        assert branched.exit_code == 0, _failure_report(branched.output, store)
        child_run_id = _parse_run_id(branched.output)
        _wait_for_terminal_run(runner, store, branched.output)

    # -- FR-033: the lineage is on the record, on the child, naming parent run and turn ---------
    child = store.get_run(RunId(child_run_id))
    assert child is not None
    assert child.parent_run_id == parent_run_id
    assert child.parent_turn == 1
    assert child.run_id != parent_run_id

    branch_events = [
        event
        for event in store.list_run_events(RunId(child_run_id))
        if event.event_type.value == "branch_created"
    ]
    assert branch_events, "no branch_created event was recorded (FR-033)"
    assert branch_events[0].detail["parent_run_id"] == parent_run_id
    assert branch_events[0].detail["parent_turn"] == 1

    # -- FR-036: the parent's own save was loaded, not a nearby one ----------------------------
    assert len(loaded) == 1
    assert loaded[0].run_id == parent_run_id
    assert loaded[0].turn_number == 1

    # -- T226's binding half: comparability and host platform are this host's, not a claim -----
    # This composition's `support_probe` reports the R6 capture-hygiene spike as NOT passed, so
    # the host gate resolves `visually_degraded`. A branch must record that, never `comparable`.
    assert child.comparability_status is ComparabilityStatus.VISUALLY_DEGRADED
    assert child.host_platform["os"] == "linux"
    parent = store.get_run(RunId(parent_run_id))
    assert parent is not None
    assert child.comparability_status == parent.comparability_status, (
        "the branch recorded a different comparability than the identically-composed run it "
        "branched from; one of the two is not reporting what the host gate resolved"
    )

    # -- FR-034 / I12: the parent's own record is untouched by the branch -----------------------
    assert parent.parent_run_id is None
    assert store.turn_gaps(RunId(parent_run_id)) == []


def _script_load_path(server: FakeNexusServer, game: _FakeGame) -> None:
    """The three commands the production `LuaSaveLoader` (T217) issues, answered the way the
    T217 spike measured a real client answering them: the exit flips the phase to the front
    end, an accepted `Network.LoadGame` flips it back to a loaded game *and restores the saved
    position*, and the far-side read-back reports that position. Queued after `_script_game`'s
    entries, which none of these commands match, and each pinned to the Lua state the loader
    must issue it in -- a mis-targeted command falls through to the fake's generic default,
    which the loader correctly refuses."""

    def _exit_to_menu(_command: ReceivedCommand) -> dict[str, Any]:
        server.set_state_table(front_end_state_table())
        return {"issued": True, "error": ""}

    def _load_game(command: ReceivedCommand) -> dict[str, Any]:
        # A real load restores the saved position; save `civsim__<run>__t000N` was taken at the
        # start of turn N, so the counter the far side reports afterwards is N.
        marker = 'loadGame.Name = "'
        start = command.lua_body.index(marker) + len(marker)
        name = command.lua_body[start : command.lua_body.index('"', start)]
        game.turn = int(name.rsplit("__t", 1)[1])
        server.set_state_table(loaded_game_state_table())
        return {"issued": True, "accepted": True, "error": ""}

    def _verify_position(_command: ReceivedCommand) -> dict[str, Any]:
        return {
            "issued": True,
            "error": "",
            "turn": game.turn,
            "local_player": 0,
            "in_front_end": False,
        }

    server.queue_response(
        _exit_to_menu,
        match="Events.ExitToMainMenu",
        state_index=STATE_INDEX_IN_GAME,
        repeatable=True,
    )
    server.queue_response(
        _load_game,
        match="Network.LoadGame",
        state_index=STATE_INDEX_MAIN_MENU,
        repeatable=True,
    )
    server.queue_response(
        _verify_position,
        match="UI.IsInFrontEnd",
        state_index=STATE_INDEX_IN_GAME,
        repeatable=True,
    )


def test_run_branch_through_the_production_loader_loads_the_parent_save_and_plays(
    tmp_path: Path,
) -> None:
    """T217 x T226: with **no** `save_loader=` injected, the composition default is the
    production `LuaSaveLoader`, and a branch completes end to end through it: exit to the front
    end, the verified `Network.LoadGame` table from `MainMenu`, the far-side position
    verification, then the branch's own preparation and a played turn. This is the wiring test
    that fails if the composition root stops constructing the production loader."""
    _write_seed_set(tmp_path / "seedsets")
    config_path = tmp_path / "run.yaml"
    config_values = _write_run_config(config_path)
    saves_dir = FakeHostPlatform().resolve_game_directories(home=tmp_path).saves_dir
    game = _FakeGame(saves_dir=saves_dir, config_values=config_values)

    with _ServerThread(FakeNexusServer()) as server:
        _script_game(server, game)
        _script_load_path(server, game)
        # No `save_loader=` -- the production default (LuaSaveLoader) applies.
        deps, store = _compose_dependencies(
            tmp_path, port=server.port, provider=_build_provider()
        )
        runner = Runner(deps)
        cli.configure_runner_factory(lambda: runner)

        started = CliRunner().invoke(cli.app, ["run", "start", str(config_path)])
        assert started.exit_code == 0, _failure_report(started.output, store)
        parent_run_id = _wait_for_terminal_run(runner, store, started.output)

        branch_path = tmp_path / "branch.yaml"
        _write_branch_config(branch_path, parent_run_id=parent_run_id, turn=1)
        branched = CliRunner().invoke(
            cli.app, _branch_argv(parent_run_id, 1, branch_path, tmp_path)
        )
        assert branched.exit_code == 0, _failure_report(branched.output, store)
        child_run_id = _parse_run_id(branched.output)
        _wait_for_terminal_run(runner, store, branched.output)

        load_commands = [
            cmd for cmd in server.received if "Network.LoadGame" in cmd.lua_body
        ]

    # -- the parent's own turn-1 save went through the verified front-end call ----------------
    assert len(load_commands) == 1
    assert f'loadGame.Name = "civsim__{parent_run_id}__t0001"' in load_commands[0].lua_body
    assert "ServerType.SERVER_TYPE_NONE" in load_commands[0].lua_body

    # -- and the branch is a real branch: lineage recorded, its own turn played ---------------
    child = store.get_run(RunId(child_run_id))
    assert child is not None
    assert child.parent_run_id == parent_run_id
    assert child.parent_turn == 1
    assert store.get_turn_cycle(RunId(child_run_id), 1) is not None, (
        "the branch never played its first turn after the load"
    )


def test_run_branch_whose_live_load_fails_refuses_loudly_and_creates_nothing(
    tmp_path: Path,
) -> None:
    """T226 x T217: a branch whose live save load fails must refuse by name, never start an
    unbranched run.

    The production default (`saves/load_game.py`'s `LuaSaveLoader`) is wired and reached -- but
    this fake game does not implement the load path, so its first step
    (`Events.ExitToMainMenu()`) comes back without the shape a real client prints, and the
    loader fails loudly naming that step and the save. A branch that cannot load its parent's
    save is not a branch, and starting from turn 1 of a fresh game while claiming to be one is
    far worse than refusing -- so the refusal is specific, and **creates nothing**.
    """
    _write_seed_set(tmp_path / "seedsets")
    config_path = tmp_path / "run.yaml"
    config_values = _write_run_config(config_path)
    saves_dir = FakeHostPlatform().resolve_game_directories(home=tmp_path).saves_dir
    game = _FakeGame(saves_dir=saves_dir, config_values=config_values)

    with _ServerThread(FakeNexusServer()) as server:
        _script_game(server, game)
        # Deliberately no `_script_load_path`: the load path is what fails here.
        # No `save_loader=` -- the production default applies, which is the point.
        deps, store = _compose_dependencies(
            tmp_path, port=server.port, provider=_build_provider()
        )
        runner = Runner(deps)
        cli.configure_runner_factory(lambda: runner)

        started = CliRunner().invoke(cli.app, ["run", "start", str(config_path)])
        assert started.exit_code == 0, _failure_report(started.output, store)
        parent_run_id = _wait_for_terminal_run(runner, store, started.output)

        branch_path = tmp_path / "branch.yaml"
        _write_branch_config(branch_path, parent_run_id=parent_run_id, turn=1)
        branched = CliRunner().invoke(
            cli.app, _branch_argv(parent_run_id, 1, branch_path, tmp_path)
        )

    assert branched.exit_code == 1, branched.output
    # Names the failing load step and the save, not a generic "preparation failed".
    assert "Events.ExitToMainMenu" in branched.output, branched.output
    assert f"civsim__{parent_run_id}__t0001" in branched.output, branched.output
    # ...and nothing was created: no run anywhere carries a branch lineage.
    assert _runs_with_a_parent(tmp_path / "match-store.db") == []


def test_run_branch_refuses_a_document_whose_lineage_disagrees_with_the_command(
    tmp_path: Path,
) -> None:
    """T226 / Principle IV: the lineage is stated twice and the two must agree.

    `civsim run branch <run_id> --turn N --config doc.yaml` names the lineage in the command and
    again inside the document. A disagreement is not a detail to reconcile silently: the lineage
    is the load-bearing claim a branch's entire comparative value rests on, so the mismatch is
    refused with both values named, before the runner is ever reached.
    """
    _write_seed_set(tmp_path / "seedsets")
    config_path = tmp_path / "run.yaml"
    config_values = _write_run_config(config_path)
    saves_dir = FakeHostPlatform().resolve_game_directories(home=tmp_path).saves_dir
    game = _FakeGame(saves_dir=saves_dir, config_values=config_values)

    with _ServerThread(FakeNexusServer()) as server:
        _script_game(server, game)
        deps, store = _compose_dependencies(
            tmp_path, port=server.port, provider=_build_provider()
        )
        runner = Runner(deps)
        cli.configure_runner_factory(lambda: runner)

        started = CliRunner().invoke(cli.app, ["run", "start", str(config_path)])
        assert started.exit_code == 0, _failure_report(started.output, store)
        parent_run_id = _wait_for_terminal_run(runner, store, started.output)

        branch_path = tmp_path / "branch.yaml"
        # The document says turn 2; the command below says turn 1.
        _write_branch_config(branch_path, parent_run_id=parent_run_id, turn=2)
        branched = CliRunner().invoke(
            cli.app, _branch_argv(parent_run_id, 1, branch_path, tmp_path)
        )

    assert branched.exit_code == 1, branched.output
    assert "branch_from" in branched.output
    assert _runs_with_a_parent(tmp_path / "match-store.db") == []


def _runs_with_a_parent(db_path: Path) -> list[str]:
    """Every run in the store that records a branch lineage, by id.

    Read straight from SQLite rather than through `list_active_runs`: the claim is "no branch was
    created **at all**", which a filter over only the non-terminal runs could not make.
    """
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        return [
            str(row[0])
            for row in conn.execute(
                "SELECT run_id FROM runs WHERE parent_run_id IS NOT NULL"
            ).fetchall()
        ]
    finally:
        conn.close()


# --------------------------------------------------------------------------
# T239 -- record_completeness_status is the derivation, through the real
# composition root
# --------------------------------------------------------------------------


def test_a_branch_completeness_becomes_the_derivation_once_it_has_a_record(
    tmp_path: Path,
) -> None:
    """T239's branch case: a branch is created UNKNOWN (nothing to judge yet) and, once its own
    record exists, both the persisted Run and `run status` must carry the derivation -- COMPLETE
    for the gap-free branch this test plays. Before T239 nothing in production ever re-derived
    the field: a finished branch stayed `unknown` forever on a perfect record, which is exactly
    what this fails with when the turn-persisted refresh (`run/turn_cycle.py`) and the read-time
    derivation (`Runner.get_status`) are reverted."""
    _write_seed_set(tmp_path / "seedsets")
    config_path = tmp_path / "run.yaml"
    config_values = _write_run_config(config_path)
    saves_dir = FakeHostPlatform().resolve_game_directories(home=tmp_path).saves_dir
    game = _FakeGame(saves_dir=saves_dir, config_values=config_values)

    class _NoOpLoader:
        async def load(self, save: Any) -> None:
            return None

    with _ServerThread(FakeNexusServer()) as server:
        _script_game(server, game)
        deps, store = _compose_dependencies(
            tmp_path, port=server.port, provider=_build_provider(), save_loader=_NoOpLoader()
        )
        runner = Runner(deps)
        cli.configure_runner_factory(lambda: runner)

        started = CliRunner().invoke(cli.app, ["run", "start", str(config_path)])
        assert started.exit_code == 0, _failure_report(started.output, store)
        parent_run_id = _wait_for_terminal_run(runner, store, started.output)

        branch_path = tmp_path / "branch.yaml"
        _write_branch_config(branch_path, parent_run_id=parent_run_id, turn=1)
        branched = CliRunner().invoke(
            cli.app, _branch_argv(parent_run_id, 1, branch_path, tmp_path)
        )
        assert branched.exit_code == 0, _failure_report(branched.output, store)
        child_run_id = _parse_run_id(branched.output)
        _wait_for_terminal_run(runner, store, branched.output)

        # Served: `run status` derives fresh from the record, never the creation-time constant.
        served = runner.get_status(RunId(child_run_id)).record_completeness_status
        assert served.value == "complete", (
            f"run status served {served.value!r} for a finished, gap-free branch -- the "
            "creation-time UNKNOWN was never re-derived (T239)"
        )

    # Persisted: the Run the store holds -- what crosses the port to Deliverable 1's trend
    # gate -- carries the derivation too.
    child = store.get_run(RunId(child_run_id))
    assert child is not None
    assert child.parent_run_id == parent_run_id
    assert child.record_completeness_status.value == "complete", (
        "a finished branch with a perfect record stayed stamped "
        f"{child.record_completeness_status.value!r} forever (T239's exact finding)"
    )
    # ...and the fresh run's persisted field is the same derivation.
    parent = store.get_run(RunId(parent_run_id))
    assert parent is not None
    assert parent.record_completeness_status.value == "complete"


# --------------------------------------------------------------------------
# T242 -- the V2 no-read-path fallback is closed: covered fields verify live,
# and anything that still falls back is recorded on the run
# --------------------------------------------------------------------------


def test_a_field_passing_v2_only_by_fallback_is_recorded_on_the_run(tmp_path: Path) -> None:
    """T242: `victory_types` now has a real read path (so it must NOT appear as unverified), and
    a configured field with no getter at all -- the open-ended custom-settings tail -- is
    RECORDED on the preparing -> playing transition event rather than silently wearing
    "verified". Far-side assertions only: what the store's timeline holds.

    Reverting either half fails this: with the recording reverted, the transition event carries
    no `v2_unverified_fields` at all; with the `game_settings.victory_types` getter reverted,
    the field falls back into the recorded list and the exact-list assertion fails."""
    _write_seed_set(tmp_path / "seedsets")
    config_path = tmp_path / "run.yaml"
    config_values = _write_run_config(
        config_path,
        extra_game_settings={
            # Covered by the new getter (T242): read back live, so V2 for it is real.
            "victory_types": ["VICTORY_CULTURE", "VICTORY_TECHNOLOGY"],
            # No getter exists or can exist for an arbitrary custom key: takes the fallback,
            # which must now be recorded, never silent.
            "experimental_toggle": "ON",
        },
    )

    saves_dir = FakeHostPlatform().resolve_game_directories(home=tmp_path).saves_dir
    game = _FakeGame(saves_dir=saves_dir, config_values=config_values)
    provider = _build_provider()

    with _ServerThread(FakeNexusServer()) as server:
        _script_game(server, game)
        runner, store = _compose(tmp_path, port=server.port, provider=provider)
        cli.configure_runner_factory(lambda: runner)

        result = CliRunner().invoke(cli.app, ["run", "start", str(config_path)])
        assert result.exit_code == 0, _failure_report(result.output, store)
        run_id = _wait_for_terminal_run(runner, store, result.output)

    playing_transitions = [
        event
        for event in store.list_run_events(RunId(run_id))
        if event.event_type.value == "lifecycle_transition"
        and event.detail.get("to") == "playing"
    ]
    assert len(playing_transitions) == 1
    detail = playing_transitions[0].detail
    assert detail.get("v2_unverified_fields") == ["game_settings.experimental_toggle"], (
        "the preparation event must name exactly the fields that passed V2 through the "
        "no-read-path fallback -- nothing more (victory_types has a real getter now) and "
        f"nothing less (silence is the T242 defect). Got: {detail!r}"
    )
    assert "seed-set agreement" in detail.get("v2_unverified_reason", "")


# --------------------------------------------------------------------------
# T250 -- phase-dependent settings are read in-game only, and an unobservable
# field is recorded rather than run-killing
# --------------------------------------------------------------------------


def test_unobservable_resources_is_recorded_on_the_run_not_run_killing(tmp_path: Path) -> None:
    """T250 halves one and two, far side: a run configuring `map_settings.resources` -- the
    field the peer's live session confirmed unreadable in either phase -- COMPLETES, with the
    field recorded on the preparing -> playing transition under its own `v2_unobservable_fields`
    marker (never conflated with T242's no-getter `v2_unverified_fields`); and the in-game-only
    reads (`mod_set`, `opponents.major_count`) genuinely ran in the one setup dispatch, at the
    run's post-load/pre-turn-1 in-game moment, through the phase-correct derivation.

    Reverting either half fails this: with the old `RESOURCES` sentinel getter restored, the
    field reads back unread, V2 records a mismatch, and the run dies before turn 1 (exit code 1
    here); with the `GetAIPlayerCount()` getter restored, the dispatched Lua assertion fails.
    """
    _write_seed_set(tmp_path / "seedsets")
    config_path = tmp_path / "run.yaml"
    config_values = _write_run_config(
        config_path,
        extra_map_settings={"resources": "RESOURCES_STANDARD"},
        extra_opponents={"major_count": 5},
    )
    # The client cannot report resources back -- that is the whole finding. The fake reporting
    # it anyway would be a fake with a getter the real game does not have.
    config_values.pop("map_settings.resources")

    saves_dir = FakeHostPlatform().resolve_game_directories(home=tmp_path).saves_dir
    game = _FakeGame(saves_dir=saves_dir, config_values=config_values)
    provider = _build_provider()

    with _ServerThread(FakeNexusServer()) as server:
        _script_game(server, game)
        runner, store = _compose(tmp_path, port=server.port, provider=provider)
        cli.configure_runner_factory(lambda: runner)

        result = CliRunner().invoke(cli.app, ["run", "start", str(config_path)])
        assert result.exit_code == 0, _failure_report(result.output, store)
        run_id = _wait_for_terminal_run(runner, store, result.output)

    run = store.get_run(run_id)
    assert run is not None
    assert run.lifecycle_state is LifecycleState.FINISHED

    # -- the unobservable field is recorded, distinctly, on the run itself --------------------
    playing_transitions = [
        event
        for event in store.list_run_events(RunId(run_id))
        if event.event_type.value == "lifecycle_transition"
        and event.detail.get("to") == "playing"
    ]
    assert len(playing_transitions) == 1
    detail = playing_transitions[0].detail
    assert detail.get("v2_unobservable_fields") == ["map_settings.resources"], (
        "the unobservable field must be recorded under its own marker on the transition that "
        f"concludes preparation. Got: {detail!r}"
    )
    assert "unobservable" in detail.get("v2_unobservable_reason", "")
    assert "map_settings.resources" not in detail.get("v2_unverified_fields", []), (
        "a live-confirmed unobservable field must never be conflated with the no-getter tail"
    )

    # -- the in-game-only fields were read for real, in-game, phase-correctly -----------------
    assert game.setup_lua_bodies, "the setup read-back never reached the fake tuner"
    setup_lua = game.setup_lua_bodies[0]
    assert "Modding.GetActiveMods" in setup_lua, (
        "mod_set was not read at the run's in-game moment -- the deferred comparison that "
        "never runs is the vacuous-pass pattern T250 exists to prevent"
    )
    assert "IsMajor" in setup_lua, "major_count was not read through the Players derivation"
    assert "GetAIPlayerCount" not in setup_lua, (
        "GetAIPlayerCount() counts city-states, Free Cities, and Barbarians in-game (T218's "
        "6-vs-16 decomposition) and must not be dispatched in any phase"
    )
    assert "RESOURCES" not in setup_lua, "the retired resources sentinel getter was dispatched"


def test_an_in_game_only_field_mismatch_still_fails_the_run_closed(tmp_path: Path) -> None:
    """T250's fail-closed half, far side: the in-game comparison of an in-game-only field is a
    real V2 gate, not a recorded shrug. The client reports `opponents.major_count` as 16 -- the
    exact value `GetAIPlayerCount()` would have fabricated from the phase confusion -- where 5
    was configured: the run fails before turn 1 with the mismatch recorded, and no turn is
    played. (Recording without comparing would pass here; this is the guard against it.)"""
    _write_seed_set(tmp_path / "seedsets")
    config_path = tmp_path / "run.yaml"
    config_values = _write_run_config(config_path, extra_opponents={"major_count": 5})
    config_values["opponents.major_count"] = 16

    saves_dir = FakeHostPlatform().resolve_game_directories(home=tmp_path).saves_dir
    game = _FakeGame(saves_dir=saves_dir, config_values=config_values)
    provider = _build_provider()

    with _ServerThread(FakeNexusServer()) as server:
        _script_game(server, game)
        runner, store = _compose(tmp_path, port=server.port, provider=provider)
        cli.configure_runner_factory(lambda: runner)

        result = CliRunner().invoke(cli.app, ["run", "start", str(config_path)])
        assert result.exit_code == 1, result.output
        run_id = _parse_run_id(result.output)

    run = store.get_run(run_id)
    assert run is not None
    assert run.lifecycle_state is LifecycleState.FAILED
    assert game.end_turns_issued == 0
    assert store.get_turn_cycle(run_id, 1) is None

    mismatches = [
        mismatch
        for event in store.list_run_events(run_id)
        if event.event_type.value == "preparation_mismatch"
        for mismatch in event.detail.get("mismatches", [])
    ]
    major_count_mismatches = [m for m in mismatches if m["field"] == "opponents.major_count"]
    assert major_count_mismatches, f"the mismatch was not recorded; got {mismatches!r}"
    assert major_count_mismatches[0]["expected"] == 5
    assert major_count_mismatches[0]["actual"] == 16


# --------------------------------------------------------------------------
# T243 -- V11's preflight: the estimated footprint must fit in free disk
# before anything is prepared
# --------------------------------------------------------------------------


def test_a_run_whose_estimated_footprint_cannot_fit_is_refused_at_preflight(
    tmp_path: Path,
) -> None:
    """V11 (T243): with free disk above the configured floor (`min_free_disk_gb: 0`) but below
    the run's estimated save/capture footprint, `run start` is refused for free -- before a
    `Run` exists and before the tuner is ever dialled. No server is running on the composed
    port at all, which is the structural proof the refusal comes from the preflight gate: with
    the gate reverted, preparation runs on to `connect()` and fails as a connection error that
    names no V11, and the store-side assertions still hold vacuously -- the output assertion is
    what catches it."""
    _write_seed_set(tmp_path / "seedsets")
    config_path = tmp_path / "run.yaml"
    _write_run_config(config_path)

    # STOP_AT_TURN=2 estimates ~180 MB of saves + captures under saves/headroom.py's documented
    # conservative defaults; 50 MB free clears the 0-GB floor and cannot hold that.
    deps, store = _compose_dependencies(
        tmp_path,
        port=1,  # nothing listens here -- reaching connect() at all would be the defect
        provider=_build_provider(),
        free_disk_bytes=50 * 1024 * 1024,
    )
    runner = Runner(deps)
    cli.configure_runner_factory(lambda: runner)

    result = CliRunner().invoke(cli.app, ["run", "start", str(config_path)])

    assert result.exit_code == 1, result.output
    assert "V11" in result.output, (
        "the refusal must name V11's footprint preflight, not surface later as a transport or "
        f"quicksave failure. Output:\n{result.output}"
    )
    assert "estimated" in result.output

    # Refused for free: no Run was created, nothing was prepared, nothing written anywhere.
    assert store.list_active_runs() == []
    assert _total_run_count(tmp_path / "match-store.db") == 0


def _total_run_count(db_path: Path) -> int:
    """Every run row, straight from SQLite -- `list_active_runs` alone could hide a run that
    was created and immediately failed, and the claim is "nothing was created at all"."""
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        return int(conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0])
    finally:
        conn.close()
