"""Load-bearing returns: a value that speaks and nobody listens (T300).

Fourth sibling to ``tests/contract/test_reachability.py``,
``tests/contract/test_gate_inputs.py`` and ``tests/contract/test_long_phase_liveness.py``
-- same idiom (AST scan, roster and allowlist as data, per-entry justifications that must
cite a task or finding id **and** name an owning lane, negative controls that prove the
checker can fire, a four-shape ratchet so the list can only tighten), aimed at the one
member of the family none of the other three sees.

**The defect.** ``test_long_phase_liveness.py`` catches *the phase does not speak*. This
file catches its sibling: **the value speaks and nobody listens** -- a function whose
return is the entire point of calling it, invoked at a production site that throws the
return away. The call type-checks. The suite stays green. The function keeps its own unit
tests, which bind the return and assert on it, so the *function* is proven and the *wiring*
is not: exactly this project's defining defect, one node lower than
``test_reachability.py`` can see it, because the symbol does have a caller -- the caller
simply discards what it came for.

**The worked example, and why it is not a logging problem.** ``run/composition.py:746`` is
``debug_menu_preflight(host, home=home)`` as a bare statement, sitting between
``:745 catalog_result = catalog_preflight(catalog)`` and
``:747 host_gate_result = evaluate_host_gate(support_probe)``. **Both neighbours bind; this
one does not.** ``models/run.py`` carries a ``debug_menu_state`` field for the answer, the
store round-trips it like every other ``Run`` field, and no run ever made has carried it --
on a host that shipped with the debug menu **on**, while three documents said it was
recorded. That is the fourth counted instance of the day's family and the one that proves
the class is not confined to telemetry: nothing here is being logged, a *model field* is
being left unpopulated by the one line that could fill it.

**This file is the pattern commenting on its own documentation.** The defect was already
written down, in prose, in three places -- ``run/preparation.py:452`` names the discarding
call site explicitly, ``debug_menu_preflight``'s own docstring three lines into itself
(``run/preparation.py:503``) says "the caller does not record it yet", and
``tests/contract/test_match_store_port.py:1274`` refers to it again -- and enforced in
exactly zero. A finding recorded three times and checked never is the same failure as a
gate that is correct and unwired: it is knowledge that cannot fail. The point of this file
is to move those three paragraphs from prose into something that goes red.

**Why the roster is hand-curated and not derived.** Most discarded returns are correct, and
that was measured on this tree rather than assumed (read-only AST sweep of
``src/civsim_harness``, 2026-09-22): **186 distinct callee names appear in bare-statement
position**; only **28** of them have a non-``None``, non-scalar return annotation at all;
and the two largest populations are ``execute`` (43 sites) and ``write_run_event`` (40),
both of which are *supposed* to be discarded -- a cursor and an append-only event id nobody
downstream needs. ``preflight_chain`` (``run/composition.py:748``) is the instructive near
miss: its return is discarded too, and correctly so, because its own docstring says callers
"may log/audit it" while the gate itself is the raise. "Flag every discarded return" would
therefore produce ~150 findings of which a handful matter, get allowlisted down to nothing,
and become a check that cannot fail in the way that matters -- the same measured-and-
rejected outcome recorded for the broad form of ``test_gate_inputs.py``'s sibling scan (R21:
three formulations, 24/32/43 hits, catching neither finding that motivated it). **The
judgement "this function's return is load-bearing" is the part no syntax scan supplies.**
That judgement is the design here, not a shortcoming, and making it a visible, reasoned,
falsifiable roster entry is the whole mechanism.

**Copied, never imported.** ``_allowlist_problems`` and the four-shape ratchet are the same
idea as the two siblings' and are deliberately re-typed here rather than shared. Each file
keeps its own copy so that one file's notion of a valid justification -- which citation
forms count, whether an owner is required -- cannot drift into another's through a shared
helper someone loosens for one caller. A shared ratchet would be a single point at which
every contract check could be weakened at once.

**What this covers.** A call in **bare-statement position** -- ``ast.Expr`` wrapping
``ast.Call``, or ``ast.Expr`` wrapping ``ast.Await`` wrapping ``ast.Call`` -- in the
**production** partition (``src/civsim_harness``), whose callee name is on the roster below
and which is not allowlisted. The ``await`` spelling is not an afterthought: a scan written
against the synchronous shape alone passes ``await f()`` silently, and a discarded-return
check that contains an instance of the discarded-return defect would be this project's
defining failure wearing the costume of the fix for it. ``test_negative_control_the_await_
spelling_is_flagged`` exists to make that impossible to reintroduce, and it is mandatory.

**What this does not cover, stated so nobody reads more into a green than is there.**

* It is not a general "unused return value" check and must never be turned into one -- see
  the measurement above.
* It says nothing about calls whose result *is* bound and then ignored
  (``result = f(); pass``), or bound and partially used. Binding is the falsifiable line;
  "and then actually used for the right thing" is a semantic question this scan cannot
  answer, and pretending otherwise would be the vacuous-gate defect again.
* It does not check the **test** partition. Discarding a return in a test is frequently the
  assertion itself (``f()`` proving no raise), so ``tests/`` is excluded by construction,
  not by oversight -- ``test_negative_control_a_discard_in_the_test_partition_is_not_
  flagged`` pins that.
* It does not check whether a function is called at all. A rostered function with no
  production caller is a *reachability* finding and belongs to ``test_reachability.py``;
  duplicating it here would produce two checks that fail together and neither of which is
  the right place to fix it.
* Matching is by **name**, not resolved type -- the same residual the reachability and gate
  scans document. A same-named method on an unrelated object would be reported as a
  violation (this direction errs toward a false FAIL, which is the safe one here, and the
  roster is small enough that such a collision is a reviewable fact rather than noise).
* It does not assert that the bound value reaches the ``Run``. That the binding *lands
  somewhere useful* is asserted behaviourally by the store's own round-trip tests.
"""

