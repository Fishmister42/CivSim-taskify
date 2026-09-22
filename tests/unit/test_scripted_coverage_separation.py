"""A scripted landing must never be counted as an agent's chosen landing (T326).

**The guardrail this file is the control for.** ``provider/scripted.py`` executes a declared
action sequence through the harness's own dispatch path. That proves the *action* works end to
end; it says nothing about whether an agent would have chosen it. The breadth/coverage
scorecard (``civsim store coverage``) only ever meant the second claim, so a scripted landing
entering ``ActionCoverage.applied`` -- or the "Actions demonstrated live" headline -- would be a
record **better than the truth**, which is this project's named worst case: under-reporting is a
measurement problem, fabricating is a credibility problem that poisons the sound claims too.

**The positive twin varies the one dimension the rule constrains.** Both runs below are built by
the *same* fixture function with the *same* action list, the *same* outcomes, the *same*
observations and the *same* turn shape. The only thing that differs between them is
``ModelCall.model_served.provider`` -- the provenance axis itself. A twin that varied the action
id, the turn number or the store path would pass no matter how the split were implemented, which
is exactly the failure mode that took the suite down on 2026-09-22 (a validator whose positive
case varied the path shape and never the capability kind).

**Failed first, on purpose.** Before ``store/coverage.py`` grew its tier split, the first
assertion in :func:`test_a_scripted_landing_is_not_counted_as_a_chosen_landing` read
``applied == 1`` against an expected ``0`` -- the scripted landing was counted in the chosen
total, exactly as the guardrail says it must not be.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from civsim_harness.models.decision import Decision
from civsim_harness.models.provenance import (
    SCRIPTED_MODEL_REF,
    SCRIPTED_PROVIDER_NAME,
    DecisionProvenance,
    provenance_of,
)
from civsim_harness.models.records import ModelCall
from civsim_harness.models.turn import DecisionStep, Observation
from civsim_harness.store.coverage import (
    ClaimedSurface,
    ScreenSurface,
    compute_coverage,
    render_json,
    render_markdown,
)
from civsim_harness.store.port import DecisionStepBundle, TurnCycleRecord
from civsim_harness.store.sqlite_adapter import SqliteMatchStore
from store_support.builders import (
    NOW,
    at,
    catalog_ref,
    make_config,
    make_run,
    make_save_point,
    make_turn_cycle,
)

#: The two catalog actions the immediate use case needs, and the end turn behind them. Held as a
#: tiny explicit surface so these assertions stay stable as the real catalog grows.
CLAIMED = ClaimedSurface(
    action_ids=("cities.select", "cities.set_production", "turn.end_turn"),
    observation_ids=("cities.state",),
    view_ids=(),
    screens=ScreenSurface(),
    catalog_version="test",
    catalog_content_hash="deadbeefdeadbeef",
    catalog_root=Path("catalogs"),
)

AGENT_MODEL_SERVED: dict[str, str] = {"provider": "openrouter", "model": "x"}
SCRIPTED_MODEL_SERVED: dict[str, str] = {
    "provider": SCRIPTED_MODEL_REF.provider,
    "model": SCRIPTED_MODEL_REF.model,
}


# --------------------------------------------------------------------------
# One fixture shape, parameterised on provenance and nothing else
# --------------------------------------------------------------------------


def _bundle(
    run_id: str,
    turn_cycle_id: str,
    step_index: int,
    *,
    action: str,
    served: dict[str, str],
    outcome: str = "applied",
    rejection_reason: str | None = None,
) -> DecisionStepBundle:
    step_id = f"{turn_cycle_id}-step{step_index}"
    call_id = f"{step_id}-call"
    execution: dict[str, Any] = {
        "outcome": outcome,
        "verification": {"declaration_id": action, "result": outcome == "applied"},
        "verified_at": NOW,
    }
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
                "visually_degraded": True,
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
                "entries": [
                    {
                        "declaration_id": "cities.state",
                        "key": "cities",
                        "value": {"cities": [{"id": 1}]},
                        "context": "GameCore_Tuner",
                    }
                ],
                "captures": [],
                "screen_identity": "world",
            }
        ),
        decision=Decision.model_validate(
            {
                "decision_id": f"{step_id}-dec",
                "decision_step_id": step_id,
                "action_declaration_id": action,
                "parameters": {},
                "reasoning": "fixture",
                "trigger": "proactive",
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
                "model_requested": AGENT_MODEL_SERVED,
                "model_served": served,
                "latency_ms": 10,
                "cost": {},
                "retry_count": 0,
                "fallback_occurred": False,
                "image_count": 0,
                "outcome": "decision_returned",
            }
        ),
    )


def _write_run(
    store: SqliteMatchStore,
    run_id: str,
    *,
    served: dict[str, str],
    started_minutes: int,
    actions: Sequence[tuple[str, str, str | None]],
    is_authoritative: bool = True,
) -> None:
    """One run whose every step was served by *served*.

    *actions* is ``(declaration_id, outcome, rejection_reason)`` per step. Everything else about
    the two runs this module builds is identical, so the only variable under test is *served*.
    """
    store.create_run(
        make_run(run_id, f"{run_id}-cfg", started_at=at(started_minutes)),
        make_config(f"{run_id}-cfg"),
    )
    store.write_save_point(make_save_point(f"{run_id}-sp1", run_id, 1))
    turn_cycle_id = f"{run_id}-t1-a0"
    bundles = [
        _bundle(
            run_id,
            turn_cycle_id,
            index,
            action=action,
            served=served,
            outcome=outcome,
            rejection_reason=reason,
        )
        for index, (action, outcome, reason) in enumerate(actions, start=1)
    ]
    store.write_turn_cycle(
        TurnCycleRecord(
            turn_cycle=make_turn_cycle(
                turn_cycle_id,
                run_id,
                1,
                0,
                is_authoritative=is_authoritative,
                step_count=len(bundles),
                save_point_id=f"{run_id}-sp1",
                outcome="ended_by_agent",
            ),
            steps=bundles,
        )
    )


#: The same three steps in both runs: one landing, one refusal, one end turn.
STEPS: tuple[tuple[str, str, str | None], ...] = (
    ("cities.select", "applied", None),
    ("cities.set_production", "rejected", "unavailable_to_human_now"),
    ("turn.end_turn", "applied", None),
)

AGENT_RUN = "run-agent"
SCRIPTED_RUN = "run-scripted"


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    path = tmp_path / "separation.db"
    store = SqliteMatchStore(path)
    try:
        _write_run(store, AGENT_RUN, served=AGENT_MODEL_SERVED, started_minutes=0, actions=STEPS)
        _write_run(
            store, SCRIPTED_RUN, served=SCRIPTED_MODEL_SERVED, started_minutes=10, actions=STEPS
        )
    finally:
        store.close()
    return path


def _scorecard(path: Path, **kwargs: Any) -> Any:
    store = SqliteMatchStore(path, read_only=True)
    try:
        return compute_coverage(store, CLAIMED, store_path=str(path), **kwargs)
    finally:
        store.close()


# --------------------------------------------------------------------------
# The guardrail
# --------------------------------------------------------------------------


def test_a_scripted_landing_is_not_counted_as_a_chosen_landing(store_path: Path) -> None:
    """The failing-first assertion: a scripted `applied` must not reach the chosen total.

    Scoped to the scripted run alone so that nothing an agent did can mask the result. Before
    the tier split existed this read ``applied == 1``.
    """
    scorecard = _scorecard(store_path, run_ids=[SCRIPTED_RUN])
    chosen = {row.declaration_id: row for row in scorecard.actions}

    assert chosen["cities.select"].applied == 0
    assert chosen["turn.end_turn"].applied == 0
    assert chosen["cities.select"].attempts == 0
    assert chosen["cities.set_production"].attempts == 0
    assert not any(row.demonstrated for row in scorecard.actions)

    headline = next(
        line for line in scorecard.headlines() if line.label == "Actions demonstrated live"
    )
    assert headline.demonstrated == 0
    attempted = next(
        line for line in scorecard.headlines() if line.label == "Actions ever attempted"
    )
    assert attempted.demonstrated == 0


def test_the_positive_twin_varies_only_provenance(store_path: Path) -> None:
    """The identical fixture, served by an agent, IS counted -- so the split is not vacuous.

    Everything about this run matches the scripted one except ``model_served.provider``. If this
    fails, the test above proves nothing: it would pass against a scorecard that had simply
    stopped counting anything at all.
    """
    scorecard = _scorecard(store_path, run_ids=[AGENT_RUN])
    chosen = {row.declaration_id: row for row in scorecard.actions}

    assert chosen["cities.select"].applied == 1
    assert chosen["cities.select"].demonstrated is True
    assert chosen["turn.end_turn"].applied == 1
    assert chosen["cities.set_production"].attempts == 1
    assert chosen["cities.set_production"].drawn_while_unavailable == 1

    headline = next(
        line for line in scorecard.headlines() if line.label == "Actions demonstrated live"
    )
    assert headline.demonstrated == 2


def test_a_scripted_landing_is_recorded_in_its_own_tier(store_path: Path) -> None:
    """Excluded from the chosen total, but never erased.

    "Absence and unobservability must not share a representation": a scripted landing that
    happened must be distinguishable from one that never ran, so it is reported -- with its
    evidence address -- in a tier of its own.
    """
    scorecard = _scorecard(store_path, run_ids=[SCRIPTED_RUN])
    scripted = {row.declaration_id: row for row in scorecard.scripted.actions}

    assert scripted["cities.select"].landings == 1
    assert scripted["cities.select"].first_landed is not None
    assert scripted["cities.select"].first_landed.run_id == SCRIPTED_RUN
    assert scripted["cities.set_production"].landings == 0
    assert scripted["cities.set_production"].refusals == 1
    assert scripted["cities.set_production"].refusals_by_reason == {"unavailable_to_human_now": 1}
    assert scorecard.scripted.steps == 3
    assert scorecard.scripted.run_ids == (SCRIPTED_RUN,)


def test_a_scripted_row_cannot_be_added_to_the_chosen_total(store_path: Path) -> None:
    """The separation is structural: a scripted row has no field a chosen total reads.

    Not a filter at the boundary -- a scripted row is a different type with no ``applied``,
    no ``attempts`` and no ``demonstrated``, so the ordinary ways of summing a coverage total
    (``sum(row.applied ...)``, ``sum(1 for row in ... if row.demonstrated)``) raise rather than
    quietly returning a number that mixes the tiers. Someone adding a new count next month, who
    has never heard of scripted runs, gets a correct number by construction.
    """
    scorecard = _scorecard(store_path)
    row = scorecard.scripted.actions[0]

    for forbidden in ("applied", "attempts", "demonstrated", "attempts_while_available"):
        assert not hasattr(row, forbidden), (
            f"ScriptedActionCoverage.{forbidden} exists -- a scripted landing could then be "
            "summed into a chosen total by a caller that never knew the tiers were different"
        )
    assert all(type(each).__name__ == "ActionCoverage" for each in scorecard.actions)


def test_a_mixed_store_keeps_the_two_tiers_apart(store_path: Path) -> None:
    """Both runs in scope: the chosen total counts only the agent's, the scripted tier only the
    script's, and the per-run rows say which was which."""
    scorecard = _scorecard(store_path)
    chosen = {row.declaration_id: row for row in scorecard.actions}
    scripted = {row.declaration_id: row for row in scorecard.scripted.actions}

    assert chosen["cities.select"].applied == 1
    assert scripted["cities.select"].landings == 1

    runs = {row.run_id: row for row in scorecard.runs}
    assert runs[AGENT_RUN].actions_applied == 2
    assert runs[AGENT_RUN].scripted_landings == 0
    assert runs[SCRIPTED_RUN].actions_applied == 0
    assert runs[SCRIPTED_RUN].scripted_landings == 2
    assert runs[SCRIPTED_RUN].decision_provenance == (DecisionProvenance.SCRIPTED.value,)
    assert runs[AGENT_RUN].decision_provenance == (DecisionProvenance.AGENT_CHOSEN.value,)


