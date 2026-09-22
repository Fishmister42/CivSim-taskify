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
--
-- RETIRED 2026-09-21 (catalogs/README.md §6, evidence in
-- specs/002-civ-playing-harness/spikes/screens-unmapped-2026-09-21.md):
--   * `prompt.religion_selection` -- founding a religion opens the SAME `ReligionScreen` the
--     launch bar opens for browsing, so it is an open screen, not a blocking prompt, and its real
--     interactions are already claimed by catalogs/actions/religion.yaml. A duplicate overclaim.
--   * `prompt.city_state_quest` -- there is no city-state quest popup in the shipped UI at all,
--     and nothing in Civ VI accepts or declines a quest. The claim described an interaction the
--     game does not have.
-- Both are gone from here and from catalogs/actions/prompts.yaml. Do not re-add either: the probe
-- reporting one would be a fabricated prompt, which is exactly what FR-049 exists to prevent.
local CIVSIM_KNOWN_SCREENS = {
    "world", "strategic", "city_screen", "diplomacy", "congress",
    "prompt.unit_promotion", "prompt.pantheon_selection",
    "prompt.great_person_selection", "prompt.diplomatic_approach", "prompt.declare_war_response",
    "prompt.congress_intro", "prompt.congress_vote", "prompt.era_transition",
    "prompt.era_dedication",
    "prompt.tech_civic_completed", "prompt.boost_unlocked", "prompt.great_work_created",
    "prompt.natural_disaster",
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
    -- MEASURED 2026-09-21 (gameplay day, game turn 42, after block 15): a Gathering Storm volcanic
    -- eruption raised "NATURAL DISASTER OCCURRING / Megacolossal Eruption" full-screen; the probe
    -- answered `world` because nothing watched it, and the game's save call returned false under it
    -- (spelled out here would route every probe to the test fake's save handler, which matches
    -- bodies by that name),
    -- so block 16 failed its turn-1 quicksave in 12 s. The state is `NaturalDisasterPopup`
    -- (dlc/expansion2/ui/additions/naturaldisasterpopup.xml; `/InGame/NaturalDisasterPopup`
    -- reported hidden=false while it was up, live). Its own header `Close` button
    -- (naturaldisasterpopup.xml:22) runs `OnClose` -> `Close()` (lua:87,132), which hides the
    -- context and raises `LuaEvents.NaturalDisasterPopup_Closed`; `set_hide` is the honest fallback
    -- since `Close()` itself is a SetHide plus that event. Not a UIManager popup.
    ["prompt.natural_disaster"] = {
        state = "NaturalDisasterPopup",
        close_control = "Close",
        fallback = "set_hide",
    },
    -- MEASURED 2026-09-21 (gameplay block 18, game turns 43-47): Gathering Storm's "The World Has
    -- Entered the Classical Era" card was up for five turns; the probe answered `world` because
    -- the id mapped only the base game's `EraCompletePopup`, while this build shows
    -- `EraReviewPopup` (dlc/expansion2/ui/additions/erareviewpopup.xml; `/InGame/EraReviewPopup`
    -- reported hidden=false live). Sonnet 5 described the card from the delivered frame and asked to
    -- acknowledge it at 40 of 41 steps -- refused each time because the text probe had this hole.
    -- Its `Continue` button (xml:69) and `Close` (xml:67) both run `UIManager:DequeuePopup`
    -- (lua:241-246), so the fallback is dequeue_popup.
    ["prompt.era_transition"] = {
        state = "EraReviewPopup",
        close_control = "Continue",
        fallback = "dequeue_popup",
    },
    -- MEASURED 2026-09-21 (live stage, game turn 57): the World Congress "Begin Voting" welcome
    -- card came up over the Classical era review and the run paused `UnknownScreenEncountered` --
    -- `WorldCongressIntro` was already on the watchlist below but mapped to no catalog id, so the
    -- probe correctly refused to name it (FR-049) and play stopped. It is a one-button welcome:
    -- `worldcongressintro.xml:13` declares a single `AcceptButton` (String
    -- `LOC_WORLD_CONGRESS_INTRO_ACCEPT` = "Begin Voting"), and `worldcongressintro.lua:153` binds
    -- it to `OnClose` (:26-29), which is `UIManager:DequeuePopup(ContextPtr)` FOLLOWED BY
    -- `LuaEvents.WorldCongressIntro_ShowWorldCongress()` -- the event that actually opens the
    -- congress (`worldcongresspopup.lua:2632` subscribes to it).
    --
    -- So the fallback here is weaker than the real click in a way that must be recorded, not
    -- hidden: `UIManager:DequeuePopup` alone closes the welcome card and does NOT open the
    -- congress, because the second half of that handler is a LuaEvent raised inside the intro's
    -- own state. The real click through the host input port runs both halves. The Lua reports
    -- which mechanism ran, and `prompts.congress_intro`'s verification predicate (the intro is no
    -- longer the current screen) is what decides `applied`.
    --
    -- Its option is "accept", not "continue": the button a human reads says Begin Voting, and
    -- accepting the congress is a different act from acknowledging a card that only reports.
    ["prompt.congress_intro"] = {
        state = "WorldCongressIntro",
        close_control = "AcceptButton",
        fallback = "dequeue_popup",
        option = "accept",
    },
}
local CIVSIM_ACKNOWLEDGE_OPTION = "continue"

-- The single option an acknowledge-only popup offers. "continue" for the cards that only report
-- something; a popup whose one button does something a human would not call "continue" names its
-- own (see `prompt.congress_intro`).
local function CivSim_AcknowledgeOption(descriptor)
    if type(descriptor.option) == "string" then return descriptor.option end
    return CIVSIM_ACKNOWLEDGE_OPTION
end

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
--
-- ORDER IS NOW LOAD-BEARING (2026-09-21). The prompt that names the screen is the FIRST open
-- state in this list that maps to a `prompt.*` id (see CivSim_Screens_State) -- it used to be the
-- last, which was an arbitrary artefact of the loop rather than a decision. Two pairs of states
-- can now legitimately be open at once, and in each pair the modal card that covers the other
-- must win, so each is listed before the screen it covers:
--   * `WorldCongressIntro` before `WorldCongressPopup` -- the welcome card is modal over the
--     congress it is about to open (`worldcongressintro.lua:26-29`).
--   * `DedicationPopup` before `EraReviewPopup` -- the era card's own Continue dequeues itself
--     before raising `EraReviewPopup_MakeDedication` (`erareviewpopup.lua:241-247`), so in
--     practice only one is up; the order makes the outcome deterministic if both ever are.
local CIVSIM_SCREEN_WATCHLIST = {
    "CityPanel", "ProductionPanel", "TechTree", "CivicsTree", "GovernmentScreen", "ReligionScreen",
    "DiplomacyActionView", "DiplomacyDealView", "DeclareWarPopup", "UnitPromotionPopup",
    "PantheonChooser", "GreatPeoplePopup", "GreatWorkShowcase", "WorldCongressIntro",
    "WorldCongressPopup", "WorldCongressBetweenTurns", "EventPopup", "EraCompletePopup",
    "NaturalWonderPopup", "LeaderScene", "TechCivicCompletedPopup", "BoostUnlockedPopup",
    "Civilopedia", "InGamePopup", "TopOptionsMenu", "PausePanel", "Options", "SaveGameMenu",
    "LoadGameMenu", "NaturalDisasterPopup", "DedicationPopup", "EraReviewPopup",
}

