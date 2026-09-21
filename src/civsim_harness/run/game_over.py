"""Game-over detection at the turn boundary (2026-09-21, gameplay).

MEASURED, 18:50 EDT 2026-09-21, ``run-…`` on the live Linux client: Persia was defeated at game
turn 59 (Georgia took the only city). The harness had no game-over detection at all, so the next
turn started exactly as any other: ``run/turn_cycle.py`` asked for a start-of-turn quicksave, the
game -- already sitting on ``EndGameMenu`` -- never wrote one, and the run paused with
``SaveVerificationError("expected .Civ6Save not found ... waited 10 s")``. A fresh ``InGame`` read
taken a minute later showed ``Players[0]:IsAlive() == false`` and ``EndGameMenu`` as the only shown
screen. The run's honest terminal state is a **defeat**, recorded as a stop resolution -- not a
save error dressed up as one, and not a ``paused`` run an operator has to go and interpret.

**Why this is a separate read rather than the existing ``game.outcome_state``.**
``catalogs/observations/game.yaml``'s ``game.outcome_state`` (T216, ``lua/gamecore/
game_outcome.lua``) already answers "has the local player won or lost", and ``run/composition.py``
feeds it to ``run/stop.py`` through ``evaluate_stop_facts``. But that value is read **inside** a
decision step's observation sweep and consumed **after** a turn finishes -- and the turn that
discovers the defeat is precisely the turn that cannot start, because its very first act
(FR-007's quicksave) is the thing the finished game refuses. The existing path is not wrong; it is
one turn too late to ever fire on this shape of ending. This module is the same question asked at
the one moment that answers it: before the quicksave, in the same pre-save position as the
blocking-prompt probe.

**Principle I -- what may be read, and when.** ``lua/gamecore/game_outcome.lua``'s parity note
forbids reporting *which* civilization won or *by which victory type* -- correctly, because while
the game is still being played that is opponent state a human cannot see. The moment the game is
over, the end-game screen states all of it to the human in plain text: the victory type's own
name, and the winning leader/civilization. So this module's Lua reads those two fields **only
once the game is already over**, from the same accessors ``endgamemenu.lua`` itself uses to build
that screen, and reports ``None`` for both at every other moment. That gate is in the Lua (see
``lua/ingame/game_over.lua``) and re-asserted here: :func:`interpret_game_over` drops
``victory_type``/``winner`` from any read that does not also say the game is over.

**A read that errors is not a game over.** Every failure mode -- a transport error, a malformed
body, a Lua accessor that does not exist on this build, an outcome string this module does not
recognise -- resolves to "no game over detected, proceed exactly as today". Recording a defeat
because a Lua read came back malformed would be strictly worse than the gap it closes; this is the
same rule ``run/composition.py``'s ``_interpret_game_outcome`` already states for its own path.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from civsim_harness.errors import HarnessError
from civsim_harness.models.run import StopResolution
from civsim_harness.run.stop import GameOutcome

#: Where ``lua/ingame/game_over.lua`` lives, relative to the repository root -- the same
#: ``lua_root``-relative convention every ``IntegrationCapability.implementation_ref`` uses
#: (``run/composition.py``'s ``DEFAULT_LUA_ROOT``), so one setting locates every Lua file the
#: harness ships whether or not a catalog declaration names it.
GAME_OVER_LUA_REF = "lua/ingame/game_over.lua"

#: The one dispatch-table entry ``lua/ingame/game_over.lua`` exposes.
_LUA_MODULE = "CivSim_GameOver"
_LUA_FUNCTION = "read"

GameOverReader = Callable[[], Awaitable[Any]]
"""The seam :class:`~civsim_harness.run.turn_cycle.TurnCycleDependencies` takes: a zero-argument
coroutine function returning whatever the game-over Lua printed (already JSON-decoded by
``NexusClient.execute_command``). Kept as a plain callable so ``run/turn_cycle.py`` has no
dependency on the transport, exactly as its ``read_observation_inputs``/``execute_action`` seams
do."""


@dataclass(frozen=True)
class GameOverRead:
    """One interpreted game-over read.

    ``game_over`` is the only field a caller should branch on: it is true **only** for a read that
    both says the game is over *and* names an outcome this module recognises. Everything else --
    a still-running game, an unreadable one, an outcome string from a future build -- lands with
    ``game_over=False``, which means "carry on with the turn", never "assume the worst".
    """

    game_over: bool
    local_player_alive: bool | None
    outcome: GameOutcome | None
    victory_type: str | None
    winner: str | None
    raw: Mapping[str, Any]

    @property
    def stop_resolution(self) -> StopResolution | None:
        """The :class:`~civsim_harness.models.run.StopResolution` this read terminates the run
        with, or ``None`` when it terminates nothing. Both values already exist in that enum
        (``victory``/``defeat``, data-model.md §4) -- nothing new is invented here."""
        if not self.game_over:
            return None
        if self.outcome is GameOutcome.VICTORY:
            return StopResolution.VICTORY
        if self.outcome is GameOutcome.DEFEAT:
            return StopResolution.DEFEAT
        return None

    def as_detail(self) -> dict[str, Any]:
        """The ``RunEvent.detail`` body for the ``game_over_detected`` event (Principle III).

        Carries the interpreted fields *and* the raw Lua result, so a later reader can tell a
        defeat the harness inferred from elimination apart from one it read off a rival's
        victory, without having to re-run anything.
        """
        return {
            "game_over": self.game_over,
            "local_player_alive": self.local_player_alive,
            "outcome": self.outcome.value if self.outcome is not None else None,
            "victory_type": self.victory_type,
            "winner": self.winner,
            "raw": dict(self.raw),
        }


#: The "nothing was detected" read, returned for every unreadable or unrecognised result.
NO_GAME_OVER = GameOverRead(
    game_over=False,
    local_player_alive=None,
    outcome=None,
    victory_type=None,
    winner=None,
    raw={},
)


def _optional_text(value: Any) -> str | None:
    """A displayable name, or ``None``. Empty and whitespace-only strings are ``None``: the
    end-game screen showing a blank line is not a name, and recording ``""`` as the winner would
    read as a fact rather than as the absence of one."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def interpret_game_over(value: Any) -> GameOverRead:
    """Interpret one ``lua/ingame/game_over.lua`` result.

    Never raises: every shape that is not an explicit, recognised game over becomes
    :data:`NO_GAME_OVER`, which the turn cycle treats as "proceed exactly as today". In
    particular a ``game_over: true`` carrying an ``outcome`` this module does not recognise is
    **not** a game over here -- naming the ending is the whole point, and a run recorded as
    finished without one would violate ``Run``'s own "finished requires exactly one
    stop_resolution" invariant anyway.
    """
    if not isinstance(value, Mapping):
        return NO_GAME_OVER

    alive_raw = value.get("local_player_alive")
    local_player_alive = alive_raw if isinstance(alive_raw, bool) else None

    if value.get("game_over") is not True:
        return GameOverRead(
            game_over=False,
            local_player_alive=local_player_alive,
            outcome=None,
            victory_type=None,
            winner=None,
            raw=dict(value),
        )

    outcome_raw = value.get("outcome")
    if outcome_raw == GameOutcome.VICTORY.value:
        outcome = GameOutcome.VICTORY
    elif outcome_raw == GameOutcome.DEFEAT.value:
        outcome = GameOutcome.DEFEAT
    else:
        # "Over" with no outcome anyone here can name is not actionable: a run cannot be
        # `finished` without exactly one stop_resolution, so this is reported as no game over
        # rather than guessed at.
        return GameOverRead(
            game_over=False,
            local_player_alive=local_player_alive,
            outcome=None,
            victory_type=None,
            winner=None,
            raw=dict(value),
        )

    # Principle I: the victory type and the winner are exactly what the end-game screen states to
    # the human, and they are carried only on a read that says that screen's condition holds.
    return GameOverRead(
        game_over=True,
        local_player_alive=local_player_alive,
        outcome=outcome,
        victory_type=_optional_text(value.get("victory_type")),
        winner=_optional_text(value.get("winner")),
        raw=dict(value),
    )


