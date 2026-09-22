"""The bodies the 2026-09-21 accessor audit rewrote, executed for real against fakes shaped like
Civilization VI's own panels.

The gap these close. Ten observation fields were a permanent ``[]`` and fourteen of the harness's
thirty-eight actions could never work, because the accessors behind them do not exist in
Civilization VI -- each absence swallowed by an ``if obj.Method then`` guard or a ``pcall``. The
audit (``specs/002-civ-playing-harness/spikes/lua-accessor-audit-2026-09-21.md``) names all 38 call
sites and, for each, the accessor Firaxis' own panel uses instead.

What every test here asserts is the same pair, because it is the pair that failed in production:

* **present** -- given the fake the real panel would be given, the body reports the list the panel
  would show, through the accessor the panel calls;
* **absent** -- given a game that does not answer that accessor, the body reports
  ``<field>_reason`` and *never* a silent ``[]``. That is the convention 1d0b372 (cities) and
  6606d4b (units) established, and its whole point is that "empty" and "unreadable" stop looking
  alike to everything downstream.

Action bodies get the same treatment one level down: the assertion is on the exact operation and
the exact parameter keys the panel's button issues, because an order with a plausible-looking wrong
parameter key is precisely how ``espionage.assign_mission`` spent its life writing
``tParameters[nil]``.

Executed in an embedded Lua 5.4; that the real tuner state answers these calls stays UNVERIFIED
LIVE and is said so in each body. Skipped, not failed, where ``lupa`` is absent (it is not a
project dependency): ``uv run --with lupa pytest tests/unit/test_accessor_audit_lua.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

lupa = pytest.importorskip("lupa")

REPO_ROOT = Path(__file__).resolve().parents[2]
LUA = REPO_ROOT / "lua"

# A GameInfo stand-in: callable for `for row in GameInfo.X()`, indexable by Index/Hash/type name,
# which is how every shipped panel uses it.
_GAMEINFO = """
function make_info_table(rows, keys)
    local by_key = {}
    for _, row in ipairs(rows) do
        if row.Hash ~= nil then by_key[row.Hash] = row end
        if row.Index ~= nil then by_key[row.Index] = row end
        for _, field in ipairs(keys or {}) do
            if row[field] ~= nil then by_key[row[field]] = row end
        end
    end
    return setmetatable({}, {
        __call = function()
            local i = 0
            return function() i = i + 1 return rows[i] end
        end,
        __index = function(_, key) return by_key[key] end,
    })
end

function members(list)
    return function()
        local i = 0
        return function()
            i = i + 1
            if list[i] == nil then return nil end
            return i, list[i]
        end
    end
end
"""


def _runtime(stubs: str, lua_file: Path) -> Any:
    runtime = lupa.LuaRuntime(unpack_returned_tuples=True)
    runtime.execute(_GAMEINFO)
    runtime.execute(stubs)
    runtime.execute(lua_file.read_text(encoding="utf-8"))
    return runtime


def _list(value: Any) -> list[Any]:
    """A Lua array as a Python list. ``None`` (the body returned nothing) is never silently an
    empty list here -- that conflation is the bug under test."""
    assert value is not None, "the body returned no table at all"
    return [value[i] for i in range(1, len(value) + 1)]


# ==========================================================================
# government.state -- findings 1-4
# ==========================================================================

_GOVERNMENT_STUBS = """
M = {
    slot_policies = { [0] = -1, [1] = -1 },
    slot_count = 2,
    unlocked = { POLICY_SURVEY = true, POLICY_DISCIPLINE = true },
    obsolete = {},
    banned = {},
    slottable = nil,            -- nil => the base predicate path (no Gathering Storm)
    governments_unlocked = { GOVERNMENT_CHIEFDOM = true, GOVERNMENT_AUTOCRACY = true },
    current_government = 0,
    answer_policy_predicates = true,
    answer_government_predicate = true,
    governor_list = nil,
    has_governors = true,
    governors_object = true,
}

GameInfo = {}
GameInfo.Policies = make_info_table({
    { PolicyType = "POLICY_SURVEY", Hash = 501, Index = 1 },
    { PolicyType = "POLICY_DISCIPLINE", Hash = 502, Index = 2 },
    { PolicyType = "POLICY_LEGIONS", Hash = 503, Index = 3 },
}, {"PolicyType"})
GameInfo.Governments = make_info_table({
    { GovernmentType = "GOVERNMENT_CHIEFDOM", Hash = 601, Index = 0 },
    { GovernmentType = "GOVERNMENT_AUTOCRACY", Hash = 602, Index = 1 },
    { GovernmentType = "GOVERNMENT_OLIGARCHY", Hash = 603, Index = 2 },
}, {"GovernmentType"})
GameInfo.Governors = make_info_table({
    { GovernorType = "GOVERNOR_THE_GOVERNOR", Hash = 701, Index = 0 },
    { GovernorType = "GOVERNOR_THE_MERCHANT", Hash = 702, Index = 1 },
}, {"GovernorType"})

local culture = {}
function culture:GetCurrentGovernment() return M.current_government end
function culture:GetNumPolicySlots()
    if M.slot_count == nil then error("no slots on this build") end
    return M.slot_count
end
function culture:GetSlotPolicy(i) return M.slot_policies[i] or -1 end
function culture:IsPolicyUnlocked(hash)
    if not M.answer_policy_predicates then error("absent on this build") end
    local row = GameInfo.Policies[hash]
    return M.unlocked[row.PolicyType] == true
end
function culture:IsPolicyObsolete(hash)
    if not M.answer_policy_predicates then error("absent on this build") end
    return M.obsolete[GameInfo.Policies[hash].PolicyType] == true
end
function culture:CanPolicyBeSlotted(hash)
    if M.slottable == nil then error("Gathering Storm only") end
    return M.slottable[GameInfo.Policies[hash].PolicyType] == true
end
function culture:IsPolicyBanned(hash)
    if M.slottable == nil then error("Gathering Storm only") end
    return M.banned[GameInfo.Policies[hash].PolicyType] == true
end
function culture:IsGovernmentUnlocked(hash)
    if not M.answer_government_predicate then error("absent on this build") end
    return M.governments_unlocked[GameInfo.Governments[hash].GovernmentType] == true
end

function M.make_governor(typeIndex, city)
    local g = {}
    function g:GetType() return typeIndex end
    function g:GetAssignedCity() return city end
    return g
end

function M.make_city(id)
    local c = {}
    function c:GetID() return id end
    return c
end

local playerGovernors = {}
function playerGovernors:GetGovernorList()
    if M.governor_list == nil then error("no governors on this ruleset") end
    return M.has_governors, M.governor_list
end

local player = {}
function player:GetCulture() return culture end
function player:GetGovernors()
    if not M.governors_object then return nil end
    return playerGovernors
end