def test_the_rendered_reports_never_present_a_scripted_landing_as_demonstrated(
    store_path: Path,
) -> None:
    """Both rendered surfaces carry the tier, and the JSON keeps them in separate keys."""
    scorecard = _scorecard(store_path)

    payload = render_json(scorecard)
    assert payload["scripted"]["steps"] == 3
    assert payload["scripted"]["run_ids"] == [SCRIPTED_RUN]
    demonstrated = next(
        line for line in payload["headlines"] if line["label"] == "Actions demonstrated live"
    )
    assert demonstrated["demonstrated"] == 2  # the agent run's two, never the script's

    markdown = render_markdown(scorecard)
    assert "Scripted (capability test)" in markdown
    assert SCRIPTED_RUN in markdown


# --------------------------------------------------------------------------
# The field the whole separation rests on
# --------------------------------------------------------------------------


def test_a_clean_scripted_run_carries_no_replay_caveat(store_path: Path) -> None:
    scorecard = _scorecard(store_path, run_ids=[SCRIPTED_RUN])
    assert scorecard.scripted.steps_in_abandoned_attempts == 0
    assert scorecard.scripted.replay_caveat() == ""
    assert "CAVEAT" not in render_markdown(scorecard)


def test_a_replayed_scripted_run_says_its_chain_may_have_been_split(tmp_path: Path) -> None:
    """A script's chain only holds while its steps run in order against one continuous board.

    The cursor does not rewind, so an abandoned-and-replayed attempt can leave a dependent step
    issued after its setup went to a discarded attempt. A refusal there is a replay artefact, not
    a finding about the harness, and without this the two arrive as the same record. The store
    knows which attempts were abandoned; the provider does not, and does not guess.
    """
    path = tmp_path / "replayed.db"
    store = SqliteMatchStore(path)
    try:
        _write_run(
            store,
            SCRIPTED_RUN,
            served=SCRIPTED_MODEL_SERVED,
            started_minutes=0,
            actions=STEPS,
            is_authoritative=False,
        )
    finally:
        store.close()

    scorecard = _scorecard(path)
    assert scorecard.scripted.steps == 3
    assert scorecard.scripted.steps_in_abandoned_attempts == 3
    caveat = scorecard.scripted.replay_caveat()
    assert "abandoned and replayed" in caveat
    assert "replay artefact rather than a finding" in caveat
    assert caveat in scorecard.scripted.headline()
    assert "CAVEAT" in render_markdown(scorecard)
    assert render_json(scorecard)["scripted"]["steps_in_abandoned_attempts"] == 3
    assert render_json(scorecard)["scripted"]["replay_caveat"] == caveat


