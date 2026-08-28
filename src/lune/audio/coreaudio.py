"""PyAudio streams pinned to the current macOS CoreAudio default devices.

The input callback performs no inference and never waits: it only copies PCM
into :class:`LocalAudioTransport`. Blocking PortAudio lifecycle and playback
calls are serialized on one private executor so AVSpeech's main run loop keeps
turning on the asyncio thread.
"""

from __future__ import annotations

import asyncio
import ctypes
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Protocol, cast

from lune.audio.devices import DeviceInfo, DeviceSnapshot
from lune.audio.transport import LocalAudioTransport
from lune.tts.contracts import PCMChunk

_CORE_AUDIO_PATH = "/System/Library/Frameworks/CoreAudio.framework/CoreAudio"
_SYSTEM_OBJECT = 1
_SCOPE_GLOBAL = 0x676C6F62  # 'glob'
_ELEMENT_MAIN = 0
_DEFAULT_INPUT_DEVICE = 0x64496E20  # 'dIn '
_DEFAULT_OUTPUT_DEVICE = 0x644F7574  # 'dOut'
_DEVICE_TRANSPORT_TYPE = 0x7472616E  # 'tran'
_TRANSPORT_BUILT_IN = 0x626C746E  # 'bltn'
_NO_DEVICE = 0


class CoreAudioDeviceError(RuntimeError):
    """Finite device/lifecycle failure that contains no device identifier."""


class UnsafeAudioOutputError(CoreAudioDeviceError):
    """Playback was refused because the selected output is built in."""


class _AudioObjectPropertyAddress(ctypes.Structure):
    _fields_ = (
        ("mSelector", ctypes.c_uint32),
        ("mScope", ctypes.c_uint32),
        ("mElement", ctypes.c_uint32),
    )


class CoreAudioPropertyReader(Protocol):
    def default_input_id(self) -> int: ...

    def default_output_id(self) -> int: ...

    def is_builtin(self, device_id: int) -> bool: ...


class NativeCoreAudioPropertyReader:
    """Read the small UInt32 CoreAudio property subset Lune needs."""

    def __init__(self, library: Any | None = None) -> None:
        core = library if library is not None else ctypes.CDLL(_CORE_AUDIO_PATH)
        getter = core.AudioObjectGetPropertyData
        getter.argtypes = (
            ctypes.c_uint32,
            ctypes.POINTER(_AudioObjectPropertyAddress),
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_void_p,
        )
        getter.restype = ctypes.c_int32
        self._getter: Callable[..., int] = getter

    def default_input_id(self) -> int:
        return self._uint32(_SYSTEM_OBJECT, _DEFAULT_INPUT_DEVICE)

    def default_output_id(self) -> int:
        return self._uint32(_SYSTEM_OBJECT, _DEFAULT_OUTPUT_DEVICE)

    def is_builtin(self, device_id: int) -> bool:
        return self._uint32(device_id, _DEVICE_TRANSPORT_TYPE) == _TRANSPORT_BUILT_IN

    def _uint32(self, object_id: int, selector: int) -> int:
        address = _AudioObjectPropertyAddress(selector, _SCOPE_GLOBAL, _ELEMENT_MAIN)
        size = ctypes.c_uint32(ctypes.sizeof(ctypes.c_uint32))
        value = ctypes.c_uint32()
        status = self._getter(
            object_id,
            ctypes.byref(address),
            0,
            None,
            ctypes.byref(size),
            ctypes.byref(value),
        )
        if status != 0 or size.value != ctypes.sizeof(ctypes.c_uint32):
            raise CoreAudioDeviceError("coreaudio_property_unavailable")
        if value.value == _NO_DEVICE:
            raise CoreAudioDeviceError("default_device_unavailable")
        return int(value.value)


class PortAudioStream(Protocol):
    def write(
        self,
        frames: bytes,
        num_frames: int | None = None,
        exception_on_underflow: bool = False,
    ) -> None: ...

    def stop_stream(self) -> None: ...

    def close(self) -> None: ...


class PortAudioHost(Protocol):
    def get_default_input_device_info(self) -> Mapping[str, object]: ...

    def get_default_output_device_info(self) -> Mapping[str, object]: ...

    def open(self, **kwargs: object) -> PortAudioStream: ...

    def terminate(self) -> None: ...


@dataclass(frozen=True, slots=True)
class PortAudioBindings:
    host: PortAudioHost
    int16_format: int
    continue_code: int
    abort_code: int
    input_overflow_flag: int
    stream_info: Callable[[tuple[int, ...]], object]


type PortAudioFactory = Callable[[], PortAudioBindings]


