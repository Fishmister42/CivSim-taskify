"""The bounded verification re-read, generalised to every action (`act.verify.confirm_execution`).

MEASURED 2026-09-21: a client read can lag its own game state. `units.move_to` was dispatched,
`RequestOperation` accepted it, and the read taken at +0 s still showed the warrior on its old plot
-- it was on the destination at +1 s (probed live, game turn 25); four of seven moves in gameplay
block 2 (run-d2184c44) were recorded `verification_failed` for that reason alone. The same shape
had already been measured for the agent's own end turn, which the client confirms only after the AI
players have taken theirs.

So verification re-reads, bounded, for every action -- and the bound fails closed: an effect the
game never confirms inside it is still `verification_failed`, now recorded *with* the window it was
looked for in and with what the last read actually said, so "the client was a beat behind" and "the
game refused the order" stop being the same row in the ledger.

Time is injected (`sleep`, `monotonic`), so these run instantly and assert the window rather than
waiting for it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from civsim_harness.act.verify import ConfirmWindow, confirm_execution, last_read, verify_execution
from civsim_harness.models.catalog import DeclarationKind, ParityDeclaration
from civsim_harness.models.common import (
    CapabilityId,
    CatalogVersionRef,
    DecisionStepId,
    DeclarationId,
    LuaContext,
    ObservationId,
)
from civsim_harness.models.decision import ExecutionOutcome
from civsim_harness.models.turn import Observation, ObservationEntry, StepProgress

#: The real catalog declaration this whole lane exists for.
SET_PRODUCTION = ParityDeclaration(
    declaration_id=DeclarationId("cities.set_production"),
    kind=DeclarationKind.ACTION,
    summary="Set the selected city's current production to an available item.",
    parity_basis="Open the city's production panel and click an available production item.",
    context=LuaContext.IN_GAME,
    capability_id=CapabilityId("cities.orders"),
    availability_predicate=(
        "city.is_selected and city.owner_is_local_player and target in city.available_productions"
    ),
    verification_predicate="target in city.production_queue",
    introduced_in_version="2026.09.1",
)


def _observation(queue: list[str]) -> Observation:
    city = {
        "city_id": 65538,
        "name": "Pasargadae",
        "owner_player_id": 0,
        "owner_is_local_player": True,
        "plot": {"x": 10, "y": 12},
        "population": 3,
        "available_productions": ["UNIT_BUILDER"],
        "production_queue": queue,
    }
    entries = [
        ObservationEntry(
            declaration_id=DeclarationId("cities.state"),
            key="cities.state",
            value={"cities": [city]},
            context=LuaContext.IN_GAME,
        ),
        ObservationEntry(
            declaration_id=DeclarationId("cities.selection"),
            key="cities.selection",
            value={"has_selection": True, "selected_city_id": 65538},
            context=LuaContext.IN_GAME,
        ),
    ]
    return Observation(
        observation_id=ObservationId("obs-1"),
        decision_step_id=DecisionStepId("step-1"),
        assembled_at=datetime(2026, 9, 21, tzinfo=UTC),
        catalog_version=CatalogVersionRef(version="2026.09.7", content_hash="deadbeef"),
        entries=entries,
        captures=[],
        screen_identity="city_screen",
    )


class _Clock:
    """A monotonic clock that only advances when the code under test sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _verified_at() -> datetime:
    return datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