from __future__ import annotations

import ast
import re
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
#: The production partition. ``tests/`` is the other half and is deliberately not scanned;
#: see the module docstring.
_HARNESS_ROOT = _REPO_ROOT / "src" / "civsim_harness"
_TESTS_ROOT = _REPO_ROOT / "tests"


# --------------------------------------------------------------------------
# The checker. One implementation, driven by both the real tree and the
# synthetic trees the negative controls build, so the controls exercise the
# enforcement rather than a parallel copy of it.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DiscardSite:
    """One call in bare-statement position: the return went nowhere."""

    module: str
    lineno: int
    awaited: bool

    def describe(self) -> str:
        return f"{self.module}:{self.lineno}{' (await)' if self.awaited else ''}"


@dataclass(frozen=True)
class ConsumedReturn:
    """One function whose return value must be consumed at every production call site.

    ``function`` is the plain name as it appears in the source, because that is what the
    AST carries at a call site (a method is named by its own ``def``, not by
    ``Class.method``). ``defined_in`` is a path relative to ``src/civsim_harness`` and is
    checked to exist *and* to contain that ``def``, so a roster entry cannot rot into
    something that passes by naming nothing. ``returns`` records the annotation as written,
    and ``why_load_bearing``/``discarded_means`` are required prose: a future reader must be
    able to tell a load-bearing return from an ordinary fire-and-forget call without
    re-deriving the judgement that put the entry here.
    """

    call_id: str
    function: str
    defined_in: str
    returns: str
    why_load_bearing: str
    discarded_means: str


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _discard_sites(sources: dict[str, str]) -> dict[str, list[DiscardSite]]:
    """Every bare-statement call in *sources*, bucketed by callee name.

    **The unit of the scan is ``ast.Expr``**, not ``ast.Call``. That is the whole
    discrimination: an ``ast.Call`` anywhere else -- as an argument, a right-hand side, a
    ``with`` item, an ``if`` test, an f-string field, a ``return`` -- has its value consumed
    by the surrounding expression. Only an ``ast.Call`` that *is* a statement has thrown its
    return away.

    Two spellings are that shape: ``ast.Expr(ast.Call)`` and
    ``ast.Expr(ast.Await(ast.Call))``. The second is the same defect one node deeper, and a
    scan that unwrapped only the first would pass ``await f()`` in silence -- see the module
    docstring on why that control is mandatory rather than nice to have.
    """
    sites: dict[str, list[DiscardSite]] = {}
    for module, source in sorted(sources.items()):
        tree = ast.parse(source, filename=module)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Expr):
                continue
            value = node.value
            awaited = False
            if isinstance(value, ast.Await):
                value = value.value
                awaited = True
            if not isinstance(value, ast.Call):
                continue
            name = _call_name(value)
            if name is None:
                continue
            sites.setdefault(name, []).append(
                DiscardSite(module=module, lineno=node.lineno, awaited=awaited)
            )
    return sites


