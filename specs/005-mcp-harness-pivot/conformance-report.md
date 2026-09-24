# Conformance Report — `civ6-mcp` against the CivSim Constitution

**Date**: 2026-09-24
**Candidate**: `Fishmister42/civ6-mcp`, forked from `lmwilki/civ6-mcp` at upstream commit `dd20190` (v1.1.11), MIT
**Audited on**: this Linux node (native Aspyr build, X11), the only node that can run the client
**Harness**: `audit/minimal_agent.py` in the fork — an agent whose only game-facing capability is the MCP tool surface over stdio. No CivSolver code on its import path (FR-003).
**Evidence**: `specs/005-mcp-harness-pivot/evidence/` — per-call JSONL, tool surface dump, screenshots, driver logs.

---

## 0. What this report is, and what it is not

This is a **partial** audit, and the boundary is stated up front because a conformance report that
lets silence read as a pass is worse than no report. **22 of 76 capabilities were exercised against a
live client. 54 were not.** Every verdict below is labelled with how it was reached: *observed*
(delivered to a model and inspected), *source-verified* (read in the candidate's Lua/Python and
reasoned about), or *not examined*.

The distinction is not pedantry. This project has already shipped a claim that a capability worked
when it had never been exercised, and the correction cost a week. **A capability that was not
exercised is not working; it is unverified.**

---

## 1. Principle I — Human-Parity Information & Action Boundary (NON-NEGOTIABLE)

### 1.1 The headline: fog of war is genuinely respected, and that is the hard part

The largest information surface — map and tile data — is **correctly gated**, verified by reading
`src/civ_mcp/lua/map.py`:

- A tile is emitted only inside `if revealed then`, where `revealed = vis:IsRevealed(plotIdx)` and
  `vis = PlayersVisibility[me]`.
- Tile yields, fresh water, and **foreign units on the tile** are emitted only inside a nested
  `if visible then`, where `visible = vis:IsVisible(plotIdx)`.
- Resources pass an additional `resVisible(resEntry)` check against the local player's techs, so
  strategic resources the player cannot yet identify are withheld.

This is the right shape, and it is the part most likely to have been done wrong by a project with no
obligation to get it right. It was not done wrong. **Source-verified, clean.**

Two further checks came back clean on inspection:

- `get_pending_diplomacy` emits a civilization only when `DiplomacyManager.FindOpenSessionID(me, i) >= 0`
  — i.e. only when the player is literally in a conversation with them. **Source-verified, clean.**
- `get_pantheon_beliefs` filters out pantheons already taken by other players. This is exactly what
  the game's own pantheon picker does, and it discloses *that* a belief is taken, never *who* took
  it. **Source-verified, clean.**

A methodological note, because it changed the result: a first mechanical sweep for "foreign-player
loops without a nearby visibility gate" flagged **20 sites**. Reading them, most were false positives
— the gate was the enclosing block, or the loop only touched `PlayerConfigurations` for a name, or it
ran only after the game was already over. **The sweep was a way to find candidates, not a way to
count violations**, and reporting its raw output as a finding would have been the same error as
counting MCP calls that returned as actions that applied.

### 1.2 Confirmed violations

| # | Capability | Verdict | How reached | Removable? |
|---|---|---|---|---|
| V1 | `run_lua` | **Violation by construction** | Source-verified | Yes, cleanly |
| V2 | `get_diary` | **Violation (code path)** | Source-verified; exercised but returned empty | Yes, or fix the query |
| V3 | `get_diplomacy` | **Violation (delivered)** | **Observed** | Yes, gate on existing handle |
| V4 | `get_trade_destinations` | **Violation (fallback path)** | Source-verified | Yes, add gate |
| V5 | `unit_action` found-city refusal | **Minor violation** | Source-verified | Yes, redact name |
| V6 | settle advisors | **Indirect violation** | Source-verified | Yes, gate the preamble |

