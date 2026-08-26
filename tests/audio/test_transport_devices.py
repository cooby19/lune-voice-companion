from __future__ import annotations

import queue
import threading

from lune.audio.devices import DeviceInfo, DeviceSnapshot, DeviceStateMachine
from lune.audio.transport import LocalAudioTransport
from lune.audio.types import AudioSpan


class BlockingQueue(queue.Queue[AudioSpan]):
    def __init__(self) -> None:
        super().__init__(maxsize=2)
        self.put_started = threading.Event()
        self.allow_put = threading.Event()

    def put_nowait(self, item: AudioSpan) -> None:
        self.put_started.set()
        if not self.allow_put.wait(timeout=2):
            raise RuntimeError("test timed out while holding callback enqueue")
        super().put_nowait(item)


def test_microphone_starts_off() -> None:
    transport = LocalAudioTransport(max_callbacks=1)
    assert not transport.microphone_enabled
    assert transport.audio_callback(b"\x00\x00" * 160)
    assert transport.read_nowait() is None


def test_bounded_queue_overflow_requires_rebuild() -> None:
    transport = LocalAudioTransport(max_callbacks=1)
    transport.set_generation(7)
    transport.set_microphone(True)
    assert transport.audio_callback(b"\x00\x00" * 160)
    assert not transport.audio_callback(b"\x00\x00" * 160)
    health = transport.health()
    assert health.overflowed
    assert health.dropped_callbacks == 1
    transport.rebuild(generation_id=8)
    assert not transport.health().overflowed
    assert transport.read_nowait() is None
    assert not transport.microphone_enabled


def test_rebuild_cannot_leave_inflight_old_generation_audio() -> None:
    transport = LocalAudioTransport(max_callbacks=2)
    blocking_queue = BlockingQueue()
    transport._queue = blocking_queue
    transport.set_generation(7)
    transport.set_microphone(True)
    callback_result: list[bool] = []

    callback = threading.Thread(
        target=lambda: callback_result.append(transport.audio_callback(b"\x00\x00" * 160))
    )
    callback.start()
    assert blocking_queue.put_started.wait(timeout=2)
    callback_holds_state_lock = transport._state_lock.locked()

    rebuild = threading.Thread(target=lambda: transport.rebuild(generation_id=8))
    rebuild.start()
    blocking_queue.allow_put.set()
    callback.join(timeout=2)
    rebuild.join(timeout=2)

    assert callback_holds_state_lock
    assert not callback.is_alive()
    assert not rebuild.is_alive()
    assert callback_result == [True]
    assert transport.read_nowait() is None
    assert not transport.microphone_enabled


async def test_device_change_cancels_before_rebuild_and_builtin_pauses() -> None:
    calls: list[str] = []

    async def cancel(reason: str) -> None:
        calls.append(f"cancel:{reason}")

    async def rebuild(snapshot: DeviceSnapshot) -> None:
        calls.append(f"rebuild:{snapshot.output.uid}")

    state = DeviceStateMachine(cancel_generation=cancel, rebuild_streams=rebuild)
    headphones = DeviceSnapshot(
        input=DeviceInfo(uid="mic", name="Default mic", is_builtin=True),
        output=DeviceInfo(uid="phones", name="Headphones", is_builtin=False),
    )
    speakers = DeviceSnapshot(
        input=headphones.input,
        output=DeviceInfo(uid="speakers", name="Mac speakers", is_builtin=True),
    )
    first = await state.apply_default_devices(headphones)
    assert not first.cancelled_generation
    assert state.set_microphone(True) == "listening"
    second = await state.apply_default_devices(speakers)
    assert second.cancelled_generation
    assert second.state == "paused_unsafe_output"
    assert calls == [
        "rebuild:phones",
        "cancel:device_changed",
        "rebuild:speakers",
    ]


async def test_unchanged_devices_do_not_cancel_or_rebuild() -> None:
    calls: list[str] = []
    snapshot = DeviceSnapshot(
        input=DeviceInfo(uid="mic", name="Mic", is_builtin=True),
        output=DeviceInfo(uid="usb", name="USB", is_builtin=False),
    )
    state = DeviceStateMachine(
        cancel_generation=lambda reason: calls.append(reason),
        rebuild_streams=lambda devices: calls.append(devices.output.uid),
    )
    await state.apply_default_devices(snapshot)
    calls.clear()
    transition = await state.apply_default_devices(snapshot)
    assert not transition.changed
    assert calls == []
