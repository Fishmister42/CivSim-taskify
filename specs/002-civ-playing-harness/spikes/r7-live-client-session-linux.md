# R7 — the first live session against the real client: what worked, and four traps

**Date:** 2026-09-20 · **Host:** Linux live node · Follows `t217-RESOLVED-frontend-loadgame.md`.

Both remaining host primitives are now verified **against Civilization VI itself**, not against
`Xephyr` or throwaway X11 windows. Four traps were found in the process, three of them the kind that
report success while doing nothing useful.

## ✅ The first real Civ VI frame through `capture_window()`

`tests/live/test_civ6_real_frame.py`, driving the whole production chain —
`find_game_window` → `capture_preconditions` → `capture_window`:

```
[2] find_game_window() -> GameWindow(handle=136314920, title='Civilization VI',
                                     rect=WindowRect(left=2560, top=0, width=1920, height=1200), pid=841328)
[3] capture_preconditions() -> CapturePreconditions(composite_extension=True, compositing_manager=True,
                                                    unredirect_fullscreen_windows=False, detail=None)
[4.0] capture_window() -> status=ok in 46.7 ms
[4.1] capture_window() -> status=ok in 34.4 ms
[4.2] capture_window() -> status=ok in 37.0 ms
[5] frame: 1920x1200 format=BGRA8 bytes=9216000 -> MATCH
[6] distinct sampled pixels: 877 (real content)
```

**34–47 ms on the live client**, faster than the ~79 ms measured on synthetic windows. The saved PNG
(`t217-evidence/`) shows the real HUD, and it **cross-validates the Lua reads**: the frame says
`TURN 1/500`, Eleanor's portrait, `10` gold and a settler awaiting orders, exactly matching
`Game.GetCurrentGameTurn()=1`, `LEADER_ELEANOR_ENGLAND`, `gold=10`, `units=2/cities=0` read over the
tuner. Two independent observation channels agreeing on the same position is the strongest evidence
this project has produced for the capture path.

No Steam FPS overlay is present — capture hygiene is clean on this host.

**No repeated-capture flicker was observed** across three back-to-back captures, so per-capture
redirect churn is not visibly disruptive. Fullscreen capture and GPU overlays remain unmeasured; the
window here is 1920x1200 windowed on the second monitor.

### 🔴 Trap 1 — `find_game_window()` returns `None` if you locate the process by cmdline

The first run failed with `find_game_window() -> None`. The adapter was right; **the pid was wrong.**

Under Steam's pressure-vessel runtime, several processes carry the game's path in their cmdline —
the launch wrapper, `reaper`, `pv-adverb`, `srt-bwrap` — and `pgrep -f` returns the **wrapper first**:

```
pgrep -f "common/Sid Meier's Civilization VI/./Civ6"  -> 841213   (reaper)
xprop _NET_WM_PID on the game window                  -> 841328   (the real Civ6)
```

Only the real `Civ6` process owns a window, so a cmdline match hands `find_game_window` a pid that
owns nothing, and it correctly returns `None`. **Locate by exact process name (`pgrep -x Civ6`).**
This matters directly for whatever eventually sets `window_provider` — the reachability audit's
open defect. A process locator that gets this wrong produces exactly the symptom already recorded:
every step captured as `None` and every step recorded visually degraded.

## ✅ The first synthetic event into Civ VI

`tests/live/test_civ6_real_input.py`. Asserted on the client's own pixels, not on `InputStatus`:

```
[2] focused window pid=841328 (Civ pid=841328)
[3] before: 9216000 bytes, sig=20692aff183638ff52656aff6681a1ff
[4] send_input(Escape) -> status=ok
[5] after:  9216000 bytes, sig=010f0bff0b030cff210d09ff2f260bff
[6] far-side verdict: pixels CHANGED -> Civ VI ACCEPTED the synthetic key
```

**Civ VI accepts XTest input.** That was inference until now.

The test refuses to send unless the Civ window is focused at the moment of the call — XTest has no
window targeting, so without that guard a stray keystroke lands in whatever the owner has focused.

## 🔑 The loader needs exactly one keystroke — and no coordinates

The open question from `t217-RESOLVED` is **answered, in the inconvenient direction.**

A second load (`civsim__rep__t0004`) also returned `true`, and **also stopped on the leader-intro
screen** — so the `CONTINUE GAME` screen is **not** a turn-1 artifact. It appears on every load and
waits indefinitely: measured **150 s with no input and the tuner still closed.**

Which key dismisses it, measured through the production `send_input` path:

