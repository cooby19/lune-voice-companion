from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

from lune.audio.devices import DeviceInfo, DeviceSnapshot
from lune.config import BudgetConfig
from lune.llm.budget import BudgetLedger
from lune.llm.contracts import (
    GenerationFunctionCallFrame,
    GenerationLLMTextFrame,
    ProviderStreamFrame,
    ProviderTerminalFrame,
)
from lune.pipeline.benchmark import (
    collect_interruption_samples,
    collect_latency_samples,
    evaluate_interruption,
)
from lune.tts.contracts import TTSBackendError
from tests.pipeline.conftest import NATIVE_WINDOW, RecordingOutputDevice
from tests.pipeline.harness import (
    SESSION_ID,
    Harness,
    ScriptedTTSBackend,
    build_harness,
)

HEADSET = DeviceSnapshot(
    input=DeviceInfo(uid="mic-1", name="Headset", is_builtin=False),
    output=DeviceInfo(uid="out-1", name="Headset", is_builtin=False),
)
BUILT_IN = DeviceSnapshot(
    input=DeviceInfo(uid="mic-1", name="Headset", is_builtin=False),
    output=DeviceInfo(uid="out-2", name="MacBook Speakers", is_builtin=True),
)


def text(value: str) -> Callable[[int, str], ProviderStreamFrame]:
    return lambda generation, attempt: GenerationLLMTextFrame(
        text=value,
        generation_id=generation,
        attempt_id=attempt,
    )


def terminal(status: str = "completed") -> Callable[[int, str], ProviderStreamFrame]:
    return lambda generation, attempt: ProviderTerminalFrame(
        generation_id=generation,
        attempt_id=attempt,
        status=status,  # type: ignore[arg-type]
    )


def tool(name: str, arguments: dict[str, object]) -> Callable[[int, str], ProviderStreamFrame]:
    return lambda generation, attempt: GenerationFunctionCallFrame(
        function_name=name,
        tool_call_id="call-1",
        arguments=json.dumps(arguments),
        generation_id=generation,
        attempt_id=attempt,
    )


async def listen(harness: Harness) -> None:
    await harness.pipeline.session.start()
    await harness.pipeline.session.apply_default_devices(HEADSET)
    assert harness.pipeline.session.set_microphone(True) == "listening"


async def wait_for_state(harness: Harness, state: str, *, ticks: int = 50) -> None:
    for _ in range(ticks):
        if harness.pipeline.session.state == state:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"session never reached {state}: {harness.pipeline.session.state}")


def stored_messages(harness: Harness) -> list[tuple[str, str]]:
    turns = harness.store.unsummarized_complete_turns(SESSION_ID)
    return [(message.role, message.content) for turn in turns for message in turn.messages]


@pytest.mark.asyncio
async def test_one_complete_turn_reaches_playback_and_local_storage(tmp_path: Path) -> None:
    harness = build_harness(
        tmp_path,
        terra_scripts=((text("你好。"), text("今天好嗎\uff1f"), terminal()),),
    )
    await listen(harness)

    await harness.speak_utterance()
    assert len(harness.stt.requests) == 1
    assert harness.pipeline.session.state == "thinking"

    await harness.stt.emit_final("早安")
    assert await harness.pipeline.session.wait_for_turns() is True

    assert stored_messages(harness) == [
        ("user", "早安"),
        ("assistant", "你好。今天好嗎\uff1f"),
    ]
    device = harness.device
    assert isinstance(device, RecordingOutputDevice)
    assert len(device.written) == 4
    assert set(device.generations()) == {0}
    assert harness.pipeline.session.state == "listening"

    report = harness.pipeline.session.reports[-1]
    assert report.outcome == "completed"
    assert report.models_attempted == ("gpt-5.6-terra",)
    await harness.pipeline.session.close()


