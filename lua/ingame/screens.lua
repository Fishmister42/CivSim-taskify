-- lua/ingame/screens.lua
-- Context: InGame. Read (screen-identity probe) and write (prompt response) both live here
-- because both are UI-bound in a way only the InGame context can reach (research R3, R13).
-- Backs declaration_id: game.screen_state (catalogs/observations/game.yaml), capability_id:
-- screens.probe; and every declaration_id in catalogs/actions/prompts.yaml, capability_id:
-- prompts.orders.
--
-- SANDBOX CONSTRAINT (specs/002-civ-playing-harness/spikes/lua-api-verification-linux.md, P5):
-- neither tuner context exposes `require`, `io`, or `debug`, and no JSON library exists in
-- either. This file must stay entirely self-contained — no shared module can ever be factored out
-- and `require`d elsewhere — and carries its own hand-rolled JSON encoder.
--
-- CORRECTED against a live client (spike P2). The original draft here assumed a global
-- `UIManager`-style "what is topmost" query. That does not exist: `UIManager.GetScreen`,
-- `UIManager.GetTopmostScreen`, `UIManager.GetCurrentPopup`, `UIManager.ClosePopup`,
-- `UI.IsScreenOpen`, and `UI.GetScreenName` are all confirmed `nil` in InGame.
--
-- VERIFIED (P2): the real mechanism. Every Civ VI UI screen is its own separate Lua state (137
-- observed at turn 1; spikes/r5-raw/00_states.txt), each with its own private `ContextPtr`, and
-- that state's own `ContextPtr:IsHidden()` reports whether that one screen is currently showing.
-- Live-proven: 26 watched screens started `hidden=true`; sending one Escape to the client flipped
-- exactly `InGameTopOptionsMenu` to `hidden=false` and nothing else
-- (spikes/sweep-raw/screen_identity.md). So FR-010 and FR-049 ARE reachable through FireTuner —
-- there is no firetuner_gap to document for screen identity.
--
-- ARCHITECTURAL CONSEQUENCE — reported, not fixed here (this agent owns lua/ and these two
-- catalog files only, not the dispatcher). Because each screen is a genuinely separate Lua VM
-- state, code running in the InGame state cannot read another screen's ContextPtr by name — there
-- is no shared global namespace across states, the same isolation that makes `require` absent.
-- contracts/nexus-protocol.md's wire format (`CMD:<state_index>:<lua_code>`, resolved via the
-- `LSQ:` handshake) already supports addressing any enumerated Lua state, not just
-- `GameCore_Tuner`/`InGame` — the transport can do this. What is missing is on the
-- catalog/dispatcher side (outside this directory): today a declaration's `context` resolves to
-- exactly one of those two named contexts, and the client "refuses to execute an entry in the
-- wrong one". Answering game.screen_state for real means the dispatcher must additionally resolve
-- each name in CIVSIM_SCREEN_WATCHLIST below to its own state index (via `LSQ:`) and issue one
-- `CivSim_Screens.probe()` command per candidate screen, in watchlist order, folding the
-- per-screen `hidden` results into game.screen_state's aggregate shape — rather than assuming one
-- call into "InGame" is enough, which was this file's previous (wrong) assumption.
--
-- Three honest limits on what this answers even once the dispatcher does that (see spike P2):
-- 1. It answers "is screen X open", not "what is topmost" — Z-order is not exposed. Sufficient
--    for FR-049 (the unknown case is "the game is blocked and nothing known is open"), not for
--    true stacking order.
-- 2. One round-trip per screen state — 26 screens is 26 commands, since these are genuinely
--    separate Lua environments with no single call spanning all of them. CIVSIM_SCREEN_WATCHLIST
--    is ordered by likelihood so a per-step probe can check the most probable screens first
--    rather than always scanning all 26.
-- 3. A screen whose state is not instantiated until first use reads as absent from the `LSQ:`
--    enumeration (a dispatcher-level fact), not as `hidden=true` — a Lua probe can only run once
--    the dispatcher has found the state at all. All 26 watched states existed at turn 1; that
--    should not be assumed for rarely-opened screens across a whole game.
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
    "prompt.tech_civic_completed", "prompt.boost_unlocked",
}