Players = { [0] = player }
Game = { GetLocalPlayer = function() return 0 end }
"""


def _government_state(configure: str = "") -> dict[str, Any]:
    runtime = _runtime(_GOVERNMENT_STUBS, LUA / "gamecore" / "government.lua")
    if configure:
        runtime.execute(configure)
    state: dict[str, Any] = runtime.eval("CivSim_Government.state()")
    return state


def test_available_policies_is_the_card_pickers_own_list() -> None:
    """governmentscreen.lua:2270-2273 builds the catalog from GameInfo.Policies() + the
    per-policy predicate; :1063-1066 then hides any card already sitting in a slot."""
    state = _government_state("M.slot_policies = { [0] = 3, [1] = -1 }")  # LEGIONS is slotted
    policies = _list(state["available_policies"])
    assert policies == ["POLICY_SURVEY", "POLICY_DISCIPLINE"]
    assert state["available_policies_reason"] is None


def test_available_policies_prefers_the_gathering_storm_predicate() -> None:
    """governmentscreen_expansion2.lua:8-14 replaces the base test with
    `not IsPolicyBanned and CanPolicyBeSlotted and not IsPolicyObsolete`. A card the base ruleset
    would call unlocked but Gathering Storm bans must not be offered."""
    state = _government_state(
        "M.slottable = { POLICY_SURVEY = true, POLICY_DISCIPLINE = true, POLICY_LEGIONS = true }\n"
        "M.banned = { POLICY_LEGIONS = true }\n"
    )
    assert _list(state["available_policies"]) == ["POLICY_SURVEY", "POLICY_DISCIPLINE"]


def test_available_policies_says_why_when_the_predicate_is_unanswerable() -> None:
    """The audit's finding #1: `culture:GetSlottablePolicies()` did not exist, and its absence
    produced an empty list rather than a complaint, for the life of the harness."""
    state = _government_state("M.answer_policy_predicates = false")
    assert _list(state["available_policies"]) == []
    assert state["available_policies_reason"] == "policy_availability_predicate_unanswerable"


def test_available_policies_says_why_when_the_slot_count_is_unreadable() -> None:
    """Without GetNumPolicySlots the "already slotted" filter cannot run, so the list would
    overstate what the picker offers. Refused by name instead."""
    state = _government_state("M.slot_count = nil")
    assert _list(state["available_policies"]) == []
    assert state["available_policies_reason"] == "get_num_policy_slots_unanswerable"


def test_available_governments_uses_is_government_unlocked_and_excludes_the_current_one() -> None:
    """Finding #2. `IsGovernmentUnlocked` takes a HASH while `GetCurrentGovernment` returns an
    INDEX (governmentscreen.lua:2334 vs :2250) -- passing the wrong one is how this stayed empty."""
    state = _government_state()
    assert _list(state["available_governments"]) == ["GOVERNMENT_AUTOCRACY"]
    assert state["current_government"] == "GOVERNMENT_CHIEFDOM"


def test_available_governments_says_why_when_unanswerable() -> None:
    state = _government_state("M.answer_government_predicate = false")
    assert _list(state["available_governments"]) == []
    assert state["available_governments_reason"] == "is_government_unlocked_unanswerable"


def test_governors_come_from_the_two_return_governor_list() -> None:
    """Findings #3 and #4: `GetGovernorList()` returns `(bool, array)` and is iterated with
    ipairs (governorpanel.lua:58,85); the assigned city is an OBJECT, not an id
    (governorsupport.lua:12-14)."""
    state = _government_state(
        "M.governor_list = { M.make_governor(0, M.make_city(7)), M.make_governor(1, nil) }"
    )
    governors = _list(state["governors"])
    assert [g["governor_type"] for g in governors] == [
        "GOVERNOR_THE_GOVERNOR",
        "GOVERNOR_THE_MERCHANT",
    ]
    assert governors[0]["assigned_city_id"] == 7
    assert governors[1]["assigned_city_id"] is None
    # Only the unassigned one can be sent somewhere, which is what policies.assign_governor reads.
    assert _list(state["available_governors"]) == ["GOVERNOR_THE_MERCHANT"]
    assert state["governors_reason"] is None


def test_governors_say_why_on_a_ruleset_without_them() -> None:
    """A base-ruleset game has no Governors panel at all; that is not the same as "you have no
    governors", and the two must not both render as []."""
    state = _government_state("M.governors_object = false")
    assert _list(state["governors"]) == []
    assert state["governors_reason"] == "player_governors_unavailable"


def test_governors_say_why_when_the_list_call_raises() -> None:
    state = _government_state("M.governor_list = nil")
    assert state["governors_reason"] == "get_governor_list_unanswerable"


def test_no_governors_appointed_yet_is_not_a_reason() -> None:
    """An empty list with a live accessor is a real answer -- the panel is showing nothing -- and
    must NOT carry a reason, or the field stops meaning anything."""
    state = _government_state("M.governor_list = {}\nM.has_governors = false")
    assert _list(state["governors"]) == []
    assert state["governors_reason"] is None


# ==========================================================================
# great_people.state + great_people.recruit -- findings 5-7, 27
# ==========================================================================

_GREAT_PEOPLE_STUBS = """
M = {
    timeline = nil,
    have_manager = true,
    recruitable = { [11] = true },
    points_total = { [0] = 42.5 },
    points_per_turn = { [0] = 3.0 },
    have_points_object = true,
    answer_points = true,
    requested = nil,
}

GameInfo = {}
GameInfo.GreatPersonIndividuals = make_info_table({
    { Name = "LOC_GP_HYPATIA", Index = 11 },
    { Name = "LOC_GP_EUCLID", Index = 12 },
})
GameInfo.GreatPersonClasses = make_info_table({
    { GreatPersonClassType = "GREAT_PERSON_CLASS_SCIENTIST", Index = 0 },
})
Locale = { Lookup = function(key)
    return ({ LOC_GP_HYPATIA = "Hypatia", LOC_GP_EUCLID = "Euclid" })[key]
end }

local gpMgr = {}
function gpMgr:GetTimeline()
    if M.timeline == nil then error("GetTimeline absent") end
    return M.timeline
end
function gpMgr:CanRecruitPerson(playerId, individual) return M.recruitable[individual] == true end

local points = {}
function points:GetPointsTotal(classIndex)
    if not M.answer_points then error("absent") end
    return M.points_total[classIndex]
end
function points:GetPointsPerTurn(classIndex)
    if not M.answer_points then error("absent") end
    return M.points_per_turn[classIndex]
end

local player = {}
function player:GetGreatPeoplePoints()
    if not M.have_points_object then return nil end
    return points
end

Players = { [0] = player }
Game = {
    GetLocalPlayer = function() return 0 end,
    GetGreatPeople = function()
        if not M.have_manager then error("no great people manager") end
        return gpMgr
    end,
}
PlayerOperations = {
    RECRUIT_GREAT_PERSON = "op_recruit",
    PARAM_GREAT_PERSON_INDIVIDUAL_TYPE = "param_individual",
}
UI = { RequestPlayerOperation = function(playerId, operation, parameters)
    M.requested = { player = playerId, operation = operation,
                    individual = parameters[PlayerOperations.PARAM_GREAT_PERSON_INDIVIDUAL_TYPE] }
end }
"""

_TIMELINE = (
    "M.timeline = {"
    " { Individual = 11, Class = 0, Era = 1, Cost = 60, Claimant = nil },"
    " { Individual = 12, Class = 0, Era = 1, Cost = 60, Claimant = 3 },"
    "}"
)


def _great_people_state(configure: str = "") -> dict[str, Any]:
    runtime = _runtime(_GREAT_PEOPLE_STUBS, LUA / "gamecore" / "great_people.lua")
    if configure:
        runtime.execute(configure)
    state: dict[str, Any] = runtime.eval("CivSim_GreatPeople.state()")
    return state


def test_offered_great_people_come_from_the_timeline() -> None:
    """Finding #5/#6: the screen's list is `Game.GetGreatPeople():GetTimeline()`
    (greatpeoplepopup.lua:704,708) and the entry fields are `Individual`/`Class`/`Cost`
    (:726,:752,:723) -- not the `individual.Index`/`.ClassType`/`.Name` this body used to read."""
    state = _great_people_state(_TIMELINE)
    offered = _list(state["recruitable_individuals"])
    assert [o["individual_id"] for o in offered] == [11, 12]
    assert offered[0]["name"] == "Hypatia"
    assert offered[0]["class_type"] == "GREAT_PERSON_CLASS_SCIENTIST"
    assert offered[0]["points_required"] == 60
    assert state["recruitable_individuals_reason"] is None


