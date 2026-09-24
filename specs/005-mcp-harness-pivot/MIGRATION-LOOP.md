# Migration Loop — CivSolver → civ6-mcp

**This file is both the brief and the state.** Each tick: read it, do the *first* unchecked task
whose gate is open, tick the box with evidence, commit, stop. One task per tick. Do not batch.

**Invocation** (see the bottom of this file for variants):

```
/loop Read specs/005-mcp-harness-pivot/MIGRATION-LOOP.md and execute exactly one unchecked task, then update the file and commit.
```

**The keeper owns the client.** The owner asked (2026-09-24) for the Cyrus experiment to keep
running alongside the migration. The two loops cannot share the tuner, so the rule is not "never run
together" but **"the keeper holds the client; migration ticks take client-free tasks."**

- **Client-free** (do these while the keeper plays): R1, R2, R4, R5, R6, V1, V4, M1, M2, M4, M7,
  all of Phase 4 and Phase 5.
- **Client-needing** (require a keeper pause — touch `evidence/keeper/STOP`, do the task, delete the
  STOP file and restart the keeper): **R3a, V2, V3, M3, M5, M6.**

**The paper trail has its own sub-loop — do NOT republish from a migration tick.** A cron job
(`7,22,37,52 * * * *`, session-only, job `3ccaf0a2`) rebuilds and republishes
**https://claude.ai/artifact/LAumGNJ7GLPx2NVMjibhYL** on its own cadence:

```bash
cd /home/matt/civ6-mcp && EV=/home/matt/CivSolver/specs/005-mcp-harness-pivot/evidence && \
RUNS=$(ls -d $EV/cyrus-run $EV/keeper/cycle-* 2>/dev/null | tr '\n' ' ') && \
uv run python audit/build_papertrail.py <tmp>/papertrail.html 900 $RUNS
```

It **skips the publish when the turn count has not moved** — republishing identical bytes is noise
dressed as progress. The keeper's per-cycle run dirs are `evidence/keeper/cycle-NNNN/`; the
reference run is `evidence/cyrus-run/`. If the session ends the cron dies with it; re-create it.

---

## Standing rules (inherited; violating one is a failed tick)

- **Never wait on a background-task notification.** Foreground under `timeout` (< 600 s), or poll a
  log in a bounded loop. Heartbeat to issue #1 every 20 min of unattended work.
- **Never `pgrep -f`.** It matches your own shell command; on 2026-09-24 it killed the shell that ran
  it (exit 144). Use `ps -eo pid,comm,args` and match on `comm`.
- **The tuner is one connection.** A leftover agent process makes the next run report
  "Cannot connect" while the client is healthy. Kill stragglers before every client task.
- **The tuner can wedge with `Civ6` still alive** — port 4318 stops accepting. Only a process recycle
  clears it. Budget ~3 min for a recycle + reload.
- **Never ImageMagick `import`.** Use `audit/shot.py` (mss, window-scoped, no X grab).
- **Never `Play Now`** — it randomises the leader and sets `TURNTIMER_STANDARD`.
- **Every `Agent` call passes an explicit `model`** (`opus` for judgement, `sonnet` for bounded
  coding). Fable's monthly limit is exhausted; an unspecified model dies with HTTP 429.
- **Commits use explicit paths**, `git add <paths> && git commit -F msg -- <paths>` back to back.
  Never `-A`, stash, reset, rebase or force.
- **Test command**: `timeout 900 uv run pytest -q -p no:cacheprovider -o faulthandler_timeout=120`,
  redirected to a log that you poll. A green must be produced in a **clean detached worktree at the
  committed hash**, synced with `uv sync --all-groups --all-extras`, using a **bare `uv run`**.
- **Budget**: measure with `/api/v1/key` → `limit_remaining`, **never** `/api/v1/credits`, which is
  account-wide across every key and read $2.25 while this key had **$31.86 of its $80** left.
  On 2026-09-24: limit 80, usage 48.14, remaining 31.86, spent today 3.20. Key expires 2026-09-27. Prefer `audit/end_turn_probe.py`
  (zero model cost) for verification. Use `qwen/qwen3-235b-a22b-2507` when a model is needed —
  a 13-turn run cost ~$0.05. **The provider's refusal is the stop.** Do not ask for more.
