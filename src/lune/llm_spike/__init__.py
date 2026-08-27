"""M6 prerequisite: local Qwen Q4 spike primitives.

Nothing in this package installs a runtime, downloads a model or changes the process
architecture. Every gate fails closed until the corresponding evidence exists.
"""

from lune.llm_spike.cancellation import (
    CancellationGate,
    CancelObservation,
    evaluate_cancellation,
)
from lune.llm_spike.decision import LocalProviderDecision, decide_local_provider
from lune.llm_spike.model_pin import (
    BASE_MODEL_ID,
    LOCAL_LLM_PIN,
    LocalLLMManifestCheck,
    check_local_llm_manifest,
)
from lune.llm_spike.performance import (
    LatencyBudget,
    LocalLLMMeasurements,
    PerformanceGate,
    evaluate_performance,
)
from lune.llm_spike.report import SanitizedLocalLLMReport, build_sanitized_report
from lune.llm_spike.runtime import CANDIDATES, EndpointCheck, check_local_endpoint
from lune.llm_spike.thinking import ThinkingFilter, ThinkingGate, evaluate_thinking
from lune.llm_spike.tools import ToolCallGate, ToolCallValidator, evaluate_tool_calls

__all__ = [
    "BASE_MODEL_ID",
    "CANDIDATES",
    "LOCAL_LLM_PIN",
    "CancelObservation",
    "CancellationGate",
    "EndpointCheck",
    "LatencyBudget",
    "LocalLLMManifestCheck",
    "LocalLLMMeasurements",
    "LocalProviderDecision",
    "PerformanceGate",
    "SanitizedLocalLLMReport",
    "ThinkingFilter",
    "ThinkingGate",
    "ToolCallGate",
    "ToolCallValidator",
    "build_sanitized_report",
    "check_local_endpoint",
    "check_local_llm_manifest",
    "decide_local_provider",
    "evaluate_cancellation",
    "evaluate_performance",
    "evaluate_thinking",
    "evaluate_tool_calls",
]
