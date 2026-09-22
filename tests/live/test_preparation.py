"""T191 live acceptance: FR-002 / V2 -- exact match starts, deliberate mismatch refuses (SC/V2).

**Task**: T191 (`specs/002-civ-playing-harness/tasks.md`). **Requirement**: FR-002, preparation
gate V2 (`run/preparation.py::verify_configuration`) -- "against the real client with real
settings, an exact match starts, and any deliberate mismatch fails before turn 1 with the
mismatch recorded."

**Prior evidence this formalises**: `specs/002-civ-playing-harness/spikes/demo-evidence-linux-landed/`
already drove the production composition root (`build_runner_dependencies` -> `Runner.start` ->
V2 setup read-back agreement -> turn cycles -> `SqliteMatchStore`) against a real client with an
exact-match configuration and recorded it finishing three turns. What that evidence does NOT cover
is the negative control: a configuration that deliberately disagrees with the live client. This
module drives both halves through the actual production V2 gate -- never a hand-rolled pre-check
of our own -- exactly the way `Runner.start` is called in production and in
`tests/live/demo_landed_run.py`.

**What V2 actually does, read from source** (`run/preparation.py::verify_configuration`,
`run/composition.py::_prepare_connected_run`): every configured field
(`map_seed`, `civilization`, `leader`, `ruleset`, `mod_set`, `difficulty`, every
`map_settings.*`/`game_settings.*`/`opponents.*` key) is read back live and compared. A clean
match lets the run proceed to `PLAYING` with no ``preparation_mismatch`` event. Any mismatch --
even one field -- transitions the `Run` straight to `FAILED` (`_fail_preparation`) and writes a
`RunEvent` of type `RunEventType.PREPARATION_MISMATCH` whose `detail["mismatches"]` names the
field, the expected value, and the value actually read back. No turn-1 quicksave, no turn cycle,
no decision step is ever recorded for a run that fails here -- preflight, before turn 1.

**Precondition this test assumes** (skips cleanly otherwise): the live client is already sitting
in an active `CivSim DEFAULT` game (Persia/Cyrus/`RULESET_EXPANSION_2`/`DIFFICULTY_EMPEROR`/
`GAMESPEED_ONLINE`/`ERA_ANCIENT`/`MAPSIZE_SMALL`, `InGame` state reachable) -- the same charter
`configs/seedsets/civsim-default.yaml` pins and `demo-evidence-linux-landed/` was recorded
against. That is an operator/bring-up step (T237 is not landed), not something this test can
create; see `tests/live/demo_landed_run.py --bring-up` for how the evidence run above was staged.

**What a human runs to make this pass, and what would make it fail**: with Civ VI up, the tuner
reachable on 127.0.0.1:4318, and the client sitting in a `CivSim DEFAULT` game matching the
charter above, run ``pytest tests/live/test_preparation.py -m live``. `test_exact_match_starts`
fails if a genuinely matching configuration is refused by V2 (a false-positive mismatch, or V2
comparing the wrong value) or never reaches turn 1. `test_deliberate_mismatch_fails_before_turn_1`
fails if a configuration that deliberately disagrees with the live client is nonetheless allowed
to start, reach turn 1, or leaves no recorded mismatch -- i.e. if V2 stops actually gating.

This module never sends input to, or otherwise mutates, the live client -- it only connects the
production Nexus client to read state back and drives the harness's own preparation/turn-cycle
machinery, exactly as `Runner.start` does for every real run.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tests"))

from civsim_harness.models.records import RunEventType  # noqa: E402
from civsim_harness.models.run import LifecycleState  # noqa: E402
from civsim_harness.nexus.client import NexusClient  # noqa: E402
from civsim_harness.run.composition import build_runner_dependencies  # noqa: E402
from civsim_harness.run.preparation import LuaGameSetupReader  # noqa: E402
from civsim_harness.run.runner import Runner  # noqa: E402
from civsim_harness.store.sqlite_adapter import SqliteMatchStore  # noqa: E402

pytestmark = pytest.mark.live

SEED_SET_PATH = REPO / "configs" / "seedsets" / "civsim-default.yaml"

# The charter `demo-evidence-linux-landed/` and `configs/seedsets/civsim-default.yaml` both pin.
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
POLL_TIMEOUT_S = 180.0


def _require(condition: bool, reason: str) -> None:
    if not condition:
        pytest.skip(reason)


def _civ6_pid() -> int | None:
    out = subprocess.run(["pgrep", "-x", "Civ6"], capture_output=True, text=True).stdout.split()
    return int(out[0]) if out else None


def _tuner_reachable() -> bool:
    return b":4318 " in subprocess.run(["ss", "-ltn"], capture_output=True).stdout


async def _connect(retries: int = 5) -> NexusClient:
    last: Exception | None = None
    for _ in range(retries):
        client = NexusClient(connect_timeout_s=5.0)
        try:
            await client.connect()
            return client
        except Exception as exc:  # noqa: BLE001 -- any connect failure is a skip, not an error
            last = exc
            await asyncio.sleep(1.0)
    raise RuntimeError(f"tuner unreachable: {last}")


async def _read_live_setup() -> dict[str, Any]:
    """One production V2 read-back (`LuaGameSetupReader`), exactly as `demo_landed_run.py`'s
    `read_setup()` does it -- the harness's own reader, not a hand probe."""
    client = await _connect()
    try:
        reader = LuaGameSetupReader(
            lambda i, b: client.execute_command(state_index=i, lua_body=b),
            state_index_source=client,
            state_name="InGame",
        )
        snapshot = await reader.read(
            [*EXPECTED, "map_seed", "opponents.major_count", "mod_set"]
        )
        return dict(snapshot.values)
    finally:
        await client.close()


