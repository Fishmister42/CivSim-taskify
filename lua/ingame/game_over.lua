-- lua/ingame/game_over.lua
-- Context: InGame (read-only). Backs NO catalog declaration by design -- see the parity note
-- below. Dispatched by src/civsim_harness/run/game_over.py's LuaGameOverReader, from
-- run/turn_cycle.py's pre-save position, once per turn.
--
-- WHY THIS FILE EXISTS (MEASURED 2026-09-21, 18:50 EDT, live Linux client 1.0.12.9):
-- Persia was defeated at game turn 59 (Georgia took the only city). The harness had no game-over
-- detection, so the next turn began as any other: the FR-007 start-of-turn quicksave was
-- requested, the already-finished game never wrote one, and the run paused with
-- `SaveVerificationError("expected .Civ6Save not found ... waited 10 s")`. A fresh InGame read
-- taken afterwards showed `Players[0]:IsAlive() == false` and `EndGameMenu` as the only shown
-- screen. The run's honest terminal state is a defeat; this file is what lets the harness say so
-- before it asks a finished game for a save it will never write.
--
-- PARITY BOUNDARY (Principle I, catalogs/README.md §3.5) -- READ BEFORE EXTENDING.
-- `lua/gamecore/game_outcome.lua` (T216, declaration `game.outcome_state`) reports whether the
-- LOCAL player has won or lost and deliberately reports NOTHING about who else won or how,
-- because while the game is still being played that is opponent state a human cannot see. That
-- boundary is unchanged and still binding for that file.
--
-- This file is the other side of the same line: the moment the game is over, Firaxis's own
-- end-game screen states all of it to the human in plain text -- the victory's own name and the
-- winning civilization -- so reading those two, and ONLY once the game is already over, is
-- human-parity-correct rather than a leak. The gate is structural below: `victory_type` and
-- `winner` are computed only after `game_over` has already been established, and are absent from
-- every other result. Do NOT hoist them above that gate, and do NOT extend this file with
-- `Game.GetVictoryProgressForTeam(...)`, another player's score, or any other civ's alive/dead
-- state -- none of those is on the end-game screen.
--
-- This read is harness bookkeeping at a turn boundary, not part of the agent's observation
-- surface: it carries no `ParityDeclaration` and never reaches a decision request. What the agent
-- sees about outcomes remains exactly `game.outcome_state`.
--
-- WHAT IT MIRRORS, WITH CITATIONS. Shipped UI under the Civ VI install's `steamassets/`
-- (this host: `~/.steam/debian-installation/steamapps/common/Sid Meier's Civilization VI/`):
--   * `base/assets/ui/endgame/endgamemenu.lua:1055-1069` -- `ShowEndGame`, the shipped path that
--     answers "what should this human be shown right now": `1057 if(player:IsAlive()) then`,
--     `1058 local victor, victoryType = Game.GetWinningTeam();`, `1059 if(victor ==
--     player:GetTeam()) then`, `1060 local victory = GameInfo.Victories[victoryType];`,
--     `1062 View(TeamVictoryData(victor, victory.VictoryType))`, and the fallthrough
--     `1069 View(PlayerDefeatedData(playerId, "DEFEAT_DEFAULT"))`. A dead `IsAlive()` is exactly
--     how the shipped UI decides to tell a human they lost.
--   * `endgamemenu.lua:868-875` -- the event-driven victory test, verbatim the same comparison:
--     `868 local localPlayerID = Game.GetLocalPlayer();` `870 local localPlayerTeamID =
--     pLocalPlayer:GetTeam();` `875 data.IsWinnerLocalPlayer = winningTeamID == localPlayerTeamID;`
--   * `endgamemenu.lua:925-926` -- the victory type's human name: `925 local victory =
--     GameInfo.Victories[victoryType];` `926 data.VictoryTypeHeader = victory.Name;`, rendered at
--     `636 Controls.VictoryTypeName:SetText(...)`. Rows: `base/assets/gameplay/data/victories.xml:33-39`
--     (plus `dlc/expansion2/data/expansion2_victories.xml:7` for `VICTORY_DIPLOMATIC`).
--   * `endgamemenu.lua:947-951` -- the winner's name: `948 local winnerPlayerID =
--     Teams[winningTeamID][1];` `951 data.WinnerName =
--     Locale.Lookup(pWinnerConfig:GetCivilizationDescription());`, rendered at
--     `637 Controls.VictoryPlayerName:SetText(...)`. The multiplayer-only " (PlayerName)" suffix
--     (`952-955`) is deliberately NOT reproduced: this harness plays single player, where the
--     screen does not show it.
--   * `endgamemenu.lua:832` / `969` -- when the local player was simply eliminated with nobody
--     having won, the screen's winner panel is empty (`data.WinnerName = ""`). That is why
--     `victory_type`/`winner` stay absent on an elimination defeat here.
--   * `base/assets/ui/worldinput.lua:3444-3445` -- the shipped "is the game over" probe,
--     `return Game.GetWinningTeam() ~= nil`, which is the authority for treating a nil return as
--     "nobody has won"; `base/assets/ui/menus/ingametopoptionsmenu.lua:357-359` tests it the same
--     way. `Game.GetWinningPlayer()` does NOT exist in the shipped UI (zero call sites anywhere
--     under `steamassets/`), so it is not used here.
--   * `base/assets/ui/menus/hotseatbackground.lua:12` --
--     `ContextPtr:LookUpControl("/InGame/EndGameMenu")` is how shipped code reaches the end-game
--     screen's own context; it is declared `base/assets/ui/ingame.xml:120` as
--     `<LuaContext ID="EndGameMenu" FileName="EndGameMenu" Hidden="1"/>` and queued at
--     `endgamemenu.lua:755 UIManager:QueuePopup(ContextPtr, PopupPriority.EndGameMenu)`.
--     Reported here as `end_game_screen_shown`, CORROBORATION ONLY -- never a gate. A defeat whose
--     popup failed to queue (the tutorial ruleset never subscribes at all:
--     `endgamemenu.lua:1327-1330`) is still a defeat, and gating on the screen would silently miss
--     it.
--
-- Expansion overrides change none of this: `dlc/expansion1/ui/replacements/
-- endgamemenu_expansion1.lua` and `dlc/expansion2/ui/replacements/endgamemenu_expansion2.lua`
-- only add the Historic Moments/Export buttons and a `Styles["VICTORY_DIPLOMATIC"]` entry.
--
-- SANDBOX CONSTRAINT (specs/002-civ-playing-harness/spikes/lua-api-verification-linux.md, P5):
-- neither tuner context exposes `require`, `io`, or `debug`, and no JSON library exists in either.
-- This file must stay entirely self-contained and carries its own JSON encoder.
--
-- UNVERIFIED LIVE: every accessor below is individually pcall'd and an unreadable value is simply
-- absent from the result -- never a fabricated outcome. `Players[id]:IsAlive()` was observed false
-- on the defeated player at 18:50 EDT; `Game.GetWinningTeam`, `GameInfo.Victories`, `Teams`,
-- `PlayerConfigurations:GetCivilizationDescription` and `Locale.Lookup` are the shipped screen's
-- own calls but have not been exercised from the InGame tuner state on a finished game. The Python
-- side treats any unreadable or unnameable result as "no game over" and plays the turn as usual.

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

-- A displayable string, or nil. `Locale.Lookup` is applied exactly where the shipped screen
-- applies it (endgamemenu.lua:951 for the winner; the victory header is stored as its own LOC key
-- at :926 and resolved by the label, so it is resolved here too -- a raw `LOC_...` key is what a
-- reader of the record must never be handed as "what the human saw"). A lookup that fails falls
-- back to the key itself rather than to nothing: an untranslated name is still a name.
local function CivSim_GameOver_Localized(key)
    if type(key) ~= "string" or key == "" then return nil end
    local ok, text = pcall(function() return Locale.Lookup(key) end)
    if ok and type(text) == "string" and text ~= "" then return text end
    return key
end

-- endgamemenu.lua:925-926. `victoryType` is `Game.GetWinningTeam()`'s SECOND return value (:1058),
-- an index into `GameInfo.Victories`; the human-visible name is that row's `Name`.
local function CivSim_GameOver_VictoryName(victoryType)
    if victoryType == nil then return nil end
    local ok, row = pcall(function() return GameInfo.Victories[victoryType] end)
    if not ok or row == nil then return nil end
    local okName, name = pcall(function() return row.Name end)
    if not okName then return nil end
    return CivSim_GameOver_Localized(name)
end

-- endgamemenu.lua:948-951. The winning team's first member is the civilization the victory panel
-- names. Single player only, by construction: the " (PlayerName)" suffix at :952-955 is
-- multiplayer-only and is not reproduced.
local function CivSim_GameOver_WinnerName(winningTeam)
    local okTeam, members = pcall(function() return Teams[winningTeam] end)
    if not okTeam or members == nil then return nil end
    local okId, playerId = pcall(function() return members[1] end)
    if not okId or playerId == nil then return nil end
    local okCfg, config = pcall(function() return PlayerConfigurations[playerId] end)
    if not okCfg or config == nil then return nil end
    local okDesc, description = pcall(function() return config:GetCivilizationDescription() end)
    if not okDesc then return nil end
    return CivSim_GameOver_Localized(description)
end

-- hotseatbackground.lua:12-14 resolves the end-game screen's own context by name and asks the UI
-- manager about it; the harness's own established equivalent (lua/ingame/screens.lua) is
-- `ContextPtr:LookUpControl("/InGame/<State>")` plus `IsHidden()`, which is what is used here so
-- the two agree about what "shown" means. Reported, never decisive -- see the header.
local function CivSim_GameOver_EndScreenShown()
    local okCtx, context = pcall(function()
        return ContextPtr:LookUpControl("/InGame/EndGameMenu")
    end)
    if not okCtx or context == nil then return nil end
    local okHidden, hidden = pcall(function() return context:IsHidden() end)
    if not okHidden or type(hidden) ~= "boolean" then return nil end
    return hidden == false
end

local function CivSim_GameOver_Read()
    local result = { game_over = false }

    local localPlayer = nil
    local okLocal, lp = pcall(function() return Game.GetLocalPlayer() end)
    if okLocal then localPlayer = lp end
    if type(localPlayer) ~= "number" or localPlayer < 0 then
        -- No local player (an observer slot, or the game is not in a playable state). Reported
        -- honestly rather than guessed at; the Python side plays the turn as usual.
        result.reason = "local player is not resolvable from this state"
        return result
    end

    result.end_game_screen_shown = CivSim_GameOver_EndScreenShown()

    local alive = nil
    local okAlive, isAlive = pcall(function() return Players[localPlayer]:IsAlive() end)
    if okAlive and type(isAlive) == "boolean" then alive = isAlive end
    result.local_player_alive = alive

    local localTeam = nil
    local okTeam, team = pcall(function() return Players[localPlayer]:GetTeam() end)
    if okTeam then localTeam = team end

    -- Both return values (endgamemenu.lua:1058). A nil winning team is "nobody has won yet"
    -- (worldinput.lua:3444); the `>= 0` guard additionally covers a negative-id sentinel, so the
    -- only way this misreads is a NON-NEGATIVE sentinel for "no winner", which would misreport a
    -- live game as over. If a live client ever shows one, correct this guard.
    local winningTeam, victoryType = nil, nil
    local okWin, winner, winningVictory = pcall(function() return Game.GetWinningTeam() end)
    if okWin then
        winningTeam = winner
        victoryType = winningVictory
    end
    local haveWinner = (type(winningTeam) == "number" and winningTeam >= 0)

    if alive == false then
        -- endgamemenu.lua:1057/1069: not alive -> the human is shown the defeat screen, whatever
        -- else is true. This is the 2026-09-21 case exactly.
        result.game_over = true
        result.outcome = "defeat"
        result.basis = "local_player_not_alive"
    elseif haveWinner then
        result.game_over = true
        if localTeam ~= nil and winningTeam == localTeam then
            result.outcome = "victory"
            result.basis = "winning_team_is_local_team"
        else
            result.outcome = "defeat"
            result.basis = "winning_team_is_another_team"
        end
    else
        -- Alive, nobody has won: the game is still being played. Nothing below this line runs,
        -- which is the structural half of the parity gate in the header.
        return result
    end

    if haveWinner then
        result.victory_type = CivSim_GameOver_VictoryName(victoryType)
        result.winner = CivSim_GameOver_WinnerName(winningTeam)
    end
    return result
end

CivSim_GameOver = {
    read = CivSim_GameOver_Read,
}

-- Example dispatch (performed by run/game_over.py's LuaGameOverReader, not by this file):
--   print(CivSim_JsonEncode(CivSim_GameOver.read()))
