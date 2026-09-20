"""Seed set loading and the V3 agreement check (T070, FR-031).

contracts/run-configuration.md "Seed set definition"; data-model.md §1.

Two responsibilities:

1. :func:`load_seed_set_yaml` / :func:`load_seed_set_file` parse a seed set
   document into a :class:`~civsim_harness.models.config.SeedSet`, including
   its pinned ``game_build`` (research R18, R20) and any
   ``accepted_build_changes`` (each already validated as a
   :class:`~civsim_harness.models.common.BuildAcceptance` by the model layer).
2. :func:`check_seed_set_agreement` is **V3**: when a run configuration names
   a seed set, ``civilization``, ``leader``, ``ruleset``, and ``mod_set`` must
   match it exactly. A mismatch is an error, never an override (FR-031) --
   this raises rather than silently preferring either side's value or
   treating the run configuration as authoritative.

The build-pin comparison itself (V10, invariant I18) is
``run/preparation.py``'s job (T072), not this module's -- this module only
loads the pinned identity and the accepted-changes list that check consumes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError as PydanticValidationError

from civsim_harness.errors import PreflightError
from civsim_harness.models.common import ModRef, SeedSetId
from civsim_harness.models.config import RunConfiguration, SeedSet

SUPPORTED_SCHEMA_VERSION = 1


def load_seed_set_yaml(text: str, *, source: str = "<string>") -> SeedSet:
    """Parse *text* as a seed set document and validate it as a
    :class:`SeedSet` (contracts/run-configuration.md "Seed set definition").
    """
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PreflightError("seed set is not valid YAML", detail={"source": source}) from exc

    if not isinstance(raw, dict):
        raise PreflightError("seed set must be a YAML mapping", detail={"source": source})

    version = raw.get("schema_version")
    if version != SUPPORTED_SCHEMA_VERSION:
        raise PreflightError(
            "unsupported seed set schema_version",
            detail={"source": source, "expected": SUPPORTED_SCHEMA_VERSION, "actual": version},
        )

    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise PreflightError("seed set is missing its (unique) name", detail={"source": source})

    payload: dict[str, Any] = {
        # `name` doubles as the id: data-model.md §1 requires `name` to be
        # unique, which is exactly what a stable id needs, and it saves every
        # caller from having to separately mint and track a SeedSetId.
        "seed_set_id": SeedSetId(name),
        "name": name,
        "seeds": raw.get("seeds", []),
        "civilization": raw.get("civilization"),
        "leader": raw.get("leader"),
        "ruleset": raw.get("ruleset"),
        "mod_set": raw.get("mod_set", []),
        "game_build": raw.get("game_build"),
        "accepted_build_changes": raw.get("accepted_build_changes", []),
        "created_at": raw.get("created_at", datetime.now(UTC)),
    }

    try:
        return SeedSet.model_validate(payload)
    except PydanticValidationError as exc:
        raise PreflightError(
            "seed set failed validation",
            detail={"source": source, "errors": _summarize(exc)},
        ) from exc


def load_seed_set_file(path: Path) -> SeedSet:
    """:func:`load_seed_set_yaml` over a file's contents."""
    return load_seed_set_yaml(path.read_text(encoding="utf-8"), source=str(path))


@dataclass(frozen=True)
class SeedSetFieldMismatch:
    """One field on which a ``RunConfiguration`` disagrees with its named
    seed set (V3)."""

    field: str
    configured: Any
    seed_set: Any


def check_seed_set_agreement(config: RunConfiguration, seed_set: SeedSet) -> None:
    """**V3**: when *config* names a seed set, ``civilization``, ``leader``,
    ``ruleset``, and ``mod_set`` must match *seed_set* exactly.

    Raises ``PreflightError`` (never silently overrides either side) the
    moment any of the four disagree, naming every mismatched field at once
    rather than stopping at the first.
    """
    mismatches: list[SeedSetFieldMismatch] = []
    if config.civilization != seed_set.civilization:
        mismatches.append(
            SeedSetFieldMismatch("civilization", config.civilization, seed_set.civilization)
        )
    if config.leader != seed_set.leader:
        mismatches.append(SeedSetFieldMismatch("leader", config.leader, seed_set.leader))
    if config.ruleset != seed_set.ruleset:
        mismatches.append(SeedSetFieldMismatch("ruleset", config.ruleset, seed_set.ruleset))
    if _mod_set_key(config.mod_set) != _mod_set_key(seed_set.mod_set):
        mismatches.append(SeedSetFieldMismatch("mod_set", config.mod_set, seed_set.mod_set))

    if mismatches:
        raise PreflightError(
            "run configuration disagrees with its seed_set (V3, FR-031); a mismatch "
            "is an error, never an override",
            detail={
                "seed_set": seed_set.name,
                "mismatches": [
                    {
                        "field": mismatch.field,
                        "configured": _jsonable(mismatch.configured),
                        "seed_set": _jsonable(mismatch.seed_set),
                    }
                    for mismatch in mismatches
                ],
            },
        )


def _mod_set_key(mod_set: list[ModRef]) -> frozenset[tuple[str, str]]:
    """``mod_set`` agreement is an exact-set comparison, order-independent --
    an empty list is meaningful ("no mods") and compares equal only to
    another empty list, never to an absent/unset value.
    """
    return frozenset((mod.id, mod.version) for mod in mod_set)


def _jsonable(value: Any) -> Any:
    if isinstance(value, list):
        return [item.model_dump() if hasattr(item, "model_dump") else item for item in value]
    return value


def _summarize(exc: PydanticValidationError) -> list[dict[str, str]]:
    return [
        {"loc": ".".join(str(part) for part in error["loc"]), "msg": error["msg"]}
        for error in exc.errors(include_url=False)
    ]
