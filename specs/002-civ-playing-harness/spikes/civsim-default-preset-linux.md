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

## Parameters recovered from the file

Extracted from the plaintext regions. **These are inferred from the encoded strings, not read back
through `GameConfiguration`** — treat as strong indications pending confirmation.

| Field | Value |
|---|---|
| Civilization | **`CIVILIZATION_PERSIA`** |
| Leader | **`LEADER_CYRUS`** (the fixed leader) |
| Map | **`LOC_MAP_PANGAEA`** — Pangaea |
| Game speed | **`GAMESPEED_ONLINE`** |
| Start era | **`ERA_ANCIENT`** |
| Civ level | `CIVILIZATION_LEVEL_FULL_CIV` |

`GAMESPEED_ONLINE` is the fastest speed in the game and is a deliberate-looking choice for an
experimentation harness — turns resolve in far fewer game-years. It is **not** the `GAMESPEED_STANDARD`
that a `Play Now` game uses, which matters: any yardstick calibrated on standard speed (including the
"100 science / 100 culture by turn 50" goal in the constitution) means something different here.

The file also contains the full roster of every leader and mod in the installation — that is the
*available options* list, not the selection, so leader names other than Cyrus in a `strings` dump
should not be mistaken for configuration.

## What is still needed

1. **Authoritative read-back through `GameConfiguration`.** The values above come from parsing a file,
   which is exactly the kind of inference FR-002 / V2 exist to forbid relying on. Loading the preset
   and reading each field back is the real answer, and is also what exercises the read-back path the
   spec requires.
2. **🔴 Does the preset carry `TURNTIMER_STANDARD`?** This is the highest-value open question on the
   host, because it decides whether the
   [turn-timer blocker](./turn-timer-blocker-linux.md) is a property of `Play Now` defaults or of
   every game on this machine. Reading `GameConfiguration.GetTurnTimerType()` after loading the
   preset answers it in one call.
3. **Difficulty, map size, opponent count, city-state count, victory conditions** — not recovered
   from the plaintext regions; they are presumably in the compressed section.
4. **Confirm it loads from the standard UI.** Selecting a saved configuration in Create Game is an
   ordinary menu action, which is what makes it parity-valid under Principle I — but that is
   reasoning, not something this spike observed.

## Note on `Play Now`

`Play Now` **randomises the leader** — England/Eleanor, Korea/Seondeok, and Mali/Mansa Musa across
three consecutive launches on this host. It is unusable for seeded work, which is precisely why the
fixed-leader preset exists. No run should be prepared through it.
