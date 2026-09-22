"""T192 live acceptance: FR-002 / FR-031, quickstart Scenario 9 -- the build pin refuses a
patched (or, here, deliberately wrong) client build; `seedset accept-build` recovers it.

**Task**: T192. **Requirement**: FR-002, FR-031, preparation gate V10 / invariant I18
(`run/preparation.py::build_pin_preflight`), quickstart.md "Scenario 9 -- The build pin refuses a
patched client".

**What V10 actually does, read from source.** `run/composition.py::_prepare_run`'s own docstring
states its step ordering explicitly: `build_pin_preflight` (step 5) runs **before** the `Run`
record is even created (step 7: "every gate that could refuse the run for free has now passed").
`build_pin_preflight` (`run/preparation.py`) RAISES `BuildMismatchError` when the client's actual
composite build disagrees with the seed set's pinned `game_build` and no `BuildAcceptance` covers
that exact transition -- so **no `Run` row is ever written to the store for a rejected attempt**.
`Runner.start` wraps any `HarnessError` raised during preparation as
`civsim_harness.run.runner.RunPreparationFailed` and re-raises it
(`detail={"reason": str(exc)}` -- see `Runner.start`'s own except clause). This is a materially
different shape from a V2 setup mismatch (`tests/live/test_preparation.py`), which DOES create a
`Run` and transitions it to `FAILED` with a recorded event -- here there is no run_id to inspect
at all, only the raised exception.

Once an acceptance covers the transition, `build_pin_preflight` returns a `BuildPinResult` instead
of raising, preparation proceeds normally, and the resulting `Run.game_build_acceptance_ref` is
set to the covering `BuildAcceptance.acceptance_id` (`composition.py` line ~878).

**Never mutates the real seed set file.** Every seed set this module writes or accepts against is
a copy of `configs/seedsets/civsim-default.yaml` inside `tmp_path`; `configs/seedsets/` itself is
never touched.

**What a human runs to make this pass, and what would make it fail**: with Civ VI up at a
`CivSim DEFAULT` game and the tuner reachable, run
``pytest tests/live/test_build_pin.py -m live``. It fails if a seed set pinned to a build the
client does not report is nonetheless allowed to start a run, if `seedset accept-build`'s
resulting acceptance does not actually let a matching run start, if the started run's
`game_build_acceptance_ref` disagrees with the acceptance that was recorded, or if the seed set
does not report itself non-uniform (`SeedSet.is_uniform`) once a build change has been accepted.
This module never sends input to the client -- it only reads the client's own reported build via
the same `UI.GetAppVersion()` accessor named in this task, and drives the harness's real
composition root.
"""

from __future__ import annotations

import asyncio
import shutil
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

from civsim_harness.config.seed_set import load_seed_set_file  # noqa: E402
from civsim_harness.models.run import LifecycleState  # noqa: E402
from civsim_harness.nexus.client import NexusClient  # noqa: E402
from civsim_harness.nexus.sentinels import LUA_JSON_PRELUDE, lua_print_json  # noqa: E402
from civsim_harness.operator.cli import seedset_accept_build  # noqa: E402
from civsim_harness.run.composition import build_runner_dependencies  # noqa: E402
from civsim_harness.run.runner import Runner, RunPreparationFailed  # noqa: E402
from civsim_harness.store.sqlite_adapter import SqliteMatchStore  # noqa: E402

pytestmark = pytest.mark.live

SEED_SET_PATH = REPO / "configs" / "seedsets" / "civsim-default.yaml"
SEED_SET_NAME = "civsim-default"
WRONG_BUILD = "linux/0.0.0.0-does-not-exist"


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
            await asyncio.sleep(1.0)
    raise RuntimeError(f"tuner unreachable: {last}")


async def _read_actual_build() -> str:
    """The client's own reported build, composed the same way `observe/game_build.py`'s
    `compose_build("linux", version)` does -- read directly via `UI.GetAppVersion()` (named in
    this task; measured elsewhere in this repo as `"1.0.12.9 (564030)"`), the same raw-Lua
    technique `tests/live/demo_landed_run.py`/`test_pinned_leader_survives.py` already use for
    live reads that have no wired declaration to go through.
    """
    client = await _connect()
    try:
        indices = await client.refresh_state_indices()
        for state_name in ("InGame", "HostGame", "GameCore_Tuner"):
            if state_name not in indices.by_name:
                continue
            result = await client.execute_command(
                state_index=indices.by_name[state_name],
                lua_body=LUA_JSON_PRELUDE
                + lua_print_json({"version": "tostring(UI.GetAppVersion())"}),
            )
            if isinstance(result, dict) and result.get("version"):
                raw = str(result["version"])
                version = raw.split()[0]  # "1.0.12.9 (564030)" -> "1.0.12.9"
                return f"linux/{version}"
        raise RuntimeError(
            f"UI.GetAppVersion() did not resolve in any of the client's current states "
            f"({sorted(indices.by_name)})"
        )
    finally:
        await client.close()


@pytest.fixture(scope="module")
def actual_build() -> str:
    _require(_civ6_pid() is not None, "Civilization VI (Civ6 process) is not running")
    _require(_tuner_reachable(), "FireTuner tuner port 4318 is not reachable")
    try:
        return asyncio.run(_read_actual_build())
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"could not read the live client's build back: {exc}")