@pytest.fixture(scope="module")
def live_setup() -> dict[str, Any]:
    """The live client's current setup, read back once through production code.

    Skips the whole module if Civ VI/the tuner is absent, or if the client is not currently
    sitting in a game matching the CivSim DEFAULT charter (an operator/bring-up precondition this
    test cannot create -- see the module docstring).
    """
    _require(_civ6_pid() is not None, "Civilization VI (Civ6 process) is not running")
    _require(_tuner_reachable(), "FireTuner tuner port 4318 is not reachable")

    try:
        values = asyncio.run(_read_live_setup())
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"could not read the live game setup back: {exc}")

    for name, expected in EXPECTED.items():
        if values.get(name) != expected:
            pytest.skip(
                f"live client is not at the CivSim DEFAULT charter: {name}={values.get(name)!r}, "
                f"expected {expected!r} -- bring the client to that preset first"
            )
    if not isinstance(values.get("map_seed"), (str, int)) or not values.get("map_seed"):
        pytest.skip(f"live map_seed read-back was not usable: {values.get('map_seed')!r}")
    return values


def _base_config_dict(live_setup: dict[str, Any], *, config_id: str) -> dict[str, Any]:
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
        "stop_condition": {"type": "turn_reached", "turn": 1},
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


def _write_config(path: Path, config: dict[str, Any]) -> Path:
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return path


def _run_to_terminal(config_path: Path, store_path: Path) -> tuple[Any, Any, Any]:
    """Drive *config_path* through the real production path (`build_runner_dependencies` ->
    `Runner`), with `FakeModelProvider` (zero model calls, zero cost -- this is a preflight test,
    not an agent-play test), and poll to a terminal/PLAYING state.

    Returns ``(runner, run_id, status)``.
    """
    from fakes.fake_provider import FakeModelProvider  # tests/fakes -- never importable from src

    from civsim_harness.host.factory import get_host_platform

    store = SqliteMatchStore(store_path)
    deps = build_runner_dependencies(
        store=store,
        host=get_host_platform(),
        catalog_root=REPO / "catalogs",
        provider=FakeModelProvider(),
    )
    runner = Runner(deps)
    run_id = runner.start(config_path)

    waited = 0.0
    status = runner.get_status(run_id)
    while status.lifecycle_state not in TERMINAL and waited < POLL_TIMEOUT_S:
        import time as _time

        _time.sleep(POLL_S)
        waited += POLL_S
        status = runner.get_status(run_id)
    return runner, run_id, status


def test_exact_match_starts(live_setup: dict[str, Any], tmp_path: Path) -> None:
    """A configuration that genuinely matches the live client passes V2 and reaches turn 1."""
    config = _base_config_dict(live_setup, config_id=f"t191-exact-{uuid.uuid4().hex[:8]}")
    config_path = _write_config(tmp_path / "exact.yaml", config)
    store_path = tmp_path / "exact-match.db"

    runner, run_id, status = _run_to_terminal(config_path, store_path)

    assert status.lifecycle_state is not LifecycleState.FAILED, (
        f"an exact-match configuration was refused by V2: last_error={status.last_error}"
    )
    store = SqliteMatchStore(store_path)
    mismatch_events = store.list_run_events(run_id, event_types=[RunEventType.PREPARATION_MISMATCH])
    assert mismatch_events == [], (
        f"an exact-match run recorded a preparation_mismatch it should not have: "
        f"{[e.detail for e in mismatch_events]}"
    )
    good_save = store.get_last_known_good(run_id)
    assert good_save is not None and good_save.turn_number >= 1, (
        "an exact-match run never reached a verified turn-1 quicksave"
    )


def test_deliberate_mismatch_fails_before_turn_1(live_setup: dict[str, Any], tmp_path: Path) -> None:
    """A configuration that deliberately disagrees with the live client is refused by V2,
    before turn 1, with the mismatch recorded -- the negative control T191 requires."""
    config = _base_config_dict(live_setup, config_id=f"t191-mismatch-{uuid.uuid4().hex[:8]}")
    wrong_civilization = "CIVILIZATION_ROME"
    assert wrong_civilization != live_setup["civilization"], (
        "test bug: the deliberate mismatch must actually disagree with the live read-back"
    )
    config["civilization"] = wrong_civilization
    config_path = _write_config(tmp_path / "mismatch.yaml", config)
    store_path = tmp_path / "mismatch.db"

    runner, run_id, status = _run_to_terminal(config_path, store_path)

    assert status.lifecycle_state is LifecycleState.FAILED, (
        f"a deliberately mismatched configuration was NOT refused: "
        f"lifecycle_state={status.lifecycle_state}"
    )
    store = SqliteMatchStore(store_path)
    good_save = store.get_last_known_good(run_id)
    assert good_save is None, (
        f"a run refused by V2 nonetheless recorded a verified save at turn "
        f"{good_save.turn_number if good_save else None} -- it must fail before turn 1"
    )

    mismatch_events = store.list_run_events(run_id, event_types=[RunEventType.PREPARATION_MISMATCH])
    assert len(mismatch_events) == 1, (
        f"expected exactly one preparation_mismatch event, got {len(mismatch_events)}"
    )
    mismatches = mismatch_events[0].detail.get("mismatches", [])
    fields = {m["field"] for m in mismatches}
    assert "civilization" in fields, (
        f"the recorded mismatch does not name the field that actually disagreed: {mismatches!r}"
    )
    civ_mismatch = next(m for m in mismatches if m["field"] == "civilization")
    assert civ_mismatch["expected"] == wrong_civilization
    assert civ_mismatch["actual"] == live_setup["civilization"]
