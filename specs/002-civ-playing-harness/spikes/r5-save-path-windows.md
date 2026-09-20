# Spike R5 / T077 — Is there a FireTuner Lua path that takes a save? (Windows)

**Feature**: `002-civ-playing-harness` · **Task**: T077 · **Executed**: 2026-09-20
**Host**: Windows 11 (26200), Steam build · **Executed by**: Windows live bring-up node

## Verdict

**Outcome A — a Lua save path exists and works on Windows.** `Network.SaveGame(gameFile)`, called in
the **`InGame`** context, writes a real `.Civ6Save`. Four consecutive calls produced four valid,
size-stable files. This matches the Linux result exactly.

| | |
|---|---|
| `save_game` capability path | `path: firetuner` |
| Bespoke input-automation save driver | **Not needed on Windows either** |
| `firetuner_gap` evidence required | None |

> ⚠️ **But stock `civsim_harness` cannot do this today.** The saves above were taken through the
> harness's own `NexusClient` **with three protocol defects patched**. Against unmodified
> `civsim_harness`, this client cannot complete a handshake, and even after that it cannot read the
> result of a single command. See [The three protocol defects](#the-three-protocol-defects) — that is
> the most important finding in this document, more than the save result itself.

## Environment

| | |
|---|---|
| Client build | `1.0.12.68 (1023995)` — **much newer than the Linux peer's `1.0.12.9 (564030)`** |
| Storefront | **Steam** (`Base\Binaries\Win64Steam\CivilizationVI.exe`), DX11 |
| Renderer | D3D11, NVIDIA RTX 4070 SUPER, windowed 2560×1440 |
| Tuner | `EnableTuner 1`; client listening on `127.0.0.1:4318` |
| Game state | Single player, loaded autosave, **turn 8**, Portuguese Empire (João III) |
| Mods active | **BBG (Better Balanced Game) 7.5.0**, BBS, BSM, MP helper — Lua states `bbg_script`, `BBM_UI`, `bbg_uitogameplay`, `MP_helper`, `MPHOptions` all present |
| Lua | `2013.2.0 r13768` |

Raw scripts and unedited transcripts: [`r5-raw-windows/`](./r5-raw-windows/).

## Which install is live — the briefing was wrong, and so was T050

The hypervisor's briefing stated the **Epic** profile was the live, configured one. It is not.

| Evidence | Steam profile | Epic profile |
|---|---|---|
| Newest file under the profile | `Saves\Single\auto\AutoSave_0008.Civ6Save`, **2026-09-16 16:50** | `Mods\...\project.idx`, 2024-06-08; newest *save* **2021-05-30** |
| `AppOptions.txt` mtime | **2026-09-16 15:56** | 2021-09-19 |
| Startup log | `Game Version = 1.0.12.68`, session 2026-09-16 15:55→16:51 | none today |
| BBG | **7.5.0, via Steam Workshop** (`workshop\content\289070\`) | 4.1.1, unused since 2024 |

The Epic profile's populated `Mods\` folder is what made it *look* live. It is not: those mods have
never been loaded by a running client on this machine. The Steam client gets BBG from the **Workshop**
directory, which is why its `Documents\...\Mods\` looked empty.

**So the run is charter-compliant on the BBG requirement** — BBG 7.5.0 was loaded throughout.

### The real T050 defect: Windows uses *two* roots, not one

T050 specifies `%USERPROFILE%\Documents\My Games\Sid Meier's Civilization VI\` and hangs both the
save directory and `AppOptions.txt` off it. Half of that is right:

```
saves   -> %USERPROFILE%\Documents\My Games\<profile>\Saves\Single\      CORRECT
options -> %LOCALAPPDATA%\Firaxis Games\<profile>\AppOptions.txt         T050 HAD THIS WRONG
```

There is **no `AppOptions.txt` anywhere under `Documents\My Games\Sid Meier's Civilization VI\`**.
Anything reading the tuner/debug/turn-timer settings through the old path — `debug_menu_preflight`,
`turn_timer_preflight` — was reading a file that does not exist on this platform.

The older **all-under-Documents** layout still exists here for the Epic profile, so
`resolve_game_directories` now probes both layouts for both profile names and picks the candidate
whose `AppOptions.txt` actually exists, most-recently-written first. It does **not** require
`Saves\Single\` to exist (R5's Linux finding: it is created by the first save).
`CIVSIM_CIV6_PROFILE` overrides the profile directory name.

Verified live:

```
LIVE saves  : C:\Users\...\Documents\My Games\Sid Meier's Civilization VI\Saves\Single   exists: True
LIVE options: C:\Users\...\AppData\Local\Firaxis Games\Sid Meier's Civilization VI\AppOptions.txt  exists: True
```

## The three protocol defects

All three are the same mistake — **"the next frame I read is my answer"** — and all three are in
`src/civsim_harness/nexus/client.py`, which is outside this agent's lane. They are demonstrated in
[`r5-raw-windows/r5_save_spike.py::LiveNexusClient`](./r5-raw-windows/r5_save_spike.py) rather than
fixed in place.

### 1. `_handshake` never reads the `APP:` reply

`_handshake` sends `APP:<name>`, then immediately calls `_query_states`, which sends `LSQ:` and reads
the next frame — which is the **`APP:` response**, not the state list. Observed
([`raw_protocol_transcript.txt`](./r5-raw-windows/raw_protocol_transcript.txt)):

```
--- after APP:: 1 frame(s) ---
  [0] tag=4 payload="Civ6\x00Sid Meier's Civilization 6\x00C:\\...\\Base\\Binaries\\Debug"
--- after LSQ:: 2 frame(s) ---
  [0] tag=4 payload='0\x00Main State\x001\x00DebugHotloadCache\x002\x00Options\x00...'
```

So `_parse_state_list` receives the identification string, which has three NUL-separated fields, and
raises:

```
NexusError: Nexus LSQ response payload has an odd number of NUL-separated fields
```

**`NexusClient.connect()` cannot succeed against this client at all.**

### 2. `_query_states` raises on any interleaved frame

It reads exactly one frame and raises if the tag is not `TAG_HANDSHAKE`. The client interleaves
asynchronous log frames into the same stream, so any log line emitted between the `LSQ:` send and its
reply aborts the read. Observed live on the first `refresh_state_indices()`:

```
NexusError: Expected a TAG_HANDSHAKE response to LSQ:, got a different tag | detail={'tag': -1}
```

### 3. `_await_result` listens on the wrong tag — the decisive one

`_await_result` feeds only `TAG_COMMAND` (3) payloads to the sentinel correlator. On this client the
tag-3 frame is an **empty acknowledgement**, and the actual printed output arrives on **tag −1**,
each line prefixed `O\0<StateName>: `. Measured directly
([`raw_command_transcript.txt`](./r5-raw-windows/raw_command_transcript.txt)):

```
sending CMD to state 'InGame' (index 132), nonce=57a06e82d1a0432c949c3455ad3e0e45
  tag=-1 payload='O\x00InGame: ---BEGIN:57a06e82...---'   <<<< CARRIES OUR NONCE
  tag=-1 payload='O\x00InGame: {"probe":"tag-hunt","lua_version":"2013.2.0 r13768"}'
  tag=-1 payload='O\x00InGame: ---END:57a06e82...---'     <<<< CARRIES OUR NONCE
  tag=3  payload=''
RESULT: sentinels came back on tag(s) [-1]; TAG_COMMAND is 3
```

The correlator therefore never sees its own sentinels and **every command times out — while the Lua
runs perfectly well**. This is not a hypothetical: the `Network.LoadGame` call that reported

```
NexusError: Nexus command exceeded its per-operation timeout | detail={'state_index': 24, 'timeout_s': 30.0}
```

**loaded the game.** A caller that trusts that error would conclude the load failed, retry, and load
twice. A timeout here means "we could not read the answer", not "it did not happen".

> **Why this was never caught.** The Linux R5 spike states it used "a raw socket client independent
> of `civsim_harness`". So on the evidence in this repository, `NexusClient`'s command path had
> **never** been exercised against a real client on any platform before today. `contracts/nexus-protocol.md`
> documents neither the `APP:` reply, nor tag −1, nor the `O\0<State>: ` prefix.
> Whether this Windows behaviour is a build difference from `1.0.12.9` or was always true and simply
> never observed is **not established here** — the Linux peer should re-probe with
> [`raw_command_probe.py`](./r5-raw-windows/raw_command_probe.py) before anyone assumes a platform split.

## Live test — the save is real

Through the harness's own (patched) `NexusClient`, in the `InGame` context:

```lua
local g = {}
g.Name = "civsim__winspike__t0001"
g.Location = SaveLocations.LOCAL_STORAGE   -- 1
g.Type = SaveTypes.SINGLE_PLAYER           -- 1
g.IsAutosave = false; g.IsQuicksave = false
local ok, err = pcall(function() return Network.SaveGame(g) end)
```

Surface probe first, in `InGame`:

```json
{"Network": "table", "Network_SaveGame": "function", "Network_LoadGame": "function",
 "SaveLocations_LOCAL_STORAGE": "1", "SaveTypes_SINGLE_PLAYER": "1"}
```

Game state read back through the same path: `{"turn": 8, "local_player": 0, "lua_version": "2013.2.0 r13768"}`.

Four saves, each verified **on disk** with a size stable across two reads — asserting on what the far
side produced, never on `SaveGame`'s return value:

```
civsim__winspike__t0001  pcall_ok=true  exists=True  stable_size=1164811
civsim__winspike__t0002  pcall_ok=true  exists=True  stable_size=1164811
civsim__winspike__t0003  pcall_ok=true  exists=True  stable_size=1164811
civsim__winspike__t0004  pcall_ok=true  exists=True  stable_size=1164811
```

**4 calls, 4 files, 0 failures**, written to
`C:\Users\...\Documents\My Games\Sid Meier's Civilization VI\Saves\Single\`.
Full record: [`r5-raw-windows/r5_save_spike_results.json`](./r5-raw-windows/r5_save_spike_results.json).

## Bonus: the load path (T217) works from the front end

R5's Linux spike left "loading a save back" as its highest-value follow-up, and owner ruling C asks
whether the client can be started with a save already loaded. Measured here:

- **There is no `.Civ6Save` file association on this host** (no `HKLM`/`HKCU` `.Civ6Save` key), so the
  "open a save from the command line" variant of ruling C is **not available** on Windows.
- **`Network.LoadGame` works from the `MainMenu` Lua context** and does not need `InGame` to exist:

```lua
local f = {}
f.Name = "AutoSave_0008"
f.Location = SaveLocations.LOCAL_STORAGE
f.Type = SaveTypes.SINGLE_PLAYER; f.GameType = SaveTypes.SINGLE_PLAYER
f.IsAutosave = true
Network.LoadGame(f, ServerType.SERVER_TYPE_NONE)   -- -> ok=true
```

Confirmed on the far side, not by the return value: the client's `Lua.log` shows
`LoadScreen: OnLoadGameViewStateDone` and `MapLabelManager: MEDITERRANEAN SEA`, and the state table
grows from 32 front-end states to **136** including `GameCore_Tuner` (10) and `InGame` (132).

**Two pieces of UI still need input and are not reachable through Lua here**:
1. the attract/logo screen — the front-end Lua states do not exist until the client leaves it;
2. the leader-intro "Continue Game" modal after a load — `InGame` does not appear until it is dismissed.

Both were driven with **`WindowsHostPlatform.send_input`** (the T050 adapter path, exercised against a
real window for the first time): `escape`/`space`/`return` for the attract screen, one left click at
`(1058, 1076)` for the modal. Both reported `ok` and both worked. See
[`bringup.py`](./r5-raw-windows/bringup.py).

## The client is not durable on this host — and it is not our bug

Repeatedly, the client ran 30 seconds to a few minutes and vanished, with no crash dump and no clean
shutdown marker. The cause is in **Steam's** log, not the game's:

```
[2026-09-20 14:55:22] src\clientdll\cminterface.cpp (3753) : Assertion Failed: Expected connection state 0/1 but got 2
[2026-09-20 14:55:22] LogonFailure No Connection
[2026-09-20 14:57:58] Client version: 1788652215      <-- Steam restarted
```

Steam loses its connection to the Steam servers, restarts itself, and kills the game processes it
tracks. Steam restarted **8 times** during this session. Related, and worth knowing:

- **This desktop is a Steam Remote Play *client* for the owner's laptop.** `steam://rungameid/289070`
  started Civ VI **on the laptop** (`CRemoteClientJobStartRemoteApp ... on 10684637010189335358 (laptop)`)
  and streamed it back, so nothing ran locally.
- Launching `CivilizationVI.exe` directly made the game ask Steam to relaunch it, which hit the same
  remote path. Adding **`steam_appid.txt`** (contents `289070`) next to the executable stops that
  handshake and makes the client run locally. **This file was created by this spike** — see
  [What was changed](#what-was-changed-on-the-owners-machine).
- With Steam **not running**, the client exits within a second; Steam must be up.
- **`CivilizationVI_DX12.exe` exits immediately** on this host. Only the DX11 binary is usable.

## Not yet verified

1. **Whether the tag −1 / `APP:`-reply behaviour is Windows-specific or universal.** Not established.
   The Linux peer must re-run `raw_command_probe.py`.
2. **Save/load round-trip identity** (FR-034). Saves were written and a save was loaded, but not the
   same one, and no position comparison was made.
3. **Name-collision behaviour** — untested, as on Linux.
4. **`IsQuicksave = true`** — untested; all four saves used `false`.
5. **Saving mid-turn vs at turn start**, and saving with a modal open. The four saves here were taken
   with a "Research Completed" popup on screen and succeeded, which is suggestive but not a test.
6. **An unattended multi-turn run.** Impossible on this host until Steam stops restarting.
7. **macOS.** Untouched.

## What was changed on the owner's machine

| Change | Why | Revert |
|---|---|---|
| **Created** `...\Base\Binaries\Win64Steam\steam_appid.txt` containing `289070` | Without it, launching the client hands off to Steam, which starts the game on the owner's laptop over Remote Play instead of locally | Delete the file |
| Steam was shut down and restarted once | To test whether the client runs without Steam (it does not) | Already restarted |
| Four `civsim__winspike__t000N.Civ6Save` files written to `Saves\Single\` | The spike itself | Delete the four files |
| OBS scene + profile edited (Display Capture on, Game Capture off, `RecFormat` mp4→mkv) | OBS **22.0.2**'s game-capture hook DLL crashed the 2025 client within 3 seconds; mkv survives an abrupt stop | Backups in [`demo-evidence/`](./demo-evidence/) |

**No game settings, options files, mods, or pre-existing saves were modified.** `EnableTuner 1` was
already set; nothing needed changing to enable the tuner.
