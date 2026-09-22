"""The long phases fixed in T302 and T301 actually emit, per attempt and mid-flight.

``tests/contract/test_long_phase_liveness.py`` asserts that an emission *call site exists*
inside each rostered phase -- a structural check, so it can enumerate the whole set and
ratchet. It cannot tell a heartbeat from a single "started" line, and "started three minutes
ago" is exactly the silence the watchdog criterion is meant to catch, wearing a timestamp.
This file asserts the *behaviour* the structural check cannot see:

* ``confirm_execution`` emits once **per attempt**, before the attempt is evaluated, with
  the attempt number and elapsed time -- so a 200 s end-turn confirm publishes roughly a
  hundred records rather than one;
* ``provider_call_liveness`` emits while the call is still in flight, not only at its
  edges, and closes its record even when the call raises;
* neither record carries game state (Principle I: this is harness telemetry, not
  observation).

**T301 adds the reload path** -- ``saves/load_game.py::_await_phase`` and
``resilience/recovery.py::recover``, the last two silent roster entries, fixed as one
enumeration (``saves/liveness.py``'s ``RELOAD_PATH``). What that half of this file asserts
that the structural check cannot:

* ``_await_phase`` emits once per **completed poll**, with the poll number, the step, the
  elapsed time and what the poll found -- and, the load-bearing one, emits **nothing while a
  poll is hung**, which is what makes the signal work-derived rather than a ticker beside
  the wait;
* ``recover`` publishes the rung and every completed step, and a failed attempt names the
  step that failed rather than attributing it to the loader by default;
* the enumeration is honest about itself: every step it declares emitting is observed
  emitting in a real run, and the one step it declares silent is observed silent;
* no reload record carries game state, checked as an exact key set.

Time and the emitter are injected throughout, so nothing here waits on a real clock, opens
a socket, or needs a logging handler.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from typing import Any

import httpx
import pytest

from civsim_harness.act.liveness import CONFIRM_EXECUTION_WAITING
from civsim_harness.act.verify import confirm_execution
from civsim_harness.errors import NexusError
from civsim_harness.models.decision import ExecutionOutcome
from civsim_harness.models.records import RunEventType
from civsim_harness.models.run import LifecycleState
from civsim_harness.nexus.client import NexusClient, StateIndices
from civsim_harness.provider.liveness import (
    PROVIDER_CALL_FINISHED,
    PROVIDER_CALL_STARTED,
    PROVIDER_CALL_WAITING,
    provider_call_liveness,
)
from civsim_harness.provider.openrouter import OpenRouterProvider
from civsim_harness.resilience.recovery import RecoveryEngine
from civsim_harness.saves import liveness as reload_liveness
from civsim_harness.saves.liveness import (
    OUTCOME_FAILED,
    OUTCOME_LOADED,
    OUTCOME_PENDING,
    OUTCOME_RECORDED,
    OUTCOME_RESUMED,
    OUTCOME_SATISFIED,
    OUTCOME_UNREACHABLE,
    RECOVERY_RUNG,
    RELOAD_PATH,
    RELOAD_PATH_WORST_CASE_S,
    RELOAD_PHASE_ENTERED,
    RELOAD_PHASE_POLLED,
    RELOAD_RUNG_ENTERED,
    RELOAD_RUNG_STEP,
    STEP_ABANDON_ATTEMPT,
    STEP_EXIT_TO_MENU,
    STEP_LOAD_GAME,
    STEP_LOAD_SAVE,
    STEP_LOCATE_PHASE,
    STEP_RESUME,
    STEP_VERIFY_FAR_SIDE,
    WATCHDOG_SILENCE_BUDGET_S,
)
from civsim_harness.saves.load_game import LuaSaveLoader
from fakes.fake_nexus import FakeNexusServer, loaded_game_state_table

from .test_detection import _FakeLoader, _FakeStore, _run, _save
from .test_save_loader import _LoadableGame, _save_point, _script
from .test_verify_confirm import SET_PRODUCTION, _Clock, _observation, _verified_at


class _Recorder:
    """Collects emissions instead of logging them."""

    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, event: str, payload: Mapping[str, Any]) -> None:
        self.records.append((event, dict(payload)))

    def of(self, event: str) -> list[dict[str, Any]]:
        return [payload for name, payload in self.records if name == event]


# --------------------------------------------------------------------------
# Task 1 -- act/verify.py::confirm_execution
# --------------------------------------------------------------------------


async def _confirm(reads: list[Any], *, timeout_s: float, poll_s: float, emit: _Recorder) -> Any:
    remaining = list(reads[1:])

    async def reobserve() -> Any:
        return remaining.pop(0) if remaining else reads[-1]

    clock = _Clock()
    verification, _ = await confirm_execution(
        declaration=SET_PRODUCTION,
        pre_observation=_observation([]),
        first_read=reads[0],
        observation_of=lambda observation: observation,
        reobserve=reobserve,
        target="UNIT_BUILDER",
        clock=_verified_at,
        timeout_s=timeout_s,
        poll_s=poll_s,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        emit=emit,
    )
    return verification


@pytest.mark.asyncio
async def test_confirm_execution_emits_once_per_attempt() -> None:
    """The load-bearing assertion. A never-confirming 200 s window at the production
    2 s poll must publish a record for every attempt, not one at the start -- otherwise
    the watchdog's three-minute budget is blown by attempt ~90 while the loop is healthy.
    """
    emit = _Recorder()
    verification = await _confirm([_observation([])], timeout_s=200.0, poll_s=2.0, emit=emit)

    assert verification.execution.outcome is ExecutionOutcome.REJECTED
    waiting = emit.of(CONFIRM_EXECUTION_WAITING)
    assert len(waiting) == 101, f"expected one record per attempt, got {len(waiting)}"
    assert [record["attempt"] for record in waiting] == list(range(1, 102))
    assert [record["elapsed_s"] for record in waiting] == [float(2 * index) for index in range(101)]

    # The gap between consecutive records is the poll interval, which is what makes this a
    # heartbeat rather than a timestamped silence.
    gaps = [
        waiting[index + 1]["elapsed_s"] - waiting[index]["elapsed_s"]
        for index in range(len(waiting) - 1)
    ]
    assert max(gaps) == 2.0


@pytest.mark.asyncio
async def test_confirm_execution_emits_on_the_first_attempt_before_evaluating() -> None:
    """A confirm that succeeds immediately still says what it was waiting for -- and the
    record is published before the evaluation, so a hang *inside* the evaluation is still
    preceded by evidence that the loop reached it."""
    emit = _Recorder()
    verification = await _confirm(
        [_observation(["UNIT_BUILDER"])], timeout_s=200.0, poll_s=2.0, emit=emit
    )

    assert verification.execution.outcome is ExecutionOutcome.APPLIED
    waiting = emit.of(CONFIRM_EXECUTION_WAITING)
    assert len(waiting) == 1
    assert waiting[0]["attempt"] == 1
    assert waiting[0]["elapsed_s"] == 0.0


@pytest.mark.asyncio
async def test_confirm_execution_record_says_what_it_is_waiting_for() -> None:
    emit = _Recorder()
    await _confirm([_observation([])], timeout_s=4.0, poll_s=1.0, emit=emit)

    record = emit.of(CONFIRM_EXECUTION_WAITING)[0]
    assert record["declaration_id"] == "cities.set_production"
    assert record["predicate"] == "target in city.production_queue"
    assert record["timeout_s"] == 4.0
    assert record["poll_s"] == 1.0


@pytest.mark.asyncio
async def test_confirm_execution_record_carries_no_game_state() -> None:
    """Principle I. The record names the *question* (the static catalog predicate), never
    its answer: no observation, no predicate bindings, no `last_read` quotation. A city
    name that reached a log line would be a second channel by which game state escapes
    the observation path."""
    emit = _Recorder()
    await _confirm([_observation(["UNIT_BUILDER"])], timeout_s=4.0, poll_s=1.0, emit=emit)

    record = emit.of(CONFIRM_EXECUTION_WAITING)[0]
    assert set(record) == {
        "declaration_id",
        "predicate",
        "attempt",
        "elapsed_s",
        "timeout_s",
        "poll_s",
    }
    # Values from `_observation`, none of which may appear anywhere in the payload.
    rendered = repr(record)
    for leaked in ("Pasargadae", "65538", "UNIT_BUILDER", "city_screen", "obs-1"):
        assert leaked not in rendered, f"{leaked!r} leaked into a liveness record"


# --------------------------------------------------------------------------
# Task 2 -- provider/liveness.py
# --------------------------------------------------------------------------


def test_provider_liveness_ticks_while_the_call_is_in_flight() -> None:
    """The load-bearing assertion. A start/end pair is not a heartbeat: a "started" line
    logged 179 s ago is indistinguishable from a hang. The window must publish *during*
    the call."""
    emit = _Recorder()
    with provider_call_liveness(
        provider="openrouter",
        model="anthropic/claude-opus-5",
        step_index=3,
        bound_s=120.0,
        tick_interval_s=0.01,
        emit=emit,
    ):
        deadline = time.monotonic() + 2.0
        while not emit.of(PROVIDER_CALL_WAITING) and time.monotonic() < deadline:
            time.sleep(0.01)

    waiting = emit.of(PROVIDER_CALL_WAITING)
    assert waiting, "no tick was published while the call was in flight"
    assert waiting[0]["tick"] == 1
    assert waiting[0]["elapsed_s"] > 0.0
    assert waiting[0]["provider"] == "openrouter"
    assert waiting[0]["model"] == "anthropic/claude-opus-5"
    assert waiting[0]["bound_s"] == 120.0
    assert waiting[0]["step_index"] == 3


def test_provider_liveness_brackets_the_call() -> None:
    emit = _Recorder()
    with provider_call_liveness(provider="openrouter", model="m", emit=emit):
        pass

    assert emit.records[0][0] == PROVIDER_CALL_STARTED
    assert emit.records[-1][0] == PROVIDER_CALL_FINISHED
    assert emit.records[0][1]["elapsed_s"] == 0.0


def test_provider_liveness_closes_its_record_when_the_call_raises() -> None:
    """A call that dies mid-flight must not leave a dangling "started" -- that reads as a
    hang to exactly the watchdog this exists to inform."""
    emit = _Recorder()
    with (
        pytest.raises(RuntimeError),
        provider_call_liveness(provider="openrouter", model="m", emit=emit),
    ):
        raise RuntimeError("connection reset")

    assert emit.of(PROVIDER_CALL_FINISHED), "no finished record on the exception path"


def test_provider_liveness_never_fails_the_call_it_describes() -> None:
    """Telemetry that can break a model call has inverted its own purpose."""
    from civsim_harness.provider.liveness import emit_provider_liveness

    def exploding_log(*args: Any, **kwargs: Any) -> None:
        raise OSError("log device full")

    import civsim_harness.provider.liveness as module

    original = module.log_event
    module.log_event = exploding_log  # type: ignore[assignment]
    try:
        emit_provider_liveness("provider.call.started", {"provider": "p"})
    finally:
        module.log_event = original  # type: ignore[assignment]


def test_openrouter_complete_publishes_the_window_around_its_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wiring, end to end through the real adapter: `complete` must reach the emitter,
    not merely import it. This is the shape the whole task existed for --
    `run/decision_loop.py:779` is `ctx.provider.complete(request)`, and in production that
    lands here."""
    from .test_openrouter_adapter import _VALID_DECISION_CONTENT, _provider_with_handler, _request

    published: list[tuple[str, dict[str, Any]]] = []

    import civsim_harness.provider.liveness as module

    monkeypatch.setattr(
        module,
        "log_event",
        lambda logger, level, message, **kw: published.append(
            (message, dict(kw.get("extra") or {}))
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"choices": [{"message": {"content": _VALID_DECISION_CONTENT}}]}
        )

    provider: OpenRouterProvider = _provider_with_handler(handler, monkeypatch)
    provider.complete(_request())

    kinds = [name for name, _ in published]
    assert PROVIDER_CALL_STARTED in kinds
    assert PROVIDER_CALL_FINISHED in kinds


