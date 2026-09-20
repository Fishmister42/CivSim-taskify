"""Unit tests for the capability executor (T206).

The integration-readiness audit's headline finding was that
``IntegrationCapability.implementation_ref`` is read nowhere outside its own model definition --
nothing loads or executes a single ``lua/`` file. These tests exercise
:class:`~civsim_harness.capability.executor.CapabilityExecutor` in isolation, against a small,
hand-built catalog and a recording fake transport (no real Nexus connection, no real Civ VI client)
to prove: the named Lua file is actually read from disk and its content actually reaches the
dispatched command; execution in the wrong Lua context is refused rather than silently dispatched;
the state index used for a dispatch is re-resolved, by name, on every call -- never a value cached
from construction or from an earlier call; and a missing ``implementation_ref`` file raises rather
than returning an empty or fabricated result.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from civsim_harness.capability.executor import CapabilityExecutor
from civsim_harness.capability.loader import Catalog
from civsim_harness.capability.registry import CapabilityRegistry, WrongContextError
from civsim_harness.errors import CatalogError, NexusError
from civsim_harness.models.catalog import (
    CapabilityPath,
    CatalogVersion,
    DeclarationKind,
    IntegrationCapability,
    ParityDeclaration,
)
from civsim_harness.models.common import CapabilityId, DeclarationId, LuaContext

_DECLARATION_ID = DeclarationId("test.read_value")
_CAPABILITY_ID = CapabilityId("test.capability")
_LUA_RELATIVE_PATH = "test_capability.lua"
_MARKER = "CIVSIM_TEST_MARKER_9f2a"

_LUA_SOURCE = f"""-- {_LUA_RELATIVE_PATH}
-- {_MARKER}
local function CivSim_Test_ReadValue()
    return {{ ok = true }}
end

CivSim_Test = {{
    read_value = CivSim_Test_ReadValue,
}}
"""


def _build_registry(*, context: LuaContext = LuaContext.GAME_CORE_TUNER) -> CapabilityRegistry:
    declaration = ParityDeclaration(
        declaration_id=_DECLARATION_ID,
        kind=DeclarationKind.OBSERVATION,
        summary="Test-only observation.",
        parity_basis="Look at the test value.",
        context=context,
        capability_id=_CAPABILITY_ID,
        output_schema={"type": "object"},
        introduced_in_version="test",
    )
    capability = IntegrationCapability(
        capability_id=_CAPABILITY_ID,
        path=CapabilityPath.FIRETUNER,
        implementation_ref=_LUA_RELATIVE_PATH,
        reads=["test value"],
        writes=[],
    )
    catalog = Catalog(
        root=Path("."),
        version=CatalogVersion(
            version="test", content_hash="test", declaration_ids=[_DECLARATION_ID]
        ),
        declarations=MappingProxyType({_DECLARATION_ID: declaration}),
        capabilities=MappingProxyType({_CAPABILITY_ID: capability}),
    )
    return CapabilityRegistry(catalog=catalog)


@dataclass
class _FakeStateIndices:
    by_name: dict[str, int]


class _FakeSession:
    """A minimal stand-in for a connected ``NexusClient``: exposes a mutable
    ``state_indices`` so a test can simulate a phase transition between two calls."""

    def __init__(self, by_name: dict[str, int]) -> None:
        self.state_indices = _FakeStateIndices(by_name=by_name)


class _RecordingExecuteCommand:
    """Records every ``(state_index, lua_body)`` pair dispatched, positionally -- matching
    ``saves.save_game``'s own established ``execute(state_index, lua_body)`` seam shape."""

    def __init__(self, response: Any = None) -> None:
        self.calls: list[tuple[int, str]] = []
        self._response = response if response is not None else {"ok": True}

    async def __call__(self, state_index: int, lua_body: str) -> Any:
        self.calls.append((state_index, lua_body))
        return self._response


@pytest.fixture
def lua_root(tmp_path: Path) -> Path:
    (tmp_path / _LUA_RELATIVE_PATH).write_text(_LUA_SOURCE, encoding="utf-8")
    return tmp_path


# --------------------------------------------------------------------------
# implementation_ref is actually read and dispatched
# --------------------------------------------------------------------------


async def test_execute_reads_implementation_ref_and_dispatches_the_named_lua_file(
    lua_root: Path,
) -> None:
    registry = _build_registry(context=LuaContext.GAME_CORE_TUNER)
    session = _FakeSession({"GameCore_Tuner": 3, "InGame": 4})
    command = _RecordingExecuteCommand({"ok": True, "value": 42})
    executor = CapabilityExecutor(
        registry=registry, execute_command=command, session=session, lua_root=lua_root
    )

    result = await executor.execute(_DECLARATION_ID, context=LuaContext.GAME_CORE_TUNER)

    assert result.declaration_id == _DECLARATION_ID
    assert result.value == {"ok": True, "value": 42}

    assert len(command.calls) == 1
    state_index, lua_body = command.calls[0]
    # Dispatched against the declaration's own declared context (GameCore_Tuner == index 3),
    # never a hard-coded or wrong-context index.
    assert state_index == 3
    # The real file's own content genuinely reached the dispatched command -- not a fabricated
    # or empty body standing in for it.
    assert _MARKER in lua_body
    assert "CivSim_Test.read_value()" in lua_body
    assert lua_body.strip().endswith(")))")


