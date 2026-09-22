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
own docstring for the specific, honestly-stated gaps (as of T308, ``camera.*`` IS wired to
``camera.read_state``, added T221; a few scalar fields such as ``unit.is_selected`` are named in the
README's vocabulary but not yet produced by any authored ``output_schema``). Those are
catalog-authoring gaps outside this wave's file ownership (``catalogs/**``), not evaluator bugs, and
are called out in this wave's report.
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
    return bool(evaluate_expression(predicate, bindings))


def evaluate_expression(expression: str, bindings: Mapping[str, Any]) -> Any:
    """The *value* of a predicate-grammar *expression*, rather than its truthiness.

    Identical in every restriction to :func:`evaluate_predicate` -- same symbol-table check, same
    ``_parse`` allow-list walk, same bindings-only name resolution -- and is in fact what that
    function evaluates before coercing to ``bool``. It exists because an availability *check* only
    ever needs the boolean, while explaining a check that failed needs the reading itself: "the
    screen is ``world``, not ``prompt.congress_vote``" and "``player.available_beliefs`` is empty"
    are both statements about a value. Callers that want a yes/no answer should keep using
    :func:`evaluate_predicate`, which can never be misread as "a non-empty list means available".
    """
    unknown = unknown_predicate_symbols(expression)
    if unknown:
        raise PredicateEvaluationError(
            "predicate references a namespace outside the exposed table",
            detail={"predicate": expression, "unknown_symbols": sorted(unknown)},
        )

    tree = _parse(expression)
    return _eval_node(tree.body, bindings, expression)


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
    # T258: the top bar. Gives `player.*_per_turn` and, through the renames below, the README's
    # long-listed `player.gold` / `player.faith` balances their first producer.
    DeclarationId("player.yields"),
)
_PLAYER_FIELD_RENAMES: Mapping[str, str] = {"gold_balance": "gold", "faith_balance": "faith"}
_CONGRESS_DECLARATION_ID = DeclarationId("congress.state")

#: T313 (2026-09-22, live lane): `units.found_city`'s verification used to read `not unit.exists`
#: -- whether the Settler disappeared -- which fabricates: a Settler also disappears when it is
#: captured or killed (MEASURED same day: Georgia captured five Builders and a capital on the
#: previous board; `run-f9aea1fc1c7346eca0e72cf6d8492882` shows the predicate rejecting a founding
#: step whose own later observations prove the city was founded). Per the owner's own fix
#: ("count [cities], programmatically") this binds a real, positive count -- the number of
#: `cities.state` entries that are the local player's own -- as `player.city_count`, so a
#: verification predicate can compare it before/after through the same `observed_*` mechanism
#: `turn.end_turn` already uses, rather than inferring success from the Settler's absence.
_CITIES_STATE_DECLARATION_ID = DeclarationId("cities.state")


def _local_player_city_count(index: Mapping[DeclarationId, Any]) -> int | None:
    """How many cities `cities.state` reports as the local player's own, or ``None`` when
    `cities.state` was not observed or its `cities` field is missing/not a list -- never coerced
    to ``0``, because ``0`` legitimately means "no cities yet" while ``None`` means "we do not
    know". `cities.state` also lists other civilizations' currently-visible cities
    (`catalogs/observations/cities.yaml`), so this counts only entries whose own
    `owner_is_local_player` is `True` -- an enemy city entering or leaving vision must never move
    this count.
    """
    source = index.get(_CITIES_STATE_DECLARATION_ID)
    cities = source.get("cities") if isinstance(source, Mapping) else None
    if not isinstance(cities, list):
        return None
    return sum(
        1
        for city in cities
        if isinstance(city, Mapping) and city.get("owner_is_local_player") is True
    )

