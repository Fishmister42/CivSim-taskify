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

Ambiguity resolved: contracts/nexus-protocol.md says ``LSQ:`` "enumerate[s]
available Lua states" but does not pin the response payload's wire format.
:func:`_parse_state_list` resolves this as newline-separated state names in
positional (index) order -- the simplest reading consistent with "indices
are positional" (Connection sequence, step 4-5). If a later wave's recorded
transcript shows a different separator, that function is the only place to
change.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
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

_logger = logging.getLogger(__name__)


def _default_telemetry_sink(text: str) -> None:
    """Fallback telemetry route: stdlib logging under this module's name.

    Deliberately does not import :mod:`civsim_harness.telemetry` -- that
    package is being written concurrently elsewhere. A caller that wants
    unmatched output routed into the harness's structured telemetry instead
    passes its own sink via ``NexusClient(on_unmatched_output=...)``.
    """
    _logger.warning("nexus: discarding unmatched output: %s", text)


def _parse_state_list(payload: str) -> list[str]:
    """Parse an ``LSQ:`` response payload into ordered Lua state names.

    See the module docstring's "Ambiguity resolved" note: this is the one
    function to change if a recorded transcript shows a different wire
    format for the state listing.
    """
    return [line.strip() for line in payload.splitlines() if line.strip()]


@dataclass(frozen=True)
class StateIndices:
    """Resolved Lua state indices for one connected session.

    Positional and not guaranteed stable across game versions or mod sets
    (contracts/nexus-protocol.md "Connection sequence", step 5) -- callers
    must re-resolve on every (re)connect rather than reusing indices from
    before a disconnect (T159).
    """

    game_core_tuner: int
    in_game: int


class NexusClient:
    """Asyncio client for the Firaxis Nexus / FireTuner wire protocol.

    One instance owns exactly one socket to the game for the lifetime of a
    run. All command execution goes through :meth:`execute_command`, which
    serializes access with an internal lock and enforces a per-command
    timeout -- there is no turn-level timer anywhere in this class.
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
        """Connect, run the full handshake, and resolve state indices.

        A refused (or otherwise failed) connection is a *preparation*
        failure (contracts/nexus-protocol.md "Connection sequence", step 1:
        "the client is not running or the tuner is not enabled") -- raised
        as :class:`PreflightError`, distinct from :class:`NexusError`, so
        callers can route "nothing to connect to" differently from an
        in-run transport fault.
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
        client is entitled to rely on.
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
        await self._send_raw(TAG_HANDSHAKE, f"APP:{self._app_name}")
        await self._send_raw(TAG_HANDSHAKE, "LSQ:")

        frame = await self._read_frame()
        if frame.tag != TAG_HANDSHAKE:
            raise NexusError(
                "Expected a TAG_HANDSHAKE response to LSQ:, got a different tag",
                detail={"reason": REASON_HANDSHAKE_FAILED, "tag": frame.tag},
            )

        state_names = _parse_state_list(frame.payload)
        positions = {name: index for index, name in enumerate(state_names)}
        missing = [name for name in _REQUIRED_STATES if name not in positions]
        if missing:
            raise NexusError(
                "Nexus LSQ response is missing required Lua state(s)",
                detail={
                    "reason": REASON_HANDSHAKE_FAILED,
                    "missing": missing,
                    "states": state_names,
                },
            )

        return StateIndices(
            game_core_tuner=positions["GameCore_Tuner"],
            in_game=positions["InGame"],
        )

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
