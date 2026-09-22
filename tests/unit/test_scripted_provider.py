"""The scripted decision provider: load-time validation, dispatch, ledger, identity (T326).

`provider/scripted.py` executes a fixed, declared action sequence through the harness's ordinary
dispatch path. It is a **harness capability test** -- it proves an action chain works end to end
-- and every claim it can make is scoped by the fact that the choice came from outside. The
coverage separation that keeps its landings out of the agent-chosen scorecard is asserted
separately, in `tests/unit/test_scripted_coverage_separation.py`; this file asserts the adapter.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "tests") not in sys.path:  # pragma: no cover - import-path setup
    sys.path.insert(0, str(REPO / "tests"))

from civsim_harness.agent import context as agent_context  # noqa: E402
from civsim_harness.capability.loader import load_catalog  # noqa: E402
from civsim_harness.models.common import DeclarationId, ModelRef  # noqa: E402
from civsim_harness.models.provenance import (  # noqa: E402
    SCRIPTED_PROVIDER_NAME,
    DecisionProvenance,
    provenance_of_model_ref,
)
from civsim_harness.models.records import CallOutcome  # noqa: E402
from civsim_harness.provider import scripted as scripted_module  # noqa: E402
from civsim_harness.provider.port import DecisionRequest  # noqa: E402
from civsim_harness.provider.scripted import (  # noqa: E402
    ActionScript,
    ScriptedModelProvider,
    ScriptStep,
    ScriptStepStatus,
    ScriptValidationError,
    UnavailablePolicy,
    load_action_script,
)
from civsim_harness.provider.stochastic import END_TURN_DECLARATION_ID  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG_ROOT = REPO_ROOT / "catalogs"
LIVE_SCRIPT = REPO_ROOT / "tests" / "live" / "scripts" / "builder_from_capital.yaml"


@pytest.fixture(scope="module")
def catalog() -> Any:
    return load_catalog(CATALOG_ROOT)


# --------------------------------------------------------------------------
# Building a request the way agent/context.py renders one
# --------------------------------------------------------------------------


def observation_text(available: list[str], unavailable: list[tuple[str, str]]) -> str:
    """A hand-built observation in `agent/context.py`'s grammar, for the focused unit tests.

    The grammar itself is pinned against the production renderer's real output in
    :func:`test_the_parser_reads_the_production_renderers_own_output`, so this shortcut can
    never drift away from what the harness actually writes without something failing.
    """
    lines = [
        '- [InGame] game.screen_state: {"screen": "world", "has_blocking_prompt": false}',
        "Actions you may take (choose exactly one per step by its id):",
        agent_context.AVAILABLE_GROUP_HEADER,
    ]
    lines.extend(f"- {action}: does a thing" for action in available)
    lines.append(agent_context.UNAVAILABLE_GROUP_HEADER)
    lines.extend(
        f"- {action}: does a thing{agent_context.UNAVAILABLE_REASON_MARKER}{reason}"
        for action, reason in unavailable
    )
    return "\n".join(lines)


def request_for(
    available: list[str], unavailable: list[tuple[str, str]] | None = None, *, step_index: int = 1
) -> DecisionRequest:
    return DecisionRequest(
        model=ModelRef(provider="openrouter", model="anthropic/x"),
        system="role",
        observation=observation_text(available, unavailable or []),
        images=[],
        step_index=step_index,
        response_schema={},
    )


def a_script(
    steps: list[ScriptStep],
    *,
    policy: UnavailablePolicy = UnavailablePolicy.HALT,
    attempts: int = 1,
) -> ActionScript:
    return ActionScript(
        script_id="t",
        steps=tuple(steps),
        on_unavailable=policy,
        attempts_per_step=attempts,
    )


SELECT = ScriptStep(DeclarationId("cities.select"), {"target": 65536}, note="open the panel")
PRODUCE = ScriptStep(DeclarationId("cities.set_production"), {"target": "UNIT_BUILDER"})
END_TURN = ScriptStep(END_TURN_DECLARATION_ID, {})


# --------------------------------------------------------------------------
# The rendered-catalog grammar this adapter reads
# --------------------------------------------------------------------------


def test_the_group_headers_are_pinned_to_the_renderer_that_writes_them() -> None:
    """A rename in `agent/context.py` must not silently make every step look unavailable.

    This adapter copies the four header/marker spellings as text rather than importing them
    (`agent/context.py` reaches the catalog loader). The copy is only safe while something fails
    when the two drift -- and the drift would fail SILENTLY and in the fail-safe direction: every
    action would read as "not listed", every step would be refused, and the policy would halt
    every script while looking like an honest board reading.
    """
    assert scripted_module._AVAILABLE_GROUP_HEADER in agent_context.AVAILABLE_GROUP_HEADER
    assert scripted_module._UNAVAILABLE_GROUP_HEADER in agent_context.UNAVAILABLE_GROUP_HEADER
    assert scripted_module._UNAVAILABLE_REASON_MARKER == agent_context.UNAVAILABLE_REASON_MARKER
    rendered = observation_text(["a.b"], [("c.d", "no city selected")])
    assert scripted_module._ACTION_SECTION_HEADER in rendered
    # Positive control for the parser itself: run it against text KNOWN to contain both groups.
    parsed = scripted_module._listed_actions(rendered)
    assert parsed == {"a.b": (True, ""), "c.d": (False, "no city selected")}


def test_the_parser_reads_the_production_renderers_own_output(catalog: Any) -> None:
    """The pin that actually binds: render the REAL catalog through the REAL renderer and read
    it back with this adapter's parser.

    Comparing constants only proves the two spellings agree today; it does not prove the parser
    survives a change to how the groups are laid out -- a blank line inserted, a bullet reshaped,
    the reason moved. This drives `agent.context.assemble_action_catalog_text` with a real
    observation and asserts the parser recovers **exactly** the catalog's action ids, with both
    groups non-empty. The failure mode it guards is silent and fail-safe: if the parser stopped
    seeing the groups, every scripted step would read as "not listed", every step would be
    refused, and a halted script would look like an honest reading of an empty board.
    """
    from store_support.builders import make_step_bundle

    entries = [
        {
            "declaration_id": "cities.state",
            "key": "cities",
            "value": {
                "cities": [
                    {
                        "city_id": 65536,
                        "owner_is_local_player": True,
                        "is_selected": False,
                        "available_productions": ["UNIT_BUILDER"],
                        "production_queue": [],
                    }
                ]
            },
            "context": "GameCore_Tuner",
        },
        {
            "declaration_id": "game.screen_state",
            "key": "game.screen_state",
            "value": {
                "screen": "world",
                "has_blocking_prompt": False,
                "is_local_player_turn": True,
            },
            "context": "InGame",
        },
    ]
    bundle = make_step_bundle("r", "tc", 1, entries=entries)
    rendered = agent_context.assemble_action_catalog_text(
        catalog.declarations.values(), observation=bundle.observation
    )

    parsed = scripted_module._listed_actions(rendered)
    catalog_action_ids = {
        str(declaration.declaration_id)
        for declaration in catalog.declarations.values()
        if declaration.kind.value == "action"
    }
    assert parsed.keys() == catalog_action_ids
    assert any(available for available, _ in parsed.values()), "the available group went unread"
    assert any(
        not available for available, _ in parsed.values()
    ), "the greyed-out group went unread"
    # A greyed-out action carries the tooltip's own reason through to the ledger, verbatim.
    assert parsed["cities.set_production"] == (False, "no city selected")
    assert parsed["turn.end_turn"][0] is True


# --------------------------------------------------------------------------
# A script cannot be constructed without steps
# --------------------------------------------------------------------------


def test_a_script_cannot_be_empty() -> None:
    with pytest.raises(ScriptValidationError, match="at least one step"):
        a_script([])


def test_a_provider_cannot_be_constructed_without_a_script() -> None:
    """The shape this project keeps being bitten by, closed at the type.

    There is no `script=None` default, so the failure is a `TypeError` at the call, not a
    provider that quietly ends every turn.
    """
    with pytest.raises(TypeError):
        ScriptedModelProvider()  # type: ignore[call-arg]


def test_attempts_per_step_is_refused_where_it_means_nothing() -> None:
    with pytest.raises(ScriptValidationError, match="only meaningful for on_unavailable: retry"):
        a_script([END_TURN], policy=UnavailablePolicy.HALT, attempts=3)
    with pytest.raises(ScriptValidationError, match="at least 1"):
        a_script([END_TURN], policy=UnavailablePolicy.RETRY, attempts=0)


# --------------------------------------------------------------------------
# Load-time validation against the catalog
# --------------------------------------------------------------------------


def write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "script.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_an_unknown_declaration_id_fails_at_load(tmp_path: Path, catalog: Any) -> None:
    path = write(
        tmp_path,
        "script_id: s\non_unavailable: halt\nsteps:\n  - declaration_id: cities.no_such_order\n",
    )
    with pytest.raises(ScriptValidationError, match="unknown declaration_id"):
        load_action_script(path, catalog=catalog)


def test_an_observation_declaration_is_not_an_action(tmp_path: Path, catalog: Any) -> None:
    path = write(
        tmp_path,
        "script_id: s\non_unavailable: halt\nsteps:\n  - declaration_id: cities.state\n",
    )
    with pytest.raises(ScriptValidationError, match="not an action"):
        load_action_script(path, catalog=catalog)


def test_a_target_of_the_wrong_shape_fails_at_load(tmp_path: Path, catalog: Any) -> None:
    """`cities.select` declares target_kind city_id -- an integer. A string is not one."""
    path = write(
        tmp_path,
        "script_id: s\non_unavailable: halt\nsteps:\n"
        "  - declaration_id: cities.select\n    parameters:\n      target: Pasargadae\n",
    )
    with pytest.raises(ScriptValidationError, match="target_kind city_id"):
        load_action_script(path, catalog=catalog)


def test_a_missing_target_fails_at_load(tmp_path: Path, catalog: Any) -> None:
    path = write(
        tmp_path,
        "script_id: s\non_unavailable: halt\nsteps:\n  - declaration_id: cities.select\n",
    )
    with pytest.raises(ScriptValidationError, match="requires a target"):
        load_action_script(path, catalog=catalog)


def test_a_target_on_a_targetless_action_fails_at_load(tmp_path: Path, catalog: Any) -> None:
    path = write(
        tmp_path,
        "script_id: s\non_unavailable: halt\nsteps:\n"
        "  - declaration_id: turn.end_turn\n    parameters:\n      target: 3\n",
    )
    with pytest.raises(ScriptValidationError, match="takes no target"):
        load_action_script(path, catalog=catalog)


def test_an_unexpected_parameter_key_fails_at_load(tmp_path: Path, catalog: Any) -> None:
    path = write(
        tmp_path,
        "script_id: s\non_unavailable: halt\nsteps:\n"
        "  - declaration_id: turn.end_turn\n    parameters:\n      city: 3\n",
    )
    with pytest.raises(ScriptValidationError, match="only carry 'target'"):
        load_action_script(path, catalog=catalog)


def test_a_missing_policy_fails_at_load(tmp_path: Path, catalog: Any) -> None:
    path = write(tmp_path, "script_id: s\nsteps:\n  - declaration_id: turn.end_turn\n")
    with pytest.raises(ScriptValidationError, match="must declare on_unavailable"):
        load_action_script(path, catalog=catalog)


def test_an_unknown_policy_fails_at_load(tmp_path: Path, catalog: Any) -> None:
    path = write(
        tmp_path,
        "script_id: s\non_unavailable: carry_on\nsteps:\n  - declaration_id: turn.end_turn\n",
    )
    with pytest.raises(ScriptValidationError, match="unknown on_unavailable policy"):
        load_action_script(path, catalog=catalog)


def test_retry_without_a_declared_bound_fails_at_load(tmp_path: Path, catalog: Any) -> None:
    path = write(
        tmp_path,
        "script_id: s\non_unavailable: retry\nsteps:\n  - declaration_id: turn.end_turn\n",
    )
    with pytest.raises(ScriptValidationError, match="must declare attempts_per_step"):
        load_action_script(path, catalog=catalog)


def test_an_empty_step_list_fails_at_load(tmp_path: Path, catalog: Any) -> None:
    path = write(tmp_path, "script_id: s\non_unavailable: halt\nsteps: []\n")
    with pytest.raises(ScriptValidationError, match="at least one step"):
        load_action_script(path, catalog=catalog)


def test_a_missing_file_fails_at_load(tmp_path: Path, catalog: Any) -> None:
    with pytest.raises(ScriptValidationError, match="could not read"):
        load_action_script(tmp_path / "absent.yaml", catalog=catalog)


def test_repeat_is_expanded_into_concrete_declared_steps(tmp_path: Path, catalog: Any) -> None:
    path = write(
        tmp_path,
        "script_id: s\non_unavailable: halt\nsteps:\n"
        "  - declaration_id: turn.end_turn\n    repeat: 4\n",
    )
    script = load_action_script(path, catalog=catalog)
    assert len(script.steps) == 4
    assert [str(step.declaration_id) for step in script.steps] == ["turn.end_turn"] * 4


def test_the_shipped_builder_script_loads_against_the_real_catalog(catalog: Any) -> None:
    """The immediate use case, validated against the catalog it will actually run against.

    A positive control for the whole load path: every rejection test above proves the validator
    refuses something, and this proves it still accepts the one script we intend to run.
    """
    script = load_action_script(LIVE_SCRIPT, catalog=catalog)
    assert script.script_id == "builder_from_capital"
    assert script.on_unavailable is UnavailablePolicy.RETRY
    assert script.attempts_per_step == 6
    ids = [str(step.declaration_id) for step in script.steps]
    assert ids[:2] == ["cities.select", "cities.set_production"]
    assert ids[2:] == ["turn.end_turn"] * 8


# --------------------------------------------------------------------------
# Serving decisions
# --------------------------------------------------------------------------


def test_it_serves_the_declared_steps_in_order() -> None:
    provider = ScriptedModelProvider(a_script([SELECT, PRODUCE, END_TURN]))
    available = ["cities.select", "cities.set_production", "turn.end_turn"]

    first = provider.complete(request_for(available))
    second = provider.complete(request_for(available, step_index=2))
    third = provider.complete(request_for(available, step_index=3))

    assert first.decision is not None
    assert str(first.decision.action_declaration_id) == "cities.select"
    assert first.decision.parameters == {"target": 65536}
    assert first.decision.is_end_turn is False
    assert second.decision is not None
    assert str(second.decision.action_declaration_id) == "cities.set_production"
    assert third.decision is not None
    assert third.decision.is_end_turn is True


def test_every_response_reports_the_scripted_identity() -> None:
    """The field the whole coverage separation rests on, on every path this adapter can take.

    Four distinct exits are exercised: a step issued while available, a step issued while
    unavailable, the halt that follows it, and the post-halt end turn. If any one of them
    reported something else, a scripted landing would be filed as an agent's.
    """
    provider = ScriptedModelProvider(a_script([SELECT, PRODUCE]))
    responses = [
        provider.complete(request_for(["cities.select"])),
        provider.complete(request_for([], [("cities.set_production", "no city selected")])),
        provider.complete(request_for(["turn.end_turn"])),
        provider.complete(request_for(["turn.end_turn"])),
    ]
    assert len(responses) == 4
    for response in responses:
        assert response.model_served.provider == SCRIPTED_PROVIDER_NAME
        assert provenance_of_model_ref(response.model_served) is DecisionProvenance.SCRIPTED
        assert response.outcome is CallOutcome.DECISION_RETURNED
        assert response.cost.amount_usd == 0.0
        assert response.decision is not None


def test_every_recorded_reasoning_says_a_script_named_the_action() -> None:
    """The store keeps the reasoning verbatim, so the warning has to be in it -- an operator
    reading one decision out of the ledger must not have to know which run it came from."""
    provider = ScriptedModelProvider(a_script([SELECT]))
    first = provider.complete(request_for(["cities.select"])).decision
    after = provider.complete(request_for(["turn.end_turn"])).decision
    assert first is not None and after is not None
    for decision in (first, after):
        assert "DECLARED SCRIPT, NOT CHOSEN BY AN AGENT" in decision.reasoning
        assert "No model was consulted" in decision.reasoning
    assert "declared step 1 of 1" in first.reasoning
    assert "open the panel" in first.reasoning  # the step's declared purpose travels with it


def test_the_request_is_not_modified() -> None:
    """One of the port contract's "Adapter obligations" (P2)."""
    provider = ScriptedModelProvider(a_script([SELECT]))
    request = request_for(["cities.select"])
    before = request.observation
    response = provider.complete(request)
    assert request.observation == before
    assert response.image_count == len(request.images)


