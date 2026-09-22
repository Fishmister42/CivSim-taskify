# Frame retro-audit: did any contaminated frame reach a model?

**Date**: 2026-09-22 · **Scope**: every capture in `civsim-match-store.db` that reached, or may have
reached, a model · **Verdict**: **0 contaminated, 301 clean, 0 unknown** among the frames that reached
a model. **The release obligation is discharged on evidence; the control that was supposed to
discharge it is still broken.**

## The decisive measurement

Across 20 frames drawn one per run — 20 different games, seeds, turns and camera positions — the
fraction of pixels that stay unchanged (per-channel max−min ≤ 6) is **0.0000**, and **zero** of the
1440 40×40 blocks in the frame are more than 90% static. The top 32 rows, where a desktop panel would
sit, are **0.13%** static; the bottom 32 rows are 0.04%.

A FireTuner window, a console, a debug HUD, a GNOME top bar or a notification toast is by construction
a region that does not change when the game state does. No such region exists anywhere in these
frames. That is not "we looked and saw nothing" — it rules the failure mode out arithmetically, and it
agrees with a frame-by-frame visual inspection of every distinct frame in the set.

## What reached a model

| Quantity | Count |
|---|---|
| Captures in the store | 784 |
| `screening_status = screened_clean` (blob retained) | 363 |
| `shown_to_agent = True` | 301 |
| …of those, joined to a `model_calls` row with `image_count ≥ 1` | **288** (dispatch-confirmed) |
| …of those, flagged shown with **no** `model_calls` row at all | 13 (dispatch **unconfirmed**) |
| Model calls carrying an image (`image_count = 1`, all `decision_returned`) | 288 |
| Model calls with an image but no shown capture | 0 |
| Distinct image blobs behind the 301 captures | **188** |

Every one of the 301 is `capture_path = xcomposite`, `view_declaration_id = views.world`, on a host
recorded as `os = linux`, `session_type = x11`, across **20 runs** and both dates (290 on 2026-09-21,
11 on 2026-09-22). All 188 blobs are PNG, **1920×1200**, RGB — one single geometry, no frame larger
than, smaller than or offset from any other.

**The 13 dispatch-unconfirmed captures** (run-4c0b8fb4 ×2, run-a06e8e68 ×10, run-1091122b ×1) are the
rows commit `51c2b1a` (T271) independently measured and fixed: `shown_to_agent` was written one line
*before* the provider dispatch it claimed to describe, so a turn interrupted inside `complete()` left
the flag set with its evidence gone. Whether those frames were transmitted is unknowable from the
record. They were audited anyway, on the conservative reading that they may have reached a model, and
they are clean — so the ambiguity does not change any verdict here.

## Method

Read-only throughout. The store was copied to scratch and opened `mode=ro`; the repo-root
`civsim-match-store.db` mtime was `2026-09-22 08:41:17.913444955 -0400` before this work and
unchanged after. The Civ VI client, the tuner, `CivSolver-live/` and `tests/live/**` were never
approached.

1. **Join.** `captures.shown_to_agent` → `decision_step_id` → `model_calls.image_count`, giving both
   the strict set (288) and the conservative union (301). Blobs resolved content-addressed from
   `blobs/<sha[:2]>/<sha>`; all 188 present, none missing.
2. **Programmatic pass, all 188 frames.** Dimensions and format; per-channel whole-frame variance;
   the live detector's own corner-variance test re-run offline; the live border-ring test re-run
   offline; per-row luminance statistics for the top and bottom 48 rows; and the cross-run
   static-pixel mask above.
3. **Visual pass, all 188 frames — 100%, not a sample.** 18 frames were read at full resolution (one
   per visual family found by clustering, plus every corner-flagged frame); the remaining frames were
   read at 50% scale in 2×2 contact sheets, four to a sheet, 47 sheets. Every run, both dates, every
   turn number 1–7 and the single frame geometry are covered because the set is exhaustive.

## Findings

### Contaminated: 0

