"""Two-phase host validation for model-proposed memory and affinity changes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from lune.memory.embedding import E5MemoryRetriever
from lune.memory.store import MemoryStore

type ProposalStatus = Literal["committed", "duplicate", "rejected_limit", "cancelled"]
_MEMORY_CATEGORIES = frozenset(
    {"stable_preference", "important_person_or_event", "explicit_plan", "explicit_request"}
)


@dataclass(frozen=True, slots=True)
class MemoryProposal:
    proposal_id: str
    generation_id: int
    session_id: str
    turn_id: str
    category: str
    importance: float
    content: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class AffinityProposal:
    proposal_id: str
    generation_id: int
    session_id: str
    turn_id: str
    delta: int
    reason: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ProposalResult:
    proposal_id: str
    status: ProposalStatus


class ProposalHost:
    """Keep proposals transient until the generation is explicitly committed."""

    def __init__(self, store: MemoryStore, retriever: E5MemoryRetriever) -> None:
        self._store = store
        self._retriever = retriever
        self._memory: dict[str, MemoryProposal] = {}
        self._affinity: dict[str, AffinityProposal] = {}

    @property
    def pending_count(self) -> int:
        return len(self._memory) + len(self._affinity)

    def propose_memory(self, proposal: MemoryProposal) -> bool:
        self._validate_common(proposal.proposal_id, proposal.generation_id)
        if proposal.proposal_id in self._memory or proposal.proposal_id in self._affinity:
            return False
        if not self._store.turn_matches(
            proposal.turn_id, proposal.session_id, proposal.generation_id
        ):
            return False
        if not proposal.content.strip():
            raise ValueError("memory proposal cannot be empty")
        if proposal.category not in _MEMORY_CATEGORIES:
            raise ValueError("unsupported memory category")
        if not 0.0 <= proposal.importance <= 1.0:
            raise ValueError("memory importance must be between zero and one")
        self._memory[proposal.proposal_id] = proposal
        return True

    def propose_affinity(self, proposal: AffinityProposal) -> bool:
        self._validate_common(proposal.proposal_id, proposal.generation_id)
        if proposal.delta not in {-1, 1}:
            raise ValueError("affinity proposal delta must be exactly -1 or 1")
        if proposal.proposal_id in self._memory or proposal.proposal_id in self._affinity:
            return False
        if not self._store.turn_matches(
            proposal.turn_id, proposal.session_id, proposal.generation_id
        ):
            return False
        self._affinity[proposal.proposal_id] = proposal
        return True

    def cancel_generation(self, generation_id: int) -> int:
        memory_ids = [
            proposal_id
            for proposal_id, proposal in self._memory.items()
            if proposal.generation_id == generation_id
        ]
        affinity_ids = [
            proposal_id
            for proposal_id, proposal in self._affinity.items()
            if proposal.generation_id == generation_id
        ]
        proposal_ids = memory_ids + affinity_ids
        for proposal_id in proposal_ids:
            self._memory.pop(proposal_id, None)
            self._affinity.pop(proposal_id, None)
        return len(proposal_ids)

    def commit_generation(
        self,
        generation_id: int,
        *,
        is_generation_current: Callable[[int], bool],
    ) -> tuple[ProposalResult, ...]:
        memory = [item for item in self._memory.values() if item.generation_id == generation_id]
        affinity = [item for item in self._affinity.values() if item.generation_id == generation_id]
        if not is_generation_current(generation_id):
            self.cancel_generation(generation_id)
            return tuple(ProposalResult(item.proposal_id, "cancelled") for item in memory) + tuple(
                ProposalResult(item.proposal_id, "cancelled") for item in affinity
            )

        embedded = [
            (proposal, self._retriever.embed_passage(proposal.content)) for proposal in memory
        ]
        if not is_generation_current(generation_id):
            self.cancel_generation(generation_id)
            return tuple(ProposalResult(item.proposal_id, "cancelled") for item in memory) + tuple(
                ProposalResult(item.proposal_id, "cancelled") for item in affinity
            )

        results: list[ProposalResult] = []
        for memory_proposal, embedding in embedded:
            if not self._store.turn_matches(
                memory_proposal.turn_id,
                memory_proposal.session_id,
                memory_proposal.generation_id,
            ):
                results.append(ProposalResult(memory_proposal.proposal_id, "cancelled"))
                continue
            stored = self._store.add_memory(
                memory_id=memory_proposal.proposal_id,
                content=memory_proposal.content,
                category=memory_proposal.category,
                importance=memory_proposal.importance,
                embedding=embedding,
                embedding_model=self._retriever.encoder.model_id,
                embedding_revision=self._retriever.encoder.revision,
                source_turn_id=memory_proposal.turn_id,
            )
            results.append(
                ProposalResult(memory_proposal.proposal_id, "committed" if stored else "duplicate")
            )
        for affinity_proposal in affinity:
            if not self._store.turn_matches(
                affinity_proposal.turn_id,
                affinity_proposal.session_id,
                affinity_proposal.generation_id,
            ):
                results.append(ProposalResult(affinity_proposal.proposal_id, "cancelled"))
                continue
            event = self._store.apply_affinity(
                event_id=affinity_proposal.proposal_id,
                session_id=affinity_proposal.session_id,
                turn_id=affinity_proposal.turn_id,
                generation_id=affinity_proposal.generation_id,
                delta=affinity_proposal.delta,
                reason=affinity_proposal.reason,
            )
            results.append(
                ProposalResult(
                    affinity_proposal.proposal_id,
                    "committed" if event is not None else "rejected_limit",
                )
            )
        self.cancel_generation(generation_id)
        return tuple(results)

    @staticmethod
    def _validate_common(proposal_id: str, generation_id: int) -> None:
        if (
            not proposal_id
            or len(proposal_id) > 128
            or any(character.isspace() for character in proposal_id)
        ):
            raise ValueError("proposal ID must contain 1 to 128 non-whitespace characters")
        if generation_id < 0:
            raise ValueError("generation ID cannot be negative")
