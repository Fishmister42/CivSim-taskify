"""Game build read (T071, research R18, R20; FR-020).

Reads the Civilization VI client's build identity as a **composite**
``<platform>/<version>`` string (e.g. ``"win/1.0.12.9"``, ``"mac/1.0.12.9"``),
because the Aspyr macOS and Linux ports are separately built binaries whose
version numbering need not track the Windows build's (R20) -- a bare version
string would hide exactly the hazard the seed-set build pin exists to catch.

This value is **out-of-game provenance** either way (FR-020): it is recorded
on the run and compared against the seed set's pinned ``game_build``
(``run/preparation.py``, T072), and must never be placed in the agent's
context.

Two paths, preferred to fallback (R18):

1. **Preferred**: a declared ``GameCore_Tuner`` observation of the game's own
   version, reached "where reachable" -- catalog authorship for that
   declaration belongs to another wave (``catalogs/`` is not owned by this
   task), so this module accepts the resolved reader as an injected
   callable (:data:`TunerVersionReader`) rather than hard-coding a
   ``declaration_id``. :func:`make_tuner_version_reader` builds one bound to
   an already-connected Nexus session once that declaration exists.
2. **Fallback**: the client executable's own file version, read "through the
   HostPlatform port". **Known gap**: extracting a semantic version from an
   executable (a Windows PE VERSIONINFO resource, a macOS bundle's
   ``Info.plist``, ...) is inherently OS-specific work, and
   ``host/port.py``'s ``HostPlatform`` Protocol -- frozen for this feature,
   exactly six methods per research R19 -- exposes no such capability today.
   :func:`make_host_executable_version_reader` can locate the binary via the
   port's existing ``locate_game_process()``, but versioning it requires an
   ``extract_version`` callable a future host-adapter wave must supply; until
   one exists this fallback correctly (and honestly) reports "unknown" rather
   than fabricating a value.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from civsim_harness.errors import NexusError, PreflightError
from civsim_harness.host.detect import OperatingSystem
from civsim_harness.host.port import HostPlatform

# --------------------------------------------------------------------------
# Composite platform/version identity (R20)
# --------------------------------------------------------------------------

# Matches contracts/run-configuration.md's own examples ("win/1.0.12.9",
# "mac/1.0.12.9"); "linux" extends the same convention for the third native
# platform (research R1, R19).
_PLATFORM_LABELS: dict[OperatingSystem, str] = {
    OperatingSystem.windows: "win",
    OperatingSystem.macos: "mac",
    OperatingSystem.linux: "linux",
}


def platform_label(operating_system: OperatingSystem) -> str:
    """The platform half of the composite build identity (R20)."""
    return _PLATFORM_LABELS[operating_system]


def compose_build(platform: str, version: str) -> str:
    """``<platform>/<version>`` (R20), e.g. ``compose_build("win", "1.0.12.9")``."""
    return f"{platform}/{version}"


def split_build(build: str) -> tuple[str, str]:
    """The inverse of :func:`compose_build`: ``(platform, version)``.

    Raises ``ValueError`` on a string with no ``/`` -- a composite identity
    that cannot be split is not a valid ``game_build`` (R20).
    """
    platform, sep, version = build.partition("/")
    if not sep:
        raise ValueError(
            f"game_build {build!r} is not a composite platform/version identity (R20)"
        )
    return platform, version


def is_platform_transition(from_build: str, to_build: str) -> bool:
    """True when the platform component differs between two composite builds --
    matches ``BuildAcceptance.is_platform_transition`` (data-model.md §1).
    """
    return split_build(from_build)[0] != split_build(to_build)[0]


# --------------------------------------------------------------------------
# The read itself
# --------------------------------------------------------------------------

TunerVersionReader = Callable[[], Awaitable["str | None"]]
"""Reads the game's version through a declared ``GameCore_Tuner`` observation,
returning ``None`` (never raising) when that path is not reachable right now
-- e.g. the declaration does not yet resolve in the loaded catalog, or the
Lua call itself errors. R18's "where reachable" is exactly this: a caller
supplies the concrete reader, and this module treats *any* failure of it as
"not reachable", falling through to the host-based fallback rather than
failing the whole read on that account alone.
"""

HostVersionReader = Callable[[], "str | None"]
"""Reads the client executable's own version through the ``HostPlatform``
port, returning ``None`` when it cannot be determined. See the module
docstring's "Known gap" note.
"""


async def read_game_build(
    *,
    operating_system: OperatingSystem,
    read_via_tuner: TunerVersionReader,
    read_via_host: HostVersionReader,
) -> str:
    """Resolve this run's composite game build (R18, R20): a declared
    ``GameCore_Tuner`` observation where reachable, otherwise the client
    binary's version via the ``HostPlatform`` port.

    Raises ``PreflightError`` when neither path can determine a version: an
    unknown build can never be checked against a seed set's pin (V10), so a
    run that cannot determine its own build must not proceed silently.
    """
    version = await read_via_tuner()
    if not version:
        version = read_via_host()
    if not version:
        raise PreflightError(
            "could not determine the client's game version through either the "
            "declared GameCore_Tuner observation or the HostPlatform fallback (R18)",
            detail={"attempted": ["GameCore_Tuner", "host_platform"]},
        )
    return compose_build(platform_label(operating_system), version)


# --------------------------------------------------------------------------
# Concrete readers
# --------------------------------------------------------------------------

# UNVERIFIED (research R18 names this exact spike as unresolved): no public
# documentation confirms a Lua global that reports the running game's own
# version from GameCore_Tuner. The call below names the *shape* the R18
# spike should confirm, not a fact this repo could verify from here -- it is
# written to fail closed (any Lua-side error, or a missing global, is
# treated as "not reachable") rather than assert something unconfirmed. This
# is the one place to update once R18 records its finding.
_VERSION_LUA = (
    'local ok, value = pcall(function() return tostring(Modding.GetActiveGameVersion()) end); '
    'return { ["ok"] = ok, ["version"] = ok and value or nil }'
)


def make_tuner_version_reader(
    execute: Callable[[int, str], Awaitable[Any]],
    *,
    game_core_tuner_state_index: int,
) -> TunerVersionReader:
    """Build a :data:`TunerVersionReader` bound to an already-connected Nexus
    session's ``GameCore_Tuner`` state.

    *execute* is typically
    :meth:`civsim_harness.nexus.client.NexusClient.execute_command` called as
    ``execute(state_index, lua_body)``; it is accepted as a plain callable
    (rather than importing ``NexusClient`` directly) so this module's pure
    logic has no hard dependency on the transport.
    """

    async def _read() -> str | None:
        try:
            result = await execute(game_core_tuner_state_index, _VERSION_LUA)
        except NexusError:
            return None
        if isinstance(result, dict) and result.get("ok") and result.get("version"):
            return str(result["version"])
        return None

    return _read


def make_host_executable_version_reader(
    host: HostPlatform,
    *,
    extract_version: Callable[[Path], str | None] | None = None,
) -> HostVersionReader:
    """Build a :data:`HostVersionReader` around the client executable path the
    ``HostPlatform`` port can locate (``locate_game_process().executable_path``).

    **Known gap**: turning that path into an actual version string is
    OS-specific work that ``HostPlatform`` (host/port.py, six methods, frozen
    for this feature) does not expose a capability for today -- see the
    module docstring. *extract_version* is therefore a required-by-convention
    injection point: without one supplied, this reader can locate the binary
    but not version it, and reports ``None`` rather than fabricating a value,
    exactly like :func:`read_game_build`'s own "cannot determine" path.
    """

    def _read() -> str | None:
        process = host.locate_game_process()
        if process is None or process.executable_path is None or extract_version is None:
            return None
        return extract_version(process.executable_path)

    return _read
