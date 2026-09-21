"""Integration test: prompt routing and the unknown-screen stall (T067).

FR-010, FR-049, SC-005: every declared game-initiated prompt type is answered as a
`prompt_response` decision at its own decision step, recorded like any other decision -- never
dismissed, defaulted, or absorbed. An unrecognised screen must stall the run visibly instead.

Two layers, both against already-implemented code:

1. **The routing layer** (`act/prompts.py::route_prompt`), swept across *every* prompt family the
   real `catalogs/actions/prompts.yaml` declares, plus the unknown-screen and no-prompt cases --
   this is the direct, exhaustive test of the contract's own wording.
2. **The wired loop** (`run/decision_loop.py::run_decision_loop`), driven end-to-end with a real
   catalog, a scripted `ObservationReader`, and `tests/fakes`' `FakeModelProvider`/
   `FakeHostPlatform` -- proving a prompt is actually recorded as its own `DecisionStep` inside a
   real turn attempt,
   and that an unrecognised screen halts the loop before the model is ever even consulted (never
   "dismissed, defaulted, or absorbed" -- SC-005's own three forbidden verbs).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from civsim_harness.act.prompts import PromptRouteStatus, route_prompt
from civsim_harness.capability.loader import load_catalog
from civsim_harness.capability.registry import CapabilityRegistry
from civsim_harness.host.detect import HostInfo, OperatingSystem
from civsim_harness.models.common import (
    CatalogVersionRef,
    DeclarationId,
    ModelRef,
    RunId,
    TurnCycleId,
)
from civsim_harness.models.decision import DecisionTrigger
from civsim_harness.models.records import RunEventType
from civsim_harness.models.turn import TurnOutcome
from civsim_harness.observe.assemble import CapabilityResult
from civsim_harness.observe.screen_identity import PROMPT_SCREEN_PREFIX, ScreenIdentityResult
from civsim_harness.parity.screening import load_screening_profiles
from civsim_harness.provider.port import RawDecision
from civsim_harness.run.decision_loop import (
    DecisionLoopContext,
    UnknownScreenEncountered,
    run_decision_loop,
)
from civsim_harness.store.sqlite_adapter import SqliteMatchStore
from fakes.fake_host import DEFAULT_WINDOW, FakeHostPlatform
from fakes.fake_provider import FakeModelProvider

REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG_ROOT = REPO_ROOT / "catalogs"

NOW = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)

SCREEN_STATE_ID = DeclarationId("game.screen_state")
TURN_STATE_ID = DeclarationId("game.turn_state")

_REGISTRY = CapabilityRegistry(load_catalog(CATALOG_ROOT))
_PROMPT_DECLARATION_IDS: tuple[DeclarationId, ...] = tuple(
    sorted(
        declaration_id
        for declaration_id in _REGISTRY.catalog.declarations
        if str(declaration_id).startswith("prompts.")
    )
)


# --------------------------------------------------------------------------
# Layer 1 -- act.prompts.route_prompt, swept across every declared prompt family
# --------------------------------------------------------------------------


def test_the_real_catalog_declares_more_than_one_prompt_type() -> None:
    """Sanity anchor: the sweep below is only meaningful if the catalog actually declares a
    non-trivial number of prompt families (catalogs/actions/prompts.yaml)."""
    assert len(_PROMPT_DECLARATION_IDS) >= 5


@pytest.mark.parametrize("declaration_id", _PROMPT_DECLARATION_IDS)
def test_each_declared_prompt_type_routes_to_its_own_prompt_response_decision(
    declaration_id: DeclarationId,
) -> None:
    family = str(declaration_id)[len("prompts.") :]
    screen_name = f"{PROMPT_SCREEN_PREFIX}{family}"
    screen = ScreenIdentityResult(
        screen=screen_name,
        raw_screen_id=screen_name,
        recognized=True,
        has_blocking_prompt=True,
        prompt_options=("OPTION_A", "OPTION_B"),
    )

    route = route_prompt(
        screen=screen,
        run_id=RunId("run-1"),
        turn_number=5,
        step_index=2,
        occurred_at=NOW,
        registry=_REGISTRY,
    )

    assert route.status is PromptRouteStatus.prompt_decision
    assert route.action_declaration_id == declaration_id
    assert route.prompt_type == screen_name
    assert route.options == ("OPTION_A", "OPTION_B")
    assert route.event is None  # a decision is routed here, never a stall


def test_an_unrecognised_screen_routes_to_a_stall_never_a_default() -> None:
    screen = ScreenIdentityResult(
        screen="unknown",
        raw_screen_id="CivSim_NeverSeenBeforeScreen",
        recognized=False,
        has_blocking_prompt=False,
    )

    route = route_prompt(
        screen=screen,
        run_id=RunId("run-1"),
        turn_number=5,
        step_index=2,
        occurred_at=NOW,
        registry=_REGISTRY,
    )

    assert route.status is PromptRouteStatus.unknown_screen
    assert route.action_declaration_id is None
    assert route.prompt_type is None
    assert route.event is not None
    assert route.event.event_type is RunEventType.UNKNOWN_SCREEN
    assert route.event.detail["raw_screen_id"] == "CivSim_NeverSeenBeforeScreen"


def test_a_recognised_screen_with_no_blocking_prompt_routes_to_no_prompt() -> None:
    screen = ScreenIdentityResult(
        screen="world", raw_screen_id="WorldView", recognized=True, has_blocking_prompt=False
    )

    route = route_prompt(
        screen=screen, run_id=RunId("run-1"), turn_number=5, step_index=2, occurred_at=NOW
    )

    assert route.status is PromptRouteStatus.no_prompt
    assert route.action_declaration_id is None
    assert route.event is None


# --------------------------------------------------------------------------
# Layer 2 -- the wired run_decision_loop, real catalog + fakes
# --------------------------------------------------------------------------


def _screen_state_result(
    *,
    screen: str,
    recognized: bool,
    has_blocking_prompt: bool,
    prompt_options: tuple[str, ...] = (),
) -> CapabilityResult:
    return CapabilityResult(
        declaration_id=SCREEN_STATE_ID,
        value={
            "screen": screen,
            "raw_screen_id": screen,
            "recognized": recognized,
            "has_blocking_prompt": has_blocking_prompt,
            "prompt_options": list(prompt_options),
        },
    )


def _turn_state_result(
    *, turn_number: int, is_local_player_turn: bool, is_waiting_for_other_players: bool = False
) -> CapabilityResult:
    return CapabilityResult(
        declaration_id=TURN_STATE_ID,
        value={
            "turn_number": turn_number,
            "is_local_player_turn": is_local_player_turn,
            "is_waiting_for_other_players": is_waiting_for_other_players,
        },
    )


class _ScriptedObservationReader:
    """Replays one scripted `(results, screen_identity)` pair per call, in order -- the
    `ObservationReader` seam `run/decision_loop.py` documents as an injected async callable."""

    def __init__(self, calls: list[tuple[list[CapabilityResult], str]]) -> None:
        self._calls = calls
        self.call_count = 0

    async def __call__(self) -> tuple[list[CapabilityResult], str]:
        results, screen_identity = self._calls[self.call_count]
        self.call_count += 1
        return results, screen_identity


class _RecordingActionExecutor:
    """The `ActionExecutor` seam: performs nothing, just records what it was asked to do."""

    def __init__(self) -> None:
        self.calls: list[tuple[DeclarationId, dict, object]] = []

    async def __call__(
        self, declaration_id: DeclarationId, parameters: dict, target: object
    ) -> None:
        self.calls.append((declaration_id, dict(parameters), target))


def _build_context(
    *,
    observation_reader: _ScriptedObservationReader,
    action_executor: _RecordingActionExecutor,
    provider: FakeModelProvider,
    store: SqliteMatchStore,
) -> DecisionLoopContext:
    catalog = load_catalog(CATALOG_ROOT)
    registry = CapabilityRegistry(catalog)
    return DecisionLoopContext(
        run_id=RunId("run-prompts-1"),
        turn_number=1,
        turn_cycle_id=TurnCycleId("tc-1"),
        registry=registry,
        catalog_version=CatalogVersionRef(
            version=catalog.version.version, content_hash=catalog.version.content_hash
        ),
        model=ModelRef(provider="openrouter", model="anthropic/claude-opus-5"),
        guidance=None,
        provider=provider,
        no_progress_step_limit=8,
        read_observation_inputs=observation_reader,
        execute_action=action_executor,
        host=FakeHostPlatform(),
        host_info=HostInfo(os=OperatingSystem.windows, os_version="10.0.26200", session_type=None),
        view_declaration_id=DeclarationId("views.world"),
        screening_profiles=load_screening_profiles(),
        store=store,
        window_provider=lambda: DEFAULT_WINDOW,
    )


async def test_a_declared_prompt_is_answered_as_a_prompt_response_decision_at_its_own_step(
    tmp_path: Path,
) -> None:
    store = SqliteMatchStore(tmp_path / "match.db")
    try:
        reader = _ScriptedObservationReader(
            [
                (
                    [
                        _screen_state_result(
                            screen="prompt.unit_promotion",
                            recognized=True,
                            has_blocking_prompt=True,
                            prompt_options=("PROMOTION_A",),
                        ),
                        _turn_state_result(turn_number=1, is_local_player_turn=True),
                    ],
                    "prompt.unit_promotion",
                ),
                (
                    [
                        _screen_state_result(
                            screen="world", recognized=True, has_blocking_prompt=False
                        ),
                        _turn_state_result(turn_number=1, is_local_player_turn=True),
                    ],
                    "world",
                ),
                (
                    [
                        _screen_state_result(
                            screen="world", recognized=True, has_blocking_prompt=False
                        ),
                        _turn_state_result(turn_number=2, is_local_player_turn=True),
                    ],
                    "world",
                ),
            ]
        )
        executor = _RecordingActionExecutor()
        provider = FakeModelProvider()
        provider.queue_decision(
            RawDecision(
                action_declaration_id=DeclarationId("prompts.unit_promotion"),
                reasoning="Pick the offensive promotion for this unit.",
                parameters={"target": "PROMOTION_A"},
                is_end_turn=False,
                # prompt_type deliberately omitted -- the harness's own screen-identity routing
                # must supply it (run/decision_loop.py's own documented "authoritative" behaviour).
                prompt_type=None,
            )
        )
        provider.queue_decision(
            RawDecision(
                action_declaration_id=DeclarationId("turn.end_turn"),
                reasoning="Nothing else to do this turn.",
                parameters={},
                is_end_turn=True,
            )
        )

        ctx = _build_context(
            observation_reader=reader, action_executor=executor, provider=provider, store=store
        )

        result = await run_decision_loop(ctx)
    finally:
        store.close()

    assert result.outcome is TurnOutcome.ENDED_BY_AGENT
    assert len(result.steps) == 2

    step_one, step_two = result.steps
    assert step_one.step.step_index == 1
    assert step_one.decision.trigger is DecisionTrigger.PROMPT_RESPONSE
    assert step_one.decision.prompt_type == "prompt.unit_promotion"
    assert step_one.decision.action_declaration_id == DeclarationId("prompts.unit_promotion")

    assert step_two.step.step_index == 2
    assert step_two.decision.trigger is DecisionTrigger.PROACTIVE
    assert step_two.decision.prompt_type is None
    assert step_two.decision.is_end_turn is True

    assert executor.calls  # the prompt response was actually dispatched, not just recorded
    assert executor.calls[0][0] == DeclarationId("prompts.unit_promotion")


async def test_an_unrecognised_screen_stalls_the_run_visibly_rather_than_being_absorbed(
    tmp_path: Path,
) -> None:
    store = SqliteMatchStore(tmp_path / "match.db")
    try:
        reader = _ScriptedObservationReader(
            [
                (
                    [
                        _screen_state_result(
                            screen="CivSim_NeverSeenBeforeScreen",
                            recognized=False,
                            has_blocking_prompt=False,
                        )
                    ],
                    "unknown",
                )
            ]
        )
        executor = _RecordingActionExecutor()
        provider = FakeModelProvider()  # deliberately never scripted -- it must never be called

        ctx = _build_context(
            observation_reader=reader, action_executor=executor, provider=provider, store=store
        )

        with pytest.raises(UnknownScreenEncountered) as excinfo:
            await run_decision_loop(ctx)
    finally:
        store.close()

    assert excinfo.value.event.event_type is RunEventType.UNKNOWN_SCREEN
    assert excinfo.value.event.detail["raw_screen_id"] == "CivSim_NeverSeenBeforeScreen"
    # Not dismissed, not defaulted, not absorbed (SC-005): the model is never even consulted, and
    # no action is ever dispatched for an unrecognised screen.
    assert provider.calls == []
    assert executor.calls == []
