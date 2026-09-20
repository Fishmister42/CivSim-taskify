"""Nexus client (T031, T032, T159): connection, handshake, request/response discipline.

First-party asyncio client for the Firaxis Nexus / FireTuner wire protocol
(research R2) -- no third-party transport library. This is the only module
permitted to hold a socket to the game (contracts/nexus-protocol.md
"Implementation notes"): Civ VI accepts exactly one tuner connection at a
time, and a single :class:`asyncio.Lock` serializes command execution so
concurrent callers cannot interleave output across nonces on that one
connection.

Civ VI is not running in this environment and there is no fake tuner server
yet (that arrives in a later wave), so the socket-facing paths here are not
exercised by this task's own tests. They are still written to the full
contract; :mod:`civsim_harness.nexus.sentinels` isolates the one piece that
*is* unit-tested here (sentinel correlation) from everything socket-shaped.

Verified against a real client (first real-world Nexus transport
verification, a live Civ VI client on Linux): contracts/nexus-protocol.md
says ``LSQ:`` "enumerate[s] available Lua states" but does not pin the
response payload's wire format. An earlier implementation guessed
"newline-separated state names in positional order" -- that guess was
wrong. A captured transcript (see the real-client fixture in
tests/unit/test_nexus_client.py) confirms the actual format: NUL-separated
alternating ``<index>\0<name>\0`` pairs, e.g.
``"0\x00Main State\x001\x00DebugHotloadCache"``. There is no newline
anywhere in that payload. :func:`_parse_state_list` parses each index from
the payload itself -- never from a pair's position in the list, since nothing
guarantees the wire enumerates states in index order.

Also verified against that same real client: the main-menu state table
contains only ``Main State`` and ``DebugHotloadCache`` -- ``GameCore_Tuner``
and ``InGame`` do not exist until a game is loaded. That makes "connect and
resolve the game states" two separate concerns with two separate points in
the run sequence: :meth:`NexusClient.connect` performs the handshake and
resolves whatever states exist (so it succeeds against a client sitting at
the main menu), and :meth:`NexusClient.resolve_game_states` is the explicit
later step, called once a game is loaded, that requires ``GameCore_Tuner``
and ``InGame`` to be present.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from civsim_harness.errors import NexusError, PreflightError
from civsim_harness.nexus.codec import (
    TAG_COMMAND,
    TAG_HANDSHAKE,
    NexusFrame,
    NexusFrameDecoder,
    encode_frame,
)
from civsim_harness.nexus.sentinels import SentinelCorrelator, TelemetrySink, wrap_lua

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4318

#: Bound on the initial TCP connect + handshake round-trip. Not a
#: per-operation command timeout -- see DEFAULT_COMMAND_TIMEOUT_S for that.
DEFAULT_CONNECT_TIMEOUT_S = 5.0

#: Default per-command timeout (research R12, FR-014: scoped to a single
#: operation, never to a turn -- callers may override per call via
#: ``execute_command(timeout_s=...)``, e.g. resilience/operation_bounds.py
#: assigning a different bound per capability).
DEFAULT_COMMAND_TIMEOUT_S = 30.0

_REQUIRED_STATES = ("GameCore_Tuner", "InGame")

# Structured, greppable values for NexusError.detail["reason"] -- exported so
# callers (heartbeat probing, resilience detection/recovery) can distinguish
# failure modes without a proliferation of exception subclasses.
REASON_NOT_CONNECTED = "not_connected"
REASON_TIMEOUT = "timeout"
REASON_INVALID_RESULT_JSON = "invalid_result_json"
REASON_HANDSHAKE_FAILED = "handshake_failed"
REASON_CONNECTION_CLOSED = "connection_closed"
#: Raised by resolve_game_states() when GameCore_Tuner and/or InGame are not
#: (yet) in the state table -- distinct from REASON_HANDSHAKE_FAILED, which
#: is reserved for an actual protocol violation (e.g. the wrong response
#: tag). A missing game state is an expected, reportable condition (no game
#: loaded yet), not a broken handshake.
REASON_GAME_STATES_UNAVAILABLE = "game_states_unavailable"

_logger = logging.getLogger(__name__)


def _default_telemetry_sink(text: str) -> None:
    """Fallback telemetry route: stdlib logging under this module's name.

    Deliberately does not import :mod:`civsim_harness.telemetry` -- that
    package is being written concurrently elsewhere. A caller that wants
    unmatched output routed into the harness's structured telemetry instead
    passes its own sink via ``NexusClient(on_unmatched_output=...)``.
    """
    _logger.warning("nexus: discarding unmatched output: %s", text)


def _parse_state_list(payload: str) -> dict[str, int]:
    """Parse an ``LSQ:`` response payload into a ``{name: index}`` mapping.

    **Verified against a real client** (first real-world Nexus transport
    verification -- see the captured-bytes regression fixture in
    tests/unit/test_nexus_client.py). The wire format is NUL-separated,
    alternating ``<index>\\0<name>\\0`` pairs, e.g.::

        "0\\x00Main State\\x001\\x00DebugHotloadCache"

    There is no newline anywhere in that payload. An earlier implementation
    guessed "newline-separated names in positional order"; against a real
    client that guess produced a single unsplit element and every index
    resolution failed. Each index is parsed from its own field in the
    payload -- never inferred from a pair's position in the list, so a
    state table with non-contiguous or reordered indices still resolves
    correctly.
    """
    tokens = payload.split("\x00")
    # A well-formed payload splits into an even number of non-empty tokens
    # (index, name, index, name, ...). Tolerate one trailing empty token, in
    # case a payload ever keeps a trailing separator.
    if tokens and tokens[-1] == "":
        tokens = tokens[:-1]
    if not tokens:
        return {}
    if len(tokens) % 2 != 0:
        raise NexusError(
            "Nexus LSQ response payload has an odd number of NUL-separated "
            "fields; expected alternating <index>\\0<name> pairs",
            detail={"reason": REASON_HANDSHAKE_FAILED, "payload": payload},
        )

    states: dict[str, int] = {}
    for position in range(0, len(tokens), 2):
        index_text, name = tokens[position], tokens[position + 1]
        try:
            index = int(index_text)
        except ValueError as exc:
            raise NexusError(
                "Nexus LSQ response payload has a non-integer state index",
                detail={"reason": REASON_HANDSHAKE_FAILED, "field": index_text},
            ) from exc
        states[name] = index
    return states


@dataclass(frozen=True)
class StateIndices:
    """Resolved Lua state indices for one connected session.

    ``by_name`` reflects whatever the most recent ``LSQ:`` query reported.
    Verified against a real client: at the main menu that is only
    ``Main State`` and ``DebugHotloadCache`` -- ``GameCore_Tuner`` and
    ``InGame`` do not exist in the state table until a game is loaded.
    ``game_core_tuner`` / ``in_game`` are ``None`` until then; check
    :attr:`has_game_states`, or call :meth:`NexusClient.resolve_game_states`
    to turn "not available yet" into an explicit, reportable
    :class:`~civsim_harness.errors.PreflightError` naming what is missing,
    instead of an opaque failure.

    Positional and not guaranteed stable across game versions or mod sets
    (contracts/nexus-protocol.md "Connection sequence", step 5) -- callers
    must re-resolve on every (re)connect rather than reusing indices from
    before a disconnect (T159).
    """

    by_name: Mapping[str, int]
    game_core_tuner: int | None = None
    in_game: int | None = None

    @property
    def has_game_states(self) -> bool:
        """Whether both game-play states (``GameCore_Tuner``, ``InGame``) are resolved."""
        return self.game_core_tuner is not None and self.in_game is not None


class NexusClient:
    """Asyncio client for the Firaxis Nexus / FireTuner wire protocol.

    One instance owns exactly one socket to the game for the lifetime of a
    run. All command execution goes through :meth:`execute_command`, which
    serializes access with an internal lock and enforces a per-command
    timeout -- there is no turn-level timer anywhere in this class.

    :meth:`connect` performs the handshake and resolves whatever Lua states
    exist, succeeding even against a client sitting at the main menu.
    :meth:`resolve_game_states` is the separate, later step -- call it once
    a game is loaded -- that requires ``GameCore_Tuner`` and ``InGame`` and
    raises :class:`~civsim_harness.errors.PreflightError` naming whichever
    is still missing.
    """

    def __init__(
        self,
        *,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        app_name: str = "civsim_harness",
        connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S,
        command_timeout_s: float = DEFAULT_COMMAND_TIMEOUT_S,
        on_unmatched_output: TelemetrySink | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._app_name = app_name
        self._connect_timeout_s = connect_timeout_s
        self._command_timeout_s = command_timeout_s
        self._on_unmatched_output: TelemetrySink = on_unmatched_output or _default_telemetry_sink

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._decoder = NexusFrameDecoder()
        self._correlator = SentinelCorrelator(on_unmatched=self._on_unmatched_output)
        self._pending_frames: list[NexusFrame] = []
        self._lock = asyncio.Lock()
        self._state_indices: StateIndices | None = None

    @property
    def state_indices(self) -> StateIndices | None:
        """Indices resolved by the most recent (re)connect, or ``None`` before any connect."""
        return self._state_indices

    @property
    def is_connected(self) -> bool:
        return self._writer is not None

    # -- connection lifecycle -------------------------------------------

    async def connect(self) -> StateIndices:
        """Connect, run the handshake, and resolve whatever Lua states exist.

        A refused (or otherwise failed) connection is a *preparation*
        failure (contracts/nexus-protocol.md "Connection sequence", step 1:
        "the client is not running or the tuner is not enabled") -- raised
        as :class:`PreflightError`, distinct from :class:`NexusError`, so
        callers can route "nothing to connect to" differently from an
        in-run transport fault.

        This succeeds against a client sitting at the main menu, where the
        state table has no game-play states yet (verified against a real
        client -- see the module docstring). It does **not** require
        ``GameCore_Tuner`` or ``InGame`` to be present; call
        :meth:`resolve_game_states` separately once a game is loaded.
        """
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port),
                timeout=self._connect_timeout_s,
            )
        except (OSError, TimeoutError) as exc:
            raise PreflightError(
                "Could not connect to the Nexus tuner interface",
                detail={"host": self._host, "port": self._port, "reason": str(exc)},
            ) from exc

        self._reader = reader
        self._writer = writer
        self._decoder = NexusFrameDecoder()
        self._pending_frames = []
        self._correlator = SentinelCorrelator(on_unmatched=self._on_unmatched_output)

        try:
            self._state_indices = await asyncio.wait_for(
                self._handshake(), timeout=self._connect_timeout_s
            )
        except TimeoutError as exc:
            await self._close()
            raise NexusError(
                "Nexus handshake did not complete within the connect timeout",
                detail={"reason": REASON_TIMEOUT},
            ) from exc
        except Exception:
            await self._close()
            raise
        return self._state_indices

    async def reconnect(self) -> StateIndices:
        """Reconnect discipline (T159): drop any stale connection, then connect() again.

        Always re-runs the full handshake and re-resolves state indices.
        Indices captured before a disconnect are never reused, even if the
        new connection happens to enumerate Lua states in the same order --
        that would be an assumption about game/mod state, not a fact this
        client is entitled to rely on. As with the first connect, the
        result may not yet have game states resolved (``has_game_states``
        may be ``False``) -- call :meth:`resolve_game_states` again if the
        caller needs them.
        """
        await self._close()
        self._state_indices = None
        return await self.connect()

    async def close(self) -> None:
        """Close the connection. Safe to call when already closed."""
        await self._close()

    async def _close(self) -> None:
        writer = self._writer
        self._reader = None
        self._writer = None
        self._pending_frames = []
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    # -- handshake --------------------------------------------------------

    async def _handshake(self) -> StateIndices:
        """``APP:`` to identify, then resolve whatever Lua states currently exist.

        Does not require ``GameCore_Tuner`` / ``InGame`` -- see
        :meth:`resolve_game_states` for the step that does.
        """
        await self._send_raw(TAG_HANDSHAKE, f"APP:{self._app_name}")
        return await self._query_states()

    async def _query_states(self) -> StateIndices:
        """Send ``LSQ:`` and parse whatever Lua states the client currently reports.

        Shared by :meth:`_handshake` (the initial connect) and
        :meth:`resolve_game_states` (the later, explicit step): both need
        "the current state table," just at different points in the run.
        """
        await self._send_raw(TAG_HANDSHAKE, "LSQ:")

        frame = await self._read_frame()
        if frame.tag != TAG_HANDSHAKE:
            raise NexusError(
                "Expected a TAG_HANDSHAKE response to LSQ:, got a different tag",
                detail={"reason": REASON_HANDSHAKE_FAILED, "tag": frame.tag},
            )

        by_name = _parse_state_list(frame.payload)
        return StateIndices(
            by_name=by_name,
            game_core_tuner=by_name.get("GameCore_Tuner"),
            in_game=by_name.get("InGame"),
        )

    async def resolve_game_states(self) -> StateIndices:
        """Require ``GameCore_Tuner`` and ``InGame`` to be present, right now.

        Call this once a game is loaded -- :meth:`connect` deliberately does
        not require these two states, since (verified against a real
        client) they do not exist in the state table at the main menu.
        Sequencing "connect" and "the game is actually loaded" as two
        distinct steps lets a caller like ``doctor`` report "tuner
        reachable, no game loaded" as a normal, useful diagnostic instead of
        connect() failing with a confusing preflight error.

        Re-sends ``LSQ:`` (not the full handshake -- ``APP:`` identifies the
        session once, at :meth:`connect`) so this reflects the state table
        as it is *now*, and updates :attr:`state_indices` on success.

        Raises :class:`PreflightError` naming exactly which required
        state(s) are still missing if either is absent -- this is an
        expected, reportable condition (no game loaded yet), not a broken
        handshake.
        """
        if self._writer is None or self._reader is None:
            raise NexusError(
                "Nexus client is not connected", detail={"reason": REASON_NOT_CONNECTED}
            )

        async with self._lock:
            indices = await self._query_states()

        missing = [name for name in _REQUIRED_STATES if name not in indices.by_name]
        if missing:
            raise PreflightError(
                "Required Nexus Lua state(s) are not available yet -- is a game loaded?",
                detail={
                    "reason": REASON_GAME_STATES_UNAVAILABLE,
                    "missing": missing,
                    "states": sorted(indices.by_name),
                },
            )

        self._state_indices = indices
        return indices

    # -- request/response discipline --------------------------------------

    async def execute_command(
        self,
        *,
        state_index: int,
        lua_body: str,
        timeout_s: float | None = None,
    ) -> Any:
        """Execute *lua_body* in *state_index* and return its parsed JSON result.

        Wraps *lua_body* with a fresh per-request nonce
        (contracts/nexus-protocol.md "Request/response discipline") and
        waits for the matched ``---BEGIN:<nonce>---`` / ``---END:<nonce>---``
        pair. Everything else the game prints while waiting -- stray
        prints, a previous request's late tail, output carrying a different
        nonce -- is routed to telemetry and never considered as this call's
        result (see :mod:`civsim_harness.nexus.sentinels`).

        *timeout_s* bounds this one command only (research R12, FR-014):
        there is no turn-level timer anywhere in this client. A single
        :class:`asyncio.Lock` around the send-and-wait sequence serializes
        access to the shared socket so two concurrent callers cannot
        interleave output across nonces.
        """
        if self._writer is None or self._reader is None:
            raise NexusError(
                "Nexus client is not connected", detail={"reason": REASON_NOT_CONNECTED}
            )

        bound = timeout_s if timeout_s is not None else self._command_timeout_s
        nonce = uuid.uuid4().hex
        payload = f"CMD:{state_index}:{wrap_lua(nonce, lua_body)}"

        async with self._lock:
            await self._send_raw(TAG_COMMAND, payload)
            try:
                raw_result = await asyncio.wait_for(self._await_result(nonce), timeout=bound)
            except TimeoutError as exc:
                self._correlator.discard_unmatched()
                raise NexusError(
                    "Nexus command exceeded its per-operation timeout",
                    detail={
                        "reason": REASON_TIMEOUT,
                        "state_index": state_index,
                        "timeout_s": bound,
                    },
                ) from exc

        try:
            return json.loads(raw_result)
        except json.JSONDecodeError as exc:
            raise NexusError(
                "Nexus command result was not valid JSON",
                detail={"reason": REASON_INVALID_RESULT_JSON, "state_index": state_index},
            ) from exc

    async def _await_result(self, nonce: str) -> str:
        while True:
            result = self._correlator.take_result(nonce)
            if result is not None:
                return result
            frame = await self._read_frame()
            if frame.tag == TAG_COMMAND:
                self._correlator.feed(frame.payload)
            # Frames of any other tag are not print output and are not fed
            # to the correlator, which only ever interprets TAG_COMMAND
            # payloads as game print output.

    # -- wire plumbing ------------------------------------------------------

    async def _send_raw(self, tag: int, payload: str) -> None:
        if self._writer is None:
            raise NexusError(
                "Nexus client is not connected", detail={"reason": REASON_NOT_CONNECTED}
            )
        self._writer.write(encode_frame(tag, payload))
        await self._writer.drain()

    async def _read_frame(self) -> NexusFrame:
        while not self._pending_frames:
            if self._reader is None:
                raise NexusError(
                    "Nexus client is not connected", detail={"reason": REASON_NOT_CONNECTED}
                )
            chunk = await self._reader.read(4096)
            if not chunk:
                raise NexusError(
                    "Nexus connection closed by the game client",
                    detail={"reason": REASON_CONNECTION_CLOSED},
                )
            self._pending_frames.extend(self._decoder.feed(chunk))
        return self._pending_frames.pop(0)
