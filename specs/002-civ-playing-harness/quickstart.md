# Quickstart & Validation Guide: Civilization-Playing Harness

**Feature**: `002-civ-playing-harness` | **Date**: 2026-09-19 | **Plan**: [plan.md](./plan.md)

How to stand the harness up and prove it works end to end. Each scenario maps to a user story's
Independent Test and names the success criteria it discharges, so "done" is checkable rather than
asserted.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| A native Windows, macOS, or Linux desktop | **Not Proton/Wine** — the tuner interface is built into the native binary and is not exposed under a compatibility layer, so a Linux host needs the native Aspyr port (research R1) |
| Civilization VI, with the configured ruleset and mod set installed and active | The exact versions the run configuration names — a mismatch is meant to fail the run |
| The tuner interface enabled, and the client restarted | Platform-specific — see below. The Windows-only SDK/Development Tools are **not** required: the harness speaks the protocol directly and never opens the FireTuner GUI |
| Python 3.12+ and `uv` | |
| Linux only: `libdbus-1-dev` installed before `uv sync` | `uv sync --extra linux` builds `dbus-python` from source; without the D-Bus headers it fails with a meson build error that never names the missing package. `sudo apt-get install -y libdbus-1-dev` |
| An OpenRouter API key in the environment | `OPENROUTER_API_KEY`. Never in run configuration (FR-043) |
| A reachable match store | Deliverable 3, or the local reference adapter (plan C2) |
| Auto-end-turn disabled in the game's options | Otherwise the game advances turns out from under the harness |
| Disk headroom above the run's `min_free_disk_gb` | Nothing is ever deleted to make room; the run halts instead (FR-036) |
| The client on the platform **and** build the seed set pins | A different build *or platform* fails the run before turn 1 unless accepted for that set (FR-002, R20) |
| Platform capability grants, where the OS requires them | macOS: Screen Recording, plus Accessibility if the bespoke save path is in use. Linux/Wayland: a screen-capture portal grant — see the tier note below |

### Enabling the tuner

| Platform | How |
|---|---|
| Windows | In-game **Options → Tuner (disables achievements)** |
| macOS | Set `EnableTuner 1` in `~/Library/Application Support/Sid Meier's Civilization VI/Firaxis Games/Sid Meier's Civilization VI/AppOptions.txt` — not exposed in the menu |
| Linux | Set `EnableTuner 1` in `~/.local/share/aspyr-media/Sid Meier's Civilization VI/AppOptions.txt` — not exposed in the menu |

On macOS and Linux this means hand-editing `AppOptions.txt`. **Edit it with the game closed** — the
client rewrites this file on exit, so an edit made while the game is still running is silently
overwritten the moment you quit it. Restart the client afterwards; it then listens on TCP
`127.0.0.1:4318`.

## Setup

```bash
uv sync                                  # install (adds your platform's host extras)
uv run civsim doctor                     # preflight the environment
```

Commands below are shown in POSIX shell form. They are identical on Windows PowerShell except where
noted; only the process-kill in Scenario 4 differs.

`doctor` must report all green before anything else is worth trying:

```text
platform          : ok  (macos 15.3, tier VALIDATED)
tuner connection  : ok  (GameCore_Tuner=2, InGame=5)
client            : ok  (pid 18244, build mac/1.0.12.9)
store             : ok
catalog           : ok  (version 2026.09.1, 215 declarations, 0 undeclared)
capture path      : ok  (screencapturekit, hygiene spike PASSED)
provider key      : present
disk headroom     : 41.2 GB free
```

The same run on another host reports its own host layer, and the tier is the line that tells you
what you will actually get:

```text
platform          : ok  (linux/wayland, tier SUPPORTED — runs will be visually degraded)
capture path      : none (portal grant unavailable for unattended capture)
```

Four lines deserve attention rather than a glance:

- **`catalog: 0 undeclared`** — a non-zero count means a capability exists without a parity
  declaration, and no run may start (FR-023).
- **`capture path: hygiene spike PASSED`** — until the R6 spike passes, this reads
  `none (runs will be visually degraded)`. That is a legitimate operating state, not a blocker; what
  it must never read is a capture path in use without a passing spike.
