"""T217: does Network.LoadGame work from the FRONT END?

The six shapes in load-path-linux.md were all tried from `InGame`. Firaxis'
own shipped automation (automation_dailysmoketest.lua:241-285) opens with:

    if (not UI.IsInFrontEnd()) then Events.ExitToMainMenu(); return; end

i.e. the load is front-end-only by design. This probe measures that, it does
not assume it.
"""
from __future__ import annotations

import sys

sys.path.insert(0, "/home/matt/CivSolver/specs/002-civ-playing-harness/spikes/r5-raw")
from nexus_probe import Probe  # noqa: E402

LUA_FRONTEND_CHECK = r"""
local function try(label, fn)
  local ok, v = pcall(fn)
  print(string.format("  %-5s %-40s = %s", ok and "OK" or "ERR", label, tostring(v)))
end
try("UI.IsInFrontEnd()", function() return UI.IsInFrontEnd() end)
try("Network.LoadGame type", function() return type(Network.LoadGame) end)
try("Network.SaveGame type", function() return type(Network.SaveGame) end)
try("UI.QuerySaveGameList type", function() return type(UI.QuerySaveGameList) end)
for _, name in ipairs({"SaveLocations","SaveTypes","SaveDirectories","SaveFileTypes","SaveGameTypes","ServerType"}) do
  local found, n = {}, 0
  local ok = pcall(function()
    local t = _ENV and _ENV[name]
    if t == nil then return end
    for k, v in pairs(t) do n = n + 1; found[#found+1] = tostring(k).."="..tostring(v) end
  end)
  print(string.format("  enum %-16s ok=%s n=%d  %s", name, tostring(ok), n,
    n > 0 and table.concat(found, ", ") or "(absent/not enumerable)"))
end
"""


def main() -> None:
    p = Probe(app="civsim-t217-frontend")
    try:
        states = p.handshake()
        print(f"=== {len(states)} Lua states at this phase ===")
        for name, idx in sorted(states.items(), key=lambda kv: kv[1]):
            print(f"  {idx:4d}  {name}")
        print()
        for target in sys.argv[1:] or ["Main State"]:
            if target not in states:
                print(f"!! state {target!r} not present")
                continue
            print(f"=== [{target}] front-end check ===")
            out, raw = p.exec_lua(states[target], LUA_FRONTEND_CHECK, wait=5.0)
            print(out or f"(no sentinel output)\nRAW:\n{raw[:2000]}")
            print()
    finally:
        p.close()


if __name__ == "__main__":
    main()
