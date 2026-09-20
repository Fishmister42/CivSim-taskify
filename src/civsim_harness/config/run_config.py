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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml
from pydantic import ValidationError as PydanticValidationError

from civsim_harness.errors import PreflightError
from civsim_harness.models.common import ConfigId
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