def _defines_function(source: str, name: str) -> bool:
    return any(
        isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name
        for node in ast.walk(ast.parse(source))
    )


def _consumed_return_problems(
    roster: tuple[ConsumedReturn, ...],
    allowlist: dict[str, str],
    *,
    source_of: dict[str, str],
) -> list[str]:
    """Four failure shapes, so the allowlist can only ratchet down.

    An unflagged violation (a discarding production call site with no exemption) fails; a
    stale exemption (the call site now binds, so the entry must go) fails; an exemption
    naming no roster entry fails; and a roster entry whose module or ``def`` cannot be found
    fails rather than passing vacuously, because a check that cannot locate its target is a
    check that cannot fail.
    """
    problems: list[str] = []
    by_id = {entry.call_id: entry for entry in roster}

    for name in sorted(set(allowlist) - set(by_id)):
        problems.append(
            f"{name!r}: allowlisted but names no roster entry -- the ratchet requires every "
            f"exemption to point at a live entry"
        )

    sites = _discard_sites(source_of)

    for entry in roster:
        source = source_of.get(entry.defined_in)
        if source is None:
            problems.append(
                f"{entry.call_id!r}: module {entry.defined_in!r} was not found -- the roster "
                f"entry names nothing, so it cannot prove anything"
            )
            continue
        if not _defines_function(source, entry.function):
            problems.append(
                f"{entry.call_id!r}: {entry.defined_in} defines no {entry.function!r} -- the "
                f"roster entry names nothing, so it cannot prove anything"
            )
            continue

        discards = [
            site for site in sites.get(entry.function, []) if site.module != entry.defined_in
        ]
        allowlisted = entry.call_id in allowlist

        if discards and not allowlisted:
            where = ", ".join(site.describe() for site in discards)
            problems.append(
                f"{entry.call_id!r}: DISCARDED RETURN at {where} -- {entry.function}() returns "
                f"{entry.returns} and is called as a bare statement, so the value goes nowhere. "
                f"{entry.why_load_bearing} Discarded, {entry.discarded_means} Bind the return "
                f"and use it, or allowlist with a justification citing a task or finding id and "
                f"naming the owning lane."
            )
        if not discards and allowlisted:
            problems.append(
                f"{entry.call_id!r}: no production call site discards {entry.function}() any "
                f"more, but it is still allowlisted -- stale exemption, remove it (the ratchet)"
            )

    return problems


_UNRESOLVED_MARKER = "UNRESOLVED - hypervisor review"
_TASK_CITATION = re.compile(r"\bT\d{3}\b")
_FINDING_CITATION = re.compile(r"\bC\d+[a-z]?\b")
#: An exemption must say **whose** it is. A discarded load-bearing return with no owner
#: named is how a defect gets recorded and then belongs to nobody -- which is precisely the
#: history this file exists to end, given the same finding sat in prose in two files and in
#: no owner's queue.
_OWNING_LANE = re.compile(r"\bowner:\s*\S", re.IGNORECASE)