-- VERIFIED (P2, screen_identity.md) that each named state exists; UNVERIFIED that
-- ContextPtr:IsHidden()==false on that exact state precisely coincides with the catalog concept
-- named on the left, beyond the one live-flipped case (TopOptionsMenu, confirmed).
--
-- DELIBERATELY UNMAPPED, 2026-09-21 (read against the shipped UI at
-- steamassets/base/assets/ui/ and dlc/expansion2/ui/; written up in
-- specs/002-civ-playing-harness/spikes/screens-unmapped-2026-09-21.md). `civsim store coverage`
-- flags each of these as a claimed screen id that no Lua state maps to. That flag is CORRECT and
-- must stay: neither has a UI state of its own, so any mapping here would be a fabrication that
-- made the probe assert a screen the client never said was up. Both are kept as claims because
-- the interaction is real and only the probe's reach is missing -- unlike
-- `prompt.religion_selection` and `prompt.city_state_quest`, whose claims were themselves wrong
-- and were RETIRED on the same day (see CIVSIM_KNOWN_SCREENS above and catalogs/README.md §6).
--   * `strategic` — `<LuaContext ID="StrategicView" FileName="StrategicView"/>` (base
--     ingame.xml:13) carries NO Hidden attribute and strategicview.lua is six lines of comment
--     with no show/hide logic at all, so its `IsHidden()` is false at the ordinary world view too.
--     Strategic view is a world *render mode*, not a screen; `UI.GetWorldRenderView()` already
--     answers it through lua/ingame/camera.lua and catalogs/observations/views.yaml. So the probe
--     cannot report `strategic`, and must not: watching that context would answer it permanently.
--   * `prompt.congress_vote` — `WorldCongressPopup` (dlc/expansion2/ui/replacements/ingame.xml:121)
--     is one context for every stage: proposals, voting (`OnVoteResolution`/`OnVoteProposal`,
--     worldcongresspopup.lua:983, :1452) and results (`OnWorldCongressResults`, :2347). The
--     forced-vote moment is `m_CurrentStage`/`m_CurrentPhase` inside that context's own private
--     Lua state, which `IsHidden()` cannot see, and `congress` already maps to that state —
--     mapping the prompt id here would both fabricate a distinction and shadow `congress`.
--     RESOLVED 2026-09-21 the same way `prompt.diplomatic_approach` was, and for the same reason:
--     not by reading the private stage variable but by reading the PHASE CONTROLS' own
--     visibility, which is how the stage is expressed on screen (see CIVSIM_CONGRESS_STATE
--     below). The id is answered directly and must stay out of this table.
--   * `prompt.diplomatic_approach` — an AI-initiated approach is `Events.DiplomacyStatement` ->
--     `OnDiplomacyStatement` (diplomacyactionview.lua:2741), which shows the SAME
--     `DiplomacyActionView` context in CONVERSATION_MODE/CINEMA_MODE that `diplomacy` already maps
--     to. What separates the two is `ms_ActiveSessionID`/the view mode, private Lua state of that
--     context that `IsHidden()` cannot see. Resolved without a state mapping: the mode IS readable
--     as control visibility, so the probe answers this id directly (see CIVSIM_DIPLOMACY_STATE
--     below) and it must stay out of this table.
local CIVSIM_SCREEN_ID_BY_STATE = {
    city_screen = "CityPanel",
    congress = "WorldCongressPopup",
    diplomacy = "DiplomacyActionView",
    ["prompt.unit_promotion"] = "UnitPromotionPopup",
    ["prompt.pantheon_selection"] = "PantheonChooser",
    ["prompt.great_person_selection"] = "GreatPeoplePopup",
    ["prompt.declare_war_response"] = "DeclareWarPopup",
    -- MEASURED 2026-09-21 (block 18): this Gathering Storm build shows `EraReviewPopup` for the
    -- era card; the base game's `EraCompletePopup` (still on the watchlist) was never seen open.
    ["prompt.era_transition"] = "EraReviewPopup",
    -- T253: the two acknowledge-only popups (see CIVSIM_ACKNOWLEDGE_ONLY_PROMPTS above). The state
    -- names are the ones on the watchlist, confirmed to exist at turn 1 (P2); that IsHidden()==false
    -- on `TechCivicCompletedPopup` coincides with the popup being up was observed live (attempt 5).
    ["prompt.tech_civic_completed"] = "TechCivicCompletedPopup",
    ["prompt.boost_unlocked"] = "BoostUnlockedPopup",
    -- UNVERIFIED LIVE: measured open at game turn 27 (block 3) while the probe said `world`; that
    -- `/InGame/GreatWorkShowcase` reports hidden=false for exactly that popup is what the live
    -- lane still has to observe.
    ["prompt.great_work_created"] = "GreatWorkShowcase",
    -- MEASURED 2026-09-21: `/InGame/NaturalDisasterPopup` hidden=false while the eruption
    -- cinematic was up (see CIVSIM_ACKNOWLEDGE_ONLY_PROMPTS).
    ["prompt.natural_disaster"] = "NaturalDisasterPopup",
    -- MEASURED 2026-09-21 (live stage, game turn 57): the congress welcome card blocked play and
    -- the probe answered `unknown` -- the state was watched but unmapped. Unlike
    -- `WorldCongressPopup`, `WorldCongressIntro` is a single-purpose context: it exists only to be
    -- the "Begin Voting" card and is hidden at every other moment
    -- (`worldcongressintro.lua:139` `ContextPtr:SetHide(true) -- PRODUCTION`, shown only from
    -- `OnOpen` -> `UIManager:QueuePopup`, :43), so `IsHidden()==false` on it means exactly that
    -- card is up. That is why it CAN carry a state mapping where `prompt.congress_vote` cannot.
    ["prompt.congress_intro"] = "WorldCongressIntro",
    -- MEASURED 2026-09-21 (gameplay block 20): Gathering Storm's Classical-era dedication chooser
    -- was on screen for 41 steps while the probe answered `world`, and Sonnet 5 -- reading the
    -- delivered frame alone -- asked to answer it at 40 of them; every request was refused because
    -- no screen id covered it. The state is `DedicationPopup`
    -- (`dlc/expansion2/ui/additions/dedicationpopup.lua`, added to the InGame context tree by
    -- `dlc/expansion2/expansion2.modinfo:302-306`'s `<AddUserInterfaces>` with
    -- `<Context>InGame</Context>`, which is why it appears in no shipped `ingame.xml`). Like the
    -- intro above it is single-purpose: `Initialize` hides it (`dedicationpopup.lua:283`) and the
    -- only thing that shows it is `ShowPopup` -> `UIManager:QueuePopup` (:220-222) from
    -- `LuaEvents.EraReviewPopup_MakeDedication` (:293).
    --
    -- MEASURED 2026-09-21 (autoplay spike, `specs/002-civ-playing-harness/spikes/
    -- autoplay-fast-forward-linux.md`): `ContextPtr:LookUpControl("/InGame/DedicationPopup")`
    -- resolves from InGame and the context reported `hidden_after = true` once dequeued -- the
    -- path and the read are both live-measured. What is UNVERIFIED LIVE is everything below it:
    -- the per-instance label read and the two clicks.
    ["prompt.era_dedication"] = "DedicationPopup",
}

