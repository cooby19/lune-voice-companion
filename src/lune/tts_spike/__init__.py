"""M0.5 safety and performance spike primitives."""

from lune.tts_spike.decision import BackendDecision, decide_default_backend
from lune.tts_spike.evaluation import SpikeEvaluation, evaluate_spike
from lune.tts_spike.manifest import PrivateManifestCheck, check_private_manifest
from lune.tts_spike.performance import (
    PerformanceGate,
    SpikeMeasurements,
    evaluate_performance,
)
from lune.tts_spike.report import SanitizedSpikeReport, write_sanitized_report
from lune.tts_spike.sandbox import SandboxCheck, probe_sandbox

__all__ = [
    "BackendDecision",
    "PerformanceGate",
    "PrivateManifestCheck",
    "SandboxCheck",
    "SanitizedSpikeReport",
    "SpikeEvaluation",
    "SpikeMeasurements",
    "check_private_manifest",
    "decide_default_backend",
    "evaluate_performance",
    "evaluate_spike",
    "probe_sandbox",
    "write_sanitized_report",
]
