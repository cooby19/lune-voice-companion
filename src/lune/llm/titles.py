"""Name a conversation with the model that is already loaded for it.

The UI spec fixes one rule for both phases of the plan: the automatic thread
title must not cost a request of its own.  In the test phase that is literal --
the only provider is the on-device worker, so the title is generated there, for
free and without a single byte leaving the machine.
"""

from __future__ import annotations

from typing import Final

from lune.llm.local_qwen import LocalQwenLLMService
from lune.memory.titles import ThreadTitleRequest

TITLE_INSTRUCTION: Final[str] = (
    "你是對話標題產生器。讀完這一輪對話，寫出一個描述談話主題的繁體中文短標題。"
    "只輸出標題本身，不要引號、不要標點、不要說明、不要換行。"
)
_TITLE_TOKENS: Final[int] = 32
_EXCERPT_CHARACTERS: Final[int] = 400


class LocalQwenTitleBackend:
    """Generate the one automatic title on the worker that already holds the weights.

    The turn path owns this service: the call runs on its lock, emits no
    pipeline frames, and is cancelled the moment a new generation wants the
    worker.  When that happens it returns an empty string, which the title
    manager reads as "keep the default title".
    """

    def __init__(
        self,
        service: LocalQwenLLMService,
        *,
        max_tokens: int = _TITLE_TOKENS,
        excerpt_characters: int = _EXCERPT_CHARACTERS,
    ) -> None:
        if excerpt_characters < 1:
            raise ValueError("a title needs at least one character of the turn to read")
        self._service = service
        self._max_tokens = max_tokens
        self._excerpt_characters = excerpt_characters

    async def __call__(self, request: ThreadTitleRequest) -> str:
        transcript = self._transcript(request)
        if not transcript:
            return ""
        return await self._service.complete_once(
            messages=(
                {"role": "system", "content": TITLE_INSTRUCTION},
                {
                    "role": "user",
                    "content": (
                        f"以下是一輪對話。請給它一個不超過 {request.max_characters} 個字的標題。"
                        f"\n\n{transcript}"
                    ),
                },
            ),
            max_tokens=self._max_tokens,
        )

    def _transcript(self, request: ThreadTitleRequest) -> str:
        """Quote only as much of the turn as a title needs, both roles labelled."""

        labels = {"user": "使用者", "assistant": "Lune"}
        lines = []
        for message in request.turn.messages:
            speaker = labels.get(message.role, message.role)
            lines.append(f"{speaker}\uff1a{message.content[: self._excerpt_characters]}")
        return "\n".join(lines)
