"""Unit test for the disk-headroom guard (T163).

spec edge case, research R17: with free space below `min_free_disk_gb`,
the run halts in a recorded state with a `disk_headroom_low` event and
**zero saves deleted** -- the harness must never free space by removing an
unarchived save. `saves/headroom.py` (T081) already implements the check
and the event builder; this test exercises them together with a real
`SqliteMatchStore` and a scripted `FakeHostPlatform`, and proves the "zero
saves deleted" half concretely by planting a save file on disk first and
showing it is untouched afterward.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from civsim_harness.errors import DiskHeadroomError
from civsim_harness.host.port import DiskSpace
from civsim_harness.models.config import RunConfiguration
from civsim_harness.models.records import RunEvent
from civsim_harness.models.run import LifecycleState, Run
from civsim_harness.run.lifecycle import transition
from civsim_harness.saves.headroom import build_disk_headroom_event, check_headroom
from civsim_harness.store.sqlite_adapter import SqliteMatchStore
from fakes.fake_host import FakeHostPlatform

NOW = datetime(2026, 9, 19, tzinfo=UTC)
_ONE_GB = 1024**3


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SqliteMatchStore]:
    adapter = SqliteMatchStore(tmp_path / "match.db")
    yield adapter
    adapter.close()


def _make_run(run_id: str) -> Run:
    return Run.model_validate(
        {
            "run_id": run_id,
            "config_id": "cfg-headroom",
            "lifecycle_state": "playing",
            "record_completeness_status": "unknown",
            "comparability_status": "comparable",
            "observation_catalog_version": {"version": "2026.09.1", "content_hash": "abc"},
            "action_catalog_version": {"version": "2026.09.1", "content_hash": "abc"},
            "game_build": "win/1.0.12.9",
            "host_support_tier": "validated",
            "capture_path": "windows_graphics_capture",
        }
    )


def _make_config(config_id: str) -> RunConfiguration:
    return RunConfiguration.model_validate(
        {
            "config_id": config_id,
            "map_seed": "1",
            "civilization": "CIVILIZATION_ROME",
            "leader": "LEADER_TRAJAN",
            "ruleset": "RULESET_STANDARD",
            "difficulty": "DIFFICULTY_PRINCE",
            "stop_condition": {"type": "turn_reached", "turn": 50},
            "model_config": {"primary": {"provider": "openrouter", "model": "x"}},
            "no_progress_step_limit": 8,
            "recovery_attempt_limit": 3,
            "min_free_disk_gb": 25,
            "created_at": NOW,
        }
    )


def _event_types(store: SqliteMatchStore, run_id: str) -> list[str]:
    """White-box helper (mirrors tests/contract/test_match_store_port.py's
    own `_run_archived_event_types`): the port has no read op for events, so
    verifying one was written requires reaching past the port into the
    adapter's own connection -- legitimate here since this test targets
    this adapter's own persisted state, not just the public surface.
    """
    rows = store._conn.execute(  # noqa: SLF001 -- intentional white-box check
        "SELECT event_json FROM run_events WHERE run_id = ?", (run_id,)
    ).fetchall()
    return [RunEvent.model_validate_json(row[0]).event_type.value for row in rows]


def test_check_headroom_raises_below_the_configured_floor(tmp_path: Path) -> None:
    host = FakeHostPlatform()
    host.set_disk_space(
        DiskSpace(path=tmp_path, free_bytes=5 * _ONE_GB, total_bytes=100 * _ONE_GB)
    )

    with pytest.raises(DiskHeadroomError):
        check_headroom(host=host, path=tmp_path, min_free_disk_gb=25)


def test_headroom_halt_records_disk_headroom_low_event_and_deletes_no_saves(
    store: SqliteMatchStore, tmp_path: Path
) -> None:
    saves_dir = tmp_path / "saves"
    saves_dir.mkdir()
    planted_save = saves_dir / "civsim__run-hr__t0007.Civ6Save"
    planted_save.write_bytes(b"not a real save, just a marker")

    host = FakeHostPlatform()
    host.set_disk_space(
        DiskSpace(path=saves_dir, free_bytes=5 * _ONE_GB, total_bytes=100 * _ONE_GB)
    )

    run = _make_run("run-hr")
    store.create_run(run, _make_config("cfg-headroom"))

    with pytest.raises(DiskHeadroomError) as excinfo:
        check_headroom(host=host, path=saves_dir, min_free_disk_gb=25)

    # The warning before the halt (data-model.md SS14): without it, the
    # halt below would look arbitrary.
    warning_event = build_disk_headroom_event(
        run_id=run.run_id, occurred_at=NOW, detail=excinfo.value.detail, turn_number=7
    )
    store.write_run_event(warning_event)

    # The halt itself: a legal, recorded lifecycle transition -- never a
    # deletion.
    halted, transition_event = transition(
        run, LifecycleState.PAUSED, occurred_at=NOW, turn_number=7
    )
    store.write_run_event(transition_event)
    store.update_run(run.run_id, lifecycle_state=halted.lifecycle_state)

    # -- the run halted in a recorded state --
    persisted = store.get_run("run-hr")
    assert persisted is not None
    assert persisted.lifecycle_state == LifecycleState.PAUSED

    # -- with the disk_headroom_low warning on the timeline --
    assert "disk_headroom_low" in _event_types(store, "run-hr")

    # -- and zero saves deleted: the planted save file is untouched, and no
    # save point was ever written, let alone removed, by this path --
    assert planted_save.exists()
    assert planted_save.read_bytes() == b"not a real save, just a marker"
    assert store.list_save_points("run-hr") == []
