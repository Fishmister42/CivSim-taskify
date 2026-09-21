"""Run configuration loader (T069).

Parses contracts/run-configuration.md's YAML format into a
:class:`~civsim_harness.models.config.RunConfiguration`, and applies the three
preflight validations that belong to this loader specifically
(contracts/run-configuration.md "Validation"):

- **V1** -- every required field present and non-null. Enforced by
  ``RunConfiguration``'s own required fields; a missing/null field surfaces as
  a ``pydantic.ValidationError``, which this loader wraps as a
  ``PreflightError`` so every preflight failure in this feature shares one
  exception family.
- **V9** -- no credential-shaped value anywhere in the configuration. Checked
  against the *raw* parsed YAML mapping, in addition to ``ModelConfig``'s own
  ``request_params`` check (models/config.py), since a credential could in
  principle be planted under any key (``map_settings``, ``opponents``, a
  mistaken top-level field, ...), not only ``request_params``. This is a
  validation **error**, never a warning -- ``load_run_configuration_yaml``
  raises before a ``RunConfiguration`` is even constructed.
- **V12** -- ``no_progress_step_limit`` >= 1, with **no upper bound**.
  Enforced by ``RunConfiguration``'s own field constraint (``Field(ge=1)``);
  this loader adds nothing beyond letting that constraint surface as a
  ``PreflightError`` like every other V1-shaped failure, and never imposes a
  ceiling of its own.

**V3** (seed-set agreement) is ``config/seed_set.py``'s responsibility (T070):
this loader resolves a YAML ``seed_set: <name>`` reference to
``RunConfiguration.seed_set_id`` verbatim (the name doubles as the id, see
``seed_set.py``) and performs no agreement check itself. A caller that has
already loaded the referenced ``SeedSet`` calls
``seed_set.check_seed_set_agreement`` separately.

There is deliberately no handling of a ``turn_time_budget_s`` field anywhere
in this module, matching ``RunConfiguration`` itself -- its reappearance
would be a regression (FR-014), not a missing feature, so this loader does
not special-case or strip such a field; if one is ever present in a document
it fails as an unrecognised extra field, exactly like any other typo, because
``RunConfiguration`` is ``extra="forbid"``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml
from pydantic import ValidationError as PydanticValidationError

from civsim_harness.errors import PreflightError
from civsim_harness.models.common import ConfigId, RunId
from civsim_harness.models.config import RunConfiguration
from civsim_harness.telemetry.redaction import redact_value

SUPPORTED_SCHEMA_VERSION = 1

# YAML surface keys that are *references* (contracts/run-configuration.md)
# but map onto RunConfiguration's own resolved-id fields under a different
# name. Resolving the referenced SeedSet/GuidanceSet content is
# config/seed_set.py's (T070) and config/guidance.py's (T073) job
# respectively -- here the raw name/ref string is carried through verbatim as
# the id (SeedSetId/GuidanceSetId are both plain NewType(str), so this is a
# valid, if provisional, id on its own).
_RENAMED_FIELDS: Mapping[str, str] = {
    "seed_set": "seed_set_id",
    "guidance_set": "guidance_set_id",
}

# Fields this loader stamps itself rather than passing through verbatim.
_LOADER_OWNED_FIELDS = frozenset({"schema_version", "config_id", "created_at"})


def load_run_configuration_yaml(text: str, *, source: str = "<string>") -> RunConfiguration:
    """Parse *text* as a run configuration document and validate V1/V9/V12.

    Raises ``PreflightError`` on: a YAML syntax error; a non-mapping
    document; a ``schema_version`` this loader does not support; a
    credential-shaped value anywhere in the document (V9); or a
    ``RunConfiguration`` construction failure (a missing/null required
    field -- V1 -- or ``no_progress_step_limit < 1`` -- V12 -- or any other
    field-level violation).
    """
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PreflightError(
            "run configuration is not valid YAML", detail={"source": source}
        ) from exc

    if not isinstance(raw, dict):
        raise PreflightError(
            "run configuration must be a YAML mapping", detail={"source": source}
        )

    _check_schema_version(raw, source=source)
    _reject_credential_shaped_values(raw, source=source)

    payload = _translate_to_model_fields(raw)
    payload["config_id"] = _resolve_config_id(raw.get("config_id"))
    payload.setdefault("created_at", datetime.now(UTC))

    try:
        return RunConfiguration.model_validate(payload)
    except PydanticValidationError as exc:
        raise PreflightError(
            "run configuration failed validation (V1/V12)",
            detail={"source": source, "errors": _summarize(exc)},
        ) from exc


def load_run_configuration_file(path: Path) -> RunConfiguration:
    """:func:`load_run_configuration_yaml` over a file's contents."""
    return load_run_configuration_yaml(path.read_text(encoding="utf-8"), source=str(path))


