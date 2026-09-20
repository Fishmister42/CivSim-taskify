# R8 — a verified programmatic new-game start, and the pinned leader DOES survive

> **RESOLVED — see "The complete sequence" below.** The question this spike opened is answered: a
> leader pinned before the start **survives map generation and plays the game**. The missing piece
> was a human player slot. The rest of this document records how it was established, including one
> claim of mine that turned out to be wrong.

## ✅ The complete sequence, verified end to end

```
  OK    SlotStatus.SS_TAKEN                      = 3
  OK    SetToDefaults                            = done
  OK    LoadGame(GAME_CONFIGURATION)             = true
  OK    humanCount BEFORE slot assign            = 0
  OK    SetSlotStatus(0, SS_TAKEN)               = done
  OK    humanCount AFTER slot assign             = 1      <- the missing piece
  OK    leader (setup)                           = LEADER_CYRUS
  OK    civ    (setup)                           = CIVILIZATION_PERSIA
  OK    turnTimer                                = -1525060181  (TURNTIMER_NONE)
  HostGame ok=true ret=1
  ...game starts, 1 Escape press dismisses the intro...
  OK    leader (in-game)       = LEADER_CYRUS
  OK    civ    (in-game)       = CIVILIZATION_PERSIA
  OK    turn                   = 1
  OK    turnTimer (in-game)    = -1525060181     (TURNTIMER_NONE)
  PINNED LEADER SURVIVED = true
  PINNED CIV SURVIVED    = true
```

```lua
GameConfiguration.SetToDefaults()
Network.LoadGame({ Location = SaveLocations.LOCAL_STORAGE,
                   Type = SaveTypes.SINGLE_PLAYER,
                   FileType = SaveFileTypes.GAME_CONFIGURATION,
                   IsAutosave = false, IsQuicksave = false,
                   Directory = SaveDirectories.DEFAULT,
                   Name = "CivSim DEFAULT" }, ServerType.SERVER_TYPE_NONE)
PlayerConfigurations[0]:SetSlotStatus(SlotStatus.SS_TAKEN)      -- SS_TAKEN = 3, human
PlayerConfigurations[0]:SetLeaderTypeName("LEADER_CYRUS")
PlayerConfigurations[0]:SetCivilizationTypeName("CIVILIZATION_PERSIA")
Network.HostGame(ServerType.SERVER_TYPE_NONE)
-- then: Escape until the tuner rebinds (the intro screen, as for a load)
```

**What this settles:**

- **The pinned leader survives.** `Play Now` randomising the leader does not generalise to the
  `HostGame` path — read back in-game, the run plays as the pinned leader. V2's read-back at
  preparation time is therefore meaningful, not a check on a value that is about to be replaced.
- **`TURNTIMER_NONE` survives into the game too.** Read in-game, not just at setup. The turn-timer
  blocker is closed on this path.
- **A run can be started from nothing but Lua** — no UI, no `Play Now`, no hand-made save.

## ❌ Correction: `HostGame` returning `1` is NOT an error, and I said it was

This spike previously stated that `Network.HostGame(SERVER_TYPE_NONE)` *"returns `1` and the game
does not start"*, treating `1` as a refusal code. **That was wrong.**

The successful run above **also** returned `ret=1`. The return value is identical whether the game
starts or not — so it carries **no information at all** about the outcome. The real difference was
`GetHumanPlayerCount()`: 0 in the failing runs, 1 in the successful one.

This is the project's own boundary rule catching me: I read a return value and inferred an outcome
from it, exactly the thing that has burned this codebase repeatedly. **Never branch on `HostGame`'s
return.** Assert on the far side — the game states appearing, `UI.IsInFrontEnd()` going false.

## 🔴 `Network.HostGame` appears nowhere in `src/`

**Date:** 2026-09-20 · **Host:** Linux live node · Client `1.0.12.9 (564030)`, native Aspyr.

The queue item was *"does the leader pinned at setup survive into the running game?"* — the concern
being that `Play Now` randomises the leader, which proves a reassignment step exists on some path, so
a pin that verifies at preparation time could still play the run as someone else.

**The question is still open, and the reason is the finding.**

## 🔴 `Network.HostGame` appears nowhere in `src/`

```
$ grep -rn 'Network.HostGame' src/
(no matches)
```

The harness has a verified path to **resume an existing save** (`saves/load_game.py`,
`Network.LoadGame`) and a verified path to **pin a leader** into the `HostGame` Lua state
(`run/preparation.py`). It has **no path that starts a new game.**

So preparation writes `PlayerConfigurations[0]:SetLeaderTypeName(...)` into a configuration that
**nothing ever hosts**. The write is real, the read-back is real, and the game it configures is never
begun. This is the same family as the four already found — a step that works perfectly and runs on
nothing — landing on the most fundamental step in the run lifecycle.

Every run today must therefore begin from a save that already exists, produced by hand. That may be
an acceptable interim design, but it is not what the preparation code reads as if it does, and the
pinned-leader verification is currently verifying a configuration with no consumer.

**Reported, not fixed** — `run/preparation.py` and the composition root are outside this node's
owned paths.

## What a new-game start actually requires, from Firaxis' own code

`automation_standardtests.lua:270-309` is the reference sequence, and it is more than one call:

```lua
if (not UI.IsInFrontEnd()) then Events.ExitToMainMenu(); return; end
GameConfiguration.SetToDefaults();
-- optional: load a saved setup configuration (see below)
ApplyCommonNewGameParametersToConfiguration();
ReadUserConfigOptions();
Network.HostGame(ServerType.SERVER_TYPE_NONE);
```