**V1 — `run_lua` executes arbitrary Lua in the `GameCore` or `InGame` context.** Its own docstring
advertises `Players[]`, `GameInfo.*`, `Map.*`, `Game.*`. There is no boundary here to respect: any
hidden state the engine holds is one tool call away, and no amount of filtering elsewhere survives
it. This is not a bug in the candidate — it is a deliberate escape hatch that a benchmark harness
reasonably wants and this project's constitution categorically forbids. **It was withheld from the
model in every block of this audit and would have to be deleted, not merely discouraged, before
adoption.** A prompt instruction not to call it is a rule, not a control.

**V2 — `get_diary` runs an explicitly omniscient per-player query.** `build_diary_full_query()` in
`src/civ_mcp/lua/overview.py:722` carries the docstring *"Emits per-player lines (all alive major
civs, omniscient)"* — the candidate's own word. The loop at `overview.py:798` is preceded by the
source comment `# === Player loop (omniscient — all alive major civs) ===` and reads, for every
living major civ with no `HasMet` and no visibility gate: score, city count, total population,
science yield, culture yield, gold balance, gold per turn, and faith. Worse, `overview.py:766`
builds `aliveVis[i] = PlayersVisibility[i]` — **handles to other players' visibility** — and uses
them to compute how much of the map each rival has explored. No human player can obtain that.

  **Stated precisely, because the distinction is the whole point of this report:** `get_diary` was
  called twice during the audit and returned `No diary entries yet for this game` both times, 64
  characters. **The omniscient payload was never delivered.** This is a confirmed code path and an
  unobserved leak. Reporting it as "the agent received omniscient data" would have been false.

**V3 — `get_diplomacy` gates selectively, and the inconsistency is the finding.** This one *was*
delivered to the model. Verbatim from the recorded payload:

```
  Cree (Poundmaker) — FRIENDLY (+9) [player 2]
    Cities (6): Makwa-Sakahikan pop 6 (28,17); ... + 3 in fog
    Military: 274 vs our 609 (0.4x)
    Agenda: [Hidden] — Requires Secret diplomatic visibility (spy or alliance)
```

Read those three lines together. The per-city details **are** correctly gated on
`pVis:IsRevealed`, which is why three cities appear as `+ 3 in fog`. The hidden agenda **is**
correctly gated on diplomatic visibility, with the mechanism named in the output. So the candidate
both understands diplomatic visibility and already holds the handle — `local vis = pDiplo:GetVisibilityOn(i)`
sits four lines above in the same query.

  And yet `MILITARY|` is printed unconditionally on `HasMet`, and the **total** city count `(6)` is
  printed ungated while its constituent details are gated. The player is told exactly how many
  cities a rival has, including ones never seen, and their exact military strength.

  **What I am not claiming:** I have not put the in-client intel panel side by side with this output
  at the same diplomatic visibility level to establish exactly where Civ VI's own line falls. The
  defect that is *certain* is the **inconsistency** — a query that gates one field on visibility and
  not the neighbouring two has no coherent boundary, whatever the correct boundary turns out to be.
  The exact remediation needs that in-client comparison, which this audit did not perform.

**V4 — `get_trade_destinations` has a clean primary path and an ungated fallback.** The primary path
(`economy.py:248`) filters destinations through `UnitManager.CanStartOperation`, the engine's own
legality check — that is the game's trade-route picker and is human-parity. But when it yields
nothing (`found == 0`, a common case: trader at capacity), the fallback at `economy.py:274`
enumerates **every city of every living player**, gated only on `IsAlive()` and not-at-war,
printing name, civilization, coordinates and yields. Cities of civilizations never met are included.
**Not exercised** — no trade-destination call was made in any block.

**V5 — the found-city refusal names cities the player has never seen.** `map.py:382` checks all
cities within 3 tiles and bails with `"Too close to " .. Locale.Lookup(c:GetName())`. A human is
told the site is too close; they are not told the name of a city they have never discovered.
Minor, and trivially fixed by redacting the name when the city is not revealed. **Not exercised.**

**V6 — settle recommendations are shaped by cities the player cannot see.** `_SETTLE_PREAMBLE`
(`map.py:36`) builds the city-distance list from all cities of all living players, ungated. The
advisor's output is therefore *contaminated* by hidden information even though it never prints it:
a recommendation that steers away from an unseen city leaks its existence through the
recommendation. **This is the subtlest of the six and the easiest to miss in review** — the leak is
in the shape of the answer, not in the text of it. **Not exercised.**