-- MEASURED 2026-09-21 (gameplay day, block 7, game turn 35): play stalled for five harness turns
-- on Australia's first-meeting leader scene (John Curtin, two statement choices). Nine
-- `send_delegation` orders were refused and four end turns never confirmed, because the probe
-- reported `diplomacy` with has_blocking_prompt=false -- correct for the STATE, wrong for the
-- MODE. `DiplomacyActionView` is one context for four modes (diplomacyactionview.lua:39-42:
-- OVERVIEW / CONVERSATION / CINEMA / DEAL), and an AI-initiated approach is CONVERSATION_MODE in
-- that same context, which is why CIVSIM_SCREEN_ID_BY_STATE alone can never tell them apart.
--
-- The mode IS readable from InGame, because the mode switch is expressed as control visibility:
-- `SetConversationMode` shows `Controls.ConversationContainer` and hides `Controls.
-- OverviewContainer` (diplomacyactionview.lua:1744-1756), and OVERVIEW_MODE does the reverse
-- (:1831-1834). The statement's choices are instances in `Controls.ConversationSelectionStack`
-- (instance manager declared at :104 over the `ConversationSelectionInstance` whose root control
-- is `SelectionButton`, with a `SelectionText` label inside it --
-- diplomacyactionview.xml:381-382, :432, :441, :451). `ApplyStatement` (:561-643) fills one
-- instance per selection, sets its label to the localized choice text a human reads, disables the
-- ones that are not takeable, and registers the click callback that calls
-- `handler.OnSelectionButtonClicked(selection.Key)` -- i.e. `OnSelectConversationDiplomacyStatement`
-- (:488, bound at :2534).
--
-- Parity: the options reported are the visible label texts of the ENABLED, VISIBLE choice buttons
-- only -- exactly the set a human could click, never a superset. A disabled choice is shown to the
-- human but cannot be taken, so it is not offered. The instance manager leaves recycled instances
-- in the stack hidden and pushed to the back (techandcivicsupport.lua:216-218 documents this), so
-- hidden children are skipped rather than reported as choices.
--
-- Corroborated by the operator scripting that cleared this greeting by hand
-- (spikes/gameplay-2026-09-21/operator_answer_greeting.py, MEASURED 12:35 EDT):
-- `ContextPtr:LookUpControl("/InGame/DiplomacyActionView")` does resolve from InGame and its
-- `IsHidden()` does report the scene, which is the same path shape the control lookups below use.
-- That script also measured that answering once is not the end of it -- the leader replies and the
-- session stays open until the conversation's own Exit choice is taken. That is the human flow
-- (click a reply, then click Exit), so the probe simply reports the prompt again with the new
-- options and the agent answers again; `still_in_conversation` on the answer result says so.
--
-- UNVERIFIED LIVE: every step here. Each is pcall'd, and a failure degrades to the previous
-- behaviour (`diplomacy`, not blocking) rather than inventing a prompt.
local CIVSIM_DIPLOMACY_STATE = "DiplomacyActionView"
local CIVSIM_DIPLOMACY_CONVERSATION_CONTAINER = "ConversationContainer"
local CIVSIM_DIPLOMACY_SELECTION_STACK = "ConversationSelectionStack"

-- MEASURED 2026-09-21 (live stage, game turns 42 and 58; store runs `run-d0933ca8...` and its two
-- siblings). Two separate faults, both fatal to answering a leader:
--
-- 1. THE ANSWER DID NOT LAND. After a reload onto Georgia's first-meeting greeting, three goal runs
--    stuck 16 of 16 steps on `prompts.ai_diplomatic_approach`. Read back out of the store: at every
--    one of those 16 steps `prompt_options` was byte-identical --
--    ["Would you like to visit our nearby city and sample our hospitality?", "Thanks for the
--    introduction, but we have no time for further pleasantries."] -- and the target was the first
--    of them. The options never changed, so nothing the harness did reached the game. The host
--    click on a conversation `SelectionButton` had never been demonstrated live; what HAD been
--    demonstrated, at 12:35 EDT on the same greeting, was
--    `specs/002-civ-playing-harness/spikes/gameplay-2026-09-21/operator_answer_greeting.py`:
--    `DiplomacyManager.AddResponse(sessionID, localPlayer, "NEGATIVE")` returned ok, the leader
--    answered, and the session stayed open with a new set of choices.
-- 2. THE EXIT WAS DETECTED BY THE WRONG THING. The previous pass matched the exit choice by its
--    label being `Locale.Lookup("LOC_DIPLO_CHOICE_EXIT")` ("Goodbye"). A first meeting's decline is
--    `CHOICE_EXIT` too, but it carries its OWN text -- e.g.
--    `<Key>CHOICE_EXIT</Key><Text>LOC_DIPLO_CHOICE_FIRST_MEET_DECLINE_VISIT</Text>`
--    (base/assets/gameplay/data/diplomacystatements_firstmeet.xml:172-178, and the same shape at
--    :155-162, :186-193, :213-218, :228-234). Matching "Goodbye" could never have recognised it.
--
-- So the answer is resolved by the selection's own KEY, and the key is not guessed at: it is read
-- from the same gameplay-database rows `ExtractStatement` itself reads to build the buttons.
-- `DiplomacySupport_ExtractStatement` -> `GetStatementFromQuery` runs
-- `DB.Query("SELECT Text, Tooltip, Key, Sort, DiplomaticActionType from DiplomacySelections ...")`
-- (base/assets/ui/diplomacystatementsupport.lua:77-84) and `ApplyStatement` puts
-- `Locale.Lookup(selection.Text)` on the button (diplomacyactionview.lua:592). Looking up the same
-- table from InGame and localising the same `Text` therefore reproduces the exact string on the
-- button, and hands back the `Key` behind it. A label that more than one distinct key renders to is
-- dropped from the map rather than resolved to either -- no guessing.
--
-- With the key in hand, the call made is the one the popup's own handler
-- `OnSelectConversationDiplomacyStatement` makes for that key (diplomacyactionview.lua:489-534):
--   * `CHOICE_EXIT`     -> `ExitConversationMode()` (:491-492), whose whole effect is
--                          `DiplomacyManager.CloseSession(ms_ActiveSessionID)` (:310-330, :323).
--   * `CHOICE_POSITIVE` -> `DiplomacyManager.AddResponse(session, localPlayer, "POSITIVE")` (:525-526)
--   * `CHOICE_NEGATIVE` -> `... "NEGATIVE"` (:528-529)   [the route measured live at 12:35 EDT]
--   * `CHOICE_IGNORE`   -> `... "RESPONSE_IGNORE"` (:531-532)
-- Every other key in that switch -- the `CHOICE_DECLARE_*_WAR` family, `CHOICE_MAKE_PEACE`,
-- `CHOICE_MAKE_DEAL`, `CHOICE_MAKE_DEMAND` (:493-521) -- is deliberately NOT reproduced here. Those
-- are consequential moves with their own catalog actions (`diplomacy.declare_war`,
-- `diplomacy.make_peace`), and issuing one off a label lookup is not a risk worth taking for a
-- convenience; they stay on the click path with the key recorded, so the record says exactly why.
--
-- `ms_ActiveSessionID` is private, so the session is found the way Firaxis's own shared code finds
-- it: loop the player slots and ask `DiplomacyManager.FindOpenSessionID(localPlayer, other)`
-- (base/assets/ui/civ6common.lua:688-698, the identical loop). Exactly one open session is the
-- unambiguous case and the only one acted on; with none or several it falls back to the click,
-- recording why, rather than answering a session it cannot prove is the one on screen.
--
-- UNVERIFIED LIVE: the key lookup and the POSITIVE/EXIT routes. `path` on every answer result says
-- which route ran (`add_response`, `close_session` or `host_click`) and `choice_key` says what the
-- label resolved to, so the next first meeting settles it from the record alone.
local CIVSIM_DIPLOMACY_EXIT_TEXT_KEY = "LOC_DIPLO_CHOICE_EXIT"
local CIVSIM_DIPLOMACY_EXIT_CHOICE_KEY = "CHOICE_EXIT"

-- The shipped handler's own key -> response mapping, nothing added (diplomacyactionview.lua
-- :524-532).
local CIVSIM_DIPLOMACY_RESPONSE_BY_KEY = {
    CHOICE_POSITIVE = "POSITIVE",
    CHOICE_NEGATIVE = "NEGATIVE",
    CHOICE_IGNORE = "RESPONSE_IGNORE",
}

