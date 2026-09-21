"""Contract tests for `contracts/run-configuration.md` (T063; V11 added by T243).

Asserts V1, V3, V7, V8, V9, V11, V12, and V13 against the already-implemented preflight checks:

- **V1** -- a partially specified run does not start (`config/run_config.py`).
- **V3** -- a `seed_set` mismatch is an error, never an override (`config/seed_set.py`).
- **V7** -- the store answers `ping()`; an unreachable store is rejected
  (`store/sqlite_adapter.py`).
- **V8** -- no run is already active on this client or this run identity (`run/identity_lock.py`).
- **V9** -- a credential-shaped value anywhere in the configuration is a validation error, never a
  warning (`config/run_config.py`).
- **V11** -- free disk space at or above `min_free_disk_gb` AND the run's estimated save/capture
  footprint fits within it (`saves/headroom.py`'s `estimate_footprint` + `check_headroom`, called
  at preflight by `run/composition.py`'s `_prepare_run` -- T243; before that, `estimate_footprint`
  had no caller in `src/` and V11 was asserted nowhere).
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
CIVSIM_DEFAULT_SEED_SET_PATH = REPO_ROOT / "configs" / "seedsets" / "civsim-default.yaml"

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
# civsim-default.yaml -- the pinned CivSim DEFAULT seed set (CIVILIZATION_PERSIA /
# LEADER_CYRUS), read back live on the Linux validation host
# (specs/002-civ-playing-harness/spikes/civsim-default-preset-linux.md,
# preset_readback.txt). Loads/validates like any other seed set (V1-shaped), and its 22-mod pin
# is exercised against V3 exactly like shuffle-classic-2026q3.yaml's mod_set is above.
# --------------------------------------------------------------------------


def test_civsim_default_seed_set_loads_and_validates() -> None:
    seed_set = load_seed_set_yaml(CIVSIM_DEFAULT_SEED_SET_PATH.read_text(encoding="utf-8"))

    assert seed_set.name == "civsim-default"
    assert seed_set.civilization == "CIVILIZATION_PERSIA"
    assert seed_set.leader == "LEADER_CYRUS"
    assert seed_set.ruleset == "RULESET_EXPANSION_2"
    # The validation host is Linux, not Windows -- unlike shuffle-classic-2026q3.yaml's
    # `win/...` worked example (R20: platform is part of the composite identity).
    assert seed_set.game_build == "linux/1.0.12.9"
    assert len(seed_set.seeds) >= 1
    assert seed_set.accepted_build_changes == []
    assert seed_set.is_uniform is True


def test_civsim_default_mod_set_has_22_entries_pinned_on_id_and_version() -> None:
    """22 mods, not empty -- an empty list would be a false pin against this host, which
    genuinely loads with 22 mods active (see the seed set file's own comments). Pinned on
    `id`/`version` only: `ModRef` carries no title field at all, so there is no way for the
    known-broken bracket-matched title parsing (one live title is literally
    `[ENDCOLOR]TopPanel Extension [COLOR:ResGoldLabelCS]Pro[ENDCOLOR]`) to have leaked in here.

    T251 (measured live, `spikes/t251-mod-set-identity-linux.md`): only the three workshop mods
    report a version through `Modding.GetModProperty(handle, "Version")`; every official pack
    answers nil, so those are pinned on id only (`version: None`). The earlier `"v1"` on all 22
    was a placeholder that could never have matched a live read-back."""
    seed_set = load_seed_set_yaml(CIVSIM_DEFAULT_SEED_SET_PATH.read_text(encoding="utf-8"))

    assert len(seed_set.mod_set) == 22
    ids = [mod.id for mod in seed_set.mod_set]
    assert len(ids) == len(set(ids))  # no duplicate ids
    assert ids == [mod_id.lower() for mod_id in ids]  # canonical case, as compared
    versions = {mod.id: mod.version for mod in seed_set.mod_set}
    assert versions["619ac86e-d99d-4bf3-b8f0-8c5b8c402567"] == "179"  # Multiplayer Helper 1.7.9
    assert versions["c88cba8b-8311-4d35-90c3-51a4a5d66542"] == "1.39.5"  # Better Balanced Map
    assert versions["cb84075d-5007-4207-b662-c35a5f7be260"] == "70500"  # Better Balanced Game
    assert sum(version is None for version in versions.values()) == 19  # official packs


def test_v3_empty_mod_set_against_civsim_default_is_rejected() -> None:
    """An empty `mod_set` would be a false pin against this host: 22 mods are genuinely active
    (spikes/civsim-default-preset-linux.md, sweep-raw/gameconfig_values.txt). V3 must reject a
    run configuration claiming no mods against a seed set that pins 22, exactly as it already
    rejects any other mod_set disagreement
    (test_v3_mod_set_mismatch_names_mod_set_not_a_different_field above)."""
    seed_set = load_seed_set_yaml(CIVSIM_DEFAULT_SEED_SET_PATH.read_text(encoding="utf-8"))
    config = load_run_configuration_yaml(CONFIG_PATH.read_text(encoding="utf-8"))
    otherwise_agreeing = config.model_copy(
        update={
            "civilization": seed_set.civilization,
            "leader": seed_set.leader,
            "ruleset": seed_set.ruleset,
            "mod_set": [],
        }
    )

    with pytest.raises(PreflightError) as excinfo:
        check_seed_set_agreement(otherwise_agreeing, seed_set)

    mismatched_fields = {m["field"] for m in excinfo.value.detail["mismatches"]}
    assert mismatched_fields == {"mod_set"}
    # Never an override: the seed set's own 22-mod pin is untouched by the attempt.
    assert len(seed_set.mod_set) == 22


def test_v3_civsim_default_agreeing_configuration_passes_cleanly() -> None:
    seed_set = load_seed_set_yaml(CIVSIM_DEFAULT_SEED_SET_PATH.read_text(encoding="utf-8"))
    config = load_run_configuration_yaml(CONFIG_PATH.read_text(encoding="utf-8"))
    agreeing = config.model_copy(
        update={
            "civilization": seed_set.civilization,
            "leader": seed_set.leader,
            "ruleset": seed_set.ruleset,
            "mod_set": seed_set.mod_set,
        }
    )

    check_seed_set_agreement(agreeing, seed_set)  # must not raise


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
# V11 -- free disk >= min_free_disk_gb AND the estimated footprint fits (T243)
# --------------------------------------------------------------------------


def _host_with_free_bytes(path: Path, free_bytes: int) -> FakeHostPlatform:
    from civsim_harness.host.port import DiskSpace

    host = FakeHostPlatform()
    host.set_disk_space(
        DiskSpace(path=path, free_bytes=free_bytes, total_bytes=free_bytes * 2)
    )
    return host


def test_v11_a_footprint_that_cannot_fit_in_free_disk_is_rejected_even_above_the_floor(
    tmp_path: Path,
) -> None:
    """V11's second clause -- the half T081 never wired: free space clears `min_free_disk_gb`,
    but the run's estimated save/capture footprint does not fit, so preflight must refuse.
    `configs/turn50-validation.yaml` stops at turn 50, which under `saves/headroom.py`'s
    documented conservative defaults estimates ~4.5 GB; 2 GB free clears a 1-GB floor and cannot
    hold that."""
    from civsim_harness.errors import DiskHeadroomError
    from civsim_harness.saves.headroom import check_headroom, estimate_footprint

    config = load_run_configuration_yaml(CONFIG_PATH.read_text(encoding="utf-8"))
    footprint = estimate_footprint(config)
    two_gb = 2 * 1024**3
    assert footprint.total_bytes > two_gb  # the fixture is meaningful, not vacuous

    with pytest.raises(DiskHeadroomError) as excinfo:
        check_headroom(
            host=_host_with_free_bytes(tmp_path, two_gb),
            path=tmp_path,
            min_free_disk_gb=1,
            footprint=footprint,
        )
    assert excinfo.value.detail["estimated_footprint_bytes"] == footprint.total_bytes


def test_v11_below_the_floor_is_rejected_regardless_of_footprint(tmp_path: Path) -> None:
    from civsim_harness.errors import DiskHeadroomError
    from civsim_harness.saves.headroom import check_headroom, estimate_footprint

    config = load_run_configuration_yaml(CONFIG_PATH.read_text(encoding="utf-8"))
    with pytest.raises(DiskHeadroomError):
        check_headroom(
            host=_host_with_free_bytes(tmp_path, 1 * 1024**3),
            path=tmp_path,
            min_free_disk_gb=float(config.min_free_disk_gb),
            footprint=estimate_footprint(config),
        )


def test_v11_enough_room_for_both_the_floor_and_the_footprint_passes(tmp_path: Path) -> None:
    from civsim_harness.saves.headroom import check_headroom, estimate_footprint

    config = load_run_configuration_yaml(CONFIG_PATH.read_text(encoding="utf-8"))
    footprint = estimate_footprint(config)
    generous = footprint.total_bytes + (config.min_free_disk_gb + 10) * 1024**3

    check_headroom(  # must not raise
        host=_host_with_free_bytes(tmp_path, int(generous)),
        path=tmp_path,
        min_free_disk_gb=float(config.min_free_disk_gb),
        footprint=footprint,
    )


def test_v11_has_a_production_caller_at_preflight() -> None:
    """The wiring half, stated structurally: `run/composition.py`'s `_prepare_run` must call
    `estimate_footprint` and pass its result to `check_headroom` -- T243's finding was exactly
    that both existed and neither had a production caller, so a run below headroom started and
    died at its first quicksave instead of being refused for free. (The behavioural proof runs
    through the real composition root in tests/integration/test_end_to_end_wiring.py's
    `test_a_run_whose_estimated_footprint_cannot_fit_is_refused_at_preflight`; this assertion is
    the cheap guard that the call sites stay in the preflight path named here.)"""
    import inspect

    from civsim_harness.run import composition

    source = inspect.getsource(composition._prepare_run)
    assert "estimate_footprint(" in source
    assert "footprint=footprint" in source


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
