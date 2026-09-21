"""The restricted predicate evaluator, and the bindings it evaluates against (T106).

Binds to the fixed symbol table already declared in
:mod:`civsim_harness.capability.predicates` (``EXPOSED_PREDICATE_NAMESPACES``, ``OBSERVED_PREFIX``)
-- this module does not invent a second one. That module performs the *syntactic*, load-time check
("does this predicate touch a namespace nobody declared"); this one performs the *runtime*
evaluation contracts/capability-catalog.md's validation 6 and ``catalogs/README.md`` §4 describe:
literals (numbers, strings, ``true``/``false``, ``null``, list literals), ``.``-attribute access,
the operators ``and``, ``or``, ``not``, ``==``, ``!=``, ``<``, ``<=``, ``>``, ``>=``, ``in``, and
binary ``+``/``-`` between two numeric operands (e.g. ``observed_turn_number + 1``, the form
``catalogs/actions/turn.yaml``'s own ``turn.end_turn`` verification predicate and contracts/
capability-catalog.md's worked example both use). No function calls, no multiplication or division,
no arithmetic on non-numeric operands, no assignment.

**Why `+`/`-` and nothing more.** The grammar's restriction exists to rule out arbitrary evaluation
and function calls (capability-catalog.md load-time rule 6: predicates reference only exposed
symbols) -- not arithmetic itself. Integer/float addition and subtraction inside this
AST-restricted evaluator introduce no such risk: both operands are already fully evaluated,
plain-data values before the operator is ever applied, and the operator set stays a fixed,
enumerable pair. Forbidding this specific arithmetic would instead force the harness to precompute
derived values like "next turn number" in Python and hand them in as bespoke bindings, pushing game
semantics out of the declarative catalog and into code -- the opposite of what the catalog-as-data
design wants. ``*`` and ``/`` remain forbidden because nothing in this catalog needs them; adding
them would only widen the surface without a documented use.

**How "no arbitrary evaluation" is enforced structurally, not by convention.** A predicate string is
parsed once with Python's own ``ast.parse(expr, mode="eval")`` -- used purely as a grammar
recognizer, never handed to ``eval()``/``exec()``. Every node the parse produces is checked against
an explicit allow-list (``_ALLOWED_NODE_TYPES``) before :func:`evaluate_predicate` interprets it;
an unlisted node (a call, ``*``/``/``/``**``/... arithmetic, a lambda, a comprehension, an f-string,
a subscript, ...) raises immediately -- ``ast.BinOp`` is allowed structurally, but only ``ast.Add``
and ``ast.Sub`` are in the allow-list, so a multiplication or division expression still fails this
same walk because its ``ast.Mult``/``ast.Div`` operator node is not listed. Name and attribute
resolution are both handled by this module's own code walking *bindings*, a plain mapping supplied
by the caller -- there is no path from a parsed predicate to Python's normal name resolution,
globals, or builtins.

**Bindings.** ``evaluate_predicate(predicate, bindings)`` is deliberately generic: it takes whatever
namespace-name -> value mapping the caller supplies and does not know where that mapping came from.
:func:`build_predicate_bindings` is the concrete builder ``act.dispatch`` (T107) and ``act.verify``
(T108) both use to turn a live :class:`~civsim_harness.models.turn.Observation` into that mapping,
following ``catalogs/README.md`` §4's namespace/field vocabulary and its "binding subject namespaces
to target" rule. It is necessarily best-effort against the catalog data that exists today -- see its
own docstring for the specific, honestly-stated gaps (``camera.*`` has no backing observation
declaration at all yet; a few scalar fields such as ``player.gold``/``player.faith`` and
``unit.is_selected`` are named in the README's vocabulary but not yet produced by any authored
``output_schema``). Those are catalog-authoring gaps outside this wave's file ownership
(``catalogs/**``), not evaluator bugs, and are called out in this wave's report.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence
from typing import Any

from civsim_harness.capability.predicates import OBSERVED_PREFIX, unknown_predicate_symbols
from civsim_harness.errors import CatalogError
from civsim_harness.models.common import DeclarationId
from civsim_harness.models.turn import Observation

# --------------------------------------------------------------------------
# The evaluator
# --------------------------------------------------------------------------


class PredicateEvaluationError(CatalogError):
    """A predicate could not be evaluated against its supplied bindings.

    Subclasses :class:`~civsim_harness.errors.CatalogError` (no new error family) because a
    runtime evaluation failure here is still a catalog-contract violation (contracts/
    capability-catalog.md validation 6) -- just one the load-time syntactic check in
    :mod:`civsim_harness.capability.predicates` cannot catch, since it has no concrete binding
    environment to check names against. This mirrors
    :class:`civsim_harness.capability.registry.WrongContextError`'s own choice to subclass
    ``CatalogError`` rather than sit beside it.
    """


#: Every AST node type this evaluator is willing to walk. Anything else -- ``ast.Call``,
#: ``ast.Mult``/``ast.Div``/``ast.Pow``/... (any ``BinOp`` operator besides ``Add``/``Sub``),
#: ``ast.Lambda``, ``ast.Subscript``, ``ast.JoinedStr``, comprehensions, ``ast.Dict`` -- is rejected
#: outright. This is the concrete, structural form of "no function calls, no arithmetic beyond
#: binary +/- on numeric operands, no assignment". ``ast.BinOp`` itself is allowed (a binary
#: operator expression can be ``+``/``-``), but only ``ast.Add``/``ast.Sub`` are listed as operator
#: node types, so ``ast.walk`` still surfaces and rejects any other operator (``ast.Mult``, ...) it
#: finds inside that same ``BinOp``.
_ALLOWED_NODE_TYPES: tuple[type[ast.AST], ...] = (
    ast.Expression,
    ast.BoolOp,
    ast.UnaryOp,
    ast.BinOp,
    ast.Compare,
    ast.Name,
    ast.Attribute,
    ast.Constant,
    ast.List,
    ast.Load,
    ast.And,
    ast.Or,
    ast.Not,
    ast.USub,
    ast.Add,
    ast.Sub,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.In,
    ast.NotIn,
)

#: ``true``/``false``/``null`` are lowercase in the catalog's own grammar (catalogs/README.md §4),
#: so they parse as ``ast.Name`` rather than Python's capitalised ``True``/``False``/``None``
#: constants. Resolved specially in :func:`_resolve_name` before falling through to *bindings*.
_LITERAL_NAMES: Mapping[str, Any] = {"true": True, "false": False, "null": None}


def evaluate_predicate(predicate: str, bindings: Mapping[str, Any]) -> bool:
    """Evaluate *predicate* against *bindings* and return a ``bool``.

    *bindings* must supply exactly the root namespaces the predicate actually touches (a subset of
    ``EXPOSED_PREDICATE_NAMESPACES``, plus any ``observed_*`` snapshot keys it references) -- see
    :mod:`civsim_harness.capability.predicates` and ``catalogs/README.md`` §4.

    Raises :class:`PredicateEvaluationError` when *predicate* references a namespace outside the
    exposed table (defence in depth: catalog load already checked this once syntactically; this is
    the same "re-check at the point of use" discipline
    :meth:`civsim_harness.capability.registry.CapabilityRegistry.authorize` applies to context),
    when it uses a construct outside the documented grammar, or when it references a name *bindings*
    does not supply. Never returns a truthy result for an unevaluable predicate -- an action's
    ``verification_predicate`` failing to evaluate must never be read as success (FR-011).
    """
    unknown = unknown_predicate_symbols(predicate)
    if unknown:
        raise PredicateEvaluationError(
            "predicate references a namespace outside the exposed table",
            detail={"predicate": predicate, "unknown_symbols": sorted(unknown)},
        )

    tree = _parse(predicate)
    return bool(_eval_node(tree.body, bindings, predicate))


def _parse(predicate: str) -> ast.Expression:
    try:
        tree = ast.parse(predicate, mode="eval")
    except SyntaxError as exc:
        raise PredicateEvaluationError(
            "predicate is not valid predicate-grammar syntax",
            detail={"predicate": predicate, "reason": str(exc)},
        ) from exc

    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODE_TYPES):
            raise PredicateEvaluationError(
                "predicate uses a construct outside the documented grammar "
                "(catalogs/README.md §4): no function calls, no arithmetic beyond binary "
                "+/- on numeric operands, no assignment",
                detail={"predicate": predicate, "node_type": type(node).__name__},
            )
    return tree


def _eval_node(node: ast.AST, bindings: Mapping[str, Any], predicate: str) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.List):
        return [_eval_node(elt, bindings, predicate) for elt in node.elts]
    if isinstance(node, ast.Name):
        return _resolve_name(node.id, bindings, predicate)
    if isinstance(node, ast.Attribute):
        base = _eval_node(node.value, bindings, predicate)
        return _resolve_attribute(base, node.attr)
    if isinstance(node, ast.UnaryOp):
        operand = _eval_node(node.operand, bindings, predicate)
        if isinstance(node.op, ast.Not):
            return not operand
        # ast.USub -- the only other unary op _ALLOWED_NODE_TYPES permits.
        try:
            return -operand
        except TypeError as exc:
            raise PredicateEvaluationError(
                "predicate applied unary '-' to a non-numeric value",
                detail={"predicate": predicate, "reason": str(exc)},
            ) from exc
    if isinstance(node, ast.BoolOp):
        return _eval_bool_op(node, bindings, predicate)
    if isinstance(node, ast.BinOp):
        return _eval_binop(node, bindings, predicate)
    if isinstance(node, ast.Compare):
        return _eval_compare(node, bindings, predicate)
    raise PredicateEvaluationError(  # pragma: no cover - _ALLOWED_NODE_TYPES already filters this
        "predicate uses a construct outside the documented grammar",
        detail={"predicate": predicate, "node_type": type(node).__name__},
    )


def _is_numeric(value: Any) -> bool:
    """``int``/``float`` only -- ``bool`` is a ``int`` subclass in Python but is not a number in
    this catalog's grammar (``true``/``false`` are boolean literals, never arithmetic operands)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _eval_binop(node: ast.BinOp, bindings: Mapping[str, Any], predicate: str) -> Any:
    # ast.Add / ast.Sub -- the only two operator node types _ALLOWED_NODE_TYPES permits; anything
    # else (ast.Mult, ast.Div, ...) was already rejected by _parse's structural walk.
    left = _eval_node(node.left, bindings, predicate)
    right = _eval_node(node.right, bindings, predicate)
    if not _is_numeric(left) or not _is_numeric(right):
        raise PredicateEvaluationError(
            "predicate applied arithmetic ('+'/'-') to a non-numeric operand",
            detail={
                "predicate": predicate,
                "left": repr(left),
                "right": repr(right),
            },
        )
    if isinstance(node.op, ast.Add):
        return left + right
    return left - right


