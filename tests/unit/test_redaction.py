"""Unit tests for the credential redaction filter (T016, SC-018).

Plants key-shaped secrets in three places the spec calls out explicitly --
config-shaped data, a provider-response-shaped payload, and a raised
exception -- and asserts the planted value survives in none of: a
serialized record, a rendered log line, or a formatted traceback.
"""

from __future__ import annotations

import io
import json
import logging
import traceback

from civsim_harness.errors import HarnessError, PreflightError, ProviderChainExhausted
from civsim_harness.models.records import RunEvent
from civsim_harness.telemetry.logging import attach_handler
from civsim_harness.telemetry.redaction import REDACTED, redact_text, redact_value

SECRET_API_KEY = "sk-or-v1-AAAABBBBCCCCDDDDEEEEFFFF0000"
SECRET_BEARER = "Bearer abcdefgh12345678ijklmnop"
SECRET_AWS = "AKIAABCDEFGHIJKLMNOP"
SECRET_PASSWORD = "hunter2superlongpassword"


def _assert_absent(haystack: str, *secrets: str) -> None:
    for secret in secrets:
        assert secret not in haystack, f"leaked secret {secret!r} in: {haystack!r}"


# --------------------------------------------------------------------------
# Structural redaction: key-shaped values
# --------------------------------------------------------------------------


def test_redact_value_blanks_credential_shaped_keys_in_config() -> None:
    """A config-shaped dict with credential-shaped keys never keeps the raw value."""
    config_like = {
        "seed_set_id": "shuffle-classic-2026q3",
        "model_config": {
            "primary": {"provider": "openrouter", "model": "anthropic/claude-opus-5"},
            "request_params": {"temperature": 0.7},
        },
        # Planted: a credential that should never have been here in the first place,
        # but the filter must catch it structurally regardless.
        "api_key": SECRET_API_KEY,
        "nested": {"auth_token": SECRET_BEARER, "password": SECRET_PASSWORD},
    }

    redacted = redact_value(config_like)

    assert redacted["api_key"] == REDACTED
    assert redacted["nested"]["auth_token"] == REDACTED
    assert redacted["nested"]["password"] == REDACTED
    serialized = json.dumps(redacted)
    _assert_absent(serialized, SECRET_API_KEY, SECRET_BEARER, SECRET_PASSWORD)


def test_redact_value_handles_lists_and_pydantic_models() -> None:
    event = RunEvent(
        event_id="evt_1",  # type: ignore[arg-type]
        run_id="run_1",  # type: ignore[arg-type]
        turn_number=None,
        step_index=None,
        event_type="provider_failure",
        occurred_at="2026-09-20T00:00:00Z",
        detail={"error": "context_rejected", "api_key": SECRET_API_KEY},
    )
    # RunEvent.detail redacts on the way in (data-model.md's own annotation:
    # "Type-specific, credential-redacted").
    assert event.detail["api_key"] == REDACTED

    dumped = event.model_dump(mode="json")
    serialized = json.dumps(dumped)
    _assert_absent(serialized, SECRET_API_KEY)

    also_redacted = redact_value([{"token": SECRET_BEARER}, {"plain": "fine"}])
    assert also_redacted[0]["token"] == REDACTED
    assert also_redacted[1]["plain"] == "fine"


# --------------------------------------------------------------------------
# Textual redaction: value-shaped secrets with no key to inspect
# --------------------------------------------------------------------------


def test_redact_text_scrubs_key_shaped_values_in_free_text() -> None:
    """Simulates a provider response body: free text with no dict structure at all."""
    response_body = (
        f"Request failed. Authorization: {SECRET_BEARER}. "
        f"Retry with a fresh key: {SECRET_API_KEY} or aws key {SECRET_AWS}. "
        f"Also tried api_key={SECRET_PASSWORD} inline."
    )

    redacted = redact_text(response_body)

    _assert_absent(redacted, SECRET_API_KEY, SECRET_BEARER, SECRET_AWS, SECRET_PASSWORD)
    assert REDACTED in redacted


# --------------------------------------------------------------------------
# Exceptions: redacted at construction, so no downstream leak is possible
# --------------------------------------------------------------------------


def test_exception_detail_is_redacted_at_construction() -> None:
    exc = PreflightError(
        "model preflight failed",
        detail={"provider": "openrouter", "api_key": SECRET_API_KEY},
    )

    assert exc.detail["api_key"] == REDACTED
    _assert_absent(str(exc), SECRET_API_KEY)
    _assert_absent(repr(exc), SECRET_API_KEY)


def test_exception_message_itself_is_scrubbed() -> None:
    exc = ProviderChainExhausted(f"chain exhausted, last error used key {SECRET_API_KEY}")

    _assert_absent(str(exc), SECRET_API_KEY)


def test_raised_exception_traceback_never_contains_the_secret() -> None:
    def _raise() -> None:
        raise HarnessError(
            "boom",
            detail={"secret_key": SECRET_API_KEY, "session_token": SECRET_BEARER},
        )

    try:
        _raise()
    except HarnessError as exc:
        formatted = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        _assert_absent(formatted, SECRET_API_KEY, SECRET_BEARER)
    else:  # pragma: no cover - the raise above always fires
        raise AssertionError("expected HarnessError to be raised")


# --------------------------------------------------------------------------
# Logging: the redacting handler/formatter is mandatory, not opt-in
# --------------------------------------------------------------------------


def test_log_line_never_contains_a_planted_secret_in_message_or_exc_info() -> None:
    buffer = io.StringIO()
    logger = logging.getLogger("civsim_harness.test.redaction")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False
    attach_handler(logger, logging.StreamHandler(buffer))

    logger.info("provider call used api_key=%s", SECRET_API_KEY)

    try:
        raise ValueError(f"leaked: {SECRET_BEARER}")
    except ValueError:
        logger.exception("provider call failed with token %s", SECRET_AWS)

    output = buffer.getvalue()
    _assert_absent(output, SECRET_API_KEY, SECRET_BEARER, SECRET_AWS)

    # Also confirm it is genuine structured JSON, not a stringified fallback,
    # so the redaction pass really did run over a parsed structure.
    lines = [line for line in output.splitlines() if line.strip()]
    assert lines
    for line in lines:
        payload = json.loads(line)
        assert "message" in payload
