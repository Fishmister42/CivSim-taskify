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

## 🔴 The production `LuaSaveLoader` fails **every** load on this host — measured, with the fix

`tests/live/test_production_save_loader.py`, driving the shipped `saves/load_game.py` against the
live client. Its module docstring carries the open item honestly — *"This loader cannot click it"* —
and since R7 measured the intro screen appearing on **every** load, that open item is not a
contingency on this host. It is the normal path.

```
save='civsim__spike__t0001'  short timeout=150s (default is 300s)

=== ATTEMPT 1 - production loader, exactly as shipped ===
  RESULT: load() FAILED after 160.4s
    HarnessError: timed out after 150s waiting for the game states (GameCore_Tuner, InGame)
    to reappear after Network.LoadGame accepted save 'civsim__spike__t0001'

=== ATTEMPT 2 - same loader + send_input(Escape) retried until the port returns ===
    [dismisser] Escape #1..#7 -> ok
  RESULT: load() SUCCEEDED in 40.1s

  as shipped       : FAIL
  with one Escape  : PASS
  => the gap is exactly the missing keystroke
```

**As shipped it cannot complete a load — it can only time out.** With the 300 s default that is five
minutes per attempt, and every retry re-enters the same state. Note also that a failed attempt
**strands the client**: the intro screen holds the port closed, so the *next* attempt cannot even
connect, which is why the test has to press Escape before it can start at all.

### The fix is a retry loop, not a single press — and that distinction is the real finding

The obvious implementation — wait a fixed delay after the port closes, press Escape once — **was
tried first and failed** (attempt 2 of the earlier run, one press at +6 s: FAIL). The reason is
structural:

- the tuner port closes the *instant* the load is issued;
- the intro screen only appears once the load *finishes*, tens of seconds later;
- and the intro screen **cannot be observed through the tuner**, because the tuner is precisely what
  it is holding closed.

So there is no signal that says "the intro screen is up now." The only observable is **the port
coming back**, which happens *after* the dismissal succeeds. Dismissal therefore has to be a loop
that presses Escape and re-checks the port until it rebinds — seven presses on the measured run. The
early presses land during loading and are harmless; the loop exits without pressing once the port is
back, so it cannot leave a stray Escape in the running game.

A capture-based check (look for the intro screen in a frame) would be the alternative, and is worth
considering, since `capture_window()` keeps working while the tuner is closed.

### ✅ Checklist items this closes

- **The 300 s load bound is generous, not tight.** A successful load took **40.1 s** end to end on
  this hardware, *including* the dismissal loop — roughly 7.5× headroom. The failing path is what
  consumes the budget, not the loading.
- **Turn agreement post-load holds.** `load()` only returns after its own `_verify_far_side` /
  `_check_position` have compared the client against the `SavePoint`; it returned, so the position
  the client came up in matched the save's recorded `turn_number`.
- **Dismissal behaviour:** `Escape` only. Re-confirmed three separate times today, including twice
  as incidental recovery between attempts.

## ✅ Checklist item 4 — the full cycle runs, and it exonerates the end-turn

`tests/live/test_full_turn_cycle.py`. Persist is deliberately ordered **before** the risky step, so a
lost client would still leave a restore point and still prove the save half.

```
=== [1] OBSERVE ===              turn = 1   canEndTurn = true   gold = 10
=== [2] PERSIST ===              Network.SaveGame ok=true ret=true
                                 new files on disk: ['civsim__cycle__probe.Civ6Save']
                                 size=681083 bytes  -> persist half PASSES
=== [3] END TURN ===             RequestAction ok=true err=nil
    t+ 1s .. t+20s               Civ6=alive  tuner=up      (every second)
=== [4] VERIFY (later command) ===
                                 turn now   = 2
```

**load → observe → end turn → persist → verify all pass**, with the turn advance read back in a
*later* command rather than from the action's own return (it returns nil; the action is async).

### This narrows Trap 2 substantially

**The earlier clean exit did not reproduce.** Twenty seconds of per-second liveness polling after an
`ACTION_ENDTURN` issued from a *verified-clean* in-game screen: alive throughout, tuner up
throughout, turn advanced 1 → 2.

So `ACTION_ENDTURN` **on its own is not the cause**. One non-reproduction does not make it
unconditionally safe, but it does move the remaining weight onto the other candidate: the earlier
run had **blind `Return`/`space` presses** fired at the intro screen moments before, and those are
the plausible menu-activating input. The rule already drawn from it stands and is now better
supported — verify the screen before firing keys, and prefer keys whose effect you have measured.

### 🟡 New gotcha: the tuner refuses a reconnect for ~2 s after the previous socket closes

The first run of this test died with `ConnectionRefusedError` on its *second* command. The client was
alive and the port was listening; it simply will not accept a new connection immediately after the
previous one closes. This is the known "one connection at a time" rule with a timing tail nobody had
recorded.

**A refused connection right after a clean close is expected, not a dead client** — the same
misreading the loader already guards against mid-load. Retry with a short backoff (measured: refused
immediately, fine ~2 s later). Worth checking `NexusClient.reconnect()` handles it, since any
command sequence that opens and closes per command will hit this constantly.

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
