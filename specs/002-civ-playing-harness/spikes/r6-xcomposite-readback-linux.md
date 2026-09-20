# Spike R6 / T052 — XComposite pixmap readback (Linux)

**Status:** ✅ Implemented and verified on real hardware.
**Date:** 2026-09-20
**Host:** X11 / Cinnamon, Mutter (Muffin) compositing, `Composite` 0.4, depth-24 TrueColor,
python-xlib 0.33.

`LinuxHostPlatform.capture_window()` was the last stub between the harness and images reaching the
agent on any platform. It now returns real pixels.

## Verdict

| Claim | Result |
|---|---|
| `NameWindowPixmap` reachable from python-xlib | ✅ Yes — `Xlib.ext.composite.name_window_pixmap` |
| Pixmap readback produces correct pixels | ✅ Yes — verified against known colours |
| Channel order correct (no silent R/B swap) | ✅ Yes — verified, see below |
| Format decodable by the parity screening gates | ✅ Yes — `BGRA8` |
| Occlusion-immune through the harness's own path | ✅ Yes — evidence below |
| Cost at Civ-like resolution | ✅ ~79 ms for 1920×1200 (9.2 MB) |
| **Verified against Civilization VI itself** | ❌ **No — see "What this does not cover"** |

## The sequence that works

```python
display = Display()
composite.redirect_window(xwindow, composite.RedirectAutomatic, onerror=err)  # REQUIRED
display.sync()
pixmap = composite.name_window_pixmap(xwindow, onerror=err)
display.sync()
image = pixmap.get_image(0, 0, width, height, X.ZPixmap, 0xFFFFFFFF)
pixmap.free()
```

Three findings shape that code, each measured rather than reasoned.

### 1. A running compositor is NOT sufficient — this client must redirect the window itself

The natural assumption is that because Muffin already composites every window, the off-screen pixmap
already exists and can simply be named. **It cannot.** Measured directly:

```
[A] name_window_pixmap WITHOUT our own redirect
  name_window_pixmap    : FAILED -> BadMatch

[B] redirect_window(RedirectAutomatic) then name_window_pixmap
  redirect_window       : OK
  name_window_pixmap    : OK
```

Muffin redirects root's *subwindows* (`CompositeRedirectSubwindows`), which does not satisfy the
per-window redirect `NameWindowPixmap` requires. `RedirectAutomatic` is the correct mode: the server
keeps presenting the window on screen normally, so the client's own display is unaffected.

### 2. python-xlib reports X errors ASYNCHRONOUSLY — and this one bites hard

Both calls **return an object even when the server rejects them**. The first version of this probe
printed:

```
  name_window_pixmap  : OK (compositor's redirect sufficed)     <-- a lie
  ...
  get_image           : FAILED -> BadDrawable <Resource 0x07800000>
```

The `BadMatch` from finding (1) surfaced only as an out-of-band print from python-xlib's default
error handler, while the code carried on holding a pixmap id the server had never created. The
failure then reappeared much later as a confusing `BadDrawable` on an unrelated-looking request.

**Every request in the adapter therefore carries an explicit `Xlib.error.CatchError` plus a
`sync()`.** Without that, `capture_window` reports a confident, wrong success — the single worst
outcome available to this function, and exactly the class of bug that a dimensions-only test would
never catch.

### 3. A window gets a NEW off-screen pixmap on every map and resize

From python-xlib's own docstring for `name_window_pixmap`:

> the window will get a new off-screen pixmap every time it is mapped or resized, so to keep track of
> the contents you must listen for these events and get a new pixmap after them

The adapter therefore names a **fresh pixmap per capture and frees it after**, rather than caching
one. At ~79 ms for a full 1920×1200 frame, per-capture naming is not a cost worth optimising against
correctness.

## Pixel format — verified, not assumed

A silent red/blue swap would satisfy every size assertion while corrupting every image the agent is
ever shown. So it was measured with known colours rather than reasoned from `image_byte_order`:

| Window background | First 4 bytes returned | Reading |
|---|---|---|
| `#ff0000` (pure red) | `00 00 ff ff` | B=00 G=00 R=ff → **BGRX** |
| `#3366cc` | `cc 66 33 ff` | B=cc G=66 R=33 → **BGRX** |

`image_byte_order` is `0` (LSBFirst) on this host. Depth-24 ZPixmap therefore packs as B,G,R,pad,
which the adapter emits as **`BGRA8`** — a format already in the screening gates'
`_RAW_MODE_MAP`. The fourth byte is X padding, **not meaningful alpha**; the gates drop it when
converting to RGB, so its value is irrelevant.

