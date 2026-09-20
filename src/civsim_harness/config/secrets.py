"""Secrets resolution (T074, FR-043).

Provider credentials never live in ``RunConfiguration`` (``models.config.ModelConfig
.request_params`` even rejects a credential-shaped key outright at construction
time) -- they resolve from the environment or a secrets file **at call time
only**, through this module. Nothing here accepts a ``RunConfiguration`` or any
other run-configuration value as input, by design: a function that cannot see
run configuration cannot leak a credential *from* it, which is a stronger
guarantee than a convention asking every caller to remember not to pass one in.

Two sources, environment first:

1. The environment: ``<secret_name upper-cased>``, e.g. ``openrouter_api_key``
   resolves from ``$OPENROUTER_API_KEY``.
2. A YAML secrets file (default ``secrets.yaml`` in the current working
   directory, overridable via ``$CIVSIM_SECRETS_FILE``) -- flat
   ``secret_name: value`` pairs. Both ``secrets.yaml`` and ``secrets.yml`` are
   already gitignored.

``doctor`` (a later operator-surface wave, contracts/operator-surface.md
"Secrets") reports whether a key is *present*, never its value --
:func:`secret_is_present` is what that check should call, precisely so it
never has to touch the resolved value at all.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

import yaml

from civsim_harness.errors import PreflightError

DEFAULT_SECRETS_FILENAME = "secrets.yaml"
SECRETS_FILE_ENV_VAR = "CIVSIM_SECRETS_FILE"


def default_secrets_path() -> Path:
    """The secrets file resolved by default: ``$CIVSIM_SECRETS_FILE``, or
    ``./secrets.yaml`` relative to the current working directory.
    """
    override = os.environ.get(SECRETS_FILE_ENV_VAR)
    if override:
        return Path(override)
    return Path.cwd() / DEFAULT_SECRETS_FILENAME


def _env_var_name(secret_name: str) -> str:
    return secret_name.strip().upper()


def _read_secrets_file(path: Path) -> Mapping[str, object]:
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise PreflightError(
            "secrets file is not valid YAML", detail={"path": str(path)}
        ) from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise PreflightError(
            "secrets file must contain a YAML mapping of secret_name: value",
            detail={"path": str(path)},
        )
    return data


def resolve_secret(
    secret_name: str,
    *,
    env: Mapping[str, str] | None = None,
    secrets_file: Path | None = None,
) -> str | None:
    """Resolve *secret_name* from the environment, then a secrets file, at
    call time only.

    Returns ``None`` if found in neither -- this never raises for a
    merely-absent secret, since "is a key configured at all" (``doctor``) is
    a legitimate question distinct from "I needed one and there was none"
    (:func:`require_secret`).
    """
    active_env = env if env is not None else os.environ
    from_env = active_env.get(_env_var_name(secret_name))
    if from_env:
        return from_env

    path = secrets_file if secrets_file is not None else default_secrets_path()
    file_values = _read_secrets_file(path)
    from_file = file_values.get(secret_name)
    if isinstance(from_file, str) and from_file:
        return from_file
    return None


def require_secret(
    secret_name: str,
    *,
    env: Mapping[str, str] | None = None,
    secrets_file: Path | None = None,
) -> str:
    """Like :func:`resolve_secret`, but raises ``PreflightError`` when the
    secret is not set in either source -- for a call site that cannot
    proceed without it (e.g. a provider adapter about to make its first
    request).
    """
    value = resolve_secret(secret_name, env=env, secrets_file=secrets_file)
    if value is None:
        raise PreflightError(
            f"secret {secret_name!r} is not set in the environment "
            f"(${_env_var_name(secret_name)}) or the secrets file",
            detail={"secret_name": secret_name},
        )
    return value


def secret_is_present(
    secret_name: str,
    *,
    env: Mapping[str, str] | None = None,
    secrets_file: Path | None = None,
) -> bool:
    """Whether *secret_name* resolves to a value -- never its value itself.

    This is the one call ``doctor`` (contracts/operator-surface.md "Secrets")
    should make: it reports presence, never a secret's contents (FR-043,
    SC-018).
    """
    return resolve_secret(secret_name, env=env, secrets_file=secrets_file) is not None


def provider_key_name(provider: str) -> str:
    """The canonical secret name for a ``ModelRef.provider``'s API key, e.g.
    ``provider_key_name("openrouter") == "openrouter_api_key"``.
    """
    return f"{provider.strip().lower()}_api_key"