# --------------------------------------------------------------------------
# The unavailable policies -- declared, never a silent skip
# --------------------------------------------------------------------------


def test_an_unavailable_step_is_still_issued_so_the_refusal_is_recorded() -> None:
    """Never skipped: the action goes out, the harness's own dispatcher records the refusal with
    the game's reason, and only then does the policy decide what the script does next."""
    provider = ScriptedModelProvider(a_script([SELECT], policy=UnavailablePolicy.HALT))
    response = provider.complete(request_for([], [("cities.select", "no city of yours exists")]))
    assert response.decision is not None
    assert str(response.decision.action_declaration_id) == "cities.select"
    assert "NOT available" in response.decision.reasoning
    assert "no city of yours exists" in response.decision.reasoning


def test_halt_stops_the_script_and_says_what_was_not_reached() -> None:
    provider = ScriptedModelProvider(
        a_script([SELECT, PRODUCE, END_TURN], policy=UnavailablePolicy.HALT)
    )
    provider.complete(request_for([], [("cities.select", "no city")]))
    after = provider.complete(request_for(["turn.end_turn"]))

    assert after.decision is not None
    assert after.decision.is_end_turn is True
    assert "HALTED at declared step 1 of 3" in after.decision.reasoning
    assert "2 later step(s) were never reached" in after.decision.reasoning

    statuses = [row.status for row in provider.ledger()]
    assert statuses == [
        ScriptStepStatus.ISSUED_UNAVAILABLE,
        ScriptStepStatus.NOT_REACHED,
        ScriptStepStatus.NOT_REACHED,
    ]


