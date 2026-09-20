"""Contract tests for the composite build pin (T064, T065).

**T064 -- V10** (`run/preparation.py::build_pin_preflight`): a client build differing from the seed
set's `game_build` fails the run before turn 1 with the mismatch recorded; a matching
`BuildAcceptance` permits it while the caller records `game_build_acceptance_ref` on the run
(FR-002, FR-031).

**T065 -- the platform half of the composite pin**: a *platform* difference fails preflight exactly
as a version difference does, and an acceptance of `win/1.0.12.9 -> win/1.0.12.11` must not also
permit `win -> mac` (research R20). A seed set carrying any accepted build change reports as
non-uniform.

Also covers the branch half of the same mechanism (`saves/branching.py::check_branch_build`,
FR-033): a platform-crossing branch acceptance additionally requires a passing R20 spike result
(`BuildAcceptance.r20_spike_ref`) -- enforced at this call site, not by the `BuildAcceptance` model
itself, since `BuildAcceptance.is_platform_transition` is recomputed by a model validator from
`from_build`/`to_build` and cannot be constructed to misrepresent what it crosses.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from civsim_harness.errors import BuildMismatchError
from civsim_harness.models.common import AcceptanceId, BuildAcceptance, ModRef, SeedSetId
from civsim_harness.models.config import SeedSet
from civsim_harness.run.preparation import build_pin_preflight
from civsim_harness.saves.branching import (
    BranchBuildMismatchError,
    BranchPlatformSpikeRequiredError,
    check_branch_build,
)

NOW = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)

_WIN_9 = "win/1.0.12.9"
_WIN_11 = "win/1.0.12.11"
_MAC_9 = "mac/1.0.12.9"


def _seed_set(
    *, game_build: str = _WIN_9, accepted_build_changes: list[BuildAcceptance] | None = None
) -> SeedSet:
    return SeedSet(
        seed_set_id=SeedSetId("shuffle-classic-2026q3"),
        name="shuffle-classic-2026q3",
        seeds=["1849275663"],
        civilization="CIVILIZATION_ROME",
        leader="LEADER_TRAJAN",
        ruleset="RULESET_EXPANSION_2",
        mod_set=[ModRef(id="bbg-community-balance", version="4.2.1")],
        game_build=game_build,
        accepted_build_changes=accepted_build_changes or [],
        created_at=NOW,
    )


def _acceptance(
    *,
    from_build: str,
    to_build: str,
    r20_spike_ref: str | None = None,
    acceptance_id: str = "acc_01",
) -> BuildAcceptance:
    return BuildAcceptance(
        acceptance_id=AcceptanceId(acceptance_id),
        from_build=from_build,
        to_build=to_build,
        accepted_by="researcher",
        accepted_at=NOW,
        reason="Patch notes affect only multiplayer matchmaking.",
        r20_spike_ref=r20_spike_ref,
    )


# --------------------------------------------------------------------------
# T064 -- V10: version-only mismatch/acceptance (run/preparation.py)
# --------------------------------------------------------------------------


def test_identical_build_matches_with_no_acceptance_needed() -> None:
    seed_set = _seed_set(game_build=_WIN_9)

    result = build_pin_preflight(seed_set, _WIN_9)

    assert result.matches is True
    assert result.acceptance is None
    assert result.is_platform_transition is False


def test_a_differing_build_fails_the_run_before_turn_1_with_the_mismatch_recorded() -> None:
    seed_set = _seed_set(game_build=_WIN_9)

    with pytest.raises(BuildMismatchError) as excinfo:
        build_pin_preflight(seed_set, _WIN_11)

    assert excinfo.value.detail["from_build"] == _WIN_9
    assert excinfo.value.detail["to_build"] == _WIN_11
    assert excinfo.value.detail["is_platform_transition"] is False


def test_a_matching_build_acceptance_permits_the_run() -> None:
    acceptance = _acceptance(from_build=_WIN_9, to_build=_WIN_11)
    seed_set = _seed_set(game_build=_WIN_9, accepted_build_changes=[acceptance])

    result = build_pin_preflight(seed_set, _WIN_11)  # must not raise

    assert result.matches is False
    assert result.acceptance is acceptance


def test_the_acceptance_id_is_what_a_run_records_as_game_build_acceptance_ref() -> None:
    """FR-002: "every dependent run records game_build and game_build_acceptance_ref" --
    `BuildPinResult.acceptance.acceptance_id` is exactly the value a caller building the `Run`
    record stores there."""
    acceptance = _acceptance(from_build=_WIN_9, to_build=_WIN_11, acceptance_id="acc_the_one")
    seed_set = _seed_set(game_build=_WIN_9, accepted_build_changes=[acceptance])

    result = build_pin_preflight(seed_set, _WIN_11)

    game_build_acceptance_ref = result.acceptance.acceptance_id
    assert game_build_acceptance_ref == AcceptanceId("acc_the_one")


# --------------------------------------------------------------------------
# T065 -- the platform half of the composite pin (run/preparation.py)
# --------------------------------------------------------------------------


def test_a_platform_difference_fails_preflight_exactly_as_a_version_difference_does() -> None:
    seed_set = _seed_set(game_build=_WIN_9)

    with pytest.raises(BuildMismatchError) as excinfo:
        build_pin_preflight(seed_set, _MAC_9)  # same version, different platform

    assert excinfo.value.detail["from_build"] == _WIN_9
    assert excinfo.value.detail["to_build"] == _MAC_9
    assert excinfo.value.detail["is_platform_transition"] is True


def test_a_version_only_acceptance_does_not_also_permit_a_platform_change() -> None:
    """"accepting a version bump never implicitly accepts a platform change" (R20)."""
    version_only_acceptance = _acceptance(from_build=_WIN_9, to_build=_WIN_11)
    seed_set = _seed_set(game_build=_WIN_9, accepted_build_changes=[version_only_acceptance])

    with pytest.raises(BuildMismatchError) as excinfo:
        build_pin_preflight(seed_set, _MAC_9)

    assert excinfo.value.detail["is_platform_transition"] is True


def test_a_platform_crossing_acceptance_does_not_also_permit_a_different_version() -> None:
    """The converse: an accepted win -> mac transition does not cover win/1.0.12.9 ->
    mac/1.0.12.11 -- the acceptance is scoped to one exact composite transition (contracts/
    run-configuration.md "The build pin")."""
    platform_only_acceptance = _acceptance(
        from_build=_WIN_9, to_build=_MAC_9, r20_spike_ref="r20-spike-001"
    )
    seed_set = _seed_set(game_build=_WIN_9, accepted_build_changes=[platform_only_acceptance])

    with pytest.raises(BuildMismatchError):
        build_pin_preflight(seed_set, "mac/1.0.12.11")


def test_a_seed_set_with_no_accepted_build_changes_is_uniform() -> None:
    seed_set = _seed_set(game_build=_WIN_9, accepted_build_changes=[])
    assert seed_set.is_uniform is True


def test_a_seed_set_carrying_any_accepted_build_change_is_not_uniform() -> None:
    acceptance = _acceptance(from_build=_WIN_9, to_build=_WIN_11)
    seed_set = _seed_set(game_build=_WIN_9, accepted_build_changes=[acceptance])
    assert seed_set.is_uniform is False


# --------------------------------------------------------------------------
# BuildAcceptance.is_platform_transition is recomputed, never trusted from the caller
# --------------------------------------------------------------------------


def test_build_acceptance_is_platform_transition_is_recomputed_not_trusted() -> None:
    """A caller cannot construct a `BuildAcceptance` that misrepresents what it crosses --
    passing `is_platform_transition=False` for a genuine platform-crossing transition is
    silently corrected by the model's own validator, not accepted at face value."""
    acceptance = BuildAcceptance(
        acceptance_id=AcceptanceId("acc_lie"),
        from_build=_WIN_9,
        to_build=_MAC_9,
        accepted_by="researcher",
        accepted_at=NOW,
        reason="attempted misrepresentation",
        is_platform_transition=False,  # a lie
    )
    assert acceptance.is_platform_transition is True