@pytest.mark.asyncio
async def test_typed_text_can_skip_speech_without_losing_the_completed_turn(tmp_path: Path) -> None:
    harness = build_harness(tmp_path, terra_scripts=((text("文字回覆。"), terminal()),))
    await listen(harness)

    submitted = await harness.pipeline.session.submit_text("請用文字回覆", speak_text=False)
    assert submitted == "thinking"
    assert await harness.pipeline.session.wait_for_turns() is True

    assert harness.backend.requests == []
    assert stored_messages(harness) == [
        ("user", "請用文字回覆"),
        ("assistant", "文字回覆。"),
    ]
    assert harness.pipeline.session.state == "listening"
    await harness.pipeline.session.close()


@pytest.mark.asyncio
async def test_typed_input_interrupts_an_active_response_through_the_central_fence(
    tmp_path: Path,
) -> None:
    resume = asyncio.Event()
    harness = build_harness(
        tmp_path,
        terra_scripts=(
            (text("第一個回答。"), text("不該留下。"), terminal()),
            (text("新的文字回答。"), terminal()),
        ),
        backend=ScriptedTTSBackend(chunks=6, pause=resume.wait),
    )
    await listen(harness)
    await harness.speak_utterance()
    await harness.stt.emit_final("先說一件事")
    await wait_for_state(harness, "speaking")

    submitted = await harness.pipeline.session.submit_text("等等，改用文字", speak_text=False)
    assert submitted == "thinking"
    assert harness.pipeline.coordinator.cancel_events[-1].reason == "text_barge_in"
    assert harness.pipeline.session.generation_id == 1

    resume.set()
    assert await harness.pipeline.session.wait_for_turns() is True
    assert stored_messages(harness) == [
        ("user", "等等，改用文字"),
        ("assistant", "新的文字回答。"),
    ]
    await harness.pipeline.session.close()


@pytest.mark.asyncio
async def test_the_end_to_end_clock_starts_at_the_last_voiced_sample(tmp_path: Path) -> None:
    harness = build_harness(tmp_path, terra_scripts=((text("好。"), terminal()),))
    await listen(harness)
    await harness.speak_utterance()
    await harness.stt.emit_final("早安")
    await harness.pipeline.session.wait_for_turns()

    samples = collect_latency_samples(harness.pipeline.session, harness.pipeline.playback)
    assert len(samples) == 1
    assert samples[0].delivered is True
    latency = samples[0].latency_ms
    assert latency is not None
    # The clock starts at the last voiced sample, so the fixed 350 ms of
    # end-of-turn silence is inside the budget; the fixture adds nothing else.
    assert 350.0 <= latency < 400.0
    await harness.pipeline.session.close()


@pytest.mark.asyncio
async def test_a_barge_in_stops_output_and_leaves_nothing_behind(tmp_path: Path) -> None:
    resume = asyncio.Event()
    backend = ScriptedTTSBackend(chunks=4, pause=resume.wait)
    harness = build_harness(
        tmp_path,
        terra_scripts=(
            (
                text("第一句。"),
                tool(
                    "propose_memory",
                    {
                        "content": "漢堡喜歡散步",
                        "category": "stable_preference",
                        "importance": 0.6,
                    },
                ),
                text("第二句。"),
                terminal(),
            ),
        ),
        backend=backend,
    )
    await listen(harness)
    await harness.speak_utterance()
    await harness.stt.emit_final("早安")

    await wait_for_state(harness, "speaking")
    assert harness.pipeline.proposals.pending_count == 1

    device = harness.device
    assert isinstance(device, RecordingOutputDevice)
    written_before = len(device.written)

    await harness.start_barge_in()

    assert harness.pipeline.session.generation_id == 1
    assert harness.fence.interrupted == [0]
    assert harness.backend.cancelled == [0]

    resume.set()
    await harness.pipeline.session.wait_for_turns()

    assert device.generations()[written_before:] == []
    assert stored_messages(harness) == []
    assert harness.store.list_memories() == ()
    assert harness.pipeline.session.reports[-1].outcome == "cancelled"
    await harness.pipeline.session.close()


