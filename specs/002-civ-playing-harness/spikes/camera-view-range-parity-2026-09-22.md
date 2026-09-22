# Camera parity: the harness occupied a camera state no human could occupy (2026-09-22)

**Headline, because the wrong one is easy to reach.** This is not "`camera.zoom` accepted a bad
argument". A bad argument is a validation nicety. What happened is that the harness asked the game
for a **camera state no human player can be in** — and the only reason anyone noticed is that the
frames taken afterwards failed the provenance gate and were withheld. That is Principle I, and it
is why this was fixed rather than shipped with a caveat.

Scope: this entry covers that finding only. Written by the headless lane under an explicit
one-file grant into `spikes/**`, which is otherwise the live lane's.

## What the declarations prove, with no client needed

- `catalogs/observations/views.yaml` declares `views.world` with `zoom_range: [0.2, 1.0]` and
  `views.strategic` with `zoom_range: [0.05, 0.3]`.
- `catalogs/actions/camera.yaml` declares `camera.zoom`'s availability predicate as
  `target >= 0.05 and target <= 1.0`. That is the **union** of the two ranges above.
- On 2026-09-21 the harness issued `camera.zoom` with `{"target": 0.05}` while the camera was in
  **world** mode. 16 of 17 captures in that block were withheld; the provenance gate checks mode
  before zoom, and the message emitted was the zoom one, so the camera was in world mode at
  0.0499997.
- 0.05 is a perfectly legal **strategic** zoom. It is not a legal world zoom. On the standard
  interface, scrolling past a threshold switches to the strategic view: mode and zoom move
  together, and **there is no seat a human can take in world mode at 0.05**.

Everything above is readable from the checked-in catalog and the withheld-capture record. None of
it depends on what the engine did with the request.

## What is NOT established, and must not be cited

Why the zoom did not take effect on that run is **open**. An earlier reading of this incident —
that the engine accepted the value and clamped nothing — was withdrawn, and so was the follow-up
measurement offered in its place (a `UI.SetMapZoom` call said to return cleanly while
`UI.GetMapZoom` still read the old value); the live lane retracted it the same day, zoom now reads
inside the world range. Note also that a clean `dispatch_result` means the Lua call **returned**,
not that it **took effect** — do not read one as evidence of the other anywhere.

None of that changes the finding. The violation is in what the harness **asked for**, which the
declarations settle on their own.

## The root cause was structural, and it was in the validator

`act/camera.py::validate_camera_action` — which `run/decision_loop.py` uses to *replace* the
generic dispatch outcome for every `camera.*` action — bound `{"target": target}` and nothing
else. With no `Observation` it had no way to know which view the camera was in, so it could only
ever check the union. **Any target legal in either view was accepted in both.** Two views' ranges
were collapsed into one predicate, and the collapse was invisible because each endpoint of the
union is legal *somewhere*.

This is also the reason the fix could not live in `act/availability.py` alone: `decision_loop`
overwrites the dispatch outcome for exactly the one action that matters, so a check landed only
there would have been correct, tested, and dead on the production path.

## What landed (T276)

An argument outside the **current view's** declared range is refused at availability time, naming
the view and the range it violated, before anything is dispatched. The current view comes from the
observation's `camera.read_state` mode; the range comes from that view's own
`camera_requirements.zoom_range`. No bound is written down in code or in the tests — change
`views.yaml` and what is refused changes with it.

`validate_camera_action` now takes a **required** keyword-only `observation`. Required, not
optional-with-a-default: an optional one would be supplied by every test and by no production
caller, which is the shape of defect this project spent 2026-09-22 finding three times.

Explicitly **not** done: clamping `camera.zoom` to the world range. That would have made the
strategic view's own declared range unreachable — a fix that breaks a second thing to hide the
first. The strategic zoom is authorised in the strategic view, and there is a test that says so.

## What is held (T277) — the live lane's measurement

What should happen when a target is legal for a *different* view — refuse it, or perform the mode
change first — depends on what a real scroll-wheel zoom does to `camera.read_state`'s `mode` and
`zoom` **together** on this build. That is client-gated and was not guessed. The seam is a
parameter (`act/availability.py`'s `CrossViewZoom`); its shipped value refuses, and its unmeasured
alternative raises rather than quietly re-admitting the argument.

The measurement to take, when the client is free: put the camera in the world view, zoom out past
the strategic threshold with the scroll wheel alone, and read `mode` and `zoom` back on both sides
of the threshold. If the mode flips on its own, a human reaches 0.05 by scrolling and the harness's
equivalent is a view change followed by the zoom. If it does not, refusing is right permanently.
