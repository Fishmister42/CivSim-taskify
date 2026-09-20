# T218 RESULTS — the five unverified `_SETTING_GETTERS`, measured in-game

**Date:** 2026-09-20 · **Host:** Linux live node · **Phase measured:** in-game (`InGame`, idx 132),
on a loaded save (`civsim__spike__t0001`), 136 Lua states.

**Read the values as evidence that a getter returns something, not as the preset's settings.** The
client was in the loaded spike save, not the `CivSim DEFAULT` preset, so the *values* below describe
that save. What T218 needed was whether each getter resolves at all — that is what is settled here.

## Verdict per getter

| setting | getter | verdict |
|---|---|---|
| `map_seed` | `GameConfiguration.GetValue("GAME_SYNC_RANDOM_SEED")` | ✅ `-986870912` |
| `map_type` | `MapConfiguration.GetScript()` | ✅ `"Continents.lua"` (string, no hash) |
| `map_size` | `MapConfiguration.GetValue("MAP_SIZE")` | ✅ hash, resolves both ways |
| `resources` | — | ❌ **no getter found** |
| `major_count` | `GameConfiguration.GetAIPlayerCount()` | ⚠️ **returns the wrong thing in-game** |
| `mod_set` | `Modding.GetActiveMods()` | ✅ 22 mods, id + name |

Raw output is in `t218_setting_getters.lua` / `t218_major_count.lua` runs; the decisive lines follow.

## 🔴 `major_count` — the same getter means two different things by phase

This is the finding that matters, and it is a silent-wrong-answer, not a failure.

```
  OK    GameConfiguration.GetAIPlayerCount()             = 16
  OK    GameConfiguration.GetParticipatingPlayerCount()  = 17
  OK    GameConfiguration.GetHumanPlayerCount()          = 1
  OK    GameConfiguration.GetValue('CITY_STATE_COUNT')   = 9
  GetAIPlayerIDs()  n=16  ids=[1,2,3,4,5,6,7,8,9,10,11,12,13,14,62,63]
```

Per-player identity makes the decomposition unambiguous — nothing here is inferred:

```
    id=0   major=true  barb=false leader=LEADER_ELEANOR_ENGLAND     <- the human
    id=1..5   major=true                                            <- 5 AI majors
    id=6..14  major=false            LEADER_MINOR_CIV_*             <- 9 city-states
    id=62     major=false            LEADER_FREE_CITIES
    id=63     major=false barb=true                                 <- barbarians
```

**16 = 5 AI majors + 9 city-states + Free Cities + Barbarians.** `GetAIPlayerCount()` in-game counts
every non-human player, not major opponents. At the Create Game screen the same call returned **6**
for the `CivSim DEFAULT` preset (`preset_readback.txt`), where it *did* mean major AI count, because
city-states are not instantiated as players until the game starts.

So a preparation step that records `major_count` at setup and a verification that re-reads it
in-game compare **6 against 16** and disagree forever — while every unit test against a fake passes,
because a fake returns one number for both phases. This is the `window=None` shape again: the
mismatch only exists where the two phases meet, which is exactly where no test looks.

**Correct in-game derivation** (measured: yields 6 total majors, 5 excluding the human):

```lua
local majors = 0
for _, player in ipairs(Players) do
  if player:IsAlive() and player:IsMajor() then majors = majors + 1 end
end
-- opponents.major_count = majors - 1   (exclude the local player)
```

`CITY_STATE_COUNT` is separately available in-game as `GameConfiguration.GetValue("CITY_STATE_COUNT")`
= 9. Note `MapConfiguration.GetValue("CITY_STATE_COUNT")` is **nil** — the non-uniformity the probe
was written to expect, confirmed again.

## `map_seed` — available, and there are *two* seeds

```
  OK    GameConfiguration.GetValue('GAME_SYNC_RANDOM_SEED') = -986870912
  nil   GameConfiguration.GetValue('GAME_RANDOM_SEED')
  nil   GameConfiguration.GetValue('RANDOM_SEED')
  OK    MapConfiguration.GetValue('RANDOM_SEED')            = -986870911
  nil   MapConfiguration.GetValue('MAP_SEED')
  ERR   GameConfiguration.GetRandomSeed()                   <- does not exist
```

The game seed and the map seed are **different values, one apart** (`-986870912` vs `-986870911`).
Recording only one loses the other; they are not interchangeable. `GAME_SYNC_RANDOM_SEED` is also
the key Firaxis' own automation *writes* when it fixes a seed
(`automation_standardtests.lua:207`), which makes it the right one for `map_seed`'s game half.

## `map_size` — a hash, and it resolves through `GameInfo.Maps`, not `GameInfo.MapSizes`

```
  OK    MapConfiguration.GetValue('MAP_SIZE')  = -1837222328
  ->    resolves to nil          via GameInfo.MapSizes
  ->    resolves to MAPSIZE_SMALL via GameInfo.Maps
  forward check: DB.MakeHash('MAPSIZE_SMALL')  = -1837222328
```

Both directions agree, so the value is pinned. **The obvious table is the wrong one** — scanning
`GameInfo.MapSizes` returns nil; the row lives in `GameInfo.Maps`. Prefer the forward check
(`DB.MakeHash`) for assertions, as the established rule says.

## `resources` — a real negative result

All three candidates return nil in-game:

```
  nil   MapConfiguration.GetValue('RESOURCES')
  nil   MapConfiguration.GetValue('RESOURCE_DENSITY')
  nil   GameConfiguration.GetValue('RESOURCES')
```

No getter for resource density was found. This getter should be **removed or explicitly recorded as
unavailable** rather than left returning `UnreadSetting` — an unread setting that compares unequal to
everything lands a run `failed` before turn 1, which is the exact failure homebase ranked #1. A
documented "not observable" is a complete answer; a silent sentinel is not.

Worth one more look at the Create Game screen before calling it closed: resource density is a
*setup* option, so it may exist as a `HostGame`-phase key that is simply not retained in-game — the
same phase-dependence `major_count` just demonstrated.

## `mod_set` — works, and the ids are stable

`Modding.GetActiveMods()` returns 22 entries with `Id` (GUID) and `Name`. `Modding.GetEnabledMods()`
**errors** — it does not exist; use `GetActiveMods`. Names are partly unlocalised
(`LOC_EXPANSION2_MOD_TITLE`), so **key `mod_set` on `Id`, not `Name`** — the GUIDs are stable where
the display names depend on localisation state.

## Still to measure

- The same six getters at the **Create Game screen** (`HostGame`), which is the phase preparation
  actually runs in. `major_count` is now known to differ by phase; the others must not be assumed
  phase-stable just because they resolved here.
- Whether `resources` exists at setup.
