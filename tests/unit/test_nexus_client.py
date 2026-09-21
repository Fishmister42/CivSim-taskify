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
import re
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest

from civsim_harness.errors import NexusError, PreflightError
from civsim_harness.nexus.client import (
    REASON_CONNECTION_CLOSED,
    REASON_GAME_STATES_UNAVAILABLE,
    REASON_HANDSHAKE_FAILED,
    REASON_STALE_STATE_INDEX,
    REASON_TIMEOUT,
    NexusClient,
    StateIndices,
    _parse_state_list,
    _strip_print_prefix,
)
from civsim_harness.nexus.codec import (
    TAG_ASYNC_OUTPUT,
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

    Not ``tests/fakes/fake_nexus.py`` -- this is a small, self-contained
    stand-in local to this module, just enough to drive
    connect()/resolve_game_states() sequencing against a scriptable state
    table.

    Speaks the live-verified handshake framing (2026-09-20 Windows capture,
    specs/002-civ-playing-harness/spikes/r5-raw-windows/
    raw_protocol_transcript.txt): ``APP:`` is *answered* with a
    ``TAG_HANDSHAKE`` identification frame of its own -- the real reply has
    three NUL-separated fields, an odd count that ``_parse_state_list``
    rejects, so a client that fails to consume it cannot connect at all
    (that was live defect 1). ``LSQ:`` is answered from the scripted payload
    list.
    """

    #: The identification payload shape a real client sent back to ``APP:``.
    APP_REPLY = "Civ6\x00Sid Meier's Civilization 6\x00C:\\ScriptedTuner\\Binaries\\Debug"

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
                    if frame.tag == TAG_HANDSHAKE and frame.payload.startswith("APP:"):
                        writer.write(encode_frame(TAG_HANDSHAKE, self.APP_REPLY))
                        await writer.drain()
                    elif frame.tag == TAG_HANDSHAKE and frame.payload == "LSQ:":
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


async def test_a_non_handshake_frame_is_never_parsed_as_the_state_list() -> None:
    """Rewritten with the live-protocol fix.

    The pre-live version of this test asserted the exact opposite behaviour
    -- that a non-``TAG_HANDSHAKE`` frame arriving after ``LSQ:`` raises
    ``REASON_HANDSHAKE_FAILED``. Against a real client that assertion is the
    defect: unsolicited tag -1 log frames interleave into the handshake
    window routinely (live defect 2), so a wrong-tag frame must be
    *skipped*, not fatal. What must survive from the old test is the
    property that such a frame is never parsed as the state list: here the
    server sends only junk on a command tag and closes, so a correct client
    reports the dead connection rather than ever handing back a state table
    built from a non-handshake payload.
    """

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        decoder = NexusFrameDecoder()
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                # Close the server side too: from Python 3.12.1 `Server.wait_closed()`
                # waits for every accepted connection to close, and a handler that returns
                # on EOF without closing its writer leaves this one in CLOSE-WAIT forever
                # (measured: the suite hung here indefinitely on 3.12.3, Linux).
                writer.close()
                return
            for frame in decoder.feed(chunk):
                if frame.tag == TAG_HANDSHAKE and frame.payload.startswith("APP:"):
                    writer.write(encode_frame(TAG_HANDSHAKE, _ScriptedTuner.APP_REPLY))
                    await writer.drain()
                elif frame.tag == TAG_HANDSHAKE and frame.payload == "LSQ:":
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

    assert excinfo.value.detail["reason"] == REASON_CONNECTION_CLOSED
    assert client.state_indices is None


# --------------------------------------------------------------------------
# Live-protocol regressions -- the three defects found driving a real client
# (2026-09-20 Windows session; specs/002-civ-playing-harness/spikes/
# r5-save-path-windows.md "The three protocol defects"). Each test below
# fails against the pre-live implementation of exactly one fix; the framings
# are transcribed from r5-raw-windows/raw_protocol_transcript.txt and
# raw_command_transcript.txt, not invented.
# --------------------------------------------------------------------------

# The APP: reply a real client sent, verbatim from raw_protocol_transcript.txt.
_REAL_APP_REPLY_PAYLOAD = (
    "Civ6\x00Sid Meier's Civilization 6\x00C:\\Program Files (x86)\\Steam\\steamapps"
    "\\common\\Sid Meier's Civilization VI\\Base\\Binaries\\Debug"
)


async def test_live_defect_1_handshake_consumes_the_app_reply_before_the_state_list() -> None:
    """`APP:` is answered, and that reply must be consumed -- not parsed as LSQ's.

    The identification payload has three NUL-separated fields (an odd
    count), so the pre-live client -- which sent `APP:` and then read the
    very next frame as the `LSQ:` reply -- failed every connect with "odd
    number of NUL-separated fields". Uses the verbatim captured payload.
    """

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        decoder = NexusFrameDecoder()
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                # Close the server side too: from Python 3.12.1 `Server.wait_closed()`
                # waits for every accepted connection to close, and a handler that returns
                # on EOF without closing its writer leaves this one in CLOSE-WAIT forever
                # (measured: the suite hung here indefinitely on 3.12.3, Linux).
                writer.close()
                return
            for frame in decoder.feed(chunk):
                if frame.tag == TAG_HANDSHAKE and frame.payload.startswith("APP:"):
                    writer.write(encode_frame(TAG_HANDSHAKE, _REAL_APP_REPLY_PAYLOAD))
                    await writer.drain()
                elif frame.tag == TAG_HANDSHAKE and frame.payload == "LSQ:":
                    writer.write(encode_frame(TAG_HANDSHAKE, _MENU_ONLY_LSQ_PAYLOAD))
                    await writer.drain()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = NexusClient(host="127.0.0.1", port=port, connect_timeout_s=5.0)
    try:
        indices = await client.connect()
    finally:
        await client.close()
        server.close()
        await server.wait_closed()

    assert indices.by_name == {"Main State": 0, "DebugHotloadCache": 1}


async def test_live_defect_2_interleaved_async_frames_do_not_abort_a_state_query() -> None:
    """Unsolicited tag -1 log frames mid-query are telemetry, not a failure.

    Live capture: `O\\0StagingRoom: RefreshStatus()...` interleaved around
    the `LSQ:` reply; the pre-live client read exactly one frame and raised
    "Expected a TAG_HANDSHAKE response to LSQ:, got a different tag" with
    detail tag=-1 on the first real refresh_state_indices().
    """
    captured: list[str] = []
    async with FakeNexusServer() as server:
        server.inject_async_frame_before_next_lsq(
            "RefreshStatus()\tSun Sep 20 15:19:10 2026\tfalse"
        )
        client = NexusClient(
            host="127.0.0.1", port=server.port, on_unmatched_output=captured.append
        )
        try:
            indices = await client.connect()
            assert indices.has_game_states is True

            # And again mid-session: the exact call the live session saw fail.
            server.inject_async_frame_before_next_lsq("CheckPausedState()\t1789932309")
            refreshed = await client.refresh_state_indices()
        finally:
            await client.close()

    assert refreshed.by_name == indices.by_name
    assert captured == [
        "RefreshStatus()\tSun Sep 20 15:19:10 2026\tfalse",
        "CheckPausedState()\t1789932309",
    ]


async def test_live_defect_3_result_arrives_on_tag_minus_1_prints_with_an_empty_tag_3() -> None:
    """A command's result rides tag -1 print frames; tag 3 is an empty ack.

    Framing transcribed from raw_command_transcript.txt: three tag -1
    frames (`O\\0InGame: ---BEGIN:<nonce>---`, the JSON line, the END line),
    then `tag=3 payload=''`. The pre-live client fed only tag-3 payloads to
    the correlator, so every command timed out while its Lua actually ran
    (the "timed-out" Network.LoadGame had loaded the game). This server
    speaks that captured framing byte for byte -- independently of
    tests/fakes/fake_nexus.py, so a change to the shared fake cannot
    silently weaken this regression.
    """
    lsq_payload = "10\x00GameCore_Tuner\x00132\x00InGame"

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        decoder = NexusFrameDecoder()
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                # Close the server side too: from Python 3.12.1 `Server.wait_closed()`
                # waits for every accepted connection to close, and a handler that returns
                # on EOF without closing its writer leaves this one in CLOSE-WAIT forever
                # (measured: the suite hung here indefinitely on 3.12.3, Linux).
                writer.close()
                return
            for frame in decoder.feed(chunk):
                if frame.tag == TAG_HANDSHAKE and frame.payload.startswith("APP:"):
                    writer.write(encode_frame(TAG_HANDSHAKE, _REAL_APP_REPLY_PAYLOAD))
                elif frame.tag == TAG_HANDSHAKE and frame.payload == "LSQ:":
                    writer.write(encode_frame(TAG_HANDSHAKE, lsq_payload))
                elif frame.tag == TAG_COMMAND:
                    begin_match = re.search(r"---BEGIN:([0-9a-f]+)---", frame.payload)
                    assert begin_match is not None
                    nonce = begin_match.group(1)
                    json_line = '{"probe":"tag-hunt","lua_version":"2013.2.0 r13768"}'
                    for line in (
                        f"---BEGIN:{nonce}---",
                        json_line,
                        f"---END:{nonce}---",
                    ):
                        writer.write(
                            encode_frame(TAG_ASYNC_OUTPUT, f"O\x00InGame: {line}")
                        )
                    writer.write(encode_frame(TAG_COMMAND, ""))
                await writer.drain()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = NexusClient(host="127.0.0.1", port=port, command_timeout_s=5.0)
    try:
        indices = await client.connect()
        assert indices.in_game == 132
        result = await client.execute_command(state_index=132, lua_body="print(true)")
    finally:
        await client.close()
        server.close()
        await server.wait_closed()

    assert result == {"probe": "tag-hunt", "lua_version": "2013.2.0 r13768"}


async def test_a_non_empty_tag_3_payload_is_telemetry_not_a_result() -> None:
    """The never-observed pre-live framing must fail loudly, not quietly work.

    If a client ever did deliver a sentinel-bracketed result as a non-empty
    tag-3 payload (the framing the old implementation assumed and no real
    client has exhibited), accepting it silently would mask a protocol
    regression -- the command must time out and the payload must be routed
    to telemetry instead, per the "no silent dual-protocol tolerance" rule.
    """
    captured: list[str] = []
    lsq_payload = "0\x00GameCore_Tuner\x001\x00InGame"

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        decoder = NexusFrameDecoder()
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                # Close the server side too: from Python 3.12.1 `Server.wait_closed()`
                # waits for every accepted connection to close, and a handler that returns
                # on EOF without closing its writer leaves this one in CLOSE-WAIT forever
                # (measured: the suite hung here indefinitely on 3.12.3, Linux).
                writer.close()
                return
            for frame in decoder.feed(chunk):
                if frame.tag == TAG_HANDSHAKE and frame.payload.startswith("APP:"):
                    writer.write(encode_frame(TAG_HANDSHAKE, _REAL_APP_REPLY_PAYLOAD))
                elif frame.tag == TAG_HANDSHAKE and frame.payload == "LSQ:":
                    writer.write(encode_frame(TAG_HANDSHAKE, lsq_payload))
                elif frame.tag == TAG_COMMAND:
                    begin_match = re.search(r"---BEGIN:([0-9a-f]+)---", frame.payload)
                    assert begin_match is not None
                    nonce = begin_match.group(1)
                    writer.write(
                        encode_frame(
                            TAG_COMMAND,
                            f"---BEGIN:{nonce}---\ntrue\n---END:{nonce}---",
                        )
                    )
                await writer.drain()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = NexusClient(
        host="127.0.0.1",
        port=port,
        command_timeout_s=0.3,
        on_unmatched_output=captured.append,
    )
    try:
        await client.connect()
        with pytest.raises(NexusError) as excinfo:
            await client.execute_command(state_index=1, lua_body="print(true)")
    finally:
        await client.close()
        server.close()
        await server.wait_closed()

    assert excinfo.value.detail["reason"] == REASON_TIMEOUT
    assert any("---BEGIN:" in text for text in captured)


def test_strip_print_prefix_removes_the_state_prefix_from_a_print_line() -> None:
    # Verbatim shapes from raw_command_transcript.txt.
    assert _strip_print_prefix("O\x00InGame: ---BEGIN:57a06e82---") == "---BEGIN:57a06e82---"
    assert _strip_print_prefix('O\x00InGame: {"probe":"tag-hunt"}') == '{"probe":"tag-hunt"}'
    assert (
        _strip_print_prefix("O\x00StagingRoom: RefreshStatus()\tfalse")
        == "RefreshStatus()\tfalse"
    )


def test_strip_print_prefix_passes_unprefixed_payloads_through_unchanged() -> None:
    assert _strip_print_prefix("---BEGIN:abc---") == "---BEGIN:abc---"
    assert _strip_print_prefix("") == ""


def test_strip_print_prefix_tolerates_a_marker_with_no_state_separator() -> None:
    # "O\0" marker but no ": " separator -- strip only the marker, keep the rest.
    assert _strip_print_prefix("O\x00Weird") == "Weird"


# --------------------------------------------------------------------------
# T246 -- the post-close connection-refusal tail (live finding, Linux
# 1.0.12.9, 2026-09-20, issue #1). The client refuses new tuner connections
# for a short window (~2s) after the previous one closes; a reconnect() that
# races that tail must ride it out with a bounded retry rather than conclude
# the client is dead. connect() (and a reconnect before this client has ever
# connected) must still fail fast -- a genuinely dead client must not cost the
# retry budget. Each test below fails against the pre-fix reconnect (a single
# connect with no retry) with the connection-refused symptom.
# --------------------------------------------------------------------------


async def test_reconnect_rides_out_the_post_close_connection_refusal_tail() -> None:
    """A reconnect racing the tail retries until the window elapses, then succeeds.

    The fake refuses every connection for a wall-clock window after the previous
    one closes. ``reconnect()`` closes session 1 (arming the tail) and races it;
    the bounded retry survives the window instead of misreading the healthy
    client as dead. Reverting reconnect to a single connect fails this test with
    a refused connection (`PreflightError`/`NexusError`) during the tail.
    """
    async with FakeNexusServer() as server:
        server.refuse_connections_for_after_close(0.3)
        client = NexusClient(
            host="127.0.0.1",
            port=server.port,
            reconnect_backoff_s=0.05,
            reconnect_max_backoff_s=0.05,
            reconnect_max_attempts=60,
        )
        try:
            first = await client.connect()
            assert first.has_game_states is True

            second = await client.reconnect()
        finally:
            await client.close()

    assert second.has_game_states is True
    # The tail actually bit -- at least one attempt inside the window was
    # refused -- and was survived, which is the whole point of the retry.
    assert server.post_close_refusals >= 1


async def test_reconnect_on_a_never_connected_client_fails_fast() -> None:
    """A reconnect before any live connection spends no retry budget.

    There is no previous socket being released, so a refusal is a genuinely
    unreachable client (the "first connect fails fast" rule). The injected sleep
    proves the backoff schedule was never entered.
    """
    probe = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = probe.sockets[0].getsockname()[1]
    probe.close()
    await probe.wait_closed()

    sleeps: list[float] = []

    async def _record_sleep(delay: float) -> None:
        sleeps.append(delay)

    client = NexusClient(
        host="127.0.0.1", port=port, connect_timeout_s=1.0, reconnect_sleep=_record_sleep
    )
    with pytest.raises(PreflightError):
        await client.reconnect()

    assert sleeps == []  # never connected -> no tail -> the retry budget stays untouched


async def test_reconnect_gives_up_after_the_bounded_budget_when_refusals_never_stop() -> None:
    """The tail retry is bounded: a client that stays refused surfaces a failure, not a loop.

    A huge post-close window makes every reconnect attempt refused forever; the
    injected sleep records the backoff schedule without actually pausing. The
    reconnect gives up after exactly ``reconnect_max_attempts`` attempts, having
    slept the doubling-and-capped schedule between them.
    """
    sleeps: list[float] = []

    async def _instant(delay: float) -> None:
        sleeps.append(delay)

    async with FakeNexusServer() as server:
        server.refuse_connections_for_after_close(3600.0)  # effectively forever
        client = NexusClient(
            host="127.0.0.1",
            port=server.port,
            reconnect_max_attempts=4,
            reconnect_backoff_s=0.5,
            reconnect_max_backoff_s=2.0,
            reconnect_sleep=_instant,
        )
        try:
            await client.connect()
            with pytest.raises(PreflightError) as excinfo:
                await client.reconnect()
        finally:
            await client.close()

    assert sleeps == [0.5, 1.0, 2.0]  # three backoffs between four attempts, doubling then capped
    assert excinfo.value.detail["attempts"] == 4


async def test_a_handshake_failure_on_reconnect_is_not_retried() -> None:
    """Only the refusal symptom rides out the tail; a real protocol violation fails at once.

    The server hands back a good handshake first (so the client has connected),
    then a malformed state list on the reconnect. That is a protocol violation,
    not the refusal tail, so it is raised immediately with no retry -- the
    injected sleep is never called.
    """
    sleeps: list[float] = []

    async def _record_sleep(delay: float) -> None:
        sleeps.append(delay)

    good_lsq = "0\x00GameCore_Tuner\x001\x00InGame"
    broken_lsq = "0\x00GameCore_Tuner\x001"  # odd field count -> REASON_HANDSHAKE_FAILED
    state = {"broken": False}

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        decoder = NexusFrameDecoder()
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                # Close the server side too: from Python 3.12.1 `Server.wait_closed()`
                # waits for every accepted connection to close, and a handler that returns
                # on EOF without closing its writer leaves this one in CLOSE-WAIT forever
                # (measured: the suite hung here indefinitely on 3.12.3, Linux).
                writer.close()
                return
            for frame in decoder.feed(chunk):
                if frame.tag == TAG_HANDSHAKE and frame.payload.startswith("APP:"):
                    writer.write(encode_frame(TAG_HANDSHAKE, _ScriptedTuner.APP_REPLY))
                    await writer.drain()
                elif frame.tag == TAG_HANDSHAKE and frame.payload == "LSQ:":
                    payload = broken_lsq if state["broken"] else good_lsq
                    writer.write(encode_frame(TAG_HANDSHAKE, payload))
                    await writer.drain()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = NexusClient(host="127.0.0.1", port=port, reconnect_sleep=_record_sleep)
    try:
        await client.connect()  # good handshake -> the client has now connected once
        state["broken"] = True
        with pytest.raises(NexusError) as excinfo:
            await client.reconnect()
    finally:
        await client.close()
        server.close()
        await server.wait_closed()

    assert excinfo.value.detail["reason"] == REASON_HANDSHAKE_FAILED
    assert sleeps == []  # a protocol violation is not the tail; no retry budget spent


async def test_a_lua_runtime_error_fails_the_command_at_once_and_names_it() -> None:
    """MEASURED (Linux 1.0.12.9, live): a Lua runtime error in a dispatched chunk comes back as a
    NON-empty tag-3 payload -- `ERR:Runtime Error: [string ...]:68: function expected instead of
    nil` plus the stack -- and no sentinel-bracketed result ever follows. Before this the client
    logged it and waited out the full per-operation timeout, so five failing observation bodies
    each cost 30s and were recorded as "timeout" (T213). Now: a `NexusError` at once, carrying the
    error text, reason `lua_error`."""
    import time

    from civsim_harness.nexus.client import REASON_LUA_ERROR

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        decoder = NexusFrameDecoder()
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                writer.close()
                return
            for frame in decoder.feed(chunk):
                if frame.tag == TAG_HANDSHAKE and frame.payload.startswith("APP:"):
                    writer.write(encode_frame(TAG_HANDSHAKE, _REAL_APP_REPLY_PAYLOAD))
                elif frame.tag == TAG_HANDSHAKE and frame.payload == "LSQ:":
                    writer.write(encode_frame(TAG_HANDSHAKE, _MENU_ONLY_LSQ_PAYLOAD))
                elif frame.tag == TAG_COMMAND:
                    writer.write(
                        encode_frame(
                            TAG_COMMAND,
                            'ERR:Runtime Error: [string "print(...)"]:68: function expected '
                            "instead of nil\nstack traceback:\n\t[C]: in function 'x'",
                        )
                    )
                await writer.drain()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = NexusClient(host="127.0.0.1", port=port, command_timeout_s=30.0)
    try:
        await client.connect()
        started = time.perf_counter()
        with pytest.raises(NexusError) as raised:
            await client.execute_command(state_index=0, lua_body="print('never answered')")
        assert time.perf_counter() - started < 5.0  # not the 30s timeout
        assert raised.value.detail["reason"] == REASON_LUA_ERROR
        assert "function expected instead of nil" in str(raised.value)
        assert "stack traceback" in raised.value.detail["lua_error"]
    finally:
        await client.close()
        server.close()
        await server.wait_closed()
