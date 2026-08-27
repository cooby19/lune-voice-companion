"""Generation-fenced rolling summary orchestration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from lune.llm.contracts import ModelName
from lune.memory.store import MemoryStore, StoredTurn, SummaryCoverage


@dataclass(frozen=True, slots=True)
class SummaryRequest:
    generation_id: int
    turns: tuple[StoredTurn, ...] = field(repr=False)
    previous_summary: str | None = field(default=None, repr=False)
    model: ModelName = "gpt-5.6-luna"


class SummaryBackend(Protocol):
    async def __call__(self, request: SummaryRequest) -> str: ...


class RollingSummaryManager:
    def __init__(self, store: MemoryStore, backend: SummaryBackend) -> None:
        self._store = store
        self._backend = backend

    async def maybe_summarize(
        self,
        session_id: str,
        *,
        generation_id: int,
        is_generation_current: Callable[[int], bool],
    ) -> SummaryCoverage | None:
        if generation_id < 0:
            raise ValueError("generation ID cannot be negative")
        unsummarized = self._store.unsummarized_complete_turns(session_id)
        if len(unsummarized) <= 12 or not is_generation_current(generation_id):
            return None
        chunk = unsummarized[:4]
        previous = self._store.get_summary(session_id)
        content = await self._backend(
            SummaryRequest(
                generation_id=generation_id,
                turns=chunk,
                previous_summary=previous.content if previous is not None else None,
            )
        )
        if not is_generation_current(generation_id):
            return None
        return self._store.advance_summary(session_id, chunk, content)
