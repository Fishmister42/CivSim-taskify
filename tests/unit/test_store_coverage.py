"""`civsim store coverage` -- the claimed-versus-demonstrated scorecard.

The fixture store is built through the store's **own write API** (`SqliteMatchStore`), never by
hand-writing rows, so what the scorecard reads back is exactly what a live run would have left
behind: applied and refused actions, observations that produced something and observations that
produced an empty value, a delivered capture and a withheld one, an abandoned attempt alongside
its replay, and a recorded `unknown_screen` stall. The real repo-root store is never touched.

The claimed surface is supplied as a small, explicit `ClaimedSurface` in most tests so the
assertions stay stable as the catalog grows; one test loads the real catalog and the real
`lua/ingame/screens.lua` through the production loaders to prove that path works too.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from civsim_harness.errors import StoreReadError, StoreWriteError
from civsim_harness.models.decision import Decision
from civsim_harness.models.records import ModelCall, RunEvent
from civsim_harness.models.turn import DecisionStep, Observation
from civsim_harness.operator.cli import app
from civsim_harness.store.contract import MatchTrackingStore
from civsim_harness.store.coverage import (
    ClaimedSurface,
    ScreenSurface,
    compute_coverage,
    load_claimed_surface,
    load_screen_surface,
    render_json,
    render_markdown,
)
from civsim_harness.store.port import DecisionStepBundle, TurnCycleRecord
from civsim_harness.store.sqlite_adapter import SqliteMatchStore
from store_support.builders import (
    NOW,
    at,
    catalog_ref,
    make_capture,
    make_config,
    make_run,
    make_save_point,
    make_turn_cycle,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

runner = CliRunner()


# --------------------------------------------------------------------------
# Fixture records
# --------------------------------------------------------------------------


def _entries(*, cities_value: Any, units_value: Any, raw_screen_id: str) -> list[dict[str, Any]]:
    return [
        {
            "declaration_id": "cities.state",
            "key": "cities",
            "value": cities_value,
            "context": "GameCore_Tuner",
        },
        {
            "declaration_id": "units.state",
            "key": "units",
            "value": units_value,
            "context": "InGame",
        },
        {
            "declaration_id": "game.screen_state",
            "key": "game.screen_state",
            "value": {
                "screen": "world",
                "raw_screen_id": raw_screen_id,
                "recognized": True,
                "has_blocking_prompt": False,
            },
            "context": "InGame",
        },
    ]


def _bundle(
    run_id: str,
    turn_cycle_id: str,
    step_index: int,
    *,
    action: str,
    outcome: str = "applied",
    rejection_reason: str | None = None,
    screen_identity: str = "world",
    raw_screen_id: str = "InGame",
    prompt_type: str | None = None,
    image_count: int = 0,
    captures: Sequence[str] = (),
    cost_usd: float | None = None,
) -> DecisionStepBundle:
    """One decision step, with the outcome and observation shape this suite needs."""
    step_id = f"{turn_cycle_id}-step{step_index}"
    call_id = f"{step_id}-call"
    execution: dict[str, Any] = {"outcome": outcome, "verified_at": NOW}
    if rejection_reason is not None:
        execution["rejection_reason"] = rejection_reason
    return DecisionStepBundle(
        step=DecisionStep.model_validate(
            {
                "decision_step_id": step_id,
                "turn_cycle_id": turn_cycle_id,
                "step_index": step_index,
                "observation_id": f"{step_id}-obs",
                "decision_id": f"{step_id}-dec",
                "model_call_id": call_id,
                "progress": "changed_state" if outcome == "applied" else "rejected",
                "no_progress_streak_after": 0,
                "visually_degraded": image_count == 0,
                "started_at": NOW,
                "ended_at": NOW,
            }
        ),
        observation=Observation.model_validate(
            {
                "observation_id": f"{step_id}-obs",
                "decision_step_id": step_id,
                "assembled_at": NOW,
                "catalog_version": catalog_ref(),
                # `units.state` deliberately carries an empty list: recorded, attributed, and
                # still not a demonstration that the declaration ever produced anything.
                "entries": _entries(
                    cities_value={"cities": [{"id": 1}]},
                    units_value=[],
                    raw_screen_id=raw_screen_id,
                ),
                "captures": list(captures),
                "screen_identity": screen_identity,
            }
        ),
        decision=Decision.model_validate(
            {
                "decision_id": f"{step_id}-dec",
                "decision_step_id": step_id,
                "action_declaration_id": action,
                "parameters": {},
                "reasoning": "because",
                "trigger": "prompt_response" if prompt_type else "proactive",
                "prompt_type": prompt_type,
                "is_end_turn": action == "turn.end_turn",
                "model_call_id": call_id,
                "execution": execution,
            }
        ),
        model_call=ModelCall.model_validate(
            {
                "model_call_id": call_id,
                "run_id": run_id,
                "turn_cycle_id": turn_cycle_id,
                "decision_step_id": step_id,
                "model_requested": {"provider": "openrouter", "model": "x"},
                "model_served": {"provider": "openrouter", "model": "x"},
                "latency_ms": 10,
                "cost": {"amount_usd": cost_usd} if cost_usd is not None else {},
                "retry_count": 0,
                "fallback_occurred": False,
                "image_count": image_count,
                "outcome": "decision_returned",
            }
        ),
    )


def _record(
    run_id: str,
    turn: int,
    attempt: int,
    bundles: list[DecisionStepBundle],
    *,
    is_authoritative: bool = True,
    outcome: str = "ended_by_agent",
) -> TurnCycleRecord:
    cycle = make_turn_cycle(
        f"{run_id}-t{turn}-a{attempt}",
        run_id,
        turn,
        attempt,
        is_authoritative=is_authoritative,
        step_count=len(bundles),
        save_point_id=f"{run_id}-sp{turn}",
        outcome=outcome,
    )
    return TurnCycleRecord(turn_cycle=cycle, steps=bundles)


RUN_ID = "run-cov"
OTHER_RUN_ID = "run-later"


def _populate(path: Path) -> None:
    """Two runs: one with the full shape under test, one later run for `--run` / `--since`."""
    store = SqliteMatchStore(path)
    try:
        store.create_run(
            make_run(RUN_ID, f"{RUN_ID}-cfg", started_at=at(0)), make_config(f"{RUN_ID}-cfg")
        )
        store.write_save_point(make_save_point(f"{RUN_ID}-sp1", RUN_ID, 1))
        store.write_save_point(make_save_point(f"{RUN_ID}-sp2", RUN_ID, 2))

        turn1 = f"{RUN_ID}-t1-a0"
        store.write_turn_cycle(
            _record(
                RUN_ID,
                1,
                0,
                [
                    _bundle(
                        RUN_ID,
                        turn1,
                        1,
                        action="units.found_city",
                        outcome="applied",
                        image_count=1,
                        captures=["cap-shown"],
                        cost_usd=0.01,
                    ),
                    _bundle(
                        RUN_ID,
                        turn1,
                        2,
                        action="units.move_to",
                        outcome="rejected",
                        rejection_reason="unavailable_to_human_now",
                    ),
                    _bundle(
                        RUN_ID,
                        turn1,
                        3,
                        action="turn.end_turn",
                        outcome="rejected",
                        rejection_reason="verification_failed",
                    ),
                ],
            )
        )

        # Turn 2 was attempted, abandoned, and replayed: the abandoned attempt's action really
        # did run on the client, so coverage counts it.
        turn2_abandoned = f"{RUN_ID}-t2-a0"
        store.write_turn_cycle(
            _record(
                RUN_ID,
                2,
                0,
                [
                    _bundle(
                        RUN_ID,
                        turn2_abandoned,
                        1,
                        action="units.found_city",
                        outcome="rejected",
                        rejection_reason="illegal_in_context",
                    )
                ],
                is_authoritative=False,
                outcome="abandoned",
            )
        )
        turn2 = f"{RUN_ID}-t2-a1"
        store.write_turn_cycle(
            _record(
                RUN_ID,
                2,
                1,
                [
                    _bundle(
                        RUN_ID,
                        turn2,
                        1,
                        action="prompts.tech_civic_completed",
                        outcome="applied",
                        screen_identity="prompt.tech_civic_completed",
                        raw_screen_id="TechCivicCompletedPopup",
                        prompt_type="prompt.tech_civic_completed",
                    )
                ],
            )
        )

        shown, blob = make_capture("cap-shown", RUN_ID, 1, f"{turn1}-step1", blob=b"png-bytes")
        store.write_capture(shown, blob)
        withheld, _ = make_capture(
            "cap-withheld",
            RUN_ID,
            1,
            f"{turn1}-step2",
            blob=None,
            withheld_reason="provenance_failure",
        )
        store.write_capture(withheld, None)

        store.write_run_event(
            RunEvent.model_validate(
                {
                    "event_id": "ev-unknown",
                    "run_id": RUN_ID,
                    "turn_number": 2,
                    "step_index": 1,
                    "event_type": "unknown_screen",
                    "occurred_at": at(2),
                    "detail": {"raw_screen_id": "TechCivicCompletedPopup"},
                }
            )
        )
        store.write_run_event(
            RunEvent.model_validate(
                {
                    "event_id": "ev-capfail",
                    "run_id": RUN_ID,
                    "turn_number": 1,
                    "event_type": "capture_failed",
                    "occurred_at": at(1),
                    "detail": {},
                }
            )
        )
        store.write_run_event(
            RunEvent.model_validate(
                {
                    "event_id": "ev-withheld",
                    "run_id": RUN_ID,
                    "turn_number": 1,
                    "event_type": "image_withheld",
                    "occurred_at": at(1),
                    "detail": {},
                }
            )
        )

        store.create_run(
            make_run(OTHER_RUN_ID, f"{OTHER_RUN_ID}-cfg", started_at=at(120)),
            make_config(f"{OTHER_RUN_ID}-cfg"),
        )
        store.write_save_point(make_save_point(f"{OTHER_RUN_ID}-sp1", OTHER_RUN_ID, 1))
        store.write_turn_cycle(
            _record(
                OTHER_RUN_ID,
                1,
                0,
                [
                    _bundle(
                        OTHER_RUN_ID,
                        f"{OTHER_RUN_ID}-t1-a0",
                        1,
                        action="camera.move",
                        outcome="applied",
                    )
                ],
            )
        )
    finally:
        store.close()


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    path = tmp_path / "coverage.db"
    _populate(path)
    return path


@pytest.fixture
def store(store_path: Path) -> Iterator[SqliteMatchStore]:
    adapter = SqliteMatchStore(store_path, read_only=True)
    yield adapter
    adapter.close()


CLAIMED = ClaimedSurface(
    action_ids=(
        "camera.move",
        "prompts.tech_civic_completed",
        "turn.end_turn",
        "units.found_city",
        "units.move_to",
    ),
    observation_ids=("cities.state", "game.screen_state", "research.state", "units.state"),
    view_ids=("views.city_screen", "views.world"),
    screens=ScreenSurface(
        screen_ids=("prompt.tech_civic_completed", "prompt.unit_promotion", "strategic", "world"),
        watched_states=("CityPanel", "TechCivicCompletedPopup", "UnitPromotionPopup"),
        state_by_screen_id={
            "prompt.tech_civic_completed": "TechCivicCompletedPopup",
            "prompt.unit_promotion": "UnitPromotionPopup",
        },
        direct_screen_ids=("world",),
        source=Path("lua/ingame/screens.lua"),
    ),
    catalog_version="test.1",
    catalog_content_hash="deadbeefcafe0000",
    catalog_root=Path("catalogs"),
    prompt_action_by_screen={
        "prompt.tech_civic_completed": "prompts.tech_civic_completed",
        "prompt.unit_promotion": "prompts.unit_promotion",
    },
)

FIXED_NOW = datetime(2026, 9, 21, 18, 0, tzinfo=UTC)


def _score(store: MatchTrackingStore, **kwargs: Any) -> Any:
    return compute_coverage(store, CLAIMED, now=FIXED_NOW, **kwargs)


def _action(scorecard: Any, declaration_id: str) -> Any:
    return next(row for row in scorecard.actions if row.declaration_id == declaration_id)


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------


def test_applied_actions_are_counted_with_their_first_and_last_evidence(
    store: SqliteMatchStore,
) -> None:
    scorecard = _score(store, run_ids=(RUN_ID,))
    found_city = _action(scorecard, "units.found_city")
    assert found_city.applied == 1
    assert found_city.demonstrated is True
    assert (found_city.first_applied.run_id, found_city.first_applied.turn) == (RUN_ID, 1)
    assert found_city.first_applied.step == 1
    assert found_city.last_applied == found_city.first_applied

    prompt = _action(scorecard, "prompts.tech_civic_completed")
    assert prompt.applied == 1 and prompt.prompt_responses == 1
    assert prompt.first_applied.turn == 2 and prompt.first_applied.attempt == 1


def test_refusals_are_grouped_by_reason_and_never_counted_as_demonstrations(
    store: SqliteMatchStore,
) -> None:
    scorecard = _score(store, run_ids=(RUN_ID,))
    found_city = _action(scorecard, "units.found_city")
    # The abandoned attempt's refusal is counted: it really ran on the client.
    assert found_city.rejected == 1
    assert dict(found_city.refusals_by_reason) == {"illegal_in_context": 1}

    end_turn = _action(scorecard, "turn.end_turn")
    assert end_turn.applied == 0 and end_turn.demonstrated is False
    assert dict(end_turn.refusals_by_reason) == {"verification_failed": 1}

    move = _action(scorecard, "units.move_to")
    assert dict(move.refusals_by_reason) == {"unavailable_to_human_now": 1}
    assert move.attempts == 1 and move.demonstrated is False


def test_headline_percentages_separate_applied_from_merely_attempted(
    store: SqliteMatchStore,
) -> None:
    scorecard = _score(store, run_ids=(RUN_ID,))
    headlines = {headline.label: headline for headline in scorecard.headlines()}

    applied = headlines["Actions demonstrated live"]
    assert (applied.demonstrated, applied.total) == (2, 5)
    assert applied.percent == pytest.approx(40.0)
    assert applied.summary == "Actions demonstrated live: 2 of 5 (40.0%)"

    attempted = headlines["Actions ever attempted"]
    assert (attempted.demonstrated, attempted.total) == (4, 5)

    assert headlines["Observations demonstrated live"].demonstrated == 2
    assert headlines["Views (capture targets) demonstrated"].demonstrated == 1
    assert headlines["Screens/prompts encountered"].demonstrated == 2
    assert headlines["Watched screen states seen open"].demonstrated == 1
    images = headlines["Images delivered to the agent"]
    assert (images.demonstrated, images.total) == (1, 5)


def test_the_never_demonstrated_lists_name_every_undemonstrated_id(
    store: SqliteMatchStore,
) -> None:
    never = _score(store, run_ids=(RUN_ID,)).never_demonstrated()
    assert never["actions"] == ("camera.move", "turn.end_turn", "units.move_to")
    assert never["actions_attempted_never_applied"] == ("turn.end_turn", "units.move_to")
    assert never["actions_never_attempted"] == ("camera.move",)
    assert never["observations"] == ("research.state", "units.state")
    assert never["views"] == ("views.city_screen",)
    assert never["screens"] == ("prompt.unit_promotion", "strategic")
    assert never["watched_states"] == ("CityPanel", "UnitPromotionPopup")


# --------------------------------------------------------------------------
# Observations, views, screens, images
# --------------------------------------------------------------------------


def test_an_observation_that_only_ever_produced_an_empty_value_is_not_demonstrated(
    store: SqliteMatchStore,
) -> None:
    scorecard = _score(store, run_ids=(RUN_ID,))
    by_id = {row.declaration_id: row for row in scorecard.observations}
    assert by_id["units.state"].steps_present == 5
    assert by_id["units.state"].steps_with_value == 0
    assert by_id["units.state"].demonstrated is False
    assert by_id["cities.state"].steps_with_value == 5
    assert by_id["cities.state"].first_with_value.step == 1
    assert by_id["research.state"].steps_present == 0


def test_captures_split_into_delivered_and_withheld_with_the_recorded_reason(
    store: SqliteMatchStore,
) -> None:
    scorecard = _score(store, run_ids=(RUN_ID,))
    world = next(row for row in scorecard.views if row.declaration_id == "views.world")
    assert (world.captures, world.delivered, world.withheld) == (2, 1, 1)
    assert dict(world.withheld_by_reason) == {"provenance_failure": 1}
    assert world.demonstrated is True

    images = scorecard.images
    assert images.steps == 5 and images.steps_with_image == 1 and images.images_sent == 1
    assert images.captures_recorded == 2
    assert images.captures_delivered == 1 and images.captures_withheld == 1
    assert dict(images.withheld_by_reason) == {"provenance_failure": 1}
    assert images.capture_failed_events == 1 and images.image_withheld_events == 1


def test_screens_and_watchlist_states_are_counted_from_the_record(
    store: SqliteMatchStore,
) -> None:
    scorecard = _score(store, run_ids=(RUN_ID,))
    screens = {row.screen_id: row for row in scorecard.screens}
    assert screens["world"].encountered_steps == 4
    assert screens["prompt.tech_civic_completed"].encountered_steps == 1
    assert screens["prompt.tech_civic_completed"].prompt_responses == 1
    assert screens["prompt.tech_civic_completed"].mapped_state == "TechCivicCompletedPopup"
    assert screens["prompt.unit_promotion"].demonstrated is False

    states = {row.state_name: row for row in scorecard.watched_states}
    assert states["TechCivicCompletedPopup"].observed_open == 1
    assert states["TechCivicCompletedPopup"].unknown_screen_events == 1
    assert states["CityPanel"].demonstrated is False


def test_a_screen_no_lua_state_maps_to_is_reported_as_unattestable(
    store: SqliteMatchStore,
) -> None:
    scorecard = _score(store, run_ids=(RUN_ID,))
    surfaces = {finding.surface: finding.reason for finding in scorecard.unattested}
    assert "screen strategic" in surfaces
    assert "CIVSIM_SCREEN_ID_BY_STATE" in surfaces["screen strategic"]
    # `world` has no state mapping either, but the probe answers it directly and the store
    # has 4 steps proving it -- it must never be called unattestable.
    assert "screen world" not in surfaces
    # A delivered capture exists, so the capture path is attested.
    assert "capture/image path" not in surfaces


def test_a_store_with_no_delivered_image_says_so_explicitly(tmp_path: Path) -> None:
    path = tmp_path / "withheld-only.db"
    writer = SqliteMatchStore(path)
    try:
        writer.create_run(
            make_run("run-w", "run-w-cfg", started_at=at(0)), make_config("run-w-cfg")
        )
        writer.write_save_point(make_save_point("run-w-sp1", "run-w", 1))
        writer.write_turn_cycle(
            _record(
                "run-w",
                1,
                0,
                [_bundle("run-w", "run-w-t1-a0", 1, action="units.move_to", outcome="applied")],
            )
        )
        capture, _ = make_capture(
            "cap-w", "run-w", 1, "run-w-t1-a0-step1", blob=None, withheld_reason="capture_failed"
        )
        writer.write_capture(capture, None)
    finally:
        writer.close()

    store = SqliteMatchStore(path, read_only=True)
    try:
        scorecard = compute_coverage(store, CLAIMED, now=FIXED_NOW)
    finally:
        store.close()
    surfaces = {finding.surface for finding in scorecard.unattested}
    assert "capture/image path" in surfaces
    assert "model spend" in surfaces  # no call reported a price


# --------------------------------------------------------------------------
# Per-run rows and run selection
# --------------------------------------------------------------------------


def test_the_per_run_table_carries_provider_turns_steps_calls_cost_and_gaps(
    store: SqliteMatchStore,
) -> None:
    scorecard = _score(store)
    assert [row.run_id for row in scorecard.runs] == [RUN_ID, OTHER_RUN_ID]
    row = scorecard.runs[0]
    assert row.models_served == ("openrouter/x",)
    assert row.turns_recorded == 2 and row.highest_turn == 2
    assert row.attempts == 3 and row.steps == 5 and row.model_calls == 5
    assert row.cost_usd == pytest.approx(0.01)
    assert row.record_completeness == "complete" and row.has_gaps is False
    assert row.actions_applied == 2
    assert row.unknown_screen_events == 1
    assert row.capture_path == "xcomposite"


def test_run_and_since_narrow_the_window(store: SqliteMatchStore) -> None:
    only_later = _score(store, run_ids=(OTHER_RUN_ID,))
    assert only_later.runs_in_scope == 1
    assert _action(only_later, "camera.move").applied == 1
    assert _action(only_later, "units.found_city").applied == 0

    since_later = _score(store, since_run_id=OTHER_RUN_ID)
    assert [row.run_id for row in since_later.runs] == [OTHER_RUN_ID]

    since_first = _score(store, since_run_id=RUN_ID)
    assert [row.run_id for row in since_first.runs] == [RUN_ID, OTHER_RUN_ID]


def test_an_unknown_run_id_is_refused_rather_than_silently_dropped(
    store: SqliteMatchStore,
) -> None:
    with pytest.raises(StoreReadError):
        _score(store, run_ids=("run-nope",))
    with pytest.raises(StoreReadError):
        _score(store, since_run_id="run-nope")


# --------------------------------------------------------------------------
# Read-only
# --------------------------------------------------------------------------


def test_coverage_computes_through_a_read_only_store_and_writes_nothing(
    store_path: Path,
) -> None:
    store = SqliteMatchStore(store_path, read_only=True)
    try:
        assert store.read_only is True
        scorecard = compute_coverage(store, CLAIMED, now=FIXED_NOW)
        assert scorecard.runs_in_scope == 2
        # Any write through this handle is refused, so nothing computed above could have written.
        with pytest.raises(StoreWriteError):
            store.write_save_point(make_save_point("nope", RUN_ID, 9))
    finally:
        store.close()


def test_the_cli_opens_the_store_read_only(
    store_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from civsim_harness.operator import store_cli

    seen: list[bool] = []
    original = store_cli._open

    def spy(path: Path, *, read_only: bool) -> SqliteMatchStore:
        seen.append(read_only)
        return original(path, read_only=read_only)

    monkeypatch.setattr(store_cli, "_open", spy)
    result = runner.invoke(
        app, ["store", "coverage", "--store", str(store_path), "--catalog-root", "catalogs"]
    )
    assert result.exit_code == 0, result.output
    assert seen == [True]


# --------------------------------------------------------------------------
# Rendering and the CLI
# --------------------------------------------------------------------------


def test_markdown_carries_the_headlines_evidence_and_never_demonstrated_list(
    store: SqliteMatchStore,
) -> None:
    text = render_markdown(_score(store, run_ids=(RUN_ID,)))
    assert "# CivSim live coverage scorecard" in text
    assert "**Actions demonstrated live: 2 of 5 (40.0%)**" in text
    assert "| `units.found_city` | 1 | 1 |" in text
    assert f"{RUN_ID} t1/s1" in text
    assert "unavailable_to_human_now x1" in text
    assert "**Actions never even attempted (1)**: `camera.move`" in text
    assert "## What the store cannot attest to" in text


def test_json_carries_the_same_numbers_as_the_markdown(store: SqliteMatchStore) -> None:
    scorecard = _score(store, run_ids=(RUN_ID,))
    payload = render_json(scorecard)
    assert payload["catalog_version"] == "test.1"
    headlines = {row["label"]: row for row in payload["headlines"]}
    assert headlines["Actions demonstrated live"]["demonstrated"] == 2
    assert headlines["Actions demonstrated live"]["total"] == 5
    assert headlines["Actions demonstrated live"]["percent"] == 40.0
    found_city = next(
        row for row in payload["actions"] if row["declaration_id"] == "units.found_city"
    )
    assert found_city["first_applied"] == {
        "run_id": RUN_ID,
        "turn": 1,
        "step": 1,
        "attempt": 0,
    }
    assert payload["never_demonstrated"]["views"] == ["views.city_screen"]
    assert payload["images"]["captures_withheld"] == 1
    # Serialisable as it stands -- this is what the CLI emits.
    assert json.loads(json.dumps(payload))["runs_in_scope"] == 1


def test_the_cli_emits_markdown_by_default_and_json_on_request(store_path: Path) -> None:
    md = runner.invoke(app, ["store", "coverage", "--store", str(store_path)])
    assert md.exit_code == 0, md.output
    assert "# CivSim live coverage scorecard" in md.stdout
    assert "Actions demonstrated live:" in md.stdout

    js = runner.invoke(
        app, ["store", "coverage", "--store", str(store_path), "--format", "json"]
    )
    assert js.exit_code == 0, js.output
    payload = json.loads(js.stdout)
    assert payload["runs_in_scope"] == 2
    assert any(row["declaration_id"] == "units.found_city" for row in payload["actions"])


def test_the_cli_refuses_a_bad_format_and_an_unknown_run(store_path: Path) -> None:
    bad_format = runner.invoke(
        app, ["store", "coverage", "--store", str(store_path), "--format", "csv"]
    )
    assert bad_format.exit_code == 1

    bad_run = runner.invoke(
        app, ["store", "coverage", "--store", str(store_path), "--run", "run-nope"]
    )
    assert bad_run.exit_code == 2


def test_the_cli_run_filter_narrows_the_scorecard(store_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "store",
            "coverage",
            "--store",
            str(store_path),
            "--run",
            OTHER_RUN_ID,
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["runs_in_scope"] == 1
    assert payload["runs"][0]["run_id"] == OTHER_RUN_ID


# --------------------------------------------------------------------------
# The claimed surface, loaded the way production loads it
# --------------------------------------------------------------------------


def test_the_screen_surface_is_read_from_the_real_screens_lua() -> None:
    surface = load_screen_surface(REPO_ROOT / "lua" / "ingame" / "screens.lua")
    assert surface.source is not None
    # T253's mapping is what turns an open `TechCivicCompletedPopup` into a catalog prompt id.
    assert surface.state_by_screen_id["prompt.tech_civic_completed"] == "TechCivicCompletedPopup"
    assert surface.state_by_screen_id["prompt.boost_unlocked"] == "BoostUnlockedPopup"
    assert "TechCivicCompletedPopup" in surface.watched_states
    assert "CityPanel" in surface.watched_states
    # The plain world view's id is the literal the probe returns, not a mapped state.
    assert "world" in surface.direct_screen_ids
    assert "world" in surface.screen_ids
    assert "unknown" not in surface.screen_ids


def test_the_probes_world_view_id_is_the_one_the_catalog_vocabulary_declares() -> None:
    """The probe used to answer `world_view`, a name no catalog file ever used;
    `catalogs/README.md`'s `game.current_screen` vocabulary and the Lua's own
    CIVSIM_KNOWN_SCREENS both say `world`. One name, and it is the catalog's."""
    screens_lua = REPO_ROOT / "lua" / "ingame" / "screens.lua"
    surface = load_screen_surface(screens_lua)
    assert "world" in surface.direct_screen_ids
    assert '"world_view"' not in screens_lua.read_text(encoding="utf-8")
    assert "world_view" not in (REPO_ROOT / "catalogs" / "README.md").read_text(encoding="utf-8")


