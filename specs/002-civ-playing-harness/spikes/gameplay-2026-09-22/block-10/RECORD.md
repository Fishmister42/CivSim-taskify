# Block 10 -- LIVE STAGE 6: sub-second dispatch->effect latency probe

2026-09-22, Linux live lane, spec `002-civ-playing-harness`.

## Provenance (stated explicitly, per the stage brief)

Played from `/home/matt/CivSolver-live` **as it stands**: detached at `287dee7`, **plus two
uncommitted local edits** (`lua/ingame/screens.lua`, `catalogs/README.md`) whose content is
identical to what landed in `main` at `f14bad0` -- the `EndGameMenu` watchlist mapping.
**This is not a clean checkout.** The worktree was deliberately NOT advanced: advancing requires
an isolated verification of the new head that was not taken, and the owner was waiting. Before
any dispatch, `uv sync --all-groups --all-extras` was run in that worktree -- it had been missing
`python-xlib`, which silently kills the GIF-recording thread without failing the run (visible as
the `ModuleNotFoundError: No module named 'Xlib'` traceback in block-07's `driver.log`).

## What this block is, and what it is not

Operator scripting, **counted as nothing** on the coverage scorecard (`catalogs/README.md`).
It exists because the store samples an action's effect **once per decision step**, so every
latency number the store can yield is a **ceiling, not the curve**. `units.found_city` was
deliberately **never dispatched here** -- founding the capital is an agent decision that belongs
in the store with a run id, and an operator dispatching it would have spent the one demonstration
this board offered. That founding happened in block-09 instead; see below.

## Method

`latency_probe.py`, on the production `load_catalog -> CapabilityRegistry -> CapabilityExecutor`
chain. For each dispatch: issue the action, then **poll in SEPARATE tuner commands** until the
effect is first observable. Separate commands are required because a **same-command Lua readback
returns the PRE-CALL value** (verified three ways on 2026-09-22).

- **Poll interval: 0.10 s sleep between polls**, each poll its own tuner command.
- **Measured bare-command round-trip floor: 0.0663, 0.0670, 0.0666, 0.0663, 0.0666, 0.0670,
  0.0668, 0.0665 s** (8 samples, `units.state`). So the **effective first-look is ~0.133-0.167 s**
  -- the measurement is only as precise as that.
- Per-dispatch poll deadline 20 s, so the block is bounded regardless of client behaviour.

## Raw samples -- first time the effect was observable (seconds)

| action class | n | raw samples | poll index at first observation |
|---|---|---|---|
| `cities.select` | 3 | 0.1337, 0.1334, 0.1333 | 1, 1, 1 |
| `units.select` | 5 | 0.1670, 0.1664, 0.1667, 0.1666, 0.1667 | 1, 1, 1, 1, 1 |
| `units.move_to` | 2 | 0.1668, 0.1668 | 1, 1 |
| `camera.move` | 6 | **not observed within 20 s** (134 polls each) | -- |

**No bound is proposed here.** Four thresholds were set on 2026-09-22 without measuring what they
bound; the number gets set from this data by whoever takes that task.

**The load-bearing caveat: every observed sample landed on poll #1.** 0.133-0.167 s is therefore
**this probe's own floor, not the game's latency** -- selection and movement were already complete
at the earliest instant the probe could look, and this instrument cannot resolve how much faster
they are. Read every number above as an **upper bound**. A finer measurement needs a poll cadence
below the ~0.067 s command round-trip, which this transport does not offer.

## Finding 1 -- the action classes differ by at least 40x, so a single bound cannot fit them

`cities.select`, `units.select` and `units.move_to` settle **under ~0.17 s**. In block-09's run,
`units.found_city` had **still not satisfied `not unit.exists` at 7.291 s**. So the 7.2-7.5 s
cluster in the store is **not one phenomenon**: it is a genuinely slow class (`found_city`) mixed
with a **stopped clock** (3 attempts at a 1 s poll, then give up), and the store cannot tell them
apart because it only ever looks once per step.

## Finding 2 -- `camera.move` dispatches `ok: true` and can NEVER verify (code-confirmed)

Six dispatches, six `ok: true`, zero effects observed in 20 s. The dispatch result is the
diagnosis:

```
{"target_plot": {"x": {"y": 31, "x": 44}}, "ok": true}
```

The plot table is nested **under `x`**. `lua/ingame/camera.lua:66` is
`local function CivSim_Camera_Move(x, y)` -- two positional scalars, **no table guard**. The
dispatcher passes a decision's `target` as the LAST positional argument, and `camera.move`'s
`target_kind` is `plot`, so `{x=44,y=31}` arrives as `x` with `y = nil`.
`UI.LookAtPlot(table, nil)` does not throw, so the pcall succeeds and the function reports
`ok = true` -- a **false success** -- while the camera never moves and
`camera.target_plot == target` is **unsatisfiable by construction**.

`units.move_to` was given exactly this guard on 2026-09-21
(`if type(unitId) == "table" and x == nil then x, y, unitId = unitId.x, unitId.y, nil end`);
**`camera.move` never was.** This explains block-07's `camera.move: out_of_parity_camera=1` and
why camera actions sit among those never applied. Not fixed here: a live block is not where that
lands.

## Finding 3 -- `found_second_city` is a goal id whose NAME asserts what its predicate does not

`tests/live/goals/found_second_city.yaml` has prerequisite `player.units.settler_count >= 1` -- a
settler, **not a city** -- and success `player.cities.count >= observed_start_cities_count + 1`,
which is **relative to the run's own first observation**. With zero cities, founding the
**capital** satisfies it exactly. The only thing that says "second" is the name. A run picking
goals by name, or a human reading a coverage board, would both wrongly conclude the capital was
out of scope; this cost real confusion during this very block. **Deliberately not renamed here** --
a rename has consequences for the store's existing records and for anything citing the id, so it
goes out as a task.

## Finding 4 -- the store gives no visibility into an in-flight turn

For eight minutes of block-09's run the store held **zero turn cycles and zero decision steps**,
because steps land at turn-cycle commit. From the record alone, **a run doing careful work is
indistinguishable from a wedged one**. Answering "is it alive?" required falling back on a driver
log, the nexus side channel, and `/proc` -- **none of which is the store, and none of which is in
the record**. That list is a concrete requirements statement for the liveness feature: liveness
has to be *emitted by the waiting phase*, not inferred from the record.

## Process defects observed this block (recorded because a good outcome does not retire them)

At 11:26:38 a goal run **this stage did not start** took the run lock on this client
(`run-f9aea1fc1c7346eca0e72cf6d8492882`), while this stage had been told it owned the client
exclusively. Two things about it are wrong regardless of whose it was:

1. It launched from **`/home/matt/CivSolver`**, the shared main repo, **not the detached
   worktree** -- so it ran against a tree carrying other lanes' uncommitted edits.
2. **Nobody checked `/tmp/civsim_harness/run_locks/` before starting it.**

This stage's own probe launched seconds later, was refused at the socket
(`ConnectionRefusedError: [Errno 111]`, `probe-aborted-lockheld.log`), **issued zero game
commands**, and was killed immediately so it could not disturb that run. The colliding run was
**not** killed: it was bounded at 2 turns and a `timeout` SIGKILL leaks a run lock that no
in-process guard can catch.

## Block 09's result, read from the store (the store decides)

**A city was founded by the harness's own decision.** `run-f9aea1fc1c7346eca0e72cf6d8492882`,
turn cycle 1, step 1, `units.found_city`, `parameters {"target": 65536}`,
`dispatch_result {"unit_id": 65536, "ok": true}`. Step 1's own observation read
`cities.state = {"cities": []}`; **step 2's observation -- a later command -- reads**
`LOC_CITY_NAME_PASARGADAE` at plot `{x: 43, y: 30}`, population 1. Goal REACHED at recorded
turn 1.

**And the same record scores it a failure**: `outcome: rejected`,
`rejection_reason: verification_failed`, `confirm_timeout_s: 4.0`, `confirm_poll_s: 1.0`,
`confirm_attempts: 3`, **`confirm_elapsed_s: 7.291`**. Two more in the same run dispatched `ok`
and were denied by the record: `cities.set_production UNIT_SCOUT` at 7.187 s
(`{"ok": true, "reason": "issued_not_yet_confirmed"}`) and `units.move_to` at 7.509 s. Every
**applied** action in that run confirmed at `confirm_elapsed_s: 0.0` on attempt 1.

The set_production denial is now demonstrably wrong on the board as well as in theory: this
block's own close-out read shows Pasargadae with `production_queue: ["UNIT_SCOUT"]`.

This run also closes the remaining half of the availability-gate question: the layer **offered**
`units.found_city` on a board where it was genuinely available, the harness dispatched it, and it
worked -- so the gate discriminates correctly in both directions.

## The coverage block was NOT taken by this stage (second collision, stated plainly)

The stage's third task was a `--provider stochastic --provider-policy coverage` block. It was
**not taken by this stage**, and the reason is a repeat of the first collision rather than a
choice:

- 11:41:45-11:43:35 another lane ran `set_capital_production --provider stochastic
  --provider-policy coverage` **into this block-10 directory**. It **aborted**:
  `HarnessError: a run already in a terminal lifecycle state cannot be stopped |
  detail={'run_id': 'run-65863ee47fc145118115aa8841268ffc', 'lifecycle_state': 'finished'}`,
  with `parts: []` and `reached: false`. So it demonstrated no breadth.
- 11:44:02 this stage checked: lock directory empty, no driver. 11:45:44 it launched its own
  coverage block into `block-11` and found a lock **already held**
  (`run-7cb7148c32564483ab2f2175681cb667`) with three driver processes live -- another lane had
  taken the client inside that 100-second window.
- This stage **SIGTERMed its own run immediately** (SIGTERM, not SIGKILL, because the commit
  backstop catches SIGTERM and releases the lock; a SIGKILL leaks one). It died before reaching
  `Runner.start`, so it **never took a run lock and leaked nothing** -- `block-11/block-11.stdout`
  shows it got only as far as "recording started" and "objective written".
- The lock now belongs to another lane's `move_unit_to_plot --provider stochastic
  --provider-policy coverage --turns 4`, in flight. **That is the coverage block**, so this stage
  stood down rather than fight for the client a second time.

**The scheduling defect, stated once: the client has no admission control that anyone is
honouring.** Checking the lock directory is only sound if every lane checks it and takes the lock
before acting; today two runs started inside windows where another agent had just verified the
board was free, and one of them wrote into a directory another stage was already using. The lock
file works; the convention around it does not.

## Client state left behind

Game turn **4**, `InGame` world screen, `recognized: true`, `has_blocking_prompt: false`,
`is_local_player_turn: true`. Run lock directory **empty**, no runner, tuner connection closed.
Named save **confirmed on disk, not merely issued**:
`civsim__stage6__block10__t0003.Civ6Save`, **1055922 bytes**, size-stable across a recheck.

## Files

- `latency_probe.py` -- the probe (operator scripting; never dispatches `units.found_city`)
- `latency-probe.json` -- full raw record: every dispatch result and every individual poll
- `probe.log` -- the probe's own stdout
- `probe-aborted-lockheld.log` -- the first attempt, refused at the socket by the held lock
- `close_out_save.py` -- named close-out save through the production `saves.save_game` capability
