"""Privacy-preserving LLM provider, streaming, and budget policy."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from lune.llm.budget import BudgetLedger
    from lune.llm.provider import LLMProviderFactory
    from lune.llm.sentence_gate import SentenceGate

__all__ = ["BudgetLedger", "LLMProviderFactory", "SentenceGate"]


def __getattr__(name: str) -> Any:
    """Keep package re-exports from importing every provider during a leaf import.

    ``MemoryStore`` only needs the budget contracts.  Eagerly importing the
    provider registry from here made that otherwise leaf import loop back
    through the local-Qwen memory proposal code before the store class existed.
    """

    if name == "BudgetLedger":
        from lune.llm.budget import BudgetLedger

        return BudgetLedger
    if name == "LLMProviderFactory":
        from lune.llm.provider import LLMProviderFactory

        return LLMProviderFactory
    if name == "SentenceGate":
        from lune.llm.sentence_gate import SentenceGate

        return SentenceGate
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