def test_build_acceptance_is_platform_transition_false_for_a_version_only_transition() -> None:
    acceptance = _acceptance(from_build=_WIN_9, to_build=_WIN_11)
    assert acceptance.is_platform_transition is False


# --------------------------------------------------------------------------
# Branch build check (saves/branching.py::check_branch_build, FR-033) --
# the r20_spike_ref gate lives here, not on the BuildAcceptance model
# --------------------------------------------------------------------------


def test_branch_same_build_matches_with_no_acceptance_needed() -> None:
    result = check_branch_build(parent_build=_WIN_9, child_build=_WIN_9)
    assert result.matches is True


def test_branch_differing_build_is_refused_without_a_covering_acceptance() -> None:
    with pytest.raises(BranchBuildMismatchError):
        check_branch_build(parent_build=_WIN_9, child_build=_WIN_11)


def test_branch_version_only_transition_needs_no_spike_ref() -> None:
    acceptance = _acceptance(from_build=_WIN_9, to_build=_WIN_11, r20_spike_ref=None)

    result = check_branch_build(
        parent_build=_WIN_9, child_build=_WIN_11, accepted_build_changes=[acceptance]
    )  # must not raise

    assert result.matches is False
    assert result.is_platform_transition is False


def test_branch_platform_crossing_acceptance_without_a_spike_ref_is_refused() -> None:
    """research R20: Civ VI's cross-platform saves are not assumed to resolve identically --
    a platform-crossing branch acceptance with no recorded passing R20 spike result must be
    refused, never silently treated the same as a version-only acceptance."""
    acceptance = _acceptance(from_build=_WIN_9, to_build=_MAC_9, r20_spike_ref=None)

    with pytest.raises(BranchPlatformSpikeRequiredError):
        check_branch_build(
            parent_build=_WIN_9, child_build=_MAC_9, accepted_build_changes=[acceptance]
        )


def test_branch_platform_crossing_acceptance_with_a_passing_spike_ref_is_permitted() -> None:
    acceptance = _acceptance(from_build=_WIN_9, to_build=_MAC_9, r20_spike_ref="r20-spike-001")

    result = check_branch_build(
        parent_build=_WIN_9, child_build=_MAC_9, accepted_build_changes=[acceptance]
    )  # must not raise

    assert result.matches is False
    assert result.is_platform_transition is True
    assert result.acceptance is acceptance