def _check_schema_version(raw: Mapping[str, Any], *, source: str) -> None:
    version = raw.get("schema_version")
    if version != SUPPORTED_SCHEMA_VERSION:
        raise PreflightError(
            "unsupported run configuration schema_version",
            detail={"source": source, "expected": SUPPORTED_SCHEMA_VERSION, "actual": version},
        )


def _reject_credential_shaped_values(raw: Mapping[str, Any], *, source: str) -> None:
    """**V9**: no credential-shaped value anywhere in the raw document.

    Reuses the same structural redaction filter every other record in this
    codebase is checked against
    (:mod:`civsim_harness.telemetry.redaction`), so "credential-shaped" means
    one consistent thing everywhere rather than a second definition invented
    here. A credential-shaped value is a **validation error**, not a warning
    -- this raises rather than stripping the value and continuing.
    """
    redacted = redact_value(dict(raw))
    if redacted != raw:
        raise PreflightError(
            "run configuration contains a credential-shaped value (V9, FR-043); "
            "keys resolve from the environment or a secrets file at call time only, "
            "never from run configuration",
            detail={"source": source},
        )


def _resolve_config_id(raw_config_id: Any) -> ConfigId:
    """``config_id: auto`` (or an absent ``config_id``) generates one; any
    other value is used verbatim (contracts/run-configuration.md).
    """
    if raw_config_id in (None, "auto"):
        return ConfigId(f"config_{uuid4().hex}")
    return ConfigId(str(raw_config_id))


