"""Run preparation: the build-pin preflight (T072) and setup application/
verification (T076).

data-model.md §1, §4; contracts/run-configuration.md V2, V10; research R18,
R20; invariant I18.

Two responsibilities, both scoped to *before turn 1* -- but with a
deliberately different failure shape, matching the spec's own asymmetry:

1. :func:`build_pin_preflight` (**T072 / V10 / invariant I18**): compares the
   client's actually-read composite build (``observe.game_build``) against
   the seed set's pinned ``game_build``. A mismatch **without** a covering
   ``BuildAcceptance`` raises -- the run "must not be constructible" per I18,
   so this is a hard failure before a ``Run`` record even exists, not
   something recorded on one afterwards.
2. :func:`apply_configuration` / :func:`verify_configuration` (**T076 / V2**):
   apply the configured seed, civilization, ruleset, mod set, map and game
   settings, difficulty, and opponents, then read the actual setup back and
   compare field by field. Here a mismatch does **not** raise -- V2 and
   data-model.md §4 are explicit that this "creates the run in `failed`
   state with the mismatch recorded", i.e. the ``Run`` already exists (in
   `preparing`) and the caller (a later wave's run orchestrator) transitions
   it to `failed` via ``run.lifecycle.transition`` using the returned
   mismatches as the ``preparation_mismatch`` event detail. This module
   therefore returns a result rather than raising, exactly like
   ``run.lifecycle.transition`` itself returns rather than persists --
   neither module owns the store write.

Neither function here dispatches to the game directly: applying a setting and
reading one back are both genuinely declared-observation/action concerns
(``act/**``, ``observe/assemble.py``), owned by other waves. Both functions
accept the actual apply/read mechanism as an injected callable, matching the
seam pattern already used throughout this codebase (``HostPlatform``,
``ModelProvider``, ``observe.game_build``'s tuner/host readers).

3. :func:`catalog_preflight` (**T135 / T136 / FR-022 / FR-023 / V5 / SC-006 / SC-007**): the
   catalog-side counterpart to the two responsibilities above, following the same "self-contained
   function another wave's module calls" shape :func:`~civsim_harness.observe.host_gate.
   evaluate_host_gate` (T101) already established for this same file. Two things, one function,
   because they are two views of the same loaded :class:`~civsim_harness.capability.loader.Catalog`
   and always run together at preflight:

   - **T136, the gate**: every *capability* the run could use must be governed by at least one
     parity declaration, or the run does not start, naming the offending ``capability_id``. This is
     deliberately the *reverse* of what :func:`~civsim_harness.capability.loader.load_catalog`
     already checks at load time (its validation 2 confirms every *declaration*'s ``capability_id``
     resolves to a loaded capability -- declaration -> capability). FR-023's "any observation or
     action available to it lacks a declared parity basis" is the other direction: a loaded,
     invokable capability with **zero** declarations pointing to it would be reachable machinery
     with no parity basis governing it at all, and nothing at load time catches that, because the
     loader only ever walks declarations outward to their capability, never capabilities inward to
     their declarations.
   - **T135, the record**: the version and content hash of the one loaded catalog, packaged as the
     ``CatalogVersionRef`` pair ``Run.observation_catalog_version`` / ``Run.action_catalog_version``
     both need (FR-022). One catalog root, one ``catalogs/VERSION``, one computed content hash
     (:func:`~civsim_harness.capability.version.compute_content_hash`) -- so both fields get the
     identical reference; a future split into independently-versioned observation/action catalogs
     would need this function's own return shape to change, not merely its call site.

4. :func:`debug_menu_preflight` (**T204 hardening item 1**): reads ``EnableDebugMenu`` from
   ``AppOptions.txt`` -- the same file already located via
   ``HostPlatform.resolve_game_directories()`` for ``EnableTuner`` -- and returns it for the
   caller to record on the run. A live-client spike
   (``specs/002-civ-playing-harness/spikes/principle-i-debugmenu-linux.md``) found the tuner's own
   callable surface byte-identical across three separate comparisons with the debug menu on and
   off, so there is no constitutional tension (Principle I) to enforce here -- this function
   **records the setting, it never refuses a run over it**.
5. :func:`turn_timer_preflight` (**T204 hardening item 2**): verifies, via an injected reader,
   that this run is not using a turn timer before it starts. A live spike originally attributed a
   validation host's turns advancing on their own (``spikes/load-path-linux.md``) to auto-end-turn
   being enabled; that finding was retracted once ``UserOptions.txt``'s own ``AutoEndTurn 0`` was
   found already correctly set, and the real, measured cause is
   ``GameConfiguration.GetTurnTimerType()`` returning ``TURNTIMER_STANDARD`` even in a
   single-player game -- turn 1 advanced to turn 6 at roughly one turn per 25 s with zero input
   once one end-turn was issued. Three outcomes: verified no-timer (proceeds), verified a timer is
   active (raises -- the run must not start), and undeterminable (proceeds, but the result records
   an explicit *unverified* precondition rather than silently treating "cannot tell" as
   "confirmed off"). See the function's own docstring for why FR-011, FR-014, and FR-015/SC-022 all
   depend on this holding.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from civsim_harness.capability.loader import Catalog
from civsim_harness.errors import BuildMismatchError, PreflightError
from civsim_harness.host.port import HostPlatform
from civsim_harness.models.common import BuildAcceptance, CapabilityId, CatalogVersionRef
from civsim_harness.models.config import RunConfiguration, SeedSet
from civsim_harness.observe.game_build import is_platform_transition

# --------------------------------------------------------------------------
# T072 -- build-pin preflight (V10, invariant I18)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BuildPinResult:
    """The outcome of checking the client's build against a seed set's pin.

    ``acceptance`` is non-``None`` exactly when ``matches`` is ``False`` but a
    recorded :class:`BuildAcceptance` covers this exact transition --
    matching ``Run.game_build_acceptance_ref``'s own invariant
    (data-model.md §4: "non-null exactly when game_build differs ... in
    either component"). When ``matches`` is ``False`` and no acceptance
    covers the transition, :func:`build_pin_preflight` raises instead of
    returning a result at all -- there is no "unaccepted mismatch" value of
    this type.
    """

    matches: bool
    seed_set_build: str
    actual_build: str
    acceptance: BuildAcceptance | None
    is_platform_transition: bool


def _find_acceptance(
    accepted_build_changes: list[BuildAcceptance], *, from_build: str, to_build: str
) -> BuildAcceptance | None:
    """An acceptance covers **exactly** one composite ``from_build -> to_build``
    transition (contracts/run-configuration.md "The build pin"): accepting
    ``win/1.0.12.9 -> win/1.0.12.11`` must not also permit ``win -> mac``, and
    an exact string match on both ends is what guarantees that -- a platform
    difference simply never matches a version-only acceptance's ``to_build``,
    and vice versa.
    """
    for acceptance in accepted_build_changes:
        if acceptance.from_build == from_build and acceptance.to_build == to_build:
            return acceptance
    return None


def build_pin_preflight(seed_set: SeedSet, actual_build: str) -> BuildPinResult:
    """**V10 / invariant I18**: compare *actual_build* (the client's
    just-read composite build, ``observe.game_build.read_game_build``)
    against *seed_set*'s pinned ``game_build``.

    Returns a :class:`BuildPinResult` when the builds match, or when they
    differ but a :class:`BuildAcceptance` on *seed_set* covers this exact
    transition. Raises :class:`~civsim_harness.errors.BuildMismatchError`
    otherwise -- **a run whose build differs from its seed set's without an
    acceptance reference must not be constructible** (I18), so this is a
    hard preflight failure, not something recorded on an already-created
    run.

    A platform difference (e.g. ``win/1.0.12.9`` vs ``mac/1.0.12.9``) fails
    exactly the same way a version difference does -- both are composite
    ``game_build`` strings compared for exact equality, with no special
    casing of either component (R20).
    """
    if actual_build == seed_set.game_build:
        return BuildPinResult(
            matches=True,
            seed_set_build=seed_set.game_build,
            actual_build=actual_build,
            acceptance=None,
            is_platform_transition=False,
        )

    platform_transition = is_platform_transition(seed_set.game_build, actual_build)
    acceptance = _find_acceptance(
        seed_set.accepted_build_changes,
        from_build=seed_set.game_build,
        to_build=actual_build,
    )
    if acceptance is None:
        raise BuildMismatchError(
            "the client's game build does not match the seed set's pinned game_build, "
            "and no BuildAcceptance covers this exact transition (V10, invariant I18)",
            detail={
                "seed_set": seed_set.name,
                "from_build": seed_set.game_build,
                "to_build": actual_build,
                "is_platform_transition": platform_transition,
            },
        )

    return BuildPinResult(
        matches=False,
        seed_set_build=seed_set.game_build,
        actual_build=actual_build,
        acceptance=acceptance,
        is_platform_transition=platform_transition,
    )


# --------------------------------------------------------------------------
# T076 -- apply configured setup, then verify it field by field (V2)
# --------------------------------------------------------------------------


def configured_fields(config: RunConfiguration) -> dict[str, Any]:
    """Flatten the configured elements T076 names -- seed, civilization,
    leader, ruleset, mod set, map settings, game settings, difficulty, and
    opponents -- into a flat ``name -> value`` mapping used for both applying
    and reading back (map/game settings and opponents are nested objects in
    ``RunConfiguration``; each of their own keys becomes its own dotted field
    here, so a mismatch names exactly the sub-setting that disagreed rather
    than the whole nested object).
    """
    fields: dict[str, Any] = {
        "map_seed": config.map_seed,
        "civilization": config.civilization,
        "leader": config.leader,
        "ruleset": config.ruleset,
        "mod_set": [mod.model_dump() for mod in config.mod_set],
        "difficulty": config.difficulty,
    }
    for key, value in config.map_settings.items():
        fields[f"map_settings.{key}"] = value
    for key, value in config.game_settings.items():
        fields[f"game_settings.{key}"] = value
    for key, value in config.opponents.items():
        fields[f"opponents.{key}"] = value
    return fields


def apply_configuration(
    config: RunConfiguration,
    *,
    apply_setting: Callable[[str, Any], None],
) -> tuple[str, ...]:
    """Apply every configured field in :func:`configured_fields` via
    *apply_setting* (the actual game dispatch, owned by ``act/**`` -- injected
    here rather than imported, since that package is a different wave's
    responsibility). Returns the field names applied, in order.
    """
    fields = configured_fields(config)
    for name, value in fields.items():
        apply_setting(name, value)
    return tuple(fields.keys())


@dataclass(frozen=True)
class SettingMismatch:
    """One field where the game's actual setup disagreed with what was configured."""

    field: str
    expected: Any
    actual: Any


@dataclass(frozen=True)
class PreparationResult:
    """The outcome of reading the actual game setup back and comparing it,
    field by field, against what was configured (V2).

    An empty ``mismatches`` tuple (``matched`` is ``True``) is what lets a run
    proceed to turn 1; any mismatch at all means the caller must create the
    run in ``failed`` state with these mismatches recorded as a
    ``preparation_mismatch`` event (data-model.md §4) and attempt no turn 1 --
    this type deliberately carries no lifecycle/store behaviour of its own,
    matching ``run.lifecycle.transition``'s own "returns rather than
    persists" pattern.
    """

    applied_fields: tuple[str, ...]
    mismatches: tuple[SettingMismatch, ...]

    @property
    def matched(self) -> bool:
        return not self.mismatches


def verify_configuration(
    config: RunConfiguration,
    *,
    read_setting: Callable[[str], Any],
) -> PreparationResult:
    """**V2 / T076**: read the actual game setup back via *read_setting* (the
    actual declared-observation reads, owned by ``observe/assemble.py`` --
    injected here for the same reason as :func:`apply_configuration`'s
    *apply_setting*) and compare it against :func:`configured_fields`, field
    by field.

    Every configured field is checked -- this does not stop at the first
    mismatch -- so a caller building the ``preparation_mismatch`` event
    detail sees the complete picture in one pass rather than one field per
    replay.
    """
    fields = configured_fields(config)
    mismatches: list[SettingMismatch] = []
    for name, expected in fields.items():
        actual = read_setting(name)
        if actual != expected:
            mismatches.append(SettingMismatch(field=name, expected=expected, actual=actual))
    return PreparationResult(applied_fields=tuple(fields.keys()), mismatches=tuple(mismatches))


# --------------------------------------------------------------------------
# T135 / T136 -- catalog preflight: capability-resolution gate + version record
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CatalogPreflightResult:
    """T135's deliverable: the ``CatalogVersionRef`` pair to set on the ``Run`` under
    construction, returned only once :func:`catalog_preflight`'s T136 gate has already passed --
    there is no way to obtain one of these for a catalog carrying an ungoverned capability.
    """

    observation_catalog_version: CatalogVersionRef
    action_catalog_version: CatalogVersionRef


def _capabilities_without_declarations(catalog: Catalog) -> list[CapabilityId]:
    """Every ``capability_id`` in *catalog* that no loaded ``ParityDeclaration`` points to.

    The reverse of ``capability.loader.load_catalog``'s own validation 2 (see module docstring) --
    sorted so a raised :class:`~civsim_harness.errors.PreflightError`'s ``detail`` is deterministic
    across runs, not dependent on dict iteration order.
    """
    governed = {declaration.capability_id for declaration in catalog.declarations.values()}
    return sorted(
        capability_id for capability_id in catalog.capabilities if capability_id not in governed
    )


def catalog_preflight(catalog: Catalog) -> CatalogPreflightResult:
    """**T135 + T136** (FR-022, FR-023, V5, SC-006, SC-007): the run does not start unless every
    capability *catalog* implements is governed by at least one parity declaration; once that
    holds, returns the ``CatalogVersionRef`` pair to record on the ``Run`` under construction.

    Raises :class:`~civsim_harness.errors.PreflightError` naming every offending
    ``capability_id`` at once (never just the first) when
    :func:`_capabilities_without_declarations` finds any -- mirroring
    :func:`~civsim_harness.provider.preflight.preflight_chain`'s "describe every model, name every
    failure" discipline rather than stopping at the first bad capability. A run whose catalog fails
    this gate has no ``Run`` record at all yet (same failure shape as :func:`build_pin_preflight`):
    this is a hard preflight failure, not something recorded on an already-created run.
    """
    offending = _capabilities_without_declarations(catalog)
    if offending:
        raise PreflightError(
            "a capability this run could use has no governing parity declaration; "
            "the run does not start (FR-023, V5, SC-006)",
            detail={"capability_ids": [str(capability_id) for capability_id in offending]},
        )

    ref = CatalogVersionRef(
        version=catalog.version.version, content_hash=catalog.version.content_hash
    )
    return CatalogPreflightResult(observation_catalog_version=ref, action_catalog_version=ref)


# --------------------------------------------------------------------------
# T204 hardening item 1 -- EnableDebugMenu, read and recorded (never enforced)
# --------------------------------------------------------------------------


class DebugMenuState(Enum):
    """Whether ``EnableDebugMenu`` could be read from ``AppOptions.txt``, and if so what it said.

    ``spikes/principle-i-debugmenu-linux.md``'s own live-client spike ran the tuner three separate
    ways with ``EnableDebugMenu`` on and off -- a curated symbol probe, a full namespace
    enumeration, and the Lua state table -- and found all three byte-identical in both modes.
    There is therefore no constitutional tension to enforce here (Principle I): the tuner's own
    callable surface does not widen with the debug menu on. What the spike's own recommendation
    asks for is *recording*, not refusing -- "so a run's parity configuration is reconstructible
    from its record alone rather than from a claim about how the host was set up" -- which is
    exactly what this enum and :func:`debug_menu_preflight` exist to do, and nothing more.
    """

    ENABLED = "enabled"
    DISABLED = "disabled"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class DebugMenuPreflightResult:
    """What :func:`debug_menu_preflight` hands back: the state, the file it was read from, and,
    when the state is not a plain on/off reading, why."""

    state: DebugMenuState
    app_options_path: Path
    detail: str | None = None


_APP_OPTIONS_ENTRY = re.compile(r"^\s*([A-Za-z0-9_]+)\s+(\S+)")


def _read_app_options_entry(text: str, key: str) -> str | None:
    """Read one bare ``KEY VALUE`` entry from ``AppOptions.txt``-shaped text (research spike
    ``launch-tuning-linux.md``'s own example: ``EnableDebugMenu 0    # [Debug] - ...``) -- no
    ``=``, no section headers, one setting per line, trailing ``#``/``;`` comments ignored.
    Returns ``None`` when *key* is not present at all, so a caller can distinguish "absent" from
    "present but unrecognised".
    """
    lowered_key = key.lower()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith(";"):
            continue
        match = _APP_OPTIONS_ENTRY.match(stripped)
        if match is None:
            continue
        name, value = match.group(1), match.group(2)
        if name.lower() == lowered_key:
            return value
    return None


def debug_menu_preflight(
    host: HostPlatform, *, home: Path | None = None
) -> DebugMenuPreflightResult:
    """Read ``EnableDebugMenu`` from ``AppOptions.txt`` at preflight and return it for the caller
    to record on the run (T204 hardening item 1; ``spikes/principle-i-debugmenu-linux.md``).

    Uses the same ``AppOptions.txt`` path ``HostPlatform.resolve_game_directories()`` already
    resolves for ``EnableTuner`` (research R1). **Never raises, and never refuses a run on this
    setting's value** -- the live spike's evidence is that the tuner's callable surface is
    identical with the debug menu on or off, so there is no parity basis for treating this as a
    gate; a caller wanting to *require* ``DISABLED`` (the spike's own "costs nothing, so require
    it" recommendation for runs feeding trending/metrics/optimization) is free to check ``.state``
    itself and act on it, but that policy decision does not belong inside this reader.

    A missing ``AppOptions.txt`` -- the client rewrites this file on exit, and its directory need
    not pre-exist on a fresh install, exactly the accommodation ``host._shared.read_disk_space``
    already had to make for the same reason -- is reported as ``DebugMenuState.UNKNOWN``, not
    raised: this function must never crash preflight over a file the game itself is free to not
    have written yet. Any other read failure (permission denied, a mid-write partial file) is
    reported the same way, with the underlying error preserved in ``detail``.
    """
    directories = host.resolve_game_directories(home=home)
    path = directories.app_options_path
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return DebugMenuPreflightResult(
            state=DebugMenuState.UNKNOWN,
            app_options_path=path,
            detail=(
                "AppOptions.txt does not exist at the resolved path -- a fresh install, or the "
                "client has not written it yet; EnableDebugMenu cannot be read"
            ),
        )
    except OSError as exc:
        return DebugMenuPreflightResult(
            state=DebugMenuState.UNKNOWN,
            app_options_path=path,
            detail=f"AppOptions.txt could not be read: {exc}",
        )

    raw_value = _read_app_options_entry(text, "EnableDebugMenu")
    if raw_value is None:
        return DebugMenuPreflightResult(
            state=DebugMenuState.UNKNOWN,
            app_options_path=path,
            detail="AppOptions.txt exists but has no EnableDebugMenu entry",
        )
    if raw_value == "1":
        return DebugMenuPreflightResult(state=DebugMenuState.ENABLED, app_options_path=path)
    if raw_value == "0":
        return DebugMenuPreflightResult(state=DebugMenuState.DISABLED, app_options_path=path)
    return DebugMenuPreflightResult(
        state=DebugMenuState.UNKNOWN,
        app_options_path=path,
        detail=f"AppOptions.txt EnableDebugMenu value {raw_value!r} was not recognised",
    )


# --------------------------------------------------------------------------
# T204 hardening item 2 -- the turn-timer-type precondition seam
# --------------------------------------------------------------------------


class TurnTimerReadStatus(Enum):
    """The two-state outcome of one attempt to read this host's turn-timer type."""

    DETERMINED = "determined"
    UNDETERMINABLE = "undeterminable"


#: Resolved turn-timer-type names a live, measured finding confirms mean "no timer is running".
#: Both names are distinct, build-dependent ``DB.MakeHash`` values on the probed build
#: (``TURNTIMER_NONE`` -> ``-1525060181``, ``NO_TURNTIMER`` -> ``-1206781825``) -- a reader is
#: expected to check which name its own build's ``GameInfo`` table actually uses rather than this
#: module assuming one, and both are accepted here precisely because the live finding named both
#: explicitly ("accept either if you cannot tell"). Deliberately *not* a set of raw hash integers:
#: hashes are build-dependent, so any comparison against one belongs inside the injected reader
#: (which has the build's own reverse lookup available), never hard-coded in this module.
_NO_TIMER_NAMES = frozenset({"TURNTIMER_NONE", "NO_TURNTIMER"})


@dataclass(frozen=True)
class TurnTimerReading:
    """One reader's answer to "what turn-timer type is this run using", resolved to a name.

    ``turn_timer_type`` and ``turn_timer_hash`` are independent facts a caller should record
    together -- hash-authoritative (``turn_timer_hash``, the raw ``DB.MakeHash`` result actually
    read) plus a resolved display name (``turn_timer_type``, looked up via the same build's own
    reverse scan of ``GameInfo`` for ``row.Hash == value``) -- because ``DB.MakeHash`` output is
    build-dependent: a stored name resolved through that build's own table is what stays meaningful
    across builds, where a bare stored hash would not (T204 note: "config values should be stored
    hash-authoritative with a resolved display name for the record").

    ``turn_timer_type`` is required exactly when ``status`` is ``DETERMINED``; ``reason`` is
    required exactly when it is not (mirrors ``host.port.CaptureResult``/``InputResult``'s own
    "report the gap, never guess" discipline).
    """

    status: TurnTimerReadStatus
    turn_timer_type: str | None = None
    turn_timer_hash: int | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.status is TurnTimerReadStatus.DETERMINED:
            if self.turn_timer_type is None:
                raise ValueError(
                    "TurnTimerReading.status is DETERMINED but turn_timer_type is None"
                )
        elif not self.reason:
            raise ValueError(f"TurnTimerReading.status is {self.status!r} but no reason was given")


class TurnTimerPreconditionState(Enum):
    """What :func:`turn_timer_preflight` records when the run is allowed to proceed.

    There is no "verified active" member here on purpose: that reading never produces a result at
    all -- it raises (see :func:`turn_timer_preflight`) -- so this type cannot represent "the run
    started anyway with a timer running".
    """

    VERIFIED_NONE = "verified_none"
    UNVERIFIED = "unverified"


@dataclass(frozen=True)
class TurnTimerPreflightResult:
    """What :func:`turn_timer_preflight` hands back when the run is allowed to proceed --
    ``turn_timer_type``/``turn_timer_hash`` are populated exactly when ``state`` is
    ``VERIFIED_NONE`` (the actual name/hash that was confirmed safe); both are ``None`` for
    ``UNVERIFIED``, since nothing was actually read.
    """

    state: TurnTimerPreconditionState
    turn_timer_type: str | None = None
    turn_timer_hash: int | None = None
    reason: str | None = None


def turn_timer_preflight(
    *, read_turn_timer: Callable[[], TurnTimerReading]
) -> TurnTimerPreflightResult:
    """T204 hardening item 2: verify -- never assume -- that this run is not using a turn timer,
    before it starts.

    **Why this matters, concretely.** A live validation-host finding, measured rather than
    inferred: ``GameConfiguration.GetTurnTimerType()`` returned ``TURNTIMER_STANDARD`` in an
    ordinary *single-player* game (``IsAnyMultiplayer``/``IsHotseat``/``IsNetworkMultiplayer`` all
    false) -- one end-turn was issued, the host was then left untouched for 130 s, and the turn
    advanced 1 -> 6 at roughly one turn per 25 s with zero input, indefinitely. **Do not assume
    single-player implies no timer; that host is the counterexample.** A running timer invalidates
    three things at once, silently:

    - FR-014: a turn has no time bound, but a timer imposes exactly one -- a single ~30 s model
      call is already over a 25 s-per-turn budget, so the agent's own thinking time competes with
      a clock nothing in this harness is supposed to have.
    - FR-011: turn-advance verification has only the before/after turn-number readback as
      evidence (``UI.RequestAction`` returns ``nil``); a timer-advanced turn reads back exactly
      like an agent-ended one, so the verification predicate reports success for turns the agent
      never ended -- the identical corruption ``run/turn_cycle.py``'s own auto-end-turn concern
      would have caused, from an entirely different mechanism.
    - FR-015/SC-022: the no-progress backstop's accounting assumes nothing but the harness (via
      the agent's decision or the backstop itself) ever advances a turn; a timer violates that on
      every turn, not just ones where something else already went wrong.

    **The failure mode is that nothing errors.** The run completes, every turn has a decision and
    an advance, ``record_completeness_status`` reads ``complete`` -- the decisions and the advances
    are simply not causally related to each other. A clean-looking, complete dataset that means
    nothing is worse than a failed run, which is why this is a hard preflight gate rather than
    something merely recorded and left for later analysis to notice (contrast
    :func:`debug_menu_preflight` immediately above, which genuinely has no parity basis for being a
    gate -- this does).

    **The seam.** *read_turn_timer* is an injected callable, exactly like every other real
    game-state read this module depends on (``apply_setting``/``read_setting`` above) -- there is
    no live client in this repo to dispatch ``GameConfiguration.GetTurnTimerType()`` /
    ``DB.MakeHash`` against directly, and fabricating that call here would be indistinguishable
    from guessing. A caller wires in the real Lua dispatch once ``observe``/``act`` exposes it.

    **Three outcomes, deliberately asymmetric** -- the same "verified / actively unsafe /
    undeterminable" shape as :func:`build_pin_preflight`'s and :func:`catalog_preflight`'s own hard
    gates above, but with an explicit third state neither of those needs, since this is the one
    precondition in this module a caller genuinely may not be able to read at all yet:

    - Determined, and the resolved name is one of :data:`_NO_TIMER_NAMES` (``TURNTIMER_NONE`` or
      ``NO_TURNTIMER`` -- both accepted, since which name a given build actually uses is not
      assumed here): the precondition holds. Returns a result with ``state=VERIFIED_NONE``.
    - Determined, and the resolved name is anything else (e.g. ``TURNTIMER_STANDARD``,
      ``TURNTIMER_DYNAMIC``, ``TURNTIMER_FIXED``): raises ``PreflightError`` naming the offending
      type and its hash -- the run must not start, because every turn-advance and no-progress
      reading it could ever produce would be untrustworthy, not merely degraded.
    - Undeterminable (the reader could not resolve an answer at all): **does not raise, and does
      not default to treating this as safe** -- assuming no timer is exactly the shape of
      unverified assumption this hardening item exists to replace. Returns a result with
      ``state=UNVERIFIED`` instead, so the run's own record carries an explicit "this precondition
      was never confirmed" fact rather than silently proceeding as though it had passed.
    """
    reading = read_turn_timer()
    if reading.status is TurnTimerReadStatus.UNDETERMINABLE:
        return TurnTimerPreflightResult(
            state=TurnTimerPreconditionState.UNVERIFIED,
            reason=reading.reason,
        )

    assert reading.turn_timer_type is not None  # guaranteed by TurnTimerReading.__post_init__
    if reading.turn_timer_type in _NO_TIMER_NAMES:
        return TurnTimerPreflightResult(
            state=TurnTimerPreconditionState.VERIFIED_NONE,
            turn_timer_type=reading.turn_timer_type,
            turn_timer_hash=reading.turn_timer_hash,
            reason=reading.reason,
        )

    raise PreflightError(
        "a turn timer is active on this host; FR-014 (a turn has no time bound, but a timer "
        "imposes one), FR-011 (a timer-advanced turn is indistinguishable from one the agent "
        "ended), and FR-015/SC-022 (no-progress accounting) all depend on nothing but the "
        "harness or the agent ever advancing a turn -- a clean-looking, 'complete' dataset "
        "produced under a timer would not mean what it claims to",
        detail={
            "turn_timer_type": reading.turn_timer_type,
            "turn_timer_hash": reading.turn_timer_hash,
        },
    )
