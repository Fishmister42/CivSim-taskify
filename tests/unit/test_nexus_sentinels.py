"""Unit tests for Nexus sentinel correlation (T033).

The protocol has no native return values: a command's result is `print`
output that arrives asynchronously and can be interleaved with stray game
prints, a previous request's late-arriving tail, or a pair carrying a
nonce nobody is waiting for. This is the test that stops the client from
ever handing back another request's answer -- each of those three shapes
must be routed to telemetry and never returned as a result
(contracts/nexus-protocol.md "Request/response discipline" and "Timeouts,
health, and failure").
"""

from __future__ import annotations

from civsim_harness.nexus.sentinels import (
    SentinelCorrelator,
    begin_marker,
    end_marker,
    generate_nonce,
    wrap_lua,
)


class Recorder:
    """A minimal recording telemetry sink, used across most tests below."""

    def __init__(self) -> None:
        self.captured: list[str] = []

    def __call__(self, text: str) -> None:
        self.captured.append(text)


# --------------------------------------------------------------------------
# generate_nonce / markers / wrap_lua
# --------------------------------------------------------------------------


def test_generate_nonce_is_nonempty_and_unique_across_many_calls() -> None:
    nonces = {generate_nonce() for _ in range(1000)}
    assert len(nonces) == 1000
    assert all(nonce for nonce in nonces)


def test_markers_match_the_contracts_sentinel_format() -> None:
    assert begin_marker("abc123") == "---BEGIN:abc123---"
    assert end_marker("abc123") == "---END:abc123---"


def test_wrap_lua_brackets_the_body_with_both_sentinels_in_order() -> None:
    wrapped = wrap_lua("n1", 'print("hello")')
    lines = wrapped.splitlines()
    assert lines[0] == 'print("---BEGIN:n1---")'
    assert lines[1] == 'print("hello")'
    assert lines[2] == 'print("---END:n1---")'


def test_wrap_lua_does_not_let_a_line_comment_swallow_the_end_sentinel() -> None:
    # If the body were concatenated onto one line, a trailing `--` comment
    # would comment out everything after it -- including the END print.
    wrapped = wrap_lua("n1", "local x = 1 -- a comment with no newline")
    assert f'print("{end_marker("n1")}")' in wrapped.splitlines()


# --------------------------------------------------------------------------
# SentinelCorrelator: the happy path
# --------------------------------------------------------------------------


def test_take_result_extracts_the_json_between_a_matched_pair() -> None:
    recorder = Recorder()
    correlator = SentinelCorrelator(on_unmatched=recorder)
    correlator.feed('---BEGIN:abc---\n{"ok": true}\n---END:abc---')

    result = correlator.take_result("abc")

    assert result == '{"ok": true}'
    assert recorder.captured == []  # nothing to discard: no stray content at all


def test_take_result_returns_none_when_the_pair_has_not_fully_arrived() -> None:
    correlator = SentinelCorrelator()
    correlator.feed("---BEGIN:abc---\n{\"ok\": true}\n")  # no END yet
    assert correlator.take_result("abc") is None


def test_take_result_reassembles_a_body_fragmented_across_many_feeds() -> None:
    # The wire promises fragmentation across arbitrary packet boundaries; feed
    # the sentinel-wrapped text one character at a time to be adversarial.
    recorder = Recorder()
    correlator = SentinelCorrelator(on_unmatched=recorder)
    full_text = '---BEGIN:frag---\n{"turn": 42}\n---END:frag---'

    result = None
    for char in full_text:
        correlator.feed(char)
        result = correlator.take_result("frag")
        if result is not None:
            break

    assert result == '{"turn": 42}'
    assert recorder.captured == []


def test_multiple_sequential_requests_do_not_cross_contaminate() -> None:
    # Regression guard for "the client returns another request's answer."
    recorder = Recorder()
    correlator = SentinelCorrelator(on_unmatched=recorder)

    correlator.feed('---BEGIN:r1---\n{"n": 1}\n---END:r1---')
    assert correlator.take_result("r1") == '{"n": 1}'

    correlator.feed('---BEGIN:r2---\n{"n": 2}\n---END:r2---')
    assert correlator.take_result("r2") == '{"n": 2}'

    # r1's answer must never resurface for r2, and the buffer is clean.
    assert correlator.take_result("r1") is None
    assert recorder.captured == []