### 1.3 Principle I verdict

**FAILS as shipped.** All six violations appear remediable without touching the architecture — one
deletion (`run_lua`), three visibility gates (`get_diary`, `get_trade_destinations`,
`_SETTLE_PREAMBLE`), one field redaction (found-city refusal), and one consistency fix plus an
in-client comparison (`get_diplomacy`). None requires redesigning how the candidate talks to the
game.

The constitution says a change that cannot satisfy Principle I must be **blocked rather than shipped
with a caveat**. It does not say a remediable component must be rejected. That is what makes this an
*adopt-with-remediations*, and the remediations are blocking.

---

## 2. Principle II — Firetuner-First

**PASSES, emphatically.** The candidate is Firetuner-native: the same Nexus wire protocol on TCP
4318, the same `[4-byte LE length][4-byte LE tag][null-terminated payload]` framing, the same
GameCore-reads / InGame-commands split this project reverse-engineered independently. Its only
bespoke path is OCR-driven main-menu navigation, used for exactly the gap this project also
documented — `Network.LoadGame` does not work from Lua on the Aspyr Linux port — and their source
says so in a comment. That is the documented-gap justification Principle II requires, arrived at
independently.

**One correction to their comment, from our evidence:** `src/civ_mcp/game_lifecycle.py:457` reads
*"On the Aspyr Linux port, Network.LoadGame silently does nothing (same as Network.SaveGame)."* The
`LoadGame` half matches our finding. The `SaveGame` half does **not** — `Network.SaveGame` works on
this host and wrote the several hundred `civsim__run-*.Civ6Save` files this audit's own save was
copied from. Worth carrying upstream.

---

## 3. Principle III — Complete Match Telemetry

**NOT SATISFIED as-is; not architecturally blocked.** The candidate has real telemetry
(`telemetry.py`, `diary.py`, a Convex sync, a HuggingFace publisher) but it is aimed at *their*
benchmark pipeline, not at this project's match store. Nothing observed writes turn-by-turn state
to `civsim-match-store.db`.

The constitution's requirement is that every turn is persisted **before the next turn begins**, with
enough state to reconstruct decisions without replaying. Meeting that on top of the candidate means
writing an adapter at the MCP-client boundary — which the audit harness already demonstrates in
miniature: `audit/minimal_agent.py` records every call, its arguments, its outcome and its latency to
JSONL without any cooperation from the server. **Source-verified as feasible; not built.**

---

## 4. Principle VII — Provider-Agnostic Model Access & Resilience

**Provider-agnostic: PASSES at the boundary that matters.** The candidate's own eval stack is wired
to Anthropic/OpenAI/Google SDKs, but that is *their* harness, not the MCP server. The server speaks
stdio JSON-RPC and knows nothing about model providers. This audit's harness drove it entirely
through **OpenRouter**, which is the proof: `anthropic/claude-sonnet-4.6` and
`qwen/qwen3-235b-a22b-2507` both played through the same tool surface with no server-side change.
**Observed.**

**Resilience: FAILS on the evidence of this session, and this is the second-most-serious finding.**
See §5.

---

## 5. The crash

| when | what |
|---|---|
| 14:17:06Z | `send_diplomatic_action(other_player_id=2, action="DECLARE_FRIENDSHIP")` → `ACCEPTED\|Cree accepted your friendship declaration` |
| 14:17:07.6Z | `Civ6 segfault at 8 ip 00007bf44226f8f0 error 4 in libGameCore_XP2.so[7bf440200000+3802000]` |

**1.6 seconds.** The crash wall-clock is derived from the kernel monotonic stamp (666588.58 s)
against boot (2026-09-16 17:07:19), so it does not depend on the harness's own clock.

Symbol resolution by the method this project used for its 2026-09-21 crash — `nm -DC` on the shipped
library, `ip − base = 0x206f8f0`:

```
000000000206f8f0 W GameCore::Diplomacy::Action::Instance::GetType() const
```

