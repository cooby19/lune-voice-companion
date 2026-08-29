from pathlib import Path

import pytest
from pipecat.services.openai.responses.llm import OpenAIResponsesLLMService

from lune.llm.local_qwen import LocalQwenLLMService
from lune.llm.provider import (
    LLMProviderFactory,
    LocalQwenProviderConfig,
    OpenAIResponsesProviderConfig,
)


@pytest.mark.asyncio
async def test_openai_registry_builds_independent_websocket_services_with_safe_params() -> None:
    config = OpenAIResponsesProviderConfig(
        api_key="test-key-not-a-secret",
        system_instruction="private instruction",
    )
    assert "test-key-not-a-secret" not in repr(config)
    assert "private instruction" not in repr(config)

    pair = LLMProviderFactory().build_openai_pair(
        api_key="test-key-not-a-secret",
        system_instruction="private instruction",
    )
    assert isinstance(pair.primary, OpenAIResponsesLLMService)
    assert isinstance(pair.fallback, OpenAIResponsesLLMService)
    assert pair.primary is not pair.fallback

    primary = pair.primary
    fallback = pair.fallback
    primary_params = primary._build_response_params({"input": []})
    fallback_params = fallback._build_response_params({"input": []})

    assert primary_params == {
        "model": "gpt-5.6-terra",
        "stream": True,
        "store": False,
        "input": [],
        "max_output_tokens": 192,
        "service_tier": "default",
        "reasoning": {"effort": "none"},
        "include": ["reasoning.encrypted_content"],
    }
    assert fallback_params["model"] == "gpt-5.6-luna"
    assert fallback_params["store"] is False
    assert LLMProviderFactory().capabilities("openai_responses").remote_cancel

    await primary.cleanup()
    await fallback.cleanup()


def test_local_registry_builds_the_on_device_service_without_touching_the_disk() -> None:
    config = LocalQwenProviderConfig(
        model_dir=Path("/nonexistent/qwen-local"),
        runtime_python=Path("/nonexistent/qwen-runtime/bin/python"),
        system_instruction="private instruction",
    )
    assert "private instruction" not in repr(config)
    assert config.model == "qwen3.5-4b-q4-local"

    service = LLMProviderFactory().build(config)
    assert isinstance(service, LocalQwenLLMService)

    capabilities = LLMProviderFactory().capabilities("local_qwen")
    assert capabilities.function_calling is True
    assert capabilities.usage_reporting is True
    # The spike could not demonstrate a cancel landing before generation ended in
    # every trial, so this stays unclaimed rather than assumed.
    assert capabilities.remote_cancel is False


def test_the_local_config_requires_a_persona_and_the_m3_output_bound() -> None:
    paths = {
        "model_dir": Path("/nonexistent/qwen-local"),
        "runtime_python": Path("/nonexistent/qwen-runtime/bin/python"),
    }
    with pytest.raises(ValueError):
        LocalQwenProviderConfig(**paths, system_instruction="")
    with pytest.raises(ValueError):
        LocalQwenProviderConfig(**paths, system_instruction="persona", max_output_tokens=0)
