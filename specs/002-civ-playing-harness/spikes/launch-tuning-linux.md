# Launch tuning — Linux / Aspyr

**Feature**: `002-civ-playing-harness` · **Executed**: 2026-09-20 · **Host**: Linux (native Aspyr)
**Client**: `1.0.12.9 (564030)`, Gathering Storm, BBG active · **Session**: X11 / Cinnamon

**Linux/Aspyr-specific.** The Windows build may behave differently and none of this should be
assumed to carry over — same per-platform rule as T099.

## Cold-launch timing — the headline is counterintuitive

Two signals were measured, because they are **not the same moment** and the difference is what
matters for the FR-045 recovery budget:

- `t_port` — TCP 4318 accepts a connection (the process is up)
- `t_lsq` — the tuner answers `LSQ:` with a state table (the tuner is usable)

Measured by launching from a killed state and polling at 1 s granularity:

| Configuration | `t_port` | `t_lsq` |
|---|---|---|
| `PlayIntroVideo 0` | **12.0 s** | 29.4 s |
| `PlayIntroVideo 1` | **12.0 s** | 28.9 s |

### The intro video does not delay tuner readiness at all

This was not the expected result. **The tuner comes up independently of the intro cinematic** — port
4318 binds at 12 s and answers `LSQ:` at ~29 s whether the intro plays or not. The 0.5 s spread
between the two rows is noise, not signal.

**Consequence for SC-010 / FR-045:** if crash recovery re-attaches over the tuner and drives through
Lua, **the intro video costs it nothing**, and disabling it buys no recovery-budget time. The earlier
assumption that intro playback "sits directly in that loop" does not hold for a Lua-driven recovery
path. It only holds for a recovery path that needs the *visual menu*.

### What disabling it actually buys

`PlayIntroVideo 0` removes the need for **synthetic input to dismiss the intro**. Verified
end-to-end: with it set, the client lands on the main menu on its own, no key presses.

With `PlayIntroVideo 1`, reaching the main menu required **four `Escape` presses** through: attract
cinematic → legal/copyright screen → main menu. That is four synthetic-input events that must
succeed unattended, on a path with no reliable completion signal.

So the value is **removing a fragile unattended UI step**, not saving wall-clock. That is still worth
having — it is one fewer thing to get wrong during an automated restart — but it should be justified
on reliability grounds rather than on the recovery budget.

### Method verified / not verified

| Method | Result |
|---|---|
| **`PlayIntroVideo 0` in `AppOptions.txt`** | ✅ **Verified working.** This is the recommended method. |
| Steam launch options (`-skipintro`, `-nointro`, `-noslowintro`, `\SkipIntro`) | ⛔ **Not tested** — the `AppOptions.txt` route worked on the first attempt, so no launch-option string is confirmed either way. Do not assume any of them work. |
| Renaming/removing `.bk2` video files | ⛔ **Not attempted, and not recommended.** Unnecessary given the supported setting exists, and it would modify the game install rather than user config. |

Note that `AppOptions.txt` is **rewritten by the client on exit**, so the file must be edited while
the game is closed or the edit is clobbered. The value then persists across subsequent clean exits.

## Graphics settings — recorded because they are not pinned anywhere

The owner configured this host for lowest graphics. **Graphics settings change what the agent
literally sees**, so two runs at different settings are not visually equivalent inputs — and
`SeedSet` currently pins `civilization`, `leader`, `ruleset`, `mod_set`, and `game_build`, but **not
graphics**. Recording them here so the first runs are reproducible even though the harness does not
yet pin them.

From `GraphicsOptions.txt` (`Version 10`), all non-comment values:

