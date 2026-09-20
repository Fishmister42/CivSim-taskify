"""Crash/hang detection, recovery, bounded retry, degradation marking.

Four independent detection signals (research R12, `resilience.detector`),
each scoped to a single check or a single operation -- never to a turn
(FR-014): process liveness (`resilience.liveness`), the tuner heartbeat
(`resilience.heartbeat_monitor`), per-operation bounds
(`resilience.operation_bounds`), and the screen-identity probe (declared
elsewhere, consumed here only through a protocol). `resilience.recovery`
implements the FR-045 - FR-048 response once one of those signals -- or a
mid-turn `ObservationAssemblyError` -- trips: abandon the interrupted
attempt, resume from that turn's own start quicksave as the same
continuous run, and stop in a recorded `failed` state after
`recovery_attempt_limit` consecutive failures rather than retrying
indefinitely.
"""

from __future__ import annotations

from civsim_harness.resilience.detector import (
    DetectionAggregator,
    ScreenIdentityProbe,
    check_heartbeat,
    check_operation_bound,
    check_process_liveness,
    check_screen_identity,
    make_event,
)
from civsim_harness.resilience.heartbeat_monitor import HeartbeatMonitor, HeartbeatOutcome
from civsim_harness.resilience.liveness import ProcessLivenessMonitor, is_process_alive
from civsim_harness.resilience.operation_bounds import (
    DEFAULT_OPERATION_BOUNDS_S,
    OperationKind,
    OperationTimedOut,
    run_bounded,
)
from civsim_harness.resilience.recovery import RecoveryEngine, RecoveryResult, SaveLoader

__all__ = [
    "DEFAULT_OPERATION_BOUNDS_S",
    "DetectionAggregator",
    "HeartbeatMonitor",
    "HeartbeatOutcome",
    "OperationKind",
    "OperationTimedOut",
    "ProcessLivenessMonitor",
    "RecoveryEngine",
    "RecoveryResult",
    "SaveLoader",
    "ScreenIdentityProbe",
    "check_heartbeat",
    "check_operation_bound",
    "check_process_liveness",
    "check_screen_identity",
    "is_process_alive",
    "make_event",
    "run_bounded",
]
