"""Nexus sentinel discipline (T032, T033): nonce wrapping and result extraction.

The tuner protocol has no native return values (contracts/nexus-protocol.md
"Request/response discipline") -- everything a command returns comes back as
``print`` output, asynchronously, and fragmented across arbitrary packet
boundaries. This module is the boundary that turns that stream back into
"the result for this one request, and nothing else":

- :func:`wrap_lua` wraps a declared Lua body so its *printed* output (not the
  Lua source) is bracketed by a per-request nonce's ``---BEGIN:<nonce>---``
  / ``---END:<nonce>---`` sentinels.
- :class:`SentinelCorrelator` consumes arriving text and extracts the JSON
  body between a matched BEGIN/END pair for one nonce at a time. Everything
  outside a matched pair for the nonce currently being awaited -- stray
  engine prints, a previous request's late-arriving tail, a pair carrying a
  different nonce entirely -- is routed to a caller-supplied telemetry sink
  and is never returned as a result. This is what stops the client from
  ever handing back another request's answer.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable

#: A sink for text the correlator has decided is not this request's result.
#: The client wires this to out-of-game telemetry; kept as a plain callable
#: here (rather than importing civsim_harness.telemetry) so this module has
#: no dependency on that package's still-changing internals.
TelemetrySink = Callable[[str], None]

_logger = logging.getLogger(__name__)


def generate_nonce() -> str:
    """A per-request correlation id. Collision odds are astronomically low."""
    return uuid.uuid4().hex


def begin_marker(nonce: str) -> str:
    """The exact text printed to open *nonce*'s bracketed output."""
    return f"---BEGIN:{nonce}---"


def end_marker(nonce: str) -> str:
    """The exact text printed to close *nonce*'s bracketed output."""
    return f"---END:{nonce}---"


def wrap_lua(nonce: str, lua_body: str) -> str:
    """Wrap *lua_body* so its printed output is bracketed by *nonce*'s sentinels.

    Joined with newlines rather than concatenated on one line: a ``lua_body``
    containing a ``--`` line comment would otherwise silently swallow the
    trailing END sentinel's ``print`` call, since Lua comments run to the end
    of the line.
    """
    return "\n".join(
        [
            f'print("{begin_marker(nonce)}")',
            lua_body,
            f'print("{end_marker(nonce)}")',
        ]
    )


def _default_sink(text: str) -> None:
    _logger.warning("nexus: discarding unmatched output: %s", text)


class SentinelCorrelator:
    """Extracts one command's result from a stream of interleaved game output.

    Not socket-aware: callers (:mod:`civsim_harness.nexus.client`) feed it
    text as frames arrive and ask for a nonce's result once they think it
    might be complete. This split is what makes the sentinel-matching logic
    unit-testable against synthetic strings without a real connection.
    """

    def __init__(self, *, on_unmatched: TelemetrySink | None = None) -> None:
        self._buffer = ""
        self._on_unmatched: TelemetrySink = on_unmatched or _default_sink

    def feed(self, text: str) -> None:
        """Append newly arrived output text to the pending stream."""
        self._buffer += text

    def take_result(self, nonce: str) -> str | None:
        """Return the JSON body between *nonce*'s matched BEGIN/END pair, if complete.

        Anything sitting before the matched BEGIN -- stray prints, a
        previous request's trailing output, a pair carrying a different
        nonce -- is routed to the telemetry sink and discarded. Returns
        ``None`` if *nonce*'s pair has not fully arrived yet; the caller
        should feed more and retry, subject to its own timeout.
        """
        begin = begin_marker(nonce)
        end = end_marker(nonce)

        begin_index = self._buffer.find(begin)
        if begin_index == -1:
            return None

        end_index = self._buffer.find(end, begin_index + len(begin))
        if end_index == -1:
            return None

        if begin_index > 0:
            self._discard(self._buffer[:begin_index])

        result = self._buffer[begin_index + len(begin) : end_index].strip()
        self._buffer = self._buffer[end_index + len(end) :]
        return result

    def discard_unmatched(self) -> None:
        """Flush whatever is buffered without ever matching a nonce.

        Called by the client when a command's per-operation timeout has
        elapsed (contracts/nexus-protocol.md "Output arrives with no
        matching nonce -> discarded to telemetry"): whatever is sitting in
        the buffer belongs to no live request any more and must never leak
        into a later ``take_result`` call for a different nonce.
        """
        if self._buffer:
            self._discard(self._buffer)
            self._buffer = ""

    def _discard(self, text: str) -> None:
        stripped = text.strip()
        if stripped:
            self._on_unmatched(stripped)
