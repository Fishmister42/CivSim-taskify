"""Which commands the game is offering right now, and -- when it is not -- why (T262).

**The measured problem.** Availability predicates were evaluated in exactly one place: the
executor, at dispatch, *after* the agent had already chosen (``act/dispatch.py``). The agent was
never shown them, so the rendered action catalog listed all 38 catalog actions as if all 38 were
possible. On the live store as of 2026-09-21, 23 actions had been attempted and only 9 ever
applied; nine of the fourteen never-applied actions were draws for a situation that was not on
screen -- a promotion with no promotable unit, a pantheon with no faith, a peace deal with nobody
at war, a prompt answer with no prompt up -- every one refused at dispatch as
``unavailable_to_human_now`` without ever reaching the game.

A human does not make those draws, and not because they are a better player: the button is greyed
out. This module computes the same thing the greyed-out button encodes, from the catalog's own
``availability_predicate`` and the current ``Observation``, so ``agent/context.py`` can render the
catalog in the two groups a human's screen already shows. **Principle I check**: nothing here
reads anything the agent's own context does not already carry. The inputs are the parity-filtered
``Observation`` (the same object rendered as observed state, entry for entry) and the catalog
declaration's own predicate; the output is a boolean and an English phrase built from those two.
No store, no live game, no hidden state -- and the predicate's *source text* never reaches the
agent, only the plain-language consequence of it, which is what a tooltip says.

**Choosing an unavailable action stays legal.** This module ranks and explains; it forbids
nothing. ``act/dispatch.py`` remains the only authority on whether an action may proceed, the
refusal path is a real recorded behaviour, and an agent that chooses from the "not available now"
group gets exactly the refusal it would have got before -- now having been told first.

**Three kinds of conjunct, because a predicate is not one question.** A predicate is split into
its top-level ``and`` operands (``act.predicates.split_conjuncts``) and each is answered the way
it can honestly be answered *before a target is named*:

1. **Decidable now** -- no ``target``, no target-resolved subject (``game.is_local_player_turn``,
   ``unit.is_selected``, ``player.religion_founded``). Evaluated directly, with the action's own
   selected unit/city bound as ``target`` exactly as ``dispatch`` would bind it.
2. **"Pick one of these"** -- ``target in <collection>``. The *choice* is the agent's, but whether
   there is anything to choose is on screen: an empty ``player.available_beliefs`` is a greyed-out
   pantheon button. Only the collection's emptiness is judged, never which element is right.
3. **"Any of these?"** -- a conjunct over ``other_player``/``congress``/``great_person``/``spy``,
   which the harness resolves from ``target`` alone. Answered by binding each id the observation
   actually shows for that namespace in turn and asking whether *any* satisfies the group. "Make
   peace" is available when some civilization is at war with you, and greyed out when none is --
   which is the question, and it is one the observation can answer.

Anything else that mentions ``target`` (``target >= 0.05``, ``target.is_revealed``) is left
undecided and therefore rendered available: its answer genuinely depends on a value the agent has
not chosen yet, and claiming otherwise would grey out a button the game does not.

A predicate that cannot be evaluated at all is reported **unavailable**, matching
``dispatch_action``'s own fail-closed rule (a predicate that will not evaluate is never read as
available).
"""

from __future__ import annotations

import ast
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from civsim_harness.act.predicates import (
    TARGET_RESOLVED_SUBJECTS,
    PredicateEvaluationError,
    build_predicate_bindings,
    evaluate_expression,
    evaluate_predicate,
    membership_collection,
    predicate_root_names,
    resolve_selected_subject_target,
    split_conjuncts,
    subject_candidate_targets,
)
from civsim_harness.models.catalog import DeclarationKind, ParityDeclaration
from civsim_harness.models.turn import Observation

__all__ = [
    "ActionAvailability",
    "availability_by_action",
    "evaluate_action_availability",
]


