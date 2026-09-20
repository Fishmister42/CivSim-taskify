"""Helpers shared by all three adapters (research R19).

Two of the six `HostPlatform` capabilities -- locating the game process and
reading free disk space -- happen to be implemented identically on every
platform, because their underlying libraries (`psutil`, the stdlib
`shutil`) are already cross-platform. R19's own capability table lists
`psutil` and "stdlib" for all three platform columns. Keeping one
implementation here, imported by each adapter, avoids tripling logic that
never actually varies, while still presenting the capability through every
adapter as `HostPlatform` requires.

This module is private (`_shared`, not re-exported from `civsim_harness.
host`) because it is plumbing for the adapters, not part of the port
itself.
"""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from pathlib import Path

import psutil

from civsim_harness.host.port import DiskSpace, GameProcess


def locate_process_by_names(candidate_names: Sequence[str]) -> GameProcess | None:
    """Find the first running process whose name matches one of `candidate_names`.

    Matching is case-insensitive and accepts a prefix match (so
    `"CivilizationVI"` matches a reported name of `"CivilizationVI.exe"` and
    vice versa) -- the harness does not know in advance exactly how each
    platform's process table will spell the executable name (see the
    per-adapter `# UNVERIFIED` notes on the exact candidate lists).
    """
    lowered_candidates = {name.lower() for name in candidate_names}
    for proc in psutil.process_iter():
        try:
            reported_name = proc.name()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        lowered_name = str(reported_name).lower()
        if not lowered_name:
            continue
        matched = lowered_name in lowered_candidates or any(
            lowered_name.startswith(candidate) or candidate.startswith(lowered_name)
            for candidate in lowered_candidates
        )
        if not matched:
            continue
        try:
            executable = proc.exe()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            executable = None
        return GameProcess(
            pid=int(proc.pid),
            name=str(reported_name),
            executable_path=Path(str(executable)) if executable else None,
        )
    return None


def read_disk_space(path: Path) -> DiskSpace:
    """Read free/total bytes at `path` via the stdlib (identical on every platform).

    `path` itself is **not** required to exist (research R5's T077 spike: the
    Linux save directory did not exist until the first save created it, and
    the same is true of the equivalent directories this repo cannot yet
    confirm live on Windows/macOS). `shutil.disk_usage` needs a real,
    existing path to stat -- on Windows it raises `FileNotFoundError` for a
    path that is merely missing a leaf directory, not actually inaccessible
    -- so a caller headroom-checking a not-yet-created save directory on a
    fresh install would otherwise get a spurious crash rather than an
    honest "free/total bytes" answer. Walking up to the nearest existing
    ancestor resolves that: free space is a property of the volume/mount
    the path *would* land on, which an ancestor already sitting on that same
    volume reports identically. The returned `DiskSpace.path` is still the
    originally requested `path`, not the ancestor substituted internally --
    callers asked about `path` and should see `path` back.
    """
    probe = path
    while not probe.exists():
        parent = probe.parent
        if parent == probe:
            # Reached the filesystem root (or an anchor with no further
            # parent) without finding an existing directory; let
            # `disk_usage` raise its own error rather than loop forever.
            break
        probe = parent
    usage = shutil.disk_usage(probe)
    return DiskSpace(path=path, free_bytes=int(usage.free), total_bytes=int(usage.total))