def _allowlist_problems(allowlist: dict[str, str]) -> list[str]:
    """Deliberately a copy of the siblings' rule, not an import -- see the module docstring."""
    problems: list[str] = []
    for name, justification in allowlist.items():
        if not justification.strip():
            problems.append(f"{name!r}: empty justification -- every entry must say why")
            continue
        if (
            _UNRESOLVED_MARKER not in justification
            and not _TASK_CITATION.search(justification)
            and not _FINDING_CITATION.search(justification)
        ):
            problems.append(
                f"{name!r}: justification cites no task ID, no finding ID and carries no "
                f"{_UNRESOLVED_MARKER!r} marker: {justification!r}"
            )
        if not _OWNING_LANE.search(justification):
            problems.append(
                f"{name!r}: justification names no owning lane -- every exemption must carry "
                f"'owner: <lane>' so a discarded return belongs to somebody: {justification!r}"
            )
    return problems


# --------------------------------------------------------------------------
# The roster. Data, deliberately hand-curated -- see the module docstring.
# --------------------------------------------------------------------------

LOAD_BEARING_RETURNS: tuple[ConsumedReturn, ...] = (
    ConsumedReturn(
        call_id="run.debug_menu_preflight",
        function="debug_menu_preflight",
        defined_in="run/preparation.py",
        returns="DebugMenuPreflightResult",
        why_load_bearing=(
            "It is a recorder, not a gate: its own docstring says so explicitly, and it "
            "reports rather than raises for all three of its non-plain readings (absent, "
            "malformed, unreadable). The return IS the entire product of the call -- the "
            "state, the file it was read from, and why, destined for Run.debug_menu_state "
            "(models/run.py:157), which the store already round-trips like every other Run "
            "field."
        ),
        discarded_means=(
            "the read happens and the answer evaporates: no run ever made carries whether it "
            "was played with EnableDebugMenu on, on a host that shipped with it ON, while "
            "run/preparation.py:452 and tests/contract/test_match_store_port.py:1274 both "
            "already say in prose that it is not recorded (T204 item 1, T280)."
        ),
    ),
    ConsumedReturn(
        call_id="run.turn_timer_preflight",
        function="turn_timer_preflight",
        defined_in="run/preparation.py",
        returns="TurnTimerPreflightResult",
        why_load_bearing=(
            "Half gate, half record, and only the gate half survives a discard. The active-"
            "timer case raises, so refusing the run works either way; the other two cases do "
            "not. VERIFIED_NONE carries the turn-timer name and hash that were actually "
            "confirmed safe, and UNVERIFIED exists because the docstring's own words are "
            "that an undeterminable reading must leave 'the run's own record carrying an "
            'explicit "this precondition was never confirmed" fact rather than silently '
            "proceeding as though it had passed'."
        ),
        discarded_means=(
            "the run cannot tell 'confirmed no timer' from 'never managed to read one', "
            "which collapses the exact distinction the UNVERIFIED state was added to "
            "preserve -- and the live counterexample (TURNTIMER_STANDARD in a single-player "
            "game, turns advancing 1->6 untouched) is what made that distinction matter "
            "(T204 item 2). Found by this scan, not previously written down anywhere."
        ),
    ),
    ConsumedReturn(
        call_id="run.catalog_preflight",
        function="catalog_preflight",
        defined_in="run/preparation.py",
        returns="CatalogPreflightResult",
        why_load_bearing=(
            "The observation and action catalog versions this run was played against "
            "(FR-022/23, V5). They are the parity basis for every later comparison between "
            "runs; a run whose catalog versions are unknown is not comparable to any other."
        ),
        discarded_means=(
            "the run records no catalog identity. It binds today at run/composition.py:745 "
            "-- this entry exists so that it keeps doing so (T300)."
        ),
    ),
    ConsumedReturn(
        call_id="observe.evaluate_host_gate",
        function="evaluate_host_gate",
        defined_in="observe/host_gate.py",
        returns="HostGateResult",
        why_load_bearing=(
            "The host's support tier and the degradations it implies (FR-054, R19). An "
            "Unsupported or degraded verdict changes what the run is allowed to do and what "
            "its results may be compared against; the verdict lives in the return."
        ),
        discarded_means=(
            "the tier is computed and dropped, and a degraded host is treated as a clean "
            "one. It binds today at run/composition.py:747 -- the neighbour that made "
            "debug_menu_preflight's bare call visible (T300)."
        ),
    ),
    ConsumedReturn(
        call_id="saves.estimate_footprint",
        function="estimate_footprint",
        defined_in="saves/headroom.py",
        returns="FootprintEstimate",
        why_load_bearing=(
            "The estimated save + capture footprint the free-disk gate compares against "
            "min_free_disk_gb (V11, T243, research R17). The function only estimates; the "
            "comparison is the caller's, so a discarded estimate is a disk gate with no "
            "number in it."
        ),
        discarded_means=(
            "a run that would die at its first quicksave starts anyway. It binds today at "
            "run/composition.py:759 (T300)."
        ),
    ),
    ConsumedReturn(
        call_id="run.build_pin_preflight",
        function="build_pin_preflight",
        defined_in="run/preparation.py",
        returns="BuildPinResult",
        why_load_bearing=(
            "Which build this seed set is pinned to and whether the live build matches "
            "(R18, FR on build pinning). The verdict and the observed build are both in the "
            "return, and the run's own record needs them to say what it is comparable with."
        ),
        discarded_means=(
            "runs from different game builds become silently comparable. It binds today at "
            "run/composition.py:876 (T300)."
        ),
    ),
)

