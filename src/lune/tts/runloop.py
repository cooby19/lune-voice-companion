"""Keep the main thread's Core Foundation run loop turning for AVSpeech."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, Final, Protocol

_RUN_HANDLED_SOURCE: Final[int] = 4
"""``kCFRunLoopRunHandledSource``: the drain actually delivered something."""


class RunLoopHost(Protocol):
    def submit(self, work: Callable[[], None]) -> None: ...

    def close(self) -> None: ...


class CoreFoundationLike(Protocol):
    """The two Core Foundation symbols the pump needs, named as they are there."""

    kCFRunLoopDefaultMode: Any

    def CFRunLoopRunInMode(
        self,
        mode: Any,
        seconds: float,
        return_after_source_handled: bool,
    ) -> int: ...


class RunLoopUnavailable(RuntimeError):
    """The Core Foundation run loop cannot be driven in this process."""


class MainRunLoopPump:
    """Drain the main run loop from inside the asyncio loop that owns it.

    Measured on the target Mac: ``AVSpeechSynthesizer`` delivers
    ``writeUtterance:toBufferCallback:`` buffers **only** on the main thread's
    run loop, whichever thread asked for them, and delivers nothing while that
    run loop is not turning. A run loop on any other thread produced zero
    buffers, so the synthesis work cannot simply be moved off the main thread.

    Lune's engine runs asyncio on the main thread, so the pump is an asyncio
    task doing a non-blocking drain rather than a second thread. It slows down
    on its own once nothing is being delivered, and a new submission wakes it
    immediately instead of waiting out the idle interval.
    """

    def __init__(
        self,
        *,
        interval_s: float = 0.005,
        idle_interval_s: float = 0.05,
        core: CoreFoundationLike | None = None,
    ) -> None:
        if interval_s <= 0 or idle_interval_s < interval_s:
            raise ValueError("the idle interval cannot be shorter than the active one")
        self._interval_s = interval_s
        self._idle_interval_s = idle_interval_s
        self._core = core if core is not None else _load_core_foundation()
        self._nudge = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._drains = 0

    @property
    def drains(self) -> int:
        """How often the run loop has been drained, for tests and diagnostics."""

        return self._drains

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def submit(self, work: Callable[[], None]) -> None:
        """Run one Core Foundation call and make sure its callbacks can arrive.

        The work runs inline: the caller is already on the loop that owns the
        run loop, and hopping threads would not change where AVSpeech delivers.
        """

        if self._closed:
            raise RunLoopUnavailable("the run loop pump is closed")
        self._start()
        self._nudge.set()
        work()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()

    def _start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError as error:
            raise RunLoopUnavailable("the run loop pump needs a running asyncio loop") from error
        self._task = asyncio.create_task(self._run(), name="lune-runloop-pump")

    async def _run(self) -> None:
        mode = self._core.kCFRunLoopDefaultMode
        while not self._closed:
            handled = self._core.CFRunLoopRunInMode(mode, 0, False) == _RUN_HANDLED_SOURCE
            self._drains += 1
            active = handled or self._nudge.is_set()
            self._nudge.clear()
            if active:
                await asyncio.sleep(self._interval_s)
                continue
            try:
                await asyncio.wait_for(self._nudge.wait(), timeout=self._idle_interval_s)
            except TimeoutError:
                continue


def _load_core_foundation() -> CoreFoundationLike:
    try:
        import CoreFoundation  # type: ignore[import-untyped]
    except ImportError as error:  # pragma: no cover - macOS-only dependency
        raise RunLoopUnavailable("Core Foundation is unavailable") from error
    return CoreFoundation  # type: ignore[no-any-return]
