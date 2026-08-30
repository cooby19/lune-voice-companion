from __future__ import annotations

import asyncio
import builtins
import ctypes
import threading
from collections.abc import Callable, Mapping
from typing import cast

import pytest

from lune.audio.coreaudio import (
    CoreAudioDeviceError,
    CoreAudioStreamOwner,
    MicrophonePermissionError,
    NativeCoreAudioPropertyReader,
    NativeMicrophoneAuthorizer,
    PortAudioBindings,
    UnsafeAudioOutputError,
)
from lune.audio.transport import LocalAudioTransport
from lune.tts.contracts import PCMChunk


class FakeStream:
    def __init__(self) -> None:
        self.writes: list[tuple[bytes, int | None, bool]] = []
        self.stopped = False
        self.closed = False

    def write(
        self,
        frames: bytes,
        num_frames: int | None = None,
        exception_on_underflow: bool = False,
    ) -> None:
        self.writes.append((frames, num_frames, exception_on_underflow))

    def stop_stream(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True


class AllowMicrophone:
    def __init__(self) -> None:
        self.calls = 0

    async def authorize(self) -> None:
        self.calls += 1


class FakeCaptureDevice:
    status = 3
    grant = True
    requests = 0

    @classmethod
    def authorizationStatusForMediaType_(cls, media_type: object) -> int:
        assert media_type == "audio"
        return cls.status

    @classmethod
    def requestAccessForMediaType_completionHandler_(
        cls, media_type: object, completion: Callable[[bool], None]
    ) -> None:
        assert media_type == "audio"
        cls.requests += 1
        completion(cls.grant)


class FakeAVFoundation:
    AVCaptureDevice = FakeCaptureDevice
    AVMediaTypeAudio = "audio"
    AVAuthorizationStatusNotDetermined = 0
    AVAuthorizationStatusAuthorized = 3


class BlockingStream(FakeStream):
    def __init__(self) -> None:
        super().__init__()
        self.first_write = threading.Event()
        self.release_write = threading.Event()

    def write(
        self,
        frames: bytes,
        num_frames: int | None = None,
        exception_on_underflow: bool = False,
    ) -> None:
        super().write(frames, num_frames, exception_on_underflow)
        if len(self.writes) == 1:
            self.first_write.set()
            assert self.release_write.wait(timeout=2)


class FakePropertyGetter:
    def __init__(self) -> None:
        self.argtypes: object = None
        self.restype: object = None
        self.calls: list[tuple[int, int]] = []

    def __call__(
        self,
        object_id: int,
        address_pointer: object,
        qualifier_size: int,
        qualifier: object,
        size_pointer: object,
        value_pointer: object,
    ) -> int:
        del qualifier_size, qualifier
        address = ctypes.cast(address_pointer, ctypes.POINTER(ctypes.c_uint32 * 3)).contents
        selector = int(address[0])
        self.calls.append((object_id, selector))
        values = {
            (1, 0x64496E20): 101,
            (1, 0x644F7574): 202,
            (101, 0x7472616E): 0x626C746E,
            (202, 0x7472616E): 0x626C7565,
        }
        ctypes.cast(size_pointer, ctypes.POINTER(ctypes.c_uint32)).contents.value = 4
        ctypes.cast(value_pointer, ctypes.POINTER(ctypes.c_uint32)).contents.value = values[
            (object_id, selector)
        ]
        return 0


class FakeCoreAudioLibrary:
    def __init__(self) -> None:
        self.AudioObjectGetPropertyData = FakePropertyGetter()


class FakeHost:
    def __init__(self, stream_factory: Callable[[], FakeStream] = FakeStream) -> None:
        self.opens: list[dict[str, object]] = []
        self.streams: list[FakeStream] = []
        self.terminated = False
        self._stream_factory = stream_factory

    def get_default_input_device_info(self) -> Mapping[str, object]:
        return {"index": 3, "name": "Default input"}

    def get_default_output_device_info(self) -> Mapping[str, object]:
        return {"index": 8, "name": "Headphones"}

    def open(self, **kwargs: object) -> FakeStream:
        stream = self._stream_factory()
        self.opens.append(kwargs)
        self.streams.append(stream)
        return stream

    def terminate(self) -> None:
        self.terminated = True


class FakeProperties:
    def __init__(self, *, output_builtin: bool = False) -> None:
        self.output_builtin = output_builtin
        self.input_ids = [101]
        self.output_ids = [202]

    def default_input_id(self) -> int:
        return self.input_ids.pop(0) if len(self.input_ids) > 1 else self.input_ids[0]

    def default_output_id(self) -> int:
        return self.output_ids.pop(0) if len(self.output_ids) > 1 else self.output_ids[0]

    def is_builtin(self, device_id: int) -> bool:
        return device_id == 101 or self.output_builtin


def bindings(host: FakeHost) -> PortAudioBindings:
    return PortAudioBindings(
        host=host,
        int16_format=8,
        continue_code=0,
        abort_code=2,
        input_overflow_flag=2,
        stream_info=lambda channel_map: ("map", channel_map),
    )


def test_native_property_reader_uses_the_documented_coreaudio_selectors() -> None:
    library = FakeCoreAudioLibrary()
    reader = NativeCoreAudioPropertyReader(library)

    assert reader.default_input_id() == 101
    assert reader.default_output_id() == 202
    assert reader.is_builtin(101) is True
    assert reader.is_builtin(202) is False


@pytest.mark.asyncio
async def test_microphone_authorizer_requests_only_when_status_is_not_determined() -> None:
    FakeCaptureDevice.status = 3
    FakeCaptureDevice.requests = 0
    await NativeMicrophoneAuthorizer(FakeAVFoundation()).authorize()
    assert FakeCaptureDevice.requests == 0

    FakeCaptureDevice.status = 0
    FakeCaptureDevice.grant = True
    await NativeMicrophoneAuthorizer(FakeAVFoundation()).authorize()
    assert FakeCaptureDevice.requests == 1


def test_microphone_status_reads_the_decision_without_ever_prompting() -> None:
    """Onboarding reads this on every refresh, so it must not be a request.

    The prompt is shown once by macOS and never again, so a status read that
    quietly asked for access would spend that one chance behind the user's
    back.
    """

    FakeCaptureDevice.requests = 0
    authorizer = NativeMicrophoneAuthorizer(FakeAVFoundation())

    FakeCaptureDevice.status = 3
    assert authorizer.status() == "authorized"
    FakeCaptureDevice.status = 0
    assert authorizer.status() == "undetermined"
    FakeCaptureDevice.status = 2
    assert authorizer.status() == "denied"
    # Restricted is not the user's decision either, and this process cannot
    # change it, so it reads the same as denied.
    FakeCaptureDevice.status = 1
    assert authorizer.status() == "denied"

    assert FakeCaptureDevice.requests == 0


@pytest.mark.asyncio
async def test_a_missing_framework_reads_as_unavailable_and_still_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The status read reports it; the request still refuses."""

    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "AVFoundation":
            raise ImportError("no AVFoundation here")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert NativeMicrophoneAuthorizer().status() == "unavailable"
    with pytest.raises(MicrophonePermissionError, match="microphone_authorization_unavailable"):
        await NativeMicrophoneAuthorizer().authorize()


@pytest.mark.asyncio
async def test_microphone_authorizer_fails_closed_when_access_is_denied() -> None:
    FakeCaptureDevice.status = 2
    with pytest.raises(MicrophonePermissionError, match="microphone_permission_denied"):
        await NativeMicrophoneAuthorizer(FakeAVFoundation()).authorize()

    FakeCaptureDevice.status = 0
    FakeCaptureDevice.grant = False
    with pytest.raises(MicrophonePermissionError, match="microphone_permission_denied"):
        await NativeMicrophoneAuthorizer(FakeAVFoundation()).authorize()


@pytest.mark.asyncio
async def test_construction_and_close_never_initialize_or_open_portaudio() -> None:
    calls = 0

    def factory() -> PortAudioBindings:
        nonlocal calls
        calls += 1
        return bindings(FakeHost())

    owner = CoreAudioStreamOwner(LocalAudioTransport(), portaudio_factory=factory)
    assert calls == 0
    await owner.close()
    assert calls == 0


@pytest.mark.asyncio
async def test_default_query_resolves_indices_without_opening_a_stream() -> None:
    host = FakeHost()
    owner = CoreAudioStreamOwner(
        LocalAudioTransport(),
        portaudio_factory=lambda: bindings(host),
        properties=FakeProperties(),
    )

    snapshot = await owner.default_devices()

    assert snapshot.input.uid == "coreaudio:101"
    assert snapshot.output.uid == "coreaudio:202"
    assert snapshot.output.is_builtin is False
    assert host.opens == []
    await owner.rebuild_streams(snapshot)
    assert host.opens == []
    cold = owner.status()
    assert cold.host_active is True
    assert cold.input_open is False
    assert cold.output_open is False
    await owner.close()
    assert host.terminated is True
    closed = owner.status()
    assert closed.closed is True
    assert closed.host_active is False
    assert closed.input_open is False
    assert closed.output_open is False


@pytest.mark.asyncio
async def test_input_callback_only_copies_current_mono_pcm_and_aborts_on_discontinuity() -> None:
    host = FakeHost()
    transport = LocalAudioTransport(max_callbacks=2)
    authorizer = AllowMicrophone()
    owner = CoreAudioStreamOwner(
        transport,
        portaudio_factory=lambda: bindings(host),
        properties=FakeProperties(),
        microphone_authorizer=authorizer,
    )
    snapshot = await owner.default_devices()
    await owner.rebuild_streams(snapshot)
    await owner.set_microphone(True)
    assert authorizer.calls == 1
    listening = owner.status()
    assert listening.input_open is True
    assert listening.input_sample_rate == 16_000
    assert listening.input_channels == 1
    assert len(host.opens) == 1
    open_call = host.opens[0]
    assert open_call["input_device_index"] == 3
    assert open_call["rate"] == 16_000
    assert open_call["channels"] == 1
    callback = cast(
        Callable[[bytes | None, int, Mapping[str, float], int], tuple[None, int]],
        open_call["stream_callback"],
    )

    # The engine does not flip LocalAudioTransport on until stream creation succeeds.
    assert callback(b"\x00\x00" * 512, 512, {}, 0) == (None, 0)
    assert transport.read_nowait() is None
    transport.set_microphone(True)
    assert callback(b"\x01\x00" * 512, 512, {}, 0) == (None, 0)
    span = transport.read_nowait()
    assert span is not None and span.frame_count == 512

    assert callback(b"\x00\x00" * 512, 512, {}, 2) == (None, 2)
    assert transport.health().overflowed is True
    assert owner.consume_health().input_failed is True
    await owner.close()


@pytest.mark.asyncio
async def test_output_reuses_one_format_reopens_on_change_and_flushes() -> None:
    host = FakeHost()
    owner = CoreAudioStreamOwner(
        LocalAudioTransport(),
        portaudio_factory=lambda: bindings(host),
        properties=FakeProperties(),
    )
    snapshot = await owner.default_devices()
    await owner.rebuild_streams(snapshot)
    mono = PCMChunk(0, 24_000, 1, b"\x01\x00" * 12)
    stereo = PCMChunk(0, 48_000, 2, b"\x01\x00\x02\x00" * 8)

    await owner.write(mono)
    await owner.write(mono)
    assert len(host.opens) == 1
    assert host.opens[0]["output_device_index"] == 8
    assert host.streams[0].writes == [
        (mono.data, 12, False),
        (mono.data, 12, False),
    ]

    await owner.write(stereo)
    output = owner.status()
    assert output.output_open is True
    assert output.output_sample_rate == 48_000
    assert output.output_channels == 2
    assert len(host.opens) == 2
    assert host.streams[0].stopped and host.streams[0].closed
    assert host.opens[1]["output_host_api_specific_stream_info"] == ("map", (0, 1))
    assert host.streams[1].writes == [(stereo.data, 8, False)]

    await owner.flush()
    assert host.streams[1].stopped and host.streams[1].closed
    await owner.close()


@pytest.mark.asyncio
async def test_large_output_chunks_are_split_into_cancellation_sized_blocks() -> None:
    host = FakeHost()
    owner = CoreAudioStreamOwner(
        LocalAudioTransport(),
        output_block_ms=20,
        portaudio_factory=lambda: bindings(host),
        properties=FakeProperties(),
    )
    snapshot = await owner.default_devices()
    await owner.rebuild_streams(snapshot)
    chunk = PCMChunk(0, 24_000, 1, b"\x01\x00" * 1_200)

    await owner.write(chunk)

    assert [write[1] for write in host.streams[0].writes] == [480, 480, 240]
    # A device that drains faster than this writer refills it is a glitch to ride
    # out, not a reason to tear the stream down in the middle of an answer.
    assert [write[2] for write in host.streams[0].writes] == [False, False, False]
    await owner.close()


@pytest.mark.asyncio
async def test_flush_preempts_a_large_write_after_the_current_short_block() -> None:
    host = FakeHost(BlockingStream)
    owner = CoreAudioStreamOwner(
        LocalAudioTransport(),
        output_block_ms=20,
        portaudio_factory=lambda: bindings(host),
        properties=FakeProperties(),
    )
    snapshot = await owner.default_devices()
    await owner.rebuild_streams(snapshot)
    chunk = PCMChunk(0, 24_000, 1, b"\x01\x00" * 2_400)

    write_task = asyncio.create_task(owner.write(chunk))
    for _ in range(100):
        if host.streams:
            break
        await asyncio.sleep(0.001)
    assert host.streams
    stream = cast(BlockingStream, host.streams[0])
    assert await asyncio.to_thread(stream.first_write.wait, 2)
    flush_task = asyncio.create_task(owner.flush())
    await asyncio.sleep(0)
    stream.release_write.set()
    await write_task
    await flush_task

    assert len(stream.writes) == 1
    assert stream.stopped and stream.closed
    await owner.close()


@pytest.mark.asyncio
async def test_builtin_output_refuses_input_activation_and_playback() -> None:
    host = FakeHost()
    owner = CoreAudioStreamOwner(
        LocalAudioTransport(),
        portaudio_factory=lambda: bindings(host),
        properties=FakeProperties(output_builtin=True),
    )
    snapshot = await owner.default_devices()
    await owner.rebuild_streams(snapshot)
    assert snapshot.output.is_builtin is True

    with pytest.raises(UnsafeAudioOutputError):
        await owner.set_microphone(True)
    with pytest.raises(UnsafeAudioOutputError):
        await owner.write(PCMChunk(0, 24_000, 1, b"\x00\x00"))
    assert host.opens == []
    assert owner.consume_health().output_failed is True
    await owner.close()


@pytest.mark.asyncio
async def test_a_switch_during_default_query_is_rejected_without_opening() -> None:
    host = FakeHost()
    properties = FakeProperties()
    properties.output_ids = [202, 303]
    owner = CoreAudioStreamOwner(
        LocalAudioTransport(),
        portaudio_factory=lambda: bindings(host),
        properties=properties,
    )

    with pytest.raises(CoreAudioDeviceError, match="default_device_changed_during_query"):
        await owner.default_devices()
    assert host.opens == []
    await owner.close()
