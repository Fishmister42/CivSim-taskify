"""Credential redaction filter (T014).

FR-043 / SC-018 / invariant I8 require that no credential ever reaches a
serialized record, a log line, or an exception payload. This module is the
single, mandatory enforcement point for that rule: every other place in the
harness that touches something that might carry a secret (``errors.py``,
``telemetry/logging.py``, ``RunEvent.detail``) routes through the functions
here rather than re-implementing its own notion of "looks like a secret".

This is a *filter*, not a convention: it is designed so that skipping
redaction takes a deliberate act (constructing a payload by hand and never
passing it through :func:`redact_value`/:func:`redact_text`), not merely
forgetting a step that every call site is expected to remember.

Two complementary passes are applied:

- **Structural** (:func:`redact_value`): walks dicts/lists/pydantic models
  recursively and blanks *any* value whose key looks credential-shaped,
  regardless of the value's own content. This is the primary defence for
  structured data -- records, event details, config dumps.
- **Textual** (:func:`redact_text`): scans free text for value shapes that
  look like a credential even with no key to inspect at all -- the case that
  matters for log messages, provider response bodies, and exception text.

Neither pass is a complete secret-detector; both are heuristic. They are
deliberately layered so that a credential has to evade *both* the key-name
check and the value-shape check to leak, rather than relying on either alone.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

from pydantic import BaseModel

REDACTED: Final[str] = "***REDACTED***"

# --------------------------------------------------------------------------
# Structural (key-name-based) redaction
# --------------------------------------------------------------------------

# Key names that mark a value as credential-shaped no matter what the value
# itself looks like. Matched case-insensitively against the key with
# separators normalised, so "API_KEY", "apiKey", "api-key", and "api.key"
# are all caught by the same pattern.
_CREDENTIAL_KEY_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:^|[_\-.\s])("
    r"api[_\-.\s]?key|apikey|"
    r"secret(?:[_\-.\s]?key)?|"
    r"token|access[_\-.\s]?token|refresh[_\-.\s]?token|bearer[_\-.\s]?token|"
    r"password|passwd|pwd|"
    r"credentials?|"
    r"auth(?:orization)?|"
    r"private[_\-.\s]?key|client[_\-.\s]?secret|"
    r"session[_\-.\s]?id"
    r")(?:$|[_\-.\s])",
    re.IGNORECASE,
)


def _key_is_credential_shaped(key: str) -> bool:
    padded = f"_{key}_"
    return _CREDENTIAL_KEY_PATTERN.search(padded) is not None


# --------------------------------------------------------------------------
# Textual (value-shape-based) redaction
# --------------------------------------------------------------------------

_BEARER_RE: Final[re.Pattern[str]] = re.compile(
    r"\bBearer\s+[A-Za-z0-9\-_.=]{8,}", re.IGNORECASE
)
# Vendor key prefixes commonly include internal hyphens of their own
# (e.g. "sk-or-v1-...", "sk-ant-api03-...", "sk-proj-..."), so this matches
# the whole hyphen/underscore-delimited token after the "sk-" prefix rather
# than requiring one unbroken alphanumeric run.
_OPENAI_STYLE_RE: Final[re.Pattern[str]] = re.compile(r"\bsk-[A-Za-z0-9_-]{10,}\b")
_AWS_KEY_RE: Final[re.Pattern[str]] = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_GENERIC_KV_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)\b(api[_\-]?key|secret(?:[_\-]?key)?|token|password|passwd|"
    r"authorization|access[_\-]?token|refresh[_\-]?token|client[_\-]?secret|"
    r"private[_\-]?key)(\s*[:=]\s*)(\"[^\"]+\"|'[^']+'|[A-Za-z0-9\-_.+/=]{4,})"
)


def redact_text(text: str) -> str:
    """Redact credential-shaped substrings from free text.

    Used wherever there is no key/value structure to inspect -- log
    messages, exception text, raw provider response bodies.
    """
    result = _BEARER_RE.sub(f"Bearer {REDACTED}", text)
    result = _OPENAI_STYLE_RE.sub(REDACTED, result)
    result = _AWS_KEY_RE.sub(REDACTED, result)
    result = _GENERIC_KV_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", result)
    return result


def redact_value(value: Any) -> Any:
    """Recursively redact a value of arbitrary shape.

    - Mappings: any entry whose key is credential-shaped is blanked
      entirely (whatever its value); every other entry is redacted
      recursively.
    - Lists/tuples: every element is redacted recursively.
    - Pydantic models: dumped to a plain (JSON-mode) dict first, then
      redacted as a mapping -- this is what makes ``redact_value(some_record)``
      a valid way to redact an entire record model in one call.
    - Strings: passed through :func:`redact_text`.
    - Everything else (int, float, bool, None, enums, ...): returned as-is;
      these types cannot carry a credential shape on their own.
    """
    if isinstance(value, BaseModel):
        return redact_value(value.model_dump(mode="json"))

    if isinstance(value, Mapping):
        return {
            key: (REDACTED if _key_is_credential_shaped(str(key)) else redact_value(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        redacted_items = [redact_value(item) for item in value]
        return type(value)(redacted_items) if isinstance(value, tuple) else redacted_items
    if isinstance(value, Sequence) and not isinstance(value, str):
        return [redact_value(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_mapping(mapping: Mapping[str, Any]) -> dict[str, Any]:
    """Convenience wrapper for the common "redact a dict" case, typed as such."""
    result = redact_value(dict(mapping))
    if not isinstance(result, dict):  # pragma: no cover - dict in, dict out by construction
        raise TypeError("redact_value did not return a dict for a dict input")
    return result
