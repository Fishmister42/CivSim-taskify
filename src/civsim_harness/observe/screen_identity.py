"""Screen-identity probe interpretation (T097).

Research R13: "which screen is currently up" is a declared catalog observation
(``game.screen_state``, ``catalogs/observations/game.yaml``), backed by
``lua/ingame/screens.lua`` -- itself flagged as the highest-uncertainty file in the whole catalog,
since whether the ``InGame`` tuner context can enumerate "what is currently topmost" at all is not
confirmed without a live client. This module's whole job is to turn that capability's
already-honest raw result into a small, safe Python value: an unrecognised screen is a
**recordable state** (``ScreenIdentityResult.recognized is False``), never a raised exception.
``screens.lua``'s own probe already reports ``screen == "unknown", recognized == False`` for
exactly this case (see its
``CivSim_ScreenIsKnown`` gate) -- this module does not re-derive that judgement, it only gives the
rest of ``observe``/``act`` a stable type to consume instead of a bare ``dict``.

**What ``recognized`` means, and what it used to mean (corrected 2026-09-22).** It means "I
identified what is on screen". It did NOT: until 2026-09-22 the Lua probe answered
``screen = "world", recognized = true`` whenever nothing on its hand-maintained watchlist was
open, so "nothing I know about is blocking" was reported as "nothing is blocking" -- three times
in production, most recently over a full-screen ``EndGameMenu`` DEFEAT modal and the
``HistoricMoments`` era card. The probe now reads the engine's own popup stack first and reports
``recognized = false`` plus a :data:`SCREEN_PROBE_REASON_FIELD` for anything it cannot name or
could not read, which this module carries through as
:attr:`ScreenIdentityResult.unrecognized_reason`. **Absence and unobservability do not share a
representation here**, and that is the property to preserve in any future change to this file.

Probed between decision steps and after every interruption (research R13) -- this module does not
decide *when* to probe; that is the run loop's job (T110, a later wave). It only interprets one
probe result at a time, which is what keeps it callable identically from ordinary step-boundary
probing and from post-interruption probing without any special-casing here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from civsim_harness.errors import ObservationAssemblyError

#: The screen id ``lua/ingame/screens.lua`` reports for anything outside its own known-screens
#: list (``CIVSIM_KNOWN_SCREENS``). Kept here, not re-declared ad hoc, so a caller checking for the
#: unknown state has exactly one string to compare against.
UNKNOWN_SCREEN = "unknown"

#: The prefix every blocking-prompt screen id carries (``prompt.unit_promotion``, ...), matching
#: ``catalogs/actions/prompts.yaml`` and ``lua/ingame/screens.lua``.
PROMPT_SCREEN_PREFIX = "prompt."

#: The field ``lua/ingame/screens.lua`` sets when the probe could not identify the board, naming
#: which branch fired (``popup_stack_unreadable``, ``unnamed_popup_showing``, ...). Its presence
#: is what distinguishes "I looked and nothing is blocking" from "I could not look" -- the two
#: states that shared a representation until 2026-09-22 and produced three false all-clears.
SCREEN_PROBE_REASON_FIELD = "screen_probe_reason"

#: Set when a value claims ``recognized: true`` while also carrying a reason it could not identify
#: the board. The Lua never emits that pair; a value that does is trusted in the safe direction.
INCONSISTENT_RECOGNITION_REASON = "recognized_with_probe_reason"


@dataclass(frozen=True)
class ScreenIdentityResult:
    """One screen-identity probe's interpreted result (research R13).

    ``recognized is False`` is the recordable "I do not know what this screen is" state the spec's
    edge case calls for -- never raised, never guessed at, never clicked through. A caller that
    receives one still has ``raw_screen_id`` to record for the operator/telemetry trail even though
    ``screen`` itself is normalised to :data:`UNKNOWN_SCREEN`.
    """

    screen: str
    raw_screen_id: str
    recognized: bool
    has_blocking_prompt: bool
    prompt_options: tuple[str, ...] = ()
    #: Why the probe could not identify the board, when it could not. ``None`` on a recognised
    #: screen, and ``None`` on an unrecognised one only when the client did not say -- which is
    #: itself worth seeing, so it is never defaulted to a plausible string. The whole point of
    #: this field is that "I looked and nothing is blocking" and "I could not look" stop sharing
    #: a representation; filling it in with a guess would undo that.
    unrecognized_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.recognized and (self.has_blocking_prompt or self.prompt_options):
            raise ValueError(
                "an unrecognised screen cannot also report a blocking prompt or its options "
                "-- nothing here may guess at a screen it does not know how to handle"
            )
        if self.has_blocking_prompt and not self.screen.startswith(PROMPT_SCREEN_PREFIX):
            raise ValueError(
                "has_blocking_prompt is true but screen does not carry the prompt.* prefix "
                f"(got {self.screen!r})"
            )

    @property
    def is_prompt(self) -> bool:
        """Whether this screen names a blocking, catalog-declared prompt family."""
        return self.recognized and self.screen.startswith(PROMPT_SCREEN_PREFIX)


def interpret_screen_state(value: Any) -> ScreenIdentityResult:
    """Interpret one ``game.screen_state`` capability value as a :class:`ScreenIdentityResult`.

    *value* is expected to already have passed :mod:`civsim_harness.observe.assemble`'s
    ``output_schema`` validation (``required: [screen, recognized, has_blocking_prompt]``) -- this
    function re-checks the same required keys defensively rather than trusting that upstream gate
    blindly, since a screen-identity misread has an outsized blast radius (every prompt-routing and
    end-turn-availability decision depends on it).

    Raises :class:`~civsim_harness.errors.ObservationAssemblyError` only for a value that is not
    even shaped like the declared schema (not a mapping, or missing a required key) -- a
    structurally malformed capability result is a genuine assembly defect, categorically distinct
    from "the game showed a screen we don't recognise", which this function represents as an
    ordinary, non-exceptional return value instead (research R13).
    """
    if not isinstance(value, dict):
        raise ObservationAssemblyError(
            "game.screen_state value is not a JSON object",
            detail={"value_type": type(value).__name__},
        )

    missing = [key for key in ("screen", "recognized", "has_blocking_prompt") if key not in value]
    if missing:
        raise ObservationAssemblyError(
            "game.screen_state value is missing a required field",
            detail={"missing_fields": missing},
        )

    screen = str(value["screen"])
    recognized = bool(value["recognized"])
    raw_screen_id = str(value.get("raw_screen_id", screen))

    reason_value = value.get(SCREEN_PROBE_REASON_FIELD)
    probe_reason = str(reason_value) if reason_value is not None else None

    if recognized and probe_reason is not None:
        # A value that says both "I identified this" and "here is why I could not" is internally
        # inconsistent, and the two readings are not equally safe: one authorises actions against
        # the board and the other stalls it. Resolve toward the stall and record both strings, so
        # a producer drifting into this shape is visible rather than silently believed.
        recognized = False
        probe_reason = f"{INCONSISTENT_RECOGNITION_REASON}:{probe_reason}"

    if not recognized:
        # Never trust a prompt/options claim riding along with an unrecognised screen -- treat the
        # whole probe result as "unknown" rather than a partially-known one (R13: no guessing).
        return ScreenIdentityResult(
            screen=UNKNOWN_SCREEN,
            raw_screen_id=raw_screen_id,
            recognized=False,
            has_blocking_prompt=False,
            prompt_options=(),
            unrecognized_reason=probe_reason,
        )

    has_blocking_prompt = bool(value["has_blocking_prompt"])
    prompt_options = tuple(value.get("prompt_options") or []) if has_blocking_prompt else ()

    try:
        return ScreenIdentityResult(
            screen=screen,
            raw_screen_id=raw_screen_id,
            recognized=True,
            has_blocking_prompt=has_blocking_prompt,
            prompt_options=prompt_options,
        )
    except ValueError as exc:
        # A "recognized" screen whose own fields are internally inconsistent (e.g.
        # has_blocking_prompt=true on a non-prompt screen id) is a capability defect, not a genuine
        # unrecognised-screen state -- surfaced the same way any other malformed result is.
        raise ObservationAssemblyError(
            "game.screen_state value is internally inconsistent",
            detail={"screen": screen, "reason": str(exc)},
        ) from exc