- **`client: build mac/1.0.12.9`** — the build is a composite of **platform and version**, compared
  against the seed set's pin at every run's preflight. A differing build *or platform* fails the run
  before turn 1 unless an operator has accepted that exact transition for the set (FR-002, research
  R18, R20).
- **`platform: tier VALIDATED`** — the harness resolves its own capability rather than trusting
  documentation. `VALIDATED` means full capability with a passing capture-hygiene spike;
  `SUPPORTED` means it will run and record honestly but visually degraded; `UNSUPPORTED` means
  preflight will refuse to start, because no verified quicksave path exists and FR-007 makes a turn
  without its quicksave impossible (research R19).

---

## Scenario 1 — An unattended run to a turn stop condition

**Validates**: User Story 1 · FR-001 – FR-015 · SC-001, SC-003, SC-004, SC-012, SC-022

```bash
uv run civsim run start ./configs/turn50-validation.yaml
```

Then walk away. Do not touch the keyboard or the game client.

```bash
uv run civsim run status <run_id>        # lifecycle only — records live in the store
```

**Expected**:

- The game reaches exactly the configured seed, civilization, ruleset, build, and settings, then
  begins playing with no further input (US1 §1).
- The run terminates at turn 50 with `stop_resolution = turn_reached`.
- Every turn from 1 to 50 has a persisted record containing **every decision step in order** — the
  observation given at that step, the image shown, the decision issued, the reasoning stated, and
  the verified outcome — plus the turn's resulting yields. **No turn number is missing, and no step
  index is missing within a turn.**
- Every turn has a verified turn-start quicksave.
- Every turn ends either on the agent's `turn.end_turn` decision or on the no-progress backstop, and
  the two are distinguishable in the record.
- `record_completeness_status = complete`.

**Turn durations will vary wildly, and that is expected.** Turn 3 may be two steps; turn 47 may be
sixty. Nothing bounds a turn by time, step count, or cost (FR-014).