async def test_missing_implementation_ref_file_raises_rather_than_returning_empty(
    tmp_path: Path,
) -> None:
    registry = _build_registry(context=LuaContext.GAME_CORE_TUNER)
    session = _FakeSession({"GameCore_Tuner": 0, "InGame": 1})
    command = _RecordingExecuteCommand()
    # tmp_path is empty -- test_capability.lua was never written here.
    executor = CapabilityExecutor(
        registry=registry, execute_command=command, session=session, lua_root=tmp_path
    )

    with pytest.raises(CatalogError):
        await executor.execute(_DECLARATION_ID, context=LuaContext.GAME_CORE_TUNER)

    # Never dispatched anything on the strength of a file that does not exist.
    assert command.calls == []


# --------------------------------------------------------------------------
# Wrong-context execution refusal (FR-022, research R3)
# --------------------------------------------------------------------------


async def test_wrong_context_execution_is_refused(lua_root: Path) -> None:
    # Declared for GameCore_Tuner; requesting InGame must be refused, never silently dispatched
    # (a live spike confirmed UI/Network/UIManager are entirely absent from GameCore_Tuner --
    # dispatching the wrong way round is a real runtime hazard, not a pedantic check).
    registry = _build_registry(context=LuaContext.GAME_CORE_TUNER)
    session = _FakeSession({"GameCore_Tuner": 0, "InGame": 1})
    command = _RecordingExecuteCommand()
    executor = CapabilityExecutor(
        registry=registry, execute_command=command, session=session, lua_root=lua_root
    )

    with pytest.raises(WrongContextError):
        await executor.execute(_DECLARATION_ID, context=LuaContext.IN_GAME)

    assert command.calls == []


# --------------------------------------------------------------------------
# State index resolved by name, fresh, on every call -- never cached
# --------------------------------------------------------------------------


async def test_state_index_is_resolved_fresh_by_name_on_every_call_not_cached(
    lua_root: Path,
) -> None:
    registry = _build_registry(context=LuaContext.IN_GAME)
    session = _FakeSession({"GameCore_Tuner": 0, "InGame": 1})
    command = _RecordingExecuteCommand()
    # Constructed once, before any phase change -- exactly the scenario the task calls out:
    # indices differ by game phase, and a stale index is frequently still valid for an unrelated
    # state in the new table, so it must never be captured once and reused.
    executor = CapabilityExecutor(
        registry=registry, execute_command=command, session=session, lua_root=lua_root
    )

    await executor.execute(_DECLARATION_ID, context=LuaContext.IN_GAME)
    assert command.calls[-1][0] == 1

    # Simulate a phase transition: the same name, "InGame", now sits at a completely different
    # index (mirrors the live capture: LoadGameMenu is 18 at the Create Game screen, 112 in
    # game -- a same-named state moving to a different index within one connection).
    session.state_indices = _FakeStateIndices({"GameCore_Tuner": 50, "InGame": 112})

    await executor.execute(_DECLARATION_ID, context=LuaContext.IN_GAME)
    assert command.calls[-1][0] == 112

    assert [call[0] for call in command.calls] == [1, 112]


async def test_unresolved_state_indices_raise_nexus_error(lua_root: Path) -> None:
    registry = _build_registry(context=LuaContext.GAME_CORE_TUNER)

    class _NeverConnectedSession:
        state_indices = None

    command = _RecordingExecuteCommand()
    executor = CapabilityExecutor(
        registry=registry,
        execute_command=command,
        session=_NeverConnectedSession(),
        lua_root=lua_root,
    )

    with pytest.raises(NexusError):
        await executor.execute(_DECLARATION_ID, context=LuaContext.GAME_CORE_TUNER)
    assert command.calls == []


# --------------------------------------------------------------------------
# Lua source is cached by path
# --------------------------------------------------------------------------


async def test_lua_source_is_cached_by_path_not_reread_on_every_call(lua_root: Path) -> None:
    registry = _build_registry(context=LuaContext.GAME_CORE_TUNER)
    session = _FakeSession({"GameCore_Tuner": 0, "InGame": 1})
    command = _RecordingExecuteCommand()
    executor = CapabilityExecutor(
        registry=registry, execute_command=command, session=session, lua_root=lua_root
    )

    await executor.execute(_DECLARATION_ID, context=LuaContext.GAME_CORE_TUNER)
    first_body = command.calls[0][1]

    # Mutate the file on disk after the first call; if the executor re-read it, the second
    # dispatch's body would reflect this new content instead of the cached original.
    (lua_root / _LUA_RELATIVE_PATH).write_text(
        _LUA_SOURCE.replace(_MARKER, "CIVSIM_TEST_MARKER_CHANGED"), encoding="utf-8"
    )

    await executor.execute(_DECLARATION_ID, context=LuaContext.GAME_CORE_TUNER)
    second_body = command.calls[1][1]

    assert _MARKER in second_body
    assert "CIVSIM_TEST_MARKER_CHANGED" not in second_body
    assert first_body == second_body
