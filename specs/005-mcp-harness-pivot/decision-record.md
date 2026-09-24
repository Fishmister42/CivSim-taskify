# Decision Record — Adopt `civ6-mcp`?

**Date**: 2026-09-24
**Feature**: `specs/005-mcp-harness-pivot`
**Candidate**: `Fishmister42/civ6-mcp` ← `lmwilki/civ6-mcp` @ `dd20190` (v1.1.11), MIT
**Evidence**: `conformance-report.md`, `evidence/`
**Decided by**: the Linux node, from this session's measurements

---

## Recommendation: **ADOPT WITH REMEDIATIONS**

The remediations are **blocking**. Six of them. None is architectural.

---

## Why adopt

**The capability gap is not close, and it is not a matter of polish.**

| | CivSolver, measured 2026-09-22 | civ6-mcp, this session |
|---|---|---|
| Agent-callable game capabilities | 41 declared, **11 applied live** | **76 declared, 22 exercised in ~4 min** |
| Capabilities found structurally impossible | **14 of 38** (28 invented accessor names) | none found |
| Time to first working agent | ~4 weeks | **~90 minutes**, including forking and installing |
| Longest game state driven | turn 59, 1 city, 1 warrior | turn 74, **4 cities, 25 units, 5 civs met** |
| Platform tiers | Linux only | Linux exercised here; Windows/macOS claimed |

The decisive fact is not the tool count. It is that **the candidate reached the same substrate
independently and got further along it**: the identical FireTuner Nexus protocol on TCP 4318, the
same framing, the same GameCore-reads / InGame-commands split. Two teams converged on the same
answer; one of them has 76 working tools and a 300-turn benchmark, and it is not us. Continuing to
hand-wrap this interface would be re-deriving, at our own cost, work that already exists under a
licence that lets us take it.

Three further things the audit found that we would otherwise have had to build:

1. **Fog of war is genuinely respected in the largest surface.** Map queries gate tiles on
   `IsRevealed` and yields and foreign units on `IsVisible`. That is the part most likely to have
   been done wrong by a project with no obligation to get it right, and it was done right.
2. **They read state back and name their own silent failures.** `set_policies` returned
   `POLICIES_SET` *and* `WARN:SILENT_FAILURE — engine rejected: slot 0` in one body. That is the
   exact discipline whose absence cost us `cities.set_production` for weeks.
3. **A working answer to a problem we never solved.** `Network.LoadGame` does not work from Lua on
   the Aspyr Linux port — we found that and stopped; they found it, said so in a source comment, and
   built OCR menu navigation around it.

## Why not simply adopt

**Principle I fails as shipped.** Six violations, all cited in the conformance report:

| | What | Blocking remediation |
|---|---|---|
| V1 | `run_lua` — arbitrary Lua, reads anything | **Delete the tool.** Not "discourage" — a prompt instruction is a rule, not a control |
| V2 | `get_diary` — a per-player loop their own source labels `omniscient`, plus other players' `PlayersVisibility` handles | Gate on `HasMet` + visibility, or drop the rival block |
| V3 | `get_diplomacy` — exact military strength and full city count ungated, while agendas *are* gated on diplomatic visibility in the same query | Make the gating consistent; needs one in-client comparison to fix correctly |
| V4 | `get_trade_destinations` fallback — every city of every living player, ungated | Gate the fallback as the primary path already is |
| V5 | found-city refusal names cities never seen | Redact the name when unrevealed |
| V6 | settle advisors' distance list is built from all cities including unseen | Gate `_SETTLE_PREAMBLE` |

**V3 is the one that shows the shape of the problem.** The candidate both understands diplomatic
visibility and already holds the handle four lines above — it prints
`Agenda: [Hidden] — Requires Secret diplomatic visibility` — and then prints exact military strength
with no gate at all. The defect is not ignorance of the boundary; it is that **there is no single
place where the boundary is enforced**, so each query re-decides it and some decide wrong.