Measured here: calling `Network.HostGame(ServerType.SERVER_TYPE_NONE)` **without** that preamble
returns **`1`** and the game does not start — the client stays in the front end.

```
  SetLeaderTypeName ok       = true
  SetCivilizationTypeName ok = true
  readback leader (setup)    = LEADER_CYRUS
  readback civ    (setup)    = CIVILIZATION_PERSIA
  HostGame ok=true ret=1        <- accepted, did nothing
```

**Likely cause, not asserted:** the front-end configuration has **no human player**. The T218
front-end run measured `GameConfiguration.GetHumanPlayerCount()` = **0** (against 1 in-game), and
`ApplyCommonNewGameParametersToConfiguration()` is exactly the helper that assigns human/AI slots
before hosting. Confirming that is the next cheap step.

### ✅ VERIFIED — the `CivSim DEFAULT` preset applies from Lua, no UI

`spikes/t213_preset_via_lua.lua`, run in `HostGame` (14). The discriminator exists and has exactly
two members:

```
  SaveFileTypes members:  GAME_CONFIGURATION=1, GAME_STATE=0
  Network.LoadGame(GAME_CONFIGURATION)  = true
```

The configuration measurably changed, before → after, and every field matches the preset's documented
contents:

| field | before | after | resolves to |
|---|---|---|---|
| `GetRuleSet()` | `RULESET_STANDARD` | **`RULESET_EXPANSION_2`** | Gathering Storm |
| `GetGameSpeedType()` | `327976177` | **`-1649545904`** | `GAMESPEED_ONLINE` |
| `MapConfiguration.GetScript()` | `Continents.lua` | **`Pangaea.lua`** | Pangaea |
| `MAP_SIZE` | `-1837222328` | `-1837222328` | `MAPSIZE_SMALL` |
| `GetTurnTimerType()` | `-1525060181` | `-1525060181` | **`TURNTIMER_NONE`** |
| `GetAIPlayerCount()` | `6` | `6` | 6 AI |

Hashes confirmed forward in the same read — `DB.MakeHash("GAMESPEED_ONLINE") = -1649545904`,
`DB.MakeHash("TURNTIMER_NONE") = -1525060181`, `DB.MakeHash("MAPSIZE_SMALL") = -1837222328`.

**Consequences:**

- **Preparation no longer needs the UI to apply a preset.** Ruleset, map script, size, speed, AI
  count and turn timer all arrive from one Lua call.
- **`TURNTIMER_NONE` is confirmed as the loaded value.** The turn-timer blocker — `TURNTIMER_STANDARD`
  silently auto-advancing turns and destroying FR-011 verification — is settled by this path without
  going near Advanced Setup or `Play Now`.
- **The preset does not assign a human player.** `GetHumanPlayerCount()` is **0** before *and* after,
  which is consistent with the preset leaving all slots `Random Leader`. That is almost certainly why
  `Network.HostGame` refuses with `1`, and it is the one remaining piece before a game can be started
  programmatically.

### The original note on this (kept for the record)

`automation_standardtests.lua:283-298` loads a *setup configuration* through `Network.LoadGame` with
a file-type discriminator:

```lua
loadParams.FileType = SaveFileTypes.GAME_CONFIGURATION;
loadParams.Name     = configurationFile;
Network.LoadGame(loadParams, ServerType.SERVER_TYPE_NONE);
```

This matters because the project currently treats `CivSim DEFAULT.Civ6Cfg` as something only the UI
can apply. If this works, preparation can apply the whole preset — ruleset, map, speed, AI count,
turn-timer — **from Lua**, then pin the leader on top, then host. Not yet verified here; it is the
single highest-value thing to try next on this question.

## 🟡 Front-end Lua states load lazily — 2 at a fresh main menu, 29 once the menus initialise

A freshly launched client at the main menu reports **2** states (`Main State`,
`DebugHotloadCache`). Seconds later it reports 3, then eventually the full **29** including
`HostGame` (14), `MainMenu` (24), `LoadGameMenu` (18).

The first run of this probe failed for exactly this reason — `HostGame` was absent when the sequence
started, so `SetToDefaults()` and the configuration load were skipped, and only the later steps ran.

**Anything that resolves `HostGame` must wait for it to exist, not merely re-resolve it by name.**
`preparation.py` already re-resolves by name on every call, which is right, but a resolver that runs
too early gets a table that legitimately does not contain the state yet. That is a *timing* gap, not
a caching gap, and the existing comment addresses only the caching one.

## 🔴 A refused `HostGame` leaves the client alive with a permanently dead tuner

After `HostGame` returned `1`, the client sat at the main menu — window present, process alive,
screenshot clean — and the tuner port **never rebound**. Measured: still closed after **90 s** of
polling, with no recovery. The client had to be restarted.

```
Civ6 process : alive (pid 923954)
window       : present, main menu, renders normally
tuner 4318   : closed, permanently
```

**This is a hang signature, and it is precisely what T233's detection layer has to catch.** Every
naive liveness check passes: the process is alive, the window exists and paints. Only the tuner is
gone, and it never comes back. A detector keyed on process-or-window liveness will report this client
as healthy forever.

It is also distinct from the *expected* mid-load port closure the loader already tolerates — same
observable, opposite meaning. Distinguishing them needs a phase expectation: port closed **during a
load** is normal and self-resolving; port closed **at the front end with no load in flight** is a
dead client.
