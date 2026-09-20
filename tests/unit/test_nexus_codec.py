"""Unit tests for the Nexus wire codec (T030).

Covers frame/parse round-trips, payloads fragmented across multiple packets
(fed in adversarial, non-frame-aligned chunk sizes -- not one clean buffer),
short frames, and oversize frames, per contracts/nexus-protocol.md "Wire
format".
"""

from __future__ import annotations

import struct

import pytest

from civsim_harness.errors import NexusError
from civsim_harness.nexus.codec import (
    HEADER_SIZE,
    TAG_COMMAND,
    TAG_HANDSHAKE,
    NexusFrame,
    NexusFrameDecoder,
    encode_frame,
)


def _chunk(data: bytes, sizes: list[int]) -> list[bytes]:
    """Split *data* into pieces of *sizes*, with any remainder as a final piece."""
    pieces = []
    offset = 0
    for size in sizes:
        pieces.append(data[offset : offset + size])
        offset += size
    if offset < len(data):
        pieces.append(data[offset:])
    return pieces


def test_tag_constants_match_the_contract() -> None:
    assert TAG_HANDSHAKE == 4
    assert TAG_COMMAND == 3


def test_header_size_is_eight_bytes() -> None:
    # uint32 length + int32 tag, both little-endian.
    assert HEADER_SIZE == 8


def test_encode_frame_length_includes_the_nul_terminator() -> None:
    encoded = encode_frame(TAG_COMMAND, "hi")
    length, tag = struct.unpack_from("<Ii", encoded, 0)
    # "hi" is 2 bytes; the declared length must be 3 (payload + NUL), not 2.
    assert length == 3
    assert tag == TAG_COMMAND
    assert encoded[HEADER_SIZE:] == b"hi\x00"


def test_encode_frame_length_is_off_by_one_check_for_empty_payload() -> None:
    # An empty payload still carries its terminator: declared length is 1, not 0.
    encoded = encode_frame(TAG_HANDSHAKE, "")
    length, _tag = struct.unpack_from("<Ii", encoded, 0)
    assert length == 1
    assert encoded[HEADER_SIZE:] == b"\x00"


def test_encode_frame_counts_utf8_bytes_not_code_points() -> None:
    # Multi-byte UTF-8 characters must be counted in bytes, not characters,
    # or a length field computed from len(str) would be wrong here.
    payload = "café\U0001f600"  # "café" + an emoji: several multi-byte chars
    encoded = encode_frame(TAG_COMMAND, payload)
    length, _tag = struct.unpack_from("<Ii", encoded, 0)
    expected_bytes = payload.encode("utf-8") + b"\x00"
    assert length == len(expected_bytes)
    assert encoded[HEADER_SIZE:] == expected_bytes


def test_round_trip_single_frame_fed_whole() -> None:
    encoded = encode_frame(TAG_COMMAND, "CMD:0:print(1)")
    decoder = NexusFrameDecoder()
    frames = decoder.feed(encoded)
    assert frames == [NexusFrame(tag=TAG_COMMAND, payload="CMD:0:print(1)")]
    assert len(decoder) == 0  # nothing left buffered


def test_round_trip_preserves_multibyte_payload() -> None:
    payload = "---BEGIN:abc---\n{\"city\": \"Xi’an\"}\n---END:abc---"
    encoded = encode_frame(TAG_COMMAND, payload)
    decoder = NexusFrameDecoder()
    frames = decoder.feed(encoded)
    assert len(frames) == 1
    assert frames[0].payload == payload


def test_multiple_frames_delivered_in_one_feed_call() -> None:
    encoded = encode_frame(TAG_HANDSHAKE, "APP:civsim") + encode_frame(TAG_HANDSHAKE, "LSQ:")
    decoder = NexusFrameDecoder()
    frames = decoder.feed(encoded)
    assert [f.payload for f in frames] == ["APP:civsim", "LSQ:"]
    assert all(f.tag == TAG_HANDSHAKE for f in frames)


@pytest.mark.parametrize(
    "chunk_sizes",
    [
        [1] * 40,  # byte-at-a-time: the most adversarial possible chunking
        [3, 1, 1, 7, 2, 5],
        [HEADER_SIZE - 1, 1, 2, 100],  # split exactly inside the header
        [HEADER_SIZE + 1, 1, 1, 1],  # split exactly inside the payload
        [1000],  # larger than the whole buffer: delivered in one go
    ],
)
def test_single_frame_fragmented_across_adversarial_chunk_sizes(
    chunk_sizes: list[int],
) -> None:
    payload = "CMD:1:" + "x" * 50
    encoded = encode_frame(TAG_COMMAND, payload)
    decoder = NexusFrameDecoder()

    pieces = _chunk(encoded, chunk_sizes)
    collected: list[NexusFrame] = []
    for piece in pieces[:-1]:
        collected.extend(decoder.feed(piece))
        # Must not fabricate a frame before all of its bytes have arrived.
        assert collected == []

    collected.extend(decoder.feed(pieces[-1]))
    assert collected == [NexusFrame(tag=TAG_COMMAND, payload=payload)]
    assert len(decoder) == 0


