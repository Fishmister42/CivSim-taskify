"""Unit tests for the operator surface: CLI (T118), loopback HTTP API (T119),
command handling (T120), and `doctor` (T121).

Three things this suite exists to prove, beyond ordinary behaviour coverage:

1. **The `status` schema is closed structurally, not by convention.**
   `RunStatusView` (and its nested value types) reject an unrecognised field
   at construction time -- proven directly against the pydantic model, and
   again against the literal JSON the HTTP endpoint serves.
2. **A command is recorded before it takes effect.** `operator/commands.py`
   writes the `lifecycle_command_received` event first and calls the runner
   second, for pause/resume/stop -- proven by a shared ordering log both the
   fake store and the fake runner append to.
3. **The HTTP server only ever binds `127.0.0.1`.** `run_server` has no `host`
   parameter to override that -- proven both by inspecting its signature and
   by intercepting the `uvicorn.run` call it makes.

`RunnerProtocol` (operator/runner_protocol.py) has no implementation yet --
`run/runner.py` (T116) is a later wave -- so every test here supplies its own
`FakeRunner`, which is also this suite's structural conformance check: it
satisfies `RunnerProtocol` with nothing more than plain methods, proving the
protocol is implementable without importing anything from this wave's files.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from typer.testing import CliRunner

from civsim_harness.errors import HarnessError, NexusError, PreflightError
from civsim_harness.host.detect import (
    UNPROBED,
    HostInfo,
    LinuxSessionType,
    OperatingSystem,
    SupportProbeResult,
)
from civsim_harness.models.common import RunId
from civsim_harness.models.run import ComparabilityStatus, LifecycleState, RecordCompletenessStatus
from civsim_harness.operator import api, cli, commands, doctor
from civsim_harness.operator.runner_protocol import RunnerProtocol
from civsim_harness.operator.schemas import (
    ConnectionHealth,
    LastError,
    LastKnownGoodSave,
    RunStatusView,
)
from civsim_harness.store.sqlite_adapter import SqliteMatchStore

REPO_CATALOG_ROOT = Path(__file__).resolve().parents[2] / "catalogs"

# --------------------------------------------------------------------------
# Shared fixtures / test doubles
# --------------------------------------------------------------------------


def _status(
    run_id: str,
    lifecycle_state: LifecycleState = LifecycleState.PLAYING,
    *,
    requested_state: LifecycleState | None = None,
    archived: bool = False,
) -> RunStatusView:
    return RunStatusView(
        run_id=RunId(run_id),
        lifecycle_state=lifecycle_state,
        requested_state=requested_state,
        current_turn=3,
        current_step=7,
        last_known_good_save=LastKnownGoodSave(turn=3, save_point_id="sp-3"),
        last_error=None,
        connection_health=ConnectionHealth(tuner="ok", client="ok", store="ok"),
        record_completeness_status=RecordCompletenessStatus.UNKNOWN,
        comparability_status=ComparabilityStatus.COMPARABLE,
        archived=archived,
        disk_headroom_gb=42.0,
    )


@dataclass
class FakeRunner:
    """A minimal `RunnerProtocol` implementation -- also this suite's proof
    that the protocol is satisfiable with plain methods, no inheritance.
    """

    order_log: list[str] = field(default_factory=list)
    statuses: dict[str, RunStatusView] = field(default_factory=dict)
    fail_commands: frozenset[str] = frozenset()
    next_run_id: str = "run-started"

    def start(self, config_path: Path) -> RunId:
        self.order_log.append("runner:start")
        run_id = RunId(self.next_run_id)
        self.statuses[run_id] = _status(run_id)
        return run_id

    def request_pause(self, run_id: RunId) -> None:
        self._request("pause", run_id, LifecycleState.PAUSED)

    def request_resume(self, run_id: RunId) -> None:
        self._request("resume", run_id, LifecycleState.PLAYING)

    def request_stop(self, run_id: RunId) -> None:
        self._request("stop", run_id, LifecycleState.FINISHED)

    def resume_from(self, run_id: RunId, turn: int) -> None:
        """T173 addition to `RunnerProtocol` -- resume the same run_id from
        an earlier recorded turn's save, as distinct from `request_resume`
        (which only resumes a currently-paused run from where it already
        is).
        """
        self.order_log.append(f"runner:resume_from:{turn}")
        if "resume_from" in self.fail_commands or run_id not in self.statuses:
            raise HarnessError(f"resume_from rejected for run {run_id!r} turn {turn}")
        current = self.statuses[run_id]
        self.statuses[run_id] = _status(
            str(run_id), LifecycleState.PLAYING, archived=current.archived
        )

    def _request(self, verb: str, run_id: RunId, requested: LifecycleState) -> None:
        self.order_log.append(f"runner:{verb}")
        if verb in self.fail_commands or run_id not in self.statuses:
            raise HarnessError(f"{verb} rejected")
        current = self.statuses[run_id]
        self.statuses[run_id] = _status(
            str(run_id), current.lifecycle_state, requested_state=requested
        )

    def get_status(self, run_id: RunId) -> RunStatusView:
        if run_id not in self.statuses:
            raise HarnessError(f"unknown run {run_id!r}")
        return self.statuses[run_id]


class OrderLoggingStore:
    """The only `MatchStore` method `operator/commands.py` calls is
    `write_run_event` -- this double supplies exactly that, appending to the
    same `order_log` a `FakeRunner` shares, so a test can assert the event
    was written before the runner was ever called.
    """

    def __init__(self, order_log: list[str]) -> None:
        self.order_log = order_log
        self.events: list[Any] = []

    def write_run_event(self, event: Any) -> str:
        self.order_log.append(f"store:{event.event_type.value}")
        self.events.append(event)
        return event.event_id


# --------------------------------------------------------------------------
# 1. The status schema is closed structurally (FR-053)
# --------------------------------------------------------------------------


def test_run_status_view_rejects_an_unrecognised_field() -> None:
    base = _status("run-1").model_dump(mode="json")
    base["turn_records"] = ["should never be accepted"]
    with pytest.raises(ValidationError):
        RunStatusView.model_validate(base)


@pytest.mark.parametrize(
    "cls,payload",
    [
        (LastKnownGoodSave, {"turn": 1, "save_point_id": "sp", "extra": "x"}),
        (LastError, {"type": "crash_detected", "at": "2026-01-01T00:00:00Z", "extra": "x"}),
        (ConnectionHealth, {"tuner": "ok", "client": "ok", "store": "ok", "extra": "x"}),
    ],
)
def test_nested_status_value_types_are_also_closed(cls: type, payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        cls.model_validate(payload)


def test_run_status_view_carries_only_the_fr053_named_fields() -> None:
    fields = set(RunStatusView.model_fields)
    assert fields == {
        "run_id",
        "lifecycle_state",
        "requested_state",
        "current_turn",
        "current_step",
        "last_known_good_save",
        "last_error",
        "connection_health",
        "record_completeness_status",
        "comparability_status",
        "archived",
        "disk_headroom_gb",
    }


# --------------------------------------------------------------------------
# 2. RunnerProtocol conformance
# --------------------------------------------------------------------------


def test_fake_runner_satisfies_runner_protocol_structurally() -> None:
    runner: RunnerProtocol = FakeRunner()
    assert isinstance(runner, RunnerProtocol)


# --------------------------------------------------------------------------
# 3. commands.py -- record before effect; start has no event
# --------------------------------------------------------------------------


def test_pause_records_the_event_before_calling_the_runner() -> None:
    order: list[str] = []
    runner = FakeRunner(order_log=order, statuses={RunId("run-1"): _status("run-1")})
    store = OrderLoggingStore(order)

    result = commands.pause(store, runner, RunId("run-1"))

    assert order == ["store:lifecycle_command_received", "runner:pause"]
    assert result.requested_state == LifecycleState.PAUSED
    assert store.events[0].detail == {"command": "pause"}


def test_resume_and_stop_also_record_before_effect() -> None:
    order: list[str] = []
    runner = FakeRunner(order_log=order, statuses={RunId("run-1"): _status("run-1")})
    store = OrderLoggingStore(order)

    commands.resume(store, runner, RunId("run-1"))
    commands.stop(store, runner, RunId("run-1"))

    assert order == [
        "store:lifecycle_command_received",
        "runner:resume",
        "store:lifecycle_command_received",
        "runner:stop",
    ]


def test_pause_still_recorded_even_when_the_runner_rejects_it() -> None:
    """The event is the record of what was *asked*, independent of whether
    the runner honours it -- so it must land even on a rejection.
    """
    order: list[str] = []
    runner = FakeRunner(
        order_log=order,
        statuses={RunId("run-1"): _status("run-1")},
        fail_commands=frozenset({"pause"}),
    )
    store = OrderLoggingStore(order)

    with pytest.raises(HarnessError):
        commands.pause(store, runner, RunId("run-1"))

    assert order == ["store:lifecycle_command_received", "runner:pause"]


def test_start_writes_no_command_event() -> None:
    """`start` has no run_id until the runner creates one -- see
    `operator/commands.py`'s module docstring for why it is the one command
    with no `lifecycle_command_received` event of its own.
    """
    runner = FakeRunner()
    run_id = commands.start(runner, Path("config.yaml"))
    assert run_id == RunId("run-started")
    assert runner.order_log == ["runner:start"]


def test_status_is_a_read_with_no_event() -> None:
    runner = FakeRunner(statuses={RunId("run-1"): _status("run-1")})
    result = commands.status(runner, RunId("run-1"))
    assert result.run_id == RunId("run-1")


# --------------------------------------------------------------------------
# 4. The HTTP form (T119) -- same closed schema, loopback-only binding
# --------------------------------------------------------------------------


@pytest.fixture
def http_client() -> TestClient:
    runner = FakeRunner(statuses={RunId("run-1"): _status("run-1")})
    store = OrderLoggingStore([])

    async def doctor_provider() -> doctor.DoctorReport:
        return _fake_doctor_report()

    app = api.create_app(runner=runner, store=store, doctor_provider=doctor_provider)
    return TestClient(app)


def _fake_doctor_report() -> doctor.DoctorReport:
    return doctor.DoctorReport(
        platform=doctor.PlatformDiagnostic(status="ok", os="windows", tier="validated"),
        tuner=doctor.TunerDiagnostic(status="ok", game_core_tuner_index=1, in_game_index=2),
        client=doctor.ClientDiagnostic(status="ok", pid=123, build="win/1.0.0"),
        store=doctor.StoreDiagnostic(status="ok"),
        catalog=doctor.CatalogDiagnostic(
            status="ok", version="t", declaration_count=1, undeclared_count=0
        ),
        capture_path=doctor.CapturePathDiagnostic(
            status="ok", path="windows_graphics_capture", hygiene_spike="passed"
        ),
        provider_key=doctor.ProviderKeyDiagnostic(env_var="OPENROUTER_API_KEY", present=True),
        disk=doctor.DiskDiagnostic(status="ok", free_gb=10.0, total_gb=100.0),
    )


def test_get_status_returns_exactly_the_closed_field_set(http_client: TestClient) -> None:
    response = http_client.get("/runs/run-1/status")
    assert response.status_code == 200
    assert set(response.json().keys()) == set(RunStatusView.model_fields)


def test_get_status_404s_for_an_unknown_run(http_client: TestClient) -> None:
    response = http_client.get("/runs/does-not-exist/status")
    assert response.status_code == 404


def test_pause_resume_stop_round_trip_over_http(http_client: TestClient) -> None:
    assert http_client.post("/runs/run-1/pause").status_code == 200
    assert http_client.post("/runs/run-1/resume").status_code == 200
    assert http_client.post("/runs/run-1/stop").status_code == 200


def test_pause_maps_a_harness_error_to_409(http_client: TestClient) -> None:
    response = http_client.post("/runs/does-not-exist/pause")
    assert response.status_code == 409


def test_start_run_returns_201_with_a_run_id(http_client: TestClient) -> None:
    response = http_client.post("/runs", json={"config_path": "config.yaml"})
    assert response.status_code == 201
    assert response.json()["run_id"] == "run-started"


def test_health_serves_the_injected_doctor_report(http_client: TestClient) -> None:
    response = http_client.get("/health")
    assert response.status_code == 200
    assert response.json()["tuner"]["status"] == "ok"


def test_health_503s_when_no_doctor_provider_is_configured() -> None:
    runner = FakeRunner()
    store = OrderLoggingStore([])
    app = api.create_app(runner=runner, store=store, doctor_provider=None)
    response = TestClient(app).get("/health")
    assert response.status_code == 503


def test_run_server_has_no_host_parameter() -> None:
    """Structural guarantee: there is no way to call `run_server` with a
    non-loopback host, because the parameter does not exist.
    """
    import inspect

    assert "host" not in inspect.signature(api.run_server).parameters


def test_run_server_always_binds_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_uvicorn_run(app: Any, *, host: str, port: int) -> None:
        captured["host"] = host
        captured["port"] = port

    import uvicorn

    monkeypatch.setattr(uvicorn, "run", fake_uvicorn_run)

    runner = FakeRunner()
    store = OrderLoggingStore([])
    api.run_server(runner=runner, store=store, port=9999)

    assert captured == {"host": api.LOOPBACK_HOST, "port": 9999}
    assert api.LOOPBACK_HOST == "127.0.0.1"


# --------------------------------------------------------------------------
# 5. doctor (T121)
# --------------------------------------------------------------------------


class _FakeHost:
    """A minimal `HostPlatform` double: only the two methods `doctor` calls."""

    def __init__(self, *, pid: int | None = None, free_bytes: int = 10 * 1024**3) -> None:
        self._pid = pid
        self._free_bytes = free_bytes

    def locate_game_process(self) -> Any:
        if self._pid is None:
            return None
        from civsim_harness.host.port import GameProcess

        return GameProcess(pid=self._pid, name="CivilizationVI")

    def free_disk_space(self, path: Path) -> Any:
        from civsim_harness.host.port import DiskSpace

        return DiskSpace(path=path, free_bytes=self._free_bytes, total_bytes=100 * 1024**3)


def _host_info(**overrides: Any) -> HostInfo:
    defaults: dict[str, Any] = {
        "os": OperatingSystem.windows,
        "os_version": "10.0",
        "session_type": None,
    }
    defaults.update(overrides)
    return HostInfo(**defaults)


class _RaisingNexusClient:
    """A `NexusClient` stand-in whose `connect()` fails as if nothing is
    listening -- exactly what `doctor` must report as `unreachable`, not
    crash on.
    """

    async def connect(self) -> Any:
        raise PreflightError("no tuner listening", detail={"reason": "refused"})

    async def close(self) -> None:
        return None


class _MainMenuNexusClient:
    """`connect()` succeeds (main menu); `resolve_game_states()` raises, per
    the verified-against-a-real-client behaviour this task's brief documents.
    """

    async def connect(self) -> Any:
        return None

    async def resolve_game_states(self) -> Any:
        raise PreflightError("no game loaded", detail={"reason": "game_states_unavailable"})

    async def close(self) -> None:
        return None


class _InGameNexusClient:
    def __init__(self) -> None:
        from civsim_harness.nexus.client import StateIndices

        self._indices = StateIndices(
            by_name={"GameCore_Tuner": 2, "InGame": 5}, game_core_tuner=2, in_game=5
        )

    async def connect(self) -> Any:
        return self._indices

    async def resolve_game_states(self) -> Any:
        return self._indices

    async def close(self) -> None:
        return None


class _FakeStoreHealth:
    def __init__(self, ok: bool, detail: str | None = None) -> None:
        self.ok = ok
        self.detail = detail


class _FakeDoctorStore:
    def __init__(self, ok: bool = True, detail: str | None = None) -> None:
        self._health = _FakeStoreHealth(ok, detail)

    def ping(self) -> Any:
        return self._health


@pytest.mark.parametrize(
    "client_factory,expected_status",
    [
        (_RaisingNexusClient, "unreachable"),
        (_MainMenuNexusClient, "no_game_loaded"),
        (_InGameNexusClient, "ok"),
    ],
)
async def test_run_doctor_tuner_states(
    client_factory: Callable[[], Any], expected_status: str
) -> None:
    report = await doctor.run_doctor(
        host=_FakeHost(),
        host_info=_host_info(),
        nexus_client_factory=client_factory,
        store=_FakeDoctorStore(),
        catalog_root=REPO_CATALOG_ROOT,
    )
    assert report.tuner.status == expected_status


async def test_run_doctor_client_not_running_vs_running() -> None:
    not_running = await doctor.run_doctor(
        host=_FakeHost(pid=None),
        host_info=_host_info(),
        nexus_client_factory=_RaisingNexusClient,
        store=_FakeDoctorStore(),
        catalog_root=REPO_CATALOG_ROOT,
    )
    assert not_running.client.status == "not_running"

    running = await doctor.run_doctor(
        host=_FakeHost(pid=4242),
        host_info=_host_info(),
        nexus_client_factory=_RaisingNexusClient,
        store=_FakeDoctorStore(),
        catalog_root=REPO_CATALOG_ROOT,
        build_reader=lambda: "win/1.2.3",
    )
    assert running.client.status == "ok"
    assert running.client.pid == 4242
    assert running.client.build == "win/1.2.3"


async def test_run_doctor_store_unreachable_is_reported_not_raised() -> None:
    report = await doctor.run_doctor(
        host=_FakeHost(),
        host_info=_host_info(),
        nexus_client_factory=_RaisingNexusClient,
        store=_FakeDoctorStore(ok=False, detail="disk full"),
        catalog_root=REPO_CATALOG_ROOT,
    )
    assert report.store.status == "unreachable"
    assert report.store.detail == "disk full"


async def test_run_doctor_catalog_reports_real_repo_catalog() -> None:
    report = await doctor.run_doctor(
        host=_FakeHost(),
        host_info=_host_info(),
        nexus_client_factory=_RaisingNexusClient,
        store=_FakeDoctorStore(),
        catalog_root=REPO_CATALOG_ROOT,
    )
    assert report.catalog.status == "ok"
    assert report.catalog.undeclared_count == 0
    assert report.catalog.declaration_count and report.catalog.declaration_count > 0


async def test_run_doctor_catalog_error_is_reported_not_raised(tmp_path: Path) -> None:
    report = await doctor.run_doctor(
        host=_FakeHost(),
        host_info=_host_info(),
        nexus_client_factory=_RaisingNexusClient,
        store=_FakeDoctorStore(),
        catalog_root=tmp_path / "no-such-catalog",
    )
    assert report.catalog.status == "error"


async def test_run_doctor_capture_path_reflects_support_tier() -> None:
    validated = await doctor.run_doctor(
        host=_FakeHost(),
        host_info=_host_info(),
        nexus_client_factory=_RaisingNexusClient,
        store=_FakeDoctorStore(),
        catalog_root=REPO_CATALOG_ROOT,
        support_probe=SupportProbeResult(
            quicksave_path_verified=True, capture_hygiene_spike_passed=True, reason="spike passed"
        ),
        capture_path_name="windows_graphics_capture",
    )
    assert validated.capture_path.status == "ok"
    assert validated.capture_path.hygiene_spike == "passed"

    degraded = await doctor.run_doctor(
        host=_FakeHost(),
        host_info=_host_info(),
        nexus_client_factory=_RaisingNexusClient,
        store=_FakeDoctorStore(),
        catalog_root=REPO_CATALOG_ROOT,
        support_probe=UNPROBED,
    )
    assert degraded.capture_path.status == "none"
    assert degraded.capture_path.hygiene_spike == "not_passed"


async def test_run_doctor_provider_key_presence_only(tmp_path: Path) -> None:
    # The absent case pins the file source to a path that cannot exist: with
    # no override the resolver falls back to Path.cwd()/secrets.yaml, so a
    # real operator key in the repo root would satisfy "present" here.
    absent = await doctor.run_doctor(
        host=_FakeHost(),
        host_info=_host_info(),
        nexus_client_factory=_RaisingNexusClient,
        store=_FakeDoctorStore(),
        catalog_root=REPO_CATALOG_ROOT,
        env={},
        secrets_file=tmp_path / "absent.yaml",
    )
    assert absent.provider_key.present is False

    present = await doctor.run_doctor(
        host=_FakeHost(),
        host_info=_host_info(),
        nexus_client_factory=_RaisingNexusClient,
        store=_FakeDoctorStore(),
        catalog_root=REPO_CATALOG_ROOT,
        env={"OPENROUTER_API_KEY": "sk-super-secret-value"},
        secrets_file=tmp_path / "absent.yaml",
    )
    assert present.provider_key.present is True
    # Presence only -- the value itself must never appear anywhere on the report.
    assert "sk-super-secret-value" not in present.model_dump_json()


async def test_run_doctor_sees_a_key_that_lives_only_in_the_secrets_file(
    tmp_path: Path,
) -> None:
    """The live regression: doctor said MISSING on a host whose key lives only
    in secrets.yaml, while every production provider call authenticated fine --
    doctor must report presence through the same resolution the adapter uses.
    """
    secrets_file = tmp_path / "secrets.yaml"
    secrets_file.write_text(
        "openrouter_api_key: sk-file-only-secret-value\n", encoding="utf-8"
    )
    report = await doctor.run_doctor(
        host=_FakeHost(),
        host_info=_host_info(),
        nexus_client_factory=_RaisingNexusClient,
        store=_FakeDoctorStore(),
        catalog_root=REPO_CATALOG_ROOT,
        env={},
        secrets_file=secrets_file,
    )
    assert report.provider_key.present is True
    assert "sk-file-only-secret-value" not in report.model_dump_json()


async def test_run_doctor_linux_wayland_platform_line() -> None:
    report = await doctor.run_doctor(
        host=_FakeHost(),
        host_info=_host_info(os=OperatingSystem.linux, session_type=LinuxSessionType.wayland),
        nexus_client_factory=_RaisingNexusClient,
        store=_FakeDoctorStore(),
        catalog_root=REPO_CATALOG_ROOT,
    )
    assert report.platform.os == "linux"
    assert report.platform.session_type == "wayland"


def test_format_doctor_report_renders_every_section() -> None:
    text = doctor.format_doctor_report(_fake_doctor_report())
    for label in (
        "platform",
        "tuner connection",
        "client",
        "store",
        "catalog",
        "capture path",
        "provider key",
        "disk headroom",
    ):
        assert label in text
    assert "present" in text


# --------------------------------------------------------------------------
# 6. CLI (T118)
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_cli_wiring() -> Any:
    """Every CLI test starts from a clean slate: no runner/store factory
    configured, matching what a fresh process would see.
    """
    cli.configure_runner_factory(None)
    cli.configure_store_factory(None)
    yield
    cli.configure_runner_factory(None)
    cli.configure_store_factory(None)


@pytest.fixture
def cli_runner() -> CliRunner:
    return CliRunner()


def test_civsim_help_lists_run_doctor_and_audit(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    assert "run" in result.output
    assert "doctor" in result.output
    assert "audit" in result.output


def test_run_status_without_a_configured_runner_fails_clearly(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(cli.app, ["run", "status", "run-1"])
    assert result.exit_code == 1
    assert "no runner is configured" in result.output


def test_run_start_pause_resume_stop_status_with_a_fake_runner(
    cli_runner: CliRunner, tmp_path: Path
) -> None:
    runner = FakeRunner()
    cli.configure_runner_factory(lambda: runner)
    cli.configure_store_factory(lambda path: OrderLoggingStore([]))

    config_path = tmp_path / "config.yaml"
    config_path.write_text("placeholder: true\n", encoding="utf-8")

    start_result = cli_runner.invoke(cli.app, ["run", "start", str(config_path)])
    assert start_result.exit_code == 0
    assert "run-started" in start_result.output

    pause_result = cli_runner.invoke(cli.app, ["run", "pause", "run-started"])
    assert pause_result.exit_code == 0
    assert "requested_state" in pause_result.output

    resume_result = cli_runner.invoke(cli.app, ["run", "resume", "run-started"])
    assert resume_result.exit_code == 0

    stop_result = cli_runner.invoke(cli.app, ["run", "stop", "run-started"])
    assert stop_result.exit_code == 0

    status_result = cli_runner.invoke(cli.app, ["run", "status", "run-started"])
    assert status_result.exit_code == 0
    assert "lifecycle_state" in status_result.output


def test_run_start_exits_nonzero_when_the_run_lands_in_failed_state(
    cli_runner: CliRunner, tmp_path: Path
) -> None:
    class _FailingRunner(FakeRunner):
        def start(self, config_path: Path) -> RunId:
            run_id = RunId("run-failed")
            self.statuses[run_id] = _status(run_id, LifecycleState.FAILED)
            return run_id

    cli.configure_runner_factory(_FailingRunner)
    config_path = tmp_path / "config.yaml"
    config_path.write_text("placeholder: true\n", encoding="utf-8")

    result = cli_runner.invoke(cli.app, ["run", "start", str(config_path)])
    assert result.exit_code == 1
    assert "failed" in result.output.lower()


def test_doctor_command_runs_cleanly_end_to_end(cli_runner: CliRunner, tmp_path: Path) -> None:
    cli.configure_store_factory(lambda path: SqliteMatchStore(path))
    result = cli_runner.invoke(
        cli.app,
        [
            "doctor",
            "--catalog-root",
            str(REPO_CATALOG_ROOT),
            "--store-path",
            str(tmp_path / "match.db"),
        ],
    )
    assert result.exit_code == 0
    for label in ("platform", "tuner connection", "client", "store", "catalog", "disk headroom"):
        assert label in result.output


def test_audit_cli_reports_insufficient_data_with_exit_code_two(
    cli_runner: CliRunner, tmp_path: Path
) -> None:
    store_path = tmp_path / "match.db"
    cli.configure_store_factory(lambda path: SqliteMatchStore(path))

    result = cli_runner.invoke(
        cli.app,
        [
            "audit",
            "steps",
            "no-such-run",
            "--store-path",
            str(store_path),
        ],
    )
    assert result.exit_code == 2
    assert "insufficient_data" in result.output


def test_audit_parity_cli_passes_against_a_well_formed_run(
    cli_runner: CliRunner, tmp_path: Path
) -> None:
    from civsim_harness.models.config import RunConfiguration
    from civsim_harness.models.decision import Decision
    from civsim_harness.models.records import ModelCall, SavePoint
    from civsim_harness.models.run import Run
    from civsim_harness.models.turn import DecisionStep, Observation, TurnCycle
    from civsim_harness.store.port import DecisionStepBundle, TurnCycleRecord

    now = "2026-01-01T00:00:00Z"
    store_path = tmp_path / "match.db"
    store = SqliteMatchStore(store_path)
    try:
        store.create_run(
            Run.model_validate(
                {
                    "run_id": "run-cli",
                    "config_id": "cfg-cli",
                    "lifecycle_state": "playing",
                    "record_completeness_status": "unknown",
                    "comparability_status": "comparable",
                    "observation_catalog_version": {"version": "v", "content_hash": "h"},
                    "action_catalog_version": {"version": "v", "content_hash": "h"},
                    "game_build": "win/1.0.0",
                    "host_support_tier": "validated",
                    "capture_path": "windows_graphics_capture",
                }
            ),
            RunConfiguration.model_validate(
                {
                    "config_id": "cfg-cli",
                    "map_seed": "1",
                    "civilization": "CIVILIZATION_ROME",
                    "leader": "LEADER_TRAJAN",
                    "ruleset": "RULESET_STANDARD",
                    "difficulty": "DIFFICULTY_PRINCE",
                    "stop_condition": {"type": "turn_reached", "turn": 1},
                    "model_config": {"primary": {"provider": "openrouter", "model": "x"}},
                    "no_progress_step_limit": 8,
                    "recovery_attempt_limit": 3,
                    "min_free_disk_gb": 1,
                    "created_at": now,
                }
            ),
        )
        store.write_save_point(
            SavePoint.model_validate(
                {
                    "save_point_id": "sp-1",
                    "run_id": "run-cli",
                    "turn_number": 1,
                    "save_name": "civsim__run-cli__t0001",
                    "taken_at": now,
                    "verified": True,
                    "retention_status": "retained",
                }
            )
        )
        step = DecisionStep.model_validate(
            {
                "decision_step_id": "step-1",
                "turn_cycle_id": "tc-1",
                "step_index": 1,
                "observation_id": "obs-1",
                "decision_id": "dec-1",
                "model_call_id": "call-1",
                "progress": "changed_state",
                "no_progress_streak_after": 0,
                "visually_degraded": False,
                "started_at": now,
                "ended_at": now,
            }
        )
        observation = Observation.model_validate(
            {
                "observation_id": "obs-1",
                "decision_step_id": "step-1",
                "assembled_at": now,
                "catalog_version": {"version": "v", "content_hash": "h"},
                "entries": [
                    {
                        "declaration_id": "map.state",
                        "key": "k",
                        "value": 1,
                        "context": "GameCore_Tuner",
                    }
                ],
                "captures": [],
                "screen_identity": "world",
            }
        )
        decision = Decision.model_validate(
            {
                "decision_id": "dec-1",
                "decision_step_id": "step-1",
                "action_declaration_id": "units.move_to",
                "reasoning": "r",
                "trigger": "proactive",
                "model_call_id": "call-1",
                "execution": {
                    "outcome": "applied",
                    # FR-011: an applied execution must carry the predicate verdict that
                    # confirmed it; an empty verification means nothing re-read the board.
                    "verification": {"declaration_id": "units.move_to", "result": True},
                    "verified_at": now,
                },
            }
        )
        model_call = ModelCall.model_validate(
            {
                "model_call_id": "call-1",
                "run_id": "run-cli",
                "turn_cycle_id": "tc-1",
                "decision_step_id": "step-1",
                "model_requested": {"provider": "openrouter", "model": "x"},
                "model_served": {"provider": "openrouter", "model": "x"},
                "latency_ms": 1,
                "cost": {},
                "retry_count": 0,
                "fallback_occurred": False,
                "image_count": 0,
                "outcome": "decision_returned",
            }
        )
        turn_cycle = TurnCycle.model_validate(
            {
                "turn_cycle_id": "tc-1",
                "run_id": "run-cli",
                "turn_number": 1,
                "attempt_index": 0,
                "is_authoritative": True,
                "save_point_id": "sp-1",
                "step_count": 1,
                "outcome": "ended_by_agent",
                "final_no_progress_streak": 0,
                "visually_degraded": False,
                "started_at": now,
                "ended_at": now,
                "persisted_at": now,
            }
        )
        bundle = DecisionStepBundle(
            step=step, observation=observation, decision=decision, model_call=model_call
        )
        store.write_turn_cycle(TurnCycleRecord(turn_cycle=turn_cycle, steps=[bundle]))
    finally:
        store.close()

    cli.configure_store_factory(lambda path: SqliteMatchStore(path))
    result = cli_runner.invoke(
        cli.app,
        [
            "audit",
            "parity",
            "run-cli",
            "--catalog-root",
            str(REPO_CATALOG_ROOT),
            "--store-path",
            str(store_path),
        ],
    )
    assert result.exit_code == 0
    assert "passed" in result.output


# --------------------------------------------------------------------------
# `doctor` against a tuner that is actually listening
# --------------------------------------------------------------------------


class _HandshakeTimesOutNexusClient:
    """Something *is* listening on the tuner port, but the handshake never completes.

    The whole family of failures this stands for -- a handshake that times out, a socket that
    drops mid-handshake, a state table that comes back malformed -- raises `NexusError`, not
    `PreflightError`, and **every one of them is only reachable when a client is actually
    running**. That is why this path had no coverage: until a live client existed, nothing could
    reach it.
    """

    async def connect(self) -> Any:
        raise NexusError(
            "Nexus handshake exceeded its timeout",
            detail={"reason": "timeout", "timeout_s": 5.0},
        )

    async def close(self) -> None:
        return None


class _StateQueryFailsNexusClient:
    """`connect()` succeeds, then the state query fails at the transport level.

    Distinct from `_MainMenuNexusClient`, whose `resolve_game_states()` raises `PreflightError`
    to mean the honest, expected "reachable, no game loaded". This one is the transport failing,
    which is not a diagnosis -- it is a crash unless reported.
    """

    async def connect(self) -> Any:
        return None

    async def resolve_game_states(self) -> Any:
        raise NexusError("socket closed during LSQ", detail={"reason": "connection_lost"})

    async def close(self) -> None:
        return None


class _UncloseableNexusClient:
    """Connects and resolves fine, but will not shut its socket down cleanly."""

    async def connect(self) -> Any:
        return None

    async def resolve_game_states(self) -> Any:
        from civsim_harness.nexus.client import StateIndices

        return StateIndices(
            by_name={"GameCore_Tuner": 7, "InGame": 8}, game_core_tuner=7, in_game=8
        )

    async def close(self) -> None:
        raise OSError("socket refused to close")


@pytest.mark.parametrize(
    ("client_factory", "expected_detail_fragment"),
    [
        (_HandshakeTimesOutNexusClient, "handshake"),
        (_StateQueryFailsNexusClient, "socket closed"),
    ],
)
async def test_a_tuner_that_answers_but_fails_is_reported_not_raised(
    client_factory: Callable[[], Any], expected_detail_fragment: str
) -> None:
    """`doctor`'s own contract is "nothing here ever raises out of `run_doctor`".

    `_probe_tuner` caught only `PreflightError`, which covers the two states reachable with **no**
    client running -- a refused connection, and a reachable tuner with no game loaded. Every
    transport-level failure raises `NexusError` and escaped, so `civsim doctor` -- the first
    command anyone runs against a live client -- would crash on exactly the host it exists to
    diagnose, and would do so intermittently, depending on whether the client happened to be up.
    """
    report = await doctor.run_doctor(
        host=_FakeHost(),
        host_info=_host_info(),
        nexus_client_factory=client_factory,
        store=_FakeDoctorStore(),
        catalog_root=REPO_CATALOG_ROOT,
    )
    assert report.tuner.status == "unreachable"
    assert report.tuner.detail is not None
    assert expected_detail_fragment in report.tuner.detail
    # The rest of the report still arrives -- one failed section never costs the others.
    assert report.store.status == "ok"
    assert report.catalog.status == "ok"


async def test_a_socket_that_will_not_close_does_not_discard_the_diagnosis() -> None:
    """The close happens after everything useful has been gathered; it must not replace it."""
    report = await doctor.run_doctor(
        host=_FakeHost(),
        host_info=_host_info(),
        nexus_client_factory=_UncloseableNexusClient,
        store=_FakeDoctorStore(),
        catalog_root=REPO_CATALOG_ROOT,
    )
    assert report.tuner.status == "ok"
    assert report.tuner.game_core_tuner_index == 7
    assert report.tuner.in_game_index == 8


async def test_a_build_read_that_fails_still_reports_the_client_as_running() -> None:
    """`build_reader` only runs when a process was actually located, so this line had never
    executed on a host with no client. It reads a version off disk and can fail for ordinary
    reasons -- a permission-denied read, a path the adapter resolved differently, a missing
    optional dependency -- none of which mean "no client is running", which is the only question
    this probe is being asked.
    """

    def _explode() -> str | None:
        raise OSError("permission denied reading the game executable")

    report = await doctor.run_doctor(
        host=_FakeHost(pid=4242),
        host_info=_host_info(),
        nexus_client_factory=_RaisingNexusClient,
        store=_FakeDoctorStore(),
        catalog_root=REPO_CATALOG_ROOT,
        build_reader=_explode,
    )
    assert report.client.status == "ok"
    assert report.client.pid == 4242
    assert report.client.build is None
