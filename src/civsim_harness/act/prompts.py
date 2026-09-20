"""Prompt and interrupt handling (T109).

Game-initiated prompts and between-turn interrupts route through ``catalogs/actions/prompts.yaml``
as ``prompt_response`` decisions, at their own decision steps -- exactly the same
dispatch/verify pipeline (:mod:`civsim_harness.act.dispatch`, :mod:`civsim_harness.act.verify`)
every other action goes through, since each ``prompts.yaml`` entry is an ordinary catalog action
with its own ``availability_predicate``/``verification_predicate``. This module's own job is
narrower: turn a :class:`~civsim_harness.observe.screen_identity.ScreenIdentityResult` into either
"here is the one action family now in play, and its options" or "this screen is not recognised,
and the run must stall visibly" (FR-010, FR-049, SC-005) -- it never itself clicks through,
dismisses, or defaults a screen it does not recognise.

**Between-turn interrupts route through the same path.** This module does not distinguish "a prompt
that appeared mid-turn" from "a prompt found after resuming from an interruption" -- both are simply
a screen-identity result handed to :func:`route_prompt` at its own decision step (research R13); the
run loop (T110, a later wave) decides *when* to call this, not this module.

**"Stalls the run visibly" without raising.** Consistent with this whole wave's tagged-outcome
convention (``host.port.CaptureResult``, ``act.dispatch.DispatchOutcome``, ...), an unrecognised
screen is reported as a :class:`PromptRoute` with ``status = unknown_screen`` and a ready-to-persist
``unknown_screen`` :class:`~civsim_harness.models.records.RunEvent`, not a raised exception -- this
mirrors :mod:`civsim_harness.observe.screen_identity`'s own choice to make "I don't recognise this"
a recordable value rather than a crash. The run loop is expected to transition the run's lifecycle
state (e.g. to ``interrupted``) and persist the event on this status, per FR-049/SC-005; this module
has no lifecycle or store handle to do that itself.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum

from civsim_harness.capability.registry import CapabilityRegistry
from civsim_harness.errors import CatalogError
from civsim_harness.models.common import DeclarationId, EventId, RunId, Timestamp
from civsim_harness.models.records import RunEvent, RunEventType
from civsim_harness.observe.screen_identity import PROMPT_SCREEN_PREFIX, ScreenIdentityResult

#: `prompts.yaml`'s own naming convention: a screen id `prompt.<family>` (e.g.
#: `prompt.city_state_quest`) answers to the action declaration `prompts.<family>`
#: (e.g. `prompts.city_state_quest`) -- see `lua/ingame/screens.lua` and `catalogs/actions/
#: prompts.yaml`'s own header comment, which documents this exact pairing.
_PROMPT_DECLARATION_PREFIX = "prompts."


class PromptRouteStatus(Enum):
    """The outcome of one :func:`route_prompt` call."""

    prompt_decision = "prompt_decision"
    """A recognised, blocking prompt is up: exactly one prompts.yaml action is now the only
    legal proactive move, with its options carried along for the agent to choose from."""

    no_prompt = "no_prompt"
    """The screen is recognised and nothing is blocking -- there is nothing to route."""

    unknown_screen = "unknown_screen"
    """The screen was not recognised at all (research R13) -- the run must stall visibly rather
    than guess, click through, or dismiss (FR-049, SC-005)."""


@dataclass(frozen=True)
class PromptRoute:
    """The tagged outcome of :func:`route_prompt`."""

    status: PromptRouteStatus
    action_declaration_id: DeclarationId | None = None
    prompt_type: str | None = None
    options: tuple[str, ...] = ()
    event: RunEvent | None = field(default=None)

    def __post_init__(self) -> None:
        if self.status is PromptRouteStatus.prompt_decision:
            if self.action_declaration_id is None or self.prompt_type is None:
                raise ValueError(
                    "PromptRoute.status is 'prompt_decision' but action_declaration_id/prompt_type "
                    "was not supplied"
                )
            if self.event is not None:
                raise ValueError(
                    "PromptRoute.status is 'prompt_decision' but an event was supplied"
                )
        elif self.status is PromptRouteStatus.unknown_screen:
            if self.event is None:
                raise ValueError("PromptRoute.status is 'unknown_screen' but no event was supplied")
            if self.action_declaration_id is not None or self.prompt_type is not None:
                raise ValueError(
                    "PromptRoute.status is 'unknown_screen' but action_declaration_id/prompt_type "
                    "was supplied"
                )
        else:  # no_prompt
            if self.action_declaration_id is not None or self.prompt_type is not None:
                raise ValueError(
                    "PromptRoute.status is 'no_prompt' but action_declaration_id/prompt_type "
                    "was supplied"
                )
            if self.event is not None:
                raise ValueError("PromptRoute.status is 'no_prompt' but an event was supplied")


def prompt_declaration_id_for_screen(screen: str) -> DeclarationId:
    """Map a recognised ``prompt.<family>`` screen id to its ``prompts.<family>`` action id.

    Raises :class:`~civsim_harness.errors.CatalogError` for a screen id that does not carry the
    ``prompt.`` prefix at all -- that is a caller-discipline bug (calling this on a non-prompt
    screen), not a recordable game state.
    """
    if not screen.startswith(PROMPT_SCREEN_PREFIX):
        raise CatalogError(
            "screen id is not a prompt screen", detail={"screen": screen}
        )
    suffix = screen[len(PROMPT_SCREEN_PREFIX) :]
    return DeclarationId(_PROMPT_DECLARATION_PREFIX + suffix)


def route_prompt(
    *,
    screen: ScreenIdentityResult,
    run_id: RunId,
    turn_number: int | None,
    step_index: int | None,
    occurred_at: Timestamp,
    registry: CapabilityRegistry | None = None,
    event_id: EventId | None = None,
) -> PromptRoute:
    """Route one screen-identity result to its prompt decision, or to a stall.

    *registry*, when supplied, is used to defensively confirm the mapped ``prompts.<family>``
    declaration actually exists in the loaded catalog -- a recognised prompt screen with no
    matching catalog action would be a genuine catalog-authoring bug, not a game-side "unknown
    screen", so that case raises :class:`~civsim_harness.errors.CatalogError` rather than being
    folded into :attr:`PromptRouteStatus.unknown_screen`.
    """
    if not screen.recognized:
        event = RunEvent(
            event_id=event_id if event_id is not None else EventId(uuid.uuid4().hex),
            run_id=run_id,
            turn_number=turn_number,
            step_index=step_index,
            event_type=RunEventType.UNKNOWN_SCREEN,
            occurred_at=occurred_at,
            detail={"raw_screen_id": screen.raw_screen_id},
        )
        return PromptRoute(status=PromptRouteStatus.unknown_screen, event=event)

    if not screen.has_blocking_prompt:
        return PromptRoute(status=PromptRouteStatus.no_prompt)

    declaration_id = prompt_declaration_id_for_screen(screen.screen)
    if registry is not None:
        # Raises CatalogError if unresolved -- a genuine catalog gap, not a game-side surprise.
        registry.resolve(declaration_id)

    return PromptRoute(
        status=PromptRouteStatus.prompt_decision,
        action_declaration_id=declaration_id,
        prompt_type=screen.screen,
        options=screen.prompt_options,
    )