def test_the_provider_identity_survives_the_store_round_trip(store_path: Path) -> None:
    """`model_served.provider` is what classifies a step, so prove it is durable, not merely
    present in memory: read it back out of sqlite through the ordinary contract."""
    store = SqliteMatchStore(store_path, read_only=True)
    try:
        record = store.get_turn_cycle_attempt(SCRIPTED_RUN, 1, 0)
        assert record is not None
        assert [bundle.model_call.model_served.provider for bundle in record.steps] == [
            SCRIPTED_PROVIDER_NAME
        ] * 3
        assert all(
            provenance_of(bundle.model_call) is DecisionProvenance.SCRIPTED
            for bundle in record.steps
        )
        calls = store.list_model_calls(SCRIPTED_RUN)
        assert calls, "the model_calls table must carry the scripted identity too"
        assert {row.call.model_served.provider for row in calls} == {SCRIPTED_PROVIDER_NAME}
    finally:
        store.close()


def test_provenance_is_total_over_every_model_ref() -> None:
    """No third value, so no caller has an "unknown" branch to resolve in the wrong direction."""
    from civsim_harness.models.common import ModelRef
    from civsim_harness.models.provenance import provenance_of_model_ref

    assert (
        provenance_of_model_ref(ModelRef(provider="openrouter", model="anthropic/x"))
        is DecisionProvenance.AGENT_CHOSEN
    )
    assert (
        provenance_of_model_ref(ModelRef(provider="stochastic", model="coverage-v1"))
        is DecisionProvenance.AGENT_CHOSEN
    )
    assert (
        provenance_of_model_ref(SCRIPTED_MODEL_REF) is DecisionProvenance.SCRIPTED
    )
    assert len(list(DecisionProvenance)) == 2
