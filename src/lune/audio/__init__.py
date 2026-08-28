"""Local audio primitives with sample-accurate turn boundaries."""

from lune.audio.coreaudio import (
    CoreAudioDeviceError,
    CoreAudioStreamOwner,
    StreamOwnerHealth,
    UnsafeAudioOutputError,
)
from lune.audio.devices import DeviceInfo, DeviceSnapshot, DeviceStateMachine
from lune.audio.preroll import PreRollBuffer, PreRollCapture
from lune.audio.silero import SileroVoiceDetector
from lune.audio.transport import LocalAudioTransport
from lune.audio.types import AudioSpan
from lune.audio.vad import TurnEvent, TurnEventKind, TurnPolicy, TurnPolicyConfig

__all__ = [
    "AudioSpan",
    "CoreAudioDeviceError",
    "CoreAudioStreamOwner",
    "DeviceInfo",
    "DeviceSnapshot",
    "DeviceStateMachine",
    "LocalAudioTransport",
    "PreRollBuffer",
    "PreRollCapture",
    "SileroVoiceDetector",
    "StreamOwnerHealth",
    "TurnEvent",
    "TurnEventKind",
    "TurnPolicy",
    "TurnPolicyConfig",
    "UnsafeAudioOutputError",
]