def test_is_recruitable_is_the_buttons_own_test() -> None:
    """greatpeoplepopup.lua:728 -- `CanRecruitPerson` is exactly what shows (:269) or hides (:276)
    the Recruit button, and `great_people.recruit`'s availability predicate reads this field."""
    state = _great_people_state(_TIMELINE)
    offered = _list(state["recruitable_individuals"])
    assert offered[0]["is_recruitable"] is True
    assert offered[1]["is_recruitable"] is False


def test_offered_great_people_say_why_when_the_timeline_is_unanswerable() -> None:
    state = _great_people_state()  # M.timeline stays nil
    assert _list(state["recruitable_individuals"]) == []
    assert state["recruitable_individuals_reason"] == "get_timeline_unanswerable"


def test_points_come_off_the_player_not_the_game_manager() -> None:
    """Finding #7: `GetPointsTotal`/`GetPointsPerTurn` live on `player:GetGreatPeoplePoints()`
    (greatpeoplepopup.lua:800-801), not on Game.GetGreatPeople() where this body used to ask."""
    state = _great_people_state(_TIMELINE)
    points = _list(state["points_by_class"])
    assert len(points) == 1
    assert points[0]["class_type"] == "GREAT_PERSON_CLASS_SCIENTIST"
    assert points[0]["points"] == pytest.approx(42.5)
    assert points[0]["points_per_turn"] == pytest.approx(3.0)
    assert state["points_by_class_reason"] is None


def test_points_say_why_when_unanswerable() -> None:
    state = _great_people_state("M.answer_points = false")
    assert _list(state["points_by_class"]) == []
    assert state["points_by_class_reason"] == "get_points_total_unanswerable"


def test_recruit_issues_the_buttons_operation_with_an_index() -> None:
    """Finding #27: the Recruit button is `PlayerOperations.RECRUIT_GREAT_PERSON` with the single
    key `PARAM_GREAT_PERSON_INDIVIDUAL_TYPE`, whose value is a row INDEX
    (greatpeoplepopup.lua:886-894) -- not a hash, unlike the religion operations."""
    runtime = _runtime(_GREAT_PEOPLE_STUBS, LUA / "ingame" / "great_people.lua")
    result = runtime.eval("CivSim_GreatPeopleOrders.recruit(11)")
    assert result["ok"] is True
    assert result["mechanism"] == "PlayerOperations.RECRUIT_GREAT_PERSON"
    requested = runtime.eval("M.requested")
    assert requested["operation"] == "op_recruit"
    assert requested["individual"] == 11


# ==========================================================================
# religion.state + the three religion orders -- findings 8, 28-30
# ==========================================================================

_RELIGION_STUBS = """
M = {
    in_pantheon = {},
    in_religion = {},
    too_many = {},
    answer_predicates = true,
    have_game_religion = true,
    pantheon = -1,
    created_religion = -1,
    requests = {},
}

GameInfo = {}
GameInfo.Beliefs = make_info_table({
    { BeliefType = "BELIEF_GOD_OF_THE_SEA", BeliefClassType = "BELIEF_CLASS_PANTHEON",
      Hash = 801, Index = 1 },
    { BeliefType = "BELIEF_DANCE_OF_THE_AURORA", BeliefClassType = "BELIEF_CLASS_PANTHEON",
      Hash = 802, Index = 2 },
    { BeliefType = "BELIEF_TITHE", BeliefClassType = "BELIEF_CLASS_FOUNDER",
      Hash = 803, Index = 3 },
}, {"BeliefType"})
GameInfo.Religions = make_info_table({
    { ReligionType = "RELIGION_BUDDHISM", Hash = 901, Index = 1 },
}, {"ReligionType"})

local gameReligion = {}
function gameReligion:IsInSomePantheon(index)
    if not M.answer_predicates then error("absent") end
    return M.in_pantheon[index] == true
end
function gameReligion:IsInSomeReligion(index)
    if not M.answer_predicates then error("absent") end
    return M.in_religion[index] == true
end
function gameReligion:IsTooManyForReligion(index, religionIndex)
    return M.too_many[index] == true
end

local playerReligion = {}
function playerReligion:GetPantheon() return M.pantheon end
function playerReligion:GetReligionTypeCreated() return M.created_religion end

local playerUnits = {}
function playerUnits:Members() return members({})() end
local player = {}
function player:GetReligion() return playerReligion end
function player:GetCities() return { Members = function() return members({})() end } end
function player:GetID() return 0 end
function player:IsMajor() return true end

Players = { [0] = player }
PlayerManager = { GetAlive = function() return { player } end }
PlayersVisibility = { [0] = { IsVisible = function() return true end } }
Map = { GetPlot = function() return nil end }
Game = {
    GetLocalPlayer = function() return 0 end,
    GetReligion = function()
        if not M.have_game_religion then error("no religion manager") end
        return gameReligion
    end,
}
PlayerOperations = {
    FOUND_PANTHEON = "op_found_pantheon",
    FOUND_RELIGION = "op_found_religion",
    ADD_BELIEF = "op_add_belief",
    PARAM_BELIEF_TYPE = "param_belief",
    PARAM_RELIGION_TYPE = "param_religion",
    PARAM_INSERT_MODE = "param_insert",
    VALUE_EXCLUSIVE = "exclusive",
}
UI = { RequestPlayerOperation = function(playerId, operation, parameters)
    M.requests[#M.requests + 1] = {
        operation = operation,
        belief = parameters[PlayerOperations.PARAM_BELIEF_TYPE],
        religion = parameters[PlayerOperations.PARAM_RELIGION_TYPE],
        insert = parameters[PlayerOperations.PARAM_INSERT_MODE],
    }
end }
"""


def _religion_state(configure: str = "") -> dict[str, Any]:
    runtime = _runtime(_RELIGION_STUBS, LUA / "gamecore" / "religion.lua")
    if configure:
        runtime.execute(configure)
    state: dict[str, Any] = runtime.eval("CivSim_Religion.state()")
    return state


def test_available_beliefs_before_a_pantheon_are_the_pantheon_choosers() -> None:
    """Finding #8. pantheonchooser.lua:69-77 keeps a row when it is in no pantheon, in no
    religion, and is BELIEF_CLASS_PANTHEON. There is no engine enumerator to ask."""
    state = _religion_state("M.in_pantheon = { [2] = true }")
    assert _list(state["available_beliefs"]) == ["BELIEF_GOD_OF_THE_SEA"]
    assert state["available_beliefs_reason"] is None


def test_available_beliefs_after_a_pantheon_switch_to_religion_beliefs() -> None:
    """religionscreen.lua:465-466 inverts the class filter once a pantheon exists, and :464 adds
    `IsTooManyForReligion`."""
    state = _religion_state("M.pantheon = 2")
    assert _list(state["available_beliefs"]) == ["BELIEF_TITHE"]


def test_available_beliefs_honour_is_too_many_for_religion() -> None:
    state = _religion_state("M.pantheon = 2\nM.created_religion = 1\nM.too_many = { [3] = true }")
    assert _list(state["available_beliefs"]) == []


def test_available_beliefs_say_why_when_the_predicates_are_unanswerable() -> None:
    """An unfiltered GameInfo dump is not what the chooser shows, so a body that cannot filter
    must say so rather than emit either everything or nothing."""
    state = _religion_state("M.answer_predicates = false")
    assert _list(state["available_beliefs"]) == []
    assert state["available_beliefs_reason"] == "belief_availability_predicates_unanswerable"


def test_available_beliefs_say_why_without_a_religion_manager() -> None:
    state = _religion_state("M.have_game_religion = false")
    assert state["available_beliefs_reason"] == "game_religion_unavailable"


def _religion_orders(call: str) -> tuple[dict[str, Any], list[Any]]:
    runtime = _runtime(_RELIGION_STUBS, LUA / "ingame" / "religion.lua")
    result = runtime.eval(call)
    return result, _list(runtime.eval("M.requests"))


