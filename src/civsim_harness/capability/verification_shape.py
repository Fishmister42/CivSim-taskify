"""Load-time rejection of a verification predicate a NO-OP can satisfy (T322).

**The defect this exists to make unrepeatable.** ``prompts.ai_diplomatic_approach`` verified with
``target not in prompt.options`` -- a *negative over a container*. That expression is satisfied by
success, and it is equally satisfied by the container emptying, by the prompt being dismissed, by
the board moving on, and by nothing happening at all. MEASURED (``civsim-match-store.db``,
2026-09-22): all 14 of that action's ``applied`` records confirmed on the FIRST post-dispatch read
(``confirm_attempts: 1``, ``confirm_elapsed_s: 0.0``), and 12 of the 14 confirmed against
``prompt_options: []``. In 7 of those the engine's own ``raw_screen_id`` was **unchanged** across
the action -- ``DiplomacyActionView`` before and after, with ``has_blocking_prompt`` merely
flipping false -- while the other 5 left ``DiplomacyActionView`` for ``InGame``. Only the option
list distinguished them, and the predicate read both the same way.

**Stated precisely, because the weaker claim is the provable one.** This does NOT establish that
those 7 were no-ops -- the build exposes no session-state read (``IsSessionActive()`` does not
exist here) and the once-popular ``CloseSession()``-is-a-no-op explanation was **retracted** on
2026-09-22 (``specs/002-civ-playing-harness/analyze-2026-09-22.md``: the tuner returns the
pre-call value when the readback rides in the same command; "Both calls worked; the measurement
was wrong"). What IS established is about the predicate, not the game: **it cannot discriminate
"the leader answered" from "the options went away", and it records ``applied`` either way.** A
verification that cannot tell those apart is a Principle I defect whichever way the 7 actually
went, which is why the fix is to the predicate and the claim about the 7 stays graded.

**Why ``d74a4c7`` did not close it, verified rather than assumed.** That change made a comparison
with a *missing* operand unevaluable. It cannot fire here:
:func:`~civsim_harness.act.predicates._bind_prompt_namespace` hard-defaults ``options`` to ``[]``,
so the right operand is never ``None``. ``d74a4c7`` closes **absence**; this closes **emptiness**,
and an empty list is a value (``.specify/memory/loop-state.md``). Two of the fourteen records were
produced with ``d74a4c7`` already in the running tree, which is the direct evidence that it does
not cover this.

**A rule is not a control, so this is not a rule.** Telling future catalog authors "assert
something positive" makes Principle I rest on every author phrasing predicates correctly. Instead
the catalog *rejects* the shape at load time: :func:`assert_verification_asserts_something_positive`
runs inside :func:`~civsim_harness.capability.loader.load_catalog`, so a declaration carrying the
shape cannot be loaded at all, and a new one cannot be added without also adding itself to
:data:`KNOWN_NEGATIVE_VERIFICATIONS` below -- a diff a reviewer sees, the same staleness discipline
``act.predicates.KNOWN_PHANTOM_PREDICATE_FIELDS`` already applies.

**What counts as asserting something positive.** Every top-level ``or`` branch must contain at
least one assertion that some state was *reached*, not merely that some state is *no longer* on
offer:

* ``==`` / ``<`` / ``<=`` / ``>`` / ``>=`` / ``in`` -- a value was reached, or a thing appeared in
  a collection.
* a bare field read used for its truth (``city.is_selected``, ``player.religion_founded``) -- a
  flag became true. Under a ``not`` it stops counting, which is the ``great_people.recruit`` shape.
* ``!=`` with an explicit ``null`` literal on one side (``spy.mission != null``) -- a presence
  test, the catalog's own deliberate idiom for asking about absence (``d74a4c7``'s carve-out).
* ``!=`` / ``not in`` against an ``observed_*`` pre-execution snapshot -- a change *relative to a
  recorded baseline* is a positive statement about the board, unlike a change relative to a
  literal.

**What this does NOT claim.** It is a shape check, not a proof of correctness: a predicate can pass
it and still be wrong (a positive conjunct pinned to the wrong subject, say). It removes one
enumerable failure shape -- the bare negative -- rather than certifying the rest. Said plainly here
because a check described as stronger than it is becomes a false all-clear, which is the exact
class of defect this module was written in response to.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping

from civsim_harness.capability.predicates import OBSERVED_PREFIX
from civsim_harness.models.common import DeclarationId

#: Comparison operators that assert a state was REACHED.
_POSITIVE_OPERATORS: tuple[type[ast.cmpop], ...] = (
    ast.Eq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.In,
)

#: ``true``/``false``/``null`` are grammar, not fields -- a bare one of these asserts nothing about
#: the board (``catalogs/README.md`` §4, and ``act.predicates._LITERAL_NAMES``).
_GRAMMAR_NAMES: frozenset[str] = frozenset({"true", "false", "null"})


def _is_null_literal(node: ast.AST) -> bool:
    """The catalog grammar's own ``null``, written out in the predicate source."""
    return (isinstance(node, ast.Name) and node.id == "null") or (
        isinstance(node, ast.Constant) and node.value is None
    )


