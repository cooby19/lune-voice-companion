from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from lune.tts.circuit import TTSCircuitBreaker
from lune.tts.contracts import PCMChunk, TTSBackendError, TTSRequest
from lune.tts.router import TTSRouterService


class FakeBackend:
    def __init__(self, events: list[PCMChunk | TTSBackendError]) -> None:
        self.events = events
        self.requests: list[TTSRequest] = []
        self.cancelled: list[int] = []
        self.closed = False

    async def synthesize(self, request: TTSRequest) -> AsyncIterator[PCMChunk]:
        self.requests.append(request)
        for event in self.events:
            if isinstance(event, TTSBackendError):
                raise event
            yield event

    async def cancel(self, generation_id: int) -> None:
        self.cancelled.append(generation_id)

    async def close(self) -> None:
        self.closed = True


def test_circuit_breaker_opens_and_success_resets_failures() -> None:
    breaker = TTSCircuitBreaker(failure_threshold=2)
    breaker.record_failure()
    assert breaker.allows_request
    breaker.record_success()
    assert breaker.consecutive_failures == 0
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state == "open"
    assert not breaker.allows_request


def test_rebuild_failure_opens_circuit_immediately() -> None:
    breaker = TTSCircuitBreaker(failure_threshold=3)
    breaker.record_failure()
    breaker.record_rebuild_failure()
    assert breaker.state == "open"


@pytest.mark.asyncio
async def test_router_falls_back_only_before_first_gpt_chunk() -> None:
    av_chunk = PCMChunk(2, 22_050, 1, b"\x02\x00")
    avspeech = FakeBackend([av_chunk])
    gpt = FakeBackend([TTSBackendError("backend_unavailable")])
    router = TTSRouterService(
        avspeech=avspeech,
        gpt_sovits=gpt,
        preferred_backend="gpt_sovits",
    )

    result = [chunk async for chunk in router.synthesize(TTSRequest("r", 2, "hello"))]

    assert result == [av_chunk]
    assert router.was_degraded(2)
    assert len(gpt.requests) == 1
    assert len(avspeech.requests) == 1


@pytest.mark.asyncio
async def test_router_never_changes_voice_after_pcm_started() -> None:
    gpt_chunk = PCMChunk(3, 32_000, 1, b"\x01\x00")
    avspeech = FakeBackend([PCMChunk(3, 22_050, 1, b"\x02\x00")])
    gpt = FakeBackend([gpt_chunk, TTSBackendError("synthesis_failed")])
    router = TTSRouterService(
        avspeech=avspeech,
        gpt_sovits=gpt,
        preferred_backend="gpt_sovits",
    )
    emitted: list[PCMChunk] = []

    with pytest.raises(TTSBackendError, match="synthesis_failed"):
        async for chunk in router.synthesize(TTSRequest("r", 3, "hello")):
            emitted.append(chunk)

    assert emitted == [gpt_chunk]
    assert avspeech.requests == []


@pytest.mark.asyncio
async def test_router_cancel_and_close_target_selected_backends() -> None:
    avspeech = FakeBackend([])
    gpt = FakeBackend([])
    router = TTSRouterService(
        avspeech=avspeech,
        gpt_sovits=gpt,
        preferred_backend="gpt_sovits",
    )

    await router.cancel(99)
    await router.close()

    assert avspeech.closed and gpt.closed