def _eval_bool_op(node: ast.BoolOp, bindings: Mapping[str, Any], predicate: str) -> Any:
    is_and = isinstance(node.op, ast.And)
    result: Any = None
    for value_node in node.values:
        result = _eval_node(value_node, bindings, predicate)
        if is_and and not result:
            return result
        if not is_and and result:
            return result
    return result


def _eval_compare(node: ast.Compare, bindings: Mapping[str, Any], predicate: str) -> bool:
    left = _eval_node(node.left, bindings, predicate)
    for op, comparator in zip(node.ops, node.comparators, strict=True):
        right = _eval_node(comparator, bindings, predicate)
        if not _apply_comparison(op, left, right, predicate):
            return False
        left = right
    return True


def _apply_comparison(op: ast.cmpop, left: Any, right: Any, predicate: str) -> bool:
    try:
        if isinstance(op, ast.Eq):
            return bool(left == right)
        if isinstance(op, ast.NotEq):
            return bool(left != right)
        if isinstance(op, ast.Lt):
            return bool(left < right)
        if isinstance(op, ast.LtE):
            return bool(left <= right)
        if isinstance(op, ast.Gt):
            return bool(left > right)
        if isinstance(op, ast.GtE):
            return bool(left >= right)
        if isinstance(op, ast.In):
            return bool(right is not None and left in right)
        if isinstance(op, ast.NotIn):
            return bool(right is None or left not in right)
    except TypeError as exc:
        # A predicate comparing incompatible types (e.g. an absent numeric field, bound to `None`,
        # against a number) is an evaluation-time failure, not a Python crash -- this is what keeps
        # an un-short-circuited comparison against an absent subject namespace from propagating an
        # opaque TypeError instead of the same PredicateEvaluationError every other failure mode
        # here raises.
        raise PredicateEvaluationError(
            "predicate compared incompatible values",
            detail={"predicate": predicate, "operator": type(op).__name__, "reason": str(exc)},
        ) from exc
    raise PredicateEvaluationError(  # pragma: no cover - _ALLOWED_NODE_TYPES already filters this
        "unsupported comparison operator", detail={"operator": type(op).__name__}
    )


