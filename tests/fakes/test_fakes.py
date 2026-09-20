"""Smoke tests for the three test doubles (T055, T056, T057).

Each section proves its fake behaves as scripted -- these are not
conformance suites (`tests/contract/test_host_platform_port.py` and its
future `model_provider`/`nexus` siblings own that job once written); they
exist so a later wave trusting these fakes has evidence they do what their
docstrings claim, including the one thing that matters most for T056: a
real, unmodified `NexusClient` actually round-tripping against the fake
server over a real socket.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest

from civsim_harness.errors import NexusError, PreflightError
from civsim_harness.host.detect import SupportTier, resolve_support_tier
from civsim_harness.host.port import CaptureStatus, HostPlatform, InputStatus
from civsim_harness.models.common import DeclarationId, ModelRef
from civsim_harness.models.records import CallOutcome
from civsim_harness.nexus.client import REASON_GAME_STATES_UNAVAILABLE, NexusClient
from civsim_harness.nexus.heartbeat import probe_heartbeat
from civsim_harness.provider.port import (
    DecisionRequest,
    Image,
    ModelCapabilities,
    ModelProvider,
    RawDecision,
)

from .fake_host import DEFAULT_PROCESS, DEFAULT_WINDOW, FakeHostPlatform
from .fake_nexus import (
    SYNTHETIC_TURN_TRANSCRIPT,
    FakeNexusServer,
    loaded_game_state_table,
    menu_only_state_table,
)
from .fake_provider import DEFAULT_CAPABILITIES, FakeModelProvider, ProviderContractViolation

# --------------------------------------------------------------------------
# T055 -- FakeHostPlatform
# --------------------------------------------------------------------------


def test_fake_host_satisfies_the_host_platform_protocol() -> None:
    assert isinstance(FakeHostPlatform(), HostPlatform)


def test_fake_host_default_is_validated_and_everything_works(tmp_path: Path) -> None:
    host = FakeHostPlatform()
    assert host.tier is SupportTier.validated
    assert resolve_support_tier(host.probe_result) is SupportTier.validated

    assert host.locate_game_process() == DEFAULT_PROCESS
    assert host.find_game_window(DEFAULT_PROCESS) == DEFAULT_WINDOW

    capture = host.capture_window(DEFAULT_WINDOW)
    assert capture.status is CaptureStatus.ok
    assert capture.frame is not None
    assert capture.frame.rect == DEFAULT_WINDOW.rect

    input_result = host.send_input([])
    assert input_result.status is InputStatus.ok

    dirs = host.resolve_game_directories(home=tmp_path)
    assert tmp_path in dirs.saves_dir.parents
    assert tmp_path in dirs.app_options_path.parents

    space = host.free_disk_space(tmp_path)
    assert space.total_bytes >= space.free_bytes >= 0


@pytest.mark.parametrize(
    "tier,expected_tier_from_probe",
    [
        (SupportTier.unsupported, SupportTier.unsupported),
        (SupportTier.supported, SupportTier.supported),
        (SupportTier.validated, SupportTier.validated),
    ],
)
def test_fake_host_presents_every_tier_on_demand(
    tier: SupportTier, expected_tier_from_probe: SupportTier
) -> None:
    host = FakeHostPlatform(tier=tier)
    assert host.tier is tier
    assert resolve_support_tier(host.probe_result) is expected_tier_from_probe


def test_fake_host_unsupported_tier_defaults_capture_to_unavailable_with_a_reason() -> None:
    host = FakeHostPlatform(tier=SupportTier.unsupported)
    result = host.capture_window(DEFAULT_WINDOW)
    assert result.status is CaptureStatus.unavailable
    assert result.reason  # CaptureResult.__post_init__ already enforces this is non-empty


def test_fake_host_per_capability_override_wins_over_tier_default() -> None:
    host = FakeHostPlatform(tier=SupportTier.validated)
    host.set_capture_unavailable("scripted: capture device unplugged mid-run")
    result = host.capture_window(DEFAULT_WINDOW)
    assert result.status is CaptureStatus.unavailable
    assert result.reason == "scripted: capture device unplugged mid-run"

    host.set_capture_failed("scripted: capture attempt errored")
    failed = host.capture_window(DEFAULT_WINDOW)
    assert failed.status is CaptureStatus.failed
    assert failed.reason == "scripted: capture attempt errored"


def test_fake_host_scripts_input_unavailable_like_wayland() -> None:
    host = FakeHostPlatform()
    host.set_input_unavailable("scripted: wayland blocks synthetic input by design")
    result = host.send_input([])
    assert result.status is InputStatus.unavailable
    assert "wayland" in (result.reason or "").lower()


def test_fake_host_scripts_process_and_window_absence() -> None:
    host = FakeHostPlatform()
    host.set_process(None)
    assert host.locate_game_process() is None

    host.set_window(None)
    assert host.find_game_window(DEFAULT_PROCESS) is None


def test_fake_host_scripts_find_window_raising_preflight_error() -> None:
    host = FakeHostPlatform()
    error = PreflightError("scripted: window identity dependency missing")
    host.set_find_window_error(error)
    with pytest.raises(PreflightError):
        host.find_game_window(DEFAULT_PROCESS)


# --------------------------------------------------------------------------
# T057 -- FakeModelProvider
# --------------------------------------------------------------------------

_MODEL = ModelRef(provider="openrouter", model="anthropic/claude-sonnet-5")


def _request(*, images: list[Image] | None = None, step_index: int = 0) -> DecisionRequest:
    return DecisionRequest(
        model=_MODEL,
        system="you are playing Civilization VI",
        observation="{}",
        images=images if images is not None else [],
        step_index=step_index,
        response_schema={},
    )


def test_fake_provider_satisfies_the_model_provider_protocol() -> None:
    # `ModelProvider` is not `@runtime_checkable` (unlike `HostPlatform`), so
    # this is a static assertion: mypy rejects the assignment below unless
    # `FakeModelProvider` structurally satisfies the Protocol.
    provider: ModelProvider = FakeModelProvider()
    assert provider.describe(_MODEL) is not None


def test_fake_provider_describe_defaults_generous_and_is_overridable() -> None:
    provider = FakeModelProvider()
    assert provider.describe(_MODEL) == DEFAULT_CAPABILITIES

    tiny = ModelCapabilities(
        accepts_images=False, max_context_tokens=64, max_images_per_request=0, confirmed=True
    )
    provider.set_capabilities(_MODEL, tiny)
    assert provider.describe(_MODEL) == tiny


def test_fake_provider_queue_decision_reports_image_count_and_default_model_served() -> None:
    provider = FakeModelProvider()
    decision = RawDecision(
        action_declaration_id=DeclarationId("turn.end_turn"), reasoning="done for the turn"
    )
    provider.queue_decision(decision)

    images = [Image(media_type="image/png", data=b"a"), Image(media_type="image/png", data=b"b")]
    response = provider.complete(_request(images=images))

    assert response.outcome is CallOutcome.DECISION_RETURNED
    assert response.decision == decision
    assert response.model_served == _MODEL
    assert response.image_count == 2


def test_fake_provider_queue_decision_can_report_a_different_served_model() -> None:
    provider = FakeModelProvider()
    fallback_model = ModelRef(provider="openrouter", model="google/gemini-3-pro")
    provider.queue_decision(
        RawDecision(action_declaration_id=DeclarationId("turn.end_turn"), reasoning="fallback"),
        model_served=fallback_model,
        fallback_occurred=True,
    )

    response = provider.complete(_request())

    assert response.model_served == fallback_model
    assert response.fallback_occurred is True


@pytest.mark.parametrize(
    "queue_method,expected_outcome",
    [
        ("queue_empty_response", CallOutcome.EMPTY_RESPONSE),
        ("queue_rate_limited", CallOutcome.RATE_LIMITED),
        ("queue_context_rejected", CallOutcome.CONTEXT_REJECTED),
        ("queue_failed", CallOutcome.FAILED),
    ],
)
def test_fake_provider_scripts_every_failure_outcome(
    queue_method: str, expected_outcome: CallOutcome
) -> None:
    provider = FakeModelProvider()
    getattr(provider, queue_method)()

    response = provider.complete(_request())

    assert response.outcome is expected_outcome
    assert response.decision is None


def test_fake_provider_multi_decision_raises_the_contract_violation() -> None:
    provider = FakeModelProvider()
    provider.queue_multi_decision(
        [
            RawDecision(action_declaration_id=DeclarationId("units.move_to"), reasoning="one"),
            RawDecision(action_declaration_id=DeclarationId("units.move_to"), reasoning="two"),
        ]
    )

    with pytest.raises(ProviderContractViolation):
        provider.complete(_request())


def test_fake_provider_multi_decision_needs_at_least_two_decisions() -> None:
    provider = FakeModelProvider()
    with pytest.raises(ValueError, match="at least two"):
        provider.queue_multi_decision(
            [RawDecision(action_declaration_id=DeclarationId("turn.end_turn"), reasoning="only")]
        )


def test_fake_provider_default_decision_answers_indefinitely_when_queue_is_empty() -> None:
    provider = FakeModelProvider()
    provider.set_default_decision(
        RawDecision(action_declaration_id=DeclarationId("turn.end_turn"), reasoning="keep going")
    )

    for step_index in range(5):
        response = provider.complete(_request(step_index=step_index))
        assert response.outcome is CallOutcome.DECISION_RETURNED


def test_fake_provider_default_decision_factory_varies_per_request() -> None:
    provider = FakeModelProvider()
    provider.set_default_decision_factory(
        lambda request: RawDecision(
            action_declaration_id=DeclarationId("turn.end_turn"),
            reasoning=f"step {request.step_index}",
        )
    )

    first = provider.complete(_request(step_index=0))
    second = provider.complete(_request(step_index=1))

    assert first.decision is not None and first.decision.reasoning == "step 0"
    assert second.decision is not None and second.decision.reasoning == "step 1"


def test_fake_provider_raises_when_nothing_is_scripted() -> None:
    provider = FakeModelProvider()
    with pytest.raises(AssertionError):
        provider.complete(_request())


# --------------------------------------------------------------------------
# T056 -- FakeNexusServer, exercised through the real NexusClient
# --------------------------------------------------------------------------

_TURN_NUMBER_LUA = "print(CivSim_JsonEncode(CivSim_TurnControl.read_turn_number()))"
_SCREEN_PROBE_LUA = "print(CivSim_JsonEncode(CivSim_Screens.probe()))"
_END_TURN_LUA = "print(CivSim_JsonEncode(CivSim_TurnControl.end_turn()))"


async def test_real_nexus_client_round_trips_against_the_fake_server() -> None:
    server = FakeNexusServer(list(SYNTHETIC_TURN_TRANSCRIPT))
    await server.start()
    client = NexusClient(host="127.0.0.1", port=server.port)
    try:
        indices = await client.connect()
        assert indices.game_core_tuner == 0
        assert indices.in_game == 1

        turn_1 = await client.execute_command(
            state_index=indices.game_core_tuner, lua_body=_TURN_NUMBER_LUA
        )
        assert turn_1 == {
            "turn_number": 1,
            "is_local_player_turn": True,
            "is_waiting_for_other_players": False,
        }

        screen = await client.execute_command(
            state_index=indices.game_core_tuner, lua_body=_SCREEN_PROBE_LUA
        )
        assert screen["screen"] == "world_view"

        end_turn = await client.execute_command(
            state_index=indices.in_game, lua_body=_END_TURN_LUA
        )
        assert end_turn == {"ok": True, "path": "Game.EndTurn"}

        turn_2 = await client.execute_command(
            state_index=indices.game_core_tuner, lua_body=_TURN_NUMBER_LUA
        )
        assert turn_2["turn_number"] == 2
    finally:
        await client.close()
        await server.stop()

    assert server.connection_count == 1
    assert len(server.received) == 4
    assert server.app_names == ["civsim_harness"]
    assert server.unmatched_requests == []


async def test_fake_server_drops_the_connection_at_a_scripted_step() -> None:
    server = FakeNexusServer()
    server.drop_connection_at(1)
    await server.start()
    client = NexusClient(host="127.0.0.1", port=server.port)
    try:
        await client.connect()
        with pytest.raises(NexusError):
            await client.execute_command(state_index=0, lua_body=_TURN_NUMBER_LUA)
    finally:
        await client.close()
        await server.stop()


async def test_fake_server_stalls_one_operation_while_a_real_heartbeat_still_answers() -> None:
    # Uses the real `civsim_harness.nexus.heartbeat.probe_heartbeat` (not a
    # stand-in) for the "client alive but not servicing an operation" proof
    # research R15 and the wire contract's "Heartbeat nonce fails to
    # round-trip" row name explicitly.
    server = FakeNexusServer()
    server.stall_on("SlowOperation")
    server.queue_response(True, match="print(true)")  # the real heartbeat's exact Lua body
    await server.start()
    client = NexusClient(host="127.0.0.1", port=server.port, command_timeout_s=0.2)
    try:
        indices = await client.connect()
        assert indices.game_core_tuner is not None

        with pytest.raises(NexusError):
            await client.execute_command(
                state_index=indices.game_core_tuner,
                lua_body="print(CivSim_JsonEncode(SlowOperation()))",
            )

        # The connection survived the stalled operation: a real heartbeat
        # probe, sent right after, still round-trips normally.
        assert await probe_heartbeat(client) is True
    finally:
        await client.close()
        await server.stop()


async def test_fake_server_presents_the_menu_only_state_table_on_demand() -> None:
    server = FakeNexusServer(state_table=menu_only_state_table())
    await server.start()
    client = NexusClient(host="127.0.0.1", port=server.port)
    try:
        indices = await client.connect()
        assert indices.by_name == {"Main State": 0, "DebugHotloadCache": 1}
        assert indices.has_game_states is False

        with pytest.raises(PreflightError) as excinfo:
            await client.resolve_game_states()
        assert excinfo.value.detail["reason"] == REASON_GAME_STATES_UNAVAILABLE
        assert excinfo.value.detail["missing"] == ["GameCore_Tuner", "InGame"]
    finally:
        await client.close()
        await server.stop()


async def test_fake_server_can_transition_from_menu_to_a_loaded_game_mid_session() -> None:
    server = FakeNexusServer(state_table=menu_only_state_table())
    await server.start()
    client = NexusClient(host="127.0.0.1", port=server.port)
    try:
        menu_indices = await client.connect()
        assert menu_indices.has_game_states is False

        # "The game loads" mid-session, without a reconnect.
        server.set_state_table(loaded_game_state_table(game_core_tuner=4, in_game=7))
        game_indices = await client.resolve_game_states()

        assert game_indices.game_core_tuner == 4
        assert game_indices.in_game == 7
        assert game_indices.has_game_states is True
    finally:
        await client.close()
        await server.stop()


async def test_fake_server_speaks_the_live_verified_reply_framing_on_the_wire() -> None:
    """Raw-socket audit of the fake's own frames, no NexusClient in the loop.

    The fake must speak the framing a real client was measured speaking
    (specs/002-civ-playing-harness/spikes/r5-raw-windows/
    raw_protocol_transcript.txt + raw_command_transcript.txt), not the
    framing the harness client expects -- a fake written to the client's
    expectations confirms that client forever, which is exactly how the
    pre-live protocol defects survived 1500+ green tests. Asserted here:

    - `APP:` is answered with a TAG_HANDSHAKE identification frame whose
      payload has an odd NUL-field count (three fields);
    - a command's result arrives as tag -1 frames, one per printed line,
      each prefixed `O\\0<StateName>: `;
    - the TAG_COMMAND reply frame is empty.
    """
    from civsim_harness.nexus.codec import TAG_ASYNC_OUTPUT, NexusFrameDecoder, encode_frame
    from civsim_harness.nexus.codec import TAG_COMMAND as TC
    from civsim_harness.nexus.codec import TAG_HANDSHAKE as TH
    from civsim_harness.nexus.sentinels import wrap_lua

    server = FakeNexusServer()
    server.queue_response({"ok": True}, match="print(true)")
    await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        decoder = NexusFrameDecoder()

        async def read_frames(count: int) -> list:
            frames: list = []
            while len(frames) < count:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=5.0)
                assert chunk, "fake server closed the connection unexpectedly"
                frames.extend(decoder.feed(chunk))
            return frames

        writer.write(encode_frame(TH, "APP:wire-audit"))
        await writer.drain()
        (app_reply,) = await read_frames(1)
        assert app_reply.tag == TH
        # Three NUL-separated fields -- the odd count that must break a
        # client which misreads this frame as the LSQ: reply.
        assert len(app_reply.payload.split("\x00")) == 3

        writer.write(encode_frame(TH, "LSQ:"))
        await writer.drain()
        (lsq_reply,) = await read_frames(1)
        assert lsq_reply.tag == TH
        assert "GameCore_Tuner" in lsq_reply.payload

        writer.write(encode_frame(TC, f"CMD:1:{wrap_lua('cafe01', 'print(true)')}"))
        await writer.drain()
        frames = await read_frames(4)

        assert [f.tag for f in frames] == [
            TAG_ASYNC_OUTPUT,
            TAG_ASYNC_OUTPUT,
            TAG_ASYNC_OUTPUT,
            TC,
        ]
        assert frames[0].payload == "O\x00InGame: ---BEGIN:cafe01---"
        assert frames[1].payload == 'O\x00InGame: {"ok": true}'
        assert frames[2].payload == "O\x00InGame: ---END:cafe01---"
        assert frames[3].payload == ""  # the empty tag-3 acknowledgement

        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
    finally:
        await server.stop()


async def test_stray_output_is_discarded_to_telemetry_not_returned_as_a_result() -> None:
    captured: list[str] = []
    server = FakeNexusServer()
    server.inject_stray_text(1, "Barbarians sighted near Kyiv.")
    server.queue_response({"n": 1})
    await server.start()
    client = NexusClient(
        host="127.0.0.1", port=server.port, on_unmatched_output=captured.append
    )
    try:
        await client.connect()
        result = await client.execute_command(state_index=0, lua_body=_TURN_NUMBER_LUA)
        assert result == {"n": 1}
        assert captured == ["Barbarians sighted near Kyiv."]
    finally:
        await client.close()
        await server.stop()
