"""Record a run driven by the LANDED harness against a live Civilization VI client.

The run goes through the production composition root -- `build_runner_dependencies` ->
`Runner.start(config)` -- so what is exercised is the shipped code: `NexusClient`,
`LinuxHostPlatform` (`capture_window`, `focus_window`, `send_input`), the capability catalog and
its Lua, `CapabilityExecutor`, the decision loop, the preflights (V2/V3/V10/V11, turn timer, host
gate), quicksaves and `SqliteMatchStore` persistence. Every frame in the recording comes from the
production `capture_window()` path, so the artifact is window-scoped by construction.

    python3 -m tests.live.demo_landed_run OUT_DIR --provider fake --turns 3 --bring-up
    python3 -m tests.live.demo_landed_run OUT_DIR --provider openrouter --turns 3
    python3 -m tests.live.demo_landed_run OUT_DIR --provider stochastic --provider-seed 7 \
        --provider-policy coverage          # RECOMMENDED for a zero-cost coverage block

**What is NOT production, stated plainly (ruling: demos record landed code only):**

- `--bring-up` is an OPERATOR step this script performs because the harness has no
  cold-client-to-turn-1 path yet (T237): it brings the client to a fresh `CivSim DEFAULT` game with
  a human slot and Cyrus/Persia pinned, and dismisses the leader-intro screen. It uses the
  production port operations (`focus_window`, `send_input`) but the sequence itself is scripted
  here, not in `src/`.
- The run configuration is WRITTEN BY THIS SCRIPT from the live client's own V2 read-back (the
  production `LuaGameSetupReader`), so that `map_seed`/`opponents.major_count` -- values the game
  chooses -- match. Every charter value (Persia, Cyrus, Gathering Storm, Emperor, Online speed,
  Ancient era, Small map) is asserted against the read-back first and the demo refuses on a
  mismatch, so the configuration is the charter confirmed live, not "whatever the client said".
- `--provider fake`: every decision is scripted ("end the turn") by `FakeModelProvider`.
  **ZERO model calls.** This is a harness demo, not an agent playing Civilization.
- `--provider openrouter`: the production `OpenRouterProvider` with the real key; real model
  calls; spend is recorded in the store's `model_calls` table and in `results.json`.
- `--provider stochastic`: the production `StochasticModelProvider` (`provider/stochastic.py`,
  resolved through the composition root's own `build_provider`). **ZERO model calls, $0.** Every
  decision is sampled from the actions the decision request itself lists, with a target taken
  from what that same request shows -- so this is the harness's *action surface* being exercised,
  still not an agent playing Civilization. `--provider-seed` fixes the stream (distinct from
  `--seed`, which is the map seed the bring-up types into the client).
- `--provider-policy` picks how that sampler chooses, and `coverage` is the RECOMMENDED setting
  for a coverage block (T262): it draws only from the actions the request shows in its
  "available now" group -- the owner's rule, verbatim, "the stochastic testing should only
  select from actions of a reachable state" -- preferring what the run has not landed yet, and
  it never falls back to a greyed-out action even when nothing is available (it ends the turn,
  recorded). `uniform` is the default and is left exactly as it was, so the two can be compared:
  under it, MEASURED 2026-09-21, nine of the fourteen catalog actions that had been attempted
  but never applied were draws for a situation that was not on screen. The policy is reported in
  the store as `stochastic/uniform-v1` or `stochastic/coverage-v1`.

Everything the script asserts is read back from the far side (the game, the store), never
inferred from the recording.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tests"))

import yaml  # noqa: E402
from PIL import Image  # noqa: E402

from civsim_harness.host.factory import get_host_platform  # noqa: E402
from civsim_harness.host.port import (  # noqa: E402
    CaptureStatus,
    GameProcess,
    HostPlatform,
    InputEvent,
    InputEventKind,
    InputStatus,
)
from civsim_harness.models.common import DeclarationId, ModelRef  # noqa: E402
from civsim_harness.models.run import LifecycleState  # noqa: E402
from civsim_harness.nexus.client import NexusClient  # noqa: E402
from civsim_harness.nexus.sentinels import LUA_JSON_PRELUDE, lua_print_json  # noqa: E402
from civsim_harness.provider.port import ModelCapabilities, RawDecision  # noqa: E402
from civsim_harness.run.composition import (  # noqa: E402
    PROVIDER_POLICY_NAMES,
    build_provider,
    build_runner_dependencies,
)
from civsim_harness.run.preparation import LuaGameSetupReader  # noqa: E402
from civsim_harness.run.runner import Runner, RunPreparationFailed  # noqa: E402
from civsim_harness.store.sqlite_adapter import SqliteMatchStore  # noqa: E402

# -- the charter values (configs/seedsets/civsim-default.yaml), asserted against the live read-back
EXPECTED = {
    "civilization": "CIVILIZATION_PERSIA",
    "leader": "LEADER_CYRUS",
    "ruleset": "RULESET_EXPANSION_2",
    "difficulty": "DIFFICULTY_EMPEROR",
    "game_settings.game_speed": "GAMESPEED_ONLINE",
    "game_settings.starting_era": "ERA_ANCIENT",
    "map_settings.map_size": "MAPSIZE_SMALL",
}
PRESET = "CivSim DEFAULT"
SEED_SET = "civsim-default"
SEED_SET_PATH = REPO / "configs" / "seedsets" / "civsim-default.yaml"
PRIMARY_MODEL = "anthropic/claude-sonnet-5"
FALLBACK_MODEL = "anthropic/claude-opus-5"
END_TURN = DeclarationId("turn.end_turn")
TERMINAL = {LifecycleState.FINISHED, LifecycleState.FAILED}
GIF_WIDTH = 720
FRAME_INTERVAL_S = 1.0
INTRO_DISMISS_KEY = "Escape"


# --------------------------------------------------------------------------
# recording -- production capture path, background thread
# --------------------------------------------------------------------------


class Recorder:
    def __init__(self, host: HostPlatform) -> None:
        self.host = host
        self.frames: list[tuple[float, Image.Image]] = []
        self.captions: list[tuple[float, str]] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self.capture_failures = 0

    def note(self, text: str) -> None:
        self.captions.append((time.time(), text))
        print(f"  [{time.strftime('%H:%M:%S')}] {text}", flush=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=10)

    def _loop(self) -> None:
        while not self._stop.is_set():
            window = civ_window(self.host)
            if window is not None:
                result = self.host.capture_window(window)
                if result.status is CaptureStatus.ok and result.frame is not None:
                    frame = result.frame
                    img = Image.frombytes("RGBA", (frame.width, frame.height), frame.image_bytes)
                    b, g, r, _a = img.split()
                    rgb = Image.merge("RGB", (r, g, b))
                    h = int(rgb.height * GIF_WIDTH / rgb.width)
                    self.frames.append((time.time(), rgb.resize((GIF_WIDTH, h), Image.LANCZOS)))
                else:
                    self.capture_failures += 1
            self._stop.wait(FRAME_INTERVAL_S)

    def write(self, out: Path) -> dict[str, Any]:
        if not self.frames:
            return {"frames": 0}
        imgs = [f for _, f in self.frames]
        gif = out / "harness-landed-run.gif"
        imgs[0].save(
            gif,
            save_all=True,
            append_images=imgs[1:],
            duration=int(FRAME_INTERVAL_S * 1000),
            loop=0,
            optimize=True,
        )
        t0 = self.frames[0][0]
        keyframes = []
        for i, idx in enumerate(sorted({0, len(imgs) // 3, 2 * len(imgs) // 3, len(imgs) - 1})):
            ts, img = self.frames[idx]
            name = f"keyframe_{i}_t{int(ts - t0):03d}s.png"
            img.save(out / name)
            keyframes.append(name)
        with (out / "timeline.txt").open("w") as fh:
            for ts, text in self.captions:
                fh.write(f"t+{int(ts - t0):03d}s  {text}\n")
        return {
            "frames": len(imgs),
            "gif": gif.name,
            "gif_kb": gif.stat().st_size // 1024,
            "keyframes": keyframes,
            "capture_failures": self.capture_failures,
        }


def civ_window(host: HostPlatform):  # noqa: ANN201
    out = subprocess.run(["pgrep", "-x", "Civ6"], capture_output=True, text=True).stdout.split()
    if not out:
        return None
    return host.find_game_window(GameProcess(pid=int(out[0]), name="Civ6"))


def tuner_up() -> bool:
    return b":4318 " in subprocess.run(["ss", "-ltn"], capture_output=True).stdout


# --------------------------------------------------------------------------
# operator bring-up (NOT production -- T237): fresh CivSim DEFAULT game, Cyrus pinned
# --------------------------------------------------------------------------


async def connect(retries: int = 30) -> NexusClient:
    last: Exception | None = None
    for _ in range(retries):
        client = NexusClient()
        try:
            await client.connect()
            return client
        except Exception as exc:  # noqa: BLE001
            last = exc
            await asyncio.sleep(3.0)
    raise RuntimeError(f"tuner unreachable: {last}")


async def lua(client: NexusClient, state: str, body: str) -> Any:
    indices = await client.refresh_state_indices()
    if state not in indices.by_name:
        raise RuntimeError(f"{state} absent; states: {sorted(indices.by_name)}")
    return await client.execute_command(state_index=indices.by_name[state], lua_body=body)


async def tuner_answers() -> bool:
    """A real handshake, not a listening socket. Measured 2026-09-20: after `Network.HostGame`
    the port keeps LISTENING through content-configure and the leader-intro screen while the
    tuner does not answer, so `ss -ltn` said "back" with the intro still up and the run then
    spent four minutes failing handshakes. The production loader keys its retry on the
    handshake (`_await_phase`'s reconnect), and so must this."""
    client = NexusClient(connect_timeout_s=5.0)
    try:
        await client.connect()
        return True
    except Exception:  # noqa: BLE001 -- any failure to answer is "press again"
        return False
    finally:
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            pass


async def dismiss_intro_until_port(
    host: HostPlatform, rec: Recorder, budget_s: float = 300
) -> int:
    """Escape until the tuner answers -- the T248 shape, using the production port ops."""
    presses = 0
    deadline = time.time() + budget_s
    while time.time() < deadline:
        if await tuner_answers():
            return presses
        window = civ_window(host)
        if window is not None:
            focused = host.focus_window(window)
            if focused.status is InputStatus.ok:
                sent = host.send_input(
                    [InputEvent(kind=InputEventKind.key_press, key=INTRO_DISMISS_KEY)]
                )
                if sent.status is InputStatus.ok:
                    presses += 1
            else:
                rec.note(f"focus_window refused: {focused.status.value} ({focused.reason})")
        await asyncio.sleep(5.0)
    return presses


async def bring_up(host: HostPlatform, rec: Recorder, map_seed: str) -> None:
    client = await connect()
    try:
        indices = await client.refresh_state_indices()
        if "InGame" in indices.by_name:
            rec.note("operator bring-up: leaving the current game (Events.ExitToMainMenu)")
            try:
                await lua(
                    client, "InGame", "pcall(function() Events.ExitToMainMenu() end) print('ok')"
                )
            except Exception:  # noqa: BLE001 -- the transition tears the connection down
                pass
    finally:
        await client.close()

    await asyncio.sleep(5.0)
    client = await connect()
    try:
        for _ in range(40):
            indices = await client.refresh_state_indices()
            if "HostGame" in indices.by_name and "InGame" not in indices.by_name:
                break
            await asyncio.sleep(3.0)
        else:
            raise RuntimeError("HostGame state never appeared at the front end")

        rec.note(f"operator bring-up: applying '{PRESET}' from Lua, human slot, Cyrus/Persia, seed")
        result = await lua(
            client,
            "HostGame",
            (
                LUA_JSON_PRELUDE
                + "GameConfiguration.SetToDefaults(); "
                + "local lp = {}; lp.Location=SaveLocations.LOCAL_STORAGE; lp.Type=SaveTypes.SINGLE_PLAYER; "
                + "lp.FileType=SaveFileTypes.GAME_CONFIGURATION; lp.IsAutosave=false; lp.IsQuicksave=false; "
                + f'lp.Directory=SaveDirectories.DEFAULT; lp.Name="{PRESET}"; '
                + "local loaded = Network.LoadGame(lp, ServerType.SERVER_TYPE_NONE); "
                + "PlayerConfigurations[0]:SetSlotStatus(SlotStatus.SS_TAKEN); "
                + f'PlayerConfigurations[0]:SetLeaderTypeName("{EXPECTED["leader"]}"); '
                + f'PlayerConfigurations[0]:SetCivilizationTypeName("{EXPECTED["civilization"]}"); '
                + f'pcall(function() GameConfiguration.SetValue("RANDOM_SEED", {int(map_seed)}) end); '
                + lua_print_json(
                    {
                        "preset_loaded": "loaded",
                        "humans": "GameConfiguration.GetHumanPlayerCount()",
                        "leader": "PlayerConfigurations[0]:GetLeaderTypeName()",
                        "seed": 'tostring(GameConfiguration.GetValue("RANDOM_SEED"))',
                        "turn_timer_none": 'GameConfiguration.GetTurnTimerType() == DB.MakeHash("TURNTIMER_NONE")',
                    }
                )
            ),
        )
        rec.note(f"operator bring-up: front-end read-back {result}")
        if not (
            isinstance(result, dict)
            and result.get("preset_loaded") is True
            and result.get("humans") == 1
            and result.get("turn_timer_none") is True
        ):
            raise RuntimeError(f"bring-up read-back is not the expected setup: {result!r}")

        rec.note("operator bring-up: Network.HostGame(SERVER_TYPE_NONE)")
        try:
            await lua(
                client,
                "HostGame",
                "pcall(function() Network.HostGame(ServerType.SERVER_TYPE_NONE) end) print('issued')",
            )
        except Exception:  # noqa: BLE001 -- the load closes the port; expected
            pass
    finally:
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            pass

    await asyncio.sleep(20.0)
    rec.note("operator bring-up: dismissing the leader-intro screen (focus_window + send_input)")
    presses = await dismiss_intro_until_port(host, rec)
    rec.note(f"operator bring-up: intro dismissed after {presses} Escape press(es); port is back")

    client = await connect()
    try:
        for _ in range(40):
            indices = await client.refresh_state_indices()
            if "InGame" in indices.by_name:
                return
            await asyncio.sleep(3.0)
        raise RuntimeError("InGame never appeared after HostGame")
    finally:
        await client.close()


# --------------------------------------------------------------------------
# read the setup back through the production reader and write the configuration
# --------------------------------------------------------------------------


async def read_setup() -> dict[str, Any]:
    client = await connect()
    try:
        reader = LuaGameSetupReader(
            lambda i, b: client.execute_command(state_index=i, lua_body=b),
            state_index_source=client,
            state_name="InGame",
        )
        snapshot = await reader.read(
            list(EXPECTED) + ["map_seed", "opponents.major_count", "mod_set"]
        )
        turn = await lua(
            client,
            "InGame",
            LUA_JSON_PRELUDE + lua_print_json({"turn": "Game.GetCurrentGameTurn()"}),
        )
        return {
            "values": dict(snapshot.values),
            "unread": dict(snapshot.unread),
            "turn_timer_hash": snapshot.turn_timer_hash,
            "turn_timer_type": snapshot.turn_timer_type,
            "turn": turn["turn"] if isinstance(turn, dict) else None,
        }
    finally:
        await client.close()


def write_config(
    out: Path, setup: dict[str, Any], *, provider: str, turns: int, use_seed_set: bool
) -> tuple[Path, int]:
    values = setup["values"]
    for name, expected in EXPECTED.items():
        if values.get(name) != expected:
            raise SystemExit(
                f"REFUSED: live read-back {name}={values.get(name)!r}, charter expects {expected!r}"
            )
    start_turn = int(setup["turn"])
    seed_set = yaml.safe_load(SEED_SET_PATH.read_text(encoding="utf-8"))
    config: dict[str, Any] = {
        "schema_version": 1,
        "config_id": f"demo-landed-{provider}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}",
        "map_seed": str(values["map_seed"]),
        "civilization": EXPECTED["civilization"],
        "leader": EXPECTED["leader"],
        "ruleset": EXPECTED["ruleset"],
        "mod_set": seed_set["mod_set"],
        "map_settings": {"map_size": EXPECTED["map_settings.map_size"]},
        "game_settings": {
            "game_speed": EXPECTED["game_settings.game_speed"],
            "starting_era": EXPECTED["game_settings.starting_era"],
        },
        "difficulty": EXPECTED["difficulty"],
        "opponents": {},
        "stop_condition": {"type": "turn_reached", "turn": turns},  # harness-relative: runner.py counts its own turns from 1
        "model_config": {
            "primary": {"provider": "openrouter", "model": PRIMARY_MODEL},
            "fallbacks": [{"provider": "openrouter", "model": FALLBACK_MODEL}],
            "request_params": {"temperature": 0.7},
        },
        "no_progress_step_limit": 8,
        "recovery_attempt_limit": 3,
        "min_free_disk_gb": 25,
    }
    if use_seed_set:
        config["seed_set"] = SEED_SET
    if isinstance(values.get("opponents.major_count"), int):
        config["opponents"]["major_count"] = values["opponents.major_count"]
    path = out / "run-config.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return path, start_turn


# --------------------------------------------------------------------------
# the run -- production composition root
# --------------------------------------------------------------------------


def fake_provider() -> Any:
    from fakes.fake_provider import FakeModelProvider

    provider = FakeModelProvider()
    for model in (PRIMARY_MODEL, FALLBACK_MODEL):
        provider.set_capabilities(
            ModelRef(provider="openrouter", model=model),
            ModelCapabilities(
                max_context_tokens=1_000_000,
                accepts_images=True,
                max_images_per_request=8,
                confirmed=True,
            ),
        )
    provider.set_default_decision(
        RawDecision(
            action_declaration_id=END_TURN,
            reasoning="scripted demo decision: end the turn (no model was consulted)",
            is_end_turn=True,
        )
    )
    return provider


def store_counts(db: Path, run_id: str) -> dict[str, Any]:
    """What the store recorded for *run_id* -- the far-side record, read straight from SQLite.
    Every table keeps its record as a JSON column (`run_json`, `capture_json`, `call_json`), so
    the fields are pulled out of those rather than assumed as columns."""
    conn = sqlite3.connect(db)
    try:

        def count(table: str) -> int:
            columns = {row[1] for row in conn.execute(f"pragma table_info({table})")}  # noqa: S608
            if "run_id" in columns:
                where = "run_id = ?"
            else:
                # decision_steps hang off their turn cycle, not the run.
                where = "turn_cycle_id in (select turn_cycle_id from turn_cycles where run_id = ?)"
            return conn.execute(
                f"select count(*) from {table} where {where}",  # noqa: S608
                (run_id,),
            ).fetchone()[0]

        row = conn.execute("select run_json from runs where run_id = ?", (run_id,)).fetchone()
        run = json.loads(row[0]) if row else {}
        captures = [
            json.loads(r[0])
            for r in conn.execute("select capture_json from captures where run_id = ?", (run_id,))
        ]
        calls = [
            json.loads(r[0])
            for r in conn.execute("select call_json from model_calls where run_id = ?", (run_id,))
        ]
        turns = [
            r[0]
            for r in conn.execute(
                "select distinct turn_number from turn_cycles where run_id = ? order by 1",
                (run_id,),
            )
        ]
        counts = {
            t: count(t)
            for t in (
                "turn_cycles",
                "decision_steps",
                "save_points",
                "captures",
                "run_events",
                "model_calls",
            )
        }
    except sqlite3.OperationalError as exc:
        return {"error": f"store query failed: {exc}"}
    finally:
        conn.close()

    def _sum(key: str) -> float:
        return sum((c.get("cost") or {}).get(key) or 0 for c in calls)

    return {
        "run": {
            k: run.get(k)
            for k in (
                "lifecycle_state",
                "stop_resolution",
                "host_support_tier",
                "record_completeness_status",
                "comparability_status",
                "game_build",
                "capture_path",
            )
        },
        **counts,
        "turns_recorded": turns,
        "captures_shown_to_agent": sum(1 for c in captures if c.get("shown_to_agent") is True),
        "capture_screening": sorted({str(c.get("screening_status")) for c in captures}),
        "model_calls_served": sorted(
            {
                f"{(c.get('model_served') or {}).get('provider')}/{(c.get('model_served') or {}).get('model')}"
                for c in calls
            }
        ),
        "model_cost_usd": round(_sum("amount_usd"), 4),
        "model_input_tokens": int(_sum("input_tokens")),
        "model_output_tokens": int(_sum("output_tokens")),
    }


def resolve_provider(
    name: str, *, seed: int, policy: str, script_path: str | Path | None = None
) -> Any:
    """The provider `--provider NAME` selects, sampling by `--provider-policy POLICY`.

    `openrouter` is left to `build_runner_dependencies`' own default (`None` here) so this script
    never constructs the production adapter itself; `stochastic` and `scripted` go through the
    composition root's own `build_provider`, so every flag resolves the same way any other
    caller's would; and `fake` is the one name that cannot come from production code -- it is a
    test double under `tests/fakes/`, built here. `policy` is ignored by every provider that has
    no sampler.

    *script_path* (T326) is `--script PATH` for `--provider scripted`. It is keyword-only with a
    `None` default **only** so that the two existing call sites and every caller of the other
    three names keep working unchanged; it is emphatically not a safe default. `build_provider`
    raises `ValueError` when `name == "scripted"` and no script was supplied, so the one shape
    this project keeps being bitten by -- an optional parameter whose empty default every test
    supplies and the production call site does not -- fails loudly here rather than producing a
    provider that silently ends every turn. `tests/unit/test_scripted_provider.py` asserts that
    refusal through this exact function.
    """
    if name == "fake":
        return fake_provider()
    if name == "openrouter":
        return None
    return build_provider(
        name,
        seed=seed,
        policy=policy,
        script_path=Path(script_path) if script_path is not None else None,
        catalog_root=REPO / "catalogs",
    )


def run_it(
    out: Path,
    config_path: Path,
    *,
    provider: str,
    provider_seed: int,
    provider_policy: str,
    rec: Recorder,
    store_path: Path,
    host: HostPlatform,
) -> dict[str, Any]:
    store = SqliteMatchStore(store_path)
    deps = build_runner_dependencies(
        store=store,
        host=host,
        catalog_root=REPO / "catalogs",
        provider=resolve_provider(provider, seed=provider_seed, policy=provider_policy),
    )
    runner = Runner(deps)
    rec.note(
        f"PRODUCTION: Runner.start({config_path.name}) via build_runner_dependencies "
        f"[provider={provider} provider_seed={provider_seed} "
        f"provider_policy={provider_policy}]"
    )
    t0 = time.perf_counter()
    try:
        run_id = runner.start(config_path)
    except RunPreparationFailed as exc:
        rec.note(f"run preparation FAILED: {exc} detail={getattr(exc, 'detail', None)}")
        return {"started": False, "error": str(exc), "detail": getattr(exc, "detail", None)}
    rec.note(f"run created: {run_id}")

    last: tuple[Any, ...] | None = None
    while True:
        status = runner.get_status(run_id)
        key = (status.lifecycle_state, status.current_turn, status.current_step)
        if key != last:
            rec.note(
                f"status: {status.lifecycle_state.value} turn={status.current_turn} "
                f"step={status.current_step}"
                + (f" last_error={status.last_error}" if status.last_error else "")
            )
            last = key
        if status.lifecycle_state in TERMINAL:
            break
        if status.lifecycle_state is LifecycleState.PAUSED:
            # A paused run is a visible stall (unknown screen, catalog gap, unconfirmed end turn).
            # The runner keeps it resumable, but this driver has no operator loop: treat it as
            # this block's terminal state so a stall costs seconds, not the whole block
            # (gameplay 2026-09-21: blocks 1, 7, 11 and 12 each hung here until killed).
            rec.note("run paused: the driver treats a paused run as this block's terminal state")
            break
        time.sleep(2.0)
    elapsed = time.perf_counter() - t0
    rec.note(f"run {status.lifecycle_state.value} after {elapsed:.0f}s")
    time.sleep(3.0)
    if status.lifecycle_state is LifecycleState.PAUSED:
        # This process is the run's only holder; once it exits the run-identity lock is stale and
        # would refuse the next block on the same client (T235 tripwire, operator action).
        lock = Path("/tmp/civsim_harness/run_locks") / f"{run_id}.lock.json"
        if lock.exists():
            lock.unlink()
            rec.note(f"paused run: stale run-identity lock cleared by the driver ({lock.name})")
    return {
        "started": True,
        "run_id": str(run_id),
        "final_state": status.lifecycle_state.value,
        "elapsed_s": round(elapsed, 1),
        "last_error": status.last_error.model_dump() if status.last_error else None,
        "store": store_counts(store_path, str(run_id)),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--provider", choices=("fake", "openrouter", "stochastic"), default="fake")
    # Distinct from `--seed` below, which is the MAP seed the bring-up types into the client.
    ap.add_argument("--provider-seed", type=int, default=0)
    # T262. `coverage` is the recommended setting for a zero-cost coverage block: it draws only
    # from the actions the decision request shows as available right now. `uniform` stays the
    # default so the two policies remain comparable on the same board.
    ap.add_argument(
        "--provider-policy",
        choices=PROVIDER_POLICY_NAMES,
        default="uniform",
        help=(
            "how --provider stochastic samples: 'coverage' (RECOMMENDED for a coverage block) "
            "draws only from the actions the request shows as available now, preferring what "
            "this run has not landed yet, and never falls back to a greyed-out action; "
            "'uniform' (default) draws from everything the request lists"
        ),
    )
    ap.add_argument("--turns", type=int, default=3)
    ap.add_argument("--bring-up", action="store_true")
    ap.add_argument("--no-seed-set", action="store_true")
    ap.add_argument("--store", default=str(REPO / "civsim-match-store.db"))
    ap.add_argument("--seed", default="1414213562")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    host = get_host_platform()
    rec = Recorder(host)
    rec.start()
    rec.note("recording started: every frame via production capture_window() (window-scoped)")
    results: dict[str, Any] = {
        "provider": args.provider,
        "provider_seed": args.provider_seed,
        "provider_policy": args.provider_policy,
        "turns_requested": args.turns,
        "bring_up": args.bring_up,
        "started_at": datetime.now(UTC).isoformat(),
    }
    try:
        if args.bring_up:
            asyncio.run(bring_up(host, rec, args.seed))
        rec.note("PRODUCTION: LuaGameSetupReader read-back of the live setup (V2's own path)")
        setup = asyncio.run(read_setup())
        results["live_setup"] = setup
        rec.note(
            f"live setup: turn={setup['turn']} "
            + " ".join(
                f"{k.split('.')[-1]}={v}" for k, v in setup["values"].items() if k != "mod_set"
            )
        )
        config_path, start_turn = write_config(
            out,
            setup,
            provider=args.provider,
            turns=args.turns,
            use_seed_set=not args.no_seed_set,
        )
        results["start_turn"] = start_turn
        results["config"] = config_path.name
        results["run"] = run_it(
            out,
            config_path,
            provider=args.provider,
            provider_seed=args.provider_seed,
            provider_policy=args.provider_policy,
            rec=rec,
            store_path=Path(args.store),
            host=host,
        )
    except BaseException as exc:  # noqa: BLE001 -- the record must be written whatever happened
        rec.note(f"ABORTED: {type(exc).__name__}: {exc}")
        results["aborted"] = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, KeyboardInterrupt):
            pass
    finally:
        rec.stop()
        results["recording"] = rec.write(out)
        results["finished_at"] = datetime.now(UTC).isoformat()
        (out / "results.json").write_text(json.dumps(results, indent=2, default=str))
        print(json.dumps(results, indent=2, default=str))
    return 0 if results.get("run", {}).get("final_state") == "finished" else 1


if __name__ == "__main__":
    raise SystemExit(main())
