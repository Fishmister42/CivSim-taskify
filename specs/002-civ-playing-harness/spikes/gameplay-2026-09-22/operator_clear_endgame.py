"""OPERATOR SCRIPTING, not production: clear Firaxis's `EndGameMenu` (the full-screen DEFEAT
results screen) by clicking its own "Main Menu" button, the way a human clears it.

MEASURED 2026-09-22 14:53Z, live Linux client 1.0.12.9, game turn 67, Persia eliminated by
Georgia. The screen was left up because the harness has NO path to it:

  * `game.screen_state` (production chain) answered
    {"screen": "world", "raw_screen_id": "InGame", "recognized": true,
     "has_blocking_prompt": false, "prompt_options": []}
    -- the `#open == 0` branch of `CivSim_Screens_State()`, because `EndGameMenu` is not on
    `CIVSIM_SCREEN_WATCHLIST` at all. See block-04/screen-identity-false-negative.md.
  * With `has_blocking_prompt = false` and an empty `prompt_options`, the harness's prompt path
    (`prompts.orders`) has nothing to act on: there is no option list to answer. So this could not
    be recorded as a real harness action, and is recorded as an operator intervention instead --
    counted as nothing, per catalogs/README.md.

What the click does (base/assets/ui/endgame/endgamemenu.lua:340-346, bound at :1290):
`OnMainMenu` -> `Controls.Movie:Close()`, `UI.UnloadSoundBankGroup(5)`, `UI.ReleasePauseEvent()`,
`Events.ExitToMainMenu()`. The button is `<GridButton ID="MainMenuButton" .../>` in
`ButtonStack` (endgamemenu.xml:181). Clicked at its own rectangle through the host input port,
exactly as `capability/executor.py::_perform_host_click_if_requested` clicks.

    uv run python specs/002-civ-playing-harness/spikes/gameplay-2026-09-22/operator_clear_endgame.py [run_id]
"""
from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / "src"))

from civsim_harness.host.factory import get_host_platform  # noqa: E402
from civsim_harness.host.port import InputEvent, InputEventKind  # noqa: E402
from civsim_harness.models.common import EventId, RunId  # noqa: E402
from civsim_harness.models.records import RunEvent, RunEventType  # noqa: E402
from civsim_harness.nexus.client import NexusClient  # noqa: E402
from civsim_harness.nexus.sentinels import LUA_JSON_PRELUDE, lua_print_json  # noqa: E402
from civsim_harness.store.sqlite_adapter import SqliteMatchStore  # noqa: E402

RUN_ID = sys.argv[1] if len(sys.argv) > 1 else None
STORE = Path("/home/matt/CivSolver/civsim-match-store.db")
BUTTON = "/InGame/EndGameMenu/ButtonStack/MainMenuButton"

STATE = """(function()
  local function h(path)
    local c = ContextPtr:LookUpControl(path)
    if not c then return "absent" end
    local ok, v = pcall(function() return c:IsHidden() end)
    return ok and (v and "hidden" or "SHOWN") or "err"
  end
  local lp = Game.GetLocalPlayer()
  local p = Players[lp]
  return { endgame = h("/InGame/EndGameMenu"), main_menu_button = h("%s"),
           turn = Game.GetCurrentGameTurn(),
           alive = tostring(p ~= nil and p:IsAlive()) }
end)()""" % BUTTON

RECT = """(function()
  local c = ContextPtr:LookUpControl("%s")
  if not c then return { ok = false, reason = "absent" } end
  local okO, ox, oy = pcall(function() return c:GetScreenOffset() end)
  local okS, sw, sh = pcall(function() return c:GetSizeVal() end)
  local okU, uw, uh = pcall(function() return UIManager:GetScreenSizeVal() end)
  if not (okO and okS) then return { ok = false, reason = "no_rect" } end
  return { ok = true, x = ox, y = oy, w = sw, h = sh, uw = okU and uw or nil, uh = okU and uh or nil }
end)()""" % BUTTON