**MSBFirst is refused rather than guessed at.** The adapter has only been verified against LSBFirst
and returns `failed` with a clear reason on an MSBFirst server, instead of emitting plausible pixels
with swapped channels.

## Occlusion immunity, through the harness's own pipeline

T099 established occlusion immunity using `import`. This re-establishes it through
`LinuxHostPlatform.capture_window()` itself — the code the agent will actually depend on.

Two overlapping windows; the occluder raised above the target so it covers a **420×260 px** region of
it on screen:

```
geometry:
  target   0x0780000a (130,192) 760x520
  occluder 0x07a0000b (310,352) 420x260  [raised above target]
  overlap  420x260 px of the target is covered on screen

frames captured through LinuxHostPlatform.capture_window():
  target    rect=(130,192,760x520) format=BGRA8 -> harness-xcomposite-target.png
  occluder  rect=(310,352,420x260) format=BGRA8 -> harness-xcomposite-occluder.png
```

The target frame is **complete and unobstructed** — the full clock face, with no trace of the red
occluder sitting on top of it:

![target window captured through the harness while occluded](r6-evidence/harness-xcomposite-target.png)

Reproduce with `python specs/002-civ-playing-harness/spikes/capture_evidence.py`.

**No root or screen grab appears anywhere in that script**, including to illustrate the overlap. A
root grab is the FR-025 parity breach this path exists to avoid
(`r6-evidence/root-scoped-same-region-LEAKS.png`), and it would also drag the operator's unrelated
windows into a committed artefact. The overlap is evidenced by geometry and stacking order instead.

## Preflight: `capture_preconditions()`

The two constraints T099 asked to be carried in are now code, as
`LinuxHostPlatform.capture_preconditions()`:

```
preconditions:
  Composite extension  : True
  compositing manager  : True
  unredirect-fullscreen: False
  can_capture          : True
```

- **Compositing is detected by selection ownership of `_NET_WM_CM_Sn`, never by
  `XDG_SESSION_TYPE`.** That variable names the session protocol, not whether a compositor runs, and
  "X11 with no compositor" is precisely the case where the only thing still producing an image would
  be a root grab. `can_capture` is `False` there, and capture reports `unavailable`.
- **`unredirect-fullscreen-windows` is read** from `org.cinnamon.muffin`, falling back to
  `org.gnome.mutter`. `True` and *unknown* each raise a warning: a compositor that unredirects a
  fullscreen client destroys its off-screen pixmap, so capture fails or silently freezes on a stale
  frame. `None` is reported as a real answer — a desktop that does not publish the key has not been
  shown safe *or* unsafe.

This is additive to the `HostPlatform` port, which has no preflight hook. **A port-level preflight
method is the right long-term home** — flagged for the owning side rather than changed here.

## What this does not cover

- **Civilization VI itself has not been captured through this path.** The client could not be
  launched during this work — see `steam-dependency-linux.md`. Everything above was verified against
  ordinary X11 windows, which the XComposite path treats identically, but *identically in principle*
  is not the same as measured. **The first real Civ frame through this pipeline remains outstanding**
  and should be treated as the acceptance test for T052's capture branch.
- **No fullscreen test.** `unredirect-fullscreen-windows` is `false` on this host, so a fullscreen
  client *should* stay redirected — but that is inference. The game ran windowed in every prior spike.
- **Wayland remains unexecuted.** The `xdg-desktop-portal` branch is untouched by this work. A Linux
  pass is not full Linux coverage.
- **No test under GPU/driver-level overlays** (MangoHud, driver HUDs), which composite outside the
  window's own pixmap and could land inside the frame the way Steam's FPS counter did.
- Repeated redirect/unredirect churn: the adapter opens and closes a `Display` per capture, which
  releases its redirect each time. Whether that causes any visible flicker on a live client is
  **not measured**, and needs a human observer on a real game window to settle.

## Tests

`tests/live/test_linux_xcomposite_capture.py` (5 tests, `-m live`, excluded from the default run).
They target throwaway windows, not Civ VI, and so run on any compositing X11 host:

```
test_capture_returns_a_frame_of_the_windows_own_size           PASSED
test_captured_pixels_carry_the_windows_actual_colour           PASSED
test_frame_format_is_decodable_by_the_parity_screening_gates   PASSED
test_capture_of_a_destroyed_window_fails_without_falling_back_to_a_screen_grab PASSED
test_preconditions_report_this_hosts_real_capture_capability   PASSED
```

Default suite after the change: **961 passed, 2 skipped**.
