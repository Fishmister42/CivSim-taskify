-- lua/ingame/save_game.lua
-- Context: InGame (write/act). Backs declaration_id: saves.save_game
-- (catalogs/actions/saves.yaml), capability_id: saves.save_game.
--
-- VERIFIED against a real Civilization VI client (T077 / research R5 spike;
-- specs/002-civ-playing-harness/spikes/r5-save-path.md). `Network`, `UI`, and
-- `UIManager` are entirely absent from `GameCore_Tuner` -- every save
-- candidate errors at the root there -- so this call must run in `InGame`;
-- declaring it under `GameCore_Tuner` is a catalog-load error by design
-- (research R3). `SaveLocations.LOCAL_STORAGE` and `SaveTypes.SINGLE_PLAYER`
-- both evaluate to `1` on a live client and are passed through their named
-- constants rather than the bare literal. An arbitrary `Name` is honoured
-- verbatim as the filename stem, confirmed live with
-- `civsim__<run_id>__t<turn:04d>`-shaped names.
--
-- `Network.SaveGame` returns `true` immediately, but the file is written to
-- disk *asynchronously* (confirmed by the spike) -- this file's own return
-- value only reports that the call was issued without a Lua-side error, and
-- must never be read as proof the save exists. Filesystem verification
-- (`saves/verify.py`, T079: the `.Civ6Save` present with a size stable
-- across two reads) is the real confirmation and happens outside Lua
-- entirely, after this call returns.
--
-- Parity note: this is the Esc -> Save Game -> type a name -> Save flow a
-- human takes from the in-game menu, reaching the same operation that menu
-- does and nothing else. It does not decide whether saving now is otherwise
-- sensible -- that is what saves.save_game's availability_predicate is for.

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

-- Issue a named save through the verified FireTuner path. `saveName` is the
-- caller-supplied filename stem (no extension, no directory) -- typically
-- `civsim__<run_id>__t<turn:04d>` (saves/save_point.py's save_name_for).
local function CivSim_Saves_SaveGame(saveName)
    local gameFile = {}
    gameFile.Name = saveName
    gameFile.Location = SaveLocations.LOCAL_STORAGE
    gameFile.Type = SaveTypes.SINGLE_PLAYER
    gameFile.IsAutosave = false
    gameFile.IsQuicksave = false

    local ok, err = pcall(function() Network.SaveGame(gameFile) end)
    -- `(ok and "") or tostring(err)` rather than `ok and nil or tostring(err)`:
    -- the latter is the classic Lua and/or ternary trap -- `nil` is itself
    -- falsy, so that form would always fall through to `tostring(err)`
    -- regardless of `ok`. `""` is truthy, so this reports correctly.
    return { ["issued"] = ok, ["save_name"] = saveName, ["error"] = (ok and "") or tostring(err) }
end

CivSim_Saves = {
    save_game = CivSim_Saves_SaveGame,
}

-- Example dispatch (performed by the Nexus dispatcher, not by this file):
--   print(CivSim_JsonEncode(CivSim_Saves.save_game("civsim__abc123__t0007")))
