"""The parity red-team suite (T122, research R15's "load-bearing test tier").

A fixture list of forbidden values -- unrevealed map contents, opponent
internal state, hidden AI intent, undisclosed opponent research/civics,
unit/city data beyond what the standard UI reveals, RNG state, and debug/
provenance data (FR-019) -- asserted absent from contexts assembled from
realistic transcripts, **at every decision step of a multi-step turn, not
once per turn** (SC-006, SC-008).

**Why this test lives in ``tests/contract``.** SC-006 and SC-008 make any
finding release-blocking, so this suite is meant to run on every build, not
per release (research R15) -- that is what puts it in the contract tier
rather than unit, even though it exercises the same modules
``tests/unit/test_image_screening.py`` and ``tests/unit/test_telemetry_exclusion.py``
do. Any finding here fails the build; this suite never weakens an assertion
to reach green.

**The realistic transcript this suite builds.** Real catalog declarations
(loaded from the checked-in ``catalogs/`` tree, not a synthetic stand-in) are
each given a raw capability payload that satisfies their own declared
``output_schema`` for every *required* property -- and, in the contaminated
half of the suite, also carries additional, undeclared properties a buggy or
malicious Lua implementation could return. This is not a hypothetical: the
schema validator both ``observe/assemble.py`` and this harness's catalog
loader use only checks properties the schema *names*; neither rejects an
*extra*, undeclared one (there is no ``additionalProperties: false``
enforcement anywhere on the value-validation path). A capability that leaks
one more field than its schema promises is therefore not caught by schema
validation at all -- it is caught here, by the forbidden-field guard
(T128), which is exactly the "belt-and-braces" role research R9 rule 5
assigns it. This suite proves that role is actually filled, across
:mod:`civsim_harness.parity.filter` and ``observe/assemble.py`` alike --
which since T231 are the *same* implementation of research R9 rule 1's
"capability result is the only input" shape, the assembler having routed
through ``parity.filter`` rather than copying it (see the T231 tests at the
bottom of this file) -- and across every step of a multi-step turn.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from civsim_harness.capability.loader import load_catalog
from civsim_harness.capability.registry import CapabilityRegistry
from civsim_harness.errors import ParityViolation
from civsim_harness.models.common import CatalogVersionRef, DecisionStepId, ObservationId
from civsim_harness.models.turn import Observation
from civsim_harness.observe.assemble import CapabilityResult as AssembleCapabilityResult
from civsim_harness.observe.assemble import assemble_observation
from civsim_harness.parity.filter import CapabilityResult as FilterCapabilityResult
from civsim_harness.parity.filter import filter_to_entries
from civsim_harness.parity.forbidden import enforce_parity_boundary, find_literal_leaks

_REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOGS_ROOT = _REPO_ROOT / "catalogs"


@pytest.fixture(scope="module")
def registry() -> CapabilityRegistry:
    return CapabilityRegistry(catalog=load_catalog(CATALOGS_ROOT))


# --------------------------------------------------------------------------
# Fixture: the red-team values that must never appear anywhere downstream.
# Each is distinctive enough that a hit can only mean a real leak, never a
# coincidental collision with legitimate game data.
# --------------------------------------------------------------------------

FORBIDDEN_VALUES: dict[str, str] = {
    "rng_seed": "RNG_SEED_MARKER_7f3a9c21",
    "opponent_research": "TECH_ROCKETRY_HIDDEN_MARKER_9f2",
    "opponent_civic": "CIVIC_DIPLOMATIC_SERVICE_HIDDEN_MARKER_9f2",
    "unrevealed_plot": "UNREVEALED_PLOT_MARKER_9f2",
    "hidden_ai_intent": "AI_INTENT_DECLARE_WAR_MARKER_9f2",
    "debug_note": "DEBUG_PROVENANCE_MARKER_9f2",
    "true_combat_modifier": "9137.4471",
    "model_identity_leak": "TELEMETRY_MODEL_LEAK_MARKER_9f2",
    "latency_leak": "918273",
}


def _legitimate_payloads() -> dict[str, dict[str, Any]]:
    """Realistic, schema-conforming payloads for four real catalog declarations."""
    return {
        "map.state": {
            "width": 44,
            "height": 44,
            "revealed_plots": [
                {
                    "x": 12,
                    "y": 7,
                    "terrain": "TERRAIN_GRASS",
                    "is_currently_visible": True,
                    "owner_player_id": 0,
                }
            ],
        },
        "units.state": {
            "units": [
                {
                    "unit_id": 101,
                    "unit_type": "UNIT_WARRIOR",
                    "owner_player_id": 0,
                    "owner_is_local_player": True,
                    "plot": {"x": 12, "y": 7},
                }
            ]
        },
        "research.state": {
            "current_research": "TECH_BRONZE_WORKING",
            "current_civic": "CIVIC_CODE_OF_LAWS",
            "researchable_techs": ["TECH_POTTERY", "TECH_ANIMAL_HUSBANDRY"],
            "researchable_civics": ["CIVIC_FOREIGN_TRADE"],
        },
        "game.turn_state": {
            "turn_number": 42,
            "is_local_player_turn": True,
            "is_waiting_for_other_players": False,
        },
    }


def _contaminate(payloads: dict[str, dict[str, Any]], step_index: int) -> dict[str, dict[str, Any]]:
    """Return a deep-enough copy with one class of FR-019/FR-020 leak injected per step.

    Spread across steps rather than dumped into one, mirroring how a real
    defect would surface: a single buggy capability leaking on one call, not
    every capability leaking on every call.
    """
    contaminated = {name: {**payload} for name, payload in payloads.items()}

    if step_index == 1:
        # FR-019: undisclosed opponent research/civics, RNG state.
        contaminated["research.state"] = {
            **contaminated["research.state"],
            "opponent_research_progress": {"player_2": FORBIDDEN_VALUES["opponent_research"]},
            "opponent_civic_progress": {"player_2": FORBIDDEN_VALUES["opponent_civic"]},
            "rng_seed": FORBIDDEN_VALUES["rng_seed"],
        }
    elif step_index == 2:
        # FR-019: unrevealed map contents, debug/provenance data.
        contaminated["map.state"] = {
            **contaminated["map.state"],
            "unrevealed_plots": [
                {"x": 99, "y": 99, "terrain": FORBIDDEN_VALUES["unrevealed_plot"]}
            ],
            "debug_notes": FORBIDDEN_VALUES["debug_note"],
        }
    else:
        # FR-019 (hidden AI intent / unit data beyond the UI) + FR-020 (telemetry
        # merged into game data by a hypothetical bug).
        contaminated["units.state"] = {
            "units": [
                {
                    **contaminated["units.state"]["units"][0],
                    "hidden_ai_intent": FORBIDDEN_VALUES["hidden_ai_intent"],
                    "true_combat_modifier": FORBIDDEN_VALUES["true_combat_modifier"],
                    "model_served": FORBIDDEN_VALUES["model_identity_leak"],
                    "latency_ms": FORBIDDEN_VALUES["latency_leak"],
                }
            ]
        }
    return contaminated


def _build_observation_via_filter(
    registry: CapabilityRegistry, payloads: dict[str, dict[str, Any]], step_index: int
) -> Observation:
    entries = []
    for declaration_id, payload in payloads.items():
        entries.extend(
            filter_to_entries(
                FilterCapabilityResult(declaration_id=declaration_id, value=payload),  # type: ignore[arg-type]
                registry=registry,
            )
        )
    return Observation(
        observation_id=ObservationId(f"obs_{step_index}"),
        decision_step_id=DecisionStepId(f"step_{step_index}"),
        assembled_at="2026-09-20T00:00:00Z",  # type: ignore[arg-type]
        catalog_version=CatalogVersionRef(
            version=registry.catalog.version.version,
            content_hash=registry.catalog.version.content_hash,
        ),
        entries=entries,
        captures=[],
        screen_identity="world_view",
    )


def _build_observation_via_assemble(
    registry: CapabilityRegistry, payloads: dict[str, dict[str, Any]], step_index: int
) -> Observation:
    results = [
        AssembleCapabilityResult(declaration_id=declaration_id, value=payload)  # type: ignore[arg-type]
        for declaration_id, payload in payloads.items()
    ]
    return assemble_observation(
        observation_id=ObservationId(f"obs_asm_{step_index}"),
        decision_step_id=DecisionStepId(f"step_{step_index}"),
        catalog_version=CatalogVersionRef(
            version=registry.catalog.version.version,
            content_hash=registry.catalog.version.content_hash,
        ),
        registry=registry,
        results=results,
        screen_identity="world_view",
        assembled_at="2026-09-20T00:00:00Z",  # type: ignore[arg-type]
    )


_BUILDERS = {
    "parity.filter": _build_observation_via_filter,
    "observe.assemble": _build_observation_via_assemble,
}


# --------------------------------------------------------------------------
# Proof the vulnerability is real: schema validation alone lets an extra,
# undeclared field straight through into the assembled Observation.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("builder_name", sorted(_BUILDERS))
@pytest.mark.parametrize("step_index", [1, 2, 3])
def test_schema_validation_alone_does_not_strip_a_contaminated_field(
    registry: CapabilityRegistry, builder_name: str, step_index: int
) -> None:
    """Negative control: proves the guard is load-bearing, not decorative.

    Neither producer's schema check rejects an undeclared extra property, so
    without the forbidden-field guard the contaminated value really would
    reach the Observation -- this is what makes T128 necessary rather than
    redundant with T127/T095's own schema validation.
    """
    payloads = _contaminate(_legitimate_payloads(), step_index)
    observation = _BUILDERS[builder_name](registry, payloads, step_index)

    rendered_values = str([entry.value for entry in observation.entries])
    planted_this_step = [value for value in FORBIDDEN_VALUES.values() if value in rendered_values]

    assert planted_this_step, "expected this step's planted marker(s) to survive assembly unguarded"


# --------------------------------------------------------------------------
# The guard itself: every contaminated step is caught, independently.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("builder_name", sorted(_BUILDERS))
@pytest.mark.parametrize("step_index", [1, 2, 3])
def test_forbidden_field_guard_catches_every_contaminated_step(
    registry: CapabilityRegistry, builder_name: str, step_index: int
) -> None:
    """FR-019/FR-020, SC-006, SC-008 -- asserted **per step**, not once per turn.

    Each of the three steps below plants a *different* class of leak (see
    ``_contaminate``); this test is parametrized over all three so a build
    that only checks step 1 (or only the first Observation it happens to see)
    cannot pass by accident.
    """
    payloads = _contaminate(_legitimate_payloads(), step_index)
    observation = _BUILDERS[builder_name](registry, payloads, step_index)

    with pytest.raises(ParityViolation):
        enforce_parity_boundary(observation=observation)


def test_a_clean_multi_step_transcript_never_trips_the_guard(registry: CapabilityRegistry) -> None:
    """Positive control: legitimate data across a full multi-step turn raises nothing.

    Guards against the guard itself being so aggressive it flags ordinary
    game state (a false positive here would be its own release-blocking
    defect, since it would mask real findings behind noise).
    """
    for step_index in (1, 2, 3):
        payloads = _legitimate_payloads()
        for builder_name, builder in _BUILDERS.items():
            observation = builder(registry, payloads, step_index)
            enforce_parity_boundary(observation=observation)  # must not raise

            rendered_values = str([entry.value for entry in observation.entries])
            hits = find_literal_leaks(rendered_values, FORBIDDEN_VALUES.values())
            assert hits == [], f"{builder_name} step {step_index}: unexpected hit(s) {hits}"


# --------------------------------------------------------------------------
# Whole-transcript sweep: simulate a full turn's worth of steps at once and
# confirm the guard is applied to every one of them independently -- the
# literal SC-006/SC-008 shape ("zero observations reach the agent ... without
# a declared parity basis", "zero instances ... appearing in an agent
# context").
# --------------------------------------------------------------------------


def test_realistic_transcript_every_step_independently_checked(
    registry: CapabilityRegistry,
) -> None:
    transcript: list[tuple[int, Observation]] = []
    for step_index in (1, 2, 3):
        payloads = _contaminate(_legitimate_payloads(), step_index)
        observation = _build_observation_via_filter(registry, payloads, step_index)
        transcript.append((step_index, observation))

    caught_steps: list[int] = []
    for step_index, observation in transcript:
        try:
            enforce_parity_boundary(observation=observation)
        except ParityViolation:
            caught_steps.append(step_index)

    assert caught_steps == [1, 2, 3], (
        "expected every contaminated step to be caught independently; "
        f"a per-turn (rather than per-step) check would only catch the first: {caught_steps}"
    )


# --------------------------------------------------------------------------
# T231 -- one structural filter, enforced structurally
# --------------------------------------------------------------------------


_HARNESS_ROOT = _REPO_ROOT / "src" / "civsim_harness"

#: The one module allowed to construct an `ObservationEntry`, relative to `src/civsim_harness`.
#: `models/turn.py` defines the class and is excluded by the AST check itself (a `ClassDef` is
#: not a `Call`).
_SOLE_ENTRY_PRODUCER = Path("parity") / "filter.py"


def _modules_constructing_observation_entry() -> list[str]:
    """Every harness module with a literal `ObservationEntry(...)` call, by relative path.

    An AST scan rather than a text scan, for the same reason
    `tests/contract/test_read_only_boundary.py` gives: several modules *name* `ObservationEntry`
    in prose to document what they must not build, and a text match would flag exactly the files
    whose job is to be explicit about the boundary.
    """
    import ast

    found: list[str] = []
    for path in sorted(_HARNESS_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            if name == "ObservationEntry":
                found.append(str(path.relative_to(_HARNESS_ROOT)))
                break
    return found


def test_exactly_one_module_in_the_harness_builds_an_observation_entry() -> None:
    """T231 / FR-018, Principle I: one structural parity filter, not two.

    `parity/filter.py`'s own docstring makes the load-bearing claim -- "there is no other function
    in this package (and there must be none anywhere else in the harness) that builds an
    `ObservationEntry` from a bare Lua table, a raw Nexus payload string, or an unattributed
    dict". Until T231 that claim was false in the way that matters most: `observe/assemble.py`
    re-implemented the same rule inline (resolve the declaration, reject a non-observation/view
    kind, key by declaration id, carry the declaration's own `context`), and **it** was the copy
    the agent's data actually passed through, while the `parity/` one had no caller in `src/` at
    all. Both were correct, which is precisely why it was dangerous: a reviewer hardening the
    parity boundary would have hardened the dead path.

    Asserted structurally rather than behaviourally on purpose. Two correct implementations agree
    on every input, so no behavioural test can tell "one definition" from "two that happen to
    match today" -- only the shape of the code can.
    """
    producers = _modules_constructing_observation_entry()
    assert producers == [str(_SOLE_ENTRY_PRODUCER)], (
        "every ObservationEntry in the harness must be built by parity/filter.py, the module "
        "whose own package docstring calls itself Principle I's enforcement point; a second "
        "producer means the parity boundary can be hardened in a place the agent's data never "
        f"travels (T231, FR-018). Producers found: {producers}"
    )


def test_the_assembler_routes_through_the_parity_filter_rather_than_copying_it(
    registry: CapabilityRegistry,
) -> None:
    """The same claim from the calling side: `assemble_observation` delegates, it does not copy.

    A spy rather than an import check: an import can exist and be unused, which is the exact
    failure mode Phase 11 was written about. This asserts the call actually happens, once per
    result, on the real assembly path.
    """
    import civsim_harness.observe.assemble as assemble_module

    calls: list[Any] = []
    real = assemble_module.filter_to_entries

    def _spy(result: Any, *, registry: CapabilityRegistry) -> Any:
        calls.append(result.declaration_id)
        return real(result, registry=registry)

    payloads = _legitimate_payloads()
    assemble_module.filter_to_entries = _spy  # type: ignore[assignment]
    try:
        observation = _build_observation_via_assemble(registry, payloads, 1)
    finally:
        assemble_module.filter_to_entries = real  # type: ignore[assignment]

    assert calls, "assemble_observation built its entries without the parity filter (T231)"
    assert len(calls) == len(payloads)
    assert [entry.declaration_id for entry in observation.entries] == calls
