"""The within-turn decision-step loop (T110, T112; research R14).

For step *n* = 1, 2, 3 ... **unbounded**: assemble a fresh observation and capture, obtain one
decision and its reasoning from the model provider, execute it, verify its effect, then assemble a
*new* observation reflecting that effect before asking for the next decision. The agent is never
asked to commit to a later decision before it has seen the result of the earlier one (FR-008,
invariants I13, I14).

**The loop exits on exactly two conditions and no others** (T112): the agent's own decision names
the declared end-turn action (``outcome = ended_by_agent``), or the no-progress backstop
(``run/no_progress.py``) trips (``outcome = ended_on_no_progress``). :func:`run_decision_loop`'s
body is a single ``while True:`` with exactly two ``return`` statements, both guarded by one of
those two conditions -- there is no wall-clock check, no step-count check, and no cost check
anywhere in this module, and there must never be one added. ``tests/unit/test_no_truncation.py``
(T061) is the negative test that a 500-step productive turn completes untouched; every
plausible-sounding guard rail it would catch (a turn timeout "for safety", a step cap "to bound
cost") is a regression, not a feature (invariant I16).

**One observation assembly serves two purposes, which is what makes the loop a loop.** After a
step's decision is dispatched (and, if authorized, executed), the loop takes exactly one fresh
observation: it is handed to :func:`~civsim_harness.act.verify.verify_execution` as that step's
``post_observation`` (research R14's "verify" phase: reads back game state against the action's
declared predicate) *and* it becomes the **next** step's own ``observation`` -- the board the agent
looks at before its next decision, already reflecting the effect of the one it just made. This
holds even when the decision was rejected at dispatch (never executed): invariant I14 still
requires a genuinely fresh read for the next step, so no branch here ever reuses a prior step's
observation, capture, or their ids.

**Injected collaborators.** Two things this module needs have no existing bound API in this
codebase to call directly, because "how do you actually read live game state" and "how do you
actually dispatch an authorized action" are Nexus/fake-transport concerns outside this wave's
ownership (``observe/``, ``act/`` supply the *pure* pieces -- schema validation, availability/
verification predicate evaluation -- not the I/O). Both are accepted as injected async callables
(:class:`ObservationReader`, :class:`ActionExecutor`), matching this codebase's established seam
pattern (``saves.save_game.SaveCapability``, ``resilience.recovery.SaveLoader``,
``run.preparation``'s ``apply_setting``/``read_setting``). Tests and real callers alike wire in
whatever they need -- a scripted in-memory game, or eventually a real ``NexusClient`` --
implementing these two narrow shapes.

**FR-042 -- no fabricated decision when none can be obtained.** When the provider (or whatever
retry/fallback chain sits in front of it -- that layer is ``provider/chain.py``, a different
wave's task; this module treats the ``ModelProvider`` it is handed as already representing "the
one call for this step") returns anything other than ``CallOutcome.DECISION_RETURNED``, this
module writes the failed ``ModelCall`` directly (it will never ride along in a persisted
``TurnCycleRecord``, since no ``DecisionStep`` was produced -- ``store.port``'s own docstring:
"independent of whether it produced a decision step") and raises
:class:`~civsim_harness.errors.ProviderChainExhausted`. Nothing in this module ever substitutes a
default or heuristic move for the missing decision.
"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Protocol

from civsim_harness.act.camera import CAMERA_ACTION_DECLARATION_IDS, validate_camera_action
from civsim_harness.act.dispatch import (
    DispatchStatus,
    dispatch_action,
    rejection_to_execution,
)
from civsim_harness.act.prompts import PromptRouteStatus, route_prompt
from civsim_harness.act.verify import verify_execution
from civsim_harness.agent.context import assemble_context, select_screened_images
from civsim_harness.agent.decisions import RESPONSE_SCHEMA, build_decision
from civsim_harness.capability.registry import CapabilityRegistry
from civsim_harness.errors import (
    HarnessError,
    ObservationAssemblyError,
    ParityViolation,
    ProviderChainExhausted,
)
from civsim_harness.host.detect import HostInfo
from civsim_harness.host.port import GameWindow, HostPlatform
from civsim_harness.models.common import (
    CatalogVersionRef,
    DecisionId,
    DecisionStepId,
    DeclarationId,
    LuaContext,
    ModelCallId,
    ModelRef,
    ObservationId,
    RunId,
    Timestamp,
    TurnCycleId,
)
from civsim_harness.models.config import GuidanceSet
from civsim_harness.models.decision import DecisionTrigger, RejectionReason
from civsim_harness.models.records import CallOutcome, RunEvent
from civsim_harness.models.run import ComparabilityStatus, HostSupportTier
from civsim_harness.models.turn import (
    DecisionStep,
    Observation,
    ScreenCapture,
    StepProgress,
    TurnOutcome,
)
from civsim_harness.observe.assemble import CapabilityResult, assemble_observation
from civsim_harness.observe.capture import StepCapture, capture_for_step
from civsim_harness.observe.screen_identity import interpret_screen_state
from civsim_harness.parity.forbidden import enforce_parity_boundary
from civsim_harness.parity.screening import ScreeningProfiles
from civsim_harness.provider.accounting import build_model_call
from civsim_harness.provider.port import Image, ModelProvider
from civsim_harness.run.no_progress import NoProgressTracker, build_no_progress_event
from civsim_harness.store.port import DecisionStepBundle, MatchStore

#: The catalog's own convention (``catalogs/observations/game.yaml``, ``act.predicates``): the
#: observation carrying "which screen is up" and whether a blocking prompt is open. Looked up by
#: this well-known id in whatever `CapabilityResult`\ s a step's :class:`ObservationReader`
#: returned -- optional by construction: a caller (or test catalog) that never produces this
#: declaration simply never triggers prompt routing below, rather than failing.
SCREEN_STATE_DECLARATION_ID = DeclarationId("game.screen_state")


def _utcnow() -> Timestamp:
    return datetime.now(UTC)


class MidTurnObservationFailure(HarnessError):
    """A fresh observation could not be assembled mid-attempt (T096, research R14).

    Wraps the underlying :class:`~civsim_harness.errors.ObservationAssemblyError` and carries
    every step that *did* complete in this attempt before the failure (``steps``, ``events``) --
    T152 requires an abandoned attempt to be retained with all the steps it completed, and the
    only place that count is known is right here, at the moment the loop cannot continue. The
    caller (``run/turn_cycle.py``) is expected to persist ``steps``/``events`` as an
    ``outcome=abandoned`` attempt (when non-empty) and replay a fresh attempt from the turn's
    start quicksave -- never finish this attempt from the last good view (invariant I14).
    """

    def __init__(
        self,
        message: str,
        *,
        cause: ObservationAssemblyError,
        steps: tuple[DecisionStepBundle, ...],
        events: tuple[RunEvent, ...],
        no_progress_streak: int,
    ) -> None:
        super().__init__(message, detail=dict(cause.detail))
        self.cause = cause
        self.steps = steps
        self.events = events
        self.no_progress_streak = no_progress_streak


class UnknownScreenEncountered(HarnessError):
    """The current screen does not resolve to any declared one (research R13, FR-049).

    Raised instead of guessing, clicking through, or dismissing -- "the run must stall visibly"
    (spec edge case). Carries the ready-to-persist ``unknown_screen`` event this module already
    built via :func:`~civsim_harness.act.prompts.route_prompt`; the caller
    (``run/turn_cycle.py``/``run/runner.py``) decides how the run's lifecycle responds.
    """

    def __init__(self, message: str, *, event: RunEvent) -> None:
        super().__init__(message, detail={"raw_screen_id": event.detail.get("raw_screen_id")})
        self.event = event


class ObservationReader(Protocol):
    """Read every catalog capability this decision step needs, fresh from the live game (or a
    fake standing in for it).

    Returns ``(results, screen_identity)`` -- the raw capability results
    :func:`~civsim_harness.observe.assemble.assemble_observation` consumes, plus the plain
    ``screen_identity`` string it records verbatim on the resulting
    :class:`~civsim_harness.models.turn.Observation`. Never cached, never reused across steps
    (FR-008, invariant I14): the loop calls this exactly once per fresh read, always after the
    previous step's effect (if any) has already been executed.
    """

    async def __call__(self) -> tuple[Sequence[CapabilityResult], str]: ...


class ActionExecutor(Protocol):
    """Actually perform one authorized action's effect in the game (e.g. via the Nexus client's
    ``execute_command``, or a fake standing in for it).

    ``act.dispatch.dispatch_action`` only decides whether an action *may* proceed; this is what
    makes it happen. Its return value, if any, is not consumed by the loop -- the effect is
    confirmed by re-observing and re-verifying afterward, never by trusting this call's own
    return (research R14's "verify" phase).
    """

    async def __call__(
        self, declaration_id: DeclarationId, parameters: Mapping[str, Any], target: Any
    ) -> Any: ...


@dataclass(frozen=True)
class DecisionLoopContext:
    """Everything one turn attempt's decision-step loop needs, fixed for the whole loop.

    Identity fields (``run_id``/``turn_number``/``turn_cycle_id``) are supplied by the caller
    (``run/turn_cycle.py``), which owns quicksave-before-loop and persist-after-loop sequencing
    (T114). Everything else is either a pure collaborator already owned by another wave
    (``registry``, ``provider``, ``host``) or one of this module's own two injected seams
    (``read_observation_inputs``, ``execute_action``).
    """

    run_id: RunId
    turn_number: int
    turn_cycle_id: TurnCycleId
    registry: CapabilityRegistry
    catalog_version: CatalogVersionRef
    model: ModelRef
    guidance: GuidanceSet | None
    provider: ModelProvider
    no_progress_step_limit: int
    read_observation_inputs: ObservationReader
    execute_action: ActionExecutor
    host: HostPlatform
    host_info: HostInfo
    view_declaration_id: DeclarationId
    screening_profiles: ScreeningProfiles
    store: MatchStore
    window_provider: Callable[[], GameWindow | None] = field(default=lambda: None)
    camera_state_provider: Callable[[], Mapping[str, Any] | Awaitable[Mapping[str, Any]]] = field(
        default=dict
    )
    """Where this step's ``camera_state`` comes from (FR-026).

    **May be synchronous or a coroutine function**, and :func:`_observe` awaits whichever it is
    handed. The real provider (``run/composition.py``, T221) reads the live camera through the
    declared ``camera.read_state`` capability, which is inherently ``async``; the ``dict`` default
    -- an empty camera state, which fails the provenance gate closed -- is synchronous, as is
    every test's. Widening the type rather than forcing one shape is what let the composition root
    supply a real reader without every existing caller of this context changing.
    """

    clock: Callable[[], Timestamp] = field(default=_utcnow)


#: T225: the shortest literal this loop will hand :func:`enforce_parity_boundary` as a
#: run-specific forbidden value. ``find_literal_leaks`` matches by plain substring, so a short
#: value is not evidence of anything -- a two-character provider name, or a model called
#: ``test``, would match ordinary prose ("latest") and fail every run on a coincidence. A guard
#: that cries wolf is a guard someone switches off, which would cost more than the narrow class
#: of leak a short literal could have caught. Long identifiers (a 32-hex ``run_id`` or
#: ``turn_cycle_id``, a real vendor-qualified model name) are distinctive enough that a match is
#: a genuine finding, and those are exactly the values FR-020 names.
_MIN_DISTINCTIVE_LITERAL_LEN = 8


def _run_forbidden_literals(ctx: DecisionLoopContext) -> tuple[str, ...]:
    """The run-specific values FR-020 forbids from ever appearing in the agent's context.

    Model identity, run identity, and this attempt's own identifiers are harness telemetry: they
    belong in the run record and nowhere near the prompt. They are checked as literals (the
    structural scan in :func:`enforce_parity_boundary` only sees the ``Observation``'s keys, not
    the rendered request text), filtered to the distinctive ones per
    :data:`_MIN_DISTINCTIVE_LITERAL_LEN`.
    """
    candidates = (
        str(ctx.run_id),
        str(ctx.turn_cycle_id),
        str(ctx.model.model),
        str(ctx.model.provider),
    )
    return tuple(
        value for value in candidates if len(value.strip()) >= _MIN_DISTINCTIVE_LITERAL_LEN
    )


@dataclass(frozen=True)
class DecisionLoopResult:
    """The finished loop's output, ready for ``run/turn_cycle.py`` to persist as one
    ``TurnCycleRecord`` (T117). ``outcome`` is exactly one of the two T112 permits."""

    outcome: TurnOutcome
    steps: tuple[DecisionStepBundle, ...]
    final_no_progress_streak: int
    events: tuple[RunEvent, ...]


@dataclass(frozen=True)
class _FreshObservation:
    """One ``_observe`` call's full result: the assembled ``Observation``, plus this step's own
    **not-yet-persisted** capture (T238).

    The capture is deliberately unwritten here. ``shown_to_agent`` is a statement about a
    decision request actually dispatched to the provider, and no such request exists when the
    frame is taken -- so the loop persists the record only at the moment the attachment question
    is genuinely settled: its own step's provider dispatch, or one of the paths where the frame
    demonstrably reached no request at all (loop exit, stall, refused context). The persisted
    flag therefore reports what happened, never an intention (FR-015, SC-019).
    ``step_capture.visually_degraded`` is T157's per-step flag, carried into the ``DecisionStep``
    this observation eventually belongs to; ``events`` is every event the capture attempt
    produced."""

    observation: Observation
    step_capture: StepCapture
    events: tuple[RunEvent, ...]


async def _resolve_camera_state(ctx: DecisionLoopContext) -> Mapping[str, Any]:
    """Call ``ctx.camera_state_provider`` and await it if it returned an awaitable.

    Exists because the real provider is inherently ``async`` (it reads the live camera through a
    declared catalog capability) while the default and every test's is a plain callable -- see
    :attr:`DecisionLoopContext.camera_state_provider`. This loop defaults and rescues nothing here:
    a provider that raises propagates, because a camera state the harness could not read is not a
    camera state *this module* may substitute for. Whether an unreadable camera is worth failing a
    step over is the provider's own policy (the real one, in ``run/composition.py``, degrades the
    capture instead -- FR-050), not a decision taken behind its back here.
    """
    produced = ctx.camera_state_provider()
    if inspect.isawaitable(produced):
        return await produced
    return produced


def _downgrade_run_comparability(ctx: DecisionLoopContext) -> None:
    """T240, FR-050, SC-013: downgrade the *run's* recorded comparability when a capture
    degrades mid-run.

    ``Run.comparability_status`` is set once at preparation from the host gate
    (``observe/host_gate.py``) and until this function existed was never written again -- so a
    run that started ``COMPARABLE`` on a validated host and then lost its images kept
    ``comparable`` on record while its steps carried ``visually_degraded`` flags. The step/turn
    flags say *which* steps lost images; this is the run-level rollup the spec names
    (``observe/capture.py``'s own docstring promises it), persisted through the existing
    ``MatchStore.update_run`` port surface. Only ``COMPARABLE`` downgrades: a run already
    ``VISUALLY_DEGRADED`` or ``NOT_COMPARABLE`` is left alone, and nothing here can ever move a
    status *up*. A run the store does not know (compositions that never ``create_run``) has no
    record to downgrade, so ``None`` is a no-op rather than an error.
    """
    run = ctx.store.get_run(ctx.run_id)
    if run is None or run.comparability_status is not ComparabilityStatus.COMPARABLE:
        return
    ctx.store.update_run(ctx.run_id, comparability_status=ComparabilityStatus.VISUALLY_DEGRADED)


def _images_permitted_for_run(ctx: DecisionLoopContext) -> bool:
    """T099's rule: until a platform's R6 capture-hygiene spike has passed, no run on that
    platform may show images to the agent.

    The spike's status reaches this loop through the run's own record: preparation resolves the
    per-platform probe (``host/detect.py::probe_host_support``) through the host gate
    (``observe/host_gate.py::evaluate_host_gate``), and ``Run.host_support_tier`` is
    ``VALIDATED`` exactly when this platform *and session*'s own R6 spike passed --
    ``SUPPORTED`` means quicksave-verified with capture hygiene *not* established, and such a
    run proceeds visually degraded under FR-050: every clean frame is still screened and stored,
    but none is ever attached to a decision request. Fails closed when the run record cannot be
    read at all -- no evidence of a passed spike means no images.
    """
    run = ctx.store.get_run(ctx.run_id)
    return run is not None and run.host_support_tier is HostSupportTier.VALIDATED


def _step_images(
    ctx: DecisionLoopContext,
    *,
    observation: Observation,
    step_capture: StepCapture,
    images_permitted: bool,
) -> list[Image]:
    """Build this step's agent-visible images -- through T134's one chokepoint, or not at all.

    The only candidate is this step's own fresh capture (FR-015 already forbids any other), and
    it must clear three layers: the run-level R6 gate (*images_permitted*, T099); wire readiness
    (a clean blob whose frame format has a real media type -- a raw framebuffer has no wire form
    and cannot be attached as-is); and :func:`~civsim_harness.agent.context.
    select_screened_images` itself, which re-checks screened-clean status, catalog resolution of
    the view, and current-step binding (FR-024, FR-025, FR-015). Nothing in this module hands an
    ``Image`` to ``assemble_context`` by any other path -- keeping T134's function the only way
    a capture becomes an agent-visible image.
    """
    if not images_permitted:
        return []
    if step_capture.blob is None or step_capture.blob_media_type is None:
        return []
    candidate = Image(media_type=step_capture.blob_media_type, data=step_capture.blob)
    return select_screened_images(
        observation=observation,
        registry=ctx.registry,
        candidates=[(step_capture.capture, candidate)],
    )


def _as_shown(capture: ScreenCapture) -> ScreenCapture:
    """A copy of *capture* with ``shown_to_agent=True`` -- rebuilt through ``model_validate``
    rather than ``model_copy`` so the model's own withheld-never-shown invariant re-runs on the
    exact record persisted (a withheld capture can never be flipped shown, even by a bug in the
    attachment logic above this)."""
    return ScreenCapture.model_validate({**capture.model_dump(), "shown_to_agent": True})


async def _observe(
    ctx: DecisionLoopContext, *, step_id: DecisionStepId, step_index: int
) -> _FreshObservation:
    """One fresh read + one fresh capture, assembled into one fresh ``Observation`` (FR-008,
    FR-015, invariant I14). Called once before the first decision, and once again after every
    executed (or rejected) decision -- see module docstring.

    The capture is *not* written here (T238): its ``shown_to_agent`` cannot be truthful until the
    loop knows whether the frame actually rode a decision request out of the harness, so the
    write happens at that settling point instead -- see :class:`_FreshObservation`. The two
    exceptions, handled right here because the loop never gets the chance: a capture whose
    observation cannot even be assembled (persisted un-shown before the T096 failure propagates),
    and the run-level comparability downgrade (T240), applied the moment degradation is detected
    regardless of what later happens to this attempt -- the frames were lost either way.
    """
    results, screen_identity = await ctx.read_observation_inputs()

    window = ctx.window_provider()
    # The camera state is only resolved when there is actually a window to attribute a frame to.
    # A step with no resolved window produces a withheld capture regardless (``capture_for_step``
    # never even calls the host), so reading the live camera there would be a Lua round trip whose
    # result could not change anything -- and it would happen on every step of a headless or
    # window-less run. When there *is* a window, this read is mandatory: the provenance gate
    # checks it against the view's declared ``camera_requirements`` (FR-026), and an empty camera
    # state fails that gate on its first check.
    camera_state = await _resolve_camera_state(ctx) if window is not None else {}
    step_capture = capture_for_step(
        host=ctx.host,
        host_info=ctx.host_info,
        window=window,
        view_declaration_id=ctx.view_declaration_id,
        camera_state=dict(camera_state),
        run_id=ctx.run_id,
        turn_number=ctx.turn_number,
        decision_step_id=step_id,
        step_index=step_index,
        captured_at=ctx.clock(),
        registry=ctx.registry,
        profiles=ctx.screening_profiles,
    )
    if step_capture.visually_degraded:
        # T240, FR-050, SC-013: capture degradation is a *run*-level comparability fact, not
        # only a per-step flag -- applied here, at detection, so it lands even when this attempt
        # is later abandoned (the frames were lost in reality either way).
        _downgrade_run_comparability(ctx)

    try:
        observation = assemble_observation(
            observation_id=ObservationId(uuid.uuid4().hex),
            decision_step_id=step_id,
            catalog_version=ctx.catalog_version,
            registry=ctx.registry,
            results=results,
            screen_identity=screen_identity,
            assembled_at=ctx.clock(),
            # The capture-id listing is finalized by the loop at attachment time, exactly like
            # the capture's own shown_to_agent -- an id appears here only once its image
            # genuinely rode this step's decision request (T238, FR-015).
            captures=(),
        )
    except ObservationAssemblyError:
        # This attempt dies here (T096) and the capture never reaches any decision request --
        # persist it saying exactly that before the failure propagates: the record (clean or
        # withheld) is evidence either way and may not be lost (FR-051, D5, SC-019).
        ctx.store.write_capture(step_capture.capture, step_capture.blob)
        raise

    return _FreshObservation(
        observation=observation,
        step_capture=step_capture,
        events=step_capture.events,
    )


async def _observe_or_wrap(
    ctx: DecisionLoopContext,
    *,
    step_id: DecisionStepId,
    step_index: int,
    completed_steps: tuple[DecisionStepBundle, ...],
    completed_events: tuple[RunEvent, ...],
    no_progress_streak: int,
) -> _FreshObservation:
    """:func:`_observe`, with an :class:`~civsim_harness.errors.ObservationAssemblyError` wrapped
    into :class:`MidTurnObservationFailure` carrying whatever steps already completed in this
    attempt (T096, T152)."""
    try:
        return await _observe(ctx, step_id=step_id, step_index=step_index)
    except ObservationAssemblyError as exc:
        raise MidTurnObservationFailure(
            "a fresh observation could not be assembled; this attempt cannot continue on a "
            "stale board and must be abandoned and replayed from its start quicksave "
            f"(step_index context: {len(completed_steps)} step(s) already completed)",
            cause=exc,
            steps=completed_steps,
            events=completed_events,
            no_progress_streak=no_progress_streak,
        ) from exc


def _resolve_trigger(
    observation: Observation,
    *,
    ctx: DecisionLoopContext,
    step_index: int,
    occurred_at: Timestamp,
) -> tuple[DecisionTrigger, str | None]:
    """Derive this step's ``trigger``/``prompt_type`` from the screen-identity observation, if the
    running catalog declares one (research R13, FR-010). Optional by construction: a test catalog
    (or a real one, mid-authoring) that never produces ``game.screen_state`` simply always resolves
    to ``proactive`` here rather than failing -- see ``SCREEN_STATE_DECLARATION_ID``'s docstring.

    Raises :class:`UnknownScreenEncountered` when the screen is recognised as *not* recognised
    (FR-049) -- the one path in this module that deliberately does not return a value, since
    guessing what to do next is exactly what the spec forbids here.
    """
    screen_value = next(
        (
            entry.value
            for entry in observation.entries
            if entry.declaration_id == SCREEN_STATE_DECLARATION_ID
        ),
        None,
    )
    if screen_value is None:
        return DecisionTrigger.PROACTIVE, None

    try:
        screen = interpret_screen_state(screen_value)
    except ObservationAssemblyError:
        # A malformed game.screen_state value is a genuine assembly defect that
        # observe.assemble's own output_schema check should already have caught upstream; treat
        # defensively as "nothing to route" rather than duplicating that policing here.
        return DecisionTrigger.PROACTIVE, None

    route = route_prompt(
        screen=screen,
        run_id=ctx.run_id,
        turn_number=ctx.turn_number,
        step_index=step_index,
        occurred_at=occurred_at,
        registry=ctx.registry,
    )
    if route.status is PromptRouteStatus.unknown_screen:
        assert route.event is not None
        raise UnknownScreenEncountered(
            "the current screen is not recognised by any declared catalog entry; the run "
            "must stall visibly rather than guess (FR-049, research R13)",
            event=route.event,
        )
    if route.status is PromptRouteStatus.prompt_decision:
        return DecisionTrigger.PROMPT_RESPONSE, route.prompt_type
    return DecisionTrigger.PROACTIVE, None


async def run_decision_loop(ctx: DecisionLoopContext) -> DecisionLoopResult:
    """Run one turn attempt's decision-step loop to one of its two permitted exits (T110, T112).

    Raises :class:`MidTurnObservationFailure` when a fresh observation cannot be assembled (T096)
    -- this module never recovers in place: the attempt is not continuable on a stale board
    (research R14), so the caller (``run/turn_cycle.py``) must abandon this whole attempt (T152:
    retaining whatever steps the exception carries) and replay a fresh one from the turn's start
    quicksave. Raises :class:`~civsim_harness.errors.ProviderChainExhausted` when no decision can
    be obtained for a step (FR-042) and :class:`UnknownScreenEncountered` when a screen cannot be
    identified (FR-049) -- both propagate for the same reason: neither is a condition this loop is
    permitted to paper over by fabricating a decision or guessing what to do.
    """
    tracker = NoProgressTracker(limit=ctx.no_progress_step_limit)
    steps: list[DecisionStepBundle] = []
    events: list[RunEvent] = []

    # T238 / T099: resolved once per attempt -- the tier is fixed on the run record at
    # preparation, so re-reading it per step could never change the answer mid-attempt.
    images_permitted = _images_permitted_for_run(ctx)

    step_index = 1
    step_id = DecisionStepId(uuid.uuid4().hex)
    initial = await _observe_or_wrap(
        ctx,
        step_id=step_id,
        step_index=step_index,
        completed_steps=(),
        completed_events=(),
        no_progress_streak=tracker.streak,
    )
    observation = initial.observation
    step_capture = initial.step_capture
    events.extend(initial.events)

    while True:
        started_at = ctx.clock()

        try:
            trigger, prompt_type = _resolve_trigger(
                observation, ctx=ctx, step_index=step_index, occurred_at=started_at
            )
        except UnknownScreenEncountered:
            # The run stalls before this step's decision request ever exists (FR-049), so its
            # capture demonstrably reached no agent -- persisted saying exactly that on the way
            # out (T238; the record may not be lost, FR-051/D5).
            ctx.store.write_capture(step_capture.capture, step_capture.blob)
            raise

        # T238, FR-024/FR-015, T134: this step's own clean capture -- and only it -- may become
        # the request's image, and only through `select_screened_images`. `shown_to_agent` and
        # the observation's capture listing are finalized here, from the *actual* attachment,
        # never from the screening outcome alone: a clean-but-unattached frame (R6 gate closed,
        # no wire form) stays recorded not shown with an empty listing.
        images = _step_images(
            ctx,
            observation=observation,
            step_capture=step_capture,
            images_permitted=images_permitted,
        )
        capture_record = step_capture.capture
        if images:
            capture_record = _as_shown(capture_record)
            observation = observation.model_copy(
                update={"captures": [capture_record.capture_id]}
            )

        request = assemble_context(
            observation=observation,
            guidance=ctx.guidance,
            model=ctx.model,
            step_index=step_index,
            response_schema=RESPONSE_SCHEMA,
            images=images,
            # The commands a human sees available (ids + summaries) and the `target`
            # convention -- without them the model can only guess ids and never targets
            # (measured 2026-09-21: 24/24 decisions refused before dispatch).
            actions=ctx.registry.catalog.declarations.values(),
        )

        # T225 / Constitution Principle I (NON-NEGOTIABLE), FR-019, FR-020: the detective
        # red-team guard, run on the fully assembled context for THIS step, at the last moment
        # before it leaves the harness. Everything upstream of here -- catalog validation, the
        # attributed-entry construction in `observe/assemble.py`, the four screening gates in
        # `parity/screening.py` -- is preventive; this is the belt-and-braces check that a leak
        # slipping past all of them still fails loudly instead of silently reaching the agent.
        # It was written (T128) and then never called from anywhere in `src/`, which meant every
        # assurance SC-006/SC-008 rest on came from tests invoking it directly rather than from
        # the path the agent's context actually travels.
        #
        # `ParityViolation` is a `HarnessError` and is deliberately NOT swallowed here or
        # anywhere below: `run/runner.py` turns it into a recorded run failure. Principle I says
        # a failure to satisfy it must be *blocked*, not shipped with a caveat -- so the run
        # stops rather than continuing with the offending step withheld. The re-raise below
        # exists only so the step's capture record is not lost with the halted attempt: the
        # refused request never left the harness, so the capture is persisted un-shown -- the
        # truth -- before the violation propagates unchanged (T238).
        try:
            enforce_parity_boundary(
                observation=observation,
                decision_request=request,
                extra_forbidden_values=_run_forbidden_literals(ctx),
            )
        except ParityViolation:
            ctx.store.write_capture(step_capture.capture, step_capture.blob)
            raise

        # T238: the capture record is written at the moment its fate is settled -- the request
        # dispatched on the very next line is the one it describes, so `shown_to_agent` reports
        # actual attachment and the durable write still precedes anything that could interrupt
        # the step afterwards (FR-051, D5).
        ctx.store.write_capture(capture_record, step_capture.blob)

        response = ctx.provider.complete(request)

        # T232, FR-040, contracts/model-provider-port.md P2/P3/P7: one definition of how a
        # completed provider call becomes a `ModelCall`. This loop used to construct it inline,
        # field by field, while `provider/accounting.py` -- built for exactly this and carrying
        # the P2 image-count re-check -- had no caller anywhere in `src/`.
        #
        # The re-check is not redundant with `ProviderChain._assert_image_count`. That one guards
        # `complete_step`; this one guards *this* loop, whose `ctx.provider` is only a chain in
        # the production composition (`_ChainBackedProvider`) and is a bare `ModelProvider` in
        # every other caller. A `ModelCall` whose own `image_count` disagrees with the request it
        # is accounting for cannot be trusted to show whether images were silently dropped
        # (FR-039, invariant I7), so it is refused rather than written.
        model_call = build_model_call(
            model_call_id=ModelCallId(uuid.uuid4().hex),
            run_id=ctx.run_id,
            turn_cycle_id=ctx.turn_cycle_id,
            decision_step_id=step_id,
            model_requested=ctx.model,
            request=request,
            response=response,
        )

        if response.outcome is not CallOutcome.DECISION_RETURNED or response.decision is None:
            # FR-042: this call never produced a DecisionStep, so it never rides along in the
            # persisted TurnCycleRecord -- record it directly, right now, or it is lost entirely.
            ctx.store.write_model_call(model_call)
            raise ProviderChainExhausted(
                "no decision could be obtained for this decision step",
                detail={"step_index": step_index, "outcome": response.outcome.value},
            )

        raw_decision = response.decision
        if trigger is DecisionTrigger.PROMPT_RESPONSE and raw_decision.prompt_type is None:
            # The harness's own screen-identity routing is authoritative (FR-011's "never assert
            # from the executor" spirit applied to prompt identity, not just verification): a
            # provider that omits the optional echo still gets recorded as answering the prompt
            # the harness determined was open.
            raw_decision = replace(raw_decision, prompt_type=prompt_type)

        target = raw_decision.parameters.get("target")
        dispatch_outcome = dispatch_action(
            registry=ctx.registry,
            context=LuaContext.IN_GAME,
            action_declaration_id=raw_decision.action_declaration_id,
            observation=observation,
            target=target,
        )
        # T230, FR-026/FR-027, research R8: a camera request is an *information-channel* request.
        # Pointing the camera at an unrevealed plot and capturing it would hand the agent
        # fog-of-war contents through the image channel, bypassing the structured filter
        # entirely -- which is why FR-026 gives it its own rejection reason rather than folding
        # it into the generic "not available right now". `act/camera.py` (T132) is the only
        # producer of `OUT_OF_PARITY_CAMERA`, and it had no caller in `src/`, so the reason was
        # unreachable in production and every refused camera move was recorded as an ordinary
        # unavailability.
        #
        # Layered on top of `dispatch_action` rather than replacing it: the generic call still
        # owns catalog resolution and Lua-context authorization (`NOT_IN_CATALOG`,
        # `ILLEGAL_IN_CONTEXT`), neither of which `validate_camera_action` performs. Only once
        # those pass does the camera validator get to classify the parity question, so a camera
        # action can never *gain* authorization here -- it can only be re-refused with the more
        # specific reason, or confirmed by the same catalog predicate `dispatch_action` just
        # evaluated.
        if (
            raw_decision.action_declaration_id in CAMERA_ACTION_DECLARATION_IDS
            and dispatch_outcome.rejection_reason
            not in (RejectionReason.NOT_IN_CATALOG, RejectionReason.ILLEGAL_IN_CONTEXT)
        ):
            dispatch_outcome = validate_camera_action(
                registry=ctx.registry,
                action_declaration_id=raw_decision.action_declaration_id,
                target=target,
            )

        next_step_id = DecisionStepId(uuid.uuid4().hex)
        next_step_index = step_index + 1

        if dispatch_outcome.status is DispatchStatus.rejected:
            execution = rejection_to_execution(dispatch_outcome, verified_at=ctx.clock())
            progress = StepProgress.REJECTED
            next_fresh = await _observe_or_wrap(
                ctx,
                step_id=next_step_id,
                step_index=next_step_index,
                completed_steps=tuple(steps),
                completed_events=tuple(events),
                no_progress_streak=tracker.streak,
            )
        else:
            declaration = dispatch_outcome.declaration
            assert declaration is not None
            await ctx.execute_action(
                declaration.declaration_id,
                raw_decision.parameters,
                raw_decision.parameters.get("target"),
            )
            next_fresh = await _observe_or_wrap(
                ctx,
                step_id=next_step_id,
                step_index=next_step_index,
                completed_steps=tuple(steps),
                completed_events=tuple(events),
                no_progress_streak=tracker.streak,
            )
            verification = verify_execution(
                declaration=declaration,
                pre_observation=observation,
                post_observation=next_fresh.observation,
                target=raw_decision.parameters.get("target"),
                verified_at=ctx.clock(),
            )
            execution = verification.execution
            progress = verification.progress

        next_observation = next_fresh.observation
        events.extend(next_fresh.events)

        ended_at = ctx.clock()
        no_progress_streak_after = tracker.record(progress)

        decision_record = build_decision(
            raw_decision,
            decision_id=DecisionId(uuid.uuid4().hex),
            decision_step_id=step_id,
            model_call_id=model_call.model_call_id,
            trigger=trigger,
            execution=execution,
        )

        step_record = DecisionStep(
            decision_step_id=step_id,
            turn_cycle_id=ctx.turn_cycle_id,
            step_index=step_index,
            observation_id=observation.observation_id,
            decision_id=decision_record.decision_id,
            model_call_id=model_call.model_call_id,
            progress=progress,
            no_progress_streak_after=no_progress_streak_after,
            visually_degraded=step_capture.visually_degraded,
            started_at=started_at,
            ended_at=ended_at,
        )
        bundle = DecisionStepBundle(
            step=step_record,
            observation=observation,
            decision=decision_record,
            model_call=model_call,
        )
        steps.append(bundle)

        if raw_decision.is_end_turn:
            # The turn's final fresh read (taken to verify the end-turn effect, I14) never
            # serves another decision, so its capture reached no request -- persisted un-shown,
            # which is the truth of what happened (T238).
            ctx.store.write_capture(next_fresh.step_capture.capture, next_fresh.step_capture.blob)
            return DecisionLoopResult(
                outcome=TurnOutcome.ENDED_BY_AGENT,
                steps=tuple(steps),
                final_no_progress_streak=tracker.streak,
                events=tuple(events),
            )

        if tracker.tripped:
            # Same as the agent-ended exit above: the backstop ends the turn before this fresh
            # read's capture could serve any decision request (T238).
            ctx.store.write_capture(next_fresh.step_capture.capture, next_fresh.step_capture.blob)
            events.append(
                build_no_progress_event(
                    run_id=ctx.run_id,
                    turn_number=ctx.turn_number,
                    occurred_at=ctx.clock(),
                    final_no_progress_streak=tracker.streak,
                    step_index=step_index,
                )
            )
            return DecisionLoopResult(
                outcome=TurnOutcome.ENDED_ON_NO_PROGRESS,
                steps=tuple(steps),
                final_no_progress_streak=tracker.streak,
                events=tuple(events),
            )

        step_index = next_step_index
        step_id = next_step_id
        observation = next_observation
        step_capture = next_fresh.step_capture
