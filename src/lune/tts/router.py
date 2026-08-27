"""Whole-utterance TTS routing with AVSpeech fallback."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Literal

from lune.tts.contracts import PCMChunk, StreamingTTSBackend, TTSBackendError, TTSRequest

type BackendName = Literal["avspeech", "gpt_sovits"]


class TTSRouterService:
    """Select one voice per utterance and never switch after PCM starts."""

    def __init__(
        self,
        *,
        avspeech: StreamingTTSBackend,
        gpt_sovits: StreamingTTSBackend | None = None,
        preferred_backend: BackendName = "avspeech",
    ) -> None:
        self._avspeech = avspeech
        self._gpt_sovits = gpt_sovits
        self._preferred = preferred_backend
        self._active: dict[int, StreamingTTSBackend] = {}
        self._closed = False
        self._degraded_generations: set[int] = set()

    @property
    def preferred_backend(self) -> BackendName:
        return self._preferred

    def was_degraded(self, generation_id: int) -> bool:
        return generation_id in self._degraded_generations

    async def synthesize(self, request: TTSRequest) -> AsyncIterator[PCMChunk]:
        if self._closed:
            raise TTSBackendError("backend_unavailable")
        primary = (
            self._gpt_sovits
            if self._preferred == "gpt_sovits" and self._gpt_sovits is not None
            else self._avspeech
        )
        self._active[request.generation_id] = primary
        emitted = False
        fallback = False
        try:
            async for chunk in primary.synthesize(request):
                emitted = True
                yield chunk
            return
        except TTSBackendError as error:
            if primary is self._avspeech or emitted or error.code == "cancelled":
                raise
            fallback = True
        finally:
            self._active.pop(request.generation_id, None)

        # GPT failed before producing audio: restart the complete utterance with
        # the system voice rather than changing voices mid-sentence.
        if not fallback:
            return
        self._degraded_generations.add(request.generation_id)
        self._active[request.generation_id] = self._avspeech
        try:
            async for chunk in self._avspeech.synthesize(request):
                yield chunk
        finally:
            self._active.pop(request.generation_id, None)

    async def cancel(self, generation_id: int) -> None:
        backend = self._active.get(generation_id)
        if backend is not None:
            await backend.cancel(generation_id)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._avspeech.close()
        if self._gpt_sovits is not None and self._gpt_sovits is not self._avspeech:
            await self._gpt_sovits.close()
        self._active.clear()
