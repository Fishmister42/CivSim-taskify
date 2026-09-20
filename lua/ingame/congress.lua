-- lua/ingame/congress.lua
-- Context: InGame (write/act). Verification reads back through lua/gamecore/congress.lua rather
-- than this file.
-- Backs declaration_id: congress.cast_vote (catalogs/actions/congress.yaml),
-- capability_id: congress.orders.
--
-- UNVERIFIED (whole file): see lua/gamecore/congress.lua's header — the whole World Congress Lua
-- surface, read and write, is unconfirmed pending a live-client spike.
--
-- Parity note: only a resolution and choice already reported active by CivSim_Congress.state()
-- may be voted on, spending no more of the local player's own diplomatic favor than the standard
-- Congress screen would allow.

local function CivSim_JsonEncode(value)
    local t = type(value)
    if value == nil then
        return "null"
    elseif t == "boolean" then
        return value and "true" or "false"
    elseif t == "number" then
        if value ~= value then return "null" end
        return tostring(value)
    elseif t == "string" then
        local escaped = value:gsub('[%c"\\]', function(c)
            if c == '"' then return '\\"'
            elseif c == '\\' then return '\\\\'
            elseif c == '\n' then return '\\n'
            elseif c == '\r' then return '\\r'
            elseif c == '\t' then return '\\t'
            else return string.format('\\u%04x', string.byte(c)) end
        end)
        return '"' .. escaped .. '"'
    elseif t == "table" then
        local n = 0
        for _ in pairs(value) do n = n + 1 end
        if n == 0 then return "[]" end
        local isArray = true
        for i = 1, n do if value[i] == nil then isArray = false break end end
        if isArray then
            local parts = {}
            for i = 1, n do parts[i] = CivSim_JsonEncode(value[i]) end
            return "[" .. table.concat(parts, ",") .. "]"
        else
            local parts = {}
            for k, v in pairs(value) do
                parts[#parts + 1] = CivSim_JsonEncode(tostring(k)) .. ":" .. CivSim_JsonEncode(v)
            end
            return "{" .. table.concat(parts, ",") .. "}"
        end
    else
        return "null"
    end
end

local function CivSim_Congress_CastVote(resolutionId, choiceId, favorSpent)
    local localPlayer = Game.GetLocalPlayer()
    local ok, result = pcall(function()
        return Game.GetCongress():CastVote(localPlayer, resolutionId, choiceId, favorSpent) -- UNVERIFIED
    end)
    return {
        ok = (ok and result ~= false),
        resolution_id = resolutionId,
        choice_id = choiceId,
        favor_spent = favorSpent,
    }
end

CivSim_CongressOrders = {
    cast_vote = CivSim_Congress_CastVote,
}

-- Example dispatch (performed by the Nexus dispatcher, not by this file):
--   print(CivSim_JsonEncode(CivSim_CongressOrders.cast_vote(3, 1, 5)))