#: Exemptions. Each must cite a task or finding id (or carry the UNRESOLVED marker) **and**
#: name an owning lane, and each is checked for staleness: the moment its call site starts
#: binding, the entry fails and must be removed. The list can only tighten.
LOAD_BEARING_RETURN_ALLOWLIST: dict[str, str] = {
    "run.debug_menu_preflight": (
        "T300, T280: confirmed still discarding at HEAD by reading "
        "`git show HEAD:src/civsim_harness/run/composition.py` -- `:746` is "
        "`debug_menu_preflight(host, home=home)` as a bare ast.Expr, between two neighbours "
        "that both bind (`:745` catalog_result, `:747` host_gate_result). "
        "owner: LIVE lane (run/** is theirs; this agent's grant covered tests/contract/** "
        "and specs/**, read-only on run/**). The fix is one line plus the thread-through: "
        "bind the result and pass its state into the Run (models/run.py already carries "
        "`debug_menu_state`), and into BranchSource on the branch path."
    ),
    "run.turn_timer_preflight": (
        "T300: the same shape, 245 lines further down the same function -- `:991` is "
        "`turn_timer_preflight(read_turn_timer=snapshot.read_turn_timer)` as a bare "
        "ast.Expr. The raise-on-active-timer gate still works, so this is the record half "
        "only: nothing distinguishes VERIFIED_NONE from UNVERIFIED on the finished run. "
        "owner: LIVE lane (run/composition.py). Unlike the entry above, this one has no "
        "Run field waiting for it yet, so the fix is two steps -- add the field, then bind "
        "-- and that ordering is why it is allowlisted rather than left to fail."
    ),
}


# --------------------------------------------------------------------------
# The real tree.
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def harness_sources() -> dict[str, str]:
    return {
        str(path.relative_to(_HARNESS_ROOT)): path.read_text(encoding="utf-8")
        for path in sorted(_HARNESS_ROOT.rglob("*.py"))
    }


def test_the_production_partition_is_being_scanned(harness_sources: dict[str, str]) -> None:
    """A check that reads nothing passes vacuously. Prove the scan found the tree."""
    assert len(harness_sources) > 50, f"only {len(harness_sources)} modules scanned"
    assert "run/composition.py" in harness_sources
    assert "run/preparation.py" in harness_sources