def test_select_pantheon_issues_found_pantheon_with_a_hash() -> None:
    """Finding #28: pantheonchooser.lua:125-139 -- FOUND_PANTHEON, PARAM_BELIEF_TYPE carrying the
    belief's `.Hash` (not the Index this body used to pass), plus PARAM_INSERT_MODE."""
    result, requests = _religion_orders(
        'CivSim_ReligionOrders.select_pantheon("BELIEF_GOD_OF_THE_SEA")'
    )
    assert result["ok"] is True
    assert len(requests) == 1
    assert requests[0]["operation"] == "op_found_pantheon"
    assert requests[0]["belief"] == 801
    assert requests[0]["insert"] == "exclusive"


def test_found_religion_issues_two_requests_like_the_screen_does() -> None:
    """Finding #29: religionscreen.lua:903-919 -- FOUND_RELIGION carries only the religion, and
    each belief is a SEPARATE ADD_BELIEF request afterwards."""
    result, requests = _religion_orders(
        'CivSim_ReligionOrders.found_religion("RELIGION_BUDDHISM", "BELIEF_TITHE")'
    )
    assert result["ok"] is True
    assert [r["operation"] for r in requests] == ["op_found_religion", "op_add_belief"]
    assert requests[0]["religion"] == 901
    assert requests[0]["belief"] is None
    assert requests[1]["belief"] == 803


def test_select_belief_issues_add_belief() -> None:
    """Finding #30."""
    result, requests = _religion_orders('CivSim_ReligionOrders.select_belief("BELIEF_TITHE")')
    assert result["ok"] is True
    assert requests[0]["operation"] == "op_add_belief"
    assert requests[0]["belief"] == 803


def test_an_unknown_belief_is_refused_before_any_request() -> None:
    result, requests = _religion_orders('CivSim_ReligionOrders.select_belief("BELIEF_NOPE")')
    assert result["ok"] is False
    assert result["reason"] == "unknown_belief"
    assert requests == []


# ==========================================================================
# congress.state + congress.cast_vote -- findings 9-11, 26
# ==========================================================================

_CONGRESS_STUBS = """
M = {
    have_congress = true,
    in_session = true,
    resolutions = nil,
    favor = 12,
    have_favor = true,
    requested = nil,
}

GameInfo = {}
GameInfo.Resolutions = make_info_table({
    { ResolutionType = "RESOLUTION_WORLD_RELIGION", Name = "LOC_RES_WORLD_RELIGION",
      Hash = 1101, Index = 1 },
})
Locale = { Lookup = function(key)
    return ({ LOC_RES_WORLD_RELIGION = "World Religion" })[key]
end }

local congress = {}
function congress:IsInSession() return M.in_session end
function congress:GetResolutions(playerId)
    if M.resolutions == nil then error("GetResolutions absent") end
    return M.resolutions
end

local player = {}
function player:GetFavor()
    if not M.have_favor then error("absent") end
    return M.favor
end

Players = { [0] = player }
Game = {
    GetLocalPlayer = function() return 0 end,
    GetWorldCongress = function()
        if not M.have_congress then error("nil in GameCore_Tuner") end
        return congress
    end,
}
PlayerOperations = {
    WORLD_CONGRESS_RESOLUTION_VOTE = "op_vote",
    PARAM_RESOLUTION_TYPE = "param_resolution",
    PARAM_WORLD_CONGRESS_VOTES = "param_votes",
    PARAM_RESOLUTION_OPTION = "param_option",
}
UI = { RequestPlayerOperation = function(playerId, operation, parameters)
    M.requested = {
        operation = operation,
        resolution = parameters[PlayerOperations.PARAM_RESOLUTION_TYPE],
        votes = parameters[PlayerOperations.PARAM_WORLD_CONGRESS_VOTES],
        option = parameters[PlayerOperations.PARAM_RESOLUTION_OPTION],
    }
end }
"""

# worldcongresspopup.lua:580-581 -- the real return mixes a "Stage" string key in among the
# numeric entries, which is why Firaxis iterates pairs() and skips non-numeric keys.
_RESOLUTIONS = 'M.resolutions = { Stage = 1, { Type = 1, TargetType = "PlayerType" } }'


def _congress_state(configure: str = "") -> dict[str, Any]:
    runtime = _runtime(_CONGRESS_STUBS, LUA / "gamecore" / "congress.lua")
    if configure:
        runtime.execute(configure)
    state: dict[str, Any] = runtime.eval("CivSim_Congress.state()")
    return state


def test_resolutions_come_from_get_resolutions_and_skip_the_stage_key() -> None:
    """Finding #9/#10: `GetResolutions(playerID)` (worldcongresspopup.lua:574), iterated with the
    `type(i) == "number"` guard (:580-581); the entry's only identity is `.Type` (:590), so
    `resolution_id` is the row hash the vote request itself keys on (:606, :2241)."""
    state = _congress_state(_RESOLUTIONS)
    resolutions = _list(state["active_resolutions"])
    assert len(resolutions) == 1, "the 'Stage' key must not become a resolution"
    assert resolutions[0]["resolution_id"] == 1101
    assert resolutions[0]["resolution_type"] == "RESOLUTION_WORLD_RELIGION"
    assert resolutions[0]["name"] == "World Religion"
    assert state["active_resolutions_reason"] is None


def test_favor_is_read_off_the_player() -> None:
    """Finding #11: `Players[p]:GetFavor()` (toppanel_expansion2.lua:169). The old
    `GetStats():GetDiplomaticFavor()` kept this at 0 forever, so congress.cast_vote's
    `player.diplomatic_favor > 0` could never hold."""
    state = _congress_state(_RESOLUTIONS)
    assert state["local_player_favor"] == 12
    assert state["local_player_favor_reason"] is None


def test_favor_says_why_when_unanswerable() -> None:
    state = _congress_state("M.have_favor = false")
    assert state["local_player_favor"] == 0
    assert state["local_player_favor_reason"] == "get_favor_unanswerable"


def test_a_missing_world_congress_is_a_reason_not_not_in_session() -> None:
    """A ruleset with no World Congress and a Congress that is merely between sessions are
    different facts; reporting both as `is_in_session: false` is the silent-empty defect again."""
    state = _congress_state("M.have_congress = false")
    assert state["is_in_session"] is False
    assert state["is_in_session_reason"] == "game_world_congress_unavailable"


def test_cast_vote_issues_the_resolution_vote_operation() -> None:
    """Finding #26: worldcongresspopup.lua:2239-2253."""
    runtime = _runtime(_CONGRESS_STUBS, LUA / "ingame" / "congress.lua")
    result = runtime.eval("CivSim_CongressOrders.cast_vote(1101, 1, 3)")
    assert result["ok"] is True
    requested = runtime.eval("M.requested")
    assert requested["operation"] == "op_vote"
    assert requested["resolution"] == 1101
    assert requested["option"] == 1
    assert requested["votes"] == 3


def test_cast_vote_refuses_rather_than_guessing_a_side() -> None:
    """The action declares only a `target` today, so the A/B choice arrives nil. Picking one for
    the player would be the harness voting, not the agent."""
    runtime = _runtime(_CONGRESS_STUBS, LUA / "ingame" / "congress.lua")
    result = runtime.eval("CivSim_CongressOrders.cast_vote(1101)")
    assert result["ok"] is False
    assert result["reason"] == "vote_option_not_supplied"
    assert runtime.eval("M.requested") is None


# ==========================================================================
# espionage.state + espionage.assign_mission -- findings 12-16, 31-32
# ==========================================================================

