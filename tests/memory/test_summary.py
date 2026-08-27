from __future__ import annotations

from lune.memory.store import MemoryStore
from lune.memory.summary import RollingSummaryManager, SummaryRequest
from tests.memory.conftest import complete_turn


class RecordingSummarizer:
    def __init__(self) -> None:
        self.requests: list[SummaryRequest] = []
        self.cancel_on_call = False
        self.current = True

    async def __call__(self, request: SummaryRequest) -> str:
        self.requests.append(request)
        if self.cancel_on_call:
            self.current = False
        return f"summary-{len(self.requests)}"


async def test_thirteenth_turn_advances_continuous_nonoverlapping_coverage(
    store: MemoryStore,
) -> None:
    session_id = store.start_session("session-summary")
    for generation_id in range(1, 14):
        complete_turn(store, session_id, generation_id)
    backend = RecordingSummarizer()
    manager = RollingSummaryManager(store, backend)

    first = await manager.maybe_summarize(
        session_id,
        generation_id=13,
        is_generation_current=lambda generation_id: generation_id == 13,
    )

    assert first is not None
    assert (first.start_turn_sequence, first.end_turn_sequence, first.covered_turn_count) == (
        1,
        4,
        4,
    )
    assert [turn.sequence for turn in backend.requests[0].turns] == [1, 2, 3, 4]
    assert backend.requests[0].previous_summary is None
    assert backend.requests[0].model == "gpt-5.6-luna"
    prompt = store.build_prompt_context(session_id)
    assert prompt.summary == "summary-1"
    assert len(prompt.recent_messages) == 18

    for generation_id in range(14, 18):
        complete_turn(store, session_id, generation_id)
    second = await manager.maybe_summarize(
        session_id,
        generation_id=17,
        is_generation_current=lambda generation_id: generation_id == 17,
    )

    assert second is not None
    assert (second.start_turn_sequence, second.end_turn_sequence, second.covered_turn_count) == (
        1,
        8,
        8,
    )
    assert [turn.sequence for turn in backend.requests[1].turns] == [5, 6, 7, 8]
    assert backend.requests[1].previous_summary == "summary-1"


async def test_cancelled_summary_and_incomplete_turn_never_land(store: MemoryStore) -> None:
    session_id = store.start_session("session-cancel-summary")
    for generation_id in range(1, 14):
        complete_turn(store, session_id, generation_id)
    incomplete = store.begin_turn(session_id, 14)
    store.accept_user_transcript(incomplete, "final but incomplete")
    backend = RecordingSummarizer()
    backend.cancel_on_call = True
    manager = RollingSummaryManager(store, backend)

    result = await manager.maybe_summarize(
        session_id,
        generation_id=13,
        is_generation_current=lambda _generation_id: backend.current,
    )

    assert result is None
    assert store.get_summary(session_id) is None
    assert len(store.unsummarized_complete_turns(session_id)) == 13
    assert "final but incomplete" not in repr(backend.requests[0])
