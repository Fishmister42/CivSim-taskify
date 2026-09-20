"""Panel Registry conformance: rules P1-P6 (T016).

`contracts/panel-registry.md` states six load-time rules and says a load
failure blocks startup. This suite is what makes that claim checkable: one
deliberately-broken registry per rule, each asserted to fail *and to name the
rule it violated*. Naming matters -- "the registry failed to load" is not
actionable at 2am, and a test that only asserts "something raised" would pass
even if every rule collapsed into one generic check.

The shipped registry is also asserted to load clean, which is the SC-005 audit
("every panel has a documented parity basis; any finding blocks release") run
in CI rather than by hand per release.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from civsim_web.registry.loader import (
    REGISTRY_FILENAMES,
    PanelRegistryLoadError,
    default_panels_dir,
    load_panel_registry,
)

VALID_IN_GAME_PANEL = {
    "panel_id": "current_turn.city_yields",
    "title": "City Yields",
    "story": ["US1", "US3"],
    "scope": "step",
    "source_fields": ["Observation.entries[declaration_id=cities.yields]"],
    "parity_basis": "Opening a city's Yields tab in the in-game City Panel",
    "category": "in_game",
    "introduced_in_version": "1",
}

VALID_TELEMETRY_PANEL = {
    "panel_id": "turn.model_call_cost",
    "title": "Model Call Cost & Latency",
    "story": ["US1", "US2", "US3"],
    "scope": "step",
    "source_fields": ["ModelCall.latency_ms", "ModelCall.cost", "ModelCall.model_served"],
    "parity_basis": None,
    "category": "out_of_game_telemetry",
    "introduced_in_version": "1",
}


def write_registry(
    root: Path,
    *,
    version: str = "1",
    live: list[dict] | None = None,
    history: list[dict] | None = None,
    catalog: list[dict] | None = None,
    shared: list[dict] | None = None,
    lock: dict | None = None,
) -> Path:
    """Materialise a registry directory. Every file is always written."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "VERSION").write_text(version + "\n", encoding="utf-8")
    contents = {
        "live.yaml": live or [],
        "history.yaml": history or [],
        "catalog.yaml": catalog or [],
        "shared.yaml": shared or [],
    }
    for filename in REGISTRY_FILENAMES:
        (root / filename).write_text(
            yaml.safe_dump(contents[filename], sort_keys=False), encoding="utf-8"
        )
    if lock is not None:
        (root / "VERSION.lock").write_text(yaml.safe_dump(lock), encoding="utf-8")
    return root


# --------------------------------------------------------------------------
# The shipped registry
# --------------------------------------------------------------------------


def test_shipped_registry_loads_clean():
    """`panels/` as shipped must load. This is the SC-005 audit, in CI."""
    registry = load_panel_registry(default_panels_dir())
    assert registry.version
    assert registry.content_hash
    for declaration in registry.declarations:
        if declaration.category == "in_game":
            assert (declaration.parity_basis or "").strip(), (
                f"panel {declaration.panel_id} is in_game with no parity_basis "
                f"-- SC-005 blocks release on this"
            )


def test_a_valid_registry_indexes_by_source_field(tmp_path):
    """The index is what makes UP-001 structural, so it is asserted directly."""
    root = write_registry(
        tmp_path / "panels", live=[VALID_IN_GAME_PANEL], shared=[VALID_TELEMETRY_PANEL]
    )
    registry = load_panel_registry(root)

    assert registry.panel_ids == ("current_turn.city_yields", "turn.model_call_cost")
    assert registry.get("current_turn.city_yields") is not None
    # An unregistered panel id resolves to nothing -- a 404 at the route layer,
    # never a "field unavailable" render.
    assert registry.get("no.such.panel") is None
    # A registered field names its panel; an unregistered one names none, which
    # is how a view-model constructor never reads it at all.
    assert registry.panels_for_field("Observation", "entries")
    assert registry.panels_for_field("Observation", "screen_identity") == ()


# --------------------------------------------------------------------------
# P1 - P6
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "parity_basis",
    [None, "", "   "],
    ids=["missing", "empty", "whitespace"],
)
def test_p1_in_game_panel_without_parity_basis_fails_to_load(tmp_path, parity_basis):
    """P1: category in_game with an empty/missing parity_basis fails to load."""
    broken = {**VALID_IN_GAME_PANEL, "parity_basis": parity_basis}
    root = write_registry(tmp_path / "panels", live=[broken])

    with pytest.raises(PanelRegistryLoadError) as excinfo:
        load_panel_registry(root)
    assert excinfo.value.rule == "P1"
    assert "parity_basis" in str(excinfo.value)


def test_p2_telemetry_panel_with_a_parity_basis_fails_to_load(tmp_path):
    """P2: out_of_game_telemetry must have `parity_basis: null`.

    The failure mode this prevents is specific: a telemetry panel acquiring a
    fabricated in-client justification it does not have, which would launder
    harness data into looking like a parity-cleared observation (FR-013).
    """
    broken = {
        **VALID_TELEMETRY_PANEL,
        "parity_basis": "Hovering the turn timer in the in-game HUD",
    }
    root = write_registry(tmp_path / "panels", shared=[broken])

    with pytest.raises(PanelRegistryLoadError) as excinfo:
        load_panel_registry(root)
    assert excinfo.value.rule == "P2"


def test_p3_source_field_absent_from_002_data_model_fails_to_load(tmp_path):
    """P3: every source field must exist in 002's published data-model.md."""
    broken = {
        **VALID_IN_GAME_PANEL,
        "source_fields": ["Observation.opponent_research_progress"],
    }
    root = write_registry(tmp_path / "panels", live=[broken])

    with pytest.raises(PanelRegistryLoadError) as excinfo:
        load_panel_registry(root)
    assert excinfo.value.rule == "P3"
    assert "opponent_research_progress" in str(excinfo.value)


