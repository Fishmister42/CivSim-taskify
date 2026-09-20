# T217 RESOLVED — `Network.LoadGame` works from Lua. No Firetuner gap.

**Date:** 2026-09-20 · **Host:** Linux live node · **Status:** ✅ measured, end to end

## The result

A save was loaded from a one-shot tuner command, with no UI automation, no event hunting, and no
bespoke driver. `Network.LoadGame` returned **`true`** — the first `true` this project has seen from
that call — and the client came up in the saved position.

```
=== T217 front-end load attempt: save='civsim__spike__t0001' state='MainMenu' ===
  OK    UI.IsInFrontEnd()                        = true
  OK    type(Network.LoadGame)                   = function
  table: Location=1 Type=1 Directory=0 Name=civsim__spike__t0001
  >>> Network.LoadGame ok=true ret=true
```

Far-side assertion, on a fresh connection after the phase transition — **not** the call's own return
value:

```
=== 136 states; game states: ['InGame', 'GameCore_Tuner'] ===
  OK    Game.GetCurrentGameTurn()              = 1
  OK    Game.GetLocalPlayer()                  = 0
  OK    UI.IsInFrontEnd()                      = false
  OK    leader                                 = LEADER_ELEANOR_ENGLAND
  OK    civ                                    = CIVILIZATION_ENGLAND
  OK    units                                  = 2
  OK    cities                                 = 0
  OK    gold                                   = 10
```

**This closes the keystone.** T177, T226's load, and every `resume-from` are unblocked. Option B
(bespoke UI driver) is **not needed** and should not be built. Option C (launch-with-save) is not
needed either, though the automation flags it turned up are independently useful — see below.

## Why three rounds failed, and it was not the query

`load-path-linux.md` concluded *"`Network.LoadGame` does not work from Lua"* and hypothesised that it
needs a file record produced by `UI.QuerySaveGameList`. **Both are retracted.** Two independent
mistakes, each sufficient on its own:

### 1. Wrong phase — the call is front-end only

All six `gameFile` shapes were tried from **`InGame`**. Firaxis' own shipped automation opens its
load test with the precondition, in `steamassets/base/assets/ui/automation/automation_dailysmoketest.lua:241`:

```lua
Tests["LoadGame"].Run = function()
    -- We must be at the Main Menu to do this test.
    if (not UI.IsInFrontEnd()) then
        Events.ExitToMainMenu();
        return;
    end
```

`Network.LoadGame` exists as a `function` in `InGame` and is callable there — it simply returns
`false`, which reads as "bad argument shape" and is actually "not from here." That is why `ok=true
ret=false` was so stable across six shapes: **the shapes were never the variable.**

### 2. Wrong enum names — the tables guessed at do not exist

Round 3 used `SaveFileTypes.GAME_STATE` and `SaveGameTypes.SINGLE_PLAYER`. The real table is
**`SaveTypes`**. `SaveGameTypes` is `nil`, so `SaveGameTypes.SINGLE_PLAYER` was a nil index inside a
`pcall` — silently producing a table with a missing field rather than an error.

Measured members, read by direct global reference (`_ENV` is nil in this sandbox, so an `_ENV[name]`
lookup reports "absent" for everything and says nothing):

```
SaveLocations      n=3  LOCAL_STORAGE=1, STEAM_CLOUD=2, FIRAXIS_CLOUD=3
SaveTypes          n=5  SINGLE_PLAYER=1, NETWORK_MULTIPLAYER=2, HOTSEAT=3, WORLDBUILDER_MAP=4, TILED_MAP=5
SaveDirectories    n=6  DEFAULT=0, USER=1, BENCHMARK=2, AUTOMATION=3, TUTORIAL=4, DEMO=5
ServerType         n=8  SERVER_TYPE_NONE=0, SERVER_TYPE_LAN=1, SERVER_TYPE_INTERNET=2,
                        SERVER_TYPE_STEAM=3, SERVER_TYPE_STEAM_DEDICATED=4, SERVER_TYPE_HOTSEAT=5,
                        SERVER_TYPE_FIRAXIS_CLOUD=6, SERVER_TYPE_CROSSPLAY=7
```

These enums are **absent from `Main State`** and present in `LoadGameMenu` (18), `MainMenu` (24) and
`SaveGameMenu` (19) — the per-state symbol rule again. `Main State` has `Network.LoadGame` but none
of the enums, so a probe run there resolves the table to all-nil and fails for a third reason.