class GameOverDetected(HarnessError):
    """The game was already over when this turn tried to begin (2026-09-21).

    Raised out of :func:`~civsim_harness.run.turn_cycle.run_turn_cycle` from the pre-save
    position, **after** the ``game_over_detected`` ``RunEvent`` is durably written and **before**
    the FR-007 quicksave is attempted -- because it cannot succeed: the client is showing
    ``EndGameMenu`` and will not write a save. Exactly like the headroom halt and the save failure
    beside it, "the turn never comes into existence as an attempt" (data-model.md §5's failure
    table), so no ``TurnCycle`` is written for it and the record carries no gap: a turn with no
    quicksave is not an attempted turn, so ``store/completeness.py`` never owes it a record.

    ``run/runner.py`` (T116) is what turns this into the run's terminal state: ``playing ->
    finished`` with :attr:`stop_resolution` -- the one recorded ``StopResolution`` (FR-005,
    invariant I10) -- rather than the ``paused`` every other unclassified mid-play ``HarnessError``
    lands in. A defeat is an ending, not a fault.
    """

    def __init__(
        self,
        message: str,
        *,
        stop_resolution: StopResolution,
        read: GameOverRead,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message, detail=detail)
        self.stop_resolution = stop_resolution
        self.read = read


class LuaGameOverReader:
    """Dispatch ``lua/ingame/game_over.lua`` through an already-connected Nexus session.

    Built the same way ``saves/save_game.py``'s ``LuaSaveCapability`` is -- an
    ``execute(state_index, lua_body)`` callable plus a resolver for the current ``InGame`` state
    index, never a raw ``int``
    captured at construction (a Lua state index can differ across a phase transition, and a stale
    one silently runs the wrong Lua; see ``nexus/client.py``). The file's own source is sent
    verbatim with one appended ``print`` line, which is exactly what
    ``capability/executor.py`` does for every catalogued Lua file -- this reader exists only
    because this read deliberately carries **no** catalog declaration: it is harness bookkeeping at
    a turn boundary, never part of the agent's observation surface, and ``catalogs/observations/
    game.yaml`` is another lane's file today.
    """

    def __init__(
        self,
        execute: Callable[[int, str], Awaitable[Any]],
        *,
        in_game_state_index: Callable[[], int],
        lua_root: Path | str = Path("."),
    ) -> None:
        self._execute = execute
        self._in_game_state_index = in_game_state_index
        self._lua_root = Path(lua_root)
        self._source: str | None = None

    def _lua_body(self) -> str:
        if self._source is None:
            # Immutable within a catalog version, exactly as capability/executor.py assumes for
            # every other lua/**/*.lua file, so it is read once per reader.
            self._source = (self._lua_root / GAME_OVER_LUA_REF).read_text(encoding="utf-8")
        return (
            f"{self._source}\n\n"
            f"print(CivSim_JsonEncode({_LUA_MODULE}.{_LUA_FUNCTION}()))"
        )

    async def __call__(self) -> Any:
        return await self._execute(self._in_game_state_index(), self._lua_body())