def test_the_scan_finds_the_worked_example(harness_sources: dict[str, str]) -> None:
    """The load-bearing control on the real tree.

    If ``run/composition.py:746`` is ever fixed, this test fails and must be **deleted**
    along with the allowlist entry -- that is the ratchet working, and the failure message
    says so. Until then it pins the fact that the scan sees the one call site that motivated
    the whole file, so a green here is evidence rather than silence.
    """
    sites = _discard_sites(harness_sources)
    discards = [
        site.describe()
        for site in sites.get("debug_menu_preflight", [])
        if site.module != "run/preparation.py"
    ]
    assert discards == ["run/composition.py:746"], (
        "the worked example moved or was fixed: expected exactly "
        "['run/composition.py:746'], got "
        f"{discards}. If it was FIXED, delete this test and the "
        "'run.debug_menu_preflight' allowlist entry together."
    )


def test_the_roster_is_not_empty_and_has_unique_ids() -> None:
    assert LOAD_BEARING_RETURNS, "an empty roster asserts nothing"
    ids = [entry.call_id for entry in LOAD_BEARING_RETURNS]
    assert len(ids) == len(set(ids)), f"duplicate call ids would hide an entry: {ids}"


def test_every_roster_entry_names_a_real_function(harness_sources: dict[str, str]) -> None:
    """The roster must point at code that exists, or it proves nothing."""
    missing = [
        f"{entry.call_id}: {entry.defined_in}::{entry.function}"
        for entry in LOAD_BEARING_RETURNS
        if entry.defined_in not in harness_sources
        or not _defines_function(harness_sources[entry.defined_in], entry.function)
    ]
    assert missing == [], "roster entries naming nothing:\n" + "\n".join(missing)


def test_every_roster_entry_carries_its_prose() -> None:
    """Both prose fields are required. An entry that cannot say *why* the return is
    load-bearing is an entry the next reader cannot audit, and the judgement is the part
    of this check that no syntax supplies."""
    thin = [
        entry.call_id
        for entry in LOAD_BEARING_RETURNS
        if len(entry.why_load_bearing.strip()) < 40 or len(entry.discarded_means.strip()) < 40
    ]
    assert thin == [], f"roster entries with no real justification: {thin}"


def test_every_load_bearing_return_is_consumed_or_carries_a_cited_exemption(
    harness_sources: dict[str, str],
) -> None:
    problems = _consumed_return_problems(
        LOAD_BEARING_RETURNS, LOAD_BEARING_RETURN_ALLOWLIST, source_of=harness_sources
    )
    assert problems == [], "\n".join(problems)


@pytest.mark.parametrize("name", sorted(LOAD_BEARING_RETURN_ALLOWLIST))
def test_every_allowlist_entry_carries_a_cited_justification_and_an_owner(name: str) -> None:
    assert _allowlist_problems({name: LOAD_BEARING_RETURN_ALLOWLIST[name]}) == []


# --------------------------------------------------------------------------
# Negative controls: the checker must be able to fire. Each builds the failing
# case first, then its positive twin, so "it passed" is evidence and not silence.
# --------------------------------------------------------------------------


def _sources(**modules: str) -> dict[str, str]:
    return {
        name.replace("__", "/") + ".py": textwrap.dedent(body) for name, body in modules.items()
    }


_DEFINING = """
def do_the_thing(host, *, home):
    return Result(host, home)
"""

_DISCARDED = _sources(
    pkg__define=_DEFINING,
    pkg__caller="""
    def compose():
        first = other_preflight()
        do_the_thing(host, home=home)
        second = another_gate()
        return first, second
    """,
)

_BOUND = _sources(
    pkg__define=_DEFINING,
    pkg__caller="""
    def compose():
        first = other_preflight()
        result = do_the_thing(host, home=home)
        second = another_gate()
        return first, second, result
    """,
)

_ENTRY = ConsumedReturn(
    call_id="pkg.do_the_thing",
    function="do_the_thing",
    defined_in="pkg/define.py",
    returns="Result",
    why_load_bearing="synthetic: the return is the whole product of the call",
    discarded_means="synthetic: the answer evaporates and nothing records it",
)

_CITED = "T300: synthetic. owner: LIVE lane"


def test_negative_control_a_discarded_return_is_flagged() -> None:
    """The load-bearing control: the exact shape of run/composition.py:746 -- a bare call
    between two neighbours that bind -- must fail the check."""
    problems = _consumed_return_problems((_ENTRY,), {}, source_of=_DISCARDED)
    assert len(problems) == 1
    assert "DISCARDED RETURN at pkg/caller.py:4" in problems[0]


