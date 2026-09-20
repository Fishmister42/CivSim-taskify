# Spike R6 / T099 — Capture hygiene (Windows)

**Feature**: `002-civ-playing-harness` · **Task**: T099 · **Executed**: 2026-09-20
**Host**: Windows 11 (26200), NVIDIA RTX 4070 SUPER · single 2560×1440 primary monitor
**Client**: `1.0.12.68 (1023995)`, Steam/DX11, window `2560×1440` at `+0+0`, **in game at turn 8**
**Capture path**: Windows.Graphics.Capture (research R6 **rank 1**), window-scoped

**This is a Windows result and says nothing about Linux, macOS, or another Windows host with a
different overlay set.**

## Verdict

**Window-scoped WGC capture PASSES the occlusion requirement. One in-frame contaminant remains, and
it is not the game's.**

| Requirement (T099, FR-025, FR-030, Scenario 3) | Result |
|---|---|
| A console placed over the client is absent from the capture | ✅ **Pass** — proven, 0 of 600,000 pixels |
| Unrelated windows over the client are absent | ✅ **Pass** — proven, 0 of 520,000 pixels |
| WGC capture border does not appear (`IsBorderRequired = false`) | ✅ **Pass** — requested false; no border in frame |
| No harness-irrelevant chrome inside the frame | ❌ **FAIL** — an FPS overlay is composited into the client's own frame |

**Three of four pass. The fourth fails, and it fails the same way it failed on Linux** — a third
party compositing into the client's frame, which window scoping cannot exclude by construction.

## The occlusion test

Two `overrideredirect`, always-on-top windows were placed over the client: a magenta `#FF00FF`
"HARNESS CONSOLE" panel at `(400, 350) 1000×300`, and a green `#00FF00` "UNRELATED WINDOW" panel at
`(400, 760) 1000×260`. Both carry legible text, but the assertion keys on **flat colour**, which
cannot be confused with Civilization VI's art.

### Stacking was verified, not assumed

This is the step that makes "the occluder is absent" mean anything. Enumerating top-level windows in
z-order (`GetWindow(GW_HWNDFIRST/GW_HWNDNEXT)`), at capture time:

```
game zpos=7
occluders=[{'hwnd': 853852,   'zpos': 4, 'rect': (400, 350, 1400, 650),  'above_game': True},
           {'hwnd': 17631136, 'zpos': 5, 'rect': (400, 760, 1400, 1020), 'above_game': True}]
all_above_game = True
```

Lower z-position is nearer the front, so both panels were genuinely **in front of** the client. The
spike refuses to proceed if this check fails — an occluder that was behind the window all along would
make the test vacuous, and that is exactly how this kind of pass silently becomes meaningless.

### Result

```
occluder #FF00FF: matching fraction 0.00000000 -> ABSENT (pass)
occluder #00FF00: matching fraction 0.00000000 -> ABSENT (pass)
VERDICT: PASS - no occluder leaked
```

Zero matching pixels in either rectangle. The captured frame shows the game map continuing
uninterrupted through both regions:
[`demo-evidence/r6-occluded-window-scoped.png`](./demo-evidence/r6-occluded-window-scoped.png)
(turn 8, 3440 BC, a "Research Completed — Sailing" popup, Setúbal Bay and the Guadiana River). Clean
baseline taken immediately before the occluders were raised:
[`demo-evidence/r6-baseline-clean.png`](./demo-evidence/r6-baseline-clean.png). Machine-readable
record: [`r6-raw-windows/r6_hygiene_results.json`](./r6-raw-windows/r6_hygiene_results.json).

The frame is real content, not a black screen: mean luma **71.7** occluded vs **71.7** baseline —
i.e. raising two large bright windows over the client changed the captured image by essentially
nothing, which is the result in one number.

> **No root-scoped comparison was taken, deliberately.** The Linux spike proved the leak with a
> root grab (`r6-evidence/root-scoped-same-region-LEAKS.png`). Repeating that here would mean taking
> a full-screen capture of the owner's desktop — the exact thing Principle I forbids. The
> demonstration already exists; it does not need doing twice. The z-order check above is what makes
> absence meaningful without it.

## The capture border

T099 names the WGC border specifically. `GraphicsCaptureSession.IsBorderRequired` is set **false**
for every capture this spike takes, and no border appears in any frame.

Two caveats, both honest:

- The implementation goes through the `windows-capture` binding (`draw_border=False`), which is that
  library's spelling of `IsBorderRequired`. **The property was not read back from the session
  object**, so this is "we asked for false and no border is visible", not "the system reported
  false". Weaker than the rule this project uses elsewhere — assert on what the far side agreed to.
  The visual absence across every frame is what carries the claim.
- On Windows 11 build 22000+, a capture started this way does not raise the yellow border. A host on
  an older build has no `IsBorderRequired` property at all and would be a **different capability
  case**, not a pass.

