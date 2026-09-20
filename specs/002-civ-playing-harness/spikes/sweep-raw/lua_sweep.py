"""Lua API verification sweep: existence-probe every symbol the catalog authors
marked UNVERIFIED, in both tuner contexts. Non-mutating."""
from __future__ import annotations

import re
from pathlib import Path

from nexus_probe import Probe

OUT = Path(__file__).parent / "sweep_raw"
OUT.mkdir(exist_ok=True)
PREFIX = re.compile(r"^O\x00[A-Za-z_0-9]+:\s?")


def clean(t: str) -> str:
    return "\n".join(
        PREFIX.sub("", ln).rstrip() for ln in t.splitlines() if PREFIX.sub("", ln).strip()
    )


GROUPS: dict[str, list[str]] = {
    "P1_turn_control": [
        "Game.EndTurn", "Game.SetCurrentGameTurn", "Game.GetCurrentGameTurn",
        "Game.GetCurrentTurnSegment", "Game.GetCurrentTurnPhase", "Game.GetGameEndTurn",
        "Game.GetMaxGameTurns",
        "UI.RequestAction", "UI.RequestPlayerOperation", "UI.CanEndTurn",
        "ActionTypes.ACTION_ENDTURN", "ActionTypes.ACTION_NEXTREADYUNIT",
        "Network.SendPlayerOperation", "Network.SendAction",
        "PlayerOperations.ENDTURN", "PlayerOperationTypes.ENDTURN",
        "EndTurnBlockingTypes.NO_ENDTURN_BLOCKING",
        "NotificationManager.GetCount",
        "UI.GetHeadSelectedUnit", "UI.SelectNextReadyUnit",
    ],
    "P2_screens": [
        "UIManager.GetScreen", "UIManager.IsInPopupQueue", "UIManager.GetCurrentPopup",
        "UIManager.QueuePopup", "UIManager.DequeuePopup", "UIManager.ClosePopup",
        "UIManager.GetTopmostScreen", "UIManager.IsPopupQueueEmpty",
        "UI.GetInterfaceMode", "UI.SetInterfaceMode",
        "InterfaceModeTypes.SELECTION", "InterfaceModeTypes.CITY_MANAGEMENT",
        "ContextPtr", "Controls", "LuaEvents", "Events",
        "UI.IsScreenOpen", "UI.GetScreenName",
    ],
    "P3_congress_greatpeople_religion": [
        "Game.GetWorldCongress", "Game.GetReligion", "Game.GetGreatPeople",
        "Game.GetEmergencyManager", "Game.GetHistoryManager", "Game.GetEras",
        "Game.GetQuestsManager", "Game.GetGossipManager",
    ],
    "P4_spotchecks": [
        "Map.GetGridSize", "Map.GetPlot", "Map.GetPlotByIndex", "Map.GetPlotDistance",
        "UnitManager.RequestOperation", "UnitManager.RequestCommand",
        "UnitManager.GetOperationTargets", "UnitManager.CanStartOperation",
        "UnitOperationTypes.PARAM_X", "UnitOperationTypes.PARAM_Y",
        "UnitOperationTypes.FOUND_CITY", "UnitOperationTypes.MOVE_TO",
        "UnitCommandTypes.PARAM_X",
        "CityManager.RequestOperation", "CityManager.RequestCommand",
        "CityCommandTypes.PARAM_X",
        "Players", "PlayerManager.GetAlive",
        "Cities.GetCity", "Units.GetUnit",
    ],
    "P5_json_and_misc": [
        "json", "JSON", "cjson", "dkjson", "Serialize", "DB", "Locale",
        "GameInfo", "GameConfiguration", "PlayerConfigurations", "MapConfiguration",
        "Modding", "Options", "UserConfiguration",
        "os", "io", "require", "loadstring", "debug",
    ],
}


def build_lua(names: list[str]) -> str:
    out = ['print("=== PROBE ===")']
    for dotted in names:
        out.append(
            f'do local ok, v = pcall(function() return {dotted} end)\n'
            f'  if not ok then print("  {dotted}  ->  ERROR(root missing)")\n'
            f'  else print("  {dotted}  ->  " .. type(v)) end end'
        )
    return "\n".join(out)


p = Probe()
states = p.handshake()
summary = []
for label in ("GameCore_Tuner", "InGame"):
    idx = states[label]
    for gname, names in GROUPS.items():
        res, raw = p.exec_lua(idx, build_lua(names), wait=10.0)
        body = clean(res if res else raw)
        (OUT / f"{label}__{gname}.txt").write_text(body)
        present = len([l for l in body.splitlines() if "->" in l and "nil" not in l and "ERROR" not in l])
        total = len(names)
        summary.append(f"{label}/{gname}: {present}/{total} present")
p.close()
print("\n".join(summary))
