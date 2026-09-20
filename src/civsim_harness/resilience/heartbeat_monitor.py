"""Heartbeat monitoring (T148), built on the T034 probe in `nexus/heartbeat.py`.

research R12's second signal: a periodic nonce round-trip through
`GameCore_Tuner`, bounded by its own short timeout, catching a client that
is alive (process liveness sees it) but stuck servicing real work -- a
hang, a deadlock, a dropped tuner connection. Each call to
:meth:`HeartbeatMonitor.check` is one independent probe with its own
bound; nothing here accumulates elapsed time across probes or across a
turn (research R12, FR-014) -- a turn that has been running for hours is
simply a turn whose heartbeat has round-tripped successfully, hundreds or
thousands of times.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from civsim_harness.errors import NexusError
from civsim_harness.nexus.client import REASON_TIMEOUT, NexusClient
from civsim_harness.nexus.heartbeat import DEFAULT_HEARTBEAT_TIMEOUT_S, probe_heartbeat


class HeartbeatOutcome(StrEnum):
    """The three distinguishable shapes one heartbeat check can end in.

    ``HANG`` is the `hang_detected` signal proper: the round-trip did not
    complete within its bound. ``CONNECTION_LOST`` is deliberately kept
    distinct -- a dropped socket is a different failure shape (closer to
    process liveness / reconnect discipline than to a hang) and must not be
    reported as the same event type research R12 reserves for a stuck-but-
    connected client.
    """

    OK = "ok"
    HANG = "hang"
    CONNECTION_LOST = "connection_lost"


@dataclass(frozen=True)
class HeartbeatMonitor:
    """Binds `probe_heartbeat` to one `NexusClient` and bound for repeated checks."""

    client: NexusClient
    timeout_s: float = DEFAULT_HEARTBEAT_TIMEOUT_S

    async def check(self) -> HeartbeatOutcome:
        """Round-trip one nonce within :attr:`timeout_s` and classify the result.

        A plain timeout is ``HANG`` -- research R12's hang/deadlock/dropped-
        tuner-connection shape. Any other `NexusError` (e.g. the socket
        itself has closed) is reported as ``CONNECTION_LOST`` rather than
        re-raised, so a caller polling this on a schedule does not need its
        own `NexusError` handling to keep polling -- it only needs to react
        to the outcome.
        """
        try:
            ok = await probe_heartbeat(self.client, timeout_s=self.timeout_s)
        except NexusError as exc:
            if exc.detail.get("reason") == REASON_TIMEOUT:
                # probe_heartbeat() already turns a timeout into `False`, not
                # a raised NexusError -- but guard the classification anyway
                # in case that contract ever changes underneath this module.
                return HeartbeatOutcome.HANG
            return HeartbeatOutcome.CONNECTION_LOST
        return HeartbeatOutcome.OK if ok else HeartbeatOutcome.HANG