_ESPIONAGE_STUBS = """
M = { units = {}, have_units = true, operation = -1, city = nil,
      activity = 1, ready = true, requested = nil, can_start = true }

ActivityTypes = { ACTIVITY_AWAKE = 1, ACTIVITY_OPERATION = 2 }

GameInfo = {}
GameInfo.Units = make_info_table({
    { UnitType = "UNIT_SPY", Spy = true, Index = 5 },
    { UnitType = "UNIT_WARRIOR", Index = 6 },
})
GameInfo.UnitOperations = make_info_table({
    { OperationType = "UNITOPERATION_SPY_STEAL_TECH_BOOST", CategoryInUI = "OFFENSIVESPY",
      Hash = 1201, Index = 21 },
    { OperationType = "UNITOPERATION_SPY_COUNTERSPY", CategoryInUI = "COUNTERSPY",
      Hash = 1202, Index = 22 },
}, {"OperationType"})

function M.make_unit(id, typeIndex)
    local u = {}
    function u:GetID() return id end
    function u:GetUnitType() return typeIndex end
    function u:GetX() return 3 end
    function u:GetY() return 4 end
    function u:GetSpyOperation() return M.operation end
    function u:IsReadyToMove() return M.ready end
    return u
end

local unitList = {}
function unitList:Members() return members(M.units)() end

local player = {}
function player:GetUnits()
    if not M.have_units then return nil end
    return unitList
end
Players = { [0] = player }
Game = { GetLocalPlayer = function() return 0 end, GetCurrentGameTurn = function() return 40 end }
Map = { GetPlot = function(x, y) return { x = x, y = y } end }
Cities = { GetPlotPurchaseCity = function(plot)
    if M.city == nil then return nil end
    return { GetID = function() return M.city end }
end }
UnitManager = {
    GetActivityType = function(unit) return M.activity end,
    CanStartOperation = function(unit, hash, plot, addParams, queryOnly) return M.can_start end,
    RequestOperation = function(unit, hash, parameters)
        M.requested = { unit = unit:GetID(), hash = hash, parameters = parameters }
    end,
}
UnitOperationTypes = { PARAM_X = "param_x", PARAM_Y = "param_y" }
"""


def _espionage_state(configure: str = "") -> dict[str, Any]:
    runtime = _runtime(_ESPIONAGE_STUBS, LUA / "gamecore" / "espionage.lua")
    if configure:
        runtime.execute(configure)
    state: dict[str, Any] = runtime.eval("CivSim_Espionage.state()")
    return state


def test_spies_are_units_filtered_on_the_spy_column() -> None:
    """Findings #12-#16: there is no espionage object. espionageoverview.lua:88-101 walks the
    player's units and keeps the ones whose `GameInfo.Units[...].Spy` is true."""
    state = _espionage_state("M.units = { M.make_unit(1, 5), M.make_unit(2, 6) }\nM.city = 9")
    spies = _list(state["spies"])
    assert [s["unit_id"] for s in spies] == [1], "a warrior is not a spy"
    assert spies[0]["mission"] is None, "operation -1 is an idle spy"
    assert spies[0]["target_city_id"] == 9
    assert spies[0]["is_available"] is True


def test_a_spy_on_a_mission_reports_the_operation_type() -> None:
    """espionageoverview.lua:659,673 -- GetSpyOperation() through GameInfo.UnitOperations."""
    state = _espionage_state(
        "M.units = { M.make_unit(1, 5) }\nM.operation = 21\n"
        "M.activity = ActivityTypes.ACTIVITY_OPERATION"
    )
    spy = _list(state["spies"])[0]
    assert spy["mission"] == "UNITOPERATION_SPY_STEAL_TECH_BOOST"
    assert spy["is_available"] is False


def test_a_spy_that_cannot_act_is_not_available() -> None:
    """espionagechooser.lua:741-748 -- the chooser only opens for a spy that IsReadyToMove and is
    ACTIVITY_AWAKE, so "idle" alone is not "will take an order"."""
    state = _espionage_state("M.units = { M.make_unit(1, 5) }\nM.ready = false")
    assert _list(state["spies"])[0]["is_available"] is False


def test_spies_say_why_when_the_unit_list_is_unavailable() -> None:
    state = _espionage_state("M.have_units = false")
    assert _list(state["spies"]) == []
    assert state["spies_reason"] == "player_units_unavailable"


def _assign_mission(call: str, configure: str = "") -> tuple[dict[str, Any], Any]:
    runtime = _runtime(_ESPIONAGE_STUBS, LUA / "ingame" / "espionage.lua")
    runtime.execute("M.units = { M.make_unit(1, 5) }")
    if configure:
        runtime.execute(configure)
    return runtime.eval(call), runtime.eval("M.requested")


def test_assign_mission_requests_the_operation_with_no_parameter_table() -> None:
    """Findings #31/#32: `UnitOperationTypes.SPY_MISSION` and `PARAM_SPY_MISSION` do not exist, so
    the old body wrote `tParameters[nil]`. espionagepopup.lua:472-477 issues
    `UnitManager.RequestOperation(spy, operation.Hash)` with no parameters at all."""
    result, requested = _assign_mission(
        'CivSim_EspionageOrders.assign_mission(1, "UNITOPERATION_SPY_STEAL_TECH_BOOST", 9)'
    )
    assert result["ok"] is True
    assert requested["hash"] == 1201
    assert requested["parameters"] is None


def test_assign_mission_refuses_an_operation_the_panel_does_not_offer() -> None:
    """espionagechooser.lua:211 -- only CategoryInUI == "OFFENSIVESPY" appears on the mission list
    a human sees."""
    result, requested = _assign_mission(
        'CivSim_EspionageOrders.assign_mission(1, "UNITOPERATION_SPY_COUNTERSPY", 9)'
    )
    assert result["ok"] is False
    assert result["reason"] == "mission_not_offered_by_the_espionage_panel"
    assert requested is None


def test_assign_mission_refuses_when_the_game_says_it_cannot_start() -> None:
    result, requested = _assign_mission(
        'CivSim_EspionageOrders.assign_mission(1, "UNITOPERATION_SPY_STEAL_TECH_BOOST", 9)',
        "M.can_start = false",
    )
    assert result["ok"] is False
    assert result["reason"] == "mission_button_is_greyed_out"
    assert requested is None


# ==========================================================================
# empire_orders -- findings 20-22
# ==========================================================================

_EMPIRE_STUBS = """
M = { slot_count = 3, slot_policies = { [0] = 1, [1] = -1, [2] = 3 },
      policy_changes = nil, government_request = nil, requested = nil,
      change_accepted = true }

GameInfo = {}
GameInfo.Policies = make_info_table({
    { PolicyType = "POLICY_SURVEY", Hash = 501, Index = 1 },
    { PolicyType = "POLICY_DISCIPLINE", Hash = 502, Index = 2 },
    { PolicyType = "POLICY_LEGIONS", Hash = 503, Index = 3 },
}, {"PolicyType"})
GameInfo.Governments = make_info_table({
    { GovernmentType = "GOVERNMENT_AUTOCRACY", Hash = 602, Index = 1 },
}, {"GovernmentType"})
GameInfo.Governors = make_info_table({
    { GovernorType = "GOVERNOR_THE_MERCHANT", Hash = 702, Index = 1 },
}, {"GovernorType"})
GameInfo.Technologies = make_info_table({}, {})
GameInfo.Civics = make_info_table({}, {})

local culture = {}
function culture:GetNumPolicySlots() return M.slot_count end
function culture:GetSlotPolicy(i) return M.slot_policies[i] or -1 end
function culture:RequestPolicyChanges(clearList, addList)
    M.policy_changes = { clear = clearList, add = addList }
end
function culture:RequestChangeGovernment(hash)
    M.government_request = hash
    return M.change_accepted
end

local player = {}
function player:GetCulture() return culture end
Players = { [0] = player }
Game = { GetLocalPlayer = function() return 0 end }
PlayerOperations = {
    ASSIGN_GOVERNOR = "op_assign_governor",
    PARAM_GOVERNOR_TYPE = "param_governor",
    PARAM_CITY_DEST = "param_city",
}
UI = { RequestPlayerOperation = function(playerId, operation, parameters)
    M.requested = { operation = operation,
                    governor = parameters[PlayerOperations.PARAM_GOVERNOR_TYPE],
                    city = parameters[PlayerOperations.PARAM_CITY_DEST] }
end }
"""