@dataclass(frozen=True)
class ActionAvailability:
    """One action's availability against one observation, with the reason when it is not.

    *reason* is one short human-facing phrase -- the tooltip, not the predicate. It is always
    empty when *available*, and never empty when it is not.
    """

    declaration_id: str
    available: bool
    reason: str = ""

    def __post_init__(self) -> None:
        if self.available and self.reason:
            raise ValueError("an available action carries no reason")
        if not self.available and not self.reason:
            raise ValueError("an unavailable action must say why")


def evaluate_action_availability(
    declaration: ParityDeclaration, observation: Observation
) -> ActionAvailability:
    """Whether *declaration* is a command the game is offering at *observation*, and why not.

    A declaration that is not an action (or carries no predicate, which the model forbids for an
    action) is reported available: this function's job is to grey out what the game greys out,
    never to invent a restriction of its own.
    """
    declaration_id = str(declaration.declaration_id)
    predicate = declaration.availability_predicate
    if declaration.kind is not DeclarationKind.ACTION or not predicate:
        return ActionAvailability(declaration_id, True)

    selected = resolve_selected_subject_target(declaration.declaration_id, observation)
    bindings = build_predicate_bindings(observation=observation, target=selected)

    subject_conjuncts: list[str] = []
    for conjunct in split_conjuncts(predicate):
        roots = predicate_root_names(conjunct)
        if roots & TARGET_RESOLVED_SUBJECTS:
            subject_conjuncts.append(conjunct)
            continue
        collection = membership_collection(conjunct)
        if collection is not None:
            empty = _nothing_to_choose(collection, bindings)
            if empty is not None:
                return ActionAvailability(declaration_id, False, empty)
            continue
        if "target" in roots:
            # Undecidable until the agent names a target -- see the module docstring.
            continue
        failure = _check(conjunct, bindings)
        if failure is not None:
            return ActionAvailability(declaration_id, False, failure)

    if subject_conjuncts:
        failure = _check_subject(subject_conjuncts, observation)
        if failure is not None:
            return ActionAvailability(declaration_id, False, failure)

    return ActionAvailability(declaration_id, True)


def availability_by_action(
    declarations: Iterable[ParityDeclaration], observation: Observation
) -> dict[str, ActionAvailability]:
    """:func:`evaluate_action_availability` for every action in *declarations*, keyed by its id.

    Non-action declarations are skipped entirely (they are not commands and are never rendered as
    such), so the result's keys are exactly the action ids the caller passed in.
    """
    return {
        str(declaration.declaration_id): evaluate_action_availability(declaration, observation)
        for declaration in declarations
        if declaration.kind is DeclarationKind.ACTION
    }


# --------------------------------------------------------------------------
# Answering one conjunct
# --------------------------------------------------------------------------

#: What is said when a predicate will not evaluate against what the game reported. The action is
#: reported unavailable, matching ``dispatch_action``'s fail-closed rule -- and the phrasing says
#: what the agent can actually act on (the board does not show it) rather than naming an
#: evaluator failure, which is harness internals.
_UNEVALUABLE: Final = "the board is not showing what this command needs"


def _check(conjunct: str, bindings: Mapping[str, Any]) -> str | None:
    """``None`` when *conjunct* holds against *bindings*; the reason it does not, otherwise."""
    try:
        if evaluate_predicate(conjunct, bindings):
            return None
    except PredicateEvaluationError:
        return _UNEVALUABLE
    return _phrase(conjunct, bindings)


def _nothing_to_choose(collection: str, bindings: Mapping[str, Any]) -> str | None:
    """``None`` when *collection* offers at least one value to pick; the reason it does not."""
    try:
        value = evaluate_expression(collection, bindings)
    except PredicateEvaluationError:
        return _UNEVALUABLE
    if value is None:
        return f"the board is not showing {collection}"
    if isinstance(value, (str, bytes, list, tuple, set, frozenset, Mapping)) and not value:
        return f"there is nothing to choose: {collection} is empty"
    return None


