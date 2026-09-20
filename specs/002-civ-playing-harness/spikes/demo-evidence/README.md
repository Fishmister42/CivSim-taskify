# Windows live demo — evidence

2026-09-20. Civilization VI `1.0.12.68 (1023995)`, Steam/DX11, **BBG 7.5.0 active**, Windows 11.

> **Decisions were scripted, not model-generated.** There is no OpenRouter key on this machine and
> **no model call was made**. What these artifacts show is the *harness* driving a real client — its
> Nexus transport, Lua execution, window-scoped capture, and synthetic-input path. They do **not**
> show an agent playing Civilization, and must not be presented as doing so.

## The demo

| File | What it shows |
|---|---|
| [`harness-ends-a-turn.gif`](./harness-ends-a-turn.gif) | The headline. Seven window-scoped frames while the harness issues `UI.RequestAction(ActionTypes.ACTION_ENDTURN)` and the game advances. |
| [`key-01-before.png`](./key-01-before.png) | Turn 8, researching Sailing. |
| [`key-02-during.png`](./key-02-during.png) | Mid turn-change. |
| [`key-03-after.png`](./key-03-after.png) | Turn 9 — the World Tracker has moved on to Pottery. |
| [`end_turn_demo_results.json`](./end_turn_demo_results.json) | `turn_before: 8`, `turn_after: 9`, `advanced: true`, `model_calls: 0`. |
| [`end_turn_demo.py`](./end_turn_demo.py) | The script. |

The turn advance is asserted on the **far side** — the turn counter read back out of the game — not
on the fact that the end-turn call returned. The client's own telemetry corroborated it:
`OnGameTurnStarted: Turn 9`.

## R6 capture hygiene

See [`../r6-capture-hygiene-windows.md`](../r6-capture-hygiene-windows.md).

| File | What it shows |
|---|---|
| [`r6-occluded-window-scoped.png`](./r6-occluded-window-scoped.png) | Full-resolution capture taken with two large always-on-top windows verified stacked **above** the client. Neither appears. |
| [`r6-baseline-clean.png`](./r6-baseline-clean.png) | The same window immediately before the occluders were raised (downscaled). |
| [`r6-crop-FF00FF.png`](./r6-crop-FF00FF.png), [`r6-crop-00FF00.png`](./r6-crop-00FF00.png) | The two occluder rectangles cropped out of the capture: game map, 0 matching pixels. |
| [`r6-inframe-fps-overlay.png`](./r6-inframe-fps-overlay.png) | The one **failing** requirement — a third-party FPS overlay composited inside the client's own frame, which window scoping cannot exclude. |

**Every capture here is window-scoped.** No root or full-screen grab was taken by the harness or by
any spike script, and there is no code path in them that would (Principle I).

## Not published here

A 973 MB OBS full-desktop screen recording exists at `C:\Users\<owner>\Videos\2026-09-20 14-56-03.mkv`.
It is **deliberately not committed**: it is a Display Capture of the owner's whole desktop and shows
unrelated windows. It is a human-facing artifact only. The clean, window-scoped GIF above is the
publishable version.

`obs-scene-ORIGINAL-backup.json` and `obs-basic.ini-ORIGINAL-backup` are the owner's untouched OBS
configuration, kept so the changes this session made can be reverted.
