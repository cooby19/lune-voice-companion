import pytest
from pipecat.services.openai.responses.llm import OpenAIResponsesLLMService

from lune.llm.provider import LLMProviderFactory, OpenAIResponsesProviderConfig


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
