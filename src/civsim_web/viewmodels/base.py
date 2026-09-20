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

from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "COMPLETE",
    "HealthState",
    "HealthStatus",
    "InterventionInfo",
    "PanelRegistryVersion",
    "RunSummaryView",
    "TrendEligibility",
    "TrendIneligibleReason",
    "UnavailableField",
    "ViewModel",
    "derive_trend_eligibility",
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


#: The one value of 002's ``Run.record_completeness_status`` that means the
#: turn-by-turn record is whole. ``has_gaps`` and ``unknown`` are the other two
#: the schema publishes; anything else is a value this code does not recognise.
COMPLETE = "complete"


class TrendIneligibleReason(StrEnum):
    """Why a run's results may not be used as trending input (Principle III).

    Each value is a *separate* disqualification and several can hold at once, so
    ``TrendEligibility.reasons`` is a list rather than a single value: "this run
    is marked ``has_gaps`` **and** the store lists turn 7 as missing" is two
    facts, and collapsing them would hide one of them.
    """

    NOT_ASSESSED = "not_assessed"
    RECORD_NOT_COMPLETE = "record_completeness_status_is_not_complete"
    TURN_GAPS_RECORDED = "turn_gaps_recorded"
    COMPLETENESS_UNRECOGNIZED = "record_completeness_status_unrecognized"


class TrendEligibility(ViewModel):
    """Whether this run may be used as trending input, and why not (Principle III).

    The constitution's Principle III is not advisory and its second sentence is
    the whole of this model's reason to exist:

        A run's results MUST NOT be used for trending, datamining, or
        optimization input if its turn-by-turn record has gaps.

    Three properties make that structural rather than remembered:

    1. **The default is ineligible.** ``TrendEligibility()`` -- the value a
       caller gets by forgetting to assess a run -- is ``eligible=False`` with
       ``NOT_ASSESSED``. A run reaches a trend line only by something having
       positively established that it may, never by nobody having checked.
    2. **Two independent store facts must agree, and disagreement fails
       closed.** ``Run.record_completeness_status`` is read *verbatim* and never
       re-derived (invariant V5) -- but ``MatchStore.turn_gaps()`` is a second,
       separately published read of the same question, and a run the store calls
       ``complete`` while also listing turn numbers with no authoritative
       attempt is quarantined on the strength of the gap list. This is not a
       competing completeness judgment: it adds no opinion of its own, it simply
       refuses to average a run that the store describes two ways.
    3. **An unrecognised status is a disqualification, not a pass.** The same
       fail-closed discipline ``CaptureView`` applies to an unknown
       ``screening_status``: a future 002 schema value must not silently
       qualify a run this code was never updated to understand.

    ``eligible`` being false does **not** hide the run. FR-016 requires marking,
    not hiding: the run still appears in the catalog and in
    ``ComparisonView.runs`` with this explanation attached, and is excluded only
    from the series and divergence computation (FR-021).
    """

    eligible: bool = False
    assessed: bool = False
    reasons: tuple[TrendIneligibleReason, ...] = (TrendIneligibleReason.NOT_ASSESSED,)
    record_completeness_status: str | None = None
    """Verbatim from ``Run`` -- shown beside the verdict so a reader sees the
    store's own word, not only our reading of it."""
    gapped_turns: tuple[int, ...] = ()
    """Verbatim from ``turn_gaps()``. Non-empty is itself a disqualification."""
    explanation: str = ""


#: Said in the words the page shows, so the user and the directing session read
#: the same sentence about the same run (Principle VI).
_TREND_EXPLANATIONS: dict[TrendIneligibleReason, str] = {
    TrendIneligibleReason.NOT_ASSESSED: (
        "Trend eligibility was not assessed for this response. A run is never "
        "treated as trend-eligible by default (Principle III)."
    ),
    TrendIneligibleReason.RECORD_NOT_COMPLETE: (
        "The store records this run's turn-by-turn record as incomplete, so its "
        "results may not be used for trending (Principle III)."
    ),
    TrendIneligibleReason.TURN_GAPS_RECORDED: (
        "The store lists turn numbers with no authoritative attempt for this "
        "run, so its results may not be used for trending (Principle III)."
    ),
    TrendIneligibleReason.COMPLETENESS_UNRECOGNIZED: (
        "This run's recorded completeness status is a value this interface does "
        "not recognise. An unrecognised status is treated as incomplete rather "
        "than as complete-by-default."
    ),
}

ELIGIBLE_EXPLANATION = (
    "The store records this run's turn-by-turn record as complete and lists no "
    "missing turns, so it may be used as trending input (Principle III)."
)

#: The two non-``complete`` values 002's schema publishes. Listed so a third,
#: future value is distinguishable from these and gets its own reason rather
#: than being folded into one that asserts something nobody recorded.
_KNOWN_INCOMPLETE = ("has_gaps", "unknown")


def derive_trend_eligibility(
    *,
    record_completeness_status: str | None,
    gapped_turns: Sequence[int] | None,
    assessed: bool = True,
) -> TrendEligibility:
    """Decide whether a run's results may be used as trending input.

    ``gapped_turns`` is ``MatchStore.turn_gaps(run_id)``'s answer, read rather
    than computed. Passing ``None`` means the gap read was not performed, which
    is itself a disqualification -- there is no "probably fine" branch here.
    """
    if not assessed:
        return TrendEligibility(
            record_completeness_status=record_completeness_status,
            explanation=_TREND_EXPLANATIONS[TrendIneligibleReason.NOT_ASSESSED],
        )

    reasons: list[TrendIneligibleReason] = []
    status = (record_completeness_status or "").strip()
    if status != COMPLETE:
        reasons.append(TrendIneligibleReason.RECORD_NOT_COMPLETE)
        if status not in _KNOWN_INCOMPLETE:
            reasons.append(TrendIneligibleReason.COMPLETENESS_UNRECOGNIZED)

    if gapped_turns is None:
        reasons.append(TrendIneligibleReason.TURN_GAPS_RECORDED)
        gaps: tuple[int, ...] = ()
    else:
        gaps = tuple(sorted(int(turn) for turn in gapped_turns))
        if gaps and TrendIneligibleReason.TURN_GAPS_RECORDED not in reasons:
            reasons.append(TrendIneligibleReason.TURN_GAPS_RECORDED)

    if not reasons:
        return TrendEligibility(
            eligible=True,
            assessed=True,
            reasons=(),
            record_completeness_status=record_completeness_status,
            gapped_turns=gaps,
            explanation=ELIGIBLE_EXPLANATION,
        )

    return TrendEligibility(
        eligible=False,
        assessed=True,
        reasons=tuple(reasons),
        record_completeness_status=record_completeness_status,
        gapped_turns=gaps,
        explanation=" ".join(_TREND_EXPLANATIONS[reason] for reason in reasons),
    )


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

    **``trend_eligibility`` is the Principle III gate** (T050). It defaults to
    *ineligible, not assessed*: a summary built without the gap read -- the live
    glance, for instance, which asks a different question -- says so, and never
    presents an unchecked run as fit to average. Only the catalog projection
    (``store_client/catalog.py``), which performs the ``turn_gaps()`` read,
    produces an assessed verdict.
    """

    run_id: str
    lifecycle_state: str
    health: HealthStatus
    record_completeness_status: str
    comparability_status: str
    trend_eligibility: TrendEligibility = Field(default_factory=TrendEligibility)

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
