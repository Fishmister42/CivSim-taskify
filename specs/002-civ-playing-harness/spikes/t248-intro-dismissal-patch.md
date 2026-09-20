# T248 — the intro-screen dismissal patch, for transcription into `LuaSaveLoader`

Published for the Windows side to transcribe into `src/civsim_harness/saves/load_game.py`, per the
ruling that the demo records against landed code only. **This is the shape that was measured to
work** (`tests/live/test_production_save_loader.py`: as-shipped FAIL after 160.4 s, with this PASS in
40.1 s), not a design.

## The one thing to get right

**It must be a retry loop, not a single timed press.** The obvious implementation — wait a fixed
delay after the port closes, press Escape once — **was tried first and measured to fail.** The reason
is structural and will not go away:

- the tuner port closes the **instant** the load is issued;
- the intro screen only appears once the load **finishes**, tens of seconds later;
- and the intro screen **cannot be observed through the tuner, because the tuner is exactly what it
  is holding closed.**

There is no signal that says *"the intro screen is up now."* The only observable is **the port coming
back**, which happens *after* dismissal has already succeeded. So the press has to be retried against
the same poll loop that is already waiting for the port.

Measured: **7 presses** at 5 s intervals on one run, **1 press** on another (the count depends on how
long map generation or loading takes). Early presses land during loading and are inert.

## The patch

`_await_phase` already polls exactly when we need to act — its `except` branches are entered
precisely when the port is unreachable. Give it an optional hook and call it there.

```python
    async def _await_phase(
        self,
        *,
        predicate: Any,
        timeout_s: float,
        waiting_for: str,
        save: SavePoint,
        hint: str | None = None,
        on_unreachable: Callable[[], None] | None = None,   # NEW
    ) -> StateIndices:
        ...
        while True:
            try:
                if needs_reconnect:
                    indices = await client.reconnect()
                    needs_reconnect = False
                else:
                    indices = await client.refresh_state_indices()
            except (NexusError, PreflightError) as exc:
                last_error = f"{type(exc).__name__}: {exc.message}"
                needs_reconnect = True
                if on_unreachable is not None:      # NEW -- the port is closed right now
                    on_unreachable()
            except OSError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                needs_reconnect = True
                if on_unreachable is not None:      # NEW
                    on_unreachable()
            else:
                ...
```

The dismisser itself:

```python
    #: Measured: `Escape` dismisses the leader-intro screen. `Return` and `space` do NOT
    #: (spikes/r7-live-client-session-linux.md). The screen appears on EVERY load, not only
    #: on saves taken before it was first dismissed, and holds the tuner port closed
    #: indefinitely -- 150s measured with no recovery.
    INTRO_DISMISS_KEY = "Escape"

    def _dismiss_intro_screen(self) -> None:
        """Press Escape once, if we have a host to press it with.

        Called only from the unreachable branch of `_await_phase`, i.e. only while the
        tuner port is closed. Once the port is back the poll succeeds and this is never
        called again -- so no keystroke can land in the running game.
        """
        if self._host is None:
            return
        try:
            self._host.send_input(
                [InputEvent(kind=InputEventKind.key_press, key=INTRO_DISMISS_KEY)]
            )
        except Exception:  # never let a failed keystroke break the wait
            pass
```

Wired at step 4a only — **not** at the exit-to-menu wait, where no intro screen exists:

```python
        await self._await_phase(
            predicate=lambda st: st.has_game_states,
            timeout_s=self._load_timeout_s,
            waiting_for=(...),
            save=save,
            hint=hint,
            on_unreachable=self._dismiss_intro_screen,      # NEW
        )
```

## 🔴 One gap this exposes, and it will ship broken without a decision

**`send_input` has no window targeting.** XTest delivers to whatever window currently holds focus, so
the loader as patched will dismiss the intro screen **only if Civilization VI happens to be
focused.** If the operator has another window focused, the `Escape` goes there instead — and on this
host that could be a browser.

My live test worked around it by calling `xdotool windowactivate` before each press. That is not
available through the port: **`HostPlatform` has no focus/activate operation.** So one of:

1. **Add a focus operation to the port** (`focus_window(window)` or similar) and have the loader call
   it before each press. Cleanest, and it also serves any later input consumer.
2. **Document `send_input` as requiring the game to already hold focus**, and have the loader's
   preflight assert it. Cheaper, but it makes unattended runs fragile in exactly the way that is hard
   to debug later.

I'd take (1), and it is small — the Linux half is one `xdotool windowactivate` equivalent, which the
adapter can do directly since it already resolves the window. **Flagging rather than choosing: the
port is yours.**

Note this is the same class as the `window_provider` gap — an input primitive that works perfectly in
isolation and targets nothing in production.

## Re-verification, once it lands

`tests/live/test_production_save_loader.py` already drives the real `LuaSaveLoader` and reports
as-shipped vs patched. Once T248 lands I re-run it against the production version, unmodified, and
post the transcript. Expected: the ATTEMPT-1 path that currently fails becomes the passing one, in
roughly 40 s.
