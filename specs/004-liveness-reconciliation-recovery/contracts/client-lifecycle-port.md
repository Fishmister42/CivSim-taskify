# Contract: The Client Lifecycle Capability

**Between**: rung 5 and a host adapter that does not implement this yet — **on any platform**.
**Schema version**: 1.0 | **Date**: 2026-09-22
**Status**: **SPECIFIED, UNIMPLEMENTED, AND DECLARED UNAVAILABLE EVERYWHERE.**

---

## 1. Why a contract for something nobody has built

Principle VII requires the harness to *"detect Civilization VI crashes, preserve the last-known
save/state, and **resume or restart** the run without silent data loss."* The restart clause is
**unimplementable**, not merely unimplemented: no method anywhere in the harness launches, restarts,
or terminates the Civilization VI client or the tuner.

Writing the shape down now does three things that leaving a gap does not:

1. It makes the absence a **recorded fact about a host**, which FR-034 and the `ClientLifecycleCapability`
   entity require, rather than an assumption someone drifts into.
2. It stops the capability being invented **ad hoc during an incident**, when the pressure to "just
   kill it and restart" is highest and the data-safety argument is least likely to be made.
3. It names the gate — §4 — that must be satisfied before the capability may ever fire, so whoever
   builds it inherits the constraint rather than rediscovering it.

---

## 2. The verified absence

Checked against the tree for this plan rather than taken from the spec.

`HostPlatform` (`src/civsim_harness/host/port.py`) declares **nine** methods:
`locate_game_process`, `find_game_window`, `capture_window`, `check_capture_preconditions`,
`list_window_titles`, `resolve_game_directories`, `send_input`, `focus_window`, `free_disk_space`.
**None is a lifecycle method.**

A search of `src/civsim_harness/` for any definition matching
launch / restart / terminate / kill / spawn / start-client / stop-client returns **nothing**, on
Linux, macOS and Windows adapters alike. The only textual hits anywhere in the host package are
prose in preflight error messages ("…then restart the harness") and an unrelated comment about a
DirectX launch choice.

**The search could have matched, which is what makes the negative result evidence.** A negative
search result is evidence only if the search could have matched — a rule this project adopted after
a `grep` for a prose sentence returned zero because the sentence wrapped across two lines.

---

## 3. The proposed shape

```python
class ClientLifecycle(Protocol):
    """Start, restart or terminate the game client on this host. NOT IMPLEMENTED ANYWHERE."""

    def capability(self) -> ClientLifecycleCapability:
        """What this host can actually do. All three flags are False on every host today."""

    def terminate(self, process: GameProcess, *, safety: TerminationSafety) -> LifecycleResult:
        """End the client. MUST refuse unless `safety` carries a positive observation
        that nothing in flight would be lost."""

    def start(self, *, build: GameBuild) -> LifecycleResult:
        """Launch the client and return once it is answering. MUST verify by an independent
        read that the client is up, never by the launcher's own return value."""
```

```jsonc
// ClientLifecycleCapability — recorded on the run at preparation
{
  "schema_version": "1.0",
  "host_profile": "linux/x11",
  "can_start": false,
  "can_restart": false,
  "can_terminate": false,
  "basis": "HostPlatform declares nine methods, none of them a lifecycle method; no launch/restart/terminate definition exists anywhere in src/civsim_harness/"
}
```

`basis` is required and must say **what was checked**, so the negative result is auditable rather
than asserted.

---

## 4. The gate that must be satisfied before this ever fires

**Ending the client process is destructive and is gated on a positive observation that nothing in
flight would be lost** (FR-036). This is the whole reason the capability is worth specifying rather
than improvising.

| Situation | May `terminate` fire? | Basis |
|---|---|---|
| An empty post-defeat main menu | **Yes** | Measured data-safe; a fresh menu comes back in ~38 s |
| A board mid-turn | **No** | The in-progress turn would be lost outside the abandoned/authoritative record |
| Unknown or unobservable state | **No** | Absence of a contrary observation is not a precondition (FR-028) |

And the rung-level constraints, which apply whatever the host later implements:

- The rung answers `unreachable` with cause `client_process_absent` or
  `client_present_not_answering` — **never** a `blocked_by_harness` or `blocked_by_game`
  disposition, and never `undetermined`.
- Its outcome is verified by an **independent read** that the client is up and answering, never by
  the launcher's return value.
- It is **never reached under an `undetermined` disposition**, and never reached at all while the
  rungs below it have unexhausted, satisfiable preconditions.
- **Two runs contending for one client**: a lifecycle action must never resolve contention by taking
  something from a live owner. Terminating a client another live run holds is the most destructive
  thing in this entire feature.

---

## 5. Until it exists

- Rung 5 is **declared unavailable** at run preparation, per host, with its reason (FR-039, SC-022).
- The ladder **stops at rung 4** in a recorded failed state naming the last-known good save and the
  highest rung reached (FR-034, FR-037), rather than presenting a ladder whose top it cannot climb.
- **Zero escalations are attempted toward an unavailable rung** (SC-022). A run that escalates
  toward a rung its host cannot perform converts a recoverable stall into a failed one while
  reporting that it tried everything.
- The record says the ladder was four rungs tall on this host, **and why** — which is the difference
  between an honest limitation and a silent one.
