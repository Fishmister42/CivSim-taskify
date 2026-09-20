"""Contract tests for `contracts/run-configuration.md` (T063).

Asserts V1, V3, V7, V8, V9, V12, and V13 against the already-implemented preflight checks:

- **V1** -- a partially specified run does not start (`config/run_config.py`).
- **V3** -- a `seed_set` mismatch is an error, never an override (`config/seed_set.py`).
- **V7** -- the store answers `ping()`; an unreachable store is rejected
  (`store/sqlite_adapter.py`).
- **V8** -- no run is already active on this client or this run identity (`run/identity_lock.py`).
- **V9** -- a credential-shaped value anywhere in the configuration is a validation error, never a
  warning (`config/run_config.py`).
- **V12** -- `no_progress_step_limit` >= 1, with no silent upper bound (`config/run_config.py`,
  `models/config.py`).
- **V13** -- an `UNSUPPORTED` host is rejected before turn 1 with the missing capability recorded,
  while a merely degraded (`SUPPORTED`) host starts and is marked instead (`observe/host_gate.py`),
  exercised through `tests/fakes/fake_host.py`'s `FakeHostPlatform`.

Uses `configs/turn50-validation.yaml` / `configs/seedsets/shuffle-classic-2026q3.yaml` (the repo's
own worked examples for this contract) as the known-good baseline every negative case mutates.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from civsim_harness.config.run_config import load_run_configuration_yaml
from civsim_harness.config.seed_set import check_seed_set_agreement, load_seed_set_yaml
from civsim_harness.errors import PreflightError
from civsim_harness.host.detect import SupportTier
from civsim_harness.models.common import RunId
from civsim_harness.models.run import ComparabilityStatus, HostSupportTier
from civsim_harness.observe.host_gate import evaluate_host_gate
from civsim_harness.run.identity_lock import RunIdentityLock, RunIdentityLockError
from civsim_harness.store.sqlite_adapter import SqliteMatchStore
from fakes.fake_host import FakeHostPlatform

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO_ROOT / "configs" / "turn50-validation.yaml"
SEED_SET_PATH = REPO_ROOT / "configs" / "seedsets" / "shuffle-classic-2026q3.yaml"

NOW = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)


def _valid_config_dict() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


def _load(raw: dict) -> object:
    return load_run_configuration_yaml(yaml.safe_dump(raw))


# --------------------------------------------------------------------------
# V1 -- every required field present and non-null; a partial run does not start
# --------------------------------------------------------------------------


def test_v1_the_worked_example_itself_loads_cleanly() -> None:
    """Sanity anchor: the known-good baseline every other V1 case below mutates must itself
    load, or every negative case in this section is meaningless."""
    config = load_run_configuration_yaml(CONFIG_PATH.read_text(encoding="utf-8"))
    assert config.civilization == "CIVILIZATION_ROME"
    assert config.no_progress_step_limit == 8


@pytest.mark.parametrize(
    "missing_field",
    [
        "map_seed",
        "civilization",
        "leader",
        "ruleset",
        "difficulty",
        "stop_condition",
        "model_config",
        "no_progress_step_limit",
        "recovery_attempt_limit",
        "min_free_disk_gb",
    ],
)
def test_v1_a_missing_required_field_does_not_start_the_run(missing_field: str) -> None:
    raw = _valid_config_dict()
    del raw[missing_field]
    with pytest.raises(PreflightError):
        _load(raw)


@pytest.mark.parametrize(
    "nulled_field",
    ["map_seed", "civilization", "leader", "ruleset", "difficulty", "stop_condition"],
)
def test_v1_a_null_required_field_does_not_start_the_run(nulled_field: str) -> None:
    """"Present and non-null" (V1) -- present-but-null is exactly as disqualifying as absent."""
    raw = _valid_config_dict()
    raw[nulled_field] = None
    with pytest.raises(PreflightError):
        _load(raw)


# --------------------------------------------------------------------------
# V3 -- a seed_set mismatch is an error, never an override
# --------------------------------------------------------------------------


def test_v3_seed_set_mismatch_is_rejected_not_silently_overridden() -> None:
    config = load_run_configuration_yaml(CONFIG_PATH.read_text(encoding="utf-8"))
    seed_set = load_seed_set_yaml(SEED_SET_PATH.read_text(encoding="utf-8"))

    disagreeing = config.model_copy(update={"civilization": "CIVILIZATION_GREECE"})

    with pytest.raises(PreflightError) as excinfo:
        check_seed_set_agreement(disagreeing, seed_set)

    mismatched_fields = {m["field"] for m in excinfo.value.detail["mismatches"]}
    assert "civilization" in mismatched_fields
    # Never an override: the seed set's own recorded value is untouched by the attempt.
    assert seed_set.civilization == "CIVILIZATION_ROME"


def test_v3_agreeing_configuration_passes_cleanly() -> None:
    config = load_run_configuration_yaml(CONFIG_PATH.read_text(encoding="utf-8"))
    seed_set = load_seed_set_yaml(SEED_SET_PATH.read_text(encoding="utf-8"))
    check_seed_set_agreement(config, seed_set)  # must not raise


def test_v3_mod_set_mismatch_names_mod_set_not_a_different_field() -> None:
    config = load_run_configuration_yaml(CONFIG_PATH.read_text(encoding="utf-8"))
    seed_set = load_seed_set_yaml(SEED_SET_PATH.read_text(encoding="utf-8"))
    disagreeing = config.model_copy(update={"mod_set": []})

    with pytest.raises(PreflightError) as excinfo:
        check_seed_set_agreement(disagreeing, seed_set)

    mismatched_fields = {m["field"] for m in excinfo.value.detail["mismatches"]}
    assert mismatched_fields == {"mod_set"}


# --------------------------------------------------------------------------
# V7 -- the store answers ping(); an unreachable store is rejected
# --------------------------------------------------------------------------


def test_v7_a_reachable_store_answers_ping_ok(tmp_path: Path) -> None:
    store = SqliteMatchStore(tmp_path / "match.db")
    try:
        health = store.ping()
        assert health.ok is True
    finally:
        store.close()


def test_v7_an_unreachable_store_is_rejected(tmp_path: Path) -> None:
    """V7: a run must not start against a store that cannot answer ping() -- this is the exact
    check a preflight gate performs before allowing a run to begin (contracts/run-configuration.md,
    FR-051, edge case: store unreachable)."""
    store = SqliteMatchStore(tmp_path / "match.db")
    store.close()  # the store is now unreachable

    health = store.ping()

    assert health.ok is False
    assert health.detail  # a legible reason, not a bare failure flag


# --------------------------------------------------------------------------
# V8 -- no run already active on this client or this run identity
# --------------------------------------------------------------------------


def test_v8_a_second_run_on_the_same_client_is_rejected(tmp_path: Path) -> None:
    lock = RunIdentityLock(tmp_path / "locks")
    client_pid = os.getpid()  # guaranteed alive for the duration of this test

    lock.acquire(run_id=RunId("run_alpha"), client_pid=client_pid, now=NOW)

    with pytest.raises(RunIdentityLockError):
        lock.acquire(run_id=RunId("run_beta"), client_pid=client_pid, now=NOW)


def test_v8_a_second_run_on_the_same_run_identity_is_rejected(tmp_path: Path) -> None:
    lock = RunIdentityLock(tmp_path / "locks")
    first_client_pid = os.getpid()
    second_client_pid = os.getppid()  # a distinct, also-guaranteed-alive pid

    lock.acquire(run_id=RunId("run_alpha"), client_pid=first_client_pid, now=NOW)

    with pytest.raises(RunIdentityLockError):
        lock.acquire(run_id=RunId("run_alpha"), client_pid=second_client_pid, now=NOW)


def test_v8_a_released_run_identity_may_be_reacquired(tmp_path: Path) -> None:
    lock = RunIdentityLock(tmp_path / "locks")
    client_pid = os.getpid()

    lock.acquire(run_id=RunId("run_alpha"), client_pid=client_pid, now=NOW)
    lock.release(RunId("run_alpha"))

    lock.acquire(run_id=RunId("run_alpha"), client_pid=client_pid, now=NOW)  # must not raise


# --------------------------------------------------------------------------
# V9 -- no credentials; a key-shaped value anywhere is a validation error, never a warning
# --------------------------------------------------------------------------


def test_v9_a_credential_shaped_value_under_request_params_is_rejected() -> None:
    raw = _valid_config_dict()
    raw["model_config"]["request_params"]["api_key"] = "sk-or-v1-abcdef0123456789abcdef"
    with pytest.raises(PreflightError):
        _load(raw)


def test_v9_a_credential_shaped_value_anywhere_in_the_document_is_rejected() -> None:
    """V9 governs the whole document, not just `model_config.request_params` -- planted under an
    unrelated top-level section (`opponents`), per `config/run_config.py`'s own docstring."""
    raw = _valid_config_dict()
    raw["opponents"]["api_key"] = "sk-or-v1-abcdef0123456789abcdef"
    with pytest.raises(PreflightError):
        _load(raw)


