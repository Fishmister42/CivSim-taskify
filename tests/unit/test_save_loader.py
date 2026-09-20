"""T217 -- `saves/load_game.py`'s `LuaSaveLoader`: the production save-load path.

Driven through a **real** `NexusClient` against the recorded-transcript `FakeNexusServer` --
never a hand-rolled client stub -- so the sentinel discipline, the `LSQ:` re-resolution at every
phase boundary, and the reconnect behaviour across the load's own port-closure are all exercised
over the actual wire protocol. The fake's state table is flipped by the scripted game exactly
when a real client's would flip (`Events.ExitToMainMenu` -> the front end; `Network.LoadGame`
accepted -> the game states reappear), matching what the T217 spike measured
(`specs/002-civ-playing-harness/spikes/t217-RESOLVED-frontend-loadgame.md`).

One honesty note about the fake's limits: the server prints the sentinel markers itself, so it
cannot catch a Lua body that ends in a bare `return` (the defect class Phase 9 found in all four
embedded bodies). The `print('{'` assertions below check the bodies' own print discipline
directly instead.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from civsim_harness.errors import HarnessError
from civsim_harness.models.common import RunId, SavePointId
from civsim_harness.models.records import RetentionStatus, SavePoint
from civsim_harness.nexus.client import NexusClient
from civsim_harness.saves.load_game import LuaSaveLoader
from civsim_harness.saves.save_point import save_name_for
from civsim_harness.saves.verify import SAVE_FILE_SUFFIX, resolve_saves_dir
from fakes.fake_host import FakeHostPlatform
from fakes.fake_nexus import (
    STATE_INDEX_IN_GAME,
    STATE_INDEX_MAIN_MENU,
    FakeNexusServer,
    ReceivedCommand,
    front_end_state_table,
    loaded_game_state_table,
)

NOW = datetime(2026, 9, 20, tzinfo=UTC)
RUN_ID = RunId("run-load-t217")
SAVE_TURN = 3


def _save_point(turn: int = SAVE_TURN) -> SavePoint:
    return SavePoint(
        save_point_id=SavePointId("sp-load-t217"),
        run_id=RUN_ID,
        turn_number=turn,
        save_name=save_name_for(RUN_ID, turn),
        taken_at=NOW,
        verified=True,
        lineage={},
        retention_status=RetentionStatus.RETAINED,
        missing=False,
    )


def _loader(client: NexusClient, **overrides: object) -> LuaSaveLoader:
    """Short, test-sized bounds; an honest timeout must not cost a test wall-clock minutes."""
    kwargs: dict[str, object] = {
        "exit_to_menu_timeout_s": 5.0,
        "load_timeout_s": 5.0,
        "verify_timeout_s": 5.0,
        "poll_interval_s": 0.01,
    }
    kwargs.update(overrides)
    return LuaSaveLoader(client, **kwargs)  # type: ignore[arg-type]


class _LoadableGame:
    """The scripted client behind the fake server, doing what the spike measured a real one
    does: the exit flips the phase to the front end, an accepted load flips it back to a loaded
    game (and restores the saved position), and the far-side read-back reports that position."""

    def __init__(
        self,
        server: FakeNexusServer,
        *,
        accept_load: bool = True,
        turn_after_load: int = SAVE_TURN,
        in_front_end_after_load: bool = False,
        local_player: int = 0,
    ) -> None:
        self.server = server
        self.accept_load = accept_load
        self.turn_after_load = turn_after_load
        self.in_front_end_after_load = in_front_end_after_load
        self.local_player = local_player

    def exit_to_menu(self, _command: ReceivedCommand) -> dict[str, object]:
        self.server.set_state_table(front_end_state_table())
        return {"issued": True, "error": ""}

    def load_game(self, _command: ReceivedCommand) -> dict[str, object]:
        if not self.accept_load:
            return {"issued": True, "accepted": False, "error": ""}
        self.server.set_state_table(loaded_game_state_table())
        return {"issued": True, "accepted": True, "error": ""}

    def verify(self, _command: ReceivedCommand) -> dict[str, object]:
        return {
            "issued": True,
            "error": "",
            "turn": self.turn_after_load,
            "local_player": self.local_player,
            "in_front_end": self.in_front_end_after_load,
        }


def _script(server: FakeNexusServer, game: _LoadableGame) -> None:
    """Each entry pinned to the Lua state it must arrive in: an exit sent anywhere but InGame,
    a load anywhere but MainMenu, or a read-back anywhere but InGame falls through to the
    fake's generic default response -- which the loader correctly refuses -- so mis-targeted
    state indices fail these tests rather than passing silently."""
    server.queue_response(
        game.exit_to_menu,
        match="Events.ExitToMainMenu",
        state_index=STATE_INDEX_IN_GAME,
        repeatable=True,
    )
    server.queue_response(
        game.load_game,
        match="Network.LoadGame",
        state_index=STATE_INDEX_MAIN_MENU,
        repeatable=True,
    )
    server.queue_response(
        game.verify,
        match="UI.IsInFrontEnd",
        state_index=STATE_INDEX_IN_GAME,
        repeatable=True,
    )


def _bodies(server: FakeNexusServer, match: str) -> list[str]:
    return [cmd.lua_body for cmd in server.received if match in cmd.lua_body]


# --------------------------------------------------------------------------
# The happy path, from both starting phases
# --------------------------------------------------------------------------


async def test_load_from_in_game_exits_to_menu_loads_and_verifies_far_side(
    tmp_path: Path,
) -> None:
    """The full verified sequence: InGame -> Events.ExitToMainMenu() -> FrontEnd ->
    Network.LoadGame (verified table shape) -> game states reappear -> far-side position
    read-back matches the save. The filesystem pre-check runs too: the save's file exists."""
    save = _save_point()
    host = FakeHostPlatform()
    saves_dir = resolve_saves_dir(host, home=tmp_path)
    saves_dir.mkdir(parents=True, exist_ok=True)
    (saves_dir / f"{save.save_name}{SAVE_FILE_SUFFIX}").write_bytes(b"fake-save")

    async with FakeNexusServer(state_table=loaded_game_state_table()) as server:
        game = _LoadableGame(server)
        _script(server, game)
        client = NexusClient(host="127.0.0.1", port=server.port)
        try:
            await client.connect()
            await _loader(client, host=host, home=tmp_path).load(save)

            # The loader leaves the client on fresh, post-load indices (the phase boundary rule).
            indices = client.state_indices
            assert indices is not None and indices.has_game_states
        finally:
            await client.close()

    # -- the sequence, in order, each step in its own required Lua state ----------------------
    exit_cmds = [c for c in server.received if "Events.ExitToMainMenu" in c.lua_body]
    load_cmds = [c for c in server.received if "Network.LoadGame" in c.lua_body]
    verify_cmds = [c for c in server.received if "UI.IsInFrontEnd" in c.lua_body]
    assert len(exit_cmds) == 1 and exit_cmds[0].state_index == STATE_INDEX_IN_GAME
    assert len(load_cmds) == 1 and load_cmds[0].state_index == STATE_INDEX_MAIN_MENU
    assert len(verify_cmds) == 1 and verify_cmds[0].state_index == STATE_INDEX_IN_GAME
    assert exit_cmds[0].index < load_cmds[0].index

    # -- the exact call shape the spike verified ----------------------------------------------
    load_body = load_cmds[0].lua_body
    assert f'loadGame.Name = "{save.save_name}"' in load_body
    assert "loadGame.Location = SaveLocations.LOCAL_STORAGE" in load_body
    assert "loadGame.Type = SaveTypes.SINGLE_PLAYER" in load_body
    assert "loadGame.Directory = SaveDirectories.DEFAULT" in load_body
    assert "loadGame.IsAutosave = false" in load_body
    assert "loadGame.IsQuicksave = false" in load_body
    assert "Network.LoadGame(loadGame, ServerType.SERVER_TYPE_NONE)" in load_body

    # -- every embedded body PRINTS its JSON result; the tuner discards a bare `return` -------
    for body in (exit_cmds[0].lua_body, load_body, verify_cmds[0].lua_body):
        assert "print('{'" in body, f"body does not print its JSON result: {body[:120]}..."


