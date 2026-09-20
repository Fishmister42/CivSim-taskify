-- lua/ingame/great_people.lua
-- Context: InGame (write/act). Verification reads back through lua/gamecore/great_people.lua
-- rather than this file.
-- Backs declaration_id: great_people.recruit (catalogs/actions/great_people.yaml),
-- capability_id: great_people.orders.
--
-- UNVERIFIED (whole file): the recruit/patronize Lua call is not confidently known; assumed by
-- analogy with Game.GetGreatPeople()'s read side in lua/gamecore/great_people.lua.
--
-- Parity note: only an individual already reported by CivSim_GreatPeople.state()'s
-- recruitable_individuals for the local player may be recruited — exactly the choice the standard
-- Great People screen would currently offer.

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

local function CivSim_GreatPeople_Recruit(individualId)
    local localPlayer = Game.GetLocalPlayer()
    local ok, result = pcall(function()
        return Game.GetGreatPeople():Recruit(localPlayer, individualId) -- UNVERIFIED
    end)
    return { ok = (ok and result ~= false), individual_id = individualId }
end

CivSim_GreatPeopleOrders = {
    recruit = CivSim_GreatPeople_Recruit,
}

-- Example dispatch (performed by the Nexus dispatcher, not by this file):
--   print(CivSim_JsonEncode(CivSim_GreatPeopleOrders.recruit(14)))