def test_continue_advances_past_an_unavailable_step() -> None:
    provider = ScriptedModelProvider(
        a_script([SELECT, PRODUCE], policy=UnavailablePolicy.CONTINUE)
    )
    first = provider.complete(request_for([], [("cities.select", "no city")]))
    second = provider.complete(request_for(["cities.set_production"]))

    assert first.decision is not None
    assert str(first.decision.action_declaration_id) == "cities.select"
    assert second.decision is not None
    assert str(second.decision.action_declaration_id) == "cities.set_production"
    statuses = [row.status for row in provider.ledger()]
    assert statuses == [
        ScriptStepStatus.ISSUED_UNAVAILABLE,
        ScriptStepStatus.ISSUED_AVAILABLE,
    ]


def test_retry_re_issues_the_same_step_up_to_its_declared_bound_then_halts() -> None:
    """The bound lives on the provider, which lives for the whole run -- not in a local that a
    re-entry would hand a fresh ladder (the guard-scope defect found twice on 2026-09-22)."""
    provider = ScriptedModelProvider(
        a_script([SELECT, PRODUCE], policy=UnavailablePolicy.RETRY, attempts=3)
    )
    unavailable = request_for([], [("cities.select", "no city")])

    for _ in range(3):
        response = provider.complete(unavailable)
        assert response.decision is not None
        assert str(response.decision.action_declaration_id) == "cities.select"

    after = provider.complete(request_for(["turn.end_turn"]))
    assert after.decision is not None
    assert after.decision.is_end_turn is True
    assert "HALTED at declared step 1 of 2" in after.decision.reasoning
    assert provider.ledger()[0].issues == 3
    assert provider.ledger()[1].status is ScriptStepStatus.NOT_REACHED


