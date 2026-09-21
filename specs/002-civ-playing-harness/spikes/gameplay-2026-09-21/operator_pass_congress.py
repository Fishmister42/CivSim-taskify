"""OPERATOR SCRIPTING, not production (owner's ruling, 2026-09-21): get past the World Congress
session so play can continue while `prompt.congress_vote` is still a documented gap (the popup's
stage is private to its own Lua state; the welcome context `WorldCongressIntro` is not on the
watchlist at all -- block 21 paused on it as `UnknownScreenEncountered`, raw id `WorldCongressIntro`).

This is exactly the human's clicks, made the way the executor makes them (real clicks at the
control's own rectangle through the host input port; `capability/executor.py::_perform_host_click_if_requested`):
  1. the intro's `AcceptButton` ("Begin Voting") -> worldcongressintro.lua:26-28 `OnClose`
     (DequeuePopup + `LuaEvents.WorldCongressIntro_ShowWorldCongress`),
  2. the popup's `NextButton` through the phases (worldcongresspopup.lua:2319 `OnNext`),
  3. the popup's `AcceptButton` at the last phase (worldcongresspopup.lua:2222 `OnAccept`;
     line 409 shows it only at `PHASE_STEP_MAX`). No votes are cast: abstaining is a choice the
     human can make with the same clicks.

    uv run python specs/002-civ-playing-harness/spikes/gameplay-2026-09-21/operator_pass_congress.py [run_id]

With a run_id an `operator_intervention` RunEvent is written against that run. Never counted as a
demonstrated capability. Everything printed is read back from the client after each click.
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
from civsim_harness.host.port import InputEvent, InputEventKind, InputStatus  # noqa: E402
from civsim_harness.models.common import EventId, RunId  # noqa: E402
from civsim_harness.models.records import RunEvent, RunEventType  # noqa: E402
from civsim_harness.nexus.client import NexusClient  # noqa: E402
from civsim_harness.nexus.sentinels import LUA_JSON_PRELUDE, lua_print_json  # noqa: E402
from civsim_harness.store.sqlite_adapter import SqliteMatchStore  # noqa: E402

RUN_ID = sys.argv[1] if len(sys.argv) > 1 else None

STATE = """(function()
  local function h(path)
    local c = ContextPtr:LookUpControl(path)
    if not c then return "absent" end
    local ok, v = pcall(function() return c:IsHidden() end)
    return ok and tostring(v) or "err"
  end
  return { intro = h("/InGame/WorldCongressIntro"), intro_accept = h("/InGame/WorldCongressIntro/AcceptButton"),
           popup = h("/InGame/WorldCongressPopup"), next = h("/InGame/WorldCongressPopup/NextButton"),
           accept = h("/InGame/WorldCongressPopup/AcceptButton"), pass = h("/InGame/WorldCongressPopup/PassButton"),
           can_end_turn = tostring(UI.CanEndTurn()), turn = Game.GetCurrentGameTurn() }
end)()"""

RECT = """(function()
  local c = ContextPtr:LookUpControl("%s")
  if not c then return { ok = false, reason = "absent" } end
  local okO, ox, oy = pcall(function() return c:GetScreenOffset() end)
  local okS, sw, sh = pcall(function() return c:GetSizeVal() end)
  local okU, uw, uh = pcall(function() return UIManager:GetScreenSizeVal() end)
  if not (okO and okS) then return { ok = false, reason = "no_rect", o = tostring(ox), s = tostring(sw) } end
  return { ok = true, x = ox, y = oy, w = sw, h = sh, uw = okU and uw or nil, uh = okU and uh or nil }
end)()"""


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

    clicks: list[dict] = []

    async def click(path: str) -> dict:
        r = (await query({"r": RECT % path}))["r"]
        if not r.get("ok"):
            return {"path": path, **r}
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
        out = {"path": path, "clicked_at": [cx, cy], "scale": [round(sx, 4), round(sy, 4)],
               "status": str(res.status)}
        clicks.append(out)
        return out

    before = (await query({"s": STATE}))["s"]
    print("BEFORE:", json.dumps(before))
    state = before
    if state["intro"] == "false":
        print("CLICK intro Accept:", json.dumps(await click("/InGame/WorldCongressIntro/AcceptButton")))
        for _ in range(6):
            await asyncio.sleep(1.0)
            state = (await query({"s": STATE}))["s"]
            if state["intro"] != "false":
                break
        print("  after intro:", json.dumps(state))
    for i in range(10):
        state = (await query({"s": STATE}))["s"]
        if state["popup"] != "false":
            break
        if state["accept"] == "false":
            print(f"CLICK popup Accept (#{i}):", json.dumps(await click("/InGame/WorldCongressPopup/AcceptButton")))
        elif state["next"] == "false":
            print(f"CLICK popup Next (#{i}):", json.dumps(await click("/InGame/WorldCongressPopup/NextButton")))
        elif state["pass"] == "false":
            print(f"CLICK popup Pass (#{i}):", json.dumps(await click("/InGame/WorldCongressPopup/PassButton")))
        else:
            print("popup up but no Next/Accept/Pass shown:", json.dumps(state))
            break
        await asyncio.sleep(1.5)
    after = (await query({"s": STATE}))["s"]
    print("AFTER:", json.dumps(after))
    await client.close()

    if RUN_ID:
        store = SqliteMatchStore(REPO / "civsim-match-store.db")
        store.write_run_event(
            RunEvent(
                event_id=EventId(uuid.uuid4().hex),
                run_id=RunId(RUN_ID),
                turn_number=None,
                step_index=None,
                event_type=RunEventType.OPERATOR_INTERVENTION,
                occurred_at=datetime.now(UTC),
                detail={
                    "what": "clicked through the World Congress session (Begin Voting, Next..., Accept with no votes)",
                    "why": "WorldCongressIntro is not on the watchlist (UnknownScreenEncountered, block 21) and "
                    "prompt.congress_vote is a documented gap; play was stuck at this game turn",
                    "mechanism": "host clicks at the controls' own rectangles, as the executor clicks",
                    "clicks": clicks,
                    "before": before,
                    "after": after,
                    "counted_as_demonstrated_capability": False,
                },
            )
        )
        print("operator_intervention event written to", RUN_ID)
    return 0 if after["popup"] != "false" and after["intro"] != "false" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
