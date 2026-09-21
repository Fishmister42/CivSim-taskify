"""The web interface over the real store takes no degraded-capability path (T026; 003 FR-016,
SC-003; contract E1-E5).

`civsim_web` is **not** modified by deliverable 3. This test builds the actual FastAPI app
through the web's own test fixture, binds it to a `SqliteMatchStore` opened `read_only=True` --
exactly how a reader process opens the store -- and asserts the three answers the interface used
to give when its probes found nothing (a partial catalog, an unreachable blob, an unaddressable
attempt) are gone, and the configuration columns the keying collision used to blank are filled.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from civsim_harness.store.sqlite_adapter import SqliteMatchStore
from store_support.builders import (
    make_capture,
    make_config,
    make_run,
    make_save_point,
    make_turn_cycle_record,
    record_run,
)

pytest.importorskip("fastapi")
pytest.importorskip("jinja2")

KEPT_BYTES = b"\x89PNG\r\n\x1a\n" + b"frame-bytes" * 8


@pytest.fixture
def populated(tmp_path: Path) -> Iterator[SqliteMatchStore]:
    writer = SqliteMatchStore(tmp_path / "web.db")
    # run-1: playing, three turns, a kept and a withheld capture on turn 2 step 1.
    writer.create_run(
        make_run("run-1", "cfg-1", started_at=datetime(2026, 9, 21, 9, tzinfo=UTC)),
        make_config("cfg-1", seed_set_id="ss"),
    )
    for turn in (1, 2, 3):
        writer.write_save_point(make_save_point(f"run-1-sp{turn}-0", "run-1", turn))
        captures = {1: ["cap-kept", "cap-withheld"]} if turn == 2 else {}
        writer.write_turn_cycle(
            make_turn_cycle_record(
                "run-1",
                turn,
                num_steps=2,
                yields={"science": 3 * turn},
                cities=1,
                captures_by_step=captures,
            )
        )
    kept, blob = make_capture("cap-kept", "run-1", 2, "run-1-t2-a0-step1", blob=KEPT_BYTES)
    writer.write_capture(kept, blob)
    withheld, _ = make_capture("cap-withheld", "run-1", 2, "run-1-t2-a0-step1", blob=None)
    writer.write_capture(withheld, None)
    # run-2: finished and archived.
    record_run(
        writer, "run-2", turns=2, seed_set_id="ss", lifecycle_state="finished", started_minutes=-120
    )
    writer.archive_run("run-2", by="t", at=datetime(2026, 9, 21, 10, tzinfo=UTC))
    # run-3: turn 2 replayed -- attempt 0 abandoned, attempt 1 authoritative.
    writer.create_run(
        make_run("run-3", "cfg-3", started_at=datetime(2026, 9, 21, 8, tzinfo=UTC)),
        make_config("cfg-3", seed_set_id="ss"),
    )
    for turn in (1, 2):
        writer.write_save_point(make_save_point(f"run-3-sp{turn}-0", "run-3", turn))
    writer.write_turn_cycle(make_turn_cycle_record("run-3", 1, num_steps=1))
    writer.write_turn_cycle(make_turn_cycle_record("run-3", 2, 0, num_steps=1))
    writer.mark_turn_superseded("run-3", 2, 0)
    writer.write_turn_cycle(make_turn_cycle_record("run-3", 2, 1, num_steps=2))
    writer.close()

    reader = SqliteMatchStore(tmp_path / "web.db", read_only=True)
    yield reader
    reader.close()


def _find(payload: Any, key: str) -> list[Any]:
    found: list[Any] = []
    if isinstance(payload, dict):
        for name, value in payload.items():
            if name == key:
                found.append(value)
            found.extend(_find(value, key))
    elif isinstance(payload, list):
        for item in payload:
            found.extend(_find(item, key))
    return found


def test_the_catalog_is_complete_and_its_configuration_columns_are_filled(
    populated: SqliteMatchStore,
) -> None:
    from web_support.fixtures import make_client

    with make_client(populated) as client:
        body = client.get("/runs", headers={"Accept": "application/json"}).json()
    assert body["listing_is_partial"] is False and body["partial_reason"] is None
    assert body["total"] == 3
    by_id = {run["run_id"]: run for run in body["runs"]}
    assert set(by_id) == {"run-1", "run-2", "run-3"}  # archived run-2 listed like any other
    for run in by_id.values():
        assert run["civilization"] == "CIVILIZATION_PERSIA"
        assert run["leader"] == "LEADER_CYRUS"
        assert run["ruleset"] == "RULESET_EXPANSION_2"
        assert run["seed"] == "123"
        assert not [u for u in run["unavailable"] if u["field"].startswith("RunConfiguration")]
    assert by_id["run-1"]["turn_count"] == 3
    assert by_id["run-1"]["outcome_metrics"].get("science") == 9.0


def test_a_kept_capture_serves_its_bytes_and_a_withheld_one_is_explicitly_unavailable(
    populated: SqliteMatchStore,
) -> None:
    from web_support.fixtures import make_client

    with make_client(populated) as client:
        kept = client.get("/captures/cap-kept/image")
        withheld = client.get(
            "/captures/cap-withheld/image", headers={"Accept": "application/json"}
        )
    assert kept.status_code == 200
    assert kept.content == KEPT_BYTES
    assert kept.headers["content-type"].startswith("image/png")
    assert withheld.status_code == 404  # never 503 capture_blob_unreachable, never a placeholder
    assert "capture_blob_unreachable" not in withheld.text
    assert "capture_unavailable" in withheld.text


def test_a_named_abandoned_attempt_is_returned_as_itself(populated: SqliteMatchStore) -> None:
    from web_support.fixtures import make_client

    with make_client(populated) as client:
        attempt0 = client.get(
            "/runs/run-3/turns/2?attempt=0", headers={"Accept": "application/json"}
        )
        current = client.get("/runs/run-3/turns/2", headers={"Accept": "application/json"})
    assert attempt0.status_code == 200, attempt0.text
    assert "attempt_not_addressable" not in attempt0.text
    body0 = attempt0.json()
    assert 0 in _find(body0, "attempt_index")
    assert False in _find(body0, "is_authoritative")
    body1 = current.json()
    assert 1 in _find(body1, "attempt_index")
    assert json.dumps(body1) != json.dumps(body0)


def test_the_live_view_resolves_the_current_turn_from_the_store(
    populated: SqliteMatchStore,
) -> None:
    from web_support.fixtures import make_client

    with make_client(populated) as client:
        body = client.get("/runs/run-1", headers={"Accept": "application/json"}).json()
    assert body["summary"]["run_id"] == "run-1"
    assert body["summary"]["civilization"] == "CIVILIZATION_PERSIA"
    assert body["summary"]["turn_count"] == 3
