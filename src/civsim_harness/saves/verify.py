"""Filesystem save verification (T079, FR-007, SC-004, research R5, R19).

The turn-start quicksave is only real once the filesystem says so. The
``.Civ6Save`` must appear in the platform's save directory -- resolved
through the ``HostPlatform`` port, **never a hard-coded path**, since the
directory differs by platform and Aspyr has relocated it across updates
before (R5) -- with its size stable across two reads before the turn
proceeds. An unverifiable quicksave **fails the turn** rather than being
recorded as taken (FR-007): there is no path here that returns a partial or
"probably fine" result.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from civsim_harness.errors import PreflightError
from civsim_harness.host.port import HostPlatform

SAVE_FILE_SUFFIX = ".Civ6Save"

#: How long to wait between the two stability-check reads by default. Kept
#: short for the common case (a save that finished writing already) while
#: still catching a save still being flushed to disk.
DEFAULT_STABILITY_WAIT_S = 0.5

#: How long to wait for the ``.Civ6Save`` to APPEAR before the stability reads begin, and how
#: often to look. MEASURED (2026-09-21, Linux 1.0.12.9, the first real run through the
#: composition root): ``Network.SaveGame`` returns before the file lands. The turn-2 quicksave
#: was reported "expected .Civ6Save not found" and was on disk -- 1,009,647 bytes -- within the
#: same second; the preparation-time save had a wider gap before its check and passed by
#: accident. A bounded wait for appearance is not a relaxation of FR-007: the file must still
#: exist, and still be size-stable across two reads, before the turn proceeds -- the game is
#: simply given the seconds it demonstrably needs to write it.
DEFAULT_APPEARANCE_TIMEOUT_S = 10.0
DEFAULT_APPEARANCE_POLL_S = 0.25


class SaveVerificationError(PreflightError):
    """A quicksave could not be verified on the filesystem; the turn must fail
    rather than be recorded as taken (FR-007, SC-004).
    """


@dataclass(frozen=True)
class VerifiedSave:
    """A ``.Civ6Save`` confirmed present with a size stable across two reads."""

    path: Path
    size_bytes: int


def resolve_saves_dir(host: HostPlatform, *, home: Path | None = None) -> Path:
    """The platform's save directory, resolved through the ``HostPlatform``
    port -- never hard-coded (R5, R19). *home* exists purely for
    deterministic testing, mirroring ``HostPlatform.resolve_game_directories``
    itself.

    The returned path is **not** required to already exist (T077 spike
    finding: on a fresh install, the Linux save directory did not exist
    until the first save created it) -- this is pure path resolution, with
    no ``is_dir()``/``exists()`` check here or in any ``HostPlatform``
    adapter's ``resolve_game_directories``. ``verify_save`` below is the
    only place a missing directory has any consequence, and there it is
    indistinguishable from -- and handled identically to -- a missing file:
    both simply fail to verify.
    """
    return host.resolve_game_directories(home=home).saves_dir


def verify_save(
    host: HostPlatform,
    save_name: str,
    *,
    home: Path | None = None,
    stability_wait_s: float = DEFAULT_STABILITY_WAIT_S,
    appearance_timeout_s: float = DEFAULT_APPEARANCE_TIMEOUT_S,
    appearance_poll_s: float = DEFAULT_APPEARANCE_POLL_S,
    sleep: Callable[[float], None] = time.sleep,
) -> VerifiedSave:
    """Confirm *save_name* (no extension) exists in the resolved save
    directory with a size stable across two reads, before the turn proceeds
    (research R5).

    The file is given *appearance_timeout_s* to appear (polled every
    *appearance_poll_s*; the client writes it asynchronously after
    ``Network.SaveGame`` returns -- see the module constants), then must be
    size-stable across two reads *stability_wait_s* apart. The poll count is
    derived from the two durations rather than from a clock, so an injected
    no-op *sleep* still terminates.

    *sleep* is injectable so tests can verify the two-read discipline without
    actually waiting *stability_wait_s* seconds.

    Raises :class:`SaveVerificationError` -- never returns a partial or
    "probably fine" result -- when the file never appears, disappears between
    reads, or its size has not stabilised: an unverifiable quicksave fails
    the turn rather than being recorded as taken (FR-007).
    """
    saves_dir = resolve_saves_dir(host, home=home)
    path = saves_dir / f"{save_name}{SAVE_FILE_SUFFIX}"

    polls = max(1, int(appearance_timeout_s / appearance_poll_s)) if appearance_poll_s > 0 else 1
    for _ in range(polls):
        if path.is_file():
            break
        sleep(appearance_poll_s)
    if not path.is_file():
        raise SaveVerificationError(
            "expected .Civ6Save not found in the resolved save directory",
            detail={"path": str(path), "waited_s": appearance_timeout_s},
        )
    # A single pair of reads makes "the game is still writing" indistinguishable
    # from "this save will never settle", and the first is the common case: a
    # large .Civ6Save on a busy disk is routinely still growing 0.5 s after
    # `Network.SaveGame` returns. Raising there paused a live run at game turn 3
    # on 2026-09-22 -- play stopped for ten minutes because a file was mid-write.
    #
    # So resample until two CONSECUTIVE reads agree. The budget is derived from
    # the two durations already reviewed above rather than from a new constant,
    # and the loop is bounded by a COUNT rather than a clock, so an injected
    # no-op `sleep` still terminates -- the same discipline as the appearance
    # poll. Fail-closed is preserved exactly: a save that never settles inside
    # the budget still raises, and the error now says how many times it looked.
    stability_attempts = (
        max(1, int(appearance_timeout_s / stability_wait_s)) if stability_wait_s > 0 else 1
    )

    previous_size = path.stat().st_size
    for _ in range(stability_attempts):
        sleep(stability_wait_s)

        if not path.is_file():
            raise SaveVerificationError(
                "expected .Civ6Save disappeared between the stability-check reads",
                detail={"path": str(path)},
            )
        current_size = path.stat().st_size

        if current_size == previous_size:
            return VerifiedSave(path=path, size_bytes=current_size)
        previous_size = current_size

    raise SaveVerificationError(
        "the .Civ6Save's size never stabilised across consecutive reads; the game "
        "may still be writing it",
        detail={
            "path": str(path),
            "last_size": previous_size,
            "attempts": stability_attempts,
            "waited_s": stability_attempts * stability_wait_s,
        },
    )