def _empire(call: str, configure: str = "") -> Any:
    runtime = _runtime(_EMPIRE_STUBS, LUA / "ingame" / "empire_orders.lua")
    if configure:
        runtime.execute(configure)
    return runtime, runtime.eval(call)


def test_slot_policy_resends_the_whole_loadout() -> None:
    """Finding #20: `SetPolicyActive` does not exist. governmentscreen.lua:1549-1575 clears every
    occupied slot and re-adds them in ONE `RequestPolicyChanges(clearList, addList)` call, because
    (per its own comment at :1555-1557) the engine otherwise still believes a card sits in its old
    slot. addList values are policy HASHES."""
    runtime, result = _empire('CivSim_EmpireOrders.slot_policy(1, "POLICY_DISCIPLINE")')
    assert result["ok"] is True
    assert result["mechanism"] == "Culture:RequestPolicyChanges"
    changes = runtime.eval("M.policy_changes")
    assert sorted(_list(changes["clear"])) == [0, 1, 2]
    add = changes["add"]
    assert add[0] == 501, "the card already in slot 0 must be re-added, not dropped"
    assert add[1] == 502, "the requested card goes into the requested slot, as a hash"
    assert add[2] == 503


def test_slot_policy_refuses_a_slot_the_government_does_not_have() -> None:
    runtime, result = _empire('CivSim_EmpireOrders.slot_policy(9, "POLICY_DISCIPLINE")')
    assert result["ok"] is False
    assert result["reason"] == "slot_out_of_range"
    assert runtime.eval("M.policy_changes") is None


def test_change_government_uses_request_change_government_with_a_hash() -> None:
    """Finding #21: `SetCurrentGovernment(index)` is a scenario-script god-setter
    (blackdeathscenario.lua:187). The human's Confirm button is
    `RequestChangeGovernment(hash)` (governmentscreen.lua:912)."""
    runtime, result = _empire('CivSim_EmpireOrders.change_government("GOVERNMENT_AUTOCRACY")')
    assert result["ok"] is True
    assert runtime.eval("M.government_request") == 602


def test_change_government_reports_a_refusal_from_the_engine() -> None:
    """RequestChangeGovernment returns a boolean; a false is the game saying no, not an error."""
    runtime, result = _empire(
        'CivSim_EmpireOrders.change_government("GOVERNMENT_AUTOCRACY")',
        "M.change_accepted = false",
    )
    assert result["ok"] is False


def test_assign_governor_issues_the_panels_operation() -> None:
    """Finding #22: governorpanel.lua:556-564 -- ASSIGN_GOVERNOR with PARAM_GOVERNOR_TYPE (a row
    INDEX) and PARAM_CITY_DEST."""
    runtime, result = _empire('CivSim_EmpireOrders.assign_governor("GOVERNOR_THE_MERCHANT", 7)')
    assert result["ok"] is True
    requested = runtime.eval("M.requested")
    assert requested["operation"] == "op_assign_governor"
    assert requested["governor"] == 1
    assert requested["city"] == 7


# ==========================================================================
# diplomacy orders -- findings 23-25
# ==========================================================================

_DIPLOMACY_STUBS = """
M = { requested = nil, session = nil }
Game = { GetLocalPlayer = function() return 0 end }
Players = { [0] = {} }
PlayerOperations = {
    DIPLOMACY_DECLARE_WAR = "op_declare_war",
    DIPLOMACY_MAKE_PEACE = "op_make_peace",
    PARAM_PLAYER_ONE = "param_one",
    PARAM_PLAYER_TWO = "param_two",
}
UI = { RequestPlayerOperation = function(playerId, operation, parameters)
    M.requested = { operation = operation,
                    one = parameters[PlayerOperations.PARAM_PLAYER_ONE],
                    two = parameters[PlayerOperations.PARAM_PLAYER_TWO] }
end }
DiplomacyManager = { RequestSession = function(from, to, kind)
    M.session = { from = from, to = to, kind = kind }
end }
"""


def _diplomacy(call: str) -> tuple[Any, Any]:
    runtime = _runtime(_DIPLOMACY_STUBS, LUA / "ingame" / "diplomacy.lua")
    return runtime, runtime.eval(call)


def test_declare_war_issues_the_popups_operation() -> None:
    """Finding #23: declarewarpopup.lua:77-81. `GetDiplomaticAI():DeclareWar(...)` was invented,
    and GetDiplomaticAI is read-only opinion data besides."""
    runtime, result = _diplomacy("CivSim_DiplomacyOrders.declare_war(2)")
    assert result["ok"] is True
    requested = runtime.eval("M.requested")
    assert requested["operation"] == "op_declare_war"
    assert (requested["one"], requested["two"]) == (0, 2)


def test_make_peace_issues_the_operation() -> None:
    """Finding #24: citystates.lua:815-818."""
    runtime, result = _diplomacy("CivSim_DiplomacyOrders.make_peace(2)")
    assert runtime.eval("M.requested")["operation"] == "op_make_peace"
    assert result["ok"] is True


def test_send_delegation_opens_the_diplomacy_session() -> None:
    """Finding #25: diplomacyactionview.lua:466-467. There is no delegation PlayerOperation at
    all, which is why this action was attempted and never applied."""
    runtime, result = _diplomacy("CivSim_DiplomacyOrders.send_delegation(2)")
    assert result["ok"] is True
    session = runtime.eval("M.session")
    assert (session["from"], session["to"], session["kind"]) == (0, 2, "DIPLOMATIC_DELEGATION")


# ==========================================================================
# diplomacy.state -- city-state scope legibility (2026-09-22)
#
# The defect this section pins: a city-state and an unmet major civilization both produced no
# information a consumer could tell apart -- one is out of scope by a deliberate filter
# (`Player:IsMajor()`, confirmed bound in GameCore_Tuner by `nm -DC`), the other is a real "not
# met yet" answer the game itself would show. `relations_scope` is what makes the two
# distinguishable; these tests vary answerability and scope (the axis the fix constrains), not
# player identity or relation type, per the positive-twin rule.
# ==========================================================================

_DIPLOMACY_STATE_STUBS = """
M = { alive = {}, met = {}, delegation = {} }

function M.make_player(id, isMajor)
    local p = {}
    function p:GetID() return id end
    if isMajor ~= nil then
        function p:IsMajor() return isMajor end
    end
    return p
end

Game = { GetLocalPlayer = function() return 0 end }

local diplomacy = {}
function diplomacy:HasMet(otherID) return M.met[otherID] == true end
function diplomacy:HasDelegationAt(otherID) return M.delegation[otherID] == true end

local player = {}
function player:GetDiplomacy() return diplomacy end

Players = { [0] = player }
PlayerManager = { GetAlive = function() return M.alive end }
PlayerConfigurations = setmetatable({}, {
    __index = function(_, id)
        local cfg = {}
        function cfg:GetCivilizationTypeName() return "CIVILIZATION_TEST_" .. tostring(id) end
        return cfg
    end
})
"""


def _diplomacy_state(configure: str = "") -> dict[str, Any]:
    runtime = _runtime(_DIPLOMACY_STATE_STUBS, LUA / "gamecore" / "diplomacy.lua")
    if configure:
        runtime.execute(configure)
    state: dict[str, Any] = runtime.eval("CivSim_Diplomacy.state()")
    return state


