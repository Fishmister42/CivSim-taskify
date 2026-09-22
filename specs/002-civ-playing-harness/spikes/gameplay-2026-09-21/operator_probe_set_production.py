"""Operator probe (labelled, never counted): does a script-issued BUILD ever fill the capital's queue?

Issues exactly what the shipped non-panel set-production does (tutorialuiroot.lua ResetProduction):
  { [PARAM_UNIT_TYPE] = GameInfo.Units["UNIT_BUILDER"].Hash, [PARAM_INSERT_MODE] = VALUE_EXCLUSIVE }
via CityManager.RequestOperation(city, CityOperationTypes.BUILD, t) from InGame, after asking
CanStartOperation with the identical table, then reads the build queue at +1, +3, +6 and +10 s.

Usage: python operator_probe_set_production.py [run_id]
Records an operator_intervention event on run_id when given.
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

RUN_ID = sys.argv[1] if len(sys.argv) > 1 else None

STATE = """(function()
  local lp = Game.GetLocalPlayer()
  local city = nil
  for _, c in Players[lp]:GetCities():Members() do city = c; break end
  if city == nil then return { ok = false, reason = "no city" } end
  local bq = city:GetBuildQueue()
  local size = -1; pcall(function() size = bq:GetSize() end)
  local cur = -2; pcall(function() cur = bq:GetCurrentProductionTypeHash() end)
  local curname = "nil"
  if cur ~= nil and cur ~= 0 and cur ~= -1 and cur ~= -2 then
    for row in GameInfo.Units() do if row.Hash == cur then curname = row.UnitType end end
    for row in GameInfo.Buildings() do if row.Hash == cur then curname = row.BuildingType end end
    for row in GameInfo.Districts() do if row.Hash == cur then curname = row.DistrictType end end
  end
  local cb = "n/a"; pcall(function() cb = tostring(bq:CurrentlyBuilding()) end)
  local tl = -1; pcall(function() tl = bq:GetTurnsLeft() end)
  local at0 = "n/a"; pcall(function() local e = bq:GetAt(0); at0 = tostring(e and (e.UnitType or e.BuildingType or e.DistrictType or e.ProjectType) or "nil") end)
  return { ok = true, city_id = city:GetID(), size = size, cur_hash = cur, cur_name = curname,
           currently_building = cb, turns_left = tl, at0 = at0, game_turn = Game.GetCurrentGameTurn() }
end)()"""

ISSUE = """(function()
  local lp = Game.GetLocalPlayer()
  local city = nil
  for _, c in Players[lp]:GetCities():Members() do city = c; break end
  local t = {}
  t[CityOperationTypes.PARAM_UNIT_TYPE] = GameInfo.Units["UNIT_BUILDER"].Hash
  t[CityOperationTypes.PARAM_INSERT_MODE] = CityOperationTypes.VALUE_EXCLUSIVE
  local can, results = "n/a", nil
  local okc, errc = pcall(function() can, results = CityManager.CanStartOperation(city, CityOperationTypes.BUILD, t, true) end)
  local reasons = {}
  pcall(function()
    if results ~= nil and results[CityOperationResults.FAILURE_REASONS] ~= nil then
      for _, r in ipairs(results[CityOperationResults.FAILURE_REASONS]) do table.insert(reasons, Locale.Lookup(r)) end
    end
  end)
  local okr, errr = pcall(function() CityManager.RequestOperation(city, CityOperationTypes.BUILD, t) end)
  return { can_start = tostring(can), can_ok = okc, can_err = tostring(errc), failure_reasons = reasons,
           request_ok = okr, request_err = tostring(errr), unit_hash = t[CityOperationTypes.PARAM_UNIT_TYPE],
           insert_mode = tostring(CityOperationTypes.VALUE_EXCLUSIVE) }
end)()"""


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
    issued = (await query({"r": ISSUE}))["r"]
    print("ISSUE:", json.dumps(issued))
    reads = {}
    elapsed = 0.0
    for wait in (1, 2, 3, 4):
        await asyncio.sleep(wait)
        elapsed += wait
        reads[f"+{int(elapsed)}s"] = (await query({"s": STATE}))["s"]
        print(f"  +{int(elapsed)}s:", json.dumps(reads[f"+{int(elapsed)}s"]))
    await client.close()

    landed = any(r.get("size", -1) and r.get("size", -1) > 0 for r in reads.values())
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
                    "what": "operator probe: script-issued BUILD (UNIT_BUILDER, VALUE_EXCLUSIVE) on the capital, queue read at +1/+3/+6/+10 s",
                    "why": "cities.set_production rejected 24/24 under the 4 s bounded re-read (run-ba3ad80d) with last_read production_queue [] -- split 'never lands' from 'harness queue read is wrong'",
                    "mechanism": "CityManager.CanStartOperation + RequestOperation(city, BUILD, {PARAM_UNIT_TYPE=hash, PARAM_INSERT_MODE=VALUE_EXCLUSIVE}) -- tutorialuiroot.lua ResetProduction",
                    "before": before,
                    "issued": issued,
                    "reads": reads,
                    "landed_in_queue": landed,
                    "counted_as_demonstrated_capability": False,
                },
            )
        )
        print("operator_intervention event written to", RUN_ID)
    print("LANDED:", landed)
    return 0 if landed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