**That points at the remediation that actually matters, and it is not any of the six.** Patching six
sites leaves the seventh to be written next month. What the candidate lacks is the thing this
project already built: `src/civsim_harness/parity/` — an enforcement layer with a category-to-
technique mapping and a negative control. **Our parity work is not made redundant by the pivot; it
becomes the most valuable thing we own**, because it is the only part of our stack the candidate has
no equivalent of. Guarantee the property by the absence of an edge, not by six correct patches.

## The crash

`send_diplomatic_action` was followed **1.6 seconds later** by a client segfault whose address
resolves at offset `+0x0` to `GameCore::Diplomacy::Action::Instance::GetType()`, reading a member at
+8 off a null `this`. Attribution is tight; **a controlled reproduction was not performed**, and
this decision does not rest on it being proven.

It does not change the recommendation, for a reason worth stating: this is the **second** NULL-read
in `libGameCore_XP2.so` this project has seen from a tuner-driven call, and the first one was
**ours** (`Government::GetPrereqCivicReference()`, 2026-09-21). Same shape, different subsystem,
different codebase. That makes it look like a property of driving this engine over the tuner rather
than a defect either team wrote — so rejecting the candidate over it would trade a known instance of
a hazard for an unknown instance of the same hazard, in code we would then have to write ourselves.
The candidate at least ships auto-resume with retries and a restart-and-load recovery path, which
we never had.

## What the audit did not establish

Stated plainly, because these gaps bear directly on how much the recommendation is worth:

- **SC-001 was initially NOT met, then root-caused and met in part.** Three blocks ended zero turns.
  The cause was not the models and not the game interface: `end_turn`'s JSON Schema declares its
  five required reflection fields as optional with empty defaults (conformance report, §6). A direct
  probe supplying them advanced **8 consecutive turns (76 → 84)**, each verified by read-back; a
  one-line schema repair in the harness produced **model-driven** turns immediately (103 → 104 →
  106), and a fresh Cyrus/Persia game from turn 1 then ran unattended under model control. **The
  capability is established. The precise "10 consecutive turns by a model, unattended" wording is
  met by the fresh-Cyrus run** — see `evidence/cyrus-run/` for the per-call record.
- **SC-002 (beat 11 applied actions) was NOT met.** Five write tools applied:
  `set_research`, `set_policies`, `propose_trade`, `unit_action`, `send_diplomatic_action`. The audit
  ran out of provider budget and client stability, not out of candidate capability — but the
  measurement says 5, and 5 is what goes in the record.
- **54 of 76 capabilities were never exercised.** Their verdicts are source-verified or absent.
- **Principles IV, V and VI were not examined at all.**
- Windows and macOS: upstream claims, unverified here.

## Conditions that would reverse this

1. A remediation turns out to be architectural rather than local — in particular, if gating
   `get_diary`'s rival block cannot be done without breaking their telemetry pipeline.
2. `end_turn` proves unreliable on this host. This is the first thing the next session must settle;
   everything else is moot if the harness cannot advance a turn.
3. The diplomacy crash reproduces deterministically on a minimal call and has no guard.
4. Upstream abandons the project. It is MIT and forked, so this is a maintenance cost, not a block.

## Stability and cost, for the record

- Two client deaths in the session: one segfault attributed to a diplomacy action; one **tuner wedge
  with the process still alive** — port 4318 stopped accepting while `Civ6` kept running, requiring a
  process recycle. Upstream's README documents exactly this ("the tuner often hangs after a bad
  handshake and won't recover until the process is recycled"), so it is a known hazard of the
  substrate, not a surprise.
- One robustness defect observed: `get_game_overview` raises `ValueError: Empty overview response`
  when the tuner returns nothing, rather than retrying — which is how a transient wedge becomes a
  dead run.
- OpenRouter spend this session: **~$0.10** of the remaining cap (from $3.03 to ~$2.93). The budget
  was already at $2.93 of $360 before this session's blocks; `anthropic/claude-sonnet-4.6` was
  swapped for `qwen/qwen3-235b-a22b-2507` at roughly 1/100th the cost once that was seen. **The
  provider's refusal is the stop, per the standing ruling, and it was very nearly reached.**