def _is_observed_snapshot(node: ast.AST) -> bool:
    """An ``observed_*`` name -- the pre-execution reading ``act.verify`` threads in.

    A ``!=`` against one of these compares the board to *what it was before this action*, which is
    a positive statement that something changed. A ``!=`` against a literal is not: it is satisfied
    by every other value the field could ever hold, including the ones that mean nothing happened.
    """
    return isinstance(node, ast.Name) and node.id.startswith(OBSERVED_PREFIX)


def _compare_is_positive(node: ast.Compare) -> bool:
    operands: list[ast.AST] = [node.left, *node.comparators]
    for index, op in enumerate(node.ops):
        left, right = operands[index], operands[index + 1]
        if isinstance(op, _POSITIVE_OPERATORS):
            return True
        if isinstance(op, (ast.NotEq, ast.NotIn)):
            if _is_null_literal(left) or _is_null_literal(right):
                return True
            if _is_observed_snapshot(left) or _is_observed_snapshot(right):
                return True
    return False


def _positive_assertion_count(node: ast.AST, *, negated: bool) -> int:
    """How many positive assertions *node* makes, not counting any made under a ``not``."""
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return _positive_assertion_count(node.operand, negated=not negated)
    if isinstance(node, ast.BoolOp):
        return sum(_positive_assertion_count(value, negated=negated) for value in node.values)
    if negated:
        return 0
    if isinstance(node, ast.Compare):
        return 1 if _compare_is_positive(node) else 0
    if isinstance(node, ast.Attribute):
        return 1
    if isinstance(node, ast.Name):
        return 0 if node.id in _GRAMMAR_NAMES else 1
    return 0


def _disjuncts(node: ast.AST) -> list[ast.AST]:
    """*node*'s top-level ``or`` branches -- each one able to confirm the action by itself."""
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
        branches: list[ast.AST] = []
        for value in node.values:
            branches.extend(_disjuncts(value))
        return branches
    return [node]


def negative_only_branches(verification_predicate: str) -> tuple[str, ...]:
    """Each top-level ``or`` branch of *verification_predicate* that asserts nothing positive.

    An empty result means every branch states that some state was reached. A non-empty result names
    the branches a no-op can satisfy, rendered back from the parse so the caller quotes the branch
    rather than the whole predicate. An unparseable predicate returns ``()`` -- it is not this
    check's job to report a syntax error the evaluator's own ``_parse`` already reports.
    """
    try:
        tree = ast.parse(verification_predicate, mode="eval")
    except SyntaxError:
        return ()
    return tuple(
        ast.unparse(branch)
        for branch in _disjuncts(tree.body)
        if _positive_assertion_count(branch, negated=False) == 0
    )