local function CivSim_DiplomacyExitLabel()
    local ok, text = pcall(function() return Locale.Lookup(CIVSIM_DIPLOMACY_EXIT_TEXT_KEY) end)
    if ok and type(text) == "string" and text ~= "" then return text end
    return nil
end

-- Rendered button label -> the `CHOICE_*` key behind it, built from the game's own
-- `DiplomacySelections` rows. A label two different keys render to is dropped: an ambiguous label
-- resolves to nothing and falls through to the click, which is what a human does anyway.
local function CivSim_DiplomacyKeyByLabel()
    local okQ, rows = pcall(function()
        return DB.Query("SELECT Text, Key from DiplomacySelections")
    end)
    if not okQ or rows == nil then return nil end
    local byLabel, ambiguous = {}, {}
    local okI = pcall(function()
        for _, row in ipairs(rows) do
            local text, key = row.Text, row.Key
            if type(text) == "string" and type(key) == "string" then
                local okL, label = pcall(function() return Locale.Lookup(text) end)
                if okL and type(label) == "string" and label ~= "" then
                    if byLabel[label] ~= nil and byLabel[label] ~= key then
                        ambiguous[label] = true
                    else
                        byLabel[label] = key
                    end
                end
            end
        end
    end)
    if not okI then return nil end
    for label in pairs(ambiguous) do byLabel[label] = nil end
    return byLabel
end