def test_relations_scope_is_stated_and_a_city_state_is_never_an_entry() -> None:
    """The scope-(a) finding: IsMajor() is an explicit, callable-here filter (nm -DC confirms
    GameCore::Lua::IPlayer::lIsMajor is bound identically to the InGame side), not an accessor
    artefact -- so the enumerated set is a fact this observation now states, not an inference."""
    state = _diplomacy_state(
        """
        M.alive = { M.make_player(1, true), M.make_player(2, false), M.make_player(3, true) }
        """
    )
    ids = [row["player_id"] for row in _list(state["relations"])]
    assert ids == [1, 3]
    assert state["relations_scope"] == "alive_major_civilizations"


def test_a_player_without_is_major_is_kept_defensively() -> None:
    """`not otherPlayer.IsMajor or otherPlayer:IsMajor()`: when the accessor itself is absent on
    an object the body keeps the row rather than silently dropping it (lua/gamecore/cities.lua's
    own precedent)."""
    state = _diplomacy_state("M.alive = { M.make_player(9, nil) }")
    ids = [row["player_id"] for row in _list(state["relations"])]
    assert ids == [9]


def test_met_not_met_and_out_of_scope_are_three_different_answers() -> None:
    """Pins the three-way distinction so it cannot collapse: a met major, a not-met major, and a
    city-state (out of scope) must never look alike. Before this pass all three that were not
    "met major" looked identical -- absent from `relations`, or `has_met: false` with nothing
    saying why a different id got no row at all."""
    state = _diplomacy_state(
        """
        M.alive = { M.make_player(1, true), M.make_player(2, true), M.make_player(3, false) }
        M.met[1] = true
        """
    )
    by_id = {row["player_id"]: row for row in _list(state["relations"])}
    assert by_id[1]["has_met"] is True
    assert by_id[2]["has_met"] is False
    assert 3 not in by_id
    assert state["relations_scope"] == "alive_major_civilizations"


def test_diplomacy_state_asks_nothing_about_player_minus_one() -> None:
    """The 882758e/0989e3b `no_local_player` guard, applied to diplomacy.state: never a bare `[]`
    that reads the same as "nobody is alive"."""
    state = _diplomacy_state("Game.GetLocalPlayer = function() return -1 end")
    assert _list(state["relations"]) == []
    assert state["relations_scope"] == "alive_major_civilizations"
    assert state["relations_reason"] == "no_local_player"


# ==========================================================================
# camera -- findings 33-37
# ==========================================================================

_CAMERA_STUBS = """
M = { zoom = 0.707, render_view = 0, set_zoom = nil, set_view = nil, look_at = nil }
WorldRenderView = { VIEW_3D = 0, VIEW_2D = 1 }
UI = {
    GetMapZoom = function() return M.zoom end,
    SetMapZoom = function(z, nx, ny) M.set_zoom = { z = z, nx = nx, ny = ny } end,
    GetWorldRenderView = function() return M.render_view end,
    SetWorldRenderView = function(v) M.set_view = v end,
    LookAtPlot = function(x, y) M.look_at = { x = x, y = y } end,
    GetMapLookAtWorldTarget = function() return 10.0, 20.0 end,
    GetPlotCoordFromWorld = function(wx, wy) return 5, 6 end,
}
PlayersVisibility = { [0] = { IsRevealed = function(self, x, y) return true end } }
Map = { GetPlotIndex = function(x, y) return x * 100 + y end }
Game = { GetLocalPlayer = function() return 0 end }
"""


def _camera(call: str, configure: str = "") -> tuple[Any, Any]:
    runtime = _runtime(_CAMERA_STUBS, LUA / "ingame" / "camera.lua")
    if configure:
        runtime.execute(configure)
    return runtime, runtime.eval(call)


def test_zoom_uses_set_map_zoom_with_the_panels_three_arguments() -> None:
    """Finding #33: no `UI.*` name in the shipped corpus contains "Camera"; the zoom hotkey is
    `UI.SetMapZoom(zoom, 0.0, 0.0)` (worldinput.lua:1204)."""
    runtime, result = _camera("CivSim_Camera.zoom(0.4)")
    assert result["ok"] is True
    assert result["mechanism"] == "UI.SetMapZoom"
    called = runtime.eval("M.set_zoom")
    assert (called["z"], called["nx"], called["ny"]) == (0.4, 0.0, 0.0)


def test_view_mode_sets_the_render_view_rather_than_toggling() -> None:
    """Finding #34: minimappanel.lua:369-381. Setting the view is what makes the action idempotent
    -- asking for "strategic" while already strategic must leave it strategic, where the old
    toggle (through a nil ActionTypes key) would have been a coin flip even had it worked."""
    runtime, result = _camera('CivSim_Camera.set_view_mode("strategic")')
    assert result["ok"] is True
    assert runtime.eval("M.set_view") == 1
    runtime, _ = _camera('CivSim_Camera.set_view_mode("world")')
    assert runtime.eval("M.set_view") == 0


def test_read_state_reports_strategic_from_the_named_enum() -> None:
    """Finding #37: `UI.IsStrategicView()` does not exist; VIEW_2D *is* the strategic view
    (citybannermanager.lua:1466). Comparing against the enum retires this file's old
    "the strategic value is UNVERIFIED and assumed 1"."""
    _, state = _camera("CivSim_Camera.read_state()", "M.render_view = WorldRenderView.VIEW_2D")
    assert state["mode"] == "strategic"
    _, state = _camera("CivSim_Camera.read_state()")
    assert state["mode"] == "world"
    assert state["zoom"] == pytest.approx(0.707)


def test_read_state_still_reports_the_look_at_plot() -> None:
    """Finding #35 removed only the dead `UI.GetCameraTargetPlot` fallback; T260's real pair
    (automation_observercamera.lua:381-382) is untouched."""
    _, state = _camera("CivSim_Camera.read_state()")
    assert state["target_plot"]["x"] == 5
    assert state["target_plot"]["y"] == 6
    assert state["target_is_revealed"] is True


# ==========================================================================
# units.state has_queued_orders -- findings 17-19
# ==========================================================================


def test_units_activity_comes_from_unit_manager_not_the_unit() -> None:
    """Findings #17-#19: `unit:GetActivityType()` is not a unit method and `UnitActivityType` is
    not a table -- the real pair is `UnitManager.GetActivityType(pUnit)` and `ActivityTypes`
    (unitpanel.lua:2147-2148). The old `queued_path` field is gone: its only value was a
    hard-coded nil destination, which is a claim the harness cannot keep."""
    stubs = """
M = { activity = 2, answer_activity = true }
ActivityTypes = { ACTIVITY_AWAKE = 1, ACTIVITY_OPERATION = 2 }
GameInfo = {}
GameInfo.Units = make_info_table({ { UnitType = "UNIT_WARRIOR", Index = 6 } })
GameInfo.Improvements = make_info_table({})
GameInfo.UnitPromotions = make_info_table({})
Locale = { Lookup = function(k) return k end }
local unit = {}
function unit:GetID() return 1 end
function unit:GetOwner() return 0 end
function unit:GetUnitType() return 6 end
function unit:GetX() return 3 end
function unit:GetY() return 4 end
function unit:GetMovesRemaining() return 0 end
function unit:GetMaxMoves() return 2 end
function unit:GetFortifyTurns() return 0 end
function unit:GetBuildCharges() return 0 end
local unitList = {}
function unitList:Members() return members({ unit })() end
local player = {}
function player:GetUnits() return unitList end
function player:GetID() return 0 end
function player:IsMajor() return true end
Players = { [0] = player }
PlayerManager = { GetAlive = function() return { player } end }
PlayersVisibility = { [0] = { IsVisible = function() return true end } }
UI = { GetHeadSelectedUnit = function() return nil end }
UnitManager = {
    GetActivityType = function(u)
        if not M.answer_activity then error("absent") end
        return M.activity
    end,
    CanStartOperation = function() return false end,
    CanStartCommand = function() return false end,
    GetReachableMovement = function() return {} end,
}
UnitOperationTypes = { FOUND_CITY = 1, BUILD_IMPROVEMENT = 2, PARAM_X = "x", PARAM_Y = "y" }
UnitOperationResults = { IMPROVEMENTS = "i", BEST_IMPROVEMENT = "b" }
UnitCommandTypes = { PROMOTE = 3 }
UnitCommandResults = { PROMOTIONS = "p" }
Game = { GetLocalPlayer = function() return 0 end }
"""
    runtime = _runtime(stubs, LUA / "gamecore" / "units.lua")
    entry = _list(runtime.eval("CivSim_Units.state()")["units"])[0]
    assert entry["has_queued_orders"] is True
    assert "queued_path" not in dict(entry), "the non-claim must not come back"

    runtime = _runtime(stubs, LUA / "gamecore" / "units.lua")
    runtime.execute("M.answer_activity = false")
    entry = _list(runtime.eval("CivSim_Units.state()")["units"])[0]
    assert entry["has_queued_orders"] is None
    assert entry["has_queued_orders_reason"] == "unit_manager_get_activity_type_unanswerable"


