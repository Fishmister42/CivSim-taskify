# T213 live — every observation body, dispatched through the production executor

2026-09-21, Linux validation host, Civilization VI 1.0.12.9 (Aspyr), a fresh `CivSim DEFAULT` game
at turn 1 (Cyrus/Persia, Emperor, Online speed, Small Pangaea, 22 mods). Probe:
`tests/live/probe_observation_bodies.py` — the production `load_catalog` → `CapabilityRegistry` →
`CapabilityExecutor` chain on the production `NexusClient`, every `kind: observation`
declaration, each result validated with the same `_validate_output` the production
`ObservationReader` applies. Raw results: `t213-observation-bodies-linux.json` (the passing run).

## Why this was measured

The first real run through the composition root (`run-b2c485cc…`, 23:45) reached `playing` —
build pin, V2 (with T251), V3 and the identity lock all passed live for the first time — and then
died in its first observation sweep on `CivSim_Diplomacy_GetState` (`Player:GetDiplomaticAI()`:
"function expected instead of nil"). Rather than fix one body and hit the next, all fourteen were
dispatched exactly as a run dispatches them.

## First pass: 9 of 14

| declaration | context | result | cause |
|---|---|---|---|
| `camera.read_state` | InGame | OK | |
| `cities.state` | GameCore_Tuner | OK | (no cities yet) |
| `congress.state` | GameCore_Tuner | OK | |
| `diplomacy.state` | GameCore_Tuner | OK | after the `GetDiplomaticAI` pcall fix (below) |
| `espionage.state` | GameCore_Tuner | OK | |
| `game.outcome_state` | GameCore_Tuner | OK | |
| **`game.screen_state`** | InGame | **ERR** schema | the body returned the per-state probe shape `{screen_probe_ok, hidden}`; the declaration requires the aggregate `{screen, recognized, has_blocking_prompt}` |
| `game.turn_state` | InGame | OK | |
| **`government.state`** | GameCore_Tuner | **ERR** schema | `current_government` required — but there is no government before Code of Laws; a Lua table cannot carry a nil key, so the encoder omits it |
| `great_people.state` | GameCore_Tuner | OK | |
| **`map.state`** | GameCore_Tuner | **ERR** 30 s timeout | `Plot:IsRevealed(playerID)` does not exist → Lua error on the first plot |
| `religion.state` | GameCore_Tuner | OK | |
| **`research.state`** | GameCore_Tuner | **ERR** schema | `current_research` required — nothing chosen at turn 1 |
| **`units.state`** | GameCore_Tuner | **ERR** 30 s timeout | `Unit:GetUnitType()` does not exist in GameCore_Tuner → Lua error on the first unit |

## The API facts, measured call by call (both contexts unless stated)

| call | GameCore_Tuner | InGame |
|---|---|---|
| `Map.GetGridSize()` | `74x46` | `74x46` |
| `Map.GetPlot(x,y)` / `:GetTerrainType()` → `GameInfo.Terrains[...].TerrainType` | ok (`TERRAIN_OCEAN`) | ok |
| `Plot:IsRevealed(pid)`, `Plot:IsVisible(pid)` | **nil** | **nil** |
| `PlayersVisibility[pid]:IsRevealed(x,y)` / `:IsVisible(x,y)` | ok (22 revealed at turn 1) | ok |
| `Plot:IsResourceVisible` | absent | absent |
| `Plot:GetOwner/GetFeatureType/GetResourceType/GetImprovementType` | ok (`-1` unowned/none) | ok |
| `Players[0]:GetUnits():Members()` | ok (2 units) | ok |
| `Unit:GetMovesRemaining/GetMaxMoves` | `2/2` | `2/2` |
| `Unit:GetUnitType()` | **nil** | ok (`UNIT_SETTLER`) |
| `Unit:IsFortified` / `Unit:GetFortifyTurns` | absent / present | absent / present |
| `UnitManager.GetReachablePlots` | absent | absent |
| `UnitManager.GetReachableMovement(unit)` | **nil** | ok — `table` of 8 plot **indices** |
| `UnitManager.CanStartOperation(unit, FOUND_CITY)` | **nil** | `true` |
| `Player:GetDiplomaticAI()` | **nil** | (UI-side) |
| `ContextPtr:LookUpControl("/InGame/<State>")` | — | resolves each watchlist screen; `nil` for a name that does not exist (`CivilopediaScreen` is absent on this build; a bogus name → nil) |
| `<screen ctx>:IsHidden()` | — | `true` for every watchlist screen at the world view |
| `UIManager:IsInPopupQueue(ContextPtr)` | — | `false` |
| `UI.GetInterfaceMode()` | — | hash |

