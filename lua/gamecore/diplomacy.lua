-- lua/gamecore/diplomacy.lua
-- Context: GameCore_Tuner (read-only, best-effort).
-- Backs declaration_id: diplomacy.state (catalogs/observations/diplomacy.yaml),
-- capability_id: diplomacy.read.
--
-- CAUTION (research.md R3): diplomacy is named as a "UI-bound surface" that may only be fully
-- reachable from the InGame context, not GameCore_Tuner. The fields below are the subset believed
-- reachable as plain game-state reads (diplomatic state/visibility, which the standard diplomacy
-- overview shows regardless of which screen is open); anything that turns out to require the
-- InGame UI state at runtime must be re-declared against lua/ingame/diplomacy.lua's read side
-- instead of this file. This tension is flagged, not resolved, here. Not independently tested by
-- the live-client sweep (specs/002-civ-playing-harness/spikes/lua-api-verification-linux.md).
--
-- SANDBOX CONSTRAINT (spike P5): neither tuner context exposes `require`, `io`, or `debug`, and
-- no JSON library exists in either. This file must stay entirely self-contained — no shared
-- module can ever be factored out and `require`d elsewhere — and carries its own hand-rolled JSON
-- encoder.
--
-- Parity note: only diplomatic *state* visible to the human player (met/not met, current
-- diplomatic state, public agreements) is reported — never hidden AI intent, undisclosed grievances
-- weighting, or an opponent's private diplomatic calculations.

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

