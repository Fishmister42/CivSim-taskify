"""Long phases: anything that can outlast the watchdog's silence budget must emit (T290).

Third sibling to ``tests/contract/test_reachability.py`` and
``tests/contract/test_gate_inputs.py`` -- same idiom (AST scan, roster and allowlist as
data, per-entry justifications that must cite a task or finding id, negative controls that
prove the checker can fire, a ratchet so the list can only tighten), aimed at a defect
family neither of those sees.

**The criterion this file exists to serve.** The owner's watchdog kills a harness game run
after **three minutes with no affirmative liveness signal**. Absence of a heartbeat counts
as stuck; absence of *completion* does not. That is a good rule, and it has one failure
mode: a phase that is legitimately slow *and* silent is killed while working perfectly, and
the obvious "fix" is to raise the watchdog's ceiling until the silence fits.

    A phase that is silent by construction is a defect in the phase, never evidence about
    the run -- and widening a ceiling until the silence fits is how a threshold gets chosen
    without measuring what it bounds.

So the rule this file enforces is **make the phase emit**, and the thing it makes
falsifiable is the enumeration itself: the set of phases that can outlast three minutes is
published here as data, and every member must be shown to emit or be allowlisted to a named
owning lane with a cited justification.

**Why the roster is hand-curated and not derived.** The obvious generalisation -- "flag
every loop containing a sleep" -- was measured against this tree before this file was
written and is not the signal. ``src/civsim_harness`` holds ten-odd sleeping loops, and the
ones that matter are distinguished by their *bound*, not their shape:
``nexus/client.py``'s reconnect backoff tops out at 2 s, ``saves/verify.py``'s appearance
poll at 10 s, ``resilience/detector.py``'s re-probe at 2 s. None of those can approach 180 s,
so flagging them would produce a list dominated by entries nobody should act on -- which
gets allowlisted down to nothing and becomes a check that cannot fail in the way that
matters. Whether a bound *can* exceed the budget is a judgement about the numbers and about
what the phase actually waits on, which is exactly the part no syntax scan supplies.

**What counts as "emits".** A call, anywhere in the named function's body, to one of the
names the roster entry lists in ``emits_via``. The emission must reach the **driver log**
(``telemetry/logging.py`` -> the redacting stderr handler ``operator/cli.py`` configures),
because that is what a watchdog polls. A ``RunEvent`` written to the match store is *not*
an affirmative liveness signal for this purpose: it is durable evidence for the ledger, and
a watchdog reading a log file cannot see it. ``provider/chain.py`` is the worked example --
it recorded ``provider_retry`` events for a long time while publishing nothing a watchdog
could read.

**What counts as "can outlast three minutes".** Either an explicit bound above 180 s, or a
wait with no bound at all, or a composite whose leaf waits sum past it.

**Phases considered and deliberately excluded.** An undeclared exclusion is the same defect
as an undeclared silent phase, so the reasons are recorded here rather than left implicit:

* ``nexus/client.py`` -- ``DEFAULT_COMMAND_TIMEOUT_S`` 30 s, ``DEFAULT_CONNECT_TIMEOUT_S``
  5 s, reconnect backoff capped at ``DEFAULT_RECONNECT_MAX_BACKOFF_S`` 2 s. No path here
  reaches 180 s.
* ``saves/verify.py`` -- ``DEFAULT_APPEARANCE_TIMEOUT_S`` 10 s, ``DEFAULT_STABILITY_WAIT_S``
  0.5 s. Silent, and that is fine: it cannot approach the budget.
* ``resilience/detector.py`` -- ``DEFAULT_RECONNECT_REPROBE_DELAY_S`` 2 s, one re-probe, no
  loop. The module docstring is explicit that it never loops or imposes its own wait.
* ``resilience/operation_bounds.py`` -- bounds are 15-30 s per operation and the module
  keeps no running total across calls, by design.
* ``saves/load_game.py::_exit_to_menu`` -- ``DEFAULT_EXIT_TO_MENU_TIMEOUT_S`` 90 s, and
  ``_verify_far_side`` -- ``DEFAULT_VERIFY_TIMEOUT_S`` 30 s. Both under the budget on their
  own; both sit inside ``load`` which is on the roster, so a load that stacks them is
  covered by that entry.
* ``store/sqlite_adapter.py`` writes over large turn cycles -- scanned for a bound, a poll
  or a sleep and has none of the three: it is a synchronous SQLite transaction with no wait
  in it. It is excluded because there is **no measurement showing it slow**, not because one
  shows it fast; if a turn-cycle write is ever measured past the budget it belongs on the
  roster, not in this list.
* ``provider/stochastic.py`` -- the simulated provider measures its own latency with
  ``perf_counter`` but never sleeps, and is not on the live path.

**Honest limits.** Matching is by *name*, not resolved type, so a same-named call on an
unrelated object would satisfy an entry -- the same residual the reachability scan
documents, erring only toward a false PASS. The roster asserts that an emission *call site
exists in the function*, not that it fires on every iteration; the per-attempt behaviour of
``confirm_execution`` and the tick cadence of ``provider_call_liveness`` are asserted
behaviourally in ``tests/unit/test_phase_liveness_emission.py``, which is the right place
for it. And this file says nothing about whether a bound is the *right* bound: that is a
measurement question, and the whole point of the rule above is that it must never be
answered by widening.
"""

