"""Every step record carries the action's own dispatch answer, distinct from its verification.

MEASURED LIVE 2026-09-21 (Stage 5, run-ba3ad80d): `cities.set_production` was refused 24 times by
its own Lua with `{ok = false, reason = "city_not_found"}` -- the item name was arriving in the
city-id parameter -- and not one of those refusals appears anywhere in the ledger. The run loop
awaited `execute_action` and threw the answer away, so the step recorded only the verification
predicate's verdict: `verification_failed`, 24 times, with no hint that the order had never been
accepted in the first place. Diagnosing it cost a whole live stage and a hand-written probe.

"The game refused this order, and here is the game's word for why" and "the effect was not visible
on the board afterwards" are different facts about a step. `ActionExecution.dispatch_result` now
carries the first, verbatim, alongside (never instead of) `verification`.

These run the real `run_decision_loop` against the same compact synthetic catalog and fakes
`test_no_truncation.py` uses, so what is asserted is the production record-building path.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from civsim_harness.capability.loader import Catalog
from civsim_harness.capability.registry import CapabilityRegistry
from civsim_harness.host.detect import HostInfo, LinuxSessionType, OperatingSystem
from civsim_harness.models.catalog import CapabilityPath as CatalogCapabilityPath
from civsim_harness.models.catalog import (
    CatalogVersion,
    DeclarationKind,
    IntegrationCapability,
    ParityDeclaration,
)
from civsim_harness.models.common import (
    CapabilityId,
    CatalogVersionRef,
    DeclarationId,
    LuaContext,
    ModelRef,
    RunId,
    TurnCycleId,
)
from civsim_harness.models.decision import ExecutionOutcome
from civsim_harness.observe.assemble import CapabilityResult
from civsim_harness.parity.screening import load_screening_profiles
from civsim_harness.provider.port import DecisionRequest, RawDecision
from civsim_harness.run import decision_loop
from civsim_harness.run.decision_loop import (
    DecisionLoopContext,
    _dispatch_result,
    run_decision_loop,
)
from civsim_harness.store.sqlite_adapter import SqliteMatchStore
from fakes.fake_host import FakeHostPlatform
from fakes.fake_provider import FakeModelProvider

TICK = DeclarationId("test.tick")
TURN_STATE = DeclarationId("game.turn_state")


def _build_registry() -> CapabilityRegistry:
    turn_state = ParityDeclaration(
        declaration_id=TURN_STATE,
        kind=DeclarationKind.OBSERVATION,
        summary="Test-only turn state.",
        parity_basis="Look at the turn counter in the top bar.",
        context=LuaContext.IN_GAME,
        capability_id=CapabilityId("test.turn_control"),
        output_schema={
            "type": "object",
            "required": ["turn_number", "is_local_player_turn", "is_waiting_for_other_players"],
            "properties": {
                "turn_number": {"type": "integer"},
                "is_local_player_turn": {"type": "boolean"},
                "is_waiting_for_other_players": {"type": "boolean"},
            },
        },
        introduced_in_version="test",
    )
    tick = ParityDeclaration(
        declaration_id=TICK,
        kind=DeclarationKind.ACTION,
        summary="Test-only action.",
        parity_basis="Click a UI element that advances the test counter.",
        context=LuaContext.IN_GAME,
        capability_id=CapabilityId("test.turn_control"),
        availability_predicate="true",
        verification_predicate="game.turn_number == observed_turn_number + 1",
        introduced_in_version="test",
    )
    capability = IntegrationCapability(
        capability_id=CapabilityId("test.turn_control"),
        path=CatalogCapabilityPath.FIRETUNER,
        implementation_ref="test",
        reads=["turn state"],
        writes=["turn state"],
    )
    catalog = Catalog(
        root=Path("."),
        version=CatalogVersion(
            version="test", content_hash="test", declaration_ids=[TICK, TURN_STATE]
        ),
        declarations=MappingProxyType({d.declaration_id: d for d in (turn_state, tick)}),
        capabilities=MappingProxyType({capability.capability_id: capability}),
    )
    return CapabilityRegistry(catalog=catalog)


class _FakeGame:
    """A game whose action returns a Lua-shaped answer table, and optionally does nothing."""

    def __init__(self, *, answer: Any, applies: bool) -> None:
        self.turn_number = 1
        self.answer = answer
        self.applies = applies

    async def read(self) -> tuple[Sequence[CapabilityResult], str]:
        return (
            [
                CapabilityResult(
                    declaration_id=TURN_STATE,
                    value={
                        "turn_number": self.turn_number,
                        "is_local_player_turn": True,
                        "is_waiting_for_other_players": False,
                    },
                )
            ],
            "world",
        )

    async def execute(
        self, declaration_id: DeclarationId, parameters: Mapping[str, Any], target: Any
    ) -> Any:
        if self.applies:
            self.turn_number += 1
        return self.answer


def _decision_factory(request: DecisionRequest) -> RawDecision:
    return RawDecision(
        action_declaration_id=TICK,
        reasoning="tick",
        parameters={},
        is_end_turn=True,
        prompt_type=None,
    )


async def _run_one_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, answer: Any, applies: bool
) -> Any:
    # The bounded confirmation window is `test_verify_confirm.py`'s subject, not this file's:
    # zeroed here so a step whose effect never lands costs one read instead of 45 seconds.
    monkeypatch.setattr(decision_loop, "END_TURN_CONFIRM_TIMEOUT_S", 0.0)
    monkeypatch.setattr(decision_loop, "ACTION_CONFIRM_TIMEOUT_S", 0.0)
    provider = FakeModelProvider()
    provider.set_default_decision_factory(_decision_factory)
    store = SqliteMatchStore(tmp_path / "match.db")
    game = _FakeGame(answer=answer, applies=applies)
    ctx = DecisionLoopContext(
        run_id=RunId("run-1"),
        turn_number=1,
        turn_cycle_id=TurnCycleId("tc-1"),
        registry=_build_registry(),
        catalog_version=CatalogVersionRef(version="test", content_hash="test"),
        model=ModelRef(provider="test", model="test-model"),
        guidance=None,
        provider=provider,
        no_progress_step_limit=10,
        read_observation_inputs=game.read,
        execute_action=game.execute,
        host=FakeHostPlatform(),
        host_info=HostInfo(
            os=OperatingSystem.linux, os_version="test", session_type=LinuxSessionType.x11
        ),
        view_declaration_id=DeclarationId("views.test"),
        screening_profiles=load_screening_profiles(),
        store=store,
    )
    try:
        result = await run_decision_loop(ctx)
    finally:
        store.close()
    return result.steps[0].decision.execution


async def test_a_refused_order_records_the_games_own_word_for_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The measured failure, made legible: a dispatch refusal is named in the record instead of
    hiding behind the verification's verdict."""
    execution = await _run_one_step(
        tmp_path,
        monkeypatch,
        answer={"ok": False, "reason": "city_not_found"},
        applies=False,
    )

    assert execution.dispatch_result == {"ok": False, "reason": "city_not_found"}
    # And the verification still reports, separately, what the board said afterwards.
    assert execution.outcome is ExecutionOutcome.REJECTED
    assert execution.verification["result"] is False
    assert "city_not_found" not in str(execution.verification)


async def test_an_accepted_order_records_its_answer_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not only failures: every action records what its Lua said, so an `applied` step can be
    checked against the order's own report rather than only the predicate."""
    execution = await _run_one_step(
        tmp_path,
        monkeypatch,
        answer={"ok": True, "city_id": 65538, "production": "UNIT_BUILDER", "confirmed": True},
        applies=True,
    )

    assert execution.outcome is ExecutionOutcome.APPLIED
    assert execution.dispatch_result == {
        "ok": True,
        "city_id": 65538,
        "production": "UNIT_BUILDER",
        "confirmed": True,
    }


async def test_a_capability_that_answers_nothing_records_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`None` is recorded as `None`, never as an empty dict that would read as "it answered"."""
    execution = await _run_one_step(tmp_path, monkeypatch, answer=None, applies=True)

    assert execution.outcome is ExecutionOutcome.APPLIED
    assert execution.dispatch_result is None


def test_a_non_mapping_answer_is_kept_verbatim_rather_than_dropped() -> None:
    assert _dispatch_result(None) is None
    assert _dispatch_result({"ok": True}) == {"ok": True}
    assert _dispatch_result("some string the Lua returned") == {
        "value": "some string the Lua returned"
    }
    assert _dispatch_result(17) == {"value": 17}
