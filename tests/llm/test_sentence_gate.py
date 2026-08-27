import pytest

from lune.llm.contracts import GenerationLLMTextFrame
from lune.llm.sentence_gate import SentenceGate


def _frame(
    text: str, *, generation_id: int = 4, attempt_id: str = "attempt"
) -> GenerationLLMTextFrame:
    return GenerationLLMTextFrame(
        text=text,
        generation_id=generation_id,
        attempt_id=attempt_id,
    )


PUNCTUATION_FIXTURES = [
    f"第{index}題。這是第二句\uff01這是第三句\uff1f第四句不應放行。" for index in range(50)
] + [
    f"Case {index}. This is sentence two! Is this sentence three? Sentence four stays out."
    for index in range(50)
]


@pytest.mark.parametrize("streamed", PUNCTUATION_FIXTURES)
def test_one_hundred_bilingual_fixtures_release_exactly_three_sentences(streamed: str) -> None:
    gate = SentenceGate()
    released = ""
    reached_limit = False
    for offset in range(0, len(streamed), 2):
        result = gate.feed(_frame(streamed[offset : offset + 2]))
        released += "".join(frame.text for frame in result.frames)
        reached_limit = reached_limit or result.reached_limit
    released += "".join(frame.text for frame in gate.finish().frames)

    assert gate.released_sentences == 3
    assert reached_limit
    assert "第四句" not in released
    assert "Sentence four" not in released
    assert released.endswith(("\uff1f", "? ", "?"))


def test_sentence_gate_keeps_decimal_and_closing_quotes_inside_sentence() -> None:
    gate = SentenceGate()
    streamed = "版本 5.6 很穩。」「第二句\uff01」「第三句\uff1f」第四句。"
    result = gate.feed(_frame(streamed))

    assert (
        "".join(frame.text for frame in result.frames)
        == "版本 5.6 很穩。」「第二句\uff01」「第三句\uff1f」"
    )
    assert result.reached_limit


def test_provider_completion_flushes_one_unpunctuated_remainder() -> None:
    gate = SentenceGate()
    assert gate.feed(_frame("A complete sentence. ")).frames
    assert gate.feed(_frame("short remainder")).frames == ()

    finished = gate.finish()

    assert [frame.text for frame in finished.frames] == ["short remainder"]
    assert gate.released_sentences == 2


def test_sentence_gate_rejects_mixed_attempts() -> None:
    gate = SentenceGate()
    gate.feed(_frame("first"))
    with pytest.raises(ValueError, match="cannot mix"):
        gate.feed(_frame("second", attempt_id="late-attempt"))