@pytest.mark.asyncio
async def test_the_interrupting_speech_becomes_the_next_utterance(tmp_path: Path) -> None:
    resume = asyncio.Event()
    harness = build_harness(
        tmp_path,
        terra_scripts=(
            (text("第一句。"), terminal()),
            (text("第二句。"), terminal()),
        ),
        backend=ScriptedTTSBackend(chunks=4, pause=resume.wait),
    )
    await listen(harness)
    await harness.speak_utterance()
    await harness.stt.emit_final("早安")
    await wait_for_state(harness, "speaking")

    barge_in_frames = NATIVE_WINDOW * 12
    await harness.start_barge_in(voiced_frames=barge_in_frames)
    assert harness.pipeline.session.generation_id == 1

    resume.set()
    await harness.pipeline.session.wait_for_turns()
    await harness.feed(5_600 + NATIVE_WINDOW, voiced=False)

    assert len(harness.stt.requests) == 2
    captured = harness.stt.requests[-1]
    assert captured.generation_id == 1
    samples = np.frombuffer(captured.audio.pcm, dtype="<i2")
    assert int(np.count_nonzero(samples)) >= barge_in_frames
    await harness.pipeline.session.close()


@pytest.mark.asyncio
async def test_a_speech_recognition_failure_is_visible_and_recoverable(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    await listen(harness)
    await harness.speak_utterance()

    await harness.stt.emit_failure("setup_required")
    assert harness.pipeline.session.state == "setup_required"
    assert stored_messages(harness) == []

    await harness.stt.emit_failure("inference_failed")
    assert harness.pipeline.session.state == "error"
    await harness.pipeline.session.close()


@pytest.mark.asyncio
async def test_a_stuck_recognizer_is_cancelled_by_the_watchdog(tmp_path: Path) -> None:
    harness = build_harness(tmp_path, stt_timeout_s=0.02)
    await listen(harness)
    await harness.speak_utterance()
    assert harness.pipeline.session.state == "thinking"

    await asyncio.sleep(0.05)

    assert harness.pipeline.session.generation_id == 1
    assert harness.pipeline.session.state == "error"
    assert harness.fence.interrupted == [0]
    await harness.pipeline.session.close()


@pytest.mark.asyncio
async def test_a_failing_voice_backend_fails_the_turn_without_storing_it(tmp_path: Path) -> None:
    harness = build_harness(
        tmp_path,
        terra_scripts=((text("你好。"), terminal()),),
        backend=ScriptedTTSBackend(error=TTSBackendError("synthesis_failed")),
    )
    await listen(harness)
    await harness.speak_utterance()
    await harness.stt.emit_final("早安")
    await harness.pipeline.session.wait_for_turns()

    assert harness.pipeline.session.state == "error"
    assert harness.pipeline.session.reports[-1].outcome == "error"
    assert stored_messages(harness) == []
    await harness.pipeline.session.close()


@pytest.mark.asyncio
async def test_an_overflowing_output_queue_cancels_instead_of_growing(tmp_path: Path) -> None:
    resume = asyncio.Event()
    harness = build_harness(
        tmp_path,
        terra_scripts=((text("你好。"), terminal()),),
        backend=ScriptedTTSBackend(chunks=8, pause=resume.wait),
        playback_capacity=1,
    )
    await listen(harness)
    await harness.speak_utterance()
    await harness.stt.emit_final("早安")

    for _ in range(50):
        await asyncio.sleep(0)
        if harness.pipeline.session.generation_id == 1:
            break
    resume.set()
    await harness.pipeline.session.wait_for_turns()

    assert harness.pipeline.playback.health().overflowed is True
    assert harness.pipeline.session.generation_id == 1
    assert stored_messages(harness) == []
    await harness.pipeline.session.close()


@pytest.mark.asyncio
async def test_a_locked_budget_stops_the_turn_before_any_cloud_request(tmp_path: Path) -> None:
    ledger = BudgetLedger(BudgetConfig(fallback_at_twd=0.0, lock_at_twd=0.001))
    harness = build_harness(tmp_path, ledger=ledger)
    await listen(harness)
    await harness.speak_utterance()
    await harness.stt.emit_final("早安")
    await harness.pipeline.session.wait_for_turns()

    assert harness.pipeline.session.budget_locked is True
    assert harness.pipeline.session.state == "budget_locked"
    assert harness.pipeline.session.reports[-1].outcome == "budget_locked"
    assert stored_messages(harness) == []
    await harness.pipeline.session.close()


@pytest.mark.asyncio
async def test_an_accepted_tool_call_is_committed_only_with_the_turn(tmp_path: Path) -> None:
    harness = build_harness(
        tmp_path,
        terra_scripts=(
            (
                tool(
                    "propose_memory",
                    {
                        "content": "漢堡每週三上瑜伽課",
                        "category": "explicit_plan",
                        "importance": 0.8,
                    },
                ),
                tool("propose_affinity", {"delta": 1, "reason": "warm exchange"}),
                text("好的。"),
                terminal(),
            ),
        ),
    )
    await listen(harness)
    await harness.speak_utterance()
    await harness.stt.emit_final("記住我每週三上瑜伽課")
    await harness.pipeline.session.wait_for_turns()

    memories = harness.store.list_memories()
    assert [memory.content for memory in memories] == ["漢堡每週三上瑜伽課"]
    assert harness.store.affinity() == 51
    assert harness.pipeline.proposals.pending_count == 0
    await harness.pipeline.session.close()


@pytest.mark.asyncio
async def test_a_second_tool_call_of_the_same_kind_is_rejected(tmp_path: Path) -> None:
    payload = {"content": "漢堡喜歡海邊", "category": "stable_preference", "importance": 0.5}
    harness = build_harness(
        tmp_path,
        terra_scripts=(
            (
                tool("propose_memory", payload),
                tool("propose_memory", {**payload, "content": "漢堡也喜歡山上"}),
                text("知道了。"),
                terminal(),
            ),
        ),
    )
    await listen(harness)
    await harness.speak_utterance()
    await harness.stt.emit_final("記住這件事")
    await harness.pipeline.session.wait_for_turns()

    assert [memory.content for memory in harness.store.list_memories()] == ["漢堡喜歡海邊"]
    assert harness.pipeline.session.reports[-1].rejected_tool_calls == 1
    await harness.pipeline.session.close()


@pytest.mark.asyncio
async def test_switching_to_built_in_speakers_cancels_and_pauses(tmp_path: Path) -> None:
    harness = build_harness(tmp_path, terra_scripts=((text("你好。"), terminal()),))
    await listen(harness)
    await harness.speak_utterance()

    state = await harness.pipeline.session.apply_default_devices(BUILT_IN)

    assert state == "paused_unsafe_output"
    assert harness.pipeline.session.generation_id == 1
    assert harness.fence.interrupted == [0]
    assert harness.pipeline.turn_gate.turn_active is False
    await harness.pipeline.session.close()


@pytest.mark.asyncio
async def test_the_session_recovers_and_serves_a_second_turn(tmp_path: Path) -> None:
    harness = build_harness(
        tmp_path,
        terra_scripts=(
            (text("第一句。"), terminal()),
            (text("第二句。"), terminal()),
        ),
    )
    await listen(harness)
    await harness.speak_utterance()
    await harness.stt.emit_final("第一個問題")
    await harness.pipeline.session.wait_for_turns()

    await harness.speak_utterance()
    await harness.stt.emit_final("第二個問題")
    await harness.pipeline.session.wait_for_turns()

    assert stored_messages(harness) == [
        ("user", "第一個問題"),
        ("assistant", "第一句。"),
        ("user", "第二個問題"),
        ("assistant", "第二句。"),
    ]
    assert harness.pipeline.session.state == "listening"
    await harness.pipeline.session.close()


@pytest.mark.asyncio
async def test_a_turn_records_the_memories_that_reached_the_model(tmp_path: Path) -> None:
    harness = build_harness(
        tmp_path,
        terra_scripts=(
            (
                tool(
                    "propose_memory",
                    {
                        "content": "漢堡每週三上瑜伽課",
                        "category": "explicit_plan",
                        "importance": 0.8,
                    },
                ),
                text("記住了。"),
                terminal(),
            ),
            (text("你要上瑜伽課。"), terminal()),
        ),
    )
    await listen(harness)
    await harness.speak_utterance()
    await harness.stt.emit_final("記住我每週三上瑜伽課")
    await harness.pipeline.session.wait_for_turns()

    memories = harness.store.list_memories()
    assert len(memories) == 1

    await harness.speak_utterance()
    await harness.stt.emit_final("我週三要做什麼")
    await harness.pipeline.session.wait_for_turns()

    # The first turn ran before that memory existed, so only the second one can
    # name it, and only on the answer the memory was actually handed to.
    assert [
        (message.role, message.memory_ids)
        for message in harness.store.conversation_messages(SESSION_ID)
    ] == [
        ("user", ()),
        ("assistant", ()),
        ("user", ()),
        ("assistant", (memories[0].id,)),
    ]
    await harness.pipeline.session.close()


@pytest.mark.asyncio
async def test_usage_is_settled_against_the_local_month(tmp_path: Path) -> None:
    harness = build_harness(tmp_path, terra_scripts=((text("你好。"), terminal()),))
    await listen(harness)
    await harness.speak_utterance()
    await harness.stt.emit_final("早安")
    await harness.pipeline.session.wait_for_turns()

    settled = harness.ledger.settled_attempts
    assert len(settled) == 1
    assert settled[0].reservation.model == "gpt-5.6-terra"
    assert harness.ledger.active_reservations == ()
    assert harness.ledger.total_with_reservations(datetime.now(UTC)) > 0
    await harness.pipeline.session.close()


@pytest.mark.asyncio
async def test_a_confirmed_barge_in_meets_the_two_hundred_millisecond_budget(
    tmp_path: Path,
) -> None:
    resume = asyncio.Event()
    harness = build_harness(
        tmp_path,
        terra_scripts=((text("第一句。"), text("第二句。"), terminal()),),
        backend=ScriptedTTSBackend(chunks=6, pause=resume.wait),
    )
    await listen(harness)
    await harness.speak_utterance()
    await harness.stt.emit_final("早安")
    await wait_for_state(harness, "speaking")

    device = harness.device
    assert isinstance(device, RecordingOutputDevice)
    await harness.start_barge_in()
    written_after_fence = len(device.written)

    resume.set()
    await harness.pipeline.session.wait_for_turns()

    late = sum(1 for chunk in device.written[written_after_fence:] if chunk.generation_id == 0)
    samples = collect_interruption_samples(
        harness.pipeline.coordinator,
        late_events={0: late},
    )
    gate = evaluate_interruption(samples)

    assert len(samples) == 1
    assert gate.passed is True
    assert gate.late_events == 0
    assert gate.max_ms is not None
    assert gate.max_ms <= 200.0
    await harness.pipeline.session.close()


@pytest.mark.asyncio
async def test_only_sentences_that_reached_the_device_are_reported_as_played(
    tmp_path: Path,
) -> None:
    harness = build_harness(
        tmp_path,
        terra_scripts=((text("第一句。"), text("第二句。"), text("第三句。"), terminal()),),
    )
    await listen(harness)
    await harness.speak_utterance()
    await harness.stt.emit_final("早安")
    await harness.pipeline.session.wait_for_turns()

    report = harness.pipeline.session.reports[-1]
    assert report.sentences_played == 3
    assert len(harness.backend.requests) == 3
    device = harness.device
    assert isinstance(device, RecordingOutputDevice)
    assert len(device.written) == 6
    await harness.pipeline.session.close()


@pytest.mark.asyncio
async def test_a_stale_recognition_result_cannot_disarm_the_current_watchdog(
    tmp_path: Path,
) -> None:
    harness = build_harness(tmp_path, stt_timeout_s=0.05)
    await listen(harness)
    await harness.speak_utterance()
    assert len(harness.stt.requests) == 1

    await harness.pipeline.coordinator.cancel("device_changed")
    await harness.speak_utterance()
    assert len(harness.stt.requests) == 2
    assert harness.stt.requests[-1].generation_id == 1

    await harness.stt.emit_final("舊世代的結果", generation_id=0)
    assert harness.pipeline.session.reports == ()

    await asyncio.sleep(0.1)
    assert harness.pipeline.session.generation_id == 2
    assert harness.pipeline.session.state == "error"
    await harness.pipeline.session.close()


@pytest.mark.asyncio
async def test_a_cancelled_turn_leaves_the_session_listening_again(tmp_path: Path) -> None:
    resume = asyncio.Event()
    harness = build_harness(
        tmp_path,
        terra_scripts=((text("第一句。"), text("第二句。"), terminal()),),
        backend=ScriptedTTSBackend(chunks=6, pause=resume.wait),
    )
    await listen(harness)
    await harness.speak_utterance()
    await harness.stt.emit_final("早安")
    await wait_for_state(harness, "speaking")

    await harness.pipeline.coordinator.cancel("error")

    # The interrupted turn owns the "speaking" state, so cancelling it has to
    # give the microphone back without waiting for that task to unwind.
    assert harness.pipeline.session.state == "listening"

    resume.set()
    await harness.pipeline.session.wait_for_turns()
    assert harness.pipeline.session.state == "listening"
    assert stored_messages(harness) == []
    await harness.pipeline.session.close()


@pytest.mark.asyncio
async def test_an_unexpected_provider_fault_ends_the_turn_and_keeps_listening(
    tmp_path: Path,
) -> None:
    # An exhausted script deque raises inside the provider, which is what a
    # crashed local worker looks like from the session's side.
    harness = build_harness(tmp_path, terra_scripts=())
    await listen(harness)

    await harness.speak_utterance()
    await harness.stt.emit_final("早安")
    assert await harness.pipeline.session.wait_for_turns() is True

    report = harness.pipeline.session.reports[-1]
    assert report.outcome == "error"
    assert report.sentences_played == 0
    assert stored_messages(harness) == []
    assert harness.pipeline.session.state == "error"

    # The microphone must not stay stuck behind a turn that will never answer.
    await harness.speak_utterance()
    assert len(harness.stt.requests) == 2
    await harness.pipeline.session.close()


class StubSampleClock:
    """Answer with the capture time the device would have reported."""

    def __init__(self, at: float) -> None:
        self.at = at
        self.asked: list[int] = []

    def wall_time_of_sample(self, sample: int) -> float | None:
        self.asked.append(sample)
        return self.at


@pytest.mark.asyncio
async def test_the_end_to_end_clock_uses_capture_time_not_processing_time(
    tmp_path: Path,
) -> None:
    # Subtracting the trailing silence from "now" hides however far the pipeline
    # trails the microphone, and it can only make the measured latency shorter.
    clock = StubSampleClock(at=1_234.5)
    harness = build_harness(
        tmp_path,
        terra_scripts=((text("你好。"), terminal()),),
        sample_clock=clock,
    )
    await listen(harness)

    await harness.speak_utterance()
    generation = harness.pipeline.session.generation_id

    assert harness.pipeline.session.speech_end_at(generation) == 1_234.5
    assert clock.asked and clock.asked[-1] > 0
    await harness.pipeline.session.close()