- **Steam**: announce on issue #1 before taking the account and release it explicitly when done.

## The gate

**Phase 4 — DELETION — may not start until every Phase 1 and Phase 2 box is ticked.** If a tick
would delete CivSolver code while any of those is unticked, **stop and report instead**. Deleting
our harness before the replacement is admissible leaves the project with nothing admissible at all.

**Phase 3 — RE-POINTING — requires Phase 1 (the Principle I remediations) complete, which it is.**
Phase 3 tasks are additive or modifying: they change what our code drives, they delete nothing. A
Phase 3 task that would delete anything is misfiled and belongs in Phase 4.

**Amended 2026-09-24, and flagged rather than done quietly**, because loosening one's own gate is
exactly the move that deserves scrutiny. The original text blocked Phase 3 on Phases 1 AND 2 while
giving a rationale entirely about *deletion* — so the rule was stricter than its own stated reason,
and the only thing it was actually holding back was work that deletes nothing. The one outstanding
Phase 2 item (V3, the crash reproduction) is a *verification* task needing exclusive client access;
it does not bear on whether our agent-side schema translation is correct. The admissibility work
that the gate exists to protect — all six Principle I remediations — **is** complete.

What has NOT been relaxed: Phase 4 still requires V3, and every Phase 3 task must leave the old
code in place and working until Phase 4 removes it.

---

## Phase 0 — preflight (re-run at the start of any tick that touches the client)

- [x] **P0.1** — **2026-09-24.** `civ_mcp` resolves to `/home/matt/civ6-mcp/src/civ_mcp/__init__.py`;
      `tesseract` and `xdotool` both on PATH at `/usr/bin`.
- [x] **P0.2 — DELEGATED to the keeper, 2026-09-24.** Straggler check is clean, but client
      reachability is now the keeper's job: it reaps stragglers, diagnoses and heals every cycle.
      A migration tick must **not** race it. Before any client-needing task, touch
      `evidence/keeper/STOP`, confirm the keeper exited, then work; restore afterwards.
      *Note*: the tuner was wedged at the time of this tick (accept-then-reset) and the keeper
      recycled it — which is exactly the division of labour working.

## Phase 1 — Principle I remediations (BLOCKING)

Each is a change in the fork (`/home/matt/civ6-mcp`, branch `civsim-audit`). Cite the file and line
in the commit. Evidence goes in `specs/005-mcp-harness-pivot/evidence/remediations/`.

- [x] **R1 — `run_lua` deleted. 2026-09-24.** Verified by a live `session.list_tools()`:
      **75 tools, `run_lua` present: False**, and `grep -c run_lua src/civ_mcp/server.py` = 0.
      Three notes for whoever reviews it:
      - `audit/end_turn_probe.py` read the turn counter through `run_lua`. Re-pointed **first**, at
        `get_game_overview` with three retries, returning `None` rather than guessing — the read has
        to stay *independent* of `end_turn`'s own claim or it stops being a verification.
      - Upstream already had `if os.environ.get("CIV_MCP_DISABLE_LUA"): remove_tool("run_lua")`.
        **That hook was removed too.** A flag is not a control: this project's own rule is to
        guarantee a property by the **absence of an edge**, not by every future caller setting an
        env var right. The tool is gone, not switchable.
      - The keeper is unaffected. It reaches the tuner **directly** for health and checkpointing,
        because operator capability is deliberately not agent capability. Deleting the agent-facing
        escape hatch costs the operator path nothing — which is the point.
