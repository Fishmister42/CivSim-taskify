# First model-driven run attempt — 2026-09-20 (Windows live host)

**Operator:** live-run operator node (Fable 5). **Host:** Windows 11 (10.0.26200), Steam Civ VI, BBG.
**Intended:** the first model-driven run in project history — real OpenRouter calls deciding real
turns against the live client. **Model chain intended:** primary `anthropic/claude-sonnet-5`,
fallback `anthropic/claude-opus-5` (both confirmed live: 1M ctx, image-capable).

## Verdict

**BLOCKED at client bring-up. Zero turns played, zero model calls made, $0 spent.** The Civ VI
client could not be kept alive long enough to reach a loaded game: Steam's own logon instability
shut Steam down within ~7 s of the client's tuner opening, on **both** launch windows, taking the
game process with it each time. This is the documented host condition (r5-save-path-windows.md;
hypervisor-log Run 5: "Steam relaunches every few minutes (LogonFailure, 8x today)"), not a harness
defect. Per the standing hard rule (one relaunch attempt, then stop — do not loop), the operator
stopped after the single relaunch.

## Timeline (all times local, 2026-09-20)

| Time | Event |
|---|---|
| 17:20:47 | Round-1 launch: `CivilizationVI.exe` (DX11, local; steam_appid.txt present). Tuner up 17:20:54, connected (2–3 states, IntroScreen phase). |
| 17:21:01 | **Steam Shutdown** (bootstrap_log.txt) — killed the game; every subsequent tuner refresh failed. Client never left IntroScreen. |
| ~17:25 | Round-1 script gave up ("front end never appeared"). Client + Steam both down. |
| 17:28:37 | Operator started Steam; waited ~4 min; Steam idle ("Nothing to do"), stable window. |
| 17:32:26 | **Single relaunch attempt**: `CivilizationVI.exe` (DX11, local). Tuner up 17:32:33, connected once (2 states). |
| 17:32:40 | **Steam Shutdown again** (bootstrap_log.txt) — 7 s after tuner opened; ConnectionResetError then connection-refused for the rest of the run. |
| ~17:38 | Attempt gave up ("front end never appeared"). Client Lua.log never advanced past `DebugHotloadCache: GameDebug initialized!`. |

Steam `connection_log.txt` shows it flapping between `Logged Off` and a brief `Connected` right up
to each shutdown — i.e. it never held a stable session. `steam_appid.txt` (289070) was present, so
the launches ran the game **locally** (not streamed to the owner's laptop via Remote Play).

## What DID verify (the production wiring reached, live)

- **`civsim doctor`** (before and after, identical): platform ok (windows, tier SUPPORTED),
  store ok, catalog ok (version 2026.09.2, 53 declarations, **0 undeclared**), disk 891–893 GB.
  tuner unreachable / client not running (client dead). `provider key : MISSING` — see Finding A.
- **Provider layer, fully validated live (free GETs, $0):** the OpenRouter key resolves through the
  *production* path (`config.secrets.require_secret`, reads gitignored `secrets.yaml`).
  `OpenRouterProvider.describe()` confirmed `anthropic/claude-sonnet-5` and
  `anthropic/claude-opus-5` both `confirmed=True, accepts_images=True, ctx=1_000_000` — so
  `preflight_chain` (FR-039, worst_case 200k, requires_images) **would pass** for this chain. The
  historic model-preflight blocker is not in play.
- **OpenRouter `GET /auth/key`** → 200. `limit: 80`, `limit_remaining: 80`, `usage: 0`,
  `is_free_tier: false`, expires 2026-09-27. Cap fully intact; nothing spent.

## Where the production path would have gone next (from the code, not run)

`civsim run start` → `Runner.start` → `_prepare_run` (composition.py). Order: pure gates
(seed-set/V3, catalog, host gate, **provider preflight** — all clearable here) → `connect()` →
`refresh_state_indices()` → branch on phase. **The connect step is exactly what cannot succeed
against a dead client**, so preparation stops there with a `PreflightError` (connection refused),
before any `Run` record exists.

## Findings for the hypervisor (discovered by reading the wiring; not all observable live today)

**A. `doctor` reports `provider key: MISSING` when the key is actually resolvable.**
`operator/doctor.py:298` — `_probe_provider_key` does `present=bool(env.get("OPENROUTER_API_KEY"))`,
an **environment-variable-only** check. The real resolution path every run uses
(`config/secrets.py::require_secret`, used by `OpenRouterProvider._resolve_api_key` and the provider
preflight) reads `secrets.yaml` too. Confirmed live this session: doctor says MISSING, yet
`secret_is_present("openrouter_api_key")` → True, `describe()` authenticates, and `GET /auth/key`
→ 200. Fix: have `_probe_provider_key` call `config.secrets.secret_is_present(...)` (or
`resolve_secret`) rather than reading the env var directly, so doctor agrees with the path a run
actually takes. Cosmetic but actively misleading on this host, where the key lives only in
`secrets.yaml`.

**B. There is no verified FireTuner path to START a fresh run — only to load a named save.**
`composition.py::_prepare_connected_run` raises `no_game_loaded` unless the client is already at the
Create Game screen (`HostGame`) or already `InGame`; its own error text (composition.py ~795–808)
states "no verified FireTuner path exists to load a setup preset or start a *new* game." So a fresh
model-driven run is gated on an operator manually bringing the client to a loaded game **before**
`run start` — independent of the Steam problem. For an *unattended* first run this is a structural
gap: the harness can resume/branch from a save (T217) but cannot itself reach turn 1 of a brand-new
game.

**C. V2 setup verification would land a run in `failed` against an arbitrary loaded save** — three
known-wrong/mismatched getters in `run/preparation.py::_SETTING_GETTERS`, corroborated by
`spikes/t218-RESULTS-setting-getters.md` (could not re-observe live — no client survived):
  - `opponents.major_count` → `GameConfiguration.GetAIPlayerCount()` returns **16 in-game**
    (counts city-states) vs the majors a config records (~6). Phase-dependent; a fake returns one
    number for both phases so unit tests pass.
  - `map_settings.map_type` → `MapConfiguration.GetScript()` returns e.g. `"Continents.lua"`, a
    script filename, while a run config carries the `MAPTYPE_CONTINENTS` enum → guaranteed mismatch.
  - `map_seed` getter uses key `"RANDOM_SEED"`; T218 found the value-bearing key is
    `"GAME_SYNC_RANDOM_SEED"` → the code's key likely reads nil.
  A run against a real save would need a config hand-tuned to these (sometimes wrong) read-backs to
  pass V2, which is itself the smell.

## Evidence files (this directory)

- `model-driven-run-2026-09-20.md` — this record.
- `model-driven-bringup-attempt-log.json` — the single relaunch attempt's timestamped log.
- `model-driven-bringup-round1-log.json` — the earlier round-1 attempt (Steam died mid-run).

No harness captures were persisted: no run reached turn 1, and this host's capture path resolves
`NONE` regardless (SUPPORTED tier, no passing R6 hygiene credit).

## Honest label

This run was **model-driven in intent only**. No provider completion was ever issued — the decision
loop never ran because no live game was reachable. Do not represent this as the agent having played.
The provider layer was proven live (auth + capability preflight); the game client was not.