from __future__ import annotations

import ast
import re
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HARNESS_ROOT = _REPO_ROOT / "src" / "civsim_harness"

#: The watchdog's silence budget, in seconds. Present as a named constant so the roster's
#: own numbers can be read against it, and so that raising it is a visible, reviewable edit
#: to this file rather than a quiet argument in a comment somewhere.
WATCHDOG_SILENCE_BUDGET_S: float = 180.0


# --------------------------------------------------------------------------
# The checker. One implementation, driven by both the real tree and the
# synthetic trees the negative controls build.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LongPhase:
    """One phase that can outlast :data:`WATCHDOG_SILENCE_BUDGET_S`.

    ``module`` is a path relative to ``src/civsim_harness``. ``function`` is the plain
    function name as it appears in the source (methods are named by their own ``def``, not
    by ``Class.method``, because that is what the AST carries). ``emits_via`` is the set of
    call names that count as publishing a liveness record from inside that function --
    empty means the phase is claimed silent, which is what the allowlist is for.
    """

    phase_id: str
    module: str
    function: str
    bound: str
    why_long: str
    emits_via: tuple[str, ...]


def _function_node(tree: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            return node
    return None


def _called_names(node: ast.AST) -> set[str]:
    """Every call name reachable inside *node*, as both ``f`` and ``obj.f`` spellings.

    ``with provider_call_liveness(...)`` is an ``ast.Call`` inside an ``ast.With`` item, so
    a plain walk finds context-manager emissions as well as bare calls -- which matters,
    since the provider fix is a context manager and the act fix is a bare call.
    """
    names: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


def _phase_problems(
    phases: tuple[LongPhase, ...],
    allowlist: dict[str, str],
    *,
    source_of: dict[str, str],
) -> list[str]:
    """Four failure shapes, so the allowlist can only ratchet down.

    A silent phase that is not allowlisted fails; an allowlisted phase that has since
    started emitting fails as **stale** (its exemption must go); an allowlist entry naming
    no roster member fails; and a roster entry whose module or function cannot be found
    fails rather than silently passing, because a check that cannot locate its target is a
    check that cannot fail.
    """
    problems: list[str] = []
    by_id = {phase.phase_id: phase for phase in phases}

    for name in sorted(set(allowlist) - set(by_id)):
        problems.append(
            f"{name!r}: allowlisted but names no roster phase -- the ratchet requires every "
            f"exemption to point at a live entry"
        )

    for phase in phases:
        source = source_of.get(phase.module)
        if source is None:
            problems.append(f"{phase.phase_id!r}: module {phase.module!r} was not found")
            continue
        node = _function_node(ast.parse(source), phase.function)
        if node is None:
            problems.append(
                f"{phase.phase_id!r}: {phase.module}::{phase.function} was not found -- the "
                f"roster entry names nothing, so it cannot prove anything"
            )
            continue

        emits = bool(_called_names(node) & set(phase.emits_via))
        allowlisted = phase.phase_id in allowlist

        if not emits and not allowlisted:
            problems.append(
                f"{phase.phase_id!r}: {phase.module}::{phase.function} can run for "
                f"{phase.bound} with no bound under {WATCHDOG_SILENCE_BUDGET_S:g}s, and calls "
                f"none of {sorted(phase.emits_via)} -- a phase that is silent by construction "
                f"is a defect in the phase. Make it emit, or allowlist it with a justification "
                f"citing a task or finding id and naming the owning lane."
            )
        if emits and allowlisted:
            found = sorted(_called_names(node) & set(phase.emits_via))
            problems.append(
                f"{phase.phase_id!r}: now emits via {found} but is still allowlisted -- "
                f"stale exemption, remove it (the ratchet)"
            )

    return problems


_UNRESOLVED_MARKER = "UNRESOLVED - hypervisor review"
_TASK_CITATION = re.compile(r"\bT\d{3}\b")
_FINDING_CITATION = re.compile(r"\bC\d+[a-z]?\b")
#: An exemption must say **whose** it is. A silent phase with no owner named is how a defect
#: gets recorded and then belongs to nobody -- the brief's own rule, made falsifiable.
_OWNING_LANE = re.compile(r"\bowner:\s*\S", re.IGNORECASE)


def _allowlist_problems(allowlist: dict[str, str]) -> list[str]:
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
                f"'owner: <lane>' so a silent phase belongs to somebody: {justification!r}"
            )
    return problems