```ini
[Video]
PerformanceImpact 0            MemoryImpact 0
MSAA 1                         MSAAQuality 0
VSync 1                        RefreshRateInHz 60
ShadowMapResolution 2048       AODepthResolution 1024
AORenderResolution 1024        TerrainHeightMaskResolution 1024
ReducedAssetTextures 1

[Terrain]
TerrainSynthesisDetailLevel 2  TerrainQuality 0
ReducedTerrainMaterials 1      LowQualityTerrainShader 1

[General]
SSReflectPasses 0              UseLowResWater 1
UseLowQualityWaterShader 1
```

Client window geometry on this host: **1920 × 1200**, positioned at `+2560+0` on a 4480 × 1440
two-monitor X11 desktop.

## The `60 FPS` overlay is Steam's, not the game's

The client's top-right corner renders a green `60 FPS` readout that appears in every capture.

**It is not the game and not the debug menu** — it persists with `EnableDebugMenu 0` (see
[principle-i-debugmenu-linux.md](./principle-i-debugmenu-linux.md)). It is the **Steam overlay's**
FPS counter:

```
~/.steam/debian-installation/userdata/<id>/config/localconfig.vdf
    "InGameOverlayShowFPSCorner"    "2"        # 2 = top-right; 0 = off
```

### Why this matters more than a cosmetic blemish

It renders **inside the game's own frame**, so window-scoped capture cannot exclude it the way it
excludes a desktop panel or taskbar. Under FR-025 / Scenario 3's zero-tolerance wording, every
capture on this host currently contains non-player chrome.

It also generalises: this is proof that **a third party can composite content into the client's frame
without the client knowing**. The screening profile needs to account for that class of contamination,
not just for this one overlay. A Discord, MangoHud, or driver overlay would land the same way.

**Not changed by this spike.** `InGameOverlayShowFPSCorner` is a **Steam-client-wide** setting
affecting every game on the owner's account, so it is his call, not the harness's. Two ways to clear
it, both one step:

- Steam → Settings → In Game → *In-game FPS counter* → **Off**, or
- set `"InGameOverlayShowFPSCorner" "0"` in the file above **with Steam closed** (Steam rewrites
  `localconfig.vdf` on exit).

Until then, Linux captures should be treated as carrying known contamination.

## Menu geometry is not stable — a trap for coordinate-based automation

Worth recording because it produced a real misclick during this session.

The Single Player submenu is `Load Game / Play Now / Scenarios / Create Game` on a fresh profile, but
becomes `Resume Game / Load Game / Play Now / Scenarios / Create Game` **once any save exists**. Every
item below the insertion point shifts down by one row, and a click calibrated before the first save
lands on the wrong entry afterwards.

**Any UI-driven preparation path must locate menu entries by content, not by fixed coordinates**, or
it will silently break the moment the first quicksave is written — which is turn 1 of the first run.
This is an argument for keeping preparation on the Lua path wherever possible.

## Reproducing the fast configuration on a fresh host

With the client **closed**, in `~/.local/share/aspyr-media/Sid Meier's Civilization VI/AppOptions.txt`:

```ini
EnableTuner 1        # [Debug] - required; not exposed in the options menu on Linux
EnableDebugMenu 0    # [Debug] - Principle I; costs nothing, see the debug-menu spike
PlayIntroVideo 0     # removes 4 synthetic Escape presses from unattended startup
```

Launch with `setsid steam steam://rungameid/289070` (Steam must already be running). Expect the
tuner at ~12 s and a usable `LSQ:` at ~29 s.

The original file on this host is preserved at `AppOptions.txt.pre-civsim.bak`
(`EnableTuner 0`, `EnableDebugMenu 1`, `PlayIntroVideo 1`).

## Still open

- **`CivSim DEFAULT` preset**: not yet loaded, and its on-disk file not yet located. Needs the game
  setup screen. Note that `Play Now` **randomises the leader** (England/Eleanor then Korea/Seondeok
  across two launches), which is exactly why the fixed-leader preset matters.
- **Time-to-interactable-main-menu** is not measured. `t_lsq` is a tuner signal, not a UI one;
  measuring the visual menu needs an image-based signal.
- Launch-option strings are untested (see the method table above).
