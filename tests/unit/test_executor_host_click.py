"""The executor completes a Lua `requires_host_click` result with a real synthetic click.

Measured 2026-09-21 (gameplay blocks 13 and 14): no Lua API reachable from InGame fires a
control's registered callback -- `control:CallCallback(Mouse.eLClick)` returned without error and
did nothing, and it appears nowhere in Firaxis' shipped UI Lua. The button a human clicks can only
be clicked as a human clicks it, so `lua/ingame/screens.lua` returns the button's own on-screen
rectangle and `CapabilityExecutor` clicks its centre through the host's synthetic-input port
(constitution Principle II: a bespoke path for a documented Firetuner gap).

These tests pin the contract: the click lands at the rectangle's centre offset by the game
window's screen origin; without a host, a window, or a well-formed rectangle the result stays
`ok=false` with its own reason and is never reported clicked; and an input failure is recorded,
not hidden.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from civsim_harness.capability.executor import CapabilityExecutor
from civsim_harness.capability.loader import Catalog
from civsim_harness.capability.registry import CapabilityRegistry
from civsim_harness.host.port import (
    GameWindow,
    InputEvent,
    InputEventKind,
    InputResult,
    InputStatus,
    WindowRect,
)
from civsim_harness.models.catalog import (
    CapabilityPath,
    CatalogVersion,
    DeclarationKind,
    IntegrationCapability,
    ParityDeclaration,
)
from civsim_harness.models.common import CapabilityId, DeclarationId, LuaContext
from fakes.fake_host import FakeHostPlatform

_DECLARATION_ID = DeclarationId("prompts.ai_diplomatic_approach")
_CAPABILITY_ID = CapabilityId("prompts.orders")
_LUA_RELATIVE_PATH = "lua/ingame/screens.lua"
_LUA_SOURCE = f"""-- {_LUA_RELATIVE_PATH} (test stub: the transport fake answers)
local function CivSim_Screens_RespondToPrompt(promptType, optionId)
    return {{ ok = false }}
end

CivSim_Screens = {{
    respond = CivSim_Screens_RespondToPrompt,
}}
"""

_RECT = {"x": 100.0, "y": 200.0, "w": 50.0, "h": 20.0}
_WINDOW = GameWindow(
    handle=0x1,
    title="Sid Meier's Civilization VI",
    rect=WindowRect(left=2560, top=0, width=1920, height=1200),
    pid=4242,
)


def _build_registry() -> CapabilityRegistry:
    declaration = ParityDeclaration(
        declaration_id=_DECLARATION_ID,
        kind=DeclarationKind.ACTION,
        summary="Answer an AI leader's diplomatic approach.",
        parity_basis="Click the statement a human would click.",
        context=LuaContext.IN_GAME,
        capability_id=_CAPABILITY_ID,
        availability_predicate="game.has_blocking_prompt",
        verification_predicate='game.current_screen != "prompt.diplomatic_approach"',
        output_schema={"type": "object"},
        introduced_in_version="test",
    )
    capability = IntegrationCapability(
        capability_id=_CAPABILITY_ID,
        path=CapabilityPath.FIRETUNER,
        implementation_ref=_LUA_RELATIVE_PATH,
        reads=[],
        writes=["the diplomacy session"],
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
    def __init__(self, by_name: dict[str, int]) -> None:
        self.state_indices = _FakeStateIndices(by_name=by_name)


class _Command:
    def __init__(self, response: Any) -> None:
        self._response = response
        self.calls: list[tuple[int, str]] = []

    async def __call__(self, state_index: int, lua_body: str) -> Any:
        self.calls.append((state_index, lua_body))
        return self._response


class _RecordingHost(FakeHostPlatform):
    """The repo's fake host, recording every synthetic input batch and answering as scripted."""

    def __init__(self, *, status: InputStatus = InputStatus.ok, reason: str | None = None) -> None:
        super().__init__()
        self.batches: list[list[InputEvent]] = []
        self._status = status
        self._reason = reason

    def send_input(self, events: Any) -> InputResult:
        self.batches.append(list(events))
        return InputResult(status=self._status, reason=self._reason)


def _lua_root(tmp_path: Path) -> Path:
    target = tmp_path / _LUA_RELATIVE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_LUA_SOURCE, encoding="utf-8")
    return tmp_path


def _requires_click(*, ui_screen: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "ok": False,
        "reason": "requires_host_click",
        "mechanism": "host_click_at_control_rect",
        "click": dict(_RECT),
        "ui_screen": ui_screen,
        "prompt": "prompt.diplomatic_approach",
        "option": "No time for further pleasantries.",
    }


