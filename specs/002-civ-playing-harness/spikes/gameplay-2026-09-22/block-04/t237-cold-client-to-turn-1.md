# T237 — the path from a running client's main menu to turn 1, with zero human input

MEASURED 2026-09-22 15:00:40Z–15:01:58Z (local 11:00:40–11:01:58), live Linux client 1.0.12.9.
Driver: `tests/live/demo_landed_run.py --bring-up`. Log lines quoted below are verbatim.

## Verdict, bounded

**T237 does NOT close. It narrows to exactly one step: launching the client.**

The Civ VI process was **already running** when this block began (`pgrep -x Civ6` → 2459457,
checked before anything was touched). The harness drove it from its main menu; the harness did not
start it. T237 asks for a verified path **from a cold client**, and a client already up at its menu
is not a cold client.

What IS now verified, and was not before: **from a running client sitting at the main menu, the
path to turn 1 required zero human input.** Every step below was issued by code. No click, no
keystroke and no decision came from a person.

## The step list

| # | Step | Mechanism | Harness-driven or operator-required |
|---|---|---|---|
| 0 | Launch `Civ6` and reach the main menu | — | **NOT DEMONSTRATED THIS BLOCK.** The process was already up. This is the whole of what remains of T237. |
| 0b | Leave a finished game to reach the menu | `Events.ExitToMainMenu()` from `InGame` | Harness-driven, **and not exercised here** — the client was already at the menu (this block put it there by clearing `EndGameMenu`), so `bring_up` skipped it: `InGame` was absent from the state list. Previously exercised on other blocks. |
| 1 | Wait for the front end | poll `refresh_state_indices()` until `HostGame` present and `InGame` absent | Harness-driven |
| 2 | Reset config | `GameConfiguration.SetToDefaults()` | Harness-driven |
| 3 | Load the preset | `Network.LoadGame({... Name="CivSim DEFAULT", FileType=GAME_CONFIGURATION}, SERVER_TYPE_NONE)` | Harness-driven |
| 4 | Claim a human slot | `PlayerConfigurations[0]:SetSlotStatus(SlotStatus.SS_TAKEN)` | Harness-driven |
| 5 | Pin leader and civ | `SetLeaderTypeName("LEADER_CYRUS")`, `SetCivilizationTypeName("CIVILIZATION_PERSIA")` | Harness-driven |
| 6 | Set the map seed | `GameConfiguration.SetValue("RANDOM_SEED", 20260922)` | Harness-driven |
| 7 | Read back the setup before committing to it | `preset_loaded`, `humans`, `leader`, `seed`, `turn_timer_none` | Harness-driven |
| 8 | Start the game | `Network.HostGame(ServerType.SERVER_TYPE_NONE)` | Harness-driven |
| 9 | Dismiss the leader-intro screen | **3 synthetic `Escape` presses** via `focus_window` + `send_input`, retried until the tuner handshake answers | Harness-driven, **but see the Principle II finding below** |
| 10 | Confirm turn 1 and the pinned identity | `LuaGameSetupReader` (production path) | Harness-driven, **production** |

Verbatim read-back at step 7:

    {'preset_loaded': True, 'humans': 1, 'leader': 'LEADER_CYRUS',
     'seed': '20260922', 'turn_timer_none': True}

Verbatim confirmation at step 10:

    turn=1 civilization=CIVILIZATION_PERSIA leader=LEADER_CYRUS
    ruleset=RULESET_EXPANSION_2 difficulty=DIFFICULTY_EMPEROR
    game_speed=GAMESPEED_ONLINE starting_era=ERA_ANCIENT
    map_size=MAPSIZE_SMALL map_seed=1745156737 major_count=5

Steps 1–9 live in `tests/live/demo_landed_run.py`, which is **operator scripting, not production**.
That is a statement about which side of the production line the code sits on, **not** about whether
a human is needed — no human was. `run/preparation.py`'s gate still requires an already-loaded
game, so production cannot perform steps 1–9 at all; only step 10 is production code.

## Reproducing this exact board

    preset        CivSim DEFAULT   (GAME_CONFIGURATION save, SaveDirectories.DEFAULT)
    humans        1  (PlayerConfigurations[0], SS_TAKEN)
    leader / civ  LEADER_CYRUS / CIVILIZATION_PERSIA   (pinned pre-start, survives HostGame)
    RANDOM_SEED   20260922        (set at step 6)
    turn timer    TURNTIMER_NONE  (read back true at setup; `Play Now` would have set
                                   TURNTIMER_STANDARD and silently broken turn verification)
    ruleset       RULESET_EXPANSION_2      difficulty  DIFFICULTY_EMPEROR
    game speed    GAMESPEED_ONLINE         map size    MAPSIZE_SMALL, 5 majors
    run id        run-e8c96e29c8474d0f958eec032ca69d00
    save          civsim__run-e8c96e29c8474d0f958eec032ca69d00__t0001.Civ6Save
                  707,431 bytes, size-stable across two stats 4 s apart, md5 8401cb95eb16…

**Unexplained, recorded rather than smoothed over:** `RANDOM_SEED` was set to `20260922` and read
back as `20260922` at setup, but the in-game `LuaGameSetupReader` reports `map_seed=1745156737`.
These are two different fields and the in-game one is not the value that was set. Anyone relying on
`RANDOM_SEED` for a reproducible map must settle which field actually determines the map before
trusting it. Not investigated here.

## Principle II finding: the intro-dismiss Escapes are on an undeclared synthetic-input path

Step 9 drives real synthetic input (`HostPlatform.send_input`, `InputEventKind.key_press`).
`tests/contract/test_synthetic_input_declaration.py` exists precisely so a synthetic-input path
cannot ship undeclared — but it re-derives call sites **from `src/civsim_harness` only**. Step 9's
call site is in `tests/live/demo_landed_run.py`, which that scan never reaches, so the contract
test neither covers it nor could fail on it.

This is not a violation of what the test enforces; it is a gap in what the test can see. Because
the bring-up is operator scripting rather than a catalog capability, nothing required it to be
declared. But if any part of steps 1–9 is ever promoted toward production to close T237's remaining
step, **step 9 must be declared `path: bespoke` with a non-empty `firetuner_gap` at that moment** —
there is no Lua call reachable from the front end that dismisses the leader intro, which is exactly
the gap that declaration is for. Filed here so the promotion cannot happen quietly.
