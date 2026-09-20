# Spike R5 / T052 — `xtest.fake_input` via python-xlib (Linux)

**Status:** ✅ Driven and verified. **Three real defects found and fixed in `send_input`.**
**Date:** 2026-09-20
**Method:** all synthetic input injected into a nested `Xephyr` server, never the operator's
session. Delivery verified by reading `xev`'s event stream, not by trusting `InputResult.status`.

## Verdict

| Claim | Result |
|---|---|
| `xtest.fake_input` reachable via python-xlib | ✅ Yes |
| Keystrokes arrive as **real device events** | ✅ Yes — `synthetic NO` |
| Mouse positioning works | ✅ Yes — **root-absolute** coordinates |
| `send_input` dispatched what it was asked to | ❌ **No — three defects, now fixed** |
| Verified against Civilization VI | ❌ No — client unavailable, see `steam-dependency-linux.md` |

## `synthetic NO` — why XTest rather than `XSendEvent`

```
KeyPress event, serial 28, synthetic NO, window 0x200001,
    state 0x0, keycode 9 (keysym 0xff1b, Escape), same_screen YES,
```

XTest events enter the server's own input pipeline, so an application **cannot distinguish them from
a human's keystroke**. `XSendEvent` events arrive flagged `synthetic YES`, and games routinely ignore
those. This is the load-bearing reason the adapter uses XTest.

**The trade-off, which the harness must handle: XTest has no window targeting at all.** It injects at
the server level, into whatever currently holds focus. `send_input` takes no window parameter and
cannot take one. **If the Civ window is not focused, synthetic input goes to whatever is** — on a
shared desktop that means the operator's own windows. Flagged below.

## Three defects, each reporting `ok` while doing nothing

All three were measured against a live XTest server *before* being fixed. They share one shape:
**silent input loss reported as success** — the harness records that it acted, the game never saw the
event, and the divergence gets blamed on the client.

| Sent | Reported | Actually delivered |
|---|---|---|
| `key="F5"` (any key outside a 5-entry table) | `ok` | **nothing** |
| `kind=text, text="civsim"` | `ok` | **nothing** |
| `button="right"` | `ok` | **button 1** (left click) |

### 1. Unmapped keys were silently dropped

`_KEYSYMS` held five entries (enter/return/esc/tab), and any other key name hit a bare `continue`.
The table is now widened (space, backspace, delete, home/end, page up/down, arrows, modifiers,
F1–F12), and — more importantly — **an unknown key is now a reported `failed`, naming the key and
listing what is known**, rather than a no-op.

### 2. `InputEventKind.text` had no branch at all

The port defines `text` as an event kind; `send_input` never handled it. Sending `"civsim"` delivered
zero events and returned `ok`.

This is the event the bespoke save path most needs — **a save dialog wants a filename typed into
it**. Now implemented, with the shift level taken from the server's own keyboard mapping rather than
assuming a US layout:

```
Shift_L ↓  keysym 0x43, C   (state 0x1)
           keysym 0x69, i
           keysym 0x76, v
Shift_L ↓  keysym 0x53, S   (state 0x1)
           ...
Shift_L ↓  keysym 0x5f, underscore
           keysym 0x34, 4
           keysym 0x32, 2
Shift_L ↓  keysym 0x21, exclam
```

`CivSim_42!` types exactly, with Shift held only for the characters that need it. A character whose
keysym sits on neither the shifted nor unshifted level is **refused rather than guessed at**.

### 3. `button` was ignored entirely

Every click dispatched as button 1. Verified by sending `button="right"` and reading `button 1` back
off the wire. Now mapped (left/middle/right/scroll_up/scroll_down), with an unknown button reported
as `failed`.

After the fix, the same request delivers:

```
ButtonPress event, ..., state 0x0, button 3, same_screen YES
```

## Coordinates are ROOT-ABSOLUTE — and the port does not say so

```
root 0x210, subw 0x0, time 82165065, (398,298), root:(400,300),
```

Sending `x=400, y=300` positions the pointer at root `(400,300)`. `InputEvent.x/y` carry no
documented coordinate space, and this adapter interprets them as root-absolute because that is what
XTest requires.

⚠️ **This is a live ambiguity for callers, and the failure mode is quiet.** The Civ window on this
host sits at offset `2560,0` on the second monitor, and window-relative coordinates are in its own
`1920x1200` space. **A caller passing window-relative coordinates would click on the wrong
monitor entirely.** A regression test now pins the root-absolute behaviour so it cannot change
silently, but **the port should state the coordinate space explicitly** — flagged for the owning
side, not changed here.

## What this does not cover

- **Civilization VI has not received a single synthetic event from this adapter.** All of the above
  is a nested `Xephyr` server and `xev`. The client could not be launched
  (`steam-dependency-linux.md`). In particular, **whether Civ VI accepts XTest input at all is still
  unproven** — `synthetic NO` makes it very likely, but that is inference.
- **Focus management is not implemented anywhere.** Nothing in the harness ensures the Civ window is
  focused before `send_input` fires. On a shared desktop this is a real hazard, not a theoretical
  one; see the flag above.
- **No modifier-combination API.** `ctrl`/`alt`/`shift` exist as individual keys, but there is no way
  to express "Ctrl+S" as one event. The port has no field for it.
- Key auto-repeat, held-key durations, and input timing/pacing are unexamined.
- Wayland remains `unavailable` by design and untested.

## Tests

`tests/live/test_linux_xtest_input.py` (10 tests, `-m live`). **Each one starts its own `Xephyr`
server**, so the suite can never inject input into the operator's session:

```
test_keystrokes_arrive_as_real_device_events                          PASSED
test_text_events_actually_type_their_text                             PASSED
test_text_events_hold_shift_for_capitals_and_symbols                  PASSED
test_click_honours_the_requested_button                               PASSED
test_mouse_coordinates_are_root_absolute                              PASSED
test_undispatchable_events_fail_loudly_instead_of_being_dropped[x5]   PASSED
```

Default suite after the change: **961 passed, 2 skipped**. Live tier: **15 passed**.