def _copy_seed_set(tmp_path: Path, *, game_build: str) -> Path:
    seedset_root = tmp_path / "seedsets"
    seedset_root.mkdir()
    raw = yaml.safe_load(SEED_SET_PATH.read_text(encoding="utf-8"))
    raw["game_build"] = game_build
    raw["accepted_build_changes"] = []
    dest = seedset_root / f"{SEED_SET_NAME}.yaml"
    dest.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return seedset_root


def _run_config_dict(*, config_id: str) -> dict[str, Any]:
    """Deliberately minimal: this test exercises V10 (the build pin) alone -- every other
    charter field is irrelevant to it, and a V2/V3 mismatch on some other field must not be able
    to mask or fake this gate's own result. `stop_condition` is `turn_reached` at turn 1 so a
    successful run costs as little wall-clock as possible."""
    seed_set = yaml.safe_load(SEED_SET_PATH.read_text(encoding="utf-8"))
    return {
        "schema_version": 1,
        "config_id": config_id,
        "map_seed": "1414213562",
        "civilization": seed_set["civilization"],
        "leader": seed_set["leader"],
        "ruleset": seed_set["ruleset"],
        "mod_set": seed_set["mod_set"],
        "map_settings": {},
        "game_settings": {},
        "difficulty": "DIFFICULTY_EMPEROR",
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
        "seed_set": SEED_SET_NAME,
    }


def _write_config(path: Path, config: dict[str, Any]) -> Path:
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return path


def _build_runner(store_path: Path, seedset_root: Path) -> tuple[Any, Any]:
    from fakes.fake_provider import FakeModelProvider

    from civsim_harness.host.factory import get_host_platform

    store = SqliteMatchStore(store_path)
    deps = build_runner_dependencies(
        store=store,
        host=get_host_platform(),
        catalog_root=REPO / "catalogs",
        seedset_root=seedset_root,
        provider=FakeModelProvider(),
    )
    return Runner(deps), store


def test_wrong_build_pin_refuses_before_any_run_is_created(
    actual_build: str, tmp_path: Path
) -> None:
    assert WRONG_BUILD != actual_build, "test bug: WRONG_BUILD must actually disagree"
    seedset_root = _copy_seed_set(tmp_path, game_build=WRONG_BUILD)
    runner, _store = _build_runner(tmp_path / "wrong.db", seedset_root)

    config_path = _write_config(
        tmp_path / "wrong-build-run.yaml",
        _run_config_dict(config_id=f"t192-wrong-{uuid.uuid4().hex[:8]}"),
    )

    with pytest.raises(RunPreparationFailed) as excinfo:
        runner.start(config_path)

    reason = str(excinfo.value.detail.get("reason", ""))
    assert WRONG_BUILD in reason, (
        f"the raised preparation failure does not name the seed set's pinned build: {reason!r}"
    )
    assert actual_build in reason, (
        f"the raised preparation failure does not name the client's actual build: {reason!r}"
    )
    # No Run was ever created for this attempt -- build_pin_preflight raises before the Run
    # record is persisted (composition.py's own step-5-before-step-7 ordering), so there is
    # deliberately no run_id to look up: the raised, correctly-detailed exception above IS the
    # assertion that this refused before a run could be constructed at all.


def test_accepted_build_change_starts_and_records_its_acceptance_and_makes_the_set_non_uniform(
    actual_build: str, tmp_path: Path
) -> None:
    seedset_root = _copy_seed_set(tmp_path, game_build=WRONG_BUILD)
    seed_set_path = seedset_root / f"{SEED_SET_NAME}.yaml"

    seedset_accept_build(
        name=SEED_SET_NAME,
        to_build=actual_build,
        reason="t192 live test: deliberate build pin recovery",
        by="t192-live-test",
        seedset_root=seedset_root,
    )

    accepted_set = load_seed_set_file(seed_set_path)
    assert not accepted_set.is_uniform, (
        "a seed set with an accepted build change must report itself non-uniform"
    )
    assert len(accepted_set.accepted_build_changes) == 1
    acceptance = accepted_set.accepted_build_changes[0]
    assert acceptance.from_build == WRONG_BUILD
    assert acceptance.to_build == actual_build
    assert acceptance.acceptance_id

    runner, store = _build_runner(tmp_path / "accepted.db", seedset_root)
    config_path = _write_config(
        tmp_path / "accepted-build-run.yaml",
        _run_config_dict(config_id=f"t192-accepted-{uuid.uuid4().hex[:8]}"),
    )

    run_id = runner.start(config_path)
    status = runner.get_status(run_id)
    assert status.lifecycle_state is not LifecycleState.FAILED, (
        f"a run against an accepted build change was refused: {status.last_error}"
    )

    stored_run = store.get_run(run_id)
    assert stored_run is not None
    assert stored_run.game_build_acceptance_ref == acceptance.acceptance_id, (
        "the started run does not record the acceptance reference that let it start "
        f"(got {stored_run.game_build_acceptance_ref!r}, expected {acceptance.acceptance_id!r})"
    )
