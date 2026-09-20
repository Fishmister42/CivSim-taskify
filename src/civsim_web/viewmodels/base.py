"""View models shared across every user story (T009, data-model.md SS1, SS2, SS4, SS13).

Every model here is frozen and ``extra='forbid'``. Both matter: a view model is
the *same object* the JSON body and the HTML template are built from, so a
field added at runtime would appear in one reader's view and not necessarily
the other's -- the precise asymmetry Principle VI forbids. Freezing makes
"there is no second, hand-maintained API shape" structural rather than stated.

``UnavailableField`` is the one idea worth reading before the models. Three separate
requirements -- FR-011 (a value outside the parity boundary), FR-025 (a field a
historical run's schema version predates), and plan.md C1 (a field the
published port cannot currently reach) -- all resolve to the same rendering
obligation: say *unavailable*, never zero, never blank, never silently omitted.
These models express that as an explicit ``None`` plus a named entry in
``unavailable``, so a reader can always tell "we know this is missing and why"
from "nobody thought about it".
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "HealthState",
    "HealthStatus",
    "InterventionInfo",
    "PanelRegistryVersion",
    "RunSummaryView",
    "UnavailableField",
    "ViewModel",
]


class ViewModel(BaseModel):
    """Base for every view model: frozen, closed, serialized identically twice."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class UnavailableField(ViewModel):
    """A field this response knows it cannot show, and why (FR-011, FR-025).

    Rendered as an explicit marker in both readers. The alternative -- omitting
    the key from the JSON and the row from the page -- is what UP-005 calls
    rendering absence as confirmed fact, and it is exactly what a reader cannot
    distinguish from "this run genuinely had no such value".
    """

    field: str
    reason: str


class HealthState(StrEnum):
    """Run health, as FR-003 words it, plus two states the vocabulary lacks.

    The first seven are FR-003's verbatim vocabulary. The last two exist
    because 002's published lifecycle state machine has states FR-003's list
    does not cover, and the honest-state rule (UP-005) forbids resolving them
    into a neighbour that would read as confirmed fact:

    - ``paused`` -- 002's ``Run.lifecycle_state`` includes ``paused`` (a run
      that stopped advancing, e.g. on model-chain exhaustion per its FR-042).
      data-model.md SS2's derivation rule maps eight of nine lifecycle states
      and omits this one. It cannot be folded into ``running`` (the run is not
      advancing) and it must not be folded into ``stalled``, whose validation
      rule permits that value *only* on a ``hang_detected`` /
      ``unresponsive_detected`` event.
    - ``unknown`` -- a ``lifecycle_state`` this feature's code does not
      recognise, e.g. one a later 002 schema version adds. Failing closed to an
      explicit "unknown" is the same discipline ``CaptureView`` applies to an
      unrecognised ``screening_status``.

    Both are recorded as findings against the spec rather than quiet
    inventions; see the run report for this feature's implementation.
    """

    RUNNING = "running"
    WAITING_ON_MODEL = "waiting_on_model"
    WAITING_ON_GAME = "waiting_on_game"
    STALLED = "stalled"
    CRASHED = "crashed"
    RESUMED = "resumed"
    FINISHED = "finished"
    PAUSED = "paused"
    UNKNOWN = "unknown"


class HealthStatus(ViewModel):
    """Derived run health (data-model.md SS2).

    Derived, never a raw store field -- but derived *only* from
    ``Run.lifecycle_state`` and 002's own ``RunEvent``s. This feature runs no
    timer of its own and forms no independent judgment about whether a run is
    stuck (research R7, invariant V3): a second detector that could disagree
    with 002's is the asymmetry Principle VI exists to prevent. The derivation
    itself lives in ``civsim_web.health.derive``.
    """

    state: HealthState
    reason_event_id: str | None = None
    since: datetime | None = None
    lifecycle_state: str
    """The raw ``Run.lifecycle_state`` this was derived from, shown verbatim so
    a reader can always see the store's own word alongside our reading of it."""


class InterventionInfo(ViewModel):
    """What the user needs to act *elsewhere* (FR-027, UP-010, invariant V9).

    **This model has no action fields, and that is the point.** No start,
    pause, stop, or branch target; no URL a template could POST to; no callable
    of any kind. The interface reports and never acts, so there is nothing here
    for a future contributor to wire a control to -- FR-026 is enforced by the
    shape of the type rather than by a reviewer noticing a button.
    """

    run_id: str
    lifecycle_status: str
    last_known_good_turn: int | None = None
    last_known_good_save_id: str | None = None
    last_known_good_save_name: str | None = None
    unavailable: tuple[UnavailableField, ...] = ()


class PanelRegistryVersion(ViewModel):
    """The registry version that decided how a response was displayed (SS13).

    Carried on every top-level response alongside the run's own
    ``observation_catalog_version`` / ``action_catalog_version``, so a rendered
    panel traces to *both* the game-side catalog version that produced the
    underlying data and the registry version that decided to show it
    (invariant V10, SC-005's "auditable after the fact").
    """

    version: str
    content_hash: str
    panel_ids: tuple[str, ...] = ()


class RunSummaryView(ViewModel):
    """A run as a catalog row and as a live-view header (SS1; FR-018, FR-001).

    Several columns FR-018 requires -- seed, civilization, leader, ruleset,
    model -- live on 002's ``RunConfiguration``, which the published
    ``MatchStore`` port has no operation to resolve from a ``Run.config_id``
    (plan.md Complexity Tracking C1, restated in ``store_client/port.py``).
    They are therefore optional here and, when a store cannot supply them, are
    named in ``unavailable`` rather than left blank: an empty civilization
    column that might mean "no civilization" is worse than one that says why it
    is missing.
    """

    run_id: str
    lifecycle_state: str
    health: HealthStatus
    record_completeness_status: str
    comparability_status: str

    # FR-018 catalog columns (see the class docstring on optionality).
    seed: str | None = None
    civilization: str | None = None
    leader: str | None = None
    ruleset: str | None = None
    model_primary: str | None = None

    turn_count: int = 0
    outcome_metrics: dict[str, float] = Field(default_factory=dict)
    started_at: datetime | None = None
    ended_at: datetime | None = None

    # Branch lineage, display-only -- this feature never creates or walks a
    # branch, it only shows the lineage 002 already recorded (Principle IV is
    # not triggered here; plan.md Constitution Check).
    parent_run_id: str | None = None
    parent_turn: int | None = None

    # Invariant V10: the game-side catalog versions in force while this run
    # played, recorded verbatim next to the registry version on the response.
    observation_catalog_version: str | None = None
    action_catalog_version: str | None = None

    unavailable: tuple[UnavailableField, ...] = ()
