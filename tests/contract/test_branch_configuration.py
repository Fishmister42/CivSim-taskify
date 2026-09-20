"""Contract test for branch configuration (T164).

contracts/run-configuration.md "Branch configuration": a `branch_from`
document that restates seed, civilization, ruleset, mod set, map, or game
settings *differently* from its parent is rejected; only `model_config`,
`guidance_set`, `stop_condition`, `no_progress_step_limit`,
`recovery_attempt_limit`, and `min_free_disk_gb` may vary.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from civsim_harness.config.run_config import (
    BranchFrom,
    load_branch_configuration_yaml,
)
from civsim_harness.errors import PreflightError
from civsim_harness.models.config import RunConfiguration

NOW = datetime(2026, 9, 19, tzinfo=UTC)


def _parent_config(**overrides: object) -> RunConfiguration:
    payload: dict[str, object] = {
        "config_id": "config_parent",
        "map_seed": "1849275663",
        "civilization": "CIVILIZATION_ROME",
        "leader": "LEADER_TRAJAN",
        "ruleset": "RULESET_EXPANSION_2",
        "mod_set": [{"id": "bbg-community-balance", "version": "4.2.1"}],
        "map_settings": {"map_type": "MAPTYPE_CONTINENTS", "map_size": "MAPSIZE_SMALL"},
        "game_settings": {"game_speed": "GAMESPEED_STANDARD"},
        "difficulty": "DIFFICULTY_PRINCE",
        "opponents": {"major_count": 5, "city_state_count": 10},
        "stop_condition": {"type": "turn_reached", "turn": 50},
        "model_config": {"primary": {"provider": "openrouter", "model": "anthropic/claude-opus-5"}},
        "no_progress_step_limit": 8,
        "recovery_attempt_limit": 3,
        "min_free_disk_gb": 25,
        "created_at": NOW,
    }
    payload.update(overrides)
    return RunConfiguration.model_validate(payload)


def _branch_yaml(body: str) -> str:
    return (
        "schema_version: 1\n"
        "branch_from:\n"
        "  run_id: run_parent_01\n"
        "  turn: 23\n" + body
    )


# --------------------------------------------------------------------------
# Happy path: only variable fields change; everything else inherits
# --------------------------------------------------------------------------


def test_branch_inherits_every_unspecified_field_from_the_parent() -> None:
    parent = _parent_config()
    text = _branch_yaml(
        "model_config:\n"
        "  primary: { provider: openrouter, model: anthropic/claude-sonnet-5 }\n"
    )

    child, branch_from = load_branch_configuration_yaml(text, parent_config=parent)

    assert branch_from == BranchFrom(run_id="run_parent_01", turn=23)
    assert child.civilization == parent.civilization
    assert child.leader == parent.leader
    assert child.ruleset == parent.ruleset
    assert child.mod_set == parent.mod_set
    assert child.map_settings == parent.map_settings
    assert child.game_settings == parent.game_settings
    assert child.difficulty == parent.difficulty
    assert child.opponents == parent.opponents
    assert child.map_seed == parent.map_seed
    # The one field this branch actually varied:
    assert child.agent_model_config.primary.model == "anthropic/claude-sonnet-5"
    assert child.agent_model_config.primary.model != parent.agent_model_config.primary.model


def test_branch_may_vary_every_field_the_contract_names_as_variable() -> None:
    parent = _parent_config()
    text = _branch_yaml(
        "model_config:\n"
        "  primary: { provider: openrouter, model: anthropic/claude-sonnet-5 }\n"
        "stop_condition: { type: turn_reached, turn: 30 }\n"
        "no_progress_step_limit: 12\n"
        "recovery_attempt_limit: 5\n"
        "min_free_disk_gb: 40\n"
    )

    child, _ = load_branch_configuration_yaml(text, parent_config=parent)

    assert child.stop_condition.turn == 30  # type: ignore[union-attr]
    assert child.no_progress_step_limit == 12
    assert child.recovery_attempt_limit == 5
    assert child.min_free_disk_gb == 40


def test_branch_restating_an_inherited_field_with_the_same_value_is_not_rejected() -> None:
    """Restating an inherited field is only rejected when it *differs* --
    matching the parent's value is harmless and must not error.
    """
    parent = _parent_config()
    text = _branch_yaml("civilization: CIVILIZATION_ROME\n")

    child, _ = load_branch_configuration_yaml(text, parent_config=parent)
    assert child.civilization == "CIVILIZATION_ROME"


def test_branch_restating_mod_set_in_a_different_order_is_not_rejected() -> None:
    """mod_set agreement is order-independent (mirrors config/seed_set.py's
    own V3 check) -- the same mods restated in a different order is not
    "restating differently".
    """
    parent = _parent_config(
        mod_set=[
            {"id": "mod-a", "version": "1.0"},
            {"id": "mod-b", "version": "2.0"},
        ]
    )
    text = _branch_yaml(
        "mod_set:\n"
        "  - { id: mod-b, version: '2.0' }\n"
        "  - { id: mod-a, version: '1.0' }\n"
        "model_config:\n"
        "  primary: { provider: openrouter, model: anthropic/claude-sonnet-5 }\n"
    )

    child, _ = load_branch_configuration_yaml(text, parent_config=parent)
    assert {(m.id, m.version) for m in child.mod_set} == {("mod-a", "1.0"), ("mod-b", "2.0")}


# --------------------------------------------------------------------------
# Rejections: restating an inherited field differently
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field_yaml", "field_name"),
    [
        ("civilization: CIVILIZATION_GREECE\n", "civilization"),
        ("leader: LEADER_GORGO\n", "leader"),
        ("ruleset: RULESET_STANDARD\n", "ruleset"),
        ("map_seed: '999999'\n", "map_seed"),
        ("difficulty: DIFFICULTY_DEITY\n", "difficulty"),
        (
            "map_settings:\n  map_type: MAPTYPE_ISLANDS\n  map_size: MAPSIZE_SMALL\n",
            "map_settings",
        ),
        ("game_settings:\n  game_speed: GAMESPEED_MARATHON\n", "game_settings"),
        ("opponents:\n  major_count: 8\n  city_state_count: 10\n", "opponents"),
        (
            "mod_set:\n  - { id: some-other-mod, version: '1.0' }\n",
            "mod_set",
        ),
    ],
)
def test_branch_restating_an_inherited_field_differently_is_rejected(
    field_yaml: str, field_name: str
) -> None:
    parent = _parent_config()
    text = _branch_yaml(field_yaml)

    with pytest.raises(PreflightError) as excinfo:
        load_branch_configuration_yaml(text, parent_config=parent)

    mismatched_fields = {m["field"] for m in excinfo.value.detail["mismatches"]}
    assert field_name in mismatched_fields


def test_branch_rejects_multiple_mismatches_at_once() -> None:
    parent = _parent_config()
    text = _branch_yaml("civilization: CIVILIZATION_GREECE\nruleset: RULESET_STANDARD\n")

    with pytest.raises(PreflightError) as excinfo:
        load_branch_configuration_yaml(text, parent_config=parent)

    mismatched_fields = {m["field"] for m in excinfo.value.detail["mismatches"]}
    assert mismatched_fields == {"civilization", "ruleset"}


def test_branch_cannot_change_seed_set_id() -> None:
    parent = _parent_config(seed_set_id="shuffle-classic-2026q3")
    text = _branch_yaml("seed_set: some-other-set\n")

    with pytest.raises(PreflightError) as excinfo:
        load_branch_configuration_yaml(text, parent_config=parent)

    mismatched_fields = {m["field"] for m in excinfo.value.detail["mismatches"]}
    assert "seed_set_id" in mismatched_fields


# --------------------------------------------------------------------------
# Structural requirements
# --------------------------------------------------------------------------


def test_branch_configuration_requires_a_branch_from_block() -> None:
    parent = _parent_config()
    text = (
        "schema_version: 1\n"
        "model_config:\n"
        "  primary: { provider: openrouter, model: anthropic/claude-sonnet-5 }\n"
    )

    with pytest.raises(PreflightError):
        load_branch_configuration_yaml(text, parent_config=parent)


def test_branch_from_requires_run_id_and_turn() -> None:
    parent = _parent_config()
    text = "schema_version: 1\nbranch_from:\n  run_id: run_parent_01\n"

    with pytest.raises(PreflightError):
        load_branch_configuration_yaml(text, parent_config=parent)


def test_branch_configuration_rejects_a_credential_shaped_value() -> None:
    parent = _parent_config()
    text = _branch_yaml("api_key: sk-should-never-be-here\n")

    with pytest.raises(PreflightError):
        load_branch_configuration_yaml(text, parent_config=parent)