Two transport facts fell out of the same session:

- **A Lua runtime error in a dispatched chunk arrives as a NON-empty tag-3 payload**
  (`ERR:Runtime Error: [string ...]:68: function expected instead of nil` + stack) and no
  sentinel-bracketed result ever follows. `NexusClient._await_result` logged it as an unexpected
  framing and waited out the full 30 s per-operation timeout, so every failing body above cost 30 s
  and was recorded as "timeout". Now a `NexusError` at once, reason `lua_error`, carrying the text.
- **A Lua *syntax* error prints nothing at all** (no sentinel, no `ERR:`) — measured by accident
  with `obj:method ~= nil`, which does not parse. That case still times out; there is no signal.

## What landed

- `lua/gamecore/diplomacy.lua`: `GetDiplomaticAI` under pcall; `diplomatic_state` null when the
  accessor is absent, with a `GetDiplomacy():GetDiplomaticStateIndex` fallback (UNVERIFIED).
- `lua/gamecore/map.lua`: `PlayersVisibility[pid]:IsRevealed/IsVisible(x, y)`; resources reported
  only when `Players[pid]:GetResources():IsResourceVisible(hash)` (UNVERIFIED, pcall) confirms —
  withholding, never over-revealing (Principle I).
- `lua/gamecore/units.lua` + `catalogs/observations/units.yaml`: **context → InGame** (the only
  context where `GetUnitType`, `GetReachableMovement`, `CanStartOperation` exist);
  `GetReachableMovement` indices → `Map.GetPlotByIndex`; `is_fortified` from `GetFortifyTurns`.
- `lua/ingame/screens.lua`: `screens.probe` now returns the aggregate `game.screen_state` shape,
  built from InGame by looking up every watchlist screen's context and its hidden flag; a
  recognised `prompt.*` screen outranks anything else open; unmapped open screen → `unknown`,
  `recognized=false`. The per-state probe survives as `probe_own_state`.
- `catalogs/observations/government.yaml`, `research.yaml`: `current_government`,
  `current_research`, `current_civic` no longer `required` (their types already allowed null).
- `nexus/client.py`: fail fast on a tag-3 `ERR:` payload (`REASON_LUA_ERROR`), with a unit test.

## Second pass: 14 of 14

All fourteen declarations answer with a schema-valid value in 27–67 ms each (`map.state` 33 ms for
22 revealed plots; `units.state` 49 ms with `can_found_city: true` and 8 reachable plots for the
Settler). `game.screen_state` at the world view: `{"screen": "world_view", "raw_screen_id":
"InGame", "recognized": true, "has_blocking_prompt": false, "prompt_options": []}`.

## Still UNVERIFIED after this pass

- `research.state.current_civic` read `CIVIC_CODE_OF_LAWS` at turn 1 before any civic was chosen
  — plausibly the game's default selection, not evidence the accessor distinguishes "chosen" from
  "default". `researchable_civics` was `[]` while `researchable_techs` listed five.
- `map.state`'s resource visibility accessor and `diplomacy.state`'s `diplomatic_state` fallback
  have only been exercised on their absent/turn-1 branches.
- `game.screen_state` has been observed at the world view only; an open prompt has not yet been
  seen through it live.
- Everything here is Linux 1.0.12.9; per R19 each host confirms its own.

## Third pass (turn 2, after the AI's first turn): two more, same root cause

The first real run after the second pass (`run-ce0cc7a0…`, 00:14) reached `playing`, took its
**first production quicksave** (`civsim__run-ce0cc7a0…__t0001`, `save_taken` event), and then the
new fail-fast path named the next error at once: `CivSim_Religion_GetState` line 107. Re-probing
at turn 2 — the AI had founded cities by then — `cities.state` and `religion.state` both failed
where they had passed at turn 1: their per-city loops reach `plot:IsVisible(localPlayer)` on the
first *foreign* city, and `Plot:IsVisible` does not exist. Turn 1 had no cities, so the loops never
ran. `lua/ingame/camera.lua`'s `target_is_revealed` used `plot:IsRevealed(...)` under a pcall, so
it silently reported `false` rather than failing. All four now use `PlayersVisibility[pid]`.
Re-probed at turn 2: **14/14**, with `cities.state` reporting `[]` (no *visible* foreign city,
correctly) rather than erroring.

Lesson for the roster: a body that passes at turn 1 has only proven its empty-collection branch.
