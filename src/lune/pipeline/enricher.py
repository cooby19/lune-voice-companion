"""Aggregate local context and enrich it with bounded retrieved memories."""

from __future__ import annotations

from dataclasses import dataclass

from lune.llm.prompt import ConversationMessage, PromptContext
from lune.memory.embedding import (
    E5MemoryRetriever,
    E5SetupRequired,
    MemorySearchResult,
    memory_contents,
)
from lune.memory.store import MemoryStore

MAX_RECENT_TURNS = 12
MAX_MEMORIES = 5
MAX_MEMORY_CHARACTERS = 1_200


@dataclass(frozen=True, slots=True)
class EnrichedContext:
    """One turn's prompt context, alongside what retrieval contributed to it.

    The identifiers travel separately from the text because they serve a
    different consumer: the prompt only ever needs the memory content, while the
    interface needs to name which stored memories were in front of the model.
    """

    context: PromptContext
    memory_ids: tuple[str, ...]


class ContextEnricher:
    """Select the minimum local text one cloud request is allowed to carry.

    ``MemoryStore.build_prompt_context`` only sees completed turns, so the turn
    being answered right now would never reach the model. This aggregates the
    stored history and appends the live user message, then adds retrieval on top.
    Retrieval is best-effort: a missing local E5 model degrades the answer, it
    does not fail the turn.
    """

    def __init__(
        self,
        store: MemoryStore,
        retriever: E5MemoryRetriever | None = None,
        *,
        max_recent_turns: int = MAX_RECENT_TURNS,
        max_memories: int = MAX_MEMORIES,
        max_memory_characters: int = MAX_MEMORY_CHARACTERS,
    ) -> None:
        if not 1 <= max_recent_turns <= MAX_RECENT_TURNS:
            raise ValueError("recent turn window must be between one and twelve")
        if not 1 <= max_memories <= MAX_MEMORIES:
            raise ValueError("memory count must be between one and five")
        if not 1 <= max_memory_characters <= MAX_MEMORY_CHARACTERS:
            raise ValueError("memory characters must be between one and 1,200")
        self._store = store
        self._retriever = retriever
        self._max_recent_turns = max_recent_turns
        self._max_memories = max_memories
        self._max_memory_characters = max_memory_characters
        self._retrieval_available = retriever is not None

    @property
    def retrieval_available(self) -> bool:
        """False once the local encoder proved unusable for this session."""

        return self._retrieval_available

    def enrich(self, session_id: str, *, user_text: str) -> EnrichedContext:
        history = self._store.unsummarized_complete_turns(session_id)[-self._max_recent_turns :]
        messages = tuple(message for turn in history for message in turn.messages)
        summary = self._store.get_summary(session_id)
        retrieved = self._memories(user_text)
        return EnrichedContext(
            context=PromptContext(
                recent_messages=(*messages, ConversationMessage("user", user_text)),
                summary=summary.content if summary is not None else None,
                relevant_memories=memory_contents(retrieved),
            ),
            memory_ids=tuple(result.id for result in retrieved),
        )

    def _memories(self, user_text: str) -> tuple[MemorySearchResult, ...]:
        if self._retriever is None or not self._retrieval_available:
            return ()
        try:
            results = self._retriever.search(
                user_text,
                top_k=self._max_memories,
                character_limit=self._max_memory_characters,
            )
        except E5SetupRequired:
            self._retrieval_available = False
            return ()
        return results
