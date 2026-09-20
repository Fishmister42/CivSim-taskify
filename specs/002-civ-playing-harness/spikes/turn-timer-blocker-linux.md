# 🔴 Run-blocker — a turn timer is active, and it will expire the agent's turn

**Feature**: `002-civ-playing-harness` · **Executed**: 2026-09-20 · **Host**: Linux (native Aspyr)
**Client**: `1.0.12.9 (564030)`, Gathering Storm, BBG + Multiplayer Helper 1.7.9 active

## The finding

**`GameConfiguration.GetTurnTimerType()` resolves to `TURNTIMER_STANDARD`** in a single-player
"Play Now" game on this host. A turn timer is running, and turns advance on it with **no input at
all**.

Reproduced deliberately, with one end-turn issued and then nothing touched for 130 seconds:

```
before:      turn=1 canEnd=true
t+15s:       turn=2 canEnd=true
t+35s:       turn=3 canEnd=true
t+60s:       turn=4 canEnd=true
t+95s:       turn=5 canEnd=true
t+130s:      turn=6 canEnd=true
```

Roughly one turn per 25 s, indefinitely, from a single end-turn request.

## Why this blocks the run loop

This is not a nuisance; it invalidates three requirements at once.

- **FR-014** says nothing bounds a turn by time — *"A turn of several hundred steps running for hours
  is a valid turn."* With a turn timer, the agent's turn expires **while it is thinking**. A model
  call taking 30 s is already over budget.
- **FR-011** verifies a turn advance by comparing the turn number before and after the agent's
  end-turn decision. **A turn that advances on a timer is indistinguishable from one the agent
  ended.** The verification predicate silently reports success for turns the agent never ended.
- **FR-015 / SC-022** no-progress accounting breaks the same way: the backstop cannot distinguish a
  stuck turn from a timer-expired one, and the `ended_by_agent` vs `ended_on_no_progress` distinction
  the spec exists to preserve is destroyed.

The failure mode is the dangerous kind — **the run keeps going and the record looks plausible**. Every
turn would carry a decision and a turn advance; they would simply not be causally related.

## Correction to an earlier report

I previously flagged this as **auto-end-turn being enabled**. **That was wrong and I am retracting
it.** `UserOptions.txt` line 12 reads `AutoEndTurn 0` — it is correctly disabled. I inferred the cause
from the symptom instead of measuring it.

The measurement that settled it: after loading a save, **the turn did not advance for 90 s with no
input**. The advance only begins once an end-turn is issued — which is the timer starting, not
auto-end-turn.

## Where it comes from — likely, not confirmed

`IsAnyMultiplayer = false`, `IsHotseat = false`, `IsNetworkMultiplayer = false`, yet a *standard*
turn timer is set. The most probable source is **Multiplayer Helper 1.7.9**, which is active in the
mod set and exists to add turn-timer behaviour. **This is a hypothesis — I have not isolated it** by
disabling the mod.

Worth settling before designing around it, because the fix differs:

- **If it is the mod**: either drop it from the pinned `mod_set`, or find its configuration. Note the
  mod set is part of seed-set identity, so changing it is a comparability decision, not a
  convenience one.
- **If it is a game-setup default**: it becomes a required field in the `CivSim DEFAULT` preset and a
  preparation-time assertion.

## What preparation must do regardless

**Read `GameConfiguration.GetTurnTimerType()` at preflight and refuse to start unless it resolves to
a no-timer value.** This belongs with the other V2 read-back assertions and is cheap:

```lua
local t = GameConfiguration.GetTurnTimerType()
-- must equal DB.MakeHash("TURNTIMER_NONE")  (-1525060181 on this build)
```

Do not assume single-player implies no timer. This host is the counterexample.

## Bonus: how to resolve hashed config values

This spike produced the technique that answers the *"config getters return hashes, not strings"*
problem recorded in [lua-api-verification-linux.md](./lua-api-verification-linux.md).

**`DB.MakeHash(name)` exists and is callable.** Two usable directions:

**Forward — hash a known name and compare.** Best for assertions, since it needs no table scan and
is localisation-independent:

```
TURNTIMER_NONE      -> -1525060181
NO_TURNTIMER        -> -1206781825
TURNTIMER_STANDARD  ->  2133509568   <== this host's value
TURNTIMER_DYNAMIC   ->   698670180
TURNTIMER_FIXED     ->  1033907547
```

**Reverse — scan the matching `GameInfo` table for `row.Hash == value`.** Best for records, where a
human needs to read the value back:

```
GameSpeed  hash=327976177    -> GAMESPEED_STANDARD
Difficulty hash=-179952465   -> DIFFICULTY_PRINCE
StartEra   hash=-1851407529  -> ERA_ANCIENT
```

Recommendation stands from the earlier spike: **store the hash as authoritative** (stable, not
localised) and resolve a display name through `GameInfo` for the human-readable record. Preflight
assertions should use the forward direction.

## Also worth knowing: rapid reconnects get refused

While testing this, a probe that opened a fresh tuner connection per reading hit
`ConnectionRefusedError: [Errno 111]` mid-run, and the client was **fine** — still running, still
listening.

The client accepts **one tuner connection at a time**, and a rapid disconnect/reconnect cycle can be
refused while the previous socket is still being released. `contracts/nexus-protocol.md` already
mandates one `NexusClient` per run holding the socket, so the design is right — but a *transient
connection refusal is not evidence the client crashed*, and crash detection (FR-044, SC-010) should
not treat it as such. Re-probe before declaring a crash.

## Scope limits

- One host, one mod set, "Play Now" games only. The `CivSim DEFAULT` preset has **not** been tested
  and may set a different turn timer.
- The mod attribution is unconfirmed.
- Timer interval (~25 s) was estimated from turn transitions, not read from configuration.
