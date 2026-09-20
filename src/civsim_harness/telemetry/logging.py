"""Out-of-game structured logging (T015).

Harness telemetry -- model identity, cost, latency, retries, lifecycle
transitions -- is recorded here as structured JSON log lines, and never
presented to the playing agent as game information (FR-020).

The T014 redaction filter is mandatory on every handler this module hands
out: :func:`configure_logging` and :func:`attach_handler` are the only
sanctioned ways to wire a handler onto a harness logger, and both force the
redacting formatter and filter onto the handler regardless of what the
caller asked for. There is no code path here that lets an un-redacted
handler reach a harness logger -- bypassing redaction requires going around
this module entirely, not just forgetting an argument to it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, TextIO

from civsim_harness.telemetry.redaction import redact_value

HARNESS_LOGGER_NAME = "civsim_harness"

# The custom LogRecord attribute used to carry structured context alongside
# a log message (see log_event() below). Kept out of logging's own
# reserved-attribute namespace.
_EXTRA_ATTR = "harness_extra"


class RedactingFilter(logging.Filter):
    """Defence-in-depth: redacts a record's message and args before formatting.

    The formatter (below) redacts the fully rendered line, which is the
    primary guarantee; this filter additionally scrubs the record's raw
    fields so that any other code inspecting ``record.getMessage()`` or
    ``record.args`` ahead of formatting also sees redacted content.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_value(str(record.msg))
        if record.args:
            if isinstance(record.args, Mapping):
                record.args = redact_value(dict(record.args))
            else:
                record.args = tuple(redact_value(arg) for arg in record.args)
        extra = getattr(record, _EXTRA_ATTR, None)
        if extra is not None:
            setattr(record, _EXTRA_ATTR, redact_value(extra))
        return True


class JsonRedactingFormatter(logging.Formatter):
    """Renders a structured JSON line, then redacts the whole rendered payload.

    Building the JSON payload first and redacting it as one structure (rather
    than redacting only the message) is what catches a secret embedded in
    the exception traceback text or in structured ``extra`` context, not just
    in the top-level message string.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        extra = getattr(record, _EXTRA_ATTR, None)
        if extra is not None:
            payload["extra"] = extra

        redacted = redact_value(payload)
        return json.dumps(redacted, default=str, sort_keys=True)


def attach_handler(logger: logging.Logger, handler: logging.Handler) -> None:
    """The only sanctioned way to attach a handler to a harness logger.

    Forces the redacting formatter and filter onto *handler* regardless of
    what the caller set on it beforehand, so a handler can never reach a
    harness logger un-redacted.
    """
    handler.setFormatter(JsonRedactingFormatter())
    handler.addFilter(RedactingFilter())
    logger.addHandler(handler)


def configure_logging(
    *,
    name: str = HARNESS_LOGGER_NAME,
    level: int = logging.INFO,
    stream: TextIO | None = None,
) -> logging.Logger:
    """Configure (or reconfigure) a harness logger with a redacting stream handler.

    Reconfiguring clears existing handlers first, so a stale, differently
    configured handler from a previous call can never linger alongside the
    redacted one.
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)
    for existing in list(logger.handlers):
        logger.removeHandler(existing)
    attach_handler(logger, logging.StreamHandler(stream))
    logger.propagate = False
    return logger


def get_harness_logger(name: str = HARNESS_LOGGER_NAME) -> logging.Logger:
    """Return the named harness logger, configuring it with a default handler if needed."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        return configure_logging(name=name)
    return logger


def log_event(
    logger: logging.Logger,
    level: int,
    message: str,
    *,
    extra: Mapping[str, Any] | None = None,
    exc_info: bool = False,
) -> None:
    """Log a structured harness event with optional structured context.

    ``extra`` is attached under the record's ``harness_extra`` attribute and
    redacted by both the filter and the formatter above -- this is the
    intended entry point for logging anything with structured context (a
    run id, a retry count, a provider payload fragment) rather than
    interpolating it into the message string by hand.
    """
    log_extra = {_EXTRA_ATTR: dict(extra)} if extra else None
    logger.log(level, message, extra=log_extra, exc_info=exc_info)
