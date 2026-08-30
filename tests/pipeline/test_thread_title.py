"""Where the automatic thread title hangs off the turn path.

The title is generated after the turn is committed and after the session is
already back to idle, so it can neither delay an answer nor hold the microphone.
It is fenced by the generation that earned the turn: a cancellation while the
model is still choosing words leaves the default title exactly as it was.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest

from lune.memory.store import DEFAULT_CONVERSATION_TITLE
from lune.memory.titles import ThreadTitleRequest
from tests.pipeline.harness import SESSION_ID, Harness, ScriptedTTSBackend, build_harness
from tests.pipeline.test_session import HEADSET, terminal, text, wait_for_state


class SlowTitler:
    """A backend the test can hold open while the pipeline keeps moving."""

    def __init__(self, *, title: str = "早晨的問候", error: Exception | None = None) -> None:
        self.requests: list[ThreadTitleRequest] = []
        self.started = asyncio.Event()
        self.release: asyncio.Event | None = None
        self.observe: Callable[[], str] | None = None
        self.states: list[str] = []
        self._title = title
        self._error = error

    async def __call__(self, request: ThreadTitleRequest) -> str:
        self.requests.append(request)
        if self.observe is not None:
            self.states.append(self.observe())
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        if self._error is not None:
            raise self._error
        return self._title


def thread_title(harness: Harness) -> tuple[str, str]:
    thread = harness.store.get_conversation_thread(SESSION_ID)
    assert thread is not None
    return thread.title, thread.title_source


async def listen(harness: Harness) -> None:
    await harness.pipeline.session.start()
    await harness.pipeline.session.apply_default_devices(HEADSET)
    assert harness.pipeline.session.set_microphone(True) == "listening"


@pytest.mark.asyncio
async def test_the_first_completed_turn_names_the_thread_and_the_second_does_not(
    tmp_path: Path,
) -> None:
    titler = SlowTitler()
    harness = build_harness(
        tmp_path,
        terra_scripts=(
            (text("早安。"), terminal()),
            (text("好的。"), terminal()),
        ),
        title_backend=titler,
    )
    titler.observe = lambda: harness.pipeline.session.state
    await listen(harness)

    await harness.speak_utterance()
    await harness.stt.emit_final("早安")
    assert await harness.pipeline.session.wait_for_turns() is True

    assert thread_title(harness) == ("早晨的問候", "generated")
    assert len(titler.requests) == 1
    assert titler.requests[0].generation_id == 0
    # The title reads the turn that earned it, not a re-derived context.
    assert [message.content for message in titler.requests[0].turn.messages] == ["早安", "早安。"]
    # The answer was never blocked on it: the session was already back to
    # listening when the model was asked for a title.
    assert titler.states == ["listening"]

    await harness.speak_utterance()
    await harness.stt.emit_final("再一次")
    assert await harness.pipeline.session.wait_for_turns() is True

    assert len(titler.requests) == 1
    assert thread_title(harness) == ("早晨的問候", "generated")
    await harness.pipeline.session.close()


@pytest.mark.asyncio
async def test_a_barge_in_while_the_title_is_generating_keeps_the_default_title(
    tmp_path: Path,
) -> None:
    titler = SlowTitler()
    titler.release = asyncio.Event()
    harness = build_harness(
        tmp_path,
        terra_scripts=(
            (text("早安。"), terminal()),
            (text("換個話題。"), terminal()),
        ),
        title_backend=titler,
    )
    await listen(harness)

    await harness.speak_utterance()
    await harness.stt.emit_final("早安")
    await asyncio.wait_for(titler.started.wait(), timeout=1.0)

    # The user speaks again while the title is still being written. The fence
    # moves, so the title that finally arrives belongs to a generation that no
    # longer exists and must not land.
    await harness.pipeline.coordinator.cancel("barge_in")
    titler.release.set()
    assert await harness.pipeline.session.wait_for_turns() is True

    assert thread_title(harness) == (DEFAULT_CONVERSATION_TITLE, "default")
    # The turn itself still stands: only the title was dropped.
    assert len(harness.store.unsummarized_complete_turns(SESSION_ID)) == 1
    await harness.pipeline.session.close()


@pytest.mark.asyncio
async def test_a_failing_titler_never_fails_the_turn(tmp_path: Path) -> None:
    titler = SlowTitler(error=RuntimeError("the worker went away"))
    harness = build_harness(
        tmp_path,
        terra_scripts=((text("早安。"), terminal()),),
        title_backend=titler,
    )
    await listen(harness)

    await harness.speak_utterance()
    await harness.stt.emit_final("早安")
    assert await harness.pipeline.session.wait_for_turns() is True

    assert thread_title(harness) == (DEFAULT_CONVERSATION_TITLE, "default")
    assert harness.pipeline.session.reports[-1].outcome == "completed"
    assert harness.pipeline.session.state == "listening"
    await harness.pipeline.session.close()


@pytest.mark.asyncio
async def test_an_interrupted_turn_is_never_named(tmp_path: Path) -> None:
    resume = asyncio.Event()
    titler = SlowTitler()
    harness = build_harness(
        tmp_path,
        terra_scripts=((text("第一個回答。"), text("不該留下。"), terminal()),),
        backend=ScriptedTTSBackend(chunks=6, pause=resume.wait),
        title_backend=titler,
    )
    await listen(harness)
    await harness.speak_utterance()
    await harness.stt.emit_final("先說一件事")
    await wait_for_state(harness, "speaking")

    await harness.pipeline.coordinator.cancel("barge_in")
    resume.set()
    assert await harness.pipeline.session.wait_for_turns() is True

    # A cancelled turn leaves no transcript, so there is nothing to name from.
    assert titler.requests == []
    assert thread_title(harness) == (DEFAULT_CONVERSATION_TITLE, "default")
    await harness.pipeline.session.close()
