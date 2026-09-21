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
    "prompt.tech_civic_completed", "prompt.boost_unlocked", "prompt.great_work_created",
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
--
-- MEASURED 2026-09-21 (gameplay day, block 4, game turn 30): T253's acknowledge was wrong in a way
-- only live play showed. `UIManager:DequeuePopup(ctx)` hides the popup's context but never runs the
-- popup's OWN Continue handler, and both tech/civic and boost popups keep a private queue in their
-- own Lua state. Firaxis's `techciviccompletedpopup.lua` Close button calls `OnClose()` ->
-- `TryClose()` (line 319-345), which clears `m_kCurrentData`, shows the next queued entry if there
-- is one, and only then calls `Close()` (line 303-309, the DequeuePopup). Dequeuing without that
-- leaves `m_kCurrentData` set, so the NEXT completed civic re-queues the context, `OnShow()` ->
-- `RealizeNextPopup()` (line 266) sees `m_kCurrentData ~= nil` and re-displays the STALE card:
-- observed live as the turn-20 "Code of Laws" card reappearing at turn 30 under a tracker saying
-- Craftsmanship had completed. That is a Principle I problem, not just a nuisance -- the agent was
-- shown a card that did not describe what had just happened.
--
-- So the acknowledge now drives the popup's own close control first -- the literal button a human
-- clicks, whose callback runs inside the popup's own state and therefore consumes that state's
-- queue -- and falls back to the per-popup close primitive that IS reachable from InGame only when
-- the control cannot be driven, recording which mechanism ran and why. Per-popup close paths, read
-- from the shipped UI:
--   * TechCivicCompletedPopup: `Controls.CloseButton` -> `OnClose` -> `TryClose`
--     (techciviccompletedpopup.lua:464, :346, :319; the button is `CloseButton` in
--     techciviccompletedpopup.xml:10). `TryClose` is a global in that popup's own isolated Lua
--     state, so InGame cannot call it by name; DequeuePopup is the documented fallback.
--   * BoostUnlockedPopup: `Controls.ContinueButton` -> `OnClose`, which is
--     `UIManager:DequeuePopup(ContextPtr)` followed by `ShowNextQueuedPopup()`
--     (boostunlockedpopup.lua:397, :313-318; button `ContinueButton` in boostunlockedpopup.xml:38).
--     The fallback therefore matches the first half of its own handler but skips the requeue.
--   * GreatWorkShowcase: `Controls.ModalScreenClose` -> `OnHideScreen` -> `HideScreen()`, which is
--     exactly `ContextPtr:SetHide(true)` (greatworkshowcase.lua:240-244, :253-255, :399). This one
--     is NOT a UIManager popup at all -- it is never queued -- so DequeuePopup would be the wrong
--     call and `SetHide(true)` is what the human's click literally does.
-- UNVERIFIED LIVE (all three): whether a control obtained from another state via LookUpControl
-- exposes `CallCallback`. Every step is pcall'd and the mechanism actually used is returned, so a
-- build without it records `close_control_uncallable` and takes the fallback rather than failing.
local CIVSIM_ACKNOWLEDGE_ONLY_PROMPTS = {
    ["prompt.tech_civic_completed"] = {
        state = "TechCivicCompletedPopup",
        close_control = "CloseButton",
        fallback = "dequeue_popup",
    },
    ["prompt.boost_unlocked"] = {
        state = "BoostUnlockedPopup",
        close_control = "ContinueButton",
        fallback = "dequeue_popup",
    },
    -- MEASURED 2026-09-21 (gameplay day, block 3, game turn 27): a relic from a tribal village
    -- raised "Your civilization has produced a Great Work" and the probe answered `world` because
    -- no watchlist entry covered it -- every units.move_to underneath it failed verification. The
    -- state is `GreatWorkShowcase` (base/assets/ui/ingame.xml:70, ID == FileName; Lua state 62 in
    -- spikes/r5-raw/00_states.txt); relics reach it through
    -- `LuaEvents.NotificationPanel_ShowRelicCreated` -> `OnShowRelicCreated` ->
    -- `DisplayGreatWorkCreated(..., showRelics=true)` -> `ShowScreen()`
    -- (greatworkshowcase.lua:273-275, :236-239).
    ["prompt.great_work_created"] = {
        state = "GreatWorkShowcase",
        close_control = "ModalScreenClose",
        fallback = "set_hide",
    },
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
--
-- CORRECTED 2026-09-21 (read, not measured — these names are now CONTROL IDs, not Lua state
-- names). `CivSim_Screens_State` resolves each entry as `/InGame/<name>`, and Civ VI's context
-- tree is keyed by a `<LuaContext>`'s **ID** attribute, while the Lua *state* is named after its
-- **FileName**. For every entry below except two they are the same string. The two that differ
-- were silently unprobeable:
--   * `<LuaContext ID="Civilopedia" FileName="CivilopediaScreen" .../>` (base ingame.xml:129,
--     dlc/expansion2/ui/replacements/ingame.xml:142) — so `/InGame/CivilopediaScreen` is nil and
--     T213's note that "CivilopediaScreen is absent on this build" was reading the wrong name; the
--     state does exist (index 109 in 00_states.txt).
--   * `<LuaContext ID="TopOptionsMenu" FileName="InGameTopOptionsMenu" .../>` (base
--     ingame.xml:136) — and `/InGame/TopOptionsMenu` is the path Firaxis's own Lua uses
--     (base/assets/ui/ingame.lua, base/assets/ui/tutorialuiroot.lua). So the pause/options menu,
--     the one screen the P2 sweep actually flipped live, was never seen by the aggregate probe.
-- Both are corrected below. UNVERIFIED LIVE that the corrected paths resolve; the next sweep says.
-- `Options`, `SaveGameMenu` and `LoadGameMenu` are Lua states but are NOT `<LuaContext>` children
-- of InGame in any shipped ingame.xml, so `/InGame/<name>` cannot reach them; they are kept only
-- as documentation of what a future per-state dispatcher would enumerate.
local CIVSIM_SCREEN_WATCHLIST = {
    "CityPanel", "ProductionPanel", "TechTree", "CivicsTree", "GovernmentScreen", "ReligionScreen",
    "DiplomacyActionView", "DiplomacyDealView", "DeclareWarPopup", "UnitPromotionPopup",
    "PantheonChooser", "GreatPeoplePopup", "GreatWorkShowcase", "WorldCongressPopup",
    "WorldCongressBetweenTurns", "WorldCongressIntro", "EventPopup", "EraCompletePopup",
    "NaturalWonderPopup", "LeaderScene", "TechCivicCompletedPopup", "BoostUnlockedPopup",
    "Civilopedia", "InGamePopup", "TopOptionsMenu", "PausePanel", "Options", "SaveGameMenu",
    "LoadGameMenu",
}

-- VERIFIED (P2, screen_identity.md) that each named state exists; UNVERIFIED that
-- ContextPtr:IsHidden()==false on that exact state precisely coincides with the catalog concept
-- named on the left, beyond the one live-flipped case (TopOptionsMenu, confirmed).
--
-- DELIBERATELY UNMAPPED, 2026-09-21 (read against the shipped UI at
-- steamassets/base/assets/ui/ and dlc/expansion2/ui/; written up in
-- specs/002-civ-playing-harness/spikes/screens-unmapped-2026-09-21.md). `civsim store coverage`
-- flags each of these as a claimed screen id that no Lua state maps to. That flag is CORRECT and
-- must stay: none of them has a UI state of its own, so any mapping here would be a fabrication
-- that made the probe assert a screen the client never said was up.
--   * `strategic` — `<LuaContext ID="StrategicView" FileName="StrategicView"/>` (base
--     ingame.xml:13) carries NO Hidden attribute and strategicview.lua is six lines of comment
--     with no show/hide logic at all, so its `IsHidden()` is false at the ordinary world view too.
--     Strategic view is a world *render mode*, not a screen; `UI.GetWorldRenderView()` already
--     answers it through lua/ingame/camera.lua and catalogs/observations/views.yaml.
--   * `prompt.religion_selection` — founding a religion is a NOTIFICATION, not a modal. Activating
--     it fires `LuaEvents.NotificationPanel_OpenReligionPanel()` (notificationpanel.lua:1322) which
--     opens the same `ReligionScreen` the launch bar opens for browsing (religionscreen.lua:1426,
--     :1606-1609). `IsHidden()==false` there means "the religion screen is open", never "a
--     blocking founding prompt is up".
--   * `prompt.diplomatic_approach` — an AI-initiated approach is `Events.DiplomacyStatement` ->
--     `OnDiplomacyStatement` (diplomacyactionview.lua:2741), which shows the SAME
--     `DiplomacyActionView` context in CONVERSATION_MODE/CINEMA_MODE that `diplomacy` already maps
--     to. What separates the two is `ms_ActiveSessionID`/the view mode, private Lua state of that
--     context that `IsHidden()` cannot see.
--   * `prompt.congress_vote` — `WorldCongressPopup` (dlc/expansion2/ui/replacements/ingame.xml:121)
--     is one context for every stage: proposals, voting (`OnVoteResolution`/`OnVoteProposal`,
--     worldcongresspopup.lua:983, :1452) and results. The forced-vote moment is `m_CurrentStage`/
--     `m_CurrentPhase` inside it, and `congress` already maps to that state.
--   * `prompt.city_state_quest` — there is no city-state quest popup in the shipped UI at all.
--     Quests arrive as notifications (`NotificationTypes.CITYSTATE_QUEST_COMPLETED`,
--     notificationpanel.lua:134) and are read in the `CityStates` partial screen
--     (base/assets/ui/partialscreens/citystates.lua); no state in 00_states.txt corresponds to a
--     blocking quest prompt, so the catalog claim itself is what needs correcting.
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
    -- UNVERIFIED LIVE: measured open at game turn 27 (block 3) while the probe said `world`; that
    -- `/InGame/GreatWorkShowcase` reports hidden=false for exactly that popup is what the live
    -- lane still has to observe.
    ["prompt.great_work_created"] = "GreatWorkShowcase",
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
        -- CORRECTED 2026-09-21: this answered `world_view`, a name that appeared nowhere in the
        -- catalog. `game.current_screen`'s declared vocabulary (catalogs/README.md, "Field
        -- vocabulary") and CIVSIM_KNOWN_SCREENS above both say `world`, so the catalog contract
        -- wins and the probe now answers `world`. Nothing else in the catalog ever named
        -- `world_view`; it existed only here and in the Python fakes that mirrored it.
        return {
            screen = "world", raw_screen_id = "InGame", recognized = true,
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
-- Dismiss an acknowledge-only popup the way its own close button does. From the InGame state the
-- popup's context is reachable as a Control (`ContextPtr:LookUpControl`, the same resolution
-- CivSim_Screens_State uses to see it), and so is any named control inside it.
--
-- CORRECTED 2026-09-21 (block 4, live): T253 went straight to `UIManager:DequeuePopup(<the
-- context>)`. That is the second half of the popup's own `Close()`, not the whole of what its
-- button does, and skipping the first half left the popup's private queue holding the card that
-- had just been acknowledged -- see CIVSIM_ACKNOWLEDGE_ONLY_PROMPTS above for the measured
-- consequence. The order is now: drive the popup's own close CONTROL first (its callback runs
-- inside the popup's own Lua state, which is the only place that state's queue can be consumed),
-- and only if that control cannot be driven fall back to the per-popup primitive that InGame can
-- reach, recording which mechanism ran and why the first one did not.
--
-- Still never presses a key, and `set_hide` is used only for `GreatWorkShowcase`, whose own close
-- handler IS `ContextPtr:SetHide(true)` (it is not a UIManager popup). For the two queued popups
-- SetHide would be wrong for exactly the reason T253 gave -- it would leave them queued -- so
-- their fallback stays DequeuePopup.
--
-- UNVERIFIED LIVE: that `CallCallback` exists on a control obtained from another state, that
-- DequeuePopup accepts another state's context from InGame, and that `UIManager`/`Mouse` are
-- reachable here. Every step is pcall'd and every outcome returned, so a failure is recorded as
-- itself and the action's own verification predicate (the popup is no longer the current screen)
-- is what decides `applied`.
local function CivSim_Screens_AcknowledgePopup(promptType, descriptor, optionId)
    if optionId ~= CIVSIM_ACKNOWLEDGE_OPTION then
        return { ok = false, reason = "unknown_option", prompt = promptType, option = optionId }
    end
    local stateName = descriptor.state
    local okC, ctx = pcall(function() return ContextPtr:LookUpControl("/InGame/" .. stateName) end)
    if not okC or ctx == nil then
        return { ok = false, reason = "popup_state_absent", prompt = promptType, option = optionId }
    end
    local okH, hidden = pcall(function() return ctx:IsHidden() end)
    if okH and hidden == true then
        return { ok = false, reason = "popup_not_open", prompt = promptType, option = optionId }
    end

    local result = {
        prompt = promptType, option = optionId, state = stateName,
        close_control = descriptor.close_control,
    }

    -- 1. The button a human clicks. Its callback was registered in the popup's own state, so
    --    invoking it runs that popup's own OnClose/TryClose there -- queue and all.
    local controlPath = "/InGame/" .. stateName .. "/" .. descriptor.close_control
    local okB, button = pcall(function() return ContextPtr:LookUpControl(controlPath) end)
    if not okB or button == nil then
        result.fallback_reason = "close_control_absent"
    else
        local okCall, callErr = pcall(function() button:CallCallback(Mouse.eLClick) end)
        if not okCall then
            result.fallback_reason = "close_control_uncallable"
            result.close_control_error = tostring(callErr)
        else
            local okS, hiddenNow = pcall(function() return ctx:IsHidden() end)
            if okS and hiddenNow == true then
                result.ok = true
                result.mechanism = "close_control_callback"
                result.hidden_after = true
                return result
            end
            -- The callback ran and the popup is still up. For a queued popup that is the CORRECT
            -- outcome: `TryClose` showed the next card behind this one, which is exactly what a
            -- human sees after clicking Continue. Falling back here would dequeue the whole
            -- context and destroy a card the human would have been shown, so this path stops.
            -- The action's verification predicate reports the popup still current and the agent
            -- acknowledges again -- one click per card, as a human does.
            result.ok = true
            result.mechanism = "close_control_callback"
            result.hidden_after = false
            result.note = "still_open_next_queued_card_likely_shown"
            return result
        end
    end

    -- 2. The per-popup primitive InGame can reach, chosen from what that popup's own handler does.
    local okD, err
    if descriptor.fallback == "set_hide" then
        result.mechanism = "ContextPtr:SetHide"
        okD, err = pcall(function() ctx:SetHide(true) end)
    else
        result.mechanism = "UIManager:DequeuePopup"
        okD, err = pcall(function() UIManager:DequeuePopup(ctx) end)
    end
    local okA, hiddenAfter = pcall(function() return ctx:IsHidden() end)
    result.ok = okD
    result.hidden_after = (okA and hiddenAfter == true)
    result.error = (not okD) and tostring(err) or nil
    return result
end

local function CivSim_Screens_RespondToPrompt(promptType, optionId)
    if not CivSim_ScreenIsKnown(promptType) then
        return { ok = false, reason = "unknown_prompt" }
    end
    local descriptor = CIVSIM_ACKNOWLEDGE_ONLY_PROMPTS[promptType]
    if descriptor ~= nil then
        return CivSim_Screens_AcknowledgePopup(promptType, descriptor, optionId)
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
--   print(CivSim_JsonEncode(CivSim_Screens.respond("prompt.great_work_created", "continue")))