-- STATED SCOPE, not silent (2026-09-22, the 882758e/0989e3b convention). `relations` has always
-- enumerated only alive players for which `Player:IsMajor()` answers true -- a deliberate filter,
-- not an accessor artefact: `nm -DC` on the shipped `libGameCore_{Base,XP1,XP2}.so` shows
-- `GameCore::Lua::IPlayer::lIsMajor` (this body's own GameCore_Tuner namespace) exported
-- identically to `GameCore::Cache::Lua::IPlayer::lIsMajor` (InGame), so `IsMajor` is callable
-- exactly where this body runs; there is no context fault behind the filter. A minor civilization
-- (city-state) is therefore NEVER a `relations` entry, met or not, by design -- and prior to this
-- change that omission was indistinguishable from "not yet met" for a player_id the consumer had
-- no other way to classify (specs/002-civ-playing-harness/spikes/gameplay-2026-09-22/
-- BREADTH-MAP.md's "Kong" finding). `relations_scope` below makes the enumerated set a fact in
-- the observation itself rather than something a reader had to infer from an empty list.
local CIVSIM_DIPLOMACY_RELATIONS_SCOPE = "alive_major_civilizations"

local function CivSim_Diplomacy_GetState()
    -- The check every shipped screen makes first (the 882758e guard): never touch a player object
    -- with no local player, and say so rather than answering with a bare empty list a reader
    -- could misread as "no relations exist".
    local localPlayer = Game.GetLocalPlayer()
    if localPlayer == nil or localPlayer == -1 then
        return {
            relations = {},
            relations_scope = CIVSIM_DIPLOMACY_RELATIONS_SCOPE,
            relations_reason = "no_local_player",
        }
    end
    local player = Players[localPlayer]
    -- MEASURED (2026-09-20, Linux 1.0.12.9, live run): `Player:GetDiplomaticAI()` does NOT exist
    -- in GameCore_Tuner ("function expected instead of nil"), and the unguarded call killed the
    -- whole diplomacy read -- and with it turn 1 of the first real run. It is the UI-side
    -- accessor (InGame). Acquired under pcall; when absent, `diplomatic_state` is reported as
    -- null (unobservable from this context) rather than the read failing.
    --
    -- STATIC CONFIRMATION (2026-09-22, `nm -DC` on the shipped `libGameCore_{Base,XP1,XP2}.so`,
    -- the 0989e3b technique, `GameCore::Lua::` == GameCore_Tuner / `GameCore::Cache::Lua::` ==
    -- InGame per T213): `GameCore::Cache::Lua::IPlayer::lGetDiplomaticAI` is exported; there is no
    -- `GameCore::Lua::IPlayer::lGetDiplomaticAI` in any of the three libraries -- the 2026-09-20
    -- live measurement above is not a fluke of that build, it is this context's whole namespace.
    -- The "GameCore-side fallback" two lines below is *also* unreachable here by the same static
    -- check: `GetDiplomaticStateIndex` exists ONLY as `GameCore::Cache::Lua::IAiDiplomacy::
    -- lGetDiplomaticStateIndex` -- there is no `GameCore::Lua::` binding for it on ANY interface,
    -- so `player:GetDiplomacy():GetDiplomaticStateIndex(...)` fails exactly like `diploAI` does,
    -- for every relation, always. `diplomatic_state` is therefore Cache/InGame-only end to end in
    -- this GameCore_Tuner-context body -- CONFIRMED empirically too: every `has_met: true`
    -- relation ever recorded in the store (834 of 834 as of this pass, spanning catalog versions
    -- 2026.09.2 through 2026.09.14) carries no non-null `diplomatic_state`. This is the SAME
    -- defect class as 0989e3b's `CanProgress`, already tracked for the action-predicate side at
    -- `src/civsim_harness/act/predicates.py`'s `KNOWN_SCHEMA_BODY_DISAGREEMENTS` /
    -- `KNOWN_PHANTOM_PREDICATE_FIELDS` (tasks T307/T308) -- reported here, not fixed here: moving
    -- this body's declared context to InGame is exactly the kind of new-engine-call-reachability
    -- change specs/002-civ-playing-harness/spikes/client-segfault-2026-09-21.md requires a
    -- crash-watched live block to land, and none has run against this change.
    local diploAI = nil
    local okAI, ai = pcall(function() return player:GetDiplomaticAI() end)
    if okAI then diploAI = ai end

    -- VERIFIED (P4 spot-check): PlayerManager.GetAlive() exists, replacing the unconfirmed
    -- GetAliveMajors() guess. UNVERIFIED LIVE: whether it returns majors only or all alive
    -- players; Player:IsMajor() is applied defensively (kept if unavailable) as in
    -- lua/gamecore/cities.lua. STATIC CONFIRMATION (2026-09-22, `nm -DC`): both
    -- `GameCore::Lua::IPlayer::lIsMajor` (this context) and `GameCore::Cache::Lua::IPlayer::
    -- lIsMajor` are exported identically in all three shipped libraries, and
    -- `GameCore::Lua::IPlayerManager::lGetAlive` likewise -- this is an EXPLICIT, callable-here
    -- filter, not a context artefact: `relations` enumerating only major civilizations
    -- (`CIVSIM_DIPLOMACY_RELATIONS_SCOPE` above) is this body's deliberate scope, not a silent
    -- side effect of an accessor that only half-works. Empirically, every player_id this body has
    -- ever recorded across the full store is one of exactly 5 major-civilization ids; no
    -- city-state id has ever appeared, in either polarity.
    local relations = {}
    for _, otherPlayer in ipairs(PlayerManager.GetAlive()) do
        if not otherPlayer.IsMajor or otherPlayer:IsMajor() then -- UNVERIFIED LIVE: Player:IsMajor() semantics
            local otherID = otherPlayer:GetID()
            if otherID ~= localPlayer then
                local hasMet = false
                local ok1, met = pcall(function() return player:GetDiplomacy():HasMet(otherID) end) -- UNVERIFIED
                if ok1 then hasMet = met end
                if hasMet then
                    local stateName = nil
                    local ok2, stateIndex = pcall(function()
                        if diploAI ~= nil then
                            return diploAI:GetDiplomaticStateIndex(otherID) -- UNVERIFIED
                        end
                        -- GameCore-side fallback: the PlayerDiplomacy object. UNVERIFIED.
                        return player:GetDiplomacy():GetDiplomaticStateIndex(otherID)
                    end)
                    if ok2 and stateIndex then
                        local okName, name = pcall(function()
                            return GameInfo.DiplomaticStates[stateIndex].StateType -- UNVERIFIED
                        end)
                        if okName then stateName = name end
                    end
                    local hasDelegation = false
                    local ok3, delegation = pcall(function()
                        return player:GetDiplomacy():HasDelegationAt(otherID) -- UNVERIFIED
                    end)
                    if ok3 then hasDelegation = delegation end
                    relations[#relations + 1] = {
                        player_id = otherID,
                        has_met = true,
                        diplomatic_state = stateName,
                        has_delegation = hasDelegation,
                        civilization = PlayerConfigurations[otherID]:GetCivilizationTypeName(), -- UNVERIFIED accessor name
                    }
                else
                    relations[#relations + 1] = { player_id = otherID, has_met = false }
                end
            end
        end
    end

    -- Always present, never a `_reason` (the filter itself never fails to answer -- see the
    -- comment above `CIVSIM_DIPLOMACY_RELATIONS_SCOPE`): the exact set `relations` enumerates, so
    -- a player_id missing from it is legible as "out of scope" rather than an unanswered question.
    return { relations = relations, relations_scope = CIVSIM_DIPLOMACY_RELATIONS_SCOPE }
end

CivSim_Diplomacy = {
    state = CivSim_Diplomacy_GetState,
}

-- Example dispatch (performed by the Nexus dispatcher, not by this file):
--   print(CivSim_JsonEncode(CivSim_Diplomacy.state()))
