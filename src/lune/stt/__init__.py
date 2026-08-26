"""Generation-fenced, final-only local speech recognition."""

from lune.stt.contracts import FinalTranscript, STTFailure, TranscriptionRequest
from lune.stt.mlx import LuneFinalOnlySTTService, build_mlx_stt
from lune.stt.model_manifest import (
    WHISPER_MODEL_ID,
    WHISPER_MODEL_PIN,
    WHISPER_MODEL_REVISION,
    ModelManifestCheck,
    check_model_manifest,
)

__all__ = [
    "WHISPER_MODEL_ID",
    "WHISPER_MODEL_PIN",
    "WHISPER_MODEL_REVISION",
    "FinalTranscript",
    "LuneFinalOnlySTTService",
    "ModelManifestCheck",
    "STTFailure",
    "TranscriptionRequest",
    "build_mlx_stt",
    "check_model_manifest",
]
