"""Streaming Chinese/English sentence boundary gate capped at three sentences."""

from __future__ import annotations

from dataclasses import dataclass

from lune.llm.contracts import GenerationLLMTextFrame

_TERMINATORS = frozenset("。\uff01\uff1f!?…")
_CLOSERS = frozenset("\"'”\u2019」』】\uff09)]\uff5d}")


@dataclass(frozen=True, slots=True)
class GateResult:
    frames: tuple[GenerationLLMTextFrame, ...]
    reached_limit: bool


class SentenceGate:
    """Release only complete units and request cancellation after the third."""

    def __init__(self, *, max_sentences: int = 3) -> None:
        if not 1 <= max_sentences <= 3:
            raise ValueError("sentence limit must be between one and three")
        self._max_sentences = max_sentences
        self._buffer = ""
        self._released = 0
        self._generation_id: int | None = None
        self._attempt_id: str | None = None
        self._limited = False

    @property
    def released_sentences(self) -> int:
        return self._released

    @property
    def reached_limit(self) -> bool:
        return self._limited

    def feed(self, frame: GenerationLLMTextFrame) -> GateResult:
        if self._limited:
            return GateResult((), True)
        self._check_correlation(frame)
        self._buffer += frame.text
        frames: list[GenerationLLMTextFrame] = []
        while self._released < self._max_sentences:
            boundary = _next_boundary(self._buffer, final=False)
            if boundary is None:
                break
            sentence, self._buffer = self._buffer[:boundary], self._buffer[boundary:]
            if not sentence:
                continue
            frames.append(self._frame(sentence))
            self._released += 1
            if self._released == self._max_sentences:
                self._limited = True
                self._buffer = ""
                break
        return GateResult(tuple(frames), self._limited)

    def finish(self) -> GateResult:
        """Flush a provider-complete remainder as its final sentence."""

        if self._limited or not self._buffer:
            self._buffer = ""
            return GateResult((), self._limited)
        frames: list[GenerationLLMTextFrame] = []
        while self._buffer and self._released < self._max_sentences:
            boundary = _next_boundary(self._buffer, final=True)
            if boundary is None:
                boundary = len(self._buffer)
            sentence, self._buffer = self._buffer[:boundary], self._buffer[boundary:]
            if sentence:
                frames.append(self._frame(sentence))
                self._released += 1
        self._buffer = ""
        return GateResult(tuple(frames), self._limited)

    def discard(self) -> None:
        self._buffer = ""

    def _check_correlation(self, frame: GenerationLLMTextFrame) -> None:
        if self._generation_id is None:
            self._generation_id = frame.generation_id
            self._attempt_id = frame.attempt_id
            return
        if (frame.generation_id, frame.attempt_id) != (
            self._generation_id,
            self._attempt_id,
        ):
            raise ValueError("sentence gate cannot mix generations or attempts")

    def _frame(self, text: str) -> GenerationLLMTextFrame:
        assert self._generation_id is not None
        assert self._attempt_id is not None
        return GenerationLLMTextFrame(
            text=text,
            generation_id=self._generation_id,
            attempt_id=self._attempt_id,
        )


def _next_boundary(text: str, *, final: bool) -> int | None:
    index = 0
    while index < len(text):
        char = text[index]
        terminal = char in _TERMINATORS or char == "."
        if not terminal:
            index += 1
            continue
        if char == "." and _is_decimal_point(text, index):
            index += 1
            continue

        end = index + 1
        while end < len(text) and text[end] in _TERMINATORS | {"."}:
            end += 1
        while end < len(text) and text[end] in _CLOSERS:
            end += 1
        if end == len(text) and not final:
            return None
        while end < len(text) and text[end].isspace():
            end += 1
        return end
    return None


def _is_decimal_point(text: str, index: int) -> bool:
    return (
        index > 0
        and index + 1 < len(text)
        and text[index - 1].isdigit()
        and text[index + 1].isdigit()
    )
