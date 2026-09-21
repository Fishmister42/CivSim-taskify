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

import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from civsim_harness.errors import NexusError, PreflightError
from civsim_harness.host.detect import OperatingSystem
from civsim_harness.host.port import HostPlatform
from civsim_harness.nexus.sentinels import LUA_JSON_PRELUDE, lua_print_json

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
# The result is **printed** as JSON, not ``return``-ed: the tuner has no native return channel,
# so a body ending in ``return { ... }`` prints nothing between its nonce sentinels and the
# command fails on an empty result (see ``nexus.sentinels.LUA_JSON_PRELUDE``). Every
# ``lua/**/*.lua`` file already follows the same ``print(<json>)`` convention.
_VERSION_LUA = (
    LUA_JSON_PRELUDE
    + "local ok, value = pcall(function() "
    + "return tostring(Modding.GetActiveGameVersion()) end); "
    + lua_print_json({"ok": "ok", "version": "ok and value or nil"})
)

# MEASURED (2026-09-20, Linux/Aspyr 1.0.12.9, live): the R18 spike above resolves to "no".
# ``Modding.GetActiveGameVersion`` does not exist on this client in either state -- ``Modding``
# is absent from ``GameCore_Tuner`` altogether, and in ``InGame`` the function is nil -- so the
# preferred path never answers there, and with no Linux ``extract_version`` the host fallback
# is dead too: every run on this host failed preparation with "could not determine the
# client's game version". What does answer is **``UI.GetAppVersion()`` in the UI-side
# ``InGame`` state**: ``"1.0.12.9 (564030)"`` (the same string the T217 spike's raw session
# recorded). ``UI`` is absent from ``GameCore_Tuner``, so this read targets ``InGame``,
# resolved fresh on every call exactly like the tuner index. Tried second, after the declared
# path, so a build where ``Modding.GetActiveGameVersion`` does exist still uses it.
_UI_VERSION_LUA = (
    LUA_JSON_PRELUDE
    + "local ok, value = pcall(function() return tostring(UI.GetAppVersion()) end); "
    + lua_print_json({"ok": "ok", "version": "ok and value or nil"})
)

_VERSION_TOKEN = re.compile(r"\d+(?:\.\d+)+")


def normalise_version(raw: str) -> str | None:
    """The dotted version out of what the client prints: ``"1.0.12.9 (564030)"`` ->
    ``"1.0.12.9"``. The composite pin is compared in that form (contracts/run-configuration.md's
    own examples, ``"win/1.0.12.9"``; the seed sets pin ``linux/1.0.12.9``), and the
    parenthesised build number is not part of it. ``None`` when no dotted token is present:
    a string with no version in it is not a version, and is never passed through as one."""
    match = _VERSION_TOKEN.search(raw)
    return match.group(0) if match else None


# --------------------------------------------------------------------------
# GameCore_Tuner state index resolution -- never a raw int captured once
# --------------------------------------------------------------------------
#
# Same hazard ``saves/save_game.py`` documents and fixes for the InGame
# index: a later live capture established that Lua state indices differ by
# *game phase*, not only by client version or reconnect (see
# ``civsim_harness.nexus.client``'s module docstring,
# ``REASON_STALE_STATE_INDEX``, ``refresh_state_indices()``). The
# :data:`TunerVersionReader` this factory returns is a stored callable that
# can outlive the phase it was built in -- exactly the shape that let a
# captured int go silently stale -- so it resolves the index fresh on every
# call instead of closing over one.


class _StateIndicesLike(Protocol):
    """Structural shape of ``civsim_harness.nexus.client.StateIndices`` --
    duck-typed, never imported, so this module keeps no hard dependency on
    the transport (matching *execute*'s own convention below)."""

    @property
    def game_core_tuner(self) -> int | None: ...

    @property
    def in_game(self) -> int | None: ...


@runtime_checkable
class _HasStateIndices(Protocol):
    """Structural shape of a connected ``NexusClient`` (or anything alike):
    "the client itself", read fresh on every call rather than snapshotted
    once."""

    @property
    def state_indices(self) -> _StateIndicesLike | None: ...


GameCoreTunerIndexResolver = Callable[[], int]
"""Zero-argument, synchronous callable returning the *current*
``GameCore_Tuner`` Lua state index -- synchronous and in-memory by design,
matching ``saves.save_game.InGameStateIndexResolver``."""