def _resolve_name(name: str, bindings: Mapping[str, Any], predicate: str) -> Any:
    if name in _LITERAL_NAMES:
        return _LITERAL_NAMES[name]
    if name in bindings:
        return bindings[name]
    raise PredicateEvaluationError(
        "predicate references a name with no supplied binding",
        detail={"predicate": predicate, "name": name},
    )


def _resolve_attribute(base: Any, attr: str) -> Any:
    if base is None:
        return False if attr == "exists" else None
    if isinstance(base, Mapping):
        return base.get(attr, None)
    return getattr(base, attr, None)


# --------------------------------------------------------------------------
# Bindings -- realising the fixed symbol table from a live Observation
# --------------------------------------------------------------------------

# Which observation declaration_id(s) feed each whole-game/whole-player namespace, and any field
# rename needed between the catalog's own field name and the predicate-exposed attribute name
# (catalogs/README.md §4). Declared here, not duplicated in act.dispatch/act.verify, so both bind to
# one table.
_GAME_SOURCES: tuple[DeclarationId, ...] = (
    DeclarationId("game.screen_state"),
    DeclarationId("game.turn_state"),
)
_GAME_FIELD_RENAMES: Mapping[str, str] = {"screen": "current_screen"}

_PLAYER_SOURCES: tuple[DeclarationId, ...] = (
    DeclarationId("research.state"),
    DeclarationId("government.state"),
    DeclarationId("religion.state"),
)
_CONGRESS_DECLARATION_ID = DeclarationId("congress.state")