async def _run(
    reads: list[Observation], *, timeout_s: float = 4.0, poll_s: float = 1.0
) -> tuple[Any, Any, _Clock]:
    """Drive `confirm_execution` over a scripted sequence of reads; the last one repeats."""
    clock = _Clock()
    remaining = list(reads[1:])

    async def reobserve() -> Observation:
        return remaining.pop(0) if remaining else reads[-1]

    verification, read = await confirm_execution(
        declaration=SET_PRODUCTION,
        pre_observation=_observation([]),
        first_read=reads[0],
        observation_of=lambda observation: observation,
        reobserve=reobserve,
        target="UNIT_BUILDER",
        clock=_verified_at,
        timeout_s=timeout_s,
        poll_s=poll_s,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    return verification, read, clock


async def test_an_effect_already_visible_confirms_on_the_first_read_without_waiting() -> None:
    """A client that keeps up costs nothing: no sleep, one attempt."""
    verification, _read, clock = await _run([_observation(["UNIT_BUILDER"])])

    assert verification.execution.outcome is ExecutionOutcome.APPLIED
    assert verification.progress is StepProgress.CHANGED_STATE
    assert verification.execution.verification["confirm_attempts"] == 1
    assert clock.slept == []


async def test_an_effect_confirmed_on_the_second_read_is_applied_not_failed() -> None:
    """The measured case: the read at +0 s still showed the old board, the next one did not."""
    verification, read, clock = await _run([_observation([]), _observation(["UNIT_BUILDER"])])

    assert verification.execution.outcome is ExecutionOutcome.APPLIED
    assert verification.progress is StepProgress.CHANGED_STATE
    assert verification.execution.verification["confirm_attempts"] == 2
    assert verification.execution.verification["confirm_elapsed_s"] == 1.0
    assert clock.slept == [1.0]
    # The read handed back is the one actually verified against, so the caller records the board
    # that confirmed the effect rather than the stale one it started from.
    assert read.entries[0].value["cities"][0]["production_queue"] == ["UNIT_BUILDER"]


async def test_an_effect_never_confirmed_within_the_bound_fails_with_the_bound_stated() -> None:
    """The bound fails closed -- and says how long and how often it looked, so a genuine refusal
    is distinguishable from a slow client in the record."""
    verification, _read, clock = await _run([_observation([])], timeout_s=4.0, poll_s=1.0)

    detail = verification.execution.verification
    assert verification.execution.outcome is ExecutionOutcome.REJECTED
    assert verification.progress is StepProgress.REJECTED
    assert detail["result"] is False
    assert detail["confirm_timeout_s"] == 4.0
    assert detail["confirm_poll_s"] == 1.0
    assert detail["confirm_attempts"] == 5  # t = 0, 1, 2, 3, 4
    assert detail["confirm_elapsed_s"] == 4.0
    assert clock.slept == [1.0, 1.0, 1.0, 1.0]


async def test_a_failed_confirmation_quotes_what_the_last_read_actually_said() -> None:
    """"Not confirmed" on its own is unreadable in the ledger: an empty production queue and one
    holding a Monument are very different failures."""
    verification, _read, _clock = await _run([_observation([])])

    assert verification.execution.verification["last_read"] == {"city.production_queue": []}

    other, _read2, _clock2 = await _run([_observation(["BUILDING_MONUMENT"])])
    assert other.execution.verification["last_read"] == {
        "city.production_queue": ["BUILDING_MONUMENT"]
    }


async def test_the_bound_is_respected_exactly_and_never_polls_past_it() -> None:
    """A zero-length window is one read and no sleep -- the degenerate case a future caller
    passing 0 must not turn into an unbounded loop."""
    verification, _read, clock = await _run([_observation([])], timeout_s=0.0, poll_s=1.0)

    assert verification.execution.outcome is ExecutionOutcome.REJECTED
    assert verification.execution.verification["confirm_attempts"] == 1
    assert clock.slept == []


def test_a_verification_with_no_window_records_no_window() -> None:
    """`verify_execution` stays usable on its own (the backstop end turn calls it directly); the
    window is additive, never required."""
    verification = verify_execution(
        declaration=SET_PRODUCTION,
        pre_observation=_observation([]),
        post_observation=_observation([]),
        target="UNIT_BUILDER",
        verified_at=_verified_at(),
    )

    detail = verification.execution.verification
    assert verification.execution.outcome is ExecutionOutcome.REJECTED
    assert "confirm_timeout_s" not in detail
    assert "last_read" not in detail


def test_a_window_is_recorded_on_a_confirmed_verification_too() -> None:
    verification = verify_execution(
        declaration=SET_PRODUCTION,
        pre_observation=_observation([]),
        post_observation=_observation(["UNIT_BUILDER"]),
        target="UNIT_BUILDER",
        verified_at=_verified_at(),
        confirm_window=ConfirmWindow(timeout_s=4.0, poll_s=1.0, attempts=3, elapsed_s=2.0),
    )

    assert verification.execution.outcome is ExecutionOutcome.APPLIED
    assert verification.execution.verification["confirm_attempts"] == 3


@pytest.mark.parametrize(
    ("predicate", "expected"),
    [
        # `target in <collection>` quotes the collection -- the thing the harness looked in.
        ("target in city.production_queue", {"city.production_queue": []}),
        # Every top-level `and` operand is read, so a multi-part predicate says which part failed.
        (
            "city.owner_is_local_player and target in city.production_queue",
            {"city.owner_is_local_player": True, "city.production_queue": []},
        ),
    ],
)
def test_last_read_quotes_each_operand_of_the_predicate(
    predicate: str, expected: dict[str, Any]
) -> None:
    from civsim_harness.act.predicates import build_predicate_bindings

    bindings = build_predicate_bindings(observation=_observation([]), target="UNIT_BUILDER")

    assert last_read(predicate, bindings) == expected


def test_an_unevaluable_operand_is_recorded_as_saying_so_never_dropped() -> None:
    from civsim_harness.act.predicates import build_predicate_bindings

    bindings = build_predicate_bindings(observation=_observation([]), target="UNIT_BUILDER")

    reading = last_read("observed_turn_number + 1 == game.turn_number", bindings)

    assert len(reading) == 1
    assert "could not be evaluated" in next(iter(reading.values()))
