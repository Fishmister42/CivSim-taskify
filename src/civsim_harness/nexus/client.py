"""Nexus client (T031, T032, T159, T246): connection, handshake, request/response discipline.

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

A follow-up capture against that same real client (2026-09-20) established
something stronger: Lua state indices differ by *game phase*, not only by
client version or connection. The Create Game (setup) screen exposes an
entirely different, smaller state table -- 31 states total, built from
``HostGame``, ``MainMenu``, ``StagingRoom``, ``Lobby``, and ``Mods`` -- than
the 136-state table once a game is actually loaded. Critically, a
same-*named* state can sit at a *different* index in each table:
``LoadGameMenu`` is index 18 at the Create Game screen but index 112 once
in game; ``SaveGameMenu`` is 19 versus 113. Neither table has
``GameCore_Tuner``/``InGame`` before a game is loaded, so :meth:`connect`
still succeeds at either point -- but an index resolved in one phase is not
merely "possibly stale" in another: it is frequently still a *valid* index
into the new table, just for a completely different state, so reusing it
raises nothing at all -- the wrong Lua just runs. contracts/nexus-protocol.md
already required re-resolving indices on reconnect; this is the same rule
extended to every phase transition *within* one connection.

Two things follow from that, both implemented here rather than as a
periodic background poll or an extra per-command network round trip (a
per-operation cost on the hot path is not acceptable, and the state table
is stable *within* a phase -- see FR-014 and the "No turn-level time
bound" note on :meth:`NexusClient.execute_command`):

- :meth:`NexusClient.refresh_state_indices` is the explicit re-resolution
  step a caller (the run sequence) calls at a *known* phase boundary --
  a game finishes loading, the run returns to a menu, and so on -- the
  same "re-send LSQ:, adopt the new table" mechanism
  :meth:`resolve_game_states` already used, generalized to not require
  ``GameCore_Tuner``/``InGame`` (a phase boundary can just as easily be
  *leaving* the game as entering it).
- :meth:`NexusClient.execute_command` itself refuses to send a command
  whose ``state_index`` is not present in the *current* state table
  (``REASON_STALE_STATE_INDEX``), rather than forwarding it to the wire.
  This is a cheap, in-memory membership check -- no extra round trip --
  and it catches the common case where a phase transition made the index
  disappear outright. It cannot catch the harder case where a stale index
  happens to still be valid in the new table for a *different* state
  (nothing observable distinguishes that from a legitimate call), which is
  why re-resolving at known phase boundaries via
  :meth:`refresh_state_indices` remains the primary defence and this
  check is deliberately a backstop, not the fix.

An automatic "retry once on a failure that looks like staleness" was
considered and rejected: the wire protocol defines no signal that
distinguishes "this failed because the index is stale" from an ordinary
Lua error, so any such heuristic would either miss real staleness or mask
genuine bugs behind a silent retry -- worse than today's opaque failure,
not better.

A third live verification (2026-09-20, Windows client 1.0.12.68 -- the first
time this client's *command* path ever ran against a real game on any
platform) established the reply framing, which the pre-live implementation
had guessed wrong in three places:

- ``APP:<name>`` is *answered*: one ``TAG_HANDSHAKE`` identification frame
  (``"Civ6\\0<title>\\0<binary dir>"``) that must be consumed before the
  ``LSQ:`` reply is read (:meth:`NexusClient._handshake`).
- Unsolicited ``TAG_ASYNC_OUTPUT`` (tag -1) log frames interleave into the
  stream at any point, including between ``LSQ:`` and its reply
  (:meth:`NexusClient._read_handshake_frame` skips them).
- A command's printed output arrives on tag -1, one frame per printed line,
  each prefixed ``O\\0<StateName>: ``; the tag-3 reply is an **empty**
  acknowledgement (:meth:`NexusClient._await_result`, :func:`_strip_print_prefix`).

Evidence: specs/002-civ-playing-harness/spikes/r5-raw-windows/
(raw_protocol_transcript.txt, raw_command_transcript.txt) and
specs/002-civ-playing-harness/spikes/r5-save-path-windows.md; the Linux
spikes agree on the prefix and on draining the ``APP:`` reply
(specs/002-civ-playing-harness/spikes/r5-raw/t077_enumerate.py,
nexus_probe.py). contracts/nexus-protocol.md "Reply framing" is the
normative write-up.

A fourth live finding (2026-09-20 evening, Linux client 1.0.12.9, issue #1 --
the peer's session) established the *post-close connection-refusal tail*: the
client refuses new tuner connections for a short window (~2s) after the
previous one closes, an undocumented timing tail on the "one tuner connection
at a time" rule. :meth:`NexusClient.reconnect` rides it out with a bounded
retry-with-backoff; :meth:`connect` (and a :meth:`reconnect` before this client
has ever connected) still fails fast, so a genuinely dead client never costs
the retry budget. contracts/nexus-protocol.md "The post-close
connection-refusal tail" is the normative write-up.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from civsim_harness.errors import NexusError, PreflightError
from civsim_harness.nexus.codec import (
    TAG_ASYNC_OUTPUT,
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

#: How many times :meth:`NexusClient.reconnect` retries a *refused* connection
#: before giving up -- the bounded retry that rides out the post-close
#: connection-refusal tail (T246; contracts/nexus-protocol.md "The post-close
#: connection-refusal tail"; live finding Linux 1.0.12.9, 2026-09-20, issue
#: #1). This budget applies to a *re*connect only: :meth:`connect` and a
#: :meth:`reconnect` on a client that has never held a live connection both
#: fail fast on the first refusal (a genuinely dead client must not cost the
#: budget). See :meth:`reconnect`.
DEFAULT_RECONNECT_MAX_ATTEMPTS = 5

#: Initial backoff between reconnect retries, in seconds. Each subsequent
#: backoff doubles, capped at :data:`DEFAULT_RECONNECT_MAX_BACKOFF_S`, so the
#: default schedule (0.5, 1.0, 2.0, 2.0) sleeps ~5.5s across five attempts --
#: comfortably covering the ~2s tail the live session measured, with margin for
#: a slower host, while staying a bounded, finite budget.
DEFAULT_RECONNECT_BACKOFF_S = 0.5

#: Ceiling on any single reconnect backoff, so the doubling schedule cannot run
#: away on a large attempt count.
DEFAULT_RECONNECT_MAX_BACKOFF_S = 2.0

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
#: Raised by execute_command() when the requested state_index is not present
#: in the current state table -- i.e. it was resolved before a phase
#: transition (or a reconnect) invalidated it and never re-resolved. This is
#: the backstop half of the phase-transition fix (see the module docstring):
#: it only catches an index that has become entirely absent from the current
#: table, not one that happens to still be valid there for a different
#: state -- callers must still re-resolve at known phase boundaries via
#: refresh_state_indices() rather than rely on this check alone.
REASON_STALE_STATE_INDEX = "stale_state_index"

_logger = logging.getLogger(__name__)


def _default_telemetry_sink(text: str) -> None:
    """Fallback telemetry route: stdlib logging under this module's name.

    Deliberately does not import :mod:`civsim_harness.telemetry` -- that
    package is being written concurrently elsewhere. A caller that wants
    unmatched output routed into the harness's structured telemetry instead
    passes its own sink via ``NexusClient(on_unmatched_output=...)``.
    """
    _logger.warning("nexus: discarding unmatched output: %s", text)


def _is_reconnect_refusal(exc: Exception) -> bool:
    """Whether *exc* from :meth:`NexusClient.connect` is the post-close refusal tail's symptom.

    The tail (T246; contracts/nexus-protocol.md "The post-close connection-refusal
    tail") surfaces three ways, all meaning "the previous socket is not yet released,
    try again":

    - a TCP-level refusal -- ``ConnectionRefusedError`` (or a connect timeout),
      which :meth:`connect` raises as :class:`PreflightError`;
    - a client that accepts the socket but drops it with a clean EOF before the
      handshake completes, which surfaces as :class:`NexusError` with
      ``REASON_CONNECTION_CLOSED``; and
    - the same drop as an :class:`OSError` (``ConnectionResetError`` /
      ``ConnectionAbortedError`` -- Windows surfaces a peer-closed connection this
      way rather than as a clean EOF, exactly as ``saves/load_game.py``'s reconnect
      loop already handles).

    All are retried by :meth:`reconnect`. A handshake that fails any *other* way (a
    real protocol violation -- a malformed state list, the wrong tag) is **not** the
    tail and is never retried: retrying it would only mask a genuine bug behind a
    silent loop.
    """
    if isinstance(exc, PreflightError):
        return True
    if isinstance(exc, NexusError):
        return exc.detail.get("reason") == REASON_CONNECTION_CLOSED
    return isinstance(exc, OSError)


def _strip_print_prefix(payload: str) -> str:
    """Strip the ``O\\0<LuaStateName>: `` prefix from one async output frame.

    **Verified against a real client** (2026-09-20 Windows live session,
    specs/002-civ-playing-harness/spikes/r5-raw-windows/
    raw_command_transcript.txt; the Linux spikes strip the identical prefix
    -- specs/002-civ-playing-harness/spikes/r5-raw/t077_enumerate.py): every
    ``print()`` line arrives as its own ``TAG_ASYNC_OUTPUT`` frame whose
    payload is prefixed ``O\\0<StateName>: ``, e.g.::

        "O\\x00InGame: ---BEGIN:57a06e82...---"  ->  "---BEGIN:57a06e82...---"

    A payload without the prefix is returned unchanged -- feeding it through
    verbatim keeps any unrecognized async line visible to telemetry rather
    than silently eaten.
    """
    if payload.startswith("O\x00"):
        payload = payload[2:]
        head, sep, tail = payload.partition(": ")
        if sep and "\x00" not in head:
            return tail
        return payload
    return payload


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

    Positional and not guaranteed stable across game versions, mod sets, or
    -- verified against a real client (module docstring) -- *game phase*
    within a single connection (contracts/nexus-protocol.md "Connection
    sequence", step 5). Callers must re-resolve on every (re)connect rather
    than reusing indices from before a disconnect (T159), and *also* on
    every phase transition within one connection, via
    :meth:`NexusClient.refresh_state_indices` or
    :meth:`NexusClient.resolve_game_states` -- the same name can be a
    different index in a different phase, so an index from before a
    transition is not just potentially outdated, it may silently be valid
    for an unrelated state after one.
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
    is still missing. :meth:`refresh_state_indices` is the general form of
    that same re-resolution, for any other known phase boundary. Indices
    are invalidated by a phase transition just as much as by a reconnect
    (module docstring) -- :meth:`execute_command` refuses to send a command
    against a ``state_index`` no longer present in the current table.
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
        reconnect_max_attempts: int = DEFAULT_RECONNECT_MAX_ATTEMPTS,
        reconnect_backoff_s: float = DEFAULT_RECONNECT_BACKOFF_S,
        reconnect_max_backoff_s: float = DEFAULT_RECONNECT_MAX_BACKOFF_S,
        reconnect_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._host = host
        self._port = port
        self._app_name = app_name
        self._connect_timeout_s = connect_timeout_s
        self._command_timeout_s = command_timeout_s
        self._on_unmatched_output: TelemetrySink = on_unmatched_output or _default_telemetry_sink
        if reconnect_max_attempts < 1:
            raise ValueError("reconnect_max_attempts must be at least 1")
        self._reconnect_max_attempts = reconnect_max_attempts
        self._reconnect_backoff_s = reconnect_backoff_s
        self._reconnect_max_backoff_s = reconnect_max_backoff_s
        self._reconnect_sleep = reconnect_sleep

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._decoder = NexusFrameDecoder()
        self._correlator = SentinelCorrelator(on_unmatched=self._on_unmatched_output)
        self._pending_frames: list[NexusFrame] = []
        self._lock = asyncio.Lock()
        self._state_indices: StateIndices | None = None
        #: Whether this client has ever completed a live connection. A
        #: `reconnect()` only rides out the post-close refusal tail when this is
        #: true -- otherwise it is effectively a first connect and must fail fast
        #: on a dead client (T246). Set once, never cleared: the tail is a
        #: property of "there was a socket to release", which stays true for the
        #: rest of the client's life once it has connected once.
        self._has_connected = False

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
        self._has_connected = True
        return self._state_indices

    async def reconnect(self) -> StateIndices:
        """Reconnect discipline (T159, T246): drop any stale connection, then connect() again,
        retrying a *refused* connection to ride out the post-close refusal tail.

        Always re-runs the full handshake and re-resolves state indices.
        Indices captured before a disconnect are never reused, even if the
        new connection happens to enumerate Lua states in the same order --
        that would be an assumption about game/mod state, not a fact this
        client is entitled to rely on. As with the first connect, the
        result may not yet have game states resolved (``has_game_states``
        may be ``False``) -- call :meth:`resolve_game_states` again if the
        caller needs them.

        **The post-close connection-refusal tail (T246).** A live session
        (Linux client 1.0.12.9, 2026-09-20, issue #1) established that the
        client refuses new tuner connections for a short window (~2s) after the
        previous one closes -- an undocumented timing tail on the "one tuner
        connection at a time" rule (contracts/nexus-protocol.md). A reconnect
        that lands inside that window sees a refused connection and, without
        this retry, would report a perfectly healthy client as dead. So a
        reconnect whose connect is refused (:class:`PreflightError`) is retried,
        with a doubling backoff, up to ``reconnect_max_attempts`` -- a bounded,
        finite budget covering the measured tail with margin.

        **The retry rides on this client having connected before.** The tail
        exists only because a *previous* socket is still being released; a
        client that has never connected has no socket to release, so a refusal
        there is a genuinely-unreachable client and is not retried. This is the
        same "the first connect() of a session must fail fast" rule as
        :meth:`connect`: the retry budget is spent only where there is a real
        tail to ride out, never on a dead client's first contact.

        The tail surfaces two ways, both retried (see :func:`_is_reconnect_refusal`):
        a TCP-level refusal (:class:`PreflightError`), and a client that accepts
        the socket then drops it before the handshake completes (:class:`NexusError`
        with ``REASON_CONNECTION_CLOSED``). A handshake that fails any *other* way --
        a malformed state list, the wrong tag: a real protocol violation, not a
        refusal -- is never retried.
        """
        await self._close()
        self._state_indices = None
        if not self._has_connected:
            # No prior live connection -> no socket being released -> no tail to
            # ride out. Behave exactly like a first connect: one attempt, fail
            # fast on a refused/unreachable client (T246).
            return await self.connect()

        backoff = self._reconnect_backoff_s
        last_error: Exception | None = None
        for attempt in range(1, self._reconnect_max_attempts + 1):
            try:
                return await self.connect()
            except (PreflightError, NexusError, OSError) as exc:
                if not _is_reconnect_refusal(exc):
                    # A real protocol violation, not the refusal tail -- fail at
                    # once rather than mask a genuine bug behind a silent retry.
                    raise
                last_error = exc
                if attempt >= self._reconnect_max_attempts:
                    break
                _logger.debug(
                    "nexus: reconnect attempt %d/%d refused; backing off %.3gs to ride out the "
                    "post-close connection-refusal tail (T246)",
                    attempt,
                    self._reconnect_max_attempts,
                    backoff,
                )
                await self._reconnect_sleep(backoff)
                backoff = min(backoff * 2, self._reconnect_max_backoff_s)

        assert last_error is not None  # the loop only exits here via a refusal
        last_detail = getattr(last_error, "detail", None)
        last_reason = last_detail.get("reason") if isinstance(last_detail, dict) else None
        raise PreflightError(
            "Could not reconnect to the Nexus tuner interface within the bounded post-close "
            "retry budget; the client is not merely releasing a previous socket",
            detail={
                "host": self._host,
                "port": self._port,
                "attempts": self._reconnect_max_attempts,
                "reason": last_reason if last_reason is not None else type(last_error).__name__,
            },
        ) from last_error

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
        """``APP:`` to identify, consume its reply, then resolve the Lua states.

        **Verified against a real client** (2026-09-20 Windows live session,
        specs/002-civ-playing-harness/spikes/r5-raw-windows/
        raw_protocol_transcript.txt): the client answers ``APP:<name>`` with
        one ``TAG_HANDSHAKE`` identification frame of its own, e.g.
        ``"Civ6\\0Sid Meier's Civilization 6\\0<binary dir>"``. That reply
        MUST be read and consumed here -- an earlier implementation skipped
        straight to ``LSQ:`` and parsed the identification frame as the
        state list, so ``connect()`` failed against every real client
        ("odd number of NUL-separated fields": the greeting has three).
        The payload is treated as opaque and logged for the transcript;
        nothing downstream depends on its contents.

        Does not require ``GameCore_Tuner`` / ``InGame`` -- see
        :meth:`resolve_game_states` for the step that does.
        """
        await self._send_raw(TAG_HANDSHAKE, f"APP:{self._app_name}")
        greeting = await self._read_handshake_frame()
        _logger.debug("nexus: APP: handshake reply: %r", greeting.payload[:200])
        return await self._query_states()

    async def _query_states(self) -> StateIndices:
        """Send ``LSQ:`` and parse whatever Lua states the client currently reports.

        Shared by :meth:`_handshake` (the initial connect) and
        :meth:`resolve_game_states` (the later, explicit step): both need
        "the current state table," just at different points in the run.

        The reply is the next ``TAG_HANDSHAKE`` frame -- **not** the next
        frame of any tag. Verified against a real client (2026-09-20,
        raw_protocol_transcript.txt): the client interleaves unsolicited
        ``TAG_ASYNC_OUTPUT`` (tag -1) log frames into the same stream, e.g.
        ``O\\0StagingRoom: RefreshStatus()...`` arriving around the ``LSQ:``
        reply. An earlier implementation read exactly one frame and raised
        on any other tag, so any log line emitted in that window aborted
        the query. :meth:`_read_handshake_frame` skips those frames (routing
        print output to telemetry) instead of failing on them.
        """
        await self._send_raw(TAG_HANDSHAKE, "LSQ:")
        frame = await self._read_handshake_frame()

        by_name = _parse_state_list(frame.payload)
        return StateIndices(
            by_name=by_name,
            game_core_tuner=by_name.get("GameCore_Tuner"),
            in_game=by_name.get("InGame"),
        )

    async def _read_handshake_frame(self) -> NexusFrame:
        """Read frames until the next ``TAG_HANDSHAKE`` one, skipping async noise.

        Interleaved ``TAG_ASYNC_OUTPUT`` print/log frames are routed to the
        unmatched-output telemetry sink (prefix stripped), never raised on
        and never parsed as a handshake payload. A ``TAG_COMMAND`` frame here
        is a stale empty acknowledgement from an earlier command (the real
        client acknowledges every command with an empty tag-3 frame, which
        can still be unread when the sentinels already completed the result)
        -- skipped; a *non-empty* one would be the pre-live-verification
        framing no real client has ever exhibited, so it is logged loudly
        (which framing was seen) and still not treated as a result or a
        state list. Callers bound this loop with their own timeout
        (:meth:`connect`'s connect timeout, or the command timeout in
        :meth:`resolve_game_states` / :meth:`refresh_state_indices`).
        """
        while True:
            frame = await self._read_frame()
            if frame.tag == TAG_HANDSHAKE:
                return frame
            if frame.tag == TAG_ASYNC_OUTPUT:
                stripped = _strip_print_prefix(frame.payload)
                if stripped.strip():
                    self._on_unmatched_output(stripped)
                continue
            if frame.tag == TAG_COMMAND and not frame.payload:
                continue  # a previous command's empty acknowledgement, late
            _logger.warning(
                "nexus: skipping unexpected frame while awaiting a handshake "
                "reply (tag=%d, payload=%r) -- the live-verified protocol "
                "delivers print output on tag -1 and only empty "
                "acknowledgements on tag 3",
                frame.tag,
                frame.payload[:200],
            )

    def _require_connected(self) -> None:
        if self._writer is None or self._reader is None:
            raise NexusError(
                "Nexus client is not connected", detail={"reason": REASON_NOT_CONNECTED}
            )

    async def resolve_game_states(self) -> StateIndices:
        """Require ``GameCore_Tuner`` and ``InGame`` to be present, right now.

        Call this once a game is loaded -- :meth:`connect` deliberately does
        not require these two states, since (verified against a real
        client) they do not exist in the state table at the main menu, nor
        at the Create Game screen. Sequencing "connect" and "the game is
        actually loaded" as two distinct steps lets a caller like ``doctor``
        report "tuner reachable, no game loaded" as a normal, useful
        diagnostic instead of connect() failing with a confusing preflight
        error.

        Re-sends ``LSQ:`` (not the full handshake -- ``APP:`` identifies the
        session once, at :meth:`connect`) so this reflects the state table
        as it is *now*, and updates :attr:`state_indices` **only on
        success** -- a failed attempt (missing state(s)) leaves the last
        resolved :attr:`state_indices` untouched rather than clobbering it
        with an incomplete table, so a caller that races this against a
        transient loading screen cannot lose a last-known-good table it
        already had.

        Raises :class:`PreflightError` naming exactly which required
        state(s) are still missing if either is absent -- this is an
        expected, reportable condition (no game loaded yet), not a broken
        handshake.

        This is the "entering a game" special case of the more general
        :meth:`refresh_state_indices` -- call that one instead at a phase
        boundary that is not specifically "a game just loaded" (e.g.
        returning to a menu), since it does not require these two states.
        """
        self._require_connected()

        async with self._lock:
            indices = await self._bounded_query_states()

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

    async def refresh_state_indices(self) -> StateIndices:
        """Re-resolve whatever Lua states currently exist and adopt them unconditionally.

        Call this at every *known* phase boundary within a single
        connection -- not on a schedule and not before every command (see
        the module docstring for why a periodic poll or a per-operation
        network round trip is rejected here). "Known phase boundary" means
        a point the run sequence itself recognises: a game finishes
        loading, the run returns to a menu, a save/load submenu is entered
        or left, and so on.

        Verified against a real client (module docstring, 2026-09-20
        capture): the same *name* can sit at a different index
        in each phase's table (``LoadGameMenu``/``SaveGameMenu`` are 18/19
        at the Create Game screen but 112/113 once in game), so an index
        resolved before a phase transition cannot be assumed valid --
        or, worse, may silently be valid for something else -- after one.

        Unlike :meth:`resolve_game_states`, this does not require
        ``GameCore_Tuner``/``InGame`` to be present and always replaces
        :attr:`state_indices` with whatever the fresh ``LSQ:`` reports,
        whether that is more states, fewer, or none of the game-play ones
        -- a phase boundary can just as easily be *leaving* the game as
        entering it. Re-sends ``LSQ:`` only, not the full handshake.
        """
        self._require_connected()

        async with self._lock:
            indices = await self._bounded_query_states()

        self._state_indices = indices
        return indices

    async def _bounded_query_states(self) -> StateIndices:
        """One ``LSQ:`` round-trip under the per-command timeout.

        :meth:`_query_states` now skips interleaved async frames while
        waiting for the ``TAG_HANDSHAKE`` reply (see
        :meth:`_read_handshake_frame`), so a client that keeps emitting log
        frames but never answers ``LSQ:`` must be bounded here rather than
        looping forever. :meth:`connect` already bounds its handshake with
        the connect timeout; this is the equivalent bound for the later
        re-resolution calls.
        """
        try:
            return await asyncio.wait_for(self._query_states(), timeout=self._command_timeout_s)
        except TimeoutError as exc:
            raise NexusError(
                "Nexus LSQ: state query exceeded the per-operation timeout",
                detail={"reason": REASON_TIMEOUT, "timeout_s": self._command_timeout_s},
            ) from exc

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

        Before sending anything, *state_index* is checked -- a cheap,
        in-memory membership test, not a network round trip -- against the
        state table :attr:`state_indices` currently holds. If it is not
        present there, this raises :class:`NexusError` with
        ``REASON_STALE_STATE_INDEX`` instead of forwarding a command that
        might silently execute against whatever now occupies that slot
        (module docstring: indices are invalidated by a phase transition,
        not only a reconnect). This only catches an index that has become
        entirely absent from the current table; it cannot catch one that
        happens to still be valid there for a *different* state, which is
        why a caller must still re-resolve at known phase boundaries via
        :meth:`refresh_state_indices` or :meth:`resolve_game_states`
        rather than rely on this check alone.
        """
        self._require_connected()

        current_indices = self._state_indices
        if current_indices is not None and state_index not in current_indices.by_name.values():
            raise NexusError(
                "Nexus command targets a Lua state index that is not in the "
                "current state table -- it was likely resolved before a "
                "phase transition (or reconnect) invalidated it; call "
                "refresh_state_indices() or resolve_game_states() again "
                "rather than reusing an index resolved earlier",
                detail={
                    "reason": REASON_STALE_STATE_INDEX,
                    "state_index": state_index,
                    "known_indices": sorted(set(current_indices.by_name.values())),
                },
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
        """Collect *nonce*'s sentinel-bracketed result from the async output stream.

        **Verified against a real client** (2026-09-20 Windows live session,
        specs/002-civ-playing-harness/spikes/r5-raw-windows/
        raw_command_transcript.txt): a command's printed output arrives as
        ``TAG_ASYNC_OUTPUT`` (tag -1) frames, one per printed line, each
        prefixed ``O\\0<StateName>: `` -- and the ``TAG_COMMAND`` (tag 3)
        reply is an **empty acknowledgement** carrying no output at all. An
        earlier implementation fed only tag-3 payloads to the correlator, so
        against a real client every command timed out while its Lua ran to
        completion (the ``Network.LoadGame`` that "timed out" had loaded the
        game). Only tag -1 frames are fed to the correlator, prefix
        stripped, one line each; a *non-empty* tag-3 payload would be that
        never-observed pre-live framing, so it is logged loudly (which
        framing was seen) and routed to telemetry rather than silently
        accepted as a result -- its command then times out visibly instead
        of a wrong framing being masked.
        """
        while True:
            result = self._correlator.take_result(nonce)
            if result is not None:
                return result
            frame = await self._read_frame()
            if frame.tag == TAG_ASYNC_OUTPUT:
                self._correlator.feed(_strip_print_prefix(frame.payload) + "\n")
            elif frame.tag == TAG_COMMAND and frame.payload:
                _logger.warning(
                    "nexus: discarding non-empty TAG_COMMAND payload -- the "
                    "live-verified protocol delivers results on tag -1 and "
                    "an empty tag-3 acknowledgement only (payload=%r)",
                    frame.payload[:200],
                )
                self._on_unmatched_output(frame.payload)
            # An empty TAG_COMMAND frame is the client's bare acknowledgement
            # (it usually arrives after the sentinels); frames of any other
            # tag are not print output. Neither is fed to the correlator.

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