def _load_portaudio() -> PortAudioBindings:
    try:
        import pyaudio  # type: ignore[import-untyped]
    except ImportError as error:  # pragma: no cover - required production dependency
        raise CoreAudioDeviceError("pyaudio_unavailable") from error
    host = cast(PortAudioHost, pyaudio.PyAudio())
    return PortAudioBindings(
        host=host,
        int16_format=int(pyaudio.paInt16),
        continue_code=int(pyaudio.paContinue),
        abort_code=int(pyaudio.paAbort),
        input_overflow_flag=int(pyaudio.paInputOverflow),
        stream_info=lambda channel_map: pyaudio.PaMacCoreStreamInfo(
            flags=pyaudio.PaMacCoreStreamInfo.paMacCorePlayNice,
            channel_map=channel_map,
        ),
    )


@dataclass(frozen=True, slots=True)
class StreamOwnerHealth:
    input_failed: bool
    output_failed: bool


@dataclass(frozen=True, slots=True)
class _ResolvedDevices:
    snapshot: DeviceSnapshot
    input_index: int
    output_index: int


class CoreAudioStreamOwner:
    """Own input/output streams and implement ``AudioOutputDevice``.

    Merely constructing this object does not initialize PortAudio, enumerate a
    device, or open a stream. ``default_devices`` performs the first read-only
    query; input remains closed until ``set_microphone(True)`` and output opens
    lazily on the first safe PCM write.
    """

    def __init__(
        self,
        transport: LocalAudioTransport,
        *,
        frames_per_buffer: int = 512,
        output_block_ms: int = 20,
        portaudio_factory: PortAudioFactory = _load_portaudio,
        properties: CoreAudioPropertyReader | None = None,
    ) -> None:
        if frames_per_buffer <= 0 or output_block_ms <= 0:
            raise ValueError("audio buffer settings must be positive")
        self._transport = transport
        self._frames_per_buffer = frames_per_buffer
        self._output_block_ms = output_block_ms
        self._portaudio_factory = portaudio_factory
        self._properties = properties
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lune-coreaudio")
        self._bindings: PortAudioBindings | None = None
        self._resolved: _ResolvedDevices | None = None
        self._input_stream: PortAudioStream | None = None
        self._output_stream: PortAudioStream | None = None
        self._output_format: tuple[int, int] | None = None
        self._closed = False
        self._input_failed = threading.Event()
        self._output_failed = threading.Event()
        self._flush_requested = threading.Event()

    async def default_devices(self) -> DeviceSnapshot:
        resolved = cast(_ResolvedDevices, await self._run(self._query_defaults))
        self._resolved = resolved
        return resolved.snapshot

    async def rebuild_streams(self, snapshot: DeviceSnapshot) -> None:
        self._flush_requested.set()
        await self._run(self._rebuild_sync, snapshot)

    async def set_microphone(self, enabled: bool) -> None:
        await self._run(self._set_microphone_sync, enabled)

    async def write(self, chunk: PCMChunk) -> None:
        try:
            await self._run(self._write_sync, chunk)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._output_failed.set()
            raise

    async def flush(self) -> None:
        # Set this before entering the executor: a large blocking PCM write is
        # split into short blocks and observes the fence between blocks.
        self._flush_requested.set()
        await self._run(self._close_output_sync)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._flush_requested.set()
        try:
            await self._run(self._close_sync, allow_closed=True)
        finally:
            self._executor.shutdown(wait=True, cancel_futures=True)

    def consume_health(self) -> StreamOwnerHealth:
        health = StreamOwnerHealth(
            input_failed=self._input_failed.is_set(),
            output_failed=self._output_failed.is_set(),
        )
        self._input_failed.clear()
        self._output_failed.clear()
        return health

    async def _run(
        self,
        function: Callable[..., object],
        *args: object,
        allow_closed: bool = False,
    ) -> Any:
        if self._closed and not allow_closed:
            raise CoreAudioDeviceError("stream_owner_closed")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, function, *args)

    def _get_bindings(self) -> PortAudioBindings:
        if self._bindings is None:
            self._bindings = self._portaudio_factory()
        return self._bindings

    def _get_properties(self) -> CoreAudioPropertyReader:
        if self._properties is None:
            self._properties = NativeCoreAudioPropertyReader()
        return self._properties

    def _query_defaults(self) -> _ResolvedDevices:
        bindings = self._get_bindings()
        properties = self._get_properties()
        input_id = properties.default_input_id()
        output_id = properties.default_output_id()
        input_info = bindings.host.get_default_input_device_info()
        output_info = bindings.host.get_default_output_device_info()
        # Re-read the IDs so a switch between CoreAudio and PortAudio queries is
        # rejected rather than composing a mismatched snapshot.
        if input_id != properties.default_input_id() or output_id != properties.default_output_id():
            raise CoreAudioDeviceError("default_device_changed_during_query")
        return _ResolvedDevices(
            snapshot=DeviceSnapshot(
                input=DeviceInfo(
                    uid=f"coreaudio:{input_id}",
                    name=_device_name(input_info),
                    is_builtin=properties.is_builtin(input_id),
                ),
                output=DeviceInfo(
                    uid=f"coreaudio:{output_id}",
                    name=_device_name(output_info),
                    is_builtin=properties.is_builtin(output_id),
                ),
            ),
            input_index=_device_index(input_info),
            output_index=_device_index(output_info),
        )

    def _rebuild_sync(self, snapshot: DeviceSnapshot) -> None:
        resolved = self._resolved
        if resolved is None or resolved.snapshot != snapshot:
            raise CoreAudioDeviceError("unresolved_device_snapshot")
        self._close_input_sync()
        self._close_output_sync()
        self._input_failed.clear()
        self._output_failed.clear()

    def _set_microphone_sync(self, enabled: bool) -> None:
        if not enabled:
            self._close_input_sync()
            return
        resolved = self._require_resolved()
        if resolved.snapshot.output.is_builtin:
            raise UnsafeAudioOutputError("unsafe_output")
        if self._input_stream is not None:
            return
        bindings = self._get_bindings()

        def callback(
            pcm: bytes | None,
            frame_count: int,
            time_info: Mapping[str, float],
            status_flags: int,
        ) -> tuple[None, int]:
            del time_info
            expected = frame_count * self._transport.channels * 2
            if (
                pcm is None
                or len(pcm) != expected
                or bool(status_flags & bindings.input_overflow_flag)
            ):
                self._transport.mark_discontinuity()
                self._input_failed.set()
                return None, bindings.abort_code
            if not self._transport.audio_callback(pcm):
                self._input_failed.set()
                return None, bindings.abort_code
            return None, bindings.continue_code

        try:
            self._input_stream = bindings.host.open(
                format=bindings.int16_format,
                channels=self._transport.channels,
                rate=self._transport.sample_rate,
                input=True,
                input_device_index=resolved.input_index,
                frames_per_buffer=self._frames_per_buffer,
                input_host_api_specific_stream_info=bindings.stream_info(
                    tuple(range(self._transport.channels))
                ),
                stream_callback=callback,
                start=True,
            )
        except Exception as error:
            self._input_failed.set()
            raise CoreAudioDeviceError("input_stream_open_failed") from error

    def _write_sync(self, chunk: PCMChunk) -> None:
        resolved = self._require_resolved()
        if resolved.snapshot.output.is_builtin:
            raise UnsafeAudioOutputError("unsafe_output")
        output_format = (chunk.sample_rate, chunk.channels)
        if self._output_stream is None or self._output_format != output_format:
            self._close_output_sync()
            bindings = self._get_bindings()
            try:
                self._output_stream = bindings.host.open(
                    format=bindings.int16_format,
                    channels=chunk.channels,
                    rate=chunk.sample_rate,
                    output=True,
                    output_device_index=resolved.output_index,
                    frames_per_buffer=0,
                    output_host_api_specific_stream_info=bindings.stream_info(
                        tuple(range(chunk.channels))
                    ),
                    start=True,
                )
            except Exception as error:
                raise CoreAudioDeviceError("output_stream_open_failed") from error
            self._output_format = output_format
        stream = self._output_stream
        assert stream is not None
        try:
            bytes_per_frame = 2 * chunk.channels
            block_frames = max(1, chunk.sample_rate * self._output_block_ms // 1_000)
            block_bytes = block_frames * bytes_per_frame
            for offset in range(0, len(chunk.data), block_bytes):
                if self._flush_requested.is_set():
                    break
                block = chunk.data[offset : offset + block_bytes]
                stream.write(
                    block,
                    num_frames=len(block) // bytes_per_frame,
                    exception_on_underflow=True,
                )
        except Exception as error:
            self._close_output_sync()
            raise CoreAudioDeviceError("output_stream_write_failed") from error

    def _require_resolved(self) -> _ResolvedDevices:
        if self._resolved is None:
            raise CoreAudioDeviceError("default_devices_unresolved")
        return self._resolved

    def _close_input_sync(self) -> None:
        stream = self._input_stream
        self._input_stream = None
        _close_stream(stream)

    def _close_output_sync(self) -> None:
        stream = self._output_stream
        self._output_stream = None
        self._output_format = None
        _close_stream(stream)
        self._flush_requested.clear()

    def _close_sync(self) -> None:
        self._close_input_sync()
        self._close_output_sync()
        bindings = self._bindings
        self._bindings = None
        if bindings is not None:
            bindings.host.terminate()


def _close_stream(stream: PortAudioStream | None) -> None:
    if stream is None:
        return
    with suppress(Exception):
        stream.stop_stream()
    with suppress(Exception):
        stream.close()


def _device_index(info: Mapping[str, object]) -> int:
    value = info.get("index")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CoreAudioDeviceError("invalid_portaudio_device")
    index = int(value)
    if index < 0 or float(value) != index:
        raise CoreAudioDeviceError("invalid_portaudio_device")
    return index


def _device_name(info: Mapping[str, object]) -> str:
    value = info.get("name")
    if not isinstance(value, str) or not value.strip():
        raise CoreAudioDeviceError("invalid_portaudio_device")
    # Names remain in memory and are never emitted by this module.
    return value.strip()
