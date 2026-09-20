-- lua/ingame/religion.lua
-- Context: InGame (write/act). Verification reads back through lua/gamecore/religion.lua rather
-- than this file.
-- Backs declaration_ids: religion.select_pantheon, religion.found_religion,
-- religion.select_belief (catalogs/actions/religion.yaml), capability_id: religion.orders.
--
-- UNVERIFIED (whole file): the set-side religion API is not confidently known; see the read-side
-- caveats in lua/gamecore/religion.lua. Choices offered are restricted to what
-- CivSim_Religion.state()'s available_beliefs already reported.
--
-- Parity note: a pantheon/religion/belief may only be chosen from the options the standard
-- pantheon/religion-founding screen would offer for the local player right now.

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

local function CivSim_Religion_SelectPantheon(beliefType)
    local localPlayer = Game.GetLocalPlayer()
    local religionMgr = Players[localPlayer]:GetReligion()
    local row = GameInfo.Beliefs[beliefType]
    if row == nil then
        return { ok = false, reason = "unknown_belief" }
    end
    local ok, result = pcall(function() return religionMgr:ChoosePantheon(row.Index) end) -- UNVERIFIED
    return { ok = (ok and result ~= false), belief = beliefType }
end

local function CivSim_Religion_FoundReligion(religionType, beliefType)
    local localPlayer = Game.GetLocalPlayer()
    local religionMgr = Players[localPlayer]:GetReligion()
    local religionRow = GameInfo.Religions[religionType]
    local beliefRow = GameInfo.Beliefs[beliefType]
    if religionRow == nil or beliefRow == nil then
        return { ok = false, reason = "unknown_religion_or_belief" }
    end
    local ok, result = pcall(function()
        return religionMgr:FoundReligion(religionRow.Index, beliefRow.Index) -- UNVERIFIED
    end)
    return { ok = (ok and result ~= false), religion = religionType, belief = beliefType }
end

local function CivSim_Religion_SelectBelief(beliefType)
    local localPlayer = Game.GetLocalPlayer()
    local religionMgr = Players[localPlayer]:GetReligion()
    local row = GameInfo.Beliefs[beliefType]
    if row == nil then
        return { ok = false, reason = "unknown_belief" }
    end
    local ok, result = pcall(function() return religionMgr:AddBelief(row.Index) end) -- UNVERIFIED
    return { ok = (ok and result ~= false), belief = beliefType }
end

CivSim_ReligionOrders = {
    select_pantheon = CivSim_Religion_SelectPantheon,
    found_religion = CivSim_Religion_FoundReligion,
    select_belief = CivSim_Religion_SelectBelief,
}

-- Example dispatch (performed by the Nexus dispatcher, not by this file):
--   print(CivSim_JsonEncode(CivSim_ReligionOrders.select_pantheon("BELIEF_GOD_OF_THE_SEA")))
