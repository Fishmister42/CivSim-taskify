"""A tuner double that dies the way a killed game client dies: with a TCP reset (T281).

`fakes.fake_nexus.FakeNexusServer` can already drop a connection mid-operation
(`drop_connection_at`), but it drops it *politely*: the handler returns, the writer is closed,
and the client sees a clean EOF -- an empty `read()`, which
`civsim_harness.nexus.client.NexusClient` has always named
(`NexusError`, `REASON_CONNECTION_CLOSED`).

A real client that is killed, crashes, or is force-closed does not send a FIN. The peer's kernel
answers with an RST, and the harness side's `read()`/`drain()` raises `ConnectionResetError` --
an `OSError`, **not** a `HarnessError`. That is the shape T281 exists for, and nothing in the
test suite could produce it before this module: `SO_LINGER {on, 0}` followed by a close is the
one portable way to make a socket abort rather than shut down.

Deliberately minimal -- handshake and abort, nothing else. Any test that needs a *working*
command transcript wants `FakeNexusServer`; this one exists to stop working.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import struct
from collections.abc import Mapping

from civsim_harness.nexus.codec import (
    TAG_HANDSHAKE,
    NexusFrameDecoder,
    encode_frame,
)
from fakes.fake_nexus import APP_REPLY_PAYLOAD, loaded_game_state_table


def encode_state_table(state_table: Mapping[str, int]) -> str:
    """The live-verified `LSQ:` payload shape: NUL-separated `<index>\\0<name>` pairs."""
    return "\x00".join(f"{index}\x00{name}" for name, index in state_table.items())


class AbortingTuner:
    """Answers the Nexus handshake, then aborts the connection on demand (RST, not FIN).

    Usage::

        tuner = AbortingTuner()
        await tuner.start()
        client = NexusClient(host="127.0.0.1", port=tuner.port)
        await client.connect()
        tuner.abort()          # the client is now dead the way a killed game is dead
        ...                    # every later command raises NexusError(REASON_CONNECTION_RESET)
        await tuner.stop()

    `abort()` is synchronous and idempotent: it sets `SO_LINGER` to `{on, 0}` on the live socket
    and aborts the transport, so the peer's next read or write gets `ECONNRESET`/`EPIPE` rather
    than an orderly end of stream.
    """

    def __init__(self, state_table: Mapping[str, int] | None = None) -> None:
        self._state_table = dict(state_table or loaded_game_state_table())
        self._server: asyncio.AbstractServer | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._connected = asyncio.Event()
        self.port = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        self.abort()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None

    async def wait_connected(self, *, timeout_s: float = 5.0) -> None:
        await asyncio.wait_for(self._connected.wait(), timeout=timeout_s)

    def abort(self) -> None:
        """Kill the live connection with a reset. Safe to call when there is none, or twice."""
        writer = self._writer
        self._writer = None
        if writer is None:
            return
        sock = writer.get_extra_info("socket")
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.setsockopt(
                    socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
                )
        with contextlib.suppress(Exception):
            writer.transport.abort()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writer = writer
        decoder = NexusFrameDecoder()
        try:
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    return
                for frame in decoder.feed(chunk):
                    if frame.tag != TAG_HANDSHAKE:
                        continue  # a command: this tuner never answers one
                    if frame.payload.startswith("APP:"):
                        writer.write(encode_frame(TAG_HANDSHAKE, APP_REPLY_PAYLOAD))
                        await writer.drain()
                    elif frame.payload == "LSQ:":
                        writer.write(
                            encode_frame(TAG_HANDSHAKE, encode_state_table(self._state_table))
                        )
                        await writer.drain()
                        self._connected.set()
        except (OSError, asyncio.CancelledError):
            return
        finally:
            with contextlib.suppress(Exception):
                writer.close()


__all__ = ["AbortingTuner", "encode_state_table"]