# namespace -> (backing declaration_id, list field, id field matched against `target`)
# (catalogs/README.md §4 "Binding subject namespaces to target").
#
# Known, documented simplification: the README's own text carves out unit/city specifically --
# "the harness resolves the action's own target (**or, for a unit/city action, the unit/city the
# human selected before issuing it**) against the matching observation's list" -- meaning a
# unit/city action's subject is, strictly, a separately-tracked "currently selected unit/city",
# not necessarily the same value as the action's own `target` parameter (e.g. for a move order,
# `target` is the destination plot, not the unit being moved). Threading a distinct
# "selected-subject id" through `Decision.parameters` would need a parameter-naming convention
# that is not specified anywhere in this catalog or data-model today; rather than invent one
# unreviewed, every namespace here -- including unit/city -- is resolved against the single
# `target` this function already accepts. This is accurate for other_player/congress/
# great_person/spy (whose `target` genuinely is the subject id) and an approximation for unit/city
# that a caller with a distinct selected-subject id can work around by passing that id as `target`
# for the purposes of this lookup. Called out in this wave's report as a gap for whoever wires the
# real dispatch call site.
_SUBJECT_SOURCES: Mapping[str, tuple[DeclarationId, str, str]] = {
    "unit": (DeclarationId("units.state"), "units", "unit_id"),
    "city": (DeclarationId("cities.state"), "cities", "city_id"),
    "other_player": (DeclarationId("diplomacy.state"), "relations", "player_id"),
    "congress": (_CONGRESS_DECLARATION_ID, "active_resolutions", "resolution_id"),
    "great_person": (
        DeclarationId("great_people.state"),
        "recruitable_individuals",
        "individual_id",
    ),
    "spy": (DeclarationId("espionage.state"), "spies", "unit_id"),
}
_CONGRESS_TOP_LEVEL_FIELDS = ("is_in_session",)

_GAME_SCREEN_DECLARATION_ID = DeclarationId("game.screen_state")

#: T255: the InGame declaration that says which city the game has selected. ``cities.state``
#: runs in GameCore_Tuner, where ``UI`` does not exist, so its entries carry no ``is_selected``;
#: this declaration's ``selected_city_id`` is overlaid onto the ``city`` namespace instead (and
#: onto :func:`resolve_selected_subject_target`'s answer), which is what lets a city order's
#: ``city.is_selected and ...`` predicate ever be true. Units need no overlay: ``units.state``
#: already runs InGame and reports ``is_selected`` itself.
_CITY_SELECTION_DECLARATION_ID = DeclarationId("cities.selection")


