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
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from civsim_harness.capability.loader import Catalog
from civsim_harness.errors import BuildMismatchError, PreflightError
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
