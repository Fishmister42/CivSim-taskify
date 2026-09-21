"""Unit test for save retention (T162, FR-036, invariant I17, research R17).

A **finished, unarchived, year-old** run yields **zero** eligible save
points -- not by age, not by quota, not by retention window, not by being
terminal, not by thinning. Only `saves/archival.py`'s `archive_run` (which
delegates to `MatchStore.archive_run`, contracts/match-store-port.md A1-A4)
ever makes a save point eligible. A required save found missing is
reported (`saves/addressing.py`, T172) rather than worked around by
resuming from -- or branching from -- a different turn.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from civsim_harness.errors import StoreWriteError
from civsim_harness.models.config import RunConfiguration
from civsim_harness.models.records import RetentionStatus, SavePoint
from civsim_harness.models.run import Run
from civsim_harness.saves.addressing import (
    SaveAddressingError,
    report_save_missing,
    require_available_save_point,
)
from civsim_harness.saves.archival import archive_run
from civsim_harness.store.sqlite_adapter import SqliteMatchStore

NOW = datetime(2026, 9, 19, tzinfo=UTC)
A_YEAR_AGO = NOW - timedelta(days=400)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SqliteMatchStore]:
    adapter = SqliteMatchStore(tmp_path / "match.db")
    yield adapter
    adapter.close()


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


def _make_run(
    run_id: str, *, lifecycle_state: str = "playing", stop_resolution: str | None = None
) -> Run:
    return Run.model_validate(
        {
            "run_id": run_id,
            "config_id": "cfg-ret",
            "lifecycle_state": lifecycle_state,
            "stop_resolution": stop_resolution,
            "record_completeness_status": "unknown",
            "comparability_status": "comparable",
            "observation_catalog_version": {"version": "2026.09.1", "content_hash": "abc"},
            "action_catalog_version": {"version": "2026.09.1", "content_hash": "abc"},
            "game_build": "win/1.0.12.9",
            "host_support_tier": "validated",
            "capture_path": "windows_graphics_capture",
        }
    )


def _make_save_point(
    save_point_id: str, run_id: str, turn: int, *, taken_at: datetime
) -> SavePoint:
    return SavePoint.model_validate(
        {
            "save_point_id": save_point_id,
            "run_id": run_id,
            "turn_number": turn,
            "save_name": f"civsim__{run_id}__t{turn:04d}",
            "taken_at": taken_at,
            "verified": True,
            "retention_status": "retained",
        }
    )


# --------------------------------------------------------------------------
# The core negative test: age, quota, terminal-ness, and thinning are all
# not eligibility rules.
# --------------------------------------------------------------------------


def test_finished_unarchived_year_old_run_yields_zero_eligible_save_points(
    store: SqliteMatchStore,
) -> None:
    store.create_run(
        _make_run("run-old", lifecycle_state="finished", stop_resolution="turn_reached"),
        _make_config("cfg-ret"),
    )
    # A full, ancient "game" worth of saves -- if any rule other than
    # explicit archival made a save eligible, this is exactly the shape
    # that rule would trigger on: many saves, all very old, on a run that
    # already reached a terminal state.
    for turn in range(1, 301):
        store.write_save_point(
            _make_save_point(f"run-old-sp{turn}", "run-old", turn, taken_at=A_YEAR_AGO)
        )

    eligible = store.list_eligible_save_points()
    eligible_for_this_run = [sp for sp in eligible if sp.run_id == "run-old"]

    assert eligible_for_this_run == []
    statuses = {sp.retention_status for sp in store.list_save_points("run-old")}
    assert statuses == {RetentionStatus.RETAINED}


def test_a_run_still_playing_with_old_saves_is_equally_ineligible(store: SqliteMatchStore) -> None:
    """Not being terminal is not what protects a save either -- nothing
    about lifecycle_state feeds eligibility in either direction.
    """
    store.create_run(_make_run("run-playing", lifecycle_state="playing"), _make_config("cfg-ret"))
    store.write_save_point(
        _make_save_point("run-playing-sp1", "run-playing", 1, taken_at=A_YEAR_AGO)
    )

    assert store.list_eligible_save_points() == []


# --------------------------------------------------------------------------
# The only positive path: archive_run, and nothing else
# --------------------------------------------------------------------------


def test_archive_run_is_the_only_thing_that_makes_a_save_eligible(store: SqliteMatchStore) -> None:
    store.create_run(
        _make_run("run-to-archive", lifecycle_state="finished", stop_resolution="turn_reached"),
        _make_config("cfg-ret"),
    )
    store.write_save_point(
        _make_save_point("run-to-archive-sp1", "run-to-archive", 1, taken_at=A_YEAR_AGO)
    )
    assert store.list_eligible_save_points() == []

    archive_run(store, "run-to-archive", by="researcher", at=NOW)

    eligible_ids = {sp.save_point_id for sp in store.list_eligible_save_points()}
    assert "run-to-archive-sp1" in eligible_ids


def test_archive_run_rejects_a_non_terminal_run(store: SqliteMatchStore) -> None:
    """A3: archival itself requires a terminal run -- but that is a gate on
    *archiving*, not an alternate route to eligibility. A non-terminal run
    still has zero eligible saves either way (asserted above); this test
    only shows archive_run cannot be used to smuggle one through early.
    """
    store.create_run(_make_run("run-active", lifecycle_state="playing"), _make_config("cfg-ret"))

    with pytest.raises(StoreWriteError):
        archive_run(store, "run-active", by="researcher", at=NOW)


def test_archive_run_requires_a_non_empty_operator_identity(store: SqliteMatchStore) -> None:
    store.create_run(
        _make_run("run-anon", lifecycle_state="finished", stop_resolution="turn_reached"),
        _make_config("cfg-ret"),
    )
    with pytest.raises(ValueError):
        archive_run(store, "run-anon", by="", at=NOW)


# --------------------------------------------------------------------------
# A required save found missing is reported, never worked around
# --------------------------------------------------------------------------


def test_required_save_found_missing_is_reported_not_substituted(store: SqliteMatchStore) -> None:
    store.create_run(_make_run("run-missing"), _make_config("cfg-ret"))
    store.write_save_point(_make_save_point("run-missing-sp23", "run-missing", 23, taken_at=NOW))
    # A neighbouring turn's save exists and is perfectly healthy -- proving
    # a later requirement failure is not "there was nothing else to use".
    store.write_save_point(_make_save_point("run-missing-sp24", "run-missing", 24, taken_at=NOW))

    save_point = require_available_save_point(store, "run-missing", 23)
    updated = report_save_missing(store, save_point, occurred_at=NOW)
    assert updated.missing is True

    with pytest.raises(SaveAddressingError) as excinfo:
        require_available_save_point(store, "run-missing", 23)

    # The exact missing save is named -- never a nearby turn substituted.
    assert excinfo.value.detail["turn"] == 23
    assert excinfo.value.detail["save_name"] == "civsim__run-missing__t0023"

    # Turn 24's save is untouched and still resolvable on its own.
    still_fine = require_available_save_point(store, "run-missing", 24)
    assert still_fine.missing is False


def test_removed_save_point_is_also_refused_by_addressing(store: SqliteMatchStore) -> None:
    """The reaper (T171) marks a deleted save's record `removed`, never
    deleting the record itself -- addressing must treat that exactly like
    `missing`: refused, never silently retargeted.
    """
    store.create_run(_make_run("run-removed"), _make_config("cfg-ret"))
    save_point = _make_save_point("run-removed-sp5", "run-removed", 5, taken_at=NOW)
    store.write_save_point(save_point)
    removed = save_point.model_copy(update={"retention_status": RetentionStatus.REMOVED})
    store.write_save_point(removed)

    with pytest.raises(SaveAddressingError):
        require_available_save_point(store, "run-removed", 5)


# --------------------------------------------------------------------------
# 003 FR-023 / US5 AC3 (T041): a setup preset beside quicksaves is never eligible
# --------------------------------------------------------------------------


def test_a_preset_beside_quicksaves_is_never_eligible_and_never_reaped(tmp_path: Path) -> None:
    """The saves directory shares space with `.Civ6Cfg` setup presets (live finding,
    `spikes/civsim-default-preset-linux.md`). Eligibility is a property of *records*, and a
    preset can never be one: `SavePoint.save_name` must match `civsim__<run>__t<turn>`, and
    `list_eligible_save_points` reads records, never the directory. The reaper's plan names
    only paths derived from eligible records -- so the preset, and the unarchived run's
    quicksaves, are structurally out of reach.
    """
    from pydantic import ValidationError

    from civsim_harness.saves.reaper import plan_reap
    from civsim_harness.saves.verify import SAVE_FILE_SUFFIX, resolve_saves_dir
    from fakes.fake_host import FakeHostPlatform

    store = SqliteMatchStore(tmp_path / "match.db")
    try:
        host = FakeHostPlatform()
        saves_dir = resolve_saves_dir(host, home=tmp_path)
        saves_dir.mkdir(parents=True)
        preset = saves_dir / "CivSim DEFAULT.Civ6Cfg"
        preset.write_bytes(b"preset")

        for run_id in ("run-archived", "run-open"):
            store.create_run(
                _make_run(run_id, lifecycle_state="finished", stop_resolution="turn_reached"),
                _make_config("cfg1"),
            )
            for turn in (1, 2):
                save = _make_save_point(f"{run_id}-sp{turn}", run_id, turn, taken_at=NOW)
                store.write_save_point(save)
                (saves_dir / f"{save.save_name}{SAVE_FILE_SUFFIX}").write_bytes(b"save")
        store.archive_run("run-archived", by="t", at=NOW)

        eligible = store.list_eligible_save_points()
        assert {s.run_id for s in eligible} == {"run-archived"}
        assert all(s.save_name.startswith("civsim__run-archived__t") for s in eligible)

        planned = {item.path.name for item in plan_reap(store, host, home=tmp_path)}
        assert planned == {f"civsim__run-archived__t000{t}{SAVE_FILE_SUFFIX}" for t in (1, 2)}
        assert preset.name not in planned
        assert not any("run-open" in name for name in planned)

        with pytest.raises(ValidationError):
            SavePoint.model_validate(
                {
                    **_make_save_point("x", "run-open", 1, taken_at=NOW).model_dump(),
                    "save_name": "CivSim DEFAULT.Civ6Cfg",
                }
            )
    finally:
        store.close()