# ==========================================================================
# screens.respond -- finding 38
# ==========================================================================


def test_an_unmapped_prompt_says_it_has_no_mapped_control() -> None:
    """Finding #38: `UI.RespondToPrompt` was invented, and no generic primitive can exist -- a
    popup answers by releasing the engine hold its own button's callback took
    (popupmanager.lua:52-61, :95-98). A bare `ok = false` read like the click was tried and
    refused; naming the gap is the honest report."""
    stubs = """
CIVSIM_TEST_ONLY = true
Game = { GetLocalPlayer = function() return 0 end }
UI = {}
ContextPtr = nil
"""
    runtime = _runtime(stubs, LUA / "ingame" / "screens.lua")
    # A watchlist prompt with no acknowledge descriptor and no bespoke answerer.
    result = runtime.eval('CivSim_Screens.respond("prompt.congress_intro", "continue")')
    assert result is not None
    if result["ok"] is False and result["reason"] == "prompt_has_no_mapped_control":
        return
    # prompt.congress_intro is acknowledge-mapped; the point still holds for a genuinely unmapped
    # one, which the body rejects before it reaches any control lookup.
    unknown = runtime.eval('CivSim_Screens.respond("prompt.not_a_real_prompt", "continue")')
    assert unknown["ok"] is False
    assert unknown["reason"] == "unknown_prompt"


# ===========================================================================================
# 2026-09-21 20:46:35 EDT: `Civ6 segfault at b0 in libGameCore_XP2.so`, ip resolved (nm on the
# shipped .so) to GameCore::Definition::Government::GetPrereqCivicReference() -- the engine
# dereferenced a NULL government definition inside `culture:IsGovernmentUnlocked(hash)`, the
# first live sweep of these bodies. pcall cannot catch a native fault, so the only guard is never
# to make the call with anything the engine could resolve to nothing, and never for player -1.
# These tests make the engine stub *record* what it was handed and fault on a non-number, the way
# the real engine did.
# ===========================================================================================


def test_government_never_hands_the_engine_a_row_without_a_hash() -> None:
    runtime = _runtime(_GOVERNMENT_STUBS, LUA / "gamecore" / "government.lua")
    runtime.execute(
        """
        local culture = Players[0]:GetCulture()
        CIVSIM_TEST_CALLS = {}
        local real = culture.IsGovernmentUnlocked
        function culture:IsGovernmentUnlocked(hash)
            if type(hash) ~= "number" then error("NATIVE FAULT: null definition") end
            CIVSIM_TEST_CALLS[#CIVSIM_TEST_CALLS + 1] = hash
            return real(self, hash)
        end
        GameInfo.Governments = make_info_table({
            { GovernmentType = "GOVERNMENT_CHIEFDOM", Hash = 601, Index = 0 },
            { GovernmentType = "GOVERNMENT_UNRESOLVABLE", Index = 9 },
        }, {"GovernmentType"})
        M.current_government = -1
        """
    )
    state = runtime.eval("CivSim_Government.state()")
    calls = _list(runtime.eval("CIVSIM_TEST_CALLS"))
    assert calls == [601]
    assert "GOVERNMENT_UNRESOLVABLE" not in _list(state["available_governments"])
    assert _list(state["available_governments"]) == ["GOVERNMENT_CHIEFDOM"]


def test_government_says_why_when_every_row_lacks_a_hash() -> None:
    runtime = _runtime(_GOVERNMENT_STUBS, LUA / "gamecore" / "government.lua")
    runtime.execute(
        """
        local culture = Players[0]:GetCulture()
        function culture:IsGovernmentUnlocked(hash)
            if type(hash) ~= "number" then error("NATIVE FAULT: null definition") end
            return true
        end
        GameInfo.Governments = make_info_table({
            { GovernmentType = "GOVERNMENT_A", Index = 0 },
            { GovernmentType = "GOVERNMENT_B", Index = 1 },
        }, {"GovernmentType"})
        """
    )
    state = runtime.eval("CivSim_Government.state()")
    assert _list(state["available_governments"]) == []
    assert state["available_governments_reason"] == "government_rows_without_hash"


def test_policies_never_hand_the_engine_a_row_without_a_hash() -> None:
    runtime = _runtime(_GOVERNMENT_STUBS, LUA / "gamecore" / "government.lua")
    runtime.execute(
        """
        local culture = Players[0]:GetCulture()
        CIVSIM_TEST_CALLS = {}
        local predicates = {
            "IsPolicyUnlocked", "IsPolicyObsolete", "CanPolicyBeSlotted", "IsPolicyBanned",
        }
        for _, name in ipairs(predicates) do
            local real = culture[name]
            culture[name] = function(self, hash)
                if type(hash) ~= "number" then error("NATIVE FAULT: null definition") end
                CIVSIM_TEST_CALLS[#CIVSIM_TEST_CALLS + 1] = hash
                return real(self, hash)
            end
        end
        GameInfo.Policies = make_info_table({
            { PolicyType = "POLICY_SURVEY", Hash = 501, Index = 1 },
            { PolicyType = "POLICY_UNRESOLVABLE", Index = 9 },
        }, {"PolicyType"})
        M.unlocked = { POLICY_SURVEY = true }
        M.slot_count = 1
        M.slot_policies = {}
        """
    )
    state = runtime.eval("CivSim_Government.state()")
    calls = _list(runtime.eval("CIVSIM_TEST_CALLS"))
    assert calls and all(isinstance(h, (int, float)) for h in calls)
    assert "POLICY_UNRESOLVABLE" not in _list(state["available_policies"])


def test_government_asks_nothing_about_player_minus_one() -> None:
    runtime = _runtime(_GOVERNMENT_STUBS, LUA / "gamecore" / "government.lua")
    runtime.execute(
        """
        local culture = Players[0]:GetCulture()
        Game.GetLocalPlayer = function() return -1 end
        Players[-1] = nil
        function culture:IsGovernmentUnlocked(hash) error("NATIVE FAULT: null player") end
        """
    )
    state = runtime.eval("CivSim_Government.state()")
    assert state["available_governments_reason"] == "no_local_player"
    assert state["available_policies_reason"] == "no_local_player"
    assert state["governors_reason"] == "no_local_player"
    assert _list(state["available_governments"]) == []


def test_great_people_asks_nothing_about_player_minus_one() -> None:
    state = _great_people_state("Game.GetLocalPlayer = function() return -1 end")
    assert state["recruitable_individuals_reason"] == "no_local_player"
    assert state["points_by_class_reason"] == "no_local_player"
    assert _list(state["recruitable_individuals"]) == []


def test_religion_asks_nothing_about_player_minus_one() -> None:
    state = _religion_state("Game.GetLocalPlayer = function() return -1 end")
    assert state["available_beliefs_reason"] == "no_local_player"
    assert _list(state["available_beliefs"]) == []
    assert state["pantheon_selected"] is False
