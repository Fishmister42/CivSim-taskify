"""Nexus sentinel discipline (T032, T033): nonce wrapping and result extraction.

The tuner protocol has no native return values (contracts/nexus-protocol.md
"Request/response discipline") -- everything a command returns comes back as
``print`` output, asynchronously, and fragmented across arbitrary packet
boundaries. This module is the boundary that turns that stream back into
"the result for this one request, and nothing else":

- :func:`wrap_lua` wraps a declared Lua body so its *printed* output (not the
  Lua source) is bracketed by a per-request nonce's ``---BEGIN:<nonce>---``
  / ``---END:<nonce>---`` sentinels.
- :data:`LUA_JSON_PRELUDE` / :func:`lua_print_json` are the *write* side of
  that same discipline: the tuner has no native return channel, so a Lua body
  that ends in ``return { ... }`` produces **no result at all** -- the table
  goes nowhere and nothing is printed between the sentinels, so
  :meth:`~civsim_harness.nexus.client.NexusClient.execute_command` sees an
  empty body and fails its ``json.loads``. Every Lua body that wants to return
  something must ``print`` it as JSON. The ``lua/**/*.lua`` files do this with
  their own hand-rolled ``CivSim_JsonEncode`` (and the T206 capability
  executor appends the ``print`` call for them); the small Lua bodies embedded
  directly in Python modules -- the save call, the leader-selection write and
  read-back, the version probe -- use these two helpers instead of carrying a
  fourth copy of an encoder.
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


# --------------------------------------------------------------------------
# Emitting a result: the write side of the sentinel discipline
# --------------------------------------------------------------------------

#: A self-contained JSON encoder for a Lua body embedded in a Python module.
#:
#: Semantically identical to the ``CivSim_JsonEncode`` every ``lua/**/*.lua``
#: file carries -- same control-character escaping, same array-vs-object
#: decision, and the same "an empty table encodes as ``[]``" convention -- so
#: a value round-trips the same way whichever path dispatched it. It exists
#: separately only because those files are self-contained by design (the
#: sandbox has no ``require``), while the bodies embedded in Python have no
#: file of their own to carry a copy in; this is the one shared copy for them
#: rather than a fourth hand-rolled one per call site.
#:
#: ``string.format('%q', ...)`` is deliberately not used anywhere here: Lua's
#: ``%q`` escapes a newline as a backslash followed by a *literal* newline,
#: which is valid Lua source but not valid JSON.
LUA_JSON_PRELUDE = (
    "local function civsim_json_value(v) "
    'local t = type(v); '
    'if v == nil then return "null" end '
    'if t == "boolean" then return tostring(v) end '
    'if t == "number" then if v ~= v then return "null" end return tostring(v) end '
    'if t == "table" then '
    "local n = 0; for _ in pairs(v) do n = n + 1 end; "
    'if n == 0 then return "[]" end '
    "local isArray = true; "
    "for i = 1, n do if v[i] == nil then isArray = false break end end "
    "local parts = {}; "
    "if isArray then "
    "for i = 1, n do parts[i] = civsim_json_value(v[i]) end "
    'return "[" .. table.concat(parts, ",") .. "]" '
    "else "
    "for k, item in pairs(v) do "
    'parts[#parts + 1] = civsim_json_value(tostring(k)) .. ":" .. civsim_json_value(item) end '
    'return "{" .. table.concat(parts, ",") .. "}" end end '
    "local s = tostring(v); "
    "s = s:gsub('[%c\"\\\\]', function(c) "
    "if c == '\"' then return '\\\\\"' "
    "elseif c == '\\\\' then return '\\\\\\\\' "
    "elseif c == '\\n' then return '\\\\n' "
    "elseif c == '\\r' then return '\\\\r' "
    "elseif c == '\\t' then return '\\\\t' "
    "else return string.format('\\\\u%04x', string.byte(c)) end end); "
    "return '\"' .. s .. '\"' end; "
)


def lua_print_json(fields: dict[str, str]) -> str:
    """One Lua statement printing *fields* as a JSON object.

    Each value is a Lua **expression** (evaluated in the body's own scope),
    not a literal -- e.g. ``{"issued": "ok", "error": "err"}`` emits
    ``print("{" .. '"issued":' .. civsim_json_value(ok) .. ...)``. Requires
    :data:`LUA_JSON_PRELUDE` to have been emitted earlier in the same body.

    Key order is the caller's insertion order, so a body's printed shape is
    deterministic and readable in a transcript rather than dict-order
    dependent.
    """
    parts = [
        f"'{_json_key(name)}:' .. civsim_json_value({expression})"
        for name, expression in fields.items()
    ]
    joined = " .. ',' .. ".join(parts) if parts else "''"
    return f"print('{{' .. {joined} .. '}}')"


def _json_key(name: str) -> str:
    """A JSON object key, quoted for splicing into a single-quoted Lua string
    literal. Names here are always plain identifiers chosen by this codebase,
    never game-supplied, so a quote or backslash in one would be a bug in the
    caller rather than untrusted input -- rejected outright instead of
    escaped, so it can never silently produce a malformed document."""
    if '"' in name or "\\" in name or "'" in name:
        raise ValueError(f"JSON key {name!r} must not contain quotes or backslashes")
    return f'"{name}"'


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
