"""What the on-device title backend asks the worker, and what it costs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import pytest

from lune.llm.prompt import ConversationMessage
from lune.llm.titles import TITLE_INSTRUCTION, LocalQwenTitleBackend
from lune.memory.store import StoredTurn
from lune.memory.titles import ThreadTitleRequest


class RecordingService:
    """Stand in for the loaded worker service without a process behind it."""

    def __init__(self, *, answer: str = "京都的行程") -> None:
        self.calls: list[tuple[tuple[Mapping[str, str], ...], int]] = []
        self._answer = answer

    async def complete_once(
        self,
        *,
        messages: Sequence[Mapping[str, str]],
        max_tokens: int = 32,
    ) -> str:
        self.calls.append((tuple(messages), max_tokens))
        return self._answer


def _request(*, user: str = "我想去京都", assistant: str = "那要先看機票") -> ThreadTitleRequest:
    return ThreadTitleRequest(
        generation_id=3,
        turn=StoredTurn(
            "turn-1",
            "thread-1",
            3,
            1,
            (
                ConversationMessage(role="user", content=user),
                ConversationMessage(role="assistant", content=assistant),
            ),
        ),
    )


async def test_the_title_is_one_local_completion_over_the_turn_that_earned_it() -> None:
    service = RecordingService()
    backend = LocalQwenTitleBackend(service)  # type: ignore[arg-type]

    assert await backend(_request()) == "京都的行程"

    assert len(service.calls) == 1
    messages, max_tokens = service.calls[0]
    assert max_tokens == 32
    assert messages[0] == {"role": "system", "content": TITLE_INSTRUCTION}
    # Both sides of the turn are quoted, labelled, and asked for one short title.
    assert "我想去京都" in messages[1]["content"]
    assert "那要先看機票" in messages[1]["content"]
    assert "24" in messages[1]["content"]


async def test_a_long_turn_is_quoted_only_as_far_as_a_title_needs() -> None:
    service = RecordingService()
    backend = LocalQwenTitleBackend(service, excerpt_characters=5)  # type: ignore[arg-type]

    await backend(_request(user="一二三四五六七八九十", assistant="回覆"))

    quoted = service.calls[0][0][1]["content"]
    assert "一二三四五" in quoted
    assert "六七八九十" not in quoted


async def test_an_empty_turn_is_never_sent_to_the_worker() -> None:
    service = RecordingService()
    backend = LocalQwenTitleBackend(service)  # type: ignore[arg-type]
    empty = ThreadTitleRequest(
        generation_id=1,
        turn=StoredTurn("turn-2", "thread-1", 1, 1, ()),
    )

    assert await backend(empty) == ""
    assert service.calls == []


def test_the_backend_refuses_an_excerpt_that_quotes_nothing() -> None:
    with pytest.raises(ValueError):
        LocalQwenTitleBackend(RecordingService(), excerpt_characters=0)  # type: ignore[arg-type]