**Offset +0x0 — an exact symbol match, not a nearest-preceding approximation.** Fault address `8`,
`error 4` (read), and the faulting instruction in the kernel's `Code:` dump is `8b 47 08` =
`mov eax,[rdi+8]`: `GetType()` called on a **NULL `Action::Instance`**, reading a member at +8 off a
null `this`.

**What this is evidence of, stated carefully.** The attribution is tight — a diplomacy symbol, 1.6 s
after a diplomacy action, at an exact offset — but it is attribution, not proof. A controlled
reproduction (the same call, isolated, on a fresh load) is the missing step and is listed in §7 as
owed. `send_diplomatic_action` was withheld from the model for every subsequent block so that the
turn-count evidence would not be contaminated by it.

**Why it matters beyond one bug:** this is the *second* NULL-read in `libGameCore_XP2.so` this
project has seen from a tuner-driven call. The first was
`Government::GetPrereqCivicReference()` on 2026-09-21, from calling a real engine method from a
context Firaxis never calls it in. Same shape, different subsystem, **different codebase**. That
makes it look like a property of driving this engine over the tuner rather than a defect either team
wrote. **The rule this project adopted then survives the pivot intact: a method that exists is not
safe from every context.** Adopting the candidate does not buy us out of that class of failure; it
inherits it — along with the candidate's own mitigation, an auto-resume path with retries and a
restart-and-load recovery tool, which is more than this project ever had.

---

## 6. Capability exercise ledger

**76 advertised. 22 exercised. 54 not exercised.** Reconciles to 76 (SC-004).

Of the 22 exercised: 19 reached `applied`, 5 produced at least one `engine_refused`
(`unit_action`, `get_game_overview`, `set_policies`, `get_governors`, plus refusals within otherwise
applied tools), 0 transport failures inside a block, 0 MCP-level errors.

**Write tools that actually applied: 5** — `set_research`, `set_policies`, `propose_trade`,
`unit_action`, `send_diplomatic_action`. Against this project's own measured ceiling of **11
distinct actions applied live** after four weeks, **SC-002 is NOT met by this audit.** The honest
reading is that the audit ran out of provider budget, not that the candidate ran out of capability —
but SC-002 asks for a measurement, and the measurement says 5.

The full per-tool tally is `evidence/exercised.json`. The not-exercised 54 include every governor,
religion, great-person, world-congress, espionage, purchase and victory capability.

### The `end_turn` schema defect — why three blocks ended zero turns

Across the first three model-driven blocks, **not one `end_turn` call was made**. It looked like
model dithering. It was not.

`end_turn` requires all five reflection fields (`tactical`, `strategic`, `tooling`, `planning`,
`hypothesis`) to be non-empty **at runtime** — it answers
`Empty reflections: ... Provide non-empty entries for all 5 fields`. But the JSON Schema it
advertises declares every one of them as:

```json
"tactical": { "default": "", "title": "Tactical", "type": "string" }
```

**`"default": ""`, and no `required` array anywhere.** A model reading that schema is being told,
correctly per JSON Schema, that it may call `end_turn` with no arguments — and the refusal that
follows is narrated in the body without `isError`, so it does not even read as a failure.

This is the **same defect class this project named in its own 2026-09-22 audit** — *"an optional
parameter with a safe-looking empty default that every unit test supplies and the one production
call site does not"* — mirrored: optional by schema, required by runtime. It sits on **the single
tool that advances the game**.

**Both halves were tested.**

*The capability is sound.* A direct probe supplying the five fields — no model, no cost — advanced
**8 consecutive turns, 76 → 84**, each verified by reading `Game.GetCurrentGameTurn()` back rather
than trusting the reply. The two non-advances were the first call (game still loading) and a
**World Congress gate**, a legitimate game state the server surfaced correctly. `end_turn` also
**auto-dismissed a blocking pre-turn popup**.

*The schema was the blocker.* Re-declaring those five fields as `required` in the harness's schema
translation — **nothing else changed** — produced model-driven turns immediately: **103 → 104 →
106**, unattended.

### The measurement defect this exposed, which is the audit's most portable finding

**The MCP server does not set `isError` for game-level refusals.** It narrates them in the result
body and returns success. Three consequences, all observed:

1. `set_policies` returned `POLICIES_SET|Policies updated.` **and**
   `WARN:SILENT_FAILURE — engine rejected: slot 0 (wanted POLICY_CONSCRIPTION, got POLICY_RETAINERS)`
   in the same body. Credit where it is due: **they read the state back and named their own silent
   failure**, which is exactly the discipline whose absence cost this project `cities.set_production`
   for weeks. But the caller cannot act on it programmatically.
2. After the client crashed, **twelve consecutive "Cannot connect to Civ 6" replies were recorded as
   successful tool calls** by the first version of this audit's own harness.
3. The first tally therefore read "12 of 12 tools OK" when it included a refusal.

Fixed mid-audit: every call is now classified `applied` / `engine_refused` / `transport_failure` /
`mcp_error` from the narration, and the headline counts **write tools that applied**, not calls that
returned. **Any adoption must classify at this boundary**, or every downstream coverage number
inherits the same inflation this project already shipped once.

---

## 6b. The fresh Cyrus run — and the one tool that kept failing

The owner's mid-session directive was to rebuild on a fresh Cyrus/Persia seed, because the
inherited save (`civsim-gameplay-2026-09-22-end2`) turned out to be a **John Curtin / Australia**
game that autoplay had run to ~turn 100, with four cities, 25 units, five civs met, a religious
emergency and a World Congress in flight — a board on which almost nothing is attributable to the
agent. A new game was built from the host's `CivSim DEFAULT` preset (Gathering Storm / Emperor /
Online / Pangaea Small / 6 AI / **Smart-Timer Off**), with Cyrus pinned in the human slot, and the
agent run from turn 1.

It is a markedly cleaner picture than the inherited save. The agent oriented, called
`get_settle_advisor`, founded Pasargadae, set production and research, explored with the Warrior and
ended turns — legibly, one decision at a time.

**And it surfaced a robustness defect the busy save had hidden: `get_game_overview` failed 2 of its
first 3 calls** with `Error: Empty overview response`, raised by `parse_overview_response` when the
tuner returns nothing (or returns the FireTuner status string `Resolving Buffered Parameters`, which
trips a second parser: `Overview response has 1 fields, expected >=14`). The parsers **raise instead
of retrying**, which is how a transient tuner hiccup becomes a dead call — and this is the one tool
the server's own guidance tells the agent to orient with every turn.

Two things worth saying precisely about that number. First, **those were the only failures in the
run** — every other call applied. Second, **the agent routed around it on its own**, falling back to
`get_units` and `get_cities` and not calling the broken tool again. That is good agent behaviour
covering for a defect, not the defect being harmless: a retry belongs in the server.

A third defect the agent caught before the audit did: `unit_action(found_city, target 11,22)`
returned `FOUNDED|11,23` — **founding at a different tile than the one requested** — and the
following `get_cities` returned `No cities.` while the city in fact existed. The agent's own turn
reflection reads: *"Unit action returned FOUNDED|11,23 — possible coordinate mismatch… get_cities
returned empty after unit_action reported city founded. Possible sync delay or failed founding."*
Both halves need an in-client check before adoption.

## 7. What this audit did NOT examine

Named explicitly so that silence is not read as a pass (FR-022, SC-010):

- **Principles IV, V, VI** — seeded reproducibility and save lineage, the guidebook gate, and unified
  observability were not examined at all. The candidate has a session-replay web app that plausibly
  bears on VI; it was not evaluated.
- **54 of 76 capabilities** were never called. Their human-parity verdicts above are source-verified
  where stated and absent otherwise.
- **A controlled reproduction of the crash.** Attributed, not reproduced.
- **The in-client comparison for V3** — where Civ VI's own intel panel draws the line on military
  strength and city count at a given diplomatic visibility level.
- **Windows and macOS.** Upstream claims both; nothing here verifies either.
- **Long-horizon behaviour.** The longest block was minutes. Upstream reports 300+ turn games; this
  audit observed nothing of the sort and takes no position on it.
- **Capture hygiene of the candidate's own screenshot path.** This audit's screenshots come from its
  own mss-based window capture, not the candidate's.