# --------------------------------------------------------------------------
# The roster. Data, deliberately hand-curated -- see the module docstring.
# --------------------------------------------------------------------------

LONG_PHASES: tuple[LongPhase, ...] = (
    LongPhase(
        phase_id="provider.in_flight_call",
        module="provider/openrouter.py",
        function="complete",
        bound="request_timeout_s, default 120 s -- but httpx applies it per socket "
        "operation rather than to the whole call, and the wall clock was MEASURED at "
        "146 s on the live driver logs of 2026-09-22",
        why_long="A blocking HTTP POST to a reasoning model with no progress callback. "
        "This was the worst case in the system: the entire provider package contained no "
        "logger, no logging import and no telemetry call of any kind (T290).",
        emits_via=("provider_call_liveness",),
    ),
    LongPhase(
        phase_id="provider.chain",
        module="provider/chain.py",
        function="complete_step",
        bound="unbounded in aggregate -- max_attempts_per_model (3) x len(models) x the "
        "per-request bound, plus backoff capped at max_delay_s + jitter",
        why_long="A composite: several in-flight calls back to back. Its leaf wait is "
        "provider.in_flight_call, which ticks; the chain's own backoff sleeps used to "
        "publish only RunEvents, which reach the store and not the driver log a watchdog "
        "polls (T290).",
        emits_via=("emit_provider_liveness",),
    ),
    LongPhase(
        phase_id="act.confirm_execution",
        module="act/verify.py",
        function="confirm_execution",
        bound="timeout_s, 200 s at both production call sites "
        "(run/decision_loop.py::END_TURN_CONFIRM_TIMEOUT_S and "
        "run/turn_cycle.py::BACKSTOP_CONFIRM_TIMEOUT_S)",
        why_long="The single longest legitimate operation in the system: an end turn is "
        "confirmed only once every AI player has taken theirs. It polled the tuner every "
        "2 s and emitted nothing, so it was the first healthy thing the watchdog would "
        "kill -- during exactly the slow turn its raised bound exists to accommodate (T290).",
        emits_via=("emit_confirm_liveness",),
    ),
    LongPhase(
        phase_id="run.backstop_end_turn_confirm",
        module="run/turn_cycle.py",
        function="_dispatch_backstop_end_turn",
        bound="BACKSTOP_CONFIRM_TIMEOUT_S = 200 s, polled every BACKSTOP_CONFIRM_POLL_S = 2 s",
        why_long="A second, hand-rolled 200 s confirm loop that does not go through "
        "act/verify.py::confirm_execution, so the act/liveness.py fix does not reach it. "
        "Same shape, same silence (T290).",
        emits_via=("emit_confirm_liveness", "log_event"),
    ),
    LongPhase(
        phase_id="saves.load_await_phase",
        module="saves/load_game.py",
        function="_await_phase",
        bound="DEFAULT_LOAD_TIMEOUT_S = 300 s, polled every DEFAULT_POLL_INTERVAL_S = 1 s",
        why_long="The longest explicit bound anywhere in src/: waiting for the game's Lua "
        "state table to reappear on the far side of a load. 300 s is 1.67x the watchdog's "
        "whole budget, and the entire saves/ package contains no logging at all -- "
        "confirmed two ways, by grep for logger/logging across saves/*.py and by an import "
        "scan of the same files (T290).",
        emits_via=("log_event",),
    ),
    LongPhase(
        phase_id="resilience.recover",
        module="resilience/recovery.py",
        function="recover",
        bound="unbounded -- dominated by the save load it performs, i.e. up to "
        "DEFAULT_LOAD_TIMEOUT_S = 300 s per attempt, up to recovery_attempt_limit attempts",
        why_long="A composite whose leaf wait is saves.load_await_phase. It records "
        "RunEvents to the store as it goes, which is durable evidence but not something a "
        "watchdog polling the driver log can read (T290).",
        emits_via=("log_event",),
    ),
)

