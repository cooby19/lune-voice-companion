from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import numpy.typing as npt

from lune.memory.embedding import E5_MODEL_ID, E5_MODEL_REVISION, E5MemoryRetriever
from lune.memory.proposals import AffinityProposal, MemoryProposal, ProposalHost
from lune.memory.store import EMBEDDING_DIMENSIONS, MemoryStore

type FloatArray = npt.NDArray[np.float32]


class FixedEncoder:
    model_id = E5_MODEL_ID
    revision = E5_MODEL_REVISION

    def encode_query(self, query: str) -> FloatArray:
        return self.encode_passages((query,))[0]

    def encode_passages(self, passages: Sequence[str]) -> FloatArray:
        vector = np.zeros((len(passages), EMBEDDING_DIMENSIONS), dtype=np.float32)
        vector[:, 0] = 1.0
        return vector


def _pending_turn(store: MemoryStore, session_id: str, generation_id: int) -> str:
    turn_id = store.begin_turn(session_id, generation_id)
    store.accept_user_transcript(turn_id, f"user-{generation_id}")
    return turn_id


def test_duplicate_memory_proposal_and_cancelled_generation_do_not_write(
    store: MemoryStore,
) -> None:
    session_id = store.start_session("proposal-session")
    turn_id = _pending_turn(store, session_id, 7)
    host = ProposalHost(store, E5MemoryRetriever(store, FixedEncoder()))
    first = MemoryProposal(
        "proposal-1",
        7,
        session_id,
        turn_id,
        "explicit_request",
        0.8,
        "Remember tea",
    )
    duplicate_content = MemoryProposal(
        "proposal-2",
        7,
        session_id,
        turn_id,
        "explicit_request",
        0.8,
        "  remember   tea  ",
    )
    assert host.propose_memory(first)
    assert not host.propose_memory(first)
    assert host.propose_memory(duplicate_content)

    results = host.commit_generation(7, is_generation_current=lambda value: value == 7)

    assert [result.status for result in results] == ["committed", "duplicate"]
    assert len(store.list_memories()) == 1
    cancelled_turn = _pending_turn(store, session_id, 8)
    assert host.propose_memory(
        MemoryProposal(
            "proposal-cancel",
            8,
            session_id,
            cancelled_turn,
            "explicit_plan",
            0.6,
            "A future plan",
        )
    )
    cancelled = host.commit_generation(8, is_generation_current=lambda _value: False)
    assert cancelled[0].status == "cancelled"
    assert len(store.list_memories()) == 1


def test_affinity_has_one_event_per_turn_session_cap_and_audit(store: MemoryStore) -> None:
    session_id = store.start_session("affinity-session")
    host = ProposalHost(store, E5MemoryRetriever(store, FixedEncoder()))
    statuses: list[str] = []
    for generation_id in range(1, 5):
        turn_id = _pending_turn(store, session_id, generation_id)
        proposal = AffinityProposal(
            f"affinity-{generation_id}",
            generation_id,
            session_id,
            turn_id,
            1,
            "trust",
        )
        assert host.propose_affinity(proposal)
        assert not host.propose_affinity(proposal)
        statuses.extend(
            result.status
            for result in host.commit_generation(
                generation_id,
                is_generation_current=lambda value, expected=generation_id: value == expected,
            )
        )

    assert statuses == ["committed", "committed", "committed", "rejected_limit"]
    assert store.affinity() == 53
    events = store.relationship_events(session_id)
    assert len(events) == 3
    assert [event.resulting_affinity for event in events] == [51, 52, 53]
    assert sum(event.delta for event in events) == 3
    repeated = host.propose_affinity(
        AffinityProposal("affinity-repeat", 1, session_id, events[0].turn_id, -1, "repair")
    )
    assert repeated
    assert host.commit_generation(1, is_generation_current=lambda value: value == 1)[0].status == (
        "rejected_limit"
    )


def test_affinity_never_exceeds_global_bounds(store: MemoryStore) -> None:
    event_number = 0
    for session_number in range(17):
        session_id = store.start_session(f"bound-session-{session_number}")
        count = 2 if session_number == 16 else 3
        for _offset in range(count):
            event_number += 1
            turn_id = _pending_turn(store, session_id, event_number)
            assert store.apply_affinity(
                event_id=f"bound-event-{event_number}",
                session_id=session_id,
                turn_id=turn_id,
                generation_id=event_number,
                delta=1,
                reason="trust",
            )
    assert event_number == 50
    assert store.affinity() == 100
    final_session = store.start_session("bound-final-session")
    final_turn = _pending_turn(store, final_session, 51)
    assert (
        store.apply_affinity(
            event_id="bound-event-final",
            session_id=final_session,
            turn_id=final_turn,
            generation_id=51,
            delta=1,
            reason="trust",
        )
        is None
    )
    assert store.affinity() == 100