## The one in-frame contaminant: an FPS overlay

Every frame carries a small green FPS readout in the **top-left** corner — `152 FPS`, `58 FPS`,
`180 FPS` across captures. Cropped and enlarged:
[`demo-evidence/r6-inframe-fps-overlay.png`](./demo-evidence/r6-inframe-fps-overlay.png).

It is **not the game and not the debug menu**. The NVIDIA overlay (`NVIDIA Overlay.exe`) is running on
this host, and the Steam overlay injects into the client as well
(`GameOverlay: started gameoverlayui64.exe for game process 28896`). Either composites *inside the
client's own frame*, so **window scoping cannot exclude it** — the same mechanism, and the same
conclusion, as the Steam FPS counter the Linux spike found:

> A third party can composite content into the client's frame without the client knowing. Discord,
> MangoHud, NVIDIA and Steam overlays all land the same way. The screening profile should treat
> *"foreign content inside the game frame"* as a class to detect, not special-case one overlay.

This spike did **not** change the setting — it is an account/driver-wide preference belonging to the
owner, exactly as on Linux. Turn it off in GeForce Experience / NVIDIA App → *In-Game Overlay*, and in
Steam → Settings → In Game → *In-game FPS counter* → Off.

**Consequence:** until that overlay is off, or the screening profile detects foreign in-frame content,
Windows runs should proceed **visually degraded under FR-050**. The occlusion property is sound; the
frame is not yet clean.

## Mechanism, and the dependency it creates

Occlusion immunity here is a property of **Windows.Graphics.Capture** itself: WGC captures a window's
own composed content from DWM rather than reading the screen, so what is stacked above it is
irrelevant. Unlike the Linux result, this does **not** depend on a user-changeable desktop setting —
DWM compositing is not optional on Windows 11, so there is no Windows analogue of the Linux
"X11 without a compositor" hazard, and no preflight check is needed for it.

What *is* needed is a check that the rank-1 path is actually the one being used. Rank 3
(`PrintWindow`) has no such guarantee for a D3D-rendered client, and rank 2 (DXGI Desktop
Duplication) is occlusion-**sensitive** by design and must never be reached.

## `winsdk` cannot reach WGC on its own — a concrete correction to the adapter

`host/windows/adapter.py` returns `None` from `_capture_via_windows_graphics_capture` with the note
that the `IGraphicsCaptureItemInterop` factory "could not be confirmed a `winsdk` binding for". That
note is wrong in its premise and right in its conclusion, for a different reason:

- The interop factory **is** bound: `winsdk.windows.graphics.capture.interop.create_for_window`
  exists. `GraphicsCaptureItem.try_create_from_window_id(WindowId(hwnd))` also works and needs no
  COM interop at all.
- `GraphicsCaptureSession.is_border_required` is a real, settable property on the projection.
- **The actual blocker is the D3D device.** `Direct3D11CaptureFramePool.create_free_threaded` needs an
  `IDirect3DDevice`, which is only obtainable by passing an `IDXGIDevice` to
  `CreateDirect3D11DeviceFromDXGIDevice` (a plain C export returning an `IInspectable`). `winsdk`
  exposes no way to wrap a raw `IInspectable` pointer: `_winrt.Object` is not constructible
  (`TypeError: type 'Object' is not activatable`) and `IDirect3DDevice._from` rejects an integer
  (`TypeError: not a System.Object`). The ctypes half works; the projection will not accept its
  result. See [`r6-raw-windows/wgc_capture.py`](./r6-raw-windows/wgc_capture.py) for the attempt.

So rank 1 is reachable on Windows, but **not through `winsdk` alone**. This spike used the
`windows-capture` package (Rust-backed WGC). Whoever closes T050's capture half should either take
that dependency or write the `IInspectable` wrapping in a small extension — not leave the note as-is,
which implies the API is unavailable when it is the binding that is insufficient.

## What this spike does not cover

- **Notification toasts were not tested.** By the DWM mechanism they should be excluded, but that is
  reasoning, not evidence.
- **No exclusive-fullscreen test.** The client ran windowed-borderless at `2560×1440`. True exclusive
  fullscreen changes the presentation path and is the most likely way this pass stops holding.
- **`IsBorderRequired` was not read back** from the session (see above).
- **Minimized capture was not tested** on Windows. The Linux spike found it works there but flagged
  the "is it still rendering?" hazard; neither half is established here.
- **Capture cost and latency were not measured.**
- **The harness's own `capture_window` was not the path used.** `WindowsHostPlatform.capture_window`
  still attempts rank 1, gets `None`, and falls back to `PrintWindow`. This spike validates the WGC
  *path*; wiring it into the adapter is T050/T224 work and was left to the agent editing that file
  concurrently.