#: Exemptions. Each must cite a task or finding id (or carry the UNRESOLVED marker) **and**
#: name an owning lane, and each is checked for staleness: the moment its phase starts
#: emitting, the entry fails and must be removed. The list can only tighten.
LONG_PHASE_ALLOWLIST: dict[str, str] = {
    "run.backstop_end_turn_confirm": (
        "T290: confirmed silent by reading run/turn_cycle.py:409-422 -- a hand-rolled "
        "deadline/poll loop with no emission, not routed through confirm_execution, so the "
        "act/liveness.py fix does not reach it. owner: LIVE lane (run/** is theirs; this "
        "agent's grant covered provider/** and act/verify.py only). The fix is two lines: "
        "import emit_harness_liveness from act/liveness.py and call it once per iteration "
        "with the deadline and elapsed."
    ),
    "saves.load_await_phase": (
        "T290: confirmed silent by reading saves/load_game.py:486-547 and by two "
        "independent negative searches over saves/*.py (grep for logger|log_event|logging, "
        "and an import scan) -- the package has no logging at all. owner: UNRESOLVED - "
        "hypervisor review; saves/** was granted to no lane in this agent's brief. This is "
        "the highest-value remaining entry: 300 s is the longest bound in src/ and is 1.67x "
        "the watchdog budget on its own."
    ),
    "resilience.recover": (
        "T290: confirmed by reading resilience/recovery.py:182-285 -- it emits RunEvents to "
        "the match store but nothing to the driver log, so a log-polling watchdog sees "
        "nothing for the duration of the load it drives. owner: UNRESOLVED - hypervisor "
        "review; resilience/** was granted to no lane in this agent's brief. Lower priority "
        "than saves.load_await_phase because fixing that one makes this composite readable "
        "for its longest leg."
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


def test_the_harness_tree_is_being_scanned(harness_sources: dict[str, str]) -> None:
    """A check that reads nothing passes vacuously. Prove the scan found the tree."""
    assert len(harness_sources) > 50, f"only {len(harness_sources)} modules scanned"
    assert "provider/openrouter.py" in harness_sources
    assert "act/verify.py" in harness_sources


def test_the_roster_is_not_empty_and_has_unique_ids() -> None:
    assert LONG_PHASES, "an empty roster asserts nothing"
    ids = [phase.phase_id for phase in LONG_PHASES]
    assert len(ids) == len(set(ids)), f"duplicate phase ids would hide an entry: {ids}"


def test_every_roster_entry_names_a_real_function(harness_sources: dict[str, str]) -> None:
    """The roster must point at code that exists, or it proves nothing."""
    missing = [
        f"{phase.phase_id}: {phase.module}::{phase.function}"
        for phase in LONG_PHASES
        if phase.module not in harness_sources
        or _function_node(ast.parse(harness_sources[phase.module]), phase.function) is None
    ]
    assert missing == [], "roster entries naming nothing:\n" + "\n".join(missing)


def test_every_long_phase_emits_or_carries_a_cited_exemption(
    harness_sources: dict[str, str],
) -> None:
    problems = _phase_problems(LONG_PHASES, LONG_PHASE_ALLOWLIST, source_of=harness_sources)
    assert problems == [], "\n".join(problems)


@pytest.mark.parametrize("name", sorted(LONG_PHASE_ALLOWLIST))
def test_every_allowlist_entry_carries_a_cited_justification_and_an_owner(name: str) -> None:
    assert _allowlist_problems({name: LONG_PHASE_ALLOWLIST[name]}) == []


# --------------------------------------------------------------------------
# Negative controls: the checker must be able to fire. Each builds the failing
# case first, then the passing one, so "it passed" is evidence and not silence.
# --------------------------------------------------------------------------


def _sources(**modules: str) -> dict[str, str]:
    return {
        name.replace("__", "/") + ".py": textwrap.dedent(body) for name, body in modules.items()
    }


_SILENT = _sources(
    slowpkg__slow="""
    def wait_a_long_time():
        deadline = now() + 300.0
        while True:
            if check():
                return
            if now() >= deadline:
                raise TimeoutError()
            sleep(1.0)
    """
)

_LOUD = _sources(
    slowpkg__slow="""
    def wait_a_long_time():
        deadline = now() + 300.0
        while True:
            emit_liveness("waiting", {"elapsed": now()})
            if check():
                return
            if now() >= deadline:
                raise TimeoutError()
            sleep(1.0)
    """
)

_PHASE = LongPhase(
    phase_id="slowpkg.wait",
    module="slowpkg/slow.py",
    function="wait_a_long_time",
    bound="300 s",
    why_long="synthetic",
    emits_via=("emit_liveness",),
)


def test_negative_control_a_silent_long_phase_is_flagged() -> None:
    """The load-bearing control: the exact shape of saves/load_game.py::_await_phase --
    a 300 s deadline, a 1 s poll, and nothing emitted -- must fail the check."""
    problems = _phase_problems((_PHASE,), {}, source_of=_SILENT)
    assert len(problems) == 1
    assert "silent by construction" in problems[0]


def test_negative_control_an_emitting_long_phase_passes() -> None:
    assert _phase_problems((_PHASE,), {}, source_of=_LOUD) == []


def test_negative_control_a_context_manager_emission_counts() -> None:
    """The provider fix is a ``with`` block, not a bare call -- the scan must see it, or it
    would flag the one phase it was written to fix."""
    source = _sources(
        slowpkg__slow="""
        def wait_a_long_time():
            with emit_liveness(provider="p", model="m"):
                return do_the_slow_thing()
        """
    )
    assert _phase_problems((_PHASE,), {}, source_of=source) == []


def test_negative_control_a_method_call_emission_counts() -> None:
    """``self._emit(...)`` must satisfy an entry naming ``_emit``, since a phase may own its
    emitter rather than importing a free function."""
    source = _sources(
        slowpkg__slow="""
        def wait_a_long_time():
            while True:
                self.emit_liveness("waiting")
                sleep(1.0)
        """
    )
    assert _phase_problems((_PHASE,), {}, source_of=source) == []


def test_negative_control_an_unrelated_call_does_not_count_as_emitting() -> None:
    """A phase that calls *something* is not a phase that emits."""
    source = _sources(
        slowpkg__slow="""
        def wait_a_long_time():
            while True:
                self._record_event("provider_retry")
                sleep(1.0)
        """
    )
    problems = _phase_problems((_PHASE,), {}, source_of=source)
    assert len(problems) == 1
    assert "silent by construction" in problems[0]


def test_negative_control_an_allowlisted_silent_phase_passes() -> None:
    assert (
        _phase_problems((_PHASE,), {"slowpkg.wait": "T290 owner: LIVE lane"}, source_of=_SILENT)
        == []
    )


def test_negative_control_a_stale_allowlist_entry_is_flagged() -> None:
    """The ratchet: once a phase starts emitting, its exemption must go."""
    problems = _phase_problems(
        (_PHASE,), {"slowpkg.wait": "T290 owner: LIVE lane"}, source_of=_LOUD
    )
    assert len(problems) == 1
    assert "stale exemption" in problems[0]


def test_negative_control_an_allowlist_entry_naming_no_phase_is_flagged() -> None:
    problems = _phase_problems(
        (_PHASE,), {"slowpkg.nonesuch": "T290 owner: LIVE lane"}, source_of=_LOUD
    )
    assert any("names no roster phase" in problem for problem in problems)


def test_negative_control_a_roster_entry_naming_nothing_is_flagged() -> None:
    """A check that cannot locate its target must fail, not pass vacuously."""
    ghost = LongPhase(
        phase_id="slowpkg.ghost",
        module="slowpkg/slow.py",
        function="no_such_function",
        bound="300 s",
        why_long="synthetic",
        emits_via=("emit_liveness",),
    )
    problems = _phase_problems((ghost,), {}, source_of=_LOUD)
    assert len(problems) == 1
    assert "was not found" in problems[0]


@pytest.mark.parametrize(
    "justification",
    ["", "   ", "because", "it is fine for now", "owner: LIVE lane"],
)
def test_negative_control_an_uncited_or_unowned_justification_is_rejected(
    justification: str,
) -> None:
    """Both halves are load-bearing: 'owner: LIVE lane' alone cites nothing, and a bare
    task id names nobody."""
    assert _allowlist_problems({"slowpkg.wait": justification}) != []


def test_negative_control_a_cited_but_unowned_justification_is_rejected() -> None:
    problems = _allowlist_problems({"slowpkg.wait": "T290: confirmed silent by reading."})
    assert len(problems) == 1
    assert "names no owning lane" in problems[0]


def test_negative_control_a_properly_cited_and_owned_justification_is_accepted() -> None:
    assert (
        _allowlist_problems({"slowpkg.wait": "T290: confirmed silent by reading. owner: LIVE lane"})
        == []
    )
    assert _allowlist_problems({"slowpkg.wait": f"{_UNRESOLVED_MARKER}. owner: hypervisor"}) == []