def test_retry_stops_retrying_as_soon_as_the_step_becomes_available() -> None:
    provider = ScriptedModelProvider(
        a_script([SELECT, PRODUCE], policy=UnavailablePolicy.RETRY, attempts=4)
    )
    provider.complete(request_for([], [("cities.select", "no city")]))
    provider.complete(request_for(["cities.select"]))
    third = provider.complete(request_for(["cities.set_production"]))

    assert third.decision is not None
    assert str(third.decision.action_declaration_id) == "cities.set_production"
    assert provider.ledger()[0].status is ScriptStepStatus.ISSUED_AVAILABLE
    assert provider.ledger()[0].issues == 2


def test_an_action_the_request_does_not_list_at_all_is_distinguishable() -> None:
    """"Greyed out with a reason" and "not listed at all" are different facts about the board,
    and the ledger says which one it was rather than collapsing both to "unavailable"."""
    provider = ScriptedModelProvider(a_script([SELECT], policy=UnavailablePolicy.HALT))
    provider.complete(request_for(["turn.end_turn"]))
    row = provider.ledger()[0]
    assert row.status is ScriptStepStatus.ISSUED_UNAVAILABLE
    assert row.reason == scripted_module.NOT_LISTED_REASON


# --------------------------------------------------------------------------
# The ledger: absence and unobservability do not share a representation
# --------------------------------------------------------------------------


