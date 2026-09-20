# Contract: Firaxis Nexus (FireTuner) Protocol

**Feature**: `002-civ-playing-harness` | **Schema version**: 1
**Transport**: TCP `127.0.0.1:4318`

Firaxis does not document this protocol. It is written down here because it is the boundary through
which every observation and every action passes — if it lives only in code, the Principle I audit
has to trace control flow instead of reading a specification. Derived from the reverse-engineering
recorded in [research.md](../research.md) R2–R3.

## Enabling the interface

The tuner interface must be enabled in the game's configuration. After enabling and restarting, the
client listens on TCP 4318.

| Platform | How |
|---|---|
| Windows | In-game **Options → Tuner (disables achievements)** |
| macOS | `EnableTuner 1` in `~/Library/Application Support/Sid Meier's Civilization VI/Firaxis Games/Sid Meier's Civilization VI/AppOptions.txt` |
| Linux | `EnableTuner 1` in `~/.local/share/aspyr-media/Sid Meier's Civilization VI/AppOptions.txt` |

**The Windows-only SDK is not a prerequisite.** The Development Tools ship the FireTuner *GUI*, and
that GUI does not run on macOS or Linux — but this protocol is spoken by the game client itself,
which exposes it on all three native builds. The harness talks to the client directly and never
opens the GUI, so the transport described here is platform-independent (research R1).

**Proton/Wine is the exception**: the interface is built into the native binary and is not exposed
when the Windows build runs under a compatibility layer, so a Linux host needs the native Aspyr
port.

**The game accepts one tuner connection at a time.** This is a protocol-level fact with a
requirements-level consequence: a second harness instance cannot interleave actions into the same
game, which is the primary enforcement of FR-006.

## Wire format

Every message is a header followed by a null-terminated UTF-8 payload.

```text
┌────────────────┬────────────────┬──────────────────────────────┐
│ length uint32  │ tag  int32     │ payload UTF-8, NUL-terminated │
│ little-endian  │ little-endian  │ length bytes incl. terminator │
└────────────────┴────────────────┴──────────────────────────────┘
```

| Tag | Name | Payload |
|---|---|---|
| 4 | `TAG_HANDSHAKE` | Sent: `APP:<name>` — identify; `LSQ:` — enumerate available Lua states. Received: the reply to either (see "Reply framing" below) |
| 3 | `TAG_COMMAND` | Sent: `CMD:<state_index>:<lua_code>` — execute Lua in that state. Received: an **empty** acknowledgement frame — a command's output never arrives on this tag |
| −1 | `TAG_ASYNC_OUTPUT` | Received only: one frame per printed/logged line, payload `O\0<LuaStateName>: <line>`. Carries **all** print output, including every command's sentinel-bracketed result, and arrives unsolicited at any point in the stream |

## Reply framing — verified against a live client (2026-09-20)

