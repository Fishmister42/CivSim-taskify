"""`civsim doctor` / `GET /health` (T121; quickstart.md "Setup", FR-043).

Reports, section by section, exactly what quickstart.md's Setup walkthrough
documents: platform and support tier, tuner connection (with resolved state
indices), client liveness (with its build), store reachability, catalog load
(version, declaration count, undeclared count), capture path (with
hygiene-spike status), disk headroom, and provider key *presence* only -- never
its value (FR-043, SC-018).

**Every section fails independently and reports honestly; nothing here ever
raises out of `run_doctor`.** A missing game client, an unreachable tuner, or
an unconfigured store are all legitimate, expected states before a run has
ever started -- `doctor` exists to *name* them cleanly, not to treat them as
crashes. Each `_probe_*` helper below therefore catches exactly the exception
its own dependency is documented to raise and turns it into a diagnostic
value; nothing is caught blindly.

**The live tuner/game-loaded distinction (a finding from this wave, since
`NexusClient.connect()` was verified against a real client mid-implementation):**
`NexusClient.connect()` now succeeds at the main menu, where the state table
holds only `Main State` and `DebugHotloadCache` -- `GameCore_Tuner` and
`InGame` do not exist until a game is loaded, and are resolved by the separate
`NexusClient.resolve_game_states()`, which raises `PreflightError` naming
what is still missing. This module reports that condition as `"tuner
reachable, no game loaded"` -- a normal, useful diagnostic state, never a
failure -- rather than folding it into the same bucket as "tuner unreachable."

**Capture-hygiene spike wiring is intentionally left as an injection point.**
The R6 spike itself (an occlusion/border capture test against a live game
window) is not this task's to implement -- no host adapter exposes a "run the
spike now" method today (`host/port.py`'s six methods are frozen for this
feature per research R19). `run_doctor` accepts a
`civsim_harness.host.detect.SupportProbeResult` directly (defaulting to
`UNPROBED`, i.e. "not yet probed" -- the honest default) so that whichever wave
does wire the live spike can pass its result straight in without this module
changing shape. Until then, `doctor` correctly and honestly reports capture
path as `none (runs will be visually degraded)`, exactly as quickstart.md
documents for a host whose spike has not passed.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path

from civsim_harness.capability.loader import load_catalog
from civsim_harness.errors import CatalogError, NexusError, PreflightError
from civsim_harness.host.detect import (
    UNPROBED,
    HostInfo,
    SupportProbeResult,
    SupportTier,
    detect_host_info,
    resolve_support_tier,
)
from civsim_harness.host.port import HostPlatform
from civsim_harness.models.common import HarnessModel
from civsim_harness.nexus.client import NexusClient, StateIndices
from civsim_harness.store.port import MatchStore

__all__ = [
    "DEFAULT_PROVIDER_KEY_ENV_VAR",
    "CapturePathDiagnostic",
    "CatalogDiagnostic",
    "ClientDiagnostic",
    "DiskDiagnostic",
    "DoctorReport",
    "PlatformDiagnostic",
    "ProviderKeyDiagnostic",
    "StoreDiagnostic",
    "TunerDiagnostic",
    "format_doctor_report",
    "run_doctor",
]

#: quickstart.md Prerequisites: "An OpenRouter API key in the environment ...
#: Never in run configuration (FR-043)".
DEFAULT_PROVIDER_KEY_ENV_VAR = "OPENROUTER_API_KEY"

NexusClientFactory = Callable[[], NexusClient]


# --------------------------------------------------------------------------
# Report shape -- plain HarnessModel sections, one per quickstart.md line
# --------------------------------------------------------------------------


class PlatformDiagnostic(HarnessModel):
    status: str  # "ok" | "error"
    os: str | None = None
    os_version: str | None = None
    session_type: str | None = None
    tier: str | None = None
    tier_reason: str | None = None
    detail: str | None = None


class TunerDiagnostic(HarnessModel):
    status: str  # "ok" | "no_game_loaded" | "unreachable"
    game_core_tuner_index: int | None = None
    in_game_index: int | None = None
    detail: str | None = None


class ClientDiagnostic(HarnessModel):
    status: str  # "ok" | "not_running"
    pid: int | None = None
    build: str | None = None


class StoreDiagnostic(HarnessModel):
    status: str  # "ok" | "unreachable"
    detail: str | None = None


class CatalogDiagnostic(HarnessModel):
    status: str  # "ok" | "error"
    version: str | None = None
    declaration_count: int | None = None
    undeclared_count: int | None = None
    detail: str | None = None


class CapturePathDiagnostic(HarnessModel):
    status: str  # "ok" | "none"
    path: str | None = None
    hygiene_spike: str  # "passed" | "not_passed"
    detail: str | None = None


class ProviderKeyDiagnostic(HarnessModel):
    env_var: str
    present: bool


class DiskDiagnostic(HarnessModel):
    status: str  # "ok" | "error"
    free_gb: float | None = None
    total_gb: float | None = None
    detail: str | None = None


class DoctorReport(HarnessModel):
    platform: PlatformDiagnostic
    tuner: TunerDiagnostic
    client: ClientDiagnostic
    store: StoreDiagnostic
    catalog: CatalogDiagnostic
    capture_path: CapturePathDiagnostic
    provider_key: ProviderKeyDiagnostic
    disk: DiskDiagnostic


# --------------------------------------------------------------------------
# Per-section probes -- each catches only its own dependency's documented
# failure mode and returns a diagnostic value; none of these raise.
# --------------------------------------------------------------------------


def _probe_platform(
    host_info: HostInfo, support_probe: SupportProbeResult
) -> PlatformDiagnostic:
    tier = resolve_support_tier(support_probe)
    return PlatformDiagnostic(
        status="ok",
        os=host_info.os.value,
        os_version=host_info.os_version,
        session_type=(host_info.session_type.value if host_info.session_type else None),
        tier=tier.value,
        tier_reason=support_probe.reason,
    )


async def _probe_tuner(client_factory: NexusClientFactory) -> TunerDiagnostic:
    """Report the tuner's state. Never raises -- see this module's docstring.

    **`NexusError` is caught alongside `PreflightError`, and that is not defensive padding.**
    `PreflightError` alone covers the two states reachable with *no* client running: a refused
    connection, and a reachable tuner with no game loaded. Every other way the handshake can end
    -- it times out, the socket drops mid-handshake, the state table comes back malformed --
    raises `NexusError`, and all three are only reachable when something is **actually listening
    on the tuner port**. That is precisely the situation `doctor` exists for: it is the first
    command anyone runs against a live client, and a `doctor` that crashes instead of reporting
    is worse than no `doctor` at all. Reported as `unreachable` with the underlying message,
    which is the honest description of a tuner that answered but could not be handshaked.
    """
    client = client_factory()
    try:
        await client.connect()
    except PreflightError as exc:
        return TunerDiagnostic(status="unreachable", detail=exc.message)
    except NexusError as exc:
        return TunerDiagnostic(status="unreachable", detail=exc.message)

    try:
        indices: StateIndices = await client.resolve_game_states()
    except PreflightError:
        return TunerDiagnostic(status="no_game_loaded")
    except NexusError as exc:
        return TunerDiagnostic(status="unreachable", detail=exc.message)
    finally:
        await _close_quietly(client)

    return TunerDiagnostic(
        status="ok",
        game_core_tuner_index=indices.game_core_tuner,
        in_game_index=indices.in_game,
    )


async def _close_quietly(client: NexusClient) -> None:
    """Close *client* without letting a failed close become the error the operator sees.

    `doctor` has already gathered everything it came for by the time this runs; a socket that
    will not shut down cleanly is not a diagnostic result and must not replace one.
    """
    try:
        await client.close()
    except Exception:  # noqa: BLE001 - a failed close must never crash a diagnostic command
        return


def _probe_client(
    host: HostPlatform, *, build_reader: Callable[[], str | None]
) -> ClientDiagnostic:
    """Report client liveness and, when one is running, its build. Never raises.

    `build_reader` is only ever called when a process was actually located, so until a live
    client existed on a host this line had never run. It reads the executable's version off disk
    (`observe/game_build.py`), which can fail for perfectly ordinary reasons on a real machine --
    a permission-denied read, a path the platform adapter resolved differently, a missing
    optional dependency. None of those mean "no client is running", which is the only thing this
    probe is being asked, so the build is reported as unknown and the liveness answer -- the
    useful one -- survives.
    """
    process = host.locate_game_process()
    if process is None:
        return ClientDiagnostic(status="not_running")
    try:
        build = build_reader()
    except Exception:  # noqa: BLE001 - see docstring: liveness must survive a failed build read
        build = None
    return ClientDiagnostic(status="ok", pid=process.pid, build=build)


def _probe_store(store: MatchStore) -> StoreDiagnostic:
    health = store.ping()
    if health.ok:
        return StoreDiagnostic(status="ok")
    return StoreDiagnostic(status="unreachable", detail=health.detail)


def _probe_catalog(catalog_root: Path) -> CatalogDiagnostic:
    try:
        catalog = load_catalog(catalog_root)
    except CatalogError as exc:
        return CatalogDiagnostic(status="error", detail=exc.message)

    declared_capability_ids = {
        declaration.capability_id for declaration in catalog.declarations.values()
    }
    undeclared = [
        capability_id
        for capability_id in catalog.capabilities
        if capability_id not in declared_capability_ids
    ]
    return CatalogDiagnostic(
        status="ok",
        version=catalog.version.version,
        declaration_count=len(catalog.declarations),
        undeclared_count=len(undeclared),
    )


def _probe_capture_path(
    host_info: HostInfo,
    support_probe: SupportProbeResult,
    *,
    capture_path_name: str | None,
) -> CapturePathDiagnostic:
    tier = resolve_support_tier(support_probe)
    if tier is SupportTier.validated:
        return CapturePathDiagnostic(
            status="ok", path=capture_path_name, hygiene_spike="passed"
        )
    return CapturePathDiagnostic(
        status="none",
        path=capture_path_name,
        hygiene_spike="not_passed",
        detail=support_probe.reason,
    )


def _probe_provider_key(
    env: Mapping[str, str], *, env_var: str
) -> ProviderKeyDiagnostic:
    return ProviderKeyDiagnostic(env_var=env_var, present=bool(env.get(env_var)))


def _probe_disk(host: HostPlatform, path: Path) -> DiskDiagnostic:
    try:
        space = host.free_disk_space(path)
    except OSError as exc:
        return DiskDiagnostic(status="error", detail=str(exc))
    gib = 1024**3
    return DiskDiagnostic(
        status="ok",
        free_gb=round(space.free_bytes / gib, 1),
        total_gb=round(space.total_bytes / gib, 1),
    )


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


async def run_doctor(
    *,
    host: HostPlatform,
    host_info: HostInfo | None = None,
    support_probe: SupportProbeResult = UNPROBED,
    nexus_client_factory: NexusClientFactory | None = None,
    build_reader: Callable[[], str | None] | None = None,
    store: MatchStore,
    catalog_root: Path,
    capture_path_name: str | None = None,
    disk_path: Path | None = None,
    env: Mapping[str, str] | None = None,
    provider_key_env_var: str = DEFAULT_PROVIDER_KEY_ENV_VAR,
) -> DoctorReport:
    """Run every `doctor` probe and assemble the full report.

    Every argument beyond `host`, `store`, and `catalog_root` is injectable
    with an honest, non-crashing default so this is callable in a unit test
    with no live tuner, no real game process, and no populated catalog root --
    see the module docstring for why the capture/tuner probes are structured
    this way.
    """
    resolved_host_info = host_info if host_info is not None else detect_host_info()
    resolved_env = env if env is not None else os.environ
    resolved_disk_path = disk_path if disk_path is not None else Path.cwd()
    resolved_build_reader = build_reader if build_reader is not None else (lambda: None)
    resolved_client_factory: NexusClientFactory = (
        nexus_client_factory if nexus_client_factory is not None else NexusClient
    )

    return DoctorReport(
        platform=_probe_platform(resolved_host_info, support_probe),
        tuner=await _probe_tuner(resolved_client_factory),
        client=_probe_client(host, build_reader=resolved_build_reader),
        store=_probe_store(store),
        catalog=_probe_catalog(catalog_root),
        capture_path=_probe_capture_path(
            resolved_host_info, support_probe, capture_path_name=capture_path_name
        ),
        provider_key=_probe_provider_key(resolved_env, env_var=provider_key_env_var),
        disk=_probe_disk(host, resolved_disk_path),
    )


# --------------------------------------------------------------------------
# Human-readable rendering (quickstart.md Setup's exact line shapes)
# --------------------------------------------------------------------------

_LABEL_WIDTH = 18


def _line(label: str, body: str) -> str:
    return f"{label:<{_LABEL_WIDTH}}: {body}"


def format_doctor_report(report: DoctorReport) -> str:
    """Render *report* in the line-per-section shape quickstart.md documents."""
    lines: list[str] = []

    platform = report.platform
    if platform.status == "ok":
        session = f"/{platform.session_type}" if platform.session_type else ""
        tier_label = (platform.tier or "?").upper()
        body = f"ok  ({platform.os}{session} {platform.os_version}, tier {tier_label})"
        lines.append(_line("platform", body))
    else:
        lines.append(_line("platform", f"error  ({platform.detail})"))

    tuner = report.tuner
    if tuner.status == "ok":
        lines.append(
            _line(
                "tuner connection",
                f"ok  (GameCore_Tuner={tuner.game_core_tuner_index}, InGame={tuner.in_game_index})",
            )
        )
    elif tuner.status == "no_game_loaded":
        lines.append(_line("tuner connection", "reachable, no game loaded"))
    else:
        lines.append(_line("tuner connection", f"unreachable  ({tuner.detail})"))

    client = report.client
    if client.status == "ok":
        lines.append(_line("client", f"ok  (pid {client.pid}, build {client.build or 'unknown'})"))
    else:
        lines.append(_line("client", "not running"))

    store = report.store
    lines.append(_line("store", "ok" if store.status == "ok" else f"unreachable  ({store.detail})"))

    catalog = report.catalog
    if catalog.status == "ok":
        lines.append(
            _line(
                "catalog",
                f"ok  (version {catalog.version}, {catalog.declaration_count} declarations, "
                f"{catalog.undeclared_count} undeclared)",
            )
        )
    else:
        lines.append(_line("catalog", f"error  ({catalog.detail})"))

    capture = report.capture_path
    if capture.status == "ok":
        lines.append(_line("capture path", f"ok  ({capture.path}, hygiene spike PASSED)"))
    else:
        lines.append(_line("capture path", "none (runs will be visually degraded)"))

    lines.append(
        _line("provider key", "present" if report.provider_key.present else "MISSING")
    )

    disk = report.disk
    if disk.status == "ok":
        lines.append(_line("disk headroom", f"{disk.free_gb} GB free"))
    else:
        lines.append(_line("disk headroom", f"error  ({disk.detail})"))

    return "\n".join(lines)
