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

**Once the agent HAS named an argument** (:func:`evaluate_argument_availability`, T276). The
undecided-until-a-target-is-named half above is only half the question, and the other half is not
"is this number in range" -- it is *which* range. Observed live on 2026-09-21: ``camera.zoom`` was
issued with ``{"target": 0.05}`` while the camera was in **world** mode, the captures that
followed failed the provenance gate on zoom, and 16 of 17 were withheld. ``camera.zoom``'s own
``availability_predicate`` is ``target >= 0.05 and target <= 1.0`` -- the *union* of two views'
declared ranges (``views.world`` [0.2, 1.0], ``views.strategic`` [0.05, 0.3]) -- so 0.05 is a
perfectly legal **strategic** zoom that the harness asked for while still in **world** mode. On
the standard interface, scrolling past the threshold switches views: mode and zoom move together,
and there is no seat a human can take in world mode at 0.05. What the engine then did with the
request is a separate question and not this module's: the harness had already *asked* for a camera
state no view in the catalog declares, which is a parity violation on the declarations alone.

The defect is the union itself, and it was structural. ``act/camera.py``'s
``validate_camera_action`` bound ``{"target": target}`` and nothing else -- no ``Observation``, so
no way to know which view the camera was in -- and could therefore only ever check the union. Any
target legal in *either* view was accepted in *both*.

So an argument is checked against the range the view the board *is currently in* declares, not
against the union of every view's. The check is derived entirely from the catalog: which action
this is comes from its own verification predicate -- the declaration's own statement that it puts
``camera.zoom`` at ``target`` -- the current view comes from the observation's
``camera.read_state`` mode, and the range comes from that view's ``camera_requirements.zoom_range``.
Nothing about ``(0.2, 1.0)`` is written down here; changing ``catalogs/observations/views.yaml``
changes what this refuses, which is the only way the check and the declaration cannot drift apart.

**That recognition is by SYMBOL, not by the text of the comparison** (T321). It used to require the
rendered shape ``camera.zoom == target``, and giving that predicate a tolerance instead of exact
float equality -- a change to ``catalogs/actions/camera.yaml`` alone, touching no Python -- silently
un-geared this whole check: a 0.05 zoom requested in world mode went from refused to **authorised**.
:func:`_sets_camera_zoom` carries the full account; the short version is that a matcher whose
non-match means "nothing there" produces a confident false all-clear, and this one guards a
Principle I property. Two existing tests caught it and a ratchet in
``tests/unit/test_camera_validation.py`` now pins that the guard still finds its action.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from civsim_harness.act.predicates import (
    TARGET_RESOLVED_SUBJECTS,
    PredicateEvaluationError,
    build_predicate_bindings,
    evaluate_expression,
    evaluate_predicate,
    membership_collection,
    predicate_attribute_chains,
    predicate_root_names,
    resolve_selected_subject_target,
    split_conjuncts,
    subject_candidate_targets,
)
from civsim_harness.models.catalog import DeclarationKind, ParityDeclaration
from civsim_harness.models.turn import Observation

