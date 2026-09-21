"""Load a named save through the production LuaSaveLoader and report where the client landed.

    uv run python specs/002-civ-playing-harness/spikes/gameplay-2026-09-21/load_save.py <save_name> <expected_game_turn>

On a verification mismatch the load itself has already happened; this reports the far side as it
is (InGame? game turn? popup up?) rather than reloading.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / "src"))

from civsim_harness.host.factory import get_host_platform  # noqa: E402
from civsim_harness.models.records import SavePoint  # noqa: E402
from civsim_harness.nexus.client import NexusClient  # noqa: E402
from civsim_harness.nexus.sentinels import LUA_JSON_PRELUDE, lua_print_json  # noqa: E402
from civsim_harness.saves.load_game import LuaSaveLoader  # noqa: E402

SAVE = sys.argv[1]
TURN = int(sys.argv[2])


async def far_side() -> dict:
    for _ in range(20):
        client = NexusClient(connect_timeout_s=5.0)
        try:
            idx = await client.connect()
            names = idx.by_name
            if "InGame" not in names:
                return {"in_game": False, "states": sorted(names)[:8]}
            body = LUA_JSON_PRELUDE + lua_print_json({
                "turn": "Game.GetCurrentGameTurn()",
                "local_player": "Game.GetLocalPlayer()",
                "tech_popup_hidden": '(function() local c = ContextPtr:LookUpControl("/InGame/TechCivicCompletedPopup"); if c == nil then return "absent" end; return tostring(c:IsHidden()) end)()',
                "boost_popup_hidden": '(function() local c = ContextPtr:LookUpControl("/InGame/BoostUnlockedPopup"); if c == nil then return "absent" end; return tostring(c:IsHidden()) end)()',
                "cities": '(function() local n=0; for _,c in Players[Game.GetLocalPlayer()]:GetCities():Members() do n=n+1 end; return n end)()',
                "research": '(function() local t = Players[Game.GetLocalPlayer()]:GetTechs():GetResearchingTech(); return tostring(t) end)()',
            })
            out = await client.execute_command(state_index=names["InGame"], lua_body=body)
            return {"in_game": True, **(out if isinstance(out, dict) else {"raw": str(out)})}
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(3.0)
        finally:
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass
    return {"in_game": None, "error": last}


async def main() -> int:
    host = get_host_platform()
    client = NexusClient()
    await client.connect()
    loader = LuaSaveLoader(client, host=host, load_timeout_s=240.0, exit_to_menu_timeout_s=120.0, verify_timeout_s=120.0)
    sp = SavePoint(save_point_id="sp-gameplay-load", run_id="run-gameplay-load", turn_number=TURN,
                   save_name=SAVE, taken_at=datetime.now(UTC), verified=True, retention_status="retained")
    t0 = time.perf_counter()
    result: dict = {"save": SAVE, "expected_turn": TURN}
    try:
        await loader.load(sp)
        result["load"] = f"ok in {time.perf_counter() - t0:.1f}s"
    except Exception as exc:  # noqa: BLE001
        result["load"] = f"raised after {time.perf_counter() - t0:.1f}s: {type(exc).__name__}: {exc}"
        result["detail"] = str(getattr(exc, "detail", "") or "")
    finally:
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            pass
    result["intro_presses"] = getattr(loader, "_intro_presses", None)
    result["far_side"] = await far_side()
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
