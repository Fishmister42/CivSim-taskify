"""The two long phases fixed in T302 actually emit, per attempt and mid-flight.

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

Time and the emitter are injected throughout, so nothing here waits on a real clock, opens
a socket, or needs a logging handler.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

import httpx
import pytest

from civsim_harness.act.liveness import CONFIRM_EXECUTION_WAITING
from civsim_harness.act.verify import confirm_execution
from civsim_harness.models.decision import ExecutionOutcome
from civsim_harness.provider.liveness import (
    PROVIDER_CALL_FINISHED,
    PROVIDER_CALL_STARTED,
    PROVIDER_CALL_WAITING,
    provider_call_liveness,
)
from civsim_harness.provider.openrouter import OpenRouterProvider

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
