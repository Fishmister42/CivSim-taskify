"""T061 -- the negative test: a long, productive turn is never truncated.

FR-014 / invariant I16: a turn ends **only** on the agent's own end-turn decision or the
no-progress backstop tripping -- never because it grew long or expensive. This is the one test in
the suite explicitly written to catch a regression toward "the obvious implementation": any
wall-clock bound, step cap, or cost ceiling added to ``run/decision_loop.py`` "just in case" would
cut this test's scripted 500-step turn short, and that is a defect, not a safety feature, however
sensible it sounds in review.

Every one of the 500 steps here is genuinely productive (each one's action verifies
``changed_state``), so the no-progress counter never approaches its own configured limit either --
this test is not merely "the loop doesn't crash at 500 steps," it is "the loop does not stop before
the agent says so, and the no-progress accounting stays at zero the whole way," which is what makes
a truncation bug visible: a step cap masquerading as "safety" would fire here even though nothing
is stuck.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any

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
from civsim_harness.models.decision import DecisionTrigger
from civsim_harness.models.turn import TurnOutcome
from civsim_harness.observe.assemble import CapabilityResult
from civsim_harness.parity.screening import load_screening_profiles
from civsim_harness.provider.port import DecisionRequest, RawDecision
from civsim_harness.run.decision_loop import DecisionLoopContext, run_decision_loop
from civsim_harness.store.sqlite_adapter import SqliteMatchStore
from fakes.fake_host import FakeHostPlatform
from fakes.fake_provider import FakeModelProvider

TICK_DECLARATION_ID = DeclarationId("test.tick")
GAME_TURN_STATE_DECLARATION_ID = DeclarationId("game.turn_state")

STEP_COUNT = 500


def _build_registry() -> CapabilityRegistry:
    turn_state = ParityDeclaration(
        declaration_id=GAME_TURN_STATE_DECLARATION_ID,
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
        declaration_id=TICK_DECLARATION_ID,
        kind=DeclarationKind.ACTION,
        summary="Test-only productive action: always available, always advances the turn counter.",
        parity_basis="Click a UI element that always advances the test counter.",
        context=LuaContext.IN_GAME,
        capability_id=CapabilityId("test.turn_control"),
        availability_predicate="true",
        verification_predicate="game.turn_number == observed_turn_number + 1",
        introduced_in_version="test",
    )
    capability = IntegrationCapability(
        capability_id=CapabilityId("test.turn_control"),
        path=CatalogCapabilityPath.FIRETUNER,
        implementation_ref="test.lua",
        reads=["turn state"],
        writes=["turn state"],
    )
    catalog = Catalog(
        root=Path("."),
        version=CatalogVersion(
            version="test",
            content_hash="test",
            declaration_ids=[TICK_DECLARATION_ID, GAME_TURN_STATE_DECLARATION_ID],
        ),
        declarations=MappingProxyType({d.declaration_id: d for d in (turn_state, tick)}),
        capabilities=MappingProxyType({capability.capability_id: capability}),
    )
    return CapabilityRegistry(catalog=catalog)


class _FakeGame:
    """A minimal, entirely productive game: every ``test.tick`` dispatch advances the turn
    counter by exactly one, so every step's verification reports ``changed_state``."""

    def __init__(self) -> None:
        self.turn_number = 1

    async def read(self) -> tuple[Sequence[CapabilityResult], str]:
        return (
            [
                CapabilityResult(
                    declaration_id=GAME_TURN_STATE_DECLARATION_ID,
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
    ) -> None:
        assert declaration_id == TICK_DECLARATION_ID
        self.turn_number += 1


def _decision_factory(request: DecisionRequest) -> RawDecision:
    """Every step issues ``test.tick``; the very last one also flags ``is_end_turn`` -- the loop
    watches that flag, not the step count, to decide when to stop (T112)."""
    is_last = request.step_index >= STEP_COUNT
    return RawDecision(
        action_declaration_id=TICK_DECLARATION_ID,
        reasoning=f"step {request.step_index}: keep advancing" if not is_last else "done for now",
        parameters={},
        is_end_turn=is_last,
        prompt_type=None,
    )


async def test_500_step_productive_turn_completes_untouched(tmp_path: Path) -> None:
    registry = _build_registry()
    game = _FakeGame()
    provider = FakeModelProvider()
    provider.set_default_decision_factory(_decision_factory)
    store = SqliteMatchStore(tmp_path / "match.db")

    ctx = DecisionLoopContext(
        run_id=RunId("run-1"),
        turn_number=1,
        turn_cycle_id=TurnCycleId("tc-1"),
        registry=registry,
        catalog_version=CatalogVersionRef(version="test", content_hash="test"),
        model=ModelRef(provider="test", model="test-model"),
        guidance=None,
        provider=provider,
        # Deliberately far smaller than the step count: this turn proves the loop does not
        # truncate a *productive* turn, not that the backstop is disabled.
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

    # The loop ran every one of the 500 scripted steps -- nothing ended it early.
    assert len(result.steps) == STEP_COUNT
    assert result.outcome is TurnOutcome.ENDED_BY_AGENT
    # Every step was genuinely productive: the no-progress streak never left zero, and the
    # backstop -- configured well below the step count -- never came close to tripping.
    assert result.final_no_progress_streak == 0
    for index, bundle in enumerate(result.steps, start=1):
        assert bundle.step.step_index == index
        assert bundle.step.progress.value == "changed_state"
        assert bundle.step.no_progress_streak_after == 0
    # The final step, and only the final step, is the agent's own end-turn decision.
    assert result.steps[-1].decision.is_end_turn is True
    assert all(not bundle.decision.is_end_turn for bundle in result.steps[:-1])
    assert all(bundle.decision.trigger is DecisionTrigger.PROACTIVE for bundle in result.steps)
