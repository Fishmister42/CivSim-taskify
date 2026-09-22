"""`camera.move`: the plot the dispatcher sends is one table, not two scalars.

CODE-CONFIRMED LIVE 2026-09-22: `camera.move`'s live `dispatch_result` was
``{"target_plot": {"x": {"y": 31, "x": 44}}, "ok": true}`` -- the plot table nested under `x`.
`act/executor.py`'s `_build_arguments` passes a decision's `target` as the LAST positional
argument, and for a camera move the target is the destination plot `{x, y}`
(`tests/unit/test_camera_validation.py`'s own `CAMERA_MOVE` targets), so every dispatch arrived at
`lua/ingame/camera.lua` as `move({x=44, y=31, ...})`: the whole table landed in the `x` parameter
of `CivSim_Camera_Move(x, y)` with `y` nil. `UI.LookAtPlot(table, nil)` does not throw on the live
client, so `ok` came back true and the verification `camera.target_plot == target` was
unsatisfiable -- the recorded cause of `out_of_parity_camera`.

Exactly the shape of the `units.promote` bug (6606d4b) and the `cities.set_production` bug
(454b2f8) -- `lua/ingame/unit_orders.lua:107` already carried the normalisation guard for a lone
table; `camera.lua` did not.

Like `test_promote_dispatch.py` and `test_set_production_dispatch.py`, the first group here runs
the production argument-marshalling (`act/executor.ActionExecutor`) against the real shipped
catalog and feeds the arguments it produces straight into the real Lua, executed in an embedded
Lua 5.4. The engine stub faults on a non-numeric argument (the 2026-09-21 guard-test discipline,
specs/002-civ-playing-harness/spikes/client-segfault-2026-09-21.md) rather than silently accepting
one, so a regression that reintroduces the table-in-x shape is caught as a fault, not a silent
pass. Skipped, not failed, where `lupa` is absent (not a project dependency):
``uv run --with lupa pytest tests/unit/test_camera_move_dispatch.py``.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import pytest

from civsim_harness.act.executor import ActionExecutor
from civsim_harness.capability.executor import CapabilityExecutor, _lua_literal
from civsim_harness.capability.loader import load_catalog
from civsim_harness.capability.registry import CapabilityRegistry
from civsim_harness.models.common import DeclarationId, LuaContext
from civsim_harness.observe.assemble import CapabilityResult

lupa = pytest.importorskip("lupa")

REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG_ROOT = REPO_ROOT / "catalogs"
CAMERA_LUA = REPO_ROOT / "lua" / "ingame" / "camera.lua"

CAMERA_MOVE = DeclarationId("camera.move")

# The engine stub. UI.LookAtPlot is modeled to FAULT on anything that is not two numbers, the way
# the 2026-09-21 guard tests do (client-segfault-2026-09-21.md's own rule: "a native fault is the
# one failure pcall cannot turn into a reason", so the regression test must be able to see one) --
# unlike the live client, which the live incident showed does NOT throw on `LookAtPlot(table, nil)`
# and so hid the bug behind `ok: true`. Every call is recorded before the fault check runs, so a
# test can tell "never called" from "called and faulted" apart.
_STUBS = """
local M = {
    calls = 0,
    last_call = nil,
    accept = true,
}

UI = {}
function UI.LookAtPlot(x, y)
    M.calls = M.calls + 1
    M.last_call = { x = x, y = y }
    if type(x) ~= "number" or type(y) ~= "number" then
        error("stubbed UI.LookAtPlot received a non-numeric argument: x=" .. tostring(x)
              .. " y=" .. tostring(y))
    end
    return M.accept
end

Game = {}
function Game.GetLocalPlayer() return 0 end

