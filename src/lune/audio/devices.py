"""Default-device lifecycle and unsafe built-in-output pause policy."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

DeviceState = Literal["mic_off", "listening", "paused_unsafe_output"]
MaybeAwaitable = Awaitable[None] | None


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    uid: str
    name: str
    is_builtin: bool

    def __post_init__(self) -> None:
        if not self.uid:
            raise ValueError("device UID is required")


@dataclass(frozen=True, slots=True)
class DeviceSnapshot:
    input: DeviceInfo
    output: DeviceInfo


@dataclass(frozen=True, slots=True)
class DeviceTransition:
    changed: bool
    cancelled_generation: bool
    rebuilt_streams: bool
    state: DeviceState


async def _maybe_await(value: MaybeAwaitable) -> None:
    if inspect.isawaitable(value):
        await value


class DeviceStateMachine:
    def __init__(
        self,
        *,
        cancel_generation: Callable[[str], MaybeAwaitable],
        rebuild_streams: Callable[[DeviceSnapshot], MaybeAwaitable],
    ) -> None:
        self._cancel_generation = cancel_generation
        self._rebuild_streams = rebuild_streams
        self._snapshot: DeviceSnapshot | None = None
        self._microphone_requested = False
        self._state: DeviceState = "mic_off"

    @property
    def state(self) -> DeviceState:
        return self._state

    @property
    def snapshot(self) -> DeviceSnapshot | None:
        return self._snapshot

    def set_microphone(self, enabled: bool) -> DeviceState:
        self._microphone_requested = enabled
        if self._snapshot is not None and self._snapshot.output.is_builtin:
            self._state = "paused_unsafe_output"
        else:
            self._state = "listening" if enabled else "mic_off"
        return self._state

    async def apply_default_devices(self, snapshot: DeviceSnapshot) -> DeviceTransition:
        if snapshot == self._snapshot:
            return DeviceTransition(
                changed=False,
                cancelled_generation=False,
                rebuilt_streams=False,
                state=self._state,
            )
        had_previous = self._snapshot is not None
        if had_previous:
            await _maybe_await(self._cancel_generation("device_changed"))
        await _maybe_await(self._rebuild_streams(snapshot))
        self._snapshot = snapshot
        if snapshot.output.is_builtin:
            self._state = "paused_unsafe_output"
        else:
            self._state = "listening" if self._microphone_requested else "mic_off"
        return DeviceTransition(
            changed=True,
            cancelled_generation=had_previous,
            rebuilt_streams=True,
            state=self._state,
        )