**Verification** (against the store, not the harness's own word):

```bash
uv run civsim audit completeness <run_id>    # expect: 0 turn gaps, 0 step gaps, 50/50 authoritative
uv run civsim audit saves <run_id>           # expect: 50/50 verified quicksaves
uv run civsim audit decisions <run_id>       # expect: 0 decisions without a model_call_id
uv run civsim audit steps <run_id>           # expect: 1 decision + 1 model call + 1 observation per step
uv run civsim audit endings <run_id>         # expect: every turn ended_by_agent or ended_on_no_progress
```

The third catches the failure mode SC-012 cares about — a turn ended on a fabricated or defaulted
action recorded as if the agent had decided it. The fourth catches the one the clarification
introduced: a step that batched several decisions, or reused the previous step's observation instead
of assembling a fresh one, both of which would look like a working run while quietly defeating
FR-008.

**On SC-001's 90 %**: every started attempt counts in the denominator, and any attempt that did not
reach its stop condition is a failure — including one that stopped in a correctly recorded failed
state, and one lost to something outside the harness like a host reboot. Do not reclassify or
exclude attempts after the fact; the 10 % headroom is what absorbs them.

---

## Scenario 2 — A full game to a victory or defeat outcome

**Validates**: User Story 1 (second half) · FR-009, FR-010 · SC-002, SC-005

Same as Scenario 1 with `stop_condition: { type: game_outcome }`. Expect hours, not minutes.

**Expected**:

- The run plays across every era transition without stalling on a mechanic that era introduced.
- Game-initiated prompts — unit promotions, pantheon and religion, great people, AI diplomatic
  approaches, war declarations, city-state quests, World Congress votes, era transitions — are each
  answered through a declared catalog entry and recorded as decisions.
- Termination on victory or defeat, recorded as exactly one stop condition.

```bash
uv run civsim audit prompts <run_id>
# expect: every prompt encountered is either a recorded prompt_response decision
#         or a recorded stall. Zero dismissed, defaulted, or absorbed.
```

An `unknown_screen` event here is a *pass*, not a failure — it means the harness met a screen it did
not recognise and stalled visibly instead of clicking blindly (FR-049). What it must never produce
is a turn that advanced past a prompt with no decision recorded.

---

## Scenario 3 — Audit the parity boundary from the record alone

**Validates**: User Story 2 · FR-016 – FR-026 · SC-006, SC-007, SC-008, SC-009, SC-019, SC-020

Working only from a completed run's record:

```bash
uv run civsim audit parity <run_id>
```

**Expected**:

- Every distinct observation and action the run used — structured and visual — resolves to a parity
  declaration in the catalog version recorded on the run.
- Zero observations or actions without a declared basis.
- Zero forbidden values in any turn's context: unrevealed map contents, opponent internal state,
  hidden AI intent, undisclosed opponent research or civics, RNG state, debug or provenance data.
- Zero harness telemetry present as game information — model identity, cost, latency, retries, save
  lineage, run configuration.
- Zero images containing the FireTuner window, developer console, debug overlay, or harness UI —
  plus whatever non-player chrome the **host platform** contributes, which differs: a Windows
  taskbar, a macOS menu bar or Dock, a Linux panel or notification toast. The rule is identical
  everywhere; the detector profile is per platform because the chrome is (research R7, R19).
- Zero images produced from a camera state a human could not reach.
- Every capability in use is `path: firetuner`, or carries a non-empty `firetuner_gap`.

**Any finding blocks release.** These criteria are worded as zero-tolerance in the spec, so this
audit runs per release and the parity red-team suite in `tests/contract` runs on every build.

---

## Scenario 4 — Survive a crash mid-turn

**Validates**: User Story 3 · FR-044 – FR-048 · SC-010, SC-011, SC-021

Start a run, let it reach a mid-game turn, then kill the client:

```bash
pkill -9 -f "Civilization"               # macOS / Linux
```

```powershell
Stop-Process -Name CivilizationVI -Force  # Windows
```

**Expected**:

- The crash is detected and recorded within 60 seconds.
- The harness resumes from **that turn's** quicksave as the same continuous run — not a new run.
- It re-observes and re-captures before acting; nothing obtained before the interruption is reused.
- The turn record shows both the abandoned attempt and the replayed one, with the replay marked
  authoritative.
- The run completes to its stop condition, with the interruption and resume point in event sequence.

```bash
uv run civsim audit recovery <run_id>
# expect: crash_detected + resumed events, 1 abandoned + 1 authoritative attempt
#         on the interrupted turn, and 0 silently missing turns
```

**Also verify the give-up path.** With `recovery_attempt_limit: 1`, kill the client repeatedly: the
run must stop in a recorded `failed` state identifying its last-known-good save, rather than looping
indefinitely (SC-021).

---

## Scenario 5 — Branch and replay from a turn

**Validates**: User Story 4 · FR-031 – FR-036 · SC-014

```bash
uv run civsim run branch <run_id> --turn 23 --config ./configs/branch-model-a.yaml
uv run civsim run branch <run_id> --turn 23 --config ./configs/branch-model-b.yaml
```

**Expected**:

- Both branches begin from an identical game position.
- Each records `parent_run_id` and `parent_turn = 23`.
- The parent run's record is unchanged — byte-identical before and after.
- Both play forward to turn 30 under their own model configuration.
- Save points remain addressable by run, turn, and lineage; no filesystem inspection is needed.

```bash
uv run civsim audit lineage <branch_id>
uv run civsim audit immutability <run_id>   # expect: parent record unchanged
```

Then confirm retention safety, which under explicit-archival is a stronger claim than it used to be:

```bash
uv run civsim saves reap --dry-run
# expect: 0 eligible saves. The parent run is finished but NOT archived,
#         so none of its turn-start saves may be removed — however old it is.

uv run civsim run archive <run_id>
uv run civsim saves reap --dry-run
# expect: the parent's saves now listed as eligible; records, events, and captures untouched
```

The first command is the real test. A finished run is exactly what a reasonable retention policy
would collect, and exactly what this design forbids collecting — every turn-start save is a branch
point, and only an operator may decide to give one up (FR-036, research R17).

---

## Scenario 6 — Swap the model with no code change

**Validates**: User Story 5 · FR-037 – FR-043 · SC-015, SC-016, SC-017, SC-018

Run the same seed twice, changing **only** `model_config` between them — no code, no new
integration, no rebuild. To discharge SC-015's "verified on at least two providers" — which means two
distinct vendors, not two models from one — route the two runs through `anthropic/claude-sonnet-5`
and `google/gemini-3-pro` via OpenRouter, both image-capable, for the identical seed and
configuration otherwise.

**Expected**:

- Both runs complete through the same observation and action surface.
- The `anthropic/claude-sonnet-5` run and the `google/gemini-3-pro` run both reach the same stop
  condition, exercising two distinct vendors rather than two models from the same one.
- Every model call records the model that actually served it, its latency, cost, and retry count.
- Where a fallback served a turn, that turn is distinguishable from primary-served turns.
- Zero calls made with images dropped to fit a limit.

```bash
uv run civsim audit models <run_id>
uv run civsim audit secrets <run_id>   # expect: 0 credential-shaped values in records, captures, logs
```

**Then verify the refusal**, which is the more interesting half: configure a fallback chain
containing a text-only model. The run must **fail to start**, naming the model that cannot carry
images — not start and quietly drop them (FR-039, SC-017).

---

## Scenario 7 — Honest degradation

**Validates**: FR-050 · SC-013

Force the capture path to fail (disable it in configuration, or obstruct the capture source) and
start a run.

**Expected**:

- Images are withheld after bounded retries rather than passed through unscreened.
- Affected **steps** are recorded `visually_degraded = true`, which rolls up to their turns.
- The run's `comparability_status` reflects it.
- The run still plays and still records — degraded is a marked state, not a failure.

Degradation is recorded at the step because captures are per step: a turn that lost its images
half-way through must be distinguishable from one that never had them, and from one that had them
throughout.

The failure this scenario is designed to catch is the tempting one: the harness quietly continuing on
structured state alone, producing a run that looks like every other run in the catalog while the
agent was in fact perceiving the game differently.

---

## Scenario 8 — The within-turn loop and its only two exits

**Validates**: FR-008, FR-014, FR-015 · SC-003, SC-012, SC-022

This is the clarification session's scenario and has no pre-revision equivalent. Three behaviours,
each checkable on a short run.

**8a — The loop re-observes between decisions.** Take any completed turn with more than one step:

```bash
uv run civsim audit loop <run_id> --turn 12
# expect: for each step n, observation(n) assembled AFTER execution of decision(n-1) was verified,
#         a distinct capture per step, and exactly one model call per step
```

The failure to catch is an observation reused across steps, or captures shared between them. Either
would mean the agent chose its next move while looking at a board that predates its last one, which
is the precise thing the loop exists to prevent.

**8b — A long turn is not truncated.** Let a mid-game turn run to a large step count and verify
nothing intervenes:

```bash
uv run civsim audit endings <run_id>
# expect: 0 turns ended by the harness while the agent was still making progress
```

There is no wall-clock bound, no step cap, and no cost ceiling. A turn of several hundred steps
running for hours is a valid turn (FR-014). If a turn ends for any reason other than the agent's
end-turn decision or the backstop, that is a defect regardless of how sensible the reason sounds.

**8c — The backstop ends a stuck turn, and is recorded as such.** Configure
`no_progress_step_limit: 3` and point the agent at a position where its decisions are rejected or
change nothing:

**Expected**:

- The turn ends after exactly three consecutive no-progress steps.
- `outcome = ended_on_no_progress`, with a `turn_ended_on_no_progress` event on the timeline.
- **No `end_turn` decision was synthesised** to represent it — the backstop is not an agent
  decision, and recording it as one would destroy the distinction SC-022 exists to preserve.
- A productive step mid-sequence resets the counter, so the turn continues.

---

## Scenario 9 — The build pin refuses a patched client

**Validates**: FR-002, FR-031 · Reproducibility under Principle IV

Simulate a patch by changing the seed set's recorded `game_build` to a value the client does not
report, then start a run on that set.

**Expected**:

- The run fails **before turn 1**, in `failed` state, with the build mismatch recorded.
- No turn 1 quicksave, no decisions, no model calls — the refusal is at preflight.

Then take the override path:

```bash
uv run civsim seedset accept-build shuffle-classic-2026q3 --to win/1.0.12.11 --reason "MP-only patch"
uv run civsim run start ./configs/turn50-validation.yaml
uv run civsim audit builds shuffle-classic-2026q3
# expect: the set reports as NON-uniform, its runs partitioned by build, and every run played
#         after the acceptance carrying game_build_acceptance_ref
```

The second half is the one that matters. An override that let the set keep reading as uniform would
be worse than no check at all — it would launder a mixed-build set into looking like clean data.

---

## Scenario 10 — The same harness on another platform

**Validates**: research R19, R20 · FR-002, FR-031 · SC-009, SC-013

Install and run on a second platform, changing nothing but the host:

```bash
uv sync
uv run civsim doctor        # expect: platform line resolves, tier reported with its reason
uv run pytest tests/contract/test_host_platform_port.py
```

**Expected**:

- `doctor` names the platform, its session type where it matters (X11 vs Wayland), the capture path
  it selected, and its support tier — resolved by probing, not by configuration.
- The `HostPlatform` conformance suite passes the same assertions it passes on every other platform.
- A `VALIDATED` host produces screened images; a `SUPPORTED` host produces runs marked
  `visually_degraded`; an `UNSUPPORTED` host **refuses to start a run** rather than playing without
  a verified quicksave path (FR-007).

**Then verify the comparability guard**, which is the half that protects the data:

```bash
uv run civsim run start ./configs/turn50-validation.yaml
# expect: REFUSED before turn 1 — the seed set pins a different platform (FR-002, R20)

uv run civsim audit builds shuffle-classic-2026q3
# expect: if a platform change was accepted, the set reports NON-uniform
#         with its runs partitioned by platform and build
```

The refusal is the point. Running the same seed set across two platforms without recording it would
produce halves that are not comparable while every other recorded field still matched — the same
silent corruption the build pin exists to prevent, which is why platform is part of the same pinned
identity rather than a separate notion.

Branching across platforms is refused on the same basis: Civ VI's cross-platform saves are
account-gated and version-matched, which is not a foundation for "two branches from the same save
point begin from an identical position" (FR-034). Whether such a save loads identically is research
R20's spike, and only the relaxation depends on it.

---

## CI-runnable subset

Scenarios 1–10 need a real client. These run without one and gate every build, **on all three
platforms as a matrix**:

```bash
uv run pytest tests/unit tests/contract tests/integration
```

The matrix is doing real work rather than ceremony: the portable core is most of the harness, so a
build on each OS catches an accidental platform-specific import long before the live tier would.

Covering: Nexus codec round-trips and sentinel correlation; catalog load-time validation (including
that a missing `parity_basis` and an undocumented bespoke path both fail the load); the parity
red-team suite; `MatchStore`, `ModelProvider`, and `HostPlatform` port conformance; the full turn
cycle — multi-step loop, both turn endings, mid-turn observation failure and replay — against a
recorded-transcript fake game, fake provider, and fake host; run and turn state machines;
no-progress accounting; retention eligibility; platform and build pin comparison; credential
redaction.

Four of these are negative tests guarding invariants whose violation would look like a feature:

| Test | What it forbids |
|---|---|
| A 500-step productive turn completes untouched | Any wall-clock, step, or cost cap on a turn (I16) |
| A finished, unarchived, year-old run yields 0 eligible saves | Age- or state-based retention (I17) |
| A fake provider returning two decisions raises | Batched decisions slipping back in (I13) |
| A step reusing the prior step's observation or capture fails | Stale-board play (I14) |

The live tier is marked and excluded:

```bash
uv run pytest tests/live -m live    # requires a running Civ VI client, and runs per platform —
                                    # a pass on one host says nothing about another's host adapter
```

---

## Reference

- Requirement-by-requirement detail: [spec.md](./spec.md)
- Entity shapes and invariants: [data-model.md](./data-model.md)
- Interface details: [contracts/](./contracts/)
- Decisions and their rationale: [research.md](./research.md)