def test_v9_credential_rejection_is_an_error_not_a_stripped_warning() -> None:
    """The whole load must fail -- a caller must never get back a "cleaned" RunConfiguration with
    the credential silently removed, which would be indistinguishable from a warning."""
    raw = _valid_config_dict()
    raw["opponents"]["password"] = "hunter2hunter2hunter2"
    with pytest.raises(PreflightError):
        _load(raw)


def test_v9_an_ordinary_configuration_with_no_secret_loads_cleanly() -> None:
    load_run_configuration_yaml(CONFIG_PATH.read_text(encoding="utf-8"))  # must not raise


# --------------------------------------------------------------------------
# V12 -- no_progress_step_limit >= 1, with no silent upper bound
# --------------------------------------------------------------------------


def test_v12_zero_no_progress_step_limit_is_rejected() -> None:
    raw = _valid_config_dict()
    raw["no_progress_step_limit"] = 0
    with pytest.raises(PreflightError):
        _load(raw)


def test_v12_negative_no_progress_step_limit_is_rejected() -> None:
    raw = _valid_config_dict()
    raw["no_progress_step_limit"] = -1
    with pytest.raises(PreflightError):
        _load(raw)


def test_v12_one_is_the_minimum_legal_value() -> None:
    raw = _valid_config_dict()
    raw["no_progress_step_limit"] = 1
    config = _load(raw)
    assert config.no_progress_step_limit == 1


