"""T249 (seam half) -- the capture-precondition preflight, asserted on the far side.

The seam: ``observe/capture.py`` asks the host port's ``check_capture_preconditions`` BEFORE any
frame is taken, and a non-passing result withholds the step through the exact degradation path
every other host capture failure takes (T157/T133/T238's machinery -- nothing parallel). This
file proves the far side of that claim through the real ``run_turn_cycle`` orchestration against
a real ``SqliteMatchStore``, reusing ``test_image_attachment``'s T238 harness so the two files
cannot quietly diverge on what "the production capture path" means:

- a failing precondition on a VALIDATED host (the one tier that WOULD have permitted attachment)
  yields withheld captures with the reason recorded, no blob stored, no frame ever taken from
  the host, and a provider request carrying no image;
- and because the refusal rides the existing degradation paths, T240's run-level comparability
  downgrade fires exactly as it does for any other capture loss -- reused, not reinvented.

The passing-precondition side needs no test here by construction: the fake host's default
preflight passes, so every T238 test in ``test_image_attachment.py`` already runs with a passing
preflight and pins that it changes nothing. The per-adapter implementations are covered in
``tests/unit/test_host_probe_and_composition.py``; the source-half contract in
``tests/unit/test_capture_for_step.py``.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from civsim_harness.models.common import RunId
from civsim_harness.models.records import RunEventType
from civsim_harness.models.run import ComparabilityStatus, HostSupportTier
from civsim_harness.models.turn import ScreeningStatus, TurnOutcome, WithheldReason
from civsim_harness.run.turn_cycle import run_turn_cycle
from civsim_harness.store.sqlite_adapter import SqliteMatchStore
from fakes.fake_host import FakeHostPlatform
from fakes.fake_provider import FakeModelProvider
from integration.test_image_attachment import (
    _build_run_and_config,
    _clean_capture_result,
    _FakeGame,
    _make_deps,
    _png_bytes,
    _SpyStore,
    _tick,
)

_PRECONDITION_REASON = "scripted: no compositing manager owns _NET_WM_CM_S0"


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)


async def test_a_failing_precondition_on_a_validated_host_withholds_and_attaches_nothing(
    tmp_path: Path,
) -> None:
    """The seam's whole contract in one far-side scenario: the host WOULD serve a clean frame
    and the run's tier WOULD permit attachment, so everything withheld below is the preflight's
    doing -- and "withheld" means the frame was never taken, not taken and discarded."""
    run_id = RunId("run-preflight-refusal")
    run, config = _build_run_and_config(
        run_id, tier=HostSupportTier.VALIDATED, comparability=ComparabilityStatus.COMPARABLE
    )
    store = SqliteMatchStore(tmp_path / "match.db")
    store.create_run(run, config)
    spy = _SpyStore(store)

    host = FakeHostPlatform()
    host.set_capture_result(_clean_capture_result(_png_bytes()))
    host.set_capture_preconditions_failed(_PRECONDITION_REASON)

    provider = FakeModelProvider()
    provider.queue_decision(_tick(is_end_turn=True))

    deps = _make_deps(
        tmp_path=tmp_path, run_id=run_id, store=spy, game=_FakeGame(), provider=provider, host=host
    )

    try:
        outcome = await run_turn_cycle(deps, run=run)
        assert outcome.outcome is TurnOutcome.ENDED_BY_AGENT

        # Far side of the provider port: the request the provider received carries no image.
        assert len(provider.calls) == 1
        assert provider.calls[0].images == []

        # The frame was never taken at all: the preflight was consulted, capture_window never.
        assert host.capture_precondition_calls >= 1
        assert host.capture_calls == []

        # What the store received: every capture written withheld, reason class recorded,
        # nothing shown, no blob anywhere (the decision step's own capture plus the trailing
        # end-turn verification read's).
        assert len(spy.capture_writes) == 2
        for capture, blob in spy.capture_writes:
            assert capture.screening_status is ScreeningStatus.WITHHELD
            assert capture.withheld_reason is WithheldReason.CAPTURE_FAILED
            assert capture.shown_to_agent is False
            assert capture.blob_ref is None
            assert blob is None

        # The persisted record agrees end to end: no capture listed on the observation, the
        # step and turn honestly degraded, zero images accounted on the model call.
        record = store.get_turn_cycle(run_id, 1)
        assert record is not None
        bundle = record.steps[0]
        assert bundle.observation.captures == []
        assert bundle.step.visually_degraded is True
        assert bundle.model_call.image_count == 0
        assert record.turn_cycle.visually_degraded is True

        # The precondition's own reason is on the timeline, verbatim, on the same
        # capture_failed events every other capture loss produces.
        failures = store.list_run_events(run_id, event_types=[RunEventType.CAPTURE_FAILED])
        assert failures != []
        for event in failures:
            assert _PRECONDITION_REASON in str(event.detail.get("reason"))

        # And the run-level rollup is the same one every degradation path takes (T240):
        # a COMPARABLE run that lost its images is downgraded on the record, exactly once.
        assert spy.update_run_calls == [
            (run_id, {"comparability_status": ComparabilityStatus.VISUALLY_DEGRADED})
        ]
    finally:
        store.close()
