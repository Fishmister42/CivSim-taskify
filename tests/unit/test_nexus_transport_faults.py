"""T281 -- a dead peer on the Nexus wire must arrive as a *named* harness failure.

**The defect these tests pin.** `ConnectionResetError` (and `BrokenPipeError`, and
`ConnectionAbortedError`) are `OSError`s, not `civsim_harness.errors.HarnessError`s. The steady
-state transport -- `NexusClient._send_raw`'s `write()`/`drain()` and `_read_frame`'s `read()` --
used to let one out raw. Every handler above the transport is keyed on `HarnessError`:
`run/turn_cycle.py`'s pre-save fault handling, and `Runner._handle_run_failure`, which is
exhaustive over `HarnessError` and routes every member to a legal recorded lifecycle state. A raw
`OSError` sailed past all of it into `Runner._record_unexpected_failure`, which logs and sets an
in-memory flag and performs **no** lifecycle transition -- so a client that died mid-run left the
run in `playing` forever with no recorded stop condition at all (FR-005, data-model.md invariant
I10).

`_read_frame` already named the *other* way a peer can vanish -- a clean EOF, `REASON_CONNECTION
_CLOSED` -- and `_is_reconnect_refusal` already documented `ConnectionResetError`/`Connection
AbortedError` as an expected shape. The knowledge was in the file; it had simply never been
applied to the read/write path.

`tests/integration/test_transport_fault_recorded.py` is the other half of this: it proves the
*production* path (a real `NexusClient` -> the real decision loop -> `run/turn_cycle.py` ->
`Runner`) now records the failure, rather than only that the exception type changed here.
"""

from __future__ import annotations

from typing import Any

import pytest

from civsim_harness.errors import NexusError, PreflightError
from civsim_harness.nexus.client import (
    REASON_CONNECTION_CLOSED,
    REASON_CONNECTION_RESET,
    NexusClient,
    _is_reconnect_refusal,
)
from civsim_harness.nexus.codec import TAG_COMMAND
from fakes.aborting_tuner import AbortingTuner

# The three `OSError` shapes a killed / crashed / force-closed client actually produces. All are
# the same fact -- "the peer is gone" -- and none of them was a `HarnessError` before T281.
DEAD_PEER_ERRORS = [
    ConnectionResetError(104, "Connection reset by peer"),
    BrokenPipeError(32, "Broken pipe"),
    ConnectionAbortedError(103, "Software caused connection abort"),
]


class _DeadWriter:
    """An `asyncio.StreamWriter` stand-in whose `drain()` reports the peer is gone."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.written: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        raise self._exc


class _DeadReader:
    """An `asyncio.StreamReader` stand-in whose `read()` reports the peer is gone."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def read(self, _size: int) -> bytes:
        raise self._exc


def _client_with(*, reader: Any = None, writer: Any = None) -> NexusClient:
    """A `NexusClient` holding the given transport halves, without a real socket.

    Reaching for the private attributes is deliberate and is the narrowest possible injection
    point: everything above them -- `_send_raw`, `_read_frame`, `execute_command` -- is the real
    production code under test. The real-socket test below covers the half this cannot: that a
    genuine kernel-level RST produces exactly these exceptions in the first place.
    """
    client = NexusClient(host="127.0.0.1", port=1)
    client._reader = reader
    client._writer = writer
    return client


@pytest.mark.parametrize("exc", DEAD_PEER_ERRORS, ids=lambda e: type(e).__name__)
async def test_send_raw_names_a_dead_peer_instead_of_leaking_an_oserror(
    exc: OSError,
) -> None:
    """A write to a dead peer raises `NexusError`, not the bare `OSError` it started as."""
    client = _client_with(writer=_DeadWriter(exc), reader=_DeadReader(exc))

    with pytest.raises(NexusError) as excinfo:
        await client._send_raw(TAG_COMMAND, "CMD:0:print('x')")

    assert excinfo.value.detail["reason"] == REASON_CONNECTION_RESET
    # The original failure is never thrown away -- it is the `__cause__`, so a traceback still
    # says which OS-level error actually happened.
    assert isinstance(excinfo.value.__cause__, type(exc))
    assert excinfo.value.detail["error_type"] == type(exc).__name__