async def test_a_ui_space_rect_is_scaled_onto_the_window_before_the_click(
    tmp_path: Path,
) -> None:
    """Measured 2026-09-21: the UI lays out in 1024x768 and the engine stretches it onto the
    1920x1200 window; a click at the unscaled position hit empty scene, one at the scaled
    position answered the greeting."""
    host = _RecordingHost()
    executor = CapabilityExecutor(
        registry=_build_registry(),
        execute_command=_Command(_requires_click(ui_screen={"w": 1024, "h": 768})),
        session=_FakeSession({"InGame": 4}),
        lua_root=_lua_root(tmp_path),
        host=host,
        window_resolver=lambda: _WINDOW,
    )

    result = await executor.execute(_DECLARATION_ID, context=LuaContext.IN_GAME)

    assert result.value["ok"] is True
    assert result.value["mechanism"] == "host_click_at_control_rect"
    # rect centre (125, 210) in UI space -> (234.375, 328.125) px, offset by the origin (2560, 0)
    assert result.value["clicked_at"] == {
        "x": 2794,
        "y": 328,
        "window_origin": [2560, 0],
        "scale": [1.875, 1.5625],
        "scale_source": "window_over_ui_screen",
    }
    assert len(host.batches) == 1
    kinds = [event.kind for event in host.batches[0]]
    assert kinds == [InputEventKind.mouse_move, InputEventKind.mouse_click]
    assert all(event.x == 2794 and event.y == 328 for event in host.batches[0])
    assert host.batches[0][1].button == "left"


async def test_without_a_reported_ui_size_the_rect_is_taken_as_pixels(tmp_path: Path) -> None:
    host = _RecordingHost()
    executor = CapabilityExecutor(
        registry=_build_registry(),
        execute_command=_Command(_requires_click()),
        session=_FakeSession({"InGame": 4}),
        lua_root=_lua_root(tmp_path),
        host=host,
        window_resolver=lambda: _WINDOW,
    )

    result = await executor.execute(_DECLARATION_ID, context=LuaContext.IN_GAME)

    assert result.value["ok"] is True
    # centre of the rect (125, 210) offset by the window origin (2560, 0), unscaled
    assert result.value["clicked_at"]["x"] == 2685
    assert result.value["clicked_at"]["y"] == 210
    assert result.value["clicked_at"]["scale_source"] == "assumed_1_to_1"


async def test_without_a_host_the_result_stays_unclicked_with_its_own_reason(
    tmp_path: Path,
) -> None:
    executor = CapabilityExecutor(
        registry=_build_registry(),
        execute_command=_Command(_requires_click()),
        session=_FakeSession({"InGame": 4}),
        lua_root=_lua_root(tmp_path),
    )

    result = await executor.execute(_DECLARATION_ID, context=LuaContext.IN_GAME)

    assert result.value["ok"] is False
    assert result.value["reason"] == "requires_host_click"
    assert result.value["host_click"] == "no_host_input_port"


async def test_an_unresolvable_window_never_clicks(tmp_path: Path) -> None:
    host = _RecordingHost()
    executor = CapabilityExecutor(
        registry=_build_registry(),
        execute_command=_Command(_requires_click()),
        session=_FakeSession({"InGame": 4}),
        lua_root=_lua_root(tmp_path),
        host=host,
        window_resolver=lambda: None,
    )

    result = await executor.execute(_DECLARATION_ID, context=LuaContext.IN_GAME)

    assert result.value["ok"] is False
    assert result.value["host_click"] == "game_window_unresolved"
    assert host.batches == []


async def test_a_malformed_rect_never_clicks(tmp_path: Path) -> None:
    host = _RecordingHost()
    response = _requires_click()
    response["click"] = {"x": 1, "y": 2}  # no size
    executor = CapabilityExecutor(
        registry=_build_registry(),
        execute_command=_Command(response),
        session=_FakeSession({"InGame": 4}),
        lua_root=_lua_root(tmp_path),
        host=host,
        window_resolver=lambda: _WINDOW,
    )

    result = await executor.execute(_DECLARATION_ID, context=LuaContext.IN_GAME)

    assert result.value["ok"] is False
    assert result.value["host_click"] == "rect_malformed"
    assert host.batches == []


async def test_an_input_failure_is_recorded_not_reported_as_a_click(tmp_path: Path) -> None:
    host = _RecordingHost(status=InputStatus.failed, reason="XTest dispatch failed: stubbed")
    executor = CapabilityExecutor(
        registry=_build_registry(),
        execute_command=_Command(_requires_click()),
        session=_FakeSession({"InGame": 4}),
        lua_root=_lua_root(tmp_path),
        host=host,
        window_resolver=lambda: _WINDOW,
    )

    result = await executor.execute(_DECLARATION_ID, context=LuaContext.IN_GAME)

    assert result.value["ok"] is False
    assert result.value["host_click"] == "input_failed"
    assert result.value["host_click_status"] == "failed"
    assert "stubbed" in result.value["host_click_reason"]
    assert len(host.batches) == 1


async def test_an_ordinary_result_passes_through_untouched(tmp_path: Path) -> None:
    host = _RecordingHost()
    executor = CapabilityExecutor(
        registry=_build_registry(),
        execute_command=_Command({"ok": True, "mechanism": "close_control_callback"}),
        session=_FakeSession({"InGame": 4}),
        lua_root=_lua_root(tmp_path),
        host=host,
        window_resolver=lambda: _WINDOW,
    )

    result = await executor.execute(_DECLARATION_ID, context=LuaContext.IN_GAME)

    assert result.value == {"ok": True, "mechanism": "close_control_callback"}
    assert host.batches == []
