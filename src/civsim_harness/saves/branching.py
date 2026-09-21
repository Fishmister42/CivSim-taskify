"""Branch creation (T167), abandonment (T169), and same-build enforcement
(T175) -- FR-033, FR-034, FR-035, FR-036, invariant I12, research R20.

Branch creation loads the parent's turn-start save point (resolved purely
through ``saves/addressing.py``, never the filesystem or the game client
directly -- FR-032) and creates a new run recording ``parent_run_id`` and
``parent_turn`` as its lineage (FR-033). A branch from a save that no
longer exists -- ``missing``, or ``retention_status=removed`` after the
reaper has run -- is rejected with the missing save named, never silently
retargeted to a nearby turn (FR-036). This module never writes to any
record keyed by the parent's own ``run_id``: every write here targets the
*child's* rows only, which is the structural half of parent immutability
(FR-034, I12) this module is responsible for -- the store-side half
(``store/sqlite_adapter.py``) is T168.

**Config inheritance is not re-checked here.** ``config/run_config.py``'s
``load_branch_configuration_yaml`` (T166) is where a restated seed,
civilization, ruleset, mod set, map, or game setting that disagrees with
the parent is rejected; this module receives an already-validated child
``RunConfiguration`` and does not re-derive or re-verify that inheritance.
The parent configuration that check needs is read through
``MatchStore.get_run_configuration`` (T226) by the caller that loads the
branch document, not here.

**Same-build branching (T175, FR-033, research R20).** A branch whose game
build -- platform or version -- differs from its parent's is refused by
default, under the same recorded-acceptance mechanism
``run/preparation.py``'s ``build_pin_preflight`` uses for FR-002 (T072),
applied here to parent -> child instead of seed_set -> client. A
platform-crossing acceptance additionally needs a passing R20 spike result
on record (``BuildAcceptance.r20_spike_ref``, T199) -- a version-only
transition needs none. **Note on ``BuildAcceptance`` fields**:
data-model.md SS1 documents ``is_platform_transition`` and ``r20_spike_ref``
on ``BuildAcceptance`` itself; T017 (this repo's ``models/common.py``,
outside this module's ownership) predates that revision and does not yet
carry them. ``is_platform_transition`` is computed here directly from the
two build strings (``observe.game_build.is_platform_transition``, which
needs no model field), and ``r20_spike_ref`` is read defensively via
``getattr`` so this check is correct today (every platform-crossing
acceptance fails closed, exactly as required until T199 lands) and picks up
the real field the moment ``models/common.py`` gains it, with no change
needed here.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from civsim_harness.errors import PreflightError
from civsim_harness.models.common import (
    AcceptanceId,
    BuildAcceptance,
    CapturePath,
    CatalogVersionRef,
    EventId,
    RunId,
    Timestamp,
)
from civsim_harness.models.config import RunConfiguration
from civsim_harness.models.records import RunEvent, RunEventType, SavePoint
from civsim_harness.models.run import (
    ComparabilityStatus,
    HostSupportTier,
    LifecycleState,
    Run,
    StopResolution,
)
from civsim_harness.observe.game_build import is_platform_transition
from civsim_harness.run.lifecycle import TERMINAL_STATES, transition
from civsim_harness.saves.addressing import require_available_save_point
from civsim_harness.store.completeness import record_completeness_status, refresh_run_completeness
from civsim_harness.store.port import MatchStore

# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class BranchSourceMissingError(PreflightError):
    """The named parent run does not exist at all (FR-033)."""


class BranchBuildMismatchError(PreflightError):
    """FR-033: a branch whose game build differs from its parent's, with no
    ``BuildAcceptance`` covering this exact transition on record.
    """


class BranchPlatformSpikeRequiredError(PreflightError):
    """FR-033, research R20: a platform-crossing branch acceptance exists,
    but carries no passing R20 spike result (``r20_spike_ref``, T199) --
    Civ VI's cross-platform saves are account-gated and version-matched,
    not a foundation for FR-034's identical starting position.
    """


class BranchTargetError(PreflightError):
    """FR-035: an abandonment was requested for a run that is not a branch
    (no recorded ``parent_run_id``), or does not exist.
    """


class BranchStillActiveError(PreflightError):
    """FR-035, T241: abandonment was requested for a branch whose lifecycle
    state says a run loop may still be driving it (``playing``, either
    ``waiting_*`` sub-state, ``interrupted``, ``resuming``) or that never
    reached play at all (``preparing``). Abandoning strips the branch's own
    authoritative record; doing that under a live turn loop would race the
    very writes it is superseding (the same discipline
    ``Runner.resume_from`` applies: pause first -- FR-004/FR-008, a turn is
    never cut short). Nothing was superseded and nothing was recorded.
    """


# --------------------------------------------------------------------------
# Save loading (a small local seam, mirroring resilience/recovery.py's own
# SaveLoader -- not imported from there, for the same reason that module
# gives for not importing from saves/: this is a stable, tiny protocol, and
# neither wave should have to track the other's internal shape).
# --------------------------------------------------------------------------


class SaveLoader(Protocol):
    """What branch creation needs from the save-management layer: load a
    specific save point into the (new) running game client. Which save to
    load is entirely this module's decision -- this protocol only executes
    it.
    """

    async def load(self, save: SavePoint) -> None: ...


# --------------------------------------------------------------------------
# T175 -- same-build branching
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BranchBuildCheck:
    """The outcome of checking a branch's game build against its parent's."""

    matches: bool
    parent_build: str
    child_build: str
    acceptance: BuildAcceptance | None
    is_platform_transition: bool


def _find_acceptance(
    accepted_build_changes: Sequence[BuildAcceptance], *, from_build: str, to_build: str
) -> BuildAcceptance | None:
    """An acceptance covers **exactly** one composite ``from_build ->
    to_build`` transition (mirrors ``run/preparation.py``'s own
    ``_find_acceptance`` for FR-002; duplicated in full rather than
    imported, since that name is private to a different wave's module and
    not meant as a shared entry point).
    """
    for acceptance in accepted_build_changes:
        if acceptance.from_build == from_build and acceptance.to_build == to_build:
            return acceptance
    return None


def check_branch_build(
    *,
    parent_build: str,
    child_build: str,
    accepted_build_changes: Sequence[BuildAcceptance] = (),
) -> BranchBuildCheck:
    """FR-033: refuse a branch whose game build differs from its parent's
    unless a recorded :class:`BuildAcceptance` covers this exact
    transition.

    Raises :class:`BranchBuildMismatchError` when no acceptance covers the
    transition, or :class:`BranchPlatformSpikeRequiredError` when an
    acceptance exists but the transition crosses platform and carries no
    ``r20_spike_ref`` (research R20; see module docstring's field note).
    """
    if child_build == parent_build:
        return BranchBuildCheck(
            matches=True,
            parent_build=parent_build,
            child_build=child_build,
            acceptance=None,
            is_platform_transition=False,
        )

    platform_transition = is_platform_transition(parent_build, child_build)
    acceptance = _find_acceptance(
        accepted_build_changes, from_build=parent_build, to_build=child_build
    )
    if acceptance is None:
        raise BranchBuildMismatchError(
            "branch game build differs from the parent run's, and no BuildAcceptance "
            "covers this exact transition (FR-033, FR-034)",
            detail={
                "parent_build": parent_build,
                "child_build": child_build,
                "is_platform_transition": platform_transition,
            },
        )

    # See module docstring: r20_spike_ref is not yet a field on
    # BuildAcceptance in this repo's models/common.py. Read defensively so
    # a platform-crossing acceptance fails closed today and this check
    # needs no change once the field lands.
    spike_ref = getattr(acceptance, "r20_spike_ref", None)
    if platform_transition and not spike_ref:
        raise BranchPlatformSpikeRequiredError(
            "a platform-crossing branch is refused unless the BuildAcceptance it relies on "
            "carries a passing R20 spike result (r20_spike_ref, T199); a save is not "
            "assumed to resolve identically across platforms (FR-033, FR-034, R20)",
            detail={
                "parent_build": parent_build,
                "child_build": child_build,
                "acceptance_id": acceptance.acceptance_id,
            },
        )

    return BranchBuildCheck(
        matches=False,
        parent_build=parent_build,
        child_build=child_build,
        acceptance=acceptance,
        is_platform_transition=platform_transition,
    )


# --------------------------------------------------------------------------
# T167 -- branch creation
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BranchFrom:
    """A lineage reference: which run and turn a branch starts from
    (contracts/run-configuration.md "Branch configuration", FR-033).
    """

    run_id: RunId
    turn: int


@dataclass(frozen=True)
class BranchSource:
    """Everything about the child run that is resolved at its own preflight
    -- as any run's would be -- and is not derivable from the parent alone.

    **`comparability_status` and `host_platform` have no defaults, and that
    is the point (T226).** An earlier version of this module hard-coded
    ``comparability_status=COMPARABLE`` and set no ``host_platform`` at all,
    so a branch started on a degraded host recorded itself as fully
    comparable -- a silent falsehood in precisely the field that makes
    cross-branch comparison trustworthy (Principle IV: "comparing strategies
    across branches or runs is only meaningful when starting conditions are
    identical"; FR-050/SC-013's "a platform may be less capable, but it may
    not be less honest"). Both are now required of the caller, resolved from
    the same ``evaluate_host_gate`` result and ``HostInfo`` an ordinary
    run's own preparation uses -- there is no way to construct a
    ``BranchSource`` that omits them, so the falsehood is unreachable
    rather than merely discouraged.
    """

    run_id: RunId
    config: RunConfiguration
    game_build: str
    observation_catalog_version: CatalogVersionRef
    action_catalog_version: CatalogVersionRef
    host_support_tier: HostSupportTier
    capture_path: CapturePath
    comparability_status: ComparabilityStatus
    """What this host's own capability gate actually resolved for this run --
    never assumed comparable. A branch that is not comparable must say so."""

    host_platform: dict[str, Any]
    """The platform this branch actually ran on (`os`, `os_version`,
    `session_type`), recorded exactly as the non-branch path records it. An
    empty or absent host platform makes a run's comparability
    uninterpretable after the fact."""


def _event(
    *,
    run_id: RunId,
    event_type: RunEventType,
    occurred_at: Timestamp,
    turn_number: int | None = None,
    detail: dict[str, Any] | None = None,
) -> RunEvent:
    return RunEvent(
        event_id=EventId(uuid.uuid4().hex),
        run_id=run_id,
        turn_number=turn_number,
        event_type=event_type,
        occurred_at=occurred_at,
        detail=dict(detail) if detail else {},
    )


async def create_branch(
    store: MatchStore,
    loader: SaveLoader,
    *,
    branch_from: BranchFrom,
    child: BranchSource,
    accepted_build_changes: Sequence[BuildAcceptance] = (),
    occurred_at: Timestamp,
) -> tuple[Run, SavePoint, RunEvent]:
    """Load the parent's turn-start save and create a new run recording
    ``parent_run_id``/``parent_turn`` as its lineage, emitting
    ``branch_created`` (FR-033, T167).

    Raises :class:`BranchSourceMissingError` when *branch_from.run_id* does
    not exist; :class:`~civsim_harness.saves.addressing.SaveAddressingError`
    when its save point at *branch_from.turn* is missing or was already
    removed (FR-036) -- never retargeted to a nearby turn; and
    :class:`BranchBuildMismatchError` /
    :class:`BranchPlatformSpikeRequiredError` on an unaccepted or
    insufficiently-accepted build difference (T175, FR-033, R20).

    Nothing here writes any record keyed by *branch_from.run_id* -- every
    store write below targets *child.run_id*'s own rows (FR-034, I12).
    """
    parent = store.get_run(branch_from.run_id)
    if parent is None:
        raise BranchSourceMissingError(
            "cannot branch: no such parent run", detail={"parent_run_id": branch_from.run_id}
        )

    build_check = check_branch_build(
        parent_build=parent.game_build,
        child_build=child.game_build,
        accepted_build_changes=accepted_build_changes,
    )

    save_point = require_available_save_point(store, branch_from.run_id, branch_from.turn)

    await loader.load(save_point)

    acceptance_ref: AcceptanceId | None = (
        build_check.acceptance.acceptance_id if build_check.acceptance is not None else None
    )

    child_run = Run(
        run_id=child.run_id,
        config_id=child.config.config_id,
        # T226: `preparing`, not `playing`. A branch's own setup still has to be verified against
        # the *loaded* save before it may play (FR-002/V2), exactly as a fresh run's does, and
        # `failed` is reachable only from `preparing` or `resuming` (data-model.md SS4) -- a branch
        # created straight into `playing` could never be failed by its own V2 check, which would
        # make the verification unenforceable on precisely the runs Principle IV cares most about.
        # The caller drives `preparing -> playing` after that check, on the one code path that
        # already does so for every other run.
        lifecycle_state=LifecycleState.PREPARING,
        # T239: the derivation, never a constructor constant. For a child with no record yet
        # this resolves to UNKNOWN (no save points), and the same one definition
        # (store/completeness.py) re-derives it the moment the branch has a record of its own.
        record_completeness_status=record_completeness_status(store, child.run_id),
        # T226: taken from the caller's own host gate, never assumed. See `BranchSource`.
        comparability_status=child.comparability_status,
        host_platform=dict(child.host_platform),
        observation_catalog_version=child.observation_catalog_version,
        action_catalog_version=child.action_catalog_version,
        parent_run_id=branch_from.run_id,
        parent_turn=branch_from.turn,
        game_build=child.game_build,
        game_build_acceptance_ref=acceptance_ref,
        host_support_tier=child.host_support_tier,
        capture_path=child.capture_path,
    )
    store.create_run(child_run, child.config)

    event = _event(
        run_id=child.run_id,
        event_type=RunEventType.BRANCH_CREATED,
        occurred_at=occurred_at,
        turn_number=branch_from.turn,
        detail={
            "parent_run_id": branch_from.run_id,
            "parent_turn": branch_from.turn,
            "save_point_id": save_point.save_point_id,
            "game_build_acceptance_ref": acceptance_ref,
            "is_platform_transition": build_check.is_platform_transition,
        },
    )
    store.write_run_event(event)

    return child_run, save_point, event


# --------------------------------------------------------------------------
# T169 -- branch abandonment
# --------------------------------------------------------------------------


def abandon_branch(
    store: MatchStore,
    run_id: RunId,
    *,
    turns: Sequence[int],
    reason: str | None = None,
    occurred_at: Timestamp,
) -> RunEvent:
    """Record a ``branch_abandoned`` event and mark every turn in *turns*
    superseded via ``mark_turn_superseded`` -- **never deleted** (FR-035).

    *turns* names the turn numbers whose authoritative attempt on this
    branch is being given up; each is resolved to its current authoritative
    ``attempt_index`` via ``get_turn_cycle`` before being superseded, so the
    caller only needs to know which turns, not which attempt. A turn with
    no authoritative attempt recorded is skipped (there is nothing to
    supersede), not treated as an error.

    Raises :class:`BranchTargetError` when *run_id* does not exist or is
    not a branch at all (no recorded ``parent_run_id``) -- abandonment is a
    branch-lifecycle action, not a way to touch an arbitrary run's turns.
    """
    run = store.get_run(run_id)
    if run is None:
        raise BranchTargetError("cannot abandon: no such run", detail={"run_id": run_id})
    if run.parent_run_id is None:
        raise BranchTargetError(
            "cannot abandon: this run has no recorded parent, so it is not a branch (FR-035)",
            detail={"run_id": run_id},
        )

    superseded: list[dict[str, int]] = []
    for turn_number in turns:
        record = store.get_turn_cycle(run_id, turn_number, authoritative_only=True)
        if record is None:
            continue
        attempt_index = record.turn_cycle.attempt_index
        store.mark_turn_superseded(run_id, turn_number, attempt_index)
        superseded.append({"turn_number": turn_number, "attempt_index": attempt_index})

    detail: dict[str, Any] = {"superseded_turns": superseded}
    if reason is not None:
        detail["reason"] = reason

    event = _event(
        run_id=run_id,
        event_type=RunEventType.BRANCH_ABANDONED,
        occurred_at=occurred_at,
        detail=detail,
    )
    store.write_run_event(event)

    # T239: superseding is one of the moments record_completeness_status can change, and an
    # abandoned branch's field must be the derivation -- with its own attempts superseded, that
    # is honestly `has_gaps` (nothing authoritative remains to trend on), or `unknown` for a
    # branch that never played a turn of its own.
    refresh_run_completeness(store, run_id)
    return event


#: The lifecycle states :func:`abandon_branch_run` accepts (T241). ``paused`` is the one
#: non-terminal state with no live turn loop attached by contract (a pause lands only at a turn
#: boundary); the two terminal states are already over. Everything else is refused -- see
#: :class:`BranchStillActiveError`.
_ABANDONABLE_STATES: frozenset[LifecycleState] = frozenset(
    {LifecycleState.PAUSED, LifecycleState.FINISHED, LifecycleState.FAILED}
)


@dataclass(frozen=True)
class BranchAbandonment:
    """What :func:`abandon_branch_run` did, for the operator surface to report (T241)."""

    run: Run
    """The branch's ``Run`` as persisted after the abandonment -- terminal, with its
    ``record_completeness_status`` re-derived (T239)."""

    event: RunEvent
    """The recorded ``branch_abandoned`` event (FR-035)."""

    superseded_turns: tuple[int, ...]
    """The turn numbers whose authoritative attempts were marked superseded -- never deleted."""


def abandon_branch_run(
    store: MatchStore,
    run_id: RunId,
    *,
    reason: str | None = None,
    occurred_at: Timestamp,
) -> BranchAbandonment:
    """The production abandonment path (T241, FR-035): supersede every one of the branch's own
    authoritative turns, record ``branch_abandoned``, and leave the run's lifecycle honestly
    terminal.

    :func:`abandon_branch` is the mechanism (event + supersede, T169); this function is its one
    production caller's policy:

    - **Which turns**: every turn the branch itself ever attempted -- the distinct turn numbers
      across its own FR-007 save points. :func:`abandon_branch` already skips a turn with no
      authoritative attempt, so the trailing in-flight turn of a paused branch needs no special
      casing here. A turn some *further* branch recorded as its own lineage point is refused by
      the store itself (parent immutability, FR-034/I12) and surfaces loudly rather than being
      worked around: a branch that is itself a parent cannot have its lineage point stripped.
    - **Which lifecycle states**: ``paused``, ``finished``, or ``failed`` only
      (:class:`BranchStillActiveError` otherwise -- see that error's docstring).
    - **Terminal honesty**: a branch abandoned from ``paused`` must not linger non-terminal
      forever with every attempt superseded -- a zombie claiming to be resumable. data-model.md
      SS4's only legal path out of ``paused`` runs through ``playing``, so the two transitions
      ``paused -> playing -> finished`` are recorded (the same first edge
      ``Runner.resume_from`` drives for a paused rewind), the second carrying
      ``stop_resolution=operator_stop`` -- an operator ended this run, which is exactly what an
      abandonment is -- and a detail naming the abandonment. An already-terminal branch keeps
      the terminal state it earned.
    - **T239**: ``record_completeness_status`` is re-derived and persisted by
      :func:`abandon_branch` itself, so the abandoned branch's field is the derivation, never a
      leftover.
    """
    run = store.get_run(run_id)
    if run is None:
        raise BranchTargetError("cannot abandon: no such run", detail={"run_id": run_id})
    if run.parent_run_id is None:
        raise BranchTargetError(
            "cannot abandon: this run has no recorded parent, so it is not a branch (FR-035)",
            detail={"run_id": run_id},
        )
    if run.lifecycle_state not in _ABANDONABLE_STATES:
        raise BranchStillActiveError(
            "cannot abandon: this branch's lifecycle state says a run loop may still be driving "
            "it -- pause or stop it first (FR-004/FR-008: a turn is never cut short, and "
            "abandonment must not race a live turn loop's own writes)",
            detail={"run_id": run_id, "lifecycle_state": run.lifecycle_state.value},
        )

    turns = sorted({save.turn_number for save in store.list_save_points(run_id)})
    event = abandon_branch(store, run_id, turns=turns, reason=reason, occurred_at=occurred_at)

    if run.lifecycle_state not in TERMINAL_STATES:
        # paused -> playing -> finished; see the docstring. Each edge is recorded and persisted
        # separately, exactly as `run/runner.py` records its own transitions.
        interim, resume_event = transition(
            run,
            LifecycleState.PLAYING,
            occurred_at=occurred_at,
            detail={"reason": "branch_abandoned: paused has no direct edge to finished (SS4)"},
        )
        store.write_run_event(resume_event)
        store.update_run(run_id, lifecycle_state=interim.lifecycle_state)

        finish_detail: dict[str, Any] = {"reason": "branch_abandoned"}
        if reason is not None:
            finish_detail["abandon_reason"] = reason
        finished, finish_event = transition(
            interim,
            LifecycleState.FINISHED,
            occurred_at=occurred_at,
            stop_resolution=StopResolution.OPERATOR_STOP,
            detail=finish_detail,
        )
        store.write_run_event(finish_event)
        store.update_run(
            run_id,
            lifecycle_state=finished.lifecycle_state,
            ended_at=finished.ended_at,
            stop_resolution=finished.stop_resolution,
        )
        # The lifecycle just left the actively-playing set, which widens turn_gaps' checked
        # range (a trailing attempted-but-unrecorded turn is now a real gap) -- re-derive.
        refresh_run_completeness(store, run_id)

    final_run = store.get_run(run_id)
    assert final_run is not None  # it existed above and nothing here deletes runs
    superseded = tuple(
        entry["turn_number"] for entry in event.detail.get("superseded_turns", [])
    )
    return BranchAbandonment(run=final_run, event=event, superseded_turns=superseded)