GameCoreTunerIndexSource = GameCoreTunerIndexResolver | _HasStateIndices
"""What :func:`make_tuner_version_reader` accepts in place of a raw ``int``:
either a :data:`GameCoreTunerIndexResolver`, or a connected Nexus session
itself (anything shaped like ``NexusClient``, i.e. exposing
``.state_indices``). Passing the client directly is the common case: it
already holds the current state table in memory."""


def _resolve_game_core_tuner_index(source: GameCoreTunerIndexSource) -> int:
    """Ask *source* for the current ``GameCore_Tuner`` Lua state index,
    right now -- never a value captured earlier."""
    if isinstance(source, _HasStateIndices):
        indices = source.state_indices
        if indices is None or indices.game_core_tuner is None:
            raise NexusError(
                "Cannot resolve the GameCore_Tuner Lua state index -- this "
                "Nexus session has not connected, or resolve_game_states() "
                "has not yet succeeded, so no game-play state table is "
                "available to read the build from",
                detail={"reason": "game_core_tuner_state_index_unresolved"},
            )
        return indices.game_core_tuner
    return source()


def _resolve_in_game_index(source: GameCoreTunerIndexSource) -> int:
    """The current ``InGame`` index for the measured ``UI.GetAppVersion()`` read -- only a
    connected session can supply it; a bare ``GameCore_Tuner`` resolver cannot, and then the
    UI-side read is simply not reachable (``NexusError``, falling through like any other)."""
    if isinstance(source, _HasStateIndices):
        indices = source.state_indices
        if indices is None or indices.in_game is None:
            raise NexusError(
                "Cannot resolve the InGame Lua state index -- this Nexus session has not "
                "connected, or no game is loaded, so UI.GetAppVersion() is not reachable",
                detail={"reason": "in_game_state_index_unresolved"},
            )
        return indices.in_game
    raise NexusError(
        "a bare GameCore_Tuner index resolver cannot supply the InGame index the "
        "UI.GetAppVersion() read needs; pass the connected client instead",
        detail={"reason": "in_game_state_index_unresolved"},
    )


def make_tuner_version_reader(
    execute: Callable[[int, str], Awaitable[Any]],
    *,
    game_core_tuner_state_index: GameCoreTunerIndexSource,
) -> TunerVersionReader:
    """Build a :data:`TunerVersionReader` bound to an already-connected Nexus
    session's ``GameCore_Tuner`` state.

    *execute* is typically
    :meth:`civsim_harness.nexus.client.NexusClient.execute_command` called as
    ``execute(state_index, lua_body)``; it is accepted as a plain callable
    (rather than importing ``NexusClient`` directly) so this module's pure
    logic has no hard dependency on the transport.

    *game_core_tuner_state_index* is deliberately **not** a bare ``int``:
    the returned reader is a stored callable that may be invoked more than
    once over the life of a run, and a phase transition (or reconnect)
    between two of those calls can make a captured index silently wrong
    rather than merely absent -- see this module's docstring. Pass either a
    zero-argument resolver or the connected ``NexusClient`` itself; the
    index is re-resolved fresh on every call. A resolution failure (no
    ``GameCore_Tuner`` state available yet) is treated exactly like any
    other ``NexusError`` from this reader: "not reachable right now",
    falling through to the host-based fallback rather than failing the
    whole read.
    """

    async def _read() -> str | None:
        # The declared GameCore_Tuner read first (R18's preferred path), then the measured
        # UI-side read (see `_UI_VERSION_LUA`). Each is "not reachable" on any failure --
        # an unresolvable index, a transport error, a Lua-side error caught by the pcall, or
        # a string with no version in it -- and falls through to the next.
        attempts = (
            (_resolve_game_core_tuner_index, _VERSION_LUA),
            (_resolve_in_game_index, _UI_VERSION_LUA),
        )
        for resolve, lua_body in attempts:
            try:
                state_index = resolve(game_core_tuner_state_index)
                result = await execute(state_index, lua_body)
            except NexusError:
                continue
            if isinstance(result, dict) and result.get("ok") and result.get("version"):
                version = normalise_version(str(result["version"]))
                if version:
                    return version
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
