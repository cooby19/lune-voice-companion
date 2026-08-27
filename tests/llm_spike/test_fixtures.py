from __future__ import annotations

import pytest

from lune.llm_spike.fixtures import (
    EN_PROMPTS,
    MIXED_PROMPTS,
    MULTI_TURN_PROMPTS,
    ZH_PROMPTS,
    stability_prompts,
)
from lune.llm_spike.performance import MIN_STABILITY_TURNS


def test_each_language_group_is_populated() -> None:
    assert len(ZH_PROMPTS) >= 5
    assert len(EN_PROMPTS) >= 5
    assert len(MIXED_PROMPTS) >= 5
    assert len(MULTI_TURN_PROMPTS) == len(ZH_PROMPTS) + len(EN_PROMPTS) + len(MIXED_PROMPTS)


def test_mixed_prompts_really_mix_scripts() -> None:
    for prompt in MIXED_PROMPTS:
        assert any("一" <= character <= "鿿" for character in prompt)
        assert any(character.isascii() and character.isalpha() for character in prompt)


def test_english_prompts_carry_no_han_characters() -> None:
    for prompt in EN_PROMPTS:
        assert not any("一" <= character <= "鿿" for character in prompt)


def test_stability_run_fills_every_turn() -> None:
    prompts = stability_prompts(MIN_STABILITY_TURNS)
    assert len(prompts) == MIN_STABILITY_TURNS
    assert set(prompts) <= set(MULTI_TURN_PROMPTS)
    assert set(prompts) == set(MULTI_TURN_PROMPTS)


def test_negative_turn_count_is_rejected() -> None:
    with pytest.raises(ValueError, match="turn count"):
        stability_prompts(-1)
