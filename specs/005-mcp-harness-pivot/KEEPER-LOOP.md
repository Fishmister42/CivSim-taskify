# Keeper Loop — hold the live game in continuous play

**Independent of the migration.** The migration loop (`MIGRATION-LOOP.md`) changes code; this one
changes nothing and only keeps a game being played. They must never run at the same time — the
tuner accepts **one connection**, and a migration tick that touches the client will make the keeper
look broken, and vice versa. Whoever starts second stands down.

**Invocation:**

```
/loop Read specs/005-mcp-harness-pivot/KEEPER-LOOP.md and do one keeper tick.
```

---

## The division of labour, and why it is drawn here

`audit/keeper.py` is a supervisor that does everything **mechanical**: reap stragglers, diagnose
down / wedged / at-menu / in-game, relaunch, recycle, reload the save, checkpoint, run one bounded
play block, record a JSONL line, repeat. It needs no agent and makes no judgements.

**This loop exists for the things a script cannot decide.** Your tick is: read the keeper's own
record, judge what it cannot, act, report. If you find yourself re-implementing relaunch logic in a
tick, stop — that belongs in `keeper.py`, where it runs every cycle instead of whenever an agent
remembers.

**Operator capability is not agent capability.** The keeper reaches the tuner directly to save and
to check health. The agent it spawns only ever sees the MCP tool surface. Do not blur this — it is
the same boundary Principle I turns on.

## Start the supervisor (first tick only, or after it has exited)

```bash
cd /home/matt/civ6-mcp
nohup uv run python audit/keeper.py \
    --out /home/matt/CivSolver/specs/005-mcp-harness-pivot/evidence/keeper \
    --turns-per-block 12 \
    --model qwen/qwen3-235b-a22b-2507 \
    --budget-floor 0.25 \
  > /home/matt/CivSolver/specs/005-mcp-harness-pivot/evidence/keeper/keeper.log 2>&1 &
```

Stop it by creating `evidence/keeper/STOP`. It also stops on its own at the budget floor.

## Each tick, in order

1. **Is the owner asking for the account?** Read the tail of issue #1. If any comment asks for Steam
   back, **touch the STOP file immediately**, confirm the keeper has exited, post a `status: done`
   release on #1, and end the loop. This outranks everything below.
2. **Is the supervisor alive?** `ps -eo pid,comm,args | awk '$2 ~ /^python/ && /keeper.py/'`.
   If not, read the last `keeper.jsonl` line for why it exited, then restart it unless the reason
   was `stop_file` or `budget`.
3. **Read the last 3 cycles** of `evidence/keeper/keeper.jsonl`. Judge only these:
   - **`new_segfault: true`** — resolve the symbol the way this project always does:
     `nm -DC --defined-only <libGameCore_XP2.so>` against `ip − base`. If the symbol is
     `Diplomacy::Action::Instance::GetType`, it is the known one (2026-09-24) — note and move on.
     **Any other symbol is a new crash class**: record it in `evidence/keeper/crashes.md` with the
     preceding tool call from that cycle's `tool_calls.jsonl`, and ban that tool via `--ban` on the
     next restart.
   - **`turns_ended` is 0 for two cycles running** while the state was `in_game` — something is
     blocking turns that is not the client. Check for a modal the auto-dismiss does not handle,
     and screenshot it.
   - **`turn_after` not advancing across cycles** — the reload may be landing on a stale save.
     Verify `KEEPER` on disk is newer than the block that should have checkpointed it.
   - **`reload_failed` twice running** — the menu coordinates have drifted, or a dialog is in the
     way. Screenshot before touching anything.
4. **Post a heartbeat to issue #1 at most every 20 minutes**, with the turn reached, cycles
   completed, turns ended, spend, and any new crash class. Attach a screenshot from the newest
   cycle's `shots/`. *Never* report a turn as played that the record does not show.
5. **Schedule the next tick.** 20–30 min is right: a 12-turn block is ~20 min of wall clock, so a
   shorter tick just watches the same cycle twice.

## What you must not do

- **Do not restart the client yourself.** The keeper diagnoses and heals; a second healer racing it
  produces the wedge it is trying to fix.
- **Do not `pgrep -f`.** It matches your own shell command. On 2026-09-24 it killed the shell that
  ran it. `ps -eo pid,comm,args`, match on `comm`.
- **Do not run `import`.** `audit/shot.py` only.
- **Do not raise the budget floor** to keep playing. The provider's refusal is the stop.
- **Do not edit any CivSolver or fork source from this loop.** Findings go to
  `evidence/keeper/` and issue #1; code changes belong to the migration loop.

## Known-good state, for comparison

The reference run (`evidence/cyrus-run/`, 2026-09-24): fresh Cyrus/Persia from turn 1,
**15 turns, 89 tool calls, 83 applied, 6 engine-refused, 0 transport failures, 0 tool errors,
1190 s wall, 16 distinct tools, 6 action tools, ~$0.05.** A cycle materially worse than that is
worth a look rather than a shrug.

Two defects are expected and are not news:

- **`get_game_overview` fails intermittently** (`Empty overview response`; also
  `Resolving Buffered Parameters` reaching a parser expecting 14 fields). The parsers raise instead
  of retrying. The agent routes around it. Only escalate if it starts failing *every* call.
- **`unit_action(found_city)` can return a different tile than requested**, and `get_cities` can
  read empty immediately after a successful founding.

## The artifact sub-loop

The paper trail is **not** this loop's job either. A separate cron sub-loop
(`7,22,37,52 * * * *`, job `3ccaf0a2`) rebuilds it from `evidence/cyrus-run` plus every
`evidence/keeper/cycle-*` and republishes to
**https://claude.ai/artifact/LAumGNJ7GLPx2NVMjibhYL**, skipping the publish when the turn count has
not moved. Three loops, three jobs, one client: the keeper plays, the migration loop changes code,
the sub-loop renders. None of them touches another's surface.

The sub-loop is session-only and dies with the session — re-create it with CronCreate if the
artifact stops moving while cycles are still landing.

## Stop conditions

1. The owner asks for Steam (tick 1) — stop immediately.
2. Budget floor reached — the keeper stops itself; confirm, report the final tally, end the loop.
3. Three consecutive cycles reach no in-game state.
4. A new crash class reproduces twice after banning the implicated tool.