-- T253 (MEASURED 2026-09-21, attempt 5 of the first model-driven runs): the civic "Code of Laws"
-- completed, the game queued `TechCivicCompletedPopup`, and because that state was on the
-- watchlist but mapped to no catalog id the run stalled `UnknownScreenEncountered` -- correctly,
-- per FR-049, and play stopped there. These two popups are pure acknowledgements: Firaxis's own
-- `techciviccompletedpopup.lua` / `boostunlockedpopup.lua` close through
-- `UIManager:DequeuePopup(ContextPtr)` from their Continue/close button and from their
-- Escape (and Return) key handler, and offer nothing else the harness exposes. The one extra
-- control on the civic popup ("Change Government") is deliberately NOT offered -- a strict
-- subset of what the human sees, never a superset (Principle I); governments and policies are
-- reachable through the policies.* actions instead. The single offered option is "continue".
local CIVSIM_ACKNOWLEDGE_ONLY_PROMPTS = {
    ["prompt.tech_civic_completed"] = "TechCivicCompletedPopup",
    ["prompt.boost_unlocked"] = "BoostUnlockedPopup",
}
local CIVSIM_ACKNOWLEDGE_OPTION = "continue"

local function CivSim_ScreenIsKnown(screenId)
    for _, known in ipairs(CIVSIM_KNOWN_SCREENS) do
        if known == screenId then return true end
    end
    return false
end

-- Real Civ VI Lua state names (spikes/r5-raw/00_states.txt), likelihood-ordered (most commonly
-- opened mid-turn first, menu/pause screens last) for the dispatcher to resolve via `LSQ:` and
-- probe one at a time — see the header's architectural note. This is data for that future
-- multi-state dispatch, not something this file loops over itself.
local CIVSIM_SCREEN_WATCHLIST = {
    "CityPanel", "ProductionPanel", "TechTree", "CivicsTree", "GovernmentScreen", "ReligionScreen",
    "DiplomacyActionView", "DiplomacyDealView", "DeclareWarPopup", "UnitPromotionPopup",
    "PantheonChooser", "GreatPeoplePopup", "WorldCongressPopup", "WorldCongressBetweenTurns",
    "WorldCongressIntro", "EventPopup", "EraCompletePopup", "NaturalWonderPopup", "LeaderScene",
    "TechCivicCompletedPopup", "BoostUnlockedPopup", "CivilopediaScreen", "InGamePopup",
    "InGameTopOptionsMenu", "PausePanel", "Options", "SaveGameMenu", "LoadGameMenu",
}

-- VERIFIED (P2, screen_identity.md) that each named state exists; UNVERIFIED that
-- ContextPtr:IsHidden()==false on that exact state precisely coincides with the catalog concept
-- named on the left, beyond the one live-flipped case (InGameTopOptionsMenu, confirmed). Entries
-- intentionally left out below (e.g. "strategic", most `prompt.*` ids) have no confirmed 1:1 state
-- and are not guessed here.
local CIVSIM_SCREEN_ID_BY_STATE = {
    city_screen = "CityPanel",
    congress = "WorldCongressPopup",
    diplomacy = "DiplomacyActionView",
    ["prompt.unit_promotion"] = "UnitPromotionPopup",
    ["prompt.pantheon_selection"] = "PantheonChooser",
    ["prompt.great_person_selection"] = "GreatPeoplePopup",
    ["prompt.declare_war_response"] = "DeclareWarPopup",
    ["prompt.era_transition"] = "EraCompletePopup",
    -- T253: the two acknowledge-only popups (see CIVSIM_ACKNOWLEDGE_ONLY_PROMPTS above). The state
    -- names are the ones on the watchlist, confirmed to exist at turn 1 (P2); that IsHidden()==false
    -- on `TechCivicCompletedPopup` coincides with the popup being up was observed live (attempt 5).
    ["prompt.tech_civic_completed"] = "TechCivicCompletedPopup",
    ["prompt.boost_unlocked"] = "BoostUnlockedPopup",
}