def test_pending_never_reads_as_not_reached_while_the_script_is_still_running() -> None:
    provider = ScriptedModelProvider(a_script([SELECT, PRODUCE, END_TURN]))
    assert [row.status for row in provider.ledger()] == [ScriptStepStatus.PENDING] * 3
    provider.complete(request_for(["cities.select"]))
    assert [row.status for row in provider.ledger()] == [
        ScriptStepStatus.ISSUED_AVAILABLE,
        ScriptStepStatus.PENDING,
        ScriptStepStatus.PENDING,
    ]
    assert provider.steps_not_reached == 0


def test_a_completed_script_reports_complete_not_halted() -> None:
    provider = ScriptedModelProvider(a_script([SELECT]))
    provider.complete(request_for(["cities.select"]))
    summary = provider.ledger_summary()
    assert "COMPLETE" in summary
    assert "HALTED" not in summary
    assert provider.steps_not_reached == 0


def test_the_ledger_serialises_for_a_results_file() -> None:
    provider = ScriptedModelProvider(a_script([SELECT, PRODUCE]))
    provider.complete(request_for(["cities.select"]))
    rows = [row.as_dict() for row in provider.ledger()]
    assert rows[0]["ordinal"] == 1
    assert rows[0]["status"] == "issued_available"
    assert rows[0]["parameters"] == {"target": 65536}
    assert rows[1]["status"] == "pending"


