from __future__ import annotations

import json

import pytest

from lune.llm_spike.tools import (
    MAX_TOOL_BLOCK_CHARS,
    ExtractionResult,
    ToolCallExtractor,
)

CALL = json.dumps(
    {"name": "propose_memory", "arguments": {"content": "私人內容", "category": "explicit_plan"}},
    ensure_ascii=False,
)


def drain(chunks: tuple[str, ...]) -> tuple[str, tuple[object, ...]]:
    extractor = ToolCallExtractor()
    text = ""
    calls: list[object] = []
    for chunk in chunks:
        result = extractor.feed(chunk)
        text += result.text
        calls.extend(result.tool_calls)
    final = extractor.finish()
    return text + final.text, tuple(calls) + final.tool_calls


def test_plain_text_is_untouched() -> None:
    text, calls = drain(("好的\uff0c我記住了。",))
    assert text == "好的\uff0c我記住了。"
    assert calls == ()


def test_tool_call_is_lifted_out_of_the_text() -> None:
    text, calls = drain((f"好的。<tool_call>{CALL}</tool_call>",))
    assert text == "好的。"
    assert "私人內容" not in text
    assert len(calls) == 1
    call = calls[0]
    assert call.tool_name == "propose_memory"  # type: ignore[attr-defined]
    assert not call.malformed  # type: ignore[attr-defined]


def test_tags_split_across_chunks_are_handled() -> None:
    text, calls = drain(("答覆。<tool", "_call>", CALL, "</tool", "_call>", "尾巴。"))
    assert text == "答覆。尾巴。"
    assert len(calls) == 1
    assert calls[0].tool_name == "propose_memory"  # type: ignore[attr-defined]


def test_string_arguments_are_preserved() -> None:
    payload = json.dumps({"name": "propose_affinity", "arguments": '{"delta": 1}'})
    _, calls = drain((f"<tool_call>{payload}</tool_call>",))
    assert calls[0].arguments_json == '{"delta": 1}'  # type: ignore[attr-defined]


def test_malformed_json_is_reported_not_leaked() -> None:
    text, calls = drain(("<tool_call>{not json}</tool_call>回答。",))
    assert text == "回答。"
    assert calls[0].malformed  # type: ignore[attr-defined]


def test_missing_name_is_malformed() -> None:
    _, calls = drain(('<tool_call>{"arguments": {}}</tool_call>',))
    assert calls[0].malformed  # type: ignore[attr-defined]


def test_unterminated_block_never_leaks_partial_json() -> None:
    text, calls = drain((f"回答。<tool_call>{CALL}",))
    assert text == "回答。"
    assert len(calls) == 1
    assert calls[0].malformed  # type: ignore[attr-defined]


def test_oversized_block_is_bounded() -> None:
    text, calls = drain(("<tool_call>" + "x" * (MAX_TOOL_BLOCK_CHARS + 10),))
    assert text == ""
    assert any(call.malformed for call in calls)  # type: ignore[attr-defined]


def test_partial_tag_at_end_is_released() -> None:
    text, calls = drain(("結果 a<tool",))
    assert text == "結果 a<tool"
    assert calls == ()


def test_feed_after_finish_is_rejected() -> None:
    extractor = ToolCallExtractor()
    extractor.finish()
    with pytest.raises(ValueError, match="closed"):
        extractor.feed("x")


def test_result_repr_hides_content() -> None:
    result = ExtractionResult(text="私人內容")
    assert "私人內容" not in repr(result)
