"""A worker thread whose ``CFRunLoop`` keeps turning for callback-based macOS APIs."""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from typing import Any, Final, Protocol

_RUN_FINISHED: Final[int] = 1
"""``kCFRunLoopRunFinished``: the loop had nothing to wait on and returned at once."""


class RunLoopHost(Protocol):
    def submit(self, work: Callable[[], None]) -> None: ...

    def close(self) -> None: ...


class RunLoopUnavailable(RuntimeError):
    """The Core Foundation run loop could not be started on this host."""


class CFRunLoopThread:
    """Serialize Core Foundation work onto one thread with a running run loop.

    ``AVSpeechSynthesizer.writeUtterance:toBufferCallback:`` delivers nothing
    while no run loop is turning, and Lune's engine is an asyncio process with no
    run loop of its own. Owning one here keeps the release TTS path working
    without forcing every embedder to pump Core Foundation.
    """

    def __init__(
        self,
        *,
        slice_s: float = 0.01,
        idle_slice_s: float = 0.05,
        queue_capacity: int = 64,
        start_timeout_s: float = 5.0,
        name: str = "lune-runloop",
    ) -> None:
        if slice_s <= 0 or idle_slice_s <= 0 or start_timeout_s <= 0:
            raise ValueError("run loop intervals must be positive")
        if queue_capacity < 1:
            raise ValueError("run loop queue capacity must be positive")
        self._slice_s = slice_s
        self._idle_slice_s = idle_slice_s
        self._work: queue.Queue[Callable[[], None]] = queue.Queue(maxsize=queue_capacity)
        self._stopping = threading.Event()
        self._started = threading.Event()
        self._pending = threading.Event()
        self._failure: BaseException | None = None
        self._failed_work = 0
        self._loop: Any = None
        self._core: Any = None
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()
        if not self._started.wait(timeout=start_timeout_s):
            self._stopping.set()
            raise RunLoopUnavailable("the run loop thread did not start")
        if self._failure is not None:
            raise RunLoopUnavailable("Core Foundation is unavailable") from self._failure

    @property
    def failed_work_items(self) -> int:
        return self._failed_work

    @property
    def running(self) -> bool:
        return self._thread.is_alive() and not self._stopping.is_set()

    def submit(self, work: Callable[[], None]) -> None:
        if self._stopping.is_set():
            raise RunLoopUnavailable("the run loop thread is closed")
        try:
            self._work.put_nowait(work)
        except queue.Full as error:
            raise RunLoopUnavailable("the run loop queue is saturated") from error
        self._wake()

    def close(self) -> None:
        if self._stopping.is_set():
            return
        self._stopping.set()
        self._wake()
        self._thread.join(timeout=self._idle_slice_s * 20)

    def _wake(self) -> None:
        self._pending.set()
        if self._core is not None and self._loop is not None:
            self._core.CFRunLoopStop(self._loop)

    def _run(self) -> None:
        try:
            import CoreFoundation  # type: ignore[import-untyped]
        except ImportError as error:  # pragma: no cover - macOS-only dependency
            self._failure = error
            self._started.set()
            return
        self._core = CoreFoundation
        self._loop = CoreFoundation.CFRunLoopGetCurrent()
        mode = CoreFoundation.kCFRunLoopDefaultMode
        self._started.set()
        while not self._stopping.is_set():
            handled = self._drain()
            interval = self._slice_s if handled else self._idle_slice_s
            result = CoreFoundation.CFRunLoopRunInMode(mode, interval, False)
            if result == _RUN_FINISHED and not handled:
                # With no sources attached the call returns immediately. Waiting
                # on the submit event keeps an idle synthesizer from spinning a
                # whole core while still picking new work up without delay.
                self._pending.wait(timeout=interval)
                self._pending.clear()
        self._drain()

    def _drain(self) -> bool:
        handled = False
        while True:
            try:
                work = self._work.get_nowait()
            except queue.Empty:
                return handled
            handled = True
            try:
                work()
            except Exception:
                # Work items own their own error reporting; one failing item must
                # not take the run loop down with it. The count stays observable
                # so a silently broken driver cannot look healthy.
                self._failed_work += 1
