# `CivSim DEFAULT` — where the preset lives and what is in it

**Feature**: `002-civ-playing-harness` · **Executed**: 2026-09-20 · **Host**: Linux (native Aspyr)

Answers the on-disk question fully and the parameter question partially. **The authoritative
read-back has not been done** — see [What is still needed](#what-is-still-needed).

## Where it is stored

```
~/.local/share/aspyr-media/Sid Meier's Civilization VI/Saves/Single/CivSim DEFAULT.Civ6Cfg
```

**Game setup configurations live in the same directory as saves**, with a `.Civ6Cfg` extension. That
is worth stating explicitly because it has a direct consequence for retention:

> ⚠️ **`Saves/Single/` is not exclusively save files.** Any reaper, retention sweep, or cleanup that
> globs that directory must exclude `.Civ6Cfg`, or it will delete the run configuration the seed set
> depends on. FR-036 already forbids deleting saves to make room; this is a second class of file in
> the same directory that must never be collected.

Sibling files confirm the pattern is the game's own, not a one-off:

```
Saves/Single/CivSim DEFAULT.Civ6Cfg
Saves/Single/auto/AutoConfigGame_01.Civ6Cfg
Saves/Single/auto/prev/AutoConfigGame_01.Civ6Cfg
```

## Format

`file` reports it as *"Sid Meier's Civilization VI saved game"* — **`.Civ6Cfg` uses the same `CIV6`
container as `.Civ6Save`**, with a `789c` zlib section following a plaintext header.

Readable header fields, without decompression:

```
CIV6
LINUX                       <-- platform, in the file
1.0.12.9 (363760)           <-- build, in the file
```

**The preset records the platform and build it was authored on.** That is directly useful: it is an
independent cross-check for the seed set's platform+build pin (FR-002, R20) that does not depend on
asking the running client. Same property already noted for saves via `Saved By Version`.

## Parameters — read back authoritatively

The preset was loaded through the UI (**Single Player → Create Game → Advanced Setup → Load
Configuration → `CivSim DEFAULT`**) and every field read back through `GameConfiguration` from the
`HostGame` Lua state. **These are observed values, not inferences from the file.**

| Field | Value | Raw |
|---|---|---|
| Ruleset | `RULESET_EXPANSION_2` (Gathering Storm) | string |
| **Turn timer** | **`TURNTIMER_NONE`** ✅ | `-1525060181` |
| Difficulty | **`DIFFICULTY_EMPEROR`** | `1499830429` |
| Game speed | **`GAMESPEED_ONLINE`** | `-1649545904` |
| Start era | `ERA_ANCIENT` | `-1851407529` |
| Map script | **`Pangaea.lua`** | string |
| Map size | Small | `-1837222328` |
| Max turns | `0` (no limit) | |
| AI players | **6** | |
| Participating players | 6 | |
| Multiplayer | `false` | |

Advanced Setup additionally shows, for fields without a `GameConfiguration` getter probed here:
**Smart-Timer `Off`**, City-States **9**, Disaster Intensity **2**, Resources **Abundant**, Strategic
Resources **Abundant**, Natural Wonders Density Standard, Leader Pool 1 & 2 *Everything*,
BCY affected City Centers `OFF`, BCY City Center yields `Balanced`, Settler Capture `Capture`.

### ⚠️ The fixed leader is NOT set in the preset

The owner described the preset as having *"a fixed leader under harness control."* **It does not, as
loaded.** Every player slot reads back empty:

```
player[1] id=0 civ=nil leader=nil human=false
player[2] id=1 civ=nil leader=nil human=false
...   (6 slots, all nil)
```

and the Create Game UI shows **`Random Leader`** in every slot after loading the configuration. The
`LEADER_CYRUS` / `CIVILIZATION_PERSIA` strings visible in the `.Civ6Cfg` plaintext are part of the
**available-options roster**, which also contains every other leader in the installation — reading
them as the selection was my error, and the read-back corrects it.

`HumanPlayerCount = 0` likewise, so the human slot is not configured at setup time either.

### ✅ Resolved — preparation sets the leader itself, and the owner has chosen Cyrus

The owner confirmed the configuration will not hold a civ selection, and **ruled that runs start with
`LEADER_CYRUS` (Persia)**. That decision is now fixed.

**The preset does not need to carry it.** `PlayerConfigurations` exposes working setters at the
`HostGame` state, so preparation can pin the leader itself after loading the configuration:

```lua
local pc = PlayerConfigurations[0]
pc:SetLeaderTypeName("LEADER_CYRUS")
pc:SetCivilizationTypeName("CIVILIZATION_PERSIA")
```

Verified live, including the read-back V3 requires:

```
before:  slot0 leader=nil          civ=nil
set:     SetLeaderTypeName ok=true   SetCivilizationTypeName ok=true
after:   slot0 leader=LEADER_CYRUS civ=CIVILIZATION_PERSIA
```

**The game honours it**: the Create Game UI refreshed to show **Cyrus** with the Persia icon in
slot 1. Note the UI does *not* repaint immediately on the Lua write — it still read `Random Leader`
right after the call and updated shortly after. **Do not treat the UI as the verification signal;
read the value back through `PlayerConfigurations`**, which is the FR-002 / V2 pattern anyway.

Related setters confirmed present on `GameConfiguration` at the same state:
`RemovePlayer` (function, returns `true`), `SetParticipatingPlayerCount`, `GetAIPlayerIDs`.
`SetAIPlayerCount` does **not** exist — player count is changed by adding/removing players, not by
setting a count.

So the full preparation path is reachable without UI automation beyond loading the configuration:
load preset → set leader/civ → read back and compare field by field → start.

`GAMESPEED_ONLINE` is the fastest speed in the game and is a deliberate-looking choice for an
experimentation harness — turns resolve in far fewer game-years. It is **not** the `GAMESPEED_STANDARD`
that a `Play Now` game uses, which matters: any yardstick calibrated on standard speed (including the
"100 science / 100 culture by turn 50" goal in the constitution) means something different here.

The file also contains the full roster of every leader and mod in the installation — that is the
*available options* list, not the selection, so leader names other than Cyrus in a `strings` dump
should not be mistaken for configuration.

## What is still needed

*(Items 1–4 of the original draft asked questions the read-back above has since answered — the
authoritative read-back is done, the turn timer resolves to `TURNTIMER_NONE`, difficulty/map
size/opponent count are recovered, and the preset was loaded through the ordinary Create Game menu,
which settles the Principle I parity basis by observation rather than reasoning. They are removed
rather than left contradicting the table above.)*

1. **🔴 Does the leader survive the transition into the game?** The pinning above is confirmed **at
   the setup screen only**. Whether map generation reassigns it has not been tested, and `Play Now`
   randomising the leader proves a reassignment step exists somewhere on that path. The failure mode
   is nasty: the write sticks, V2's read-back passes at preparation time, and the run then plays as
   a different leader. **Do not treat the leader as pinned until this is confirmed in-game.**
2. **Which build number is authoritative.** The client reports `1.0.12.9 (564030)` at the main menu
   but `1.0.12.9 (363760)` in-game, in `Saved By Version`, and in this `.Civ6Cfg` header. The
   composite build pin needs one of them named; `363760` is the one embedded in artifacts, but this
   is not yet measured through a declared observation.
3. **Victory conditions** — not recovered from the plaintext regions and not exposed by the getters
   probed so far.

## Note on `Play Now`

`Play Now` **randomises the leader** — England/Eleanor, Korea/Seondeok, and Mali/Mansa Musa across
three consecutive launches on this host. It is unusable for seeded work, which is precisely why the
fixed-leader preset exists. No run should be prepared through it.
