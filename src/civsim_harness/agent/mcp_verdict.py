"""Classify an MCP tool reply into what actually happened — spec-005 M2.

The single most portable finding of the civ6-mcp audit: **a tool call that returned is not
an action that applied.** The MCP server narrates game-level refusals in the result body and
does not set ``isError``, so the transport-level "success" everyone naturally counts is the
wrong number.

What that cost when it was not classified:

- A first coverage tally read *12 of 12 tools OK* while one of the twelve was a refusal.
- After a client crash, **twelve consecutive** ``Cannot connect to Civ 6`` replies were
  recorded as successful calls.
- ``set_policies`` returned ``POLICIES_SET|Policies updated.`` **and**
  ``WARN:SILENT_FAILURE — engine rejected: slot 0`` in the same body.

This project has already shipped an inflated coverage number once, from its own harness, for
exactly this reason. Anything that counts MCP calls must classify at this boundary or inherit
the same inflation.

The split is by KIND, not by a longer keyword list, and that distinction was itself learned
the hard way — see :data:`SOFT_REFUSAL`.
"""

from __future__ import annotations

from enum import Enum

__all__ = ["Verdict", "classify_reply", "is_applied"]


class Verdict(str, Enum):
    """What a tool reply actually means."""

    #: The game accepted it. The only verdict that may be counted as coverage.
    APPLIED = "applied"
    #: The game understood and refused — a blocked move, a turn blocker, a bad argument.
    #: A legitimate answer, not a defect, but NOT an applied action.
    ENGINE_REFUSED = "engine_refused"
    #: The client was unreachable. Says nothing about the action; the call never landed.
    TRANSPORT_FAILURE = "transport_failure"
    #: The MCP layer itself errored (timeout, protocol, exception).
    MCP_ERROR = "mcp_error"


#: The client is gone. Checked first: a transport failure must never be read as a refusal,
#: because "the game said no" and "nothing was asked" are different facts about the world.
TRANSPORT_MARKERS: tuple[str, ...] = (
    "Cannot connect to Civ 6",
    "ConnectionError",
    "ConnectionResetError",
    "Connection refused",
    "0 bytes read on a total of",
)

#: Structured signals the server emits deliberately. Matched ANYWHERE in the body, because
#: they are markers rather than prose and mean the same thing wherever they appear.
HARD_REFUSAL: tuple[str, ...] = ("WARN:SILENT_FAILURE", "ERR:", "FAILED:")

#: English phrasing that only indicates refusal when it OPENS the reply.
#:
#: Scanned over a whole body these produce false positives on any long narration. Measured
#: 2026-09-24: an aggregated turn summary of ~12 KB of entirely correct game state was
#: classified as a refusal because the word "cannot" appears somewhere inside it, and
#: because ``"Error:"`` matches ``"RuntimeError:"`` printed by one degraded section.
#:
#: That is not cosmetic. A harness that aborts a run on repeated refusals will abort on a
#: tool that is working — a guard firing on its own measurement error. Position is what
#: separates a refusal from a mention.
SOFT_REFUSAL: tuple[str, ...] = (
    "Error:",
    "Error executing",
    "Empty reflections",
    "cannot ",
    "could not ",
    "couldn't",
    "not available",
    "not possible",
    "invalid",
    "not found",
)

#: How far into the reply a soft marker still counts as opening it.
SOFT_WINDOW = 200


def classify_reply(body: str, *, is_error: bool = False) -> Verdict:
    """Classify one MCP tool reply.

    Args:
        body: the reply text, concatenated across content blocks.
        is_error: the MCP ``isError`` flag. Honoured when set, but never relied upon —
            the server does not set it for game-level refusals, which is the whole reason
            this function exists.
    """
    if is_error:
        return Verdict.MCP_ERROR
    if any(m in body for m in TRANSPORT_MARKERS):
        return Verdict.TRANSPORT_FAILURE
    if any(m in body for m in HARD_REFUSAL):
        return Verdict.ENGINE_REFUSED
    head = body[:SOFT_WINDOW]
    lowered = head.lower()
    for marker in SOFT_REFUSAL:
        if marker in head or marker.lower() in lowered:
            return Verdict.ENGINE_REFUSED
    return Verdict.APPLIED


def is_applied(body: str, *, is_error: bool = False) -> bool:
    """True only when the game accepted the action.

    Use this, never ``not response.isError``, when counting coverage.
    """
    return classify_reply(body, is_error=is_error) is Verdict.APPLIED
