"""OPERATOR SCRIPTING, not production (owner's ruling, 2026-09-21 12:30 EDT): answer an AI
leader's first-meeting greeting so play can continue while `prompt.diplomatic_approach` is still
unmapped in the catalog. This is exactly the human's click on the greeting's decline button --
`OnSelectConversationDiplomacyStatement("CHOICE_NEGATIVE")` in Firaxis' diplomacyactionview.lua
(lines 490-531) runs `DiplomacyManager.AddResponse(sessionID, localPlayer, "NEGATIVE")`; the
session id comes from `DiplomacyManager.FindOpenSessionID(localPlayer, otherPlayer)` (line 2048).

    uv run python specs/002-civ-playing-harness/spikes/gameplay-2026-09-21/operator_answer_greeting.py <other_player_id> [run_id]

With a run_id, an `operator_intervention` RunEvent is written to the repo-root store against that
run so the record says this was not the agent's doing. It is never counted as a demonstrated
capability. Everything printed is read back from the client after the response.
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

from civsim_harness.models.common import EventId, RunId  # noqa: E402
from civsim_harness.models.records import RunEvent, RunEventType  # noqa: E402
from civsim_harness.nexus.client import NexusClient  # noqa: E402
from civsim_harness.nexus.sentinels import LUA_JSON_PRELUDE, lua_print_json  # noqa: E402
from civsim_harness.store.sqlite_adapter import SqliteMatchStore  # noqa: E402

OTHER = int(sys.argv[1])
RUN_ID = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] != "close" else None
# MEASURED 12:35 EDT: `AddResponse(..., "NEGATIVE")` returned ok, the leader answered, and the
# session stayed open with the scene up -- the human's second click is the conversation's Exit,
# `ExitConversationMode()` -> `DiplomacyManager.CloseSession(sessionID)` (diplomacyactionview.lua
# lines 312-324, no queued session). `close` as the last argument issues that click instead.
MODE = "close" if sys.argv[-1] == "close" else "respond"

STATE = """(function()
  local lp = Game.GetLocalPlayer()
  local okS, sid = pcall(function() return DiplomacyManager.FindOpenSessionID(lp, %d) end)
  local dav = ContextPtr:LookUpControl("/InGame/DiplomacyActionView")
  local ls = ContextPtr:LookUpControl("/InGame/LeaderScene")
  return { session_open = (okS and sid ~= nil and sid ~= -1) and sid or -1,
           dav_hidden = dav and tostring(dav:IsHidden()) or "absent",
           leaderscene_hidden = ls and tostring(ls:IsHidden()) or "absent",
           can_end_turn = tostring(UI.CanEndTurn()), turn = Game.GetCurrentGameTurn() }
end)()""" % OTHER

RESPOND = """(function()
  local lp = Game.GetLocalPlayer()
  local sid = DiplomacyManager.FindOpenSessionID(lp, %d)
  if sid == nil or sid == -1 then return { ok = false, reason = "no open session with that player" } end
  local ok, err = pcall(function() DiplomacyManager.AddResponse(sid, lp, "NEGATIVE") end)
  return { ok = ok, err = tostring(err), session_id = sid, response = "NEGATIVE", choice_key = "CHOICE_NEGATIVE" }
end)()""" % OTHER

CLOSE = """(function()
  local lp = Game.GetLocalPlayer()
  local sid = DiplomacyManager.FindOpenSessionID(lp, %d)
  if sid == nil or sid == -1 then return { ok = false, reason = "no open session with that player" } end
  local ok, err = pcall(function() DiplomacyManager.CloseSession(sid) end)
  return { ok = ok, err = tostring(err), session_id = sid, mechanism = "DiplomacyManager.CloseSession (the conversation's Exit click)" }
end)()""" % OTHER


async def main() -> int:
    client = NexusClient(connect_timeout_s=8.0)
    indices = await client.connect()
    in_game = indices.by_name["InGame"]

    async def query(fields: dict[str, str]) -> dict:
        return await client.execute_command(
            state_index=in_game, lua_body=LUA_JSON_PRELUDE + lua_print_json(fields)
        )

    before = (await query({"s": STATE}))["s"]
    print("BEFORE:", json.dumps(before))
    responded = (await query({"r": CLOSE if MODE == "close" else RESPOND}))["r"]
    print("CLOSE:" if MODE == "close" else "RESPOND:", json.dumps(responded))
    after = before
    for i in range(8):
        await asyncio.sleep(1.0)
        after = (await query({"s": STATE}))["s"]
        print(f"  +{i + 1}s:", json.dumps(after))
        if after["dav_hidden"] == "true" and after["session_open"] == -1:
            break
    await client.close()

    if RUN_ID:
        store = SqliteMatchStore(REPO / "civsim-match-store.db")
        event = RunEvent(
            event_id=EventId(uuid.uuid4().hex),
            run_id=RunId(RUN_ID),
            turn_number=None,
            step_index=None,
            event_type=RunEventType.OPERATOR_INTERVENTION,
            occurred_at=datetime.now(UTC),
            detail={
                "what": "answered an AI leader's first-meeting greeting (decline)",
                "why": "prompt.diplomatic_approach maps to no Lua state yet; the harness saw the "
                "non-blocking 'diplomacy' screen and no catalog action could answer it; play "
                "was stuck at this game turn (owner's ruling: clear it as operator scripting)",
                "mechanism": "DiplomacyManager.AddResponse(session, local_player, 'NEGATIVE') -- "
                "the CHOICE_NEGATIVE button's own handler in diplomacyactionview.lua",
                "other_player_id": OTHER,
                "before": before,
                "after": after,
                "counted_as_demonstrated_capability": False,
            },
        )
        store.write_run_event(event)
        print("operator_intervention event written to", RUN_ID)
    return 0 if after["dav_hidden"] == "true" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
