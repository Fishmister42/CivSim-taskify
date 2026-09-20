-- lua/ingame/screens.lua
-- Context: InGame. Read (screen-identity probe) and write (prompt response) both live here
-- because both are UI-bound in a way only the InGame context can reach (research R3, R13).
-- Backs declaration_id: game.screen_state (catalogs/observations/game.yaml), capability_id:
-- screens.probe; and every declaration_id in catalogs/actions/prompts.yaml, capability_id:
-- prompts.orders.
--
-- HIGHEST UNCERTAINTY FILE IN THIS CATALOG. Research R13 calls for "which screen is currently up"
-- as a declared observation, but does not (and could not, without a live client) specify the
-- concrete Lua mechanism. Civ VI's UI screens are ordinarily owned by their own Lua files via a
-- private `ContextPtr`; whether the InGame tuner context can enumerate "which UI context is
-- currently topmost" at all, and by what call, is not confirmed. Everything below is a best-effort
-- skeleton against the most plausible API shape (a global UI-manager-style query), written so the
-- JSON contract it produces is stable even though its internals must be validated against a live
-- client before first use — exactly the kind of gap the R13 implementer needs flagged, not hidden
-- behind a plausible-looking call.
--
-- Parity note: a screen identifier and its offered options are things a human player already sees
-- by looking at their own screen; nothing here reads hidden state to determine "what happens if I
-- pick option X" beyond what that screen itself displays.

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

-- Known screen/prompt identifiers this catalog declares handling for. Anything the probe reports
-- outside this set is, by design (FR-049), an "unknown_screen" the harness must stall on rather
-- than guess about.
local CIVSIM_KNOWN_SCREENS = {
    "world", "strategic", "city_screen", "diplomacy", "congress",
    "prompt.unit_promotion", "prompt.pantheon_selection", "prompt.religion_selection",
    "prompt.great_person_selection", "prompt.diplomatic_approach", "prompt.declare_war_response",
    "prompt.city_state_quest", "prompt.congress_vote", "prompt.era_transition",
}

local function CivSim_ScreenIsKnown(screenId)
    for _, known in ipairs(CIVSIM_KNOWN_SCREENS) do
        if known == screenId then return true end
    end
    return false
end

-- UNVERIFIED: the actual "what is currently on top" query. `UI.GetTopmostContext` (or similar) is
-- assumed as a placeholder; the real mechanism may require polling `ContextPtr` visibility from
-- each known screen's own Lua file via a LuaEvents broadcast instead of a single global query.
local function CivSim_Screens_Probe()
    local screenId = "world"
    local isBlocking = false
    local promptOptions = {}

    local ok, topmost = pcall(function() return UI.GetTopmostContext() end) -- UNVERIFIED
    if ok and topmost ~= nil then
        screenId = topmost
    end

    local recognized = CivSim_ScreenIsKnown(screenId)
    if not recognized then
        return {
            screen = "unknown",
            raw_screen_id = screenId,
            recognized = false,
            has_blocking_prompt = false,
        }
    end

    local isPrompt = (screenId:sub(1, 7) == "prompt.")
    if isPrompt then
        isBlocking = true
        -- UNVERIFIED: per-prompt option enumeration. Each known prompt type would need its own
        -- accessor (e.g. available promotions, available beliefs) rather than one generic call;
        -- this returns an empty list as a structurally honest placeholder rather than a guess.
        promptOptions = {}
    end

    return {
        screen = screenId,
        raw_screen_id = screenId,
        recognized = true,
        has_blocking_prompt = isBlocking,
        prompt_options = promptOptions,
    }
end

-- Answer a currently open prompt with one of its offered options. UNVERIFIED: the actual
-- dismiss/answer call is prompt-specific in the real client (e.g. selecting a pantheon goes
-- through lua/ingame/religion.lua's select_pantheon, not a generic "answer prompt" call). This
-- generic entry point exists only for prompt types with no dedicated orders file (e.g. era
-- transition acknowledgement, city-state quest acceptance).
local function CivSim_Screens_RespondToPrompt(promptType, optionId)
    if not CivSim_ScreenIsKnown(promptType) then
        return { ok = false, reason = "unknown_prompt" }
    end
    local ok, result = pcall(function()
        return UI.RespondToPrompt(promptType, optionId) -- UNVERIFIED
    end)
    return { ok = (ok and result ~= false), prompt = promptType, option = optionId }
end

CivSim_Screens = {
    probe = CivSim_Screens_Probe,
    respond = CivSim_Screens_RespondToPrompt,
}

-- Example dispatch (performed by the Nexus dispatcher, not by this file):
--   print(CivSim_JsonEncode(CivSim_Screens.probe()))
--   print(CivSim_JsonEncode(CivSim_Screens.respond("prompt.city_state_quest", "accept")))
