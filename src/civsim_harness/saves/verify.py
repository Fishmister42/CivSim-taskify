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
    """
    return host.resolve_game_directories(home=home).saves_dir


def verify_save(
    host: HostPlatform,
    save_name: str,
    *,
    home: Path | None = None,
    stability_wait_s: float = DEFAULT_STABILITY_WAIT_S,
    sleep: Callable[[float], None] = time.sleep,
) -> VerifiedSave:
    """Confirm *save_name* (no extension) exists in the resolved save
    directory with a size stable across two reads, before the turn proceeds
    (research R5).

    *sleep* is injectable so tests can verify the two-read discipline without
    actually waiting *stability_wait_s* seconds.

    Raises :class:`SaveVerificationError` -- never returns a partial or
    "probably fine" result -- when the file is missing, disappears between
    reads, or its size has not stabilised: an unverifiable quicksave fails
    the turn rather than being recorded as taken (FR-007).
    """
    saves_dir = resolve_saves_dir(host, home=home)
    path = saves_dir / f"{save_name}{SAVE_FILE_SUFFIX}"

    if not path.is_file():
        raise SaveVerificationError(
            "expected .Civ6Save not found in the resolved save directory",
            detail={"path": str(path)},
        )
    first_size = path.stat().st_size

    sleep(stability_wait_s)

    if not path.is_file():
        raise SaveVerificationError(
            "expected .Civ6Save disappeared between the stability-check reads",
            detail={"path": str(path)},
        )
    second_size = path.stat().st_size

    if first_size != second_size:
        raise SaveVerificationError(
            "the .Civ6Save's size is not yet stable across two reads; the game "
            "may still be writing it",
            detail={"path": str(path), "first_size": first_size, "second_size": second_size},
        )

    return VerifiedSave(path=path, size_bytes=second_size)
