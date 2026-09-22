# Contract: The Blocking-View Registry

**Between**: rung 1, run preparation, and the auditors who satisfy SC-007.
**Location**: `catalogs/views/blocking_views.yaml` — versioned reviewable data, like the rest of the
catalog.
**Schema version**: 1.0 | **Date**: 2026-09-22

---

## 1. Why this is data and not code

It **is** the Principle I boundary for recovery, in the same way the capability catalog is the
boundary for play. An auditor asking *"could the harness have done something a human could not?"*
should read declarations, not trace control flow. SC-007 and SC-010 are audited against this format.

---

## 2. The entry

```yaml
- view_id: diplomacy.session
  recognised_by: screens.probe                 # a declared observation
  carries_player_choice: false
  human_action: "press Escape"
  declaration_id: views.dismiss_blocking_view  # the declared action, with its parity_basis
  reachable_by_input_path:
    linux/x11:  { reachable: true,  reason: null }
    linux/wayland: { reachable: false, reason: "synthetic input blocked by the compositor" }
    windows:    { reachable: true,  reason: null }
    macos:      { reachable: true,  reason: null }
  closed_confirmed_by: screens.probe           # an INDEPENDENT re-read, never the dismissing call
```

All seven fields are required. An entry missing any of them does not load.

---

## 3. Rules

### 3.1 An unregistered view stalls the run visibly

Nothing is dismissed by guesswork. Full-game coverage means the harness will meet screens it has
never seen — era transitions, new popups, patch-introduced dialogs — and the wrong answer is to
click blindly. The unknown view is **recorded** and the run stalls (spec 002 FR-049).

### 3.2 `carries_player_choice` decides who answers, and a single button still counts

> **"Goodbye" looks like a dismissal and is in fact the player's answer.**

A view carrying a choice — **including one presented as a single acknowledging button** — is routed
to the agent as a declared prompt and answered as a recorded decision (spec 002 FR-010). The
watchdog's job is to make it *answerable*, not to answer it.

**Agent first, then watchdog** (owner ruling, 2026-09-22). If the agent does not act within a
bounded window, **or is unavailable**, the harness dismisses the view itself and records a
**labelled watchdog intervention that counts as nothing toward the agent's decision coverage** — it
is not an agent decision, is never presented as one, and appears in no measure of what the agent
played.

The ordering is load-bearing in **both** directions and MUST NOT be collapsed to either extreme:

- **Why the agent goes first.** Letting the watchdog answer a choice makes it a second, undeclared
  player, and Principle I does not permit that at any quality of intent.
- **Why the watchdog goes second rather than never.** A view only the agent may answer, when the
  agent is wedged or gone, is an indefinite freeze — the case that cost hours on the day this was
  specified. Refusing to ever dismiss trades a bounded parity footnote for an unbounded outage.
- The **separate labelling** is what keeps both true at once: the board gets unstuck and the play
  record stays honest about who did it.

The agent's window is bounded by the **same affirmative-liveness rule** as everything else, not by a
separate timer: an agent that is emitting keeps its turn; one that is silent or unavailable is what
triggers the dismissal. That is one fewer number in the system.

### 3.3 `reachable_by_input_path` is a precondition, not a discovery

Two cases are already **measured false** and must be in the registry from day one:

| Case | Why |
|---|---|
| The post-defeat exit-confirm modal | it **ignores synthetic input** |
| Any view, on a Wayland session | the compositor blocks synthetic input by design |

Where a view's dismissal is not reachable, the rung's precondition is **observably unsatisfiable**:
it is skipped with that reason recorded and escalation continues (FR-039, and rung contract §3.4).
Discovering it mid-stall instead burns the attempt limit and converts a recoverable stall into a
failed run.

### 3.4 `closed_confirmed_by` may never be the dismissing call's return value

Two measured hazards, and satisfying one does not satisfy the other:

- **The call can report success without acting.** A popup's own click callback was measured to
  report success and perform nothing — which is *why* the existing dismissal path completes with a
  real host click.
- **The re-read can report the pre-call value** if it is issued in the same tuner command. See
  [reconciliation-check.md](./reconciliation-check.md); it produced a retracted finding against an
  engine call that was working correctly.

### 3.5 `human_action` is what a human does, and `declaration_id` carries the parity basis

The declared action's `parity_basis` field (required, non-empty, on every `ParityDeclaration`) is
what an SC-007 audit reads. Pressing Escape qualifies; clicking the one button the view is already
offering qualifies. **A debug call does not, however convenient.**

Where the action is delivered by synthetic input rather than through Firetuner, the declaration is
`path: bespoke` with the measured gap recorded verbatim — no Lua API reachable from `InGame` fires a
control's registered callback, and `UI.RespondToPrompt` exists in none of Firaxis' 645 shipped Lua
files. `tests/contract/test_synthetic_input_declaration.py` `ast`-derives every
`HostPlatform.send_input` call site in `src/` and enforces this, so an undeclared dismissal call
site cannot ship.

---

## 4. The initial set

| `view_id` | Choice? | Human action | Reachable? | Evidence |
|---|---|---|---|---|
| `diplomacy.session` | no | Escape | yes on X11 | the view opened by `diplomacy.send_delegation` that no harness action could close; **one human Escape cleared it** after the board sat frozen for hours |
| `diplomacy.leader_approach` | **yes** | the session's own close path, not a response | yes on X11 | "Goodbye" rejected 16 times running because the exit needed the close path rather than a response |
| `prompt.war_declaration` | **yes** — single button | acknowledge | yes on X11 | an AI leader's declaration and its single-button popup were holding the board as this was specified |
| `screen.leader_intro` | no | Escape | yes on X11 | already handled by a hard-wired call site in `saves/load_game.py`; brought into the registry rather than left as a special case |
| `menu.exit_confirm_post_defeat` | yes | click Yes | **NO — ignores synthetic input** | measured; the rung is skipped with that reason and escalation continues |

**A note on the first row, because the retraction matters more than the incident.** The stranding
was real and is recorded as a standing project rule. The root cause first attributed to it — *"the
engine's close call returns success and does nothing"* — was **retracted by this project's own
ledger as a measurement artifact**: a same-command readback returning the pre-call value. The close
probably worked all along and the check lied about it. That retraction is load-bearing here, not a
footnote: it is exactly why `closed_confirmed_by` must be an independent re-read.

**And a note on what is NOT in this registry**: `IsSessionActive()` does not exist on this build. A
missing engine method is detected at preparation and means **unknown**, never **no** (FR-053), so
`diplomacy.session` is recognised by the screen-identity probe rather than by asking the engine
whether a session is open.
