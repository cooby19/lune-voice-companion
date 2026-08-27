"""Public, sanitized prompt fixtures for the local LLM spike.

These carry no persona, no interview material and nothing about the owner. They exist so
prompt processing, throughput and multi-turn stability can be measured, and so public tests
can exercise the gates, without any private text entering the repository. The private
persona rubric stays separate and still needs its own authorisation.
"""

from __future__ import annotations

from typing import Final

ZH_PROMPTS: Final[tuple[str, ...]] = (
    "今天台北的天氣如何\uff1f",
    "幫我用兩句話說明什麼是快取。",
    "我等一下要去買菜\uff0c提醒我帶環保袋。",
    "你可以簡單解釋一下什麼是時區嗎\uff1f",
    "請把這段話改得更口語一點。",
)

EN_PROMPTS: Final[tuple[str, ...]] = (
    "What is the difference between a list and a tuple?",
    "Summarise how a read-through cache works in two sentences.",
    "Remind me to stretch after sitting for an hour.",
    "Explain what a time zone offset means.",
    "Rewrite this sentence so it sounds more casual.",
)

MIXED_PROMPTS: Final[tuple[str, ...]] = (
    "幫我看一下這個 function 的名字取得好不好。",
    "我想把 meeting 改到下午三點\uff0c這樣可以嗎\uff1f",
    "這個 bug 我 debug 很久了\uff0c你有什麼想法\uff1f",
    "請用中文解釋什麼是 embedding。",
    "幫我把這份 note 整理成三個重點。",
)

MULTI_TURN_PROMPTS: Final[tuple[str, ...]] = ZH_PROMPTS + EN_PROMPTS + MIXED_PROMPTS


def stability_prompts(turns: int) -> tuple[str, ...]:
    """Cycle the public fixtures to fill a stability run of `turns` turns."""

    if turns < 0:
        raise ValueError("turn count cannot be negative")
    pool = MULTI_TURN_PROMPTS
    return tuple(pool[index % len(pool)] for index in range(turns))