# --------------------------------------------------------------------------
# T301 -- the reload path: saves/load_game.py::_await_phase (the wait) and
# resilience/recovery.py::recover (the rung built on it), as ONE enumeration.
# --------------------------------------------------------------------------


@pytest.fixture
def reload_records(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    """Intercept at ``reload_liveness.log_event`` -- the one call both phases publish through,
    and the name the roster requires at their call sites."""
    recorder = _Recorder()
    monkeypatch.setattr(reload_liveness, "log_event", recorder)
    return recorder


def _steps_of(recorder: _Recorder, event: str) -> list[str]:
    return [payload["step"] for payload in recorder.of(event)]


class _ScriptedClient:
    """Duck-typed `NexusClient`: only what `_await_phase` touches. Each scripted entry is
    either an exception the round trip raises or the `StateIndices` it returns, so a test can
    say exactly what each poll found."""

    def __init__(self, *steps: Any, block_forever: bool = False) -> None:
        self._steps = list(steps)
        self._block_forever = block_forever
        self.is_connected = True
        self.calls = 0

    async def refresh_state_indices(self) -> StateIndices:
        return await self._round_trip()

    async def reconnect(self) -> StateIndices:
        return await self._round_trip()

    async def _round_trip(self) -> StateIndices:
        self.calls += 1
        if self._block_forever:
            await asyncio.Event().wait()  # a round trip that never returns
        if not self._steps:
            raise AssertionError("the scripted client ran out of polls -- the test under-scripted")
        outcome = self._steps.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        assert isinstance(outcome, StateIndices)
        return outcome


def _indices(*names: str) -> StateIndices:
    return StateIndices(by_name={name: index for index, name in enumerate(names)})


def _awaiting_loader(client: Any) -> LuaSaveLoader:
    return LuaSaveLoader(client, poll_interval_s=0.001)  # type: ignore[arg-type]


# -- the enumeration ------------------------------------------------------------------------


def test_the_reload_path_enumeration_sums_past_the_watchdog_budget() -> None:
    """Why the two phases were fixed as one change. Neither entry's own bound tells you the
    number that matters: a single `LuaSaveLoader.load` can stack 90 + 90 + 300 + 30 s, and it
    is the *sum* that is 2.8x the budget. Fixing them apart would have left it unstated."""
    assert RELOAD_PATH_WORST_CASE_S == 510.0
    assert RELOAD_PATH_WORST_CASE_S > 2.8 * WATCHDOG_SILENCE_BUDGET_S

    understated = [
        step.step
        for step in RELOAD_PATH
        if step.bound_s is not None
        and step.bound_s > WATCHDOG_SILENCE_BUDGET_S
        and not step.can_outlast_budget
    ]
    assert understated == [], f"bound exceeds the budget but is not declared to: {understated}"
    assert {step.step for step in RELOAD_PATH if not step.emits} == {STEP_VERIFY_FAR_SIDE}


# -- _await_phase: the wait -------------------------------------------------------------------


async def test_await_phase_emits_once_per_completed_poll(reload_records: _Recorder) -> None:
    """The load-bearing cadence assertion. A 300 s wait on a 1 s poll must publish ~300 records,
    each naming what that poll found -- the port refused (the expected mid-load shape), the
    state table read but the predicate not yet holding, or the wait satisfied."""
    client = _ScriptedClient(
        NexusError("connection refused", detail={}),
        OSError("connection reset by peer"),
        _indices("Main State", "DebugHotloadCache"),
        _indices("GameCore_Tuner", "InGame"),
    )
    indices = await _awaiting_loader(client)._await_phase(
        step=STEP_LOAD_GAME,
        predicate=lambda st: "InGame" in st.by_name,
        timeout_s=30.0,
        waiting_for="the game states to reappear",
        save=_save(5),
    )

    assert "InGame" in indices.by_name
    polled = reload_records.of(RELOAD_PHASE_POLLED)
    assert len(polled) == 4 == client.calls, "one record per completed round trip, no more"
    assert [record["poll"] for record in polled] == [1, 2, 3, 4]
    assert [record["outcome"] for record in polled] == [
        OUTCOME_UNREACHABLE,
        OUTCOME_UNREACHABLE,
        OUTCOME_PENDING,
        OUTCOME_SATISFIED,
    ]
    # `state_count` is the proof a read actually happened: absent when the client would not
    # answer, present (and a number, never a name) when it did.
    assert [record["state_count"] for record in polled] == [None, None, 2, 2]
    assert {record["step"] for record in polled} == {STEP_LOAD_GAME}

    entered = reload_records.of(RELOAD_PHASE_ENTERED)
    assert len(entered) == 1
    assert entered[0] == {"step": STEP_LOAD_GAME, "timeout_s": 30.0, "poll_interval_s": 0.001}


async def test_await_phase_publishes_nothing_while_a_poll_is_hung(
    reload_records: _Recorder,
) -> None:
    """**The work-derived assertion, and the whole reason this is not a ticker thread.**

    `provider/liveness.py` registers itself observer-derived because a daemon thread beside a
    blocking socket ticks just as happily when the socket is dead -- it reports that the call
    has not returned, never that it is progressing. Here the emission is the poll's own
    byproduct, so a round trip that never comes back publishes *nothing*: the stream going
    quiet is the signal, rather than being masked by a clock that keeps talking.
    """
    client = _ScriptedClient(block_forever=True)
    task = asyncio.create_task(
        _awaiting_loader(client)._await_phase(
            step=STEP_LOAD_GAME,
            predicate=lambda st: True,
            timeout_s=30.0,
            waiting_for="the game states to reappear",
            save=_save(5),
        )
    )
    await asyncio.sleep(0.1)  # 100x the poll interval: a timer-driven tick would have fired
    try:
        assert reload_records.of(RELOAD_PHASE_POLLED) == [], (
            "a record was published for a poll that never completed -- the signal is "
            "observer-derived, not work-derived"
        )
        assert len(reload_records.of(RELOAD_PHASE_ENTERED)) == 1
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_await_phase_record_carries_no_game_state(reload_records: _Recorder) -> None:
    """Principle I, as an exact key set so a future author who widens the payload gets a red
    test rather than a silent second channel out of the observation path. The record names the
    *wait* -- which step, which poll, how long, against what bound -- and never what the client
    said: no state names, no turn number, no local player, no save name."""
    client = _ScriptedClient(_indices("InGame", "Pasargadae_CityBanner", "GameCore_Tuner"))
    await _awaiting_loader(client)._await_phase(
        step=STEP_LOCATE_PHASE,
        predicate=lambda st: True,
        timeout_s=90.0,
        waiting_for="a recognisable phase",
        save=_save(5),
    )

    record = reload_records.of(RELOAD_PHASE_POLLED)[0]
    assert set(record) == {
        "step",
        "poll",
        "elapsed_s",
        "timeout_s",
        "poll_interval_s",
        "outcome",
        "state_count",
    }
    rendered = repr(reload_records.records)
    for leaked in ("Pasargadae", "InGame", "GameCore_Tuner", "civsim__run-1__t0005", "sp-5"):
        assert leaked not in rendered, f"{leaked!r} leaked into a reload liveness record"


async def test_a_full_load_publishes_every_loader_step_the_enumeration_declares(
    reload_records: _Recorder,
) -> None:
    """The wiring, end to end through the real `LuaSaveLoader` over the recorded-transcript
    fake -- `load()` must *reach* the emitter at every step, not merely import it. Asserted
    against the enumeration itself, in both directions: every `loader.*` step declared to emit
    is observed, and the one declared silent is observed silent."""
    save = _save_point()
    async with FakeNexusServer(state_table=loaded_game_state_table()) as server:
        game = _LoadableGame(server)
        _script(server, game)
        client = NexusClient(host="127.0.0.1", port=server.port)
        try:
            await client.connect()
            await LuaSaveLoader(
                client,
                exit_to_menu_timeout_s=5.0,
                load_timeout_s=5.0,
                verify_timeout_s=5.0,
                poll_interval_s=0.01,
            ).load(save)
        finally:
            await client.close()

    observed = set(_steps_of(reload_records, RELOAD_PHASE_POLLED))
    declared = {step.step for step in RELOAD_PATH if step.emits and step.step.startswith("loader")}
    assert observed == declared == {STEP_LOCATE_PHASE, STEP_EXIT_TO_MENU, STEP_LOAD_GAME}
    assert STEP_VERIFY_FAR_SIDE not in observed, "the declared-silent step emitted anyway"
    assert set(_steps_of(reload_records, RELOAD_PHASE_ENTERED)) == declared


# -- recover: the rung ------------------------------------------------------------------------


def _engine(loader: Any, store: _FakeStore, **overrides: Any) -> RecoveryEngine:
    kwargs: dict[str, Any] = {
        "run_id": store.run.run_id,
        "store": store,
        "loader": loader,
        "recovery_attempt_limit": 3,
    }
    kwargs.update(overrides)
    return RecoveryEngine(**kwargs)


async def test_recover_publishes_the_rung_and_every_completed_step(
    reload_records: _Recorder,
) -> None:
    """Rung 4 is a long phase, and it used to publish only `RunEvent`s -- durable ledger
    evidence a log-polling watchdog cannot see. Now the rung says it is in flight, and each
    step says it finished, so "a rung of this run is running" is a recorded state."""
    store = _FakeStore(run=_run(LifecycleState.PLAYING))
    result = await _engine(_FakeLoader(), store).recover(
        store.run,
        turn_number=5,
        turn_start_save=_save(5),
        trigger_event_type=RunEventType.CRASH_DETECTED,
    )

    assert result.run.lifecycle_state is LifecycleState.PLAYING
    entered = reload_records.of(RELOAD_RUNG_ENTERED)
    assert len(entered) == 1
    assert entered[0] == {
        "run_id": "run-1",
        "rung": RECOVERY_RUNG,
        "attempt": 1,
        "attempt_limit": 3,
        "trigger": "crash_detected",
    }
    steps = [(record["step"], record["outcome"]) for record in reload_records.of(RELOAD_RUNG_STEP)]
    assert steps == [
        (STEP_ABANDON_ATTEMPT, OUTCOME_RECORDED),
        (STEP_LOAD_SAVE, OUTCOME_LOADED),
        (STEP_RESUME, OUTCOME_RESUMED),
    ]


async def test_a_failed_attempt_names_its_step_and_counts_against_the_limit(
    reload_records: _Recorder,
) -> None:
    """A failure is attributed to the step that actually failed, and the attempt number says
    how much ladder is left -- so an escalation that burned three attempts is readable as
    three attempts rather than as one long silence."""
    store = _FakeStore(run=_run(LifecycleState.PLAYING))
    engine = _engine(_FakeLoader(fail_times=2), store)
    with pytest.raises(RuntimeError):
        await engine.recover(
            store.run,
            turn_number=5,
            turn_start_save=_save(5),
            trigger_event_type=RunEventType.HANG_DETECTED,
        )

    assert reload_records.of(RELOAD_RUNG_ENTERED)[0]["attempt"] == 1
    steps = [(record["step"], record["outcome"]) for record in reload_records.of(RELOAD_RUNG_STEP)]
    assert steps == [(STEP_ABANDON_ATTEMPT, OUTCOME_RECORDED), (STEP_LOAD_SAVE, OUTCOME_FAILED)]

    # The next attempt says it is the second, which is what makes "the ladder is working" and
    # "the ladder is looping" distinguishable from outside the process.
    reload_records.records.clear()
    with pytest.raises(RuntimeError):
        await engine.recover(
            store.run,
            turn_number=5,
            turn_start_save=_save(5),
            trigger_event_type=RunEventType.HANG_DETECTED,
        )
    assert reload_records.of(RELOAD_RUNG_ENTERED)[0]["attempt"] == 2


async def test_rung_records_carry_no_game_state(reload_records: _Recorder) -> None:
    """Principle I on the rung, as an exact key set. `run_id` scopes the record to the thing
    the watchdog guards; nothing else identifies anything in the game -- no turn number, no
    save name, no save point id, no lifecycle detail."""
    store = _FakeStore(run=_run(LifecycleState.PLAYING))
    await _engine(_FakeLoader(), store).recover(
        store.run,
        turn_number=5,
        turn_start_save=_save(5),
        trigger_event_type=RunEventType.CRASH_DETECTED,
    )

    assert set(reload_records.of(RELOAD_RUNG_ENTERED)[0]) == {
        "run_id",
        "rung",
        "attempt",
        "attempt_limit",
        "trigger",
    }
    for record in reload_records.of(RELOAD_RUNG_STEP):
        assert set(record) == {"run_id", "rung", "step", "attempt", "elapsed_s", "outcome"}
    rendered = repr(reload_records.records)
    for leaked in ("civsim__run-1__t0005", "sp-5", "turn_number"):
        assert leaked not in rendered, f"{leaked!r} leaked into a rung liveness record"


async def test_a_watchdog_can_tell_rung_four_loading_a_save_from_a_wedged_harness(
    reload_records: _Recorder,
) -> None:
    """**The acceptance question**, asserted rather than argued: the real `RecoveryEngine`
    driving the real `LuaSaveLoader`, and the driver log alone answering *which rung, which
    step, how far into which bound*.

    Before this change the whole sequence below published nothing a log-polling watchdog could
    read -- for up to 510 s -- so a healthy rung 4 and a wedged process were the same thing to
    it. Worse, killing the healthy one leaves wreckage indistinguishable from the crash the
    reload was repairing.
    """
    save = _save_point()
    async with FakeNexusServer(state_table=loaded_game_state_table()) as server:
        game = _LoadableGame(server)
        _script(server, game)
        client = NexusClient(host="127.0.0.1", port=server.port)
        try:
            await client.connect()
            loader = LuaSaveLoader(
                client,
                exit_to_menu_timeout_s=5.0,
                load_timeout_s=5.0,
                verify_timeout_s=5.0,
                poll_interval_s=0.01,
            )
            store = _FakeStore(run=_run(LifecycleState.PLAYING))
            await _engine(loader, store).recover(
                store.run,
                turn_number=save.turn_number,
                turn_start_save=save,
                trigger_event_type=RunEventType.CRASH_DETECTED,
            )
        finally:
            await client.close()

    # 1. Which rung, and that it is in flight at all.
    assert reload_records.of(RELOAD_RUNG_ENTERED)[0]["rung"] == RECOVERY_RUNG
    # 2. Which step of it -- the poll records for the 300 s leg arrive *between* the rung's
    #    entry and the step that closes it, so the interior of the long wait is not silent.
    kinds = [event for event, _ in reload_records.records]
    opened = kinds.index(RELOAD_RUNG_ENTERED)
    closed = len(kinds) - 1 - kinds[::-1].index(RELOAD_RUNG_STEP)
    interior = [
        payload["step"]
        for event, payload in reload_records.records[opened:closed]
        if event == RELOAD_PHASE_POLLED
    ]
    assert STEP_LOAD_GAME in interior
    # 3. How far into which bound: every poll record carries both, so elapsed can be read
    #    against the phase's own bound rather than against the watchdog's ceiling.
    for payload in reload_records.of(RELOAD_PHASE_POLLED):
        assert payload["elapsed_s"] < payload["timeout_s"]


def test_reload_liveness_never_fails_the_operation_it_describes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Telemetry that can break a recovery has inverted its own purpose -- and this one
    describes the path the run takes *after* something already went wrong."""

    def exploding_log(*args: Any, **kwargs: Any) -> None:
        raise OSError("log device full")

    monkeypatch.setattr(reload_liveness, "_log_harness_event", exploding_log)
    assert reload_liveness.log_event(RELOAD_PHASE_POLLED, {"step": STEP_LOAD_GAME}) is None
