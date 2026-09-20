"""T077 part 3: does Network.SaveGame actually produce a file? Existence != works."""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

from nexus_probe import Probe

USERDATA = Path.home() / ".local/share/aspyr-media/Sid Meier's Civilization VI"
OUT = Path(__file__).parent / "t077_raw"
PREFIX = re.compile(r"^O\x00[A-Za-z_0-9]+:\s?")


def clean(t: str) -> str:
    return "\n".join(
        PREFIX.sub("", ln).rstrip() for ln in t.splitlines() if PREFIX.sub("", ln).strip()
    )


def snapshot() -> set[str]:
    return {str(p.relative_to(USERDATA)) for p in USERDATA.rglob("*") if p.is_file()}


SAVE_NAME = "civsim__spike__t0001"

SAVE_LUA = f"""
local gameFile = {{}}
gameFile.Name = "{SAVE_NAME}"
gameFile.Location = SaveLocations.LOCAL_STORAGE
gameFile.Type = SaveTypes.SINGLE_PLAYER
gameFile.IsAutosave = false
gameFile.IsQuicksave = false
gameFile.GameType = SaveTypes.SINGLE_PLAYER
print("turn before save: " .. tostring(Game.GetCurrentGameTurn()))
print("SaveLocations.LOCAL_STORAGE = " .. tostring(SaveLocations.LOCAL_STORAGE))
print("SaveTypes.SINGLE_PLAYER = " .. tostring(SaveTypes.SINGLE_PLAYER))
local ok, err = pcall(function() return Network.SaveGame(gameFile) end)
print("Network.SaveGame pcall ok = " .. tostring(ok))
print("Network.SaveGame returned/err = " .. tostring(err))
"""

before = snapshot()
print(f"files before: {len(before)}")

p = Probe()
states = p.handshake()
res, raw = p.exec_lua(states["InGame"], SAVE_LUA, wait=10.0)
body = clean(res if res else raw)
print("--- lua output ---")
print(body)
p.close()

print("\nwaiting for disk...")
new: set[str] = set()
for _ in range(20):
    time.sleep(1.5)
    new = snapshot() - before
    if any(".Civ6Save" in f for f in new):
        break

print(f"--- new files ({len(new)}) ---")
for f in sorted(new):
    full = USERDATA / f
    print(f"  {f}   ({full.stat().st_size} bytes)")

saves = [f for f in new if f.lower().endswith(".civ6save")]
report = [
    "## Network.SaveGame live test",
    "",
    "```",
    body,
    "```",
    "",
    f"New files after call ({len(new)}):",
    "",
    "```",
    *[f"{f}   ({(USERDATA / f).stat().st_size} bytes)" for f in sorted(new)],
    "```",
    "",
    f"VERDICT: {'SAVE FILE CREATED' if saves else 'NO .Civ6Save PRODUCED'}",
]
(OUT / "savegame_livetest.md").write_text("\n".join(report))
print("\nVERDICT:", "SAVE FILE CREATED" if saves else "NO .Civ6Save PRODUCED")
