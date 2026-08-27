"""Shared incremental scanning for tagged blocks in a token stream.

Both the thinking filter and the tool-call extractor face the same problem: a tag
may be split across chunks, so a suffix that could still grow into a tag must be
held back rather than released downstream.
"""

from __future__ import annotations


def held_suffix(text: str, tags: tuple[str, ...]) -> int:
    """Length of the longest suffix of ``text`` that could still become one of ``tags``."""

    if not tags:
        return 0
    limit = min(max(len(tag) for tag in tags) - 1, len(text))
    for size in range(limit, 0, -1):
        suffix = text[-size:]
        if any(tag.startswith(suffix) for tag in tags):
            return size
    return 0
