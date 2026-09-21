# Linux landed-code demo — the production harness plays three turns of a real game

2026-09-21 00:28 EDT. Civilization VI 1.0.12.9 (Aspyr, native Linux), BBG 7.5.0 / BBM 1.39.5 /
MPH 1.7.9 active, X11/Cinnamon. Run `run-9505f323bbf846739a5eb2bb0bee9778`, **finished**,
`stop_resolution = turn_reached`, 11 seconds wall-clock.

> **Decisions were scripted, not model-generated — zero model calls.** `FakeModelProvider`
> answered every decision step with "end the turn". What this shows is the **landed harness**
> driving a real client through its production composition root: `build_runner_dependencies` →
> `Runner.start(config)` → preparation (build pin V10, setup read-back V2, seed-set agreement V3,
> turn-timer preflight, host gate, run-identity lock) → three turn cycles, each with a verified
> quicksave, a full 14-declaration observation sweep, a decision step, and an end-turn asserted on
> the far side → `SqliteMatchStore`. It does **not** show an agent playing Civilization: Cyrus's
> Settler stood still for three turns. The model-driven run is a separate recording.

## What is landed and what is not — stated plainly

| step | production code? | note |
|---|---|---|
| The game itself (fresh `CivSim DEFAULT` game, human slot, Cyrus/Persia pinned, intro dismissed) | **no — operator step** | `tests/live/demo_landed_run.py --bring-up`, because the harness has no cold-client-to-turn-1 path yet (T237). It uses the production port operations (`focus_window`, `send_input`) but the sequence is the script's. The game had been brought up earlier in the session and was at turn 3 when this run started. |
| The run configuration (`run-config.yaml`) | **no — written by the script** from the live client's own V2 read-back through the production `LuaGameSetupReader`, so `map_seed` (which the game chose: `1745156737`, a pre-`HostGame` `RANDOM_SEED` did not stick) and `opponents.major_count` match. Every charter value — Persia, Cyrus, Gathering Storm, Emperor, Online speed, Ancient era, Small map — is asserted against the read-back first; the script refuses on a mismatch. |
| Everything from `Runner.start` to `finished` | **yes** | unmodified `live/linux` at `2cdecf5` + the two files landed with this evidence (`saves/verify.py` appearance wait, `lua/ingame/camera.lua` zoom) |
| Every frame of the recording | **yes** | `LinuxHostPlatform.capture_window()` — XComposite, window-scoped by construction; no desktop recorder, no root grab |

## The far-side record (`store-evidence.json`, read straight from `civsim-match-store.db`)

```
lifecycle_state            finished          stop_resolution   turn_reached
game_build                 linux/1.0.12.9    host_support_tier supported
record_completeness_status complete          comparability     visually_degraded
turn_cycles                3 (turns 1,2,3; attempt 0; all authoritative; 1 step each)
save_points                civsim__run-9505f323…__t0001 / __t0002 / __t0003  (each verified on disk)
captures                   6 taken, 0 shown to the agent (withheld: provenance_failure)
model_calls                0
game turn, read back       3 before -> 5 after  (Game.GetCurrentGameTurn(), fresh connection)
```

The turn advance is asserted on the far side — `Game.GetCurrentGameTurn()` read back over a fresh
tuner connection after the run, and the `TURN 5/250` in the last keyframe — never on the end-turn
call's own return.

**Why "visually degraded", and why that is the honest answer.** Two independent gates keep images
off the agent on this host today: (1) `doctor` reports tier `SUPPORTED`, not `VALIDATED`, because
the composition root never feeds the T249 compositor check into the support probe (proposed T252),
so the T238 R6 gate refuses; (2) every capture was **withheld** by the provenance gate — first for
"no numeric zoom" (fixed here: `UI.GetMapZoom()`), then for "the view requires a revealed target
plot, and none was confirmed revealed", because this build exposes **no camera look-at getter**
(`UI.GetCameraTargetPlot` / `GetMapLookAtPlot` do not exist; only `GetMapZoom`, `SetMapZoom`,
`LookAtPlot`, `GetCursorPlotID`, `GetWorldRenderView`). Withholding is the fail-closed direction the
spec requires; the record says so rather than pretending the agent saw a screen.

## Files

| file | what |
|---|---|
| `harness-landed-run.gif` | 12 frames @ 720 px, 1 fps, 2.3 MB — the whole run, through the production capture path |
| `keyframe_0_t000s.png` … `keyframe_3_t012s.png` | four full frames: the last shows `TURN 5/250`, the Settler still awaiting orders |
| `timeline.txt` | the captioned timeline written by the driver |
| `results.json` | the driver's own record (its `store` block errored on a column name — superseded by `store-evidence.json`) |
| `store-evidence.json` | the run as the match store recorded it |
| `run-config.yaml` | the configuration the script wrote from the read-back and `Runner.start` consumed |

## What it took to get here tonight (each measured, each in a spike)

Seven attempts, each stopped by the next thing a real client says that a fake never did — all in
`spikes/t213-observation-bodies-linux.md` and `t251-mod-set-identity-linux.md`: the game version
was unreadable on Linux (`Modding.GetActiveGameVersion` does not exist; `UI.GetAppVersion()` does);
`mod_set` could not pass V2 on any modded host; five of fourteen observation bodies errored on
accessors that exist only in the other Lua context or not at all; a Lua error cost a 30 s timeout
instead of failing at once; the turn-2 quicksave was checked before the client had finished writing
it; a stale run-identity lock survives a killed runner. The three earlier runs in the store
(`run-b2c485cc…`, `run-ce0cc7a0…`, `run-76435621…`) are those attempts, left as recorded — each
`paused` at the exact step that stopped it.

## Reproduce

```
python3 -m tests.live.demo_landed_run specs/002-civ-playing-harness/spikes/demo-evidence-linux-landed \
    --provider fake --turns 3 [--bring-up]
```
