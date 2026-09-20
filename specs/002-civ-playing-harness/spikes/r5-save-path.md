# Spike R5 / T077 — Is there a FireTuner Lua path that takes a save?

**Feature**: `002-civ-playing-harness` · **Task**: T077 · **Executed**: 2026-09-20
**Host**: Linux (native Aspyr build) · **Executed by**: Linux live-validation node

## Verdict

**Outcome A — a Lua save path exists and works.**

`Network.SaveGame(gameFile)`, called in the **`InGame`** context, writes a real `.Civ6Save` to disk.
Four consecutive calls produced four valid, size-stable save files.

Per [research.md](../research.md) R5's outcome table, this means:

| | |
|---|---|
| `save_game` capability path | `path: firetuner` |
| Bespoke input-automation save driver | **Not needed. Complexity item C1 disappears.** |
| `firetuner_gap` evidence required | None — there is no gap to document |

This removes the only known Principle II bespoke path from the save surface, on **all three
platforms at once**: the blocked Wayland case and the macOS Accessibility-grant prerequisite in R5's
risk table were both consequences of needing synthetic input to save. They do not apply to a Lua
save path.

> **Scope of this claim.** This spike proves a save is *written*. It does **not** prove a save can be
> *loaded back*. See [Not yet verified](#not-yet-verified) — FR-034 branch identity depends on the
> load half, and that is a separate test.

## Environment

| | |
|---|---|
| Client build | `1.0.12.9 (564030)`, Gathering Storm, reported as `MPH / 179` |
| Platform | Linux, native Aspyr port (not Proton) |
| Session | X11 / Cinnamon |
| Tuner | `EnableTuner 1` in `AppOptions.txt`; client listening on `127.0.0.1:4318` |
| Game state | Single player, "Play Now", English Empire, turn 1 |
| Mods active | BBG (`bbg_script`, `BBM_UI`, `bbg_uitogameplay`) and an MP helper (`MP_helper`, `MPHOptions`) |

Probing used a **raw socket client independent of `civsim_harness`**, so these results are evidence
about the game rather than about our implementation.

## Method and why the obvious approach fails

R5 asks for an enumeration of `Network.*`, `UI.*`, and `Game.*`. Two facts about Civ VI's Lua
sandbox make a naive enumeration return nothing, and both are worth recording because they will
mislead the next person who tries:

1. **`_G` is nil.** There is no global table to walk. An attempt raises
   `bad argument #1 to 'pairs' (table/struct expected, got nil)`.
2. **`UI` and `Network` are opaque.** `pairs()` over them yields **0 entries** — they are not plain
   tables, and their members are not reachable by iteration. `Game` *is* iterable.

So "enumerate the namespace" is not achievable by iteration for exactly the two namespaces that
matter. The workable method is **direct name probing**: evaluate each candidate under `pcall` and
record its `type()`. That is what the tables below report.

## Raw enumeration — `Game` is iterable, `UI`/`Network` are not

```
=== Network (0 entries, pairs_ok=true) ===
=== UI (0 entries, pairs_ok=true) ===
=== Game (53 entries, pairs_ok=true) ===
```

`Game`'s 53 members in `InGame` (41 in `GameCore_Tuner`) are all read-only accessors —
`GetCurrentGameTurn`, `GetLocalPlayer`, `GetPlayers`, `GetUnits`, `GetVictoryProgressForTeam`, and
similar. **No member of `Game` writes, saves, or mutates anything.** Full listing:
[`r5-raw/InGame__namespaces.txt`](./r5-raw/InGame__namespaces.txt).

## Candidate probe — the actual result

Evaluated in both contexts. `ERROR` means the root namespace does not exist in that context at all.

| Candidate | `InGame` | `GameCore_Tuner` |
|---|---|---|
| **`Network.SaveGame`** | **`function`** | `ERROR` (no `Network`) |
| **`Network.LoadGame`** | **`function`** | `ERROR` |
| `Network.QuickSave` | `nil` | `ERROR` |
| `Network.QuickLoad` | `nil` | `ERROR` |
| `Network.BeginSaveGame` | `nil` | `ERROR` |
| `Network.IsSaveGameInProgress` | `nil` | `ERROR` |
| `Network.GetSaveGameList` | `nil` | `ERROR` |
| `UI.QuickSave` *(the Civ V API)* | `nil` | `ERROR` |
| `UI.QuickLoad` | `nil` | `ERROR` |
| `UI.SaveGame` / `UI.LoadGame` | `nil` | `ERROR` |
| `UI.RequestSave` / `UI.SaveGameAs` / `UI.QueueSaveGame` | `nil` | `ERROR` |
| `Game.SaveGame` / `Game.QuickSave` / `Game.Save` | `nil` | `nil` |
| `Automation.SaveGame` | `nil` | `nil` |
| `SaveLocations.LOCAL_STORAGE` | `number` (1) | `number` (1) |
| `SaveTypes.SINGLE_PLAYER` | `number` (1) | `number` (1) |
| *control:* `Game.GetCurrentGameTurn` | `function` | `function` |
| *control:* `UI.GetInterfaceMode` | `function` | `ERROR` |
| *control:* `UIManager.QueuePopup` | `function` | `ERROR` |

Two findings beyond the headline:

- **R5's reasoning was right, its conclusion was wrong.** `UI.QuickSave` is indeed absent — the Civ V
  API does not carry over, exactly as research predicted. The error was inferring from that absence
  that *no* Lua save path exists. The save moved to `Network`, not to nowhere.
- **`GameCore_Tuner` cannot save, and cannot act at all.** `Network`, `UI`, and `UIManager` are
  entirely absent there, so every save candidate errors at the root. This is direct confirmation of
  `nexus-protocol.md`'s execution-context table: `GameCore_Tuner` is read-only state queries, and
  `InGame` is "the only path for acting." That row is now observed rather than assumed.

## Live test — the save is real

The call, exactly as issued:

```lua
local gameFile = {}
gameFile.Name       = "civsim__spike__t0001"
gameFile.Location   = SaveLocations.LOCAL_STORAGE   -- 1
gameFile.Type       = SaveTypes.SINGLE_PLAYER       -- 1
gameFile.IsAutosave = false
gameFile.IsQuicksave = false
Network.SaveGame(gameFile)
```

Output:

```
turn before save: 1
SaveLocations.LOCAL_STORAGE = 1
SaveTypes.SINGLE_PLAYER = 1
Network.SaveGame pcall ok = true
Network.SaveGame returned/err = true
```

Filesystem, diffed before and after the call:

```
Saves/Single/civsim__spike__t0001.Civ6Save   (681096 bytes)
```

### Repeatability

Three further saves from the same session, then each file's size read twice:

```
civsim__rep__t0002.Civ6Save  681092B  stable=True
civsim__rep__t0003.Civ6Save  681092B  stable=True
civsim__rep__t0004.Civ6Save  681092B  stable=True
civsim__spike__t0001.Civ6Save  681096B  stable=True
```

4 calls, 4 files, 0 failures. R5's outcome 3 ("a path exists but is unreliable") is not supported by
this evidence.

## The Linux save directory is confirmed

R5 listed the Linux save directory as an assumption to confirm per platform, noting Aspyr has
relocated these before. Confirmed exactly as written:

```
~/.local/share/aspyr-media/Sid Meier's Civilization VI/Saves/Single/
```

**The directory did not exist before the first save** — only `Aspyr/ Cache/ Logs/ Mods/
ModUserData/` were present. `Network.SaveGame` created `Saves/Single/` on demand. A host adapter
that probes for the directory's existence during preflight will get a false negative on a fresh
install; it should resolve the path without requiring it to already exist.

The arbitrary `Name` is honoured verbatim as the filename stem, so R5's naming convention
`civsim__<run_id>__t<turn:04d>` works as specified with no escaping concerns for that character set.

## Consequences for the design

- **T078–T081 are unblocked with a positive result.** Implement `save_game` as a declared FireTuner
  capability in the `InGame` context. No bespoke capability, no `firetuner_gap` statement.
- **Verification stays filesystem-based.** `Network.SaveGame` returns `true` immediately but the file
  appears asynchronously, so the return value is not proof. R5's rule — confirm the `.Civ6Save`
  exists with a size stable across two reads before the turn proceeds — is the right design and
  should be kept exactly as written. It is what this spike used.
- **The save capability must declare context `InGame`.** Declaring it under `GameCore_Tuner` would
  fail at catalog load, which is the intended behaviour.
- **Principle I / parity basis is unchanged and sound.** A human takes this save via
  Esc → Save Game → type a name → Save. The Lua path reaches the same operation the same menu does,
  so the capability remains declarable on the basis R5 already wrote.

## Not yet verified

These are open, and none of them is implied by the result above:

1. **Loading a save back.** `Network.LoadGame` exists as a function but was not called. FR-034 branch
   identity ("two branches from the same save point begin from an identical position") rests on the
   load half, not the save half. **This is the highest-value follow-up** and needs its own spike.
2. **Name collision behaviour** — overwrite, silent fail, or suffix? Untested. Matters for replayed
   turns after a crash, which write the same turn's save twice.
3. **The `IsQuicksave` flag's effect.** Set to `false` here. Whether `true` changes the filename,
   the directory, or the game's own rotation is unknown, and R5's "quicksave in addition to the
   autosave rotation" wording should not be read as requiring this flag.
4. **Saving mid-turn vs at turn start**, and whether a save issued while a prompt or modal is open
   behaves differently.
5. **Windows and macOS.** This is a Linux result. The API is very unlikely to differ, but the
   directory layout will, and per R19 each platform confirms its own.
6. Whether the **BBG mod** affects any of the above. It was active throughout, so these results
   describe a BBG-loaded client — which is the project's target configuration anyway.

## Reproducing

Scripts used are in [`r5-raw/`](./r5-raw/) alongside their unedited output. `nexus_probe.py` is a
standalone raw-protocol client — it deliberately does not import `civsim_harness`.