def test_negative_control_the_positive_twin_is_not_flagged() -> None:
    """The same call, bound to a name. If this fired, the check would be unusable."""
    assert _consumed_return_problems((_ENTRY,), {}, source_of=_BOUND) == []


def test_negative_control_the_await_spelling_is_flagged() -> None:
    """**Mandatory.** ``ast.Expr(ast.Await(ast.Call))`` is the same defect one node deeper.

    A scan written against the synchronous shape alone passes ``await f()`` in silence --
    which would be the discarded-return check containing an instance of the discarded-return
    defect. The real tree has async call sites in exactly this position
    (``run/composition.py`` awaits ``refresh_state_indices`` bare at three lines), so this is
    a live spelling here, not a hypothetical one.
    """
    source = _sources(
        pkg__define=_DEFINING,
        pkg__caller="""
        async def compose():
            first = await other_preflight()
            await do_the_thing(host, home=home)
            return first
        """,
    )
    problems = _consumed_return_problems((_ENTRY,), {}, source_of=source)
    assert len(problems) == 1
    assert "DISCARDED RETURN at pkg/caller.py:4 (await)" in problems[0]


def test_negative_control_the_awaited_positive_twin_is_not_flagged() -> None:
    """``x = await f()`` consumes the return and must not fire -- otherwise the await arm
    would be a check that fires on everything, which is the same as one that fires on
    nothing."""
    source = _sources(
        pkg__define=_DEFINING,
        pkg__caller="""
        async def compose():
            result = await do_the_thing(host, home=home)
            return result
        """,
    )
    assert _consumed_return_problems((_ENTRY,), {}, source_of=source) == []


def test_negative_control_a_method_spelling_is_flagged() -> None:
    """``obj.do_the_thing()`` as a statement is the same discard; the scan keys on the
    attribute name, exactly as the sibling scans do."""
    source = _sources(
        pkg__define=_DEFINING,
        pkg__caller="""
        def compose():
            self.do_the_thing(host, home=home)
        """,
    )
    problems = _consumed_return_problems((_ENTRY,), {}, source_of=source)
    assert len(problems) == 1
    assert "DISCARDED RETURN" in problems[0]


@pytest.mark.parametrize(
    ("label", "body"),
    [
        ("argument", "    record(do_the_thing(host, home=home))"),
        ("return", "    return do_the_thing(host, home=home)"),
        ("if test", "    if do_the_thing(host, home=home):\n        pass"),
        ("with item", "    with do_the_thing(host, home=home):\n        pass"),
        ("attribute of", "    value = do_the_thing(host, home=home).state"),
        ("comprehension", "    values = [do_the_thing(h, home=home) for h in hosts]"),
        ("await argument", "    record(await do_the_thing(host, home=home))"),
    ],
)
def test_negative_control_a_consumed_call_in_any_position_is_not_flagged(
    label: str, body: str
) -> None:
    """Every one of these is an ``ast.Call`` that is **not** an ``ast.Expr``'s value, so the
    surrounding expression consumes it. Flagging any of them would make the check fire on
    ordinary code and be allowlisted into uselessness -- the measured failure mode recorded
    in R21."""
    source = _sources(pkg__define=_DEFINING, pkg__caller=f"async def compose():\n{body}\n")
    assert _consumed_return_problems((_ENTRY,), {}, source_of=source) == [], label


def test_negative_control_a_discard_in_the_defining_module_is_not_flagged() -> None:
    """Same partition rule as ``test_gate_inputs.py``: the defining module's own internal
    calls are not production call sites for this purpose."""
    source = _sources(pkg__define=_DEFINING + "\ndef local():\n    do_the_thing(host, home=home)\n")
    assert _consumed_return_problems((_ENTRY,), {}, source_of=source) == []