local function CivSim_DiplomacyOpenSessionIds()
    local ids = {}
    local okL, localPlayerID = pcall(function() return Game.GetLocalPlayer() end)
    if not okL or type(localPlayerID) ~= "number" then return ids end
    for i = 0, 63 do
        local ok, sessionID = pcall(function()
            local other = Players[i]
            if other == nil then return nil end
            local otherID = other:GetID()
            if otherID == localPlayerID then return nil end
            return DiplomacyManager.FindOpenSessionID(localPlayerID, otherID)
        end)
        if ok and type(sessionID) == "number" then ids[#ids + 1] = sessionID end
    end
    return ids
end

-- The visible text of one control: its own text if it carries one, else the first non-empty text
-- found walking its children depth-first, at most `depth` levels down. Depth 1 is the diplomacy
-- case (the `SelectionText` label lives directly inside the `SelectionButton`). The dedication
-- chooser needs depth 3: the instance's root control is the `SelectCheck` GridButton, whose
-- `MomentCategory` label sits inside an unnamed `Stack` inside it (dedicationpopup.xml:38-49).
-- Depth-first order is what makes the FIRST label found the one a human reads as the option's
-- name -- `MomentCategory` ("Free Inquiry", "Monumentality", ...) is declared before
-- `MomentBonuses` in that stack (dedicationpopup.xml:46-47).
local function CivSim_ControlTextDeep(control, depth)
    local okT, text = pcall(function() return control:GetText() end)
    if okT and type(text) == "string" and text ~= "" then return text end
    if depth <= 0 then return nil end
    local okC, children = pcall(function() return control:GetChildren() end)
    if not okC or children == nil then return nil end
    for _, child in ipairs(children) do
        local childText = CivSim_ControlTextDeep(child, depth - 1)
        if childText ~= nil then return childText end
    end
    return nil
end

local function CivSim_ControlText(control)
    return CivSim_ControlTextDeep(control, 1)
end

local function CivSim_DiplomacyLookUp(controlName)
    local ok, control = pcall(function()
        return ContextPtr:LookUpControl("/InGame/" .. CIVSIM_DIPLOMACY_STATE .. "/" .. controlName)
    end)
    if not ok then return nil end
    return control
end

-- Every visible, enabled statement-choice button currently offered, as {control, text} pairs, or
-- nil when the diplomacy view is not in conversation mode at all (no statement is awaiting an
-- answer). An empty list means conversation mode with nothing takeable -- still not a prompt the
-- agent can answer, so the caller treats it as the ordinary `diplomacy` screen.
local function CivSim_DiplomacyStatementChoices()
    local container = CivSim_DiplomacyLookUp(CIVSIM_DIPLOMACY_CONVERSATION_CONTAINER)
    if container == nil then return nil end
    local okH, hidden = pcall(function() return container:IsHidden() end)
    if not okH or hidden ~= false then return nil end
    local stack = CivSim_DiplomacyLookUp(CIVSIM_DIPLOMACY_SELECTION_STACK)
    if stack == nil then return nil end
    local okC, children = pcall(function() return stack:GetChildren() end)
    if not okC or children == nil then return {} end
    local choices = {}
    for _, child in ipairs(children) do
        local okCh, childHidden = pcall(function() return child:IsHidden() end)
        local okD, disabled = pcall(function() return child:IsDisabled() end)
        if okCh and childHidden == false and not (okD and disabled == true) then
            local text = CivSim_ControlText(child)
            if text ~= nil then choices[#choices + 1] = { control = child, text = text } end
        end
    end
    return choices
end

-- ---------------------------------------------------------------------------
-- The Gathering Storm era dedication chooser (`prompt.era_dedication`)
-- ---------------------------------------------------------------------------
--
-- MEASURED 2026-09-21 (gameplay block 20, Classical era): the chooser was on screen for 41 steps
-- while the text probe answered `world`. Sonnet 5 described the card from the delivered frame and
-- asked to answer it at 40 of 41 steps; all 40 were refused, because an id the catalog does not
-- know is exactly what FR-049 makes the harness stall on. The picture was right and the text was
-- blind -- this section closes that hole.
--
-- What a human sees and clicks (`dlc/expansion2/ui/additions/dedicationpopup.lua` / `.xml`):
--   * a card titled "Make your Dedication for the <Era> Era" (lua:57, `LOC_ERA_COMMEMORATION_
--     POPUP_DEDICATION_SUBHEADER`) over an age banner;
--   * one big button per offered commemoration, stacked in `CommemorationsStack` (xml:23). Each is
--     a `Commemoration` instance whose ROOT control is the `SelectCheck` GridButton (xml:38; the
--     instance manager is declared over exactly that control name, lua:25), carrying an icon, a
--     `MomentCategory` label -- the commemoration's name, uppercased by `Locale.ToUpper`
--     (lua:121) -- and a `MomentBonuses` label describing what it does;
--   * a `Confirm` button (xml:33), DISABLED until exactly
--     `Game.GetEras():GetPlayerNumAllowedCommemorations(localPlayer)` of them are selected
--     (`UpdateConfirmButton`, lua:200-203), and a `CloseButton` X (xml:30).
-- Clicking an option toggles its `SelectCheck` selected state (`OnCommemorationSelected`,
-- lua:160-197); at capacity a further click clears the previous picks and starts over
-- (lua:181-194). `Confirm` runs `OnConfirm` (lua:206-217): close, then one
-- `UI.RequestPlayerOperation(localPlayer, PlayerOperations.COMMEMORATE, {PARAM_COMMEMORATION_TYPE
-- = <type>})` per selection. `CloseButton` runs `OnClose` (lua:225-227), a bare
-- `UIManager:DequeuePopup(ContextPtr)` -- a human MAY dedicate nothing that way.
--
-- Parity: the options reported are the visible, enabled instances' own labels -- exactly the
-- buttons a human could click, never a superset. Hidden instances are recycled instance-manager
-- slots (the same convention documented for the diplomacy stack above) and are skipped. Nothing
-- reads which commemoration is "best": the bonus text on the card is the human's own information.
--
-- UNVERIFIED LIVE: the per-instance label read and both clicks. `/InGame/DedicationPopup` itself
-- resolving from InGame, and `UIManager:DequeuePopup` on it reporting `hidden_after = true`, WERE
-- measured (spikes/autoplay-fast-forward-linux.md).
local CIVSIM_DEDICATION_STATE = "DedicationPopup"
local CIVSIM_DEDICATION_STACK = "CommemorationsStack"
local CIVSIM_DEDICATION_CONFIRM = "Confirm"
local CIVSIM_DEDICATION_LABEL_DEPTH = 3

local function CivSim_LookUp(stateName, controlName)
    local path = "/InGame/" .. stateName
    if controlName ~= nil then path = path .. "/" .. controlName end
    local ok, control = pcall(function() return ContextPtr:LookUpControl(path) end)
    if not ok then return nil end
    return control
end

local function CivSim_IsVisible(control)
    local ok, hidden = pcall(function() return control:IsHidden() end)
    return ok and hidden == false
end

local function CivSim_IsDisabled(control)
    local ok, disabled = pcall(function() return control:IsDisabled() end)
    return ok and disabled == true
end

-- Every commemoration a human could click right now, as {control, text, selected} records, or nil
-- when the chooser is not open at all. An empty list means the chooser is open with nothing
-- clickable in it, which is not an answerable prompt either.
local function CivSim_DedicationChoices()
    local ctx = CivSim_LookUp(CIVSIM_DEDICATION_STATE, nil)
    if ctx == nil or not CivSim_IsVisible(ctx) then return nil end
    local stack = CivSim_LookUp(CIVSIM_DEDICATION_STATE, CIVSIM_DEDICATION_STACK)
    if stack == nil then return {} end
    local okC, children = pcall(function() return stack:GetChildren() end)
    if not okC or children == nil then return {} end
    local choices = {}
    for _, child in ipairs(children) do
        if CivSim_IsVisible(child) and not CivSim_IsDisabled(child) then
            local text = CivSim_ControlTextDeep(child, CIVSIM_DEDICATION_LABEL_DEPTH)
            if text ~= nil then
                local okS, selected = pcall(function() return child:IsSelected() end)
                choices[#choices + 1] = {
                    control = child, text = text, selected = (okS and selected == true),
                }
            end
        end
    end
    return choices
end

-- How many commemorations this dedication allows, read the same way the popup itself reads it
-- (`dedicationpopup.lua:65`, :162, :201). The local player's own allowance, which the human learns
-- by watching Confirm stay greyed out until that many are picked -- never another player's. nil
-- when the call is unavailable, in which case `Confirm`'s own disabled state (which IS what the
-- human reads) still drives the answer.
local function CivSim_DedicationSelectionsAllowed()
    local ok, allowed = pcall(function()
        return Game.GetEras():GetPlayerNumAllowedCommemorations(Game.GetLocalPlayer())
    end)
    if ok and type(allowed) == "number" then return allowed end
    return nil
end

-- ---------------------------------------------------------------------------
-- The World Congress phase controls (`prompt.congress_vote`)
-- ---------------------------------------------------------------------------
--
-- MEASURED 2026-09-21 (live stage, game turn 57): the congress opened over the era review and the
-- run had to be cleared by hand -- Accept on the welcome card, Next through the proposal pages,
-- then Accept with no votes, which is the sequence a human uses to abstain.
--
-- `WorldCongressPopup` is one context for every stage of a congress, and which stage is up lives
-- in `m_CurrentStage`/`m_CurrentPhase`, private to that context's own Lua state -- the documented
-- gap. This does NOT read that variable. It reads what the stage is expressed AS on screen: the
-- navigation buttons' own visibility and disabled state, set by `UpdateNavButtons`
-- (worldcongresspopup.lua:380-425) and declared as direct, named children of the popup in
-- `ButtonStack` (worldcongresspopup.xml:117-124):
--   * `NextButton` -- shown unless the last phase is reached (`SetHide(m_CurrentStage >= 3 or
--     m_CurrentPhase == PHASE_STEP_MAX)`, :391) and disabled unless every proposal on the page has
--     been answered (`SetDisabled(not canNextPhase)`, :392, over `CanMoveToNextPhase`, :310-330).
--     Its handler is `OnNext` -> `SetPhase(m_CurrentPhase + 1)` (:2319-2323).
--   * `AcceptButton` -- shown at the last phase (`SetShow(m_CurrentPhase == PHASE_STEP_MAX)`,
--     :409), disabled per `CanSubmit()` (:414-415), which returns true outright for the ordinary
--     session stages (:347) -- this is the abstain path: submitting with no votes cast is
--     something the game lets a human do. Its handler is `OnAccept` (:2222), which issues the
--     player's votes through `UI.RequestPlayerOperation`.
--   * `PassButton` -- shown only for a special-session emergency proposal (:399); `OnPass`
--     (:2523-2526) closes the popup and dismisses that notification.
-- `PrevButton` and `ReturnButton` exist too and are deliberately NOT offered: they navigate
-- backwards / return to a review tab rather than answer the moment, so the offered set stays a
-- strict subset of what the human is being asked for.
--
-- SCOPE, said plainly: this answers the congress's NAVIGATION, not its content. Casting actual
-- votes -- choosing a resolution's option, spending favor, upvoting or downvoting a proposal --
-- runs through per-instance pulldowns and vote steppers inside the popup and is NOT mapped here;
-- an agent using this action abstains. `catalogs/actions/congress.yaml`'s `congress.cast_vote`
-- remains the separate, unmapped claim for casting a vote.
--
-- Deliberately not in CIVSIM_SCREEN_ID_BY_STATE, for the reason the diplomatic approach is not:
-- `congress` already maps to this state, and mapping the prompt id there would report a blocking
-- prompt for any open congress screen, including browsing last session's results.
--
-- UNVERIFIED LIVE: every step here. Each is pcall'd, and a failure degrades to `congress`
-- (non-blocking) rather than inventing a prompt.
local CIVSIM_CONGRESS_STATE = "WorldCongressPopup"
local CIVSIM_CONGRESS_PHASE_CONTROLS = {
    { option = "next", control = "NextButton" },
    { option = "accept", control = "AcceptButton" },
    { option = "pass", control = "PassButton" },
}

-- Every navigation control the human could click right now, as {option, control} records, or nil
-- when the congress popup is not open at all.
local function CivSim_CongressPhaseChoices()
    local ctx = CivSim_LookUp(CIVSIM_CONGRESS_STATE, nil)
    if ctx == nil or not CivSim_IsVisible(ctx) then return nil end
    local choices = {}
    for _, entry in ipairs(CIVSIM_CONGRESS_PHASE_CONTROLS) do
        local control = CivSim_LookUp(CIVSIM_CONGRESS_STATE, entry.control)
        if control ~= nil and CivSim_IsVisible(control) and not CivSim_IsDisabled(control) then
            choices[#choices + 1] = { option = entry.option, control = control }
        end
    end
    return choices
end

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
    -- Three prompts are MODES of a context that is also an ordinary screen, so they are read from
    -- the context's own controls rather than from its state name (block 7, game turn 35 for the
    -- leader statement; game turn 57 for the congress). Each reader answers nil when its context
    -- is not open at all, so the cost here is one lookup per open candidate, not per watchlist
    -- entry.
    local diplomacyChoices = nil
    local congressChoices = nil
    local dedicationChoices = nil
    for _, name in ipairs(open) do
        if name == CIVSIM_DIPLOMACY_STATE then
            diplomacyChoices = CivSim_DiplomacyStatementChoices()
        elseif name == CIVSIM_CONGRESS_STATE then
            congressChoices = CivSim_CongressPhaseChoices()
        elseif name == CIVSIM_DEDICATION_STATE then
            dedicationChoices = CivSim_DedicationChoices()
        end
    end

    -- A recognised prompt outranks anything else that is open (it is what blocks the player);
    -- otherwise the first open watchlist screen names the view.
    --
    -- CORRECTED 2026-09-21: the FIRST open state that maps to a prompt id wins, in watchlist
    -- order, and the search stops there. It used to be the last one found, which was an accident
    -- of the loop rather than a decision -- and now that two modal cards can each cover a screen
    -- that is itself mapped (see CIVSIM_SCREEN_WATCHLIST's ordering note), which one is reported
    -- has to be a decision.
    local raw, screen = nil, nil
    for _, name in ipairs(open) do
        for id, state in pairs(CIVSIM_SCREEN_ID_BY_STATE) do
            if state == name and string.sub(id, 1, 7) == "prompt." and raw == nil then
                raw, screen = name, id
            end
        end
        if raw ~= nil then break end
    end
    if raw == nil and diplomacyChoices ~= nil and #diplomacyChoices > 0 then
        raw = CIVSIM_DIPLOMACY_STATE
        -- Written as a literal, not a constant: this is an id the probe answers WITHOUT going
        -- through CIVSIM_SCREEN_ID_BY_STATE (it must not be in that table, or an open diplomacy
        -- screen in any mode would report a blocking prompt), and `civsim store coverage` reads
        -- these literals to know the id is reachable at all.
        screen = "prompt.diplomatic_approach"
    end
    if raw == nil and congressChoices ~= nil and #congressChoices > 0 then
        raw = CIVSIM_CONGRESS_STATE
        -- The same shape, and a literal for the same two reasons: `congress` already maps to this
        -- state, so the id must stay out of CIVSIM_SCREEN_ID_BY_STATE, and coverage reads these
        -- literals. A congress popup open with no navigation control offered (browsing last
        -- session's results, say) falls through to the ordinary `congress` screen below.
        screen = "prompt.congress_vote"
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
    local extra = nil
    local acknowledge = CIVSIM_ACKNOWLEDGE_ONLY_PROMPTS[screen]
    if acknowledge ~= nil then
        options = { CivSim_AcknowledgeOption(acknowledge) }
    elseif screen == "prompt.diplomatic_approach" and diplomacyChoices ~= nil then
        for _, choice in ipairs(diplomacyChoices) do options[#options + 1] = choice.text end
    elseif screen == "prompt.congress_vote" and congressChoices ~= nil then
        for _, choice in ipairs(congressChoices) do options[#options + 1] = choice.option end
    elseif screen == "prompt.era_dedication" and dedicationChoices ~= nil then
        -- The commemoration labels a human reads on the cards, plus how many of them this
        -- dedication takes and which are already ticked -- the three things that are on the
        -- screen and that decide whether Confirm is clickable yet.
        local selected = {}
        for _, choice in ipairs(dedicationChoices) do
            options[#options + 1] = choice.text
            if choice.selected then selected[#selected + 1] = choice.text end
        end
        extra = {
            prompt_selections_allowed = CivSim_DedicationSelectionsAllowed(),
            prompt_selections_made = #selected,
            prompt_selected_options = selected,
        }
    end
    local result = {
        screen = screen, raw_screen_id = raw, recognized = true,
        has_blocking_prompt = (string.sub(screen, 1, 7) == "prompt."), prompt_options = options,
    }
    if extra ~= nil then
        for k, v in pairs(extra) do result[k] = v end
    end
    return result
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
-- no dedicated orders file (e.g. era transition acknowledgement).
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
-- The request the harness completes with a real click: the control's own on-screen rectangle in
-- UI units (`GetScreenOffset` + `GetSizeVal`, the accessors diplomacyactionview.lua:1876 itself
-- uses for `UI.SetLeaderPosition`) plus the UI's own screen size, because the engine lays the UI
-- out in that space (MEASURED 2026-09-21: 1024x768 on a 1920x1200 window) and stretches it onto
-- the window -- the executor scales by the window it actually found. nil when either accessor is
-- unavailable, so the caller can fall back honestly rather than guess at a position.
local function CivSim_Screens_HostClickRequest(control)
    local okO, ox, oy = pcall(function() return control:GetScreenOffset() end)
    local okS, sw, sh = pcall(function() return control:GetSizeVal() end)
    if not (okO and type(ox) == "number" and type(oy) == "number") then return nil end
    if not (okS and type(sw) == "number" and type(sh) == "number") then return nil end
    local okU, uw, uh = pcall(function() return UIManager:GetScreenSizeVal() end)
    local uiScreen = nil
    if okU and type(uw) == "number" and type(uh) == "number" and uw > 0 and uh > 0 then
        uiScreen = { w = uw, h = uh }
    end
    return {
        ok = false, reason = "requires_host_click", mechanism = "host_click_at_control_rect",
        click = { x = ox, y = oy, w = sw, h = sh }, ui_screen = uiScreen,
    }
end

local function CivSim_Screens_AcknowledgePopup(promptType, descriptor, optionId)
    if optionId ~= CivSim_AcknowledgeOption(descriptor) then
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

    -- 1. The button a human clicks. Its callback was registered in the popup's own state, so a
    --    real click runs that popup's own OnClose/TryClose there -- queue and all.
    --    MEASURED LIVE 2026-09-21 (blocks 13-15): `control:CallCallback(Mouse.eLClick)` returns
    --    without error and fires nothing (it is not an engine API; see the diplomacy answer
    --    below), so this step now hands the harness the button's own on-screen rectangle and the
    --    executor clicks it through the host input port -- the same button, the same click, the
    --    popup's own handler, its queue consumed. One click per card, as a human does: if the
    --    next queued card appears, the verification predicate reports the popup still current
    --    and the agent acknowledges again.
    local controlPath = "/InGame/" .. stateName .. "/" .. descriptor.close_control
    local okB, button = pcall(function() return ContextPtr:LookUpControl(controlPath) end)
    if not okB or button == nil then
        result.fallback_reason = "close_control_absent"
    else
        local okBH, buttonHidden = pcall(function() return button:IsHidden() end)
        if okBH and buttonHidden == true then
            result.fallback_reason = "close_control_hidden"
        else
            local request = CivSim_Screens_HostClickRequest(button)
            if request ~= nil then
                for k, v in pairs(request) do result[k] = v end
                return result
            end
            result.fallback_reason = "close_control_rect_unreadable"
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

-- Answer an AI leader's statement with the call that statement's own button makes.
--
-- MEASURED LIVE 2026-09-21, in order:
--  * blocks 13-14, game turn 42: `control:CallCallback(Mouse.eLClick)` returned without error and
--    did nothing. `CallCallback` appears nowhere in Firaxis' shipped UI Lua (grep 2026-09-21), so
--    it is not an engine API for firing a registered callback, and nothing reachable from InGame
--    can invoke the closure `ApplyStatement` registers on the button
--    (diplomacyactionview.lua:599-602). A documented Firetuner gap (constitution, Principle II).
--  * game turn 42, 12:35 EDT, operator scripting: `DiplomacyManager.AddResponse(session,
--    localPlayer, "NEGATIVE")` answered the SAME greeting -- the leader replied and the session
--    stayed open with a new set of choices
--    (specs/002-civ-playing-harness/spikes/gameplay-2026-09-21/operator_answer_greeting.py).
--  * game turn 58 and the three goal runs after the turn-42 reload: answering by handing the
--    harness the button's rectangle for a host click changed NOTHING -- `prompt_options` was
--    byte-identical at all 16 steps of `run-d0933ca8...`. The click path has never been
--    demonstrated on a conversation button, only on the era card.
--
-- So the direct call is the primary route and the click is the fallback, not the other way round.
-- The call is chosen by the selection's own `CHOICE_*` key, read from the game's own
-- `DiplomacySelections` rows (see CIVSIM_DIPLOMACY_RESPONSE_BY_KEY above for the mapping and the
-- shipped line numbers), never invented from the label. Keys outside that small set -- the war,
-- peace, deal and demand statements -- deliberately keep the click path with the key recorded:
-- firing one of those off a label lookup is not a risk worth taking, and each has its own catalog
-- action.
--
-- One answer is not the end of it: the leader replies and the session stays open until the
-- conversation's own exit is taken (measured, block 9). `still_in_conversation` and
-- `offered_after` say so, and the action verifies on the chosen statement no longer being offered
-- rather than on the scene closing -- see `prompts.ai_diplomatic_approach` in
-- catalogs/actions/prompts.yaml.
--
-- Every result carries `path` (`add_response` / `close_session` / `host_click`) and, when it is
-- not the direct call, `path_reason` saying what stopped it. UNVERIFIED LIVE: the key lookup, the
-- POSITIVE route and the CloseSession route.
local function CivSim_Screens_AnswerDiplomaticApproach(promptType, optionId)
    local choices = CivSim_DiplomacyStatementChoices()
    if choices == nil then
        return { ok = false, reason = "not_in_conversation_mode", prompt = promptType,
                 option = optionId }
    end
    local offered, chosen = {}, nil
    for _, choice in ipairs(choices) do
        offered[#offered + 1] = choice.text
        if choice.text == optionId and chosen == nil then chosen = choice end
    end
    if chosen == nil then
        return { ok = false, reason = "option_not_offered", prompt = promptType, option = optionId,
                 offered = offered }
    end

    -- Which selection is this, in the game's own vocabulary?
    local keyByLabel = CivSim_DiplomacyKeyByLabel()
    local choiceKey = keyByLabel ~= nil and keyByLabel[optionId] or nil
    if choiceKey == nil then
        -- The table was unreadable, or the label resolved to no key or to more than one. Fall back
        -- on the one signal that stands on its own: the generic exit text, which is what the
        -- previous pass matched and is still right for the plain "Goodbye" conversations.
        local exitLabel = CivSim_DiplomacyExitLabel()
        if exitLabel ~= nil and optionId == exitLabel then
            choiceKey = CIVSIM_DIPLOMACY_EXIT_CHOICE_KEY
        end
    end
    local pathReason = nil
    if choiceKey == nil then
        pathReason = (keyByLabel == nil) and "selection_table_unreadable" or "choice_key_unresolved"
    end

    local response = choiceKey ~= nil and CIVSIM_DIPLOMACY_RESPONSE_BY_KEY[choiceKey] or nil
    local isExit = (choiceKey == CIVSIM_DIPLOMACY_EXIT_CHOICE_KEY)
    if pathReason == nil and response == nil and not isExit then
        pathReason = "choice_key_not_directly_answerable"
    end

    if pathReason == nil then
        local sessions = CivSim_DiplomacyOpenSessionIds()
        if #sessions ~= 1 then
            pathReason = (#sessions == 0) and "no_open_session_found" or "several_open_sessions"
            -- fall through to the click, recording the count
            local request = CivSim_Screens_HostClickRequest(chosen.control)
            if request ~= nil then
                request.prompt, request.option = promptType, optionId
                request.path, request.path_reason = "host_click", pathReason
                request.choice_key = choiceKey
                request.open_session_count = #sessions
                return request
            end
            return { ok = false, reason = "control_rect_unreadable", prompt = promptType,
                     option = optionId, path = "host_click", path_reason = pathReason,
                     choice_key = choiceKey, open_session_count = #sessions }
        end

        local sessionID = sessions[1]
        local okD, err
        if isExit then
            okD, err = pcall(function() DiplomacyManager.CloseSession(sessionID) end)
        else
            okD, err = pcall(function()
                DiplomacyManager.AddResponse(sessionID, Game.GetLocalPlayer(), response)
            end)
        end
        -- What the screen shows now, read back the same way the probe reads it.
        local remaining = CivSim_DiplomacyStatementChoices()
        local after = {}
        if remaining ~= nil then
            for _, choice in ipairs(remaining) do after[#after + 1] = choice.text end
        end
        return {
            ok = okD,
            path = isExit and "close_session" or "add_response",
            mechanism = isExit and "DiplomacyManager.CloseSession"
                or "DiplomacyManager.AddResponse",
            prompt = promptType, option = optionId, choice_key = choiceKey,
            response = response, session_id = sessionID,
            still_in_conversation = (remaining ~= nil and #remaining > 0),
            offered_after = after,
            error = (not okD) and tostring(err) or nil,
        }
    end

    -- The click a human makes, with the reason the direct call was not available recorded next to
    -- it: the button's own on-screen rectangle (`GetScreenOffset` + `GetSizeVal`, the accessors
    -- diplomacyactionview.lua:1876 itself uses for `UI.SetLeaderPosition`), clicked at its centre
    -- through the host's synthetic-input port.
    local request = CivSim_Screens_HostClickRequest(chosen.control)
    if request == nil then
        local okO, ox = pcall(function() return chosen.control:GetScreenOffset() end)
        return {
            ok = false, reason = "control_rect_unreadable", prompt = promptType,
            option = optionId, path = "host_click", path_reason = pathReason,
            choice_key = choiceKey,
            error = (not okO) and tostring(ox) or "GetSizeVal unavailable",
        }
    end
    request.prompt, request.option = promptType, optionId
    request.path, request.path_reason = "host_click", pathReason
    request.choice_key = choiceKey
    return request
end

-- Answer the era dedication chooser by clicking what a human clicks, ONE click per dispatch --
-- the option's own card, and then Confirm once the popup itself says Confirm is clickable. That
-- is the human's flow (pick, watch Confirm light up, confirm), and it is also the only flow this
-- harness can perform honestly: `CivSim_Screens_HostClickRequest` hands the executor one
-- rectangle, and the executor performs one click (capability/executor.py,
-- `_perform_host_click_if_requested`). Between the two dispatches the probe re-reads the popup, so
-- what decides the second click is the popup's own state, not a remembered intention here.
--
-- With more than one commemoration allowed (a Golden or Heroic age), the agent names one label per
-- step: each call selects that one and then reports `more_selections_required` with the counts and
-- the labels still available, until the popup enables Confirm. Nothing here picks a second
-- commemoration on the agent's behalf.
--
-- The fallback is the popup's own X (`CloseButton` -> `OnClose` -> `UIManager:DequeuePopup`,
-- dedicationpopup.lua:225-227, :288), and it is taken ONLY when no rectangle can be read at all,
-- because it dedicates NOTHING -- something a human may do, but never what was asked for. It is
-- recorded as itself: `mechanism`, `fallback_reason`, and `dedication_made = false`. A click
-- request is never followed by a dequeue: the popup is left standing so the next probe can see it.
local function CivSim_Screens_AnswerEraDedication(promptType, optionId)
    local ctx = CivSim_LookUp(CIVSIM_DEDICATION_STATE, nil)
    if ctx == nil then
        return { ok = false, reason = "popup_state_absent", prompt = promptType, option = optionId }
    end
    local choices = CivSim_DedicationChoices()
    if choices == nil then
        return { ok = false, reason = "popup_not_open", prompt = promptType, option = optionId }
    end

    local offered, selected, chosen = {}, {}, nil
    for _, choice in ipairs(choices) do
        offered[#offered + 1] = choice.text
        if choice.selected then selected[#selected + 1] = choice.text end
        if choice.text == optionId then chosen = choice end
    end
    if chosen == nil then
        return { ok = false, reason = "option_not_offered", prompt = promptType, option = optionId,
                 offered = offered }
    end

    local result = { prompt = promptType, option = optionId, state = CIVSIM_DEDICATION_STATE,
                     selections_made = #selected,
                     selections_allowed = CivSim_DedicationSelectionsAllowed() }

    -- 1. Not ticked yet: click its card. `OnCommemorationSelected` runs inside the popup's own
    --    state and is the only thing that can tick it (dedicationpopup.lua:156, :160-197).
    if not chosen.selected then
        local request = CivSim_Screens_HostClickRequest(chosen.control)
        if request ~= nil then
            for k, v in pairs(request) do result[k] = v end
            result.step = "select_option"
            return result
        end
        result.fallback_reason = "option_control_rect_unreadable"
    else
        -- 2. Already ticked: Confirm, but only if the popup says it is clickable. `Confirm` is
        --    disabled until exactly the allowed number are ticked (UpdateConfirmButton,
        --    dedicationpopup.lua:200-203), which is precisely the greyed-out button a human sees.
        local confirm = CivSim_LookUp(CIVSIM_DEDICATION_STATE, CIVSIM_DEDICATION_CONFIRM)
        if confirm == nil then
            result.fallback_reason = "confirm_control_absent"
        elseif not CivSim_IsVisible(confirm) then
            result.fallback_reason = "confirm_control_hidden"
        elseif CivSim_IsDisabled(confirm) then
            -- Not a failure and not a fallback: the human could not click Confirm either. Say how
            -- many more the dedication wants and what is still on the table, and stop.
            local remaining = {}
            for _, choice in ipairs(choices) do
                if not choice.selected then remaining[#remaining + 1] = choice.text end
            end
            result.ok = false
            result.reason = "more_selections_required"
            result.selected_options = selected
            result.remaining_options = remaining
            return result
        else
            local request = CivSim_Screens_HostClickRequest(confirm)
            if request ~= nil then
                for k, v in pairs(request) do result[k] = v end
                result.step = "confirm"
                result.control = CIVSIM_DEDICATION_CONFIRM
                return result
            end
            result.fallback_reason = "confirm_control_rect_unreadable"
        end
    end

    -- 3. No rectangle anywhere: the popup's own X, which dedicates nothing. Recorded as that.
    result.mechanism = "UIManager:DequeuePopup"
    result.dedication_made = false
    local okD, err = pcall(function() UIManager:DequeuePopup(ctx) end)
    local okA, hiddenAfter = pcall(function() return ctx:IsHidden() end)
    result.ok = okD
    result.hidden_after = (okA and hiddenAfter == true)
    result.error = (not okD) and tostring(err) or nil
    return result
end

-- Answer the World Congress by clicking one of its navigation buttons. Same shape as the
-- diplomatic approach, and deliberately with NO fallback: `OnAccept` submits the player's votes
-- and `OnPass` dismisses a special-session notification (worldcongresspopup.lua:2222, :2523), so
-- there is no InGame-reachable primitive that means the same thing as either. Closing the popup
-- instead would abandon the session without answering it, which is not what any of these buttons
-- does. A click that cannot be built fails, recorded, and the run stalls honestly.
local function CivSim_Screens_AnswerCongressPhase(promptType, optionId)
    local choices = CivSim_CongressPhaseChoices()
    if choices == nil then
        return { ok = false, reason = "popup_not_open", prompt = promptType, option = optionId }
    end
    local offered = {}
    for _, choice in ipairs(choices) do offered[#offered + 1] = choice.option end
    for _, choice in ipairs(choices) do
        if choice.option == optionId then
            local request = CivSim_Screens_HostClickRequest(choice.control)
            if request == nil then
                return { ok = false, reason = "control_rect_unreadable", prompt = promptType,
                         option = optionId, state = CIVSIM_CONGRESS_STATE }
            end
            request.prompt = promptType
            request.option = optionId
            request.state = CIVSIM_CONGRESS_STATE
            return request
        end
    end
    return { ok = false, reason = "option_not_offered", prompt = promptType, option = optionId,
             offered = offered }
end

local function CivSim_Screens_RespondToPrompt(promptType, optionId)
    if not CivSim_ScreenIsKnown(promptType) then
        return { ok = false, reason = "unknown_prompt" }
    end
    local descriptor = CIVSIM_ACKNOWLEDGE_ONLY_PROMPTS[promptType]
    if descriptor ~= nil then
        return CivSim_Screens_AcknowledgePopup(promptType, descriptor, optionId)
    end
    if promptType == "prompt.diplomatic_approach" then
        return CivSim_Screens_AnswerDiplomaticApproach(promptType, optionId)
    end
    if promptType == "prompt.era_dedication" then
        return CivSim_Screens_AnswerEraDedication(promptType, optionId)
    end
    if promptType == "prompt.congress_vote" then
        return CivSim_Screens_AnswerCongressPhase(promptType, optionId)
    end
    -- ACCESSOR AUDIT (2026-09-21, spikes/lua-accessor-audit-2026-09-21.md): this used to fall
    -- through to `UI.RespondToPrompt(promptType, optionId)`, a name that appears in none of
    -- Firaxis' 645 shipped Lua files and in none of the shipped binaries' UI binding tables. No
    -- such primitive can exist: a popup answers by releasing the engine hold it took
    -- (`UI.ReferenceCurrentEvent()` / `UI.ReleaseEventID()`, base/assets/ui/popupmanager.lua:52-61
    -- and :95-98) after ITS OWN button's callback has run
    -- (base/assets/ui/popups/popupdialog.lua:215-217). There is nothing generic that stands in for
    -- a specific button, so a prompt with no mapped control cannot be answered at all -- and the
    -- honest report of that is the prompt's name, not a bare `ok = false` that looks like the
    -- click was tried and refused.
    return {
        ok = false,
        reason = "prompt_has_no_mapped_control",
        prompt = promptType,
        option = optionId,
    }
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
