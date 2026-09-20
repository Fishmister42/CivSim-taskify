"""Unit tests for the Nexus client (T031, T032, T159) -- state parsing and sequencing.

Three real-world defects, found via live-Civ-VI-client verification of the
Nexus transport, are covered here:

1. ``_parse_state_list`` guessed "newline-separated state names in positional
   order" for the ``LSQ:`` response payload. Against a real client that guess
   is wrong: the wire format is NUL-separated alternating
   ``<index>\\0<name>\\0`` pairs, and indices must be read from the payload
   itself rather than inferred from list position. The captured bytes below
   are a real transcript, not a synthetic guess -- do not replace them with
   one.
2. ``connect()`` used to require ``GameCore_Tuner`` and ``InGame`` before
   returning, which fails against a real client sitting at the main menu (that
   state table has only ``Main State`` and ``DebugHotloadCache`` -- the
   game-play states appear only once a game is loaded). ``connect()`` now
   resolves whatever states exist and succeeds at the menu;
   ``resolve_game_states()`` is the separate, explicit step the run sequence
   calls once a game is loaded.
3. Indices differ by *game phase*, not only by client version or connection
   (a follow-up 2026-09-20 capture: 31 states at the Create Game screen with
   ``LoadGameMenu``/``SaveGameMenu`` at 18/19, versus 136 states once in game
   with the *same-named* states at 112/113). A cached index from one phase
   can silently be a *valid* index for an unrelated state in another --
   caching indices once per run and never re-resolving would target the
   wrong Lua state with no error at all. See the "phase transition" tests
   below, which use ``tests/fakes/fake_nexus.py``'s ``FakeNexusServer`` and
   its ``set_state_table`` (that fixture now speaks the real NUL-separated
   ``LSQ:`` format, same as this module).

The ``_ScriptedTuner``-based tests below use a small scripted TCP double local
to this module, just enough to drive ``connect()``/``resolve_game_states()``
sequencing against a scriptable state table without pulling in the fuller
``FakeNexusServer`` machinery (command transcripts, stalls, drops) that
defect 3's tests below need instead.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest

from civsim_harness.errors import NexusError, PreflightError
from civsim_harness.nexus.client import (
    REASON_GAME_STATES_UNAVAILABLE,
    REASON_HANDSHAKE_FAILED,
    REASON_STALE_STATE_INDEX,
    NexusClient,
    StateIndices,
    _parse_state_list,
)
from civsim_harness.nexus.codec import (
    TAG_COMMAND,
    TAG_HANDSHAKE,
    NexusFrameDecoder,
    encode_frame,
)
from fakes.fake_nexus import FakeNexusServer

# --------------------------------------------------------------------------
# Defect 1 -- _parse_state_list: real-client capture + robustness
# --------------------------------------------------------------------------

# REAL CLIENT CAPTURE -- not synthetic. Recorded from the first real-world
# verification of the Nexus transport against a live Civilization VI client
# (Linux/Aspyr build) during this fix (2026-09-20). This is ground truth for
# the LSQ: response wire format; if a future change ever "corrects" this back
# toward a newline-separated guess, that change is wrong -- re-verify against
# a real client before touching it.
#
#   0000  21 00 00 00 04 00 00 00 30 00 4d 61 69 6e 20 53  !.......0.Main S
#   0010  74 61 74 65 00 31 00 44 65 62 75 67 48 6f 74 6c  tate.1.DebugHotl
#   0020  6f 61 64 43 61 63 68 65 00                       oadCache.
_REAL_LSQ_RESPONSE_FRAME_BYTES = bytes.fromhex(
    "21 00 00 00 04 00 00 00 30 00 4d 61 69 6e 20 53"
    "74 61 74 65 00 31 00 44 65 62 75 67 48 6f 74 6c"
    "6f 61 64 43 61 63 68 65 00"
)


def test_real_client_capture_lsq_response_decodes_via_the_wire_codec() -> None:
    """The captured bytes decode to the exact payload documented in the fix.

    Exercises the real bytes through the actual frame decoder (not a
    hand-built string) so the fixture also stands as evidence the header
    (length=33, tag=TAG_HANDSHAKE) and NUL-terminator framing match a real
    client, not just the payload content.
    """
    decoder = NexusFrameDecoder()
    frames = decoder.feed(_REAL_LSQ_RESPONSE_FRAME_BYTES)
    assert len(frames) == 1
    frame = frames[0]
    assert frame.tag == TAG_HANDSHAKE
    assert frame.payload == "0\x00Main State\x001\x00DebugHotloadCache"
    # The defining fact of the defect: no newline anywhere in a real payload.
    assert "\n" not in frame.payload


def test_real_client_capture_lsq_response_parses_to_the_menu_state_table() -> None:
    """`_parse_state_list` on the real capture resolves both main-menu states.

    At the main menu the real state table contains only these two states --
    `GameCore_Tuner` / `InGame` are absent until a game is loaded (Defect 2).
    """
    decoder = NexusFrameDecoder()
    (frame,) = decoder.feed(_REAL_LSQ_RESPONSE_FRAME_BYTES)

    states = _parse_state_list(frame.payload)

    assert states == {"Main State": 0, "DebugHotloadCache": 1}


def test_parse_state_list_resolves_indices_from_payload_not_position() -> None:
    """Indices come from the payload's own fields, never from list position.

    A naive `enumerate()` over parsed names would assign 0/1/2 by position
    and get every index wrong here; the real indices are non-contiguous and
    listed out of numeric order.
    """
    payload = "7\x00InGame\x0042\x00Zeta_Debug_Only\x004\x00GameCore_Tuner"

    states = _parse_state_list(payload)

    assert states == {"InGame": 7, "Zeta_Debug_Only": 42, "GameCore_Tuner": 4}


def test_parse_state_list_handles_a_lone_pair() -> None:
    assert _parse_state_list("0\x00Main State") == {"Main State": 0}


def test_parse_state_list_tolerates_a_trailing_nul() -> None:
    # A payload that happens to keep an extra trailing separator (e.g. one
    # more NUL than this fix's examples show) should not be treated as an
    # extra, unpaired field.
    assert _parse_state_list("0\x00Main State\x00") == {"Main State": 0}


def test_parse_state_list_on_empty_payload_returns_no_states() -> None:
    assert _parse_state_list("") == {}


def test_parse_state_list_raises_nexus_error_on_odd_field_count() -> None:
    with pytest.raises(NexusError) as excinfo:
        _parse_state_list("0\x00Main State\x001")  # dangling index with no name

    assert excinfo.value.detail["reason"] == REASON_HANDSHAKE_FAILED


def test_parse_state_list_raises_nexus_error_on_non_integer_index() -> None:
    with pytest.raises(NexusError) as excinfo:
        _parse_state_list("not-a-number\x00Main State")

    assert excinfo.value.detail["reason"] == REASON_HANDSHAKE_FAILED


# --------------------------------------------------------------------------
# StateIndices.has_game_states
# --------------------------------------------------------------------------


def test_state_indices_has_game_states_is_false_when_either_is_missing() -> None:
    assert StateIndices(by_name={"Main State": 0}).has_game_states is False
    assert (
        StateIndices(by_name={"GameCore_Tuner": 0}, game_core_tuner=0).has_game_states is False
    )
    assert StateIndices(by_name={"InGame": 1}, in_game=1).has_game_states is False


def test_state_indices_has_game_states_is_true_when_both_are_resolved() -> None:
    indices = StateIndices(
        by_name={"GameCore_Tuner": 4, "InGame": 7}, game_core_tuner=4, in_game=7
    )
    assert indices.has_game_states is True


# --------------------------------------------------------------------------
# Defect 2 -- connect() succeeds at the menu; resolve_game_states() is separate
# --------------------------------------------------------------------------

_MENU_ONLY_LSQ_PAYLOAD = "0\x00Main State\x001\x00DebugHotloadCache"


class _ScriptedTuner:
    """A minimal in-process TCP double for the Nexus wire protocol.

    Not ``tests/fakes/fake_nexus.py`` (a separate, shared double another
    agent is writing concurrently, still scripted against the pre-fix
    newline LSQ format) -- this is a small, self-contained stand-in local to
    this module, just enough to drive connect()/resolve_game_states()
    sequencing against a scriptable state table.

    Answers only ``LSQ:``; like the real client (and like the concurrent
    fake), it never waits for a reply to ``APP:`` before sending ``LSQ:``,
    so a double that answered ``APP:`` too would make the client misread
    that acknowledgement as the ``LSQ:`` result.
    """

    def __init__(self, lsq_payloads: list[str]) -> None:
        self._lsq_payloads = lsq_payloads
        self._lsq_calls = 0
        self._server: asyncio.AbstractServer | None = None
        self.port = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        decoder = NexusFrameDecoder()
        try:
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    return
                for frame in decoder.feed(chunk):
                    if frame.tag == TAG_HANDSHAKE and frame.payload == "LSQ:":
                        position = min(self._lsq_calls, len(self._lsq_payloads) - 1)
                        response = self._lsq_payloads[position]
                        self._lsq_calls += 1
                        writer.write(encode_frame(TAG_HANDSHAKE, response))
                        await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            return
        finally:
            writer.close()


TunerFactory = Callable[[list[str]], Awaitable[_ScriptedTuner]]


@pytest.fixture
async def scripted_tuner() -> AsyncIterator[TunerFactory]:
    tuners: list[_ScriptedTuner] = []

    async def factory(lsq_payloads: list[str]) -> _ScriptedTuner:
        tuner = _ScriptedTuner(lsq_payloads)
        await tuner.start()
        tuners.append(tuner)
        return tuner

    yield factory

    for tuner in tuners:
        await tuner.stop()


async def test_connect_succeeds_at_the_main_menu_with_no_game_states(
    scripted_tuner: TunerFactory,
) -> None:
    tuner = await scripted_tuner([_MENU_ONLY_LSQ_PAYLOAD])
    client = NexusClient(host="127.0.0.1", port=tuner.port)
    try:
        indices = await client.connect()
    finally:
        await client.close()

    assert indices.by_name == {"Main State": 0, "DebugHotloadCache": 1}
    assert indices.game_core_tuner is None
    assert indices.in_game is None
    assert indices.has_game_states is False
    assert client.state_indices == indices


async def test_resolve_game_states_raises_preflight_error_at_the_main_menu(
    scripted_tuner: TunerFactory,
) -> None:
    tuner = await scripted_tuner([_MENU_ONLY_LSQ_PAYLOAD, _MENU_ONLY_LSQ_PAYLOAD])
    client = NexusClient(host="127.0.0.1", port=tuner.port)
    try:
        await client.connect()
        with pytest.raises(PreflightError) as excinfo:
            await client.resolve_game_states()

        # A failed resolve attempt must not clobber the last-good indices.
        assert client.state_indices is not None
        assert client.state_indices.has_game_states is False
    finally:
        await client.close()

    assert excinfo.value.detail["reason"] == REASON_GAME_STATES_UNAVAILABLE
    assert excinfo.value.detail["missing"] == ["GameCore_Tuner", "InGame"]


async def test_resolve_game_states_succeeds_once_a_game_is_loaded(
    scripted_tuner: TunerFactory,
) -> None:
    # Non-contiguous, out-of-list-order indices once a game is loaded --
    # must resolve by the index embedded in the payload, not by position.
    loaded_payload = (
        "0\x00Main State\x00"
        "1\x00DebugHotloadCache\x00"
        "7\x00InGame\x00"
        "4\x00GameCore_Tuner"
    )
    tuner = await scripted_tuner([_MENU_ONLY_LSQ_PAYLOAD, loaded_payload])
    client = NexusClient(host="127.0.0.1", port=tuner.port)
    try:
        menu_indices = await client.connect()
        assert menu_indices.has_game_states is False

        game_indices = await client.resolve_game_states()
    finally:
        await client.close()

    assert game_indices.game_core_tuner == 4
    assert game_indices.in_game == 7
    assert game_indices.has_game_states is True
    assert client.state_indices == game_indices


async def test_reconnect_never_reuses_pre_disconnect_game_state_indices(
    scripted_tuner: TunerFactory,
) -> None:
    loaded_payload = "4\x00GameCore_Tuner\x007\x00InGame"
    tuner = await scripted_tuner([loaded_payload, _MENU_ONLY_LSQ_PAYLOAD])
    client = NexusClient(host="127.0.0.1", port=tuner.port)
    try:
        first = await client.connect()
        assert first.has_game_states is True

        second = await client.reconnect()
    finally:
        await client.close()

    # The second connection's state table (menu-only) must win outright --
    # the previous connection's GameCore_Tuner=4 / InGame=7 are not carried
    # forward just because they resolved once before.
    assert second.has_game_states is False
    assert second.game_core_tuner is None
    assert second.in_game is None
    assert client.state_indices == second


async def test_resolve_game_states_before_connect_raises_not_connected() -> None:
    client = NexusClient(host="127.0.0.1", port=1)
    with pytest.raises(NexusError):
        await client.resolve_game_states()


# --------------------------------------------------------------------------
# Defect 3 -- indices are invalidated by a phase transition, not only a
# reconnect (2026-09-20 live capture)
# --------------------------------------------------------------------------
#
# Representative subsets of the two real tables, not the full 31/136 states
# -- enough to prove the defining fact: `LoadGameMenu`/`SaveGameMenu` exist
# in *both* phases, under the *same* name, at *different* indices, and
# neither phase's table has `GameCore_Tuner`/`InGame` until a game is loaded.

_CREATE_GAME_SCREEN_STATE_TABLE = {
    "HostGame": 0,
    "MainMenu": 1,
    "StagingRoom": 2,
    "Lobby": 3,
    "Mods": 4,
    "LoadGameMenu": 18,
    "SaveGameMenu": 19,
}

_IN_GAME_STATE_TABLE = {
    "GameCore_Tuner": 0,
    "InGame": 1,
    "LoadGameMenu": 112,
    "SaveGameMenu": 113,
}


async def test_indices_resolved_at_create_game_phase_are_not_reused_after_the_game_loads() -> None:
    async with FakeNexusServer(state_table=_CREATE_GAME_SCREEN_STATE_TABLE) as server:
        client = NexusClient(host="127.0.0.1", port=server.port)
        try:
            setup_indices = await client.connect()
            assert setup_indices.by_name["LoadGameMenu"] == 18
            assert setup_indices.by_name["SaveGameMenu"] == 19
            assert setup_indices.has_game_states is False

            # The game phase transitions -- no reconnect, same client process.
            server.set_state_table(_IN_GAME_STATE_TABLE)
            game_indices = await client.resolve_game_states()
        finally:
            await client.close()

    # The new phase's LoadGameMenu/SaveGameMenu indices win outright -- the
    # setup screen's 18/19 are not carried forward just because they
    # resolved once before, under the same names.
    assert game_indices.by_name["LoadGameMenu"] == 112
    assert game_indices.by_name["SaveGameMenu"] == 113
    assert game_indices.has_game_states is True
    assert client.state_indices == game_indices


async def test_a_stale_index_from_a_prior_phase_is_not_used_silently() -> None:
    async with FakeNexusServer(state_table=_CREATE_GAME_SCREEN_STATE_TABLE) as server:
        client = NexusClient(host="127.0.0.1", port=server.port)
        try:
            setup_indices = await client.connect()
            stale_load_game_menu_index = setup_indices.by_name["LoadGameMenu"]  # 18

            server.set_state_table(_IN_GAME_STATE_TABLE)
            await client.resolve_game_states()  # phase transition observed

            with pytest.raises(NexusError) as excinfo:
                await client.execute_command(
                    state_index=stale_load_game_menu_index, lua_body="print(true)"
                )
        finally:
            await client.close()

    assert excinfo.value.detail["reason"] == REASON_STALE_STATE_INDEX
    assert excinfo.value.detail["state_index"] == 18
    # Not "used silently" in the strongest sense: the command never even
    # reached the wire -- the fake's own receipt log proves it, not just
    # that *some* error came back.
    assert server.received == []


async def test_execute_command_succeeds_once_a_stale_index_is_refreshed() -> None:
    async with FakeNexusServer(
        state_table=_CREATE_GAME_SCREEN_STATE_TABLE, default_response={"ok": True}
    ) as server:
        client = NexusClient(host="127.0.0.1", port=server.port)
        try:
            await client.connect()

            server.set_state_table(_IN_GAME_STATE_TABLE)
            game_indices = await client.resolve_game_states()

            result = await client.execute_command(
                state_index=game_indices.by_name["LoadGameMenu"], lua_body="print(true)"
            )
        finally:
            await client.close()

    assert result == {"ok": True}
    assert len(server.received) == 1
    assert server.received[0].state_index == 112


async def test_refresh_state_indices_adopts_the_new_table_unconditionally() -> None:
    async with FakeNexusServer(state_table=_CREATE_GAME_SCREEN_STATE_TABLE) as server:
        client = NexusClient(host="127.0.0.1", port=server.port)
        try:
            await client.connect()

            # Entering a game -- refresh_state_indices() (not just
            # resolve_game_states()) picks up the new table too.
            server.set_state_table(_IN_GAME_STATE_TABLE)
            entered = await client.refresh_state_indices()
            assert entered.has_game_states is True
            assert entered.by_name["LoadGameMenu"] == 112
            assert client.state_indices == entered

            # Leaving the game again -- refresh_state_indices() does not
            # require GameCore_Tuner/InGame to be present, unlike
            # resolve_game_states(), since a phase boundary can just as
            # easily be leaving the game as entering it.
            server.set_state_table(_CREATE_GAME_SCREEN_STATE_TABLE)
            left = await client.refresh_state_indices()
        finally:
            await client.close()

    assert left.has_game_states is False
    assert left.by_name["LoadGameMenu"] == 18
    assert client.state_indices == left


async def test_refresh_state_indices_before_connect_raises_not_connected() -> None:
    client = NexusClient(host="127.0.0.1", port=1)
    with pytest.raises(NexusError):
        await client.refresh_state_indices()


# --------------------------------------------------------------------------
# Regression coverage for handshake behaviour this fix must not change
# --------------------------------------------------------------------------


async def test_connect_raises_preflight_error_when_connection_is_refused() -> None:
    # Bind an ephemeral port, learn its number, then release it immediately
    # so nothing is listening there when connect() tries it.
    probe = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = probe.sockets[0].getsockname()[1]
    probe.close()
    await probe.wait_closed()

    client = NexusClient(host="127.0.0.1", port=port, connect_timeout_s=1.0)
    with pytest.raises(PreflightError) as excinfo:
        await client.connect()

    assert excinfo.value.detail["port"] == port


async def test_connect_raises_nexus_error_when_lsq_response_has_the_wrong_tag() -> None:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        decoder = NexusFrameDecoder()
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                return
            for frame in decoder.feed(chunk):
                if frame.tag == TAG_HANDSHAKE and frame.payload == "LSQ:":
                    writer.write(encode_frame(TAG_COMMAND, "not a handshake response"))
                    await writer.drain()
                    writer.close()
                    return

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = NexusClient(host="127.0.0.1", port=port)
    try:
        with pytest.raises(NexusError) as excinfo:
            await client.connect()
    finally:
        server.close()
        await server.wait_closed()

    assert excinfo.value.detail["reason"] == REASON_HANDSHAKE_FAILED
