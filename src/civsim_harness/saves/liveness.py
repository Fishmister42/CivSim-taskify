"""Liveness for the reload path: recovery rung 4 and every bounded wait inside it (T301).

**One enumeration, not two fixes.** ``saves/load_game.py::_await_phase`` and
``resilience/recovery.py::recover`` were the last two silent entries on
``tests/contract/test_long_phase_liveness.py``'s roster, and they are not two defects: they
are one phase seen at two altitudes. ``recover`` *is* rung 4 of spec 004's recovery ladder,
``_await_phase`` is the wait rung 4 is built on, and the 300 s bound that made the roster
entry the worst row on the board is the same 300 s in both rows. So this module publishes
:data:`RELOAD_PATH` -- **every step the reload path can be sitting in while a watchdog is
counting**, with its bound, whether that bound can outlast the silence budget, and whether
the step emits -- and both phases emit through it. Fixing them separately would have left
the composite's total unstated, which is the number that actually matters:

    The reload path's own worst case is :data:`RELOAD_PATH_WORST_CASE_S`, summed from the
    table below rather than asserted in prose -- 2.8x the watchdog's whole budget, in a
    package that until this module existed contained **no logging of any kind** (confirmed
    two ways: ``grep -rn 'logger|log_event|logging' src/civsim_harness/saves/`` exits 1, and
    an import scan of every ``saves/*.py`` finds no logging import).

**The recursion hazard this closes, and it is why the silence was not survivable.** The
ladder's rungs are themselves long phases. A rung that does not emit is *detected as a stall
while it is recovering from one*: the classifier escalates against the rung's own action and
burns the attempt limit on a recovery that was working. Worse, rung 4's dominant wait is
``_await_phase``, so a watchdog polling the driver log would kill the harness mid-load --
and a killed reload looks exactly like the crash it was reloading to repair. The records
below let the classifier hold *"rung 4 of this run is in flight, on step X, for Y seconds"*
as a **recorded state** rather than reading it as silence.

**Why a ``RunEvent`` was not enough.** ``recover`` already wrote ``turn_abandoned``,
``resumed`` and the lifecycle transitions durably. Those reach the **match store**; a
watchdog polling a log file cannot see them, and -- decisively -- the store's own write path
is one of the things that can wedge, so a signal that requires it cannot testify about it.
``provider/chain.py`` was the first instance of this confusion; this was the second.

Derivation, in the vocabulary of ``specs/004-.../contracts/liveness-signal.md`` §1.3 -- a
**work-derived** signal is a byproduct of the work and may clear a silence bound; an
**observer-derived** one comes from something beside the work and may not:

=========================== ================ ==============================================
Record                      Derivation       What it establishes
=========================== ================ ==============================================
:data:`RELOAD_PHASE_POLLED` **work-derived** One poll of the client completed. It cannot be
                                             produced by a timer: the loop has to reach the
                                             socket and get an answer before there is a
                                             record to publish.
:data:`RELOAD_RUNG_STEP`    **work-derived** One step of the rung finished -- its durable
                                             writes landed, or the loader call returned.
:data:`RELOAD_PHASE_ENTERED` observer-derived The phase was entered. Nothing has completed.
:data:`RELOAD_RUNG_ENTERED`  observer-derived The rung was entered. Nothing has completed.
=========================== ================ ==============================================

This is deliberately *not* the shape of ``provider/liveness.py``, which registers itself
observer-derived because its emitter is a daemon thread ticking beside a blocking socket: a
ticker beside a hung socket ticks forever. Nothing here ticks beside anything. There is no
thread, no timer and no clock-driven emission -- every record but the two ``entered`` lines
is published by the loop body after a unit of work returned, so the record rate *is* the
work rate, and a stalled loop goes quiet on its own without anyone having to notice.

**The honest sub-registration on the poll record, which the name is built to carry.** During
the load the tuner port is closed by design, so most polls answer ``unreachable``. That
outcome is work-derived **about the poll loop** -- a refused connect is an answer from the
operating system, not a timer -- but it is *not* evidence that the load is progressing. The
signal is therefore named :data:`RELOAD_PHASE_POLLED`, for the poll, which is exactly what
it directly observes, and never for the load, which it does not (liveness-signal.md §4: a
signal must not be named for a condition it does not directly observe). A reader seeing a
long run of ``unreachable`` outcomes knows the harness is alive and polling and the client
is not answering; the honest reading of the *load* during that window is undetermined, and
``outcome`` is the field that makes the difference legible rather than assumed.

**Principle I.** These are harness telemetry. The payloads carry the *shape* of the wait --
which step, which poll, how long, against what bound, what the poll's outcome was -- and
never its subject: no ``Observation``, no state-table names, no turn number, no local
player, no save name and no save contents. ``state_count`` is a cardinality, the minimum
evidence that a real read completed, and a number is not a reading. Nothing here is returned
to a caller, attached to a ``RecoveryResult``, or reachable from the agent's context.
``tests/unit/test_phase_liveness_emission.py`` asserts the exact key set of every record
built here, so widening a payload turns red instead of leaking quietly.

**Failure is never the caller's problem.** :func:`log_event` swallows everything. A
telemetry failure must not be able to turn a working recovery into a failed one -- the
emission exists to describe the reload, so letting it break the reload inverts the point.

**Why one module for two packages, when ``act`` and ``provider`` were deliberately kept
apart.** Those two were separated because their Principle I exclusions *differ* -- one must
exclude prompt and response content, the other game state -- and a shared helper would put
both exclusions one refactor away from each other. These two share one exclusion, one
enumeration and one bound; separating them is what hid the composite's total. It lives under
``saves/`` because ``resilience`` already imports ``saves`` (``saves.addressing``) and the
reverse direction would cycle.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from logging import INFO
from typing import Any, Final

from civsim_harness.telemetry.logging import get_harness_logger
from civsim_harness.telemetry.logging import log_event as _log_harness_event

#: The watchdog's silence budget, in seconds -- the number the table below is read against.
#: Duplicated from ``tests/contract/test_long_phase_liveness.py`` rather than imported: src
#: must not import from tests, and a bound that src cannot state for itself is a bound src
#: cannot be checked against.
WATCHDOG_SILENCE_BUDGET_S: Final[float] = 180.0

#: Which rung of spec 004's recovery ladder ``resilience/recovery.py::RecoveryEngine`` is.
#: Carried on every rung record so a reader does not have to know the mapping.
RECOVERY_RUNG: Final[int] = 4

# -- the record kinds -------------------------------------------------------------------------
#: Fixed strings, so a log consumer matches on a constant rather than on prose.

#: Observer-derived. The phase was entered; nothing has completed yet.
RELOAD_PHASE_ENTERED: Final[str] = "reload.phase.entered"
#: **Work-derived.** One poll of the client completed. See the module docstring for the
#: sub-registration on the ``unreachable`` outcome.
RELOAD_PHASE_POLLED: Final[str] = "reload.phase.polled"
#: Observer-derived. The rung was entered; nothing has completed yet.
RELOAD_RUNG_ENTERED: Final[str] = "reload.rung.entered"
#: **Work-derived.** One step of the rung finished.
RELOAD_RUNG_STEP: Final[str] = "reload.rung.step"

# -- the steps --------------------------------------------------------------------------------

STEP_ABANDON_ATTEMPT: Final[str] = "rung.abandon_attempt"
STEP_LOCATE_PHASE: Final[str] = "loader.locate_phase"
STEP_EXIT_TO_MENU: Final[str] = "loader.exit_to_menu"
STEP_LOAD_GAME: Final[str] = "loader.load_game"
STEP_VERIFY_FAR_SIDE: Final[str] = "loader.verify_far_side"
STEP_LOAD_SAVE: Final[str] = "rung.load_save"
STEP_RESUME: Final[str] = "rung.resume"

# -- poll outcomes ----------------------------------------------------------------------------

#: The poll reached the client and read its state table; the predicate does not hold yet.
OUTCOME_PENDING: Final[str] = "pending"
#: The poll reached the client and the predicate holds -- this record closes the wait.
OUTCOME_SATISFIED: Final[str] = "satisfied"
#: The poll completed against a client that would not answer (refused, dropped, unresolved).
#: Work-derived about the loop, not about the load -- see the module docstring.
OUTCOME_UNREACHABLE: Final[str] = "unreachable"

# -- rung-step outcomes -----------------------------------------------------------------------

OUTCOME_RECORDED: Final[str] = "recorded"
OUTCOME_LOADED: Final[str] = "loaded"
OUTCOME_RESUMED: Final[str] = "resumed"
#: The save was *already* recorded absent on entry, so the load was never attempted.
OUTCOME_SAVE_ABSENT: Final[str] = "save_absent"
#: The loader discovered the file gone only now (``FileNotFoundError``).
OUTCOME_SAVE_MISSING: Final[str] = "save_missing"
OUTCOME_FAILED: Final[str] = "failed"


@dataclass(frozen=True)
class ReloadStep:
    """One step the reload path can be sitting in while a watchdog is counting.

    ``bound_s`` is the *shipped default* bound, not the live one -- every bound on this path
    is injectable at construction, and a table that claimed to know the live value would be
    asserting something it cannot see. It is here so the enumeration can be read against
    :data:`WATCHDOG_SILENCE_BUDGET_S` without opening four files; the live bound travels on
    each record as ``timeout_s``. ``None`` means the step imposes no wait of its own.

    ``emits`` is a claim this module's tests check both ways: a step declared to emit whose
    id appears at no call site fails, and a call site passing an id this table does not carry
    fails. A step declared **not** to emit is a *declared* exclusion, which is the only kind
    this project accepts -- an undeclared exclusion is the same defect as an undeclared
    silent phase.
    """

    step: str
    bound_s: float | None
    can_outlast_budget: bool
    emits: bool
    waits_on: str


#: **The enumeration.** In the order the path walks them. The two ``rung.*`` bookends are the
#: composite's own steps; the four ``loader.*`` entries are what one call to
#: ``LuaSaveLoader.load`` can stack, and their sum is what makes the composite long.
RELOAD_PATH: tuple[ReloadStep, ...] = (
    ReloadStep(
        step=STEP_ABANDON_ATTEMPT,
        bound_s=None,
        can_outlast_budget=False,
        emits=True,
        waits_on="the run's lifecycle drive to interrupted/resuming -- store writes, no wait "
        "of its own, but it is the step that is in flight if the store is the thing wedged",
    ),
    ReloadStep(
        step=STEP_LOCATE_PHASE,
        bound_s=90.0,
        can_outlast_budget=False,
        emits=True,
        waits_on="a reachable tuner reporting a recognisable phase (InGame, or the front end)",
    ),
    ReloadStep(
        step=STEP_EXIT_TO_MENU,
        bound_s=90.0,
        can_outlast_budget=False,
        emits=True,
        waits_on="the front end after Events.ExitToMainMenu(); skipped when already there",
    ),
    ReloadStep(
        step=STEP_LOAD_GAME,
        bound_s=300.0,
        can_outlast_budget=True,
        emits=True,
        waits_on="the game states to reappear after Network.LoadGame -- the longest explicit "
        "bound in src/, 1.67x the whole budget on its own, and the tuner port is closed by "
        "design for most of it",
    ),
    ReloadStep(
        step=STEP_VERIFY_FAR_SIDE,
        bound_s=30.0,
        can_outlast_budget=False,
        emits=False,
        waits_on="the far-side position read-back. DECLARED SILENT: 30 s, well under the "
        "budget, its own loop rather than _await_phase's, and it is bracketed on both sides "
        "by instrumented steps -- so a stall inside it is bounded by two records 30 s apart. "
        "If that bound is ever raised past the budget this entry must be instrumented, not "
        "re-justified",
    ),
    ReloadStep(
        step=STEP_LOAD_SAVE,
        bound_s=None,
        can_outlast_budget=True,
        emits=True,
        waits_on="the whole SaveLoader.load call: every loader.* step above, back to back",
    ),
    ReloadStep(
        step=STEP_RESUME,
        bound_s=None,
        can_outlast_budget=False,
        emits=True,
        waits_on="the resumed/playing records and the run update -- store writes, no wait",
    ),
)

#: The reload path's worst case, **summed from the table** rather than asserted: 510 s, or
#: 2.8x :data:`WATCHDOG_SILENCE_BUDGET_S`. Stating this number is the whole reason the two
#: phases were enumerated together instead of patched apart.
RELOAD_PATH_WORST_CASE_S: Final[float] = float(
    sum(step.bound_s for step in RELOAD_PATH if step.bound_s is not None)
)


def log_event(event: str, payload: Mapping[str, Any]) -> None:
    """Publish one harness-telemetry record, swallowing anything it raises.

    ``telemetry.logging.log_event`` on the ``civsim_harness`` logger, so the record passes
    the redacting filter and formatter that module forces onto every sanctioned handler and
    lands on the stderr handler ``operator/cli.py`` configures -- which is what the driver
    log a watchdog polls actually captures. It is **not** written to the match store: a tick
    per second for the length of a load is write amplification against a store with a
    no-delete floor, and a signal that needs the store healthy cannot testify about it.

    Named ``log_event`` at the call site on purpose. ``tests/contract/test_long_phase_
    liveness.py``'s roster names ``log_event`` as what counts as emitting for both of these
    phases, and the alternative -- widening the roster's ``emits_via`` to admit a new name --
    is the same "widen the check until the code fits" move that whole file exists to refuse.
    Reached only as ``reload_liveness.log_event(...)``, so no call site can confuse it with
    ``telemetry.logging.log_event``'s different (logger, level, message) shape.
    """
    try:
        _log_harness_event(get_harness_logger(), INFO, event, extra=dict(payload))
    except Exception:  # noqa: BLE001 -- telemetry must never fail the reload it describes
        pass


def phase_entered_record(*, step: str, timeout_s: float, poll_interval_s: float) -> dict[str, Any]:
    """Observer-derived: the wait was entered, against *timeout_s*, polling every
    *poll_interval_s*. Published before the first poll, so a wait whose very first round trip
    hangs still leaves evidence that it was reached -- and so a reader knows which bound the
    records that follow are counting against. It establishes nothing about progress and may
    not clear a silence bound."""
    return {"step": step, "timeout_s": timeout_s, "poll_interval_s": poll_interval_s}


def phase_poll_record(
    *,
    step: str,
    poll: int,
    elapsed_s: float,
    timeout_s: float,
    poll_interval_s: float,
    outcome: str,
    state_count: int | None,
) -> dict[str, Any]:
    """**Work-derived**: one poll of the client completed and this is what it found.

    ``poll`` counts from 1 and increments only when a round trip finished, so the sequence
    itself is the progress evidence -- a gap in it is a loop that stopped executing, which no
    timer-driven tick can tell you. ``state_count`` is the size of the state table the poll
    read (``None`` when the client would not answer): the minimum proof a read happened,
    carrying no state *name*. ``elapsed_s`` and ``timeout_s`` together answer "how far into
    its legitimate bound is this", which is the question that separates a slow reload from a
    wedged one.
    """
    return {
        "step": step,
        "poll": poll,
        "elapsed_s": round(elapsed_s, 3),
        "timeout_s": timeout_s,
        "poll_interval_s": poll_interval_s,
        "outcome": outcome,
        "state_count": state_count,
    }


def rung_entered_record(
    *, run_id: str, rung: int, attempt: int, attempt_limit: int, trigger: str
) -> dict[str, Any]:
    """Observer-derived: the rung was entered. Nothing has completed, so it may not clear a
    bound -- but it is what lets the classifier hold "a rung of this run is in flight" as a
    recorded state instead of reading the reload's duration as silence. ``attempt`` against
    ``attempt_limit`` says how much ladder is left; ``trigger`` says which detection asked
    for it."""
    return {
        "run_id": run_id,
        "rung": rung,
        "attempt": attempt,
        "attempt_limit": attempt_limit,
        "trigger": trigger,
    }


def rung_step_record(
    *, run_id: str, rung: int, step: str, attempt: int, elapsed_s: float, outcome: str
) -> dict[str, Any]:
    """**Work-derived**: one step of the rung finished, and *outcome* is how. Published after
    the step's durable writes landed or the loader call returned -- never before, so the
    record cannot exist unless the work did. Together with the ``entered`` record these
    bracket each step, and the loader's own poll records fill the interior of the long one."""
    return {
        "run_id": run_id,
        "rung": rung,
        "step": step,
        "attempt": attempt,
        "elapsed_s": round(elapsed_s, 3),
        "outcome": outcome,
    }


__all__ = [
    "OUTCOME_FAILED",
    "OUTCOME_LOADED",
    "OUTCOME_PENDING",
    "OUTCOME_RECORDED",
    "OUTCOME_RESUMED",
    "OUTCOME_SATISFIED",
    "OUTCOME_SAVE_ABSENT",
    "OUTCOME_SAVE_MISSING",
    "OUTCOME_UNREACHABLE",
    "RECOVERY_RUNG",
    "RELOAD_PATH",
    "RELOAD_PATH_WORST_CASE_S",
    "RELOAD_PHASE_ENTERED",
    "RELOAD_PHASE_POLLED",
    "RELOAD_RUNG_ENTERED",
    "RELOAD_RUNG_STEP",
    "STEP_ABANDON_ATTEMPT",
    "STEP_EXIT_TO_MENU",
    "STEP_LOAD_GAME",
    "STEP_LOAD_SAVE",
    "STEP_LOCATE_PHASE",
    "STEP_RESUME",
    "STEP_VERIFY_FAR_SIDE",
    "WATCHDOG_SILENCE_BUDGET_S",
    "ReloadStep",
    "log_event",
    "phase_entered_record",
    "phase_poll_record",
    "rung_entered_record",
    "rung_step_record",
]