async def test_load_from_the_front_end_skips_the_exit_step() -> None:
    """Already at the front end (MainMenu present, no game states): `Events.ExitToMainMenu()`
    must not be issued -- Firaxis's own guard is `if not UI.IsInFrontEnd()`, not an
    unconditional exit."""
    save = _save_point()
    async with FakeNexusServer(state_table=front_end_state_table()) as server:
        game = _LoadableGame(server)
        _script(server, game)
        client = NexusClient(host="127.0.0.1", port=server.port)
        try:
            await client.connect()
            await _loader(client).load(save)
        finally:
            await client.close()

    assert _bodies(server, "Events.ExitToMainMenu") == []
    assert len(_bodies(server, "Network.LoadGame")) == 1
    assert len(_bodies(server, "UI.IsInFrontEnd")) == 1


# --------------------------------------------------------------------------
# The client refusing the load
# --------------------------------------------------------------------------


async def test_a_delivered_false_from_network_loadgame_fails_loudly_and_by_name() -> None:
    """`ok=true ret=false` is the client refusing the call (the shape three spike rounds
    misread as 'bad argument'): the loader must fail immediately, naming the save and both
    readings of `false`, and must never proceed to a far-side verification of a load that was
    never accepted."""
    save = _save_point()
    async with FakeNexusServer(state_table=front_end_state_table()) as server:
        game = _LoadableGame(server, accept_load=False)
        _script(server, game)
        client = NexusClient(host="127.0.0.1", port=server.port)
        try:
            await client.connect()
            with pytest.raises(HarnessError) as excinfo:
                await _loader(client).load(save)
        finally:
            await client.close()

    assert "Network.LoadGame returned false" in excinfo.value.message
    assert save.save_name in excinfo.value.message
    assert excinfo.value.detail["save_name"] == save.save_name
    assert _bodies(server, "UI.IsInFrontEnd") == [], (
        "a refused load must not be followed by a far-side verification"
    )