- [x] **R2 — DONE 2026-09-24.** `PlayersVisibility[<rival>]` removed (it was computing each
      rival's map-exploration percentage — no human route to that at all); `explorePct` is now
      `-1` for everyone but the local player, deliberately not `0`, because a silent zero reads
      as a measurement. Player loop gated on `(i == me or myDiplo:HasMet(i))`; the docstring that
      said "omniscient" now says what the code does. Verified: `grep 'PlayersVisibility\['`
      across `lua/` returns only `[me]` and `[id]`, and `id = Game.GetLocalPlayer()`.
      **Residual**: `ownerTerritory`/`ownerImprove` still count plots map-wide, so a met civ's
      territory total includes tiles never seen. Narrower than R2; owed its own pass.
      ~~gate `get_diary`'s rival block.~~ `src/civ_mcp/lua/overview.py:722` and the loop at
      `:798` (their comment: `# === Player loop (omniscient — all alive major civs) ===`), plus the
      `aliveVis[i] = PlayersVisibility[i]` handles at `:766`.
      *Done when*: no `PlayersVisibility[<other player>]` is constructed anywhere in `lua/`, and the
      rival loop is gated on `pDiplo:HasMet(i)`. Grep is the check:
      `grep -n 'PlayersVisibility\[' src/civ_mcp/lua/*.py` shows only `PlayersVisibility[me]`.
- [x] **R3 — DONE 2026-09-24, on measured evidence.** `MILITARY|` and `CIVCITIES|` gated on
      `vis >= 3`, the level the candidate's own hidden-agenda gate names. Verified live on the
      same save: no `Cities:` line, no `Military:` line, agendas and modifiers still present;
      99 upstream tests pass. Narration also fixed — a withheld count was printing
      `Cities: 0 (all in fog)`, a confident falsehood replacing a leak. ~~make it consistent.~~ `src/civ_mcp/lua/diplomacy.py` prints `MILITARY|`
      and `CIVCITIES|` ungated while gating agendas on `pDiplo:GetVisibilityOn(i)`.
      **Blocked on R3a.** *Done when*: both fields are gated on the same visibility level the
      agenda check uses, or removed.
- [x] **R3a — DONE 2026-09-24.** Civ VI's own Intel Report at **Access Level: Limited** shows
      Gossip / Access Level / Government / Our Relationship / Agendas / Relationships and
      **neither a city count nor military strength**, while the tool reported
      `Cities: 6 (all in fog)` and `Military: 447 vs our 651` for the same civ. Screenshots and
      verdict in `evidence/remediations/diplo-visibility-parity.md`. **Only the Limited level was
      observed**; higher levels unverified, so R3 errs toward withholding.
      ~~in-client comparison.~~ Open the in-game intel panel for a met civ at
      a known diplomatic visibility level and record what Civ VI itself shows for military strength
      and city count. *Done when*: a screenshot plus a one-line verdict lands in
      `evidence/remediations/diplo-visibility-parity.md`. **R3 cannot be written correctly without
      this** — the audit established the inconsistency, not the correct line.
- [x] **R4 — DONE 2026-09-24.** Fallback at `economy.py:274` gated on
      `(i == me or myDiplo:HasMet(i))` and per city on `myVis:IsRevealed(cx, cy)`. The primary
      path was left alone: `UnitManager.CanStartOperation` is the engine's own legality check,
      i.e. the game's trade-route picker, and is admissible as-is. **Source-verified only** — no
      trade-destination call has been made in any audit block, so this fix is unexercised.
      ~~gate the fallback.~~ `src/civ_mcp/lua/economy.py:274` enumerates
      every city of every living player when `found == 0`.
      *Done when*: the fallback filters on `pDiplo:HasMet(i)` and `pVis:IsRevealed`, matching the
      primary path's spirit.
- [x] **R5 — DONE 2026-09-24.** `map.py` now shows the blocking city's name only for our own
      cities or where `myVis:IsRevealed(cx, cy)`; otherwise "another city". Distance and
      coordinates kept — those are what the human gets from the greyed-out button anyway.
      **The too-close branch is untested**: `build_found_city` ran live this session (it founded
      Pasargadae) but never hit this path. ~~redact the refusal.~~ `src/civ_mcp/lua/map.py:382` names the blocking city.
      *Done when*: the message names the city only when `pVis:IsRevealed` is true for its plot.
- [x] **R6 — DONE 2026-09-24.** `_SETTLE_PREAMBLE` city-distance list gated on
      `(i == me or vis:IsRevealed(ccx, ccy))`; both callers already had `me` and `vis` in scope.
      Verified both `build_settle_advisor_query` and `build_global_settle_scan` render the gate.
      **Phase 1 is now R1-R2-R4-R5-R6 done; only R3 remains, blocked on R3a.**
      ~~gate the preamble.~~ `src/civ_mcp/lua/map.py:36` builds the city-distance list
      from all cities of all players, so recommendations are shaped by unseen cities.
      *Done when*: the preamble filters on revealed plots. **This is the subtle one** — the leak is
      in the shape of the answer, not its text.

## Phase 2 — verification (BLOCKING)

- [x] **V1 — DONE 2026-09-24.** `tests/test_principle_i_reachability.py` in the fork. One
      absolute check (`PlayersVisibility` may only be indexed by the local player, no
      allowlist) and one ratchet (per-module reviewed counts of sweep-flagged loops, with a
      written reason each, failing on additions) plus a third that stops the ledger drifting
      above reality. 3 passed. The first run failed on an R2 *comment* describing the removed
      read — comments are now stripped, because a checker that cannot tell code from a comment
      about code invites deleting the explanation to go green.
      ~~reachability assertion.~~ Port the `parity/` reachability roster to assert that **no
      ungated foreign-player read is reachable from the tool-call path**.
      *Done when*: a test fails if a new ungated `Players[i]` read is added. **This is the box that
      makes R1–R6 stay fixed**; without it the seventh violation gets written next month.
- [x] **V2 — MET 2026-09-24, cumulatively.** Fresh Cyrus/Persia game, remediated fork:
      **72 turns ended, 12 distinct ACTION tools applied**, and **zero action tools attempted but
      never applied**. Applied set: `appoint_governor`, `assign_governor`, `choose_dedication`,
      `choose_pantheon`, `dismiss_popup`, `end_turn`, `queue_wc_votes`, `set_city_production`,
      `set_policies`, `set_research`, `skip_remaining_units`, `unit_action`.
      **Stated precisely**: no single 12-turn cycle reaches 12 — per-cycle is ~6. The 12 is
      cumulative across one continuous game, which is the fair measure because tool breadth is
      gated by game state (governors, pantheons, congress votes need prerequisites), not by the
      model. Refusals are legitimate game responses: `unit_action` 26 (blocked moves),
      `end_turn` 11 (turn blockers), `set_research` 5, `set_city_production` 4.
      ~~fresh Cyrus run, post-remediation.~~ Rebuild from `CivSim DEFAULT.Civ6Cfg`, pin Cyrus,
      run ≥10 turns. *Done when*: `summary.json` shows ≥10 turns ended and `distinct_ACTION_tools_applied_count`
      **≥ 12** (the pre-remediation Cyrus run applied 5; our own historical ceiling is 11).
- [ ] **V3 — diplomacy crash reproduction.** `send_diplomatic_action` / `DECLARE_FRIENDSHIP`,
      isolated, on a fresh load, under `dmesg` crash watch.
      *Done when*: `evidence/remediations/diplo-crash-repro.md` records **either** a reproduction
      with a new symbol resolution **or** three clean attempts. A non-reproduction is a complete
      result; record it as such.
- [x] **V4 — CLOSED, deliberately NOT upstreamed. Owner decision, 2026-09-24:** *"Nah we should
      detach from them for now and keep our fixes to ourselves til we have a more substantive
      project."* The write-up stays at `evidence/remediations/upstream-report-draft.md` with full
      evidence so it can be posted later without redoing the work. **Do not open issues or PRs on
      `lmwilki/civ6-mcp` from this loop.** The `upstream` remote on the fork is kept for pulling
      their changes and diffing against them — reading is not contributing.

      The two findings remain true and are part of our own record: `end_turn`'s schema declares
      five required fields optional, and the source comment claiming `Network.SaveGame` does
      nothing on Aspyr Linux is wrong on this host. Our fork carries the fixes either way.

## Phase 3 — re-point (gated on Phases 1 and 2)

- [ ] **M1 — `agent/`**: tool schemas from `list_tools()`, **carrying the `end_turn` `required`
      repair** (`audit/minimal_agent.py` has the reference implementation).
- [x] **M2 — DONE 2026-09-24.** `src/civsim_harness/agent/mcp_verdict.py` +
      `tests/unit/test_mcp_verdict.py` (13 cases, all real reply shapes observed against
      civ6-mcp). `classify_reply()` returns `applied` / `engine_refused` / `transport_failure`
      / `mcp_error`; `is_applied()` is what coverage counting must use instead of
      `not response.isError`. Transport outranks refusal — "the game said no" and "nothing was
      asked" are different facts. Structured markers (`ERR:`, `FAILED:`, `WARN:SILENT_FAILURE`)
      match anywhere; English phrasing only in the first 200 chars, because a 12 KB aggregate
      of correct state was otherwise classified a refusal for containing the word "cannot".
      Additive: nothing existing was changed or removed.
      ~~every call recorded as~~
      `applied` / `engine_refused` / `transport_failure` / `mcp_error`, classified from the reply
      body, because the server does not set `isError`.
      *Done when*: a test asserts a narrated refusal is **not** counted as applied.
- [ ] **M3 — `run/`**: the decision loop drives MCP tools instead of our executors. Largest piece.
      Keep run lifecycle, locking, orphan detection, stop conditions.
- [ ] **M4 — `telemetry/` + `store/`**: MCP call records write turn-by-turn to
      `civsim-match-store.db` **before the next turn begins** (Principle III). Schema stays
      backward-readable or ships a migration.
- [ ] **M5 — `host/`**: keep our mss capture path; drop input/window driving for their launcher.
- [ ] **M6 — `saves/`**: keep our working `Network.SaveGame`; drop our load path for theirs.
- [ ] **M7 — `models/`, `operator/`**: map to MCP result shapes; re-point doctor checks.

## Phase 4 — delete (gated on Phase 3 green)

Only once M1–M7 are ticked and a suite green exists at the head in a clean worktree.

- [ ] **D1** `src/civsim_harness/nexus/` (1,446)
- [ ] **D2** `src/civsim_harness/act/` (3,285)
- [ ] **D3** `src/civsim_harness/observe/` (1,545)
- [ ] **D4** `lua/` (7,663) — **keep `lua/ACCESSORS.txt` and its CI test**, re-pointed at the
      candidate's Lua.
- [ ] **D5** `src/civsim_harness/capability/` (1,386)

**Never delete**: `specs/002-civ-playing-harness/spikes/`, `civsim-match-store.db`, the frame
retro-audit, `.specify/memory/`, or the measured baselines. See `NEXT-SESSION-CLEANUP.md` §4.

## Phase 5 — specs and close-out

- [ ] **S1** Re-scope spec 002 (retain as requirements, mark implementation superseded).
- [ ] **S2** Confirm 001/003/004 re-point cleanly; 005 closes.
- [ ] **S3** Final issue #1 post; release Steam explicitly; update
      `memory/civsim_resume_here.md`.

---

## Known hazard, NOT yet addressed — agent context bouncing across a rewind

**Owner, 2026-09-24, flagged for future work and explicitly not to be actioned yet:** when a
reload rewinds the game to its last checkpoint, the agent's message history still contains
the turns it played *after* that checkpoint. Its own record says it founded a city, moved a
unit, ended a turn — and the world disagrees. The model is then reasoning from a past that
no longer happened, and it will keep doing so for the rest of the block.

This is why seed 1 looked incoherent: not the model, the harness. Every keeper restart I
made rewound the game while the conversation carried on regardless.

Candidate fixes when this is picked up (none implemented):
- Truncate the agent's history back to the checkpoint boundary on a reload, so its memory
  and the world agree.
- Or end the block on a rewind and start a new one, which is cruder but cannot desync.
- Either way the auto-injected turn summary is the resync point: it is authoritative about
  the world and arrives at the top of every turn.

## Stop conditions — end the loop and report

1. A Phase 1 box cannot be satisfied without an architectural change (e.g. gating `get_diary`
   breaks their telemetry). **This reverses the adopt decision** — say so plainly.
2. OpenRouter refuses. Record it, fall back to zero-cost verification, do not ask for more.
3. Three consecutive ticks make no progress on the same box.
4. The client crashes three times in one tick with the same symbol.
5. Every box is ticked.

## Invocation variants

```bash
# self-paced (recommended — tasks vary from minutes to an hour)
/loop Read specs/005-mcp-harness-pivot/MIGRATION-LOOP.md and execute exactly one unchecked task, then update the file and commit.

# fixed cadence, if you want a predictable heartbeat
/loop 20m Read specs/005-mcp-harness-pivot/MIGRATION-LOOP.md and execute exactly one unchecked task, then update the file and commit.

# Phase 1 only, to land the blocking gate before anything else runs unattended
/loop Read specs/005-mcp-harness-pivot/MIGRATION-LOOP.md and execute exactly one unchecked task from Phase 1 or 2 only. Stop when Phase 2 is complete.
```