@pytest.mark.parametrize("exc", DEAD_PEER_ERRORS, ids=lambda e: type(e).__name__)
async def test_read_frame_names_a_dead_peer_instead_of_leaking_an_oserror(
    exc: OSError,
) -> None:
    """A read from a dead peer raises `NexusError`, not the bare `OSError` it started as."""
    client = _client_with(reader=_DeadReader(exc), writer=_DeadWriter(exc))

    with pytest.raises(NexusError) as excinfo:
        await client._read_frame()

    assert excinfo.value.detail["reason"] == REASON_CONNECTION_RESET
    assert isinstance(excinfo.value.__cause__, type(exc))


async def test_a_clean_eof_is_still_named_a_close_not_a_reset() -> None:
    """The pre-existing distinction survives: an orderly shutdown is not a reset.

    Both mean "the peer is gone", but *how* it went is evidence worth keeping -- a reset is a
    process death or a forced close, an EOF is an orderly shutdown.
    """

    class _EofReader:
        async def read(self, _size: int) -> bytes:
            return b""

    client = _client_with(reader=_EofReader(), writer=_DeadWriter(ConnectionResetError()))

    with pytest.raises(NexusError) as excinfo:
        await client._read_frame()

    assert excinfo.value.detail["reason"] == REASON_CONNECTION_CLOSED


@pytest.mark.parametrize("where", ["read", "drain"])
async def test_a_timeout_is_not_mis_named_a_dead_peer(where: str) -> None:
    """`TimeoutError` is an `OSError` subclass (PEP 3151) -- it must not be swallowed as a death.

    A busy client is not a faulty one (research R12, FR-014), and `execute_command` /`connect`
    have their own `REASON_TIMEOUT` handling that only works if the timeout keeps its identity.
    """
    exc = TimeoutError("still working")
    client = _client_with(reader=_DeadReader(exc), writer=_DeadWriter(exc))

    with pytest.raises(TimeoutError):
        if where == "read":
            await client._read_frame()
        else:
            await client._send_raw(TAG_COMMAND, "CMD:0:print('x')")


async def test_execute_command_over_a_real_tcp_reset_raises_a_named_nexus_error() -> None:
    """End to end over a real socket: a genuine RST becomes `NexusError`, never an `OSError`.

    This is the shape the live client produced (a client death during the pre-save probe or
    sweep) and the one no fake in this suite could make before `fakes.aborting_tuner`:
    `FakeNexusServer.drop_connection_at` closes politely and yields a clean EOF instead.
    """
    tuner = AbortingTuner()
    await tuner.start()
    client = NexusClient(host="127.0.0.1", port=tuner.port)
    try:
        indices = await client.connect()
        assert indices.has_game_states is True
        await tuner.wait_connected()

        tuner.abort()

        with pytest.raises(NexusError) as excinfo:
            await client.execute_command(
                state_index=indices.in_game or 0,
                lua_body="return 1",
                timeout_s=5.0,
            )
    finally:
        await client.close()
        await tuner.stop()

    # An `OSError` here -- which is what this raised before T281 -- is the whole defect: it is not
    # a `HarnessError`, so no handler above the transport would ever have seen it.
    assert not isinstance(excinfo.value, OSError)
    assert excinfo.value.detail["reason"] == REASON_CONNECTION_RESET
    assert isinstance(excinfo.value.__cause__, OSError)


def test_is_reconnect_refusal_still_recognises_a_reset_now_that_it_has_a_name() -> None:
    """T246's post-close refusal tail must keep retrying the reset shape it always retried.

    Before T281 a reset during the handshake arrived at `reconnect()` as a bare `OSError`, which
    `_is_reconnect_refusal`'s last line matched. Now it arrives named, so the `NexusError` arm has
    to recognise it -- otherwise naming the failure would silently *disable* the bounded retry
    that rides out the live-measured ~2 s tail, turning a healthy client into a dead one.
    """
    reset = NexusError("gone", detail={"reason": REASON_CONNECTION_RESET})
    closed = NexusError("gone", detail={"reason": REASON_CONNECTION_CLOSED})
    protocol_fault = NexusError("odd number of NUL-separated fields", detail={"reason": "other"})

    assert _is_reconnect_refusal(reset) is True
    assert _is_reconnect_refusal(closed) is True
    assert _is_reconnect_refusal(PreflightError("refused")) is True
    # A real protocol violation is still never retried -- retrying it would mask a genuine bug.
    assert _is_reconnect_refusal(protocol_fault) is False
