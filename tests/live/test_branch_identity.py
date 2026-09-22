"""T177 live acceptance: SC-014 -- two branches from one save point begin from an identical
game position.

**Task**: T177. **Requirement**: SC-014 ("two runs started from the same save point begin from
an identical game position, and branching a run leaves the parent's record unchanged"). Mirrors
`tests/integration/test_branching.py`'s shape (two branches from one save point, each recording
`parent_run_id`/`parent_turn`, the parent's own record unchanged) but drives it against a REAL
client actually loading each branch's inherited save -- that integration test uses a
`_RecordingLoader` test double that never touches a game; this module is the live half T226's own
closing note calls out as client-gated.

**CRITICAL, load-bearing constraint (research R5): saves are NOT byte-stable across
save/load/save.** R5's own live finding is that re-saving a loaded game does not reproduce the
original file byte-for-byte. Therefore "identical game position" in this module is asserted
EXCLUSIVELY on game state read back from the live client after each branch loads (turn number,
treasury gold, map seed) -- **never** on `.Civ6Save` file bytes, sizes, or hashes. No assertion in
this file touches a save file's contents; every comparison below is a value read back live over a
fresh Nexus connection, independently for each branch.

**What a human runs to make this pass, and what would make it fail**: with Civ VI up, the tuner
reachable, and the client able to load a `CivSim DEFAULT` game (bring-up as in
`tests/live/demo_landed_run.py --bring-up`), run
``pytest tests/live/test_branch_identity.py -m live``. Expect this to take several minutes: it
plays a short parent run to a turn-1 quicksave, then drives TWO SEQUENTIAL branches from that
save point (only one client connection exists, so branches cannot run concurrently), reading game
state back independently after each. It fails if the two branches' read-back state diverges (a
real position-non-determinism or save/load regression), or if the parent's own stored record
(`Run`, save points) changes as a side effect of branching (a lineage-recording regression,
Constitution Principle IV).
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tests"))

from civsim_harness.models.run import LifecycleState  # noqa: E402
from civsim_harness.nexus.client import NexusClient  # noqa: E402
from civsim_harness.nexus.sentinels import LUA_JSON_PRELUDE, lua_print_json  # noqa: E402
from civsim_harness.run.composition import build_runner_dependencies  # noqa: E402
from civsim_harness.run.preparation import LuaGameSetupReader  # noqa: E402
from civsim_harness.run.runner import Runner  # noqa: E402
from civsim_harness.store.sqlite_adapter import SqliteMatchStore  # noqa: E402

pytestmark = pytest.mark.live

SEED_SET_PATH = REPO / "configs" / "seedsets" / "civsim-default.yaml"
EXPECTED: dict[str, Any] = {
    "civilization": "CIVILIZATION_PERSIA",
    "leader": "LEADER_CYRUS",
    "ruleset": "RULESET_EXPANSION_2",
    "difficulty": "DIFFICULTY_EMPEROR",
    "game_settings.game_speed": "GAMESPEED_ONLINE",
    "game_settings.starting_era": "ERA_ANCIENT",
    "map_settings.map_size": "MAPSIZE_SMALL",
}
TERMINAL = {LifecycleState.FINISHED, LifecycleState.FAILED, LifecycleState.PAUSED}
POLL_S = 2.0
POLL_TIMEOUT_S = 300.0
BRANCH_TURN = 1


def _require(condition: bool, reason: str) -> None:
    if not condition:
        pytest.skip(reason)


def _civ6_pid() -> int | None:
    out = subprocess.run(["pgrep", "-x", "Civ6"], capture_output=True, text=True).stdout.split()
    return int(out[0]) if out else None


def _tuner_reachable() -> bool:
    return b":4318 " in subprocess.run(["ss", "-ltn"], capture_output=True).stdout


async def _connect(retries: int = 10) -> NexusClient:
    last: Exception | None = None
    for _ in range(retries):
        client = NexusClient(connect_timeout_s=5.0)
        try:
            await client.connect()
            return client
        except Exception as exc:  # noqa: BLE001
            last = exc
            await asyncio.sleep(2.0)
    raise RuntimeError(f"tuner unreachable after {retries} attempts: {last}")


async def _read_live_setup() -> dict[str, Any]:
    client = await _connect()
    try:
        reader = LuaGameSetupReader(
            lambda i, b: client.execute_command(state_index=i, lua_body=b),
            state_index_source=client,
            state_name="InGame",
        )
        snapshot = await reader.read([*EXPECTED, "map_seed", "opponents.major_count"])
        return dict(snapshot.values)
    finally:
        await client.close()


async def _read_game_position(retries: int = 15) -> dict[str, Any]:
    """Independent, fresh-connection read-back of the game state that makes "identical position"
    checkable -- turn number, local player's treasury gold, and the map seed. Every field here is
    a Lua accessor already used elsewhere in this repo (see the module docstring and this task's
    own report for citations): `Game.GetCurrentGameTurn()`, `Game.GetLocalPlayer()`,
    `Players[pid]:GetTreasury():GetGoldBalance()` (shape used in `tests/unit/test_yields_lua.py`),
    `MapConfiguration.GetValue("RANDOM_SEED")` (shape used in `run/preparation.py`'s own V2
    getters and `tests/live/demo_landed_run.py`).
    """
    client = await _connect(retries=retries)
    try:
        indices = await client.refresh_state_indices()
        _require("InGame" in indices.by_name, "client is not InGame; cannot read game position")
        body = LUA_JSON_PRELUDE + lua_print_json(
            {
                "turn": "Game.GetCurrentGameTurn()",
                "player": "Game.GetLocalPlayer()",
                "gold": (
                    "(function() local pid = Game.GetLocalPlayer(); "
                    "local p = Players[pid]; "
                    "if p == nil then return nil end; "
                    "local t = p:GetTreasury(); "
                    "if t == nil then return nil end; "
                    "return t:GetGoldBalance() end)()"
                ),
                "map_seed": 'tostring(MapConfiguration.GetValue("RANDOM_SEED"))',
            }
        )
        result = await client.execute_command(
            state_index=indices.by_name["InGame"], lua_body=body
        )
        _require(isinstance(result, dict), f"unexpected read-back shape: {result!r}")
        assert isinstance(result, dict)
        return result
    finally:
        await client.close()


def _base_config_dict(live_setup: dict[str, Any], *, config_id: str, stop_turn: int) -> dict[str, Any]:
    seed_set = yaml.safe_load(SEED_SET_PATH.read_text(encoding="utf-8"))
    config: dict[str, Any] = {
        "schema_version": 1,
        "config_id": config_id,
        "map_seed": str(live_setup["map_seed"]),
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
        "stop_condition": {"type": "turn_reached", "turn": stop_turn},
        "model_config": {
            "primary": {"provider": "openrouter", "model": "anthropic/claude-sonnet-5"},
            "fallbacks": [{"provider": "openrouter", "model": "anthropic/claude-opus-5"}],
            "request_params": {"temperature": 0.7},
        },
        "no_progress_step_limit": 8,
        "recovery_attempt_limit": 3,
        "min_free_disk_gb": 5,
    }
    if isinstance(live_setup.get("opponents.major_count"), int):
        config["opponents"]["major_count"] = live_setup["opponents.major_count"]
    return config


def _branch_config_dict(*, config_id: str, parent_run_id: str, turn: int) -> dict[str, Any]:
    """A minimal branch document: everything but `branch_from` and `stop_condition` is inherited
    from the parent's own `RunConfiguration` (contracts/run-configuration.md "Branch
    configuration") -- restating an inherited field here would be a MISMATCH the loader refuses,
    not a safe override, so this deliberately omits civilization/leader/ruleset/mod_set/map/game
    settings entirely."""
    return {
        "schema_version": 1,
        "config_id": config_id,
        "branch_from": {"run_id": parent_run_id, "turn": turn},
        "stop_condition": {"type": "turn_reached", "turn": turn},
        "model_config": {
            "primary": {"provider": "openrouter", "model": "anthropic/claude-sonnet-5"},
            "fallbacks": [{"provider": "openrouter", "model": "anthropic/claude-opus-5"}],
            "request_params": {"temperature": 0.7},
        },
    }


def _write_config(path: Path, config: dict[str, Any]) -> Path:
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return path


def _build_runner(store_path: Path) -> tuple[Any, Any]:
    from fakes.fake_provider import FakeModelProvider

    from civsim_harness.host.factory import get_host_platform

    store = SqliteMatchStore(store_path)
    deps = build_runner_dependencies(
        store=store,
        host=get_host_platform(),
        catalog_root=REPO / "catalogs",
        provider=FakeModelProvider(),
    )
    return Runner(deps), store


def _wait_terminal(runner: Any, run_id: Any) -> Any:
    waited = 0.0
    status = runner.get_status(run_id)
    while status.lifecycle_state not in TERMINAL and waited < POLL_TIMEOUT_S:
        time.sleep(POLL_S)
        waited += POLL_S
        status = runner.get_status(run_id)
    if status.lifecycle_state not in TERMINAL:
        pytest.fail(
            f"run {run_id} never reached a terminal state within {POLL_TIMEOUT_S:g}s "
            f"(last: {status.lifecycle_state})"
        )
    return status


@pytest.fixture(scope="module")
def live_setup() -> dict[str, Any]:
    _require(_civ6_pid() is not None, "Civilization VI (Civ6 process) is not running")
    _require(_tuner_reachable(), "FireTuner tuner port 4318 is not reachable")
    try:
        values = asyncio.run(_read_live_setup())
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"could not read the live game setup back: {exc}")
    for name, expected in EXPECTED.items():
        if values.get(name) != expected:
            pytest.skip(
                f"live client is not at the CivSim DEFAULT charter: {name}={values.get(name)!r}"
            )
    return values


def test_two_branches_from_one_save_point_begin_from_an_identical_position(
    live_setup: dict[str, Any], tmp_path: Path
) -> None:
    store_path = tmp_path / "branch-identity.db"
    runner, store = _build_runner(store_path)

    # -- 1. a short parent run, to a verified turn-1 quicksave (scaffolding, not the point of
    #    this test, but every assertion below is real: a broken parent leaves nothing to branch
    #    from, and this test must say so rather than silently pass on an empty comparison).
    parent_config = _base_config_dict(
        live_setup, config_id=f"t177-parent-{uuid.uuid4().hex[:8]}", stop_turn=BRANCH_TURN
    )
    parent_path = _write_config(tmp_path / "parent.yaml", parent_config)
    parent_run_id = runner.start(parent_path)
    parent_status = _wait_terminal(runner, parent_run_id)
    assert parent_status.lifecycle_state is not LifecycleState.FAILED, (
        f"parent run failed before it could reach turn {BRANCH_TURN}: {parent_status.last_error}"
    )
    parent_save_before = store.get_last_known_good(parent_run_id)
    assert parent_save_before is not None and parent_save_before.turn_number >= BRANCH_TURN, (
        "parent run has no verified quicksave at the branch turn -- nothing to branch from"
    )
    parent_run_before = store.get_run(parent_run_id)
    parent_saves_before = store.list_save_points(parent_run_id)
    assert parent_run_before is not None

    # -- 2. two sequential branches from the SAME parent run_id/turn. Sequential because there
    #    is exactly one live client connection (standing rule: "the tuner is one connection").
    readings: list[dict[str, Any]] = []
    for label in ("a", "b"):
        branch_config = _branch_config_dict(
            config_id=f"t177-branch-{label}-{uuid.uuid4().hex[:8]}",
            parent_run_id=str(parent_run_id),
            turn=BRANCH_TURN,
        )
        branch_path = _write_config(tmp_path / f"branch-{label}.yaml", branch_config)
        branch_run_id = runner.branch(
            parent_run_id=parent_run_id, turn=BRANCH_TURN, config_path=branch_path
        )
        branch_status = _wait_terminal(runner, branch_run_id)
        assert branch_status.lifecycle_state is not LifecycleState.FAILED, (
            f"branch {label} failed to start/play from the parent's turn-{BRANCH_TURN} save: "
            f"{branch_status.last_error}"
        )

        branch_run = store.get_run(branch_run_id)
        assert branch_run is not None
        assert branch_run.parent_run_id == parent_run_id, (
            f"branch {label} does not record its parent lineage (FR-033/FR-034): "
            f"parent_run_id={branch_run.parent_run_id}"
        )

        # The client's own client for this branch's run is closed once it reaches a terminal
        # state (T214) -- only THEN is it safe to open our own independent connection.
        try:
            reading = asyncio.run(_read_game_position())
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"could not read branch {label}'s game position back: {exc}")
        readings.append(reading)

    # -- 3. the real SC-014 assertion: independently read-back state agrees across both branches.
    #    NOTHING here compares .Civ6Save file bytes -- see the module docstring.
    reading_a, reading_b = readings
    assert reading_a.get("turn") == reading_b.get("turn"), (
        f"branches diverged on turn number: {reading_a.get('turn')!r} vs {reading_b.get('turn')!r}"
    )
    assert reading_a.get("gold") == reading_b.get("gold"), (
        f"branches diverged on treasury gold: {reading_a.get('gold')!r} vs {reading_b.get('gold')!r}"
    )
    assert reading_a.get("map_seed") == reading_b.get("map_seed"), (
        f"branches diverged on map seed: {reading_a.get('map_seed')!r} vs "
        f"{reading_b.get('map_seed')!r}"
    )

    # -- 4. the parent's own record is unchanged by branching (SC-014's second half, Principle
    #    IV) -- re-read the store's OWN objects, never client save bytes.
    parent_run_after = store.get_run(parent_run_id)
    parent_saves_after = store.list_save_points(parent_run_id)
    assert parent_run_after == parent_run_before, (
        "the parent Run record changed as a side effect of branching"
    )
    assert parent_saves_after == parent_saves_before, (
        "the parent's save points changed as a side effect of branching"
    )