def _selected_city_id(index: Mapping[DeclarationId, Any]) -> Any | None:
    """The id ``cities.selection`` reports as selected, or ``None`` when there is no such entry,
    it reports no selection, or the value is not a usable id -- never a guess."""
    source = index.get(_CITY_SELECTION_DECLARATION_ID)
    if not isinstance(source, Mapping) or source.get("has_selection") is not True:
        return None
    return source.get("selected_city_id")


def _is_selected(item: Mapping[str, Any], *, id_field: str, selection_id: Any | None) -> bool:
    """An entry is the selected subject when its own body says so (``is_selected: true``) or,
    when its body does not carry the field at all, when the separate selection declaration names
    its id. A body that carries ``is_selected: false`` is *not* overridden -- it answered."""
    own = item.get("is_selected")
    if own is not None:
        return own is True
    return selection_id is not None and item.get(id_field) == selection_id


def observation_index(observation: Observation) -> dict[DeclarationId, Any]:
    """Map each entry's ``declaration_id`` to its raw value, for repeated lookups below."""
    return {entry.declaration_id: entry.value for entry in observation.entries}


def build_predicate_bindings(
    *,
    observation: Observation,
    target: Any = None,
    observed_snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the ``evaluate_predicate`` bindings environment from a live ``Observation``.

    Realises ``catalogs/README.md`` §4's namespace table from the entries actually present in
    *observation*: ``game``/``player`` are merges of the (few) declarations that back them;
    ``prompt`` is derived from ``game.screen_state``; the subject namespaces (``unit``, ``city``,
    ``other_player``, ``congress``, ``great_person``, ``spy``) are resolved by matching *target*
    against the matching observation's own list, per the README's "binding subject namespaces to
    target" rule, defaulting to ``{"exists": False}`` (every other attribute read then resolves to
    ``None`` via :func:`_resolve_attribute`, which is falsy in every predicate this catalog
    declares). ``camera`` has no backing observation declaration in this catalog today and is
    always ``{}`` -- a catalog-authoring gap, not something this function can source data for.
    ``target`` itself is bound to the raw value passed in, so ``target == ...`` / ``target in ...``
    compare against it directly and ``target.<field>`` falls back to ``None`` for any field a plain
    value does not carry.

    *observed_snapshot*, when given, is merged in verbatim (already-prefixed ``observed_*`` keys) --
    see ``act.verify`` for how a pre-execution snapshot is built and threaded through here.
    """
    index = observation_index(observation)

    game = _merge_fields(index, _GAME_SOURCES, renames=_GAME_FIELD_RENAMES)
    if "current_screen" in game:
        screen = game["current_screen"]
        game.setdefault(
            "active_prompt_type",
            screen if isinstance(screen, str) and screen.startswith("prompt.") else None,
        )

    player = _merge_fields(index, _PLAYER_SOURCES)
    congress_source = index.get(_CONGRESS_DECLARATION_ID)
    if isinstance(congress_source, Mapping) and "local_player_favor" in congress_source:
        player["diplomatic_favor"] = congress_source["local_player_favor"]

    prompt = _bind_prompt_namespace(index)

    bindings: dict[str, Any] = {
        "game": game,
        "player": player,
        "prompt": prompt,
        "camera": {},  # no backing observation declaration exists yet (catalog gap, out of scope)
        "target": target,
    }
    for namespace, (declaration_id, list_field, id_field) in _SUBJECT_SOURCES.items():
        extra = _CONGRESS_TOP_LEVEL_FIELDS if namespace == "congress" else ()
        bindings[namespace] = _bind_subject_namespace(
            index,
            declaration_id=declaration_id,
            list_field=list_field,
            id_field=id_field,
            target=target,
            extra_top_level_fields=extra,
            selection_id=_selected_city_id(index) if namespace == "city" else None,
        )

    if observed_snapshot:
        for key, value in observed_snapshot.items():
            if key.startswith(OBSERVED_PREFIX):
                bindings[key] = value

    return bindings


#: Action-id domain prefix -> the subject namespace whose SELECTED entry stands in for a missing
#: `target` (catalogs/README.md §4: "the action's own `target` (or, for a unit/city action, the
#: unit/city the human selected before issuing it)"). Only these two subjects carry a selection.
_SELECTED_SUBJECT_BY_ACTION_PREFIX: Mapping[str, str] = {"units": "unit", "cities": "city"}


def resolve_selected_subject_target(
    action_declaration_id: DeclarationId, observation: Observation
) -> Any | None:
    """The README §4 fallback the binding above never implemented: for a unit or city action
    issued with no ``target``, the subject is the entry the game shows as selected
    (``is_selected: true`` in ``units.state`` / ``cities.state``) -- exactly what a human's click
    acts on. Returns that entry's id, or ``None`` when the action is not a unit/city action, the
    observation carries no such list, or nothing is selected (then the predicate binds the absent
    namespace as before and the action is refused, never guessed).

    MEASURED (2026-09-21, the first three model-driven runs on Linux): the model chose
    ``units.found_city`` 56 times, wrote the settler's id into its reasoning, and never put it in
    ``parameters.target``; every decision was refused before dispatch on ``unit.is_selected``.
    """
    prefix = str(action_declaration_id).split(".", 1)[0]
    namespace = _SELECTED_SUBJECT_BY_ACTION_PREFIX.get(prefix)
    if namespace is None:
        return None
    declaration_id, list_field, id_field = _SUBJECT_SOURCES[namespace]
    index = observation_index(observation)
    source = index.get(declaration_id)
    items = source.get(list_field) if isinstance(source, Mapping) else None
    if not isinstance(items, list):
        return None
    selection_id = _selected_city_id(index) if namespace == "city" else None
    for item in items:
        if isinstance(item, Mapping) and _is_selected(
            item, id_field=id_field, selection_id=selection_id
        ):
            return item.get(id_field)
    return None


def _merge_fields(
    index: Mapping[DeclarationId, Any],
    sources: Sequence[DeclarationId],
    *,
    renames: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for declaration_id in sources:
        value = index.get(declaration_id)
        if not isinstance(value, Mapping):
            continue
        for key, field_value in value.items():
            merged[(renames or {}).get(key, key)] = field_value
    return merged


def _bind_prompt_namespace(index: Mapping[DeclarationId, Any]) -> dict[str, Any]:
    source = index.get(_GAME_SCREEN_DECLARATION_ID)
    prompt: dict[str, Any] = {"type": None, "is_active": False, "options": []}
    if isinstance(source, Mapping):
        prompt["type"] = source.get("screen")
        prompt["is_active"] = bool(source.get("has_blocking_prompt", False))
        prompt["options"] = list(source.get("prompt_options") or [])
    return prompt


def _bind_subject_namespace(
    index: Mapping[DeclarationId, Any],
    *,
    declaration_id: DeclarationId,
    list_field: str,
    id_field: str,
    target: Any,
    extra_top_level_fields: Sequence[str] = (),
    selection_id: Any | None = None,
) -> dict[str, Any]:
    source = index.get(declaration_id)
    namespace: dict[str, Any] = {"exists": False}
    if not isinstance(source, Mapping):
        return namespace

    for field in extra_top_level_fields:
        if field in source:
            namespace[field] = source[field]

    items = source.get(list_field)
    if not isinstance(items, list):
        return namespace

    def _bind(item: Mapping[str, Any]) -> dict[str, Any]:
        namespace.update(item)
        namespace["exists"] = True
        # T255: a body that does not report its own selection (cities.state, GameCore_Tuner)
        # gets it from the separate selection declaration, so `city.is_selected` is a real
        # field on the bound subject rather than an absent one that resolves to None.
        if "is_selected" not in item and selection_id is not None:
            namespace["is_selected"] = item.get(id_field) == selection_id
        return namespace

    if target is not None:
        for item in items:
            if isinstance(item, Mapping) and item.get(id_field) == target:
                return _bind(item)
    # README §4's parenthesis: "(or, for a unit/city action, the unit/city the human selected
    # before issuing it)". When `target` names no entry -- it is a plot for units.move_to, a
    # promotion for units.promote, a production item for a city order, or absent -- the subject
    # is the entry the game shows as selected. MEASURED 2026-09-21 (attempt 4): the model ordered
    # its warrior to move by passing the unit's id as `target`; nothing here selects a subject
    # the game has not already selected.
    for item in items:
        if isinstance(item, Mapping) and _is_selected(
            item, id_field=id_field, selection_id=selection_id
        ):
            return _bind(item)
    return namespace
