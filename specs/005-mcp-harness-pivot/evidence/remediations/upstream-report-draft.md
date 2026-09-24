# V4 — upstream corrections for `lmwilki/civ6-mcp` (DRAFT, not yet posted)

**Status**: prepared 2026-09-24, **awaiting the owner's go-ahead before posting**. Opening issues on
a third party's public repository under his GitHub identity is outward-facing and not easily undone,
so the work is done and the posting is his call.

Two findings from the spec-005 audit, both reproducible, both small and concrete. Suggested as two
separate issues so they can be triaged independently.

---

## Issue 1 — `end_turn`'s schema says its required fields are optional

**Impact: high.** This is the tool that advances the game, and the mismatch makes it unreachable for
a model that trusts the schema.

`end_turn` rejects the call at runtime unless all five reflection fields are non-empty:

```
Empty reflections: tactical, strategic, tooling, planning, hypothesis.
Provide non-empty entries for all 5 fields.
```

But the JSON Schema advertised over MCP declares every one of them optional, with an empty default
and **no `required` array anywhere**:

```json
"tactical": { "default": "", "title": "Tactical", "type": "string" }
```

A model reading that schema is being told — correctly, per JSON Schema — that it may call `end_turn`
with no arguments. It does, the runtime refuses, and because the refusal is narrated in the result
body **without `isError` being set**, the refusal does not even read as a failure.

**Observed**: across three model-driven blocks (Claude Sonnet 4.6 and Qwen3-235B, via OpenRouter),
**not one `end_turn` call succeeded**. It looked like the models were dithering. Re-declaring the
five fields as `required` in our client's schema translation — changing nothing else, not the server,
not the prompt — produced turns immediately: turn 103 → 104 → 106, then a fresh game unattended from
turn 1 to turn 57 and counting.

A direct probe supplying the fields by hand confirmed the capability was never the problem: 8
consecutive turns, 76 → 84, each verified by reading `Game.GetCurrentGameTurn()` back rather than
trusting the reply.

**Suggested fix**: add `"required": ["tactical", "strategic", "tooling", "planning", "hypothesis"]`
to the tool's input schema and drop the `"default": ""` entries, so the advertised contract matches
the enforced one.

**Worth considering alongside it**: setting `isError` on refusals generally. Game-level rejections
currently arrive as successful tool calls with the reason in the body, which means a caller counting
"calls that returned" over-counts actions that applied. Our own first tally read *12 of 12 tools OK*
while including a refusal, and later counted twelve consecutive `Cannot connect to Civ 6` replies as
successes after the client had crashed.

---

## Issue 2 — `Network.SaveGame` does work on the Aspyr Linux port

**Impact: low, but it is load-bearing for anyone building save/restore on Linux.**

`src/civ_mcp/game_lifecycle.py` (around line 457) reads:

```python
# On the Aspyr Linux port, Network.LoadGame silently does nothing
# (same as Network.SaveGame). Skip Lua tier and go straight to OCR
# menu navigation which actually works.
```

**The `Network.LoadGame` half matches our own independent finding exactly** — we hit it separately
and reached the same conclusion, that only the UI path loads on this port.

**The `Network.SaveGame` half does not hold on this host.** Saving through Lua works:

```lua
local f = {}
f.Name = "KEEPER"
f.Location = SaveLocations.LOCAL_STORAGE
f.Type = SaveTypes.SINGLE_PLAYER
f.IsAutosave = false
f.IsQuicksave = false
local ok = Network.SaveGame(f)
```

This is the checkpoint path our supervisor uses between play blocks. It has written
`Saves/Single/KEEPER.Civ6Save` repeatedly today, with the file's mtime and size advancing each time
as the game progressed (e.g. 1,423,515 bytes at turn 52 → 1,439,927 at turn 54), and the resulting
saves load correctly through the UI.

**Host**: native Aspyr Linux build, Steam app 289070, `1.0.12.9 (564030)`, X11/Cinnamon, Gathering
Storm, tuner enabled via `EnableTuner 1` in `AppOptions.txt`. 22 mods active including BBG.

**Suggested fix**: narrow the comment to `Network.LoadGame` only. The distinction matters because a
reader planning Linux save/restore will otherwise build OCR navigation for the save direction too,
which is slower and much more fragile than the Lua call that already works.

---

## What we are not claiming

- We have not tested either finding on Windows or macOS.
- The `Network.SaveGame` result is from one Linux host and one build; it contradicts the comment
  here but we cannot speak to the configuration that produced the original observation.
- The `end_turn` schema finding is independent of platform — it is in the advertised tool contract.

## Attribution note for the post

The audit is at `Fishmister42/CivSim-taskify` (spec `005-mcp-harness-pivot`), with the harness that
produced these findings on the `civsim-audit` branch of our fork. Worth saying plainly in the issue
that the project is good and the audit concluded **adopt** — these are two small corrections from
someone who took the thing seriously, not a list of complaints.