None of this section was documented before the first live run of the harness's own transport
(Windows client 1.0.12.68, Steam, BBG 7.5.0 — `spikes/r5-save-path-windows.md`, "The three protocol
defects"). The pre-live implementation guessed all three of the following wrong, and the guesses
were mutually reinforced by a fake that spoke the same guesses. Every claim below cites the raw
capture it was read from.

1. **`APP:<name>` is answered.** The client replies with one `TAG_HANDSHAKE` frame identifying
   itself: `"Civ6\0Sid Meier's Civilization 6\0<binary directory>"` — three NUL-separated fields
   (`spikes/r5-raw-windows/raw_protocol_transcript.txt`, "after APP:"). That reply **must be read
   and consumed before `LSQ:`'s reply is parsed**: its odd field count fails the state-list parser,
   so a client that treats "the next frame" as the `LSQ:` reply cannot complete a handshake at all.
   The harness treats the payload as opaque (logged, nothing parsed from it). The Linux raw probe
   drains and discards frames after `APP:` before sending `LSQ:` (`spikes/r5-raw/nexus_probe.py`,
   `handshake()`), consistent with this framing.
2. **The `LSQ:` reply is the next `TAG_HANDSHAKE` frame — not the next frame.** Unsolicited
   `TAG_ASYNC_OUTPUT` (tag −1) log frames interleave into the same stream at any point, including
   between `LSQ:` and its reply — observed live: `O\0StagingRoom: RefreshStatus()\t...\tfalse`
   arriving with the front-end state list (`raw_protocol_transcript.txt`, "after LSQ:"), and
   `O\0PausePanel: CheckPausedState()...` frames mid-session
   (`spikes/r5-raw-windows/demo_live_results.json`). Interleaved frames are routed to telemetry and
   skipped, never a protocol error.
3. **A command's printed output arrives on tag −1, one frame per printed line, each prefixed
   `O\0<LuaStateName>: `; the tag-3 reply is an empty acknowledgement.** Measured directly
   (`spikes/r5-raw-windows/raw_command_transcript.txt`):

   ```text
   tag=-1 payload='O\x00InGame: ---BEGIN:57a06e82...---'
   tag=-1 payload='O\x00InGame: {"probe":"tag-hunt","lua_version":"2013.2.0 r13768"}'
   tag=-1 payload='O\x00InGame: ---END:57a06e82...---'
   tag=3  payload=''
   ```

   The prefix is stripped per frame before sentinel correlation. The Linux spikes strip the
   identical prefix (`spikes/r5-raw/t077_enumerate.py`, `t077_probe2.py`, `t077_savetest.py`:
   `^O\x00[A-Za-z_0-9]+:\s?`), so **Windows and Linux agree on this framing**. The empty tag-3
   acknowledgement is **verified universal across builds**: the Linux peer re-ran
   `raw_command_probe.py` against `1.0.12.9` (2026-09-20, issue #1) — tag 3 literally empty,
   sentinels on tag −1 with the same prefix, matching Windows `1.0.12.68` exactly. In the peer's
   words: T234 is not a Windows workaround, it is the protocol. A non-empty tag-3
   payload has never been observed from a real client; the harness logs one loudly and routes it to
   telemetry rather than accepting it as a result, so a framing change cannot pass silently.

**Consequence for timeout semantics:** a command timeout means *the answer was not read*, not *the
Lua did not run*. Live proof: a `Network.LoadGame` issued through the pre-fix client "timed out" —
and loaded the game (`r5-save-path-windows.md`, defect 3). Callers must never infer from a timeout
that the command had no effect.

## Connection sequence

1. Connect to `127.0.0.1:4318`. Connection refused ⇒ the client is not running or the tuner is not
   enabled — a preparation failure, not a run failure.
2. Send `APP:` to identify; read and consume its identification reply (see "Reply framing" #1).
3. Send `LSQ:` to enumerate Lua states; the reply is the next `TAG_HANDSHAKE` frame, with any
   interleaved tag −1 frames routed to telemetry (see "Reply framing" #2).
4. Resolve `GameCore_Tuner` and `InGame` to their state indices.
5. **Record the resolved indices on the run.** Indices are positional and not guaranteed stable
   across game versions or mod sets, so they are run data rather than constants.

## Lua state indices are re-resolved on reconnect **and** on every phase transition

Indices must be re-resolved on every reconnect (already covered above) — but a live capture against
a real client (2026-09-20) established that they must **also** be re-resolved on a **game phase
transition within a single connection and a single client process**. The state table is not a fixed
property of "this client version" or "this connection"; it is scoped to whatever screen/phase the
client is currently in.

Evidence, captured from the same running client without a reconnect between the two columns:

| | total states | `LoadGameMenu` | `SaveGameMenu` |
|---|---|---|---|
| Create Game screen | 31 | 18 | 19 |
| In game | 136 | 112 | 113 |

The Create Game (setup) screen exposes an entirely different state table — built from `HostGame`,
`MainMenu`, `StagingRoom`, `Lobby`, and `Mods` — none of which exist once a game is loaded. Neither
table contains `GameCore_Tuner`/`InGame` (consistent with the main-menu finding above), so a
connection sequence performed at the Create Game screen still resolves successfully; it simply has
no game-play states yet, same as at the bare main menu.

The dangerous case is not the missing states — it's the states that exist in **both** tables under
the **same name** at **different indices**: `LoadGameMenu` and `SaveGameMenu` above. An index
resolved for `LoadGameMenu` while at the Create Game screen (18) is not merely stale once the game
is running — 18 is frequently still a *valid* index in the 136-state in-game table, just for some
unrelated state. Executing against a stale index in that case raises nothing: the wrong Lua runs,
silently. This is the same silent-corruption class the `LSQ:` payload's wire-format defect was
(guessing structure instead of reading ground truth) — the general lesson is the same: never assume
continuity of something positional across a boundary that hasn't been proven stable.

**Consequence:** a caller must re-resolve indices at every point the run sequence knows a phase
transition may have occurred (a game finishes loading, the run returns to a menu, a save/load
submenu is entered or left, etc.) — not only immediately after connecting, and not only once per
run. `NexusClient.refresh_state_indices()` is the explicit re-resolution step for this; a cached
index from a prior phase must never be reused without going through it (or
`resolve_game_states()`) again.

## Execution contexts

| Context | Use | Limits |
|---|---|---|
| `GameCore_Tuner` | Read-only state queries | Faster, more stable; cannot issue orders or reach UI-bound data |
| `InGame` | Orders, production changes, and UI-bound surfaces (diplomacy, World Congress) | Slower; the only path for acting |

Every catalog entry declares its context, and the client refuses to execute an entry in the wrong
one — a mismatch is a catalog-load error rather than a mid-run surprise (R3).

## Request/response discipline

The protocol has **no native return values**. Results come back as `print` output, asynchronously —
delivered as `TAG_ASYNC_OUTPUT` (tag −1) frames, one per printed line, each carrying the
`O\0<LuaStateName>: ` prefix, while the tag-3 reply is an empty acknowledgement (see "Reply
framing" #3). Two rules make that usable:

### 1. Correlated sentinels

Each command is wrapped with a per-request nonce:

```lua
print("---BEGIN:" .. nonce .. "---")
-- declared Lua body
print("---END:" .. nonce .. "---")
```

The client collects lines between a matched `BEGIN`/`END` pair. Anything outside a matched pair —
stray game prints, a previous request's tail, engine warnings — is routed to out-of-game telemetry
and never parsed as a result. A fixed sentinel cannot make that distinction, which is why this
deviates from the prior art.

### 2. Structured payloads

The Lua body emits a single JSON document between the sentinels. Serialization is the Lua side's
job, so the Python side never parses prose, and a capability's output validates against the
`output_schema` on its declaration.

## Timeouts, health, and failure

| Condition | Detection | Handling |
|---|---|---|
| Connection refused at startup | Immediate | Preparation fails; the run does not start |
| Connection dropped mid-run | Socket error | `crash_detected` or `hang_detected` run event; recovery path (FR-044, FR-045) |
| Command exceeds its timeout | Per-command bound | Treated as unresponsive; contributes to the 60 s detection budget (SC-010). A timeout does **not** mean the Lua did not run — see "Reply framing", consequence note |
| Heartbeat nonce fails to round-trip | Periodic `GameCore_Tuner` probe | `hang_detected` — catches a game that is alive but stuck (R12) |
| Output arrives with no matching nonce | Sentinel mismatch | Discarded to telemetry; never returned as a result |

## Security and parity constraints

- **Loopback only.** The harness connects to `127.0.0.1`; there is no remote-tuner configuration.
- **No raw-Lua path into the agent's context.** The client executes declared entries from `lua/`.
  Arbitrary Lua execution exists in the diagnostic tooling only, is never reachable from context
  assembly or action dispatch, and its output cannot enter a record as an observation (FR-018).
- **The tuner window is not the interface.** The harness speaks the protocol directly and does not
  require the FireTuner GUI to be open. This matters for image hygiene: the less that window is on
  screen, the smaller the surface FR-025 and FR-030 have to defend (R6, R7).

## Implementation notes

- One `NexusClient` per run, owning the socket and serializing access with a lock — concurrent
  commands on one socket would interleave output across nonces.
- Reconnection re-runs the full handshake and re-resolves state indices; indices from before a
  disconnect are not reused.
- Indices are also re-resolved on every phase transition within a single connection (see "Lua state
  indices are re-resolved on reconnect and on every phase transition" above) — `connect()` resolves
  whatever exists at that moment, `resolve_game_states()` re-resolves and additionally requires
  `GameCore_Tuner`/`InGame`, and `refresh_state_indices()` re-resolves unconditionally for any other
  known phase boundary. `execute_command()` refuses to send a command against a `state_index` no
  longer present in the current table, as a cheap backstop against the case where a stale index has
  simply gone missing — it cannot catch a stale index that happens to still be valid for a different
  state in the new table, which is why re-resolving at known phase boundaries is the actual fix.
- The client is the only module permitted to hold a socket to the game.
