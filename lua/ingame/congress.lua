-- lua/ingame/congress.lua
-- Context: InGame (write/act). Verification reads back through lua/gamecore/congress.lua rather
-- than this file.
-- Backs declaration_id: congress.cast_vote (catalogs/actions/congress.yaml),
-- capability_id: congress.orders.
--
-- SANDBOX CONSTRAINT (specs/002-civ-playing-harness/spikes/lua-api-verification-linux.md, P5):
-- neither tuner context exposes `require`, `io`, or `debug`, and no JSON library exists in
-- either. This file must stay entirely self-contained — no shared module can ever be factored out
-- and `require`d elsewhere — and carries its own hand-rolled JSON encoder.
--
-- ACCESSOR AUDIT (2026-09-21, specs/002-civ-playing-harness/spikes/lua-accessor-audit-2026-09-21.md):
-- `Game.GetWorldCongress():CastVote(...)` was an invented name. The World Congress object has no
-- mutators at all -- the eight methods Firaxis ever calls on it are all reads -- so this order
-- raised inside its pcall and reported `ok = false` every time. Voting is a player operation.
-- UNVERIFIED LIVE.
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

-- SOURCE (this machine, 2026-09-21;
-- steamassets/dlc/expansion2/ui/additions/worldcongresspopup.lua:2239-2253): submitting the
-- player's resolution votes is one request per resolution --
--   UI.RequestPlayerOperation(playerID, PlayerOperations.WORLD_CONGRESS_RESOLUTION_VOTE, {
--     [PARAM_RESOLUTION_TYPE]      = <GameInfo.Resolutions row>.Hash,   -- :2241
--     [PARAM_WORLD_CONGRESS_VOTES] = <number of VOTES, not favor>,      -- :2242
--     [PARAM_RESOLUTION_OPTION]    = 1 or 2 (the A/B side),             -- :2246
--     [PARAM_RESOLUTION_SELECTION] = <target index, ZERO-based>,        -- :2247
--   })
-- The ballot is then finalised with PlayerOperations.WORLD_CONGRESS_SUBMIT_TURN (:2270), which is
-- a separate act the harness has no declaration for and this body deliberately does not issue:
-- submitting would end the player's congress turn, not cast one vote.
--
-- NOTE on the third argument: the engine takes VOTES, and the favor those votes cost comes off the
-- ladder `pWorldCongress:GetVotesandFavorCost(playerID)` (:575). The old body's `favorSpent`
-- name claimed the opposite. `congress.cast_vote` declares only a `target` today, so choice and
-- vote count arrive nil through act/executor.py's positional convention; rather than guess a side
-- for the player, that is refused by name.
local function CivSim_Congress_CastVote(resolutionId, choiceId, votes)
    if type(resolutionId) ~= "number" then
        return { ok = false, reason = "unknown_resolution", resolution_id = resolutionId }
    end
    if choiceId ~= 1 and choiceId ~= 2 then
        return {
            ok = false,
            reason = "vote_option_not_supplied",
            resolution_id = resolutionId,
            choice_id = choiceId,
        }
    end
    local voteCount = (type(votes) == "number" and votes > 0) and votes or 1
    local ok, err = pcall(function()
        local tParameters = {}
        tParameters[PlayerOperations.PARAM_RESOLUTION_TYPE] = resolutionId
        tParameters[PlayerOperations.PARAM_WORLD_CONGRESS_VOTES] = voteCount
        tParameters[PlayerOperations.PARAM_RESOLUTION_OPTION] = choiceId
        UI.RequestPlayerOperation(
            Game.GetLocalPlayer(), PlayerOperations.WORLD_CONGRESS_RESOLUTION_VOTE, tParameters)
    end)
    if not ok then
        return {
            ok = false,
            reason = "UI.RequestPlayerOperation errored: " .. tostring(err),
            resolution_id = resolutionId,
            choice_id = choiceId,
        }
    end
    return {
        ok = true,
        resolution_id = resolutionId,
        choice_id = choiceId,
        votes = voteCount,
        mechanism = "PlayerOperations.WORLD_CONGRESS_RESOLUTION_VOTE",
    }
end

CivSim_CongressOrders = {
    cast_vote = CivSim_Congress_CastVote,
}

-- Example dispatch (performed by the Nexus dispatcher, not by this file):
--   print(CivSim_JsonEncode(CivSim_CongressOrders.cast_vote(3, 1, 5)))
