from __future__ import annotations

import pytest

from lune.llm_spike.thinking import (
    MAX_REASONING_CHARS,
    ThinkingFilter,
    ThinkingFilterResult,
    evaluate_thinking,
)

REASONING = "chain of thought that must never leave the filter"


def drain(filter_: ThinkingFilter, chunks: tuple[str, ...]) -> tuple[str, ThinkingFilterResult]:
    text = "".join(filter_.feed(chunk).text for chunk in chunks)
    final = filter_.finish()
    return text + final.text, final


def test_plain_text_passes_through_unchanged() -> None:
    filter_ = ThinkingFilter()
    text, result = drain(filter_, ("你好\uff0c", "今天過得如何\uff1f"))
    assert text == "你好\uff0c今天過得如何\uff1f"
    assert result.clean
    assert result.violations == ()


def test_reasoning_block_is_stripped_and_recorded() -> None:
    filter_ = ThinkingFilter()
    text, result = drain(filter_, (f"<think>{REASONING}</think>實際回答。",))
    assert text == "實際回答。"
    assert REASONING not in text
    assert result.violations == ("reasoning_emitted",)
    assert not result.clean


def test_tags_split_across_chunks_still_strip() -> None:
    filter_ = ThinkingFilter()
    text, result = drain(filter_, ("<th", "ink>", REASONING, "</th", "ink>", "可見的回答。"))
    assert text == "可見的回答。"
    assert REASONING not in text
    assert result.violations == ("reasoning_emitted",)


def test_partial_tag_that_never_completes_is_released_at_finish() -> None:
    filter_ = ThinkingFilter()
    text, result = drain(filter_, ("結果是 3 < 4", ""))
    assert text == "結果是 3 < 4"
    assert result.clean


def test_text_before_reasoning_is_preserved() -> None:
    filter_ = ThinkingFilter()
    text, _ = drain(filter_, ("前面。<think>", REASONING, "</think>後面。"))
    assert text == "前面。後面。"


def test_unterminated_reasoning_leaks_nothing() -> None:
    filter_ = ThinkingFilter()
    text, result = drain(filter_, ("<think>", REASONING))
    assert text == ""
    assert set(result.violations) == {"reasoning_emitted", "unterminated_reasoning"}


def test_unopened_close_tag_is_recorded() -> None:
    filter_ = ThinkingFilter()
    text, result = drain(filter_, ("回答</think>尾巴。",))
    assert text == "回答尾巴。"
    assert result.violations == ("unopened_reasoning_close",)


def test_repeated_blocks_record_one_violation_each_kind() -> None:
    filter_ = ThinkingFilter()
    text, result = drain(filter_, ("<think>a</think>甲。<think>b</think>乙。",))
    assert text == "甲。乙。"
    assert result.violations == ("reasoning_emitted",)


def test_overflow_is_flagged_without_buffering_reasoning() -> None:
    filter_ = ThinkingFilter()
    filter_.feed("<think>")
    filter_.feed("x" * (MAX_REASONING_CHARS + 1))
    result = filter_.finish()
    assert "reasoning_overflow" in result.violations
    assert result.text == ""


def test_result_repr_never_contains_text() -> None:
    result = ThinkingFilterResult(text=REASONING, violations=())
    assert REASONING not in repr(result)


def test_feed_after_finish_is_rejected() -> None:
    filter_ = ThinkingFilter()
    filter_.finish()
    with pytest.raises(ValueError, match="closed"):
        filter_.feed("x")


def test_reset_restores_a_reusable_filter() -> None:
    filter_ = ThinkingFilter()
    drain(filter_, ("<think>x</think>甲。",))
    filter_.reset()
    text, result = drain(filter_, ("乙。",))
    assert text == "乙。"
    assert result.clean


def test_gate_requires_every_response_to_be_clean() -> None:
    clean = ThinkingFilterResult(text="甲。", violations=())
    dirty = ThinkingFilterResult(text="乙。", violations=("reasoning_emitted",))
    assert evaluate_thinking((clean, clean)).passed
    failed = evaluate_thinking((clean, dirty))
    assert not failed.passed
    assert failed.reasons == ("reasoning_emitted",)
    assert failed.responses_with_reasoning == 1


def test_gate_without_responses_is_not_a_pass() -> None:
    gate = evaluate_thinking(())
    assert not gate.evaluated
    assert not gate.passed
    assert gate.reasons == ("no_responses",)
