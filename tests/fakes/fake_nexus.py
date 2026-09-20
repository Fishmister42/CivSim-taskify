"""Recorded-transcript fake Nexus server (T056, research R15).

Replays scripted request/response pairs over the *real* Firaxis Nexus wire
protocol (`civsim_harness.nexus.codec`, `civsim_harness.nexus.sentinels`),
so the full turn cycle, recovery paths, and record shapes are testable
without a running Civilization VI client, and so an unmodified
`civsim_harness.nexus.client.NexusClient` can connect to, handshake with,
and exchange commands against this server exactly as it would the real
game.

**What this fake can do (contracts/nexus-protocol.md, research R15):**

- A normal request/response exchange, matched against a scripted
  `TranscriptEntry` list (content-addressed; see `TranscriptEntry`).
- Drop the connection mid-turn at an arbitrary decision step
  (`drop_connection_at`), simulating a crash.
- Stall on one specific operation -- by ordinal (`stall_at`) or by content
  (`stall_on`) -- while continuing to answer every other command normally,
  including a heartbeat probe issued afterward. This is the case research
  R15 and the wire contract's "Heartbeat nonce fails to round-trip" row
  name explicitly: a client that is alive but not servicing one operation.
- Emit stray, sentinel-less output ahead of a real response
  (`inject_stray_text`), proving a real `NexusClient` discards it to
  telemetry instead of returning it as a result (contract: "Output arrives
  with no matching nonce -> Discarded to telemetry; never returned as a
  result").
- Present either Lua state table a real client can see (`set_state_table`):
  a "main menu" table with no game-play states, or a "game loaded" table
  with `GameCore_Tuner`/`InGame` at any (including non-contiguous or
  reordered) indices -- see "State table" below.

**State table (`LSQ:`).** Wire format verified against a real client (see
`civsim_harness.nexus.client._parse_state_list` and its real-capture
fixture in `tests/unit/test_nexus_client.py`, fixed concurrently by another
agent working that module): NUL-separated, alternating
``<index>\0<name>\0`` pairs -- **not** newline-separated, and indices are
read from each pair's own field, never inferred from list position. This
fake defaults to `loaded_game_state_table()` (a game already loaded, which
is what the turn cycle needs) but can present `menu_only_state_table()` --
matching a real client's actual main-menu table, which has no
`GameCore_Tuner`/`InGame` at all -- or any custom mapping, via
`set_state_table`. Every `LSQ:` query (the initial handshake's, and any
later one `NexusClient.resolve_game_states()` sends) is answered from
whatever table is current at that moment, so a test can script "starts at
the menu, then a game loads mid-session" by calling `set_state_table`
between two client calls.

The server also accepts only one connection at a time, mirroring the real
client's own limit (contracts/nexus-protocol.md: "The game accepts one
tuner connection at a time").
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from civsim_harness.nexus.codec import (
    TAG_COMMAND,
    TAG_HANDSHAKE,
    NexusFrame,
    NexusFrameDecoder,
    encode_frame,
)
from civsim_harness.nexus.sentinels import begin_marker, end_marker

# Default Lua state indices for `loaded_game_state_table()` -- also what
# `SYNTHETIC_TURN_TRANSCRIPT` below addresses via `TranscriptEntry.state_index`.
STATE_INDEX_GAME_CORE_TUNER = 0
STATE_INDEX_IN_GAME = 1

_CMD_PREFIX = "CMD:"
_BEGIN_LINE_RE = re.compile(r'print\("---BEGIN:(.+?)---"\)')


# --------------------------------------------------------------------------
# Lua state tables (`LSQ:` responses)
# --------------------------------------------------------------------------


def menu_only_state_table() -> dict[str, int]:
    """No game loaded: the two states a real client actually reports at the
    main menu -- `GameCore_Tuner`/`InGame` are absent (verified against a
    real client; see `tests/unit/test_nexus_client.py`). A client's
    `connect()` still succeeds against this table; `resolve_game_states()`
    must raise `PreflightError` naming both as missing."""
    return {"Main State": 0, "DebugHotloadCache": 1}


def loaded_game_state_table(
    *,
    game_core_tuner: int = STATE_INDEX_GAME_CORE_TUNER,
    in_game: int = STATE_INDEX_IN_GAME,
) -> dict[str, int]:
    """A game loaded: both required states present, at the given (by default
    contiguous) indices. Pass non-default, non-contiguous, or swapped values
    to prove index resolution reads each pair's own field rather than
    inferring it from position."""
    return {"GameCore_Tuner": game_core_tuner, "InGame": in_game}


# The `MainMenu` index a real front-end table reported in the T217 spike
# (`specs/002-civ-playing-harness/spikes/t217-RESOLVED-frontend-loadgame.md`:
# the enums are "present in LoadGameMenu (18), MainMenu (24) and SaveGameMenu
# (19)"). Deliberately non-contiguous with the other entries, for the same
# reason `loaded_game_state_table` accepts arbitrary indices.
STATE_INDEX_MAIN_MENU = 24


def front_end_state_table(*, main_menu: int = STATE_INDEX_MAIN_MENU) -> dict[str, int]:
    """The FrontEnd phase, as the T217 load spike measured it: `MainMenu`
    present (the state verified to carry both `Network.LoadGame` and the
    `SaveLocations`/`SaveTypes`/`SaveDirectories`/`ServerType` enums), and no
    game-play states at all -- `GameCore_Tuner`/`InGame` do not exist until a
    game is loaded. This is the table a save *load* must be issued from;
    `menu_only_state_table()` remains the sparser two-state capture from an
    earlier client observation and deliberately has no `MainMenu`, so a
    loader polling for the front end must keep waiting on it."""
    return {"Main State": 0, "DebugHotloadCache": 1, "MainMenu": main_menu}


def _encode_state_table(state_table: Mapping[str, int]) -> str:
    """Encode a `{name: index}` mapping as the real wire's NUL-separated,
    alternating `<index>\\0<name>\\0` payload -- the exact inverse of
    `civsim_harness.nexus.client._parse_state_list`."""
    parts: list[str] = []
    for name, index in state_table.items():
        parts.append(str(index))
        parts.append(name)
    return "\x00".join(parts)


# --------------------------------------------------------------------------
# Transcript
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TranscriptEntry:
    """One recorded request/response pair to replay (research R15).

    `match` is a substring that must appear in the incoming command's Lua
    body (the declared body a catalog entry dispatches -- the sentinel
    wrapper is stripped before matching) for this entry to apply. An empty
    string matches anything, which makes plain ordered replay ("the next
    request gets the next recorded response") a degenerate case of content
    matching rather than a separate code path. `state_index`, if given,
    additionally requires the request's own state index to match
    (`STATE_INDEX_GAME_CORE_TUNER` / `STATE_INDEX_IN_GAME`).

    Entries are consumed at most once, checked in the order the transcript
    lists them, from among those not yet consumed -- so two entries with
    the same `match` (e.g. the same read repeated before and after an
    action) serve consecutive calls in order rather than one being reused.

    `repeatable` opts one entry out of that consumption: it stays in the
    transcript and answers every matching command for the rest of the
    session. A fixed-length transcript is the right shape for a scripted
    scenario of known length, but not for a run whose *length is the thing
    under test* (an end-to-end run plays as many turns as its stop condition
    takes, issuing the same handful of reads each turn) -- enumerating one
    entry per expected command there would encode the very sequence the test
    is supposed to discover.

    `response` may also be a **callable** taking this command's
    `ReceivedCommand` and returning the value to answer with. That is what
    lets a fake stand in for a *stateful* game rather than a fixed recording:
    the turn counter a run reads has to actually advance when the run ends a
    turn, or `turn.end_turn`'s own verification predicate (`game.turn_number
    == observed_turn_number + 1`) could never be satisfied by any static
    transcript. Side effects belong here too -- a real client writes a save
    file when `Network.SaveGame` is dispatched, and a fake that answers
    "issued" without writing one would fail the filesystem verification
    `run/turn_cycle.py` does immediately afterward.
    """

    match: str
    response: Any
    state_index: int | None = None
    repeatable: bool = False


@dataclass(frozen=True)
class ReceivedCommand:
    """One command this fake actually received, for asserting on what a
    caller under test really sent -- not just what the fixture claims."""

    index: int  # 1-based ordinal among all commands this connection has received
    state_index: int
    lua_body: str
    nonce: str


# --------------------------------------------------------------------------
# SYNTHETIC_TURN_TRANSCRIPT -- a small, honest fixture
# --------------------------------------------------------------------------
#
# SYNTHETIC. This repo has no live Nexus capture pipeline yet, so nothing
# below was recorded from a running Civilization VI client. It is
# hand-authored to be *shaped like* real traffic: the Lua dispatch
# expressions mirror lua/ingame/turn_control.lua's and lua/ingame/
# screens.lua's actual `CivSim_*` dispatch tables, and the JSON shapes
# mirror catalogs/actions/turn.yaml's `turn.end_turn` and
# catalogs/observations/game.yaml's `game.turn_state` / `game.screen_state`
# `output_schema`s. A later wave, once a genuine capture exists (research
# R15), replaces this fixture wholesale. Do not treat it as evidence of
# real game behaviour.

SYNTHETIC_TURN_TRANSCRIPT: list[TranscriptEntry] = [
    TranscriptEntry(
        state_index=STATE_INDEX_GAME_CORE_TUNER,
        match="CivSim_TurnControl.read_turn_number",
        response={
            "turn_number": 1,
            "is_local_player_turn": True,
            "is_waiting_for_other_players": False,
        },
    ),
    TranscriptEntry(
        state_index=STATE_INDEX_GAME_CORE_TUNER,
        match="CivSim_Screens.probe",
        response={
            "screen": "world_view",
            "raw_screen_id": "WorldView",
            "recognized": True,
            "has_blocking_prompt": False,
            "prompt_options": [],
        },
    ),
    TranscriptEntry(
        state_index=STATE_INDEX_IN_GAME,
        match="CivSim_TurnControl.end_turn",
        response={"ok": True, "path": "Game.EndTurn"},
    ),
    TranscriptEntry(
        state_index=STATE_INDEX_GAME_CORE_TUNER,
        match="CivSim_TurnControl.read_turn_number",
        response={
            "turn_number": 2,
            "is_local_player_turn": True,
            "is_waiting_for_other_players": False,
        },
    ),
]


# --------------------------------------------------------------------------
# Wire parsing helpers (server-side inverse of nexus.client's construction)
# --------------------------------------------------------------------------


def _parse_command(payload: str) -> tuple[int, str, str]:
    """Parse a `CMD:<state_index>:<wrapped_lua>` payload into `(state_index, nonce, lua_body)`.

    The exact inverse of `civsim_harness.nexus.client.NexusClient.
    execute_command`'s own construction (`f"CMD:{state_index}:
    {wrap_lua(nonce, lua_body)}"`) -- kept private to this fake since no
    other module needs to parse a command payload from the server side.
    """
    if not payload.startswith(_CMD_PREFIX):
        raise ValueError(f"fake nexus: expected a CMD: payload, got {payload!r}")
    rest = payload[len(_CMD_PREFIX) :]
    state_index_str, _, wrapped = rest.partition(":")
    lines = wrapped.split("\n")
    begin_match = _BEGIN_LINE_RE.match(lines[0]) if lines else None
    nonce = begin_match.group(1) if begin_match else ""
    lua_body = "\n".join(lines[1:-1]) if len(lines) > 2 else ""
    return int(state_index_str), nonce, lua_body


class _FrameReader:
    """Incremental per-connection frame reader, mirroring `NexusClient`'s own
    `_read_frame`: `NexusFrameDecoder.feed` can hand back more than one
    complete frame per socket read, so completed frames are queued rather
    than dropped."""

    def __init__(self, reader: asyncio.StreamReader) -> None:
        self._reader = reader
        self._decoder = NexusFrameDecoder()
        self._pending: list[NexusFrame] = []

    async def read_one(self) -> NexusFrame | None:
        while not self._pending:
            chunk = await self._reader.read(4096)
            if not chunk:
                return None  # peer closed the connection
            self._pending.extend(self._decoder.feed(chunk))
        return self._pending.pop(0)


# --------------------------------------------------------------------------
# The server
# --------------------------------------------------------------------------


class FakeNexusServer:
    """Recorded-transcript fake Nexus server (T056, research R15).

    See the module docstring for the scenarios this fake is built to drive.
    Usage::

        async with FakeNexusServer(SYNTHETIC_TURN_TRANSCRIPT) as server:
            client = NexusClient(host="127.0.0.1", port=server.port)
            indices = await client.connect()
            ...
            await client.close()
    """

    def __init__(
        self,
        transcript: Sequence[TranscriptEntry] = (),
        *,
        host: str = "127.0.0.1",
        default_response: Any = None,
        state_table: Mapping[str, int] | None = None,
    ) -> None:
        self._transcript: deque[TranscriptEntry] = deque(transcript)
        self._host = host
        self._default_response: Any = default_response if default_response is not None else {
            "ok": True
        }
        self._state_table: dict[str, int] = (
            dict(state_table) if state_table is not None else loaded_game_state_table()
        )

        self._server: asyncio.Server | None = None
        self._session_active = False

        self._drop_at: int | None = None
        self._drop_after: int | None = None
        self._refuse_connections = 0
        self._stall_at: set[int] = set()
        self._stall_matches: list[str] = []
        self._stray_before: dict[int, list[str]] = {}

        # Audit trail: what this fake actually saw and did, independent of
        # the transcript it was handed.
        self.received: list[ReceivedCommand] = []
        self.unmatched_requests: list[ReceivedCommand] = []
        self.app_names: list[str] = []
        self.connection_count = 0

    @property
    def port(self) -> int:
        if self._server is None or not self._server.sockets:
            raise RuntimeError("FakeNexusServer has not been started (call start() first)")
        return int(self._server.sockets[0].getsockname()[1])

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_connection, host=self._host, port=0
        )

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def close_now(self) -> None:
        """Stop listening without awaiting in-flight connection handlers.

        `stop()` awaits `wait_closed()`, which does not return while a client is still connected
        -- fine for a test that owns both ends, but a caller driving a *run* does not: nothing in
        the harness closes its `NexusClient` when a run finishes, so the handler stays live and a
        graceful stop would block forever. Synchronous so it can be handed straight to
        `loop.call_soon_threadsafe` during teardown.
        """
        if self._server is not None:
            self._server.close()
            self._server = None

    async def __aenter__(self) -> FakeNexusServer:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()

    # -- scripting: drops, stalls, stray output -------------------------------

    def drop_connection_at(self, command_index: int) -> None:
        """Simulate a mid-turn crash: close the connection the instant the
        `command_index`-th command (1-based) arrives, without ever
        responding to it. Every command before it is served normally."""
        self._drop_at = command_index

    def clear_drop(self) -> None:
        self._drop_at = None
        self._drop_after = None

    def drop_connection_after(self, command_index: int) -> None:
        """Close the connection immediately *after* responding to the
        `command_index`-th command (1-based). This is the shape the T217 load
        spike measured for `Network.LoadGame`: the call's own response (`true`)
        is delivered, and then the tuner port goes away for the duration of
        the load -- distinct from `drop_connection_at`, which eats the command
        without ever answering it (a crash mid-operation)."""
        self._drop_after = command_index

    def refuse_next_connections(self, count: int) -> None:
        """Close the next `count` incoming connections during their handshake,
        before any session is established -- simulating the window the T217
        spike measured while a load is in flight, when the tuner port refuses
        every connection until the game is back up. Connections after the
        `count`-th proceed normally, which is the 'rebound' half of that same
        measurement."""
        self._refuse_connections = count

    def stall_at(self, command_index: int) -> None:
        """Never respond to the `command_index`-th command (1-based); every
        other command, before or after -- including on the same connection
        -- is served normally."""
        self._stall_at.add(command_index)

    def stall_on(self, match: str) -> None:
        """Never respond to any command whose Lua body contains `match`,
        regardless of position -- the content-addressed sibling of
        `stall_at`, for a scenario that names *which operation* hangs
        (e.g. a specific capability's dispatch call) rather than an
        ordinal that later changes if an unrelated call is added earlier
        in the sequence. Combined with a distinct heartbeat-shaped
        transcript entry that is *not* stalled, this reproduces "client
        alive but not servicing an operation" (research R15)."""
        self._stall_matches.append(match)

    def clear_stalls(self) -> None:
        self._stall_at.clear()
        self._stall_matches.clear()

    def inject_stray_text(self, before_command_index: int, text: str) -> None:
        """Schedule `text` to be written on the wire, outside any BEGIN/END
        sentinel pair, immediately before the response to the
        `before_command_index`-th command (1-based). `text` is written
        verbatim -- pass something with no sentinel of its own (stray
        engine prints) or one carrying a stale/foreign nonce, matching the
        two "unmatched output" shapes the wire contract names."""
        self._stray_before.setdefault(before_command_index, []).append(text)

    def queue_response(
        self,
        response: Any,
        *,
        match: str = "",
        state_index: int | None = None,
        repeatable: bool = False,
    ) -> None:
        """Append one more transcript entry on top of whatever the
        constructor was given -- for scripting the next call or two inline
        in a test rather than building a whole transcript up front. See
        `TranscriptEntry` for `repeatable` and for answering with a callable
        rather than a fixed value."""
        self._transcript.append(
            TranscriptEntry(
                match=match,
                response=response,
                state_index=state_index,
                repeatable=repeatable,
            )
        )

    def set_state_table(self, state_table: Mapping[str, int]) -> None:
        """Script what every subsequent `LSQ:` query reports.

        Takes effect immediately, including for a query already in flight
        on an open connection -- `NexusClient.resolve_game_states()` sends
        a fresh `LSQ:` on the same connection, so calling this between two
        client calls scripts "starts at the menu, then a game loads
        mid-session" without a reconnect. Use `menu_only_state_table()` /
        `loaded_game_state_table()` for the two shapes verified against a
        real client, or any custom mapping (non-contiguous/reordered
        indices included).
        """
        self._state_table = dict(state_table)

    # -- connection handling ---------------------------------------------------

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if self._refuse_connections > 0:
            # See `refuse_next_connections`: the tuner port is "closed" while a
            # load is in flight. Closing during the handshake is how a fake TCP
            # listener that cannot un-listen models a refused port.
            self._refuse_connections -= 1
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            return

        if self._session_active:
            # "The game accepts one tuner connection at a time"
            # (contracts/nexus-protocol.md) -- mirror that rather than
            # silently interleaving two sessions' command streams.
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            return

        self._session_active = True
        self.connection_count += 1
        frames = _FrameReader(reader)
        commands_seen = 0
        try:
            while True:
                frame = await frames.read_one()
                if frame is None:
                    return  # client closed the connection

                if frame.tag == TAG_HANDSHAKE:
                    # Answered generically, not just during an initial
                    # handshake phase: `NexusClient.resolve_game_states()`
                    # sends a fresh `LSQ:` later in the same session, and it
                    # must get a real reply too (see `set_state_table`).
                    if frame.payload.startswith("APP:"):
                        self.app_names.append(frame.payload[len("APP:") :])
                    elif frame.payload == "LSQ:":
                        writer.write(
                            encode_frame(TAG_HANDSHAKE, _encode_state_table(self._state_table))
                        )
                        await writer.drain()
                    # Any other handshake payload is unrecognised; the real
                    # client never sends one, so just ignore and keep going.
                    continue

                if frame.tag != TAG_COMMAND:
                    continue  # not command traffic; nothing else is expected here

                commands_seen += 1
                state_index, nonce, lua_body = _parse_command(frame.payload)
                received = ReceivedCommand(
                    index=commands_seen, state_index=state_index, lua_body=lua_body, nonce=nonce
                )
                self.received.append(received)

                if self._drop_at is not None and commands_seen == self._drop_at:
                    return  # simulate a crash: the connection dies mid-operation

                if commands_seen in self._stall_at or any(
                    m in lua_body for m in self._stall_matches
                ):
                    continue  # never respond to this one; later commands still get served

                for stray in self._stray_before.pop(commands_seen, []):
                    writer.write(encode_frame(TAG_COMMAND, stray))
                    await writer.drain()

                response = self._resolve_response(state_index, lua_body, received)
                payload = f"{begin_marker(nonce)}\n{json.dumps(response)}\n{end_marker(nonce)}"
                writer.write(encode_frame(TAG_COMMAND, payload))
                await writer.drain()

                if self._drop_after is not None and commands_seen == self._drop_after:
                    # The response above was delivered; now the connection goes
                    # away, as it does for the duration of a real load. See
                    # `drop_connection_after`.
                    return
        finally:
            self._session_active = False
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    def _resolve_response(
        self, state_index: int, lua_body: str, received: ReceivedCommand
    ) -> Any:
        for entry in list(self._transcript):
            if entry.state_index is not None and entry.state_index != state_index:
                continue
            if entry.match not in lua_body:
                continue
            if not entry.repeatable:
                self._transcript.remove(entry)
            # A callable entry stands in for a *stateful* game: it may both compute this
            # command's answer and perform the side effect a real client would have (see
            # `TranscriptEntry`). Called with the command itself so it can read the Lua body.
            if callable(entry.response):
                return entry.response(received)
            return entry.response
        self.unmatched_requests.append(received)
        return self._default_response