# --------------------------------------------------------------------------
# The far-side assertion: position mismatches fail the operation (Principle IV)
# --------------------------------------------------------------------------


async def test_a_load_landing_on_the_wrong_turn_fails_the_operation() -> None:
    """The call returned true, the phase transitioned -- and the client sits at turn 7 where
    the save recorded turn 3. Principle IV: anywhere but the named save's exact position fails
    the operation, with expected and seen both named."""
    save = _save_point(turn=SAVE_TURN)
    async with FakeNexusServer(state_table=front_end_state_table()) as server:
        game = _LoadableGame(server, turn_after_load=7)
        _script(server, game)
        client = NexusClient(host="127.0.0.1", port=server.port)
        try:
            await client.connect()
            with pytest.raises(HarnessError) as excinfo:
                await _loader(client).load(save)
        finally:
            await client.close()

    assert "wrong position" in excinfo.value.message
    assert f"turn {SAVE_TURN}" in excinfo.value.message
    assert "7" in excinfo.value.message
    assert excinfo.value.detail["expected_turn"] == SAVE_TURN
    assert excinfo.value.detail["observed"]["turn"] == 7


async def test_a_far_side_still_reporting_the_front_end_fails_the_operation() -> None:
    """`UI.IsInFrontEnd()` still true after the load is the definitive 'no game loaded' -- it
    must fail regardless of what any other field claims."""
    save = _save_point()
    async with FakeNexusServer(state_table=front_end_state_table()) as server:
        game = _LoadableGame(server, in_front_end_after_load=True)
        _script(server, game)
        client = NexusClient(host="127.0.0.1", port=server.port)
        try:
            await client.connect()
            with pytest.raises(HarnessError) as excinfo:
                await _loader(client).load(save)
        finally:
            await client.close()

    assert "UI.IsInFrontEnd()" in excinfo.value.message
    assert "expected false" in excinfo.value.message


# --------------------------------------------------------------------------
# Honest timeouts: what was awaited, for how long, and what was last seen
# --------------------------------------------------------------------------


async def test_an_exit_that_never_reaches_the_front_end_times_out_honestly() -> None:
    """The exit call reports success but the phase never changes: the loader must not trust the
    call's own return (the far-side rule) -- it times out naming what it was waiting for and
    the state table it last saw."""
    save = _save_point()
    async with FakeNexusServer(state_table=loaded_game_state_table()) as server:
        # The exit is answered "issued" but the state table is never flipped.
        server.queue_response(
            {"issued": True, "error": ""}, match="Events.ExitToMainMenu", repeatable=True
        )
        client = NexusClient(host="127.0.0.1", port=server.port)
        try:
            await client.connect()
            with pytest.raises(HarnessError) as excinfo:
                await _loader(client, exit_to_menu_timeout_s=0.3).load(save)
        finally:
            await client.close()

    assert "timed out" in excinfo.value.message
    assert "Events.ExitToMainMenu()" in excinfo.value.message
    assert "InGame" in excinfo.value.detail["last_state_table"]