# --------------------------------------------------------------------------
# The cursor does not rewind
# --------------------------------------------------------------------------


def test_each_ledger_row_records_the_sequence_epoch_it_was_issued_in() -> None:
    """The observable fact about a chain's continuity, recorded without being interpreted.

    A `step_index` restart is a new turn cycle -- which is a new turn far more often than it is a
    replayed attempt, and nothing in the request tells the two apart. So the provider counts the
    restarts and says which epoch each step went to; `civsim store coverage` answers the same
    question definitively from the store, where the attempt index lives.
    """
    provider = ScriptedModelProvider(a_script([SELECT, PRODUCE, END_TURN]))
    provider.complete(request_for(["cities.select"], step_index=1))
    provider.complete(request_for(["cities.set_production"], step_index=2))
    # A new turn cycle -- or a replay. Indistinguishable from here, and not guessed at.
    provider.complete(request_for(["turn.end_turn"], step_index=1))

    rows = provider.ledger()
    assert [row.sequence_epoch for row in rows] == [0, 0, 1]
    assert [row.step_index for row in rows] == [1, 2, 1]
    assert rows[0].as_dict()["sequence_epoch"] == 0


def test_a_scrambled_chain_is_distinguishable_from_a_chain_that_does_not_work() -> None:
    """The two records the replay question is really about, side by side.

    "The setup landed and the dependent step was still greyed out" is a finding about the
    harness. "The setup went somewhere else" is not. They differ in the ledger, because the board
    itself is the authority on whether the precondition held -- the provider never issues a
    dependent step as though its setup had landed when the request says otherwise.
    """
    works = ScriptedModelProvider(a_script([SELECT, PRODUCE], policy=UnavailablePolicy.CONTINUE))
    works.complete(request_for(["cities.select"]))
    works.complete(request_for(["cities.set_production"]))
    assert [row.status for row in works.ledger()] == [
        ScriptStepStatus.ISSUED_AVAILABLE,
        ScriptStepStatus.ISSUED_AVAILABLE,
    ]

    broken = ScriptedModelProvider(a_script([SELECT, PRODUCE], policy=UnavailablePolicy.CONTINUE))
    broken.complete(request_for(["cities.select"]))
    broken.complete(request_for([], [("cities.set_production", "no city selected")]))
    rows = broken.ledger()
    assert rows[0].status is ScriptStepStatus.ISSUED_AVAILABLE
    assert rows[1].status is ScriptStepStatus.ISSUED_UNAVAILABLE
    assert rows[1].reason == "no city selected"


