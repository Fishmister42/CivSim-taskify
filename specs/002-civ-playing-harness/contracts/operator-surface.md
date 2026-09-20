# Contract: Operator Surface

**Feature**: `002-civ-playing-harness` | **Schema version**: 1

Lifecycle control without opening, focusing, or interacting with the Civilization VI client
(FR-004) — bounded so it never becomes a second presentation of run state (FR-053, Principle VI).

Two forms of the same contract: a `typer` CLI for humans, and a loopback-only HTTP endpoint for the
directing session and for deliverable 4's orchestration.

## Commands

| Command | HTTP | Effect |
|---|---|---|
| `civsim run start <config.yaml>` | `POST /runs` | Validate, preflight, prepare, begin playing |
| `civsim run pause <run_id>` | `POST /runs/{id}/pause` | Pause at the next safe point |
| `civsim run resume <run_id>` | `POST /runs/{id}/resume` | Resume a paused run |
| `civsim run stop <run_id>` | `POST /runs/{id}/stop` | Stop with `operator_stop` recorded |
| `civsim run resume-from <run_id> --turn N` | `POST /runs/{id}/resume-from` | Resume the same run from a recorded save |
| `civsim run branch <run_id> --turn N --config <c.yaml>` | `POST /runs/{id}/branch` | Start a new run from that save, recording lineage |
| `civsim run archive <run_id>` | `POST /runs/{id}/archive` | Mark a terminal run archived — the only thing that makes its saves eligible for removal (FR-004, FR-036) |
| `civsim run status <run_id>` | `GET /runs/{id}/status` | Lifecycle diagnostics — see the bound below |
| `civsim seedset accept-build <set> --to <build> --reason <text>` | `POST /seedsets/{name}/accept-build` | Record an operator acceptance of a Civ VI build change for that set (FR-002) |
| `civsim saves reap --dry-run` / `--confirm` | *(no HTTP form)* | Delete save files for **already archived** runs only. Operator-invoked; never a background job (R17) |
| `civsim doctor` | `GET /health` | Host platform and support tier, tuner connection, client liveness and build, store reachability, catalog load, capture path, disk headroom |

**Every command is recorded as a `lifecycle_command_received` run event** before it takes effect, so
the timeline shows what was asked as well as what happened.

**Pause is safe-point, not immediate.** It takes effect at a turn boundary rather than mid-turn —
interrupting between executing an action and persisting the turn is precisely the half-written state
FR-013 forbids. A researcher wanting an immediate halt uses `stop`, which also lands on a boundary
but records a terminal condition.

**A pause may therefore take a long time to land, and that is correct.** A turn has no time bound
(FR-014), so a pause requested during a 300-step late-game turn waits for that turn to finish. The
command is acknowledged and recorded immediately; the state transition happens at the boundary. What
the harness must not do is cut the turn short to honour the pause faster — that would be a
harness-initiated turn ending, which FR-008 and SC-022 forbid. `status` reports the requested state
alongside the current one so the wait is visible rather than looking like a hung command.

**Archive and reap are two steps, deliberately.** `archive` is a recorded decision that this run is
no longer a branch source; `reap` is the separate, explicitly-invoked deletion over what archival
made eligible. Keeping them apart means no unattended process ever deletes a save, and `--dry-run`
is the default posture for the destructive half (FR-036, research R17).

## The FR-053 bound — what `status` may and may not return

`status` returns **only** lifecycle and diagnostic fields:

```json
{
  "run_id": "run_01J8X...",
  "lifecycle_state": "interrupted",
  "requested_state": null,
  "current_turn": 41,
  "current_step": 137,
  "last_known_good_save": { "turn": 41, "save_point_id": "sp_..." },
  "last_error": { "type": "crash_detected", "at": "2026-09-19T14:22:31Z" },
  "connection_health": { "tuner": "disconnected", "client": "not_running", "store": "ok" },
  "record_completeness_status": "unknown",
  "comparability_status": "comparable",
  "archived": false,
  "disk_headroom_gb": 41.2
}
```

It **must not** return turn records, observations, decisions, reasoning, yields, metric series, or
captures. Those live in the store and are deliverable 1's to present. The distinction is not
stylistic: two surfaces rendering run state will eventually disagree, and Principle VI exists
because a disagreement between the user's picture and the session's picture is the specific failure
this project cannot afford.

**`current_step` is a progress indicator, not a record.** With unbounded turns, a run can legitimately
sit on one turn for hours, and without this an operator cannot tell a working run from a wedged one.
It is a bare integer for exactly that reason — the step's observation, decision, and reasoning stay
on the other side of the FR-053 line. `requested_state` exposes a pause that has been accepted but
not yet landed on a turn boundary, for the same diagnostic reason. `archived` is derived, not
separately stored: `archived = (Run.archived_at is not null)` (data-model.md §4) — the boolean is a
projection for operator convenience, not a second source of truth.

The enforcement is structural rather than a rule someone must remember:

1. The response model is a closed schema with no run-record fields — adding one requires changing
   this contract.
2. The endpoint binds `127.0.0.1` only. Deliverable 1 binds the LAN address (its FR-028); the
   operator surface is unreachable from the devices deliverable 1 serves (FR-029's spirit applied
   to the harness).
3. `last_known_good_save` is present on purpose — deliverable 1's FR-027 requires the user to be
   able to see what they need in order to act *here*, so the two surfaces meet at exactly that
   handoff and nowhere else.

## Error responses

| Condition | Result |
|---|---|
| Configuration invalid | Rejected with the failing validation rule named; nothing prepared |
| Preflight mismatch (V2) | Run created in `failed` state with the mismatch recorded; no turn 1 (FR-002) |
| Model chain fails preflight | Rejected before preparation, naming the failing model (FR-039) |
| A run is already active on this client or identity | Rejected; the second run never attaches (FR-006) |
| Store unreachable | Rejected; a run that cannot record must not start (FR-051) |
| Undeclared capability reachable | Rejected, naming the capability (FR-023) |
| Save missing on resume-from or branch | Rejected, reporting the missing save — never resumed from a different turn (FR-036) |
| Game build or platform differs from the seed set's | Run created in `failed` state with the mismatch recorded; no turn 1. Proceeding requires `seedset accept-build` for that exact composite transition first (FR-002, R20) |
| Host platform resolves `unsupported` | Rejected at preflight, naming the missing capability — a host with no verified quicksave path cannot run, since FR-007 forbids a turn without one (R19) |
| Branch target is on a different platform than the parent run | Rejected with the mismatch named; cross-platform save loadability is unproven and Principle IV requires branches to begin from an identical position (FR-034, R20) |
| Disk headroom below `min_free_disk_gb` | Rejected at preflight; an in-flight run halts in a recorded state. Never resolved by deleting a save (R17) |
| `archive` on a non-terminal run | Rejected — archival marks a run no longer a branch source, which is not a statement to make about a run still playing |
| `reap` with no archived runs | Succeeds having deleted nothing. An empty reap is the normal case, not an error |

## Secrets

No command accepts a credential and no response contains one. Provider keys resolve from the
environment or a secrets file at call time. `doctor` reports whether a key is *present*, never its
value (FR-043, SC-018).