## The working recipe

Run in a front-end state that has both the function and the enums — **`MainMenu`** is verified:

```lua
local loadGame = {}
loadGame.Location    = SaveLocations.LOCAL_STORAGE   -- 1
loadGame.Type        = SaveTypes.SINGLE_PLAYER       -- 1
loadGame.IsAutosave  = false
loadGame.IsQuicksave = false
loadGame.Directory   = SaveDirectories.DEFAULT       -- 0
loadGame.Name        = "civsim__spike__t0001"        -- no extension, no path
Network.LoadGame(loadGame, ServerType.SERVER_TYPE_NONE)
```

Preconditions and consequences, all measured:

- **`UI.IsInFrontEnd()` must be `true`.** From in-game, `Events.ExitToMainMenu()` first — that is
  what Firaxis' own code does, and it is the documented parity path.
- **The tuner port closes during the load** and rebinds when the game is up. Measured: refused for
  the whole load, rebound within ~5 s of the game becoming interactive. A `SaveLoader` must treat
  connection-refused during load as expected, not as a crash — this is exactly the shape the
  (unbuilt) crash detector T233 would otherwise misread.
- **`Name` is the bare save name** — no `.Civ6Save`, no directory.

### One open item, honestly flagged

The load stopped on the **leader-intro screen** (`ENGLISH EMPIRE JOINS THE WORLD STAGE`) with a
`CONTINUE GAME` button, and stayed there indefinitely — >120 s with the tuner closed. One click at
window-relative `(537, 1160)` dismissed it and the tuner rebound within 5 s.

**Not yet known:** whether that screen appears for *every* load or only for a save taken at turn 1
before the intro was dismissed. `civsim__spike__t0001` is a turn-1 save, so this is the ambiguous
case. Until a mid-game save is loaded and measured, a loader must assume **one dismissal click may
be required** — which is a single, narrowly-scoped input, not a general UI driver, and it now has a
real caller for the input layer that had none. Resolving this is cheap and is the next thing to
measure.

## The automation subsystem, found on the way

`strings` on the Aspyr binary turned up real launch flags — `-autoscript`, `-autoparams`,
`-autojson`, `-gameviewscript`, `-EnableDevFeatures`, `-TunerIP` — and behind them Civ VI's
**Automation** system (`Src/App/Scripting/AppAutomation.cpp`), with shipped scripts in
`steamassets/base/assets/ui/automation/`.

Not needed for T217 any more, but worth recording because it is a supported, parity-clean automation
surface nobody had looked at:

- **Lua API:** `Automation.GetSetParameter/SetSetParameter`, `GetStartupParameter`,
  `SetAutoStartEnabled`, `GenerateSaveName`, `GetLastGeneratedSaveName`, `SendTestComplete`.
- **Real event names**, which is what option A spent three rounds hunting: `LuaEvents.AutomationAppInitComplete`,
  `AutomationMainMenuStarted`, `AutomationPostGameInitialization`, `AutomationGameStarted`,
  `AutomationGameEnded`, `AutoPlayEnd`.
- **`AutoplayManager`** — `SetTurns`, `SetActive`, `SetObserveAsPlayer`, `SetReturnAsPlayer`.
- A second, simpler load form exists in `automation_profile.lua:256` —
  `Network.LoadGame(TEST_SAVE_NAME, ServerType.SERVER_TYPE_NONE)` with a **bare string** — called
  from `LuaEvents.AutomationMainMenuStarted`. Untested here; the table form is verified and is
  enough.

Reading Firaxis' own shipped Lua is the cheapest oracle this project has found so far. Three rounds
of guessing argument shapes were answered by a file already on disk.

## What this retracts

- ❌ *"`Network.LoadGame` does not work from Lua"* → **false**, it works from the front end.
- ❌ *"`Network.LoadGame` requires a record produced by `UI.QuerySaveGameList`"* → **unsupported**;
  a hand-built table loads fine. The query is not on the load path at all.
- ❌ *"Loading needs a Principle II `firetuner_gap` and a bespoke UI driver"* → **withdrawn**. The
  gap does not exist.
- The memory/host-facts line *"Saving works … Loading from Lua does not"* is now wrong and is
  corrected.