# T221/T308: `camera.read_state`'s own `output_schema` (catalogs/observations/camera.yaml) already
# names its fields exactly as the three camera actions' predicates reference them (`mode`, `zoom`,
# `target_plot`, `target_is_revealed`) -- no rename table needed, unlike `game`/`player` above.
_CAMERA_SOURCES: tuple[DeclarationId, ...] = (DeclarationId("camera.read_state"),)

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
    *observation*: ``game``/``player``/``camera`` are merges of the (few) declarations that back
    them (T308: ``camera`` from ``camera.read_state``, added T221, the same merge shape as
    ``game``/``player`` -- when the observation carries no ``camera.read_state`` entry, the merge
    naturally produces ``{}``, exactly as it always has, rather than special-casing absence);
    ``prompt`` is derived from ``game.screen_state``; the subject namespaces (``unit``, ``city``,
    ``other_player``, ``congress``, ``great_person``, ``spy``) are resolved by matching *target*
    against the matching observation's own list, per the README's "binding subject namespaces to
    target" rule, defaulting to ``{"exists": False}`` (every other attribute read then resolves to
    ``None`` via :func:`_resolve_attribute`, which is falsy in every predicate this catalog
    declares). ``target`` itself is bound to the raw value passed in, so ``target == ...`` /
    ``target in ...`` compare against it directly and ``target.<field>`` falls back to ``None`` for
    any field a plain value does not carry.

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

    player = _merge_fields(index, _PLAYER_SOURCES, renames=_PLAYER_FIELD_RENAMES)
    congress_source = index.get(_CONGRESS_DECLARATION_ID)
    if isinstance(congress_source, Mapping) and "local_player_favor" in congress_source:
        player["diplomatic_favor"] = congress_source["local_player_favor"]
    # T313: `player.city_count`, from `cities.state` -- see `_local_player_city_count`'s own
    # docstring. Explicitly set even when `None` (rather than merely absent), so
    # `player.city_count != null` -- the guard `units.found_city`'s verification predicate opens
    # with -- reads a real key, never a missing-name evaluation error.
    player["city_count"] = _local_player_city_count(index)

    prompt = _bind_prompt_namespace(index)
    camera = _merge_fields(index, _CAMERA_SOURCES)

    bindings: dict[str, Any] = {
        "game": game,
        "player": player,
        "prompt": prompt,
        "camera": camera,  # T308: wired to camera.read_state, same merge shape as game/player.
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


#: The subject namespaces resolved *only* from the action's own ``target`` -- there is no
#: "currently selected congress resolution" the way there is a currently selected unit, so a
#: predicate over one of these cannot be decided until some concrete id is bound to ``target``.
#: :mod:`civsim_harness.act.availability` uses this to ask the question a human's greyed-out
#: button answers instead: *is there any entry the game is showing for which this holds?*
TARGET_RESOLVED_SUBJECTS: frozenset[str] = frozenset(
    {"other_player", "congress", "great_person", "spy"}
)

#: The two subject namespaces the game itself carries a selection for (README §4's parenthesis,
#: realised by :func:`resolve_selected_subject_target`).
SELECTED_SUBJECTS: frozenset[str] = frozenset(_SELECTED_SUBJECT_BY_ACTION_PREFIX.values())


def split_conjuncts(predicate: str) -> tuple[str, ...]:
    """*predicate*'s top-level ``and`` operands, each a predicate in its own right.

    ``"unit.is_selected and unit.can_found_city"`` -> two strings; anything that is not a
    top-level ``and`` (an ``or``, a bare comparison, a predicate this grammar will not parse)
    comes back as the single-element tuple ``(predicate,)``. This exists so a renderer can say
    *which part* of an availability predicate is not satisfied -- the same thing a human reads off
    a greyed-out button's tooltip -- without re-implementing the evaluator.
    """
    try:
        tree = _parse(predicate)
    except PredicateEvaluationError:
        return (predicate,)
    body = tree.body
    if isinstance(body, ast.BoolOp) and isinstance(body.op, ast.And):
        return tuple(ast.unparse(value) for value in body.values)
    return (predicate,)


def predicate_root_names(predicate: str) -> frozenset[str]:
    """Every root namespace *predicate* reads (``unit``, ``target``, ``game``, ...).

    Read from the same restricted parse :func:`evaluate_predicate` uses, so it agrees with the
    evaluator by construction rather than by a second regex. ``true``/``false``/``null`` are
    grammar, not namespaces, and are never returned. An unparseable predicate reads nothing.
    """
    try:
        tree = _parse(predicate)
    except PredicateEvaluationError:
        return frozenset()
    return frozenset(
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id not in _LITERAL_NAMES
    )


def predicate_attribute_chains(predicate: str) -> frozenset[tuple[str, str]]:
    """Every ``(namespace, attribute)`` pair *predicate* reads via one dot (``unit.is_selected`` ->
    ``("unit", "is_selected")``), for namespaces whose root is a plain :class:`ast.Name` -- i.e.
    every ``ast.Attribute`` node this restricted grammar can produce, since ``catalogs/README.md``
    §4's grammar has no subscripts, calls, or nested attribute chains beyond one level deep in
    practice for the root namespaces (``other_player.diplomatic_state``, not
    ``other_player.a.b``). Used by :func:`known_predicate_fields`'s ratchet (T3xx) to check every
    identifier an action predicate references against the field set an observation body actually
    emits, the same defect class that let ``diplomacy.declare_war`` reference
    ``other_player.diplomatic_state`` -- a name that parses, binds via ``bindings`` at runtime
    (:func:`_resolve_attribute` never raises for an absent key), and is therefore never caught by
    :func:`evaluate_predicate` itself. An unparseable predicate reads nothing, matching
    :func:`predicate_root_names`.
    """
    try:
        tree = _parse(predicate)
    except PredicateEvaluationError:
        return frozenset()
    return frozenset(
        (node.value.id, node.attr)
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
    )


def membership_collection(predicate: str) -> str | None:
    """For a predicate of the exact form ``target in <expr>``, ``<expr>``'s source; else ``None``.

    ``target in player.researchable_techs`` is the catalog's way of saying "pick one of these",
    and whether *any* can be picked is decidable before a target is chosen: the collection is
    either empty or it is not. That is precisely the difference between a greyed-out "Choose a
    belief" button and a live one.
    """
    try:
        tree = _parse(predicate)
    except PredicateEvaluationError:
        return None
    body = tree.body
    if (
        isinstance(body, ast.Compare)
        and len(body.ops) == 1
        and isinstance(body.ops[0], ast.In)
        and isinstance(body.left, ast.Name)
        and body.left.id == "target"
    ):
        return ast.unparse(body.comparators[0])
    return None


def subject_candidate_targets(namespace: str, observation: Observation) -> list[Any]:
    """Every id *observation* shows for a subject *namespace*, in the order the game listed them.

    These are exactly the values :func:`build_predicate_bindings` would bind that namespace from
    if they were passed as ``target`` -- the civilizations on the diplomacy screen, the
    resolutions in session, the recruitable great people, the spies. Empty when the observation
    carries no such list at all, which is itself the answer to "is there anything to act on".
    """
    source_spec = _SUBJECT_SOURCES.get(namespace)
    if source_spec is None:
        return []
    declaration_id, list_field, id_field = source_spec
    source = observation_index(observation).get(declaration_id)
    items = source.get(list_field) if isinstance(source, Mapping) else None
    if not isinstance(items, list):
        return []
    return [
        item[id_field]
        for item in items
        if isinstance(item, Mapping) and item.get(id_field) is not None
    ]


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


# --------------------------------------------------------------------------
# The phantom-field ratchet (T306): the emitted-field set an action predicate may reference,
# published as data.
# --------------------------------------------------------------------------
#
# `diplomacy.declare_war`'s `availability_predicate`/`verification_predicate` both read
# `other_player.diplomatic_state` -- a name `catalogs/observations/diplomacy.yaml`'s
# `output_schema` DOES declare, but `lua/gamecore/diplomacy.lua` never actually puts in the
# emitted JSON: `Player:GetDiplomaticAI()` is MEASURED absent in `GameCore_Tuner` (that file's own
# header), the GameCore-side fallback (`player:GetDiplomacy():GetDiplomaticStateIndex`) is marked
# UNVERIFIED, and a Lua table constructor silently drops a `key = nil` entry rather than emitting
# `"key":null` -- so whenever that fallback fails or returns nothing, the key is not merely null,
# it is ABSENT. CONFIRMED empirically (2026-09-22, read-only queries against
# `civsim-match-store.db{,.v1.0.bak-20260921T152108Z}`'s `decision_steps.bundle_json`, 670 + 115
# real captured bundles, including 670 `has_met: true` relations for a real met civilization
# (`CIVILIZATION_AUSTRALIA`)): the key union of every `diplomacy.state.relations[]` entry ever
# actually captured is exactly `{player_id, has_met, civilization, has_delegation}` --
# `diplomatic_state` NEVER appears, met or not. Because `_resolve_attribute` (above) resolves an
# absent key to `None` rather than raising, `other_player.diplomatic_state != "war"` reads `True`
# forever (declare_war's availability -- so the sampler believes war is always declarable) and
# `other_player.diplomatic_state == "war"` reads `False` forever (declare_war's verification -- so
# a war that actually lands is recorded `rejected`). This section exists so that specific defect
# shape -- a predicate identifier that resolves syntactically (no `PredicateEvaluationError`) but
# can never bind to a real value -- cannot recur silently in any other declared action.
#
# `camera` was the second, independently-found instance of the same shape, one layer down: unlike
# `diplomacy.state`, `catalogs/observations/camera.yaml`'s `camera.read_state` (added T221) DOES
# reliably back `mode`/`zoom`/`target_plot`/`target_is_revealed` in `lua/ingame/camera.lua` -- but
# `build_predicate_bindings` hardcoded `"camera": {}` unconditionally and was never updated to
# source it from `camera.read_state` once that declaration existed, so `camera.move`, `camera.zoom`,
# and `camera.set_view_mode`'s own `verification_predicate`s (`camera.target_plot == target and
# camera.target_is_revealed`, `camera.zoom == target`, `camera.mode == target`) could never be true
# either, regardless of the real camera state -- a wiring gap, not a missing Lua field, but the
# identical observable symptom. FIXED (T308): `build_predicate_bindings` now merges `camera` from
# `camera.read_state` the same way `game`/`player` are merged from their own sources (see
# `_CAMERA_SOURCES` above); `known_predicate_fields` below derives `camera`'s known-good field set
# from that same declaration's schema instead of hardcoding `frozenset()`, and the three camera
# actions' entries are removed from `KNOWN_PHANTOM_PREDICATE_FIELDS` below accordingly. All three
# camera verification predicates compare with `==` (or read a boolean field for truthiness) against
# `camera.*`, never `!=`/`not in` -- so an absent/null field (no `camera.read_state` entry, or a
# `pcall`-guarded read that came back empty) resolves the comparison to `False`, never fabricating a
# `True`: this wiring can only ever make a previously-unresolvable predicate correctly resolve to
# `False` more often, never turn an absent field into an `applied` verification. See
# `tests/unit/test_predicates.py`'s camera tests for the polarity proof, per predicate.
KNOWN_SCHEMA_BODY_DISAGREEMENTS: Mapping[DeclarationId, frozenset[str]] = {
    DeclarationId("diplomacy.state"): frozenset({"diplomatic_state"}),
}

#: Fields present on a bound namespace that :func:`build_predicate_bindings` derives or overlays
#: rather than copying straight off one observation body's own schema -- each cited to the exact
#: code above that adds it, so this table can never silently drift from what binding actually does.
_DERIVED_NAMESPACE_FIELDS: Mapping[str, frozenset[str]] = {
    # `_resolve_attribute`: every subject namespace defaults to `{"exists": False}` and every
    # `_bind` call sets `exists = True` -- present on `unit`/`city`/`other_player`/`congress`/
    # `great_person`/`spy` regardless of what their backing observation's schema declares.
    "unit": frozenset({"exists"}),
    "city": frozenset({"exists", "is_selected"}),  # T255: `is_selected` overlaid from cities.selection
    "other_player": frozenset({"exists"}),
    "congress": frozenset({"exists"}),
    "great_person": frozenset({"exists"}),
    "spy": frozenset({"exists"}),
    # `build_predicate_bindings`: `active_prompt_type` is computed from `game.current_screen`,
    # not copied from any schema.
    "game": frozenset({"active_prompt_type"}),
    # `build_predicate_bindings`: `diplomatic_favor` is merged in from `congress.state`'s
    # `local_player_favor`, a cross-namespace rename the schema tables below do not express.
    # T313: `city_count` is computed from `cities.state`'s `cities` list length (local-player
    # entries only, see `_local_player_city_count`) -- a derived scalar, not a field either
    # source schema declares by that name.
    "player": frozenset({"diplomatic_favor", "city_count"}),
    # `_bind_prompt_namespace`: built as a literal `{"type": ..., "is_active": ..., "options":
    # ...}`, never sourced from an `output_schema` at all.
    "prompt": frozenset({"type", "is_active", "options"}),
}

#: Root namespaces this ratchet does not check. `target` binds to whatever raw value the caller
#: passed as the action's own parameter (a plot, a name, a number, ...) -- never to an observation
#: body -- so its field vocabulary is the target_kind's shape, a contract this module has no way
#: to check and no business checking. `camera` (T308) IS checked like every other namespace now
#: that it is wired: its known-good set comes from `camera.read_state`'s own schema (see
#: `known_predicate_fields` below), not a hardcoded `frozenset()`.
UNCHECKED_PREDICATE_NAMESPACES: frozenset[str] = frozenset({"target"})


def _schema_top_level_fields(output_schema: Mapping[str, Any] | None) -> frozenset[str]:
    if not isinstance(output_schema, Mapping):
        return frozenset()
    properties = output_schema.get("properties")
    if not isinstance(properties, Mapping):
        return frozenset()
    return frozenset(properties.keys())


def _schema_list_item_fields(
    output_schema: Mapping[str, Any] | None, list_field: str
) -> frozenset[str]:
    if not isinstance(output_schema, Mapping):
        return frozenset()
    properties = output_schema.get("properties")
    if not isinstance(properties, Mapping):
        return frozenset()
    list_schema = properties.get(list_field)
    if not isinstance(list_schema, Mapping):
        return frozenset()
    items_schema = list_schema.get("items")
    if not isinstance(items_schema, Mapping):
        return frozenset()
    item_properties = items_schema.get("properties")
    if not isinstance(item_properties, Mapping):
        return frozenset()
    return frozenset(item_properties.keys())


def known_predicate_fields(
    declarations: Mapping[DeclarationId, Any],
) -> dict[str, frozenset[str]]:
    """The field set each predicate namespace can actually bind to, derived mechanically from
    *declarations*'s own declared ``output_schema``s -- never hand-typed -- through the exact same
    namespace -> backing-declaration tables :func:`build_predicate_bindings` itself binds from
    (``_GAME_SOURCES``/``_GAME_FIELD_RENAMES``, ``_PLAYER_SOURCES``/``_PLAYER_FIELD_RENAMES``,
    ``_SUBJECT_SOURCES``), so this can never silently drift from what the evaluator actually does:
    a rename applied there is applied here from the identical table, not a second, hand-copied one.

    *declarations* is a ``declaration_id -> ParityDeclaration``-shaped mapping (a loaded
    :class:`~civsim_harness.capability.loader.Catalog`'s own ``.declarations``, or any mapping
    exposing the same ``.output_schema`` attribute) -- passed in rather than loaded here so this
    stays a pure function of whatever catalog a caller already has open.

    Two adjustments on top of the mechanical schema union, both cited to their own reason:
    :data:`KNOWN_SCHEMA_BODY_DISAGREEMENTS` removes a field the schema declares but the Lua body
    is confirmed never to actually emit (today: exactly ``diplomacy.state``'s
    ``diplomatic_state``); :data:`_DERIVED_NAMESPACE_FIELDS` adds a field
    :func:`build_predicate_bindings` derives or overlays rather than copying off a schema. ``camera``
    (T308) is derived the same mechanical way as ``game``/``player``, from ``_CAMERA_SOURCES`` --
    i.e. ``camera.read_state``'s own ``output_schema`` -- since :func:`build_predicate_bindings` now
    actually binds it there.
    """

    def schema_of(declaration_id: DeclarationId) -> Mapping[str, Any] | None:
        declaration = declarations.get(declaration_id)
        schema = getattr(declaration, "output_schema", None)
        return schema if isinstance(schema, Mapping) else None

    fields: dict[str, frozenset[str]] = {}

    game_fields: set[str] = set()
    for source_id in _GAME_SOURCES:
        raw = _schema_top_level_fields(schema_of(source_id))
        renamed = {_GAME_FIELD_RENAMES.get(name, name) for name in raw}
        game_fields |= renamed
    fields["game"] = frozenset(game_fields) | _DERIVED_NAMESPACE_FIELDS["game"]

    player_fields: set[str] = set()
    for source_id in _PLAYER_SOURCES:
        raw = _schema_top_level_fields(schema_of(source_id))
        renamed = {_PLAYER_FIELD_RENAMES.get(name, name) for name in raw}
        player_fields |= renamed
    fields["player"] = frozenset(player_fields) | _DERIVED_NAMESPACE_FIELDS["player"]

    fields["prompt"] = _DERIVED_NAMESPACE_FIELDS["prompt"]

    camera_fields: set[str] = set()
    for source_id in _CAMERA_SOURCES:
        camera_fields |= _schema_top_level_fields(schema_of(source_id))
    fields["camera"] = frozenset(camera_fields) | _DERIVED_NAMESPACE_FIELDS.get(
        "camera", frozenset()
    )

    for namespace, (declaration_id, list_field, _id_field) in _SUBJECT_SOURCES.items():
        item_fields = _schema_list_item_fields(schema_of(declaration_id), list_field)
        if namespace == "congress":
            item_fields |= _schema_top_level_fields(schema_of(declaration_id)) & frozenset(
                _CONGRESS_TOP_LEVEL_FIELDS
            )
        disagreements = KNOWN_SCHEMA_BODY_DISAGREEMENTS.get(declaration_id, frozenset())
        fields[namespace] = (
            item_fields - disagreements
        ) | _DERIVED_NAMESPACE_FIELDS.get(namespace, frozenset())

    return fields


#: Declared action predicates KNOWN, today, to reference a ``(namespace, attribute)`` pair that
#: cannot resolve to anything :func:`known_predicate_fields` reports as bound -- the completed
#: output of the T306 sweep, each entry reviewed and cited rather than silently excluded. Growing
#: this table is a diff a reviewer sees; the ratchet (``tests/unit/test_predicate_field_ratchet.py``)
#: fails the moment a listed declaration's predicate changes so that it no longer needs its entry,
#: the same staleness discipline ``live.goal_run.KNOWN_AVAILABILITY_EXCEPTIONS`` already applies to
#: goals.
KNOWN_PHANTOM_PREDICATE_FIELDS: Mapping[DeclarationId, frozenset[tuple[str, str]]] = {
    # PROVEN (2026-09-22, T306 sweep). See KNOWN_SCHEMA_BODY_DISAGREEMENTS's own comment above:
    # `diplomacy.state` never actually emits `diplomatic_state` on this build (confirmed against
    # 670 + 115 real captured bundles). A live `declare_war` that actually landed and ended a run
    # today was recorded `rejected` for exactly this reason. NOT fixed in this pass: no honest,
    # guardable accessor was confirmed in the time available without a live client to verify a new
    # Lua call against (specs/002-civ-playing-harness/spikes/client-segfault-2026-09-21.md's own
    # rule: a new engine call unverified live "may crash the client"). Reported as tasks T307/T308
    # rather than patched blind. `diplomacy.make_peace` shares the identical identifier and
    # therefore the identical defect -- not a second, separate finding.
    DeclarationId("diplomacy.declare_war"): frozenset({("other_player", "diplomatic_state")}),
    DeclarationId("diplomacy.make_peace"): frozenset({("other_player", "diplomatic_state")}),
    # `camera.move`/`camera.zoom`/`camera.set_view_mode` WERE listed here (T306 sweep): they were
    # a second, independently-found instance of the same defect shape, at `build_predicate_bindings`
    # hardcoding `"camera": {}` instead of sourcing it from `camera.read_state` (added T221). FIXED
    # (T308): `camera` is now wired the same way `game`/`player` are (see `_CAMERA_SOURCES` and this
    # module's binding-header comment above), so all three now resolve against a real observation
    # and the exception entries are removed -- an unresolvable exception the fix already closed is
    # exactly the kind of rot this table's own docstring warns against leaving in place.
}


def unresolved_predicate_fields(
    *,
    availability_predicate: str,
    verification_predicate: str,
    known_fields: Mapping[str, frozenset[str]],
) -> frozenset[tuple[str, str]]:
    """Every ``(namespace, attribute)`` pair either predicate references that cannot resolve
    against *known_fields* -- ignoring :data:`UNCHECKED_PREDICATE_NAMESPACES` -- with no exception
    table applied. Callers that want the reviewed, ``KNOWN_PHANTOM_PREDICATE_FIELDS``-aware check
    should use :func:`assert_predicate_fields_resolve`; this is the raw computation it (and a
    negative control) build on.
    """
    unresolved: set[tuple[str, str]] = set()
    for predicate in (availability_predicate, verification_predicate):
        for namespace, attr in predicate_attribute_chains(predicate):
            if namespace in UNCHECKED_PREDICATE_NAMESPACES:
                continue
            if attr not in known_fields.get(namespace, frozenset()):
                unresolved.add((namespace, attr))
    return frozenset(unresolved)


def assert_predicate_fields_resolve(
    *,
    declaration_id: DeclarationId,
    availability_predicate: str,
    verification_predicate: str,
    known_fields: Mapping[str, frozenset[str]],
    exceptions: Mapping[DeclarationId, frozenset[tuple[str, str]]] = KNOWN_PHANTOM_PREDICATE_FIELDS,
) -> None:
    """Raise :class:`AssertionError` unless every identifier *declaration_id*'s
    ``availability_predicate``/``verification_predicate`` reference resolves to a field some
    observation body actually emits (per *known_fields*, see :func:`known_predicate_fields`) --
    except a pair *exceptions* names for this exact ``declaration_id``, each one a reviewed,
    already-reported finding (:data:`KNOWN_PHANTOM_PREDICATE_FIELDS`) rather than a silent pass.

    This is the ratchet ``diplomacy.declare_war`` should have tripped before today: a
    ``PredicateEvaluationError`` is never raised for an unresolvable attribute
    (:func:`_resolve_attribute` returns ``None``/``False`` for any name a bound namespace does not
    carry, by design, so a human-readable "not available right now" can be reported instead of a
    crash) -- which is exactly why an absent field is invisible at runtime and must be caught here,
    statically, instead.
    """
    allowed = exceptions.get(declaration_id, frozenset())
    unresolved = (
        unresolved_predicate_fields(
            availability_predicate=availability_predicate,
            verification_predicate=verification_predicate,
            known_fields=known_fields,
        )
        - allowed
    )
    if unresolved:
        named = ", ".join(f"{namespace}.{attr}" for namespace, attr in sorted(unresolved))
        raise AssertionError(
            f"{declaration_id}: predicate references {named}, which no observation body emits. "
            "A predicate that references a field no observation body emits can never be true, so "
            "the action it guards will be recorded as refused even when it works."
        )
