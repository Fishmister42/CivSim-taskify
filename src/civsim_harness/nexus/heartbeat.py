"""Nexus heartbeat probe (T034): a periodic, bounded nonce round-trip through GameCore_Tuner.

Detects the hang shape research R12 names: a game that is alive and will
still execute trivial Lua (so process liveness and the socket both look
fine) but is stuck servicing real work -- e.g. a modal the harness cannot
see past. This module owns only the single probe; aggregating it with the
other detection signals (process liveness, per-operation bounds,
screen-identity) into one 60s-budget verdict belongs to
``resilience/heartbeat_monitor.py`` (T148) and ``resilience/detector.py``
(T150), not here.
"""

from __future__ import annotations

from civsim_harness.errors import NexusError
from civsim_harness.nexus.client import REASON_TIMEOUT, NexusClient

#: Default bound for one heartbeat round-trip. Deliberately short relative to
#: a general command timeout: the whole point of the heartbeat is to be a
#: fast, cheap probe (research R12), not a stand-in for a real command.
DEFAULT_HEARTBEAT_TIMEOUT_S = 10.0

# Lua's `print(true)` emits the text "true", which is also a valid JSON
# literal -- so the heartbeat can be an ordinary execute_command() round-trip
# with no special-cased "no result expected" path through the client.
_HEARTBEAT_LUA_BODY = "print(true)"


async def probe_heartbeat(
    client: NexusClient, *, timeout_s: float = DEFAULT_HEARTBEAT_TIMEOUT_S
) -> bool:
    """Round-trip one nonce through ``GameCore_Tuner`` within *timeout_s*.

    Returns ``True`` on a successful round-trip, ``False`` if it timed out
    (the ``hang_detected`` signal -- the caller is responsible for recording
    that as a run event; this function only measures). Any other Nexus
    failure (e.g. the socket has dropped) propagates as :class:`NexusError`
    so a hang is never conflated with a dead connection.
    """
    indices = client.state_indices
    if indices is None:
        raise NexusError(
            "Cannot probe the heartbeat before the Nexus handshake has resolved state indices"
        )
    game_core_tuner = indices.game_core_tuner
    if game_core_tuner is None:
        # GameCore_Tuner does not exist in the state table until a game is
        # loaded (verified against a real client -- see nexus/client.py);
        # a heartbeat probe before that point has nothing to round-trip
        # through, distinct from "never connected" above.
        raise NexusError(
            "Cannot probe the heartbeat before GameCore_Tuner is resolved -- is a game loaded?"
        )

    try:
        result = await client.execute_command(
            state_index=game_core_tuner,
            lua_body=_HEARTBEAT_LUA_BODY,
            timeout_s=timeout_s,
        )
    except NexusError as exc:
        if exc.detail.get("reason") == REASON_TIMEOUT:
            return False
        raise
    return result is True