def test_two_frames_fragmented_across_a_boundary_that_splits_between_them() -> None:
    first = encode_frame(TAG_COMMAND, "CMD:0:one")
    second = encode_frame(TAG_COMMAND, "CMD:0:two")
    combined = first + second

    # Split so that one chunk ends partway through the second frame's header,
    # forcing the decoder to hold state across the call boundary.
    split_at = len(first) + 3
    decoder = NexusFrameDecoder()

    frames_from_first_chunk = decoder.feed(combined[:split_at])
    assert [f.payload for f in frames_from_first_chunk] == ["CMD:0:one"]

    frames_from_second_chunk = decoder.feed(combined[split_at:])
    assert [f.payload for f in frames_from_second_chunk] == ["CMD:0:two"]


def test_short_frame_waits_for_more_data_without_raising() -> None:
    encoded = encode_frame(TAG_COMMAND, "CMD:0:print(42)")
    decoder = NexusFrameDecoder()

    # Feed everything except the last three bytes: header complete, payload short.
    frames = decoder.feed(encoded[:-3])
    assert frames == []
    assert len(decoder) == len(encoded) - 3

    # The rest arrives later; now it resolves into exactly one frame.
    frames = decoder.feed(encoded[-3:])
    assert len(frames) == 1
    assert frames[0].payload == "CMD:0:print(42)"


def test_incomplete_header_waits_for_more_data_without_raising() -> None:
    decoder = NexusFrameDecoder()
    frames = decoder.feed(b"\x01\x02\x03")  # fewer than HEADER_SIZE bytes
    assert frames == []
    assert len(decoder) == 3


def test_oversize_frame_raises_nexus_error() -> None:
    max_len = 32
    decoder = NexusFrameDecoder(max_frame_length=max_len)
    header = struct.pack("<Ii", max_len + 1, TAG_COMMAND)
    with pytest.raises(NexusError):
        decoder.feed(header)


def test_frame_at_exactly_the_maximum_length_is_accepted() -> None:
    max_len = 16
    decoder = NexusFrameDecoder(max_frame_length=max_len)
    body = ("x" * (max_len - 1)).encode("utf-8") + b"\x00"  # exactly max_len bytes
    assert len(body) == max_len
    header = struct.pack("<Ii", max_len, TAG_COMMAND)
    frames = decoder.feed(header + body)
    assert len(frames) == 1
    assert frames[0].payload == "x" * (max_len - 1)


def test_zero_length_frame_raises_nexus_error() -> None:
    decoder = NexusFrameDecoder()
    header = struct.pack("<Ii", 0, TAG_COMMAND)
    with pytest.raises(NexusError):
        decoder.feed(header)


def test_missing_nul_terminator_raises_nexus_error() -> None:
    decoder = NexusFrameDecoder()
    body = b"abc"  # no trailing NUL
    header = struct.pack("<Ii", len(body), TAG_COMMAND)
    with pytest.raises(NexusError):
        decoder.feed(header + body)


def test_real_client_capture_app_response_payload_round_trips() -> None:
    """Codec-level regression fixture from the first real-world Nexus verification.

    This is the ``APP:`` response payload verified against a live Civ VI
    client (Linux/Aspyr build): three NUL-separated fields (short name,
    display name, install path), same header framing as every other frame.
    It corroborates the NUL-separated wire format independently confirmed
    for ``LSQ:`` responses (see the real-client fixture in
    tests/unit/test_nexus_client.py) -- this is a captured real value, not a
    synthetic guess, so it should not be replaced with one.

    The baked-in Windows-looking path is an Aspyr port artifact of that
    particular build, not a live path -- it is asserted here only to prove
    byte-for-byte round-tripping, not as anything semantically meaningful.
    """
    payload = "Civ6\x00Sid Meier's Civilization 6\x00C:\\Emu\\AppAssets\\base\\binaries\\Debug"
    encoded = encode_frame(TAG_HANDSHAKE, payload)
    decoder = NexusFrameDecoder()
    frames = decoder.feed(encoded)
    assert len(frames) == 1
    assert frames[0].tag == TAG_HANDSHAKE
    assert frames[0].payload == payload
    assert frames[0].payload.split("\x00") == [
        "Civ6",
        "Sid Meier's Civilization 6",
        "C:\\Emu\\AppAssets\\base\\binaries\\Debug",
    ]