__all__ = [
    "CAMERA_STATE_DECLARATION_ID",
    "CROSS_VIEW_ZOOM",
    "ActionAvailability",
    "CrossViewZoom",
    "availability_by_action",
    "evaluate_action_availability",
    "evaluate_argument_availability",
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
# The argument, once the agent has named one (T276)
# --------------------------------------------------------------------------

#: ``catalogs/observations/camera.yaml``'s own declaration_id -- where the camera actually is, read
#: fresh every decision step (T221). Restated here rather than imported from ``run/composition.py``,
#: exactly as ``observe/reader.py`` restates ``game.screen_state`` for the same reason: this module
#: has no business depending on the run loop for a single literal, and the literal is catalog data.
CAMERA_STATE_DECLARATION_ID: Final = "camera.read_state"

#: The symbol a declaration uses to say "the camera's zoom", as the
#: (namespace, attribute) pair :func:`predicate_attribute_chains` yields. An action whose
#: verification predicate constrains this symbol against ``target`` is, by its own declaration, the
#: action that puts the camera at ``target`` -- which is how :func:`_sets_camera_zoom` finds it
#: without this module hard-coding the id ``camera.zoom``. T321: the pair, not the rendered text
#: ``camera.zoom == target``, because the guard must survive a rewrite of the comparison; see
#: :func:`_sets_camera_zoom` for the Principle I regression that taught us the difference.
_CAMERA_ZOOM_CHAIN: Final[tuple[str, str]] = ("camera", "zoom")


class CrossViewZoom(StrEnum):
    """What to do with an argument the **current** view forbids but another declared view allows.

    **This is the undecided half, and it is deliberately not decided here.** 0.05 is outside
    ``views.world``'s declared [0.2, 1.0] and inside ``views.strategic``'s declared [0.05, 0.3].
    Two readings are open, and which is right is a fact about this build's interface, not about
    this module:

    - the scroll wheel, pushed past the threshold, changes the mode *and* the zoom together -- in
      which case a human reaches 0.05 by scrolling, and the harness's equivalent is a view change
      followed by the zoom, so the argument should be honoured after that view change rather than
      refused; or
    - the mode only ever changes by its own toggle (``camera.set_view_mode``) -- in which case
      asking for 0.05 while in world mode is asking for a seat no human has, and refusing it is
      right permanently.

    Settling it means watching what a real scroll-wheel zoom does to ``camera.read_state``'s
    ``mode`` and ``zoom`` together on a live client. That is client-gated and belongs to the live
    lane (tasks.md **T277**); nothing here guesses it.

    :attr:`REFUSE` is what ships, because it is the answer under *both* readings for an argument
    outside every declared range, and under the second for this one -- and because a refusal is
    recoverable by the agent in one step (issue ``camera.set_view_mode``, then the zoom) while a
    camera parked in a state no view declares is not. :attr:`SWITCH_VIEW_FIRST` is named so the
    seam is a value and not a rewrite, and raises rather than silently re-admitting the argument:
    honouring it needs the measurement above *and* an executor step that performs the view change,
    and neither exists yet.
    """

    REFUSE = "refuse"
    SWITCH_VIEW_FIRST = "switch_view_first"


#: The policy in force until T277 lands the measurement. Callers may pass their own.
CROSS_VIEW_ZOOM: Final = CrossViewZoom.REFUSE


def evaluate_argument_availability(
    declaration: ParityDeclaration,
    observation: Observation,
    *,
    target: Any,
    declarations: Iterable[ParityDeclaration],
    cross_view: CrossViewZoom = CROSS_VIEW_ZOOM,
) -> ActionAvailability:
    """Whether *target* is an argument a human could give *declaration* **on this board**.

    The companion to :func:`evaluate_action_availability`, which runs before a target is named and
    therefore leaves every ``target`` conjunct undecided. This one runs after, and answers the one
    question the union-shaped predicate in the catalog cannot: not "is this number in the range
    some view allows" but "is it in the range the view we are *in* allows" (see the module
    docstring). Every parameter is required for that reason -- an argument check with an optional
    observation, or an optional catalog, is a check that silently does nothing wherever the caller
    forgets it.

    *declarations* is the catalog (or any slice of it containing the ``kind: view`` declarations);
    non-views are ignored. Refuses with a reason naming the view and the range it violated, so the
    refusal is actionable rather than a bare "out of parity".

    **Abstains, rather than refusing, when the board reports no camera mode** -- there is then no
    current view to measure against, and inventing a restriction is this module's one standing
    prohibition. That is not a hole in the guarantee: an unreadable camera state already fails the
    *capture* side closed (``parity/screening.py``'s provenance gate withholds the image), which is
    where a camera that cannot be confirmed belongs.
    """
    declaration_id = str(declaration.declaration_id)
    refusal = _out_of_current_view_range(
        declaration,
        observation,
        target=target,
        declarations=declarations,
        cross_view=cross_view,
    )
    if refusal is not None:
        return ActionAvailability(declaration_id, False, refusal)
    return ActionAvailability(declaration_id, True)


def _out_of_current_view_range(
    declaration: ParityDeclaration,
    observation: Observation,
    *,
    target: Any,
    declarations: Iterable[ParityDeclaration],
    cross_view: CrossViewZoom,
) -> str | None:
    """The reason *target* is outside the current view's declared zoom range, or ``None``."""
    if declaration.kind is not DeclarationKind.ACTION or not _sets_camera_zoom(declaration):
        return None
    if isinstance(target, bool) or not isinstance(target, (int, float)):
        # Not a zoom level at all. The action's own availability predicate is the authority on a
        # malformed argument, and it already fails closed on one.
        return None

    mode = _current_camera_mode(observation)
    if mode is None:
        return None
    views = [view for view in declarations if _declares_mode(view, mode)]
    if not views:
        # The board reports a mode no view declares. That is a catalog gap, and refusing the
        # agent's argument is not how a catalog gap gets reported.
        return None
    if any(_accepts_zoom(view, target) for view in views):
        return None

    elsewhere = sorted(
        str(view.declaration_id)
        for view in declarations
        if _accepts_zoom(view, target) and not _declares_mode(view, mode)
    )
    if elsewhere and cross_view is CrossViewZoom.SWITCH_VIEW_FIRST:
        raise NotImplementedError(
            "CrossViewZoom.SWITCH_VIEW_FIRST is named, not implemented: honouring an argument "
            "legal in another view needs the live measurement of what a scroll-wheel zoom does to "
            "mode and zoom together, and an executor step that performs the view change first "
            f"(tasks.md T277). The argument {target!r} is legal in {', '.join(elsewhere)}."
        )

    declared = "; ".join(
        f"{view.declaration_id} declares {_zoom_range_words(view)}" for view in views
    )
    reason = (
        f"the camera is in the {mode} view, where a zoom of {_number(target)} is not a seat a "
        f"human can take: {declared}"
    )
    if elsewhere:
        reason += f" ({_number(target)} is inside {', '.join(elsewhere)}, which is another view)"
    return reason


def _sets_camera_zoom(declaration: ParityDeclaration) -> bool:
    """Whether *declaration* says, in its own verification predicate, that it puts the camera's
    zoom at ``target``.

    **Keyed on the symbols the declaration constrains, never on the shape of the comparison
    between them** (T321). This used to require the literal node shape ``camera.zoom == target``,
    matched with :func:`ast.unparse` over each conjunct. That read as a virtue -- it found the
    action without hard-coding the id ``camera.zoom`` -- and it was a latent Principle I hazard,
    because **a declaration it failed to recognise was silently ungeared rather than loudly
    broken**: :func:`_out_of_current_view_range` returns ``None`` for anything this says ``False``
    to, so a non-match reads as "not a zoom action", i.e. *nothing there*. That is this project's
    own named failure mode -- an allowlist read as a detector, whose failure is a confident false
    all-clear rather than a visible refusal.

    It fired for real: giving ``camera.zoom`` a tolerance instead of exact float equality (T321,
    a change touching only ``catalogs/actions/camera.yaml``) removed the ``==`` shape, the T276
    per-view range guard stopped applying, and a zoom of 0.05 requested in world mode -- a camera
    state no view declares and no human occupies -- was **authorised** instead of refused. Two
    pre-existing tests in ``tests/unit/test_camera_validation.py`` caught it immediately; a ratchet
    there now also pins that exactly one shipped action is recognised, so a silent zero can never
    again pass for "no zoom action exists".

    The predicate's *meaning* is what the guard needs: this action's success is defined by
    ``camera.zoom`` standing in some declared relation to the requested ``target``. Both symbols
    present is exactly that, and it survives any future rewrite of the relation -- while still
    refusing a predicate that constrains the zoom without reference to the target
    (``camera.zoom <= 1.0``) or names a target without constraining the zoom
    (``camera.mode == target``).
    """
    predicate = declaration.verification_predicate
    if not predicate:
        return False
    return _CAMERA_ZOOM_CHAIN in predicate_attribute_chains(
        predicate
    ) and "target" in predicate_root_names(predicate)


def _declares_mode(declaration: ParityDeclaration, mode: str) -> bool:
    """Whether *declaration* is a view declared for camera *mode*."""
    requirements = declaration.camera_requirements
    return (
        declaration.kind is DeclarationKind.VIEW
        and requirements is not None
        and requirements.mode.value == mode
    )


def _accepts_zoom(declaration: ParityDeclaration, zoom: float) -> bool:
    """Whether *declaration* is a view whose own declared ``zoom_range`` contains *zoom*."""
    requirements = declaration.camera_requirements
    if declaration.kind is not DeclarationKind.VIEW or requirements is None:
        return False
    low, high = requirements.zoom_range
    return low <= zoom <= high


def _current_camera_mode(observation: Observation) -> str | None:
    """The camera mode the board is reporting, or ``None`` when it reports none."""
    for entry in observation.entries:
        if str(entry.declaration_id) != CAMERA_STATE_DECLARATION_ID:
            continue
        value = entry.value
        mode = value.get("mode") if isinstance(value, Mapping) else None
        return mode if isinstance(mode, str) and mode else None
    return None


def _number(value: float) -> str:
    return f"{value:g}"


def _zoom_range_words(view: ParityDeclaration) -> str:
    assert view.camera_requirements is not None  # _declares_mode already established this
    low, high = view.camera_requirements.zoom_range
    return f"a zoom range of {_number(low)} to {_number(high)}"


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
