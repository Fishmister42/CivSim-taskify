"""Can we identify the topmost/open UI screen from the tuner?

No UIManager.GetTopmostScreen exists. But every Civ VI UI screen is its OWN Lua
state, and each has a ContextPtr. Hypothesis: ContextPtr:IsHidden() per screen
state reveals which screens are open. Proof = flip a known screen and re-read.
"""
from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

from nexus_probe import Probe

PREFIX = re.compile(r"^O\x00[A-Za-z_0-9]+:\s?")
OUT = Path(__file__).parent / "sweep_raw"
OUT.mkdir(exist_ok=True)

# Screens whose open/closed state the harness would need for FR-010 / FR-049.
WATCH = [
    "PausePanel", "SaveGameMenu", "LoadGameMenu", "Options", "InGameTopOptionsMenu",
    "InGamePopup", "DiplomacyActionView", "DiplomacyDealView", "DeclareWarPopup",
    "UnitPromotionPopup", "EventPopup", "PantheonChooser", "GreatPeoplePopup",
    "TechTree", "CivicsTree", "ProductionPanel", "CityPanel", "WorldCongressPopup",
    "EraCompletePopup", "NaturalWonderPopup", "LeaderScene", "CivilopediaScreen",
    "TechCivicCompletedPopup", "BoostUnlockedPopup", "ReligionScreen", "GovernmentScreen",
]

PROBE = """
local r = "?"
local ok, err = pcall(function()
  if ContextPtr == nil then r = "NO_CONTEXTPTR"
  elseif ContextPtr.IsHidden == nil then r = "NO_ISHIDDEN"
  else r = tostring(ContextPtr:IsHidden()) end
end)
if not ok then r = "ERR:" .. tostring(err) end
print("ishidden=" .. r)
"""


def clean(t: str) -> str:
    return "\n".join(
        PREFIX.sub("", ln).rstrip() for ln in t.splitlines() if PREFIX.sub("", ln).strip()
    )


def read_all(p: Probe, states: dict[str, int]) -> dict[str, str]:
    out = {}
    for name in WATCH:
        idx = states.get(name)
        if idx is None:
            out[name] = "STATE_ABSENT"
            continue
        res, raw = p.exec_lua(idx, PROBE, wait=3.0)
        body = clean(res if res else raw)
        m = re.search(r"ishidden=(\S+)", body)
        out[name] = m.group(1) if m else f"NO_ANSWER({body[:40]})"
    return out


p = Probe()
states = p.handshake()
print("--- baseline (expect: gameplay screens open, menus hidden) ---")
base = read_all(p, states)
for k, v in base.items():
    print(f"  {k:<26} hidden={v}")
p.close()

# Flip a known screen: Escape opens the in-game pause/options menu.
win = subprocess.run(
    "wmctrl -l | grep -i Civilization | awk '{print $1}'", shell=True,
    capture_output=True, text=True).stdout.strip()
print(f"\n--- opening pause menu via Escape (window {win}) ---")
subprocess.run(f"wmctrl -i -a {win}", shell=True)
time.sleep(1.5)
subprocess.run(f"xdotool key --window {win} Escape", shell=True)
time.sleep(3)

p2 = Probe()
states2 = p2.handshake()
after = read_all(p2, states2)
p2.close()

print("\n--- CHANGED after Escape ---")
changed = {k: (base.get(k), after.get(k)) for k in WATCH if base.get(k) != after.get(k)}
if changed:
    for k, (b, a) in changed.items():
        print(f"  {k:<26} {b}  ->  {a}")
else:
    print("  (nothing changed)")

lines = ["# ContextPtr:IsHidden() per screen state", "",
         "| Screen state | baseline hidden | after Escape |", "|---|---|---|"]
for k in WATCH:
    mark = "  **<-- CHANGED**" if k in changed else ""
    lines.append(f"| `{k}` | `{base.get(k)}` | `{after.get(k)}`{mark} |")
(OUT / "screen_identity.md").write_text("\n".join(lines))
print(f"\nwrote {OUT / 'screen_identity.md'}")