def _check_subject(conjuncts: Sequence[str], observation: Observation) -> str | None:
    """``None`` when some id the observation shows satisfies every one of *conjuncts*.

    This is the "any of these?" question of the module docstring. When more than one
    target-resolved subject appears in one predicate there is no single list to walk, so the
    question is left undecided rather than answered by a guess.
    """
    namespaces = sorted(
        {name for conjunct in conjuncts for name in predicate_root_names(conjunct)}
        & TARGET_RESOLVED_SUBJECTS
    )
    if len(namespaces) != 1:
        return None
    namespace = namespaces[0]
    candidates = subject_candidate_targets(namespace, observation)
    if not candidates:
        return f"the board is showing no {_subject_words(namespace)}"

    reasons: list[str] = []
    for candidate in candidates:
        bindings = build_predicate_bindings(observation=observation, target=candidate)
        failure: str | None = None
        for conjunct in conjuncts:
            failure = _check(conjunct, bindings)
            if failure is not None:
                break
        if failure is None:
            return None
        if failure not in reasons:
            reasons.append(failure)
    return f"no {_subject_words(namespace)} qualifies: " + "; ".join(reasons[:2])


# --------------------------------------------------------------------------
# Saying it in English
# --------------------------------------------------------------------------

#: Subject-namespace name -> the words a player would use for it. Only the four namespaces whose
#: catalog name is not already the player's word need an entry; anything else is used verbatim.
_SUBJECT_WORDS: Mapping[str, str] = {
    "other_player": "other civilization",
    "great_person": "great person",
    "congress": "World Congress resolution",
    "spy": "spy",
}

#: Readability overrides for the handful of symbols the mechanical rules below render awkwardly.
#: Not load-bearing: an override missing here still produces a correct, if plainer, phrase, and a
#: new catalog symbol needs no entry.
_FALSY_OVERRIDES: Mapping[str, str] = {
    "game.is_local_player_turn": "it is not your turn",
    "unit.owner_is_local_player": "the selected unit is not yours",
    "city.owner_is_local_player": "the selected city is not yours",
    "player.religion_founded": "you have not founded a religion",
    "player.pantheon_selected": "you have not chosen a pantheon",
    "congress.is_in_session": "the World Congress is not in session",
    "target.is_revealed": "that plot is not revealed",
}

#: The same, for a ``not <symbol>`` conjunct that failed because the symbol was true.
_TRUTHY_OVERRIDES: Mapping[str, str] = {
    "game.has_blocking_prompt": "a prompt is blocking play",
    "prompt.is_active": "a prompt is blocking play",
    "player.religion_founded": "you have already founded a religion",
    "player.pantheon_selected": "you have already chosen a pantheon",
    "other_player.has_delegation": "you already have a delegation there",
}

#: Comparison operator -> how a person says it, for "X is <actual>, not <this> <wanted>".
_COMPARISON_WORDS: Mapping[type[ast.cmpop], str] = {
    ast.NotEq: "anything but",
    ast.Lt: "less than",
    ast.LtE: "at most",
    ast.Gt: "more than",
    ast.GtE: "at least",
}


def _subject_words(namespace: str) -> str:
    return _SUBJECT_WORDS.get(namespace, namespace)


def _render(value: Any) -> str:
    """A value as the agent already sees it in the observed state, or "not reported"."""
    if value is None:
        return "not reported"
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):  # pragma: no cover - default=str already covers this
        return str(value)


