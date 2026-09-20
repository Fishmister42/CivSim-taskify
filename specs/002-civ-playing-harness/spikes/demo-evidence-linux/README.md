# Linux demo evidence — ⚠️ spike-driven, NOT the deliverable demo

**Read this before using any of it.** Per the ruling that the demo records against **landed code
only**, this recording does **not** qualify and is not offered as the demo. It is spike evidence that
the sequence works, recorded while establishing it.

**What drives it:** `tests/live/demo_recorded_run.py`, a spike script. The two things it demonstrates
are both *absent from production*:

- `Network.HostGame(...)` — the new-game start. **Appears nowhere in `src/`** (see
  `r8-new-game-start-linux.md`), so nothing in the harness can do this yet.
- The `Escape` intro dismissal — published for transcription as **T248**
  (`t248-intro-dismissal-patch.md`), not yet landed.

The deliverable demo records after T248 lands and is re-verified against the production
`LuaSaveLoader`.

## What the recording nonetheless proves

Every frame comes from **`capture_window()`** — the harness's own XComposite path, the same one a run
uses to show an agent the screen. It is **window-scoped by construction**; no desktop recorder was
used, and the harness capture rule (window-scoped, never root) is unchanged and unrelaxed. So the
artifact demonstrates the capture path rather than merely depicting it: 152 frames captured live at
~1 fps with no dropped frames and no interference with the running client.

## Timeline (`timeline.txt`)

```
t+000s  recording started (frames via capture_window(), window-scoped)
t+004s  leaving the current game: Events.ExitToMainMenu()
t+035s  applying the 'CivSim DEFAULT' preset from Lua -- no UI
t+051s  starting the game: Network.HostGame(SERVER_TYPE_NONE)
t+093s  dismissing the leader-intro screen with send_input(Escape)
t+098s  intro dismissed after 1 Escape press
t+103s  in game -- observing through the tuner
t+123s  ending the turn
t+147s  verifying the turn advanced (far side, later command)
t+165s  recording stopped: 152 frames
```

Read back over the tuner during the run, not inferred from the pixels:

```
  preset loaded  = true
  humans         = 1
  leader         = LEADER_CYRUS
  turnTimer NONE = true
  ...
  leader (in-game) = LEADER_CYRUS
  turn             = 1
  turn now         = 2
```

**Zero model calls.** Every decision in this recording is scripted; nothing consulted an LLM. The
provider key is missing on both hosts, so a model-driven run is not yet possible on either.

## Files

| file | what |
|---|---|
| `harness-driven-run.gif` | 59 frames @ 480px, ~4.7 MB — downsampled for inline rendering |
| `keyframe_*.png` | four full-resolution 720px frames at t+0 / t+54 / t+110 / t+164 |
| `timeline.txt` | the captioned timeline above, written by the recorder |