-- VERIFIED (P2): the confirmed screen-identity mechanism. This is written to be dispatched once
-- *per candidate screen state* (see header) — when the dispatcher targets a given screen's own
-- Lua state and calls this, it reports that screen's own hidden flag. The caller already knows
-- which screen it targeted (it chose the state index/name), so this function needs no argument
-- and does not itself decide which screen it is probing.
local function CivSim_Screens_ProbeOwnState()
    local ok, hidden = pcall(function() return ContextPtr:IsHidden() end)
    if not ok then
        -- ContextPtr missing/erroring in a state the dispatcher successfully targeted is distinct
        -- from that screen's state not existing at all (STATE_ABSENT, a fact the `LSQ:` handshake
        -- surfaces before this Lua ever runs — see header limitation 3).
        return { screen_probe_ok = false, hidden = nil }
    end
    return { screen_probe_ok = true, hidden = (hidden == true) }
end

-- MEASURED (2026-09-21, Linux 1.0.12.9, live, T213): the per-state dispatch the header describes
-- was never wired -- the production executor dispatches `screens.probe` ONCE, from `InGame`, and
-- validates the result against game.screen_state's aggregate schema, so the per-state shape above
-- failed every real run's first observation sweep. From `InGame`,
-- `ContextPtr:LookUpControl("/InGame/<StateName>")` resolves each watchlist screen's own context
-- (nil for a state that does not exist -- `CivilopediaScreen` is absent on this build, a bogus name
-- returns nil) and `:IsHidden()` is its open flag; at the plain world view every watchlist screen
-- answered hidden=true. That is the aggregate, built from `InGame` in one dispatch.
local function CivSim_Screens_State()
    local open = {}
    for _, name in ipairs(CIVSIM_SCREEN_WATCHLIST) do
        local okC, ctx = pcall(function() return ContextPtr:LookUpControl("/InGame/" .. name) end)
        if okC and ctx ~= nil then
            local okH, hidden = pcall(function() return ctx:IsHidden() end)
            if okH and hidden == false then open[#open + 1] = name end
        end
    end
    if #open == 0 then
        return {
            screen = "world_view", raw_screen_id = "InGame", recognized = true,
            has_blocking_prompt = false, prompt_options = {},
        }
    end
    -- A recognised prompt outranks anything else that is open (it is what blocks the player);
    -- otherwise the first open watchlist screen names the view.
    local raw, screen = nil, nil
    for _, name in ipairs(open) do
        for id, state in pairs(CIVSIM_SCREEN_ID_BY_STATE) do
            if state == name and string.sub(id, 1, 7) == "prompt." then raw, screen = name, id end
        end
    end
    if raw == nil then
        raw = open[1]
        for id, state in pairs(CIVSIM_SCREEN_ID_BY_STATE) do
            if state == raw then screen = id end
        end
    end
    if screen == nil then
        return {
            screen = "unknown", raw_screen_id = raw, recognized = false,
            has_blocking_prompt = false, prompt_options = {},
        }
    end
    -- T253: an acknowledge-only popup offers exactly one option. Every other prompt family still
    -- reports an empty list (per-prompt option enumeration is not yet implemented -- the header
    -- of catalogs/actions/prompts.yaml says so), which keeps those actions unavailable rather than
    -- guessed at.
    local options = {}
    if CIVSIM_ACKNOWLEDGE_ONLY_PROMPTS[screen] ~= nil then
        options = { CIVSIM_ACKNOWLEDGE_OPTION }
    end
    return {
        screen = screen, raw_screen_id = raw, recognized = true,
        has_blocking_prompt = (string.sub(screen, 1, 7) == "prompt."), prompt_options = options,
    }
end

-- Dispatched from InGame by the production executor (game.screen_state): the aggregate. The
-- per-state probe is kept as `probe_own_state` for a future per-state dispatcher.
local function CivSim_Screens_Probe()
    return CivSim_Screens_State()
end

-- Answer a currently open prompt with one of its offered options. UNVERIFIED: the sweep did not
-- test prompt response, so this remains an unconfirmed placeholder, not a corrected call —
-- `UI.RespondToPrompt` was not among the symbols probed and is not on either the confirmed or the
-- confirmed-wrong list. The dismiss/answer call is prompt-specific in the real client (e.g.
-- selecting a pantheon goes through lua/ingame/religion.lua's select_pantheon, not a generic
-- "answer prompt" call). Given P2's finding that each screen (very plausibly including each
-- prompt popup, e.g. PantheonChooser/DeclareWarPopup in CIVSIM_SCREEN_WATCHLIST above) is its own
-- isolated Lua state, a single generic call issued from InGame is now additionally suspect for
-- the same reason the old screen-identity query was wrong: the real answer call more plausibly
-- belongs inside that prompt's own state (e.g. driving one of its Controls' callbacks), not a
-- global `UI.*` function reachable from InGame. Not fixed here — untested, and reported above as
-- part of the same architectural gap. This generic entry point exists only for prompt types with
-- no dedicated orders file (e.g. era transition acknowledgement, city-state quest acceptance).
-- T253: dismiss an acknowledge-only popup the way its own Continue button does. From the InGame
-- state the popup's context is reachable as a Control (`ContextPtr:LookUpControl`, the same
-- resolution CivSim_Screens_State uses to see it), and `UIManager:DequeuePopup(<that context>)`
-- is the exact call the popup's own `Close()` makes (Firaxis `techciviccompletedpopup.lua` line
-- 307, `boostunlockedpopup.lua` line 315). UNVERIFIED LIVE that DequeuePopup accepts another
-- state's context from InGame and that `UIManager` is reachable here (its screen-query methods
-- are confirmed nil in InGame; the object itself was not probed) -- every step is pcall'd and
-- every outcome is returned, so a failure is recorded as itself, and the action's own
-- verification predicate (the popup is no longer the current screen) is what decides `applied`.
-- Never presses a key and never hides the control directly: SetHide would leave the popup
-- queued in UIManager, which is not what a human's click does.
local function CivSim_Screens_AcknowledgePopup(promptType, stateName, optionId)
    if optionId ~= CIVSIM_ACKNOWLEDGE_OPTION then
        return { ok = false, reason = "unknown_option", prompt = promptType, option = optionId }
    end
    local okC, ctx = pcall(function() return ContextPtr:LookUpControl("/InGame/" .. stateName) end)
    if not okC or ctx == nil then
        return { ok = false, reason = "popup_state_absent", prompt = promptType, option = optionId }
    end
    local okH, hidden = pcall(function() return ctx:IsHidden() end)
    if okH and hidden == true then
        return { ok = false, reason = "popup_not_open", prompt = promptType, option = optionId }
    end
    local okD, err = pcall(function() UIManager:DequeuePopup(ctx) end)
    local okA, hiddenAfter = pcall(function() return ctx:IsHidden() end)
    return {
        ok = okD, prompt = promptType, option = optionId, mechanism = "UIManager:DequeuePopup",
        hidden_after = (okA and hiddenAfter == true),
        error = (not okD) and tostring(err) or nil,
    }
