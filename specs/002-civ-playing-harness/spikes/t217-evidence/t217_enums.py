"""T217 step 2: read the enum members Firaxis' loader uses, by direct global
reference (`_ENV` is nil in this sandbox, so the previous read said nothing)."""
from __future__ import annotations

import sys

sys.path.insert(0, "/home/matt/CivSolver/specs/002-civ-playing-harness/spikes/r5-raw")
from nexus_probe import Probe  # noqa: E402

LUA = r"""
local function show(label, fn)
  local ok, v = pcall(fn)
  print(string.format("  %-5s %-44s = %s", ok and "OK" or "ERR", label, tostring(v)))
end

print("-- existence of the enum tables themselves --")
show("type(SaveLocations)",   function() return type(SaveLocations) end)
show("type(SaveTypes)",       function() return type(SaveTypes) end)
show("type(SaveDirectories)", function() return type(SaveDirectories) end)
show("type(ServerType)",      function() return type(ServerType) end)
show("type(SaveFileTypes)",   function() return type(SaveFileTypes) end)

print("-- the exact members automation_dailysmoketest.lua uses --")
show("SaveLocations.LOCAL_STORAGE",    function() return SaveLocations.LOCAL_STORAGE end)
show("SaveTypes.SINGLE_PLAYER",        function() return SaveTypes.SINGLE_PLAYER end)
show("SaveDirectories.DEFAULT",        function() return SaveDirectories.DEFAULT end)
show("ServerType.SERVER_TYPE_NONE",    function() return ServerType.SERVER_TYPE_NONE end)

print("-- iterate them directly (not via _ENV) --")
for _, pair in ipairs({
    {"SaveLocations", SaveLocations}, {"SaveTypes", SaveTypes},
    {"SaveDirectories", SaveDirectories}, {"ServerType", ServerType}}) do
  local name, t = pair[1], pair[2]
  local found, n = {}, 0
  if type(t) == "table" then
    for k, v in pairs(t) do n = n + 1; found[#found+1] = tostring(k).."="..tostring(v) end
  end
  print(string.format("  %-18s n=%d  %s", name, n,
    n > 0 and table.concat(found, ", ") or "(not a table / opaque)"))
end

print("-- what does a save list query return here, in the front end? --")
show("UI.QuerySaveGameList()", function() return UI.QuerySaveGameList() end)
"""


def main() -> None:
    p = Probe(app="civsim-t217-enums")
    try:
        states = p.handshake()
        targets = sys.argv[1:] or ["Main State"]
        for target in targets:
            if target not in states:
                print(f"!! state {target!r} absent at this phase\n")
                continue
            print(f"=== [{target}] idx={states[target]} ===")
            out, raw = p.exec_lua(states[target], LUA, wait=6.0)
            print(out or f"(no sentinel)\nRAW:\n{raw[:1500]}")
            print()
    finally:
        p.close()


if __name__ == "__main__":
    main()