def test_a_replayed_turn_attempt_does_not_rewind_the_script() -> None:
    """`step_index` restarting at 1 is the harness's turn boundary, and a script is a sequence
    for the RUN. The ledger records what was actually sent, not what a fresh attempt "should"
    have sent -- reconstructing the latter would be a record of something that did not happen."""
    provider = ScriptedModelProvider(a_script([SELECT, PRODUCE, END_TURN]))
    provider.complete(request_for(["cities.select"], step_index=1))
    provider.complete(request_for(["cities.set_production"], step_index=2))
    # The harness abandons the attempt and replays it: step_index restarts at 1.
    third = provider.complete(request_for(["turn.end_turn"], step_index=1))
    assert third.decision is not None
    assert third.decision.is_end_turn is True
    assert [row.issues for row in provider.ledger()] == [1, 1, 1]


# --------------------------------------------------------------------------
# describe(), and the composition root
# --------------------------------------------------------------------------


def test_describe_reports_this_adapter_not_the_named_model() -> None:
    provider = ScriptedModelProvider(a_script([END_TURN]))
    caps = provider.describe(ModelRef(provider="openrouter", model="anthropic/x"))
    assert caps.confirmed is True
    assert caps.accepts_images is True


def test_build_provider_refuses_scripted_without_a_script() -> None:
    from civsim_harness.run.composition import PROVIDER_NAMES, build_provider

    assert "scripted" in PROVIDER_NAMES
    with pytest.raises(ValueError, match="requires a declared script"):
        build_provider("scripted")


def test_build_provider_loads_and_validates_the_script() -> None:
    from civsim_harness.run.composition import build_provider

    provider = build_provider(
        "scripted", script_path=LIVE_SCRIPT, catalog_root=CATALOG_ROOT
    )
    assert isinstance(provider, ScriptedModelProvider)
    assert provider.script.script_id == "builder_from_capital"


def test_build_provider_refuses_a_bad_script_before_a_run_starts(tmp_path: Path) -> None:
    from civsim_harness.run.composition import build_provider

    path = write(
        tmp_path,
        "script_id: s\non_unavailable: halt\nsteps:\n  - declaration_id: nope.nope\n",
    )
    with pytest.raises(ScriptValidationError):
        build_provider("scripted", script_path=path, catalog_root=CATALOG_ROOT)


def test_the_live_drivers_resolve_scripted_through_the_composition_root() -> None:
    """The exact call the goal-run driver makes, exercised in the suite rather than three frames
    inside a live run -- the lesson T262 taught this driver the hard way.

    Both directions are asserted: the refusal when no script is supplied (so the optional
    keyword's `None` default can never quietly produce a do-nothing provider at the one
    production call site), and the success when one is.
    """
    from live import goal_run

    with pytest.raises(ValueError, match="requires a declared script"):
        goal_run.build_goal_provider("scripted", seed=0, policy="uniform")

    provider = goal_run.build_goal_provider(
        "scripted", seed=0, policy="uniform", script_path=LIVE_SCRIPT
    )
    assert isinstance(provider, ScriptedModelProvider)


def test_the_goal_run_cli_refuses_the_two_incoherent_flag_combinations() -> None:
    from live import goal_run

    with pytest.raises(SystemExit):
        goal_run.main(["--goal", "build_a_builder", "--provider", "scripted", "out"])
    with pytest.raises(SystemExit):
        goal_run.main(
            ["--goal", "build_a_builder", "--provider", "stochastic", "--script", "x", "out"]
        )


def test_the_goal_run_results_carry_the_ledger_and_the_warning() -> None:
    from live import goal_run

    provider = ScriptedModelProvider(a_script([SELECT]))
    provider.complete(request_for(["cities.select"]))
    fields = goal_run.scripted_result_fields(provider, LIVE_SCRIPT)
    assert fields["script_id"] == "t"
    assert fields["script_on_unavailable"] == "halt"
    assert fields["script_ledger"][0]["status"] == "issued_available"
    assert "not chosen by an agent" in fields["scripted_landing_warning"]

    # Every other provider has no ledger, so these keys are absent rather than empty -- their
    # presence IS the statement that the run was a capability test.
    assert goal_run.scripted_result_fields(object(), None) == {}