def _translate_to_model_fields(raw: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in raw.items():
        if key in _LOADER_OWNED_FIELDS:
            continue
        target = _RENAMED_FIELDS.get(key, key)
        payload[target] = value
    return payload


def _summarize(exc: PydanticValidationError) -> list[dict[str, str]]:
    return [
        {"loc": ".".join(str(part) for part in error["loc"]), "msg": error["msg"]}
        for error in exc.errors(include_url=False)
    ]


# --------------------------------------------------------------------------
# Branch configuration (T166, FR-033, contracts/run-configuration.md
# "Branch configuration")
#
# A branch is a run configuration plus a lineage reference: the seed,
# civilization, ruleset, mod set, map, and game settings are **inherited
# from the parent and may not be overridden** -- the branch starts from the
# parent's save, so restating them differently would describe a position
# that does not exist. Only `model_config`, `guidance_set`,
# `stop_condition`, `no_progress_step_limit`, `recovery_attempt_limit`, and
# `min_free_disk_gb` may vary; this is exactly the set deliverable 4's
# ablation work needs to vary, and no more. A restatement that differs is
# **rejected, not merged** -- this loader never silently prefers one side.
# --------------------------------------------------------------------------

#: Payload-key names (post `_translate_to_model_fields`) that are inherited
#: from the parent and may not be restated differently. Together with
#: `_VARIABLE_FIELDS` this is exhaustive over every `RunConfiguration` field
#: except the loader-owned `config_id`/`created_at`.
_INHERITED_FIELDS: tuple[str, ...] = (
    "seed_set_id",
    "map_seed",
    "civilization",
    "leader",
    "ruleset",
    "mod_set",
    "map_settings",
    "game_settings",
    "difficulty",
    "opponents",
)

#: Payload-key names a branch may set independently of its parent. `"model_config"`
#: is the wire/alias name for `RunConfiguration.agent_model_config` -- see
#: that model's own docstring for why the Python attribute differs.
_VARIABLE_FIELDS: tuple[str, ...] = (
    "model_config",
    "guidance_set_id",
    "stop_condition",
    "no_progress_step_limit",
    "recovery_attempt_limit",
    "min_free_disk_gb",
)

_BRANCH_CONFIGURABLE_FIELDS: tuple[str, ...] = _INHERITED_FIELDS + _VARIABLE_FIELDS


@dataclass(frozen=True)
class BranchFrom:
    """The lineage reference parsed from a `branch_from` block
    (contracts/run-configuration.md "Branch configuration", FR-033).
    """

    run_id: RunId
    turn: int


@dataclass(frozen=True)
class BranchFieldMismatch:
    """One inherited field on which a branch document disagrees with its
    parent's configuration (T164).
    """

    field: str
    parent: Any
    branch: Any


def _mod_set_key(mod_set: Any) -> Any:
    """Order-independent identity for a validated `mod_set`
    (`list[ModRef]`), mirroring `config/seed_set.py`'s own agreement check:
    the exact same mods restated in a different order is not "restating
    differently".
    """
    if isinstance(mod_set, list):
        # T251: a GUID's case is not identity (the client reports them mixed).
        return frozenset((mod.id.lower(), mod.version) for mod in mod_set)
    return mod_set


def _branch_values_equal(field: str, parent_value: Any, branch_value: Any) -> bool:
    if field == "mod_set":
        return bool(_mod_set_key(parent_value) == _mod_set_key(branch_value))
    return bool(parent_value == branch_value)


def _jsonable(value: Any) -> Any:
    if isinstance(value, list):
        return [item.model_dump() if hasattr(item, "model_dump") else item for item in value]
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return value


def parse_branch_from(raw: Mapping[str, Any], *, source: str) -> BranchFrom:
    """Parse and validate the `branch_from: {run_id, turn}` block on its
    own, independent of the rest of a branch document's validity.
    """
    block = raw.get("branch_from")
    if not isinstance(block, Mapping):
        raise PreflightError(
            "branch_from must be a mapping with run_id and turn "
            "(contracts/run-configuration.md 'Branch configuration')",
            detail={"source": source},
        )
    run_id = block.get("run_id")
    turn = block.get("turn")
    if not isinstance(run_id, str) or not run_id:
        raise PreflightError(
            "branch_from.run_id is required and must be a non-empty string",
            detail={"source": source},
        )
    if not isinstance(turn, int) or isinstance(turn, bool) or turn < 1:
        raise PreflightError(
            "branch_from.turn is required and must be an integer >= 1",
            detail={"source": source},
        )
    return BranchFrom(run_id=RunId(run_id), turn=turn)


def load_branch_configuration_yaml(
    text: str, *, parent_config: RunConfiguration, source: str = "<string>"
) -> tuple[RunConfiguration, BranchFrom]:
    """Parse *text* as a branch run-configuration document: a `branch_from`
    block plus whichever of `model_config`, `guidance_set`,
    `stop_condition`, `no_progress_step_limit`, `recovery_attempt_limit`,
    and `min_free_disk_gb` the branch wants to vary.

    Every other field -- seed, civilization, ruleset, mod set, map, and game
    settings -- is inherited from *parent_config*: omitted entirely, it is
    filled in verbatim; restated with a *different* value, this raises
    `PreflightError` naming every mismatched field at once (never merges,
    never silently prefers either side). A restated value that happens to
    match the parent is not an error.

    Applies the same `schema_version` and credential (V9) checks as
    `load_run_configuration_yaml`. Raises `PreflightError` on: invalid YAML;
    a missing or malformed `branch_from` block; an inherited-field mismatch;
    or a `RunConfiguration` construction failure once the parent's values
    are merged in.
    """
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PreflightError(
            "branch configuration is not valid YAML", detail={"source": source}
        ) from exc

    if not isinstance(raw, dict):
        raise PreflightError(
            "branch configuration must be a YAML mapping", detail={"source": source}
        )

    _check_schema_version(raw, source=source)
    _reject_credential_shaped_values(raw, source=source)

    if "branch_from" not in raw:
        raise PreflightError(
            "a branch configuration requires a branch_from block (run_id, turn) "
            "(contracts/run-configuration.md 'Branch configuration')",
            detail={"source": source},
        )
    branch_from = parse_branch_from(raw, source=source)

    body = {key: value for key, value in raw.items() if key != "branch_from"}
    payload = _translate_to_model_fields(body)
    payload["config_id"] = _resolve_config_id(body.get("config_id"))
    payload.setdefault("created_at", datetime.now(UTC))

    parent_dump = parent_config.model_dump(by_alias=True)
    for field in _BRANCH_CONFIGURABLE_FIELDS:
        if field not in payload:
            payload[field] = parent_dump[field]

    try:
        child_config = RunConfiguration.model_validate(payload)
    except PydanticValidationError as exc:
        raise PreflightError(
            "branch configuration failed validation",
            detail={"source": source, "errors": _summarize(exc)},
        ) from exc

    mismatches: list[BranchFieldMismatch] = []
    for field in _INHERITED_FIELDS:
        parent_value = getattr(parent_config, field)
        branch_value = getattr(child_config, field)
        if not _branch_values_equal(field, parent_value, branch_value):
            mismatches.append(
                BranchFieldMismatch(field=field, parent=parent_value, branch=branch_value)
            )

    if mismatches:
        raise PreflightError(
            "branch_from configuration restates an inherited field differently from its "
            "parent run; seed, civilization, ruleset, mod set, map, and game settings are "
            "inherited and may not be overridden (contracts/run-configuration.md 'Branch "
            "configuration')",
            detail={
                "source": source,
                "parent_run_id": branch_from.run_id,
                "mismatches": [
                    {
                        "field": mismatch.field,
                        "parent": _jsonable(mismatch.parent),
                        "branch": _jsonable(mismatch.branch),
                    }
                    for mismatch in mismatches
                ],
            },
        )

    return child_config, branch_from


def load_branch_configuration_file(
    path: Path, *, parent_config: RunConfiguration
) -> tuple[RunConfiguration, BranchFrom]:
    """:func:`load_branch_configuration_yaml` over a file's contents."""
    return load_branch_configuration_yaml(
        path.read_text(encoding="utf-8"), parent_config=parent_config, source=str(path)
    )


def peek_branch_from(path: Path) -> BranchFrom | None:
    """Which run and turn *path* branches from, or `None` if it is an ordinary run configuration.

    The one thing a caller must know **before** choosing a loader (T226): a branch document and a
    plain run configuration are the same file format apart from the `branch_from` block, and the
    two loaders are not interchangeable -- `load_run_configuration_file` rejects `branch_from`
    outright (`RunConfiguration` is `extra="forbid"`), while `load_branch_configuration_file`
    requires a *parent* `RunConfiguration` that can only be fetched once the parent run id is
    known. This resolves that chicken-and-egg with one cheap parse and no validation of anything
    else: a malformed `branch_from` still raises here (`parse_branch_from`), so a document that
    means to be a branch and is not well-formed is never quietly loaded as a fresh run.

    Raises `PreflightError` for unreadable or non-mapping YAML, so a caller never has to decide
    what an unparseable file "probably" was.
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise PreflightError(
            "run configuration is not valid YAML", detail={"source": str(path)}
        ) from exc
    if not isinstance(raw, dict):
        raise PreflightError(
            "run configuration must be a YAML mapping", detail={"source": str(path)}
        )
    if "branch_from" not in raw:
        return None
    return parse_branch_from(raw, source=str(path))
