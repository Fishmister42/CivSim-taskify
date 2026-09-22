"""Unit tests for the catalog loader and registry (T037).

Each test isolates exactly one of contracts/capability-catalog.md's seven
load-time validations (plus the registry's runtime wrong-context refusal) by
building a small, otherwise-valid catalog fixture under ``tmp_path`` and
breaking a single field. These deliberately do **not** touch the real
``catalogs/`` tree: that directory is authored and validated by a different
task (an integration concern), and is in flux while this test was written.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from civsim_harness.capability.loader import Catalog, load_catalog
from civsim_harness.capability.registry import CapabilityRegistry, WrongContextError
from civsim_harness.errors import CatalogError
from civsim_harness.models.common import LuaContext

# --------------------------------------------------------------------------
# Fixture builders -- a minimal, otherwise-valid catalog entry per kind.
# --------------------------------------------------------------------------


def _capability(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "capability_id": "demo.cap",
        "path": "firetuner",
        "implementation_ref": "lua/gamecore/demo.lua",
        "reads": ["demo state"],
        "writes": [],
        "firetuner_gap": None,
        "parity_basis": "Look at the demo panel.",
    }
    base.update(overrides)
    return base


def _observation(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "declaration_id": "demo.state",
        "kind": "observation",
        "summary": "Demo state.",
        "parity_basis": "Look at the demo panel.",
        "context": "GameCore_Tuner",
        "capability_id": "demo.cap",
        "output_schema": {
            "type": "object",
            "properties": {"value": {"type": "integer"}},
        },
        "introduced_in_version": "2026.09.1",
    }
    base.update(overrides)
    return base


def _action(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "declaration_id": "demo.do_thing",
        "kind": "action",
        "summary": "Do the thing.",
        "parity_basis": "Click the thing button.",
        "context": "InGame",
        "capability_id": "demo.cap",
        "availability_predicate": "game.is_local_player_turn and unit.is_selected",
        "verification_predicate": "unit.plot == target",
        "introduced_in_version": "2026.09.1",
    }
    base.update(overrides)
    return base


def _without(entry: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {key: value for key, value in entry.items() if key not in keys}


def _write_catalog(
    root: Path,
    *,
    version: str | None = "2026.09.1",
    capabilities: list[dict[str, Any]] | None = None,
    observations: list[dict[str, Any]] | None = None,
    actions: list[dict[str, Any]] | None = None,
) -> Path:
    if version is not None:
        (root / "VERSION").write_text(version + "\n", encoding="utf-8")

    (root / "observations").mkdir(exist_ok=True)
    (root / "actions").mkdir(exist_ok=True)

    caps = capabilities if capabilities is not None else [_capability()]
    obs = observations if observations is not None else [_observation()]
    acts = actions if actions is not None else [_action()]

    (root / "capabilities.yaml").write_text(yaml.safe_dump(caps, sort_keys=False), encoding="utf-8")
    (root / "observations" / "demo.yaml").write_text(
        yaml.safe_dump(obs, sort_keys=False), encoding="utf-8"
    )
    (root / "actions" / "demo.yaml").write_text(
        yaml.safe_dump(acts, sort_keys=False), encoding="utf-8"
    )
    return root


# --------------------------------------------------------------------------
# Baseline: the fixture builder itself produces a loadable catalog.
# --------------------------------------------------------------------------


def test_valid_catalog_loads(tmp_path: Path) -> None:
    root = _write_catalog(tmp_path)

    catalog = load_catalog(root)

    assert isinstance(catalog, Catalog)
    assert catalog.version.version == "2026.09.1"
    assert catalog.version.content_hash
    assert set(catalog.version.declaration_ids) == {"demo.state", "demo.do_thing"}
    assert "demo.state" in catalog.declarations
    assert "demo.do_thing" in catalog.declarations
    assert "demo.cap" in catalog.capabilities


# --------------------------------------------------------------------------
# Validation 1 -- non-empty parity_basis
# --------------------------------------------------------------------------


def test_missing_parity_basis_fails_load(tmp_path: Path) -> None:
    root = _write_catalog(tmp_path, observations=[_without(_observation(), "parity_basis")])

    with pytest.raises(CatalogError) as exc_info:
        load_catalog(root)

    assert "parity_basis" in str(exc_info.value)


def test_empty_parity_basis_fails_load(tmp_path: Path) -> None:
    root = _write_catalog(tmp_path, observations=[_observation(parity_basis="")])

    with pytest.raises(CatalogError):
        load_catalog(root)


# --------------------------------------------------------------------------
# Validation 2 -- capability_id resolution and the bespoke/firetuner_gap rule
# --------------------------------------------------------------------------


def test_bespoke_without_firetuner_gap_fails_load(tmp_path: Path) -> None:
    root = _write_catalog(tmp_path, capabilities=[_capability(path="bespoke", firetuner_gap="")])

    with pytest.raises(CatalogError) as exc_info:
        load_catalog(root)

    assert "firetuner_gap" in str(exc_info.value)


def test_unresolved_capability_id_fails_load(tmp_path: Path) -> None:
    root = _write_catalog(tmp_path, observations=[_observation(capability_id="nonexistent.cap")])

    with pytest.raises(CatalogError) as exc_info:
        load_catalog(root)

    assert "capability_id" in str(exc_info.value)


# --------------------------------------------------------------------------
# Validation 3 -- declaration_id uniqueness across files
# --------------------------------------------------------------------------


def test_duplicate_declaration_id_fails_load(tmp_path: Path) -> None:
    root = _write_catalog(
        tmp_path,
        observations=[_observation(declaration_id="demo.dup")],
        actions=[_action(declaration_id="demo.dup")],
    )

    with pytest.raises(CatalogError) as exc_info:
        load_catalog(root)

    assert "duplicate" in str(exc_info.value).lower()


# --------------------------------------------------------------------------
# Validation 4 -- actions require both predicates
# --------------------------------------------------------------------------


def test_action_missing_availability_predicate_fails_load(tmp_path: Path) -> None:
    root = _write_catalog(tmp_path, actions=[_without(_action(), "availability_predicate")])

    with pytest.raises(CatalogError) as exc_info:
        load_catalog(root)

    assert "predicate" in str(exc_info.value).lower()


def test_action_missing_verification_predicate_fails_load(tmp_path: Path) -> None:
    root = _write_catalog(tmp_path, actions=[_without(_action(), "verification_predicate")])

    with pytest.raises(CatalogError) as exc_info:
        load_catalog(root)

    assert "predicate" in str(exc_info.value).lower()


# --------------------------------------------------------------------------
# Validation 5 -- observations/views require a valid output_schema
# --------------------------------------------------------------------------


def test_observation_missing_output_schema_fails_load(tmp_path: Path) -> None:
    root = _write_catalog(tmp_path, observations=[_without(_observation(), "output_schema")])

    with pytest.raises(CatalogError) as exc_info:
        load_catalog(root)

    assert "output_schema" in str(exc_info.value)


def test_observation_with_malformed_output_schema_fails_load(tmp_path: Path) -> None:
    root = _write_catalog(
        tmp_path, observations=[_observation(output_schema={"type": "not-a-real-json-schema-type"})]
    )

    with pytest.raises(CatalogError) as exc_info:
        load_catalog(root)

    assert "output_schema" in str(exc_info.value)


# --------------------------------------------------------------------------
# Validation 6 -- predicates reference only exposed symbols
# --------------------------------------------------------------------------


def test_predicate_with_unexposed_symbol_fails_load(tmp_path: Path) -> None:
    root = _write_catalog(
        tmp_path, actions=[_action(availability_predicate="opponent.secret_hand_visible")]
    )

    with pytest.raises(CatalogError) as exc_info:
        load_catalog(root)

    assert "symbol" in str(exc_info.value).lower()


def test_predicate_with_observed_snapshot_symbol_loads(tmp_path: Path) -> None:
    root = _write_catalog(
        tmp_path,
        actions=[
            _action(
                declaration_id="turn.end_turn",
                verification_predicate="game.turn_number == observed_turn_number + 1",
            )
        ],
    )

    catalog = load_catalog(root)

    assert "turn.end_turn" in catalog.declarations


# --------------------------------------------------------------------------
# Validation 7 -- catalogs/VERSION presence
# --------------------------------------------------------------------------


def test_missing_version_file_fails_load(tmp_path: Path) -> None:
    root = _write_catalog(tmp_path, version=None)

    with pytest.raises(CatalogError) as exc_info:
        load_catalog(root)

    assert "VERSION" in str(exc_info.value)


# --------------------------------------------------------------------------
# Registry -- wrong-context execution refusal (T036, FR-022, research R3)
# --------------------------------------------------------------------------


def test_wrong_context_execution_is_refused(tmp_path: Path) -> None:
    root = _write_catalog(tmp_path)
    catalog = load_catalog(root)
    registry = CapabilityRegistry(catalog=catalog)

    # "demo.do_thing" is declared context: InGame.
    with pytest.raises(WrongContextError):
        registry.authorize("demo.do_thing", LuaContext.GAME_CORE_TUNER)

    # "demo.state" is declared context: GameCore_Tuner.
    with pytest.raises(WrongContextError):
        registry.authorize("demo.state", LuaContext.IN_GAME)


def test_correct_context_execution_is_authorized(tmp_path: Path) -> None:
    root = _write_catalog(tmp_path)
    catalog = load_catalog(root)
    registry = CapabilityRegistry(catalog=catalog)

    action = registry.authorize("demo.do_thing", LuaContext.IN_GAME)
    observation = registry.authorize("demo.state", LuaContext.GAME_CORE_TUNER)

    assert action.declaration_id == "demo.do_thing"
    assert observation.declaration_id == "demo.state"


def test_unregistered_declaration_id_is_refused(tmp_path: Path) -> None:
    root = _write_catalog(tmp_path)
    catalog = load_catalog(root)
    registry = CapabilityRegistry(catalog=catalog)

    with pytest.raises(CatalogError):
        registry.authorize("no.such.declaration", LuaContext.IN_GAME)


def test_implementation_ref_must_be_a_path_not_prose() -> None:
    """T314: a sentence in `implementation_ref` killed every run that met a prompt.

    `capability/executor.py::_load_dispatch_table` resolves this field with
    `self._lua_root / implementation_ref` and raises `CatalogError` when the result is not
    a file. That resolution is **lazy and cached** -- it happens the first time the
    capability is dispatched, *inside a live run*. So `prompts.orders` shipping
    `lua/ingame/screens.lua + src/civsim_harness/capability/executor.py` did not fail the
    catalog load; it paused every run that met a prompt, and only those. Runs that drew no
    `prompts.*` never touched it.

    The field was introduced by the commit that made the *bespoke-path* declaration
    honest -- a commit about a validator that could only ever agree, which added a field
    nothing validated. Hence this test: the guard has to be able to fail.
    """
    import pytest
    from pydantic import ValidationError

    from civsim_harness.models.catalog import IntegrationCapability
    from civsim_harness.models.common import CapabilityId

    # The exact value that shipped, verbatim.
    with pytest.raises(ValidationError, match="single relative source path"):
        IntegrationCapability(
            capability_id=CapabilityId("prompts.orders"),
            path="bespoke",
            implementation_ref=(
                "lua/ingame/screens.lua + src/civsim_harness/capability/executor.py"
            ),
            firetuner_gap="measured gap",
        )

    # Neighbouring prose shapes are refused too, so the fix is not keyed to one separator.
    for prose in (
        "lua/ingame/screens.lua, src/civsim_harness/capability/executor.py",
        "lua/ingame/screens.lua and the host click path",
        "src/civsim_harness/capability/executor.py",
        "",
    ):
        with pytest.raises(ValidationError):
            IntegrationCapability(
                capability_id=CapabilityId("x.y"),
                path="firetuner",
                implementation_ref=prose,
            )

    # Positive twin: the shape every other capability uses is accepted.
    ok = IntegrationCapability(
        capability_id=CapabilityId("x.y"),
        path="firetuner",
        implementation_ref="lua/ingame/screens.lua",
    )
    assert ok.implementation_ref == "lua/ingame/screens.lua"


def test_every_shipped_implementation_ref_resolves_to_a_file_on_disk() -> None:
    """The load-time twin of the dispatch-time check, so it cannot fail in a run first."""
    from pathlib import Path

    from civsim_harness.capability.loader import load_catalog

    catalog = load_catalog(Path("catalogs"))
    unresolved = [
        cap.capability_id
        for cap in catalog.capabilities.values()
        if not (Path(".") / cap.implementation_ref).is_file()
    ]
    assert unresolved == [], f"implementation_ref does not resolve on disk: {unresolved}"


def test_a_bespoke_capability_may_declare_a_python_implementation_ref() -> None:
    """T314 follow-up: the positive twin whose absence broke the tree.

    The first version of this validator required ``.lua`` for every capability. That is
    true of a *firetuner* capability -- the executor resolves the ref and dispatches into
    it -- and false of a *bespoke* one, whose ref nothing loads as Lua and which documents
    where the implementation lives. `saves.save_game` declares
    ``src/civsim_harness/saves/dialog_driver.py`` and is real, pre-existing and correct.

    It got past the original test set because **nothing in it declared a Python
    implementation**, so the positive twin could not fail. The validator encoded the
    fixtures rather than the contract -- which is the same error the `lua/` prefix version
    was rejected for, one variant over, and the reason this test exists.
    """
    from civsim_harness.models.catalog import IntegrationCapability
    from civsim_harness.models.common import CapabilityId

    cap = IntegrationCapability(
        capability_id=CapabilityId("saves.save_game"),
        path="bespoke",
        implementation_ref="src/civsim_harness/saves/dialog_driver.py",
        firetuner_gap="No save-to-named-file call is reachable from Lua.",
    )
    assert cap.implementation_ref.endswith(".py")


def test_a_firetuner_capability_may_not_declare_a_python_implementation_ref() -> None:
    """The other half: the kind rule is conditioned, not abandoned.

    A ``firetuner`` ref is operational -- the executor dispatches into it -- so a Python
    path there is the shape that would fail at dispatch time inside a live run, which is
    exactly what T314 was about. Relaxing the rule for bespoke must not relax it here.
    """
    import pytest
    from pydantic import ValidationError

    from civsim_harness.models.catalog import IntegrationCapability
    from civsim_harness.models.common import CapabilityId

    with pytest.raises(ValidationError, match="must end in .lua"):
        IntegrationCapability(
            capability_id=CapabilityId("x.y"),
            path="firetuner",
            implementation_ref="src/civsim_harness/saves/dialog_driver.py",
        )
