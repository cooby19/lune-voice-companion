"""The one automatic thread title: when it is attempted, and when it is dropped."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from lune.memory.store import DEFAULT_CONVERSATION_TITLE, MemoryStore, StoreChange
from lune.memory.titles import ThreadTitleManager, ThreadTitleRequest
from tests.memory.conftest import complete_turn

START = datetime(2026, 8, 30, 9, tzinfo=UTC)


class RecordingTitler:
    def __init__(self, *, title: str = "週末的旅行計畫", error: Exception | None = None) -> None:
        self.requests: list[ThreadTitleRequest] = []
        self.current = True
        self.cancel_on_call = False
        self._title = title
        self._error = error

    async def __call__(self, request: ThreadTitleRequest) -> str:
        self.requests.append(request)
        if self.cancel_on_call:
            self.current = False
        if self._error is not None:
            raise self._error
        return self._title

    def fence(self, _generation_id: int) -> bool:
        return self.current


def _titled_thread(store: MemoryStore, thread_id: str) -> tuple[str, str]:
    thread = store.get_conversation_thread(thread_id)
    assert thread is not None
    return thread.title, thread.title_source


async def test_the_first_completed_turn_is_named_once_from_that_turn_alone(
    store: MemoryStore,
) -> None:
    thread_id = store.start_session("thread-first-turn", at=START)
    complete_turn(store, thread_id, 1, user="我想去京都", assistant="那要看看機票")
    backend = RecordingTitler()
    manager = ThreadTitleManager(store, backend)

    title = await manager.maybe_title(
        thread_id, generation_id=1, is_generation_current=backend.fence
    )

    assert title == "週末的旅行計畫"
    assert _titled_thread(store, thread_id) == ("週末的旅行計畫", "generated")
    assert len(backend.requests) == 1
    request = backend.requests[0]
    assert request.generation_id == 1
    assert [message.content for message in request.turn.messages] == ["我想去京都", "那要看看機票"]
    # The title is asked of the model that already holds this conversation, so
    # naming a thread never opens a request of its own.
    assert request.model == "qwen3.5-4b-q4-local"


async def test_a_named_thread_is_never_asked_for_a_second_title(store: MemoryStore) -> None:
    thread_id = store.start_session("thread-once", at=START)
    complete_turn(store, thread_id, 1)
    backend = RecordingTitler()
    manager = ThreadTitleManager(store, backend)
    assert await manager.maybe_title(
        thread_id, generation_id=1, is_generation_current=backend.fence
    )

    complete_turn(store, thread_id, 2)
    second = await manager.maybe_title(
        thread_id, generation_id=2, is_generation_current=backend.fence
    )

    assert second is None
    assert len(backend.requests) == 1
    # A rename still wins, and it too is never re-generated over.
    store.rename_conversation_thread(thread_id, "我改的標題", at=START + timedelta(minutes=1))
    assert (
        await manager.maybe_title(thread_id, generation_id=3, is_generation_current=backend.fence)
        is None
    )
    assert _titled_thread(store, thread_id) == ("我改的標題", "manual")
    assert len(backend.requests) == 1


async def test_nothing_is_spent_before_a_turn_completes_or_after_the_first_one(
    store: MemoryStore,
) -> None:
    thread_id = store.start_session("thread-not-due", at=START)
    pending = store.begin_turn(thread_id, 1, at=START)
    store.accept_user_transcript(pending, "還沒完成的一輪", at=START)
    backend = RecordingTitler()
    manager = ThreadTitleManager(store, backend)

    assert (
        await manager.maybe_title(thread_id, generation_id=1, is_generation_current=backend.fence)
        is None
    )

    # Two completed turns mean the first round is long gone; a thread that was
    # never named then stays on its default title rather than being named late.
    complete_turn(store, thread_id, 2)
    complete_turn(store, thread_id, 3)
    assert (
        await manager.maybe_title(thread_id, generation_id=3, is_generation_current=backend.fence)
        is None
    )
    assert backend.requests == []
    assert _titled_thread(store, thread_id) == (DEFAULT_CONVERSATION_TITLE, "default")


async def test_a_cancelled_generation_leaves_the_default_title(store: MemoryStore) -> None:
    thread_id = store.start_session("thread-cancelled", at=START)
    complete_turn(store, thread_id, 1, user="不該變成標題的話")
    backend = RecordingTitler()
    backend.cancel_on_call = True
    changes: list[StoreChange] = []
    store.set_change_listener(changes.append)
    manager = ThreadTitleManager(store, backend)

    assert (
        await manager.maybe_title(thread_id, generation_id=1, is_generation_current=backend.fence)
        is None
    )

    assert _titled_thread(store, thread_id) == (DEFAULT_CONVERSATION_TITLE, "default")
    # The fence moved after the model answered, so nothing was written and the
    # UI was never told about a title that does not exist.
    assert changes == []


async def test_a_failing_backend_is_indistinguishable_from_no_title(store: MemoryStore) -> None:
    thread_id = store.start_session("thread-failure", at=START)
    complete_turn(store, thread_id, 1)
    backend = RecordingTitler(error=RuntimeError("worker died"))
    manager = ThreadTitleManager(store, backend)

    assert (
        await manager.maybe_title(thread_id, generation_id=1, is_generation_current=backend.fence)
        is None
    )
    assert _titled_thread(store, thread_id) == (DEFAULT_CONVERSATION_TITLE, "default")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("「京都的行程」", "京都的行程"),
        ('  "Weekend plans."  ', "Weekend plans"),
        ("標題\uff1a買菜清單\n（這一輪在討論採買）", "標題\uff1a買菜清單"),
        ("一二三四五六七八九十" * 3, "一二三四五六七八九十" * 2 + "一二三四"),
        ("   \n\n  ", None),
        ("。", None),
    ],
)
async def test_free_model_output_becomes_one_short_printable_line(
    store: MemoryStore, raw: str, expected: str | None
) -> None:
    thread_id = store.start_session("thread-cleanup", at=START)
    complete_turn(store, thread_id, 1)
    backend = RecordingTitler(title=raw)
    manager = ThreadTitleManager(store, backend)

    title = await manager.maybe_title(
        thread_id, generation_id=1, is_generation_current=backend.fence
    )

    assert title == expected
    stored_title, source = _titled_thread(store, thread_id)
    if expected is None:
        assert (stored_title, source) == (DEFAULT_CONVERSATION_TITLE, "default")
    else:
        assert (stored_title, source) == (expected, "generated")


async def test_a_negative_generation_is_rejected_rather_than_guessed(store: MemoryStore) -> None:
    thread_id = store.start_session("thread-negative", at=START)
    complete_turn(store, thread_id, 1)
    manager = ThreadTitleManager(store, RecordingTitler())

    with pytest.raises(ValueError):
        await manager.maybe_title(
            thread_id, generation_id=-1, is_generation_current=lambda _generation: True
        )
