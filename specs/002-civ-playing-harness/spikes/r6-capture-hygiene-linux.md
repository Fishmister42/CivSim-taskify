# Spike R6 / T099 — Capture hygiene (Linux)

**Feature**: `002-civ-playing-harness` · **Task**: T099 · **Executed**: 2026-09-20
**Host**: Linux, **X11 / Cinnamon**, WM = `Mutter (Muffin)` (compositing) · 4480×1440, two monitors
**Client**: `1.0.12.9 (564030)`, window `1920×1200` at `+2560+0`

**This is a Linux/X11 result and says nothing about Windows, macOS, or Wayland.** See
[Wayland](#wayland-is-not-validated-by-this-spike).

## Verdict

**Window-scoped capture PASSES the occlusion requirement. One in-frame contaminant remains, and it
is not the game's.**

| Requirement (FR-025, FR-030, Scenario 3) | Result |
|---|---|
| A console placed over the client is absent from the capture | ✅ **Pass** — proven |
| Unrelated windows over the client are absent | ✅ **Pass** — proven |
| Linux desktop chrome (panels, toasts) excluded | ✅ **Pass** — excluded by construction |
| No harness-irrelevant chrome inside the frame | ❌ **Fail** — the Steam FPS overlay |

Until the overlay is cleared, Linux captures carry **known contamination** and runs on this host
should be treated accordingly.

## The occlusion test

Capture method: `import -window <client_window_id>` — a window-scoped `XGetImage`, not a screen grab.

1. Client raised, baseline capture taken.
2. An `xmessage` dialog launched **on top of** the client (confirmed stacked above it: `wmctrl`
   reports it at `+3010+472`, inside the client's rectangle).
3. Client re-captured **window-scoped**, and the screen captured **root-scoped**, back to back.
4. Both cropped to the identical region the dialog occupied.

### Window-scoped capture of the occluded region

Pure game content — main menu entries (`Benchmark`, `World Builder`, `Exit to Desktop`), the Julius
Caesar banner, the 2K Account badge. **No trace of the dialog.**

### Root-scoped capture of the same coordinates

The `xmessage` dialog with its `okay` button — **and, below it, a terminal with harness command text
legible in it**:

```
ulator 2>&1; echo "--- civ state ---"; pgrep -x Civ6 >/dev/null && echo "Ci
```

That is precisely the FR-025 failure: the harness's own console, readable, inside an image that would
otherwise have gone to the agent. It is worth keeping this artifact, because it is the concrete
demonstration that **full-screen capture is not an acceptable path on this platform** — not a
stylistic preference but a parity violation that a screening pass would then have to catch after the
fact.

**Conclusion: the capture path must be window-scoped. A root/screen capture must never be used, even
as a fallback.** A degraded run under FR-050 is a recorded, honest state; a screen grab is a parity
breach.

## Capture works while the client is minimized — with a caveat that matters

Tested because an unattended run cannot assume the window stays raised, and because the owner uses
this machine.

Minimized (`xdotool windowminimize`), then captured window-scoped: **the capture succeeded and
returned valid, current game content** at normal brightness (mean 0.279 vs ~0.27–0.31 unoccluded).

This is a genuinely useful property — the harness does not need to own the screen, and the owner can
use the desktop while a run proceeds.

> ⚠️ **Unverified and dangerous if wrong: whether a minimized client keeps *rendering new frames*.**
> Many games stop rendering when minimized. If Civ VI does, the redirected pixmap would return the
> **last rendered frame**, which still looks like a perfectly valid capture. That is the exact
> failure Scenario 8a exists to prevent — the agent choosing its next move while looking at a board
> that predates its last one — except arriving through the capture path rather than through reuse.
>
> **This must be settled before any unattended run is allowed to minimize the client.** The test is
> cheap: minimize, mutate game state through Lua, capture, and check the image reflects the mutation.
> Until then, keep the client unminimized during runs.

## The one in-frame contaminant: Steam's FPS overlay

A green `60 FPS` readout in the client's top-right corner appears in every capture.

**It is not the game and not the debug menu** — it survives `EnableDebugMenu 0` (see
[principle-i-debugmenu-linux.md](./principle-i-debugmenu-linux.md)). It is the Steam overlay's FPS
counter: `"InGameOverlayShowFPSCorner" "2"` in Steam's `localconfig.vdf` (2 = top-right, 0 = off).

**Window scoping cannot exclude it**, because Steam composites it *inside the client's own frame*
rather than drawing a separate window over it. That is the important generalisation:

> A third party can composite content into the client's frame without the client knowing. Discord,
> MangoHud, and driver overlays all land the same way. The screening profile should treat *"foreign
> content inside the game frame"* as a class to detect, not special-case this one overlay.

Not changed by this spike — it is a Steam-client-wide setting affecting every game on the owner's
account, so it is his decision. One-step fix in Steam → Settings → In Game → *In-game FPS counter* →
Off, or set the value to `"0"` with Steam closed.

## Mechanism, and the dependency it creates

Occlusion-immunity here is a property of the **compositing window manager**, not of `import` or of
X11 in general. Under a compositing WM (`Mutter (Muffin)`, with the `Composite` extension present at
version 0.4), windows are redirected to offscreen pixmaps, so a window-scoped `XGetImage` reads the
client's own backing content rather than whatever is on screen in that rectangle.

**Without compositing, X11 does not store obscured window contents, and the same call would return
the occluding window's pixels.** The pass above would silently become a failure — captures that look
fine but contain other windows.

**Therefore the host adapter must not assume this.** Recommended: verify compositing at preflight
rather than infer it from `XDG_SESSION_TYPE=x11`, and treat "X11 without a compositor" as a distinct
capability case — not `VALIDATED`. This is a concrete requirement for `host/linux` (T052) and is
**not** currently implemented.

## Wayland is not validated by this spike

This host is X11 (`XDG_SESSION_TYPE=x11`, `DISPLAY=:0`, no `WAYLAND_DISPLAY`). Nothing here tests the
Linux adapter's Wayland branch, including:

- `find_game_window` returning `None` on Wayland by design
- the `xdg-desktop-portal` ScreenCast path
- the requirement to **report synthetic input as unavailable rather than attempting it**

**A Linux pass must not be recorded as full Linux coverage.** The Wayland branch remains unexecuted
code, and a Wayland host should not be assumed to reach the same tier.

## What this spike does not cover

- **Notification toasts were not tested.** A toast is a separate override-redirect window, so by the
  same mechanism it should be excluded — but that is reasoning, not evidence, and toasts are exactly
  the kind of thing that is drawn differently.
- **No fullscreen/exclusive mode test.** The client ran windowed (`1920×1200` at `+2560+0`).
  A compositor that unredirects fullscreen windows would **defeat the occlusion immunity
  demonstrated above** — the single most likely way this pass stops holding.

  On this host that risk is currently absent: `org.cinnamon.muffin unredirect-fullscreen-windows` is
  **`false`**, so fullscreen windows stay redirected. But it is a **user-changeable desktop setting
  with no relationship to the harness**, and flipping it would silently turn the pass above into a
  failure. Preflight should read it alongside the compositing check rather than trust it.
- No test of capture under GPU/driver-level overlays.
- Capture cost and latency were not measured.
