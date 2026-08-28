from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from lune.tts.contracts import TTSBackendError
from lune.tts.runloop import MainRunLoopPump, RunLoopUnavailable

_RUN_HANDLED_SOURCE = 4
_RUN_FINISHED = 1


class FakeCoreFoundation:
    """Stand in for Core Foundation so pump behaviour is deterministic."""

    kCFRunLoopDefaultMode = "default"

    def __init__(self, *, handled: int = 0) -> None:
        self.calls: list[tuple[Any, float, bool]] = []
        self._remaining_handled = handled

    def CFRunLoopRunInMode(
        self,
        mode: Any,
        seconds: float,
        return_after_source_handled: bool,
    ) -> int:
        self.calls.append((mode, seconds, return_after_source_handled))
        if self._remaining_handled > 0:
            self._remaining_handled -= 1
            return _RUN_HANDLED_SOURCE
        return _RUN_FINISHED


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


@pytest.mark.asyncio
async def test_submitted_work_runs_inline_and_starts_the_pump() -> None:
    core = FakeCoreFoundation()
    pump = MainRunLoopPump(interval_s=0.001, idle_interval_s=0.002, core=core)
    ran: list[int] = []

    pump.submit(lambda: ran.append(1))

    assert ran == [1]
    assert pump.running is True
    await asyncio.sleep(0.02)
    assert core.calls
    assert core.calls[0] == ("default", 0, False)
    pump.close()
    assert pump.running is False


@pytest.mark.asyncio
async def test_the_pump_drains_without_blocking_the_event_loop() -> None:
    core = FakeCoreFoundation(handled=5)
    pump = MainRunLoopPump(interval_s=0.001, idle_interval_s=0.5, core=core)
    pump.submit(lambda: None)

    ticks = 0
    for _ in range(30):
        await asyncio.sleep(0.001)
        ticks += 1

    assert ticks == 30
    assert pump.drains >= 5
    pump.close()


@pytest.mark.asyncio
async def test_an_idle_pump_backs_off_instead_of_spinning() -> None:
    core = FakeCoreFoundation()
    pump = MainRunLoopPump(interval_s=0.001, idle_interval_s=1.0, core=core)
    pump.submit(lambda: None)
    await asyncio.sleep(0.05)
    idle_drains = pump.drains

    await asyncio.sleep(0.05)
    assert pump.drains == idle_drains

    pump.submit(lambda: None)
    await asyncio.sleep(0.01)
    assert pump.drains > idle_drains
    pump.close()


@pytest.mark.asyncio
async def test_a_closed_pump_refuses_more_work() -> None:
    pump = MainRunLoopPump(core=FakeCoreFoundation())
    pump.submit(lambda: None)
    pump.close()
    pump.close()
    with pytest.raises(RunLoopUnavailable):
        pump.submit(lambda: None)


def test_the_pump_needs_a_running_asyncio_loop() -> None:
    pump = MainRunLoopPump(core=FakeCoreFoundation())
    with pytest.raises(RunLoopUnavailable):
        pump.submit(lambda: None)


def test_intervals_are_validated() -> None:
    with pytest.raises(ValueError):
        MainRunLoopPump(interval_s=0.0, core=FakeCoreFoundation())
    with pytest.raises(ValueError):
        MainRunLoopPump(interval_s=0.05, idle_interval_s=0.01, core=FakeCoreFoundation())


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