return M
"""


@pytest.fixture
def camera() -> tuple[Any, Any]:
    runtime = lupa.LuaRuntime(unpack_returned_tuples=True)
    stubs = runtime.execute(_STUBS)
    runtime.execute(CAMERA_LUA.read_text(encoding="utf-8"))
    return runtime, stubs


def _lua_value(runtime: Any, value: Any) -> Any:
    """Encode *value* as a genuine Lua value the same way production does
    (`capability/executor.py`'s own `_lua_literal`, splicing an argument into the dispatched Lua
    source), not a raw Python object lupa would otherwise wrap opaquely. A Python dict handed
    straight to a lupa call does NOT become a Lua table -- `type(x) == "table"` inside the guard
    would read false for it, and the guard this bug is actually about would never be exercised."""
    return runtime.eval(_lua_literal(value))


def _move(runtime: Any, *args: Any) -> dict[str, Any]:
    encoded = [_lua_value(runtime, arg) for arg in args]
    return dict(runtime.globals()["CivSim_Camera"]["move"](*encoded))


# --------------------------------------------------------------------------
# What the production dispatcher actually hands the Lua, for the real declaration
# --------------------------------------------------------------------------


class _RecordingCapabilityExecutor:
    """The T206 seam: performs nothing, records the positional arguments it was handed."""

    def __init__(self) -> None:
        self.arguments: tuple[Any, ...] = ()

    async def execute(
        self,
        declaration_id: DeclarationId,
        *,
        context: LuaContext,
        arguments: Sequence[Any] = (),
    ) -> CapabilityResult:
        self.arguments = tuple(arguments)
        return CapabilityResult(declaration_id=declaration_id, value={"ok": True})


async def _dispatch_arguments(target: Any) -> tuple[Any, ...]:
    registry = CapabilityRegistry(catalog=load_catalog(CATALOG_ROOT))
    recorder = _RecordingCapabilityExecutor()
    executor = ActionExecutor(executor=cast(CapabilityExecutor, recorder), registry=registry)
    await executor(CAMERA_MOVE, {"target": target}, target)
    return recorder.arguments


async def test_the_target_plot_is_the_only_argument_the_dispatcher_sends() -> None:
    """The bug's root, pinned against the real catalog: one positional argument, a table --
    never (x, y) as two separate positionals."""
    target = {"x": 44, "y": 31, "is_revealed": True}
    assert await _dispatch_arguments(target) == (target,)


# --------------------------------------------------------------------------
# The failing-without-fix test: one table argument, y nil, must reach the engine as two scalars
# --------------------------------------------------------------------------


async def test_a_lone_table_target_reaches_the_engine_as_two_scalars_not_a_table(
    camera: tuple[Any, Any],
) -> None:
    """End to end: the production dispatcher's own arguments, fed to the real Lua. This is the
    load-bearing assertion -- confirmed to FAIL against the pre-fix file (see the task report):
    without the guard, `x` arrives at `UI.LookAtPlot` as the whole plot table and `y` as nil, the
    stub's fault check fires, and `last_call["x"]` is a Lua table rather than the number 44."""
    runtime, stubs = camera
    target = {"x": 44, "y": 31, "is_revealed": True}

    arguments = await _dispatch_arguments(target)
    result = _move(runtime, *arguments)

    last_call = dict(stubs.last_call)
    # The load-bearing assertion: two scalars reached the engine, not a table in x.
    assert isinstance(last_call["x"], int | float)
    assert isinstance(last_call["y"], int | float)
    assert last_call["x"] == 44
    assert last_call["y"] == 31
    assert stubs.calls == 1
    assert result["ok"] is True
    assert dict(result["target_plot"]) == {"x": 44, "y": 31}
    assert result.get("reason") is None


def test_two_explicit_scalars_still_work(camera: tuple[Any, Any]) -> None:
    """The (x, y) two-scalar form is kept for a caller that already has two numbers -- same
    backward-compatible shape as `unit_orders.lua`'s guard."""
    runtime, stubs = camera

    result = _move(runtime, 44, 31)

    assert result["ok"] is True
    assert dict(result["target_plot"]) == {"x": 44, "y": 31}
    assert dict(stubs.last_call) == {"x": 44, "y": 31}


# --------------------------------------------------------------------------
# Malformed input: neither two scalars nor a {x, y} table -- a named reason, never a call
# --------------------------------------------------------------------------


def test_a_table_missing_y_is_refused_before_the_engine_is_touched(
    camera: tuple[Any, Any],
) -> None:
    runtime, stubs = camera

    result = _move(runtime, {"x": 44})

    assert result["ok"] is False
    assert result["reason"] == "target_plot_invalid"
    assert stubs.calls == 0
    assert stubs.last_call is None


def test_a_table_missing_x_is_refused_before_the_engine_is_touched(
    camera: tuple[Any, Any],
) -> None:
    runtime, stubs = camera

    result = _move(runtime, {"y": 31})

    assert result["ok"] is False
    assert result["reason"] == "target_plot_invalid"
    assert stubs.calls == 0


def test_a_non_table_non_scalar_argument_is_refused_before_the_engine_is_touched(
    camera: tuple[Any, Any],
) -> None:
    """A lone string is neither the two-scalar form nor the `{x, y}` table form."""
    runtime, stubs = camera

    result = _move(runtime, "not-a-plot")

    assert result["ok"] is False
    assert result["reason"] == "target_plot_invalid"
    assert stubs.calls == 0


def test_a_table_of_non_numeric_coordinates_is_refused_before_the_engine_is_touched(
    camera: tuple[Any, Any],
) -> None:
    """A table shaped right but carrying the wrong types is not silently accepted either --
    FR-026's fail-closed direction, not just a shape check."""
    runtime, stubs = camera

    result = _move(runtime, {"x": "forty-four", "y": 31})

    assert result["ok"] is False
    assert result["reason"] == "target_plot_invalid"
    assert stubs.calls == 0


def test_no_arguments_at_all_is_refused_before_the_engine_is_touched(
    camera: tuple[Any, Any],
) -> None:
    runtime, stubs = camera

    result = _move(runtime)

    assert result["ok"] is False
    assert result["reason"] == "target_plot_invalid"
    assert stubs.calls == 0
