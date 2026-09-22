"""A declaration says which arguments its Lua takes, and the dispatcher passes them.

**The defect, re-derived from the store on 2026-09-22 rather than inherited from a brief.**
``act/executor.py::_build_arguments`` produced ``(*extra, target)`` where ``extra`` was every
non-``target`` key of the decision's free-form ``parameters``. Across all **1600** action records
in ``civsim-match-store.db`` (snapshot 2026-09-22T23:27Z -- the store is live and these counts
grow) the parameter key-set is **only** ``('target',)`` (1194) or ``()`` (406) -- never anything
else. So ``extra`` was empty on every dispatch the harness has ever made, and **exactly one
argument** could reach the Lua. Five shipped actions need two or three:

===========================  ======================================  ==========================
declaration                  the Lua it dispatches to                what the store says
===========================  ======================================  ==========================
``policies.slot_policy``     ``SlotPolicy(slotIndex, policyType)``   71 records, 71 refused
                                                                     ``unknown_policy``, 0 applied
``religion.found_religion``  ``FoundReligion(religionType,           40 records, 40 refused
                             beliefType)``                           ``unknown_religion_or_belief``,
                                                                     0 applied
``congress.cast_vote``       ``CastVote(resolutionId, choiceId,      0 records -- never attempted
                             votes)``
``espionage.assign_mission`` ``AssignMission(spyUnitId,              0 records -- never attempted
                             missionType, targetCityId)``
``policies.assign_governor`` ``AssignGovernor(governorType,          0 records -- never attempted
                             cityId)``
===========================  ======================================  ==========================

The last three were predicted blocked and are confirmed **by reading their Lua**, which is the
only evidence available for an action nobody has issued: ``CastVote`` refuses
``vote_option_not_supplied`` unless ``choiceId`` is 1 or 2; ``AssignMission`` looks up
``GameInfo.UnitOperations[nil]`` and answers ``unknown_mission``; ``AssignGovernor`` requires
``type(cityId) == "number"`` and answers ``unknown_city``. Each takes its *own* declared ``target``
in a parameter position other than the first, or needs a value the model has no way to send.

**The positive control the store supplies, and the reason it matters.** An action that has applied
cannot be blocked at the Lua boundary. ``policies.change_government`` (2 applied),
``research.set_tech`` (72 applied), ``cities.select`` (49/49 applied) all dispatch exactly one
argument and all work -- so the failure is specific to arity, not to the dispatch path.

**What is NOT this defect, stated so it is not folded in.** ``units.promote``'s absence has a
different cause. Five actions already work by carrying a *per-action shim* in their own Lua --
``units.move_to``, ``units.build_improvement``, ``units.promote``, ``camera.move`` and the
``cities.*`` family's shared ``CivSim_CityOrders_NormaliseArguments`` -- each re-deriving "a lone
string/table in the first parameter is really the last one". That convention is what this
declaration field replaces; :data:`SHIMMED_ACTIONS` below is its published, diffable inventory and
the ratchet at the bottom is what stops a sixth from being written instead of declared.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from civsim_harness.act.dispatch import DispatchStatus, dispatch_action
from civsim_harness.act.executor import ActionExecutor
from civsim_harness.capability.executor import CapabilityExecutor
from civsim_harness.capability.loader import load_catalog
from civsim_harness.capability.registry import CapabilityRegistry
from civsim_harness.errors import CatalogError
from civsim_harness.models.catalog import DeclarationKind, ParityDeclaration
from civsim_harness.models.common import (
    CatalogVersionRef,
    DecisionStepId,
    DeclarationId,
    LuaContext,
    ObservationId,
)
from civsim_harness.models.decision import RejectionReason
from civsim_harness.models.turn import Observation, ObservationEntry
from civsim_harness.observe.assemble import CapabilityResult

REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG_ROOT = REPO_ROOT / "catalogs"

SLOT_POLICY = DeclarationId("policies.slot_policy")
FOUND_RELIGION = DeclarationId("religion.found_religion")
CAST_VOTE = DeclarationId("congress.cast_vote")
ASSIGN_MISSION = DeclarationId("espionage.assign_mission")
ASSIGN_GOVERNOR = DeclarationId("policies.assign_governor")


# --------------------------------------------------------------------------
# The production argument-marshalling, against the real shipped catalog
# --------------------------------------------------------------------------


class _RecordingCapabilityExecutor:
    """The T206 seam: performs nothing, records the positional arguments it was handed."""

    def __init__(self) -> None:
        self.arguments: tuple[Any, ...] = ()
        self.calls = 0

    async def execute(
        self,
        declaration_id: DeclarationId,
        *,
        context: LuaContext,
        arguments: Sequence[Any] = (),
    ) -> CapabilityResult:
        self.calls += 1
        self.arguments = tuple(arguments)
        return CapabilityResult(declaration_id=declaration_id, value={"ok": True})


def _registry() -> CapabilityRegistry:
    return CapabilityRegistry(catalog=load_catalog(CATALOG_ROOT))


@pytest.fixture
def slot_policy_observation() -> Observation:
    """A board on which ``policies.slot_policy``'s own availability predicate
    (``target in player.available_policies``) holds -- so the only thing the refusal below can be
    about is the missing argument."""
    return Observation(
        observation_id=ObservationId("obs-1"),
        decision_step_id=DecisionStepId("step-1"),
        assembled_at=datetime(2026, 9, 22, tzinfo=UTC),
        catalog_version=CatalogVersionRef(version="2026.09.21", content_hash="deadbeef"),
        entries=[
            ObservationEntry(
                declaration_id=DeclarationId("government.state"),
                key="government.state",
                value={
                    "current_government": None,
                    "available_governments": [],
                    "available_policies": ["POLICY_URBAN_PLANNING"],
                    "governors": [],
                    "available_governors": [],
                },
                context=LuaContext.GAME_CORE_TUNER,
            )
        ],
        screen_identity="world_view",
    )


async def _dispatch_arguments(
    declaration_id: DeclarationId, parameters: dict[str, Any]
) -> tuple[Any, ...]:
    recorder = _RecordingCapabilityExecutor()
    executor = ActionExecutor(executor=cast(CapabilityExecutor, recorder), registry=_registry())
    await executor(declaration_id, parameters, parameters.get("target"))
    return recorder.arguments


# --------------------------------------------------------------------------
# The five blocked actions, each in its own Lua's own parameter order
# --------------------------------------------------------------------------


async def test_slot_policy_sends_the_slot_then_the_policy() -> None:
    """``SlotPolicy(slotIndex, policyType)``. The target is the POLICY and it is the SECOND
    argument -- 71 of 71 records answered ``unknown_policy`` because it arrived first."""
    assert await _dispatch_arguments(
        SLOT_POLICY, {"target": "POLICY_URBAN_PLANNING", "slot_index": 0}
    ) == (0, "POLICY_URBAN_PLANNING")


async def test_found_religion_sends_the_religion_then_the_belief() -> None:
    """``FoundReligion(religionType, beliefType)``. This declaration's own target is the BELIEF
    (``target in player.available_beliefs``), so the religion cannot be the target and had no way
    to be sent at all -- 39 of 39 answered ``unknown_religion_or_belief``."""
    assert await _dispatch_arguments(
        FOUND_RELIGION,
        {"target": "BELIEF_RELIGIOUS_SETTLEMENTS", "religion_type": "RELIGION_BUDDHISM"},
    ) == ("RELIGION_BUDDHISM", "BELIEF_RELIGIOUS_SETTLEMENTS")


async def test_cast_vote_sends_the_resolution_the_choice_and_the_vote_count() -> None:
    """``CastVote(resolutionId, choiceId, votes)`` -- three arguments, and its own Lua already
    documented that two of them "arrive nil through act/executor.py's positional convention"."""
    assert await _dispatch_arguments(CAST_VOTE, {"target": 3, "choice_id": 1, "votes": 5}) == (
        3,
        1,
        5,
    )


async def test_assign_mission_sends_the_spy_the_mission_and_the_city() -> None:
    """``AssignMission(spyUnitId, missionType, targetCityId)``. Target is the spy, so the mission
    -- the thing the action is named for -- could never be sent."""
    assert await _dispatch_arguments(
        ASSIGN_MISSION,
        {"target": 88, "mission_type": "MISSION_STEAL_TECH_BOOST", "target_city_id": 12},
    ) == (88, "MISSION_STEAL_TECH_BOOST", 12)


async def test_assign_governor_sends_the_governor_then_the_city() -> None:
    """``AssignGovernor(governorType, cityId)``, whose Lua refuses ``unknown_city`` unless the
    second argument is a number."""
    assert await _dispatch_arguments(
        ASSIGN_GOVERNOR, {"target": "GOVERNOR_THE_GOVERNOR", "city_id": 65536}
    ) == ("GOVERNOR_THE_GOVERNOR", 65536)


# --------------------------------------------------------------------------
# THE POSITIVE CONTROL, varying the dimension the rule constrains
# --------------------------------------------------------------------------
#
# The dimension is "does this declaration declare its Lua arguments". A twin that varied only the
# *action* (another two-argument Lua, also declared) could not fail. These vary the DECLARATION:
# an action that says nothing must marshal exactly as it did before the field existed.


@pytest.mark.parametrize(
    ("declaration_id", "parameters", "expected"),
    [
        # A single-argument action that has APPLIED in the store (71 times) -- it cannot be
        # blocked by arity, and it must not start behaving differently now.
        (DeclarationId("research.set_tech"), {"target": "TECH_POTTERY"}, ("TECH_POTTERY",)),
        # The shimmed five: their Lua still does its own normalising and must keep receiving the
        # one argument it normalises.
        (
            DeclarationId("units.promote"),
            {"target": "PROMOTION_BATTLECRY"},
            ("PROMOTION_BATTLECRY",),
        ),
        (
            DeclarationId("units.build_improvement"),
            {"target": "IMPROVEMENT_MINE"},
            ("IMPROVEMENT_MINE",),
        ),
        (DeclarationId("units.move_to"), {"target": {"x": 44, "y": 30}}, ({"x": 44, "y": 30},)),
        (DeclarationId("cities.set_production"), {"target": "UNIT_BUILDER"}, ("UNIT_BUILDER",)),
        (DeclarationId("camera.move"), {"target": {"x": 1, "y": 2}}, ({"x": 1, "y": 2},)),
        # No target at all: still the empty tuple, as before.
        (DeclarationId("turn.end_turn"), {}, ()),
    ],
)
async def test_a_declaration_that_does_not_say_marshals_exactly_as_before(
    declaration_id: DeclarationId, parameters: dict[str, Any], expected: tuple[Any, ...]
) -> None:
    assert await _dispatch_arguments(declaration_id, parameters) == expected


async def test_an_undeclared_action_still_ignores_stray_parameters_the_old_way() -> None:
    """The pre-existing sorted-key convention for an undeclared action is untouched, including
    its own oddity: a stray parameter still leads. Pinned so a future change to it is visible."""
    assert await _dispatch_arguments(
        DeclarationId("research.set_tech"), {"target": "TECH_POTTERY", "aaa": 1}
    ) == (1, "TECH_POTTERY")


async def test_prompt_responses_keep_their_own_shape() -> None:
    """``prompts.*`` share one generic ``respond(promptType, optionId)``; that rule predates this
    field and stays first, so no prompt declaration can be re-routed by declaring arguments."""
    assert await _dispatch_arguments(
        DeclarationId("prompts.ai_diplomatic_approach"), {"target": "Goodbye"}
    ) == ("prompt.diplomatic_approach", "Goodbye")


# --------------------------------------------------------------------------
# A missing required argument refuses LOUDLY -- it never passes fewer
# --------------------------------------------------------------------------


async def test_a_missing_declared_argument_is_refused_before_any_lua_runs() -> None:
    """Never ``(0,)`` where ``(0, policy)`` was declared: the dispatch does not happen at all."""
    recorder = _RecordingCapabilityExecutor()
    executor = ActionExecutor(executor=cast(CapabilityExecutor, recorder), registry=_registry())

    with pytest.raises(CatalogError) as excinfo:
        await executor(SLOT_POLICY, {"target": "POLICY_URBAN_PLANNING"}, "POLICY_URBAN_PLANNING")

    assert recorder.calls == 0
    assert excinfo.value.detail["missing"] == ["slot_index"]
    assert excinfo.value.detail["declaration_id"] == str(SLOT_POLICY)


async def test_a_declared_target_that_was_not_supplied_is_refused_too() -> None:
    """``target`` is declared like any other argument and is not exempt from the check."""
    recorder = _RecordingCapabilityExecutor()
    executor = ActionExecutor(executor=cast(CapabilityExecutor, recorder), registry=_registry())

    with pytest.raises(CatalogError):
        await executor(CAST_VOTE, {"choice_id": 1, "votes": 5}, None)

    assert recorder.calls == 0


def test_the_decision_is_refused_at_dispatch_rather_than_crashing_the_run(
    slot_policy_observation: Observation,
) -> None:
    """The loud refusal the run loop actually sees. ``act/dispatch.py`` owns "may this decision
    proceed"; a decision missing a declared argument is stopped there, recorded with its own
    reason, and the board is left alone -- the executor's raise above is the point-of-use
    backstop, not the production path."""
    outcome = dispatch_action(
        registry=_registry(),
        context=LuaContext.IN_GAME,
        action_declaration_id=SLOT_POLICY,
        observation=slot_policy_observation,
        target="POLICY_URBAN_PLANNING",
        parameters={"target": "POLICY_URBAN_PLANNING"},
    )

    assert outcome.status is DispatchStatus.rejected
    assert outcome.rejection_reason is RejectionReason.MISSING_REQUIRED_ARGUMENT
    assert outcome.detail["missing"] == ["slot_index"]


def test_the_same_decision_with_its_argument_is_authorized(
    slot_policy_observation: Observation,
) -> None:
    """The control for the refusal above: same action, same board, one key added."""
    outcome = dispatch_action(
        registry=_registry(),
        context=LuaContext.IN_GAME,
        action_declaration_id=SLOT_POLICY,
        observation=slot_policy_observation,
        target="POLICY_URBAN_PLANNING",
        parameters={"target": "POLICY_URBAN_PLANNING", "slot_index": 0},
    )

    assert outcome.status is DispatchStatus.authorized


# --------------------------------------------------------------------------
# THE RATCHET: a Lua function that takes more than one argument must say so
# --------------------------------------------------------------------------
#
# "A rule is not a control." The convention "declare your arguments" would rest on every future
# author following it; this derives the requirement from the Lua itself. A new multi-parameter
# order either declares `lua_arguments` of matching arity or appears below, in a diff a reviewer
# reads.

#: The five per-action shims that predate this field: their Lua re-derives the argument order
#: itself, from a lone string/table in the first parameter. Each is recorded with the commit or
#: measurement that put it there. They are exempt from the ratchet because their Lua ALREADY
#: normalises -- not because anyone decided they need not declare.
SHIMMED_ACTIONS: dict[str, str] = {
    "units.move_to": "lua/ingame/unit_orders.lua: a lone table argument is the plot",
    "units.promote": "lua/ingame/unit_orders.lua (6606d4b): a lone string is the promotion",
    "units.build_improvement": "lua/ingame/unit_orders.lua: a lone string is the improvement",
    "cities.set_production": "lua/ingame/city_orders.lua CivSim_CityOrders_NormaliseArguments",
    "cities.purchase_with_gold": "lua/ingame/city_orders.lua CivSim_CityOrders_NormaliseArguments",
    "cities.purchase_with_faith": "lua/ingame/city_orders.lua CivSim_CityOrders_NormaliseArguments",
    "camera.move": "lua/ingame/camera.lua: a lone table argument is the plot",
    # Not a shim: one generic Lua function for the whole family, supplied by act/executor.py's own
    # prompts rule (`respond(promptType, optionId)`), which predates and outranks this field.
}

_DISPATCH_TABLE_RE = re.compile(r"^(CivSim_\w+)\s*=\s*\{(.*?)\n\}", re.MULTILINE | re.DOTALL)
_DISPATCH_ENTRY_RE = re.compile(r"(\w+)\s*=\s*(CivSim_\w+)")


def _lua_parameter_count(implementation_ref: str, function_key: str) -> int | None:
    """How many parameters the Lua function serving *function_key* declares.

    ``None`` when the file's dispatch table does not name that key -- the resolution rules for
    that live in ``capability/executor.py`` and are not re-derived here.
    """
    source = (REPO_ROOT / implementation_ref).read_text(encoding="utf-8")
    table = _DISPATCH_TABLE_RE.search(source)
    if table is None:
        return None
    entries = dict(_DISPATCH_ENTRY_RE.findall(table.group(2)))
    function_name = entries.get(function_key)
    if function_name is None:
        return None
    signature = re.search(
        rf"^local function {re.escape(function_name)}\((.*?)\)", source, re.MULTILINE
    )
    if signature is None:
        return None
    parameters = [p.strip() for p in signature.group(1).split(",") if p.strip()]
    return len(parameters)


def _action_declarations() -> list[ParityDeclaration]:
    catalog = load_catalog(CATALOG_ROOT)
    return [d for d in catalog.declarations.values() if d.kind is DeclarationKind.ACTION]


def test_the_lua_signature_parser_finds_a_signature_we_know_is_there() -> None:
    """The positive control for the parser itself: a negative result from an unvalidated search
    is not evidence. ``slot_policy`` is known to take two parameters."""
    assert _lua_parameter_count("lua/ingame/empire_orders.lua", "slot_policy") == 2
    assert _lua_parameter_count("lua/ingame/empire_orders.lua", "set_research") == 1
    assert _lua_parameter_count("lua/ingame/congress.lua", "cast_vote") == 3


def test_every_multi_argument_lua_order_either_declares_or_is_a_listed_shim() -> None:
    from civsim_harness.capability.executor import _parse_dispatch_table, _resolve_function_key

    catalog = load_catalog(CATALOG_ROOT)
    capabilities = catalog.capabilities

    undeclared: dict[str, int] = {}
    for declaration in _action_declarations():
        capability = capabilities[declaration.capability_id]
        source = (REPO_ROOT / capability.implementation_ref).read_text(encoding="utf-8")
        _module, keys = _parse_dispatch_table(
            source, implementation_ref=capability.implementation_ref
        )
        key = _resolve_function_key(
            declaration_id=declaration.declaration_id,
            capability_id=declaration.capability_id,
            table_keys=keys,
        )
        count = _lua_parameter_count(capability.implementation_ref, key)
        if count is None or count < 2:
            continue
        name = str(declaration.declaration_id)
        if name in SHIMMED_ACTIONS or name.startswith("prompts."):
            continue
        if declaration.lua_arguments is None:
            undeclared[name] = count

    assert undeclared == {}, (
        "these actions dispatch to a Lua function taking two or more parameters but declare no "
        f"lua_arguments and carry no shim: {undeclared}"
    )


def test_a_declarations_argument_count_matches_its_lua_signature() -> None:
    from civsim_harness.capability.executor import _parse_dispatch_table, _resolve_function_key

    catalog = load_catalog(CATALOG_ROOT)
    capabilities = catalog.capabilities

    mismatched: dict[str, tuple[int, int]] = {}
    declared = 0
    for declaration in _action_declarations():
        if declaration.lua_arguments is None:
            continue
        declared += 1
        capability = capabilities[declaration.capability_id]
        source = (REPO_ROOT / capability.implementation_ref).read_text(encoding="utf-8")
        _module, keys = _parse_dispatch_table(
            source, implementation_ref=capability.implementation_ref
        )
        key = _resolve_function_key(
            declaration_id=declaration.declaration_id,
            capability_id=declaration.capability_id,
            table_keys=keys,
        )
        count = _lua_parameter_count(capability.implementation_ref, key)
        if count is not None and count != len(declaration.lua_arguments):
            mismatched[str(declaration.declaration_id)] = (
                len(declaration.lua_arguments),
                count,
            )

    assert mismatched == {}
    # The ratchet is worthless if nothing declares: this is the "could it ever have failed" half.
    # Pinned rather than `>= 1` so a sixth declaration is a line a reviewer has to change --
    # bump it, and say in the message which action was added and what its Lua takes.
    assert declared == 5, (
        f"{declared} declarations carry lua_arguments; the shipped set is the five actions whose "
        "Lua takes two or three arguments (policies.slot_policy, policies.assign_governor, "
        "religion.found_religion, congress.cast_vote, espionage.assign_mission)"
    )


def test_the_shim_inventory_names_only_real_actions() -> None:
    """A shim list that has drifted off the catalog is an exemption nobody can audit."""
    known = {str(d.declaration_id) for d in _action_declarations()}
    assert set(SHIMMED_ACTIONS) <= known


# --------------------------------------------------------------------------
# The model is the only caller that can supply these, so it has to be told
# --------------------------------------------------------------------------
#
# This is what makes the field reachable rather than an optional parameter nothing passes. The
# store's evidence that it was NOT reachable before is exact: 1549 action records, parameter
# key-sets `('target',)` and `()` only -- because `assemble_action_catalog_text` told the model
# "names that thing in parameters.target, and nothing else".


def test_the_action_listing_names_each_declared_extra_parameter() -> None:
    from civsim_harness.agent.context import assemble_action_catalog_text

    text = assemble_action_catalog_text(_action_declarations())
    line = next(ln for ln in text.splitlines() if ln.startswith(f"- {SLOT_POLICY}:"))

    assert "slot_index" in line
    vote_line = next(ln for ln in text.splitlines() if ln.startswith(f"- {CAST_VOTE}:"))
    assert "choice_id" in vote_line
    assert "votes" in vote_line


def test_an_action_with_no_declared_arguments_renders_exactly_as_before() -> None:
    """The positive control along the same dimension: the listing for an undeclared action must
    not grow a parameter tail."""
    from civsim_harness.agent.context import assemble_action_catalog_text

    text = assemble_action_catalog_text(_action_declarations())
    line = next(ln for ln in text.splitlines() if ln.startswith("- research.set_tech:"))

    assert "also send" not in line


def test_the_listing_no_longer_tells_the_model_to_send_nothing_else() -> None:
    """The sentence that made the field unreachable. It has to admit the exception, or the model
    will keep sending `target` alone and every declared action refuses for a missing argument."""
    from civsim_harness.agent.context import assemble_action_catalog_text

    text = assemble_action_catalog_text(_action_declarations())

    assert "and nothing else:" not in text
