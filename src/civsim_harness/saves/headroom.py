"""Disk headroom guard (T081, V11, research R17).

``min_free_disk_gb`` is a precondition, not a cleanup trigger
(contracts/run-configuration.md): checked once at preflight against the
run's *estimated* save + capture footprint (V11), and again **before every
quicksave** for the floor alone. Below the floor the run halts in a recorded
state with a ``disk_headroom_low`` event -- **nothing is deleted to make
room**, because no save is eligible for removal until its run is archived
(R17, invariant I17). This module contains no deletion, thinning, or TTL
path, and none should be added here: that is precisely the rule R17 exists
to enforce.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from civsim_harness.errors import DiskHeadroomError
from civsim_harness.host.port import HostPlatform
from civsim_harness.models.common import EventId, RunId, Timestamp
from civsim_harness.models.config import RunConfiguration, TurnReachedStopCondition
from civsim_harness.models.records import RunEvent, RunEventType

_BYTES_PER_GB = 1024**3

# Conservative, documented placeholders (UNVERIFIED: real .Civ6Save and
# capture sizes vary with map size, turn, and screen resolution; this repo
# has no live measurement to calibrate against). Deliberately sized to
# over-, not under-, estimate -- underestimating footprint is exactly the
# failure V11 exists to catch. Callers with better measurements should pass
# their own values rather than rely on these.
DEFAULT_BYTES_PER_SAVE = 50 * 1024 * 1024
DEFAULT_BYTES_PER_CAPTURE = 2 * 1024 * 1024
DEFAULT_STEPS_PER_TURN_ESTIMATE = 20
#: Used only when the configured stop condition has no inherent turn bound
#: (``game_outcome`` / ``operator_stop``) -- research R17's own example of a
#: "full game" footprint.
DEFAULT_TURNS_FOR_OPEN_ENDED_STOP = 300


@dataclass(frozen=True)
class FootprintEstimate:
    """The estimated save + capture footprint of a run, in bytes."""

    estimated_turns: int
    estimated_saves_bytes: int
    estimated_captures_bytes: int

    @property
    def total_bytes(self) -> int:
        return self.estimated_saves_bytes + self.estimated_captures_bytes


def estimate_footprint(
    config: RunConfiguration,
    *,
    bytes_per_save: int = DEFAULT_BYTES_PER_SAVE,
    bytes_per_capture: int = DEFAULT_BYTES_PER_CAPTURE,
    steps_per_turn_estimate: int = DEFAULT_STEPS_PER_TURN_ESTIMATE,
    default_turns_for_open_ended_stop: int = DEFAULT_TURNS_FOR_OPEN_ENDED_STOP,
) -> FootprintEstimate:
    """Estimate *config*'s save + capture footprint from its stop condition
    (V11, R17).

    A ``turn_reached`` stop bounds the estimate exactly; ``game_outcome`` and
    ``operator_stop`` have no such bound, so a documented conservative
    default (a full game's worth of turns) is used instead of pretending the
    footprint is knowable in advance.
    """
    stop = config.stop_condition
    if isinstance(stop, TurnReachedStopCondition):
        turns = stop.turn
    else:
        turns = default_turns_for_open_ended_stop
    saves_bytes = turns * bytes_per_save
    captures_bytes = turns * steps_per_turn_estimate * bytes_per_capture
    return FootprintEstimate(
        estimated_turns=turns,
        estimated_saves_bytes=saves_bytes,
        estimated_captures_bytes=captures_bytes,
    )


def check_headroom(
    *,
    host: HostPlatform,
    path: Path,
    min_free_disk_gb: float,
    footprint: FootprintEstimate | None = None,
) -> None:
    """Raise :class:`~civsim_harness.errors.DiskHeadroomError` when free space
    at *path* is below *min_free_disk_gb* (the floor checked before every
    quicksave), or, when *footprint* is supplied (the preflight call, V11),
    when the estimated run footprint would not fit within the free space
    either.

    Never deletes anything and never returns a value describing "how much
    was freed" -- there is no such path. Callers halt the run in a recorded
    state on this raise; a save becomes eligible for removal only through an
    explicit archive action (R17, invariant I17), never through this check.
    """
    disk_space = host.free_disk_space(path)
    floor_bytes = int(min_free_disk_gb * _BYTES_PER_GB)

    if disk_space.free_bytes < floor_bytes:
        raise DiskHeadroomError(
            "free disk space is below the configured floor (min_free_disk_gb, V11, R17)",
            detail={
                "path": str(path),
                "free_bytes": disk_space.free_bytes,
                "floor_bytes": floor_bytes,
                "min_free_disk_gb": min_free_disk_gb,
            },
        )

    if footprint is not None and disk_space.free_bytes < footprint.total_bytes:
        raise DiskHeadroomError(
            "free disk space cannot fit the run's estimated save/capture footprint "
            "(V11, R17)",
            detail={
                "path": str(path),
                "free_bytes": disk_space.free_bytes,
                "estimated_footprint_bytes": footprint.total_bytes,
                "estimated_turns": footprint.estimated_turns,
            },
        )


def build_disk_headroom_event(
    *,
    run_id: RunId,
    occurred_at: Timestamp,
    detail: dict[str, object],
    turn_number: int | None = None,
) -> RunEvent:
    """Build the ``disk_headroom_low`` ``RunEvent`` for a headroom halt
    (data-model.md §14: "the warning before the halt ... without it the halt
    looks arbitrary").

    Mirrors ``run.lifecycle.transition``'s own pattern: this only builds the
    record, it does not write it -- the caller (a later wave's run
    orchestrator) writes it through the store alongside whatever lifecycle
    transition accompanies the halt.
    """
    return RunEvent(
        event_id=EventId(uuid.uuid4().hex),
        run_id=run_id,
        turn_number=turn_number,
        step_index=None,
        event_type=RunEventType.DISK_HEADROOM_LOW,
        occurred_at=occurred_at,
        detail=dict(detail),
    )