#: Shipped declarations whose ``verification_predicate`` IS a bare negative today, each with the
#: reason it is still listed and -- the thing that actually matters -- **which way it fails**.
#: Reported in full rather than fixed in one pass: several are correct-by-domain and the rest need
#: a live client to establish what the game exposes instead. Growing this table is a diff a reviewer
#: sees; :func:`assert_no_stale_negative_verification_exceptions` fails the moment an entry stops
#: being needed, so a fix cannot leave its exception rotting behind it.
KNOWN_NEGATIVE_VERIFICATIONS: Mapping[DeclarationId, str] = {
    # ---- the acknowledge-only and answer-a-prompt families: `game.current_screen != "prompt.X"`.
    # Satisfied by the prompt going away for ANY reason. For a one-button popup ("continue") that
    # is close to the real effect, since dismissing IS the effect -- but it is also satisfied by
    # `lua/ingame/screens.lua`'s documented allowlist fall-through, where a screen absent from
    # CIVSIM_SCREEN_WATCHLIST reads as `world` (loop-state's third allowlist-as-detector instance,
    # open separately). NOT fixed here: the fall-through is the other lane's live file this pass
    # must not touch, and a screen-identity fix is the honest remedy for all twelve at once.
    DeclarationId("prompts.unit_promotion"): "screen-went-away; see allowlist fall-through",
    DeclarationId("prompts.pantheon_selection"): "screen-went-away; see allowlist fall-through",
    DeclarationId("prompts.great_person_selection"): "screen-went-away; allowlist fall-through",
    DeclarationId("prompts.declare_war_response"): "screen-went-away; allowlist fall-through",
    DeclarationId("prompts.congress_vote"): "screen-went-away; allowlist fall-through",
    DeclarationId("prompts.congress_intro"): "screen-went-away; allowlist fall-through",
    DeclarationId("prompts.era_transition"): "screen-went-away; allowlist fall-through",
    # SHARPEST of the twelve: the chooser's own X dismisses WITHOUT dedicating anything
    # (`catalogs/actions/prompts.yaml`'s own note, dedicationpopup.lua:225-227), so "the screen is
    # gone" is satisfied by a dedication that was never made. Fabricates.
    DeclarationId("prompts.era_dedication"): "screen-went-away AND the X dedicates nothing",
    DeclarationId("prompts.tech_civic_completed"): "screen-went-away; allowlist fall-through",
    DeclarationId("prompts.boost_unlocked"): "screen-went-away; allowlist fall-through",
    DeclarationId("prompts.natural_disaster"): "screen-went-away; allowlist fall-through",
    DeclarationId("prompts.great_work_created"): "screen-went-away; allowlist fall-through",
    # ---- negatives over a container: "the thing I asked for is no longer on the list".
    # Each is satisfied by the list emptying or by the backing observation reporting a shorter
    # list for an unrelated reason (the screen closed, the city deselected, the era changed).
    # Same shape as the defect above; none has a live `applied` record to prove it fired, which is
    # why they are reported rather than rewritten blind.
    DeclarationId("cities.purchase_with_gold"): "negative over city.purchasable_with_gold",
    DeclarationId("cities.purchase_with_faith"): "negative over city.purchasable_with_faith",
    DeclarationId("policies.slot_policy"): "negative over player.available_policies",
    DeclarationId("policies.assign_governor"): "negative over player.available_governors",
    DeclarationId("religion.select_belief"): "negative over player.available_beliefs",
    DeclarationId("units.promote"): "negative over unit.available_promotions",
    # ---- negation of a bare field. `not great_person.is_recruitable` reads `not None` -> True
    # whenever the field is absent, and `_refuse_unresolved_operand` cannot see it because there is
    # no `ast.Compare` node to guard. Closed at the evaluator in this same pass
    # (`act.predicates._refuse_unresolved_unary_operand`); the SHAPE is still a bare negative, so it
    # stays listed until the declaration asserts the recruitment positively.
    DeclarationId("great_people.recruit"): "negation of a bare field; absence hole closed T322",
    # ---- `!=` against a literal. loop-state already names this one: `declare_war`'s `== "war"`
    # under-reports on the same absent field while `make_peace`'s `!= "war"` fabricates, and it is
    # unreachable today only because availability reads that field first. Listed, not fixed: the
    # field `diplomacy.state` never actually emits is `KNOWN_PHANTOM_PREDICATE_FIELDS`' finding and
    # needs a live-verified accessor, not a predicate rewrite.
    DeclarationId("diplomacy.make_peace"): "!= against a literal on a never-emitted field",
}


def assert_verification_asserts_something_positive(
    *,
    declaration_id: DeclarationId,
    verification_predicate: str,
    exceptions: Mapping[DeclarationId, str] = KNOWN_NEGATIVE_VERIFICATIONS,
) -> None:
    """Raise :class:`AssertionError` unless every ``or`` branch asserts something positive.

    *exceptions* names the shipped declarations reviewed and knowingly left in this shape
    (:data:`KNOWN_NEGATIVE_VERIFICATIONS`). Everything else -- every declaration added from here on
    -- must state what the action MADE TRUE, not what it stopped being.
    """
    if declaration_id in exceptions:
        return
    branches = negative_only_branches(verification_predicate)
    if branches:
        rendered = "; ".join(branches)
        raise AssertionError(
            f"{declaration_id}: verification_predicate asserts only that something is no longer "
            f"true ({rendered}). A negative over a container or a field is satisfied by success, "
            "by the subject going away, and by the container simply emptying -- so a no-op is "
            "recorded `applied`, which nothing downstream can ever detect. State what the action "
            "made true instead (==, in, a flag that became set, or a change against an observed_* "
            "pre-execution snapshot). If this declaration genuinely has no positive read-back, add "
            "it to KNOWN_NEGATIVE_VERIFICATIONS with the reason and its failure direction."
        )


def assert_no_stale_negative_verification_exceptions(
    predicates: Mapping[DeclarationId, str],
    *,
    exceptions: Mapping[DeclarationId, str] = KNOWN_NEGATIVE_VERIFICATIONS,
) -> None:
    """Raise :class:`AssertionError` if any *exceptions* entry is no longer needed.

    *predicates* maps declaration id -> that declaration's ``verification_predicate``. An exception
    that outlives the shape it excused is how a table like this rots into a blanket permission --
    and an unresolvable exception is exactly what ``KNOWN_PHANTOM_PREDICATE_FIELDS``' own docstring
    warns against leaving in place.
    """
    stale = sorted(
        str(declaration_id)
        for declaration_id, predicate in predicates.items()
        if declaration_id in exceptions and not negative_only_branches(predicate)
    )
    missing = sorted(
        str(declaration_id)
        for declaration_id in exceptions
        if declaration_id not in predicates
    )
    if stale or missing:
        raise AssertionError(
            "KNOWN_NEGATIVE_VERIFICATIONS is stale. No longer a bare negative (remove the entry): "
            f"{stale or 'none'}. Named but not in the catalog at all (remove the entry): "
            f"{missing or 'none'}."
        )