def _phrase(conjunct: str, bindings: Mapping[str, Any]) -> str:
    """Why *conjunct* -- known to be false -- is false, in one phrase a player would recognise."""
    try:
        node = ast.parse(conjunct, mode="eval").body
    except SyntaxError:  # pragma: no cover - split_conjuncts only emits parseable operands
        return _UNEVALUABLE
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return _truthy_phrase(node.operand, bindings)
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
        return "; ".join(
            dict.fromkeys(_phrase(ast.unparse(value), bindings) for value in node.values)
        )
    if isinstance(node, ast.Compare) and len(node.ops) == 1:
        return _comparison_phrase(node, bindings)
    if isinstance(node, ast.Attribute):
        return _falsy_phrase(node, bindings)
    return _UNEVALUABLE


def _path_parts(node: ast.Attribute) -> tuple[str, str, str]:
    """``(full path, root namespace, last attribute)`` for an attribute chain."""
    path = ast.unparse(node)
    return path, path.split(".", 1)[0], node.attr


def _falsy_phrase(node: ast.Attribute, bindings: Mapping[str, Any]) -> str:
    """``unit.is_selected`` -> "no unit selected"; ``game.has_blocking_prompt`` -> "no blocking
    prompt"; ``city.can_buy_with_gold`` -> "the city cannot buy with gold".

    The ``is_``/``has_``/``can_``/``exists`` prefixes are the catalog's own field-naming
    conventions (``catalogs/README.md`` §4), so reading them is reading the vocabulary the agent
    already sees in the observed state, not a table that has to be kept in step with it.
    """
    path, namespace, attr = _path_parts(node)
    override = _FALSY_OVERRIDES.get(path)
    if override is not None:
        return override
    subject = _subject_words(namespace)
    if attr == "exists":
        return f"the board is showing no {subject}"
    if attr.startswith("is_"):
        return f"no {subject} {attr[3:].replace('_', ' ')}"
    if attr.startswith("has_"):
        return f"no {attr[4:].replace('_', ' ')}"
    if attr.startswith("can_"):
        return f"the {subject} cannot {attr[4:].replace('_', ' ')}"
    return f"{path} is {_render(_value_of(path, bindings))}"


def _truthy_phrase(node: ast.expr, bindings: Mapping[str, Any]) -> str:
    """Why a ``not X`` conjunct failed: because ``X`` held."""
    if not isinstance(node, ast.Attribute):
        return _UNEVALUABLE
    path, namespace, attr = _path_parts(node)
    override = _TRUTHY_OVERRIDES.get(path)
    if override is not None:
        return override
    subject = _subject_words(namespace)
    if attr.startswith("has_"):
        return f"there is already {attr[4:].replace('_', ' ')}"
    if attr.startswith("is_"):
        return f"the {subject} is already {attr[3:].replace('_', ' ')}"
    return f"{path} is {_render(_value_of(path, bindings))}"


def _comparison_phrase(node: ast.Compare, bindings: Mapping[str, Any]) -> str:
    """``game.current_screen == "prompt.congress_vote"`` -> "the current screen is "world", not
    "prompt.congress_vote""; ``player.diplomatic_favor > 0`` -> "player.diplomatic_favor is 0, not
    more than 0"."""
    left_source = ast.unparse(node.left)
    right_source = ast.unparse(node.comparators[0])
    operator = node.ops[0]
    if isinstance(operator, (ast.In, ast.NotIn)):
        return f"{_render(_value_of(left_source, bindings))} is not among {right_source}"
    actual = _render(_value_of(left_source, bindings))
    wanted = _render(_value_of(right_source, bindings))
    if isinstance(operator, ast.Eq):
        return f"{left_source} is {actual}, not {wanted}"
    words = _COMPARISON_WORDS.get(type(operator))
    if words is None:  # pragma: no cover - every grammar operator is listed above
        return f"{left_source} is {actual}"
    return f"{left_source} is {actual}, not {words} {wanted}"


def _value_of(expression: str, bindings: Mapping[str, Any]) -> Any:
    """:func:`evaluate_expression`, but never raising -- a phrase must always be produced."""
    try:
        return evaluate_expression(expression, bindings)
    except PredicateEvaluationError:
        return None
