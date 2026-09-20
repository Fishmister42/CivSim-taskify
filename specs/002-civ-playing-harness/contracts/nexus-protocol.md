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
| 4 | `TAG_HANDSHAKE` | `APP:<name>` — identify; `LSQ:` — enumerate available Lua states |
| 3 | `TAG_COMMAND` | `CMD:<state_index>:<lua_code>` — execute Lua in that state |

## Connection sequence

1. Connect to `127.0.0.1:4318`. Connection refused ⇒ the client is not running or the tuner is not
   enabled — a preparation failure, not a run failure.
2. Send `APP:` to identify.
3. Send `LSQ:` to enumerate Lua states.
4. Resolve `GameCore_Tuner` and `InGame` to their state indices.
5. **Record the resolved indices on the run.** Indices are positional and not guaranteed stable
   across game versions or mod sets, so they are run data rather than constants.

## Execution contexts

| Context | Use | Limits |
|---|---|---|
| `GameCore_Tuner` | Read-only state queries | Faster, more stable; cannot issue orders or reach UI-bound data |
| `InGame` | Orders, production changes, and UI-bound surfaces (diplomacy, World Congress) | Slower; the only path for acting |

Every catalog entry declares its context, and the client refuses to execute an entry in the wrong
one — a mismatch is a catalog-load error rather than a mid-run surprise (R3).

## Request/response discipline

The protocol has **no native return values**. Results come back as `print` output, asynchronously,
fragmented across packets. Two rules make that usable:

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
| Command exceeds its timeout | Per-command bound | Treated as unresponsive; contributes to the 60 s detection budget (SC-010) |
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
- The client is the only module permitted to hold a socket to the game.
