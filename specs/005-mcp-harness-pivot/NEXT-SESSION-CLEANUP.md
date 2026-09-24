# Next-Session Context — Cleaning CivSolver Down to the MCP Pivot

**Written**: 2026-09-24, end of the spec-005 audit session
**Gate**: the audit's decision is **ADOPT WITH REMEDIATIONS** (`decision-record.md`). That satisfies
FR-026, so this file exists. **Read `decision-record.md` and `conformance-report.md` first** — they
are short, and everything below assumes their findings.

**This file is self-contained.** You should not need to re-run the audit, re-read the upstream
source, or reconstruct any measurement to execute the plan below.

---

## STOP — do not start deleting

Two things must be true before a single file is removed, and neither is true as of this writing:

1. **The six Principle I remediations are landed and verified** (`decision-record.md`, "Why not
   simply adopt"). The constitution says a change that cannot satisfy Principle I is blocked, not
   caveated. Deleting our own harness before the replacement is admissible would leave the project
   with nothing admissible at all.
2. **A fresh Cyrus/Persia run from turn 1 has been observed playing.** This is the owner's explicit
   mid-session directive, and it is the acceptance evidence: *"Once we can consistently play through
   a turn, we should retouch on a fresh Cyrus seed based on the configuration saved to this host. I
   want to better understand what the civ agent is doing and hard to tell in existing saved game."*

Until both hold, this is a plan, not a work order.

---

## 1. Where things are

| | |
|---|---|
| Fork | `Fishmister42/civ6-mcp`, cloned at `/home/matt/civ6-mcp`, `upstream` remote set |
| Audited revision | `dd20190` (v1.1.11), MIT |
| Audit harness | `/home/matt/civ6-mcp/audit/` — `minimal_agent.py`, `end_turn_probe.py`, `shot.py`, `load_save.py` |
| Evidence | `specs/005-mcp-harness-pivot/evidence/` |
| Our repo | `/home/matt/CivSolver`, branch `live/linux` |

**The audit harness is uncommitted.** Commit it to the fork before anything else — it is the only
thing that reproduces the findings.

## 2. How to run the replacement on this host

```bash
cd /home/matt/civ6-mcp
uv sync --extra launcher-linux          # needs tesseract-ocr + xdotool (both installed 2026-09-24)

# a play block
uv run python audit/minimal_agent.py \
    --turns 10 --calls-per-turn 6 \
    --model qwen/qwen3-235b-a22b-2507 \
    --ban send_diplomatic_action \
    --out <abs out dir>

# turn advancement with no model and no cost
uv run python audit/end_turn_probe.py <abs out dir> 10
```

Facts you would otherwise rediscover:

- **`end_turn` needs all five reflection fields non-empty**, and its JSON Schema wrongly declares
  them optional with `"default": ""` and no `required` array. `minimal_agent.py` patches this in its
  schema translation. **Without that patch a model will not end turns** — three blocks proved it.
- **`send_diplomatic_action` is banned by default** pending the crash reproduction (§5).
- **The tuner accepts one connection at a time.** A leftover agent process holds it and the next run
  reports "Cannot connect" while the client is perfectly healthy. Kill by `ps -eo pid,comm,args`
  filtered on `comm` — **never `pgrep -f`**, which matches your own shell command and, on 2026-09-24,
  killed the shell that ran it.
- **The tuner can wedge with `Civ6` still alive.** Port 4318 stops accepting; only a process recycle
  clears it. Upstream's README documents this.
- **OCR menu loading is flaky here — 1 of 3.** It fails when save names truncate in the list UI, and
  when the OCR bounding box for a menu item comes back wide and the click lands on its edge. Driving
  the menu directly with `xdotool` at known coordinates is more reliable. Working coordinates at
  1920×1200, window at 0,0: Single Player `(863,488)`, Load Game `(1077,660)`, first save in list
  `(628,298)`, Load Game button `(640,1149)`, Continue Game splash `(573,1131)`.
- **Screenshot with `audit/shot.py`** (mss, window-scoped). **Never ImageMagick `import`** — it takes
  an X server grab and froze this desktop for 80 minutes once.
- **OpenRouter had ~$2.93 of $360 left** at the end of this session. Use `qwen/qwen3-235b-a22b-2507`
  (~1/100th of Sonnet's cost) for anything where play quality is not the evidence.

## 3. Disposition of our own code

Sizes are current measured Python/Lua line counts.

### REMOVE — superseded, and the replacement is demonstrably better

| Area | Lines | Replaced by | Why |
|---|---|---|---|
| `src/civsim_harness/nexus/` | 1,446 | `civ_mcp/tuner_client.py` | Same Nexus protocol, independently derived. Theirs is exercised harder. |
| `src/civsim_harness/act/` | 3,285 | 76 MCP tools | Ours: 41 declared, **11 applied live, 14 structurally impossible**. |
| `src/civsim_harness/observe/` | 1,545 | MCP read tools | Ours had **10 permanently-empty observation fields**. |
| `lua/` | 7,663 | `civ_mcp/lua/` | **28 of our accessor names exist nowhere in Civ VI** (audit `a606729`). |
| `src/civsim_harness/capability/` | 1,386 | the MCP tool surface | The catalog exists to describe our executors. |

**≈ 15,300 lines.** Do not delete `lua/ACCESSORS.txt` or its CI test — see KEEP.

### KEEP — not replaced by anything upstream has

| Area | Lines | Why it survives |
|---|---|---|
| `src/civsim_harness/parity/` | 2,376 | **The most valuable thing we own.** The candidate has no enforcement layer, and the audit found **six** Principle I violations because each of its queries re-decides the boundary. This is what stops the seventh. |
| `src/civsim_harness/store/` | 6,532 | Spec 003, Principle III. Their telemetry goes to their Convex/HuggingFace pipeline, not our match record. |
| `src/civsim_web/` | 9,214 | Spec 001, Principle VI. Their replay app is theirs and is not our unified view. |
| `src/civsim_harness/provider/` | 3,673 | Principle VII. The audit drove the whole thing through our OpenRouter path. |
| `src/civsim_harness/resilience/` | 1,236 | Spec 004. Two client deaths this session; recovery is more relevant after the pivot, not less. |
| `lua/ACCESSORS.txt` + its CI test | — | The allowlist that caught 28 invented names. Re-point it at the candidate's Lua. |
| `specs/002-.../spikes/` | — | **Evidence, never code.** See §4. |

### RE-POINT — keep the logic, change what it drives

| Area | Lines | Change |
|---|---|---|
| `src/civsim_harness/run/` | 9,936 | The decision loop drives MCP tools instead of our executors. **Largest single piece of work.** Keep run lifecycle, locking, orphan detection, stop conditions. |
| `src/civsim_harness/models/` | 2,033 | Map our domain models onto MCP result shapes. |
| `src/civsim_harness/agent/` | 638 | Tool schemas come from `list_tools()`. **Apply the `end_turn` `required` repair here.** |
| `src/civsim_harness/host/` | 3,201 | Keep our **capture** path (mss, window-scoped, no X grab — the audit found it structurally safer than theirs). Drop input/window driving in favour of their launcher. |
| `src/civsim_harness/saves/` | 2,649 | Keep our `Network.SaveGame` path — **it works on this host and their source comment wrongly says it does not**. Drop our load path for theirs. |
| `src/civsim_harness/operator/` | 3,906 | Keep the CLI and doctor; re-point the checks. |
| `src/civsim_harness/telemetry/` | 281 | Bridge MCP call records into the store. |

**A classification rule worth carrying:** everything in REMOVE is something the candidate does
better; everything in KEEP is something the candidate does not do at all. Nothing is removed merely
because it is ours.

## 4. What must survive the deletion

Delete none of this, and do not let it become unreachable when the code it describes goes:

- **`specs/002-civ-playing-harness/spikes/`** — the Linux host knowledge. Steam dependency, tuner
  enablement, the `import` X-grab hazard, `pgrep -f` false positives, the segfault analyses, the
  `CivSim DEFAULT` preset, the LuaEvents auto-vivification trap. **This is why the audit took hours
  instead of days**, and none of it is in the candidate.
- **`civsim-match-store.db`** — 43 runs, the coverage numbers, the per-run spend ledger. The
  baselines this pivot is measured against live here.
- **The frame retro-audit** (`spikes/frame-retro-audit-2026-09-22.md`) — 301 captures, 0
  contaminated, 100% examined. A discharged obligation; do not make it owed again.
- **The measured baselines**: 11 of 41 actions applied live, 14 of 38 structurally impossible, 28
  invented accessor names, 287 of 619 steps with images. **Every claim in the decision record rests
  on these.**
- **The two segfault analyses** — ours (`Government::GetPrereqCivicReference`) and the candidate's
  (`Diplomacy::Action::Instance::GetType`). Together they are the evidence that this is a property of
  driving the engine over the tuner, not a defect either team wrote.
- **`.specify/memory/`** — constitution, loop state, hypervisor log.

## 5. Owed work, in order

1. **Fresh Cyrus/Persia run from turn 1** (owner's directive). The `CivSim DEFAULT` preset at
   `Saves/Single/CivSim DEFAULT.Civ6Cfg` gives Gathering Storm / Emperor / Online / Pangaea / Small /
   6 AI / no turn limit / **no turn timer** — but **does not pin a leader**. Pin it at the `HostGame`
   state and read it back:
   `PlayerConfigurations[0]:SetLeaderTypeName("LEADER_CYRUS")` /
   `:SetCivilizationTypeName("CIVILIZATION_PERSIA")`.
   **Never use Play Now** — it randomises the leader and sets `TURNTIMER_STANDARD`, which silently
   destroys turn verification.
2. **The six Principle I remediations**, blocking. Delete `run_lua`; gate `get_diary`,
   `get_trade_destinations` and `_SETTLE_PREAMBLE`; redact the found-city refusal; make
   `get_diplomacy` consistent.
3. **The in-client comparison for `get_diplomacy`** — where Civ VI's own intel panel draws the line
   on military strength and city count at a given diplomatic visibility level. The remediation cannot
   be written correctly without it.
4. **Reproduce the diplomacy crash** — `send_diplomatic_action` / `DECLARE_FRIENDSHIP`, isolated, on a
   fresh load, under crash watch. Attributed at +0x0, not reproduced.
5. **Exercise the other 54 capabilities**, or state per capability that they remain unverified.
6. **Examine Principles IV, V and VI**, untouched by this audit.
7. **Do NOT upstream anything.** Owner decision 2026-09-24: *"we should detach from them for now
   and keep our fixes to ourselves til we have a more substantive project."* The two corrections
   (`Network.SaveGame` works on Aspyr Linux; `end_turn`'s schema needs a `required` array) are
   written up at `evidence/remediations/upstream-report-draft.md` for whenever that changes. Keep
   the `upstream` remote for pulling and diffing — reading is not contributing.

## 6. Disposition of the existing specs

| Spec | Disposition |
|---|---|
| **001 — unified web interface** | **KEEP, unchanged.** Principle VI. Its data source becomes the store fed from MCP records. 75/75 converged. |
| **002 — civ playing harness** | **SUPERSEDED IN IMPLEMENTATION, RETAINED AS REQUIREMENTS.** Its FRs are still what the project wants; the candidate becomes how they are met. **Do not delete it** — FR-025/FR-030 and SC-009/SC-019 are the Principle I criteria the conformance report measures against, and its spikes are the host knowledge. Re-scope rather than retire. |
| **003 — match tracking store** | **KEEP, unchanged.** Principle III. 46/46 converged. Needs a new writer, not a new design. |
| **004 — liveness/reconciliation/recovery** | **KEEP, re-pointed.** Two client deaths this session. Recovery now also covers the tuner wedge, which our spec did not anticipate. |
| **005 — this spec** | **CLOSES** when the decision record is accepted. |
| Deliverables 4 and 5 (optimization layer, `GUIDEBOOK.md`) | **Still unwritten.** Principle V gates deliverable 4 on `GUIDEBOOK.md` existing. Unaffected by the pivot. |

## 7. The one thing most likely to go wrong

The temptation will be to delete the ~15,300 REMOVE lines first, because it is the satisfying part
and the diff looks like progress. **Do the remediations first.** If you delete our action layer and
then discover that gating `get_diary` breaks the candidate's telemetry in a way that cannot be
worked around, the project has no admissible harness and no way back except `git revert` across a
very large deletion.

The audit's own finding applies to the cleanup itself: **remove the dependency rather than strengthen
the thing it depends on.** Land the remediations, prove a Cyrus run from turn 1, and only then delete
— at which point the deletion is a formality rather than a bet.