```
send_input(Return) -> ok    'Return' did not dismiss it
send_input(space)  -> ok    'space'  did not dismiss it
send_input(Escape) -> ok    ==> TUNER UP: 'Escape' dismissed the intro screen
```

**`Escape` dismisses it. `Return` and `space` do not.** So the complete `SaveLoader` is:

1. `Network.LoadGame(table, ServerType.SERVER_TYPE_NONE)` from `MainMenu`
2. wait for the tuner to close, then for the intro screen
3. `send_input(key_press "Escape")`
4. wait for the tuner to rebind (~5 s after the game is interactive)

**This is keyboard-only — there are no UI coordinates anywhere in it.** That retires the strongest
objection raised against a UI-assisted load path: coordinates are platform-specific and break when
the client's UI moves, but a single `Escape` is neither. It is also parity-clean — dismissing an
intro screen is exactly what a human does.

No setting was found to skip the intro: `UserOptions.txt` has only `HasSeenCivRoyaleIntro` and
`HasSeenPiratesIntro`, neither of which covers leader intros.

## 🔴 Trap 2 — the client exited cleanly mid-session, and the cause is NOT established

After the load, an end-turn was issued to build a genuine mid-game save:

```
turn before      = 1
UI.CanEndTurn()  = true
RequestAction ok = true err=nil
```

Within ~6 s the client was **gone**. What is known, and what is not:

- **It did not crash.** No dump was produced (`/tmp/dumps` holds only an unrelated 10:17 file), the
  game flushed its logs normally, and Steam logged an ordinary teardown —
  `Removing process 841328 ... Shutdown`. This is the signature of a clean quit.
- **The cause is undetermined.** Two candidates, neither confirmed: the synthetic `Return`/`space`
  sent moments earlier may have left a menu focused and activated a default button, or
  `ACTION_ENDTURN` was issued from a UI state that does not accept it. **I am not asserting either.**

The operational lesson stands regardless and is the important part:

> **Synthetic input leaves the client in a UI state we did not observe.** `send_input` reporting `ok`
> says a key was dispatched, not that the client is where we think it is. Any input step must verify
> the expected screen *before* and *after* the key — the same boundary rule that caught the capture
> and input defects, applied to UI state rather than to a return value.

Blind key sequences against a live client are not safe, and a sequence that "did nothing" visible
may still have moved focus.

## 🔴 Trap 3 — `steam://rungameid/289070` can silently resolve to Remote Play streaming

Relaunching after the exit produced a window titled
**`Sid Meier's Civilization VI (DX11) [Streaming]`** — and **no local `Civ6` process, no tuner.**

```
863827  .../ubuntu12_64/streaming_client --universe 1 --realm 1 --steamid ... --gameid 289070 --appid 289070 --server 192...
```

Steam resolved the launch to **Remote Play streaming from another machine on the LAN** instead of
running the local native build. `(DX11)` is the giveaway: the native Linux build is Vulkan/Metal-era
Aspyr, so a DX11 title bar means the frames are coming from a Windows host.

This is a **live-node availability hazard**, and a nastier one than the Steam dependency already
recorded, because it *looks like the game launched*: there is a window with the right name showing
real gameplay. A naive readiness check — "is there a window titled Civilization VI?" — passes while
the tuner never binds and no local process exists.

**Detection:** require a local process (`pgrep -x Civ6`) **and** a bound tuner port, never a window
title. The streaming client also holds the app session, so further `rungameid` launches will not
start the local build until it is closed.

## 🟡 Trap 4 — `doctor` reports `capture path : none` on a host where capture works

After merging `origin/002-civ-playing-harness` (which fixed `tier UNSUPPORTED` → **`SUPPORTED`**),
`civsim doctor` still prints:

```
platform          : ok  (linux/x11 ..., tier SUPPORTED)
capture path      : none (runs will be visually degraded)
provider key      : MISSING
```

on the same host where `capture_window()` had just returned three real 1920x1200 BGRA8 frames.

**Reported, not fixed** — `operator/doctor.py` is outside this node's owned paths. It may be
legitimate (no client was running at that moment, so there may be no window to resolve), but if so
the wording is misleading: it states a property of the *host's capture path* while measuring
something that requires a *running client*. Worth one check on the owning side, because a host that
reports `capture path: none` when its capture path is fine is the same class of false signal as a
guard with no caller.

**`provider key : MISSING` is real on this host too** — no `OPENROUTER_API_KEY` in the environment
and no secrets file in the repo. Both hosts are blocked on this for a decision-making run; it is an
owner action, not a task.