async def test_an_unreachable_tuner_fails_with_a_specific_message_not_a_generic_one() -> None:
    """Honest degradation: no tuner listening means the load fails naming the unreachable tuner
    and the refused connection -- never a bare 'load failed'."""
    server = FakeNexusServer()
    await server.start()
    port = server.port
    await server.stop()  # nothing is listening on `port` any more

    client = NexusClient(host="127.0.0.1", port=port)
    with pytest.raises(HarnessError) as excinfo:
        await _loader(
            client, exit_to_menu_timeout_s=0.3
        ).load(_save_point())

    assert "timed out" in excinfo.value.message
    assert "reachable tuner" in excinfo.value.message
    assert "PreflightError" in str(excinfo.value.detail["last_transport_error"])


# --------------------------------------------------------------------------
# The port closing during the load is the expected shape, not a crash
# --------------------------------------------------------------------------


async def test_the_tuner_port_closing_during_the_load_is_survived_by_reconnecting() -> None:
    """The spike's measured shape: `Network.LoadGame` answers true, then the tuner port goes
    away for the duration of the load and refuses connections until the game is back up. The
    loader must treat that window as expected, keep retrying, and complete the far-side
    verification on the fresh connection that finally succeeds."""
    save = _save_point()
    async with FakeNexusServer(state_table=loaded_game_state_table()) as server:
        game = _LoadableGame(server)

        def _load_and_close_port(command: ReceivedCommand) -> dict[str, object]:
            # The response below is still delivered (drop_connection_after fires *after* the
            # write); the next two reconnect attempts then find the port "closed".
            server.refuse_next_connections(2)
            return game.load_game(command)

        server.queue_response(
            game.exit_to_menu,
            match="Events.ExitToMainMenu",
            state_index=STATE_INDEX_IN_GAME,
            repeatable=True,
        )
        server.queue_response(
            _load_and_close_port,
            match="Network.LoadGame",
            state_index=STATE_INDEX_MAIN_MENU,
            repeatable=True,
        )
        server.queue_response(
            game.verify,
            match="UI.IsInFrontEnd",
            state_index=STATE_INDEX_IN_GAME,
            repeatable=True,
        )
        # Connection 1 carries: exit (command 1), load (command 2) -- the port drops right
        # after the load's own response is delivered.
        server.drop_connection_after(2)

        client = NexusClient(host="127.0.0.1", port=server.port)
        try:
            await client.connect()
            await _loader(client).load(save)
        finally:
            await client.close()

    assert server.connection_count >= 2, (
        "the loader never reconnected; the port-closure window was not survived"
    )
    assert len(_bodies(server, "UI.IsInFrontEnd")) == 1


# --------------------------------------------------------------------------
# The filesystem pre-check: FileNotFoundError is recovery's `save_missing` signal
# --------------------------------------------------------------------------


async def test_a_save_whose_file_is_gone_raises_file_not_found_before_touching_the_client(
    tmp_path: Path,
) -> None:
    """T172/FR-036: the `.Civ6Save` is absent from the resolved save directory -- the loader
    raises `FileNotFoundError` (the one shape `RecoveryEngine` maps to a durable `save_missing`
    and an outright, never-retried failure) without a single command reaching the client."""
    save = _save_point()
    host = FakeHostPlatform()

    async with FakeNexusServer(state_table=loaded_game_state_table()) as server:
        client = NexusClient(host="127.0.0.1", port=server.port)
        try:
            await client.connect()
            with pytest.raises(FileNotFoundError) as excinfo:
                await _loader(client, host=host, home=tmp_path).load(save)
        finally:
            await client.close()

    assert save.save_name in str(excinfo.value)
    assert str(resolve_saves_dir(host, home=tmp_path)) in str(excinfo.value)
    assert server.received == [], "the client was touched despite the file being gone"
