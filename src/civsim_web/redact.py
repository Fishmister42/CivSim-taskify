"""FR-030's second redaction pass, at this feature's own boundary (T066).

plan.md Constraints: *"Response and view models never carry ``ModelConfig``
credentials or any field 002's own redaction (FR-043) might have missed; this
is a second redaction pass at the view-model boundary, not a rerun of 002's."*

Almost every value this feature renders is a *structured* field it reads through
the Panel Registry gate, so the boundary is enforced by the gate rather than by
string inspection: a credential-shaped field has no panel, so no constructor
reads it. This module exists for the handful of values that are **free-form text
produced by something else** -- a store's own ``ping()`` detail, and the string
form of an exception it raised -- where the content is not a field anyone
declared and the gate has nothing to check.

That distinction is why the redactor is not applied everywhere: running it over
declared fields would be a second, weaker check on values the gate already
refuses, and would risk mangling legitimate prose (an agent's reasoning, a
panel's parity basis) to no benefit.

**Why this matters here specifically.** FR-028 requires the interface to be
reachable, unauthenticated, from any device on the local network, and FR-030
draws the conclusion: anything rendered is *published*, not merely logged. A
store that cannot be reached typically says so with its connection string in the
message -- ``could not connect to postgresql://civsim:hunter2@10.2.0.5:5432/db``
-- which is the single most likely way a credential reaches this interface.

The patterns are deliberately **shape-based, not name-based**. A name-based
scan is what the schema audit already does; free-form text carries no names.
"""

from __future__ import annotations

import re

__all__ = ["REDACTED", "redact_secrets"]

#: What replaces a matched secret. A visible marker, never silent removal: an
#: operator reading ``ping raised: could not connect to <redacted>`` learns both
#: that the store is unreachable and that the detail was withheld, where a
#: silently shortened message would read as the whole truth.
REDACTED = "[redacted]"

#: URL userinfo -- ``scheme://user:password@host``. The leading scheme is
#: required so an ordinary ``host:port`` pair is left alone. Only the
#: ``user:password@`` run is replaced; the scheme and host survive, because
#: *which* store could not be reached is the diagnostic an operator needs and
#: is not itself a secret.
_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^\s/:@]+:[^\s/@]+@")

#: ``key=value`` / ``key: value`` pairs whose key names a credential. The key is
#: kept and the value replaced, so the message still says what kind of thing was
#: withheld. The name set is the one `tests/contract/test_web_read_api.py`'s
#: `_CREDENTIAL_NAMES` audits, so the redactor and the audit cannot drift into
#: disagreeing about what counts as a credential.
#:
#: The optional quote either side of the separator is what makes this match a
#: store that reported its failure as serialized JSON (``{"password": "..."}``)
#: rather than as prose -- a shape worth handling because a store answering with
#: its own structured error is at least as likely as one answering with a
#: sentence.
_NAMED_VALUE = re.compile(
    r"(?i)\b((?:api[_-]?key|secret|password|passwd|pwd|credential|authorization"
    r"|private[_-]?key|access[_-]?key|client[_-]?secret"
    r"|(?:access|auth|refresh|id|api|bearer|session)[_-]?tokens?"
    r"|session[_-]?id|cookie)s?)[\"']?\s*[=:]\s*(\"[^\"]*\"|'[^']*'|\S+)"
)

#: Credential *value* shapes that carry no key at all: an HTTP authorization
#: header's payload and the vendor key prefixes Principle VII's provider layer
#: could plausibly hold. This is the same set the FR-030 response audit greps
#: for, so a value the audit would flag is a value this redactor removes.
_VALUE_SHAPES = re.compile(
    r"(Bearer\s+\S+|Basic\s+[A-Za-z0-9+/=]{8,}|sk-[A-Za-z0-9_\-]{8,}"
    r"|xoxb-\S*|ghp_[A-Za-z0-9]{8,})"
)


def redact_secrets(text: str | None) -> str | None:
    """Replace credential-shaped runs in free-form text with ``REDACTED``.

    ``None`` and the empty string pass through unchanged -- there is nothing to
    redact and inventing a marker would say a secret had been withheld when
    none was present.

    The three passes run in order: userinfo first (so a DSN's password is gone
    before the ``key=value`` pass could match part of it), then named values,
    then bare value shapes.
    """
    if not text:
        return text
    redacted = _URL_USERINFO.sub(rf"\1{REDACTED}@", text)
    redacted = _NAMED_VALUE.sub(rf"\1={REDACTED}", redacted)
    return _VALUE_SHAPES.sub(REDACTED, redacted)