def test_the_diplomatic_approach_is_reachable_without_a_state_mapping() -> None:
    """MEASURED 2026-09-21 (block 7, turn 35): a leader statement is a MODE of DiplomacyActionView,
    not a state of its own, so the probe answers the id directly after reading the conversation
    controls. It must stay out of the state map -- mapping it there would report a blocking prompt
    for an ordinary open diplomacy screen -- while still being an id coverage can attest."""
    surface = load_screen_surface(REPO_ROOT / "lua" / "ingame" / "screens.lua")
    assert "prompt.diplomatic_approach" in surface.direct_screen_ids
    assert "prompt.diplomatic_approach" not in surface.state_by_screen_id
    assert surface.state_by_screen_id["diplomacy"] == "DiplomacyActionView"


def test_the_great_work_showcase_is_mapped_and_watched() -> None:
    """MEASURED 2026-09-21 (block 3, turn 27): the relic showcase blocked play while the probe
    reported the world view. UNVERIFIED LIVE that the state reports open for it."""
    surface = load_screen_surface(REPO_ROOT / "lua" / "ingame" / "screens.lua")
    assert surface.state_by_screen_id["prompt.great_work_created"] == "GreatWorkShowcase"
    assert "GreatWorkShowcase" in surface.watched_states


@pytest.mark.parametrize(
    "screen_id",
    [
        "strategic",
        "prompt.religion_selection",
        "prompt.congress_vote",
        "prompt.city_state_quest",
    ],
)
def test_the_documented_unmappable_screen_ids_stay_unmapped(screen_id: str) -> None:
    """None of these has a UI state of its own in the shipped Civ VI UI -- see
    `specs/002-civ-playing-harness/spikes/screens-unmapped-2026-09-21.md`. `civsim store coverage`
    flagging them is correct, and a future mapping here would be a fabrication, so the gap is
    pinned rather than papered over."""
    surface = load_screen_surface(REPO_ROOT / "lua" / "ingame" / "screens.lua")
    assert screen_id in surface.screen_ids, "still a claimed id, so the coverage flag is honest"
    assert screen_id not in surface.state_by_screen_id
    assert screen_id not in surface.direct_screen_ids


def test_a_missing_screens_lua_yields_an_empty_surface_not_an_error(tmp_path: Path) -> None:
    surface = load_screen_surface(tmp_path / "absent.lua")
    assert surface.source is None and surface.screen_ids == ()


def test_the_claimed_surface_loads_through_the_real_catalog_loader(
    store: SqliteMatchStore,
) -> None:
    claimed = load_claimed_surface(REPO_ROOT / "catalogs")
    assert len(claimed.action_ids) >= 36
    assert "units.found_city" in claimed.action_ids
    assert "game.screen_state" in claimed.observation_ids
    assert "views.world" in claimed.view_ids
    # The screen -> action binding comes from the predicate text, so the one prompt whose
    # naming breaks the `prompt.x`/`prompts.x` convention still resolves.
    assert (
        claimed.prompt_action_by_screen["prompt.diplomatic_approach"]
        == "prompts.ai_diplomatic_approach"
    )
    scorecard = compute_coverage(store, claimed, now=FIXED_NOW, run_ids=(RUN_ID,))
    applied = next(
        headline
        for headline in scorecard.headlines()
        if headline.label == "Actions demonstrated live"
    )
    assert applied.demonstrated == 2 and applied.total == len(claimed.action_ids)