async def main() -> int:
    host = get_host_platform()
    proc = host.locate_game_process()
    window = host.find_game_window(proc) if proc else None
    if window is None:
        print("no game window resolved")
        return 2
    client = NexusClient(connect_timeout_s=8.0)
    indices = await client.connect()
    in_game = indices.by_name["InGame"]

    async def query(fields: dict[str, str]) -> dict:
        return await client.execute_command(
            state_index=in_game, lua_body=LUA_JSON_PRELUDE + lua_print_json(fields)
        )

    before = (await query({"s": STATE}))["s"]
    print("BEFORE:", json.dumps(before, sort_keys=True))
    if before["endgame"] != "SHOWN":
        print("EndGameMenu is not up; nothing to clear")
        await client.close()
        return 1

    r = (await query({"r": RECT}))["r"]
    print("RECT:", json.dumps(r, sort_keys=True))
    if not r.get("ok"):
        await client.close()
        return 3
    sx = window.rect.width / float(r["uw"]) if r.get("uw") else 1.0
    sy = window.rect.height / float(r["uh"]) if r.get("uh") else 1.0
    cx = int(window.rect.left + (r["x"] + r["w"] / 2) * sx)
    cy = int(window.rect.top + (r["y"] + r["h"] / 2) * sy)
    res = host.send_input(
        [
            InputEvent(kind=InputEventKind.mouse_move, x=cx, y=cy),
            InputEvent(kind=InputEventKind.mouse_click, x=cx, y=cy, button="left"),
        ]
    )
    click = {"path": BUTTON, "clicked_at": [cx, cy], "scale": [round(sx, 4), round(sy, 4)],
             "status": str(res.status)}
    print("CLICK:", json.dumps(click, sort_keys=True))

    # The tuner connection dies with the InGame state on exit to the shell, so a readback here is
    # expected to fail once the click lands. That failure IS the confirmation.
    after: dict | str
    for i in range(10):
        await asyncio.sleep(2.0)
        try:
            after = (await query({"s": STATE}))["s"]
        except Exception as exc:  # noqa: BLE001
            after = f"ingame_state_gone: {type(exc).__name__}: {exc}"
            print(f"  poll {i}: {after}")
            break
        print(f"  poll {i}: {json.dumps(after, sort_keys=True)}")
        if after.get("endgame") != "SHOWN":
            break
    print("AFTER:", json.dumps(after, sort_keys=True) if isinstance(after, dict) else after)
    try:
        await client.close()
    except Exception:  # noqa: BLE001
        pass

    if RUN_ID:
        store = SqliteMatchStore(STORE)
        store.write_run_event(
            RunEvent(
                event_id=EventId(uuid.uuid4().hex),
                run_id=RunId(RUN_ID),
                turn_number=None,
                step_index=None,
                event_type=RunEventType.OPERATOR_INTERVENTION,
                occurred_at=datetime.now(UTC),
                detail={
                    "what": "clicked EndGameMenu's own 'Main Menu' button to clear the full-screen "
                    "DEFEAT results screen at game turn 67",
                    "why": "EndGameMenu is not on CIVSIM_SCREEN_WATCHLIST, so game.screen_state "
                    "answered screen='world', recognized=true, has_blocking_prompt=false with the "
                    "modal demonstrably up; with no blocking prompt and no prompt_options the "
                    "harness's own prompt path had nothing to act on and could not clear it",
                    "mechanism": "host click at the control's own rectangle, as the executor clicks "
                    "(endgamemenu.lua:340-346 OnMainMenu -> Events.ExitToMainMenu)",
                    "click": click,
                    "before": before,
                    "after": after,
                    "evidence": "specs/002-civ-playing-harness/spikes/gameplay-2026-09-22/block-04/"
                    "screen-identity-false-negative.md",
                    "counted_as_demonstrated_capability": False,
                },
            )
        )
        print("operator_intervention event written to", RUN_ID)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
