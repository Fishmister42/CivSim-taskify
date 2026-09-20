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
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from civsim_harness.host.detect import (
    HostInfo,
    LinuxSessionType,
    OperatingSystem,
    SupportProbeResult,
)
from civsim_harness.models.common import CapturePath, DeclarationId, ModelRef
from civsim_harness.models.run import LifecycleState, StopResolution
from civsim_harness.nexus.client import NexusClient
from civsim_harness.operator import cli
from civsim_harness.provider.port import ModelCapabilities, RawDecision
from civsim_harness.run.composition import build_runner_dependencies
from civsim_harness.run.runner import Runner
from civsim_harness.store.sqlite_adapter import SqliteMatchStore
from fakes.fake_host import FakeHostPlatform
from fakes.fake_nexus import FakeNexusServer, ReceivedCommand
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

    def read_turn_number(self, _command: ReceivedCommand) -> dict[str, Any]:
        return {
            "turn_number": self.turn,
            "is_local_player_turn": True,
            "is_waiting_for_other_players": False,
        }

    def probe_screen(self, _command: ReceivedCommand) -> dict[str, Any]:
        return {
            "screen": "world_view",
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

    def read_setup(self, _command: ReceivedCommand) -> dict[str, Any]:
        """Answer the single `LuaGameSetupReader` snapshot with the configured setup, keyed the
        way that reader flattens dotted field names."""
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


def _write_run_config(path: Path) -> dict[str, Any]:
    """Write the run configuration and return the flattened setup the fake game must report back.

    Deliberately carries only the configured fields `run.preparation`'s own setup reader has a
    registered getter for: a field with no read path is reported unread and fails V2 closed by
    design, which is a separate behaviour with its own test below rather than something to work
    around here.
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
        "map_settings": {},
        "game_settings": {"game_speed": "GAMESPEED_ONLINE", "starting_era": "ERA_ANCIENT"},
        "difficulty": _DIFFICULTY,
        "opponents": {"city_state_count": 10},
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
        "opponents.city_state_count": 10,
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


def _compose(tmp_path: Path, *, port: int, provider: FakeModelProvider) -> tuple[Runner, Any]:
    """Build a real `Runner` through the real composition root, against fakes."""
    store = SqliteMatchStore(tmp_path / "match-store.db")
    host = FakeHostPlatform()
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
        disk_check_path=tmp_path,
        home=tmp_path,
    )
    return Runner(deps), store


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
