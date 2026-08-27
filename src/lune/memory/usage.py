"""Bridge the M3 ledger to durable M4 usage rows."""

from __future__ import annotations

from lune.config import BudgetConfig
from lune.llm.budget import BudgetLedger
from lune.memory.store import MemoryStore


def persistent_budget_ledger(
    store: MemoryStore, config: BudgetConfig | None = None
) -> BudgetLedger:
    return BudgetLedger(
        config,
        confirmed_twd=store.confirmed_usage_totals(),
        settlement_sink=store.record_usage,
    )
