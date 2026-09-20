"""T077 / R5 save-path spike: enumerate callable save paths in both tuner contexts.

Writes raw enumeration output to files (bulk evidence) and prints only a short
summary, so the coordinating session's context stays clean.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from nexus_probe import Probe

OUT = Path(__file__).parent / "t077_raw"
OUT.mkdir(exist_ok=True)

# Output lines arrive prefixed "O\0<StateName>: "
PREFIX = re.compile(r"^O\x00[A-Za-z_0-9]+:\s?")


def clean(text: str) -> str:
    lines = []
    for ln in text.splitlines():
        ln = PREFIX.sub("", ln)
        if ln.strip():
            lines.append(ln.rstrip())
    return "\n".join(lines)


DUMP_LUA = r"""
local function dump(name, t)
  if t == nil then print("=== " .. name .. " = nil ===") return end
  if type(t) ~= "table" and type(t) ~= "userdata" then
    print("=== " .. name .. " is " .. type(t) .. " ===") return
  end
  local keys = {}
  local ok = pcall(function()
    for k, v in pairs(t) do keys[#keys+1] = tostring(k) .. "  :  " .. type(v) end
  end)
  table.sort(keys)
  print("=== " .. name .. " (" .. #keys .. " entries, pairs_ok=" .. tostring(ok) .. ") ===")
  for _, k in ipairs(keys) do print("  " .. k) end
end
dump("Network", Network)
dump("UI", UI)
dump("Game", Game)
"""

GREP_LUA = r"""
local pat = {"save", "load", "quick", "autosave", "serial"}
local function matches(s)
  local l = string.lower(s)
  for _, p in ipairs(pat) do if string.find(l, p, 1, true) then return true end end
  return false
end
print("=== global namespaces containing save/load-ish members ===")
local seen = {}
for gk, gv in pairs(_G) do
  if type(gv) == "table" and not seen[gk] then
    seen[gk] = true
    local hits = {}
    local ok = pcall(function()
      for k, v in pairs(gv) do
        if matches(tostring(k)) then hits[#hits+1] = "  " .. gk .. "." .. tostring(k) .. "  :  " .. type(v) end
      end
    end)
    if ok and #hits > 0 then
      table.sort(hits)
      for _, h in ipairs(hits) do print(h) end
    end
  end
end
print("=== top-level _G save/load-ish names ===")
for k, v in pairs(_G) do
  if matches(tostring(k)) then print("  _G." .. tostring(k) .. "  :  " .. type(v)) end
end
"""

p = Probe()
states = p.handshake()
(OUT / "00_states.txt").write_text(
    "\n".join(f"{i:>4}  {n}" for n, i in sorted(states.items(), key=lambda kv: kv[1]))
)

summary: list[str] = [f"lua states: {len(states)}  GameCore_Tuner={states.get('GameCore_Tuner')}  InGame={states.get('InGame')}"]

for label in ("GameCore_Tuner", "InGame"):
    idx = states.get(label)
    if idx is None:
        summary.append(f"{label}: ABSENT")
        continue
    for tag, lua in (("namespaces", DUMP_LUA), ("savegrep", GREP_LUA)):
        res, raw = p.exec_lua(idx, lua, wait=12.0)
        body = clean(res if res else raw)
        f = OUT / f"{label}__{tag}.txt"
        f.write_text(body)
        counts = dict(re.findall(r"=== (\w+) \((\d+) entries", body))
        summary.append(f"{label}/{tag}: {len(body.splitlines())} lines -> {f.name}  {counts}")

p.close()
print("\n".join(summary))