# --------------------------------------------------------------------------
# The three discard shapes named in the task: stray prints, a previous
# request's trailing output, and output with no matching nonce.
# --------------------------------------------------------------------------


def test_stray_game_prints_before_begin_are_discarded_to_telemetry() -> None:
    recorder = Recorder()
    correlator = SentinelCorrelator(on_unmatched=recorder)

    correlator.feed("Turn 45 begins.\nBarbarians sighted near Kyiv.\n")
    # The stray text alone, with no sentinel for our nonce, resolves nothing yet.
    assert correlator.take_result("real-nonce") is None
    assert recorder.captured == []  # not yet known to be unmatched

    correlator.feed('---BEGIN:real-nonce---\n{"ok": true}\n---END:real-nonce---')
    result = correlator.take_result("real-nonce")

    assert result == '{"ok": true}'
    assert recorder.captured == ["Turn 45 begins.\nBarbarians sighted near Kyiv."]


def test_previous_requests_trailing_output_is_discarded_not_returned() -> None:
    recorder = Recorder()
    correlator = SentinelCorrelator(on_unmatched=recorder)

    # r1 completes normally.
    correlator.feed('---BEGIN:r1---\n{"n": 1}\n---END:r1---')
    assert correlator.take_result("r1") == '{"n": 1}'

    # r1's engine emits more output after its END arrived (a late tail), and
    # then r2's real pair arrives, all delivered as one chunk.
    correlator.feed(
        "AI civ finished its turn (late r1 tail)\n"
        '---BEGIN:r2---\n{"n": 2}\n---END:r2---'
    )

    result = correlator.take_result("r2")

    assert result == '{"n": 2}'
    assert recorder.captured == ["AI civ finished its turn (late r1 tail)"]
    # Never, under any circumstance, does r2's call see r1's content.
    assert "1" not in result


def test_output_with_no_matching_nonce_is_discarded_via_discard_unmatched() -> None:
    recorder = Recorder()
    correlator = SentinelCorrelator(on_unmatched=recorder)

    # A fully formed pair for a nonce nobody is waiting for any more
    # (e.g. the client already gave up on it after a per-command timeout).
    correlator.feed('---BEGIN:ghost---\n{"n": 99}\n---END:ghost---')

    # Waiting on a different, live nonce never matches this content.
    assert correlator.take_result("expected") is None
    assert recorder.captured == []  # not yet flushed

    # The client's timeout path calls this to flush what will never be used.
    correlator.discard_unmatched()

    assert recorder.captured == ['---BEGIN:ghost---\n{"n": 99}\n---END:ghost---']
    # And it is genuinely gone: a later arrival of "expected" starts clean.
    correlator.feed('---BEGIN:expected---\n{"n": 1}\n---END:expected---')
    assert correlator.take_result("expected") == '{"n": 1}'


def test_a_foreign_nonces_pair_ahead_of_ours_is_discarded_as_one_unmatched_chunk() -> None:
    # Not just isolated stray prints -- a whole other nonce's BEGIN/END pair
    # sitting in front of ours must also never be mistaken for our result.
    recorder = Recorder()
    correlator = SentinelCorrelator(on_unmatched=recorder)

    correlator.feed(
        '---BEGIN:other---\n{"n": 0}\n---END:other---'
        '---BEGIN:mine---\n{"n": 7}\n---END:mine---'
    )

    result = correlator.take_result("mine")

    assert result == '{"n": 7}'
    assert recorder.captured == ['---BEGIN:other---\n{"n": 0}\n---END:other---']


def test_default_sink_does_not_raise_when_none_is_supplied() -> None:
    correlator = SentinelCorrelator()  # no on_unmatched given
    correlator.feed("some stray text with no sentinel at all")
    correlator.discard_unmatched()  # must not raise