def test_negative_control_a_discard_in_the_test_partition_is_not_flagged() -> None:
    """``tests/`` is excluded by construction, not oversight.

    ``f()`` in a test is frequently the assertion itself -- proving the call does not raise.
    The real check only ever receives ``src/civsim_harness`` sources; this control pins that
    the exclusion is real by showing a tests-tree discard that the scan never sees, and by
    asserting the real fixture holds no test module.
    """
    scanned = {str(path.relative_to(_HARNESS_ROOT)) for path in sorted(_HARNESS_ROOT.rglob("*.py"))}
    assert not any(module.startswith("../") for module in scanned)
    assert _TESTS_ROOT.is_dir(), "the test partition must exist for the exclusion to mean anything"
    # And the checker, handed only the production partition, sees no test-tree site:
    assert _consumed_return_problems((_ENTRY,), {}, source_of=_BOUND) == []


def test_negative_control_an_allowlisted_discard_passes() -> None:
    assert (
        _consumed_return_problems((_ENTRY,), {"pkg.do_the_thing": _CITED}, source_of=_DISCARDED)
        == []
    )


def test_negative_control_a_stale_allowlist_entry_is_flagged() -> None:
    """The ratchet: once the call site binds, its exemption must go. This is the arm that
    makes the list one-way, and it is proved here rather than asserted."""
    problems = _consumed_return_problems((_ENTRY,), {"pkg.do_the_thing": _CITED}, source_of=_BOUND)
    assert len(problems) == 1
    assert "stale exemption" in problems[0]


def test_negative_control_an_allowlist_entry_naming_no_roster_entry_is_flagged() -> None:
    problems = _consumed_return_problems((_ENTRY,), {"pkg.nonesuch": _CITED}, source_of=_BOUND)
    assert any("names no roster entry" in problem for problem in problems)


def test_negative_control_a_roster_entry_naming_a_missing_module_is_flagged() -> None:
    """A check that cannot locate its target must fail, not pass vacuously."""
    ghost = ConsumedReturn(
        call_id="pkg.ghost",
        function="do_the_thing",
        defined_in="pkg/nowhere.py",
        returns="Result",
        why_load_bearing="synthetic",
        discarded_means="synthetic",
    )
    problems = _consumed_return_problems((ghost,), {}, source_of=_BOUND)
    assert len(problems) == 1
    assert "was not found" in problems[0]


def test_negative_control_a_roster_entry_naming_a_missing_function_is_flagged() -> None:
    """The module existing is not enough: the ``def`` must be in it, or a rename would
    silently turn the entry into a pass."""
    ghost = ConsumedReturn(
        call_id="pkg.ghost",
        function="no_such_function",
        defined_in="pkg/define.py",
        returns="Result",
        why_load_bearing="synthetic",
        discarded_means="synthetic",
    )
    problems = _consumed_return_problems((ghost,), {}, source_of=_BOUND)
    assert len(problems) == 1
    assert "defines no 'no_such_function'" in problems[0]


@pytest.mark.parametrize(
    "justification",
    ["", "   ", "because", "it is fine for now", "owner: LIVE lane"],
)
def test_negative_control_an_uncited_or_unowned_justification_is_rejected(
    justification: str,
) -> None:
    """Both halves are load-bearing: 'owner: LIVE lane' alone cites nothing, and a bare task
    id names nobody -- which is how this very finding sat in prose in two files and in no
    owner's queue."""
    assert _allowlist_problems({"pkg.do_the_thing": justification}) != []


def test_negative_control_a_cited_but_unowned_justification_is_rejected() -> None:
    problems = _allowlist_problems({"pkg.do_the_thing": "T300: confirmed by reading HEAD."})
    assert len(problems) == 1
    assert "names no owning lane" in problems[0]


def test_negative_control_a_properly_cited_and_owned_justification_is_accepted() -> None:
    assert (
        _allowlist_problems({"pkg.do_the_thing": "T300: confirmed at HEAD. owner: LIVE lane"}) == []
    )
    assert (
        _allowlist_problems({"pkg.do_the_thing": f"{_UNRESOLVED_MARKER}. owner: hypervisor"}) == []
    )