def test_v12_there_is_no_silent_upper_bound() -> None:
    """A very large value must be accepted verbatim -- no default cap, silent or otherwise."""
    raw = _valid_config_dict()
    raw["no_progress_step_limit"] = 1_000_000
    config = _load(raw)
    assert config.no_progress_step_limit == 1_000_000


# --------------------------------------------------------------------------
# V13 -- an UNSUPPORTED host is rejected before turn 1, naming the missing capability;
# a merely degraded (SUPPORTED) host starts and is marked instead
# --------------------------------------------------------------------------


def test_v13_an_unsupported_host_is_rejected_before_turn_1_with_missing_capability_named() -> None:
    fake_host = FakeHostPlatform(tier=SupportTier.unsupported)

    with pytest.raises(PreflightError) as excinfo:
        evaluate_host_gate(fake_host.probe_result)

    assert excinfo.value.detail["missing_capability"] == "quicksave_path_verified"
    assert excinfo.value.detail["reason"]


def test_v13_a_degraded_but_supported_host_starts_and_is_marked_visually_degraded() -> None:
    fake_host = FakeHostPlatform(tier=SupportTier.supported)

    result = evaluate_host_gate(fake_host.probe_result)  # must not raise

    assert result.tier is HostSupportTier.SUPPORTED
    assert result.comparability_status is ComparabilityStatus.VISUALLY_DEGRADED


def test_v13_a_fully_validated_host_starts_fully_comparable() -> None:
    fake_host = FakeHostPlatform(tier=SupportTier.validated)

    result = evaluate_host_gate(fake_host.probe_result)  # must not raise

    assert result.tier is HostSupportTier.VALIDATED
    assert result.comparability_status is ComparabilityStatus.COMPARABLE
