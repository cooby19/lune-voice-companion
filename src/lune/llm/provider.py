"""Pipecat-native provider registry for Responses WebSocket and deterministic CI."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, Protocol, cast

from pipecat.frames.frames import Frame, LLMContextFrame
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.llm_service import LLMService
from pipecat.services.openai.responses.llm import OpenAIResponsesLLMService

from lune.llm.contracts import ModelName, ProviderCapabilities, ProviderName


@dataclass(frozen=True, slots=True)
class OpenAIResponsesProviderConfig:
    provider: Literal["openai_responses"] = "openai_responses"
    model: ModelName = "gpt-5.6-terra"
    api_key: str = field(default="", repr=False)
    system_instruction: str = field(default="", repr=False)
    reasoning_effort: Literal["none"] = "none"
    max_output_tokens: int = 192
    service_tier: Literal["default"] = "default"
    store: Literal[False] = False
    tracing: Literal[False] = False
    telemetry: Literal[False] = False

    def __post_init__(self) -> None:
        if not self.api_key:
            raise ValueError("an API key from the host Keychain is required")
        if not self.system_instruction:
            raise ValueError("a private persona instruction is required")
        if not 1 <= self.max_output_tokens <= 192:
            raise ValueError("M3 output limit must be between one and 192 tokens")


@dataclass(frozen=True, slots=True)
class DeterministicFakeProviderConfig:
    provider: Literal["deterministic_fake"] = "deterministic_fake"
    frames: tuple[Frame, ...] = field(default=(), repr=False)


type ProviderConfig = OpenAIResponsesProviderConfig | DeterministicFakeProviderConfig


@dataclass(frozen=True, slots=True)
class ProviderHooks:
    configure: Callable[[LLMService], None] | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ProviderPair:
    primary: LLMService
    fallback: LLMService


class ProviderBuilder(Protocol):
    def __call__(self, config: ProviderConfig, hooks: ProviderHooks) -> LLMService: ...


class DeterministicFakeLLMService(LLMService):
    """A Pipecat LLMService that emits a fixed typed script without network access."""

    def __init__(self, frames: tuple[Frame, ...]) -> None:
        super().__init__()
        self._frames = frames

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMContextFrame):
            for scripted in self._frames:
                await self.push_frame(scripted)
        else:
            await self.push_frame(frame, direction)


class LLMProviderFactory:
    """Build providers by registry name while keeping the pipeline provider-neutral."""

    _CAPABILITIES: Mapping[ProviderName, ProviderCapabilities] = MappingProxyType(
        {
            "openai_responses": ProviderCapabilities(
                function_calling=True,
                remote_cancel=True,
                usage_reporting=True,
            ),
            "deterministic_fake": ProviderCapabilities(
                function_calling=True,
                remote_cancel=True,
                usage_reporting=True,
            ),
        }
    )

    def __init__(self) -> None:
        self._builders: dict[ProviderName, ProviderBuilder] = {
            "openai_responses": _build_openai_responses,
            "deterministic_fake": _build_fake,
        }

    def capabilities(self, provider: ProviderName) -> ProviderCapabilities:
        return self._CAPABILITIES[provider]

    def build(
        self,
        config: ProviderConfig,
        hooks: ProviderHooks | None = None,
    ) -> LLMService:
        effective_hooks = hooks or ProviderHooks()
        service = self._builders[config.provider](config, effective_hooks)
        if effective_hooks.configure is not None:
            effective_hooks.configure(service)
        return service

    def build_openai_pair(
        self,
        *,
        api_key: str,
        system_instruction: str,
        max_output_tokens: int = 192,
    ) -> ProviderPair:
        primary = self.build(
            OpenAIResponsesProviderConfig(
                model="gpt-5.6-terra",
                api_key=api_key,
                system_instruction=system_instruction,
                max_output_tokens=max_output_tokens,
            )
        )
        fallback = self.build(
            OpenAIResponsesProviderConfig(
                model="gpt-5.6-luna",
                api_key=api_key,
                system_instruction=system_instruction,
                max_output_tokens=max_output_tokens,
            )
        )
        if primary is fallback:
            raise AssertionError("Terra and Luna must be independent service instances")
        return ProviderPair(primary=primary, fallback=fallback)


def _build_openai_responses(config: ProviderConfig, hooks: ProviderHooks) -> LLMService:
    del hooks
    typed = cast(OpenAIResponsesProviderConfig, config)
    settings = OpenAIResponsesLLMService.Settings(
        model=typed.model,
        system_instruction=typed.system_instruction,
        max_completion_tokens=typed.max_output_tokens,
        reasoning=OpenAIResponsesLLMService.ReasoningConfig(
            effort=typed.reasoning_effort,
        ),
    )
    return cast(
        LLMService,
        OpenAIResponsesLLMService(
            api_key=typed.api_key,
            service_tier=typed.service_tier,
            settings=settings,
        ),
    )


def _build_fake(config: ProviderConfig, hooks: ProviderHooks) -> LLMService:
    del hooks
    typed = cast(DeterministicFakeProviderConfig, config)
    return DeterministicFakeLLMService(typed.frames)
