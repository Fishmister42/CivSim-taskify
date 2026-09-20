-- lua/gamecore/research.lua
-- Context: GameCore_Tuner (read-only).
-- Backs declaration_id: research.state (catalogs/observations/research.yaml), capability_id: research.read.
--
-- Parity note: reports only the local player's own tech tree progress and choices, matching the
-- standard Research/Civics tree screen. No opponent research or civic progress — FR-019
-- explicitly names "other civilizations' undisclosed research or civic progress" as forbidden.

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

local function CivSim_Research_GetState()
    local localPlayer = Game.GetLocalPlayer()
    local player = Players[localPlayer]
    local techs = player:GetTechs()   -- Player:GetTechs()
    local culture = player:GetCulture() -- Player:GetCulture()

    local researchableTechs = {}
    for row in GameInfo.Technologies() do -- UNVERIFIED: GameInfo table iteration idiom
        local techType = row.Index -- UNVERIFIED: index vs hash usage
        if techs.CanResearch and techs:CanResearch(techType) then -- UNVERIFIED: CanResearch accessor
            researchableTechs[#researchableTechs + 1] = row.TechnologyType
        end
    end

    local researchableCivics = {}
    for row in GameInfo.Civics() do -- UNVERIFIED
        local civicType = row.Index
        if culture.CanProgress and culture:CanProgress(civicType) then -- UNVERIFIED
            researchableCivics[#researchableCivics + 1] = row.CivicType
        end
    end

    local currentTechType = nil
    local currentTechHash = techs.GetResearchingTech and techs:GetResearchingTech() or -1 -- UNVERIFIED
    if currentTechHash and currentTechHash ~= -1 then
        currentTechType = GameInfo.Technologies[currentTechHash].TechnologyType -- UNVERIFIED
    end

    local currentCivicType = nil
    local currentCivicHash = culture.GetProgressingCivic and culture:GetProgressingCivic() or -1 -- UNVERIFIED
    if currentCivicHash and currentCivicHash ~= -1 then
        currentCivicType = GameInfo.Civics[currentCivicHash].CivicType -- UNVERIFIED
    end

    return {
        current_research = currentTechType,
        current_civic = currentCivicType,
        researchable_techs = researchableTechs,
        researchable_civics = researchableCivics,
    }
end

CivSim_Research = {
    state = CivSim_Research_GetState,
}

-- Example dispatch (performed by the Nexus dispatcher, not by this file):
--   print(CivSim_JsonEncode(CivSim_Research.state()))