No frame that reached a model contains the FireTuner window, a developer console, a debug overlay or
FPS counter, harness-owned UI, the GNOME top bar or any desktop panel, or a notification toast. Every
frame is full-bleed Civilization VI: the game's own resource bar begins at row 0, the game's own UI
occupies the edges, and the content behind it is the map, a leader-meet screen, or an in-game modal
(era dedication, civic/tech completed, production picker). Nothing in any frame originates outside the
game client.

**Two flags the programmatic pass raised, both dismissed on inspection:**

- *Corner-variance*, the one live technique still functioning on Linux, fires on 5 of 188 frames when
  re-run offline: blobs `38b3cfe6`, `635197d9`, `65f6a510`, `74ab88d7`, `e6b35afa`, all
  `run-02168773ad79486c9eb7c7a0f6f7b958` turn 2, 2026-09-21 23:38–23:40. Every one sits at a
  corner/whole variance ratio of **1.800–1.802** against a 1.8 threshold — they are threshold noise
  (PIL's histogram-based variance vs. float variance decides them either way), and inspection shows an
  ordinary busy top-right corner: leader portraits and a tooltip, game UI.
- *Border-ring* fires on 185 of 188. That is the game's own dark UI edging against a lighter map
  interior. It is irrelevant to the live outcome: no category in the Linux reject set carries the
  `border` token, so the live gate short-circuits and never runs this test on this platform.

### Unknown: the 421 withheld captures — and this is a real gap

All 421 withheld captures carry `blob_ref = NULL`. **Their pixels do not exist.** Whether a withheld
frame contained a contaminant the gate could not name cannot be answered from this store, by any
method. They are **unknown**, not clean. They also never reached a model, so they do not bear on the
release question — but "the gate was accidentally protective" cannot be *demonstrated* from the
evidence, only inferred from the reasons it recorded.

What withheld them (the brief's open question — the counts have grown since it was written):

| Withheld reason | Captures | Gate |
|---|---|---|
| `provenance_failure` | 307 | provenance (gate 3) |
| `non_player_ui` | 114 | **content (gate 4)** |

So the answer to "what withheld the other 114" is: **the content gate itself**. The gate was not
entirely dead — its one surviving technique fired 114 times. `capture_failed` event reasons break
down as 287 unrevealed-target + 16 zoom-out-of-range + 2 no-numeric-zoom on the provenance side, and
70 `['debug_overlay']` + 24 `['windows_capture_border']` + 10 both on the content side.

### Finding for a task: 34 withholds cite a Windows-only category on a Linux host

34 content-gate withholds name `windows_capture_border` — a category the `linux` profile does not
contain and that cannot apply to an X11 host. They come from exactly two runs:
**`run-227998759f75`** (19) and **`run-fd9c08a1df1d`** (15), both 2026-09-21 ~16:37–16:45, both
recorded `os = linux, session_type = x11`. For that category to be in the reject set at all, profile
resolution must have returned `profiles.default` rather than `profiles.linux` for those runs —
`resolve_screening_profile` falls back to `default` whenever the declared key is `default` or the
platform key is absent, and neither should have been true here.

This is not a curiosity. A profile resolving categories that cannot apply to the host means profile
selection is wrong somewhere, and it means those 34 withholds were not the protection they appear to
be — they were a Windows recording-border test firing on the game's own dark UI edging, exactly as the
border-ring test does on 185 of the 188 frames that *passed*. **Impact is bounded**: neither run ever
produced a `shown_to_agent` capture, so no frame from either reached a model.

### Why the frames are clean — and why that is not the gate

`host/linux/adapter.py::_capture_via_xcomposite` redirects the game window, names **that window's own
off-screen pixmap** via `NameWindowPixmap`, and reads it back. It refuses to capture at all when there
is no compositing manager, and it has **no root- or screen-grab fallback on any branch** — the module
says so explicitly and cites `spikes/r6-evidence/root-scoped-same-region-LEAKS.png` as the reason.
Another window on the same desktop, including FireTuner, is therefore structurally absent from the
frame: it is not in the pixmap being read.

The evidence supports that explanation directly. The zero-static-pixel result excludes not only
overlapping windows but also the harder case the screening module was built for — chrome the game
composites *into its own surface* — which window-scoping would **not** have excluded.

## What this does and does not prove

**Proved.** No frame that reached a model in any of these 20 runs, on either date, contained any of
the six contaminant categories. Measured, not argued: 188/188 distinct frames inspected by eye and by
statistic, covering all 301 candidate captures.

**Not proved — and this is the part that must not be allowed to merge with the above.** The image
content-screening gate did not produce this outcome and contributed nothing to it. Throughout every
run audited here, `detected_text_tokens` was supplied by no production call site
(`run/decision_loop.py` passes neither `detector` nor `detected_text_tokens` to `capture_for_step`,
which defaults it), so the declared-text technique never ran, and on the Linux profile the only live
technique was corner-variance, which maps to `debug_overlay` alone. **Five of six categories —
`firetuner_window`, `developer_console`, `harness_owned_ui`, `linux_panel`,
`linux_notification_toast` — had no functioning technique at all.** Had FireTuner been composited into
the game's surface, nothing would have caught it.

The right characterisation is that **the window-scoped capture path was accidentally protective**. A
capture path that happens to be safe is not a control. The clean result discharges the release
obligation for the delivered backlog **on evidence**; the gate defect remains open independently and
is not closed by this document.

**Also not proved**: that no withheld frame hid a contaminant (no pixels retained), and that the
result generalises to any future run, a different window mode, a different compositor, or a host where
the game does composite an overlay into its own surface — the 60 FPS overlay the screening module's
own docstring was written for is precisely that case, and it did not occur here.

## Bearing on SC-009 and SC-019

- **SC-009** ("Zero images shown to the agent contain the Firetuner window, developer console, debug
  overlay, or other harness UI, and zero were produced from a camera state a human could not reach —
  audited per release, and any finding blocks release"). The non-player-UI half is **met on measured
  evidence** for all 301 shown captures: 0 findings, so nothing here blocks release. The camera-state
  half is **not addressed by this audit** and needs its own check; note in its favour that the
  provenance gate is demonstrably live, withholding 307 captures including 287 for an unconfirmed
  revealed target.
- **SC-019** ("Zero stored captures contain the Firetuner window, developer console, or other
  non-player UI"). **Met for the 363 stored captures that have bytes** — the 188 distinct blobs behind
  them are the frames audited here, and all are clean. It is **unverifiable for the 421 withheld
  captures**, which are stored as records with no bytes; if SC-019 is read as covering those, it
  cannot be audited at all and the criterion should say so.
- **For both**: passing on evidence while the control is blind is a distinct, still-open defect. A
  release sign-off that cites this audit should cite it as *"no contamination was found"*, never as
  *"the screening gate held"*. The gate fix landed today in `483017c` — after every frame audited
  here was captured — so no frame in this set was screened by the repaired gate, and nothing in this
  document is evidence for or against that fix.

## Reproduction

Read-only, on a copy; no repo file is written by any step.

1. `cp civsim-match-store.db <scratch>/audit.db`; open `file:audit.db?mode=ro`.
2. Shown set: `captures.capture_json -> shown_to_agent == true`; dispatch-confirmed subset by joining
   `decision_step_id` to `model_calls.call_json -> image_count >= 1`.
3. Blob path: `blobs/<blob_ref[:2]>/<blob_ref>`; each is a PNG as stored.
4. Static mask: one frame per run, `stack.max(0) - stack.min(0)`, threshold ≤ 6 per channel; report
   the mean and the fraction of 40×40 blocks above 0.90.
5. Detector replication: `_corner_overlay_is_suspect` at `_CORNER_FRACTION = 0.12`,
   ratio ≥ 1.8 and absolute ≥ 200; `_border_ring_is_suspect` at thickness 4, inset 12, stddev ≤ 12,
   delta ≥ 40 — both from `src/civsim_harness/parity/screening.py`.