def test_p3_unknown_entity_fails_to_load(tmp_path):
    """P3 again: an entity 002 never published cannot be declared over."""
    broken = {**VALID_IN_GAME_PANEL, "source_fields": ["HiddenAIIntent.plan"]}
    root = write_registry(tmp_path / "panels", live=[broken])

    with pytest.raises(PanelRegistryLoadError) as excinfo:
        load_panel_registry(root)
    assert excinfo.value.rule == "P3"


def test_p4_duplicate_panel_id_across_files_fails_to_load(tmp_path):
    """P4: panel_id is one namespace across all four files."""
    root = write_registry(
        tmp_path / "panels",
        live=[VALID_IN_GAME_PANEL],
        shared=[dict(VALID_IN_GAME_PANEL)],
    )

    with pytest.raises(PanelRegistryLoadError) as excinfo:
        load_panel_registry(root)
    assert excinfo.value.rule == "P4"
    assert "current_turn.city_yields" in str(excinfo.value)


def test_p5_run_scoped_panel_reading_a_step_level_field_fails_to_load(tmp_path):
    """P5: scope must be consistent with every source field's granularity.

    A run-scoped panel listing a DecisionStep-level field is the aggregation
    that could smuggle step detail into what reads as a run-level summary.
    """
    broken = {
        **VALID_IN_GAME_PANEL,
        "panel_id": "run.sneaky_summary",
        "scope": "run",
        "source_fields": ["DecisionStep.step_index"],
    }
    root = write_registry(tmp_path / "panels", catalog=[broken])

    with pytest.raises(PanelRegistryLoadError) as excinfo:
        load_panel_registry(root)
    assert excinfo.value.rule == "P5"


def test_p5_step_scoped_panel_may_read_a_run_level_field(tmp_path):
    """The converse is allowed: a fine panel may cite a coarse field.

    Asserted so P5 is not implemented as "scope must equal field scope", which
    would make a step panel unable to name its own run.
    """
    fine = {
        **VALID_IN_GAME_PANEL,
        "panel_id": "step.with_run_context",
        "scope": "step",
        "source_fields": ["Observation.entries", "Run.run_id"],
    }
    root = write_registry(tmp_path / "panels", live=[fine])
    assert load_panel_registry(root).get("step.with_run_context") is not None


def test_p6_declaration_claiming_a_future_version_fails_to_load(tmp_path):
    """P6, first half: a declaration cannot claim a version later than the registry's."""
    broken = {**VALID_IN_GAME_PANEL, "introduced_in_version": "2"}
    root = write_registry(tmp_path / "panels", version="1", live=[broken])

    with pytest.raises(PanelRegistryLoadError) as excinfo:
        load_panel_registry(root)
    assert excinfo.value.rule == "P6"


def test_p6_editing_a_declaration_within_a_frozen_version_fails_to_load(tmp_path):
    """P6, second half: declarations are immutable within a version.

    A frozen version is recorded in `panels/VERSION.lock`. Editing a
    declaration in place then fails with instructions to bump the version,
    which is what "changing one is a new registry version" means operationally.
    """
    root = tmp_path / "panels"
    write_registry(root, version="1", live=[VALID_IN_GAME_PANEL])
    frozen_hash = _hashes(load_panel_registry(root))

    edited = {**VALID_IN_GAME_PANEL, "title": "City Yields (revised)"}
    write_registry(
        root,
        version="1",
        live=[edited],
        lock={"1": {"panels": frozen_hash}},
    )

    with pytest.raises(PanelRegistryLoadError) as excinfo:
        load_panel_registry(root)
    assert excinfo.value.rule == "P6"
    assert "immutable" in str(excinfo.value)


def test_p6_bumping_the_version_accepts_the_edit(tmp_path):
    """The escape hatch P6 prescribes actually works, or the rule is a wall."""
    root = tmp_path / "panels"
    write_registry(root, version="1", live=[VALID_IN_GAME_PANEL])
    frozen_hash = _hashes(load_panel_registry(root))

    edited = {
        **VALID_IN_GAME_PANEL,
        "title": "City Yields (revised)",
        "introduced_in_version": "1",
    }
    write_registry(root, version="2", live=[edited], lock={"1": {"panels": frozen_hash}})

    registry = load_panel_registry(root)
    assert registry.version == "2"
    assert registry.get("current_turn.city_yields").title == "City Yields (revised)"


# --------------------------------------------------------------------------
# Schema-level rejections (not numbered rules, but load-blocking all the same)
# --------------------------------------------------------------------------


def test_unknown_key_in_a_declaration_fails_to_load(tmp_path):
    """An unrecognised key is a typo or a field someone expected to matter."""
    broken = {**VALID_IN_GAME_PANEL, "parity_bassis": "typo"}
    root = write_registry(tmp_path / "panels", live=[broken])

    with pytest.raises(PanelRegistryLoadError) as excinfo:
        load_panel_registry(root)
    assert excinfo.value.rule == "schema"


def test_missing_registry_file_fails_to_load(tmp_path):
    """All four files are required; a missing one is not an empty one."""
    root = write_registry(tmp_path / "panels")
    (root / "history.yaml").unlink()

    with pytest.raises(PanelRegistryLoadError) as excinfo:
        load_panel_registry(root)
    assert excinfo.value.rule == "schema"


def _hashes(registry) -> dict[str, str]:
    """panel_id -> declaration hash, the shape `panels/VERSION.lock` records."""
    from civsim_web.registry.loader import _declaration_payload, _hash

    return {d.panel_id: _hash(_declaration_payload(d)) for d in registry.declarations}
