"""Integration test for the capability executor and its two production callers (T206-T208).

The integration-readiness audit's headline finding: 21 declared Lua files and 51 catalog
declarations existed, and no Python code path ever loaded or executed a single one of them --
``IntegrationCapability.implementation_ref`` was read nowhere outside its own model definition.
This test loads the *real* catalog under ``catalogs/`` (not a hand-built test fixture), dispatches
real declarations from it through :class:`~civsim_harness.capability.executor.CapabilityExecutor`,
:class:`~civsim_harness.observe.reader.ObservationReader`, and
:class:`~civsim_harness.act.executor.ActionExecutor` against
``tests/fakes/fake_nexus.py``'s ``FakeNexusServer`` (a real Nexus wire-protocol server, driven by
an unmodified ``NexusClient`` -- no hand-rolled transport stand-in), and asserts the produced
:class:`~civsim_harness.observe.assemble.CapabilityResult`\\ s carry the correct ``declaration_id``.
This is the test that proves the catalog and the ``lua/`` files are genuinely connected, not merely
declared side by side.
"""

from __future__ import annotations

from pathlib import Path

from civsim_harness.act.executor import ActionExecutor
from civsim_harness.capability.executor import CapabilityExecutor
from civsim_harness.capability.loader import load_catalog
from civsim_harness.capability.registry import CapabilityRegistry
from civsim_harness.models.common import DeclarationId, LuaContext
from civsim_harness.nexus.client import NexusClient
from civsim_harness.observe.reader import ObservationReader
from civsim_harness.observe.screen_identity import UNKNOWN_SCREEN
from fakes.fake_nexus import (
    STATE_INDEX_IN_GAME,
    FakeNexusServer,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOGS_ROOT = _REPO_ROOT / "catalogs"

_UNITS_STATE = DeclarationId("units.state")
_END_TURN = DeclarationId("turn.end_turn")


async def test_the_real_catalog_loads_with_the_documented_shape() -> None:
    catalog = load_catalog(CATALOGS_ROOT)
    # 51/23 before T216 (game.outcome_state + the game.outcome capability) and T221
    # (camera.read_state) -- see tests/contract/test_catalog_integration.py's own constants.
    assert len(catalog.declarations) == 57  # +2 prompts, +cities.selection, +player.yields
    assert len(catalog.capabilities) == 26  # +cities.selection, +yields.read (2026-09-21)
    assert _UNITS_STATE in catalog.declarations
    assert _END_TURN in catalog.declarations


async def test_a_real_observation_declaration_executes_end_to_end_through_a_real_lua_file() -> None:
    """units.state (catalogs/observations/units.yaml, capability_id units.read) is backed by
    lua/gamecore/units.lua; dispatching it must genuinely load and send that file's own source,
    not a fixture standing in for it. Declared in the InGame context since T213's live pass
    (2026-09-21): GetUnitType/GetReachableMovement/CanStartOperation exist only there."""
    async with FakeNexusServer() as server:
        server.queue_response(
            {"units": []},
            match="CivSim_Units.state",
            state_index=STATE_INDEX_IN_GAME,
        )

        client = NexusClient(host="127.0.0.1", port=server.port)
        await client.connect()
        await client.resolve_game_states()
        try:
            catalog = load_catalog(CATALOGS_ROOT)
            registry = CapabilityRegistry(catalog=catalog)

            async def execute_command(state_index: int, lua_body: str) -> object:
                return await client.execute_command(state_index=state_index, lua_body=lua_body)

            executor = CapabilityExecutor(
                registry=registry,
                execute_command=execute_command,
                session=client,
                lua_root=_REPO_ROOT,
            )

            result = await executor.execute(_UNITS_STATE, context=LuaContext.IN_GAME)

            assert result.declaration_id == _UNITS_STATE
            assert result.value == {"units": []}

            # A real request genuinely reached the fake server -- never a fabricated local result.
            assert len(server.received) == 1
            assert server.received[0].state_index == STATE_INDEX_IN_GAME
            assert "CivSim_Units.state()" in server.received[0].lua_body
            assert server.unmatched_requests == []
        finally:
            await client.close()


async def test_observation_reader_and_action_executor_both_drive_real_declarations() -> None:
    """T207 and T208 both go through the same T206 executor against the same real catalog --
    one observation (units.state) and the declared end-turn action (turn.end_turn, the contract's
    own worked example)."""
    async with FakeNexusServer() as server:
        server.queue_response(
            {"units": []},
            match="CivSim_Units.state",
            state_index=STATE_INDEX_IN_GAME,
        )
        server.queue_response(
            {"ok": True, "result_is_informative": False, "path": "UI.RequestAction"},
            match="CivSim_TurnControl.end_turn",
            state_index=STATE_INDEX_IN_GAME,
        )

        client = NexusClient(host="127.0.0.1", port=server.port)
        await client.connect()
        await client.resolve_game_states()
        try:
            catalog = load_catalog(CATALOGS_ROOT)
            registry = CapabilityRegistry(catalog=catalog)

            async def execute_command(state_index: int, lua_body: str) -> object:
                return await client.execute_command(state_index=state_index, lua_body=lua_body)

            executor = CapabilityExecutor(
                registry=registry,
                execute_command=execute_command,
                session=client,
                lua_root=_REPO_ROOT,
            )

            reader = ObservationReader(
                executor=executor, registry=registry, declaration_ids=[_UNITS_STATE]
            )
            results, screen_identity = await reader()

            assert len(results) == 1
            assert results[0].declaration_id == _UNITS_STATE
            assert results[0].value == {"units": []}
            # game.screen_state was not among the configured declarations -- the reader must not
            # fabricate a screen identity it never actually read.
            assert screen_identity == UNKNOWN_SCREEN

            action_executor = ActionExecutor(executor=executor, registry=registry)
            returned = await action_executor(_END_TURN, {}, None)
            assert returned == {
                "ok": True,
                "result_is_informative": False,
                "path": "UI.RequestAction",
            }

            assert len(server.received) == 2
            assert server.received[0].state_index == STATE_INDEX_IN_GAME
            assert server.received[1].state_index == STATE_INDEX_IN_GAME
            assert "CivSim_TurnControl.end_turn()" in server.received[1].lua_body
            assert server.unmatched_requests == []
        finally:
            await client.close()
