from __future__ import annotations

import threading
import time
from collections.abc import Callable

import pytest

from lune.tts.contracts import TTSBackendError
from lune.tts.runloop import CFRunLoopThread, RunLoopUnavailable

pytest.importorskip("CoreFoundation", reason="Core Foundation is only present on macOS")


class RecordingRunLoop:
    def __init__(self, *, fail: bool = False) -> None:
        self.submitted: list[Callable[[], None]] = []
        self.closed = False
        self._fail = fail

    def submit(self, work: Callable[[], None]) -> None:
        if self._fail:
            raise RunLoopUnavailable("closed")
        self.submitted.append(work)

    def close(self) -> None:
        self.closed = True


def test_work_runs_on_the_thread_that_owns_the_run_loop() -> None:
    host = CFRunLoopThread(slice_s=0.005, idle_slice_s=0.005)
    done = threading.Event()
    threads: list[int] = []

    def work() -> None:
        threads.append(threading.get_ident())
        done.set()

    host.submit(work)
    assert done.wait(timeout=5.0) is True
    assert threads and threads[0] != threading.get_ident()
    host.close()
    assert host.running is False


def test_a_failing_work_item_is_counted_and_the_loop_survives() -> None:
    host = CFRunLoopThread(slice_s=0.005, idle_slice_s=0.005)
    done = threading.Event()

    def broken() -> None:
        raise RuntimeError("callback blew up")

    host.submit(broken)
    host.submit(done.set)
    assert done.wait(timeout=5.0) is True
    assert host.failed_work_items == 1
    host.close()


def test_a_closed_run_loop_refuses_more_work() -> None:
    host = CFRunLoopThread(slice_s=0.005, idle_slice_s=0.005)
    host.close()
    host.close()
    with pytest.raises(RunLoopUnavailable):
        host.submit(lambda: None)


def test_the_work_queue_is_bounded() -> None:
    host = CFRunLoopThread(slice_s=0.005, idle_slice_s=0.005, queue_capacity=1)
    blocked = threading.Event()
    host.submit(blocked.wait)
    time.sleep(0.05)
    host.submit(lambda: None)
    with pytest.raises(RunLoopUnavailable):
        host.submit(lambda: None)
    blocked.set()
    host.close()


def test_an_idle_loop_does_not_spin_a_core() -> None:
    host = CFRunLoopThread(slice_s=0.01, idle_slice_s=0.02)
    started = time.process_time()
    time.sleep(0.3)
    consumed = time.process_time() - started
    host.close()
    assert consumed < 0.15


def test_the_native_driver_routes_avfoundation_calls_through_the_run_loop() -> None:
    pytest.importorskip("AVFoundation", reason="AVFoundation is only present on macOS")
    from lune.tts.avspeech import _NativeAVSpeechDriver

    host = RecordingRunLoop()
    driver = _NativeAVSpeechDriver(host)

    driver.start("你好", "zh", lambda item: None)
    assert len(host.submitted) == 1

    driver.stop()
    assert len(host.submitted) == 2

    driver.close()
    assert host.closed is True


def test_a_dead_run_loop_is_reported_as_an_unavailable_backend() -> None:
    pytest.importorskip("AVFoundation", reason="AVFoundation is only present on macOS")
    from lune.tts.avspeech import _NativeAVSpeechDriver

    driver = _NativeAVSpeechDriver(RecordingRunLoop(fail=True))
    received: list[object] = []

    driver.start("你好", "zh", received.append)

    assert len(received) == 1
    error = received[0]
    assert isinstance(error, TTSBackendError)
    assert error.code == "backend_unavailable"