end

local function CivSim_Screens_RespondToPrompt(promptType, optionId)
    if not CivSim_ScreenIsKnown(promptType) then
        return { ok = false, reason = "unknown_prompt" }
    end
    local acknowledgeState = CIVSIM_ACKNOWLEDGE_ONLY_PROMPTS[promptType]
    if acknowledgeState ~= nil then
        return CivSim_Screens_AcknowledgePopup(promptType, acknowledgeState, optionId)
    end
    local ok, result = pcall(function()
        return UI.RespondToPrompt(promptType, optionId) -- UNVERIFIED
    end)
    return { ok = (ok and result ~= false), prompt = promptType, option = optionId }
end

CivSim_Screens = {
    probe = CivSim_Screens_Probe,
    probe_own_state = CivSim_Screens_ProbeOwnState,
    respond = CivSim_Screens_RespondToPrompt,
}

-- Example dispatch (performed by the Nexus dispatcher, not by this file). Once per candidate
-- screen state in CIVSIM_SCREEN_WATCHLIST order — folding results into game.screen_state's
-- aggregate shape is the dispatcher's job, not this file's (see header):
--   print(CivSim_JsonEncode(CivSim_Screens.probe()))
--   print(CivSim_JsonEncode(CivSim_Screens.respond("prompt.city_state_quest", "accept")))
