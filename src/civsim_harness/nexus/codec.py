"""Nexus wire codec (T029): framing for the Firaxis Nexus / FireTuner protocol.

Every message on the wire is a fixed 8-byte header followed by a
NUL-terminated UTF-8 payload (contracts/nexus-protocol.md "Wire format")::

    length uint32 LE | tag int32 LE | payload UTF-8, NUL-terminated

``length`` is the size of the payload *including* its NUL terminator. That
"+1 for the terminator" is the classic off-by-one here, so it is exercised
explicitly in tests/unit/test_nexus_codec.py.

This module only frames and de-frames bytes. It does not know about
sockets, handshakes, or sentinel correlation -- those live in
:mod:`civsim_harness.nexus.client`.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from civsim_harness.errors import NexusError

#: Identifies the application and enumerates available Lua states
#: (``APP:<name>`` / ``LSQ:`` payloads).
TAG_HANDSHAKE = 4

#: Executes Lua in a resolved state (``CMD:<state_index>:<lua_code>`` payload).
#: Verified live (2026-09-20, specs/002-civ-playing-harness/spikes/r5-raw-windows/
#: raw_command_transcript.txt): the client's *reply* on this tag is an **empty**
#: acknowledgement frame -- a command's actual printed output arrives on
#: :data:`TAG_ASYNC_OUTPUT` instead.
TAG_COMMAND = 3

#: Asynchronous output from the game client: every ``print()`` line (and every
#: engine log line) arrives as its own frame on this tag, payload prefixed
#: ``O\0<LuaStateName>: ``. This is the only tag that ever carries a command's
#: printed result -- verified live on Windows (specs/002-civ-playing-harness/
#: spikes/r5-raw-windows/raw_command_transcript.txt: sentinels came back on
#: tag -1 while the tag-3 reply was empty) and consistent with the Linux
#: spikes, which strip the identical prefix (specs/002-civ-playing-harness/
#: spikes/r5-raw/t077_enumerate.py). These frames also interleave unsolicited
#: into handshake traffic (r5-raw-windows/raw_protocol_transcript.txt).
TAG_ASYNC_OUTPUT = -1

# length: uint32 little-endian, tag: int32 little-endian.
_HEADER = struct.Struct("<Ii")
HEADER_SIZE = _HEADER.size  # 8

#: Defensive upper bound on a single frame's payload length. Not specified by
#: the protocol (which places no documented limit); this exists solely so a
#: corrupted or malicious length field cannot make the decoder buffer an
#: unbounded amount of memory while waiting for a frame that will never
#: complete. Chosen generously relative to any real tuner payload (a JSON
#: capability result or a Lua command body), so it should never bind in
#: normal operation.
DEFAULT_MAX_FRAME_LENGTH = 16 * 1024 * 1024


@dataclass(frozen=True)
class NexusFrame:
    """One fully decoded wire frame."""

    tag: int
    payload: str


def encode_frame(tag: int, payload: str) -> bytes:
    """Encode *payload* under *tag* into one complete wire frame.

    The declared ``length`` covers the UTF-8-encoded payload bytes plus the
    trailing NUL terminator this function appends.
    """
    body = payload.encode("utf-8") + b"\x00"
    header = _HEADER.pack(len(body), tag)
    return header + body


class NexusFrameDecoder:
    """Incremental frame decoder for a byte stream that may arrive in any chunking.

    The wire does not promise that one frame arrives in one ``recv()`` --
    a header or a payload can be split across an arbitrary number of
    packets. Callers feed bytes as they arrive via :meth:`feed`, which
    returns every frame that became complete as a result, holding a partial
    frame internally until the rest of it shows up.
    """

    def __init__(self, *, max_frame_length: int = DEFAULT_MAX_FRAME_LENGTH) -> None:
        self._buffer = bytearray()
        self._max_frame_length = max_frame_length

    def __len__(self) -> int:
        """Number of bytes currently buffered that have not yet resolved into a frame."""
        return len(self._buffer)

    def feed(self, data: bytes) -> list[NexusFrame]:
        """Append newly arrived bytes and return every frame that is now complete."""
        self._buffer.extend(data)
        frames: list[NexusFrame] = []
        while True:
            frame = self._try_take_one()
            if frame is None:
                break
            frames.append(frame)
        return frames

    def _try_take_one(self) -> NexusFrame | None:
        if len(self._buffer) < HEADER_SIZE:
            return None  # header itself has not fully arrived yet

        length, tag = _HEADER.unpack_from(self._buffer, 0)

        if length <= 0:
            raise NexusError(
                "Nexus frame declared a non-positive length",
                detail={"length": length, "tag": tag},
            )
        if length > self._max_frame_length:
            raise NexusError(
                "Nexus frame exceeds the maximum allowed length",
                detail={"length": length, "max_frame_length": self._max_frame_length, "tag": tag},
            )

        total = HEADER_SIZE + length
        if len(self._buffer) < total:
            return None  # short frame: payload has not fully arrived yet

        body = bytes(self._buffer[HEADER_SIZE:total])
        del self._buffer[:total]

        if not body.endswith(b"\x00"):
            raise NexusError(
                "Nexus payload is missing its NUL terminator",
                detail={"tag": tag, "length": length},
            )
        return NexusFrame(tag=tag, payload=body[:-1].decode("utf-8"))
